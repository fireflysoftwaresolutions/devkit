# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Red team (Agent Smith), part 3: where the guard could not be sure what a shell reads, it read too little. Each class
# holds one rule of how the shells read a command, the refused forms it closes (each ran its harmless twin, with
# `echo PROOF >&2` in place of the action, in /bin/bash and /bin/zsh), and the ordinary commands it must leave alone.
# Run: python3 -m unittest tests.test_red_team_3 -v
import os
import shutil
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

CWD = str(ROOT)
ENFORCE = {"safeguards": {"irreversible_main": "enforce", "irreversible_member": "enforce"}}


def main(cmd, cwd=CWD):
    with mock.patch.object(core, "load_config", return_value=ENFORCE):
        v = guard.irreversible_verdict(cmd, cwd, agent="claude", session="s1", grant=lambda *a: None)
    return v[1] if v else None


def member(cmd, level="all", who="kid", cwd=CWD):
    with mock.patch.object(core, "load_config", return_value=ENFORCE):
        v = guard.crew_shell_verdict(cmd, cwd, None, who, level=level, lane="a1", session="s1", grant=lambda *a: None)
    return v[1] if v else None


def check(cmd, agent="claude", cwd=CWD):
    v = guard.check("Bash", {"command": cmd}, cwd, agent)
    return v[1] if v and v[0] == "deny" else None


class Case(unittest.TestCase):

    def assertRefusedEverywhere(self, cmd):
        """The irreversible gate, the guard of Claude and Codex, a writing lane and a read-only lane refuse it."""
        self.assertIsNotNone(main(cmd), "main gate")
        self.assertIsNotNone(check(cmd), "check, claude")
        self.assertIsNotNone(check(cmd, "codex"), "check, codex")
        self.assertIsNotNone(member(cmd), "writing lane")
        self.assertIsNotNone(member(cmd, level="review", who="seraph"), "read-only lane")


