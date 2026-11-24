# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# What the tech lead's session learns from the transcript: a message the developer types while the session works
# (Claude Code records it there as a queued command; its prompt hook carries the running turn's prompt id, not the
# message's own), and an answer the tech lead sends a member with SendMessage instead of `dev crew answer`. Driven
# through the hooks as Claude Code fires them, with a transcript written the way Claude Code writes it.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_watch_requests
import contextlib
import io
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _hermetic  # noqa: E402

_hermetic.activate()  # a probe that imports this module by path gets the suite's temporary home too

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
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
            hooks.run("post-tool", "claude")
        return out.getvalue()

    def launch_advice(self, label, kind) -> list:
        # the launch's advice lines from the crew's slots ([] when the launch fits)
        data = {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_use_id": "toolu_l",
                "tool_input": {"description": label, "subagent_type": kind, "prompt": "go"},
                "cwd": str(self.root), "transcript_path": str(self.transcript)}
        return hooks._crew_limits_advice("claude", data)


class MidTurnRequestTests(RequestCase):
    def test_a_step_assigned_after_a_mid_turn_message_may_take_the_slot_kept_for_the_developer(self):
        # 26.11.6 never saw the message (it read none from the transcript), so the docs launch the developer had
        # just asked for counted as queued plan work and was refused the reserved slot
        self.assertTrue(self.launch_advice("Ghost: docs", "devkit:ghost"))  # plan work: the slot stays kept
        self.append(queued("Write the docs now, before the API is done", self.now - 120))
        pg.assign("docs", "Ghost", why="the developer asked for it now", now=self.now - 60)
        self.assertEqual(self.launch_advice("Ghost: docs", "devkit:ghost"), [])

    def test_only_the_developers_own_queued_message_is_a_request_never_a_task_notification(self):
        self.append(queued("<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n"
                           "</task-notification>", self.now - 120, human=False))
        pg.assign("docs", "Ghost", why="test", now=self.now - 60)
        self.post_tool("t1")
        self.assertTrue(self.launch_advice("Ghost: docs", "devkit:ghost"))  # a notification asks for nothing
        self.append(queued("Start the docs", self.now - 40))
        pg.assign("docs", "Ghost", why="the developer asked", now=self.now - 30)
        self.post_tool("t2")
        self.assertEqual(self.launch_advice("Ghost: docs", "devkit:ghost"), [])

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


