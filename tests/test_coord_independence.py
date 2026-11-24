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
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import coord, crew  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_progress import TempState, gates_passed, launch  # noqa: E402

T = 1_800_000_000.0  # a fixed clock
NEO_REPORT = ("**[Firefly> DevKit · Senior engineer (Neo)]** api · `main` · review\n- **Done:** x\n"
              "- **Check:** ✓ approve: read it\n- **Next:** the tech lead merges")
TANK_REPORT = ("**[Firefly> DevKit · Backend engineer (Tank)]** api · `main` · x\n- **Done:** x\n"
               "- **Check:** ✓ 3 tests pass\n- **Next:** the tech lead reviews it")
KEY_REPORT = TANK_REPORT.replace("Backend engineer (Tank)", "Security engineer (The Keymaker)")


def kstep(name, owner="main", after=(), activity="backend", state="done"):
    return {"name": name, "state": state, "owner": owner, "after": list(after), "activity": activity}


class KernelTests(unittest.TestCase):
    def test_a_reviewer_devkit_cannot_name_is_not_independent(self):
        # coord.independent read an empty member as "nobody did the work": a lane with no persona took any review
        run = {"plan": [kstep("x", "L"), kstep("review-x", after=["x"], activity="review", state="pending")],
               "agents": {"L": {"persona": "Tank", "state": "done"}}}
        self.assertFalse(coord.independent(run, run["plan"][1], ""))

    def test_a_reviewer_lane_is_never_named_by_the_label_of_the_review_it_would_take(self):
        # a lane with no persona and no launch description read as the member the review is assigned to, so it was
        # independent of the work it follows and could take that review
        review = dict(kstep("review-x", after=["x"], activity="review", state="pending"), label="Seraph")
        run = {"plan": [kstep("x", "L"), review], "agents": {"L": {"persona": "Tank", "state": "done"}}}
        anonymous = {"persona": "", "label": "", "type": "general-purpose", "state": "running"}
        self.assertFalse(coord.independent(run, review, anonymous))

    def test_the_chain_counts_the_work_a_review_follows_through_other_work_but_not_through_reviews(self):
        # Tank built api, Neo integrated it, Seraph reviewed api and Tank fixed what the review found: neither Tank
        # nor Neo reviews the integration or the fix, while Seraph, who only reviewed, reviews the fix
        plan = [kstep("api", "LT"), kstep("integrate", "LN", ["api"]), kstep("review-int", after=["integrate"],
                                                                              activity="review", state="pending"),
                kstep("review-api", "LS", ["api"], activity="review"), kstep("fix", "LT2", ["review-api"]),
                kstep("review-fix", after=["fix"], activity="review", state="pending")]
        run = {"plan": plan, "agents": {"LT": {"persona": "Tank"}, "LN": {"persona": "Neo"},
                                        "LS": {"persona": "Seraph"}, "LT2": {"persona": "Tank"}}}
        by = {s["name"]: s for s in plan}
        self.assertEqual([coord.independent(run, by["review-int"], m) for m in ("Tank", "Neo", "Seraph")],
                         [False, False, True])
        self.assertEqual([coord.independent(run, by["review-fix"], m) for m in ("Tank", "Seraph")],
                         [False, True])


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

    def test_assigning_a_review_when_the_roster_read_fails_is_refused_with_what_to_do(self):
        # the roster file rewritten while dev progress assign runs: the review rule read it, the reviewer's name did
        # not, and the command ended in the roster's own error, a traceback instead of a refusal
        pg.start("Fix", plan=["x=30m", "review-x=20m [review] after x"], now=self.m(0))

        def unreadable(*_args, **_kwargs):
            raise crew.RosterError(["not valid JSON (Expecting value: line 1 column 1)"])

        with mock.patch.object(crew, "_for_label", side_effect=unreadable):
            with self.assertRaisesRegex(pg.StepError, "cannot read the crew roster"):
                pg.assign("review-x", "Seraph", why="a second look", now=self.m(1))
        self.assertNotEqual(self.st("review-x").get("label"), "Seraph")


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


