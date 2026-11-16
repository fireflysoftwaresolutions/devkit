# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# A message the developer types while the agent works. Claude Code runs UserPromptSubmit for it with the running
# turn's prompt_id (seen live, 2026-11-16); Codex runs it for a steering prompt with the running turn's turn_id, and
# sends no prompt_id. It is the developer's latest request inside the same turn, never a new turn: the turn clock
# (turn_started), the turn's own request in continuity, and the once-per-turn rules keyed on the turn stay as they
# were. The transcript (Claude Code) or the rollout (Codex) reports the same message again at a later hook, with its
# own time: continuity keeps it once.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_stop_midturn
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / "lib"))
sys.path.insert(0, str(TESTS))

from devkit import continuity, hooks, state  # noqa: E402
from test_stop_presentation import NEXT, PresentationCase  # noqa: E402

MIDTURN = "Also add a totals row at the bottom of the export, please."
CONTINUITY = hooks._continuity  # the real one: PresentationCase replaces it in every test


class MidturnPromptTests(PresentationCase):
    def ask(self, agent, text, prompt_id=None, **ids):
        # the prompt hook on <text>; Claude Code names the turn by prompt_id, Codex by turn_id (pass turn_id=…)
        if prompt_id is not None:
            ids["prompt_id"] = prompt_id
        out = self.run_hook(hooks.prompt, agent, hook_event_name="UserPromptSubmit", prompt=text, **ids)
        return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else ""

    def prompts(self, agent):
        record = state.read(continuity.record_path(agent, "s1")) or {}
        return [p.get("text") for p in record.get("checkpoint", {}).get("prompts", [])]

    def started_earlier(self, sf):
        # the turn the first prompt started, moved five minutes back: a prompt that restarts the turn shows at once
        at = round(time.time()) - 300
        state.update(sf, lambda d: d.update({"turn_started": at}))
        return at

    def test_a_message_typed_mid_turn_keeps_the_turn_clock_and_a_new_prompt_id_restarts_it(self):
        sf = self.begin("claude")
        self.ask("claude", NEXT, "p-1")
        at = self.started_earlier(sf)
        self.ask("claude", MIDTURN, "p-1")
        self.assertEqual(state.read(sf)["turn_started"], at)
        self.ask("claude", "Now write the release notes for it.", "p-2")  # the developer's next turn
        self.assertGreater(state.read(sf)["turn_started"], at)

    def test_a_codex_steering_prompt_with_the_running_turn_id_keeps_the_turn_clock_and_a_new_turn_id_restarts_it(self):
        sf = self.begin("codex")
        self.ask("codex", NEXT, turn_id="t-1")
        at = self.started_earlier(sf)
        self.ask("codex", MIDTURN, turn_id="t-1")
        self.assertEqual(state.read(sf)["turn_started"], at)
        self.ask("codex", "Now write the release notes for it.", turn_id="t-2")
        self.assertGreater(state.read(sf)["turn_started"], at)

    def test_the_turns_own_request_stays_in_continuity_beside_the_message_typed_mid_turn(self):
        self.begin("claude")
        with mock.patch.object(hooks, "_continuity", CONTINUITY):
            self.ask("claude", NEXT, "p-1")
            self.ask("claude", MIDTURN, "p-1")
        self.assertEqual(self.prompts("claude"), [NEXT, MIDTURN])

    def test_a_message_typed_mid_turn_is_kept_once_when_the_transcript_reports_it_seconds_later(self):
        for agent, ids in (("claude", {"prompt_id": "p-1"}), ("codex", {"turn_id": "t-1"})):
            with self.subTest(agent=agent):
                self.begin(agent)
                with mock.patch.object(hooks, "_continuity", CONTINUITY):
                    self.ask(agent, NEXT, **ids)
                    self.ask(agent, MIDTURN, **ids)
                # what progress.note_midturn passes at a later tool call: the transcript's (or rollout's) own time;
                # Codex's rollout reader also sees the turn's own prompt
                continuity.on_midturn(agent, "s1", MIDTURN, ts=time.time() + 8)
                continuity.on_midturn(agent, "s1", NEXT, ts=time.time() + 3)
                self.assertEqual(self.prompts(agent), [NEXT, MIDTURN])


if __name__ == "__main__":
    unittest.main()
