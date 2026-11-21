"""Stand-ins for audio hardware and Wi-Fi so the app runs on any computer (--simulate)."""

import math
import threading
import time

import numpy as np

from .devices import Interface


class CallbackStop(Exception):
    pass


class _Status:
    input_overflow = False


class _FakeStream:
    def __init__(self, callback, channels, samplerate, blocksize, is_input):
        self.cb = callback
        self.channels = channels
        self.rate = samplerate
        self.block = blocksize or 1024
        self.is_input = is_input
        self._run = False
        self._t = None
        self.active = False
        self.n = 0
        rng = np.random.default_rng(channels)
        self.freqs = rng.uniform(110, 880, channels)
        self.gains = rng.uniform(0.05, 0.6, channels)

    def start(self):
        self._run = True
        self.active = True
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self):
        period = self.block / float(self.rate)
        nxt = time.monotonic()
        while self._run:
            if self.is_input:
                t = (np.arange(self.block) + self.n) / self.rate
                env = 0.55 + 0.45 * np.abs(np.sin(2 * math.pi * 0.4 * t[:, None] + np.arange(self.channels)))
                data = (np.sin(2 * math.pi * self.freqs * t[:, None]) * self.gains * env).astype(np.float32)
                if np.random.random() < 0.004:               # an occasional clip
                    data[:, np.random.randint(self.channels)] *= 3.0
                np.clip(data, -1, 1, out=data)
                self.n += self.block
                try:
                    self.cb(data, self.block, None, _Status())
                except Exception:
                    break
            else:
                out = np.zeros((self.block, self.channels), dtype=np.float32)
                try:
                    self.cb(out, self.block, None, _Status())
                except Exception:
                    break
            nxt += period
            time.sleep(max(0, nxt - time.monotonic()))
        self.active = False

    def stop(self):
        self._run = False
        if self._t and self._t is not threading.current_thread():
            self._t.join(timeout=1)
        self.active = False

    def close(self):
        self.stop()


class FakeSD:
    CallbackStop = CallbackStop

    @staticmethod
    def check_input_settings(device=None, channels=None, samplerate=None, dtype=None):
        if samplerate not in (44100, 48000, 96000):
            raise ValueError("bad rate")

    @staticmethod
    def InputStream(device=None, channels=1, samplerate=48000, dtype="float32", blocksize=1024, latency=None, callback=None):
        return _FakeStream(callback, channels, samplerate, blocksize, True)

    @staticmethod
    def OutputStream(device=None, channels=2, samplerate=48000, dtype="float32", blocksize=1024, latency=None, callback=None):
        return _FakeStream(callback, channels, samplerate, blocksize, False)


def fake_interfaces(preset="umc+xr18"):
    umc = Interface(name="UMC404HD", short="UMC", card="UMC404HD 192k", in_index=1, out_index=1, ins=4, outs=4)
    xr = Interface(name="XR18", short="XR18", card="XR18", in_index=2, out_index=2, ins=18, outs=18, stereo=[17])
    if preset == "umc":
        return [umc]
    return [umc, xr]


class FakeWifi:
    def __init__(self):
        self.current = "HomeNetwork"
        self.nets = [
            {"ssid": "HomeNetwork", "signal": 82, "secure": True},
            {"ssid": "GigPhone", "signal": 55, "secure": True},
            {"ssid": "Venue Guest", "signal": 20, "secure": False},
        ]

    def status(self):
        return self.current

    def ip_address(self):
        return "192.168.1.42" if self.current else ""

    def scan(self):
        time.sleep(0.5)
        return [dict(n, in_use=(n["ssid"] == self.current)) for n in self.nets]

    def connect(self, ssid, password=None):
        time.sleep(1.0)
        self.current = ssid
        return True, "Connected to " + ssid

    def disconnect(self):
        self.current = None
        return True

    @staticmethod
    def signal_word(sig):
        from . import wifi
        return wifi.signal_word(sig)