class LongTranscriptLineTests(unittest.TestCase):
    # one transcript line longer than a read (a screenshot, base64) held the reading position, so every message the
    # developer typed after it, mid-turn, was never seen and every hook re-read the same bytes
    def test_a_message_after_a_line_longer_than_a_read_is_still_found(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.jsonl"
            long = json.dumps({"type": "user", "message": {"content": "x" * (pg.MIDTURN_READ + 100_000)}})
            path.write_text(long + "\n" + json.dumps(queued("Release it after the review", 1_700_000_000.0)) + "\n")
            found, offset = pg.midturn_requests(str(path), 0)
            for _ in range(3):  # one hook per call: the message arrives within a few hooks, never stuck
                if found:
                    break
                found, offset = pg.midturn_requests(str(path), offset)
            self.assertEqual([m["text"] for m in found], ["Release it after the review"])


class SendMessageAnswerTests(RequestCase):
    def test_sendmessage_to_a_member_with_an_open_question_answers_it_via_sendmessage(self):
        # 26.11.6 did not see SendMessage at all: the question stayed open, on the developer's list, until the run
        # ended
        pg.crew_ask("May I rename the column?", from_agent="a_tank", now=self.now - 200)
        out = io.StringIO()
        payload = {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "SendMessage",
                   "tool_use_id": "toolu_s", "tool_input": {"to": "a_tank", "message": "M1: yes, rename it to amount"},
                   "tool_response": {"success": True}, "cwd": str(self.root)}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
            hooks.run("post-tool", "claude")
        run = pg.load_run("claude", "s1")
        q = next(m for m in run["messages"] if m["id"] == "M1")
        self.assertEqual((q.get("answered"), q.get("via")), (True, "SendMessage"))
        self.assertEqual(pg.waiting_messages(run), [])
        answer = next(m for m in run["messages"] if m.get("reply_to") == "M1")
        self.assertEqual((answer["text"], answer["to"], answer["delivered"]),
                         ("M1: yes, rename it to amount", "a_tank", True))  # SendMessage delivered it already
        rows = [json.loads(ln) for f in sorted(audit.AUDIT.glob("*.jsonl")) for ln in f.read_text().splitlines()]
        done = [r for r in rows if r.get("action") == "crew.message-answered"]
        self.assertEqual(len(done), 1, rows[-3:])
        self.assertIn("SendMessage", json.dumps(done[0]))

    def send(self, to, text):
        return pg.answered_by_sendmessage("claude", {"session_id": "s1", "tool_input": {"to": to, "message": text},
                                                     "tool_response": {"success": True}}, now=self.now)

    def open_ids(self):
        return sorted(m["id"] for m in pg.waiting_messages(pg.load_run("claude", "s1")))

    def test_a_name_two_lanes_answer_to_closes_neither_lanes_questions(self):
        # the name matched both Backend engineer lanes: both questions closed, and the answer reached only one
        launch("a_tank2", "devkit:tank", "Tank: migration", self.now - 300)
        pg.crew_ask("May I rename the column?", from_agent="a_tank", now=self.now - 200)
        pg.crew_ask("May I drop the index?", from_agent="a_tank2", now=self.now - 100)
        self.assertEqual(self.send("tank", "Yes"), [])
        self.assertEqual(self.open_ids(), ["M1", "M2"])
        self.assertEqual(self.send("a_tank2", "M2: yes, drop it"), ["M2"])
        self.assertEqual(self.open_ids(), ["M1"])

    def test_a_message_that_names_no_question_answers_none_and_tells_the_lead(self):
        # kit-consulting: the tech lead sent a member a new work item with SendMessage, devkit took it for the answer
        # to the member's open question, and the member's `dev watch --wait --for me` said none of its questions waited
        pg.crew_ask("May I change hooks/turn.mjs?", from_agent="a_tank", now=self.now - 200)
        self.assertEqual(self.send("a_tank", "One more item after item 1: every logo Claude supplies is a proposal."), [])
        self.assertEqual(self.open_ids(), ["M1"])
        told = [m["text"] for m in pg.load_run("claude", "s1")["messages"]
                if m.get("to") == "main" and m.get("from") == "devkit"]
        self.assertTrue(any("M1 still waits" in t and 'dev crew answer M1 "' in t for t in told), told)

    def test_a_message_that_names_a_question_answers_only_that_one(self):
        pg.crew_ask("May I rename the column?", from_agent="a_tank", now=self.now - 200)
        pg.crew_ask("Should the export skip empty rows?", from_agent="a_tank", now=self.now - 100)
        self.assertEqual(self.send("a_tank", "M2: yes, skip them"), ["M2"])
        self.assertEqual(self.open_ids(), ["M1"])


class MemberWaitRouteTests(RequestCase):
    # A member's `dev watch --wait --for me` must take the member's own events, never the tech lead's, in the foreground
    # and in the background (a background call leaves the ledger as soon as it starts).
    def pre(self, cmd, background, mode=None, tid="toolu_w"):
        out = io.StringIO()
        inp = dict({"command": cmd}, **({"run_in_background": True} if background else {}))
        payload = {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash", "agent_id": "a_tank",
                   "tool_use_id": tid, "tool_input": inp, "cwd": str(self.root),
                   **({"permission_mode": mode} if mode else {})}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
            hooks.run("pre-tool", "claude")
        text = out.getvalue().strip()
        return payload, ((json.loads(text) if text else {}).get("hookSpecificOutput") or {}).get("updatedInput") or {}

    def test_a_members_plain_wait_goes_out_as_written_and_its_ledger_names_its_lane(self):
        # where Claude Code checks the developer's rules the wait goes out exactly as typed (so an allow rule for
        # `dev watch` matches it); its lane rides on the text pin: the ledger entry names the member, and a background
        # call's record is kept past its close for the wait's dev command (identity.keep_background)
        from devkit import identity
        for background in (False, True):
            with self.subTest(background=background):
                tid = f"toolu_w{int(background)}"
                data, got = self.pre("dev watch --wait --for me --timeout 9", background, tid=tid)
                self.assertEqual(got, {})
                entry = pg.call_entry(data)
                self.assertEqual((entry.get("caller"), entry.get("pin_by"), bool(entry.get("background"))),
                                 ("a_tank", "text", background))
                hooks._close_call(dict(data, hook_event_name="PostToolUse"))
                self.assertEqual(pg.call_entry(data), {})
                kept = identity._kept_background("s1", time.time())
                self.assertEqual([(e.get("tool_use_id"), e.get("caller")) for e in kept],
                                 [(tid, "a_tank")] if background else [])

    def test_a_members_wait_where_the_payload_names_bypass_carries_its_lane_in_front(self):
        # bypassPermissions: every call carries its token, and a member's own wait its lane variable right before dev
        for background in (False, True):
            with self.subTest(background=background):
                _, got = self.pre("dev watch --wait --for me --timeout 9", background, "bypassPermissions",
                                  tid=f"toolu_b{int(background)}")
                self.assertRegex(got.get("command") or "",
                                 rf"; export DEVKIT_CALL=toolu_b{int(background)}\.[A-Za-z0-9_-]{{22}}; readonly DEVKIT_CALL; "
                                 r"DEVKIT_FROM_AGENT=a_tank dev watch --wait --for me --timeout 9\Z")
                self.assertEqual(got.get("run_in_background", False), background)

    def test_only_a_wait_of_its_own_is_pinned_or_routed(self):
        for mode in (None, "bypassPermissions"):
            with self.subTest(mode=mode):
                data, got = self.pre("dev watch --wait; rm -rf build", False, mode, tid=f"toolu_c{mode}")
                self.assertNotIn("DEVKIT_FROM_AGENT", got.get("command") or "")
                self.assertNotEqual(pg.call_entry(data).get("pin_by"), "text")


# The end-to-end check, with real processes: a stand-in for Claude Code's agent process (its argv[0] is `claude`)
# holds a devkit session in a home of its own, fires the real PreToolUse and PostToolUse hooks for a member's
# `dev watch --wait --for me --timeout 9` in a mode that checks the developer's rules, and runs the call's shell in
# Claude Code's form (`zsh -c … eval '<command>' < /dev/null && pwd -P >| …`). Its `dev` starts once the stand-in says
# go: for the background call, after the call's PostToolUse took it out of the ledger.
STAND_IN = r"""
import json, os, subprocess, sys, time
from unittest import mock
W, OUT = sys.argv[1:3]
sys.path.insert(0, os.path.join(W, "lib"))
from devkit import progress as pg, watch
SID, CMD, now = "se2e", "dev watch --wait --for me --timeout 9", time.time()
pg.SESSIONS.mkdir(parents=True, exist_ok=True)
(pg.SESSIONS / f"claude-{SID}.json").write_text(json.dumps({"agent": "claude", "session": SID, "started": now - 60,
                                                            "pid": os.getpid(), "cwd": OUT}))
with mock.patch.object(pg, "caller", lambda *a, **k: {"caller": "main", "how": "test"}), \
        mock.patch.object(pg, "resolve_session", lambda *a, **k: ("claude", SID, "ancestry")):
    pg.start("Work", plan="api=30m", now=now - 600)
    pg.assign("api", "Tank", why="the test's assignment", now=now - 590)
    pg.note_agent_launch("claude", {"session_id": SID, "tool_use_id": "toolu_a_tank",
                                    "tool_input": {"description": "Tank: api", "subagent_type": "devkit:tank"}},
                         now=now - 580)
    pg.on_subagent("claude", {"session_id": SID, "agent_id": "a_tank", "agent_type": "devkit:tank"}, True, now=now - 580)
    pg.launch_result("claude", {"session_id": SID, "tool_use_id": "toolu_a_tank", "tool_response": {"agentId": "a_tank"}},
                     now=now - 580)
dev = [sys.executable, "-I", "-S", os.path.join(W, "bin", "dev")]
def hook(event, payload):
    return subprocess.run(dev + ["hook", event, "--agent", "claude"], input=json.dumps(payload), capture_output=True,
                          text=True, timeout=120).stdout
result = {}
for mode in ("fg", "bg"):
    tid, go = f"toolu_{mode}", os.path.join(OUT, f"go-{mode}")
    pre = {"session_id": SID, "tool_use_id": tid, "tool_name": "Bash", "hook_event_name": "PreToolUse", "cwd": OUT,
           "agent_id": "a_tank", "permission_mode": "default",
           "tool_input": {"command": CMD, **({"run_in_background": True} if mode == "bg" else {})}}
    rewritten = "updatedInput" in hook("pre-tool", pre)
    env = {k: v for k, v in os.environ.items() if not k.startswith("DEVKIT_") or k == "DEVKIT_TEST_PATH"}
    env.update(PATH=os.path.join(OUT, "bin") + os.pathsep + env.get("PATH", ""), CLAUDECODE="1", E2E_GO=go,
               E2E_PY=sys.executable, E2E_DEV=dev[-1])
    script = "export DEVKIT_AGENT=claude\n: && eval '" + CMD + "' < /dev/null && pwd -P >| " + os.path.join(OUT, "cwd")
    shell = subprocess.Popen(["/bin/zsh", "-c", script], env=env, cwd=OUT, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
    post = dict(pre, hook_event_name="PostToolUse", tool_response={"stdout": ""})
    if mode == "bg":
        hook("post-tool", post)
    left = not pg.call_entry(pre)
    pg._ask_into("claude", SID, f"FOR-MAIN-{mode}: may I rename the column?", None, "a_tank", time.time())
    pg._ask_into("claude", SID, f"FOR-TANK-{mode}: update the docs too", "a_tank", "main", time.time(), kind="notice")
    open(go, "w").close()
    out, _ = shell.communicate(timeout=120)
    if mode == "fg":
        hook("post-tool", post)
    result[mode] = {"rewritten": rewritten, "left_ledger": left, "rc": shell.returncode, "out": out}
result["due_main"] = [e["line"] for e in watch.pending("claude", SID, "main")]
result["due_tank"] = [e["line"] for e in watch.pending("claude", SID, "a_tank")]
print("RESULT " + json.dumps(result))
"""

DEV_GATE = "#!/bin/sh\nwhile [ ! -f \"$E2E_GO\" ]; do sleep 0.05; done\nexec \"$E2E_PY\" -I -S \"$E2E_DEV\" \"$@\"\n"


def interpreter() -> str:
    # the interpreter's own binary: a macOS framework build's python3 starts another binary and drops argv[0]
    if sys.platform != "darwin":
        return sys.executable
    import subprocess
    path = subprocess.run(["ps", "-o", "comm=", "-p", str(os.getpid())], capture_output=True, text=True,
                          timeout=10).stdout.strip()
    return path if os.path.isabs(path) and os.access(path, os.X_OK) else sys.executable


class MemberWaitEndToEndTests(unittest.TestCase):
    def test_a_members_plain_wait_takes_its_own_events_in_the_foreground_and_the_background(self):
        import subprocess
        import tempfile
        import _sandbox
        tmp = Path(tempfile.mkdtemp(prefix="devkit-e2e-wait-"))
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        (tmp / "home").mkdir()
        (tmp / "tmp").mkdir()
        (tmp / "bin").mkdir()
        gate = tmp / "bin" / "dev"
        gate.write_text(DEV_GATE)
        gate.chmod(0o700)
        script = tmp / "stand_in.py"
        script.write_text(STAND_IN)
        env = {k: v for k, v in _sandbox.env().items() if not k.startswith(("DEVKIT_", "CLAUDE", "CODEX"))
               or k in ("DEVKIT_TEST_PATH", "DEVKIT_NO_NOTIFY")}
        env.update(HOME=str(tmp / "home"), TMPDIR=str(tmp / "tmp"))
        tree = str(Path(__file__).resolve().parents[1])
        run = subprocess.run(["claude", "-I", str(script), tree, str(tmp)], executable=interpreter(), env=env,
                             cwd=str(tmp), capture_output=True, text=True, timeout=300)
        line = next((ln for ln in run.stdout.splitlines() if ln.startswith("RESULT ")), "")
        self.assertTrue(line, run.stdout[-2000:] + run.stderr[-2000:])
        got = json.loads(line[len("RESULT "):])
        for mode in ("fg", "bg"):
            with self.subTest(mode=mode):
                r = got[mode]
                self.assertEqual((r["rewritten"], r["left_ledger"], r["rc"]), (False, mode == "bg", 0), r["out"])
                self.assertIn(f"FOR-TANK-{mode}", r["out"])
                self.assertNotIn("FOR-MAIN", r["out"])
        self.assertEqual(got["due_tank"], [])
        self.assertEqual(sorted(ln.split("“")[1][:11] for ln in got["due_main"]), ["FOR-MAIN-bg", "FOR-MAIN-fg"])


if __name__ == "__main__":
    unittest.main()
