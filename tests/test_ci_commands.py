# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev ci --commands` and the delivery policy's test gate: the commands CI runs on pushes and pull requests.

Run: python3 -m unittest tests.test_ci_commands -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import ci  # noqa: E402

CI = """name: ci
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: make test
"""
# devkit's own bug registry workflow: it runs on issues, comments, releases and by hand, never on a push
REGISTRY = """name: bug registry
on:
  issues:
    types: [opened, edited]
  issue_comment:
  release:
    types: [published]
  workflow_dispatch:
jobs:
  fold:
    runs-on: ubuntu-latest
    steps:
      - name: Fold
        run: python3 tools/bug_registry_action.py fold
"""


class LocalCommandsTests(unittest.TestCase):
    # local_commands listed every workflow's steps, so devkit's own test gate began with the bug registry's Fold step,
    # which no push or pull request ever runs; dev ci --run already skips such a workflow.
    def test_a_workflow_that_never_runs_on_push_or_pull_requests_is_left_out(self):
        with tempfile.TemporaryDirectory() as d:
            wf = Path(d) / ".github" / "workflows"
            wf.mkdir(parents=True)
            (wf / "bug-registry.yml").write_text(REGISTRY)
            (wf / "ci.yml").write_text(CI)
            (wf / "merge-queue.yml").write_text(CI.replace("[push, pull_request]", "[merge_group]").replace(
                "make test", "make queue-test"))
            self.assertEqual(ci.local_commands(Path(d)), ["make test", "make queue-test"])


if __name__ == "__main__":
    unittest.main()
