# pi4-IA-Homekit-Camera

**🇫🇷 [Version française](README.fr.md)**

Turn a Raspberry Pi 4 and a camera module into a **native HomeKit Secure Video camera** — like a €300 camera fresh out of the box.

```
sudo bash install.sh  →  scan the QR code  →  done.
```

No Homebridge, no plugins, no cloud account, no web dashboard. The camera pairs directly with the Home app, streams live video, detects motion, and records HKSV clips to iCloud that start *before* the motion happened.

## Features

- **Live streaming** — the Pi's hardware H264 encoder is passed straight through to HomeKit (SRTP, zero re-encoding). Fluid 1080p30 with near-idle CPU.
- **HomeKit Secure Video** — motion-triggered recordings stored in iCloud, viewable directly in the Home app's timeline. A rolling 4-second prebuffer means every clip starts before the motion event.
- **Smart classification** — People / Animals / Vehicles / Packages detection is done by your Apple home hub (Apple TV / HomePod), exactly like commercial HKSV cameras. The Pi just reports motion, cheaply and reliably.
- **Rich notifications** — motion alerts with a snapshot on your iPhone.
- **Lightweight** — ~330 MB RAM total, low CPU load, three small systemd services.
- **Private** — everything runs on your Pi. The only cloud involved is your own iCloud (for HKSV recordings, end-to-end encrypted by Apple).

## Requirements

| | |
|---|---|
| **Board** | Raspberry Pi 4 (any RAM size) |
| **Camera** | Any CSI camera module supported by `libcamera` (Camera Module 2/3, HQ, NoIR…) |
| **OS** | Raspberry Pi OS Bookworm (64-bit recommended) |
| **Apple side** | iPhone + a home hub (Apple TV 4K or HomePod) |
| **For recordings** | iCloud+ subscription (any tier — HKSV recordings don't count against your storage) |

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
   — the page shows the QR code and PIN for your camera
2. Open **Home** → **+** → **Add Accessory** → scan the QR code
   (or tap *More options…* and enter the PIN)
3. The "not certified" warning is normal for any DIY accessory — tap *Add Anyway*

### Enable HomeKit Secure Video

1. Long-press the camera tile → settings (gear icon)
2. **Recording Options** → select **Stream & Allow Recording**
3. Choose when to record (e.g. *When motion is detected*) and which activity (People, Animals, Vehicles…)

That's it. Walk in front of the camera: a clip appears in the Home app timeline, starting ~4 seconds before you entered the frame.

## How it works

```
┌─ pi4cam.service (Python) ──────────────────────────────────┐
│ picamera2                                                   │
│  ├─ main 1920×1080, hardware H264 (keyframe every 4 s)      │
│  │    └→ ffmpeg -c copy → RTSP → mediamtx                   │
│  └─ lores 320×240 → OpenCV MOG2 motion detection            │
│       └→ POST localhost:8989/motion                         │
└─────────────────────────────────────────────────────────────┘
┌─ mediamtx.service ─────────────────────────────────────────┐
│ RTSP fan-out (:8554) — 1 producer, N consumers              │
└─────────────────────────────────────────────────────────────┘
┌─ pi4cam-homekit.service (Node, HAP-NodeJS) ────────────────┐
│ Standalone HomeKit camera accessory:                        │
│  • Live   : RTSP → SRTP passthrough (-c:v copy)             │
│  • Snapshot: one JPEG frame via ffmpeg (cached 4 s)         │
│  • Motion : MotionSensor + local HTTP endpoint :8989        │
│  • HKSV   : continuous fragmented-MP4 prebuffer (12 s ring) │
│             → recording delegate streams init + 4 s         │
│               fragments to the home hub on motion           │
└─────────────────────────────────────────────────────────────┘
```

The video is encoded **once**, in hardware, on the camera. Everything downstream (live stream, recordings, snapshots) reuses that same H264 stream without re-encoding — that's why it stays fluid and light.

## Configuration

Everything lives in one file: [`config.yaml`](config.yaml). After editing, re-run `sudo bash install.sh` (or copy it to `/opt/pi4cam/config.yaml` and restart the services).

| Key | Default | Description |
|---|---|---|
| `camera.width` × `height` | 1920×1080 | Capture / stream / recording resolution |
| `camera.fps` | 30 | Frame rate |
| `camera.bitrate` | 4000000 | H264 bitrate (bit/s) |
| `camera.rotation` | 0 | 0 / 90 / 180 / 270 |
| `homekit.camera_name` | Pi Camera | Name shown in the Home app |
| `homekit.motion_timeout` | 10 | Seconds the motion sensor stays active |
| `detection.min_motion_area` | 1500 | Motion sensitivity (smaller = more sensitive) |
| `detection.cooldown` | 30 | Seconds between two motion triggers |
| `detection.require_person` | false | Optional local person filter (MobileNet-SSD) before triggering |

The pairing secrets (PIN, setup ID, accessory MAC) are generated once by the installer into `/opt/pi4cam/homekit/pairing.json` and survive re-installs — updating the code never requires re-pairing.

## Troubleshooting

```bash
journalctl -u pi4cam -f            # camera pipeline + motion detection
journalctl -u pi4cam-homekit -f    # HomeKit app (pairing QR, streams, HKSV)
journalctl -u mediamtx -f          # RTSP server
```

- **Camera not found when pairing** — both the iPhone and the Pi must be on the same network; check that `avahi-daemon` is running (mDNS).
- **"Recording Options" missing in the Home app** — the accessory's capabilities are cached at pairing time. Remove the camera from the Home app and pair it again.
- **First snapshot is slow** — normal: with a keyframe every 4 s, the first JPEG can take a few seconds. It's cached afterwards.
- **Check the raw stream** — `ffprobe rtsp://<pi-ip>:8554/camera` should show `h264, 1920x1080`.

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
