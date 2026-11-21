"""'Send to computer': a small web page for downloading recordings over Wi-Fi.

Routes:
  /                      list of takes, with a Download button for every file
  /file/<take>/<name>    download one file
  /zip/<take>            download a whole take as one .zip
"""

import html
import json
import os
import re
import shutil
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote

PORT = 8080
SAFE = re.compile(r"^[A-Za-z0-9._ -]+$")


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


def hms(sec):
    sec = int(sec or 0)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def describe_file(name, meta):
    """Plain-English label for a track file, e.g. 'UMC404HD · input 3 (mono)'."""
    for t in meta.get("track_list", []):
        if t.get("file") == name:
            ins = t.get("inputs", [])
            if len(ins) == 2:
                return f"{t.get('interface')} · inputs {ins[0]}–{ins[1]} (stereo)"
            if ins:
                return f"{t.get('interface')} · input {ins[0]} (mono)"
    m = re.match(r"(.+?)_(\d+)(?:-(\d+))?\.wav$", name, re.I)
    if m:
        if m.group(3):
            return f"{m.group(1)} · inputs {int(m.group(2))}–{int(m.group(3))} (stereo)"
        return f"{m.group(1)} · input {int(m.group(2))} (mono)"
    return ""


def load_takes(root):
    takes = []
    try:
        names = sorted(os.listdir(root), reverse=True)
    except OSError:
        return takes
    for n in names:
        d = os.path.join(root, n)
        if not os.path.isdir(d) or not SAFE.match(n):
            continue
        meta = {}
        try:
            with open(os.path.join(d, "take.json")) as f:
                meta = json.load(f)
        except (OSError, ValueError):
            pass
        files = []
        for fn in sorted(os.listdir(d)):
            if fn.lower().endswith(".wav") and SAFE.match(fn):
                try:
                    size = os.path.getsize(os.path.join(d, fn))
                except OSError:
                    continue
                files.append((fn, size))
        if files:
            takes.append((n, meta, files))
    return takes


