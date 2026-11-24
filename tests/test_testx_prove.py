# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev test prove`: which tests of a changed test file count as new or changed (testx._changed_units), and how their
results are matched (testx._member). Hermetic: tiny git repositories in temporary folders (test_testx.Env).
Run: python3 -m unittest tests.test_testx_prove -v"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
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


class HelperClassTests(unittest.TestCase):
    # the members of a class without tests (a fake) are read at the class body's own depth
    def test_an_arrow_property_s_if_block_is_no_member_of_a_javascript_class(self):
        before = ("class Fake {\n  close = () => {\n    return 2;\n  };\n  get() {\n    return 1;\n  }\n}\n\n"
                  "test('a', () => { expect(new Fake().get()).toBe(1); });\n")
        after = before.replace("    return 2;\n", "    if (this.y) {\n      return 3;\n    }\n    return 2;\n")
        self.assertNotIn(("Fake", "if"), testx._js_members(after))
        for word in ("for", "while", "switch"):
            self.assertNotIn(("Fake", word), testx._js_members(after.replace("if (this.y)", f"{word} (this.y)")))

    # only a name at the class body's own depth is a member: a call inside an arrow property's body is none
    def test_a_call_inside_an_arrow_property_is_no_member_of_a_javascript_class(self):
        text = ("class Fake {\n  close = () => {\n    cleanup(this.y);\n    return 2;\n  };\n  get() {\n    return 1;\n"
                "  }\n}\n")
        self.assertEqual({("Fake", "close"), ("Fake", "get")}, set(testx._js_members(text)))


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
    # a restore is checked: the rebuilt module must read as the base module with the tests now. Two members on one
    # line cannot be rebuilt one by one, so the module keeps its change
    def test_a_restore_that_does_not_give_back_each_base_helper_cannot_be_proven(self):
        before = ("import unittest\n\n\nclass Fake:\n    x = 1\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n"
                  "        self.assertEqual(Fake.x, 1)\n")
        after = before.replace("    x = 1\n", "    x = 2; z = 3\n") + \
            "\n    def test_z(self):\n        self.assertEqual(Fake.z, 3)\n"
        self.assertIs(testx._SUPPORT_TANGLED, support_text("tests/test_a.py", before, after))

    # imports are code outside the tests: a removed import keeps the module's change
    def test_a_rebuilt_helper_that_needs_a_removed_import_cannot_be_proven(self):
        before = ("import os\nimport unittest\n\n\ndef sep():\n    return os.sep\n\n\nclass T(unittest.TestCase):\n"
                  "    def test_a(self):\n        self.assertTrue(sep())\n")
        after = before.replace("import os\n", "").replace("return os.sep", "return '/'")
        self.assertIs(testx._SUPPORT_TANGLED, support_text("tests/test_a.py", before, after))
        js = ("import { sep } from 'node:path';\n\nfunction where() {\n  return sep;\n}\n\n"
              "test('a', () => { expect(where()).toBeTruthy(); });\n")
        after = js.replace("import { sep } from 'node:path';\n", "").replace("return sep;", "return '/';")
        self.assertIs(testx._SUPPORT_TANGLED, support_text("tests/a.test.js", js, after))


PY_TEST = "class T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(h(), 1)\n"
JS_TEST = "test('a', () => { expect(h()).toBe(1); });\n"
PY_H = "import unittest\n\n\ndef h():\n    return 1\n\n\n"
JS_H = "function h() {\n  return 1;\n}\n\n"
DISPATCH = ("import functools\nimport unittest\n\n\n@functools.singledispatch\ndef fmt(v):\n    return 'any'\n\n\n"
            "@fmt.register\ndef _(v: int):\n    return 'int'\n\n\n@fmt.register\ndef _(v: str):\n    return 'str'\n\n\n")
FAKE_PROPERTY = ("class Fake:\n    def __init__(self):\n        self._x = 1\n\n    @property\n    def x(self):\n"
                 "        return self._x\n\n    @x.setter\n    def x(self, v):\n        self._x = v\n\n\n")
NODE_HEAD = 'import assert from "node:assert/strict";\nimport { test, describe, beforeEach } from "node:test";\n\n'
HOOK_A = ('describe("a", () => {\n  beforeEach(() => {\n    state.n = 1;\n  });\n'
          '  test("a one", () => assert.equal(state.n, 1));\n});\n\n')
HOOK_B = ('describe("b", () => {\n  beforeEach(() => {\n    state.n = 0;\n  });\n'
          '  test("b one", () => assert.equal(typeof state.n, "number"));\n});\n')
NODE_PACKAGE = json.dumps({"name": "root", "type": "module", "scripts": {"test": "node --test test/"}})
# devkit's cause for a JavaScript or TypeScript test module that changed outside its tests, and the verdict of a test
# of it that passes without the change
JS_NOT_REBUILT = "devkit rebuilds only Python test modules"
JS_KEPT = "cannot be proven: its module keeps its change: " + JS_NOT_REBUILT


def rebuilt(f, before, after):
    """What _rebuild_helpers gives for test module `f` going from `before` to `after`: {"text", "first"}, or None."""
    r = testx._rebuild_helpers(f, before, after)
    return None if r is None else {"text": r.get("text"), "first": r.get("first", "no first difference named")}


