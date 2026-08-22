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

echo
echo "== Done. Start / check the service with: =="
echo "   sudo systemctl start  $SVC_NAME"
echo "   sudo systemctl status $SVC_NAME"
echo "   sudo journalctl -u $SVC_NAME -f"
echo
echo "Web UI will be available at:  http://<pi-ip>:8000"
