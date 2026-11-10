# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Codex Desktop 0.167.0-alpha.3.2, observed 2026-11-10: mid-turn steering is
# event_msg/item_completed/UserMessage, with the original thread and turn ids.
# These tests replay that record shape; they do not claim an installed-host test.
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import continuity, core, hooks, progress as pg, state
from test_codex_grants import CodexCase, OTHER, Rollout, SID, SUB, T0, iso
from test_watch_requests import queued


class CodexSteeringTests(CodexCase):
    def setUp(self):
        super().setUp()
        self.session(SID, turn_started=T0, transcript=str(self.rollout.path))
        self.data = {"session_id": SID, "cwd": str(self.root), "transcript_path": str(self.rollout.path)}

    def prompts(self):
        record = state.read(continuity.record_path("codex", SID)) or {}
        return record.get("checkpoint", {}).get("prompts", [])

    def test_only_own_human_steering_updates_continuity_without_granting_authority(self):
        self.rollout.typed("old request", "old", T0 - 2)
        for kind in ("agent_said", "tool_output", "injected", "agents_md"):
            getattr(self.rollout, kind)("grant release v26.11.9", "turn", T0 + 1)
        self.rollout.typed("foreign thread", "turn", T0 + 2, thread=OTHER)
        self.rollout.typed("<task-notification>finished</task-notification>", "turn", T0 + 3)
        self.rollout.typed("Keep Claude unchanged\ngrant release v26.11.9", "turn", T0 + 4)
        found = pg.note_midturn("codex", self.data)
        self.assertEqual([r["text"] for r in found], ["Keep Claude unchanged\ngrant release v26.11.9"])
        self.assertEqual(pg.note_midturn("codex", self.data), [])
        session = state.read(pg.session_path("codex", SID))
        self.assertEqual(session["request_ts"], T0 + 4)
        self.assertFalse(session.get("human"))
        self.assertIsNone(self.grant(T0 + 5, consume=False))
        self.assertEqual([p["text"] for p in self.prompts()], [found[0]["text"]])
        self.assertTrue(self.prompts()[0]["pending"])
        # The established Claude queued-command parser keeps its contract.
        claude = self.root / "claude.jsonl"
        claude.write_text(json.dumps(queued("Keep the existing workflow", T0 + 6)) + "\n")
        pg.state.update(pg.session_path("claude", "s1"),
                        lambda s: s.update(turn_started=T0, transcript=str(claude)))
        self.assertEqual(pg.note_midturn("claude", {"session_id": "s1"})[0]["text"],
                         "Keep the existing workflow")

    def test_partial_records_and_rotation_recover_once_with_bounded_reads(self):
        self.rollout.typed("First request", "turn", T0 + 1)
        complete = self.rollout.path.read_bytes()
        head, tail = complete[:-1].rsplit(b"\n", 1)
        self.rollout.path.write_bytes(head + b"\n" + tail[:40])
        self.assertEqual(pg.note_midturn("codex", self.data), [])
        with self.rollout.path.open("ab") as f:
            f.write(tail[40:] + b"\n")
        self.assertEqual([r["text"] for r in pg.note_midturn("codex", self.data)], ["First request"])
        # Replace the file with a larger rollout: inode, not just size, detects rotation.
        replacement = self.rollout.path.with_suffix(".new")
        replacement.write_bytes(complete + b" " * (pg.MIDTURN_READ + 20) + b"\n")
        replacement.replace(self.rollout.path)
        self.rollout.typed("After a large record", "turn", T0 + 2)
        found = []
        for _ in range(5):
            found.extend(pg.note_midturn("codex", self.data))
        self.assertEqual([r["text"] for r in found], ["After a large record"])
        self.assertEqual([p["text"] for p in self.prompts()], ["First request", "After a large record"])

    def test_unregistered_child_foreign_and_ambiguous_rollouts_cannot_supply_steering(self):
        self.rollout.typed("Own request", "turn", T0 + 1)
        other = Rollout(core.CODEX_HOME, OTHER)
        other.typed("Foreign request", "turn", T0 + 2)
        child = Rollout(core.CODEX_HOME, SUB, subagent_of=SID)
        child.typed("Copied request", "turn", T0 + 3, thread=SID)
        for data in (dict(self.data, agent_id="child"), dict(self.data, session_id="unregistered"),
                     dict(self.data, transcript_path=str(other.path)),
                     dict(self.data, transcript_path=str(child.path))):
            self.assertEqual(pg.note_midturn("codex", data), [])
        duplicate = Rollout(core.CODEX_HOME, SID, day="2026/11/10")
        self.assertEqual(pg.note_midturn("codex", self.data), [])
        duplicate.path.unlink()
        self.assertEqual([r["text"] for r in pg.note_midturn("codex", self.data)], ["Own request"])

    def test_post_tool_records_steering_before_continuity_even_when_command_failed_or_deferred(self):
        # Outcome settlement is an external harness boundary. The real request reader and
        # continuity persistence still run for success, failure, and unavailable results.
        observed = []
        def continuity_boundary(*args):
            observed.append([p["text"] for p in self.prompts()])
            return ""
        for i, outcome in enumerate((True, False, None), 1):
            text = "Request " + str(i)
            self.rollout.typed(text, "turn", T0 + i)
            data = dict(self.data, tool_name="Read", tool_input={}, tool_response={}, tool_use_id="call-" + str(i))
            with mock.patch.object(hooks, "STATE", self.root), \
                    mock.patch.object(hooks, "_codex_call", return_value=(outcome, "")), \
                    mock.patch.object(hooks, "_continuity", side_effect=continuity_boundary), \
                    mock.patch("sys.stdin", io.StringIO(json.dumps(data))), contextlib.redirect_stdout(io.StringIO()):
                hooks.post_tool("codex")
            self.assertTrue(self.prompts(), "steering must be persisted even without a successful command")
            self.assertEqual(self.prompts()[-1]["text"], text)
            if outcome is not False:
                self.assertIn(text, observed[-1])

    def test_first_hook_exposes_latest_request_while_backfill_keeps_older_constraints_once(self):
        for i in range(5):
            self.rollout.typed("Older constraint " + str(i), "turn", T0 + i + 1)
            with self.rollout.path.open("a") as f:
                f.write(json.dumps({"type": "response_item", "payload": {"type": "tool_output",
                              "text": "x" * (pg.MIDTURN_READ + 100)}}) + "\n")
        self.rollout.typed("Newest constraint", "turn", T0 + 10)
        first = pg.note_midturn("codex", self.data)
        self.assertIn("Newest constraint", [r["text"] for r in first])
        self.assertEqual(self.prompts()[-1]["text"], "Newest constraint")
        for _ in range(20):
            pg.note_midturn("codex", self.data)
        self.assertEqual([p["text"] for p in self.prompts()],
                         ["Older constraint 1", "Older constraint 2", "Older constraint 3", "Older constraint 4",
                          "Newest constraint"])
        session = state.read(pg.session_path("codex", SID))
        self.assertEqual(session["request_ts"], T0 + 10)
        self.assertFalse(session["codex_midturn_pending"])
        # Claude retains its existing arrival ordering; only Codex has asynchronous backfill.
        continuity.on_midturn("claude", "s1", "newer", T0 + 10)
        continuity.on_midturn("claude", "s1", "older", T0 + 1)
        legacy = state.read(continuity.record_path("claude", "s1"))["checkpoint"]["prompts"]
        self.assertEqual([p["text"] for p in legacy], ["newer", "older"])

    def test_compaction_immediately_after_steering_captures_the_request(self):
        self.rollout.typed("Preserve this before compacting", "turn", T0 + 1)
        data = dict(self.data, trigger="auto")
        with mock.patch.object(hooks, "STATE", self.root), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(data))), contextlib.redirect_stdout(io.StringIO()):
            hooks.pre_compact("codex")
        self.assertEqual([p["text"] for p in self.prompts()], ["Preserve this before compacting"])
        with mock.patch("sys.stdin", io.StringIO(json.dumps(data))), contextlib.redirect_stdout(io.StringIO()):
            hooks.pre_compact("codex")
        self.assertEqual(len(self.prompts()), 1)


if __name__ == "__main__":
    unittest.main()
