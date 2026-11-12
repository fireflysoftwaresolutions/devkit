# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The guard judges a command in time: a hook past its 15 s timeout lets the command run unjudged, so a slow reading is
# a way around the guard. It reads what a launcher (xcrun, op run, nix run, mise exec …) may start once per command,
# within a bound, and its pattern scans are linear in the length of a word.
# Run: python3 -m unittest tests.test_guard_launcher_depth -v
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()  # run alone, this module gets the suite's temporary home too (tests/_hermetic.py)
sys.path.insert(0, str(ROOT / "lib"))

from devkit import audit, core, guard  # noqa: E402
import test_guard_old_copies as copies  # noqa: E402  (its releases folder: an older copy and the pinned one)

# a long message, as `dev crew ask` and `dev commit` take: 4000 characters of plain sentences
TEXT = ("The guard read each command a launcher may start in full, so the hook ran past its timeout. " * 45)[:4000]


class _Late(BaseException):
    # a BaseException, so no `except Exception` in the guard swallows it and the reading goes on
    pass


class LaunchersAreReadOnceInTime(unittest.TestCase):
    # each word of a launcher may name the program it starts, so the guard reads the command from each of them. A
    # launcher among those words read them all again (twice the time with each launcher in a chain), and a launcher
    # with thousands of words read thousands of commands in full: both ran past the hook's timeout

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "handoff" / "projects").mkdir(parents=True)
        self.repo = str(root / "repo")
        subprocess.run(["git", "init", "-q", "-b", "feat/x", self.repo], check=True)
        for p in (mock.patch.object(audit, "AUDIT", root / "audit"),
                  mock.patch.object(core, "HANDOFF", root / "handoff"),
                  mock.patch.object(core, "NOTES", root / "handoff" / "projects"),
                  mock.patch.object(audit, "policy_file", lambda: root / "no-policy.json"),
                  mock.patch.dict(os.environ)):
            p.start()
            self.addCleanup(p.stop)
        for k in ("DEVKIT_FROM_AGENT", "DEVKIT_SESSION", "DEVKIT_AGENT"):
            os.environ.pop(k, None)

    def within(self, fn, seconds=1.0):
        """fn's result, or a failure once it has run for `seconds`: far below the hook's 15 s timeout."""
        def late(*_):
            raise _Late()
        before = signal.signal(signal.SIGALRM, late)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            return fn()
        except _Late:
            self.fail(f"not judged within {seconds} s")
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, before)

    def check(self, cmd):
        v = self.within(lambda: guard.check("Bash", {"command": cmd}, self.repo, "claude"))
        return v[1] if v else None

    def member(self, cmd):
        v = self.within(lambda: guard.crew_shell_verdict(cmd, self.repo, None, "tank", lane="a1", session="s1",
                                                         grant=lambda *a, **k: None))
        return v[1] if v else None

    def gate(self, cmd):
        found = self.within(lambda: guard.irreversible_actions(cmd, self.repo))
        return None if found is None else [(a["action"], a["tier"], a["target"]) for a in found]

    def test_a_push_behind_chained_launchers_is_judged_in_time(self):
        for n in (8, 20):
            chain = " ".join(["xcrun"] * n)
            with self.subTest(launchers=n):
                self.assertIn(("push-destructive", 2, "main"), self.gate(f"{chain} git push origin main") or [])
                self.assertIn("Crew members never run `git push origin main`",
                              self.member(f"{chain} git push origin main") or "")
                self.assertIn("force-push", self.check(f"{chain} git push --force origin main") or "")

    def test_the_word_bound_lies_between_real_commands_and_a_runaway_launcher(self):
        # below it, a long message after a launcher (or a prefix such as nice) is read as before; above it, a
        # launcher with thousands of words is refused at once, where reading each of its commands took 22 s
        for cmd in (f'nice dev crew ask "{TEXT}"', f'op run -- dev crew ask "{TEXT}"',
                    f'op run -- dev commit -m "fix(guard): x" --why "{TEXT}" --cause "{TEXT}" --verified "{TEXT}"'):
            with self.subTest(cmd=cmd[:24]):
                self.assertIsNone(self.check(cmd))
                self.assertEqual(self.gate(cmd), [])
        cmd = "xcrun " + "w " * 3000 + "git push origin main"
        self.assertEqual(self.check(cmd), guard.LAUNCH_REASON)
        self.assertIn("more words than the guard reads", self.member(cmd) or "")
        found = self.within(lambda: guard.irreversible_actions(cmd, self.repo)) or []
        self.assertEqual([(a["action"], a["tier"], a.get("why", ("",))[0]) for a in found],
                         [("unknown", 2, "unsure")])  # it may start anything: the gate's strictest tier

    def test_a_launcher_deeper_than_the_guard_reads_is_refused(self):
        with mock.patch.object(guard, "_LAUNCH_DEPTH", 1):
            self.assertIsNone(self.check("xcrun git status"))  # one launcher deep: read
            self.assertEqual(self.check("xcrun xcrun git status"), guard.LAUNCH_REASON)
            self.assertEqual([(a, t) for a, t, _ in self.gate("xcrun xcrun git status")], [("unknown", 2)])


