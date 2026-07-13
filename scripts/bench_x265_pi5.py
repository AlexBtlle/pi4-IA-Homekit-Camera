#!/usr/bin/env python3
"""
Pi 5 x265 3-tier encoding benchmark (#59, Volet 2 pre-work).

Measures whether a Pi 5 can sustain the new HKSV spec's "2K Camera" encoding
ladder in software — three simultaneous libx265 encodes from one sensor
capture — under the REAL capture pipeline (picamera2 video configuration,
same as the live service; rpicam-vid standalone is known-unfaithful):

    High    2560x1440 (or --size WxH) @ 30 fps   2800k avg / 3000k max
    Medium  1920x1080 @ 30 fps                   1700k avg / 1800k max
    Low     640x360   @ 15 fps                    180k avg /  190k max

One raw-YUV420 pipe feeds a single ffmpeg using -filter_complex split=3;
outputs go to the null muxer (we measure encode capacity — RTSP/CMAF muxing
overhead is negligible by comparison). Because ffmpeg never drops frames on
its own (it backpressures the pipe instead), the achieved END-TO-END fps is
the binding metric: if the encoders can't keep up, the writer stalls, the
libcamera queue overflows and the measured fps sags below nominal.

Metrics logged every 5 s and summarised at the end (markdown, ready to paste
into the GitHub issue): achieved fps (average + worst 30 s window), CPU per
core and total (/proc/stat), SoC temperature, throttling flags (vcgencmd),
x265 encoder summaries.

--with-detection additionally runs the production motion-detection load
(MOG2 → morphology open 3x3 ellipse → findContours → contourArea on the
lores Y plane at --analysis-fps) so the numbers reflect the real combined
deployment condition, not the encode alone.

Module 3 (IMX708) note: full-FOV binned mode is 2304x1296@56fps — a strict
2560x1440 output is therefore an ISP *upscale*. The spec allows approximate
resolutions, so also benchmark the native binned size:

    python3 bench_x265_pi5.py --duration 1800 --with-detection
    python3 bench_x265_pi5.py --duration 1800 --with-detection --size 2304x1296

Requires: python3-picamera2 (apt), system ffmpeg with libx265 (checked at
startup), python3-opencv for --with-detection.
"""
import argparse
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

REPORT_DEFAULT = "bench_x265_report.md"

TIERS = [
    # (name, width, height, fps, avg_kbps, max_kbps) — High geometry comes
    # from --size; Medium/Low are fixed by the spec's 2K-camera ladder.
    ("medium", 1920, 1080, 30, 1700, 1800),
    ("low", 640, 360, 15, 180, 190),
]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--size", default="2560x1440",
                   help="High-tier capture/encode size WxH (default 2560x1440; "
                        "try 2304x1296 = IMX708 native binned)")
    p.add_argument("--fps", type=int, default=30, help="capture/High-tier fps")
    p.add_argument("--duration", type=int, default=1800,
                   help="benchmark duration in seconds (default 1800 = 30 min; "
                        "thermal throttling only shows up on long runs)")
    p.add_argument("--preset", default="ultrafast",
                   help="x265 preset for all tiers (default ultrafast)")
    p.add_argument("--with-detection", action="store_true",
                   help="also run the production MOG2 motion-detection load")
    p.add_argument("--no-legacy-h264", action="store_true",
                   help="drop the 4th output leg (x264 1080p). The real "
                        "deployment keeps rtsp://…/camera alive with it, so "
                        "the DEFAULT (4 encodes) is the faithful load")
    p.add_argument("--analysis-fps", type=float, default=10.0,
                   help="detection analysis rate (default 10, like production)")
    p.add_argument("--report", default=REPORT_DEFAULT,
                   help=f"output report path (default {REPORT_DEFAULT})")
    return p.parse_args()


# ----------------------------------------------------------------------
# System sampling
# ----------------------------------------------------------------------