class RebuildRuleTests(unittest.TestCase):
    # a Python test module rebuilds only when its text outside its tests differs from the base only inside the bodies
    # of helpers both sides hold in the same places, and the rebuilt module is then the base text with the tests now
    # spliced in: a helper fix comes back as its base text, and so does every blank line between statements. A test
    # brings the comment and blank lines right above it. devkit rebuilds only Python test modules: a JavaScript one
    # with a hook fix in a describe keeps its change
    def test_a_fix_inside_helper_bodies_rebuilds_a_python_module_and_keeps_a_javascript_one(self):
        before = '"""Tests of h."""\n' + PY_H + PY_TEST
        new_test = "\n    # the limit, which the helper now reads\n    def test_b(self):\n        self.assertEqual(h(), 2)\n"
        after = before.replace("    return 1\n", "    n = 2\n    return n\n").replace(
            "\n\n\nclass T", "\n\n\n\nclass T") + new_test
        self.assertEqual({"text": before + new_test, "first": ""}, rebuilt("tests/test_a.py", before, after))
        before = ("const state = {};\n\n" + JS_H + "describe('d', () => {\n  beforeEach(() => {\n    state.n = 1;\n"
                  "  });\n  test('a', () => { expect(h()).toBe(state.n); });\n});\n")
        new_test = "  test('b', () => { expect(h()).toBe(2); });\n"
        after = before.replace("return 1;", "return 2;").replace("state.n = 1;", "state.n = 2;").replace(
            "toBe(state.n); });\n", "toBe(state.n); });\n" + new_test)
        self.assertEqual({"text": None, "first": JS_NOT_REBUILT}, rebuilt("tests/a.test.js", before, after))

    # helpers of one name are matched by their places, so a fix in one of them comes back place by place: a property
    # getter beside its setter, a singledispatch group left as it was beside a fixed helper. Two beforeEach hooks of a
    # JavaScript module whose bodies trade places keep the module's change
    def test_helpers_of_one_name_in_the_same_places_come_back_place_by_place(self):
        new_test = "\n    def test_x(self):\n        self.assertEqual(Fake().x, 10)\n"
        before = "import unittest\n\n\n" + FAKE_PROPERTY + PY_TEST
        after = before.replace("        return self._x\n", "        return self._x * 10\n") + new_test
        self.assertEqual({"text": before + new_test, "first": ""}, rebuilt("tests/test_a.py", before, after))
        before = DISPATCH + "def h():\n    return 1\n\n\n" + PY_TEST
        after = before.replace("    return 1\n", "    return 2 - 1\n")
        self.assertEqual({"text": before, "first": ""}, rebuilt("tests/test_a.py", before, after))
        before = NODE_HEAD + "const state = {};\n\n" + HOOK_A + HOOK_B
        new_test = '  test("b two", () => assert.equal(state.n, 1));\n'
        after = before.replace("state.n = 1;", "state.n = @;").replace("state.n = 0;", "state.n = 1;").replace(
            "state.n = @;", "state.n = 0;").replace('"number"));\n', '"number"));\n' + new_test)
        self.assertEqual({"text": None, "first": JS_NOT_REBUILT}, rebuilt("test/util.test.mjs", before, after))

    # nothing outside the tests changed but blank lines between statements (a test added before helpers it is named
    # like, with a comment of its own): the module runs as it is, with nothing to rebuild. A comment or a docstring
    # inside a Python helper's body is part of that body: an edited one comes back as its base text. One inside a
    # JavaScript helper's body is a change outside the tests, which keeps the module
    def test_a_module_whose_code_outside_its_tests_reads_as_the_base_needs_no_rebuild(self):
        before = JS_H + "function g() {\n  return 1;\n}\n\n" + JS_TEST
        self.assertIsNone(rebuilt("tests/a.test.js", before,
                                  "// h, first\ntest('h', () => { expect(h()).toBe(1); });\n\n" + before))
        before = PY_H + PY_TEST
        self.assertIsNone(rebuilt("tests/test_a.py", before, before.replace("def h", "\n\ndef h") +
                                  "\n    # a second one\n    def test_b(self):\n        self.assertEqual(h(), 1)\n"))
        before = JS_H.replace("return 1;", "return 1; // one") + JS_TEST
        self.assertEqual({"text": None, "first": JS_NOT_REBUILT},
                         rebuilt("tests/a.test.js", before, before.replace("// one", "// the only one")))
        before = PY_H.replace("return 1", '"""One."""\n    return 1  # one') + PY_TEST
        self.assertEqual({"text": before, "first": ""},
                         rebuilt("tests/test_a.py", before, before.replace("# one", "# the only one")
                                 .replace("One.", "The one.")))

    # any other change to the code outside the tests keeps the module as changed, and the result names the first
    # difference: an added, removed or edited helper, import, class or statement, a helper's decorators or parameters,
    # a statement in a test class body. A JavaScript module keeps such a change too (a helper, an import, a statement,
    # the describe structure and its titles, helpers of one name whose places shift), named by devkit's cause: it
    # rebuilds only Python test modules
    def test_any_other_change_outside_the_tests_keeps_the_module_and_names_the_first_difference(self):
        fake = "import unittest\n\n\nclass Fake:\n    def get(self):\n        return 1\n\n\n"
        fake_test = "class T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(Fake().get(), 1)\n"
        in_class = ("import unittest\n\n\n" + "def h():\n    return 1\n\n\n" + "class T(unittest.TestCase):\n"
                    "    if True:\n        LIMIT = 1\n\n    def test_a(self):\n        self.assertEqual(h(), 1)\n")
        attr = "import unittest\n\n\nclass Fake:\n    x = 1\n\n\n" + fake_test.replace("Fake().get()", "Fake.x")
        box = "class Box {\n  static {\n    globalThis.LIMIT = 1;\n  }\n\n  get() {\n    return LIMIT;\n  }\n}\n\n"
        hooked = ("const state = {};\n\ndescribe('one', () => {\n  beforeEach(() => {\n    state.n = 1;\n  });\n"
                  "  test('t', () => { expect(state.n).toBe(1); });\n});\n")
        traded = NODE_HEAD + "const state = {};\n\n" + HOOK_A + HOOK_B
        cases = {  # name: (file, before, after, the first difference)
            "an added helper": ("tests/test_a.py", PY_H + PY_TEST,
                                PY_H.replace("def h", "def build():\n    return 2\n\n\ndef h") + PY_TEST,
                                "an added helper `build` (line 4)"),
            "a removed helper": ("tests/test_a.py", PY_H + PY_TEST.replace(
                "    def test_a", "    def setUp(self):\n        self.k = 1\n\n    def test_a"), PY_H + PY_TEST,
                "a removed helper `T.setUp` (line 9 at the base)"),
            "an added import that rebinds a name": ("tests/test_a.py", PY_H + PY_TEST, PY_H.replace(
                "import unittest\n", "import unittest\nfrom math import pi as LIMIT\n") + PY_TEST,
                "an added import `from math import pi as LIMIT` (line 2)"),
            "a removed import": ("tests/test_a.py", "import os\n" + PY_H + PY_TEST, PY_H + PY_TEST,
                                 "a removed import `import os` (line 1 at the base)"),
            "an edited module statement": (
                "tests/test_a.py", PY_H.replace("\n\n\ndef", "\n\nsys.path.insert(0, 'lib')\n\n\ndef") + PY_TEST,
                PY_H.replace("\n\n\ndef", "\n\nsys.path.insert(0, 'src')\n\n\ndef").replace("return 1", "return 2")
                + PY_TEST, "an edited statement `sys.path.insert(0, 'src')` (line 3)"),
            "an assignment added to rebind a built-in": (
                "tests/test_a.py", PY_H + PY_TEST,
                PY_H.replace("\n\n\ndef", "\n\nbuiltins.len = lambda x: 7\n\n\ndef") + PY_TEST,
                "an added statement `builtins.len = lambda x: 7` (line 3)"),
            "a statement removed from a test class body": (
                "tests/test_a.py", in_class, in_class.replace("    if True:\n        LIMIT = 1\n\n", ""),
                "a removed statement `if True:` (line 9 at the base)"),
            "a helper's parameters edited": ("tests/test_a.py", PY_H + PY_TEST, PY_H.replace(
                "def h():\n    return 1", "def h(n=1):\n    return n") + PY_TEST, "an edited helper `h` (line 4)"),
            "a decorator added to a helper": ("tests/test_a.py", PY_H + PY_TEST,
                                              PY_H.replace("def h", "@staticmethod\ndef h") + PY_TEST,
                                              "an edited helper `h` (line 4)"),
            "an added class": ("tests/test_a.py", PY_H + PY_TEST, PY_H.replace(
                "def h", "class New:\n    def __getattr__(self, name):\n        return 1\n\n\ndef h") + PY_TEST,
                "an added class `New` (line 4)"),
            "a method added to a class without tests": ("tests/test_a.py", fake + fake_test, fake.replace(
                "        return 1\n", "        return 1\n\n    def close(self):\n        return True\n") + fake_test,
                "an added helper `Fake.close` (line 8)"),
            "a class attribute edited": ("tests/test_a.py", attr, attr.replace("x = 1", "x = 10"),
                                         "an edited statement `x = 10` (line 5)"),
            "the main guard edited": ("tests/test_a.py",
                                      PY_H + PY_TEST + "\n\nif __name__ == '__main__':\n    unittest.main()\n",
                                      PY_H + PY_TEST + "\n\nif __name__ == '__main__':\n    unittest.main(verbosity=2)\n",
                                      "an edited statement `if __name__ == '__main__':` (line 13)"),
            "a same-name registration added before the others": (
                "tests/test_a.py", DISPATCH + PY_TEST, DISPATCH.replace(
                    "@fmt.register\ndef _(v: int)", "@fmt.register\ndef _(v: float):\n    return 'float'\n\n\n"
                                                    "@fmt.register\ndef _(v: int)") + PY_TEST,
                "an added helper `_` (line 10)"),
            "an added javascript helper": ("tests/a.test.js", JS_H + JS_TEST,
                                           "function build() {\n  return 2;\n}\n\n" + JS_H + JS_TEST,
                                           JS_NOT_REBUILT),
            "an added const that holds an object": (
                "tests/a.test.js", JS_H + JS_TEST, "const proto = { extra() { return 1; } };\n\n" + JS_H + JS_TEST,
                JS_NOT_REBUILT),
            "an added javascript import": (
                "tests/a.test.js", JS_H + JS_TEST, 'import { other as parseInt } from "../src/util.mjs";\n\n' + JS_H +
                JS_TEST, JS_NOT_REBUILT),
            "a removed global": ("tests/a.test.js",
                                 "globalThis.LIMIT = 1;\n\n" + JS_H.replace("return 1", "return LIMIT") + JS_TEST,
                                 JS_H + JS_TEST, JS_NOT_REBUILT),
            "a describe title edited": ("tests/a.test.js", hooked,
                                        hooked.replace("'one'", "'uno'").replace("state.n = 1;", "state.n = 2;"),
                                        JS_NOT_REBUILT),
            "describes that trade places": ("test/util.test.mjs", traded,
                                            NODE_HEAD + "const state = {};\n\n" + HOOK_B + "\n" + HOOK_A,
                                            JS_NOT_REBUILT),
            "a static block removed from a helper class": (
                "tests/a.test.js", box + JS_TEST, box.replace("  static {\n    globalThis.LIMIT = 1;\n  }\n\n", "")
                .replace("return LIMIT;", "return 1;") + JS_TEST, JS_NOT_REBUILT),
        }
        for name, (f, before, after, first) in cases.items():
            with self.subTest(name):
                self.assertEqual({"text": None, "first": first}, rebuilt(f, before, after))


