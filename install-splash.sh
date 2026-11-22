#!/usr/bin/env bash
# Show the Audio Capture Box image while the Pi boots, instead of scrolling text.
# Run from the repo folder:   ./install-splash.sh   then reboot.
# To use your own picture, replace extras/splash.png (800x480 PNG) and run this again.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
THEME_NAME=audio-capture-box
THEME=/usr/share/plymouth/themes/$THEME_NAME

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

echo "==> Hiding boot messages"
CMD=/boot/firmware/cmdline.txt
[ -f "$CMD" ] || CMD=/boot/cmdline.txt
[ -f "$CMD.before-splash" ] || sudo cp "$CMD" "$CMD.before-splash"
for p in quiet splash plymouth.ignore-serial-consoles logo.nologo vt.global_cursor_default=0 loglevel=3 systemd.show_status=false; do
  if ! tr ' ' '\n' < "$CMD" | grep -qx -- "$p"; then
    sudo sed -i "1 s|\$| $p|" "$CMD"
  fi
done

CFG=/boot/firmware/config.txt
[ -f "$CFG" ] || CFG=/boot/config.txt
if ! grep -q '^disable_splash=1' "$CFG"; then
  echo 'disable_splash=1' | sudo tee -a "$CFG" >/dev/null     # no rainbow square at power-on
fi

echo "==> Loading the display driver early so the splash appears sooner"
# Normally the screen driver loads from the SD card late in boot, so Plymouth has
# nothing to draw on until then. Putting the driver in the initramfs (the small
# early-boot image) lets the screen and backlight come on several seconds sooner.
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
if ! grep -q '^boot_delay=' "$CFG"; then
  echo 'boot_delay=0' | sudo tee -a "$CFG" >/dev/null
fi

echo "==> Setting the theme (this rebuilds the boot files, takes a minute)"
sudo plymouth-set-default-theme -R "$THEME_NAME"

echo
echo "Done. Reboot to see it:  sudo reboot"
echo "To undo: sudo cp $CMD.before-splash $CMD"
echo "         sudo cp /etc/initramfs-tools/modules.before-splash /etc/initramfs-tools/modules 2>/dev/null; sudo update-initramfs -u"
echo "         sudo reboot"
