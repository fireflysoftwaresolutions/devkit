# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Red team (Agent Smith), part 5: a Codex thread mistaken for a nested (headless) one, and settle lines that only look
# like the developer's (an invisible or full-width character in the id, a longer word, text before it). Each asserts
# the safe behavior. The Codex cases run on the suite's fixtures (tests/codexrollout.py).
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_red_team_5 -v (and with /usr/bin/python3, for Python 3.9)
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import peers  # noqa: E402
from devkit import progress as pg  # noqa: E402
from codexrollout import OTHER, SID, CodexCase  # noqa: E402
from test_hooks_peers import PeerHookCase  # noqa: E402
from test_peers import CLAUDE_A, CLAUDE_B, sender  # noqa: E402

EID = "E-abcdefgh"
ZERO_WIDTH, FULL_WIDTH_ZERO = chr(0x200B), chr(0xFF10)


class CodexNestedTests(CodexCase):
    def test_a_codex_thread_with_no_pid_is_neither_headless_nor_nested(self):
        # the hooks write a Codex thread's record without a pid (its threads share one process): it must not read
        # as a session another session started, whatever processes run above the test
        rec = {"agent": "codex", "session": SID, "surface": "codex-tui"}
        self.assertFalse(pg.headless(rec))
        with mock.patch.object(pg, "_ancestor_pids", lambda: [99999, 1]):
            self.assertFalse(pg.nested(rec))

    def test_a_codex_exec_run_is_headless(self):
        self.assertTrue(pg.headless({"agent": "codex", "session": SID, "surface": "codex exec"}))

    def test_a_codex_record_with_a_pid_below_another_session_is_nested(self):
        self.session(OTHER)
        outer = pg.SESSIONS / ("codex-" + OTHER + ".json")
        rec = json.loads(outer.read_text())
        rec["pid"] = 99101
        outer.write_text(json.dumps(rec))
        inner = {"agent": "codex", "session": SID, "surface": "codex-tui", "pid": 99500}
        with mock.patch.object(pg, "_parents_of", lambda pid: ([99101, 1], {})):
            self.assertTrue(pg.nested(inner))


class SettleLineTests(unittest.TestCase):
    def test_the_exact_settle_line_parses(self):
        self.assertEqual(peers.settle_lines([f"settle {EID}: A"]), [(EID, "A", f"settle {EID}: A")])

    def test_an_upper_case_id_settles_the_same_escalation(self):
        # by design: the developer may type the id in either case
        self.assertEqual(peers.settle_lines(["settle E-ABCDEFGH: A"]), [(EID, "A", "settle E-ABCDEFGH: A")])

    def test_lookalike_and_misplaced_settle_lines_parse_to_nothing(self):
        for line in (f"settle E-abcdefg{ZERO_WIDTH}h: A",  # an invisible character inside the id
                     f"settle E-abcdefg{FULL_WIDTH_ZERO}: A",  # a full-width digit in the id
                     f"settled {EID}: A",  # "settle" inside a longer word
                     f"please settle {EID}: A",  # not at the start of the line
                     f"x settle {EID}: A",
                     f"settle {EID}"):  # no choice
            with self.subTest(line=repr(line)):
                self.assertEqual(peers.settle_lines([line]), [])


class SettleLookalikeHookTests(PeerHookCase):
    def test_lookalike_and_misplaced_settle_prompts_settle_nothing_and_the_exact_one_does(self):
        self.session("claude", CLAUDE_A, pid=99101)
        self.session("claude", CLAUDE_B, pid=99303)
        eid = peers.escalate(sender(CLAUDE_A), f"@{CLAUDE_B[:8]}", "Which project releases 26.11.9 first?",
                             ["A: devkit first", "B: kit-consulting first"], recommend="A")["id"]
        for text in (f"settle {eid[:-1]}{ZERO_WIDTH}{eid[-1]}: A", f"settle {eid[:-1]}{FULL_WIDTH_ZERO}: A",
                     f"settled {eid}: A", f"please settle {eid}: A", f"x settle {eid}: A"):
            with self.subTest(text=repr(text)):
                self.prompt("claude", CLAUDE_B, text)
                self.assertEqual([e["id"] for e in peers.escalations("open")], [eid])
        self.prompt("claude", CLAUDE_B, f"settle {eid}: A")
        self.assertEqual(peers.escalations("settled")[0]["settled"]["choice"], "A")


if __name__ == "__main__":
    unittest.main()
