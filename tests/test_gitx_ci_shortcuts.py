# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Two delivery waits devkit no longer imposes, and the cases where it still must.

- A change of CI host settings only (timeout-minutes, runs-on, concurrency, comments) keeps the local CI evidence of
  the tree before it, once actionlint and the devkit workflow audit pass: `dev push`, `dev ci --run --resume`.
- `dev release` does not wait for main's CI to repeat a merge whose tree a passed pull request run already tested.

Offline: the gitx Sandbox (real repositories, `gh` faked); the workflow checks are faked except in the test that runs
the real actionlint. Run: python3 -m unittest tests.test_gitx_ci_shortcuts -v
"""
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import gitx  # noqa: E402
import test_gitx as G  # noqa: E402  (its Sandbox and dev() drive the commands)

WF = ".github/workflows/ci.yml"
WORKFLOW = """# CI for the widget
name: ci
on:
  pull_request:
  push:
    branches: [main]
permissions:
  contents: read
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true
jobs:
  test:
    runs-on: ubuntu-24.04
    timeout-minutes: 30 # the suite takes 10 minutes
    steps:
      - uses: actions/checkout@v4
      - name: Lint
        run: test -f calc.py
      - name: Unit
        timeout-minutes: 20
        run: python -m unittest discover -s tests