ROOT_JS = "function isRoot(p) {\n  return /^\\/*$/.test(p);\n}\n\n"


class RebuildByteTests(unittest.TestCase):
    # a rebuild compares the base and the module now byte for byte outside the tests and the helpers' bodies, blank
    # lines between statements aside: a docstring, a coding declaration, or a comment keeps the module as it is now,
    # even beside a helper fix, and names the first difference. A JavaScript module whose text outside its tests
    # differs (a blank line inside a template literal too) keeps it, named by devkit's cause
    def test_any_other_byte_outside_the_tests_and_the_helper_bodies_keeps_the_module(self):
        fix = ("    return 1\n", "    return 2 - 1\n")
        box = PY_H + "class Box:\n    '''one'''\n\n    def tag(self):\n        return 'one'\n\n\n" + PY_TEST
        template = "const EXPECTED = `a\n\nb`;\n\n" + JS_H + JS_TEST
        cases = {  # name: (file, before, after, the first difference)
            "a module docstring edited": ("tests/test_a.py", '"""one"""\n' + PY_H + PY_TEST,
                                          '"""two"""\n' + PY_H.replace(*fix) + PY_TEST,
                                          'an edited docstring `"""two"""` (line 1)'),
            "a class docstring edited": ("tests/test_a.py", box, box.replace("one", "two"),
                                         "an edited docstring `'''two'''` (line 9)"),
            "a coding declaration added": ("tests/test_a.py", PY_H + PY_TEST,
                                           "# -*- coding: latin-1 -*-\n" + PY_H.replace(*fix) + PY_TEST,
                                           "an added comment `# -*- coding: latin-1 -*-` (line 1)"),
            "a coding declaration right above the first test": (
                "tests/test_a.py", "def test_a():\n    assert h() == 1\n\n\ndef h():\n    return 1\n",
                "# -*- coding: latin-1 -*-\ndef test_a():\n    assert h() == 1\n\n\ndef h():\n    return 2 - 1\n",
                "an added comment `# -*- coding: latin-1 -*-` (line 1)"),
            "a comment edited between helpers": ("tests/test_a.py", PY_H.replace("def h", "# one\ndef h") + PY_TEST,
                                                 PY_H.replace("def h", "# two\ndef h").replace(*fix) + PY_TEST,
                                                 "an edited comment `# two` (line 4)"),
            "a blank line inside a template literal": (
                "tests/a.test.js", template, template.replace("`a\n\nb`", "`a\nb`").replace("return 1;", "return 2;"),
                JS_NOT_REBUILT),
            "trailing blanks inside a template literal": (
                "tests/a.test.js", template.replace("`a\n\nb`", "`a  \nb`"),
                template.replace("`a\n\nb`", "`a\nb`").replace("return 1;", "return 2;"),
                JS_NOT_REBUILT),
            "a global renamed after a regular expression that follows return": (
                "tests/a.test.js", ROOT_JS + "globalThis.MAX = 5; /* the limit */\n\n" +
                JS_H.replace("return 1;", "return globalThis.MAX;") + JS_TEST,
                ROOT_JS + "globalThis.LIMIT = 5; /* the limit */\n\n" +
                JS_H.replace("return 1;", "return globalThis.LIMIT;") + JS_TEST,
                JS_NOT_REBUILT),
        }
        for name, (f, before, after, first) in cases.items():
            with self.subTest(name):
                self.assertEqual({"text": None, "first": first}, rebuilt(f, before, after))

    # a regular expression after a keyword that ends no expression (return, typeof, …) is read as one, so the comment
    # its `/*` seems to open hides no code after it
    def test_a_regular_expression_after_return_hides_no_code(self):
        masked = testx._js_mask(ROOT_JS + "globalThis.LIMIT = 5; /* the limit */\nconst k = typeof /a*/;\n")
        self.assertIn("globalThis.LIMIT = 5;", masked)
        self.assertIn("const k = typeof /  /;", masked)


# a class attribute right after a method, below a comment: the rebuild must not take the attribute for the method's body
LIMIT_FAKE = ("class Fake:\n    def get(self):\n        x = 1\n        return self.limit + x - 1\n    limit = %d\n\n\n"
              "class LimitTests(unittest.TestCase):\n    def test_limit(self):\n"
              "        self.assertEqual(Fake().get(), %d)\n")


class UnseenLineBreakTests(unittest.TestCase):
    # Python ends a line at a carriage return without a new line; the rebuild's readers end lines at new lines only. A
    # module that holds such a line break keeps its change, named by it: here a comment that holds one moves a class
    # attribute edit into the method above it for the readers, and a rebuild would run the base attribute against the
    # test now
    def test_a_python_carriage_return_without_a_new_line_keeps_the_module(self):
        before = with_tests("\n\n# a\r# b\n" + LIMIT_FAKE % (2, 2))
        after = with_tests("\n\n# a\r# b\n" + LIMIT_FAKE % (3, 3))
        rc, rows, text = prove_rows(before, after)
        self.assertEqual([("test_calc.LimitTests.test_limit",
                           "cannot be proven: its module keeps a change outside its helpers' bodies: a carriage return "
                           "without a new line, a line break the rebuild does not read (line 14)")], rows, text)


JS_IMPORTS = 'import assert from "node:assert/strict";\nimport { test } from "node:test";\n\n'
JS_H_TEST = 'test("t", () => {\n  assert.equal(h(), 1);\n});\n'
UTIL_HEAD = NODE_HEAD + 'import * as util from "../src/util.mjs";\n\n'
UTIL_BEFORE = "export const value = () => 1;\n"
UTIL_AFTER = UTIL_BEFORE + "export const other = () => 3;\n"
# a template literal that holds code, between two lines with a backtick the reader once took for one that opens a
# template literal (in a comment after a division by `of`, in a regular expression after a closing parenthesis): the
# reader read the code inside the template literal as a helper of the module
TEMPLATE_FIXTURE = 'const SRC = `\nfunction fixture() {\n  return "%s";\n}\n`;\n'
TEMPLATE_AROUND = {
    "a backtick in a comment after a division by of": ("const of = 4;\nlet r = of / 2; // a ` mark\n",
                                                       "let r2 = of / 2; // `\n\n"),
    "a backtick in a regular expression after a closing parenthesis": ("const s = 'x';\nif (s) /`/.test(s);\n",
                                                                       "if (s) /`/.test(s);\n\n"),
}


def node_prove(before, after, *args):
    """(exit, output, errors) of prove after test/util.test.mjs goes from `before` to `after`, beside a production
    change that adds util.other."""
    with Env() as e:
        r = e.repo({"package.json": NODE_PACKAGE, "src/util.mjs": UTIL_BEFORE, "test/util.test.mjs": before})
        edit(r, {"src/util.mjs": UTIL_AFTER, "test/util.test.mjs": after})
        return dev(r, "prove", "--runs", "1", *args)


