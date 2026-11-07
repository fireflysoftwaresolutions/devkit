# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Compaction, resume and clear payloads (spec C12, step F10 "compaction spike"): contract tests on real payloads.
#
# tests/fixtures/hooks/claude/: payloads Claude Code 2.1.319 really sent to a capture-only hook in a throwaway
# headless session (manual /compact, --resume, /clear, PreCompact exit 2), redacted with hooks._scrub, home path
# rewritten to /Users/dev. tests/fixtures/hooks/claude/transcript/compact.jsonl: the two transcript entries a
# compaction writes. tests/fixtures/hooks/codex/: the hook schemas embedded in the codex 0.166.0 binary, NOT live
# payloads (unverified live until 2026-11-09).
#
# Each test pins a field or an ordering the continuity module (C12) will read. When a Claude Code or Codex upgrade
# moves one, refresh the fixture and these tests show what moved.
# Run: python3 -m unittest tests.test_compaction_payloads
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import audit, hooks  # noqa: E402

HOOKS = Path(__file__).resolve().parent / "fixtures" / "hooks"
CLAUDE = HOOKS / "claude"
CODEX = HOOKS / "codex"


def claude(name: str) -> dict:
    return json.loads((CLAUDE / f"{name}.json").read_text())


def codex(name: str) -> dict:
    return json.loads((CODEX / f"{name}.schema.json").read_text())


def summary() -> str:
    """The whole compact_summary the spike's PostCompact carried; the payload fixture keeps its first 200 characters,
    as a capture does (hooks.CAPTURE_CUT_KEYS)."""
    return (CLAUDE / "transcript" / "compact-summary.txt").read_text()


def transcript() -> list:
    return [json.loads(line) for line in (CLAUDE / "transcript" / "compact.jsonl").read_text().splitlines() if line]


class ClaudeCompactionTests(unittest.TestCase):
    def test_one_manual_compaction_fires_three_hooks_that_share_session_and_prompt_ids(self):
        # PreCompact, SessionStart(compact), PostCompact: one compaction = one prompt_id, so devkit can pair them
        # and record the compaction once even when only one of the three hooks ran.
        pre, start, post = claude("PreCompact.manual"), claude("SessionStart.compact"), claude("PostCompact.manual")
        self.assertEqual({pre["session_id"]}, {start["session_id"], post["session_id"]})
        self.assertEqual({pre["prompt_id"]}, {start["prompt_id"], post["prompt_id"]})
        self.assertEqual((pre["trigger"], post["trigger"], start["source"]), ("manual", "manual", "compact"))

    def test_pre_compact_carries_the_trigger_and_the_user_instructions_but_no_token_counts(self):
        # custom_instructions is null for a bare /compact (the text after "/compact " otherwise). Token counts are
        # only in the transcript's compact_boundary, so the record reads them there.
        pre = claude("PreCompact.manual")
        self.assertIn("custom_instructions", pre)
        self.assertIsNone(pre["custom_instructions"])
        self.assertFalse({"agent_id", "preTokens", "context_tokens"} & set(pre))
        self.assertTrue(pre["transcript_path"].endswith(f"/{pre['session_id']}.jsonl"))

    def test_post_compact_gives_the_whole_summary_devkit_checks_for_missing_items(self):
        # The "Not in the summary" list (C12.3) is computed from compact_summary: the <summary> block the
        # summarizer wrote, with its "All user messages" section.
        whole = summary()
        self.assertTrue(whole.startswith(claude("PostCompact.manual")["compact_summary"][:-1]))
        self.assertIn("<summary>", whole)
        self.assertIn("</summary>", whole)
        self.assertIn("All user messages", whole)

    def test_pre_compact_stdout_and_claude_md_compact_instructions_both_reach_the_summarizer(self):
        # The capture hook printed "Include the exact word QUOKKA-PRE in the summary." at PreCompact (exit 0), and
        # the project CLAUDE.md had a "## Compact Instructions" section asking for ZEPHYR-CMD. Both words are in the
        # summary: devkit can steer the summarizer from PreCompact, and the global rules section works too.
        self.assertIn("QUOKKA-PRE", summary())
        self.assertIn("ZEPHYR-CMD", summary())

    def test_session_start_compact_has_no_resume_fields_and_names_the_model(self):
        start = claude("SessionStart.compact")
        self.assertEqual(start["model"].split("-")[0], "claude")
        self.assertFalse({"context_tokens", "seconds_since_last_response", "agent_id"} & set(start))

    def test_the_summarizer_runs_as_an_unnamed_subagent_that_only_stops(self):
        # Compaction runs a forked summarizer that fires SubagentStop (agent_type "", same prompt_id) with no
        # SubagentStart and no transcript file. devkit's lane binding must ignore it, not close or invent a lane.
        stop, pre = claude("SubagentStop.compact-summarizer"), claude("PreCompact.manual")
        self.assertEqual(stop["agent_type"], "")
        self.assertEqual(stop["prompt_id"], pre["prompt_id"])
        self.assertTrue(stop["last_assistant_message"].startswith("<analysis>"))
        self.assertFalse((CLAUDE / "SubagentStart.compact-summarizer.json").exists())

    def test_a_blocked_compaction_fires_pre_compact_only(self):
        # PreCompact exit 2 refused the /compact ("Compaction blocked by PreCompact hook: ..."): no SessionStart
        # (compact), no PostCompact, no boundary. C12.8: devkit never blocks; it fails open.
        blocked = claude("PreCompact.manual.blocked")
        self.assertEqual((blocked["hook_event_name"], blocked["trigger"]), ("PreCompact", "manual"))
        self.assertEqual(blocked["session_id"], claude("PreCompact.manual")["session_id"])
        self.assertNotEqual(blocked["prompt_id"], claude("PreCompact.manual")["prompt_id"])

    def test_resume_after_a_compaction_reports_the_compacted_size(self):
        # context_tokens at resume equals the boundary's postTokens: resume can tell the session was compacted.
        resume = claude("SessionStart.resume.after-compact")
        boundary = transcript()[0]
        self.assertEqual(resume["source"], "resume")
        self.assertEqual(resume["context_tokens"], boundary["compactMetadata"]["postTokens"])
        self.assertLess(resume["context_tokens"], boundary["compactMetadata"]["preTokens"])

    def test_clear_ends_the_old_session_and_starts_a_new_one_with_no_link_back(self):
        # /clear: SessionEnd(reason clear) for the old id, then SessionStart(clear) with a NEW session_id and no
        # field naming the old one. devkit links them by cwd and time (C12.4 open-run notice).
        end, start = claude("SessionEnd.clear"), claude("SessionStart.clear")
        self.assertEqual((end["reason"], start["source"]), ("clear", "clear"))
        self.assertEqual(end["session_id"], claude("PreCompact.manual")["session_id"])
        self.assertNotEqual(start["session_id"], end["session_id"])
        self.assertEqual(start["cwd"], end["cwd"])
        self.assertNotIn(end["session_id"], json.dumps(start))

    def test_the_transcript_records_a_boundary_then_the_summary_message(self):
        # The late-detection fallback (C12.1) looks for this pair after the last offset it saw.
        boundary, summary = transcript()
        self.assertEqual((boundary["type"], boundary["subtype"]), ("system", "compact_boundary"))
        self.assertLessEqual({"trigger", "preTokens", "postTokens", "cumulativeDroppedTokens", "durationMs",
                              "preservedSegment"}, set(boundary["compactMetadata"]))
        self.assertEqual(boundary["compactMetadata"]["trigger"], claude("PreCompact.manual")["trigger"])
        self.assertTrue(summary["isCompactSummary"])
        self.assertEqual(summary["parentUuid"], boundary["uuid"])
        self.assertEqual(summary["sessionId"], boundary["sessionId"])

    def test_the_fixtures_hold_no_tokens_home_paths_or_uncut_text(self):
        files = sorted(CLAUDE.glob("*Compact*.json")) + sorted(CLAUDE.glob("*.c*-*.json")) + \
            sorted(CLAUDE.glob("*.clear.json")) + [CLAUDE / "transcript" / "compact.jsonl",
                                                   CLAUDE / "transcript" / "compact-summary.txt"]
        for f in files:
            with self.subTest(f.name):
                text = f.read_text()
                self.assertIsNone(audit.SECRET.search(text))
                self.assertNotIn(str(Path.home()), text)
                self.assertNotIn("/private/tmp/", text)
                if f.suffix == ".json":
                    d = json.loads(text)
                    for key in hooks.CAPTURE_CUT_KEYS:
                        if isinstance(d.get(key), str):
                            self.assertLessEqual(len(d[key]), hooks.CAPTURE_CUT + 1)


