# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# One rule for every close and every stop of a step: did a lane that ran it, or the tech lead, work on it after a
# time? Work counts only when it is the step's: a file of its scope in a checkout of the project, a file its lanes
# edited for it, or a progress record that names it. The tech lead's close, an approval's ✓ and a review's ✓ of
# unverified work its author redid since, the author's own close of its unverified step, and the stop of a lane whose
# step was closed (by another, or by the lane itself) all ask it. Progress state in a temporary folder
# (test_progress.TempState), with a real git checkout as the session's folder, so a repository-relative scope names
# files in it.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_review_rework -v
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import approvals  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_progress import TempState, gates_passed, launch, repo_with_worktrees  # noqa: E402

TANK, SER = "Backend engineer (Tank)", "Code reviewer (Seraph)"


def report(role, task, check):
    return (f"**[Firefly> DevKit · {role}]** api · `main` · {task}\n- **Done:** {task}\n- **Check:** {check}\n"
            "- **Next:** the tech lead merges")


class Base(unittest.TestCase):
    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = repo_with_worktrees(tmp.name)[0]  # the session works in this checkout of acme/api
        sess = json.loads(self.state.sess.read_text())
        self.state.sess.write_text(json.dumps(dict(sess, top=str(self.repo))))
        self.t0 = time.time() - 7200

    def m(self, minutes):
        return self.t0 + minutes * 60

    def run_(self):
        return pg.load_run("claude", "s1")

    def st(self, name):
        return next(s for s in self.run_()["plan"] if s["name"] == name)

    def stop(self, aid, kind, text, at):
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": aid, "agent_type": kind,
                                  "last_assistant_message": text}, False, now=at)

    def edit(self, by, path, at):
        # an edit tool's call (Write) of `path`, a path in the checkout unless it is absolute, by lane `by` or main
        full = path if path.startswith("/") else str(self.repo / path)
        call = {"session_id": "s1", "tool_name": "Write", "tool_use_id": f"t{at}", "tool_input": {"file_path": full}}
        pg.note_edit("claude", dict(call, agent_id=by) if by != "main" else call, now=at)

    def approved(self, name):
        return [a["by"] for a in self.run_().get("approvals") or [] if name in (a.get("steps") or [])]

    def told(self, words):
        return [m["text"] for m in self.run_().get("messages") or [] if m.get("to") == "main" and words in m["text"]]

    def end_review(self, at=30):
        gates_passed("a_ser", at=self.m(at - 1))
        self.stop("a_ser", "devkit:seraph", report(SER, "review-x", "✓ approve: read it"), self.m(at))


