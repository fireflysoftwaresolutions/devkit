# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""devkit's process applies to development turns only (lib/devkit/scope.py)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from devkit import scope  # noqa: E402

DEV, OTHER = scope.DEV, scope.OTHER


class ClassifyTests(unittest.TestCase):
    def test_everyday_requests_are_not_development(self):
        for text in ("Dame una receta de tortilla de patatas", "What's a good recipe for banana bread?",
                     "¿Qué vino va bien con una paella?", "Plan a 3-day trip to Lisbon"):
            self.assertEqual(scope.classify(text), OTHER, text)

    def test_consulting_deliverables_are_not_development(self):
        for text in ("Hazme un deck para Example Bank sobre su estrategia de pagos",
                     "Prepare a proposal for Example Bank with the consulting kit",
                     "Revisa la presentación del workshop del jueves",
                     "a deck about our API strategy for the board"):
            self.assertEqual(scope.classify(text), OTHER, text)

    def test_development_requests(self):
        for text in ("Fix the failing test in lib/devkit/scope.py", "arregla el bug del renderer de decks",
                     "el build falla con un Traceback en main.py", "despliega la versión 26.11.0",
                     "refactor the payment service", "¿por qué falla el pipeline de CI?",
                     "add a CSV export endpoint to the invoices API"):
            self.assertEqual(scope.classify(text), DEV, text)

    def test_follow_ups_inherit_the_previous_class(self):
        for text in ("continúa y termina todo", "sí", "ok, go ahead", "perfecto"):
            self.assertIsNone(scope.classify(text), text)
        self.assertEqual(scope.resolve("continúa", DEV), DEV)
        self.assertEqual(scope.resolve("continúa", OTHER), OTHER)
        self.assertEqual(scope.resolve("now fix the bug in the parser", OTHER), DEV)

    def test_spanish_and_or_is_not_a_path(self):
        self.assertEqual(scope.classify("recetas de cocina y/o postres"), OTHER)


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
        self.assertTrue(scope.is_dev_turn(DEV, nothing))  # a question about the code, answered without tools
        self.assertFalse(scope.is_dev_turn(OTHER, nothing))  # a recipe
        self.assertFalse(scope.is_dev_turn(None, nothing))  # "what's the weather?"
        self.assertTrue(scope.is_dev_turn(OTHER, commit))  # it said deck, but it committed code
        self.assertFalse(scope.is_dev_turn(None, deck))  # consulting work through the kit's skills
        self.assertTrue(scope.is_dev_turn(None, dict(nothing, commands=["git status"])))

    def test_decide_reads_the_session_and_transcript(self):
        p = self._t([_claude_prompt("dame una receta"), _claude_tool("WebSearch", {"query": "receta"})])
        self.assertFalse(scope.decide({"transcript_path": p}, {"turn_class": OTHER}))
        self.assertFalse(scope.decide({"transcript_path": p}, {"turn_class": None}))  # outside a repository
        self.assertTrue(scope.decide({"transcript_path": p}, {"turn_class": None, "top": "/repo"}))  # "how is it going?"
        self.assertFalse(scope.decide({"transcript_path": p}, {"turn_class": OTHER, "top": "/repo"}))  # a recipe in a repo
        self.assertTrue(scope.decide({"transcript_path": p}, {"turn_class": DEV}))
        os.environ["DEVKIT_SCOPE"] = "all"
        try:
            self.assertTrue(scope.decide({"transcript_path": p}, {"turn_class": OTHER}))
        finally:
            del os.environ["DEVKIT_SCOPE"]


if __name__ == "__main__":
    unittest.main()
