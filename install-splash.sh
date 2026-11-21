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

echo "==> Setting the theme (this rebuilds the boot files, takes a minute)"
sudo plymouth-set-default-theme -R "$THEME_NAME"

echo
echo "Done. Reboot to see it:  sudo reboot"
echo "To undo: sudo cp $CMD.before-splash $CMD && sudo reboot"
