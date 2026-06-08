import logging
import os
import signal
import socket

import yaml

from pyhap.accessory_driver import AccessoryDriver
from pyhap.accessory import Bridge

from .camera_accessory import PiCamera4, _build_options
from .camera_manager import CameraManager
from .motion_sensor import MotionSensor
from .presence_detector import PresenceDetector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATHS = [
    os.path.join(os.path.dirname(__file__), "..", "config.yaml"),
    "/opt/pi4tohomekit/config.yaml",
]
STATE_FILE = os.path.join(
    os.environ.get("PI4HK_STATE_DIR", os.path.join(os.path.dirname(__file__), "..")),
    "accessory.state",
)


def load_config() -> dict:
    for path in DEFAULT_CONFIG_PATHS:
        if os.path.isfile(path):
            with open(path) as f:
                cfg = yaml.safe_load(f) or {}
            logger.info("Config loaded from %s", os.path.abspath(path))
            return cfg
    logger.warning("No config.yaml found — using defaults")
    return {}


def get_local_address() -> str:
    """Return the Pi's primary LAN IP address."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "0.0.0.0"


def main() -> None:
    config = load_config()
    hk_cfg = config.get("homekit", {})

    address = get_local_address()
    logger.info("Local address: %s", address)

    # Start camera hardware before entering the asyncio event loop
    camera_manager = CameraManager.get_instance(config)
    camera_manager.start()

    options = _build_options(config)
    options["address"] = address

    pincode = hk_cfg.get("pincode", "").encode() or None
    driver = AccessoryDriver(
        address=address,
        port=int(hk_cfg.get("port", 51826)),
        persist_file=STATE_FILE,
        pincode=pincode,
    )

    bridge = Bridge(driver, hk_cfg.get("name", "Pi4 Camera"))

    camera_acc = PiCamera4(options, driver, "Camera", camera_manager, config)
    motion_acc = MotionSensor(driver, "Motion Sensor")

    bridge.add_accessory(camera_acc)
    bridge.add_accessory(motion_acc)
    driver.add_accessory(accessory=bridge)

    detector = PresenceDetector(camera_manager, motion_acc, driver, config)
    detector.start()

    def _shutdown(sig, frame):
        logger.info("Shutdown signal received (%s)", sig)
        detector.stop()
        camera_manager.stop()
        driver.signal_handler(sig, frame)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    logger.info("Starting HomeKit bridge — scan QR code or enter PIN to pair")
    driver.start()


if __name__ == "__main__":
    main()
