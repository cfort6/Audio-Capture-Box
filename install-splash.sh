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

echo "==> Setting up the early-boot image (initramfs)"
# Raspberry Pi OS defaults to MODULES=dep, which tries to detect the SD card's
# driver and can fail ("failed to determine device for /"), leaving a broken image
# that hangs at boot. The SD card and ext4 drivers are built into the Pi kernel, so
# list mode (only what we name) is enough and avoids that detection.
CONF=/etc/initramfs-tools/initramfs.conf
[ -f "$CONF.before-splash" ] || sudo cp "$CONF" "$CONF.before-splash"
if lsmod | grep -qE '^(ext4|sdhci_iproc|mmc_block) '; then
  sudo sed -i 's/^MODULES=.*/MODULES=most/' "$CONF"    # this kernel needs disk drivers as modules
else
  sudo sed -i 's/^MODULES=.*/MODULES=list/' "$CONF"
fi

# Optional and off by default: also load the screen driver early (ACB_EARLY_DRIVERS=1).
if [ "${ACB_EARLY_DRIVERS:-0}" = "1" ]; then
  MODS=$(lsmod | awk 'NR>1 {print $1}' | grep -E '^(vc4|panel_.*|rpi_panel_.*|.*attiny.*|.*backlight.*|pwm_bl|edt_ft5x06|goodix.*)$' || true)
  [ -f /etc/initramfs-tools/modules.before-splash ] || sudo cp /etc/initramfs-tools/modules /etc/initramfs-tools/modules.before-splash
  for m in $MODS; do
    grep -qx "$m" /etc/initramfs-tools/modules || echo "$m" | sudo tee -a /etc/initramfs-tools/modules >/dev/null
  done
  echo "    early drivers: $(echo $MODS | tr '\n' ' ')"
fi

echo "==> Setting the splash image and rebuilding the early-boot image"
sudo configure-splash "$TGA"
if ! sudo update-initramfs -u; then
  echo
  echo "!! Rebuilding the early-boot image failed, so no boot settings were changed."
  echo "!! Send the error above to get help. Your Pi will still boot normally."
  exit 1
fi
if ! grep -q '^auto_initramfs=1' "$CFG"; then
  sudo sed -i 's/^auto_initramfs=.*/auto_initramfs=1/' "$CFG"
  grep -q '^auto_initramfs=1' "$CFG" || echo 'auto_initramfs=1' | sudo tee -a "$CFG" >/dev/null
fi

echo "==> Hiding boot messages"
for p in quiet loglevel=3 systemd.show_status=false vt.global_cursor_default=0; do
  add_param "$p"
done
grep -q '^disable_splash=1' "$CFG" || echo 'disable_splash=1' | sudo tee -a "$CFG" >/dev/null   # no rainbow square
grep -q '^boot_delay=' "$CFG" || echo 'boot_delay=0' | sudo tee -a "$CFG" >/dev/null


echo
echo "Done. Reboot to see it:  sudo reboot"
echo "To undo: sudo cp $CMD.before-splash $CMD"
echo "         (or, from a computer, set auto_initramfs=0 in config.txt on the SD card)"
echo "         sudo reboot"
