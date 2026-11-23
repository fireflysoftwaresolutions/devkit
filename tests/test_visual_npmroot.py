# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# lib/devkit/visual/npmroot.py asks npm for its global folder (`npm root -g`), where the pinned @playwright/cli and
# axe-core live, for devkit's Python, with the rule of npmroot.cjs: a good answer (exit 0 and one absolute folder) is
# kept for the process; an answer that is no folder (npm printed nothing, failed, timed out) is asked again once; and
# when npm still names none, the caller says "npm root -g gave no folder" and what npm did, never that the pinned
# Playwright or axe-core is missing. Its callers here: the browser of the comparison (compare._playwright, which
# `dev visual verify`, `dev visual compare` and the marks use) and the folder `dev visual` names for axe-core
# (commands.py); wizard.py's are in tests/test_wizard.py.
#
# A stand-in npm first on PATH answers from a plan, one line a call: `empty` prints nothing, `fail` exits 1, `slow`
# sleeps past the timeout, anything else is printed as the folder; it logs every call. The folder it names holds a
# playwright-core as dev install puts it under @playwright/cli. Each caller runs in a Python process of its own, so a
# test sees what one process asks.
# Tests carry comments, not docstrings: `dev test prove` leaves out tests with a docstring.
# Run: python3 -m unittest tests.test_visual_npmroot
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "lib"
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
import _sandbox  # noqa: E402  (the net against this Mac's real codex, claude, brew and gh)

sys.path.insert(0, str(LIB))
ANSI = re.compile(r"\x1b\[[0-9;]*m")
# what a caller's process runs first: devkit on its path, and said(f), f's answer or the error it raised as text
PRELUDE = (f"import json, sys\nsys.path.insert(0, {str(LIB)!r})\nfrom pathlib import Path\n"
           "def said(f):\n"
           "    try:\n"
           "        return f()\n"
           "    except Exception as e:\n"
           "        return f'{type(e).__name__}: {e}'\n")


class NpmStandIn:
    """A stand-in npm first on PATH for the test (mixed into a TestCase), and a Python process that runs with it."""

    def start_npm(self):
        self.top = Path(tempfile.mkdtemp(prefix="visual-npmroot-test-"))
        self.addCleanup(shutil.rmtree, self.top, True)
        self.bin = self.top / "bin"
        self.bin.mkdir()
        self.plan, self.log = self.top / "plan", self.top / "calls"
        npm = self.bin / "npm"
        npm.write_text("#!/bin/sh\n"
                       f"echo \"$*\" >> '{self.log}'\n"
                       f"line=$(sed -n \"$(wc -l < '{self.log}' | tr -d ' ')p\" '{self.plan}')\n"
                       'case "$line" in\n'
                       "  empty) exit 0 ;;\n"
                       '  fail) echo "npm error code EAGAIN" >&2; exit 1 ;;\n'
                       "  slow) exec sleep 5 ;;\n"
                       '  *) echo "$line" ;;\n'
                       "esac\n")
        npm.chmod(0o755)
        self.globals = self.top / "npm-globals"
        self.globals.mkdir()

    def answers(self, *lines):
        if self.log.exists():
            self.log.unlink()
        self.plan.write_text("".join(f"{line}\n" for line in lines))

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def python(self, body: str) -> subprocess.CompletedProcess:
        # one process, with the stand-in npm first on PATH; body prints its answer as JSON
        p = subprocess.run([sys.executable, "-c", PRELUDE + body], capture_output=True, text=True, timeout=120,
                           env=_sandbox.env(stubs=[self.bin]), cwd=str(self.top))
        self.assertEqual(p.returncode, 0, p.stderr)
        return p


class Lookup(NpmStandIn, unittest.TestCase):
    def setUp(self):
        self.start_npm()
        from devkit.visual import npmroot
        self.npmroot = npmroot
        for patcher in (mock.patch.dict(os.environ, {"PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}"}),
                        mock.patch.object(npmroot, "_kept", None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_an_answer_that_is_no_folder_twice_names_what_npm_did(self):
        for answer, said in (("empty", "it printed nothing"), ("fail", "it failed with exit 1"),
                             ("relative/folder", "it printed no absolute folder")):
            with self.subTest(answer=answer):
                self.answers(answer, answer)
                with self.assertRaises(self.npmroot.NoFolder) as e:
                    self.npmroot.global_root()
                self.assertEqual(str(e.exception), f"npm root -g gave no folder ({said}, twice)")
                self.assertEqual(self.calls(), ["root -g", "root -g"])

    def test_an_npm_that_does_not_answer_in_time_is_asked_again_then_named(self):
        self.answers("slow", "slow")
        with mock.patch.object(self.npmroot, "TIMEOUT", 1), self.assertRaises(self.npmroot.NoFolder) as e:
            self.npmroot.global_root()
        self.assertEqual(str(e.exception), "npm root -g gave no folder (it did not answer in 1 s, twice)")
        self.assertEqual(len(self.calls()), 2)

    def test_an_npm_that_cannot_start_is_named(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.top / "no-npm-here")}), \
                self.assertRaises(self.npmroot.NoFolder) as e:
            self.npmroot.global_root()
        self.assertEqual(str(e.exception), "npm root -g gave no folder (it could not start (ENOENT), twice)")

    def test_a_folder_on_the_second_ask_is_the_answer(self):
        for first in ("empty", "fail", "slow"):
            with self.subTest(first=first), mock.patch.object(self.npmroot, "_kept", None), \
                    mock.patch.object(self.npmroot, "TIMEOUT", 1):
                self.answers(first, self.globals)
                self.assertEqual(self.npmroot.global_root(), self.globals)
                self.assertEqual(len(self.calls()), 2)

    def test_a_good_answer_is_kept_for_the_process(self):
        self.answers(self.globals, "fail")
        self.assertEqual([self.npmroot.global_root(), self.npmroot.global_root()], [self.globals, self.globals])
        self.assertEqual(self.calls(), ["root -g"])

    def test_no_folder_is_not_kept_so_the_next_lookup_asks_again(self):
        self.answers("empty", "empty", self.globals)
        with self.assertRaises(self.npmroot.NoFolder):
            self.npmroot.global_root()
        self.assertEqual(self.npmroot.global_root(), self.globals)
        self.assertEqual(len(self.calls()), 3)


