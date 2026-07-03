"""
pi4-IA-Homekit-Camera — main entry point.

Starts the camera pipeline:
  picamera2 → H264 pipe → RtspPublisher (ffmpeg → mediamtx RTSP)
  picamera2 → lores YUV → PresenceDetector (motion → HomeKit app webhook)

HomeKit (HKSV + live stream + motion sensor) is handled by the HAP-NodeJS
app in homekit/, running as the separate pi4cam-homekit.service.
"""

import logging
import os
import signal
import sys
import yaml

from .camera_manager import CameraManager
from .control_server import ControlServer
from .rtsp_publisher import RtspPublisher
from .presence_detector import PresenceDetector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def load_config() -> dict:
    candidates = [
        os.path.join(os.path.dirname(__file__), "..", "config.yaml"),
        "/opt/pi4cam/config.yaml",
    ]
    for path in candidates:
        if os.path.isfile(path):
            with open(path) as f:
                return yaml.safe_load(f) or {}
    logger.warning("config.yaml not found, using defaults")
    return {}


def main() -> None:
    config = load_config()

    rtsp_cfg = config.get("rtsp", {})
    rtsp_url = f"rtsp://localhost:{rtsp_cfg.get('port', 8554)}/camera"

    camera = CameraManager.get_instance(config)
    camera.start()

    publisher = RtspPublisher(camera.get_h264_read_fd(), rtsp_url)
    publisher.start()

    detector = PresenceDetector(camera, config)
    detector.start()

    # Localhost-only control endpoint: the HomeKit app drives the encoder
    # bitrate to what live viewers negotiate (#47).
    control = ControlServer(
        int(config.get("camera", {}).get("control_port", 8990)),
        camera.set_bitrate,
    )
    control.start()

    def _shutdown(signum, _frame):
        logger.info("Shutting down (signal %d)…", signum)
        control.stop()
        detector.stop()
        publisher.stop()
        camera.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    logger.info("pi4cam running — RTSP: %s", rtsp_url)
    signal.pause()


if __name__ == "__main__":
    main()