class CodexCompactionSchemaTests(unittest.TestCase):
    # From the codex 0.166.0 binary's embedded hook schemas. Unverified live until 2026-11-09.

    def test_the_schemas_are_labeled_as_extracted_not_captured(self):
        for name in ("PreCompact.input", "PreCompact.output", "PostCompact.input", "PostCompact.output",
                     "SessionStart.input", "SessionStart.output"):
            with self.subTest(name):
                self.assertIn("unverified live until 2026-11-09", codex(name)["$comment"])

    def test_compaction_inputs_carry_trigger_and_turn_but_no_summary_or_instructions(self):
        # Unlike Claude, Codex sends no custom_instructions and no compact_summary: the "Not in the summary" list
        # cannot be computed from a Codex hook.
        for name, event in (("PreCompact.input", "PreCompact"), ("PostCompact.input", "PostCompact")):
            with self.subTest(name):
                s = codex(name)
                self.assertEqual(s["properties"]["hook_event_name"]["const"], event)
                self.assertEqual(s["properties"]["trigger"]["enum"], ["manual", "auto"])
                self.assertLessEqual({"session_id", "turn_id", "transcript_path", "cwd", "model", "trigger"},
                                     set(s["required"]))
                self.assertLessEqual({"agent_id", "agent_type"}, set(s["properties"]) - set(s["required"]))
                self.assertFalse({"custom_instructions", "compact_summary"} & set(s["properties"]))
                self.assertFalse(s["additionalProperties"])

    def test_compaction_hooks_cannot_add_context_only_session_start_can(self):
        # PreCompact and PostCompact outputs allow only continue, stopReason, suppressOutput and systemMessage, so
        # on Codex the brief goes through SessionStart(compact) additionalContext.
        for name in ("PreCompact.output", "PostCompact.output"):
            with self.subTest(name):
                self.assertEqual(set(codex(name)["properties"]),
                                 {"continue", "stopReason", "suppressOutput", "systemMessage"})
        start_in, start_out = codex("SessionStart.input"), codex("SessionStart.output")
        self.assertIn("compact", start_in["properties"]["source"]["enum"])
        wire = start_out["definitions"]["SessionStartHookSpecificOutputWire"]["properties"]
        self.assertIn("additionalContext", wire)


if __name__ == "__main__":
    unittest.main()
