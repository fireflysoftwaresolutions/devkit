# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The crew gates see a Codex member's executed `dev progress step` as its proof. Codex records each command as it
ran, so its CommandExecution item carries the stamps devkit's own hooks put in front of it: the lane and session
variables (`DEVKIT_SESSION=codex:<thread> DEVKIT_FROM_AGENT=<agent id> dev …`, or the `export …;` form) and the call
token. Those are taken off before the command is read; any other assignment in front of `dev` (a PATH of its own)
still keeps it from counting, since it changes which program runs. A Claude Code transcript records the command as
the model wrote it, so its path is unchanged. Only a review step's close (and an --approves verdict) waits for the
gates' pass, so this showed on review lanes while a design or tooling step closed on its own call.

The rollout below has the shape of a Codex 0.167.2 member's own rollout (2026-11-19): a session_meta that is not a
fork (no forked_from_id, history_mode "paginated", source.subagent.thread_spawn), its turn_context, task_started, the
code-mode `exec` call whose script calls tools.exec_command, and the CommandExecution item of the command as run,
with its ordinal, thread_id, file:// folder and exit code.

Run: python3 -m unittest tests.test_gates_codex_proofs -v"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from devkit import gates  # noqa: E402

LEAD = "01a1f737-1701-7791-bb05-000000000001"
SERAPH = "01a1f737-3cd7-7ce1-b6fd-000000000002"
TURN = "01a1f737-3cdb-7402-bf18-000000000003"
STEP = "dev progress step 'review done' --step review --check '✓ read the diff, 12 tests pass'"
# each form devkit's hooks write in front of a member's dev command, as a Codex CommandExecution item records it
STAMPED = {
    "lane and session (26.11.25)": f"DEVKIT_SESSION=codex:{LEAD} DEVKIT_FROM_AGENT={SERAPH} {STEP}",
    "exported session": f"export DEVKIT_SESSION=codex:{LEAD}; DEVKIT_FROM_AGENT={SERAPH} {STEP}",
    "call token, exported": f"export DEVKIT_CALL=call_0642.Zt0q-2_mXy9kP4bS7nW1aA; readonly DEVKIT_CALL; "
                            f"DEVKIT_FROM_AGENT={SERAPH} {STEP}",
    "call token, plain": f"DEVKIT_CALL=call_0642.Zt0q-2_mXy9kP4bS7nW1aA DEVKIT_FROM_AGENT={SERAPH} {STEP}",
    "lane, then call token": f"DEVKIT_FROM_AGENT={SERAPH} DEVKIT_CALL=exec-01a1f737-5.iZj-U3exUAJI8QUjgQLDgA {STEP}",
}


class CodexMemberRollout:
    """A Codex 0.167.2 member's own rollout in `home`, for a member working in `repo`."""

    def __init__(self, home: Path, repo: Path):
        self.path = home / "sessions" / "2026" / "11" / "19" / f"rollout-2026-11-19T20-29-14-{SERAPH}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.repo, self.n = repo, 0
        self.add("session_meta", {
            "id": SERAPH, "session_id": LEAD, "parent_thread_id": LEAD, "timestamp": "2026-11-19T20:29:14.000Z",
            "cwd": str(repo), "originator": "codex_exec", "cli_version": "0.167.2", "thread_source": "subagent",
            "history_mode": "paginated", "multi_agent_version": "v2", "agent_path": "/root/review",
            "agent_nickname": "Tesla", "source": {"subagent": {"thread_spawn": {
                "parent_thread_id": LEAD, "depth": 1, "agent_path": "/root/review", "agent_nickname": "Tesla",
                "agent_role": None}}}})
        self.add("turn_context", {"turn_id": TURN, "cwd": str(repo), "approval_policy": "never",
                                  "sandbox_policy": {"type": "danger-full-access"}, "model": "gpt-6-luna"})
        self.add("event_msg", {"type": "task_started", "turn_id": TURN})

    def add(self, kind: str, payload: dict):
        with self.path.open("a") as fh:
            fh.write(json.dumps({"timestamp": "2026-11-19T20:29:19.000Z", "ordinal": self.n, "type": kind,
                                 "payload": payload}) + "\n")
        self.n += 1

    def ran(self, command: str, code: int = 0):
        """One command through code mode: the exec script that calls exec_command, then its CommandExecution item
        (the command as it ran) and the script's output."""
        call = f"call_{self.n:04d}"
        self.add("response_item", {"type": "custom_tool_call", "status": "completed", "call_id": call, "name": "exec",
                                   "input": f"text(await tools.exec_command({{cmd:{json.dumps(command)}}}))\n"})
        self.add("event_msg", {"type": "item_completed", "thread_id": SERAPH, "turn_id": TURN,
                               "started_at_ms": 1795148959000, "completed_at_ms": 1795148959300, "item": {
                                   "type": "CommandExecution", "id": f"exec-{self.n:08d}", "process_id": "99924",
                                   "command": ["/bin/zsh", "-lc", command], "cwd": f"file://{self.repo}",
                                   "parsed_cmd": [{"type": "unknown", "cmd": command}],
                                   "source": "unified_exec_startup", "status": "completed" if code == 0 else "failed",
                                   "aggregated_output": "✓ Seraph · review done\n", "exit_code": code,
                                   "duration": {"secs": 0, "nanos": 2208}}})
        self.add("response_item", {"type": "custom_tool_call_output", "call_id": call, "output": [
            {"type": "input_text", "text": "Script completed\nWall time 0.3 seconds\nOutput:\n"}]})
        return self


class CodexProofTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(os.path.realpath(tmp.name))
        self.repo = self.home / "api"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        tools = self.home / "bin"  # `dev` on PATH is devkit's own bin/dev, as on a Mac devkit installed
        tools.mkdir()
        (tools / "dev").symlink_to(ROOT / "bin" / "dev")
        path = mock.patch.dict(os.environ, {"PATH": str(tools) + os.pathsep + os.environ["PATH"]})
        path.start()
        self.addCleanup(path.stop)

    def proof_findings(self, command: str) -> list:
        rollout = CodexMemberRollout(self.home, self.repo).ran(command)
        found = gates.crew_gates("seraph", str(rollout.path), str(self.repo), agent="codex", session=LEAD,
                                 lane=SERAPH, successful=True)
        rollout.path.unlink()
        return [(f["rule"], f["item"]) for f in found if f["rule"] == "proofs"]

    def test_a_codex_members_dev_progress_step_counts_with_each_stamp_devkit_writes_in_front_of_it(self):
        for form, command in STAMPED.items():
            with self.subTest(form=form):
                self.assertEqual(self.proof_findings(command), [])

    def test_an_assignment_devkit_does_not_write_still_keeps_the_command_from_counting(self):
        # a PATH of its own changes which `dev` runs: only devkit's own stamps are taken off
        self.assertEqual(self.proof_findings(f"DEVKIT_SESSION=codex:{LEAD} DEVKIT_FROM_AGENT={SERAPH} {STEP}"), [])
        self.assertEqual(self.proof_findings(f"PATH=/tmp/elsewhere DEVKIT_FROM_AGENT={SERAPH} {STEP}"),
                         [("proofs", "dev progress step")])


if __name__ == "__main__":
    unittest.main()
