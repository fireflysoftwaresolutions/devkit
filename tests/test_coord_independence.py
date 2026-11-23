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


