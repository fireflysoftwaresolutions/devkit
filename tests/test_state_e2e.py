# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# End to end: the lifecycles of work items, runs, decisions, crew lanes, CI watches and the installed configuration,
# each driven through devkit's own entry points (the `dev` parser, the hooks, the sync and watch jobs) and checked by
# what a developer sees at the end: the item's status and changelog, the run and its steps, the needs-you list of the
# status block and the dashboard, `dev doctor`'s work state, the CI messages a session gets, the settings and
# instruction files.
#
# Every test runs in a sandbox of its own: devkit's state in the test's folder, the handoff repository as a clone of
# a local bare remote, a project repository whose origin is a local bare repository, fixed clocks where time decides
# the outcome, a fake `gh` and a fake CI host, and no network. The suite's temporary home (tests/_hermetic.py) keeps
# the developer's real devkit out of reach.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_state_e2e
import argparse
import calendar
import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import (audit, ciwatch, cistatus, cli, core, crew, dashboard, dashnow, delivery, gitx,  # noqa: E402
                    hooks, lessons, plans, policy, progress as pg, registry, selfcheck, setup, state, work)
from test_progress import TempState, launch  # noqa: E402

PROJECT = "acme/widget"
GITHUB_URL = "https://github.com/acme/widget.git"
ISOLATE = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Ana Dev",
           "GIT_AUTHOR_EMAIL": "ana@example.com", "GIT_COMMITTER_NAME": "Ana Dev",
           "GIT_COMMITTER_EMAIL": "ana@example.com", "GIT_TERMINAL_PROMPT": "0"}
ANA = {"user": "ana", "host": "mbp1", "agent": "claude", "session": "s1"}
BOB = {"user": "bob", "host": "mbp2", "agent": "codex", "session": "s2"}
MERGE_SHA = "abc1234" + "0" * 33
REAL_CREW_LOAD = crew.load  # the roster without the team's overlay: crew.load(team=False)
YY, MM = time.localtime().tm_year % 100, time.localtime().tm_mon
NEO_REPORT = ("**[Firefly> DevKit · Senior engineer (Neo)]** acme/api · `main` · parser\n"
              "- **Done:** parser\n- **Check:** ✓ 12 parser tests pass\n- **Next:** the tech lead")


def git(cwd, *args) -> str:
    rc, out, err = core.sh(["git", "-C", str(cwd), *args], timeout=60)
    if rc != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}: {err or out}")
    return out


_PARSER = []


def dev(*argv) -> tuple:
    # `dev <argv>` through devkit's own parser, in this process: (exit code, stdout, stderr). A parse error (an
    # unknown command) is exit 2, as in a shell.
    if not _PARSER:
        _PARSER.append(cli.build_parser())
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            a = _PARSER[0].parse_args([str(x) for x in argv])
            rc = a.fn(a) or 0
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
    return rc, out.getvalue(), err.getvalue()


def patch_if(stack, module, name, value):
    # patch a module's name when this release has it (a sandbox path a newer release added)
    if hasattr(module, name):
        stack.enter_context(mock.patch.object(module, name, value))


class FakeGH:
    # a canned `gh`: the newest matching route wins; every call is recorded
    def __init__(self):
        self.calls, self.routes = [], []

    def on(self, prefix, out="", rc=0, err="", when=None):
        self.routes.insert(0, (list(prefix), rc, out if isinstance(out, str) else json.dumps(out), err, when))

    def __call__(self, args, cwd=None, timeout=60):
        args = list(args)
        self.calls.append(args)
        for prefix, rc, out, err, when in self.routes:
            if args[:len(prefix)] == prefix and (when is None or when(args)):
                return rc, out, err
        return 1, "", "no canned response for: gh " + " ".join(args)


class Spawner:
    # the subprocess module as one devkit module sees it: the real one, but Popen is the test's (patching
    # subprocess.Popen itself would change every git call of the process)
    def __init__(self, popen):
        self.Popen = popen

    def __getattr__(self, name):
        return getattr(subprocess, name)


class Clock:
    # the time module as one devkit module sees it: the real one, but time() is a moment the test moves
    def __init__(self, t: float):
        self.t = t

    def time(self):
        return self.t

    def __getattr__(self, name):
        return getattr(time, name)


