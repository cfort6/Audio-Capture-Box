#!/usr/bin/env bash
# Copy this fully set-up Pi onto a blank SD card in a USB card reader, ready to
# go into another Pi. The copy is cleaned up so it's a fresh unit:
#   - no recordings, Trash, or saved settings
#   - no saved GitHub login (~/.git-credentials) or shell history
#   - optionally no saved Wi-Fi networks
#   - gets its own name (recorder-XXXX), SSH keys and network ID on first boot
# This Pi (the "master") is not changed.
#
# Usage:   ./clone-to-card.sh            (asks which drive)
#          ./clone-to-card.sh sda        (card reader is /dev/sda)
set -euo pipefail

MASTER_USER="${SUDO_USER:-$USER}"
MNT=/mnt/acb-clone

# ---- 1. make sure rpi-clone is installed
if ! command -v rpi-clone >/dev/null; then
  echo "==> Installing rpi-clone"
  sudo apt-get install -y git rsync
  tmp=$(mktemp -d)
  git clone --depth 1 https://github.com/geerlingguy/rpi-clone.git "$tmp/rpi-clone"
  sudo cp "$tmp/rpi-clone/rpi-clone" "$tmp/rpi-clone/rpi-clone-setup" /usr/local/sbin/
  rm -rf "$tmp"
fi

# ---- 2. first-boot service (lives on the master too, but only runs where /etc/acb-firstboot exists)
sudo tee /usr/local/sbin/acb-firstboot.sh >/dev/null <<'EOF'
#!/bin/sh
# Runs once on a freshly cloned Audio Capture Box.
serial=$(awk '/^Serial/ {print $3}' /proc/cpuinfo | tail -c 5 | tr -d '\n')
[ -n "$serial" ] || serial=$(tr -dc 'a-f0-9' < /dev/urandom | head -c 4)
name="recorder-$serial"
echo "$name" > /etc/hostname
hostname "$name"
sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$name/" /etc/hosts
ssh-keygen -A
rm -f /etc/acb-firstboot
EOF
sudo chmod +x /usr/local/sbin/acb-firstboot.sh
sudo tee /etc/systemd/system/acb-firstboot.service >/dev/null <<'EOF'
[Unit]
Description=Audio Capture Box first boot (unique name and SSH keys)
ConditionPathExists=/etc/acb-firstboot
DefaultDependencies=no
After=local-fs.target
Before=ssh.service avahi-daemon.service NetworkManager.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/acb-firstboot.sh

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable acb-firstboot.service >/dev/null 2>&1

# ---- 3. pick the card
DEV="${1:-}"
if [ -z "$DEV" ]; then
  echo
  lsblk -d -o NAME,SIZE,TRAN,MODEL | grep -v -E '^(mmcblk|loop|zram|ram)' || true
  echo
  read -r -p "Which drive is the blank card in the USB reader (e.g. sda)? " DEV
fi
DEV="${DEV#/dev/}"
if [ ! -b "/dev/$DEV" ] || [[ "$DEV" == mmcblk* ]]; then
  echo "/dev/$DEV isn't a USB drive. Stopping."
  exit 1
fi
read -r -p "Everything on /dev/$DEV will be erased. Continue? [y/N] " ok
[[ "$ok" =~ ^[Yy]$ ]] || exit 1
read -r -p "Remove saved Wi-Fi networks from the copy? (say y if it's for someone else) [y/N] " wipe_wifi

# ---- 4. copy
echo "==> Copying this Pi to /dev/$DEV (a few minutes)"
sudo rpi-clone -f -U "$DEV"

# ---- 5. clean up the copy
echo "==> Preparing the copy as a fresh unit"
P2="/dev/${DEV}2"
[ -b "$P2" ] || P2="/dev/${DEV}p2"
sudo mkdir -p "$MNT"
sudo mount "$P2" "$MNT"
H="$MNT/home/$MASTER_USER"
sudo rm -rf "$H/recordings/"* "$H/recordings-trash" "$H/.config/audio-capture-box" \
            "$H/.git-credentials" "$H/.bash_history" "$MNT/root/.bash_history"
if [[ "$wipe_wifi" =~ ^[Yy]$ ]]; then
  sudo find "$MNT/etc/NetworkManager/system-connections" -type f -exec grep -l '^type=wifi' {} + 2>/dev/null | xargs -r sudo rm -f
fi
sudo rm -f "$MNT"/etc/ssh/ssh_host_*
sudo truncate -s 0 "$MNT/etc/machine-id"
sudo touch "$MNT/etc/acb-firstboot"
sudo umount "$MNT"
sync

echo
echo "Done. Take the card out, put it in another Pi and power it on."
echo "On its first boot it names itself recorder-XXXX (shown on its Send to computer screen)."
