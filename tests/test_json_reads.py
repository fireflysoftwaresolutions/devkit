# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""devkit parses JSON that a model, a teammate, another Mac or another session wrote with core.load_json, which reads a
value nested deeper than core.JSON_MAX_DEPTH as unreadable. A transcript, a Codex rollout and a hook's input hold
JSON built from a model's tool input, so a tool input nested a few thousand levels deep reached the parser: Python 3.9
to 3.13 raise RecursionError at about a thousand levels, Python 3.15 at about 150,000, and the readers caught only
ValueError, so the Stop hook and the other readers crashed.

Each reader test feeds one reader a deep document and checks that the reader treats it as it treats any unreadable
one, and still reads the records around it. The last test keeps the pattern from coming back: it fails on any
json.load or json.loads in lib/devkit outside core.load_json and the reviewed call sites of REVIEWED, each with the
reason it is safe. Every file here is temporary. Run: python3 -m unittest tests.test_json_reads (under
/usr/bin/python3 too, for Python 3.9).
"""
import ast
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
import test_acceptance_hooks as acceptance  # noqa: E402  (the hook entry run as an agent's harness runs it)
import test_mcp  # noqa: E402  (the MCP server run as the Claude Desktop extension runs it)
sys.path.insert(0, str(ROOT / "lib"))

from devkit import (audit, branches, bugs, canary, ciwatch, codexlaunch, continuity, core, gates, gc,  # noqa: E402
                    hooks, language, lessonlint, lessons, mcp, policy, progress, scope, shellparse, skillguard, state)
from devkit.visual import compare as vcompare, map as vmap, report as vreport, store as vstore, walk as vwalk  # noqa: E402,E501

# 1,000 levels: past the parser of Python 3.9 to 3.13. 150,000 levels: past the parser of Python 3.15 too, and still
# inside every reader's window (canary reads the last 400 KB of a transcript).
DEPTHS = (1_000, 150_000)


def deep(d: int) -> str:
    return "[" * d + "]" * d


def lines(*records) -> str:
    """JSONL: each record a dict (dumped) or text (written as it is)."""
    return "".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in records)


def user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def assistant_text(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def bash(command, use="toolu_1", **extra):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": use, "name": "Bash", "input": dict({"command": command}, **extra)}]}}


def deep_bash(d: int) -> str:
    """A Claude Code tool_use record whose input holds a value nested `d` levels deep."""
    return json.dumps(bash("ls"))[:-4] + ', "x": ' + deep(d) + "}}]}}"


def codex_call(name, arguments: str, call="call_1"):
    return {"type": "response_item", "payload": {"type": "function_call", "name": name, "call_id": call,
                                                 "arguments": arguments}}


class TempFolder(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def write(self, name: str, text: str) -> Path:
        f = self.root / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        return f


class TranscriptReaderTests(TempFolder):
    """Claude Code transcripts and Codex rollouts: what an agent's model produced, read by the hooks."""

    def test_turn_actions_skips_a_deep_record_and_reads_the_rest_of_the_turn(self):
        for d in DEPTHS:
            with self.subTest(depth=d):
                t = self.write("t.jsonl", lines(user("run the tests"), bash("uv run pytest -q"), deep_bash(d)))
                got = scope.turn_actions(str(t))
                self.assertEqual(got["commands"], ["uv run pytest -q"])

    def test_turn_actions_reads_deep_codex_arguments_as_unreadable_ones(self):
        start = {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}}
        for d in DEPTHS:
            with self.subTest(depth=d):
                t = self.write("r.jsonl", lines(start, codex_call("exec_command", '{"cmd": "uv run pytest -q"}'),
                                                codex_call("exec_command", '{"cmd": "ls", "x": ' + deep(d) + "}",
                                                           "call_2")))
                got = scope.turn_actions(str(t))
                self.assertEqual(got["commands"], ["uv run pytest -q", ""])
                self.assertIn("exec_command", got["tools"])

    def test_last_assistant_text_skips_a_deep_record(self):
        for d in DEPTHS:
            with self.subTest(depth=d):
                t = self.write("t.jsonl", lines(user("hi"), assistant_text("the reply"), deep_bash(d)))
                self.assertEqual(canary.last_assistant_text(str(t)), "the reply")

    def test_surface_of_a_rollout_with_a_deep_first_record_is_unknown(self):
        for d in DEPTHS:
            with self.subTest(depth=d):
                t = self.write("r.jsonl", lines('{"type": "session_meta", "payload": {"x": ' + deep(d) + "}}"))
                self.assertEqual(canary.surface("codex", {"transcript_path": str(t)}), "unknown")

    def test_canary_records_skip_a_deep_line(self):
        for d in DEPTHS:
            with self.subTest(depth=d):
                log = self.write("canary.jsonl", lines({"ts": 2_000_000_000, "ok": True}, deep(d)))
                with mock.patch.object(canary, "LOG", log):
                    self.assertEqual(canary.records(), [{"ts": 2_000_000_000, "ok": True}])

    def test_gates_records_and_brief_skip_a_deep_record(self):
        for d in DEPTHS:
            with self.subTest(depth=d):
                t = self.write("m.jsonl", lines(deep_bash(d), user("the brief"), bash("dev sec scan")))
                self.assertEqual([r["type"] for r in gates._records(t)], ["user", "assistant"])
                self.assertEqual(gates._brief(t), "")

    def test_gates_read_a_deep_receipt_output_as_unreadable_output(self):
        for d in DEPTHS:
            with self.subTest(depth=d):
                receipt = {"ok": True, "output": deep(d)}
                plain = {"ok": True, "output": "not JSON"}
                self.assertEqual(gates._execution_receipt_words(["dev", "sec", "scan"], receipt, str(self.root)),
                                 gates._execution_receipt_words(["dev", "sec", "scan"], plain, str(self.root)))
                self.assertEqual(gates._verification_eligible(receipt, "dev test run"),
                                 gates._verification_eligible(plain, "dev test run"))

    def test_continuity_reads_a_deep_record_as_no_boundary_and_no_brief(self):
        for d in DEPTHS:
            with self.subTest(depth=d):
                raw = ('{"type": "system", "subtype": "compact_boundary", "x": ' + deep(d) + "}").encode()
                self.assertIsNone(continuity._boundary(raw))
                small = min(d, 20_000)  # the brief is read from the first 64 KB
                t = self.write("m.jsonl", lines('{"type": "user", "x": ' + deep(small) + "}", user("the brief")))
                self.assertEqual(continuity._brief_head(str(t)), "the brief")

    def test_language_skips_a_deep_record(self):
        for d in DEPTHS:
            with self.subTest(depth=d):
                t = self.write("t.jsonl", lines(user("hola, ¿qué tal?"), '{"type": "user", "x": ' + deep(d) + "}"))
                self.assertEqual(list(language.developer_messages(str(t))), ["hola, ¿qué tal?"])
                r = self.write("r.jsonl", lines('{"type": "session_meta", "payload": {"parent_thread_id": "p", '
                                                '"x": ' + deep(d) + "}}"))
                self.assertFalse(language.member_rollout(str(r)))
                self.assertEqual(language._answers(deep(d)), [])

    def test_skillguard_read_skips_a_deep_record(self):
        skill = {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_2", "name": "Skill", "input": {"skill": "devkit:testing"}}]}}
        for d in DEPTHS:
            with self.subTest(agent="claude", depth=d):
                t = self.write("t.jsonl", lines(skill, deep_bash(d)))
                loaded, _, commands, _ = skillguard.read("claude", str(t))
                self.assertEqual((loaded, commands), ({"testing"}, []))
            with self.subTest(agent="codex", depth=d):
                t = self.write("r.jsonl", lines(codex_call("exec_command", '{"cmd": "uv run pytest -q"}'),
                                                codex_call("exec_command", '{"cmd": "ls", "x": ' + deep(d) + "}"),
                                                '{"type": "response_item", "function_call": ' + deep(d) + "}"))
                _, _, commands, _ = skillguard.read("codex", str(t))
                self.assertEqual(commands, ["uv run pytest -q", ""])

    def test_fork_start_reads_a_deep_record_as_none(self):
        meta = {"type": "session_meta", "payload": {"forked_from_id": "p", "subagent_history_start_ordinal": 1}}
        started = '{"type": "event_msg", "payload": {"type": "task_started"}}'
        for d in DEPTHS:
            with self.subTest(depth=d):
                self.assertEqual(skillguard.fork_start([deep(d), started]), 0)
                deep_start = '{"type": "event_msg", "x": "task_started", "y": ' + deep(d) + "}"
                self.assertEqual(skillguard.fork_start([json.dumps(meta), deep_start, started]), 2)

    def test_executions_read_deep_codex_arguments_and_outputs_as_unreadable(self):
        meta = {"type": "session_meta", "payload": {"id": "T", "cwd": str(self.root)}}
        ok = {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call_1",
                                                   "output": json.dumps({"exit_code": 0, "output": "1 passed"})}}
        for d in DEPTHS:
            with self.subTest(depth=d):
                bad_out = {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call_2",
                                                                "output": '{"exit_code": 0, "x": ' + deep(d) + "}"}}
                t = self.write("r.jsonl", lines(meta, codex_call("exec_command", '{"cmd": "uv run pytest -q"}'), ok,
                                                codex_call("exec_command", '{"cmd": "ls"}', "call_2"), bad_out,
                                                codex_call("exec_command", '{"cmd": "ls", "x": ' + deep(d) + "}",
                                                           "call_3")))
                got = skillguard.executions("codex", str(t))
                self.assertEqual([(r["command"], r["ok"]) for r in got], [("uv run pytest -q", True)])


