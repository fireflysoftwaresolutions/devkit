# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev test prove`: which tests of a changed test file count as new or changed (testx._changed_units), and how their
results are matched (testx._member). Hermetic: tiny git repositories in temporary folders (test_testx.Env).
Run: python3 -m unittest tests.test_testx_prove -v"""
import sys
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import testx  # noqa: E402
from test_testx import Env, edit  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
