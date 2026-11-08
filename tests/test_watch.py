# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# One wait for every devkit event (`dev watch --wait`): an idle session hears a crew question, a decision, a ready
# step, a member that stopped or was lost, and a CI result, each once, whichever of a hook and the wait takes it
# first. The wait runs through devkit's own `dev` parser, with the world it waits on scripted: each time the wait
# blocks for a file change, the script lands the next event (as a member, the tech lead or devkit would write it)
# and says a file changed; with nothing left, the clock passes the wait's timeout.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_watch
import contextlib
import io
import json
import os
import select
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import hooks, progress as pg, state  # noqa: E402
from test_progress import TempState, launch  # noqa: E402
from test_state_e2e import Clock, dev, patch_if  # noqa: E402

try:
    from devkit import watch
except ImportError:  # a release before the watch manager: each test fails on the behavior it checks
    watch = None


class Script:
    # The wait's file-change function. Each call runs the next action of the world and says a file changed; with
    # none left, the clock moves past the time the wait asked to block, and nothing changed.
    def __init__(self, clock, *actions):
        self.clock, self.actions, self.calls = clock, list(actions), 0

    def __call__(self, seconds):
        self.calls += 1
        if self.actions:
            self.actions.pop(0)()
            return True
        self.clock.t += max(float(seconds), 1.0)
        return False


class WatchCase(unittest.TestCase):
    # A session with a run whose Backend engineer works on the API; devkit's state in the test's folder.
    def setUp(self):
        self.ts = TempState().__enter__()
        self.addCleanup(self.ts.__exit__, None, None, None)
        self.root = Path(self.ts.tmp.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(hooks, "STATE", self.root))
        self.stack.enter_context(mock.patch.object(pg, "_publish", lambda: None))
        self.t0 = time.time()
        self.clock = Clock(self.t0)
        patch_if(self.stack, watch, "LIVE", self.root / "watch" / "live")
        patch_if(self.stack, watch, "time", self.clock)
        for name in ("DEVKIT_FROM_AGENT",):
            self.stack.enter_context(mock.patch.dict(os.environ, {name: ""}))

    def run_with_member(self):
        pg.start("Feature", plan=["api", "tests"], now=self.t0)
        pg.assign("api", "Tank", now=self.t0)
        launch("a1", "devkit:tank", "Tank: api", self.t0)

    def world(self, *actions):
        # what lands while the wait blocks; the wait gets this script as its file-change function
        script = Script(self.clock, *actions)
        patch_if(self.stack, watch, "Changes", lambda *a, **k: script)
        return script

    def post_tool_main(self, tid="toolu_x"):
        return pg.on_tool_done("claude", {"session_id": "s1", "tool_name": "Bash", "tool_use_id": tid,
                                          "tool_input": {"command": "git status"}}) or ""


