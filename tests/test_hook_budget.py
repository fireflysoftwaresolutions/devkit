# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The guard hook judges one call within one time budget. An agent that stops the hook at its timeout runs the call
# unjudged, so every judge of a PreToolUse call shares the guard's time: check, the hook's own judges after it and a
# crew lane's edit-scope check, whatever the number of files a patch names; and a lock wait inside a judgment keeps
# the judgment's deadline.
# Run: python3 -m unittest tests.test_hook_budget -v
import contextlib
import io
import json
import os
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

from devkit import audit, core, guard, hooks  # noqa: E402
from test_codex_launch import TANK  # noqa: E402
import test_guard_launcher_depth as launchers  # noqa: E402  (by module: its tests are not collected here again)
from test_readonly_attribution import CodexOverlap  # noqa: E402


def patch_of(files) -> str:
    return "*** Begin Patch\n" + "".join(f"*** Add File: {f}\n+x\n" for f in files) + "*** End Patch\n"


class _GitCounted:
    """core.gitout, counting the git processes it starts."""

    def __init__(self):
        self.real, self.calls = core.gitout, []

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        return self.real(*args, **kwargs)


class LaneScopeIsJudgedInTheGuardsTime(CodexOverlap, unittest.TestCase):
    # The Backend engineer (Tank) works in a Codex lane whose step scope is api/**, in a git repository of its own.
    # The edit-scope check ran after check and outside the guard's time, and asked git for the project once per file
    # the patch names (6.8 ms each here, three git processes in a real checkout): a patch of a few thousand files
    # in the scope ran past the hook's timeout before the scope check reached one outside it.

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = os.path.realpath(tmp.name)
        subprocess.run(["git", "init", "-q", "-b", "feat/x", self.repo], check=True)
        p = mock.patch.object(hooks, "STATE", Path(self.repo) / ".state")
        p.start()
        self.addCleanup(p.stop)

    def payload(self, files):
        self.calls = getattr(self, "calls", 0) + 1  # each call its own tool_use_id, as the agent gives it
        return {"session_id": self.sid, "agent_id": TANK, "tool_name": "apply_patch",
                "tool_use_id": f"patch-{self.calls}", "hook_event_name": "PreToolUse", "cwd": self.repo,
                "tool_input": {"input": patch_of(files)}}

    def scope(self, files):
        return hooks._scope_violation("codex", self.payload(files))

    def hook(self, files):
        """The guard hook's (decision, reason) for Tank's patch of `files`: (None, "") when it lets the call run."""
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(self.payload(files)))), contextlib.redirect_stdout(out):
            hooks.guard("codex")
        lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
        told = json.loads(lines[-1])["hookSpecificOutput"] if lines else {}
        return told.get("permissionDecision"), told.get("permissionDecisionReason") or ""

    def test_a_patch_of_hundreds_of_files_in_scope_and_one_outside_is_refused_within_the_budget(self):
        self.assertEqual(self.hook(["api/x.py"]), (None, ""))  # one file in its scope: the call runs
        decision, why = self.hook(["outside.py"])
        self.assertEqual(decision, "deny")
        self.assertIn("outside.py is outside", why)
        # the same file after 900 in the scope, with the guard's time running from the hook's start as it does in
        # the hook's own process: refused, and before the time is up
        files = [f"api/f{i}.py" for i in range(900)] + ["outside.py"]
        with mock.patch.object(guard, "judge_budget", return_value=2.0), \
                mock.patch.object(guard, "_IN_GUARD_HOOK", True), mock.patch.object(guard, "_LOADED", time.monotonic()):
            decision, why = self.hook(files)
            elapsed = time.monotonic() - guard._LOADED
        self.assertEqual(decision, "deny")
        self.assertLess(elapsed, 2.5)
        self.assertTrue("outside.py is outside" in why or guard.judge_time_reason(2.0) in why, why[:200])

    def test_a_scope_check_past_the_guards_time_is_refused_with_the_time_reason(self):
        files = [f"api/f{i}.py" for i in range(40)] + ["outside.py"]

        def slow(*_a, **_k):  # 50 ms for each file: the check would end after 2 s
            time.sleep(0.05)
            return False
        with mock.patch.object(guard, "judge_budget", return_value=0.3), \
                mock.patch.object(guard, "member_write_exempt", side_effect=slow):
            started = time.monotonic()
            self.assertEqual(self.scope(files), guard.judge_time_reason(0.3))
            self.assertLess(time.monotonic() - started, 0.8)
            decision, why = self.hook(files)
        self.assertEqual(decision, "deny")
        self.assertIn(guard.judge_time_reason(0.3), why)
        # recorded as the guard's time refusal, which has its own bug title, not as a scope refusal
        last = audit.query(action="guard.deny")[-1]
        self.assertEqual(last["ref"].get("refusal_id"), "_judged_in_time:judge-time")

    def test_in_the_guard_hook_the_judges_after_check_share_its_time(self):
        # in the hook's own process the time runs from the guard's start: once it is spent, every judge refuses
        shell = {"session_id": self.sid, "agent_id": TANK, "tool_name": "exec_command", "cwd": self.repo,
                 "tool_input": {"cmd": "git commit -m 'feat: x'"}}
        spent = guard.judge_time_reason(guard.judge_budget())
        with mock.patch.object(guard, "_LOADED", time.monotonic() - guard.judge_budget() - 1), \
                mock.patch.object(guard, "_IN_GUARD_HOOK", True):
            for name, judge in (("identity", lambda: hooks._identity_verdict(shell)),
                                ("crew-limits", lambda: hooks._crew_limits("codex", shell)),
                                ("stub-commit", lambda: hooks._stub_commit_gate(shell))):
                with self.subTest(judge=name):
                    self.assertEqual(judge(), ("deny", spent))
            self.assertEqual(self.scope(["api/x.py"]), spent)
        self.assertIsNone(self.scope(["api/x.py"]))  # with time left, the same edit is in its scope

    def test_the_scope_check_asks_git_once_per_call_not_once_per_file(self):
        scratch = tempfile.TemporaryDirectory()  # a temp folder outside the repository: the member's scratch files
        self.addCleanup(scratch.cleanup)
        for where, name in (("in its scope", lambda i: f"api/f{i}.py"),
                            ("scratch files", lambda i: os.path.join(os.path.realpath(scratch.name), f"f{i}.txt"))):
            counted, starts = _GitCounted(), []
            with mock.patch.object(core, "gitout", counted):
                for n in (1, 200):
                    counted.calls.clear()
                    self.assertIsNone(self.scope([name(i) for i in range(n)]))
                    starts.append(len(counted.calls))
            with self.subTest(files=where):
                self.assertEqual(starts[0], starts[1], "git processes for a patch of 1 file and of 200")

    def test_a_patch_over_the_path_bound_is_refused_by_the_scope_check_too(self):
        with mock.patch.object(guard, "_PATCH_PATHS", 3, create=True):
            self.assertIsNone(self.scope(["api/a.py", "api/b.py", "api/c.py"]))
            why = self.scope(["api/a.py", "api/b.py", "api/c.py", "api/d.py"])
        self.assertIsNotNone(why)
        self.assertEqual(why, guard.PATCH_PATHS_REASON)


