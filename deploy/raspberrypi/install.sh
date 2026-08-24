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
# Optional SMB file share (Windows "Netzwerkfreigabe"): set INSTALL_SAMBA=1 to
# install Samba and publish the recordings folder as a read/write share named
# "aufnahmen". macOS can use it as a guest; Windows 11 blocks guest SMB by
# default, so the installer also creates an authenticated Samba user (prompted,
# or via RADIOTIMER_SMB_PASSWORD) that Windows connects with. Leave the password
# empty for a guest-only share. Useful to listen to and manage recordings from
# Windows Explorer:
#   sudo INSTALL_SAMBA=1 bash /home/pi/radiotimer/deploy/raspberrypi/install.sh
#
# NOTE: the variable must come AFTER "sudo", not before it. "INSTALL_SAMBA=1
# sudo bash ..." sets it only on the sudo process, which sudo then strips from
# the script's environment, so the Samba block would be silently skipped.
# (Same rule for RADIOTIMER_SMB_PASSWORD etc.)
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
    # Point the default paths at the real service user's home instead of the
    # hard-coded /home/pi, so a differently named user (e.g. "radiotimer")
    # still gets sensible locations.
    SVC_HOME="$(getent passwd "$SVC_USER" | cut -d: -f6)"
    if [ -n "$SVC_HOME" ]; then
        sed -i "s#/home/pi/#$SVC_HOME/#g" "$SCRIPT_DIR/.env"
    fi
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

# Optional: Samba share so the recordings are reachable from Windows as a
# guest, read/write file share (no password). Guest connections are mapped to
# the service user so they can write/delete within the output directory.
# Enable via env var AFTER sudo (sudo INSTALL_SAMBA=1 bash ...) OR a positional
# arg (sudo bash install.sh samba) -- the arg form survives sudo's env stripping.
if [ "${1:-}" = "samba" ]; then
    INSTALL_SAMBA=1
fi
if [ "${INSTALL_SAMBA:-0}" = "1" ]; then
    echo "== Installing Samba file share =="
    sudo apt-get install -y samba

    # Guest access requires the global "map to guest" setting.
    if ! sudo grep -q "^[[:space:]]*map to guest" /etc/samba/smb.conf; then
        sudo sed -i '/^\[global\]/a\   map to guest = bad user' /etc/samba/smb.conf
    fi
    # Windows 11 requires SMB signing; enforce it server-side so guests and
    # authenticated clients negotiate a signed session.
    if ! sudo grep -q "^[[:space:]]*server signing" /etc/samba/smb.conf; then
        sudo sed -i '/^\[global\]/a\   server signing = mandatory' /etc/samba/smb.conf
    fi

    # Drop our share definition into an included snippets directory so we never
    # clobber an existing smb.conf. Note: Samba's "include" does NOT support
    # globs, so we must name the exact file.
    #
    # Windows clients (10/11) block anonymous/guest SMB logons by default, so
    # connecting to this guest share prompts for credentials that cannot be
    # skipped. Fix on the client: enable "insecure guest logons" (gpedit ->
    # Lanman Workstation, or registry AllowInsecureGuestAuth=1), or map with
    # `net use <letter>: \\<host>\aufnahmen /user:guest ""`.
    sudo mkdir -p /etc/samba/smb.conf.d
    if ! sudo grep -q "include = /etc/samba/smb.conf.d/aufnahmen.conf" /etc/samba/smb.conf; then
        printf '\ninclude = /etc/samba/smb.conf.d/aufnahmen.conf\n' | sudo tee -a /etc/samba/smb.conf >/dev/null
    fi

    # Create an authenticated Samba user so Windows 11 can connect without
    # relaxing its guest policy. Set the password via RADIOTIMER_SMB_PASSWORD
    # or you will be prompted. Leaving it empty yields a guest-only share.
    GUEST_ONLY=no
    SMBPW="${RADIOTIMER_SMB_PASSWORD:-}"
    if [ -z "$SMBPW" ]; then
        read -s -p "Samba password for '$SVC_USER' (leave empty for guest-only): " SMBPW || true
        echo
    fi
    if [ -n "$SMBPW" ]; then
        if ! echo -e "$SMBPW\n$SMBPW" | sudo smbpasswd -a -s "$SVC_USER" 2>/dev/null; then
            echo -e "$SMBPW\n$SMBPW" | sudo smbpasswd -s "$SVC_USER" 2>/dev/null || \
                echo "WARN: could not set Samba password for $SVC_USER"
        fi
    else
        GUEST_ONLY=yes
    fi

    OUTDIR="${RADIOTIMER_OUTPUT:-/home/pi/recordings}"
    SHARE=$(mktemp)
    cat > "$SHARE" <<EOF
[aufnahmen]
   comment = RadioTimer Aufnahmen
   path = $OUTDIR
   browseable = yes
   read only = no
   guest ok = yes
   guest only = $GUEST_ONLY
   force user = $SVC_USER
   force group = $SVC_USER
   create mask = 0644
   directory mask = 0755
EOF
    sudo cp "$SHARE" /etc/samba/smb.conf.d/aufnahmen.conf
    rm -f "$SHARE"

    # The service user must own the share so guest writes (force user) succeed.
    sudo mkdir -p "$OUTDIR"
    sudo chown -R "$SVC_USER":"$SVC_USER" "$OUTDIR"
    # Samba checks filesystem perms on every ancestor of the share path, so the
    # guest (mapped to nobody) needs +x on each parent to reach the folder.
    p="$OUTDIR"
    while [ "$p" != "/" ]; do
        sudo chmod o+x "$p"
        p="$(dirname "$p")"
    done

    if ! sudo testparm -s >/dev/null; then
        echo "WARN: smb.conf has validation issues - review with 'sudo testparm -s'"
    fi
    sudo systemctl enable smbd
    sudo systemctl restart smbd
    sudo systemctl enable nmbd 2>/dev/null && sudo systemctl restart nmbd 2>/dev/null || true
    if [ "$GUEST_ONLY" = "yes" ]; then
        echo "SMB share 'aufnahmen' is available at  \\\\<pi-host>\\aufnahmen  (guest, read/write)"
    else
        echo "SMB share 'aufnahmen' is available at  \\\\<pi-host>\\aufnahmen"
        echo "  - macOS: guest, no credentials needed"
        echo "  - Windows 11: connect as user '$SVC_USER' (Win11 blocks guest SMB)"
    fi
fi

echo
echo "== Done. Start / check the service with: =="
echo "   sudo systemctl start  $SVC_NAME"
echo "   sudo systemctl status $SVC_NAME"
echo "   sudo journalctl -u $SVC_NAME -f"
echo
echo "Web UI will be available at:  http://<pi-ip>:8000"
