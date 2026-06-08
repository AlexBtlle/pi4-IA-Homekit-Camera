import subprocess
import threading
import time
import logging

logger = logging.getLogger(__name__)

_MAX_BACKOFF = 30


class RtspPublisher:
    """
    Reads the H264 stream from CameraManager's pipe and publishes it to
    mediamtx via ffmpeg RTSP. Restarts automatically with exponential backoff
    if ffmpeg exits unexpectedly.
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
            try:
                self._proc = subprocess.Popen(
                    [
                        "ffmpeg",
                        "-hide_banner", "-loglevel", "warning",
                        "-f", "h264",
                        "-i", f"pipe:{self._pipe_r_fd}",
                        "-c:v", "copy",
                        "-rtsp_transport", "tcp",
                        "-f", "rtsp",
                        self._rtsp_url,
                    ],
                    pass_fds=(self._pipe_r_fd,),
                )
                self._proc.wait()
                if self._stop_event.is_set():
                    break
                logger.warning(
                    "ffmpeg exited (code %d), restarting in %ds",
                    self._proc.returncode, delay,
                )
                time.sleep(delay)
                delay = min(delay * 2, _MAX_BACKOFF)
            except Exception:
                logger.exception("RtspPublisher error")
                time.sleep(delay)
                delay = min(delay * 2, _MAX_BACKOFF)
            else:
                delay = 1  # reset backoff on clean exit followed by restart
