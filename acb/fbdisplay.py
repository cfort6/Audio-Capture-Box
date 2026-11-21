"""Draw to the Linux framebuffer (/dev/fb0) and read the touchscreen with evdev.

This avoids SDL's GPU-based display drivers, which need EGL/GBM libraries that
Raspberry Pi OS Lite may not have working. pygame draws into an off-screen surface
(SDL "dummy" driver) and each frame is copied into the framebuffer.
"""

import fcntl
import mmap
import os
import struct
import threading
import time

import numpy as np
import pygame

FBIOGET_VSCREENINFO = 0x4600


def available(dev="/dev/fb0"):
    return os.path.exists(dev) and os.access(dev, os.R_OK | os.W_OK)


class FrameBuffer:
    def __init__(self, dev="/dev/fb0", size=None, bpp=None, stride=None):
        self.f = open(dev, "r+b", buffering=0)
        if size is None or bpp is None:
            info = fcntl.ioctl(self.f.fileno(), FBIOGET_VSCREENINFO, bytes(160))
            xres, yres, _xv, _yv, _xo, _yo, bits = struct.unpack_from("7I", info)
            size, bpp = (xres, yres), bits
        self.w, self.h = size
        self.bpp = bpp
        if stride is None:
            name = os.path.basename(dev)
            try:
                with open(f"/sys/class/graphics/{name}/stride") as s:
                    stride = int(s.read().strip())
            except (OSError, ValueError):
                stride = self.w * bpp // 8
        self.stride = stride
        self.mm = mmap.mmap(self.f.fileno(), self.stride * self.h)
        self.dst = np.ndarray((self.h, self.stride), dtype=np.uint8, buffer=self.mm)

    def show(self, surf):
        sw, sh = surf.get_size()
        w, h = min(self.w, sw), min(self.h, sh)
        if self.bpp == 32:
            data = np.frombuffer(pygame.image.tobytes(surf, "BGRA"), np.uint8).reshape(sh, sw, 4)
            self.dst[:h, :w * 4] = data[:h, :w].reshape(h, w * 4)
        elif self.bpp == 16:
            rgb = np.frombuffer(pygame.image.tobytes(surf, "RGB"), np.uint8).reshape(sh, sw, 3)[:h, :w]
            r = rgb[..., 0].astype(np.uint16)
            g = rgb[..., 1].astype(np.uint16)
            b = rgb[..., 2].astype(np.uint16)
            px = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
            self.dst[:h, :w * 2] = px.view(np.uint8).reshape(h, w * 2)
        elif self.bpp == 24:
            data = np.frombuffer(pygame.image.tobytes(surf, "RGB"), np.uint8).reshape(sh, sw, 3)[:h, :w, ::-1]
            self.dst[:h, :w * 3] = np.ascontiguousarray(data).reshape(h, w * 3)

    def clear(self):
        self.dst[:] = 0

    def close(self):
        try:
            self.mm.close()
            self.f.close()
        except Exception:
            pass


class TouchReader:
    """Turns touchscreen events into pygame mouse events (down / motion / up)."""

    def __init__(self, width, height):
        self.w, self.h = width, height
        self.invert_x = os.environ.get("ACB_TOUCH_INVERT_X") == "1"
        self.invert_y = os.environ.get("ACB_TOUCH_INVERT_Y") == "1"
        self.swap_xy = os.environ.get("ACB_TOUCH_SWAP_XY") == "1"
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self.thread.start()

    def _find(self):
        import evdev
        from evdev import ecodes as e
        for path in evdev.list_devices():
            try:
                d = evdev.InputDevice(path)
                caps = d.capabilities()
            except OSError:
                continue
            absd = dict(caps.get(e.EV_ABS, []))
            keys = caps.get(e.EV_KEY, [])
            if (e.ABS_MT_POSITION_X in absd or e.ABS_X in absd) and e.BTN_TOUCH in keys:
                return d, absd
        return None, None

    def _loop(self):
        try:
            import evdev  # noqa: F401
            from evdev import ecodes as e
        except ImportError:
            print("python3-evdev is not installed; touch input disabled")
            return
        while self.running:
            dev, absd = self._find()
            if dev is None:
                time.sleep(3)
                continue
            print(f"Touchscreen: {dev.name} ({dev.path})")
            xi = absd.get(e.ABS_MT_POSITION_X) or absd.get(e.ABS_X)
            yi = absd.get(e.ABS_MT_POSITION_Y) or absd.get(e.ABS_Y)
            rx, ry = 0, 0
            touching, pending_down, pending_up, moved = False, False, False, False
            try:
                for ev in dev.read_loop():
                    if ev.type == e.EV_ABS:
                        if ev.code in (e.ABS_MT_POSITION_X, e.ABS_X):
                            rx, moved = ev.value, True
                        elif ev.code in (e.ABS_MT_POSITION_Y, e.ABS_Y):
                            ry, moved = ev.value, True
                    elif ev.type == e.EV_KEY and ev.code == e.BTN_TOUCH:
                        if ev.value:
                            pending_down = True
                        else:
                            pending_up = True
                    elif ev.type == e.EV_SYN and ev.code == e.SYN_REPORT:
                        pos = self._scale(rx, ry, xi, yi)
                        if pending_down and not touching:
                            touching = True
                            self._post(pygame.MOUSEMOTION, pos)
                            self._post(pygame.MOUSEBUTTONDOWN, pos)
                        elif touching and moved:
                            self._post(pygame.MOUSEMOTION, pos)
                        if pending_up and touching:
                            touching = False
                            self._post(pygame.MOUSEBUTTONUP, pos)
                        pending_down = pending_up = moved = False
            except OSError:
                time.sleep(1)       # device unplugged; look again

    def _scale(self, rx, ry, xi, yi):
        def norm(v, info):
            lo, hi = (info.min, info.max) if info else (0, 1)
            return (v - lo) / float(max(1, hi - lo))
        nx, ny = norm(rx, xi), norm(ry, yi)
        if self.swap_xy:
            nx, ny = ny, nx
        if self.invert_x:
            nx = 1 - nx
        if self.invert_y:
            ny = 1 - ny
        return (int(max(0, min(self.w - 1, nx * self.w))), int(max(0, min(self.h - 1, ny * self.h))))

    def _post(self, etype, pos):
        try:
            if etype == pygame.MOUSEMOTION:
                pygame.event.post(pygame.event.Event(etype, pos=pos, rel=(0, 0), buttons=(1, 0, 0)))
            else:
                pygame.event.post(pygame.event.Event(etype, pos=pos, button=1))
        except pygame.error:
            pass
