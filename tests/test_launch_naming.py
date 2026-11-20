# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# A crew launch checked before it starts (hooks._launch_naming) by the rule that binds it once it runs
# (progress._bind_step): a description that names a finished step is a follow-up only when it names none of the
# steps waiting for its member; one that names two of them equally gets the tech lead the line that links the one it
# does. Progress state in a temporary folder (test_progress.TempState).
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_launch_naming -v
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import hooks  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_progress import TempState, launch  # noqa: E402


def call(description: str, kind: str = "devkit:tank") -> dict:
    return {"session_id": "s1", "tool_name": "Agent", "tool_use_id": "toolu_n1", "hook_event_name": "PreToolUse",
            "cwd": tempfile.gettempdir(),
            "tool_input": {"description": description, "subagent_type": kind, "prompt": "Write the docs."}}


class FollowUpNamingTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        t0 = time.time() - 3600
        pg.start("Export", plan=["api", "api docs", "api tests"], now=t0)
        pg.assign("api", "Tank", why="the api", now=t0)
        launch("a_t1", "devkit:tank", "Tank: api", at=t0 + 60)
        pg.step("api done", which="api", check="✓ 3 tests pass", from_agent="a_t1", now=t0 + 120)
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a_t1", "agent_type": "devkit:tank",
                                  "last_assistant_message": "done"}, False, now=t0 + 180)

    def test_a_launch_naming_a_done_step_and_two_waiting_steps_equally_gets_the_line_that_links_one(self):
        for step in ("api docs", "api tests"):
            pg.assign(step, "Tank", why="the follow-up work")
        new, told = hooks._launch_naming("claude", call("Tank: api release notes"))
        self.assertIsNone(new)
        self.assertEqual(len(told), 1, told)
        self.assertIn("names “api docs”, “api tests” equally", told[0])
        self.assertIn('dev progress assign "<step>" --to "Tank: api release notes"', told[0])


if __name__ == "__main__":
    unittest.main()
