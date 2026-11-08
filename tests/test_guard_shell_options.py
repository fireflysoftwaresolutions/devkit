# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The guard reads a shell's words as the shell does (guard._shell_args): bash, zsh, sh, dash and ksh read options up
# to the first word that is not one, -c among them, so the -c line is the first word after all of them; and a shell
# given -n, -o noexec or zsh's --noexec only parses its program, which runs nothing, so it is a read. Each case was run
# in the real shells with a harmless twin (`echo RAN`): the line after `-c -e` runs, `-n` runs neither the script nor
# BASH_ENV nor zsh's startup files, and `+n`, `+o noexec` turn running back on.
# Run: python3 -m unittest tests.test_guard_shell_options -v
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


def member(cmd, level="all", who="kid"):
    with mock.patch.object(core, "load_config", return_value=ENFORCE):
        v = guard.crew_shell_verdict(cmd, CWD, None, who, level=level, lane="a1", session="s1", grant=lambda *a: None)
    return v[1] if v else None


def check(cmd):
    v = guard.check("Bash", {"command": cmd}, CWD, "claude")
    return v[1] if v and v[0] == "deny" else None


class TheLineAfterTheOptions(unittest.TestCase):

    def test_options_after_c_do_not_hide_the_line(self):
        for cmd in ("bash -c -e 'gh pr merge 12'", "bash -c +x 'gh pr merge 12'", "sh -c -x 'gh pr merge 12'",
                    "zsh -c -o errexit 'gh pr merge 12'", "bash -c -- 'gh pr merge 12'", "dash -c -e 'gh pr merge 12'",
                    "ksh -c -e 'gh pr merge 12'", "bash -ec 'gh pr merge 12'",
                    "bash -c -eu -o pipefail 'gh pr merge 12'"):
            with self.subTest(cmd=cmd):
                self.assertIn("merges PR 12", main(cmd) or "")
                self.assertIn("merges PR 12", member(cmd) or "")
                self.assertIsNotNone(check(cmd))
        # the forged-environment rule reads the same line
        self.assertIn("sets HOME", check("bash -c +e 'HOME=/tmp/x dev status'") or "")
        # twins: the line is the harmless one, the word after it is $0
        for cmd in ("bash -c -e 'git status'", "bash -c 'echo ok' 'gh pr merge 12'"):
            with self.subTest(twin=cmd):
                self.assertIsNone(main(cmd))
                self.assertIsNone(member(cmd))

    def test_csh_and_tcsh_take_the_word_right_after_c(self):
        # csh -c -e 'line' runs `-e` and fails: the guard reads `-e` as the line, as before
        self.assertEqual(guard._shell_line(["-c", "-e", "gh pr merge 12"], "csh"), "-e")
        self.assertEqual(guard._shell_line(["-c", "-e", "gh pr merge 12"], "bash"), "gh pr merge 12")


class AShellThatOnlyParses(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.release = Path(tmp.name) / "release.sh"
        self.release.write_text("gh pr merge 12\n")

    def test_n_and_noexec_are_reads_and_their_running_twins_are_not(self):
        s = self.release
        for cmd in (f"bash -n {s}", f"sh -n {s}", f"zsh -n {s}", f"zsh --noexec {s}", f"bash -o noexec {s}",
                    f"dash -n {s}", "bash -n -c 'gh pr merge 12'", "bash -nc 'gh pr merge 12'",
                    f"bash -n < {s}", f"cat {s} | bash -n", f"bash -n -x {s}"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(main(cmd))
                self.assertIsNone(member(cmd))
                self.assertIsNone(member(cmd, level="review", who="seraph"))
        # twins that run after all: +n and +o noexec turn running back on; an interactive or login shell ignores -n;
        # -n after the script is one of its words; csh is not read as parsing only
        for cmd in (f"bash -n +n {s}", "bash -n -c +n 'gh pr merge 12'", f"bash -n +o noexec {s}",
                    f"bash -ni {s}", f"bash -n -l {s}", f"bash {s} -n", f"csh -n {s}", f"bash {s}"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(main(cmd))
                self.assertIsNotNone(member(cmd))
                self.assertIsNotNone(member(cmd, level="review", who="seraph"))


if __name__ == "__main__":
    unittest.main()
