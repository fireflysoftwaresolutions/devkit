# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The reply-language rule at Stop (the developer, 2026-11-12): when the developer's message has a known language (or
# devkit's language is set to a locale) and the final message's prose is surely in another one, the Stop hook sends
# it back once per turn with "reply in <language>". It never fires on a guess, never on the reply to a send-back, and
# never on a crew member's report (members report to the tech lead in American English).
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_canary_language
import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / "lib"))
sys.path.insert(0, str(TESTS))

import _sandbox  # noqa: E402,F401

from devkit import canary, core, hooks, scope  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_progress import TempState  # noqa: E402
from test_language_samples import (ENGLISH_ABOUT_OTHER_TEXT, PROMPTS, REPLIES, SWITCHED,  # noqa: E402
                                   claude_transcript)

T0 = 1_800_000_000.0


class LanguageReasonTests(unittest.TestCase):
    def test_the_reason_names_both_languages_and_what_stays_as_it_is(self):
        why = canary.language_reason("es", "en", "latest")
        self.assertIn("the developer wrote in Spanish, but your final message is in English: reply in Spanish", why)
        self.assertIn("**[Firefly> DevKit]**", why)
        self.assertIn("American English", why)

    def test_a_set_locale_is_named_as_the_reason(self):
        self.assertIn("devkit's language is set to Spanish, but your final message is in English: reply in Spanish",
                      canary.language_reason("es", "en", "set"))


class LanguageGateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.transcript = self.dir / "s.jsonl"
        claude_transcript(self.transcript, [PROMPTS["es"][0]])
        p = mock.patch.dict(core.CFG, {"language": "auto", "canary": {"enforce": True}})
        p.start()
        self.addCleanup(p.stop)

    def gate(self, text, sess, agent="claude", active=False, transcript=None):
        data = {"session_id": "s1", "last_assistant_message": text, "stop_hook_active": active,
                "transcript_path": str(transcript or self.transcript)}
        return canary.language_gate(agent, data, sess)

    def test_an_english_reply_to_a_spanish_developer_goes_back_once_per_turn(self):
        sess = {"turn_started": T0}
        why = self.gate(REPLIES["en"], sess)
        self.assertIn("reply in Spanish", why)
        self.assertEqual(sess["language_told"], T0)
        self.assertEqual(self.gate(REPLIES["en"], sess), "")  # the same turn: never a second time
        sess["turn_started"] = T0 + 60  # the next turn is checked again
        self.assertIn("reply in Spanish", self.gate(REPLIES["en"], sess))

    def test_an_english_reply_to_an_english_request_about_spanish_text_passes(self):
        # "Please translate this customer email into English: Hola equipo, …": the reply is rightly in English
        for prompt in ENGLISH_ABOUT_OTHER_TEXT:
            claude_transcript(self.transcript, [prompt])
            with self.subTest(prompt=prompt[:50]):
                self.assertEqual(self.gate(REPLIES["en"], {"turn_started": T0}), "")

    def test_a_reply_in_the_language_the_developer_switched_to_passes(self):
        # an earlier message in one language, the latest in another, often with text to work on: the latest one's own
        # words decide, or nothing does; an older message never decides for a message that has prose
        for earlier, latest, reply in SWITCHED:
            claude_transcript(self.transcript, [earlier, latest])
            with self.subTest(latest=latest[:40]):
                self.assertEqual(self.gate(reply, {"turn_started": T0}), "")

    def test_a_short_message_after_one_that_could_not_decide_never_reaches_an_older_one(self):
        # English, then a Spanish question with a Spanish paragraph (no language: silent), then "vale": the short one
        # stops at the message before it, which tells nothing, so the English one never decides
        claude_transcript(self.transcript, [PROMPTS["en"][0], "¿Puedes revisarlo?\n\nEl build falla en main porque "
                                            "el lockfile está desactualizado y npm ci lo rechaza siempre.", "vale"])
        self.assertEqual(self.gate(REPLIES["es"], {"turn_started": T0}), "")

    def test_a_spanish_reply_passes(self):
        self.assertEqual(self.gate(REPLIES["es"], {"turn_started": T0}), "")

    def test_the_reply_to_a_send_back_is_not_checked_again(self):
        self.assertEqual(self.gate(REPLIES["en"], {"turn_started": T0}, active=True), "")

    def test_an_unclear_developer_gives_no_finding(self):
        claude_transcript(self.transcript, ["ok"])
        self.assertEqual(self.gate(REPLIES["es"], {"turn_started": T0}), "")
        self.assertEqual(self.gate(REPLIES["en"], {"turn_started": T0}), "")

    def test_a_set_locale_is_enforced_whatever_the_developer_writes(self):
        claude_transcript(self.transcript, [PROMPTS["en"][0]])
        with mock.patch.dict(core.CFG, {"language": "es-ES"}):
            self.assertIn("reply in Spanish", self.gate(REPLIES["en"], {"turn_started": T0}))
        with mock.patch.dict(core.CFG, {"language": "en-US"}):
            claude_transcript(self.transcript, [PROMPTS["es"][0]])
            self.assertEqual(self.gate(REPLIES["en"], {"turn_started": T0}), "")

    def test_a_codex_members_rollout_is_not_checked(self):
        # a Codex member's rollout copies its parent's history: the developer's Spanish is there, the report is not
        rollout = self.dir / "member.jsonl"
        rows = [{"type": "session_meta", "payload": {"id": "t2", "source": {"subagent": {"thread_spawn": {}}}}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": PROMPTS["es"][0]}}]
        rollout.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.assertEqual(self.gate(REPLIES["en"], {"turn_started": T0}, agent="codex", transcript=rollout), "")
        own = self.dir / "own.jsonl"
        rows[0] = {"type": "session_meta", "payload": {"id": "t1", "source": "cli"}}
        own.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.assertIn("reply in Spanish", self.gate(REPLIES["en"], {"turn_started": T0}, agent="codex",
                                                    transcript=own))

    def test_the_rule_is_off_with_canary_enforce_or_canary_language(self):
        for canary_cfg in ({"enforce": False}, {"enforce": True, "language": False}):
            with self.subTest(canary=canary_cfg), mock.patch.dict(core.CFG, {"canary": canary_cfg}):
                self.assertEqual(self.gate(REPLIES["en"], {"turn_started": T0}), "")

    def test_an_error_fails_open(self):
        with mock.patch("devkit.language.wanted", side_effect=RuntimeError("boom")):
            self.assertEqual(self.gate(REPLIES["en"], {"turn_started": T0}), "")


class HookWiringTests(unittest.TestCase):
    # hooks.stop sends an English reply to a Spanish developer back on both agents; hooks.prompt adds the line
    def setUp(self):
        self.state = TempState()
        self.state.__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.transcript = Path(self.state.tmp.name) / "t.jsonl"
        claude_transcript(self.transcript, [PROMPTS["es"][0]])
        p = mock.patch.dict(core.CFG, {"language": "auto", "canary": {"enforce": True}})
        p.start()
        self.addCleanup(p.stop)

    def run_hook(self, fn, agent, data):
        sf = pg.SESSIONS / f"{agent}-s1.json"
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text(json.dumps({"agent": agent, "session": "s1", "started": time.time() - 600,
                                  "turn_started": time.time() - 60}))
        out = io.StringIO()
        with mock.patch.object(scope, "decide", lambda data, sess: True), \
                mock.patch.object(hooks, "_session_file", lambda a, sid: pg.SESSIONS / f"{a}-{sid}.json"), \
                mock.patch.object(hooks, "STATE", Path(self.state.tmp.name)), \
                mock.patch.object(hooks, "_continuity", lambda *a, **k: None), \
                mock.patch.object(hooks, "_spawn_sync", lambda args: None), \
                mock.patch.object(canary, "LOG", Path(self.state.tmp.name) / "canary.jsonl"), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(data))), contextlib.redirect_stdout(out):
            (hooks.stop if fn == "stop" else hooks.prompt)(agent)
        return out.getvalue()

    def test_stop_sends_an_english_reply_to_a_spanish_developer_back_on_both_agents(self):
        for agent in ("claude", "codex"):
            data = {"session_id": "s1", "cwd": self.state.tmp.name, "last_assistant_message": REPLIES["en"],
                    "transcript_path": str(self.transcript)}
            out = json.loads(self.run_hook("stop", agent, data) or "{}")
            self.assertEqual(out.get("decision"), "block", agent)
            self.assertIn("reply in Spanish", out.get("reason", ""), agent)

    def test_the_prompt_hook_adds_the_reply_language_line(self):
        data = {"session_id": "s1", "cwd": self.state.tmp.name, "prompt": PROMPTS["es"][1],
                "transcript_path": str(self.transcript)}
        out = json.loads(self.run_hook("prompt", "claude", data))["hookSpecificOutput"]["additionalContext"]
        self.assertIn("devkit · the developer wrote in Spanish: reply in Spanish", out)


if __name__ == "__main__":
    unittest.main()
