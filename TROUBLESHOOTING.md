# Troubleshooting

A symptom-by-symptom guide for a deployed install. Paths assume the default
install location `/opt/pi4cam`. The primary target is the **Raspberry Pi Zero 2 W**,
so most notes are written with its constraints in mind.

The systemd units installed by `install.sh`:

| Service | Role |
|---|---|
| `pi4cam` | Camera pipeline (picamera2 → hardware H264 → RTSP) + motion detection |
| `pi4cam-homekit` | HomeKit accessory (live stream, snapshot, HKSV, status page) |
| `mediamtx` | Local RTSP server (`rtsp://127.0.0.1:8554/camera`) |
| `pi4cam-warm` (+ timer) | Keeps ffmpeg's dependency tree soft-cached for fast live starts |

(One more unit exists in the repo but is **not** installed by install.sh:
`scripts/pi4cam-ircut-release.service`, the opt-in hardware IR-CUT daemon —
see its section below.)

## Contents

- [First reflexes](#first-reflexes)
- [Thermal & throttling](#thermal--throttling)
- [Memory & swap](#memory--swap)
- [Live stream is slow to appear (or won't show)](#live-stream-is-slow-to-appear-or-wont-show)
- [Camera service won't start / VIDIOC_STREAMON crash](#camera-service-wont-start--vidioc_streamon-crash)
- [Motion detection](#motion-detection)
- [HKSV clips: where they start and end](#hksv-clips-where-they-start-and-end)
- [IR night vision (beta)](#ir-night-vision-beta)
- [Hardware IR-CUT filter — GPIO day/night (opt-in)](#hardware-ir-cut-filter--gpio-daynight-opt-in)
- [USB webcam (beta)](#usb-webcam-beta)
- [Snapshot](#snapshot)
- [Pairing: backup & restore](#pairing-backup--restore)
- [Pairing / discovery](#pairing--discovery)
- [Pi Zero W v1 / Pi 1 (ARMv6) — unofficial](#pi-zero-w-v1--pi-1-armv6--unofficial-here-be-dragons)

---

## First reflexes

Before digging in, two quick checks cover most situations.

**1. The status dashboard.** Open `http://<pi-hostname>.local:8080` — the page
shows an at-a-glance health view: overall status pill, temperature and throttle
state, CPU load, RAM/swap, uptime, service status, snapshot freshness, HKSV
armed state, last motion, and whether the HomeKit mDNS announcement still
matches the machine's addresses. Start here.

**2. The logs.** Follow each service live:

```bash
journalctl -u pi4cam -f            # camera pipeline + motion detection
journalctl -u pi4cam-homekit -f    # HomeKit app (pairing QR, streams, HKSV)
journalctl -u mediamtx -f          # RTSP server
```

Service state and restart:

```bash
systemctl status pi4cam pi4cam-homekit mediamtx
sudo systemctl restart pi4cam pi4cam-homekit
```

All three services use `Restart=on-failure`, so a crash is followed by an
automatic restart. A tight restart loop means the same error keeps recurring —
read the log for the reason.

### Logs do not survive a reboot

On Raspberry Pi OS the systemd journal is kept in RAM and discarded when the
machine restarts, so `journalctl -b -1` reports:

```
Specifying boot ID or boot offset has no effect, no persistent journal was found.
```

A failure that ends in a reboot therefore cannot be investigated afterwards —
neither the reason for the restart nor anything logged before it.

Persistence can be turned on. **The installer does not do this, and this guide
does not advise it either** — it is written down so the option is known, with
its cost stated:

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=persistent\nSystemMaxUse=50M\n' \
  | sudo tee /etc/systemd/journald.conf.d/persistent.conf
sudo systemctl restart systemd-journald
```

What it costs: the journal is then written to the SD card continuously, for as
long as the camera runs. `SystemMaxUse` caps how much space it occupies — not
how often it writes, which is what wears the card out. This project moves
snapshots to `/dev/shm` for that exact reason, so enabling this trades a known
wear source against the ability to diagnose across a reboot. On a camera that
runs 24/7 and is never touched, that trade is not obviously worth making.

To undo it:

```bash
sudo rm /etc/systemd/journald.conf.d/persistent.conf
sudo systemctl restart systemd-journald
```

---

## Thermal & throttling

The Pi Zero 2 W has no heatsink by default and runs hot under continuous H264
encoding.

```bash
vcgencmd measure_temp      # e.g. temp=77.4'C
vcgencmd get_throttled     # e.g. throttled=0x0
```

`get_throttled` is a bitmask. The **low bits (0–3)** are the *current* state; the
**high bits (16–19)** are *sticky* — they record that an event happened since
boot, and reset only on reboot.

| Bit | Hex | Meaning |
|---|---|---|
| 0 | `0x1` | Under-voltage **now** |
| 1 | `0x2` | ARM frequency capped **now** |
| 2 | `0x4` | Throttled **now** |
| 3 | `0x8` | Soft temperature limit active **now** |
| 16 | `0x10000` | Under-voltage **has occurred** (since boot) |
| 17 | `0x20000` | ARM frequency capping **has occurred** |
| 18 | `0x40000` | Throttling **has occurred** |
| 19 | `0x80000` | Soft temperature limit **has occurred** |

Examples:
- `0x0` — all good, never throttled.
- `0x60000` — bits 17+18: frequency capping and throttling *have occurred* in the
  past, but nothing is active right now. The board crossed the ~80 °C limit at
  least once. **Not** under-voltage (bit 16 clear), so the power supply is fine.
- `0x50005` — under-voltage now *and* in the past → suspect the power
  supply/cable, not the temperature.

**Thermal thresholds:** the SoC soft-limits around 80 °C and hard-throttles by
85 °C. Sustained operation above ~80 °C means the CPU is being slowed to protect
itself.

**Fix:** fit a **full-board heatsink** (drops ~5 °C), and add ventilation or a
small 5 V fan for another 10–20 °C. With airflow you should sit around 55–65 °C
and never throttle.

The status dashboard shows the temperature (colour-coded) and the decoded
throttle state, so you rarely need the raw command.

---

## Memory & swap

512 MB shared with the GPU is tight. Watch the right numbers.

```bash
free -h
cat /proc/swaps          # is swap on zram (RAM-compressed) or the SD card?
```

**Read it correctly:** Linux counts the page cache as "used" RAM — a high
`used` figure is **not** a leak. What matters is:
- the **swap** trend (`Swap` in `free`), and
- the **RSS per service** over time.

For a long-term view, install `sysstat` (records every 10 min):

```bash
sudo apt install sysstat
sudo sed -i 's/ENABLED="false"/ENABLED="true"/' /etc/default/sysstat
sudo systemctl enable --now sysstat
sar -r      # RAM history (watch %memused vs kbcached)
sar -S      # swap history — the key curve
```

A genuine leak shows up as **swap climbing monotonically** and never coming
back down. RAM used that rises but is mostly `kbcached`, with flat swap, is
normal.

**Normal, not a leak:** memory jumps by ~250 MB of committed memory when HKSV
arms (you leave home). That's the continuous prebuffer `ffmpeg` starting up. It
drops back when recording disarms. On zram, that swap is RAM-compressed (~2:1),
so 180 MB of swap ≈ 60–90 MB of real RAM — fast, no SD wear.

---

## Live stream is slow to appear (or won't show)

The live view uses `ffmpeg -c:v copy` — no re-encoding. A hardware H264 decoder
**cannot render anything until it receives a keyframe**, so the delay is mostly
the wait for the next keyframe plus `ffmpeg` startup.

- **GOP / keyframe.** The encoder emits a keyframe every 1 s (`iperiod = fps` in
  `camera_manager.py`). Shorter GOP = faster first frame; don't raise it without
  reason (it lengthens the time-to-first-frame).
- **The ffmpeg startup tax.** Debian's ffmpeg links ~150 shared libraries;
  spawning it costs ~1.7 s of linking CPU on a quiet Zero 2 W — and **5-8 s**
  when memory is tight (HKSV armed), where the process stalls in reclaim
  while mapping ~50 MB of private pages (field-measured: 7.6 s real for
  1.5 s CPU with the page cache 100 % warm). Mitigations shipped: the
  **`pi4cam-warm.timer`** keeps ffmpeg's full dependency tree soft-cached,
  and the app **forces encoder keyframes** (salvo over the first 20 s) so
  the viewer never waits out the GOP once ffmpeg connects. The real cure is
  the **lean static ffmpeg** (RTSP/RTP/SRTP/H264-copy/AAC only, zero
  external libraries, ~0.2 s startup) at `/opt/pi4cam/bin/ffmpeg-static`,
  which the app auto-detects on restart (it logs its choice:
  `[main] ffmpeg for live/HKSV: …`). **On arm64 (Zero 2 W / Pi 3 / Pi 4),
  install.sh already downloads a checksum-verified prebuilt** — check
  whether the file is there before doing anything. Building locally with
  `bash scripts/build-static-ffmpeg.sh` (~45 min on the Pi) is only needed
  on armv6/armv7 boards, where no prebuilt is published. To revert to the
  system ffmpeg, delete that file.

Diagnose the cold-start theory (optional):

```bash
sudo apt install vmtouch
vmtouch -v $(readlink -f /usr/bin/ffmpeg /usr/lib/aarch64-linux-gnu/libav*.so.*)
# open the live view once, then re-run: residency should jump toward 100%
```

Hard-locking the libraries in RAM (`vmtouch -l`) is still not recommended on a
512 MB board — the shipped timer uses a soft touch precisely so the kernel can
reclaim those pages whenever it genuinely needs them.

**If the stream never appears at all:**

```bash
ffprobe rtsp://127.0.0.1:8554/camera   # run ON the Pi (RTSP is localhost-only)
```

Should report `h264, 1920x1080`. If it fails, the problem is upstream (`pi4cam`
or `mediamtx`), not HomeKit — check their logs.

**Black tile on an IPv6 network (beta):** when the iPhone/hub negotiates the
live stream over IPv6, the session log shows `negotiated … over IPv6 (beta)`.
That path (udp6 return ports + bracketed ffmpeg URL) is implemented per spec
but not yet field-validated. If the tile stays black only on IPv6-preferred
networks, check `journalctl -u pi4cam-homekit` for `prepareStream failed`
(the Pi may have IPv6 disabled — the error is deliberate and explicit) and
please open an issue with the log lines.

---

## Camera service won't start / `VIDIOC_STREAMON` crash

Symptom: `pi4cam` loops on restart, with `ProcessLookupError` or a
`VIDIOC_STREAMON` failure in `journalctl -u pi4cam`.

**Cause #1: resolution too high.** The hardware H264 encoder cannot exceed
**1920×1080 on any Pi model** — VideoCore IV (Zero 2 W, Pi 3) and VideoCore VI
(Pi 4) alike. Setting `camera.width` above 1920 crashes `VIDIOC_STREAMON`
(field-tested: OV5647 at 2592×1944 on a Zero 2 W, IMX708 at 2304×1296 on a
Pi 4 — same `ProcessLookupError`).

**Fix:** keep `width`/`height` at or below 1920×1080. See the recommended
resolutions per camera module documented at the top of
[`config.yaml`](config.yaml). After editing, re-run `sudo bash install.sh` and
restart.

> There is no runtime config validation by design — `config.yaml` documents the
> safe ranges, and this guide is the diagnostic net. If you change a default,
> you own the outcome.

---

## Motion detection

Detection runs OpenCV MOG2 on the low-resolution stream and posts to the
HomeKit app on movement; the Apple home hub does the People / Animals / Vehicles
classification.

- **`detection.min_motion_area` is absolute pixels on the lores frame**
  (`lores_width × lores_height`). If you change `lores_width`/`lores_height`, you
  must recalibrate it. A value near or above the whole lores area means detection
  never triggers.
- **Cat vs false positives** pull in opposite directions. Keep the threshold low
  enough to catch what you want (a cat triggers a few hundred px on a 256×192
  lores frame) and let **HKSV classification on the Apple TV / HomePod** filter
  notifications by type.
- **Ignoring regions** (a tree, a road): use HomeKit's built-in **Activity Zones**
  in the Home app rather than tuning on the Pi — it's the native, per-camera way
  to mask areas.

Watch triggers live:

```bash
journalctl -u pi4cam -f | grep -i motion
```

---

## HKSV clips: where they start and end

Who controls what, in a recorded clip's timeline:

- **The clip starts before the motion** thanks to the continuous prebuffer
  (~4 s ring, running whenever recording is enabled in the Home app). If your
  clips start *at* the motion instead, check that the prebuffer is alive at
  idle: `pgrep -af ffmpeg` should show a second ffmpeg (the `anullsrc` one)
  even with no motion, and `journalctl -u pi4cam-homekit` should log
  `serving N pre-roll fragment(s)` at each recording.
- **The clip's END is edited by the Apple home hub, not by the camera.** The
  hub runs its own video analysis (the same engine that classifies People /
  Animals / Vehicles) and trims the saved clip to the activity *it* sees.
  `detection.cooldown` and `homekit.motion_timeout` extend how long the
  *stream* stays open after the last motion (field-measured: 30 + 10 s of
  quiet tail is really sent) — but the hub cuts the boring tail when it
  assembles the iCloud clip, and no HomeKit parameter lets an accessory
  override that. Commercial HKSV cameras behave the same way. If a clip seems
  to stop abruptly at the end of the motion: that's the hub's editing, not a
  camera bug.

---

## IR night vision (beta)

`ir_grayscale` is a **beta** feature, **off by default**. When enabled, the
stream and snapshot switch to grayscale under IR illumination, killing the pink
cast from 850 nm LEDs.

**How it works** (v2): IR is detected from the *chroma statistics* of the
low-res detection stream — 850 nm light collapses chroma to a uniform residual
cast (near-zero variance), whereas daylight scenes have diverse hues. When
night mode is active, the frame's colour planes are neutralised in the camera
callback just before H264 encoding; the ISP saturation is never touched, so the
detector always sees real colour data and dawn/dusk transitions are picked up
within seconds (entry ~3 s, exit ~10 s of consistent frames — the asymmetry
stops car headlights from flipping the mode at night).

- **Transitions logged**: look for `Night vision detected → grayscale stream` /
  `Daylight detected → colour stream` in `journalctl -u pi4cam`.
- **Image too dark at night?** Three knobs, in the order to try them:
  1. `camera.ir_gamma` (default `2.2`) — **night auto-levels**: while night
     mode is active, the scene's real signal range is measured continuously
     (lores-luma percentiles, flicker-smoothed, digital gain capped at ~5x)
     and stretched to full scale before encoding — stream, HKSV and snapshot
     all get it, and it works even with the sensor fully saturated. This is
     the digital AGC every commercial IR camera runs; static curves cannot do
     the job (a deep-night scene lives entirely at luma ~15-50, at the noise
     floor — field-measured). The value shapes the curve: higher = brighter
     shadows. `2.2` fits most scenes; `1.0` disables. The periodic
     `IR stats … lut=low→high` log line (every 10 min) shows the measured
     range and the sensor's real exposure/gain — the calibration evidence.
  2. `camera.ir_min_fps` (default `10`) — **real light**: lets the framerate
     drop when dark so the exposure lengthens (at the configured fps libcamera
     caps the shutter at ~1/fps s and the AE can only add gain). No added
     noise; costs night framerate (motion blur) and a longer keyframe interval.
     Note the sensor mode has its own exposure ceiling — going absurdly low
     (1 fps) stops helping once that ceiling is hit. `>= fps` disables.
  3. `camera.ir_exposure` (EV bias, `+1` = 2× target) — fine-tune only; with a
     saturated sensor it does nothing (field-confirmed: EV +8, zero change).

  Edit `/opt/pi4cam/config.yaml`, `sudo systemctl restart pi4cam`, and watch
  `journalctl -u pi4cam` — `Night camera tuning on → …` confirms the controls
  applied, and the per-minute `IR stats: … exp=…ms gain=…x` line shows how the
  sensor actually responded (whether the shutter and gain really moved).
- **Never enters grayscale at night?** The AWB may be cancelling the cast more
  aggressively than the thresholds assume. The detector constants live at the
  top of `camera/camera_manager.py` (`IR_CHROMA_STD_MAX`, `IR_CAST_MIN`).
- **AWB gains and Lux are *not* usable signals** at 850 nm (gains barely move,
  and IR reads as ~200 lux on an OV5647) — past approaches based on them were
  removed; don't reintroduce them.

---

## Hardware IR-CUT filter — GPIO day/night (opt-in)

Some camera modules carry a **mechanical IR-cut filter** — glass in front of
the sensor by day (true colours), retracted by night so 850 nm IR reaches the
sensor. On the field-tested module (**Waveshare IMX219-160 IR-CUT**) that
filter is *not* autonomous: its onboard photoresistor drives the IR **LEDs**,
but the **filter** only follows a control pin, so the Pi must decide day/night
and drive it. This is a **standalone, opt-in** add-on — nothing in the main
pipeline touches it; pi4cam runs identically with or without it.

**How it works.** `pi4cam` publishes the AEC's Lux estimate to
`/dev/shm/pi4cam-lux` as write-only telemetry (a few times a minute — no
filter logic in the main program). The standalone daemon
`scripts/ircut_release_gpio.py --watch` reads that number and drives the GPIO
with two-threshold hysteresis:

- **GPIO output low** → **day** (filter engaged, true colours)
- **GPIO input / no pull** → **night** (filter retracted; the module lights
  its own IR LEDs separately)

Field-tested polarity — **confirm on yours**, the Waveshare wiki has it
backwards. Wiring: the module's IR-CUT control pin to a free GPIO
(field-tested **BCM17 = physical pin 11**) with a **common ground** (already
shared if the module is powered from the Pi's CSI/header).

> **⚠ The feedback trap — the one thing to get right.** The measured Lux
> *depends on the filter position*: engaged, IR is blocked (a dusk scene read
> ~7 lux); retracted, the IR + the module's own LEDs reach the sensor, so the
> **same** scene reads much higher (~27 lux). If `--day-above` sits below that
> IR-lit reading, the filter **oscillates** (dark → retract → Lux jumps above
> day_above → re-engage → Lux drops → …, a ~75 s clignotement seen in the
> field). **`--day-above` must be set ABOVE the Lux the scene reads at night
> with the filter out** — the daemon logs it. Field values: filter-in dusk
> ≈ 7 lux, filter-out IR-lit ≈ 27 lux → `--day-above 45` keeps night stable
> while a real dawn (visible light, well past 45) still flips back cleanly.

### Enable

1. **Wire it** (above) and confirm the pin controls the filter — you should
   hear a click:
   ```bash
   python3 scripts/ircut_release_gpio.py --gpio 17 --night   # filter out
   python3 scripts/ircut_release_gpio.py --gpio 17 --day     # filter in
   python3 scripts/ircut_release_gpio.py --gpio 17 --status
   ```
2. **Enable the Lux telemetry** — it is **opt-in** (empty = off, the shipped
   default): set the key in `/opt/pi4cam/config.yaml`
   ```yaml
   camera:
     lux_path: /dev/shm/pi4cam-lux
   ```
   then deploy/restart (`install.sh` runs the camera from `/opt/pi4cam` — a
   `git pull` alone is not enough) and verify:
   ```bash
   sudo bash install.sh           # safest full deploy (preserves your config)
   sudo systemctl restart pi4cam
   cat /dev/shm/pi4cam-lux        # a number must appear within a few seconds
   ```
   Without the key, the daemon logs `no fresh Lux` and holds day mode.
3. **Install the daemon** (`install.sh` does **not** deploy `scripts/` — it
   runs from your git checkout, so fix the path and pin):
   ```bash
   sudo cp scripts/pi4cam-ircut-release.service /etc/systemd/system/
   sudo sed -i "s#/home/pi/pi4-IA-Homekit-Camera#$HOME/pi4-IA-Homekit-Camera#" \
     /etc/systemd/system/pi4cam-ircut-release.service
   # edit --gpio and the thresholds in the unit if needed, then:
   sudo systemctl daemon-reload
   sudo systemctl enable --now pi4cam-ircut-release.service
   journalctl -u pi4cam-ircut-release -f
   ```
   You should see a single `→ NIGHT` / `→ DAY` per real transition and stable
   `lux=… state=…` heartbeats in between — **not** a flip-flop (if it clignote,
   raise `--day-above`, see the feedback trap).

### Disable

```bash
sudo systemctl disable --now pi4cam-ircut-release.service
python3 scripts/ircut_release_gpio.py --gpio 17 --day   # leave the filter engaged (day)
```

Stopping the daemon leaves the GPIO wherever it last was, so reset it to day
(or unplug the control wire — the module reverts to its own default). To also
stop the telemetry, clear `camera.lux_path` in `/opt/pi4cam/config.yaml`
(empty = off) and restart `pi4cam` — though leaving it on is harmless
(~10 bytes to tmpfs every 2 s).

### Calibrate

Watch `journalctl -u pi4cam-ircut-release -f` and set the thresholds in the
service unit's `ExecStart` from real readings on your rig:

- **`--night-below`** (script default 8; the shipped unit passes 15) — how dark
  before it goes night. Raise it to switch earlier at dusk (more ambient light
  left), lower it to wait for deeper dark.
- **`--day-above`** (default 45) — the anti-oscillation guard: keep it **above**
  your filter-out night Lux (the trap above). Only `--day-above > --night-below`
  is valid.
- **`--samples` / `--interval`** (default 4 × 15 s ≈ 1 min sustained) — debounce,
  so a cloud or a passing headlight never toggles the filter. Shorten both for a
  snappier test run.

**Relation to `ir_grayscale`.** They are complementary layers, not the same
thing: the IR-cut is the *physical* filter (colours by day, IR sensitivity by
night); `ir_grayscale` (software, above) neutralises the residual pink cast
that returns at night once the filter is retracted and the IR LEDs are on. With
a working IR-cut you can leave `ir_grayscale` off and accept a slight night
tint, or enable it for clean grayscale nights — your call.

---

## USB webcam (beta)

`camera.source: usb` drives a UVC webcam through one long-lived ffmpeg
instead of picamera2 — live view, snapshots, motion detection **and HKSV**
work unchanged (everything downstream only sees mediamtx and the snapshot
file). Requested by a Pi 3 user; field feedback welcome.

**Pick the right `usb_format`** — list what your webcam actually outputs:

```bash
v4l2-ctl --device /dev/video0 --list-formats-ext
```

- `MJPG` → `usb_format: mjpeg` (most webcams; decoded then re-encoded by the
  Pi's **hardware** H264 encoder `h264_v4l2m2m` — never software x264)
- `H264` → `usb_format: h264` — the jackpot: onboard encoder, `-c:v copy`
  end-to-end, near-zero CPU (the CSI philosophy, ideal on a Zero 2 W)
- `YUYV` → `usb_format: yuyv` — raw frames: USB 2.0 bandwidth caps this
  around 720p30. Prefer MJPEG when available.

**Match `width`/`height`/`fps` to a mode the webcam really offers** (from the
same v4l2-ctl output) — ffmpeg fails fast on an unsupported combination and
the service restarts in a loop; `journalctl -u pi4cam` shows ffmpeg's error.

**Beta limitations:** `ir_grayscale` is CSI-only (ignored with a warning);
dynamic bitrate (#47) and instant-keyframe startup (#43) need the encoder
ioctl handle the CSI backend owns — on USB the bitrate is fixed at
`camera.bitrate` and a live view waits out the 1 s GOP (h264-native webcams:
whatever GOP the camera uses, check its own settings if startup feels slow).

---

## Snapshot

The camera service writes a fresh JPEG to a **tmpfs** path (RAM-backed, no SD
wear) every `snapshot_interval` seconds — default `/dev/shm/pi4cam-snapshot.jpg`
(`camera.snapshot_path`). The HomeKit app just reads that file.

```bash
ls -l /dev/shm/pi4cam-snapshot.jpg   # mtime should advance every few seconds
```

If the dashboard shows **snapshot stale** (or the timestamp stops advancing),
`pi4cam` has stopped producing frames — check `journalctl -u pi4cam`. The frame
watchdog restarts the service automatically on a libcamera timeout.

---

## Pairing: backup & restore

The pairing identity lives in `/opt/pi4cam/homekit/`:
- `pairing.json` — PIN, setup ID, accessory MAC
- `persist/` — HAP pairing state

These are **secrets and are never committed to git**. They are generated once by
`install.sh` and survive code updates. **If the SD card dies, the pairing is
lost** — you'd have to remove the camera from the Home app and add it again,
losing its link to existing HKSV history.

**Back up (run on the Pi, then copy the archive off the device):**

```bash
sudo tar czf pi4cam-pairing-backup.tgz -C /opt/pi4cam/homekit pairing.json persist
# then scp/copy pi4cam-pairing-backup.tgz somewhere safe
```

> ⚠️ This archive contains the accessory's **Ed25519 private key** (in
> `persist/AccessoryInfo.*.json`) — anyone holding it can impersonate the
> camera. Store the backup like a password, not like a config file.

**Restore onto a freshly imaged card** (after running `install.sh`, which
generates *new* secrets — restore over them so the accessory keeps its identity
and you don't have to re-pair):

```bash
sudo systemctl stop pi4cam-homekit
sudo tar xzf pi4cam-pairing-backup.tgz -C /opt/pi4cam/homekit
sudo chown -R "$USER:$USER" /opt/pi4cam/homekit/pairing.json /opt/pi4cam/homekit/persist
sudo systemctl start pi4cam-homekit
```

The Home app then sees the same camera — no re-pairing needed.

---

## Pairing / discovery

- **Camera not found when adding it** — the iPhone and the Pi must be on the same
  network, and mDNS must work. Check `avahi-daemon` is running.
- **Camera already paired shows "Not Responding"** — usually the mDNS
  announcement no longer matches the machine. HAP announces the accessory on
  the addresses it holds at startup and never re-announces on its own, so a
  changed IP address leaves a perfectly healthy camera unreachable: the service
  keeps running, motion keeps being detected, and only HomeKit sees nothing.
  The dashboard's *HomeKit announce* row reports this. The fix is a restart:

  ```bash
  sudo systemctl restart pi4cam-homekit
  ```

  The startup case — service announcing before Wi-Fi has an address, seen after
  a reboot — no longer happens: the service now waits for a non-loopback IPv4
  before announcing.
- **"Recording Options" missing in the Home app** — the accessory's capabilities
  are cached at pairing time. Remove the camera from the Home app and pair it
  again.
- **"Not certified" warning** — normal for any DIY HomeKit accessory. Tap *Add
  Anyway*.

---

## Pi Zero W v1 / Pi 1 (ARMv6) — unofficial, here be dragons

**Not a supported target.** The primary board is the Pi Zero 2 W. The single
1 GHz ARM11 core (ARMv6, no NEON) with 512 MB is right at the edge — but a
**live** HomeKit camera does run on it, field-tested. This is a recipe for the
curious, not a supported configuration.

| | On a Zero W v1 |
|---|---|
| Live view | ✅ works, ~4-5 s to appear (with the local static ffmpeg below) |
| Snapshot / dashboard | ✅ works |
| Motion → notifications | ✅ works (webhook may time out during the Node boot — harmless, it retries) |
| **HKSV (iCloud recording)** | ❌ **not recommended** — see below |

**HKSV is not recommended on the v1.** During an active live the single core
already sits at ~90-96 % (most of it Node/HAP doing SRTP in JS). HKSV adds a
*continuous* prebuffer ffmpeg plus fragmented-MP4 muxing on top of that same
core — it will tip an already-saturated CPU over. Run this board as **live +
motion notifications**, and leave "Recording Options" on *Stream Only* (or off).
If you want iCloud recording, use a Zero 2 W.

### Recipe

1. **Flash 32-bit Raspberry Pi OS.** The 64-bit image does not boot on ARMv6.
   The Foundation's 32-bit build is compiled for ARMv6, so every apt package is
   compatible.

2. **Get the camera detected.** If `rpicam-hello --list-cameras` shows nothing,
   `camera_auto_detect=1` in `/boot/firmware/config.txt` usually isn't enough on
   a Zero — add the explicit overlay for your sensor and reboot:
   ```bash
   echo "dtoverlay=ov5647" | sudo tee -a /boot/firmware/config.txt   # or imx219 / imx708
   sudo reboot
   ```

3. **Add swap** so `npm ci` + `tsc` don't get OOM-killed on 512 MB:
   ```bash
   sudo dphys-swapfile swapoff
   sudo sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=1024/' /etc/dphys-swapfile
   sudo dphys-swapfile setup && sudo dphys-swapfile swapon
   ```

4. **Install normally.** `install.sh` already handles ARMv6: it keeps the
   apt-provided Node (NodeSource has no ARMv6 build) and fetches the
   `linux_armv6` mediamtx. The prebuilt static ffmpeg is arm64-only, so it
   falls back to the system ffmpeg here — which is exactly the slow part we fix
   in step 6.
   ```bash
   git clone https://github.com/AlexBtlle/pi4-IA-Homekit-Camera.git
   cd pi4-IA-Homekit-Camera && sudo bash install.sh
   ```

5. **Calm the config for the single core** (`/opt/pi4cam/config.yaml`). MOG2 has
   no NEON on ARMv6, so `analysis_fps` is the dominant CPU lever:
   ```yaml
   camera:
     width: 1280
     height: 720
     fps: 15
     bitrate: 3000000
     lores_width: 160
     lores_height: 120
     snapshot_interval: 15
   detection:
     analysis_fps: 2
     min_motion_area: 200   # recalibrated for 160×120 lores
   ```

6. **Build the static ffmpeg locally — the decisive lever.** Debian's ffmpeg
   costs ~6 s just to *start* on this core (measured: 6.4 s real for 3.1 s CPU —
   the shared-library / memory-reclaim tax, see the "Live stream is slow" section
   above), and the live path spawns it twice. The prebuilt release is arm64-only,
   so build it on the Pi once (single core, `-j1`; ~45-90 min, run detached):
   ```bash
   cd ~/pi4-IA-Homekit-Camera
   sudo JOBS=1 nohup bash scripts/build-static-ffmpeg.sh > /tmp/ffmpeg-build.log 2>&1 &
   tail -f /tmp/ffmpeg-build.log      # Ctrl-C stops watching, not the build
   ```
   When it finishes it installs to `/opt/pi4cam/bin/ffmpeg-static`; the app
   auto-detects it on restart. Verify and restart:
   ```bash
   time /opt/pi4cam/bin/ffmpeg-static -version >/dev/null   # ~0.02 s vs 6.4 s
   sudo systemctl restart pi4cam-homekit
   ```

### Measured reality (this rig, IMX219, calmed config, local static ffmpeg)

- Live appears in **~4-5 s** (down from ~25-30 s on the system ffmpeg).
- Idle (camera + MOG2, no viewer): load ~1.4.
- During an active live: single core **~90-96 %**, load ~3.3-3.7, RAM 250/427 MB
  with swap barely touched. Busy but functional — and with no headroom left for
  HKSV, hence the recommendation above.
