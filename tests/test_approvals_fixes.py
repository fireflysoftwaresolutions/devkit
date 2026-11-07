# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Review batch 3 (Seraph, M244) on approvals.py: an approval needs an explicit check mark, and a step with no finish
# time takes no approval, because none can be shown to come after the work. Run: python3 -m unittest tests.test_approvals_fixes
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import approvals as ap, crew  # noqa: E402

ROSTER = crew.load(team=False)  # devkit's own roster, never the team's changes in the developer's handoff repository
T = 1_800_000_000.0  # a fixed clock
LANES = {"a-tank": "Tank", "a-seraph": "Seraph"}


def step(name, lane, activity, finished=T, after=()):
    return {"name": name, "state": "done", "owner": lane, "activity": activity, "started": T - 600,
            "finished": finished, "after": list(after)}


def run_of(*steps):
    return {"v": 2, "agent": "claude", "session": "s1", "goal": "Release", "state": "running", "plan": list(steps),
            "agents": {aid: {"persona": who, "state": "done", "updates": [], "started": T - 3600}
                       for aid, who in LANES.items()}, "intervals": []}


class ExplicitVerdictTests(unittest.TestCase):
    def test_a_review_check_without_a_check_mark_approves_nothing(self):
        # "tests not run" read as ✓, so a review that says nothing was checked approved the work
        run = run_of(step("api", "a-tank", "backend"),
                     step("review api", "a-seraph", "review", finished=T + 60, after=["api"]))
        self.assertIsNone(ap.from_review(run, run["plan"][1], "tests not run", T + 60, roster=ROSTER))
        self.assertEqual(ap.owed(run, roster=ROSTER), [("seraph", ["api"])])
        with self.assertRaises(ap.ApprovalError):
            ap.verdict_of("read the diff")
        self.assertEqual((ap.verdict_of("✓ read the diff"), ap.verdict_of("⚠ two tests skipped")),
                         ("approved", "changes"))


class FinishTimeTests(unittest.TestCase):
    def test_a_step_without_a_finish_time_does_not_take_an_older_approval(self):
        # an approval Seraph gave long ago counted for a done step whose finish time is missing
        run = run_of(step("api", "a-tank", "backend", finished=None))
        run["approvals"] = [{"ts": T - 86400, "by": "seraph", "steps": ["api"], "verdict": "approved", "via": "step"}]
        self.assertEqual(ap.owed(run, roster=ROSTER), [("seraph", ["api"])])

    def test_an_approval_moments_after_the_finish_counts(self):
        # Tank, wiring F6: the approval's time was rounded to 0.1 s, so one 10 ms after the finish fell before it
        run = run_of(step("api", "a-tank", "backend", finished=T + 0.03))
        ap.record(run, "seraph", ["api"], "approved", T + 0.04, roster=ROSTER)
        self.assertEqual(ap.owed(run, roster=ROSTER), [])

    def test_an_approval_of_a_step_without_a_finish_time_is_refused_with_the_way_out(self):
        run = run_of(step("api", "a-tank", "backend", finished=None))
        with self.assertRaisesRegex(ap.ApprovalError, "no finish time.*dev progress reopen"):
            ap.record(run, "seraph", ["api"], "approved", T + 60, roster=ROSTER)
        self.assertNotIn("approvals", run)


class RecoveryTests(unittest.TestCase):
    def test_a_lane_seen_moments_after_it_was_lost_works_again(self):
        # Tank, wiring F6: lost_at was rounded up to the next 0.1 s, so a hook 20 ms after the loss did not count
        run = run_of(step("api", "a-tank", "backend", finished=None))
        run["plan"][0]["state"] = "running"
        lane = run["agents"]["a-tank"]
        lane.update(state="running", started=T - 9000)
        lost_at = T + 0.06
        made = ap.apply(run, ap.reconcile(run, {"lanes": {"a-tank": {"last_seen": T - 9000}}}, lost_at), lost_at)
        self.assertEqual(lane["state"], "lost", made)
        moves = ap.reconcile(run, {"lanes": {"a-tank": {"last_seen": lost_at + 0.02}}}, lost_at + 0.02)
        ap.apply(run, moves, lost_at + 0.02)
        self.assertEqual((lane["state"], run["plan"][0]["state"]), ("running", "running"))


if __name__ == "__main__":
    unittest.main()