class AJudgmentAsksGitOncePerFolder(unittest.TestCase):
    # what a judgment asks git about a folder (its project) it asks once, and the next judgment asks again: a call
    # may run after a checkout changed, so nothing is kept from one call to the next

    setUp = launchers.LaunchersAreReadOnceInTime.setUp

    def test_a_folders_project_is_looked_up_once_per_judgment(self):
        counted = _GitCounted()

        def judge():
            return [guard.current_project(self.repo) for _ in range(5)]
        with mock.patch.object(core, "gitout", counted):
            guard._in_time()(judge)()
            once = len(counted.calls)
            guard._in_time()(judge)()
            twice = len(counted.calls)
            guard.current_project(self.repo)  # outside any judgment: asked each time
            alone = len(counted.calls) - twice
        self.assertGreater(once, 0)
        self.assertEqual((twice, alone), (2 * once, once))


class PatchesNameABoundedNumberOfFiles(unittest.TestCase):
    # check judges each file a patch names: a patch of thousands takes seconds, and the scope check after it more,
    # so a patch that names more files than the bound is refused before any of them is judged

    setUp = launchers.LaunchersAreReadOnceInTime.setUp

    def check(self, files):
        return guard.check("apply_patch", {"input": patch_of(files)}, self.repo, "codex")

    def test_a_patch_over_the_path_bound_is_refused_at_once_and_a_large_real_one_is_not(self):
        started = time.monotonic()
        v = self.check([f"docs/f{i}.md" for i in range(2_300)])  # the patch that outran the Codex hook's 15 s
        self.assertEqual(v and v[0], "deny")
        self.assertEqual(v[1], guard.PATCH_PATHS_REASON)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIsNone(self.check([f"docs/f{i}.md" for i in range(200)]))  # a large real refactor: far below it
        with mock.patch.object(guard, "_PATCH_PATHS", 3):
            self.assertIsNone(self.check(["a.md", "b.md", "c.md"]))  # at the bound: judged as before
            self.assertEqual(self.check(["a.md", "b.md", "c.md", "d.md"]), ("deny", guard.PATCH_PATHS_REASON))


