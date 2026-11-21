"""'Send to computer': a small web page for downloading recordings over Wi-Fi.

Routes:
  GET  /                      list of takes, with a Download button for every file
  GET  /trash                 takes in the trash
  GET  /file/<take>/<name>    download one file
  GET  /zip/<take>            download a whole take as one .zip
  POST /delete/<take>         move a take to the trash
  POST /restore/<take>        move a take back out of the trash
  POST /purge/<take>          delete a take in the trash for good
  POST /empty-trash           delete everything in the trash for good
"""

import html
import json
import os
import re
import shutil
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote

from . import storage

PORT = 8080
SAFE = re.compile(r"^[A-Za-z0-9._ -]+$")
MONTHS = ["Jan.", "Feb.", "March", "April", "May", "June", "July", "Aug.", "Sept.", "Oct.", "Nov.", "Dec."]


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


def nice_date(t):
    """struct_time -> 'Nov. 21, 2026, 12:51 PM'."""
    hour = t.tm_hour % 12 or 12
    ampm = "AM" if t.tm_hour < 12 else "PM"
    return f"{MONTHS[t.tm_mon - 1]} {t.tm_mday}, {t.tm_year}, {hour}:{t.tm_min:02d} {ampm}"


def take_date(meta, folder):
    for value, fmt in ((str(meta.get("created", "")), "%Y-%m-%d %H:%M:%S"), (folder[:17], "%Y-%m-%d_%H%M%S")):
        try:
            return nice_date(time.strptime(value, fmt))
        except ValueError:
            continue
    return ""


def format_line(meta):
    parts = []
    if meta.get("rate"):
        r = int(meta["rate"])
        parts.append("44.1 kHz" if r == 44100 else f"{r // 1000} kHz")
    if meta.get("bits"):
        b = int(meta["bits"])
        parts.append("32-bit float" if b == 32 else f"{b}-bit")
    return " / ".join(parts)


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


def read_take(d):
    meta = {}
    try:
        with open(os.path.join(d, "take.json")) as f:
            meta = json.load(f)
    except (OSError, ValueError):
        pass
    files = []
    try:
        for fn in sorted(os.listdir(d)):
            if fn.lower().endswith(".wav") and SAFE.match(fn):
                try:
                    files.append((fn, os.path.getsize(os.path.join(d, fn))))
                except OSError:
                    pass
    except OSError:
        pass
    return meta, files


def load_takes(root):
    takes = []
    try:
        names = sorted(os.listdir(root), reverse=True)
    except OSError:
        return takes
    for n in names:
        d = os.path.join(root, n)
        if os.path.isdir(d) and SAFE.match(n):
            meta, files = read_take(d)
            if files:
                takes.append((n, meta, files))
    return takes


