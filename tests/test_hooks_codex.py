# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""A Codex crew member's stop: the same report checks a Claude Code member gets at SubagentStop.

Codex CLI 0.167.0 sends SubagentStop with the member's final message (`last_assistant_message`), its own rollout
(`agent_transcript_path`) and `stop_hook_active`; its output schema (subagent-stop.command.output, embedded in the
binary) accepts `decision: "block"` with a `reason`, and nothing else beside continue, stopReason, suppressOutput and
systemMessage. The member's launch comes from SubagentStart (tests/test_codex_launch.py).

Run: python3 -m unittest tests.test_hooks_codex -v (and with /usr/bin/python3, for Python 3.9)
"""
import json
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_codex_launch import LEAD, TANK, TRINITY, CodexCrew  # noqa: E402

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
    hook-side shell rule reads it as it reads Bash's `command` (guard.tool_commands)."""

    def setUp(self):
        super().setUp()
        f = self.home / ".config" / "devkit" / "config.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"progress": {"max_parallel": 2, "reserve": 0},
                                 "safeguards": {"irreversible_main": "enforce"}}))

    def verdict(self, s, tool: str, inp: dict, **member) -> tuple:
        g = s.hook("guard", s.payload("PreToolUse", tool_name=tool, tool_use_id=f"exec-{time.time_ns()}",
                                      tool_input=inp, **member))
        spec = g.get("hookSpecificOutput") or {}
        return spec.get("permissionDecision"), spec.get("permissionDecisionReason") or ""

    def test_an_exec_command_meets_the_main_sessions_irreversible_gate_like_bash(self):
        s = self.codex(LEAD, self.api)
        bash = self.verdict(s, "Bash", {"command": "dev release v26.11.9"})
        self.assertEqual(bash[0], "deny", bash)
        self.assertEqual(self.verdict(s, "exec_command", {"cmd": "dev release v26.11.9"}), bash)

    def test_the_gate_judges_an_exec_command_in_its_workdir(self):
        s = self.codex(LEAD, self.api)
        self.verdict(s, "exec_command", {"cmd": "git push origin main", "workdir": str(self.kit)})
        gate = [r for r in self.audit("guard.irreversible") if "push" in json.dumps(r)]
        self.assertEqual(len(gate), 1, gate)
        # the action is judged in the repository it runs in; the record keeps the session's project
        self.assertEqual((gate[0]["ref"]["repo"], gate[0]["project"]), ("firefly/kit-consulting", "acme/api"))

    def test_an_exec_command_meets_the_members_lane_verdict_like_bash(self):
        s = self.lead()
        self.spawn(s, TANK, "tank", "api")
        team = 'dev crew set tank summary "Builds APIs" --why "shorter"'
        member = {"agent_id": TANK, "agent_type": "tank"}
        bash = self.verdict(s, "Bash", {"command": team}, **member)
        self.assertEqual(bash[0], "deny", bash)
        self.assertEqual(self.verdict(s, "exec_command", {"cmd": team}, **member), bash)


if __name__ == "__main__":
    unittest.main()
