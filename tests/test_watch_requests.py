# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# What the tech lead's session learns outside UserPromptSubmit: a message the developer types while the session works
# (Claude Code records it in the transcript as a queued command, and runs no prompt hook for it), and an answer the
# tech lead sends a member with SendMessage instead of `dev crew answer`. Driven through the hooks as Claude Code
# fires them, with a transcript written the way Claude Code writes it.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_watch_requests
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

from devkit import audit, continuity, core, hooks, progress as pg, state  # noqa: E402
from test_progress import TempState, launch  # noqa: E402


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + ".000Z"


def queued(text: str, at: float, human: bool = True) -> dict:
    # Claude Code's record of a message typed while the session works: an attachment of type queued_command
    if human:
        att = {"type": "queued_command", "prompt": text, "commandMode": "prompt", "origin": {"kind": "human"},
               "timestamp": iso(at), "humanTurn": True}
    else:
        att = {"type": "queued_command", "prompt": text, "commandMode": "task-notification",
               "origin": {"kind": "task-notification"}, "timestamp": iso(at)}
    return {"type": "attachment", "timestamp": iso(at), "attachment": att}


class RequestCase(unittest.TestCase):
    # The tech lead's turn started an hour into a run whose plan has an API step (the Backend engineer works on it)
    # and a docs step assigned to the Technical writer; two crew slots, the last one kept for the developer.
    def setUp(self):
        self.ts = TempState().__enter__()
        self.addCleanup(self.ts.__exit__, None, None, None)
        self.root = Path(self.ts.tmp.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(hooks, "STATE", self.root))
        self.stack.enter_context(mock.patch.dict(core.CFG, {"progress": {"max_parallel": 2}}))
        self.now = time.time()
        pg.start("Work", plan="api=30m; docs=30m", now=self.now - 3600)
        pg.assign("api", "Tank", why="test", now=self.now - 3500)
        pg.assign("docs", "Ghost", why="test", now=self.now - 3500)
        launch("a_tank", "devkit:tank", "Tank: api", self.now - 3400)
        self.transcript = self.root / "t.jsonl"
        self.transcript.write_text(json.dumps({"type": "user", "timestamp": iso(self.now - 600),
                                               "message": {"role": "user", "content": "carry on"}}) + "\n")
        pg.state.update(pg.session_path("claude", "s1"),
                        lambda s: s.update(turn_started=self.now - 600, transcript=str(self.transcript)))

    def append(self, *records, partial: str = ""):
        with self.transcript.open("a") as fh:
            fh.write("".join(json.dumps(r) + "\n" for r in records) + partial)

    def post_tool(self, tid="toolu_b"):
        out = io.StringIO()
        payload = {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_use_id": tid,
                   "tool_input": {"command": "git status"}, "tool_response": {"stdout": ""}, "cwd": str(self.root),
                   "transcript_path": str(self.transcript)}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out), \
                mock.patch("devkit.guard.check", lambda *a, **k: None):
            hooks.run("post-tool", "claude")
        return out.getvalue()

    def launch_verdict(self, label, kind):
        data = {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_use_id": "toolu_l",
                "tool_input": {"description": label, "subagent_type": kind, "prompt": "go"},
                "cwd": str(self.root), "transcript_path": str(self.transcript)}
        return hooks._crew_limits("claude", data)


