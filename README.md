# pi4-IA-Homekit-Camera

**🇫🇷 [Version française](README.fr.md)**

Turn a Raspberry Pi and a camera module into a **native HomeKit Secure Video camera** like a camera fresh out of the box.

```
Install  →  scan the QR code  →  done.
```

No Homebridge, no plugins, no cloud account, no admin dashboard to babysit. The camera pairs directly with the Home app, streams live video, detects motion, and records HKSV clips to iCloud that start *before* the motion happened.

## Features

- **Live streaming** — the Pi's hardware H264 encoder is passed straight through to HomeKit (SRTP, zero re-encoding). Fluid 1080p30 with near-idle CPU. IPv6 controllers are supported (*beta* — implemented per spec, not yet field-tested on an IPv6-preferred network; reports welcome).
- **HomeKit Secure Video** — motion-triggered recordings stored in iCloud, viewable directly in the Home app's timeline. A rolling 4-second prebuffer means every clip starts before the motion event.
- **Smart classification** — People / Animals / Vehicles detection is done by your Apple home hub (Apple TV / HomePod), exactly like commercial HKSV cameras. The Pi just reports motion, cheaply and reliably.
- **Rich notifications** — motion alerts with a snapshot on your iPhone.
- **Status dashboard** — a built-in web page (`http://<pi>.local:8080`) shows the pairing QR code and a live health view: overall status, temperature & throttle state, CPU load, RAM/swap, uptime, per-service status, snapshot freshness, HKSV state and last motion.
- **Lightweight** — ~210 MB RAM with an active stream, low CPU load, three small systemd services.
- **Private** — everything runs on your Pi. The RTSP stream is bound to localhost (never exposed on the network); the only cloud involved is your own iCloud (for HKSV recordings, end-to-end encrypted by Apple).

## Requirements

