#!/usr/bin/env bash
# Show the Audio Capture Box image while the Pi boots, instead of scrolling text.
# Uses Plymouth, Linux's standard boot splash program.
# Run from the repo folder:   ./install-splash.sh   then reboot.
# To use your own picture, replace extras/splash.png (800x480 PNG) and run this again.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
THEME_NAME=audio-capture-box
THEME=/usr/share/plymouth/themes/$THEME_NAME
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

echo "==> Removing other splash setups we tried, if present"
if [ -f /etc/systemd/system/acb-splash.service ]; then
  sudo systemctl disable acb-splash.service >/dev/null 2>&1 || true
  sudo rm -f /etc/systemd/system/acb-splash.service /usr/local/sbin/acb-splash
  sudo rm -rf /usr/local/share/acb
  sudo systemctl daemon-reload
fi
sudo systemctl enable getty@tty1.service >/dev/null 2>&1 || true
if dpkg -s rpi-splash-screen-support >/dev/null 2>&1; then
  sudo apt-get purge -y rpi-splash-screen-support >/dev/null 2>&1 || true
fi
for p in fullscreen_logo=1 fullscreen_logo_name=logo.tga; do remove_param "$p"; done

echo "==> Installing Plymouth (the boot splash program)"
sudo apt-get install -y plymouth

echo "==> Installing the splash theme"
sudo mkdir -p "$THEME"
sudo cp "$DIR/extras/splash.png" "$THEME/splash.png"
sudo tee "$THEME/$THEME_NAME.plymouth" >/dev/null <<EOF
[Plymouth Theme]
Name=Audio Capture Box
Description=Audio Capture Box boot image
ModuleName=script

[script]
ImageDir=$THEME
ScriptFile=$THEME/$THEME_NAME.script
EOF
sudo tee "$THEME/$THEME_NAME.script" >/dev/null <<'EOF'
Window.SetBackgroundTopColor(0.055, 0.063, 0.075);
Window.SetBackgroundBottomColor(0.055, 0.063, 0.075);
img = Image("splash.png");
scale = Math.Min(Window.GetWidth() / img.GetWidth(), Window.GetHeight() / img.GetHeight());
if (scale < 1) img = img.Scale(img.GetWidth() * scale, img.GetHeight() * scale);
s = Sprite(img);
s.SetX(Window.GetWidth() / 2 - img.GetWidth() / 2);
s.SetY(Window.GetHeight() / 2 - img.GetHeight() / 2);
EOF

echo "==> Setting up the early-boot image (initramfs)"
# Raspberry Pi OS's MODULES=dep can fail to detect the SD card ("failed to determine
# device for /") and leave a broken image that hangs at boot. The SD card and ext4
# drivers are built into the Pi kernel, so list mode (only what's named) is enough.
CONF=/etc/initramfs-tools/initramfs.conf
[ -f "$CONF.before-splash" ] || sudo cp "$CONF" "$CONF.before-splash"
if lsmod | grep -qE '^(ext4|sdhci_iproc|mmc_block) '; then
  sudo sed -i 's/^MODULES=.*/MODULES=most/' "$CONF"
else
  sudo sed -i 's/^MODULES=.*/MODULES=list/' "$CONF"
fi
# Optional, off by default: load the screen driver early so the splash shows sooner.
# Run with ACB_EARLY_DRIVERS=1 ./install-splash.sh to try it.
if [ "${ACB_EARLY_DRIVERS:-0}" = "1" ]; then
  MODS=$(lsmod | awk 'NR>1 {print $1}' | grep -E '^(vc4|panel_.*|rpi_panel_.*|.*attiny.*|.*backlight.*|pwm_bl|edt_ft5x06|goodix.*)$' || true)
  [ -f /etc/initramfs-tools/modules.before-splash ] || sudo cp /etc/initramfs-tools/modules /etc/initramfs-tools/modules.before-splash
  for m in $MODS; do
    grep -qx "$m" /etc/initramfs-tools/modules || echo "$m" | sudo tee -a /etc/initramfs-tools/modules >/dev/null
  done
  echo "    early drivers: $(echo $MODS | tr '\n' ' ')"
fi

echo "==> Setting the theme and rebuilding the early-boot image (takes a minute)"
sudo plymouth-set-default-theme "$THEME_NAME"
if ! sudo update-initramfs -u; then
  echo
  echo "!! Rebuilding the early-boot image failed, so no boot settings were changed."
  echo "!! Send the error above to get help. Your Pi will still boot normally."
  exit 1
fi

echo "==> Hiding boot messages"
for p in quiet splash plymouth.ignore-serial-consoles logo.nologo vt.global_cursor_default=0 loglevel=3 systemd.show_status=false; do
  add_param "$p"
done
grep -q '^disable_splash=1' "$CFG" || echo 'disable_splash=1' | sudo tee -a "$CFG" >/dev/null   # no rainbow square
grep -q '^boot_delay=' "$CFG" || echo 'boot_delay=0' | sudo tee -a "$CFG" >/dev/null
sudo sed -i 's/^auto_initramfs=.*/auto_initramfs=1/' "$CFG"
grep -q '^auto_initramfs=1' "$CFG" || echo 'auto_initramfs=1' | sudo tee -a "$CFG" >/dev/null

echo
echo "Done. Reboot to see it:  sudo reboot"
echo "To undo: sudo cp $CMD.before-splash $CMD && sudo reboot"
echo "         (or, from a computer, restore cmdline.txt and set auto_initramfs=0 in config.txt)"
