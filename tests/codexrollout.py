# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# A Codex thread for the tests that read Codex's own records: the thread's session record as the hooks write it, and
# its rollout as Codex 0.167 writes it (~/.codex/sessions/YYYY/MM/DD/rollout-<time>-<thread id>.jsonl), with the
# developer's prompts (an `event_msg` `item_completed` UserMessage), agent messages, tool output, injected context and
# AGENTS.md blocks. The record shapes are cut from real Codex 0.167.0 rollouts (2026-11-09: the parity spike's hook
# capture and its scout subagent). A helper module, not a test module: the tests that use it import it.
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import core  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_progress import TempState  # noqa: E402

# made-up thread ids, UUIDv7 like Codex's own
SID = "01a1c19a-3b2c-7d4e-8f50-61728394a5b6"  # the Codex thread the developer types in
SUB = "01a1c19a-7e8f-7a01-9b12-c3d4e5f60718"  # a subagent that thread spawned
OTHER = "01a1c19b-1a2b-7c3d-8e4f-5a6b7c8d9e0f"  # another Codex session on this Mac
T0 = 1_800_000_000.0


def iso(at):
    # a rollout record's time as Codex writes it: UTC, milliseconds, "Z"
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(at)) + f".{round(at * 1000) % 1000:03d}Z"


class Rollout:
    # A Codex 0.167 rollout: one JSON record per line, each with its time and ordinal, opening with the session_meta
    # of the thread it belongs to. `subagent_of` makes it a subagent's (its meta names the parent, and the copied
    # history of the parent comes before its own records); `meta_id` writes another thread's meta into this file.
    def __init__(self, home, sid, at=T0, archived=False, day="2026/11/09", subagent_of="", meta_id="", **meta):
        folder = Path(home) / "archived_sessions" if archived else Path(home) / "sessions" / day
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / f"rollout-2026-11-09T10-00-39-{sid}.jsonl"
        self.sid, self.n = sid, 0
        m = {"id": meta_id or sid, "session_id": subagent_of or sid, "timestamp": iso(at), "cwd": "/repo",
             "originator": "codex-tui", "cli_version": "0.167.0", "source": "cli", "forked_from_id": None,
             "base_instructions": {"text": "You are Codex."}}
        if subagent_of:
            m.update(source={"subagent": {"thread_spawn": {"parent_thread_id": subagent_of, "depth": 1,
                                                           "agent_path": "/root/list_files",
                                                           "agent_nickname": "Halley", "agent_role": "scout"}}},
                     forked_from_id=subagent_of, parent_thread_id=subagent_of, agent_role="scout",
                     subagent_history_start_ordinal=0)
        m.update(meta)
        self.add("session_meta", m, at)

    def add(self, kind, payload, at):
        with self.path.open("a") as fh:
            fh.write(json.dumps({"timestamp": iso(at), "ordinal": self.n, "type": kind, "payload": payload}) + "\n")
        self.n += 1

    def message(self, role, text, turn, at, kinds, provenance):
        self.add("response_item", {"type": "message", "id": f"msg_{self.n}", "role": role,
                                   "content": [{"type": "input_text", "text": text}],
                                   "internal_chat_message_metadata_passthrough": {
                                       "turn_id": turn, "create_time": at, "content_item_kinds": [kinds],
                                       "content_item_metadata": [{"provenance": provenance}]}}, at)

    # the developer's prompt: the model's input item, then the UserMessage item of the thread's turn
    def typed(self, text, turn, at, thread=""):
        self.message("user", text, turn, at, "user.text", {"type": "user"})
        self.add("event_msg", {"type": "item_completed", "thread_id": thread or self.sid, "turn_id": turn,
                               "item": {"type": "UserMessage", "id": f"um-{self.n}",
                                        "content": [{"type": "text", "text": text, "text_elements": []}]},
                               "started_at_ms": int(at * 1000), "completed_at_ms": int(at * 1000)}, at)

    def agent_said(self, text, turn, at):
        self.add("event_msg", {"type": "item_completed", "thread_id": self.sid, "turn_id": turn,
                               "item": {"type": "AgentMessage", "id": f"msg_{self.n}", "phase": "final_answer",
                                        "content": [{"type": "Text", "text": text}]}}, at)
        self.add("response_item", {"type": "message", "role": "assistant", "status": "completed",
                                   "content": [{"type": "output_text", "text": text}]}, at)

    def tool_output(self, text, turn, at):
        self.add("event_msg", {"type": "item_completed", "thread_id": self.sid, "turn_id": turn,
                               "item": {"type": "CommandExecution", "id": f"exec-{self.n}", "exit_code": 0,
                                        "status": "completed", "command": ["/bin/zsh", "-lc", "cat notes.txt"],
                                        "aggregated_output": text}}, at)
        self.add("response_item", {"type": "custom_tool_call_output", "call_id": f"call_{self.n}",
                                   "output": [{"type": "input_text", "text": text}]}, at)

    def injected(self, text, turn, at):  # a hook's additional context, as Codex records it
        self.message("developer", text, turn, at, "hooks.additional_context", {"type": "harness", "hook": True})

    def agents_md(self, text, turn, at):  # the AGENTS.md block Codex sends in the user's role
        self.message("user", f"# AGENTS.md instructions\n\n<INSTRUCTIONS>\n{text}\n</INSTRUCTIONS>", turn, at,
                     "agents_md.instructions", {"type": "agents_md"})


class CodexCase(unittest.TestCase):
    # devkit's state in a temporary folder, Codex's home below it, the thread SID registered and its rollout begun
    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.root = Path(self.state.tmp.name)
        p = mock.patch.object(core, "CODEX_HOME", self.root / ".codex")
        p.start()
        self.addCleanup(p.stop)
        os.environ["DEVKIT_SESSION"] = f"codex:{SID}"  # TempState puts the previous value back
        self.session(SID)
        self.rollout = Rollout(core.CODEX_HOME, SID)

    def session(self, sid, **more):
        # a Codex thread's record as the hooks write it: no pid (Codex threads share their process)
        rec = dict({"agent": "codex", "session": sid, "started": T0, "project": "acme/api", "surface": "codex-tui"},
                   **more)
        (pg.SESSIONS / f"codex-{sid}.json").write_text(json.dumps(rec))
