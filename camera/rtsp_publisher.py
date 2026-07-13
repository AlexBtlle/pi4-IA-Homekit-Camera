import fcntl
import os
import select
import struct
import subprocess
import termios
import threading
import time
import logging

logger = logging.getLogger(__name__)

_MAX_BACKOFF = 30
# A run longer than this is a healthy one: the next failure restarts the
# backoff from 1 s instead of compounding forever. Without this, a handful of
# unrelated incidents spread over months of uptime pushed the delay past the
# 10 s frame watchdog — turning every later ffmpeg hiccup into a full service
# restart.
_HEALTHY_RUN_S = 60
# ffmpeg alive but the pipe continuously full for this long → it stopped
# consuming (hung mediamtx blocks its TCP write; field-tested: -rw_timeout is
# ignored by the rtsp output, so ffmpeg never exits on its own). Kill it and
# let the drain/backoff loop recover. Must stay well under the 10 s frame
# watchdog, counting the ~2 s the 1 MB pipe takes to fill.
_STALL_KILL_S = 4


class RtspPublisher:
    """
    Reads the H264 stream from CameraManager's pipe and publishes it to
    mediamtx via ffmpeg RTSP. Restarts automatically with exponential backoff
    if ffmpeg exits unexpectedly.

    While ffmpeg is down, the pipe is drained (read and discarded): the
    hardware encoder writes continuously and a full pipe would block its
    output thread, starve the picamera2 buffers and trip the frame watchdog.
    Draining keeps the camera, motion detection and snapshots alive through
    an RTSP outage.
    """

    def __init__(self, pipe_r_fd: int, rtsp_url: str):
        self._pipe_r_fd = pipe_r_fd
        self._rtsp_url = rtsp_url
        self._proc: subprocess.Popen | None = None
        self._stop_event = threading.Event()
        # "Pipe is full" threshold for the stall detector: the real capacity
        # minus one write's worth of slack (falls back to the 64 KB default
        # if the fd is not a pipe, e.g. in unit tests).
        try:
            capacity = fcntl.fcntl(pipe_r_fd, getattr(fcntl, "F_GETPIPE_SZ", 1032))
        except OSError:
            capacity = 65536
        self._stall_threshold = max(capacity - 65536, capacity // 2)
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="rtsp-publisher"
        )

    def start(self) -> None:
        self._thread.start()
        logger.info("RtspPublisher → %s", self._rtsp_url)

    def stop(self) -> None:
        self._stop_event.set()
        if self._proc is not None:
            self._proc.terminate()

    def _ffmpeg_args(self) -> list[str]:
        """The ffmpeg command line — overridden by pipe-fed subclasses
        (HevcPublisher) that share the whole lifecycle below but encode
        instead of copying."""
        return [
            "ffmpeg",
            "-hide_banner", "-loglevel", "warning",
            "-f", "h264",
            # Raw H264 from the pipe carries NO real timestamps,
            # only the encoder's *nominal* rate baked into the SPS
            # VUI (= configured fps, e.g. 30). But the sensor
            # delivers fewer frames when it slows down — night mode
            # relaxes FrameDurationLimits toward ir_min_fps (~10-12
            # fps in the dark). Tagged at the nominal 30, that
            # footage plays back ~2.5x fast: fine on the real-time
            # live view, but HKSV freezes the wrong timing into the
            # recorded clip. Stamp each frame by its real arrival
            # instant instead, so playback speed stays correct at
            # any capture rate. Field-measured: 10 real s → 3.97 s
            # file before this (#57 follow-up).
            "-use_wallclock_as_timestamps", "1",
            "-i", f"pipe:{self._pipe_r_fd}",
            "-c:v", "copy",
            "-rtsp_transport", "tcp",
            # Best effort (µs): field testing showed the rtsp
            # output IGNORES this on Pi OS's ffmpeg — the FIONREAD
            # stall detector (_wait_or_kill_stalled) is the real
            # guarantee — but it is harmless and may help on
            # other builds.
            "-rw_timeout", "5000000",
            "-f", "rtsp",
            self._rtsp_url,
        ]

    def _run(self) -> None:
        delay = 1
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                self._proc = subprocess.Popen(
                    self._ffmpeg_args(),
                    pass_fds=(self._pipe_r_fd,),
                )
            except FileNotFoundError:
                logger.critical("ffmpeg not found — cannot publish RTSP stream")
                os._exit(1)
            except Exception:
                logger.exception("RtspPublisher: failed to start ffmpeg")
                self._drain_pipe(delay)
                delay = min(delay * 2, _MAX_BACKOFF)
                continue

            self._wait_or_kill_stalled()
            if self._stop_event.is_set():
                break
            if time.monotonic() - started > _HEALTHY_RUN_S:
                delay = 1  # healthy long run: forget accumulated incidents
            logger.warning(
                "ffmpeg exited (code %d), restarting in %ds",
                self._proc.returncode, delay,
            )
            self._drain_pipe(delay)
            delay = min(delay * 2, _MAX_BACKOFF)

    def _wait_or_kill_stalled(self) -> None:
        """
        Wait for ffmpeg to exit — and kill it ourselves if it stops consuming
        the pipe while staying alive (a hung mediamtx blocks its TCP write
        forever). The kernel tells us the truth: FIONREAD on the read end
        reports the backlog; a pipe continuously full for _STALL_KILL_S means
        the reader is stuck. Killing it hands recovery to the drain/backoff
        loop and keeps the camera side under the frame watchdog's radar.
        """
        stalled_since = None
        while True:
            try:
                self._proc.wait(timeout=1.0)
                return
            except subprocess.TimeoutExpired:
                pass
            if self._pipe_backlog() >= self._stall_threshold:
                now = time.monotonic()
                if stalled_since is None:
                    stalled_since = now
                elif now - stalled_since >= _STALL_KILL_S:
                    logger.warning(
                        "ffmpeg alive but not reading (pipe full for %ds) — killing it",
                        _STALL_KILL_S,
                    )
                    self._proc.kill()
                    self._proc.wait()
                    return
            else:
                stalled_since = None

    def _pipe_backlog(self) -> int:
        """Bytes currently sitting unread in the pipe (0 on error)."""
        try:
            raw = fcntl.ioctl(self._pipe_r_fd, termios.FIONREAD, b"\x00\x00\x00\x00")
            return struct.unpack("i", raw)[0]
        except OSError:
            return 0

    def _drain_pipe(self, seconds: float) -> None:
        """
        Read and discard the encoder's output for `seconds` while ffmpeg is
        down (never called while it runs — no competing reader). Returns
        early if stop() is called.
        """
        deadline = time.monotonic() + seconds
        while not self._stop_event.is_set():
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                return
            # Short select timeout so a stop() is noticed promptly even when
            # the camera side produces no data.
            ready, _, _ = select.select(
                [self._pipe_r_fd], [], [], min(timeout, 0.2)
            )
            if ready:
                try:
                    os.read(self._pipe_r_fd, 65536)
                except OSError:
                    return
