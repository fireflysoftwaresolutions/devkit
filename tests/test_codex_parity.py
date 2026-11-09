# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Reproductions of the Codex parity failures found live with Codex CLI 0.167.0 on 2026-11-09.

Each test failed before the fix of the bug it names. The record shapes are copied from a real Codex 0.167.0 rollout
(~/.codex/sessions/2026/11/09/rollout-...-01a1c151-....jsonl) and from the hook schemas the Codex binary embeds
(stop.command.input requires last_assistant_message). The apply_patch guard case lives in test_guard_codex_patch.py.

Run: python3 -m unittest tests.test_codex_parity -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
sys.path.insert(0, str(REPO / "tests"))
from devkit import audit, bugs, canary, hooks, scope  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_bugs_detect import BugState  # noqa: E402
from test_language_neutral_hooks import HookCase  # noqa: E402
from test_progress import TempState  # noqa: E402

BLOCK = ("**[Firefly> DevKit]** proj · `main` · Run requested steps\n\n- **Done:** ran the steps.\n"
         "- **Check:** ✓ git log shows the seed commit\n- **Next:** nothing.")


def _codex_rollout(cmd: str, final: str) -> str:
    """A one-turn Codex 0.167.0 rollout: the prompt, one code-mode `exec` call running `cmd`, the final message."""
    recs = [
        {"type": "session_meta", "payload": {"cli_version": "0.167.0", "originator": "codex_exec"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "run the steps"}},
        {"type": "response_item", "payload": {
            "type": "custom_tool_call", "status": "completed", "call_id": "call_1", "name": "exec",
            "input": "text(await tools.exec_command({cmd:" + json.dumps(cmd) + ",max_output_tokens:1000}));\n"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "call_1",
                                              "output": [{"type": "input_text", "text": "Script completed\n"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "status": "completed",
                                              "content": [{"type": "output_text", "text": final}]}},
        {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": final}},
    ]
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in recs))
    return path


class CodexTranscriptTests(unittest.TestCase):
    def setUp(self):
        self.path = _codex_rollout("git commit -m 'docs: add notes'", BLOCK)
        self.addCleanup(os.unlink, self.path)

    def test_stop_counts_a_codex_turn_that_opens_with_the_status_block_as_development(self):
        # Live: `dev agents test codex` -> "Stop hook + instructions (canary): no canary record";
        # hooks.log "stop codex: not development work (no signal); devkit stays out" although the reply opened
        # with the block. The Stop payload carries the reply in last_assistant_message.
        data = {"hook_event_name": "Stop", "transcript_path": self.path, "last_assistant_message": BLOCK,
                "stop_hook_active": False}
        self.assertTrue(scope.decide(data, {"top": "/repo"}))

    def test_turn_actions_sees_the_shell_command_of_a_code_mode_exec_call(self):
        # Codex 0.167.0 (code_mode_host on by default) runs shell commands inside its `exec` JavaScript tool.
        self.assertEqual(scope.turn_actions(self.path)["commands"], ["git commit -m 'docs: add notes'"])

    def test_last_assistant_text_reads_a_codex_rollout(self):
        # SubagentStop and the shown-decisions record fall back to the transcript when the payload has no text.
        self.assertEqual(canary.last_assistant_text(self.path), BLOCK)


class CodexStopTests(unittest.TestCase):
    # Live (probe A): a Codex turn that committed ended without what the rules ask and was not sent back. Here the
    # Stop hook runs its own classifier (scope.decide is not replaced), on a 0.167 rollout and payload
    def setUp(self):
        self.state = TempState()
        self.state.__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.path = _codex_rollout("git commit -m 'docs: add notes'", BLOCK)
        self.addCleanup(os.unlink, self.path)

    def test_a_codex_turn_without_the_progress_line_is_sent_back(self):
        (pg.SESSIONS / "codex-c1.json").write_text(json.dumps({
            "agent": "codex", "session": "c1", "started": time.time() - 600, "turn_started": time.time() - 60}))
        with mock.patch.dict(os.environ, {"DEVKIT_SESSION": "codex:c1"}):
            pg.start("Hotfix", plan=["plan=10m", "api=20m"])
        data = {"session_id": "c1", "turn_id": "t1", "hook_event_name": "Stop", "cwd": self.state.tmp.name,
                "model": "gpt-6", "permission_mode": "bypassPermissions", "transcript_path": self.path,
                "last_assistant_message": BLOCK, "stop_hook_active": False}
        out = io.StringIO()
        with mock.patch.object(hooks, "_session_file", lambda a, sid: pg.SESSIONS / f"{a}-{sid}.json"), \
                mock.patch.object(hooks, "STATE", Path(self.state.tmp.name)), \
                mock.patch.object(hooks, "_continuity", lambda *a, **k: None), \
                mock.patch.object(hooks, "_spawn_sync", lambda args: None), \
                mock.patch.object(canary, "LOG", Path(self.state.tmp.name) / "canary.jsonl"), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(data))), contextlib.redirect_stdout(out):
            hooks.stop("codex")
        sent = json.loads(out.getvalue() or "{}")
        self.assertEqual(sent.get("decision"), "block")
        self.assertIn("- **Progress:** 0 of 2 steps · now 1. plan · ", sent.get("reason", ""))


