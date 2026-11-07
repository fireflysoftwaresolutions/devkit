# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The dashboard as a local, live, read-only app (spec P-fxk274bjfar).

`dev dashboard` binds a socket on 127.0.0.1 (a port the OS picks) and hands it to a background server process
(`python -m devkit.dashserver serve --fd N`, the token in its environment, never in argv), then opens the browser with
a one-time link. The server keeps a snapshot of the page's data (`dashboard.collect`), rebuilds it when the files
behind it change, and tells open pages through Server-Sent Events which areas changed. It answers GET and HEAD only,
checks the Host header (DNS rebinding), needs a per-server secret (the CLI's Bearer token, or the browser's HttpOnly
SameSite=Strict cookie that a one-time ticket buys), sends a nonce CSP with the page, and stops by itself after
`dashboard.idle_minutes` without a page open or a request. Standard library only, Python 3.9.

API (all under /api/v1, JSON; errors are RFC 9457 problem details):
  GET /health · /ticket · /dashboard[?omit=a,b|only=a,b] (ETag, 304) · /events (SSE) · /items/{id} · /runs/{id}
      /jobs · /jobs/{name} · /ci · /files[?path=<relative .md>] · /branches/{project id}
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import hmac
import http.client
import http.server
import json
import os
import re
import secrets
import select
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import zlib
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from . import core

API = "/api/v1"
POLL_SECONDS = 2.0          # the watcher looks at modification times this often (it is also the debounce)
REGISTRY_MIN_SECONDS = 10   # a change rebuilds the registry (git, PRs, notes) at most this often ...
REGISTRY_MAX_SECONDS = 60   # ... and at least this often while a page is open
ENRICH_SECONDS = 600        # providers and configuration drift (slow: the agents' CLIs), while a page is open
ACTIVE_SECONDS = 120        # after the last request the watcher keeps the snapshot fresh this long
TICKET_SECONDS = 60
HEARTBEAT_SECONDS = 15
HISTORY = 128               # versions whose changed areas are remembered for a page that reconnects
CACHE_KEYS = 64
BRANCH_SECONDS = 5.0        # a project's branch map (git, PRs) is read at most this often; between, the same answer
BRANCH_READS = 2            # at most this many maps are read at once: one more is a 429 with Retry-After
BRANCH_WAIT_SECONDS = 30    # a request for a map that is being read waits this long for that read
GZIP_MIN = 1400
VOLATILE = {"rendered", "generated"}   # change on every build: not a reason to tell the pages
AREA_RE = re.compile(r"[a-z_]{1,40}")
TO_RE = re.compile(r"/[A-Za-z0-9/._~%?=&,:+-]{0,500}")
PROJECT_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}(?:/[A-Za-z0-9_.-]{1,100}){0,6}")  # a project id: owner/repo, host/group/…/repo
SERVE_MARKS = ("devkit.dashserver serve", "dashboard serve")
_CHILDREN = {}              # servers this process started: kept, so they are reaped (no zombie, no warning)


def state_file() -> Path:
    return core.STATE / "dashboard" / "server.json"


def log_file() -> Path:
    return core.STATE / "logs" / "dashboard.log"


def idle_minutes() -> float:
    try:
        v = float((core.CFG.get("dashboard") or {}).get("idle_minutes", 30))
    except (TypeError, ValueError):
        return 30.0
    return v if v > 0 else 30.0


