# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
import contextlib
import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.test_audit import AuditCase
from devkit import audit, backup, branches, core, gitx, hooks, identity, progress, registry


class CodexAuditIdentityTests(AuditCase):
    def setUp(self):
        super().setUp()
        for patch in (mock.patch.dict(os.environ), mock.patch.dict(audit._FOUND, clear=True),
                      mock.patch.object(progress, "_ancestor_pids", return_value=[456, 123, 1]),
                      mock.patch.object(progress, "CALLS", self.root / "calls"),
                      mock.patch.object(hooks, "STATE", self.root)):
            patch.start()
            self.addCleanup(patch.stop)
        for key in (*core.AGENT_MARKERS, "DEVKIT_SESSION", "DEVKIT_AGENT", "DEVKIT_FROM_AGENT",
                    "DEVKIT_SPEAKER", "DEVKIT_HEADLESS"):
            os.environ.pop(key, None)

    def record(self, **env):
        with mock.patch.dict(os.environ, env):
            return audit.build("dev.task", ["add", "audit attribution"])

    def named(self, record):
        return record["actor"]["agent"], record["actor"]["session"]

    def session(self, agent, sid, lane="", pid=456):
        progress.SESSIONS.mkdir(parents=True, exist_ok=True)
        progress.RUNS.mkdir(parents=True, exist_ok=True)
        progress.session_path(agent, sid).write_text(json.dumps({
            "agent": agent, "session": sid, "pid": pid, "project": "acme/" + sid, "branch": "fix/" + sid,
        }))
        progress.run_path(agent, sid).write_text(json.dumps({
            "agent": agent, "session": sid, "goal": "Work for " + sid, "project": "acme/" + sid,
            "state": "running", "agents": {lane: {"persona": "neo", "state": "running"}} if lane else {},
            "plan": [{"name": "Implement " + sid, "owner": lane or "main", "state": "running"}],
        }))

    def test_native_thread_is_audit_metadata_without_granting_a_session(self):
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "native-thread"}):
            before = identity.speaker().as_dict()
            record = self.record()
            self.assertEqual(identity.speaker().as_dict(), before)
            self.assertEqual(before["how"], "human")
        self.assertEqual(self.named(record), ("codex", "native-threa"))

    def test_native_thread_replaces_inherited_claude_run_and_foreign_lane(self):
        self.session("claude", "old", "foreign-lane", pid=999)
        self.session("codex", "native", "own-lane")
        launcher = {"DEVKIT_AGENT": "claude", "DEVKIT_SESSION": "claude:old", "CLAUDECODE": "1",
                    "DEVKIT_FROM_AGENT": "foreign-lane"}
        baseline = self.record(**launcher)
        self.assertEqual(self.named(baseline), ("claude", "old"))
        self.assertEqual(baseline["run"], "Work for old")
        self.assertEqual(baseline["crew"]["lane"], "foreign-lane")
        record = self.record(**launcher, CODEX_THREAD_ID="native")
        self.assertEqual(self.named(record), ("codex", "native"))
        self.assertEqual((record["project"], record["run"]), ("acme/native", "Work for native"))
        self.assertNotIn("crew", record)
        self.assertNotIn("step", record)
        own = self.record(**{**launcher, "DEVKIT_FROM_AGENT": "own-lane"}, CODEX_THREAD_ID="native")
        self.assertEqual((own["crew"]["lane"], own["step"]), ("own-lane", "Implement native"))

    def test_registered_claude_process_keeps_its_identity_despite_inherited_native_marker(self):
        self.session("claude", "current", pid=456)
        self.session("codex", "launcher", pid=123)
        env = {"DEVKIT_AGENT": "claude", "DEVKIT_SESSION": "claude:current", "CLAUDECODE": "1",
               "CODEX_THREAD_ID": "launcher"}
        progress.call_opened("claude", {"session_id": "current", "tool_use_id": "expired",
                                        "tool_input": {"command": "dev task"}}, now=1)
        record = self.record(**env)
        self.assertEqual(self.named(record), ("claude", "current"))
        self.assertEqual(record["run"], "Work for current")
        expired = [r for r in self.lines() if r["action"] == "calls.unclosed"]
        # Lookup may expire calls; the legacy explicit-variable path does not. Any resulting record stays Claude.
        for nested in expired:
            self.assertEqual(self.named(nested), ("claude", "current"))
        # The inverse has a nearer Codex process and an inherited, older Claude parent.
        self.session("claude", "current", pid=123)
        self.session("codex", "launcher", pid=456)
        record = self.record(**env)
        self.assertEqual(self.named(record), ("codex", "launcher"))
        self.assertEqual(record["run"], "Work for launcher")

    def test_native_child_keeps_only_its_registered_parent_run(self):
        self.session("codex", "parent", "native-child")
        self.session("codex", "foreign", "someone-else", pid=999)
        env = {"CODEX_THREAD_ID": "native-child", "DEVKIT_AGENT": "codex",
               "DEVKIT_SESSION": "codex:parent", "DEVKIT_FROM_AGENT": "native-child"}
        record = self.record(**env)
        self.assertEqual(self.named(record), ("codex", "parent"))
        self.assertEqual((record["run"], record["crew"]["lane"]), ("Work for parent", "native-child"))
        for stamp in ("codex:foreign", "codex:missing", "codex:../parent"):
            with self.subTest(stamp=stamp):
                record = self.record(**{**env, "DEVKIT_SESSION": stamp})
                self.assertEqual(self.named(record), ("codex", "native-child"))
                self.assertNotIn("run", record)
                self.assertNotIn("crew", record)
        # A registered parent with no ownership of this child is still a foreign run.
        self.session("codex", "parent", "someone-else")
        record = self.record(**env)
        self.assertEqual(self.named(record), ("codex", "native-child"))
        self.assertNotIn("run", record)

    def test_native_record_metadata_cannot_relax_backup_or_embargo_member_restrictions(self):
        # The native thread cannot claim this root for an audit record, but the existing restriction still
        # knows the command carries a crew lane. Record attribution must not turn that caller into the lead.
        ctx = SimpleNamespace(pid="acme/repo", top=self.root)
        for provider in ("claude", "codex"):
            self.session(provider, "parent", "native-child", pid=999)
            env = {"CODEX_THREAD_ID": "native-child", "DEVKIT_AGENT": provider,
                   "DEVKIT_SESSION": f"{provider}:parent", "DEVKIT_FROM_AGENT": "native-child"}
            with mock.patch.dict(os.environ, env), mock.patch.object(gitx, "ctx", return_value=ctx):
                with self.subTest(provider=provider, action="member"):
                    self.assertEqual(backup.crew_member(), "neo")
                policy = SimpleNamespace(branches=["policy", "on"], project=None,
                                         why="verify member restriction", dry_run=True)
                public = SimpleNamespace(branches=[], project=None, why=None, public=True,
                                         all=False, accept_ci=False, dry_run=True, json=False)
                embargo = SimpleNamespace(branch="fix/sensitive", lift=True)
                for action, command, args in (("policy", backup.cmd_policy, policy),
                                               ("public", backup.cmd_backup, public),
                                               ("embargo", branches.cmd_embargo, embargo)):
                    out = io.StringIO()
                    with self.subTest(provider=provider, action=action), \
                            mock.patch.object(core, "gitout", return_value=str(self.root)), \
                            mock.patch.object(core, "project_identity", return_value=("acme/repo",)), \
                            mock.patch.object(registry, "main_of", return_value=self.root), \
                            mock.patch.object(branches, "embargoes", return_value={}), \
                            mock.patch.object(backup, "run", side_effect=AssertionError("backup must not run")), \
                            mock.patch.object(branches, "set_embargo", side_effect=AssertionError("embargo must stay")), \
                            contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                        self.assertEqual(command(args), 2)
                        self.assertIn("developer's own decision", out.getvalue())

    def test_native_ids_are_bounded_without_changing_empty_or_malformed_fallback(self):
        for thread in ("", " ", "../other", "line\nbreak", "x" * 129):
            with self.subTest(thread=thread):
                record = self.record(CODEX_THREAD_ID=thread, DEVKIT_SESSION="claude:legacy", DEVKIT_AGENT="claude")
                self.assertEqual(self.named(record), ("claude", "legacy"))
        record = self.record(CODEX_THREAD_ID="a" * 128)
        self.assertEqual(self.named(record), ("codex", "a" * 12))

    def test_nested_hook_payloads_win_and_restore_native_identity_on_success_and_exit(self):
        os.environ.update(CODEX_THREAD_ID="native", DEVKIT_SESSION="claude:inherited", DEVKIT_AGENT="claude")
        for exits in (False, True):
            with self.subTest(exits=exits):
                seen = []

                def inner(agent):
                    seen.append(self.named(self.record()))
                    raise SystemExit(7)

                def outer(agent):
                    seen.append(self.named(self.record()))
                    with mock.patch("sys.stdin", io.StringIO('{"session_id":"codex-hook"}')):
                        with self.assertRaises(SystemExit):
                            hooks.run("audit-inner", "codex")
                    seen.append(self.named(self.record()))
                    if exits:
                        raise SystemExit(8)

                with mock.patch.dict(hooks.HANDLERS, {"audit-outer": outer, "audit-inner": inner}), \
                        mock.patch("sys.stdin", io.StringIO('{"session_id":"claude-hook"}')):
                    if exits:
                        with self.assertRaises(SystemExit):
                            hooks.run("audit-outer", "claude")
                    else:
                        self.assertEqual(hooks.run("audit-outer", "claude"), 0)
                self.assertEqual(seen, [("claude", "claude-hook"), ("codex", "codex-hook"),
                                        ("claude", "claude-hook")])
                self.assertEqual(self.named(self.record()), ("codex", "native"))
                self.assertEqual(os.environ["DEVKIT_SESSION"], "claude:inherited")


if __name__ == "__main__":
    unittest.main()
