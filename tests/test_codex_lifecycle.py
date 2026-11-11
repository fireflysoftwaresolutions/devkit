# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Codex lifecycle replay contracts; these are not live-host acceptance results.

The SubAgentActivity shape was observed during a real native interrupt/resume on
2026-11-11 at 02:06 UTC. Tests keep all session and run records in a sandbox.
"""
import json
import contextlib
import io
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import approvals, codexlaunch, core, hooks, progress as pg  # noqa: E402
from test_progress import TempState  # noqa: E402
from test_codex_admission import setup_run  # noqa: E402


class CodexLifecycleTests(unittest.TestCase):
    def setUp(self):
        state = TempState().__enter__()
        self.addCleanup(state.__exit__, None, None, None)
        self.root = Path(state.tmp.name)
        self.run, self.now = setup_run("codex", ["tank", "trinity"], "neo", True)
        self.now = round(self.now, 3)  # native timestamps have millisecond resolution
        self.rollout = self.root / "parent.jsonl"
        self.rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": "s1"}}) + "\n")
        self.record = pg.session_path("codex", "s1")
        doc = json.loads(self.record.read_text())
        doc["transcript"] = str(self.rollout)
        self.record.write_text(json.dumps(doc))

    def event(self, kind, at, aid="member0", thread="s1"):
        stamp = datetime.fromtimestamp(at, timezone.utc).isoformat().replace("+00:00", "Z")
        with self.rollout.open("a") as out:
            out.write(json.dumps({"timestamp": stamp, "type": "event_msg", "payload": {
                "type": "item_completed", "thread_id": thread, "item": {
                    "type": "SubAgentActivity", "id": "call-" + kind,
                    "kind": kind, "agent_thread_id": aid, "agent_path": "/root/work0"}}}) + "\n")

    def test_native_interrupt_releases_the_owned_step_on_next_hook(self):
        self.event("interrupted", self.now)
        pg.reconcile_hook("codex", {"session_id": "s1"}, now=self.now + 1)
        run = pg.load_run("codex", "s1")
        self.assertEqual(run["agents"]["member0"]["state"], "stopped")
        self.assertEqual(run["plan"][0]["state"], "pending")
        self.assertEqual(run["plan"][0]["owner"], "main")

    def test_interaction_does_not_cancel_an_interrupt_or_admit_a_stopped_lane(self):
        self.event("interrupted", self.now)
        self.event("interacted", self.now + 1)
        # A late-written older stop must not replace the newer interruption.
        self.event("interrupted", self.now - 1)
        pg.reconcile_hook("codex", {"session_id": "s1"}, now=self.now + 2)
        run = pg.load_run("codex", "s1")
        self.assertEqual(run["agents"]["member0"]["state"], "stopped")
        self.assertEqual(run["plan"][0]["owner"], "main")
        self.event("interrupted", self.now + 3)
        pg.reconcile_hook("codex", {"session_id": "s1"}, now=self.now + 4)
        self.assertEqual(pg.load_run("codex", "s1")["agents"]["member0"]["state"], "stopped")
        self.event("interacted", self.now + 5)
        pg.reconcile_hook("codex", {"session_id": "s1"}, now=self.now + 6)
        # Sending a message alone grants neither a slot nor the step's ownership.
        self.assertEqual(pg.load_run("codex", "s1")["agents"]["member0"]["state"], "stopped")

    def test_foreign_thread_unknown_child_and_message_text_cannot_stop_a_lane(self):
        self.event("interrupted", self.now, thread="other")
        self.event("interrupted", self.now, aid="unknown")
        with self.rollout.open("a") as out:
            out.write(json.dumps({"type": "response_item", "payload": {
                "type": "message", "content": [{"type": "output_text", "text":
                'SubAgentActivity interrupted member0'}]}}) + "\n")
        pg.reconcile_hook("codex", {"session_id": "s1"}, now=self.now + 1)
        self.assertEqual(pg.load_run("codex", "s1")["agents"]["member0"]["state"], "running")
        self.event("interrupted", self.now)
        pg.reconcile_hook("claude", {"session_id": "s1", "transcript_path": str(self.rollout)},
                          now=self.now + 1)
        self.assertEqual(pg.load_run("codex", "s1")["agents"]["member0"]["state"], "running")
        pg.reconcile_hook("codex", {"session_id": "s1"}, now=self.now + 1)
        self.assertEqual(pg.load_run("codex", "s1")["agents"]["member0"]["state"], "stopped")

    def test_stale_and_future_interruptions_do_not_stop_current_work(self):
        self.event("interrupted", self.now - 60)
        pg.reconcile_hook("codex", {"session_id": "s1"}, now=self.now + 1)
        self.assertEqual(pg.load_run("codex", "s1")["agents"]["member0"]["state"], "running")
        self.event("interrupted", self.now + 60)
        pg.reconcile_hook("codex", {"session_id": "s1"}, now=self.now + 1)
        self.assertEqual(pg.load_run("codex", "s1")["agents"]["member0"]["state"], "running")
        # The same event becomes authoritative once it is no longer in the future.
        pg.reconcile_hook("codex", {"session_id": "s1"}, now=self.now + 61)
        self.assertEqual(pg.load_run("codex", "s1")["agents"]["member0"]["state"], "stopped")

    def test_native_interrupt_does_not_use_an_unregistered_payload_transcript(self):
        self.event("interrupted", self.now)
        doc = json.loads(self.record.read_text())
        doc.pop("transcript")
        self.record.write_text(json.dumps(doc))
        pg.reconcile_hook("codex", {"session_id": "s1", "transcript_path": str(self.rollout)},
                          now=self.now + 1)
        self.assertEqual(pg.load_run("codex", "s1")["agents"]["member0"]["state"], "running")
        doc["transcript"] = str(self.rollout)
        self.record.write_text(json.dumps(doc))
        pg.reconcile_hook("codex", {"session_id": "s1"}, now=self.now + 1)
        self.assertEqual(pg.load_run("codex", "s1")["agents"]["member0"]["state"], "stopped")

    def test_resume_after_snapshot_is_not_stopped_by_old_interruption(self):
        self.event("interrupted", self.now)
        snapshot = pg.load_run("codex", "s1")
        live = pg.load_run("codex", "s1")
        live["agents"]["member0"]["resumed_at"] = self.now + 1
        pg.run_path("codex", "s1").write_text(json.dumps(live))
        self.assertEqual(codexlaunch.reconcile_lifecycle(snapshot, json.loads(self.record.read_text()),
                                                       self.now + 2), [])
        self.assertEqual(pg.load_run("codex", "s1")["agents"]["member0"]["state"], "running")

    def test_interrupted_child_must_pass_admission_again_before_resuming(self):
        self.event("interrupted", self.now)
        pg.reconcile_hook("codex", {"session_id": "s1"}, now=self.now + 1)
        self.assertEqual(pg.load_run("codex", "s1")["agents"]["member0"]["state"], "stopped")
        self.event("interacted", self.now + 2)
        child = {"session_id": "s1", "agent_id": "member0", "agent_type": "tank"}
        with mock.patch.dict(core.CFG, {"progress": {"max_parallel": 1, "reserve": 0}}):
            pg.bind("codex", child, now=self.now + 3)
        denied = pg.load_run("codex", "s1")
        self.assertEqual(denied["agents"]["member0"]["state"], "stopped")
        self.assertTrue(denied["agents"]["member0"].get("waits_slot"))
        self.assertEqual(denied["plan"][0]["owner"], "main")
        with mock.patch.dict(core.CFG, {"progress": {"max_parallel": 2, "reserve": 0}}):
            pg.bind("codex", child, now=self.now + 4)
        resumed = pg.load_run("codex", "s1")
        self.assertEqual(resumed["agents"]["member0"]["state"], "running")
        self.assertFalse(resumed["agents"]["member0"].get("waits_slot"))
        self.assertEqual(resumed["plan"][0]["owner"], "member0")

    def test_followup_before_reconciliation_cannot_skip_readmission(self):
        self.event("interrupted", self.now)
        self.event("interacted", self.now + 1)
        child = {"session_id": "s1", "agent_id": "member0", "agent_type": "tank",
                 "hook_event_name": "PreToolUse", "tool_name": "apply_patch", "tool_use_id": "native-edit",
                 "cwd": str(self.root), "tool_input": {"input":
                 "*** Begin Patch\n*** Add File: feature.py\n+print('feature')\n*** End Patch"}}
        output = io.StringIO()
        with mock.patch.dict(core.CFG, {"progress": {"max_parallel": 1, "reserve": 0}}), \
                mock.patch.object(hooks, "STATE", self.root), \
                mock.patch.object(hooks.time, "time", return_value=self.now + 2), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(child))), contextlib.redirect_stdout(output):
            hooks.guard("codex")
        lane = pg.load_run("codex", "s1")["agents"]["member0"]
        self.assertEqual(lane["state"], "stopped")
        self.assertTrue(lane.get("waits_slot"))
        result = json.loads(output.getvalue())["hookSpecificOutput"]
        self.assertEqual(result["permissionDecision"], "deny")
        self.assertIn("read-only", result["permissionDecisionReason"])

    def test_rounded_observation_times_do_not_hide_a_later_native_interrupt(self):
        for field, fraction in (("last_seen", 0.07), ("updates", 0.57)):
            with self.subTest(field=field):
                event_at = float(int(self.now) + 1) + fraction
                observed_at = event_at - 0.01
                run = json.loads(json.dumps(self.run))
                lane = run["agents"]["member0"]
                if field == "last_seen":
                    lane["last_seen"] = round(observed_at, 1)
                else:
                    lane["updates"] = [{"ts": round(observed_at), "kind": "note", "text": "working"}]
                pg.run_path("codex", "s1").write_text(json.dumps(run))
                self.event("interrupted", event_at)
                pg.reconcile_hook("codex", {"session_id": "s1"}, now=event_at + 1)
                stopped = pg.load_run("codex", "s1")
                self.assertEqual(stopped["agents"]["member0"]["state"], "stopped")
                self.assertEqual(stopped["plan"][0]["owner"], "main")

    def test_precise_admission_just_after_an_interrupt_protects_new_work(self):
        event_at = float(int(self.now) + 1) + 0.07
        self.event("interrupted", event_at)
        for field in ("started", "resumed_at"):
            with self.subTest(field=field):
                run = json.loads(json.dumps(self.run))
                run["agents"]["member0"][field] = event_at + 0.001
                pg.run_path("codex", "s1").write_text(json.dumps(run))
                pg.reconcile_hook("codex", {"session_id": "s1"}, now=event_at + 1)
                self.assertEqual(pg.load_run("codex", "s1")["agents"]["member0"]["state"], "running")
        self.event("interrupted", event_at + 0.002)
        pg.reconcile_hook("codex", {"session_id": "s1"}, now=event_at + 1)
        self.assertEqual(pg.load_run("codex", "s1")["agents"]["member0"]["state"], "stopped")

    def test_recovered_codex_lane_keeps_precise_admission_for_the_next_stop(self):
        event_at = float(int(self.now) + 1) + 0.07
        recovered_at = event_at - 0.01
        self.event("interrupted", event_at)
        run = pg.load_run("codex", "s1")
        run["agents"]["member0"]["state"] = "lost"
        move = approvals._lane_move("member0", "lost", "running", "seen again", at=recovered_at)
        approvals.apply(run, [move], recovered_at)
        pg.run_path("codex", "s1").write_text(json.dumps(run))
        pg.reconcile_hook("codex", {"session_id": "s1"}, now=event_at + 1)
        self.assertEqual(pg.load_run("codex", "s1")["agents"]["member0"]["state"], "stopped")

        # The shared reconciler preserves Claude's established display precision.
        run["agent"] = "claude"
        run["agents"]["member0"]["state"] = "lost"
        fields_before = set(run["agents"]["member0"])
        approvals.apply(run, [move], recovered_at)
        lane = run["agents"]["member0"]
        self.assertEqual(lane["resumed_at"], round(recovered_at, 1))
        self.assertEqual(lane["recovered"], round(recovered_at, 1))
        self.assertEqual(set(lane), fields_before)


if __name__ == "__main__":
    unittest.main()
