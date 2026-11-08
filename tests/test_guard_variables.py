# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# A variable the command itself sets from a literal (`py=python3; $py …`, `for P in python3 /usr/bin/python3; do $P
# …; done`) names a program or a path the guard reads before the shell runs (guard.literal_variants): the command is
# judged once per value, in the main session, a write lane and a read-only lane. A value from outside the command
# stays unknown.
# Run: python3 -m unittest tests.test_guard_variables -v
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

# the folder the commands run in: a project of the run's dev root (below its temporary home), so a lane's
# verdicts do not follow where this checkout lives (in a copy inside a session scratchpad the guard's
# scratch-folder rule would judge every path first)
CWD = str(core.DEV / "fireflysoftwaresolutions" / "devkit")
ENFORCE = {"safeguards": {"irreversible_main": "enforce", "irreversible_member": "enforce"}}

# recorded refusals: a for loop over two Pythons, an assignment, a quoted variable, a variable on a continued line
RECORDED = (
    'for py in python3 /usr/bin/python3; do echo "== $($py --version)"; $py -m unittest tests.test_guard 2>&1 | '
    'tail -3; done',
    "for P in /opt/homebrew/bin/python3 /usr/bin/python3; do echo \"== $P\"; $P -m unittest tests.test_crew_gates "
    "2>&1 | tail -3; done",
    "py=python3; $py -m unittest tests.test_guard",
    'C=/usr/bin/python3; "$C" --version',
    "py=/usr/bin/python3; $py -m unittest \\\n  tests.test_guard",
    "for PY in python3 /usr/bin/python3; do\n  $PY -m unittest tests.test_guard\ndone",
    "git status --short lib/devkit/guard.py; for py in python3 /usr/bin/python3; do echo \"== $py\"; "
    "$py -m unittest tests.test_guard 2>&1 | grep -E '^Ran|^OK'; done")

# the value comes from outside the command, or the guard cannot say which: still a program known only when it runs
UNKNOWN = (
    "$py -m unittest tests.test_guard", "read py; $py -m unittest tests.test_guard",
    "if false; then py=python3; fi; $py -m unittest tests.test_guard",
    "cd x && py=python3; $py -m unittest tests.test_guard", "py=python3 $py -m unittest tests.test_guard",
    "py=python3 & $py -m unittest tests.test_guard", "py=python3 | true; $py -m unittest tests.test_guard",
    'for P in python3 "$EVIL"; do $P -m unittest tests.test_guard; done',
    "for P in python3 /tmp/x*; do $P -m unittest tests.test_guard; done",
    "for P in python3 \\$X; do $P -m unittest tests.test_guard; done",
    "py=python3; py=$1; $py -m unittest tests.test_guard", "py=python3; ${py:-perl} -e 1",
    "py=python3; IFS=/; $py -m unittest tests.test_guard", "py=python3; eval py=perl; $py -e 1",
    "py=python3; declare -n r=py; r=perl; $py -e 1", "py=python3; (( py = 1 )); $py -e 1",
    "py=python3; (py=perl); $py -e 1", "py=python3; printf -v py perl; $py -e 1",
    'py="python3"; $py -m unittest tests.test_guard', "py=python3; f() { py=perl; }; f; $py -e 1",
    "for P in a b; do :; done; $P -m unittest tests.test_guard", "py=python3; $py[1] -e 1")


def member(cmd, level="all", who="kid", cwd=CWD):
    with mock.patch.object(core, "load_config", return_value=ENFORCE):
        v = guard.crew_shell_verdict(cmd, cwd, None, who, level=level, lane="a1", session="s1", grant=lambda *a: None)
    return v[1] if v else None


def main(cmd, cwd=CWD):
    with mock.patch.object(core, "load_config", return_value=ENFORCE):
        v = guard.irreversible_verdict(cmd, cwd, agent="claude", session="s1", grant=lambda *a: None)
    return v[1] if v else None


