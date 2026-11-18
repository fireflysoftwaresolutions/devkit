# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# `dev ci status --watch`: it follows the run of the commit the branch is at, not whatever run is newest.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_cli_ci
import argparse
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TREE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TREE / "lib"))

from devkit import cistatus, cli  # noqa: E402


def status_args(**kw) -> argparse.Namespace:
    base = dict(all_branches=False, branch=None, limit=5, watch=True, json=False, all_jobs=False, timeout=90)
    base.update(kw)
    return argparse.Namespace(**base)


def run(sha, status, rid):
    return {"id": rid, "sha": sha, "status": status, "workflow": "CI", "branch": "main", "event": "push",
            "title": "merge", "jobs": []}


class StatusWatchTests(unittest.TestCase):
    # Right after a merge the newest listed run is the branch's older, finished one: --watch saw nothing running and
    # ended at once, before the merge commit's run appeared
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.top = tmp.name
        git = ["git", "-C", self.top, "-c", "user.email=t@example.com", "-c", "user.name=t"]
        for args in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "merge"]):
            subprocess.run(git + args, check=True, capture_output=True)
        self.head = subprocess.run(git + ["rev-parse", "HEAD"], check=True, capture_output=True,
                                   text=True).stdout.strip()[:7]
        self.clock = [1000.0]
        for patch in (mock.patch.object(cli.time, "sleep", lambda s: self.clock.__setitem__(0, self.clock[0] + s)),
                      mock.patch.object(cli.time, "time", lambda: self.clock[0])):
            patch.start()
            self.addCleanup(patch.stop)

    def watch(self, answers, **kw):
        calls = []

        def runs_for(top, branch, limit=5, detail=True, max_age=20):
            calls.append(branch)
            return {"provider": "github", "repo": "acme/api", "branch": branch,
                    "runs": answers[min(len(calls), len(answers)) - 1]}
        out = io.StringIO()
        with mock.patch.object(cistatus, "runs_for", runs_for), contextlib.redirect_stdout(out):
            rc = cli.cmd_ci_status(status_args(**kw), self.top)
        return rc, len(calls), out.getvalue()

    def test_watch_waits_for_the_run_of_the_branchs_commit(self):
        older = run("0ld0000", "success", 1)
        rc, calls, _ = self.watch([[older], [run(self.head, "running", 2), older],
                                   [run(self.head, "success", 2), older]])
        self.assertEqual((rc, calls), (0, 3))

    def test_watch_says_so_when_no_run_of_the_commit_appears(self):
        rc, calls, out = self.watch([[run("0ld0000", "success", 1)]])
        self.assertEqual(rc, 4)
        self.assertIn(f"no run of {self.head}", out)
        self.assertGreater(calls, 1)

    def test_watch_ends_when_the_commits_run_finished_already(self):
        rc, calls, _ = self.watch([[run(self.head, "failed", 2)]])
        self.assertEqual((rc, calls), (1, 1))


class ChecksErrorTests(unittest.TestCase):
    # dev status cut the CI host's error at 80 characters, mid-word ("…to read the reposit")
    def test_a_long_error_is_cut_at_a_word(self):
        error = ("HTTP 403: Resource not accessible by personal access token: the token lacks the actions scope "
                 "needed to read the repository's workflow runs")
        view = cli._checks_view({"runs": [], "error": error, "provider": "github"}, {"branch": "main"}, None)
        said = view["text"].split("could not read the checks: ", 1)[1]
        self.assertTrue(said.endswith("…"), said)
        self.assertTrue(error.startswith(said[:-1].rstrip()), said)
        self.assertIn(said[:-1].rstrip().rsplit(" ", 1)[-1], error.split())  # its last word is a whole word
        self.assertLessEqual(len(said), 80)

    def test_a_short_error_is_kept_whole(self):
        view = cli._checks_view({"runs": [], "error": "HTTP 401: bad credentials", "provider": "github"},
                                {"branch": "main"}, None)
        self.assertTrue(view["text"].endswith("could not read the checks: HTTP 401: bad credentials"))


if __name__ == "__main__":
    unittest.main()
