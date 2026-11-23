#!/usr/bin/env bash
# Show the Audio Capture Box image while the Pi starts, instead of scrolling text.
#
# A tiny service copies a ready-made image to the screen the moment the screen
# driver is up. It runs alongside normal startup, so if it ever fails the Pi still
# boots. (Boot-level splash tools hung this Pi with its DSI screen, so this script
# also removes those if they were installed.)
#
# Run from the repo folder:   ./install-splash.sh   then reboot.
# To use your own picture, replace extras/splash.png (800x480 PNG) and run this again.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
CMD=/boot/firmware/cmdline.txt
[ -f "$CMD" ] || CMD=/boot/cmdline.txt
[ -f "$CMD.before-splash" ] || sudo cp "$CMD" "$CMD.before-splash"

remove_param() {   # remove a word from the one-line cmdline.txt
  sudo sed -i -E "1 s/(^| )$1( |$)/ /g; 1 s/  +/ /g; 1 s/^ //; 1 s/ $//" "$CMD"
}
add_param() {
  if ! tr ' ' '\n' < "$CMD" | grep -qx -- "$1"; then
    sudo sed -i "1 s|\$| $1|" "$CMD"
  fi
}

echo "==> Removing earlier boot-level splash setups, if any"
for pkg in plymouth plymouth-label rpi-splash-screen-support; do
  if dpkg -s "$pkg" >/dev/null 2>&1; then sudo apt-get purge -y "$pkg" >/dev/null 2>&1 || true; fi
done
sudo rm -rf /usr/share/plymouth/themes/audio-capture-box
for p in splash plymouth.ignore-serial-consoles fullscreen_logo=1 fullscreen_logo_name=logo.tga; do
  remove_param "$p"
done

echo "==> Preparing the boot image"
sudo mkdir -p /usr/local/share/acb
tmp=$(mktemp)
(cd "$DIR" && python3 -m acb.bootsplash extras/splash.png "$tmp")
sudo install -m 644 "$tmp" /usr/local/share/acb/splash.fb
rm -f "$tmp"

echo "==> Installing the boot image service"
sudo tee /usr/local/sbin/acb-splash >/dev/null <<'EOF'
#!/bin/sh
# Draw the Audio Capture Box image as soon as the screen exists.
i=0
while [ ! -e /dev/fb0 ] && [ "$i" -lt 150 ]; do sleep 0.1; i=$((i + 1)); done
[ -e /dev/fb0 ] || exit 0
printf '\033[?25l\033[2J' > /dev/tty1 2>/dev/null || true
cat /usr/local/share/acb/splash.fb > /dev/fb0 2>/dev/null || true
exit 0
EOF
sudo chmod +x /usr/local/sbin/acb-splash
sudo tee /etc/systemd/system/acb-splash.service >/dev/null <<'EOF'
[Unit]
Description=Audio Capture Box boot image
DefaultDependencies=no
After=systemd-udevd.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/acb-splash
TimeoutStartSec=20

[Install]
WantedBy=sysinit.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable acb-splash.service

echo "==> Keeping text off the screen during startup"
# Standard, safe kernel options: fewer messages, no blinking cursor, no raspberry logos.
for p in quiet loglevel=3 systemd.show_status=false vt.global_cursor_default=0 logo.nologo; do
  add_param "$p"
done
# The login prompt on the screen would draw over the image; log in over SSH instead.
sudo systemctl disable getty@tty1.service >/dev/null 2>&1 || true
CFG=/boot/firmware/config.txt
[ -f "$CFG" ] || CFG=/boot/config.txt
grep -q '^disable_splash=1' "$CFG" || echo 'disable_splash=1' | sudo tee -a "$CFG" >/dev/null

echo
echo "Done. Reboot to see it:  sudo reboot"
echo "To undo:  sudo systemctl disable acb-splash && sudo systemctl enable getty@tty1"
echo "          sudo cp $CMD.before-splash $CMD && sudo reboot"
