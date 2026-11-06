# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev mcp` (the MCP server), the Claude Desktop launcher and `dev desktop pack|status`. Hermetic: every process
runs with a throwaway HOME, config and dev root, and nothing touches the network."""
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from devkit import audit, cli, core, desktop, mcp  # noqa: E402

LAUNCHER = ROOT / "desktop" / "server" / "launcher.py"


def hermetic_env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("DEVKIT_HOME", "DEVKIT_SESSION", "DEVKIT_AGENT", "CLAUDECODE", "DEV_ROOT", "DEVKIT_CONFIG")}
    env.update(HOME=str(home), DEVKIT_CONFIG=str(home / ".config" / "devkit" / "config.json"),
               DEV_ROOT=str(home / "Development"), DEVKIT_NO_AUTOSYNC="1", DEVKIT_NO_NOTIFY="1", NO_COLOR="1")
    return env


class Session:
    """An MCP server process driven over stdio, one JSON message per line, with a timeout on every read."""

    def __init__(self, argv, env, cwd):
        self.err = tempfile.TemporaryFile()
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.err, env=env,
                                  cwd=str(cwd))
        self.lines, self.n = queue.Queue(), 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.p.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def send(self, msg):
        self.p.stdin.write((msg if isinstance(msg, str) else json.dumps(msg)).encode() + b"\n")
        self.p.stdin.flush()

    def recv(self, timeout=60):
        line = self.lines.get(timeout=timeout)
        if line is None:
            self.err.seek(0)
            raise AssertionError("server closed stdout: " + self.err.read().decode(errors="replace")[-800:])
        return json.loads(line)

    def request(self, method, params=None):
        self.n += 1
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        reply = self.recv()
        assert reply.get("id") == self.n, reply
        return reply

    def call(self, name, arguments=None):
        return self.request("tools/call", {"name": name, "arguments": arguments or {}})

    def close(self):
        try:
            self.p.stdin.close()
            return self.p.wait(timeout=30)
        finally:
            self.reader.join(timeout=10)
            self.p.stdout.close()
            self.err.close()


class TempHome:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "Development").mkdir()
        return self

    def __exit__(self, *exc):
        self.tmp.cleanup()

    def env(self):
        return hermetic_env(self.home)


class ServerTests(unittest.TestCase):
    """`bin/dev mcp` over stdio."""

    @classmethod
    def setUpClass(cls):
        cls.h = TempHome().__enter__()
        audit_dir = cls.h.home / ".local" / "state" / "devkit" / "audit"
        audit_dir.mkdir(parents=True)
        recs = [{"ts": time.time() - 60, "user": "t", "host": "mac", "agent": "claude", "action": "guard.deny",
                 "target": "git push --force", "outcome": "denied"},
                {"ts": time.time() - 30, "user": "t", "host": "mac", "agent": "human", "action": "task.add",
                 "target": "T-1", "outcome": "ok"}]
        (audit_dir / time.strftime("%Y-%m.jsonl")).write_text("".join(json.dumps(r) + "\n" for r in recs))
        cls.s = Session([sys.executable, str(ROOT / "bin" / "dev"), "mcp"], cls.h.env(), cls.h.home)
        cls.init = cls.s.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                "clientInfo": {"name": "test", "version": "1"}})
        cls.s.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    @classmethod
    def tearDownClass(cls):
        cls.s.close()
        cls.h.__exit__(None, None, None)

    def test_initialize(self):
        r = self.init["result"]
        self.assertEqual(r["protocolVersion"], "2025-06-18")
        self.assertEqual(r["serverInfo"], {"name": "devkit", "version": core.VERSION})
        self.assertIn("tools", r["capabilities"])
        self.assertIn("**[Firefly> DevKit]**", r["instructions"])
        self.assertIn("- **Check:**", r["instructions"])

    def test_unknown_protocol_version_gets_the_newest(self):
        r = self.s.request("initialize", {"protocolVersion": "1999-01-01", "capabilities": {}})["result"]
        self.assertEqual(r["protocolVersion"], mcp.PROTOCOL_VERSIONS[0])
        r = self.s.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})["result"]
        self.assertEqual(r["protocolVersion"], "2025-06-18")

    def test_ping_and_unknown_method(self):
        self.assertEqual(self.s.request("ping")["result"], {})
        self.assertEqual(self.s.request("resources/list")["error"]["code"], -32601)

    def test_parse_error_and_invalid_request(self):
        self.s.send("this is not json")
        r = self.s.recv()
        self.assertEqual((r["id"], r["error"]["code"]), (None, -32700))
        self.s.send({"jsonrpc": "1.0", "id": 99, "method": "ping"})
        r = self.s.recv()
        self.assertEqual((r["id"], r["error"]["code"]), (99, -32600))

    def test_tools_list_matches_the_definitions(self):
        tools = self.s.request("tools/list")["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], [t["name"] for t in mcp.TOOLS])
        self.assertEqual(len(tools), 19)
        for t in tools:
            with self.subTest(tool=t["name"]):
                self.assertTrue(t["name"].startswith("devkit_"))
                self.assertGreater(len(t["description"]), 60)
                self.assertEqual(t["inputSchema"]["type"], "object")
                for hint in ("readOnlyHint", "destructiveHint", "idempotentHint"):
                    self.assertIsInstance(t["annotations"][hint], bool)
        writes = {t["name"] for t in tools if not t["annotations"]["readOnlyHint"]}
        self.assertEqual(writes, {"devkit_task_add", "devkit_task_update"})
        # K5: devkit_task_update can block, finish or release any project's task and its lock
        destructive = {t["name"] for t in tools if t["annotations"]["destructiveHint"]}
        self.assertEqual(destructive, {"devkit_task_update"})

    def test_call_returns_json_as_text_and_structured_content(self):
        r = self.s.call("devkit_audit", {"days": 1, "action": "guard"})["result"]
        self.assertNotIn("isError", r)
        self.assertEqual(r["structuredContent"]["total"], 1)
        self.assertEqual(r["structuredContent"]["records"][0]["action"], "guard.deny")
        self.assertEqual(json.loads(r["content"][0]["text"]), r["structuredContent"])

    def test_crew_lists_the_roster_without_a_running_run(self):
        r = self.s.call("devkit_crew")["result"]
        self.assertNotIn("isError", r)
        crew = r["structuredContent"]
        self.assertEqual(len(crew["members"]), 26)
        self.assertEqual(crew["members"][0]["id"], "morpheus")
        self.assertEqual(next(m for m in crew["members"] if m["id"] == "neo")["spawn"]["claude_code"], "devkit:neo")
        self.assertNotIn("run", crew)  # nothing is running in the throwaway HOME

    def test_call_returns_plain_text(self):
        r = self.s.call("devkit_lessons")["result"]
        self.assertNotIn("isError", r)
        self.assertIn("Lessons", r["content"][0]["text"])
        self.assertNotIn("\x1b[", r["content"][0]["text"])

    def test_unknown_tool_is_a_protocol_error(self):
        r = self.s.call("devkit_nope")
        self.assertEqual(r["error"]["code"], -32602)
        self.assertIn("devkit_nope", r["error"]["message"])

    def test_bad_arguments_are_tool_errors(self):
        for name, args, needle in (("devkit_task_add", {"when": "now"}, "`title` is required"),
                                   ("devkit_task_add", {"title": "x y z", "when": "soon"}, "one of"),
                                   ("devkit_tasks", {"project": "--all"}, "cannot contain"),
                                   ("devkit_tasks", {"project": "a/b", "initiative": "c"}, "not both"),
                                   ("devkit_standup", {"days": 0}, "between"),
                                   ("devkit_jobs", {"verbose": True}, "unknown argument")):
            with self.subTest(name=name, args=args):
                r = self.s.call(name, args)["result"]
                self.assertTrue(r.get("isError"))
                self.assertIn(needle, r["content"][0]["text"])

    def test_dev_failure_is_a_tool_error_with_its_message(self):
        r = self.s.call("devkit_tasks", {"project": "no-such/project"})["result"]
        self.assertTrue(r.get("isError"))
        self.assertIn("no project matches", r["content"][0]["text"])


class ArgvTests(unittest.TestCase):
    """The arguments each tool gives `dev` parse as intended (user text never becomes an option)."""

    def parse(self, name, args):
        return cli.build_parser().parse_args(mcp.build_argv(name, mcp.validate(name, args)))

    def test_task_add(self):
        a = self.parse("devkit_task_add", {"title": "-p evil --later title", "project": "acme/api", "when": "now",
                                           "type": "bug", "why": "-- because"})
        self.assertEqual((a.cmd, a.task_cmd, a.text, a.project, a.now, a.later, a.type, a.why),
                         ("task", "add", ["-p evil --later title"], "acme/api", True, False, "bug", "-- because"))

    def test_task_update(self):
        a = self.parse("devkit_task_update", {"id": "T-1", "action": "block", "why": "waiting on Finance",
                                              "initiative": "lending"})
        self.assertEqual((a.task_cmd, a.selector, a.initiative, a.why), ("block", ["T-1"], "lending", "waiting on Finance"))

    def test_reads_stay_unaudited(self):
        for name, args in (("devkit_tasks", {"project": "acme/api"}), ("devkit_lessons", {"project": "acme/api"}),
                           ("devkit_delivery_policy", {"project": "acme/api"}), ("devkit_overview", {}),
                           ("devkit_ci_status", {"project": "acme/api"}), ("devkit_jobs", {})):
            with self.subTest(name=name):
                self.assertFalse(audit.audited(mcp.build_argv(name, mcp.validate(name, args))))
        self.assertTrue(audit.audited(mcp.build_argv("devkit_task_add", mcp.validate(
            "devkit_task_add", {"title": "Export invoices", "when": "next"}))))

    def test_mcp_and_desktop_status_are_read_only(self):
        self.assertFalse(audit.audited(["mcp"]))
        self.assertFalse(audit.audited(["desktop", "status"]))
        self.assertTrue(audit.audited(["desktop", "pack"]))

    def test_text_is_compacted(self):
        raw = "\x1b[1m  Lessons · 2\x1b[0m ─────────\n  ██ ▄▄\n  JOB  STATE\n  ───  ─────\n\n\n\n  sync on"
        self.assertEqual(mcp.clean(raw), "  Lessons · 2\n  JOB  STATE\n\n  sync on")


class Capture:
    """A server's output stream, kept as parsed messages."""

    def __init__(self):
        self.msgs = []

    def write(self, data):
        self.msgs.append(json.loads(data))

    def flush(self):
        pass