class JsModuleKeptTests(unittest.TestCase):
    # devkit rebuilds only Python test modules: a JavaScript or TypeScript test module that changed outside its tests,
    # a helper's body too, keeps its change, so the run without the change runs it as it is now. One whose only change
    # is its tests (with the comment and blank lines right above each) has nothing to rebuild
    def test_a_javascript_or_typescript_module_that_changed_outside_its_tests_keeps_its_change(self):
        before = JS_IMPORTS + JS_H + JS_H_TEST
        new_test = '\n// the second one\ntest("u", () => {\n  assert.equal(h(), 1);\n});\n'
        for ext in ("js", "cjs", "mjs", "ts", "jsx", "tsx", "mts", "cts"):
            with self.subTest(ext):
                f = f"test/util.test.{ext}"
                self.assertEqual({"text": None, "first": JS_NOT_REBUILT},
                                 rebuilt(f, before, before.replace("return 1;", "return 2 - 1;") + new_test))
                self.assertIsNone(rebuilt(f, before, before + new_test))

    # a test of such a module that passes without the change cannot be proven, named by devkit's cause, whatever the
    # reader takes for a helper: here the code inside a template literal fixture, which a misread backtick once put
    # outside it, so the run without the change ran the module with the fixture's base text
    @unittest.skipUnless(shutil.which("node"), "node is not installed, so the node runner cannot run here")
    def test_a_test_of_a_kept_javascript_module_that_passes_without_the_change_cannot_be_proven(self):
        old = 'test("old", () => {\n  assert.equal(util.value(), 1);\n});\n'
        new = 'test("src", () => {\n  assert.match(SRC, /new/);\n});\n'
        for name, (head, tail) in TEMPLATE_AROUND.items():
            with self.subTest(name):
                before = UTIL_HEAD + head + TEMPLATE_FIXTURE % "old" + tail + old
                after = UTIL_HEAD + head + TEMPLATE_FIXTURE % "new" + tail + old + new
                rc, out, err = node_prove(before, after, "--json")
                self.assertEqual({"test/util.test.mjs::src": JS_KEPT},
                                 {t["id"]: t["verdict"] for t in json.loads(out)["tests"]}, out + err)
                self.assertEqual(2, rc, out + err)
        rc, out, err = node_prove(before, after)
        self.assertRegex(out, r"devkit rebuilds only Python test modules[^\n]*:\n\s+test/util.test.mjs\s+"
                              r"test/util.test.mjs::src\n")

    # a test of such a module that fails without the production change proves it, on an error too: only the
    # production change was undone, so nothing else decided the failure. Here the failing test reads what the helper's
    # edit gives first, so with the helper's base body it would fail on another error
    @unittest.skipUnless(shutil.which("node"), "node is not installed, so the node runner cannot run here")
    def test_a_test_of_a_kept_javascript_module_that_fails_without_the_change_proves_it(self):
        before = UTIL_HEAD + "function h() {\n  return null;\n}\n\n" + \
            'test("a", () => assert.equal(util.value(), 1));\n'
        after = before.replace("return null;", "return { k: 1 };") + \
            'test("other", () => {\n  const k = h().k;\n  assert.equal(util.other() * k, 3);\n});\n' + \
            'test("tripled", () => {\n  const k = h().k;\n' \
            '  assert.equal((util.other ?? (() => 0))() * 3 * k, 9);\n});\n'
        rc, out, err = node_prove(before, after, "--json")
        self.assertEqual({"test/util.test.mjs::other": "proves the change",
                          "test/util.test.mjs::tripled": "proves the change"},
                         {t["id"]: t["verdict"] for t in json.loads(out)["tests"]}, out + err)
        self.assertEqual(0, rc, out + err)


class FailureKindTests(unittest.TestCase):
    # a failure without the change proves a rebuilt module's test only when it is an assertion failure, so each
    # runner's failed case says which it is: "assertion", or the name of what it raised
    def test_each_runner_says_whether_a_failure_is_an_assertion_or_names_what_was_raised(self):
        top = "/w"
        rn = {"name": "unittest", "root": ".", "start": "tests"}
        out = ("test_a (test_x.T.test_a) ... FAIL\ntest_b (test_x.T.test_b) ... ERROR\n\n" + "=" * 70 +
               "\nFAIL: test_a (test_x.T.test_a)\n" + "-" * 70 + "\nTraceback (most recent call last):\n"
               '  File "/w/tests/test_x.py", line 5, in test_a\n    self.assertEqual(1, 2)\n'
               "AssertionError: 1 != 2\n\n" + "=" * 70 + "\nERROR: test_b (test_x.T.test_b)\n" + "-" * 70 +
               '\nTraceback (most recent call last):\n  File "/w/tests/test_x.py", line 8, in test_b\n'
               "    make()['k']\nTypeError: list indices must be integers or slices, not str\n\n" + "-" * 70 +
               "\nRan 2 tests in 0.001s\n\nFAILED (failures=1, errors=1)\n")
        self.assertEqual({"test_x.T.test_a": "assertion", "test_x.T.test_b": "TypeError"},
                         {c["id"]: c.get("failure") for c in testx.parse_unittest(out, top, rn)})
        node = ('<?xml version="1.0" encoding="utf-8"?>\n<testsuites>\n'
                '<testcase name="value" classname="test" file="/w/test/a.test.mjs">\n'
                '<failure type="testCodeFailure" message="Expected values to be strictly equal:&#10;&#10;0 !== 1">\n'
                "[Error [ERR_TEST_FAILURE]: Expected values to be strictly equal:\n\n0 !== 1\n] {\n"
                "  code: 'ERR_TEST_FAILURE',\n  failureType: 'testCodeFailure',\n"
                "  cause: AssertionError [ERR_ASSERTION]: Expected values to be strictly equal:\n  \n  0 !== 1\n"
                "  \n      at TestContext.&lt;anonymous> (file:///w/test/a.test.mjs:5:29) {\n"
                "    code: 'ERR_ASSERTION'\n  }\n}\n</failure>\n</testcase>\n"
                '<testcase name="get" classname="test" file="/w/test/a.test.mjs">\n'
                '<failure type="testCodeFailure" message="make(...).get is not a function">\n'
                "[Error [ERR_TEST_FAILURE]: make(...).get is not a function] {\n  code: 'ERR_TEST_FAILURE',\n"
                "  failureType: 'testCodeFailure',\n  cause: TypeError [Error]: make(...).get is not a function\n"
                "      at TestContext.&lt;anonymous> (file:///w/test/a.test.mjs:6:41)\n}\n</failure>\n</testcase>\n"
                "</testsuites>\n")
        self.assertEqual({"test/a.test.mjs::value": "assertion", "test/a.test.mjs::get": "TypeError"},
                         {c["id"]: c.get("failure") for c in testx.parse_node_junit(node, top, top)})
        # jest's expect.assertions(n) and expect.hasAssertions() fail a test that asserted too little: an assertion
        jest = {"testResults": [{"name": "/w/src/a.test.ts", "assertionResults": [
            {"fullName": f"t{i}", "status": "failed", "failureMessages": [m]} for i, m in enumerate((
                "Error: expect(received).toBe(expected) // Object.is equality\n\nExpected: 1\nReceived: 0",
                "AssertionError: expected 0 to be 1 // Object.is equality",
                "TypeError: make(...).get is not a function\n    at Object.<anonymous> (/w/src/a.test.ts:6:20)",
                "Error: expect.assertions(2)\n\nExpected two assertions to be called but received one assertion call.",
                "expect.hasAssertions()\n\nExpected at least one assertion to be called but received none."))]}]}
        self.assertEqual({"src/a.test.ts::t0": "assertion", "src/a.test.ts::t1": "assertion",
                          "src/a.test.ts::t2": "TypeError", "src/a.test.ts::t3": "assertion",
                          "src/a.test.ts::t4": "assertion"},
                         {c["id"]: c.get("failure") for c in testx.parse_jest_json(json.dumps(jest), top)})
        # pytest's Failed (pytest.raises or pytest.warns that saw nothing, pytest.fail) is the test's own check
        pytest = ('<testsuites><testsuite name="pytest">'
                  '<testcase classname="tests.test_x" name="test_a"><failure message="assert 0 == 1">x</failure>'
                  '</testcase><testcase classname="tests.test_x" name="test_b"><failure message="TypeError: '
                  "'list' object is not callable\">x</failure></testcase>"
                  '<testcase classname="tests.test_x" name="test_c"><failure message="json.decoder.JSONDecodeError: '
                  'Expecting value: line 1 column 1 (char 0)">x</failure></testcase>'
                  '<testcase classname="tests.test_x" name="test_d"><failure message="Failed: DID NOT RAISE '
                  '&lt;class \'ValueError\'&gt;">x</failure></testcase>'
                  '<testcase classname="tests.test_x" name="test_e"><failure message="Failed: add is off by one">x'
                  '</failure></testcase></testsuite></testsuites>')
        self.assertEqual(["assertion", "TypeError", "json.decoder.JSONDecodeError", "assertion", "assertion"],
                         [c.get("failure") for c in testx.parse_junit(pytest, top, top, "pytest")])

    # Playwright names a failure by its type: an expect matcher (expect.toBe) or an AssertionError is an assertion, any
    # other type names what was raised. A release that types no failure (1.55 writes FAILURE) has the error in its
    # text
    def test_playwright_says_by_a_failure_s_type_whether_it_is_an_assertion(self):
        top = "/w"

        def case(name, tag, kind, message, text="x"):
            return (f'<testcase name="{name}" classname="a.spec.ts"><{tag} message="{message}" type="{kind}">'
                    f"<![CDATA[{text}]]></{tag}></testcase>")
        report = ('<testsuites><testsuite name="a.spec.ts">' +
                  case("value", "failure", "expect.toBe", "expect(received).toBe(expected) // Object.is equality") +
                  case("not value", "failure", "expect.not.toBe", "expect(received).not.toBe(expected)") +
                  case("get", "error", "TypeError", "make(...).get is not a function") +
                  case("node", "error", "AssertionError", "Expected values to be strictly equal:") +
                  case("old value", "failure", "FAILURE", "a.spec.ts:7:5 old value",
                       "  1) [chromium] › a.spec.ts:7:5 › old value ───\n\n    Error: expect(received).toBe(expected)"
                       "\n\n    Expected: 1\n    Received: 0\n") +
                  case("old get", "failure", "FAILURE", "a.spec.ts:9:5 old get",
                       "  2) [chromium] › a.spec.ts:9:5 › old get ───\n\n    TypeError: make(...).get is not a "
                       "function\n\n       at /w/a.spec.ts:10:20\n") +
                  "</testsuite></testsuites>")
        self.assertEqual({"a.spec.ts::value": "assertion", "a.spec.ts::not value": "assertion",
                          "a.spec.ts::get": "TypeError", "a.spec.ts::node": "assertion",
                          "a.spec.ts::old value": "assertion", "a.spec.ts::old get": "TypeError"},
                         {c["id"]: c.get("failure") for c in testx.parse_playwright_junit(report, top, top, {})})


