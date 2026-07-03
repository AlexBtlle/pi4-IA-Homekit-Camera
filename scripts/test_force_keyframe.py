#!/usr/bin/env python3
"""Phase-0 probe for #43 — can the encoder emit a keyframe on demand?

The GOP is set to 10 s (iperiod=300); we poke V4L2_CID_MPEG_VIDEO_FORCE_KEY_FRAME
at t≈3 s and watch whether an IDR arrives within the second — the scheduled one
would only come at t≈10 s, so an early keyframe proves the control works.

Usage (on the Pi, camera free):
    sudo systemctl stop pi4cam
    python3 scripts/test_force_keyframe.py
    sudo systemctl start pi4cam
"""
import ctypes
import fcntl
import time

from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import Output

POKE_AT = 3.0
WATCH_UNTIL = 6.0
# Fallback CID if the distro's V4L2 binding doesn't name it:
# V4L2_CTRL_CLASS_MPEG (0x00990000) | 0x900 base + 229.
FORCE_KEY_FRAME_CID = 0x009909E5


class KeyframeLog(Output):
    """Discards frames, recording the arrival time of each keyframe."""

    def __init__(self):
        super().__init__()
        self.t0 = time.monotonic()
        self.keyframes = []

    def outputframe(self, frame, keyframe=True, *args, **kwargs):
        if keyframe:
            self.keyframes.append(time.monotonic() - self.t0)


def force_keyframe(encoder) -> None:
    from picamera2.encoders import v4l2_encoder as v4l2

    cid = getattr(v4l2, "V4L2_CID_MPEG_VIDEO_FORCE_KEY_FRAME", FORCE_KEY_FRAME_CID)
    ctrl = v4l2.v4l2_ext_control()
    ctrl.id = cid
    ctrl.value = 1
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
    out = KeyframeLog()
    encoder = H264Encoder(bitrate=4_000_000, iperiod=300, profile="high")  # GOP 10 s
    picam2.start()
    out.t0 = time.monotonic()
    picam2.start_encoder(encoder, out)
    print(f"Encoder running, GOP=10 s. Forcing a keyframe at t={POKE_AT:.0f}s…")

    poked_at = None
    try:
        time.sleep(POKE_AT)
        try:
            poked_at = time.monotonic() - out.t0
            force_keyframe(encoder)
            print(f"[t={poked_at:.2f}s] force-keyframe control sent")
        except OSError as e:
            print(f"\n!!! VIDIOC_S_EXT_CTRLS refused: {e}")
            print("VERDICT: NOT SUPPORTED — driver rejects the control.")
            return
        time.sleep(WATCH_UNTIL - POKE_AT)
    finally:
        picam2.stop_encoder()
        picam2.stop()

    print(f"\nKeyframes seen at: {[f'{t:.2f}s' for t in out.keyframes]}")
    forced = [t for t in out.keyframes if poked_at <= t <= poked_at + 1.0]
    if forced:
        print(f"VERDICT: SUPPORTED — IDR delivered {forced[0] - poked_at:.3f}s "
              "after the request. The Eve trick works; wire it up (#43).")
    else:
        print("VERDICT: NOT SUPPORTED — control accepted but no early IDR "
              "(next keyframe stayed on the 10 s schedule).")


if __name__ == "__main__":
    main()
