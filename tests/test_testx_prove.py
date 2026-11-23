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
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import testx  # noqa: E402
from test_testx import MUL, PROVE_BASE, TEST_MUL, Env, built_install, dev, edit, with_tests  # noqa: E402


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


class HelperClassTests(unittest.TestCase):
    # a class without tests (a fake) is matched member by member: a method the change adds stays, as an added helper
    # does, and only lines added inside a method that was there go with the change
    def support_text(self, f, before, after):
        with Env() as e:
            r = e.repo({f: before})
            edit(r, {f: after})
            return testx._support_text(str(r), "main", f)

    def test_a_method_added_to_a_python_helper_class_stays(self):
        before = ("import unittest\n\n\nclass Fake:\n    def get(self):\n        y = 0\n        return 1 + y\n\n\n"
                  "class T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(Fake().get(), 1)\n")
        test = "Fake().get(), 1)\n\n    def test_close(self):\n        self.assertTrue(Fake().close())\n"
        close = "        return 1 + y\n\n    def close(self):\n        return True\n"
        separate = before.replace("y = 0", "y = 0 * 2").replace("        return 1 + y\n", close) \
            .replace("Fake().get(), 1)\n", test)
        text = self.support_text("tests/test_a.py", before, separate)
        self.assertIn("    def close(self):\n        return True\n", text)
        self.assertIn("        y = 0\n", text)  # the edit inside get goes with the change

    # one hunk edits get's last line and adds close after it: the edit goes with the change line by line, close stays
    def test_a_mixed_hunk_restores_the_edited_lines_and_keeps_the_added_method(self):
        before = ("import unittest\n\n\nclass Fake:\n    def get(self):\n        y = 0\n        return 1 + y\n\n\n"
                  "class T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(Fake().get(), 1)\n")
        mixed = before.replace("        return 1 + y\n",
                               "        return 2 + y\n\n    def close(self):\n        return True\n")
        text = self.support_text("tests/test_a.py", before, mixed)
        self.assertIsNotNone(text)
        self.assertIn("        return 1 + y\n", text)
        self.assertNotIn("return 2 + y", text)
        self.assertIn("    def close(self):\n        return True\n", text)

    # Fake.__init__ sets z instead of y (one hunk) and get returns z with a new close() after it (another hunk); the
    # production change is unrelated: the run without the change must not fail on get's z and __init__'s y
    def test_a_mixed_hunk_whose_edit_needs_a_reverted_member_proves_nothing(self):
        fake = "class Fake:\n    def __init__(self):\n        self.y = 1\n\n    def get(self):\n        return self.y\n\n\n"
        test = with_tests().replace("class CalcTests(", fake + "class CalcTests(")
        with Env() as e:
            r = e.repo(dict(PROVE_BASE, **{"tests/test_calc.py": test}))
            after = test.replace("self.y = 1", "self.z = 1").replace(
                "        return self.y\n", "        return self.z\n\n    def close(self):\n        return True\n") + \
                "\n    def test_close(self):\n        self.assertTrue(Fake().close())\n" \
                "        self.assertEqual(Fake().get(), 1)\n"
            edit(r, {"tests/test_calc.py": after, "calc.py": MUL})
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            rows = json.loads(out)["tests"]
        self.assertEqual([("test_calc.CalcTests.test_close", "does not test the change")],
                         [(x["id"], x["verdict"]) for x in rows], out + err)
        self.assertEqual(1, rc)

    # one hunk edits two attributes with a new method between them: each attribute comes back from its own base text
    # and the method stays, so the test of the edit is proven
    def test_a_hunk_that_edits_two_members_around_an_added_one_is_undone_member_by_member(self):
        fake = "class Fake:\n    x = 1\n    y = 2\n\n\n"
        test = with_tests().replace("class CalcTests(", fake + "class CalcTests(")
        with Env() as e:
            r = e.repo(dict(PROVE_BASE, **{"tests/test_calc.py": test}))
            after = test.replace("    x = 1\n    y = 2\n",
                                 "    x = 10\n\n    def c(self):\n        return 3\n\n    y = 20\n") + \
                "\n    def test_c(self):\n        self.assertEqual(Fake().c() + Fake.x, 13)\n"
            edit(r, {"tests/test_calc.py": after, "calc.py": MUL})
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            rows = json.loads(out)["tests"]
        self.assertEqual([("test_calc.CalcTests.test_c", "proves the change")],
                         [(x["id"], x["verdict"]) for x in rows], out + err)
        self.assertEqual(0, rc)

    def test_an_arrow_property_s_if_block_is_no_member_of_a_javascript_class(self):
        before = ("class Fake {\n  close = () => {\n    return 2;\n  };\n  get() {\n    return 1;\n  }\n}\n\n"
                  "test('a', () => { expect(new Fake().get()).toBe(1); });\n")
        after = before.replace("    return 2;\n", "    if (this.y) {\n      return 3;\n    }\n    return 2;\n")
        self.assertNotIn(("Fake", "if"), testx._js_members(after))
        for word in ("for", "while", "switch"):
            self.assertNotIn(("Fake", word), testx._js_members(after.replace("if (this.y)", f"{word} (this.y)")))

    def test_a_method_added_to_a_javascript_helper_class_stays(self):
        before = ("class Fake {\n  get() {\n    const y = 0;\n    return 1 + y;\n  }\n}\n\n"
                  "test('a', () => { expect(new Fake().get()).toBe(1); });\n")
        after = before.replace("const y = 0;", "const y = 0 * 2;").replace(
            "    return 1 + y;\n  }\n", "    return 1 + y;\n  }\n  close() {\n    return true;\n  }\n") + \
            "test('b', () => { expect(new Fake().close()).toBe(true); });\n"
        text = self.support_text("tests/a.test.js", before, after)
        self.assertIn("  close() {\n    return true;\n  }\n", text)
        self.assertIn("    const y = 0;\n", text)


FAKE = "class Fake:\n    def __init__(self):\n        self.y = 1\n\n    def get(self):\n        return self.y\n\n\n"


def fake_module(fake=FAKE):
    """PROVE_BASE's test module with the helper code `fake` before its test class."""
    return with_tests().replace("class CalcTests(", fake + "class CalcTests(")


def support_text(f, before, after):
    """_support_text after `f` goes from `before` (committed on main) to `after`."""
    with Env() as e:
        r = e.repo({f: before})
        edit(r, {f: after})
        return testx._support_text(str(r), "main", f)


def prove_rows(before, after):
    """(exit, [(test id, verdict)], output) of prove after tests/test_calc.py goes from `before` to `after`, beside a
    production change no test of it reaches."""
    with Env() as e:
        r = e.repo(dict(PROVE_BASE, **{"tests/test_calc.py": before}))
        edit(r, {"tests/test_calc.py": after, "calc.py": MUL})
        rc, out, err = dev(r, "prove", "--json", "--runs", "1")
    return rc, [(x["id"], x["verdict"]) for x in json.loads(out)["tests"]], out + err


