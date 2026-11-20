# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# How devkit reads the words of a shell command (lib/devkit/shellparse.py), as a POSIX shell splits them: the program a
# command runs past its prefixes and zsh's precommand modifiers, the commands a substitution or a nested shell runs,
# heredoc bodies as data, the devkit calls a command makes and their kind, and the files an apply_patch envelope
# names. The call ledger, lane routing and the crew gates read commands through it. No command runs.
# Run: python3 -m unittest tests.test_shellparse -v
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()  # run alone, this module gets the suite's temporary home too (tests/_hermetic.py)
sys.path.insert(0, str(ROOT / "lib"))

from devkit import core, shellparse as sp  # noqa: E402

CWD = str(core.DEV / "fireflysoftwaresolutions" / "devkit")


class ProgramTests(unittest.TestCase):

    def test_the_program_is_read_past_wrappers_and_zshs_precommand_modifiers(self):
        cases = {("noglob", "nocorrect", "builtin", "rm", "-rf", "x"): ["rm", "-rf", "x"],
                 ("-", "rm", "x"): ["rm", "x"],
                 ("env", "-u", "X", "A=1", "nice", "-n", "5", "sudo", "-u", "me", "rm", "x"): ["rm", "x"],
                 ("timeout", "5", "git", "push"): ["git", "push"],
                 ("git", "status"): ["git", "status"]}
        for argv, program in cases.items():
            with self.subTest(argv=argv):
                self.assertEqual(sp.unwrap(list(argv)), program)

    def test_a_prefix_assignment_is_stripped_and_the_command_kept(self):
        self.assertEqual(sp.strip_prefix(["env", "A=1", "git", "push"]), ["git", "push"])


class SplitTests(unittest.TestCase):

    def test_a_substitution_is_one_word_and_its_command_one_of_its_own(self):
        self.assertEqual(sp.split_commands("rm $(true) -rf ~/x; echo a | grep b > /dev/null"),
                         [(["rm", "$", "-rf", "~/x"], []), (["true"], []), ([], []), (["echo", "a"], []),
                          (["grep", "b"], ["/dev/null"])])

    def test_operators_come_with_each_command(self):
        self.assertEqual(sp.split_commands("cd lib && dev progress step x || true", ops=True),
                         [(["cd", "lib"], [], "&&", []), (["dev", "progress", "step", "x"], [], "||", []),
                          (["true"], [], "", [])])

    # `>|` truncates its file like `>`, and also where noclobber is set; it was read as two words of the command, so
    # `pytest >| ~/.bashrc` wrote the developer's shell settings while no caller saw a write
    def test_a_clobbering_redirect_is_a_redirect_with_or_without_a_descriptor(self):
        self.assertEqual(sp.split_commands("pytest >| ~/.bashrc"), [(["pytest"], ["~/.bashrc"])])
        self.assertEqual(sp.split_commands("pytest 2>| err.log | tee out.log"),
                         [(["pytest"], ["err.log"]), (["tee", "out.log"], [])])

    def test_a_line_that_does_not_tokenize_is_none(self):
        self.assertIsNone(sp.split_commands("echo 'open"))

    def test_a_heredoc_body_is_data(self):
        self.assertEqual(sp.strip_heredocs("cat <<'EOF' > f\nrm -rf x\nEOF\necho done"), "cat <<'EOF' > f\necho done")

    def test_every_command_a_line_runs_is_found_nested_shells_and_substitutions_included(self):
        calls = sp.shell_calls("echo a; sh -c 'rm -rf x' && echo $(git status)", CWD)
        self.assertIn((["rm", "-rf", "x"], None), calls)
        self.assertIn((["git", "status"], None), calls)
        self.assertIn((["sh", "-c", "rm -rf x"], None), calls)

    def test_an_interpreter_carries_the_code_it_runs(self):
        code = 'import os; os.system("dev progress step x")'
        self.assertEqual(sp.shell_calls(f"python3 -c '{code}'", CWD), [(["python3", "-c", code], code)])

    def test_the_substitutions_the_shell_runs_are_listed(self):
        self.assertEqual(sp.expansions("echo $HOME ${X:-y} $(date) `id`"), ["date", "id"])
        self.assertEqual(sp.expansions("echo '$(date)'"), [])