class FailedTimeoutTests(unittest.TestCase):
    # pytest-timeout fails a test that ran too long with pytest's Failed ("Failed: Timeout >1.0s" before 2.2, "Failed:
    # Timeout (>1.0s) from pytest-timeout." since), which is no check of the test: only pytest's own Failed, bare or as
    # _pytest.outcomes.Failed, is an assertion, never one that says Timeout, and a Failed of another module is named by
    # what was raised
    def test_only_pytest_s_own_failed_and_never_a_timeout_is_an_assertion(self):
        top = "/w"
        messages = ["Failed: Timeout &gt;1.0s", "_pytest.outcomes.Failed: add is off by one", "plugin.Failed: lost",
                    "Failed: DID NOT RAISE &lt;class 'ValueError'&gt;",
                    "Failed: Timeout (&gt;1.0s) from pytest-timeout.",
                    "E   Failed: Timeout (&gt;1.0s) from pytest-timeout.",
                    "_pytest.outcomes.Failed: Timeout (&gt;1.0s) from pytest-timeout."]
        report = ('<testsuites><testsuite name="pytest">' + "".join(
            f'<testcase classname="tests.test_x" name="test_{i}"><failure message="{m}">x</failure></testcase>'
            for i, m in enumerate(messages)) + "</testsuite></testsuites>")
        self.assertEqual(["Failed", "assertion", "plugin.Failed", "assertion", "Failed", "Failed",
                          "_pytest.outcomes.Failed"],
                         [c.get("failure") for c in testx.parse_junit(report, top, top, "pytest")])


class RebuildVerdictTests(unittest.TestCase):
    # under a rebuild, a failure without the change proves only when it is an assertion failure: one that raises
    # anything else (a TypeError on a member only the helper edit gives) cannot be proven, named by what it raised. A
    # helper fix that starts to use a module both sides import is proven by its assertion
    def test_under_a_rebuild_only_an_assertion_failure_proves(self):
        before = fake_module("import json\n\n\ndef make():\n    return [0]\n\n\ndef h(x):\n    return str(x)\n\n\n")
        after = before.replace("return [0]", "return {'k': 1, 0: 1}").replace("return str(x)", "return json.dumps(x)") + \
            "\n    def test_key(self):\n        self.assertEqual(make()['k'], 1)\n" \
            "\n    def test_value(self):\n        self.assertEqual(make()[0], 1)\n" \
            "\n    def test_quoted(self):\n        self.assertEqual(h('a'), '\"a\"')\n"
        rc, rows, text = prove_rows(before, after)
        self.assertEqual([("test_calc.CalcTests.test_key", "cannot be proven: without the change it fails on "
                                                            "TypeError, not on an assertion"),
                          ("test_calc.CalcTests.test_quoted", "proves the change"),
                          ("test_calc.CalcTests.test_value", "proves the change")], rows, text)
        self.assertEqual(0, rc, text)

    # a module whose change outside its helpers' bodies stays (here a helper the change adds) runs as it is now: a
    # test that fails without the production change proves it, and one that passes cannot be proven, named by the
    # module's first difference, never "does not test the change"
    def test_a_module_that_keeps_its_change_proves_by_a_failure_and_names_its_first_difference(self):
        after = with_tests().replace("class CalcTests", "def pair():\n    return 2, 3\n\n\nclass CalcTests") + \
            "\n    def test_mul(self):\n        self.assertEqual(calc.mul(*pair()), 6)\n" \
            "\n    def test_pair(self):\n        self.assertEqual(pair(), (2, 3))\n"
        kept = ("cannot be proven: its module keeps a change outside its helpers' bodies: an added helper `pair` "
                "(line 9)")
        with Env() as e:
            r = e.repo(dict(PROVE_BASE, **{"tests/test_calc.py": with_tests()}))
            edit(r, {"tests/test_calc.py": after, "calc.py": MUL})
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            self.assertEqual([("test_calc.CalcTests.test_mul", "proves the change"),
                              ("test_calc.CalcTests.test_pair", kept)],
                             [(x["id"], x["verdict"]) for x in json.loads(out)["tests"]], out + err)
            self.assertEqual(0, rc, out + err)
            rc, out, err = dev(r, "prove", "--runs", "1")
        self.assertRegex(out, r"keeps a change outside its helpers' bodies[^\n]*:\n\s+tests/test_calc.py\s+"
                              r"test_calc.CalcTests.test_pair  · an added helper `pair` \(line 9\)")


CROSS_HEAD = ("import sys\nimport unittest\nfrom pathlib import Path\n\nsys.path.insert(0, str(Path(__file__).resolve()"
              ".parents[1]))\nsys.path.insert(0, str(Path(__file__).resolve().parent))\nimport calc  # noqa: E402\n")
