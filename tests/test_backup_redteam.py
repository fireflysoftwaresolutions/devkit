# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# dev backup holds a branch of a public repository that may carry a security fix (backup._security_hint). A red-team
# test is the open gap's description only while it still expects a failure (@unittest.expectedFailure, pytest's
# xfail, test.failing): once the gap is fixed and every red-team test passes, the branch publishes nothing an attacker
# can use, and the backup goes on. The tip of the branch decides, as that is what a push publishes. Tests carry
# comments, not docstrings: `dev test prove` leaves out tests with a docstring.
# Run: python3 -m unittest tests.test_backup_redteam
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import backup, core  # noqa: E402

PASSING = ("import unittest\n\n\nclass TokenReplay(unittest.TestCase):\n"
           "    def test_a_replayed_token_is_refused(self):\n        self.assertEqual(1, 1)\n")
OPEN_GAP = ("import unittest\n\n\nclass TokenReplay(unittest.TestCase):\n    @unittest.expectedFailure  # T-x: still open\n"
            "    def test_a_replayed_token_is_refused(self):\n        self.assertEqual(1, 2)\n")
XFAIL = ("import pytest\n\n\n@pytest.mark.xfail(reason='T-y: the path traversal is still open')\n"
         "def test_upload_refuses_dot_dot():\n    assert False\n")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=dict(core.GIT_ENV)).stdout.strip()


class RedTeamHoldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "T")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "chore: start")
        self.mine = []

    def commit(self, rel, text, msg="test: cover the token replay"):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", msg)
        self.mine.insert(0, git(self.repo, "rev-parse", "HEAD"))  # newest first, as backup lists a branch's commits

    def hint(self):
        return backup._security_hint(str(self.repo), self.mine)

    # the developer's case of 2026-11-06: the branch fixed the gaps and its red-team tests all pass. A commit that
    # reads as a security fix still holds it, whatever its tests
    def test_red_team_tests_that_all_pass_hold_nothing(self):
        self.commit("tests/test_red_team_5.py", OPEN_GAP)
        self.commit("tests/test_red_team_5.py", PASSING, "fix(peers): refuse a replayed token")
        self.commit("tests/test_identity_redteam.py", PASSING)
        self.assertEqual(self.hint(), "")
        self.commit("lib/upload.py", "SAFE = True\n", "fix(security): close the path traversal in uploads")
        self.assertIn("reads as a security fix", self.hint())

    def test_a_red_team_test_that_still_expects_a_failure_holds_the_branch(self):
        for rel, text in (("tests/test_red_team_6.py", OPEN_GAP), ("tests/test_adversarial_upload.py", XFAIL)):
            with self.subTest(rel=rel):
                self.mine = []
                self.commit(rel, text)
                self.commit("lib/other.py", f"x = {rel!r}\n", "feat: something else")
                hint = self.hint()
                self.assertIn("still expects a failure", hint)
                self.assertIn(rel, hint)


if __name__ == "__main__":
    unittest.main()
