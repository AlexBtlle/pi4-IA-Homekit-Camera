"""Tests for CameraManager._select_full_fov_mode() — no hardware required."""
import pytest
from unittest.mock import MagicMock, PropertyMock

from camera.camera_manager import CameraManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mgr(fps: int = 30, width: int = 1920, height: int = 1080) -> CameraManager:
    CameraManager._instance = None
    return CameraManager({"camera": {"fps": fps, "width": width, "height": height}})


def _with_modes(modes: list, **kwargs) -> CameraManager:
    mgr = _mgr(**kwargs)
    mgr._picam2 = MagicMock()
    mgr._picam2.sensor_modes = modes
    return mgr


# ---------------------------------------------------------------------------
# Realistic sensor mode data (crop_limits = full physical sensor rectangle)
# ---------------------------------------------------------------------------

OV5647 = [
    {"size": (2592, 1944), "fps": 15, "crop_limits": (0, 0, 2592, 1944)},  # full res, too slow
    {"size": (1920, 1080), "fps": 30, "crop_limits": (336, 432, 1920, 1080)},  # center crop
    {"size": (1296, 972),  "fps": 47, "crop_limits": (0, 0, 2592, 1944)},  # binned full FOV
    {"size": (640,  480),  "fps": 90, "crop_limits": (0, 0, 2592, 1944)},
]

IMX219 = [
    {"size": (3280, 2464), "fps": 21, "crop_limits": (0, 0, 3280, 2464)},  # full res, too slow
    {"size": (3280, 1848), "fps": 28, "crop_limits": (0, 0, 3280, 2464)},  # full-width 16:9
    {"size": (1920, 1080), "fps": 47, "crop_limits": (680, 692, 1920, 1080)},  # center crop
    {"size": (1640, 1232), "fps": 41, "crop_limits": (0, 0, 3280, 2464)},  # binned 4:3
    {"size": (1640, 922),  "fps": 60, "crop_limits": (0, 0, 3280, 2464)},  # binned 16:9
]

IMX708 = [
    {"size": (4608, 2592), "fps": 14,  "crop_limits": (0, 0, 4608, 2592)},  # full res
    {"size": (2304, 1296), "fps": 56,  "crop_limits": (0, 0, 4608, 2592)},  # binned 16:9
    {"size": (1536, 864),  "fps": 120, "crop_limits": (0, 0, 4608, 2592)},
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSelectFullFovMode:

    def test_ov5647_falls_to_tier3_binned(self):
        """OV5647: no full-FOV mode with src>=1920 at >=25fps → tier 3, best binned."""
        mode = _with_modes(OV5647)._select_full_fov_mode()
        assert mode["size"] == (1296, 972)

    def test_imx219_picks_tier2_downscale(self):
        """IMX219: 3280×1848 @ 28fps → tier 2 (src>=output, fps between 25 and 30)."""
        mode = _with_modes(IMX219)._select_full_fov_mode()
        assert mode["size"] == (3280, 1848)

    def test_imx708_picks_tier1_downscale(self):
        """IMX708: 2304×1296 @ 56fps → tier 1 (src>=output, fps>=target)."""
        mode = _with_modes(IMX708)._select_full_fov_mode()
        assert mode["size"] == (2304, 1296)

    def test_tier1_wins_over_larger_tier2(self):
        """Tier-1 mode wins even if a tier-2 mode has higher resolution."""
        modes = [
            {"size": (4000, 2000), "fps": 26, "crop_limits": (0, 0, 4000, 2000)},  # tier 2
            {"size": (2000, 1080), "fps": 30, "crop_limits": (0, 0, 4000, 2000)},  # tier 1
        ]
        mode = _with_modes(modes)._select_full_fov_mode()
        assert mode["size"] == (2000, 1080)

    def test_tier2_floor_is_25fps(self):
        """A mode at exactly 25fps qualifies for tier 2."""
        modes = [
            {"size": (3000, 2000), "fps": 25, "crop_limits": (0, 0, 3000, 2000)},
            {"size": (1500, 1000), "fps": 60, "crop_limits": (0, 0, 3000, 2000)},
        ]
        mode = _with_modes(modes)._select_full_fov_mode()
        assert mode["size"] == (3000, 2000)

    def test_below_floor_excluded_from_tier2(self):
        """A mode at 24fps is below the floor and falls to tier 3."""
        modes = [
            {"size": (3000, 2000), "fps": 24, "crop_limits": (0, 0, 3000, 2000)},
            {"size": (1500, 1000), "fps": 30, "crop_limits": (0, 0, 3000, 2000)},
        ]
        mode = _with_modes(modes)._select_full_fov_mode()
        assert mode["size"] == (1500, 1000)

    def test_highest_resolution_wins_within_same_tier(self):
        """Among tier-1 candidates, the highest-resolution mode wins."""
        modes = [
            {"size": (2304, 1296), "fps": 56, "crop_limits": (0, 0, 4608, 2592)},
            {"size": (3456, 1944), "fps": 30, "crop_limits": (0, 0, 4608, 2592)},
        ]
        mode = _with_modes(modes)._select_full_fov_mode()
        assert mode["size"] == (3456, 1944)

    def test_empty_modes_returns_none(self):
        mgr = _mgr()
        mgr._picam2 = MagicMock()
        mgr._picam2.sensor_modes = []
        assert mgr._select_full_fov_mode() is None

    def test_sensor_read_exception_returns_none(self):
        mgr = _mgr()
        mgr._picam2 = MagicMock()
        type(mgr._picam2).sensor_modes = PropertyMock(
            side_effect=RuntimeError("no camera attached")
        )
        assert mgr._select_full_fov_mode() is None

    def test_respects_configured_output_width(self):
        """With a narrower output (1280px), a smaller mode can qualify for tier 1."""
        modes = [
            {"size": (1920, 1080), "fps": 30, "crop_limits": (0, 0, 1920, 1080)},
        ]
        mode = _with_modes(modes, fps=30, width=1280)._select_full_fov_mode()
        assert mode["size"] == (1920, 1080)
