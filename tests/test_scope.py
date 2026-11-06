# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""devkit's process applies to development turns only, decided from what a turn did (lib/devkit/scope.py)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from devkit import scope  # noqa: E402


def _transcript(records) -> str:
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in records))
    return path


def _claude_prompt(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _claude_tool(name, inp):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": name,
                                                                                "input": inp}]}}


class TurnActionTests(unittest.TestCase):
    def tearDown(self):
        for p in getattr(self, "paths", []):
            os.unlink(p)

    def _t(self, records):
        p = _transcript(records)
        self.paths = getattr(self, "paths", []) + [p]
        return p

    def test_only_the_last_turn_counts(self):
        p = self._t([_claude_prompt("fix it"), _claude_tool("Bash", {"command": "git commit -m fix"}),
                     {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
                     _claude_prompt("a recipe please"), _claude_tool("WebSearch", {"query": "tortilla"})])
        acts = scope.turn_actions(p)
        self.assertEqual(acts["commands"], [])
        self.assertEqual(acts["tools"], {"WebSearch"})

    def test_codex_shell_and_patch_calls(self):
        p = self._t([{"type": "event_msg", "payload": {"type": "user_message", "message": "run the tests"}},
                     {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
                                                           "arguments": json.dumps({"cmd": "pytest -q"})}},
                     {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch",
                                                           "input": "*** Begin Patch\n*** Update File: src/a.py\n"}}])
        acts = scope.turn_actions(p)
        self.assertEqual(acts["commands"], ["pytest -q"])
        self.assertEqual(acts["edits"], ["src/a.py"])

    def test_dev_turn_decisions(self):
        nothing = {"edits": [], "commands": [], "skills": [], "tools": set()}
        commit = dict(nothing, commands=["git commit -m 'fix: x'"])
        deck = dict(nothing, skills=["firefly-consulting-desktop:firefly-deck"], commands=["ls decks"])
        self.assertTrue(scope.is_dev_turn(nothing, declared=True))  # a question about the code, with the status block
        self.assertFalse(scope.is_dev_turn(nothing))  # a recipe, or "what's the weather?"
        self.assertFalse(scope.is_dev_turn(nothing, in_repo=True))  # no tools used, nothing declared
        self.assertTrue(scope.is_dev_turn(commit))  # it committed code
        self.assertFalse(scope.is_dev_turn(deck, in_repo=True))  # consulting work through the kit's skills
        self.assertTrue(scope.is_dev_turn(dict(deck, commands=["git push"])))  # unless it ships code
        self.assertTrue(scope.is_dev_turn(dict(nothing, commands=["git status"])))
        self.assertTrue(scope.is_dev_turn(dict(nothing, tools={"Grep"}), in_repo=True))

    def test_decide_reads_the_session_and_transcript(self):
        p = self._t([_claude_prompt("dame una receta"), _claude_tool("WebSearch", {"query": "receta"})])
        self.assertFalse(scope.decide({"transcript_path": p}, {}))
        self.assertFalse(scope.decide({"transcript_path": p}, {"top": "/repo"}))  # a web search is no repository work
        q = self._t([_claude_prompt("how is it going?"),
                     {"type": "assistant", "message": {"role": "assistant", "content": [
                         {"type": "text", "text": "**[Firefly> DevKit]** api · `main` · Status\n- **Done:** x"}]}}])
        self.assertTrue(scope.decide({"transcript_path": q}, {"top": "/repo"}))  # the block declares it
        os.environ["DEVKIT_SCOPE"] = "all"
        try:
            self.assertTrue(scope.decide({"transcript_path": p}, {}))
        finally:
            del os.environ["DEVKIT_SCOPE"]


if __name__ == "__main__":
    unittest.main()