class ChainAndReopenTests(Base):
    def test_a_review_after_integration_does_not_go_to_the_member_that_built_what_was_integrated(self):
        pg.start("Ship", plan=["api=30m [backend]", "integrate=20m [backend] after api",
                               "review=20m [review] after integrate"], now=self.m(0))
        pg.assign("api", "Tank", now=self.m(0))
        launch("a_tank", "devkit:tank", "Tank: api", at=self.m(1))
        pg.step("api done", which="api", check="✓ 3 tests pass", from_agent="a_tank", now=self.m(10))
        pg.assign("integrate", "Neo", why="integration", now=self.m(11))
        launch("a_neo", "devkit:neo", "Neo: integrate", at=self.m(12))
        pg.step("integrated", which="integrate", check="✓ 5 tests pass", from_agent="a_neo", now=self.m(20))
        with self.assertRaisesRegex(pg.StepError, "own work"):
            pg.assign("review", "Tank", why="a second look", now=self.m(21))
        self.assertNotEqual(self.st("review").get("label"), "Tank")

    def test_a_reopened_step_keeps_its_author_so_its_review_avoids_that_member(self):
        # the reopen gave x back to the tech lead (owner main), which erased who did it
        pg.start("Fix", plan=["x=30m", "review-x=20m [review] after x"], now=self.m(0))
        pg.assign("x", "Tank", why="test", now=self.m(0))
        launch("a_tank", "devkit:tank", "Tank: x", at=self.m(1))
        pg.step("x done", which="x", check="✓ 3 tests pass", from_agent="a_tank", now=self.m(10))
        self.stop("a_tank", "devkit:tank", TANK_REPORT, self.m(11))
        pg.reopen("x", why="a gap", now=self.m(12))
        self.assertEqual(self.st("x").get("owner"), "main")
        with self.assertRaisesRegex(pg.StepError, "own work"):
            pg.assign("review-x", "Tank", why="a second look", now=self.m(13))

    def test_the_tech_leads_close_of_a_reopened_step_keeps_its_doer_for_the_approval(self):
        # the Security engineer built the pipeline (its approver for cicd work); after a reopen the tech lead closed
        # it, and the doer read as the tech lead, so the Security engineer was owed its own work's approval
        pg.start("Ship", plan=["pipeline=10m", "wrap=5m"], now=self.m(0))
        pg.assign("pipeline", "Keymaker", activity="cicd", why="the release engineer is away", now=self.m(0))
        launch("a_key", "devkit:keymaker", "Security engineer (The Keymaker): pipeline", at=self.m(1))
        pg.step("pipeline built", which="pipeline", check="✓ 3 jobs run", from_agent="a_key", now=self.m(10))
        self.stop("a_key", "devkit:keymaker", TANK_REPORT.replace("Backend engineer (Tank)",
                                                                  "Security engineer (The Keymaker)"), self.m(11))
        pg.reopen("pipeline", why="one more job", now=self.m(12))
        pg.step("pipeline redone", which="pipeline", check="✓ 4 jobs run", now=self.m(20))
        self.assertEqual(pg.approvals_owed(self.run_()), [("seraph", ["pipeline"])])


class EarlierAuthorTests(Base):
    def setUp(self):
        # the Security engineer, the Accountable member for cicd work, builds the pipeline; it is reopened and the
        # Release engineer redoes it: only the last author counted as its doer, so the Security engineer approved it
        super().setUp()
        pg.start("Ship", plan=["pipeline=10m", "threat=10m", "review=10m [review] after pipeline"], now=self.m(0))
        pg.assign("pipeline", "Keymaker", activity="cicd", why="the release engineer is away", now=self.m(0))
        launch("a_key", "devkit:keymaker", "Security engineer (The Keymaker): pipeline", at=self.m(1))
        pg.step("pipeline built", which="pipeline", check="✓ 3 jobs run", from_agent="a_key", now=self.m(10))
        self.stop("a_key", "devkit:keymaker", KEY_REPORT, self.m(11))
        pg.reopen("pipeline", why="one more job", now=self.m(12))
        pg.assign("pipeline", "Link", why="the release engineer is back", now=self.m(13))
        launch("a_link", "devkit:link", "Release engineer (Link): pipeline", at=self.m(14))
        pg.step("pipeline redone", which="pipeline", check="✓ 4 jobs run", from_agent="a_link", now=self.m(20))

    def test_no_author_of_a_reopened_step_is_owed_its_approval(self):
        self.assertEqual(pg.approvals_owed(self.run_()), [("seraph", ["pipeline"])])

    def test_an_earlier_authors_verdict_on_the_reopened_step_is_refused_as_self_approval(self):
        pg.assign("threat", "Keymaker", activity="security", now=self.m(21))
        launch("a_key2", "devkit:keymaker", "Security engineer (The Keymaker): threat", at=self.m(22))
        with self.assertRaisesRegex(pg.StepError, "self-approval"):
            pg.step("threat model", which="threat", check="✓ read it", from_agent="a_key2", approves="pipeline",
                    now=self.m(30))

    def test_a_review_by_the_approver_after_every_author_approves_the_reopened_step(self):
        pg.assign("review", "Seraph", why="r", now=self.m(21))
        launch("a_ser", "devkit:seraph", "Seraph: review", at=self.m(22))
        gates_passed("a_ser", at=self.m(29))
        self.stop("a_ser", "devkit:seraph", NEO_REPORT.replace("Senior engineer (Neo)", "Code reviewer (Seraph)"),
                  self.m(30))
        self.assertEqual(pg.approvals_owed(self.run_()), [])


if __name__ == "__main__":
    unittest.main()
