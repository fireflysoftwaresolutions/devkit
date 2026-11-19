# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The bash completion devkit generates (lib/devkit/completion.py, bash): what a Tab offers for a word typed with
escapes, and the live values kept apart per folder. Each test sources the generated script in `bash --norc` with a
home, a folder and a `dev` of its own. Run: python3 -m unittest discover -s tests -t ."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import completion  # noqa: E402

BASH = shutil.which("bash")


@unittest.skipUnless(BASH, "bash is not installed")
class BashWordTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name).resolve()
        self.script = self.d / "dev.bash"
        self.script.write_text(completion.bash())
        self.bin = self.d / "bin"
        self.bin.mkdir()
        # the live values: the name of the folder `dev __complete` runs in
        (self.bin / "dev").write_text('#!/bin/sh\nprintf "%s:here\\n" "${PWD##*/}"\n')
        (self.bin / "dev").chmod(0o755)

    def bash(self, body, cwd):
        r = subprocess.run([BASH, "--norc", "--noprofile", "-c", f"source {self.script}\n" + body],
                           capture_output=True, text=True, timeout=60, cwd=str(cwd),
                           env={"HOME": str(self.d), "PATH": f"{self.bin}:/usr/bin:/bin", "LANG": "en_US.UTF-8"})
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.splitlines()

    def test_a_file_name_typed_with_an_escaped_space_completes(self):
        (self.d / "my file.txt").write_text("")
        got = self.bash("COMP_WORDS=(dev rm 'my\\ fi'); COMP_CWORD=2; _dev; printf '%s\\n' \"${COMPREPLY[@]}\"",
                        self.d)
        self.assertEqual(got, ["my\\ file.txt"])

    def test_folders_whose_names_differ_in_a_hyphen_keep_their_own_values(self):
        for name in ("a-b", "a_b"):
            (self.d / name).mkdir()
        got = self.bash("_dev_wait=10 _dev_first_wait=10\n"
                        "_dev_values branches; echo \"${_dev_reply[@]}\"\n"
                        "cd ../a_b && _dev_values branches; echo \"${_dev_reply[@]}\"", self.d / "a-b")
        self.assertEqual(got, ["a-b", "a_b"])


if __name__ == "__main__":
    unittest.main()
