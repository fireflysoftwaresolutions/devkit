# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Crew coordination scenarios on Claude Code's path, through the progress API and its hooks: what a member's record
says and changes, the steps that wait for a member and the order its lanes take them in, blocked steps and their
answers, and the files a lane may write. The same scenarios on Codex are in tests/test_codex_coord_parity.py.
Run: python3 -m unittest tests.test_coord_scenarios"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import coord, ui  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_progress import TempState, launch, member_call, progress_cli_out  # noqa: E402

REPORT = ("**[Firefly> DevKit · Backend engineer (Tank)]** api · `main` · {task}\n- **Done:** {task}\n"
          "- **Check:** {check}\n- **Next:** the tech lead reviews it")


class Scenario(unittest.TestCase):
    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.t0 = time.time() - 3600

    def m(self, minutes):
        return self.t0 + minutes * 60

    def run_(self):
        return pg.load_run("claude", "s1")

    def held(self, name):
        st = next(s for s in self.run_()["plan"] if s["name"] == name)
        return st.get("owner"), st.get("state")

    def end(self, aid, task, at, member="tank", check="✓ 12 tests pass"):
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": aid, "agent_type": f"devkit:{member}",
                                  "last_assistant_message": REPORT.format(task=task, check=check)}, False, now=at)

    def resume(self, aid, at, member="tank"):
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": aid, "agent_type": f"devkit:{member}"}, True,
                       now=at)

    def cli(self, aid, *args):
        with member_call(aid):
            return progress_cli_out("step", *args)

    def sound(self):
        self.assertEqual(coord.check(self.run_(), time.time(), review=pg.is_review_step), [])


class MemberRecordTests(Scenario):
    def test_member_step_without_step_closes_its_single_running_step(self):
        pg.start("Export", plan=["api=30m", "docs=15m"], now=self.m(0))
        pg.assign("api", "Tank", scope="api/**", why="test", now=self.m(0))
        launch("a_tank", "devkit:tank", "Tank: api", at=self.m(1))
        run = pg.step("api committed", check="✓ 12 tests pass", from_agent="a_tank", now=self.m(10))
        self.assertEqual(run["lane_outcome"]["changed"], [("api", "running", "done")])
        self.assertEqual(self.held("api"), ("a_tank", "done"))
        self.sound()

    def test_member_step_without_any_held_step_prints_warning(self):
        pg.start("Export", plan=["api=30m"], now=self.m(0))
        launch("a_tank", "devkit:tank", "Tank: look around", at=self.m(1))
        rc, out, err = self.cli("a_tank", "read the code", "--check", "✓ nothing to run")
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.startswith(ui.WARN), out)
        self.assertIn("closes no step", out)
        self.assertEqual(self.held("api"), ("main", "running"))

    def test_a_record_for_a_step_already_done_says_so_with_a_warning(self):
        pg.start("Export", plan=["api=30m", "docs=15m"], now=self.m(0))
        pg.assign("api", "Tank", why="test", now=self.m(0))
        launch("a_tank", "devkit:tank", "Tank: api", at=self.m(1))
        pg.step("api committed", which="api", check="✓ 12 tests pass", from_agent="a_tank", now=self.m(10))
        rc, out, err = self.cli("a_tank", "api committed again", "--step", "api", "--check", "✓ 12 tests pass")
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.startswith(ui.WARN), out)
        self.assertIn("is done already", out)

    def test_a_record_naming_two_running_steps_is_refused_without_step(self):
        pg.start("Review", plan=["api=30m", "review=20m [review]"], now=self.m(0))
        pg.assign("api", "Tank", why="test", now=self.m(0))
        launch("a_tank", "devkit:tank", "Tank: api", at=self.m(1))
        run = self.run_()
        run["plan"][1].update(owner="a_tank", state="running", label="Tank")  # two build steps in one lane
        run["plan"][1].pop("activity", None)
        pg.run_path("claude", "s1").write_text(__import__("json").dumps(run))
        with self.assertRaises(pg.StepError) as e:
            pg.step("both done", check="✓ 12 tests pass", from_agent="a_tank", now=self.m(10))
        self.assertIn("say which with --step", str(e.exception))


