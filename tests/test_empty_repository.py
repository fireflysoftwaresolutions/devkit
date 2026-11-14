# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Initial commits with an empty GitHub origin, using real Git and offline API responses."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import _sandbox
from tests.test_gitx import FakeGH, ISOLATE, dev, git
from devkit import core, gitx, guard


class EmptyRepositoryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.repo = Path(folder.name).resolve()
        env = _sandbox.env(**ISOLATE)
        patch = mock.patch.dict(core.GIT_ENV, env)
        patch.start()
        self.addCleanup(patch.stop)
        self.gh = FakeGH()
        patch = mock.patch.object(gitx, "GH", self.gh)
        patch.start()
        self.addCleanup(patch.stop)
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        git(self.repo, "init", "-q", "-b", "chore/bootstrap")
        git(self.repo, "remote", "add", "origin", "https://github.com/acme/empty.git")
        os.chdir(self.repo)
        (self.repo / "README.md").write_text("# Empty repository\n")

    def test_first_and_followup_commits_use_declared_default_not_local_branch(self):
        self.gh.on(["api", "repos/acme/empty"], {"full_name": "acme/empty", "default_branch": "main"})
        # A valid name alone cannot fix the bug: without origin refs every local name became the default.
        self.assertIsNone(guard.branch_violation("chore/bootstrap"))
        rc, _, err = dev("commit", "-m", "chore: initialize repository", "README.md")
        self.assertEqual(rc, 0, err)
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s"), "chore: initialize repository")
        (self.repo / "README.md").write_text("# Repository\nReady for development.\n")
        rc, _, err = dev("commit", "-m", "docs: describe repository", "README.md")
        self.assertEqual(rc, 0, err)
        self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD"), "2")
        self.assertEqual(git(self.repo, "for-each-ref", "refs/remotes"), "")
        # Once a fetched default exists, ordinary context checks remain offline.
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.gh.calls.clear()
        self.assertEqual(gitx.ctx().default, "main")
        self.assertEqual(self.gh.calls, [])
        # An explicit symbolic default also takes precedence, including an unborn target.
        git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/chore/declared")
        self.assertEqual(gitx.ctx().default, "chore/declared")
        self.assertEqual(self.gh.calls, [])

    def test_declared_default_controls_refusal_even_with_a_feature_style_name(self):
        self.gh.on(["api", "repos/acme/empty"], {"full_name": "acme/empty", "default_branch": "chore/default"})
        self.assertEqual(gitx.ctx().default, "chore/default")
        for branch in ("main", "master", "chore/default"):
            with self.subTest(branch=branch):
                git(self.repo, "symbolic-ref", "HEAD", "refs/heads/" + branch)
                rc, _, err = dev("commit", "-m", "chore: initialize repository", "README.md")
                self.assertEqual(rc, 2, err)
                self.assertIn("changes go through a branch and a PR", err)
                self.assertEqual(git(self.repo, "diff", "--cached", "--name-only"), "")

    def test_failed_or_invalid_metadata_keeps_the_conservative_default(self):
        for rc, response in ((1, ""), (0, "invalid json"), (0, "null"), (0, "{}"),
                             (0, '{"full_name": "acme/empty", "default_branch": null}'),
                             (0, '{"full_name": "acme/empty", "default_branch": 42}'),
                             (0, '{"full_name": "other/repo", "default_branch": "main"}'),
                             (0, '{"full_name": "acme/empty", "default_branch": "bad..branch"}')):
            with self.subTest(rc=rc, response=response):
                self.gh.on(["api", "repos/acme/empty"], response, rc=rc)
                result, _, err = dev("commit", "-m", "chore: initialize repository", "README.md")
                self.assertEqual(result, 2, err)
                self.assertEqual(git(self.repo, "diff", "--cached", "--name-only"), "")
        # Recovering the same API is sufficient; no branch rename or remote mutation is necessary.
        self.gh.on(["api", "repos/acme/empty"], {"full_name": "acme/empty", "default_branch": "main"})
        result, _, err = dev("commit", "-m", "chore: initialize repository", "README.md")
        self.assertEqual(result, 0, err)


if __name__ == "__main__":
    unittest.main()
