# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# A verdict covers the work it saw. A crew member's `dev progress step --approves` waits in its lane until its stop,
# so the step it names can be reopened, reworked and end unverified again meanwhile: that verdict then closes and
# approves nothing, and a step an approval closes is never dated before it last ended unverified. The approver hears
# why the step it approves is unverified, so a ✓ is given knowingly. Progress state in a temporary folder
# (test_progress.TempState).
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_approvals_rework -v
import contextlib
import io
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import progress as pg  # noqa: E402
from test_progress import TempState, gates_passed, launch  # noqa: E402

KEY_REPORT = ("**[Firefly> DevKit · Security engineer (The Keymaker)]** api · `main` · threat\n- **Done:** x\n"
              "- **Check:** ✓ ran the 3 jobs\n- **Next:** the tech lead merges")
LINK_REPORT = ("**[Firefly> DevKit · Release engineer (Link)]** api · `main` · pipeline\n- **Done:** x\n"
               "- **Check:** ⚠ 2 of 3 jobs run\n- **Next:** the tech lead reviews it")


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

    def approved(self, name):
        return [a for a in self.run_().get("approvals") or [] if name in (a.get("steps") or [])]


class StaleVerdictTests(Base):
    def setUp(self):
        # the Release engineer's pipeline ends unverified at m10; the Security engineer, its approver, gives its ✓ at
        # m20, held in its lane until its stop
        super().setUp()
        pg.start("Work", plan="pipeline=10m; threat=10m; wrap=5m", now=self.m(0))
        pg.assign("pipeline", "Link", activity="cicd", now=self.m(0))
        launch("a_link", "devkit:link", "Release engineer (Link): pipeline", self.m(1))
        pg.step("pipeline built", which="pipeline", check="⚠ 2 of 3 jobs run", from_agent="a_link", now=self.m(10))
        pg.assign("threat", "Keymaker", activity="security", now=self.m(11))
        launch("a_key", "devkit:keymaker", "Security engineer (The Keymaker): threat", self.m(12))
        pg.step("threat model done", which="threat", check="✓ ran the 3 jobs", from_agent="a_key",
                approves="pipeline", now=self.m(20))

    def test_a_verdict_given_before_a_reopen_does_not_close_the_reworked_step(self):
        # the tech lead reopens the pipeline at m25, the Release engineer reworks it and ends unverified again at m40:
        # the Security engineer's stop at m50 closed it done, dated m20, with the verdict on the old work
        pg.reopen("pipeline", why="rework", now=self.m(25))
        pg.step("pipeline rebuilt", which="pipeline", check="⚠ 0 of 3 jobs run, rewrote the deploy job",
                from_agent="a_link", now=self.m(40))
        gates_passed("a_key", at=self.m(49))
        self.stop("a_key", "devkit:keymaker", KEY_REPORT, self.m(50))
        self.assertEqual(self.st("pipeline")["state"], "unverified")
        self.assertEqual(self.approved("pipeline"), [])

    def test_a_reopen_drops_the_verdicts_lanes_hold_on_the_step_and_tells_their_members(self):
        pg.reopen("pipeline", why="rework", now=self.m(25))
        run = self.run_()
        held = [v for v in run["agents"]["a_key"].get("verdicts") or [] if "pipeline" in v.get("steps") or []]
        self.assertEqual(held, [])
        told = [m["text"] for m in run.get("messages") or [] if m.get("to") == "a_key"]
        self.assertTrue(any("“pipeline”" in t and "reopened" in t for t in told), told)

    def test_a_verdict_older_than_the_steps_last_unverified_end_closes_nothing(self):
        # no reopen: the Release engineer's lane ends, a new launch of it takes the unverified pipeline again and
        # ends it unverified at m40, after the verdict
        self.stop("a_link", "devkit:link", LINK_REPORT, self.m(30))
        launch("a_link2", "devkit:link", "Release engineer (Link): pipeline", self.m(31))
        self.assertEqual((self.st("pipeline")["owner"], self.st("pipeline")["state"]), ("a_link2", "running"))
        pg.step("pipeline rebuilt", which="pipeline", check="⚠ 0 of 3 jobs run", from_agent="a_link2",
                now=self.m(40))
        gates_passed("a_key", at=self.m(49))
        self.stop("a_key", "devkit:keymaker", KEY_REPORT, self.m(50))
        self.assertEqual(self.st("pipeline")["state"], "unverified")
        self.assertEqual(self.approved("pipeline"), [])

    def test_the_authors_stop_with_no_work_since_keeps_a_verdict_given_after_its_step_ended(self):
        # the Release engineer's lane stops at m30 with no work since its ⚠ at m10: that stop moved the time the
        # pipeline ended unverified to m30, after the Security engineer's verdict, which the run then refused
        self.stop("a_link", "devkit:link", LINK_REPORT, self.m(30))
        gates_passed("a_key", at=self.m(49))
        self.stop("a_key", "devkit:keymaker", KEY_REPORT, self.m(50))
        self.assertEqual(self.st("pipeline")["state"], "done")
        self.assertEqual([a["by"] for a in self.approved("pipeline")], ["keymaker"])

    def test_the_authors_stop_after_more_work_on_the_step_voids_an_earlier_verdict(self):
        pg.note("fixed the deploy job", which="pipeline", from_agent="a_link", now=self.m(25))
        self.stop("a_link", "devkit:link", LINK_REPORT, self.m(30))
        gates_passed("a_key", at=self.m(49))
        self.stop("a_key", "devkit:keymaker", KEY_REPORT, self.m(50))
        self.assertEqual(self.st("pipeline")["state"], "unverified")
        self.assertEqual(self.approved("pipeline"), [])

    def edit(self, aid, at):
        # what the edit hook records for a Write call of lane `aid`
        pg.note_edit("claude", {"session_id": "s1", "agent_id": aid, "tool_name": "Write", "tool_use_id": f"t_{at}",
                                "tool_input": {"file_path": "/tmp/api/.github/workflows/ci.yml"}}, now=at)

    def test_the_authors_silent_edit_after_its_step_ended_voids_an_earlier_verdict(self):
        # the Release engineer's lane rewrites the workflow at m25 and records nothing: its stop kept the time the
        # pipeline ended, m10, so the Security engineer's verdict of m20 closed and approved work it never saw
        self.edit("a_link", self.m(25))
        self.stop("a_link", "devkit:link", LINK_REPORT, self.m(30))
        gates_passed("a_key", at=self.m(49))
        self.stop("a_key", "devkit:keymaker", KEY_REPORT, self.m(50))
        self.assertEqual(self.st("pipeline")["state"], "unverified")
        self.assertEqual(self.approved("pipeline"), [])

    def test_another_lanes_edit_after_the_step_ended_keeps_the_verdict(self):
        self.edit("a_key", self.m(25))
        self.stop("a_link", "devkit:link", LINK_REPORT, self.m(30))
        gates_passed("a_key", at=self.m(49))
        self.stop("a_key", "devkit:keymaker", KEY_REPORT, self.m(50))
        self.assertEqual([a["by"] for a in self.approved("pipeline")], ["keymaker"])

    def test_the_authors_note_on_a_step_its_lane_does_not_hold_voids_an_earlier_verdict(self):
        # the note names the tech lead's wrap step, which the lane does not hold: its work may be the pipeline's
        pg.note("fixed the deploy job", which="wrap", from_agent="a_link", now=self.m(25))
        self.stop("a_link", "devkit:link", LINK_REPORT, self.m(30))
        gates_passed("a_key", at=self.m(49))
        self.stop("a_key", "devkit:keymaker", KEY_REPORT, self.m(50))
        self.assertEqual(self.st("pipeline")["state"], "unverified")
        self.assertEqual(self.approved("pipeline"), [])

    def test_a_closed_step_is_never_dated_before_it_last_ended_unverified(self):
        run = self.run_()
        s = next(x for x in run["plan"] if x["name"] == "pipeline")
        entry = {"ts": self.m(5), "by": "keymaker", "steps": ["pipeline"], "verdict": "approved",
                 "closes": ["pipeline"], "check": "✓ ran the 3 jobs"}
        pg._close_approved(run, entry, "a_key", "Security engineer (The Keymaker)", self.m(20), "claude", "s1")
        self.assertEqual(s["state"], "done")
        self.assertGreaterEqual(s["finished"], self.m(10))