class StopHookTests(acceptance.Sandbox):
    """The Stop hook end to end, as Codex starts it: a turn whose rollout holds a deep tool input still owes the
    testing skill for the tests it ran, and the hook logs no error (a hook fails open, so an error shows only there)."""

    def setUp(self):
        super().setUp()
        skill = self.home / ".codex" / "plugins" / "cache" / "firefly-devkit" / "devkit" / "26.11.9" / "skills" / \
            "testing" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: testing\n---\n")

    def test_a_deep_tool_input_leaves_the_turn_readable(self):
        for n, d in enumerate(DEPTHS):
            with self.subTest(depth=d):
                self.sid = acceptance.CODEX_SID[:-1] + str(n)  # a session of its own: each owes the skill once
                self.start_codex_turn()
                self.append_rollout(self.exec_call('text(await tools.exec_command({cmd:"uv run pytest -q"}));\n'),
                                    self.command_record(1, "uv run pytest -q"))
                with self.rollout.open("a") as fh:
                    fh.write(lines(codex_call("exec_command", '{"cmd": "ls", "x": ' + deep(d) + "}", "call_9"),
                                   '{"type": "response_item", "payload": {"type": "function_call", "x": ' + deep(d)
                                   + "}}"))
                out = self.hook("stop", "codex", self.codex("Stop", stop_hook_active=False,
                                                            last_assistant_message=acceptance.BLOCK))
                self.assertEqual(self.errors(), [])
                self.assertEqual(out.get("decision"), "block")
                self.assertIn("`testing`", out.get("reason", ""))


@unittest.skipUnless(Path("/usr/bin/python3").exists(), "no system Python 3.9 on this machine")
class StopHookPython39Tests(StopHookTests):
    """The same, with the hook under macOS's own Python 3.9, whose parser fails at about a thousand levels."""

    python = "/usr/bin/python3"


class RecordReaderTests(TempFolder):
    """devkit's state records (state.read, load, load_bounded) and the team's lesson rule files."""

    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(core, "STATE", self.root), mock.patch.object(audit, "AUDIT", self.root / "audit")):
            p.start()
            self.addCleanup(p.stop)

    def test_a_deep_state_record_is_unreadable_and_never_written_over(self):
        for d in DEPTHS:
            with self.subTest(depth=d):
                f = self.write("rec.json", '{"a": ' + deep(d) + "}")
                self.assertIsNone(state.read(f))
                with self.assertRaises(state.StateBusy):
                    state.load(f)
                with self.assertRaises(state.StateCorrupt):
                    state.load_bounded(f, None)
                self.assertEqual(f.read_text(), '{"a": ' + deep(d) + "}")

    def test_a_deep_lesson_rule_file_names_no_rule(self):
        with mock.patch.object(lessons, "LESSONS", self.root / "handoff" / "lessons"):
            self.write("handoff/lessons/rules/global.json", json.dumps({"rules": [{"id": "R-1"}]}))
            for d in DEPTHS:
                with self.subTest(depth=d):
                    self.write("handoff/lessons/rules/team.json", '{"rules": [{"id": "R-2", "x": ' + deep(d) + "}]}")
                    self.assertEqual(lessonlint._rule_ids(), {"R-1"})


