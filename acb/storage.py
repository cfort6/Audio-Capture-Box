"""Where recordings go: the SD card or a USB thumb drive."""

import json
import os
import shutil
import subprocess
import time

SD_ROOT = os.path.expanduser("~/recordings")


def _run(cmd, timeout=8):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def find_usb_drive(auto_mount=True):
    """Return the mount point of the first USB storage partition, or None.

    Raspberry Pi OS Lite doesn't mount USB drives on its own, so when we find an
    unmounted USB partition we ask udisks to mount it (allowed by our polkit rule).
    """
    r = _run(["lsblk", "-J", "-o", "NAME,TRAN,TYPE,FSTYPE,MOUNTPOINT,LABEL,SIZE"])
    if not r or r.returncode != 0:
        return None
    try:
        tree = json.loads(r.stdout)
    except ValueError:
        return None
    for disk in tree.get("blockdevices", []):
        if disk.get("tran") != "usb":
            continue
        parts = disk.get("children") or [disk]
        for p in parts:
            if not p.get("fstype"):
                continue
            mp = p.get("mountpoint")
            if mp:
                return mp
            if auto_mount:
                m = _run(["udisksctl", "mount", "--no-user-interaction", "-b", "/dev/" + p["name"]], timeout=15)
                if m and m.returncode == 0:
                    # "Mounted /dev/sda1 at /media/user/LABEL"
                    out = m.stdout.strip()
                    if " at " in out:
                        return out.split(" at ", 1)[1].rstrip(".")
    return None


def recordings_dir(dest_root):
    path = os.path.join(dest_root, "recordings") if dest_root != SD_ROOT else SD_ROOT
    os.makedirs(path, exist_ok=True)
    return path


def free_bytes(path):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def new_take_dir(rec_dir, take_number):
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    path = os.path.join(rec_dir, f"{stamp}_Take-{take_number}")
    os.makedirs(path, exist_ok=True)
    return path


def list_takes(rec_dir):
    """Newest first. Each take is a folder with take.json and one WAV per track."""
    takes = []
    try:
        names = sorted(os.listdir(rec_dir), reverse=True)
    except OSError:
        return takes
    for n in names:
        d = os.path.join(rec_dir, n)
        meta_path = os.path.join(d, "take.json")
        if not os.path.isfile(meta_path):
            continue
        try:
            with open(meta_path) as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        meta["dir"] = d
        meta["files"] = sorted(x for x in os.listdir(d) if x.lower().endswith(".wav"))
        takes.append(meta)
    return takes


def next_take_number(rec_dir):
    best = 0
    try:
        for n in os.listdir(rec_dir):
            if "_Take-" in n:
                try:
                    best = max(best, int(n.rsplit("_Take-", 1)[1]))
                except ValueError:
                    pass
    except OSError:
        pass
    return best + 1
