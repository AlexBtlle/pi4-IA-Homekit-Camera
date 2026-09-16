#!/usr/bin/env python3
"""
Regression check to run after an iOS / tvOS / iPadOS update.

WHY THIS EXISTS
---------------
Apple's new HKSV protocol (shipped with the 27 generation) negotiates live
video completely differently: multi-tier RTP, Setup Endpoints answered on the
write-response channel, controller-assigned SSRCs. Field probing (#59) showed
the controller picks the protocol from what the ACCESSORY ADVERTISES, and this
camera advertises none of the new services — so it keeps being driven with the
legacy R17 path. That is an observation, not a guarantee: it has to be
re-checked every time Apple ships a major release.

The important failure mode is NOT a crash. It is an ABSENCE: a controller that
switched protocols never writes Setup Endpoints, so no session is ever
negotiated and the Home app just says the camera is not responding, while every
service on the Pi looks perfectly healthy. A dashboard cannot see that, which
is exactly why this script watches the journal instead.

WHAT IT CAN AND CANNOT DO
-------------------------
Phase 1 runs alone and checks everything observable from the Pi. Phase 2 needs
you, an iPhone and roughly three minutes: the script walks you through the
manual steps and asserts on what the journal actually shows, so "it looked
fine" becomes "the session negotiated 1920x1080@30 at 4000 kbps".

Run from the git clone (install.sh does not deploy scripts/), on the Pi:

    python3 scripts/ios_release_check.py              # full run, guided
    python3 scripts/ios_release_check.py --quick      # phase 1 only, unattended
    python3 scripts/ios_release_check.py --report out.json

Keep the JSON reports: comparing two releases side by side is the whole point.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

CONFIG_PATHS = ("/opt/pi4cam/config.yaml", "config.yaml")
PERSIST_GLOB = "/opt/pi4cam/homekit/persist/AccessoryInfo.*.json"
SERVICES = ("pi4cam", "pi4cam-homekit", "mediamtx")

# Log patterns. These are asserted against the real format strings in
# homekit/src/ — if a log line is reworded, this script must follow, otherwise
# it reports green on a broken camera. That is the one maintenance burden here.
RE_NEGOTIATED = re.compile(
    r"\[stream ([0-9a-f]{8})\] negotiated (\d+)x(\d+)@(\d+) max (\d+) kbps"
)
RE_SPAWNED = re.compile(r"\[stream ([0-9a-f]{8})\] ffmpeg spawned \+(\d+) ms")
RE_EXITED = re.compile(r"\[stream ([0-9a-f]{8})\] ffmpeg exited (\S+)")
RE_SNAP_FAIL = re.compile(r"\[stream\] snapshot failed")
RE_MOTION = re.compile(r"\[motion\] motion detected")
RE_HKSV_START = re.compile(r"\[hksv\] stream (\d+) started")
RE_PREROLL = re.compile(
    r"\[prebuffer\] session start: serving (\d+) pre-roll fragment\(s\)"
)
RE_HKSV_END = re.compile(r"\[hksv\] stream (\d+) (?:ended|closed)")

GREEN, RED, YELLOW, GREY, BOLD, OFF = (
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[90m",
    "\033[1m",
    "\033[0m",
)
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = GREY = BOLD = OFF = ""


class Report:
    """Collects one result per check, and decides the exit code."""

    def __init__(self) -> None:
        self.checks: list[dict] = []

    def add(self, name: str, status: str, detail: str = "", **extra) -> None:
        # Flatten: systemctl and urllib happily return multi-line errors, which
        # would wreck one-line-per-check readability.
        detail = " ".join(detail.split())
        if len(detail) > 150:
            detail = detail[:147] + "…"
        self.checks.append({"name": name, "status": status, "detail": detail, **extra})
        mark = {
            "pass": f"{GREEN}  ok  {OFF}",
            "fail": f"{RED} FAIL {OFF}",
            "warn": f"{YELLOW} warn {OFF}",
            "skip": f"{GREY} skip {OFF}",
        }[status]
        print(f"[{mark}] {name}" + (f" {GREY}— {detail}{OFF}" if detail else ""))

    @property
    def failed(self) -> list[dict]:
        return [c for c in self.checks if c["status"] == "fail"]

    @property
    def warned(self) -> list[dict]:
        return [c for c in self.checks if c["status"] == "warn"]


def section(title: str) -> None:
    print(f"\n{BOLD}{title}{OFF}")


def run(cmd: list[str], timeout: float = 10.0) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


def load_config() -> dict:
    """Read the DEPLOYED config — the services run from /opt/pi4cam, not the clone."""
    try:
        import yaml
    except ImportError:
        print(f"{RED}PyYAML missing (apt install python3-yaml){OFF}", file=sys.stderr)
        sys.exit(2)
    for path in CONFIG_PATHS:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            cfg["_path"] = path
            return cfg
    print(f"{RED}no config.yaml found in {CONFIG_PATHS}{OFF}", file=sys.stderr)
    sys.exit(2)


# --------------------------------------------------------------------------
# Phase 1 — what the Pi can establish on its own
# --------------------------------------------------------------------------


def check_services(rep: Report) -> None:
    for unit in SERVICES:
        rc, active = run(["systemctl", "is-active", unit])
        if active != "active":
            rep.add(f"service {unit}", "fail", active or "inactive")
            continue
        _, restarts = run(["systemctl", "show", "-p", "NRestarts", "--value", unit])
        n = restarts.strip() or "0"
        # A restart count above zero is not a failure by itself — it is the
        # trace of one. Worth surfacing, not worth failing on.
        rep.add(
            f"service {unit}",
            "warn" if n not in ("0", "") else "pass",
            f"active, NRestarts={n}" if n not in ("0", "") else "active",
            restarts=n,
        )


def check_rtsp(rep: Report, cfg: dict) -> None:
    port = int((cfg.get("rtsp") or {}).get("port", 8554))
    url = f"rtsp://127.0.0.1:{port}/camera"
    if not shutil.which("ffprobe"):
        rep.add("rtsp stream", "skip", "ffprobe not installed")
        return
    rc, out = run(
        [
            "ffprobe", "-v", "error", "-rtsp_transport", "tcp",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height",
            "-of", "json", url,
        ],
        timeout=20.0,
    )
    if rc != 0:
        rep.add("rtsp stream", "fail", f"{url} unreadable: {out[:120]}")
        return
    try:
        s = (json.loads(out).get("streams") or [{}])[0]
        desc = f"{s.get('codec_name')} {s.get('width')}x{s.get('height')}"
    except Exception:
        rep.add("rtsp stream", "warn", "readable but ffprobe output unparsed")
        return
    rep.add("rtsp stream", "pass", desc, stream=desc)


def check_snapshot(rep: Report, cfg: dict) -> None:
    cam = cfg.get("camera") or {}
    path = cam.get("snapshot_path", "/dev/shm/pi4cam-snapshot.jpg")
    interval = float(cam.get("snapshot_interval", 2))
    try:
        st = os.stat(path)
    except OSError as e:
        rep.add("snapshot file", "fail", f"{path}: {e.strerror}")
        return
    age = time.time() - st.st_mtime
    # Three intervals of slack: the writer skips a frame when the encode queue
    # is full, which is normal under load.
    budget = max(3 * interval, 10.0)
    with open(path, "rb") as f:
        magic = f.read(2)
    if magic != b"\xff\xd8":
        rep.add("snapshot file", "fail", "not a JPEG")
        return
    rep.add(
        "snapshot file",
        "pass" if age <= budget else "fail",
        f"{age:.1f}s old, {st.st_size // 1024} KiB (budget {budget:.0f}s)",
        age_s=round(age, 1),
    )


def listening_sockets() -> dict[int, set[str]]:
    rc, out = run(["ss", "-tlnH"])
    listeners: dict[int, set[str]] = {}
    if rc != 0:
        return listeners
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]
        addr, _, port = local.rpartition(":")
        if port.isdigit():
            listeners.setdefault(int(port), set()).add(addr.strip("[]"))
    return listeners


def check_binds(rep: Report, cfg: dict) -> None:
    """The loopback-only endpoints must stay loopback-only.

    This also catches a real field problem: install.sh never overwrites an
    existing /opt/pi4cam/mediamtx.yml, so an install created before v1.2 still
    binds RTSP to every interface — the full video stream, on the LAN, with no
    update ever fixing it.
    """
    listeners = listening_sockets()
    if not listeners:
        rep.add("listening sockets", "skip", "ss unavailable")
        return
    cam = cfg.get("camera") or {}
    hk = cfg.get("homekit") or {}
    expect_local = {
        int((cfg.get("rtsp") or {}).get("port", 8554)): "rtsp (mediamtx)",
        int(hk.get("motion_port", 8989)): "motion webhook",
        int(cam.get("control_port", 8990)): "camera control",
    }
    for port, label in expect_local.items():
        addrs = listeners.get(port)
        if not addrs:
            rep.add(f"bind :{port}", "fail", f"{label} not listening")
            continue
        exposed = {a for a in addrs if a not in ("127.0.0.1", "::1")}
        if exposed:
            rep.add(
                f"bind :{port}",
                "fail",
                f"{label} EXPOSED on {', '.join(sorted(exposed))} — expected loopback",
                addrs=sorted(addrs),
            )
        else:
            rep.add(f"bind :{port}", "pass", f"{label} loopback only")
    # The status page is deliberately reachable from the LAN (you open it from
    # your phone) and serves the pairing PIN. Assumed choice, stated here so a
    # future reader does not mistake it for an oversight.
    qr = int(hk.get("qr_web_port", 8080))
    rep.add(
        f"bind :{qr}",
        "pass" if listeners.get(qr) else "fail",
        "status page on all interfaces (by design, serves the pairing PIN)"
        if listeners.get(qr)
        else "status page not listening",
    )


def http_probe(port: int, path: str = "/", timeout: float = 5.0) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{path}", timeout=timeout
        ) as r:
            return r.status == 200, f"HTTP {r.status}"
    except Exception as e:  # noqa: BLE001 — any failure is a failed probe
        return False, str(e)[:80]


def check_endpoints(rep: Report, cfg: dict) -> None:
    hk = cfg.get("homekit") or {}
    ok, detail = http_probe(int(hk.get("qr_web_port", 8080)))
    rep.add("status page", "pass" if ok else "fail", detail)
    ok, detail = http_probe(int(hk.get("motion_port", 8989)), "/health")
    rep.add("motion endpoint", "pass" if ok else "fail", detail)


def check_mdns(rep: Report) -> None:
    """Is the accessory actually advertised?

    Note: the advertiser is @homebridge/ciao, inside the Node process — NOT
    avahi-daemon. avahi-browse is used here only as a client to look at the
    network, so a missing avahi-utils is a skip, never a failure.
    """
    if not shutil.which("avahi-browse"):
        rep.add("mDNS advertisement", "skip", "avahi-browse not installed")
        return
    rc, out = run(["avahi-browse", "-rpt", "_hap._tcp"], timeout=15.0)
    if rc != 0:
        rep.add("mDNS advertisement", "skip", "avahi-browse failed")
        return
    host = socket.gethostname().split(".")[0].lower()
    hits = [l for l in out.splitlines() if l.startswith("=") and host in l.lower()]
    rep.add(
        "mDNS advertisement",
        "pass" if hits else "fail",
        f"{len(hits)} _hap._tcp record(s) for {host}"
        if hits
        else f"no _hap._tcp record for {host} — controllers cannot find it",
    )


def check_pairings(rep: Report) -> None:
    """Report how many controllers are paired, and how many are admins.

    Deliberately prints NOTHING else: the same file holds the pincode and the
    accessory's Ed25519 private key.
    """
    files = glob.glob(PERSIST_GLOB)
    if not files:
        rep.add("pairings", "skip", "persist store unreadable (run with sudo?)")
        return
    try:
        with open(files[0], encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        rep.add("pairings", "skip", f"unreadable: {e}")
        return
    clients = info.get("pairedClients") or {}
    perms = info.get("pairedClientsPermission") or {}
    admins = sum(1 for v in perms.values() if v == 1)
    if not clients:
        rep.add("pairings", "warn", "accessory is NOT paired — nothing will work")
        return
    rep.add(
        "pairings",
        "pass",
        f"{len(clients)} controller(s), {admins} admin(s)",
        pairings=len(clients),
        admins=admins,
    )


# --------------------------------------------------------------------------
# Phase 2 — guided, needs a phone
# --------------------------------------------------------------------------


def journal_since(epoch: float) -> str:
    rc, out = run(
        [
            "journalctl", "-u", "pi4cam-homekit", "-u", "pi4cam",
            "--since", f"@{int(epoch)}", "--no-pager", "-o", "cat",
        ],
        timeout=20.0,
    )
    return out if rc == 0 else ""


def wait_for(epoch: float, pattern: re.Pattern, seconds: int) -> re.Match | None:
    """Poll the journal until `pattern` shows up, or the budget runs out."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        m = pattern.search(journal_since(epoch))
        if m:
            return m
        left = int(deadline - time.time())
        print(f"\r  {GREY}waiting… {left}s{OFF}   ", end="", flush=True)
        time.sleep(2)
    print("\r" + " " * 40 + "\r", end="")
    return None


