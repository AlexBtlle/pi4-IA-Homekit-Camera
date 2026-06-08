#!/usr/bin/env bash
# Installation script for pi4-IA-Homekit-Camera
# Must be run as root: sudo bash install.sh
set -euo pipefail

INSTALL_DIR="/opt/pi4cam"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-pi}"

MEDIAMTX_ARCH="linux_arm64v8"

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
    ffmpeg \
    rpicam-apps \
    curl \
    ca-certificates \
    gnupg \
    lsb-release \
    jq

# -----------------------------------------------------------------------
# 2. Docker
# -----------------------------------------------------------------------
if ! command -v docker &>/dev/null; then
    info "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    usermod -aG docker "${RUN_USER}"
    info "Docker installed. Note: log out and back in for group membership to take effect."
else
    info "Docker already installed, skipping."
fi

if ! command -v docker &>/dev/null; then
    fatal "Docker installation failed."
fi

# Install docker-compose plugin if not present
if ! docker compose version &>/dev/null 2>&1; then
    info "Installing docker-compose plugin..."
    apt-get install -y docker-compose-plugin
fi

# -----------------------------------------------------------------------
# 3. mediamtx
# -----------------------------------------------------------------------
if [[ ! -f /usr/local/bin/mediamtx ]]; then
    info "Downloading mediamtx..."
    MEDIAMTX_VERSION="$(curl -fsSL \
        https://api.github.com/repos/bluenviron/mediamtx/releases/latest \
        | jq -r '.tag_name')"
    [[ -n "${MEDIAMTX_VERSION}" ]] || fatal "Could not determine mediamtx version."

    TMP_DIR="$(mktemp -d)"
    ARCHIVE="${MEDIAMTX_VERSION}_${MEDIAMTX_ARCH}.tar.gz"
    curl -fsSL \
        "https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${ARCHIVE}" \
        -o "${TMP_DIR}/mediamtx.tar.gz"
    tar -xzf "${TMP_DIR}/mediamtx.tar.gz" -C "${TMP_DIR}"
    install -m 755 "${TMP_DIR}/mediamtx" /usr/local/bin/mediamtx
    rm -rf "${TMP_DIR}"
    info "mediamtx ${MEDIAMTX_VERSION} installed."
else
    info "mediamtx already installed, skipping."
fi

# -----------------------------------------------------------------------
# 4. Project files
# -----------------------------------------------------------------------
info "Copying project files to ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}"

# docker-compose.yml — always update
cp "${SRC_DIR}/docker-compose.yml" "${INSTALL_DIR}/"

# mediamtx.yml — only copy if not already customised
if [[ ! -f "${INSTALL_DIR}/mediamtx.yml" ]]; then
    cp "${SRC_DIR}/mediamtx.yml" "${INSTALL_DIR}/"
fi

chown -R "${RUN_USER}:${RUN_USER}" "${INSTALL_DIR}"
usermod -aG video "${RUN_USER}" || true

# -----------------------------------------------------------------------
# 5. mediamtx systemd service
# -----------------------------------------------------------------------
info "Installing mediamtx systemd service..."
sed "s/__USER__/${RUN_USER}/" "${SRC_DIR}/mediamtx.service" \
    > /etc/systemd/system/mediamtx.service
systemctl daemon-reload
systemctl enable --now mediamtx

# -----------------------------------------------------------------------
# 6. Scrypted (Docker)
# -----------------------------------------------------------------------
info "Starting Scrypted..."
cd "${INSTALL_DIR}"
docker compose pull --quiet
docker compose up -d

# Enable Scrypted to start on boot via Docker's own restart policy
systemctl enable docker

# -----------------------------------------------------------------------
# Done
# -----------------------------------------------------------------------
LOCAL_IP="$(hostname -I | awk '{print $1}')"

echo ""
echo "=========================================================="
echo "  Installation complete!"
echo ""
echo "  RTSP stream:  rtsp://${LOCAL_IP}:8554/camera"
echo "  Scrypted UI:  http://${LOCAL_IP}:11080"
echo ""
echo "  Next steps:"
echo "  1. Open Scrypted at the URL above"
echo "  2. Create an account (local only)"
echo "  3. Install plugins: HomeKit + OpenCV Object Detector"
echo "     (or TensorFlow Lite Object Detector)"
echo "  4. Add camera: Plugins → RTSP Camera"
echo "     URL: rtsp://localhost:8554/camera"
echo "  5. Enable HomeKit → camera appears in Apple Home"
echo "  6. Enable HKSV in the camera's HomeKit settings"
echo "  7. Configure person detection for smart notifications"
echo ""
echo "  Logs:"
echo "    mediamtx: journalctl -u mediamtx -f"
echo "    Scrypted:  docker logs -f scrypted"
echo "=========================================================="
