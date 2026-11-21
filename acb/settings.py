"""Persistent user settings (sample rate, bit depth, arming, stereo links...)."""

import json
import os

CONFIG_DIR = os.path.expanduser("~/.config/audio-capture-box")
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")

DEFAULTS = {
    "rate": 48000,          # 44100, 48000, 96000
    "bits": 24,             # 16, 24, 32 (32 = 32-bit float)
    "dest": "sd",           # "sd" or "usb"
    "out": "hp",            # "hp" (headphone jack) or an interface short name
    "clock": None,          # interface short name used as clock master
    "armed": {},            # channel id -> bool  (missing = armed)
    "links": {},            # odd channel id -> bool (missing = device default)
    "reserve_gb": 2.0,      # space always left free on the drive
}


class Settings:
    def __init__(self, path=CONFIG_FILE):
        self.path = path
        self.data = dict(DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(self.path) as f:
                saved = json.load(f)
            for k in DEFAULTS:
                if k in saved:
                    self.data[k] = saved[k]
        except (OSError, ValueError):
            pass

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v
        self.save()
