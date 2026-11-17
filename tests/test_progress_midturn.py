# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""progress.on_prompt for a message the developer types while the turn runs (midturn): the running turn goes on, with
its start, its reminder count and its open timing interval, and the message becomes the developer's latest request."""
import json
import unittest
from unittest import mock

from tests.test_progress import TempState
from devkit import progress as pg

T0 = 1_800_000_000.0


class MidTurnPromptTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        with mock.patch.object(pg.time, "time", return_value=T0):
            pg.start("Release", plan=["build", "ship"], now=T0)
        self.prompt(T0 + 10)  # the prompt that opens the turn
        rec = self.session()
        self.session(dict(rec, nudges=2))  # the turn was reminded twice so far

    def prompt(self, at, midturn=False, text="Ship it."):
        return pg.on_prompt("claude", {"session_id": "s1", "prompt": text}, now=at, midturn=midturn)

    def session(self, rec=None):
        if rec is not None:
            self.state.sess.write_text(json.dumps(rec))
        return json.loads(self.state.sess.read_text())

    def turns(self):
        return [iv for iv in pg.load_run("claude", "s1")["intervals"] if iv[2] == "turn"]

    def test_a_message_typed_mid_turn_keeps_the_running_turn(self):
        before, turns = self.session(), self.turns()
        self.assertEqual([iv[:2] for iv in turns], [[T0 + 10, None]], "one open turn interval")
        self.prompt(T0 + 70, midturn=True, text="Also update the docs.")
        after = self.session()
        self.assertEqual((after["turn_started"], after["nudges"], after["last_update"]),
                         (before["turn_started"], 2, before["last_update"]))
        self.assertEqual((after["request_ts"], after["last_prompt"]), (T0 + 70, T0 + 70))
        self.assertEqual(self.turns(), turns, "the open turn interval is neither closed nor opened again")

    def test_a_new_prompt_starts_a_new_turn(self):
        self.prompt(T0 + 70)
        after = self.session()
        self.assertEqual((after["turn_started"], after["nudges"], after["request_ts"]), (T0 + 70, 0, None))
        self.assertEqual([iv[:2] for iv in self.turns()], [[T0 + 10, T0 + 70], [T0 + 70, None]])


if __name__ == "__main__":
    unittest.main()