def read_proc_stat():
    """Return {cpu_label: (busy_jiffies, total_jiffies)} from /proc/stat."""
    out = {}
    with open("/proc/stat") as f:
        for line in f:
            if not line.startswith("cpu"):
                break
            parts = line.split()
            label, vals = parts[0], [int(v) for v in parts[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle+iowait
            out[label] = (sum(vals) - idle, sum(vals))
    return out


def cpu_percent(prev, cur):
    """Per-label CPU%% between two read_proc_stat() snapshots."""
    pct = {}
    for label in cur:
        if label not in prev:
            continue
        db = cur[label][0] - prev[label][0]
        dt = cur[label][1] - prev[label][1]
        pct[label] = 100.0 * db / dt if dt > 0 else 0.0
    return pct


def read_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000.0
    except OSError:
        return float("nan")


def read_throttled():
    """vcgencmd get_throttled bitmask (0x0 = clean run), or None if absent."""
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"],
                             capture_output=True, text=True, timeout=5)
        return int(out.stdout.strip().split("=")[1], 16)
    except Exception:
        return None


# ----------------------------------------------------------------------
# ffmpeg
# ----------------------------------------------------------------------

def check_ffmpeg():
    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg not found — install the system package: sudo apt install ffmpeg")
    enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                         capture_output=True, text=True).stdout
    if "libx265" not in enc:
        sys.exit("this ffmpeg has no libx265 encoder — the apt ffmpeg on "
                 "Raspberry Pi OS (bookworm+) ships it; do not use the "
                 "project's static ffmpeg here")
    ver = subprocess.run(["ffmpeg", "-version"],
                         capture_output=True, text=True).stdout.splitlines()[0]
    return ver


def x265_output(name, fps, avg_kbps, max_kbps, preset, pad):
    """One null-muxed x265 output leg. keyint=fps → 1 s GOP like production."""
    return [
        "-map", f"[{pad}]",
        "-c:v", "libx265", "-preset", preset, "-tune", "zerolatency",
        "-b:v", f"{avg_kbps}k", "-maxrate", f"{max_kbps}k",
        "-bufsize", f"{max_kbps}k",
        "-x265-params", f"keyint={fps}:min-keyint={fps}:scenecut=0",
        "-f", "null", "-",
    ]


def build_ffmpeg_cmd(width, height, fps, preset, legacy_h264=True):
    (m_name, m_w, m_h, m_fps, m_avg, m_max) = TIERS[0]
    (l_name, l_w, l_h, l_fps, l_avg, l_max) = TIERS[1]
    n = 4 if legacy_h264 else 3
    pads = "[hi][mid][lo]" + ("[leg]" if legacy_h264 else "")
    fc = (
        f"[0:v]split={n}{pads};"
        f"[mid]scale={m_w}:{m_h}[mid2];"
        f"[lo]scale={l_w}:{l_h},fps={l_fps}[lo2]"
    )
    if legacy_h264:
        fc += ";[leg]scale=1920:1080[leg2]"
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-y",
        "-f", "rawvideo", "-pix_fmt", "yuv420p",
        "-s", f"{width}x{height}", "-r", str(fps), "-i", "pipe:0",
        "-filter_complex", fc,
    ]
    cmd += x265_output("high", fps, 2800, 3000, preset, "hi")
    cmd += x265_output(m_name, m_fps, m_avg, m_max, preset, "mid2")
    cmd += x265_output(l_name, l_fps, l_avg, l_max, preset, "lo2")
    if legacy_h264:
        # The deployment's 4th leg (hevc_publisher.py): the legacy H.264
        # stream that keeps the existing HomeKit path alive on the Pi 5.
        cmd += [
            "-map", "[leg2]", "-c:v", "libx264", "-preset", "superfast",
            "-tune", "zerolatency", "-profile:v", "high", "-b:v", "4000k",
            "-g", str(fps), "-sc_threshold", "0", "-f", "null", "-",
        ]
    return cmd


# ----------------------------------------------------------------------
# Detection load (mirrors camera/presence_detector.py's per-frame ops)
# ----------------------------------------------------------------------

