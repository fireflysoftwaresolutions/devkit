# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev test run` with a list of tests in one argument: zsh does not split an unquoted $VAR into words, so
`dev test run $MODULES` passes the whole list as one word. Hermetic: a tiny unittest project in a temporary folder
(test_testx.Env). Run: python3 -m unittest tests.test_testx_args -v"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import testx  # noqa: E402
from test_testx import CALC, Env, dev, edit  # noqa: E402


class OneWordListTests(unittest.TestCase):
    # each test of a list that arrived as one word runs, and the run says it split the word
    def test_a_list_of_files_or_modules_in_one_word_runs_each(self):
        for word in ("tests/test_calc.py tests/test_other.py", "tests.test_calc  tests.test_other\n"):
            with self.subTest(word=word), Env() as e:
                r = e.repo(CALC)
                rc, out, err = dev(r, "run", word, "--json")
                run = json.loads(out)["steps"][0]["runs"][0]
                self.assertEqual((rc, run["count"], run["failed"]), (0, 3, 0), out + err)
                self.assertIn("split", err)

    # a pytest id whose parameters hold a space is one test, alone or in a list
    def test_a_pytest_id_with_a_space_in_its_parameters_stays_whole(self):
        one = "tests/test_calc.py::test_add[one two]"
        self.assertEqual([one], testx._split_lists([one]))
        self.assertEqual(["tests/test_other.py", one, "tests/test_calc.py::test_sub[a b c]"],
                         testx._split_lists([f"tests/test_other.py {one}\ttests/test_calc.py::test_sub[a b c]"]))

    def test_prove_takes_a_list_in_one_word_as_its_tests(self):
        with Env() as e:
            r = e.repo(CALC)
            edit(r, {"calc.py": CALC["calc.py"] + "\n\ndef mul(a, b):\n    return a * b\n",
                     "tests/test_calc.py": CALC["tests/test_calc.py"].rstrip(" ") + (
                         "            def test_mul(self):\n                self.assertEqual(calc.mul(2, 3), 6)\n"),
                     "tests/test_other.py": CALC["tests/test_other.py"].rstrip(" ") + (
                         "            def test_more(self):\n                self.assertEqual(len('ab'), 2)\n")})
            rc, out, err = dev(r, "prove", "tests/test_calc.py tests/test_other.py", "--runs", "1", "--json")
            res = json.loads(out)
            self.assertEqual([], res["skipped"], out + err)
            self.assertEqual({"tests/test_calc.py", "tests/test_other.py"}, {t["file"] for t in res["tests"]})


if __name__ == "__main__":
    unittest.main()
