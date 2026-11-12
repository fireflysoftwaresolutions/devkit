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


class LiteralValues(Case):
    """A variable the command sets from a literal, a `~` path or a choice between two literal echoes names a program
    the guard reads before it runs."""

    def test_a_home_path_or_a_choice_of_literals_runs(self):
        for cmd in ("D=~/Development/x/bin/dev; $D bug --help 2>&1 | head -30",
                    "PY=$( [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3 ); $PY -m unittest x",
                    "PY=$(test -x .venv/bin/python && echo .venv/bin/python || echo python3); $PY --version"):
            self.runs(cmd)

    def test_a_loop_next_to_a_quoted_heredoc_runs(self):
        edit = "python3 - <<'EOF'\nfrom pathlib import Path\nprint('do $py and $x')\nEOF\n"
        self.runs(edit + "for py in python3 /usr/bin/python3; do $py -m unittest tests.test_x; done")
        self.runs("cat > notes.txt <<'EOF'\n$py here\nEOF\npy=python3; $py --version")
        self.refused(edit + "for g in gh; do $g pr merge 12; done")
        # the shell expands an unquoted heredoc's body: the variables stay unknown
        self.refused("cat <<EOF\n$py\nEOF\nfor py in python3; do $py -c 1; done; $py -c 2")
        self.assertEqual([], guard.literal_variants("bash <<EOF\n$g pr merge 12\nEOF\ng=gh"))

    def test_a_value_that_names_an_irreversible_program_is_refused(self):
        for cmd in ("G=~/bin/gh; $G pr merge 12", "G=$( [ -x x ] && echo gh || echo python3 ); $G pr merge 12",
                    "G=$( [ -x x ] && echo python3 || echo gh ); $G pr merge 12",
                    "G=$( [ -x x ] && echo $(cat f) || echo python3 ); $G -c 1",
                    "G=$( [ -x x ] && gh pr merge 1 || echo python3 ); $G -c 1"):
            self.refused(cmd)


if __name__ == "__main__":
    unittest.main()
