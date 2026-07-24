"""Tests for the opt-in Lux telemetry (_publish_lux) — no hardware.

This is the feature whose missing config key motivated the config-key guard
test: it shipped enabled-by-default and undocumented. Locked in here:
disabled means NOTHING happens, enabled writes atomically and
self-throttles, and failures never propagate into the capture callback.

_publish_lux takes the metadata DICT (fetched once per analysed frame by
the callback and shared with the day-lift and IR stats consumers), not the
request object.
"""
from camera.camera_manager import CameraManager


def _mgr(lux_path=None, **camera) -> CameraManager:
    CameraManager._instance = None
    cfg = dict(camera)
    if lux_path is not None:
        cfg["lux_path"] = lux_path
    return CameraManager({"camera": cfg})


def test_disabled_by_default_does_nothing(tmp_path):
    mgr = _mgr()  # no lux_path key → opt-in default: off
    mgr._publish_lux({"Lux": 215.5})
    assert list(tmp_path.iterdir()) == []  # nothing written anywhere near us
    assert mgr._last_lux_publish == 0.0    # throttle clock untouched: no work done


def test_empty_and_whitespace_paths_disable():
    for raw in ("", "   "):
        mgr = _mgr(lux_path=raw)
        mgr._publish_lux({"Lux": 215.5})
        assert mgr._last_lux_publish == 0.0


def test_enabled_writes_the_value_atomically(tmp_path):
    target = tmp_path / "lux"
    mgr = _mgr(lux_path=str(target))
    mgr._publish_lux({"Lux": 215.51})
    assert target.read_text() == "215.5\n"
    # os.replace() semantics: no half-written temp file left behind
    assert list(tmp_path.iterdir()) == [target]


def test_throttles_to_one_write_per_interval(tmp_path):
    target = tmp_path / "lux"
    mgr = _mgr(lux_path=str(target))
    mgr._publish_lux({"Lux": 10.0})
    mgr._publish_lux({"Lux": 99.0})  # immediately after → throttled
    assert target.read_text() == "10.0\n"


def test_missing_metadata_or_lux_writes_nothing(tmp_path):
    target = tmp_path / "lux"
    mgr = _mgr(lux_path=str(target))
    mgr._publish_lux(None)  # metadata fetch failed upstream
    mgr._publish_lux({})    # tuning without a Lux estimate
    assert not target.exists()


def test_malformed_lux_never_raises(tmp_path):
    target = tmp_path / "lux"
    mgr = _mgr(lux_path=str(target))
    mgr._publish_lux({"Lux": "not-a-number"})  # must not raise into the callback
    assert not target.exists()


def test_unwritable_path_never_raises():
    mgr = _mgr(lux_path="/nonexistent-dir/lux")
    mgr._publish_lux({"Lux": 5.0})  # OSError swallowed by design
