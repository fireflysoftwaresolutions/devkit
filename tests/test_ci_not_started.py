# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""A CI job that never started has no log: devkit reads its check run's annotations and names the cause, with the one
next action. `dev run view --failed`, `dev pr checks` (gitx.failing_logs) and the CI watch (ciwatch.diagnose) share
one reader (ciwatch.never_started). A job is classed `not_started` (subclass `budget` for an account's spending limit
or failed payments, which no code change or push fixes), and a run that failed at startup `workflow_invalid`. The
annotation is host text: one line, control characters stripped, secrets redacted, at most 300 characters. Every gh
call goes to a fake (gitx's FakeGH, or a stand-in gh first on PATH); no test reaches GitHub.

Run: python3 -m unittest tests.test_ci_not_started -v"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import ciwatch, gitx  # noqa: E402
from test_gitx import Sandbox, dev  # noqa: E402

BUDGET = "The job was not started because an Actions budget is preventing further use."
NO_RUNNER = "The job was not acquired by Runner of type self-hosted even after multiple attempts"
JOB_ID, RUN_ID = "135985601878", "44645118961"
JOB_URL = f"https://github.com/acme/api/actions/runs/{RUN_ID}/job/{JOB_ID}"


class GitxNeverStartedTests(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox().__enter__()
        self.addCleanup(self.s.__exit__, None, None, None)
        self.s.gh.on(["run", "view"], rc=1, err=f"failed to get run log: log not found: {JOB_ID}",
                     when=lambda a: "--log-failed" in a)
        self.s.gh.on(["api"], [{"annotation_level": "failure", "message": BUDGET, "path": ".github"}],
                     when=lambda a: a[1:2] == [f"repos/acme/widget/check-runs/{JOB_ID}/annotations"])

    def test_run_view_failed_names_why_a_job_never_started_and_the_one_next_action(self):
        self.s.gh.on(["run", "view"], {"workflowName": "ci", "number": 40, "conclusion": "failure",
                                       "headBranch": "release/26.11.25", "headSha": "0bde25cdef", "event": "pull_request",
                                       "url": JOB_URL, "jobs": [{"name": "tests (part 1 of 2)", "conclusion": "failure",
                                                                 "databaseId": int(JOB_ID), "steps": []}]},
                     when=lambda a: "--json" in a)
        rc, out, err = dev("run", "view", RUN_ID, "--failed")
        self.assertEqual(rc, 1, out + err)
        self.assertIn(f"job “tests (part 1 of 2)” never started: {BUDGET} (budget)", out + err)
        self.assertIn("an account limit, not the code", out + err)
        self.assertNotIn("log unavailable", out + err)

    def test_pr_checks_name_why_a_failed_job_never_started_instead_of_a_missing_log(self):
        checks = [{"name": "tests (part 1 of 2)", "bucket": "fail", "link": f"https://github.com/acme/widget/actions/"
                                                                              f"runs/{RUN_ID}/job/{JOB_ID}"}]
        lines = dict(gitx.failing_logs(gitx.ctx(self.s.repo), checks))["tests (part 1 of 2)"]
        self.assertEqual(lines[0], f"never started: {BUDGET} (budget)")
        self.assertTrue(any("an account limit, not the code" in ln for ln in lines), lines)


class StandInGh(unittest.TestCase):
    """A stand-in gh first on PATH that answers a job's annotations with `self.notes` and fails every other call."""
    notes = []

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tools = Path(tmp.name)
        (tools / "notes.json").write_text(json.dumps(self.notes))
        (tools / "gh").write_text("#!/bin/sh\n"
                                  'case "$*" in\n'
                                  f'  "api repos/acme/api/check-runs/{JOB_ID}/annotations") cat "{tools}/notes.json" ;;\n'
                                  '  *) echo "no canned answer: gh $*" >&2; exit 1 ;;\n'
                                  "esac\n")
        (tools / "gh").chmod(0o755)
        path = mock.patch.dict(os.environ, {"PATH": str(tools) + os.pathsep + os.environ["PATH"]})
        path.start()
        self.addCleanup(path.stop)
        log = mock.patch.object(ciwatch, "failed_log", lambda *a, **k: "")  # a job that never ran has no log
        log.start()
        self.addCleanup(log.stop)

    def diagnose(self, conclusion="failure", jobs=None):
        run = {"id": RUN_ID, "name": "ci", "number": 40, "status": "failed", "conclusion": conclusion,
               "url": f"https://github.com/acme/api/actions/runs/{RUN_ID}", "jobs": jobs if jobs is not None else [
                   {"name": "tests (part 1 of 2)", "status": "failed", "current": "", "url": JOB_URL,
                    "steps_total": 0, "started": 0}]}
        return ciwatch.diagnose("github", tempfile.gettempdir(), run)  # the repository: from the job's or run's link


class BudgetDiagnosisTests(StandInGh):
    notes = [{"annotation_level": "warning", "message": "Node.js 16 actions are deprecated."},
             {"annotation_level": "failure", "message": BUDGET}]

    def test_a_job_an_actions_budget_kept_from_starting_is_not_started_budget_with_its_cause(self):
        d = self.diagnose()
        self.assertEqual((d["class"], d["subclass"], d["cause"]), ("not_started", "budget", BUDGET))
        self.assertIn("do not push again", d["action"])
        said = ciwatch.message({"state": "failed", "branch": "main", "sha": "abc1234", "minutes": 1,
                                "runs": [{"name": "ci", "number": 40, "status": "failed", "conclusion": "failed"}],
                                "diagnoses": [d]})
        self.assertIn(f"job “tests (part 1 of 2)” never started: {BUDGET} (budget)", said)


class NoRunnerDiagnosisTests(StandInGh):
    notes = [{"annotation_level": "failure", "message": NO_RUNNER}]

    def test_a_job_no_runner_took_is_not_started_without_the_budget_subclass(self):
        d = self.diagnose()
        self.assertEqual((d["class"], d["subclass"], d["cause"]), ("not_started", "", NO_RUNNER))
        self.assertIn("runs-on", d["action"])


class HostTextTests(StandInGh):
    notes = [{"annotation_level": "failure",
              "message": "The job was not started:\n\x1b[31mtoken ghp_" + "a" * 36 + "\x1b[0m " + "x" * 400}]

    def test_an_annotation_is_shown_as_one_redacted_line_of_at_most_300_characters(self):
        cause = self.diagnose()["cause"]
        self.assertLessEqual(len(cause), 300)
        self.assertTrue(cause.startswith("The job was not started: token "), cause)
        self.assertNotIn("ghp_" + "a" * 36, cause)
        self.assertFalse(any(ch in cause for ch in "\n\r\x1b"), repr(cause))


class GitxStoppedRunTests(unittest.TestCase):
    """What `dev run view --failed` and `dev pr checks` must not take for a job that never started: a matrix leg the
    run cancelled, a failed test whose name holds an account word, and a run GitHub could not start at all."""

    def setUp(self):
        self.s = Sandbox().__enter__()
        self.addCleanup(self.s.__exit__, None, None, None)
        self.s.gh.on(["run", "view"], rc=1, err=f"failed to get run log: log not found: {JOB_ID}",
                     when=lambda a: "--log-failed" in a)

    def run_view(self, conclusion, jobs):
        self.s.gh.on(["run", "view"], {"workflowName": "ci", "number": 40, "conclusion": conclusion,
                                       "headBranch": "main", "headSha": "0bde25cdef", "event": "pull_request",
                                       "url": JOB_URL, "jobs": jobs}, when=lambda a: "--json" in a)
        return dev("run", "view", RUN_ID, "--failed")

    def test_run_view_failed_leaves_a_matrix_leg_fail_fast_cancelled_out_of_the_jobs_that_never_started(self):
        self.s.gh.on(["run", "view"], "tests (part 1 of 2)\tUnit tests\tFAILED tests/test_x.py::test_y - AssertionError",
                     when=lambda a: "--log-failed" in a)
        rc, out, err = self.run_view("failure", [
            {"name": "tests (part 1 of 2)", "conclusion": "failure", "databaseId": 1001,
             "steps": [{"name": "Unit tests", "conclusion": "failure"}]},
            {"name": "tests (part 2 of 2)", "conclusion": "cancelled", "databaseId": 1002, "steps": []}])
        self.assertEqual(rc, 1, out + err)
        self.assertNotIn("never started", out + err)
        self.assertIn("FAILED tests/test_x.py::test_y", out + err)

    def test_run_view_failed_classes_a_run_github_could_not_start_and_fails(self):
        rc, out, err = self.run_view("startup_failure", [])
        self.assertEqual(rc, 1, out + err)
        self.assertIn("class workflow_invalid", out + err)
        self.assertIn("dev ci audit", out + err)

    def test_pr_checks_keep_a_failed_test_named_billing_a_missing_log_not_an_account_limit(self):
        self.s.gh.on(["api"], [{"annotation_level": "failure", "message": "tests/test_billing.py::test_total - "
                                                                         "AssertionError"}],
                     when=lambda a: a[1:2] == ["repos/acme/widget/check-runs/2002/annotations"])
        checks = [{"name": "unit", "bucket": "fail",
                   "link": f"https://github.com/acme/widget/actions/runs/{RUN_ID}/job/2002"}]
        lines = dict(gitx.failing_logs(gitx.ctx(self.s.repo), checks))["unit"]
        self.assertEqual(lines, [f"(log unavailable: failed to get run log: log not found: {JOB_ID})"])


class CancelledDiagnosisTests(StandInGh):
    notes = [{"annotation_level": "failure",
              "message": "Canceling since a higher priority waiting request for 'ci-refs/heads/main' exists"}]
    QUEUED_LEG = {"name": "tests (part 2 of 2)", "status": "cancelled", "current": "", "url": JOB_URL,
                  "steps_total": 0, "started": 0}

    def test_a_run_a_newer_push_cancelled_is_cancelled_not_never_started(self):
        # concurrency's cancel-in-progress: the job was still queued, so it has no steps and no start
        d = self.diagnose(conclusion="cancelled", jobs=[self.QUEUED_LEG])
        self.assertEqual((d["class"], d["subclass"], d["never_started"]), ("cancelled", "", None))
        self.assertIn("newer push", d["action"])

    def test_a_matrix_leg_fail_fast_cancelled_is_not_a_job_that_never_started(self):
        ran = {"name": "tests (part 1 of 2)", "status": "failed", "current": "Unit tests",
               "url": f"https://github.com/acme/api/actions/runs/{RUN_ID}/job/1001", "steps_total": 6,
               "started": 1_800_000_000}
        d = self.diagnose(conclusion="failure", jobs=[ran, self.QUEUED_LEG])
        self.assertNotEqual(d["class"], "not_started")
        self.assertIsNone(d["never_started"])


class StartupFailureTests(StandInGh):
    def test_a_run_that_failed_at_startup_is_an_invalid_workflow(self):
        d = self.diagnose(conclusion="startup_failure", jobs=[])
        self.assertEqual(d["class"], "workflow_invalid")
        self.assertIn("dev ci audit", d["action"])


if __name__ == "__main__":
    unittest.main()
