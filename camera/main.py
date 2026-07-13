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

import cv2
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
    # OpenCV's per-core thread pool buys nothing at lores sizes (320×240 MOG2)
    # and only contends with the encoder and ffmpeg children for the four
    # 1 GHz cores (#41).
    cv2.setNumThreads(1)

    config = load_config()

    rtsp_cfg = config.get("rtsp", {})
    rtsp_base = f"rtsp://localhost:{rtsp_cfg.get('port', 8554)}"
    rtsp_url = f"{rtsp_base}/camera"

    # Camera backend (#19): csi = picamera2 (default, unchanged) | usb = UVC
    # webcam via ffmpeg [BETA]. Both expose the same surface, so everything
    # downstream — publisher, detector, control server, the whole HomeKit
    # side including HKSV — is backend-agnostic.
    source = str(config.get("camera", {}).get("source", "csi")).lower()
    if source == "usb":
        from .usb_camera_manager import UsbCameraManager
        if config.get("camera", {}).get("ir_grayscale"):
            logger.warning("ir_grayscale is CSI-only — ignored on the USB backend")
        camera = UsbCameraManager(config)
    else:
        if source != "csi":
            logger.warning("camera.source '%s' unknown — using csi", source)
        camera = CameraManager.get_instance(config)
    camera.start()

    # HEVC multi-tier mode (#59 Volet 2, Pi 5 opt-in): the pipe carries raw
    # YUV and one ffmpeg produces the x265 ladder + the legacy x264 /camera
    # stream. Classic mode publishes the encoder's H264 pipe unchanged.
    hevc_info = getattr(camera, "hevc_stream_info", lambda: None)()
    if hevc_info:
        from .hevc_publisher import HevcPublisher
        publisher = HevcPublisher(camera.get_stream_read_fd(), rtsp_base, hevc_info)
    else:
        publisher = RtspPublisher(camera.get_h264_read_fd(), rtsp_url)
    publisher.start()

    detector = PresenceDetector(camera, config)
    detector.start()

    # Localhost-only control endpoint: the HomeKit app drives the encoder
    # bitrate to what live viewers negotiate (#47) and requests an immediate
    # keyframe when a live session starts (#43).
    control = ControlServer(
        int(config.get("camera", {}).get("control_port", 8990)),
        camera.set_bitrate,
        camera.force_keyframe,
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
