# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The identity reader of the PreToolUse hook (hooks.identity_change) reads the script a shell runs, so a member
# cannot change who calls devkit inside it. bash 3.2, macOS's /bin/bash, runs `+c` like `-c`.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_hooks_identity
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import hooks  # noqa: E402


class ShellScriptIdentityTests(unittest.TestCase):
    def test_a_script_bash_runs_with_plus_c_is_read_like_one_it_runs_with_dash_c(self):
        with tempfile.TemporaryDirectory() as d:
            for flag in ("-c", "+c", "-ec", "+ec"):
                with self.subTest(flag=flag):  # an assignment in front of a call inside the script it runs
                    self.assertEqual(hooks.identity_change(f"bash {flag} 'DEVKIT_FROM_AGENT=a2 dev crew ask x'", d),
                                     ["DEVKIT_FROM_AGENT"])
            self.assertEqual(hooks.identity_change("bash +x tools/run.sh", d), [])  # a script file, no -c or +c


if __name__ == "__main__":
    unittest.main()
