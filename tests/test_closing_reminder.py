# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The status-block reminder at the tool calls that usually end a turn (a subagent launch, a background command,
the closing `dev` commands), so the block comes first without the Stop hook sending the turn back.
Run: python3 -m unittest"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import hooks, progress  # noqa: E402

T0 = 1_793_000_000  # a fixed clock


def shell(cmd, background=False):
    tin = {"command": cmd}
    if background:
        tin["run_in_background"] = True
    return {"session_id": "s1", "tool_name": "Bash", "tool_input": tin}


LAUNCH = {"session_id": "s1", "tool_name": "Agent", "tool_input": {"description": "Ghost: review the guard"}}


class ClosingReminderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "api"
        subprocess.run(["git", "init", "-q", "-b", "feat/export", str(self.repo)], check=True)
        self.sess = {"project": "acme/api", "top": str(self.repo), "turn_started": T0, "turn_class": "dev"}

    def tearDown(self):
        self.tmp.cleanup()

    def remind(self, data, now=T0 + 60):
        return hooks.closing_reminder(data, self.sess, now)

    def test_a_subagent_launch_gets_the_block_with_project_and_branch(self):
        text = self.remind(LAUNCH)
        self.assertIn("**[Firefly> DevKit]** api · `feat/export` · <task>", text)
        self.assertIn("Nothing above it", text)
        self.assertIn("a subagent launch", text)

    def test_a_session_started_outside_the_repository_uses_the_folder_of_the_call(self):
        self.sess = {"turn_started": T0}  # started in the home folder: no project, no top
        (self.repo / "src").mkdir()
        text = self.remind(dict(LAUNCH, cwd=str(self.repo / "src")))
        self.assertIn("**[Firefly> DevKit]** api · `feat/export` · <task>", text)

    def test_background_and_closing_commands_get_it_ordinary_commands_do_not(self):
        self.assertIn("a background command", self.remind(shell("pytest -q", background=True)))
        self.sess.pop("closing_nudge", None)
        self.assertIn("`dev progress done`", self.remind(shell('dev progress done "shipped" && dev sync')))
        self.sess.pop("closing_nudge", None)
        self.assertIn("`dev handoff --log`", self.remind(shell('bin/dev handoff --log "export works"')))
        self.sess.pop("closing_nudge", None)
        codex = {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": ["bash", "-lc", "dev push"]}}
        self.assertIn("`dev push`", self.remind(codex))
        for quiet in ("ls -la", 'echo "dev progress done"', 'dev progress step "parser" --check "3 tests"',
                      "git status"):
            self.sess.pop("closing_nudge", None)
            self.assertEqual(self.remind(shell(quiet)), "", quiet)

    def test_parallel_launches_get_one_reminder_until_the_gap_or_a_new_turn(self):
        self.assertTrue(self.remind(LAUNCH, now=T0 + 60))
        self.assertEqual(self.remind(LAUNCH, now=T0 + 90), "")  # the second of four parallel launches
        self.assertTrue(self.remind(LAUNCH, now=T0 + 60 + hooks.CLOSING_GAP))
        self.sess["turn_started"] = T0 + 200  # a new turn: its first closing moment is reminded again
        self.assertTrue(self.remind(LAUNCH, now=T0 + 210))

    def test_subagents_and_non_development_turns_get_nothing(self):
        self.assertEqual(self.remind(dict(LAUNCH, agent_id="a1")), "")
        self.sess["turn_class"] = "other"
        self.assertEqual(self.remind(LAUNCH), "")
        with mock.patch.dict(os.environ, {"DEVKIT_SCOPE": "all"}):
            self.assertTrue(self.remind(LAUNCH))

    def test_the_post_tool_hook_delivers_it_and_remembers_when(self):
        sf = Path(self.tmp.name) / "claude-s1.json"
        sf.write_text(json.dumps(self.sess))
        out = io.StringIO()
        with mock.patch.object(hooks, "_session_file", lambda agent, sid: sf), \
                mock.patch.object(progress, "on_tool_done", lambda agent, data: None), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(LAUNCH))), contextlib.redirect_stdout(out):
            hooks.post_tool("claude")
        ctx = json.loads(out.getvalue())["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "PostToolUse")
        self.assertIn("**[Firefly> DevKit]** api · `feat/export`", ctx["additionalContext"])
        self.assertGreater(json.loads(sf.read_text())["closing_nudge"], T0)


if __name__ == "__main__":
    unittest.main()
