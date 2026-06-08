#!/usr/bin/env bash
# Installation script for pi4-IA-Homekit-Camera
# Must be run as root: sudo bash install.sh
set -euo pipefail

INSTALL_DIR="/opt/pi4cam"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-pi}"

MEDIAMTX_VERSION_OVERRIDE="${MEDIAMTX_VERSION:-}"
# MobileNet-SSD (Caffe) for OpenCV DNN person detection — committed directly
# in this repo (not behind a Google Drive link), so it is curl-able and stable.
DNN_BASE_URL="https://raw.githubusercontent.com/djmv/MobilNet_SSD_opencv/master"
DNN_PROTOTXT_URL="${DNN_BASE_URL}/MobileNetSSD_deploy.prototxt"
DNN_CAFFEMODEL_URL="${DNN_BASE_URL}/MobileNetSSD_deploy.caffemodel"

case "$(uname -m)" in
    aarch64) MEDIAMTX_ARCH="linux_arm64v8" ;;
    armv7l)  MEDIAMTX_ARCH="linux_arm7"    ;;
    x86_64)  MEDIAMTX_ARCH="linux_amd64"   ;;
    *) fatal "Unsupported architecture: $(uname -m)" ;;
esac

# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------
info()  { echo "==> $*"; }
fatal() { echo "ERROR: $*" >&2; exit 1; }

[[ "$EUID" -eq 0 ]] || fatal "Run as root: sudo bash $0"

# -----------------------------------------------------------------------
# 1. System packages
# -----------------------------------------------------------------------
info "Installing system packages..."
apt-get update -qq
apt-get install -y \
    python3-picamera2 \
    python3-libcamera \
    python3-numpy \
    python3-opencv \
    python3-venv \
    ffmpeg \
    rpicam-apps \
    curl \
    ca-certificates \
    gnupg \
    lsb-release \
    jq \
    avahi-daemon

# numpy and opencv (cv2) come from apt above — prebuilt pip wheels are
# unreliable across Raspberry Pi OS / Python versions. The venv accesses
# them via --system-site-packages. No fragile ML runtime (tflite) needed:
# person detection runs through OpenCV's built-in DNN module.

# -----------------------------------------------------------------------
# 2. Node.js LTS (for homebridge)
# -----------------------------------------------------------------------
if ! command -v node &>/dev/null; then
    info "Installing Node.js LTS..."
    curl -fsSL https://deb.nodesource.com/setup_lts.x | bash -
    apt-get install -y nodejs
else
    info "Node.js $(node --version) already installed, skipping."
fi

# -----------------------------------------------------------------------
# 3. homebridge + homebridge-camera-ffmpeg
# -----------------------------------------------------------------------
# homebridge is pinned to the 1.8.x LTS line: homebridge-camera-ffmpeg's HKSV
# (HomeKit Secure Video) implementation was written against homebridge 1.x's
# recording-delegate API. homebridge 2.x changed that API and HKSV silently
# fails to register (the "Recording Options" menu never appears in Home).
HOMEBRIDGE_PKG="homebridge@^1.8.0"
if ! command -v homebridge &>/dev/null; then
    info "Installing homebridge (1.8.x) and homebridge-camera-ffmpeg..."
    npm install -g --unsafe-perm "${HOMEBRIDGE_PKG}" homebridge-camera-ffmpeg
else
    HB_MAJOR="$(homebridge --version 2>/dev/null | cut -d. -f1)"
    if [[ "${HB_MAJOR}" != "1" ]]; then
        info "homebridge ${HB_MAJOR}.x detected — pinning to 1.8.x for HKSV..."
        npm install -g --unsafe-perm "${HOMEBRIDGE_PKG}"
    else
        info "homebridge $(homebridge --version) already installed, skipping."
    fi
    npm list -g homebridge-camera-ffmpeg --depth=0 &>/dev/null || \
        npm install -g --unsafe-perm homebridge-camera-ffmpeg
fi

