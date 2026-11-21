"""Full-screen touchscreen interface (800x480) drawn with pygame."""

import collections
import getpass
import math
import os
import queue
import socket
import threading
import time

import pygame

from . import devices, fbdisplay, storage
from .engine import BYTES_PER_SAMPLE, Engine, Player
from .settings import Settings
from .share import PORT, Share

W, H = 800, 480
FPS = 25

C = {
    "bg": (14, 16, 19), "panel": (23, 26, 31), "chip": (28, 32, 38), "line": (35, 39, 46),
    "text": (232, 234, 237), "muted": (154, 160, 166), "soft": (201, 205, 210),
    "btn": (31, 35, 41), "btnb": (44, 49, 56), "key": (37, 42, 49), "keyb": (50, 56, 67),
    "blue": (90, 169, 255), "bluebg": (30, 51, 80), "bluetext": (191, 216, 255), "link": (140, 196, 255),
    "stereobg": (27, 36, 51), "dash": (74, 106, 148),
    "green": (61, 220, 132), "yellow": (255, 201, 60), "red": (255, 69, 69),
    "recbg": (42, 18, 21), "recb": (255, 77, 77), "rectext": (255, 138, 138), "recsolid": (198, 40, 40),
    "sheet": (20, 23, 28), "card": (26, 30, 36), "well": (11, 13, 16), "dark": (18, 21, 26),
    "wavoff": (58, 63, 71), "clipbg": (42, 14, 16), "cliptext": (255, 107, 107),
}
IFACE_COLORS = [(90, 169, 255), (255, 159, 67), (180, 140, 255), (63, 208, 201)]

GREEN_TOP = 0.70   # -18 dBFS on a 60 dB meter
YELLOW_TOP = 0.90  # -6 dBFS
CLIP = 0.999
CLIP_HOLD = 10.0
HIST_LEN = 48
HIST_EVERY = 0.25
DISK_EVERY = 5.0
MONO_W = 84
PAIR_W = MONO_W * 2 + 8

FONT_DIR = "/usr/share/fonts/truetype/dejavu"


def clamp(v, a, b):
    return a if v < a else b if v > b else v


def to_meter(peak):
    if peak <= 1e-6:
        return 0.0
    return clamp((20 * math.log10(peak) + 60) / 60.0, 0.0, 1.0)


