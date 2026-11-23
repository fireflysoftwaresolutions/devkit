# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev test --listed` (or config test.listing): which files hold tests comes from the runner's own listing (pytest
--collect-only, unittest's discovery, vitest list, playwright test --list, jest --listTests), not from file names,
cached by the runner's configuration and the project's file names. Hermetic: tiny git repositories in temporary
folders (test_testx.Env); pytest is a stand-in that lists what its listing.txt says, as the runner's own word.
Run: python3 -m unittest tests.test_testx_listing -v"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import testx  # noqa: E402
from test_testx import Env, dev, write  # noqa: E402

# `python -m pytest --collect-only -q`: the node ids of listing.txt, one per line, and a count of the listings it
# made in listed.count, so a test sees whether the cache answered
LISTING_PYTEST = '''
import os
import sys

if "--collect-only" in sys.argv:
    with open("listed.count", "a") as fh:
        fh.write("1")
    print(open("listing.txt").read().strip())
    print()
    print("3 tests collected in 0.01s")
    sys.exit(0)
sys.exit(4)
'''

PROJECT = {
    "pytest.ini": "[pytest]\npython_files = check_*.py test_*.py\n",
    "pytest.py": LISTING_PYTEST,
    "app/__init__.py": "",
    "app/core.py": "X = 1\n",
    "app/test_probe.py": "def test_db(session):\n    return session\n",  # named like a test; the runner skips it
    "tests/check_core.py": "from app import core\n\n\ndef test_x():\n    assert core.X == 1\n",
    "tests/test_more.py": "from app import core\n\n\ndef test_y():\n    assert core.X\n",
    "listing.txt": "tests/check_core.py::test_x\ntests/test_more.py::test_y\ntests/test_more.py::test_z\n",
    ".gitignore": "listed.count\n__pycache__/\n",
}


def listed(r):
    count = Path(r) / "listed.count"
    return len(count.read_text()) if count.exists() else 0


