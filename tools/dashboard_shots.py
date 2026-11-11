#!/usr/bin/env python3
# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Screenshots of the dashboard from neutral fixture data, for reviews, the UX critique and the docs.

    python3 tools/dashboard_shots.py <out dir> [--route NAME=HASH ...] [--width 1440 390] [--theme light dark]
                                     [--doc example|empty|welcome | --live] [--height N]
    python3 tools/dashboard_shots.py --serve

It runs inside the test suite's hermetic home (tests/_hermetic.py), so the page is built from the tests' fixture and
never reads or writes the developer's devkit. With --doc, Now's areas come from that golden (tests/fixtures/dashboard/),
the page's clock is frozen at the golden's time and the time zone is UTC, so two runs give the same pictures.

With --live, the pictures are of the live page: the neutral fixture's example (tests/dashfixture.py) is laid out as files
in the hermetic home (run files, presence, CI watches, the PR cache, work items), the real dashboard server
(devkit.dashserver) serves the real collect() snapshot of it, and each picture signs in with its own one-time link. The
server's clock is the fixture's (2026-11-06 15:15 UTC) and the page's clock starts there. --serve starts the same server,
prints a sign-in link and keeps serving, for a check in a real browser: `kill -USR2 <pid>` prints a new link (each works
once, for a minute), Ctrl-C stops it. Answering D1 is the tech lead's typed command (progress, spec C4): this tool never
records one.