CROSS_TESTS = "\n\nclass CalcTests(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(calc.add(1, 2), 3)\n"
OLD_MAKE = "class Old:\n    pass\n\n\ndef make():\n    return Old()\n"
NEW_MAKE = OLD_MAKE.replace("return Old()", "class N:\n        def extra(self):\n            return 1\n    return N()")
CROSS_NEW = "\n    def test_x(self):\n        self.assertEqual(make().extra(), 1)\n" + TEST_MUL


class SupportRerunTests(unittest.TestCase):
    # a test whose run without the change undid test support (its own module rebuilt, or a rebuilt module or a test
    # helper file it imports) and failed there on an error, not on an assertion, runs once more with that support as
    # it is now: only an assertion failure there proves the production change, as in the run without the change; an
    # error there (here a function only the change adds) or a pass leaves it unproven, named by what it raised in the
    # run without the change (a member only the support's edit gives)
    def test_an_error_under_a_rebuild_proves_only_when_the_support_as_it_is_now_fails_on_an_assertion(self):
        before = fake_module("def make():\n    return [0]\n\n\n")
        after = before.replace("return [0]", "return {'k': 1}") + TEST_MUL + \
            "\n    def test_key(self):\n        self.assertEqual(make()['k'], 1)\n" \
            "\n    def test_tripled(self):\n        k = make()['k']\n" \
            "        self.assertEqual(getattr(calc, 'mul', lambda a, b: 0)(2, 3 * k), 6)\n"
        rc, rows, text = prove_rows(before, after)
        self.assertEqual([("test_calc.CalcTests.test_key", "cannot be proven: without the change it fails on "
                                                            "TypeError, not on an assertion"),
                          ("test_calc.CalcTests.test_mul", "cannot be proven: without the change it fails on "
                                                            "AttributeError, not on an assertion"),
                          ("test_calc.CalcTests.test_tripled", "proves the change")], rows, text)
        self.assertEqual(0, rc, text)

    def test_a_test_that_imports_another_rebuilt_module_proves_only_by_an_assertion_or_the_rerun(self):
        b_old = CROSS_HEAD + "\n\n" + OLD_MAKE + CROSS_TESTS.replace("CalcTests", "BTests")
        a_old = CROSS_HEAD + "from test_b import make  # noqa: E402\n" + CROSS_TESTS
        with Env() as e:
            r = e.repo({"calc.py": PROVE_BASE["calc.py"], "tests/test_b.py": b_old, "tests/test_a.py": a_old})
            edit(r, {"calc.py": MUL, "tests/test_b.py": b_old.replace(OLD_MAKE, NEW_MAKE),
                     "tests/test_a.py": a_old + CROSS_NEW})
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
        self.assertEqual({"test_a.CalcTests.test_x": "cannot be proven: without the change it fails on AttributeError, "
                                                     "not on an assertion",
                          "test_a.CalcTests.test_mul": "cannot be proven: without the change it fails on "
                                                       "AttributeError, not on an assertion",
                          "test_b.BTests.test_add": "does not test the change"},
                         {t["id"]: t["verdict"] for t in json.loads(out)["tests"]}, out + err)

    def test_a_test_that_imports_a_changed_helper_file_proves_only_by_an_assertion_or_the_rerun(self):
        a_old = CROSS_HEAD + "from helpers import make  # noqa: E402\n" + CROSS_TESTS
        with Env() as e:
            r = e.repo({"calc.py": PROVE_BASE["calc.py"], "tests/helpers.py": OLD_MAKE, "tests/test_a.py": a_old})
            edit(r, {"calc.py": MUL, "tests/helpers.py": NEW_MAKE, "tests/test_a.py": a_old + CROSS_NEW})
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
        self.assertEqual({"test_a.CalcTests.test_x": "cannot be proven: without the change it fails on AttributeError, "
                                                     "not on an assertion",
                          "test_a.CalcTests.test_mul": "cannot be proven: without the change it fails on "
                                                       "AttributeError, not on an assertion"},
                         {t["id"]: t["verdict"] for t in json.loads(out)["tests"]}, out + err)


class CleanRerunTests(unittest.TestCase):
    # the run once more with the support as it is now starts from a fresh copy: a folder that the run without the
    # change left (its test stopped before it removed it) must not make that run fail, which would prove a change the
    # test never reaches
    def test_the_rerun_does_not_see_what_the_run_without_the_change_left(self):
        before = "import shutil\n" + fake_module(OLD_MAKE + "\n\n")
        after = before.replace(OLD_MAKE, NEW_MAKE) + (
            "\n    def test_x(self):\n        out = Path(__file__).resolve().parent / 'out'\n        out.mkdir()\n"
            "        self.assertEqual(make().extra(), 1)\n        shutil.rmtree(out)\n")
        rc, rows, text = prove_rows(before, after)
        self.assertEqual([("test_calc.CalcTests.test_x", "cannot be proven: without the change it fails on "
                                                         "AttributeError, not on an assertion")], rows, text)


def leaving(where):
    """A new test for fake_module's module: it makes the folder `where` (an expression), checks a member only the
    helper's edit gives, then removes the folder. Without the change it stops before, so the folder stays."""
    return ("\n    def test_x(self):\n        out = " + where + "\n        out.mkdir()\n"
            "        self.assertEqual(make().extra(), 1)\n        shutil.rmtree(out)\n")


LEFT_BASE = "import shutil\nimport tempfile\n" + fake_module(OLD_MAKE + "\n\n")
# a new test that fails when its temp folder holds what an earlier run left, or is not each of TMPDIR, TEMP and TMP
TEMP_TEST = """
    def test_mul(self):
        mark = Path(tempfile.gettempdir()) / 'mark'
        self.assertFalse(mark.exists())
        mark.write_text('x')
        self.assertEqual({os.environ.get(k) for k in ('TMPDIR', 'TEMP', 'TMP')}, {tempfile.gettempdir()})
        self.assertEqual(calc.mul(2, 3), 6)
"""


class LeftStateRerunTests(unittest.TestCase):
    # the run once more with the support as it is now proves only by an assertion failure, as the run without the
    # change does, and each run has a fresh temp folder of its own: a folder that the run without the change left
    # outside the copy (in the temp folder, the home folder, the folder beside the copy) never proves a change the test
    # does not reach
    def test_a_folder_left_outside_the_copy_proves_nothing(self):
        places = {"the temp folder": "Path(tempfile.gettempdir()) / 'left'", "the home folder": "Path.home() / '.left'",
                  "the folder beside the copy": "Path(__file__).resolve().parents[2] / 'left'"}
        for name, where in places.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.dict(os.environ, {"HOME": home, "TMPDIR": tmp}):
                rc, rows, text = prove_rows(LEFT_BASE, LEFT_BASE.replace(OLD_MAKE, NEW_MAKE) + leaving(where))
                self.assertEqual([("test_calc.CalcTests.test_x", "cannot be proven: without the change it fails on "
                                                                 "AttributeError, not on an assertion")], rows, text)

    # each run of a test (here twice with the change, once without) starts with an empty temp folder of its own,
    # named by TMPDIR, TEMP and TMP, and removed with the run: what one run leaves there never reaches the next
    def test_each_run_has_a_fresh_temp_folder_of_its_own(self):
        before = with_tests()
        after = "import os\nimport tempfile\n" + with_tests(TEMP_TEST)
        with Env() as e, tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"TMPDIR": tmp}):
            r = e.repo(dict(PROVE_BASE, **{"tests/test_calc.py": before}))
            edit(r, {"tests/test_calc.py": after, "calc.py": MUL})
            rc, out, err = dev(r, "prove", "--json", "--runs", "2")
            self.assertEqual([], os.listdir(tmp))
        self.assertEqual({"test_calc.CalcTests.test_mul": "proves the change"},
                         {x["id"]: x["verdict"] for x in json.loads(out)["tests"]}, out + err)


