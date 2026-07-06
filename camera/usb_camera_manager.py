"""[BETA] USB webcam backend (#19) — V4L2/UVC via a single long-lived ffmpeg.

Same public surface as CameraManager, so everything downstream is untouched:
RtspPublisher keeps reading the H264 pipe, PresenceDetector keeps pulling
lores frames, ControlServer keeps its endpoints, and the whole HomeKit side
(live passthrough, snapshots, HKSV recordings) works unchanged — it only ever
sees mediamtx and the snapshot file.

One ffmpeg, three outputs (fd numbers are inherited via pass_fds):

    /dev/video0 ──► ffmpeg ──► H264 elementary stream ──► pipe ──► RtspPublisher
                       ├─────► lores grayscale rawvideo ─► pipe ──► PresenceDetector
                       └─────► MJPEG @ snapshot_interval ► pipe ──► tmpfs JPEG (atomic)

Encoding path depends on what the webcam outputs (camera.usb_format):
  - h264  → -c:v copy: zero re-encode, the CSI philosophy — best case.
  - mjpeg → decode + h264_v4l2m2m (the Pi's HARDWARE encoder, never libx264).
  - yuyv  → same, but raw frames don't fit USB 2.0 above ~720p30.

Beta limitations (documented in TROUBLESHOOTING):
  - No dynamic bitrate / force-keyframe (the encoder lives inside ffmpeg,
    no ioctl handle) — set_bitrate/force_keyframe are polite no-ops.
  - No IR night mode (ir_grayscale needs the CSI chroma path) — ignored.
  - If the capture ffmpeg dies, the process exits and systemd restarts the
    service clean (same philosophy as the CSI frame watchdog).
"""

import logging
import os
import subprocess
import threading
import time

import numpy as np

from .camera_manager import DEFAULT_SNAPSHOT_PATH

logger = logging.getLogger(__name__)


