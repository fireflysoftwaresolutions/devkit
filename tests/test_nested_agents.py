# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Nested agents (F3 of the agent and session lifecycle spec, gaps G3 and G17, contract C3): a subagent can launch
# agents of its own (verified at F0: PreToolUse of Agent inside a subagent carries the parent's agent_id, fixture
# PreToolUse.Agent.in-subagent.json). Such a helper records its parent and depth, gets its parent's tool level and
# scope and never more, takes no plan step, and counts against the parallel limit like any launch; the limit drops
# to 1 when the recorded 5-hour usage is above 90 %. Run: python3 -m unittest tests.test_nested_agents
import json
import sys
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_lane_binding import LaneCase, fixture  # noqa: E402
from devkit import hooks, dispatch, progress as pg  # noqa: E402


class NestedCase(LaneCase):
    # a crew member launched by Morpheus and bound to its launch, working on its step
    def member(self, aid, tid, label, kind="general-purpose"):
        self.launch(tid, label, kind=kind)
        self.start(aid, kind=kind)
        self.result(tid, aid, label, kind=kind)

    # the member launches a helper; the helper starts, and its meta file names its launch
    def helper(self, parent, aid, tid, label="helper: look around", kind="general-purpose"):
        self.launch(tid, label, kind=kind, parent=parent)
        self.start(aid, kind=kind)
        self.meta(aid, tid, label, kind=kind)
        self.first_tool(aid, kind=kind)  # its first call binds it (spec D2)

    def edit(self, aid, rel):
        return self.first_tool(aid, tool="Edit", file_path=rel)

    def spawn(self, parent=None, tid="toolu_next"):
        p = fixture("PreToolUse.Agent.in-subagent" if parent else "PreToolUse.Agent", **self.common(tool_use_id=tid))
        if parent:
            p["agent_id"] = parent
        return hooks._crew_limits("claude", p) or ("allow", "")

    def quota(self, used, resets_in=3600):
        q = Path(self.state.tmp.name) / "quota"
        q.mkdir(exist_ok=True)
        (q / "claude.json").write_text(json.dumps({"ts": int(time.time()), "five_hour": {
            "used_percentage": used, "resets_at": int(time.time() + resets_in)}}))


class LaunchRecordTests(NestedCase):
    def test_a_nested_launch_records_its_parent_and_depth_and_its_lane_keeps_them(self):
        pg.start("Work", plan=["parser"], now=self.m(0))
        pg.assign("parser", "Tank", scope="lib/parser/**", now=self.m(0))
        self.member("a_tank", "toolu_tank", "Tank: parser")
        self.helper("a_tank", "a_help", "toolu_help")
        launches = self.session()["launches"]
        self.assertEqual((launches["toolu_tank"]["depth"], launches["toolu_help"]["depth"]), (1, 2))
        lane = self.lane("a_help")
        self.assertEqual((lane.get("binding"), lane.get("parent"), lane.get("depth")), ("exact", "a_tank", 2))
        self.assertEqual(pg.lane_chain(self.run_(), "a_help"), ["a_help", "a_tank"])

    def test_a_helper_takes_none_of_its_members_steps(self):
        # a helper labeled after its member ("Tank: …") must not pick up the member's next step and its scope
        pg.start("Work", plan=["parser", "parser docs"], now=self.m(0))
        pg.assign("parser", "Tank", scope="lib/parser/**", now=self.m(0))
        pg.assign("parser docs", "Tank", scope="docs/**", now=self.m(0))
        self.member("a_tank", "toolu_tank", "Tank: parser")
        self.helper("a_tank", "a_help", "toolu_help", label="Tank: parser docs")
        self.assertEqual((self.step("parser docs")["owner"], self.step("parser docs")["state"]), ("main", "pending"))
        self.assertIn('"permissionDecision": "deny"', self.edit("a_help", "docs/parser.md"))