class DetectionLoad(threading.Thread):
    """Production-shaped motion analysis on the lores Y plane."""

    def __init__(self, analysis_fps):
        super().__init__(daemon=True, name="bench-detection")
        import cv2  # noqa — only needed with --with-detection
        self._cv2 = cv2
        self._interval = 1.0 / analysis_fps
        self._mog2 = cv2.createBackgroundSubtractorMOG2(
            history=500, varThreshold=40, detectShadows=False)
        self._kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self._lock = threading.Lock()
        self._frame = None
        self._stop = threading.Event()
        self.analysed = 0

    def submit(self, y_plane):
        with self._lock:
            self._frame = y_plane

    def run(self):
        cv2 = self._cv2
        while not self._stop.is_set():
            t0 = time.monotonic()
            with self._lock:
                frame, self._frame = self._frame, None
            if frame is not None:
                fg = self._mog2.apply(frame)
                fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, self._kernel)
                contours, _ = cv2.findContours(
                    fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                max((cv2.contourArea(c) for c in contours), default=0)
                self.analysed += 1
            time.sleep(max(0.0, self._interval - (time.monotonic() - t0)))

    def stop(self):
        self._stop.set()


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    args = parse_args()
    width, height = (int(v) for v in args.size.lower().split("x"))
    ffmpeg_version = check_ffmpeg()

    from picamera2 import Picamera2  # apt: python3-picamera2

    picam2 = Picamera2()
    # Same construction as the live service: full-FOV binned raw mode, ISP
    # scales to the requested main size; lores plane for the detection load.
    # Largest sensor mode that sustains the target fps — on IMX708 that is
    # the 2304x1296@56 binned mode (full-res 4608x2592 only does 14 fps).
    binned = max((m for m in picam2.sensor_modes if m.get("fps", 0) >= args.fps),
                 key=lambda m: m["size"][0] * m["size"][1], default=None)
    cfg_kwargs = dict(
        main={"size": (width, height), "format": "YUV420"},
        lores={"size": (320, 240), "format": "YUV420"},
        controls={"FrameRate": args.fps},
    )
    if binned is not None:
        cfg_kwargs["raw"] = {"size": binned["size"]}
    picam2.configure(picam2.create_video_configuration(**cfg_kwargs))

    main_cfg = picam2.camera_configuration()["main"]
    stride = main_cfg["stride"]
    sensor_size = picam2.camera_configuration().get("raw", {}).get("size")

    detection = DetectionLoad(args.analysis_fps) if args.with_detection else None

    cmd = build_ffmpeg_cmd(width, height, args.fps, args.preset,
                           legacy_h264=not args.no_legacy_h264)
    ff = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=False)

    # Collect ffmpeg stderr in a side thread (x265 prints its per-encoder
    # summary there at shutdown — that's per-tier ground truth).
    ff_err: list[bytes] = []
    err_thread = threading.Thread(
        target=lambda: ff_err.append(ff.stderr.read()), daemon=True)
    err_thread.start()

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    picam2.start()
    if detection:
        detection.start()

    print(f"# bench: {width}x{height}@{args.fps} High + 1080p30 Medium + "
          f"360p15 Low{'' if args.no_legacy_h264 else ' + x264 1080p legacy'}"
          f" | preset {args.preset} | {args.duration}s"
          f"{' | +detection' if detection else ''}")
    print(f"# sensor mode: {sensor_size} | main stride {stride}")

    frames = 0
    samples = []           # (t, fps_5s, cpu_pct_dict, temp)
    window_counts = []     # frame count per 5 s sample, for worst-30s-window
    t_start = time.monotonic()
    t_sample = t_start
    frames_at_sample = 0
    prev_stat = read_proc_stat()
    temp_start = read_temp()

    try:
        while not stop.is_set() and time.monotonic() - t_start < args.duration:
            req = picam2.capture_request()
            try:
                arr = req.make_array("main")
                if detection and frames % max(1, int(args.fps / args.analysis_fps)) == 0:
                    lores = req.make_array("lores")
                    detection.submit(lores[:240, :320])  # Y plane only
            finally:
                req.release()
            # YUV420: (h*3/2, stride). Drop stride padding if present, then
            # one contiguous write into ffmpeg (blocks if encoders lag — that
            # backpressure is exactly what we want to measure).
            data = arr if stride == width else arr[:, :width].copy()
            try:
                ff.stdin.write(data.tobytes())
            except BrokenPipeError:
                print("!! ffmpeg died — aborting", file=sys.stderr)
                break
            frames += 1

            now = time.monotonic()
            if now - t_sample >= 5.0:
                cur_stat = read_proc_stat()
                pct = cpu_percent(prev_stat, cur_stat)
                prev_stat = cur_stat
                fps5 = (frames - frames_at_sample) / (now - t_sample)
                samples.append((now - t_start, fps5, pct, read_temp()))
                window_counts.append(fps5)
                frames_at_sample, t_sample = frames, now
                print(f"t={now - t_start:6.0f}s  fps={fps5:5.2f}  "
                      f"cpu={pct.get('cpu', 0):5.1f}%  temp={read_temp():.1f}°C")
    finally:
        if detection:
            detection.stop()
        picam2.stop()
        try:
            ff.stdin.close()
        except OSError:
            pass
        try:
            ff.wait(timeout=30)
        except subprocess.TimeoutExpired:
            ff.kill()
        err_thread.join(timeout=5)

    elapsed = time.monotonic() - t_start
    write_report(args, width, height, sensor_size, stride, ffmpeg_version,
                 frames, elapsed, samples, window_counts, temp_start,
                 detection, ff_err)


