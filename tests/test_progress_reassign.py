# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# A step queued for a member's busy lane (`waits_lane`) was announced to that member ("it joins your lane once …
# closes"). When `dev progress assign` gives it to someone else, the reassignment clears the queue, and the member
# whose lane it was queued for gets a devkit notice that names the step and its new owner, through the run's messages
# that its next hook and `dev watch --wait` deliver. A step queued for nobody sends no lane anything. Progress state
# and the audit trail in a temporary folder (test_progress.TempState).
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_progress_reassign -v
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()  # run alone, this module gets the suite's temporary home too
sys.path.insert(0, str(ROOT / "lib"))

from devkit import progress as pg  # noqa: E402
from test_progress import TempState, launch  # noqa: E402


def lane_messages(aid: str) -> list:
    return [m for m in pg.load_run("claude", "s1").get("messages") or [] if m.get("to") == aid]


class ReassignQueuedStepTests(unittest.TestCase):
    def test_the_lane_a_step_was_queued_for_is_told_its_new_owner_and_no_other_lane_is(self):
        with TempState():
            pg.start("Importer", plan=["parser", "csv", "schema", "docs"])
            pg.assign("parser", "Neo", why="test")
            pg.assign("schema", "Dozer", why="test")
            launch("a_neo", "devkit:neo", "Neo: parser")
            launch("a_dozer", "devkit:dozer", "Dozer: schema")
            pg.assign("csv", "Neo: parser", why="test")  # that lane works on parser: csv is queued for it
            pg.assign("docs", "Mouse", why="test")  # no Mouse lane works: docs waits for a launch, queued for nobody
            told = {aid: len(lane_messages(aid)) for aid in ("a_neo", "a_dozer")}
            queued = pg._queued_lanes(next(s for s in pg.load_run("claude", "s1")["plan"] if s["name"] == "csv"))
            pg.assign("csv", "Tank", why="test")
            pg.assign("docs", "Tank", why="test")
            neo = lane_messages("a_neo")[told["a_neo"]:]
            dozer = lane_messages("a_dozer")[told["a_dozer"]:]
            delivered = pg.on_tool_done("claude", {"session_id": "s1", "agent_id": "a_neo"}) or ""
            csv = next(s for s in pg.load_run("claude", "s1")["plan"] if s["name"] == "csv")
        self.assertEqual(queued, ["a_neo"])  # the queue lives in the step's intent, read by progress._queued_lanes
        self.assertEqual(len(neo), 1, neo)
        self.assertEqual(neo[0].get("kind"), "notice")
        self.assertIn("the tech lead gave “csv” to Backend engineer (Tank)", neo[0]["text"])
        self.assertIn("no longer joins your lane", neo[0]["text"])
        self.assertIn(neo[0]["id"], delivered)  # its next hook shows it
        self.assertEqual(dozer, [])
        self.assertNotIn("waits_lane", csv)


if __name__ == "__main__":
    unittest.main()