class WaitWakesTests(WatchCase):
    def test_a_member_question_wakes_the_wait_once_and_the_next_post_tool_use_repeats_nothing(self):
        # 26.11.6 had no wait: the question reached the tech lead only at its next hook, which an idle session never
        # runs, so the member stayed blocked until the developer wrote
        self.run_with_member()
        self.world(lambda: pg.crew_ask("May I rename the column?", from_agent="a1", now=self.clock.t))
        rc, out, err = dev("watch", "--wait", "--timeout", "5")
        self.assertEqual(rc, 0, out + err)
        lines = [ln for ln in out.splitlines() if "M1" in ln]
        self.assertEqual(len(lines), 1, out)
        self.assertTrue(lines[0].startswith("devkit · question M1 from Backend engineer (Tank)"), lines[0])
        self.assertIn("May I rename the column?", lines[0])
        self.assertIn('dev crew answer M1 "…"', lines[0])
        self.assertIn("arm again: dev watch --wait", out.splitlines()[-1])
        self.assertNotIn("M1", self.post_tool_main())

    def test_a_wait_and_a_hook_racing_on_one_message_report_it_once(self):
        # the hook and the wait take through the same locked "delivered" flag: one of them reports the question,
        # never both, and a wait that lost the race keeps waiting to its timeout
        self.run_with_member()
        pg.crew_ask("Which schema version?", from_agent="a1", now=self.t0)
        start = threading.Barrier(2)
        got = {}

        def hook():
            start.wait(5)
            got["hook"] = self.post_tool_main()
        t = threading.Thread(target=hook)
        t.start()
        self.world()
        start.wait(5)
        rc, out, err = dev("watch", "--wait", "--timeout", "1")
        t.join(10)
        reports = got["hook"].count("Which schema version?") + out.count("Which schema version?")
        self.assertEqual(reports, 1, (got["hook"], out))
        self.assertEqual(rc, 0 if "Which schema version?" in out else 3, out + err)

    def test_the_timeout_exits_3_with_the_command_that_arms_it_again(self):
        self.run_with_member()
        self.world()
        rc, out, err = dev("watch", "--wait", "--timeout", "2")
        self.assertEqual(rc, 3, out + err)
        self.assertIn("arm again: dev watch --wait", out.splitlines()[-1])
        rc, listed, _ = dev("watches")
        self.assertEqual(rc, 0)
        rows = [ln.split()[0] for ln in listed.splitlines()[1:] if ln.startswith("  ") and not ln.startswith("   ")]
        self.assertEqual(rows[:1], ["expired"], listed)
        self.assertNotIn("live", rows, listed)

    def test_a_newer_wait_supersedes_the_older_which_prints_nothing_it_did_not_take(self):
        # two waits armed in one turn: the newer one takes the question; the older one ends with one line (exit 5)
        self.run_with_member()
        newer = {}

        def arm_the_newer():
            inner = Script(self.clock, lambda: pg.crew_ask("Ship it?", from_agent="a1", now=self.clock.t))
            with contextlib.ExitStack() as stack:
                patch_if(stack, watch, "Changes", lambda *a, **k: inner)
                newer["rc"], newer["out"], _ = dev("watch", "--wait", "--timeout", "5")
        self.world(arm_the_newer)
        rc, out, err = dev("watch", "--wait", "--timeout", "5")
        self.assertEqual(rc, 5, out + err)
        self.assertEqual(newer.get("rc"), 0, newer.get("out"))
        self.assertIn("Ship it?", newer["out"])
        self.assertEqual(len(out.strip().splitlines()), 1, out)
        self.assertNotIn("Ship it?", out)

    def test_for_me_never_returns_another_lanes_message(self):
        # a member waits for the answer to its own question; the tech lead's message to another member stays that
        # member's, undelivered
        self.run_with_member()
        launch("a2", "devkit:mouse", "Mouse: tests", self.t0)
        pg.crew_ask("Do the columns stay nullable?", from_agent="a1", now=self.t0)
        pg.on_tool_done("claude", {"session_id": "s1", "tool_name": "Bash", "tool_use_id": "t0"})  # main reads it
        self.world(lambda: pg.crew_ask("Use the fixtures in tests/data", to="a2", now=self.clock.t),
                   lambda: pg.crew_answer("M1", "Yes, nullable", now=self.clock.t))
        with mock.patch.dict(os.environ, {"DEVKIT_FROM_AGENT": "a1"}):
            rc, out, err = dev("watch", "--wait", "--for", "me", "--timeout", "9")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("Yes, nullable", out)
        self.assertNotIn("fixtures", out)
        to_a2 = [m for m in pg.load_run("claude", "s1")["messages"] if m["to"] == "a2"]
        self.assertEqual([m["delivered"] for m in to_a2], [False])

    def test_a_wait_with_nothing_that_can_arrive_exits_2_at_once(self):
        # no run in this session: no member can ask, nothing is launched, no CI runs; the wait says so at once
        # instead of blocking until its timeout
        script = self.world()
        rc, out, err = dev("watch", "--wait", "--timeout", "240")
        self.assertEqual(rc, 2, out + err)
        self.assertIn("nothing can arrive", out)
        self.assertEqual(script.calls, 0)


class ChangesTests(unittest.TestCase):
    # The wait's default file-change function sees a state file replaced by an atomic rename, and the next one after
    # it, with kqueue and with the stat fallback alike.
    def check(self, use_kqueue):
        with TempState() as ts:
            root = Path(ts.tmp.name)
            run = root / "runs" / "claude-s1.json"
            state.update(run, lambda d: d.update(n=1))
            other = root / "runs" / "claude-s2.json"
            changes = watch.Changes([run, root / "sessions" / "claude-s1.json"], use_kqueue=use_kqueue, batch=0,
                                    poll=0.05)
            try:
                self.assertFalse(changes(0.2))
                state.update(other, lambda d: d.update(n=1))  # another session's file in the same folder
                self.assertFalse(changes(0.2))
                for n in (2, 3):
                    t = threading.Timer(0.05, lambda n=n: state.update(run, lambda d: d.update(n=n)))
                    t.start()
                    self.assertTrue(changes(5))
                    t.join()
            finally:
                changes.close()

    def test_a_replaced_state_file_is_seen_with_kqueue(self):
        if watch is None:
            self.fail("no watch manager in this release")
        if not hasattr(select, "kqueue"):
            self.skipTest("kqueue exists on macOS and BSD only; the stat fallback test covers Linux")
        self.check(True)

    def test_a_replaced_state_file_is_seen_by_the_stat_fallback(self):
        if watch is None:
            self.fail("no watch manager in this release")
        self.check(False)


if __name__ == "__main__":
    unittest.main()
