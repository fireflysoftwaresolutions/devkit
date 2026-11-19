# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev crew delegate` to a member whose lane still works on a step follows the busy-lane queue, as `dev progress
assign`, `reopen` and `dev crew next` do: the delegated step waits, joins that lane once its current step closes, and
takes its own scope then, instead of starting at once with its files merged into the lane's scope. devkit's state is
a temporary folder (test_progress.TempState). Run: python3 -m unittest tests.test_dispatch_busy_lane -v"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import dispatch  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_dispatch import launch  # noqa: E402
from test_progress import TempState  # noqa: E402


class DelegateToBusyLaneTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.t0 = time.time() - 3600
        pg.start("Parser fixes", plan=["launcher-depth=30m"], now=self.m(0))
        pg.assign("launcher-depth", "Neo", scope="lib/devkit/shellparse.py", why="test", now=self.m(0))
        launch("a_neo", "devkit:neo", "Neo: launcher-depth parser timing fix", at=self.m(1))
        launch("a_dozer", "devkit:dozer", "Dozer: schema", at=self.m(1))

    def m(self, minutes):
        return self.t0 + minutes * 60

    def held(self, name):
        st = next(s for s in pg.load_run("claude", "s1")["plan"] if s["name"] == name)
        return st.get("owner"), st.get("state")

    def scope(self):
        return pg.load_run("claude", "s1")["agents"]["a_neo"].get("scope")

    def test_a_step_delegated_to_a_busy_member_joins_its_lane_with_its_own_scope_once_its_step_closes(self):
        name = dispatch.delegate("parse the CSV columns", to="Neo", from_agent="a_dozer",
                                 scope="lib/devkit/csvparse.py", now=self.m(5))["step"]["name"]
        self.assertNotEqual(self.held(name), ("a_neo", "running"), "it started while the lane works on another step")
        self.assertEqual(self.scope(), ["lib/devkit/shellparse.py"])
        pg.step("launcher-depth committed", which="launcher-depth", check="✓ 46 parser test files pass",
                from_agent="a_neo", now=self.m(10))
        self.assertEqual(self.held(name), ("a_neo", "running"))
        self.assertEqual(self.scope(), ["lib/devkit/csvparse.py"])


if __name__ == "__main__":
    unittest.main()
