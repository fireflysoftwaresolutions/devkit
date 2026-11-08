# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The guard reads a shell's words as the shell does (guard._shell_args): bash, zsh, sh, dash and ksh read options up
# to the first word that is not one, -c among them, so the -c line is the first word after all of them. Each case
# was run in the real shells with a harmless twin (`echo RAN`): the line after `-c -e`, `-c +x`, `-c --` and
# `-c -o errexit` runs, while csh and tcsh run the word right after -c.
# Run: python3 -m unittest tests.test_guard_shell_options -v
import sys
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


if __name__ == "__main__":
    unittest.main()
