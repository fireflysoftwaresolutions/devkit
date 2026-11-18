# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""What `dev install` says when it refuses to pin a commit (lib/devkit/setup.py, _verify_source): the way out it
names must lead somewhere from where the checkout is. A temporary devkit-like checkout whose origin is a local
repository with a release tag; no network, nothing written outside the temporary folder.
Run: python3 -m unittest discover -s tests -t ."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core, setup  # noqa: E402

ISOLATED = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="Ada", GIT_AUTHOR_EMAIL="ada@example.com", GIT_COMMITTER_NAME="Ada",
               GIT_COMMITTER_EMAIL="ada@example.com", **ISOLATED)


def git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
                          env=GIT_ENV).stdout.strip()


def commit(repo: Path, version: str, tag: bool = False):
    (repo / "VERSION").write_text(version + "\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", version)
    if tag:
        git(repo, "tag", "-a", "v" + version, "-m", "v" + version)


class UnverifiedHeadAdviceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        for p in (mock.patch.object(core, "STATE", root / "state"), mock.patch.dict(core.GIT_ENV, ISOLATED)):
            p.start()
            self.addCleanup(p.stop)
        origin, self.src = root / "origin", root / "devkit"
        origin.mkdir()
        git(origin, "init", "-q", "-b", "main")
        commit(origin, "26.11.3", tag=True)
        git(root, "clone", "-q", str(origin), str(self.src))
        commit(self.src, "26.11.4")  # one commit past the release on main: neither a release nor tested

    def refusal(self) -> str:
        with self.assertRaises(setup.PinError) as e:
            setup._verify_source(self.src, git(self.src, "rev-parse", "HEAD"), "26.11.4")
        return str(e.exception)

    def test_on_a_branch_the_way_back_to_a_release_names_the_tag_to_check_out(self):
        why = self.refusal()
        self.assertIn(f"git -C {core.tilde(self.src)} checkout v26.11.3", why)
        self.assertNotIn("move to the newest release with `dev update`", why)  # on a branch it only pulls again


if __name__ == "__main__":
    unittest.main()