Each file is <name>-<width>-<theme>.png. It needs Chrome or the Playwright Chromium, found like the browser tests find it
(DEVKIT_TEST_CHROME names another one). Every picture is taken at a true viewport: Chrome lays a page out 500 px wide
at least, whatever its window size, so the tool sets the page's width and height itself through the DevTools protocol,
as Playwright does, and stops when the page's innerWidth is not the width asked for. Chrome runs headless with a mock
keychain, since the hermetic home has no login keychain. A picture that cannot be taken stops the run with the reason
and the last lines of Chrome's log; nothing is half-written, so running the same command again is safe.
"""
import argparse
import base64
import contextlib
import http.client
import json
import os
import secrets
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tests  # noqa: E402,F401  (the hermetic home, before devkit is imported)

sys.path.insert(0, str(ROOT / "lib"))
from devkit import core, dashboard  # noqa: E402
from tests import test_dashboard as T  # noqa: E402

# the page's clock starts at the fixture's instant and runs on from there (the live page's timers need it to move)
SHIFT = ('<script%s>(() => { const R = Date, off = %d * 1000 - R.now(); class F extends R { constructor(...a) { super(...(a.length ? a : [R.now() + off])); } '
         'static now() { return R.now() + off; } } window.Date = F; })();</script>')


def theme_first(page: str, theme: str, nonce: str = "") -> str:
    """The page with its theme chosen before anything paints (what the theme switch stores)."""
    attr = f' nonce="{nonce}"' if nonce else ""
    return page.replace("<head>", f'<head><script{attr}>document.documentElement.dataset.theme="{theme}"</script>', 1)


# runs in the page once it has loaded: its fonts are in and two frames are drawn; -> the width it is laid out at
SETTLE = ("(async () => { if (document.fonts) await document.fonts.ready;"
          " await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))); return innerWidth; })()")


class Browser:
    """A headless Chrome driven over the DevTools protocol on a pipe (--remote-debugging-pipe: commands on its fd 3,
    answers and events on its fd 4, each a JSON text ended by a NUL byte). Its profile and log live in a temporary
    folder that goes with it."""

    def __init__(self, env: dict, timeout: float = 120):
        self.deadline, self.next_id, self.events, self.buf = time.monotonic() + timeout, 0, [], b""
        self.tmp = tempfile.TemporaryDirectory()
        self.log = open(Path(self.tmp.name) / "chrome.log", "wb")
        cmd_r, self.cmd_w = os.pipe()
        self.ans_r, ans_w = os.pipe()
        args = [T.CHROME, "--headless", "--use-mock-keychain", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                "--no-default-browser-check", f"--user-data-dir={self.tmp.name}/profile", "--remote-debugging-pipe", "about:blank"]
        # the shell gives Chrome the pipes as fds 3 and 4, no stdin, and stdout on its log, then becomes Chrome; Chrome
        # and its helpers get a process group of their own, which goes with the picture
        self.proc = subprocess.Popen(["/bin/sh", "-c", 'exec "$@" 3<&0 4>&1 0</dev/null 1>&2', "sh", *args],
                                     stdin=cmd_r, stdout=ans_w, stderr=self.log, env=env, start_new_session=True)
        os.close(cmd_r)
        os.close(ans_w)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            os.write(self.cmd_w, json.dumps({"id": self.next_id + 1, "method": "Browser.close"}).encode() + b"\0")
        except OSError:
            pass
        os.close(self.cmd_w)  # the end of its command pipe also tells Chrome to quit
        try:  # without a display Chrome hangs at exit until its teardown watchdog ends it (10 s); its profile is thrown away
            self.proc.wait(2)
        except subprocess.TimeoutExpired:
            pass
        with contextlib.suppress(ProcessLookupError, PermissionError):  # its helpers too, which outlive a killed Chrome
            os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc.wait()
        os.close(self.ans_r)
        self.log.close()
        self.tmp.cleanup()

    def fail(self, what: str):
        self.log.flush()
        tail = (Path(self.tmp.name) / "chrome.log").read_text(errors="replace").strip()[-400:]
        raise SystemExit(f"{what}. Chrome ({T.CHROME}) said: {tail or 'nothing'}. Check that it starts headless "
                         "(it needs no display), or name another Chrome or Chromium in DEVKIT_TEST_CHROME, then run again.")

    def _message(self, what: str) -> dict:
        while b"\0" not in self.buf:
            left = self.deadline - time.monotonic()
            if left <= 0:
                self.fail(f"Chrome did not answer in time while it {what}")
            ready, _, _ = select.select([self.ans_r], [], [], min(left, 1.0))
            if ready:
                chunk = os.read(self.ans_r, 1 << 20)
                if not chunk:
                    self.fail(f"Chrome stopped (exit {self.proc.poll()}) while it {what}")
                self.buf += chunk
        raw, self.buf = self.buf.split(b"\0", 1)
        return json.loads(raw)

    def call(self, method: str, params: dict = None, session: str = None, what: str = "") -> dict:
        self.next_id += 1
        msg = {"id": self.next_id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        os.write(self.cmd_w, json.dumps(msg).encode() + b"\0")
        while True:
            m = self._message(what or f"ran {method}")
            if m.get("id") == msg["id"]:
                if "error" in m:
                    self.fail(f"Chrome refused {method}: {m['error'].get('message')}")
                return m.get("result") or {}
            self.events.append(m)

    def wait(self, event: str, session: str, what: str) -> dict:
        while True:
            for i, m in enumerate(self.events):
                if m.get("method") == event and m.get("sessionId") == session:
                    return self.events.pop(i)
            self.events.append(self._message(what))


def check_width(page_width, width: int, out: Path):
    """Stop when the page is laid out at another width than the picture's: it would show another layout, cropped."""
    if page_width != width:
        raise SystemExit(f"{out.name}: the page is {page_width} px wide, not {width}; no picture was written. "
                         "The browser ignored the viewport the tool set: try another Chrome or Chromium (DEVKIT_TEST_CHROME).")


