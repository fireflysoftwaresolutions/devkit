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


class WaitAlone(unittest.TestCase):

    def test_work_joined_after_a_wait_is_refused(self):
        for cmd in ("dev ci watch --wait --branch main --sha 4aa253e 2>&1 | tail -3; echo next",
                    "dev watch --wait && git status", "dev watch --wait; echo done", "dev watch --wait || true",
                    "dev ci watch --wait --pr 12\nls", "dev watch --wait & ls",
                    "dev ci watch --wait --pr 12 | python3 x.py"):
            for agent in ("claude", "codex"):
                with self.subTest(cmd=cmd, agent=agent):
                    self.assertIn("act after it wakes you", check(cmd, agent) or "")

    def test_a_wait_alone_or_with_read_only_filters_runs(self):
        for cmd in ("dev ci watch --wait --branch main --sha 4aa253e 2>&1 | tail -3", "dev watch --wait",
                    "dev watch --wait | grep -v x | tail -3", "dev watch --wait &", "cd lib && dev watch --wait",
                    "dev ci watch --pr 12; echo done"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(check(cmd))


class ActionsGatedOnTheResult(unittest.TestCase):

    def test_an_action_that_ignores_the_check_before_it_is_refused(self):
        for cmd in ("npm test; npm run release", "npm test | tail -3 && npm run release",
                    "dev ci watch --pr 12 | tail -3; npm run release -- --no-check > log 2>&1",
                    "python3 -m unittest tests.test_x; git commit -m x", "pytest; git add . && git commit -m x",
                    "make test || make release", "npm run build; npm publish", "pnpm lint; pnpm run release",
                    "cargo test; cargo publish", "just check; just release", "dev test; dev release"):
            for agent in ("claude", "codex"):
                with self.subTest(cmd=cmd, agent=agent):
                    self.assertIn("gate it on the real result", (check(cmd, agent) or "").lower())

    def test_an_action_that_depends_on_the_result_or_follows_no_check_runs(self):
        for cmd in ("npm test && npm run release", "set -o pipefail; npm test | tail -3 && npm run release",
                    "npm run release", "make test && make release", "git add . && git commit -m x",
                    "python3 -m unittest tests.test_x && git add -A && git commit -m x", "npm test; npm run lint",
                    "npm run release:notes > notes.md; echo done"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(check(cmd))


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