class ForeignTextReaderTests(TempFolder):
    """Text the public, a model, a repository or an MCP client chooses: an issue's body, a Codex call's arguments, a
    JSON-RPC message, an agent's settings file, a package.json."""

    def test_an_issue_body_with_a_deep_json_block_holds_no_payload_or_record(self):
        for d in DEPTHS:
            with self.subTest(depth=d):
                block = "```json\n" + '{"devkit_bug": 1, "fingerprint": "f", "x": ' + deep(d) + "}\n```"
                self.assertIsNone(bugs.parse_payload(block))
                self.assertIsNone(bugs.parse_record(bugs.RECORD_MARK + "\n" + block))
        self.assertEqual(bugs.parse_payload('```json\n{"devkit_bug": 1}\n```'), {"devkit_bug": 1})

    def test_deep_codex_shell_arguments_read_as_unreadable_ones(self):
        for d in DEPTHS:
            with self.subTest(depth=d):
                args = '{"cmd": "git push", "x": ' + deep(d) + "}"
                t = self.write("r.jsonl", lines(codex_call("exec_command", '{"cmd": "uv run pytest -q"}'),
                                                codex_call("exec_command", args, "call_2"),
                                                codex_call("exec_command", "not JSON", "call_3")))
                calls, _ = ciwatch._shell_calls(str(t), 2_000_000_000)
                self.assertEqual([c["command"] for c in calls], ["uv run pytest -q", args, "not JSON"])

    def test_the_mcp_server_answers_a_deep_message_with_a_parse_error_and_serves_on(self):
        for d in DEPTHS:
            with self.subTest(depth=d):
                call = ('{"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "devkit_status", '
                        '"arguments": {"x": ' + deep(d) + "}}}")
                out = io.BytesIO()
                server = mcp.Server(io.BytesIO((call + "\n" + json.dumps({"jsonrpc": "2.0", "id": 8, "method": "ping"})
                                                + "\n").encode()), out)
                self.assertEqual(server.serve(), 0)
                replies = [json.loads(ln) for ln in out.getvalue().splitlines()]
                self.assertEqual([(r["id"], (r.get("error") or {}).get("code")) for r in replies],
                                 [(None, mcp.PARSE_ERROR), (8, None)])

    def test_a_deep_settings_file_reads_like_an_unparsable_one(self):
        for d in DEPTHS:
            with self.subTest(depth=d):
                text = '{"env": {"DEVKIT_HOME": "/x", "DEVKIT_A": ' + deep(d) + '}, "y": ' + deep(d) + "}"
                self.assertEqual(policy.devkit_env(text), {"DEVKIT_HOME": '"/x"', "DEVKIT_A": deep(d)})

    def test_a_deep_package_json_names_no_main(self):
        pkg = self.root / "tool"
        for name in ("app.js", "index.js"):
            self.write(f"tool/{name}", "")
        self.write("tool/package.json", '{"main": "app.js"}')
        self.assertEqual(shellparse._run_target(pkg, "node"), pkg / "app.js")
        for d in DEPTHS:
            with self.subTest(depth=d):
                self.write("tool/package.json", '{"main": "app.js", "x": ' + deep(d) + "}")
                self.assertEqual(shellparse._run_target(pkg, "node"), pkg / "index.js")


class TeamRecordReaderTests(TempFolder):
    """Records that sync from teammates through the handoff repository: the published audit trails and the visual
    diagnosis records."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(core, "HANDOFF", self.root / "handoff")
        p.start()
        self.addCleanup(p.stop)
        self.records = self.root / "handoff" / "visual" / "acme" / "api"

    def test_embargo_records_skip_a_deep_line(self):
        rec = {"action": "git.embargo", "outcome": "ok", "id": "A-1", "project": "acme/api", "branch": "fix/a",
               "detail": "set"}
        for d in DEPTHS:
            with self.subTest(depth=d):
                f = self.write(f"audit/{d}.jsonl", lines(rec, json.dumps(dict(rec, id="A-2", branch="fix/b"))[:-1]
                                                         + ', "x": ' + deep(d) + "}"))
                self.assertEqual([r["branch"] for r in branches._embargo_records(f)], ["fix/a"])

    def test_a_deep_visual_record_is_unreadable_to_gc_which_then_prunes_nothing(self):
        self.write("handoff/visual/acme/api/captures/VC-abcd.json", json.dumps({"id": "VC-abcd"}))
        self.assertIsNotNone(gc._visual_in_use(self.records))
        for d in DEPTHS:
            with self.subTest(depth=d):
                f = self.write("handoff/visual/acme/api/findings/VF-abcd.json",
                               '{"id": "VF-abcd", "status": "open", "x": ' + deep(d) + "}")
                self.assertIsNone(gc._visual_json(f))
                self.assertIsNone(gc._visual_in_use(self.records))

    def test_a_deep_capture_record_cannot_be_compared(self):
        for d in DEPTHS:
            with self.subTest(depth=d):
                self.write("handoff/visual/acme/api/captures/VC-abcd.json", '{"id": "VC-abcd", "x": ' + deep(d) + "}")
                with self.assertRaises(vcompare.CompareError):
                    vcompare._capture(self.records, "VC-abcd")

    def test_a_deep_map_or_record_is_unreadable(self):
        for d in DEPTHS:
            with self.subTest(depth=d):
                f = self.write("handoff/visual/acme/api/map/current.json", '{"version": 1, "x": ' + deep(d) + "}")
                with self.assertRaises(vmap.MapError):
                    vmap._read(f)
                self.assertIsNone(vmap._record(f))
                self.assertIsNone(vreport._read(f))

    def test_a_deep_store_record_is_skipped_in_lists_and_unreadable_alone(self):
        self.write("handoff/visual/acme/api/captures/VC-good.json", json.dumps({"id": "VC-good"}))
        for d in DEPTHS:
            with self.subTest(depth=d):
                self.write("handoff/visual/acme/api/captures/VC-deep.json", '{"id": "VC-deep", "x": ' + deep(d) + "}")
                self.assertEqual([r["id"] for r in vstore._all("acme/api", "VC")], ["VC-good"])
                with self.assertRaises(vstore.VisualError):
                    vstore.load("acme/api", "VC-deep")

    def test_a_deep_walk_record_is_skipped(self):
        self.write("handoff/visual/acme/api/walks/VA-abcd/j1.json", json.dumps({"journey": "J:j1"}))
        for d in DEPTHS:
            with self.subTest(depth=d):
                self.write("handoff/visual/acme/api/walks/VA-abcd/j2.json", '{"journey": "J:j2", "x": ' + deep(d)
                           + "}")
                self.assertEqual([r["journey"] for r in vwalk.load("acme/api", "VA-abcd")], ["J:j1"])


class PreToolHookTests(acceptance.Sandbox):
    """The PreToolUse hook end to end: a command that runs node on a folder outside the repository whose package.json
    is deep, then `dev watch --wait`. The hook's readers of the command (the speaker stamp, the crew-lane advice) read
    the folder's index.js instead, and none fails: a failing one logs its traceback and loses its rewrite or advice."""

    def test_node_on_a_folder_with_a_deep_package_json(self):
        tool = self.home / "vendor" / "tool"  # outside the repository: devkit reads a program it cannot trust
        tool.mkdir(parents=True)
        (tool / "index.js").write_text("console.log('hi')\n")
        self.start_claude_turn()
        log = self.state / "logs" / "hooks.log"
        for n, d in enumerate(DEPTHS):
            with self.subTest(depth=d):
                (tool / "package.json").write_text('{"main": "index.js", "x": ' + deep(d) + "}")
                cmd = f"node {tool} && dev watch --wait"
                out = self.hook("pre-tool", "claude", self.claude("PreToolUse", tool_name="Bash",
                                                                  tool_input={"command": cmd},
                                                                  tool_use_id=f"toolu_0{n}"))
                self.assertEqual(acceptance.decision(out), "allow")
                failed = [ln for ln in (log.read_text() if log.exists() else "").splitlines()
                          if ln.endswith("Error: maximum recursion depth exceeded while decoding a JSON array from a "
                                         "unicode string") or "pre-tool route" in ln or "pre-tool advice" in ln]
                self.assertEqual(failed, [])