# -----------------------------------------------------------------------
# 4. mediamtx
# -----------------------------------------------------------------------
if [[ ! -f /usr/local/bin/mediamtx ]]; then
    if [[ -z "${MEDIAMTX_VERSION_OVERRIDE}" ]]; then
        info "Fetching latest mediamtx version from GitHub..."
        MEDIAMTX_VERSION="$(curl -fsSL \
            "https://api.github.com/repos/bluenviron/mediamtx/releases/latest" \
            | jq -r '.tag_name // empty')"
    else
        MEDIAMTX_VERSION="${MEDIAMTX_VERSION_OVERRIDE}"
    fi

    if [[ ! "${MEDIAMTX_VERSION:-}" =~ ^v[0-9]+\.[0-9]+ ]]; then
        fatal "Could not determine mediamtx version (got: '${MEDIAMTX_VERSION:-}').
       Set it manually: MEDIAMTX_VERSION=v1.19.0 sudo bash $0"
    fi

    TMP_DIR="$(mktemp -d)"
    trap 'rm -rf "${TMP_DIR}"' EXIT

    for ARCH_SUFFIX in "${MEDIAMTX_ARCH}" "${MEDIAMTX_ARCH/arm64v8/arm64}"; do
        DOWNLOAD_URL="https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_${ARCH_SUFFIX}.tar.gz"
        info "Trying: ${DOWNLOAD_URL}"
        if curl -fsSL "${DOWNLOAD_URL}" -o "${TMP_DIR}/mediamtx.tar.gz" 2>/dev/null; then
            break
        fi
        info "Not found, trying next arch variant..."
    done

    [[ -s "${TMP_DIR}/mediamtx.tar.gz" ]] || \
        fatal "Download failed for mediamtx ${MEDIAMTX_VERSION}.
       Try: MEDIAMTX_VERSION=v1.19.0 sudo bash $0"

    tar -xzf "${TMP_DIR}/mediamtx.tar.gz" -C "${TMP_DIR}"
    install -m 755 "${TMP_DIR}/mediamtx" /usr/local/bin/mediamtx
    info "mediamtx ${MEDIAMTX_VERSION} installed."
else
    info "mediamtx already installed at /usr/local/bin/mediamtx, skipping."
fi

# -----------------------------------------------------------------------
# 5. Project files
# -----------------------------------------------------------------------
info "Deploying project files to ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}"/{src,models,homebridge}

# Python sources
cp -r "${SRC_DIR}/src/." "${INSTALL_DIR}/src/"

# mediamtx config — only copy if not already customised
if [[ ! -f "${INSTALL_DIR}/mediamtx.yml" ]]; then
    cp "${SRC_DIR}/mediamtx.yml" "${INSTALL_DIR}/"
fi

# app config — only copy if not already present
if [[ ! -f "${INSTALL_DIR}/config.yaml" ]]; then
    cp "${SRC_DIR}/config.yaml" "${INSTALL_DIR}/"
fi

# -----------------------------------------------------------------------
# 6. MobileNet-SSD detection model (OpenCV DNN)
# -----------------------------------------------------------------------
PROTOTXT_PATH="${INSTALL_DIR}/models/MobileNetSSD_deploy.prototxt"
CAFFEMODEL_PATH="${INSTALL_DIR}/models/MobileNetSSD_deploy.caffemodel"
if [[ ! -f "${CAFFEMODEL_PATH}" ]]; then
    info "Downloading MobileNet-SSD detection model..."
    curl -fsSL "${DNN_PROTOTXT_URL}"   -o "${PROTOTXT_PATH}"
    curl -fsSL "${DNN_CAFFEMODEL_URL}" -o "${CAFFEMODEL_PATH}"
    # Sanity check: caffemodel must be a real binary (~23 MB), not an HTML 404
    if [[ ! -s "${CAFFEMODEL_PATH}" ]] || [[ "$(stat -c%s "${CAFFEMODEL_PATH}")" -lt 1000000 ]]; then
        rm -f "${CAFFEMODEL_PATH}"
        fatal "MobileNet-SSD model download failed or incomplete."
    fi
    info "MobileNet-SSD model installed."
else
    info "MobileNet-SSD model already present, skipping."
fi

# -----------------------------------------------------------------------
# 7. Python virtual environment
# -----------------------------------------------------------------------
VENV="${INSTALL_DIR}/venv"
if [[ ! -d "${VENV}" ]]; then
    info "Creating Python virtual environment..."
    # --system-site-packages gives access to apt-installed picamera2/libcamera
    python3 -m venv --system-site-packages "${VENV}"
