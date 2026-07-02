"""Tests for the chroma-based IR night-vision detector (v2) — no hardware.

Covers the pure frame classifier (_is_ir_frame) and the hysteresis state
machine (_apply_ir_vote) that the v1 probe design never had tests for.
"""
from camera.camera_manager import CameraManager

ENTRY = CameraManager.IR_ENTRY_FRAMES
EXIT = CameraManager.IR_EXIT_FRAMES


def make_manager() -> CameraManager:
    # Direct construction on purpose (bypasses the singleton); __init__ never
    # touches picamera2, so this works with the conftest hardware mocks.
    return CameraManager({"camera": {"ir_grayscale": True}})


# ----------------------------------------------------------------------
# _is_ir_frame — one frame's chroma statistics
# ----------------------------------------------------------------------

def test_ir_pink_cast():
    # uniform chroma, warm cast (the original rig's signature) → IR
    assert CameraManager._is_ir_frame(u_mean=120, u_std=2.0, v_mean=140, v_std=3.0)


def test_ir_blue_cast():
    # observed on the enclosure's LED pods + fisheye lens: the AWB landed
    # blue — uniform chroma pushed cold. Direction must not matter.
    assert CameraManager._is_ir_frame(u_mean=150, u_std=2.0, v_mean=112, v_std=2.5)


def test_ir_red_cast():
    # also observed between nights on the same hardware
    assert CameraManager._is_ir_frame(u_mean=124, u_std=1.5, v_mean=155, v_std=3.0)


def test_ir_with_awb_partially_cancelling_cast():
    # AWB pulled the cast down but couldn't kill it; still uniform → IR
    assert CameraManager._is_ir_frame(u_mean=126, u_std=1.5, v_mean=133, v_std=2.0)


def test_colourful_daylight():
    # diverse hues → high chroma std → not IR
    assert not CameraManager._is_ir_frame(u_mean=125, u_std=18.0, v_mean=135, v_std=20.0)


def test_grey_overcast_scene_without_cast():
    # uniform but chroma-neutral (fog, concrete): no cast at all → not IR
    assert not CameraManager._is_ir_frame(u_mean=128, u_std=1.0, v_mean=128.5, v_std=1.0)


def test_headlights_break_uniformity():
    # night scene lit by a warm headlight beam: cast present but V no longer
    # uniform → single frames read as not-IR (hysteresis absorbs them)
    assert not CameraManager._is_ir_frame(u_mean=122, u_std=3.0, v_mean=140, v_std=15.0)


def test_cast_threshold_boundary_warm():
    at = 128.0 + CameraManager.IR_CAST_MIN
    assert not CameraManager._is_ir_frame(128, 1.0, at, 1.0)        # not strictly above
    assert CameraManager._is_ir_frame(128, 1.0, at + 0.1, 1.0)      # just above


def test_cast_threshold_boundary_cold():
    # symmetric: a cast below neutral counts with the same amplitude
    at = 128.0 - CameraManager.IR_CAST_MIN
    assert not CameraManager._is_ir_frame(128, 1.0, at, 1.0)
    assert CameraManager._is_ir_frame(128, 1.0, at - 0.1, 1.0)


def test_cast_on_u_plane_alone_counts():
    # the offset can sit on either plane — U-only cast is enough
    assert CameraManager._is_ir_frame(
        u_mean=128.0 + CameraManager.IR_CAST_MIN + 0.1, u_std=1.0,
        v_mean=128.0, v_std=1.0,
    )


def test_std_threshold_boundary():
    at = CameraManager.IR_CHROMA_STD_MAX
    assert not CameraManager._is_ir_frame(120, at, 140, 1.0)        # u_std not below max
    assert CameraManager._is_ir_frame(120, at - 0.1, 140, 1.0)


# ----------------------------------------------------------------------
# _apply_ir_vote — hysteresis state machine
# ----------------------------------------------------------------------

def test_entry_requires_consecutive_ir_frames():
    cam = make_manager()
    for _ in range(ENTRY - 1):
        cam._apply_ir_vote(True)
    assert cam._ir_mode is False
    cam._apply_ir_vote(True)
    assert cam._ir_mode is True


def test_one_daylight_frame_resets_the_entry_streak():
    cam = make_manager()
    for _ in range(ENTRY - 1):
        cam._apply_ir_vote(True)
    cam._apply_ir_vote(False)  # flicker: streak resets
    for _ in range(ENTRY - 1):
        cam._apply_ir_vote(True)
    assert cam._ir_mode is False  # needs the full run again
    cam._apply_ir_vote(True)
    assert cam._ir_mode is True


def test_exit_is_slower_than_entry():
    assert EXIT > ENTRY


def test_exit_requires_consecutive_daylight_frames():
    cam = make_manager()
    cam._ir_mode = True
    for _ in range(EXIT - 1):
        cam._apply_ir_vote(False)
    assert cam._ir_mode is True
    cam._apply_ir_vote(False)
    assert cam._ir_mode is False


def test_headlight_flicker_does_not_exit_night_mode():
    cam = make_manager()
    cam._ir_mode = True
    # repeated bursts of non-IR votes, always shorter than the exit window,
    # each interrupted by an IR frame (headlights passing)
    for _ in range(10):
        for _ in range(EXIT - 1):
            cam._apply_ir_vote(False)
        cam._apply_ir_vote(True)
    assert cam._ir_mode is True


def test_matching_votes_keep_streak_reset():
    cam = make_manager()
    cam._apply_ir_vote(False)  # matches day mode
    assert cam._ir_streak == 0
    cam._ir_mode = True
    cam._apply_ir_vote(True)   # matches night mode
    assert cam._ir_streak == 0
