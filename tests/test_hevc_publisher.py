"""Tests for HevcPublisher's ffmpeg command (#59 Volet 2) — no hardware.

The process lifecycle (backoff/drain/stall) is RtspPublisher's, already
covered by test_rtsp_publisher.py; here we pin down the multi-tier command.
Tier defaults encode the measured Pi 5 capacity map (issue #59): High is
H.264 (2K30 x265 saturates the SoC at 23.8/30 fps — field fact), Medium is
disabled (the legacy 1080p stream doubles as Medium), per-tier codec is
switchable for whatever the tvOS 27 probe reveals.
"""
import pytest

from camera.hevc_publisher import HevcPublisher


def make_info(**overrides) -> dict:
    info = {
        "width": 2304,
        "height": 1296,
        "stride": 2304,
        "framesize": 2304 * 1296 * 3 // 2,
        "fps": 30,
        "preset": "ultrafast",
        "tiers": {"high": {}, "medium": {}, "low": {}},
        "legacy": {"width": 1920, "height": 1080, "bitrate": 4_000_000},
    }
    info.update(overrides)
    return info


def make_publisher(**overrides) -> HevcPublisher:
    return HevcPublisher(7, "rtsp://localhost:8554", make_info(**overrides))


def output_urls(args: list) -> list:
    """URLs following each '-f rtsp' output pair."""
    return [
        args[i + 2]
        for i in range(len(args) - 2)
        if args[i] == "-f" and args[i + 1] == "rtsp"
    ]


def test_default_ladder_is_h264_high_low_plus_legacy():
    args = make_publisher()._ffmpeg_args()
    assert output_urls(args) == [
        "rtsp://localhost:8554/camera_high",
        "rtsp://localhost:8554/camera_low",
        "rtsp://localhost:8554/camera",
    ]
    # Measured capacity map: everything H.264 by default, no x265 at all.
    assert args.count("libx264") == 3
    assert args.count("libx265") == 0
    graph = args[args.index("-filter_complex") + 1]
    assert "split=3" in graph  # medium disabled → high, low, legacy


def test_high_tier_native_binned_needs_no_scale():
    args = make_publisher()._ffmpeg_args()
    graph = args[args.index("-filter_complex") + 1]
    # capture == high geometry → the high pad goes straight to its encoder
    assert "[hi]scale" not in graph
    assert "scale=640:360" in graph and "fps=15" in graph  # low tier
    assert "scale=1920:1080[leg2]" in graph  # legacy


def test_h265_tier_is_still_available_per_config():
    args = make_publisher(
        tiers={"high": {"codec": "h265"}, "medium": {}, "low": {}},
    )._ffmpeg_args()
    assert args.count("libx265") == 1  # high back to HEVC on demand
    assert args.count("libx264") == 2  # low + legacy
    joined = " ".join(args)
    assert "keyint=30:min-keyint=30:scenecut=0" in joined


def test_medium_tier_can_be_enabled():
    args = make_publisher(
        tiers={"high": {}, "medium": {"enabled": True}, "low": {}},
    )._ffmpeg_args()
    urls = output_urls(args)
    assert "rtsp://localhost:8554/camera_medium" in urls
    graph = args[args.index("-filter_complex") + 1]
    assert "split=4" in graph
    assert "scale=1920:1080[mid2]" in graph


def test_raw_input_is_wallclock_stamped_before_i():
    # Same failure mode as #57: a declared nominal rate would make low-light
    # footage (sensor below nominal fps) play back fast in HKSV clips.
    args = make_publisher()._ffmpeg_args()
    assert "-use_wallclock_as_timestamps" in args
    assert args.index("-use_wallclock_as_timestamps") < args.index("-i")
    assert "-r" not in args


def test_padded_buffer_is_described_then_cropped():
    # 2368-byte stride and 16-row height padding: ffmpeg must read the
    # PADDED geometry and crop back to the visible frame.
    stride, w, h, padded_h = 2368, 2304, 1296, 1312
    args = make_publisher(
        stride=stride, framesize=stride * padded_h * 3 // 2,
    )._ffmpeg_args()
    assert args[args.index("-video_size") + 1] == f"{stride}x{padded_h}"
    graph = args[args.index("-filter_complex") + 1]
    assert f"crop={w}:{h}:0:0" in graph


def test_inconsistent_layout_fails_loudly():
    pub = make_publisher(framesize=12345)  # not stride*h*1.5 for any h
    with pytest.raises(ValueError, match="unsupported main buffer layout"):
        pub._ffmpeg_args()


def test_capacity_map_bitrate_defaults():
    args = make_publisher()._ffmpeg_args()
    joined = " ".join(args)
    # High: H.264 at 4.5/5 Mb/s (HEVC's 2800k target is not enough for x264)
    assert "-b:v 4500000 -maxrate 5000000" in joined
    # 1 s GOP on the H.264 legs
    assert "-g 30 -sc_threshold 0" in joined
    assert "-g 15 -sc_threshold 0" in joined  # low tier at its own fps


def test_config_overrides_reach_the_command():
    args = make_publisher(
        tiers={
            "high": {"bitrate": 6_000_000, "max_bitrate": 7_000_000},
            "medium": {},
            "low": {},
        },
    )._ffmpeg_args()
    assert "-b:v 6000000 -maxrate 7000000" in " ".join(args)


def test_legacy_leg_matches_camera_config():
    args = make_publisher()._ffmpeg_args()
    joined = " ".join(args)
    assert "-c:v libx264 -preset superfast -tune zerolatency -profile:v high" in joined
    assert "-b:v 4000000" in joined
    assert output_urls(args)[-1] == "rtsp://localhost:8554/camera"
