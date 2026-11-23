# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Every command's -p takes "a project id, a project name, a path, or part of an id" (docs/commands.md). A path is any
# folder of a project: its main checkout, a folder inside it, or one of its worktrees, as the Stop hook's own
# suggestion `dev ci watch --wait -p <worktree path>` names one. The worktree's path stays the folder the command
# works in, so its branch is the one followed. Tests carry comments, not docstrings: `dev test prove` leaves out
# tests with a docstring. Run: python3 -m unittest tests.test_cli_project_path
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import ciwatch, cli, registry  # noqa: E402


class ProjectPathTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(os.path.realpath(tmp.name))
        self.main, self.tree = root / "acme" / "api", root / ".worktrees" / "acme" / "api" / "feat-x"
        for d in (self.main / "lib", self.tree, root / "elsewhere"):
            d.mkdir(parents=True)
        self.elsewhere = root / "elsewhere"
        self.reg = {"projects": [
            {"id": "acme/api", "name": "api", "path": str(self.main), "local": True,
             "worktrees": [{"path": str(self.tree), "branch": "feat/x"}]},
            {"id": "acme/web", "name": "web", "path": str(root / "acme" / "web"), "local": False, "worktrees": []}]}
        for p in (mock.patch.object(registry, "lookup", return_value=None),
                  mock.patch.object(cli, "_reg", return_value=self.reg)):
            p.start()
            self.addCleanup(p.stop)

    def test_a_worktree_path_resolves_its_project_in_the_worktree(self):
        p = cli._resolve(None, str(self.tree))
        self.assertEqual((p["id"], p["path"]), ("acme/api", str(self.tree)))

    # a folder inside a checkout resolves its project; a path of no project still says none matches
    def test_a_folder_inside_a_checkout_resolves_its_project(self):
        for given in (str(self.main / "lib"), str(self.main) + "/"):
            with self.subTest(given=given):
                p = cli._resolve(None, given)
                self.assertEqual((p["id"], p["path"]), ("acme/api", str(self.main)))
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as e:
            cli._resolve(None, str(self.elsewhere))
        self.assertEqual(e.exception.code, 2)
        self.assertIn("no project matches", err.getvalue())

    # the Stop hook's suggestion, as an agent runs it: the wait follows the worktree's checkout
    def test_dev_ci_watch_wait_with_a_worktree_path_waits_in_the_worktree(self):
        with mock.patch.object(ciwatch, "cmd_wait", return_value=0) as wait:
            rc = cli.main(["ci", "watch", "--wait", "-p", str(self.tree)])
        self.assertEqual(rc, 0)
        self.assertEqual(wait.call_args.args[1], str(self.tree))


if __name__ == "__main__":
    unittest.main()
