import os
import threading
import logging
import numpy as np

logger = logging.getLogger(__name__)


class CameraManager:
    """
    Singleton owning the Picamera2 instance.
    Provides a main H264 stream (piped to ffmpeg) and a lores YUV420 stream
    (consumed by PresenceDetector in a background thread).
    Thread-safe: all public methods can be called from any thread.
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

        self._stream_lock = threading.Lock()
        self._streaming = False

        self._lores_condition = threading.Condition()
        self._latest_lores_frame: np.ndarray | None = None

    def start(self) -> None:
        """Configure and start picamera2 with main + lores streams."""
        from picamera2 import Picamera2

        self._picam2 = Picamera2()

        video_cfg = self._picam2.create_video_configuration(
            main={"size": (self._width, self._height), "format": "YUV420"},
            lores={"size": (self._lores_w, self._lores_h), "format": "YUV420"},
            controls={"FrameRate": self._fps},
        )

        if self._rotation:
            from libcamera import Transform
            transforms = {90: Transform(hflip=1, vflip=0), 180: Transform(hflip=1, vflip=1),
                          270: Transform(hflip=0, vflip=1)}
            video_cfg["transform"] = transforms.get(self._rotation, Transform())

        self._picam2.configure(video_cfg)
        self._picam2.pre_callback = self._lores_callback
        self._picam2.start()
        logger.info("Picamera2 started (%dx%d @ %d fps, lores %dx%d)",
                    self._width, self._height, self._fps, self._lores_w, self._lores_h)

    def _lores_callback(self, request) -> None:
        """
        Called by picamera2's internal capture thread on every frame.
        Must be fast — only copy and notify.
        """
        arr = request.make_array("lores")
        # Extract Y plane only: shape (lores_h, lores_w) from YUV420 (lores_h*3//2, lores_w)
        y_plane = arr[:self._lores_h, :self._lores_w].copy()
        with self._lores_condition:
            self._latest_lores_frame = y_plane
            self._lores_condition.notify_all()

    def start_stream(self, stream_config: dict) -> tuple[int, int]:
        """
        Create a new os.pipe and start the H264Encoder writing to the write end.
        Returns (pipe_r_fd, pipe_w_fd). Caller passes pipe_r_fd to ffmpeg stdin.
        """
        from picamera2.encoders import H264Encoder
        from picamera2.outputs import FileOutput

        with self._stream_lock:
            if self._streaming:
                self._stop_encoder()

            self._pipe_r, self._pipe_w = os.pipe()
            self._encoder = H264Encoder(bitrate=self._bitrate)

            profile_map = {"baseline": 0, "main": 2, "high": 4}
            h264_profile = self._cfg.get("h264_profile", "baseline")
            if h264_profile in profile_map:
                self._encoder.profile = profile_map[h264_profile]

            self._picam2.start_encoder(
                self._encoder,
                FileOutput(os.fdopen(self._pipe_w, "wb")),
            )
            self._streaming = True
            logger.info("H264 encoder started, pipe r=%d w=%d", self._pipe_r, self._pipe_w)
            return self._pipe_r, self._pipe_w

    def stop_stream(self) -> None:
        with self._stream_lock:
            self._stop_encoder()

    def _stop_encoder(self) -> None:
        if self._streaming and self._encoder is not None:
            try:
                self._picam2.stop_encoder()
            except Exception:
                logger.debug("Encoder stop error (may already be stopped)", exc_info=True)

            try:
                if self._pipe_r != -1:
                    os.close(self._pipe_r)
            except OSError:
                pass
            # pipe_w is closed by FileOutput when the encoder stops

            self._pipe_r = -1
            self._pipe_w = -1
            self._encoder = None
            self._streaming = False
            logger.info("H264 encoder stopped")

    def get_lores_frame(self, timeout: float = 1.0) -> np.ndarray | None:
        """
        Block until a new lores Y-plane frame is available or timeout.
        Returns uint8 ndarray (lores_h, lores_w), or None on timeout.
        Called exclusively from PresenceDetector's background thread.
        """
        with self._lores_condition:
            notified = self._lores_condition.wait(timeout=timeout)
            if not notified:
                return None
            return self._latest_lores_frame

    def stop(self) -> None:
        """Cleanup: stop encoder and picamera2."""
        with self._stream_lock:
            self._stop_encoder()
        if self._picam2 is not None:
            try:
                self._picam2.stop()
            except Exception:
                logger.debug("Picamera2 stop error", exc_info=True)
        logger.info("CameraManager stopped")
