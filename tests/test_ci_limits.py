# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev ci --run` gives each local step the time its project allows: `--step-timeout` when given; else the step's own
`timeout-minutes`, then its job's; else 1.5 times the longest pass recorded for the step, rounded up to a minute and
never below the floor; else the floor of 30 minutes. A step that runs past its limit ends with exit 124 and says the
limit and where it came from. devkit's own unit-test step takes about 36 minutes and used to end with 124 at 30.

Run: python3 -m unittest tests.test_ci_limits -v"""
import contextlib
import io
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import gitx  # noqa: E402
from test_gitx import Sandbox, dev  # noqa: E402


BOUND = 20  # seconds: a stopped step returns well within this, while its child would sleep for ten minutes


def alive(pid: int) -> bool:
    return subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0


def workflow(job_timeout=None, step_timeout=None, steps=(("Unit tests", "test -f README.md"),)) -> str:
    """A pull-request workflow with one job `test` and its run steps, with the timeouts given."""
    text = "name: CI\non: [pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
    if job_timeout is not None:
        text += f"    timeout-minutes: {job_timeout}\n"
    text += "    steps:\n"
    for i, (name, run) in enumerate(steps):
        text += f"      - name: {name}\n        run: {run}\n"
        if step_timeout is not None and i == 0:
            text += f"        timeout-minutes: {step_timeout}\n"
    return text


class StepLimitCase(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox().__enter__()
        self.addCleanup(self.s.__exit__, None, None, None)
        self.limits, self.real_run = {}, gitx._step_shell

    def ci(self, text, *flags, slow=()):
        """`dev ci --run` on a branch with workflow `text` -> (rc, output); each step's limit lands in self.limits,
        and a step whose command is in `slow` runs past it."""
        self.s.commit("ci: the workflow", files={".github/workflows/ci.yml": text})

        def spy(argv, *args, **kwargs):  # the step's own shell, as run_local_ci starts it
            self.limits[argv[-1]] = kwargs.get("timeout")
            if argv[-1] in slow:
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
            return self.real_run(argv, *args, **kwargs)
        with mock.patch.object(gitx, "_step_shell", spy):
            rc, out, err = dev("ci", "--run", *flags)
        return rc, out + err

    def record(self, step, secs, rc=0, how="full"):
        """An earlier local run of `test › <step>`, as dev ci --run records it."""
        gitx._save_step(gitx.ctx(self.s.repo), f"test › {step}",
                        {"job": "test", "step": step, "rc": rc, "secs": secs, "how": how, "ts": 1795140000})


class WorkflowLimitTests(StepLimitCase):
    def setUp(self):
        super().setUp()
        self.s.branch("feat/ci")

    def test_the_jobs_timeout_minutes_is_the_limit_of_its_steps_unless_the_command_line_gives_one(self):
        rc, out = self.ci(workflow(job_timeout=45))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.limits, {"test -f README.md": 45 * 60})
        rc, out = self.ci(workflow(job_timeout=45) + "# again\n", "--step-timeout", "5")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.limits, {"test -f README.md": 5 * 60})

    def test_a_steps_own_timeout_minutes_wins_over_its_jobs(self):
        rc, out = self.ci(workflow(job_timeout=40, step_timeout=50))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.limits, {"test -f README.md": 50 * 60})

    def test_a_step_past_its_limit_ends_124_and_names_the_limit_and_its_source(self):
        rc, out = self.ci(workflow(job_timeout=45), slow=("test -f README.md",))
        self.assertEqual(rc, 1, out)
        self.assertIn("timed out after 45 min (limit from the job's timeout-minutes)", out)
        self.assertEqual(gitx.step_records(gitx.ctx(self.s.repo))["test › Unit tests"]["rc"], 124)


    def test_a_step_past_its_limit_names_the_command_that_gives_it_twice_the_time(self):
        # a timed-out run records no pass, so the step's history never learns that it needs longer
        rc, out = self.ci(workflow(job_timeout=45), slow=("test -f README.md",))
        self.assertEqual(rc, 1, out)
        self.assertIn("dev ci --run --resume --step-timeout 90", out)

    def test_a_step_limit_under_one_minute_is_refused(self):
        for minutes in ("0", "-5"):  # 0 was ignored, and -5 ended every step at once with exit 124
            with self.subTest(minutes=minutes):
                with self.assertRaises(SystemExit) as refused, contextlib.redirect_stderr(io.StringIO()):
                    dev("ci", "--run", "--step-timeout", minutes)
                self.assertEqual(refused.exception.code, 2)


class StepStopTests(StepLimitCase):
    """A step stopped at its limit or by Ctrl-C stops every process it started, within seconds. The step's child
    sleeps ten minutes, so a stop that only waits for it fails the bound, and a child that ignores SIGTERM ends only
    with SIGKILL."""

    def setUp(self):
        super().setUp()
        self.s.branch("feat/ci")
        self.pidfile = self.s.repo / "child.pid"
        self.child = f"sh -c 'echo $$ > {self.pidfile}; exec sleep 600'"  # the step's child, far past any bound
        self.fired = False
        self.addCleanup(self.kill_child)

    def kill_child(self):
        with contextlib.suppress(OSError, ValueError):
            os.kill(int(self.pidfile.read_text()), signal.SIGKILL)

    def guard(self) -> float:
        """Kill the child BOUND seconds from now, so a stop that waits for it fails the test instead of hanging it
        for ten minutes (self.fired says it had to) -> the time now."""
        def fire():
            self.fired = True
            self.kill_child()
        watchdog = threading.Timer(BOUND, fire)
        watchdog.daemon = True
        self.addCleanup(watchdog.cancel)
        watchdog.start()
        return time.monotonic()

    def ctrl_c(self, times=1, apart=0.2) -> list:
        """Press Ctrl-C once the child runs, then again every `apart` seconds, `times` presses in all -> the list
        their times go in."""
        presses = []

        def press(signum, frame):
            if not presses and not (self.pidfile.exists() and self.pidfile.read_text().strip()):
                signal.setitimer(signal.ITIMER_REAL, 0.1)  # the child is not up yet
                return
            presses.append(time.monotonic())
            if len(presses) < times:
                signal.setitimer(signal.ITIMER_REAL, apart)
            raise KeyboardInterrupt
        old = signal.signal(signal.SIGALRM, press)
        self.addCleanup(signal.signal, signal.SIGALRM, old)
        self.addCleanup(signal.setitimer, signal.ITIMER_REAL, 0)
        signal.setitimer(signal.ITIMER_REAL, 0.1)
        return presses

    def step(self, run, timeout, grace=5.0):
        """The step's own shell running `run`, as dev ci --run starts it."""
        with open(os.devnull, "w") as out:
            return gitx._step_shell(["bash", "-c", run], cwd=str(self.s.repo), stdout=out, env=dict(os.environ),
                                    timeout=timeout, grace=grace)

    def assertStopped(self, took, why):
        pid = int(self.pidfile.read_text())
        self.assertFalse(alive(pid), f"the step's child {pid} still runs after {why}")
        self.assertFalse(self.fired, f"the stop waited for the step's child until the test killed it, {BOUND} s on")
        self.assertLess(took, BOUND)

    def test_a_step_past_its_limit_stops_every_process_it_started(self):
        # devkit's own unit-test step: its shell starts the suite as a child, and when the step's limit ended the
        # shell, the suite ran on for over an hour, writing the step's log
        run = "sh -c 'echo $$ > child.pid; exec sleep 600'; true"  # the shell waits for its child
        self.s.commit("ci: the workflow", files={".github/workflows/ci.yml": workflow(steps=(("Unit tests", run),))})

        def short(argv, *args, **kwargs):  # the real step, with a limit of two seconds
            return self.real_run(argv, *args, **dict(kwargs, timeout=2))
        started = self.guard()
        with mock.patch.object(gitx, "_step_shell", short):
            rc, out, err = dev("ci", "--run")
        self.assertEqual(rc, 1, out + err)
        self.assertStopped(time.monotonic() - started, "its step timed out")
        self.assertIn("passed its limit of", out)  # the step's log says why it ended

    def test_ctrl_c_during_a_step_stops_every_process_it_started(self):
        # the step runs in a session of its own, so the terminal's Ctrl-C reaches only dev ci --run
        self.ctrl_c()
        started = self.guard()
        with self.assertRaises(KeyboardInterrupt):
            self.step(f"{self.child}; true", timeout=30)
        self.assertStopped(time.monotonic() - started, "Ctrl-C")

    def test_a_child_that_ignores_sigterm_is_killed_once_the_grace_ends(self):
        # SIGTERM first, so a suite can clean up; a process that ignores it ends only with the SIGKILL after it
        started = self.guard()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.step(f"trap '' TERM; {self.child}; true", timeout=2, grace=0.5)
        self.assertStopped(time.monotonic() - started, "its step timed out")


