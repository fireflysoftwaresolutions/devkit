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

Each file is <name>-<width>-<theme>.png. It needs Chrome or the Playwright Chromium, found like the browser tests find it.
"""
import argparse
import contextlib
import http.client
import json
import os
import secrets
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


def shoot(url: str, out: Path, width: int, height: int, env: dict, budget: bool = True) -> Path:
    """One screenshot. `budget`: let the page's timers run 4 s first (a file page); a live page holds its event stream
    open, which stalls Chrome's virtual time, so it is taken once the page has loaded (it draws from embedded data)."""
    with tempfile.TemporaryDirectory() as t:
        res = subprocess.run([T.CHROME, "--headless", "--disable-gpu", "--hide-scrollbars", "--no-first-run", "--no-default-browser-check",
                              f"--user-data-dir={t}/profile", f"--window-size={width},{height}", *(["--virtual-time-budget=4000"] if budget else []),
                              f"--screenshot={out}", url], capture_output=True, text=True, timeout=120, env=env)
    if res.returncode or not out.exists():
        raise SystemExit(f"Chrome made no screenshot for {out.name}: {res.stderr.strip()[-400:]}")
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
                    print(shoot(link("/" + hash_.lstrip("#/"), theme), a.out / f"{name}-{width}-{theme}.png", width, h, env, budget=False))
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
