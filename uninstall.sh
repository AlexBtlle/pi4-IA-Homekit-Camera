#!/usr/bin/env bash
# Désinstallation complète de pi4-IA-Homekit-Camera
# sudo bash uninstall.sh
set -euo pipefail

[[ "$EUID" -eq 0 ]] || { echo "Run as root: sudo bash $0" >&2; exit 1; }

echo "==> Arrêt et désactivation des services..."
for svc in pi4cam-homekit homebridge pi4cam mediamtx pi4cam-warm.timer pi4cam-warm.service; do
    systemctl stop    "$svc" 2>/dev/null || true
    systemctl disable "$svc" 2>/dev/null || true
done

echo "==> Suppression des fichiers de service systemd..."
rm -f /etc/systemd/system/pi4cam-homekit.service
rm -f /etc/systemd/system/homebridge.service
rm -f /etc/systemd/system/pi4cam.service
rm -f /etc/systemd/system/mediamtx.service
rm -f /etc/systemd/system/pi4cam-warm.service
rm -f /etc/systemd/system/pi4cam-warm.timer
systemctl daemon-reload

echo "==> Suppression des fichiers de l'application..."
rm -rf /opt/pi4cam

echo "==> Désinstallation de homebridge (héritage v1, si présent)..."
npm uninstall -g homebridge homebridge-camera-ffmpeg 2>/dev/null || true

echo "==> Suppression de mediamtx..."
rm -f /usr/local/bin/mediamtx

echo "==> Suppression des dépôts Node.js (nodesource)..."
rm -f /etc/apt/sources.list.d/nodesource.list
rm -f /etc/apt/sources.list.d/nodesource.list.distUpgrade
rm -f /etc/apt/keyrings/nodesource.gpg
rm -f /usr/share/keyrings/nodesource.gpg

echo "==> Suppression de Node.js..."
apt-get remove -y --purge nodejs 2>/dev/null || true
apt-get autoremove -y 2>/dev/null || true

echo ""
echo "=========================================================="
echo "  Désinstallation terminée."
echo ""
echo "  Paquets système conservés (apt):"
echo "    python3-picamera2, python3-opencv, ffmpeg, avahi-daemon"
echo "  (ils ne posent aucun problème — lance apt autoremove si"
echo "   tu veux les retirer aussi)"
echo "=========================================================="
