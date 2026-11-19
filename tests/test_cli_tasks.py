# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# `dev task`: what the list says when devkit cannot number it.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_cli_tasks
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TREE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TREE / "lib"))

from devkit import cli, core, work  # noqa: E402

SCRIPT = ("no open call of this session holds this command as typed (a loop over a variable, a script, a call open "
          "past its time limit)")


class UnnumberedListTests(unittest.TestCase):
    # From a script or a loop the ledger cannot tell whose command `dev task` is, so the list has no numbers; the
    # hint said "dev task numbers it again", which repeats the same unnumbered list from the same script
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for patch in (mock.patch.object(core, "HANDOFF", root / "handoff"),
                      mock.patch.object(core, "STATE", root / "state"),
                      mock.patch.object(core, "CONFIG_FILE", root / "config.json"),
                      mock.patch.object(work, "HANDOFF", root / "handoff"),
                      mock.patch.object(work, "ITEMS", root / "handoff" / "items"),
                      mock.patch.object(work, "_commit", lambda *a, **k: None)):
            patch.start()
            self.addCleanup(patch.stop)
        work.create("acme/api", "Fix the login timeout", kind="bug", why="users are logged out")

    def listing(self, viewer):
        out = io.StringIO()
        with mock.patch.object(work, "_viewer", return_value=viewer), contextlib.redirect_stdout(out):
            cli._print_items("acme/api", "acme/api")
        return out.getvalue()

    def test_a_list_nobody_can_be_told_apart_for_says_why_and_points_to_the_ids(self):
        out = self.listing(("", SCRIPT))
        line = next(ln for ln in out.splitlines() if "no numbers" in ln)
        self.assertIn(SCRIPT, line)
        self.assertNotIn("dev task numbers it again", line)
        self.assertIn("ids", line)

    def test_a_list_of_a_known_viewer_is_numbered(self):
        out = self.listing(("claude:s1", ""))
        self.assertNotIn("no numbers", out)
        self.assertRegex(out, r"\b1 B-")


class ListAllTests(unittest.TestCase):
    # `dev task ls --all` printed the top-level usage ("unrecognized arguments: --all"): only `dev task --all` knew it
    def test_ls_takes_all_as_dev_task_does(self):
        for argv in (["task", "ls", "--all"], ["task", "--all", "ls"], ["task", "--all"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                self.assertTrue(cli.build_parser().parse_args(argv).all)
        self.assertFalse(cli.build_parser().parse_args(["task", "ls"]).all)


if __name__ == "__main__":
    unittest.main()
