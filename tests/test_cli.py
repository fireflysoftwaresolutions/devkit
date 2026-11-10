# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# What `dev` itself tells the reader to type: a command it teaches must be one devkit accepts as taught.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_cli
import contextlib
import io
import json
import re
import shlex
import sys
import unittest
from pathlib import Path

TREE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TREE / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import cli, lessonlint  # noqa: E402
import test_crew_gates as crew_gates  # noqa: E402  (the gates' transcript fixture; a module, so its tests run once)
from test_lessons_base import LessonCase  # noqa: E402

# The one form of `dev lesson add` that every place teaches (the agent profiles, the hooks' hint, AGENTS.md)
LESSON_ADD = ('dev lesson add "…" --happened "…" --cause "…" --prevent "…" --file <glob> '
              '--command "<command words>" --error "<output text>"')
TAUGHT = re.compile(r"`(dev lesson add [^`]*)`")  # a command taught in running text, between backticks


def refusals(taught: str) -> list:
    # what the lesson shape check says of the command as taught: [] when devkit accepts it
    a = cli.build_parser().parse_args(shlex.split(taught)[1:])
    return lessonlint.check_shape(" ".join(a.title), a.happened, a.evidence or [])


def strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from strings(v)


class LessonHintTests(LessonCase):
    def test_an_empty_lesson_list_teaches_the_add_command_its_shape_check_accepts(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["lesson", "ls"])
        self.assertEqual(rc, 0)
        line = next((ln for ln in out.getvalue().splitlines() if "dev lesson add" in ln), "")
        taught = line[line.find("dev lesson add"):]
        self.assertEqual(taught, LESSON_ADD, out.getvalue())
        self.assertEqual(refusals(taught), [])

    # a crew member's brief with known pitfalls says how to record a new one: the same command as the hint
    def test_a_crew_brief_with_known_pitfalls_teaches_the_same_add_command(self):
        from devkit import skillguard
        self.add("A regex rewrite broke the parser", activities=["feature"], files=["src/parser/**"])
        out = skillguard.on_agent_brief("claude", {"description": "Neo: fix the parser",
                                                   "prompt": "Fix the parser after the rewrite."}, "acme/api")
        self.assertIn("## devkit · known pitfalls", out["prompt"])
        self.assertEqual([LESSON_ADD], TAUGHT.findall(out["prompt"]))

    # the global instructions, every crew member's profile and the crew's configuration teach only commands the
    # shape check accepts
    def test_the_instructions_and_the_crew_teach_an_add_command_the_shape_check_accepts(self):
        from devkit import crew
        r = crew.load(team=False)
        texts = [("agents/AGENTS.md", (TREE / "agents" / "AGENTS.md").read_text())]
        texts += [(f"the profile of {m['id']}", crew.body(m, r)) for m in crew.members(r, include_main=False)]
        texts += [("config/crew.json", t) for t in strings(json.loads((TREE / "config" / "crew.json").read_text()))]
        taught = [(where, t) for where, text in texts for t in TAUGHT.findall(text)]
        self.assertIn("agents/AGENTS.md", {where for where, _ in taught})
        self.assertIn("the profile of tank", {where for where, _ in taught})
        for where, t in taught:
            with self.subTest(where=where, taught=t):
                self.assertEqual(refusals(t), [])


class GateHintTests(crew_gates.GateCase):
    # a crew member answered two guard refusals with no lesson: the gate's fix teaches a command the check accepts
    def test_the_gate_after_two_guard_refusals_teaches_an_add_command_the_shape_check_accepts(self):
        self.complete()
        self.tx.refused("git worktree add ../copy").refused("git worktree add ../copy2")
        fixes = [f["fix"] for f in self.gates() if f["rule"] == "lessons"]
        taught = [t for fix in fixes for t in TAUGHT.findall(fix)]
        self.assertEqual(1, len(taught), fixes)
        self.assertEqual(refusals(taught[0]), [])


if __name__ == "__main__":
    unittest.main()
