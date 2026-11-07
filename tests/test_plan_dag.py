# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The plan is a DAG: each step's `after` lists the steps it waits for, and every transition that starts, owns or
closes a step obeys it (start, add, assign, begin, the main lane's advance, a subagent's start, reopen, skip, the
launch queue). Run: python3 -m unittest discover -s tests -p test_plan_dag.py"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import audit, dispatch, progress as pg  # noqa: E402
from test_progress import boundary  # noqa: E402


class PlanDagTests(unittest.TestCase):
    # two hours ago: devkit's sweep for lost subagents compares lanes with the real clock
    T0 = int(time.time()) // 60 * 60 - 7200

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (pg.RUNS, pg.TIMING, pg.PENDING, pg.SESSIONS, pg._publish, audit.AUDIT)
        pg.RUNS, pg.TIMING, pg.SESSIONS = root / "runs", root / "timing", root / "sessions"
        pg.PENDING = pg.TIMING / "pending"
        pg._publish = lambda: None
        audit.AUDIT = root / "audit"
        self.env = mock.patch.dict(os.environ, {"DEVKIT_SESSION": "claude:dag", "DEVKIT_NO_NOTIFY": "1"})
        self.env.start()
        for p in boundary(root):  # Morpheus's calls, devkit's default configuration
            p.start()
            self.addCleanup(p.stop)
        pg.SESSIONS.mkdir(parents=True)
        (pg.SESSIONS / "claude-dag.json").write_text(json.dumps({"agent": "claude", "session": "dag",
                                                                  "started": self.T0, "project": "acme/api"}))

    def tearDown(self):
        self.env.stop()
        pg.RUNS, pg.TIMING, pg.PENDING, pg.SESSIONS, pg._publish, audit.AUDIT = self.saved
        self.tmp.cleanup()

    def m(self, minutes):
        return self.T0 + minutes * 60

    def steps(self):
        return {s["name"]: s for s in pg.load_run("claude", "dag")["plan"]}

    def states(self):
        return {n: (s["state"], s.get("owner") or "main") for n, s in self.steps().items()}

    def launch(self, aid, kind, label, at):
        # Claude Code's sequence: the Agent call (PreToolUse), the agent's start, the call's result naming the agent
        pg.note_agent_launch("claude", {"session_id": "dag", "tool_use_id": f"toolu_{aid}",
                                        "tool_input": {"description": label, "subagent_type": kind}}, now=at)
        pg.on_subagent("claude", {"session_id": "dag", "agent_id": aid, "agent_type": kind}, True, now=at)
        pg.launch_result("claude", {"session_id": "dag", "tool_use_id": f"toolu_{aid}",
                                    "tool_response": {"agentId": aid}}, now=at)

    def stop(self, aid, kind, at):  # with its report's Check ✓: the step ends done (F6)
        pg.on_subagent("claude", {"session_id": "dag", "agent_id": aid, "agent_type": kind,
                                  "last_assistant_message": "**[Firefly> DevKit · Crew member]** api · `main` · step\n- **Done:** the step\n- **Check:** ✓ its tests pass\n- **Next:** Morpheus"}, False, now=at)

    # ------------------------------------------------------------------ entering the plan: start and add

    def test_start_refuses_a_cycle_and_names_it(self):
        with self.assertRaises(pg.StepError) as e:
            pg.start("Work", plan=["api after ui", "ui after docs", "docs after api"], now=self.m(0))
        self.assertIn("cycle", str(e.exception))
        self.assertIn("“api” waits for “ui”, “ui” waits for “docs”, “docs” waits for “api”", str(e.exception))
        self.assertIsNone(pg.load_run("claude", "dag"))  # nothing was started
        with self.assertRaises(pg.StepError) as e:
            pg.start("Work", plan=["design", "api after api"], now=self.m(0))
        self.assertIn("“api” waits for “api”", str(e.exception))

    def test_adding_steps_that_form_a_cycle_is_refused(self):
        pg.start("Work", plan=["design", "api after design"], now=self.m(0))
        with self.assertRaises(pg.StepError) as e:
            pg.add_step("docs after review; review after docs", now=self.m(1))
        self.assertIn("“docs” waits for “review”, “review” waits for “docs”", str(e.exception))
        self.assertEqual(list(self.steps()), ["design", "api"])  # the plan is unchanged

    def test_an_unknown_dependency_is_refused_at_start_and_at_add(self):
        with self.assertRaises(pg.StepError) as e:
            pg.start("Work", plan=["design", "api after desing"], now=self.m(0))
        self.assertIn("“api” waits for “desing”, which is not a step of the plan", str(e.exception))
        self.assertIsNone(pg.load_run("claude", "dag"))
        pg.start("Work", plan=["design", "api after design"], now=self.m(0))
        with self.assertRaises(pg.StepError) as e:
            pg.add_step("docs=20m after reveiw", now=self.m(1))
        self.assertIn("“docs” waits for “reveiw”, which is not a step of the plan", str(e.exception))
        self.assertEqual(list(self.steps()), ["design", "api"])

    def test_start_runs_the_first_step_whose_dependencies_are_done(self):
        pg.start("Work", plan=["docs=20m after api", "api=1h", "ui=30m"], now=self.m(0))
        self.assertEqual(self.states(), {"docs": ("pending", "main"), "api": ("running", "main"),
                                         "ui": ("pending", "main")})

    # ------------------------------------------------------------------ the main lane

    def test_the_main_lane_moves_on_to_a_ready_step_not_to_one_that_waits(self):
        pg.start("Work", plan=["prep=10m", "dashboard ui after dashboard server", "dashboard server", "docs"],
                 now=self.m(0))
        pg.step("prep done", check="checklist ticked", now=self.m(10))
        self.assertEqual(self.steps()["dashboard ui"]["state"], "pending")
        self.assertEqual(self.steps()["dashboard server"]["state"], "running")
        pg.step("dashboard server done", check="12 tests pass", now=self.m(40))
        self.assertEqual(self.steps()["dashboard ui"]["state"], "running")

    def test_handing_a_step_to_a_member_does_not_start_its_dependents_on_the_main_lane(self):
        # the bug that started this: the main lane ran "dashboard ui" while "dashboard server" was running
        pg.start("Dashboard", plan=["dashboard server=1h", "dashboard ui=1h after dashboard server", "docs=20m"],
                 now=self.m(0))
        pg.assign("dashboard server", "Tank", now=self.m(1))
        self.assertEqual(self.states(), {"dashboard server": ("pending", "main"), "dashboard ui": ("pending", "main"),
                                         "docs": ("running", "main")})
        self.launch("t1", "devkit:tank", "Tank: dashboard server", self.m(2))
        self.assertEqual(self.states()["dashboard server"], ("running", "t1"))
        pg.step("docs written", check="dev docs check clean", now=self.m(20))
        self.assertEqual(self.states()["dashboard ui"], ("pending", "main"))  # nothing on the main lane yet
        self.stop("t1", "devkit:tank", self.m(60))
        self.assertEqual(self.states()["dashboard ui"], ("running", "main"))

    # ------------------------------------------------------------------ begin, assign, a subagent's start

    def test_begin_refuses_a_step_whose_dependency_is_open(self):
        pg.start("Work", plan=["design=30m", "api=1h after design", "docs=20m"], now=self.m(0))
        with self.assertRaises(pg.StepError) as e:
            pg.begin("api", now=self.m(1))
        self.assertIn("“api” waits for design", str(e.exception))
        self.assertEqual(self.states()["api"], ("pending", "main"))
        pg.begin("docs", now=self.m(1))  # no dependency: parallel work is fine
        self.assertEqual([n for n, (st, _) in self.states().items() if st == "running"], ["design", "docs"])

    def test_a_member_starting_early_does_not_take_a_step_that_waits(self):
        pg.start("Feature", plan=["design=30m", "api=1h after design"], now=self.m(0))
        pg.assign("api", "Tank", now=self.m(0))
        self.launch("t1", "devkit:tank", "Tank: api", self.m(1))
        self.assertEqual(self.states()["api"], ("pending", "main"))
        self.assertEqual(pg.load_run("claude", "dag")["agents"]["t1"]["state"], "running")
        told = " ".join(x["text"] for x in pg.crew_inbox())
        self.assertIn("“api” waits for design", told)
        pg.step("design done", check="ADR-12 written", now=self.m(30))
        self.assertEqual(self.states()["api"], ("pending", "main"))  # Tank is busy: Morpheus links it
        pg.assign("api", "Tank", now=self.m(31))
        self.assertEqual(self.states()["api"], ("running", "t1"))

    def test_a_delegated_step_joins_a_working_member_only_once_its_dependency_is_done(self):
        pg.start("Feature", plan=["design=30m", "api=1h"], now=self.m(0))
        self.launch("s1", "devkit:seraph", "Seraph: review", self.m(1))
        r = dispatch.delegate("review the design", to="Seraph", after="design", now=self.m(2))
        self.assertIsNone(r["lane"])  # design is open: the step waits
        pg.step("design done", check="ADR-12 written", now=self.m(30))
        r = dispatch.delegate("review the api", to="Seraph", after="design", now=self.m(31))
        self.assertEqual(r["lane"], "s1")
        self.assertEqual(self.states()["review the api"], ("running", "s1"))

    # ------------------------------------------------------------------ skip and reopen

    def test_a_skipped_dependency_counts_as_done(self):
        pg.start("Dashboard", plan=["dashboard server=1h", "dashboard ui=1h after dashboard server", "docs=20m"],
                 now=self.m(0))
        pg.assign("dashboard server", "Tank", now=self.m(1))
        self.assertEqual(self.states()["dashboard ui"], ("pending", "main"))  # waits for the server
        pg.reopen("dashboard server", "the current server stays; no rewrite", skip=True, now=self.m(2))
        self.launch("t1", "devkit:tank", "Tank: dashboard server", self.m(3))
        self.assertEqual(self.states()["dashboard server"], ("skipped", "main"))  # a late launch does not revive it
        pg.begin("dashboard ui", now=self.m(4))
        self.assertEqual(self.states()["dashboard ui"], ("running", "main"))
        self.assertNotIn("dashboard server", [s["name"] for s in dispatch.ready(pg.load_run("claude", "dag"))["launch"]])

    def test_reopening_a_dependency_flags_its_finished_dependents_and_they_stay_done(self):
        pg.start("Feature", plan=["design=30m", "api=1h after design", "ui=1h after design", "docs=20m after api"],
                 now=self.m(0))
        pg.assign("ui", "Trinity", now=self.m(0))
        pg.step("design done", check="ADR-12 written", now=self.m(30))
        self.launch("t1", "devkit:trinity", "Trinity: ui", self.m(31))
        pg.step("api done", check="20 tests pass", now=self.m(60))
        run = pg.reopen("design", "the schema changed", now=self.m(61))
        s = self.steps()
        self.assertEqual((s["design"]["state"], s["api"]["state"], s["api"].get("recheck")),
                         ("running", "done", ["design"]))
        self.assertEqual((s["ui"]["state"], s["ui"]["owner"], s["ui"].get("recheck")), ("running", "t1", ["design"]))
        self.assertEqual(s["docs"]["state"], "pending")
        out = pg.render_update(run, branch="main", now=self.m(62))
        self.assertIn("✓ 2. api · recheck: design reopened", out)
        pg.reopen("api", "rework it for the new schema", now=self.m(63))
        s = self.steps()
        self.assertEqual((s["api"]["state"], s["api"].get("recheck")), ("pending", None))  # waits for design again
        self.assertEqual(s["design"]["state"], "running")

    def test_a_reopened_step_of_a_finished_member_can_be_launched_again(self):
        # left to the finished lane, it would never run again, and everything after it would wait forever
        pg.start("Feature", plan=["api=1h", "docs=20m after api"], now=self.m(0))
        pg.assign("api", "Tank", now=self.m(0))
        self.launch("t1", "devkit:tank", "Tank: api", self.m(1))
        self.stop("t1", "devkit:tank", self.m(40))
        pg.reopen("api", "the review found a missing index", now=self.m(50))
        self.assertEqual(self.states(), {"api": ("pending", "main"), "docs": ("pending", "main")})
        r = dispatch.ready(pg.load_run("claude", "dag"))
        self.assertEqual([s["name"] for s in r["launch"]], ["api"])

    # ------------------------------------------------------------------ order and output

    def test_topological_order_is_stable_and_drives_the_eta(self):
        run = pg.start("Work", plan=["prep=10m", "docs=20m after api", "api=1h"], now=self.m(0))
        self.assertAlmostEqual(pg.eta(run, self.m(0)), 90)  # prep, then api, then docs: no pessimistic guess
        self.assertEqual([s["name"] for s in pg.topo_order(run["plan"])], ["prep", "api", "docs"])
        plan = [{"name": "c", "after": ["a"]}, {"name": "b"}, {"name": "a"}]
        self.assertEqual([s["name"] for s in pg.topo_order(plan)], ["b", "a", "c"])
        ok = [{"name": "design"}, {"name": "api", "after": ["design"]}, {"name": "ui", "after": ["design"]},
              {"name": "docs", "after": ["api", "ui"]}]
        self.assertEqual([s["name"] for s in pg.topo_order(ok)], ["design", "api", "ui", "docs"])  # already valid

    def test_dev_progress_shows_what_a_step_waits_for(self):
        pg.start("Feature", plan=["design=30m", "api=1h after design", "ui=1h after design", "docs=20m after api, ui"],
                 now=self.m(0))
        run = pg.assign("ui", "Trinity", now=self.m(0))
        out = pg.render_update(run, branch="main", now=self.m(1))
        self.assertIn("○ 2. api · waiting for design · ~1h00m", out)
        self.assertIn("○ 3. ui · waiting for design, then Frontend engineer (Trinity) · ~1h00m", out)
        self.assertIn("○ 4. docs · waiting for api, ui · ~20m", out)


if __name__ == "__main__":
    unittest.main()
