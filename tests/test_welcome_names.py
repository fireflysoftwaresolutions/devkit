# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The welcome names a project by its repository name. When another known project has the same repository name, the
# project's id disambiguates it, cut from the left so the repository name stays whole; a name no other project has
# stays whole; and outside a project, a run of another session keeps "in another session" whatever the name takes.
# Run: python3 -m unittest tests.test_welcome_names
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import welcome  # noqa: E402
from test_welcome import NOW, OTHER, ROSTER, SID, Sandbox  # noqa: E402

MINE, THEIRS = "fireflysoftwaresolutions/devkit", "signature-organization/devkit"


class SharedNames(Sandbox):
    def two_devkits(self):
        mine = self.repo(name="devkit", pid=MINE, branch="main", modified=0)
        return mine, self.repo(name="devkit", pid=THEIRS, branch="main", modified=0)

    def test_a_shared_repository_name_keeps_the_name_and_cuts_the_owner_from_the_left(self):
        mine, theirs = self.two_devkits()
        lines = self.lines(repo=mine, reg=self.reg(mine, theirs))
        self.assertEqual(lines[0], "devkit · …solutions/devkit · nothing needs you")
        self.assertWellFormed(lines)

    def test_outside_a_project_names_keep_their_repository_name_and_runs_their_other_session_segment(self):
        mine, theirs = self.two_devkits()
        portal = self.repo(name="customer-portal-billing", pid="acme/customer-portal-billing")  # its own name
        run = self.make_run(owner=OTHER, project=MINE, goal="Hotfix 26.11.7: watch manager live", members=("Tank",))
        asks = self.make_run(owner=OTHER, project=portal["id"], decisions=[self.decision()])
        lines = welcome.text("claude", "startup", str(self.root), data={"session_id": SID}, repo=None, top="",
                             reg=self.reg(mine, theirs, portal), runs=[run, asks], roster=ROSTER,
                             now=NOW).splitlines()
        self.assertTrue(lines[1].startswith("! customer-portal-billing · decision waits in another session: "),
                        lines[1])
        line = next(ln for ln in lines if ln.startswith("▸ "))
        self.assertTrue(line.endswith(" · step 3 of 7 · in another session"), line)
        self.assertIn("/devkit · Hotfix 26.11.7", line)
        self.assertWellFormed(lines)


if __name__ == "__main__":
    unittest.main()