STYLE = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#0E1013;color:#E8EAED;font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:960px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:26px;margin:0 0 4px}
.sub{color:#9AA0A6;margin:0 0 20px}
.help{background:#171A1F;border:1px solid #2C3138;border-radius:12px;padding:16px 18px;margin-bottom:24px}
.help h2{font-size:16px;margin:0 0 8px}
.help ul{margin:0;padding-left:20px;color:#C9CDD2}
.help li{margin:4px 0}
code{font-family:ui-monospace,Menlo,Consolas,monospace;background:#0B0D10;border-radius:5px;padding:1px 6px;color:#E8EAED}
.take{background:#171A1F;border-radius:12px;margin-bottom:18px;overflow:hidden}
.take-head{display:flex;flex-wrap:wrap;align-items:center;gap:12px;padding:16px 18px;border-bottom:1px solid #23272E}
.take-head h3{margin:0;font-size:19px}
.meta{color:#9AA0A6;font-size:13px}
.grow{flex:1 1 auto}
.btn{display:inline-flex;align-items:center;gap:8px;text-decoration:none;font-weight:700;font-size:14px;border-radius:9px;padding:9px 14px;white-space:nowrap}
.btn-primary{background:#5AA9FF;color:#0E1013}
.btn-primary:hover{background:#8CC4FF}
.btn-secondary{background:#1F2329;color:#E8EAED;border:1px solid #2C3138}
.btn-secondary:hover{border-color:#5AA9FF}
table{width:100%;border-collapse:collapse}
td{padding:10px 18px;border-top:1px solid #1F2329;vertical-align:middle}
tr:first-child td{border-top:none}
.fname{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:14px}
.desc{color:#9AA0A6;font-size:13px}
.size{color:#9AA0A6;font-size:13px;text-align:right;white-space:nowrap}
td.act{text-align:right;width:1%}
.empty{color:#9AA0A6;text-align:center;padding:40px 0}
@media (max-width:600px){td{padding:10px 12px}.size{display:none}}
"""

ICON_DL = ('<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" '
           'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>')


def render_index(root, where):
    takes = load_takes(root)
    e = html.escape
    out = [f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Audio Capture Box recordings</title><style>{STYLE}</style></head><body><main>
<h1>Recordings</h1>
<p class="sub">From your Audio Capture Box ({e(where)}). {len(takes)} take{'s' if len(takes) != 1 else ''}, newest first.</p>
<section class="help">
<h2>What these files are</h2>
<ul>
<li>Each <strong>take</strong> is one press of REC on the recorder. It's named after the date and time it started, like <code>2026-11-21_201530_Take-3</code>.</li>
<li>Each <strong>WAV file is one track</strong>: one input on your mixer or interface. <code>UMC_03.wav</code> is UMC404HD input 3. A name with two numbers, like <code>XR18_17-18.wav</code>, is a stereo pair.</li>
<li>Use <strong>Download whole take</strong> to get every track at once as a .zip. Unzip it, then drag all the WAVs into your recording software (DAW) together. They all start at the same moment, so they line up.</li>
</ul>
</section>
"""]
    if not takes:
        out.append('<p class="empty">No recordings yet. Press REC on the recorder to make one.</p>')
    for folder, meta, files in takes:
        total = sum(s for _, s in files)
        name = meta.get("name") or folder
        bits = meta.get("bits")
        fmt = []
        if meta.get("rate"):
            r = int(meta["rate"])
            fmt.append("44.1 kHz" if r == 44100 else f"{r // 1000} kHz")
        if bits:
            fmt.append("32-bit float" if int(bits) == 32 else f"{bits}-bit")
        info = " · ".join(x for x in [
            str(meta.get("created", ""))[:16], hms(meta.get("duration")) if meta.get("duration") else "",
            " / ".join(fmt), f"{len(files)} track{'s' if len(files) != 1 else ''}", human_size(total)] if x)
        out.append(f"""<section class="take">
<div class="take-head"><div class="grow"><h3>{e(name)}</h3><div class="meta">{e(info)}</div>
<div class="meta">Folder: <code>{e(folder)}</code></div></div>
<a class="btn btn-primary" href="/zip/{quote(folder)}" download>{ICON_DL}Download whole take (.zip)</a></div>
<table>""")
        for fn, size in files:
            desc = describe_file(fn, meta)
            out.append(f"""<tr><td><div class="fname">{e(fn)}</div><div class="desc">{e(desc)}</div></td>
<td class="size">{human_size(size)}</td>
<td class="act"><a class="btn btn-secondary" href="/file/{quote(folder)}/{quote(fn)}" download>{ICON_DL}Download</a></td></tr>""")
        out.append("</table></section>")
    out.append("</main></body></html>")
    return "".join(out).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    root = "."
    where = "SD card"

    def log_message(self, *args):
        pass

    def _safe_path(self, *parts):
        for p in parts:
            if not SAFE.match(p) or p in (".", ".."):
                return None
        path = os.path.realpath(os.path.join(self.root, *parts))
        if not path.startswith(os.path.realpath(self.root) + os.sep):
            return None
        return path

    def _not_found(self):
        self.send_error(404, "Not found")

    def do_GET(self):
        parts = [unquote(p) for p in self.path.split("?", 1)[0].split("/") if p]
        try:
            if not parts:
                body = render_index(self.root, self.where)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif parts[0] == "file" and len(parts) == 3:
                path = self._safe_path(parts[1], parts[2])
                if not path or not os.path.isfile(path):
                    return self._not_found()
                size = os.path.getsize(path)
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", f'attachment; filename="{parts[2]}"')
                self.end_headers()
                with open(path, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, 1024 * 1024)
            elif parts[0] == "zip" and len(parts) == 2:
                folder = self._safe_path(parts[1])
                if not folder or not os.path.isdir(folder):
                    return self._not_found()
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f'attachment; filename="{parts[1]}.zip"')
                self.end_headers()
                # WAVs barely compress, so store them as-is: fast on the Pi.
                with zipfile.ZipFile(self.wfile, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
                    for fn in sorted(os.listdir(folder)):
                        p = os.path.join(folder, fn)
                        if os.path.isfile(p) and (fn.lower().endswith(".wav") or fn == "take.json"):
                            z.write(p, arcname=f"{parts[1]}/{fn}")
            else:
                self._not_found()
        except (BrokenPipeError, ConnectionResetError):
            pass        # the browser cancelled the download


class Share:
    def __init__(self):
        self.server = None
        self.thread = None
        self.root = None

    @property
    def running(self):
        return self.server is not None

    def start(self, root, where="SD card", port=PORT):
        """Return (ok, message)."""
        if self.server and self.root == root:
            return True, ""
        self.stop()
        handler = type("Handler", (_Handler,), {"root": root, "where": where})
        try:
            self.server = ThreadingHTTPServer(("0.0.0.0", port), handler)
        except OSError as e:
            self.server = None
            return False, f"Couldn't start sharing: {e.strerror or e}"
        self.server.daemon_threads = True
        self.root = root
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return True, ""

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.server = None
        self.thread = None
        self.root = None
