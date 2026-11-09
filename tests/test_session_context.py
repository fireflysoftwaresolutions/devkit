# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The context a session starts with stays whole: Claude Code cuts a hook's output past 10,000 characters to a
# 2,000-character preview, and Codex past about 2,500 tokens (4 bytes a token). These tests run the real SessionStart
# and UserPromptSubmit handlers on a project with a long handoff note, many tasks and lessons and the real crew roster,
# and check the size, the order (what matters first) and what the trimmed parts keep.
# Run: python3 -m unittest tests.test_session_context
import contextlib
import io
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import core, hooks, lessons, notes, welcome, work  # noqa: E402
from test_hooks_welcome import OLDER, WelcomeHook  # noqa: E402

CLAUDE_CHARS = 9000  # under Claude Code's 10,000-character cap, with room for what the harness adds
CODEX_BYTES = 2500 * 4  # Codex's ~2,500 tokens, counted as 4 bytes a token
A = {"user": "ana", "host": "mbp1", "agent": "claude", "session": "s0"}
STATE_FIRST = "The CSV export streams 10,000 rows in 1.2 s; the XLSX writer is half done."


class SessionContext(WelcomeHook):
    def setUp(self):
        super().setUp()
        for obj, name, value in ((lessons, "LESSONS", self.root / "handoff" / "lessons"),
                                 (lessons, "USAGE_LOCAL", self.root / "lesson-usage.json"),
                                 (work, "ITEMS", self.root / "handoff" / "items")):  # each test its own tasks
            p = mock.patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.note = core.note_path(self.pid)
        self.write_note()
        for i in range(12):  # as long as a real project's: a title that says the whole plan
            work.create(self.pid, f"Now item {i}: keep the totals of a month equal to the ledger's; " + ", ".join(
                f"check the {x} totals against the ledger export and the bank file before the close"
                for x in ("daily", "weekly", "monthly")), bucket="now", why="finance checks them", a=A)
        for i in range(25):
            work.create(self.pid, f"Next item {i}: a follow-up for the export screens", bucket="next", a=A)
        for i in range(6):
            lessons.add(self.pid, f"Lesson {i}: a migration ran twice because a retry lacked its key",
                        happened="the job retried a whole batch without its idempotency key",
                        files=[f"jobs/batch{i}/**"], cause="the retry wrapped the batch",
                        prevention="Key each batch by its id and skip a key already applied; check the job log "
                                   "before rerunning a failed batch by hand", a=A)

    def write_note(self, state_lines: int = 70):
        state = [STATE_FIRST] + [f"- Area {i}: the importer reads quoted commas, the totals round half-even, and the "
                                 f"monthly close waits on the ledger export" for i in range(state_lines)]
        text = notes.new_note(self.pid)
        text = text.replace("_What this project is, who uses it, where it runs (1-3 lines)._",
                            "Invoicing for the finance team: billing runs, exports and the monthly close.")
        text = text.replace("_What works, what is half done, what is broken. Keep it true; rewrite rather than "
                            "append._", "\n".join(state))
        text = text.replace("- _Non-obvious facts a newcomer (human or agent) would otherwise rediscover the hard "
                            "way._", "\n".join(f"- Gotcha {i}: the ledger API pages by cursor, never by offset"
                                               for i in range(20)))
        text += "".join(f"- 2026-11-0{i % 9 + 1} 10:00 · ana@mbp1 · claude · main · session line {i}\n"
                        for i in range(12))
        self.note.parent.mkdir(parents=True, exist_ok=True)
        self.note.write_text(text)

    def prompt_without_a_start(self, agent: str) -> str:
        payload = {"session_id": "p1", "hook_event_name": "UserPromptSubmit", "prompt": "go on",
                   "cwd": str(self.repo)}
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
            self.assertEqual(hooks.run("prompt", agent), 0)
        return json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]

    def assertUnderBothCaps(self, ctx: str):
        self.assertLessEqual(len(ctx), CLAUDE_CHARS)
        self.assertLessEqual(len(ctx.encode("utf-8")), CODEX_BYTES)


class Size(SessionContext):
    def test_a_realistic_project_starts_under_both_caps_with_the_welcome_note_first(self):
        self.live_run()
        for agent, sid in (("claude", "c1"), ("codex", "x1")):
            with self.subTest(agent=agent):
                out = self.start(agent, sid)
                ctx = out["hookSpecificOutput"]["additionalContext"]
                self.assertUnderBothCaps(ctx)
                self.assertEqual(ctx.split("\n\n", 1)[0], welcome.for_model(out["systemMessage"]))
                order = [ctx.index(x) for x in (f"Project: **{self.pid}**", f"Handoff note: {core.tilde(self.note)}",
                                                "dev crew raci", "Tasks, Now:", STATE_FIRST)]
                self.assertEqual(order, sorted(order))

    def test_a_long_brief_cuts_the_handoff_note_and_never_the_welcome_the_brief_or_a_notice(self):
        brief = "devkit · this conversation was compacted. " + " ".join(f"request {i} kept word for word."
                                                                       for i in range(220))
        self.live_run()
        with mock.patch.object(hooks, "_continuity", lambda fn, *a, **k: brief if fn == "on_session_start" else ""), \
                mock.patch.object(hooks, "_older_copy_notice", lambda *a: OLDER):
            out = self.start("codex", "x2")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertUnderBothCaps(ctx)
        self.assertTrue(ctx.startswith(welcome.for_model(out["systemMessage"]) + "\n\n" + brief + "\n\n" + OLDER),
                        ctx[:300])
        self.assertNotIn("Area 69:", ctx)
        self.assertIn("the handoff note has the rest", ctx.splitlines()[-1])

    def test_a_session_that_missed_its_start_gets_the_same_bounded_context_at_its_first_prompt(self):
        ctx = self.prompt_without_a_start("codex")
        self.assertUnderBothCaps(ctx)
        self.assertIn(f"Project: **{self.pid}**", ctx)


class Trimmed(SessionContext):
    def test_the_handoff_excerpt_is_the_now_tasks_and_the_current_state(self):
        ctx = self.start("claude")["hookSpecificOutput"]["additionalContext"]
        excerpt = ctx.split("--- handoff note ---", 1)[1]
        first = notes.summarize(self.note.read_text())["tasks"]["now"][0]  # in the note's order
        self.assertIn(first[:80], excerpt)
        self.assertIn(STATE_FIRST, excerpt)
        now = excerpt.split("Tasks, Now:\n", 1)[1].split("\n\n", 1)[0].splitlines()
        self.assertEqual(len(now), 6)  # five tasks, one short line each, then how many more: the state keeps its room
        self.assertEqual(now[-1], "- … 7 more (`dev task`)")
        self.assertLessEqual(max(len(x) for x in now), 2 + 160)
        for left_out in ("Next item 0", "Gotcha 0", "session line 0", "Invoicing for the finance team"):
            self.assertNotIn(left_out, ctx)

    def test_the_context_names_three_known_pitfalls_at_most(self):
        ctx = self.start("claude")["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(ctx.count("a migration ran twice because"), 3)


if __name__ == "__main__":
    unittest.main()
