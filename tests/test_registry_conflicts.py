# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The files a handoff sync set aside after a conflict, as registry issues: one issue per file, named by what it is
(a project's note, an initiative's note, a lesson, a plan, a work item, another file), and opening with its project's
id when it has one, so that project's `dev status` finds its own. devkit's state is a temporary folder. Run:
python3 -m unittest tests.test_registry_conflicts -v"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core, registry  # noqa: E402

SET_ASIDE = ["projects/acme/api.20261109-120000.mine.md", "projects/acme/api.20261110-090000.mine.md",
             "projects/acme/api-gateway.20261109-120000.mine.md", "initiatives/billing/README.20261109-120000.mine.md",
             "lessons/L-gjq5gzdxbuu.20261109-120000.mine.md",
             "plans/acme/api/2026-11-05-invoice-export.20261109-120000.mine.md",
             "items/acme/api/T-ih0001abcdef.20261109-120000-2.mine.md", "crew/crew.json.20261109-120000.mine.md"]


class SetAsideIssueTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for target in (mock.patch.object(core, "STATE", Path(tmp.name)),
                       mock.patch.object(registry, "STATE", Path(tmp.name))):
            target.start()
            self.addCleanup(target.stop)
        self.folder = Path(tmp.name) / "handoff-conflicts"
        for rel in SET_ASIDE:
            (self.folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.folder / rel).write_text("mine\n")
            (self.folder / rel.replace(".mine.md", ".base.md")).write_text("base\n")

    def heads(self):
        issues = registry.handoff_conflict_issues()
        self.assertEqual({kind for kind, _ in issues}, {"handoff"})
        return [text.split(" was set aside after a sync conflict: ")[0] for _, text in issues]

    def test_each_set_aside_file_has_its_own_issue_named_by_its_kind(self):
        self.assertEqual(self.heads(), [
            "crew/crew.json in the handoff repository", "the note of initiative billing",
            "acme/api: its work item T-ih0001abcdef", "lesson L-gjq5gzdxbuu",
            "acme/api: its plan 2026-11-05-invoice-export", "acme/api: its handoff note",
            "acme/api-gateway: its handoff note"])

    def test_an_issue_opens_with_its_project_by_whole_name(self):
        issues = [text for _, text in registry.handoff_conflict_issues()]
        own = [t for t in issues if t.startswith("acme/api: ")]
        self.assertEqual(len(own), 3)
        self.assertFalse(any("acme/api-gateway" in t for t in own))

    def test_a_note_set_aside_twice_points_at_its_newest_copy_and_counts_the_older_one(self):
        notes = [t for _, t in registry.handoff_conflict_issues() if t.startswith("acme/api: its handoff note")]
        self.assertEqual(len(notes), 1)
        self.assertIn(f": {core.tilde(self.folder / SET_ASIDE[1])} and 1 older copy (git merge-file", notes[0])


if __name__ == "__main__":
    unittest.main()
