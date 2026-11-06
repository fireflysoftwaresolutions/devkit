# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the live dashboard server: a real server on a real 127.0.0.1 socket (in a thread), real files, and real
background processes in a temporary HOME. No network. Run: python3 -m unittest discover -s tests -v"""
import gzip
import http.client
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core, dashserver  # noqa: E402

TOKEN = "cli-token-0123456789abcdefghijklmnopqrstuv"


def until(cond, timeout=5.0, step=0.02):
    """Wait for a condition (never a fixed sleep); the last value of cond()."""
    deadline, pause = time.monotonic() + timeout, threading.Event()
    while True:
        v = cond()
        if v or time.monotonic() >= deadline:
            return v
        pause.wait(step)


class FakeClock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class TempState:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old = core.STATE
        core.STATE = self.root / "state"

    def close(self):
        core.STATE = self.old
        self.tmp.cleanup()


class LiveServer:
    """dashserver.serve() on a real socket in a thread: a temporary handoff repository, one watched file whose
    content is the `x` area of the data, an injected registry, a fake clock for the idle stop."""

    def __init__(self, root: Path, idle=30.0, version=None, files_text="# Widget\n"):
        self.handoff = root / "handoff"
        (self.handoff / "projects" / "acme").mkdir(parents=True)
        (self.handoff / "projects" / "acme" / "widget.md").write_text(files_text)
        (root / "secret.md").write_text("not for the page")
        self.watched = root / "watched"
        self.watched.mkdir()
        (self.watched / "x.json").write_text('{"n": 1}')
        self.clock, self.collects = FakeClock(), 0

        def collect(reg, now=None):
            self.collects += 1
            return {"v": 3, "generated": 1_800_000_000, "rendered": now, "host": "testmac", "projects": [],
                    "x": json.loads((self.watched / "x.json").read_text()),
                    "files": [{"path": "projects/acme/widget.md", "text": files_text}], "files_meta": {"capped": False}}
        self.snap = dashserver.Snapshot(build=lambda: {"projects": []}, collect=collect, paths=lambda: [self.watched],
                                        enrich=lambda reg: {}, clock=self.clock)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        self.port = sock.getsockname()[1]
        self.app, ready = None, threading.Event()

        def on_ready(app):
            self.app = app
            ready.set()
        self.thread = threading.Thread(target=dashserver.serve, args=(sock, TOKEN, self.snap), daemon=True,
                                       kwargs={"clock": self.clock, "idle_check": 0.02, "poll": 0.02, "idle": idle,
                                               "version": version, "handoff": self.handoff, "ready": on_ready})
        self.thread.start()
        assert ready.wait(5), "the server did not start"

    def request(self, path, headers=None, method="GET", host=None, auth=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if auth:
            h["Authorization"] = f"Bearer {TOKEN}"
        h.update(headers or {})
        c.request(method, path, headers=h)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, body

    def events(self, query="", headers=None):
        """An open event stream: (close, read); read() returns the next event as a dict (id, event, data)."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/v1/events" + query, headers=dict({"Host": f"127.0.0.1:{self.port}",
                                                                 "Authorization": f"Bearer {TOKEN}"}, **(headers or {})))
        r = c.getresponse()
        assert r.status == 200, r.status

        def read():
            ev = {}
            while True:
                line = r.fp.readline().decode().rstrip("\n")
                if not line:
                    if "event" in ev:
                        return ev
                    ev = {}
                    continue
                k, _, v = line.partition(": ")
                if k in ("id", "event", "data"):
                    ev[k] = json.loads(v) if k == "data" else v

        def close():  # the response holds the socket too: close both, as a browser closing the tab does
            r.close()
            c.close()
        return close, read

    def close(self):
        if self.thread.is_alive():
            self.app.shutdown()
            self.thread.join(5)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState()
        self.srv = LiveServer(self.state.root)

    def tearDown(self):
        self.srv.close()
        self.state.close()

    def sign_in(self) -> str:
        _, _, body = self.srv.request("/api/v1/ticket")
        status, headers, _ = self.srv.request("/auth?t=" + json.loads(body)["ticket"], auth=False)
        self.assertEqual(status, 303)
        return headers["set-cookie"].split(";")[0]

    def test_a_request_without_the_cookie_is_401_problem_json(self):
        status, headers, body = self.srv.request("/api/v1/dashboard", auth=False)
        self.assertEqual((status, headers["content-type"]), (401, "application/problem+json"))
        self.assertEqual(json.loads(body)["status"], 401)
        status, headers, _ = self.srv.request("/", auth=False)
        self.assertEqual((status, headers["content-type"]), (401, "text/html; charset=utf-8"))
        for rule in ("base-uri 'none'", "form-action 'none'"):  # the error page's policy too
            self.assertIn(rule, headers["content-security-policy"])
        status, _, _ = self.srv.request("/api/v1/health", auth=False, headers={"Authorization": "Bearer wrong"})
        self.assertEqual(status, 401)

    def test_another_host_is_421_problem_json(self):
        for host in ("evil.example", f"evil.example:{self.srv.port}", "127.0.0.1:1"):
            status, headers, body = self.srv.request("/api/v1/health", host=host)
            self.assertEqual((status, headers["content-type"], json.loads(body)["status"]),
                             (421, "application/problem+json", 421), host)
        self.assertEqual(self.srv.request("/api/v1/health", host=f"localhost:{self.srv.port}")[0], 200)

    def test_writes_are_405(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            status, headers, body = self.srv.request("/api/v1/dashboard", method=method)
            self.assertEqual((status, headers.get("allow")), (405, "GET, HEAD"), method)
            self.assertEqual(json.loads(body)["title"], "Method not allowed")

    def test_a_ticket_signs_the_browser_in_once(self):
        _, _, body = self.srv.request("/api/v1/ticket")
        ticket = json.loads(body)["ticket"]
        status, headers, _ = self.srv.request(f"/auth?t={ticket}&to=/tasks?task=T-1", auth=False)
        self.assertEqual((status, headers["location"]), (303, "/#/tasks?task=T-1"))
        cookie = headers["set-cookie"]
        self.assertTrue(cookie.startswith(f"devkit_dashboard_{self.srv.port}="), cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn(TOKEN, cookie)                                       # the browser never holds the CLI's token
        self.assertEqual(self.srv.request("/api/v1/health", {"Cookie": cookie.split(";")[0]}, auth=False)[0], 200)
        self.assertEqual(self.srv.request(f"/auth?t={ticket}", auth=False)[0], 401)          # once
        _, _, body = self.srv.request("/api/v1/ticket")
        late = json.loads(body)["ticket"]
        self.srv.clock.t += dashserver.TICKET_SECONDS + 1
        self.assertEqual(self.srv.request(f"/auth?t={late}", auth=False)[0], 401)            # and for a minute
        status, headers, _ = self.srv.request(f"/auth?t={self.srv.app.new_ticket()}&to=x%0d%0aSet-Cookie:%20a=b", auth=False)
        self.assertEqual((status, headers["location"]), (303, "/"))                          # no header injection

    def test_the_page_has_a_fresh_nonce_strict_headers_and_no_file_contents(self):
        cookie = self.sign_in()
        seen = set()
        for _ in range(2):
            status, headers, body = self.srv.request("/", {"Cookie": cookie}, auth=False)
            html = body.decode()
            nonce = re.search(r"script-src 'nonce-([^']+)'", headers["content-security-policy"]).group(1)
            seen.add(nonce)
            tags = re.findall(r"<(?:script|style)\b([^>]*)>", html)
            self.assertTrue(tags and all(f'nonce="{nonce}"' in t for t in tags))
            for k, v in (("x-content-type-options", "nosniff"), ("referrer-policy", "no-referrer"), ("cache-control", "no-store")):
                self.assertEqual(headers[k], v)
            self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
            self.assertIn("connect-src 'self'", headers["content-security-policy"])
            for rule in ("base-uri 'none'", "form-action 'none'"):  # default-src does not cover them: injected
                self.assertIn(rule, headers["content-security-policy"])  # markup could still send data out
            data = json.loads(re.search(r'<script[^>]* id="devkit-data" type="application/json">(.*?)</script>', html, re.S).group(1))
            self.assertEqual((data["live"], data["snapshot"], "files" in data), (True, 1, False))
        self.assertEqual(len(seen), 2)

    def test_the_dashboard_has_an_etag_and_answers_304(self):
        status, headers, body = self.srv.request("/api/v1/dashboard?omit=files")
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertEqual((d["x"], d["snapshot"], "files" in d), ({"n": 1}, 1, False))
        status, h2, body = self.srv.request("/api/v1/dashboard?omit=files", {"If-None-Match": headers["etag"]})
        self.assertEqual((status, body), (304, b""))
        self.assertEqual(h2["etag"], headers["etag"])
        _, h3, body = self.srv.request("/api/v1/dashboard?only=x")
        self.assertEqual(json.loads(body), {"x": {"n": 1}, "live": True, "snapshot": 1})
        self.assertNotEqual(h3["etag"], headers["etag"])                       # another representation, another tag

    def test_a_change_to_a_watched_file_bumps_the_version_and_the_stream_sends_it(self):
        close, read = self.srv.events("?since=1")
        (self.srv.watched / "x.json").write_text('{"n": 2, "more": true}')
        ev = read()
        self.assertEqual((ev["id"], ev["event"], ev["data"]), ("2", "snapshot", {"version": 2, "changed": ["x"]}))
        close()
        self.assertEqual(json.loads(self.srv.request("/api/v1/dashboard?only=x")[2])["x"], {"n": 2, "more": True})
        close, read = self.srv.events(headers={"Last-Event-ID": "1"})       # a page that reconnects after missing it
        self.assertEqual(read()["data"], {"version": 2, "changed": ["x"]})
        close()

    def test_the_same_data_keeps_its_version(self):
        n = self.srv.collects
        os.utime(self.srv.watched / "x.json", (1, 1))                       # touched, not changed
        until(lambda: self.srv.collects > n)
        self.assertGreater(self.srv.collects, n)
        self.assertEqual(self.srv.snap.version, 1)

    def test_files_stay_inside_the_handoff_repository(self):
        for bad in ("../../etc/passwd", "../secret.md", "/etc/passwd", "projects/../../secret.md"):
            status, headers, body = self.srv.request("/api/v1/files?path=" + bad)
            self.assertEqual((status, headers["content-type"]), (404, "application/problem+json"), bad)
        status, _, body = self.srv.request("/api/v1/files?path=projects/acme/widget.md")
        self.assertEqual((status, json.loads(body)["text"]), (200, "# Widget\n"))

    def test_details_that_do_not_exist_are_404_problems(self):
        for path in ("/api/v1/items/T-nope", "/api/v1/runs/claude-nope", "/api/v1/jobs/nope", "/api/v1/nothing"):
            status, headers, body = self.srv.request(path)
            self.assertEqual((status, headers["content-type"], json.loads(body)["status"]), (404, "application/problem+json", 404), path)


class GzipTests(unittest.TestCase):
    def test_large_answers_are_compressed_for_a_client_that_accepts_it(self):
        state = TempState()
        srv = LiveServer(state.root, files_text="# Big\n" + "line of text\n" * 400)
        try:
            status, headers, body = srv.request("/api/v1/files", {"Accept-Encoding": "gzip"})
            self.assertEqual((status, headers.get("content-encoding"), headers.get("vary")), (200, "gzip", "Accept-Encoding"))
            self.assertEqual(json.loads(gzip.decompress(body))["files"][0]["path"], "projects/acme/widget.md")
            _, headers, body = srv.request("/api/v1/files")
            self.assertNotIn("content-encoding", headers)
            self.assertIn(b"line of text", body)
        finally:
            srv.close()
            state.close()


class IdleTests(unittest.TestCase):
    def test_it_stops_after_the_idle_minutes_with_no_page_open_and_never_while_one_is(self):
        state = TempState()
        srv = LiveServer(state.root, idle=1)
        try:
            dashserver.write_state({"pid": os.getpid(), "port": srv.port, "token": TOKEN})
            close, read = srv.events("?since=1")
            self.assertEqual(until(lambda: srv.app.clients), 1)
            srv.clock.t += 600                                              # 10 minutes with a page open
            self.assertFalse(until(lambda: not srv.thread.is_alive(), timeout=0.5))
            close()
            self.assertEqual(until(lambda: srv.app.clients == 0), True)
            srv.clock.t += 30                                               # half a minute after the page closed
            self.assertFalse(until(lambda: not srv.thread.is_alive(), timeout=0.3))
            srv.clock.t += 31
            self.assertTrue(until(lambda: not srv.thread.is_alive(), timeout=3))
            self.assertFalse(dashserver.state_file().exists())               # its own state file is gone
        finally:
            srv.close()
            state.close()


class StateFileTests(unittest.TestCase):
    def test_the_state_file_is_0600_whatever_the_umask(self):
        state = TempState()
        old = os.umask(0)
        try:
            dashserver.write_state({"pid": 1, "port": 2, "token": TOKEN})
            dashserver.write_state({"pid": 3, "port": 4, "token": TOKEN})   # replaced, still private
            self.assertEqual(os.stat(dashserver.state_file()).st_mode & 0o777, 0o600)
            self.assertEqual(dashserver.read_state()["pid"], 3)
        finally:
            os.umask(old)
            state.close()

    def test_stop_never_signals_a_process_that_is_not_a_dashboard_server(self):
        state = TempState()
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            dashserver.write_state({"pid": other.pid, "port": 9, "token": TOKEN})
            self.assertFalse(dashserver.stop(wait=0.2))
            self.assertIsNone(other.poll())                                 # still running
            self.assertFalse(dashserver.state_file().exists())
        finally:
            other.kill()
            other.wait()
            state.close()


class ProcessTests(unittest.TestCase):
    """`ensure()` with real background servers (python -m devkit.dashserver serve --fd N) in a temporary HOME."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        cfg = self.home / ".config" / "devkit"
        cfg.mkdir(parents=True)
        (cfg / "config.json").write_text(json.dumps({"github_user": "tester", "dashboard": {"idle_minutes": 2}}))
        self.old_state = core.STATE
        core.STATE = self.home / ".local" / "state" / "devkit"
        core.STATE.mkdir(parents=True)
        (core.STATE / "providers.json").write_text("{}")                  # fresh: no agent CLI is asked
        env = {k: v for k, v in os.environ.items() if k not in ("DEVKIT_CONFIG", "PYTHONPATH")}
        env.update(HOME=str(self.home))
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()

    def tearDown(self):
        st = dashserver.read_state()
        if st.get("pid") and st.get("pid") != os.getpid():
            dashserver.stop(wait=5)
            if dashserver._alive(st["pid"]) and dashserver._is_ours(st["pid"]):
                os.kill(st["pid"], signal.SIGKILL)
        self.env.stop()
        core.STATE = self.old_state
        self.tmp.cleanup()

    def test_ensure_starts_one_then_reuses_it_and_replaces_a_dead_one(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        dashserver.write_state({"pid": dead.pid, "port": port, "token": TOKEN, "version": core.VERSION, "started": 1})
        first = dashserver.ensure()
        self.assertFalse(first["reused"])
        self.assertNotEqual(first["port"], port)
        url = dashserver.browser_url(first)                                   # waits for its first snapshot
        self.assertRegex(url, rf"^http://127\.0\.0\.1:{first['port']}/auth\?t=[\w-]+$")
        self.assertEqual(os.stat(dashserver.state_file()).st_mode & 0o777, 0o600)
        ps = subprocess.run(["ps", "-p", str(first["pid"]), "-o", "command="], capture_output=True, text=True).stdout
        self.assertIn("devkit.dashserver serve --fd", ps)
        self.assertNotIn(first["token"], ps)                                  # the token is not on the command line
        again = dashserver.ensure()
        self.assertEqual((again["reused"], again["port"], again["pid"]), (True, first["port"], first["pid"]))
        s = dashserver.status()
        self.assertEqual((s["running"], s["current"], s["version"]), (True, True, core.VERSION))
        self.assertTrue(dashserver.stop())
        self.assertFalse(dashserver._alive(first["pid"]))
        self.assertEqual(dashserver.status(), {"running": False})

    def test_ensure_replaces_a_server_of_another_version(self):
        state_root = Path(tempfile.mkdtemp(dir=self.tmp.name))
        old = LiveServer(state_root, version="0.0.1")
        try:
            dashserver.write_state({"pid": os.getpid(), "port": old.port, "token": TOKEN, "version": "0.0.1",
                                    "started": int(time.time()) - 3600})
            self.assertEqual(dashserver.status()["current"], False)
            new = dashserver.ensure()
            self.assertFalse(new["reused"])
            self.assertNotEqual(new["port"], old.port)
            dashserver.browser_url(new)
            self.assertEqual(dashserver.status()["version"], core.VERSION)
            self.assertTrue(old.thread.is_alive())                            # the test process was not signalled
        finally:
            old.close()


class CommandTests(unittest.TestCase):
    def setUp(self):
        import argparse
        self.state = TempState()
        self.parser = dashserver.add_arguments(argparse.ArgumentParser(prog="dev dashboard"))

    def tearDown(self):
        self.state.close()

    def run_cmd(self, *argv):
        from io import StringIO
        out = StringIO()
        with mock.patch("sys.stdout", out):
            rc = dashserver.cmd_dashboard(self.parser.parse_args(list(argv)))
        return rc, out.getvalue()

    def test_status_says_stopped_and_fails_when_no_server_runs(self):
        rc, out = self.run_cmd("--status")
        self.assertEqual(rc, 1)
        self.assertIn("dashboard stopped", out)

    def test_export_writes_the_static_page_without_live_parts(self):
        from devkit import registry
        target = self.state.root / "out" / "dash.html"
        with mock.patch.object(registry, "build", lambda **kw: {"projects": [], "host": "testmac", "generated": 1_800_000_000}):
            rc, out = self.run_cmd("--export", str(target))
        html = target.read_text()
        self.assertEqual(rc, 0)
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertNotIn("nonce=", html)
        self.assertNotIn('"live":true', html)
        self.assertIn("dash.html", out)
        self.assertEqual(html.count("</script>"), 3)

    def test_an_unknown_action_is_refused(self):
        with mock.patch("sys.stderr"):
            rc, _ = self.run_cmd("bogus")
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
