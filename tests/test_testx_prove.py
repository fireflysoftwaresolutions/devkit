# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev test prove`: which tests of a changed test file count as new or changed (testx._changed_units), and how their
results are matched (testx._member). Hermetic: tiny git repositories in temporary folders (test_testx.Env).
Run: python3 -m unittest tests.test_testx_prove -v"""
import json
import os
import shutil
import subprocess
import sys
import textwrap
import unittest
import venv
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import testx  # noqa: E402
from test_testx import Env, built_install, dev, edit  # noqa: E402


def changed(before, after, name="tests/test_mod.py"):
    """The units _changed_units names after `name` goes from `before` (committed on main) to `after`."""
    with Env() as e:
        r = e.repo({name: textwrap.dedent(before).lstrip("\n")})
        edit(r, {name: after})
        return testx._changed_units(str(r), "main", name)


JS_BEFORE = """
import { describe, expect, it } from 'vitest';

describe('sum', () => {
  it('adds', () => {
    expect(1 + 1).toBe(2);
  });
});
"""


class HunkContextTests(unittest.TestCase):
    # tests inserted right after an unchanged one, with lines of their own before them (a describe, a constant): those
    # lines belong to the new block, not to the test above it, which stays unchanged (firefly-weave's
    # studio/tests/loop-frame.test.ts)
    def test_tests_inserted_after_an_unchanged_one_are_the_only_changed_tests(self):
        after = JS_BEFORE.replace("""    expect(1 + 1).toBe(2);
  });
});""", """    expect(1 + 1).toBe(2);
  });
});

describe('negatives', () => {
  const minus = (x) => -x;
  it('adds negatives', () => {
    expect(minus(1) + minus(1)).toBe(-2);
  });
});""")
        self.assertEqual(["adds negatives"], changed(JS_BEFORE, after, "src/sum.test.ts"))

    def test_a_test_moved_unchanged_is_no_changed_test(self):
        before = JS_BEFORE.replace("""  });
});""", """  });

  it('subtracts', () => {
    expect(2 - 1).toBe(1);
  });
});""")
        after = before.replace("""  it('adds', () => {
    expect(1 + 1).toBe(2);
  });

  it('subtracts', () => {
    expect(2 - 1).toBe(1);
  });""", """  it('subtracts', () => {
    expect(2 - 1).toBe(1);
  });

  it('adds', () => {
    expect(1 + 1).toBe(2);
  });

  it('adds three', () => {
    expect(1 + 2).toBe(3);
  });""")
        self.assertEqual(["adds three"], changed(before, after, "src/sum.test.ts"))


class EachTests(unittest.TestCase):
    # it.each and test.each name each case from a template (%i, %s, $a): the template is the unit, and every case
    # whose name the template matches belongs to it; the test before it stays unchanged
    def test_an_each_test_is_its_own_unit_and_matches_its_cases(self):
        after = JS_BEFORE.replace("""  });
});""", """  });

  it.each([[1, 2], [2, 3]])('adds %i and one', (a, b) => {
    expect(a + 1).toBe(b);
  });
});""")
        units = changed(JS_BEFORE, after, "src/sum.test.ts")
        self.assertEqual(["adds %i and one"], units)
        f = "src/sum.test.ts"
        case = {"id": "x", "file": f, "name": "sum > adds 2 and one"}
        self.assertTrue(testx._member(case, f, units, None))
        self.assertFalse(testx._member(dict(case, name="sum > adds"), f, units, None))

    def test_a_tagged_template_each_names_its_cases_with_variables(self):
        after = JS_BEFORE.replace("""  });
});""", """  });

  test.each`
    a    | b
    ${1} | ${2}
  `('returns $b for $a', ({ a, b }) => {
    expect(a + 1).toBe(b);
  });
});""")
        units = changed(JS_BEFORE, after, "src/sum.test.ts")
        self.assertEqual(["returns $b for $a"], units)
        self.assertTrue(testx._member({"id": "x", "file": "src/sum.test.ts", "name": "sum > returns 2 for 1"},
                                      "src/sum.test.ts", units, None))


JS_HELPER = """
import { expect, it } from 'vitest';

