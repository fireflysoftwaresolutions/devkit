# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""A Codex crew member's stop: the same report checks a Claude Code member gets at SubagentStop.

Codex CLI 0.167.0 sends SubagentStop with the member's final message (`last_assistant_message`), its own rollout
(`agent_transcript_path`) and `stop_hook_active`; its output schema (subagent-stop.command.output, embedded in the
binary) accepts `decision: "block"` with a `reason`, and nothing else beside continue, stopReason, suppressOutput and
systemMessage. The member's launch comes from SubagentStart (tests/test_codex_launch.py).

Run: python3 -m unittest tests.test_hooks_codex -v (and with /usr/bin/python3, for Python 3.9)
"""
import contextlib
import io
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_codex_launch import LEAD, TANK, TRINITY, CodexCrew  # noqa: E402
from test_bugs_detect import BugState  # noqa: E402
from test_lessons_hooks import LessonHookCase  # noqa: E402
from devkit import bugs, hooks  # noqa: E402

SUBAGENT_STOP_KEYS = {"continue", "decision", "reason", "stopReason", "suppressOutput", "systemMessage"}
REPORT = ("**[Firefly> DevKit · Backend engineer (Tank)]** api · `feat/invoice-export` · the API\n"
          "- **Done:** built the endpoint.\n- **Check:** ✓ 12 tests pass\n- **Next:** the tech lead reviews it.\n\n"
          "Needs from the tech lead: nothing")


class CodexMemberStop(CodexCrew):

    def stop(self, s, aid: str, member: str, text: str, again: bool = False) -> dict:
        """SubagentStop as Codex 0.167.0 sends it: the parent's rollout as transcript, the member's own beside it."""
        return s.hook("subagent-stop", s.payload(
            "SubagentStop", agent_id=aid, agent_type=member, model="gpt-6-luna", stop_hook_active=again,
            agent_transcript_path=str(self.rollout(aid)), last_assistant_message=text))

    def test_a_report_without_its_status_block_is_sent_back_once(self):
        s = self.lead()
        self.spawn(s, TANK, "tank", "api")
        out = self.stop(s, TANK, "tank", "Built the endpoint; 12 tests pass.")
        self.assertEqual(set(out) - SUBAGENT_STOP_KEYS, set(), out)
        self.assertEqual(out.get("decision"), "block", out)
        self.assertIn("end your report with your status block first, exactly: **[Firefly> DevKit · Backend engineer "
                      "(Tank)]**", out["reason"])
        again = self.stop(s, TANK, "tank", "Built the endpoint; 12 tests pass.", again=True)
        self.assertNotIn("your status block first, exactly", again.get("reason", ""))  # the crew gates may speak, once
        self.assertEqual(self.stop(s, TANK, "tank", "Built the endpoint; 12 tests pass.", again=True), {})
        self.assertEqual(s.run_doc()["agents"][TANK]["state"], "done")

    def test_a_member_that_never_recorded_its_step_is_sent_back_to_dev_progress_step(self):
        s = self.lead()
        self.spawn(s, TANK, "tank", "api")
        run = self.state / "runs" / f"codex-{s.sid}.json"
        doc = json.loads(run.read_text())  # the member started ten minutes ago and stops without a dev progress call
        doc["agents"][TANK]["started"] = time.time() - 600
        run.write_text(json.dumps(doc))
        out = self.stop(s, TANK, "tank", REPORT)
        self.assertEqual(out.get("decision"), "block", out)
        self.assertIn("record your work through devkit before you stop: `dev progress step", out["reason"])

    def test_an_over_limit_member_owes_only_its_status_block(self):
        s = self.lead(max_parallel=1)
        self.spawn(s, TANK, "tank", "api")
        self.spawn(s, TRINITY, "trinity", "ui")
        report = REPORT.replace("Backend engineer (Tank)", "Frontend engineer (Trinity)")
        self.assertEqual(self.stop(s, TRINITY, "trinity", report), {})
        self.assertEqual(s.run_doc()["agents"][TRINITY]["state"], "done")
        self.assertEqual(self.audit("crew.gate"), [])

    def test_a_member_whose_start_left_no_record_is_checked_at_its_stop(self):
        s = self.lead()
        call = "call_late"
        self.spawn_call(s, "tank", "api", call)
        self.agent_rollout(s, TANK, "tank", "api")
        self.spawn_started(s, TANK, "api", call)  # its SubagentStart never reached devkit (the session was busy)
        out = self.stop(s, TANK, "tank", "Built the endpoint.")
        self.assertEqual(out.get("decision"), "block", out)
        self.assertEqual(self.launch_of(s, TANK)["key"], call)


class CodexShellPayloads(CodexCrew):
    """Codex's exec_command carries its shell command as `cmd` (and the folder it runs in as `workdir`): every
    hook-side shell rule reads it as it reads Bash's `command` (shellparse.tool_commands)."""

    def setUp(self):
        super().setUp()
        f = self.home / ".config" / "devkit" / "config.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"progress": {"max_parallel": 2, "reserve": 0},
                                 "safeguards": {"irreversible_main": "enforce"}}))

    def verdict(self, s, tool: str, inp: dict, **member) -> tuple:
        g = s.hook("pre-tool", s.payload("PreToolUse", tool_name=tool, tool_use_id=f"exec-{time.time_ns()}",
                                         tool_input=inp, **member))
        spec = g.get("hookSpecificOutput") or {}
        return spec.get("permissionDecision"), spec.get("additionalContext") or ""

    def test_an_exec_command_of_the_main_session_gets_what_bash_gets_and_no_decision(self):
        s = self.codex(LEAD, self.api)
        bash = self.verdict(s, "Bash", {"command": "dev release v26.11.9"})
        self.assertEqual(bash[0], None, bash)
        self.assertEqual(self.verdict(s, "exec_command", {"cmd": "dev release v26.11.9"}), bash)

    def test_an_exec_command_of_a_member_gets_what_bash_gets_and_no_decision(self):
        s = self.lead()
        self.spawn(s, TANK, "tank", "api")
        team = 'dev crew set tank summary "Builds APIs" --why "shorter"'
        member = {"agent_id": TANK, "agent_type": "tank"}
        bash = self.verdict(s, "Bash", {"command": team}, **member)
        self.assertEqual(bash[0], None, bash)
        self.assertEqual(self.verdict(s, "exec_command", {"cmd": team}, **member), bash)


