import os
import queue
import time
import threading
import logging
import numpy as np
import cv2

logger = logging.getLogger(__name__)

SNAPSHOT_PATH = "/tmp/pi4cam-snapshot.jpg"


class CameraManager:
    """
    Singleton owning the Picamera2 instance.

    Provides a continuous H264 stream (piped to RtspPublisher) and a lores
    YUV420 stream (consumed by PresenceDetector). The H264 encoder runs from
    start() onwards — no on-demand start/stop needed since mediamtx relays
    the stream and the HomeKit app reads from mediamtx.

    Also writes a JPEG snapshot to SNAPSHOT_PATH every snapshot_interval
    seconds using the main YUV420 frame directly — no H264 decode needed.
    """

    _instance = None
    _instance_lock = threading.Lock()

    @classmethod
    def get_instance(cls, config: dict) -> "CameraManager":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(config)
            return cls._instance

    def __init__(self, config: dict):
        self._cfg = config.get("camera", {})
        self._width = int(self._cfg.get("width", 1920))
        self._height = int(self._cfg.get("height", 1080))
        self._fps = int(self._cfg.get("fps", 30))
        self._bitrate = int(self._cfg.get("bitrate", 4_000_000))
        self._rotation = int(self._cfg.get("rotation", 0))
        self._lores_w = int(self._cfg.get("lores_width", 320))
        self._lores_h = int(self._cfg.get("lores_height", 240))
        self._snapshot_interval = float(self._cfg.get("snapshot_interval", 2))
        self._full_fov = bool(self._cfg.get("full_fov", True))
        self._sharpness = float(self._cfg.get("sharpness", 1.0))
        self._contrast = float(self._cfg.get("contrast", 1.0))
        self._saturation = float(self._cfg.get("saturation", 1.0))

        # Night vision: when the IR-cut filter is removed the whole image gets a
        # pink/magenta cast. We detect it and drop the ISP Saturation to 0 so the
        # entire pipeline (stream + snapshot) goes grayscale at zero CPU cost.
        self._ir_grayscale = bool(self._cfg.get("ir_grayscale", True))
        # Seconds between colour probes in night mode. ColourGains proved too
        # noisy with 850 nm LEDs (gains barely shift at dawn) and triggered probes
        # in rapid bursts, so exit detection is timer-only.
        self._ir_probe_interval = float(self._cfg.get("ir_probe_interval", 90))
        self._ir_mode = False
        self._ir_last_probe: float = 0.0
        self._ir_pending_check: bool = False
        self._latest_colour_gains: tuple | None = None

        self._picam2 = None
        self._encoder = None

        self._pipe_r: int = -1
        self._pipe_w: int = -1

        self._lores_condition = threading.Condition()
        self._latest_lores_frame: np.ndarray | None = None

        self._last_snapshot: float = 0.0
        self._snapshot_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=1)
        self._snapshot_thread = threading.Thread(
            target=self._snapshot_worker, daemon=True, name="snapshot-writer"
        )

        self._last_frame_time: float = 0.0
        self._watchdog_stop = threading.Event()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_run, daemon=True, name="frame-watchdog"
        )

        # Throttle lores extraction to analysis_fps so make_array("lores") is not
        # called 30×/s when the detector only needs 10×/s — biggest CPU saving.
        _det_fps = float(config.get("detection", {}).get("analysis_fps", 10))
        self._lores_interval = 1.0 / _det_fps if _det_fps > 0 else 0.0
        self._last_lores_time: float = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Configure picamera2, start H264 encoder, and begin lores capture."""
        from picamera2 import Picamera2
        from picamera2.encoders import H264Encoder
        from picamera2.outputs import FileOutput

        self._picam2 = Picamera2()

        cfg_kwargs = dict(
            main={"size": (self._width, self._height), "format": "YUV420"},
            lores={"size": (self._lores_w, self._lores_h), "format": "YUV420"},
            controls={
                "FrameRate": self._fps,
                "Sharpness": self._sharpness,
                "Contrast": self._contrast,
                "Saturation": self._saturation,
            },
        )

        # Many Pi sensors (IMX219, OV5647…) use a center-cropped readout for
        # their native 1080p mode, which narrows the lens's field of view.
        # Forcing a full-FOV (usually binned) sensor mode and letting the ISP
        # scale to the output size restores the full angle of the lens.
        if self._full_fov:
            mode = self._select_full_fov_mode()
            if mode is not None:
                cfg_kwargs["raw"] = {"size": mode["size"]}
                src_w = mode["size"][0]
                direction = "↓ downscale" if src_w >= self._width else "↑ upscale"
                logger.info(
                    "Full-FOV sensor mode: %s @ %.0f fps %s → %dx%d",
                    mode["size"], mode.get("fps", 0), direction, self._width, self._height,
                )

        video_cfg = self._picam2.create_video_configuration(**cfg_kwargs)

        if self._rotation:
            from libcamera import Transform
            transforms = {
                90:  Transform(hflip=1, vflip=0),
                180: Transform(hflip=1, vflip=1),
                270: Transform(hflip=0, vflip=1),
            }
            video_cfg["transform"] = transforms.get(self._rotation, Transform())

        self._picam2.configure(video_cfg)
        self._picam2.pre_callback = self._lores_callback

        self._pipe_r, self._pipe_w = os.pipe()
        # iperiod = 4 × fps → a keyframe every 4 s, matching HKSV's default
        # fragment length. The recording pipeline fragments on keyframes
        # (movflags frag_keyframe), so a 4 s GOP yields clean 4 s fMP4
        # fragments without any re-encoding.
        self._encoder = H264Encoder(
            bitrate=self._bitrate, iperiod=self._fps * 4, profile="high"
        )

        self._last_frame_time = time.monotonic()
        self._picam2.start()
        self._picam2.start_encoder(
            self._encoder,
            FileOutput(os.fdopen(self._pipe_w, "wb")),
        )
        self._snapshot_thread.start()
        self._watchdog_thread.start()

        logger.info(
            "Picamera2 started: %dx%d @ %d fps | lores %dx%d | bitrate %d | snapshot every %.0fs",
            self._width, self._height, self._fps,
            self._lores_w, self._lores_h, self._bitrate,
            self._snapshot_interval,
        )

    def get_h264_read_fd(self) -> int:
        """Return the read-end fd of the H264 pipe. Pass to RtspPublisher."""
        return self._pipe_r

    def get_lores_frame(self, timeout: float = 1.0) -> np.ndarray | None:
        """
        Block until a new lores Y-plane frame is available or timeout.
        Returns uint8 ndarray (lores_h, lores_w), or None on timeout.
        """
        with self._lores_condition:
            if not self._lores_condition.wait(timeout=timeout):
                return None
            return self._latest_lores_frame

    def stop(self) -> None:
        self._watchdog_stop.set()
        if self._encoder is not None:
            try:
                self._picam2.stop_encoder()
            except Exception:
                logger.debug("Encoder stop error", exc_info=True)
        if self._picam2 is not None:
            try:
                self._picam2.stop()
            except Exception:
                logger.debug("Picamera2 stop error", exc_info=True)
        for fd in (self._pipe_r,):
            try:
                if fd != -1:
                    os.close(fd)
            except OSError:
                pass
        logger.info("CameraManager stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _select_full_fov_mode(self):
        """
        Pick the best full-FOV sensor mode using a three-tier priority:

        1. Source resolution >= output resolution (true ISP downscale = supersampling)
           AND fps >= configured fps.
        2. Same downscale condition but fps >= MIN_FPS (25) — accepts a slight fps
           drop when the sensor offers a sharper high-res mode (e.g. IMX219 3280×1848
           @ 28 fps, IMX708 2304×1296 @ 56 fps).
        3. Fastest full-FOV binned mode at configured fps (fallback for sensors with
           no downscale path, e.g. OV5647 → 1296×972 @ 47 fps).

        "Full-FOV" means the widest crop_limits[2] across all sensor modes.
        Returns None on error (falls back to the default cropped readout).
        """
        MIN_FPS = 25

        try:
            modes = self._picam2.sensor_modes
        except Exception:
            logger.warning("Could not read sensor modes; using default crop", exc_info=True)
            return None
        if not modes:
            return None

        max_crop_w = max(m["crop_limits"][2] for m in modes)
        full = [m for m in modes if m["crop_limits"][2] == max_crop_w]

        # Tier 1: true downscale at target fps
        tier1 = [m for m in full
                 if m["size"][0] >= self._width and m.get("fps", 0) >= self._fps]
        if tier1:
            return max(tier1, key=lambda m: m["size"][0])

        # Tier 2: true downscale, slightly below target fps but above floor
        tier2 = [m for m in full
                 if m["size"][0] >= self._width and m.get("fps", 0) >= MIN_FPS]
        if tier2:
            return max(tier2, key=lambda m: m["size"][0])

        # Tier 3: best binned mode at target fps (upscale fallback)
        usable = [m for m in full if m.get("fps", 0) >= self._fps]
        return max(usable or full, key=lambda m: m["size"][0])

    def _watchdog_run(self) -> None:
        """Exit the process if no frame arrives within 10 s (libcamera timeout)."""
        timeout = 10.0
        while not self._watchdog_stop.wait(timeout=2.0):
            if time.monotonic() - self._last_frame_time > timeout:
                logger.critical(
                    "Frame watchdog: no frame for %.0f s — restarting (os._exit)",
                    timeout,
                )
                os._exit(1)

    def _lores_callback(self, request) -> None:
        """Called by picamera2's capture thread on every frame. Must be fast."""
        now = time.monotonic()
        self._last_frame_time = now  # always update for the watchdog

        if self._lores_interval > 0 and now - self._last_lores_time < self._lores_interval:
            return  # skip: detector doesn't need this frame yet
        self._last_lores_time = now

        arr = request.make_array("lores")
        y_plane = arr[:self._lores_h, :self._lores_w].copy()
        with self._lores_condition:
            self._latest_lores_frame = y_plane
            self._lores_condition.notify_all()

        if (self._snapshot_interval > 0
                and time.monotonic() - self._last_snapshot >= self._snapshot_interval):
            self._last_snapshot = time.monotonic()
            try:
                self._latest_colour_gains = request.get_metadata().get("ColourGains")
            except Exception:
                self._latest_colour_gains = None
            main_arr = request.make_array("main").copy()
            try:
                self._snapshot_queue.put_nowait(main_arr)
            except queue.Full:
                pass  # previous snapshot still pending — skip this frame

    @staticmethod
    def _is_infrared(bgr: np.ndarray) -> bool:
        """Return True when the frame has a strong pink/IR cast (R channel >> B channel)."""
        mean = bgr.mean(axis=(0, 1))  # [B, G, R]
        return float(mean[2]) - float(mean[0]) > 25 and float(mean[2]) > 60


    def _set_saturation(self, value: float) -> None:
        if self._picam2 is not None:
            try:
                self._picam2.set_controls({"Saturation": value})
            except Exception:
                logger.debug("set_controls(Saturation) failed", exc_info=True)

    def _update_night_mode(self, thumb: np.ndarray) -> bool:
        """
        Decide whether the current scene is infrared and keep ISP Saturation in
        sync. Returns True when the frame should be converted to grayscale.

        Exit strategy — periodic colour probe:
          1. When the timer fires, restore saturation and set _ir_pending_check.
          2. The next snapshot arrives in colour (saturation=1.0); _is_infrared()
             runs on it and decides.
          3. Saturation reverts to 0 immediately after the check.
        The live stream and snapshots show colour for at most one snapshot_interval
        (~5 s on Pi Zero 2 W) per probe — once every ir_probe_interval seconds.

        capture_array() is NOT used: calling it from a non-camera thread while the
        H264 encoder is running causes picamera2 buffer contention and CPU spikes.
        """
        if not self._ir_grayscale:
            return False

        now = time.monotonic()

        # ── Pending confirmation: this thumbnail is already in colour ─────────
        if self._ir_pending_check:
            self._ir_pending_check = False
            self._ir_last_probe = now
            if self._is_infrared(thumb):
                self._set_saturation(0.0)
                logger.debug("Night mode re-confirmed via colour probe")
            else:
                self._ir_mode = False
                self._set_saturation(0.0)
                logger.info("Daylight confirmed via colour probe → colour stream")
            return self._ir_mode

        # ── Entry: not in night mode, thumbnail is colour ────────────────────
        if not self._ir_mode:
            if self._is_infrared(thumb):
                self._ir_mode = True
                self._ir_last_probe = now
                self._set_saturation(0.0)
                logger.info(
                    "Night vision detected (ColourGains=%s) → grayscale stream",
                    tuple(round(g, 2) for g in self._latest_colour_gains)
                    if self._latest_colour_gains else None,
                )
            return self._ir_mode

        # ── In night mode: fire a probe when the timer expires ────────────────
        if (now - self._ir_last_probe) >= self._ir_probe_interval:
            self._set_saturation(self._saturation)
            self._ir_pending_check = True
            logger.info("IR exit probe (periodic) → colour check next snapshot")

        return self._ir_mode

    def _snapshot_worker(self) -> None:
        """Persistent worker: encodes and writes JPEG snapshots from the queue."""
        while True:
            try:
                arr = self._snapshot_queue.get(timeout=2.0)
            except queue.Empty:
                if self._watchdog_stop.is_set():
                    break
                continue
            try:
                bgr = cv2.cvtColor(arr, cv2.COLOR_YUV2BGR_I420)
                thumb = cv2.resize(bgr, (1280, 720), interpolation=cv2.INTER_AREA)
                if self._update_night_mode(thumb):
                    gray = cv2.cvtColor(thumb, cv2.COLOR_BGR2GRAY)
                    thumb = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
                ok, buf = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 92])
                if ok:
                    tmp = SNAPSHOT_PATH + ".tmp"
                    with open(tmp, "wb") as f:
                        f.write(buf.tobytes())
                    os.replace(tmp, SNAPSHOT_PATH)
            except Exception:
                logger.debug("Snapshot write failed", exc_info=True)