STYLE = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#0E1013;color:#E8EAED;font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:960px;margin:0 auto;padding:28px 20px 60px}
.top{display:flex;align-items:flex-start;gap:12px;flex-wrap:wrap;margin-bottom:22px}
h1{font-size:28px;margin:0;line-height:1.2}
.sub{color:#9AA0A6;margin:2px 0 0}
.note{color:#9AA0A6;margin:-8px 0 22px;font-size:14px}
.grow{flex:1 1 auto}
code{font-family:ui-monospace,Menlo,Consolas,monospace;background:#0B0D10;border-radius:5px;padding:1px 6px;color:#E8EAED}
.take{background:#171A1F;border-radius:12px;margin-bottom:18px;overflow:hidden}
.take-head{display:flex;flex-wrap:wrap;align-items:flex-start;gap:12px;padding:16px 18px;border-bottom:1px solid #23272E}
.take-head h3{margin:0 0 4px;font-size:19px}
.line{color:#C9CDD2;font-size:14px}
.line.muted{color:#9AA0A6}
.actions{display:flex;gap:8px;flex-wrap:wrap}
.btn{display:inline-flex;align-items:center;gap:8px;text-decoration:none;font:inherit;font-weight:700;font-size:14px;border-radius:9px;padding:9px 14px;white-space:nowrap;cursor:pointer;border:1px solid transparent}
.btn-primary{background:#5AA9FF;color:#0E1013}
.btn-primary:hover{background:#8CC4FF}
.btn-secondary{background:#1F2329;color:#E8EAED;border-color:#2C3138}
.btn-secondary:hover{border-color:#5AA9FF}
.btn-danger{background:#C62828;color:#FFFFFF}
.btn-danger:hover{background:#D84343}
table{width:100%;border-collapse:collapse}
td{padding:10px 18px;border-top:1px solid #1F2329;vertical-align:middle}
tr:first-child td{border-top:none}
.fname{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:14px}
.desc{color:#9AA0A6;font-size:13px}
.size{color:#9AA0A6;font-size:13px;text-align:right;white-space:nowrap}
td.act{text-align:right;width:1%}
.empty{color:#9AA0A6;text-align:center;padding:40px 0}
dialog{background:#171A1F;color:#E8EAED;border:1px solid #2C3138;border-radius:14px;padding:22px;max-width:420px;width:calc(100% - 40px)}
dialog::backdrop{background:rgba(5,6,8,.72)}
dialog h2{margin:0 0 8px;font-size:20px}
dialog p{margin:0 0 18px;color:#C9CDD2}
dialog .actions{justify-content:flex-end}
form{margin:0}
@media (max-width:600px){td{padding:10px 12px}.size{display:none}}
"""

ICON_DL = ('<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" '
           'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>')
ICON_TRASH = ('<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" '
              'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg>')

CONFIRM = """
<dialog id="confirm">
  <h2>Are you sure?</h2>
  <p id="confirm-text"></p>
  <form method="post" id="confirm-form">
    <div class="actions">
      <button type="button" class="btn btn-secondary" onclick="document.getElementById('confirm').close()">Cancel</button>
      <button type="submit" class="btn btn-danger" id="confirm-go">Delete files</button>
    </div>
  </form>
</dialog>
<script>
function ask(action, text, go) {
  document.getElementById('confirm-form').action = action;
  document.getElementById('confirm-text').textContent = text;
  document.getElementById('confirm-go').textContent = go || 'Delete files';
  document.getElementById('confirm').showModal();
}
</script>
"""


def page(title, body):
    return (f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · Audio Capture Box</title><style>{STYLE}</style></head><body><main>
{body}</main>{CONFIRM}</body></html>""").encode("utf-8")


def js_str(s):
    return html.escape(json.dumps(s), quote=True)


def render_index(root):
    e = html.escape
    takes = load_takes(root)
    trash = storage.list_trash(root)
    trash_label = f"Trash ({len(trash)})" if trash else "Trash"
    out = [f"""<div class="top"><div class="grow"><h1>Recordings</h1><p class="sub">Audio Capture Box</p></div>
<a class="btn btn-secondary" href="/trash">{ICON_TRASH}{trash_label}</a></div>"""]
    if not takes:
        out.append('<p class="empty">No recordings yet. Press REC on the recorder to make one.</p>')
    for folder, meta, files in takes:
        total = sum(s for _, s in files)
        name = meta.get("name") or folder
        tracks = f"{len(files)} track{'s' if len(files) != 1 else ''} · {human_size(total)}"
        lines = [take_date(meta, folder), format_line(meta), tracks]
        lines_html = "".join(f'<div class="line">{e(x)}</div>' for x in lines if x)
        out.append(f"""<section class="take">
<div class="take-head"><div class="grow"><h3>{e(name)}</h3>{lines_html}
<div class="line muted">Folder: <code>{e(folder)}</code></div></div>
<div class="actions">
<a class="btn btn-primary" href="/zip/{quote(folder)}" download>{ICON_DL}Download whole take</a>
<button type="button" class="btn btn-danger" onclick="ask('/delete/{quote(folder)}', {js_str(f'{name} and its {len(files)} files will be moved to the Trash.')})">{ICON_TRASH}Delete take</button>
</div></div>
<table>""")
        for fn, size in files:
            out.append(f"""<tr><td><div class="fname">{e(fn)}</div><div class="desc">{e(describe_file(fn, meta))}</div></td>
<td class="size">{human_size(size)}</td>
<td class="act"><a class="btn btn-secondary" href="/file/{quote(folder)}/{quote(fn)}" download>{ICON_DL}Download</a></td></tr>""")
        out.append("</table></section>")
    return page("Recordings", "".join(out))


def render_trash(root):
    e = html.escape
    items = list(reversed(storage.list_trash(root)))   # most recently deleted first
    tdir = storage.trash_dir(root)
    total = sum(s for _, _, s in items)
    empty_btn = (f"""<button type="button" class="btn btn-danger" onclick="ask('/empty-trash', {js_str(f'Everything in the Trash ({human_size(total)}) will be deleted for good.')}, 'Empty Trash')">{ICON_TRASH}Empty Trash</button>"""
                 if items else "")
    out = [f"""<div class="top"><div class="grow"><h1>Trash</h1><p class="sub">Audio Capture Box</p></div>
<a class="btn btn-secondary" href="/">Back to recordings</a>{empty_btn}</div>
<p class="note">Deleted takes stay here until the recorder needs the space. When a recording needs room, the oldest ones are removed for good automatically.</p>"""]
    if not items:
        out.append('<p class="empty">The Trash is empty.</p>')
    for folder, deleted, size in items:
        meta, files = read_take(os.path.join(tdir, folder))
        name = meta.get("name") or folder
        lines = [take_date(meta, folder), format_line(meta),
                 f"{len(files)} track{'s' if len(files) != 1 else ''} · {human_size(size)}",
                 "Deleted " + nice_date(time.localtime(deleted))]
        lines_html = "".join(f'<div class="line">{e(x)}</div>' for x in lines if x)
        out.append(f"""<section class="take"><div class="take-head" style="border-bottom:none">
<div class="grow"><h3>{e(name)}</h3>{lines_html}<div class="line muted">Folder: <code>{e(folder)}</code></div></div>
<div class="actions">
<form method="post" action="/restore/{quote(folder)}"><button class="btn btn-secondary" type="submit">Restore</button></form>
<button type="button" class="btn btn-danger" onclick="ask('/purge/{quote(folder)}', {js_str(f'{name} will be deleted for good. This can’t be undone.')})">{ICON_TRASH}Delete forever</button>
</div></div></section>""")
    return page("Trash", "".join(out))


class _Handler(BaseHTTPRequestHandler):
    root = "."

    def log_message(self, *args):
        pass

    def _parts(self):
        return [unquote(p) for p in self.path.split("?", 1)[0].split("/") if p]

    def _safe_path(self, *parts):
        for p in parts:
            if not SAFE.match(p) or p in (".", ".."):
                return None
        path = os.path.realpath(os.path.join(self.root, *parts))
        if not path.startswith(os.path.realpath(self.root) + os.sep):
            return None
        return path

    def _html(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, where):
        self.send_response(303)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parts = self._parts()
        try:
            if not parts:
                self._html(render_index(self.root))
            elif parts == ["trash"]:
                self._html(render_trash(self.root))
            elif parts[0] == "file" and len(parts) == 3:
                path = self._safe_path(parts[1], parts[2])
                if not path or not os.path.isfile(path):
                    return self.send_error(404, "Not found")
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
                    return self.send_error(404, "Not found")
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
                self.send_error(404, "Not found")
        except (BrokenPipeError, ConnectionResetError):
            pass        # the browser cancelled the download

    def do_POST(self):
        parts = self._parts()
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(min(length, 65536))
        ok_name = len(parts) == 2 and SAFE.match(parts[1]) and parts[1] not in (".", "..")
        try:
            if parts and parts[0] == "delete" and ok_name:
                storage.move_to_trash(self.root, parts[1])
                return self._redirect("/")
            if parts and parts[0] == "restore" and ok_name:
                storage.restore_from_trash(self.root, parts[1])
                return self._redirect("/trash")
            if parts and parts[0] == "purge" and ok_name:
                storage.delete_forever(self.root, parts[1])
                return self._redirect("/trash")
            if parts == ["empty-trash"]:
                storage.empty_trash(self.root)
                return self._redirect("/trash")
        except OSError:
            pass
        self.send_error(404, "Not found")


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
        handler = type("Handler", (_Handler,), {"root": root})
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