class QueueTests(Scenario):
    def setUp(self):
        super().setUp()
        pg.start("Export", plan=["api=30m", "export=30m", "import=30m"], now=self.m(0))
        pg.assign("api", "Tank", scope="api/**", why="test", now=self.m(0))
        launch("a_tank", "devkit:tank", "Tank: api", at=self.m(1))

    def test_lane_takes_queued_steps_in_order_after_each_close(self):
        pg.assign("import", "Tank", scope="import/**", why="test", now=self.m(2))
        pg.assign("export", "Tank", scope="export/**", why="test", now=self.m(3))
        pg.step("api", which="api", check="✓ 1", from_agent="a_tank", now=self.m(10))
        self.assertEqual((self.held("import"), self.held("export")), (("a_tank", "running"), ("main", "pending")))
        pg.step("import", which="import", check="✓ 2", from_agent="a_tank", now=self.m(20))
        self.assertEqual(self.held("export"), ("a_tank", "running"))
        self.assertEqual(pg.lane_scope(self.run_(), "a_tank"), [["export/**"]])
        self.sound()

    def test_queue_survives_lane_end_and_resume(self):
        pg.assign("export", "Tank: api", scope="export/**", why="test", now=self.m(2))  # bound to this lane
        pg.assign("import", "Tank", scope="import/**", why="test", now=self.m(3))
        self.end("a_tank", "api", self.m(10))
        self.assertEqual((self.held("api"), pg._queued_lanes(self.run_()["plan"][1])), (("a_tank", "done"),
                                                                                       ["a_tank"]))
        self.resume("a_tank", self.m(12))
        self.assertEqual((self.held("export"), self.held("import")), (("a_tank", "running"), ("main", "pending")))
        self.sound()

    def test_a_new_launch_naming_a_step_bound_to_an_ended_lane_takes_it(self):
        pg.assign("export", "Tank: api", scope="export/**", why="test", now=self.m(2))
        self.end("a_tank", "api", self.m(10))
        launch("a_tank2", "devkit:tank", "Tank: export", at=self.m(11))
        self.assertEqual(self.held("export"), ("a_tank2", "running"))

    def test_a_step_bound_to_a_live_lane_stays_with_it_when_another_launch_names_it(self):
        pg.assign("export", "Tank: api", scope="export/**", why="test", now=self.m(2))
        launch("a_tank2", "devkit:tank", "Tank: export", at=self.m(3))
        self.assertEqual(self.held("export"), ("main", "pending"))
        pg.step("api", which="api", check="✓ 1", from_agent="a_tank", now=self.m(10))
        self.assertEqual(self.held("export"), ("a_tank", "running"))

    def test_a_follow_up_launch_naming_a_done_step_takes_no_step(self):
        self.end("a_tank", "api", self.m(10))
        pg.assign("export", "Tank", why="test", now=self.m(11))
        launch("a_tank2", "devkit:tank", "Tank: api follow-up", at=self.m(12))
        self.assertEqual(self.held("export"), ("main", "pending"))
        self.assertEqual(self.run_()["agents"]["a_tank2"].get("follow_up"), "api")
        self.sound()


class BlockTests(Scenario):
    def setUp(self):
        super().setUp()
        pg.start("Export", plan=["api=30m", "export=30m"], now=self.m(0), mode="guided")
        pg.assign("api", "Tank", scope="api/**", why="test", now=self.m(0))
        launch("a_tank", "devkit:tank", "Tank: api", at=self.m(1))

    def test_blocked_step_frees_lane_scope_and_next_step_joins(self):
        pg.assign("export", "Tank", scope="export/**", why="test", now=self.m(2))
        pg.crew_decision("CSV or JSON?", options="A: csv | B: json", recommend="A", from_agent="a_tank",
                         now=self.m(5))
        self.assertEqual((self.held("api"), self.held("export")), (("a_tank", "blocked"), ("a_tank", "running")))
        self.assertEqual(pg.lane_scope(self.run_(), "a_tank"), [["export/**"]])
        self.sound()

    def test_answer_requeues_blocked_step_first_when_lane_busy(self):
        pg.assign("export", "Tank", scope="export/**", why="test", now=self.m(2))
        d = pg.crew_decision("CSV or JSON?", options="A: csv | B: json", recommend="A", from_agent="a_tank",
                             now=self.m(5))
        pg.crew_decide(d["id"], "A: csv", now=self.m(6))
        self.assertEqual((self.held("api"), pg._queued_lanes(self.run_()["plan"][0])), (("main", "pending"),
                                                                                       ["a_tank"]))
        pg.step("export", which="export", check="✓ 3", from_agent="a_tank", now=self.m(10))
        self.assertEqual(self.held("api"), ("a_tank", "running"))
        self.sound()

    def test_an_answer_after_the_lane_ended_goes_to_the_members_next_lane(self):
        d = pg.crew_decision("CSV or JSON?", options="A: csv | B: json", recommend="A", from_agent="a_tank",
                             now=self.m(5))
        self.end("a_tank", "api", self.m(6), check="– waiting for D1")  # it stops while it waits
        self.assertEqual(self.held("api"), ("a_tank", "blocked"))
        pg.crew_decide(d["id"], "A: csv", now=self.m(7))
        launch("a_tank2", "devkit:tank", "Tank: carry on", at=self.m(8))
        self.assertEqual(self.held("api"), ("a_tank2", "running"))


