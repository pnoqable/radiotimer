#!/usr/bin/env bash
# Native Raspberry Pi deployment for RadioTimer (no Docker).
#
# Prerequisites (done on the Mac, not here):
#   1. Flash "Raspberry Pi OS Lite (Legacy, 32-bit)" to the SD card with
#      Raspberry Pi Imager. For a Pi 1 you MUST use the Legacy image.
#   2. In Imager's "gear" settings enable SSH and set user/password,
#      and optionally configure WiFi / LAN.
#   3. Boot the Pi, log in (or SSH), then copy this repo onto it, e.g.:
#        scp -r radiotimer pi@<pi-ip>:/home/pi/
#        # or:  git clone <url> /home/pi/radiotimer
#   4. Run this script as the deploy user (e.g. pi, with sudo rights):
#        sudo bash /home/pi/radiotimer/deploy/raspberrypi/install.sh
#
# Raspberry Pi 1 (ARMv6, 256/512 MB RAM) notes:
#   * pip will COMPILE pendulum and aiohttp from source (no armv6 wheels),
#     which needs more RAM than the Pi 1 has. Increase swap first, e.g.:
#       sudo dphys-swapfile swapoff
#       sudo sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=1024/' /etc/dphys-swapfile
#       sudo dphys-swapfile setup && sudo dphys-swapfile swapon
#     (reset afterwards with CONF_SWAPSIZE=100 and re-run setup, or reboot).
#   * Keep RADIOTIMER_REENCODE OFF -- a Pi 1 cannot re-encode in real time.
#   * The install (especially the compile step) takes a long time on a
#     single-core Pi 1; be patient.
#
# Optional nginx reverse proxy (port 80): set INSTALL_NGINX=1 to also install
# nginx, drop in the bundled site config (nginx-site.conf), and point podcast
# enclosure URLs at the public host:
#   INSTALL_NGINX=1 sudo bash /home/pi/radiotimer/deploy/raspberrypi/install.sh
#
# The script installs system packages, creates a venv, installs the
# Python dependencies and registers a systemd service that auto-starts
# the recorder on boot.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"          # repo root
RS_DIR="$APP_DIR/recording-service"
SVC_USER="${SUDO_USER:-pi}"
SVC_NAME="radiotimer"

echo "== RadioTimer native installer =="
echo "Repo root : $APP_DIR"
echo "Service   : $SVC_NAME (user: $SVC_USER)"
echo

echo "== Updating system packages =="
sudo apt-get update
sudo apt-get full-upgrade -y

echo "== Installing system dependencies =="
sudo apt-get install -y \
    python3 python3-venv python3-pip \
    build-essential python3-dev \
    ffmpeg git

echo "== Creating virtualenv and installing Python packages =="
cd "$RS_DIR"
python3 -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
deactivate

# Create .env from example if it does not exist yet.
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    echo "Created $SCRIPT_DIR/.env from example - adjust RADIOTIMER_OUTPUT / RADIOTIMER_TZ if needed."
fi

# Make sure the output directory exists.
# shellcheck disable=SC1091
source "$SCRIPT_DIR/.env"
mkdir -p "${RADIOTIMER_OUTPUT:-/home/pi/recordings}"

echo "== Installing systemd service ($SVC_NAME) =="
# Generate the unit file with the actual paths so it works regardless
# of where the repo was placed.
UNIT=$(mktemp)
cat > "$UNIT" <<EOF
[Unit]
Description=RadioTimer recording service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SVC_USER
WorkingDirectory=$RS_DIR
EnvironmentFile=$SCRIPT_DIR/.env
ExecStart=$RS_DIR/venv/bin/python main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo cp "$UNIT" "/etc/systemd/system/$SVC_NAME.service"
rm -f "$UNIT"
sudo systemctl daemon-reload
sudo systemctl enable "$SVC_NAME.service"

# Optional: nginx reverse proxy so the UI/API is reachable on port 80 while
# the service stays on 8000.
if [ "${INSTALL_NGINX:-0}" = "1" ]; then
    echo "== Installing nginx reverse proxy =="
    sudo apt-get install -y nginx
    sudo cp "$SCRIPT_DIR/nginx-site.conf" /etc/nginx/sites-available/$SVC_NAME
    sudo ln -sf /etc/nginx/sites-available/$SVC_NAME /etc/nginx/sites-enabled/$SVC_NAME
    sudo rm -f /etc/nginx/sites-enabled/default
    sudo nginx -t
    sudo systemctl enable nginx
    sudo systemctl restart nginx
    # Point podcast enclosure URLs at the public host clients use.
    if ! grep -q '^RADIOTIMER_PUBLIC_URL=' "$SCRIPT_DIR/.env"; then
        echo "RADIOTIMER_PUBLIC_URL=http://radiotimer.local" >> "$SCRIPT_DIR/.env"
        sudo systemctl restart "$SVC_NAME.service"
    fi
fi

echo
echo "== Done. Start / check the service with: =="
echo "   sudo systemctl start  $SVC_NAME"
echo "   sudo systemctl status $SVC_NAME"
echo "   sudo journalctl -u $SVC_NAME -f"
echo
echo "Web UI will be available at:  http://<pi-ip>:8000"
