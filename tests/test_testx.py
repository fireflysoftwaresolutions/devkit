# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
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
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import core, gitx, testx  # noqa: E402
import test_gitx as G  # noqa: E402  (its Sandbox and dev() drive `dev ci`)

PY = sys.executable


def write(root, files):
    for rel, text in files.items():
        f = Path(root) / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(textwrap.dedent(text).lstrip("\n") if text else "")


def edit(root, files):
    """Change files after a run, the way an agent does seconds later (a same-second, same-size edit would leave
    Python's bytecode cache looking fresh)."""
    write(root, files)
    later = time.time() + 2
    for rel in files:
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

    def test_unittest_without_pytest_configuration(self):
        found = self.detect({"tests/test_a.py": "import unittest\n", "a.py": ""})
        self.assertEqual(list(found), [("unittest", ".")])
        self.assertEqual(found[("unittest", ".")]["start"], "tests")

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

    def test_unittest_module_names_from_the_start_dir(self):
        top, rn = self.runner({"tests/test_calc.py": ""}, "unittest")
        cmd = testx.build_command(top, rn, files=["tests/test_calc.py"])
        self.assertEqual(cmd["argv"], [PY, "-m", "unittest", "-v", "test_calc"])
        self.assertEqual(cmd["env"]["PYTHONPATH"].split(os.pathsep)[0], str(Path(top) / "tests"))
        cmd = testx.build_command(top, rn, ids=["test_calc.CalcTests.test_add"])
        self.assertEqual(cmd["argv"][-1], "test_calc.CalcTests.test_add")

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

    def test_jest_and_vitest_json(self):
        text = json.dumps(JEST_JSON).replace("ROOT", str(self.top))
        cases = testx.parse_jest_json(text, str(self.top))
        self.assertEqual(failures(cases), [("src/b.test.ts::b adds", "src/b.test.ts", 5,
                                            "Error: expect(received).toBe(expected)")])
        self.assertEqual(len(cases), 2)

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


def state(e):
    files = list((e.root / "state" / "tests").glob("*.json"))
    return json.loads(files[0].read_text()) if files else {}


def recorded(e):
    return sorted(f["id"] for fs in (state(e).get("failures") or {}).values() for f in fs)


class RunTests(unittest.TestCase):
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


class ProveTests(unittest.TestCase):
    def snapshot(self, r):
        return {p: p.read_bytes() for p in Path(r).rglob("*") if p.is_file() and ".git" not in p.parts}

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



class WiringTests(unittest.TestCase):
    def test_dev_parser_has_the_test_command(self):
        from devkit import cli
        a = cli.build_parser().parse_args(["test", "prove", "--runs", "3"])
        self.assertEqual((a.fn, a.test_cmd, a.runs), (testx.cmd_test, "prove", 3))

    def test_dev_test_is_not_audited_as_a_change(self):
        from devkit import audit
        for argv in (["test"], ["test", "run", "tests/test_x.py"], ["test", "--since", "main"], ["test", "plan"],
                     ["test", "prove"], ["test", "audit", "--changed"]):
            with self.subTest(argv=argv):
                self.assertFalse(audit.audited(argv))


if __name__ == "__main__":
    unittest.main()