class DevCallTests(unittest.TestCase):

    def test_a_dev_call_is_read_through_its_path_and_its_interpreter(self):
        self.assertEqual(sp.dev_args(["bin/dev", "crew", "ask", "x"]), ["crew", "ask", "x"])
        self.assertEqual(sp.dev_args(["python3", "bin/dev", "status"]), ["status"])
        self.assertIsNone(sp.dev_args(["git", "status"]))

    def test_an_options_value_is_no_word_of_the_command(self):
        self.assertEqual(sp.dev_words(["--project", "a/b", "lesson", "--search", "add", "ls"]), ["lesson", "ls"])

    def test_the_kind_of_a_crew_call(self):
        cases = {("progress", "step", "x"): "lane", ("crew", "ask", "x"): "lane", ("crew", "brief", "x"): "team",
                 ("crew", "sync"): "sync", ("crew", "show"): "read", ("status",): ""}
        for args, kind in cases.items():
            with self.subTest(args=args):
                self.assertEqual(sp.crew_kind(list(args)), kind)

    def test_one_command_is_one_program_and_a_quoted_heredoc_message_is_data(self):
        self.assertTrue(sp.runs_one_command("dev progress step x"))
        self.assertTrue(sp.runs_one_command("dev crew ask \"$(cat <<'EOF'\nuse x; then y\nEOF\n)\""))
        self.assertFalse(sp.runs_one_command("dev progress step x && ls"))
        self.assertFalse(sp.runs_one_command("dev crew ask \"$(date)\""))


class QuotedMessageTests(unittest.TestCase):
    # The lane router sends a member's single `dev crew ask|answer` or `dev progress …` call to its lane; one that
    # passes its message as a quoted heredoc runs `cat` and nothing else, so it is one command. What the shell really
    # runs around it, or expands inside it, still makes it more than one.
    MSG = "$(cat <<'EOF'\nShould I run gh pr merge 12; or wait && ask | Link?\nEOF\n)"

    def test_a_quoted_message_is_one_command(self):
        msg = self.MSG
        for cmd in ('dev crew ask "use x; then y && z | w > f"',
                    "dev crew answer 3 'is $(whoami) data? and `id` too'",
                    f'dev crew ask "{msg}"', f'dev crew answer 4 "{msg}"', f"dev crew ask {msg}",
                    f'dev progress step "fixed" --check "{msg}"',
                    'dev crew ask "before ' + msg + ' after"',
                    "dev crew ask \"$(cat <<\"EOF\"\nbody with ) and $(id) as text\nEOF\n)\"",
                    'dev crew ask "a \\$(id) escaped"'):
            with self.subTest(cmd=cmd):
                self.assertTrue(sp.runs_one_command(cmd))

    def test_what_the_shell_runs_is_more_than_one_command(self):
        msg = self.MSG
        for cmd in ('dev crew ask "$(id)"', 'dev crew ask "`id`"', 'dev crew ask "x"; gh pr merge 12',
                    f'dev crew ask "{msg}" && gh pr merge 12', 'dev crew ask "x" > /tmp/f', 'dev crew ask "open',
                    # an unquoted delimiter expands the body: $(id) in it runs
                    "dev crew ask \"$(cat <<EOF\nexpands $(id)\nEOF\n)\"",
                    # after the delimiter line the substitution runs more, before its `)`
                    "dev crew ask \"$(cat <<'EOF'\nx\nEOF\ngh pr merge 12\n)\"",
                    # a delimiter line inside the body ends it there, as in the shell
                    "dev crew ask \"$(cat <<'EOF'\nx\nEOF\ngh pr merge 12\ncat <<'EOF'\ny\nEOF\n)\"",
                    # `<<-` strips tabs from the delimiter line; not taken as data
                    "dev crew ask \"$(cat <<-'EOF'\nx\n\tEOF\n)\"",
                    # a `$(` inside single quotes is text: the operators around it run
                    "dev crew ask '$(cat <<'EOF'\n' ; gh pr merge 12 ; echo '\nEOF\n)'",
                    "dev crew ask \"$(cat /etc/hosts)\"", "dev crew ask \"$((1 + 2))\""):
            with self.subTest(cmd=cmd):
                self.assertFalse(sp.runs_one_command(cmd))

    def test_the_shell_runs_the_dev_call_and_cat_for_a_heredoc_message(self):
        calls = sp.shell_calls(f'dev crew ask "{self.MSG}"', CWD)
        self.assertEqual([a[:3] for a, _ in calls], [["dev", "crew", "ask"], ["cat"]])

    def test_a_semicolon_the_shell_passes_on_ends_a_segment(self):
        # tmux's `\;` and find's `\;` end a segment, and a quoted `;` is an operator too
        mark = ""  # a private-use character an earlier tokenizer used as a marker: an ordinary word now
        self.assertEqual(sp.split_commands("tmux new -d \\; send-keys x"),
                         [(["tmux", "new", "-d"], []), (["send-keys", "x"], [])])
        self.assertEqual(sp.split_commands(f"echo '{mark}'; tmux new -d ';' send-keys x"),
                         [(["echo", mark], []), (["tmux", "new", "-d"], []), (["send-keys", "x"], [])])


