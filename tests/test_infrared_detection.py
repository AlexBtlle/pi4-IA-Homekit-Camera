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


def test_ir_real_night_measurement():
    # Regression: actual journalctl numbers from the rig (2026-07-02 22:48).
    # The multiplicative cast makes u_std huge — the strong-cast tier must
    # classify this as IR without any uniformity requirement.
    assert CameraManager._is_ir_frame(u_mean=186.3, u_std=25.1, v_mean=121.4, v_std=2.5)


def test_strong_cast_boundary():
    at = 128.0 + CameraManager.IR_CAST_STRONG
    # at the threshold with noisy std: falls through to the moderate tier,
    # which rejects on uniformity
    assert not CameraManager._is_ir_frame(at, 25.0, 128.0, 25.0)
    # just above: strong tier fires regardless of std
    assert CameraManager._is_ir_frame(at + 0.1, 25.0, 128.0, 25.0)


def test_ir_red_cast():
    # also observed between nights on the same hardware
    assert CameraManager._is_ir_frame(u_mean=124, u_std=1.5, v_mean=155, v_std=3.0)


def test_ir_with_awb_partially_cancelling_cast():
    # AWB pulled the cast down but couldn't kill it; still uniform → IR
    assert CameraManager._is_ir_frame(u_mean=126, u_std=1.5, v_mean=138, v_std=2.0)


def test_real_morning_muted_daylight():
    # Regression: actual journalctl numbers (2026-07-03 07:04, after the IR
    # LEDs switched off). Early-morning colours are muted — V sits ~4 from
    # neutral with stds near the uniformity limit. Must stay colour: this is
    # why IR_CAST_MIN is 8, not 4.
    assert not CameraManager._is_ir_frame(u_mean=128.6, u_std=7.2, v_mean=123.9, v_std=5.9)
    # same scene, a touch calmer — even if stds dip under the limit, the
    # moderate cast alone must not flip it
    assert not CameraManager._is_ir_frame(u_mean=128.7, u_std=5.5, v_mean=123.6, v_std=5.6)


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


# ----------------------------------------------------------------------
# Night camera tuning (_apply_night_camera, driven by _apply_ir_vote)
# ----------------------------------------------------------------------

class FakePicam2:
    """Records set_controls calls so tests can assert the night tuning."""

    def __init__(self):
        self.controls_log = []

    def set_controls(self, controls):
        self.controls_log.append(dict(controls))


def make_night_manager(**camera):
    # fps defaults to 30 here, so ir_min_fps=10 (< 30) enables the shutter lever.
    cam = CameraManager({"camera": {"ir_grayscale": True, **camera}})
    cam._picam2 = FakePicam2()
    return cam


LONG = CameraManager._AE_EXPOSURE_LONG
NORMAL = CameraManager._AE_EXPOSURE_NORMAL


def test_config_read_and_clamped():
    d = CameraManager({"camera": {}})
    assert d._ir_exposure == 0.0
    assert d._ir_min_fps == 10                       # default
    c = CameraManager({"camera": {"ir_exposure": 1.5, "ir_min_fps": 8}})
    assert c._ir_exposure == 1.5 and c._ir_min_fps == 8
    # EV clamped to libcamera's ±8 range; fps floored to >= 1
    assert CameraManager({"camera": {"ir_exposure": 99}})._ir_exposure == 8.0
    assert CameraManager({"camera": {"ir_exposure": -99}})._ir_exposure == -8.0
    assert CameraManager({"camera": {"ir_min_fps": 0}})._ir_min_fps == 1


def test_night_transition_relaxes_shutter_and_reverts():
    cam = make_night_manager(ir_min_fps=10, ir_exposure=1.5)
    for _ in range(ENTRY):          # latch into night
        cam._apply_ir_vote(True)
    assert cam._ir_mode is True
    on = cam._picam2.controls_log[-1]
    assert on["FrameDurationLimits"] == (33333, 100000)   # 30 fps min .. 10 fps max
    assert on["AeExposureMode"] == LONG
    assert on["ExposureValue"] == 1.5
    for _ in range(EXIT):           # return to day
        cam._apply_ir_vote(False)
    assert cam._ir_mode is False
    off = cam._picam2.controls_log[-1]
    assert off["FrameDurationLimits"] == (33333, 33333)   # re-pinned to 30 fps
    assert off["AeExposureMode"] == NORMAL
    assert off["ExposureValue"] == 0.0


def test_tuning_fires_once_per_transition_not_per_frame():
    cam = make_night_manager(ir_min_fps=10)
    for _ in range(ENTRY + 20):     # keep voting night well past the latch
        cam._apply_ir_vote(True)
    assert len(cam._picam2.controls_log) == 1   # one write for the single flip


def test_ev_only_when_shutter_lever_disabled():
    # ir_min_fps >= fps disables the shutter lever; only the EV bias remains.
    cam = make_night_manager(ir_min_fps=30, ir_exposure=1.0)
    for _ in range(ENTRY):
        cam._apply_ir_vote(True)
    assert cam._picam2.controls_log[-1] == {"ExposureValue": 1.0}


def test_shutter_only_when_no_ev():
    cam = make_night_manager(ir_min_fps=10, ir_exposure=0.0)
    for _ in range(ENTRY):
        cam._apply_ir_vote(True)
    on = cam._picam2.controls_log[-1]
    assert "ExposureValue" not in on
    assert on["FrameDurationLimits"] == (33333, 100000)
    assert on["AeExposureMode"] == LONG


def test_fully_disabled_never_touches_controls():
    # Shutter lever off AND no EV → nothing at all, existing behaviour preserved.
    cam = make_night_manager(ir_min_fps=30, ir_exposure=0.0)
    for _ in range(ENTRY):
        cam._apply_ir_vote(True)
    assert cam._ir_mode is True
    assert cam._picam2.controls_log == []


def test_apply_night_camera_without_camera_is_noop():
    # tests/hardware-less construction leaves _picam2 = None → must not raise
    cam = CameraManager({"camera": {"ir_grayscale": True, "ir_min_fps": 10, "ir_exposure": 2.0}})
    assert cam._picam2 is None
    cam._apply_night_camera(True)   # no crash, no effect
