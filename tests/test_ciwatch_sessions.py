# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Several sessions on one CI watch (lib/devkit/ciwatch.py). devkit follows one repository, branch and commit with one
# background watcher; a second session that sends the same commit joins that watcher with a record of its own. The
# watcher's result must reach every session joined to it, Claude Code and Codex alike, each once, and a session's
# record closes only once the session holds the result: a wait that sees the record closed must find the event there.
# A fork and its upstream can hold the same branch and commit in folders of the same name: each watcher, watch record
# and result belongs to its own repository.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_ciwatch_sessions -v
import argparse
import json
import os
import subprocess
import sys
import textwrap
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _hermetic  # noqa: E402

_hermetic.activate()
from devkit import ciwatch, state  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_acceptance_watch import WatchSandbox  # noqa: E402
from test_ciwatch import GIT_ENV, Clock, Flushed, WatchCase, job, run  # noqa: E402
from test_progress import TempState  # noqa: E402

FIXED = {"GIT_AUTHOR_DATE": "2026-11-01T10:00:00Z", "GIT_COMMITTER_DATE": "2026-11-01T10:00:00Z"}


def repository(root: Path, owner: str) -> Path:
    # a checkout of github.com/<owner>/api in <root>/<owner>/api with one empty commit of fixed dates: every owner's
    # checkout holds the same commit in a folder of the same name, as a fork and its upstream do
    top = root / owner / "api"
    top.mkdir(parents=True)
    env = dict(GIT_ENV, **FIXED)
    who = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    for args in (["init", "-q", "-b", "main"], [*who, "commit", "-q", "--allow-empty", "-m", "start"],
                 ["remote", "add", "origin", f"https://github.com/{owner}/api.git"]):
        subprocess.run(["git", "-C", str(top), *args], env=env, check=True, capture_output=True)
    return top


class SessionsCase(WatchCase):
    # devkit's state in a temporary folder, the session s1 (TempState) and the upstream checkout acme/api
    def setUp(self):
        super().setUp()
        self.temp = TempState()
        self.temp.__enter__()
        self.addCleanup(self.temp.__exit__, None, None, None)
        self.root = Path(self.tmp.name)
        self.up = repository(self.root, "acme")
        self.sha = subprocess.run(["git", "-C", str(self.up), "rev-parse", "--short=7", "HEAD"], env=GIT_ENV,
                                  capture_output=True, text=True, check=True).stdout.strip()
        self.spawned = []

    def session(self, agent, sid, with_run=False):
        (pg.SESSIONS / f"{agent}-{sid}.json").write_text(json.dumps({"agent": agent, "session": sid}))
        if with_run:
            with mock.patch.dict(os.environ, {"DEVKIT_SESSION": f"{agent}:{sid}"}):
                pg.start("Release", plan=["ci"])

    def start(self, top, session):
        # the hook after a push: start_background, whose watcher here is this test's own process (alive, never run)
        def popen(*a, **k):
            self.spawned.append(k.get("cwd"))
            return mock.Mock(pid=os.getpid())
        spawn = types.SimpleNamespace(Popen=popen, DEVNULL=subprocess.DEVNULL)
        with mock.patch.object(ciwatch, "subprocess", spawn), mock.patch.dict(os.environ, {"DEVKIT_NO_CI_WATCH": ""}):
            return ciwatch.start_background(str(top), session=session, branch="main", sha=self.sha)

    def host(self, top, status="failed", conclusion="failure"):
        # the CI host's answer for the checkout `top`: one run of the commit
        owner = top.parent.name
        return lambda t, b, **kw: {"provider": "github", "repo": f"{owner}/api", "runs": [
            run(status, sha=self.sha, conclusion=conclusion, rid=f"{owner}-7", jobs=[job("tests", status)])]}

    def ends(self, top, session, status="failed", conclusion="failure", timeout_min=90):
        # what the background watcher runs at its end, as cli.cmd_ci_watch does: the watch, then the delivery to the
        # session named by --session
        clock = Clock()
        res = ciwatch.watch(str(top), branch="main", sha=self.sha, timeout_min=timeout_min,
                            runs_fn=self.host(top, status, conclusion), sleep=clock.sleep, clock=clock)
        ciwatch.deliver(*session.split(":", 1), res)
        return res

    def told(self, agent, sid):
        # what reached the session: the CI events pending in its record, and the CI messages of its run
        texts = [e.get("text") or "" for e in ciwatch.take_events(agent, sid, mark=False)]
        return texts + [m["text"] for m in (pg.load_run(agent, sid) or {}).get("messages") or []
                        if m.get("from") == "ci"]

    def heard(self, agent, sid, words):
        return sum(words in t for t in self.told(agent, sid))


