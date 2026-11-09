# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev task <verb> <n>`: a number means the item at that place in the list `dev task` showed, and is refused once the
open items changed, so closing #9 and then #40 never closes the item that moved up to #40.
Run: python3 -m unittest tests.test_task_numbers -v"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import core, work  # noqa: E402
from test_work import A, B, TempHandoff  # noqa: E402

SCOPE = "acme/api"


class ShownListTests(unittest.TestCase):
    def setUp(self):
        self.handoff = TempHandoff().__enter__()
        self.addCleanup(self.handoff.__exit__, None, None, None)
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        for patch in (mock.patch.object(core, "STATE", Path(state.name)),
                      mock.patch.dict(os.environ, {"DEVKIT_SESSION": "claude:session-one"})):
            patch.start()
            self.addCleanup(patch.stop)
        # In `dev task` order (Now, Next, Later); items made in the same second would tie on their creation time.
        self.first = work.create(SCOPE, "Fix the login timeout", bucket="now", a=A)
        self.second = work.create(SCOPE, "Export invoices as CSV", bucket="next", a=A)
        self.third = work.create(SCOPE, "Codex parity test", bucket="later", a=A)

    def ids(self, items):
        return [i["meta"]["id"] for i in items]

    def test_a_number_finds_the_item_at_that_place_in_the_list_that_was_shown(self):
        shown, numbered = work.show(SCOPE)
        self.assertTrue(numbered)
        self.assertEqual(self.ids(shown), [self.first["meta"]["id"], self.second["meta"]["id"],
                                           self.third["meta"]["id"]])
        self.assertEqual(work.find("2", SCOPE)["meta"]["id"], self.second["meta"]["id"])

    def test_closing_one_item_by_number_makes_the_next_number_refused_with_the_ids_to_use(self):
        work.show(SCOPE)
        work.release(work.find("1", SCOPE), a=A, status="done")
        with self.assertRaises(ValueError) as e:
            work.find("2", SCOPE)  # was "Export invoices as CSV"; "Codex parity test" sits at #2 now
        msg = str(e.exception)
        self.assertIn(f"#2 was {self.second['meta']['id']} “Export invoices as CSV”", msg)
        self.assertIn(f"is {self.third['meta']['id']} “Codex parity test” now", msg)
        self.assertIn("1 closed", msg)
        self.assertEqual(work.load(self.third["path"])["meta"]["status"], "todo")
        self.assertEqual(work.find(self.second["meta"]["id"], SCOPE)["meta"]["id"], self.second["meta"]["id"])

    def test_an_item_added_by_someone_else_after_the_list_was_shown_refuses_the_numbers(self):
        work.show(SCOPE)
        work.create(SCOPE, "Rotate the signing key", bucket="now", priority="high", a=B)
        with self.assertRaises(ValueError) as e:
            work.find("1", SCOPE)
        msg = str(e.exception)
        self.assertIn(f"#1 was {self.first['meta']['id']} “Fix the login timeout”", msg)
        self.assertIn("1 added", msg)

    def test_a_number_with_no_list_shown_here_is_refused(self):
        with self.assertRaises(ValueError) as e:
            work.find("1", SCOPE)
        self.assertIn("run dev task", str(e.exception))

    def test_a_list_shown_in_another_session_does_not_number_this_one(self):
        with mock.patch.dict(os.environ, {"DEVKIT_SESSION": "claude:session-two"}):
            work.show(SCOPE)
        with self.assertRaises(ValueError):
            work.find("1", SCOPE)
        with mock.patch.dict(os.environ, {"DEVKIT_SESSION": "claude:session-two"}):
            self.assertEqual(work.find("1", SCOPE)["meta"]["id"], self.first["meta"]["id"])

    def test_a_damaged_record_of_the_shown_list_counts_as_no_list_shown(self):
        work.show(SCOPE)
        f = next((core.STATE / "task-lists").glob("*.json"))
        f.write_text('{"lists": {"acme/api": {"ids": [["T-1"], {"a": 1}], "shown_at": "2026-11-09T09:00:00Z"}}}')
        with self.assertRaises(ValueError) as e:
            work.find("1", SCOPE)
        self.assertIn("run dev task", str(e.exception))

    def test_a_number_past_the_end_of_the_shown_list_is_refused(self):
        work.show(SCOPE)
        with self.assertRaises(ValueError) as e:
            work.find("4", SCOPE)
        self.assertIn("no #4 in the list", str(e.exception))
        self.assertIn("3 open items", str(e.exception))

    def test_a_list_shown_more_than_thirty_minutes_ago_no_longer_numbers_items(self):
        shown_at = time.time()
        with mock.patch.object(work.time, "time", return_value=shown_at):
            work.show(SCOPE)
        with mock.patch.object(work.time, "time", return_value=shown_at + 29 * 60):
            self.assertEqual(work.find("3", SCOPE)["meta"]["id"], self.third["meta"]["id"])
        with mock.patch.object(work.time, "time", return_value=shown_at + 31 * 60), \
                self.assertRaises(ValueError) as e:
            work.find("3", SCOPE)
        self.assertIn(f"#3 was {self.third['meta']['id']} “Codex parity test”", str(e.exception))
        self.assertIn("more than 30 minutes ago", str(e.exception))

    def test_the_list_printed_after_a_write_is_numbered_only_while_it_is_the_list_shown(self):
        work.show(SCOPE)
        work.claim(work.find("1", SCOPE), a=A)  # already in Now, so the order stays (a claim moves an item to Now)
        _, numbered = work.show(SCOPE, rebind=False)
        self.assertTrue(numbered)
        self.assertEqual(work.find("3", SCOPE)["meta"]["id"], self.third["meta"]["id"])
        work.release(work.find("1", SCOPE), a=A, status="done")
        reprinted, numbered = work.show(SCOPE, rebind=False)
        self.assertFalse(numbered)
        self.assertEqual(self.ids(reprinted), [self.second["meta"]["id"], self.third["meta"]["id"]])
        with self.assertRaises(ValueError):
            work.find("2", SCOPE)  # the reprint did not renumber: #2 still means the list shown first
        work.show(SCOPE)  # `dev task` again numbers the list as it is now
        self.assertEqual(work.find("2", SCOPE)["meta"]["id"], self.third["meta"]["id"])

    def test_a_write_names_the_verb_the_id_and_the_title(self):
        self.assertEqual(work.acted("done", self.second),
                         f"done {self.second['meta']['id']} “Export invoices as CSV”")


if __name__ == "__main__":
    unittest.main()