class RunTests(unittest.TestCase):
    """Timeouts, progress notifications and cancellation, with a slow stand-in for `dev`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        slow = Path(self.tmp.name) / "slow-dev"
        slow.write_text("import time\nprint('started', flush=True)\ntime.sleep(30)\n")
        self.saved = mcp.DEV_BIN, mcp.PROGRESS_EVERY
        mcp.DEV_BIN, mcp.PROGRESS_EVERY = slow, 0.2

    def tearDown(self):
        mcp.DEV_BIN, mcp.PROGRESS_EVERY = self.saved
        self.tmp.cleanup()

    def test_timeout_stops_the_command(self):
        t0 = time.monotonic()
        rc, out, _ = mcp.run_dev(["jobs"], timeout=1, cwd=self.tmp.name)
        self.assertIsNone(rc)
        self.assertIn("started", out)
        self.assertLess(time.monotonic() - t0, 10)

    def test_doctor_failures_are_a_report_other_failures_are_errors(self):
        mcp.DEV_BIN = Path(self.tmp.name) / "failing-dev"
        mcp.DEV_BIN.write_text("import sys\nprint('  ✗ gh authenticated   fix: gh auth login')\n"
                               "print('no such job', file=sys.stderr)\nsys.exit(1)\n")
        r = mcp.call_tool("devkit_doctor", {})
        self.assertNotIn("isError", r)
        self.assertIn("found problems (exit 1)", r["content"][0]["text"])
        self.assertIn("fix: gh auth login", r["content"][0]["text"])
        r = mcp.call_tool("devkit_jobs", {})
        self.assertTrue(r["isError"])
        self.assertEqual(r["content"][0]["text"], "no such job")

    def test_progress_then_cancel_sends_no_response(self):
        out = Capture()
        server = mcp.Server(None, out)
        server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                       "params": {"name": "devkit_jobs", "_meta": {"progressToken": "tok"}}})
        deadline = time.monotonic() + 10
        while sum(m.get("method") == "notifications/progress" for m in out.msgs) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        t0 = time.monotonic()
        server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 5}})
        server.shutdown()
        self.assertLess(time.monotonic() - t0, 10)
        progress = [m for m in out.msgs if m.get("method") == "notifications/progress"]
        self.assertTrue(len(progress) >= 2 and progress[0]["params"]["progressToken"] == "tok", out.msgs)
        values = [m["params"]["progress"] for m in progress]
        self.assertEqual(values, sorted(set(values)), "progress must increase with every notification")
        self.assertFalse([m for m in out.msgs if m.get("id") == 5], "a cancelled request gets no response")


class DigestTests(unittest.TestCase):
    def test_overview_keeps_what_a_reply_needs(self):
        data = {"days": 7, "generated": 1793948774, "issues": [["layout", "x is misplaced"]], "initiatives": [
            {"id": "lending", "title": "Lending", "members": [{"id": "acme/core", "local": False}],
             "tasks": {"now": ["Ship it"], "open": 3, "overdue": ["a"], "items": [{"id": "T-1"}] * 50},
             "decisions": "long text " * 500}],
            "projects": [
                {"id": "acme/api", "status": "active", "local": True, "now": ["Export"], "commits_7d": 4,
                 "status_line": {"line": "**[Firefly> DevKit]** api · `main` · Export", "agent": "claude", "ts": 1},
                 "purpose": "p" * 160, "path": "/x"},
                {"id": "acme/core", "status": "active", "local": False, "live_sessions": 0},
                {"id": "acme/old", "status": "archived", "local": True}]}
        d = mcp._digest_overview(data, {"days": 7, "include_all": False})
        self.assertEqual([p["id"] for p in d["projects"]], ["acme/api"])
        self.assertIn("2 project(s) not shown", d["hidden_projects"])
        self.assertEqual(d["initiatives"][0]["tasks"]["overdue"], 1)
        self.assertNotIn("decisions", d["initiatives"][0])
        self.assertEqual(d["projects"][0]["status_block"], "**[Firefly> DevKit]** api · `main` · Export")
        self.assertEqual(len(mcp._digest_overview(data, {"days": 7, "include_all": True})["projects"]), 3)

    def test_audit_digest_names_who_from_the_schema_v2_actor(self):
        # `dev audit --json` prints schema v2 records: user, host and agent live under `actor`, not at the top level
        rec = {"v": 2, "id": "01J", "ts": 1793948774.0, "time": "2026-11-06T10:26:14Z", "action": "guard.deny",
               "target": "git push", "outcome": "deny", "detail": "",
               "actor": {"user": "ann", "host": "mac", "agent": "claude", "session": "s1"}}
        d = mcp._digest_audit([rec], {"limit": 50})
        self.assertEqual((d["records"][0]["who"], d["records"][0]["agent"]), ("ann@mac", "claude"))


class ManifestTests(unittest.TestCase):
    def test_manifest_tools_match_the_server(self):
        m = desktop.source_manifest()
        self.assertEqual([t["name"] for t in m["tools"]], [t["name"] for t in mcp.TOOLS])
        self.assertEqual(desktop.check_tools(m), [])

    def test_manifest_shape(self):
        m = desktop.source_manifest()
        self.assertEqual((m["manifest_version"], m["name"], m["display_name"]), ("0.4", "devkit", "devkit"))
        self.assertEqual(m["server"]["type"], "python")
        self.assertEqual(m["server"]["entry_point"], "server/launcher.py")
        self.assertEqual(m["server"]["mcp_config"]["args"], ["${__dirname}/server/launcher.py"])
        self.assertEqual(m["server"]["mcp_config"]["env"]["DEVKIT_HOME"], "${user_config.devkit_home}")
        self.assertEqual(m["user_config"]["devkit_home"]["type"], "directory")
        self.assertFalse(m["user_config"]["devkit_home"]["required"])
        self.assertEqual(m["author"]["name"], "Firefly Software Solutions Inc.")
        self.assertTrue((ROOT / "desktop" / m["server"]["entry_point"]).is_file())

    def test_check_tools_reports_drift(self):
        m = desktop.source_manifest()
        m["tools"] = m["tools"][1:] + [{"name": "devkit_extra", "description": "x"}]
        problems = " ".join(desktop.check_tools(m))
        self.assertIn("lacks tools the server has: devkit_overview", problems)
        self.assertIn("does not have: devkit_extra", problems)


class LauncherTests(unittest.TestCase):
    def check(self, env):
        r = subprocess.run([sys.executable, str(LAUNCHER), "--check"], env=env, capture_output=True, text=True,
                           timeout=30, stdin=subprocess.DEVNULL)
        return r.returncode, json.loads(r.stdout)

    def test_fallback_when_no_checkout(self):
        with TempHome() as h:
            env = dict(h.env(), DEVKIT_HOME="${user_config.devkit_home}")  # an unfilled setting means "not set"
            rc, found = self.check(env)
            self.assertEqual((rc, found["ok"]), (1, False))
            s = Session([sys.executable, str(LAUNCHER)], env, h.home)
            try:
                r = s.request("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}})["result"]
                self.assertEqual((r["protocolVersion"], r["serverInfo"]["name"]), ("2025-03-26", "devkit"))
                tools = s.request("tools/list")["result"]["tools"]
                self.assertEqual([t["name"] for t in tools], ["devkit_status"])
                text = s.call("devkit_status")["result"]["content"][0]["text"]
                self.assertIn("No devkit checkout was found", text)
                self.assertIn("./install.sh", text)
                self.assertEqual(s.call("devkit_overview")["error"]["code"], -32602)
            finally:
                self.assertEqual(s.close(), 0)

    def test_old_checkout_is_reported(self):
        with TempHome() as h:
            old = h.home / "old-devkit"
            (old / "bin").mkdir(parents=True)
            (old / "bin" / "dev").write_text("#!/usr/bin/env python3\n")
            (old / "lib" / "devkit").mkdir(parents=True)
            (old / "lib" / "devkit" / "cli.py").write_text("")
            rc, found = self.check(dict(h.env(), DEVKIT_HOME=str(old)))
            self.assertEqual(rc, 1)
            self.assertIn("older than this extension", found["reason"])

    def test_finds_the_checkout_from_the_setting_and_from_dev_setup_link(self):
        with TempHome() as h:
            rc, found = self.check(dict(h.env(), DEVKIT_HOME=str(ROOT)))
            self.assertEqual((rc, found["home"], found["source"]), (0, os.path.realpath(ROOT), "DEVKIT_HOME"))
            (h.home / ".local" / "bin").mkdir(parents=True)
            (h.home / ".local" / "bin" / "dev").symlink_to(ROOT / "bin" / "dev")
            rc, found = self.check(dict(h.env(), DEVKIT_HOME=""))
            self.assertEqual((rc, found["home"], found["source"]), (0, os.path.realpath(ROOT), "~/.local/bin/dev"))

    def test_hands_over_to_dev_mcp(self):
        with TempHome() as h:
            s = Session([sys.executable, str(LAUNCHER)], dict(h.env(), DEVKIT_HOME=str(ROOT)), h.home)
            try:
                r = s.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}})["result"]
                self.assertEqual(r["serverInfo"], {"name": "devkit", "version": core.VERSION})
                self.assertEqual(len(s.request("tools/list")["result"]["tools"]), len(mcp.TOOLS))
            finally:
                self.assertEqual(s.close(), 0)


class PackTests(unittest.TestCase):
    def test_pack_writes_a_bundle_with_the_manifest_at_its_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            rep = desktop.pack(Path(tmp), validate=False, say=lambda _m: None)
            path = Path(rep["bundle"])
            self.assertEqual(path.name, f"devkit-{core.VERSION}.mcpb")
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
                m = json.loads(z.read("manifest.json"))
                launcher = z.read("server/launcher.py").decode()
            self.assertTrue(set(desktop.FILES) <= names, names)
            self.assertEqual(m["version"], core.VERSION)
            self.assertEqual([t["name"] for t in m["tools"]], [t["name"] for t in mcp.TOOLS])
            self.assertEqual(launcher, LAUNCHER.read_text())
            self.assertEqual(rep["packed_by"], "zip")
            self.assertEqual(rep["size"], path.stat().st_size)


class StatusTests(unittest.TestCase):
    def test_reads_what_claude_desktop_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.assertFalse(desktop.status(base)["installed"])
            (base / "Claude Extensions Settings").mkdir()
            (base / "extensions-installations.json").write_text(json.dumps({"extensions": {desktop.EXT_ID: {
                "id": desktop.EXT_ID, "version": "26.10.0", "installedAt": "2026-10-01T09:00:00.000Z",
                "manifest": {"name": "devkit"}, "signatureInfo": {"status": "unsigned"}}}}))
            (base / "Claude Extensions Settings" / f"{desktop.EXT_ID}.json").write_text(json.dumps(
                {"isEnabled": True, "userConfig": {"devkit_home": ""}}))
            s = desktop.status(base)
            self.assertEqual((s["installed"], s["installed_version"], s["current"], s["enabled"]),
                             (True, "26.10.0", core.VERSION == "26.10.0", True))
            self.assertEqual(s["signature"], "unsigned")


if __name__ == "__main__":
    unittest.main()
