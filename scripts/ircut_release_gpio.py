#!/usr/bin/env python3
"""
Release a GPIO so a camera module's own IR-CUT auto-switch can drive it.

Some IR-CUT camera modules (field-tested: Waveshare IMX219-160 IR-CUT) do
day/night switching AUTONOMOUSLY via an onboard photoresistor — no picamera2
involvement at all. Their control pin is only an OVERRIDE: driving it LOW
forces day mode (filter engaged) regardless of ambient light; leaving it
floating (input, no pull) hands control back to the module's own sensor.

Field-tested polarity on that module (confirm on yours — the vendor wiki
disagreed with the actual hardware):
    GPIO driven LOW (op dl)   → forced DAY (filter engaged)
    GPIO input, pull NONE     → autonomous (module's photoresistor decides)

The problem this script solves: after every Pi reboot, a generic GPIO can
reset to an internal PULL-DOWN — electrically identical to being wired to
GND — which would pin the module to forced-day permanently, defeating the
whole point of an auto IR-CUT module. Nothing in the camera/HomeKit pipeline
touches this pin, so nothing releases it unless this script runs.

This is a STANDALONE, OPT-IN utility — not imported by camera_manager.py or
any other project code, and it does nothing unless explicitly invoked with a
pin number. Users without this camera module are entirely unaffected; this
file can sit unused in the repo forever.

Usage:
    python3 scripts/ircut_release_gpio.py --gpio 17          # release (default action)
    python3 scripts/ircut_release_gpio.py --gpio 17 --status # just print current state
    python3 scripts/ircut_release_gpio.py --gpio 17 --force-day   # override to day

Run at every boot (state does not persist across reboots) via a oneshot
systemd unit — see scripts/pi4cam-ircut-release.service for a template;
copy and enable it manually, install.sh does not touch it.
"""
import argparse
import shutil
import subprocess
import sys

# pinctrl (Bookworm+) is the successor to raspi-gpio (Bullseye) — same
# `set <pin> <mode> [pull]` / `get <pin>` syntax, so one code path covers
# both without a Python GPIO library dependency (project convention:
# system tools over new packages — see rtsp_publisher.py's ffmpeg calls).
_TOOL = shutil.which("pinctrl") or shutil.which("raspi-gpio")


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


def release(gpio: int) -> None:
    """Input, pull none — the module's own photoresistor decides day/night."""
    _run("set", str(gpio), "ip", "pn")


def force_day(gpio: int) -> None:
    """Output low — forces day mode (filter engaged) regardless of light."""
    _run("set", str(gpio), "op", "dl")


def status(gpio: int) -> str:
    return _run("get", str(gpio))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument(
        "--gpio", type=int, required=True,
        help="BCM GPIO number wired to the module's IR-CUT control pin "
             "(check a pinout diagram for the physical-pin mapping — it is "
             "not a fixed offset). Field-tested example: BCM17, physical "
             "pin 11.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--force-day", action="store_true",
        help="Override to forced day mode instead of releasing (rare — "
             "mainly for testing the override path itself).",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Only print the pin's current state, change nothing.",
    )
    args = parser.parse_args()

    if args.status:
        print(status(args.gpio))
        return

    if args.force_day:
        force_day(args.gpio)
        print(f"GPIO{args.gpio}: forced day mode (output low)")
    else:
        release(args.gpio)
        print(f"GPIO{args.gpio}: released to input/no-pull — "
              f"module's own photoresistor now in control")


if __name__ == "__main__":
    main()
