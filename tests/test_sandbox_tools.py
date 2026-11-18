# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The suite's safety net keeps this Mac's global npm packages out of every test (tests/_sandbox.py): a test may read
# npm (`npm root -g`, which the visual tests use to find axe-core and @playwright/cli), but never install, update or
# remove a global package. Two layers: the tripwire refuses, in the test process, a start of the real npm that would
# change them; and the run's npm guard, first on PATH ahead of Homebrew's folders, refuses it in every child process
# and records it, so the test that caused it fails.
#
# No test here starts the real npm: a "real" npm is a script in a temporary folder that the test treats as outside
# what a test may start, and it leaves a file when it runs.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_sandbox_tools
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TREE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _hermetic  # noqa: E402
import _sandbox  # noqa: E402

# what a start of the "real" npm leaves behind: its arguments in `ran`, and a global folder on stdout
FAKE_NPM = '#!/bin/sh\nprintf "%s\\n" "$*" >> "$(dirname "$0")/ran"\necho /fake/lib/node_modules\n'

# A test module run on its own, in a process of its own: one test asks npm for a global install, which the run's npm
# guard refuses in the child; the "real" npm after the guard on PATH must never run. The other test starts nothing.
ONE_MODULE = '''
import os, subprocess, sys, unittest
sys.path[:0] = [{tests!r}]
import _hermetic
_hermetic.activate()


class Tiny(unittest.TestCase):
    def test_a_global_npm_install(self):
        subprocess.call(["npm", "install", "-g", "left-pad"], env=dict(os.environ, PATH=os.environ["PATH"] + ":" + {real!r}))

    def test_a_quiet_one(self):
        pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
'''


