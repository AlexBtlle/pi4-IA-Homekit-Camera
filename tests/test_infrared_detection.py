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


