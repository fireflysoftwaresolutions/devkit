# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The batched handoff pusher: a background `dev sync` commits and pushes the handoff repository only when the push is
# due (sync.push_interval seconds after the last one), one detached `dev sync --flush-at <epoch>` per Mac pushes what
# waited on time, urgent changes (an embargo, a session's end, a sync a person runs) push at once, a file over 45 MB is
# never committed, and `git gc --auto` runs only in the scheduled sync. Real git repositories in a temporary folder: a
# bare remote and two clones (tests/test_work.py's TwoMacs); the flusher's start is recorded, never run.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_handoff_push
import argparse
import contextlib
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

TREE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TREE / "lib"))
sys.path.insert(0, str(TREE / "tests"))

from devkit import audit, branches, cli, core, dashboard, hooks, registry, work  # noqa: E402
from test_work import A, TwoMacs, _git  # noqa: E402

MB = 1024 * 1024


class _Started:
    # What subprocess.Popen returns for a recorded flusher: this process's pid, so it counts as alive.
    pid = os.getpid()


class Mac(TwoMacs):
    # TwoMacs with sync.push_interval set, every audit record kept, the notes folder and cli's state in the test's
    # folder, and the detached flusher's command line recorded instead of started.
    def __init__(self, interval=300):
        self.interval = interval

    def __enter__(self):
        super().__enter__()
        s = self.stack
        s.enter_context(mock.patch.dict(core.CFG["sync"], {"push_interval": self.interval}))
        s.enter_context(mock.patch.dict(core.CFG, {"audit": dict(json.loads(json.dumps(core.DEFAULTS["audit"])),
                                                                 level="all")}))
        for mod in (core, registry):
            s.enter_context(mock.patch.object(mod, "NOTES", self.here / "projects"))
        s.enter_context(mock.patch.object(registry, "HANDOFF", self.here))
        s.enter_context(mock.patch.object(cli, "STATE", core.STATE))
        self.spawned, real = [], subprocess.Popen

        def popen(args, *a, **k):
            words = [str(x) for x in args]
            if "--flush-at" in words:
                self.spawned.append(words)
                return _Started()
            return real(args, *a, **k)

        s.enter_context(mock.patch.object(subprocess, "Popen", popen))
        return self

    def change(self, rel="notes/today.md", text="a change\n"):
        f = self.here / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        return f

    def on_remote(self, rel):
        # the file as the bare remote has it on main, or None
        rc, out, _ = core.git(self.base / "remote.git", "show", f"main:{rel}")
        return out if rc == 0 else None

    def remote_commits(self) -> int:
        return int(_git(self.base / "remote.git", "rev-list", "--count", "main"))

    def pending(self) -> tuple:
        # (files not committed, commits not pushed) in this Mac's clone
        return (_git(self.here, "status", "--porcelain"), _git(self.here, "rev-list", "--count", "@{u}..HEAD"))

    def pushes(self, outcome=None) -> list:
        recs = []
        for f in sorted(Path(audit.AUDIT).rglob("*.jsonl")):
            recs += [r for r in map(json.loads, f.read_text().splitlines()) if r.get("action") == "handoff.push"]
        return [r for r in recs if outcome is None or r.get("outcome") == outcome]

    def flush_at(self, words) -> float:
        # the epoch of a recorded flusher, parsed by dev's own parser
        a = cli.build_parser().parse_args(words[words.index("sync"):])
        self.assertIs(a.fn, cli.cmd_sync)
        return a.flush_at

    def assertIs(self, a, b):  # noqa: N802 (the fixture is not a TestCase)
        if a is not b:
            raise AssertionError(f"{a!r} is not {b!r}")


def sync(**kw) -> argparse.Namespace:
    # the arguments of a `dev sync` (the hook's background run unless told otherwise)
    base = dict(session="claude:s1", finish=False, offline=True, scheduled=False, no_push=False, quiet=True,
                renew=None, delay=0, flush_at=0)
    base.update(kw)
    return argparse.Namespace(**base)


class FakeClock:
    # A clock that a sleep moves forward; it records each sleep and what the remote had when it began.
    def __init__(self, start, seen=None):
        self.t, self.slept, self.seen = start, [], seen

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        if self.seen:
            self.seen()
        self.t += s


