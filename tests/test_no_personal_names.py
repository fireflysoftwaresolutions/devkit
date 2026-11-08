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


# folders no checkout tracks: git's own, devkit's ignored ones (.gitignore) and the caches a copy of a working folder
# can hold
UNTRACKED_DIRS = {".git", "__pycache__", "dist", "node_modules", ".venv", "venv", ".tox", ".mypy_cache",
                  ".pytest_cache", ".ruff_cache"}


def tracked_text_files(root: Path = ROOT) -> list:
    """The text files the checkout at `root` tracks; in an exported copy (git archive, no .git), which holds only
    tracked files, every file outside the folders no checkout tracks."""
    if (root / ".git").exists():
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
        names = [n for n in out.decode().split("\0") if n]
    else:
        names = []
        for d, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if x not in UNTRACKED_DIRS]
            names += [Path(d, f).relative_to(root).as_posix() for f in files]
    return [root / n for n in names if Path(n).suffix in TEXT or Path(n).name in {"VERSION", "install.sh"}]


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

    # an exported copy (git archive, no .git) holds only tracked files: the check reads the same text files there as
    # git ls-files lists in the checkout, and not the caches and build output a copy can pick up
    def test_an_exported_copy_is_checked_on_the_files_the_checkout_tracks(self):
        with tempfile.TemporaryDirectory() as d:
            repo, export = Path(d) / "repo", Path(d) / "export"
            files = {"README.md": "r\n", "VERSION": "26.11.7\n", "install.sh": "x\n", "lib/a.py": "a = 1\n",
                     "docs/guide.md": "g\n", "assets/logo.png": "png", ".gitignore": "dist/\n__pycache__/\n",
                     "dist/out.js": "ignored\n", "lib/__pycache__/a.py": "ignored\n"}
            for rel, text in files.items():
                (repo / rel).parent.mkdir(parents=True, exist_ok=True)
                (repo / rel).write_text(text)
            git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
            for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "init"]):
                subprocess.run(git + args, check=True, capture_output=True)
            export.mkdir()
            tar = subprocess.run(git + ["archive", "HEAD"], check=True, capture_output=True).stdout
            subprocess.run(["tar", "-x", "-C", str(export)], input=tar, check=True)
            for rel in ("lib/__pycache__/a.py", "node_modules/m/index.js", ".venv/lib/site.py", "dist/out.js"):
                (export / rel).parent.mkdir(parents=True, exist_ok=True)  # what a copy of a working folder can hold
                (export / rel).write_text("not tracked\n")
            tracked = sorted(str(f.relative_to(repo)) for f in tracked_text_files(repo))
            self.assertEqual(["README.md", "VERSION", "docs/guide.md", "install.sh", "lib/a.py"], tracked)
            self.assertEqual(tracked, sorted(str(f.relative_to(export)) for f in tracked_text_files(export)))

    def test_the_check_finds_a_login_in_a_path_and_as_a_word_but_not_inside_another_name(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as d:
            sample = Path(d) / "probe.md"
            sample.write_text("the prompt shows maria\nsee /Users/maria/Development\nmariana is another name\n")
            self.assertEqual([h.rsplit(":", 1)[1] for h in naming([sample], "maria")], ["1", "2"])


if __name__ == "__main__":
    unittest.main()