class LiteralVariables(unittest.TestCase):

    def test_a_recorded_variable_program_runs_in_every_context(self):
        for cmd in RECORDED:
            with self.subTest(cmd=cmd):
                self.assertTrue(guard.literal_variants(cmd))
                self.assertIsNone(main(cmd))
                self.assertIsNone(guard.check("Bash", {"command": cmd}, CWD, "claude"))
                self.assertIsNone(member(cmd))
                self.assertIsNone(member(cmd, level="review", who="seraph"))

    def test_each_value_of_a_loop_list_is_judged(self):
        self.assertEqual(guard.literal_variants("for P in python3 /usr/bin/python3; do $P x.py; done"),
                         ["for P in python3 /usr/bin/python3; do python3 x.py; done",
                          "for P in python3 /usr/bin/python3; do /usr/bin/python3 x.py; done"])
        # one value that is not a check refuses the whole command in a read-only lane
        self.assertIsNotNone(member("for P in python3 perl; do $P -m unittest tests.test_x; done", "review", "seraph"))
        self.assertIn("merges PR 12", member("for G in echo gh; do $G pr merge 12; done") or "")

    def test_a_value_from_outside_the_command_stays_unknown(self):
        for cmd in UNKNOWN:
            with self.subTest(cmd=cmd):
                self.assertEqual(guard.literal_variants(cmd), [])
                self.assertIn("only known when it runs", member(cmd) or "")
                self.assertIsNotNone(member(cmd, level="review", who="seraph"))
                self.assertIsNotNone(main(cmd))

    def test_a_literal_program_is_judged_as_if_it_were_written_out(self):
        # the variant is the command written out: an irreversible action in it is refused by name
        self.assertIn("merges PR 12", member("G=gh; $G pr merge 12") or "")
        self.assertIn("merges PR 12", main("G=gh; $G pr merge 12") or "")
        self.assertIsNotNone(guard.check("Bash", {"command": "G=gh; $G pr merge 12"}, CWD, "claude"))

    def test_a_literal_write_path_is_judged_where_it_points(self):
        with tempfile.TemporaryDirectory() as d:
            pad = Path(guard.real(d)) / "claude-501" / "-x" / "962d6aba-c6a8-44ab-9e02-1d15272468e1" / "scratchpad"
            (pad / "kid").mkdir(parents=True)
            (pad / "tank").mkdir()
            self.assertIsNone(member(f"F={pad}/kid/notes.md; echo hi > $F"))
            self.assertIn("scratchpad/kid/", member(f"F={pad}/tank/notes.md; echo hi > $F") or "")
            self.assertIsNone(member(f"S={pad}/seraph; mkdir -p $S/probes && ls $S", "review", "seraph"))
            self.assertIsNotNone(member(f"S={pad}/tank; mkdir -p $S/probes", "review", "seraph"))
        state = core.HOME / ".local" / "state" / "devkit" / "runs" / "x.json"
        self.assertEqual(guard.check("Bash", {"command": f"F={state}; echo hi > $F"}, CWD, "claude")[0], "deny")

    def test_a_script_run_through_a_literal_variable_is_read_like_the_script_written_out(self):
        # python3 $S/x.py used to go uninspected while python3 /path/x.py was read
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "x.py"
            state = core.HOME / ".local" / "state" / "devkit" / "runs" / "probe.json"
            script.write_text(f"open({str(state)!r}, 'w').write('x')\n")
            literal = guard.check("Bash", {"command": f"python3 {script}"}, CWD, "claude")
            via_var = guard.check("Bash", {"command": f"S={d}; python3 $S/x.py"}, CWD, "claude")
            self.assertEqual(literal and literal[0], "deny")
            self.assertEqual(via_var and via_var[0], "deny")

    def test_a_rebind_the_shell_runs_leaves_the_variable_unknown_and_its_twin_resolved(self):
        # each pair: the shell gives G another value than its literal (proven in zsh or bash with harmless twins), and
        # an honest command of the same shape the guard still reads as written out
        pairs = (
            ('G=echo; read "G" <<< gh; $G pr merge 12', 'G=echo; read -r line <<< gh; $G pr merge 12'),
            ('G=echo; typeset "G=gh"; $G pr merge 12', 'G=echo; export H=gh; $G pr merge 12'),
            ("G=echo; declare 'G'=gh; $G pr merge 12", "G=echo; echo 'H'=gh; $G pr merge 12"),
            ('G=echo; printf -v "G" gh; $G pr merge 12', "G=echo; printf '%s\\n' gh; $G pr merge 12"),
            ('G=echo; printf -vG gh; $G pr merge 12', 'G=echo; printf -- -x; $G pr merge 12'),
            ('G=echo; export "G=gh"; $G pr merge 12', 'G=echo; export H="gh x"; $G pr merge 12'),
            ('G=echo; readonly "G=gh"; $G pr merge 12', 'G=echo; unset H; $G pr merge 12'),
            ('G=echo; local "G=gh"; $G pr merge 12', 'G=echo; wait; $G pr merge 12'),
            ("G=echo; n=$'\\x47'; read \"$n\" <<< gh; $G pr merge 12", "G=echo; read -r n <<< gh; $G pr merge 12"),
            ('G=echo; n=G; read "$n" <<< gh; $G pr merge 12', 'G=echo; n=H; echo "$n"; $G pr merge 12'),
            ('py=echo; read p""y <<< gh; $py pr merge 12', 'py=echo; read p""z <<< gh; $py pr merge 12'),
            ('G=echo; "re"ad "$n" <<< gh; $G pr merge 12', 'G=echo; "ca"t "$n"; $G pr merge 12'),
            ('G=echo; \\read "$n" <<< gh; $G pr merge 12', 'G=echo; \\cat "$n"; $G pr merge 12'),
            ('G=echo; builtin read "$n" <<< gh; $G pr merge 12', 'G=echo; builtin cd /tmp; $G pr merge 12'),
            ('R=read; G=echo; $R "$n" <<< gh; $G pr merge 12', 'R=cat; G=echo; $R "$n"; $G pr merge 12'),
            ('REPLY=echo; read <<< gh; $REPLY pr merge 12', 'RE=echo; read -r x <<< gh; $RE pr merge 12'),
            ('_=echo; true gh; $_ pr merge 12', 'u=echo; true gh; $u pr merge 12'),
            ('MATCH=echo; [[ gh =~ gh ]]; $MATCH pr merge 12', 'M=echo; [[ gh =~ gh ]]; $M pr merge 12'),
            ('BASH_REMATCH=echo; [[ gh =~ gh ]]; $BASH_REMATCH pr merge 12', 'B=echo; [[ gh =~ gh ]]; $B pr merge 12'),
            ('G=echo; : ${!n}; $G pr merge 12', 'G=echo; : ${n}; $G pr merge 12'),
            ('G=echo; : ${(P)n::=gh}; $G pr merge 12', 'G=echo; : $n; $G pr merge 12'),
            ('G=echo; declare -n r=$n; r=gh; $G pr merge 12', 'G=echo; r=gh; $G pr merge 12'),
            ('G=echo; trap "$n" DEBUG; $G pr merge 12', 'G=echo; true "$n"; $G pr merge 12'),
            ('G=echo; wait -p "$n"; $G pr merge 12', 'G=echo; wait %1; $G pr merge 12'),
            ('G=echo; x=$n; : $(( x )); $G pr merge 12', 'G=echo; x=$n; : $x; $G pr merge 12'),
            ('G=echo; mapfile -t G <<< gh; $G pr merge 12', 'G=echo; cat <<< gh; $G pr merge 12'),
            ('G=echo; typeset -g G=gh; $G pr merge 12', 'G=echo; set -euo pipefail; $G pr merge 12'),
            ('G=echo; set -A G gh; $G pr merge 12', 'G=echo; set -e; $G pr merge 12'),
            ('G=echo; vared G; $G pr merge 12', 'G=echo; true; $G pr merge 12'),
            ('G==gh; setopt globsubst; $G pr merge 12', 'G=echo; true; $G pr merge 12'),
            ('G=echo; 2>/dev/null . /tmp/g.sh; $G pr merge 12', 'G=echo; ls 2>/dev/null .; $G pr merge 12'),
            ('G=echo; repeat 1 . /tmp/g.sh; $G pr merge 12', 'G=echo; find . -name x; $G pr merge 12'),
            ('G=echo; command -p . /tmp/g.sh; $G pr merge 12', 'G=echo; command -v ls; $G pr merge 12'),
            ('G=echo; for G in $(echo gh); do $G pr merge 12; done', 'for G in echo true; do $G pr merge 12; done'),
            ('G=echo; while read G; do $G pr merge 12; done <<< gh', 'G=echo; while read -r x; do :; done; $G pr x'),
            ('G=echo; bash -c "read G" && $G pr merge 12', 'G=echo; bash -c "read H" && $G pr x'),
        )
        for bad, twin in pairs:
            with self.subTest(cmd=bad):
                self.assertEqual(guard.literal_variants(bad), [])
                self.assertIn("only known when it runs", main(bad) or "")
                self.assertIn("only known when it runs", member(bad) or "")
            with self.subTest(twin=twin):
                self.assertTrue(guard.literal_variants(twin))
        # a quote in a comment hides no word from the variable rule; the guard refuses the unclosed quote itself
        hidden = "G=echo; # it's\nread \"$n\" <<< gh; $G pr merge 12"
        self.assertEqual(guard.literal_variants(hidden), [])
        self.assertEqual(guard.check("Bash", {"command": hidden}, CWD, "claude")[0], "deny")
        # a read-only lane: its quoted rebind to an interpreter stays unknown, the twin reads a file
        self.assertIn("variable", member('s=cat; read "s" <<< python3; $s /tmp/x.py', "review", "seraph") or "")
        self.assertIsNone(member('s=cat; read -r t <<< python3; $s /tmp/x.py', "review", "seraph"))

    def test_the_honest_forms_the_crew_needs_are_read_as_written_out(self):
        # the loop over two Pythons, a scratch path named once, a folder written out for cd; and the name inside a
        # longer word or after a backslash (\n in a jq filter) is no mention
        self.assertEqual(guard.literal_variants("S=/tmp/k; python3 $S/x.py"), ["S=/tmp/k; python3 /tmp/k/x.py"])
        self.assertEqual(guard.literal_variants('X=/elsewhere; cd "$X" && dev push'),
                         ['X=/elsewhere; cd "/elsewhere" && dev push'])
        self.assertEqual(len(guard.literal_variants("for n in 16 17; do gh pr view $n --jq '\"\\(.t)\\n\"'; done")), 2)
        self.assertEqual(len(guard.literal_variants(
            "unset CLAUDECODE; for py in python3 /usr/bin/python3; do $py -m unittest tests.test_guard; done")), 2)
        # `((` in quoted text is text, and zsh's ${=x} only splits x: neither reaches another variable
        self.assertTrue(guard.literal_variants('S=/tmp/k; node -e "f(() => 1)" > $S/out'))
        self.assertTrue(guard.literal_variants("D=/tmp/k/devfs; for a in x y; do $D ${=a}; done"))

    def test_too_many_combinations_stay_unknown(self):
        cmd = "; ".join(f"for V{k} in a b c; do $V{k} x; done" for k in range(3))
        self.assertEqual(guard.literal_variants(cmd), [])  # 27 > MAX_VARIANTS


if __name__ == "__main__":
    unittest.main()