class CloseOfReworkedWorkTests(Base):
    def setUp(self):
        # the Backend engineer's x (scope src/**) ends unverified at m10; the Code reviewer's review of it begins at m12
        super().setUp()
        pg.start("Fix", plan=["x=30m [backend]", "review-x=20m [review] after x"], now=self.m(0))
        pg.assign("x", "Tank", activity="backend", scope="src/**", now=self.m(0))
        launch("a_tank", "devkit:tank", f"{TANK}: x", at=self.m(1))
        pg.step("x built", which="x", check="⚠ 2 of 3 tests", from_agent="a_tank", now=self.m(10))
        pg.assign("review-x", "Seraph", activity="review", now=self.m(11))
        launch("a_ser", "devkit:seraph", f"{SER}: review-x", at=self.m(12))

    def test_the_tech_leads_close_after_its_authors_rework_leaves_it_to_a_new_review(self):
        # the Backend engineer rewrites src/x.py at m25 and the tech lead closes x at m28: only his own edits moved
        # when x's work ended, so the review begun at m12 approved the rewrite it never saw
        self.edit("a_tank", "src/x.py", self.m(25))
        pg.step("x", which="x", check="✓ ran the 3 tests myself", now=self.m(28), force=True)
        self.stop("a_tank", "devkit:tank", report(TANK, "x", "⚠ 2 of 3 tests"), self.m(29))
        self.end_review()
        self.assertEqual(self.approved("x"), [])
        self.assertTrue(self.told("“review-x” began before"))

    def test_a_reviews_check_after_its_authors_rework_neither_closes_nor_approves_the_work(self):
        # the Backend engineer rewrites src/x.py at m25 while x is unverified: the review's ✓ at m30 closed and
        # approved it, and the author's stop at m35 then looked only for work after that close
        self.edit("a_tank", "src/x.py", self.m(25))
        self.end_review()
        self.assertEqual((self.st("x")["state"], self.approved("x")), ("unverified", []))
        self.assertTrue(self.told("“review-x” began before"))
        self.stop("a_tank", "devkit:tank", report(TANK, "x", "⚠ 2 of 3 tests"), self.m(35))
        self.assertEqual(self.st("x")["unverified_at"], self.m(35))

    def test_the_authors_report_that_closes_its_unverified_step_ends_its_work_at_that_close(self):
        # the Backend engineer's report ✓ at m26 closes x with no recorded work since m10: only its own dev progress
        # step ✓ moved when x's work ended, so the review begun at m12 approved work closed after it began
        gates_passed("a_tank", at=self.m(25))
        self.stop("a_tank", "devkit:tank", report(TANK, "x", "✓ 3 of 3 tests"), self.m(26))
        self.assertEqual(self.st("x")["state"], "done")
        self.end_review()
        self.assertEqual(self.approved("x"), [])
        self.assertTrue(self.told("“review-x” began before"))

    def test_the_tech_leads_edit_of_the_same_path_in_another_repository_is_not_the_steps_work(self):
        # src/y.py of another repository ends like a file of x's scope: it counted as the tech lead's rework of x
        with tempfile.TemporaryDirectory() as other:
            self.edit("main", f"{other}/src/y.py", self.m(15))
        pg.step("x", which="x", check="✓ ran the 3 tests myself", now=self.m(20), force=True)
        self.end_review()
        self.assertEqual(self.approved("x"), ["seraph"])


class ApprovalOfReworkedWorkTests(Base):
    def test_an_approvals_close_of_work_reworked_since_it_ended_unverified_ends_that_work_at_the_close(self):
        # the Release engineer's pipeline (scope .github/**) ends unverified at m10 and is rewritten at m25; the
        # Security engineer's ✓ at m26 closes it: that close kept m10 as the end of its work, so a review begun at
        # m12 would approve the rewrite it never saw
        pg.start("Work", plan="pipeline=10m; threat=10m", now=self.m(0))
        pg.assign("pipeline", "Link", activity="cicd", scope=".github/**", now=self.m(0))
        launch("a_link", "devkit:link", "Release engineer (Link): pipeline", self.m(1))
        pg.step("pipeline built", which="pipeline", check="⚠ 2 of 3 jobs run", from_agent="a_link", now=self.m(10))
        pg.assign("threat", "Keymaker", activity="security", now=self.m(11))
        launch("a_key", "devkit:keymaker", "Security engineer (The Keymaker): threat", self.m(12))
        self.edit("a_link", ".github/workflows/ci.yml", self.m(25))
        pg.step("threat model done", which="threat", check="✓ ran the 3 jobs", from_agent="a_key",
                approves="pipeline", now=self.m(26))
        gates_passed("a_key", at=self.m(27))
        self.stop("a_key", "devkit:keymaker", report("Security engineer (The Keymaker)", "threat", "✓ ran the 3 jobs"),
                  self.m(28))
        pipeline = self.st("pipeline")
        self.assertEqual((pipeline["state"], self.approved("pipeline")), ("done", ["keymaker"]))
        self.assertFalse(approvals.saw(self.run_(), {"started": self.m(12)}, pipeline))


