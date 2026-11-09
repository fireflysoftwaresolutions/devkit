# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev task <verb> <n>`: a number means the item at that place in the list `dev task` showed, and is refused once the
open items changed, so closing #9 and then #40 never closes the item that moved up to #40.
Run: python3 -m unittest tests.test_task_numbers -v"""
import contextlib
import json
import os
import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import core, progress, work  # noqa: E402
from test_work import A, B, TempHandoff  # noqa: E402

SCOPE = "acme/api"
SESSION = "session-one"
TANK, GHOST = "a-tank", "a-ghost"  # two crew lanes' agent ids in SESSION


class ShownListTests(unittest.TestCase):
    def setUp(self):
        self.handoff = TempHandoff().__enter__()
        self.addCleanup(self.handoff.__exit__, None, None, None)
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        root = Path(state.name)
        for patch in (mock.patch.object(core, "STATE", root),
                      mock.patch.object(progress, "RUNS", root / "runs"),
                      mock.patch.object(progress, "SESSIONS", root / "sessions"),
                      mock.patch.object(progress, "CALLS", root / "calls"),
                      mock.patch.dict(os.environ, {"DEVKIT_SESSION": f"claude:{SESSION}"})):
            patch.start()
            self.addCleanup(patch.stop)
        for name in ("CODEX_THREAD_ID", "DEVKIT_FROM_AGENT"):  # the patch of os.environ puts them back
            os.environ.pop(name, None)
        # In `dev task` order (Now, Next, Later); items made in the same second would tie on their creation time.
        self.first = work.create(SCOPE, "Fix the login timeout", bucket="now", a=A)
        self.second = work.create(SCOPE, "Export invoices as CSV", bucket="next", a=A)
        self.third = work.create(SCOPE, "Codex parity test", bucket="later", a=A)

    def ids(self, items):
        return [i["meta"]["id"] for i in items]

    @contextlib.contextmanager
    def running(self, who, words, *others):
        """`dev <words>` as a shell call of `who` (a crew lane's agent id, or main) in SESSION: the call ledger holds
        it, and the `others` [(caller, command)] open beside it, as the hooks record them. `dev task` is not a routed
        lane call, so DEVKIT_FROM_AGENT stays unset."""
        folder = progress.CALLS / SESSION
        folder.mkdir(parents=True, exist_ok=True)
        entries = []
        for n, (caller, cmd) in enumerate([(who, f"dev {words}"), *others]):
            entries.append(folder / f"toolu_{n}.json")
            entries[-1].write_text(json.dumps({"tool_use_id": f"toolu_{n}", "caller": caller, "ts": time.time(),
                                               "ttl": 3600, "cmd": cmd}))
        try:
            with mock.patch.object(sys, "argv", ["dev", *shlex.split(words)]):
                yield
        finally:
            for e in entries:
                e.unlink()

    def assert_lanes_keep_their_own_lists(self, lane_a, lane_b):
        with self.running(lane_a, "task"):
            work.show(SCOPE)  # lane A: the login timeout, the CSV export, the parity test
        work.release(work.load(self.first["path"]), a=B, status="done")
        with self.running(lane_b, "task"):
            work.show(SCOPE)  # lane B, after the close: the CSV export, the parity test
        with self.running(lane_a, "task done 2"), self.assertRaises(ValueError) as e:
            work.find("2", SCOPE)  # A's #2 is the CSV export; in B's list #2 is the parity test
        self.assertIn(f"#2 was {self.second['meta']['id']} “Export invoices as CSV”", str(e.exception))
        self.assertEqual(work.load(self.third["path"])["meta"]["status"], "todo")

    def test_a_crew_lanes_number_points_into_its_own_list_not_the_one_another_lane_showed_since(self):
        self.assert_lanes_keep_their_own_lists(TANK, GHOST)

    def test_lanes_whose_ids_differ_only_past_the_length_of_a_file_name_keep_their_own_lists(self):
        self.assert_lanes_keep_their_own_lists("a-" + "x" * 90 + "-tank", "a-" + "x" * 90 + "-ghost")

    def test_while_two_open_calls_name_the_same_words_the_list_is_not_numbered_for_either(self):
        with self.running(GHOST, "task"):
            work.show(SCOPE)
        work.release(work.load(self.first["path"]), a=B, status="done")
        with self.running(TANK, "task", (GHOST, "dev task")):  # both lanes run `dev task` at once
            _, numbered = work.show(SCOPE)
        self.assertFalse(numbered)  # the ledger cannot say which call is this one
        with self.running(GHOST, "task done 2"), self.assertRaises(ValueError):
            work.find("2", SCOPE)  # still the list the Ghost lane saw before the close

    def test_a_codex_thread_started_from_a_claude_code_shell_numbers_its_own_list(self):
        work.show(SCOPE)  # the Claude Code session, whose DEVKIT_SESSION the Codex thread inherits
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-1"}):
            with self.assertRaises(ValueError) as e:
                work.find("1", SCOPE)
            self.assertIn("no list of acme/api was shown here", str(e.exception))
            work.show(SCOPE)
            self.assertEqual(work.find("1", SCOPE)["meta"]["id"], self.first["meta"]["id"])

    def test_shells_with_no_session_of_their_own_keep_their_own_lists(self):
        # An agent session was active in this folder a minute ago; the two shells below are not part of it.
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(folder.name)
        progress.SESSIONS.mkdir(parents=True)
        (progress.SESSIONS / "claude-session-recent.json").write_text(json.dumps(
            {"agent": "claude", "session": "session-recent", "top": os.getcwd()}))
        os.environ.pop("DEVKIT_SESSION")
        with mock.patch.object(os, "getppid", return_value=4101):
            work.show(SCOPE)
        with mock.patch.object(os, "getppid", return_value=4102), self.assertRaises(ValueError) as e:
            work.find("1", SCOPE)
        self.assertIn("no list of acme/api was shown here", str(e.exception))
        with mock.patch.object(os, "getppid", return_value=4101):
            self.assertEqual(work.find("1", SCOPE)["meta"]["id"], self.first["meta"]["id"])

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