| | |
|---|---|
| **Board** | Raspberry Pi 4 (any RAM size), Pi Zero 2 W, Pi 3. |
| **Camera** | Any CSI camera module supported by `libcamera` (Camera Module 2/3, HQ, NoIR…) |
| **OS** | Raspberry Pi OS **64-bit** |
| **Apple side** | iPhone + a home hub (Apple TV 4K or HomePod) |
| **For recordings** | iCloud+ subscription (any tier — HKSV recordings don't count against your storage) |

> **Pi Zero 2 W**: fully supported, including HKSV. Measured on a real unit: ~194 MB RAM
> idle, ~212 MB with an active live stream (out of 512 MB). The zram swap does get used
> (~180 MB, more while HKSV recording is armed) — that's compressed RAM, not the SD
> card, and it is absorbed without any tuning.
> A **heatsink** is strongly recommended: the SoC runs hot under continuous load.
> Even with a full-board heatsink, expect ~75–80 °C and occasional throttling in a
> closed enclosure — add ventilation holes or a small 5 V fan to stay safely below.

## Flash the SD card

Starting from a blank card, use **[Raspberry Pi Imager](https://www.raspberrypi.com/software/)**:

1. **Choose OS** → *Raspberry Pi OS (other)* → **Raspberry Pi OS Lite (64-bit)**. Lite is enough — the camera runs headless, no desktop needed; 64-bit is required on the Zero 2 W.
2. **Choose Storage** → your SD card.
3. Click the **gear icon** (⚙ / `Ctrl+Shift+X`) to open the advanced settings, so the Pi boots straight onto your network with no screen or keyboard:
   - **Hostname** (e.g. `cam-pi-zero`) — you'll reach the camera at `http://<hostname>.local`
   - **Enable SSH** (password or public key)
   - **Wi-Fi** SSID + password (and your country)
   - **Username / password**, locale and keyboard layout
4. **Write** the image, insert the card and power on the Pi.
5. SSH in, then continue with **Install** below:
   ```bash
   ssh <username>@<hostname>.local
   ```

## Install

```bash
git clone https://github.com/AlexBtlle/pi4-IA-Homekit-Camera.git
cd pi4-IA-Homekit-Camera
sudo bash install.sh
```

The installer sets up everything: system packages, Node.js 22, mediamtx, the Python camera pipeline, the HomeKit app, and three systemd services. At the end it prints your **pairing PIN**, and the HomeKit service logs a **QR code**:

```bash
journalctl -u pi4cam-homekit -b --no-pager | head -40
```

### Pair with the Home app

1. Open `http://<pi-hostname>.local:8080` in Safari on your iPhone or Mac
   — the page shows the QR code and PIN for your camera, plus a live status
   dashboard (services, temperature, memory, motion)
2. Open **Home** → **+** → **Add Accessory** → scan the QR code
   (or tap *More options…* and enter the PIN)
3. The "not certified" warning is normal for any DIY accessory — tap *Add Anyway*

### Enable HomeKit Secure Video

1. Long-press the camera tile → settings (gear icon)
2. **Recording Options** → select **Stream & Allow Recording**
3. Choose when to record (e.g. *When motion is detected*) and which activity (People, Animals, Vehicles…)

That's it. Walk in front of the camera: a clip appears in the Home app timeline, starting ~4 seconds before you entered the frame.

## Update

To update an existing install to the latest version:

```bash
cd pi4-IA-Homekit-Camera
git pull
sudo bash install.sh
```

The installer rebuilds what changed and restarts the three services itself. Updates are safe by design:

- **Your settings are preserved** — `/opt/pi4cam/config.yaml` is never overwritten; only keys introduced by the new version are added (the annotated defaults land in `/opt/pi4cam/config.yaml.dist` so you can diff).
- **No re-pairing** — the pairing secrets survive updates, so the camera keeps its identity in the Home app and its HKSV history.

After the update, open `http://<pi>.local:8080` and check that everything is green.

## How it works

```
┌─ pi4cam.service (Python) ──────────────────────────────────┐
│ picamera2                                                   │
│  ├─ main 1920×1080, hardware H264 (keyframe every 1 s)      │
│  │    └→ ffmpeg -c copy → RTSP → mediamtx                   │
│  ├─ main YUV420 → JPEG snapshot → /dev/shm every 2 s        │
│  └─ lores 320×240 → OpenCV MOG2 motion detection            │
│       └→ POST localhost:8989/motion                         │
│  (frame watchdog: restarts on libcamera frontend timeout)   │
└─────────────────────────────────────────────────────────────┘
┌─ mediamtx.service ─────────────────────────────────────────┐
│ RTSP fan-out (127.0.0.1:8554) — 1 producer, N consumers     │
└─────────────────────────────────────────────────────────────┘
┌─ pi4cam-homekit.service (Node, HAP-NodeJS) ────────────────┐
│ Standalone HomeKit camera accessory:                        │
│  • Live   : RTSP → SRTP passthrough (-c:v copy)             │
│  • Snapshot: serves the latest tmpfs JPEG (instant)         │
│  • Motion : MotionSensor + local HTTP endpoint :8989        │
│  • HKSV   : continuous fragmented-MP4 prebuffer (6 s ring)  │
│             → recording delegate streams init + live        │
│               fragments to the home hub on motion           │
│  • Web    : QR + status dashboard on :8080                  │
└─────────────────────────────────────────────────────────────┘
```

The video is encoded **once**, in hardware, on the camera. Everything downstream (live stream, recordings, snapshots) reuses that same H264 stream without re-encoding — that's why it stays fluid and light.

### Passthrough design notes

Zero re-encoding is the choice that makes this project viable on a Pi Zero 2 W — and it implies deliberately ignoring part of what HomeKit negotiates. These are known tolerances, shared by the DIY ecosystem (Scrypted, homebridge-camera-ffmpeg…), documented here for transparency:

- **Fixed resolution** — HomeKit picks a resolution from the advertised list (often 640×360 for the grid or remote viewing) but always receives the native stream (1080p by default). iOS scales it client-side.
- **Bitrate** *(the one negotiated parameter that IS honoured)* — live sessions drive the hardware encoder toward what they negotiate (~2 Mbps remote/cellular) and it returns to the configured ceiling when they leave. On a modest uplink, also consider a `camera.bitrate` of 3–4 Mbps.
- **H264 profile** — the stream is always High profile regardless of what was negotiated; Apple decoders read it without complaint.
- **HKSV recording configuration** — the profile/bitrate/iFrameInterval selected by the home hub are not applied (same passthrough reason); iCloud clips weigh whatever the camera encodes.
- **Ghost live audio** — an AAC-ELD audio block is declared because HomeKit requires one in the negotiation, but no audio packet is ever sent (the camera module has no microphone). The speaker icon in the Home app does nothing.
- **Snapshots** — always served at 1280×720 whatever size is requested; iOS resizes.

## Configuration

Everything lives in one file: [`config.yaml`](config.yaml). On an installed system, edit `/opt/pi4cam/config.yaml` directly, then restart the services (`sudo systemctl restart pi4cam pi4cam-homekit`). Re-running `install.sh` never overwrites your values — it only injects keys added by newer versions (an annotated reference is kept at `/opt/pi4cam/config.yaml.dist`).

| Key | Default | Description |
|---|---|---|
| `camera.width` × `height` | 1920×1080 | Capture / stream / recording resolution |
| `camera.fps` | 30 | Frame rate |
| `camera.bitrate` | 8000000 | H264 bitrate (bit/s) — ~8 Mbps for crisp 1080p30; lower to ~4 Mbps to save bandwidth |
| `camera.rotation` | 0 | 0 / 180 only — the Pi ISP cannot rotate 90°/270° (ignored with a warning) |
| `camera.full_fov` | true | Use the full sensor area so the lens shows its full angle. Most sensors (IMX219, OV5647…) center-crop in native 1080p mode, narrowing the view; this forces a full-FOV (binned) mode and scales to the output size. Set `false` for the sharper but narrower native crop. |
| `camera.sharpness` | 1.0 | ISP edge sharpening (0.0–16.0). Try 1.5–2.0 to compensate for lens softness. |
| `camera.contrast` | 1.0 | ISP contrast (0.0–32.0). |
| `camera.saturation` | 1.0 | ISP colour saturation (0.0–32.0). Try 1.2–1.5 for richer colours. |
| `camera.ir_grayscale` | false | **(beta)** Auto-switch the stream **and** snapshot to grayscale under IR night vision, removing the 850 nm pink cast. IR is detected from the chroma statistics of the detection stream (with hysteresis), and the effect neutralises the frame's colour planes before encoding — day/night transitions are measured on real colour data. |
| `camera.snapshot_path` | /dev/shm/pi4cam-snapshot.jpg | Where the JPEG snapshot is written — a tmpfs (RAM) path, to keep the 24/7 rewrites off the SD card. |
| `homekit.camera_name` | Pi Camera | Name shown in the Home app |
| `homekit.motion_timeout` | 10 | Seconds the motion sensor stays active |
| `detection.min_motion_area` | 1500 | Motion sensitivity, in absolute pixels on the low-res detection frame (smaller = more sensitive). 1500 is tuned for humans at 320×240; reduce to ~300–600 to also catch cats/dogs. Recalibrate if you change `lores_width`/`lores_height`. |
| `detection.cooldown` | 30 | Quiet time between two motion *episodes*. A continuous movement keeps the sensor — and the HKSV clip — active for its whole duration. |

The pairing secrets (PIN, setup ID, accessory MAC) are generated once by the installer into `/opt/pi4cam/homekit/pairing.json` and survive re-installs — updating the code never requires re-pairing.

## Troubleshooting

For a full symptom-by-symptom guide (thermal/throttling, memory & swap, stream
latency, motion tuning, pairing backup…), see **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)**.

```bash
journalctl -u pi4cam -f            # camera pipeline + motion detection
journalctl -u pi4cam-homekit -f    # HomeKit app (pairing QR, streams, HKSV)
journalctl -u mediamtx -f          # RTSP server
```

- **Camera not found when pairing** — both the iPhone and the Pi must be on the same network; check that `avahi-daemon` is running (mDNS).
- **"Recording Options" missing in the Home app** — the accessory's capabilities are cached at pairing time. Remove the camera from the Home app and pair it again.
- **Snapshot looks frozen** — the Python pipeline refreshes `/dev/shm/pi4cam-snapshot.jpg` every 2 s. If it stops updating, check `journalctl -u pi4cam` (the frame watchdog restarts the service automatically on a libcamera timeout).
- **Check service health** — open `http://<pi>.local:8080`: the status dashboard shows each service's status, temperature & throttle state, memory/swap and the motion count.
- **Check the raw stream** — the RTSP feed is bound to localhost for privacy, so probe it *from the Pi itself*: `ffprobe rtsp://127.0.0.1:8554/camera` should show `h264, 1920x1080`.

## Uninstall

```bash
sudo bash uninstall.sh
```

Removes the services, `/opt/pi4cam`, mediamtx, Node.js and the nodesource repo. System packages (picamera2, opencv, ffmpeg) are left in place.

## Built with

- [HAP-NodeJS](https://github.com/homebridge/HAP-NodeJS) — the HomeKit Accessory Protocol implementation (including HKSV)
- [mediamtx](https://github.com/bluenviron/mediamtx) — RTSP server
- [picamera2](https://github.com/raspberrypi/picamera2) / libcamera — camera capture & hardware H264
- Inspired by [pi0-Camera-HomeKit](https://github.com/AlexBtlle/pi0-Camera-HomeKit)

## License

[GPL-3.0](LICENSE)