class Sandbox:
    # The world one test runs in. `handoff()` makes the handoff repository a clone of a local bare remote (and, with
    # two Macs, a second clone); `project()` makes the repository acme/widget, whose GitHub URL resolves to a local
    # bare repository, with a fake gh. Everything is undone when the test ends.

    def __init__(self, test: unittest.TestCase):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.stack = contextlib.ExitStack()
        test.addCleanup(self.stack.close)
        s = self.stack
        s.enter_context(mock.patch.dict(core.GIT_ENV, ISOLATE))
        s.enter_context(mock.patch.object(core, "STATE", self.base / "state"))
        s.enter_context(mock.patch.object(cli, "STATE", self.base / "state"))
        s.enter_context(mock.patch.object(audit, "AUDIT", self.base / "audit"))
        s.enter_context(mock.patch.object(audit, "policy_file", lambda: self.base / "no-policy.json"))
        self.sync_requests = []  # the background `dev sync` runs a change asked for, run with background_sync()
        s.enter_context(mock.patch.object(work, "request_sync",
                                          lambda reason="", *a, **k: self.sync_requests.append(reason) and False))
        s.enter_context(mock.patch.object(core, "github_user", lambda: "ana"))
        s.enter_context(mock.patch.dict(core.CFG.setdefault("sync", {}), {"push": True}))
        s.enter_context(mock.patch.dict(core.CFG.setdefault("work", {}), {"lock_hours": 8}))
        saved = (state.WAIT, state.SAY_BUSY)  # hooks.run sets both for the rest of the process

        def restore():
            state.WAIT, state.SAY_BUSY = saved
        test.addCleanup(restore)
        self.cwd = os.getcwd()
        test.addCleanup(os.chdir, self.cwd)

    # ----------------------------------------------------------------- the handoff repository

    def handoff(self, macs=1):
        remote = self.base / "handoff.git"
        git(self.base, "init", "-q", "--bare", "-b", "main", str(remote))
        self.here = self.base / "here" / ".handoff"
        git(self.base, "init", "-q", "-b", "main", str(self.here))
        git(self.here, "remote", "add", "origin", str(remote))
        git(self.here, "commit", "-q", "--allow-empty", "-m", "start")
        git(self.here, "push", "-q", "-u", "origin", "main")
        self.other = None
        if macs > 1:
            self.other = self.base / "other" / ".handoff"
            git(self.base, "clone", "-q", str(remote), str(self.other))
        s = self.stack
        for module, names in ((core, ("HANDOFF",)), (work, ("HANDOFF", "ITEMS", "SESSIONS", "PRESENCE")),
                              (plans, ("HANDOFF", "PLANS")), (cli, ("HANDOFF",))):
            for name in names:
                patch_if(s, module, name, getattr(module, name))  # restored when the test ends
        self.point(self.here)
        return self.here

    @staticmethod
    def point(root: Path):
        core.HANDOFF = work.HANDOFF = plans.HANDOFF = root
        work.ITEMS, work.SESSIONS, work.PRESENCE, plans.PLANS = root / "items", root / "sessions", \
            root / "presence", root / "plans"
        if hasattr(cli, "HANDOFF"):
            cli.HANDOFF = root

    @contextlib.contextmanager
    def on_other(self):
        # devkit as the other Mac runs it: its own clone of the handoff remote
        self.point(self.other)
        try:
            yield
        finally:
            self.point(self.here)

    def item(self, iid: str, scope: str = PROJECT) -> dict:
        return work.load(work.item_path(scope, iid))

    def remote_item(self, iid: str, scope: str = PROJECT) -> dict:
        git(self.here, "fetch", "-q", "origin")
        return work.parse(git(self.here, "show", f"origin/main:items/{scope}/{iid}.md") + "\n")

    def background_sync(self):
        # what the queued `dev sync` does with the handoff repository: commit what is left, pull and push
        if not self.sync_requests:
            raise AssertionError("no background sync was asked for")
        self.sync_requests.clear()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cli.commit_push(quiet=True)

    def stopped(self) -> list:
        # a rebase git left stopped in a clone of the handoff repository
        return [f"{root.parent.name}: {d}" for root in (self.here, self.other) if root
                for d in ("rebase-merge", "rebase-apply") if (root / ".git" / d).exists()]

    # ----------------------------------------------------------------- the project repository

    def project(self):
        s = self.stack
        self.origin = self.base / "origin.git"
        git(self.base, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.repo = self.base / "widget"
        git(self.base, "init", "-q", "-b", "main", str(self.repo))
        git(self.repo, "remote", "add", "origin", GITHUB_URL)
        git(self.repo, "config", f"url.{self.origin}.insteadOf", GITHUB_URL)
        (self.repo / "README.md").write_text("# widget\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "chore: start the widget")
        git(self.repo, "push", "-q", "-u", "origin", "main")
        git(self.repo, "remote", "set-head", "origin", "main")
        self.gh = FakeGH()
        self.gh.on(["pr", "view"], rc=1, err="no pull requests found")
        self.gh.on(["pr", "list"], [])
        self.gh.on(["run", "list"], [])
        s.enter_context(mock.patch.object(gitx, "GH", self.gh))
        s.enter_context(mock.patch.object(gitx, "EXEC", lambda argv, cwd=None, env=None: 0))
        s.enter_context(mock.patch.object(gitx, "SLEEP", lambda sec: None))
        s.enter_context(mock.patch.object(gitx, "_me", lambda: "ana"))
        s.enter_context(mock.patch.object(gitx, "_handoff_log", lambda pid, text: True))
        s.enter_context(mock.patch.object(delivery, "POLICIES", self.base / "policies"))
        real_sh = registry.sh
        s.enter_context(mock.patch.object(
            registry, "sh", lambda args, **kw: self.gh(list(args)[1:]) if list(args)[:1] == ["gh"] else real_sh(args, **kw)))
        s.enter_context(mock.patch.object(registry, "STATE", self.base / "state"))
        # the project as `dev task -p acme/widget` names it (resolving a project reads the whole workspace)
        s.enter_context(mock.patch.object(cli, "_scope", lambda a: (PROJECT, PROJECT)))
        os.chdir(self.repo)
        return self.repo

    def commit(self, message: str, name: str = None) -> str:
        f = self.repo / (name or f"f{time.time_ns()}.txt")
        f.write_text(message + "\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def new_item(self, title: str) -> dict:
        rc, out, err = dev("task", "-p", PROJECT, "add", title)
        if rc != 0:
            raise AssertionError(f"dev task add failed: {err or out}")
        return next(it for it in work.all_items(PROJECT) if it["meta"]["title"] == title)


def _pr(n=7, **kw) -> dict:
    p = {"number": n, "title": "feat(invoice): add csv export", "state": "OPEN", "isDraft": False,
         "headRefName": "feat/invoice", "baseRefName": "main", "headRefOid": "f" * 40, "reviewDecision": "APPROVED",
         "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "url": f"https://github.com/acme/widget/pull/{n}",
         "statusCheckRollup": [{"conclusion": "SUCCESS"}], "additions": 10, "deletions": 2, "changedFiles": 3,
         "author": {"login": "ana"}, "updatedAt": "2026-11-05T10:00:00Z", "body": "## What and why\n\nExports invoices.\n"}
    p.update(kw)
    return p


def doctor_rows(top, scope, runs=(), now=None) -> list:
    # `dev doctor`'s work state row for a project: [(ok, detail, fix)]; a release without the row says so
    checks = getattr(selfcheck, "work_state_checks", None)
    if checks is None:
        raise AssertionError("dev doctor has no work state row in this release")
    return [(ok, detail, fix) for _, _, ok, detail, fix in checks(top, scope, runs=list(runs), now=now or time.time())]


# --------------------------------------------------------------------------- work items and merges


class MergeClosesItemsTests(unittest.TestCase):
    # An item started on a branch was still `doing` after its PR merged, and its lock kept renewing: the board showed
    # finished work as in progress. The merge closes the items of the merged branch, and only those.
    def test_a_merged_pr_closes_the_item_started_on_its_branch_and_leaves_the_item_started_on_main_open(self):
        w = Sandbox(self)
        w.handoff()
        w.project()
        refunds = w.new_item("Refunds")
        rc, _, err = dev("task", "-p", PROJECT, "start", refunds["meta"]["id"])  # on main: no branch to link
        self.assertEqual(rc, 0, err)
        git(w.repo, "switch", "-q", "-c", "feat/invoice")
        export = w.new_item("Export invoices")
        rc, _, err = dev("task", "-p", PROJECT, "start", export["meta"]["id"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(w.item(export["meta"]["id"])["meta"]["links"], ["branch:feat/invoice"])
        w.commit("feat(invoice): add csv export")
        git(w.repo, "push", "-q", "-u", "origin", "feat/invoice")
        refunds_before = w.item(refunds["meta"]["id"])

        w.gh.on(["repo", "view"], {"squashMergeAllowed": True, "viewerDefaultMergeMethod": "SQUASH"})
        w.gh.on(["pr", "merge"], "")
        w.gh.on(["pr", "view"], _pr(mergeCommit={"oid": MERGE_SHA}))
        rc, out, err = dev("pr", "merge", "7")
        self.assertEqual(rc, 0, err)
        self.assertTrue([c for c in w.gh.calls if c[:2] == ["pr", "merge"]], "the merge never reached gh")

        done = w.item(export["meta"]["id"])
        self.assertEqual((done["meta"]["status"], done["meta"]["claimed_by"], done["meta"]["lock_expires"]),
                         ("done", "", ""), done["changelog"][:2])
        self.assertIn("merged in PR #7 (abc1234)", done["changelog"][0])
        self.assertIn(f"{export['meta']['id']} “Export invoices”", out)
        held = w.item(refunds["meta"]["id"])
        self.assertEqual((held["meta"]["status"], held["meta"]["claimed_by"], held["changelog"]),
                         ("doing", refunds_before["meta"]["claimed_by"], refunds_before["changelog"]))
        self.assertNotIn(refunds["meta"]["id"], out)
        w.background_sync()
        self.assertEqual(w.remote_item(export["meta"]["id"])["meta"]["status"], "done")  # other Macs see it closed


class LockLifecycleTests(unittest.TestCase):
    # A lock was renewed by any live session of its holder, whatever branch it was on, so an item nobody worked on
    # stayed `doing` for days; an expired lock left its item `doing` with no holder; and a UTC lock time read on a Mac
    # in Madrid in summer came out an hour early. Ana's live session is on feat/live; nobody is on feat/idle; Bob, on
    # another Mac, holds a third item. The clock is moved hour by hour on 1 July 2026 (summer time in Madrid), and
    # each hour runs what the jobs run: `dev sync --renew` from Ana's live session, then the hourly expiry.
    T0 = calendar.timegm((2026, 7, 1, 8, 0, 0))  # 10:00 in Madrid

    def setUp(self):
        saved = os.environ.get("TZ")

        def restore():
            if saved is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = saved
            time.tzset()
        self.addCleanup(restore)
        os.environ["TZ"] = "Europe/Madrid"
        time.tzset()

    def hourly(self, w, at: float):
        w.clock.t = at
        live = {"agent": "claude", "session": "live1", "project": "acme/api", "branch": "feat/live", "active": True}
        with mock.patch.object(registry, "build", lambda **kw: {"projects": [], "sessions": [live]}), \
                mock.patch.object(registry, "write_outputs", lambda reg: []), \
                mock.patch.object(dashboard, "write", lambda reg: None):
            rc, out, err = dev("sync", "--renew", "claude:live1", "--offline", "--no-push", "--quiet")
        self.assertEqual(rc, 0, err or out)
        work.pull()
        work.expire_locks()

    def test_only_a_lock_someone_works_on_outlives_lock_hours_and_a_summer_lock_time_is_read_in_utc(self):
        w = Sandbox(self)
        w.handoff()
        w.clock = Clock(self.T0)
        w.stack.enter_context(mock.patch.object(work, "time", w.clock))
        me = dict(work.actor(), agent="claude", session="live1")  # Ana (core.github_user) on this Mac's host
        live = work.claim(work.create("acme/api", "Export invoices", a=me), a=me, branch="feat/live")
        idle = work.claim(work.create("acme/api", "Import invoices", a=me), a=me, branch="feat/idle")
        bobs = work.claim(work.create("acme/api", "Refunds", a=BOB), a=BOB, branch="feat/refunds")
        ids = {k: it["meta"]["id"] for k, it in (("live", live), ("idle", idle), ("bob", bobs))}
        for hour in (1, 2, 3, 4, 5, 6, 7, 7.5):
            self.hourly(w, self.T0 + hour * 3600)

        with self.subTest("a lock with half an hour left on a summer day is still held"):
            held = w.item(ids["bob"], "acme/api")
            self.assertEqual((held["meta"]["status"], held["meta"]["claimed_by"].split(" ")[0]),
                             ("doing", "bob@mbp2"), held["changelog"][0])
            self.assertEqual(work.parse_iso(held["meta"]["lock_expires"]), self.T0 + 8 * 3600)

        self.hourly(w, self.T0 + 8.5 * 3600)
        with self.subTest("a lock nobody works on runs out after lock_hours and its item goes back to todo"):
            back = w.item(ids["idle"], "acme/api")
            self.assertEqual((back["meta"]["status"], back["meta"]["claimed_by"], back["meta"]["lock_expires"]),
                             ("todo", "", ""), back["changelog"][0])
            self.assertIn(f"expired at {work.now_iso(self.T0 + 8 * 3600)}", back["changelog"][0])
            self.assertIn("back to todo", back["changelog"][0])
        with self.subTest("the lock of the branch a live session is on is renewed"):
            kept = w.item(ids["live"], "acme/api")
            self.assertEqual((kept["meta"]["status"], kept["meta"]["lock_expires"]),
                             ("doing", work.now_iso(self.T0 + 13 * 3600)))  # renewed at 13:00 Madrid for 8 h
        with self.subTest("a summer time in UTC reads back as the same moment"):
            self.assertEqual(work.parse_iso("2026-07-01T16:00:00Z"), self.T0 + 8 * 3600)


class ReleaseOpenItemsTests(unittest.TestCase):
    # A branch merged on GitHub (not with `dev pr merge`) left its item open, and the next release shipped it
    # without anyone noticing. The release refuses, names the item, and goes through once reconcile closes it.
    def test_a_release_waits_for_the_items_of_branches_merged_since_the_last_tag_until_reconcile_closes_them(self):
        w = Sandbox(self)
        w.handoff()
        w.project()
        git(w.repo, "tag", f"v{YY}.{MM}.0")
        git(w.repo, "push", "-q", "origin", f"v{YY}.{MM}.0")
        git(w.repo, "switch", "-q", "-c", "feat/invoice")
        export = w.new_item("Export invoices")
        rc, _, err = dev("task", "-p", PROJECT, "start", export["meta"]["id"])
        self.assertEqual(rc, 0, err)
        w.commit("feat(invoice): add csv export")
        git(w.repo, "switch", "-q", "main")
        git(w.repo, "merge", "-q", "--no-ff", "feat/invoice", "-m", "Merge pull request #7 from acme/feat/invoice")
        git(w.repo, "push", "-q", "origin", "main")
        merge = git(w.repo, "rev-parse", "HEAD")
        merged_pr = {"number": 7, "state": "MERGED", "headRefName": "feat/invoice", "mergeCommit": {"oid": merge},
                     "mergedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "isCrossRepository": False,
                     "baseRefName": "main"}
        w.gh.on(["pr", "list"], [merged_pr], when=lambda args: "merged" in args)
        rc, _, err = dev("ci", "--mark-passed")
        self.assertEqual(rc, 0, err)

        rc, out, err = dev("release", "--dry-run")
        with self.subTest("the release refuses and names the open item of the merged branch"):
            self.assertEqual(rc, 2, out)
            self.assertIn(f"1 open work item(s) linked to branches merged since v{YY}.{MM}.0", err)
            self.assertIn(f"{export['meta']['id']} “Export invoices” · feat/invoice (PR #7)", err)
            self.assertEqual(git(w.repo, "tag", "--list").split(), [f"v{YY}.{MM}.0"])

        rc, out, err = dev("task", "-p", PROJECT, "reconcile", "--apply")
        self.assertEqual(rc, 0, err)
        closed = w.item(export["meta"]["id"])
        self.assertEqual(closed["meta"]["status"], "done", closed["changelog"][0])
        self.assertIn(f"feat/invoice merged in {merge[:7]} (PR #7)", closed["changelog"][0])
        rc, out, err = dev("release", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn(f"dry run: release v{YY}.{MM}.1", out)
        self.assertEqual(doctor_rows(w.repo, PROJECT), [(True, f"{PROJECT}: no stale work items or runs", "")])


class TwoMacsTests(unittest.TestCase):
    # Two Macs claimed one item at the same moment and both were told they held it; and a conflicting edit of one item
    # file left the handoff repository in a stopped rebase that every later sync tripped on.
    def test_one_of_two_macs_claiming_one_item_wins_and_a_conflicting_edit_merges_field_by_field(self):
        w = Sandbox(self)
        w.handoff(macs=2)
        export = work.create(PROJECT, "Export invoices", why="a reason", a=ANA)["meta"]["id"]
        imports = work.create(PROJECT, "Import invoices", why="a reason", a=ANA)["meta"]["id"]
        git(w.here, "push", "-q")
        git(w.other, "pull", "-q")

        real_pull, raced = work.pull, []

        def pull_then_the_other_mac_claims(*a, **k):
            done = real_pull(*a, **k)
            if not raced:  # between this Mac's pull and its push, the other Mac claims and pushes
                raced.append(True)
                with w.on_other():
                    work.claim(w.item(export), a=BOB)
            return done

        with mock.patch.object(work, "pull", pull_then_the_other_mac_claims):
            try:
                work.claim(w.item(export), a=ANA)
                outcome = "this Mac was told it holds the lock"
            except ValueError as e:
                outcome = str(e)
        with self.subTest("exactly one Mac holds the lock, and the other is told who"):
            self.assertEqual(raced, [True])
            self.assertIn("claimed by bob@mbp2", outcome)
            self.assertTrue(w.remote_item(export)["meta"]["claimed_by"].startswith("bob@mbp2"))
            work.pull()
            self.assertTrue(w.item(export)["meta"]["claimed_by"].startswith("bob@mbp2"))
            self.assertEqual(w.stopped(), [])

        with w.on_other():
            work.update(w.item(imports), "move", why="start now", a=BOB, bucket="now")
            git(w.other, "push", "-q")
        work.update(w.item(imports), "edit", why="CSV only", a=ANA, body="Only CSV.")
        cli.commit_push(quiet=True)  # the background `dev sync`
        with self.subTest("both changes to one item file are kept, and no rebase is left stopped"):
            self.assertEqual(w.stopped(), [])
            pushed = w.remote_item(imports)
            self.assertEqual((pushed["meta"]["bucket"], pushed["body"]), ("now", "Only CSV."))
            self.assertEqual([e.split(" · ")[0] for e in pushed["changelog"]], ["v3", "v2", "v1"])
            self.assertEqual(git(w.here, "rev-list", "--count", "@{u}..HEAD"), "0")


# --------------------------------------------------------------------------- runs, decisions and crew lanes


class HookSandbox(unittest.TestCase):
    # A session's progress state in the test's folder (test_progress.TempState), with the hooks reading and writing
    # there too, the crew without a team overlay, and the hooks driven as Claude Code fires them.
    def setUp(self):
        self.ts = TempState().__enter__()
        self.addCleanup(self.ts.__exit__, None, None, None)
        self.root = Path(self.ts.tmp.name)
        h = self.root / "handoff"
        h.mkdir()
        saved = (state.WAIT, state.SAY_BUSY)
        self.addCleanup(lambda: setattr(state, "WAIT", saved[0]) or setattr(state, "SAY_BUSY", saved[1]))
        for p in (mock.patch.object(hooks, "STATE", self.root), mock.patch.object(pg, "_ci", lambda *a: ""),
                  mock.patch.object(lessons, "LESSONS", h / "lessons"), mock.patch.object(work, "HANDOFF", h),
                  mock.patch.object(lessons, "USAGE_LOCAL", h / "usage.json"),
                  mock.patch.object(core, "HANDOFF", h),
                  mock.patch.object(work, "request_sync", lambda *a, **k: False),
                  mock.patch.object(crew, "load", lambda *a, **k: REAL_CREW_LOAD(team=False))):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("DEVKIT_HEADLESS", None)

    def hook(self, event, payload):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out), \
                mock.patch("devkit.guard.check", lambda *a, **k: None):
            hooks.run(event, "claude")
        return out.getvalue()

    def bash_done(self, cmd, cwd=None, tid="toolu_b"):
        return {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_use_id": tid,
                "tool_input": {"command": cmd}, "tool_response": {"stdout": ""}, "cwd": str(cwd or self.root)}

    def run_(self):
        return pg.load_run("claude", "s1")


def needs_you(run, now) -> list:
    # the dashboard's needs-you rows of this run, as the Now page builds them
    raws = [copy.deepcopy(run)]
    doc = dashnow.areas(reg={"projects": [], "generated": now}, raw_runs=raws,
                        briefs=dashboard._runs(now, "mbp1", raw=copy.deepcopy(raws)), actions=[],
                        ci={"watches": [], "complete": True, "total": 0, "watching": []}, jobs=[], items=[],
                        since={}, team=[], now=now, host="mbp1", memo=dashnow.LandingMemo(clock=lambda: now),
                        roster=REAL_CREW_LOAD(team=False))
    att = doc["attention"]
    if "error" in att:
        raise AssertionError(f"the needs-you list could not be built: {att['error']}")
    return [i["id"] for i in att["items"]]


class DecisionLifecycleTests(HookSandbox):
    # Decisions stayed on the developer's list after nobody waited on them any more: the lane that asked had ended,
    # the run was replaced, or the tech lead dropped the question. The Senior engineer's lane asks D1 and its step
    # waits on it; the tech lead asks D2 before a push.
    def test_a_decision_nobody_waits_on_never_needs_the_developer_and_closing_one_never_answers_it(self):
        t0 = time.time() - 2 * 3600

        def m(minutes):
            return t0 + minutes * 60
        pg.start("Feature", plan=["parser", "docs"], now=m(0), mode="guided")
        pg.assign("parser", "Neo", now=m(0))
        launch("a1", "devkit:neo", "Neo: parser", m(1))
        pg.crew_decision("Skip empty lines or reject the file?", options="A: skip | B: reject", recommend="A",
                         from_agent="a1", now=m(2))
        pg.crew_decision("Push feat/parser?", irreversible="push", target="feat/parser", now=m(3))
        # the Senior engineer stops with its report (what the SubagentStop hook hands to progress)
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a1", "agent_type": "devkit:neo",
                                  "last_assistant_message": NEO_REPORT}, False, now=m(5))

        with self.subTest("the ended lane's decision is moot: not on the developer's list anywhere"):
            run = self.run_()
            self.assertEqual(run["agents"]["a1"]["state"], "done")
            self.assertEqual([d["id"] for d in pg.pending_decisions(run)], ["D2"])
            self.assertEqual([i for i in needs_you(run, m(6)) if i.startswith("decision:")],
                             [f"decision:{dashboard.run_id(run)}:D2"])
            you = [ln for ln in pg.render_update(run, branch="feat/parser", now=m(6)).splitlines()
                   if ln.startswith("- **You:**")]
            self.assertEqual(len(you), 1)
            self.assertNotIn("Skip empty lines", you[0])
            self.assertNotIn("D1", pg.decisions_context("claude", "s1"))

        rc, out, err = dev("crew", "withdraw", "D2", "--why", "the push waits for the release branch")
        with self.subTest("dev crew withdraw closes a decision with its reason, and it never counts as an answer"):
            self.assertEqual(rc, 0, err or out)
            d2 = next(d for d in self.run_()["decisions"] if d["id"] == "D2")
            self.assertEqual(d2["state"], "withdrawn")
            self.assertFalse(pg.approves_action(d2))
            rc, out, err = dev("crew", "decide", "D2", "A: yes")
            self.assertNotEqual(rc, 0, out)
            d2 = next(d for d in self.run_()["decisions"] if d["id"] == "D2")
            self.assertEqual((d2["state"], pg.approves_action(d2)), ("withdrawn", False))
            self.assertEqual(pg.pending_decisions(self.run_()), [])

        pg.crew_decision("Which CSV dialect?", options="A: RFC 4180 | B: Excel", recommend="A", now=m(7))  # D3
        pg.start("Feature, smaller", plan=["parser"], now=m(8), replace=True, why="the plan changed")
        with self.subTest("replacing the run closes its open decision, with the reason in the new run's history"):
            run = self.run_()
            self.assertEqual((run["goal"], run["decisions"], pg.pending_decisions(run)), ("Feature, smaller", [], []))
            moved = [(h.get("from"), h.get("to")) for h in run.get("history") or []
                     if h.get("step") == "(decision D3)"]
            self.assertEqual(moved, [("pending", "closed")])
            kept = {d["id"]: d for d in (run.get("replaced") or {}).get("decisions") or []}
            self.assertEqual(kept.get("D3", {}).get("state"), "closed")
            self.assertIn("run replaced: the plan changed", kept["D3"].get("reason", ""))
            self.assertEqual([i for i in needs_you(run, m(9)) if i.startswith("decision:")], [])


class StoppedLaneTests(HookSandbox):
    # A lane stopped with TaskStop stayed running for hours and held a crew slot, and its step waited for a review of
    # work that never came; a member's SubagentStop that met the session's lock busy was dropped, so its finished step
    # later turned unverified. The Release engineer and the Senior engineer each work on a step.
    def test_a_stopped_lane_frees_its_slot_and_a_stop_that_met_a_busy_lock_lands_at_the_next_hook(self):
        t0 = time.time() - 3600
        pg.start("Work", plan=["ciwatch reports once", "parser"], now=t0)
        pg.assign("ciwatch reports once", "Link", now=t0)
        pg.assign("parser", "Neo", now=t0)
        launch("a_link", "devkit:link", "Link: ciwatch reports once", at=t0 + 60)
        launch("a_neo", "devkit:neo", "Neo: parser", at=t0 + 90)
        self.assertEqual(sorted(k for k, _ in pg.slots(self.run_())), ["a_link", "a_neo"])

        # the tech lead stops the Release engineer: TaskStop's result reaches the PostToolUse hook
        self.hook("post-tool", {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "TaskStop",
                                "tool_use_id": "toolu_stop", "tool_input": {"task_id": "a_link"},
                                "tool_response": {"message": "Successfully stopped task: a_link (Link: ciwatch reports "
                                                             "once)", "task_id": "a_link", "task_type": "local_agent",
                                                  "command": "Link: ciwatch reports once"}, "cwd": str(self.root)})
        with self.subTest("the stopped lane ends, frees its slot, and its step waits for a new launch"):
            run = self.run_()
            step = next(s for s in run["plan"] if s["name"] == "ciwatch reports once")
            self.assertEqual(run["agents"]["a_link"]["state"], "stopped")
            self.assertEqual((step["state"], step["owner"]), ("pending", "main"))
            self.assertEqual([k for k, _ in pg.slots(run)], ["a_neo"])

        # the Senior engineer's SubagentStop arrives while another hook process holds the session's records
        held, release = threading.Event(), threading.Event()

        def hold():
            with state.hold(pg.session_path("claude", "s1")):
                held.set()
                release.wait(10)
        t = threading.Thread(target=hold)
        t.start()
        self.addCleanup(t.join)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(10))
        with mock.patch.object(state, "WAIT", 0.05), mock.patch.object(state, "HOOK_WAIT", 0.05):
            try:
                pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a_neo", "agent_type": "devkit:neo",
                                          "last_assistant_message": NEO_REPORT}, False, now=t0 + 300)
            except state.StateBusy:
                pass  # the hook fails open on it (hooks.run)
        release.set()
        t.join()
        self.assertEqual(self.run_()["agents"]["a_neo"]["state"], "running")  # not applied while the lock was busy

        self.hook("post-tool", self.bash_done("git status"))  # the session's next hook
        with self.subTest("the stop that met the busy lock is applied at the next hook, once"):
            run = self.run_()
            step = next(s for s in run["plan"] if s["name"] == "parser")
            self.assertEqual((run["agents"]["a_neo"]["state"], step["state"]), ("done", "done"))
            self.assertEqual(pg.slots(run), [])
            self.assertEqual(len([h for h in run["history"] if h["step"] == "parser" and h["to"] == "done"]), 1)