class MemberRestoreTests(unittest.TestCase):
    # the run without the change rebuilds each changed helper from its own text at the base, matched by class and name,
    # never from where git's diff puts its lines: here git pairs get's old last line with the same last line of close,
    # which the change adds after it, and get came back without its return
    def test_a_changed_helper_comes_back_as_its_base_text_beside_an_added_one_that_ends_alike(self):
        before = fake_module("class Fake:\n    def get(self):\n        r = 1\n        return r\n\n\n")
        after = before.replace("        return r\n", "        return r + 0\n\n    def close(self):\n        r = 2\n"
                                                    "        return r\n") + \
            "\n    def test_get(self):\n        self.assertIsNotNone(Fake().get())\n" \
            "        self.assertEqual(Fake().close(), 2)\n"
        with Env() as e:
            r = e.repo(dict(PROVE_BASE, **{"tests/test_calc.py": before}))
            edit(r, {"tests/test_calc.py": after, "calc.py": MUL})
            text = testx._support_text(str(r), "main", "tests/test_calc.py")
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
        self.assertIn("    def get(self):\n        r = 1\n        return r\n\n    def close(self):\n        r = 2\n"
                      "        return r\n", text)
        self.assertEqual([("test_calc.CalcTests.test_get", "does not test the change")],
                         [(x["id"], x["verdict"]) for x in json.loads(out)["tests"]], out + err)

    def test_a_changed_javascript_method_comes_back_as_its_base_text_beside_an_added_one(self):
        before = ("class Fake {\n  get() {\n    const r = 1;\n    return r;\n  }\n}\n\n"
                  "test('a', () => { expect(new Fake().get()).toBe(1); });\n")
        after = before.replace("    return r;\n  }\n", "    return r + 0;\n  }\n\n  close() {\n    const r = 2;\n"
                                                      "    return r;\n  }\n") + \
            "test('b', () => { expect(new Fake().close()).toBe(2); });\n"
        self.assertIn("  get() {\n    const r = 1;\n    return r;\n  }\n\n  close() {\n    const r = 2;\n    return r;\n"
                      "  }\n", support_text("tests/a.test.js", before, after))

    # a helper the change removes comes back too: without the change, setUp ran
    def test_a_helper_the_change_removes_comes_back(self):
        before = ("import unittest\n\n\nclass T(unittest.TestCase):\n    def setUp(self):\n        self.k = 1\n\n"
                  "    def test_a(self):\n        self.assertTrue(True)\n")
        after = before.replace("    def setUp(self):\n        self.k = 1\n\n", "") + \
            "\n    def test_no_k(self):\n        self.assertFalse(hasattr(self, 'k'))\n"
        text = support_text("tests/test_a.py", before, after)
        self.assertIn("    def setUp(self):\n        self.k = 1\n", text)
        self.assertIn("    def test_no_k(self):\n", text)

    # a restore is checked: each helper it undid reads as at the base, each it kept as now. Two members on one line
    # cannot be rebuilt one by one, so the module's tests cannot be proven, rather than proven by the member it lost
    def test_a_restore_that_does_not_give_back_each_base_helper_cannot_be_proven(self):
        before = ("import unittest\n\n\nclass Fake:\n    x = 1\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n"
                  "        self.assertEqual(Fake.x, 1)\n")
        after = before.replace("    x = 1\n", "    x = 2; z = 3\n") + \
            "\n    def test_z(self):\n        self.assertEqual(Fake.z, 3)\n"
        self.assertIs(testx._SUPPORT_TANGLED, support_text("tests/test_a.py", before, after))

    # imports keep the change: a helper rebuilt from its base text that needs a name only a removed import gave would
    # fail on that name, so the module's tests cannot be proven
    def test_a_rebuilt_helper_that_needs_a_removed_import_cannot_be_proven(self):
        before = ("import os\nimport unittest\n\n\ndef sep():\n    return os.sep\n\n\nclass T(unittest.TestCase):\n"
                  "    def test_a(self):\n        self.assertTrue(sep())\n")
        after = before.replace("import os\n", "").replace("return os.sep", "return '/'")
        self.assertIs(testx._SUPPORT_TANGLED, support_text("tests/test_a.py", before, after))
        js = ("import { sep } from 'node:path';\n\nfunction where() {\n  return sep;\n}\n\n"
              "test('a', () => { expect(where()).toBeTruthy(); });\n")
        after = js.replace("import { sep } from 'node:path';\n", "").replace("return sep;", "return '/';")
        self.assertIs(testx._SUPPORT_TANGLED, support_text("tests/a.test.js", js, after))

    # a test that reads a name only the change's helper edit gives, itself or through a helper the change adds, cannot
    # be proven: without the change it fails on that name, whatever the change does
    def test_a_test_that_reads_a_name_the_undone_helper_lines_give_cannot_be_proven(self):
        cases = {
            "an added member reads an attribute the edit sets": lambda t: t.replace("self.y = 1", "self.z = 1").replace(
                "        return self.y\n", "        return self.y if hasattr(self, 'y') else 1\n\n    def close(self):\n"
                                         "        return self.z\n") +
            "\n    def test_close(self):\n        self.assertEqual(Fake().close(), 1)\n",
            "the test reads the attribute the edit sets": lambda t: t.replace("self.y = 1", "self.z = 1").replace(
                "return self.y", "return self.z") + "\n    def test_z(self):\n        self.assertEqual(Fake().z, 1)\n",
            "an added class calls a method whose parameters changed": lambda t: t.replace(
                "    def get(self):\n        return self.y\n", "    def get(self, n):\n        return self.y * n\n").replace(
                "class CalcTests(", "class Other:\n    def make(self):\n        return Fake().get(1)\n\n\nclass CalcTests(") +
            "\n    def test_make(self):\n        self.assertEqual(Other().make(), 1)\n",
            # the edit and the added function share one hunk of git's diff
            "an added function calls a method whose parameters changed": lambda t: t.replace(
                "    def get(self):\n        return self.y\n", "    def get(self, n):\n        return self.y * n\n").replace(
                "class CalcTests(", "def make():\n    return Fake().get(1)\n\n\nclass CalcTests(") +
            "\n    def test_make(self):\n        self.assertEqual(make(), 1)\n",
        }
        for name, change in cases.items():
            with self.subTest(name):
                rc, rows, text = prove_rows(fake_module(), change(fake_module()))
                self.assertEqual([testx._HELPERS_TANGLED], [v for _, v in rows], text)
                self.assertEqual(2, rc, text)

    # a module-level name renamed is a helper removed and another added: the base one comes back and the new one stays,
    # so a new test of the new one runs as it does with the change, and does not test it
    def test_a_renamed_module_name_stays_beside_the_base_one(self):
        before = fake_module("Z = 1\n\n\n" + FAKE)
        after = before.replace("Z = 1\n", "W = 1\n").replace("class CalcTests(", "def w():\n    return W\n\n\n"
                                                                               "class CalcTests(") + \
            "\n    def test_w(self):\n        self.assertEqual(w(), 1)\n"
        rc, rows, text = prove_rows(before, after)
        self.assertEqual([("test_calc.CalcTests.test_w", "does not test the change")], rows, text)

    # only a name at the class body's own depth is a member: a call inside an arrow property's body is none
    def test_a_call_inside_an_arrow_property_is_no_member_of_a_javascript_class(self):
        text = ("class Fake {\n  close = () => {\n    cleanup(this.y);\n    return 2;\n  };\n  get() {\n    return 1;\n"
                "  }\n}\n")
        self.assertEqual({("Fake", "close"), ("Fake", "get")}, set(testx._js_members(text)))


