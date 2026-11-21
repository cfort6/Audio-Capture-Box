"""Finding USB audio interfaces and giving them short names."""

import re
from dataclasses import dataclass, field

# Brand words dropped from the front of a product name before abbreviating.
BRANDS = {
    "behringer", "focusrite", "tascam", "zoom", "presonus", "yamaha", "soundcraft",
    "allen", "heath", "mackie", "roland", "motu", "audient", "steinberg", "midas",
    "native", "instruments", "m-audio", "maudio", "esi", "arturia", "ssl", "universal",
}

# Built-in Pi audio devices we never treat as recording interfaces.
BUILTIN = ("bcm2835", "vc4-hdmi", "vc4hdmi", "headphones", "hdmi")

# Interfaces known to send a stereo pair on certain channels (odd channel number).
KNOWN_STEREO = {
    "XR18": [17],
    "X18": [17],
    "XR16": [15],
    "XR12": [11],
}


def abbrev(name):
    """Short label for channel strips.

    1) drop leading brand words, 2) one word: keep it if <= 5 characters, otherwise
    the letters before the first digit (UMC404HD -> UMC), 3) several words: first
    letter of each word, words with digits kept whole (Model 12 -> M12,
    Scarlett 2i2 -> S2i2), 4) max 5 characters.
    """
    words = [w for w in re.sub(r"[^A-Za-z0-9 ]", " ", name).split() if w]
    while len(words) > 1 and words[0].lower() in BRANDS:
        words = words[1:]
    if not words:
        return "IF"
    if len(words) == 1:
        w = words[0]
        m = re.match(r"[A-Za-z]+", w)
        lead = m.group(0) if m else ""
        out = w if len(w) <= 5 else (lead if len(lead) >= 2 else w)
    else:
        out = "".join(w if re.search(r"[0-9]", w) else w[0].upper() for w in words)
    return out[:5]


def clean_name(card_name):
    """'UMC404HD 192k' -> 'UMC404HD' ; 'Scarlett 2i2 USB' -> 'Scarlett 2i2'."""
    n = re.sub(r"\b(192k|96k|48k|USB Audio|USB)\b", " ", card_name, flags=re.I)
    n = re.sub(r"\s+", " ", n).strip()
    return n or card_name.strip()


@dataclass
class Interface:
    name: str              # display name, e.g. "UMC404HD"
    short: str             # strip label, e.g. "UMC"
    card: str              # ALSA card name as PortAudio reports it
    in_index: int          # sounddevice device index for capture
    out_index: int         # sounddevice device index for playback (-1 if none)
    ins: int
    outs: int
    stereo: list = field(default_factory=list)  # odd channel numbers reported as stereo pairs

    @property
    def key(self):
        return self.card


def dedupe_shorts(ifaces):
    seen = {}
    for f in ifaces:
        base = f.short
        seen[base] = seen.get(base, 0) + 1
        if seen[base] > 1:
            f.short = f"{base}-{seen[base]}"
    return ifaces


def discover(sd):
    """Return (interfaces, headphone_out_index) using the sounddevice module."""
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception:
        return [], -1
    alsa = None
    for i, h in enumerate(hostapis):
        if "alsa" in h["name"].lower():
            alsa = i
    by_card = {}
    headphones = -1
    for idx, d in enumerate(devices):
        if alsa is not None and d["hostapi"] != alsa:
            continue
        name = d["name"]
        m = re.match(r"(.+?): .*\(hw:(\d+),(\d+)\)", name)
        if not m:
            continue
        card_name, card_num, dev_num = m.group(1), m.group(2), m.group(3)
        low = name.lower()
        if any(b in low for b in BUILTIN):
            if "headphones" in low and d["max_output_channels"] >= 2 and headphones < 0:
                headphones = idx
            continue
        if dev_num != "0":
            continue
        by_card[card_num] = (card_name, idx, d)
    ifaces = []
    for card_num in sorted(by_card, key=int):
        card_name, idx, d = by_card[card_num]
        if d["max_input_channels"] < 1:
            continue
        nice = clean_name(card_name)
        short = abbrev(nice)
        stereo = []
        for k, v in KNOWN_STEREO.items():
            if k.lower() in nice.lower().replace(" ", ""):
                stereo = list(v)
        ifaces.append(Interface(
            name=nice, short=short, card=card_name, in_index=idx,
            out_index=idx if d["max_output_channels"] >= 2 else -1,
            ins=int(d["max_input_channels"]), outs=int(d["max_output_channels"]),
            stereo=stereo,
        ))
    return dedupe_shorts(ifaces), headphones


def cards_signature():
    """Cheap check for plugged/unplugged sound cards."""
    try:
        with open("/proc/asound/cards") as f:
            return f.read()
    except OSError:
        return ""
