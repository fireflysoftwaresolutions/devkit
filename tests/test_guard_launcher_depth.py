# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The guard reads what a launcher (xcrun, op run, nix run, mise exec …) may start once per command, within a bound:
# a hook past its 15 s timeout lets the command run unjudged, so a slow reading is a way around the guard.
# Run: python3 -m unittest tests.test_guard_launcher_depth -v
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()  # run alone, this module gets the suite's temporary home too (tests/_hermetic.py)
sys.path.insert(0, str(ROOT / "lib"))

from devkit import audit, core, guard  # noqa: E402

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
