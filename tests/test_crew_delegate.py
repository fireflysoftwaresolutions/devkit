# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# `dev crew delegate` gives the new step to its member by an intent, and a free working lane of that member claims
# it through the claim rule (coord.claim), so the step starts with its history entry and the run keeps every
# coordination rule (coord.check). Progress state in a temporary folder (test_progress.TempState).
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_crew_delegate -v
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import coord, dispatch  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_progress import TempState, launch  # noqa: E402


class DelegateClaimTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.t0 = time.time() - 3600

    def m(self, k):
        return self.t0 + k * 60

    def test_a_step_delegated_into_a_free_lane_starts_by_the_claim_rule_with_its_history(self):
        pg.start("Export", plan=["api=30m", "docs=15m"], now=self.m(0))
        pg.assign("api", "Tank", why="the api", now=self.m(0))
        launch("a_tank", "devkit:tank", "Tank: api", at=self.m(1))
        launch("a_neo", "devkit:neo", "Neo: look", at=self.m(1))
        pg.step("api done", which="api", check="✓ 3 tests pass", from_agent="a_tank", now=self.m(5))
        out = dispatch.delegate("write the parser", to="Tank", from_agent="a_neo", scope="parser/**", now=self.m(6))
        run = pg.load_run("claude", "s1")
        st = next(s for s in run["plan"] if s.get("task") == "write the parser")
        self.assertEqual((out["lane"], st.get("owner"), st.get("state")), ("a_tank", "a_tank", "running"))
        last = [h for h in run.get("history") or [] if h.get("step") == st["name"]][-1]
        self.assertEqual((last.get("from"), last.get("to")), ("pending", "running"))
        self.assertNotIn("intent", st)
        self.assertEqual(pg.lane_scope(run, "a_tank"), [["parser/**"]])
        self.assertEqual(coord.check(run, time.time(), review=pg.is_review_step), [])
        # one notice tells the lane: the delegation's, with who delegates and how to decline, not the claim's too
        told = [x.get("text") or "" for x in run.get("messages") or [] if x.get("to") == "a_tank"]
        self.assertEqual(len(told), 1, told)
        self.assertIn("delegates “write the parser”", told[0])


if __name__ == "__main__":
    unittest.main()
