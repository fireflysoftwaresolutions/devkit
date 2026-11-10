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
                    "npm run release:notes > notes.md; echo done",
                    'r=$(python3 -m unittest x 2>&1 | tail -1); echo "$r"; if [ "$r" = OK ]; then git commit -m x; fi',
                    'out=$(npm test | tail -1); [ "$out" = OK ] && npm run release'):
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

    def test_a_programs_argument_that_names_an_identity_variable_sets_nothing(self):
        # only the words before the command, env's own and unset's or export's operands change the environment: a
        # search pattern is text. Each allowed search has a refused twin that does blank or unset the variable.
        pairs = (('grep -rn "CODEX_THREAD_ID=" tests/', "CODEX_THREAD_ID= make check"),
                 ("rg -n 'DEVKIT_SESSION=' lib", "env -u CODEX_THREAD_ID make check"),
                 ("grep -n 'env -u CLAUDECODE' docs/guard.md", "timeout 5 env -u CLAUDECODE make"),
                 ("grep -rn 'unset DEVKIT_SESSION' tests", "find . -exec env -u CLAUDECODE true {} \\;"),
                 ("echo CLAUDECODE= | wc -c", "nice -n 5 env CLAUDECODE= make"),
                 ("git log --grep 'DEVKIT_AGENT='", "export -n CLAUDECODE"))
        for allowed, refused in pairs:
            for agent, tool, key in (("claude", "Bash", "command"), ("codex", "exec_command", "cmd")):
                with self.subTest(allowed=allowed, agent=agent):
                    self.assertIsNone(guard.check(tool, {key: allowed}, CWD, agent))
                with self.subTest(refused=refused, agent=agent):
                    v = guard.check(tool, {key: refused}, CWD, agent)
                    self.assertIn("an agent runs it", v[1] if v and v[0] == "deny" else "")
        # aimed at devkit, the rule on devkit's own variables may refuse first: refused either way
        for cmd in ("CODEX_THREAD_ID= dev status", "env -u CODEX_THREAD_ID dev status"):
            for agent, tool, key in (("claude", "Bash", "command"), ("codex", "exec_command", "cmd")):
                with self.subTest(refused=cmd, agent=agent):
                    self.assertEqual((guard.check(tool, {key: cmd}, CWD, agent) or ("allow",))[0], "deny")

    def test_other_variables_and_reading_them_stay_open(self):
        for cmd in ("unset FOO", "env -u FOO npm test", "FOO= npm test", "echo $CLAUDECODE", "unset -f CLAUDECODE",
                    "printenv CLAUDECODE"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(check(cmd))


if __name__ == "__main__":
    unittest.main()
