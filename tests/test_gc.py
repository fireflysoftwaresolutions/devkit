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
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from test_branches import HOUR, PID, PruneCase, git  # noqa: E402
from devkit import audit, branches, core, gc, gitx, registry  # noqa: E402

DAY = 24 * HOUR


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

    # dev wt rm said "local branch deleted (still on the remote)" for a branch never pushed whose commits had reached
    # origin/main: it judged by the commits being on some remote branch, not by the branch being there
    def test_wt_rm_says_the_branch_is_still_on_the_remote_only_when_its_own_ref_is_there(self):
        from devkit import cli
        self.feature("feat/ff", "feat: add the fast-forward thing")
        git(self.repo, "merge", "-q", "--ff-only", "feat/ff")
        git(self.repo, "push", "-q", "origin", "main")
        self.feature("feat/pushed", "feat: add the pushed thing", push=True)
        for branch, said, unsaid in (
                ("feat/ff", "local branch deleted (not on the remote as a branch; its commits are in origin/main)",
                 "still on the remote"),
                ("feat/pushed", "local branch deleted (still on the remote as origin/feat/pushed)", "not on the remote")):
            with self.subTest(branch=branch):
                wt = self.worktree(branch)
                with mock.patch.object(cli, "_reg", lambda prs=True: self.reg()):
                    rc, out = self.dev("wt", "rm", branch)
                self.assertEqual(rc, 0, out)
                self.assertFalse(wt.exists())
                self.assertEqual(self.local(branch), "")
                self.assertIn(said, out)
                self.assertNotIn(unsaid, out)

    def test_a_worktree_with_a_commit_the_default_branch_lacks_stays(self):
        self.feature("feat/wip", "feat: add the unfinished thing")
        wt = self.worktree("feat/wip")
        pl = self.plan()
        self.assertEqual(self.actions(pl, "remove-worktree"), [])
        self.assertTrue(any("feat/wip" in n and "not on any remote" in n for n in pl["notes"]), pl["notes"])
        gc.apply(pl, remote=True)
        self.assertTrue(wt.is_dir())


class GcIgnoredFilesTests(GcCase):
    # `git worktree remove` refuses modified and untracked files but deletes ignored ones without asking: a merged
    # release worktree lost 3.6 GB of evidence kept under its ignored build/. A worktree goes only when each ignored
    # entry is a dependency folder or cache a tool rebuilds, or a byte-for-byte copy of the main checkout's file.
    def setUp(self):
        super().setUp()
        (self.repo / ".git" / "info" / "exclude").write_text("evidence/\nbuild/\nnode_modules/\n__pycache__/\n.env\n")
        self.land(*self.feature("fix/picked", "fix: keep the picked thing"))
        self.wt = self.worktree("fix/picked")
        (self.repo / ".env").write_text("TOKEN=main\n")

    def put(self, rel, text="x"):
        f = self.wt / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        return f

    def remove(self):
        m = branches.merge_test(str(self.repo), "fix/picked", "origin/main")
        return gc.remove_worktree(str(self.repo), str(self.wt), "fix/picked",
                                  merged={"base": "origin/main", "sha": m["sha"], "prs": None})

    def test_a_merged_worktree_with_ignored_files_no_tool_rebuilds_stays_whole_and_names_them(self):
        evidence = self.put("evidence/run.json", "release proof")
        asset = self.put("build/a16-predecessor/asset.bin", "the predecessor's assets")
        env = self.put(".env", "TOKEN=changed here\n")
        deps = self.put("node_modules/pkg/index.js")
        ok, msg = self.remove()
        self.assertFalse(ok, msg)
        self.assertEqual([evidence.read_text(), asset.read_text(), env.read_text()],
                         ["release proof", "the predecessor's assets", "TOKEN=changed here\n"])
        self.assertTrue(deps.exists())
        self.assertTrue(self.local("fix/picked"))
        for named in ("build/ (", "evidence/ (", ".env (", f"then dev wt rm {core.tilde(str(self.wt))}"):
            self.assertIn(named, msg)
        self.assertNotIn("node_modules", msg)
        self.assertNotIn("\n", msg)  # dev branch prune shows the last line of it

    def test_a_merged_worktree_whose_ignored_files_are_caches_or_copies_goes_and_says_what_went_with_it(self):
        self.put(".env", "TOKEN=main\n")  # .worktreeinclude copied it: the main checkout has the same bytes
        self.put("node_modules/pkg/index.js")
        self.put("src/__pycache__/m.pyc")
        ok, msg = self.remove()
        self.assertTrue(ok, msg)
        self.assertFalse(self.wt.exists())
        self.assertIn("went with it: .env, node_modules/, src/__pycache__/", msg)

    def test_dev_pr_merge_says_it_kept_the_merged_branchs_worktree_for_its_ignored_files(self):
        evidence = self.put("evidence/run.json", "release proof")
        note, said = gitx._after_merge(gitx.ctx(self.repo), "fix/picked")  # a warning line, never the success line
        self.assertEqual(note, "")
        self.assertTrue(said.startswith(f"kept {core.tilde(str(self.wt))}: "), said)
        self.assertIn("evidence/ (", said)
        self.assertEqual(evidence.read_text(), "release proof")
        self.assertTrue(self.local("fix/picked"))

    def test_dev_branch_prune_says_it_kept_the_worktree_down_to_how_to_remove_it(self):
        self.put("evidence/run.json", "release proof")
        rc, out = self.dev("branch", "prune", "--local", "--apply")
        self.assertIn(f"not deleted local fix/picked: kept {core.tilde(str(self.wt))}: ", out)
        self.assertIn(f"then dev wt rm {core.tilde(str(self.wt))} (merged by content", out)
        self.assertTrue((self.wt / "evidence" / "run.json").exists())
        self.assertTrue(self.local("fix/picked"))

    def test_the_dry_run_lists_a_worktree_kept_for_its_ignored_files_under_kept_and_out_of_the_total(self):
        # dev gc counted it as "remove worktree" in the reclaimable total, and the apply then kept it
        self.put("build/a16-predecessor/asset.bin", "the predecessor's assets")

        def reg(**_):
            r = self.reg()
            for w in r["projects"][0]["worktrees"]:
                w["size_kb"] = 3 * 1024 * 1024
            return r
        with mock.patch.object(registry, "build", reg):
            rc, out = self.dev("gc", "-v")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("remove worktree", out)
        self.assertNotIn("3.0 GB", out)
        self.assertIn(f"fix/picked · {core.tilde(str(self.wt))} — removing it would delete ignored files that are "
                      f"not known to be regenerable: build/ (", out)


