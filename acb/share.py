"""'Send to computer': a small web server that lists the recordings folder."""

import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = 8080


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class Share:
    def __init__(self):
        self.server = None
        self.thread = None
        self.root = None

    @property
    def running(self):
        return self.server is not None

    def start(self, root, port=PORT):
        """Return (ok, message)."""
        if self.server and self.root == root:
            return True, ""
        self.stop()
        handler = functools.partial(_Quiet, directory=root)
        try:
            self.server = ThreadingHTTPServer(("0.0.0.0", port), handler)
        except OSError as e:
            self.server = None
            return False, f"Couldn't start sharing: {e.strerror or e}"
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
