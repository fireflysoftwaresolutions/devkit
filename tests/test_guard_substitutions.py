# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# What a shell runs that the command around it does not show: the commands inside a process substitution (`<(…)`,
# `>(…)`), which run as `$(…)` does. Each refused form ran its twin in /bin/bash and /bin/zsh
# (`cat <(/bin/date +X)` prints the date); each twin keeps passing.
# Run: python3 -m unittest tests.test_guard_substitutions -v
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


if __name__ == "__main__":
    unittest.main()