SAME_LEFT_BASE = "import shutil\n" + fake_module("def prepare(out):\n    out.mkdir()\n\n\n")
SAME_LEFT_NOW = SAME_LEFT_BASE.replace("    out.mkdir()\n\n\n", "    (out / 'f').write_text('x')\n\n\n")


def leaving_same_error(where):
    """A new test for SAME_LEFT_BASE's module: it makes the folder `where` (an expression), lets the helper prepare it,
    checks a file there, then removes the folder. Without the change the base helper makes the folder again, so the
    test stops on FileExistsError and the folder stays; a run after it fails on the test's own mkdir, with the same
    type and message."""
    return ("\n    def test_x(self):\n        out = " + where + "\n        out.mkdir()\n        prepare(out)\n"
            "        self.assertTrue((out / 'f').exists())\n        shutil.rmtree(out)\n")


class SameLeftErrorTests(unittest.TestCase):
    # what the run without the change left outside the copy (the home folder, the folder beside the copy, a fixed path)
    # can make the rerun with the support as it is now fail with the very error of that run, type and message: only an
    # assertion failure there proves, so such a test cannot be proven, whatever the production change (here one no test
    # reaches)
    def test_a_rerun_that_fails_on_the_same_error_proves_nothing(self):
        with tempfile.TemporaryDirectory() as fixed:
            places = {"the home folder": "Path.home() / '.left'",
                      "the folder beside the copy": "Path(__file__).resolve().parents[2] / 'left'",
                      "a fixed path": f"Path({str(Path(fixed).resolve())!r}) / 'left'"}
            for name, where in places.items():
                with self.subTest(name), tempfile.TemporaryDirectory() as home, \
                        mock.patch.dict(os.environ, {"HOME": home}):
                    rc, rows, text = prove_rows(SAME_LEFT_BASE, SAME_LEFT_NOW + leaving_same_error(where))
                    self.assertEqual([("test_calc.CalcTests.test_x", "cannot be proven: without the change it fails "
                                                                     "on FileExistsError, not on an assertion")],
                                     rows, text)


def locked_folder(lock, unlock):
    """A new test for fake_module's module: it makes the folder `locked` beside itself with a file in it, runs `lock`,
    checks a member only the helper's edit gives, then runs `unlock` and removes the folder. Without the change it
    stops before the unlock, so the folder stays locked in the copy."""
    return ("\n    def test_x(self):\n        out = Path(__file__).resolve().parent / 'locked'\n        out.mkdir()\n"
            "        (out / 'f').write_text('x')\n        " + lock + "\n"
            "        self.assertEqual(make().extra(), 1)\n        " + unlock + "\n        shutil.rmtree(out)\n")


LOCKED_BASE = "import os\nimport shutil\nimport stat\n" + fake_module(OLD_MAKE + "\n\n")


class LockedCopyTests(unittest.TestCase):
    # the rerun starts from a fresh copy: a read-only folder that the run without the change left in the copy is made
    # writable and removed; a copy that still cannot be removed gives no rerun, so its tests cannot be proven, never a
    # traceback. The copy is gone when prove ends
    def test_a_read_only_folder_left_in_the_copy_is_removed_for_the_rerun(self):
        after = LOCKED_BASE.replace(OLD_MAKE, NEW_MAKE) + locked_folder("out.chmod(0o555)", "out.chmod(0o755)")
        with Env() as e:
            r = e.repo(dict(PROVE_BASE, **{"tests/test_calc.py": LOCKED_BASE}))
            edit(r, {"tests/test_calc.py": after, "calc.py": MUL})
            rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            left = [p.name for p in testx.TMP_ROOT.iterdir() if p.name.startswith(testx.PROVE_COPY)]
        self.assertEqual([("test_calc.CalcTests.test_x", "cannot be proven: without the change it fails on "
                                                         "AttributeError, not on an assertion")],
                         [(x["id"], x["verdict"]) for x in json.loads(out)["tests"]], out + err)
        self.assertEqual([], left)

    @unittest.skipUnless(hasattr(os, "chflags") and hasattr(stat, "UF_IMMUTABLE"), "no file flags on this system")
    def test_a_copy_that_cannot_be_removed_leaves_the_rerun_s_tests_unproven(self):
        after = LOCKED_BASE.replace(OLD_MAKE, NEW_MAKE) + locked_folder("os.chflags(out / 'f', stat.UF_IMMUTABLE)",
                                                                        "os.chflags(out / 'f', 0)")
        with Env() as e:
            try:
                r = e.repo(dict(PROVE_BASE, **{"tests/test_calc.py": LOCKED_BASE}))
                edit(r, {"tests/test_calc.py": after, "calc.py": MUL})
                rc, out, err = dev(r, "prove", "--json", "--runs", "1")
            finally:  # unlock what the stopped test locked, so the temporary folder can go
                for p in e.root.rglob("*"):
                    if not p.is_symlink() and p.lstat().st_flags & stat.UF_IMMUTABLE:
                        os.chflags(p, 0, follow_symlinks=False)
        self.assertEqual([("test_calc.CalcTests.test_x", "cannot be proven: without the change it fails on "
                                                         "AttributeError, not on an assertion")],
                         [(x["id"], x["verdict"]) for x in json.loads(out)["tests"]], out + err)


def locked_chain(root, name, modes):
    """Folder `name` in `root` that holds one folder in another, a file in the deepest, each folder given its mode of
    `modes` from the outermost in (the deepest locked first). -> its path."""
    top = p = os.path.join(root, name)
    os.mkdir(top)
    chain = []
    for i in range(len(modes)):
        p = os.path.join(p, f"d{i}")
        os.mkdir(p)
        chain.append(p)
    Path(p, "f").write_text("x")
    for d, mode in reversed(list(zip(chain, modes))):
        os.chmod(d, mode)
    return top


def unlock_all(root):
    """Give every folder under `root` back to its owner, outermost first, so a temporary folder can go."""
    try:
        os.chmod(root, stat.S_IRWXU)
        entries = list(os.scandir(root))
    except OSError:
        return
    for e in entries:
        if e.is_dir(follow_symlinks=False):
            unlock_all(e.path)


class RemoveTreeTests(unittest.TestCase):
    # a removal cannot list a folder a test left unreadable (or listable by name only) until it unlocks it, which
    # takes one more pass for each folder locked inside another: the passes go on while one unlocks something, so
    # such folders go at any depth, and the sweep of stale proof copies removes them the same way
    def test_folders_locked_one_inside_another_are_removed(self):
        for modes in ([0o000] * 2, [0o000] * 3, [0o100] * 2, [0o555, 0o000, 0o100]):
            with self.subTest(" in ".join(oct(m) for m in reversed(modes))), tempfile.TemporaryDirectory() as root:
                top = locked_chain(root, "copy", modes)
                try:
                    self.assertTrue(testx._remove_tree(top))
                    self.assertFalse(os.path.lexists(top))
                finally:
                    unlock_all(root)

    def test_the_sweep_removes_a_stale_copy_with_folders_locked_one_inside_another(self):
        with tempfile.TemporaryDirectory() as parent:
            name = testx.PROVE_COPY + "stale"
            top = locked_chain(parent, name, [0o000] * 3)
            old = time.time() - 2 * 86400
            os.utime(top, (old, old))
            try:
                self.assertEqual([name], testx.sweep_prove_copies(parent))
                self.assertFalse(os.path.lexists(top))
            finally:
                unlock_all(parent)


class UnittestModuleFunctionTests(unittest.TestCase):
    # unittest runs only the test methods of a TestCase: a module-level function named test… is a helper there, whose
    # fix comes back as its base text, never a test prove names (unittest would refuse to run it)
    def test_a_module_function_named_like_a_test_is_a_helper_under_unittest(self):
        before = fake_module("def testing_env():\n    return 1\n\n\ndef h():\n    return 1\n\n\n")
        after = before.replace("def testing_env():\n    return 1", "def testing_env():\n    return 2").replace(
            "def h():\n    return 1", "def h():\n    return testing_env() - 1") + \
            "\n    def test_h(self):\n        self.assertEqual(h(), 1)\n"
        rc, rows, text = prove_rows(before, after)
        self.assertEqual([("test_calc.CalcTests.test_h", "does not test the change")], rows, text)


