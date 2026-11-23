# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""A multi-line CI step that runs one test command (lib/devkit/testx.py step_runner and recognize).

`dev ci --run` observes the test runner of a step only when devkit recognizes it: then the step's record carries the
runner, the failed tests and the completion receipt, and `dev ci --run --resume` re-runs only the failed and affected
tests. A multi-line step is recognized when it runs exactly one test command and every other line cannot change what
that command runs; any other multi-line step is re-run whole, and devkit says why.
Run: python3 -m unittest tests.test_testx_multiline -v
"""
import random
import sys
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import gitx, testx  # noqa: E402
import test_gitx as G  # noqa: E402
from test_testx import CALC, BROKEN_ADD, Env, edit, git, write  # noqa: E402

# devkit's own Unit tests step before it became one plain command: a part of the suite per matrix leg, the whole
# suite without one; written out here so the test does not depend on the workflow file
OLD_PART_SCRIPT = textwrap.dedent("""\
    if [ -z "${DEVKIT_TEST_PART:-}" ]; then
      python -m unittest discover -s tests -t .
      exit
    fi
    modules=$(cd tests && for f in test_*.py; do [ "$f" = test_zz_real_state_untouched.py ] || echo "$f"; done |
      awk -v n="${PARTS:-1}" -v k="$DEVKIT_TEST_PART" '(NR - 1) % n == k - 1 { sub(/[.]py$/, ""); print "tests." $0 }')
    if [ -z "$modules" ]; then echo "no test file in part $DEVKIT_TEST_PART of ${PARTS:-1}" >&2; exit 1; fi
    # shellcheck disable=SC2086 # one module name per word
    python -m unittest $modules tests.test_zz_real_state_untouched
    """)


class MultiLineRecognizeTests(unittest.TestCase):
    def repo(self, e):
        return str(e.repo({"services/api/tests/test_a.py": "", "tests/test_a.py": ""}, branch=None))

    def test_one_test_command_beside_harmless_lines_is_recognized_in_the_folder_it_runs_in(self):
        with Env() as e:
            top = self.repo(e)
            step = textwrap.dedent("""\
                # the unit suite
                set -euo pipefail

                LEVEL=debug
                export DEVKIT_NO_NOTIFY=1 SUITE=unit
                export LEVEL
                echo "running the suite; it takes a minute"
                printf '%s\\n' start
                cd services
                cd api
                python -m unittest discover \\
                  -s tests -v  # verbose, for the log
                echo done
                """)
            runner, why = testx.step_runner(step, top, top)
            self.assertIsNone(why)
            self.assertEqual((runner["name"], runner["root"], runner["start"]), ("unittest", "services/api", "tests"))
            self.assertEqual(runner["env"], {"DEVKIT_NO_NOTIFY": "1", "SUITE": "unit", "LEVEL": "debug"})
            self.assertEqual(testx.recognize(step, top, top), runner)

    def test_the_test_lines_own_assignments_win_over_exported_ones(self):
        with Env() as e:
            top = self.repo(e)
            runner, _ = testx.step_runner("export MODE=ci\nMODE=local pytest -q\n", top, top)
            self.assertEqual((runner["name"], runner["env"]), ("pytest", {"MODE": "local"}))

    def test_a_step_it_cannot_reproduce_is_left_whole_with_the_reason(self):
        with Env() as e:
            top = self.repo(e)
            cases = {
                "python -m unittest discover -s tests\npytest -q\n": "two test commands",
                "for d in a b; do echo $d; done\npytest -q\n": "a loop",
                "while true; do break; done\npytest -q\n": "a loop",
                "if [ -n \"$CI\" ]; then\n  pytest -q\nfi\n": "a conditional",
                "case $X in\n  a) pytest -q ;;\nesac\n": "a conditional",
                "[ -n \"$CI\" ] && echo ci\npytest -q\n": "a conditional",
                "run() {\n  pytest -q\n}\nrun\n": "a function",
                "function run {\n  pytest -q\n}\n": "a function",
                "cat > conf.ini <<EOF\n[x]\nEOF\npytest -q\n": "a here-document",
                "pip install -e .\npytest -q\n": "pip install -e .",
                "echo $(date)\npytest -q\n": "echo $(date)",
                "export ROOT=$PWD/lib\npytest -q\n": "export ROOT=$PWD/lib",
                "cd $HOME\npytest -q\n": "cd $HOME",
                "cd ..\npytest -q\n": "cd ..",
                "set -a\nMODE=x\npytest -q\n": "set -a",
                "pytest -q | tee log\necho done\n": "runs the test command with other commands",
                "ROOT=$PWD/lib\nexport ROOT\npytest -q\n": "export ROOT",
            }
            for step, reason in cases.items():
                with self.subTest(step=step):
                    runner, why = testx.step_runner(step, top, top)
                    self.assertIsNone(runner)
                    self.assertIn(reason, why)
                    self.assertIsNone(testx.recognize(step, top, top))

    def test_devkits_old_part_script_is_left_whole_because_of_its_conditional(self):
        with Env() as e:
            top = self.repo(e)
            runner, why = testx.step_runner(OLD_PART_SCRIPT, top, top)
            self.assertIsNone(runner)
            self.assertIn("a conditional", why)
            self.assertIn("line 1", why)

    def test_a_multiline_step_without_a_test_command_is_not_a_test_step(self):
        with Env() as e:
            top = self.repo(e)
            self.assertEqual(testx.step_runner("set -e\nnpm ci\nnpm run build\n", top, top), (None, None))
            self.assertEqual(testx.step_runner("if true; then\n  make\nfi\n", top, top), (None, None))

    def test_one_line_steps_keep_their_recognition_and_say_nothing(self):
        with Env() as e:
            top = self.repo(e)
            self.assertEqual(testx.step_runner("pytest -q", top, top)[0]["name"], "pytest")
            self.assertEqual(testx.step_runner("npm ci && npm test", top, top), (None, None))

    # property: harmless lines placed anywhere around the test command, in any order, never change the runner
    def test_harmless_lines_in_any_order_leave_the_runner_of_the_command_alone(self):
        with Env() as e:
            top = self.repo(e)
            alone = testx.recognize("python -m unittest discover -s tests -v", top, top)
            harmless = ["", "# a comment", "set -e", "set -o pipefail", "set -eux", "echo 'step; one'",
                        "printf '%s\\n' x", "NAME=value", ":", "true", "  # indented comment"]
            rng = random.Random(1234)
            for _ in range(200):
                lines = rng.sample(harmless, rng.randint(0, len(harmless)))
                lines.insert(rng.randint(0, len(lines)), "python -m unittest discover -s tests -v")
                step = "\n".join(lines) + "\n"
                with self.subTest(step=step):
                    self.assertEqual(testx.step_runner(step, top, top), (alone, None))

    def test_the_observer_replaces_the_test_line_not_a_comment_or_echo_that_names_it(self):
        with Env() as e:
            work = Path(e.root) / "work"
            work.mkdir()
            step = ("# runs python -m unittest discover\necho 'python -m unittest'\n"
                    "python -m unittest discover -s tests\n")
            completion = testx.unittest_completion(work)
            observed = testx.observe_unittest_shell(step, work, completion)
            lines = observed.splitlines()
            self.assertEqual(lines[:2], ["# runs python -m unittest discover", "echo 'python -m unittest'"])
            self.assertNotIn("-m unittest", lines[2])
            self.assertIn(str(work / "observed_unittest.py"), lines[2])
            self.assertTrue(lines[2].endswith(" discover -s tests"))


CI = """name: CI
on:
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Unit
        run: |
          # the suite, from the app folder
          set -euo pipefail
          export CALC_MODE=checked
          cd app
          echo "unit tests"
          python -m unittest discover -s tests -v
