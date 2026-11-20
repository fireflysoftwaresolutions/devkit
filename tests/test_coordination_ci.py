# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Local CI and CI diagnosis: a green local run is not cut short by a limit shorter than the workflow allows, and a
run whose jobs never started names the host's reason. Each test reproduces a fault the crew hit on devkit 26.11.25
(2026-11-19), through `dev ci --run`, `dev run view --failed` (the fake gh) and the CI watch (a stand-in gh first on
PATH). Run: python3 -m unittest tests.test_coordination_ci"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import ciwatch, gitx  # noqa: E402
from test_ciwatch import Clock, WatchCase, run  # noqa: E402
from test_gitx import Sandbox, dev  # noqa: E402

BUDGET = "The job was not started because an Actions budget is preventing further use."
JOB_ID = "135985601878"
RUN_ID = "44645118961"


class LocalStepLimitTests(unittest.TestCase):
    def test_a_step_gets_the_time_its_workflow_job_allows(self):
        # devkit's own unit-test step takes about 36 minutes; its job allows 40 (timeout-minutes), and dev ci --run
        # stopped the green suite at its own 30-minute default with exit 124
        workflow = ("name: CI\non: [pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
                    "    timeout-minutes: 40\n    steps:\n      - name: Unit tests\n        run: test -f README.md\n")
        limits, real_run = [], subprocess.run

        def spy(argv, *args, **kwargs):  # the step's own shell, as run_local_ci starts it
            if isinstance(argv, list) and argv[:1] == ["bash"] and argv[-1] == "test -f README.md":
                limits.append(kwargs.get("timeout"))
            return real_run(argv, *args, **kwargs)
        with Sandbox() as s:
            s.write(".github/workflows/ci.yml", workflow)
            s.branch("feat/ci")
            s.commit("ci: add the workflow")
            with mock.patch.object(gitx.subprocess, "run", spy):
                rc, out, err = dev("ci", "--run")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len(limits), 1, limits)
        self.assertGreaterEqual(limits[0], 40 * 60)


class JobNeverStartedTests(unittest.TestCase):
    def test_run_view_failed_names_the_annotation_of_a_job_that_never_started(self):
        # B-igm6xuux0ffp: the Signature PR run started no job; `dev run view --failed` said "failing log
        # unavailable: log not found", and only the check run's annotation said why
        with Sandbox() as s:
            s.gh.on(["run", "view"], {"workflowName": "ci", "number": 40, "conclusion": "failure",
                                      "headBranch": "release/26.11.25", "headSha": "0bde25cdef", "event": "pull_request",
                                      "url": f"https://github.com/acme/widget/actions/runs/{RUN_ID}",
                                      "jobs": [{"name": "tests (Python 3.13, part 1 of 2)", "conclusion": "failure",
                                                "databaseId": int(JOB_ID), "steps": []}]})
            s.gh.on(["run", "view"], rc=1, err=f"failed to get run log: log not found: {JOB_ID}",
                    when=lambda a: "--log-failed" in a)
            s.gh.on(["api"], [{"annotation_level": "failure", "message": BUDGET, "path": ".github"}],
                    when=lambda a: any("annotations" in x for x in a))
            rc, out, err = dev("run", "view", RUN_ID, "--failed")
        self.assertEqual(rc, 1)
        self.assertIn("an Actions budget is preventing further use", out + err)


class WatchNeverStartedTests(WatchCase):
    def setUp(self):
        super().setUp()
        ciwatch.failed_log = self.saved[4]  # the real reader: it asks the stand-in gh below
        self.top = tempfile.mkdtemp(dir=self.tmp.name)
        tools = Path(self.tmp.name) / "bin"
        tools.mkdir()
        gh = tools / "gh"
        gh.write_text("#!/bin/sh\n"
                      'case "$*" in\n'
                      f'  *--log-failed*) echo "failed to get run log: log not found: {JOB_ID}" >&2; exit 1 ;;\n'
                      f'  *annotations*) echo \'[{{"annotation_level": "failure", "message": "{BUDGET}"}}]\' ;;\n'
                      '  *) echo "no canned answer: gh $*" >&2; exit 1 ;;\n'
                      "esac\n")
        gh.chmod(0o755)
        path = mock.patch.dict(os.environ, {"PATH": str(tools) + os.pathsep + os.environ["PATH"]})
        path.start()
        self.addCleanup(path.stop)

    def test_the_ci_watch_names_why_a_job_never_started(self):
        # B-igm6xuux0ffp: `dev ci watch` reported the failed run with class unknown and no first error
        url = f"https://github.com/acme/api/actions/runs/{RUN_ID}/job/{JOB_ID}"
        failed = run("failed", rid=RUN_ID, n=40, conclusion="failure",
                     jobs=[{"name": "tests (Python 3.13, part 1 of 2)", "status": "failed", "current": "",
                            "url": url}])
        clock = Clock()
        res = ciwatch.watch(self.top, branch="release/26.11.25", sha="abc1234", sleep=clock.sleep, clock=clock,
                            runs_fn=self.sequence([run("running", rid=RUN_ID, n=40)], [failed]))
        self.assertEqual(res["state"], "failed")
        self.assertIn("an Actions budget is preventing further use", ciwatch.message(res))


if __name__ == "__main__":
    unittest.main()
