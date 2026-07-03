import ctypes
import fcntl
import os
import queue
import time
import threading
import logging
import numpy as np
import cv2

logger = logging.getLogger(__name__)

# tmpfs by default: the snapshot is rewritten every snapshot_interval seconds,
# 24/7. Writing that to the SD card (/tmp is not tmpfs on Raspberry Pi OS) wears
# it out over months. /dev/shm is RAM-backed → zero SD writes. Overridable via
# config so the camera and HomeKit services always agree on the same path.
DEFAULT_SNAPSHOT_PATH = "/dev/shm/pi4cam-snapshot.jpg"


class CameraManager:
    """
    Singleton owning the Picamera2 instance.

    Provides a continuous H264 stream (piped to RtspPublisher) and a lores
    YUV420 stream (consumed by PresenceDetector). The H264 encoder runs from
    start() onwards — no on-demand start/stop needed since mediamtx relays
    the stream and the HomeKit app reads from mediamtx.

    Also writes a JPEG snapshot to the configured snapshot_path every
    snapshot_interval seconds using the main YUV420 frame directly — no H264
    decode needed.
    """

    # IR night-vision detector thresholds (lores U/V planes, uint8, neutral=128).
    # 850 nm illumination collapses chroma to a uniform residual cast: both
    # planes near-constant (low std) with a clear offset from neutral. The
    # cast's *direction* is unpredictable — under monochromatic IR the AWB has
    # no colour to work with and lands on arbitrary gains (pink, red and blue
    # casts all observed on the same rig) — so only its amplitude is tested.
    IR_CHROMA_STD_MAX = 6.0   # max std on each of U and V → "uniform chroma"
    # Full-night calibration (2026-07-03): night cast sits at +59…+81 from
    # neutral, while muted early-morning daylight shows a natural V offset of
    # ~4.4 with stds hovering near 6 — a moderate threshold of 4 would risk
    # false grayscale on dull mornings. 8 keeps margin on both sides.
    IR_CAST_MIN = 8.0         # min |mean − 128| on U or V → cast present
    # The AWB cast is *multiplicative* (scales with pixel luminance), so the
    # chroma std can be large. But no real scene under a working AWB
    # (grey-world) averages this far from neutral: an extreme mean offset is
    # conclusive on its own — no uniformity required.
    IR_CAST_STRONG = 20.0     # |mean − 128| beyond which IR is certain, any std
    # Hysteresis, counted in analysed lores frames (analysis_fps per second).
    # Exit is slower than entry so car headlights at night can't flip us back.
    IR_ENTRY_FRAMES = 15      # ≈3 s at 5 fps before switching to grayscale
    IR_EXIT_FRAMES = 50       # ≈10 s at 5 fps before returning to colour

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
        self._current_bitrate = self._bitrate
        self._rotation = int(self._cfg.get("rotation", 0))
        self._lores_w = int(self._cfg.get("lores_width", 320))
        self._lores_h = int(self._cfg.get("lores_height", 240))
        self._snapshot_interval = float(self._cfg.get("snapshot_interval", 2))
        self._snapshot_path = str(self._cfg.get("snapshot_path", DEFAULT_SNAPSHOT_PATH))
        self._full_fov = bool(self._cfg.get("full_fov", True))
        self._sharpness = float(self._cfg.get("sharpness", 1.0))
        self._contrast = float(self._cfg.get("contrast", 1.0))
        self._saturation = float(self._cfg.get("saturation", 1.0))

        # Night vision (beta): under 850 nm IR light the image is monochrome with
        # a pink cast. Detection reads the *untouched* lores chroma planes every
        # analysed frame; the effect neutralises the main frame's U/V planes in
        # the camera callback, before the H264 encoder consumes the buffer. The
        # ISP Saturation is never touched, so day/night transitions are always
        # measured on real colour data — no probe, no deadlock.
        self._ir_grayscale = bool(self._cfg.get("ir_grayscale", False))
        self._ir_mode = False
        self._ir_streak = 0
        self._ir_last_stats_log = 0.0
        self._MappedArray = None  # picamera2.MappedArray, bound in start()

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
        from picamera2 import MappedArray, Picamera2
        from picamera2.encoders import H264Encoder
        from picamera2.outputs import FileOutput

        self._MappedArray = MappedArray
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
        # Enlarge the pipe from the 64 KB default (~65 ms of video at 8 Mbps)
        # to 1 MB (~1–2 s): short scheduling hiccups of ffmpeg/mediamtx no
        # longer block the encoder's output thread. Best effort — the kernel
        # cap (/proc/sys/fs/pipe-max-size, 1 MB by default) may be lower.
        try:
            fcntl.fcntl(self._pipe_w, getattr(fcntl, "F_SETPIPE_SZ", 1031), 1 << 20)
        except OSError:
            logger.debug("Could not enlarge the H264 pipe", exc_info=True)
        # iperiod = fps → a keyframe every 1 s. Live view (-c:v copy) can only
        # render once it receives a keyframe, so the shortest practical GOP cuts
        # the time-to-first-frame. HKSV still works: the prebuffer fragments on
        # each keyframe (movflags frag_keyframe), yielding 1 s fMP4 fragments —
        # RETAIN_MS=6000 keeps 6 of them, the declared 4 s fragmentLength is a hint.
        # Cost: ~10-15 % more bitrate (more keyframes) — negligible at 4 Mbps.
        self._encoder = H264Encoder(
            bitrate=self._bitrate, iperiod=self._fps, profile="high"
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

    def set_bitrate(self, bps: int) -> int:
        """
        Change the encoder bitrate live — no restart, no keyframe disruption,
        transparent to every -c:v copy consumer. Gate-tested on the Zero 2 W:
        the rate control follows within one second (#47).

        Mechanism only: the bitrate *policy* lives in the HomeKit app, which
        sees the negotiated sessions. Clamped to [500 kbps, configured
        bitrate]. Returns the bitrate actually in effect.
        """
        bps = max(500_000, min(int(bps), self._bitrate))
        if self._encoder is None:
            return self._current_bitrate
        if bps != self._current_bitrate:
            try:
                self._apply_bitrate(bps)
                self._current_bitrate = bps
                logger.info("Encoder bitrate → %.1f Mbps (live)", bps / 1e6)
            except OSError:
                logger.warning("Live bitrate change failed", exc_info=True)
        return self._current_bitrate

    def _apply_bitrate(self, bps: int) -> None:
        """V4L2 ext-control poke on the running encoder's fd (VIDIOC_S_EXT_CTRLS).

        Constants come from picamera2's own encoder namespace so we stay in
        lockstep with whatever V4L2 binding the distro ships."""
        from picamera2.encoders import v4l2_encoder as v4l2

        ctrl = v4l2.v4l2_ext_control()
        ctrl.id = v4l2.V4L2_CID_MPEG_VIDEO_BITRATE
        ctrl.value = bps
        ctrls = v4l2.v4l2_ext_controls()
        ctrls.ctrl_class = v4l2.V4L2_CTRL_CLASS_MPEG
        ctrls.count = 1
        ctrls.controls = ctypes.pointer(ctrl)
        fcntl.ioctl(self._encoder.vd, v4l2.VIDIOC_S_EXT_CTRLS, ctrls)

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
        if self._pipe_r != -1:
            try:
                os.close(self._pipe_r)
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

        # Detector frames, throttled to analysis_fps. The IR check reads the
        # lores chroma planes here, BEFORE any neutralisation below, so night
        # mode is always measured on real colour data.
        if self._lores_interval <= 0 or now - self._last_lores_time >= self._lores_interval:
            self._last_lores_time = now
            arr = request.make_array("lores")
            if self._ir_grayscale:
                self._update_night_mode(arr)
            y_plane = arr[:self._lores_h, :self._lores_w].copy()
            with self._lores_condition:
                self._latest_lores_frame = y_plane
                self._lores_condition.notify_all()

        # Night mode: neutralise the main frame's chroma in place (U and V →
        # 128 = perfectly grey) before the H264 encoder consumes the buffer.
        # Stream and snapshot both come out grayscale; ISP stays untouched.
        if self._ir_mode and self._MappedArray is not None:
            with self._MappedArray(request, "main") as m:
                m.array[self._height:, :] = 128

        if (self._snapshot_interval > 0
                and time.monotonic() - self._last_snapshot >= self._snapshot_interval):
            self._last_snapshot = time.monotonic()
            main_arr = request.make_array("main").copy()
            try:
                self._snapshot_queue.put_nowait(main_arr)
            except queue.Full:
                pass  # previous snapshot still pending — skip this frame

    @classmethod
    def _is_ir_frame(cls, u_mean: float, u_std: float,
                     v_mean: float, v_std: float) -> bool:
        """
        Classify one frame's chroma statistics as IR night vision or not.

        Under monochromatic 850 nm light the AWB has no colour information:
        it lands on arbitrary gains (pink, red and blue casts observed on the
        same rig, varying between nights), so the cast's *direction* is
        ignored — only its amplitude matters. Two tiers:

        1. Extreme mean offset (> IR_CAST_STRONG): conclusive on its own —
           grey-world AWB never leaves a real scene's average there. No
           uniformity required: the cast is multiplicative (∝ luminance),
           so bright areas carry more chroma offset than dark ones and the
           std can be large (measured u=186 ±25 on the real rig).
        2. Moderate offset (> IR_CAST_MIN): only with uniform chroma (low
           std on both planes) — the partially-neutralised cast case.
        """
        cast = max(abs(u_mean - 128.0), abs(v_mean - 128.0))
        if cast > cls.IR_CAST_STRONG:
            # Extreme mean offset: a working AWB never leaves the frame
            # average this far from neutral — only monochromatic light does.
            # The cast being multiplicative (∝ luminance), std may be large,
            # so uniformity is deliberately not required on this tier.
            return True
        uniform = u_std < cls.IR_CHROMA_STD_MAX and v_std < cls.IR_CHROMA_STD_MAX
        return uniform and cast > cls.IR_CAST_MIN

    def _update_night_mode(self, lores_arr) -> None:
        """
        Feed one analysed lores frame to the IR detector. Reads the U/V planes
        of the planar YUV420 array — always untouched colour data, since the
        neutralisation below only ever writes to the *main* stream's buffer.
        """
        h, w = self._lores_h, self._lores_w
        quarter = h // 4  # each packed chroma plane spans h/4 array rows
        u = lores_arr[h:h + quarter, :w]
        v = lores_arr[h + quarter:h + 2 * quarter, :w]
        u_mean, u_std = float(u.mean()), float(u.std())
        v_mean, v_std = float(v.mean()), float(v.std())
        is_ir = self._is_ir_frame(u_mean, u_std, v_mean, v_std)

        # Calibration log (beta): one line per minute with the raw chroma
        # statistics, so thresholds are tuned from journalctl evidence rather
        # than assumptions about a given LED/lens/AWB combination.
        now = time.monotonic()
        if now - self._ir_last_stats_log >= 60.0:
            self._ir_last_stats_log = now
            logger.info(
                "IR stats: u=%.1f ±%.1f  v=%.1f ±%.1f  → frame=%s mode=%s",
                u_mean, u_std, v_mean, v_std,
                "IR" if is_ir else "colour",
                "night" if self._ir_mode else "day",
            )

        self._apply_ir_vote(is_ir)

    def _apply_ir_vote(self, is_ir: bool) -> None:
        """Hysteresis: flip _ir_mode only after N consecutive contrary votes."""
        if is_ir == self._ir_mode:
            self._ir_streak = 0
            return
        self._ir_streak += 1
        needed = self.IR_EXIT_FRAMES if self._ir_mode else self.IR_ENTRY_FRAMES
        if self._ir_streak >= needed:
            self._ir_mode = is_ir
            self._ir_streak = 0
            if is_ir:
                logger.info("Night vision detected → grayscale stream")
            else:
                logger.info("Daylight detected → colour stream")

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
                # In night mode the main frame's chroma was already neutralised
                # in the camera callback, so this converts to a grey BGR as-is.
                bgr = cv2.cvtColor(arr, cv2.COLOR_YUV2BGR_I420)
                thumb = cv2.resize(bgr, (1280, 720), interpolation=cv2.INTER_AREA)
                ok, buf = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 92])
                if ok:
                    tmp = self._snapshot_path + ".tmp"
                    with open(tmp, "wb") as f:
                        f.write(buf.tobytes())
                    os.replace(tmp, self._snapshot_path)
            except Exception:
                logger.debug("Snapshot write failed", exc_info=True)
