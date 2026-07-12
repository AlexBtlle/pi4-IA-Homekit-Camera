"""Tests for the Pi 5 (PISP) software-encoder branch (#59, Volet 1).

On non-VC4 platforms picamera2 substitutes LibavH264Encoder (software x264)
for the hardware V4L2 encoder. The two live controls that poke the V4L2 fd
must adapt: dynamic bitrate becomes a logged no-op (same accepted trade-off
as the USB backend), force-keyframe uses the encoder's native API instead
of an ioctl. No hardware needed here — platform state is set directly.
"""
import logging
from unittest.mock import MagicMock

from camera.camera_manager import CameraManager


def _mgr(hw: bool) -> CameraManager:
    CameraManager._instance = None
    mgr = CameraManager({"camera": {"bitrate": 4_000_000}})
    mgr._encoder = MagicMock()
    mgr._hw_encoder = hw
    return mgr


# ----------------------------------------------------------------------
# set_bitrate
# ----------------------------------------------------------------------

def test_software_set_bitrate_is_a_noop(monkeypatch):
    mgr = _mgr(hw=False)

    def boom(_bps):
        raise AssertionError("V4L2 ioctl path must not run on a software encoder")

    monkeypatch.setattr(mgr, "_apply_bitrate", boom)
    assert mgr.set_bitrate(2_000_000) == 4_000_000
    assert mgr._current_bitrate == 4_000_000


def test_software_set_bitrate_logs_only_once(caplog):
    mgr = _mgr(hw=False)
    with caplog.at_level(logging.INFO):
        mgr.set_bitrate(2_000_000)
        mgr.set_bitrate(1_000_000)
        mgr.set_bitrate(3_000_000)
    notes = [r for r in caplog.records if "not supported" in r.getMessage()]
    assert len(notes) == 1


def test_hardware_set_bitrate_still_applies(monkeypatch):
    mgr = _mgr(hw=True)
    applied = []
    monkeypatch.setattr(mgr, "_apply_bitrate", applied.append)
    assert mgr.set_bitrate(2_000_000) == 2_000_000
    assert applied == [2_000_000]


# ----------------------------------------------------------------------
# force_keyframe
# ----------------------------------------------------------------------

def test_software_force_keyframe_uses_native_api(monkeypatch):
    mgr = _mgr(hw=False)

    def boom():
        raise AssertionError("V4L2 ioctl path must not run on a software encoder")

    monkeypatch.setattr(mgr, "_apply_force_keyframe", boom)
    assert mgr.force_keyframe() is True
    mgr._encoder.force_key_frame.assert_called_once_with()


def test_software_force_keyframe_failure_returns_false():
    mgr = _mgr(hw=False)
    mgr._encoder.force_key_frame.side_effect = RuntimeError("encoder stopped")
    assert mgr.force_keyframe() is False


def test_hardware_force_keyframe_uses_ioctl(monkeypatch):
    mgr = _mgr(hw=True)
    called = []
    monkeypatch.setattr(mgr, "_apply_force_keyframe", lambda: called.append(1))
    assert mgr.force_keyframe() is True
    assert called == [1]
    mgr._encoder.force_key_frame.assert_not_called()


# ----------------------------------------------------------------------
# Defaults
# ----------------------------------------------------------------------

def test_platform_defaults_to_hardware_before_start():
    # Constructed but not started (start() runs the platform probe): the
    # historic VC4/ioctl behaviour must be the default.
    mgr = _mgr(hw=True)
    CameraManager._instance = None
    fresh = CameraManager({"camera": {}})
    assert fresh._hw_encoder is True
    assert fresh._sw_bitrate_noted is False
    assert mgr is not fresh