@unittest.skipUnless(Path("/usr/bin/python3").exists(), "no system Python 3.9 on this machine")
class PreToolHookPython39Tests(PreToolHookTests):
    python = "/usr/bin/python3"


class McpServerTests(unittest.TestCase):
    """`dev mcp` end to end, as the Claude Desktop extension starts it (-I -S): a client message nested too deep gets
    a JSON-RPC parse error, and the server answers the next one."""

    python = sys.executable

    def test_a_deep_message_gets_a_parse_error(self):
        with test_mcp.TempHome() as h:
            s = test_mcp.Session([self.python, "-I", "-S", str(ROOT / "bin" / "dev"), "mcp"], h.env(), h.home)
            try:
                s.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "test", "version": "1"}})
                for d in DEPTHS:
                    with self.subTest(depth=d):
                        s.send('{"jsonrpc": "2.0", "id": 70, "method": "tools/call", "params": {"name": "devkit_status",'
                               ' "arguments": {"x": ' + deep(d) + "}}}")
                        reply = s.recv()
                        self.assertEqual((reply["id"], reply["error"]["code"]), (None, mcp.PARSE_ERROR))
                        self.assertEqual(s.request("ping")["result"], {})
            finally:
                self.assertEqual(s.close(), 0)


@unittest.skipUnless(Path("/usr/bin/python3").exists(), "no system Python 3.9 on this machine")
class McpServerPython39Tests(McpServerTests):
    python = "/usr/bin/python3"


class HookReaderTests(TempFolder):
    """What the hooks read besides their input: a Codex rollout's command records, its first record and devkit's
    session records; and the hook input as the capture and the identity read it."""

    def test_codex_rollout_skips_a_deep_command_record(self):
        meta = {"type": "session_meta", "payload": {"cli_version": "0.167.0"}}

        def item(n):
            return {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "CommandExecution", "id": f"E{n}", "command": ["ls"]}}}
        for d in DEPTHS:
            with self.subTest(depth=d):
                t = self.write("r.jsonl", lines(meta, item(1), json.dumps(item(2))[:-3] + ', "x": ' + deep(d) + "}}}"))
                items, _ = hooks.codex_rollout(str(t))
                self.assertEqual(sorted(items), ["E1"])

    def test_a_deep_item_or_meta_record_names_no_item_and_no_version(self):
        for d in DEPTHS:
            with self.subTest(depth=d):
                self.assertFalse(hooks._item_record(('{"type": "event_msg", "payload": {"type": "item_completed", '
                                                     '"item": {"x": ' + deep(d) + "}}}").encode()))
                self.assertEqual(hooks._codex_version(('{"type": "session_meta", "payload": {"cli_version": '
                                                       '"0.167.0", "x": ' + deep(d) + "}}").encode()), ())
        self.assertEqual(hooks._codex_version(b'{"type": "session_meta", "payload": {"cli_version": "0.167.0"}}'),
                         (0, 167, 0))

    def test_sessions_to_restart_skips_a_deep_session_record(self):
        rec = {"agent": "claude", "pid": 1, "session": "s1", "started": 1}
        self.write("sessions/claude-s1.json", json.dumps(rec))
        for d in DEPTHS:
            with self.subTest(depth=d):
                self.write("sessions/claude-s2.json", json.dumps(dict(rec, session="s2"))[:-1] + ', "x": ' + deep(d)
                           + "}")
                got = hooks.sessions_to_restart(self.root / "sessions", alive=lambda *a: True)
                self.assertEqual([s["session"] for s in got], ["s1"])

    def test_a_deep_hook_input_is_captured_unparsed_and_names_no_session(self):
        for d in DEPTHS:
            with self.subTest(depth=d):
                raw = '{"session_id": "s1", "hook_event_name": "PreToolUse", "x": ' + deep(d) + "}"
                seen = []
                with mock.patch("sys.stdin", io.StringIO(raw)), \
                        mock.patch.object(hooks, "capture", lambda e, a, data: seen.append(data)):
                    hooks._capture_stdin("pre-tool", "claude")
                    self.assertEqual(sys.stdin.read(), raw)  # the handler still gets the text
                self.assertEqual(seen, [{"unparsed": raw[:hooks.CAPTURE_CUT]}])
                with mock.patch("sys.stdin", io.StringIO(raw)), \
                        mock.patch.dict(os.environ, {"DEVKIT_SESSION": "claude:old"}):
                    hooks._own_identity("pre-tool", "claude")  # a payload naming no session keeps this agent's
                    self.assertEqual(os.environ.get("DEVKIT_SESSION"), "claude:old")
                    self.assertEqual(sys.stdin.read(), raw)
                with mock.patch("sys.stdin", io.StringIO(raw)):
                    self.assertEqual(hooks._stdin_json(), {})


