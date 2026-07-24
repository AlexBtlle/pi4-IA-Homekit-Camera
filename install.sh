#!/usr/bin/env bash
# Installation script for pi4-IA-Homekit-Camera
# Must be run as root: sudo bash install.sh
set -euo pipefail

INSTALL_DIR="/opt/pi4cam"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-pi}"

MEDIAMTX_VERSION_OVERRIDE="${MEDIAMTX_VERSION:-}"

# -----------------------------------------------------------------------
# Helpers — defined before any use (the arch check below calls fatal)
# -----------------------------------------------------------------------
info()  { echo "==> $*"; }
fatal() { echo "ERROR: $*" >&2; exit 1; }

case "$(uname -m)" in
    aarch64) MEDIAMTX_ARCH="linux_arm64v8" ;;
    armv7l)  MEDIAMTX_ARCH="linux_arm7"    ;;
    armv6l)  MEDIAMTX_ARCH="linux_armv6"   ;;  # Pi Zero W / Pi 1 (ARMv6)
    x86_64)  MEDIAMTX_ARCH="linux_amd64"   ;;
    *) fatal "Unsupported architecture: $(uname -m)" ;;
esac

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
    python3-yaml \
    ffmpeg \
    rpicam-apps \
    curl \
    ca-certificates \
    gnupg \
    lsb-release \
    jq \
    vmtouch \
    avahi-daemon

# EVERY Python dependency comes from apt (numpy, cv2, picamera2, yaml):
# prebuilt pip wheels are unreliable across Raspberry Pi OS / Python
# versions, and apt packages exist for every arch Raspbian builds — ARMv6
# included. No venv, no pip, no PyPI network dependency.

# -----------------------------------------------------------------------
# 2. Node.js 22 (for the HAP-NodeJS HomeKit app)
# -----------------------------------------------------------------------
# Pinned to Node 22 LTS: hap-nodejs targets active LTS lines. We install from
# the NodeSource apt repo, removing any stale repo file first so a previously
# configured node_24 repo can't override the node_22 pin (the exact trap that
# blocked v1: apt kept Node 24 because the old repo list still had priority).
NODE_MIN=18       # HAP-NodeJS runs on any active-ish LTS; 18 is the floor
NODE_MAJOR=22     # target when we control the install (NodeSource, arm64/armv7)
# `|| true`: with `set -euo pipefail`, a failing command substitution in an
# assignment aborts the whole script. When node isn't installed yet, the
# pipeline returns 127 — which previously killed install.sh right here.
CURRENT_NODE_MAJOR="$(node --version 2>/dev/null | sed 's/v\([0-9]*\).*/\1/' || true)"
if [[ -n "${CURRENT_NODE_MAJOR}" && "${CURRENT_NODE_MAJOR}" -ge "${NODE_MIN}" ]]; then
    info "Node.js $(node --version) already installed (>= ${NODE_MIN}), keeping it."
elif [[ "$(uname -m)" == "armv6l" ]]; then
    # NodeSource dropped ARMv6 long ago; Raspberry Pi OS ships a compatible
    # nodejs (18.x on Bookworm) in its own archive. This is what makes the
    # Pi Zero W (v1) installable at all.
    info "ARMv6: installing Node.js from apt (NodeSource has no ARMv6 build)..."
    apt-get install -y nodejs npm
    NEW_MAJOR="$(node --version 2>/dev/null | sed 's/v\([0-9]*\).*/\1/' || true)"
    [[ -n "${NEW_MAJOR}" && "${NEW_MAJOR}" -ge "${NODE_MIN}" ]] \
        || fatal "apt provides Node ${NEW_MAJOR:-none} (< ${NODE_MIN}). Install a newer Node manually."
