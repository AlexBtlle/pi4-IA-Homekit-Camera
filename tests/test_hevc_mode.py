"""CameraManager HEVC-mode wiring (#59 Volet 2) — no hardware."""
import logging
from unittest.mock import MagicMock

from camera.camera_manager import CameraManager


def _mgr(hevc: dict | None = None, **camera) -> CameraManager:
    CameraManager._instance = None
    cfg = {"camera": {"width": 1920, "height": 1080, **camera}}
    if hevc is not None:
        cfg["camera"]["hevc"] = hevc
    return CameraManager(cfg)


def test_hevc_mode_captures_main_at_high_tier_size():
    mgr = _mgr(hevc={"enabled": True, "high": {"width": 2304, "height": 1296}})
    assert (mgr._width, mgr._height) == (2304, 1296)
    # The legacy H264 output geometry survives for the x264 leg.
    assert (mgr._legacy_width, mgr._legacy_height) == (1920, 1080)


def test_hevc_defaults_to_native_binned_2304x1296():
    # IMX708 native binned — the spec allows approximate resolutions and an
    # ISP upscale to 2560x1440 would cost +23 % of encode pixels for nothing.
    mgr = _mgr(hevc={"enabled": True})
    assert (mgr._width, mgr._height) == (2304, 1296)


def test_disabled_or_absent_hevc_changes_nothing():
    assert (_mgr()._width, _mgr()._height) == (1920, 1080)
    mgr = _mgr(hevc={"enabled": False, "high": {"width": 2560, "height": 1440}})
    assert (mgr._width, mgr._height) == (1920, 1080)
    assert mgr.hevc_stream_info() is None


def test_hevc_force_keyframe_is_a_quiet_noop(caplog):
    mgr = _mgr(hevc={"enabled": True})
    mgr._encoder = MagicMock()
    mgr._hw_encoder = False
    with caplog.at_level(logging.INFO):
        assert mgr.force_keyframe() is False
        assert mgr.force_keyframe() is False
    notes = [r for r in caplog.records if "no-op in HEVC mode" in r.getMessage()]
    assert len(notes) == 1  # logged once, not per live session
    mgr._encoder.force_key_frame.assert_not_called()
