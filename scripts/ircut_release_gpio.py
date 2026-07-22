#!/usr/bin/env python3
"""
Standalone IR-CUT day/night controller for camera modules whose filter is
driven by a GPIO (field-tested: Waveshare IMX219-160 IR-CUT).

IMPORTANT correction to an earlier assumption: on the tested module the
IR-CUT filter is NOT autonomous. Its onboard photoresistor drives the IR
LEDs, but the FILTER itself only follows the control pin — it never switches
on its own. So the Pi must decide day/night and drive the pin.

Field-tested polarity (confirm on yours — the vendor wiki had it backwards):
    GPIO output LOW  (op dl)  → DAY   (filter engaged, true colours)
    GPIO input, no pull (ip pn) → NIGHT (filter retracted; the module's own
                                 photoresistor lights the IR LEDs separately)

Day/night is decided from scene illuminance. This script CANNOT read the
camera's AEC Lux itself (pi4cam holds the sensor — it can't be opened twice),
so pi4cam publishes its Lux estimate as telemetry to /dev/shm/pi4cam-lux and
--watch reads that. True AEC Lux (not raw image brightness) is what makes the
decision robust: at night the sensor runs shutter+gain wide open, so an
IR-lit scene that LOOKS bright still reports a low Lux — no oscillation once
the LEDs come on.

This stays fully DECOUPLED from the main program: pi4cam only writes a number,
all thresholds / hysteresis / GPIO logic live here. Nothing imports this file;
users without the module never touch it, and pi4cam runs identically whether
or not this daemon exists.

Usage:
    # Daemon — follow the published Lux, drive the filter (production):
    python3 scripts/ircut_release_gpio.py --gpio 17 --watch

    # Manual overrides / diagnostics:
    python3 scripts/ircut_release_gpio.py --gpio 17 --day     # force filter in
    python3 scripts/ircut_release_gpio.py --gpio 17 --night   # force filter out
    python3 scripts/ircut_release_gpio.py --gpio 17 --status  # print pin state

Run --watch as a long-running systemd service — see
scripts/pi4cam-ircut-release.service (copy and enable manually; install.sh
does not deploy scripts/).

Thresholds (--night-below / --day-above, in Lux) are STARTING POINTS to
field-calibrate: watch `journalctl -u pi4cam-ircut-release -f` — the daemon
logs the live Lux and state — and set them from real dusk/night readings on
your rig. Dusk is the tricky moment (retracting the filter too early tints
the still-lit scene pink).
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

# pinctrl (Bookworm+) is the successor to raspi-gpio (Bullseye) — same
# `set <pin> <mode> [pull]` / `get <pin>` syntax, so one code path covers
# both without a Python GPIO library dependency (project convention:
# system tools over new packages — see rtsp_publisher.py's ffmpeg calls).
_TOOL = shutil.which("pinctrl") or shutil.which("raspi-gpio")

DEFAULT_LUX_PATH = "/dev/shm/pi4cam-lux"


def _run(*args: str) -> str:
    if _TOOL is None:
        sys.exit(
            "Neither pinctrl nor raspi-gpio found. Install raspi-utils "
            "(apt) or run on a Raspberry Pi OS image that ships one."
        )
    result = subprocess.run(
        [_TOOL, *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def set_night(gpio: int) -> None:
    """Input, pull none → filter retracted (night). Matches unplugging the
    pin: the module's pull-up floats the line high = night."""
    _run("set", str(gpio), "ip", "pn")


def set_day(gpio: int) -> None:
    """Output low → filter engaged (day, true colours)."""
    _run("set", str(gpio), "op", "dl")


def status(gpio: int) -> str:
    return _run("get", str(gpio))


# ---------------------------------------------------------------------------
# Day/night decision — pure state machine, unit-tested without hardware.
# ---------------------------------------------------------------------------

class DayNightHysteresis:
    """Two-threshold hysteresis with a debounce streak.

    Below `night_below` Lux votes night, above `day_above` votes day, the band
    between keeps the current state (no flip). A vote only flips the state
    after `samples` consecutive confirmations — so a passing cloud, headlight
    or camera AE wobble never toggles the filter.
    """

    def __init__(self, night_below: float, day_above: float,
                 samples: int, state: str = "day"):
        if not night_below < day_above:
            raise ValueError("night_below must be < day_above")
        self.night_below = night_below
        self.day_above = day_above
        self.samples = max(1, samples)
        self.state = state
        self._pending: str | None = None
        self._streak = 0

    def update(self, lux: float) -> str | None:
        """Feed one Lux reading. Returns the new state if it just flipped,
        else None."""
        if lux < self.night_below:
            vote = "night"
        elif lux > self.day_above:
            vote = "day"
        else:
            vote = self.state  # inside the hysteresis band → hold
        if vote == self.state:
            self._pending, self._streak = None, 0
            return None
        if vote == self._pending:
            self._streak += 1
        else:
            self._pending, self._streak = vote, 1
        if self._streak >= self.samples:
            self.state = vote
            self._pending, self._streak = None, 0
            return self.state
        return None