PY_TEST = "class T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(h(), 1)\n"
JS_TEST = "test('a', () => { expect(h()).toBe(1); });\n"
DISPATCH = ("import functools\nimport unittest\n\n\n@functools.singledispatch\ndef fmt(v):\n    return 'any'\n\n\n"
            "@fmt.register\ndef _(v: int):\n    return 'int'\n\n\n@fmt.register\ndef _(v: str):\n    return 'str'\n\n\n")
HOOKS = ("const state = {};\n\ndescribe('a', () => {\n  beforeEach(() => {\n    state.n = 1;\n  });\n"
         "  test('a one', () => { expect(state.n).toBe(1); });\n});\n\n"
         "describe('b', () => {\n  beforeEach(() => {\n    state.n = 1;\n  });\n"
         "  test('b one', () => { expect(state.n).toBe(1); });\n});\n")


class SameNameGroupTests(unittest.TestCase):
    # helpers are matched by class, name and place among those of the same name: a helper the change adds before
    # others of its name moves each of them one place on, so each would come back as its neighbor's base text (an int
    # registration rebuilt as the str one). A group of same-name helpers the change touches, with more than one on
    # either side, cannot be told apart, so the module's tests cannot be proven
    def test_a_changed_group_of_same_name_helpers_cannot_be_proven(self):
        cases = {
            "a singledispatch registration added first": (
                "tests/test_a.py", DISPATCH + PY_TEST,
                DISPATCH.replace("@fmt.register\ndef _(v: int)",
                                 "@fmt.register\ndef _(v: float):\n    return 'float'\n\n\n@fmt.register\ndef _(v: int)") +
                PY_TEST + "\n    def test_float(self):\n        self.assertEqual(fmt(1.5), 'float')\n"),
            "an augmented assignment added before another": (
                "tests/test_a.py", "import unittest\n\nCASES = [1]\nCASES += [2]\n\n\n" + PY_TEST,
                "import unittest\n\nCASES = [1]\nCASES += [3]\nCASES += [2]\n\n\n" + PY_TEST +
                "\n    def test_cases(self):\n        self.assertEqual(sorted(CASES), [1, 2, 3])\n"),
            "a static method added before a same-name method": (
                "tests/a.test.js", "class Fake {\n  get() {\n    return 1;\n  }\n}\n\n" + JS_TEST,
                "class Fake {\n  static get() {\n    return 7;\n  }\n\n  get() {\n    return 1;\n  }\n}\n\n" + JS_TEST +
                "test('static', () => { expect(Fake.get()).toBe(7); });\n"),
            "a hook added before two identical ones": (
                "tests/a.test.js", HOOKS,
                "const state = {};\n\ndescribe('new', () => {\n  beforeEach(() => {\n    state.n = 1;\n    state.m = 5;\n"
                "  });\n  test('new five', () => { expect(state.m).toBe(5); });\n});\n\n" + HOOKS[len("const state = {};\n\n"):]),
            # a test shares the place count of the helpers of its title: put before the helper, it takes its place
            "a test titled like a changed helper, put before it": (
                "tests/a.test.js", "function h() {\n  return 1;\n}\n\n" + JS_TEST,
                "test('h', () => { expect(h()).toBe(1); });\n\nfunction h() {\n  return 2 - 1;\n}\n\n" + JS_TEST),
        }
        for name, (f, before, after) in cases.items():
            with self.subTest(name):
                self.assertIs(testx._SUPPORT_TANGLED, support_text(f, before, after))
        # a group the change leaves as it was still lets another helper come back as its base text, and so does the
        # one helper of a name with a test of that name after it
        before = DISPATCH + "def h():\n    return 1\n\n\n" + PY_TEST
        self.assertIn("def h():\n    return 1\n", support_text("tests/test_a.py", before,
                                                               before.replace("    return 1\n", "    return 2 - 1\n")))
        before = "function h() {\n  return 1;\n}\n\n" + JS_TEST
        self.assertIn("function h() {\n  return 1;\n}\n", support_text(
            "tests/a.test.js", before, before.replace("return 1;", "return 2 - 1;") +
            "test('h', () => { expect(h()).toBe(1); });\n"))


class RemovedNameTests(unittest.TestCase):
    # a helper rebuilt from its base text must still find each name it reads that the module gave it outside the
    # helpers: one bound in an if or a try block, by an expression statement or by a destructuring declaration, which
    # the change removed, fails on that name without the change, whatever the change does
    def test_a_rebuilt_helper_that_reads_a_name_the_change_removed_cannot_be_proven(self):
        cases = {
            "bound in a try block": (
                "tests/test_a.py",
                "import unittest\n\ntry:\n    import json\n    HAVE = True\nexcept ImportError:\n    HAVE = False\n\n\n"
                "def h():\n    return 1 if HAVE else 1\n\n\n" + PY_TEST,
                "import unittest\nimport json\n\n\ndef h():\n    return 1\n\n\n" + PY_TEST),
            "bound in an if block": (
                "tests/test_a.py", "import unittest\n\nif True:\n    LIMIT = 1\n\n\ndef h():\n    return LIMIT\n\n\n" + PY_TEST,
                "import unittest\n\n\ndef h():\n    return 1\n\n\n" + PY_TEST),
            "an attribute set in an if block": (
                "tests/test_a.py", "import types\nimport unittest\n\nNS = types.SimpleNamespace()\nif True:\n"
                                   "    NS.limit = 1\n\n\ndef h():\n    return NS.limit\n\n\n" + PY_TEST,
                "import types\nimport unittest\n\nNS = types.SimpleNamespace()\n\n\ndef h():\n    return 1\n\n\n" + PY_TEST),
            "set on globalThis": (
                "tests/a.test.js", "globalThis.LIMIT = 1;\n\nfunction h() {\n  return LIMIT;\n}\n\n" + JS_TEST,
                "function h() {\n  return 1;\n}\n\n" + JS_TEST),
            "a destructured const": (
                "tests/a.test.js", "const { LIMIT } = { LIMIT: 1 };\n\nfunction h() {\n  return LIMIT;\n}\n\n" + JS_TEST,
                "function h() {\n  return 1;\n}\n\n" + JS_TEST),
        }
        for name, (f, before, after) in cases.items():
            with self.subTest(name):
                self.assertIs(testx._SUPPORT_TANGLED, support_text(f, before, after))
        # a name the change keeps, or one the rebuilt helpers give each other, lets them come back
        before = "import unittest\n\nif True:\n    LIMIT = 1\n\n\ndef h():\n    return LIMIT\n\n\n" + PY_TEST
        self.assertIn("    return LIMIT\n", support_text("tests/test_a.py", before,
                                                       before.replace("return LIMIT", "return LIMIT + 0")))
        before = fake_module()
        self.assertIn("        self.y = 1\n", support_text("tests/test_calc.py", before,
                                                         before.replace("self.y", "self.z")))
        # an attribute read is no binding: os.sep stays os's although the test that also read it changed
        before = ("import os\nimport unittest\n\n\ndef slash():\n    return os.sep\n\n\nclass T(unittest.TestCase):\n"
                  "    def test_a(self):\n        self.assertEqual(os.sep, slash())\n")
        self.assertIn("    return os.sep\n", support_text("tests/test_a.py", before, before.replace(
            "    return os.sep\n", "    return os.sep + ''\n").replace("assertEqual(os.sep, slash())", "assertTrue(slash())")))