class CodexLaunchReaderTests(TempFolder):
    """A Codex parent's rollout, which its model's tool calls fill, read for native launches and interruptions."""

    META = {"timestamp": "2026-11-23T10:00:00Z", "type": "session_meta", "payload": {"id": "T1"}}

    @staticmethod
    def activity(aid, kind="interrupted", extra=""):
        return ('{"timestamp": "2026-11-23T10:00:05Z", "type": "event_msg", "payload": {"type": "item_completed", '
                '"thread_id": "T1", "item": {"type": "SubAgentActivity", "kind": "' + kind + '", "agent_thread_id": "'
                + aid + '", "id": "c-' + aid + '"' + extra + "}}}")

    @staticmethod
    def spawn(arguments: str):
        return {"timestamp": "2026-11-23T10:00:01Z", "type": "response_item", "payload": {
            "type": "function_call", "name": "spawn_agent", "namespace": "collaboration", "call_id": "c1",
            "arguments": arguments}}

    def test_a_deep_first_record_is_no_session_meta(self):
        for d in DEPTHS:
            with self.subTest(depth=d):
                t = self.write("r.jsonl", '{"type": "session_meta", "payload": {"id": "T1", "x": ' + deep(d) + "}}\n")
                self.assertEqual(codexlaunch.session_meta(t), {})
                self.assertEqual(codexlaunch.spawn_calls(t), ([], {}))

    def test_lifecycle_events_skip_a_deep_interruption(self):
        for d in DEPTHS:
            with self.subTest(depth=d):
                t = self.write("r.jsonl", lines(self.META, self.activity("A1"),
                                                self.activity("A2", extra=', "x": ' + deep(d))))
                self.assertEqual(sorted(codexlaunch.lifecycle_events(t, "T1", ["A1", "A2"])), ["A1"])

    def test_deep_spawn_arguments_leave_the_snapshot_incomplete(self):
        t = self.write("r.jsonl", lines(self.META, self.spawn('{"task_name": "scan"}')))
        self.assertEqual([c["task_name"] for c in codexlaunch.spawn_calls(t)[0]], ["scan"])
        for d in DEPTHS:
            with self.subTest(depth=d):
                why = []
                t = self.write("r.jsonl", lines(self.META, self.spawn('{"task_name": "scan", "x": ' + deep(d) + "}")))
                self.assertEqual(codexlaunch.spawn_calls(t, diagnostics=why), ([], {}))
                self.assertEqual(why, ["parent rollout is incomplete, unreadable, or malformed"])


class ProgressReaderTests(TempFolder):
    """What the progress hooks read from a Claude Code transcript or a Codex rollout, which a model's output fills (the
    typed requests, the last reply, the rollout's session_meta), and this Mac's timing records."""

    SID = "019a0000-0000-7000-8000-000000000001"

    def test_timing_records_skip_a_deep_line(self):
        good = {"ts": 2_000_000_000, "kind": "turn", "secs": 60}
        for d in DEPTHS:
            with self.subTest(depth=d):
                self.write("timing/2026-11.jsonl", lines(good, '{"ts": 2000000000, "kind": "turn", "x": ' + deep(d)
                                                         + "}", deep(d)))
                with mock.patch.object(progress, "TIMING", self.root / "timing"):
                    self.assertEqual(progress.records(), [good])

    def test_last_reply_time_skips_a_deep_reply(self):
        said = dict(assistant_text("the reply"), timestamp="2026-11-23T10:00:00Z")
        for d in DEPTHS:
            with self.subTest(depth=d):
                later = json.dumps(dict(assistant_text("a later reply"), timestamp="2026-11-23T10:05:00Z"))
                t = self.write("t.jsonl", lines(said, later[:-1] + ', "x": ' + deep(d) + "}"))
                self.assertEqual(progress._last_assistant_text_ts(str(t)), 1_795_428_000.0)

    def test_typed_requests_skip_a_deep_queued_command(self):
        def queued(text, ts, extra=""):
            return ('{"type": "attachment", "timestamp": "' + ts + '", "attachment": {"type": "queued_command", '
                    '"commandMode": "prompt", "origin": {"kind": "human"}, "prompt": "' + text + '"}' + extra + "}")
        for d in DEPTHS:
            with self.subTest(depth=d):
                t = self.write("t.jsonl", lines(queued("stop the push", "2026-11-23T10:00:00Z"),
                                                queued("also deploy", "2026-11-23T10:01:00Z", ', "x": ' + deep(d))))
                got, offset = progress.midturn_requests(str(t), 0)
                self.assertEqual(got, [{"text": "stop the push", "ts": 1_795_428_000.0}])
                self.assertEqual(offset, t.stat().st_size)

    def test_codex_typed_requests_skip_a_deep_user_message(self):
        def typed(text, ts, extra=""):
            return ('{"timestamp": "' + ts + '", "type": "event_msg", "payload": {"type": "item_completed", '
                    '"thread_id": "' + self.SID + '", "item": {"type": "UserMessage", "content": [{"type": "text", '
                    '"text": "' + text + '"}]' + extra + "}}}")
        for d in DEPTHS:
            with self.subTest(depth=d):
                t = self.write("r.jsonl", lines(typed("stop the push", "2026-11-23T10:00:00Z"),
                                                typed("also deploy", "2026-11-23T10:01:00Z", ', "x": ' + deep(d))))
                got, cursor = progress._codex_midturn_requests(str(t), self.SID, {}, 0)
                self.assertEqual(got, [{"text": "stop the push", "ts": 1_795_428_000.0}])
                self.assertEqual(cursor["offset"], t.stat().st_size)

    def test_a_deep_session_meta_is_no_session_meta(self):
        meta = '{"type": "session_meta", "payload": {"id": "' + self.SID + '", "cwd": "/x"'
        t = self.write("r.jsonl", meta + "}}\n")
        self.assertEqual(progress._codex_meta(t, self.SID), {"id": self.SID, "cwd": "/x"})
        for d in DEPTHS:
            with self.subTest(depth=d):
                t = self.write("r.jsonl", meta + ', "x": ' + deep(d) + "}}\n")
                self.assertIsNone(progress._codex_meta(t, self.SID))


