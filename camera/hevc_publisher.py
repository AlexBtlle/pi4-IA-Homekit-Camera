"""
Multi-tier publisher (#59 Volet 2 — Pi 5 + the new HKSV spec).

Consumes the RAW YUV420 pipe that CameraManager produces in multi-tier mode
and runs the entire encode fan-out in ONE ffmpeg process. Per-tier codec is
configurable; the DEFAULTS encode the Pi 5 capacity map measured on real
hardware (issue #59, 2026-07):

  - 2K30 x265 alone: 23.8/30 fps, all 4 cores saturated → HEVC at 2K is out
    of software reach (x265 ultrafast ≈ 19.4 MP/s per A76 core).
  - 2K30 x264 alone: 29.98 fps at ~40 % of the SoC (x264 ≈ 4-5x cheaper).
  - 2K x264 + Low x265 + MOG2: 29.98 fps at 54 % — validated headroom.

    raw 2K YUV ─► split ─┬─► x264 High  (2304x1296@30)  ─► rtsp …/camera_high
                         ├─► x264 Low   (640x360@15)    ─► rtsp …/camera_low
                         └─► x264 legacy (camera.width/height) ─► rtsp …/camera

The Medium tier (1080p30) is DISABLED here by default: it is the legacy
/camera stream itself — the WebRTC layer maps its medium source to the same
RTSP path, reusing that encode for zero extra CPU. The x264 legacy leg keeps
the existing HomeKit service (live passthrough + HKSV HDS recording) working
unchanged for legacy controllers — in this mode picamera2 does no encoding
at all. Whether the new-spec hub accepts an H.264 2K tier is the tvOS 27
probe's question; per-tier `codec: h265` stays available for the answer.

Process lifecycle (spawn, exponential backoff, drain-while-down, FIONREAD
stall kill) is inherited from RtspPublisher — only the command differs.
Uses the SYSTEM ffmpeg: the project's static build has neither libx265 nor
libopus (deliberate — it exists for the Zero 2 W, which never runs this).
"""
import logging

from .rtsp_publisher import RtspPublisher

logger = logging.getLogger(__name__)

# Defaults per the measured capacity map; config overrides. Bitrates in b/s.
# High is H.264 at ~1.6x the spec's HEVC target (H.264 needs more bits for
# the same quality; the LAN doesn't care). Geometry = IMX708 native binned.
_TIER_DEFAULTS = {
    "high": {"enabled": True, "codec": "h264", "width": 2304, "height": 1296,
             "fps": 30, "bitrate": 4_500_000, "max_bitrate": 5_000_000},
    "medium": {"enabled": False, "codec": "h264", "width": 1920, "height": 1080,
               "fps": 30, "bitrate": 1_700_000, "max_bitrate": 1_800_000},
    "low": {"enabled": True, "codec": "h264", "width": 640, "height": 360,
            "fps": 15, "bitrate": 180_000, "max_bitrate": 190_000},
}
_INT_TIER_KEYS = ("width", "height", "fps", "bitrate", "max_bitrate")


class HevcPublisher(RtspPublisher):
    """RtspPublisher with the raw-input, multi-output ffmpeg command."""

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
        for k, v in (self._info["tiers"].get(name) or {}).items():
            merged[k] = int(v) if k in _INT_TIER_KEYS else v
        return merged

    # -- command -----------------------------------------------------------

    def _encode_leg(self, pad: str, tier: dict, path: str) -> list[str]:
        """One output leg — x264 or x265 per the tier's codec, same 1 s GOP,
        scene-cut off, zerolatency (no B-frames: wallclock stamping + HKSV
        passthrough need display-order frames)."""
        gop = int(tier["fps"])
        codec = str(tier.get("codec", "h264")).lower()
        if codec in ("h265", "hevc", "x265"):
            codec_args = [
                "-c:v", "libx265",
                "-preset", str(self._info.get("preset", "ultrafast")),
                "-tune", "zerolatency",
                "-x265-params", f"keyint={gop}:min-keyint={gop}:scenecut=0",
            ]
        else:
            # superfast mirrors what picamera2's libav encoder picks for
            # profile "high" (Volet 1).
            codec_args = [
                "-c:v", "libx264",
                "-preset", "superfast",
                "-tune", "zerolatency",
                "-profile:v", "high",
                "-g", str(gop), "-sc_threshold", "0",
            ]
        return [
            "-map", f"[{pad}]",
            *codec_args,
            "-b:v", str(tier["bitrate"]),
            "-maxrate", str(tier["max_bitrate"]),
            "-bufsize", str(tier["max_bitrate"]),
            "-rtsp_transport", "tcp",
            "-f", "rtsp", f"{self._base_url}/{path}",
        ]

    def _ffmpeg_args(self) -> list[str]:
        w, h = int(self._info["width"]), int(self._info["height"])
        fps = int(self._info["fps"])
        stride, padded_h, crop = self._padded_geometry()
        legacy = self._info["legacy"]

        # Enabled tiers, in ladder order. Medium is off by default — the
        # legacy 1080p x264 stream doubles as the Medium source (mapped in
        # the WebRTC layer), costing zero extra encode.
        tiers = {n: self._tier(n) for n in ("high", "medium", "low")}
        active = [n for n, t in tiers.items() if t.get("enabled", True)]
        pads = {"high": "hi", "medium": "mid", "low": "lo"}

        src = "[0:v]"
        graph = []
        if crop:
            graph.append(f"{src}crop={w}:{h}:0:0[vis]")
            src = "[vis]"
        split_pads = "".join(f"[{pads[n]}]" for n in active) + "[leg]"
        graph.append(f"{src}split={len(active) + 1}{split_pads}")
        out_pads = {}
        for name in active:
            t = tiers[name]
            pad = pads[name]
            filters = []
            if (t["width"], t["height"]) != (w, h):
                filters.append(f"scale={t['width']}:{t['height']}")
            if t["fps"] != fps:
                filters.append(f"fps={t['fps']}")
            if filters:
                graph.append(f"[{pad}]{','.join(filters)}[{pad}2]")
                out_pads[name] = f"{pad}2"
            else:
                out_pads[name] = pad
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
        for name in active:
            args += self._encode_leg(out_pads[name], tiers[name], f"camera_{name}")
        # Legacy leg — feeds the unchanged HomeKit passthrough path (and the
        # Medium tier by reuse).
        legacy_tier = {
            "codec": "h264", "fps": fps,
            "bitrate": int(legacy["bitrate"]),
            "max_bitrate": int(legacy["bitrate"]),
        }
        args += self._encode_leg("leg2", legacy_tier, "camera")
        return args
