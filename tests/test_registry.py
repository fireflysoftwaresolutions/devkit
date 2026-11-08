# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The registry's word for a pull request's checks (B3 of the pipeline security review). A draft's run skips every job
# that has the draft gate, and `dev pr ready` starts the real run, so a rollup whose every check was skipped means no
# real run yet: "none", never "passing". Pure functions, no state.
#
# A checkout's state (`dev wt ls`, the session-start context, the unpushed-commit issue) by the merge test: commits
# with an equivalent in origin's default branch are merged, not unpushed. Temporary repositories with a local bare
# origin; devkit's state and handoff repository in a temp folder; no process works anywhere and nothing reaches
# GitHub. Run: python3 -m unittest tests.test_registry -v
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import audit, core, registry, work  # noqa: E402

ISOLATE = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Ana Dev",
           "GIT_AUTHOR_EMAIL": "ana@example.com", "GIT_COMMITTER_NAME": "Ana Dev",
           "GIT_COMMITTER_EMAIL": "ana@example.com"}


def git(cwd, *args):
    rc, out, err = core.sh(["git", "-C", str(cwd), *args], timeout=60)
    if rc != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {err or out}")
    return out


class ChecksTests(unittest.TestCase):
    def test_every_check_skipped_is_no_checks_yet_and_a_skip_beside_a_pass_is_passing(self):
        draft_run = [{"name": "tests", "conclusion": "SKIPPED", "status": "COMPLETED"},
                     {"name": "security", "conclusion": "skipped", "status": "COMPLETED"}]
        self.assertEqual(registry._checks(draft_run), "none")
        self.assertEqual(registry._checks([{"conclusion": "SKIPPED"}, {"conclusion": "SUCCESS"}]), "passing")
        self.assertEqual(registry._checks([{"conclusion": "SKIPPED"}, {"status": "QUEUED"}]), "pending")
        self.assertEqual(registry._checks([{"conclusion": "SKIPPED"}, {"conclusion": "FAILURE"}]), "failing")


class CheckoutStateTests(unittest.TestCase):
    # The repository `widget` on main, its origin a bare repository; each feature lives in its own worktree.
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(core.real(tmp.name))
        state, handoff = self.root / "state", self.root / "handoff"
        for patch in (mock.patch.object(core, "STATE", state), mock.patch.object(registry, "STATE", state),
                      mock.patch.object(core, "HANDOFF", handoff), mock.patch.object(registry, "HANDOFF", handoff),
                      mock.patch.object(core, "NOTES", handoff / "projects"),
                      mock.patch.object(registry, "NOTES", handoff / "projects"),
                      mock.patch.object(work, "HANDOFF", handoff), mock.patch.object(work, "ITEMS", handoff / "items"),
                      mock.patch.object(audit, "AUDIT", state / "audit"),
                      mock.patch.object(core, "CLAUDE_HOME", self.root / ".claude"),
                      mock.patch.dict(core.GIT_ENV, ISOLATE),
                      mock.patch.object(core, "active_cwds", return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)
        self.n = 0
        origin = self.root / "origin.git"
        git(self.root, "init", "-q", "--bare", "-b", "main", str(origin))
        self.repo = self.root / "widget"
        git(self.root, "init", "-q", "-b", "main", str(self.repo))
        git(self.repo, "remote", "add", "origin", str(origin))
        self.commit(self.repo, "chore: start the widget")
        git(self.repo, "push", "-q", "-u", "origin", "main")
        git(self.repo, "remote", "set-head", "origin", "main")

    def commit(self, where, msg):
        self.n += 1
        (where / f"f{self.n}.txt").write_text(msg + "\n")
        git(where, "add", "-A")
        git(where, "commit", "-q", "-m", msg)
        return git(where, "rev-parse", "HEAD")

    def feature(self, name, *msgs, base="main"):
        path = self.root / ("wt-" + name.replace("/", "-"))
        git(self.repo, "worktree", "add", "-q", "-b", name, str(path), base)
        return [self.commit(path, m) for m in msgs]

    def land(self, *shas):
        # main takes the commits the way a release branch carries them: cherry-picked onto a newer main, so the shas
        # differ (on the same parent in the same second, git would rebuild the very same commit)
        self.commit(self.repo, "chore: move main on")
        for sha in shas:
            git(self.repo, "cherry-pick", sha)
        git(self.repo, "push", "-q", "origin", "main")

    def inspect(self):
        p = registry.inspect_repo(self.repo, with_prs=False)
        return p, {w["branch"]: w for w in p["worktrees"]}

    def test_a_worktree_merged_by_content_reads_merged_not_unpushed(self):
        # dev wt ls said "2 unpushed" for a branch whose commits main had cherry-picked
        self.land(*self.feature("fix/picked", "fix: keep the first thing", "fix: keep the second thing"))
        w = self.inspect()[1]["fix/picked"]
        self.assertEqual((w["unpushed"], w.get("merged")), (2, "content"))
        self.assertEqual(registry._state(w), "merged by content")

    def test_a_branch_carried_by_a_merged_branch_is_merged_and_one_only_in_local_main_is_not(self):
        inner = self.feature("fix/inner", "fix: keep the inner thing")
        outer = self.feature("fix/outer", "fix: keep the outer thing", base="fix/inner")
        self.land(*inner, *outer)
        self.feature("feat/local", "feat: add the thing main has only here")
        git(self.repo, "merge", "-q", "--ff-only", "feat/local")  # main is ahead of origin/main
        wts = self.inspect()[1]
        self.assertEqual([registry._state(wts[b]) for b in ("fix/inner", "fix/outer", "feat/local")],
                         ["merged by content", "merged by content", "1 unpushed"])

    def test_only_commits_without_an_equivalent_in_main_count_as_unpushed(self):
        landed, _ = self.feature("fix/half", "fix: keep the landed thing", "fix: keep the stranded thing")
        self.land(landed)
        w = self.inspect()[1]["fix/half"]
        self.assertEqual((w.get("merged"), registry._state(w)), (None, "1 unpushed"))

    def test_a_worktree_merged_by_content_raises_no_unpushed_commits_issue(self):
        self.land(*self.feature("fix/picked", "fix: keep the picked thing"))
        self.feature("feat/wip", "feat: add the thing nobody merged")
        p, wts = self.inspect()
        later = time.time() + 3 * 86400  # both idle three days
        issues = [i for b in ("fix/picked", "feat/wip") for i in registry.work_issues(p["id"], wts[b], now=later)]
        self.assertEqual(issues, [("work", f"{p['id']}: 1 unpushed commit(s) on feat/wip")])


if __name__ == "__main__":
    unittest.main()
