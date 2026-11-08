# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The nightly gc closes merged branches with the same merge test as `dev branch prune`: a worktree whose branch is
# merged by content (its commits cherry-picked into the default branch) goes with its branch once it is idle, a local
# branch without a worktree goes once it has not moved for the grace period, and a remote branch goes only when its
# pull request merged or devkit itself pushed it as a backup; anyone else's merged branch waits for
# `dev branch prune --apply`.
#
# Offline: the sandbox of tests/test_branches.py (a bare "origin", devkit's state, handoff repository and audit trail
# in a temp folder, gh answering with self.prs). gc's scan of the real temp folders and its Docker prune are replaced,
# so nothing outside the sandbox is read or deleted. Run: python3 -m unittest tests.test_gc -v
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from test_branches import HOUR, PID, PruneCase, git  # noqa: E402
from devkit import audit, core, gc, registry  # noqa: E402


class GcCase(PruneCase):
    def setUp(self):
        super().setUp()
        for patch in (mock.patch.object(gc, "_temp_entries", return_value=iter(())),
                      mock.patch.object(gc, "LOGDIR", self.root / "handoff-log"),
                      mock.patch.dict(core.CFG["gc"], {"docker": False, "grace_hours": 24})):
            patch.start()
            self.addCleanup(patch.stop)

    def reg(self, idle=48 * HOUR):
        p = registry.inspect_repo(self.repo, with_prs=False)
        for w in p["worktrees"]:
            w["last_activity"] = int(time.time() - idle)
        return {"projects": [p]}

    def plan(self, **kw):
        return gc.plan(self.reg(**kw), with_sizes=False)

    def actions(self, pl, kind):
        return [a for a in pl["actions"] if a["kind"] == kind]

    def backed_up(self, name):
        audit.record("git.backup", f"origin/{name}", "pushed by the backup", "ok", project=PID, branch=name)


class GcWorktreeTests(GcCase):
    def test_the_idle_clean_worktree_of_a_branch_merged_by_content_goes_with_its_branch(self):
        self.land(*self.feature("fix/picked", "fix: keep the picked thing"))
        wt = self.worktree("fix/picked")
        pl = self.plan()
        [act] = self.actions(pl, "remove-worktree")
        self.assertIn("merged by content: 1 commit has an equivalent in origin/main", act["reason"])
        results = gc.apply(pl, remote=True)
        self.assertFalse(wt.exists(), results)
        self.assertEqual(self.local("fix/picked"), "")

    def test_wt_rm_removes_a_worktree_whose_branch_is_merged_by_content(self):
        # dev wt rm refused it: "it has commits that are on no remote", although main had every change
        from devkit import cli
        self.land(*self.feature("fix/picked", "fix: keep the picked thing"))
        wt = self.worktree("fix/picked")
        with mock.patch.object(cli, "_reg", lambda prs=True: self.reg()):
            rc, out = self.dev("wt", "rm", "fix/picked")
        self.assertEqual(rc, 0, out)
        self.assertFalse(wt.exists())
        self.assertIn("local branch deleted (merged by content", out)

    def test_a_worktree_with_a_commit_the_default_branch_lacks_stays(self):
        self.feature("feat/wip", "feat: add the unfinished thing")
        wt = self.worktree("feat/wip")
        pl = self.plan()
        self.assertEqual(self.actions(pl, "remove-worktree"), [])
        self.assertTrue(any("feat/wip" in n and "not on any remote" in n for n in pl["notes"]), pl["notes"])
        gc.apply(pl, remote=True)
        self.assertTrue(wt.is_dir())


class GcBranchTests(GcCase):
    def test_a_merged_local_branch_without_a_worktree_goes_once_it_has_not_moved_for_the_grace_period(self):
        self.land(*self.feature("fix/old", "fix: keep the old thing", age=48 * HOUR))
        pl = self.plan()
        [act] = [a for a in self.actions(pl, "prune-branch") if a["branch"] == "fix/old"]
        self.assertEqual(act["where"], "local")
        gc.apply(pl, remote=True)
        self.assertEqual(self.local("fix/old"), "")
        self.assertEqual([r["target"] for r in self.records("branch.pruned")], ["fix/old"])

    def test_a_merged_local_branch_that_moved_within_the_grace_period_stays(self):
        self.land(*self.feature("fix/recent", "fix: keep the recent thing", age=2 * HOUR))
        pl = self.plan()
        self.assertEqual([a for a in self.actions(pl, "prune-branch") if a["branch"] == "fix/recent"], [])
        self.assertTrue(any("fix/recent" in n and "moved" in n for n in pl["notes"]), pl["notes"])

    def test_a_merged_remote_branch_devkit_pushed_as_a_backup_goes(self):
        self.land(*self.feature("fix/backed", "fix: keep the backed-up thing", push=True, age=48 * HOUR))
        self.backed_up("fix/backed")
        pl = self.plan()
        self.assertIn(("remote", "fix/backed"), [(a["where"], a["branch"]) for a in self.actions(pl, "prune-branch")])
        gc.apply(pl, remote=True)
        self.assertEqual(self.on_origin("fix/backed"), "")

    def test_a_run_that_may_not_reach_origin_keeps_every_remote_branch(self):
        # dev sync --offline or --no-push runs gc when gc's own job is not installed: it deletes nothing on origin
        self.land(*self.feature("fix/backed", "fix: keep the backed-up thing", push=True, age=48 * HOUR))
        self.backed_up("fix/backed")
        results = gc.apply(self.plan(), remote=False)
        self.assertTrue(self.on_origin("fix/backed"))
        self.assertIn((False, "kept remote branch origin/fix/backed: this run does not reach origin"), results)

    def test_a_merged_remote_branch_devkit_did_not_push_and_no_pull_request_merged_stays(self):
        # someone else's branch: the nightly job leaves it to dev branch prune --apply
        self.land(*self.feature("fix/theirs", "fix: keep their thing", push=True, age=48 * HOUR))
        pl = self.plan()
        self.assertNotIn(("remote", "fix/theirs"), [(a["where"], a["branch"]) for a in self.actions(pl, "prune-branch")])
        self.assertTrue(any("origin/fix/theirs" in n and "dev branch prune --apply" in n for n in pl["notes"]),
                        pl["notes"])
        gc.apply(pl, remote=True)
        self.assertTrue(self.on_origin("fix/theirs"))

    def test_a_remote_branch_whose_pull_request_merged_goes(self):
        tips = self.feature("feat/squashed", "feat: add part one", "feat: add part two", push=True, age=48 * HOUR)
        self.squash("feat/squashed")
        self.prs = [{"number": 7, "state": "MERGED", "headRefName": "feat/squashed", "headRefOid": tips[-1],
                     "isCrossRepository": False, "mergeCommit": {"oid": git(self.repo, "rev-parse", "origin/main")}}]
        pl = self.plan()
        self.assertIn(("remote", "feat/squashed"),
                      [(a["where"], a["branch"]) for a in self.actions(pl, "prune-branch")])
        gc.apply(pl, remote=True)
        self.assertEqual(self.on_origin("feat/squashed"), "")
        self.assertIn("merged: PR #7 was merged", " ".join(r["detail"] for r in self.records("branch.pruned")))


if __name__ == "__main__":
    unittest.main()
