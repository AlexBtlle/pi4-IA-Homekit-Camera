"""Tests for HevcPublisher's ffmpeg command (#59 Volet 2) — no hardware.

The process lifecycle (backoff/drain/stall) is RtspPublisher's, already
covered by test_rtsp_publisher.py; here we pin down the four-output command:
geometry description of the raw pipe, the x265 ladder, the legacy x264 leg,
and the wallclock-timestamps lesson from #57.
"""
import pytest

from camera.hevc_publisher import HevcPublisher


def make_info(**overrides) -> dict:
    info = {
        "width": 2560,
        "height": 1440,
        "stride": 2560,
        "framesize": 2560 * 1440 * 3 // 2,
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


def test_four_outputs_three_hevc_one_legacy():
    args = make_publisher()._ffmpeg_args()
    urls = output_urls(args)
    assert urls == [
        "rtsp://localhost:8554/camera_hevc_high",
        "rtsp://localhost:8554/camera_hevc_medium",
        "rtsp://localhost:8554/camera_hevc_low",
        "rtsp://localhost:8554/camera",
    ]
    assert args.count("libx265") == 3
    assert args.count("libx264") == 1


def test_raw_input_is_wallclock_stamped_before_i():
    # Same failure mode as #57: a declared nominal rate would make low-light
    # footage (sensor below nominal fps) play back fast in HKSV clips.
    args = make_publisher()._ffmpeg_args()
    assert "-use_wallclock_as_timestamps" in args
    assert args.index("-use_wallclock_as_timestamps") < args.index("-i")
    assert "-r" not in args


def test_aligned_buffer_needs_no_crop():
    args = make_publisher()._ffmpeg_args()
    graph = args[args.index("-filter_complex") + 1]
    assert graph.startswith("[0:v]split=4")
    assert "crop" not in graph
    assert args[args.index("-video_size") + 1] == "2560x1440"


def test_padded_buffer_is_described_then_cropped():
    # 2304-wide binned mode with a 2368-byte stride and 16-row height padding:
    # ffmpeg must read the PADDED geometry and crop back to the visible frame.
    stride, w, h, padded_h = 2368, 2304, 1296, 1312
    args = make_publisher(
        width=w, height=h, stride=stride, framesize=stride * padded_h * 3 // 2,
    )._ffmpeg_args()
    assert args[args.index("-video_size") + 1] == f"{stride}x{padded_h}"
    graph = args[args.index("-filter_complex") + 1]
    assert f"crop={w}:{h}:0:0" in graph


def test_inconsistent_layout_fails_loudly():
    pub = make_publisher(framesize=12345)  # not stride*h*1.5 for any h
    with pytest.raises(ValueError, match="unsupported main buffer layout"):
        pub._ffmpeg_args()


def test_spec_bitrates_and_gop_defaults():
    args = make_publisher()._ffmpeg_args()
    joined = " ".join(args)
    # High tier: spec 2800k avg / 3000k max; 1 s GOP at 30 fps
    assert "-b:v 2800000 -maxrate 3000000" in joined
    assert "keyint=30:min-keyint=30:scenecut=0" in joined
    # Low tier: 15 fps → its own 1 s GOP and an fps filter
    assert "keyint=15:min-keyint=15:scenecut=0" in joined
    assert "fps=15" in args[args.index("-filter_complex") + 1]


def test_config_overrides_reach_the_command():
    args = make_publisher(
        preset="veryfast",
        tiers={
            "high": {"bitrate": 5_000_000, "max_bitrate": 6_000_000},
            "medium": {},
            "low": {},
        },
    )._ffmpeg_args()
    joined = " ".join(args)
    assert "-preset veryfast" in joined
    assert "-b:v 5000000 -maxrate 6000000" in joined


def test_legacy_leg_matches_camera_config():
    args = make_publisher()._ffmpeg_args()
    joined = " ".join(args)
    assert "scale=1920:1080[leg2]" in args[args.index("-filter_complex") + 1]
    assert "-c:v libx264 -preset superfast -tune zerolatency -profile:v high -b:v 4000000" in joined
    # 1 s GOP, scene-cut disabled — mirrors the classic pipeline's iperiod=fps
    assert "-g 30 -sc_threshold 0" in joined
