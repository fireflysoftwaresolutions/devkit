# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The dispatcher's launch offer must survive Codex's actual admission check."""
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import core, dispatch, progress as pg  # noqa: E402
from test_progress import TempState  # noqa: E402
from test_codex_launch import CodexCrew, LEAD, TANK  # noqa: E402


def setup_run(agent, busy, candidate, latest, quota=None):
    now = time.time()
    os.environ["DEVKIT_SESSION"] = f"{agent}:s1"
    session = pg.session_path(agent, "s1")
    session.write_text(json.dumps({"agent": agent, "session": "s1", "started": now - 300,
                                   "project": "acme/api"}))
    names = [f"work{i}" for i in range(len(busy))] + ["candidate"]
    pg.start("Admission contract", plan="; ".join(name + "=10m" for name in names), now=now - 120)
    for name, member in zip(names, [*busy, candidate]):
        pg.assign(name, member, why="contract fixture")
    for i, member in enumerate(busy):
        call = {"session_id": "s1", "tool_use_id": f"call{i}",
                "tool_input": {"agent_type": member, "description": f"{member}: work{i}"}}
        pg.note_agent_launch(agent, call, now=now - 30)
        pg.on_subagent(agent, {"session_id": "s1", "agent_id": f"member{i}", "agent_type": member}, True,
                       now=now - 30)
        pg.launch_result(agent, dict(call, tool_response={"agentId": f"member{i}"}), now=now - 30)
    rec = json.loads(session.read_text())
    rec["turn_started"] = now - (180 if latest else 60)
    session.write_text(json.dumps(rec))
    if quota is not None:
        pg.QUOTA.mkdir(parents=True, exist_ok=True)
        (pg.QUOTA / f"{agent}.json").write_text(json.dumps({"ts": now, "five_hour": {
            "used_percentage": quota, "resets_at": now + 3600}}))
    return pg.load_run(agent, "s1"), now


class AdmissionContractTests(unittest.TestCase):
    def test_dispatch_and_admission_agree_at_reserve_weight_and_quota_boundaries(self):
        cases = [
            (("tank", "architect", "roland"), "keymaker", True, None, 1, 4, True),
            (("tank", "architect", "roland"), "keymaker", False, None, 1, 4, False),
            (("tank", "architect", "roland"), "keymaker", False, None, 0, 4, True),
            (("tank", "architect", "seraph"), "keymaker", False, None, 1, 4, False),
            (("tank", "architect", "seraph"), "keymaker", True, None, 1, 4, True),
            (("tank", "architect", "roland"), "seraph", True, None, 1, 4, True),
            (("tank", "architect"), "keymaker", True, None, 0, 2, False),
            (("tank",), "keymaker", True, 85, 1, 4, True),
            (("tank",), "keymaker", False, 85, 1, 4, False),
            (("tank",), "keymaker", True, 90, 1, 4, False),
            (("tank",), "scout", True, 90, 1, 4, True),
        ]
        for agent in ("codex", "claude"):
            for busy, candidate, latest, quota, reserve, maximum, expected in cases:
                with self.subTest(agent=agent, busy=busy, candidate=candidate, latest=latest, quota=quota,
                                  reserve=reserve, maximum=maximum), TempState(), \
                        mock.patch.dict(core.CFG, {"progress": {"max_parallel": maximum, "reserve": reserve}}):
                    run, now = setup_run(agent, busy, candidate, latest, quota)
                    ready = dispatch.ready(run, now)
                    offered = "candidate" in [step["name"] for step in ready["launch"]]
                    verdict = pg.launch_verdict(agent, {"session_id": "s1", "tool_input": {
                        "agent_type": candidate, "description": f"{candidate}: candidate"}}, run, now)
                    self.assertEqual(offered, expected, ready)
                    self.assertEqual(verdict is None, offered, verdict)

    def test_a_new_quota_limit_after_the_offer_requires_readmission(self):
        with TempState(), mock.patch.dict(core.CFG, {"progress": {"max_parallel": 4, "reserve": 1}}):
            run, now = setup_run("codex", ("tank", "architect", "roland"), "keymaker", True)
            self.assertEqual([step["name"] for step in dispatch.ready(run, now)["launch"]], ["candidate"])
            pg.QUOTA.mkdir(parents=True, exist_ok=True)
            (pg.QUOTA / "codex.json").write_text(json.dumps({"ts": now, "five_hour": {
                "used_percentage": 90, "resets_at": now + 3600}}))
            call = {"session_id": "s1", "tool_input": {
                "agent_type": "keymaker", "description": "keymaker: candidate"}}
            self.assertIn("lowered from 4", pg.launch_verdict("codex", call, run, now) or "")
            self.assertEqual(dispatch.ready(run, now)["launch"], [])

    def test_a_helper_inherits_its_reviewers_slot_weight_and_still_obeys_the_limit(self):
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent), TempState(), \
                    mock.patch.dict(core.CFG, {"progress": {"max_parallel": 2, "reserve": 0}}):
                run, now = setup_run(agent, ("seraph", "tank"), "neo", True)
                self.assertEqual(dispatch.ready(run, now)["launch"], [])  # a root builder costs a full slot
                call = {"session_id": "s1", "agent_id": "member0", "tool_use_id": "nested",
                        "tool_input": {"agent_type": "neo", "description": "neo: inspect an edge case"}}
                self.assertIsNone(pg.launch_verdict(agent, call, run, now))
                pg.note_agent_launch(agent, call, now=now)
                pg.on_subagent(agent, {"session_id": "s1", "agent_id": "helper", "agent_type": "neo"}, True,
                               now=now)
                pg.launch_result(agent, dict(call, tool_response={"agentId": "helper"}), now=now)
                self.assertIn("2 of 2 crew slots", pg.launch_verdict(agent, call, pg.load_run(agent, "s1"), now) or "")


class CodexAdmissionHookTests(CodexCrew):
    def test_a_latest_security_step_offered_with_three_builders_is_admitted_by_subagent_start(self):
        self.config(max_parallel=4, reserve=1)
        s = self.codex(LEAD, self.api)
        s.ok("progress", "start", "Admission contract", "--plan",
             "api=10m [backend]; design=10m [architecture]; research=10m [research]; security=10m [security]")
        roles = (("api", "tank"), ("design", "architect"), ("research", "roland"), ("security", "keymaker"))
        for step, member in roles:
            s.ok("progress", "assign", step, "--to", member)
        for i, (step, member) in enumerate(roles[:3]):
            self.spawn(s, TANK[:-1] + str(i + 1), member, step)
        ready = s.ok("crew", "next")
        self.assertIn("3 of 4 slots in use", ready)
        self.assertIn("Ready to launch", ready)
        self.assertIn("Security engineer (The Keymaker): security", ready)
        child = TANK[:-1] + "4"
        result = self.spawn(s, child, "keymaker", "security")
        lane = s.run_doc()["agents"][child]
        self.assertNotIn("over_limit", lane, self.context(result))
        self.assertEqual(self.step(s, "security")["owner"], child)


if __name__ == "__main__":
    unittest.main()
