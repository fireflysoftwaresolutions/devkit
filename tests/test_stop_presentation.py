# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The Stop hook never makes the developer read the same reply twice. A final message that breaks a presentation rule
# (the status block missing or not first, a crew member named without the role, the Progress line, the reply
# language) is not sent back: the developer has already read it. devkit records the miss (the audit, the session's
# flags, the canary's score) and the next prompt's context carries a short reminder, so the next reply is right.
# Gates that need more work (skills, stubs, tests that cannot fail, waits) still send the turn back, and their reason
# ends by asking for only what is new since the last reply. Claude Code and Codex run the same hooks.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_stop_presentation
import contextlib
import io
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / "lib"))
sys.path.insert(0, str(TESTS))

from devkit import audit, canary, core, hooks, scope, skillguard, state  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_language_neutral_hooks import HookCase  # noqa: E402
from test_language_samples import PROMPTS, REPLIES, claude_transcript  # noqa: E402

AGENTS = ("claude", "codex")
BLOCK = ("**[Firefly> DevKit]** api · `main` · export\n- **Done:** the export.\n- **Check:** ✓ 12 tests\n"
         "- **Next:** the docs.")
UNROLED = BLOCK + "\n\nTank finished the API."
SKILLS = "devkit: load the `testing` skill with the Skill tool, then review the tests you changed."
NO_REPEAT_END = "says only what is new since that reply; never repeat what it said."
NOT_AGAIN = "do not rewrite or repeat it"
NEXT = "Add the PDF export next, please, with the same columns as the CSV one."


class PresentationCase(HookCase):
    # HookCase's temporary state, plus the canary's log in the test's folder, the dev root there (the canary scores
    # turns under ~/Development only), every turn a development turn, and no required skills unless a test says so
    def setUp(self):
        super().setUp()
        self.transcript = self.root / "t.jsonl"
        claude_transcript(self.transcript, [PROMPTS["en"][0]])
        for p in (mock.patch.object(canary, "LOG", self.root / "canary.jsonl"),
                  mock.patch.object(canary, "DEV", self.root),
                  mock.patch.object(hooks, "_session_file", lambda a, sid: pg.SESSIONS / f"{a}-{sid}.json"),
                  mock.patch.object(hooks, "_continuity", lambda *a, **k: None),
                  mock.patch.object(hooks, "_spawn_sync", lambda *a, **k: None),
                  mock.patch.object(scope, "decide", lambda data, sess: True),
                  mock.patch.object(skillguard, "on_stop", lambda *a: None),
                  mock.patch.object(audit, "policy_file", lambda: self.root / "no-policy.json"),
                  mock.patch.dict(core.CFG, {"language": "auto", "canary": {"enforce": True}, "audit": {}})):
            p.start()
            self.addCleanup(p.stop)

    def begin(self, agent):
        # a development turn of <agent>:s1 that started a minute ago
        sf = pg.SESSIONS / f"{agent}-s1.json"
        sf.parent.mkdir(parents=True, exist_ok=True)
        state.replace(sf, {"agent": agent, "session": "s1", "started": time.time() - 600,
                           "turn_started": time.time() - 60})
        return sf

    def run_hook(self, fn, agent, **data):
        payload = dict({"session_id": "s1", "cwd": str(self.root), "transcript_path": str(self.transcript)}, **data)
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
            fn(agent)
        return out.getvalue()

    def stop(self, agent, message, again=False):
        out = self.run_hook(hooks.stop, agent, hook_event_name="Stop", last_assistant_message=message,
                            stop_hook_active=again)
        return json.loads(out) if out.strip() else {}

    def prompt(self, agent, text=NEXT):
        out = self.run_hook(hooks.prompt, agent, hook_event_name="UserPromptSubmit", prompt=text)
        return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else ""

    def reminded(self, agent):
        # the rules devkit recorded as reminded for <agent>'s session, in order
        return [r["evidence"]["rule"] for r in audit.records(1, "canary.remind")
                if str((r.get("ref") or {}).get("session") or "").startswith(agent + ":")]

    def blocked(self, agent):
        return [r["evidence"]["rule"] for r in audit.records(1, "stop.block")
                if str((r.get("ref") or {}).get("session") or "").startswith(agent + ":")]