def ask(prompt: str) -> None:
    input(f"\n{BOLD}→ {prompt}{OFF}\n  Press Enter when done (or Ctrl-C to stop) ")


def confirm(question: str) -> bool:
    return input(f"  {question} [y/N] ").strip().lower().startswith("y")


def guided_live(rep: Report) -> None:
    t0 = time.time()
    ask("Open the camera's LIVE view FULL SCREEN, with the iPhone on Wi-Fi.")
    m = wait_for(t0, RE_NEGOTIATED, 45)
    if not m:
        # The signature that matters. A protocol switch produces silence, not
        # an error: no Setup Endpoints write means no session is ever set up.
        rep.add(
            "live: session negotiated",
            "fail",
            "NO negotiated line — if the Home app said 'not responding', this is "
            "the fingerprint of a controller that changed protocol",
        )
        return
    sid, w, h, fps, kbps = m.groups()
    rep.add(
        "live: session negotiated",
        "pass",
        f"{w}x{h}@{fps}, max {kbps} kbps (session {sid})",
        width=int(w), height=int(h), fps=int(fps), kbps=int(kbps),
    )
    spawned = wait_for(t0, RE_SPAWNED, 20)
    rep.add(
        "live: ffmpeg spawned",
        "pass" if spawned else "fail",
        f"+{spawned.group(2)} ms" if spawned else "ffmpeg never started",
    )
    rep.add(
        "live: picture on screen",
        "pass" if confirm("Did you actually SEE a moving picture?") else "fail",
        "human-confirmed",
    )
    # Passthrough always emits 1920x1080: a lower negotiated size is honoured
    # by nobody. Informational, and a known documented limitation.
    if (int(w), int(h)) != (1920, 1080):
        rep.add(
            "live: negotiated vs produced",
            "warn",
            f"controller asked {w}x{h}, passthrough always sends 1920x1080",
        )
    t1 = time.time()
    ask("Now CLOSE the live view.")
    ex = wait_for(t1, RE_EXITED, 30)
    rep.add(
        "live: session torn down",
        "pass" if ex else "warn",
        f"ffmpeg exited {ex.group(2)}" if ex else "no exit line seen",
    )


