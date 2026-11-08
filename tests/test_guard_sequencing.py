# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# What a command line joins after a wait or a check, and what it hides from the programs it runs: a wait that does
# more work after it wakes nobody until that work ends, an action sequenced after a check without depending on its
# result runs whatever the check said, and an agent that unsets its identity variables hides from a project's tools
# that an agent runs them. Each form comes from a command an agent ran in a real session.
# Run: python3 -m unittest tests.test_guard_sequencing -v
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


def check(cmd, agent="claude"):
    v = guard.check("Bash", {"command": cmd}, CWD, agent)
    return v[1] if v and v[0] == "deny" else None


class AgentIdentityStays(unittest.TestCase):

    def test_unsetting_or_blanking_an_identity_variable_is_refused(self):
        for cmd in ("unset CLAUDECODE FIREFLY_DOCS_CHAT; npm run release", "unset CLAUDECODE",
                    "env -u CLAUDECODE npm run build", "env --unset=CODEX_SANDBOX make", "CLAUDECODE= npm run build",
                    "export DEVKIT_AGENT=", "env -i PATH=/usr/bin npm test", "bash -c 'unset CODEX_THREAD_ID; make'",
                    "builtin unset DEVKIT_SESSION"):
            for agent in ("claude", "codex"):
                with self.subTest(cmd=cmd, agent=agent):
                    self.assertIn("an agent runs it", check(cmd, agent) or "")

    def test_other_variables_and_reading_them_stay_open(self):
        for cmd in ("unset FOO", "env -u FOO npm test", "FOO= npm test", "echo $CLAUDECODE", "unset -f CLAUDECODE",
                    "printenv CLAUDECODE"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(check(cmd))


if __name__ == "__main__":
    unittest.main()
