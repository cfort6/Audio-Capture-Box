#!/usr/bin/env bash
# Audio Capture Box installer for Raspberry Pi OS (Lite, 64-bit).
# Run from the repo folder as your normal user:   ./install.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="${SUDO_USER:-$USER}"
SERVICE=audio-capture-box

if [ "$USER_NAME" = "root" ]; then
  echo "Please run this as your normal user (not with sudo):  ./install.sh"
  exit 1
fi

echo "==> Installing packages"
sudo apt-get update
sudo apt-get install -y python3-pygame python3-numpy python3-soundfile python3-evdev libportaudio2 libsndfile1 \
  fonts-dejavu-core udisks2 network-manager alsa-utils
if ! sudo apt-get install -y python3-sounddevice; then
  echo "==> python3-sounddevice not in apt, installing with pip"
  sudo apt-get install -y python3-pip python3-cffi
  python3 -m pip install --user --break-system-packages sounddevice
fi

echo "==> Adding $USER_NAME to the audio, video, input and storage groups"
for g in audio video input render plugdev netdev; do
  if getent group "$g" >/dev/null; then sudo usermod -aG "$g" "$USER_NAME"; fi
done

echo "==> Allowing the recorder to mount USB drives and change Wi-Fi"
sudo mkdir -p /etc/polkit-1/rules.d
sudo tee /etc/polkit-1/rules.d/50-audio-capture-box.rules >/dev/null <<EOF
// Audio Capture Box: let the recorder user mount USB drives and manage Wi-Fi.
polkit.addRule(function(action, subject) {
  if (subject.user == "$USER_NAME" &&
      (action.id.indexOf("org.freedesktop.udisks2.filesystem-mount") == 0 ||
       action.id.indexOf("org.freedesktop.NetworkManager.") == 0)) {
    return polkit.Result.YES;
  }
});
EOF
if [ -d /etc/polkit-1/localauthority ]; then
  sudo mkdir -p /etc/polkit-1/localauthority/50-local.d
  sudo tee /etc/polkit-1/localauthority/50-local.d/50-audio-capture-box.pkla >/dev/null <<EOF
[Audio Capture Box]
Identity=unix-user:$USER_NAME
Action=org.freedesktop.udisks2.filesystem-mount*;org.freedesktop.NetworkManager.*
ResultAny=yes
ResultInactive=yes
ResultActive=yes
EOF
fi

echo "==> Trimming startup so the recorder is ready sooner"
# Wi-Fi still connects as normal; startup just doesn't wait for it. cloud-init only
# matters on the very first boot (it applies Raspberry Pi Imager's settings).
sudo systemctl disable NetworkManager-wait-online.service e2scrub_reap.service >/dev/null 2>&1 || true
if [ -d /etc/cloud ]; then sudo touch /etc/cloud/cloud-init.disabled; fi

echo "==> Creating the recordings folder"
mkdir -p "/home/$USER_NAME/recordings"

echo "==> Installing the startup service"
sudo tee /etc/systemd/system/$SERVICE.service >/dev/null <<EOF
[Unit]
Description=Audio Capture Box recorder
After=systemd-user-sessions.service sound.target NetworkManager.service plymouth-quit.service
Conflicts=getty@tty1.service

[Service]
User=$USER_NAME
SupplementaryGroups=audio video input render plugdev netdev
WorkingDirectory=$APP_DIR
Environment=ACB_DISPLAY=auto
ExecStartPre=-+/bin/sh -c 'TERM=linux setterm --cursor off --blank 0 --powersave off > /dev/tty1 < /dev/tty1; printf "\\033[2J" > /dev/tty1'
ExecStopPost=-+/bin/sh -c 'TERM=linux setterm --cursor on > /dev/tty1 < /dev/tty1'
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -m acb
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable $SERVICE

echo
echo "Done. Starting the recorder now (it will also start on every boot)."
sudo systemctl restart $SERVICE
echo "Check status with:   systemctl status $SERVICE"
echo "See errors with:     journalctl -u $SERVICE -e"
