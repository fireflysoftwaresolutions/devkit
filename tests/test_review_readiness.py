# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# A review step is offered after unverified work (bug B-fy3i742ews7, plan step "review readiness fix"). Under F6 a
# member's step ends unverified without evidence, and a review step's Check ✓ approves the work it follows; but the
# plan's one dependency rule (progress.deps_open) counted unverified as open, so the review that would approve that
# work was never offered. A review step (its kind of work is review, or its member works review-only) now treats an
# unverified dependency as satisfied; every other step still waits for done or skipped; the review's ✓ ends the work
# it approves done; and the run still cannot end while an approval is owed.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_review_readiness
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import crew, dispatch, progress as pg  # noqa: E402
from test_language_neutral_hooks import HookCase  # noqa: E402
from test_progress import gates_passed, launch  # noqa: E402

REPORT = ("**[Firefly> DevKit · {who}]** api · `main` · {task}\n- **Done:** {task}\n- **Check:** {check}\n"
          "- **Next:** Morpheus")


class ReviewReadinessTests(HookCase):
    def setUp(self):
        super().setUp()
        self.t0 = time.time()

    def stop(self, aid, kind, report, later=0):
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": aid, "agent_type": kind,
                                  "last_assistant_message": report}, False, now=self.t0 + later)
        return {s["name"]: s for s in pg.load_run("claude", "s1")["plan"]}

    def run_(self):
        return pg.load_run("claude", "s1")

    def unverified(self, step, member, kind, aid, activity=""):
        # the member's lane stops without evidence: its step ends unverified (approvals.ending)
        pg.assign(step, member, activity=activity)
        launch(aid, kind, f"{member}: {step}")
        plan = self.stop(aid, kind, "Finished it, all good.")
        self.assertEqual(plan[step]["state"], "unverified")

    def builder_plan(self):
        # api (Tank) ends unverified; review (Seraph) follows it; client follows the review; cli follows api
        pg.start("Work", plan="api=10m; review=10m after api; client=10m after review; cli=10m after api")
        self.unverified("api", "Tank", "devkit:tank", "a_tank")
        pg.assign("review", "Seraph")
        pg.assign("client", "Tank", activity="backend")
        pg.assign("cli", "Tank", activity="backend")

    def test_a_review_step_is_ready_after_unverified_work_and_a_builder_is_not(self):
        self.builder_plan()
        run = self.run_()
        self.assertEqual([s["name"] for s in pg.ready(run)], ["review"])
        self.assertEqual(pg.deps_open(run, pg._find_step(run, "cli")), ["api"])
        r = dispatch.ready(run)  # what dev crew next launches
        self.assertEqual([s["name"] for s in r["launch"] + r["queued"]], ["review"])
        self.assertEqual(sorted((s["name"], why) for s, why in r["waiting"]),
                         [("cli", "after api"), ("client", "after review")])
        nodes = {n["label"]: n["state"] for n in crew.session_graph(run)["nodes"] if n["kind"] == "step"}
        self.assertEqual((nodes["review"], nodes["client"], nodes["cli"]), ("ready", "waiting", "waiting"))

    def test_a_step_for_a_review_only_member_is_ready_after_unverified_work(self):
        # its kind of work is red team, not review: the member's review-only tool level makes it a review step
        pg.start("Work", plan="api=10m; attack=10m after api; deploy=10m after api")
        self.unverified("api", "Tank", "devkit:tank", "a_tank")
        pg.assign("attack", "Smith", activity="redteam")
        pg.assign("deploy", "Link", activity="cicd")
        self.assertEqual([s["name"] for s in pg.ready(self.run_())], ["attack"])

    def test_a_review_only_members_check_approves_the_unverified_work_it_follows(self):
        # the Code reviewer's batch 5: is_review_step offered the Product owner's step after unverified work (his tool
        # level is review-only), but _verified_by_review and from_review read only the kind of work, so his ✓ left
        # the work he approves unverified and recorded no approval. One rule (approvals.is_review) for all three
        pg.start("Work", plan="requirements=10m; scope=10m after requirements")
        self.unverified("requirements", "The Oracle", "devkit:oracle", "a_oracle", activity="requirements")
        pg.assign("scope", "Hamann", activity="scope")
        self.assertEqual([s["name"] for s in pg.ready(self.run_())], ["scope"])
        launch("a_hamann", "devkit:hamann", "Hamann: scope")
        gates_passed("a_hamann", at=self.t0 + 60)  # its report passes the crew gates
        plan = self.stop("a_hamann", "devkit:hamann", REPORT.format(who="Product owner (Councillor Hamann)",
                                                                    task="scope", check="✓ the three must-haves hold"),
                         later=60)
        self.assertEqual((plan["requirements"]["state"], plan["scope"]["state"]), ("done", "done"))
        run = self.run_()
        self.assertEqual([(a["by"], a["steps"], a["verdict"]) for a in run.get("approvals") or []],
                         [("hamann", ["requirements"], "approved")])
        self.assertNotIn("requirements", [n for _, names in pg.approvals_owed(run) for n in names])

    def test_the_reviews_check_ends_the_work_it_approves_done(self):
        self.builder_plan()
        launch("a_seraph", "devkit:seraph", "Seraph: review")
        gates_passed("a_seraph", at=self.t0 + 60)  # its report passes the crew gates
        plan = self.stop("a_seraph", "devkit:seraph", REPORT.format(who="Code reviewer (Seraph)", task="review",
                                                                    check="✓ read the diff, 9 tests pass"), later=60)
        self.assertEqual((plan["api"]["state"], plan["review"]["state"]), ("done", "done"))
        run = self.run_()
        self.assertEqual([(a["by"], a["steps"], a["verdict"]) for a in run.get("approvals") or []],
                         [("seraph", ["api"], "approved")])
        self.assertEqual(pg.approvals_owed(run), [])
        self.assertEqual([s["name"] for s in pg.ready(run)], ["client", "cli"])

    def test_a_review_that_asks_for_changes_leaves_the_work_unverified(self):
        self.builder_plan()
        launch("a_seraph", "devkit:seraph", "Seraph: review")
        plan = self.stop("a_seraph", "devkit:seraph", REPORT.format(who="Code reviewer (Seraph)", task="review",
                                                                    check="✗ the errors are not problem details"),
                         later=60)
        self.assertEqual((plan["api"]["state"], plan["review"]["state"]), ("unverified", "unverified"))
        self.assertEqual(self.run_().get("approvals") or [], [])

    def test_work_the_reviewer_does_not_approve_still_owes_its_approval_and_the_run_cannot_end(self):
        # Link's pipeline is approved by The Keymaker, not Seraph: Seraph's ✓ leaves it unverified, and once Morpheus
        # closes it with evidence the run still cannot end until The Keymaker approves it
        pg.start("Work", plan="pipeline=10m; review=10m after pipeline")
        self.unverified("pipeline", "Link", "devkit:link", "a_link", activity="cicd")
        pg.assign("review", "Seraph")
        self.assertEqual([s["name"] for s in pg.ready(self.run_())], ["review"])
        launch("a_seraph", "devkit:seraph", "Seraph: review")
        gates_passed("a_seraph", at=self.t0 + 60)  # its report passes the crew gates
        plan = self.stop("a_seraph", "devkit:seraph", REPORT.format(who="Code reviewer (Seraph)", task="review",
                                                                    check="✓ read the workflow"), later=60)
        self.assertEqual((plan["pipeline"]["state"], plan["review"]["state"]), ("unverified", "done"))
        with self.assertRaises(pg.StepError) as e:
            pg.step("all done", kind="done")
        self.assertIn("pipeline", str(e.exception))
        pg.step("pipeline checked", which="pipeline", check="✓ CI run green")
        self.assertEqual(pg.approvals_owed(self.run_()), [("keymaker", ["pipeline"])])
        with self.assertRaises(pg.StepError) as e:
            pg.step("all done", kind="done")
        self.assertIn("pipeline", str(e.exception))
        self.assertEqual(self.run_()["state"], "running")


if __name__ == "__main__":
    unittest.main()