class LaneWorkAfterACloseTests(Base):
    def test_a_lanes_edit_for_its_next_step_leaves_the_step_the_tech_lead_closed_done(self):
        # x (src/**) and y (web/**) are both the Backend engineer's: the tech lead closes x at m15, and the lane's
        # edit of web/y.ts at m25, for y, made x unverified again at its stop
        pg.start("Fix", plan=["x=30m [backend]", "y=30m [backend]"], now=self.m(0))
        pg.assign("x", "Tank", activity="backend", scope="src/**", now=self.m(0))
        pg.assign("y", "Tank", activity="backend", scope="web/**", now=self.m(0))
        launch("a_tank", "devkit:tank", f"{TANK}: x and y", at=self.m(1))
        pg.step("x built", which="x", check="⚠ 2 of 3 tests", from_agent="a_tank", now=self.m(10))
        pg.step("x", which="x", check="✓ ran the 3 tests myself", now=self.m(15), force=True)
        self.edit("a_tank", "web/y.ts", self.m(25))
        gates_passed("a_tank", at=self.m(29))
        self.stop("a_tank", "devkit:tank", report(TANK, "y", "✓ 4 of 4 tests"), self.m(30))
        self.assertEqual(self.st("x")["state"], "done")
        self.assertEqual(self.told("unverified again"), [])

    def test_the_authors_edit_in_its_steps_scope_after_closing_it_itself_makes_it_unverified_again(self):
        # the Backend engineer closes x itself at m10 and rewrites src/x.py, in x's scope, at m25: its own close kept x
        # done, so the review begun at m12 approved the rewrite it never saw
        pg.start("Fix", plan=["x=30m [backend]", "review-x=20m [review] after x"], now=self.m(0))
        pg.assign("x", "Tank", activity="backend", scope="src/**", now=self.m(0))
        launch("a_tank", "devkit:tank", f"{TANK}: x", at=self.m(1))
        pg.step("x built", which="x", check="✓ 3 of 3 tests", from_agent="a_tank", now=self.m(10))
        pg.assign("review-x", "Seraph", activity="review", now=self.m(11))
        launch("a_ser", "devkit:seraph", f"{SER}: review-x", at=self.m(12))
        self.edit("a_tank", "src/x.py", self.m(25))
        gates_passed("a_tank", at=self.m(28))
        self.stop("a_tank", "devkit:tank", report(TANK, "x", "✓ 3 of 3 tests"), self.m(29))
        self.assertEqual(self.st("x")["state"], "unverified")
        self.end_review()
        self.assertEqual(self.approved("x"), [])

    def test_a_close_the_runs_history_no_longer_holds_still_names_who_closed_it(self):
        # the tech lead closes x at m15, then the run's history moves on by 200 changes of his wrap step: who closed x
        # was read from that history, so the lane's rewrite of src/x.py at m25 left x done
        pg.start("Fix", plan=["x=30m [backend]", "wrap=5m"], now=self.m(0))
        pg.assign("x", "Tank", activity="backend", scope="src/**", now=self.m(0))
        launch("a_tank", "devkit:tank", f"{TANK}: x", at=self.m(1))
        pg.step("x built", which="x", check="⚠ 2 of 3 tests", from_agent="a_tank", now=self.m(10))
        pg.step("x", which="x", check="✓ ran the 3 tests myself", now=self.m(15), force=True)
        for k in range(100):
            pg.step("wrap", which="wrap", check="✓ wrapped", now=self.m(16) + 2 * k, force=True)
            pg.reopen("wrap", why="again", now=self.m(16) + 2 * k + 1)
        self.assertFalse([h for h in self.run_()["history"] if h.get("step") == "x"])
        self.edit("a_tank", "src/x.py", self.m(25))
        self.stop("a_tank", "devkit:tank", report(TANK, "x", "⚠ 2 of 3 tests"), self.m(30))
        self.assertEqual(self.st("x")["state"], "unverified")


