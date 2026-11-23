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
from test_gitx import Sandbox, dev, git  # noqa: E402


BOUND = 20  # seconds: a stopped step returns well within this, while its child would sleep for ten minutes


def alive(pid: int) -> bool:
    return subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0


# a `dev` process running one step, as dev ci --run does: argv lib, cwd, the step's command, SIGHUP's disposition, limit
DEV_RUNNING_A_STEP = """import os, signal, subprocess, sys
sys.path.insert(0, sys.argv[1])
from devkit import gitx
signal.signal(signal.SIGHUP, signal.SIG_IGN if sys.argv[4] == "ignored" else signal.SIG_DFL)
try:
    gitx._step_shell(["bash", "-c", sys.argv[3]], cwd=sys.argv[2], stdout=sys.stdout, env=dict(os.environ),
                     timeout=float(sys.argv[5]))
except subprocess.TimeoutExpired:
    sys.exit(7)
"""


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
        for sig in (signal.SIGTERM, signal.SIGHUP):  # each test starts from the defaults, whatever ran before it
            self.addCleanup(signal.signal, sig, signal.signal(sig, signal.SIG_DFL))

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
        self.assertRegex(out, r"passed its limit of [\d.]+ min; its processes were stopped")  # the log says why

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

    def test_a_second_ctrl_c_during_the_grace_ends_it_at_once_and_still_kills_and_reaps_the_step(self):
        # a second Ctrl-C used to end the wait before the SIGKILL: a child that ignores SIGTERM ran on, and so did
        # the step's shell, never reaped
        shells, real = [], subprocess.Popen

        def popen(*args, **kwargs):
            shells.append(real(*args, **kwargs))
            return shells[-1]
        presses = self.ctrl_c(times=2, apart=1.0)  # after Popen.wait's own 0.25 s on the first press: in the grace
        started = self.guard()
        with self.assertRaises(KeyboardInterrupt), mock.patch.object(gitx.subprocess, "Popen", popen):
            self.step(f"trap '' TERM; {self.child}; true", timeout=30, grace=10)
        returned = time.monotonic()
        self.assertEqual(len(presses), 2)
        self.assertStopped(returned - started, "a second Ctrl-C")
        self.assertIsNotNone(shells[0].returncode, "the step's shell was left unreaped")
        self.assertLess(returned - presses[1], 2, "the second Ctrl-C waited out the 10-second grace")

    def dev_running_a_step(self, sighup="default", limit=600):
        """A `dev` process running one step whose child sleeps ten minutes -> (the process, the child's pid), once the
        child runs."""
        lib = Path(gitx.__file__).resolve().parents[1]
        run = f"sh -c 'echo $$ > {self.pidfile}; echo up; exec sleep 600'; true"
        dev_ = subprocess.Popen([sys.executable, "-c", DEV_RUNNING_A_STEP, str(lib), str(self.s.repo), run, sighup,
                                 str(limit)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(dev_.stdout.close)
        self.addCleanup(dev_.kill)
        self.assertEqual(dev_.stdout.readline(), "up\n")  # the step's child runs
        pid = int(self.pidfile.read_text())
        self.addCleanup(subprocess.run, ["kill", "-9", str(pid)], capture_output=True)
        return dev_, pid

    def test_sigterm_or_sighup_to_dev_stops_the_step_it_runs(self):
        # the step runs in a session of its own, so a stopped agent task (SIGTERM) or a closed terminal (SIGHUP) no
        # longer reaches it: dev stops the step's processes, then exits as the signal asks
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig.name):
                dev_, pid = self.dev_running_a_step()
                dev_.send_signal(sig)
                self.assertEqual(dev_.wait(timeout=BOUND), 128 + sig)
                self.assertFalse(alive(pid), f"the step's child {pid} still runs after {sig.name} to dev")

    def test_a_signal_while_popen_still_starts_the_step_shell_stops_the_step_too(self):
        # under load SIGTERM or SIGHUP, sent once the step's child ran, raised dev's exit inside Popen, before dev held
        # the shell to stop: dev exited 143 or 129 and the step ran on; a Ctrl-C there left it running the same way.
        # Here dev stays inside Popen's start of the shell, which already runs, until a signal comes, and its handler
        # runs there, as under load
        starting = f"""import os, select, signal, subprocess
start = subprocess.Popen._execute_child

def starting(self, *args, **kwargs):
    start(self, *args, **kwargs)
    r, w = os.pipe()
    os.set_blocking(w, False)
    old = signal.set_wakeup_fd(w)
    try:
        select.select([r], [], [], {BOUND})
    finally:
        signal.set_wakeup_fd(old)
        os.close(r)
        os.close(w)
subprocess.Popen._execute_child = starting
"""
        for sig, rc in ((signal.SIGTERM, 128 + signal.SIGTERM), (signal.SIGHUP, 128 + signal.SIGHUP),
                        (signal.SIGINT, -signal.SIGINT)):  # an uncaught KeyboardInterrupt ends Python by SIGINT
            with self.subTest(signal=sig.name), mock.patch(f"{__name__}.DEV_RUNNING_A_STEP",
                                                           starting + DEV_RUNNING_A_STEP):
                dev_, pid = self.dev_running_a_step()
                dev_.send_signal(sig)
                self.assertEqual(dev_.wait(timeout=BOUND), rc)
                self.assertFalse(alive(pid), f"the step's child {pid} still runs after {sig.name} to dev")

    def test_dev_gets_its_own_signal_handlers_back_once_the_step_ends(self):
        self.assertEqual(self.step("true", timeout=30).returncode, 0)
        for sig in (signal.SIGTERM, signal.SIGHUP):
            self.assertIs(signal.getsignal(sig), signal.SIG_DFL, f"{sig.name} keeps the step's handler")

    def test_a_sighup_dev_ignores_leaves_the_step_running(self):
        # under nohup a closed terminal must not stop the step: it runs on to its limit of two seconds
        dev_, pid = self.dev_running_a_step(sighup="ignored", limit=2)
        dev_.send_signal(signal.SIGHUP)
        self.assertEqual(dev_.wait(timeout=BOUND), 7)  # TimeoutExpired: the step ran until its limit
        self.assertFalse(alive(pid))


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


class CheckoutTests(StepLimitCase):
    """Each checkout of a project (its main checkout and every worktree) keeps its own step logs and records."""

    SHARDED = workflow(job_timeout=40, steps=(("Unit tests", "test -f README.md && pwd"),)).replace(
        "    timeout-minutes: 40\n", "    timeout-minutes: 40\n    strategy:\n      matrix:\n        shard: [1, 2]\n")

    def run_in(self, checkout):
        """`dev ci --run` in `checkout` -> (rc, output); each step's limit is appended to self.given."""
        def spy(argv, *args, **kwargs):
            self.given.append(kwargs.get("timeout"))
            return self.real_run(argv, *args, **kwargs)
        here = os.getcwd()
        os.chdir(checkout)
        try:
            with mock.patch.object(gitx, "_step_shell", spy):
                rc, out, err = dev("ci", "--run")
        finally:
            os.chdir(here)
        return rc, out + err

    # dev ci --run kept one folder of step logs and one file of step records per project, and emptied the folder when
    # it started: two worktrees of one project running it at once deleted or swapped each other's logs, and each
    # --resume read the other's records. The longest pass of any checkout still sets a step's limit, so a new
    # worktree's whole suite is not cut at one matrix leg's timeout-minutes.
    def test_two_checkouts_of_a_project_keep_their_own_logs_and_records_and_share_the_longest_pass(self):
        self.given = []
        self.s.branch("feat/ci")
        self.record("Unit tests", 2142.5)  # an earlier pass in the main checkout: 35.7 minutes
        self.s.commit("ci: the workflow", files={".github/workflows/ci.yml": self.SHARDED})
        other = self.s.root / "widget-wt"
        git(self.s.repo, "worktree", "add", "-q", "-b", "feat/other", str(other))
        self.s.commit("feat: a change of its own", repo=other)
        self.assertEqual(gitx.ctx(other).pid, gitx.ctx(self.s.repo).pid)  # one project
        for checkout in (self.s.repo, other):
            rc, out = self.run_in(checkout)
            self.assertEqual(rc, 0, out)
        logs = sorted(p.read_text().strip() for p in (gitx._ci_dir() / "logs").rglob("*.log"))
        self.assertEqual(logs, sorted([str(self.s.repo), str(other.resolve())]))
        for checkout in (self.s.repo, other):
            with self.subTest(checkout=checkout.name):
                rec = gitx.step_records(gitx.ctx(checkout))["test › Unit tests"]
                self.assertEqual(rec["sha"], git(checkout, "rev-parse", "HEAD").strip())
        self.assertEqual(self.given, [54 * 60, 54 * 60])

    # Each worktree has a folder of logs and a file of records of its own, so the ones of a worktree removed long ago
    # go once nothing has written them for 30 days, as the local CI evidence does.
    def test_the_logs_and_records_of_a_checkout_untouched_for_30_days_go(self):
        self.given = []
        self.s.branch("feat/ci")
        self.s.commit("ci: the workflow", files={".github/workflows/ci.yml": self.SHARDED})
        old, recent = time.time() - 31 * 86400, time.time() - 86400
        kept = []
        for name, when in (("acme__gone@0123456789ab", old), ("acme__live@ba9876543210", recent)):
            logs, steps = gitx._ci_dir() / "logs" / name, gitx._ci_dir() / "steps" / f"{name}.json"
            logs.mkdir(parents=True)
            steps.parent.mkdir(parents=True, exist_ok=True)
            (logs / "01-test-unit-tests.log").write_text("ok\n")
            steps.write_text("{}")
            for f in (logs / "01-test-unit-tests.log", logs, steps):
                os.utime(f, (when, when))
            kept.append((logs, steps))
        rc, out = self.run_in(self.s.repo)
        self.assertEqual(rc, 0, out)
        (gone_logs, gone_steps), (live_logs, live_steps) = kept
        self.assertEqual((gone_logs.exists(), gone_steps.exists()), (False, False))
        self.assertEqual((live_logs.exists(), live_steps.exists()), (True, True))


if __name__ == "__main__":
    unittest.main()
