# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The crew coordination scenarios of tests/test_coord_scenarios.py on Codex's host path: members spawned with
spawn_agent and bound at SubagentStart by their task_name, resumed by a new SubagentStart of the same thread, each
member command a shell call through the hooks, and stops with the member's own rollout. Same outcomes as on Claude
Code; only how a member hears devkit differs (its next PostToolUse). Real `dev` processes and hooks.
Run: python3 -m unittest tests.test_codex_coord_parity"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_codex_launch import TANK  # noqa: E402
from test_coordination_codex import REPORT, CodexCoordination  # noqa: E402

TANK2 = "01a1c190-64c7-7ac1-81c6-000000000012"
TANK_REPORT = REPORT.format(role="Backend engineer (Tank)", task="{task}")


class CodexParityTests(CodexCoordination):
    def test_a_codex_members_record_with_no_step_running_warns_and_closes_nothing(self):
        s = self.run_lead("api=30m [backend]; docs=15m")
        self.spawn(s, TANK, "tank", "look around")
        r = self.member(s, TANK, "tank", "progress", "step", "read the code", "--check", "✓ nothing to run")
        self.assertEqual(r["rc"], 0, r["err"])
        self.assertNotIn("✓", r["out"].split("·")[0])
        self.assertIn("closes no step", r["out"])

    def test_a_codex_queue_bound_to_a_lane_survives_its_end_and_its_resume_takes_it(self):
        s = self.run_lead("api=30m [backend]; export=30m [backend]", ("api", "Tank", "api/**"))
        self.spawn(s, TANK, "tank", "api")
        label = s.run_doc()["agents"][TANK]["label"]
        s.ok("progress", "assign", "export", "--to", label, "--scope", "export/**")
        self.member(s, TANK, "tank", "progress", "step", "api committed", "--step", "api", "--check", "✓ 12 pass")
        self.assertEqual(self.held(s, "export"), (TANK, "running"))

    def test_a_codex_answer_while_the_lane_works_on_puts_the_blocked_step_first(self):
        s = self.run_lead("api=30m [backend]; export=30m [backend]", ("api", "Tank", "api/**"))
        s.ok("crew", "mode", "guided")
        self.spawn(s, TANK, "tank", "api")
        self.member(s, TANK, "tank", "crew", "ask", "--decision", "CSV or JSON?", "--options", "A: csv | B: json",
                    "--recommend", "A")
        s.ok("progress", "assign", "export", "--to", "Tank", "--scope", "export/**")
        self.assertEqual((self.held(s, "api"), self.held(s, "export")), ((TANK, "blocked"), (TANK, "running")))
        d = next(x["id"] for x in s.run_doc()["decisions"] if x.get("state") == "pending")
        s.ok("crew", "decide", d, "A: csv")
        self.assertEqual(self.held(s, "api"), ("main", "pending"))
        self.member(s, TANK, "tank", "progress", "step", "export committed", "--step", "export", "--check", "✓ 3")
        self.assertEqual(self.held(s, "api"), (TANK, "running"))

    def test_a_codex_task_name_naming_a_done_step_binds_none_quietly(self):
        s = self.run_lead("api=30m [backend]; export=30m [backend]", ("api", "Tank", "api/**"))
        self.spawn(s, TANK, "tank", "api")
        self.member(s, TANK, "tank", "progress", "step", "api committed", "--step", "api", "--check", "✓ 12 pass")
        self.stop(s, TANK, "tank", TANK_REPORT.format(task="api"))
        s.ok("progress", "assign", "export", "--to", "Tank")
        self.spawn(s, TANK2, "tank", "api")  # a follow-up of the finished api
        self.assertEqual(self.held(s, "export"), ("main", "pending"))
        told = [m["text"] for m in s.run_doc().get("messages") or [] if m.get("to") == "main"]
        self.assertFalse([t for t in told if "names none" in t], told)


if __name__ == "__main__":
    unittest.main()
