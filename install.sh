#!/usr/bin/env bash
# Installation script for pi4-IA-Homekit-Camera
# Must be run as root: sudo bash install.sh
set -euo pipefail

INSTALL_DIR="/opt/pi4tohomekit"
SERVICE_NAME="pi4tohomekit"
MODEL_DIR="${INSTALL_DIR}/models"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Detect the user who invoked sudo (falls back to "pi")
RUN_USER="${SUDO_USER:-pi}"

# MobileNet SSD v1 quantised COCO — person is class index 0
MODEL_ZIP_URL="https://storage.googleapis.com/download.tensorflow.org/models/tflite/coco_ssd_mobilenet_v1_1.0_quant_2018_06_29.zip"

# -----------------------------------------------------------------------
# 1. System packages
# -----------------------------------------------------------------------
echo "==> Installing system packages..."
apt-get update -qq
apt-get install -y \
    python3-venv \
    python3-pip \
    python3-picamera2 \
    python3-libcamera \
    python3-kms++ \
    ffmpeg \
    rpicam-apps \
    avahi-daemon \
    libatlas-base-dev \
    libopenblas-dev \
    wget \
    unzip

# -----------------------------------------------------------------------
# 2. Copy project files
# -----------------------------------------------------------------------
echo "==> Copying project files to ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}" "${MODEL_DIR}"

cp -r "${SRC_DIR}/src"             "${INSTALL_DIR}/"
cp    "${SRC_DIR}/requirements.txt" "${INSTALL_DIR}/"

# Copy default config only if one doesn't already exist (preserve customisation)
if [[ ! -f "${INSTALL_DIR}/config.yaml" ]]; then
    cp "${SRC_DIR}/config.yaml" "${INSTALL_DIR}/"
fi

# -----------------------------------------------------------------------
# 3. Python virtual environment
# -----------------------------------------------------------------------
echo "==> Creating Python venv..."
# --system-site-packages is REQUIRED: picamera2 installed via apt uses
# C extensions (_libcamera) that cannot be pip-installed into an isolated venv.
python3 -m venv --system-site-packages "${INSTALL_DIR}/venv"

echo "==> Installing Python dependencies..."
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip --quiet
# picamera2 is deliberately NOT listed here (installed via apt above)
"${INSTALL_DIR}/venv/bin/pip" install --quiet \
    "HAP-python[QRCode]>=4.9.0,<6.0" \
    "PyYAML>=6.0" \
    "numpy>=1.24" \
    "opencv-python-headless>=4.8" \
    "tflite-runtime>=2.14.0" \
    "qrcode[pil]>=7.4"

# -----------------------------------------------------------------------
# 4. Download TFLite model
# -----------------------------------------------------------------------
if [[ ! -f "${MODEL_DIR}/detect.tflite" ]]; then
    echo "==> Downloading MobileNet SSD v1 TFLite model..."
    TMP_DIR="$(mktemp -d)"
    wget -q --show-progress -O "${TMP_DIR}/model.zip" "${MODEL_ZIP_URL}"
    unzip -q "${TMP_DIR}/model.zip" -d "${TMP_DIR}/"
    cp "${TMP_DIR}/detect.tflite"  "${MODEL_DIR}/detect.tflite"
    # labelmap.txt may or may not be in the zip depending on version
    cp "${TMP_DIR}/labelmap.txt"   "${MODEL_DIR}/labelmap.txt" 2>/dev/null || true
    rm -rf "${TMP_DIR}"
    echo "==> Model downloaded: ${MODEL_DIR}/detect.tflite"
else
    echo "==> TFLite model already present, skipping download."
fi

# -----------------------------------------------------------------------
# 5. Generate a random PIN if config has an empty pincode
# -----------------------------------------------------------------------
CONFIG_FILE="${INSTALL_DIR}/config.yaml"
CURRENT_PIN="$(grep 'pincode:' "${CONFIG_FILE}" | awk '{print $2}' | tr -d '"' | xargs)"
if [[ -z "${CURRENT_PIN}" ]]; then
    # Generate a random valid HomeKit PIN (format: XXX-XX-XXX, no 000-00-000/111-11-111 etc.)
    while true; do
        PIN="$(printf '%03d-%02d-%03d' $((RANDOM % 1000)) $((RANDOM % 100)) $((RANDOM % 1000)))"
        # Reject trivial sequences
        DIGITS="${PIN//[^0-9]/}"
        if [[ "${DIGITS}" =~ ^([0-9])\1+$ ]]; then continue; fi
        break
    done
    sed -i "s/pincode: \"\"/pincode: \"${PIN}\"/" "${CONFIG_FILE}"
    echo "==> HomeKit PIN generated: ${PIN}"
fi

# -----------------------------------------------------------------------
# 6. Permissions
# -----------------------------------------------------------------------
echo "==> Setting permissions..."
chown -R "${RUN_USER}:${RUN_USER}" "${INSTALL_DIR}"
usermod -aG video "${RUN_USER}" || true

# -----------------------------------------------------------------------
# 7. Systemd service
# -----------------------------------------------------------------------
echo "==> Installing systemd service..."
sed "s/__USER__/${RUN_USER}/" "${SRC_DIR}/${SERVICE_NAME}.service" \
    > "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"

echo ""
echo "=========================================="
echo "  Installation complete!"
echo "  Service: ${SERVICE_NAME}"
echo "  Status:  systemctl status ${SERVICE_NAME}"
echo "  Logs:    journalctl -u ${SERVICE_NAME} -f"
echo "=========================================="