class CodexMemberCase(BugState, LessonHookCase):
    # a Codex crew member's shell calls, its rollout and the session's call ledger, as the hooks see them
    CMD = "python3 -c 'import nosuchmod'"
    MEMBER = {"agent_id": "01a1c178-64c7-7ac1-81c6-ec8b852e1772", "agent_type": "scout", "model": "gpt-6-luna"}

    def setUp(self):
        super().setUp()
        self.bug_state()
        self.rollout = self.root / "scout.jsonl"
        self.rollout.write_text("".join(json.dumps(r) + "\n" for r in (
            {"type": "session_meta", "payload": {"id": "c1", "cli_version": "0.167.0", "originator": "codex_exec"}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}})))

    def hook(self, name, payload):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
            hooks.run(name, "codex")
        return out.getvalue()

    def post(self):
        # the member's PostToolUse beside the main session's session_id, its own rollout as transcript
        self.hook("post-tool", dict(self.MEMBER, cwd=str(self.root), hook_event_name="PostToolUse",
                                    session_id="c1", tool_input={"command": self.CMD}, tool_name="Bash",
                                    tool_response="ModuleNotFoundError: No module named 'nosuchmod'\n",
                                    tool_use_id="exec-5f125236-dd03-45f9-8fd3-db673fc2b9b1",
                                    transcript_path=str(self.rollout), turn_id="t1"))

    def record(self):
        item = {"type": "CommandExecution", "id": "exec-5f125236-dd03-45f9-8fd3-db673fc2b9b1",
                "command": ["/bin/zsh", "-lc", self.CMD], "status": "failed", "exit_code": 1,
                "aggregated_output": "ModuleNotFoundError: No module named 'nosuchmod'\n"}
        with self.rollout.open("a") as fh:
            fh.write(json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "t1",
                                                                 "item": item}}) + "\n")

    def ledger(self):
        f = bugs.BUGS / "ledger" / "c1.jsonl"
        lines = [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []
        return [x["ok"] for x in lines if x.get("tool") == "Bash"]

class CodexMemberLateRecordTests(CodexMemberCase):
    # A Codex member's command whose rollout record came after its PostToolUse waited for the member's next call to
    # get its ledger line; Stop settles the main session's, but nothing settled a member's last one at SubagentStop,
    # so it left with no line and its failure never counted
    def test_a_members_last_late_command_gets_its_ledger_line_at_its_stop(self):
        self.post()  # its record is not on disk yet: unknown, never a success
        self.assertEqual(self.ledger(), [])
        self.record()
        self.hook("subagent-stop", dict(self.MEMBER, cwd=str(self.root), hook_event_name="SubagentStop",
                                        session_id="c1", stop_hook_active=False, transcript_path=str(self.rollout),
                                        agent_transcript_path=str(self.rollout), last_assistant_message=REPORT))
        self.assertEqual(self.ledger(), [False])


class CodexItemSupportTests(CodexMemberCase):
    # A rollout's session_meta names the Codex that started it: a session started with 0.166 and resumed with a newer
    # Codex, or a source build (0.0.0), read as one that writes no CommandExecution items, so a command whose item
    # came late counted as a success at once; any item record in the rollout shows the Codex writing it now does
    def meta(self, version, *records):
        self.rollout.write_text("".join(json.dumps(r) + "\n" for r in (
            {"type": "session_meta", "payload": {"id": "c1", "cli_version": version, "originator": "codex_exec"}},
            *records)))

    def test_a_session_resumed_by_a_codex_that_writes_items_waits_for_the_item(self):
        self.meta("0.166.0", {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "t2", "item": {
            "type": "UserMessage", "id": "user-1", "content": [{"type": "text", "text": "go on"}]}}})
        self.post()
        self.assertEqual(self.ledger(), [])  # unknown until its item is written, never a success
        self.record()
        self.post_again()
        self.assertEqual(self.ledger()[:1], [False])

    def test_a_source_build_waits_for_the_item(self):
        self.meta("0.0.0")
        self.post()
        self.assertEqual(self.ledger(), [])

    def test_a_codex_older_than_the_items_counts_each_command_at_once(self):
        # a command's output that quotes an item record is not one: only Codex's own event says it writes items
        self.meta("0.160.0", {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}},
                  {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c0",
                                                        "output": {"type": "item_completed", "text": "grep hit"}}})
        self.post()
        self.assertEqual(self.ledger(), [True])

    def post_again(self):
        self.hook("post-tool", dict(self.MEMBER, cwd=str(self.root), hook_event_name="PostToolUse",
                                    session_id="c1", tool_input={"command": "true"}, tool_name="Bash",
                                    tool_response="", tool_use_id="exec-5f125236-dd03-45f9-8fd3-db673fc2b9b2",
                                    transcript_path=str(self.rollout), turn_id="t1"))


if __name__ == "__main__":
    unittest.main()