def shoot(url: str, out: Path, width: int, height: int, env: dict) -> Path:
    """One screenshot of `url` at a true `width` x `height` viewport, once the page has loaded, its fonts are in and two
    frames are drawn (motion reduced). The live page holds its event stream open; that does not hold up the picture."""
    with Browser(env) as b:
        target = b.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        sid = b.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False}, sid)
        b.call("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]}, sid)
        b.call("Page.enable", {}, sid)
        b.call("Page.navigate", {"url": url}, sid)
        b.wait("Page.loadEventFired", sid, f"loaded the page for {out.name}")
        res = b.call("Runtime.evaluate", {"expression": SETTLE, "awaitPromise": True, "returnByValue": True}, sid,
                     f"drew the page for {out.name}")
        check_width((res.get("result") or {}).get("value"), width, out)
        png = base64.b64decode(b.call("Page.captureScreenshot", {"format": "png"}, sid, f"took {out.name}")["data"])
    out.write_bytes(png)
    return out


def shoot_page(page: str, out: Path, width: int, height: int, hash_: str, env: dict) -> Path:
    with tempfile.TemporaryDirectory() as t:
        f = Path(t) / "page.html"
        f.write_text(page, encoding="utf-8")
        return shoot(f.as_uri() + hash_, out, width, height, env)


# --------------------------------------------------------------------------- the live page

def lay_out_example(handoff: Path) -> dict:
    """The fixture's example as the files collect() reads, in the hermetic home; -> the registry."""
    import dashfixture as fx
    from devkit import progress as pg, work

    def put(f: Path, obj, mtime=None):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(obj, indent=1) + "\n", encoding="utf-8")
        if mtime:
            os.utime(f, (mtime, mtime))

    for run in (fx.invoice_run(), fx.docs_run()):   # fresh files: progress lists runs written in the last 12 h
        put(pg.RUNS / f"{run['agent']}-{run['session']}.json", run)
    mine ={"user": fx.USER, "host": fx.HOST, "updated": fx.iso(fx.at(15, 14)), "locks": [],
            "sessions": [{"agent": "claude", "project": "acme/lending-core", "branch": "feat/invoice-export", "started": fx.at(14, 0),
                          "status": "", "last_seen": fx.at(15, 14), "progress": pg.brief(fx.invoice_run(), fx.NOW)}]}
    for p in (mine, fx.remote_presence()):
        put(handoff / "presence" / f"{p['user']}@{p['host']}.json", p)
    for w in fx.ci_example()["watches"]:              # the watch records `dev ci watch` writes, newest by mtime
        rec = {k: w[k] for k in ("repo", "branch", "sha", "state", "ts", "minutes", "provider", "error", "runs")}
        rec["diagnoses"] = [dict({k: f[k] for k in ("run", "number", "id", "url", "class", "action", "excerpt")}, failed_jobs=f["jobs"]) for f in w["failures"]]
        put(core.STATE / "ci" / "watch" / f"{w['repo'].replace('/', '__')}-{w['branch'].replace('/', '_')}.json", rec, w["ts"])
    for repo, prs in fx.pr_cache().items():
        put(core.STATE / "prs" / f"{repo.replace('/', '__')}.json", prs, fx.PRS_AT)
    for it in fx.items_example():
        meta = {k: it[k] for k in ("id", "type", "title", "scope", "status", "bucket", "priority", "assignee", "claimed_by", "claimed_at",
                                   "lock_expires", "due", "links", "version", "created", "created_by", "updated", "updated_by")}
        f = handoff / it["path"]
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(work.render({"meta": meta, "title": it["title"], "body": "", "changelog": [
            f"v1 · {it['created']} · {it['created_by']} · create: task “{it['title']}” in {it['bucket']} — why: the fixture"]}), encoding="utf-8")
    reg = fx.registry_example()
    reg["sessions"] = [{"agent": "claude", "session": "7f3a", "project": "acme/lending-core", "branch": "feat/invoice-export",
                        "started": fx.at(14, 0), "status": "", "last_seen": fx.at(15, 14), "active": True}]   # this Mac's registry knows its session
    return reg


