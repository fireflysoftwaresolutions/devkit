# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# `dev task`: what the list says when devkit cannot number it, what a new item holds, and how `dev task show` reads
# one.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_cli_tasks
import contextlib
import io
import re
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


class ItemsCase(unittest.TestCase):
    # Three items of acme/api in a temporary handoff; dev() runs `dev task …` and returns (exit code, out, err).
    UNFILLED = ("## Symptom\n\nusers are logged out\n\n## Steps to reproduce\n\n1. _…_\n\n## Done when\n\n"
                "- [ ] _A failing test reproduces it and now passes._\n")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for patch in (mock.patch.object(core, "HANDOFF", root / "handoff"),
                      mock.patch.object(core, "STATE", root / "state"),
                      mock.patch.object(core, "CONFIG_FILE", root / "config.json"),
                      mock.patch.object(work, "HANDOFF", root / "handoff"),
                      mock.patch.object(work, "ITEMS", root / "handoff" / "items"),
                      mock.patch.object(work, "_commit", lambda *a, **k: None),
                      mock.patch.object(cli, "_scope", lambda a: ("acme/api", "acme/api"))):
            patch.start()
            self.addCleanup(patch.stop)
        self.timeout = work.create("acme/api", "Fix the login timeout", kind="bug", body=self.UNFILLED)
        self.banner = work.create("acme/api", "Fix the login banner", kind="task")
        self.export = work.create("acme/api", "Export invoices as CSV", kind="feature")

    def dev(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.cmd_task(cli.build_parser().parse_args(["task", *argv]))
        return rc or 0, out.getvalue(), err.getvalue()


class AddTests(ItemsCase):
    # A new item holds only the sections it has text for; the hint after `dev task add` names what is missing, the
    # testable criterion, and says nothing once --done gave one.
    def test_add_without_done_asks_for_the_criterion_and_with_done_says_nothing_more(self):
        rc, out, err = self.dev("add", "Rotate the signing key", "--type", "bug", "--why", "the key leaked in a log")
        self.assertEqual(rc, 0, err)
        self.assertIn("no Done when yet", out)
        added = work.find(re.search(r"\b(B-[0-9a-z]+)\b", out).group(1), "acme/api")
        self.assertEqual(added["body"], "## Symptom\n\nthe key leaked in a log")
        rc, out, err = self.dev("add", "Rotate the backup key", "--type", "bug", "--why", "same log",
                                "--done", "the old key is revoked")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("Done when yet", out)
        self.assertNotIn("fill in the description", out)


class TitleLookupTests(ItemsCase):
    # `dev task show` reads one item: by its id, or by a part of its title, a closed item's too, and it lists the
    # items a part names when it names several. A write finds only an open item by a part of its title.
    def test_show_finds_the_one_item_a_part_of_its_title_names_a_closed_one_too(self):
        work.release(work.load(self.export["path"]), why="shipped", status="done")
        rc, out, err = self.dev("show", "invoices", "as")
        self.assertEqual(rc, 0, err)
        self.assertIn(f"{self.export['meta']['id']} · Export invoices as CSV", out)
        self.assertIn("status       done", out)
        rc, _, err = self.dev("reopen", "invoices", "--why", "not shipped")  # a write finds open items only
        self.assertEqual((rc, "no item matches “invoices”" in err), (2, True), err)
        self.assertEqual(work.load(self.export["path"])["meta"]["status"], "done")

    def test_show_lists_every_item_a_part_of_a_title_names(self):
        rc, out, err = self.dev("show", "login")
        self.assertEqual(rc, 2, out)
        self.assertIn("“login” names 2 items", err)
        self.assertIn(f"{self.timeout['meta']['id']}  Fix the login timeout", err)
        self.assertIn(f"{self.banner['meta']['id']}  Fix the login banner", err)
        self.assertIn(f"dev task show {self.timeout['meta']['id']}", err)
        self.assertNotIn("Export invoices", err)


class ShowBodyTests(ItemsCase):
    # Items written before templates lost their unfilled sections keep them on disk; `dev task show` leaves them out.
    def test_show_leaves_out_unfilled_template_sections_and_keeps_them_on_disk(self):
        before = self.timeout["path"].read_bytes()
        rc, out, err = self.dev("show", self.timeout["meta"]["id"])
        self.assertEqual(rc, 0, err)
        self.assertIn("users are logged out", out)
        self.assertNotIn("Steps to reproduce", out)
        self.assertNotIn("_A failing test", out)
        self.assertEqual(self.timeout["path"].read_bytes(), before)


class ListAllTests(unittest.TestCase):
    # `dev task ls --all` printed the top-level usage ("unrecognized arguments: --all"): only `dev task --all` knew it
    def test_ls_takes_all_as_dev_task_does(self):
        for argv in (["task", "ls", "--all"], ["task", "--all", "ls"], ["task", "--all"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                self.assertTrue(cli.build_parser().parse_args(argv).all)
        self.assertFalse(cli.build_parser().parse_args(["task", "ls"]).all)


if __name__ == "__main__":
    unittest.main()
