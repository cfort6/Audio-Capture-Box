#!/usr/bin/env bash
# Show the Audio Capture Box image while the Pi boots, using Raspberry Pi's official
# splash tool (rpi-splash-screen-support), instead of scrolling text.
# Run from the repo folder:   ./install-splash.sh   then reboot.
# To use your own picture, replace extras/splash.png (800x480 PNG) and run this again.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
CMD=/boot/firmware/cmdline.txt
[ -f "$CMD" ] || CMD=/boot/cmdline.txt
CFG=/boot/firmware/config.txt
[ -f "$CFG" ] || CFG=/boot/config.txt
[ -f "$CMD.before-splash" ] || sudo cp "$CMD" "$CMD.before-splash"

remove_param() {   # remove a word from the one-line cmdline.txt
  sudo sed -i -E "1 s/(^| )$1( |$)/ /g; 1 s/  +/ /g; 1 s/^ //; 1 s/ $//" "$CMD"
}
add_param() {
  if ! tr ' ' '\n' < "$CMD" | grep -qx -- "$1"; then
    sudo sed -i "1 s|\$| $1|" "$CMD"
  fi
}

echo "==> Removing the older Plymouth splash, if it was installed"
if [ -d /usr/share/plymouth/themes/audio-capture-box ] || dpkg -s plymouth >/dev/null 2>&1; then
  sudo apt-get purge -y plymouth plymouth-label >/dev/null 2>&1 || true
  sudo rm -rf /usr/share/plymouth/themes/audio-capture-box
fi
for p in splash plymouth.ignore-serial-consoles logo.nologo; do   # logo.nologo would hide the new splash
  remove_param "$p"
done

echo "==> Installing Raspberry Pi's splash tool and an image converter"
sudo apt-get update
sudo apt-get install -y rpi-splash-screen-support imagemagick

echo "==> Converting the picture (the tool needs a 24-bit TGA with under 224 colors)"
TGA=/tmp/acb-splash.tga
CONVERT=convert
command -v convert >/dev/null || CONVERT="magick"
$CONVERT "$DIR/extras/splash.png" -colors 220 -depth 8 -type TrueColor -alpha off -compress none \
  -define tga:bits-per-sample=8 "$TGA"

echo "==> Loading the display driver early so the splash appears sooner"
# The screen driver normally loads late in boot, so nothing can be drawn until then.
# Putting it in the initramfs (the small early-boot image) lets it start sooner.
if ! grep -q '^auto_initramfs=1' "$CFG"; then
  echo 'auto_initramfs=1' | sudo tee -a "$CFG" >/dev/null
fi
MODS=$(lsmod | awk 'NR>1 {print $1}' | grep -E '^(vc4|panel_.*|rpi_panel_.*|.*attiny.*|.*backlight.*|pwm_bl|edt_ft5x06|goodix.*)$' || true)
if [ -n "$MODS" ]; then
  [ -f /etc/initramfs-tools/modules.before-splash ] || sudo cp /etc/initramfs-tools/modules /etc/initramfs-tools/modules.before-splash
  for m in $MODS; do
    grep -qx "$m" /etc/initramfs-tools/modules || echo "$m" | sudo tee -a /etc/initramfs-tools/modules >/dev/null
  done
  echo "    early drivers: $(echo $MODS | tr '\n' ' ')"
else
  echo "    (no separate display driver found; it's probably built in already)"
fi

echo "==> Hiding boot messages"
for p in quiet loglevel=3 systemd.show_status=false vt.global_cursor_default=0; do
  add_param "$p"
done
grep -q '^disable_splash=1' "$CFG" || echo 'disable_splash=1' | sudo tee -a "$CFG" >/dev/null   # no rainbow square
grep -q '^boot_delay=' "$CFG" || echo 'boot_delay=0' | sudo tee -a "$CFG" >/dev/null

echo "==> Setting the splash image"
sudo configure-splash "$TGA"
sudo update-initramfs -u

echo
echo "Done. Reboot to see it:  sudo reboot"
echo "To undo: sudo cp $CMD.before-splash $CMD"
echo "         sudo cp /etc/initramfs-tools/modules.before-splash /etc/initramfs-tools/modules 2>/dev/null; sudo update-initramfs -u"
echo "         sudo reboot"