class PresentationMissTests(PresentationCase):
    def test_a_reply_without_the_status_block_is_recorded_and_reminded_at_the_next_prompt(self):
        for agent in AGENTS:
            with self.subTest(agent=agent):
                sf = self.begin(agent)
                out = self.stop(agent, "Done: the export works.")
                self.assertNotIn("decision", out)
                if agent == "claude":  # Claude Code still shows the developer the canary's warning at once
                    self.assertIn("this reply did not start with", out.get("systemMessage", ""))
                self.assertEqual(self.reminded(agent), ["status"])
                self.assertEqual(self.blocked(agent), [])
                self.assertTrue(state.read(sf)["canary_missed"])  # the session's flag, as before
                self.assertFalse(canary.records(1)[-1]["ok"])  # the canary's score counts the miss
                ctx = self.prompt(agent)
                self.assertIn('it starts with "Done: the export works."', ctx)
                self.assertIn("**[Firefly> DevKit]** <project> · `<branch>` · <task>", ctx)
                self.assertIn(NOT_AGAIN, ctx)
                self.assertNotIn('it starts with "Done: the export works."', self.prompt(agent))  # told once

    def test_a_reply_naming_a_member_without_the_role_is_recorded_and_reminded_once(self):
        for agent in AGENTS:
            with self.subTest(agent=agent):
                sf = self.begin(agent)
                out = self.stop(agent, UNROLED)
                self.assertNotIn("decision", out)
                self.assertEqual(self.reminded(agent), ["roles"])
                self.assertIn("the export", state.read(sf)["status"])  # the block's status line is stored as before
                ctx = self.prompt(agent)
                self.assertIn("without their role: Tank", ctx)
                self.assertIn("Backend engineer (Tank)", ctx)
                self.assertIn(NOT_AGAIN, ctx)
                self.assertNotIn("without their role", self.prompt(agent))

    def test_a_block_without_the_runs_progress_line_is_reminded_with_the_line_as_it_is_now(self):
        for agent in AGENTS:
            with self.subTest(agent=agent):
                self.begin(agent)
                with mock.patch.dict(os.environ, {"DEVKIT_SESSION": f"{agent}:s1"}):
                    pg.start("Hotfix", plan=["plan=10m", "api=20m"])
                out = self.stop(agent, BLOCK)
                self.assertNotIn("decision", out)
                self.assertEqual(self.reminded(agent), ["progress"])
                ctx = self.prompt(agent)
                self.assertIn("your last reply", ctx)
                self.assertIn("`- **Progress:** 0 of 2 steps · now 1. plan · ", ctx)

    def test_a_reply_in_another_language_than_the_developers_is_recorded_and_reminded(self):
        claude_transcript(self.transcript, [PROMPTS["es"][0]])
        for agent in AGENTS:
            with self.subTest(agent=agent):
                self.begin(agent)
                out = self.stop(agent, REPLIES["en"])
                self.assertNotIn("decision", out)
                self.assertEqual(self.reminded(agent), ["language"])
                ctx = self.prompt(agent, PROMPTS["es"][1])
                self.assertIn("your last reply was not in the developer's language", ctx)
                self.assertIn("reply in Spanish", ctx)
                self.assertIn(NOT_AGAIN, ctx)

    def test_a_background_notification_that_starts_the_next_turn_carries_the_reminder(self):
        for agent in AGENTS:
            with self.subTest(agent=agent):
                self.begin(agent)
                self.stop(agent, UNROLED)
                ctx = self.prompt(agent, "<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n"
                                         "</task-notification>")
                self.assertIn("without their role: Tank", ctx)


class ReminderTests(PresentationCase):
    # canary.reminder: what the next prompt says about the notes the Stop hook left
    def test_without_a_language_line_in_the_prompt_the_turns_own_reason_is_kept(self):
        note = {"rule": "language", "text": canary.language_reason("es", "en", "latest")}
        text = canary.reminder([note], "claude", "s1", lang_line="")
        self.assertIn("your last reply missed a presentation rule", text)
        self.assertIn("(1) the developer wrote in Spanish, but your final message is in English: reply in Spanish",
                      text)

    def test_a_progress_note_is_dropped_once_the_run_is_no_longer_active(self):
        note = {"rule": "progress", "text": "devkit: your status block has no Progress bullet."}
        self.assertEqual(canary.reminder([note], "claude", "s1"), "")  # no run: no Progress bullet is owed
        roles = {"rule": "roles", "text": "devkit: your final message names crew members without their role: Tank."}
        text = canary.reminder([note, roles], "claude", "s1")
        self.assertIn("(1) your final message names crew members without their role: Tank.", text)
        self.assertNotIn("(2)", text)


class MoreWorkTests(PresentationCase):
    def test_a_gate_that_needs_more_work_still_sends_the_turn_back_and_asks_for_only_what_is_new(self):
        for agent in AGENTS:
            with self.subTest(agent=agent):
                self.begin(agent)
                with mock.patch.object(skillguard, "on_stop", lambda *a: SKILLS):
                    out = self.stop(agent, BLOCK)
                self.assertEqual(out.get("decision"), "block")
                self.assertTrue(out["reason"].startswith(SKILLS), out["reason"])
                self.assertTrue(out["reason"].endswith(NO_REPEAT_END), out["reason"])
                self.assertEqual(self.blocked(agent), ["skills"])

    def test_a_presentation_miss_in_a_turn_sent_back_rides_along_and_is_not_reminded_again(self):
        for agent in AGENTS:
            with self.subTest(agent=agent):
                self.begin(agent)
                with mock.patch.object(skillguard, "on_stop", lambda *a: SKILLS):
                    reason = self.stop(agent, UNROLED)["reason"]
                self.assertIn("Backend engineer (Tank)", reason)
                self.assertLess(reason.index("Backend engineer (Tank)"), reason.index(NO_REPEAT_END))
                self.assertNotIn("Reply again", reason)
                self.assertEqual(self.blocked(agent), ["skills"])  # the role rule did not send it back
                self.assertEqual(self.stop(agent, BLOCK, again=True), {})  # the reply to the send-back ends the turn
                self.assertNotIn("without their role", self.prompt(agent))


if __name__ == "__main__":
    unittest.main()
