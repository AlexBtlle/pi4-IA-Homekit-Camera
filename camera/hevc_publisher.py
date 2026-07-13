"""
HEVC multi-tier publisher (#59 Volet 2 — Pi 5 + the new HKSV spec).

Consumes the RAW YUV420 pipe that CameraManager produces in HEVC mode and
runs the entire encode fan-out in ONE ffmpeg process:

    raw 2K YUV ─► split ─┬─► libx265  High   (e.g. 2560x1440@30) ─► rtsp …/camera_hevc_high
                         ├─► libx265  Medium (1920x1080@30)      ─► rtsp …/camera_hevc_medium
                         ├─► libx265  Low    (640x360@15)        ─► rtsp …/camera_hevc_low
                         └─► libx264  legacy (camera.width/height) ─► rtsp …/camera

The x264 leg keeps rtsp://…/camera alive so the existing HomeKit service
(live passthrough + HKSV HDS recording) works unchanged for legacy
controllers — in HEVC mode picamera2 does no encoding at all.

Process lifecycle (spawn, exponential backoff, drain-while-down, FIONREAD
stall kill) is inherited from RtspPublisher — only the command differs.
Uses the SYSTEM ffmpeg: the project's static build has neither libx265 nor
libopus (deliberate — it exists for the Zero 2 W, which never runs this).
"""
import logging

from .rtsp_publisher import RtspPublisher

logger = logging.getLogger(__name__)

# Spec target bitrates (§2, bits per second) as defaults; config overrides.
_TIER_DEFAULTS = {
    "high": {"width": 2560, "height": 1440, "fps": 30,
             "bitrate": 2_800_000, "max_bitrate": 3_000_000},
    "medium": {"width": 1920, "height": 1080, "fps": 30,
               "bitrate": 1_700_000, "max_bitrate": 1_800_000},
    "low": {"width": 640, "height": 360, "fps": 15,
            "bitrate": 180_000, "max_bitrate": 190_000},
}


class HevcPublisher(RtspPublisher):
    """RtspPublisher with the raw-input, four-output ffmpeg command."""

    def __init__(self, pipe_r_fd: int, rtsp_base_url: str, info: dict):
        # rtsp_base_url = "rtsp://localhost:8554" (no path) — paths are fixed.
        super().__init__(pipe_r_fd, f"{rtsp_base_url}/camera")
        self._base_url = rtsp_base_url.rstrip("/")
        self._info = info
        self._thread.name = "hevc-publisher"

    # -- geometry ----------------------------------------------------------

    def _padded_geometry(self) -> tuple[int, int, bool]:
        """
        (buffer_width, buffer_height, crop_needed) describing the pipe's real
        frame layout. The ISP pads rows to `stride` and may pad the plane
        height (framesize > stride*h*1.5): describe the padded geometry to
        ffmpeg and crop back to the visible frame, instead of repacking
        166 MB/s in Python.
        """
        w, h = int(self._info["width"]), int(self._info["height"])
        stride = int(self._info.get("stride", w))
        framesize = int(self._info.get("framesize", w * h * 3 // 2))
        padded_h = (framesize * 2) // (stride * 3)
        if framesize != stride * padded_h * 3 // 2:
            # Layout we can't describe as WxH yuv420p — should not happen on
            # the Pi ISPs; fail loudly rather than feed ffmpeg garbage.
            raise ValueError(
                f"unsupported main buffer layout: stride={stride} "
                f"framesize={framesize} for {w}x{h}"
            )
        return stride, padded_h, (stride != w or padded_h != h)

    def _tier(self, name: str) -> dict:
        merged = dict(_TIER_DEFAULTS[name])
        merged.update({k: int(v) for k, v in (self._info["tiers"].get(name) or {}).items()})
        return merged

    # -- command -----------------------------------------------------------

    def _x265_leg(self, pad: str, tier: dict, path: str) -> list[str]:
        gop = int(tier["fps"])
        return [
            "-map", f"[{pad}]",
            "-c:v", "libx265",
            "-preset", str(self._info.get("preset", "ultrafast")),
            "-tune", "zerolatency",  # no B-frames: wallclock stamping + HKSV
            "-b:v", str(tier["bitrate"]),
            "-maxrate", str(tier["max_bitrate"]),
            "-bufsize", str(tier["max_bitrate"]),
            # 1 s GOP, no scene-cut keyframes: same latency/fragment trade-off
            # as the classic pipeline's iperiod=fps.
            "-x265-params", f"keyint={gop}:min-keyint={gop}:scenecut=0",
            "-rtsp_transport", "tcp",
            "-f", "rtsp", f"{self._base_url}/{path}",
        ]

    def _ffmpeg_args(self) -> list[str]:
        w, h = int(self._info["width"]), int(self._info["height"])
        fps = int(self._info["fps"])
        stride, padded_h, crop = self._padded_geometry()
        high, medium, low = (self._tier(n) for n in ("high", "medium", "low"))
        legacy = self._info["legacy"]

        src = "[0:v]"
        graph = []
        if crop:
            graph.append(f"{src}crop={w}:{h}:0:0[vis]")
            src = "[vis]"
        graph.append(f"{src}split=4[hi][mid][lo][leg]")
        graph.append(f"[mid]scale={medium['width']}:{medium['height']}[mid2]")
        graph.append(f"[lo]scale={low['width']}:{low['height']},fps={low['fps']}[lo2]")
        graph.append(f"[leg]scale={legacy['width']}:{legacy['height']}[leg2]")

        args = [
            "ffmpeg",
            "-hide_banner", "-loglevel", "warning",
            "-f", "rawvideo", "-pix_fmt", "yuv420p",
            "-video_size", f"{stride}x{padded_h}",
            # Same lesson as the H264 pipe (#57): the sensor's real delivery
            # rate drops below nominal in low light — stamp frames by arrival
            # time, never by a declared -r, or the clips play fast.
            "-use_wallclock_as_timestamps", "1",
            "-i", f"pipe:{self._pipe_r_fd}",
            "-filter_complex", ";".join(graph),
        ]
        args += self._x265_leg("hi", high, "camera_hevc_high")
        args += self._x265_leg("mid2", medium, "camera_hevc_medium")
        args += self._x265_leg("lo2", low, "camera_hevc_low")
        # Legacy x264 leg — feeds the unchanged HomeKit passthrough path.
        # superfast mirrors what picamera2's libav encoder picks for
        # profile "high" (Volet 1), zerolatency keeps display-order frames.
        args += [
            "-map", "[leg2]",
            "-c:v", "libx264",
            "-preset", "superfast",
            "-tune", "zerolatency",
            "-profile:v", "high",
            "-b:v", str(int(legacy["bitrate"])),
            "-g", str(fps), "-sc_threshold", "0",
            "-rtsp_transport", "tcp",
            "-f", "rtsp", f"{self._base_url}/camera",
        ]
        return args
