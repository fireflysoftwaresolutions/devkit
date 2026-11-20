#!/usr/bin/env python3
# Code a crew member writes, run by the tech lead: a workflow step, a test, a script. devkit's own runners
# (dev ci --run, dev test, dev test prove) start the project's code without the call token and the lane marks, so a dev
# command in it names nobody and its state change is refused (caller.no-token). Code the tech lead runs by a raw
# command still runs in the tech lead's call: its state changes go through and are audited when the call ends
# (caller.token-unlisted-use), while the call text's own dev commands are not. Real processes, on both hosts.
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
sys.path.insert(0, str(ROOT / "lib"))

from devkit import identity  # noqa: E402
from test_pre_tool_hook import PreToolCase  # noqa: E402

WORKFLOW = ("name: ci\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - run: dev progress add planted-step\n")
PLANTED_TEST = ("import subprocess, unittest\n\n\nclass Planted(unittest.TestCase):\n    def test_it(self):\n"
                "        subprocess.run(['dev', 'progress', 'add', 'planted-test'])\n")


class RunnerCase(PreToolCase):
    def on(self, agent):
        if agent == "codex":
            self.codex_policy("never")  # every call stamped: the planted code would inherit a live token
        return self.lead_on(agent)

    @staticmethod
    def plan(s):
        return [st["name"] for st in s.run_doc()["plan"]]

    def both(self, fn):
        for n, agent in enumerate(("claude", "codex")):
            with self.subTest(agent=agent):
                if n:
                    self.setUp()  # a new Mac: the first one's hosts and files go at the test's end
                fn(self.on(agent))


class DevkitRunnerTests(RunnerCase):
    def test_a_planted_workflow_step_under_dev_ci_run_is_refused(self):
        def check(s):
            wf = s.repo / ".github" / "workflows"
            wf.mkdir(parents=True, exist_ok=True)
            (wf / "ci.yml").write_text(WORKFLOW)
            s.sh("dev ci --run")
            self.assertNotIn("planted-step", self.plan(s))
            self.assertTrue(self.audit("caller.no-token"))
        self.both(check)

    def test_a_planted_test_under_dev_test_is_refused(self):
        def check(s):
            (s.repo / "tests").mkdir(exist_ok=True)
            (s.repo / "tests" / "test_planted.py").write_text(PLANTED_TEST)
            r = s.sh("dev test run tests/test_planted.py")
            self.assertIn("1 passed", r["out"] + r["err"])  # the test ran
            self.assertNotIn("planted-test", self.plan(s))
            self.assertTrue(self.audit("caller.no-token"))
        self.both(check)

    def test_a_planted_git_hook_under_dev_commit_is_refused(self):
        def check(s):
            hook = s.repo / ".git" / "hooks" / "pre-commit"
            hook.parent.mkdir(parents=True, exist_ok=True)
            hook.write_text("#!/bin/sh\ndev progress add planted-hook\nexit 0\n")  # it never stops the commit
            hook.chmod(0o755)
            (s.repo / "notes.txt").write_text("notes\n")
            s.sh("git update-ref refs/remotes/origin/main main && "  # origin's default branch, as a clone knows it
                 "git symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/main && git switch -q -c docs/notes")
            r = s.sh('dev commit -m "docs: add notes" --why "notes for the team" --verified "read them" notes.txt')
            self.assertEqual(r["rc"], 0, r["out"] + r["err"])  # the hook ran, and the commit went through
            self.assertNotIn("planted-hook", self.plan(s))
            self.assertTrue(self.audit("caller.no-token"))
        self.both(check)

    def test_the_runner_env_drops_the_token_and_the_lane_marks_and_keeps_the_rest(self):
        env = {identity.CALL_VAR: "toolu_1.x", identity.LANE_VAR: "a_tank", identity.SPEAKER_VAR: "main",
               "DEVKIT_SESSION": "claude:s1", "PATH": "/usr/bin", "CI": "true"}
        self.assertEqual(identity.runner_env(env), {"DEVKIT_SESSION": "claude:s1", "PATH": "/usr/bin", "CI": "true"})
        self.assertNotIn(identity.CALL_VAR, identity.runner_env(dict(os.environ, DEVKIT_CALL="toolu_1.x")))


class RawCommandTests(RunnerCase):
    def test_a_planted_dev_command_a_raw_test_command_runs_is_audited_and_the_calls_own_is_not(self):
        def check(s):
            script = s.repo / "run-tests.sh"  # what `npm test` or a git hook runs: the member's code
            script.write_text("#!/bin/sh\ndev progress add planted-raw\n")
            script.chmod(0o755)
            r = s.sh("./run-tests.sh; dev progress add mine")
            self.assertEqual(r["rc"], 0, r["err"])
            self.assertIn("planted-raw", self.plan(s))  # it runs in the tech lead's call: not refused
            self.assertIn("mine", self.plan(s))
            found = self.audit("caller.token-unlisted-use")
            self.assertEqual(len(found), 1, found)
            self.assertEqual(found[0]["ref"]["result"], "flagged")  # recorded, not refused
            self.assertEqual(found[0]["ref"]["by"], "main")
            self.assertEqual(found[0]["evidence"].get("count"), 1)  # the planted change, not the call's own
            s.sh("dev progress add own-only")
            self.assertEqual(len(self.audit("caller.token-unlisted-use")), 1)
        self.both(check)


if __name__ == "__main__":
    unittest.main()