class ScopeTests(Scenario):
    def test_a_closed_step_gives_its_lane_no_files(self):
        pg.start("Export", plan=["api=30m"], now=self.m(0))
        pg.assign("api", "Tank", scope="api/**", why="test", now=self.m(0))
        launch("a_tank", "devkit:tank", "Tank: api", at=self.m(1))
        pg.step("api", which="api", check="✓ 1", from_agent="a_tank", now=self.m(10))
        layers = pg.lane_scope(self.run_(), "a_tank")
        self.assertEqual((layers, pg.in_scope("api/x.py", layers)), ([[]], False))
        self.assertNotIn("scope", self.run_()["agents"]["a_tank"])

    def test_a_run_without_a_plan_keeps_a_lanes_given_scope(self):
        # a lane recorded with a scope of its own, as older runs and `dev test` in a lane have it
        run = {"goal": "Work", "agents": {"a_kid": {"scope": ["lib/calc.py"], "label": "Kid: calc"}}, "plan": []}
        self.assertEqual(pg.lane_scope(run, "a_kid"), [["lib/calc.py"]])

    def test_file_glob_does_not_overlap_recursive_glob_with_other_file_name(self):
        pg.start("Docs", plan=["guide=30m", "bugs=30m"], now=self.m(0))
        pg.assign("guide", "Kid", scope="docs/crew.md", why="test", now=self.m(0))
        launch("a_kid", "devkit:kid", "Kid: guide", at=self.m(1))
        pg.assign("bugs", "Ghost", scope="docs/**/bug*.md", why="test", now=self.m(2))
        with self.assertRaises(pg.StepError):
            pg.assign("bugs", "Ghost", scope="docs/", why="test", now=self.m(3))  # a real overlap


class VerdictNoticeTests(Scenario):
    def test_a_review_and_a_verdict_the_gates_did_not_pass_are_named_to_the_tech_lead_once(self):
        pg.start("Export", plan=["api=30m", "review=20m [review] after api"], now=self.m(0))
        pg.assign("api", "Tank", why="test", now=self.m(0))
        pg.assign("review", "Seraph", why="test", now=self.m(0))
        launch("a_tank", "devkit:tank", "Tank: api", at=self.m(1))
        pg.step("api", which="api", check="✓ 1", from_agent="a_tank", now=self.m(5))
        launch("a_ser", "devkit:seraph", "Seraph: review", at=self.m(6))
        pg.step("review done", which="review", check="✓ read the diff", from_agent="a_ser", approves="api",
                now=self.m(10))
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a_ser", "agent_type": "devkit:seraph",
                                  "last_assistant_message": REPORT.format(task="review", check="✓ read the diff")},
                       False, now=self.m(11))
        told = [m["text"] for m in self.run_().get("messages") or [] if m.get("to") == "main"
                and "crew gates" in m["text"]]
        self.assertEqual(len(told), 1, told)
        self.assertIn("“review”", told[0])
        self.assertIn("“api”", told[0])


class ReviewVerdictTests(Scenario):
    def test_a_review_closed_with_changes_requested_lets_the_step_after_it_start(self):
        pg.start("Export", plan=["api=30m", "review=20m [review] after api", "fix=20m after review"], now=self.m(0))
        pg.assign("api", "Tank", why="test", now=self.m(0))
        pg.assign("review", "Seraph", why="test", now=self.m(0))
        pg.assign("fix", "Tank", why="test", now=self.m(0))
        launch("a_tank", "devkit:tank", "Tank: api", at=self.m(1))
        pg.step("api", which="api", check="✓ 1", from_agent="a_tank", now=self.m(5))
        launch("a_ser", "devkit:seraph", "Seraph: review", at=self.m(6))
        pg.step("review", which="review", check="✗ changes requested: the CSV header is wrong", from_agent="a_ser",
                approves="api", now=self.m(10))
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a_ser", "agent_type": "devkit:seraph",
                                  "last_assistant_message": REPORT.format(task="review", check="✗ changes requested")},
                       False, now=self.m(11))
        run = self.run_()
        self.assertEqual(self.held("review")[1], "unverified")
        self.assertEqual((pg.deps_open(run, run["plan"][2]), self.held("fix")), ([], ("a_tank", "running")))
        self.assertIn("api", [s["name"] for s in run["plan"] if s["name"] == "api"])
        self.assertNotIn("api", {n for a in run.get("approvals") or [] if a.get("verdict") == "approve"
                                 for n in a.get("steps") or []})


if __name__ == "__main__":
    unittest.main()