def guided_snapshot(rep: Report) -> None:
    t0 = time.time()
    ask("Go back to the Home app grid so the camera THUMBNAIL refreshes.")
    if RE_SNAP_FAIL.search(journal_since(t0)):
        rep.add("snapshot: served to HomeKit", "fail", "'snapshot failed' in the journal")
        return
    # Successful snapshots are not logged, so the journal can only disprove
    # this one. Your eyes are the real check — say so rather than claim a pass.
    rep.add(
        "snapshot: served to HomeKit",
        "pass" if confirm("Is the thumbnail a recent image (not a grey tile)?") else "fail",
        "human-confirmed (successful snapshots are not logged)",
    )


def guided_motion_hksv(rep: Report) -> None:
    t0 = time.time()
    ask("Walk in FRONT of the camera, then step away.")
    m = wait_for(t0, RE_MOTION, 60)
    rep.add(
        "motion: sensor triggered",
        "pass" if m else "fail",
        "journal shows the trigger" if m else "no motion line — check detection settings",
    )
    if m:
        rep.add(
            "motion: notification received",
            "pass" if confirm("Did your iPhone get a notification?") else "warn",
            "human-confirmed (depends on your Home app settings)",
        )
    started = wait_for(t0, RE_HKSV_START, 45)
    if not started:
        rep.add(
            "hksv: recording stream opened",
            "warn",
            "no stream — is Recording enabled for this camera, and a hub present?",
        )
        return
    rep.add("hksv: recording stream opened", "pass", f"stream {started.group(1)}")
    pre = RE_PREROLL.search(journal_since(t0))
    rep.add(
        "hksv: pre-roll served",
        "pass" if pre and int(pre.group(1)) > 0 else "warn",
        f"{pre.group(1)} fragment(s)" if pre else "no pre-roll line",
        preroll=int(pre.group(1)) if pre else None,
    )
    ended = wait_for(t0, RE_HKSV_END, 90)
    rep.add(
        "hksv: stream closed cleanly",
        "pass" if ended else "warn",
        "hub closed the stream" if ended else "stream never closed within 90s",
    )
    rep.add(
        "hksv: clip playable",
        "pass" if confirm("Can you PLAY the new clip in the Home app timeline?") else "fail",
        "human-confirmed",
    )


# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Post-iOS-update regression check for pi4cam."
    )
    ap.add_argument(
        "--quick", action="store_true",
        help="phase 1 only — no phone needed, safe to run unattended",
    )
    ap.add_argument("--report", metavar="FILE", help="write the JSON report here")
    ap.add_argument(
        "--os-version", metavar="STR",
        help="the controller OS you are testing (e.g. 'iOS 27.0'); prompted otherwise",
    )
    args = ap.parse_args()

    cfg = load_config()
    rep = Report()

    print(f"{BOLD}pi4cam — post-update regression check{OFF}")
    print(f"{GREY}config: {cfg['_path']}  |  host: {socket.gethostname()}{OFF}")

    section("Phase 1 — checked from the Pi alone")
    check_services(rep)
    check_binds(rep, cfg)
    check_rtsp(rep, cfg)
    check_snapshot(rep, cfg)
    check_endpoints(rep, cfg)
    check_mdns(rep)
    check_pairings(rep)

    os_version = args.os_version
    if not args.quick:
        if not os_version:
            os_version = input(
                "\nController OS under test (e.g. 'iOS 27.0'): "
            ).strip() or "unspecified"
        section("Phase 2 — guided, needs your iPhone (~3 min)")
        print(
            f"{GREY}The script reads the journal after each step, so a step that "
            f"'looked fine' still has to prove it.{OFF}"
        )
        try:
            guided_live(rep)
            guided_snapshot(rep)
            guided_motion_hksv(rep)
        except KeyboardInterrupt:
            print(f"\n{YELLOW}interrupted — partial report{OFF}")

    section("Result")
    if rep.failed:
        print(f"{RED}{len(rep.failed)} check(s) FAILED{OFF}")
        for c in rep.failed:
            print(f"  {RED}·{OFF} {c['name']}: {c['detail']}")
    elif rep.warned:
        print(f"{YELLOW}no failure, {len(rep.warned)} warning(s){OFF}")
    else:
        print(f"{GREEN}everything checked is working{OFF}")

    if not args.quick and not rep.failed:
        print(
            f"\n{GREY}The legacy HAP camera path still works on "
            f"{os_version}. Worth recording in the README.{OFF}"
        )

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": socket.gethostname(),
        "controller_os": os_version or "not tested",
        "mode": "quick" if args.quick else "full",
        "checks": rep.checks,
    }
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"{GREY}report written to {args.report}{OFF}")

    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