class ListingTests(unittest.TestCase):
    def setUp(self):
        on = mock.patch.dict(os.environ, {"DEVKIT_TEST_LISTING": "1"})
        on.start()
        self.addCleanup(on.stop)

    # the runner's listing decides which files are tests: one named otherwise that it collects is one, one named like
    # a test that it does not collect is not
    def test_the_files_the_runner_lists_are_the_tests(self):
        with Env() as e:
            r = e.repo(PROJECT, branch=None)
            repo = testx._Repo(str(r))
            self.assertEqual(["tests/check_core.py", "tests/test_more.py"], repo.tests)
            self.assertFalse(repo.runnable("app/test_probe.py"))
            radius = testx.blast_radius(str(r), ["app/core.py"])
            self.assertEqual(["tests/check_core.py", "tests/test_more.py"], radius["tests"])

    # a second look reads the cache; a new file or a changed configuration lists again
    def test_the_listing_is_cached_by_the_configuration_and_the_file_names(self):
        with Env() as e:
            r = e.repo(PROJECT, branch=None)
            testx._Repo(str(r))
            testx._Repo(str(r))
            self.assertEqual(1, listed(r))
            write(r, {"tests/check_new.py": "def test_n():\n    assert True\n",
                      "listing.txt": PROJECT["listing.txt"] + "tests/check_new.py::test_n\n"})
            self.assertIn("tests/check_new.py", testx._Repo(str(r)).tests)
            self.assertEqual(2, listed(r))
            write(r, {"pytest.ini": "[pytest]\npython_files = check_*.py\n",
                      "listing.txt": "tests/check_core.py::test_x\ntests/check_new.py::test_n\n"})
            self.assertEqual(["tests/check_core.py", "tests/check_new.py"], testx._Repo(str(r)).tests)
            self.assertEqual(3, listed(r))

    # unittest's own discovery lists a unittest suite: a module named like a test that holds no test is no test file
    def test_unittest_discovery_lists_a_unittest_suite(self):
        with Env() as e:
            r = e.repo({"calc.py": "def add(a, b):\n    return a + b\n",
                        "tests/test_calc.py": "import sys\nimport unittest\nfrom pathlib import Path\n\n"
                                              "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
                                              "import calc  # noqa: E402\n\n\nclass T(unittest.TestCase):\n"
                                              "    def test_add(self):\n        self.assertEqual(calc.add(1, 2), 3)\n",
                        "tests/test_support.py": "def make():\n    return 1\n"}, branch=None)
            self.assertEqual(["tests/test_calc.py"], testx._Repo(str(r)).tests)

    # a listing cached by one devkit is not reused by a devkit whose listing reads the suite otherwise: the changed
    # listing script lists again
    def test_a_changed_listing_script_lists_again_instead_of_reading_the_old_cache(self):
        with Env() as e:
            r = e.repo({"tests/test_calc.py": "import unittest\n\n\nclass T(unittest.TestCase):\n"
                                              "    def test_a(self):\n        self.assertEqual(1, 1)\n",
                        "tests/test_support.py": "def make():\n    return 1\n"}, branch=None)
            self.assertEqual(["tests/test_calc.py"], testx._Repo(str(r)).tests)
            every_module = testx._LIST_UNITTEST.replace(
                "walk(suite)\n", "walk(suite)\nout.update(os.path.abspath(os.path.join(start, f)) "
                                 "for f in os.listdir(start) if f.endswith('.py'))\n")
            with mock.patch.object(testx, "_LIST_UNITTEST", every_module):
                self.assertEqual(["tests/test_calc.py", "tests/test_support.py"], testx._Repo(str(r)).tests)

    # a test module that fails to import is a failing test file, not a non-test: the listing keeps it, and its
    # recorded failure stays until it passes
    def test_a_unittest_module_that_fails_to_import_stays_a_test_and_keeps_its_failure(self):
        ok = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(1, 1)\n"
        with Env() as e:
            r = e.repo({"tests/__init__.py": "", "tests/test_ok.py": ok, "tests/test_broken.py": ok})
            write(r, {"tests/test_broken.py": "import no_such_module\n" + ok})
            self.assertEqual(["tests/test_broken.py", "tests/test_ok.py"], testx._Repo(str(r)).tests)
            for _ in range(2):  # the second run starts with the recorded failure
                rc, out, err = dev(r, "--json", "--listed")
                failed = [f["id"] for s in json.loads(out)["steps"] for x in s["runs"] for f in x["failures"]]
                self.assertEqual((1, True), (rc, any("test_broken" in i for i in failed)), out + err)
                self.assertNotIn("no longer select", out + err)

    # a test package whose __init__ fails to import hides its modules from unittest's discovery: the files the file
    # rules name in that folder stay tests (each fails at that import), and the __init__ itself is none
    def test_a_unittest_package_that_fails_to_import_keeps_the_tests_in_its_folder(self):
        ok = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(1, 1)\n"
        with Env() as e:
            r = e.repo({"tests/__init__.py": "", "tests/test_ok.py": ok, "tests/sub/__init__.py": "",
                        "tests/sub/test_in_sub.py": ok, "tests/sub/helper.py": "X = 1\n"})
            write(r, {"tests/sub/__init__.py": "import no_such_module\n"})
            self.assertEqual(["tests/sub/test_in_sub.py", "tests/test_ok.py"], testx._Repo(str(r)).tests)

    # dev test plan --listed says the selection came from the runner's listing
    def test_plan_says_the_tests_come_from_the_runners_listing(self):
        with Env() as e:
            r = e.repo(PROJECT)
            write(r, {"app/core.py": "X = 2\n"})
            os.environ.pop("DEVKIT_TEST_LISTING")  # the flag turns it on
            rc, out, err = dev(r, "plan", "--listed", "--json")
            self.assertEqual(0, rc, out + err)
            data = json.loads(out)
            self.assertEqual(["tests/check_core.py", "tests/test_more.py"], data["radius"]["tests"])
            self.assertEqual("listed", data["runs"][0]["selection"])


if __name__ == "__main__":
    unittest.main()