class StepWorkRuleTests(Base):
    def two_steps(self, x_scope="src/**"):
        # x (x_scope) and y (web/**) are both the Backend engineer's, in one lane; x ends unverified at m10, and the lane
        # goes on to y
        pg.start("Fix", plan=["x=30m [backend]", "y=30m [backend]"], now=self.m(0))
        pg.assign("x", "Tank", activity="backend", scope=x_scope, now=self.m(0))
        pg.assign("y", "Tank", activity="backend", scope="web/**", now=self.m(0))
        launch("a_tank", "devkit:tank", f"{TANK}: x and y", at=self.m(1))
        pg.step("x built", which="x", check="⚠ 2 of 3 tests", from_agent="a_tank", now=self.m(10))

    def test_a_file_the_lane_edited_after_the_step_ended_is_not_the_steps_file(self):
        # web/y.ts, edited for y at m12, after x's work ended at m10: a file is x's only when edited before x's work
        # ended, not before the tech lead's close at m15
        self.two_steps()
        self.edit("a_tank", "web/y.ts", self.m(12))
        pg.step("x", which="x", check="✓ ran the 3 tests myself", now=self.m(15), force=True)
        self.edit("a_tank", "web/y.ts", self.m(25))
        self.stop("a_tank", "devkit:tank", report(TANK, "y", "⚠ 3 of 4 tests"), self.m(30))
        self.assertEqual(self.st("x")["state"], "done")

    def test_a_file_the_lane_edited_before_the_step_started_is_not_the_steps_file(self):
        # the lane edits web/w.ts for w at m5 and closes w at m8, then takes x: going back to web/w.ts at m25 is w's
        # work, not x's
        pg.start("Fix", plan=["w=30m [backend]", "x=30m [backend]"], now=self.m(0))
        pg.assign("w", "Tank", activity="backend", scope="web/**", now=self.m(0))
        pg.assign("x", "Tank", activity="backend", scope="src/**", now=self.m(0))
        launch("a_tank", "devkit:tank", f"{TANK}: w", at=self.m(1))
        self.edit("a_tank", "web/w.ts", self.m(5))
        pg.step("w built", which="w", check="✓ 2 of 2 tests", from_agent="a_tank", now=self.m(8))
        self.assertEqual((self.st("x")["state"], self.st("x")["started"]), ("running", self.m(8)))
        pg.step("x built", which="x", check="⚠ 2 of 3 tests", from_agent="a_tank", now=self.m(10))
        pg.step("x", which="x", check="✓ ran the 3 tests myself", now=self.m(15), force=True)
        self.edit("a_tank", "web/w.ts", self.m(25))
        self.stop("a_tank", "devkit:tank", report(TANK, "w", "⚠ 1 of 2 tests"), self.m(30))
        self.assertEqual((self.st("x")["state"], self.st("w")["state"]), ("done", "unverified"))

    def test_a_note_naming_no_step_while_the_lane_runs_another_step_is_not_the_closed_steps_work(self):
        self.two_steps()
        pg.step("x", which="x", check="✓ ran the 3 tests myself", now=self.m(15), force=True)
        pg.note("half of the page is done", from_agent="a_tank", now=self.m(25))
        self.stop("a_tank", "devkit:tank", report(TANK, "y", "⚠ 3 of 4 tests"), self.m(30))
        self.assertEqual(self.st("x")["state"], "done")

    def test_a_file_of_another_steps_scope_is_not_the_work_of_a_step_with_no_scope(self):
        # x has no scope; the lane closes y itself at m20, then edits web/y.ts, y's file, at m25: that is y's work
        self.two_steps(x_scope="")
        pg.step("x", which="x", check="✓ ran the 3 tests myself", now=self.m(15), force=True)
        pg.step("y built", which="y", check="✓ 4 of 4 tests", from_agent="a_tank", now=self.m(20))
        self.edit("a_tank", "web/y.ts", self.m(25))
        self.stop("a_tank", "devkit:tank", report(TANK, "y", "✓ 4 of 4 tests"), self.m(30))
        self.assertEqual((self.st("x")["state"], self.st("y")["state"]), ("done", "unverified"))

    def test_the_tech_leads_edit_of_the_steps_file_after_his_own_close_makes_it_unverified_again(self):
        # the Backend engineer wrote tests/test_x.py for x; the tech lead closes x at m15 and rewrites that file at m20
        self.two_steps()
        self.edit("a_tank", "tests/test_x.py", self.m(5))
        pg.step("x", which="x", check="✓ ran the 3 tests myself", now=self.m(15), force=True)
        self.edit("main", "tests/test_x.py", self.m(20))
        self.stop("a_tank", "devkit:tank", report(TANK, "y", "⚠ 3 of 4 tests"), self.m(30))
        self.assertEqual(self.st("x")["state"], "unverified")
        self.assertTrue(self.told("Tech lead (Morpheus) worked on “x” after closing it"))


if __name__ == "__main__":
    unittest.main()
