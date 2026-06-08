import os
import subprocess
import logging

logger = logging.getLogger(__name__)


class StreamHandler:
    """
    Manages a single ffmpeg subprocess that reads raw H264 Annex B from a pipe,
    wraps it in SRTP, and sends it to the HomeKit iOS client.
    """

    def __init__(self, session_info: dict, stream_config: dict, pipe_r_fd: int):
        self._session_info = session_info
        self._stream_config = stream_config
        self._pipe_r_fd = pipe_r_fd
        self._process: subprocess.Popen | None = None

    def start(self) -> bool:
        """Launch ffmpeg. Returns True on success."""
        cmd = self._build_command()
        logger.debug("Starting ffmpeg: %s", " ".join(cmd))
        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=os.fdopen(self._pipe_r_fd, "rb"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            logger.info("ffmpeg started (pid=%d)", self._process.pid)
            return True
        except Exception:
            logger.exception("Failed to start ffmpeg")
            return False

    def stop(self) -> None:
        if self._process is None:
            return
        try:
            self._process.terminate()
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()
        except Exception:
            logger.debug("ffmpeg stop error", exc_info=True)
        logger.info("ffmpeg stopped")

    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _build_command(self) -> list[str]:
        v = self._stream_config.get("v_srtp_key", "")
        address = self._session_info.get("address")
        v_port = self._session_info.get("v_port")
        v_ssrc = self._session_info.get("v_ssrc")
        v_payload_type = self._stream_config.get("v_payload_type", 99)

        srtp_url = (
            f"srtp://{address}:{v_port}"
            f"?rtcpport={v_port}&localrtcpport={v_port}&pkt_size=1316"
        )

        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            # Input: raw H264 Annex B from stdin
            "-f", "h264",
            "-i", "pipe:0",
            # Video passthrough — no transcoding
            "-c:v", "copy",
            "-an",
            # RTP/SRTP parameters
            "-payload_type", str(v_payload_type),
            "-ssrc", str(v_ssrc),
            "-f", "rtp",
            "-srtp_out_suite", "AES_CM_128_HMAC_SHA1_80",
            "-srtp_out_params", v,
            srtp_url,
        ]
