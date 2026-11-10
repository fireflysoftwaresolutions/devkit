# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
import json
import os
import shlex
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import hooks, progress as pg  # noqa: E402
from test_progress import TempState  # noqa: E402


class CodexSessionRoutingTests(unittest.TestCase):
    def setUp(self):
        state = TempState(real_caller=True).__enter__()
        self.addCleanup(state.__exit__, None, None, None)
        env = mock.patch.dict(os.environ, {"DEVKIT_SESSION": "", "CODEX_THREAD_ID": "",
                                          "DEVKIT_FROM_AGENT": "child", "DEVKIT_SPEAKER": ""})
        env.start()
        self.addCleanup(env.stop)
        ancestry = mock.patch.object(pg, "_ancestor_pids", return_value=[789, 456, 1])
        ancestry.start()
        self.addCleanup(ancestry.stop)
        self.text = "Policy tests passed: browser bearer rejection and session expiry."
        self.args = ["crew", "ask", self.text, "--to", "Morpheus"]
        argv = mock.patch.object(sys, "argv", ["dev", *self.args])
        argv.start()
        self.addCleanup(argv.stop)

    def root(self, sid, age, lane=False, agent="codex"):
        record = {"agent": agent, "session": sid, "pid": 456, "started": time.time() - age,
                  "cwd": "/app/scratch/" + sid, "top": ""}
        pg.session_path(agent, sid).write_text(json.dumps(record))
        pg.RUNS.mkdir(exist_ok=True)
        pg.run_path(agent, sid).write_text(json.dumps({
            "agent": agent, "session": sid, "state": "running", "project": "acme/" + sid,
            "agents": {"child": {"persona": "neo", "state": "running"}} if lane else {},
            "messages": [], "plan": [],
        }))

    def call(self, sid, agent="codex", lane="child", cmd=None):
        pg.call_opened(agent, {"session_id": sid, "agent_id": lane, "tool_use_id": "exec-message",
                               "tool_input": {"cmd": cmd or "dev " + shlex.join(self.args)}})

    def test_redacted_child_message_reaches_its_parent_not_newest_shared_process_root(self):
        # The real command's ordinary prose "bearer rejection" is redacted, so its arguments no longer match.
        self.root("parent", 600, lane=True)
        self.root("unrelated", 60)
        self.call("parent")
        self.assertFalse(pg._ledger_holds("parent", self.args))
        pg.crew_ask(self.text, to="Morpheus", from_agent="child")
        self.assertEqual([m["text"] for m in pg.load_run("codex", "parent")["messages"]], [self.text])
        self.assertEqual(pg.load_run("codex", "unrelated")["messages"], [])

    def test_background_child_uses_its_registered_parent_after_the_call_closes(self):
        self.root("parent", 600, lane=True)
        self.root("unrelated", 60)
        self.call("parent")
        pg.call_closed({"session_id": "parent", "tool_use_id": "exec-message"})
        self.assertEqual(pg.resolve_session(argv=self.args), ("codex", "parent", "ancestry"))
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "unrelated"}), self.assertRaises(ValueError):
            pg.resolve_session(argv=self.args)

        # The same provider-isolated contract leaves Claude's process/compaction lookup untouched.
        for sid in ("parent", "unrelated"):
            pg.session_path("codex", sid).unlink()
        self.root("older", 600, lane=True, agent="claude")
        self.root("newer", 60, agent="claude")
        self.assertEqual(pg.resolve_session(argv=self.args), ("claude", "newer", "ancestry"))
        with mock.patch.dict(os.environ, {"DEVKIT_SESSION": "claude:older"}):
            self.assertEqual(pg.resolve_session(argv=self.args), ("claude", "older", "ancestry"))

    def test_unknown_child_cannot_write_into_any_shared_process_root(self):
        self.root("parent", 600)
        self.root("unrelated", 60)
        with self.assertRaises(ValueError):
            pg.crew_ask(self.text, from_agent="child")
        for sid in ("parent", "unrelated"):
            self.assertEqual(pg.load_run("codex", sid)["messages"], [])

    def test_duplicate_child_ownership_cannot_choose_the_newest_parent(self):
        self.root("parent", 600, lane=True)
        self.root("unrelated", 60, lane=True)
        with self.assertRaises(ValueError):
            pg.crew_ask(self.text, from_agent="child")
        for sid in ("parent", "unrelated"):
            self.assertEqual(pg.load_run("codex", sid)["messages"], [])

    def test_open_child_call_identifies_parent_before_a_run_registers_the_lane(self):
        self.root("parent", 600)
        self.root("unrelated", 60)
        self.call("parent")
        self.assertEqual(pg.resolve_session(argv=self.args), ("codex", "parent", "ancestry"))

    def main_roots(self):
        os.environ["DEVKIT_FROM_AGENT"] = ""
        os.environ["DEVKIT_SPEAKER"] = "main"
        self.root("parent", 600)
        self.root("unrelated", 60)

    def test_unstamped_redacted_main_message_refuses_without_writing_to_another_root(self):
        self.main_roots()
        self.call("parent", lane="")
        with self.assertRaises(ValueError):
            pg.crew_ask(self.text)
        for sid in ("parent", "unrelated"):
            self.assertEqual(pg.load_run("codex", sid)["messages"], [])

    def test_identical_main_commands_cannot_choose_a_root_by_cwd_or_recency(self):
        self.main_roots()
        args = ["crew", "ask", "Tests passed", "--to", "Morpheus"]
        for sid in ("parent", "unrelated"):
            self.call(sid, lane="", cmd="dev " + shlex.join(args))
        with self.assertRaises(ValueError):
            pg.resolve_session(cwd="/app/scratch/unrelated", argv=args)

    def test_text_mentioning_a_dev_command_is_not_a_main_call_identity(self):
        self.main_roots()
        args = ["crew", "ask", "Tests passed"]
        self.call("unrelated", lane="", cmd="echo " + shlex.quote("dev " + shlex.join(args)))
        with self.assertRaises(ValueError):
            pg.resolve_session(argv=args)
        self.call("unrelated", lane="", cmd="sh -c " + shlex.quote("dev " + shlex.join(args)))
        self.assertEqual(pg.resolve_session(argv=args), ("codex", "unrelated", "ancestry"))

    def test_explicit_codex_root_wins_over_another_threads_matching_command(self):
        self.main_roots()
        args = ["crew", "ask", "Tests passed"]
        self.call("unrelated", lane="", cmd="dev " + shlex.join(args))
        with mock.patch.dict(os.environ, {"DEVKIT_SESSION": "codex:parent"}):
            self.assertEqual(pg.resolve_session(argv=args), ("codex", "parent", "ancestry"))

        # Claude keeps its existing compaction precedence: the live matching call beats an older environment.
        for sid in ("parent", "unrelated"):
            pg.session_path("codex", sid).unlink()
        self.root("older", 600, agent="claude")
        self.root("newer", 60, agent="claude")
        self.call("newer", agent="claude", lane="", cmd="dev " + shlex.join(args))
        with mock.patch.dict(os.environ, {"DEVKIT_SESSION": "claude:older"}):
            self.assertEqual(pg.resolve_session(argv=args), ("claude", "newer", "ancestry"))

    def test_background_nested_main_call_keeps_its_explicit_root_after_ledger_closes(self):
        self.main_roots()
        self.call("parent", lane="", cmd="sh -c " + shlex.quote("dev " + shlex.join(self.args)))
        pg.call_closed({"session_id": "parent", "tool_use_id": "exec-message"})
        # Another root has a current matching command, which must not override the originating hook's stamp.
        args = ["progress", "note", "Background complete"]
        self.call("unrelated", lane="", cmd="dev " + shlex.join(args))
        with mock.patch.dict(os.environ, {"DEVKIT_SESSION": "codex:parent"}):
            self.assertEqual(pg.resolve_session(argv=args), ("codex", "parent", "ancestry"))

    def test_root_stamp_outside_the_current_codex_process_does_not_select_a_neighbor(self):
        self.main_roots()
        with mock.patch.dict(os.environ, {"DEVKIT_SESSION": "codex:elsewhere"}), self.assertRaises(ValueError):
            pg.resolve_session(argv=self.args)
        with mock.patch.dict(os.environ, {"DEVKIT_SESSION": "codex:parent", "CODEX_THREAD_ID": "unrelated"}), \
                self.assertRaises(ValueError):
            pg.resolve_session(argv=self.args)

    def test_explicit_root_conflicting_with_child_ownership_refuses(self):
        self.root("parent", 600, lane=True)
        self.root("unrelated", 60)
        with mock.patch.dict(os.environ, {"DEVKIT_SESSION": "codex:unrelated"}), self.assertRaises(ValueError):
            pg.crew_ask(self.text, from_agent="child")
        self.assertEqual(pg.load_run("codex", "unrelated")["messages"], [])

    def stamped_environment(self, command):
        # Execute the rewritten shell grammar with a harmless dev stand-in, never the installed CLI.
        folder = pg.RUNS.parent / "bin"
        folder.mkdir(exist_ok=True)
        dev = folder / "dev"
        dev.write_text('#!/bin/sh\nprintf "%s\\n" "$DEVKIT_SESSION" "$DEVKIT_FROM_AGENT" "$DEVKIT_SPEAKER"\n')
        dev.chmod(0o755)
        env = {**os.environ, "PATH": str(folder) + os.pathsep + os.environ.get("PATH", ""),
               "DEVKIT_SESSION": "", "DEVKIT_FROM_AGENT": "", "DEVKIT_SPEAKER": ""}
        result = subprocess.run(["/bin/sh", "-c", command], env=env, capture_output=True, text=True,
                                check=True, timeout=5)
        root, lane, speaker = result.stdout.splitlines()
        return {"DEVKIT_SESSION": root, "DEVKIT_FROM_AGENT": lane, "DEVKIT_SPEAKER": speaker}

    def test_codex_hook_root_stamp_survives_main_nested_and_background_commands(self):
        self.main_roots()
        simple = "dev " + shlex.join(self.args)
        for command in (simple, "sh -c " + shlex.quote(simple) + " | cat", "dev watch --wait &"):
            with self.subTest(command=command):
                data = {"session_id": "parent", "tool_name": "Bash", "cwd": str(pg.RUNS.parent),
                        "tool_input": {"command": command}}
                routed = hooks._speaker_route(data, "codex")
                self.assertIsNotNone(routed)
                env = self.stamped_environment(routed["command"])
                self.assertEqual(env["DEVKIT_SESSION"], "codex:parent")
                with mock.patch.dict(os.environ, env):
                    self.assertEqual(pg.resolve_session(argv=self.args), ("codex", "parent", "ancestry"))
                # Claude retains its established stamp grammar and never receives a Codex root variable.
                legacy = hooks._speaker_route(data, "claude")["command"]
                self.assertNotIn("DEVKIT_SESSION=", legacy)
                self.assertEqual(self.stamped_environment(legacy)["DEVKIT_SPEAKER"], "main")
        for sid in ("", "parent; echo injected", "parent\nexport OTHER=value"):
            with self.subTest(invalid_session=sid):
                self.assertIsNone(hooks._speaker_route({**data, "session_id": sid}, "codex"))

    def test_codex_child_hook_root_stamp_agrees_with_lane_for_simple_and_compound_calls(self):
        self.root("parent", 600, lane=True)
        self.root("unrelated", 60)
        simple = "dev " + shlex.join(self.args)
        for command in (simple, "cd " + shlex.quote(str(pg.RUNS.parent)) + " && " + simple):
            with self.subTest(command=command), mock.patch.object(hooks, "_lane_verdict", return_value=None):
                data = {"session_id": "parent", "agent_id": "child", "tool_name": "Bash",
                        "cwd": str(pg.RUNS.parent), "tool_input": {"command": command}}
                routed = hooks._route_subagent_progress(data, "codex")
                self.assertIsNotNone(routed)
                env = self.stamped_environment(routed["command"])
                self.assertEqual((env["DEVKIT_SESSION"], env["DEVKIT_FROM_AGENT"]), ("codex:parent", "child"))
                with mock.patch.dict(os.environ, env):
                    self.assertEqual(pg.resolve_session(argv=self.args), ("codex", "parent", "ancestry"))
                legacy = hooks._route_subagent_progress(data, "claude")["command"]
                self.assertNotIn("DEVKIT_SESSION=", legacy)
                self.assertEqual(self.stamped_environment(legacy)["DEVKIT_FROM_AGENT"], "child")
        with mock.patch.object(hooks, "_lane_verdict", return_value=None):
            self.assertIsNone(hooks._route_subagent_progress({**data, "session_id": "parent; echo injected"}, "codex"))


if __name__ == "__main__":
    unittest.main()