# --------------------------------------------------------------------------- CI watches


class CiWatchTests(HookSandbox):
    # Each push, PR and merge started its own watch of one commit, and each told the session the same result; a
    # cancelled run was reported as passed; and a branch only this Mac had started a watch of a run that never came.
    def setUp(self):
        super().setUp()
        self.w = Sandbox(self)
        w = self.w
        w.origin = w.base / "origin.git"
        git(w.base, "init", "-q", "--bare", "-b", "main", str(w.origin))
        w.repo = w.base / "widget"
        git(w.base, "init", "-q", "-b", "main", str(w.repo))
        git(w.repo, "remote", "add", "origin", str(w.origin))
        (w.repo / "README.md").write_text("# widget\n")
        git(w.repo, "add", "-A")
        git(w.repo, "commit", "-q", "-m", "chore: start")
        git(w.repo, "push", "-q", "-u", "origin", "main")
        self.host = {}  # branch -> the runs the CI host lists for it
        self.started = []

        def runs_for(top, branch=None, limit=5, detail=True, max_age=20):
            return {"provider": "github", "repo": PROJECT, "branch": branch, "runs": list(self.host.get(branch, []))}

        def start(top, session="", branch="", sha=""):
            self.started.append((branch or None, (sha or "")[:7] or None))
            return True
        s = w.stack
        s.enter_context(mock.patch.object(cistatus, "runs_for", runs_for))
        s.enter_context(mock.patch.object(ciwatch, "failed_log", lambda *a, **k: ""))  # no CI host to read logs from
        s.enter_context(mock.patch.object(ciwatch, "start_background", start))
        s.enter_context(mock.patch.object(core, "is_temp", lambda path: False))  # the test's repository is a real one
        for name in ("WATCH", "REPORTED", "LIVE"):
            s.enter_context(mock.patch.object(ciwatch, name, w.base / "ci" / name.lower(), create=True))
        os.chdir(w.repo)

    def run_on(self, branch, sha, status, rid):
        return {"id": rid, "number": rid - 100, "name": "CI", "status": status, "conclusion": status, "sha": sha,
                "url": f"https://github.com/acme/widget/actions/runs/{rid}", "elapsed": 300,
                "started": time.time() - 600, "branch": branch}

    def commit(self, text):
        (self.w.repo / "notes.txt").write_text(text)
        git(self.w.repo, "add", "-A")
        git(self.w.repo, "commit", "-q", "-m", text)
        return git(self.w.repo, "rev-parse", "HEAD")

    def watch(self, branch, sha):
        # what the background watcher runs: dev ci watch --quiet --branch B --sha S --session claude:s1
        return dev("ci", "watch", "--quiet", "--branch", branch, "--sha", sha[:7], "--session", "claude:s1")

    def test_a_commit_is_reported_once_a_cancelled_run_never_passes_and_an_unpushed_branch_is_not_watched(self):
        git(self.w.repo, "switch", "-q", "-c", "feat/x")
        sha = self.commit("feature")
        git(self.w.repo, "push", "-q", "-u", "origin", "feat/x")
        self.host["feat/x"] = [self.run_on("feat/x", sha, "success", 101)]
        codes = [self.watch("feat/x", sha)[0] for _ in ("push", "pr create", "pr merge")]
        told = ciwatch.pending_events("claude", "s1")
        with self.subTest("three watches of one commit tell the session once"):
            self.assertEqual(codes, [0, 0, 0])
            self.assertEqual(told.count("devkit · CI"), 1, told)
            self.assertIn(f"on feat/x @ {sha[:7]}", told)
            self.assertIn("CI #1 passed", told)
            self.assertEqual(ciwatch.pending_events("claude", "s1"), "")

        cancelled = self.commit("more")
        git(self.w.repo, "push", "-q", "origin", "feat/x")
        self.host["feat/x"] = [self.run_on("feat/x", cancelled, "cancelled", 102)]
        rc, out, _ = self.watch("feat/x", cancelled)
        told = ciwatch.pending_events("claude", "s1")
        with self.subTest("a cancelled run is never reported as passed"):
            self.assertNotEqual(rc, 0, out)
            self.assertIn("cancelled", told)
            self.assertNotIn("passed", told)
            self.assertNotIn("passed", out)

        git(self.w.repo, "switch", "-q", "-c", "feat/local")
        local = self.commit("local only")
        self.hook("post-tool", self.bash_done("dev pr create --fill", cwd=self.w.repo, tid="toolu_pr"))
        with self.subTest("a branch only this Mac has starts no watch"):
            self.assertEqual(self.started, [])
        git(self.w.repo, "push", "-q", "-u", "origin", "feat/local")
        self.hook("post-tool", self.bash_done("git push -u origin feat/local", cwd=self.w.repo, tid="toolu_push"))
        with self.subTest("the same branch, once pushed, starts one watch of the commit the remote has"):
            self.assertEqual(self.started, [("feat/local", local[:7])])