def read_lux(path: str, stale_after: float) -> float | None:
    """Latest published Lux, or None if the file is missing, stale (pi4cam
    stopped writing), or unparseable — the caller then holds state."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if time.time() - st.st_mtime > stale_after:
        return None
    try:
        with open(path) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return None


def _log(msg: str) -> None:
    # systemd stamps the time; flush so journald sees it immediately.
    print(f"[ircut] {msg}", flush=True)


def watch(gpio: int, lux_path: str, night_below: float, day_above: float,
          samples: int, interval: float) -> None:
    # Safe startup default: DAY (filter in). A wrong night in daylight shows a
    # visible pink cast; a wrong day at dusk is merely slightly dark with
    # correct colours — so bias to day until proven dark.
    hyst = DayNightHysteresis(night_below, day_above, samples, state="day")
    set_day(gpio)
    _log(f"watch start: gpio={gpio} night<{night_below} day>{day_above} lux, "
         f"{samples} samples @ {interval:.0f}s — filter engaged (day) until proven dark")

    stale_after = max(interval * 3, 30.0)
    last_stats = 0.0
    last_stale_warn = 0.0
    while True:
        time.sleep(interval)
        try:
            lux = read_lux(lux_path, stale_after)
            now = time.monotonic()
            if lux is None:
                if now - last_stale_warn >= 300.0:
                    last_stale_warn = now
                    _log(f"no fresh Lux at {lux_path} (pi4cam stopped?) — "
                         f"holding {hyst.state}")
                continue
            flipped = hyst.update(lux)
            if flipped == "night":
                set_night(gpio)
                _log(f"→ NIGHT (filter retracted) lux={lux:.1f}")
            elif flipped == "day":
                set_day(gpio)
                _log(f"→ DAY (filter engaged) lux={lux:.1f}")
            # Calibration heartbeat (~10 min), like the IR-stats line.
            if now - last_stats >= 600.0:
                last_stats = now
                _log(f"lux={lux:.1f} state={hyst.state} "
                     f"(night<{night_below} day>{day_above})")
        except Exception as e:  # a daemon must never die on a transient error
            _log(f"iteration error (continuing): {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument(
        "--gpio", type=int, required=True,
        help="BCM GPIO number wired to the module's IR-CUT control pin "
             "(check a pinout diagram for the physical-pin mapping — not a "
             "fixed offset). Field-tested example: BCM17, physical pin 11.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--watch", action="store_true",
                      help="Daemon: follow the published Lux and drive the filter.")
    mode.add_argument("--day", "--force-day", action="store_true", dest="day",
                      help="Force day mode (filter engaged / output low).")
    mode.add_argument("--night", action="store_true",
                      help="Force night mode (filter retracted / input, no pull).")
    mode.add_argument("--status", action="store_true",
                      help="Print the pin's current state, change nothing.")

    grp = parser.add_argument_group("watch tuning (Lux — field-calibrate)")
    grp.add_argument("--lux-path", default=DEFAULT_LUX_PATH,
                     help=f"Telemetry file pi4cam writes (default {DEFAULT_LUX_PATH}).")
    grp.add_argument("--night-below", type=float, default=8.0,
                     help="Enter night below this Lux (default 8 — a starting point).")
    grp.add_argument("--day-above", type=float, default=25.0,
                     help="Enter day above this Lux (default 25 — a starting point).")
    grp.add_argument("--samples", type=int, default=4,
                     help="Consecutive confirming readings before flipping (default 4).")
    grp.add_argument("--interval", type=float, default=15.0,
                     help="Seconds between Lux polls (default 15).")
    args = parser.parse_args()

    if args.status:
        print(status(args.gpio))
    elif args.day:
        set_day(args.gpio)
        print(f"GPIO{args.gpio}: forced DAY (filter engaged, output low)")
    elif args.night:
        set_night(args.gpio)
        print(f"GPIO{args.gpio}: forced NIGHT (filter retracted, input/no-pull)")
    elif args.watch:
        if not args.night_below < args.day_above:
            sys.exit("--night-below must be < --day-above")
        watch(args.gpio, args.lux_path, args.night_below, args.day_above,
              args.samples, args.interval)


if __name__ == "__main__":
    main()