class HookInputTests(acceptance.Sandbox):
    """bin/dev hook end to end, as Claude Code starts it (-I -S, the payload on stdin): a hook input nested too deep
    reads as a malformed one (the hook fails open, its output the same, no error logged), and the ConfigChange hook
    refuses a settings file it cannot parse that sets a DEVKIT_* variable."""

    def dev_hook(self, event, raw):
        p = subprocess.run([self.python, "-I", "-S", str(ROOT / "bin" / "dev"), "hook", event, "--agent", "claude"],
                           input=raw, capture_output=True, text=True, cwd=str(self.repo), env=self.env, timeout=180)
        return p.returncode, p.stdout, p.stderr

    def failures(self):
        f = self.state / "logs" / "hooks.log"
        return [ln for ln in (f.read_text() if f.exists() else "").splitlines() if " error" in ln or "Error" in ln]

    def test_a_deep_hook_input_reads_as_a_malformed_one(self):
        self.start_claude_turn()
        fields = {"pre-tool": ('{"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_use_id": "toolu_01", '
                               '"tool_input": {"command": "git status", "x": '),
                  "stop": '{"hook_event_name": "Stop", "stop_hook_active": false, "x": '}
        for event, head in fields.items():
            head = head.replace("{", '{"session_id": "' + acceptance.CLAUDE_SID + '", "cwd": "' + str(self.repo)
                                + '", "transcript_path": "' + str(self.transcript) + '", ', 1)
            close = "}}" if event == "pre-tool" else "}"
            malformed = self.dev_hook(event, head + "[")
            for d in DEPTHS:
                with self.subTest(event=event, depth=d):
                    self.assertEqual(self.dev_hook(event, head + deep(d) + close), malformed)
                    self.assertEqual(self.failures(), [])

    def test_config_change_refuses_an_unparsable_settings_file_that_sets_a_devkit_variable(self):
        local = self.repo / ".claude" / "settings.local.json"
        local.parent.mkdir()
        user = self.home / ".claude" / "settings.json"
        user.parent.mkdir(exist_ok=True)
        for d in DEPTHS:
            for f, source, key in ((local, "local_settings", "DEVKIT_HOME"), (user, "user_settings", "DEVKIT_GUARD")):
                with self.subTest(source=source, depth=d):
                    text = '{"env": {"' + key + '": "/var/tmp/x"}, "x": ' + deep(d) + "}"
                    f.write_text(text)
                    code, _, err = self.dev_hook("config-change", json.dumps(
                        {"session_id": acceptance.CLAUDE_SID, "hook_event_name": "ConfigChange", "source": source,
                         "file_path": str(f), "cwd": str(self.repo)}))
                    self.assertEqual(code, 2)
                    self.assertIn(f"env.{key}", err)
                    self.assertEqual(f.read_text(), text)  # a file devkit cannot parse it cannot rewrite either
                    self.assertIn(f"env.{key}", self.audit("config.blocked")[-1]["detail"])
            with self.subTest(source="local_settings, no DEVKIT_* variable", depth=d):
                local.write_text('{"env": {"NODE_ENV": "test"}, "x": ' + deep(d) + "}")
                code, _, err = self.dev_hook("config-change", json.dumps(
                    {"session_id": acceptance.CLAUDE_SID, "hook_event_name": "ConfigChange",
                     "source": "local_settings", "file_path": str(local), "cwd": str(self.repo)}))
                self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.failures(), [])


@unittest.skipUnless(Path("/usr/bin/python3").exists(), "no system Python 3.9 on this machine")
class HookInputPython39Tests(HookInputTests):
    python = "/usr/bin/python3"


# --------------------------------------------------------------------------- the guard

# Why a raw parse is safe where it stands. A deep document can reach a parser only through text whose shape someone
# outside devkit chooses: a model (transcripts, rollouts, hook input), a teammate or another Mac (the handoff
# repository, synced records), the public (issues). Text devkit wrote itself in a fixed shape cannot be deep.
OWN = ("devkit's own record on this Mac, written only by devkit in a fixed shape that holds no foreign JSON value "
       "whole")
SHIPPED = "a file of devkit's own release (config/, templates/), never a teammate's"
TOOL = ("the JSON output of a program devkit runs (gh and the hosts' APIs, git, the scanners, test runners, Vale, "
        "Docker, the agents' CLIs, devkit's own scripts): a fixed shape whose free text is strings")
COPY = "a copy of a value devkit holds: json.dumps runs first, and fails first on anything too deep"
STRING = "text that starts with a double quote, which parses as one string or not at all: a string has no depth"
COMMAND = ("a file or value the developer names to an interactive command (the project's package.json, a spec, a "
           "cursor): a deep one fails that command only, never a hook")
CONFIG = "the developer's own devkit configuration, which dev config validates on every write"
# Exposed readers outside the files this guard came with; each leaves the list when it is routed through load_json.
AUDIT = "OPEN: the team's published audit files; audit.py's readers move to core.load_json with the audit depth guard"

