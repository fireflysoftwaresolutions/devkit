# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev release` and `dev ci --run` in a repository that keeps change fragments (a `changes/` folder): on a release
branch dev release writes the fragments into the version's CHANGELOG section and removes them; on the default branch
it refuses while one is left; dev ci --run fails a branch that edits CHANGELOG.md outside a release branch.

Offline: real temporary repositories with a bare origin, gh faked (test_gitx's Sandbox).
Run: python3 -m unittest tests.test_release_fragments -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import changes, gitx  # noqa: E402
from test_gitx import CI_ON_PUSH, Sandbox, dev, git  # noqa: E402

CHANGELOG = "# Changelog\n\n## [26.11.27] - 2026-11-20\n\n### Fixed\n\n- An old fix.\n"
EXPORT = "### Added\n\n- `dev export` writes CSV.\n"
LOGIN = "### Fixed\n\n- The login page keeps the last user.\n"


class ReleaseFragmentsTests(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox().__enter__()
        self.addCleanup(self.s.__exit__, None, None, None)
        self.s.commit("docs: keep change fragments", {"CHANGELOG.md": CHANGELOG, "changes/README.md": "# Fragments\n",
                                                       **CI_ON_PUSH})
        git(self.s.repo, "push", "-q", "origin", "main")

    def test_dev_release_on_a_release_branch_with_two_fragments_writes_both_into_the_version_and_removes_them(self):
        self.s.branch("release/26.11.28")
        self.s.commit("feat: export", {"changes/feat-export.md": EXPORT})
        self.s.commit("fix: keep the login", {"changes/fix-login.md": LOGIN})
        rc, out, err = dev("release")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("wrote 2 change fragment(s) into CHANGELOG.md [26.11.28]", out)
        section = (self.s.repo / "CHANGELOG.md").read_text().split("## [26.11.28] - ", 1)[1].split("## [26.11.27]")[0]
        self.assertIn("### Added\n\n- `dev export` writes CSV.\n", section)
        self.assertIn("### Fixed\n\n- The login page keeps the last user.\n", section)
        self.assertEqual(changes.fragments(self.s.repo), [])
        self.assertEqual(git(self.s.repo, "tag", "--list"), "")

    def test_dev_release_on_the_default_branch_refuses_while_a_fragment_is_left(self):
        self.s.commit("feat: export", {"changes/feat-export.md": EXPORT})
        git(self.s.repo, "push", "-q", "origin", "main")
        rc, out, err = dev("release")
        self.assertEqual(rc, 2, out + err)
        self.assertIn("1 change fragment(s) are not in CHANGELOG.md yet: changes/feat-export.md", err)
        self.assertEqual(git(self.s.repo, "tag", "--list"), "")

    def test_dev_ci_run_fails_a_feature_branch_that_edits_the_changelog_until_it_restores_it(self):
        self.s.branch("feat/export")
        self.s.commit("feat: export", {"CHANGELOG.md": CHANGELOG.replace("### Fixed", "### Added\n\n- Export.\n\n"
                                                                                     "### Fixed")})
        rc, out, err = dev("ci", "--run")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("CHANGELOG.md is changed on feat/export", err)
        self.assertIn("changes/feat-export.md", err)
        self.assertIsNone(gitx.evidence(self.s.ctx()))
        self.s.commit("docs: move the line to a fragment", {"CHANGELOG.md": CHANGELOG,
                                                             "changes/feat-export.md": EXPORT})
        rc, out, err = dev("ci", "--run")
        self.assertEqual(rc, 0, out + err)


if __name__ == "__main__":
    unittest.main()