def _log(msg: str):
    try:
        log_file().parent.mkdir(parents=True, exist_ok=True)
        with open(log_file(), "a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} [{os.getpid()}] {msg}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- the snapshot

def watched_paths() -> list:
    """Every folder and file the page's data comes from: the handoff repository and devkit's state."""
    h, s = core.HANDOFF, core.STATE
    return [h / "items", h / "projects", h / "plans", h / "initiatives", h / "log", h / "presence", h / "sessions",
            h / "registry.json", h / "PROJECTS.md", s / "runs", s / "sessions", s / "timing", s / "ci" / "watch",
            s / "prs", s / "jobs", s / "logs" / "hooks.log", s / "canary.jsonl", s / "providers.json"]


def fingerprint(paths) -> str:
    """A digest of the paths, sizes and modification times under `paths` (folders walked, .git skipped)."""
    h = hashlib.blake2b(digest_size=20)
    stack = [str(p) for p in reversed(list(paths))]
    while stack:
        p = stack.pop()
        try:
            st = os.stat(p)
        except OSError:
            h.update(f"{p}\0-\n".encode())
            continue
        h.update(f"{p}\0{st.st_mtime_ns}\0{st.st_size}\n".encode())
        if stat.S_ISDIR(st.st_mode):
            try:
                with os.scandir(p) as it:
                    kids = sorted(e.path for e in it if e.name != ".git")
            except OSError:
                continue
            stack.extend(reversed(kids))
    return h.hexdigest()


def _digest(v) -> str:
    return hashlib.blake2b(json.dumps(v, sort_keys=True, default=str).encode(), digest_size=20).hexdigest()


def _enrich(reg: dict) -> dict:
    """What a scheduled `dev sync` adds to the registry: provider status and configuration drift."""
    out = {}
    try:
        from . import providers
        out["providers"] = providers.status(include_account=False)
    except Exception:
        pass
    try:
        from . import policy
        out["drift"] = [d.as_dict() for d in policy.diff()]
    except Exception:
        pass
    return out


def _build_registry() -> dict:
    from . import registry
    core._CWDS = None  # which checkouts have a live process: cached per process, and this one lives for hours
    return registry.build(with_prs=True)


class Snapshot:
    """The page's data. `refresh()` rebuilds it when the watched files changed; the version grows only when the data
    really changed, and each version remembers which top-level areas did (a page fetches only those)."""

    def __init__(self, build=None, collect=None, paths=None, enrich=None, clock=time.time):
        from . import dashboard
        self._build = build or _build_registry
        self._collect = collect or dashboard.collect
        self._paths = paths or watched_paths
        self._enrich = _enrich if enrich is None else enrich
        self.clock = clock
        self.cond = threading.Condition()
        self.version, self.data, self.digests, self.log = 0, None, {}, deque(maxlen=HISTORY)
        self.reg, self.reg_at, self.reg_stale, self.extra, self.dirty, self.fp = None, 0.0, False, {}, False, None
        self.cost = 0.0
        self._enc = {}
        self.started = clock()

    def refresh(self, force: bool = False, registry: bool = False) -> bool:
        """Rebuild when something changed. `registry`: a page is open, so the registry is also due every 60 s."""
        now = self.clock()
        fp = fingerprint(self._paths())
        moved = fp != self.fp
        if moved and self.fp is not None:
            self.reg_stale = True
        since = now - self.reg_at
        need_reg = (self.reg is None or force or (registry and since >= REGISTRY_MAX_SECONDS)
                    or (self.reg_stale and since >= REGISTRY_MIN_SECONDS))
        if not (force or moved or need_reg or self.dirty or self.data is None):
            return False
        if need_reg:
            self.reg, self.reg_at, self.reg_stale = self._build(), now, False
        self.dirty = False
        t0 = time.perf_counter()
        data = self._collect(dict(self.reg or {}, **self.extra), now=now)
        self.cost = time.perf_counter() - t0
        self.fp = fp
        return self.publish(data)

    def enrich(self):
        if self._enrich:
            self.extra = self._enrich(self.reg or {}) or {}
            self.dirty = True

    def publish(self, data: dict) -> bool:
        digests = {k: _digest(v) for k, v in data.items() if k not in VOLATILE}
        changed = sorted(k for k in set(digests) | set(self.digests) if digests.get(k) != self.digests.get(k))
        with self.cond:
            if self.data is not None and not changed:
                return False  # same data: keep the old one, so a version always means the same bytes
            self.data, self.digests = data, digests
            self.version += 1
            self.log.append((self.version, changed))
            self._enc = {}
            self.cond.notify_all()
        return True

    def wait(self, after: int, timeout: float) -> int:
        """Block until the version is newer than `after`, or `timeout` seconds; the current version."""
        with self.cond:
            self.cond.wait_for(lambda: self.version > after, timeout=timeout)
            return self.version

    def changed_since(self, v: int) -> list:
        """The areas that changed after version `v`; ["*"] when that is no longer known."""
        with self.cond:
            if v >= self.version:
                return []
            if v <= 0 or not self.log or self.log[0][0] > v + 1:
                return ["*"]
            out = set()
            for ver, ch in self.log:
                if ver > v:
                    out.update(ch)
            return sorted(out)

    def view(self, omit=(), only=None) -> dict:
        with self.cond:
            d, v = self.data or {}, self.version
        d = {k: d[k] for k in only if k in d} if only is not None else {k: x for k, x in d.items() if k not in omit}
        d["live"], d["snapshot"] = True, v
        return d

    def encoded(self, key, make):
        """(version, bytes) of `make()` (which returns a view), cached for the current version."""
        with self.cond:
            hit = self._enc.get(key)
            if hit and hit[0] == self.version:
                return hit
        d = make()
        body = json.dumps(d, ensure_ascii=False, separators=(",", ":"), default=str).encode()
        with self.cond:
            if len(self._enc) >= CACHE_KEYS:
                self._enc = {}
            self._enc[key] = (d.get("snapshot", self.version), body)
            return self._enc[key]

    def gzipped(self, key, body: bytes) -> bytes:
        gk = ("gz",) + tuple(key)
        with self.cond:
            hit = self._enc.get(gk)
            if hit and hit[0] == self.version:
                return hit[1]
        z = gzip.compress(body, compresslevel=5, mtime=0)
        with self.cond:
            self._enc[gk] = (self.version, z)
        return z


# --------------------------------------------------------------------------- HTTP

def problem(status: int, title: str, detail: str = "") -> dict:
    """An RFC 9457 problem object."""
    out = {"type": "about:blank", "title": title, "status": status}
    if detail:
        out["detail"] = detail
    return out


class App:
    """What the request handlers share: the snapshot, the secrets, one-time tickets and activity (for the idle stop)."""

    def __init__(self, snapshot: Snapshot, token: str, port: int, clock=time.time, version: str = None,
                 handoff: Path = None, idle: float = None):
        self.snapshot, self.token, self.port, self.clock = snapshot, token, port, clock
        self.version = version or core.VERSION
        self.handoff = Path(handoff) if handoff else core.HANDOFF
        self.idle_minutes = idle if idle is not None else idle_minutes()
        self.session = secrets.token_urlsafe(32)   # the browser's cookie value: never the CLI's token
        self.cookie = f"devkit_dashboard_{port}"   # cookies ignore the port: one name per server
        self.tickets, self.clients, self.last = {}, 0, clock()
        self.lock = threading.Lock()
        self.branch_maps, self.branch_reading = {}, {}   # pid -> (time, answer) · pid -> Event while git reads it

    def branch_map(self, pid: str, read):
        """The branch map of `pid` under the server's limit (the page's own throttle is not one) -> (answer, 0), or
        (None, seconds to wait) when BRANCH_READS maps are being read already. A map is read at most once per
        BRANCH_SECONDS; a request that comes while it is read waits for that read instead of starting another."""
        while True:
            with self.lock:
                hit = self.branch_maps.get(pid)
                if hit and self.clock() - hit[0] < BRANCH_SECONDS:
                    return hit[1], 0
                busy = self.branch_reading.get(pid)
                if busy is None:
                    if len(self.branch_reading) >= BRANCH_READS:
                        return None, 1
                    self.branch_reading[pid] = threading.Event()
                    break
            if not busy.wait(BRANCH_WAIT_SECONDS):
                return None, 1
            with self.lock:  # the read it waited for: its answer, whatever the clock says now
                hit = self.branch_maps.get(pid)
                if hit:
                    return hit[1], 0
        answer, ok = None, False
        try:
            answer, ok = read(), True
        finally:
            with self.lock:
                done = self.branch_reading.pop(pid)
                if ok:
                    self.branch_maps[pid] = (self.clock(), answer)
                    for k in sorted(self.branch_maps, key=lambda k: self.branch_maps[k][0])[:-CACHE_KEYS]:
                        del self.branch_maps[k]
            done.set()
        return answer, 0

    def touch(self):
        self.last = self.clock()

    def new_ticket(self) -> str:
        t = secrets.token_urlsafe(24)
        with self.lock:
            now = self.clock()
            self.tickets = {k: v for k, v in self.tickets.items() if v > now}
            self.tickets[t] = now + TICKET_SECONDS
        return t

    def use_ticket(self, t: str) -> bool:
        with self.lock:
            exp = self.tickets.pop(t, 0) if t else 0
        return exp > self.clock()

    def allowed_host(self, host: str) -> bool:
        return host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}")

    def authorized(self, headers) -> bool:
        auth = headers.get("Authorization") or ""
        if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip().encode(), self.token.encode()):
            return True
        for part in (headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == self.cookie and hmac.compare_digest(v.encode(), self.session.encode()):
                return True
        return False

    def idle(self) -> bool:
        return self.clients == 0 and self.clock() - self.last > self.idle_minutes * 60


def csp(nonce: str = "") -> str:
    """The page's policy: its own inline script and style by nonce, the brand font, same-origin fetches. Inline style
    attributes stay allowed (the page sets them through setAttribute; text always goes in with textContent)."""
    if not nonce:
        return "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    return (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}' https://fonts.googleapis.com; "
            f"style-src-elem 'nonce-{nonce}' https://fonts.googleapis.com; style-src-attr 'unsafe-inline'; "
            "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'")


