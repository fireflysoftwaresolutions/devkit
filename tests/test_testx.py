# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev test` (lib/devkit/testx.py) and `dev ci --run --resume`.

Hermetic: tiny git repositories in temp dirs, devkit state and the prove temp root redirected, no network. A real
unittest project runs; jest, vitest, maven, gradle, go and phpunit are checked through detection, the commands built
and sample report files, so those tools need not be installed.
Run: python3 -m unittest tests.test_testx -v
"""
import argparse
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _hermetic  # noqa: E402

_hermetic.activate()  # a probe that imports this module by path gets the suite's temporary home too

from devkit import core, gitx, testx  # noqa: E402
import test_gitx as G  # noqa: E402  (its Sandbox and dev() drive `dev ci`)

PY = sys.executable


def write(root, files):
    for rel, text in files.items():
        f = Path(root) / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(textwrap.dedent(text).lstrip("\n") if text else "")


def edit(root, files):
    """Change files after a run, the way an agent does seconds later. Python's bytecode cache is fresh while a source
    keeps its size and its modification time in whole seconds, so each edit moves the time whole seconds past the
    file's last one: two quick edits (0 then 1) would otherwise share a second and run the first edit's bytecode."""
    before = {rel: (Path(root) / rel).stat().st_mtime if (Path(root) / rel).exists() else 0 for rel in files}
    write(root, files)
    for rel in files:
        later = max(time.time(), before[rel] + 1) + 2
        os.utime(Path(root) / rel, (later, later))


def git(cwd, *args):
    rc, out, err = core.sh(["git", "-C", str(cwd), *args], timeout=60)
    if rc != 0:
        raise AssertionError(f"git {' '.join(args)}: {err}")
    return out


class Env:
    """devkit state and the prove temp root in a throwaway folder; git identity isolated."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.saved = core.STATE, testx.TMP_ROOT, testx.AUDIT_MODULE
        self.saved_git = {k: core.GIT_ENV.get(k) for k in G.ISOLATE}
        core.GIT_ENV.update(G.ISOLATE)
        core.STATE = self.root / "state"
        testx.TMP_ROOT = self.root / "devtmp"
        self.cwd = os.getcwd()
        return self

    def __exit__(self, *exc):
        os.chdir(self.cwd)
        core.STATE, testx.TMP_ROOT, testx.AUDIT_MODULE = self.saved
        for k, v in self.saved_git.items():
            if v is None:
                core.GIT_ENV.pop(k, None)
            else:
                core.GIT_ENV[k] = v
        self.tmp.cleanup()

    def repo(self, files, name="proj", branch="feat/change"):
        """A repository with `files` on main, pushed to a bare origin, and `branch` checked out."""
        r = self.root / name
        r.mkdir()
        write(r, files)
        origin = self.root / f"{name}-origin.git"
        url = f"https://github.com/acme/{name}.git"
        git(self.root, "init", "-q", "--bare", "-b", "main", str(origin))
        git(r, "init", "-q", "-b", "main")
        git(r, "remote", "add", "origin", url)
        git(r, "config", f"url.{origin}.insteadOf", url)
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", "chore: start")
        git(r, "push", "-q", "-u", "origin", "main")
        git(r, "remote", "set-head", "origin", "main")
        if branch:
            git(r, "switch", "-q", "-c", branch)
        return r


def _parser():
    ap = argparse.ArgumentParser(prog="dev")
    sp = ap.add_subparsers(dest="cmd")

    def add(name, fn, help_, **kw):
        p = sp.add_parser(name, help=help_, description=help_, **kw)
        p.set_defaults(fn=fn)
        return p

    testx.register(add)
    return ap


def dev(cwd, *argv):
    os.chdir(cwd)
    a = _parser().parse_args(["test", *[str(x) for x in argv]])
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = a.fn(a)
    return rc, out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------- conventions


class IsTestFileTests(unittest.TestCase):
    def test_conventions_of_every_stack(self):
        yes = ["tests/test_calc.py", "test_calc.py", "calc_test.py", "pkg/tests/helpers.py", "src/b.test.ts",
               "src/b.spec.tsx", "lib/x.test.js", "src/__tests__/a.js", "src/test/java/com/acme/FooTest.java",
               "src/test/java/com/acme/Support.java", "core/FooIT.java", "app/FooTest.kt",
               "pkg/calc/calc_test.go", "tests/Unit/MailerTest.php", "test/foo.js"]
        no = ["calc.py", "src/b.ts", "src/main/java/com/acme/Foo.java", "pkg/calc/calc.go", "src/Mailer.php",
              "README.md", "latest.py", "contest.py", "src/attestation.go", "docs/testing.md", "src/Testimony.php"]
        for p in yes:
            with self.subTest(p=p):
                self.assertTrue(testx.is_test_file(p))
        for p in no:
            with self.subTest(p=p):
                self.assertFalse(testx.is_test_file(p))


class ChangedFilesTests(unittest.TestCase):
    def test_branch_commits_staged_unstaged_untracked_and_deleted_but_not_ignored(self):
        with Env() as e:
            r = e.repo({"a.py": "A = 1\n", "b.py": "B = 1\n", "docs/x.md": "# x\n", ".gitignore": "*.log\n"})
            write(r, {"a.py": "A = 2\n"})
            git(r, "commit", "-qam", "feat: change a")
            write(r, {"c.py": "C = 1\n"})
            git(r, "add", "c.py")
            write(r, {"b.py": "B = 2\n", "d.py": "D = 1\n", "e.log": "noise\n"})
            (r / "docs" / "x.md").unlink()
            self.assertEqual(testx.changed_files(str(r)), ["a.py", "b.py", "c.py", "d.py", "docs/x.md"])
            self.assertEqual(testx.changed_files(str(r), since="HEAD"), ["b.py", "c.py", "d.py", "docs/x.md"])


# --------------------------------------------------------------------------- runner detection


class DetectTests(unittest.TestCase):
    def detect(self, files):
        with Env() as e:
            r = e.repo(files, branch=None)
            return {(x["name"], x["root"]): x for x in testx.detect(str(r))}

    def test_pytest_from_pyproject(self):
        found = self.detect({"pyproject.toml": "[tool.pytest.ini_options]\naddopts = '-q'\n",
                             "tests/test_a.py": "def test_a():\n    pass\n"})
        self.assertEqual(list(found), [("pytest", ".")])

    # a package without an environment of its own runs with the nearest one above it, up to the repository: here
    # backend/.venv for backend/sdk, not the repository's .venv and never devkit's own Python
    def test_a_package_runs_with_the_nearest_environment_above_it(self):
        with Env() as e:
            r = e.repo({"backend/sdk/pyproject.toml": "[tool.pytest.ini_options]\n",
                        "backend/sdk/tests/test_a.py": "def test_a():\n    pass\n", ".gitignore": ".venv/\n"},
                       branch=None)
            for venv in (r / "backend" / ".venv", r / ".venv"):
                (venv / "bin").mkdir(parents=True)
                (venv / "bin" / "python").write_text("")
            found = {(x["name"], x["root"]): x for x in testx.detect(str(r))}
            sdk = found[("pytest", "backend/sdk")]
            self.assertEqual((sdk["exec"], sdk["python"]),
                             ([str(r / "backend" / ".venv" / "bin" / "python"), "-m", "pytest"],
                              str(r / "backend" / ".venv" / "bin" / "python")))

    # a member of a uv workspace has no uv.lock of its own: the workspace's, above it, makes `uv run` its runner
    def test_a_uv_workspace_member_runs_with_uv_from_the_workspace_lock(self):
        with Env() as e:
            r = e.repo({"pyproject.toml": "[tool.uv.workspace]\nmembers = ['packages/*']\n", "uv.lock": "version = 1\n",
                        "packages/sdk/pyproject.toml": "[tool.pytest.ini_options]\n",
                        "packages/sdk/tests/test_a.py": "def test_a():\n    pass\n"}, branch=None)
            with mock.patch.object(testx.shutil, "which", lambda n: "/opt/bin/uv" if n == "uv" else None):
                found = {(x["name"], x["root"]): x for x in testx.detect(str(r))}
            self.assertEqual(found[("pytest", "packages/sdk")]["exec"], ["uv", "run", "pytest"])

    # the environment found has no pytest: the run is a runner problem that names that Python and how to fix it,
    # never a test failure of the selected files
    def test_a_python_without_pytest_is_reported_as_a_runner_problem(self):
        import venv
        with Env() as e:
            r = e.repo({"pyproject.toml": "[tool.pytest.ini_options]\n",
                        "tests/test_a.py": "def test_a():\n    assert True\n", ".gitignore": ".venv/\n"}, branch=None)
            venv.create(r / ".venv", with_pip=False, symlinks=True)
            runner = next(x for x in testx.detect(str(r)) if x["name"] == "pytest")
            res = testx.run_tests(str(r), runner, files=["tests/test_a.py"])
            self.assertTrue(res["crash"])
            self.assertEqual(len(res["failures"]), 1, res["failures"])
            self.assertIn(f"pytest is not installed for {r / '.venv' / 'bin' / 'python'}", res["failures"][0]["message"])
            self.assertIn("uv sync", res["failures"][0]["message"])

    def test_unittest_without_pytest_configuration(self):
        found = self.detect({"tests/test_a.py": "import unittest\n", "a.py": ""})
        self.assertEqual(list(found), [("unittest", ".")])
        self.assertEqual(found[("unittest", ".")]["start"], "tests")

    # a start folder with __init__.py is a package, which unittest imports (and so sets up) only from the root
    def test_unittest_start_folder_with_an_init_is_a_package(self):
        found = self.detect({"tests/__init__.py": "", "tests/test_a.py": "import unittest\n", "a.py": ""})
        self.assertTrue(found[("unittest", ".")]["package"])
        found = self.detect({"tests/test_a.py": "import unittest\n", "a.py": ""})
        self.assertFalse(found[("unittest", ".")]["package"])

    def test_jest_with_pnpm(self):
        found = self.detect({"package.json": json.dumps({"devDependencies": {"jest": "^29"}}),
                             "pnpm-lock.yaml": "", "src/a.test.ts": ""})
        self.assertEqual(found[("jest", ".")]["exec"], ["pnpm", "exec", "jest"])

    def test_monorepo_subprojects(self):
        found = self.detect({"web/package.json": json.dumps({"scripts": {"test": "vitest"}}), "web/yarn.lock": "",
                             "api/pom.xml": "<project/>", "api/mvnw": "#!/bin/sh\n",
                             "api/core/pom.xml": "<project/>",
                             "svc/build.gradle.kts": "", "svc/gradlew": "#!/bin/sh\n",
                             "tools/go.mod": "module example.com/tools\n",
                             "php/composer.json": json.dumps({"require-dev": {"phpunit/phpunit": "^10"}}),
                             "php/phpunit.xml": "<phpunit/>"})
        self.assertEqual(sorted(found), [("go", "tools"), ("gradle", "svc"), ("maven", "api"), ("phpunit", "php"),
                                         ("vitest", "web")])
        self.assertEqual(found[("vitest", "web")]["exec"], ["yarn", "vitest"])
        self.assertEqual(found[("maven", "api")]["exec"], ["./mvnw"])
        self.assertEqual(found[("gradle", "svc")]["exec"], ["./gradlew"])
        self.assertEqual(found[("go", "tools")]["module"], "example.com/tools")

    def test_fixture_projects_inside_tests_are_not_runners(self):
        found = self.detect({"package.json": json.dumps({"devDependencies": {"jest": "^29"}}),
                             "tests/fixtures/app/package.json": json.dumps({"devDependencies": {"vitest": "^2"}}),
                             "src/testdata/go.mod": "module x\n", "src/a.test.ts": ""})
        self.assertEqual(list(found), [("jest", ".")])

    def test_maven_without_wrapper_and_pest(self):
        found = self.detect({"pom.xml": "<project/>",
                             "composer.json": json.dumps({"require-dev": {"pestphp/pest": "^2"}})})
        self.assertEqual(found[("maven", ".")]["exec"], ["mvn"])
        self.assertEqual(found[("pest", ".")]["exec"], ["vendor/bin/pest"])

    # a package whose test script runs node (node --test, or node on its test scripts) has the node runner, whose
    # whole suite is what that script runs; without node on the PATH there is no runner to offer
    def test_a_node_test_script_is_the_node_runner_when_node_is_installed(self):
        files = {"package.json": json.dumps({"type": "module", "scripts": {"test": "node --test scripts/test/*.test.mjs"}}),
                 "mcp/package.json": json.dumps({"scripts": {"test": "node test/protocol.mjs && node test/flow.mjs"}}),
                 "web/package.json": json.dumps({"scripts": {"test": "jest"}, "devDependencies": {"jest": "^29"}}),
                 # a script that starts another test framework (mocha here) is no test node can run
                 "cli/package.json": json.dumps({"scripts": {"test": "node scripts/run-mocha.js"}})}
        with mock.patch.object(testx.shutil, "which", lambda n: "/opt/bin/node" if n == "node" else None):
            found = self.detect(files)
        self.assertEqual(sorted(found), [("jest", "web"), ("node", "."), ("node", "mcp")])
        self.assertEqual(found[("node", ".")]["suite"], ["scripts/test/*.test.mjs"])
        self.assertEqual(found[("node", "mcp")]["suite"], ["test/protocol.mjs", "test/flow.mjs"])
        with mock.patch.object(testx.shutil, "which", lambda n: None):
            self.assertEqual(sorted(self.detect(files)), [("jest", "web")])


# a package.json test script read word by word: what devkit runs must be what the script runs, or there is no runner
class NodeScriptTests(unittest.TestCase):
    def node_runner(self, scripts):
        """(exec, suite) of the node runner of a package with these package.json scripts, or None."""
        with Env() as e, mock.patch.object(testx.shutil, "which", lambda n: "/opt/bin/node" if n == "node" else None):
            r = e.repo({"package.json": json.dumps({"scripts": scripts})}, branch=None)
            rn = {(x["name"], x["root"]): x for x in testx.detect(str(r))}.get(("node", "."))
        return rn and (rn["exec"], rn["suite"])

    # node takes an option's value as the next word too: it stays with its option, never joins the suite
    def test_a_node_option_keeps_a_value_written_as_the_next_word(self):
        self.assertEqual(self.node_runner({"test": "node --test --test-coverage-include src -r ./setup.cjs "
                                                   "--import tsx test/"}),
                         (["node", "--test-coverage-include", "src", "-r", "./setup.cjs", "--import", "tsx", "--test"],
                          ["test/"]))

    # devkit sets its own reporters and never watches: those options go, with their values, in either spelling
    def test_node_reporter_and_watch_options_go_with_their_values(self):
        self.assertEqual(self.node_runner({"test": "node --test --test-reporter spec --test_reporter_destination "
                                                   "out.txt --watch-path src test/"}),
                         (["node", "--test"], ["test/"]))

    # an option devkit does not know may take the next word as its value, so the paths are unknown: no runner (the
    # tests are listed as not proven, never run wrongly); written with `=`, or a known switch, it is passed on
    def test_an_unknown_node_option_without_equals_means_no_runner(self):
        self.assertIsNone(self.node_runner({"test": "node --test --future-option value test/"}))
        self.assertEqual(self.node_runner({"test": "node --test --future-option=value --experimental-test-coverage "
                                                   "--no-warnings test/"}),
                         (["node", "--future-option=value", "--experimental-test-coverage", "--no-warnings", "--test"],
                          ["test/"]))

    # devkit runs node itself, so a step before the tests (a build, a node script that is no test) would be skipped
    # and the tests would run against stale output: no runner. A step after the tests is fine
    def test_a_step_before_the_node_tests_means_no_runner(self):
        self.assertIsNone(self.node_runner({"test": "tsc && node --test dist/test/"}))
        self.assertIsNone(self.node_runner({"test": "npm run build && node --test"}))
        self.assertIsNone(self.node_runner({"test": "node scripts/build.mjs && node --test dist/"}))
        self.assertEqual(self.node_runner({"test": "node --test test/ && eslint ."}), (["node", "--test"], ["test/"]))

    # a plain test script's node options (a loader such as tsx) are what lets it run: devkit passes them on, as it does
    # for `node --test`, and leaves out a watch or a reporter of its own
    def test_a_plain_test_scripts_node_options_are_passed_on(self):
        self.assertEqual(self.node_runner({"test": "node --import tsx --watch test/a.test.ts && node test/b.test.js"}),
                         (["node", "--import", "tsx", "--test"], ["test/a.test.ts", "test/b.test.js"]))

    # npm runs a pretest script before the test script; devkit would not
    def test_a_pretest_script_means_no_node_runner(self):
        self.assertIsNone(self.node_runner({"pretest": "tsc", "test": "node --test dist/test/"}))


class NodeScriptRedirectTests(unittest.TestCase):
    # a pipe or a redirect on the tests' command sends node's output elsewhere or feeds it input, which devkit cannot
    # reproduce when it runs node itself: no runner. The words after `|` or `>` used to become the suite, so node
    # skipped its own discovery. A quoted `|` is a pattern, not a pipe, and a pipe in a step after the tests changes
    # nothing they run
    def test_a_pipe_or_redirect_on_the_node_tests_means_no_runner(self):
        node_runner = NodeScriptTests.node_runner
        for script in ("node --test | tee out.txt", "node --test > out.txt", "node --test test/ 2>&1 | tap-spec",
                       "node --test < /dev/null", "node test/a.test.js | tap-spec"):
            with self.subTest(script):
                self.assertIsNone(node_runner(self, {"test": script}))
        self.assertEqual(node_runner(self, {"test": 'node --test --test-name-pattern="a|b" test/'}),
                         (["node", "--test-name-pattern=a|b", "--test"], ["test/"]))
        self.assertEqual(node_runner(self, {"test": "node --test test/ && eslint . | tee lint.txt"}),
                         (["node", "--test"], ["test/"]))


# --------------------------------------------------------------------------- blast radius

PY_PROJECT = {
    "pkg/__init__.py": "",
    "pkg/core.py": "VALUE = 1\n",
    "pkg/util.py": "from .core import VALUE\n\n\ndef double():\n    return VALUE * 2\n",
    "pkg/lazy.py": "def load():\n    from pkg import core\n    return core\n",
    "pkg/render.py": "TEMPLATE = 'body.md'\n",
    "pkg/orphan.py": "X = 1\n",
    "scripts/report.py": "print('report')\n",
    "templates/body.md": "# body\n",
    "tests/test_util.py": "import unittest\nfrom pkg.util import double\n",
    "tests/test_lazy.py": "import pkg.lazy\n",
    "tests/test_core_direct.py": "from pkg import core\n",
    "tests/test_report.py": "import subprocess\n",
    "tests/test_render.py": "from pkg import render\n",
    "tests/test_other.py": "import unittest\n",
    "README.md": "# proj\n",
    "docs/guide.md": "# guide\n",
    "requirements.txt": "",
    ".eslintrc.json": "{}\n",
    "config/settings.yaml": "a: 1\n",
}


