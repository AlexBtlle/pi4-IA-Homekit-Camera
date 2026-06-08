import asyncio
import logging

from pyhap.camera import Camera

from .camera_manager import CameraManager
from .stream_handler import StreamHandler

logger = logging.getLogger(__name__)


def _build_options(config: dict) -> dict:
    """Build the HAP-python camera options dict from config."""
    cam = config.get("camera", {})
    w = int(cam.get("width", 1920))
    h = int(cam.get("height", 1080))
    fps = int(cam.get("fps", 30))
    bitrate = int(cam.get("bitrate", 4_000_000))

    from pyhap.camera import (
        VIDEO_CODEC_PARAM_LEVEL_TYPES,
        VIDEO_CODEC_PARAM_PROFILE_ID_TYPES,
    )

    return {
        "video": {
            "codec": {
                "profiles": [VIDEO_CODEC_PARAM_PROFILE_ID_TYPES["BASELINE"]],
                "levels": [VIDEO_CODEC_PARAM_LEVEL_TYPES["TYPE4_0"]],
            },
            "resolutions": [
                [1920, 1080, fps],
                [1280, 720, fps],
                [640, 480, fps],
                [320, 240, fps],
            ],
        },
        "audio": {
            "codecs": [{"type": "OPUS", "samplerate": 24}],
        },
        "address": None,  # filled by AccessoryDriver
        "srtp": True,
    }


class PiCamera4(Camera):
    """
    HomeKit CameraRTPStreamManagement accessory for Pi 4.
    Delegates hardware management to CameraManager.
    Delegates SRTP streaming to StreamHandler.
    """

    def __init__(self, options: dict, driver, name: str,
                 camera_manager: CameraManager, config: dict):
        super().__init__(options, driver, name)
        self._camera_manager = camera_manager
        self._config = config

    async def start_stream(self, session_info: dict, stream_config: dict) -> bool:
        loop = asyncio.get_event_loop()
        try:
            pipe_r, _ = await loop.run_in_executor(
                None, self._camera_manager.start_stream, stream_config
            )
        except Exception:
            logger.exception("Failed to start camera stream")
            return False

        handler = StreamHandler(session_info, stream_config, pipe_r)
        if not handler.start():
            await loop.run_in_executor(None, self._camera_manager.stop_stream)
            return False

        session_info["stream_handler"] = handler
        return True

    async def stop_stream(self, session_info: dict) -> None:
        handler: StreamHandler | None = session_info.pop("stream_handler", None)
        if handler:
            handler.stop()
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._camera_manager.stop_stream)

    def reconfigure_stream(self, session_info: dict, stream_config: dict) -> bool:
        return True

    async def async_get_snapshot(self, image_size: dict) -> bytes:
        width = image_size.get("image-width", 1280)
        height = image_size.get("image-height", 720)
        # Use rpicam-jpeg subprocess to avoid interrupting the running H264 encoder
        cmd = [
            "rpicam-jpeg",
            "--nopreview",
            "-t", "1",
            "--width", str(width),
            "--height", str(height),
            "-o", "-",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                return stdout
        except Exception:
            logger.exception("Snapshot failed")
        return b""