fi
"${VENV}/bin/pip" install --quiet --upgrade pip
"${VENV}/bin/pip" install --quiet -r "${SRC_DIR}/requirements.txt"
info "Python dependencies installed."

# -----------------------------------------------------------------------
# 8. homebridge config.json (generated with unique MAC + PIN)
# -----------------------------------------------------------------------
HB_CONFIG="${INSTALL_DIR}/homebridge/config.json"
if [[ ! -f "${HB_CONFIG}" ]]; then
    info "Generating homebridge config..."

    # Random 6-byte MAC (locally administered, unicast)
    MAC="$(python3 -c "
import random
mac = [random.randint(0,255) for _ in range(6)]
mac[0] = (mac[0] & 0xFE) | 0x02   # locally administered, unicast
print(':'.join(f'{b:02X}' for b in mac))
")"

    # Valid HomeKit PIN in format XXX-XX-XXX (avoid 000-00-000 etc.)
    PIN="$(python3 -c "
import random
digits = [random.randint(0,9) for _ in range(8)]
print(f'{''.join(str(d) for d in digits[:3])}-{''.join(str(d) for d in digits[3:5])}-{''.join(str(d) for d in digits[5:])}')
")"

    sed -e "s/__MAC__/${MAC}/" -e "s/__PIN__/${PIN}/" \
        "${SRC_DIR}/homebridge/config.json" > "${HB_CONFIG}"
    info "homebridge config written (PIN: ${PIN})"
else
    info "homebridge config already exists, preserving it."
    # Extract PIN for display at the end
    PIN="$(python3 -c "import json,sys; print(json.load(open('${HB_CONFIG}'))['bridge']['pin'])")"
fi

chown -R "${RUN_USER}:${RUN_USER}" "${INSTALL_DIR}"
usermod -aG video "${RUN_USER}" || true

# -----------------------------------------------------------------------
# 9. systemd services
# -----------------------------------------------------------------------
info "Installing systemd services..."

# mediamtx
sed "s/__USER__/${RUN_USER}/" "${SRC_DIR}/mediamtx.service" \
    > /etc/systemd/system/mediamtx.service

# homebridge
cat > /etc/systemd/system/homebridge.service << EOF
[Unit]
Description=Homebridge HomeKit bridge (+ homebridge-camera-ffmpeg HKSV)
After=network-online.target avahi-daemon.service mediamtx.service pi4cam.service
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Environment=HOME=${INSTALL_DIR}/homebridge
ExecStart=$(which homebridge) -U ${INSTALL_DIR}/homebridge -I
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# pi4cam Python service
sed "s/__USER__/${RUN_USER}/" "${SRC_DIR}/pi4cam.service" \
    > /etc/systemd/system/pi4cam.service

systemctl daemon-reload
systemctl enable --now mediamtx
systemctl enable --now pi4cam
systemctl enable --now homebridge

# -----------------------------------------------------------------------
# Done
# -----------------------------------------------------------------------
LOCAL_IP="$(hostname -I | awk '{print $1}')"

echo ""
echo "=========================================================="
echo "  Installation complete!"
echo ""
echo "  RTSP stream : rtsp://${LOCAL_IP}:8554/camera"
echo ""
echo "  ┌──────────────────────────────────┐"
echo "  │  HomeKit pairing PIN: ${PIN}   │"
echo "  └──────────────────────────────────┘"
echo ""
echo "  Pour coupler la caméra :"
echo "  1. Ouvre l'app Maison sur iPhone"
echo "  2. Appuie sur + → Ajouter un accessoire"
echo "  3. Choisis 'Code sans QR code'"
echo "  4. Saisis le PIN affiché ci-dessus"
echo "     (ou scanne le QR : journalctl -u homebridge | head -50)"
echo ""
echo "  Pour activer HKSV :"
echo "  Maison → réglages caméra → Streaming et enregistrement"
echo "  → choisir 'Activité détectée'"
echo ""
echo "  Logs :"
echo "    mediamtx  : journalctl -u mediamtx -f"
echo "    pi4cam    : journalctl -u pi4cam -f"
echo "    homebridge: journalctl -u homebridge -f"
echo "=========================================================="