class BlastRadiusTests(unittest.TestCase):
    def radius(self, files, changed):
        with Env() as e:
            r = e.repo(files, branch=None)
            return testx.blast_radius(str(r), changed)

    # A production module whose name looks like a test (app/test_probe.py, app/probe_test.py) is no test file where
    # pytest's configuration would never collect it: outside its testpaths, or named against its python_files. pytest
    # collects any file named on its command line, so selecting it ran its functions as tests (missing fixtures);
    # a change to it selects the tests that import it instead.
    def test_a_module_pytest_would_not_collect_is_no_test_file(self):
        app = {"app/__init__.py": "", "app/test_probe.py": "def test_db(session):\n    return session\n",
               "app/probe_test.py": "def test_cache(cache):\n    return cache\n",
               "tests/test_app.py": "from app import probe_test, test_probe\n\n\ndef test_probes():\n"
                                    "    assert test_probe.test_db(1) == probe_test.test_cache(1)\n"}
        both = ["app/test_probe.py", "app/probe_test.py"]
        for name, cfg, changed in (("pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', both),
                                   ("pyproject.toml", '[tool.pytest]\ntestpaths = [\n  "tests",  # unit\n]\n', both),
                                   ("pytest.ini", "[pytest]\ntestpaths = tests\n", both),
                                   ("setup.cfg", "[metadata]\nname = app\n\n[tool:pytest]\ntestpaths =\n    tests\n", both),
                                   ("tox.ini", "[pytest]\npython_files = test_*.py\n", ["app/probe_test.py"]),
                                   ("pyproject.toml", "[tool.pytest.ini_options]\npython_files = ['test_*.py']\n",
                                    ["app/probe_test.py"])):
            with self.subTest(config=name, text=cfg):
                res = self.radius({**app, name: cfg}, changed)
                self.assertEqual((res["mode"], res["tests"]), ("some", ["tests/test_app.py"]))
        # with no testpaths, or none that exists, pytest collects test_*.py anywhere: the module is a test file there
        for cfg in ("[pytest]\n", "[pytest]\ntestpaths = spec\n"):
            with self.subTest(text=cfg):
                res = self.radius({**app, "pytest.ini": cfg}, ["app/test_probe.py"])
                self.assertEqual(res["tests"], ["app/test_probe.py"])

    def test_python_source_selects_its_importers_transitively(self):
        res = self.radius(PY_PROJECT, ["pkg/core.py"])
        self.assertEqual(res["mode"], "some")
        self.assertEqual(res["tests"], ["tests/test_core_direct.py", "tests/test_lazy.py", "tests/test_util.py"])
        self.assertEqual(res["reasons"][0][0], "pkg/core.py")

    def test_global_file_means_all_and_names_the_file(self):
        for f in ("requirements.txt", "tests/conftest.py", ".github/workflows/ci.yml", "pyproject.toml"):
            with self.subTest(f=f):
                res = self.radius(PY_PROJECT, [f, "pkg/core.py"])
                self.assertEqual(res["mode"], "all")
                self.assertEqual(res["reasons"][0][0], f)

    def test_python_imports_resolve_like_python_not_by_bare_file_name(self):
        files = dict(PY_PROJECT, **{"pkg/types.py": "X = 1\n", "pkg/__main__.py": "print(1)\n",
                                    "tests/test_std.py": "import types\nimport __main__\n",
                                    "ns/mod.py": "X = 1\n", "tests/test_ns.py": "import ns.mod\n",
                                    "tools/helper.py": "X = 1\n", "tools/test_tool.py": "import helper\n"})
        self.assertNotIn("tests/test_std.py", self.radius(files, ["pkg/types.py"])["tests"])
        self.assertNotIn("tests/test_std.py", self.radius(files, ["pkg/__main__.py"])["tests"])
        self.assertEqual(self.radius(files, ["ns/mod.py"])["tests"], ["tests/test_ns.py"])  # namespace package
        self.assertEqual(self.radius(files, ["tools/helper.py"])["tests"], ["tools/test_tool.py"])  # sibling

    def test_packaging_names_inside_a_package_are_code(self):
        files = dict(PY_PROJECT, **{"pkg/setup.py": "X = 1\n", "tests/test_setup.py": "from pkg import setup\n"})
        res = self.radius(files, ["pkg/setup.py"])
        self.assertEqual((res["mode"], res["tests"]), ("some", ["tests/test_setup.py"]))
        self.assertEqual(self.radius(files, ["setup.py"])["mode"], "all")

    def test_docs_mean_none(self):
        res = self.radius(PY_PROJECT, ["README.md", "docs/guide.md"])
        self.assertEqual((res["mode"], res["tests"]), ("none", []))

    def test_a_doc_the_code_reads_selects_the_tests_of_that_code(self):
        res = self.radius(PY_PROJECT, ["templates/body.md"])
        self.assertEqual(res["tests"], ["tests/test_render.py"])

    def test_changed_test_file_selects_itself(self):
        res = self.radius(PY_PROJECT, ["tests/test_other.py"])
        self.assertEqual((res["mode"], res["tests"]), ("some", ["tests/test_other.py"]))

    # node --test runs every script of a test folder: a plain one (no .test or .spec) is a test file that selects
    # itself, not a helper that widens to the folder's other tests
    def test_a_plain_node_script_in_a_test_folder_selects_itself(self):
        files = {"package.json": json.dumps({"scripts": {"test": "node --test"}}),
                 "consulting/test/smoke.mjs": "import assert from 'node:assert';\nassert.equal(1, 1);\n",
                 "consulting/test/other.test.mjs": "import test from 'node:test';\ntest('x', () => {});\n"}
        with mock.patch.object(testx.shutil, "which", lambda n: "/opt/bin/node" if n == "node" else None):
            res = self.radius(files, ["consulting/test/smoke.mjs"])
        self.assertEqual((res["mode"], res["tests"]), ("some", ["consulting/test/smoke.mjs"]))

    def test_name_convention_fallback(self):
        res = self.radius(PY_PROJECT, ["scripts/report.py"])
        self.assertEqual(res["tests"], ["tests/test_report.py"])

    def test_unmapped_source_widens_to_its_package_never_to_nothing(self):
        files = dict(PY_PROJECT, **{"pkg/tests/test_pkg_local.py": "import unittest\n"})
        res = self.radius(files, ["pkg/orphan.py"])
        self.assertEqual(res["tests"], ["pkg/tests/test_pkg_local.py"])
        self.assertIn("widened", res["reasons"][0][1])
        res = self.radius(PY_PROJECT, ["pkg/orphan.py"])  # no test near it: the whole runner
        self.assertEqual(res["mode"], "all")

    def test_tooling_config_runs_no_tests_and_unknown_files_widen(self):
        res = self.radius(PY_PROJECT, [".eslintrc.json"])
        self.assertEqual((res["mode"], res["tests"]), ("some", []))
        res = self.radius(PY_PROJECT, ["config/settings.yaml"])
        self.assertEqual(res["mode"], "all")
        self.assertEqual(res["reasons"][0][0], "config/settings.yaml")

    def test_no_changes(self):
        self.assertEqual(self.radius(PY_PROJECT, [])["mode"], "none")

    def test_js_relative_index_require_and_alias_imports(self):
        files = {"package.json": json.dumps({"devDependencies": {"jest": "^29"}}), "pnpm-lock.yaml": "",
                 "tsconfig.json": '{\n  // comment\n  "compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}}\n}\n',
                 "src/a.ts": "export const a = 1;\n",
                 "src/b.ts": "import { a } from './a';\nexport const b = a + 1;\n",
                 "src/lib/index.ts": "export * from '../a';\n",
                 "src/c.js": "const lib = require('./lib');\n",
                 "src/b.test.ts": "import {\n  b,\n} from './b';\n",
                 "src/c.test.js": "const { c } = require('./c');\n",
                 "src/alias.test.ts": "import { a } from '@/a';\n",
                 "src/unrelated.test.ts": "import x from 'lodash';\n"}
        res = self.radius(files, ["src/a.ts"])
        self.assertEqual(res["tests"], ["src/alias.test.ts", "src/b.test.ts", "src/c.test.js"])

    def test_java_references_transitively_and_same_package_convention(self):
        files = {"pom.xml": "<project/>",
                 "src/main/java/com/acme/Foo.java": "package com.acme;\npublic class Foo {}\n",
                 "src/main/java/com/acme/Bar.java": "package com.acme;\npublic class Bar { Foo foo; }\n",
                 "src/test/java/com/acme/FooTest.java": "package com.acme;\nclass FooTest {}\n",
                 "src/test/java/com/acme/BarTest.java": "package com.acme;\nclass BarTest { Bar bar; }\n",
                 "src/test/java/com/acme/OtherTest.java": "package com.acme;\nclass OtherTest {}\n"}
        res = self.radius(files, ["src/main/java/com/acme/Foo.java"])
        self.assertEqual(res["tests"], ["src/test/java/com/acme/BarTest.java", "src/test/java/com/acme/FooTest.java"])

    def test_go_package_tests_and_importing_packages(self):
        files = {"go.mod": "module example.com/shop\n\ngo 1.22\n",
                 "internal/calc/calc.go": "package calc\n",
                 "internal/calc/calc_test.go": "package calc\n",
                 "cmd/app/main.go": 'package main\n\nimport (\n\t"fmt"\n\tc "example.com/shop/internal/calc"\n)\n',
                 "cmd/app/main_test.go": "package main\n",
                 "internal/other/other.go": "package other\n",
                 "internal/other/other_test.go": "package other\n"}
        res = self.radius(files, ["internal/calc/calc.go"])
        self.assertEqual(res["tests"], ["cmd/app/main_test.go", "internal/calc/calc_test.go"])

    def test_php_use_statements_and_class_names(self):
        files = {"composer.json": "{}", "phpunit.xml": "<phpunit/>",
                 "src/Mailer.php": "<?php\nnamespace App;\nclass Mailer {}\n",
                 "src/Newsletter.php": "<?php\nnamespace App;\nuse App\\Mailer;\nclass Newsletter {}\n",
                 "tests/MailerTest.php": "<?php\nuse App\\Mailer;\n",
                 "tests/NewsletterTest.php": "<?php\nuse App\\Newsletter;\n",
                 "tests/OtherTest.php": "<?php\n"}
        res = self.radius(files, ["src/Mailer.php"])
        self.assertEqual(res["tests"], ["tests/MailerTest.php", "tests/NewsletterTest.php"])


# --------------------------------------------------------------------------- commands


class CommandTests(unittest.TestCase):
    def runner(self, files, name, root="."):
        e = Env()
        e.__enter__()
        self.addCleanup(e.__exit__)
        r = e.repo(files, branch=None)
        found = {(x["name"], x["root"]): x for x in testx.detect(str(r))}
        return str(r), found[(name, root)]

    def test_pytest_files_and_node_ids(self):
        top, rn = self.runner({"pytest.ini": "[pytest]\n", "tests/test_a.py": ""}, "pytest")
        cmd = testx.build_command(top, rn, files=["tests/test_a.py"], report="R")
        self.assertEqual(cmd["argv"], [PY, "-m", "pytest", "-q", "--junitxml=R", "tests/test_a.py"])
        self.assertEqual(cmd["cwd"], top)
        cmd = testx.build_command(top, rn, ids=["tests/test_a.py::test_x"], report="R")
        self.assertEqual(cmd["argv"][-1], "tests/test_a.py::test_x")

    # A separately packaged Python project inside this one (a worker with its own pyproject and tests) has a runner of
    # its own. pytest run for the whole suite with no file arguments walks into it and collects its tests in the outer
    # process, where its test modules clash with the outer ones by name (import file mismatch) and its imports do not
    # resolve: the outer run leaves every nested project's folder out, and each nested runner runs its own.
    def test_the_whole_pytest_suite_leaves_out_nested_python_projects(self):
        top, rn = self.runner({"pyproject.toml": "[tool.pytest.ini_options]\naddopts = '-q'\n",
                               "tests/test_api.py": "def test_api():\n    pass\n",
                               "services/worker/pyproject.toml": "[tool.pytest.ini_options]\n",
                               "services/worker/tests/test_api.py": "def test_api():\n    pass\n",
                               "tools/lint/setup.py": "", "tools/lint/test_rules.py": "def test_rules():\n    pass\n",
                               "docs/test_snippets.py": "def test_snippet():\n    pass\n"}, "pytest")
        self.assertEqual(testx.build_command(top, rn, all_=True)["argv"],
                         [PY, "-m", "pytest", "-q", "--ignore=services/worker", "--ignore=tools/lint"])
        self.assertEqual(testx.build_command(top, rn, files=["tests/test_api.py"])["argv"],
                         [PY, "-m", "pytest", "-q", "tests/test_api.py"])
        worker = {(x["name"], x["root"]): x for x in testx.detect(top)}[("pytest", "services/worker")]
        self.assertEqual(testx.build_command(top, worker, all_=True)["argv"], [PY, "-m", "pytest", "-q"])

    def test_unittest_module_names_from_the_start_dir(self):
        top, rn = self.runner({"tests/test_calc.py": ""}, "unittest")
        cmd = testx.build_command(top, rn, files=["tests/test_calc.py"])
        self.assertEqual(cmd["argv"], [PY, "-m", "unittest", "-v", "test_calc"])
        self.assertEqual(cmd["env"]["PYTHONPATH"].split(os.pathsep)[0], str(Path(top) / "tests"))
        cmd = testx.build_command(top, rn, ids=["test_calc.CalcTests.test_add"])
        self.assertEqual(cmd["argv"][-1], "test_calc.CalcTests.test_add")

    # in a package start folder, modules and ids are dotted from the root, the suite runs with -t ., and an id
    # recorded by its bare name (before the folder became a package) gets the package's prefix
    def test_unittest_package_names_from_the_root(self):
        top, rn = self.runner({"tests/__init__.py": "", "tests/test_calc.py": ""}, "unittest")
        self.assertEqual(testx.build_command(top, rn, files=["tests/test_calc.py"])["argv"],
                         [PY, "-m", "unittest", "-v", "tests.test_calc"])
        ids = ["test_calc.CalcTests.test_add", "tests.test_calc.CalcTests.test_sub"]
        self.assertEqual(testx.build_command(top, rn, ids=ids)["argv"][-2:],
                         ["tests.test_calc.CalcTests.test_add", "tests.test_calc.CalcTests.test_sub"])
        self.assertEqual(testx.build_command(top, rn, all_=True)["argv"],
                         [PY, "-m", "unittest", "discover", "-v", "-s", "tests", "-t", "."])

    def test_jest_files_and_related(self):
        top, rn = self.runner({"package.json": json.dumps({"devDependencies": {"jest": "^29"}}),
                               "pnpm-lock.yaml": ""}, "jest")
        cmd = testx.build_command(top, rn, files=["src/a.test.ts"], report="R")
        self.assertEqual(cmd["argv"], ["pnpm", "exec", "jest", "--ci", "--json", "--outputFile=R", "src/a.test.ts"])
        cmd = testx.build_command(top, rn, related=["src/a.ts"], report="R")
        self.assertEqual(cmd["argv"][-2:], ["--findRelatedTests", "src/a.ts"])

    def test_vitest_files_and_related(self):
        top, rn = self.runner({"web/package.json": json.dumps({"devDependencies": {"vitest": "^2"}}),
                               "web/yarn.lock": ""}, "vitest", "web")
        cmd = testx.build_command(top, rn, files=["web/src/a.test.ts"], report="R")
        self.assertEqual(cmd["argv"], ["yarn", "vitest", "run", "--reporter=default", "--reporter=json",
                                       "--outputFile.json=R", "src/a.test.ts"])
        self.assertEqual(cmd["cwd"], str(Path(top) / "web"))
        cmd = testx.build_command(top, rn, related=["web/src/a.ts"], report="R")
        self.assertEqual(cmd["argv"][:4], ["yarn", "vitest", "related", "--run"])
        self.assertEqual(cmd["argv"][-1], "src/a.ts")

    # Playwright browser specs, in the testDir of a playwright.config, run with Playwright: vitest excludes them and
    # ran 0 tests, so a browser test could never be proven. The unit tests beside them keep vitest. Playwright writes
    # its JUnit report to the file its environment names; a recorded failure reruns by its title.
    def test_playwright_specs_run_with_playwright_and_unit_tests_with_vitest(self):
        top, pw = self.runner({"package.json": json.dumps({"scripts": {"test": "vitest"}, "devDependencies": {
                                   "vitest": "^5", "@playwright/test": "^1.64"}}),
                               "playwright.config.ts": "import { defineConfig } from '@playwright/test';\n"
                                                       "export default defineConfig({\n  testDir: './e2e',\n});\n",
                               "e2e/home.spec.ts": "import { test } from './fixtures';\n",
                               "src/sum.test.ts": "import { it } from 'vitest';\n"}, "playwright")
        runners = testx.detect(top)
        self.assertEqual({f: testx.owner(runners, f)["name"] for f in ("e2e/home.spec.ts", "src/sum.test.ts")},
                         {"e2e/home.spec.ts": "playwright", "src/sum.test.ts": "vitest"})
        cmd = testx.build_command(top, pw, files=["e2e/home.spec.ts"], report="R")
        self.assertEqual((cmd["argv"], cmd["env"]["PLAYWRIGHT_JUNIT_OUTPUT_FILE"], cmd["cwd"]),
                         (["npx", "--no-install", "playwright", "test", "--reporter=list,junit", "e2e/home.spec.ts"],
                          "R", top))
        cmd = testx.build_command(top, pw, ids=["e2e/home.spec.ts::home › opens (the) menu"])
        self.assertEqual(cmd["argv"][-3:], ["e2e/home.spec.ts", "--grep", r"opens \(the\) menu"])

    # a playwright.config without a testDir finds specs from its own folder: there Playwright runs the specs that use
    # it, and a unit test beside them stays vitest's
    def test_playwright_without_a_test_folder_runs_only_the_specs_that_use_it(self):
        top, pw = self.runner({"package.json": json.dumps({"devDependencies": {"vitest": "^5", "@playwright/test": "1"}}),
                               "playwright.config.js": "module.exports = { use: { headless: true } };\n",
                               "tests/home.spec.js": "const { test } = require('@playwright/test');\n",
                               "tests/sum.spec.js": "import { it } from 'vitest';\n"}, "playwright")
        runners = testx.detect(top)
        self.assertEqual({f: testx.owner(runners, f)["name"] for f in ("tests/home.spec.js", "tests/sum.spec.js")},
                         {"tests/home.spec.js": "playwright", "tests/sum.spec.js": "vitest"})

    # a testDir that is the config's own folder ('.' or './') is the same search as no testDir: only the specs that
    # use Playwright are its own, and the unit tests beside them stay vitest's
    def test_a_test_folder_that_is_the_configs_own_takes_only_the_specs_that_use_playwright(self):
        for test_dir in (".", "./"):
            with self.subTest(testDir=test_dir):
                top, pw = self.runner({
                    "web/package.json": json.dumps({"devDependencies": {"vitest": "^5", "@playwright/test": "1"}}),
                    "web/playwright.config.ts": f"export default {{ testDir: '{test_dir}' }};\n",
                    "web/e2e/home.spec.ts": "import { test } from '@playwright/test';\n",
                    "web/src/sum.test.ts": "import { it } from 'vitest';\n"}, "playwright", "web")
                runners = testx.detect(top)
                self.assertEqual({f: testx.owner(runners, f)["name"] for f in ("web/e2e/home.spec.ts",
                                                                              "web/src/sum.test.ts")},
                                 {"web/e2e/home.spec.ts": "playwright", "web/src/sum.test.ts": "vitest"})

    # a testDir in a comment is no setting: Playwright searches its own folder, and a unit test in the commented-out
    # folder stays vitest's
    def test_a_commented_out_test_folder_claims_nothing(self):
        for config in ("export default {\n  // testDir: './e2e',\n  use: { headless: true },\n};\n",
                       "export default {\n  /* testDir: './e2e',\n  retries: 2, */\n  use: {},\n};\n",
                       "/** testDir: './e2e' */\nexport default { use: { baseURL: 'http://localhost/*' } };\n"):
            with self.subTest(config=config):
                top, pw = self.runner({
                    "package.json": json.dumps({"devDependencies": {"vitest": "^5", "@playwright/test": "1"}}),
                    "playwright.config.ts": config,
                    "e2e/home.spec.ts": "import { test } from '@playwright/test';\n",
                    "e2e/menu.test.ts": "import { it } from 'vitest';\n"}, "playwright")
                runners = testx.detect(top)
                self.assertEqual(pw["claims"], [])
                self.assertEqual({f: testx.owner(runners, f)["name"] for f in ("e2e/home.spec.ts", "e2e/menu.test.ts")},
                                 {"e2e/home.spec.ts": "playwright", "e2e/menu.test.ts": "vitest"})

    # a spec takes `test` from a local fixtures module that extends Playwright's: it uses Playwright through that
    # module (also through a second one), while a unit test with a local helper of its own stays vitest's
    def test_a_spec_that_takes_test_from_a_local_fixtures_module_is_playwrights(self):
        top, pw = self.runner({
            "package.json": json.dumps({"devDependencies": {"vitest": "^5", "@playwright/test": "1"}}),
            "playwright.config.ts": "export default { use: { headless: true } };\n",
            "tests/fixtures.ts": "import { test as base } from '@playwright/test';\nexport const test = base;\n",
            "tests/more/fixtures/index.ts": "export { test } from '../../fixtures';\n",
            "tests/home.spec.ts": "import { test, expect } from './fixtures';\n",
            "tests/more/cart.spec.ts": "import { test } from './fixtures';\n",
            "tests/helpers.ts": "// not '@playwright/test'\nexport const two = 2;\n",
            "tests/sum.spec.ts": "import { it } from 'vitest';\nimport { two } from './helpers';\n"}, "playwright")
        runners = testx.detect(top)
        self.assertEqual({f: testx.owner(runners, f)["name"] for f in ("tests/home.spec.ts", "tests/more/cart.spec.ts",
                                                                      "tests/sum.spec.ts")},
                         {"tests/home.spec.ts": "playwright", "tests/more/cart.spec.ts": "playwright",
                          "tests/sum.spec.ts": "vitest"})

    # Angular unit tests built by the unit-test builder (Vitest runner) run through `ng test`, which builds them with
    # the workspace's TypeScript paths, setup and providers; bare vitest loses them. Each project runs its own specs:
    # those under its source root and those its include option names elsewhere.
    def test_angular_unit_test_builder_projects_run_through_ng_test(self):
        unit = {"builder": "@angular/build:unit-test"}
        top, _ = self.runner({"package.json": json.dumps({"devDependencies": {"vitest": "^5", "@angular/build": "22"}}),
                              "angular.json": json.dumps({"version": 1, "projects": {
                                  "shop": {"root": "projects/shop", "sourceRoot": "projects/shop/src",
                                           "architect": {"test": {**unit, "options": {
                                               "include": ["**/*.spec.ts", "../widgets/**/*.spec.ts"]}}}},
                                  "admin": {"root": "projects/admin", "architect": {"test": unit}},
                                  "legacy": {"root": "projects/legacy", "architect": {"test": {
                                      "builder": "@angular/build:unit-test", "options": {"runner": "karma"}}}}}}),
                              "projects/shop/src/cart.spec.ts": "", "projects/shop/widgets/tag.spec.ts": "",
                              "projects/admin/src/users.spec.ts": "", "projects/legacy/src/old.spec.ts": "",
                              "tools/lint.test.ts": ""}, "vitest")
        runners = testx.detect(top)
        shop = next(r for r in runners if r.get("project") == "shop")
        self.assertEqual({f: (testx.owner(runners, f)["name"], testx.owner(runners, f).get("project"))
                          for f in ("projects/shop/src/cart.spec.ts", "projects/shop/widgets/tag.spec.ts",
                                    "projects/admin/src/users.spec.ts", "projects/legacy/src/old.spec.ts",
                                    "tools/lint.test.ts")},
                         {"projects/shop/src/cart.spec.ts": ("ng", "shop"),
                          "projects/shop/widgets/tag.spec.ts": ("ng", "shop"),
                          "projects/admin/src/users.spec.ts": ("ng", "admin"),
                          "projects/legacy/src/old.spec.ts": ("vitest", None), "tools/lint.test.ts": ("vitest", None)})
        cmd = testx.build_command(top, shop, report="R",
                                  files=["projects/shop/src/cart.spec.ts", "projects/shop/widgets/tag.spec.ts"])
        self.assertEqual(cmd["argv"], ["npx", "--no-install", "ng", "test", "shop", "--watch=false",
                                       "--reporters=default", "--reporters=json", "--output-file=R",
                                       "--include", "projects/shop/src/cart.spec.ts",
                                       "--include", "projects/shop/widgets/tag.spec.ts"])
        self.assertEqual(testx.build_command(top, shop, all_=True, report="R")["argv"][-1], "--output-file=R")

    # an include pattern names files, not only a folder: a test under the folder that the pattern does not match is
    # not the project's (ng test would not run it) and stays with the package's runner
    def test_angular_include_patterns_match_file_names_not_only_folders(self):
        unit = {"builder": "@angular/build:unit-test"}
        top, _ = self.runner({"package.json": json.dumps({"devDependencies": {"vitest": "^5", "@angular/build": "22"}}),
                              "angular.json": json.dumps({"projects": {"shop": {
                                  "root": "shop", "sourceRoot": "shop/src", "architect": {"test": {**unit, "options": {
                                      "include": ["app/**/*.{spec,test}.ts", "../widgets/*.spec.ts"]}}}}}}),
                              "shop/src/app/cart.spec.ts": "", "shop/src/app/deep/cart.test.ts": "",
                              "shop/src/app/cart.bench.ts": "", "shop/src/main.spec.ts": "",
                              "shop/widgets/tag.spec.ts": "", "shop/widgets/tag.test.ts": "",
                              "shop/widgets/sub/tag.spec.ts": ""}, "ng", ".")
        runners = testx.detect(top)
        self.assertEqual({f: testx.owner(runners, f)["name"] for f in (
            "shop/src/app/cart.spec.ts", "shop/src/app/deep/cart.test.ts", "shop/src/main.spec.ts",
            "shop/widgets/tag.spec.ts", "shop/widgets/tag.test.ts", "shop/widgets/sub/tag.spec.ts")},
                         {"shop/src/app/cart.spec.ts": "ng", "shop/src/app/deep/cart.test.ts": "ng",
                          "shop/src/main.spec.ts": "vitest", "shop/widgets/tag.spec.ts": "ng",
                          "shop/widgets/tag.test.ts": "vitest", "shop/widgets/sub/tag.spec.ts": "vitest"})

    # the projects of one Angular workspace share its folder: a recorded failure reruns with its own file's project
    def test_a_recorded_angular_failure_reruns_with_its_own_project(self):
        unit = {"test": {"builder": "@angular/build:unit-test"}}
        top, _ = self.runner({"package.json": json.dumps({"devDependencies": {"@angular/build": "22"}}),
                              "angular.json": json.dumps({"projects": {"shop": {"root": "shop", "architect": unit},
                                                                       "admin": {"root": "admin", "architect": unit}}}),
                              "shop/src/cart.spec.ts": "", "admin/src/users.spec.ts": ""}, "ng", ".")
        failed = {"runner": "ng", "root": ".", "id": "admin/src/users.spec.ts::users lists them",
                  "file": "admin/src/users.spec.ts"}
        groups = testx._failed_groups(top, testx.detect(top), {"failures": {top: [failed]}})
        self.assertEqual([(r["project"], [f["id"] for f in fs]) for r, fs in groups], [("admin", [failed["id"]])])
        cmd = testx.build_command(top, groups[0][0], ids=groups[0][1])
        self.assertEqual(cmd["argv"][-4:], ["--include", "admin/src/users.spec.ts", "--filter", "users lists them"])

    def test_maven_units_and_integration_tests(self):
        top, rn = self.runner({"pom.xml": "<project/>", "mvnw": ""}, "maven")
        cmd = testx.build_command(top, rn, files=["src/test/java/com/acme/FooTest.java"])
        self.assertEqual(cmd["argv"], ["./mvnw", "-B", "test", "-Dtest=FooTest",
                                       "-Dsurefire.failIfNoSpecifiedTests=false"])
        cmd = testx.build_command(top, rn, files=["src/test/java/com/acme/FooTest.java",
                                                  "src/test/java/com/acme/BarIT.java"])
        self.assertEqual(cmd["argv"], ["./mvnw", "-B", "verify", "-Dtest=FooTest", "-Dit.test=BarIT",
                                       "-Dsurefire.failIfNoSpecifiedTests=false",
                                       "-Dfailsafe.failIfNoSpecifiedTests=false"])
        cmd = testx.build_command(top, rn, ids=["com.acme.FooTest#adds"])
        self.assertIn("-Dtest=com.acme.FooTest#adds", cmd["argv"])

    def test_gradle_tests_per_project(self):
        top, rn = self.runner({"settings.gradle.kts": "", "build.gradle.kts": "", "gradlew": "",
                               "api/build.gradle.kts": ""}, "gradle")
        cmd = testx.build_command(top, rn, files=["api/src/test/kotlin/com/acme/FooTest.kt",
                                                  "src/test/java/com/acme/BarTest.java"])
        self.assertEqual(cmd["argv"], ["./gradlew", ":api:test", "--tests", "com.acme.FooTest",
                                       ":test", "--tests", "com.acme.BarTest"])
        self.assertEqual(testx.build_command(top, rn, all_=True)["argv"], ["./gradlew", "test"])

    def test_go_packages_and_failed_tests(self):
        top, rn = self.runner({"go.mod": "module example.com/shop\n"}, "go")
        cmd = testx.build_command(top, rn, files=["internal/calc/calc_test.go", "internal/calc/more_test.go"])
        self.assertEqual(cmd["argv"], ["go", "test", "-json", "./internal/calc"])
        cmd = testx.build_command(top, rn, ids=["internal/calc:TestAdd", "cmd/app:TestMain"])
        self.assertEqual(cmd["argv"], ["go", "test", "-json", "-run", "^(TestAdd|TestMain)$", "./cmd/app",
                                       "./internal/calc"])
        self.assertEqual(testx.build_command(top, rn, all_=True)["argv"], ["go", "test", "-json", "./..."])

    # node --test writes its JUnit report next to the readable output; its whole suite is what the test script runs
    def test_node_files_and_its_own_suite(self):
        with mock.patch.object(testx.shutil, "which", lambda n: "/opt/bin/node" if n == "node" else None):
            top, rn = self.runner({"mcp/package.json": json.dumps({"scripts": {"test": "node --test test/"}})},
                                  "node", "mcp")
        cmd = testx.build_command(top, rn, files=["mcp/test/flow.mjs"], report="R")
        self.assertEqual(cmd["argv"], ["node", "--test", "--test-reporter=spec", "--test-reporter-destination=stdout",
                                       "--test-reporter=junit", "--test-reporter-destination=R", "test/flow.mjs"])
        self.assertEqual(cmd["cwd"], str(Path(top) / "mcp"))
        self.assertEqual(testx.build_command(top, rn, all_=True)["argv"], ["node", "--test", "test/"])

    def test_phpunit_one_file_or_a_filter(self):
        top, rn = self.runner({"composer.json": "{}", "phpunit.xml": "<phpunit/>"}, "phpunit")
        cmd = testx.build_command(top, rn, files=["tests/MailerTest.php"], report="R")
        self.assertEqual(cmd["argv"], ["vendor/bin/phpunit", "--log-junit", "R", "tests/MailerTest.php"])
        cmd = testx.build_command(top, rn, files=["tests/MailerTest.php", "tests/UserTest.php"], report="R")
        self.assertEqual(cmd["argv"], ["vendor/bin/phpunit", "--log-junit", "R", "--filter", "(MailerTest|UserTest)"])


# --------------------------------------------------------------------------- report parsing

PYTEST_XML = """<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" tests="3">
<testcase classname="tests.test_calc" name="test_add" time="0.001"><failure message="assert 3 == 4">def test_add():
&gt;       assert add(1, 2) == 4
E       assert 3 == 4

tests/test_calc.py:5: AssertionError</failure></testcase>
<testcase classname="tests.test_calc.TestOps" name="test_sub" time="0.002" />
<testcase classname="tests.test_calc" name="test_skip" time="0.000"><skipped message="later" /></testcase>
</testsuite></testsuites>
"""

SUREFIRE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.acme.FooTest" time="0.03" tests="2" failures="1" errors="0" skipped="0">
  <testcase name="adds" classname="com.acme.FooTest" time="0.01">
    <failure message="expected: &lt;3&gt; but was: &lt;4&gt;" type="org.opentest4j.AssertionFailedError">org.opentest4j.AssertionFailedError: expected: &lt;3&gt; but was: &lt;4&gt;
\tat com.acme.FooTest.adds(FooTest.java:12)
</failure>
  </testcase>
  <testcase name="subtracts" classname="com.acme.FooTest" time="0.02"/>
</testsuite>
"""

GRADLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.acme.FooTest" tests="1" failures="1">
  <testcase name="adds()" classname="com.acme.FooTest" time="0.01">
    <failure message="org.opentest4j.AssertionFailedError: expected: &lt;3&gt; but was: &lt;4&gt;" type="x">trace
\tat com.acme.FooTest.adds(FooTest.kt:9)</failure>
  </testcase>
</testsuite>
"""

PHPUNIT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites><testsuite name="Tests" tests="2" failures="1"><testsuite name="Tests\\MailerTest" file="ROOT/tests/MailerTest.php">
<testcase name="testSends" class="Tests\\MailerTest" classname="Tests.MailerTest" file="ROOT/tests/MailerTest.php" line="10" time="0.01"><failure type="PHPUnit\\Framework\\ExpectationFailedException">Tests\\MailerTest::testSends
Failed asserting that false is true.

ROOT/tests/MailerTest.php:12</failure></testcase>
<testcase name="testQueues" class="Tests\\MailerTest" classname="Tests.MailerTest" file="ROOT/tests/MailerTest.php" line="15" time="0.01"/>
</testsuite></testsuite></testsuites>
"""

GO_JSON = "\n".join(json.dumps(x) for x in [
    {"Action": "run", "Package": "example.com/shop/internal/calc", "Test": "TestAdd"},
    {"Action": "output", "Package": "example.com/shop/internal/calc", "Test": "TestAdd", "Output": "=== RUN   TestAdd\n"},
    {"Action": "output", "Package": "example.com/shop/internal/calc", "Test": "TestAdd",
     "Output": "    calc_test.go:9: got 3, want 4\n"},
    {"Action": "fail", "Package": "example.com/shop/internal/calc", "Test": "TestAdd", "Elapsed": 0.01},
    {"Action": "pass", "Package": "example.com/shop/internal/calc", "Test": "TestSub", "Elapsed": 0},
    {"Action": "fail", "Package": "example.com/shop/internal/calc", "Elapsed": 0.02},
    {"Action": "output", "Package": "example.com/shop/cmd/app", "Output": "cmd/app/main.go:4:2: undefined: nope\n"},
    {"Action": "fail", "Package": "example.com/shop/cmd/app", "Elapsed": 0.01},
])

JEST_JSON = {"numTotalTests": 2, "testResults": [{
    "name": "ROOT/src/b.test.ts", "startTime": 1000, "endTime": 1250, "assertionResults": [
        {"ancestorTitles": ["b"], "title": "adds", "fullName": "b adds", "status": "failed", "duration": 5,
         "failureMessages": ["Error: expect(received).toBe(expected)\n\nExpected: 4\nReceived: 3\n"
                             "    at Object.<anonymous> (ROOT/src/b.test.ts:5:15)"]},
        {"ancestorTitles": ["b"], "title": "keeps", "fullName": "b keeps", "status": "passed", "duration": 1,
         "failureMessages": []}]}]}

UNITTEST_312 = """test_add (test_calc.CalcTests.test_add) ... FAIL
test_sub (test_calc.CalcTests.test_sub) ... ok
test_broken (unittest.loader._FailedTest.test_broken) ... ERROR

======================================================================
ERROR: test_broken (unittest.loader._FailedTest.test_broken)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_broken
Traceback (most recent call last):
  File "/usr/lib/python3.12/unittest/loader.py", line 394, in _find_test_path
    module = self._get_module_from_name(name)
  File "ROOT/tests/test_broken.py", line 1, in <module>
    import nothing_here
ModuleNotFoundError: No module named 'nothing_here'


======================================================================
FAIL: test_add (test_calc.CalcTests.test_add)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "ROOT/tests/test_calc.py", line 9, in test_add
    self.assertEqual(calc.add(1, 2), 3)
AssertionError: -1 != 3

----------------------------------------------------------------------
Ran 3 tests in 0.001s

FAILED (failures=1, errors=1)
"""

UNITTEST_39 = """test_add (test_calc.CalcTests) ... FAIL
test_sub (test_calc.CalcTests) ... ok

======================================================================
FAIL: test_add (test_calc.CalcTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "ROOT/tests/test_calc.py", line 9, in test_add
    self.assertEqual(calc.add(1, 2), 3)
AssertionError: -1 != 3

----------------------------------------------------------------------
Ran 2 tests in 0.001s

FAILED (failures=1)
"""

# a package run asked for a deleted test module and a renamed test (python 3.11+ names the loader's stand-in test
# with its name; 3.9 does not)
UNITTEST_NOT_LOADED = """test_gone_away (unittest.loader._FailedTest.test_gone_away) ... ERROR
test_renamed (unittest.loader._FailedTest.test_renamed) ... ERROR

======================================================================
ERROR: test_gone_away (unittest.loader._FailedTest.test_gone_away)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_gone_away
Traceback (most recent call last):
  File "/usr/lib/python3.12/unittest/loader.py", line 137, in loadTestsFromName
    module = __import__(module_name)
ModuleNotFoundError: No module named 'tests.test_gone_away'


======================================================================
ERROR: test_renamed (unittest.loader._FailedTest.test_renamed)
----------------------------------------------------------------------
AttributeError: type object 'CalcTests' has no attribute 'test_renamed'

----------------------------------------------------------------------
Ran 2 tests in 0.001s

FAILED (errors=2)
"""
UNITTEST_NOT_LOADED_39 = UNITTEST_NOT_LOADED.replace("_FailedTest.test_gone_away)", "_FailedTest)").replace(
    "_FailedTest.test_renamed)", "_FailedTest)")


# node --test --test-reporter=junit (node 26): a node:test file with a describe block, a plain script that exits 1
NODE_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
\t<testcase name="adds" time="0.000371" classname="test" file="ROOT/test/a.test.mjs"/>
\t<testcase name="breaks" time="0.000445" classname="test" file="ROOT/test/a.test.mjs" failure="Expected values to be strictly equal:&#10;&#10;2 !== 3&#10;">
\t\t<failure type="testCodeFailure" message="Expected values to be strictly equal:&#10;&#10;2 !== 3">
[Error [ERR_TEST_FAILURE]: Expected values to be strictly equal:

2 !== 3
] {
  code: 'ERR_TEST_FAILURE',
  cause: AssertionError [ERR_ASSERTION]: Expected values to be strictly equal:
      at TestContext.&lt;anonymous> (file://ROOT/test/a.test.mjs:4:31)
      at Test.runInAsyncScope (node:async_hooks:226:14)
}
\t\t</failure>
\t</testcase>
\t<testsuite name="group" time="0.000781" disabled="0" errors="0" tests="2" failures="1" skipped="0">
\t\t<testcase name="inner" time="0.000602" classname="group" file="ROOT/test/a.test.mjs"/>
\t\t<testcase name="slug" time="0.000500" classname="group" file="ROOT/test/a.test.mjs">
\t\t\t<failure type="testCodeFailure" message="Expected values to be strictly equal:&#10;+ actual - expected&#10;&#10;+ ' big news '&#10;- 'big-news'">
      at TestContext.&lt;anonymous> (file://ROOT/test/a.test.mjs:9:20)
\t\t\t</failure>
\t\t</testcase>
\t</testsuite>
\t<testcase name="test/bad.mjs" time="0.112125" classname="test" file="ROOT/test/bad.mjs" failure="test failed">
\t\t<failure type="testCodeFailure" message="test failed">
[Error: test failed] { code: 'ERR_TEST_FAILURE', failureType: 'testCodeFailure', cause: 'test failed', exitCode: 1 }
\t\t</failure>
\t</testcase>
\t<testcase name="test/plain.mjs" time="0.110449" classname="test" file="ROOT/test/plain.mjs"/>
\t<!-- tests 5 -->
</testsuites>
"""


def failures(cases):
    return [(c["id"], c["file"], c["line"], c["message"]) for c in cases if c["status"] == "failed"]


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.top = Path(self.tmp.name).resolve()
        write(self.top, {"tests/test_calc.py": "", "src/test/java/com/acme/FooTest.java": "",
                         "src/test/kotlin/com/acme/FooTest.kt": "", "tests/MailerTest.php": "",
                         "src/b.test.ts": "", "internal/calc/calc_test.go": ""})

    def tearDown(self):
        self.tmp.cleanup()

    def test_pytest_junit(self):
        cases = testx.parse_junit(PYTEST_XML, str(self.top), str(self.top), "pytest")
        self.assertEqual(failures(cases), [("tests/test_calc.py::test_add", "tests/test_calc.py", 5, "assert 3 == 4")])
        self.assertEqual({c["id"]: c["status"] for c in cases},
                         {"tests/test_calc.py::test_add": "failed", "tests/test_calc.py::TestOps::test_sub": "passed",
                          "tests/test_calc.py::test_skip": "skipped"})

    # a module pytest could not import is one case with no class, named by its dotted module (pytest 9 writes
    # classname="" name="tests.test_calc"): it belongs to its file, and its message is the error, not
    # "collection failure"
    def test_a_pytest_collection_error_belongs_to_its_file_with_its_error(self):
        xml = ('<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests"><testsuite name="pytest" '
               'errors="1" failures="0" skipped="0" tests="1"><testcase classname="" name="tests.test_calc" '
               'time="0.000"><error message="collection failure">ImportError while importing test module '
               f"'{self.top}/tests/test_calc.py'.\nHint: make sure your test modules/packages have valid Python "
               "names.\nTraceback:\ntests/test_calc.py:1: in &lt;module&gt;\n    import newmod\n"
               "E   ModuleNotFoundError: No module named 'newmod'</error></testcase></testsuite></testsuites>")
        cases = testx.parse_junit(xml, str(self.top), str(self.top), "pytest")
        self.assertEqual(failures(cases), [("tests/test_calc.py", "tests/test_calc.py", 1,
                                            "ModuleNotFoundError: No module named 'newmod'")])

    def test_surefire_and_gradle_junit(self):
        cases = testx.parse_junit(SUREFIRE_XML, str(self.top), str(self.top), "maven")
        self.assertEqual(failures(cases), [("com.acme.FooTest#adds", "src/test/java/com/acme/FooTest.java", 12,
                                            "expected: <3> but was: <4>")])
        self.assertEqual(len(cases), 2)
        cases = testx.parse_junit(GRADLE_XML, str(self.top), str(self.top), "gradle")
        self.assertEqual(failures(cases), [("com.acme.FooTest.adds", "src/test/kotlin/com/acme/FooTest.kt", 9,
                                            "org.opentest4j.AssertionFailedError: expected: <3> but was: <4>")])

    def test_phpunit_junit(self):
        cases = testx.parse_junit(PHPUNIT_XML.replace("ROOT", str(self.top)), str(self.top), str(self.top), "phpunit")
        self.assertEqual(failures(cases), [("Tests\\MailerTest::testSends", "tests/MailerTest.php", 12,
                                            "Failed asserting that false is true.")])
        self.assertEqual(len(cases), 2)

    def test_go_json(self):
        rn = {"name": "go", "root": ".", "module": "example.com/shop"}
        cases = testx.parse_go_json(GO_JSON, str(self.top), rn)
        self.assertEqual(failures(cases), [("cmd/app", "cmd/app/main.go", 4, "cmd/app/main.go:4:2: undefined: nope"),
                                           ("internal/calc:TestAdd", "internal/calc/calc_test.go", 9,
                                            "got 3, want 4")])
        self.assertEqual(len(cases), 3)

    # node --test: a node:test case by its title (in its describe blocks), a plain script as one case named by its path
    def test_node_test_junit(self):
        cases = testx.parse_node_junit(NODE_XML.replace("ROOT", str(self.top)), str(self.top), str(self.top))
        self.assertEqual(failures(cases), [
            ("test/a.test.mjs::breaks", "test/a.test.mjs", 4, "Expected values to be strictly equal: 2 !== 3"),
            ("test/a.test.mjs::group > slug", "test/a.test.mjs", 9,
             "Expected values to be strictly equal: + ' big news ' - 'big-news'"),
            ("test/bad.mjs", "test/bad.mjs", None, "the script exited 1")])
        self.assertEqual({c["id"]: c["status"] for c in cases},
                         {"test/a.test.mjs::adds": "passed", "test/a.test.mjs::breaks": "failed",
                          "test/a.test.mjs::group > inner": "passed", "test/a.test.mjs::group > slug": "failed",
                          "test/bad.mjs": "failed", "test/plain.mjs": "passed"})

    def test_jest_and_vitest_json(self):
        text = json.dumps(JEST_JSON).replace("ROOT", str(self.top))
        cases = testx.parse_jest_json(text, str(self.top))
        self.assertEqual(failures(cases), [("src/b.test.ts::b adds", "src/b.test.ts", 5,
                                            "Error: expect(received).toBe(expected)")])
        self.assertEqual(len(cases), 2)

    # Playwright's JUnit report, as Playwright 1.64 writes it: a case's classname is its file relative to the testDir,
    # its name the describe titles and the test's title joined by ' › '; the failure's stack names the file and line
    def test_playwright_junit(self):
        write(self.top, {"web/e2e/home.spec.ts": ""})
        e2e = self.top / "web" / "e2e"
        xml = f"""<testsuites id="" name="" tests="3" failures="1" skipped="1" errors="0" time="1.25">
<testsuite name="home.spec.ts" timestamp="2026-11-23T06:02:43.273Z" hostname="" tests="3" failures="1" skipped="0">
<testcase name="home › adds numbers" classname="home.spec.ts" time="0.013">
</testcase>
<testcase name="home › fails on purpose" classname="home.spec.ts" time="0.002">
<failure message="expect(received).toBe(expected) // Object.is equality" type="expect.toBe">
<![CDATA[  home.spec.ts:4:3 › home › fails on purpose ───────────
    Error: expect(received).toBe(expected) // Object.is equality

    Expected: 2
    Received: 1
        at {e2e}/home.spec.ts:4:46
]]>
</failure>
</testcase>
<testcase name="top level" classname="home.spec.ts" time="0"><skipped/></testcase>
</testsuite>
</testsuites>"""
        rn = {"name": "playwright", "root": "web", "claims": ["web/e2e"]}
        cases = testx.parse_playwright_junit(xml, str(self.top), str(self.top / "web"), rn)
        self.assertEqual([(c["id"], c["file"], c["status"], c["name"]) for c in cases], [
            ("web/e2e/home.spec.ts::home › adds numbers", "web/e2e/home.spec.ts", "passed", "home › adds numbers"),
            ("web/e2e/home.spec.ts::home › fails on purpose", "web/e2e/home.spec.ts", "failed",
             "home › fails on purpose"),
            ("web/e2e/home.spec.ts::top level", "web/e2e/home.spec.ts", "skipped", "top level")])
        self.assertEqual(failures(cases), [("web/e2e/home.spec.ts::home › fails on purpose", "web/e2e/home.spec.ts", 4,
                                            "expect(received).toBe(expected) // Object.is equality")])

    def test_unittest_output_new_and_old_formats(self):
        rn = {"name": "unittest", "root": ".", "start": "tests"}
        cases = testx.parse_unittest(UNITTEST_312.replace("ROOT", str(self.top)), str(self.top), rn)
        self.assertEqual(failures(cases), [
            ("test_broken", "tests/test_broken.py", 1, "ModuleNotFoundError: No module named 'nothing_here'"),
            ("test_calc.CalcTests.test_add", "tests/test_calc.py", 9, "AssertionError: -1 != 3")])
        self.assertIn(("test_calc.CalcTests.test_sub", "passed"), [(c["id"], c["status"]) for c in cases])
        cases = testx.parse_unittest(UNITTEST_39.replace("ROOT", str(self.top)), str(self.top), rn)
        self.assertEqual(failures(cases), [("test_calc.CalcTests.test_add", "tests/test_calc.py", 9,
                                            "AssertionError: -1 != 3")])
        self.assertEqual(len(cases), 2)

    # A test's own stderr lands between its name and its result (devkit reads stdout and stderr as one stream): a
    # line written before the result moves the result to a line of its own, one without a newline glues it to the
    # result ("no newlineok"), and text that looks like a result ("two ... ok") comes before the real one. The
    # result is the last thing written for the test, so each test still has its own. As Python 3.15 and 3.9 print it.
    def test_unittest_results_after_a_tests_own_stderr_are_read(self):
        rn = {"name": "unittest", "root": ".", "start": "tests"}
        write(self.top, {"tests/test_noise.py": ""})
        new = ("test_before (test_noise.T.test_before) ... warming up\nok\ntest_doc (test_noise.T.test_doc)\n"
               "A docstring. ... doc noise\nok\ntest_lines (test_noise.T.test_lines) ... one\ntwo ... ok\nFAIL\n"
               "test_partial (test_noise.T.test_partial) ... no newlineok\n"
               "test_skip (test_noise.T.test_skip) ... about to skip\nskipped 'not here'\n\n" + "=" * 70 + "\n"
               "FAIL: test_lines (test_noise.T.test_lines)\n" + "-" * 70 + "\nTraceback (most recent call last):\n"
               f'  File "{self.top}/tests/test_noise.py", line 16, in test_lines\n    self.assertEqual(1, 2)\n'
               "AssertionError: 1 != 2\n\n" + "-" * 70 + "\nRan 5 tests in 0.001s\n\nFAILED (failures=1, skipped=1)\n")
        old = re.sub(r" \(test_noise\.T\.\w+\)", " (test_noise.T)", new)
        want = {"test_noise.T.test_before": "passed", "test_noise.T.test_doc": "passed",
                "test_noise.T.test_lines": "failed", "test_noise.T.test_partial": "passed",
                "test_noise.T.test_skip": "skipped"}
        for label, text in (("3.12+", new), ("3.9", old)):
            with self.subTest(label):
                cases = testx.parse_unittest(text, str(self.top), rn)
                self.assertEqual({c["id"]: c["status"] for c in cases}, want)
                self.assertEqual(failures(cases), [("test_noise.T.test_lines", "tests/test_noise.py", 16,
                                                    "AssertionError: 1 != 2")])

    # Python 3.9 prints no result for a test whose subtests failed, so the next test starts on its line: each keeps
    # its own result (the expected failure was read as the first test's, and the second test was lost)
    def test_a_unittest_line_holding_two_tests_gives_each_its_result(self):
        rn = {"name": "unittest", "root": ".", "start": "tests"}
        write(self.top, {"tests/test_noise.py": ""})
        text = ("test_fine (test_noise.S) ... ok\ntest_subs (test_noise.S) ... test_xf (test_noise.S) ... expected "
                "failure\n\n" + "=" * 70 + "\nFAIL: test_subs (test_noise.S) (i=1)\n" + "-" * 70 + "\nTraceback (most "
                f'recent call last):\n  File "{self.top}/tests/test_noise.py", line 8, in test_subs\n'
                "    self.assertNotEqual(i, 1)\nAssertionError: 1 == 1\n\n" + "-" * 70 + "\nRan 3 tests in 0.001s\n\n"
                "FAILED (failures=1, expected failures=1)\n")
        cases = testx.parse_unittest(text, str(self.top), rn)
        self.assertEqual({c["id"]: c["status"] for c in cases}, {
            "test_noise.S.test_fine": "passed", "test_noise.S.test_subs": "failed",
            "test_noise.S.test_xf": "expected_failure"})

    # a package run names its tests from the root (tests.test_calc.…): each maps to its file, passed ones included
    def test_unittest_output_of_a_package_run(self):
        rn = {"name": "unittest", "root": ".", "start": "tests", "package": True}
        text = UNITTEST_312.replace("ROOT", str(self.top)).replace("(test_calc.", "(tests.test_calc.")
        cases = {c["id"]: c for c in testx.parse_unittest(text, str(self.top), rn)}
        self.assertEqual((cases["tests.test_calc.CalcTests.test_add"]["status"],
                          cases["tests.test_calc.CalcTests.test_add"]["line"]), ("failed", 9))
        self.assertEqual({i: c["file"] for i, c in cases.items() if i.startswith("tests.")},
                         {"tests.test_calc.CalcTests.test_add": "tests/test_calc.py",
                          "tests.test_calc.CalcTests.test_sub": "tests/test_calc.py"})

    # unittest names a test it could not load by its last part only (test_renamed); the result keeps the name that
    # was asked for, so a rerun selects the same test and not a module called test_renamed
    def test_a_test_that_could_not_be_loaded_keeps_the_name_it_was_selected_by(self):
        rn = {"name": "unittest", "root": ".", "start": "tests", "package": True}
        asked = ["tests.test_gone_away", "tests.test_calc.CalcTests.test_renamed"]
        for fmt, text in (("3.11+", UNITTEST_NOT_LOADED), ("3.9", UNITTEST_NOT_LOADED_39)):
            with self.subTest(fmt):
                cases = testx.parse_unittest(text, str(self.top), rn, names=asked)
                self.assertEqual([(c["id"], c["status"]) for c in cases],
                                 [("tests.test_calc.CalcTests.test_renamed", "failed"),
                                  ("tests.test_gone_away", "failed")])

    # with a docstring, unittest -v prints the test's name on one line and its first docstring line with the result
    def test_a_result_printed_after_a_docstring_line_is_parsed(self):
        rn = {"name": "unittest", "root": ".", "start": "tests", "package": True}
        for fmt, head in (("3.11+", "test_sub (tests.test_calc.CalcTests.test_sub)"),
                          ("3.9", "test_sub (tests.test_calc.CalcTests)")):
            with self.subTest(fmt):
                text = f"{head}\nSubtracts two numbers. ... ok\n\n{'-' * 70}\nRan 1 test in 0.001s\n\nOK\n"
                cases = testx.parse_unittest(text, str(self.top), rn)
                self.assertEqual([(c["id"], c["status"], c["file"]) for c in cases],
                                 [("tests.test_calc.CalcTests.test_sub", "passed", "tests/test_calc.py")])


class NodeJunitWithoutFileTests(unittest.TestCase):
    # Node before 24 (22 is an LTS) writes no file attribute on a JUnit case; devkit runs node once per test file,
    # so a case without one belongs to the file node ran
    NODE22 = ('<?xml version="1.0" encoding="utf-8"?>\n<testsuites>\n'
              '\t<testcase name="adds" time="0.000240" classname="test"/>\n\t<!-- tests 1 -->\n</testsuites>\n')

    def test_a_case_without_a_file_belongs_to_the_one_file_node_ran(self):
        with tempfile.TemporaryDirectory() as d:
            top = os.path.realpath(d)
            cases = testx.parse_node_junit(self.NODE22, top, top, os.path.join(top, "test", "add.test.mjs"))
            self.assertEqual([(c["id"], c["file"], c["status"]) for c in cases],
                             [("test/add.test.mjs::adds", "test/add.test.mjs", "passed")])

    def test_a_node_command_for_one_file_names_it_and_for_several_names_none(self):
        with tempfile.TemporaryDirectory() as d:
            top = os.path.realpath(d)
            runner = {"name": "node", "exec": ["node", "--test"], "root": ".", "suite": []}
            one = testx.build_command(top, runner, ["test/a.test.mjs"], report="r.xml")
            two = testx.build_command(top, runner, ["test/a.test.mjs", "test/b.test.mjs"], report="r.xml")
            self.assertEqual((one["only"], two["only"]), (os.path.join(top, "test", "a.test.mjs"), ""))


class JunitPrologTests(unittest.TestCase):
    """Both JUnit report readers and a document type declaration. A report never declares one, and its entities would
    expand while the report is read (external entities, billion laughs)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.top = str(Path(self.tmp.name).resolve())

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, text):
        """(the JUnit reader's cases, the node:test JUnit reader's cases), each as [(name, status)]."""
        return ([(c["name"], c["status"]) for c in testx.parse_junit(text, self.top, self.top, "pytest")],
                [(c["name"], c["status"]) for c in testx.parse_node_junit(text, self.top, self.top)])

    # the readers looked for a declaration in the first 4,096 characters only, so one after a longer comment expanded
    # (a 5,386-character report gave a 1,000,004-character case name); the whole prolog decides now, a byte order
    # mark, the XML declaration, processing instructions and comments included
    def test_a_doctype_anywhere_in_the_prolog_refuses_the_report(self):
        report = '<testsuites><testcase classname="tests.test_a" name="{}" file="test/a.test.mjs"/></testsuites>'
        doctype = '<!DOCTYPE testsuites [<!ENTITY boom "expanded">]>'
        for prolog in ('<?xml version="1.0"?><!--' + "c" * 5000 + "-->",
                       '\ufeff<?xml version="1.0"?>\n<?xml-stylesheet href="r.xsl"?>\n<!-- one -->\n<!-- two -->\n'):
            with self.subTest(prolog=prolog[:24]):
                self.assertEqual(self.read(prolog + doctype + report.format("&boom;")), ([], []))
                self.assertEqual(self.read(prolog + report.format("plain")),
                                 ([("plain", "passed")], [("plain", "passed")]))

    # a failure that quotes an HTML page (a web test's response body) carries `<!DOCTYPE html>` in its text, after the
    # prolog: the report is read
    def test_a_failure_that_quotes_an_html_page_is_read(self):
        report = ('<?xml version="1.0"?><testsuites><testcase classname="tests.test_page" name="renders" '
                  'file="test/page.test.mjs"><failure message="unexpected page"><![CDATA[<!DOCTYPE html><html>'
                  '<body>Not found</body></html>]]></failure></testcase></testsuites>')
        self.assertEqual(self.read(report), ([("renders", "failed")], [("renders", "failed")]))


# --------------------------------------------------------------------------- running a real unittest project

CALC = {
    "calc.py": "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n",
    "tests/test_calc.py": """
        import sys
        import unittest
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import calc  # noqa: E402


        class CalcTests(unittest.TestCase):
            def test_add(self):
                self.assertEqual(calc.add(1, 2), 3)

            def test_sub(self):
                self.assertEqual(calc.sub(3, 1), 2)
        """,
    "tests/test_other.py": """
        import unittest


        class OtherTests(unittest.TestCase):
            def test_truth(self):
                self.assertTrue(True)
        """,
    "README.md": "# calc\n",
    ".gitignore": "__pycache__/\n",
}
BROKEN_ADD = "def add(a, b):\n    return a - b\n\n\ndef sub(a, b):\n    return a - b\n"
# CALC as a package suite: tests/__init__.py sets the run up, and a test checks that it ran first
PKG = dict(CALC, **{
    "tests/__init__.py": "import os\n\nos.environ['CALC_SUITE_SETUP'] = 'ran'\n",
    "tests/test_setup.py": """
        import os
        import unittest


        class SetupTests(unittest.TestCase):
            def test_the_package_setup_ran_first(self):
                self.assertEqual(os.environ.get("CALC_SUITE_SETUP"), "ran")
        """,
})


def state(e):
    files = list((e.root / "state" / "tests").glob("*.json"))
    return json.loads(files[0].read_text()) if files else {}


def recorded(e):
    return sorted(f["id"] for fs in (state(e).get("failures") or {}).values() for f in fs)


class RunTests(unittest.TestCase):
    # a node option whose value is the next word reaches node whole, so the suite runs (node used to exit 9 with
    # "requires an argument", and dev test reported zero tests). The script names its test file: Node 22 reads each
    # argument after --test as a file or a glob and fails on a folder ("Cannot find module …/test/"), while Node 20
    # and 26 accept a folder; --test-name-pattern exists since Node 18.11
    @unittest.skipUnless(shutil.which("node"), "node is not installed, so the node runner cannot run here")
    def test_a_node_suite_with_an_option_value_as_the_next_word_runs(self):
        with Env() as e:
            r = e.repo({"package.json": json.dumps({"type": "module", "scripts": {
                            "test": "node --test --test-name-pattern adds test/add.test.mjs"}}),
                        "src/add.mjs": "export const add = (a, b) => a + b;\n",
                        "test/add.test.mjs": 'import assert from "node:assert/strict";\nimport { test } from '
                                             '"node:test";\nimport { add } from "../src/add.mjs";\n\n'
                                             'test("adds", () => assert.equal(add(1, 2), 3));\n'})
            rc, out, err = dev(r, "run", "--all", "--json")
            run = json.loads(out)["steps"][0]["runs"][0]
            self.assertEqual((rc, run["count"], run["failed"]), (0, 1, 0), out + err)

    # a suite that guards devkit's state folder (an audit hook refusing writes below it, as devkit's own hermetic test
    # guard does) still gets a complete run: the runner's completion receipt, written by the test process after the
    # last test, used to land below that folder, so a green suite was reported failed and incomplete
    def test_a_suite_that_guards_devkits_state_folder_still_completes(self):
        with Env() as e:
            guard = ("import sys\n\nGUARDED = %r\n\n\ndef _guard(event, args):\n"
                     "    if event == 'open' and isinstance(args[0], str) and isinstance(args[1], str) \\\n"
                     "            and any(c in args[1] for c in 'wax+') and args[0].startswith(GUARDED):\n"
                     "        raise PermissionError('suite guard: write below devkit state refused: ' + args[0])\n\n\n"
                     "sys.addaudithook(_guard)\n") % (str(core.STATE) + os.sep)
            r = e.repo(dict(CALC, **{"tests/__init__.py": guard}))
            rc, out, err = dev(r, "run", "--all", "--json")
            run = json.loads(out)["steps"][0]["runs"][0]
            self.assertEqual((rc, run["count"], run["failed"]), (0, 3, 0), out + err)

    def test_failure_is_compact_with_file_line_and_recorded(self):
        with Env() as e:
            r = e.repo(CALC)
            write(r, {"calc.py": BROKEN_ADD})
            rc, out, err = dev(r, "run")
            self.assertEqual(rc, 1, out + err)
            text = out + err
            self.assertIn("tests/test_calc.py:11", text)
            self.assertIn("AssertionError: -1 != 3", text)
            self.assertNotIn("Traceback", text)
            self.assertEqual(recorded(e), ["test_calc.CalcTests.test_add"])
            self.assertTrue(str(e.root / "state") in json.dumps(state(e)) or state(e))

    def test_affected_selection_runs_only_the_importers(self):
        with Env() as e:
            r = e.repo(CALC)
            write(r, {"calc.py": BROKEN_ADD})
            rc, out, _ = dev(r, "run", "--affected", "--json")
            data = json.loads(out)
            run = data["steps"][0]["runs"][0]
            self.assertEqual((rc, data["steps"][0]["kind"]), (1, "affected"))
            self.assertEqual((run["files"], run["count"], run["failed"]), (["tests/test_calc.py"], 2, 1))

    def test_default_runs_last_failures_first_then_affected(self):
        with Env() as e:
            r = e.repo(CALC)
            write(r, {"calc.py": BROKEN_ADD})
            dev(r, "run")
            edit(r, {"calc.py": CALC["calc.py"] + "\n\ndef mul(a, b):\n    return a * b\n"})  # fixed, still changed
            rc, out, _ = dev(r, "--json")
            data = json.loads(out)
            self.assertEqual(rc, 0)
            self.assertEqual([s["kind"] for s in data["steps"]], ["failed", "affected"])
            self.assertEqual(data["steps"][0]["runs"][0]["ids"], ["test_calc.CalcTests.test_add"])
            self.assertEqual(data["steps"][0]["runs"][0]["count"], 1)
            self.assertEqual(data["steps"][1]["runs"][0]["files"], ["tests/test_calc.py"])
            self.assertEqual(recorded(e), [])

    def test_default_stops_when_a_last_failure_still_fails(self):
        with Env() as e:
            r = e.repo(CALC)
            write(r, {"calc.py": BROKEN_ADD})
            dev(r, "run")
            rc, out, _ = dev(r, "--json")
            data = json.loads(out)
            self.assertEqual((rc, [s["kind"] for s in data["steps"]]), (1, ["failed"]))

    def test_run_failed_and_explicit_paths(self):
        with Env() as e:
            r = e.repo(CALC)
            rc, out, _ = dev(r, "run", "--failed")
            self.assertEqual(rc, 0)
            self.assertIn("no recorded failures", out)
            rc, out, _ = dev(r, "run", "tests/test_other.py", "--json")
            run = json.loads(out)["steps"][0]["runs"][0]
            self.assertEqual((rc, run["files"], run["count"]), (0, ["tests/test_other.py"], 1))

    def test_all_runs_everything_and_records_the_full_duration(self):
        with Env() as e:
            r = e.repo(CALC)
            rc, out, _ = dev(r, "run", "--all", "--json")
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out)["steps"][0]["runs"][0]["count"], 3)
            full = state(e)["full"]
            self.assertEqual(list(full), ["unittest:."])
            self.assertGreater(full["unittest:."]["secs"], 0)
            self.assertEqual(full["unittest:."]["count"], 3)

    def test_global_change_does_not_start_a_full_run_silently(self):
        with Env() as e:
            r = e.repo(CALC)
            dev(r, "run", "--all")
            write(r, {"requirements.txt": "requests\n",
                      "tests/test_other.py": CALC["tests/test_other.py"] + "# touched\n"})
            rc, out, err = dev(r)
            self.assertEqual(rc, 0, out + err)
            self.assertIn("requirements.txt", out)
            self.assertIn("dev test --all", out)
            self.assertIn("dev ci --run", out)
            self.assertIn("last full run", out)
            rc, out, _ = dev(r, "--json")
            data = json.loads(out)
            self.assertEqual(data["radius"]["mode"], "all")
            self.assertEqual([(s["kind"], s["runs"][0]["files"]) for s in data["steps"]],
                             [("affected", ["tests/test_other.py"])])

    def test_the_full_suite_estimate_uses_recorded_durations_before_any_full_run(self):
        with Env() as e:
            r = e.repo(CALC)
            dev(r, "run", "tests/test_calc.py", "tests/test_other.py")
            edit(r, {"requirements.txt": "requests\n", "tests/test_other.py": CALC["tests/test_other.py"] + "#\n"})
            rc, out, _ = dev(r)
            self.assertEqual(rc, 0)
            self.assertIn("from recorded durations", out)

    def test_a_selection_of_every_test_runs_as_the_full_suite_and_is_timed(self):
        with Env() as e:
            r = e.repo(CALC)
            edit(r, {"calc.py": CALC["calc.py"] + "# touched\n",
                     "tests/test_other.py": CALC["tests/test_other.py"] + "# touched\n"})
            rc, out, _ = dev(r, "--json")
            run = json.loads(out)["steps"][0]["runs"][0]
            self.assertEqual((rc, run["all"], run["count"]), (0, True, 3))
            self.assertEqual(state(e)["full"]["unittest:."]["count"], 3)

    def test_zero_tests_run_is_a_selection_problem_not_a_pass(self):
        with Env() as e:
            r = e.repo(dict(CALC, **{"tests/test_empty.py": "import unittest\n"}))
            rc, out, err = dev(r, "run", "tests/test_empty.py")
            self.assertEqual(rc, 2, out + err)
            self.assertIn("0 tests", err)
            self.assertIn("test_empty", err)

    def test_docs_only_change_runs_nothing(self):
        with Env() as e:
            r = e.repo(CALC)
            write(r, {"README.md": "# calc, documented\n"})
            rc, out, _ = dev(r)
            self.assertEqual(rc, 0)
            self.assertIn("nothing to run", out)

    # a suite whose tests/__init__.py sets the run up (as devkit's own does) runs as a package, so that setup runs
    # for one file and for the whole suite, and its failures are recorded by their dotted names
    def test_a_package_suite_runs_its_init_first(self):
        with Env() as e:
            r = e.repo(PKG)
            rc, out, err = dev(r, "run", "tests/test_setup.py")
            self.assertEqual(rc, 0, out + err)
            rc, out, err = dev(r, "run", "--all")
            self.assertEqual(rc, 0, out + err)
            self.assertEqual(state(e)["full"]["unittest:."]["count"], 4)
            edit(r, {"calc.py": BROKEN_ADD})
            rc, out, err = dev(r, "run", "tests/test_calc.py", "tests/test_setup.py")
            self.assertEqual(rc, 1, out + err)
            self.assertIn("tests/test_calc.py:11", out + err)
            self.assertEqual(recorded(e), ["tests.test_calc.CalcTests.test_add"])

    # each run writes its own log, so a run in another lane or worktree of the project cannot overwrite it
    def test_each_run_keeps_its_own_log(self):
        with Env() as e:
            r = e.repo(PKG)
            write(r, {"calc.py": BROKEN_ADD})
            logs = []
            for _ in range(2):
                rc, out, err = dev(r, "run", "tests/test_calc.py", "--json")
                self.assertEqual(rc, 1, out + err)
                logs.append(json.loads(out)["steps"][0]["runs"][0]["log"])
            self.assertNotEqual(logs[0], logs[1])
            for log in logs:
                self.assertIn("AssertionError: -1 != 3", Path(log).read_text())


class OutsidePathTests(unittest.TestCase):
    # `dev test run` runs this repository's tests: a path outside it, relative, absolute, or a symlink out of it, is
    # refused before anything runs (`dev test run ../outside.test.js` built `node --test ../outside.test.js`)
    def test_a_test_file_outside_the_repository_is_refused(self):
        with Env() as e, mock.patch.object(testx.shutil, "which", lambda n: "/opt/bin/node" if n == "node" else None):
            r = e.repo({"package.json": json.dumps({"scripts": {"test": "node --test"}}),
                        "test/in.test.js": "require('node:test')('in', () => {});\n"})
            outside = e.root / "outside.test.js"
            outside.write_text("require('node:fs').writeFileSync(__filename + '.ran', '');\n")
            (r / "test" / "link.test.js").symlink_to(outside)
            for path in ("../outside.test.js", str(outside), "test/link.test.js"):
                with self.subTest(path):
                    rc, out, err = dev(r, "run", path)
                    self.assertEqual((rc, f"no test runner for: {path}" in err), (2, True), out + err)
            self.assertFalse(Path(f"{outside}.ran").exists(), "the file outside the repository ran")


def seed(r, *fails):
    """The record an earlier run left: failures as (id, file)."""
    p = testx.state_path(r)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"failures": {str(r): [
        {"runner": "unittest", "root": ".", "id": i, "file": f, "line": None, "message": "", "ts": 1}
        for i, f in fails]}}))


class StaleFailureTests(unittest.TestCase):
    def test_nested_empty_summary_keeps_unexecuted_recorded_failures(self):
        with Env() as e:
            body = ("import os\nimport sys\nimport unittest\nclass T(unittest.TestCase):\n"
                    "    def test_a_exit(self):\n        self.fail('first failure')\n"
                    "    def test_b_pending(self):\n        self.fail('pending failure')\n")
            r = e.repo({"tests/__init__.py": "", "tests/test_exit.py": body})
            rc, out, err = dev(r, "run", "--all", "--json")
            self.assertEqual(rc, 1, out + err)
            pending = recorded(e)
            self.assertEqual(len(pending), 2)
            edit(r, {"tests/test_exit.py": body.replace("self.fail('first failure')",
                    "unittest.TextTestRunner(stream=sys.stderr).run(unittest.TestSuite([]))\n        os._exit(0)")})
            rc, out, err = dev(r, "run", "--failed", "--json")
            run = json.loads(out)["steps"][0]["runs"][0]
            self.assertEqual(rc, 1, out + err)
            self.assertEqual((run["complete"], run["zero"], run["crash"]), (False, False, True), out + err)
            self.assertEqual(recorded(e), pending)

    # a failure recorded by its bare name (an older run) and one whose test was renamed are rerun first; neither
    # selects a test any more, so both leave the record and the output names them, while a real failure stays
    def test_recorded_failures_that_no_longer_select_a_test_are_forgotten_and_named(self):
        with Env() as e:
            r = e.repo(PKG)
            write(r, {"calc.py": BROKEN_ADD})
            seed(r, ("test_gone_away", ""), ("tests.test_calc.CalcTests.test_renamed", "tests/test_calc.py"),
                 ("tests.test_calc.CalcTests.test_add", "tests/test_calc.py"))
            rc, out, err = dev(r)
            self.assertEqual(rc, 1, out + err)
            self.assertEqual(recorded(e), ["tests.test_calc.CalcTests.test_add"])
            self.assertIn("forgotten", err)
            self.assertIn("test_gone_away", err)
            self.assertIn("tests.test_calc.CalcTests.test_renamed", err)

    # a failure whose test file was deleted cannot run again: it leaves the record with the same line, and dev test
    # goes on to the tests the changes affect
    def test_a_failure_of_a_deleted_test_file_is_forgotten_and_named(self):
        with Env() as e:
            r = e.repo(PKG)
            edit(r, {"calc.py": CALC["calc.py"] + "\n\ndef mul(a, b):\n    return a * b\n"})
            seed(r, ("tests.test_old.OldTests.test_gone", "tests/test_old.py"))
            rc, out, err = dev(r, "--json")
            self.assertEqual(rc, 0, out + err)
            self.assertEqual(recorded(e), [])
            self.assertIn("no longer select a test (renamed or deleted): forgotten: tests.test_old.OldTests.test_gone",
                          err)

    # when the stale failure was the only one, dev test goes on to the tests the changes affect
    def test_a_stale_failure_alone_does_not_stop_the_affected_tests(self):
        with Env() as e:
            r = e.repo(PKG)
            edit(r, {"calc.py": CALC["calc.py"] + "\n\ndef mul(a, b):\n    return a * b\n"})
            seed(r, ("test_gone_away", ""))
            rc, out, err = dev(r, "--json")
            self.assertEqual(rc, 0, out + err)
            self.assertEqual([s["kind"] for s in json.loads(out)["steps"]], ["failed", "affected"])
            self.assertEqual(recorded(e), [])

    # A run that ends with a non-zero exit after its tests and no failure to name (the interpreter exited mid-run)
    # is shown as `unittest:.`, the runner and its folder. No runner can load that name: recorded, it was rerun first
    # on every `dev test`, crashed the same way, and the affected tests never ran. It is shown and never recorded.
    def test_a_run_that_exits_without_a_failure_to_name_records_nothing_to_rerun(self):
        with Env() as e:
            r = e.repo(PKG)
            write(r, {"tests/test_exit.py": "import os\nimport unittest\n\n\nclass ExitTests(unittest.TestCase):\n"
                                            "    def test_exits(self):\n        os._exit(3)\n"})
            rc, out, err = dev(r, "run", "--all", "--json")
            run = json.loads(out)["steps"][0]["runs"][0]
            self.assertEqual((rc, run["crash"], [f["id"] for f in run["failures"]]), (1, True, ["unittest:."]),
                             out + err)
            self.assertEqual(recorded(e), [])

    # `unittest:.`, recorded by an older devkit, names no test: it is forgotten and named, and the affected tests run
    def test_a_recorded_runner_label_is_forgotten_and_the_affected_tests_run(self):
        with Env() as e:
            r = e.repo(PKG)
            edit(r, {"calc.py": CALC["calc.py"] + "\n\ndef mul(a, b):\n    return a * b\n"})
            seed(r, ("unittest:.", ""))
            rc, out, err = dev(r, "--json")
            self.assertEqual(rc, 0, out + err)
            self.assertEqual([s["kind"] for s in json.loads(out)["steps"]], ["affected"])
            self.assertIn("unittest:.", err)
            self.assertEqual(recorded(e), [])

    # a test module whose own import fails is a real failure: it stays recorded under the name it was run by
    def test_a_test_module_that_fails_to_import_stays_recorded(self):
        with Env() as e:
            r = e.repo(PKG)
            edit(r, {"tests/test_calc.py": "import nothing_here\n" + textwrap.dedent(PKG["tests/test_calc.py"])})
            seed(r, ("tests.test_calc.CalcTests.test_add", "tests/test_calc.py"))
            rc, out, err = dev(r, "run", "--failed")
            self.assertEqual(rc, 1, out + err)
            self.assertIn("nothing_here", out + err)
            self.assertEqual(recorded(e), ["tests.test_calc.CalcTests.test_add"])


def result(fails, files):
    """One run's results as run_tests returns them: `fails` as (id, file), `files` the test files it ran."""
    cases = [testx._case(i, f, 3, "failed", "AssertionError: 1 != 2") for i, f in fails]
    return {"runner": "unittest", "root": ".", "files": list(files), "ids": [], "related": [], "all": False,
            "cases": cases, "failures": cases, "zero": False, "crash": False, "complete": True,
            "secs": 0.1, "count": len(cases),
            "failed": len(cases)}


class OutcomeTests(unittest.TestCase):
    def test_unittest_counts_anonymous_setup_skips_without_inventing_case_ids(self):
        with Env() as e:
            runner = {"name": "unittest", "root": ".", "start": "tests"}
            for setup, anonymous in (("skipped 'dependency unavailable'\n", 1),
                                     ("setUpClass (test_calc.T) ... skipped 'dependency unavailable'\n", 0)):
                with self.subTest(setup=setup):
                    output = setup + ("test_value (test_calc.TExtra) ... ok\n\n" + "-" * 70 +
                                      "\nRan 1 test in 0.001s\n\nOK (skipped=1)\n")
                    cases = testx.parse_unittest(output, str(e.root), runner)
                    self.assertEqual(testx._unattributed_unittest_skips(output, cases), anonymous)
                    self.assertEqual([c["id"] for c in cases], ["test_calc.TExtra.test_value"] if anonymous else
                                     ["test_calc.T", "test_calc.TExtra.test_value"])

    def test_summary_reports_actual_passes_and_skips(self):
        for extra, passed in (("", 0), ("    def test_pass(self):\n        self.assertEqual(calc.value(), 1)\n", 1)):
            with self.subTest(passed=passed), Env() as e:
                r = e.repo({"calc.py": "def value():\n    return 1\n",
                            "tests/test_calc.py": "import unittest\nimport calc\nclass T(unittest.TestCase):\n"
                            "    @unittest.skip('not available here')\n    def test_skip(self):\n"
                            "        self.assertEqual(calc.value(), 2)\n" + extra})
                rc, out, err = dev(r, "run", "--all")
                self.assertEqual(rc, 0, out + err)
                self.assertIn(f"{passed} test(s) passed", out)
                self.assertIn("1 skipped", out)
                if not passed:
                    self.assertIn("no passing tests", out)

    def test_json_and_records_keep_all_outcome_counts(self):
        with Env() as e:
            r = e.repo({"calc.py": "def value():\n    return 1\n",
                        "tests/test_calc.py": "import unittest\nimport calc\nclass T(unittest.TestCase):\n"
                        "    def test_pass(self):\n        self.assertEqual(calc.value(), 1)\n"
                        "    @unittest.skip('not available here')\n    def test_skip(self):\n"
                        "        self.assertEqual(calc.value(), 2)\n"
                        "    @unittest.expectedFailure\n    def test_expected(self):\n"
                        "        self.assertEqual(calc.value(), 2)\n"})
            rc, out, err = dev(r, "run", "--all", "--json")
            self.assertEqual(rc, 0, out + err)
            run = json.loads(out)["steps"][0]["runs"][0]
            saved = state(e)
            for result in (run, saved["last"], saved["full"]["unittest:."]):
                self.assertEqual({k: result.get(k) for k in ("count", "passed", "failed", "skipped", "expected_failure")},
                                 {"count": 3, "passed": 1, "failed": 0, "skipped": 1, "expected_failure": 1})
            rc, out, err = dev(r, "run", "--all")
            self.assertEqual(rc, 0, out + err)
            self.assertIn("1 test(s) passed", out)
            self.assertIn("1 skipped", out)
            self.assertIn("1 expected failure", out)

    def test_skip_or_expected_failure_does_not_clear_an_unresolved_failure(self):
        source = ("import unittest\nimport calc\nclass T(unittest.TestCase):\n"
                  "    def test_value(self):\n        self.assertEqual(calc.value(), 2)\n")
        for decorator in ("unittest.skip('not available here')", "unittest.expectedFailure"):
            for selection in (("--all",), ("tests/test_calc.py",), ("--failed",)):
                with self.subTest(decorator=decorator, selection=selection), Env() as e:
                    r = e.repo({"calc.py": "def value():\n    return 1\n", "tests/test_calc.py": source})
                    rc, out, err = dev(r, "run", "--all")
                    self.assertEqual(rc, 1, out + err)
                    edit(r, {"tests/test_calc.py": source.replace("    def test_value", f"    @{decorator}\n    def test_value")})
                    rc, out, err = dev(r, "run", *selection)
                    self.assertEqual(rc, 0, out + err)
                    self.assertEqual(recorded(e), ["test_calc.T.test_value"])
                    edit(r, {"calc.py": "def value():\n    return 2\n", "tests/test_calc.py": source})
                    rc, out, err = dev(r, "run", "--failed")
                    self.assertEqual(rc, 0, out + err)
                    self.assertEqual(recorded(e), [])

    def test_expected_failure_cannot_prove_an_assertion_that_fails_both_ways(self):
        with Env() as e:
            r = e.repo({"calc.py": "def value():\n    return 0\n", "tests/__init__.py": ""})
            write(r, {"calc.py": "def value():\n    return 1\n",
                      "tests/test_calc.py": "import unittest\nimport calc\nclass T(unittest.TestCase):\n"
                      "    @unittest.expectedFailure if calc.value() == 1 else lambda f: f\n"
                      "    def test_value(self):\n        self.assertEqual(calc.value(), 2)\n"})
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            self.assertEqual(rc, 2, out + err)
            rows = json.loads(out)["tests"]
            self.assertEqual([(t["with_change"], t["without_change"], t["verdict"]) for t in rows],
                             [("0/1", "failed", "expected failure")])
            rc, out, err = dev(r, "prove", "--runs", "1")
            self.assertEqual(rc, 2, out + err)
            self.assertIn("nothing proven", out)
            self.assertIn("expected failure", out)

    def test_skipped_unittest_setup_keeps_failed_cases(self):
        source = ("import unittest\nimport calc\nclass T(unittest.TestCase):\n"
                  "    def test_value(self):\n        self.assertEqual(calc.value(), 2)\n")
        other = "\nclass TExtra(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(calc.value(), 2)\n"
        with Env() as e:
            r = e.repo({"calc.py": "def value():\n    return 1\n", "tests/test_calc.py": source + other})
            rc, out, err = dev(r, "run", "--all")
            self.assertEqual(rc, 1, out + err)
            edit(r, {"calc.py": "def value():\n    return 2\n",
                     "tests/test_calc.py": source + "\n    @classmethod\n    def setUpClass(cls):\n"
                     "        raise unittest.SkipTest('dependency unavailable')\n" + other})
            rc, out, err = dev(r, "run", "--all", "--json")
            self.assertEqual(rc, 0, out + err)
            run = json.loads(out)["steps"][0]["runs"][0]
            self.assertEqual((run["count"], run["passed"], run["skipped"]), (2, 1, 1))
            self.assertEqual(recorded(e), ["test_calc.T.test_value"])

    def test_merged_runs_keep_expected_failure_counts(self):
        with Env() as e:
            r = e.repo({"calc.py": "def value():\n    return 1\n",
                        "tests/test_calc.py": "import unittest\nimport calc\nclass T(unittest.TestCase):\n"
                        "    @unittest.expectedFailure\n    def test_value(self):\n"
                        "        self.assertEqual(calc.value(), 2)\n"})
            runner = testx.detect(str(r))[0]
            runs = [testx.run_tests(r, runner, files=["tests/test_calc.py"]) for _ in range(2)]
            result = testx._merged_runs(runs, ["tests/test_calc.py"], [], None)
            self.assertEqual((result["count"], result["passed"], result.get("expected_failure")), (2, 0, 2))


class RecordTests(unittest.TestCase):
    def test_buffered_stdout_after_the_summary_does_not_hide_completed_run(self):
        with Env() as e:
            r = e.repo({"tests/test_output.py": "import unittest\nclass T(unittest.TestCase):\n"
                        "    def test_print(self):\n        print('buffered test output')\n"
                        "        self.assertEqual(2 + 2, 4)\n"})
            for isolated in (False, True):
                with self.subTest(isolated=isolated):
                    run = testx.run_tests(r, testx.detect(str(r))[0], all_=True,
                                         env={"PYTHONUNBUFFERED": ""}, isolated_imports=isolated)
                    self.assertFalse(run["crash"], run)
                    self.assertTrue(run["complete"])
                    self.assertEqual((run["count"], run["passed"], run["failures"]), (1, 1, []))

    def test_nested_summary_cannot_complete_an_interrupted_outer_run(self):
        with Env() as e:
            r = e.repo({"tests/test_nested.py": "import os\nimport sys\nimport unittest\n"
                        "class T(unittest.TestCase):\n"
                        "    def test_a_pass(self):\n        self.assertEqual(2 + 2, 4)\n"
                        "    def test_b_exit(self):\n"
                        "        suite = unittest.TestSuite([unittest.FunctionTestCase(lambda: None)])\n"
                        "        unittest.TextTestRunner(stream=sys.stderr).run(suite)\n"
                        "        os._exit(0)\n"
                        "    def test_c_pending(self):\n        self.assertEqual(1, 2)\n"})
            for nested in ("[unittest.FunctionTestCase(lambda: None)]", "[]"):
                source = (r / "tests/test_nested.py").read_text()
                edit(r, {"tests/test_nested.py": source.replace("[unittest.FunctionTestCase(lambda: None)]", nested)})
                for isolated in (False, True):
                    for kwargs in ({"all_": True}, {"files": ["tests/test_nested.py"]}):
                        with self.subTest(nested=nested, isolated=isolated, selection=kwargs):
                            run = testx.run_tests(r, testx.detect(str(r))[0], isolated_imports=isolated, **kwargs)
                            self.assertTrue(run["crash"], run)
                            self.assertFalse(run["complete"])

    def test_completion_receipt_is_bound_to_invocation_schema_and_exit(self):
        with Env() as e:
            completion = {"path": str(e.root / "receipt.json"), "token": "this invocation"}
            runner = {"name": "unittest"}
            valid = {"version": 1, "token": "this invocation", "tests": 3, "skipped": 0,
                     "successful": True, "stopped": False}
            self.assertFalse(testx.run_complete(runner, 0, completion))
            for invalid in ("{", "[]", "{}", json.dumps(dict(valid, token="earlier invocation")),
                            json.dumps(dict(valid, version=0)), json.dumps(dict(valid, tests=True)),
                            json.dumps(dict(valid, tests=-1)), json.dumps(dict(valid, skipped="0")),
                            json.dumps(dict(valid, successful=1)), json.dumps(dict(valid, stopped=True))):
                with self.subTest(receipt=invalid):
                    Path(completion["path"]).write_text(invalid)
                    self.assertFalse(testx.run_complete(runner, 0, completion))
            Path(completion["path"]).write_text(json.dumps(valid))
            self.assertTrue(testx.run_complete(runner, 0, completion))
            self.assertFalse(testx.run_complete(runner, 1, completion))
            self.assertFalse(testx.run_complete(runner, -9, completion))
            self.assertFalse(testx.run_complete(runner, 124, completion))

    def test_soft_interrupt_does_not_complete_the_outer_selection(self):
        with Env() as e:
            r = e.repo({"tests/test_stop.py": "import os\nimport signal\nimport unittest\n"
                        "class T(unittest.TestCase):\n"
                        "    def test_a_stop(self):\n"
                        "        unittest.installHandler()\n        os.kill(os.getpid(), signal.SIGINT)\n"
                        "    def test_b_pending(self):\n        self.assertEqual(1, 2)\n"})
            run = testx.run_tests(r, testx.detect(str(r))[0], all_=True)
            self.assertTrue(run["crash"], run)
            self.assertFalse(run["complete"])

    def test_interruption_after_a_failure_keeps_unexecuted_failures(self):
        source = ("import os\nimport signal\nimport unittest\nclass T(unittest.TestCase):\n"
                  "    def test_a_failed(self):\n        self.assertEqual(1, 2)\n"
                  "    def test_b_interrupt(self):\n        os.kill(os.getpid(), signal.SIGTERM)\n"
                  "    def test_c_pending(self):\n        self.assertEqual(3, 4)\n")
        for selection in ("all", "files", "ids"):
            with self.subTest(selection=selection), Env() as e:
                r = e.repo({"tests/test_interrupt.py": source})
                pending = "test_interrupt.T.test_c_pending"
                seed(r, (pending, "tests/test_interrupt.py"))
                runner = testx.detect(str(r))[0]
                kwargs = {"all_": True} if selection == "all" else {
                    selection: ["tests/test_interrupt.py"] if selection == "files" else
                    ["test_interrupt.T.test_a_failed", "test_interrupt.T.test_b_interrupt", pending]}
                run = testx.run_tests(r, runner, **kwargs)
                testx.record(r, [run])
                self.assertIn(pending, recorded(e))
                self.assertTrue(run["crash"], run)
                self.assertIs(run.get("complete"), False)
                self.assertEqual(run["count"], 1)
                self.assertIn("test_interrupt.T.test_a_failed", [f["id"] for f in run["failures"]])
                self.assertIn("unittest:.", [f["id"] for f in run["failures"]])
                self.assertNotIn("unittest:.", state(e).get("full", {}))

    def test_early_success_exit_is_not_a_complete_test_run(self):
        with Env() as e:
            r = e.repo({"tests/test_interrupt.py": "import os\nimport unittest\nclass T(unittest.TestCase):\n"
                        "    def test_a_passed(self):\n        self.assertEqual(1, 1)\n"
                        "    def test_b_interrupt(self):\n        os._exit(0)\n"})
            rc, out, err = dev(r, "run", "--all", "--json")
            run = json.loads(out)["steps"][0]["runs"][0]
            self.assertEqual(rc, 1, out + err)
            self.assertTrue(run["crash"])
            self.assertIs(run.get("complete"), False)
            self.assertNotIn("unittest:.", state(e).get("full", {}))

    def test_merged_run_keeps_an_incomplete_part(self):
        with Env() as e:
            r = e.repo(CALC)
            runner = testx.detect(str(r))[0]
            passed = testx.run_tests(r, runner, files=["tests/test_other.py"])
            write(r, {"tests/test_exit.py": "import os\nimport unittest\nclass T(unittest.TestCase):\n"
                      "    def test_exit(self):\n        os._exit(1)\n"})
            interrupted = testx.run_tests(r, runner, files=["tests/test_exit.py"])
            merged = testx._merged_runs([passed, interrupted], [], [], None)
            self.assertIs(merged.get("complete"), False)
            self.assertTrue(merged["crash"])

    # a bare test name with no file cannot be rerun: unittest would import it as a module
    def test_a_failure_without_its_module_or_file_is_not_recorded(self):
        with Env() as e:
            r = e.repo(PKG)
            testx.record(r, [result([("test_add", ""), ("tests.test_calc.CalcTests.test_sub", "tests/test_calc.py")],
                                    ["tests/test_calc.py"])])
            self.assertEqual(recorded(e), ["tests.test_calc.CalcTests.test_sub"])

    # two lanes record at the same time: the first has read the record when the second writes; both results stay
    def test_two_concurrent_writers_both_keep_their_results(self):
        with Env() as e:
            r = e.repo(PKG)
            real_load, first_read, second_wrote = testx.load_state, threading.Event(), threading.Event()

            def load(top):
                st = real_load(top)
                if threading.current_thread().name == "first":
                    first_read.set()
                    second_wrote.wait(1)  # without a lock the second writer finishes here
                return st

            def first():
                testx.record(r, [result([("tests.test_calc.CalcTests.test_add", "tests/test_calc.py")],
                                        ["tests/test_calc.py"])])

            def second():
                first_read.wait(10)
                testx.record(r, [result([("tests.test_other.OtherTests.test_truth", "tests/test_other.py")],
                                        ["tests/test_other.py"])])
                second_wrote.set()

            with mock.patch.object(testx, "load_state", load):
                threads = [threading.Thread(target=first, name="first"), threading.Thread(target=second)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(30)
            self.assertEqual(recorded(e), ["tests.test_calc.CalcTests.test_add",
                                           "tests.test_other.OtherTests.test_truth"])


class EstimateTests(unittest.TestCase):
    def test_estimates_from_history(self):
        rn = {"name": "unittest", "root": "."}
        st = {"durations": {"unittest:.": {"a": 2.0, "b": 4.0}}}
        self.assertEqual(testx.estimate(st, rn, ["a", "b"]), 6.0)
        self.assertEqual(testx.estimate(st, rn, ["a", "c"]), 5.0)  # c unknown: the average of what is known
        st["full"] = {"unittest:.": {"secs": 30.0, "count": 40}}
        self.assertEqual(testx.estimate(st, rn, ["a", "c"], n_files=10), 5.0)  # c: 30 s over 10 files
        self.assertEqual(testx.estimate(st, rn, all_=True), 30.0)
        self.assertIsNone(testx.estimate({}, rn, ["a"]))


class PlanTests(unittest.TestCase):
    def test_plan_explains_and_runs_nothing(self):
        with Env() as e:
            r = e.repo(CALC)
            write(r, {"calc.py": BROKEN_ADD, "tools/orphan.py": "X = 1\n", "README.md": "# more\n"})
            rc, out, _ = dev(r, "plan")
            self.assertEqual(rc, 0)
            self.assertIn("calc.py", out)
            self.assertIn("tests/test_calc.py", out)
            self.assertIn("widened", out)
            self.assertIn("-m unittest", out)
            self.assertEqual(state(e), {})
            data = json.loads(dev(r, "plan", "--json")[1])
            self.assertEqual(data["radius"]["mode"], "all")  # tools/orphan.py: nothing near it, so everything
            self.assertEqual(data["runs"][0]["runner"], "unittest")

    def test_plan_all_lists_the_suite(self):
        with Env() as e:
            r = e.repo(CALC)
            rc, out, _ = dev(r, "plan", "--all")
            self.assertEqual(rc, 0)
            self.assertIn("2 test file(s)", out)


def numbered(n):
    """n source modules, each with its own unittest module that reads it."""
    files = {".gitignore": "__pycache__/\n"}
    for i in range(n):
        files[f"src_{i:03d}.py"] = f"N = {i}\n"
        files[f"tests/test_src_{i:03d}.py"] = f"""
            import sys
            import unittest
            from pathlib import Path

            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            import src_{i:03d}  # noqa: E402


            class SrcTests(unittest.TestCase):
                def test_keeps_its_number(self):
                    self.assertEqual(src_{i:03d}.N % 1000, {i})
            """
    return files


class WideBranchTests(unittest.TestCase):
    # a branch far from its base (a long-lived integration branch) reaches a large part of the suite: plain
    # `dev test` says so before it starts, and names the tests of the files touched now; --affected runs them all
    def test_plain_dev_test_does_not_start_a_wide_branchs_selection(self):
        with Env() as e:
            r = e.repo(numbered(110))
            write(r, {f"src_{i:03d}.py": f"N = {i + 1000}\n" for i in range(110)})
            write(r, {f"web/w{i}.test.mjs": "" for i in range(5)})  # tests no runner here takes: not counted
            git(r, "add", "-A")
            git(r, "commit", "-q", "-m", "feat: renumber")
            write(r, {"src_007.py": "N = 2007\n"})
            rc, out, err = dev(r)
            self.assertEqual(rc, 2, out + err)
            self.assertIn("110 test file(s)", out + err)
            self.assertIn("dev test run tests/test_src_007.py", out + err)
            self.assertEqual(state(e), {})  # nothing ran, nothing recorded
            rc, out, _ = dev(r, "plan")  # the plan says so too
            self.assertEqual(rc, 0)
            self.assertIn("plain `dev test` does not start them", out)
            rc, out, err = dev(r, "--affected")
            self.assertEqual(rc, 0, out + err)
            self.assertIn("110 test(s) passed", out)

    # the hold is for plain `dev test` only: `dev test run`, even with no paths, starts what it selects
    def test_dev_test_run_without_paths_starts_a_wide_branchs_selection(self):
        with Env() as e, mock.patch.object(testx, "WIDE_CHANGES", 3), mock.patch.object(testx, "WIDE_TESTS", 3):
            r = e.repo(numbered(4))
            write(r, {f"src_{i:03d}.py": f"N = {i + 1000}\n" for i in range(4)})
            git(r, "add", "-A")
            git(r, "commit", "-q", "-m", "feat: renumber")
            self.assertEqual(dev(r)[0], 2)
            rc, out, err = dev(r, "run")
            self.assertEqual(rc, 0, out + err)
            self.assertIn("4 test(s) passed", out)


class AuditTests(unittest.TestCase):
    def test_missing_module_is_a_clear_message(self):
        with Env() as e:
            r = e.repo(CALC)
            testx.AUDIT_MODULE = "no_such_testaudit_module"
            rc, out, err = dev(r, "audit", "--changed")
            self.assertEqual(rc, 2)
            self.assertIn("not available", err)
            self.assertNotIn("Traceback", out + err)

    def test_delegates_arguments_and_command(self):
        import types
        mod = types.ModuleType("devkit.fake_testaudit")
        mod.add_arguments = lambda p: p.add_argument("--changed", action="store_true")
        mod.cmd = lambda a: 7 if a.changed else 3
        sys.modules["devkit.fake_testaudit"] = mod
        self.addCleanup(sys.modules.pop, "devkit.fake_testaudit", None)
        with Env() as e:
            r = e.repo(CALC)
            testx.AUDIT_MODULE = "fake_testaudit"
            self.assertEqual(dev(r, "audit", "--changed")[0], 7)
            self.assertEqual(dev(r, "audit")[0], 3)


# --------------------------------------------------------------------------- dev test prove

PROVE_BASE = {
    "calc.py": "def add(a, b):\n    return a + b\n",
    "tests/test_calc.py": """
        import sys
        import unittest
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import calc  # noqa: E402


        class CalcTests(unittest.TestCase):
            def test_add(self):
                self.assertEqual(calc.add(1, 2), 3)
        """,
    ".gitignore": "__pycache__/\n",
}
MUL = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
TEST_MUL = """
    def test_mul(self):
        self.assertEqual(calc.mul(2, 3), 6)
"""
TEST_ADD_AGAIN = """
    def test_add_again(self):
        self.assertEqual(calc.add(2, 2), 4)
"""


def with_tests(*extra):
    return textwrap.dedent(PROVE_BASE["tests/test_calc.py"]).lstrip("\n") + "".join(extra)


# PROVE_BASE whose test module defines a helper of its own, outside every test
SANDBOX_BASE = dict(PROVE_BASE, **{"tests/test_calc.py": textwrap.dedent(PROVE_BASE["tests/test_calc.py"]).replace(
    "\n\nclass CalcTests", "\n\ndef sandbox_env(env):\n    return dict(env)\n\n\nclass CalcTests")})


# A stand-in for pytest, so a parametrized test runs where pytest is not installed. `python -m pytest [-q]
# [--junitxml=R] <file>[::<test>]…` runs the test functions, one case per @pytest.mark.parametrize value, named
# name[value] as pytest names them, and writes pytest's JUnit report (classname and name, as pytest's xunit2 does).
# A module it cannot import is a collection error, reported as pytest 9 reports it (no classname, the dotted module as
# the name, "collection failure" and the error on an `E` line), and it stops the session before any test runs
# unless --continue-on-collection-errors is given. As pytest's default import mode does, it imports a test file by its
# name inside the packages that hold it, so two files of one session with the same such name fail to collect ("import
# file mismatch"), unless importlib mode is on its command line or in its configuration's addopts (pytest.ini, else
# pyproject.toml, as pytest picks them). Without file arguments it collects tests/**/test_*.py. Like pytest, it ends
# with a short summary (a FAILED or ERROR line per failure, then the counts) and exits 1 when a test failed; a
# <file>::<test> whose test is gone stops the session before any test runs ("ERROR: not found", "no tests ran", exit 4).
STANDIN_PYTEST = '''
import glob
import importlib.util
import os
import sys
import xml.etree.ElementTree as ET


class _Mark:
    @staticmethod
    def parametrize(argname, values):
        def deco(fn):
            fn.cases = [(f"{fn.__name__}[{v}]", {argname: v}) for v in values]
            return fn
        return deco


mark = _Mark()


_SEEN = {}


def _module_name(path):
    parts = path[:-3].split("/")
    i = len(parts) - 1
    while i > 0 and os.path.isfile(os.path.join(*parts[:i], "__init__.py")):
        i -= 1
    return ".".join(parts[i:])


def _importlib_mode():
    if "--import-mode=importlib" in sys.argv:
        return True
    for f in ("pytest.ini", "pyproject.toml"):
        if os.path.isfile(f):
            text = open(f).read()
            return "--import-mode" in text and "importlib" in text
    return False


def _load(path):
    name = _module_name(path)
    if not _importlib_mode() and _SEEN.setdefault(name, path) != path:
        raise ImportError(f"import file mismatch: imported module '{name}' has this __file__ attribute: "
                          f"{_SEEN[name]} which is not the same as the test file we want to collect: {path}")
    spec = importlib.util.spec_from_file_location(path[:-3].replace("/", "."), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cases(target, mod):
    path, _, name = target.partition("::")
    for n in [name] if name else [n for n in vars(mod) if n.startswith("test")]:
        fn = getattr(mod, n)
        for case, kw in getattr(fn, "cases", [(n, {})]):
            yield path[:-3].replace("/", "."), case, fn, kw


def _report(argv, suite):
    report = next((a.split("=", 1)[1] for a in argv if a.startswith("--junitxml=")), None)
    if report:
        root = ET.Element("testsuites")
        root.append(suite)
        ET.ElementTree(root).write(report, encoding="utf-8", xml_declaration=True)


def main(argv):
    suite = ET.Element("testsuite", name="pytest")
    ran = failed = errors = 0
    loaded, summary = [], []
    targets = [a for a in argv if not a.startswith("-")] or sorted(glob.glob("tests/**/test_*.py", recursive=True))
    for target in targets:
        path = target.partition("::")[0]
        try:
            loaded.append((target, _load(path)))
        except Exception as e:
            failed += 1
            errors += 1
            tc = ET.SubElement(suite, "testcase", classname="", name=path[:-3].replace("/", "."), time="0")
            ET.SubElement(tc, "error", message="collection failure").text = (
                f"ImportError while importing test module '{path}'.\\nE   {type(e).__name__}: {e}")
            summary.append(f"ERROR {path} - {type(e).__name__}: {e}")
    gone = [t for t, mod in loaded if t.partition("::")[2] and t.partition("::")[2] not in vars(mod)]
    if gone:
        for t in gone:
            print(f"ERROR: not found: {os.path.abspath(t.partition('::')[0])}::{t.partition('::')[2]}")
        print("no tests ran in 0.01s")
        _report(argv, ET.Element("testsuite", name="pytest"))
        return 4
    if failed and "--continue-on-collection-errors" not in argv:
        loaded = []
    for target, mod in loaded:
        for cls, case, fn, kw in _cases(target, mod):
            ran += 1
            tc = ET.SubElement(suite, "testcase", classname=cls, name=case, time="0.001")
            try:
                fn(**kw)
            except Exception as e:
                failed += 1
                ET.SubElement(tc, "failure", message=f"{type(e).__name__}: {e}").text = repr(e)
                summary.append(f"FAILED {target.partition('::')[0]}::{case} - {type(e).__name__}: {e}")
    _report(argv, suite)
    if summary:
        print("=========================== short test summary info ============================")
        for line in summary:
            print(line)
    counts = [f"{n} {word}" for n, word in ((failed - errors, "failed"), (ran - failed + errors, "passed"),
                                            (errors, "error" if errors == 1 else "errors")) if n]
    print(f"{', '.join(counts)} in 0.01s" if counts else "no tests ran in 0.01s")
    return 1 if failed else 0 if ran else 5


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''
# Stand-ins for Playwright and the Angular CLI, so their specs run where neither is installed (node is enough). A
# shared runtime registers test()/it() and describe(), runs each file given, and reports what each tool reports:
# `playwright test [--reporter=…] <files>` writes Playwright's JUnit report (classname: the file relative to e2e/, name:
# the titles joined by ' › ') to $PLAYWRIGHT_JUNIT_OUTPUT_FILE; `ng test <project> --include <file>… --output-file=R`
# sets the globals the unit-test builder sets and writes Vitest's JSON report to R. Both exit 1 when a test failed.
STANDIN_JS = {
    "node_modules/devkit-standin/runtime.mjs": """
        import path from 'node:path';
        import { pathToFileURL } from 'node:url';

        const tests = [];
        const prefix = [];
        export function test(title, fn) { tests.push({ titles: [...prefix, title], fn }); }
        export function describe(title, fn) { prefix.push(title); try { fn(); } finally { prefix.pop(); } }
        test.describe = describe;
        export const it = test;
        export function expect(actual) {
          return { toBe(expected) { if (!Object.is(actual, expected)) throw new Error(`expected ${actual} to be ${expected}`); } };
        }
        export async function run(files) {
          const results = [];
          for (const file of files) {
            await import(pathToFileURL(path.resolve(file)).href);
            for (const t of tests.splice(0)) {
              try { await t.fn({}); results.push({ file, titles: t.titles, ok: true, message: '' }); }
              catch (e) { results.push({ file, titles: t.titles, ok: false, message: String(e && e.message) }); }
            }
          }
          return results;
        }
        """,
    "node_modules/devkit-standin/playwright.mjs": """
        import { writeFileSync } from 'node:fs';
        import path from 'node:path';
        import { run } from './runtime.mjs';

        const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
        export async function main(args) {
          const files = [];
          for (let i = 0; i < args.length; i++) {
            if (args[i] === '--grep' || args[i] === '-g') i++;
            else if (args[i] !== 'test' && !args[i].startsWith('-')) files.push(args[i]);
          }
          const results = await run(files);
          const cases = results.map((r) => `<testcase name="${esc(r.titles.join(' › '))}" classname="${esc(path.relative('e2e', r.file))}">` +
            (r.ok ? '' : `<failure message="${esc(r.message)}"/>`) + '</testcase>');
          if (process.env.PLAYWRIGHT_JUNIT_OUTPUT_FILE) {
            writeFileSync(process.env.PLAYWRIGHT_JUNIT_OUTPUT_FILE, `<testsuites><testsuite name="specs">${cases.join('')}</testsuite></testsuites>`);
          }
          if (!results.length) console.log('Error: No tests found');
          process.exitCode = results.length && results.every((r) => r.ok) ? 0 : 1;
        }
        """,
    "node_modules/devkit-standin/ng.mjs": """
        import { writeFileSync } from 'node:fs';
        import path from 'node:path';
        import { describe, expect, it, run } from './runtime.mjs';

        export async function main(args) {
          Object.assign(globalThis, { describe, it, expect });
          const files = [];
          let out = '';
          for (let i = 0; i < args.length; i++) {
            if (args[i] === '--include') files.push(args[++i]);
            else if (args[i].startsWith('--output-file=')) out = args[i].slice('--output-file='.length);
          }
          const results = await run(files);
          const byFile = new Map();
          for (const r of results) {
            if (!byFile.has(r.file)) byFile.set(r.file, []);
            byFile.get(r.file).push({ ancestorTitles: r.titles.slice(0, -1), title: r.titles[r.titles.length - 1],
              fullName: r.titles.join(' '), status: r.ok ? 'passed' : 'failed', failureMessages: r.ok ? [] : [r.message] });
          }
          if (out) {
            writeFileSync(out, JSON.stringify({ testResults: [...byFile].map(([f, a]) => ({ name: path.resolve(f), assertionResults: a })) }));
          }
          if (!results.length) console.log('No test files found');
          process.exitCode = results.length && results.every((r) => r.ok) ? 0 : 1;
        }
        """,
    "node_modules/@playwright/test/package.json": '{"name": "@playwright/test", "type": "module", "exports": "./index.mjs"}\n',
    "node_modules/@playwright/test/index.mjs": "export { expect, test } from '../../devkit-standin/runtime.mjs';\n",
}
for _tool in ("playwright", "ng"):  # npm's bin links: run the tool's module with the arguments
    STANDIN_JS[f"node_modules/.bin/{_tool}"] = (
        "#!/usr/bin/env node\nconst { pathToFileURL } = require('url');\n"
        f"import(pathToFileURL(require('path').join(__dirname, '..', 'devkit-standin', '{_tool}.mjs')).href)\n"
        "  .then((m) => m.main(process.argv.slice(2)));\n")


def standin_js(r):
    """Put the Playwright and Angular CLI stand-ins in r/node_modules (ignored, so the proof copy links it)."""
    write(r, STANDIN_JS)
    for tool in ("playwright", "ng"):
        (Path(r) / "node_modules" / ".bin" / tool).chmod(0o755)


HOSTS_BASE = {
    "pytest.ini": "[pytest]\n",
    "pytest.py": STANDIN_PYTEST,
    "hosts.py": "def label(host):\n    return host\n",
    "tests/test_hosts.py": """
        import sys
        from pathlib import Path

        import pytest

        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import hosts  # noqa: E402


        def test_label_keeps_a_name():
            assert hosts.label("a") == "a"
        """,
    ".gitignore": "__pycache__/\n",
}
HOSTS_CHANGE = "def label(host):\n    return host or 'localhost'\n\n\ndef count(names):\n    return len([n for n in names if n])\n"
HOSTS_TESTS = """

@pytest.mark.parametrize("host", [None, "a", "b"])
def test_label_is_never_empty(host):
    assert hosts.label(host)


def test_count_skips_empty_hosts():
    assert hosts.count([None, "a"]) == 1
"""

# a package whose tests run with node --test: a node:test file and a plain script with its own checks (it sets a
# failing exit code), next to a harness the scripts import
NODE_BASE = {
    "package.json": json.dumps({"name": "proj", "type": "module", "scripts": {"test": "node --test"}}),
    "lib/slug.mjs": "export const slug = (s) => s.toLowerCase();\n",
    "test/harness.mjs": "export function check(ok, what) {\n  if (!ok) {\n    console.error(`FAIL: ${what}`);\n"
                        "    process.exitCode = 1;\n  }\n}\n",
}
NODE_CHANGE = {
    "lib/slug.mjs": 'export const slug = (s) => s.trim().toLowerCase().replace(/\\s+/g, "-");\n',
    "test/harness.mjs": NODE_BASE["test/harness.mjs"].replace("FAIL: ", "failed: "),
    "test/slug.test.mjs": 'import assert from "node:assert/strict";\nimport { test } from "node:test";\n'
                          'import { slug } from "../lib/slug.mjs";\n\ntest("joins words with dashes", () => {\n'
                          '  assert.equal(slug(" Big News "), "big-news");\n});\n',
    "test/page.mjs": 'import { check } from "./harness.mjs";\nimport { slug } from "../lib/slug.mjs";\n\n'
                     'check(slug("Two Words") === "two-words", "the page slug joins its words");\n',
}


# unit and integration tests with the same file name in folders that are no packages: pytest's default import mode
# imports both as `test_api`, so one pytest process cannot collect both
SAME_NAME_BASE = {
    "pytest.ini": "[pytest]\n",
    "pytest.py": STANDIN_PYTEST,
    "api.py": "def version():\n    return 1\n",
    "tests/unit/x/test_api.py": "import sys\nfrom pathlib import Path\n\n"
                                "sys.path.insert(0, str(Path(__file__).resolve().parents[3]))\nimport api  # noqa: E402\n\n\n"
                                "def test_unit_version():\n    assert api.version() >= 1\n",
    "tests/integration/x/test_api.py": "import sys\nfrom pathlib import Path\n\n"
                                       "sys.path.insert(0, str(Path(__file__).resolve().parents[3]))\n"
                                       "import api  # noqa: E402\n\n\ndef test_integration_version():\n"
                                       "    assert api.version() in (1, 2)\n",
    ".gitignore": "__pycache__/\n",
}


class SameNameTestFileTests(unittest.TestCase):
    # the two files named on the command line run in separate pytest processes, and both pass
    def test_named_test_files_with_one_module_name_run_in_separate_processes(self):
        with Env() as e:
            r = e.repo(SAME_NAME_BASE)
            rc, out, err = dev(r, "run", "tests/unit/x/test_api.py", "tests/integration/x/test_api.py", "--json")
            run = json.loads(out)["steps"][0]["runs"][0]
            self.assertEqual((rc, run["count"], run["failed"]), (0, 2, 0), out + err)
            self.assertEqual(sorted(run["files"]), ["tests/integration/x/test_api.py", "tests/unit/x/test_api.py"])

    # a change that every test depends on runs the whole suite: split the same way, and timed as the full suite
    def test_the_whole_suite_with_one_module_name_twice_runs_in_separate_processes(self):
        with Env() as e:
            r = e.repo(SAME_NAME_BASE)
            edit(r, {"api.py": "def version():\n    return 2\n"})
            rc, out, err = dev(r, "--json")
            run = json.loads(out)["steps"][0]["runs"][0]
            self.assertEqual((rc, run["all"], run["count"], run["failed"]), (0, True, 2, 0), out + err)
            self.assertEqual(state(e)["full"]["pytest:."]["count"], 2)

    # a project that imports its tests with pytest's importlib mode has no clash between same-name files: named files
    # run in one pytest process, as the project runs them
    def test_named_same_name_files_run_in_one_process_in_importlib_mode(self):
        with Env() as e:
            r = e.repo({**SAME_NAME_BASE, "pytest.ini": "[pytest]\naddopts = --import-mode=importlib\n"})
            rc, out, err = dev(r, "run", "tests/unit/x/test_api.py", "tests/integration/x/test_api.py", "--json")
            run = json.loads(out)["steps"][0]["runs"][0]
            self.assertEqual((rc, run["count"], run["failed"]), (0, 2, 0), out + err)
            self.assertNotIn(";", run["argv"])

    # in importlib mode the whole suite is one pytest run of its own collection (testpaths, norecursedirs), never a
    # list of files
    def test_the_whole_suite_is_one_pytest_run_in_importlib_mode(self):
        with Env() as e:
            base = {k: v for k, v in SAME_NAME_BASE.items() if k != "pytest.ini"}
            r = e.repo({**base, "pyproject.toml": '[tool.pytest.ini_options]\naddopts = ["--import-mode", "importlib"]\n'})
            rc, out, err = dev(r, "run", "--all", "--json")
            run = json.loads(out)["steps"][0]["runs"][0]
            self.assertEqual((rc, run["count"], run["failed"]), (0, 2, 0), out + err)
            self.assertEqual([a for a in run["argv"] if a == ";" or a.endswith(".py")], [])

    # importlib mode is read where pytest reads it: the first configuration file that configures pytest wins (a
    # pytest.ini without it hides a pyproject.toml with it), then PYTEST_ADDOPTS adds to it
    def test_importlib_mode_is_read_from_the_configuration_pytest_reads(self):
        cases = [({"setup.cfg": "[tool:pytest]\naddopts = -q --import-mode importlib\n"}, {}, True),
                 ({"tox.ini": "[tox]\nenvlist = py\n", "setup.cfg": "[tool:pytest]\naddopts = --import-mode=importlib\n"},
                  {}, True),
                 ({"pytest.ini": "[pytest]\n", "pyproject.toml": "[tool.pytest.ini_options]\n"
                                                                 "addopts = '--import-mode=importlib'\n"}, {}, False),
                 ({"pytest.ini": "[pytest]\naddopts = --import-mode=prepend\n"}, {}, False),
                 ({"pytest.ini": "[pytest]\n"}, {"PYTEST_ADDOPTS": "--import-mode=importlib"}, True)]
        for files, env, want in cases:
            with self.subTest(files=sorted(files), env=env), tempfile.TemporaryDirectory() as d:
                write(d, files)
                runner = {"name": "pytest", "root": ".", "exec": [PY, "-m", "pytest"]}
                with mock.patch.dict(os.environ, {"PYTEST_ADDOPTS": ""}):
                    self.assertEqual(testx._pytest_importlib(d, runner, env), want)

    # a recorded failure of each file reruns first, each in its own process
    def test_recorded_failures_of_both_files_rerun_in_separate_processes(self):
        with Env() as e:
            r = e.repo(SAME_NAME_BASE)
            edit(r, {"api.py": "def version():\n    return 0\n"})
            rc, out, err = dev(r, "run", "tests/unit/x/test_api.py", "tests/integration/x/test_api.py")
            self.assertEqual(rc, 1, out + err)
            self.assertEqual(recorded(e), ["tests/integration/x/test_api.py::test_integration_version",
                                           "tests/unit/x/test_api.py::test_unit_version"])
            edit(r, {"api.py": "def version():\n    return 1\n"})
            rc, out, err = dev(r, "run", "--failed", "--json")
            run = json.loads(out)["steps"][0]["runs"][0]
            self.assertEqual((rc, run["count"], run["failed"]), (0, 2, 0), out + err)
            self.assertEqual(recorded(e), [])


# calc with pytest tests (the stand-in above): three tests of calc in one file, and one of nothing in another
PYCALC = {
    "pytest.ini": "[pytest]\n",
    "pytest.py": STANDIN_PYTEST,
    "calc.py": "NAMES = ['a']\n\n\ndef add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n",
    "tests/test_calc.py": "import sys\nfrom pathlib import Path\n\n"
                          "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\nimport calc  # noqa: E402\n\n\n"
                          "def test_add():\n    assert calc.add(1, 2) == 3\n\n\n"
                          "def test_add_zero():\n    assert calc.add(0, 5) == 5\n\n\n"
                          "def test_mul():\n    assert calc.mul(2, 3) == 6\n",
    "tests/test_text.py": "def test_upper():\n    assert 'a'.upper() == 'A'\n",
    ".gitignore": "__pycache__/\n",
}
PY_BROKEN_ADD = PYCALC["calc.py"].replace("return a + b", "return a - b")
PY_BROKEN_MUL = PYCALC["calc.py"].replace("return a * b", "return a + b")


class PytestOutcomeTests(unittest.TestCase):
    # pytest has no completion receipt: a run that exits 1 and reports its failures finished its selection. Each
    # failure counts once, and an ordinary failing run is no crash
    def test_a_failing_pytest_run_counts_each_failure_once_and_is_no_crash(self):
        with Env() as e:
            r = e.repo(dict(PYCALC, **{"calc.py": PY_BROKEN_ADD}))
            rc, out, err = dev(r, "run", "tests/test_calc.py", "--json")
            run = json.loads(out)["steps"][0]["runs"][0]
            self.assertEqual((rc, run["count"], run["passed"], run["failed"], run["crash"], run["complete"]),
                             (1, 3, 1, 2, False, True), out + err)
            self.assertEqual([f["id"] for f in run["failures"]],
                             ["tests/test_calc.py::test_add", "tests/test_calc.py::test_add_zero"])

    # a recorded failure whose test was renamed: pytest stops at the name that is gone ("no tests ran", exit 4). That
    # is an empty run, not a failure: the name is forgotten instead of failing first for ever
    def test_a_renamed_pytest_test_that_failed_is_forgotten(self):
        with Env() as e:
            r = e.repo(dict(PYCALC, **{"calc.py": PY_BROKEN_MUL}))
            self.assertEqual(dev(r, "run", "tests/test_calc.py")[0], 1)
            self.assertEqual(recorded(e), ["tests/test_calc.py::test_mul"])
            edit(r, {"tests/test_calc.py": PYCALC["tests/test_calc.py"].replace("def test_mul", "def test_product"),
                     "calc.py": PYCALC["calc.py"]})
            rc, out, err = dev(r, "run", "--failed")
            self.assertEqual(rc, 0, out + err)
            self.assertIn("no longer select a test (renamed or deleted): forgotten: tests/test_calc.py::test_mul", err)
            self.assertEqual(recorded(e), [])

    # without the change, a failure of another test makes pytest exit 1: that is no crash, so a new case that did not
    # run without the change stays unproven instead of counting as a failure that proves it
    def test_a_case_that_did_not_run_without_the_change_proves_nothing_when_other_tests_fail(self):
        with Env() as e:
            r = e.repo(PYCALC)
            write(r, {"calc.py": PYCALC["calc.py"].replace("['a']", "['a', 'b']"),
                      "tests/test_names.py": "import sys\nfrom pathlib import Path\n\nimport pytest\n\n"
                                             "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
                                             "import calc  # noqa: E402\n\n\n"
                                             "@pytest.mark.parametrize('name', calc.NAMES)\n"
                                             "def test_name_is_one_letter(name):\n    assert len(name) == 1\n"})
            with open(r / "tests" / "test_calc.py", "a") as fh:
                fh.write("\n\ndef test_names_grew():\n    assert calc.NAMES == ['a', 'b']\n")
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            self.assertEqual(rc, 1, out + err)  # the "a" case passes without the change
            name = "tests/test_names.py::test_name_is_one_letter"
            self.assertEqual({t["id"]: (t["without_change"], t["verdict"]) for t in json.loads(out)["tests"]}, {
                "tests/test_calc.py::test_names_grew": ("failed", "proves the change"),
                f"{name}[a]": ("passed", "does not test the change"),
                f"{name}[b]": ("not run", "did not run without the change")})


class LaneScopeTests(unittest.TestCase):
    """A crew member's unscoped `dev test` and `dev test prove` in a checkout that other lanes change too take the
    changed files of the member's own scope. The lane is found as for `dev commit`: who calls (identity) and the
    session's run are patched at progress, the run's scope rules are real."""

    OTHER = "def sub(a, b):\n    return a - b\n"

    def lane(self, scope, project=None):
        run = {"goal": "Work", "agents": {"a_kid": {"scope": scope, "label": "Kid: calc"}}, "plan": [],
               **({"project": project} if project else {})}
        return mock.patch.multiple(_progress(), _as_lane=lambda *a, **k: "a_kid",
                                   current_session=lambda *a, **k: ("claude", "s1"),
                                   load_run=lambda *a, **k: json.loads(json.dumps(run)))

    def repo(self, e):
        # test_text.py is untouched, so the changes reach some test files, not all, and each runs by name
        r = e.repo(dict(PROVE_BASE, **{"other.py": self.OTHER, "tests/test_text.py": """
            import unittest


            class TextTests(unittest.TestCase):
                def test_upper(self):
                    self.assertEqual("a".upper(), "A")
            """, "tests/test_other.py": """
            import sys
            import unittest
            from pathlib import Path

            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            import other  # noqa: E402


            class OtherTests(unittest.TestCase):
                def test_sub(self):
                    self.assertEqual(other.sub(3, 1), 2)
            """}))
        (r / "calc.py").write_text(MUL)
        (r / "tests" / "test_calc.py").write_text(with_tests(TEST_MUL))
        (r / "other.py").write_text(self.OTHER + "\n\ndef neg(a):\n    return -a\n")
        with open(r / "tests" / "test_other.py", "a") as fh:
            fh.write("\n    def test_neg(self):\n        self.assertEqual(other.neg(2), -2)\n")
        return r

    def test_an_unscoped_dev_test_in_a_lane_runs_the_tests_of_its_own_repository_files(self):
        with Env() as e:
            r = self.repo(e)
            rc, out, err = dev(r, "--json")
            self.assertEqual(rc, 0, out + err)
            ran = sorted(f for s in json.loads(out)["steps"] for run in s["runs"] for f in run["files"])
            self.assertEqual(ran, ["tests/test_calc.py", "tests/test_other.py"])
            with self.lane(["calc.py", "tests/test_calc.py"]):
                rc, out, err = dev(r, "--json")
            self.assertEqual(rc, 0, out + err)
            ran = sorted(f for s in json.loads(out)["steps"] for run in s["runs"] for f in run["files"])
            self.assertEqual(ran, ["tests/test_calc.py"])
            self.assertEqual(json.loads(out)["outside_lane"], ["other.py", "tests/test_other.py"])
            with self.lane(["calc.py", "tests/test_calc.py"]):
                rc, out, err = dev(r)
            self.assertEqual(rc, 0, out + err)
            self.assertIn("left out 2 changed file(s) outside this crew lane's scope", err)
            with self.lane(["calc.py", "tests/test_calc.py"]):
                rc, out, err = dev(r, "run", "tests/test_other.py", "--json")
            self.assertEqual(rc, 0, out + err)
            self.assertEqual([f for s in json.loads(out)["steps"] for run in s["runs"] for f in run["files"]],
                             ["tests/test_other.py"])
            # the lane's globs name files of its run's repository: in another one (dev test -p, a test suite's own
            # temporary repositories) they say nothing, and every changed file counts
            from devkit import lanescope
            home = e.root / "dev"
            (home / "acme" / "web" / ".git").mkdir(parents=True)
            with self.lane(["calc.py", "tests/test_calc.py"], project="acme/web"), \
                    mock.patch.object(lanescope, "DEV", str(home)):
                rc, out, err = dev(r, "--json")
            self.assertEqual(rc, 0, out + err)
            ran = sorted(f for s in json.loads(out)["steps"] for run in s["runs"] for f in run["files"])
            self.assertEqual(ran, ["tests/test_calc.py", "tests/test_other.py"])

    # a lane's scope counts in the checkout `dev commit` counts it in: a lane bound to a worktree that runs dev test
    # in the repository's main checkout owns none of the main checkout's changes, which dev commit would refuse
    def test_a_lane_bound_to_another_checkout_takes_none_of_this_checkouts_files(self):
        from devkit import gitx
        with Env() as e:
            r = self.repo(e)
            wt = e.root / "wt"
            subprocess.run(["git", "-C", str(r), "worktree", "add", "-q", "-b", "feat/other", str(wt)], check=True,
                           capture_output=True)
            scope = [["calc.py", "tests/test_calc.py"]]
            # where dev commit counts this lane's scope: the worktree its scope was given in
            with self.lane(scope[0]), mock.patch.object(gitx, "_commit_lane",
                                                        side_effect=lambda c: ("a_kid", scope, str(wt))):
                rc, out, err = dev(r, "--json")
                self.assertEqual(testx.lane_files(str(r), ["calc.py"]), ([], ["calc.py"]))
                self.assertEqual(testx.lane_files(str(wt), ["calc.py"]), (["calc.py"], []))
            self.assertEqual(rc, 0, out + err)
            self.assertEqual([f for s in json.loads(out)["steps"] for run in s["runs"] for f in run["files"]], [])
            self.assertEqual(json.loads(out)["outside_lane"],
                             ["calc.py", "other.py", "tests/test_calc.py", "tests/test_other.py"])

    def test_dev_test_prove_in_a_lane_proves_the_changed_tests_of_its_own_files(self):
        with Env() as e:
            r = self.repo(e)
            with self.lane(["calc.py", "tests/test_calc.py"]):
                rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            self.assertEqual(rc, 0, out + err)
            data = json.loads(out)
            self.assertEqual([t["id"] for t in data["tests"]], ["test_calc.CalcTests.test_mul"])
            self.assertEqual(data["outside_lane"], ["tests/test_other.py"])


class AnotherProjectTests(unittest.TestCase):
    # dev test -p <name> finds that one project in the map the last dev sync cached, as dev task -p does: building
    # the whole map runs git in every checkout on the Mac before a test starts

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_startup import Layout, touching  # a Mac's dev root with git and lsof stand-ins that log calls
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mac = Layout(Path(os.path.realpath(tmp.name)))
        self.touching = touching

    def test_dev_test_of_another_project_reads_only_that_checkout(self):
        self.mac.sync()
        (self.mac.app / "tests").mkdir()
        (self.mac.app / "tests" / "test_one.py").write_text("import unittest\n\n\nclass One(unittest.TestCase):\n"
                                                            "    def test_one(self):\n        self.assertEqual(1, 1)\n")
        rc, out, calls = self.mac.dev("test", "-p", "app", "plan", "--json", cwd=self.mac.home)
        self.assertEqual(rc, 0, out)
        self.assertIn("tests/test_one.py", out)
        self.assertEqual(self.touching(calls, self.mac.api), [], calls)
        self.assertEqual([c for c in calls if c.startswith("lsof ")], [], calls)


def _progress():
    from devkit import progress
    return progress


class RunLogRetentionTests(unittest.TestCase):
    # outside Codex too, a log whose run still writes it survives the retention of newer runs: the oldest completed
    # logs go, ten stay, and the running one is kept whatever its age
    def test_a_log_still_being_written_survives_newer_runs_retention(self):
        quick = {"test_quick.py": "import unittest\n\n\nclass Quick(unittest.TestCase):\n"
                                  "    def test_ok(self):\n        self.assertEqual(2 + 2, 4)\n"}
        with Env() as e, mock.patch.object(_progress(), "resolve_session",
                                           return_value=("claude", "s1", "env")):
            r = str(e.repo(quick))
            runner = {"name": "unittest", "root": ".", "python": PY, "exec": [PY, "-m", "unittest"]}
            with testx._run_log(r, runner) as active:
                active.parent.mkdir(parents=True, exist_ok=True)  # as run_tests does before it writes
                active.write_text("a long run's output so far\n")
                newer = [testx._run_step(r, [(runner, {"files": ["test_quick.py"]})], False, True, "affected",
                                         "Tests")["runs"][0]["log"] for _ in range(11)]
                self.assertTrue(active.exists(), "retention removed a log its run still writes")
            self.assertFalse(Path(newer[0]).exists())
            self.assertEqual(sorted(active.parent.glob("*.log")), sorted([active] + [Path(p) for p in newer[2:]]))


class RunFolderSweepTests(unittest.TestCase):
    # a killed run leaves its own folder (command, reports, receipt) behind: the next run removes those older than a
    # day whose process is gone. A folder names its process; one from before that has no name to check, and only its
    # age counts. A young folder, one whose process runs, and anything else stay
    def test_a_run_removes_old_folders_of_runs_that_ended(self):
        quick = {"test_quick.py": "import unittest\n\n\nclass Quick(unittest.TestCase):\n"
                                  "    def test_ok(self):\n        self.assertEqual(2 + 2, 4)\n"}
        with Env() as e:
            r = e.repo(quick)
            ended = subprocess.Popen([PY, "-c", "pass"])
            ended.wait()
            tmp = e.root / "devtmp"
            old = time.time() - 2 * 86400
            names = {"ended": f"devkit-run-{ended.pid}-a1b2c3", "unnamed": "devkit-run-k3j4h5yz",
                     "running": f"devkit-run-{os.getpid()}-d4e5f6", "young": f"devkit-run-{ended.pid}-g7h8i9",
                     "other": "notes"}
            for key, name in names.items():
                (tmp / name).mkdir(parents=True)
                (tmp / name / "junit.xml").write_text("<testsuites/>\n")
                if key != "young":
                    os.utime(tmp / name, (old, old))
            rc, out, err = dev(r, "run", "--all")
            self.assertEqual(rc, 0, out + err)
            self.assertEqual(sorted(p.name for p in tmp.iterdir()),
                             sorted(names[k] for k in ("running", "young", "other")))


def built_install(r, files):
    """A built (not editable) install of the checkout's package `pkg` in r/.venv, as `uv sync --no-editable` or
    `pip install .` leaves it: the package's files in site-packages, and a dist-info whose direct_url.json names the
    checkout and whose RECORD lists those files. Written by hand, so no installer or network is needed."""
    import venv
    venv.create(r / ".venv", with_pip=False, symlinks=True)
    site = next((r / ".venv" / "lib").glob("python*/site-packages"))
    write(site, files)
    info = site / "pkg-0.dist-info"
    info.mkdir()
    (info / "direct_url.json").write_text(json.dumps({"url": r.as_uri(), "dir_info": {}}))
    (info / "RECORD").write_text("".join(f"{f},sha256=x,1\n" for f in files) + "pkg-0.dist-info/RECORD,,\n")


class ProveTests(unittest.TestCase):
    def test_isolated_proof_ignores_python_customization_and_supports_local_src(self):
        with Env() as e:
            base = dict(PROVE_BASE)
            base["src/calc.py"] = base.pop("calc.py")
            r = e.repo(base)
            (r / "src" / "calc.py").write_text(MUL)
            (r / "tests" / "test_calc.py").write_text(with_tests(TEST_MUL))
            external = e.root / "external"
            external.mkdir()
            marker = e.root / "customization-ran"
            (external / "sitecustomize.py").write_text(f"open({str(marker)!r}, 'w').write('ran')\n")
            (external / "calc.py").write_text("raise RuntimeError('external calc imported')\n")
            with mock.patch.dict(os.environ, {"PYTHONPATH": str(external), "PYTHONHOME": str(external),
                                              "PYTHONUSERBASE": str(external)}):
                rc, out, err = dev(r, "prove", "--isolated-imports", "--runs", "1", "--json")
                self.assertEqual(rc, 0, out + err)
                self.assertTrue(json.loads(out)["prevention_eligible"])
                proof = testx.prevention_proof(r, "tests/test_calc.py::test_calc.CalcTests.test_mul")
                self.assertIsNotNone(proof)
                self.assertFalse(marker.exists())

    def test_isolated_proof_observes_external_imports_even_after_path_and_module_removal(self):
        with Env() as e:
            r = e.repo(PROVE_BASE)
            external = e.root / "external"
            (external / "factor").mkdir(parents=True)
            (external / "factor" / "__init__.py").write_text("VALUE = 1\n")
            (r / "calc.py").write_text(MUL + f"\nimport sys\nsys.path.insert(0, {str(external)!r})\n"
                                      "import factor\nsys.path.pop(0)\ndel sys.modules['factor']\n"
                                      "def mul(a, b): return a * b * factor.VALUE\n")
            (r / "tests" / "test_calc.py").write_text(with_tests(TEST_MUL))
            rc, out, err = dev(r, "prove", "--isolated-imports", "--runs", "1", "--json")
            self.assertEqual(rc, 0, out + err)
            self.assertEqual(json.loads(out)["tests"][0]["verdict"], "proves the change")
            self.assertFalse(json.loads(out)["prevention_eligible"])
            self.assertIsNone(testx.prevention_proof(r, "tests/test_calc.py::test_calc.CalcTests.test_mul"))

    def test_isolated_proof_rejects_an_effective_external_search_path(self):
        with Env() as e:
            r = e.repo(PROVE_BASE)
            external = e.root / "removed-site-directory"
            (r / "calc.py").write_text(MUL + f"\nimport sys\nsys.path.append({str(external)!r})\n")
            (r / "tests" / "test_calc.py").write_text(with_tests(TEST_MUL))
            rc, out, err = dev(r, "prove", "--isolated-imports", "--runs", "1", "--json")
            self.assertEqual(rc, 0, out + err)
            self.assertFalse(json.loads(out)["prevention_eligible"])
            self.assertIsNone(testx.prevention_proof(r, "tests/test_calc.py::test_calc.CalcTests.test_mul"))

    def test_isolated_proof_rejects_external_code_read_and_executed_without_a_filename(self):
        with Env() as e:
            r = e.repo(PROVE_BASE)
            external = e.root / "factor.py"
            external.write_text("FACTOR = 1\n")
            (r / "calc.py").write_text(MUL + "\nfrom pathlib import Path\n"
                                      f"exec(Path({str(external)!r}).read_text())\n"
                                      "def mul(a, b): return a * b * FACTOR\n")
            (r / "tests" / "test_calc.py").write_text(with_tests(TEST_MUL))
            rc, out, err = dev(r, "prove", "--isolated-imports", "--runs", "1", "--json")
            self.assertEqual(rc, 0, out + err)
            self.assertFalse(json.loads(out)["prevention_eligible"])
            self.assertIsNone(testx.prevention_proof(r, "tests/test_calc.py::test_calc.CalcTests.test_mul"))

    def snapshot(self, r):
        return {p: p.read_bytes() for p in Path(r).rglob("*") if p.is_file() and ".git" not in p.parts}

    def test_each_restored_failure_keeps_its_own_message(self):
        with Env() as e:
            r = e.repo({"calc.py": "def value():\n    raise ValueError('second failure marker')\n",
                        "tests/__init__.py": ""})
            (r / "tests" / "test_calc.py").write_text('''
import unittest
import calc
class CalcTests(unittest.TestCase):
    def test_first(self):
        self.assertTrue(hasattr(calc, "fixed"), "first failure marker")

    def test_second(self):
        self.assertEqual(calc.value(), 42)
''')
            (r / "calc.py").write_text("fixed = True\ndef value():\n    return 42\n")
            rc, out, err = dev(r, "prove", "--runs", "1", "--json")
            self.assertEqual(rc, 0, out + err)
            rows = {row["id"].rsplit(".", 1)[-1]: row for row in json.loads(out)["tests"]}
            self.assertIn("first failure marker", rows["test_first"]["message"])
            self.assertIn("ValueError: second failure marker", rows["test_second"]["message"])
            self.assertNotIn("first failure marker", rows["test_second"]["message"])

    # without the change, the old add() writes a line of dashes to stderr, which ends the verbose list for the parser,
    # so test_add_quietly has no result of its own there although it passed. test_mul's failure is no evidence about
    # it: it did not run without the change, as far as prove can tell, and never borrows test_mul's error as proof.
    def test_a_case_without_a_result_of_its_own_never_borrows_another_cases_failure(self):
        with Env() as e:
            r = e.repo(dict(PROVE_BASE, **{"calc.py": "import sys\n\n\ndef add(a, b):\n"
                                                      "    print('\\n' + '-' * 30, file=sys.stderr)\n    return a + b\n"}))
            (r / "calc.py").write_text(MUL)
            (r / "tests" / "test_calc.py").write_text(with_tests(
                TEST_MUL, "\n    def test_add_quietly(self):\n        self.assertEqual(calc.add(1, 2), 3)\n"))
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            rows = {t["id"].rsplit(".", 1)[-1]: t for t in json.loads(out)["tests"]}
            self.assertEqual(rows["test_mul"]["verdict"], "proves the change", out + err)
            quiet = rows["test_add_quietly"]
            self.assertEqual((quiet["without_change"], quiet["verdict"]), ("not run", "did not run without the change"))
            self.assertNotIn("mul", quiet["message"])
            self.assertEqual(rc, 1)

    # a new test class whose setUpClass needs the change fails in its setup without it: no test of the class ran, so
    # the class's failure proves none of them, and they are listed as not proven, never as red
    def test_a_class_setup_failure_without_the_change_proves_none_of_its_tests(self):
        with Env() as e:
            r = e.repo(PROVE_BASE)
            (r / "calc.py").write_text(MUL)
            (r / "tests" / "test_calc.py").write_text(with_tests(TEST_MUL) + textwrap.dedent("""

                class TableTests(unittest.TestCase):
                    @classmethod
                    def setUpClass(cls):
                        cls.six = calc.mul(2, 3)

                    def test_six(self):
                        self.assertEqual(self.six, 6)
                """))
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            self.assertEqual(rc, 0, out + err)
            rows = {t["id"]: t for t in json.loads(out)["tests"]}
            self.assertEqual(rows["test_calc.CalcTests.test_mul"]["verdict"], "proves the change")
            self.assertEqual(rows["test_calc.TableTests.test_six"]["verdict"],
                             "cannot be proven: stops in its class or module setup without the change")
            self.assertIn("has no attribute 'mul'", rows["test_calc.TableTests.test_six"]["message"])

    # a test module whose top level reads a name the change adds raises AttributeError on import without the change.
    # unittest's loader catches only ImportError, so the whole run dies before any test reports: no test of that run
    # ran, so none of them is proof, whatever the error says
    def test_a_unittest_run_that_dies_loading_a_test_module_proves_none_of_its_tests(self):
        with Env() as e:
            r = e.repo(PROVE_BASE)
            write(r, {"calc.py": MUL + "\n\nSIX = mul(2, 3)\n",
                      "tests/test_six.py": """
                          import sys
                          import unittest
                          from pathlib import Path

                          sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
                          import calc  # noqa: E402

                          SIX = calc.SIX


                          class SixTests(unittest.TestCase):
                              def test_six(self):
                                  self.assertEqual(SIX, 6)
                          """})
            (r / "tests" / "test_calc.py").write_text(with_tests(TEST_MUL))
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            self.assertEqual(rc, 2, out + err)
            rows = {t["id"]: t for t in json.loads(out)["tests"]}
            self.assertEqual({i: t["verdict"] for i, t in rows.items()}, {
                "test_calc.CalcTests.test_mul": "cannot be proven: stops at an import without the change",
                "test_six.SixTests.test_six": "cannot be proven: stops at an import without the change"})
            self.assertIn("has no attribute 'SIX'", rows["test_six.SixTests.test_six"]["message"])

    # with pytest, the same module is a collection error of its own file, and the run goes on: that file's test is not
    # proof, whatever the error, and the other new test still proves the change by its own failure
    def test_a_pytest_module_that_fails_to_collect_on_a_missing_name_is_not_proof(self):
        with Env() as e:
            r = e.repo(HOSTS_BASE)
            write(r, {"hosts.py": HOSTS_CHANGE + "\n\nLOCAL = label(None)\n",
                      "tests/test_local.py": "import sys\nfrom pathlib import Path\n\n"
                                             "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
                                             "import hosts  # noqa: E402\n\nLOCAL = hosts.LOCAL\n\n\n"
                                             "def test_local_is_localhost():\n    assert LOCAL == 'localhost'\n"})
            with open(r / "tests" / "test_hosts.py", "a") as fh:
                fh.write("\n\ndef test_count_skips_empty_hosts():\n    assert hosts.count([None, 'a']) == 1\n")
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            self.assertEqual(rc, 0, out + err)
            self.assertEqual([(t["id"], t["without_change"], t["verdict"]) for t in json.loads(out)["tests"]],
                             [("tests/test_hosts.py::test_count_skips_empty_hosts", "failed", "proves the change"),
                              ("tests/test_local.py::test_local_is_localhost", "failed",
                               "cannot be proven: stops at an import without the change")])

    def test_a_test_that_passes_without_the_change_is_reported(self):
        with Env() as e:
            r = e.repo(PROVE_BASE)
            (r / "tests" / "test_calc.py").write_text(with_tests(TEST_MUL, TEST_ADD_AGAIN))
            (r / "calc.py").write_text(MUL)
            before = self.snapshot(r)
            rc, out, err = dev(r, "prove", "--runs", "2")
            self.assertEqual(rc, 1, out + err)
            head, _, tail = out.partition("these tests do not test your change")
            self.assertTrue(tail, out)
            self.assertIn("test_add_again", tail.split("fail without the change")[0])
            self.assertIn("test_mul", out)
            self.assertNotRegex(out, r"test_add\b(?!_)")
            self.assertEqual(self.snapshot(r), before)
            self.assertEqual(list((e.root / "devtmp").iterdir()), [])

    # the copies of proof runs that ended without removing theirs (a crash, a closed terminal) go before the next
    # proof makes its own: those older than a day whose process is gone. A copy names its process; one from before
    # that has no name to check, and only its age counts. A young copy, or one whose process runs, stays.
    def test_prove_removes_old_copies_of_runs_that_ended(self):
        with Env() as e:
            r = e.repo(PROVE_BASE)
            (r / "tests" / "test_calc.py").write_text(with_tests(TEST_MUL))
            (r / "calc.py").write_text(MUL)
            ended = subprocess.Popen([PY, "-c", "pass"])
            ended.wait()
            tmp = e.root / "devtmp"
            old = time.time() - 2 * 86400
            names = {"ended": f"devkit-prove-{ended.pid}-a1b2c3", "unnamed": "devkit-prove-k3j4h5yz",
                     "git": "devkit-prove-git-x9y8z7w6", "running": f"devkit-prove-{os.getpid()}-d4e5f6",
                     "young": f"devkit-prove-{ended.pid}-g7h8i9", "other": "notes"}
            for key, name in names.items():
                (tmp / name / "tests").mkdir(parents=True)
                (tmp / name / "tests" / "test_calc.py").write_text("copied\n")
                if key != "young":
                    os.utime(tmp / name, (old, old))
            rc, out, err = dev(r, "prove", "--runs", "1")
            self.assertEqual(rc, 0, out + err)
            self.assertEqual(sorted(p.name for p in tmp.iterdir()),
                             sorted(names[k] for k in ("running", "young", "other")))
            self.assertIn("removed 3 copies of proof runs that ended", err)

    def test_a_test_that_fails_without_the_change_proves_it(self):
        with Env() as e:
            r = e.repo(PROVE_BASE)
            (r / "tests" / "test_calc.py").write_text(with_tests(TEST_MUL))
            (r / "calc.py").write_text(MUL)
            rc, out, err = dev(r, "prove", "--json")
            self.assertEqual(rc, 0, out + err)
            data = json.loads(out)
            self.assertEqual(data["runs"], 5)
            self.assertEqual([(t["id"], t["with_change"], t["without_change"]) for t in data["tests"]],
                             [("test_calc.CalcTests.test_mul", "5/5", "failed")])

    def test_causal_proof_records_exact_check_and_invalidates_when_source_changes(self):
        with Env() as e:
            r = e.repo(PROVE_BASE)
            (r / "tests" / "test_calc.py").write_text(with_tests(TEST_MUL))
            (r / "calc.py").write_text(MUL)
            rc, out, err = dev(r, "prove", "--isolated-imports", "--runs", "1", "--json")
            self.assertEqual(rc, 0, out + err)
            check = "tests/test_calc.py::test_calc.CalcTests.test_mul"
            proof = testx.prevention_proof(r, check)
            self.assertEqual(proof["without_change"], "failed")
            self.assertEqual(proof["with_change"], "1/1")
            self.assertIsNone(testx.prevention_proof(r, "tests/test_calc.py"))
            with mock.patch.object(sys, "version", sys.version + " changed-runtime"):
                self.assertIsNone(testx.prevention_proof(r, check))
            (r / "calc.py").write_text("def mul(a, b): return 0\n")
            self.assertIsNone(testx.prevention_proof(r, check))

    def test_causal_proof_binds_unchanged_imports_config_and_new_files(self):
        with Env() as e:
            r = e.repo(dict(PROVE_BASE, **{"factor.py": "VALUE = 1\n", "factor.json": "1"}))
            (r / "tests" / "test_calc.py").write_text(with_tests(TEST_MUL))
            (r / "calc.py").write_text(MUL + '\nimport factor, json\nfrom pathlib import Path\n'
                                      'def mul(a, b): return a * b * factor.VALUE * json.loads('
                                      'Path(__file__).with_name("factor.json").read_text())\n')
            rc, out, err = dev(r, "prove", "--isolated-imports", "--runs", "1", "--json")
            self.assertEqual(rc, 0, out + err)
            check = "tests/test_calc.py::test_calc.CalcTests.test_mul"
            self.assertIsNotNone(testx.prevention_proof(r, check))
            for name, changed in (("factor.py", "VALUE = 0\n"), ("factor.json", "0"),
                                  ("new_config.json", "{}")):
                path = r / name
                old = path.read_text() if path.exists() else None
                path.write_text(changed)
                with self.subTest(name=name):
                    self.assertIsNone(testx.prevention_proof(r, check))
                path.write_text(old) if old is not None else path.unlink()
            (r / "factor.py").unlink()
            self.assertIsNone(testx.prevention_proof(r, check))

    def test_checkout_evidence_handles_normal_size_and_refuses_unbounded_or_external_inputs(self):
        with Env() as e:
            r = e.root / "tree"
            r.mkdir()
            git(r, "init", "-q")
            for n in range(750):
                (r / f"module_{n}.py").write_text("VALUE = 1\n")
            snapshot = testx._proof_snapshot(r)
            self.assertEqual(len(snapshot), 750)
            # the bound is work (files and bytes), not wall-clock time: a slow, loaded machine gets the same evidence
            ticks = iter(range(10**6))
            with mock.patch.object(testx.time, "monotonic", side_effect=lambda: float(next(ticks))):
                self.assertEqual(testx._proof_snapshot(r), snapshot)
            for setting, value in (("PROOF_FILES", 749), ("PROOF_BYTES", 100)):
                with self.subTest(setting=setting), mock.patch.object(testx, setting, value):
                    self.assertEqual(testx._proof_snapshot(r), {})
            cache = r / "__pycache__"
            cache.mkdir()
            (cache / "module.pyc").write_bytes(b"cache")
            self.assertEqual(testx._proof_snapshot(r), snapshot)
            dep = r / "vendor"
            dep.mkdir()
            (dep / "external.py").write_text("VALUE = 1")
            self.assertEqual(testx._proof_snapshot(r), {})

    # the evidence names the files the proof copy holds (tracked and untracked, not ignored): ignored build output
    # neither voids it nor is part of it, and a change to a file of the copy still voids it
    def test_ignored_files_the_proof_copy_leaves_out_do_not_void_isolated_import_evidence(self):
        with Env() as e:
            r = e.repo(dict(PROVE_BASE, **{".gitignore": "__pycache__/\ndist/\n"}))
            (r / "dist").mkdir()
            (r / "dist" / "bundle.js").write_text("built output\n")
            (r / "tests" / "test_calc.py").write_text(with_tests(TEST_MUL))
            (r / "calc.py").write_text(MUL)
            rc, out, err = dev(r, "prove", "--isolated-imports", "--runs", "1", "--json")
            self.assertEqual(rc, 0, out + err)
            self.assertTrue(json.loads(out)["prevention_eligible"], out)
            check = "tests/test_calc.py::test_calc.CalcTests.test_mul"
            (r / "dist" / "bundle.js").write_text("built again\n")
            self.assertIsNotNone(testx.prevention_proof(r, check))
            (r / "calc.py").write_text(MUL + "\n# changed\n")
            self.assertIsNone(testx.prevention_proof(r, check))

    def test_runner_label_does_not_attest_a_different_execution_command(self):
        with Env() as e:
            r = e.repo(PROVE_BASE)
            (r / "tests" / "test_calc.py").write_text(with_tests(TEST_MUL))
            (r / "calc.py").write_text(MUL)
            actual = testx.detect

            def wrapped_runner(*args, **kwargs):
                runners = actual(*args, **kwargs)
                for runner in runners:
                    runner["exec"] = [sys.executable, "-W", "ignore", "-m", "unittest"]
                return runners

            with mock.patch.object(testx, "detect", side_effect=wrapped_runner):
                rc, out, err = dev(r, "prove", "--runs", "1", "--json")
            self.assertEqual(rc, 0, out + err)
            self.assertIsNone(testx.prevention_proof(r, "tests/test_calc.py::test_calc.CalcTests.test_mul"))
            with mock.patch.object(testx, "detect", side_effect=wrapped_runner):
                rc, out, err = dev(r, "prove", "--isolated-imports", "--runs", "1", "--json")
            self.assertEqual(rc, 2, out + err)
            self.assertIn("requires direct unittest", out + err)

    def test_a_named_file_proves_only_its_new_tests(self):
        with Env() as e:
            r = e.repo(PROVE_BASE)
            (r / "tests" / "test_calc.py").write_text(with_tests(TEST_MUL))
            (r / "calc.py").write_text(MUL)
            rc, out, err = dev(r, "prove", "--json", "tests/test_calc.py")
            self.assertEqual(rc, 0, out + err)  # test_add existed before and passes without the change: not reported
            self.assertEqual([t["id"] for t in json.loads(out)["tests"]], ["test_calc.CalcTests.test_mul"])

    def test_flaky_with_the_change_is_reported(self):
        with Env() as e:
            counter = e.root / "counter"
            flaky = f"""
    def test_flaky(self):
        f = Path(r"{counter}")
        n = int(f.read_text()) + 1 if f.exists() else 1
        f.write_text(str(n))
        self.assertNotEqual(n, 2)
        self.assertEqual(calc.mul(1, 1), 1)
"""
            r = e.repo(PROVE_BASE)
            (r / "tests" / "test_calc.py").write_text(with_tests(flaky))
            (r / "calc.py").write_text(MUL)
            rc, out, err = dev(r, "prove", "--runs", "3")
            self.assertEqual(rc, 1, out + err)
            self.assertIn("flaky", out)
            self.assertIn("2 of 3", out)

    # a fix in a test helper (a file of the test folders that is no test module: tests/_*.py, conftest.py, fixtures)
    # is part of the change: the new test runs against the old helper too, and fails there
    def test_a_fix_in_a_test_helper_is_proven_against_the_old_helper(self):
        helper_test = """
    def test_clean_drops_x(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import _support
        self.assertEqual(_support.clean({"X": 1, "Y": 2}), {"Y": 2})
"""
        with Env() as e:
            r = e.repo({**PROVE_BASE, "tests/_support.py": "def clean(env):\n    return dict(env)\n"})
            (r / "tests" / "_support.py").write_text(
                "def clean(env):\n    return {k: v for k, v in env.items() if k != 'X'}\n")
            (r / "tests" / "test_calc.py").write_text(with_tests(helper_test))
            rc, out, err = dev(r, "prove", "--json", "--runs", "2")
            self.assertEqual(rc, 0, out + err)
            data = json.loads(out)
            self.assertEqual(data["helpers"], ["tests/_support.py"])
            self.assertEqual([(t["id"], t["without_change"], t["verdict"]) for t in data["tests"]],
                             [("test_calc.CalcTests.test_clean_drops_x", "failed", "proves the change")])

    # the copy prove runs in carries the checkout's git repository: a test that runs git on its own checkout (its
    # files, its origin) sees what it sees in the checkout, with the change and without it
    def test_a_test_that_runs_git_on_its_checkout_runs_in_the_copy_as_in_the_checkout(self):
        git_test = """
    def test_mul_in_a_tracked_file_of_acme(self):
        import subprocess
        root = str(Path(__file__).resolve().parents[1])
        run = lambda *a: subprocess.run(["git", "-C", root, *a], capture_output=True, text=True, check=True)
        self.assertIn("calc.py", run("ls-files").stdout.split())
        self.assertEqual(run("config", "--get", "remote.origin.url").stdout.strip(),
                         "https://github.com/acme/proj.git")
        self.assertEqual(calc.mul(2, 3), 6)
"""
        with Env() as e:
            r = e.repo(PROVE_BASE)
            (r / "tests" / "test_calc.py").write_text(with_tests(git_test))
            (r / "calc.py").write_text(MUL)
            before = self.snapshot(r)
            head = git(r, "rev-parse", "HEAD")
            rc, out, err = dev(r, "prove", "--json", "--runs", "2")
            self.assertEqual(rc, 0, out + err)
            self.assertEqual([(t["with_change"], t["without_change"]) for t in json.loads(out)["tests"]],
                             [("2/2", "failed")])
            self.assertEqual(self.snapshot(r), before)
            self.assertEqual(git(r, "rev-parse", "HEAD"), head)
            self.assertEqual(list((e.root / "devtmp").iterdir()), [])

    # a new test that writes to stderr while it runs still has its result: it proves the change, and is never listed
    # as failing with the change
    def test_a_test_that_writes_to_stderr_while_it_runs_is_proven(self):
        noisy_test = """
    def test_mul_with_a_progress_line(self):
        print("multiplying", file=sys.stderr)
        self.assertEqual(calc.mul(2, 3), 6)
"""
        with Env() as e:
            r = e.repo(PROVE_BASE)
            (r / "tests" / "test_calc.py").write_text(with_tests(noisy_test))
            (r / "calc.py").write_text(MUL)
            rc, out, err = dev(r, "prove", "--json", "--runs", "2")
            self.assertEqual(rc, 0, out + err)
            self.assertEqual([(t["id"], t["with_change"], t["verdict"]) for t in json.loads(out)["tests"]],
                             [("test_calc.CalcTests.test_mul_with_a_progress_line", "2/2", "proves the change")])

    # a new test its runner skips (skipIf, pytest's skipif) ran neither with nor without the change: it is reported
    # as skipped, neither proven nor failing, and the other new tests still prove the change
    def test_a_skipped_test_is_reported_as_skipped_not_as_failing(self):
        skipped_test = """
    @unittest.skipIf(sys.platform != "no-such-platform", "runs only on a platform that does not exist")
    def test_mul_elsewhere(self):
        self.assertEqual(calc.mul(2, 3), 6)
"""
        with Env() as e:
            r = e.repo(PROVE_BASE)
            (r / "tests" / "test_calc.py").write_text(with_tests(TEST_MUL, skipped_test))
            (r / "calc.py").write_text(MUL)
            rc, out, err = dev(r, "prove", "--json", "--runs", "2")
            self.assertEqual(rc, 0, out + err)
            self.assertEqual([(t["id"], t["with_change"], t["verdict"]) for t in json.loads(out)["tests"]],
                             [("test_calc.CalcTests.test_mul", "2/2", "proves the change"),
                              ("test_calc.CalcTests.test_mul_elsewhere", "0/2", "skipped")])
            rc, out, err = dev(r, "prove", "--runs", "1")
            self.assertEqual(rc, 0, out + err)
            self.assertNotIn("fail with your change present", out)
            self.assertRegex(out, r"skipped[^\n]*:\n\s+tests/test_calc.py\s+test_calc.CalcTests.test_mul_elsewhere")

    # a fix in a helper defined inside a test module (outside every test) is part of the change, as a fix in a
    # helper file is: without the change the module's tests run against the helper's old text, here the only change
    def test_a_fix_in_a_helper_inside_a_test_module_is_proven_against_the_old_helper(self):
        with Env() as e:
            r = e.repo(SANDBOX_BASE)
            text = (r / "tests" / "test_calc.py").read_text()
            (r / "tests" / "test_calc.py").write_text(
                text.replace("    return dict(env)\n", "    return {k: v for k, v in env.items() if k != 'X'}\n") +
                "\n    def test_sandbox_drops_x(self):\n"
                "        self.assertEqual(sandbox_env({'X': 1, 'Y': 2}), {'Y': 2})\n")
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            self.assertEqual(rc, 0, out + err)
            data = json.loads(out)
            self.assertEqual(data["test_module_helpers"], ["tests/test_calc.py"])
            self.assertEqual([(t["id"], t["without_change"], t["verdict"]) for t in data["tests"]],
                             [("test_calc.CalcTests.test_sandbox_drops_x", "failed", "proves the change")])
            self.assertIn("{'Y': 2}", data["tests"][0]["message"])

    # a line the change adds inside a helper that was there before is part of that helper's change too
    def test_a_line_added_inside_a_helper_is_undone_with_the_change(self):
        with Env() as e:
            r = e.repo(SANDBOX_BASE)
            text = (r / "tests" / "test_calc.py").read_text()
            (r / "tests" / "test_calc.py").write_text(
                text.replace("    return dict(env)\n",
                             "    env = {k: v for k, v in env.items() if k != 'X'}\n    return dict(env)\n") +
                "\n    def test_sandbox_drops_x(self):\n"
                "        self.assertEqual(sandbox_env({'X': 1, 'Y': 2}), {'Y': 2})\n")
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            self.assertEqual(rc, 0, out + err)
            data = json.loads(out)
            self.assertEqual(data["test_module_helpers"], ["tests/test_calc.py"])
            self.assertEqual([(t["id"], t["without_change"], t["verdict"]) for t in data["tests"]],
                             [("test_calc.CalcTests.test_sandbox_drops_x", "failed", "proves the change")])

    # a helper of a test class that gains a parameter and a body line that reads it comes back whole without the
    # change: restored by halves, its old signature ran the new body, and a test the change does not touch crashed on
    # a NameError that prove took for proof. A test that calls a helper whose parameters the change changed cannot be
    # proven: without the change, a call with the new parameters fails on the call, whatever the change does.
    def test_a_helper_comes_back_whole_so_a_test_the_change_does_not_touch_runs_cleanly(self):
        base = dict(PROVE_BASE, **{"tests/test_calc.py": with_tests().replace(
            "    def test_add(self):\n        self.assertEqual(calc.add(1, 2), 3)\n",
            "    def pair(self):\n        a, b = 1, 2\n        return a, b\n\n"
            "    def test_add(self):\n        self.assertEqual(calc.add(*self.pair()), 3)\n")})
        with Env() as e:
            r = e.repo(base)
            text = (r / "tests" / "test_calc.py").read_text()
            (r / "tests" / "test_calc.py").write_text(
                text.replace("    def pair(self):\n        a, b = 1, 2\n",
                             "    def pair(self, scale=1):\n        a, b = 1, 2\n        a, b = a * scale, b * scale\n")
                + "\n    def test_mul(self):\n        self.assertEqual(calc.mul(*self.pair(2)), 8)\n")
            (r / "calc.py").write_text(MUL)
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            self.assertNotIn("NameError", out + err)
            self.assertEqual([(t["id"], t["without_change"], t["verdict"]) for t in json.loads(out)["tests"]],
                             [("test_calc.CalcTests.test_add", "passed", testx._HELPERS_TANGLED),
                              ("test_calc.CalcTests.test_mul", "failed", testx._HELPERS_TANGLED)], out + err)

    # a test helper the change adds is test code, not the change under test: it stays in the copy, so the new test
    # runs against the old production code. Removed, every test that imports it failed on the import and was
    # reported as proving the change, here a test the change does not touch at all.
    def test_a_new_test_helper_stays_so_a_test_is_judged_on_its_assertion(self):
        pair_test = """
    def test_add_a_pair(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import _pairs
        self.assertEqual(calc.add(*_pairs.one_and_two()), 3)
"""
        with Env() as e:
            r = e.repo(PROVE_BASE)
            write(r, {"tests/_pairs.py": "def one_and_two():\n    return 1, 2\n", "calc.py": MUL})
            (r / "tests" / "test_calc.py").write_text(with_tests(TEST_MUL, pair_test))
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            self.assertEqual(rc, 1, out + err)
            data = json.loads(out)
            self.assertEqual(data["helpers"], ["tests/_pairs.py"])
            self.assertEqual([(t["id"], t["verdict"]) for t in data["tests"]],
                             [("test_calc.CalcTests.test_add_a_pair", "does not test the change"),
                              ("test_calc.CalcTests.test_mul", "proves the change")])

    # when a new test helper is the whole change, it goes with the change, and a test that imports it cannot be
    # proven: exit 2, nothing proven, never 0
    def test_a_new_helper_that_is_the_whole_change_cannot_be_proven(self):
        pair_test = """
    def test_add_a_pair(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import _pairs
        self.assertEqual(calc.add(*_pairs.one_and_two()), 3)
"""
        with Env() as e:
            r = e.repo(PROVE_BASE)
            write(r, {"tests/_pairs.py": "def one_and_two():\n    return 1, 2\n"})
            (r / "tests" / "test_calc.py").write_text(with_tests(pair_test))
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            self.assertEqual(rc, 2, out + err)
            self.assertEqual([(t["id"], t["verdict"]) for t in json.loads(out)["tests"]],
                             [("test_calc.CalcTests.test_add_a_pair", "cannot be proven: imports a file the change adds")])

    # a test that imports a module the change adds cannot run without the change: its import fails before any
    # assertion, which proves nothing about the test. It is reported as such, neither as proof nor as a failure.
    def test_a_test_of_a_new_module_cannot_be_proven(self):
        with Env() as e:
            r = e.repo(PROVE_BASE)
            write(r, {"calc.py": MUL, "stats.py": "def mean(xs):\n    return sum(xs) / len(xs)\n",
                      "tests/test_stats.py": """
                          import sys
                          import unittest
                          from pathlib import Path

                          sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
                          import stats  # noqa: E402


                          class StatsTests(unittest.TestCase):
                              def test_mean(self):
                                  self.assertEqual(stats.mean([1, 2, 3]), 2)
                          """})
            (r / "tests" / "test_calc.py").write_text(with_tests(TEST_MUL))
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            self.assertEqual(rc, 0, out + err)
            self.assertEqual([(t["id"], t["verdict"]) for t in json.loads(out)["tests"]],
                             [("test_calc.CalcTests.test_mul", "proves the change"),
                              ("test_stats.StatsTests.test_mean", "cannot be proven: imports a file the change adds")])
            rc, out, err = dev(r, "prove", "--runs", "1", "tests/test_stats.py")
            self.assertEqual(rc, 2, out + err)
            self.assertRegex(out, r"cannot be proven: (they )?import a file the change adds[^\n]*\n\s+tests/test_stats")
            self.assertNotIn("fail without the change, as expected", out)

    # with pytest, a test module that imports a file the change adds is a collection error without the change: it
    # cannot be proven, and the other new tests of the same run still run and prove the change (pytest stops the
    # session at a collection error unless it is told to go on)
    def test_a_pytest_module_of_a_new_file_cannot_be_proven_and_the_rest_still_runs(self):
        with Env() as e:
            r = e.repo(HOSTS_BASE)
            write(r, {"hosts.py": HOSTS_CHANGE, "ports.py": "def default():\n    return 443\n",
                      "tests/test_ports.py": "import sys\nfrom pathlib import Path\n\n"
                                             "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
                                             "import ports  # noqa: E402\n\n\ndef test_default_is_https():\n"
                                             "    assert ports.default() == 443\n"})
            with open(r / "tests" / "test_hosts.py", "a") as fh:
                fh.write("\n\ndef test_count_skips_empty_hosts():\n    assert hosts.count([None, 'a']) == 1\n")
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            self.assertEqual(rc, 0, out + err)
            self.assertEqual([(t["id"], t["without_change"], t["verdict"]) for t in json.loads(out)["tests"]],
                             [("tests/test_hosts.py::test_count_skips_empty_hosts", "failed", "proves the change"),
                              ("tests/test_ports.py::test_default_is_https", "failed",
                               "cannot be proven: imports a file the change adds")])

    # a new test that imports a name the change adds to an existing file (from calc import mul) stops at that import
    # without the change, before any assertion: not proven, with the import error as the reason. A test whose
    # assertion is about an import error (ImportError not raised) reached its assertion, so it still proves.
    def test_a_test_that_stops_at_an_import_without_the_change_is_not_proof(self):
        base = dict(PROVE_BASE, **{"calc.py": "def add(a, b):\n    return a + b\n\n\ndef need(name):\n    return None\n"})
        with Env() as e:
            r = e.repo(base)
            write(r, {"calc.py": "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n\n\n"
                                 "def need(name):\n    raise ImportError(name)\n",
                      "tests/test_mul.py": """
                          import sys
                          import unittest
                          from pathlib import Path

                          sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
                          from calc import mul  # noqa: E402


                          class MulTests(unittest.TestCase):
                              def test_mul_of_two_and_three_is_six(self):
                                  self.assertEqual(mul(2, 3), 6)
                          """})
            (r / "tests" / "test_calc.py").write_text(with_tests(
                "\n    def test_need_refuses_a_missing_name(self):\n"
                "        with self.assertRaises(ImportError):\n            calc.need('x')\n"))
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            self.assertEqual(rc, 0, out + err)
            rows = {t["id"]: t for t in json.loads(out)["tests"]}
            self.assertEqual(rows["test_calc.CalcTests.test_need_refuses_a_missing_name"]["verdict"],
                             "proves the change")
            mul = rows["test_mul.MulTests.test_mul_of_two_and_three_is_six"]
            self.assertEqual(mul["verdict"], "cannot be proven: stops at an import without the change")
            self.assertIn("cannot import name 'mul'", mul["message"])
            rc, out, err = dev(r, "prove", "--runs", "1", "tests/test_mul.py")
            self.assertEqual(rc, 2, out + err)
            self.assertRegex(out, r"stop at an import or collection error[^\n]*\n\s+tests/test_mul.py\s+\S+\s+· ImportError: cannot import")
            self.assertNotIn("fail without the change, as expected", out)

    # a failure whose message only mentions an import word ("Failed to load the settings file") reached the behavior
    # it tests, so it proves the change: only a message that opens with an import or collection error stops at one
    def test_a_real_failure_whose_message_mentions_failed_to_load_proves_the_change(self):
        base = dict(PROVE_BASE, **{"calc.py": "def add(a, b):\n    return a + b\n\n\ndef settings():\n"
                                              "    raise RuntimeError('Failed to load the settings file')\n"})
        with Env() as e:
            r = e.repo(base)
            write(r, {"calc.py": "def add(a, b):\n    return a + b\n\n\ndef settings():\n    return {'port': 443}\n"})
            (r / "tests" / "test_calc.py").write_text(with_tests(
                "\n    def test_the_settings_name_the_https_port(self):\n"
                "        self.assertEqual(calc.settings()['port'], 443)\n"))
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            self.assertEqual(rc, 0, out + err)
            row = {t["id"]: t for t in json.loads(out)["tests"]}["test_calc.CalcTests.test_the_settings_name_the_https_port"]
            self.assertEqual(row["verdict"], "proves the change")
            self.assertIn("Failed to load the settings file", row["message"])

    # with pytest, a module that imports a name the change adds is a collection error without the change: not proof
    def test_a_pytest_collection_error_without_the_change_is_not_proof(self):
        with Env() as e:
            r = e.repo(HOSTS_BASE)
            write(r, {"hosts.py": HOSTS_CHANGE,
                      "tests/test_count.py": "import sys\nfrom pathlib import Path\n\n"
                                             "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
                                             "from hosts import count  # noqa: E402\n\n\n"
                                             "def test_count_skips_empty_hosts():\n"
                                             "    assert count([None, 'a']) == 1\n"})
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            self.assertEqual(rc, 2, out + err)
            self.assertEqual([(t["id"], t["without_change"], t["verdict"]) for t in json.loads(out)["tests"]],
                             [("tests/test_count.py::test_count_skips_empty_hosts", "failed",
                               "cannot be proven: stops at an import without the change")])

    # when every new test was skipped, nothing is proven: exit 2, as when no runner here can run them, never 0
    def test_a_change_whose_only_new_test_is_skipped_proves_nothing(self):
        with Env() as e:
            r = e.repo(PROVE_BASE)
            (r / "tests" / "test_calc.py").write_text(with_tests(
                "\n    @unittest.skip('not on this machine')\n    def test_mul(self):\n"
                "        self.assertEqual(calc.mul(2, 3), 6)\n"))
            (r / "calc.py").write_text(MUL)
            rc, out, err = dev(r, "prove", "--runs", "1")
            self.assertEqual(rc, 2, out + err)
            self.assertIn("nothing proven", out)
            self.assertNotIn("fail with your change present", out)

    # A fake defined in a test module (a plain class, no TestCase, a name pytest does not collect) may have a method
    # named test_…; no runner collects it, so it is no changed test: selecting it matched nothing and failed the proof
    def test_a_fake_classs_test_method_is_not_a_changed_test(self):
        with Env() as e:
            r = e.repo(PROVE_BASE)
            (r / "tests" / "test_calc.py").write_text(with_tests(TEST_MUL) + textwrap.dedent("""


                class FakeProbe:
                    def test_connection(self):
                        return calc.add(1, 1) == 2


                class FakeDeepProbe(FakeProbe, object):
                    def test_route(self):
                        return True
                """))
            (r / "calc.py").write_text(MUL)
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            self.assertEqual([(t["id"], t["verdict"]) for t in json.loads(out)["tests"]],
                             [("test_calc.CalcTests.test_mul", "proves the change")], out + err)
            self.assertEqual(rc, 0)

    # A production module named test_… outside pytest's testpaths is production code: prove undoes it and proves the
    # tests that import it, instead of taking it for a test and refusing that only tests changed
    def test_a_module_outside_pytest_testpaths_is_proven_as_production_code(self):
        with Env() as e:
            r = e.repo({"pytest.ini": "[pytest]\ntestpaths = tests\n", "pytest.py": STANDIN_PYTEST,
                        "diag/__init__.py": "", "diag/test_link.py": "def test_link(host):\n    return host\n",
                        "tests/test_diag.py": "import sys\nfrom pathlib import Path\n\n"
                                              "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
                                              "from diag import test_link  # noqa: E402\n\n\n"
                                              "def test_link_keeps_the_host():\n    assert test_link.test_link('a') == 'a'\n",
                        ".gitignore": "__pycache__/\n"})
            (r / "diag" / "test_link.py").write_text("def test_link(host):\n    return host.lower()\n")
            (r / "tests" / "test_diag.py").write_text((r / "tests" / "test_diag.py").read_text() +
                                                      "\n\ndef test_link_lowers_the_host():\n"
                                                      "    assert test_link.test_link('A') == 'a'\n")
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            data = json.loads(out or "{}")
            self.assertEqual((data.get("production"), [(t["id"], t["verdict"]) for t in data.get("tests", [])]),
                             (["diag/test_link.py"],
                              [("tests/test_diag.py::test_link_lowers_the_host", "proves the change")]), out + err)
            self.assertEqual(rc, 0)

    # A new Playwright spec is proven with Playwright: vitest, the package's unit runner, excluded it and ran 0 tests
    @unittest.skipUnless(shutil.which("node") and shutil.which("npx"), "node is not installed, so the stand-in cannot run")
    def test_a_playwright_spec_is_proven_with_playwright(self):
        with Env() as e:
            r = e.repo({"package.json": json.dumps({"scripts": {"test": "vitest"},
                                                    "devDependencies": {"vitest": "5", "@playwright/test": "1"}}),
                        "playwright.config.mjs": "export default { testDir: './e2e' };\n",
                        "src/sum.mjs": "export function sum(a, b) {\n  return a - b;\n}\n",
                        ".gitignore": "node_modules/\n"})
            standin_js(r)
            write(r, {"src/sum.mjs": "export function sum(a, b) {\n  return a + b;\n}\n",
                      "e2e/sum.spec.mjs": """
                          import { expect, test } from '@playwright/test';
                          import { sum } from '../src/sum.mjs';

                          test.describe('sum', () => {
                            test('adds two numbers', () => {
                              expect(sum(1, 2)).toBe(3);
                            });
                          });
                          """})
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            data = json.loads(out or "{}")
            self.assertEqual([(t["id"], t["with_change"], t["without_change"], t["verdict"]) for t in data.get("tests", [])],
                             [("e2e/sum.spec.mjs::sum › adds two numbers", "1/1", "failed", "proves the change")],
                             out + err)
            self.assertEqual(rc, 0)

    # A new unit test of an Angular project built by the unit-test builder is proven through `ng test`
    @unittest.skipUnless(shutil.which("node") and shutil.which("npx"), "node is not installed, so the stand-in cannot run")
    def test_an_angular_unit_test_is_proven_through_ng_test(self):
        with Env() as e:
            r = e.repo({"package.json": json.dumps({"scripts": {"test": "ng test"},
                                                    "devDependencies": {"vitest": "5", "@angular/build": "22"}}),
                        "angular.json": json.dumps({"version": 1, "projects": {"app": {
                            "root": "", "sourceRoot": "src",  # the stand-in runs plain JavaScript specs
                            "architect": {"test": {"builder": "@angular/build:unit-test",
                                                   "options": {"include": ["**/*.spec.mjs"]}}}}}}),
                        "src/app/sum.mjs": "export function sum(a, b) {\n  return a - b;\n}\n",
                        ".gitignore": "node_modules/\n"})
            standin_js(r)
            write(r, {"src/app/sum.mjs": "export function sum(a, b) {\n  return a + b;\n}\n",
                      "src/app/sum.spec.mjs": """
                          import { sum } from './sum.mjs';

                          describe('sum', () => {
                            it('adds two numbers', () => {
                              expect(sum(1, 2)).toBe(3);
                            });
                          });
                          """})
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            data = json.loads(out or "{}")
            self.assertEqual([(t["id"], t["with_change"], t["without_change"], t["verdict"]) for t in data.get("tests", [])],
                             [("src/app/sum.spec.mjs::sum adds two numbers", "1/1", "failed", "proves the change")],
                             out + err)
            self.assertEqual(rc, 0)

    # `dev test --since REF prove`: the base named before the subcommand is the base of the proof, as after it
    def test_since_before_the_subcommand_sets_the_base_of_the_proof(self):
        with Env() as e:
            r = e.repo(PROVE_BASE)
            (r / "calc.py").write_text(MUL)
            (r / "tests" / "test_calc.py").write_text(with_tests(TEST_MUL))
            git(r, "commit", "-qam", "feat: mul")
            (r / "calc.py").write_text(MUL + "\n\ndef div(a, b):\n    return a / b\n")
            (r / "tests" / "test_calc.py").write_text(with_tests(
                TEST_MUL, "\n    def test_div(self):\n        self.assertEqual(calc.div(6, 3), 2)\n"))
            rc, out, err = dev(r, "--since", "HEAD", "prove", "--json", "--runs", "1")
            data = json.loads(out)
            self.assertEqual((data["base"], [t["id"] for t in data["tests"]]),
                             ("HEAD", ["test_calc.CalcTests.test_div"]), out + err)
            self.assertEqual(rc, 0)

    def test_needs_a_production_change_and_a_changed_test(self):
        with Env() as e:
            r = e.repo(PROVE_BASE)
            (r / "tests" / "test_calc.py").write_text(with_tests(TEST_ADD_AGAIN))
            rc, _, err = dev(r, "prove")
            self.assertEqual(rc, 2)
            self.assertIn("no production change", err)
            git(r, "checkout", "--", "tests/test_calc.py")
            (r / "calc.py").write_text(MUL)
            rc, _, err = dev(r, "prove")
            self.assertEqual(rc, 2)
            self.assertIn("no new or changed tests", err)

    # a parametrized test runs as one case per value: each case is reported by its id with its own result, and the
    # function's bare name, which never runs as such, is no row of its own
    def test_each_case_of_a_parametrized_test_has_its_own_result(self):
        with Env() as e:
            r = e.repo(HOSTS_BASE)
            write(r, {"hosts.py": HOSTS_CHANGE})
            with open(r / "tests" / "test_hosts.py", "a") as fh:
                fh.write(HOSTS_TESTS)
            rc, out, err = dev(r, "prove", "--since", "HEAD", "--json", "--runs", "2")
            self.assertEqual(rc, 1, out + err)  # the "a" and "b" cases pass without the change
            rows = {t["id"]: t for t in json.loads(out)["tests"]}
            name = "tests/test_hosts.py::test_label_is_never_empty"
            self.assertEqual({i: (t["with_change"], t["without_change"], t["verdict"]) for i, t in rows.items()}, {
                "tests/test_hosts.py::test_count_skips_empty_hosts": ("2/2", "failed", "proves the change"),
                f"{name}[None]": ("2/2", "failed", "proves the change"),
                f"{name}[a]": ("2/2", "passed", "does not test the change"),
                f"{name}[b]": ("2/2", "passed", "does not test the change")})
            self.assertIn("has no attribute 'count'", rows["tests/test_hosts.py::test_count_skips_empty_hosts"]["message"])
            self.assertNotIn("count", rows[f"{name}[None]"]["message"])

    # a changed test no runner here can run (a node test file in a project without a node runner) is listed and
    # left out; the other tests are proven, whether the files are found or named
    def test_a_test_no_runner_here_can_run_is_listed_and_the_rest_is_proven(self):
        with Env() as e:
            r = e.repo(PROVE_BASE)
            write(r, {"scripts/test/docs.test.mjs": 'import { test } from "node:test";\ntest("reads", () => {});\n'})
            (r / "tests" / "test_calc.py").write_text(with_tests(TEST_MUL))
            (r / "calc.py").write_text(MUL)
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            self.assertEqual(rc, 0, out + err)
            data = json.loads(out)
            self.assertEqual([t["id"] for t in data["tests"]], ["test_calc.CalcTests.test_mul"])
            self.assertEqual([s["file"] for s in data["skipped"]], ["scripts/test/docs.test.mjs"])
            rc, out, err = dev(r, "prove", "--runs", "1", "scripts/test/docs.test.mjs", "tests/test_calc.py")
            self.assertEqual(rc, 0, out + err)
            listed = out.split("not proven")[1].split("fail without the change")[0] if "not proven" in out else ""
            self.assertIn("scripts/test/docs.test.mjs", listed, out + err)
            self.assertIn("test_calc.CalcTests.test_mul", out.split("fail without the change")[-1])
            rc, _, err = dev(r, "prove", "scripts/test/docs.test.mjs")
            self.assertEqual(rc, 2)
            self.assertIn("scripts/test/docs.test.mjs", err)

    # node --test files are proven with node: a node:test case by its title, a plain test script as a whole; a
    # script the other tests import is a helper, part of the change, and restored with it
    @unittest.skipUnless(shutil.which("node"), "node is not installed, so the node runner cannot run here")
    def test_node_test_files_are_proven_with_node(self):
        with Env() as e:
            r = e.repo({**PROVE_BASE, **NODE_BASE})
            write(r, NODE_CHANGE)
            (r / "tests" / "test_calc.py").write_text(with_tests(TEST_MUL))
            (r / "calc.py").write_text(MUL)
            rc, out, err = dev(r, "prove", "--json", "--runs", "2")
            self.assertEqual(rc, 0, out + err)
            data = json.loads(out)
            self.assertEqual(data["helpers"], ["test/harness.mjs"])
            self.assertEqual([(t["id"], t["with_change"], t["without_change"]) for t in data["tests"]], [
                ("test/page.mjs", "2/2", "failed"),
                ("test/slug.test.mjs::joins words with dashes", "2/2", "failed"),
                ("test_calc.CalcTests.test_mul", "2/2", "failed")])
            rc, out, err = dev(r, "prove", "--json", "--runs", "1", "test/page.mjs")
            self.assertEqual(rc, 0, out + err)
            self.assertEqual([t["id"] for t in json.loads(out)["tests"]], ["test/page.mjs"])
            # --runner leaves the node tests out: they are listed, and the plain script is no helper for that
            rc, out, err = dev(r, "prove", "--json", "--runs", "1", "--runner", "unittest")
            self.assertEqual(rc, 0, out + err)
            data = json.loads(out)
            self.assertEqual(data["helpers"], ["test/harness.mjs"])
            self.assertEqual([s["file"] for s in data["skipped"]], ["test/page.mjs", "test/slug.test.mjs"])

    # A src layout installed in editable mode: the virtual environment's .pth names the checkout's src folder, and
    # the copy links that environment. The copy's Python must import the copy's src, where the change is undone;
    # otherwise the old code is never run, and a test that proves the change is reported as not testing it. The .pth
    # is written by hand, as an editable install writes it, so no installer or network is needed.
    def test_an_editable_src_layout_is_proven_against_the_copys_own_source(self):
        import venv
        with Env() as e:
            r = e.repo({"pyproject.toml": '[project]\nname = "pkg"\nversion = "0"\n',
                        "src/pkg/__init__.py": "", "src/pkg/mod.py": "def value():\n    return 1\n",
                        "tests/__init__.py": "",
                        "tests/test_mod.py": "import unittest\n\nfrom pkg import mod\n\n\nclass ModTests(unittest.TestCase):\n"
                                             "    def test_one(self):\n        self.assertEqual(mod.value() % 1, 0)\n",
                        ".gitignore": "__pycache__/\n.venv/\n"})
            venv.create(r / ".venv", with_pip=False, symlinks=True)
            site = next((r / ".venv" / "lib").glob("python*/site-packages"))
            (site / "_pkg_editable.pth").write_text(f"{r / 'src'}\n")
            (r / "src" / "pkg" / "mod.py").write_text("def value():\n    return 2\n")
            (r / "tests" / "test_mod.py").write_text((r / "tests" / "test_mod.py").read_text() +
                                                     "\n    def test_two(self):\n        self.assertEqual(mod.value(), 2)\n")
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            self.assertEqual(rc, 0, out + err)
            self.assertEqual([(t["id"], t["with_change"], t["without_change"]) for t in json.loads(out)["tests"]],
                             [("tests.test_mod.ModTests.test_two", "1/1", "failed")])

    # setuptools installs a package whose folder has another name (package-dir: pkg from src/pkg_impl) through an
    # import hook: the .pth line runs `import __editable___…_finder`, whose MAPPING names the checkout's folder. The
    # copy's Python must still import the copy's folder under the package's name, where the change is undone. The
    # finder is written by hand as setuptools writes it, so no installer or network is needed.
    def test_an_editable_install_that_maps_a_package_to_another_folder_is_proven_against_the_copy(self):
        import venv
        with Env() as e:
            r = e.repo({"pyproject.toml": '[project]\nname = "pkg"\nversion = "0"\n',
                        "src/pkg_impl/__init__.py": "", "src/pkg_impl/mod.py": "def value():\n    return 1\n",
                        "tests/__init__.py": "",
                        "tests/test_mod.py": "import unittest\n\nfrom pkg import mod\n\n\nclass ModTests(unittest.TestCase):\n"
                                             "    def test_one(self):\n        self.assertEqual(mod.value() % 1, 0)\n",
                        ".gitignore": "__pycache__/\n.venv/\n"})
            venv.create(r / ".venv", with_pip=False, symlinks=True)
            site = next((r / ".venv" / "lib").glob("python*/site-packages"))
            (site / "__editable___pkg_0_finder.py").write_text(textwrap.dedent(f"""
                import importlib.util
                import sys

                MAPPING: dict = {{'pkg': {str(r / 'src' / 'pkg_impl')!r}}}


                class _EditableFinder:
                    @classmethod
                    def find_spec(cls, fullname, path=None, target=None):
                        if fullname not in MAPPING:
                            return None
                        root = MAPPING[fullname]
                        return importlib.util.spec_from_file_location(
                            fullname, root + "/__init__.py", submodule_search_locations=[root])


                def install():
                    if _EditableFinder not in sys.meta_path:
                        sys.meta_path.append(_EditableFinder)
                """))
            (site / "__editable__.pkg-0.pth").write_text(
                "import __editable___pkg_0_finder; __editable___pkg_0_finder.install()\n")
            (r / "src" / "pkg_impl" / "mod.py").write_text("def value():\n    return 2\n")
            (r / "tests" / "test_mod.py").write_text((r / "tests" / "test_mod.py").read_text() +
                                                     "\n    def test_two(self):\n        self.assertEqual(mod.value(), 2)\n")
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            self.assertEqual(rc, 0, out + err)
            self.assertEqual([(t["id"], t["with_change"], t["without_change"]) for t in json.loads(out)["tests"]],
                             [("tests.test_mod.ModTests.test_two", "1/1", "failed")])
            self.assertEqual(list((e.root / "devtmp").iterdir()), [])

    # A src layout installed as a built copy: site-packages holds the package built from the changed source, and
    # nothing leads the copy's Python back to a source folder, so without care the restored code is never run and a
    # test that proves the change is reported as not testing it. The copy's own src must come first.
    def test_a_built_install_of_the_checkout_is_proven_against_the_copys_own_source(self):
        with Env() as e:
            r = e.repo({"pyproject.toml": '[project]\nname = "pkg"\nversion = "0"\n',
                        "src/pkg/__init__.py": "", "src/pkg/mod.py": "def value():\n    return 1\n",
                        "tests/__init__.py": "",
                        "tests/test_mod.py": "import unittest\n\nfrom pkg import mod\n\n\nclass ModTests(unittest.TestCase):\n"
                                             "    def test_one(self):\n        self.assertEqual(mod.value() % 1, 0)\n",
                        ".gitignore": "__pycache__/\n.venv/\n"})
            changed = "def value():\n    return 2\n"
            built_install(r, {"pkg/__init__.py": "", "pkg/mod.py": changed})
            (r / "src" / "pkg" / "mod.py").write_text(changed)
            (r / "tests" / "test_mod.py").write_text((r / "tests" / "test_mod.py").read_text() +
                                                     "\n    def test_two(self):\n        self.assertEqual(mod.value(), 2)\n")
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            self.assertEqual(rc, 0, out + err)
            data = json.loads(out)
            self.assertEqual([(t["id"], t["with_change"], t["without_change"]) for t in data["tests"]],
                             [("tests.test_mod.ModTests.test_two", "1/1", "failed")])
            self.assertNotIn("built_installs", data)

    # A built copy whose package the checkout keeps under another name (here pkg is built from src/pkg_impl): the copy
    # has no folder to put first, so prove cannot run the restored code. It says so, names the package, and says how
    # to make it provable, instead of only calling the test one that does not test the change.
    def test_a_built_install_the_copy_cannot_replace_is_named_with_the_fix(self):
        with Env() as e:
            r = e.repo({"pyproject.toml": '[project]\nname = "pkg"\nversion = "0"\n',
                        "src/pkg_impl/__init__.py": "", "src/pkg_impl/mod.py": "def value():\n    return 1\n",
                        "tests/__init__.py": "",
                        "tests/test_mod.py": "import unittest\n\nfrom pkg import mod\n\n\nclass ModTests(unittest.TestCase):\n"
                                             "    def test_one(self):\n        self.assertEqual(mod.value() % 1, 0)\n",
                        ".gitignore": "__pycache__/\n.venv/\n"})
            changed = "def value():\n    return 2\n"
            built_install(r, {"pkg/__init__.py": "", "pkg/mod.py": changed})
            (r / "src" / "pkg_impl" / "mod.py").write_text(changed)
            (r / "tests" / "test_mod.py").write_text((r / "tests" / "test_mod.py").read_text() +
                                                     "\n    def test_two(self):\n        self.assertEqual(mod.value(), 2)\n")
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            self.assertEqual(json.loads(out)["built_installs"], [{"package": "pkg", "environment": ".venv"}])
            rc, out, err = dev(r, "prove", "--runs", "1")
            self.assertEqual(rc, 1, out + err)
            self.assertIn("built copy of pkg", out)
            self.assertIn("editable", out)

    # a branch cut from a release branch that main does not have yet: its change is what it adds to the release
    # branch, so the release's own tests are not its tests and the release's code stays in the copy without the change
    def test_a_branch_cut_from_an_unmerged_release_branch_is_proven_against_that_branch(self):
        with Env() as e:
            r = e.repo(PROVE_BASE, branch="release/1.1")
            (r / "calc.py").write_text(MUL)
            (r / "tests" / "test_calc.py").write_text(with_tests(TEST_MUL))
            git(r, "commit", "-qam", "feat: mul")
            git(r, "push", "-q", "-u", "origin", "release/1.1")
            git(r, "switch", "-q", "-c", "fix/div")
            (r / "calc.py").write_text(MUL + "\n\ndef div(a, b):\n    return a / b\n")
            (r / "tests" / "test_calc.py").write_text(with_tests(
                TEST_MUL, "\n    def test_div(self):\n        self.assertEqual(calc.div(6, 3), 2)\n"))
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            got = json.loads(out)
            self.assertEqual(got["base"], "release/1.1", out + err)
            self.assertEqual([(t["id"].rsplit(".", 1)[-1], t["verdict"]) for t in got["tests"]],
                             [("test_div", "proves the change")])
            self.assertEqual(rc, 0)



# --------------------------------------------------------------------------- dev ci --run --resume


class RecognizeTests(unittest.TestCase):
    def test_plain_test_runner_commands_are_recognized(self):
        with Env() as e:
            r = e.repo({"package.json": json.dumps({"scripts": {"test": "jest --ci"}}), "pnpm-lock.yaml": "",
                        "pom.xml": "<project/>", "go.mod": "module example.com/x\n", "tests/test_a.py": ""},
                       branch=None)
            top = str(r)
            cases = {
                "python -m unittest discover -s tests": ("unittest", False),
                "pytest -q": ("pytest", False),
                "python -m pytest -x tests": ("pytest", True),
                "npm test": ("jest", False),
                "./mvnw -B verify": ("maven", False),
                "go test -race ./...": ("go", False),
                "vendor/bin/phpunit": ("phpunit", False),
            }
            for cmd, (name, failfast) in cases.items():
                with self.subTest(cmd=cmd):
                    rn = testx.recognize(cmd, top, top)
                    self.assertEqual((rn["name"], rn["failfast"]), (name, failfast))
            self.assertEqual(testx.recognize("python -m unittest discover -s tests", top, top)["start"], "tests")
            self.assertEqual(testx.recognize("./mvnw -B verify", top, top)["argv"], ["./mvnw", "-B", "verify"])
            for cmd in ("npm ci && npm test", "make test", "pytest -k slow", "echo hi", "pytest | tee log"):
                with self.subTest(cmd=cmd):
                    self.assertIsNone(testx.recognize(cmd, top, top))

    # -t . makes a CI step's unittest suite a package run (its re-run of failures too); another top level is not
    # the whole suite
    def test_unittest_with_the_top_level_at_the_root_is_a_package_suite_run(self):
        with Env() as e:
            r = e.repo({"tests/__init__.py": "", "tests/test_a.py": ""}, branch=None)
            top = str(r)
            for cmd in ("python -m unittest discover -s tests -t .", "python -m unittest discover -t . -s tests",
                        "python3 -m unittest discover --top-level-directory=. -s tests"):
                with self.subTest(cmd=cmd):
                    rn = testx.recognize(cmd, top, top)
                    self.assertEqual((rn["start"], rn["package"]), ("tests", True))
            self.assertFalse(testx.recognize("python -m unittest discover -s tests", top, top)["package"])
            self.assertIsNone(testx.recognize("python -m unittest discover -s tests -t lib", top, top))

    def test_failures_are_read_from_a_step_log(self):
        with Env() as e:
            r = e.repo({"tests/test_calc.py": "", "go.mod": "module example.com/shop\n"}, branch=None)
            top = str(r)
            pytest_log = ("tests/test_calc.py F.\n=========================== short test summary info ======\n"
                          "FAILED tests/test_calc.py::test_add - assert 3 == 4\n"
                          "ERROR tests/test_broken.py - ModuleNotFoundError: No module named 'x'\n")
            rn = testx.recognize("pytest", top, top)
            self.assertEqual([f["id"] for f in testx.step_failures(rn, pytest_log, top, 0)],
                             ["tests/test_calc.py::test_add", "tests/test_broken.py"])
            go_log = ("--- FAIL: TestAdd (0.00s)\n    calc_test.go:9: got 3\nFAIL\nFAIL\texample.com/shop/internal/calc"
                      "\t0.01s\nok  \texample.com/shop/cmd/app\t0.01s\n")
            rn = testx.recognize("go test ./...", top, top)
            self.assertEqual([f["id"] for f in testx.step_failures(rn, go_log, top, 0)], ["internal/calc:TestAdd"])
            self.assertIsNone(testx.step_failures(rn, "build failed: something odd\n", top, 0))


CI_WORKFLOW = """name: CI
on:
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Lint
        run: test -f calc.py
      - name: Unit
        run: python -m unittest discover -s tests
"""


class ResumeTests(unittest.TestCase):
    def test_full_step_preserves_shell_expansion_and_quoted_arguments(self):
        for quoted in (False, True):
            with self.subTest(quoted=quoted), G.Sandbox() as s:
                self.setup(s, CALC["calc.py"])
                cwd = s.repo / "suite space" if quoted else s.repo
                directory = "test cases" if quoted else "tests"
                write(cwd, {directory + "/test_environment.py":
                      "import os\nimport sys\nimport unittest\nfrom pathlib import Path\n"
                      "class T(unittest.TestCase):\n    def test_environment(self):\n"
                      "        self.assertEqual(os.environ['TEST_VALUE'], os.environ['HOME'])\n"
                      "        self.assertEqual(os.environ['LITERAL'], '$HOME')\n"
                      "        self.assertEqual(os.environ['WORKING'], os.getcwd())\n"
                      "        self.assertEqual(Path(sys.executable).resolve(), Path(os.environ['EXPECTED']).resolve())\n"})
                module = "'-m' \"unittest\"" if quoted else "-m unittest"
                command = ("TEST_VALUE=$HOME LITERAL='$HOME' MODULE='-m unittest' WORKING=\"$PWD\" EXPECTED="
                           + shlex.quote(PY) + " " + shlex.quote(PY) + " " + module
                           + " discover -s " + shlex.quote(directory) + " -v")
                rc, out, err = core.sh(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", command],
                                       cwd=str(cwd))
                self.assertEqual(rc, 0, out + err)
                workflow = CI_WORKFLOW.replace("python -m unittest discover -s tests", command)
                if quoted:
                    workflow = workflow.replace("      - name: Unit", "      - name: Unit\n        working-directory: suite space")
                s.write(".github/workflows/ci.yml", workflow)
                self.commit(s, "test: expand shell environment in unittest step")
                rc, out, err = G.dev("ci", "--run")
                self.assertEqual(rc, 0, out + err)
                self.assertTrue(gitx.step_records(s.ctx())["test › Unit"]["complete"])
                self.assertIsNotNone(gitx.evidence(s.ctx()))

    def setup(self, s, calc):
        write(s.repo, dict(CALC, **{"calc.py": calc}))
        s.write(".github/workflows/ci.yml", CI_WORKFLOW)
        s.branch("feat/calc")
        return self.commit(s, "feat: calc")

    def commit(self, s, msg):
        git(s.repo, "add", "-A")
        git(s.repo, "commit", "-qm", msg)
        return s.head()

    def steps(self, ev):
        return {x["step"]: x for x in ev["steps"]}

    # pytest writes no completion receipt: a step that failed with the failures it named is resumed with those
    # failures and the tests the fix can affect (tests/test_text.py is neither), never the whole suite again
    def test_resume_reruns_only_the_failed_and_affected_pytest_tests(self):
        with G.Sandbox() as s:
            write(s.repo, dict(PYCALC, **{"calc.py": PY_BROKEN_ADD}))
            s.write(".github/workflows/ci.yml", CI_WORKFLOW.replace("python -m unittest discover -s tests",
                                                                    "python -m pytest"))
            s.branch("feat/calc")
            broken = self.commit(s, "feat: calc")
            self.assertEqual(G.dev("ci", "--run")[0], 1)
            unit = gitx.step_records(s.ctx())["test › Unit"]
            self.assertEqual((unit["complete"], unit["failed_tests"]),
                             (True, ["tests/test_calc.py::test_add", "tests/test_calc.py::test_add_zero"]))
            edit(s.repo, {"calc.py": PYCALC["calc.py"]})
            self.commit(s, "fix: add adds")
            rc, out, err = G.dev("ci", "--run", "--resume")
            self.assertEqual(rc, 0, out + err)
            unit = self.steps(gitx.evidence(s.ctx()))["Unit"]
            self.assertEqual((unit["how"], unit["tests"], unit["base_sha"]), ("partial", 3, broken))

    def test_every_run_records_each_step(self):
        with G.Sandbox() as s:
            sha = self.setup(s, BROKEN_ADD)
            self.assertEqual(G.dev("ci", "--run")[0], 1)
            recs = gitx.step_records(s.ctx())
            self.assertEqual({k.split(" › ")[1]: (v["rc"] == 0, v["sha"]) for k, v in recs.items()},
                             {"Lint": (True, sha), "Unit": (False, sha)})
            unit = next(v for k, v in recs.items() if k.endswith("Unit"))
            self.assertEqual(unit["failed_tests"], ["test_calc.CalcTests.test_add"])
            self.assertGreaterEqual(unit["secs"], 0)

    def test_resume_reruns_failed_and_affected_tests_and_push_accepts_it(self):
        with G.Sandbox() as s:
            broken = self.setup(s, BROKEN_ADD)
            G.dev("ci", "--run")
            edit(s.repo, {"calc.py": CALC["calc.py"]})
            fixed = self.commit(s, "fix: add adds")
            rc, out, err = G.dev("ci", "--run", "--resume")
            self.assertEqual(rc, 0, out + err)
            ev = gitx.evidence(s.ctx())
            self.assertEqual(ev["how"], "resumed")
            steps = self.steps(ev)
            self.assertEqual((steps["Lint"]["how"], steps["Lint"]["sha"]), ("full", fixed))
            self.assertEqual((steps["Unit"]["how"], steps["Unit"]["tests"], steps["Unit"]["base_sha"]),
                             ("partial", 2, broken))
            self.assertEqual(G.dev("push")[0], 0)
            line = gitx._verify_line(ev, gitx.STRINGS["en"])
            self.assertIn("dev ci --run --resume", line)
            self.assertIn(f"Unit: full on {broken[:7]} + 2 affected tests on {fixed[:7]}", line)
            self.assertIn("resume", gitx.describe_evidence(ev))

    def test_docs_only_change_keeps_the_passed_steps(self):
        with G.Sandbox() as s:
            first = self.setup(s, CALC["calc.py"])
            self.assertEqual(G.dev("ci", "--run")[0], 0)
            write(s.repo, {"README.md": "# calc, documented\n"})
            self.commit(s, "docs: explain calc")
            rc, out, err = G.dev("ci", "--run", "--resume")
            self.assertEqual(rc, 0, out + err)
            self.assertIn("kept", out)
            steps = self.steps(gitx.evidence(s.ctx()))
            self.assertEqual({k: (v["how"], v["sha"]) for k, v in steps.items()},
                             {"Lint": ("kept", first), "Unit": ("kept", first)})
            self.assertIn("unaffected", gitx._verify_line(gitx.evidence(s.ctx()), gitx.STRINGS["en"]))

    def test_steps_that_read_every_file_rerun_after_a_docs_change(self):
        for cmd, reads in (("gitleaks dir --redact .", True), ("docker run --rm -v \"$PWD:/src\" ghcr.io/x/trivy:1 fs .", True),
                           ("dev docs check", True), ("npx markdownlint-cli2 '**/*.md'", True), ("vale docs", True),
                           ("dev sec scan", True), ("test -f calc.py", False), ("npm run build", False),
                           ("python -m unittest discover -s tests", False)):
            with self.subTest(cmd=cmd):
                self.assertEqual(gitx._reads_every_file(cmd), reads)
        with G.Sandbox() as s:
            write(s.repo, dict(CALC, **{"calc.py": CALC["calc.py"]}))
            s.write(".github/workflows/ci.yml", CI_WORKFLOW.replace(
                "      - name: Unit", "      - name: Secrets\n        run: echo gitleaks dir .\n      - name: Unit"))
            s.branch("feat/calc")
            self.commit(s, "feat: calc")
            self.assertEqual(G.dev("ci", "--run")[0], 0)
            write(s.repo, {"README.md": "# calc, with a usage section\n"})
            self.commit(s, "docs: explain calc")
            self.assertEqual(G.dev("ci", "--run", "--resume")[0], 0)
            steps = self.steps(gitx.evidence(s.ctx()))
            self.assertEqual((steps["Lint"]["how"], steps["Secrets"]["how"]), ("kept", "full"))  # a doc can leak too

    def test_a_global_change_reruns_the_whole_test_step(self):
        with G.Sandbox() as s:
            self.setup(s, BROKEN_ADD)
            G.dev("ci", "--run")
            edit(s.repo, {"calc.py": CALC["calc.py"], "requirements.txt": "requests\n"})
            self.commit(s, "fix: add adds")
            rc, out, err = G.dev("ci", "--run", "--resume")
            self.assertEqual(rc, 0, out + err)
            self.assertEqual(self.steps(gitx.evidence(s.ctx()))["Unit"]["how"], "full")

    def test_resume_with_nothing_recorded_is_a_full_run(self):
        with G.Sandbox() as s:
            self.setup(s, CALC["calc.py"])
            rc, out, err = G.dev("ci", "--run", "--resume")
            self.assertEqual(rc, 0, out + err)
            self.assertEqual(gitx.evidence(s.ctx())["how"], "run")

    def test_a_partial_failure_records_no_evidence(self):
        with G.Sandbox() as s:
            self.setup(s, BROKEN_ADD)
            G.dev("ci", "--run")
            edit(s.repo, {"calc.py": BROKEN_ADD.replace("a - b", "a * b", 1)})
            self.commit(s, "fix: try again")
            rc, out, err = G.dev("ci", "--run", "--resume")
            self.assertEqual(rc, 1, out + err)
            self.assertIsNone(gitx.evidence(s.ctx()))
            self.assertIn("test_calc.CalcTests.test_add", out + err)

    def test_interrupted_full_step_reruns_whole_even_after_an_assertion_failure(self):
        with G.Sandbox() as s:
            self.setup(s, BROKEN_ADD)
            write(s.repo, {"tests/test_interrupt.py": "import os\nimport signal\nimport unittest\n"
                          "from pathlib import Path\nclass T(unittest.TestCase):\n"
                          "    def test_interrupt(self):\n"
                          "        if Path('interrupt').exists():\n"
                          "            os.kill(os.getpid(), signal.SIGTERM)\n"
                          "    def test_pending(self):\n        Path('completed').write_text('ran')\n",
                          ".gitignore": "__pycache__/\ninterrupt\ncompleted\n",
                          ".github/workflows/ci.yml": CI_WORKFLOW.replace("discover -s", "discover -v -s")})
            self.commit(s, "test: interrupt the runner")
            write(s.repo, {"interrupt": "stop"})
            rc, out, err = G.dev("ci", "--run")
            self.assertEqual(rc, 1, out + err)
            before = gitx.step_records(s.ctx())["test › Unit"]
            self.assertIn("test_calc.CalcTests.test_add", before["failed_tests"] or [])
            self.assertFalse((s.repo / "completed").exists())
            (s.repo / "interrupt").unlink()
            edit(s.repo, {"calc.py": CALC["calc.py"]})
            rc, out, err = G.dev("ci", "--run", "--resume")
            self.assertEqual(rc, 0, out + err)
            self.assertTrue((s.repo / "completed").exists(), "the pending test did not execute")
            self.assertEqual(gitx.step_records(s.ctx())["test › Unit"]["how"], "full")
            self.assertIs(before.get("complete"), False)

    def test_interrupted_partial_step_cannot_become_a_failed_ids_only_retry(self):
        with G.Sandbox() as s:
            self.setup(s, BROKEN_ADD)
            source = (textwrap.dedent(CALC["tests/test_calc.py"]).lstrip("\n") +
                      "\n    def test_z_interrupt(self):\n"
                      "        import os\n        import signal\n"
                      "        if Path('interrupt').exists():\n"
                      "            os.kill(os.getpid(), signal.SIGTERM)\n")
            write(s.repo, {"tests/test_calc.py": source, ".gitignore": "__pycache__/\ninterrupt\ncompleted\n",
                          "tests/test_other.py": "import unittest\nfrom pathlib import Path\n"
                          "class T(unittest.TestCase):\n"
                          "    def test_remaining_suite(self):\n        Path('completed').write_text('ran')\n"})
            self.commit(s, "test: interrupt selected tests")
            self.assertEqual(G.dev("ci", "--run")[0], 1)
            write(s.repo, {"interrupt": "stop"})
            edit(s.repo, {"calc.py": BROKEN_ADD.replace("a - b", "a * b", 1)})
            rc, out, err = G.dev("ci", "--run", "--resume")
            self.assertEqual(rc, 1, out + err)
            before = gitx.step_records(s.ctx())["test › Unit"]
            self.assertEqual(before["how"], "partial")
            (s.repo / "interrupt").unlink()
            (s.repo / "completed").unlink()
            edit(s.repo, {"calc.py": CALC["calc.py"]})
            rc, out, err = G.dev("ci", "--run", "--resume")
            self.assertEqual(rc, 0, out + err)
            self.assertTrue((s.repo / "completed").exists(), "the remaining suite did not execute")
            self.assertEqual(gitx.step_records(s.ctx())["test › Unit"]["how"], "full")
            self.assertIs(before.get("complete"), False)

    def test_legacy_failure_without_completion_evidence_reruns_whole(self):
        with G.Sandbox() as s:
            self.setup(s, BROKEN_ADD)
            self.assertEqual(G.dev("ci", "--run")[0], 1)
            record = gitx.step_records(s.ctx())["test › Unit"]
            record.pop("complete", None)
            gitx._save_step(s.ctx(), "test › Unit", record)
            edit(s.repo, {"calc.py": CALC["calc.py"]})
            rc, out, err = G.dev("ci", "--run", "--resume")
            self.assertEqual(rc, 0, out + err)
            self.assertEqual(gitx.step_records(s.ctx())["test › Unit"]["how"], "full")

    def test_full_step_that_exits_successfully_before_summary_records_no_evidence(self):
        with G.Sandbox() as s:
            self.setup(s, CALC["calc.py"])
            write(s.repo, {"tests/test_interrupt.py": "import os\nimport unittest\nclass T(unittest.TestCase):\n"
                          "    def test_exit(self):\n        os._exit(0)\n"})
            self.commit(s, "test: exit before the suite finishes")
            rc, out, err = G.dev("ci", "--run")
            self.assertEqual(rc, 1, out + err)
            self.assertIsNone(gitx.evidence(s.ctx()))
            self.assertIs(gitx.step_records(s.ctx())["test › Unit"].get("complete"), False)

    def test_full_step_accepts_buffered_stdout_after_outer_summary(self):
        with G.Sandbox() as s, mock.patch.dict(os.environ, {"PYTHONUNBUFFERED": ""}):
            self.setup(s, CALC["calc.py"])
            write(s.repo, {"tests/test_output.py": "import unittest\nclass T(unittest.TestCase):\n"
                          "    def test_print(self):\n        print('buffered test output')\n"
                          "        self.assertEqual(2 + 2, 4)\n"})
            self.commit(s, "test: print buffered output")
            rc, out, err = G.dev("ci", "--run")
            self.assertEqual(rc, 0, out + err)
            self.assertIsNotNone(gitx.evidence(s.ctx()))
            self.assertTrue(gitx.step_records(s.ctx())["test › Unit"]["complete"])

    def test_full_step_rejects_nested_summary_before_outer_early_exit(self):
        with G.Sandbox() as s:
            self.setup(s, CALC["calc.py"])
            write(s.repo, {"tests/test_nested.py": "import os\nimport sys\nimport unittest\n"
                          "class T(unittest.TestCase):\n"
                          "    def test_exit(self):\n"
                          "        suite = unittest.TestSuite([unittest.FunctionTestCase(lambda: None)])\n"
                          "        unittest.TextTestRunner(stream=sys.stderr).run(suite)\n"
                          "        os._exit(0)\n"
                          "    def test_pending(self):\n        self.assertEqual(1, 2)\n"})
            self.commit(s, "test: exit after a nested summary")
            rc, out, err = G.dev("ci", "--run")
            self.assertEqual(rc, 1, out + err)
            self.assertIsNone(gitx.evidence(s.ctx()))
            self.assertFalse(gitx.step_records(s.ctx())["test › Unit"]["complete"])



class WiringTests(unittest.TestCase):
    def test_dev_parser_has_the_test_command(self):
        from devkit import cli
        a = cli.build_parser().parse_args(["test", "prove", "--runs", "3"])
        self.assertEqual((a.fn, a.test_cmd, a.runs), (testx.cmd_test, "prove", 3))

    # an option of `dev test` written before the subcommand is kept, not reset by the subcommand's own default; the
    # same option written after the subcommand still wins
    def test_options_before_the_subcommand_are_kept(self):
        from devkit import cli
        parse = cli.build_parser().parse_args
        for sub in ("prove", "plan", "run"):
            with self.subTest(sub=sub):
                a = parse(["test", "--since", "v1", "--runner", "pytest", "--json", sub])
                self.assertEqual((a.test_cmd, a.since, a.runner, a.json), (sub, "v1", "pytest", True))
                self.assertEqual(parse(["test", "--since", "v1", sub, "--since", "v2"]).since, "v2")
        a = parse(["test", "--failed", "-v", "run"])
        self.assertEqual((a.failed, a.verbose), (True, True))
        a = parse(["test", "prove"])
        self.assertEqual((a.since, a.runner, a.json, a.runs), (None, None, False, testx.RUNS))

    def test_dev_test_is_not_audited_as_a_change(self):
        from devkit import audit
        for argv in (["test"], ["test", "run", "tests/test_x.py"], ["test", "--since", "main"], ["test", "plan"],
                     ["test", "prove"], ["test", "audit", "--changed"]):
            with self.subTest(argv=argv):
                self.assertFalse(audit.audited(argv))


if __name__ == "__main__":
    unittest.main()
