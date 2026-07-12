"""Mock Pi-specific and native hardware dependencies before any test import."""
import sys
from unittest.mock import MagicMock

for _mod in (
    "cv2",
    "numpy",
    "picamera2",
    "picamera2.encoders",
    "picamera2.outputs",
    "picamera2.platform",
    "libcamera",
):
    sys.modules.setdefault(_mod, MagicMock())
