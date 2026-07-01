#!/usr/bin/env bash
# Installation script for pi4-IA-Homekit-Camera
# Must be run as root: sudo bash install.sh
set -euo pipefail

INSTALL_DIR="/opt/pi4cam"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-pi}"

MEDIAMTX_VERSION_OVERRIDE="${MEDIAMTX_VERSION:-}"

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
# Heal a half-removed NodeSource repo first: if the apt source points at a
# keyring that no longer exists (left over from a previous uninstall), every
# `apt-get update` fails for ALL repos. Read the keyring path the source
# actually references (signed-by=...) and, if it's gone, drop the stale
# source — step 2 re-adds it cleanly with a fresh keyring.
for ns_src in /etc/apt/sources.list.d/nodesource.list \
              /etc/apt/sources.list.d/nodesource.sources; do
    [[ -f "${ns_src}" ]] || continue
    ns_keyring="$(grep -hoE 'signed-by=[^] ]+' "${ns_src}" 2>/dev/null \
        | head -1 | cut -d= -f2 || true)"
    if [[ -n "${ns_keyring}" && ! -f "${ns_keyring}" ]]; then
        info "Removing stale NodeSource source ${ns_src} (keyring ${ns_keyring} missing)..."
        rm -f "${ns_src}"
    fi
done

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
# them via --system-site-packages.

# -----------------------------------------------------------------------
# 2. Node.js 22 (for the HAP-NodeJS HomeKit app)
# -----------------------------------------------------------------------
# Pinned to Node 22 LTS: hap-nodejs targets active LTS lines. We install from
# the NodeSource apt repo, removing any stale repo file first so a previously
# configured node_24 repo can't override the node_22 pin (the exact trap that
# blocked v1: apt kept Node 24 because the old repo list still had priority).
NODE_MAJOR=22
# `|| true`: with `set -euo pipefail`, a failing command substitution in an
# assignment aborts the whole script. When node isn't installed yet, the
# pipeline returns 127 — which previously killed install.sh right here.
CURRENT_NODE_MAJOR="$(node --version 2>/dev/null | sed 's/v\([0-9]*\).*/\1/' || true)"
if [[ "${CURRENT_NODE_MAJOR}" != "${NODE_MAJOR}" ]]; then
    info "Installing Node.js ${NODE_MAJOR}.x (current: ${CURRENT_NODE_MAJOR:-none})..."
    rm -f /etc/apt/sources.list.d/nodesource.list
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
    apt-get install -y --allow-downgrades nodejs
else
    info "Node.js $(node --version) already installed, skipping."
fi

# -----------------------------------------------------------------------
# 3. mediamtx
# -----------------------------------------------------------------------
if [[ ! -f /usr/local/bin/mediamtx ]]; then
    if [[ -z "${MEDIAMTX_VERSION_OVERRIDE}" ]]; then
        info "Fetching latest mediamtx version from GitHub..."
        MEDIAMTX_VERSION="$(curl -fsSL \
            "https://api.github.com/repos/bluenviron/mediamtx/releases/latest" \
            | jq -r '.tag_name // empty' || true)"
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
# 4. Project files
# -----------------------------------------------------------------------
info "Deploying project files to ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}"/{camera,homekit}

# Python sources (the camera pipeline + detection)
cp -r "${SRC_DIR}/camera/." "${INSTALL_DIR}/camera/"

# HomeKit app sources (build happens in step 8, below). Copy package files and
# the TypeScript sources; node_modules/dist/pairing.json are produced on-box.
cp "${SRC_DIR}/homekit/package.json" \
   "${SRC_DIR}/homekit/package-lock.json" \
   "${SRC_DIR}/homekit/tsconfig.json" \
   "${INSTALL_DIR}/homekit/"
cp -r "${SRC_DIR}/homekit/src" "${INSTALL_DIR}/homekit/"

# mediamtx config — only copy if not already customised
if [[ ! -f "${INSTALL_DIR}/mediamtx.yml" ]]; then
    cp "${SRC_DIR}/mediamtx.yml" "${INSTALL_DIR}/"
fi

# Always write the annotated reference config so users can diff after updates
cp "${SRC_DIR}/config.yaml" "${INSTALL_DIR}/config.yaml.dist"

if [[ ! -f "${INSTALL_DIR}/config.yaml" ]]; then
    cp "${SRC_DIR}/config.yaml" "${INSTALL_DIR}/"
    info "config.yaml installed."