TANK_REPORT = ("**[Firefly> DevKit · Backend engineer (Tank)]** api · `main` · x\n- **Done:** x\n"
               "- **Check:** ⚠ 2 of 3 tests\n- **Next:** the tech lead reviews it")
SER_REPORT = ("**[Firefly> DevKit · Code reviewer (Seraph)]** api · `main` · review-notes, review-x\n"
              "- **Done:** x\n- **Check:** ✓ approve: read it\n- **Next:** the tech lead merges")


class ReviewOfReworkedWorkTests(Base):
    def setUp(self):
        # the Backend engineer's x ends unverified at m10; the Code reviewer's review of it begins at m12
        super().setUp()
        pg.start("Fix", plan=["x=30m", "review-x=20m [review] after x"], now=self.m(0))
        pg.assign("x", "Tank", why="t", now=self.m(0))
        launch("a_tank", "devkit:tank", "Tank: x", self.m(1))
        pg.step("x done", which="x", check="⚠ 2 of 3 tests", from_agent="a_tank", now=self.m(10))
        pg.assign("review-x", "Seraph", why="r", now=self.m(11))
        launch("a_ser", "devkit:seraph", "Seraph: review-x", self.m(12))

    def end_review(self):
        gates_passed("a_ser", at=self.m(29))
        self.stop("a_ser", "devkit:seraph", SER_REPORT, self.m(30))

    def test_a_review_begun_before_a_rework_neither_closes_nor_approves_the_reworked_step(self):
        # x is reopened at m15 and reworked, unverified again at m25: the review's ✓ at m30 closed it done, approved
        pg.reopen("x", why="rework", now=self.m(15))
        pg.step("x redone", which="x", check="⚠ rewrote it", from_agent="a_tank", now=self.m(25))
        self.end_review()
        self.assertEqual(self.st("x")["state"], "unverified")
        self.assertEqual(self.approved("x"), [])
        told = [m["text"] for m in self.run_().get("messages") or [] if m.get("to") == "main"]
        self.assertTrue(any("“review-x” began before" in t and "“x”" in t for t in told), told)

    def test_reopening_unverified_work_flags_the_review_running_on_it_for_recheck(self):
        # only a done or skipped step flagged its dependents: the review kept running on the old work unflagged
        pg.reopen("x", why="rework", now=self.m(15))
        self.assertEqual(self.st("review-x").get("recheck"), ["x"])

    def test_a_new_launch_taking_unverified_work_again_flags_the_review_running_on_it_for_recheck(self):
        self.stop("a_tank", "devkit:tank", TANK_REPORT, self.m(13))
        launch("a_tank2", "devkit:tank", "Tank: x", self.m(14))
        self.assertEqual((self.st("x")["owner"], self.st("x")["state"]), ("a_tank2", "running"))
        self.assertEqual(self.st("review-x").get("recheck"), ["x"])

    def test_a_review_begun_before_a_rework_that_ended_done_does_not_approve_it(self):
        pg.reopen("x", why="rework", now=self.m(15))
        pg.step("x redone", which="x", check="✓ 3 of 3 tests", from_agent="a_tank", now=self.m(25))
        self.end_review()
        self.assertEqual(self.approved("x"), [])
        self.assertIn(("seraph", ["x"]), pg.approvals_owed(self.run_()))

    def test_the_tech_leads_close_of_unverified_work_while_its_review_runs_leaves_the_review_its_approval(self):
        # the tech lead closes x with his own evidence at m20, while the review begun at m12 runs: x's work still
        # ended at m10, so the review's ✓ at m30 approves it, and nobody hears the review began before x ended
        # (--force: no text describes a step named x)
        pg.step("x", which="x", check="✓ ran the 3 tests myself", now=self.m(20), force=True)
        self.end_review()
        self.assertEqual([a["by"] for a in self.approved("x")], ["seraph"])
        self.assertEqual(pg.approvals_owed(self.run_()), [])
        told = [m["text"] for m in self.run_().get("messages") or [] if "began before" in m.get("text", "")]
        self.assertEqual(told, [])