class SameShapeGroupTests(unittest.TestCase):
    # helpers of one class and name in the same places on both sides (a getter beside its setter) are matched place by
    # place, so a fix in one comes back as its base text and is proven
    def test_a_property_getter_fix_beside_its_setter_proves(self):
        before = fake_module(FAKE_PROPERTY)
        after = before.replace("        return self._x\n", "        return self._x * 10\n") + \
            "\n    def test_x(self):\n        self.assertEqual(Fake().x, 10)\n"
        rc, rows, text = prove_rows(before, after)
        self.assertEqual([("test_calc.CalcTests.test_x", "proves the change")], rows, text)
        self.assertEqual(0, rc, text)


# what a JS/TS module that changed outside its tests gives, as a cost test's child process compares it
COST_KEPT = f"KEPT = {{'text': None, 'first': {JS_NOT_REBUILT!r}}}\n"


class JsReaderCostTests(unittest.TestCase):
    # the JS readers run on every changed test module, so blanks after `function`, `function*`, a call, a parameter
    # list, an assignment or a quote must cost them linear time. A child process reads 40,000 blanks after each of
    # those, about 1 s in all here, and is stopped after 20 s: a pattern that backtracks over the blanks reads one
    # such text in 30 s or more (a cubic one in hours), so the bound tells them apart on a loaded machine too
    def test_the_js_readers_read_a_long_blank_run_in_linear_time(self):
        code = ("import sys\nsys.path.insert(0, sys.argv[1])\nfrom devkit import testx\n" + COST_KEPT +
                "for start in ('function', 'function*', 'function f', 'export function', 'x.function', 'catch', 'f()',"
                " 'f(): T', 'x =', 'x[0]', 'describe', 'describe(', \"'\"):\n"
                "    for blank in (' ', '\\t'):\n"
                "        text = start + blank * 40000 + ';\\n'\n"
                "        testx._js_support(text), testx._js_units(text)\n"
                "        assert testx._rebuild_helpers('a.test.js', text, text + 'x;\\n') == KEPT\n"
                "print('read')\n")
        lib = str(Path(__file__).resolve().parents[1] / "lib")
        try:
            p = subprocess.run([sys.executable, "-I", "-c", code, lib], capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            self.fail("the JS readers took more than 20 s on 40,000 blanks: a pattern backtracks over them")
        self.assertEqual("read", p.stdout.strip(), p.stderr)

    # a hook fix in a module of 8,000 nested describes, or of 2,000 describes one after another, each with a hook and
    # a test, is read in about a second here; a reader that rescans the describes around each test takes 20 s or more
    # on the nested one
    def test_a_hook_fix_among_thousands_of_describes_is_read_in_linear_time(self):
        code = ("import sys\nsys.path.insert(0, sys.argv[1])\nfrom devkit import testx\n" + COST_KEPT +
                "nested = \"describe('a', () => {\\n\" * 8000 + \"beforeEach(() => {\\n  state.n = 1;\\n});\\n\" + "
                "\"test('t', () => { expect(state.n).toBe(1); });\\n\" + '});\\n' * 8000\n"
                "flat = ''.join(f\"describe('case {i}', () => {{\\n  beforeEach(() => {{\\n    state.n = {i};\\n  }});\\n\""
                " f\"  test('one {i}', () => {{\\n    expect(state.n).toBe({i});\\n  }});\\n}});\\n\\n\" for i in range(2000))\n"
                "for text, fix in ((nested, ('state.n = 1;', 'state.n = 2;')), (flat, ('state.n = 3;', 'state.n = -7;'))):\n"
                "    assert testx._rebuild_helpers('a.test.js', text, text.replace(*fix)) == KEPT\n"
                "print('read')\n")
        lib = str(Path(__file__).resolve().parents[1] / "lib")
        try:
            p = subprocess.run([sys.executable, "-I", "-c", code, lib], capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            self.fail("reading a hook fix among thousands of describes took more than 20 s")
        self.assertEqual("read", p.stdout.strip(), p.stderr)

    # each declaration, hook, test and class finds where it ends in one pass over the brackets, so 5,000 of them
    # nested in one another, open or closed, are read in about 5 s in all here; a reader that scans from each to its
    # end takes 30 s or more on the 5,000 nested declarations alone
    def test_thousands_of_nested_declarations_hooks_tests_and_classes_read_in_linear_time(self):
        code = ("import sys\nsys.path.insert(0, sys.argv[1])\nfrom devkit import testx\n" + COST_KEPT +
                "for line, close in (('const x = () => {', '};'), ('beforeEach(() => {', '});'), "
                "(\"test('a', () => {\", '});'), ('class C {', '}'), ('function f() {', '}')):\n"
                "    for text in ((line + '\\n') * 5000, (line + '\\n') * 5000 + (close + '\\n') * 5000):\n"
                "        testx._js_support(text), testx._js_units(text)\n"
                "        assert testx._rebuild_helpers('a.test.js', text, 'y;\\n' + text) == KEPT\n"
                "print('read')\n")
        lib = str(Path(__file__).resolve().parents[1] / "lib")
        try:
            p = subprocess.run([sys.executable, "-I", "-c", code, lib], capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            self.fail("the JS readers took more than 30 s on 5,000 nested declarations, hooks, tests or classes")
        self.assertEqual("read", p.stdout.strip(), p.stderr)

    # a declaration on each of 20,000 lines that each end in an operator is one statement: each declaration ends where
    # the one before it found the statement's end, so the readers take about a second here; a reader that scans from
    # each declaration to that end takes many minutes
    def test_a_statement_over_thousands_of_declaration_lines_reads_in_linear_time(self):
        code = ("import sys\nsys.path.insert(0, sys.argv[1])\nfrom devkit import testx\n" + COST_KEPT +
                "for line in ('const x = 1 +', 'let y = a ||', 'var z = b,'):\n"
                "    text = (line + '\\n') * 20000 + '1;\\nfunction g() {\\n  return 1;\\n}\\n'\n"
                "    testx._js_support(text), testx._js_units(text)\n"
                "    assert testx._rebuild_helpers('a.test.js', text, text.replace('return 1;', 'return 2;')) == KEPT\n"
                "print('read')\n")
        lib = str(Path(__file__).resolve().parents[1] / "lib")
        try:
            p = subprocess.run([sys.executable, "-I", "-c", code, lib], capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            self.fail("the JS readers took more than 20 s on 20,000 declaration lines of one statement")
        self.assertEqual("read", p.stdout.strip(), p.stderr)

    # every reader of one module gets the same masked text, so the caches keyed by a text find it at once: a fix in
    # each of 32,000 helpers between as many tests is read in about a second here, where comparing each lookup's text
    # with an equal copy takes a minute
    def test_a_fix_among_thousands_of_helpers_and_tests_is_read_in_linear_time(self):
        code = ("import sys\nsys.path.insert(0, sys.argv[1])\nfrom devkit import testx\n" + COST_KEPT +
                "text = \"function g() {\\n  return 1;\\n}\\ntest('t', () => {});\\n\" * 32000\n"
                "assert testx._rebuild_helpers('a.test.js', text, text.replace('return 1', 'return 2')) == KEPT\n"
                "print('read')\n")
        lib = str(Path(__file__).resolve().parents[1] / "lib")
        try:
            p = subprocess.run([sys.executable, "-I", "-c", code, lib], capture_output=True, text=True, timeout=25)
        except subprocess.TimeoutExpired:
            self.fail("reading a fix in each of 32,000 helpers between as many tests took more than 25 s")
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
