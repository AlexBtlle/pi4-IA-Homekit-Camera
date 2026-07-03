#!/usr/bin/env python3
"""Phase 0 gate for #47 — can the hardware H264 encoder change bitrate live?

Starts the encoder at 8 Mbps, switches the V4L2 bitrate control to 2 Mbps
WHILE STREAMING, then back to 8, and measures the real output bitrate per
second. Prints a PASS/FAIL verdict at the end.

Usage (on the Pi, the camera must be free):
    sudo systemctl stop pi4cam
    python3 scripts/test_dynamic_bitrate.py
    sudo systemctl start pi4cam

Tip: wave at the camera during the run — a fully static scene lets the
encoder undershoot its target, which blurs the measurement.
"""
import ctypes
import fcntl
import time

from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import Output

# (target bps, seconds to hold it)
PHASES = [(8_000_000, 10), (2_000_000, 10), (8_000_000, 10)]


class CountingOutput(Output):
    """Discards the H264 stream, only counting bytes for throughput."""

    def __init__(self):
        super().__init__()
        self.bytes = 0

    def outputframe(self, frame, keyframe=True, *args, **kwargs):
        self.bytes += len(frame)


def set_bitrate_live(encoder, bps: int) -> None:
    """Poke V4L2_CID_MPEG_VIDEO_BITRATE on the running encoder's fd —
    the exact mechanism CameraManager would use in Phase 1.

    The V4L2 constants/structs are taken from picamera2's own encoder module
    namespace (it star-imports them from whatever V4L2 binding the distro
    ships — `videodev2` on Pi OS Bookworm), so this stays in lockstep with
    whatever picamera2 itself uses.
    """
    from picamera2.encoders import v4l2_encoder as v4l2

    ctrl = v4l2.v4l2_ext_control()
    ctrl.id = v4l2.V4L2_CID_MPEG_VIDEO_BITRATE
    ctrl.value = bps
    ctrls = v4l2.v4l2_ext_controls()
    ctrls.ctrl_class = v4l2.V4L2_CTRL_CLASS_MPEG
    ctrls.count = 1
    ctrls.controls = ctypes.pointer(ctrl)
    fcntl.ioctl(encoder.vd, v4l2.VIDIOC_S_EXT_CTRLS, ctrls)


def main() -> None:
    try:
        picam2 = Picamera2()
    except Exception as e:
        print(f"Cannot open the camera ({e}).")
        print("Is pi4cam still running?  sudo systemctl stop pi4cam")
        return

    cfg = picam2.create_video_configuration(
        main={"size": (1920, 1080), "format": "YUV420"},
        controls={"FrameRate": 30},
    )
    picam2.configure(cfg)
    out = CountingOutput()
    encoder = H264Encoder(bitrate=PHASES[0][0], iperiod=30, profile="high")
    picam2.start()
    picam2.start_encoder(encoder, out)
    print("Encoder running — measuring output bitrate (wave at the camera!)\n")

    averages = []
    t0 = time.monotonic()
    try:
        for i, (bps, duration) in enumerate(PHASES):
            if i > 0:
                try:
                    set_bitrate_live(encoder, bps)
                    print(f"--- switched control to {bps / 1e6:.0f} Mbps, live ---")
                except OSError as e:
                    print(f"\n!!! VIDIOC_S_EXT_CTRLS refused while streaming: {e}")
                    print("VERDICT: NOT SUPPORTED — driver rejects live changes. Plan B.")
                    return
            samples = []
            for _ in range(duration):
                before = out.bytes
                time.sleep(1)
                mbps = (out.bytes - before) * 8 / 1e6
                samples.append(mbps)
                print(f"[t={time.monotonic() - t0:5.1f}s] target {bps / 1e6:.0f} Mbps"
                      f" → measured {mbps:5.2f} Mbps")
            # skip the first 4 s of each phase: rate control needs to converge
            averages.append(sum(samples[4:]) / len(samples[4:]))
    finally:
        picam2.stop_encoder()
        picam2.stop()

    print(f"\nAverages (last 6 s of each phase): "
          f"{averages[0]:.2f} / {averages[1]:.2f} / {averages[2]:.2f} Mbps"
          f"  (targets 8 / 2 / 8)")
    went_down = averages[1] < averages[0] * 0.6
    came_back = averages[2] > averages[1] * 1.5
    if went_down and came_back:
        print("VERDICT: SUPPORTED — the encoder follows live bitrate changes."
              " Phase 1 of #47 is a go.")
    else:
        print("VERDICT: NOT SUPPORTED — control accepted but the output does"
              " not follow. Plan B (static bitrate) applies.")


if __name__ == "__main__":
    main()
