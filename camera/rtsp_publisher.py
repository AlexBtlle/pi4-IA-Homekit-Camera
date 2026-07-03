import os
import select
import subprocess
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

    def _run(self) -> None:
        delay = 1
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                self._proc = subprocess.Popen(
                    [
                        "ffmpeg",
                        "-hide_banner", "-loglevel", "warning",
                        "-f", "h264",
                        "-i", f"pipe:{self._pipe_r_fd}",
                        "-c:v", "copy",
                        "-rtsp_transport", "tcp",
                        # Bound every socket I/O (µs): a frozen/hung mediamtx
                        # must make ffmpeg EXIT — handing over to the drain +
                        # backoff loop — instead of blocking alive forever
                        # with a full pipe (the "zombie ffmpeg" mode, #34).
                        # 5 s keeps the whole stall well under the 10 s frame
                        # watchdog.
                        "-rw_timeout", "5000000",
                        "-f", "rtsp",
                        self._rtsp_url,
                    ],
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

            self._proc.wait()
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