# (module below lib/devkit, enclosing function) -> (raw parses there, why). core.load_json is the one parse of JSON
# that comes from outside; nothing else may call json.load, json.loads or json.JSONDecoder without an entry here.
REVIEWED = {
    ("approvals", "meta_reader.read"): (1, "Claude Code's subagent meta file, a fixed shape it writes itself"),
    ("audit", "_day_of"): (1, AUDIT),
    ("audit", "_held_info"): (1, AUDIT),
    ("audit", "_roster_ids"): (1, SHIPPED),
    ("audit", "_stored"): (1, AUDIT),
    ("audit", "_this_macs_copy"): (1, AUDIT),
    ("audit", "_v2"): (1, AUDIT),
    ("audit", "verify_file"): (1, AUDIT),
    ("backup", "scan"): (1, TOOL),
    ("branches", "_backup_records"): (1, OWN + " (this Mac's audit trail)"),
    ("bugs", "GhClient._json"): (1, TOOL),
    ("bugs", "GhClient._lines"): (1, TOOL),
    ("bugs", "UrlClient._get"): (1, TOOL),
    ("bugs", "_bounded_json"): (1, OWN),
    ("bugs", "_load_item"): (1, OWN),
    ("bugs", "_tail_records"): (1, OWN),
    ("bugs", "_trim_occurrences"): (1, OWN),
    ("bugs", "fit_record"): (1, COPY),
    ("bugs", "fold"): (1, COPY),
    ("bugs", "match"): (1, COPY),
    ("bugs", "stale_hooks"): (1, OWN),
    ("cistatus", "_cached"): (1, OWN),
    ("cistatus", "_github"): (2, TOOL),
    ("cistatus", "_github.typical"): (1, TOOL),
    ("cistatus", "_gitlab.api"): (1, TOOL),
    ("cistatus", "_http"): (1, TOOL),
    ("ciwatch", "_final_jobs"): (1, TOOL),
    ("ciwatch", "_gh_pr"): (1, TOOL),
    ("ciwatch", "job_annotation"): (1, TOOL),
    ("cli", "_config_broken"): (1, CONFIG),
    ("cli", "_config_validate"): (1, SHIPPED),
    ("codexrpc", "CodexRPC.call"): (1, TOOL),
    ("configschema", "audit_config"): (1, COPY),
    ("configschema", "loads"): (1, CONFIG),
    ("configschema", "progress_config"): (1, COPY),
    ("core", "_cached"): (2, OWN),
    ("core", "_copy_sources"): (1, OWN),
    ("core", "config_update"): (1, COPY),
    ("core", "gh_user.produce"): (1, TOOL),
    ("core", "pinned_release"): (1, OWN + " (install.json)"),
    ("crew", "_team_write"): (1, SHIPPED + " (the roster; the team's overlay is read with core.read_json)"),
    ("crew", "load"): (1, SHIPPED + " (the roster; the team's overlay is read with core.read_json)"),
    ("dashboard", "pr_cache"): (1, OWN),
    ("dashserver", "_request"): (1, TOOL),
    ("delivery", "_version_source"): (2, COMMAND),
    ("desktop", "source_manifest"): (1, SHIPPED),
    ("desktop", "status"): (1, TOOL),
    ("desktop", "verify_bundle"): (1, COMMAND),
    ("diagram", "load"): (1, COMMAND),
    ("diagram", "main.is_spec"): (1, COMMAND),
    ("diagram", "scaffold"): (1, COPY),
    ("disk", "_json_lines"): (1, TOOL),
    ("docs", "_vale_stops"): (1, TOOL),
    ("docs", "check"): (1, TOOL),
    ("ext", "Upstream._json"): (1, TOOL),
    ("ext", "_read_json"): (1, COMMAND + " (the agents' settings, read by dev ext and dev install)"),
    ("ext", "_rw"): (1, SHIPPED),
    ("ext", "_toml_value"): (1, COMMAND + " (a line of Codex's config.toml)"),
    ("ext", "bump"): (1, SHIPPED),
    ("fs", "_grep_ast"): (1, TOOL),
    ("fs", "_history"): (1, OWN),
    ("fs", "_rg_events"): (1, TOOL),
    ("gitx", "_dependabot_update_run"): (1, TOOL),
    ("gitx", "_docker_contexts"): (1, TOOL + " (the Docker CLI's context store)"),
    ("gitx", "_only_skipped"): (1, TOOL),
    ("gitx", "ctx"): (1, TOOL),
    ("gitx", "gh_json"): (2, TOOL),
    ("hooks", "_devkit_env_change"): (1, COPY + " (the DEVKIT_* values of devkit's own configuration)"),
    ("identity", "_read_kept"): (1, OWN),
    ("identity", "_read_used"): (1, OWN),
    ("identity", "read_uses"): (1, OWN),
    ("jobs", "journal"): (1, OWN),
    ("lessonevents", "_events"): (1, OWN),
    ("lessonevents", "prune"): (1, OWN),
    ("lessonrules", "_timing_commands"): (1, OWN),
    ("mcp", "call_tool"): (1, TOOL),
    ("messaging", "index_page"): (1, COMMAND),
    ("messaging", "read_page"): (1, COMMAND),
    ("migrate", "run"): (1, COMMAND),
    ("notes", "_block_value"): (1, STRING),
    ("notifications", "build_bundle"): (1, TOOL),
    ("peers", "_read"): (1, OWN),
    ("pipelines", "_Expr.call"): (1, COMMAND + " (fromJSON in the repository's workflow, read by dev ci audit)"),
    ("pipelines", "_expand"): (1, COMMAND),
    ("pipelines", "_gh_api"): (1, TOOL),
    ("pipelines", "_pins"): (1, SHIPPED),
    ("pipelines", "_stack_php"): (1, COMMAND),
    ("pipelines", "external_linters"): (2, TOOL),
    ("pipelines", "facts"): (1, COMMAND),
    ("plans", "_unquote"): (1, STRING),
    ("profiles", "catalog"): (1, SHIPPED),
    ("providers", "_claude_status"): (1, TOOL),
    ("providers", "_run_claude"): (1, TOOL),
    ("providers", "cached"): (1, OWN),
    ("providers", "selftest"): (1, TOOL),
    ("raci", "_hygiene_added"): (1, SHIPPED),
    ("registry", "_write_outputs"): (1, COPY),
    ("registry", "live_sessions"): (1, OWN),
    ("registry", "pr_fresh"): (1, TOOL),
    ("registry", "prs"): (3, TOOL),
    ("security", "scan"): (5, TOOL),
    ("security", "staged_secrets"): (1, TOOL),
    ("selfcheck", "d6_config_keys"): (1, SHIPPED),
    ("setup", "branch_cleanup_checks"): (1, TOOL),
    ("setup", "load_conf"): (1, SHIPPED),
    ("sleep", "_old_record"): (1, OWN),
    ("testx", "_json_loose"): (1, COMMAND + " (the project's tsconfig)"),
    ("testx", "parse_go_json"): (1, TOOL),
    ("testx", "parse_jest_json"): (2, TOOL),
    ("updatecheck", "read_cache"): (1, OWN),
    ("visual.capture_checks", "_probe"): (1, OWN + " (a capture's description, which stays on the Mac that took it)"),
    ("visual.commands", "cmd_map_apply"): (1, COMMAND),
    ("visual.compare", "_description"): (1, OWN + " (a capture's description, which stays on the Mac that took it)"),
    ("visual.compare", "compare_pictures"): (1, TOOL),
    ("visual.docmap", "read"): (1, TOOL),
    ("visual.judge", "_draw"): (1, TOOL),
    ("visual.map", "_node_json"): (1, TOOL),
    ("visual.map", "run_crawler"): (1, TOOL),
    ("visual.report", "_description"): (1, OWN + " (a capture's description, which stays on the Mac that took it)"),
    ("visual.store", "_run_capture"): (1, TOOL),
    ("visual.store", "description"): (1, OWN + " (a capture's description, which stays on the Mac that took it)"),
    ("visual.store", "open_audit"): (1, OWN),
    ("wizard", "_Wizard._interactive"): (1, COPY),
    ("wizard", "_Wizard.reopen"): (1, COPY),
    ("wizard", "_npm_package"): (1, COMMAND),
    ("wizard", "answers_changed.norm"): (1, COPY),
    ("wizard", "skill_question"): (1, SHIPPED),
    ("work", "_shown_lists"): (1, OWN),
    ("work", "writing"): (1, OWN),
}
# json's parsers, and the submodules that hold them (json.decoder.JSONDecoder, json.scanner's scanner)
PARSERS = ("load", "loads", "JSONDecoder", "decoder", "scanner")


