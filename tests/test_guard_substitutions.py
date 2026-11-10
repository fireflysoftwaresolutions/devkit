# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# What a shell runs that the command around it does not show: the commands inside a process substitution (`<(…)`,
# `>(…)`), which run as `$(…)` does. Each refused form ran its twin in /bin/bash and /bin/zsh
# (`cat <(/bin/date +X)` prints the date); each twin keeps passing.
# Run: python3 -m unittest tests.test_guard_substitutions -v
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


def main(cmd):
    with mock.patch.object(core, "load_config", return_value=ENFORCE):
        v = guard.irreversible_verdict(cmd, CWD, agent="claude", session="s1", grant=lambda *a: None)
    return v[1] if v else None


def member(cmd, level="all", who="kid", routed=False):
    with mock.patch.object(core, "load_config", return_value=ENFORCE):
        v = guard.crew_shell_verdict(cmd, CWD, None, who, level=level, lane="a1", session="s1", grant=lambda *a: None,
                                     routed=routed)
    return v[1] if v else None


def check(cmd):
    v = guard.check("Bash", {"command": cmd}, CWD, "claude")
    return v[1] if v and v[0] == "deny" else None


class ProcessSubstitution(unittest.TestCase):

    def test_the_command_inside_is_judged_as_the_shell_runs_it(self):
        for cmd in ("cat <(gh pr merge 12)", "cat < <(gh pr merge 12)", "echo x > >(gh pr merge 12)",
                    "bash -n <(gh pr merge 12)", "bash -n -c 'x' > >(gh pr merge 12)",
                    "diff <(git diff) <(gh pr merge 12)", 'echo "$(cat <(gh pr merge 12))"'):
            with self.subTest(cmd=cmd):
                self.assertIn("merges PR 12", main(cmd) or "")
                self.assertIn("merges PR 12", member(cmd) or "")
                self.assertIsNotNone(member(cmd, level="review", who="seraph"))
        self.assertIn("force-push", check("diff <(git push --force origin main) /dev/null") or "")
        self.assertIn("nested claude session", check("cat <(claude --dangerously-skip-permissions -p hi)") or "")
        self.assertIn("`dev install`", check("wc -l <(dev install)") or "")

    def test_a_lane_or_crew_call_inside_is_refused_in_every_lane(self):
        for cmd in ("wc -l <(dev crew decide D1 A)", "wc -l <(dev progress done x)", "cat <(dev crew inbox)",
                    "diff <(dev progress note a) /dev/null", "wc -l <(dev crew withdraw D1 --why x)"):
            for level, who in (("all", "kid"), ("review", "seraph")):
                with self.subTest(cmd=cmd, level=level):
                    self.assertIsNotNone(member(cmd, level=level, who=who))
                    self.assertIsNotNone(member(cmd, level=level, who=who, routed=True))

    def test_text_that_only_looks_like_one_is_not_run(self):
        # twins: double quotes, single quotes and a heredoc body keep `<(` as text; a read inside is a read
        for cmd in ('echo "<(gh pr merge 12)"', "echo '<(gh pr merge 12)'", "cat <<'EOF'\n<(gh pr merge 12)\nEOF",
                    "cat <<EOF\n<(gh pr merge 12)\nEOF"):
            with self.subTest(text=cmd):
                self.assertIsNone(main(cmd))
                self.assertIsNone(member(cmd))
        for cmd in ("diff <(git show HEAD:README.md) <(git show HEAD~1:README.md)", "cat <(git log -1)",
                    "wc -l <(dev status)"):
            with self.subTest(read=cmd):
                self.assertIsNone(main(cmd))
                self.assertIsNone(check(cmd))
                self.assertIsNone(member(cmd))
                self.assertIsNone(member(cmd, level="review", who="seraph"))


def payloads(cmd):
    """The same command as each agent sends it: Claude Code's Bash, Codex's exec_command and its shell tool."""
    return [("Bash", {"command": cmd}, "claude"), ("exec_command", {"cmd": cmd}, "codex"),
            ("shell", {"command": ["bash", "-lc", cmd]}, "codex")]


