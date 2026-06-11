import os
import threading
import logging
import numpy as np

logger = logging.getLogger(__name__)


class CameraManager:
    """
    Singleton owning the Picamera2 instance.

    Provides a continuous H264 stream (piped to RtspPublisher) and a lores
    YUV420 stream (consumed by PresenceDetector). The H264 encoder runs from
    start() onwards — no on-demand start/stop needed since mediamtx relays
    the stream and the HomeKit app reads from mediamtx.
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

        self._picam2 = None
        self._encoder = None

        self._pipe_r: int = -1
        self._pipe_w: int = -1

        self._lores_condition = threading.Condition()
        self._latest_lores_frame: np.ndarray | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Configure picamera2, start H264 encoder, and begin lores capture."""
        from picamera2 import Picamera2
        from picamera2.encoders import H264Encoder
        from picamera2.outputs import FileOutput

        self._picam2 = Picamera2()

        video_cfg = self._picam2.create_video_configuration(
            main={"size": (self._width, self._height), "format": "YUV420"},
            lores={"size": (self._lores_w, self._lores_h), "format": "YUV420"},
            controls={"FrameRate": self._fps},
        )

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
            bitrate=self._bitrate, iperiod=self._fps * 4
        )

        self._picam2.start()
        self._picam2.start_encoder(
            self._encoder,
            FileOutput(os.fdopen(self._pipe_w, "wb")),
        )

        logger.info(
            "Picamera2 started: %dx%d @ %d fps | lores %dx%d | bitrate %d",
            self._width, self._height, self._fps,
            self._lores_w, self._lores_h, self._bitrate,
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

    def _lores_callback(self, request) -> None:
        """Called by picamera2's capture thread on every frame. Must be fast."""
        arr = request.make_array("lores")
        y_plane = arr[:self._lores_h, :self._lores_w].copy()
        with self._lores_condition:
            self._latest_lores_frame = y_plane
            self._lores_condition.notify_all()
