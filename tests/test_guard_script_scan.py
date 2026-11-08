# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The guard reads an interpreter's program before it runs (guard._code_texts, _code_env_sets). A Python program that
# starts processes only with argument lists has no string a shell reads, so a Markdown code span or a regex with
# backticks in it is text; and a Python variable named HOME sets no environment. Lessons L-fy4000hoz14 and
# L-fzkgksoe9jy. Each allowed program has a twin that hands the same string to a shell, or really sets HOME for a
# devkit command, and stays refused.
# Run: python3 -m unittest tests.test_guard_script_scan -v
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

TICK = chr(0x60)
ENFORCE = {"safeguards": {"irreversible_main": "enforce", "irreversible_member": "enforce"}}
REGEX = f'CODE = re.compile(r"{TICK}([^{TICK}]+){TICK}")\n'
NOTE = f'NOTE = "Run {TICK}dev test{TICK} first, then {TICK}gh pr merge 12{TICK} once CI is green."\n'


class ProgramScan(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def script(self, name, body):
        p = self.dir / name
        p.write_text(body)
        return f"python3 {p}"

    def member(self, cmd):
        with mock.patch.object(core, "load_config", return_value=ENFORCE):
            v = guard.crew_shell_verdict(cmd, str(self.dir), None, "kid", level="all", lane="a1", session="s1",
                                         grant=lambda *a: None)
        return v[1] if v else None

    def main(self, cmd):
        v = guard.check("Bash", {"command": cmd}, str(self.dir), "claude")
        return v[1] if v and v[0] == "deny" else None

    def test_backticks_in_a_program_that_runs_argument_lists_are_text(self):
        body = "import re\nimport subprocess\n" + REGEX + NOTE + \
            'print(CODE.sub("<code>\\\\1</code>", NOTE))\nsubprocess.run(["git", "status", "--short"], check=False)\n'
        self.assertIsNone(self.member(self.script("guide.py", body)))
        heredoc = "python3 - <<'PY'\n" + body + "PY"
        self.assertIsNone(self.member(heredoc))

    def test_a_string_the_program_hands_to_a_shell_is_still_read(self):
        lines = "import re\nimport os\nimport subprocess\n" + REGEX + NOTE
        for run in ('subprocess.run(NOTE, shell=True)\n', 'subprocess.run(NOTE, **{"shell": True})\n',
                    "os.system(NOTE)\n", 'subprocess.run(["bash", "-c", NOTE])\n',
                    'subprocess.run(["/bin/zsh", "-lc", NOTE])\n', "exec(NOTE)\n",
                    'getattr(subprocess, "run")(NOTE)\n'):
            with self.subTest(run=run):
                self.assertIsNotNone(self.member(self.script("run.py", lines + run)))

    def test_a_python_variable_named_home_sets_no_environment(self):
        body = ("import subprocess\nfrom pathlib import Path\n\nHOME = Path.home()\n"
                "GUIDE = HOME / 'Development/.handoff/plans/acme/devkit/guide.md'\nprint(GUIDE.name)\n"
                "subprocess.run(['git', 'status', '--short'], check=False)\n")
        self.assertIsNone(self.main(self.script("guide.py", body)))
        self.assertIsNone(self.member(self.script("guide.py", body)))

    def test_a_program_that_sets_home_for_devkit_is_still_refused(self):
        for body in ('import os, subprocess\nos.environ["HOME"] = "/tmp/x"\nsubprocess.run(["dev", "status"])\n',
                     'import os, subprocess\nsubprocess.run(["dev", "status"], env=dict(os.environ, HOME="/tmp/x"))\n',
                     'import subprocess\nsubprocess.run(["dev", "status"], env={"HOME": "/tmp/x"})\n',
                     'import os, subprocess\nos.putenv("DEVKIT_CONFIG", "/tmp/c")\nsubprocess.run(["dev", "status"])\n'):
            with self.subTest(body=body):
                self.assertIn("sets", self.main(self.script("env.py", body)) or "")


if __name__ == "__main__":
    unittest.main()
