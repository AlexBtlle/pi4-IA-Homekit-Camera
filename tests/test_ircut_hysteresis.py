"""Tests for the IR-CUT day/night decision — pure state machine, no hardware.

The GPIO calls (pinctrl) are not exercised here; only the DayNightHysteresis
logic and the Lux-file reader, which is where the real correctness risk sits
(band, debounce, both flip directions, stale telemetry).
"""
import importlib.util
import os
import time

_spec = importlib.util.spec_from_file_location(
    "ircut", os.path.join(os.path.dirname(__file__), "..", "scripts",
                          "ircut_release_gpio.py")
)
ircut = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ircut)
DayNightHysteresis = ircut.DayNightHysteresis
read_lux = ircut.read_lux


def _feed(h, lux, n):
    """Feed the same reading n times, return the last flip (or None)."""
    result = None
    for _ in range(n):
        result = h.update(lux)
    return result


def test_starts_in_given_state_and_holds_in_band():
    h = DayNightHysteresis(night_below=8, day_above=25, samples=3, state="day")
    # 15 lux is inside the band → never flips, whatever the count
    assert _feed(h, 15.0, 10) is None
    assert h.state == "day"


def test_flips_to_night_only_after_enough_samples():
    h = DayNightHysteresis(8, 25, samples=3, state="day")
    assert h.update(2.0) is None       # 1st dark reading
    assert h.update(2.0) is None       # 2nd
    assert h.update(2.0) == "night"    # 3rd confirms → flip
    assert h.state == "night"


def test_flips_back_to_day_symmetrically():
    h = DayNightHysteresis(8, 25, samples=3, state="night")
    assert _feed(h, 40.0, 2) is None
    assert h.update(40.0) == "day"
    assert h.state == "day"


def test_transient_dip_does_not_flip():
    # A single dark reading (headlight gone dark / cloud) among bright ones
    # must not accumulate toward a flip.
    h = DayNightHysteresis(8, 25, samples=3, state="day")
    h.update(2.0)          # dark blip (streak 1)
    h.update(40.0)         # bright again → resets
    h.update(2.0)          # dark once more (streak 1, not 2)
    assert h.state == "day"
    assert h.update(2.0) is None   # only 2 consecutive now
    assert h.update(2.0) == "night"  # now 3 consecutive → flip


def test_band_reading_resets_a_pending_flip():
    h = DayNightHysteresis(8, 25, samples=3, state="day")
    h.update(2.0)          # pending night, streak 1
    h.update(15.0)         # in-band vote = current (day) → cancels pending
    assert _feed(h, 2.0, 2) is None   # streak restarts, only 2 so far
    assert h.update(2.0) == "night"


def test_invalid_thresholds_rejected():
    import pytest
    with pytest.raises(ValueError):
        DayNightHysteresis(night_below=25, day_above=8, samples=3)


# ----------------------------------------------------------------------
# Lux file reader
# ----------------------------------------------------------------------

def test_read_lux_parses_fresh_value(tmp_path):
    p = tmp_path / "lux"
    p.write_text("12.3\n")
    assert read_lux(str(p), stale_after=60) == 12.3


def test_read_lux_none_when_missing(tmp_path):
    assert read_lux(str(tmp_path / "absent"), stale_after=60) is None


def test_read_lux_none_when_stale(tmp_path):
    p = tmp_path / "lux"
    p.write_text("12.3\n")
    old = time.time() - 120
    os.utime(p, (old, old))
    assert read_lux(str(p), stale_after=30) is None


def test_read_lux_none_when_garbage(tmp_path):
    p = tmp_path / "lux"
    p.write_text("not-a-number\n")
    assert read_lux(str(p), stale_after=60) is None
