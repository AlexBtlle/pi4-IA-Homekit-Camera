"""Tests for CameraManager._is_infrared() — no hardware required."""
from unittest.mock import MagicMock

from camera.camera_manager import CameraManager


def fake_bgr(b, g, r):
    """Return a mock ndarray whose .mean(axis=...) returns [B, G, R]."""
    img = MagicMock()
    img.mean.return_value = [b, g, r]
    return img


# --- should detect as infrared ---

def test_strong_pink_cast():
    assert CameraManager._is_infrared(fake_bgr(80, 130, 180))


def test_typical_ir_image():
    # R=170 B=100 → diff 70, R>60
    assert CameraManager._is_infrared(fake_bgr(100, 140, 170))


# --- should NOT detect as infrared ---

def test_daylight_neutral():
    assert not CameraManager._is_infrared(fake_bgr(120, 120, 120))


def test_daylight_warm():
    assert not CameraManager._is_infrared(fake_bgr(100, 110, 120))


def test_too_dark():
    # R - B > 25 but R < 60 → image trop sombre, pas de détection
    assert not CameraManager._is_infrared(fake_bgr(10, 20, 40))


def test_blue_scene():
    assert not CameraManager._is_infrared(fake_bgr(180, 130, 80))


def test_exact_threshold_boundary():
    # R - B == 25 → pas strictement supérieur
    assert not CameraManager._is_infrared(fake_bgr(100, 112, 125))


def test_just_above_threshold():
    assert CameraManager._is_infrared(fake_bgr(100, 113, 126))


# --- _gains_deviate: exit-night-mode signal -------------------------------

def test_gains_stable_no_deviation():
    # Same gains → still night, no exit
    assert not CameraManager._gains_deviate((0.6, 3.2), (0.6, 3.2), 0.25)


def test_gains_small_drift_within_margin():
    # +10% on blue gain, margin 25% → no exit
    assert not CameraManager._gains_deviate((0.6, 3.52), (0.6, 3.2), 0.25)


def test_gains_large_drift_triggers_exit():
    # Daylight: red gain jumps from 0.6 to 1.8 (+200%) → exit
    assert CameraManager._gains_deviate((1.8, 1.6), (0.6, 3.2), 0.25)


def test_gains_blue_drift_triggers_exit():
    # Blue gain alone drifts past margin → exit
    assert CameraManager._gains_deviate((0.6, 2.0), (0.6, 3.2), 0.25)


def test_gains_zero_baseline_ignored():
    # A zero baseline component must not divide-by-zero; it's skipped
    assert not CameraManager._gains_deviate((0.0, 3.2), (0.0, 3.2), 0.25)