class MidTurnRequestTests(RequestCase):
    def test_a_step_assigned_after_a_mid_turn_message_may_take_the_slot_kept_for_the_developer(self):
        # 26.11.6 never saw the message (no UserPromptSubmit for it), so the docs launch the developer had just asked
        # for counted as queued plan work and was refused the reserved slot
        self.assertIsNotNone(self.launch_verdict("Ghost: docs", "devkit:ghost"))  # plan work: the slot stays kept
        self.append(queued("Write the docs now, before the API is done", self.now - 120))
        pg.assign("docs", "Ghost", why="the developer asked for it now", now=self.now - 60)
        self.assertIsNone(self.launch_verdict("Ghost: docs", "devkit:ghost"))

    def test_a_queued_task_notification_is_no_request_of_the_developer(self):
        self.append(queued("<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n"
                           "</task-notification>", self.now - 120, human=False))
        pg.assign("docs", "Ghost", why="test", now=self.now - 60)
        self.post_tool()
        self.assertIsNotNone(self.launch_verdict("Ghost: docs", "devkit:ghost"))

    def test_a_cut_line_is_read_once_it_is_whole_and_a_rotated_transcript_is_read_from_its_start(self):
        # the reader keeps an offset in the session record: a line half written when a hook ran is read at the next
        # one, whole and once; a transcript that became shorter (rotated) is read again from its start
        line = json.dumps(queued("Also bump the version", self.now - 100)) + "\n"
        self.append(partial=line[:40])
        self.post_tool("t1")
        self.assertIsNone(pg.state.read(pg.session_path("claude", "s1")).get("request_ts"))
        with self.transcript.open("a") as fh:
            fh.write(line[40:])
        self.post_tool("t2")
        self.post_tool("t3")
        sess = pg.state.read(pg.session_path("claude", "s1"))
        self.assertAlmostEqual(sess.get("request_ts") or 0, self.now - 100, delta=1)
        prompts = [p["text"] for p in (state.read(continuity.record_path("claude", "s1")) or {}).get(
            "checkpoint", {}).get("prompts", [])]
        self.assertEqual(prompts.count("Also bump the version"), 1, prompts)
        self.transcript.write_text(json.dumps(queued("Then tag it", self.now - 30)) + "\n")  # rotated: shorter
        self.post_tool("t4")
        sess = pg.state.read(pg.session_path("claude", "s1"))
        self.assertAlmostEqual(sess.get("request_ts") or 0, self.now - 30, delta=1)


class SendMessageAnswerTests(RequestCase):
    def test_sendmessage_to_a_member_with_an_open_question_answers_it_via_sendmessage(self):
        # 26.11.6 did not see SendMessage at all: the question stayed open, on the developer's list, until the run
        # ended
        pg.crew_ask("May I rename the column?", from_agent="a_tank", now=self.now - 200)
        out = io.StringIO()
        payload = {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "SendMessage",
                   "tool_use_id": "toolu_s", "tool_input": {"to": "a_tank", "message": "Yes, rename it to amount"},
                   "tool_response": {"success": True}, "cwd": str(self.root)}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
            hooks.run("post-tool", "claude")
        run = pg.load_run("claude", "s1")
        q = next(m for m in run["messages"] if m["id"] == "M1")
        self.assertEqual((q.get("answered"), q.get("via")), (True, "SendMessage"))
        self.assertEqual(pg.waiting_messages(run), [])
        answer = next(m for m in run["messages"] if m.get("reply_to") == "M1")
        self.assertEqual((answer["text"], answer["to"], answer["delivered"]),
                         ("Yes, rename it to amount", "a_tank", True))  # SendMessage delivered it already
        rows = [json.loads(ln) for f in sorted(audit.AUDIT.glob("*.jsonl")) for ln in f.read_text().splitlines()]
        done = [r for r in rows if r.get("action") == "crew.message-answered"]
        self.assertEqual(len(done), 1, rows[-3:])
        self.assertIn("SendMessage", json.dumps(done[0]))


class MemberWaitRouteTests(RequestCase):
    def test_a_members_wait_names_its_own_lane_even_in_the_background(self):
        # a background command's call leaves the ledger as soon as it starts, so a member's background wait found no
        # caller, counted as the main session and could take the tech lead's events; the PreToolUse hook gives each
        # member's `dev watch` call its lane, as it does its `dev crew` calls
        def pre(cmd, background):
            out = io.StringIO()
            inp = dict({"command": cmd}, **({"run_in_background": True} if background else {}))
            payload = {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash", "agent_id": "a_tank",
                       "tool_use_id": "toolu_w", "tool_input": inp, "cwd": str(self.root)}
            with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
                hooks.run("guard", "claude")
            text = out.getvalue().strip()
            return ((json.loads(text) if text else {}).get("hookSpecificOutput") or {}).get("updatedInput") or {}
        for background in (False, True):
            with self.subTest(background=background):
                got = pre("dev watch --wait --for me --timeout 9", background)
                self.assertEqual(got.get("command"), "DEVKIT_FROM_AGENT=a_tank dev watch --wait --for me --timeout 9")
                self.assertEqual(got.get("run_in_background", False), background)
        self.assertEqual(pre("dev watch --wait; rm -rf build", False), {})  # only a call of its own is routed


if __name__ == "__main__":
    unittest.main()
