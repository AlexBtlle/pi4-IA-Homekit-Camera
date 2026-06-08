import logging

from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_SENSOR

logger = logging.getLogger(__name__)


class MotionSensor(Accessory):
    """
    HomeKit MotionSensor accessory.
    set_motion() must be called from the asyncio event loop thread —
    use driver.add_job() from background threads.
    """

    category = CATEGORY_SENSOR

    def __init__(self, driver, name: str):
        super().__init__(driver, name)
        svc = self.add_preload_service("MotionSensor")
        self._motion_detected = svc.configure_char("MotionDetected", value=False)

    def set_motion(self, detected: bool) -> None:
        self._motion_detected.set_value(detected)
        logger.info("MotionDetected → %s", detected)