class JoinedSessionTests(SessionsCase):
    def test_the_watchers_result_reaches_each_session_joined_to_it_once(self):
        # s1 (Claude Code) started the watcher; x2 (Codex) and s3 (Claude Code, with a run) sent the same commit and
        # joined it; s4 joined it too but follows the commit with its own `dev ci watch --wait`, whose output tells it
        self.session("codex", "x2")
        self.session("claude", "s3", with_run=True)
        self.session("claude", "s4")
        self.assertTrue(self.start(self.up, "claude:s1"))
        for joined in ("codex:x2", "claude:s3", "claude:s4"):
            self.assertFalse(self.start(self.up, joined), f"{joined} started a second watcher")
        ciwatch.register({"top": str(self.up), "ref": "main", "sha": self.sha, "kind": "branch"}, "agent", "claude",
                         "s4", os.getpid(), 28)
        self.ends(self.up, "claude:s1")
        said = f"CI failed on main @ {self.sha}"
        for agent, sid, times in (("claude", "s1", 1), ("codex", "x2", 1), ("claude", "s3", 1), ("claude", "s4", 0)):
            with self.subTest(f"{agent}:{sid}"):
                self.assertEqual(self.heard(agent, sid, said), times, self.told(agent, sid))

    def test_a_joined_sessions_watch_closes_only_once_its_session_holds_the_result(self):
        # a wait that finds the session's watch closed (nothing more can arrive) must find the result already there
        self.session("codex", "x2")
        self.start(self.up, "claude:s1")
        self.start(self.up, "codex:x2")
        at_close, update = {}, state.update

        def spy(path, fn, **kw):
            before = state.read(path) if Path(path).parent == ciwatch.LIVE else None
            doc = update(path, fn, **kw)
            if before and before.get("state") in ("live", "expired") and (doc or {}).get("state") in ciwatch.FINAL:
                at_close[doc["session"]] = self.heard(doc["agent"], doc["session"], "CI failed on main")
            return doc
        with mock.patch.object(state, "update", spy):
            self.ends(self.up, "claude:s1")
        self.assertEqual(at_close, {"s1": 1, "x2": 1})

    def test_a_session_told_by_the_watch_that_ended_first_is_not_told_again(self):
        # s1's own `dev ci watch --wait` sees the run end before devkit's background watcher does
        self.session("codex", "x2")
        self.start(self.up, "claude:s1")
        self.start(self.up, "codex:x2")
        clock, out = Clock(), Flushed()
        a = argparse.Namespace(branch="main", sha=self.sha, tag=None, pr=None, timeout=28)
        code = ciwatch.cmd_wait(a, str(self.up), out=out, runs_fn=self.host(self.up), sleep=clock.sleep, clock=clock,
                                session=("claude", "s1"))
        self.assertEqual(code, 1, out.lines())
        self.ends(self.up, "claude:s1")
        with self.subTest("x2 hears it once"):
            self.assertEqual(self.heard("codex", "x2", "CI failed on main"), 1, self.told("codex", "x2"))
        with self.subTest("s1 hears it from its own wait only"):
            self.assertEqual(self.told("claude", "s1"), [])

    def test_a_watcher_that_stops_before_the_run_ends_tells_each_joined_session(self):
        self.session("codex", "x2")
        self.start(self.up, "claude:s1")
        self.start(self.up, "codex:x2")
        res = self.ends(self.up, "claude:s1", status="running", conclusion=None, timeout_min=10)
        self.assertEqual(res["state"], "timeout")
        for agent, sid in (("claude", "s1"), ("codex", "x2")):
            with self.subTest(f"{agent}:{sid}"):
                self.assertEqual(self.heard(agent, sid, "still running after the watch limit"), 1,
                                 self.told(agent, sid))


