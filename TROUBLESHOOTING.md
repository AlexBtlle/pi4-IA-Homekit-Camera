# Troubleshooting

A symptom-by-symptom guide for a deployed install. Paths assume the default
install location `/opt/pi4cam`. The primary target is the **Raspberry Pi Zero 2 W**,
so most notes are written with its constraints in mind.

Three systemd services make up the system:

| Service | Role |
|---|---|
| `pi4cam` | Camera pipeline (picamera2 → hardware H264 → RTSP) + motion detection |
| `pi4cam-homekit` | HomeKit accessory (live stream, snapshot, HKSV, status page) |
| `mediamtx` | Local RTSP server (`rtsp://127.0.0.1:8554/camera`) |

---

## First reflexes

Before digging in, two quick checks cover most situations.

**1. The status dashboard.** Open `http://<pi-hostname>.local:8080` — the page
shows an at-a-glance health view: overall status pill, temperature and throttle
state, CPU load, RAM/swap, uptime, service status, snapshot freshness, HKSV
armed state and last motion. Start here.

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
- **Cold vs warm start.** Spawning the live-view `ffmpeg` costs ~1.7 s of pure
  CPU (dynamic linking at 1 GHz) plus, when the page cache went cold, ~3.4 s of
  SD reloads (measured on a Zero 2 W: `time ffmpeg -version` 5.1 s cold, 1.7 s
  hot). The install ships **`pi4cam-warm.timer`** — a soft `vmtouch` every
  10 min that keeps ffmpeg's libraries cached (pages stay evictable) — so cold
  starts behave like warm ones. On session start the app also **forces
  encoder keyframes** (salvo over the first 3 s) so the viewer never waits out
  the GOP once ffmpeg is connected.

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
     shadows. `2.2` fits most scenes; `1.0` disables. The per-minute
     `IR stats … lut=low→high` log line shows the measured range.
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
- **"Recording Options" missing in the Home app** — the accessory's capabilities
  are cached at pairing time. Remove the camera from the Home app and pair it
  again.
- **"Not certified" warning** — normal for any DIY HomeKit accessory. Tap *Add
  Anyway*.
