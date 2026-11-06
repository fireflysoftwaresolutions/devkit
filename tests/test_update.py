# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev update`: the choice of the newest release tag, and the update of a checkout that is detached at a release,
on a branch, detached elsewhere, or has local changes, against temporary repositories (no network, no `dev install`).
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import cli, core  # noqa: E402

ISOLATED = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="Ada", GIT_AUTHOR_EMAIL="ada@example.com", GIT_COMMITTER_NAME="Ada",
               GIT_COMMITTER_EMAIL="ada@example.com", **ISOLATED)


def git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
                          env=GIT_ENV).stdout.strip()


def commit(repo: Path, version: str, tag: bool = True):
    (repo / "VERSION").write_text(version + "\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", version)
    if tag:
        git(repo, "tag", "v" + version)


class NewestReleaseTests(unittest.TestCase):
    def test_compares_the_parts_as_numbers(self):
        self.assertEqual(cli.newest_release(["v26.11.2", "v26.11.10", "v26.11.9"]), "v26.11.10")
        self.assertEqual(cli.newest_release(["v26.9.7", "v26.10.0"]), "v26.10.0")
        self.assertEqual(cli.newest_release(["v27.1.0", "v26.12.31"]), "v27.1.0")

    def test_only_release_tags_count(self):
        self.assertEqual(cli.newest_release(["v26.11.2", "v26.12.0-rc1", "nightly", "26.12.1", "v27.1"]), "v26.11.2")
        self.assertIsNone(cli.newest_release(["nightly", "v26.12.0-rc1"]))
        self.assertIsNone(cli.newest_release([]))


class UpdateCheckoutTests(unittest.TestCase):
    """A remote with releases, and a clone of it as the devkit checkout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.remote, self.home = root / "remote", root / "devkit"
        self.remote.mkdir()
        git(self.remote, "init", "-q", "-b", "main")
        commit(self.remote, "26.11.2")
        env = mock.patch.dict(core.GIT_ENV, ISOLATED)
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def clone(self, *args):
        git(self.remote.parent, "clone", "-q", *args, str(self.remote), str(self.home))

    def release(self) -> str:
        return git(self.home, "tag", "--points-at", "HEAD")

    def test_at_a_release_it_moves_to_the_newest_release(self):
        self.clone("--branch", "v26.11.2")
        commit(self.remote, "26.11.9")
        commit(self.remote, "26.11.10")
        commit(self.remote, "26.12.0-dev", tag=False)
        rc, message, install = cli.update_checkout(self.home)
        self.assertEqual((rc, install), (0, True), message)
        self.assertEqual(self.release(), "v26.11.10")
        self.assertEqual(message, "v26.11.2 → v26.11.10")

    def test_at_the_newest_release_it_says_so_and_installs_nothing(self):
        self.clone("--branch", "v26.11.2")
        rc, message, install = cli.update_checkout(self.home)
        self.assertEqual((rc, message, install), (0, "already at the newest release, v26.11.2", False))
        self.assertEqual(self.release(), "v26.11.2")

    def test_on_a_branch_it_pulls_the_branch(self):
        self.clone()
        commit(self.remote, "26.12.0-dev", tag=False)
        rc, message, install = cli.update_checkout(self.home)
        self.assertEqual((rc, install), (0, True), message)
        self.assertEqual(git(self.home, "rev-parse", "HEAD"), git(self.remote, "rev-parse", "HEAD"))
        self.assertEqual(git(self.home, "branch", "--show-current"), "main")

    def test_local_changes_are_refused_and_nothing_moves(self):
        self.clone("--branch", "v26.11.2")
        commit(self.remote, "26.11.3")
        (self.home / "VERSION").write_text("mine\n")
        rc, message, install = cli.update_checkout(self.home)
        self.assertEqual((rc, install), (1, False))
        self.assertIn("has local changes, so nothing was updated", message)
        self.assertEqual(self.release(), "v26.11.2")
        self.assertEqual((self.home / "VERSION").read_text(), "mine\n")

    def test_detached_away_from_a_release_is_refused(self):
        commit(self.remote, "26.12.0-dev", tag=False)
        self.clone()
        git(self.home, "checkout", "-q", "--detach", "HEAD")
        rc, message, install = cli.update_checkout(self.home)
        self.assertEqual((rc, install), (1, False))
        self.assertIn("neither a release nor a branch, so nothing was updated", message)


if __name__ == "__main__":
    unittest.main()