class UsbCameraManager:
    # ffmpeg's v4l2 input_format names for the config's friendly values
    _FORMATS = {"mjpeg": "mjpeg", "yuyv": "yuyv422", "h264": "h264"}

    def __init__(self, config: dict):
        self._cfg = config.get("camera", {})
        self._width = int(self._cfg.get("width", 1920))
        self._height = int(self._cfg.get("height", 1080))
        self._fps = int(self._cfg.get("fps", 30))
        self._bitrate = int(self._cfg.get("bitrate", 4_000_000))
        self._device = str(self._cfg.get("device", "/dev/video0"))
        fmt = str(self._cfg.get("usb_format", "mjpeg")).lower()
        if fmt not in self._FORMATS:
            logger.warning("usb_format '%s' unknown — falling back to mjpeg", fmt)
            fmt = "mjpeg"
        self._format = fmt
        self._lores_w = int(self._cfg.get("lores_width", 320))
        self._lores_h = int(self._cfg.get("lores_height", 240))
        self._snapshot_interval = float(self._cfg.get("snapshot_interval", 2))
        self._snapshot_path = str(self._cfg.get("snapshot_path", DEFAULT_SNAPSHOT_PATH))
        _det_fps = float(config.get("detection", {}).get("analysis_fps", 10))
        self._analysis_fps = _det_fps if _det_fps > 0 else 10.0

        self._proc: subprocess.Popen | None = None
        self._pipe_r: int = -1  # H264 read end, handed to RtspPublisher
        self._lores_r: int = -1
        self._snap_r: int = -1

        self._lores_condition = threading.Condition()
        self._latest_lores_frame: np.ndarray | None = None
        self._last_frame_time: float = 0.0

        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._bitrate_warned = False

    # ------------------------------------------------------------------
    # ffmpeg command
    # ------------------------------------------------------------------

    def _build_ffmpeg_args(self, h264_fd: int, lores_fd: int, snap_fd: int) -> list[str]:
        """The whole backend in one command. Deliberately the SYSTEM ffmpeg:
        this process is spawned once and lives for days — startup time is
        irrelevant, and the lean static build ships none of v4l2/mjpeg/m2m."""
        args = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-f", "v4l2",
            "-input_format", self._FORMATS[self._format],
            "-video_size", f"{self._width}x{self._height}",
            "-framerate", str(self._fps),
            "-i", self._device,
        ]

        # Output 1 — H264 elementary stream for RtspPublisher.
        args += ["-map", "0:v"]
        if self._format == "h264":
            # Webcam encodes on-board: pure passthrough, the CSI philosophy.
            # GOP is whatever the camera does — not under our control.
            args += ["-c:v", "copy"]
        else:
            # The Pi's hardware encoder via V4L2 M2M — never libx264 on these
            # boards. yuv420p: UVC MJPEG decodes to yuvj422p, m2m wants 4:2:0.
            # -g fps = 1 s GOP, matching the CSI backend's live-startup tuning.
            args += [
                "-vf", "format=yuv420p",
                "-c:v", "h264_v4l2m2m",
                "-b:v", str(self._bitrate),
                "-g", str(self._fps),
            ]
        args += ["-f", "h264", f"pipe:{h264_fd}"]

        # Output 2 — grayscale lores for MOG2 (the detector only uses luma).
        args += [
            "-map", "0:v",
            "-vf", f"fps={self._analysis_fps:g},scale={self._lores_w}:{self._lores_h},format=gray",
            "-c:v", "rawvideo", "-f", "rawvideo", f"pipe:{lores_fd}",
        ]

        # Output 3 — one JPEG per snapshot_interval; Python writes it to the
        # tmpfs path atomically (tmp + rename), same contract as the CSI side.
        if self._snapshot_interval > 0:
            args += [
                "-map", "0:v",
                "-vf", f"fps=1/{self._snapshot_interval:g},scale=1280:720",
                "-c:v", "mjpeg", "-q:v", "4",
                "-f", "image2pipe", f"pipe:{snap_fd}",
            ]
        return args

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._pipe_r, h264_w = os.pipe()
        self._lores_r, lores_w = os.pipe()
        self._snap_r, snap_w = os.pipe()

        args = self._build_ffmpeg_args(h264_w, lores_w, snap_w)
        logger.info("[BETA] USB backend: %s (%s, %dx%d@%d)",
                    self._device, self._format, self._width, self._height, self._fps)
        # pass_fds keeps the write ends open in the child UNDER THE SAME fd
        # numbers, which is exactly what ffmpeg's pipe:N addressing needs.
        self._proc = subprocess.Popen(args, pass_fds=(h264_w, lores_w, snap_w))
        os.close(h264_w)
        os.close(lores_w)
        os.close(snap_w)

        self._last_frame_time = time.monotonic()
        for target, name in (
            (self._lores_reader, "usb-lores-reader"),
            (self._snapshot_reader, "usb-snapshot-writer"),
            (self._watchdog, "usb-watchdog"),
        ):
            t = threading.Thread(target=target, daemon=True, name=name)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop_event.set()
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        for fd in (self._pipe_r, self._lores_r, self._snap_r):
            if fd != -1:
                try:
                    os.close(fd)
                except OSError:
                    pass
        logger.info("UsbCameraManager stopped")

    # ------------------------------------------------------------------
    # CameraManager-compatible surface
    # ------------------------------------------------------------------

    def get_h264_read_fd(self) -> int:
        return self._pipe_r

    def get_lores_frame(self, timeout: float = 1.0) -> np.ndarray | None:
        with self._lores_condition:
            if not self._lores_condition.wait(timeout=timeout):
                return None
            return self._latest_lores_frame

    def set_bitrate(self, bps: int) -> int:
        """No live control on this backend (the encoder lives inside ffmpeg,
        there is no ioctl handle) — the configured bitrate stays in force."""
        if not self._bitrate_warned:
            self._bitrate_warned = True
            logger.info("Dynamic bitrate is not supported on the USB backend (beta)")
        return self._bitrate

    def force_keyframe(self) -> bool:
        """Not supported either: live viewers wait out the 1 s GOP instead —
        the same worst case the CSI backend had before #43."""
        return False

    # ------------------------------------------------------------------
    # Reader threads
    # ------------------------------------------------------------------

    def _read_exact(self, fd: int, n: int) -> bytes | None:
        """Read exactly n bytes or None on EOF/stop."""
        chunks = []
        remaining = n
        while remaining > 0:
            if self._stop_event.is_set():
                return None
            try:
                chunk = os.read(fd, remaining)
            except OSError:
                return None
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _lores_reader(self) -> None:
        frame_size = self._lores_w * self._lores_h  # gray8: 1 byte/pixel
        while not self._stop_event.is_set():
            data = self._read_exact(self._lores_r, frame_size)
            if data is None:
                logger.warning("USB lores stream ended")
                return
            frame = np.frombuffer(data, dtype=np.uint8).reshape(
                self._lores_h, self._lores_w
            )
            self._last_frame_time = time.monotonic()
            with self._lores_condition:
                self._latest_lores_frame = frame
                self._lores_condition.notify_all()

    @staticmethod
    def _split_jpegs(buf: bytes) -> tuple[list[bytes], bytes]:
        """Split an image2pipe byte stream into complete JPEGs (…FFD9) and
        the trailing partial image. Pure function — unit-tested."""
        jpegs = []
        while True:
            end = buf.find(b"\xff\xd9")
            if end == -1:
                return jpegs, buf
            jpegs.append(buf[:end + 2])
            buf = buf[end + 2:]

    def _snapshot_reader(self) -> None:
        buf = b""
        while not self._stop_event.is_set():
            try:
                chunk = os.read(self._snap_r, 65536)
            except OSError:
                return
            if not chunk:
                logger.warning("USB snapshot stream ended")
                return
            buf += chunk
            jpegs, buf = self._split_jpegs(buf)
            for jpeg in jpegs:
                try:
                    tmp = self._snapshot_path + ".tmp"
                    with open(tmp, "wb") as f:
                        f.write(jpeg)
                    os.replace(tmp, self._snapshot_path)  # atomic, like CSI
                except OSError:
                    logger.debug("Snapshot write failed", exc_info=True)

    def _watchdog(self) -> None:
        """Capture ffmpeg dead or frozen → exit; systemd restarts us clean.
        Same philosophy as the CSI frame watchdog (a half-alive camera
        service is worse than a restart)."""
        while not self._stop_event.wait(timeout=2.0):
            if self._proc is not None and self._proc.poll() is not None:
                logger.critical(
                    "USB capture ffmpeg exited (%s) — restarting the service",
                    self._proc.returncode,
                )
                os._exit(1)
            if time.monotonic() - self._last_frame_time > 10.0:
                logger.critical("USB watchdog: no frame for 10 s — restarting")
                os._exit(1)