class InheritedLimitTests(NestedCase):
    def test_a_helper_of_a_scoped_lane_cannot_edit_outside_the_parents_scope(self):
        # spec F3 acceptance: today the helper's lane has no scope, so any file is allowed
        pg.start("Work", plan=["parser"], now=self.m(0))
        pg.assign("parser", "Tank", scope="lib/parser/**", now=self.m(0))
        self.member("a_tank", "toolu_tank", "Tank: parser")
        self.helper("a_tank", "a_help", "toolu_help")
        out = self.edit("a_help", "lib/ui/app.ts")
        self.assertIn('"permissionDecision": "deny"', out)
        self.assertIn("lib/parser/**", out)
        self.assertNotIn('"permissionDecision": "deny"', self.edit("a_help", "lib/parser/core.py"))

    def test_a_helper_two_levels_down_keeps_the_top_members_scope(self):
        pg.start("Work", plan=["parser"], now=self.m(0))
        pg.assign("parser", "Tank", scope="lib/parser/**", now=self.m(0))
        self.member("a_tank", "toolu_tank", "Tank: parser")
        self.helper("a_tank", "a_help", "toolu_help")
        with mock.patch.dict(pg.core.CFG, {"progress": {"max_depth": 3}}):
            self.helper("a_help", "a_deep", "toolu_deep")
        self.assertEqual(pg.lane_scope(self.run_(), "a_deep"), [["lib/parser/**"]])
        self.assertIn('"permissionDecision": "deny"', self.edit("a_deep", "lib/ui/app.ts"))

    def test_a_helper_of_a_review_member_cannot_edit(self):
        # G3's second scenario: Seraph is review-only; a general-purpose helper it launches has every tool
        pg.start("Work", plan=["review"], now=self.m(0))
        pg.assign("review", "Seraph", now=self.m(0))
        self.member("a_seraph", "toolu_seraph", "Seraph: review")
        self.helper("a_seraph", "a_help", "toolu_help")
        self.assertEqual(pg.lane_tools(self.run_(), "a_help"), "review")
        out = self.edit("a_help", "lib/parser/core.py")
        self.assertIn('"permissionDecision": "deny"', out)
        self.assertIn("review-only", out)

    def test_a_helper_of_a_lane_in_conflict_cannot_edit(self):
        pg.start("Work", plan=["parser"], now=self.m(0))
        pg.assign("parser", "Neo", scope="lib/parser/**", now=self.m(0))
        self.member("a_neo", "toolu_neo", "Neo: parser")
        self.start("a_forged")  # its meta file names Neo's launch: it binds as a conflict
        self.meta("a_forged", "toolu_neo", "Neo: parser")
        self.first_tool("a_forged")
        self.helper("a_forged", "a_help", "toolu_help")
        self.assertIn('"permissionDecision": "deny"', self.edit("a_help", "lib/parser/core.py"))


class LimitAtEveryDepthTests(NestedCase):
    def setUp(self):
        super().setUp()
        pg.start("Work", plan=["a", "b", "c", "d", "e"], now=self.m(0))

    def test_a_nested_launch_at_the_limit_is_denied(self):
        # spec F3 acceptance: today the limit skips launches made inside a subagent (hooks._crew_limits)
        for i, who in enumerate(("Neo", "Trinity", "Tank", "Dozer")):
            self.member(f"a{i}", f"toolu_{i}", f"{who}: part {i}")
        verdict = self.spawn(parent="a2")
        self.assertEqual(verdict[0], "deny")
        self.assertIn("the limit is 4", verdict[1])

    def test_a_launch_deeper_than_max_depth_is_denied(self):
        self.member("a_tank", "toolu_tank", "Tank: part")
        self.helper("a_tank", "a_help", "toolu_help")
        verdict = self.spawn(parent="a_help")
        self.assertEqual(verdict[0], "deny")
        self.assertIn("max_depth", verdict[1])
        self.assertEqual(self.spawn(parent="a_tank", tid="toolu_second")[0], "allow")  # depth 2 is allowed

    def test_short_quota_lowers_the_limit_to_one(self):
        # spec G17: above 90 % of the 5-hour window, one crew member at a time
        self.member("a_tank", "toolu_tank", "Tank: part")
        self.quota(95)
        verdict = self.spawn()
        self.assertEqual(verdict[0], "deny")
        self.assertIn("the limit is 1", verdict[1])
        self.assertIn("5-hour", verdict[1])
        self.assertEqual(dispatch.ready(self.run_())["free"], 0)
        self.quota(95, resets_in=-60)  # an expired reading says nothing about the current window
        self.assertEqual(self.spawn()[0], "allow")
        self.quota(85)
        self.assertEqual(self.spawn()[0], "allow")


if __name__ == "__main__":
    unittest.main()