else
    # Preserve user values; inject any new keys introduced by this version.
    # Logic lives in scripts/config_merge.py (single source of truth, tested).
    python3 "${SRC_DIR}/scripts/config_merge.py" \
        "${INSTALL_DIR}/config.yaml" "${SRC_DIR}/config.yaml"
    info "config.yaml updated: new keys added, your customisations preserved."
    info "Annotated reference: ${INSTALL_DIR}/config.yaml.dist"
fi

# -----------------------------------------------------------------------
# 5. Python virtual environment
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
# 6. HomeKit app: build + pairing secrets (unique MAC + PIN + setup ID)
# -----------------------------------------------------------------------
info "Building the HomeKit app (npm ci + tsc)..."
pushd "${INSTALL_DIR}/homekit" >/dev/null
if [[ -f package-lock.json ]]; then
    npm ci --no-audit --no-fund 2>&1 | grep -v "npm warn deprecated"
else
    npm install --no-audit --no-fund 2>&1 | grep -v "npm warn deprecated"
fi
npm run build
popd >/dev/null

# Pairing secrets — generated once, then preserved across re-runs so the
# camera keeps its identity (re-pairing not required after an update).
PAIRING="${INSTALL_DIR}/homekit/pairing.json"
if [[ ! -f "${PAIRING}" ]]; then
    info "Generating HomeKit pairing secrets..."
    PIN="$(python3 -c "
import random
d = [random.randint(0,9) for _ in range(8)]
print(f\"{''.join(map(str,d[:3]))}-{''.join(map(str,d[3:5]))}-{''.join(map(str,d[5:]))}\")
")"
    MAC="$(python3 -c "
import random
m = [random.randint(0,255) for _ in range(6)]
m[0] = (m[0] & 0xFE) | 0x02   # locally administered, unicast
print(':'.join(f'{b:02X}' for b in m))
")"
    SETUP_ID="$(python3 -c "
import random, string
print(''.join(random.choices(string.ascii_uppercase + string.digits, k=4)))
")"
    cat > "${PAIRING}" <<EOF
{
  "username": "${MAC}",
  "pincode": "${PIN}",
  "setupID": "${SETUP_ID}"
}
EOF
    chmod 600 "${PAIRING}"
    info "Pairing secrets written (PIN: ${PIN})"
else
    info "Pairing secrets already exist, preserving them."
    PIN="$(python3 -c "import json; print(json.load(open('${PAIRING}'))['pincode'])")"
fi

chown -R "${RUN_USER}:${RUN_USER}" "${INSTALL_DIR}"
usermod -aG video "${RUN_USER}" || true

# -----------------------------------------------------------------------
# 7. systemd services
# -----------------------------------------------------------------------
info "Installing systemd services..."

# mediamtx
sed "s/__USER__/${RUN_USER}/" "${SRC_DIR}/mediamtx.service" \
    > /etc/systemd/system/mediamtx.service

# pi4cam Python service (camera pipeline + detection)
sed "s/__USER__/${RUN_USER}/" "${SRC_DIR}/pi4cam.service" \
    > /etc/systemd/system/pi4cam.service

# pi4cam HomeKit app (HAP-NodeJS)
sed "s/__USER__/${RUN_USER}/" "${SRC_DIR}/pi4cam-homekit.service" \
    > /etc/systemd/system/pi4cam-homekit.service

systemctl daemon-reload
systemctl enable mediamtx pi4cam pi4cam-homekit
systemctl restart mediamtx pi4cam pi4cam-homekit

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
echo "  1. Ouvre cette page depuis ton iPhone ou Mac :"
echo "       http://\$(hostname).local:8080"
echo "       http://\${LOCAL_IP}:8080"
echo "  2. Scanne le QR code affiché sur la page"
echo "     (ou 'Plus d'options…' → saisis le PIN ci-dessus)"
echo ""
echo "  Pour activer HKSV (enregistrement iCloud) :"
echo "  Maison → réglages caméra → Options d'enregistrement"
echo "  → 'Enregistrer le flux' + activité Personnes / Animaux"
echo ""
echo "  Logs :"
echo "    mediamtx       : journalctl -u mediamtx -f"
echo "    pi4cam         : journalctl -u pi4cam -f"
echo "    pi4cam-homekit : journalctl -u pi4cam-homekit -f"
echo "=========================================================="