class AddedClassReachTests(unittest.TestCase):
    # a rebuilt helper that, with the change, makes an object of a class the change adds: a test that reads a member of
    # that class through the helper fails on it without the change, as on a name the helper edit gives, so it cannot
    # be proven
    def test_a_test_that_reads_a_member_of_a_class_only_the_helper_edit_names_cannot_be_proven(self):
        before = fake_module("class Old:\n    pass\n\n\ndef make():\n    return Old()\n\n\n")
        after = before.replace("def make():\n    return Old()",
                               "class New:\n    def extra(self):\n        return 1\n\n\ndef make():\n    return New()") + \
            "\n    def test_extra(self):\n        self.assertEqual(make().extra(), 1)\n"
        rc, rows, text = prove_rows(before, after)
        self.assertEqual([("test_calc.CalcTests.test_extra", testx._HELPERS_TANGLED)], rows, text)
        before = "class Old {}\n\nfunction make() {\n  return new Old();\n}\n\n" + JS_TEST
        after = before.replace("function make() {\n  return new Old();",
                               "class Neu {\n  extra() {\n    return 1;\n  }\n}\n\nfunction make() {\n  return new Neu();") + \
            "test('extra', () => { expect(make().extra()).toBe(1); });\n"
        self.assertEqual({"extra"}, testx._reads_undone("tests/a.test.js",
                                                        testx._rebuild_helpers("tests/a.test.js", before, after)))


class ImportedNameTests(unittest.TestCase):
    # a name an import binds in the rebuilt module means the same with and without the change: a test that names it
    # still proves a helper fix that starts to use it (str(x) to json.dumps(x), json imported on both sides)
    def test_a_helper_fix_that_reads_a_module_both_sides_import_proves(self):
        before = fake_module("import json\n\n\ndef h(x):\n    return str(x)\n\n\n")
        after = before.replace("return str(x)", "return json.dumps(x)") + \
            "\n    def test_h(self):\n        self.assertEqual(json.loads(h('a')), 'a')\n"
        rc, rows, text = prove_rows(before, after)
        self.assertEqual([("test_calc.CalcTests.test_h", "proves the change")], rows, text)


LIMIT_IF = "import unittest\n\nif True:\n    LIMIT = 1\n\n\ndef h():\n    return LIMIT\n\n\n"
H_ONE = "def h():\n    return 1\n\n\n"
JS_H_ONE = "function h() {\n  return 1;\n}\n\n"
JS_H_LIMIT = "function h() {\n  return LIMIT;\n}\n\n"


class ScopeStatementTests(unittest.TestCase):
    # only helpers come back: a statement of a test module outside its tests, helpers and imports keeps the change.
    # One the change adds, removes or edits that names a name a rebuilt helper reads leaves that helper another scope
    # than at the base (a value it read gone, a name deleted, a registry left empty), whatever binds the name now (a
    # test's local, a parameter, a default kept), so the module's tests cannot be proven
    def test_a_changed_statement_that_names_what_a_rebuilt_helper_reads_cannot_be_proven(self):
        cases = {
            "a try-bound name another test keeps as a local": (
                "tests/test_a.py",
                "import unittest\n\ntry:\n    import os\n    root = os.sep\nexcept ImportError:\n    root = None\n\n\n"
                "def fixture(name):\n    return root and name\n\n\n" + PY_TEST +
                "\n    def test_tree(self):\n        root = 'x'\n        self.assertEqual(root, 'x')\n",
                "import unittest\n\n\ndef fixture(name):\n    return name\n\n\n" + PY_TEST +
                "\n    def test_tree(self):\n        root = 'x'\n        self.assertEqual(root, 'x')\n"
                "\n    def test_fixture(self):\n        self.assertEqual(fixture('a'), 'a')\n"),
            "a removed name kept as a parameter": (
                "tests/test_a.py", LIMIT_IF + PY_TEST,
                "import unittest\n\n\n" + H_ONE + "def twice(LIMIT):\n    return 2 * LIMIT\n\n\n" + PY_TEST),
            "a default kept while the if that set the value goes": (
                "tests/test_a.py", LIMIT_IF.replace("if True:", "LIMIT = None\nif True:") + PY_TEST,
                "import unittest\n\nLIMIT = None\n\n\n" + H_ONE + PY_TEST),
            "a registry fill removed": (
                "tests/test_a.py",
                "import unittest\n\nREG = {}\nREG.update(limit=1)\n\n\ndef h():\n    return REG['limit']\n\n\n" + PY_TEST,
                "import unittest\n\nREG = {}\n\n\n" + H_ONE + PY_TEST),
            "a setattr removed": (
                "tests/test_a.py",
                "import types\nimport unittest\n\nNS = types.SimpleNamespace()\nsetattr(NS, 'limit', 1)\n\n\n"
                "def h():\n    return NS.limit\n\n\n" + PY_TEST,
                "import types\nimport unittest\n\nNS = types.SimpleNamespace()\n\n\n" + H_ONE + PY_TEST),
            "a del added": (
                "tests/test_a.py", LIMIT_IF + PY_TEST,
                LIMIT_IF.replace("\n\n\ndef h", "\ndel LIMIT\n\n\ndef h").replace("return LIMIT", "return 1") + PY_TEST),
            "an assignment removed beside the declaration it kept": (
                "tests/a.test.js", "let LIMIT;\nLIMIT = 1;\n\n" + JS_H_LIMIT + JS_TEST, "let LIMIT;\n\n" + JS_H_ONE + JS_TEST),
            "a global set by its name in brackets": (
                "tests/a.test.js", 'globalThis["LIMIT"] = 1;\n\n' + JS_H_LIMIT + JS_TEST, JS_H_ONE + JS_TEST),
            "a global defined by Object.defineProperty": (
                "tests/a.test.js", 'Object.defineProperty(globalThis, "LIMIT", { value: 1 });\n\n' + JS_H_LIMIT + JS_TEST,
                JS_H_ONE + JS_TEST),
            "a removed global another test keeps as a local": (
                "tests/a.test.js", "globalThis.LIMIT = 1;\n\n" + JS_H_LIMIT + JS_TEST,
                JS_H_ONE + JS_TEST + "test('b', () => { const LIMIT = 2; expect(LIMIT).toBe(2); });\n"),
            "one line edited in a statement over several lines": (
                "tests/a.test.js",
                'Object.defineProperty(globalThis, "LIMIT", {\n  value: 1,\n});\n\n' + JS_H_LIMIT + JS_TEST,
                'Object.defineProperty(globalThis, "LIMIT", {\n  value: 2,\n});\n\n' + JS_H_ONE + JS_TEST),
        }
        if sys.version_info >= (3, 10):  # Python reads a match statement since 3.10
            cases["a match capture removed"] = (
                "tests/test_a.py",
                "import unittest\n\nmatch 1:\n    case LIMIT:\n        pass\n\n\ndef h():\n    return LIMIT\n\n\n" + PY_TEST,
                "import unittest\n\n\n" + H_ONE + PY_TEST)
        for name, (f, before, after) in cases.items():
            with self.subTest(name):
                self.assertIs(testx._SUPPORT_TANGLED, support_text(f, before, after))
        # a statement whose change names nothing a rebuilt helper reads lets it come back as its base text: a docstring,
        # a comment, a describe titled like a name the helper reads, another call
        before = '"""Tests of h."""\n' + LIMIT_IF.replace("import unittest\n", "import sys\nimport unittest\n\n"
                                                         "sys.path.insert(0, 'a')\n") + PY_TEST
        after = before.replace("Tests of h.", "Tests of h and LIMIT.").replace("'a'", "'b'") \
            .replace("return LIMIT", "return LIMIT + 0")
        self.assertIn("    return LIMIT\n", support_text("tests/test_a.py", before, after))
        before = "// one limit\nglobalThis.LIMIT = 1;\n\n" + JS_H_LIMIT + JS_TEST
        after = before.replace("// one limit", "// the LIMIT is one").replace("return LIMIT;", "return LIMIT + 0;") + \
            "describe('LIMIT', () => {\n  test('c', () => { expect(h()).toBe(1); });\n});\n"
        self.assertIn("  return LIMIT;\n", support_text("tests/a.test.js", before, after))

    # a helper the change adds at module level that binds a name a rebuilt helper reads changes its scope the same way:
    # the helper reads the new binding (an assignment after the one it read, a function named like a built-in it calls)
    def test_an_added_helper_that_binds_what_a_rebuilt_helper_reads_cannot_be_proven(self):
        cases = {
            "an assignment after the if": (
                "tests/test_a.py", LIMIT_IF + PY_TEST,
                LIMIT_IF.replace("return LIMIT", "return 1") + "LIMIT = 5\n\n\n" + PY_TEST),
            "a function named like a built-in": (
                "tests/test_a.py", "import unittest\n\n\ndef h():\n    return len('a')\n\n\n" + PY_TEST,
                "import unittest\n\n\ndef len(x):\n    return 7\n\n\ndef h():\n    return 1\n\n\n" + PY_TEST),
            "a function named like a global": (
                "tests/a.test.js", "function h() {\n  return parseInt('1');\n}\n\n" + JS_TEST,
                "function parseInt(x) {\n  return 7;\n}\n\n" + JS_H_ONE + JS_TEST),
        }
        for name, (f, before, after) in cases.items():
            with self.subTest(name):
                self.assertIs(testx._SUPPORT_TANGLED, support_text(f, before, after))
        # a helper the change adds that binds another name, or one that only moved, lets the rebuilt helper come back
        before = LIMIT_IF + PY_TEST
        self.assertIn("    return LIMIT\n", support_text("tests/test_a.py", before, before.replace(
            "return LIMIT", "return LIMIT + 0") + "OTHER = 5\n\n\n"))


