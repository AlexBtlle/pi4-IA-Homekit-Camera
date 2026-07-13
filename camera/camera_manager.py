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
    # Day-lift auto trigger (#52), on the AEC's Lux estimate. Lux, not analogue
    # gain: the sensor's AnalogueGain max reads as the *hardware* max (63.9x on
    # OV5647) while the AEC operationally tops out ~8x, so a gain fraction never
    # fires. Lux is the AEC's absolute scene-illuminance estimate (image
    # brightness ÷ total exposure), independent of any processing we apply — no
    # feedback. Value hysteresis, thresholds from field data (this rig: dim room
    # 14-34 lux, one shutter open ~126; the lift engaged cleanly at 43 lux).
    DAY_LIFT_LUX_ENTER = 45.0   # engage the lift below this scene illuminance
    DAY_LIFT_LUX_EXIT = 90.0    # release once the scene climbs back above this
    DAY_LIFT_STREAK = 8         # consecutive confirming frames (~1.5 s at 5 fps)
    # AeExposureMode enum (libcamera): biases the AE shutter/gain split. Night
    # mode uses Long so the budget freed by a relaxed FrameDurationLimits is
    # spent on shutter time (real light) before analogue gain (noise).
    _AE_EXPOSURE_NORMAL = 0
    _AE_EXPOSURE_LONG = 2
    # NoiseReductionMode enum (libcamera draft): the ISP's hardware denoiser.
    # Video pipelines default to Fast; night mode switches to HighQuality —
    # the auto-levels stretch multiplies the gain-8x grain by ~5, and cleaning
    # it in the ISP is free (hardware) where any software denoise would eat
    # the Zero 2 W alive. Reverted to Fast by day.
    _NR_FAST = 1
    _NR_HIGH_QUALITY = 2
    # Default encoder bitrate floors for the live governor's requests (#47).
    # Field, 2026-07-05 night: at the 1 Mbps day floor the stretched night
    # image (noise across the whole frame) macroblocks into mush — two
    # screenshots seconds apart show the rate control converging from clean to
    # unusable. Night raises the floor to keep the noisy 1080p encodable; day
    # keeps the encoder's practical minimum (policy stays in the Node
    # governor). Overridable per install via camera.day_min_bitrate /
    # camera.night_min_bitrate (#53) — these are only the defaults.
    _DAY_MIN_BITRATE = 500_000
    NIGHT_MIN_BITRATE = 3_000_000
    # Night auto-levels (the digital AGC every commercial IR camera runs).
    # The scene's useful signal is found from lores-luma percentiles and
    # stretched to full range; a static curve cannot do this job — with the
    # sensor pinned at gain 8x / 41.6 ms, the whole scene lives at luma
    # ~15-50, i.e. AT the noise floor (field, 2026-07-05).
    NIGHT_LUT_LOW_PCT = 5     # percentile mapped toward black
    NIGHT_LUT_HIGH_PCT = 99   # percentile mapped to white
    NIGHT_LUT_MIN_SPAN = 48   # min stretched range: caps digital gain at ~5x
    #                           so a pitch-black room can't become pure noise
    NIGHT_STATS_EMA = 0.15    # per analysed frame (~1-2 s at 5 fps): smooths
    #                           the percentiles so the exposure never flickers

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
        # Governor floors, config-overridable (#53). Kept non-negative; the
        # ceiling (self._bitrate) always wins in _clamp_bitrate, so an
        # over-large night floor can never drive the encoder past what the
        # user allowed.
        self._day_min_bitrate = max(0, int(self._cfg.get("day_min_bitrate", self._DAY_MIN_BITRATE)))
        self._night_min_bitrate = max(0, int(self._cfg.get("night_min_bitrate", self.NIGHT_MIN_BITRATE)))
        self._rotation = int(self._cfg.get("rotation", 0))
        self._lores_w = int(self._cfg.get("lores_width", 320))
        self._lores_h = int(self._cfg.get("lores_height", 240))
        self._snapshot_interval = float(self._cfg.get("snapshot_interval", 2))
        self._snapshot_path = str(self._cfg.get("snapshot_path", DEFAULT_SNAPSHOT_PATH))
        self._full_fov = bool(self._cfg.get("full_fov", True))
        self._sharpness = float(self._cfg.get("sharpness", 1.0))
        self._contrast = float(self._cfg.get("contrast", 1.0))
        self._saturation = float(self._cfg.get("saturation", 1.0))

        # Daytime/colour brightening for dim scenes (#52) — AUTO: the night
        # auto-levels applied in colour. The scene's luma is stretched from its
        # own percentiles (p5→p99, black point anchored so it doesn't go milky)
        # and shaped by this gamma, via cv2.LUT on the main frame — engaged only
        # when the AEC's Lux says the scene is dark (see _update_day_lift).
        # ExposureValue was tried first and proved inert at this light (the AEC
        # ignores the bias — dg stays 1.0), hence the direct pixel lift. 1.0 =
        # feature off (identity). Clamped to [1, 4].
        self._day_gamma = max(1.0, min(4.0, float(self._cfg.get("day_gamma", 1.0))))
        self._day_lift_active = False   # hysteresis latch (dim-scene lift engaged)
        self._day_lift_streak = 0       # consecutive frames voting the other way
        self._day_lut: np.ndarray | None = None  # auto-levels LUT, rebuilt per frame
        self._day_low: float | None = None       # EMA'd black point (lores p5)
        self._day_high: float | None = None       # EMA'd white point (lores p99)

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

        # Night brightness — two live controls, applied only while night mode
        # is latched and reverted by day (see _apply_night_camera):
        #
        #  • ir_min_fps  — the REAL lever. At fps (e.g. 30) libcamera pins the
        #    max exposure to ~1/fps s and the AEC just piles on gain, so an EV
        #    bias saturates with no effect. Letting the framerate drop to this
        #    floor when dark lengthens the exposure instead (10 fps → 100 ms →
        #    ~3× more light) with NO added noise. < fps enables it; >= fps off.
        #  • ir_exposure — ExposureValue target bias in EV/stops (secondary
        #    fine-tune; only bites once ir_min_fps has freed shutter headroom).
        #    0.0 = libcamera default. Clamped to libcamera's ±8 range.
        self._ir_exposure = max(-8.0, min(8.0, float(self._cfg.get("ir_exposure", 0.0))))
        self._ir_min_fps = max(1, int(self._cfg.get("ir_min_fps", 10)))

        # ir_gamma — night auto-levels curve shape. Field history: the sensor
        # saturates at gain 8x / 41.6 ms (mode ceiling) long before an IR-lit
        # room meters bright, so no exposure control can help. A plain gamma
        # was milky (lifted the noise floor), a black-anchored gamma was dark
        # (the whole scene lives AT the noise floor, luma ~15-50) — a static
        # curve cannot fit both. Instead the night LUT is rebuilt from the
        # scene's own lores-luma percentiles (auto-levels, EMA-smoothed, gain
        # capped) and ir_gamma shapes the stretch: out = norm^(1/γ). The LUT
        # is applied to the main frame's luma in the night callback below.
        # 1.0 disables the whole brightening (chroma still neutralised).
        self._ir_gamma = max(1.0, min(5.0, float(self._cfg.get("ir_gamma", 2.2))))
        self._ir_gamma_lut: np.ndarray | None = None  # rebuilt per analysed frame
        self._night_low: float | None = None
        self._night_high: float | None = None

        self._picam2 = None
        self._encoder = None
        # True on VC4/VideoCore platforms (Pi 0-4): the H264 encoder is the
        # hardware V4L2 device and live bitrate/keyframe controls go through
        # ioctls on its fd. On PISP platforms (Pi 5) picamera2 substitutes a
        # software libav/x264 encoder with no V4L2 fd — detected in start().
        self._hw_encoder = True
        self._sw_bitrate_noted = False
        # HEVC multi-tier mode (#59 Volet 2, Pi 5 + new HKSV spec): the main
        # stream is captured at the High tier's size and pushed RAW through
        # the pipe; ALL encoding (3× x265 tiers + the legacy x264 stream at
        # camera.width/height) happens in HevcPublisher's single ffmpeg.
        # Explicit opt-in — never platform-detected. self._width/_height keep
        # meaning "the main frame's geometry" for every downstream consumer
        # (night LUT, snapshot, full-FOV selection); the legacy H264 output
        # geometry moves into the stream info handed to HevcPublisher.
        self._hevc_cfg = dict(self._cfg.get("hevc") or {})
        self._hevc_enabled = bool(self._hevc_cfg.get("enabled", False))
        self._legacy_width, self._legacy_height = self._width, self._height
        if self._hevc_enabled:
            _high = dict(self._hevc_cfg.get("high") or {})
            self._width = int(_high.get("width", 2560))
            self._height = int(_high.get("height", 1440))
        self._hevc_stream_info: dict | None = None
        self._hevc_keyframe_noted = False

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
                # ExposureValue starts at libcamera's 0.0 default: the day-EV
                # auto-lift (#52) engages it only once the scene proves dim,
                # and night mode drives it via ir_exposure.
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

        if self._rotation == 180:
            from libcamera import Transform
            video_cfg["transform"] = Transform(hflip=1, vflip=1)
        elif self._rotation:
            # The Pi ISP has no transposition path: libcamera.Transform only
            # does hflip/vflip. Mapping 90/270 to a single flip (what v1 did)
            # produced a silent MIRROR — worse than doing nothing (#32).
            logger.warning(
                "rotation: %d is not supported — the Pi ISP can only rotate "
                "180° (hflip+vflip). Ignoring; mount the camera upright or "
                "use rotation: 180.",
                self._rotation,
            )

        self._picam2.configure(video_cfg)
        self._picam2.pre_callback = self._lores_callback

        if self._hevc_enabled:
            # Real buffer geometry only exists after configure(): the ISP pads
            # rows to its alignment (stride ≥ width) and may pad the plane
            # height too. HevcPublisher describes the exact layout to ffmpeg's
            # rawvideo demuxer, so nothing is guessed here.
            main_cfg = self._picam2.camera_configuration()["main"]
            stride = int(main_cfg["stride"])
            framesize = int(main_cfg["framesize"])
            self._hevc_stream_info = {
                "width": self._width,
                "height": self._height,
                "stride": stride,
                "framesize": framesize,
                "fps": self._fps,
                "preset": str(self._hevc_cfg.get("preset", "ultrafast")),
                "tiers": {
                    name: dict(self._hevc_cfg.get(name) or {})
                    for name in ("high", "medium", "low")
                },
                "legacy": {
                    "width": self._legacy_width,
                    "height": self._legacy_height,
                    "bitrate": self._bitrate,
                },
            }
            logger.info(
                "HEVC mode: raw %dx%d (stride %d, framesize %d) → "
                "3-tier x265 + legacy x264 %dx%d in HevcPublisher",
                self._width, self._height, stride, framesize,
                self._legacy_width, self._legacy_height,
            )

        if self._day_gamma > 1.0:
            logger.info(
                "Day-lift armed (day_gamma=%.1f, engage ≤%.0f lux / release ≥%.0f lux)",
                self._day_gamma, self.DAY_LIFT_LUX_ENTER, self.DAY_LIFT_LUX_EXIT,
            )

        self._pipe_r, self._pipe_w = os.pipe()
        # Enlarge the pipe from the 64 KB default (~65 ms of video at 8 Mbps)
        # to 1 MB (~1–2 s): short scheduling hiccups of ffmpeg/mediamtx no
        # longer block the encoder's output thread. Best effort — the kernel
        # cap (/proc/sys/fs/pipe-max-size, 1 MB by default) may be lower.
        try:
            fcntl.fcntl(self._pipe_w, getattr(fcntl, "F_SETPIPE_SZ", 1031), 1 << 20)
        except OSError:
            logger.debug("Could not enlarge the H264 pipe", exc_info=True)
        # On non-VC4 platforms (Pi 5 / PISP) picamera2 aliases H264Encoder to
        # LibavH264Encoder — software x264, same kwargs. Its config is safe for
        # our passthrough chain (verified in picamera2 0.3.36 source):
        # tune=zerolatency (no B-frames, so wallclock stamping and -c:v copy
        # see display-order frames), gop_size=iperiod, SPS/PPS repeated.
        # profile="high" selects the "superfast" preset — the whitepaper's
        # high-quality point (~100-150 % of one A76 core at 1080p30, RP-010033).
        try:
            from picamera2.platform import Platform, get_platform
            self._hw_encoder = get_platform() == Platform.VC4
        except Exception:
            # Platform probe unavailable (old picamera2): those installs are
            # VC4-only in practice — keep the historic ioctl behaviour.
            self._hw_encoder = True

        if self._hevc_enabled:
            # HEVC mode: no picamera2 encoding at all. The base Encoder class
            # passes the mapped main frames through untouched (verified in
            # picamera2 0.3.36: Encoder._encode → outputframe(buffer)), so the
            # pipe carries raw YUV420 at the High tier's size and every encode
            # (x265 ladder + legacy x264) lives in HevcPublisher's ffmpeg.
            from picamera2.encoders import Encoder
            if self._hw_encoder:
                logger.warning(
                    "camera.hevc.enabled on a VC4 platform (Pi 0-4): 3× "
                    "software HEVC will not keep up on this hardware — "
                    "intended for the Pi 5 only"
                )
            self._encoder = Encoder(framerate=self._fps)
        else:
            # iperiod = fps → a keyframe every 1 s. Live view (-c:v copy) can
            # only render once it receives a keyframe, so the shortest
            # practical GOP cuts the time-to-first-frame. HKSV still works:
            # the prebuffer fragments on each keyframe (movflags
            # frag_keyframe), yielding 1 s fMP4 fragments — RETAIN_MS=6000
            # keeps 6 of them, the declared 4 s fragmentLength is a hint.
            # Cost: ~10-15 % more bitrate (more keyframes) — negligible at
            # 4 Mbps. framerate: nominal rate only (SPS on VC4, stream rate
            # on libav) — the publisher stamps by wallclock arrival anyway.
            self._encoder = H264Encoder(
                bitrate=self._bitrate, iperiod=self._fps, profile="high",
                framerate=self._fps,
            )

        self._last_frame_time = time.monotonic()
        self._picam2.start()
        # NOTE (#41): buffering=0 is NOT possible here — picamera2's FileOutput
        # type-gates on io.BufferedIOBase and rejects the raw FileIO that an
        # unbuffered fdopen returns (RuntimeError, field-hit: crash loop on
        # start). The default BufferedWriter stays; its per-write memcpy is
        # the price of the API.
        self._picam2.start_encoder(
            self._encoder,
            FileOutput(os.fdopen(self._pipe_w, "wb")),
        )
        self._snapshot_thread.start()
        self._watchdog_thread.start()

        logger.info(
            "Picamera2 started: %dx%d @ %d fps | lores %dx%d | bitrate %d | "
            "encoder %s | snapshot every %.0fs",
            self._width, self._height, self._fps,
            self._lores_w, self._lores_h, self._bitrate,
            "hardware (V4L2)" if self._hw_encoder else "software (libav/x264)",
            self._snapshot_interval,
        )

    def get_h264_read_fd(self) -> int:
        """Return the read-end fd of the H264 pipe. Pass to RtspPublisher."""
        return self._pipe_r

    def get_stream_read_fd(self) -> int:
        """Read-end fd of the output pipe — H264 in classic mode, raw YUV in
        HEVC mode. Same pipe either way; the name get_h264_read_fd predates
        the HEVC mode and is kept for the classic call sites."""
        return self._pipe_r

    def hevc_stream_info(self) -> dict | None:
        """Geometry/tier description for HevcPublisher — None unless
        camera.hevc.enabled (populated by start())."""
        return self._hevc_stream_info

    def _clamp_bitrate(self, bps: int) -> int:
        """Floor depends on the time of day: the stretched night image costs
        far more bits than a clean day frame (see night_min_bitrate)."""
        floor = self._night_min_bitrate if self._ir_mode else self._day_min_bitrate
        return min(max(int(bps), floor), self._bitrate)

    def set_bitrate(self, bps: int) -> int:
        """
        Change the encoder bitrate live — no restart, no keyframe disruption,
        transparent to every -c:v copy consumer. Gate-tested on the Zero 2 W:
        the rate control follows within one second (#47).

        Mechanism only: the bitrate *policy* lives in the HomeKit app, which
        sees the negotiated sessions. Clamped to [day/night floor, configured
        bitrate]. Returns the bitrate actually in effect.
        """
        bps = self._clamp_bitrate(bps)
        if self._encoder is None:
            return self._current_bitrate
        if not self._hw_encoder:
            # Pi 5: the software encoder has no V4L2 fd to poke. Accepted v1
            # trade-off (#59), same as the USB backend — the governor keeps
            # asking, the encoder keeps its configured bitrate.
            if not self._sw_bitrate_noted:
                logger.info(
                    "Dynamic bitrate is not supported by the software encoder "
                    "(Pi 5) — keeping %.1f Mbps", self._current_bitrate / 1e6,
                )
                self._sw_bitrate_noted = True
            return self._current_bitrate
        if bps != self._current_bitrate:
            try:
                self._apply_bitrate(bps)
                self._current_bitrate = bps
                logger.info("Encoder bitrate → %.1f Mbps (live)", bps / 1e6)
            except OSError:
                logger.warning("Live bitrate change failed", exc_info=True)
        return self._current_bitrate

    def force_keyframe(self) -> bool:
        """
        Ask the encoder for an immediate IDR frame (live session startup).

        A -c:v copy consumer can only render from a keyframe: forcing one when
        a viewer connects removes the 0–1 s GOP wait — the trick commercial
        cameras use for their instant startup (#43). Returns True if the
        control was accepted.
        """
        if self._encoder is None:
            return False
        if self._hevc_enabled:
            # HEVC mode: the pipe carries raw frames — keyframes belong to
            # the ffmpeg encoders and can't be requested from here. The 1 s
            # GOP keeps the live start delay bounded anyway (#43 trade-off).
            if not self._hevc_keyframe_noted:
                logger.info("force_keyframe is a no-op in HEVC mode (1 s GOP)")
                self._hevc_keyframe_noted = True
            return False
        if not self._hw_encoder:
            # Pi 5: LibavH264Encoder has a native API for this — no ioctl.
            try:
                self._encoder.force_key_frame()
                logger.info("Keyframe forced (live session start)")
                return True
            except Exception:
                logger.warning("force_key_frame() failed", exc_info=True)
                return False
        try:
            self._apply_force_keyframe()
            logger.info("Keyframe forced (live session start)")
            return True
        except OSError:
            logger.warning("Force-keyframe control rejected", exc_info=True)
            return False

    def _apply_force_keyframe(self) -> None:
        from picamera2.encoders import v4l2_encoder as v4l2

        # Fallback CID if the distro's binding doesn't name it:
        # V4L2_CTRL_CLASS_MPEG | 0x900 base + 229.
        cid = getattr(v4l2, "V4L2_CID_MPEG_VIDEO_FORCE_KEY_FRAME", 0x009909E5)
        ctrl = v4l2.v4l2_ext_control()
        ctrl.id = cid
        ctrl.value = 1
        ctrls = v4l2.v4l2_ext_controls()
        ctrls.ctrl_class = v4l2.V4L2_CTRL_CLASS_MPEG
        ctrls.count = 1
        ctrls.controls = ctypes.pointer(ctrl)
        fcntl.ioctl(self._encoder.vd, v4l2.VIDIOC_S_EXT_CTRLS, ctrls)

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

    def _apply_night_camera(self, on: bool) -> None:
        """Retune the sensor for night, reverting to daylight defaults by day.

        Live libcamera controls applied in one poke, only on the day↔night
        transition (never per frame) — no reconfigure, and the ISP Saturation
        is never touched:

        - FrameDurationLimits: relax the max frame duration so the sensor may
          slow to ir_min_fps when it's dark, lifting the ~1/fps s shutter cap
          that makes ExposureValue saturate. This is what actually adds photons,
          and it adds no gain-noise. Paired with AeExposureMode=Long so the AEC
          spends that budget on shutter time before analogue gain.
        - ExposureValue: bias the AE target up at night (only useful once the
          relaxed shutter ceiling gives the AEC room to reach it). The day-EV
          auto-lift (#52) owns this control by day; night takes it over and the
          detector is reset on the flip back, so a dim-day lift can never leak
          into night and re-evaluates from scratch once colour returns.

        Each knob is skipped when its config leaves it at the daylight default,
        so a plain install is untouched. None-guarded and wrapped so a rejected
        control can't kill the capture thread.
        """
        if self._picam2 is None:
            return
        # If a live session is holding the encoder below the night floor when
        # night falls, re-clamp it now — the governor's next request keeps
        # day behaviour after the flip back.
        if on and self._current_bitrate < self._night_min_bitrate:
            self.set_bitrate(self._current_bitrate)
        controls: dict = {}
        if self._ir_min_fps < self._fps:
            day_us = round(1e6 / self._fps)
            night_us = round(1e6 / self._ir_min_fps)
            controls["FrameDurationLimits"] = (day_us, night_us) if on else (day_us, day_us)
            controls["AeExposureMode"] = self._AE_EXPOSURE_LONG if on else self._AE_EXPOSURE_NORMAL
        if self._ir_exposure != 0.0:
            controls["ExposureValue"] = self._ir_exposure if on else 0.0
        # The day-lift (#52) bows out under night — the callback prioritises the
        # night LUT — so reset it on both transitions: it re-evaluates cleanly
        # in colour once day returns, and its ISP denoise can't linger.
        if self._day_gamma > 1.0:
            self._day_lift_active = False
            self._day_lift_streak = 0
        if self._ir_gamma > 1.0:
            # The auto-levels stretch needs the cleanest source it can get:
            # ISP hardware denoise at HighQuality while night mode is active.
            controls["NoiseReductionMode"] = (
                self._NR_HIGH_QUALITY if on else self._NR_FAST
            )
        if not controls:
            return
        try:
            self._picam2.set_controls(controls)
            logger.info("Night camera tuning %s → %s", "on" if on else "off", controls)
        except Exception:
            logger.warning("Night camera control rejected", exc_info=True)

    def _update_day_lift(self, request) -> None:
        """Auto day/colour brightening (#52): latch the gamma lift on while the
        scene is dim, off in bright light, with value hysteresis.

        The trigger is the AEC's Lux estimate (absolute scene illuminance).
        Lux, not analogue gain: the reported gain max is the sensor's hardware
        max (63.9x on OV5647), not the AEC's operational ~8x, so a gain fraction
        never fires. The latch drives the LUT applied in the capture callback;
        night owns the frame while latched, so this bows out in IR mode.
        """
        if self._ir_mode:
            return
        try:
            lux = float(request.get_metadata().get("Lux", -1.0))
        except Exception:
            return  # metadata missing — skip this frame, never crash the callback
        if lux < 0.0:
            return  # this tuning doesn't report Lux — feature stays inert
        # Value hysteresis: which state does THIS frame vote for?
        if self._day_lift_active:
            want = lux < self.DAY_LIFT_LUX_EXIT    # stay engaged until bright enough
        else:
            want = lux <= self.DAY_LIFT_LUX_ENTER  # engage only when clearly dim
        if want == self._day_lift_active:
            self._day_lift_streak = 0
            return
        # Debounce: only flip after DAY_LIFT_STREAK consecutive contrary votes.
        self._day_lift_streak += 1
        if self._day_lift_streak >= self.DAY_LIFT_STREAK:
            self._day_lift_active = want
            self._day_lift_streak = 0
            self._apply_day_lift(want, lux)

    def _apply_day_lift(self, active: bool, lux: float) -> None:
        """Toggle ISP denoise for the gamma lift (the LUT itself is applied per
        frame in the callback from the _day_lift_active latch). The digital lift
        raises the sensor's grain, so clean it in hardware at HighQuality while
        engaged, exactly as night mode does — free on the ISP. No-op in night
        mode, which owns the control (the detector already bows out)."""
        if self._picam2 is None or self._ir_mode:
            return
        nr = self._NR_HIGH_QUALITY if active else self._NR_FAST
        try:
            self._picam2.set_controls({"NoiseReductionMode": nr})
            logger.info("Day-lift %s at %.0f lux (day_gamma=%.1f)",
                        "engaged" if active else "released", lux, self._day_gamma)
        except Exception:
            logger.warning("Day-lift denoise control rejected", exc_info=True)

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
                self._update_night_mode(arr, request)
            if self._day_gamma > 1.0:
                self._update_day_lift(request)
                if self._day_lift_active:
                    self._refresh_day_lut(arr)     # track the black point every frame
                elif self._day_lut is not None:
                    self._day_lut = None           # dropped: stop applying by day
                    self._day_low = self._day_high = None
            y_plane = arr[:self._lores_h, :self._lores_w].copy()
            with self._lores_condition:
                self._latest_lores_frame = y_plane
                self._lores_condition.notify_all()

        # Brighten the main frame's luma through a gamma LUT before the H264
        # encoder consumes it. Night neutralises the chroma too (U/V → 128 =
        # grayscale); the day-lift (#52) keeps the colour. cv2.LUT writes into
        # the mapped buffer directly (dst=) — no per-frame allocation — so the
        # stream and the snapshot both inherit the brightened frame.
        if self._MappedArray is not None and (self._ir_mode or self._day_lift_active):
            with self._MappedArray(request, "main") as m:
                if self._ir_mode:
                    if self._ir_gamma_lut is not None:
                        y = m.array[:self._height]
                        cv2.LUT(y, self._ir_gamma_lut, dst=y)
                    m.array[self._height:, :] = 128        # U/V → grey (night only)
                elif self._day_lut is not None:
                    y = m.array[:self._height]
                    cv2.LUT(y, self._day_lut, dst=y)       # colour preserved

        if (self._snapshot_interval > 0
                and time.monotonic() - self._last_snapshot >= self._snapshot_interval):
            self._last_snapshot = time.monotonic()
            main_arr = request.make_array("main").copy()
            try:
                self._snapshot_queue.put_nowait(main_arr)
            except queue.Full:
                pass  # previous snapshot still pending — skip this frame

    @classmethod
    def _build_night_lut(cls, low: float, high: float, gamma: float) -> list[int]:
        """256-entry auto-levels curve: map the scene's own signal range
        [low, high] (luma percentiles) onto [0, 255], shaped by gamma.

        Below `low` (the noise pedestal) → 0: blacks stay black. Above
        `high` → 255 (top percentile clips, standard auto-levels). The span
        is floored at NIGHT_LUT_MIN_SPAN so a pitch-black scene cannot be
        stretched into pure noise (~5x max digital gain). Pure Python on
        purpose — 256 entries at analysis rate is nothing, and it stays
        testable without numpy.
        """
        span = max(high - low, float(cls.NIGHT_LUT_MIN_SPAN))
        inv = 1.0 / gamma
        lut = []
        for i in range(256):
            x = (i - low) / span
            if x <= 0.0:
                lut.append(0)
            else:
                lut.append(round(255.0 * min(1.0, x) ** inv))
        return lut

    def _refresh_night_lut(self, lores_arr) -> None:
        """Rebuild the night LUT from this frame's lores-luma statistics.

        Runs in the camera callback thread (same thread that applies the LUT,
        so the swap is race-free). The lores luma is never touched by the
        gamma (only main is), so the statistics always see the raw sensor —
        no feedback loop. EMA smoothing keeps the picture from flickering as
        scene content moves through the percentiles.
        """
        y = lores_arr[:self._lores_h, :self._lores_w]
        low = float(np.percentile(y, self.NIGHT_LUT_LOW_PCT))
        high = float(np.percentile(y, self.NIGHT_LUT_HIGH_PCT))
        if self._night_low is None or self._night_high is None:
            self._night_low, self._night_high = low, high
        else:
            a = self.NIGHT_STATS_EMA
            self._night_low += a * (low - self._night_low)
            self._night_high += a * (high - self._night_high)
        self._ir_gamma_lut = np.array(
            self._build_night_lut(self._night_low, self._night_high, self._ir_gamma),
            dtype=np.uint8,
        )

    def _refresh_day_lut(self, lores_arr) -> None:
        """Rebuild the day-lift LUT from this frame's lores-luma percentiles.

        The same auto-levels as night — anchoring the black point on the scene's
        p5 is what stops the lift going milky (a pure gamma from 0 lifts the
        noise pedestal to grey) — but shaped by day_gamma and applied to luma
        only, so the frame stays in colour. EMA-smoothed against flicker; the
        lores is never touched, so the statistics see the raw sensor.
        """
        y = lores_arr[:self._lores_h, :self._lores_w]
        low = float(np.percentile(y, self.NIGHT_LUT_LOW_PCT))
        high = float(np.percentile(y, self.NIGHT_LUT_HIGH_PCT))
        if self._day_low is None or self._day_high is None:
            self._day_low, self._day_high = low, high
        else:
            a = self.NIGHT_STATS_EMA
            self._day_low += a * (low - self._day_low)
            self._day_high += a * (high - self._day_high)
        self._day_lut = np.array(
            self._build_night_lut(self._day_low, self._day_high, self._day_gamma),
            dtype=np.uint8,
        )

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

    def _update_night_mode(self, lores_arr, request=None) -> None:
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

        # Calibration log (beta): one line every 10 minutes with the raw
        # chroma statistics, so thresholds are tuned from journalctl evidence
        # rather than assumptions about a given LED/lens/AWB combination. The
        # AE metadata tells how the sensor actually responded to the night
        # tuning (shutter really relaxed? gain pinned?) — ground truth no
        # reasoning about controls can replace. 10 min ≈ 144 lines/day:
        # journald-friendly, still plenty to calibrate a new rig overnight
        # (the full 2026-07-05 validation ran at 1/min).
        now = time.monotonic()
        if now - self._ir_last_stats_log >= 600.0:
            self._ir_last_stats_log = now
            ae = ""
            if request is not None:
                try:
                    md = request.get_metadata()
                    ae = "  exp=%.1fms gain=%.2fx dg=%.2f lux=%.0f" % (
                        md.get("ExposureTime", 0) / 1000.0,
                        md.get("AnalogueGain", 0.0),
                        md.get("DigitalGain", 0.0),
                        md.get("Lux", 0.0),
                    )
                except Exception:
                    pass  # metadata is diagnostic sugar — never block the vote
            lut_range = ""
            if self._night_low is not None and self._night_high is not None:
                lut_range = "  lut=%.0f→%.0f" % (self._night_low, self._night_high)
            logger.info(
                "IR stats: u=%.1f ±%.1f  v=%.1f ±%.1f  → frame=%s mode=%s%s%s",
                u_mean, u_std, v_mean, v_std,
                "IR" if is_ir else "colour",
                "night" if self._ir_mode else "day",
                ae, lut_range,
            )

        self._apply_ir_vote(is_ir)

        # Night auto-levels: track the scene's real signal range while night
        # mode is latched; drop the LUT (and stats) the moment day returns.
        if self._ir_mode and self._ir_gamma > 1.0:
            self._refresh_night_lut(lores_arr)
        elif self._ir_gamma_lut is not None:
            self._ir_gamma_lut = None
            self._night_low = self._night_high = None

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
            self._apply_night_camera(is_ir)

    # Snapshot thumbnail size (fixed): HomeKit resizes client-side anyway.
    _SNAP_W, _SNAP_H = 1280, 720

    def _snapshot_bgr(self, arr):
        """YUV420 main frame → 1280×720 BGR, resizing BEFORE the colour
        conversion (#42). Converting at full resolution allocated a 6 MB BGR
        image only to throw half the pixels away in the very next call —
        resizing the Y/U/V planes first roughly halves the conversion work
        and the allocation peak on every snapshot.

        I420 packed layout in the (h·3/2, w) array: each chroma plane spans
        h/4 array rows (two w/2-wide chroma rows per array row) — reshaped to
        (h/2, w/2) before resizing. In night mode U/V are already 128
        (neutralised upstream in the callback); resizing preserves that.
        Any unexpected layout (stride padding) falls back to the legacy
        full-resolution path.
        """
        h, w = self._height, self._width
        tw, th = self._SNAP_W, self._SNAP_H
        if arr.shape != (h * 3 // 2, w) or (w, h) == (tw, th):
            bgr = cv2.cvtColor(arr, cv2.COLOR_YUV2BGR_I420)
            return cv2.resize(bgr, (tw, th), interpolation=cv2.INTER_AREA)
        quarter = h // 4
        u = arr[h:h + quarter].reshape(h // 2, w // 2)
        v = arr[h + quarter:h + 2 * quarter].reshape(h // 2, w // 2)
        small = np.empty((th * 3 // 2, tw), dtype=arr.dtype)
        small[:th] = cv2.resize(arr[:h], (tw, th), interpolation=cv2.INTER_AREA)
        small[th:th + th // 4] = cv2.resize(
            u, (tw // 2, th // 2), interpolation=cv2.INTER_AREA
        ).reshape(th // 4, tw)
        small[th + th // 4:] = cv2.resize(
            v, (tw // 2, th // 2), interpolation=cv2.INTER_AREA
        ).reshape(th // 4, tw)
        return cv2.cvtColor(small, cv2.COLOR_YUV2BGR_I420)

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
                # JPEG 85: visually identical for a 720p thumbnail, faster to
                # encode and ~40 % smaller in /dev/shm (#41).
                bgr = self._snapshot_bgr(arr)
                ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if ok:
                    tmp = self._snapshot_path + ".tmp"
                    with open(tmp, "wb") as f:
                        f.write(buf.tobytes())
                    os.replace(tmp, self._snapshot_path)
            except Exception:
                logger.debug("Snapshot write failed", exc_info=True)