class DeferredPushTests(unittest.TestCase):
    def test_ten_background_syncs_inside_the_interval_make_one_push_and_start_one_flusher(self):
        # The first sync pushes; the nine after it, inside push_interval, commit and push nothing, leave their changes
        # in the working tree, and only the first of them starts a flusher, set for the interval after that push.
        with Mac(interval=300) as m:
            before = m.remote_commits()
            t0 = time.time()
            results = []
            for i in range(10):
                m.change(text=f"change {i}\n")
                results.append(cli.commit_push(quiet=True))
            t1 = time.time()
            self.assertEqual(results, ["pushed"] + ["deferred"] * 9)
            self.assertEqual(m.remote_commits(), before + 1)
            self.assertEqual(m.on_remote("notes/today.md"), "change 0")
            self.assertEqual(len(m.pushes("ok")), 1)
            dirty, ahead = m.pending()
            self.assertEqual((dirty, ahead), (" M notes/today.md", "0"))
            self.assertEqual(len(m.spawned), 1, m.spawned)
            at = m.flush_at(m.spawned[0])
            self.assertTrue(t0 + 300 <= at <= t1 + 301, (t0, at, t1))

    def test_push_interval_zero_pushes_on_every_sync(self):
        # sync.push_interval 0 keeps the old behavior: every sync commits and pushes, and no flusher starts.
        with Mac(interval=0) as m:
            before = m.remote_commits()
            results = []
            for i in range(3):
                m.change(text=f"change {i}\n")
                results.append(cli.commit_push(quiet=True))
            self.assertEqual(results, ["pushed"] * 3)
            self.assertEqual(m.remote_commits(), before + 3)
            self.assertEqual(m.spawned, [])

    def test_a_push_to_another_handoff_repository_does_not_delay_this_ones(self):
        # handoff_dir moved (or another repository was pushed a moment ago): the interval counts this repository's
        # own pushes, so its first sync pushes at once.
        with Mac() as m:
            other = m.base / "elsewhere" / ".handoff"
            _git(m.base, "clone", "-q", str(m.base / "remote.git"), str(other))
            for k, v in (("user.name", "Ana Dev"), ("user.email", "ana@example.com")):
                _git(other, "config", k, v)
            with mock.patch.object(work, "HANDOFF", other):
                (other / "x.md").write_text("x\n")
                self.assertEqual(cli.commit_push(quiet=True), "pushed")
            m.change()
            self.assertEqual(cli.commit_push(quiet=True), "pushed")
            self.assertEqual(m.spawned, [])

    def test_a_sync_with_nothing_to_push_inside_the_interval_starts_no_flusher(self):
        # Inside the interval, a sync that finds nothing new neither commits nor starts a flusher.
        with Mac() as m:
            m.change()
            self.assertEqual(cli.commit_push(quiet=True), "pushed")
            self.assertEqual(cli.commit_push(quiet=True), "nothing")
            self.assertEqual(m.spawned, [])


