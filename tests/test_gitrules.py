# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# devkit's own git rules (lib/devkit/gitrules.py): branch names follow `<type>/<ticket>-<topic>` and never name a tool
# or a throwaway marker, a message carries no AI attribution, and devkit's own commit refuses staged secrets. gitleaks
# is replaced by a fake that reports what it was asked; no repository is changed.
# Run: python3 -m unittest tests.test_gitrules -v
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()  # run alone, this module gets the suite's temporary home too (tests/_hermetic.py)
sys.path.insert(0, str(ROOT / "lib"))

from devkit import core, gitrules, security  # noqa: E402


class BranchNameTests(unittest.TestCase):

    def test_names_that_describe_the_change_pass(self):
        for name in ("feat/invoice-export", "feat/PROJ-142-invoice-export", "fix/login-timeout", "release/26.11.4",
                     "release/v2.3.0", "integration/payments", "hotfix/2.3.1", "chore/deps-bump", "main", "develop",
                     "refs/heads/feat/x", "refs/tags/v1", "HEAD", "feat/claude-desktop-parity",
                     "refactor/agent-runtime"):
            with self.subTest(name=name):
                self.assertIsNone(gitrules.branch_violation(name))

    def test_a_tool_marker_a_random_id_or_another_shape_is_refused_with_the_rename(self):
        cases = {"claude/vigilant-jang-ec71c0": "tool/agent or throwaway marker", "codex/fix-thing": "tool/agent",
                 "worktree-bold-oak-a3f2": "tool/agent", "wip": "tool/agent", "my-branch": "does not follow",
                 "feature/x": "does not follow", "feat/": "does not follow", "feat/vigilant-jang-ec71c0": "random",
                 "feat/Invoice_Export": "lowercase kebab-case", "feat/" + "x" * 60: "too long"}
        for name, why in cases.items():
            with self.subTest(name=name):
                reason = gitrules.branch_violation(name)
                self.assertIn(why, reason or "")
                self.assertIn("git branch -m", reason)

    def test_the_configured_types_and_words_replace_the_defaults(self):
        with mock.patch.dict(core.CFG, {"branches": {"types": ["story"], "forbidden_words": ["bob"]}}):
            self.assertIsNone(gitrules.branch_violation("story/login"))
            self.assertIsNotNone(gitrules.branch_violation("feat/login"))
            self.assertIn("does not follow", gitrules.branch_violation("claude/x"))  # no type, no longer a marker
            self.assertIn("tool/agent", gitrules.branch_violation("bob/x"))


class AttributionTests(unittest.TestCase):

    def test_an_ai_attribution_line_in_any_case_is_named(self):
        for text in ("feat: x\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>",
                     "x\n\n🤖 Generated with [Claude Code](https://claude.com)", "x\nco-authored-by: CODEX"):
            with self.subTest(text=text):
                self.assertIn("no AI attribution", gitrules.attribution_problem(text) or "")

    def test_a_message_without_one_passes(self):
        for text in ("fix: handle empty input", "docs: say who reviewed it\n\nCo-Authored-By: Ana <ana@example.com>",
                     ""):
            with self.subTest(text=text):
                self.assertIsNone(gitrules.attribution_problem(text))


class CommitTests(unittest.TestCase):

    def test_attribution_is_named_before_any_scan(self):
        with mock.patch.object(security, "staged_secrets", side_effect=AssertionError("scanned")):
            self.assertIn("no AI attribution", gitrules.commit_problem("/repo", "x\n\nCo-Authored-By: Claude"))

    def test_staged_secrets_are_named_with_what_the_commit_will_hold(self):
        asked = []

        def scan(top, adds=None, all_tracked=False):
            asked.append((top, adds, all_tracked))
            return ["config.py:3 aws-access-token"]

        with mock.patch.object(security, "staged_secrets", scan):
            why = gitrules.commit_problem("/repo", "feat: x", all_tracked=True, adds=[("src",)])
        self.assertIn("config.py:3 aws-access-token", why)
        self.assertIn("gitleaks found secret-like content", why)
        self.assertEqual(asked, [("/repo", [["src"]], True)])

    def test_a_clean_commit_has_no_problem(self):
        with mock.patch.object(security, "staged_secrets", lambda top, adds=None, all_tracked=False: []):
            self.assertIsNone(gitrules.commit_problem("/repo", "feat: x"))


if __name__ == "__main__":
    unittest.main()
