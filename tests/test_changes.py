# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Change fragments (devkit.changes): a repository with a `changes/` folder takes its CHANGELOG lines from
`changes/<branch>.md` files, which the release writes into CHANGELOG.md and removes, and a branch that is not a release
branch may not edit CHANGELOG.md itself.

Offline: real temporary repositories with a bare origin (test_gitx's Sandbox); nothing reaches GitHub.
Run: python3 -m unittest tests.test_changes -v"""
import argparse
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import changes, gitx  # noqa: E402
from test_gitx import Sandbox, git  # noqa: E402

PREAMBLE = "# Changelog\n\nAll notable changes. Sections follow Keep a Changelog.\n"
OLD = "## [26.11.27] - 2026-11-20\n\n### Fixed\n\n- An old fix.\n"
EXPORT = "### Fixed\n\n- Exports keep quoted commas.\n\n### Added\n\n- `dev export` writes CSV.\n"
LOGIN = "### Added\n\n- The login page remembers the last user\n  for 30 days.\n"


def release_args(version=None, dry_run=False):
    return argparse.Namespace(version=version, dry_run=dry_run)


class ParseTests(unittest.TestCase):
    def test_entries_go_under_their_sections_with_their_next_lines(self):
        text = "### Added\n\n- `dev x` writes CSV\n  in UTF-8.\n* A second entry\nwith a lazy line.\n\n### fixed\n\n- A fix.\n"
        self.assertEqual(changes.parse(text, "changes/a.md"),
                         {"Added": ["- `dev x` writes CSV\n  in UTF-8.", "- A second entry\n  with a lazy line."],
                          "Fixed": ["- A fix."]})

    def test_a_line_before_the_first_section_names_the_file_and_line(self):
        with self.assertRaises(changes.FragmentError) as e:
            changes.parse("\n- An entry with no section.\n", "changes/a.md")
        self.assertIn("changes/a.md:2:", str(e.exception))
        self.assertIn("before the first section heading", str(e.exception))

    def test_a_heading_that_is_no_keep_a_changelog_section_is_refused(self):
        with self.assertRaises(changes.FragmentError) as e:
            changes.parse("### Added\n\n- One.\n\n### Improvements\n\n- Two.\n", "changes/a.md")
        self.assertIn("changes/a.md:5:", str(e.exception))
        self.assertIn("### Improvements", str(e.exception))

    def test_text_after_a_blank_line_that_is_not_indented_is_outside_any_entry(self):
        with self.assertRaises(changes.FragmentError) as e:
            changes.parse("### Added\n\n- One.\n\nA stray paragraph.\n", "changes/a.md")
        self.assertIn("changes/a.md:5:", str(e.exception))

    def test_a_fragment_without_entries_is_refused(self):
        with self.assertRaises(changes.FragmentError) as e:
            changes.parse("### Added\n\n<!-- nothing yet -->\n", "changes/a.md")
        self.assertIn("no entries", str(e.exception))


class ReadTests(unittest.TestCase):
    """What read() takes from changes/: the fragment files, decoded as strict UTF-8."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.top = Path(tmp.name)
        (self.top / "changes").mkdir()

    def test_a_fragment_that_is_not_utf8_is_refused_with_the_line_of_its_first_bad_byte(self):
        (self.top / "changes" / "feat-cafe.md").write_bytes("### Fixed\n\n- Café export.\n".encode("latin-1"))
        with self.assertRaises(changes.FragmentError) as e:
            changes.read(self.top)
        self.assertEqual((e.exception.where, e.exception.line), ("changes/feat-cafe.md", 3))
        self.assertIn("not UTF-8", e.exception.reason)

    def test_a_byte_order_mark_is_dropped(self):
        (self.top / "changes" / "feat-bom.md").write_bytes(b"\xef\xbb\xbf### Fixed\n\n- Caf\xc3\xa9 export.\n")
        self.assertEqual(changes.read(self.top), [("changes/feat-bom.md", {"Fixed": ["- Café export."]})])


class MergeTests(unittest.TestCase):
    def test_the_new_version_goes_above_the_newest_release_with_sections_in_keep_a_changelog_order(self):
        parts = [changes.parse(EXPORT, "changes/a.md"), changes.parse(LOGIN, "changes/b.md")]
        out = changes.merge(PREAMBLE + "\n" + OLD, "26.11.28", "2026-11-23", parts)
        self.assertEqual(out, PREAMBLE + "\n## [26.11.28] - 2026-11-23\n\n### Added\n\n- `dev export` writes CSV.\n"
                              "- The login page remembers the last user\n  for 30 days.\n\n### Fixed\n\n"
                              "- Exports keep quoted commas.\n\n" + OLD)

    def test_a_section_the_release_already_has_keeps_its_lines_and_takes_the_fragments_after_them(self):
        by_hand = "## [26.11.28] - 2026-11-22\n\nOne release note.\n\n### Fixed\n\n- Written by hand.\n"
        out = changes.merge(PREAMBLE + "\n" + by_hand + "\n" + OLD, "26.11.28", "2026-11-23",
                            [changes.parse(EXPORT, "changes/a.md")])
        self.assertEqual(out, PREAMBLE + "\n## [26.11.28] - 2026-11-22\n\nOne release note.\n\n### Added\n\n"
                              "- `dev export` writes CSV.\n\n### Fixed\n\n- Written by hand.\n"
                              "- Exports keep quoted commas.\n\n" + OLD)

    def test_an_unreleased_section_stays_on_top(self):
        unreleased = "## [Unreleased]\n\n### Added\n\n- Not yet.\n"
        out = changes.merge(PREAMBLE + "\n" + unreleased + "\n" + OLD, "26.11.28", "2026-11-23",
                            [changes.parse(LOGIN, "changes/b.md")])
        self.assertEqual(out, PREAMBLE + "\n" + unreleased + "\n## [26.11.28] - 2026-11-23\n\n### Added\n\n"
                              "- The login page remembers the last user\n  for 30 days.\n\n" + OLD)


class ReleaseBranchTests(unittest.TestCase):
    """What dev release does with fragments: on a release branch it writes them into the version and removes them;
    on the default branch it refuses while one is left."""

    def setUp(self):
        self.s = Sandbox().__enter__()
        self.addCleanup(self.s.__exit__, None, None, None)
        self.s.commit("docs: start the changelog", {"CHANGELOG.md": PREAMBLE + "\n" + OLD,
                                                     "changes/README.md": "# Change fragments\n"})
        git(self.s.repo, "push", "-q", "origin", "main")

    def two_fragments_on(self, branch):
        self.s.branch(branch)
        self.s.commit("feat: export", {"changes/feat-export.md": EXPORT})
        self.s.commit("feat: remember the login", {"changes/feat-login.md": LOGIN})

    def test_a_release_branch_with_two_fragments_writes_both_into_its_version_and_removes_them(self):
        self.two_fragments_on("release/26.11.28")
        rc, err = rc_err(lambda: changes.prepare_release(gitx.ctx(self.s.repo), release_args(), today="2026-11-23"))
        self.assertEqual(rc, 0, err)
        text = (self.s.repo / "CHANGELOG.md").read_text()
        section = text.split("## [26.11.28] - 2026-11-23\n", 1)[1].split("## [26.11.27]", 1)[0]
        self.assertIn("- `dev export` writes CSV.\n- The login page remembers the last user\n  for 30 days.", section)
        self.assertIn("- Exports keep quoted commas.", section)
        self.assertEqual(changes.fragments(self.s.repo), [])
        staged = git(self.s.repo, "status", "--porcelain").splitlines()
        self.assertEqual(sorted(staged), ["D  changes/feat-export.md", "D  changes/feat-login.md",
                                          "M  CHANGELOG.md"])
        self.assertTrue((self.s.repo / "changes" / "README.md").is_file())
        self.assertEqual(gitx._ref_sha(str(self.s.repo), "refs/tags/v26.11.28"), "")

    def test_a_dry_run_shows_the_fragments_and_writes_nothing(self):
        self.two_fragments_on("release/26.11.28")
        before = (self.s.repo / "CHANGELOG.md").read_text()
        out = io_of(lambda: changes.prepare_release(gitx.ctx(self.s.repo), release_args(dry_run=True)))
        self.assertIn("changes/feat-export.md", out)
        self.assertEqual((self.s.repo / "CHANGELOG.md").read_text(), before)
        self.assertEqual(len(changes.fragments(self.s.repo)), 2)

    def test_a_version_that_differs_from_the_release_branch_is_refused(self):
        self.two_fragments_on("release/26.11.28")
        rc, err = rc_err(lambda: changes.prepare_release(gitx.ctx(self.s.repo), release_args(version="26.11.29")))
        self.assertEqual(rc, 2)
        self.assertIn("release/26.11.28", err)
        self.assertEqual(len(changes.fragments(self.s.repo)), 2)

    def test_a_malformed_fragment_is_refused_with_its_file_and_line(self):
        self.s.branch("release/26.11.28")
        self.s.commit("feat: odd", {"changes/feat-odd.md": "- No section.\n"})
        rc, err = rc_err(lambda: changes.prepare_release(gitx.ctx(self.s.repo), release_args()))
        self.assertEqual(rc, 2)
        self.assertIn("changes/feat-odd.md:1:", err)

    def test_a_changelog_that_is_not_utf8_is_refused_and_left_as_it_is(self):
        self.two_fragments_on("release/26.11.28")
        latin1 = (PREAMBLE + "\n## [26.11.27] - 2026-11-20\n\n### Fixed\n\n- Café.\n").encode("latin-1")
        (self.s.repo / "CHANGELOG.md").write_bytes(latin1)
        rc, err = rc_err(lambda: changes.prepare_release(gitx.ctx(self.s.repo), release_args()))
        self.assertEqual(rc, 2, err)
        self.assertIn("CHANGELOG.md is not UTF-8", err)
        self.assertEqual((self.s.repo / "CHANGELOG.md").read_bytes(), latin1)
        self.assertEqual(len(changes.fragments(self.s.repo)), 2)

    def test_a_changelog_with_a_byte_order_mark_keeps_it(self):
        self.two_fragments_on("release/26.11.28")
        (self.s.repo / "CHANGELOG.md").write_bytes(b"\xef\xbb\xbf" + (PREAMBLE + "\n" + OLD).encode())
        rc, err = rc_err(lambda: changes.prepare_release(gitx.ctx(self.s.repo), release_args(), today="2026-11-23"))
        self.assertEqual(rc, 0, err)
        data = (self.s.repo / "CHANGELOG.md").read_bytes()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf# Changelog\n"), data[:20])
        self.assertEqual(data.count(b"\xef\xbb\xbf"), 1)
        self.assertIn(b"## [26.11.28] - 2026-11-23", data)

    def test_the_default_branch_refuses_to_release_while_a_fragment_is_left(self):
        self.s.commit("feat: export", {"changes/feat-export.md": EXPORT})
        reason, fix = changes.release_refusal(str(self.s.repo), "26.11.28")
        self.assertIn("1 change fragment(s) are not in CHANGELOG.md yet: changes/feat-export.md", reason)
        self.assertIn("release/26.11.28", fix)

    def test_the_folder_readme_is_no_fragment(self):
        self.assertIsNone(changes.release_refusal(str(self.s.repo), "26.11.28"))


class DirectEditTests(unittest.TestCase):
    """What dev ci --run fails: a CHANGELOG.md edit on a branch that is not a release branch, or a fragment it cannot
    read; only in a repository that keeps a changes/ folder."""

    def setUp(self):
        self.s = Sandbox().__enter__()
        self.addCleanup(self.s.__exit__, None, None, None)
        self.s.commit("docs: start the changelog", {"CHANGELOG.md": PREAMBLE + "\n" + OLD,
                                                     "changes/README.md": "# Change fragments\n"})
        git(self.s.repo, "push", "-q", "origin", "main")

    def test_a_feature_branch_that_commits_a_changelog_edit_fails_with_the_commit_and_the_fragment_to_use(self):
        self.s.branch("feat/export")
        self.s.commit("feat: export and its changelog line", {"CHANGELOG.md": PREAMBLE + "\n- direct\n\n" + OLD})
        reason, detail = changes.ci_problem(str(self.s.repo), "feat/export")
        self.assertIn("CHANGELOG.md is changed on feat/export", reason)
        self.assertIn("feat: export and its changelog line", detail)
        self.assertIn("changes/feat-export.md", detail)

    def test_an_uncommitted_changelog_edit_fails_too(self):
        self.s.branch("fix/login")
        self.s.write("CHANGELOG.md", PREAMBLE + "\n- direct\n\n" + OLD)
        reason, detail = changes.ci_problem(str(self.s.repo), "fix/login")
        self.assertIn("CHANGELOG.md is changed on fix/login", reason)
        self.assertIn("uncommitted", detail)

    def test_a_feature_branch_with_its_fragment_passes(self):
        self.s.branch("feat/export")
        self.s.commit("feat: export", {"changes/feat-export.md": EXPORT})
        self.assertIsNone(changes.ci_problem(str(self.s.repo), "feat/export"))

    def test_a_release_branch_may_edit_the_changelog(self):
        self.s.branch("release/26.11.28")
        self.s.commit("chore(release): 26.11.28", {"CHANGELOG.md": PREAMBLE + "\n## [26.11.28]\n\n" + OLD})
        self.assertIsNone(changes.ci_problem(str(self.s.repo), "release/26.11.28"))

    def test_a_branch_cut_from_a_release_branch_is_judged_by_its_own_commits(self):
        self.s.branch("release/26.11.28")
        self.s.commit("chore(release): 26.11.28", {"CHANGELOG.md": PREAMBLE + "\n## [26.11.28]\n\n" + OLD})
        self.s.branch("fix/late")
        self.s.commit("fix: late", {"changes/fix-late.md": "### Fixed\n\n- Late.\n"})
        self.assertIsNone(changes.ci_problem(str(self.s.repo), "fix/late"))

    def test_a_malformed_fragment_fails_with_its_file_and_line(self):
        self.s.branch("feat/odd")
        self.s.commit("feat: odd", {"changes/feat-odd.md": "### Added\n\n- One.\n\n### Notes\n\n- Two.\n"})
        reason, _ = changes.ci_problem(str(self.s.repo), "feat/odd")
        self.assertIn("changes/feat-odd.md:5:", reason)

    def test_a_repository_without_a_changes_folder_keeps_editing_its_changelog(self):
        git(self.s.repo, "rm", "-q", "-r", "changes")
        git(self.s.repo, "commit", "-q", "-m", "chore: no fragments")
        git(self.s.repo, "push", "-q", "origin", "main")
        self.s.branch("feat/export")
        self.s.commit("feat: export", {"CHANGELOG.md": PREAMBLE + "\n- direct\n\n" + OLD})
        self.assertIsNone(changes.ci_problem(str(self.s.repo), "feat/export"))


def io_of(fn) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        fn()
    return out.getvalue()


def rc_err(fn):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn()
    return rc, err.getvalue()


if __name__ == "__main__":
    unittest.main()
