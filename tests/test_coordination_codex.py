# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The crew coordination faults of devkit 26.11.25 (2026-11-19), on Codex's host path: a Codex tech lead's thread,
its members spawned with spawn_agent and bound at SubagentStart by their task_name (Codex sends no PreToolUse for the
spawn), each member command a shell call through PreToolUse and PostToolUse that its own rollout also records as a
CommandExecution item, and notices that reach a member at its next PostToolUse. Real `dev` processes and hooks, as
tests/test_codex_launch.py drives them. The Claude Code variants are in tests/test_coordination_lanes.py and
tests/test_coordination_identity.py. Run: python3 -m unittest tests.test_coordination_codex"""
import json
import shlex
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_codex_launch import LEAD, TANK, CodexCrew, _line, _now  # noqa: E402

SERAPH = "01a1c190-64c7-7ac1-81c6-000000000007"
REPORT = ("**[Firefly> DevKit · {role}]** api · `feat/invoice-export` · {task}\n- **Done:** {task}\n"
          "- **Check:** ✓ 12 tests pass\n- **Next:** the tech lead merges it.\n\nNeeds from the tech lead: nothing")


class CodexCoordination(CodexCrew):
    def run_lead(self, plan: str, *assign):
        """A Codex tech lead's run for `plan`, each (step, member, scope) assigned by role."""
        self.config(max_parallel=3, reserve=0)
        s = self.codex(LEAD, self.api)
        s.ok("progress", "start", "Invoice export", "--plan", plan)
        for step, name, scope in assign:
            s.ok("progress", "assign", step, "--to", name, "--scope", scope)
        return s

    def member(self, s, aid: str, member: str, *args) -> dict:
        """A member's `dev` command: its shell call through the hooks, and the CommandExecution item its own rollout
        records for it."""
        r = s.dev(*args, member=aid, member_type=member)
        cmd = " ".join(["dev"] + [shlex.quote(str(a)) for a in args])
        item = {"type": "CommandExecution", "id": r["call"]["tool_use_id"], "command": ["/bin/zsh", "-lc", cmd],
                "status": "completed" if r["rc"] == 0 else "failed", "exit_code": r["rc"],
                "aggregated_output": (r["out"] + r["err"])[-2000:]}
        with self.rollout(aid).open("a") as fh:
            fh.write(_line({"timestamp": _now(), "type": "event_msg", "payload": {
                "type": "item_completed", "turn_id": "01a1c178-28b7-7fa1-a67c-2bd8b7433d66", "item": item}}))
        return r

    def stop(self, s, aid: str, member: str, report: str, again: bool = False) -> dict:
        """SubagentStop as Codex 0.167.0 sends it: the member's own rollout beside the parent's."""
        return s.hook("subagent-stop", s.payload(
            "SubagentStop", agent_id=aid, agent_type=member, model="gpt-6-luna", stop_hook_active=again,
            agent_transcript_path=str(self.rollout(aid)), last_assistant_message=report))

    def held(self, s, name: str) -> tuple:
        st = self.step(s, name)
        return st.get("owner"), st.get("state")