class FlushTests(unittest.TestCase):
    def defer(self, m):
        # one pushed sync, then a change that waits -> the flusher's epoch
        m.change(text="first\n")
        self.assertEqual(cli.commit_push(quiet=True), "pushed")
        m.change(text="second\n")
        self.assertEqual(cli.commit_push(quiet=True), "deferred")
        return m.flush_at(m.spawned[-1])

    def test_the_flusher_sleeps_until_its_time_then_pushes_what_waited(self):
        with Mac() as m:
            at = self.defer(m)
            start = time.time()
            seen = []
            clock = FakeClock(start, lambda: seen.append(m.on_remote("notes/today.md")))
            self.assertEqual(cli.flush_handoff(at, quiet=True, clock=clock.now, sleep=clock.sleep), "pushed")
            self.assertEqual(seen, ["first"])  # nothing went out before it slept
            self.assertAlmostEqual(sum(clock.slept), at - start, places=3)
            self.assertEqual(m.on_remote("notes/today.md"), "second")
            self.assertEqual(m.pending(), ("", "0"))
            self.assertEqual(len(m.pushes("ok")), 2)

    def test_dev_sync_flush_at_runs_the_flush(self):
        # The command line the flusher starts with: with its time already past it pushes at once.
        with Mac() as m:
            self.defer(m)
            a = cli.build_parser().parse_args(["sync", "--quiet", "--flush-at", str(int(time.time()) - 1)])
            self.assertEqual(a.fn(a), 0)
            self.assertEqual(m.on_remote("notes/today.md"), "second")
            self.assertEqual(m.pending(), ("", "0"))

    def test_the_flusher_pushes_under_the_sync_lock(self):
        # A full `dev sync` writes the handoff working tree, then commits and pushes it, under the sync lock: the
        # flusher's push takes that lock too, so it never commits a sync's files half written
        with Mac() as m:
            self.defer(m)
            held, real = [], cli.commit_push

            def push(**kw):
                with core.Lock("sync", wait=0) as free:
                    held.append(not free)
                return real(**kw)
            with mock.patch.object(cli, "commit_push", push):
                self.assertEqual(cli.flush_handoff(time.time() - 1, quiet=True, sleep=lambda s: None), "pushed")
            self.assertEqual(held, [True])
            self.assertEqual(m.on_remote("notes/today.md"), "second")

    def test_a_flusher_that_ends_its_rounds_with_changes_waiting_leaves_no_flusher_recorded(self):
        # The remote does not answer for all its rounds, so changes still wait when it ends. It clears its record on
        # the way out: the next deferring sync starts a new flusher at once, instead of trusting a pid that may now
        # be another process's for up to FLUSH_GRACE (the test's own pid stands for one)
        with Mac() as m, mock.patch.object(cli, "FLUSH_ROUNDS", 2):
            at = self.defer(m)
            _git(m.here, "remote", "set-url", "origin", str(m.base / "unreachable.git"))
            clock = FakeClock(time.time())
            self.assertEqual(cli.flush_handoff(at, quiet=True, clock=clock.now, sleep=clock.sleep), "failed")
            st = cli._push_state(m.here)
            self.assertEqual((st.get("flusher"), st.get("flush_at")), (None, None))
            m.change(text="third\n")
            self.assertEqual(cli.commit_push(quiet=True), "deferred")
            self.assertEqual(len(m.spawned), 2)

    def test_two_flushers_started_together_push_once(self):
        # The second flusher finds the first one waiting and leaves at once; the first one pushes.
        with Mac() as m:
            at = self.defer(m)
            entered, go, out = threading.Event(), threading.Event(), {}

            def wait(_seconds):
                entered.set()
                go.wait(30)

            def first():
                out["first"] = cli.flush_handoff(at, quiet=True, sleep=wait)

            t = threading.Thread(target=first)
            t.start()
            self.assertTrue(entered.wait(30))
            out["second"] = cli.flush_handoff(at, quiet=True, sleep=lambda s: self.fail("the second flusher waited"))
            go.set()
            t.join(60)
            self.assertEqual(out, {"first": "pushed", "second": "nothing"})
            self.assertEqual(len(m.pushes("ok")), 2)  # the deferring sync's push and the flush
            self.assertEqual(m.on_remote("notes/today.md"), "second")


class UrgentPushTests(unittest.TestCase):
    def project(self, m) -> Path:
        repo = m.base / "api"
        _git(m.base, "init", "-q", "-b", "main", str(repo))
        _git(repo, "remote", "add", "origin", "https://github.com/acme/api.git")
        return repo

    def test_an_embargo_set_and_lifted_reaches_the_remote_at_once(self):
        # Inside the interval a background sync waits, but an embargo and its lift are on the remote when the
        # command returns: another Mac reads them before its next backup.
        with Mac() as m:
            m.change()
            self.assertEqual(cli.commit_push(quiet=True), "pushed")
            repo, cwd = self.project(m), os.getcwd()
            os.chdir(repo)
            try:
                args = argparse.Namespace(branch="fix/parser-hole", why="the fix discloses an open hole", until="",
                                          lift=False, json=False)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(branches.cmd_embargo(args), 0)
                note = m.on_remote("projects/acme/api.md") or ""
                self.assertIn("fix/parser-hole", note)
                self.assertIn("the fix discloses an open hole", note)
                args.lift = True
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(branches.cmd_embargo(args), 0)
            finally:
                os.chdir(cwd)
            self.assertNotIn("fix/parser-hole", m.on_remote("projects/acme/api.md") or "")
            self.assertEqual(m.pending(), ("", "0"))
            self.assertEqual(m.spawned, [])

    def test_the_sync_that_ends_a_session_pushes_what_waited(self):
        # A session's turns leave a change waiting; the sync that ends the session pushes it, so nothing stays only
        # on this Mac when no further hook runs.
        with Mac() as m, \
                mock.patch.object(hooks, "record_session", lambda s: None), \
                mock.patch.object(registry, "build", lambda **kw: {"projects": [], "sessions": []}), \
                mock.patch.object(registry, "write_outputs", lambda reg: []), \
                mock.patch.object(dashboard, "write", lambda reg: None):
            m.change(text="first\n")
            self.assertEqual(cli.cmd_sync(sync()), 0)
            m.change(text="second\n")
            self.assertEqual(cli.cmd_sync(sync()), 0)
            self.assertEqual(m.on_remote("notes/today.md"), "first")
            self.assertEqual(cli.cmd_sync(sync(finish=True)), 0)
            self.assertEqual(m.on_remote("notes/today.md"), "second")
            self.assertEqual(m.pending(), ("", "0"))

    def test_a_sync_a_person_runs_pushes_at_once_and_a_queued_one_waits(self):
        # `dev sync` typed by a person (no session) does not wait for the interval; a queued background run does.
        with Mac() as m:
            m.change(text="first\n")
            self.assertEqual(cli._sync_handoff(sync()), "pushed")
            m.change(text="second\n")
            self.assertEqual(cli._sync_handoff(sync(session=None, delay=10)), "deferred")
            self.assertEqual(cli._sync_handoff(sync(session=None)), "pushed")
            self.assertEqual(m.on_remote("notes/today.md"), "second")


