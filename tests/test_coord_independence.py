# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# A review never goes to its author. The reviewer and the authors are compared as crew members (the roster's ids), and
# a reviewer devkit cannot name takes no review; every step the review follows counts, directly or through other
# work; an author stays an author when a reopen gives its step back to the tech lead; and no path that hands a lane a
# step (its launch naming an unverified review, a direct take) gives it a review of its own work. The kernel tests run
# coord's pure functions on a fixed clock; the others drive the real progress API in a temporary folder
# (test_progress.TempState).
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_coord_independence -v
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import coord  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_progress import TempState, launch  # noqa: E402

T = 1_800_000_000.0  # a fixed clock
NEO_REPORT = ("**[Firefly> DevKit · Senior engineer (Neo)]** api · `main` · review\n- **Done:** x\n"
              "- **Check:** ✓ approve: read it\n- **Next:** the tech lead merges")
TANK_REPORT = ("**[Firefly> DevKit · Backend engineer (Tank)]** api · `main` · x\n- **Done:** x\n"
               "- **Check:** ✓ 3 tests pass\n- **Next:** the tech lead reviews it")


def kstep(name, owner="main", after=(), activity="backend", state="done"):
    return {"name": name, "state": state, "owner": owner, "after": list(after), "activity": activity}


class KernelTests(unittest.TestCase):
    def test_a_reviewer_devkit_cannot_name_is_not_independent(self):
        # coord.independent read an empty member as "nobody did the work": a lane with no persona took any review
        run = {"plan": [kstep("x", "L"), kstep("review-x", after=["x"], activity="review", state="pending")],
               "agents": {"L": {"persona": "Tank", "state": "done"}}}
        self.assertFalse(coord.independent(run, run["plan"][1], ""))


class Base(unittest.TestCase):
    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.t0 = time.time() - 7200

    def m(self, minutes):
        return self.t0 + minutes * 60

    def run_(self):
        return pg.load_run("claude", "s1")

    def st(self, name):
        return next(s for s in self.run_()["plan"] if s["name"] == name)

    def stop(self, aid, kind, report, at):
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": aid, "agent_type": kind,
                                  "last_assistant_message": report}, False, now=at)


class ReviewerIdentityTests(Base):
    def test_a_lane_that_names_no_crew_member_takes_no_review(self):
        # a general-purpose lane described "Neo second look at review-x" (no colon) has no persona, and its loose
        # label match gave it the review of Neo's own work
        pg.start("Fix", plan=["x=30m", "review-x=20m [review] after x"], now=self.m(0))
        pg.assign("review-x", "Neo", why="second look", now=self.m(0))
        pg.assign("x", "Neo", why="test", now=self.m(0))
        launch("a_neo", "devkit:neo", "Neo: x", at=self.m(1))
        pg.step("x done", which="x", check="✓ 3 tests pass", from_agent="a_neo", now=self.m(10))
        launch("a_gp", "general-purpose", "Neo second look at review-x", at=self.m(11))
        self.assertEqual((self.st("review-x").get("owner"), self.st("review-x")["state"]), ("main", "pending"))


class RelaunchTests(Base):
    def test_a_new_launch_of_the_author_leaves_its_unverified_review_to_another_member(self):
        # Neo's review of Tank's x ends unverified; x is reopened and Neo redoes it; a new launch "Neo: review-x"
        # took the review back, a review of its own work
        pg.start("Fix", plan=["x=30m", "review-x=20m [review] after x"], now=self.m(0))
        pg.assign("x", "Tank", why="test", now=self.m(0))
        launch("a_tank", "devkit:tank", "Tank: x", at=self.m(1))
        pg.step("x done", which="x", check="✓ 3 tests pass", from_agent="a_tank", now=self.m(10))
        pg.assign("review-x", "Neo", why="review", now=self.m(11))
        launch("a_neo", "devkit:neo", "Neo: review-x", at=self.m(12))
        self.stop("a_neo", "devkit:neo", NEO_REPORT, self.m(20))  # no gates' pass: the review ends unverified
        pg.reopen("x", why="rework by Neo", now=self.m(21))
        pg.assign("x", "Neo", why="rework", now=self.m(22))
        launch("a_neo2", "devkit:neo", "Neo: x", at=self.m(23))
        pg.step("x redone", which="x", check="✓ 4 tests pass", from_agent="a_neo2", now=self.m(30))
        launch("a_neo3", "devkit:neo", "Neo: review-x", at=self.m(31))
        run = self.run_()
        rx = next(s for s in run["plan"] if s["name"] == "review-x")
        self.assertEqual((rx.get("owner"), rx["state"]), ("a_neo", "unverified"))
        told = [m["text"] for m in run.get("messages") or [] if m.get("to") == "main" and m["ts"] >= round(self.m(31))]
        self.assertTrue(any("“review-x”" in t and "own work" in t for t in told), told)

    def test_a_lane_never_takes_a_review_of_its_own_work_directly(self):
        # the last line of defense under every caller: _take_step gives a lane no review of work its member did
        pg.start("Fix", plan=["x=30m", "review-x=20m [review] after x"], now=self.m(0))
        pg.assign("x", "Neo", why="test", now=self.m(0))
        launch("a_neo", "devkit:neo", "Neo: x", at=self.m(1))
        pg.step("x done", which="x", check="✓ 3 tests pass", from_agent="a_neo", now=self.m(10))
        run = self.run_()
        rx = next(s for s in run["plan"] if s["name"] == "review-x")
        self.assertFalse(pg._take_step(run, "a_neo", rx, self.m(11)))
        self.assertEqual((rx.get("owner") or "main", rx["state"]), ("main", "pending"))


