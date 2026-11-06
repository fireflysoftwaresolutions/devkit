# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Crew guard rules: what a crew member's (a subagent's) shell command runs, and the shell forms that reach devkit's
# protected state (spec C11, K1) through the folder a program runs in. The guard only reads command text and paths:
# nothing here runs a devkit command or writes devkit's state. Run: python3 -m unittest tests.test_guard_crew -v
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from devkit import core, guard  # noqa: E402

CWD = str(ROOT)
STATE = core.HOME / ".local" / "state" / "devkit"
CONFIG_DIR = core.HOME / ".config" / "devkit"


def verdict(cmd, cwd=CWD):
    v = guard.check("Bash", {"command": cmd}, cwd, "claude")
    return v[0] if v else "allow"


class ProtectedCwdTests(unittest.TestCase):
    """Seraph's K1 review (M68, item 1): a program started inside devkit's state or configuration writes there with
    a relative path, as a redirect, sed -i or an editor in that folder would."""

    def test_a_program_that_writes_a_relative_path_inside_devkit_state_or_config_is_refused(self):
        for cmd in (f"cd {STATE}/runs && python3 -c \"import json; json.dump({{}}, open('x.json', 'w'))\"",
                    f"cd {CONFIG_DIR} && node -e \"require('fs').writeFileSync('config.json', '{{}}')\"",
                    f"pushd {STATE}/sessions; perl -e 'open(my $f, \">\", \"s.json\")'",
                    f"cd {STATE} && python3 - <<'EOF'\nopen('runs/r.json', 'w').write('{{}}')\nEOF"):
            with self.subTest(cmd=cmd):
                self.assertEqual(verdict(cmd), "deny")

    def test_a_program_that_writes_in_a_folder_the_guard_cannot_see_is_refused_when_the_command_names_state(self):
        cmd = f"D={STATE}/runs; cd $D && python3 -c \"open('x.json', 'w').write('x')\""
        self.assertEqual(verdict(cmd), "deny")

    def test_the_same_programs_elsewhere_or_reading_state_are_allowed(self):
        for cmd in ("cd /tmp && python3 -c \"import json; json.dump({}, open('x.json', 'w'))\"",
                    "cd $TMPDIR && python3 -c \"open('x.json', 'w')\"",
                    f"cd {STATE}/runs && python3 -c \"import json; print(json.load(open('x.json')))\""):
            with self.subTest(cmd=cmd):
                self.assertEqual(verdict(cmd), "allow")


if __name__ == "__main__":
    unittest.main()