class GcBuildOutputTests(GcCase):
    # The nightly job prunes ignored folders in a worktree idle for idle_artefact_days. Only what a tool fills again
    # goes without a person; a generic output name (build, dist, target, coverage, .cache) can hold files no build
    # step wrote, such as a release's evidence or a predecessor's assets.
    def setUp(self):
        super().setUp()
        (self.repo / ".git" / "info" / "exclude").write_text("build/\nnode_modules/\n")
        self.feature("feat/wip", "feat: add the unfinished thing", push=True)
        self.wt = self.worktree("feat/wip")
        self.asset = self.wt / "build" / "a16-predecessor" / "asset.bin"
        self.asset.parent.mkdir(parents=True)
        self.asset.write_bytes(b"the predecessor's assets" * 512)
        self.deps = self.wt / "node_modules" / "pkg" / "index.js"
        self.deps.parent.mkdir(parents=True)
        self.deps.write_text("module.exports = 1\n")

    def test_the_nightly_cleanup_keeps_build_output_no_tool_fills_again_and_prunes_what_one_does(self):
        results = gc.apply(self.plan(idle=8 * DAY), only_auto=True, remote=True)
        self.assertTrue(self.asset.is_file(), results)
        self.assertEqual(self.asset.read_bytes(), b"the predecessor's assets" * 512)
        self.assertFalse((self.wt / "node_modules").exists(), results)

    def test_build_output_no_tool_fills_again_is_a_check_first_action_only_an_explicit_apply_deletes(self):
        pl = gc.plan(self.reg(idle=8 * DAY), with_sizes=True)
        prunes = self.actions(pl, "prune-artefacts")
        self.assertEqual(sorted((bool(a.get("auto")), [os.path.basename(i) for i in a["items"]]) for a in prunes),
                         [(False, ["build"]), (True, ["node_modules"])])
        [check] = [a for a in prunes if not a.get("auto")]
        self.assertTrue(check["reason"].startswith("check first: "), check["reason"])
        self.assertIn(f"build/ ({core.human_kb(check['kb'])})", check["reason"])
        self.assertIn("dev gc --apply deletes it", check["reason"])
        gc.apply(pl, only_auto=False, remote=True)  # dev gc --apply, after the person saw the plan
        self.assertFalse(self.asset.exists())


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