class CodexFailedShellTests(BugState, HookCase):
    """Codex has no PostToolUseFailure event: a failed shell call reaches devkit only as PostToolUse."""

    def setUp(self):
        super().setUp()
        self.bug_state()

    FAILED = {"exit_code": 1, "stdout": "", "stderr": "ModuleNotFoundError: No module named 'nosuchmodule_xyz'"}

    def _post(self, n, response=None):
        # tool_response shape: not captured (the guard refuses DEVKIT_HOOK_CAPTURE for agents); the exit code and
        # output fields follow Codex's own command item (exit_code, aggregated_output)
        payload = {"session_id": "c1", "turn_id": "t1", "hook_event_name": "PostToolUse", "tool_name": "Bash",
                   "tool_use_id": f"call_{n}", "cwd": str(self.root), "model": "gpt-6", "permission_mode": "bypass",
                   "transcript_path": None, "tool_input": {"command": "python3 -c 'import nosuchmodule_xyz'"},
                   "tool_response": self.FAILED if response is None else response}
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
            hooks.run("post-tool", "codex")
        return out.getvalue()

    def _ledger_ok(self):
        ledger = [json.loads(x) for x in (bugs.BUGS / "ledger" / "c1.jsonl").read_text().splitlines()]
        return [x["ok"] for x in ledger if x.get("tool") == "Bash"]

    def test_a_failed_codex_shell_call_is_a_failure_in_the_ledger(self):
        self._post(1)
        self.assertEqual(self._ledger_ok(), [False])

    def test_the_same_codex_failure_twice_stops_the_patch_loop(self):
        self._post(1)
        out = self._post(2)
        self.assertIn(hooks.ROOT_CAUSE_STOP, out)
        # Codex's PostToolUse output schema accepts only its own event name
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["hookEventName"], "PostToolUse")

    def test_a_successful_codex_shell_call_stays_a_success(self):
        self._post(1, {"exit_code": 0, "stdout": "ok\n", "stderr": ""})
        self.assertEqual(self._ledger_ok(), [True])

    def test_the_exit_code_is_read_from_each_known_response_shape(self):
        # exec_command's own result (the rollout's tool output, as an object or its JSON text), the command item and
        # app-server names, and the plain-text headers older Codex shells print
        for resp in ({"exit_code": 2}, {"exitCode": 2}, {"returncode": 2},
                     json.dumps({"chunk_id": "1a305f", "wall_time_seconds": 0.1, "exit_code": 2, "output": "x"}),
                     "Exit code: 2\nWall time: 0.5 seconds\nOutput:\nboom\n",
                     "Chunk ID: 1a305f\nWall time: 0.5 seconds\nProcess exited with code 2\nOutput:\nboom\n"):
            with self.subTest(resp=resp):
                self.assertEqual(hooks.tool_exit_code(resp), 2)
        for resp in ({"stdout": "Exit code: 2"}, "Output:\nExit code: 2\n", {"exit_code": True}, None):
            with self.subTest(resp=resp):
                self.assertIsNone(hooks.tool_exit_code(resp))

    def test_an_unknown_response_shape_counts_as_success_and_is_logged_once(self):
        self._post(1, {"stdout": "ok\n"})
        self._post(2, {"stdout": "ok again\n"})
        self.assertEqual(self._ledger_ok(), [True, True])
        log = (self.root / "logs" / "hooks.log").read_text()
        self.assertEqual(log.count("no exit code in its tool_response"), 1, log)


class CodexHookIdentityTests(HookCase):
    """A Codex started from a Claude Code terminal (or a crew member's shell) inherits that session's variables."""

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"DEVKIT_AGENT": "claude", "CLAUDECODE": "1", "DEVKIT_FROM_AGENT": "a_tank"})
        env.start()
        self.addCleanup(env.stop)
        self.launcher = dict(os.environ)  # DEVKIT_SESSION=claude:s1 comes from HookCase

    def _deny(self, **member):
        # PreToolUse as Codex 0.167 sends it (captured): a member's call carries agent_id and agent_type beside the
        # main session's session_id
        payload = dict({"session_id": "c1", "turn_id": "t1", "hook_event_name": "PreToolUse", "tool_name": "Bash",
                        "tool_use_id": "exec-5f125236-dd03-45f9-8fd3-db673fc2b9b0", "cwd": str(self.root),
                        "model": "gpt-6.1-sol", "permission_mode": "bypassPermissions", "transcript_path": None,
                        "tool_input": {"command": "git checkout -b codex/parity-probe"}}, **member)
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
            hooks.run("guard", "codex")
        self.assertIn('"deny"', out.getvalue())
        recs = [json.loads(ln) for f in sorted(audit.AUDIT.glob("*.jsonl")) for ln in f.read_text().splitlines()]
        return [r for r in recs if r["action"] == "guard.deny"]

    def test_a_codex_hooks_records_carry_codex_and_its_own_session(self):
        denials = self._deny()
        self.assertEqual([(r["actor"]["agent"], r["actor"]["session"]) for r in denials], [("codex", "c1")])
        self.assertNotIn("crew", denials[0])  # never the launcher's crew lane
        self.assertEqual(dict(os.environ), self.launcher)  # the caller's own variables are left as they were

    def test_a_codex_members_records_carry_the_main_session_and_its_own_lane(self):
        aid = "01a1c178-64c7-7ac1-81c6-ec8b852e1772"  # the captured scout's agent_id
        denials = self._deny(agent_id=aid, agent_type="scout")
        self.assertEqual([(r["actor"]["agent"], r["actor"]["session"]) for r in denials], [("codex", "c1")])
        self.assertEqual(denials[0]["crew"]["lane"], aid)  # its own lane, never the launcher's a_tank


if __name__ == "__main__":
    unittest.main()