class ReviewTakenLaterTests(Base):
    def test_a_reviewer_lane_that_takes_the_review_after_the_work_ended_still_closes_and_approves_it(self):
        # the Code reviewer's lane starts at m5 on the review of the notes and takes review-x when x ends unverified
        # at m10: that review began then, not when its lane started
        pg.start("Fix", plan=["x=30m", "notes=5m", "review-notes=10m [review] after notes",
                              "review-x=20m [review] after x"], now=self.m(0))
        pg.assign("x", "Tank", why="t", now=self.m(0))
        launch("a_tank", "devkit:tank", "Tank: x", self.m(1))
        pg.step("notes written", which="notes", check="✓ read them", now=self.m(2))
        pg.assign("review-notes", "Seraph", why="r", now=self.m(3))
        pg.assign("review-x", "Seraph", why="r", now=self.m(3))
        launch("a_ser", "devkit:seraph", "Seraph: review-notes", self.m(5))
        pg.step("x done", which="x", check="⚠ 2 of 3 tests", from_agent="a_tank", now=self.m(10))
        self.assertEqual((self.st("review-x")["owner"], self.st("review-x")["state"]), ("a_ser", "running"))
        gates_passed("a_ser", at=self.m(29))
        self.stop("a_ser", "devkit:seraph", SER_REPORT, self.m(30))
        self.assertEqual(self.st("x")["state"], "done")
        self.assertEqual([a["by"] for a in self.approved("x")], ["seraph"])

    def test_a_reviewer_resumed_on_its_reopened_review_after_the_rework_approves_the_new_work(self):
        # the review asked for changes at m14; x is reworked, done at m25; the review is reopened and its lane
        # resumed at m27: the review keeps its first start (m12), and it began again when the lane took it back
        pg.start("Fix", plan=["x=30m", "review-x=20m [review] after x"], now=self.m(0))
        pg.assign("x", "Tank", why="t", now=self.m(0))
        launch("a_tank", "devkit:tank", "Tank: x", self.m(1))
        pg.step("x done", which="x", check="✓ 3 tests", from_agent="a_tank", now=self.m(10))
        pg.assign("review-x", "Seraph", why="r", now=self.m(11))
        launch("a_ser", "devkit:seraph", "Seraph: review-x", self.m(12))
        gates_passed("a_ser", at=self.m(13))
        self.stop("a_ser", "devkit:seraph", SER_REPORT.replace("✓ approve: read it", "✗ no error test"), self.m(14))
        pg.reopen("x", why="the error test", now=self.m(15))
        pg.step("x redone", which="x", check="✓ 4 tests", from_agent="a_tank", now=self.m(25))
        pg.reopen("review-x", why="review the fix", now=self.m(26))
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a_ser", "agent_type": "devkit:seraph"}, True,
                       now=self.m(27))
        self.assertEqual((self.st("review-x")["owner"], self.st("review-x")["state"]), ("a_ser", "running"))
        gates_passed("a_ser", at=self.m(29))
        self.stop("a_ser", "devkit:seraph", SER_REPORT, self.m(30))
        self.assertEqual(pg.approvals_owed(self.run_()), [])