FAKE_PROPERTY = ("class Fake:\n    def __init__(self):\n        self._x = 1\n\n    @property\n    def x(self):\n"
                 "        return self._x\n\n    @x.setter\n    def x(self, v):\n        self._x = v\n\n\n")
NODE_HEAD = 'import assert from "node:assert/strict";\nimport { test, describe, beforeEach } from "node:test";\n\n'
HOOK_A = ('describe("a", () => {\n  beforeEach(() => {\n    state.n = 1;\n  });\n'
          '  test("a one", () => assert.equal(state.n, 1));\n});\n\n')
HOOK_B = ('describe("b", () => {\n  beforeEach(() => {\n    state.n = 0;\n  });\n'
          '  test("b one", () => assert.equal(typeof state.n, "number"));\n});\n')
NODE_PACKAGE = json.dumps({"name": "root", "type": "module", "scripts": {"test": "node --test test/"}})


class SameShapeGroupTests(unittest.TestCase):
    # helpers of one class and name whose kinds keep their places on both sides (a getter beside its setter, a
    # beforeEach in each of two describes) are matched place by place, so a fix in one comes back as its base text and
    # is proven. A group whose shape changes, or whose helpers trade texts, still cannot be told apart
    def test_a_property_getter_fix_beside_its_setter_proves(self):
        before = fake_module(FAKE_PROPERTY)
        after = before.replace("        return self._x\n", "        return self._x * 10\n") + \
            "\n    def test_x(self):\n        self.assertEqual(Fake().x, 10)\n"
        rc, rows, text = prove_rows(before, after)
        self.assertEqual([("test_calc.CalcTests.test_x", "proves the change")], rows, text)
        self.assertEqual(0, rc, text)

    @unittest.skipUnless(shutil.which("node"), "node is not installed, so the node runner cannot run here")
    def test_a_fix_in_one_of_two_same_name_hooks_proves(self):
        before = NODE_HEAD + "const state = {};\n\n" + HOOK_A + HOOK_B
        after = before.replace("state.n = 0;", "state.n = 2;").replace(
            '"number"));\n', '"number"));\n  test("b two", () => assert.equal(state.n, 2));\n')
        with Env() as e:
            r = e.repo({"package.json": NODE_PACKAGE, "test/util.test.mjs": before})
            edit(r, {"test/util.test.mjs": after})
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
        self.assertEqual({"test/util.test.mjs::a > a one": "does not test the change",
                          "test/util.test.mjs::b > b one": "does not test the change",
                          "test/util.test.mjs::b > b two": "proves the change"},
                         {t["id"]: t["verdict"] for t in json.loads(out)["tests"]}, out + err)
        # the describes trade places: each hook would come back as the other's base text; a third hook is a new shape
        traded = NODE_HEAD + "const state = {};\n\n" + HOOK_B + "\n" + HOOK_A
        self.assertIs(testx._SUPPORT_TANGLED, support_text("test/util.test.mjs", before, traded))
        third = after + '\ndescribe("c", () => {\n  beforeEach(() => {\n    state.n = 3;\n  });\n' \
                        '  test("c one", () => assert.equal(state.n, 3));\n});\n'
        self.assertIs(testx._SUPPORT_TANGLED, support_text("test/util.test.mjs", before, third))

    # a hook's describe is part of its place: the hook of a describe the change puts in another's place is a hook the
    # change adds, so the group's shape changes, rather than the new hook coming back as the old one's base text
    def test_a_hook_in_another_describe_takes_no_place_of_the_old_one(self):
        before = NODE_HEAD + "const state = {};\n\n" + HOOK_A + HOOK_B
        after = before.replace('describe("b"', 'describe("c"').replace("state.n = 0;", "state.n = 5;")
        self.assertIs(testx._SUPPORT_TANGLED, support_text("test/util.test.mjs", before, after))
        nested = before.replace(HOOK_B, 'describe("outer", () => {\n' + HOOK_B + "});\n")
        self.assertIs(testx._SUPPORT_TANGLED, support_text("test/util.test.mjs", before,
                                                           nested.replace("state.n = 0;", "state.n = 5;")))
        # the same describes keep their hooks' places
        self.assertIn("    state.n = 0;\n", support_text("test/util.test.mjs", nested,
                                                       nested.replace("state.n = 0;", "state.n = 5;")))


class NamesakeTestTests(unittest.TestCase):
    # a test titled like a helper shares the place count of that name's helpers. Put before a helper the change leaves
    # as it was, it leaves nothing of that helper to undo: the module runs as it is, or its other changed helpers come
    # back as their base text
    def test_a_namesake_test_before_an_unchanged_helper_leaves_nothing_to_undo(self):
        before = "function h() {\n  return 1;\n}\n\nfunction g() {\n  return 1;\n}\n\n" + JS_TEST
        after = "test('h', () => { expect(value()).toBe(2); });\n\n" + before
        self.assertIsNone(support_text("tests/a.test.js", before, after))
        self.assertIn("function g() {\n  return 1;\n}\n",
                      support_text("tests/a.test.js", before, after.replace("g() {\n  return 1;", "g() {\n  return 2 - 1;")))