it('first', () => {
  expect(double(1)).toBe(2);
});

function double(x) {
  return x * 2;
}

it('second', () => {
  expect(1).toBe(1);
});

it('third', () => {
  expect(double(2)).toBe(4);
});
"""


class JsHelperTests(unittest.TestCase):
    # a helper between two it() blocks belongs to neither: editing it changes the tests that call it
    def test_an_edited_helper_between_tests_selects_its_callers_only(self):
        after = JS_HELPER.replace("return x * 2;", "return x + x;")
        self.assertEqual(["first", "third"], changed(JS_HELPER, after, "src/double.test.js"))

    def test_the_helper_text_is_restored_for_the_run_without_the_change(self):
        with Env() as e:
            r = e.repo({"src/double.test.js": JS_HELPER.lstrip("\n")})
            edit(r, {"src/double.test.js": JS_HELPER.replace("return x * 2;", "return x + x;")
                     .replace("expect(1).toBe(1);", "expect(2).toBe(2);")})
            text = testx._support_text(str(r), "main", "src/double.test.js")
        self.assertIn("return x * 2;", text)
        self.assertIn("expect(2).toBe(2);", text)  # a test keeps its change


PY_SHARED = """
import unittest


class Shared(unittest.TestCase):
    def setUp(self):
        self.base = 1

    def double(self, x):
        return x * 2

    def triple(self, x):
        return self.double(x) + x

    def test_double(self):
        self.assertEqual(self.double(2), 4)

    def test_triple(self):
        self.assertEqual(self.triple(1), 3)

    def test_plain(self):
        self.assertEqual(self.base, 1)