else
    info "Installing Node.js ${NODE_MAJOR}.x (current: ${CURRENT_NODE_MAJOR:-none})..."
    rm -f /etc/apt/sources.list.d/nodesource.list
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
    apt-get install -y --allow-downgrades nodejs
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
# 3b. Lean static ffmpeg — instant live startup (#43/#49)
# -----------------------------------------------------------------------
# Debian's ffmpeg links ~150 shared libraries and costs 5-8 s just to START
# on a memory-pressured 512 MB Pi; the lean build starts in ~0.2 s (x61,
# field-measured). Prebuilt by CI (.github/workflows/ffmpeg-static.yml) from
# scripts/build-static-ffmpeg.sh and published as a release. Every failure
# path here is SOFT: no release yet, offline, non-arm64, bad checksum — the
# HomeKit app auto-detects the binary and falls back to the system ffmpeg,
# so the install always works. Users never compile anything.
FFSTATIC="${INSTALL_DIR}/bin/ffmpeg-static"
if [[ "$(uname -m)" != "aarch64" ]]; then
    info "Lean ffmpeg: prebuilt only for arm64 — using the system ffmpeg."
    info "  (optional: build it locally with scripts/build-static-ffmpeg.sh)"
elif [[ -x "${FFSTATIC}" ]]; then
    info "Lean ffmpeg already installed at ${FFSTATIC}, skipping."
    info "  (delete it and re-run install.sh to fetch a newer release)"
else
    info "Fetching the prebuilt lean ffmpeg (instant live startup)..."
    FF_TAG="$(curl -fsSL \
        "https://api.github.com/repos/AlexBtlle/pi4-IA-Homekit-Camera/releases" \
        | jq -r '[.[] | select(.tag_name | startswith("ffmpeg-static-"))][0].tag_name // empty' \
        || true)"
    if [[ -z "${FF_TAG}" ]]; then
        info "No ffmpeg-static release available — using the system ffmpeg (slower live startup)."
    else
        FF_URL="https://github.com/AlexBtlle/pi4-IA-Homekit-Camera/releases/download/${FF_TAG}"
        FF_TMP="$(mktemp -d)"
        if curl -fsSL "${FF_URL}/ffmpeg-static-arm64" -o "${FF_TMP}/ffmpeg-static" \
           && curl -fsSL "${FF_URL}/ffmpeg-static-arm64.sha256" -o "${FF_TMP}/sha256" \
           && [[ "$(sha256sum "${FF_TMP}/ffmpeg-static" | awk '{print $1}')" \
                 == "$(awk '{print $1}' "${FF_TMP}/sha256")" ]] \
           && chmod +x "${FF_TMP}/ffmpeg-static" \
           && "${FF_TMP}/ffmpeg-static" -version >/dev/null 2>&1; then
            mkdir -p "${INSTALL_DIR}/bin"
            install -m 755 "${FF_TMP}/ffmpeg-static" "${FFSTATIC}"
            info "Lean ffmpeg ${FF_TAG} installed — live sessions start in ~0.2 s."
        else
            info "Download or verification failed — using the system ffmpeg (slower live startup)."
        fi
        rm -rf "${FF_TMP}"
    fi
fi

# -----------------------------------------------------------------------
# 4. Project files
# -----------------------------------------------------------------------
info "Deploying project files to ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}"/{camera,homekit}

# Python sources (the camera pipeline + detection). Sources only: a plain
# `cp -r camera/.` also ships __pycache__, and a checkout that once held a
# now-deleted module keeps its stale .pyc there — which would land in
# /opt/pi4cam as dead code. Copy the .py files, drop any bytecode.
find "${SRC_DIR}/camera" -maxdepth 1 -name '*.py' -exec cp {} "${INSTALL_DIR}/camera/" \;
rm -rf "${INSTALL_DIR}/camera/__pycache__"

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
# 5. Python runtime — system python + apt packages only (no venv, no pip)
# -----------------------------------------------------------------------
# The venv + pip dance existed to install exactly ONE package (PyYAML) that
# apt ships as python3-yaml. Dropping it saves 1-2 min of install on a
# Zero 2 W (the pip self-upgrade alone was 30-60 s), removes the PyPI
# network dependency, frees ~70 MB of SD, and keeps every Python dependency
# ARMv6-buildable from the Raspbian archive.
if [[ -d "${INSTALL_DIR}/venv" ]]; then
    info "Removing the legacy virtualenv (Python deps now come from apt)..."
    rm -rf "${INSTALL_DIR}/venv"
fi

