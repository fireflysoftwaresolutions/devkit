# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# How the guard reads the words of a command line before its rules judge them. zsh runs the command after its
# precommand modifiers (`noglob`, `nocorrect`, `-`) as it runs one after `builtin` or `command`, so the rules judge that
# command (a `-c` line cannot start with `-`: bash and zsh read it as a bad option, so `-` comes after a first
# command here). Each refused form has an allowed twin: the same wrapper around a command no rule refuses. Every case
# is judged as Claude Code's Bash and Codex's exec_command and shell (bash -lc and zsh -c) send it. No command runs.
# Run: python3 -m unittest tests.test_guard_parser -v
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
sys.path.insert(0, str(ROOT / "lib"))

from devkit import guard  # noqa: E402

CWD = str(ROOT)
MARKER = "CLAUDE" + "CODE"  # built here: the agent marker the identity rule protects


def payloads(cmd):
    """The same command as each agent sends it: Claude Code's Bash, Codex's exec_command and its shell tool."""
    return [("Bash", {"command": cmd}, "claude"), ("exec_command", {"cmd": cmd}, "codex"),
            ("shell", {"command": ["bash", "-lc", cmd]}, "codex"), ("shell", {"command": ["zsh", "-c", cmd]}, "codex")]


class Rulings(unittest.TestCase):

    def verdicts(self, cmd):
        out = {}
        for tool, inp, agent in payloads(cmd):
            v = guard.check(tool, inp, CWD, agent)
            out[f"{tool}:{inp.get('command', [''])[0] if tool == 'shell' else ''}"] = v[0] if v else "allow"
        return out

    def assert_rulings(self, refused, allowed):
        for cmd in refused:
            with self.subTest(refused=cmd):
                got = self.verdicts(cmd)
                self.assertEqual(set(got.values()), {"deny"}, got)
        for cmd in allowed:
            with self.subTest(allowed=cmd):
                got = self.verdicts(cmd)
                self.assertEqual(set(got.values()), {"allow"}, got)


class ZshPrecommands(Rulings):

    def test_the_command_after_noglob_nocorrect_or_a_dash_is_judged(self):
        self.assert_rulings(
            refused=["noglob rm -rf ~/Development", "nocorrect rm -rf ~/Development", "true; - rm -rf ~/Development",
                     "noglob nocorrect - builtin rm -rf ~/Development", "noglob git push --force origin feat/x",
                     "nocorrect git push -f origin feat/x", f"noglob unset {MARKER}", f"true && - unset {MARKER}",
                     "echo ok && noglob rm -rf ~/Development"],
            allowed=["noglob ls ~/Development", "nocorrect git status", "true; - echo hi", "noglob unset FOO",
                     "noglob git log -1", "echo ok && nocorrect ls"])


if __name__ == "__main__":
    unittest.main()