class Folders(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.home = self.root / "home"
        self.home.mkdir()
        self.real_npm = self.root / "opt-homebrew-bin" / "npm"
        self.real_npm.parent.mkdir()
        self.real_npm.write_text(FAKE_NPM)
        self.real_npm.chmod(0o755)
        self.ran = self.real_npm.parent / "ran"


class TripwireTests(Folders):
    def setUp(self):
        super().setUp()
        self.stubs = self.root / "stubs"  # the only folder a test may start npm from here
        self.stubs.mkdir()
        (self.stubs / "npm").write_text(FAKE_NPM)
        (self.stubs / "npm").chmod(0o755)

    def why(self, event, args):
        return _sandbox.refusal(event, args, allowed=(str(self.stubs),))  # resolves only, starts nothing

    def test_only_a_global_change_by_the_real_npm_is_refused(self):
        npm, env = str(self.real_npm), {"PATH": f"{self.real_npm.parent}:/usr/bin:/bin"}
        changes = {
            "install -g": ["npm", "install", "-g", "--no-fund", "--no-audit", "pyright"],
            "the flag first": ["npm", "-g", "uninstall", "pyright"],
            "an option's value before the command": ["npm", "--loglevel", "silent", "update", "--global"],
            "--location=global": ["npm", "i", "--location=global", "typescript"],
            "--location global": ["npm", "add", "--location", "global", "typescript"],
            "by its path": [npm, "rm", "-g", "typescript"],
            "through env": ["/usr/bin/env", "X=1", "npm", "install", "-g", "axe-core@4.14.0"],
            "a shell line": ["/bin/sh", "-c", "cd / && npm install -g --ignore-scripts @playwright/cli@0.1.24"],
            "an assignment in a shell line": ["/bin/sh", "-c", "npm_config_global=true npm install pyright"],
            "link, global by nature": ["npm", "link"],
        }
        for name, argv in changes.items():
            with self.subTest(name):
                self.assertIn(npm, self.why("subprocess.Popen", (None, argv, None, env)))
        for name, extra in (("npm_config_global", {"npm_config_global": "true"}),
                            ("NPM_CONFIG_LOCATION", {"NPM_CONFIG_LOCATION": "global"})):
            with self.subTest(name):
                self.assertIn(npm, self.why("subprocess.Popen", (None, ["npm", "install", "x"], None,
                                                                 dict(env, **extra))))
        with self.subTest("posix_spawn"):
            self.assertIn(npm, self.why("os.posix_spawn", (npm, [npm, "update", "-g"], env)))
        with self.subTest("os.system"):
            self.assertIn(npm, self.why("os.system", (f"'{npm}' install -g pyright",)))
        reads = {
            "npm root -g": ["npm", "root", "-g"],
            "a global list": ["npm", "ls", "-g", "--depth=0"],
            "a local install": ["npm", "install", "--no-audit"],
            "a view": ["npm", "view", "axe-core", "version"],
            "install as a package name to view": ["npm", "view", "install", "-g"],
        }
        for name, argv in reads.items():
            with self.subTest(name):
                self.assertEqual(self.why("subprocess.Popen", (None, argv, None, env)), "")
        with self.subTest("a test's own npm stand-in"):
            own = {"PATH": f"{self.stubs}:{self.real_npm.parent}:/usr/bin:/bin"}
            self.assertEqual(self.why("subprocess.Popen", (None, ["npm", "install", "-g", "x"], None, own)), "")

    def test_rare_forms_of_a_global_change_are_refused(self):
        # an option with a value npm's parser knows and ours does not, an abbreviated --global, or a --prefix that
        # names npm's global folder: refused whenever a global marker comes with a word that changes packages
        env = {"PATH": f"{self.real_npm.parent}:/usr/bin:/bin"}  # npm's global prefix here is self.root
        changes = {
            "an unknown option's value first": ["--fetch-timeout", "60000", "install", "-g", "x"],
            "--prefix of Homebrew": ["--prefix", "/opt/homebrew", "install", "x"],
            "--prefix= of the PATH's npm": [f"--prefix={self.root}", "uninstall", "x"],
            "-C of the PATH's npm": ["-C", f"{self.root}/", "update"],
            "npm_config_prefix": ["dedupe"],
            "--glob": ["--glob", "install", "x"],
            "--gl": ["prune", "--gl"],
            "--globa=true": ["--globa=true", "ci"],
        }
        for name, words in changes.items():
            with self.subTest(name):
                run = dict(env, npm_config_prefix=str(self.root)) if name == "npm_config_prefix" else env
                self.assertNotEqual(_sandbox.npm_change(words, run), "", words)
        reads = {
            "npm root -g": ["root", "-g"],
            "npm ls -g": ["ls", "-g"],
            "npm view": ["view", "axe-core", "version"],
            "a view of a package named install": ["view", "install", "-g"],
            "--glob=false": ["--glob=false", "install", "x"],
            "a local --prefix": ["--prefix", f"{self.root}/project", "install", "x"],
            "--globalconfig is not --global": ["--globalconfig", "/x/npmrc", "install", "x"],
        }
        for name, words in reads.items():
            with self.subTest(name):
                self.assertEqual(_sandbox.npm_change(words, env), "", words)

    def test_the_test_process_refuses_and_records_a_global_change_by_the_real_npm(self):
        seen = list(_hermetic.state()["violations"])  # the refusal provoked here is this test's own, not the run's
        self.addCleanup(lambda: _hermetic.state()["violations"].__setitem__(slice(None), seen))
        env = {"HOME": str(self.home), "PATH": f"{self.real_npm.parent}:/usr/bin:/bin"}
        with mock.patch.object(_sandbox, "ALLOWED", (str(self.stubs),)), self.assertRaises(PermissionError) as cm:
            subprocess.run(["npm", "install", "-g", "pyright"], env=env, capture_output=True)
        self.assertIn("tests/_sandbox.py", str(cm.exception))
        self.assertFalse(self.ran.exists(), "the real npm ran")
        self.assertEqual(len(_hermetic.state()["violations"]), len(seen) + 1)
        self.assertIn(str(self.real_npm), _hermetic.state()["violations"][-1])


class GuardTests(Folders):
    def setUp(self):
        super().setUp()
        self.guard = _sandbox.npm_guard(self.root / "guard")
        self.log = self.root / "tripwire.jsonl"

    def npm(self, *args, path):
        env = dict(os.environ, PATH=path, DEVKIT_TEST_TRIPWIRE=str(self.log))  # the trips go to this test's own log
        return subprocess.run(["npm", *args], env=env, capture_output=True, text=True, timeout=60)

    def test_a_global_change_in_a_child_is_refused_and_recorded_before_the_real_npm(self):
        r = self.npm("install", "-g", "--no-fund", "pyright", path=f"{self.guard}:{self.real_npm.parent}:/usr/bin:/bin")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("tests/_sandbox.py", r.stderr)
        self.assertFalse(self.ran.exists(), "the real npm ran")
        trips = _sandbox.trips(str(self.log))
        self.assertEqual(len(trips), 1, trips)
        self.assertIn("npm install -g --no-fund pyright", trips[0]["why"])
        self.assertEqual(trips[0]["by"], "npm guard")

    def test_a_read_passes_through_any_guard_to_the_real_npm(self):
        second = _sandbox.npm_guard(self.root / "guard-of-a-nested-run")
        r = self.npm("root", "-g", path=f"{self.guard}:{second}:{self.real_npm.parent}:/usr/bin:/bin")
        self.assertEqual((r.returncode, r.stdout), (0, "/fake/lib/node_modules\n"), r.stderr)
        self.assertEqual(self.ran.read_text(), "root -g\n")
        self.assertEqual(_sandbox.trips(str(self.log)), [])

    def test_without_a_real_npm_behind_it_npm_is_not_found(self):
        r = self.npm("root", "-g", path=f"{self.guard}:/usr/bin:/bin")
        self.assertEqual(r.returncode, 127, r.stderr)
        self.assertIn("not found", r.stderr)


class RunTests(Folders):
    def test_the_run_finds_its_npm_guard_first_but_keeps_it_out_of_the_stand_in_folders(self):
        state = _hermetic.state()
        if not state["active"]:
            self.skipTest("the run is not isolated (devkit was imported first)")
        found = shutil.which("npm")
        self.assertIsNotNone(found)
        self.assertTrue(os.path.realpath(found).startswith(os.path.realpath(state["root"]) + "/"), found)
        # a test that builds a system-only PATH from the stand-in folders keeps npm absent (tests/test_fresh_install.py)
        self.assertNotIn(os.path.dirname(found), os.environ["DEVKIT_TEST_PATH"].split(os.pathsep))

    def test_a_test_path_with_homebrew_still_finds_the_run_guard_before_the_real_npm(self):
        # tests that build their own PATH with Homebrew's folders in it (tests/test_pinned_install.py) give it to env()
        if not _hermetic.state()["active"]:
            self.skipTest("the run is not isolated (devkit was imported first)")
        log = self.root / "tripwire.jsonl"
        e = _sandbox.env({"HOME": str(self.home), "PATH": f"{self.real_npm.parent}:/usr/bin:/bin"},
                         DEVKIT_TEST_TRIPWIRE=str(log))
        r = subprocess.run(["npm", "install", "-g", "pyright"], env=e, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertFalse(self.ran.exists(), "the real npm ran")
        self.assertEqual(len(_sandbox.trips(str(log))), 1)
        r = subprocess.run(["npm", "root", "-g"], env=e, capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stdout), (0, "/fake/lib/node_modules\n"), r.stderr)
        self.assertNotIn(os.path.dirname(shutil.which("npm", path=e["PATH"])), e["DEVKIT_TEST_PATH"].split(os.pathsep))
        # a PATH without npm keeps it absent, with or without the run's folders (tests/test_fresh_install.py)
        for run in (True, False):
            bare = _sandbox.env({"HOME": str(self.home), "PATH": "/usr/bin:/bin"}, run=run)
            self.assertIsNone(shutil.which("npm", path=bare["PATH"]), run)

    def test_a_test_that_asks_for_a_global_npm_install_fails_naming_the_guard(self):
        module = self.root / "one" / "test_one.py"
        module.parent.mkdir()
        module.write_text(ONE_MODULE.format(tests=str(TREE / "tests"), real=str(self.real_npm.parent)))
        p = subprocess.run([sys.executable, "-I", str(module)], env=_sandbox.env({"HOME": str(self.home),
                                                                                    "PATH": "/usr/bin:/bin"}),
                           capture_output=True, text=True, timeout=120, cwd=str(module.parent))
        self.assertEqual(1, p.returncode, p.stderr)
        self.assertIn("FAILED (failures=1)", p.stderr)
        self.assertRegex(p.stderr, r"test_a_quiet_one \(.*\) \.\.\. ok")
        failure = p.stderr.split("\nFAIL: ", 1)[-1]
        self.assertTrue(failure.startswith("test_a_global_npm_install"), p.stderr)
        self.assertIn("npm install -g left-pad", failure)
        self.assertIn("npm guard", failure)
        self.assertFalse(self.ran.exists(), "the real npm ran")


if __name__ == "__main__":
    unittest.main(verbosity=2)
