"""Audio engine: one input stream per interface, level meters, recording, playback.

Recording runs in a writer thread per interface so the audio callback never
touches the disk. Each track (mono, or a linked stereo pair) gets its own WAV.
"""

import json
import os
import queue
import threading
import time

import numpy as np
import soundfile

SUBTYPES = {16: "PCM_16", 24: "PCM_24", 32: "FLOAT"}
BYTES_PER_SAMPLE = {16: 2, 24: 3, 32: 4}
WAV_LIMIT = 4_000_000_000          # plain WAV files can't go past ~4 GB
PLAN_HOURS = 6                      # use RF64 if a file could pass 4 GB in this time
BLOCK = 1024


class _StreamState:
    def __init__(self, iface):
        self.iface = iface
        self.stream = None
        self.peaks = np.zeros(iface.ins, dtype=np.float32)
        self.rec_q = None
        self.writer = None
        self.files = []
        self.frames = 0
        self.xruns = 0
        self.error = ""


class Engine:
    def __init__(self, sd):
        self.sd = sd
        self.states = {}           # short -> _StreamState
        self.rate = 48000
        self.recording = False
        self.rec_started = 0.0
        self.take_dir = None
        self.take_meta = None

    # ---------- streams ----------
    def check_rate(self, ifaces, rate):
        """Return the names of interfaces that can't run at this sample rate."""
        bad = []
        for f in ifaces:
            try:
                self.sd.check_input_settings(device=f.in_index, channels=f.ins, samplerate=rate, dtype="float32")
            except Exception:
                bad.append(f.name)
        return bad

    def open(self, ifaces, rate):
        """(Re)open one input stream per interface. Returns a list of error strings."""
        self.close()
        self.rate = rate
        errors = []
        for f in ifaces:
            st = _StreamState(f)
            try:
                st.stream = self.sd.InputStream(
                    device=f.in_index, channels=f.ins, samplerate=rate, dtype="float32",
                    blocksize=BLOCK, latency="high", callback=self._make_callback(st))
                st.stream.start()
            except Exception as e:
                st.error = str(e)
                st.stream = None
                errors.append(f"{f.name}: {e}")
            self.states[f.short] = st
        return errors

    def close(self):
        if self.recording:
            self.stop_recording()
        for st in self.states.values():
            if st.stream is not None:
                try:
                    st.stream.stop()
                    st.stream.close()
                except Exception:
                    pass
        self.states = {}

    def _make_callback(self, st):
        def cb(indata, frames, t, status):
            if status and getattr(status, "input_overflow", False):
                st.xruns += 1
            np.maximum(st.peaks, np.abs(indata).max(axis=0), out=st.peaks)
            q = st.rec_q
            if q is not None:
                q.put(indata.copy())
        return cb

    def read_peaks(self):
        """Peak (0..1) per channel since the last call, keyed by interface short name."""
        out = {}
        for short, st in self.states.items():
            p = st.peaks.copy()
            st.peaks[:] = 0
            out[short] = p
        return out

    def dead_streams(self):
        """Interfaces whose stream stopped unexpectedly (e.g. unplugged)."""
        dead = []
        for short, st in self.states.items():
            if st.stream is not None:
                try:
                    if not st.stream.active:
                        dead.append(short)
                except Exception:
                    dead.append(short)
        return dead

    def xruns(self):
        return sum(st.xruns for st in self.states.values())

    # ---------- recording ----------
    def start_recording(self, take_dir, tracks, bits, meta):
        """tracks: list of dicts {short, chans: [0-based channel indexes], file}."""
        subtype = SUBTYPES[bits]
        bps = BYTES_PER_SAMPLE[bits]
        by_iface = {}
        for t in tracks:
            by_iface.setdefault(t["short"], []).append(t)
        try:
            for short, tlist in by_iface.items():
                st = self.states.get(short)
                if st is None or st.stream is None:
                    continue
                st.files = []
                st.frames = 0
                for t in tlist:
                    nch = len(t["chans"])
                    projected = self.rate * bps * nch * 3600 * PLAN_HOURS
                    fmt = "RF64" if projected > WAV_LIMIT else "WAV"
                    f = soundfile.SoundFile(os.path.join(take_dir, t["file"]), mode="w",
                                            samplerate=self.rate, channels=nch, subtype=subtype, format=fmt)
                    st.files.append((list(t["chans"]), f))
                q = queue.Queue()
                st.writer = threading.Thread(target=self._writer, args=(st, q), daemon=True)
                st.writer.start()
                st.rec_q = q
        except Exception:
            self._close_files()
            raise
        self.recording = True
        self.rec_started = time.monotonic()
        self.take_dir = take_dir
        self.take_meta = dict(meta)
        self.take_meta["created"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._write_meta(duration=0)

    def _writer(self, st, q):
        last_flush = time.monotonic()
        while True:
            block = q.get()
            if block is None:
                break
            for chans, f in st.files:
                f.write(block[:, chans])
            st.frames += len(block)
            now = time.monotonic()
            if now - last_flush > 2.0:      # limit what a power cut can lose
                for _, f in st.files:
                    f.flush()
                last_flush = now

    def stop_recording(self):
        if not self.recording:
            return None
        frames = 0
        for st in self.states.values():
            q = st.rec_q
            st.rec_q = None
            if q is not None:
                q.put(None)
        for st in self.states.values():
            if st.writer is not None:
                st.writer.join(timeout=10)
                st.writer = None
            frames = max(frames, st.frames)
        self._close_files()
        self.recording = False
        duration = frames / float(self.rate) if self.rate else 0
        self._write_meta(duration=duration)
        meta = dict(self.take_meta or {})
        meta["duration"] = duration
        meta["dir"] = self.take_dir
        return meta

    def _close_files(self):
        for st in self.states.values():
            for _, f in st.files:
                try:
                    f.close()
                except Exception:
                    pass
            st.files = []

    def _write_meta(self, duration):
        if not self.take_dir or self.take_meta is None:
            return
        m = dict(self.take_meta)
        m["duration"] = round(duration, 2)
        try:
            with open(os.path.join(self.take_dir, "take.json"), "w") as f:
                json.dump(m, f, indent=2)
        except OSError:
            pass

    def elapsed(self):
        return time.monotonic() - self.rec_started if self.recording else 0.0


class Player:
    """Plays every track of a take, mixed down to stereo."""

    def __init__(self, sd):
        self.sd = sd
        self.stream = None
        self.files = []
        self.pos = 0
        self.rate = 48000
        self.duration = 0.0
        self.take_dir = None
        self.done = False

    @property
    def playing(self):
        return self.stream is not None and not self.done

    def play(self, take, device_index, device_channels):
        """Return '' on success or an error message."""
        self.stop()
        paths = [os.path.join(take["dir"], n) for n in take.get("files", [])]
        if not paths:
            return "This take has no audio files."
        try:
            self.files = [soundfile.SoundFile(p) for p in paths]
        except Exception as e:
            self.files = []
            return f"Couldn't open the take: {e}"
        self.rate = self.files[0].samplerate
        self.duration = max(f.frames for f in self.files) / float(self.rate)
        self.pos = 0
        self.done = False
        self.take_dir = take["dir"]
        nout = max(2, int(device_channels or 2))
        gain = 1.0 / max(1.0, np.sqrt(len(self.files)))
        files = self.files
        stop_exc = getattr(self.sd, "CallbackStop", Exception)

        def cb(outdata, frames, t, status):
            outdata.fill(0)
            alive = False
            for f in files:
                d = f.read(frames, dtype="float32", always_2d=True)
                n = len(d)
                if n == frames:
                    alive = True
                if n == 0:
                    continue
                if d.shape[1] >= 2:
                    outdata[:n, 0] += d[:, 0] * gain
                    outdata[:n, 1] += d[:, 1] * gain
                else:
                    outdata[:n, 0] += d[:, 0] * gain * 0.8
                    outdata[:n, 1] += d[:, 0] * gain * 0.8
            np.clip(outdata, -1.0, 1.0, out=outdata)
            self.pos += frames
            if not alive:
                self.done = True
                raise stop_exc()

        try:
            self.stream = self.sd.OutputStream(device=device_index, channels=nout, samplerate=self.rate,
                                               dtype="float32", blocksize=2048, latency="high", callback=cb)
            self.stream.start()
        except Exception as e:
            self.stop()
            return f"This output can't play the take ({e})."
        return ""

    def position(self):
        return min(self.duration, self.pos / float(self.rate)) if self.rate else 0

    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
        self.stream = None
        for f in self.files:
            try:
                f.close()
            except Exception:
                pass
        self.files = []
        self.take_dir = None
        self.done = False
