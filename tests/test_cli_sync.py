# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# `dev sync`: what the sync that ends a session reads before it backs up the branches only this Mac holds.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_cli_sync
import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TREE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TREE / "lib"))

from devkit import backup, cli, core, dashboard, registry, work  # noqa: E402


def sync(**kw) -> argparse.Namespace:
    # the arguments of the background `dev sync` a hook starts (the end of a session unless told otherwise)
    base = dict(session=None, finish=True, offline=False, scheduled=False, no_push=False, quiet=True,
                renew=None, delay=0, flush_at=0)
    base.update(kw)
    return argparse.Namespace(**base)


def project(path: str, state: str) -> dict:
    # one GitHub project of the map whose feature branch the branch map reads as `state`
    return {"id": "acme/api", "path": path, "github": "acme/api", "default_branch": "main", "main": {},
            "canonical": path, "canonical_ok": True,
            "worktrees": [], "prs_open": [], "prs_known": True, "initiatives": [],
            "branches": [{"name": "main", "default": True, "backup": {"state": "backed_up"}},
                         {"name": "feat/export", "backup": {"state": state}}]}


class FinishSyncPullRequestTests(unittest.TestCase):
    # Right after a squash merge the pull request cache (up to 15 minutes old) still lists the PR as open, so the
    # branch map reads its branch as not backed up and the end-of-session sync pushed it back to origin. The sync
    # reads GitHub again for the projects it is about to back up, so the backup sees the merge.
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "Development" / "acme" / "api"
        self.repo.mkdir(parents=True)
        for patch in (mock.patch.object(core, "STATE", self.root / "state"),
                      mock.patch.object(cli, "STATE", self.root / "state"),
                      mock.patch.object(core, "HANDOFF", self.root / "handoff"),
                      mock.patch.object(registry, "STATE", self.root / "state"),
                      mock.patch.object(registry, "HANDOFF", self.root / "handoff"),
                      mock.patch.object(core, "CONFIG_FILE", self.root / "config.json"),
                      mock.patch.object(core, "active_cwds", return_value=[]),
                      mock.patch.object(registry, "write_outputs", lambda reg: []),
                      mock.patch.object(dashboard, "write", lambda reg: None),
                      mock.patch.object(work, "write_presence", lambda sessions: None),
                      mock.patch.object(cli, "_sync_handoff", lambda a: None)):
            patch.start()
            self.addCleanup(patch.stop)
        self.inspected = []

        def inspect(main, with_prs=True, refresh_prs=False, pr_max_age=900):
            # git and GitHub: the cache still lists the PR as open; a fresh list shows it merged
            self.inspected.append(refresh_prs)
            return project(str(main), "merged" if refresh_prs else "not_backed_up")

        self.seen = []
        for patch in (mock.patch.object(registry, "build",
                                        lambda **kw: {"projects": [project(str(self.repo), "not_backed_up")],
                                                      "sessions": [], "issues": []}),
                      mock.patch.object(registry, "inspect_repo", inspect),
                      mock.patch.object(backup, "sync", lambda reg, quiet=True: self.seen.append(
                          {b["name"]: b["backup"]["state"] for p in reg["projects"] for b in p["branches"]}))):
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_sync_that_ends_a_session_backs_up_from_a_fresh_pull_request_list(self):
        self.assertEqual(cli.cmd_sync(sync()), 0)
        self.assertEqual(self.seen, [{"main": "backed_up", "feat/export": "merged"}])
        self.assertEqual(self.inspected, [True])

    def test_a_sync_with_nothing_to_back_up_reads_no_pull_requests(self):
        with mock.patch.object(registry, "build", lambda **kw: {
                "projects": [project(str(self.repo), "backed_up")], "sessions": [], "issues": []}):
            self.assertEqual(cli.cmd_sync(sync()), 0)
        self.assertEqual(self.inspected, [])
        self.assertEqual(self.seen, [{"main": "backed_up", "feat/export": "backed_up"}])


if __name__ == "__main__":
    unittest.main()
