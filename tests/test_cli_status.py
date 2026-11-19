# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# `dev status`: the issues of the map that are this project's.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_cli_status
import sys
import unittest
from pathlib import Path

TREE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TREE / "lib"))

from devkit import cli, core  # noqa: E402


class ProjectIssueTests(unittest.TestCase):
    # an issue was this project's when its id or folder appeared anywhere in the text, so acme/api's status also
    # listed acme/api-gateway's issues: a project's issue names it whole
    def setUp(self):
        home = str(core.HOME)
        self.api = {"id": "acme/api", "path": f"{home}/Development/acme/api"}
        self.reg = {"issues": [
            ("handoff", "acme/api-gateway: the handoff note has a conflict this Mac set aside"),
            ("worktree", f"acme/api-gateway: worktree registered but missing on disk "
                         f"(~/Development/acme/api-gateway/old) → `dev gc --apply` prunes it"),
            ("handoff", "acme/api: the handoff note has a conflict this Mac set aside"),
            ("worktree", "a worktree of ~/Development/acme/api/feat-x is registered but missing on disk"),
            ("error", "the projects map could not read ~/Development/acme/api.v2/.git"),
        ]}

    def test_a_projects_status_lists_only_the_issues_that_name_it_whole(self):
        texts = [n["text"] for n in cli._issue_needs(self.reg, self.api)]
        self.assertEqual(len(texts), 2, texts)
        self.assertFalse([t for t in texts if "gateway" in t], texts)

    def test_the_longer_named_project_still_gets_its_own_issues(self):
        gateway = {"id": "acme/api-gateway", "path": f"{core.HOME}/Development/acme/api-gateway"}
        self.assertEqual(len(cli._issue_needs(self.reg, gateway)), 2)


if __name__ == "__main__":
    unittest.main()