class APathBuiltOnASubstitution(unittest.TestCase):
    """A `$(pwd)/…` is a path below the folder the command runs in, after the cds before it: the guard reads it as
    `./…`, the verdict its plain twin gets, never as `/…` or as a word it cannot place. A substitution that may print
    nothing is still dropped, so a path written after it is judged as the absolute path it then is; so is a glued
    `$(pwd)x` and a pwd the command may replace."""

    def setUp(self):
        self.here = tempfile.mkdtemp(dir=str(core.DEV))  # a folder below ~/Development, where a plain write runs
        self.addCleanup(shutil.rmtree, self.here, True)

    def verdicts(self, cmd):
        out = {}
        for tool, inp, agent in payloads(cmd):
            v = guard.check(tool, inp, self.here, agent)
            out[tool] = v[0] if v else "allow"
        return out

    def test_a_path_below_pwd_is_not_read_as_one_at_the_root_and_one_after_an_empty_output_is(self):
        for cmd in ("echo x > $(pwd)/out.txt", 'echo x > "$(pwd)"/x', "cp a $(pwd)/b", "echo x > `pwd`/out.txt",
                    "echo x > $(pwd -P)/out.txt", "printf x >> $(pwd)/lib/out.txt", "touch $(pwd)/x"):
            with self.subTest(allowed=cmd):
                self.assertEqual(set(self.verdicts(cmd).values()), {"allow"}, self.verdicts(cmd))
        # the twins: a substitution that may print nothing, and a pwd the command redefines, are still dropped
        generated = core.HANDOFF / ("registry" + ".json")
        for cmd in (f"printf x > $(true){generated}", f"printf x > `true`{generated}",
                    f"pwd() {{ :; }}; printf x > $(pwd){generated}",
                    "dev$(true) install", "$(true)dev install"):
            with self.subTest(refused=cmd):
                self.assertEqual(set(self.verdicts(cmd).values()), {"deny"}, self.verdicts(cmd))

    def test_a_pwd_path_after_a_cd_is_judged_where_the_cd_went(self):
        # the folder a pwd prints is the one the cds before it chose, quoted or not: bash and zsh printed
        # `/tmp/y` for `cd /tmp && echo "$(pwd)"/y`, and `/Users` for `cd / && echo $(pwd)Users`
        h, name = core.HANDOFF, "registry" + ".json"
        refused = [f"cd {h} && printf x > $(pwd)/{name}", f"cd {h} && printf x > `pwd`/{name}",
                   f'cd {h} && printf x > "$(pwd)"/{name}', f'cd {h} && printf x > "$(pwd)/{name}"',
                   f"cd {h} && printf x > $(pwd -P)/{name}", f"cd {h.parent} && printf x > $(pwd)/{h.name}/{name}",
                   f"cd {h.parent} && printf x > `pwd`/{h.name}/{name}", f"cd / && printf x > $(pwd){str(h)[1:]}/{name}",
                   'rm -rf "$(pwd)"/..', "rm -rf $(pwd)/..", 'cd ~ && rm -rf "$(pwd)"/Development',
                   "cd ~ && rm -rf $(pwd)/Development", """sh -c 'rm -rf "$(pwd)"/..'""",
                   # a command before the pwd may change the folder, so a quoted pwd after it is masked as well
                   'true && rm -rf "$(pwd)"/..', 'git status && rm -rf "$(pwd)/.."',
                   'cd ~ && true && rm -rf "$(pwd)"/Development',
                   # a cd that fails leaves the folder as it was; only `&&` skips what follows it
                   f"cd {self.here}/missing; rm -rf $(pwd)/..", f'cd {self.here}/missing; rm -rf "$(pwd)"/..',
                   f"cd {self.here}/missing\nrm -rf $(pwd)/..", f'cd {self.here}/missing\nrm -rf "$(pwd)"/..']
        for cmd in refused:
            with self.subTest(refused=cmd):
                self.assertEqual(set(self.verdicts(cmd).values()), {"deny"}, self.verdicts(cmd))
        # a write to a folder the guard cannot place: asked in Claude Code, refused where nobody can be asked
        for cmd in (f"cd {h} && true && printf x > $(pwd)/{name}", f'cd {h} && true && printf x > "$(pwd)"/{name}'):
            with self.subTest(placed_nowhere=cmd):
                self.assertNotIn("allow", self.verdicts(cmd).values(), self.verdicts(cmd))
        for cmd in ('echo x > "$(pwd)/x y"', 'rm -rf "$(pwd)"/build', "echo x > $(pwd)x.txt",
                    f"cd {self.here} && rm -rf $(pwd)/build", f'cd {self.here}/missing && rm -rf "$(pwd)"/build'):
            with self.subTest(allowed=cmd):
                self.assertEqual(set(self.verdicts(cmd).values()), {"allow"}, self.verdicts(cmd))
        # each one gets the verdict of the command that names the folder itself
        for cmd, plain in ((f"cd {h} && printf x > $(pwd)/{name}", f"cd {h} && printf x > {name}"),
                           ('rm -rf "$(pwd)"/..', "rm -rf .."),
                           ("cd ~ && echo k >> $(pwd)/.ssh/authorized_keys", "cd ~ && echo k >> .ssh/authorized_keys"),
                           ("cd ~ && echo k >> $(pwd)/notes.txt", "cd ~ && echo k >> notes.txt"),
                           (f'cd {self.here} && echo x > "$(pwd)"/out.txt', f"cd {self.here} && echo x > out.txt")):
            with self.subTest(twin=cmd):
                self.assertEqual(self.verdicts(cmd), self.verdicts(plain))

    def test_a_pwd_the_command_may_replace_is_not_read_as_the_folder(self):
        # zsh runs a function named 'pwd' or one set in functions[pwd]; anything else before the pwd's command
        # (here a source) may define one too, so the reading that drops the substitution stands
        target = core.HANDOFF / ("registry" + ".json")
        for cmd in (f"'pwd'() {{ :; }}; printf x > $(pwd){target}", f"functions[pwd]=:; printf x > $(pwd){target}",
                    f'"pwd"() {{ :; }}; printf x > `pwd`{target}', f"function pwd {{ :; }}; printf x > $(pwd){target}",
                    f". ./env.sh; printf x > $(pwd){target}", "alias pwd='echo /tmp' && rm -rf $(pwd)/..",
                    "alias pwd='echo /tmp'\neval 'rm -rf \"$(pwd)\"/..'"):
            with self.subTest(refused=cmd):
                self.assertEqual(set(self.verdicts(cmd).values()), {"deny"}, self.verdicts(cmd))
        # the words function and alias replace nothing inside an argument or a comment
        for cmd in ("echo x > $(pwd)/out.txt", 'echo "add functions" > $(pwd)/out.txt',
                    "echo x > $(pwd)/out.txt # an alias"):
            with self.subTest(allowed=cmd):
                self.assertEqual(set(self.verdicts(cmd).values()), {"allow"}, self.verdicts(cmd))


