# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Docs, tests and code carry neutral fixture names (ana, devuser, acme), never the account of the Mac they were written
# on: 26.11.4 shipped the developer's macOS user name in a prompt example, two audit examples and two tests. This test
# fails when a tracked text file names the login or the home folder of the Mac it runs on.
# Run: python3 -m unittest tests.test_no_personal_names
import getpass
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# accounts that name a CI runner or a shared machine, not a person
SHARED = {"runner", "root", "admin", "user", "ci", "build", "jenkins", "github"}
TEXT = {".py", ".md", ".sh", ".json", ".jsonc", ".toml", ".yml", ".yaml", ".js", ".css", ".html", ".txt", ".zsh",
        ".bash", ".ini", ".cfg"}


def tracked_text_files() -> list:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True).stdout
    names = [n for n in out.decode().split("\0") if n]
    return [ROOT / n for n in names if Path(n).suffix in TEXT or Path(n).name in {"VERSION", "install.sh"}]


def naming(files, login: str) -> list:
    """The files and lines that name `login` as a whole word."""
    pattern = re.compile(rf"(?<![\w.-]){re.escape(login)}(?![\w-])")
    hits = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        hits += [f"{f.relative_to(ROOT)}:{i}" for i, line in enumerate(text.splitlines(), 1) if pattern.search(line)]
    return hits


class NoPersonalNames(unittest.TestCase):
    def test_no_tracked_file_names_this_macs_account(self):
        login = os.environ.get("DEVKIT_REAL_USER") or getpass.getuser()
        if not login or login.lower() in SHARED or len(login) < 4:
            self.skipTest(f"the account {login!r} names no person")
        files = tracked_text_files()
        self.assertGreater(len(files), 100)
        self.assertEqual(naming(files, login), [])

    def test_the_check_finds_a_login_in_a_path_and_as_a_word_but_not_inside_another_name(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as d:
            sample = Path(d) / "probe.md"
            sample.write_text("the prompt shows maria\nsee /Users/maria/Development\nmariana is another name\n")
            self.assertEqual([h.rsplit(":", 1)[1] for h in naming([sample], "maria")], ["1", "2"])


if __name__ == "__main__":
    unittest.main()