class UnverifiedReasonTests(Base):
    def test_a_members_verdict_on_unverified_work_says_why_that_work_is_unverified(self):
        pg.start("Work", plan="pipeline=10m; threat=10m", now=self.m(0))
        pg.assign("pipeline", "Link", activity="cicd", now=self.m(0))
        launch("a_link", "devkit:link", "Release engineer (Link): pipeline", self.m(1))
        pg.step("pipeline built", which="pipeline", check="⚠ 2 of 3 jobs run", from_agent="a_link", now=self.m(10))
        pg.assign("threat", "Keymaker", activity="security", now=self.m(11))
        launch("a_key", "devkit:keymaker", "Security engineer (The Keymaker): threat", self.m(12))
        run = pg.step("read the pipeline", check="✓ ran the 3 jobs", from_agent="a_key", approves="pipeline",
                      now=self.m(20))
        why = run["lane_outcome"]["why"]
        self.assertIn("“pipeline”", why)
        self.assertIn("2 of 3 jobs run", why)

    def test_the_tech_leads_verdict_on_unverified_work_says_why_it_was_unverified(self):
        # docs is approved by the tech lead: his ✓ closes the unverified step at once, and his command says why it
        # was unverified
        from devkit import cli
        pg.start("Docs", plan="docs=10m; wrap=5m", now=self.m(0))
        pg.assign("docs", "Ghost", activity="docs", now=self.m(0))
        launch("a_ghost", "devkit:ghost", "Technical writer (Ghost): docs", self.m(1))
        pg.step("docs written", which="docs", check="⚠ 1 of 2 pages checked", from_agent="a_ghost", now=self.m(10))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["progress", "step", "wrap", "--step", "wrap", "--check", "✓ read both pages",
                           "--approves", "docs"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.st("docs")["state"], "done")
        self.assertIn("1 of 2 pages checked", out.getvalue())


if __name__ == "__main__":
    unittest.main()
