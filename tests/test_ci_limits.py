# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev ci --run` gives each local step the time its project allows: `--step-timeout` when given; else the step's own
`timeout-minutes`, then its job's; else 1.5 times the longest pass recorded for the step, rounded up to a minute and
never below the floor; else the floor of 30 minutes. A step that runs past its limit ends with exit 124 and says the
limit and where it came from. devkit's own unit-test step takes about 36 minutes and used to end with 124 at 30.

Run: python3 -m unittest tests.test_ci_limits -v"""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import gitx  # noqa: E402
from test_gitx import Sandbox, dev  # noqa: E402


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
        self.limits, self.real_run = {}, subprocess.run

    def ci(self, text, *flags, slow=()):
        """`dev ci --run` on a branch with workflow `text` -> (rc, output); each step's limit lands in self.limits,
        and a step whose command is in `slow` runs past it."""
        self.s.commit("ci: the workflow", files={".github/workflows/ci.yml": text})

        def spy(argv, *args, **kwargs):  # the step's own shell, as run_local_ci starts it
            if isinstance(argv, list) and argv[:1] == ["bash"]:
                self.limits[argv[-1]] = kwargs.get("timeout")
                if argv[-1] in slow:
                    raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
            return self.real_run(argv, *args, **kwargs)
        with mock.patch.object(gitx.subprocess, "run", spy):
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

    def test_a_short_pass_keeps_the_limit_its_longest_pass_set(self):
        self.record("Unit tests", 2142.5)
        self.assertEqual(self.ci(workflow(steps=self.STEPS[:1]))[0], 0)  # passes in milliseconds this time
        self.limits.clear()
        rc, out = self.ci(workflow(steps=self.STEPS[:1]) + "# again\n")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.limits, {"test -f README.md": 54 * 60})


if __name__ == "__main__":
    unittest.main()
