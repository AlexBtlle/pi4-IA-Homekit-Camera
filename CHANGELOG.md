# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Entries before 1.7 were reconstructed from the git history and release tags,
so they summarise each version rather than reproducing its release notes.

Versions track `homekit/package.json`; each release is tagged in git.

## [Unreleased]

### Fixed

- **Camera stuck "Not Responding" after a reboot** (#65). HAP announces the
  accessory over mDNS on the interfaces present when it publishes; started
  before Wi-Fi held an address, it was never re-announced, so a perfectly
  healthy service stayed invisible to HomeKit until a manual restart — 16 h 40
  in the field incident that surfaced it. The service now waits for a
  non-loopback IPv4 before publishing, and exits non-zero after 120 s so
  systemd retries. systemd ordering could not fix this: the unit already
  ordered itself after `network-online.target`, which NetworkManager reached
  40 s before it associated the Wi-Fi.

### Added

- **Status page reports whether the HomeKit announcement is still valid**
  (#66). Throughout the outage above, the dashboard read "All systems normal" —
  correctly, since every probe it ran was green. A *HomeKit announce* row now
  compares the addresses the accessory was announced on against those the
  machine holds now, and feeds the overall pill, so an unreachable camera can
  no longer render as healthy. It also covers an address changing under a
  running accessory, which the startup guard does not.

## [1.7] — 2026-08-07

### ⚠ Upgrade notes

- **Lux telemetry is now opt-in.** `camera.lux_path` ships **empty
  (disabled)**. If you use the hardware IR-CUT day/night daemon, set it after
  upgrading, otherwise the daemon logs `no fresh Lux` and holds day mode:

  ```yaml
  camera:
    lux_path: /dev/shm/pi4cam-lux
  ```

  then `sudo systemctl restart pi4cam`. Installs without IR-CUT hardware now
  write nothing at all — which is the point of the change.
- `camera.ir_exposure` now ships as `0.0` (its documented "off" value, and the
  code's own default) instead of `1.0`. If you had deliberately set it, your
  value is preserved by the config deep-merge; only fresh installs change.

### Added

- **Hardware IR-CUT day/night control (opt-in).** Camera modules whose IR-cut
  filter is driven by a GPIO — field-tested on a Waveshare IMX219-160 IR-CUT,
  whose filter is *not* autonomous — can now switch day/night automatically:
  `pi4cam` publishes the AEC's Lux estimate as telemetry, and the standalone
  `scripts/ircut_release_gpio.py --watch` daemon drives the pin with
  two-threshold hysteresis and debouncing. Fully decoupled: the main program
  only writes a number, all logic lives in the daemon.
  See [TROUBLESHOOTING](TROUBLESHOOTING.md#hardware-ir-cut-filter--gpio-daynight-opt-in).
- Config guard test: every key shipped in `config.yaml` must be read by some
  source — the class of regression that shipped `lux_path` on one side only.
- Test coverage for the Lux telemetry, `config.ts` loading/fallbacks, the
  status page's HTML escaping, and the live-stream crash-recovery path (#38).
- `npm run typecheck` (and a CI step) covering `homekit/tests/`, which the
  build config never typechecked.

### Fixed

- **Lux telemetry ran unconditionally**, writing to `/dev/shm` every 2 s on
  every install even with no consumer, and its config key was missing from
  `config.yaml` (invisible to users, unreachable by the update deep-merge).
- **`install.sh` deployed `__pycache__`** along with the Python sources, so a
  checkout that once held a since-deleted module could ship stale `.pyc` files
  to `/opt/pi4cam`.
- **HKSV recording leaked an abort listener** on the HAP session signal for
  every recording that ended normally.
- **Status page could crash the process**: its async request handler had no
  error path (an unhandled rejection is fatal on modern Node), and a failed
  page build stayed cached for a full 3 s TTL.
- **Status page ignored a custom `rtsp.port`**, probing 8554 unconditionally
  and reporting a healthy mediamtx as `down`.
- Documentation contradicting the code: Pi 5 support described as "on the
  roadmap" (that effort is closed), prebuffer described as 4 s (6 s of
  retention), "three systemd services" (there are more), and a 45-minute
  local ffmpeg build recommended to arm64 users who already get a prebuilt.
- Code comments describing a day-lift `ExposureValue` mechanism that was
  removed as ineffective, and IR hysteresis timings quoted at the wrong
  analysis rate.

### Changed

- `tests/conftest.py` no longer mocks numpy, so numeric paths are testable;
  numpy joins pytest/pyyaml as a CI dev dependency.
- TROUBLESHOOTING gained a table of contents; the READMEs now point to
  `config.yaml` as the authoritative, always-current config reference.

## [1.6.1] — 2026-07-11

### Fixed

- **HKSV clips played back ~2.5× too fast.** Raw H264 carries no timestamps,
  only the encoder's *nominal* rate; in low light the sensor delivers fewer
  frames than that. Frames are now stamped by wall-clock arrival.
- **HKSV pre-roll was missing.** The prebuffer was tied to the hub's *Active*
  toggle, which the home hub sets lazily at the motion event — so recording
  began exactly at the trigger. It now follows the recording *configuration*.
- Prebuffer: a superseded ffmpeg's close/data handlers could corrupt the live
  instance; `getInit()` hung on an already-aborted signal.
- Documented what actually controls a clip's start and end (the hub trims the
  tail with its own analysis — `cooldown`/`motion_timeout` do not extend it).

## [1.6] — 2026-07-10

### Added

- **Automatic day-lift for dim colour scenes** (#52): dark-but-not-night
  scenes are brightened by an auto-levels LUT (black-point anchored so the
  image doesn't go milky), triggered on the AEC's Lux estimate with
  hysteresis. `day_gamma: 2.5` field-validated for a dim room at dusk.
  (`ExposureValue` was tried first and proved inert.)
- **Configurable day/night bitrate floors** (#53) — the stretched night image
  needs ~3 Mbps to stay clean, where a day frame is fine much lower.
- apt-only Python install (no venv, no pip) and ARMv6 support in `install.sh`.
- Documentation: pre-install camera detection check, unofficial Pi Zero W v1
  recipe.

## [1.5] — 2026-07-06

### Added

- **USB webcam backend (beta)** (#19): `camera.source: usb` drives a UVC
  webcam through ffmpeg — live, snapshots, motion and HKSV all work unchanged.
- **Lean static ffmpeg** (#43) for the live/HKSV spawn paths, downloaded
  prebuilt by `install.sh` (~0.2 s startup vs 5-8 s for Debian's build on a
  memory-pressured Pi). Zero-compile installs.
- **Night auto-levels** (#31): a dynamic LUT built from the scene's own luma
  percentiles — the digital AGC every commercial IR camera runs — plus ISP
  hardware denoise at HighQuality and a 3 Mbps encoder floor while night mode
  is active.

## [1.4] — 2026-07-01

### Added

- **Enriched status dashboard** (#20, #26): temperature, throttle state, CPU
  load, RAM/swap, per-service status, snapshot freshness, HKSV state, motion.
- `TROUBLESHOOTING.md`, an SD-flashing guide, and refreshed READMEs (#22, #28).
- Test coverage for the config deep-merge used on update (#24).

## [1.3] — 2026-06-30

### Added

- Dynamic resolution list advertised to HomeKit.

### Fixed

- **Snapshots moved to tmpfs** (#21) — 24/7 rewrites were wearing the SD card.
- Live time-to-first-frame cut by shortening the GOP to 1 s (`iperiod = fps`).
- Night-mode exit made reliable via a colour-confirmed probe; the Lux-based
  approach was removed (850 nm IR reads as ~200 lux on an OV5647).

## [1.2] — 2026-06-17

### Added

- **Night-vision grayscale** on both the snapshot and the live stream, killing
  the 850 nm pink cast at zero CPU cost.
- ISP sharpness/contrast/saturation controls; H264 High profile; smart sensor
  mode selection (prefer supersampling over upscaling).
- Unit test suites for Python and TypeScript, run on every push (CI).

## [1.1] — 2026-06-15

### Added

- Watchdog: automatic restart on a libcamera frontend timeout.

## [1.0] — 2026-06-12

First tagged release: live streaming (hardware H264 passthrough to HomeKit
over SRTP), HomeKit Secure Video with a rolling prebuffer, motion detection
(MOG2) with classification left to the Apple home hub, snapshots, and the
pairing/status web page.