class RepositoryTests(SessionsCase):
    # acme/api and its fork fork/api: checkouts in folders named api, both at the same commit on main
    def setUp(self):
        super().setUp()
        self.fork = repository(self.root, "fork")
        self.assertEqual(subprocess.run(["git", "-C", str(self.fork), "rev-parse", "--short=7", "HEAD"], env=GIT_ENV,
                                        capture_output=True, text=True).stdout.strip(), self.sha)
        self.session("claude", "s2")

    def watched(self, top, by, sid):
        # a watch record of `top`'s main at the commit for session `sid`, whose process (this one) runs
        ciwatch.register({"top": str(top), "ref": "main", "sha": self.sha, "kind": "branch"}, by, "claude", sid,
                         os.getpid(), 90)

    def test_a_fork_and_its_upstream_each_get_their_own_background_watcher(self):
        self.assertTrue(self.start(self.up, "claude:s1"))
        self.assertTrue(self.start(self.fork, "claude:s2"), "the fork's push joined the upstream's watcher")
        self.assertEqual(self.spawned, [str(self.up), str(self.fork)])

    def test_a_result_in_the_upstream_leaves_the_forks_watch_running_and_its_session_untold(self):
        self.watched(self.up, "devkit", "s1")
        self.watched(self.fork, "devkit", "s2")
        self.ends(self.up, "claude:s1")
        with self.subTest("the fork's watch still runs"):
            self.assertEqual([(w["session"], w["status"], w["top"]) for w in ciwatch.watches()],
                             [("s2", "live", str(self.fork))])
        with self.subTest("the fork's session hears nothing"):
            self.assertEqual(self.told("claude", "s2"), [])

    def test_a_session_waiting_on_the_upstream_itself_still_hears_the_forks_result(self):
        # s1 sent the commit to both; it follows the upstream with its own `dev ci watch --wait`, the fork through
        # devkit's background watcher
        self.watched(self.up, "agent", "s1")
        self.watched(self.fork, "devkit", "s1")
        self.ends(self.fork, "claude:s1")
        self.assertEqual(self.heard("claude", "s1", f"CI failed on main @ {self.sha}"), 1, self.told("claude", "s1"))

    def test_the_upstreams_result_does_not_excuse_the_forks_unfinished_run_at_the_end_of_a_turn(self):
        # s2 pushed the fork's main this turn and arms no wait; the upstream's run of the same commit has ended
        subprocess.run(["git", "-C", str(self.fork), "update-ref", "refs/remotes/origin/main", "HEAD"], env=GIT_ENV,
                       check=True)
        self.ends(self.up, "claude:s1")
        at = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 60))
        records = [
            {"type": "user", "timestamp": at, "message": {"role": "user", "content": "ship the fork"}},
            {"type": "assistant", "timestamp": at, "cwd": str(self.fork), "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "git push origin main"}}]}},
            {"type": "user", "timestamp": at, "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}}]
        transcript = self.root / "transcript.jsonl"
        transcript.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        turn = {"transcript_path": str(transcript), "cwd": str(self.fork), "session_id": "s2"}
        why = ciwatch.stop_gate("claude", turn, runs_fn=self.host(self.fork, status="running", conclusion=None))
        self.assertIn(f"dev ci watch --wait --branch main --sha {self.sha}", why or "")


class CodexForegroundTests(WatchSandbox):
    # Real processes: a Codex session joined the watcher a Claude Code session started, and waits in the foreground
    # with `dev watch --wait`; the watcher's end must wake it.
    def test_a_codex_session_joined_to_the_watcher_wakes_from_its_foreground_wait(self):
        top, sha = self.repos["api"], self.head("api")
        self.session("claude", "s1")
        self.session("codex", "x2")
        watcher = self.sleeper()
        for session in ("claude:s1", "codex:x2"):  # the hook after each session's push; the watcher runs already
            self.py(textwrap.dedent(f'''
                from devkit import ciwatch
                class NoSpawn:  # never start a real background watch from a test
                    DEVNULL = -3
                    def Popen(*a, **k):
                        raise AssertionError("a background watch was started")
                ciwatch.subprocess = NoSpawn
                ciwatch.WATCH.mkdir(parents=True, exist_ok=True)
                lock = ciwatch.WATCH / (ciwatch._key("api", "main") + "-{sha}.lock")
                lock.write_text("{watcher} " + str(int(time.time())))
                ciwatch.start_background({str(top)!r}, session={session!r}, branch="main", sha="{sha}")
            '''), cwd=top, DEVKIT_NO_CI_WATCH="")
        wait = self.spawn(["watch", "--wait", "--timeout", "10"], session="codex:x2")
        self.armed_wait(wait, "x2")
        self.watcher_ends("claude:s1")
        rc, out = self.finish(wait)
        self.assertEqual(rc, 0, out)
        self.assertIn(f"CI failed on main @ {sha}", out)


if __name__ == "__main__":
    unittest.main()