class WaitingChangeTests(unittest.TestCase):
    # A background sync leaves this Mac's generated view uncommitted for up to push_interval, and the other Mac pushed
    # its own version of the same view meanwhile. A claim, a release and a done pull, then push at once: what waits
    # goes into a commit before the pull, so the pull settles the view as it settles any committed file instead of
    # refusing to move this Mac's checkout, and the urgent push carries the batched change.
    def view_waits_while_the_other_mac_changes_it(self, m):
        m.change("TASKS.md", "base\n")
        self.assertEqual(cli.commit_push(quiet=True, urgent=True), "pushed")
        _git(m.other, "pull", "-q")
        m.change("TASKS.md", "this Mac's view\n")
        self.assertEqual(cli.commit_push(quiet=True), "deferred")
        (m.other / "TASKS.md").write_text("the other Mac's view\n")
        _git(m.other, "commit", "-qam", "sync: 1 file(s) from the other Mac")
        _git(m.other, "push", "-q")

    def on_remote(self, m, iid) -> dict:
        rel = m.item(iid)["path"].relative_to(m.here)
        return work.parse(m.on_remote(str(rel)) + "\n")["meta"]

    def test_a_claim_reaches_the_remote_with_the_change_a_background_sync_left_waiting(self):
        with Mac(interval=300) as m, contextlib.redirect_stderr(io.StringIO()):
            iid = m.shared_item("Export invoices")
            self.view_waits_while_the_other_mac_changes_it(m)
            it = work.claim(m.item(iid), a=A, why="start")
            self.assertTrue(it["meta"]["claimed_by"].startswith("ana@mbp1"))
            self.assertTrue(self.on_remote(m, iid)["claimed_by"].startswith("ana@mbp1"))
            self.assertEqual(m.pending(), ("", "0"))  # the waiting view went out with the claim
            self.assertEqual(m.stopped(), [], m.problem())

    def test_a_pull_alone_commits_the_waiting_view_and_moves_the_checkout(self):
        with Mac(interval=300) as m, contextlib.redirect_stderr(io.StringIO()):
            self.view_waits_while_the_other_mac_changes_it(m)
            self.assertTrue(work.pull())
            self.assertEqual(m.pending()[0], "")
            self.assertIn("the other Mac's view", _git(m.here, "log", "-p", "--format=%s", "-3", "--", "TASKS.md"))

    def test_an_item_another_command_wrote_and_not_yet_committed_keeps_its_own_commit(self):
        # an urgent pull committed everything that changed, so a writer's item went into "sync: …", and the writer's
        # own commit, with its message and audit record, found nothing left to commit
        with Mac(interval=300) as m, contextlib.redirect_stderr(io.StringIO()):
            iid = m.shared_item("Export invoices")
            self.view_waits_while_the_other_mac_changes_it(m)
            it = m.item(iid)
            it["meta"]["priority"] = "high"
            work.write_record(it["path"], work.render(it))  # written; its commit comes next
            self.assertTrue(work.pull())
            work._commit([it["path"]], f"{iid}: edit priority high (ana)")
            rel = str(it["path"].relative_to(m.here))
            self.assertEqual(_git(m.here, "log", "-1", "--format=%s", "--", rel), f"{iid}: edit priority high (ana)")
            self.assertEqual(work.writing(m.here), set())

    def test_a_record_written_while_a_sync_stages_stays_out_of_its_commit(self):
        with Mac(interval=300) as m, contextlib.redirect_stderr(io.StringIO()):
            view = m.change("TASKS.md", "this Mac's view\n")
            item = m.change("items/acme/api/T-late.md", "a record its writer has not committed yet\n")
            seen = iter([set(), {"items/acme/api/T-late.md"}])  # marked between the first look and the staging
            with mock.patch.object(work, "writing", lambda root: next(seen, set())):
                self.assertEqual(cli.commit_push(quiet=True, urgent=True), "pushed")
            self.assertIsNotNone(m.on_remote("TASKS.md"))
            self.assertIsNone(m.on_remote("items/acme/api/T-late.md"))
            self.assertIn("?? items/", m.pending()[0])  # still the writer's to commit
            self.assertTrue(view.exists() and item.exists())

    def test_a_release_and_a_done_reach_the_remote_while_a_view_waits(self):
        with Mac(interval=300) as m, contextlib.redirect_stderr(io.StringIO()):
            iid = m.shared_item("Export invoices")
            work.claim(m.item(iid), a=A, why="start")
            self.view_waits_while_the_other_mac_changes_it(m)
            work.release(m.item(iid), a=A, why="later", status="todo")
            self.assertEqual((self.on_remote(m, iid)["claimed_by"], self.on_remote(m, iid)["status"]), ("", "todo"))
            self.assertEqual(m.pending(), ("", "0"))
            work.claim(m.item(iid), a=A, why="again")
            m.change("TASKS.md", "this Mac's next view\n")
            self.assertEqual(cli.commit_push(quiet=True), "deferred")
            (m.other / "TASKS.md").write_text("the other Mac's next view\n")
            _git(m.other, "pull", "-q")
            _git(m.other, "commit", "-qam", "sync: 1 file(s) from the other Mac")
            _git(m.other, "push", "-q")
            work.release(m.item(iid), a=A, why="shipped", status="done")
            self.assertEqual(self.on_remote(m, iid)["status"], "done")
            self.assertEqual(m.pending(), ("", "0"))
            self.assertEqual(m.stopped(), [], m.problem())


