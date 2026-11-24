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

    def test_a_second_dev_release_on_the_release_branch_says_to_commit_the_staged_changelog(self):
        self.s.branch("release/26.11.28")
        self.s.commit("feat: export", {"changes/feat-export.md": EXPORT})
        self.assertEqual(dev("release")[0], 0)
        rc, out, err = dev("release")
        self.assertEqual(rc, 2, out + err)
        self.assertIn("no change fragments left on release/26.11.28: commit the staged CHANGELOG.md", err)
        self.assertIn("chore(release): 26.11.28", err)
        self.assertNotIn("releases are cut from", err)

    def test_dev_release_on_a_release_branch_without_fragments_says_where_it_tags(self):
        self.s.branch("release/26.11.28")
        rc, out, err = dev("release")
        self.assertEqual(rc, 2, out + err)
        self.assertIn("no change fragments on release/26.11.28 to write into CHANGELOG.md", err)
        self.assertIn("dev pr merge", err)

    def test_dev_release_on_a_release_branch_named_after_a_tagged_version_refuses_and_writes_nothing(self):
        git(self.s.repo, "tag", "v26.11.27")
        git(self.s.repo, "push", "-q", "origin", "--tags")
        for branch, args, only_on_origin in (("release/26.11.27", (), False), ("hotfix/26.11.27", (), True),
                                             ("hotfix/login", ("--version", "26.11.27"), False)):
            with self.subTest(branch=branch, only_on_origin=only_on_origin):
                git(self.s.repo, "reset", "-q", "--hard")
                git(self.s.repo, "switch", "-q", "main")
                self.s.branch(branch)
                self.s.commit("fix: keep the login", {"changes/fix-login.md": LOGIN})
                if only_on_origin:  # another clone released it: dev release fetches the tags before it checks
                    git(self.s.repo, "tag", "-d", "v26.11.27")
                    self.assertEqual(git(self.s.repo, "tag", "--list"), "")
                rc, out, err = dev("release", *args)
                self.assertEqual(rc, 2, out + err)
                self.assertIn("tag v26.11.27 already exists", err)
                self.assertIn("next version: ", err)
                self.assertEqual((self.s.repo / "CHANGELOG.md").read_text(), CHANGELOG)
                self.assertEqual(changes.fragments(self.s.repo), ["changes/fix-login.md"])
                self.assertEqual(git(self.s.repo, "status", "--porcelain"), "")

    def test_dev_release_on_the_default_branch_refuses_a_version_that_is_already_tagged(self):
        git(self.s.repo, "tag", "v26.11.27")
        git(self.s.repo, "push", "-q", "origin", "--tags")
        rc, out, err = dev("release", "--version", "26.11.27", "--dry-run")
        self.assertEqual(rc, 2, out + err)
        self.assertIn("tag v26.11.27 already exists", err)
        self.assertIn("next version: ", err)

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

    def test_dev_ci_run_dry_run_plans_the_steps_and_warns_about_the_changelog_edit_it_would_fail(self):
        self.s.branch("feat/export")
        self.s.commit("feat: export", {"CHANGELOG.md": CHANGELOG + "\n- Export.\n"})
        rc, out, err = dev("ci", "--run", "--dry-run")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("dev ci --run would fail before any step: CHANGELOG.md is changed on feat/export", err)
        self.assertIn("changes/feat-export.md", err)
        self.assertIn("Local CI plan", out)
        self.assertIsNone(gitx.evidence(self.s.ctx()))

    def test_dev_ci_mark_passed_records_no_evidence_for_a_branch_that_edits_the_changelog(self):
        self.s.branch("feat/export")
        self.s.commit("feat: export", {"CHANGELOG.md": CHANGELOG + "\n- Export.\n"})
        rc, out, err = dev("ci", "--mark-passed", "--note", "make test: 12 passed")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("CHANGELOG.md is changed on feat/export", err)
        self.assertIsNone(gitx.evidence(self.s.ctx()))


if __name__ == "__main__":
    unittest.main()