class DynamicMemberTests(unittest.TestCase):
    # a rebuilt helper that now reaches code the change adds whose members are set from strings or expressions
    # (setattr, type() with a dict, a class expression, Object.assign, Object.defineProperty, a statement outside the
    # helpers) makes what it gives unknown: a test that reaches that helper, by its name or because the framework
    # calls it, cannot be proven
    def test_a_test_that_reaches_an_added_class_built_from_strings_or_expressions_cannot_be_proven(self):
        old = "import unittest\n\n\nclass Old:\n    pass\n\n\ndef make():\n    return Old()\n\n\n"
        extra = "\n    def test_extra(self):\n        self.assertEqual(make().extra(), 1)\n"
        py = {
            "a setattr in a decorator": "def with_extra(cls):\n    setattr(cls, 'extra', lambda self: 1)\n    return cls"
                                        "\n\n\n@with_extra\nclass New:\n    pass\n\n\n",
            "type() with a dict of members": "New = type('New', (), {'extra': lambda self: 1})\n\n\n",
        }
        for name, added in py.items():
            with self.subTest(name):
                after = old.replace("def make", added + "def make").replace("return Old()", "return New()") + PY_TEST + extra
                r = testx._rebuild_helpers("tests/test_a.py", old + PY_TEST, after)
                self.assertEqual({"T.test_extra"}, testx._reads_undone("tests/test_a.py", r))
        old = "class Old {}\n\nfunction make() {\n  return new Old();\n}\n\n"
        js = {
            "a class expression set on globalThis": (
                "globalThis.Neu = class {\n  extra() {\n    return 1;\n  }\n};\n\n", "globalThis.Neu"),
            "a class passed to Object.assign": (
                "const reg = {};\nObject.assign(reg, { Neu: class {\n  extra() {\n    return 1;\n  }\n} });\n\n", "reg.Neu"),
            "a member defined by Object.defineProperty": (
                "const Neu = class {};\nObject.defineProperty(Neu.prototype, 'extra', { value() { return 1; } });\n\n", "Neu"),
            "a class expression held in a const": ("const Neu = class {\n  extra() {\n    return 1;\n  }\n};\n\n", "Neu"),
        }
        for name, (added, made) in js.items():
            with self.subTest(name):
                after = old.replace("function make", added + "function make").replace("new Old()", f"new {made}()") + \
                    JS_TEST + "test('extra', () => { expect(make().extra()).toBe(1); });\n"
                r = testx._rebuild_helpers("tests/a.test.js", old + JS_TEST, after)
                self.assertEqual({"extra"}, testx._reads_undone("tests/a.test.js", r))
        # a hook the framework calls reaches every test it runs for
        before = "class Old {}\n\nconst state = {};\n\nbeforeEach(() => {\n  state.obj = new Old();\n});\n\n" + JS_TEST
        after = before.replace("const state", "globalThis.Neu = class {\n  extra() {\n    return 1;\n  }\n};\n\n"
                                              "const state").replace("new Old()", "new globalThis.Neu()") + \
            "test('extra', () => { expect(state.obj.extra()).toBe(1); });\n"
        r = testx._rebuild_helpers("tests/a.test.js", before, after)
        self.assertEqual({"a", "extra"}, testx._reads_undone("tests/a.test.js", r))
        # an added helper whose members are all declared still lets a test that reads none of them prove
        before = "import unittest\n\n\ndef h(x):\n    return str(x)\n\n\n" + PY_TEST
        after = before.replace("def h", "def quote(x):\n    return repr(x)\n\n\ndef h").replace("str(x)", "quote(x)") + \
            "\n    def test_q(self):\n        self.assertEqual(h('a'), \"'a'\")\n"
        self.assertEqual(set(), testx._reads_undone("tests/test_a.py", testx._rebuild_helpers("tests/test_a.py", before,
                                                                                             after)))