class RegistryCacheTests(unittest.TestCase):
    # Each Mac keeps its projects map (registry.json) in devkit's state folder. A Mac still on 26.11.19 commits its
    # handoff copy on every sync; this Mac neither writes nor untracks that copy, so the file never goes back and forth
    # between the two, and this Mac's readers keep this Mac's map whatever the other Mac pushes.
    def test_an_older_mac_that_commits_registry_json_changes_neither_this_macs_map_nor_its_history(self):
        from devkit import completion
        mine = {"host": "here", "generated": 1794900000.0, "issues": [], "projects": [], "sessions": []}
        with Mac(interval=0) as m, contextlib.redirect_stderr(io.StringIO()):
            (m.here / "registry.json").write_text('{"host": "here-v1", "projects": []}\n')  # as 26.11.19 left it
            _git(m.here, "add", "registry.json")
            _git(m.here, "commit", "-qm", "sync: 1 file(s) from here")
            _git(m.here, "push", "-q")
            _git(m.other, "pull", "-q")
            upgraded = _git(m.here, "rev-parse", "HEAD")
            registry.write_outputs(mine)
            self.assertEqual(cli.commit_push(quiet=True), "pushed")
            with m.on_other():  # the older devkit rewrites its copy, commits it as every sync did, pulls and pushes
                (m.other / "registry.json").write_text('{"host": "other-v2", "projects": []}\n')
                _git(m.other, "add", "-A")
                _git(m.other, "commit", "-qm", "sync: 1 file(s) from other")
                self.assertEqual(work.pull_rebase(), (True, ""))
                self.assertEqual(work.push_handoff(), (True, ""))
            self.assertTrue(work.pull())
            registry.write_outputs(dict(mine, generated=mine["generated"] + 60))
            self.assertIn(cli.commit_push(quiet=True), ("pushed", "nothing"))
            self.assertEqual(completion._registry()["host"], "here")
            self.assertEqual(json.loads((m.here / "registry.json").read_text())["host"], "other-v2")
            self.assertEqual(_git(m.here, "log", "--format=%an", f"{upgraded}..HEAD", "--", "registry.json"),
                             "Bob Dev")  # only the older Mac's commit changed it
            self.assertEqual(m.pending(), ("", "0"))