"""


class SharedHelperTests(unittest.TestCase):
    # a helper method of a shared test class: a change to it changes the tests that call it, directly or through
    # another helper, not every test of the class; a change to setUp changes them all
    def test_a_changed_helper_selects_its_callers_through_other_helpers(self):
        after = PY_SHARED.replace("return x * 2", "return x + x")
        self.assertEqual(["Shared.test_double", "Shared.test_triple"], changed(PY_SHARED, after))
        # setUp runs for every test of the class: a change to it changes them all
        after = PY_SHARED.replace("self.base = 1", "self.base = 2 - 1")
        self.assertEqual(["Shared.test_double", "Shared.test_plain", "Shared.test_triple"], changed(PY_SHARED, after))

    def test_a_helper_added_for_a_new_test_selects_only_that_test(self):
        after = PY_SHARED.replace("    def test_plain(self):", """    def half(self, x):
        return x / 2

    def test_half(self):
        self.assertEqual(self.half(4), 2)

    def test_plain(self):""")
        self.assertEqual(["Shared.test_half"], changed(PY_SHARED, after))

    def test_a_changed_module_function_selects_the_tests_that_call_it(self):
        before = PY_SHARED.replace("class Shared", "def make():\n    return 5\n\n\nclass Shared").replace(
            "self.assertEqual(self.base, 1)", "self.assertEqual(make(), 5)")
        after = before.replace("return 5", "return 2 + 3")
        self.assertEqual(["Shared.test_plain"], changed(before, after))



SRC_PKG = {"pyproject.toml": '[project]\nname = "pkg"\nversion = "0"\n',
           "src/pkg/__init__.py": "", "src/pkg/mod.py": "def value():\n    return 1\n",
           "tests/__init__.py": "",
           "tests/test_mod.py": "import unittest\n\nfrom pkg import mod\n\n\nclass ModTests(unittest.TestCase):\n"
                                "    def test_one(self):\n        self.assertEqual(mod.value() % 1, 0)\n",
           ".gitignore": "__pycache__/\n.venv/\n"}
TEST_TWO = "\n    def test_two(self):\n        self.assertEqual(mod.value(), 2)\n"


def src_change(r):
    """The change: mod.value() returns 2, and a new test that needs it."""
    (r / "src" / "pkg" / "mod.py").write_text("def value():\n    return 2\n")
    (r / "tests" / "test_mod.py").write_text((r / "tests" / "test_mod.py").read_text() + TEST_TWO)


def site_packages(r):
    venv.create(r / ".venv", with_pip=False, symlinks=True)
    return next((r / ".venv" / "lib").glob("python*/site-packages"))


class ImportEvidenceTests(unittest.TestCase):
    # prove says "proven" only with evidence of where the run without the change imported the changed files from:
    # the copy, where the change is undone. A run that imported a changed file from the checkout, where the change
    # stays, proves nothing, and prove names that file.
    def prove(self, r):
        rc, out, err = dev(r, "prove", "--json", "--runs", "1")
        return rc, json.loads(out), out + err

    def test_an_editable_src_layout_is_proven_with_the_copys_import_as_evidence(self):
        with Env() as e:
            r = e.repo(SRC_PKG)
            (site_packages(r) / "_pkg_editable.pth").write_text(f"{r / 'src'}\n")
            src_change(r)
            rc, data, text = self.prove(r)
            self.assertEqual(0, rc, text)
            self.assertEqual([("tests.test_mod.ModTests.test_two", "proves the change")],
                             [(t["id"], t["verdict"]) for t in data["tests"]])
            self.assertEqual({"from_copy": ["src/pkg/mod.py"], "from_checkout": []}, data["imports"])

    def test_a_built_install_is_proven_with_the_copys_import_as_evidence(self):
        with Env() as e:
            r = e.repo(SRC_PKG)
            site_packages(r)
            built_install(r, {"pkg/__init__.py": "", "pkg/mod.py": "def value():\n    return 2\n"})
            src_change(r)
            rc, data, text = self.prove(r)
            self.assertEqual(0, rc, text)
            self.assertEqual(["proves the change"], [t["verdict"] for t in data["tests"]])
            self.assertEqual({"from_copy": ["src/pkg/mod.py"], "from_checkout": []}, data["imports"])

    def test_a_suite_that_clears_its_environment_still_leaves_its_record(self):
        # devkit's own suite drops every DEVKIT_* variable when it starts (tests/_hermetic.py)
        with Env() as e:
            r = e.repo(dict(SRC_PKG, **{"tests/__init__.py": "import os\n\nfor k in [k for k in os.environ "
                                                             "if k.startswith('DEVKIT_')]:\n    del os.environ[k]\n"}))
            (site_packages(r) / "_pkg_editable.pth").write_text(f"{r / 'src'}\n")
            src_change(r)
            rc, data, text = self.prove(r)
            self.assertEqual(0, rc, text)
            self.assertEqual(["src/pkg/mod.py"], data["imports"]["from_copy"])

    def test_a_run_that_imported_a_changed_file_from_the_checkout_is_not_proven_and_names_it(self):
        with Env() as e:
            r = e.repo(SRC_PKG)
            # a .pth whose code puts the checkout's src first: devkit reads no folder from a line that runs code
            (site_packages(r) / "_pkg_paths.pth").write_text(f"import sys; sys.path.insert(0, {str(r / 'src')!r})\n")
            src_change(r)
            rc, data, text = self.prove(r)
            self.assertEqual(2, rc, text)
            self.assertEqual(["cannot be proven: imported the checkout's src/pkg/mod.py"],
                             [t["verdict"] for t in data["tests"]])
            self.assertEqual(["src/pkg/mod.py"], data["imports"]["from_checkout"])
            rc, out, err = dev(r, "prove", "--runs", "1")
            self.assertIn("src/pkg/mod.py", out)

    def checkout_first(self, r, src=None):
        """A .pth whose code puts the checkout's src (or `src`) first on the path of every Python of the venv."""
        (site_packages(r) / "_pkg_paths.pth").write_text(
            f"import sys; sys.path.insert(0, {str(src or r / 'src')!r})\n")

    def test_an_import_inside_a_patched_sys_modules_still_names_the_checkouts_file(self):
        # patch.dict(sys.modules) drops the module again before the process ends: the import is recorded when it runs
        test = ("import sys\nimport unittest\nfrom unittest import mock\n\n\nclass ModTests(unittest.TestCase):\n"
                "    def test_one(self):\n        with mock.patch.dict(sys.modules):\n            from pkg import mod\n"
                "            self.assertEqual(mod.value() % 1, 0)\n")
        with Env() as e:
            r = e.repo(dict(SRC_PKG, **{"tests/test_mod.py": test}))
            self.checkout_first(r)
            edit(r, {"src/pkg/mod.py": "def value():\n    return 2\n",
                     "tests/test_mod.py": test + "\n    def test_two(self):\n        with mock.patch.dict(sys.modules):"
                                                 "\n            from pkg import mod\n"
                                                 "            self.assertEqual(mod.value(), 2)\n"})
            rc, data, text = self.prove(r)
            self.assertEqual(2, rc, text)
            self.assertEqual(["cannot be proven: imported the checkout's src/pkg/mod.py"],
                             [t["verdict"] for t in data["tests"]])

    def test_a_python_child_started_with_I_leaves_no_record_so_the_test_is_not_proven(self):
        # -I ignores PYTHONPATH, so the child runs without the recorder: what it imported is unknown
        test = ("import subprocess\nimport sys\nimport unittest\n\n\nclass ModTests(unittest.TestCase):\n"
                "    def test_one(self):\n        self.assertEqual(1, 1)\n")
        with Env() as e:
            r = e.repo(dict(SRC_PKG, **{"tests/test_mod.py": test}))
            self.checkout_first(r)
            edit(r, {"src/pkg/mod.py": "def value():\n    return 2\n",
                     "tests/test_mod.py": test + "\n    def test_two(self):\n        p = subprocess.run([sys.executable, "
                                                 "'-I', '-c', 'from pkg import mod; raise SystemExit(mod.value() != 2)'])"
                                                 "\n        self.assertEqual(0, p.returncode)\n"})
            rc, data, text = self.prove(r)
            self.assertEqual(2, rc, text)
            self.assertEqual([testx._NO_IMPORT_RECORD], [t["verdict"] for t in data["tests"]])
            rc, out, err = dev(r, "prove", "--runs", "1")
            self.assertIn("-I -c from pkg import mod", out)  # names the child that ran unrecorded

    # an unrecorded child in one test file costs only that file its proof: the other file's record still proves it
    def test_an_unrecorded_child_in_one_test_file_leaves_the_other_files_proven(self):
        child = ("import subprocess\nimport sys\nimport unittest\n\nfrom pkg import mod\n\n\n"
                 "class ChildTests(unittest.TestCase):\n    def test_one(self):\n        self.assertEqual(1, 1)\n")
        with Env() as e:
            r = e.repo(dict(SRC_PKG, **{"tests/test_child.py": child}))
            (site_packages(r) / "_pkg_editable.pth").write_text(f"{r / 'src'}\n")
            src_change(r)
            edit(r, {"tests/test_child.py": child + "\n    def test_two(self):\n        subprocess.run([sys.executable, "
                                                    "'-I', '-c', 'pass'])\n        self.assertEqual(mod.value(), 2)\n"})
            rc, data, text = self.prove(r)
            self.assertEqual(0, rc, text)
            self.assertEqual({("tests.test_child.ChildTests.test_two", testx._NO_IMPORT_RECORD),
                              ("tests.test_mod.ModTests.test_two", "proves the change")},
                             {(t["id"], t["verdict"]) for t in data["tests"]})

    @unittest.skipUnless(os.path.exists(os.path.realpath(__file__).upper()) or
                         os.path.exists(os.path.realpath(__file__).lower()),
                         "the file system is case-sensitive, so a checkout path in another case does not exist here")
    def test_a_checkout_path_in_another_letter_case_is_still_the_checkout(self):
        with Env() as e:
            r = e.repo(SRC_PKG)
            self.checkout_first(r, str(r / "src").upper())
            src_change(r)
            rc, data, text = self.prove(r)
            self.assertEqual(2, rc, text)
            self.assertEqual(["cannot be proven: imported the checkout's src/pkg/mod.py"],
                             [t["verdict"] for t in data["tests"]])

    def test_a_run_that_imported_no_changed_file_from_the_copy_is_not_proven(self):
        # the test runs the module's source without importing it: no record says where that code came from
        test = ("import unittest\nfrom pathlib import Path\n\nSRC = Path(__file__).resolve().parents[1] / 'src' / 'pkg' / "
                "'mod.py'\n\n\nclass ModTests(unittest.TestCase):\n    def test_one(self):\n        ns = {}\n"
                "        exec(compile(SRC.read_text(), '<mod>', 'exec'), ns)\n"
                "        self.assertEqual(ns['value']() % 1, 0)\n")
        with Env() as e:
            r = e.repo(dict(SRC_PKG, **{"tests/test_mod.py": test}))
            edit(r, {"src/pkg/mod.py": "def value():\n    return 2\n",
                     "tests/test_mod.py": test + "\n    def test_two(self):\n        ns = {}\n"
                                                 "        exec(compile(SRC.read_text(), '<mod>', 'exec'), ns)\n"
                                                 "        self.assertEqual(ns['value'](), 2)\n"})
            rc, data, text = self.prove(r)
            self.assertEqual(2, rc, text)
            self.assertEqual([testx._NOT_FROM_COPY], [t["verdict"] for t in data["tests"]])

    # a change to a data file and an unrelated Python file: the test reads the data file from the copy, which is the
    # evidence that it ran without the change, though it imports no changed Python file
    def test_a_data_file_read_from_the_copy_proves_a_change_that_also_touches_python(self):
        test = ("import json\nimport unittest\nfrom pathlib import Path\n\nDATA = Path(__file__).resolve().parents[1] / "
                "'pkg' / 'limits.json'\n\n\nclass T(unittest.TestCase):\n    def test_one(self):\n"
                "        self.assertIn('max', json.loads(DATA.read_text()))\n")
        with Env() as e:
            r = e.repo({"pkg/__init__.py": "", "pkg/other.py": "X = 1\n", "pkg/limits.json": '{"max": 1}',
                        "tests/__init__.py": "", "tests/test_limits.py": test})
            edit(r, {"pkg/limits.json": '{"max": 2}', "pkg/other.py": "X = 2\n",
                     "tests/test_limits.py": test + "\n    def test_two(self):\n"
                                                    "        self.assertEqual(2, json.loads(DATA.read_text())['max'])\n"})
            rc, data, text = self.prove(r)
            self.assertEqual(0, rc, text)
            self.assertEqual(["proves the change"], [t["verdict"] for t in data["tests"]])
            self.assertEqual(["pkg/limits.json"], data["imports"]["from_copy"])

    def test_a_flat_layout_built_install_of_the_checkout_counts_as_the_checkouts_import(self):
        # pkg/ at the checkout's root, not under src/: the built copy's pkg/mod.py is the checkout's pkg/mod.py
        with Env() as e:
            top, dest = e.root / "co", e.root / "copy"
            (dest / "pkg").mkdir(parents=True)
            top.mkdir()
            built_install(top, {"pkg/__init__.py": "", "pkg/mod.py": "VALUE = 2\n"})
            site = next((top / ".venv" / "lib").glob("python*/site-packages"))
            folder, records, env = testx._import_recorder(dest, top)
            p = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(site)!r}); import pkg.mod"],
                               env=dict(os.environ, PYTHONPATH=folder, **env), capture_output=True, text=True)
            self.assertEqual(0, p.returncode, p.stderr)
            ev = testx._import_evidence(records, top, dest, ["pkg/mod.py"], [str(top / ".venv")])
            self.assertEqual((["pkg/mod.py"], []), (ev["from_checkout"], ev["from_copy"]))



