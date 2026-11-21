"""Wi-Fi control through NetworkManager's nmcli command."""

import subprocess


def _run(args, timeout=30):
    try:
        return subprocess.run(["nmcli"] + args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _split(line):
    """Split an `nmcli -t` line on ':' while honoring '\\:' escapes."""
    out, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def ip_address():
    try:
        r = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3)
        parts = r.stdout.split()
        return parts[0] if parts else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def status():
    """Return the SSID we're connected to, or None."""
    r = _run(["-t", "-f", "ACTIVE,SSID", "dev", "wifi"], timeout=8)
    if not r or r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        f = _split(line)
        if len(f) >= 2 and f[0] == "yes" and f[1]:
            return f[1]
    return None


def scan():
    """List nearby networks: [{'ssid','signal','secure','in_use'}], strongest first."""
    r = _run(["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "--rescan", "yes"], timeout=30)
    if not r or r.returncode != 0:
        return []
    nets = {}
    for line in r.stdout.splitlines():
        f = _split(line)
        if len(f) < 4 or not f[1]:
            continue
        ssid = f[1]
        try:
            sig = int(f[2])
        except ValueError:
            sig = 0
        sec = f[3].strip() not in ("", "--")
        cur = nets.get(ssid)
        if cur is None or sig > cur["signal"]:
            nets[ssid] = {"ssid": ssid, "signal": sig, "secure": sec, "in_use": f[0].strip() == "*"}
    return sorted(nets.values(), key=lambda n: (-n["in_use"], -n["signal"]))


def connect(ssid, password=None):
    """Return (ok, message)."""
    args = ["dev", "wifi", "connect", ssid]
    if password:
        args += ["password", password]
    r = _run(args, timeout=45)
    if r is None:
        return False, "Wi-Fi didn't respond. Try again."
    if r.returncode == 0:
        return True, "Connected to " + ssid
    err = (r.stderr or r.stdout).strip()
    if "Secrets were required" in err or "password" in err.lower():
        return False, "Wrong password for " + ssid
    return False, err.splitlines()[-1][:80] if err else "Couldn't connect to " + ssid


def disconnect():
    r = _run(["dev", "disconnect", "wlan0"], timeout=15)
    return bool(r and r.returncode == 0)


def signal_word(sig):
    if sig >= 67:
        return "Strong signal"
    if sig >= 34:
        return "Good signal"
    return "Weak signal"
