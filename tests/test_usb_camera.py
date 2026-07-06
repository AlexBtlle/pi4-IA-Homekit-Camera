"""Tests for the [BETA] USB webcam backend (#19) — no hardware.

The ffmpeg command builder and the JPEG stream splitter are pure logic;
the CameraManager-compatible surface is checked so the downstream contract
(publisher / detector / control server) can rely on it.
"""
from camera.usb_camera_manager import UsbCameraManager

CONFIG = {
    "camera": {
        "width": 1280, "height": 720, "fps": 30, "bitrate": 4_000_000,
        "device": "/dev/video2", "usb_format": "mjpeg",
        "lores_width": 320, "lores_height": 240, "snapshot_interval": 5,
    },
    "detection": {"analysis_fps": 5},
}


def make(**camera):
    cfg = {"camera": {**CONFIG["camera"], **camera}, "detection": CONFIG["detection"]}
    return UsbCameraManager(cfg)


# ----------------------------------------------------------------------
# ffmpeg command builder
# ----------------------------------------------------------------------

def test_mjpeg_webcam_uses_the_hardware_encoder():
    args = make()._build_ffmpeg_args(3, 4, 5)
    joined = " ".join(args)
    assert "-input_format mjpeg" in joined
    assert "-i /dev/video2" in joined
    assert "-c:v h264_v4l2m2m" in joined          # hardware, never libx264
    assert "libx264" not in joined
    assert "-b:v 4000000" in joined
    assert "-g 30" in joined                       # 1 s GOP, like the CSI side
    assert "pipe:3" in joined and "pipe:4" in joined and "pipe:5" in joined


def test_h264_webcam_is_pure_passthrough():
    args = make(usb_format="h264")._build_ffmpeg_args(3, 4, 5)
    joined = " ".join(args)
    assert "-input_format h264" in joined
    assert "-c:v copy" in joined
    assert "h264_v4l2m2m" not in joined            # zero re-encode


def test_yuyv_maps_to_ffmpegs_yuyv422():
    args = make(usb_format="yuyv")._build_ffmpeg_args(3, 4, 5)
    assert "yuyv422" in " ".join(args)


def test_unknown_format_falls_back_to_mjpeg():
    cam = make(usb_format="nonsense")
    assert cam._format == "mjpeg"


def test_lores_output_matches_detector_expectations():
    joined = " ".join(make()._build_ffmpeg_args(3, 4, 5))
    # gray 320x240 at analysis_fps: exactly what PresenceDetector consumes
    assert "fps=5,scale=320:240,format=gray" in joined
    assert "-f rawvideo" in joined


def test_snapshot_disabled_drops_the_third_output():
    joined = " ".join(make(snapshot_interval=0)._build_ffmpeg_args(3, 4, 5))
    assert "image2pipe" not in joined


# ----------------------------------------------------------------------
# JPEG stream splitter
# ----------------------------------------------------------------------

def test_split_jpegs_extracts_complete_images_and_keeps_the_partial():
    j1 = b"\xff\xd8AAA\xff\xd9"
    j2 = b"\xff\xd8BBBBB\xff\xd9"
    partial = b"\xff\xd8CC"
    jpegs, rest = UsbCameraManager._split_jpegs(j1 + j2 + partial)
    assert jpegs == [j1, j2]
    assert rest == partial


def test_split_jpegs_waits_on_incomplete_data():
    jpegs, rest = UsbCameraManager._split_jpegs(b"\xff\xd8not-done-yet")
    assert jpegs == []
    assert rest == b"\xff\xd8not-done-yet"


# ----------------------------------------------------------------------
# CameraManager-compatible surface
# ----------------------------------------------------------------------

def test_control_surface_is_compatible_but_inert():
    cam = make()
    # the ControlServer wires these blindly — they must exist and be safe
    assert cam.set_bitrate(1_000_000) == 4_000_000   # fixed bitrate stays
    assert cam.force_keyframe() is False


def test_defaults():
    cam = UsbCameraManager({"camera": {}})
    assert cam._device == "/dev/video0"
    assert cam._format == "mjpeg"
