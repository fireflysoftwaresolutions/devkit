# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev test`'s blast radius (testx.blast_radius) for tests that see a change without importing it: tests that read a
whole source folder, and tests of a script that quotes a file's path. Hermetic: tiny git repositories in temporary
folders (test_testx.Env). Run: python3 -m unittest tests.test_testx_radius -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import testx  # noqa: E402
from test_testx import Env  # noqa: E402

TREE_READER = {
    "pkg/__init__.py": "",
    "pkg/core.py": "import json\n",
    "pkg/sub/__init__.py": "",
    "pkg/sub/deep.py": "import os\n",
    "other/tool.py": "import re\n",
    "tests/__init__.py": "",
    # reads every module of pkg as text (an AST scan), imports none of them
    "tests/test_scan.py": "import ast, unittest\nfrom pathlib import Path\nROOT = Path(__file__).parents[1]\n\n\n"
                          "class Scan(unittest.TestCase):\n    def test_imports(self):\n"
                          "        for p in (ROOT / 'pkg').rglob('*.py'):\n            ast.parse(p.read_text())\n",
    # the same with the folder written as one path
    "tests/test_walk.py": "import os, unittest\n\n\nclass Walk(unittest.TestCase):\n    def test_walk(self):\n"
                          "        for d, _, fs in os.walk('pkg/sub'):\n            pass\n",
    "tests/test_unrelated.py": "import unittest\nimport other.tool\n",
}


class TreeReaderTests(unittest.TestCase):
    # A test that walks a source folder (Path.rglob, glob, iterdir, os.walk, os.listdir, os.scandir) depends on every
    # file under it, though it imports none: a lane that adds an import to one module never ran it and broke it.
    def radius(self, files, changed):
        with Env() as e:
            return testx.blast_radius(str(e.repo(files, branch=None)), changed)

    def test_a_change_under_a_folder_a_test_walks_selects_that_test(self):
        res = self.radius(TREE_READER, ["pkg/core.py"])
        self.assertEqual("some", res["mode"])
        self.assertIn("tests/test_scan.py", res["tests"])
        self.assertNotIn("tests/test_walk.py", res["tests"])  # it walks pkg/sub only
        self.assertNotIn("tests/test_unrelated.py", res["tests"])
        deep = self.radius(TREE_READER, ["pkg/sub/deep.py"])
        self.assertEqual(["tests/test_scan.py", "tests/test_walk.py"],
                         [t for t in deep["tests"] if t != "tests/test_unrelated.py"])
        self.assertEqual(["tests/test_unrelated.py"], self.radius(TREE_READER, ["other/tool.py"])["tests"])


QUOTER = {
    "lib/app/__init__.py": "",
    "lib/app/state.py": "import os\n\n\ndef tighten(fd):\n    os.fchmod(fd, 0o700)\n",
    "lib/app/other.py": "X = 1\n",
    # a script that quotes reviewed lines of another file by its path, and reads that file at run time
    "scripts/corpus.py": "REVIEWED = ({'path': 'lib/app/state.py', 'expressions': ('os.fchmod(fd, 0o700)',)},)\n",
    "tests/__init__.py": "",
    "tests/test_corpus.py": "import importlib.util, unittest\nSCRIPT = 'scripts/corpus.py'\n",
    "tests/test_state.py": "from lib.app import state\n",
    "tests/test_other.py": "import unittest\n",
}


class QuotedPathTests(unittest.TestCase):
    # A code file that names another file by its whole path reads it (the security corpus quotes reviewed lines of
    # lib/devkit/state.py): a change to the quoted file selects the tests of the file that quotes it.
    def test_a_change_to_a_file_a_script_quotes_selects_the_scripts_tests(self):
        with Env() as e:
            r = str(e.repo(QUOTER, branch=None))
            res = testx.blast_radius(r, ["lib/app/state.py"])
            self.assertIn("tests/test_corpus.py", res["tests"])
            self.assertIn("tests/test_state.py", res["tests"])
            self.assertNotIn("tests/test_other.py", res["tests"])
            self.assertNotIn("tests/test_corpus.py", testx.blast_radius(r, ["lib/app/other.py"])["tests"])


if __name__ == "__main__":
    unittest.main()