"""
# the suite reads the exported value, so a run that loses it fails; it imports calc, so a fix of calc re-runs it
MODE_TEST = ("import os\nimport sys\nimport unittest\nfrom pathlib import Path\n\n"
             "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\nimport calc  # noqa: E402,F401\n\n\n"
             "class ModeTests(unittest.TestCase):\n"
             "    def test_the_step_exports_the_mode(self):\n"
             "        self.assertEqual(os.environ.get('CALC_MODE'), 'checked')\n")


class MultiLineResumeTests(unittest.TestCase):
    def commit(self, s, msg):
        git(s.repo, "add", "-A")
        git(s.repo, "commit", "-qm", msg)
        return s.head()

    def test_a_multiline_test_step_records_its_failures_and_resume_reruns_only_them(self):
        with G.Sandbox() as s:
            files = {"app/" + k: v for k, v in CALC.items()}
            files["app/calc.py"] = BROKEN_ADD
            files["app/tests/test_mode.py"] = MODE_TEST
            write(s.repo, files)
            s.write(".github/workflows/ci.yml", CI)
            s.branch("feat/calc")
            broken = self.commit(s, "feat: calc")
            self.assertEqual(G.dev("ci", "--run")[0], 1)
            unit = gitx.step_records(s.ctx())["test › Unit"]
            self.assertEqual((unit["runner"], unit["complete"], unit["failed_tests"]),
                             ("unittest", True, ["test_calc.CalcTests.test_add"]))
            edit(s.repo, {"app/calc.py": CALC["calc.py"]})
            self.commit(s, "fix: add adds")
            rc, out, err = G.dev("ci", "--run", "--resume")
            self.assertEqual(rc, 0, out + err)
            unit = {x["step"]: x for x in gitx.evidence(s.ctx())["steps"]}["Unit"]
            self.assertEqual((unit["how"], unit["tests"], unit["base_sha"]), ("partial", 3, broken))


if __name__ == "__main__":
    unittest.main()