class ToolInputTests(unittest.TestCase):

    def test_an_apply_patch_envelope_names_each_file_and_what_happens_to_it(self):
        patch = ("*** Begin Patch\n*** Update File: a.py\n@@\n-x\n+y\n*** Add File: b.py\n+z\n*** Delete File: c.py\n"
                 "*** Update File: e.py\n*** Move to: d.py\n*** End Patch\n")
        self.assertEqual(sp.parse_patch_paths(patch), [("update", "a.py"), ("add", "b.py"), ("delete", "c.py"),
                                                       ("update", "e.py"), ("add", "d.py")])

    def test_a_tool_inputs_commands_are_read_once_and_a_list_is_quoted(self):
        self.assertEqual(sp.tool_commands({"command": "ls -la", "cmd": "ls -la"}), ["ls -la"])
        self.assertEqual(sp.tool_commands({"cmd": ["bash", "-lc", "echo a b"]}), ["bash -lc 'echo a b'"])
        self.assertEqual(sp.tool_commands({"file_path": "x"}), [])


class WordTests(unittest.TestCase):

    def test_a_path_is_resolved_unless_the_shell_decides_it(self):
        self.assertEqual(sp.resolve("~/x", CWD), Path(core.real(str(core.HOME / "x"))))
        self.assertEqual(sp.resolve("a/../b", "/tmp"), Path(core.real("/tmp/b")))
        self.assertEqual(sp.resolve("$FOO/x", CWD), sp.UNKNOWN)

    def test_an_options_value_and_the_positionals(self):
        self.assertEqual(sp.opt_value(["commit", "-m", "msg", "--amend"], ("-m", "--message")), "msg")
        self.assertEqual(sp.opt_value(["commit", "--message=hi"], ("-m", "--message")), "hi")
        self.assertIsNone(sp.opt_value(["commit"], ("-m",)))
        self.assertEqual(sp.positionals(["-C", "dir", "push", "-u", "origin", "main"], ("-C",)),
                         ["push", "origin", "main"])

    def test_the_command_words_name_known_programs_only(self):
        # the bug registry shows a command word only when it is one of these: a program's name, never a path
        self.assertEqual({w for w in ("git", "dev", "tmux", "acme-internal-tool") if w in sp.COMMAND_WORDS},
                         {"git", "dev", "tmux"})
        self.assertIsInstance(sp.COMMAND_WORDS, frozenset)
        for w in ("gh", "sed", "rm", "find", "xargs", "python3", "bash", "tee", "pytest", "claude"):
            self.assertIn(w, sp.COMMAND_WORDS)
        self.assertEqual([w for w in sp.COMMAND_WORDS if not re.fullmatch(r"[A-Za-z][A-Za-z0-9._+-]{0,30}", w)], [])
        self.assertNotIn("/usr/bin/sed", sp.COMMAND_WORDS)
        self.assertNotIn("", sp.COMMAND_WORDS)
        self.assertEqual(sp.SHELLS, frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish", "csh", "tcsh"}))

    def test_a_code_span_holds_any_value_whole(self):
        self.assertEqual([sp.code_span(v) for v in ("a`b", "", "`x", "plain")],
                         ["``a`b``", "` `", "`` `x ``", "`plain`"])


if __name__ == "__main__":
    unittest.main()