# a child process that holds an exclusive lock on a file until it is stopped
_HOLD = ("import fcntl, sys, time\nf = open(sys.argv[1], 'w')\nfcntl.flock(f, fcntl.LOCK_EX)\n"
         "print('held', flush=True)\ntime.sleep(30)\n")


class ALockWaitKeepsTheJudgmentsDeadline(unittest.TestCase):
    # core._flock_within waits for a lock under a timer of its own. It set that timer in place of the guard's and
    # restored the guard's without its 50 ms repeat, so a lock wait inside a judgment suspended the guard's time

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        lock = os.path.join(tmp.name, "held.lock")
        holder = subprocess.Popen([sys.executable, "-c", _HOLD, lock], stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        holder.stdout.close()
        self.fd = os.open(lock, os.O_RDWR)
        self.addCleanup(os.close, self.fd)

    def test_a_lock_wait_longer_than_the_time_left_ends_at_the_deadline(self):
        started = time.monotonic()
        with self.assertRaises(guard._OutOfTime), guard._time_limit(0.3):
            core._flock_within(self.fd, 3.0)
        self.assertLess(time.monotonic() - started, 0.8)

    def test_after_a_lock_wait_the_guards_timer_still_repeats(self):
        started = time.monotonic()
        with self.assertRaises(guard._OutOfTime), guard._time_limit(0.4):
            self.assertFalse(core._flock_within(self.fd, 0.1))  # its own wait ends first: the lock is not taken
            try:
                time.sleep(2)  # a rule that swallows the guard's first tick
            except guard._OutOfTime:
                pass
            time.sleep(2)  # the next tick still ends the judgment
        self.assertLess(time.monotonic() - started, 1.2)

    def test_an_earlier_outer_timer_reaches_its_handler_during_the_wait_and_the_wait_goes_on(self):
        fired = []
        before = signal.signal(signal.SIGALRM, lambda *_: fired.append(time.monotonic()))
        self.addCleanup(signal.signal, signal.SIGALRM, before)
        signal.setitimer(signal.ITIMER_REAL, 0.1)
        self.addCleanup(signal.setitimer, signal.ITIMER_REAL, 0)
        started = time.monotonic()
        self.assertFalse(core._flock_within(self.fd, 0.4))
        waited = time.monotonic() - started
        self.assertEqual(len(fired), 1)
        self.assertLess(fired[0] - started, 0.3)  # at its own time, not after the wait
        self.assertGreater(waited, 0.35)  # a handler that does not raise leaves the wait to its own time


if __name__ == "__main__":
    unittest.main()