def hms(sec):
    sec = max(0, int(sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def left_fmt(sec):
    if sec is None or sec == float("inf"):
        return "—"
    h, m = int(sec // 3600), int(sec % 3600 // 60)
    return "99+ h" if h >= 100 else f"{h} h {m:02d} m"


def rate_label(r):
    return "44.1 kHz" if r == 44100 else f"{r // 1000} kHz"


def bit_label(b):
    return "32-bit float" if b == 32 else f"{b}-bit"


class Fonts:
    def __init__(self):
        self.cache = {}
        self.paths = {
            (False, False): os.path.join(FONT_DIR, "DejaVuSans.ttf"),
            (True, False): os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf"),
            (False, True): os.path.join(FONT_DIR, "DejaVuSansMono.ttf"),
            (True, True): os.path.join(FONT_DIR, "DejaVuSansMono-Bold.ttf"),
        }

    def get(self, size, bold=False, mono=False):
        k = (size, bold, mono)
        f = self.cache.get(k)
        if f is None:
            p = self.paths[(bold, mono)]
            if os.path.exists(p):
                f = pygame.font.Font(p, size)
            else:
                f = pygame.font.Font(None, int(size * 1.35))
                f.set_bold(bold)
            self.cache[k] = f
        return f


class App:
    def __init__(self, sd, wifi, ifaces_provider, simulate=False, windowed=False):
        self.sd = sd
        self.wifi = wifi
        self.simulate = simulate
        self.ifaces_provider = ifaces_provider
        self.fb = None
        self.touch = None
        mode = os.environ.get("ACB_DISPLAY", "auto")
        if not windowed and mode in ("auto", "fb") and fbdisplay.available():
            # Draw off-screen and copy each frame to /dev/fb0; read touch via evdev.
            os.environ["SDL_VIDEODRIVER"] = "dummy"
            pygame.display.init()
            pygame.font.init()
            self.surf = pygame.display.set_mode((W, H))
            self.fb = fbdisplay.FrameBuffer()
            print(f"Display: framebuffer {self.fb.w}x{self.fb.h} {self.fb.bpp}-bit")
            self.touch = fbdisplay.TouchReader(W, H)
            self.touch.start()
        else:
            if not windowed:
                os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")
            pygame.display.init()
            pygame.font.init()
            flags = 0 if windowed else pygame.FULLSCREEN
            self.surf = pygame.display.set_mode((W, H), flags)
            pygame.display.set_caption("Audio Capture Box")
            if not windowed:
                pygame.mouse.set_visible(False)
        self.fonts = Fonts()
        self.tcache = {}
        self.clock = pygame.time.Clock()
        self.S = Settings()
        self.engine = Engine(sd)
        self.player = Player(sd)
        self.share = Share()
        self.ui_q = queue.Queue()
        self.running = True

        self.screen = "main"
        self.hits = []
        self.scroll_regions = []
        self.scroll = {"strips": 0, "takes": 0, "nets": 0}
        self.press = None
        self.dragging = False
        self.toast_msg, self.toast_until = "", 0

        self.levels, self.peak_at, self.hist = {}, {}, {}
        self.last_hist = 0
        self.ifaces, self.headphones = [], -1
        self.cards_sig = None
        self.last_hotplug = 0

        self.usb_root = None
        self.free = {"sd": 0, "usb": 0}
        self.trash = {"sd": 0, "usb": 0}
        self.last_disk = 0
        self.disk_busy = False
        self.rec_bps = 0
        self.rec_dest = "sd"

        self.takes = []
        self.playing_dir = None

        self.wifi_ssid, self.wifi_ip = None, ""
        self.last_wifi = 0
        self.nets, self.scanning = [], False
        self.pending, self.pending_secure = None, True
        self.pwd, self.kb_shift, self.kb_sym, self.show_pwd = "", False, False, False
        self.wifi_err, self.connecting = "", False

        self.user = getpass.getuser()
        self.host = socket.gethostname()

        self.reload_devices(first=True)
        self.refresh_disk(sync=True)
        self.refresh_wifi()

    # ------------------------------------------------------------------ helpers
    def bg(self, fn, done=None):
        def run():
            try:
                res = fn()
            except Exception as e:      # keep the UI alive whatever happens
                res = e
            if done:
                self.ui_q.put(lambda: done(res))
        threading.Thread(target=run, daemon=True).start()

    def toast(self, msg, secs=3.5):
        self.toast_msg, self.toast_until = msg, time.monotonic() + secs

    def font(self, size, bold=False, mono=False):
        return self.fonts.get(size, bold, mono)

    def render(self, s, size, color, bold=False, mono=False):
        k = (s, size, color, bold, mono)
        t = self.tcache.get(k)
        if t is None:
            if len(self.tcache) > 3000:
                self.tcache.clear()
            t = self.font(size, bold, mono).render(s, True, color)
            self.tcache[k] = t
        return t

    def text(self, s, pos, size=13, color=None, bold=False, mono=False, anchor="topleft", maxw=None):
        color = color or C["text"]
        if maxw is not None:
            s = self.fit(s, size, maxw, bold, mono)
        t = self.render(s, size, color, bold, mono)
        r = t.get_rect(**{anchor: pos})
        self.surf.blit(t, r)
        return r

    def tw(self, s, size, bold=False, mono=False):
        return self.font(size, bold, mono).size(s)[0]

    def fit(self, s, size, maxw, bold=False, mono=False):
        if self.tw(s, size, bold, mono) <= maxw:
            return s
        while s and self.tw(s + "…", size, bold, mono) > maxw:
            s = s[:-1]
        return s + "…"

    def wrap(self, s, size, maxw, bold=False):
        words, lines, cur = s.split(" "), [], ""
        for w in words:
            t = (cur + " " + w).strip()
            if self.tw(t, size, bold) <= maxw or not cur:
                cur = t
            else:
                lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines

    def rrect(self, rect, fill=None, radius=8, border=None, bw=1):
        rect = pygame.Rect(rect)
        if fill is not None:
            pygame.draw.rect(self.surf, fill, rect, border_radius=radius)
        if border is not None:
            pygame.draw.rect(self.surf, border, rect, bw, border_radius=radius)

    def dashed_rect(self, rect, color, dash=6, gap=4, radius=8):
        r = pygame.Rect(rect)
        for x0, y0, x1, y1 in ((r.left + radius, r.top, r.right - radius, r.top),
                               (r.left + radius, r.bottom - 1, r.right - radius, r.bottom - 1)):
            x = x0
            while x < x1:
                pygame.draw.line(self.surf, color, (x, y0), (min(x + dash, x1), y1), 2)
                x += dash + gap
        for x in (r.left, r.right - 1):
            y = r.top + radius
            while y < r.bottom - radius:
                pygame.draw.line(self.surf, color, (x, y), (x, min(y + dash, r.bottom - radius)), 2)
                y += dash + gap
        for cx, cy, a0 in ((r.left + radius, r.top + radius, math.pi / 2), (r.right - radius, r.top + radius, 0),
                           (r.left + radius, r.bottom - radius, math.pi), (r.right - radius, r.bottom - radius, -math.pi / 2)):
            pygame.draw.arc(self.surf, color, (cx - radius, cy - radius, radius * 2, radius * 2), a0, a0 + math.pi / 2, 2)

    def hit(self, rect, fn, enabled=True):
        self.hits.append((pygame.Rect(rect), fn, enabled))

    def button(self, rect, label, fn, enabled=True, fill=None, border=None, color=None, size=14, bold=True, radius=10):
        rect = pygame.Rect(rect)
        self.rrect(rect, fill or C["btn"], radius, border or C["btnb"])
        self.text(label, rect.center, size, color or C["text"], bold, anchor="center", maxw=rect.w - 12)
        if not enabled:
            self.dim(rect, radius)
        self.hit(rect, fn, enabled)
        return rect

    def dim(self, rect, radius=8):
        s = pygame.Surface(pygame.Rect(rect).size, pygame.SRCALPHA)
        pygame.draw.rect(s, (14, 16, 19, 140), s.get_rect(), border_radius=radius)
        self.surf.blit(s, pygame.Rect(rect).topleft)

    def label(self, s, pos):
        self.text(s.upper(), pos, 11, C["muted"], True)

    # icons
    def icon_wifi(self, cx, cy, color, off=False, scale=1.0):
        for r in (10, 6.5, 3):
            rr = r * scale
            rect = pygame.Rect(0, 0, rr * 2, rr * 2)
            rect.center = (cx, cy + 5 * scale)
            pygame.draw.arc(self.surf, color, rect, math.pi / 4, 3 * math.pi / 4, max(2, int(2 * scale)))
        pygame.draw.circle(self.surf, color, (cx, int(cy + 5 * scale)), max(2, int(1.6 * scale)))
        if off:
            pygame.draw.line(self.surf, color, (cx - 9 * scale, cy - 7 * scale), (cx + 9 * scale, cy + 9 * scale), 2)

    def icon_play(self, cx, cy, s, color):
        pygame.draw.polygon(self.surf, color, [(cx - s * 0.4, cy - s * 0.55), (cx - s * 0.4, cy + s * 0.55), (cx + s * 0.55, cy)])

    def icon_stop(self, cx, cy, s, color):
        pygame.draw.rect(self.surf, color, (cx - s / 2, cy - s / 2, s, s), border_radius=2)

    def icon_close(self, cx, cy, color):
        pygame.draw.line(self.surf, color, (cx - 7, cy - 7), (cx + 7, cy + 7), 3)
        pygame.draw.line(self.surf, color, (cx + 7, cy - 7), (cx - 7, cy + 7), 3)

    def icon_chevron(self, cx, cy, direction, color, s=10):
        d = -1 if direction == "left" else 1
        pts = [(cx - d * s * 0.5, cy - s), (cx + d * s * 0.5, cy), (cx - d * s * 0.5, cy + s)]
        pygame.draw.lines(self.surf, color, False, pts, 4)

    def icon_sliders(self, cx, cy, color):
        for dy, kx in ((-6, 4), (0, -3), (6, 6)):
            pygame.draw.line(self.surf, color, (cx - 9, cy + dy), (cx + 9, cy + dy), 2)
            pygame.draw.circle(self.surf, C["chip"], (cx + kx, cy + dy), 3)
            pygame.draw.circle(self.surf, color, (cx + kx, cy + dy), 3, 2)

    def icon_lock(self, cx, cy, color):
        pygame.draw.rect(self.surf, color, (cx - 6, cy - 1, 12, 9), 2, border_radius=2)
        pygame.draw.arc(self.surf, color, (cx - 4, cy - 8, 8, 10), 0, math.pi, 2)

    def icon_link(self, cx, cy, color):
        pygame.draw.ellipse(self.surf, color, (cx - 9, cy - 4, 11, 8), 2)
        pygame.draw.ellipse(self.surf, color, (cx - 2, cy - 4, 11, 8), 2)

    def icon_drive(self, cx, cy, color):
        pygame.draw.rect(self.surf, color, (cx - 9, cy - 5, 18, 10), 2, border_radius=2)
        pygame.draw.circle(self.surf, color, (cx - 4, cy), 1)
        pygame.draw.circle(self.surf, color, (cx, cy), 1)

    # ------------------------------------------------------------------ devices / disk / wifi
    def reload_devices(self, first=False):
        if self.engine.recording:
            return
        self.player.stop()
        self.engine.close()
        self.ifaces, self.headphones = self.ifaces_provider()
        for i, f in enumerate(self.ifaces):
            f.color = IFACE_COLORS[i % len(IFACE_COLORS)]
        self.cards_sig = devices.cards_signature()
        self.levels, self.peak_at, self.hist = {}, {}, {}
        rate = self.S["rate"]
        bad = self.engine.check_rate(self.ifaces, rate)
        if bad and rate != 48000:
            self.toast(f"{', '.join(bad)} can't run at {rate_label(rate)}. Using 48 kHz.")
            rate = 48000
            self.S["rate"] = rate
        errors = self.engine.open(self.ifaces, rate)
        if errors:
            self.toast(errors[0][:90], 6)
        elif not first:
            names = ", ".join(f.name for f in self.ifaces) or "none"
            self.toast("Interfaces: " + names)

    def check_hotplug(self, now):
        if now - self.last_hotplug < 2.0:
            return
        self.last_hotplug = now
        if self.engine.recording:
            dead = self.engine.dead_streams()
            if dead:
                self.stop_recording(reason="An interface was disconnected. Recording saved.")
            return
        if self.simulate or self.player.playing:
            return
        sig = devices.cards_signature()
        if sig != self.cards_sig:
            try:
                self.sd._terminate()
                self.sd._initialize()
            except Exception:
                pass
            self.reload_devices()

    def dest(self):
        if self.engine.recording:
            return self.rec_dest
        return "usb" if self.S["dest"] == "usb" and self.usb_root else "sd"

    def dest_name(self, d=None):
        return "USB drive" if (d or self.dest()) == "usb" else "SD card"

    def rec_dir(self, d=None):
        d = d or self.dest()
        if d == "usb" and self.usb_root:
            return storage.recordings_dir(self.usb_root)
        return storage.recordings_dir(storage.SD_ROOT)

    def refresh_disk(self, sync=False):
        recording, rec_dest, rec_bps = self.engine.recording, self.rec_dest, self.rec_bps
        reserve = self.S["reserve_gb"] * 1e9

        def work():
            usb = None if self.simulate else storage.find_usb_drive()
            dirs = {"sd": storage.recordings_dir(storage.SD_ROOT)}
            if usb:
                dirs["usb"] = storage.recordings_dir(usb)
            purged = 0
            # While recording, clear the oldest takes out of the Trash if the drive
            # gets within ~2 minutes of the safety buffer.
            if recording and rec_dest in dirs:
                d = dirs[rec_dest]
                while storage.free_bytes(d) - reserve < rec_bps * 120:
                    if not storage.purge_oldest(d):
                        break
                    purged += 1
            free = {k: storage.free_bytes(v) for k, v in dirs.items()}
            trash = {k: storage.trash_bytes(v) for k, v in dirs.items()}
            return usb, free, trash, purged

        def done(res):
            self.disk_busy = False
            if isinstance(res, Exception):
                return
            usb, free, trash, purged = res
            self.usb_root = usb
            self.free = {"sd": free.get("sd", 0), "usb": free.get("usb", 0)}
            self.trash = {"sd": trash.get("sd", 0), "usb": trash.get("usb", 0)}
            if purged:
                self.toast(f"Made room by clearing {purged} take{'s' if purged != 1 else ''} out of the Trash.")

        if sync:
            done(work())
        elif not self.disk_busy:
            self.disk_busy = True
            self.bg(work, done)

    def refresh_wifi(self):
        def work():
            return self.wifi.status(), self.wifi.ip_address()

        def done(res):
            if not isinstance(res, Exception):
                self.wifi_ssid, self.wifi_ip = res
        self.bg(work, done)

    # ------------------------------------------------------------------ channel model
    def chan_id(self, f, n):
        return f"{f.short}-{n}"

    def armed(self, cid):
        return self.S["armed"].get(cid, True)

    def linked(self, f, n):
        v = self.S["links"].get(self.chan_id(f, n))
        return (n in f.stereo) if v is None else v

    def groups(self):
        out = []
        for f in self.ifaces:
            n = 1
            while n <= f.ins:
                a = self.chan_id(f, n)
                if n + 1 > f.ins:
                    out.append({"f": f, "n": n, "pair": False, "stereo": False, "w": MONO_W,
                                "strips": [{"f": f, "n": n, "ids": [a], "stereo": False, "w": MONO_W}]})
                else:
                    b = self.chan_id(f, n + 1)
                    st = self.linked(f, n)
                    strips = ([{"f": f, "n": n, "ids": [a, b], "stereo": True, "w": PAIR_W}] if st else
                              [{"f": f, "n": n, "ids": [a], "stereo": False, "w": MONO_W},
                               {"f": f, "n": n + 1, "ids": [b], "stereo": False, "w": MONO_W}])
                    out.append({"f": f, "n": n, "pair": True, "stereo": st, "w": PAIR_W, "strips": strips,
                                "device": st and n in f.stereo and self.chan_id(f, n) not in self.S["links"]})
                n += 2
        return out

    def armed_channels(self):
        return sum(1 for f in self.ifaces for n in range(1, f.ins + 1) if self.armed(self.chan_id(f, n)))

    def bytes_per_sec(self):
        return self.S["rate"] * BYTES_PER_SAMPLE[self.S["bits"]] * self.armed_channels()

    def time_left(self):
        d = self.dest()
        bps = self.rec_bps if self.engine.recording else self.bytes_per_sec()
        avail = self.available(d) - self.S["reserve_gb"] * 1e9
        if bps <= 0:
            return float("inf")
        return max(0.0, avail / bps)

    def available(self, d):
        """Free space plus whatever is in the Trash (it gets cleared when needed)."""
        return self.free[d] + self.trash[d]

    def toggle_arm(self, ids, val):
        a = dict(self.S["armed"])
        for i in ids:
            a[i] = val
        self.S["armed"] = a

    def toggle_link(self, g):
        f, n = g["f"], g["n"]
        a_id, b_id = self.chan_id(f, n), self.chan_id(f, n + 1)
        links = dict(self.S["links"])
        links[a_id] = not g["stereo"]
        self.S["links"] = links
        if not g["stereo"]:
            self.toggle_arm([b_id], self.armed(a_id))

    # ------------------------------------------------------------------ recording
    def start_recording(self):
        if self.engine.recording or not self.ifaces:
            return
        if self.armed_channels() == 0:
            self.toast("Arm at least one channel first.")
            return
        if self.time_left() <= 0:
            self.toast("Not enough free space on the " + self.dest_name() + ".")
            return
        d = self.dest()
        rec_dir = self.rec_dir(d)
        num = storage.next_take_number(rec_dir)
        take_dir = storage.new_take_dir(rec_dir, num)
        tracks = []
        for g in self.groups():
            for s in g["strips"]:
                if not self.armed(s["ids"][0]):
                    continue
                f, n = s["f"], s["n"]
                if s["stereo"]:
                    tracks.append({"short": f.short, "chans": [n - 1, n], "file": f"{f.short}_{n:02d}-{n + 1:02d}.wav"})
                else:
                    tracks.append({"short": f.short, "chans": [n - 1], "file": f"{f.short}_{n:02d}.wav"})
        meta = {
            "name": f"Take {num}", "rate": self.S["rate"], "bits": self.S["bits"],
            "channels": self.armed_channels(), "tracks": len(tracks), "dest": self.dest_name(d),
            "interfaces": [f.name for f in self.ifaces], "clock": self.clock_master().name if self.ifaces else None,
            "track_list": [{"file": t["file"], "interface": next(f.name for f in self.ifaces if f.short == t["short"]),
                            "inputs": [c + 1 for c in t["chans"]]} for t in tracks],
        }
        try:
            self.engine.start_recording(take_dir, tracks, self.S["bits"], meta)
        except Exception as e:
            self.toast(f"Couldn't start recording: {e}"[:90], 6)
            return
        self.player.stop()
        self.rec_bps = self.bytes_per_sec()
        self.rec_dest = d
        self.last_disk = time.monotonic()

    def stop_recording(self, reason=None):
        meta = self.engine.stop_recording()
        self.refresh_disk()
        if meta:
            self.toast(reason or f"Saved {meta.get('name', 'take')} ({hms(meta.get('duration', 0))}) to the {meta.get('dest', 'drive')}")

    def clock_master(self):
        if not self.ifaces:
            return None
        for f in self.ifaces:
            if f.short == self.S["clock"]:
                return f
        return max(self.ifaces, key=lambda f: f.ins)

    # ------------------------------------------------------------------ main loop
    def run(self, screenshot_dir=None):
        shots = ["main", "settings", "takes", "share", "wifi", "wifipass"]
        shot_i, shot_t = 0, time.monotonic() + 2.5
        if screenshot_dir:
            os.makedirs(screenshot_dir, exist_ok=True)
        try:
            while self.running:
                now = time.monotonic()
                while True:
                    try:
                        self.ui_q.get_nowait()()
                    except queue.Empty:
                        break
                for ev in pygame.event.get():
                    self.handle(ev)
                self.update(now)
                self.draw(now)
                if self.fb:
                    self.fb.show(self.surf)
                else:
                    pygame.display.flip()
                if screenshot_dir and now >= shot_t:
                    pygame.image.save(self.surf, os.path.join(screenshot_dir, f"{shot_i}_{self.screen}.png"))
                    shot_i += 1
                    if shot_i >= len(shots):
                        break
                    nxt = shots[shot_i]
                    if nxt == "wifipass":
                        self.open_password("GigPhone", True)
                        self.pwd = "secret1"
                    else:
                        self.go(nxt)
                    shot_t = now + 1.5
                self.clock.tick(FPS)
        finally:
            if self.engine.recording:
                self.engine.stop_recording()
            self.player.stop()
            self.share.stop()
            self.engine.close()
            if self.fb:
                self.fb.clear()
                self.fb.close()
            pygame.quit()

    def update(self, now):
        peaks = self.engine.read_peaks()
        dt = 1.0 / FPS
        push = now - self.last_hist >= HIST_EVERY
        if push:
            self.last_hist = now
        for f in self.ifaces:
            p = peaks.get(f.short)
            for n in range(1, f.ins + 1):
                cid = self.chan_id(f, n)
                raw = float(p[n - 1]) if p is not None and n - 1 < len(p) else 0.0
                if raw >= CLIP:
                    self.peak_at[cid] = now
                lvl = max(to_meter(raw), self.levels.get(cid, 0.0) - dt * 0.33)
                self.levels[cid] = lvl
                if push:
                    h = self.hist.get(cid)
                    if h is None:
                        h = self.hist[cid] = collections.deque([0.0] * HIST_LEN, maxlen=HIST_LEN)
                    h.append(lvl)
        if now - self.last_disk >= DISK_EVERY:
            self.last_disk = now
            self.refresh_disk()
            if self.engine.recording and self.time_left() <= 0:
                self.stop_recording(reason="Drive is full. Recording stopped and saved.")
        if now - self.last_wifi >= 10:
            self.last_wifi = now
            self.refresh_wifi()
        self.check_hotplug(now)
        if self.player.done and self.playing_dir:
            self.player.stop()
            self.playing_dir = None

    def go(self, screen):
        if self.screen == "share" and screen != "share":
            self.share.stop()
        self.screen = screen
        if screen == "takes":
            self.takes = storage.list_takes(self.rec_dir())
            self.scroll["takes"] = 0
        elif screen == "wifi":
            self.scan_wifi()
        elif screen == "share" and self.wifi_ssid:
            ok, msg = self.share.start(self.rec_dir(), self.dest_name())
            if not ok:
                self.toast(msg, 6)

    # ------------------------------------------------------------------ input
    def handle(self, ev):
        if ev.type == pygame.QUIT:
            self.running = False
        elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
            self.running = False
        elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            region = None
            for name, rect, axis, mx in self.scroll_regions:
                if rect.collidepoint(ev.pos):
                    region = (name, axis, mx)
            self.press = {"pos": ev.pos, "region": region, "start": self.scroll.get(region[0], 0) if region else 0}
            self.dragging = False
        elif ev.type == pygame.MOUSEMOTION and self.press:
            dx = ev.pos[0] - self.press["pos"][0]
            dy = ev.pos[1] - self.press["pos"][1]
            reg = self.press["region"]
            if reg:
                name, axis, mx = reg
                d = dx if axis == "x" else dy
                if abs(d) > 12:
                    self.dragging = True
                if self.dragging:
                    self.scroll[name] = clamp(self.press["start"] - d, 0, mx)
        elif ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
            if self.press and not self.dragging:
                for rect, fn, enabled in reversed(self.hits):
                    if rect.collidepoint(ev.pos):
                        if enabled:
                            fn()
                        break
            self.press = None
            self.dragging = False

    # ------------------------------------------------------------------ drawing
    def draw(self, now):
        self.hits = []
        self.scroll_regions = []
        self.surf.fill(C["bg"])
        getattr(self, "draw_" + self.screen)(now)
        if self.toast_msg and now < self.toast_until:
            lines = self.wrap(self.toast_msg, 14, 560)
            h = 16 + 20 * len(lines)
            r = pygame.Rect(0, 0, 600, h)
            r.midbottom = (W // 2, H - 88 if self.screen == "main" else H - 12)
            self.rrect(r, (40, 45, 54), 10, (70, 78, 90))
            for i, ln in enumerate(lines):
                self.text(ln, (r.centerx, r.top + 8 + i * 20), 14, C["text"], anchor="midtop")

    def header(self, title, back=None, close=None):
        if back:
            r = self.button((12, 6, 44, 44), "", back)
            self.icon_chevron(r.centerx, r.centery, "left", C["text"], 8)
            self.text(title, (66, 28), 17, bold=True, anchor="midleft", maxw=520)
        else:
            self.text(title, (12, 28), 17, bold=True, anchor="midleft", maxw=520)
        if close:
            r = self.button((W - 56, 6, 44, 44), "", close)
            self.icon_close(r.centerx, r.centery, C["text"])

    # ---------- main screen
    def draw_main(self, now):
        rec = self.engine.recording
        # top bar
        pygame.draw.line(self.surf, C["line"], (0, 51), (W, 51))
        sr = self.button((W - 52, 4, 44, 44), "", lambda: self.go("settings"), enabled=not rec)
        self.icon_sliders(sr.centerx, sr.centery, C["text"])
        val = left_fmt(self.time_left())
        right = W - 60
        vw = self.tw(val, 18, True, True)
        lw = self.tw("Time remaining:", 13)
        self.text(val, (right, 7), 18, bold=True, mono=True, anchor="topright")
        self.text("Time remaining:", (right - vw - 6, 11), 13, C["soft"], anchor="topright")
        sub = f"on {self.dest_name()} · {rate_label(self.S['rate'])} / {bit_label(self.S['bits'])} · {self.armed_channels()} ch"
        sw = self.tw(sub, 12)
        self.text(sub, (right, 30), 12, C["muted"], anchor="topright")
        block_left = right - max(vw + 6 + lw, sw)
        wx = block_left - 22
        self.icon_wifi(wx, 22, C["text"] if self.wifi_ssid else C["muted"], off=not self.wifi_ssid)
        x, limit = 12, wx - 20
        for f in self.ifaces:
            io = f"{f.ins} in · {f.outs} out"
            full = 12 + 10 + 8 + self.tw(f.name, 13, True) + 8 + self.tw(io, 12) + 12
            short = 12 + 10 + 8 + self.tw(f.name, 13, True) + 12
            w = full if x + full <= limit else short
            if x + w > limit:
                break
            self.rrect((x, 8, w, 36), C["chip"], 8)
            pygame.draw.circle(self.surf, f.color, (x + 17, 26), 5)
            self.text(f.name, (x + 30, 26), 13, bold=True, anchor="midleft")
            if w == full:
                self.text(io, (x + 30 + self.tw(f.name, 13, True) + 8, 26), 12, C["muted"], anchor="midleft")
            x += w + 8

        # strips
        area = pygame.Rect(0, 52, W, 352)
        groups = self.groups()
        total = sum(g["w"] for g in groups) + 8 * max(0, len(groups) - 1) + 16
        arrows = total > W
        view = pygame.Rect(56, 52, W - 112, 352) if arrows else area
        maxs = max(0, total - view.w)
        self.scroll["strips"] = clamp(self.scroll["strips"], 0, maxs)
        if arrows:
            for side in ("left", "right"):
                r = pygame.Rect(0 if side == "left" else W - 56, 52, 56, 352)
                pygame.draw.rect(self.surf, C["dark"], r)
                pygame.draw.line(self.surf, C["line"], (r.right - 1 if side == "left" else r.left, 52), (r.right - 1 if side == "left" else r.left, 403))
                self.icon_chevron(r.centerx, r.centery, side, C["soft"], 14)
                step = -360 if side == "left" else 360
                self.hit(r, lambda st=step, m=maxs: self.scroll.__setitem__("strips", clamp(self.scroll["strips"] + st, 0, m)))
        self.scroll_regions.append(("strips", view, "x", maxs))
        if not self.ifaces:
            self.text("No USB audio interface found", area.center, 18, bold=True, anchor="center")
            self.text("Plug one in. It will appear here automatically.", (area.centerx, area.centery + 28), 14, C["muted"], anchor="center")
        self.surf.set_clip(view)
        x = view.left + 8 - self.scroll["strips"]
        for g in groups:
            if x + g["w"] >= view.left and x <= view.right:
                self.draw_group(g, x, 60, now, rec)
            x += g["w"] + 8
        self.surf.set_clip(None)

        # transport
        pygame.draw.rect(self.surf, C["dark"], (0, 404, W, 76))
        pygame.draw.line(self.surf, C["line"], (0, 404), (W, 404))
        can_rec = bool(self.ifaces) and self.armed_channels() > 0
        r = pygame.Rect(12, 414, 96, 56)
        if rec:
            self.rrect(r, C["recsolid"], 12)
            col = C["text"] if int(now * 2) % 2 == 0 else (255, 200, 200)
            pygame.draw.circle(self.surf, (255, 255, 255) if int(now * 2) % 2 == 0 else (230, 150, 150), (r.x + 30, r.centery), 8)
            self.text("REC", (r.x + 44, r.centery), 16, col, True, anchor="midleft")
        else:
            self.rrect(r, C["recbg"] if can_rec else C["btn"], 12, C["recb"] if can_rec else C["btnb"], 2)
            col = C["rectext"] if can_rec else C["muted"]
            pygame.draw.circle(self.surf, col, (r.x + 30, r.centery), 8)
            self.text("REC", (r.x + 44, r.centery), 16, col, True, anchor="midleft")
        self.hit(r, self.start_recording, enabled=can_rec and not rec)
        s = self.button((118, 414, 56, 56), "", self.stop_recording, enabled=rec, radius=12)
        self.icon_stop(s.centerx, s.centery, 16, C["text"])
        el = self.engine.elapsed()
        self.text(hms(el), (188, 416), 30, C["cliptext"] if rec else C["text"], True, True)
        xr = self.engine.xruns()
        status = (f"Recording {self.armed_channels()} ch to {self.dest_name()}" if rec
                  else f"{self.armed_channels()} of {sum(f.ins for f in self.ifaces)} channels armed")
        if xr:
            status += f" · {xr} dropouts"
        self.text(status, (188, 452), 12, C["muted"], maxw=260)
        # takes button
        tw_ = 36 + 18 + 10 + self.tw("Takes", 15, True) + 10 + 24
        tr = pygame.Rect(W - 12 - tw_, 414, tw_, 56)
        self.rrect(tr, C["btn"], 12, C["btnb"])
        self.icon_play(tr.x + 26, tr.centery, 16, C["text"])
        self.text("Takes", (tr.x + 46, tr.centery), 15, bold=True, anchor="midleft")
        cnt = str(len(storage.list_takes(self.rec_dir()))) if not rec and now - getattr(self, "_tc_t", 0) > 3 else getattr(self, "_tc", "0")
        if not rec and now - getattr(self, "_tc_t", 0) > 3:
            self._tc, self._tc_t = cnt, now
        b = pygame.Rect(tr.right - 18 - 24, tr.centery - 12, max(24, self.tw(cnt, 12) + 14), 24)
        self.rrect(b, C["btnb"], 12)
        self.text(cnt, b.center, 12, anchor="center")
        if rec:
            self.dim(tr, 12)
        self.hit(tr, lambda: self.go("takes"), enabled=not rec)
        # writing-to button
        dn = self.dest_name()
        ww = 28 + 20 + 10 + max(self.tw("Writing to:", 11), self.tw(dn, 14, True))
        wr = pygame.Rect(tr.left - 10 - ww, 414, ww, 56)
        self.rrect(wr, C["btn"], 12, C["btnb"])
        self.icon_drive(wr.x + 24, wr.centery, C["text"])
        self.text("Writing to:", (wr.x + 44, wr.centery - 2), 11, C["muted"], anchor="bottomleft")
        self.text(dn, (wr.x + 44, wr.centery), 14, bold=True, anchor="topleft")
        if rec:
            self.dim(wr, 12)
        self.hit(wr, lambda: self.go("settings"), enabled=not rec)

    def draw_group(self, g, x, y, now, rec):
        if g["pair"]:
            box = pygame.Rect(x, y, g["w"], 36)
            if g["stereo"]:
                self.rrect(box, C["stereobg"], 8, C["blue"], 2)
                label = "Stereo · device" if g.get("device") else "Stereo"
                lw = self.tw(label, 13, True)
                self.icon_link(box.centerx - lw // 2 - 8, box.centery, C["bluetext"])
                self.text(label, (box.centerx + 8, box.centery), 13, C["bluetext"], True, anchor="center")
            else:
                self.dashed_rect(box, C["dash"])
                self.text("Mono", (x + MONO_W // 2, box.centery), 12, C["soft"], True, anchor="center")
                self.text("Mono", (x + MONO_W + 8 + MONO_W // 2, box.centery), 12, C["soft"], True, anchor="center")
            if rec:
                self.dim(box)
            self.hit(box, lambda gg=g: self.toggle_link(gg), enabled=not rec)
        else:
            self.text("Mono", (x + g["w"] // 2, y + 18), 12, C["muted"], True, anchor="center")
        sx = x
        for s in g["strips"]:
            self.draw_strip(s, sx, y + 42, now, rec)
            sx += s["w"] + 8

    def draw_strip(self, s, x, y, now, rec):
        f, n, ids = s["f"], s["n"], s["ids"]
        w = s["w"]
        self.rrect((x, y, w, 294), C["panel"], 10)
        pygame.draw.rect(self.surf, f.color, (x + 8, y + 8, w - 16, 4), border_radius=2)
        label = f"{f.short} {n}–{n + 1}" if s["stereo"] else f"{f.short} {n}"
        self.text(label, (x + 8, y + 16), 14, bold=True, maxw=w - 16)
        # meters
        mh, mw = 160, 24
        my = y + 42
        total = len(ids) * mw + (len(ids) - 1) * 6
        mx = x + (w - total) // 2
        for k, cid in enumerate(ids):
            r = pygame.Rect(mx + k * (mw + 6), my, mw, mh)
            self.draw_meter(r, self.levels.get(cid, 0.0))
            if now - self.peak_at.get(cid, -99) < CLIP_HOLD:
                b = pygame.Rect(r.x + 1, r.y + 3, 22, 22)
                pygame.draw.circle(self.surf, C["clipbg"], b.center, 11)
                pygame.draw.circle(self.surf, C["red"], b.center, 11, 2)
                self.text("!", b.center, 14, C["cliptext"], True, anchor="center")
                self.hit(b.inflate(10, 10), lambda c=cid: self.peak_at.pop(c, None))
        # waveform
        wr = pygame.Rect(x + 8, y + 208, w - 16, 28)
        self.rrect(wr, C["well"], 4)
        armed = self.armed(ids[0])
        color = C["wavoff"] if not armed else (C["cliptext"] if rec else f.color)
        hs = [self.hist.get(c) for c in ids]
        if any(hs):
            vals = [max((h[i] if h else 0.0) for h in hs) for i in range(HIST_LEN)]
            step = (wr.w - 1) / (HIST_LEN - 1)
            mid = wr.centery
            top = [(wr.x + i * step, mid - v * 13) for i, v in enumerate(vals)]
            bot = [(wr.x + i * step, mid + v * 13) for i, v in reversed(list(enumerate(vals)))]
            pygame.draw.polygon(self.surf, color, top + bot)
        # arm button
        ar = pygame.Rect(x + 8, y + 242, w - 16, 44)
        if armed and rec:
            self.rrect(ar, C["recsolid"], 8, C["recsolid"], 2)
            col, lab = C["text"], "REC"
        elif armed:
            self.rrect(ar, C["recbg"], 8, C["recb"], 2)
            col, lab = C["rectext"], "Armed"
        else:
            self.rrect(ar, C["btn"], 8, C["btnb"], 2)
            col, lab = C["soft"], "Arm"
        lw = self.tw(lab, 13, True)
        pygame.draw.circle(self.surf, col, (ar.centerx - lw // 2 - 6, ar.centery), 5)
        self.text(lab, (ar.centerx + 5, ar.centery), 13, col, True, anchor="center")
        if rec and not armed:
            self.dim(ar)
        self.hit(ar, lambda i=list(ids), a=armed: self.toggle_arm(i, not a), enabled=not rec)

    def draw_meter(self, r, lvl):
        zones = ((0, GREEN_TOP, C["green"]), (GREEN_TOP, YELLOW_TOP, C["yellow"]), (YELLOW_TOP, 1.0, C["red"]))
        pygame.draw.rect(self.surf, C["well"], r, border_radius=4)
        for a, b, col in zones:
            dimc = tuple(int(c * 0.16 + 11 * 0.84) for c in col)
            y0, y1 = r.bottom - int(r.h * b), r.bottom - int(r.h * a)
            pygame.draw.rect(self.surf, dimc, (r.x, y0, r.w, y1 - y0))
            if lvl > a:
                top = r.bottom - int(r.h * min(lvl, b))
                pygame.draw.rect(self.surf, col, (r.x, top, r.w, y1 - top))

    # ---------- settings
    def opt_style(self, on):
        return (C["bluebg"], C["blue"], (255, 255, 255)) if on else (C["btn"], C["btnb"], C["soft"])

    def opt_button(self, rect, label, on, fn, sub=None, enabled=True):
        fill, border, col = self.opt_style(on)
        rect = pygame.Rect(rect)
        self.rrect(rect, fill, 8, border)
        if sub is None:
            self.text(label, rect.center, 14, col, True, anchor="center", maxw=rect.w - 8)
        else:
            self.text(label, (rect.x + 12, rect.centery - 1), 14, col, True, anchor="bottomleft", maxw=rect.w - 20)
            self.text(sub, (rect.x + 12, rect.centery + 2), 11, C["muted"] if not on else C["bluetext"], anchor="topleft", maxw=rect.w - 20)
        if not enabled:
            self.dim(rect)
        self.hit(rect, fn, enabled)

    def set_rate(self, rate):
        if rate == self.S["rate"]:
            return
        bad = self.engine.check_rate(self.ifaces, rate)
        if bad:
            self.toast(f"{', '.join(bad)} can't record at {rate_label(rate)}.")
            return
        self.S["rate"] = rate
        self.player.stop()
        errs = self.engine.open(self.ifaces, rate)
        if errs:
            self.toast(errs[0][:90], 6)

    def draw_settings(self, now):
        pygame.draw.rect(self.surf, C["sheet"], (0, 0, W, H))
        self.header("Recording settings", close=lambda: self.go("main"))
        lx, rx, cw = 12, 408, 380
        # left column
        self.label("Sample rate", (lx, 58))
        bw = (cw - 12) / 3
        for i, r in enumerate((44100, 48000, 96000)):
            self.opt_button((lx + i * (bw + 6), 76, bw, 40), rate_label(r), self.S["rate"] == r, lambda rr=r: self.set_rate(rr))
        self.label("Bit depth", (lx, 128))
        for i, b in enumerate((16, 24, 32)):
            self.opt_button((lx + i * (bw + 6), 146, bw, 40), bit_label(b), self.S["bits"] == b, lambda bb=b: self.S.__setitem__("bits", bb))
        self.label("Record to", (lx, 198))
        hw = (cw - 6) / 2
        d = self.dest()
        self.opt_button((lx, 216, hw, 46), "SD card", d == "sd", lambda: self.S.__setitem__("dest", "sd"),
                        sub=f"{self.available('sd') / 1e9:.1f} GB free")
        usb_ok = self.usb_root is not None
        self.opt_button((lx + hw + 6, 216, hw, 46), "USB drive", d == "usb", lambda: self.S.__setitem__("dest", "usb"),
                        sub=f"{self.available('usb') / 1e9:.1f} GB free" if usb_ok else "Not connected", enabled=usb_ok)
        # right column
        self.label("Wi-Fi", (rx, 58))
        wr = pygame.Rect(rx, 76, cw, 46)
        self.rrect(wr, C["btn"], 8, C["btnb"])
        self.icon_wifi(wr.x + 22, wr.centery - 2, C["text"], off=not self.wifi_ssid, scale=0.9)
        self.text(self.wifi_ssid or "Not connected", (wr.x + 44, wr.centery - 1), 14, bold=True, anchor="bottomleft", maxw=250)
        self.text(f"Connected · {self.wifi_ip}" if self.wifi_ssid else "Tap to choose a network",
                  (wr.x + 44, wr.centery + 2), 11, C["muted"])
        self.text("Change", (wr.right - 12, wr.centery), 13, C["link"], True, anchor="midright")
        self.hit(wr, lambda: self.go("wifi"))
        y = 134
        if len(self.ifaces) > 1:
            self.label("Clock source", (rx, y))
            master = self.clock_master()
            for i, f in enumerate(self.ifaces[:2] if len(self.ifaces) <= 2 else self.ifaces[:4]):
                cols = 2
                bx = rx + (i % cols) * (hw + 6)
                by = y + 18 + (i // cols) * 52
                self.opt_button((bx, by, hw, 46), f.name, f is master, lambda ff=f: self.S.__setitem__("clock", ff.short),
                                sub="Clock master" if f is master else f"{f.ins} in")
            rows = (min(len(self.ifaces), 4) + 1) // 2
            y = y + 18 + rows * 52
            for ln in self.wrap("Used as the timing reference. Tracks from other interfaces can drift slightly on long recordings.", 12, cw):
                self.text(ln, (rx, y), 12, C["muted"])
                y += 16
            y += 10
        card = pygame.Rect(rx, y, cw, 120)
        self.rrect(card, C["card"], 10)
        self.text(f"Writing to: {self.dest_name()}", (card.x + 14, card.y + 10), 14, bold=True)
        self.text("Time remaining:", (card.x + 14, card.y + 38), 13, C["soft"], anchor="midleft")
        self.text(left_fmt(self.time_left()), (card.x + 14 + self.tw("Time remaining:", 13) + 8, card.y + 38), 18, bold=True, mono=True, anchor="midleft")
        tb = self.trash[self.dest()]
        free_line = f"{self.available(self.dest()) / 1e9:.1f} GB free"
        if tb > 0:
            free_line += f" (incl. {tb / 1e9:.1f} GB in Trash)"
        self.text(f"{free_line} · {self.S['reserve_gb']:.1f} GB buffer",
                  (card.x + 14, card.y + 56), 12, C["soft"], maxw=cw - 28)
        bps = self.bytes_per_sec()
        line = (f"Writing {bps / 1e6:.2f} MB/s for {self.armed_channels()} channels at {rate_label(self.S['rate'])} / {bit_label(self.S['bits'])}"
                if bps else "No channels armed")
        for i, ln in enumerate(self.wrap(line, 12, cw - 28)[:2]):
            self.text(ln, (card.x + 14, card.y + 76 + i * 16), 12, C["muted"])

    # ---------- takes
    def out_options(self):
        opts = []
        if self.headphones >= 0 or self.simulate:
            opts.append(("hp", "Headphone jack"))
        for f in self.ifaces:
            if f.outs >= 2:
                opts.append((f.short, f"{f.name} out 1–2"))
        return opts

    def play_take(self, t):
        if self.playing_dir == t["dir"]:
            self.player.stop()
            self.playing_dir = None
            return
        opts = self.out_options()
        if not opts:
            self.toast("No playback output available.")
            return
        sel = self.S["out"] if any(o[0] == self.S["out"] for o in opts) else opts[0][0]
        if sel == "hp":
            idx, ch = self.headphones, 2
        else:
            f = next(f for f in self.ifaces if f.short == sel)
            idx, ch = f.out_index, f.outs
        err = self.player.play(t, idx, ch)
        if err:
            self.toast(err[:120], 6)
            self.playing_dir = None
        else:
            self.playing_dir = t["dir"]

    def draw_takes(self, now):
        pygame.draw.rect(self.surf, C["sheet"], (0, 0, W, H))
        self.header("Takes", close=lambda: (self.player.stop(), setattr(self, "playing_dir", None), self.go("main")))
        sw = 28 + 20 + 8 + self.tw("Send to computer", 14, True)
        sr = pygame.Rect(W - 56 - 8 - sw, 6, sw, 44)
        self.rrect(sr, C["btn"], 10, C["btnb"])
        self.icon_wifi(sr.x + 22, sr.centery - 2, C["text"], scale=0.8)
        self.text("Send to computer", (sr.x + 40, sr.centery), 14, bold=True, anchor="midleft")
        self.hit(sr, lambda: self.go("share"))
        self.label("Play through", (12, 60))
        opts = self.out_options()
        sel = self.S["out"] if any(o[0] == self.S["out"] for o in opts) else (opts[0][0] if opts else None)
        x = 12
        for key, lab in opts:
            w = self.tw(lab, 14, True) + 32
            self.opt_button((x, 78, w, 44), lab, key == sel, lambda k=key: self.S.__setitem__("out", k))
            x += w + 8
        self.label(f"Recordings on {self.dest_name()} · one WAV per track", (12, 134))
        area = pygame.Rect(12, 152, W - 24, H - 164)
        if not self.takes:
            self.text("No takes yet. Press REC to make one.", (W // 2, 200), 14, C["muted"], anchor="center")
            return
        rowh = 72
        total = len(self.takes) * (rowh + 8)
        maxs = max(0, total - area.h)
        self.scroll["takes"] = clamp(self.scroll["takes"], 0, maxs)
        self.scroll_regions.append(("takes", area, "y", maxs))
        self.surf.set_clip(area)
        y = area.y - self.scroll["takes"]
        for t in self.takes:
            if y + rowh >= area.y and y <= area.bottom:
                r = pygame.Rect(area.x, y, area.w, rowh)
                self.rrect(r, C["card"], 12)
                playing = self.playing_dir == t["dir"]
                dur = float(t.get("duration", 0))
                self.text(t.get("name", "Take"), (r.x + 14, r.y + 10), 15, bold=True)
                pos = f"{hms(self.player.position())} / {hms(dur)}" if playing else hms(dur)
                self.text(pos, (r.x + 24 + self.tw(t.get("name", "Take"), 15, True), r.y + 12), 13, C["soft"], mono=True)
                meta = (f"{t.get('channels', '?')} ch · {rate_label(int(t.get('rate', 48000)))} / {bit_label(int(t.get('bits', 24)))}"
                        f" · {len(t.get('files', []))} files · {str(t.get('created', ''))[:16]}")
                self.text(meta, (r.x + 14, r.y + 34), 12, C["muted"], maxw=r.w - 100)
                bar = pygame.Rect(r.x + 14, r.y + 56, r.w - 100, 4)
                self.rrect(bar, C["btnb"], 2)
                if playing and dur > 0:
                    self.rrect((bar.x, bar.y, int(bar.w * min(1, self.player.position() / dur)), 4), C["blue"], 2)
                c = (r.right - 38, r.centery)
                pygame.draw.circle(self.surf, C["blue"], c, 24)
                if playing:
                    self.icon_stop(c[0], c[1], 14, C["bg"])
                else:
                    self.icon_play(c[0] + 2, c[1], 18, C["bg"])
                self.hit(pygame.Rect(c[0] - 26, c[1] - 26, 52, 52), lambda tt=t: self.play_take(tt))
            y += rowh + 8
        self.surf.set_clip(None)

    # ---------- share
    def draw_share(self, now):
        pygame.draw.rect(self.surf, C["sheet"], (0, 0, W, H))
        self.header("Send to computer", back=lambda: self.go("takes"))
        if not self.wifi_ssid:
            self.text("Not connected to Wi-Fi", (W // 2, 190), 18, bold=True, anchor="center")
            self.text("Connect the recorder to the same Wi-Fi network as your computer to send files.",
                      (W // 2, 222), 14, C["soft"], anchor="center")
            self.button((W // 2 - 130, 256, 260, 48), "Choose a Wi-Fi network", lambda: self.go("wifi"),
                        fill=C["bluebg"], border=C["blue"])
            return
        if not self.share.running:
            ok, msg = self.share.start(self.rec_dir(), self.dest_name())
            if not ok:
                self.toast(msg, 6)
        on = int(now * 2) % 2 == 0
        pygame.draw.circle(self.surf, C["green"] if on else (40, 120, 80), (18, 68), 5)
        self.text(f"Sharing {self.dest_name()} recordings on {self.wifi_ssid}", (30, 68), 14, C["soft"], anchor="midleft", maxw=740)
        cw = (W - 24 - 12) // 2
        a = pygame.Rect(12, 86, cw, 330)
        b = pygame.Rect(12 + cw + 12, 86, cw, 330)
        for r in (a, b):
            self.rrect(r, C["card"], 12)
        url = f"http://{self.host}.local:{PORT}"
        ip_url = f"http://{self.wifi_ip}:{PORT}" if self.wifi_ip else ""
        x, y = a.x + 16, a.y + 14
        self.label("Easiest: web browser", (x, y))
        y += 26
        for ln in self.wrap(f"1. Connect your computer to {self.wifi_ssid}.", 14, cw - 32):
            self.text(ln, (x, y), 14, C["soft"]); y += 20
        y += 6
        self.text("2. Open a web browser and go to:", (x, y), 14, C["soft"]); y += 26
        self.text(url, (x, y), 18, C["link"], True, True, maxw=cw - 32); y += 30
        if ip_url:
            self.text(f"If that page won't load, use {ip_url}", (x, y), 12, C["muted"], maxw=cw - 32); y += 26
        for ln in self.wrap("3. Click a take to download its WAV files (one per track).", 14, cw - 32):
            self.text(ln, (x, y), 14, C["soft"]); y += 20
        x, y = b.x + 16, b.y + 14
        self.label("Command line (Mac, Linux, Windows)", (x, y))
        y += 26
        for ln in self.wrap("Open Terminal (or PowerShell on Windows) and run this to copy every take into the current folder:", 14, cw - 32):
            self.text(ln, (x, y), 14, C["soft"]); y += 20
        y += 8
        path = "~/recordings" if self.dest() == "sd" else self.rec_dir()
        cmd = f"scp -r {self.user}@{self.host}.local:{path} ."
        lines, cur = [], ""
        for ch in cmd:
            if self.tw(cur + ch, 13, mono=True) > cw - 56:
                lines.append(cur)
                cur = ""
            cur += ch
        lines.append(cur)
        box = pygame.Rect(x, y, cw - 32, 20 + 18 * len(lines))
        self.rrect(box, C["well"], 8)
        for i, ln in enumerate(lines):
            self.text(ln, (box.x + 12, box.y + 10 + i * 18), 13, C["text"], mono=True)
        y = box.bottom + 12
        self.text("When asked, type the Pi's login password.", (x, y), 12, C["muted"])
        self.text("Sharing stops when you leave this screen.", (12, 446), 12, C["muted"], anchor="midleft")
        self.button((W - 12 - 150, 424, 150, 44), "Stop sharing", lambda: self.go("takes"),
                    fill=C["recbg"], border=C["recb"], color=C["rectext"])

    # ---------- wifi list
    def scan_wifi(self):
        if self.scanning:
            return
        self.scanning = True

        def done(res):
            self.scanning = False
            if not isinstance(res, Exception):
                self.nets = res
        self.bg(self.wifi.scan, done)

    def open_password(self, ssid, secure):
        self.pending, self.pending_secure = ssid, secure
        self.pwd, self.kb_shift, self.kb_sym, self.show_pwd, self.wifi_err = "", False, False, False, ""
        self.screen = "wifipass"

    def do_connect(self, ssid, pwd):
        if self.connecting:
            return
        if self.pending_secure and len(pwd) < 8:
            self.wifi_err = "Wi-Fi passwords are at least 8 characters."
            return
        self.connecting = True
        self.wifi_err = ""

        def done(res):
            self.connecting = False
            ok, msg = res if not isinstance(res, Exception) else (False, str(res))
            if ok:
                self.pwd = ""
                self.toast(msg)
                self.refresh_wifi()
                self.screen = "settings"
            else:
                self.wifi_err = msg
        self.bg(lambda: self.wifi.connect(ssid, pwd or None), done)

    def disconnect_wifi(self):
        def done(res):
            self.refresh_wifi()
            self.scan_wifi()
        self.bg(self.wifi.disconnect, done)
        self.wifi_ssid = None

    def draw_wifi(self, now):
        pygame.draw.rect(self.surf, C["sheet"], (0, 0, W, H))
        self.header("Wi-Fi networks", back=lambda: self.go("settings"))
        lab = "Scanning…" if self.scanning else "Scan again"
        bw = self.tw(lab, 14, True) + 28
        self.button((W - 12 - bw, 6, bw, 44), lab, self.scan_wifi)
        area = pygame.Rect(12, 60, W - 24, H - 72)
        if not self.nets:
            self.text("Looking for networks…" if self.scanning else "No networks found.", (W // 2, 200), 14, C["muted"], anchor="center")
            return
        total = len(self.nets) * 64
        maxs = max(0, total - area.h)
        self.scroll["nets"] = clamp(self.scroll["nets"], 0, maxs)
        self.scroll_regions.append(("nets", area, "y", maxs))
        self.surf.set_clip(area)
        y = area.y - self.scroll["nets"]
        for nw in self.nets:
            on = nw["ssid"] == self.wifi_ssid
            rw = area.w - (128 if on else 0)
            r = pygame.Rect(area.x, y, rw, 56)
            fill, border, col = self.opt_style(on)
            self.rrect(r, fill, 10, border)
            self.icon_wifi(r.x + 24, r.centery - 2, col)
            self.text(nw["ssid"], (r.x + 48, r.centery - 1), 15, col, True, anchor="bottomleft", maxw=rw - 100)
            word = self.wifi.signal_word(nw["signal"])
            sub = f"Connected · {word}" if on else (("Secured · " if nw["secure"] else "Open · ") + word)
            self.text(sub, (r.x + 48, r.centery + 2), 12, C["muted"] if not on else C["bluetext"])
            if nw["secure"]:
                self.icon_lock(r.right - 22, r.centery, C["muted"])
            if not on:
                self.hit(r, lambda n=nw: (self.open_password(n["ssid"], True) if n["secure"]
                                         else (setattr(self, "pending_secure", False), self.do_connect(n["ssid"], ""))))
            else:
                self.button((r.right + 8, y, 120, 56), "Disconnect", self.disconnect_wifi)
            y += 64
        self.surf.set_clip(None)

    # ---------- password + keyboard
    def kb_type(self, ch):
        self.pwd = (self.pwd + ch)[:63]
        self.kb_shift = False
        self.wifi_err = ""

    def draw_wifipass(self, now):
        pygame.draw.rect(self.surf, C["sheet"], (0, 0, W, H))
        self.header(f"Password for {self.pending}", back=lambda: self.go("wifi"))
        field = pygame.Rect(12, 58, W - 24 - 80, 48)
        self.rrect(field, C["well"], 10, C["dash"])
        shown = self.pwd if self.show_pwd else "•" * len(self.pwd)
        if not self.pwd:
            self.text("Type the network password", (field.x + 14, field.centery), 15, C["muted"], anchor="midleft")
            cx = field.x + 14
        else:
            t = self.text(shown[-40:], (field.x + 14, field.centery), 18, mono=True, anchor="midleft")
            cx = t.right + 2
        if int(now * 2) % 2 == 0:
            pygame.draw.rect(self.surf, C["blue"], (cx, field.centery - 11, 2, 22))
        self.button((field.right + 8, 58, 72, 48), "Hide" if self.show_pwd else "Show",
                    lambda: setattr(self, "show_pwd", not self.show_pwd))
        if self.wifi_err:
            self.text(self.wifi_err, (12, 112), 12, C["rectext"], maxw=W - 24)
        up = (lambda c: c.upper()) if self.kb_shift else (lambda c: c)
        rows = [[(c, 1, lambda ch=c: self.kb_type(ch)) for c in "1234567890"]]
        bs = ("⌫", 1.5, lambda: setattr(self, "pwd", self.pwd[:-1]))
        if not self.kb_sym:
            rows.append([(up(c), 1, lambda ch=c: self.kb_type(up(ch))) for c in "qwertyuiop"])
            rows.append([(up(c), 1, lambda ch=c: self.kb_type(up(ch))) for c in "asdfghjkl"])
            rows.append([("⇧", 1.5, lambda: setattr(self, "kb_shift", not self.kb_shift))] +
                        [(up(c), 1, lambda ch=c: self.kb_type(up(ch))) for c in "zxcvbnm"] + [bs])
        else:
            rows.append([(c, 1, lambda ch=c: self.kb_type(ch)) for c in "!@#$%^&*()"])
            rows.append([(c, 1, lambda ch=c: self.kb_type(ch)) for c in "-_=+[]{};:"])
            rows.append([(c, 1, lambda ch=c: self.kb_type(ch)) for c in "~`'\"<>\\|/"] + [bs])
        rows.append([
            ("ABC" if self.kb_sym else "#+=", 1.5, lambda: (setattr(self, "kb_sym", not self.kb_sym), setattr(self, "kb_shift", False))),
            (",", 1, lambda: self.kb_type(",")),
            ("space", 4, lambda: self.kb_type(" ")),
            (".", 1, lambda: self.kb_type(".")),
            ("Connecting…" if self.connecting else "Connect", 2.5, lambda: self.do_connect(self.pending, self.pwd)),
        ])
        top, bottom, gap = 132, H - 10, 6
        rh = (bottom - top - gap * (len(rows) - 1)) / len(rows)
        for ri, row in enumerate(rows):
            units = sum(u for _, u, _ in row)
            avail = W - 24 - gap * (len(row) - 1)
            x = 12
            y = top + ri * (rh + gap)
            for lab, u, fn in row:
                w = avail * u / units
                r = pygame.Rect(round(x), round(y), round(w), round(rh))
                special = lab in ("⇧", "⌫", "#+=", "ABC", "space")
                if lab in ("Connect", "Connecting…"):
                    self.rrect(r, C["blue"], 8)
                    self.text(lab, r.center, 15, C["bg"], True, anchor="center")
                else:
                    on = lab == "⇧" and self.kb_shift
                    fill = C["bluebg"] if on else (C["dark"] if special else C["key"])
                    self.rrect(r, fill, 8, C["blue"] if on else (C["btnb"] if special else C["keyb"]))
                    self.text(lab, r.center, 14 if special and len(lab) > 1 else 18, C["soft"] if special else C["text"],
                              bold=special, anchor="center")
                self.hit(r, fn)
                x += w + gap