class CodexLaneTests(CodexCoordination):
    def test_a_codex_members_checked_record_without_step_closes_its_lanes_only_running_step(self):
        s = self.run_lead("api=30m [backend]; ui=30m [frontend]", ("api", "Tank", "api/**"))
        self.spawn(s, TANK, "tank", "api")
        self.assertEqual(self.held(s, "api"), (TANK, "running"))
        r = self.member(s, TANK, "tank", "progress", "step", "the endpoint streams CSV", "--check", "✓ 12 tests pass")
        self.assertEqual(r["rc"], 0, r["err"])
        self.assertIn("✓", r["out"])
        self.assertEqual(self.held(s, "api"), (TANK, "done"), r["out"])

    def test_a_codex_lane_whose_step_closes_takes_the_first_of_two_steps_assigned_to_its_member(self):
        s = self.run_lead("api=30m [backend]; export=30m [backend]; import=30m [backend]",
                          ("api", "Tank", "api/**"), ("export", "Tank", "export/**"), ("import", "Tank", "import/**"))
        self.spawn(s, TANK, "tank", "api")
        self.assertEqual(self.held(s, "api"), (TANK, "running"))
        r = self.member(s, TANK, "tank", "progress", "step", "api committed", "--step", "api", "--check", "✓ 12 pass")
        self.assertEqual(r["rc"], 0, r["err"])
        self.assertEqual((self.held(s, "export"), self.held(s, "import")), ((TANK, "running"), ("main", "pending")))

    def test_a_blocked_codex_step_frees_the_lane_and_the_member_hears_of_its_next_step(self):
        s = self.run_lead("api=30m [backend]; export=30m [backend]", ("api", "Tank", "api/**"))
        self.spawn(s, TANK, "tank", "api")
        s.ok("progress", "block", "the API contract waits for the developer", "--step", "api")
        s.ok("progress", "assign", "export", "--to", "Tank", "--scope", "export/**")
        self.assertEqual(self.held(s, "export"), (TANK, "running"))
        heard = self.member_hears(s, TANK, "tank")  # the member's next PostToolUse
        self.assertNotIn("joins your lane once", heard)

    def test_a_resumed_codex_lane_takes_the_one_step_assigned_to_its_member_by_role(self):
        s = self.run_lead("api=30m [backend]; export=30m [backend]", ("api", "Tank", "api/**"))
        self.spawn(s, TANK, "tank", "api")
        self.member(s, TANK, "tank", "progress", "step", "api committed", "--step", "api", "--check", "✓ 12 pass")
        self.stop(s, TANK, "tank", REPORT.format(role="Backend engineer (Tank)", task="api"))
        s.ok("progress", "assign", "export", "--to", "Tank", "--scope", "export/**")
        self.subagent_start(s, TANK, "tank")  # resumed: Codex starts the same thread again
        self.assertEqual(self.held(s, "export"), (TANK, "running"))
        self.assertEqual(s.run_doc()["agents"][TANK].get("scope"), ["export/**"])

    def test_a_resumed_codex_reviewers_clean_report_approves_after_an_earlier_send_back(self):
        s = self.run_lead("api=30m [backend]; review=20m [review] after api", ("api", "Tank", "api/**"))
        self.spawn(s, TANK, "tank", "api")
        self.member(s, TANK, "tank", "progress", "step", "api committed", "--step", "api", "--check", "✓ 12 pass")
        self.stop(s, TANK, "tank", REPORT.format(role="Backend engineer (Tank)", task="api"))
        s.ok("progress", "assign", "review", "--to", "Seraph")
        self.spawn(s, SERAPH, "seraph", "review")
        report = REPORT.format(role="Code reviewer (Seraph)", task="review")
        self.assertEqual(self.stop(s, SERAPH, "seraph", report).get("decision"), "block")  # no step recorded
        self.stop(s, SERAPH, "seraph", report, again=True)  # let through
        self.assertEqual(self.held(s, "review")[1], "unverified")
        s.ok("progress", "reopen", "review", "--why", "the reviewer gives the verdict again")
        self.subagent_start(s, SERAPH, "seraph")
        self.assertEqual(self.held(s, "review"), (SERAPH, "running"))
        r = self.member(s, SERAPH, "seraph", "progress", "step", "review done", "--step", "review", "--check",
                        "✓ read the diff, 12 tests pass", "--approves", "api")
        self.assertEqual(r["rc"], 0, r["err"])
        self.stop(s, SERAPH, "seraph", report)
        why = [h["why"] for h in s.run_doc()["history"] if h["step"] == "review"][-1]
        self.assertEqual(self.held(s, "review")[1], "done", why)


class CodexCallerTests(CodexCoordination):
    # On Codex the tech lead's own commands are "unverified", which main-only commands allow, so a member's open
    # wait or quoting question never takes them (checked on 2026-11-19); the ack below still fails
    def test_the_codex_tech_lead_acknowledges_with_a_digest_read_in_the_same_command(self):
        s = self.run_lead("api=30m [backend]", ("api", "Tank", "api/**"))
        self.spawn(s, TANK, "tank", "api")
        self.member(s, TANK, "tank", "crew", "ask", "Report: " + "x" * 400)
        ref = next(m["id"] for m in s.run_doc()["messages"] if m.get("to") == "main")
        r = s.sh(f'dev crew ack {ref} --sha256 "$(dev crew read {ref} --json | python3 -I -c \'import json,sys; '
                 f'print(json.load(sys.stdin)["digest"])\')" --generation 1')
        self.assertEqual(r["rc"], 0, r["out"] + r["err"])
        self.assertIn("main", next(m for m in s.run_doc()["messages"] if m["id"] == ref).get("receipts") or {})


if __name__ == "__main__":
    unittest.main()