def write_report(args, width, height, sensor_size, stride, ffmpeg_version,
                 frames, elapsed, samples, window_counts, temp_start,
                 detection, ff_err):
    avg_fps = frames / elapsed if elapsed > 0 else 0.0
    # Worst 30 s window = worst mean over 6 consecutive 5 s samples.
    worst30 = min(
        (sum(window_counts[i:i + 6]) / 6 for i in range(len(window_counts) - 5)),
        default=avg_fps,
    )
    cpu_all = [s[2].get("cpu", 0.0) for s in samples]
    temps = [s[3] for s in samples]
    ncores = max((len([k for k in s[2] if k != "cpu"]) for s in samples),
                 default=0)
    throttled = read_throttled()

    lines = [
        "## Bench x265 3 paliers — Pi 5",
        "",
        f"- Config : High **{width}x{height}@{args.fps}** (2800/3000k) + "
        f"Medium 1920x1080@30 (1700/1800k) + Low 640x360@15 (180/190k)"
        + ("" if args.no_legacy_h264 else " + x264 1080p legacy (4000k)")
        + f", preset `{args.preset}`, tune `zerolatency`, GOP 1 s",
        f"- Mode capteur : {sensor_size}, main stride {stride}"
        + (" (repack par frame)" if stride != width else ""),
        f"- Détection MOG2 simultanée : "
        + (f"oui, {args.analysis_fps:g} fps d'analyse "
           f"({detection.analysed} frames analysées)" if detection else "non"),
        f"- ffmpeg : `{ffmpeg_version}`",
        f"- Durée effective : {elapsed:.0f} s — {frames} frames",
        "",
        f"| Mesure | Valeur |",
        f"|---|---|",
        f"| fps moyen bout-en-bout | **{avg_fps:.2f}** (nominal {args.fps}) |",
        f"| fps pire fenêtre 30 s | **{worst30:.2f}** |",
        f"| CPU total moyen / max | {sum(cpu_all) / len(cpu_all):.1f}% / "
        f"{max(cpu_all):.1f}% (sur {ncores} cœurs = {ncores * 100}%) |"
        if cpu_all else "| CPU | n/a |",
        f"| Température début / max | {temp_start:.1f}°C / "
        f"{max(temps):.1f}°C |" if temps else "| Température | n/a |",
        f"| vcgencmd get_throttled | "
        + (f"`{throttled:#x}`" + (" (throttling détecté !)" if throttled else " (aucun throttling)")
           if throttled is not None else "indisponible") + " |",
        "",
    ]
    err_text = ff_err[0].decode(errors="replace") if ff_err else ""
    x265_lines = [ln for ln in err_text.splitlines()
                  if "x265" in ln and ("encoded" in ln or "kb/s" in ln)]
    if x265_lines:
        lines += ["Résumés x265 par palier :", "```",
                  *x265_lines, "```", ""]

    report = "\n".join(lines)
    with open(args.report, "w") as f:
        f.write(report + "\n")
    print("\n" + report)
    print(f"# rapport écrit dans {args.report}")


if __name__ == "__main__":
    main()