class HistoryLimitTests(StepLimitCase):
    STEPS = (("Unit tests", "test -f README.md"), ("Lint", "test -d .git"))

    def setUp(self):
        super().setUp()
        self.s.branch("feat/ci")

    def test_without_a_workflow_timeout_the_longest_recorded_pass_sets_the_limit_above_the_floor(self):
        self.record("Unit tests", 2142.5)  # devkit's own unit-test step: 35.7 minutes -> 1.5 times, 54 minutes
        self.record("Lint", 60)  # 1.5 minutes from history: the 30-minute floor stays
        rc, out = self.ci(workflow(steps=self.STEPS))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.limits, {"test -f README.md": 54 * 60, "test -d .git": 30 * 60})

    def test_a_matrix_job_run_once_here_gets_more_than_one_legs_timeout_when_its_history_needs_it(self):
        # devkit's own test job: timeout-minutes 40 for each half of the suite on a runner; dev ci --run does not
        # expand a literal matrix, so here its step runs the whole suite, which passed in 35.7 minutes
        sharded = workflow(job_timeout=40, steps=self.STEPS[:1]).replace(
            "    timeout-minutes: 40\n", "    timeout-minutes: 40\n    strategy:\n      matrix:\n        shard: [1, 2]\n")
        self.record("Unit tests", 2142.5)
        rc, out = self.ci(sharded)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.limits, {"test -f README.md": 54 * 60})
        self.limits.clear()
        self.assertEqual(self.ci(workflow(job_timeout=40, steps=self.STEPS[:1]))[0], 0)  # no matrix: the job's own
        self.assertEqual(self.limits, {"test -f README.md": 40 * 60})

    def test_a_short_pass_keeps_the_limit_its_longest_pass_set(self):
        self.record("Unit tests", 2142.5)
        self.assertEqual(self.ci(workflow(steps=self.STEPS[:1]))[0], 0)  # passes in milliseconds this time
        self.limits.clear()
        rc, out = self.ci(workflow(steps=self.STEPS[:1]) + "# again\n")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.limits, {"test -f README.md": 54 * 60})


if __name__ == "__main__":
    unittest.main()