class CommitGateTests(unittest.TestCase):
    def test_a_file_over_45_mb_is_held_and_recorded_once_while_the_rest_is_pushed(self):
        # A runaway writer leaves a 46 MB file in the handoff repository: it is neither committed nor pushed (GitHub
        # would refuse every later push), the sync pushes the other change, and the audit trail names the file once.
        with Mac() as m:
            big = m.here / "audit" / "ana@mbp1" / "2026-11.jsonl"
            big.parent.mkdir(parents=True)
            with big.open("wb") as fh:
                fh.truncate(46 * MB)
            m.change(text="small\n")
            self.assertEqual(cli.commit_push(quiet=True, urgent=True), "pushed")
            self.assertEqual(m.on_remote("notes/today.md"), "small")
            self.assertIsNone(m.on_remote("audit/ana@mbp1/2026-11.jsonl"))
            self.assertEqual(_git(m.here, "ls-files", "audit"), "")
            self.assertEqual(big.stat().st_size, 46 * MB)  # still here, for the developer to deal with
            held = m.pushes("blocked")
            self.assertEqual([(r["target"], r["severity"]) for r in held],
                             [("audit/ana@mbp1/2026-11.jsonl", "critical")])
            m.change(text="smaller\n")
            self.assertEqual(cli.commit_push(quiet=True, urgent=True), "pushed")
            self.assertEqual(len(m.pushes("blocked")), 1)
            self.assertIsNone(m.on_remote("audit/ana@mbp1/2026-11.jsonl"))


class MaintenanceTests(unittest.TestCase):
    def objects(self, root) -> dict:
        out = _git(root, "count-objects", "-v")
        return {k: int(v) for k, v in (ln.split(": ") for ln in out.splitlines()) if k in ("count", "packs")}

    def test_git_gc_auto_runs_in_the_scheduled_sync_and_in_no_other(self):
        # Two packs and loose objects, with gc.autoPackLimit 1: any `git gc --auto` would pack them, and git's own
        # maintenance after a commit or fetch would pack the loose ones (its loose-objects task, set to act at one).
        # A hook's sync (commit, fetch and push) leaves them as they are; the scheduled sync packs them after its push.
        with Mac() as m:
            for k, v in (("gc.autoPackLimit", "1"), ("gc.autoDetach", "false"), ("maintenance.autoDetach", "false"),
                         ("maintenance.loose-objects.enabled", "true"), ("maintenance.loose-objects.auto", "1")):
                _git(m.here, "config", k, v)
            _git(m.here, "repack", "-q")
            m.change(text="packed\n")
            _git(m.here, "add", "-A")
            _git(m.here, "-c", "gc.auto=0", "-c", "maintenance.auto=false", "commit", "-qm", "two packs")
            _git(m.here, "repack", "-q")
            m.change(text="hook\n")
            self.assertEqual(cli._sync_handoff(sync()), "pushed")
            after_hook = self.objects(m.here)
            self.assertEqual(after_hook["packs"], 2)
            self.assertGreater(after_hook["count"], 0)
            m.change(text="hourly\n")
            self.assertEqual(cli._sync_handoff(sync(session=None, scheduled=True)), "pushed")
            self.assertEqual(self.objects(m.here)["count"], 0)
            self.assertEqual(m.on_remote("notes/today.md"), "hourly")


class ConfigTests(unittest.TestCase):
    def test_push_interval_is_saved_as_whole_seconds_up_to_an_hour_and_read_by_the_gate(self):
        # `dev config set sync.push_interval` refuses anything but a whole number of seconds from 0 to 3,600 and leaves
        # the saved file as it was; the value it saves is the effective one, and the gate reads it.
        with tempfile.TemporaryDirectory() as d, mock.patch.object(audit, "AUDIT", Path(d) / "audit"):
            path = Path(d) / "config.json"
            path.write_text("{}\n")
            self.assertEqual(core.config_snapshot(path)[0]["sync"]["push_interval"], 300)
            for bad in ("-1", "3601", "true", "1.5", '"300"'):
                with self.subTest(value=bad):
                    before = path.read_bytes()
                    with self.assertRaises(core.ConfigError):
                        core.config_set("sync.push_interval", bad, path=path)
                    self.assertEqual(path.read_bytes(), before)
            core.config_set("sync.push_interval", "120", path=path)
            cfg, errors = core.config_snapshot(path)
            self.assertEqual((errors, cfg["sync"]["push_interval"]), ([], 120))
            with mock.patch.dict(core.CFG["sync"], cfg["sync"]):
                self.assertEqual(cli.push_interval(), 120)


if __name__ == "__main__":
    unittest.main()