@contextlib.contextmanager
def live_server():
    """The real dashboard server on a free port, serving collect() of the fixture's example at the fixture's clock, in UTC.
    Yields (port, sign-in link maker)."""
    import dashfixture as fx
    from devkit import dashserver, progress as pg
    os.environ["TZ"] = "UTC"
    time.tzset()
    handoff = Path(tempfile.mkdtemp(prefix="handoff-", dir=core.STATE.parent))
    reg = lay_out_example(handoff)
    snap = dashserver.Snapshot(build=lambda: reg, paths=lambda: [pg.RUNS], enrich=lambda r: {}, clock=lambda: fx.NOW,
                               collect=lambda r, now=None: dashboard.collect(r, handoff=handoff, now=fx.NOW))
    page, theme = dashboard.page, {"name": ""}

    def live_page(data, nonce=""):   # the page's clock starts at the fixture's; a picture's theme is set before it paints
        html = page(data, nonce=nonce)
        html = html.replace("<head>", "<head>" + SHIFT % (f' nonce="{nonce}"' if nonce else "", fx.NOW), 1)
        return theme_first(html, theme["name"], nonce) if theme["name"] else html
    dashboard.page = live_page
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port, token, ready, apps = sock.getsockname()[1], secrets.token_urlsafe(32), threading.Event(), []
    t = threading.Thread(target=dashserver.serve, args=(sock, token, snap), daemon=True,
                         kwargs={"poll": 0.2, "idle_check": 5, "idle": 60, "handoff": handoff, "ready": lambda app: (apps.append(app), ready.set())})
    t.start()
    if not ready.wait(15):
        raise SystemExit("the dashboard server did not start")

    def link(to="/now", name=""):
        theme["name"] = name
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/api/v1/ticket", headers={"Authorization": f"Bearer {token}", "Host": f"127.0.0.1:{port}"})
        ticket = json.loads(c.getresponse().read())["ticket"]
        c.close()
        return f"http://127.0.0.1:{port}/auth?t={ticket}&to={to}"
    try:
        yield port, link
    finally:
        dashboard.page = page
        apps[0].shutdown()
        t.join(5)


def serve_forever() -> int:
    with live_server() as (port, link):
        signal.signal(signal.SIGUSR2, lambda *a: print("Sign in:", link(), flush=True))
        print(f"The dashboard serves the fixture on 127.0.0.1:{port} (pid {os.getpid()}). Sign in, once, within a minute:", flush=True)
        print("Sign in:", link(), flush=True)
        try:
            while True:
                signal.pause()
        except KeyboardInterrupt:
            return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", type=Path, nargs="?")
    ap.add_argument("--route", action="append", default=[], metavar="NAME=HASH", help="default: now=#/now")
    ap.add_argument("--width", type=int, nargs="+", default=[1440, 390])
    ap.add_argument("--theme", nargs="+", choices=("light", "dark"), default=["light", "dark"])
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--doc", choices=("example", "empty", "welcome"), help="Now's areas from this golden")
    src.add_argument("--live", action="store_true", help="the live page: the real server and collect() over the fixture's example")
    src.add_argument("--serve", action="store_true", help="serve the live page for a real browser until Ctrl-C")
    ap.add_argument("--height", type=int, help="window height (default 900, 844 under 768 px)")
    a = ap.parse_args(argv)
    if a.serve:
        return serve_forever()
    if not a.out:
        ap.error("the output folder is required")
    if not T.CHROME:
        raise SystemExit(T.NO_CHROME)
    routes = [r.split("=", 1) for r in a.route] or [["now", "#/now"]]
    env = dict(os.environ)
    a.out.mkdir(parents=True, exist_ok=True)
    sizes = [(w, t, a.height or (900 if w >= 768 else 844)) for w in a.width for t in a.theme]
    if a.live:
        env["TZ"] = "UTC"
        with live_server() as (_port, link):
            for name, hash_ in routes:
                for width, theme, h in sizes:
                    print(shoot(link("/" + hash_.lstrip("#/"), theme), a.out / f"{name}-{width}-{theme}.png", width, h, env))
        return 0
    with tempfile.TemporaryDirectory() as t:
        data = T.fixture_data(Path(t) / "handoff", T.NOW)
    if a.doc:
        import test_dashboard_now as N   # the tests folder is on the path (tests/__init__.py)
        data = N.with_doc(data, a.doc)
        env["TZ"] = "UTC"
    page = dashboard.page(data)
    if a.doc:
        page = N.frozen(page)
    for name, hash_ in routes:
        for width, theme, h in sizes:
            print(shoot_page(theme_first(page, theme), a.out / f"{name}-{width}-{theme}.png", width, h, hash_, env))
    return 0


if __name__ == "__main__":
    sys.exit(main())
