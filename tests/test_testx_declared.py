# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev test` runs the project's declared test command: a test_gate entry of its delivery policy, else the CI
workflow step `dev ci` reads, else its package.json test script, and only then what devkit infers from its files;
`dev test --json` says which (source: declared or inferred, declared_by: where). Hermetic: tiny git repositories in
temporary folders (test_testx.Env), the delivery policies in the test's folder, and test_testx's stand-in for pytest.
Run: python3 -m unittest tests.test_testx_declared -v"""
import json
import shutil
import sys
import unittest
import venv
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import delivery, testx  # noqa: E402
from test_testx import STANDIN_PYTEST, Env, dev  # noqa: E402

PROJECT = {
    "pytest.ini": "[pytest]\n",
    "pytest.py": STANDIN_PYTEST,
    "api.py": "def version():\n    return 1\n",
    "tests/test_api.py": "import sys\nfrom pathlib import Path\n\nsys.path.insert(0, str(Path(__file__).resolve()"
                         ".parents[1]))\nimport api  # noqa: E402\n\n\ndef test_version():\n"
                         "    assert api.version() == 1\n",
    ".gitignore": "__pycache__/\n",
}
WORKFLOW = """name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run the tests
        run: python -m pytest --tb=short
"""


class DeclaredCommandTests(unittest.TestCase):
    def run_all(self, files, policy=None):
        with Env() as e, mock.patch.object(delivery, "POLICIES", e.root / "policies"):
            if policy is not None:
                (e.root / "policies" / "acme").mkdir(parents=True)
                (e.root / "policies" / "acme" / "proj.json").write_text(json.dumps(policy))
            r = e.repo(files)
            rc, out, err = dev(r, "run", "--all", "--json")
            self.assertEqual(0, rc, out + err)
            return json.loads(out)["steps"][0]["runs"][0]

    # the CI workflow's test step decides the command: its options run, with the interpreter devkit found here in
    # place of the bare `python` the CI runner provides
    def test_the_ci_workflows_test_step_is_the_command(self):
        run = self.run_all({**PROJECT, ".github/workflows/ci.yml": WORKFLOW})
        self.assertEqual(("declared", "CI .github/workflows/ci.yml › test › Run the tests"),
                         (run["source"], run["declared_by"]))
        self.assertIn("--tb=short", run["argv"])
        self.assertNotEqual("python", run["argv"][0])

    # a bare `pytest` step runs the pytest of the CI's environment: here that is the project's own interpreter, with
    # the step's options, never whatever pytest comes first on PATH
    def test_a_bare_pytest_step_keeps_the_projects_interpreter(self):
        with Env() as e, mock.patch.object(delivery, "POLICIES", e.root / "policies"):
            r = e.repo({**PROJECT, ".github/workflows/ci.yml": WORKFLOW.replace("python -m pytest", "pytest"),
                        ".gitignore": "__pycache__/\n.venv/\n"})
            venv.create(r / ".venv", with_pip=False, symlinks=True)
            rc, out, err = dev(r, "run", "--all", "--json")
            run = json.loads(out)["steps"][0]["runs"][0]
            self.assertEqual((0, "declared"), (rc, run["source"]), out + err)
            self.assertEqual([str(r / ".venv" / "bin" / "python"), "-m", "pytest", "--tb=short"],
                             [str(Path(run["argv"][0]).parent.resolve() / Path(run["argv"][0]).name)] + run["argv"][1:4])

    # in a uv project without a .venv, a bare `pytest` step runs as devkit runs that project's pytest (uv run
    # pytest), with the step's options: never the interpreter devkit itself runs on
    def test_a_bare_pytest_step_in_a_uv_project_runs_through_uv(self):
        which = shutil.which
        with Env() as e, mock.patch.object(delivery, "POLICIES", e.root / "policies"), \
                mock.patch.object(testx.shutil, "which", lambda n, *a, **k: "/opt/uv" if n == "uv" else which(n, *a, **k)):
            r = e.repo({**PROJECT, "uv.lock": "version = 1\n",
                        ".github/workflows/ci.yml": WORKFLOW.replace("python -m pytest", "pytest")})
            pytest = next(x for x in testx.detect(str(r)) if x["name"] == "pytest")
            self.assertEqual("declared", pytest["source"])
            self.assertEqual(["uv", "run", "pytest", "--tb=short"], pytest["exec"][:4])

    # a `python -m pytest` step in the same uv project runs with the project's interpreter through uv, like the bare
    # pytest above, never with the interpreter devkit itself runs on
    def test_a_python_m_pytest_step_in_a_uv_project_runs_through_uv(self):
        which = shutil.which
        with Env() as e, mock.patch.object(delivery, "POLICIES", e.root / "policies"), \
                mock.patch.object(testx.shutil, "which", lambda n, *a, **k: "/opt/uv" if n == "uv" else which(n, *a, **k)):
            r = e.repo({**PROJECT, "uv.lock": "version = 1\n", ".github/workflows/ci.yml": WORKFLOW})
            pytest = next(x for x in testx.detect(str(r)) if x["name"] == "pytest")
            self.assertEqual("declared", pytest["source"])
            self.assertEqual(["uv", "run", "python", "-m", "pytest", "--tb=short"], pytest["exec"][:6])

    # a test entry recorded in the delivery policy comes before the CI step
    def test_the_delivery_policys_test_entry_comes_first(self):
        run = self.run_all({**PROJECT, ".github/workflows/ci.yml": WORKFLOW},
                           policy={"test_gate": ["python -m pytest --tb=line"]})
        self.assertEqual(("declared", "delivery policy test_gate"), (run["source"], run["declared_by"]))
        self.assertIn("--tb=line", run["argv"])
        self.assertNotIn("--tb=short", run["argv"])

    # with nothing declared, the command is inferred, and says so
    def test_without_a_declaration_the_command_is_inferred(self):
        run = self.run_all(PROJECT)
        self.assertEqual(("inferred", ""), (run["source"], run["declared_by"]))

    # a package.json test script declares the JS runner of its folder
    def test_a_package_test_script_declares_its_runner(self):
        with Env() as e, mock.patch.object(delivery, "POLICIES", e.root / "policies"):
            r = e.repo({"web/package.json": json.dumps({"scripts": {"test": "vitest run"},
                                                        "devDependencies": {"vitest": "^4"}}),
                        "web/src/a.test.ts": "import { it } from 'vitest';\n"})
            vitest = next(x for x in testx.detect(str(r)) if x["name"] == "vitest")
            self.assertEqual(("declared", "web/package.json test script"), (vitest["source"], vitest["declared_by"]))


if __name__ == "__main__":
    unittest.main()
