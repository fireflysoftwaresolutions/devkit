# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# What Semgrep scans in this repository (`dev sec scan`, the Security engineer's full scans). Without a .semgrepignore
# Semgrep applies its built-in list, which skips test/ and tests/; devkit's tests hold fixtures, scripts and hook
# payloads that run on the developer's machine, so the scans cover them on purpose. Build output, caches and vendored
# files stay skipped. The patterns are read with gitignore's rules, as Semgrep reads them.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_semgrepignore
import fnmatch
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Semgrep's built-in list, used when the repository has no .semgrepignore
SEMGREP_DEFAULTS = ["node_modules/", "build/", "dist/", "vendor/", ".env/", ".venv/", ".tox/", "*.min.js", ".npm/",
                    ".yarn/", "test/", "tests/", "*_test.go", ".semgrep", ".semgrep_logs/"]


def patterns() -> list:
    f = ROOT / ".semgrepignore"
    if not f.exists():
        return SEMGREP_DEFAULTS
    lines = (ln.strip() for ln in f.read_text().splitlines())
    return [ln for ln in lines if ln and not ln.startswith("#")]


def ignored(rel: str, pats: list) -> bool:
    # gitignore's rules for these patterns: "x/" matches a folder named x at any depth, a pattern with no slash
    # matches a file or folder name at any depth, one with a slash matches from the repository root
    parts = rel.split("/")
    for p in pats:
        if p.endswith("/"):
            if any(fnmatch.fnmatch(d, p[:-1]) for d in parts[:-1]):
                return True
        elif "/" in p.strip("/"):
            if fnmatch.fnmatch(rel, p.lstrip("/")) or fnmatch.fnmatch(rel, p.lstrip("/") + "/*"):
                return True
        elif any(fnmatch.fnmatch(x, p) for x in parts):
            return True
    return False


def files_under_tests() -> list:
    # every file under tests/ but the interpreter's caches, which git ignores too
    out = []
    for d, dirs, files in os.walk(ROOT / "tests"):
        dirs[:] = [x for x in dirs if x != "__pycache__"]
        out += [os.path.relpath(os.path.join(d, f), ROOT).replace(os.sep, "/") for f in files if not f.endswith(".pyc")]
    return sorted(out)


class SemgrepScopeTests(unittest.TestCase):
    def test_the_scans_cover_every_file_under_tests(self):
        pats = patterns()
        files = files_under_tests()
        self.assertGreater(len(files), 50)
        skipped = [f for f in files if ignored(f, pats)]
        self.assertEqual(skipped, [], f"{len(skipped)} of {len(files)} files under tests/ are not scanned")

    def test_build_output_caches_and_vendored_files_stay_skipped(self):
        pats = patterns()
        for rel in ("dist/devkit-26.11.6.mcpb", "build/lib/devkit/core.py", "lib/devkit/__pycache__/core.cpython-39.pyc",
                    "tests/__pycache__/test_ui.cpython-315.pyc", ".ruff_cache/0.6.0/123", ".pytest_cache/v/cache/x",
                    "node_modules/chart.js/dist/chart.umd.js", "vendor/lib.py", ".venv/lib/site.py",
                    "lib/devkit/web/chart.min.js"):
            self.assertTrue(ignored(rel, pats), rel)


if __name__ == "__main__":
    unittest.main()