# -----------------------------------------------------------------------
# 6. HomeKit app: build + pairing secrets (unique MAC + PIN + setup ID)
# -----------------------------------------------------------------------
info "Building the HomeKit app (npm ci + tsc)..."
pushd "${INSTALL_DIR}/homekit" >/dev/null
# `|| true` inside the group: with pipefail, grep -v exits 1 when EVERY line
# was filtered (or npm printed nothing) — that must not abort the install.
if [[ -f package-lock.json ]]; then
    npm ci --no-audit --no-fund 2>&1 | { grep -v "npm warn deprecated" || true; }
else
    npm install --no-audit --no-fund 2>&1 | { grep -v "npm warn deprecated" || true; }
fi
npm run build
popd >/dev/null

# Pairing secrets — generated once, then preserved across re-runs so the
# camera keeps its identity (re-pairing not required after an update).
PAIRING="${INSTALL_DIR}/homekit/pairing.json"
if [[ ! -f "${PAIRING}" ]]; then
    info "Generating HomeKit pairing secrets..."
    # `secrets` (CSPRNG), not `random` (predictable Mersenne Twister) — these
    # ARE security material. HAP also rejects a list of trivial PINs outright
    # (the accessory would fail to publish): re-draw if one comes up.
    PIN="$(python3 -c "
import secrets
BANNED = {'000-00-000','111-11-111','222-22-222','333-33-333','444-44-444',
          '555-55-555','666-66-666','777-77-777','888-88-888','999-99-999',
          '123-45-678','876-54-321'}
while True:
    d = ''.join(str(secrets.randbelow(10)) for _ in range(8))
    pin = f'{d[:3]}-{d[3:5]}-{d[5:]}'
    if pin not in BANNED:
        print(pin); break
")"
    MAC="$(python3 -c "
import secrets
m = list(secrets.token_bytes(6))
m[0] = (m[0] & 0xFE) | 0x02   # locally administered, unicast
print(':'.join(f'{b:02X}' for b in m))
")"
    SETUP_ID="$(python3 -c "
import secrets, string
print(''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4)))
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
# The homekit dir holds the accessory's identity: pairing.json (PIN) and
# persist/ (HAP Ed25519 private key, written 644 by HAP-NodeJS). Strip world
# access on every run so existing installs get fixed too (#35).
chmod -R o-rwx "${INSTALL_DIR}/homekit"
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

# warm-cache timer: keeps ffmpeg's libraries in the page cache so live-view
# spawns skip the SD reload (soft vmtouch, pages remain evictable)
cp "${SRC_DIR}/pi4cam-warm.service" /etc/systemd/system/
cp "${SRC_DIR}/pi4cam-warm.timer" /etc/systemd/system/

systemctl daemon-reload
systemctl enable mediamtx pi4cam pi4cam-homekit
systemctl enable --now pi4cam-warm.timer
systemctl start pi4cam-warm.service || true
systemctl restart mediamtx pi4cam pi4cam-homekit

# -----------------------------------------------------------------------
# Done
# -----------------------------------------------------------------------
LOCAL_IP="$(hostname -I | awk '{print $1}')"

echo ""
echo "=========================================================="
echo "  Installation complete!"
echo ""
echo "  RTSP stream : rtsp://127.0.0.1:8554/camera (local to the Pi only)"
echo ""
echo "  ┌──────────────────────────────────┐"
echo "  │  HomeKit pairing PIN: ${PIN}   │"
echo "  └──────────────────────────────────┘"
echo ""
echo "  To pair the camera:"
echo "  1. Open this page on your iPhone or Mac:"
echo "       http://$(hostname).local:8080"
echo "       http://${LOCAL_IP}:8080"
echo "  2. Scan the QR code shown on the page"
echo "     (or 'More options…' → enter the PIN above)"
echo ""
echo "  To enable HKSV (iCloud recording):"
echo "  Home → camera settings → Recording Options"
echo "  → 'Stream & Allow Recording' + People / Animals activity"
echo ""
echo "  Logs:"
echo "    mediamtx       : journalctl -u mediamtx -f"
echo "    pi4cam         : journalctl -u pi4cam -f"
echo "    pi4cam-homekit : journalctl -u pi4cam-homekit -f"
echo "=========================================================="