class LongWordsAreJudgedInTime(unittest.TestCase):
    # the check for other devkit copies tried every start of a word without separators (a pattern that opened with a
    # lazy path) and scanned back to the word's start at each copy name in it: 18.7 s for one word of 40,000 characters

    setUp = copies.OtherReleaseTests.setUp
    within = LaunchersAreReadOnceInTime.within

    def check(self, cmd):
        v = self.within(lambda: guard.check("Bash", {"command": cmd}, str(self.cwd), "claude"))
        return v[1] if v else None

    def copy_named(self, cmd):
        v = self.within(lambda: guard.other_release(cmd, str(self.cwd)))
        return v[1] if v else None

    def test_a_word_without_separators_is_judged_in_time(self):
        self.assertIsNone(self.check("echo '" + "x" * 40_000 + "'"))  # the whole check
        for size in (40_000, 200_000):  # the check for other copies, which a longer word reaches first
            word = "x" * size
            with self.subTest(size=size):
                self.assertIsNone(self.copy_named(f"echo '{word}'"))
                # another copy named after it is still refused, and so is one written after a long path
                self.assertIn(copies.OLD, self.copy_named(f"echo {word} ~/.local/share/devkit/releases/{copies.OLD}/"
                                                          f"bin/dev") or "")
                self.assertIn(copies.OLD, self.copy_named(f"~/{word}/../.local/share/devkit/releases/{copies.OLD}/"
                                                          f"bin/dev") or "")

    def test_a_word_that_names_a_copy_many_times_is_judged_in_time(self):
        # after the pinned copy's folder every name of another copy was judged with it, each from the word's start
        pinned = f"~/.local/share/devkit/releases/{copies.NEW}/"
        self.assertIsNone(self.copy_named(f"echo {pinned}{copies.OLD * 5000}"))
        self.assertIn(copies.OLD, self.check(f"R=~/.local/share/devkit; $R/releases/{copies.OLD}/bin/dev status") or "")


class _Outer(BaseException):
    pass


class JudgingStopsAtTheGuardsTime(unittest.TestCase):
    # whatever is slow in judging a call (a rule, a pattern the regular-expression engine runs in C, a long token),
    # the guard stops at its time and refuses the call, before the hook's timeout would let it run unjudged

    setUp = LaunchersAreReadOnceInTime.setUp
    within = LaunchersAreReadOnceInTime.within
    check = LaunchersAreReadOnceInTime.check

    def refused_in_time(self, slow, cmd="ls", budget=0.3):
        """The verdict of `cmd` while the releases rule runs `slow` and the guard has `budget` seconds."""
        started = time.monotonic()
        with mock.patch.object(guard, "judge_budget", return_value=budget), \
                mock.patch.object(guard, "other_release", side_effect=lambda *a, **k: slow()):
            why = self.check(cmd)
        self.assertLess(time.monotonic() - started, budget + 0.5)
        return why

    def test_a_judgment_past_its_time_is_refused_and_an_earlier_outer_timer_still_fires(self):
        self.assertEqual(self.refused_in_time(lambda: time.sleep(5)), guard.judge_time_reason(0.3))
        # a pattern the engine runs in C, with no Python between its steps: (x+x+)+y backtracks for minutes
        self.assertEqual(self.refused_in_time(lambda: re.match(r"(x+x+)+y", "x" * 40)), guard.judge_time_reason(0.3))
        # a timer around the judgment (a lock wait's, a test's) that is due first keeps its time and its handler

        def outer(*_):
            raise _Outer()
        before = signal.signal(signal.SIGALRM, outer)
        signal.setitimer(signal.ITIMER_REAL, 0.2)
        try:
            with mock.patch.object(guard, "judge_budget", return_value=2.0), \
                    mock.patch.object(guard, "other_release", side_effect=lambda *a, **k: time.sleep(3)), \
                    self.assertRaises(_Outer):
                guard.check("Bash", {"command": "ls"}, self.repo, "claude")
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, before)

    def test_a_long_token_past_its_time_is_refused(self):
        # the shell tokenizer copies a token at each of its characters: one word of 200,000 takes seconds
        with mock.patch.object(guard, "judge_budget", return_value=0.5):
            started = time.monotonic()
            self.assertEqual(self.check("echo '" + "x" * 200_000 + "'"), guard.judge_time_reason(0.5))
            self.assertLess(time.monotonic() - started, 1.0)

    def test_the_gates_after_check_share_the_hooks_time(self):
        # in the guard hook the time runs from the guard's start for all its judges of the call; elsewhere per call
        long_ago = time.monotonic() - guard.judge_budget() - 1
        with mock.patch.object(guard, "_LOADED", long_ago):
            self.assertIsNone(self.check("ls"))
            with mock.patch.object(guard, "_IN_GUARD_HOOK", True):
                self.assertEqual(self.check("ls"), guard.judge_time_reason(guard.judge_budget()))
                v = guard.irreversible_verdict("ls", self.repo, session="s1", grant=lambda *a, **k: None)
                self.assertEqual(v, ("deny", guard.judge_time_reason(guard.judge_budget())))
                v = guard.crew_shell_verdict("ls", self.repo, None, "tank", lane="a1", session="s1",
                                             grant=lambda *a, **k: None)
                self.assertEqual(v, ("deny", guard.judge_time_reason(guard.judge_budget())))

    def test_the_budget_is_the_shortest_guard_hook_timeout_devkit_installs_less_its_margin(self):
        self.assertEqual(guard.judge_budget(), 15 - guard.JUDGE_MARGIN)  # both agents' configs give the guard 15 s
        home = Path(tempfile.mkdtemp(dir=self.repo))
        for rel, limit in (("config/claude/settings.json", 40), ("config/codex/hooks.json", 30)):
            (home / rel).parent.mkdir(parents=True)
            hooks = [{"type": "command", "command": "'{{DEVKIT}}/bin/dev' hook prompt", "timeout": 5},
                     {"type": "command", "command": "'{{DEVKIT}}/bin/dev' hook guard --agent x", "timeout": limit}]
            (home / rel).write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": hooks}]}}))
        with mock.patch.object(core, "DEVKIT_HOME", home):
            self.assertEqual(guard.judge_budget(), 30 - guard.JUDGE_MARGIN)
