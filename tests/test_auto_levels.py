"""Hot-path auto-levels: percentile equivalence and LUT rebuild skipping.

These pin the two per-analysed-frame optimisations so they can never drift
from the behaviour they replaced:
- _luma_percentiles (one bincount+cumsum pass) must stay within ±1 luma of
  np.percentile's answer — the tolerance the EMA/rounding absorbs.
- the LUT is only rebuilt when the EMA'd endpoints actually move (converged
  steady state = zero Python-loop rebuilds).
"""
import numpy as np
import pytest

from camera.camera_manager import CameraManager


def _mgr(**camera) -> CameraManager:
    CameraManager._instance = None
    return CameraManager({"camera": {"lores_width": 320, "lores_height": 240, **camera}})


# ----------------------------------------------------------------------
# _luma_percentiles ≈ np.percentile
# ----------------------------------------------------------------------

@pytest.mark.parametrize("seed", [0, 1, 2, 3])
@pytest.mark.parametrize("dist", ["uniform", "night", "bimodal"])
def test_histogram_percentiles_match_numpy_within_one_luma(seed, dist):
    rng = np.random.default_rng(seed)
    if dist == "uniform":
        y = rng.integers(0, 256, size=(240, 320), dtype=np.uint8)
    elif dist == "night":
        # The real target: a deep-night scene living at the noise floor.
        y = np.clip(rng.normal(30, 12, size=(240, 320)), 0, 255).astype(np.uint8)
    else:
        half = 240 * 320 // 2
        y = np.concatenate([
            np.clip(rng.normal(20, 5, half), 0, 255),
            np.clip(rng.normal(200, 10, half), 0, 255),
        ]).astype(np.uint8).reshape(240, 320)

    low, high = CameraManager._luma_percentiles(y)
    ref_low = float(np.percentile(y, CameraManager.NIGHT_LUT_LOW_PCT))
    ref_high = float(np.percentile(y, CameraManager.NIGHT_LUT_HIGH_PCT))
    assert abs(low - ref_low) <= 1.0
    assert abs(high - ref_high) <= 1.0


def test_constant_frame_degenerates_cleanly():
    y = np.full((240, 320), 42, dtype=np.uint8)
    low, high = CameraManager._luma_percentiles(y)
    assert low == 42.0 and high == 42.0


# ----------------------------------------------------------------------
# LUT rebuild skipping
# ----------------------------------------------------------------------

def _lores(value: int) -> np.ndarray:
    # YUV420 lores layout: Y plane (240 rows) + packed chroma below; the
    # refreshers only slice the Y plane.
    arr = np.full((360, 320), 128, dtype=np.uint8)
    arr[:240] = value
    return arr


def test_night_lut_not_rebuilt_while_ema_is_converged():
    mgr = _mgr(ir_gamma=2.2)
    mgr._refresh_night_lut(_lores(30))
    first = mgr._ir_gamma_lut
    assert first is not None
    mgr._refresh_night_lut(_lores(30))  # same scene → same rounded endpoints
    assert mgr._ir_gamma_lut is first   # identity: the rebuild was skipped


def test_night_lut_rebuilds_when_the_scene_moves():
    mgr = _mgr(ir_gamma=2.2)
    mgr._refresh_night_lut(_lores(30))
    first = mgr._ir_gamma_lut
    # Feed a much brighter scene until the EMA moves the rounded endpoints.
    for _ in range(30):
        mgr._refresh_night_lut(_lores(120))
    assert mgr._ir_gamma_lut is not first


def test_night_lut_rebuilds_after_a_drop_even_with_same_key():
    mgr = _mgr(ir_gamma=2.2)
    mgr._refresh_night_lut(_lores(30))
    # The callback drops the LUT when night mode exits; re-entry with the
    # same scene must rebuild despite the unchanged key.
    mgr._ir_gamma_lut = None
    mgr._refresh_night_lut(_lores(30))
    assert mgr._ir_gamma_lut is not None


def test_day_lut_skip_mirrors_night():
    mgr = _mgr(day_gamma=2.5)
    mgr._refresh_day_lut(_lores(30))
    first = mgr._day_lut
    mgr._refresh_day_lut(_lores(30))
    assert mgr._day_lut is first