def _json_import(node) -> bool:
    """An import that names json's parsers other than as json.<name>: `from json import loads`, `from json.decoder
    import JSONDecoder`, `import json as j`, `import json.decoder`."""
    if isinstance(node, ast.ImportFrom):
        return node.level == 0 and (node.module == "json" or str(node.module).startswith("json."))
    return isinstance(node, ast.Import) and any(a.name.startswith("json.") or a.name == "json" and a.asname
                                                for a in node.names)


def _json_parser_ref(node) -> bool:
    """A reference to one of json's parsers, called or not: json.loads(t), map(json.loads, ...), parse = json.loads,
    json.JSONDecoder() kept for later, json.decoder.JSONDecoder, and getattr(json, ...), whose name is any string."""
    if isinstance(node, ast.Attribute):
        return isinstance(node.value, ast.Name) and node.value.id == "json" and node.attr in PARSERS
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and \
        bool(node.args) and isinstance(node.args[0], ast.Name) and node.args[0].id == "json"


def raw_parses(package: Path):
    """-> ({(module, enclosing function): count of references to json's parsers}, [imports that would hide one]).
    core.load_json, the depth-bounded parser, is left out."""
    found, aliases = {}, []
    for f in sorted(package.rglob("*.py")):
        module = ".".join(f.relative_to(package).with_suffix("").parts)
        tree = ast.parse(f.read_text(), str(f))

        def visit(node, names):
            for child in ast.iter_child_nodes(node):
                inner = names
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    inner = names + [child.name]
                if _json_import(child):
                    aliases.append(f"{module}:{child.lineno}")
                if _json_parser_ref(child):
                    key = (module, ".".join(inner) or "<module>")
                    if key != ("core", "load_json"):
                        found[key] = found.get(key, 0) + 1
                visit(child, inner)
        visit(tree, [])
    return found, aliases


class RawParseGuardTests(unittest.TestCase):
    def test_every_raw_json_parse_is_core_load_json_or_reviewed(self):
        found, aliases = raw_parses(ROOT / "lib" / "devkit")
        self.assertEqual(aliases, [], "parse JSON with core.load_json, never json's parsers under another name")
        new = {k: n for k, n in found.items() if REVIEWED.get(k, (0,))[0] < n}
        self.assertEqual(new, {}, "parse JSON that a model, a teammate or another Mac wrote with core.load_json; a "
                                  "parse of text only devkit shapes needs an entry in REVIEWED with its reason")
        gone = {k: v[0] for k, v in REVIEWED.items() if found.get(k, 0) < v[0]}
        self.assertEqual(gone, {}, "these reviewed parses are gone or fewer: lower or drop their REVIEWED entries")

    def test_the_guard_sees_a_raw_parse_wherever_it_hides(self):
        with tempfile.TemporaryDirectory() as d:
            pkg = Path(d)
            (pkg / "sub").mkdir()
            (pkg / "a.py").write_text("import json\nX = json.loads('1')\n\ndef f(t):\n    return json.load(t)\n")
            (pkg / "sub" / "b.py").write_text("import json\n\nclass C:\n    def m(self, t):\n"
                                              "        def inner():\n            return json.JSONDecoder()\n"
                                              "        return json.loads(t), inner\n")
            (pkg / "core.py").write_text("import json\n\ndef load_json(t):\n    return json.loads(t)\n")
            (pkg / "c.py").write_text("from json import loads\nimport json as j\n")
            # json's parsers reached without a json.loads(...) call: its submodules, a reference passed or kept, a
            # name looked up at run time, and a decoder made once at import and used later
            (pkg / "d.py").write_text(
                "import json.decoder\nimport json.scanner\nfrom json.decoder import JSONDecoder as D\nimport json\n"
                "DEC = json.JSONDecoder()\nparse = json.loads\n\n"
                "def g(t):\n    return json.decoder.JSONDecoder().decode(t)\n\n"
                "def h(ts):\n    return list(map(json.loads, ts))\n\n"
                "def k(t, name):\n    return getattr(json, name)(t)\n\n"
                "def m(t):\n    return DEC.decode(t)\n")
            found, aliases = raw_parses(pkg)
        self.assertEqual(found, {("a", "<module>"): 1, ("a", "f"): 1, ("sub.b", "C.m"): 1,
                                 ("sub.b", "C.m.inner"): 1, ("d", "<module>"): 2, ("d", "g"): 1, ("d", "h"): 1,
                                 ("d", "k"): 1})
        self.assertEqual(aliases, ["c:1", "c:2", "d:1", "d:2", "d:3"])


if __name__ == "__main__":
    unittest.main()
