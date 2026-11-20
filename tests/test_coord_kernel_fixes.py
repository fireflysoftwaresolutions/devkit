# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Crew coordination at its edges: a lane that runs no step holds no file, and `dev commit` says so; a launch whose
# description shares a word with a finished step is a follow-up only when it names none of the steps waiting for its
# member; a stop the crew gates let through tells the tech lead once. Progress state in a temporary folder
# (test_progress.TempState), every run checked against the coordination rules (coord.check).
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_coord_kernel_fixes -v
import contextlib
import io
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import coord, gitx  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_approvals_token import StopCase  # noqa: E402
from test_progress import TempState, launch  # noqa: E402


class UnassignedLaneCommitTests(unittest.TestCase):
    def test_a_commit_by_a_lane_that_runs_no_step_says_it_runs_none_and_names_no_empty_scope(self):
        # lane_scope gives a crew lane that runs no step one empty layer: it holds no file
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = gitx._lane_refusal([[]], ["server/api.py"])
        text = err.getvalue()
        self.assertEqual(rc, 2)
        self.assertIn("runs no step", text)
        self.assertIn("server/api.py", text)
        self.assertNotIn("()", text)


class GenericLaunchTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.t0 = time.time() - 3600

    def m(self, k):
        return self.t0 + k * 60

    def run_(self):
        return pg.load_run("claude", "s1")

    def held(self, name):
        s = next(x for x in self.run_()["plan"] if x["name"] == name)
        return s.get("owner"), s.get("state")

    def end(self, aid, kind):
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": aid, "agent_type": kind,
                                  "last_assistant_message": "done"}, False, now=self.m(2))

    def test_a_reviewers_launch_naming_a_finished_review_takes_the_review_waiting_for_it(self):
        pg.start("Release", plan=["review=20m", "review-27=20m"], now=self.m(0))
        pg.assign("review", "Seraph", why="the first review", now=self.m(0), activity="review")
        launch("a_s1", "devkit:seraph", "Seraph: review", at=self.m(1))
        self.end("a_s1", "devkit:seraph")
        pg.reopen("review", "the first review is finished", skip=True, now=self.m(2))
        pg.assign("review-27", "Seraph", why="the release review", now=self.m(3), activity="review")
        launch("a_s2", "devkit:seraph", "Seraph: review the release", at=self.m(4))
        self.assertEqual(self.held("review-27"), ("a_s2", "running"))
        self.assertIsNone(self.run_()["agents"]["a_s2"].get("follow_up"))
        self.assertEqual(coord.check(self.run_(), time.time(), review=pg.is_review_step), [])

    def test_a_launch_naming_a_done_step_best_still_takes_the_waiting_step_it_names(self):
        pg.start("Export", plan=["api=30m", "api docs=15m"], now=self.m(0))
        pg.assign("api", "Tank", why="the api", now=self.m(0))
        launch("a_t1", "devkit:tank", "Tank: api", at=self.m(1))
        pg.step("api done", which="api", check="✓ 3 tests pass", from_agent="a_t1", now=self.m(2))
        self.end("a_t1", "devkit:tank")
        pg.assign("api docs", "Tank", why="the docs", now=self.m(3))
        launch("a_t2", "devkit:tank", "Tank: api release notes", at=self.m(4))
        self.assertEqual(self.held("api docs"), ("a_t2", "running"))
        self.assertIsNone(self.run_()["agents"]["a_t2"].get("follow_up"))
        self.assertEqual(coord.check(self.run_(), time.time(), review=pg.is_review_step), [])


class LetThroughNoticeTests(StopCase):
    # the Code reviewer's stop, sent back once for its missing step record and then let through: the tech lead hears
    # it in one notice, with what the gates still found and the review that counts for nothing
    def test_a_stop_the_gates_let_through_tells_the_tech_lead_once(self):
        self.stop()
        self.stop(stop_hook_active=True)
        told = [x.get("text") or "" for x in self.run_().get("messages") or []
                if x.get("to") == "main" and x.get("from") == "devkit"]
        self.assertEqual(len(told), 1, told)
        self.assertIn("Code reviewer (Seraph) stopped, and the crew gates let it through", told[0])
        self.assertIn("proofs: dev progress step", told[0])
        self.assertIn("the review “review” counts for nothing yet", told[0])


class DeferredStopNoticeTests(unittest.TestCase):
    # a stop that meets a busy session lock waits in its pending file with what the gates still found, and the tech
    # lead hears it when the stop is applied
    def test_a_deferred_stop_keeps_what_the_gates_found_for_the_tech_leads_notice(self):
        from devkit import state
        with TempState():
            pg.start("Export", plan=["api=30m"])
            pg.assign("api", "Tank", why="the api")
            launch("a_tank", "devkit:tank", "Tank: api")
            stop = {"session_id": "s1", "agent_id": "a_tank", "agent_type": "devkit:tank",
                    "last_assistant_message": "done"}
            with mock.patch.object(state, "hold", side_effect=state.StateBusy("held by another hook")):
                pg.on_subagent("claude", stop, False, findings="proofs: dev progress step")
            self.assertEqual(pg.load_run("claude", "s1")["agents"]["a_tank"]["state"], "running")  # not applied yet
            self.assertEqual(pg.apply_stops("claude", "s1"), 1)
            told = [x.get("text") or "" for x in pg.load_run("claude", "s1").get("messages") or []
                    if x.get("to") == "main" and x.get("from") == "devkit" and "crew gates" in (x.get("text") or "")]
        self.assertEqual(len(told), 1, told)
        self.assertIn("Backend engineer (Tank) stopped, and the crew gates let it through with what is still missing: "
                      "proofs: dev progress step", told[0])


if __name__ == "__main__":
    unittest.main()
