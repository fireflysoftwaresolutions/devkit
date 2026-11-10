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


class SubstitutionsInsideACommand(Rulings):
    """An unquoted `$(…)`, `$((…))`, `<(…)`, `>(…)` or `${…$(…)}` is one word of the command around it: the words after
    it belong to the same command (`rm $(true) -rf ~/Development` runs rm -rf), and a `$(…)` may also expand to
    nothing. Each refused command has a twin with a substitution in the middle, refused too; commands that use
    substitutions for normal work stay allowed."""

    # (the words before the substitution, the words after it, whether one more word there keeps the command's meaning)
    REFUSED = [("rm", "-rf ~/Development", True), ("git push", "--force origin feat/x", True),
               ("dev", "install", False), ("unset", MARKER, True)]

    def test_a_substitution_inside_a_refused_command_hides_none_of_its_words(self):
        cmds = []
        for head, rest, one_more_word in self.REFUSED:
            cmds += [f"{head} {rest}", f"{head} $(true) {rest}", f"{head} `true` {rest}", f"{head} ${{X:-$(true)}} {rest}",
                     f"echo a; {head} $(true) {rest}", f"sh -c '{head} $(true) {rest}'"]
            if one_more_word:  # these always expand to one word: `git <(true) push` runs no push
                cmds += [f"{head} <(true) {rest}", f"{head} $((1)) {rest}"]
        # the twins: normal work with substitutions, and a heredoc body that only holds such a line, stay allowed
        self.assert_rulings(refused=cmds, allowed=[
            "echo $(date)", "cd $(git rev-parse --show-toplevel) && git status", "git log $(git merge-base HEAD main)",
            "export PATH=$(pwd)/bin:$PATH", "diff <(ls lib) <(ls tests)", "echo $((1 + 2))", "ls $(dirname lib/x)",
            "git diff --stat $(git merge-base HEAD origin/main)..HEAD", 'echo "$(git log -1 --format=%s)"',
            "cat <<'EOF' > /tmp/devkit-parser-note.txt\nrm $(true) -rf ~/Development\nEOF",
            "cat <<EOF\n$(date)\nEOF"])


class AwkComparisonsAndRedirections(unittest.TestCase):
    """In a read-only lane an awk program may only print to standard output. awk reads a `>` as a redirection only
    at the top level of a print or printf statement; anywhere else it compares (`NR>=2050`), so it reads."""

    def lane(self, cmd, level):
        v = guard.crew_shell_verdict(cmd, CWD, None, "seraph", level=level)
        return v[1] if v else None

    def test_a_comparison_reads_and_a_redirection_or_pipe_stays_refused(self):
        reads = ["awk 'NR>=2050 && NR<=2100' lib/devkit/guard.py", "awk 'NR>2050' f", "awk '$3 > 500 {print $1}' f",
                 "awk '{print (a > b)}' f", "awk '$1 ~ /a>b/ {print}' f", "awk '{ n = i++ / 2; print n }' f",
                 "awk 'NR>1 || /x/' f", "awk '{print \"a>b|c\"}' f", "awk '# a > b\n{print}' f",
                 "awk '{ for (i = 1; i <= NF; i++) if ($i > 0) print $i }' f", "awk '{ if (a) /x/; print }' f",
                 "awk '{ print $1,\n$2 }' f", "awk '{ print $1\n$2 > 0 }' f"]
        writes = ["awk '{print > \"/tmp/out\"}' f", "awk '{print $1 >> \"f\"}' f", "awk '{printf(\"%d\", NR) > \"f\"}' f",
                  "awk '{print | \"sh\"}' f", "awk '{ x = i++ / 2; print > \"f\"; y = 1/2 }' f",
                  "awk '{ if (x) print; else print > \"f\" }' f", "awk '{ x = /\"/; print > \"f\" }' f",
                  "awk '$0 ~ /[/]\"/ { print > \"f\" }' f", "awk '{print \"unterminated}' f",
                  "awk '{ if (a) /\"/; print > \"f\"; if (b) /\"/ }' f",
                  "awk '{ for (;;) /\"/; print > \"f\"; for (;;) /\"/ }' f",
                  "awk 'NR>=2050 {print > \"out\"}' f",
                  # awk reads a print on past a newline after `,`, `&&` or `||` (each wrote its file in /usr/bin/awk)
                  "awk '{ print $1,\n$2 > \"f\" }' f", "awk '{ printf \"%s\",\n$1 > \"f\" }' f",
                  "awk '{ print 1 ||\n0 > \"f\" }' f", "awk '{ print 1 &&\n1 > \"f\" }' f"]
        for level in ("review", "read"):
            for cmd in reads:
                with self.subTest(level=level, reads=cmd):
                    self.assertIsNone(self.lane(cmd, level))
            for cmd in writes:
                with self.subTest(level=level, writes=cmd):
                    self.assertIn("awk program writes", self.lane(cmd, level) or "")


if __name__ == "__main__":
    unittest.main()
