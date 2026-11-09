# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# devkit: allow-stub-words (this file tests the words a root cause may not be)
#
# `dev commit --cause`: a fix or a revert names its root cause, as a `Root cause:` paragraph right after the why. A
# fix or revert commit without one is refused and the refusal names the flag; a placeholder (unknown, n/a, tbd, -) or
# anything shorter than 15 characters is no cause; and the audit log records that the commit named one, never the
# text. Real temporary repositories (tests/test_gitx.py's sandbox). Tests carry comments, not docstrings: `dev test
# prove` leaves out tests with a docstring. Run: python3 -m unittest tests.test_commit_cause
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import audit  # noqa: E402
from test_gitx import Sandbox, dev, git  # noqa: E402

CAUSE = "the cache key ignored the version"


class CommitCauseTests(unittest.TestCase):
    def test_a_fix_or_revert_without_a_cause_is_refused_and_nothing_is_committed(self):
        for header in ("fix(cache): key prices on the version", "fix(api)!: drop the stale price field",
                       "revert: restore the old price cache"):
            with self.subTest(header=header), Sandbox() as s:
                s.branch("fix/price-cache")
                s.write("prices.py", "KEY = 'v2'\n")
                before = s.head()
                rc, _, err = dev("commit", "-m", header, "prices.py")
                self.assertEqual(rc, 2)
                self.assertIn("--cause", err)
                self.assertEqual(s.head(), before)
                self.assertEqual(git(s.repo, "diff", "--cached", "--name-only"), "")

    def test_the_root_cause_is_its_own_paragraph_right_after_the_why(self):
        with Sandbox() as s:
            s.branch("fix/price-cache")
            s.write("prices.py", "KEY = 'v2'\n")
            rc, out, err = dev("commit", "-m", "fix(cache): key prices on the version", "prices.py",
                               "--why", "Shoppers saw old prices for an hour after each release.",
                               "--cause", CAUSE, "--body", "The cache key now includes VERSION.",
                               "--verified", "python3 -m unittest tests.test_prices (4 tests pass)")
            self.assertEqual(rc, 0, err)
            body = git(s.repo, "log", "-1", "--format=%B").strip()
            self.assertEqual(body.split("\n\n"), [
                "fix(cache): key prices on the version",
                "Shoppers saw old prices for an hour after each release.",
                f"Root cause: {CAUSE}",
                "The cache key now includes VERSION.",
                "Verified: python3 -m unittest tests.test_prices (4 tests pass)"])

    def test_a_cause_already_labeled_is_not_labeled_twice_and_any_commit_type_may_name_one(self):
        with Sandbox() as s:
            s.branch("feat/price-cache")
            s.write("prices.py", "KEY = 'v2'\n")
            rc, _, err = dev("commit", "-m", "perf(cache): keep one entry per version", "prices.py",
                             "--cause", f"Root cause: {CAUSE}")
            self.assertEqual(rc, 0, err)
            self.assertEqual(git(s.repo, "log", "-1", "--format=%B").strip().split("\n\n"),
                             ["perf(cache): keep one entry per version", f"Root cause: {CAUSE}"])

    def test_a_placeholder_or_a_cause_under_15_characters_is_refused(self):
        for cause, says in (("tbd", "placeholder"), ("Unknown", "placeholder"), ("n/a", "placeholder"),
                            ("-", "placeholder"), ("TBD.", "placeholder"), ("   ", "empty"),
                            ("the key is old", "15 characters")):
            with self.subTest(cause=cause), Sandbox() as s:
                s.branch("fix/price-cache")
                s.write("prices.py", "KEY = 'v2'\n")
                before = s.head()
                rc, _, err = dev("commit", "-m", "fix(cache): key prices on the version", "prices.py",
                                 "--cause", cause)
                self.assertEqual(rc, 2)
                self.assertIn(says, err)
                self.assertIn("--cause", err)
                self.assertEqual(s.head(), before)

    def test_a_cause_of_exactly_15_characters_is_enough(self):
        with Sandbox() as s:
            s.branch("fix/price-cache")
            s.write("prices.py", "KEY = 'v2'\n")
            rc, _, err = dev("commit", "-m", "fix(cache): key prices on the version", "prices.py",
                             "--cause", "the key was old")
            self.assertEqual(rc, 0, err)
            self.assertIn("Root cause: the key was old", git(s.repo, "log", "-1", "--format=%B"))

    def test_a_cause_without_a_message_is_refused(self):
        with Sandbox() as s:
            s.branch("fix/price-cache")
            s.write("prices.py", "KEY = 'v2'\n")
            rc, _, err = dev("commit", "--cause", CAUSE, "prices.py")
            self.assertEqual(rc, 2)
            self.assertIn("--cause", err)

    def test_the_audit_log_records_that_the_commit_named_its_cause_but_not_the_cause(self):
        with Sandbox() as s:
            s.branch("fix/price-cache")
            s.write("prices.py", "KEY = 'v2'\n")
            rc, _, err = dev("commit", "-m", "fix(cache): key prices on the version", "prices.py", "--cause", CAUSE)
            self.assertEqual(rc, 0, err)
            recs = audit.query(days=1, action="dev.commit")
            self.assertEqual(len(recs), 1, recs)
            self.assertEqual(recs[0]["evidence"]["check"], "cause: yes")
            self.assertEqual(recs[0]["evidence"]["commit"], s.head())
            self.assertNotIn("cache key", str(recs[0]))


if __name__ == "__main__":
    unittest.main()
