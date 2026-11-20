# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""How long a CI watch waits, and when it stops waiting for a job no runner takes. A watch's limit comes from the
repository's own workflows: the longest chain of jobs that need each other, at their timeout-minutes, plus 30 minutes
for the queue, and never under the old 90-minute default; the background watch devkit starts after a push gets it
too. A GitHub job that has waited 30 minutes for a runner while nothing else of its commit runs is told to an agent's
wait at once, with the one next action, and a watch that reaches its limit names it instead of "still running"; a
job waiting for an environment's approval is not taken for one. The watch is host-independent: Claude Code and Codex
sessions follow the same watch, and `dev ci watch --wait` prints the same lines on both.

Run: python3 -m unittest tests.test_ci_watch_limits -v"""
import contextlib
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import cistatus, ciwatch  # noqa: E402
from test_ciwatch import Clock, WatchCase  # noqa: E402

CHAIN = ("name: CI\non: [pull_request]\njobs:\n"
         "  build:\n    runs-on: ubuntu-latest\n    timeout-minutes: 20\n    steps:\n      - run: make\n"
         "  test:\n    needs: build\n    runs-on: ubuntu-latest\n    timeout-minutes: 45\n    steps:\n"
         "      - run: make test\n"
         "  package:\n    needs: [test]\n    runs-on: ubuntu-latest\n    timeout-minutes: 30\n    steps:\n"
         "      - run: make dist\n"
         "  lint:\n    runs-on: ubuntu-latest\n    timeout-minutes: 10\n    steps:\n      - run: make lint\n")
NIGHTLY = ("name: nightly\non:\n  schedule:\n    - cron: '0 3 * * *'\njobs:\n  soak:\n    runs-on: ubuntu-latest\n"
           "    timeout-minutes: 300\n    steps:\n      - run: make soak\n")
UNTIMED = "name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make test\n"
CALLER = ("name: CI\non: [pull_request]\njobs:\n"
          "  build:\n    runs-on: ubuntu-latest\n    timeout-minutes: 20\n    steps:\n      - run: make\n"
          "  release:\n    needs: build\n    uses: ./.github/workflows/publish.yml\n")
PUBLISH = ("name: publish\non:\n  workflow_call:\njobs:\n"
           "  pack:\n    runs-on: ubuntu-latest\n    timeout-minutes: 50\n    steps:\n      - run: make dist\n"
           "  upload:\n    needs: pack\n    runs-on: ubuntu-latest\n    timeout-minutes: 40\n    steps:\n"
           "      - run: make upload\n")


def repo_with(**workflows) -> str:
    top = tempfile.mkdtemp()
    for name, text in workflows.items():
        f = Path(top) / ".github" / "workflows" / f"{name}.yml"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return top


def gh_run(status="in_progress", jobs=()) -> dict:
    """A run as cistatus reads it from gh, with its jobs (name, gh status, gh conclusion)."""
    now = 1_800_000_000.0
    r = cistatus.github_run({"databaseId": 7701, "number": 41, "status": status, "workflowName": "ci",
                             "headBranch": "main", "headSha": "abc1234def",
                             "url": "https://github.com/acme/api/actions/runs/7701"}, now)
    r["jobs"] = [cistatus.github_job({"name": name, "status": st, "conclusion": concl,
                                      "startedAt": "2026-11-19T10:00:00Z" if st != "queued" else None,
                                      "steps": [{"status": "in_progress", "name": "run"}] if st == "in_progress" else [],
                                      "url": f"https://github.com/acme/api/actions/runs/7701/job/{i}"}, now)
                 for i, (name, st, concl) in enumerate(jobs, 1)]
    return r


class WatchLimitTests(WatchCase):
    def test_a_watch_lasts_the_longest_needs_chain_of_job_timeouts_plus_the_queue(self):
        top = repo_with(ci=CHAIN, nightly=NIGHTLY)  # build 20 -> test 45 -> package 30, and 30 for the queue
        clock = Clock()
        res = ciwatch.watch(top, branch="main", sha="abc1234", sleep=clock.sleep, clock=clock,
                            runs_fn=self.sequence([gh_run(jobs=[("test", "in_progress", None)])]))
        self.assertEqual(res["state"], "timeout")
        self.assertGreaterEqual(res["minutes"], 125)
        self.assertLess(res["minutes"], 127)

    def test_a_job_without_timeout_minutes_counts_githubs_six_hour_default(self):
        self.assertEqual(ciwatch.watch_minutes(repo_with(ci=UNTIMED)), 360)  # 360 and the queue, at the 360 ceiling

    def test_a_local_reusable_workflow_counts_the_chain_of_its_own_jobs(self):
        top = repo_with(ci=CALLER, publish=PUBLISH)  # build 20 -> release (pack 50 -> upload 40), and the queue
        self.assertEqual(ciwatch.watch_minutes(top), 140)

    def test_the_background_watcher_never_carries_the_call_token_of_the_call_that_started_it(self):
        started = []
        spawn = types.SimpleNamespace(Popen=lambda args, **k: started.append(k) or mock.Mock(pid=os.getpid()),
                                      DEVNULL=-3)
        with mock.patch.object(ciwatch, "subprocess", spawn), \
                mock.patch.dict(os.environ, {"DEVKIT_NO_CI_WATCH": "", "DEVKIT_CALL": "toolu_01.nonce"}):
            self.assertTrue(ciwatch.start_background(repo_with(ci=CHAIN), session="claude:s1", branch="main",
                                                     sha="abc1234"))
        env = started[0].get("env")
        self.assertIsNotNone(env)
        self.assertNotIn("DEVKIT_CALL", env)
        self.assertEqual(env.get("HOME"), os.environ.get("HOME"))

    def test_the_background_watch_of_a_push_gets_the_derived_limit(self):
        top = repo_with(ci=CHAIN)
        started = []
        spawn = types.SimpleNamespace(Popen=lambda args, **k: started.append(args) or mock.Mock(pid=os.getpid()),
                                      DEVNULL=-3)
        with mock.patch.object(ciwatch, "subprocess", spawn), mock.patch.dict(os.environ, {"DEVKIT_NO_CI_WATCH": ""}):
            self.assertTrue(ciwatch.start_background(top, session="claude:s1", branch="main", sha="abc1234"))
        self.assertEqual(started[0][started[0].index("--timeout") + 1], "125")
        self.assertEqual([w.get("timeout_min") for w in ciwatch.watches()], [125])


class WatchCommandTests(unittest.TestCase):
    # `dev ci watch` in a terminal: with no --timeout it leaves the limit to the watch, which derives it
    def test_dev_ci_watch_without_timeout_lets_the_watch_take_the_repositorys_limit(self):
        from devkit import cli
        asked = []

        def watch(top, **kw):
            asked.append(kw.get("timeout_min", "missing"))
            return {"state": "none", "runs": [], "diagnoses": []}
        with mock.patch.object(ciwatch, "watch", watch), contextlib.redirect_stdout(io.StringIO()):
            cli.main(["ci", "watch", "--branch", "main", "--sha", "abc1234", "--json"])
            cli.main(["ci", "watch", "--branch", "main", "--sha", "abc1234", "--json", "--timeout", "45"])
        self.assertEqual(asked, [None, 45])


class WatchZeroTimeoutTests(unittest.TestCase):
    # `--timeout 0` meant "the default" before the limit was derived; it must not end the watch at once
    def test_dev_ci_watch_with_timeout_zero_takes_the_repositorys_limit(self):
        from devkit import cli
        asked = []

        def watch(top, **kw):
            asked.append(kw.get("timeout_min", "missing"))
            return {"state": "none", "runs": [], "diagnoses": []}
        with mock.patch.object(ciwatch, "watch", watch), contextlib.redirect_stdout(io.StringIO()):
            cli.main(["ci", "watch", "--branch", "main", "--sha", "abc1234", "--json", "--timeout", "0"])
            cli.main(["ci", "watch", "--branch", "main", "--sha", "abc1234", "--json", "--timeout", "-5"])
        self.assertEqual(asked, [None, None])


class StuckJobTests(WatchCase):
    def test_a_job_no_runner_takes_is_named_with_its_next_action_once_nothing_else_of_its_commit_runs(self):
        # the deploy job's runs-on names a self-hosted label no runner has; the release job waits for an approval.
        # The watch follows the run to its limit (a busy queue of GitHub's own runners looks the same and still ends)
        busy = gh_run(jobs=[("unit", "in_progress", None), ("deploy", "queued", None), ("release", "waiting", None)])
        idle = gh_run(jobs=[("unit", "completed", "success"), ("deploy", "queued", None), ("release", "waiting", None)])
        clock, heard = Clock(), []
        start = clock()
        res = ciwatch.watch(repo_with(ci=CHAIN), branch="main", sha="abc1234", sleep=clock.sleep, clock=clock,
                            runs_fn=self.sequence(*[[busy]] * 90, [idle]),  # unit runs 40 minutes
                            on_event=lambda line: heard.append(((clock() - start) / 60, line)))
        told = [(at, line) for at, line in heard if "for a runner" in line]
        self.assertEqual(len(told), 1, heard)  # once, 30 minutes after nothing else ran, not 30 from the start
        self.assertTrue(69 <= told[0][0] < 72, told)
        self.assertIn("job “deploy” of ci #41 has waited 30 min for a runner", told[0][1])
        self.assertIn("runs-on", told[0][1])
        self.assertEqual(res["state"], "timeout")  # the limit, 125 minutes: exit 3, the watch ended before the run
        self.assertEqual([(s["job"], s["run"], s["number"]) for s in res["stuck"]], [("deploy", "ci", 41)])
        self.assertRegex(ciwatch.message(res), r"job “deploy” of ci #41 has waited 8\d min for a runner")
        last = ciwatch.wait_lines(res, "dev ci watch --wait")[-1]
        self.assertTrue(last.startswith("CI still running · "), last)
        self.assertRegex(last, r"job “deploy” of ci #41 has waited 8\d min for a runner.*re-arm: dev ci watch --wait")

    def test_a_run_waiting_for_its_concurrency_group_is_not_taken_for_one_waiting_for_a_runner(self):
        # GitHub's "pending": the run waits until an earlier run of its concurrency group ends, not for a runner
        clock, heard = Clock(), []
        res = ciwatch.watch(repo_with(ci=CHAIN), branch="main", sha="abc1234", sleep=clock.sleep, clock=clock,
                            runs_fn=self.sequence([gh_run(status="pending")]), on_event=heard.append)
        self.assertEqual(res["state"], "timeout")
        self.assertNotIn("stuck", res)
        self.assertEqual([line for line in heard if "for a runner" in line], [])


if __name__ == "__main__":
    unittest.main()
