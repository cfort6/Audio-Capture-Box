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
    names = []
    for d in (rec_dir, os.path.join(os.path.dirname(os.path.normpath(rec_dir)), "recordings-trash")):
        try:
            names += os.listdir(d)
        except OSError:
            pass
    try:
        for n in names:
            if "_Take-" in n:
                try:
                    best = max(best, int(n.rsplit("_Take-", 1)[1]))
                except ValueError:
                    pass
    except OSError:
        pass
    return best + 1


# ---------------------------------------------------------------- trash
# Deleted takes are moved to a "recordings-trash" folder next to "recordings".
# They still count as free space: the recorder clears them out (oldest first)
# when a recording needs the room.

def trash_dir(rec_dir):
    return os.path.join(os.path.dirname(os.path.normpath(rec_dir)), "recordings-trash")


def dir_size(path):
    total = 0
    for base, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return total


def _valid(folder):
    return folder and "/" not in folder and folder not in (".", "..")


def move_to_trash(rec_dir, folder):
    if not _valid(folder):
        return False
    src = os.path.join(rec_dir, folder)
    if not os.path.isdir(src):
        return False
    tdir = trash_dir(rec_dir)
    os.makedirs(tdir, exist_ok=True)
    dst = os.path.join(tdir, folder)
    i = 2
    while os.path.exists(dst):
        dst = os.path.join(tdir, f"{folder}-{i}")
        i += 1
    os.rename(src, dst)
    os.utime(dst)            # modification time = when it was deleted
    return True


def restore_from_trash(rec_dir, folder):
    if not _valid(folder):
        return False
    src = os.path.join(trash_dir(rec_dir), folder)
    if not os.path.isdir(src):
        return False
    dst = os.path.join(rec_dir, folder)
    i = 2
    while os.path.exists(dst):
        dst = os.path.join(rec_dir, f"{folder}-{i}")
        i += 1
    os.rename(src, dst)
    return True


def delete_forever(rec_dir, folder):
    if not _valid(folder):
        return False
    p = os.path.join(trash_dir(rec_dir), folder)
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
        return True
    return False


def list_trash(rec_dir):
    """[(folder, deleted_time, size_bytes)], oldest deleted first."""
    tdir = trash_dir(rec_dir)
    out = []
    try:
        for n in os.listdir(tdir):
            p = os.path.join(tdir, n)
            if os.path.isdir(p):
                out.append((n, os.path.getmtime(p), dir_size(p)))
    except OSError:
        pass
    return sorted(out, key=lambda t: t[1])


def trash_bytes(rec_dir):
    return sum(s for _, _, s in list_trash(rec_dir))


def empty_trash(rec_dir):
    for n, _, _ in list_trash(rec_dir):
        delete_forever(rec_dir, n)


def purge_oldest(rec_dir):
    """Permanently delete the oldest take in the trash. Returns bytes freed."""
    items = list_trash(rec_dir)
    if not items:
        return 0
    n, _, size = items[0]
    delete_forever(rec_dir, n)
    return size
