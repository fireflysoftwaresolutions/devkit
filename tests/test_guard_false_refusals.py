# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Forms of honest work the guard refused in recorded sessions, written here as synthetic commands: each must pass in
# the context it was refused in (the main session, a crew member's write lane, a read-only lane), and each has a twin
# that carries the risk the refusing rule guards against and stays refused. tools/guard_replay.py measures the same
# classes on this Mac's own transcripts.
# Run: python3 -m unittest tests.test_guard_false_refusals -v
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
sys.path.insert(0, str(ROOT / "lib"))

from devkit import core, guard  # noqa: E402

# the folder the commands run in: a project of the run's dev root (below its temporary home)
CWD = str(core.DEV / "fireflysoftwaresolutions" / "devkit")
ENFORCE = {"safeguards": {"irreversible_main": "enforce", "irreversible_member": "enforce"}}


def member(cmd, level="all", who="kid", cwd=CWD):
    """The refusal of a crew member's command in its lane (crew_shell_verdict), or None."""
    with mock.patch.object(core, "load_config", return_value=ENFORCE):
        v = guard.crew_shell_verdict(cmd, cwd, None, who, level=level, lane="a1", session="s1", grant=lambda *a: None)
    return v[1] if v else None


def main(cmd, cwd=CWD):
    """The refusal of the main session's command: the guard's rules, then the irreversible-action gate."""
    with mock.patch.object(core, "load_config", return_value=ENFORCE):
        v = guard.check("Bash", {"command": cmd}, cwd, "claude") or \
            guard.irreversible_verdict(cmd, cwd, agent="claude", session="s1", grant=lambda *a: None)
    return v[1] if v else None


class Case(unittest.TestCase):

    def runs(self, cmd, **kw):
        with self.subTest(cmd=cmd, **kw):
            self.assertIsNone(member(cmd, **kw))

    def refused(self, cmd, **kw):
        with self.subTest(cmd=cmd, **kw):
            self.assertIsNotNone(member(cmd, **kw))


class Arithmetic(Case):
    """`$((…))` is arithmetic when its inner parenthesis closes into `))`; the shell runs only the substitutions in
    it, and reads each variable's value as an expression (`x='a[$(cmd)]'; echo $((x))` runs cmd)."""

    def test_arithmetic_on_numbers_runs(self):
        for cmd in ("n=3; echo $((n*2))", "echo $((1 + 2))", 's=$(date +%s); true; echo "took $(( $(date +%s) - s )) s"',
                    "sed -n \"$(( $(grep -n x lib/a.py | cut -d: -f1) + 2 )),+3p\" lib/a.py",
                    "n=0; for f in a b; do n=$((n+1)); done; echo $n", "for i in 1 2 3; do echo $((i * 2)); done",
                    "c=$(wc -l < lib/a.py); echo $((c / 2))"):
            self.runs(cmd)

    def test_a_command_inside_arithmetic_is_judged(self):
        for cmd in ("echo $(( $(gh pr merge 12) + 1 ))", "echo $(( `gh pr merge 12` + 1 ))",
                    'echo "$(( $(git push --force origin main) ))"'):
            self.refused(cmd)

    def test_a_value_read_as_an_expression_that_may_hold_code_is_refused(self):
        for cmd in ("x='a[$(gh pr merge 12)]'; echo $((x))", "read x; echo $((x + 1))", "x=$(cat f.txt); echo $((x))",
                    "echo $((x))", "printf -v x %s y; echo $((x))", "for x in $(cat f); do echo $((x)); done",
                    "x=1; declare x; echo $((x))"):
            self.refused(cmd)

    def test_a_parenthesis_that_does_not_close_into_two_is_a_command_substitution(self):
        # bash and zsh run `$((date)|wc -c)` as `$( (date) | wc -c )`
        self.runs("echo $((date)|wc -c)")
        self.refused("echo $((gh pr merge 12)|wc -c)")


class CaseArms(Case):
    """A `case W in P)` header runs no command; its subject is data, and the substitutions in it are judged."""

    def test_a_case_on_a_variable_runs(self):
        for cmd in ("f=a; case $f in a) echo A;; b) echo B;; esac",
                    "for f in a b; do case $f in a) echo A;; esac; done",
                    'for f in $(ls); do case "$f" in *.py) wc -l "$f";; esac; done',
                    "while read f; do case $f in a) echo A;; esac; done < x.txt"):
            self.runs(cmd)

    def test_a_command_in_a_case_arm_or_subject_is_judged(self):
        for cmd in ("f=a; case $f in a) gh pr merge 12;; esac", "case $(gh pr merge 12) in a) echo A;; esac",
                    "for f in a; do case $f in a) git push --force origin main;; esac; done"):
            self.refused(cmd)


if __name__ == "__main__":
    unittest.main()
