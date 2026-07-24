"""Tests for the opt-in Lux telemetry (_publish_lux) — no hardware.

This is the feature whose missing config key motivated the config-key guard
test: it shipped enabled-by-default and undocumented. Locked in here:
disabled means NOTHING happens (not even a metadata read), enabled writes
atomically and self-throttles, and failures never propagate into the
capture callback.
"""
from unittest.mock import MagicMock

from camera.camera_manager import CameraManager


def _mgr(lux_path=None, **camera) -> CameraManager:
    CameraManager._instance = None
    cfg = dict(camera)
    if lux_path is not None:
        cfg["lux_path"] = lux_path
    return CameraManager({"camera": cfg})


def _request(lux=215.5):
    req = MagicMock()
    req.get_metadata.return_value = {"Lux": lux}
    return req


def test_disabled_by_default_does_nothing():
    mgr = _mgr()  # no lux_path key → opt-in default: off
    req = _request()
    mgr._publish_lux(req)
    req.get_metadata.assert_not_called()  # not even a metadata fetch


def test_empty_and_whitespace_paths_disable():
    for raw in ("", "   "):
        req = _request()
        _mgr(lux_path=raw)._publish_lux(req)
        req.get_metadata.assert_not_called()


def test_enabled_writes_the_value_atomically(tmp_path):
    target = tmp_path / "lux"
    mgr = _mgr(lux_path=str(target))
    mgr._publish_lux(_request(lux=215.51))
    assert target.read_text() == "215.5\n"
    # os.replace() semantics: no half-written temp file left behind
    assert list(tmp_path.iterdir()) == [target]


def test_throttles_to_one_write_per_interval(tmp_path):
    target = tmp_path / "lux"
    mgr = _mgr(lux_path=str(target))
    mgr._publish_lux(_request(lux=10.0))
    mgr._publish_lux(_request(lux=99.0))  # immediately after → throttled
    assert target.read_text() == "10.0\n"


def test_missing_lux_metadata_writes_nothing(tmp_path):
    target = tmp_path / "lux"
    mgr = _mgr(lux_path=str(target))
    req = MagicMock()
    req.get_metadata.return_value = {}  # tuning without a Lux estimate
    mgr._publish_lux(req)
    assert not target.exists()


def test_metadata_error_never_raises(tmp_path):
    target = tmp_path / "lux"
    mgr = _mgr(lux_path=str(target))
    req = MagicMock()
    req.get_metadata.side_effect = RuntimeError("camera stopping")
    mgr._publish_lux(req)  # must not raise into the capture callback
    assert not target.exists()


def test_unwritable_path_never_raises():
    mgr = _mgr(lux_path="/nonexistent-dir/lux")
    mgr._publish_lux(_request())  # OSError swallowed by design