class RecorderTests(unittest.TestCase):
    # the recorder's sitecustomize (testx._IMPORT_RECORDER) on its own: a Python that runs `code` with the recorder on
    # its path, and what the records say
    def record(self, e, code, dest=None):
        top, dest = e.root / "co", Path(dest or e.root / "copy")
        top.mkdir(exist_ok=True)
        dest.mkdir(parents=True, exist_ok=True)
        folder, records, env = testx._import_recorder(dest, top)
        p = subprocess.run([sys.executable, "-c", code], env=dict(os.environ, PYTHONPATH=folder, **env),
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(0, p.returncode, p.stderr[-2000:])
        items = [json.loads(x) for f in Path(records).glob("*.jsonl") for x in f.read_text().splitlines()]
        return items, records

    # an import in one thread is recorded while another thread is inside the recorder's hook
    def test_imports_from_many_threads_are_all_recorded(self):
        with Env() as e:
            mods = e.root / "copy" / "mods"
            mods.mkdir(parents=True)
            for i in range(400):
                (mods / f"m{i}.py").write_text(f"X = {i}\n")
            code = (f"import importlib, sys, threading; sys.path.insert(0, {str(mods)!r})\n"
                    "def go(k):\n    for i in range(k, 400, 8):\n        importlib.import_module('m%d' % i)\n"
                    "ts = [threading.Thread(target=go, args=(k,)) for k in range(8)]\n"
                    "[t.start() for t in ts]\n[t.join() for t in ts]\n")
            items, _ = self.record(e, code)
            self.assertEqual(400, len({x for x in items if isinstance(x, str) and "/mods/m" in x}))

    # each way a test starts a Python that skips the recorder is written down as unrecorded; the same commands
    # without the switch that skips it are not
    def unrecorded(self, e, call):
        code = f"import os, shlex, subprocess, sys\nexe = sys.executable\n{call}\n"
        return [x["unrecorded"] for x in self.record(e, code)[0] if isinstance(x, dict)]

    def test_a_python_child_that_skips_the_recorder_is_found_behind_each_wrapper(self):
        true = shutil.which("true")
        skipping = {
            "-W value": "subprocess.run([exe, '-W', 'ignore', '-I', '-c', 'pass'])",
            "-X value": "subprocess.run([exe, '-X', 'utf8', '-I', '-c', 'pass'])",
            "executable=": "subprocess.run(['tool', '-I', '-c', 'pass'], executable=exe)",
            "env": "subprocess.run(['/usr/bin/env', exe, '-I', '-c', 'pass'])",
            "env NAME=value": "subprocess.run(['/usr/bin/env', 'A=1', exe, '-I', '-c', 'pass'])",
            "env -i": "subprocess.run(['/usr/bin/env', '-i', exe, '-c', 'pass'])",
            "shell PYTHONPATH=": "subprocess.run('PYTHONPATH= ' + shlex.quote(exe) + ' -c pass', shell=True)",
            "bash -lc": f"subprocess.run(['bash', '-lc', exe + ' -I -c pass'], executable={true!r})",
            "sh -ec": f"subprocess.run(['sh', '-ec', exe + ' -I -c pass'], executable={true!r})",
        }
        for name, call in skipping.items():
            with self.subTest(name), Env() as e:
                self.assertEqual(1, len(self.unrecorded(e, call)), call)

    def test_a_python_child_that_keeps_the_recorder_is_not_flagged(self):
        true = shutil.which("true")
        keeping = {
            "-W value": "subprocess.run([exe, '-W', 'ignore', '-c', 'pass'])",
            "env NAME=value": "subprocess.run(['/usr/bin/env', 'A=1', exe, '-c', 'pass'])",
            "shell NAME=value": "subprocess.run('A=1 ' + shlex.quote(exe) + ' -c pass', shell=True)",
            "sh -ec": f"subprocess.run(['sh', '-ec', exe + ' -c pass'], executable={true!r})",
        }
        for name, call in keeping.items():
            with self.subTest(name), Env() as e:
                self.assertEqual([], self.unrecorded(e, call), call)


# an npm workspace: node_modules links the package @acme/util to packages/util, as npm, pnpm and yarn link a
# workspace package; the root's tests import it by its name
WORKSPACE = {
    "package.json": json.dumps({"name": "root", "private": True, "type": "module", "workspaces": ["packages/*"],
                                "scripts": {"test": "node --test test/"}}),
    "packages/util/package.json": json.dumps({"name": "@acme/util", "type": "module", "main": "index.mjs"}),
    "packages/util/index.mjs": "export const value = () => 1;\n",
    "test/util.test.mjs": 'import assert from "node:assert/strict";\nimport { test } from "node:test";\n'
                          'import { value } from "@acme/util";\n\ntest("is a number", () => '
                          'assert.equal(typeof value(), "number"));\n',
    ".gitignore": "node_modules/\n",
}


class LinkedWorkspaceTests(unittest.TestCase):
    # the copy links the checkout's node_modules, whose link to a workspace package resolves into the checkout: the
    # run without the change loads the checkout's changed package, so its result proves nothing, and prove says which
    # file it loaded
    @unittest.skipUnless(shutil.which("node"), "node is not installed, so the node runner cannot run here")
    def test_a_workspace_package_linked_from_node_modules_is_not_proven_and_named(self):
        with Env() as e:
            r = e.repo(WORKSPACE)
            (r / "node_modules" / "@acme").mkdir(parents=True)
            os.symlink("../../packages/util", r / "node_modules" / "@acme" / "util")
            edit(r, {"packages/util/index.mjs": "export const value = () => 2;\n",
                     "test/util.test.mjs": WORKSPACE["test/util.test.mjs"] +
                     'test("is two", () => assert.equal(value(), 2));\n'})
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            data = json.loads(out)
            self.assertEqual(2, rc, out + err)
            self.assertEqual({"cannot be proven: imported the checkout's packages/util/index.mjs"},
                             {t["verdict"] for t in data["tests"]})
            self.assertEqual(["packages/util/index.mjs"], data["imports"]["from_checkout"])

    # for JavaScript nothing records what the run imported: a proven test says that only the workspace links were
    # checked
    @unittest.skipUnless(shutil.which("node"), "node is not installed, so the node runner cannot run here")
    def test_a_proven_javascript_test_says_only_workspace_links_were_checked(self):
        with Env() as e:
            r = e.repo({"package.json": json.dumps({"name": "root", "type": "module",
                                                    "scripts": {"test": "node --test test/"}}),
                        "src/util.mjs": "export const value = () => 1;\n",
                        "test/util.test.mjs": 'import assert from "node:assert/strict";\nimport { test } from '
                                              '"node:test";\nimport { value } from "../src/util.mjs";\n\n'
                                              'test("is a number", () => assert.equal(typeof value(), "number"));\n'})
            edit(r, {"src/util.mjs": "export const value = () => 2;\n",
                     "test/util.test.mjs": (r / "test" / "util.test.mjs").read_text() +
                     'test("is two", () => assert.equal(value(), 2));\n'})
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            data = json.loads(out)
            self.assertEqual((0, ["proves the change"]), (rc, [t["verdict"] for t in data["tests"]]), out + err)
            self.assertEqual(["workspace links only"], [t.get("imports_checked") for t in data["tests"]])
            rc, out, err = dev(r, "prove", "--runs", "1")
            self.assertIn("for JavaScript, prove checks only", out)


if __name__ == "__main__":
    unittest.main()
