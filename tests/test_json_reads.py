# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""devkit parses JSON that a model, a teammate, another Mac or another session wrote with core.load_json, which reads a
value nested deeper than core.JSON_MAX_DEPTH as unreadable. A transcript, a Codex rollout and a hook's input hold
JSON built from a model's tool input, so a tool input nested a few thousand levels deep reached the parser: Python 3.9
to 3.13 raise RecursionError at about a thousand levels, Python 3.15 at about 150,000, and the readers caught only
ValueError, so the Stop hook and the other readers crashed.

Each reader test feeds one reader a deep document and checks that the reader treats it as it treats any unreadable
one, and still reads the records around it. Every file here is temporary. Run: python3 -m unittest
tests.test_json_reads (under /usr/bin/python3 too, for Python 3.9).
"""
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
sys.path.insert(0, str(ROOT / "lib"))

from devkit import (audit, canary, continuity, core, gates, language, lessonlint, lessons, scope,  # noqa: E402
                    skillguard, state)

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


if __name__ == "__main__":
    unittest.main()
