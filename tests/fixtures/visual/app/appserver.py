# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The fixture app's server: its pages on 127.0.0.1, a second origin on another port, and a log of everything both
receive, so a test can prove what the crawler never did (R70).

    with AppServer() as app:
        app.base     http://127.0.0.1:<port>, the app
        app.second   http://127.0.0.1:<port>, the second origin; every GET there answers a "Help center" page
        app.log      every request, in order: {"origin": "app" | "second", "method", "path"} (path with its query),
                     plus {"origin", "method": "UPGRADE", "path"} for each WebSocket handshake and
                     {"origin", "method": "MESSAGE", "path", "text"} for each WebSocket message received

A path maps to a file: / is index.html and /<path> is <path>.html (/invoices/1204 is invoices/1204.html); the query
is ignored. HTML pages get their placeholders filled when served, so no file holds a whole secret: {{KEY}} becomes
an AWS access key id and {{SECOND}} the second origin. The routes below are not files:

    /help              302 to the second origin's /landing
    /old-reports       301 to /reports-2025, which answers 302 to /reports: a link that passes two redirects
    /sw.js             the service worker offline.html registers; when it runs, it requests /from-service-worker
    /socket            a WebSocket endpoint: the handshake and every text message are logged
    POST <any path>    logged, then 303 to the same path

`hide`: paths whose links are left out of every page (`<a href="/offline">…</a>` goes), so a test can make a page
disappear between two crawls. `root` and `redirects`: another folder of pages and its own redirect routes (path ->
(status, target), a target's {{SECOND}} filled), for a fixture of its own such as tests/fixtures/visual/harden/; a
`.js` file in the folder is served as JavaScript at its path. Serve it only in tests.
"""
from __future__ import annotations

import base64
import hashlib
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEY = "AKIA" + "QYLPMN5HHHFPZAM2"  # an AWS access key id, in two parts so no scanner reads this file as one
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
REDIRECTS = {"/old-reports": (301, "/reports-2025"), "/reports-2025": (302, "/reports")}
SERVICE_WORKER = b"self.addEventListener('install', (e) => e.waitUntil(fetch('/from-service-worker')));\n"


def _handler(server: "AppServer", origin: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send(self, status: int, body: bytes = b"", kind: str = "text/html; charset=utf-8", headers=()):
            self.send_response(status)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 (the http.server name)
            if self.headers.get("Upgrade", "").lower() == "websocket":
                server.record(origin, "UPGRADE", self.path)
                return self._websocket()
            server.record(origin, "GET", self.path)
            path = self.path.split("?")[0].split("#")[0]
            if origin == "second":
                return self._send(200, b"<!doctype html><html lang=en><title>Help center</title><h1>Help center</h1>")
            if path == "/help":
                return self._send(302, headers=[("Location", f"{server.second}/landing")])
            if path in server.redirects:
                status, to = server.redirects[path]
                return self._send(status, headers=[("Location", to.replace("{{SECOND}}", server.second))])
            if path == "/sw.js":
                return self._send(200, SERVICE_WORKER, "text/javascript; charset=utf-8")
            script = server.root / path.strip("/")
            if path.endswith(".js") and ".." not in path and script.is_file():
                return self._send(200, script.read_bytes(), "text/javascript; charset=utf-8")
            file = server.root / ("index.html" if path == "/" else f"{path.strip('/')}.html")
            if ".." in path or not file.is_file():
                return self._send(404, b"<!doctype html><html lang=en><title>Not found</title><h1>Not found</h1>")
            return self._send(200, server.page(file))

        def do_POST(self):  # noqa: N802
            server.record(origin, "POST", self.path)
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            self._send(303, headers=[("Location", self.path)])

        def _websocket(self):
            key = self.headers.get("Sec-WebSocket-Key", "")
            accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            self.wfile.flush()
            self.connection.settimeout(5)
            try:
                while True:
                    head = self.rfile.read(2)
                    if len(head) < 2:
                        break
                    opcode, size = head[0] & 0x0F, head[1] & 0x7F
                    if size == 126:
                        size = int.from_bytes(self.rfile.read(2), "big")
                    elif size == 127:
                        size = int.from_bytes(self.rfile.read(8), "big")
                    mask = self.rfile.read(4) if head[1] & 0x80 else b"\0\0\0\0"
                    data = bytes(b ^ mask[i % 4] for i, b in enumerate(self.rfile.read(size)))
                    if opcode == 0x8:
                        break
                    if opcode == 0x1:
                        server.record(origin, "MESSAGE", self.path, data.decode("utf-8", "replace"))
            except OSError:
                pass
            self.close_connection = True

    return Handler


class AppServer:
    def __init__(self, hide=(), root=HERE, redirects=None):
        self.hide = set(hide)
        self.root = Path(root)
        self.redirects = REDIRECTS if redirects is None else dict(redirects)
        self.log = []
        self._lock = threading.Lock()

    def record(self, origin, method, path, text=None):
        entry = {"origin": origin, "method": method, "path": path}
        if text is not None:
            entry["text"] = text
        with self._lock:
            self.log.append(entry)

    def page(self, file: Path) -> bytes:
        html = file.read_text().replace("{{KEY}}", KEY).replace("{{SECOND}}", self.second)
        for path in self.hide:
            html = re.sub(rf'<a href="{re.escape(path)}"[^>]*>.*?</a>', "", html)
        return html.encode()

    def __enter__(self):
        self._servers = []
        for origin in ("app", "second"):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self, origin))
            httpd.daemon_threads = True
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            self._servers.append(httpd)
        self.base, self.second = (f"http://127.0.0.1:{s.server_address[1]}" for s in self._servers)
        return self

    def __exit__(self, *exc):
        for httpd in self._servers:
            httpd.shutdown()
            httpd.server_close()

    def requests(self, origin="app"):
        """The (method, path) of every request one origin received, upgrades and messages included."""
        with self._lock:
            return [(e["method"], e["path"]) for e in self.log if e["origin"] == origin]