class Callers(NpmStandIn, unittest.TestCase):
    # the record of a capture whose axe.min.js does not match the pin, as test_visual_cli's CaptureLinesTest has it
    AXE_MISMATCH = {"id": "VC-g33z1a8k0e", "kind": "web", "state": "list", "recipe": {"viewport": [390, 844]},
                    "image": {"width": 390, "height": 844, "fold": 844}, "env": {"browser": "chromium 1254"},
                    "redaction": {}, "checks": {"ran": ["devkit"], "skipped": {"axe": "hash mismatch"}, "signals": []}}

    def setUp(self):
        self.start_npm()
        node = self.bin / "node"  # the comparison only needs node on PATH before it looks for Playwright
        node.write_text("#!/bin/sh\nexit 0\n")
        node.chmod(0o755)
        self.core = self.globals / "@playwright" / "cli" / "node_modules" / "playwright-core"
        self.core.mkdir(parents=True)
        (self.core / "package.json").write_text(json.dumps({"name": "playwright-core", "version": "1.99.0"}))

    def comparisons(self, n: int) -> list:
        return json.loads(self.python("from devkit.visual import compare\n"
                                      f"print(json.dumps([said(compare._playwright) for _ in range({n})]))").stdout)

    def axe_warnings(self, n: int) -> str:
        # the lines `dev visual` prints after n such captures, in one process; its warnings go to stderr
        p = self.python("from devkit.visual import commands\n"
                        f"for _ in range({n}):\n"
                        f"    commands._print_captures([{self.AXE_MISMATCH!r}], 'VA-g33z0k2m4q', 'next')\n")
        return ANSI.sub("", p.stderr)

    def test_the_comparison_says_npm_gave_no_folder_instead_of_no_browser(self):
        for answer, said in (("empty", "it printed nothing"), ("fail", "it failed with exit 1")):
            with self.subTest(answer=answer):
                self.answers(answer, answer)
                self.assertEqual(self.comparisons(1), [
                    f"CompareError: No browser to compare with: npm root -g gave no folder ({said}, twice), so devkit "
                    "cannot find the pinned Playwright. Check that npm root -g prints a folder here, then run the "
                    "command again."])

    def test_the_marks_say_npm_gave_no_folder(self):
        self.answers("empty", "empty")
        out = self.python("from devkit.visual import judge\n"
                          f"print(json.dumps(said(lambda: judge._draw({{}}, Path({str(self.top / 'work')!r})))))")
        self.assertEqual(json.loads(out.stdout),
                         "VisualError: No browser to draw the marks with: npm root -g gave no folder (it printed "
                         "nothing, twice), so devkit cannot find the pinned Playwright. Check that npm root -g prints "
                         "a folder here, then run the command again.")

    def test_the_comparison_finds_playwright_on_the_second_ask_and_asks_once_for_the_process(self):
        self.answers("empty", self.globals, "fail")
        self.assertEqual(self.comparisons(2), [{"node": str(self.bin / "node"), "core": str(self.core)}] * 2)
        self.assertEqual(len(self.calls()), 2)

    def test_the_axe_warning_says_npm_gave_no_folder_instead_of_naming_a_made_up_one(self):
        self.answers("fail", "fail")
        self.assertIn("axe.min.js in npm's global folder does not match axe-core 4.14.0. The accessibility rules did "
                      "not run on VC-g33z1a8k0e. Reinstall it: dev install --only tools. npm root -g gave no folder "
                      "(it failed with exit 1, twice), so devkit cannot name the folder.\n", self.axe_warnings(1))

    def test_the_axe_warning_names_the_folder_of_the_second_ask_and_asks_once_for_the_process(self):
        self.answers("empty", self.globals, "fail")
        line = (f"axe.min.js in {self.globals / 'axe-core'} does not match axe-core 4.14.0. The accessibility rules "
                "did not run on VC-g33z1a8k0e. Reinstall it: dev install --only tools.\n")
        self.assertEqual(self.axe_warnings(2).count(line), 2)
        self.assertEqual(len(self.calls()), 2)


if __name__ == "__main__":
    unittest.main()