class JsReaderCostTests(unittest.TestCase):
    # the JS readers run on every changed test module, so blanks after `function`, `function*`, a call, a parameter
    # list, an assignment or a quote must cost them linear time. A child process reads 40,000 blanks after each of those, about 1 s in all here,
    # and is stopped after 20 s: a pattern that backtracks over the blanks reads one such text in 30 s or more (a cubic
    # one in hours), so the bound tells them apart on a loaded machine too
    def test_the_js_readers_read_a_long_blank_run_in_linear_time(self):
        code = ("import sys\nsys.path.insert(0, sys.argv[1])\nfrom devkit import testx\n"
                "for start in ('function', 'function*', 'function f', 'export function', 'x.function', 'catch', 'f()',"
                " 'f(): T', 'x =', 'x[0]', 'describe', 'describe(', \"'\"):\n"
                "    for blank in (' ', '\\t'):\n"
                "        text = start + blank * 40000 + ';\\n'\n"
                "        testx._js_local(text), testx._js_names(text), testx._js_gives(text)\n"
                "        testx._js_support(text), testx._js_dynamic(text), testx._js_describes(text)\n"
                "        testx._scope('a.test.js', text, testx._js_entries(text))\n"
                "print('read')\n")
        lib = str(Path(__file__).resolve().parents[1] / "lib")
        try:
            p = subprocess.run([sys.executable, "-I", "-c", code, lib], capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            self.fail("the JS readers took more than 20 s on 40,000 blanks: a pattern backtracks over them")
        self.assertEqual("read", p.stdout.strip(), p.stderr)


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

    # a helper method added to a test class with three tests, which overrides the one a base class gave: the one test
    # that calls it changes, without a line of its own changing; the other two do not
    def test_a_helper_added_to_a_test_class_selects_only_the_test_that_calls_it(self):
        before = PY_SHARED.replace("class Shared(unittest.TestCase):", "class Base:\n    def one(self):\n"
                                   "        return 1\n\n\nclass Shared(Base, unittest.TestCase):").replace(
            "self.assertEqual(self.base, 1)", "self.assertEqual(self.base, self.one())")
        after = before.replace("    def double(self, x):", "    def one(self):\n        return 2 - 1\n\n"
                                                         "    def double(self, x):")
        self.assertEqual(["Shared.test_plain"], changed(before, after))

    # a helper added that no test calls yet changes no test: none counts as changed, not every test of the file
    def test_a_helper_added_that_no_test_calls_selects_no_test(self):
        after = PY_SHARED.replace("    def double(self, x):", "    def one(self):\n        return 1\n\n"
                                                             "    def double(self, x):")
        self.assertEqual([], changed(PY_SHARED, after))
        # a changed import outside every test and helper still counts the whole file
        self.assertIsNone(changed(PY_SHARED, PY_SHARED.replace("import unittest\n", "import unittest\nimport os\n")))

    # the same at prove: the test module whose only change is a helper no test calls is left out, so its unchanged
    # test does not show as "does not test the change"
    def test_prove_leaves_out_a_test_module_whose_change_reaches_no_test(self):
        with Env() as e:
            r = e.repo(PROVE_BASE)
            helper = with_tests().replace("class CalcTests(unittest.TestCase):\n",
                                          "class CalcTests(unittest.TestCase):\n    def two(self):\n        return 2\n\n")
            mul = with_tests().replace("CalcTests", "MulTests").replace(
                "    def test_add(self):\n        self.assertEqual(calc.add(1, 2), 3)\n", TEST_MUL.lstrip("\n"))
            edit(r, {"tests/test_calc.py": helper, "tests/test_mul.py": mul, "calc.py": MUL})
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            data = json.loads(out)
        self.assertEqual([("test_mul.MulTests.test_mul", "proves the change")],
                         [(x["id"], x["verdict"]) for x in data["tests"]], out + err)
        self.assertEqual(["tests/test_calc.py"], data["no_changed_tests"])
        self.assertEqual(0, rc)

    def test_a_changed_module_function_selects_the_tests_that_call_it(self):
        before = PY_SHARED.replace("class Shared", "def make():\n    return 5\n\n\nclass Shared").replace(
            "self.assertEqual(self.base, 1)", "self.assertEqual(make(), 5)")
        after = before.replace("return 5", "return 2 + 3")
        self.assertEqual(["Shared.test_plain"], changed(before, after))


FRAMEWORK = {  # name: (a test module with a helper the test framework calls, the edit of that helper)
    "a test class's run()": ("""
        import unittest


        class Shared(unittest.TestCase):
            def run(self, result=None):
                self.flag = 1
                return super().run(result)

            def test_a(self):
                self.assertEqual(self.flag, 1)


        class Other(unittest.TestCase):
            def test_b(self):
                self.assertTrue(True)
        """, ("self.flag = 1", "self.flag = 2"), ["Shared.test_a"]),
    "a test class's __init__": ("""
        import unittest


        class Shared(unittest.TestCase):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.flag = 1

            def test_a(self):
                self.assertEqual(self.flag, 1)
        """, ("self.flag = 1", "self.flag = 2"), ["Shared.test_a"]),
    "a fixture found by its name= alias": ("""
        import pytest


        @pytest.fixture(name="client")
        def _make_client():
            return 1


        def test_a(client):
            assert client == 1
        """, ("return 1", "return 2"), ["test_a"]),
    "pytestmark": ("""
        import pytest

        pytestmark = pytest.mark.skipif(False, reason="x")


        def test_a():
            assert 1 == 1
        """, ("skipif(False", "skipif(True"), ["test_a"]),
    "load_tests": ("""
        import unittest


        def load_tests(loader, tests, pattern):
            return tests


        class Shared(unittest.TestCase):
            def test_a(self):
                self.assertTrue(True)
        """, ("return tests", "return unittest.TestSuite(tests)"), ["Shared.test_a"]),
    "a pytest hook": ("""
        def pytest_generate_tests(metafunc):
            pass


        def test_a():
            assert 1 == 1
        """, ("    pass", "    metafunc.config"), ["test_a"]),
}


class FrameworkCalledHelperTests(unittest.TestCase):
    # a helper the test framework calls, which no test names: a change to it changes every test it runs for
    def test_a_changed_helper_the_framework_calls_reaches_every_test_it_runs_for(self):
        for name, (before, (old, new), tests) in FRAMEWORK.items():
            with self.subTest(name):
                self.assertEqual(tests, changed(before, textwrap.dedent(before).lstrip("\n").replace(old, new)))

    def test_an_added_helper_the_framework_calls_reaches_every_test_it_runs_for(self):
        before, _, tests = FRAMEWORK["a test class's run()"]
        before = textwrap.dedent(before).lstrip("\n")
        self.assertEqual(tests, changed(before.replace("    def run(self, result=None):\n        self.flag = 1\n"
                                                       "        return super().run(result)\n\n", ""), before))

    # a helper that was there and changed, which no test names: it may be reached in ways no name shows (getattr, a
    # base class), so every test of the file counts; only a helper the change adds that no test names reaches none
    def test_a_changed_helper_no_test_names_counts_every_test(self):
        before = PY_SHARED.replace("    def test_double", "    def spare(self):\n        return 1\n\n    def test_double")
        self.assertIsNone(changed(before, before.replace("        return 1\n", "        return 2\n")))



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

    # a test file whose own rerun stops before it loads (here: it fails on a second import in the same copy) left no
    # record of what it imports: it has no record, rather than a record of imports that came from elsewhere
    def test_a_test_file_whose_own_rerun_does_not_load_has_no_record(self):
        child = ("import subprocess\nimport sys\nimport unittest\n\nfrom pkg import mod\n\n\n"
                 "class ChildTests(unittest.TestCase):\n    def test_one(self):\n        self.assertEqual(1, 1)\n")
        once = ("import os\nimport unittest\nfrom pathlib import Path\n\nMARK = Path(__file__).with_name('ran.marker')\n"
                "if MARK.exists() and '-imports-' in os.environ.get('PYTHONPATH', ''):\n"
                "    raise RuntimeError('loaded a second time')\nMARK.touch()\nfrom pkg import mod  # noqa: E402\n\n\n"
                "class OnceTests(unittest.TestCase):\n    def test_one(self):\n        self.assertEqual(1, 1)\n")
        with Env() as e:
            r = e.repo(dict(SRC_PKG, **{"tests/test_child.py": child, "tests/test_once.py": once,
                                        ".gitignore": SRC_PKG[".gitignore"] + "ran.marker\n"}))
            (site_packages(r) / "_pkg_editable.pth").write_text(f"{r / 'src'}\n")
            src_change(r)
            edit(r, {"tests/test_child.py": child + "\n    def test_two(self):\n        subprocess.run([sys.executable, "
                                                    "'-I', '-c', 'pass'])\n        self.assertEqual(mod.value(), 2)\n",
                     "tests/test_once.py": once + TEST_TWO})
            rc, data, text = self.prove(r)
            self.assertEqual(0, rc, text)
            self.assertEqual({("tests.test_child.ChildTests.test_two", testx._NO_IMPORT_RECORD),
                              ("tests.test_once.OnceTests.test_two", testx._NO_IMPORT_RECORD),
                              ("tests.test_mod.ModTests.test_two", "proves the change")},
                             {(t["id"], t["verdict"]) for t in data["tests"]})

    # a JavaScript test file has no import record to read: the per-file rerun runs only the Python test files
    @unittest.skipUnless(shutil.which("node"), "node is not installed, so the node runner cannot run here")
    def test_the_per_file_rerun_runs_only_python_test_files(self):
        child = ("import subprocess\nimport sys\nimport unittest\n\nfrom pkg import mod\n\n\n"
                 "class ChildTests(unittest.TestCase):\n    def test_one(self):\n        self.assertEqual(1, 1)\n")
        js = ('import assert from "node:assert/strict";\nimport { test } from "node:test";\n\n'
              'test("one", () => assert.equal(1, 1));\n')
        with Env() as e:
            r = e.repo(dict(SRC_PKG, **{"tests/test_child.py": child, "js/one.test.mjs": js,
                                        "package.json": json.dumps({"name": "x", "type": "module",
                                                                    "scripts": {"test": "node --test js/"}})}))
            (site_packages(r) / "_pkg_editable.pth").write_text(f"{r / 'src'}\n")
            src_change(r)
            edit(r, {"tests/test_child.py": child + "\n    def test_two(self):\n        subprocess.run([sys.executable, "
                                                    "'-I', '-c', 'pass'])\n        self.assertEqual(mod.value(), 2)\n",
                     "js/one.test.mjs": js + 'test("two", () => assert.equal(2, 2));\n'})
            with mock.patch.object(testx, "run_tests", wraps=testx.run_tests) as runs:
                rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            reruns = [c.args[1]["lang"] for c in runs.call_args_list
                      if "-imports-" in (c.kwargs.get("env") or {}).get("PYTHONPATH", "")]
            self.assertTrue(reruns, out + err)  # the Python files did run again, each with its own record
            self.assertEqual({"py"}, set(reruns), out + err)

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

    # an open that empties the file first (w+, O_RDWR|O_TRUNC) reads none of its old content; a read after a write of
    # the same file is still a read, and two spellings of one file are one read
    def test_only_an_open_that_keeps_the_old_content_is_a_read(self):
        cases = {  # name: (the opens, whether the data file counts as read)
            "w+": ("open(p, 'w+').close()", False),
            "O_RDWR|O_TRUNC": ("os.close(os.open(p, os.O_RDWR | os.O_TRUNC))", False),
            "r+": ("open(p, 'r+').close()", True),
            "a+": ("open(p, 'a+').close()", True),
            "O_RDWR": ("os.close(os.open(p, os.O_RDWR))", True),
            "w, then r": ("open(p, 'w').close(); open(p).close()", True),
            "r by two spellings": ("open(p).close(); open(os.path.join(os.path.dirname(p), '.', 'data.json')).close()",
                                   True),
        }
        for name, (opens, read) in cases.items():
            with self.subTest(name), Env() as e:
                data = e.root / "copy" / "data.json"
                data.parent.mkdir(parents=True)
                data.write_text("{}")
                items, _ = self.record(e, f"import os\np = {str(data)!r}\n{opens}\n")
                reads = [x["read"] for x in items if isinstance(x, dict) and "read" in x]
                self.assertEqual([os.path.realpath(data)] if read else [], reads, opens)

    # each way a test starts a Python that skips the recorder is written down as unrecorded; the same commands
    # without the switch that skips it are not
    def unrecorded(self, e, call):
        code = f"import os, shlex, subprocess, sys\nexe = sys.executable\n{call}\n"
        return [x["unrecorded"] for x in self.record(e, code)[0] if isinstance(x, dict)]

    def test_a_python_child_is_flagged_behind_each_wrapper_only_when_it_skips_the_recorder(self):
        true = shutil.which("true")
        cases = {  # name: (the call, whether its child skips the recorder)
            "-W value": ("subprocess.run([exe, '-W', 'ignore', '-I', '-c', 'pass'])", True),
            "-X value": ("subprocess.run([exe, '-X', 'utf8', '-I', '-c', 'pass'])", True),
            "executable=": ("subprocess.run(['tool', '-I', '-c', 'pass'], executable=exe)", True),
            "env": ("subprocess.run(['/usr/bin/env', exe, '-I', '-c', 'pass'])", True),
            "env NAME=value": ("subprocess.run(['/usr/bin/env', 'A=1', exe, '-I', '-c', 'pass'])", True),
            "env -i": ("subprocess.run(['/usr/bin/env', '-i', exe, '-c', 'pass'])", True),
            "shell PYTHONPATH=": ("subprocess.run('PYTHONPATH= ' + shlex.quote(exe) + ' -c pass', shell=True)", True),
            "bash -lc": (f"subprocess.run(['bash', '-lc', exe + ' -I -c pass'], executable={true!r})", True),
            "sh -ec": (f"subprocess.run(['sh', '-ec', exe + ' -I -c pass'], executable={true!r})", True),
            # the same wrappers around a child that keeps the recorder
            "-W value, kept": ("subprocess.run([exe, '-W', 'ignore', '-c', 'pass'])", False),
            "env NAME=value, kept": ("subprocess.run(['/usr/bin/env', 'A=1', exe, '-c', 'pass'])", False),
            "shell NAME=value, kept": ("subprocess.run('A=1 ' + shlex.quote(exe) + ' -c pass', shell=True)", False),
            "sh -ec, kept": (f"subprocess.run(['sh', '-ec', exe + ' -c pass'], executable={true!r})", False),
            # a runner in front of env: env -i drops the recorder's PYTHONPATH
            "timeout env -i": (f"subprocess.run(['timeout', '30', 'env', '-i', exe, '-c', 'pass'], "
                               f"executable={true!r})", True),
            # echo and printf print their words: they start nothing
            "echo python -I": (f"subprocess.run(['echo', 'python', '-I'], executable={true!r})", False),
            "sh -c printf python -I": (f"subprocess.run(['sh', '-c', 'printf python -I'], executable={true!r})", False),
        }
        for name, (call, skips) in cases.items():
            with self.subTest(name), Env() as e:
                self.assertEqual(int(skips), len(self.unrecorded(e, call)), call)

    # the forms a shell, a program that runs another, and Python's own options take: `bash -c -- line`, an rc file
    # before -c, timeout/nice/xargs given a list, and an option whose value is the next word
    def test_a_python_child_is_flagged_behind_shell_options_runners_and_option_values(self):
        true = shutil.which("true")
        forms = {  # name: the call, with {i} where the child's -I goes
            "bash -c --": "subprocess.run(['bash', '-c', '--', exe + ' {i}-c pass'], executable=%r)",
            "bash --rcfile f -c": "subprocess.run(['bash', '--rcfile', '/dev/null', '-c', exe + ' {i}-c pass'], "
                                  "executable=%r)",
            "bash --init-file f -c": "subprocess.run(['bash', '--init-file', '/dev/null', '-c', exe + ' {i}-c pass'], "
                                     "executable=%r)",
            "timeout list": "subprocess.run(['timeout', '30', exe, {li}'-c', 'pass'], executable=%r)",
            "nice list": "subprocess.run(['nice', '-n', '5', exe, {li}'-c', 'pass'], executable=%r)",
            "xargs list": "subprocess.run(['xargs', exe, {li}'-c'], executable=%r)",
            # a runner in front of a shell, and a shell line whose runner starts another shell
            "timeout bash -c": "subprocess.run(['timeout', '30', 'bash', '-c', exe + ' {i}-c pass'], executable=%r)",
            "nice sh -c": "subprocess.run(['nice', 'sh', '-c', exe + ' {i}-c pass'], executable=%r)",
            "xargs sh -c": "subprocess.run(['xargs', 'sh', '-c', exe + ' {i}-c pass'], executable=%r)",
            "bash -c timeout sh -c": "subprocess.run(['bash', '-c', 'timeout 30 sh -c \"' + exe + ' {i}-c pass\"'], "
                                     "executable=%r)",
            "--check-hash-based-pycs value": "subprocess.run([exe, '--check-hash-based-pycs', 'default', {li}'-c', "
                                             "'pass'], executable=%r)",
            # a command substitution runs its command, whatever the command around it does with the output
            "sh -c echo $( )": "subprocess.run(['sh', '-c', 'echo $( ' + exe + ' {i}-c pass )'], executable=%r)",
            "sh -c echo backticks": "subprocess.run(['sh', '-c', 'echo `' + exe + ' {i}-c pass`'], executable=%r)",
            # in single quotes, $( ) is text that echo prints: only the command after it starts a Python
            "sh -c echo '$( )'": "subprocess.run(['sh', '-c', \"echo '$( \" + exe + \" -I -c pass )'; \" + exe + "
                                 "' {i}-c pass'], executable=%r)",
        }
        for name, form in forms.items():
            for skips in (True, False):
                call = form.replace("{i}", "-I " if skips else "").replace("{li}", "'-I', " if skips else "") % true
                with self.subTest(name, skips=skips), Env() as e:
                    self.assertEqual(int(skips), len(self.unrecorded(e, call)), call)


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