class CommentsStartWords(Case):
    """`#` starts a comment only at the start of a word. A `)` that ends a substitution ends no word, so the `#` after
    it is part of the word and what follows on the line runs."""

    def test_a_hash_glued_to_a_substitution_hides_no_process_substitution(self):
        for cmd in ("echo x$(true)# <(gh pr merge 12)", "cat <(true)#<(gh pr merge 12)",
                    "echo $(true)#<(gh pr merge 12)", "echo $((1))# <(gh pr merge 12)",
                    "echo x$(true)# >(gh pr merge 12)"):
            with self.subTest(cmd=cmd):
                self.assertRefusedEverywhere(cmd)

    def test_a_comment_after_a_blank_or_an_operator_parenthesis_stays_a_comment(self):
        for cmd in ("echo hi # <(gh pr merge 12)", "(echo a)# <(gh pr merge 12)"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(main(cmd))
                self.assertIsNone(check(cmd))
                self.assertIsNone(member(cmd))

    def test_a_heredoc_after_a_glued_hash_is_a_heredoc_and_one_in_a_comment_is_not(self):
        # `x$(true)#` is one word, so `<<E` opens a heredoc and its body is data; after a blank `#` comments it out
        self.assertIsNone(main("echo x$(true)# <<E\ngh pr merge 12\nE"))
        for cmd in ("echo hi # <<E\ngh pr merge 12\nE", "(echo a)# <<E\ngh pr merge 12\nE"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(main(cmd))
                self.assertIsNotNone(member(cmd))


class HeredocsOutsideExpansions(Case):
    """A `<<WORD` inside `${…}` or `$[…]` is part of the expansion, as in `$((…))`: it opens no heredoc, so the
    lines after it are commands."""

    def test_a_heredoc_word_inside_an_expansion_hides_no_line(self):
        for cmd in ("echo ${x/<<E/}\ngh pr merge 12\nE", "echo $[1<<E]\ngh pr merge 12\nE",
                    "x=1; echo ${x:+<<E}\ngh pr merge 12\nE", "echo ${x:-$(echo <<E)}\ngh pr merge 12\nE"):
            with self.subTest(cmd=cmd):
                self.assertRefusedEverywhere(cmd)

    def test_a_heredoc_after_an_expansion_is_still_data(self):
        for cmd in ("echo ${#x} <<E\ngh pr merge 12\nE", "cat <<E ${x}\ngh pr merge 12\nE",
                    "cat <<E\n${x/a/b} gh pr merge 12\nE"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(main(cmd))
                self.assertIsNone(check(cmd))
                self.assertIsNone(member(cmd))


class ZshFileSubstitution(Case):
    """zsh's `=(…)` at the start of a word runs its command as `<(…)` does and stands for a temporary file that holds
    what it wrote, so `. =(…)` sources that output."""

    def test_its_command_and_the_program_it_feeds_source_are_judged(self):
        for cmd in ("cat =(gh pr merge 12)", "diff =(true) =(gh pr merge 12)", "echo x =(true)# =(gh pr merge 12)"):
            with self.subTest(cmd=cmd):
                self.assertRefusedEverywhere(cmd)
        for cmd in (". =(echo gh pr merge 12)", "source =(echo gh pr merge 12)"):
            with self.subTest(sourced=cmd):
                self.assertIn("merges PR 12", main(cmd) or "")
                self.assertIn("merges PR 12", member(cmd) or "")
                self.assertIsNotNone(member(cmd, level="review", who="seraph"))
        self.assertIsNotNone(check(". =(echo dev install)"))

    def test_quoted_text_an_array_and_reads_stay_open(self):
        for cmd in ('echo "=(gh pr merge 12)"', "echo '=(gh pr merge 12)'", "arr=(a b); echo $arr",
                    "diff =(git show HEAD:README.md) =(git show HEAD~1:README.md)"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(main(cmd))
                self.assertIsNone(check(cmd))
                self.assertIsNone(member(cmd))
        self.assertIsNone(member("diff =(git show HEAD:README.md) =(git log -1)", level="review", who="seraph"))


class RedirectIntoASubstitution(Case):
    """`cmd > >(…)` writes into a process substitution: its command is judged, and `>(` is no file the lane writes."""

    def test_the_command_is_judged_and_no_file_is_written(self):
        for cmd in ("echo x > >(gh pr merge 12)", "bash -n -c 'x' > >(gh pr merge 12)"):
            with self.subTest(cmd=cmd):
                self.assertIn("merges PR 12", member(cmd, cwd=self.dev) or "")
        self.assertIsNone(member("git log -1 > >(wc -l)", cwd=self.dev))
        # in the home folder a write of a file asks first; writing into a substitution writes no file there
        self.assertIsNone(guard.check("Bash", {"command": "git log -1 > >(wc -l)"}, str(core.HOME), "claude"))
        self.assertIsNotNone(guard.check("Bash", {"command": "git log -1 > out.txt"}, str(core.HOME), "claude"))

    def setUp(self):
        self.dev = str(core.DEV / "fireflysoftwaresolutions" / "devkit")  # a project of the run's dev root


class SourcedFilesAreResolved(Case):
    """The file `source` or `.` reads is resolved as the system opens it (after `--`, in the folder the command runs
    in, with `//`, `.`, `..` and symbolic links read) before the guard asks whether it is the command's input; where
    it cannot tell which file it is and the command feeds it a here-string, a heredoc, a redirect or a pipe, the
    command is unknown."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rt3-")
        self.addCleanup(shutil.rmtree, self.tmp)
        os.symlink("/dev/stdin", os.path.join(self.tmp, "stdin-link"))
        os.symlink("/dev/fd/0", os.path.join(self.tmp, "fd0-link"))
        os.symlink("/dev", os.path.join(self.tmp, "devdir"))
        Path(self.tmp, "script.sh").write_text("echo hi\n")
        Path(self.tmp, "fed.txt").write_text("gh pr merge 12\n")

    def test_a_descriptor_written_another_way_is_the_input(self):
        t = self.tmp
        for cmd in ("source -- /dev/stdin <<< 'gh pr merge 12'", ". -- /dev/stdin <<< 'gh pr merge 12'",
                    "source /dev/fd//0 <<< 'gh pr merge 12'", "source //dev/stdin <<< 'gh pr merge 12'",
                    "source /dev/./stdin <<< 'gh pr merge 12'", "source /dev/../dev/stdin <<< 'gh pr merge 12'",
                    "cd /dev && source stdin <<< 'gh pr merge 12'", "cd /dev/fd && . ./0 <<< 'gh pr merge 12'",
                    f"source {t}/stdin-link <<< 'gh pr merge 12'", f". {t}/fd0-link <<< 'gh pr merge 12'",
                    f"source {t}/devdir/stdin <<< 'gh pr merge 12'", f"source /dev/stdin < {t}/fed.txt",
                    "echo 'gh pr merge 12' | . //dev/stdin", "bash //dev/stdin <<< 'gh pr merge 12'",
                    "python3 //dev/stdin <<< 'import os; os.system(\"gh pr merge 12\")'"):
            with self.subTest(cmd=cmd):
                self.assertIn("merges PR 12", main(cmd) or "")
                # a member's Python program is read from its input too: the allow-list judge names what it uses
                why = member(cmd) or ""
                self.assertTrue("merges PR 12" in why or "`os.system`" in why, why)

    def test_a_file_the_guard_cannot_place_that_is_fed_is_unknown(self):
        t = self.tmp
        for cmd in ("f=/dev/stdin; source $f <<< 'gh pr merge 12'", "source $f <<< 'gh pr merge 12'",
                    "echo 'gh pr merge 12' | source \"$F\"", f"source \"$F\" < {t}/fed.txt",
                    "source -p /dev stdin <<< 'gh pr merge 12'", "source /dev/fd//3 3<<< 'gh pr merge 12'"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(main(cmd))
                self.assertIsNotNone(member(cmd))

    def test_a_file_it_can_read_and_an_unfed_variable_stay_open(self):
        for cmd in ("source ./script.sh", "source -- ./script.sh", ". ./script.sh", "source $VENV/bin/activate",
                    "source /dev/stdin <<< 'echo hi'", "read -r line < script.sh", "cat script.sh | sh"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(main(cmd, cwd=self.tmp))
                self.assertIsNone(member(cmd, cwd=self.tmp))


class PositionalsReadAsWords(Case):
    """A -c line gets the words after it in place of $1… only when, read as the shell reads its words once quotes
    are removed, nothing in it can bind them again; otherwise they stay variables, unknown where they name a
    program."""

    def test_a_rebind_split_by_quotes_or_hidden_in_a_loop_or_function_keeps_them_unknown(self):
        for cmd in ("bash -c 'eval s\"\"hift; $1 pr merge 12' sh echo gh", "bash -c 'sh\"\"ift; $1 pr merge 12' sh echo gh",
                    "bash -c 's\"\"et -- gh pr merge 12; \"$@\"' sh echo hi",
                    "bash -c 'builtin s\"\"et -- gh pr merge 12; \"$@\"' sh echo hi",
                    "zsh -c '(){ $1 pr merge 12 } gh' zsh echo", "bash -c 'for a do $a pr merge 12; break; done' sh gh",
                    "bash -c 'select a do $a pr merge 12; break; done' sh gh",
                    "bash -c \"eval 'f(' ') { $1 pr merge 12; }'; f gh\" sh echo",
                    "bash -c 'getopts x o; $1 pr merge 12' sh echo gh", "bash -c '. /dev/null; $1 pr merge 12' sh echo gh",
                    "bash -s echo gh <<< 's\"\"hift; $1 pr merge 12'",
                    "bash -c 'function f { $1 pr merge 12; }; f gh' sh echo",
                    "bash -c 'while :; do $1 pr merge 12; shift; done' sh echo gh"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(main(cmd))
                self.assertIsNotNone(member(cmd))

    def test_the_first_command_of_a_function_or_a_loop_without_a_list_is_read(self):
        # `function f {` and `for a do` need no `;` before the command they hold
        for cmd in ("function f { gh pr merge 12; }; f", "function f { gh pr merge 12; }",
                    "for a do gh pr merge 12; done"):
            with self.subTest(cmd=cmd):
                self.assertRefusedEverywhere(cmd)

    def test_a_line_that_binds_nothing_still_gets_the_words(self):
        self.assertIn("plugin", check("sh -c 'claude \"$@\"' _ plugin install x") or "")
        self.assertIn("merges PR 12", main("bash -c 'set -euo pipefail; \"$@\"' sh gh pr merge 12") or "")
        self.assertIn("merges PR 12", main("bash -c 'set -o pipefail; \"$@\" pr merge 12' sh gh") or "")
        for cmd in ("bash -c 'echo \"$1\"' sh hi", "bash -c 'set -e; \"$@\"' sh echo hi",
                    "bash -c 'set -o pipefail; \"$@\"' sh echo hi", "bash -c 'for f in a b; do echo $f; done'",
                    "bash -c 'for f in a b; do echo $f; done; \"$@\"' sh echo hi", "bash -c 'cd \"$1\" && ls .' sh /tmp",
                    "bash -n -c 'gh pr merge 12'"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(main(cmd))
                self.assertIsNone(member(cmd))


class ProgramsKnownByWhatTheyRun(Case):
    """A program is known by the file it runs as well as by its name: a link, a hard link or a renamed copy of a
    shell is read as that shell, and a binary the guard cannot identify that is handed `-c` or an input is unknown."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rt3-")
        self.addCleanup(shutil.rmtree, self.tmp)
        sh = os.path.realpath("/bin/sh")
        os.symlink(sh, os.path.join(self.tmp, "shell-link"))
        os.symlink(sh, os.path.join(self.tmp, "git"))  # a name the guard knows, on a file that is a shell
        shutil.copy(sh, os.path.join(self.tmp, "renamed"))
        self.other = os.path.join(self.tmp, "tool")
        shutil.copy(os.path.realpath(shutil.which("ls")), self.other)

    def test_a_shell_under_another_name_runs_its_line(self):
        t = self.tmp
        for cmd in (f"{t}/shell-link -c 'gh pr merge 12'", f"{t}/git -c 'gh pr merge 12'",
                    f"{t}/renamed -c 'gh pr merge 12'", f"cd {t} && ./renamed -c 'gh pr merge 12'"):
            with self.subTest(cmd=cmd):
                self.assertIn("merges PR 12", main(cmd) or "")
                self.assertIn("merges PR 12", member(cmd) or "")
                self.assertIsNotNone(check(cmd))
        for cmd in (f"echo 'gh pr merge 12' | {t}/renamed", f"{t}/shell-link <<< 'gh pr merge 12'"):
            with self.subTest(fed=cmd):
                self.assertIn("merges PR 12", main(cmd) or "")
                self.assertIn("merges PR 12", member(cmd) or "")

    def test_a_binary_it_cannot_identify_given_a_command_line_or_an_input_is_unknown(self):
        for cmd in (f"{self.other} -c 'gh pr merge 12'", f"echo 'gh pr merge 12' | {self.other}",
                    f"{self.other} <<< 'gh pr merge 12'"):
            with self.subTest(cmd=cmd):
                self.assertIn("cannot identify", main(cmd) or "")
                self.assertIsNotNone(member(cmd))

    def test_ordinary_programs_and_reads_by_path_stay_open(self):
        t = self.tmp
        grep = os.path.realpath(shutil.which("grep"))  # the installed grep, by its path: `-c` counts, input is data
        for cmd in (f"{self.other} -la", f"{t}/shell-link -c 'echo hi'", f"{t}/shell-link -n -c 'gh pr merge 12'",
                    f"ls | {grep} -c x", f"{grep} -ciE 'a|b' x", f"cat > {t}/x.txt <<'EOF'\nhi\nEOF\n{self.other} -la",
                    "/bin/ls -la", "/bin/sh -c 'echo hi'", "/usr/bin/env python3 -c 'print(1)'", "./bin/dev status"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(main(cmd))
                self.assertIsNone(member(cmd))


if __name__ == "__main__":
    unittest.main()