# --------------------------------------------------------------------------- the installed configuration


class OlderCopyTests(unittest.TestCase):
    # A session started before `dev install` pinned a newer release still runs the older copy's hooks. On a config
    # change, that copy saw the newer settings as drift and put its own back: settings, CLAUDE.md, AGENTS.md and the
    # git ignore. The pinned release is 26.11.5; the older copy runs 26.11.4. A background `dev config repair` runs the
    # copy it names: the copy of the running release runs here, in this process; another release is another program
    # and is only recorded.
    PINNED = "26.11.5"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve() / "home"
        self.claude, self.codex = self.home / ".claude", self.home / ".codex"
        for d in (self.claude, self.codex, self.home / "state" / "logs"):
            d.mkdir(parents=True)
        self.settings = self.claude / "settings.json"
        self.gitconfig = self.home / ".gitconfig"
        self.gitconfig.write_text("")
        self.spawned = []
        st = self.home / "state"
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for module, name, value in ((core, "HOME", self.home), (core, "CLAUDE_HOME", self.claude),
                                    (core, "CODEX_HOME", self.codex), (setup, "CLAUDE_HOME", self.claude),
                                    (setup, "CODEX_HOME", self.codex), (policy, "CLAUDE_HOME", self.claude),
                                    (policy, "CODEX_HOME", self.codex), (setup, "STATE", st), (policy, "STATE", st),
                                    (hooks, "STATE", st), (audit, "AUDIT", self.home / "audit"),
                                    (core, "HANDOFF", self.home / "handoff"),
                                    (core, "gh_user", lambda: {"login": "ana", "name": "Ana Dev", "id": 7}),
                                    (setup, "sh", self.sh), (hooks, "subprocess", Spawner(self.popen))):
            patch_if(stack, module, name, value)
        stack.enter_context(mock.patch.dict(core.GIT_ENV, dict(ISOLATE, GIT_CONFIG_GLOBAL=str(self.gitconfig))))
        stack.enter_context(mock.patch.dict(core.CFG.setdefault("agents", {}), {"codex": False}))
        saved = (state.WAIT, state.SAY_BUSY)
        self.addCleanup(lambda: setattr(state, "WAIT", saved[0]) or setattr(state, "SAY_BUSY", saved[1]))
        self.pin = self.pin_release(self.PINNED)

    def pin_release(self, version):
        # a pinned copy as `dev install` leaves it: a folder of the releases folder with its manifest, named by
        # install.json with the manifest's checksum
        import hashlib
        share = self.home / ".local" / "share" / "devkit"
        copy_ = share / "releases" / f"{version}-abc1234"
        copy_.mkdir(parents=True)
        manifest = copy_ / core.PIN_MANIFEST
        manifest.write_text(json.dumps({"name": copy_.name, "version": version, "commit": "abc1234", "files": {}}))
        (share / "install.json").write_text(json.dumps({
            "name": copy_.name, "version": version, "commit": "abc1234", "path": str(copy_),
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()}))
        return copy_

    def sh(self, argv, **kw):
        if list(argv)[:1] == ["gh"]:  # gh auth setup-git: no GitHub here
            return 0, "", ""
        return core.sh(argv, **kw)

    def popen(self, argv, *a, **kw):
        # a background `dev …` (hooks._spawn): [python, -I, -S, <copy>/bin/dev, *args]
        self.spawned.append(list(argv))
        copy_ = Path(argv[3]).parent.parent
        version = core.VERSION if copy_.resolve() == Path(core.DEVKIT_HOME).resolve() else \
            json.loads((copy_ / core.PIN_MANIFEST).read_text()).get("version")
        if version == core.VERSION and list(argv[4:6]) == ["config", "repair"]:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                policy.repair(quiet=True, include_codex=False)
        return mock.Mock(pid=4242)

    def managed(self) -> dict:
        flat, commands = policy.claude_desired()
        out = {}
        for path, v in flat.items():
            d = out
            for k in path[:-1]:
                d = d.setdefault(k, {})
            d[path[-1]] = v
        out["hooks"] = {ev: [{"hooks": [{"type": "command", "command": c} for c in cs]}] for ev, cs in commands.items()}
        return out

    def plant(self) -> dict:
        # what the newer install left, as an older copy sees it: a DEVKIT_* variable it does not know, instruction
        # files it did not write, and a git ignore of another copy
        s = self.managed()
        s["env"] = dict(s.get("env") or {}, DEVKIT_GUARD="off")
        self.settings.write_text(json.dumps(s, indent=2))
        (self.claude / "CLAUDE.md").write_text("# rules of the newer install\n")
        (self.codex / "AGENTS.md").write_text("# rules of the newer install\n")
        git(self.home, "config", "--global", "core.excludesFile", "/opt/newer/config/git/ignore")
        return self.files()

    def files(self) -> dict:
        return {"settings": self.settings.read_text(), "CLAUDE.md": (self.claude / "CLAUDE.md").read_text(),
                "AGENTS.md": (self.codex / "AGENTS.md").read_text(),
                "excludesFile": core.sh(["git", "config", "--global", "core.excludesFile"])[1]}

    def config_change(self) -> int:
        payload = {"session_id": "s1", "hook_event_name": "ConfigChange", "source": "user_settings",
                   "file_path": str(self.settings)}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                hooks.run("config-change", "claude")
            except SystemExit as e:
                return e.code
        return 0

    def test_an_older_copy_leaves_the_newer_install_alone_and_the_pinned_release_repairs_the_same_change(self):
        with self.subTest("the older copy changes no managed file and records the skip"):
            with mock.patch.object(core, "VERSION", "26.11.4"):
                before = self.plant()
                self.config_change()
                self.assertEqual(self.files(), before)
                skipped = audit.records(1, action="config.repair-skipped")
                self.assertTrue(skipped, "no config.repair-skipped record")
                self.assertIn("26.11.4", skipped[-1]["detail"])
                self.assertIn(self.PINNED, skipped[-1]["detail"])
                self.assertEqual([a[3:6] for a in self.spawned], [[str(self.pin / "bin" / "dev"), "config", "repair"]])
        self.spawned.clear()
        with self.subTest("the pinned release repairs the same change"):
            with mock.patch.object(core, "VERSION", self.PINNED):
                before = self.plant()
                self.assertEqual(self.config_change(), 2)  # the DEVKIT_* variable is refused
                after = self.files()
                self.assertNotIn("DEVKIT_GUARD", (json.loads(after["settings"]).get("env") or {}))
                for name in ("CLAUDE.md", "AGENTS.md"):
                    self.assertNotEqual(after[name], before[name], name)
                    self.assertIn(setup.GENERATED_MARK, after[name][:200], name)
                self.assertNotEqual(after["excludesFile"], before["excludesFile"])
                self.assertTrue(after["excludesFile"].endswith("/config/git/ignore"), after["excludesFile"])


if __name__ == "__main__":
    unittest.main()