"""
CALC = {".gitignore": "__pycache__/\n", "calc.py": "def add(a, b):\n    return a + b\n",
        "tests/test_calc.py": "import sys\nimport unittest\nfrom pathlib import Path\n\n"
                              "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
                              "import calc  # noqa: E402\n\n\n"
                              "class CalcTests(unittest.TestCase):\n"
                              "    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n"}
# the change of the 26.11.9 hotfix: a longer job timeout, and its comment
LONGER_TIMEOUT = ("timeout-minutes: 30 # the suite takes 10 minutes", "timeout-minutes: 60 # the suite takes 35 minutes")


class FakeChecks:
    """Stands in for actionlint and the devkit workflow audit: passes, or fails with `why`."""

    def __init__(self, why=""):
        self.why, self.calls = why, []

    def __call__(self, top, files):
        self.calls.append(list(files))
        if self.why:
            return False, self.why
        return True, "actionlint: no findings · dev ci audit: no high or critical finding"


class Checks:
    """Replace gitx.WORKFLOW_CHECKS for one test."""

    def __init__(self, fake):
        self.fake = fake

    def __enter__(self):
        self.saved = getattr(gitx, "WORKFLOW_CHECKS", None)
        gitx.WORKFLOW_CHECKS = self.fake
        return self.fake

    def __exit__(self, *exc):
        gitx.WORKFLOW_CHECKS = self.saved


def tree_of(s, files):
    """The tree of the working tree with `files` written over it (nothing committed), then back to HEAD."""
    for rel, text in files.items():
        s.write(rel, text)
    G.git(s.repo, "add", "-A")
    tree = G.git(s.repo, "write-tree")
    G.git(s.repo, "reset", "-q", "--hard", "HEAD")
    G.git(s.repo, "clean", "-qfd")
    return tree


def evidenced_branch(s):
    """A branch whose tree passed `dev ci --run` (the marker it records), as cb301e0 was before the timeout change."""
    s.branch("fix/widget")
    s.commit("fix: the widget adds", dict(CALC, **{WF: WORKFLOW}))
    gitx.record_evidence(s.ctx(), "run", gitx._head(s.ctx().top)[1])
    return s.head()


# --------------------------------------------------------------------------- the rule


class HostSettingsRuleTests(unittest.TestCase):
    def test_host_keys_and_comments_alone_are_host_settings(self):
        cases = {
            "the job timeout and its comment": ([LONGER_TIMEOUT], ["jobs.test.timeout-minutes"]),
            "a step timeout": ([("        timeout-minutes: 20", "        timeout-minutes: 25")],
                               ["jobs.test.steps[2].timeout-minutes"]),
            "the runner": ([("runs-on: ubuntu-24.04", "runs-on: macos-latest")], ["jobs.test.runs-on"]),
            "cancel-in-progress": ([("cancel-in-progress: true", "cancel-in-progress: false")], ["concurrency"]),
            "a job concurrency group": ([("    runs-on: ubuntu-24.04\n",
                                          "    runs-on: ubuntu-24.04\n    concurrency: tests\n")],
                                        ["jobs.test.concurrency"]),
            "a comment": ([("# CI for the widget", "# CI for the widget: it confirms a local run")], ["comments"]),
        }
        with G.Sandbox() as s:
            evidenced_branch(s)
            base = gitx._head(s.ctx().top)[1]
            for name, (edits, keys) in cases.items():
                with self.subTest(name):
                    text = WORKFLOW
                    for old, new in edits:
                        self.assertIn(old, text)
                        text = text.replace(old, new)
                    changes, why = gitx.host_only_change(str(s.repo), base, tree_of(s, {WF: text}))
                    self.assertEqual(why, "")
                    self.assertEqual(changes, [f"{WF}: {', '.join(keys)}"])

    def test_anything_a_local_run_exercises_still_needs_one(self):
        cases = {
            "a run command": ({WF: WORKFLOW.replace("run: test -f calc.py", "run: test -f calc.py && test -d tests")},
                              "jobs.test.steps[1].run"),
            "a new step": ({WF: WORKFLOW + "      - name: Build\n        run: python -m compileall -q .\n"},
                           "jobs.test.steps"),
            "a trigger": ({WF: WORKFLOW.replace("  pull_request:\n", "  pull_request:\n    types: [opened]\n")},
                          "on.pull_request"),
            "the permissions": ({WF: WORKFLOW.replace("contents: read", "contents: write")}, "permissions.contents"),
            "a job condition": ({WF: WORKFLOW.replace("    runs-on: ubuntu-24.04\n",
                                                      "    runs-on: ubuntu-24.04\n    if: github.event_name == 'push'\n")},
                                "jobs.test.if"),
            "another file too": ({WF: WORKFLOW.replace(*LONGER_TIMEOUT), "calc.py": "def add(a, b):\n    return b + a\n"},
                                 "calc.py"),
            "a new workflow": ({".github/workflows/lint.yml": WORKFLOW}, ".github/workflows/lint.yml"),
            "a composite action": ({".github/actions/setup/action.yml": "runs:\n  using: composite\n  steps: []\n"},
                                   ".github/actions/setup/action.yml"),
            "unreadable YAML": ({WF: WORKFLOW.replace("jobs:\n", "jobs: [\n")}, "cannot read"),
        }
        with G.Sandbox() as s:
            evidenced_branch(s)
            base = gitx._head(s.ctx().top)[1]
            for name, (files, named) in cases.items():
                with self.subTest(name):
                    changes, why = gitx.host_only_change(str(s.repo), base, tree_of(s, files))
                    self.assertIsNone(changes)
                    self.assertIn(named, why)


# --------------------------------------------------------------------------- dev push


class PushTests(unittest.TestCase):
    def test_push_keeps_the_evidence_when_only_a_ci_host_setting_changed(self):
        with G.Sandbox() as s, Checks(FakeChecks()) as checks:
            run_sha = evidenced_branch(s)
            s.commit("ci: give the test job 60 minutes", {WF: WORKFLOW.replace(*LONGER_TIMEOUT)})
            rc, out, err = G.dev("push")
            self.assertEqual(rc, 0, out + err)
            self.assertEqual(s.head("fix/widget", repo=s.origin), s.head())
            self.assertEqual(checks.calls, [[WF]])
            self.assertIn(f"only CI host settings changed since the local run on {run_sha[:7]}", err)
            self.assertIn(f"{WF}: jobs.test.timeout-minutes", err)
            self.assertIn("actionlint: no findings", err)
            ev = gitx.evidence(s.ctx())
            self.assertEqual((ev["how"], ev["base"]["sha"], ev["base"]["how"]), ("host-only", run_sha, "run"))
            self.assertIn(f"on {run_sha[:7]}", gitx.describe_evidence(ev))
            line = gitx._verify_line(ev, gitx.STRINGS["en"])
            self.assertIn("`dev ci --run` passed on " + run_sha[:7], line)
            self.assertIn("jobs.test.timeout-minutes", line)

    def test_push_still_needs_a_local_run_when_a_step_changed(self):
        with G.Sandbox() as s, Checks(FakeChecks()) as checks:
            evidenced_branch(s)
            s.commit("ci: lint the tests too", {WF: WORKFLOW.replace("run: test -f calc.py",
                                                                     "run: test -f calc.py && test -d tests")})
            rc, out, err = G.dev("push")
            self.assertEqual(rc, 2, out + err)
            self.assertIn("no local CI evidence", err)
            self.assertIn("jobs.test.steps[1].run", err)
            self.assertEqual(checks.calls, [])
            self.assertEqual(G.git(s.origin, "branch", "--list", "fix/widget"), "")

    def test_push_still_needs_a_local_run_when_the_workflow_checks_fail(self):
        why = "actionlint: .github/workflows/ci.yml:15:22: expected scalar node for \"timeout-minutes\""
        with G.Sandbox() as s, Checks(FakeChecks(why)):
            evidenced_branch(s)
            s.commit("ci: give the test job 60 minutes", {WF: WORKFLOW.replace(*LONGER_TIMEOUT)})
            rc, out, err = G.dev("push")
            self.assertEqual(rc, 2, out + err)
            self.assertIn("no local CI evidence", err)
            self.assertIn(why, err)
            self.assertIsNone(gitx.evidence(s.ctx()))

    def test_an_uncommitted_workflow_edit_keeps_the_shortcut_off(self):
        # the checks read the files on disk: they must be the commit's, or they would vouch for other content
        with G.Sandbox() as s, Checks(FakeChecks()) as checks:
            evidenced_branch(s)
            s.commit("ci: give the test job 60 minutes", {WF: WORKFLOW.replace(*LONGER_TIMEOUT)})
            s.write(WF, WORKFLOW.replace(*LONGER_TIMEOUT).replace("run: test -f calc.py", "run: exit 0"))
            rc, out, err = G.dev("push")
            self.assertEqual(rc, 2, out + err)
            self.assertIn("uncommitted", err)
            self.assertEqual(checks.calls, [])


@unittest.skipUnless(shutil.which("actionlint"), "needs actionlint on PATH (brew install actionlint)")
class RealChecksTests(unittest.TestCase):
    def test_actionlint_and_the_audit_judge_the_workflow_on_disk(self):
        with G.Sandbox() as s:
            s.write(WF, WORKFLOW.replace(*LONGER_TIMEOUT))
            passed, said = gitx.WORKFLOW_CHECKS(str(s.repo), [WF])
            self.assertTrue(passed, said)
            self.assertIn("actionlint: no findings", said)
            self.assertIn("dev ci audit: no high or critical finding", said)
            s.write(WF, WORKFLOW.replace("timeout-minutes: 30 # the suite takes 10 minutes", "timeout-minutes: soon"))
            passed, said = gitx.WORKFLOW_CHECKS(str(s.repo), [WF])
            self.assertFalse(passed)
            self.assertIn(f"actionlint: {WF}:15:", said)  # line 15 holds the job's timeout-minutes


# --------------------------------------------------------------------------- dev ci --run --resume


class ResumeTests(unittest.TestCase):
    def test_resume_keeps_the_passed_steps_after_a_host_settings_change(self):
        with G.Sandbox() as s, Checks(FakeChecks()):
            s.branch("fix/widget")
            first = s.commit("fix: the widget adds", dict(CALC, **{WF: WORKFLOW}))
            rc, out, err = G.dev("ci", "--run")
            self.assertEqual(rc, 0, out + err)
            s.commit("ci: give the test job 60 minutes", {WF: WORKFLOW.replace(*LONGER_TIMEOUT)})
            rc, out, err = G.dev("ci", "--run", "--resume")
            self.assertEqual(rc, 0, out + err)
            self.assertIn("only CI host settings changed since", out)
            steps = {x["step"]: x for x in gitx.evidence(s.ctx())["steps"]}
            self.assertEqual({k: (v["how"], v["sha"]) for k, v in steps.items()},
                             {"Lint": ("kept", first), "Unit": ("kept", first)})

    def test_resume_reruns_a_step_that_reads_every_file(self):
        # a scanner reads the workflow too: a comment can leak a key as well as a README can
        scan = WORKFLOW.replace("      - name: Unit", "      - name: Secrets\n        run: echo gitleaks dir .\n"
                                                   "      - name: Unit")
        with G.Sandbox() as s, Checks(FakeChecks()):
            s.branch("fix/widget")
            s.commit("fix: the widget adds", dict(CALC, **{WF: scan}))
            self.assertEqual(G.dev("ci", "--run")[0], 0)
            s.commit("ci: say what the workflow is for", {WF: scan.replace("# CI for the widget",
                                                                            "# CI for the widget: it confirms a run")})
            rc, out, err = G.dev("ci", "--run", "--resume")
            self.assertEqual(rc, 0, out + err)
            steps = {x["step"]: x["how"] for x in gitx.evidence(s.ctx())["steps"]}
            self.assertEqual((steps["Lint"], steps["Secrets"], steps["Unit"]), ("kept", "full", "kept"))

    def test_resume_reruns_the_steps_when_the_workflow_checks_fail(self):
        with G.Sandbox() as s, Checks(FakeChecks("actionlint: ci.yml:15: bad timeout")):
            s.branch("fix/widget")
            s.commit("fix: the widget adds", dict(CALC, **{WF: WORKFLOW}))
            self.assertEqual(G.dev("ci", "--run")[0], 0)
            s.commit("ci: give the test job 60 minutes", {WF: WORKFLOW.replace(*LONGER_TIMEOUT)})
            rc, out, err = G.dev("ci", "--run", "--resume")
            self.assertEqual(rc, 0, out + err)
            self.assertIn("actionlint: ci.yml:15: bad timeout", out)
            steps = {x["step"]: x["how"] for x in gitx.evidence(s.ctx())["steps"]}
            self.assertNotIn("kept", steps.values())


# --------------------------------------------------------------------------- dev release


def pr_ci(sha, conclusion="success", event="pull_request", workflow="ci"):
    return {"status": "completed", "conclusion": conclusion, "workflowName": workflow, "event": event, "headSha": sha}


def main_ci(sha, status="in_progress", conclusion="", workflow="ci"):
    return {"status": status, "conclusion": conclusion, "workflowName": workflow, "event": "push", "headSha": sha}


class ReleaseTests(unittest.TestCase):
    def merged(self, s, squash=False, main_moved=False):
        """main with fix/x merged by a PR, as on origin -> (HEAD, the PR's head)."""
        s.branch("fix/x")
        pr_head = s.commit("fix: x adds")
        G.git(s.repo, "switch", "-q", "main")
        if main_moved:
            s.commit("docs: a change on main the PR never tested")
        if squash:
            G.git(s.repo, "merge", "-q", "--squash", "fix/x")
            G.git(s.repo, "commit", "-q", "-m", "fix: x adds (#12)")
        else:
            G.git(s.repo, "merge", "-q", "--no-ff", "-m", "Merge pull request #12 from acme/fix/x", "fix/x")
        G.git(s.repo, "push", "-q", "origin", "main")
        G.dev("ci", "--mark-passed")
        s.gh.on(["release", "create"], f"https://github.com/acme/widget/releases/tag/v{G.YY}.{G.MM}.0\n")
        return s.head(), pr_head

    def runs(self, s, by_sha):
        for sha, runs in by_sha.items():
            s.gh.on(["run", "list"], runs, when=lambda a, sha=sha: sha in a)

    def test_release_does_not_wait_for_main_to_repeat_a_passed_pr_run_on_the_same_tree(self):
        with G.Sandbox() as s:
            head, pr_head = self.merged(s)
            self.runs(s, {head: [main_ci(head)], pr_head: [pr_ci(pr_head)]})
            rc, out, err = G.dev("release")
            self.assertEqual(rc, 0, out + err)
            self.assertEqual(s.head(f"v{G.YY}.{G.MM}.0^{{commit}}", repo=s.origin), head)
            self.assertIn(f"CI still runs on {head[:7]} (ci), but its tree passed the pull request run on "
                          f"{pr_head[:7]}: releasing without waiting", out)

    def test_a_squash_merge_finds_the_pr_head_through_github(self):
        with G.Sandbox() as s:
            head, pr_head = self.merged(s, squash=True)
            s.gh.on(["api", f"repos/acme/widget/commits/{head}/pulls"], [{"number": 12, "head": {"sha": pr_head}}])
            self.runs(s, {head: [main_ci(head)], pr_head: [pr_ci(pr_head)]})
            rc, out, err = G.dev("release", "--dry-run")
            self.assertEqual(rc, 0, out + err)
            self.assertIn(f"tree passed the pull request run on {pr_head[:7]} (PR #12)", out)

    def test_release_still_waits_when_no_passed_pr_run_tested_this_tree(self):
        cases = {
            "main moved before the merge": (dict(main_moved=True), [pr_ci("x")]),
            "the PR run is still going": ({}, [dict(pr_ci("x"), status="in_progress", conclusion="")]),
            "the PR run only skipped its jobs (a draft)": ({}, [pr_ci("x", conclusion="skipped")]),
            "only a branch push ran": ({}, [pr_ci("x", event="push")]),
            "main runs a workflow the PR did not": ({}, [pr_ci("x", workflow="lint")]),
        }
        for name, (how, twin_runs) in cases.items():
            with self.subTest(name), G.Sandbox() as s:
                head, pr_head = self.merged(s, **how)
                self.runs(s, {head: [main_ci(head)], pr_head: twin_runs})
                rc, out, err = G.dev("release")
                self.assertEqual(rc, 2, out + err)
                self.assertIn(f"CI is still running on {head[:7]}", err)
                self.assertFalse(s.gh.called("release", "create"))

    def test_a_failed_run_on_head_still_refuses_even_when_the_pr_run_passed(self):
        with G.Sandbox() as s:
            head, pr_head = self.merged(s)
            self.runs(s, {head: [main_ci(head), main_ci(head, "completed", "failure", workflow="lint")],
                          pr_head: [pr_ci(pr_head), pr_ci(pr_head, workflow="lint")]})
            rc, out, err = G.dev("release")
            self.assertEqual(rc, 2, out + err)
            self.assertIn(f"CI failed on {head[:7]}: lint", err)
            self.assertFalse(s.gh.called("release", "create"))


if __name__ == "__main__":
    unittest.main()
