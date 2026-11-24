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

from devkit import (audit, branches, bugs, canary, ciwatch, continuity, core, gates, gc, language,  # noqa: E402
                    lessonlint, lessons, mcp, policy, scope, shellparse, skillguard, state)
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
HELD = "OPEN: routed through core.load_json by the change that owns progress.py, hooks.py and codexlaunch.py"
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
    ("codexlaunch", "lifecycle_events"): (1, HELD),
    ("codexlaunch", "session_meta"): (1, HELD),
    ("codexlaunch", "spawn_calls"): (3, HELD),
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
    ("hooks", "_capture_stdin"): (1, HELD),
    ("hooks", "_codex_version"): (1, HELD),
    ("hooks", "_devkit_env_change"): (1, HELD),
    ("hooks", "_item_record"): (1, HELD),
    ("hooks", "_own_identity"): (1, HELD),
    ("hooks", "_stdin_json"): (1, HELD),
    ("hooks", "codex_rollout"): (1, HELD),
    ("hooks", "sessions_to_restart"): (1, HELD),
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
    ("progress", "_codex_meta"): (1, HELD),
    ("progress", "_codex_midturn_requests"): (1, HELD),
    ("progress", "_last_assistant_text_ts"): (1, HELD),
    ("progress", "midturn_requests"): (1, HELD),
    ("progress", "records"): (1, HELD),
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
PARSERS = ("load", "loads", "JSONDecoder")


def raw_parses(package: Path):
    """-> ({(module, enclosing function): count of json.load/loads/JSONDecoder calls}, [aliases that would hide one]):
    `from json import loads`, `import json as j`. core.load_json, the depth-bounded parser, is left out."""
    found, aliases = {}, []
    for f in sorted(package.rglob("*.py")):
        module = ".".join(f.relative_to(package).with_suffix("").parts)
        tree = ast.parse(f.read_text(), str(f))

        def visit(node, names):
            for child in ast.iter_child_nodes(node):
                inner = names
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    inner = names + [child.name]
                if isinstance(child, ast.ImportFrom) and child.module == "json" or \
                        isinstance(child, ast.Import) and any(a.name == "json" and a.asname for a in child.names):
                    aliases.append(f"{module}:{child.lineno}")
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and \
                        isinstance(child.func.value, ast.Name) and child.func.value.id == "json" and \
                        child.func.attr in PARSERS:
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
            found, aliases = raw_parses(pkg)
        self.assertEqual(found, {("a", "<module>"): 1, ("a", "f"): 1, ("sub.b", "C.m"): 1,
                                 ("sub.b", "C.m.inner"): 1})
        self.assertEqual(aliases, ["c:1", "c:2"])


if __name__ == "__main__":
    unittest.main()