def _error_page(title: str, text: str) -> str:
    from .dashboard import _esc
    return ("<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><title>devkit dashboard</title>"
            "<style>body{font:16px/1.5 system-ui,sans-serif;margin:3rem auto;max-width:40rem;padding:0 1rem}"
            "@media (prefers-color-scheme:dark){body{background:#151412;color:#eee}}</style>"
            f"<h1>{_esc(title)}</h1><p>{_esc(text)}</p></html>")


def _ids(q: dict, key: str):
    vals = [x for x in ",".join(q.get(key) or []).split(",") if x]
    return [x for x in vals if AREA_RE.fullmatch(x)]


def make_handler(app: App):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "devkit-dashboard"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = 60                      # an idle keep-alive connection is closed after this

        def log_request(self, code="-", size="-"):  # errors only, and never the query string (a ticket)
            try:
                if int(code) >= 400:
                    _log(f"{self.command} {urlsplit(self.path).path} {code}")
            except (TypeError, ValueError):
                pass

        def log_message(self, fmt, *args):
            _log("http: " + (fmt % args)[:300])

        # ---- responses
        def _head(self, status: int, ctype: str, length=None, extra: dict = None, nonce: str = ""):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            if length is not None:
                self.send_header("Content-Length", str(length))
            for k, v in (("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
                         ("Referrer-Policy", "no-referrer"), ("Cross-Origin-Resource-Policy", "same-origin"),
                         ("Cross-Origin-Opener-Policy", "same-origin"), ("X-Frame-Options", "DENY"),
                         ("Content-Security-Policy", csp(nonce))):
                self.send_header(k, v)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _send(self, status: int, body: bytes, ctype: str, extra: dict = None, nonce: str = "", gz=None):
            extra = dict(extra or {})
            if gz is not None or (len(body) >= GZIP_MIN and self._gzip_ok()):
                body = gz if gz is not None else gzip.compress(body, compresslevel=5, mtime=0)
                extra.update({"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
            self._head(status, ctype, len(body), extra, nonce)
            if self.command != "HEAD":
                self.wfile.write(body)

        def _gzip_ok(self) -> bool:
            return "gzip" in [x.split(";")[0].strip().lower() for x in (self.headers.get("Accept-Encoding") or "").split(",")]

        def _json(self, status: int, obj, extra: dict = None):
            self._send(status, json.dumps(obj, ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8", extra)

        def _problem(self, status: int, title: str, detail: str = "", extra: dict = None):
            self._send(status, json.dumps(problem(status, title, detail)).encode(), "application/problem+json", extra)

        def _html_error(self, status: int, title: str, text: str):
            self._send(status, _error_page(title, text).encode(), "text/html; charset=utf-8")

        # ---- methods
        def do_HEAD(self):
            self.do_GET()

        def _refuse(self):
            self.close_connection = True  # the body, if any, is not read: never reuse the connection
            self._problem(405, "Method not allowed", "The dashboard is read-only: use `dev` to change things.",
                          {"Allow": "GET, HEAD", "Connection": "close"})

        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_CONNECT = _refuse

        def do_GET(self):
            if not app.allowed_host(self.headers.get("Host") or ""):
                return self._problem(421, "Misdirected request", "Open the dashboard with `dev dashboard`.")
            url = urlsplit(self.path)
            try:
                q = parse_qs(url.query, max_num_fields=20)
            except ValueError:
                return self._problem(400, "Bad request", "Too many query parameters.")
            try:
                if url.path == "/auth":
                    return self._auth(q)
                if not app.authorized(self.headers):
                    if url.path.startswith(API + "/") or url.path == API:
                        return self._problem(401, "Unauthorized", "Open the dashboard with `dev dashboard`.",
                                             {"WWW-Authenticate": 'Bearer realm="devkit-dashboard"'})
                    return self._html_error(401, "Sign in from the terminal",
                                            "Run `dev dashboard`: it opens this page with a one-time link.")
                app.touch()
                return self._route(url.path, q)
            except (BrokenPipeError, ConnectionResetError):
                return None
            except Exception as e:  # never a stack trace to the page
                _log(f"{url.path}: {type(e).__name__}: {e}")
                return self._problem(500, "Internal error", f"The dashboard server failed; see {core.tilde(log_file())}.")

        def _auth(self, q: dict):
            if not app.use_ticket((q.get("t") or [""])[0]):
                return self._html_error(401, "This link has expired",
                                        "Links from `dev dashboard` work once, for a minute. Run `dev dashboard` again.")
            to = (q.get("to") or [""])[0]
            loc = "/" + ("#" + to if to and TO_RE.fullmatch(to) else "")
            cookie = f"{app.cookie}={app.session}; Path=/; HttpOnly; SameSite=Strict"
            app.touch()
            return self._send(303, b"", "text/plain; charset=utf-8", {"Location": loc, "Set-Cookie": cookie})

        def _route(self, path: str, q: dict):
            from . import dashboard
            snap = app.snapshot
            if path in ("/", "/index.html"):
                nonce = secrets.token_urlsafe(18)
                html = dashboard.page(snap.view(omit=("files",)), nonce=nonce)
                return self._send(200, html.encode(), "text/html; charset=utf-8", nonce=nonce)
            if not (path == API or path.startswith(API + "/")):
                return self._html_error(404, "Not found", "Nothing here. Run `dev dashboard` to open the dashboard.")
            sub = path[len(API):]
            if sub == "/health":
                return self._json(200, {"ok": True, "version": app.version, "started": int(snap.started),
                                        "snapshot": snap.version, "pid": os.getpid(), "clients": app.clients})
            if sub == "/ticket":  # for `dev dashboard` (Bearer token): a one-time sign-in link for the browser
                return self._json(200, {"ticket": app.new_ticket(), "expires_in": TICKET_SECONDS})
            if sub == "/dashboard":
                return self._dashboard(q)
            if sub == "/events":
                return self._events(q)
            if sub.startswith("/items/"):
                d = dashboard.item_detail(sub[len("/items/"):], handoff=app.handoff, reg=snap.reg)
                return self._json(200, d) if d else self._problem(404, "Not found", "No work item with this id.")
            if sub.startswith("/runs/"):
                d = dashboard.run_detail(sub[len("/runs/"):])
                return self._json(200, d) if d else self._problem(404, "Not found", "No progress run with this id.")
            if sub.startswith("/branches/"):  # one project's branch map, read now (spec P-fy3bwa0pshk R8)
                pid = sub[len("/branches/"):]
                d, wait = app.branch_map(pid, lambda: dashboard.branch_detail(pid, snap.reg)) if \
                    PROJECT_RE.fullmatch(pid) and ".." not in pid else (None, 0)
                if wait:
                    return self._problem(429, "Too many branch reads", f"The dashboard reads at most {BRANCH_READS} "
                                         f"branch maps at once. Try again in {wait} s.", {"Retry-After": str(wait)})
                if d is None:
                    return self._problem(404, "Not found", "No project with this id is cloned on this Mac.")
                if d.get("error"):
                    return self._problem(502, "git could not read the branches", d["error"]["detail"])
                return self._json(200, d)
            if sub == "/jobs":
                return self._json(200, dashboard.jobs_detail())
            if sub.startswith("/jobs/"):
                d = dashboard.jobs_detail(sub[len("/jobs/"):])
                return self._json(200, d) if d else self._problem(404, "Not found", "No scheduled job with this name.")
            if sub == "/ci":
                return self._json(200, dashboard.ci_watches(excerpt=0))
            if sub == "/files":
                if "path" in q:
                    d = dashboard.file_detail(q["path"][0], handoff=app.handoff)
                    return self._json(200, d) if d else self._problem(404, "Not found",
                                                                      "No such Markdown file in the handoff repository.")
                v, body = snap.encoded(("files",), lambda: snap.view(only=("files", "files_meta")))
                return self._send(200, body, "application/json; charset=utf-8", {"ETag": f'"{v}.files"'},
                                  gz=snap.gzipped(("files",), body) if len(body) >= GZIP_MIN and self._gzip_ok() else None)
            return self._problem(404, "Not found", f"No such API path: {sub[:80]}")

        def _dashboard(self, q: dict):
            snap = app.snapshot
            only = _ids(q, "only") if "only" in q else None
            omit = tuple(sorted(set(_ids(q, "omit"))))
            key = ("dash", tuple(only) if only is not None else None, omit)
            v, body = snap.encoded(key, lambda: snap.view(omit=omit, only=only))
            etag = f'"{v}.{zlib.crc32(repr(key).encode()):08x}"'
            if (self.headers.get("If-None-Match") or "").strip() == etag:
                return self._head(304, "application/json; charset=utf-8", 0, {"ETag": etag})
            gz = snap.gzipped(key, body) if len(body) >= GZIP_MIN and self._gzip_ok() else None
            return self._send(200, body, "application/json; charset=utf-8", {"ETag": etag}, gz=gz)

        def _events(self, q: dict):
            snap = app.snapshot
            raw = self.headers.get("Last-Event-ID") or (q.get("since") or [""])[0]
            try:
                seen = int(raw)
            except ValueError:
                seen = -1
            self.close_connection = True  # no length and no chunking: the stream ends with the connection
            self._head(200, "text/event-stream; charset=utf-8", None, {"X-Accel-Buffering": "no"})
            if self.command == "HEAD":
                return None
            with app.lock:
                app.clients += 1
            try:
                self.wfile.write(f"retry: 3000\n: devkit dashboard {app.version}\n\n".encode())
                self.wfile.flush()
                beat = time.monotonic()
                while not self._gone():
                    v = snap.wait(seen, min(1.0, HEARTBEAT_SECONDS))
                    if v > seen:
                        changed = snap.changed_since(seen) if seen >= 0 else ["*"]
                        msg = json.dumps({"version": v, "changed": changed})
                        self.wfile.write(f"id: {v}\nevent: snapshot\ndata: {msg}\n\n".encode())
                        seen = v
                    elif time.monotonic() - beat >= HEARTBEAT_SECONDS:
                        self.wfile.write(b": keep-alive\n\n")
                    else:
                        continue
                    self.wfile.flush()
                    beat = time.monotonic()
                    app.touch()
            except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
                return None
            finally:
                with app.lock:
                    app.clients -= 1
                app.touch()

        def _gone(self) -> bool:
            """The page closed its end (a readable socket with nothing to read): noticed within a second."""
            try:
                r, _, _ = select.select([self.connection], [], [], 0)
                return bool(r) and self.connection.recv(1, socket.MSG_PEEK) == b""
            except (OSError, ValueError):
                return True

    return Handler


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def serve(sock: socket.socket, token: str, snapshot: Snapshot = None, *, clock=time.time, idle_check: float = 30.0,
          poll: float = POLL_SECONDS, idle: float = None, version: str = None, handoff: Path = None,
          ready=None) -> int:
    """Run the server on a bound, listening socket until it has been idle `idle` minutes (dashboard.idle_minutes) or
    gets SIGTERM; then remove the state file if it is this server's. `ready(app)` is called before serving."""
    port = sock.getsockname()[1]
    snap = snapshot or Snapshot(clock=clock)
    try:
        snap.refresh(force=True)
    except Exception as e:  # nothing to serve: the caller gets a refused connection and the log says why
        _log(f"first snapshot failed: {type(e).__name__}: {e}")
        sock.close()
        raise
    app = App(snap, token, port, clock=clock, version=version, handoff=handoff, idle=idle)
    srv = Server(("127.0.0.1", port), make_handler(app), bind_and_activate=False)
    srv.socket.close()
    srv.socket, srv.server_address, srv.server_name, srv.server_port = sock, sock.getsockname(), "127.0.0.1", port
    stop = threading.Event()
    app.shutdown = lambda: (stop.set(), threading.Thread(target=srv.shutdown, daemon=True).start())

    def watcher():
        next_ok = 0.0
        while not stop.wait(poll):
            try:
                if (app.clients or app.clock() - app.last < ACTIVE_SECONDS) and time.monotonic() >= next_ok:
                    snap.refresh(registry=app.clients > 0)
                    next_ok = time.monotonic() + 3 * snap.cost  # collecting never takes more than a quarter of a core
            except Exception as e:
                _log(f"refresh: {type(e).__name__}: {e}")

    def enricher():
        last = None
        while not stop.wait(poll):
            try:
                if app.clients and (last is None or time.monotonic() - last >= ENRICH_SECONDS):
                    last = time.monotonic()
                    snap.enrich()
            except Exception as e:
                _log(f"enrich: {type(e).__name__}: {e}")

    def idler():
        while not stop.wait(idle_check):
            if app.idle():
                _log(f"idle for {app.idle_minutes:g} min: stopping")
                stop.set()
                srv.shutdown()
                return

    for fn in (watcher, enricher, idler):
        threading.Thread(target=fn, daemon=True, name=f"dashboard-{fn.__name__}").start()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *a: app.shutdown())
    if ready:
        ready(app)
    _log(f"serving on 127.0.0.1:{port} (devkit {app.version}, idle stop after {app.idle_minutes:g} min)")
    try:
        srv.serve_forever(poll_interval=0.2)
    finally:
        stop.set()
        srv.server_close()
        st = read_state()
        if st.get("pid") == os.getpid() and st.get("port") == port:
            try:
                state_file().unlink()
            except OSError:
                pass
        _log("stopped")
    return 0


def serve_fd(fd) -> int:
    """The background process: serve on the inherited socket, with the token from the environment."""
    token = os.environ.pop("DEVKIT_DASHBOARD_TOKEN", "")  # gone before anything else could inherit it
    if not token or fd is None:
        print("dashboard serve: needs --fd and the token in the environment (it is started by `dev dashboard`)",
              file=sys.stderr)
        return 2
    sock = socket.socket(fileno=int(fd))
    if sock.family != socket.AF_INET or sock.getsockname()[0] != "127.0.0.1":
        print("dashboard serve: the socket must listen on 127.0.0.1", file=sys.stderr)
        return 2
    return serve(sock, token)


# --------------------------------------------------------------------------- the client side: start, find, stop

def read_state() -> dict:
    st = core.read_json(state_file(), {}) or {}
    return st if isinstance(st, dict) else {}


def write_state(st: dict):
    """The state file (pid, port, version, token): 0600 from the first byte, replaced atomically."""
    f = state_file()
    f.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(st, fh)
    os.replace(tmp, f)


def _request(port: int, path: str, token: str, timeout: float = 3.0):
    """GET a path from a running server with the Bearer token: (status, parsed JSON or None); status 0 = no answer."""
    try:
        c = http.client.HTTPConnection("127.0.0.1", int(port), timeout=timeout)
        try:
            c.request("GET", path, headers={"Authorization": f"Bearer {token}", "Host": f"127.0.0.1:{port}"})
            r = c.getresponse()
            body = r.read()
        finally:
            c.close()
        try:
            return r.status, json.loads(body or b"null")
        except ValueError:
            return r.status, None
    except (OSError, http.client.HTTPException, ValueError):
        return 0, None


def status(timeout: float = 2.0) -> dict:
    """The server named by the state file, if it answers: {running, pid, port, version, current, url, ...}."""
    st = read_state()
    if not st.get("port") or not st.get("token"):
        return {"running": False}
    code, health = _request(st["port"], f"{API}/health", st["token"], timeout=timeout)
    if code != 200 or not isinstance(health, dict) or (st.get("pid") and health.get("pid") != st.get("pid")):
        return {"running": False, "stale": True, "pid": st.get("pid"), "port": st.get("port")}
    return {"running": True, "pid": health.get("pid"), "port": st["port"], "version": health.get("version"),
            "current": health.get("version") == core.VERSION, "url": f"http://127.0.0.1:{st['port']}/",
            "snapshot": health.get("snapshot"), "started": health.get("started"), "clients": health.get("clients")}


def _alive(pid: int) -> bool:
    if pid in _CHILDREN:
        return _CHILDREN[pid].poll() is None
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:  # our own child that has exited: reaped now
            return False
    except ChildProcessError:
        pass
    except OSError:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _is_ours(pid: int) -> bool:
    """Only a devkit dashboard server process may be stopped: checked on its command line."""
    try:
        out = subprocess.run(["ps", "-p", str(int(pid)), "-o", "command="], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return any(m in out for m in SERVE_MARKS)


def stop(wait: float = 5.0) -> bool:
    """Stop the server named by the state file (only a devkit dashboard process). True when one was stopped."""
    st = read_state()
    pid, stopped = int(st.get("pid") or 0), False
    if pid > 1 and pid != os.getpid() and _is_ours(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            stopped = True
        except OSError:
            pass
        deadline = time.monotonic() + wait
        while stopped and _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
    if read_state() == st and st:
        try:
            state_file().unlink()
        except OSError:
            pass
    return stopped


# The server's process: isolated as bin/dev's shebang (-I -S: no PYTHONPATH, current folder, site-packages or .pth
# file can put code into it), so it finds devkit from the lib folder it is given, never from the environment. Its
# command line keeps `devkit.dashserver serve --fd N` (SERVE_MARKS: _is_ours stops only such a process).
_SERVE_CODE = ("import sys; sys.path.insert(0, sys.argv[1]); from devkit import dashserver; "
               "sys.exit(dashserver.serve_fd(int(sys.argv[-1])))")


def start() -> dict:
    """Start a server in the background on a fresh 127.0.0.1 port; its state (pid, port, token, version)."""
    state_file().parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_file().parent.mkdir(parents=True, exist_ok=True)
    lib = str(Path(__file__).resolve().parents[1])
    token = secrets.token_urlsafe(32)
    env = dict(os.environ, DEVKIT_DASHBOARD_TOKEN=token)  # never in argv, where `ps` shows it
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        sock.listen(64)  # requests wait in the backlog until the server accepts them: no start-up race
        port = sock.getsockname()[1]
        with open(log_file(), "a") as log:
            proc = subprocess.Popen([sys.executable, "-I", "-S", "-c", _SERVE_CODE, lib, "devkit.dashserver", "serve",
                                     "--fd", str(sock.fileno())],
                                    pass_fds=(sock.fileno(),), env=env, cwd=str(state_file().parent),
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        _CHILDREN[proc.pid] = proc
    finally:
        sock.close()
    st = {"pid": proc.pid, "port": port, "token": token, "version": core.VERSION, "started": int(time.time())}
    write_state(st)
    return st


def ensure() -> dict:
    """A running server of this devkit version: the current one (reused), or a new one; an old, dead or silent one
    is replaced."""
    with core.Lock("dashboard", wait=30):
        s = status()
        st = read_state()
        if s.get("running") and s.get("current"):
            return dict(st, reused=True)
        pid = int(st.get("pid") or 0)
        if not s.get("running") and pid and time.time() - (st.get("started") or 0) < 60 and _alive(pid) and _is_ours(pid):
            s = status(timeout=30)  # just started: still building its first snapshot
            if s.get("running") and s.get("current"):
                return dict(st, reused=True)
        if st:
            stop()
        return dict(start(), reused=False)


def browser_url(st: dict, to: str = "") -> str:
    """A one-time sign-in link for the browser (the server's token never appears in a URL)."""
    code, res = _request(st["port"], f"{API}/ticket", st["token"], timeout=60.0)  # waits for the first snapshot
    if code != 200 or not isinstance(res, dict) or not res.get("ticket"):
        raise RuntimeError(f"the dashboard server did not answer (see {core.tilde(log_file())})")
    return f"http://127.0.0.1:{st['port']}/auth?t={res['ticket']}" + (f"&to={quote(to)}" if to else "")


# --------------------------------------------------------------------------- `dev dashboard`

def add_arguments(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    p.add_argument("action", nargs="?", default="", help=argparse.SUPPRESS)   # `serve`: the background process
    p.add_argument("--fd", type=int, help=argparse.SUPPRESS)
    p.add_argument("--no-open", action="store_true", help="print the one-time link instead of opening the browser")
    p.add_argument("--status", action="store_true", help="is a server running? (port, pid, version, snapshot)")
    p.add_argument("--stop", action="store_true", help="stop the running server")
    p.add_argument("--export", metavar="FILE", help="write the static, self-contained page to FILE (no server)")
    p.add_argument("--path", action="store_true", help="write the static page devkit keeps and print its path")
    return p


def _write_static(path: Path) -> Path:
    from . import dashboard, registry
    html = dashboard.render(registry.build(with_prs=True))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(html, encoding="utf-8")
    os.replace(tmp, path)
    return path


def cmd_dashboard(a) -> int:
    from . import ui
    action = getattr(a, "action", "") or ""
    if action == "serve":
        return serve_fd(a.fd)
    if action:
        ui.err(f"unknown dashboard action “{action}”: dev dashboard [--no-open|--status|--stop|--export FILE|--path]")
        return 2
    if a.status:
        s = status()
        if not s.get("running"):
            print(f"  dashboard stopped{' (its state file names a process that does not answer)' if s.get('stale') else ''}")
            return 1
        age = core.ago(s.get("started")) if s.get("started") else "-"
        print(f"{ui.OK} dashboard running → {s['url']} · pid {s['pid']} · devkit {s['version']}"
              f"{'' if s['current'] else ' (another version: `dev dashboard` replaces it)'} · snapshot {s['snapshot']}"
              f" · {s.get('clients') or 0} page(s) open · started {age}")
        return 0
    if a.stop:
        st = read_state()
        if stop():
            print(f"{ui.OK} dashboard stopped (pid {st.get('pid')})")
        else:
            print("  no dashboard server was running")
        return 0
    if a.export:
        out = _write_static(Path(a.export).expanduser())
        print(f"{ui.OK} static dashboard → {core.tilde(out)}")
        return 0
    if a.path:
        from . import dashboard, registry
        print(dashboard.write(registry.build(with_prs=True)))
        return 0
    st = ensure()
    try:
        url = browser_url(st)
    except RuntimeError as e:
        ui.err(str(e))
        return 1
    if a.no_open:
        print(url)
        return 0
    from . import dashboard
    dashboard.open_in_browser(url)
    print(f"{ui.OK} dashboard → http://127.0.0.1:{st['port']}/" + (" (already running)" if st.get("reused") else ""))
    return 0


if __name__ == "__main__":  # python -m devkit.dashserver serve --fd N  (started by `dev dashboard`)
    _ap = argparse.ArgumentParser(prog="devkit.dashserver")
    _ap.add_argument("action", choices=["serve"])
    _ap.add_argument("--fd", type=int, required=True)
    sys.exit(serve_fd(_ap.parse_args().fd))
