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
    """Codex has no PostToolUseFailure event: a failed command reaches devkit only as PostToolUse, whose tool_response
    is the command's output alone. Its exit code is in the rollout: the CommandExecution item whose id is the hook's
    tool_use_id (captured with Codex 0.167.0 on 2026-11-09)."""

    CMD = "python3 -c 'import nosuchmod'"
    OUTPUT = ("Traceback (most recent call last):\n  File \"<string>\", line 1, in <module>\n    import nosuchmod\n"
              "ModuleNotFoundError: No module named 'nosuchmod'\n")

    def setUp(self):
        super().setUp()
        self.bug_state()
        self.rollout = self.root / "rollout.jsonl"
        self._rollout("0.167.0")

    def _rollout(self, version):
        # a rollout's first records as Codex writes them: its session_meta, which names the CLI version, then the turn
        self.rollout.write_text("".join(json.dumps(r) + "\n" for r in (
            {"type": "session_meta", "payload": {"id": "c1", "cli_version": version, "originator": "codex_exec"}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}})))

    def _record(self, n, status="failed", code=1, rollout=None):
        # the rollout's item for the command, as Codex 0.167.0 wrote it
        item = {"type": "CommandExecution", "id": f"exec-5f125236-dd03-45f9-8fd3-db673fc2b9b{n}", "process_id": "59976",
                "command": ["/bin/zsh", "-lc", self.CMD], "cwd": "file:///repo",
                "parsed_cmd": [{"type": "unknown", "cmd": self.CMD}], "source": "unified_exec_startup",
                "status": status, "aggregated_output": self.OUTPUT, "exit_code": code,
                "duration": {"secs": 0, "nanos": 3333}}
        with (rollout or self.rollout).open("a") as fh:
            fh.write(json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "t1",
                                                                 "item": item}}) + "\n")

    def _post(self, n, output=None, **member):
        # the captured PostToolUse payload of that command; `member`: a crew member's fields (agent_id, agent_type,
        # its own transcript_path) beside the main session's session_id
        payload = dict({"cwd": str(self.root), "hook_event_name": "PostToolUse", "model": "gpt-6.1-sol",
                        "permission_mode": "bypassPermissions", "session_id": "c1",
                        "tool_input": {"command": self.CMD}, "tool_name": "Bash",
                        "tool_response": self.OUTPUT if output is None else output,
                        "tool_use_id": f"exec-5f125236-dd03-45f9-8fd3-db673fc2b9b{n}",
                        "transcript_path": str(self.rollout), "turn_id": "t1"}, **member)
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
            hooks.run("post-tool", "codex")
        return out.getvalue()

    def _ledger_ok(self):
        f = bugs.BUGS / "ledger" / "c1.jsonl"
        ledger = [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []
        return [x["ok"] for x in ledger if x.get("tool") == "Bash"]

    def test_the_rollout_record_decides_a_codex_commands_outcome_never_its_output(self):
        self._record(1)
        self._post(1)  # failed; its output is a plain traceback
        self._record(2, status="completed", code=0)
        self._post(2, output="Exit code: 1\nTraceback (most recent call last):\nValueError: printed by the test\n")
        self.assertEqual(self._ledger_ok(), [False, True])

    def test_the_same_codex_failure_twice_stops_the_patch_loop(self):
        self._record(1)
        self._post(1)
        self._record(2)
        out = self._post(2)
        self.assertIn(hooks.ROOT_CAUSE_STOP, out)
        # Codex's PostToolUse output schema accepts only its own event name
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["hookEventName"], "PostToolUse")

    def test_a_command_whose_record_is_not_written_yet_is_settled_at_the_next_call(self):
        self._post(1)  # its item is not on disk yet: unknown, never a success
        self.assertEqual(self._ledger_ok(), [])
        self._record(1)
        self._record(2)
        out = self._post(2)
        self.assertEqual(self._ledger_ok(), [False, False])
        self.assertIn(hooks.ROOT_CAUSE_STOP, out)  # the second identical failure, counted with the settled first

    def _stop(self):
        payload = {"cwd": str(self.root), "hook_event_name": "Stop", "model": "gpt-6.1-sol",
                   "permission_mode": "bypassPermissions", "session_id": "c1", "stop_hook_active": False,
                   "last_assistant_message": BLOCK, "transcript_path": str(self.rollout), "turn_id": "t1"}
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
            hooks.run("stop", "codex")
        return out.getvalue()

    def test_a_command_whose_record_comes_late_is_settled_at_stop(self):
        self._record(1)
        self._post(1)  # the first failure, known at once
        self._post(2)  # the same failure again, its record not on disk yet
        self._record(2)
        self.assertNotIn(hooks.ROOT_CAUSE_STOP, self._stop())  # Stop writes the ledger line; it cannot tell the agent
        self.assertEqual(self._ledger_ok(), [False, False])
        self._record(3)
        # the next identical failure counts the streak from the ledger, and the paragraph reaches the agent now
        self.assertIn(hooks.ROOT_CAUSE_STOP, self._post(3))

    def test_a_members_hook_never_settles_the_main_sessions_command(self):
        # a Codex member's calls carry the main session's session_id beside its own agent_id and rollout (captured):
        # the main session's late record is not settled in the member's hook, so its paragraph never goes there
        scout_rollout = self.root / "scout.jsonl"
        scout = {"agent_id": "01a1c178-64c7-7ac1-81c6-ec8b852e1772", "agent_type": "scout", "model": "gpt-6-luna",
                 "transcript_path": str(scout_rollout)}
        self._record(1)
        self._post(1)  # the main session's first failure
        self._post(2)  # the same failure again, its record not on disk yet
        self._record(2)
        self._record(3, status="completed", code=0, rollout=scout_rollout)
        self.assertNotIn(hooks.ROOT_CAUSE_STOP, self._post(3, **scout))
        self.assertEqual(self._ledger_ok(), [False, True])  # the member's own line; the main session's still waits
        self._record(4, status="completed", code=0)
        self.assertIn(hooks.ROOT_CAUSE_STOP, self._post(4))  # the main session's next call settles it and is told
        self.assertEqual(self._ledger_ok(), [False, True, False, True])

    def test_a_rollout_without_command_records_gets_each_line_at_once(self):
        # a Codex older than 0.167 writes no CommandExecution items: waiting for one would lose every ledger line
        self._rollout("0.160.0")
        self._post(1)
        self.assertEqual(self._ledger_ok(), [True])  # a PostToolUse is a success, as before Codex wrote the items
        self._record(2)  # once the tail shows the items, a missing one is waited for whatever the version says
        self._post(2)
        self._post(3)
        self.assertEqual(self._ledger_ok(), [True, False])

    def test_a_settle_reads_the_rollout_once_whatever_the_calls_it_waits_for(self):
        for n in (1, 2, 3):
            self._post(n)  # three calls whose items are not written yet
        for n in (1, 2, 3, 4):
            self._record(n)
        with mock.patch.object(hooks, "codex_rollout", wraps=hooks.codex_rollout) as read:
            self._post(4)
        self.assertEqual(read.call_count, 1)  # the three settled calls and the new one share one read of the tail
        self.assertEqual(self._ledger_ok(), [False, False, False, False])

    def test_a_waiting_call_keeps_neither_its_output_nor_a_secret_in_its_command(self):
        # the pending record keeps what settling needs (the error key, devkit's marker lines, the lessons the output
        # recalled), so the paragraph still names the lesson; the output and a token in the command stay out of it
        hooks._session_file("codex", "c1").write_text(json.dumps({"project": "acme/api"}))
        lid = self.lesson("Imported a module the environment lacks", errors=["No module named 'nosuchmod'"])
        token = "ghp_" + "A1b2C3d4E5f6" * 3  # the shape of a GitHub token, built here so no literal looks like one
        self.CMD = f"GH_TOKEN={token} python3 -c 'import nosuchmod'"
        self._record(1)
        self._post(1)
        self._post(2)  # the same failure again, its record not written yet
        kept = hooks._codex_pending("c1").read_text()
        self.assertNotIn("ModuleNotFoundError", kept)
        self.assertNotIn("A1b2C3d4", kept)
        self._record(2)
        self._record(3, status="completed", code=0)
        ctx = json.loads(self._post(3))["hookSpecificOutput"]["additionalContext"]
        self.assertIn(hooks.ROOT_CAUSE_STOP, ctx)
        self.assertIn(f"- Lesson {lid} (recorded by", ctx.split(hooks.ROOT_CAUSE_STOP, 1)[1])

    def test_a_command_never_recorded_leaves_no_ledger_line_and_is_logged(self):
        self._post(1)
        self._record(2, status="completed", code=0)
        with mock.patch.object(hooks, "CODEX_SETTLE_S", 0):  # the first call's wait is over
            self._post(2)
        self.assertEqual(self._ledger_ok(), [True])  # the second call only
        log = (self.root / "logs" / "hooks.log").read_text()
        self.assertIn("exec-5f125236-dd03-45f9-8fd3-db673fc2b9b1", log)


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



class HookFailOpenTests(BugState, HookCase):
    def setUp(self):
        super().setUp()
        self.bug_state()

    def test_a_hook_run_with_no_stdin_fails_open(self):
        # a harness that closes the hook's stdin (sys.stdin is None): the hook logs the error and returns; it never
        # crashes the agent's session, and the caller's variables are left as they were
        before = dict(os.environ)
        with mock.patch("sys.stdin", None):
            try:
                code = hooks.run("post-tool", "codex")
            except Exception as e:
                self.fail(f"the hook raised {e!r}")
        self.assertEqual(code, 0)
        self.assertIn("post-tool codex error", (self.root / "logs" / "hooks.log").read_text())
        self.assertEqual(dict(os.environ), before)

if __name__ == "__main__":
    unittest.main()
