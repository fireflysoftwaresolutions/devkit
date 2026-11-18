# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The cached projects map counts as complete when it holds every project a build would map now. A leftover managed
worktree whose main checkout was moved or deleted maps to no project, so it must not keep the map incomplete (every
command would rebuild it). The dev root is a temporary folder; no git call. Run:
python3 -m unittest tests.test_registry_map -v"""
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core, registry  # noqa: E402


class CompleteMapTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dev = Path(tmp.name) / "Development"
        self.wt = self.dev / ".worktrees"
        self.wt.mkdir(parents=True)
        self.reg = {"version": 1, "dev_root": str(self.dev), "host": socket.gethostname().split(".")[0],
                    "projects": []}
        for target in (mock.patch.object(registry, "DEV", self.dev),
                       mock.patch.object(registry, "LOCAL", self.dev / "_local"),
                       mock.patch.object(registry, "WT_ROOT", self.wt),
                       mock.patch.object(registry, "NOTES", Path(tmp.name) / "handoff" / "projects"),
                       mock.patch.object(core, "read_registry", lambda default=None: self.reg)):
            target.start()
            self.addCleanup(target.stop)

    def worktree(self, name, gitdir):
        folder = self.wt / "acme" / "api" / name
        folder.mkdir(parents=True)
        (folder / ".git").write_text(f"gitdir: {gitdir}\n")
        return folder

    def test_a_worktree_whose_main_checkout_is_gone_leaves_the_map_complete_and_a_live_one_does_not(self):
        self.worktree("fix-login", self.dev / "gone" / "api" / ".git" / "worktrees" / "fix-login")
        self.assertIs(registry.cached(complete=True), self.reg)
        gitdir = self.dev / "acme" / "api" / ".git" / "worktrees" / "feat-search"
        gitdir.mkdir(parents=True)
        self.worktree("feat-search", gitdir)  # a worktree git still opens, which the map does not hold yet
        self.assertIsNone(registry.cached(complete=True))


if __name__ == "__main__":
    unittest.main()