class SourceReadsItsInput(unittest.TestCase):
    """`source` and `.` run what they read from their input, a descriptor or a process substitution, as a shell does
    (`. /dev/stdin <<< '/bin/date +X'` printed the date in /bin/bash and /bin/zsh)."""

    def test_what_source_is_fed_is_judged_as_the_program_it_runs(self):
        for cmd in ("source /dev/stdin <<< 'gh pr merge 12'", ". /dev/stdin <<< 'gh pr merge 12'",
                    "source <(echo gh pr merge 12)", ". <(echo gh pr merge 12)",
                    "echo gh pr merge 12 | source /dev/stdin", "printf 'gh pr merge 12' | . /dev/fd/0",
                    "source /dev/fd/3 3<<< 'gh pr merge 12'"):
            with self.subTest(cmd=cmd):
                self.assertIn("merges PR 12", main(cmd) or "")
                self.assertIn("merges PR 12", member(cmd) or "")
                self.assertIsNotNone(member(cmd, level="review", who="seraph"))
        # a descriptor the command does not show may hold anything
        self.assertIsNotNone(main("exec 3< f; . /dev/fd/3"))

    def test_a_lane_call_source_or_a_shell_is_fed_is_refused(self):
        for cmd in ("source /dev/stdin <<< 'dev progress done x'", "echo dev progress done x | source /dev/stdin",
                    "source <(echo dev crew decide D1 A)", "echo dev progress done x | bash",
                    "printf 'dev crew decide D1 A' | sh"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(member(cmd))

    def test_a_harmless_program_fed_to_source_runs(self):
        for cmd in ("source /dev/stdin <<< 'ls'", "echo 'export X=1' | . /dev/stdin", "source <(echo ls)",
                    "source /dev/stdin < /dev/null", "echo hi | bash"):
            with self.subTest(twin=cmd):
                self.assertIsNone(main(cmd))
                self.assertIsNone(member(cmd))


if __name__ == "__main__":
    unittest.main()
