"""Mock Pi-specific and native hardware dependencies before any test import.

numpy is deliberately NOT mocked: it's a plain dev dependency (installed on
CI next to pytest/pyyaml), and mocking it made every numeric path untestable —
to the point of dictating production design (_build_night_lut stayed pure
Python "to remain testable without numpy"). Only the modules that genuinely
need Pi hardware or system libs stay mocked.
"""
import sys
from unittest.mock import MagicMock

for _mod in (
    "cv2",
    "picamera2",
    "picamera2.encoders",
    "picamera2.outputs",
    "libcamera",
):
    sys.modules.setdefault(_mod, MagicMock())
