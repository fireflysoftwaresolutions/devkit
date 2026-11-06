# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# devkit: allow-stub-words (this file holds the markers it detects)
"""`dev test audit`: tests that cannot fail for a real bug (lib/devkit/testaudit.py).

Hermetic: every fixture is written to a temporary directory, and git runs with an isolated configuration.
Each rule has a case that must be flagged and a good test that must stay clean.
Run: python3 -m unittest tests.test_testaudit -v
"""
import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from devkit import core, testaudit  # noqa: E402

ISOLATE = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Ana Dev",
           "GIT_AUTHOR_EMAIL": "ana@example.com", "GIT_COMMITTER_NAME": "Ana Dev",
           "GIT_COMMITTER_EMAIL": "ana@example.com"}


def _show(findings) -> str:
    return "\n".join(f"  {f['file']}:{f['line']} {f['test']} {f['rule']} ({f['severity']}) {f['message']}"
                     for f in findings) or "  (no findings)"


class AuditCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.top = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel, text):
        p = self.top / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text).lstrip("\n"))
        return rel

    def audit(self, rel, text):
        self.write(rel, text)
        return testaudit.audit([rel], top=self.top)

    def flagged(self, findings, rule, test=None, severity=None):
        hits = [f for f in findings if f["rule"] == rule and (test is None or f["test"] == test)]
        self.assertTrue(hits, f"expected {rule} on {test!r}; got:\n{_show(findings)}")
        if severity:
            self.assertEqual(hits[0]["severity"], severity, _show(hits))
        return hits[0]

    def clean(self, findings, *rules):
        bad = [f for f in findings if not rules or f["rule"] in rules]
        self.assertEqual(bad, [], "unexpected findings:\n" + _show(bad))


# --------------------------------------------------------------------------- Python

PY_GOOD_PYTEST = '''
import pytest

from shop.cart import Cart, parse_qty


def assert_total(cart, expected):
    assert cart.total() == expected


@pytest.mark.parametrize("text, qty", [("1", 1), ("12", 12)])
def test_parse_qty_reads_digits(text, qty):
    assert parse_qty(text) == qty


@pytest.mark.parametrize("text", ["", "abc"])
def test_parse_qty_rejects_non_digits(text):
    with pytest.raises(ValueError, match="not a number"):
        parse_qty(text)


def test_total_sums_the_lines():
    cart = Cart()
    cart.add("apple", qty=2, price=3)
    assert_total(cart, 6)


class TestCheckout:
    def test_checkout_empties_the_cart(self):
        cart = Cart()
        cart.add("pear", qty=1, price=2)
        receipt = cart.checkout()
        assert receipt.total == 2
        assert cart.lines == []
'''

PY_GOOD_UNITTEST = '''
import time
import unittest
from unittest import mock

from shop import orders


class PlaceOrderTests(unittest.TestCase):
    def setUp(self):
        self.store = orders.Store()

    def _place(self, qty):
        order = self.store.place("apple", qty)
        self.assertEqual(order.qty, qty)
        return order

    def test_place_records_the_quantity(self):
        self._place(3)

    def test_place_rejects_zero(self):
        with self.assertRaises(orders.InvalidQuantity):
            self.store.place("apple", 0)

    @mock.patch("shop.orders.mailer")
    def test_place_mails_a_confirmation(self, mailer):
        order = self.store.place("apple", 1)
        mailer.send.assert_called_once_with(order.id)
        self.assertEqual(self.store.count(), 1)

    @unittest.skip("needs the payment sandbox, see #42")
    def test_refund_returns_the_money(self):
        pass

    def test_place_waits_until_the_stock_service_is_up(self):
        deadline = time.monotonic() + 2
        while not self.store.ready():
            if time.monotonic() > deadline:
                self.fail("the stock service never came up")
            time.sleep(0.01)
        self.assertEqual(self.store.place("apple", 1).qty, 1)

    def test_place_with_an_unknown_item_raises(self):
        try:
            self.store.place("durian", 1)
        except orders.UnknownItem as e:
            self.assertIn("durian", str(e))
        else:
            self.fail("no error for an unknown item")
'''


class PythonGoodTests(AuditCase):
    def test_realistic_pytest_suite_is_clean(self):
        self.clean(self.audit("tests/test_cart.py", PY_GOOD_PYTEST))

    def test_realistic_unittest_suite_is_clean(self):
        self.clean(self.audit("tests/test_orders.py", PY_GOOD_UNITTEST))

    def test_findings_have_the_documented_shape(self):
        f = self.audit("tests/test_x.py", """
            from shop import cart

            def test_cart_is_created():
                cart.Cart()
        """)[0]
        self.assertEqual(set(f), {"file", "line", "test", "rule", "severity", "message", "fix"})
        self.assertEqual((f["file"], f["line"], f["test"]), ("tests/test_x.py", 3, "test_cart_is_created"))
        self.assertIn(f["severity"], ("high", "medium", "low"))
        self.assertTrue(f["message"] and f["fix"] and "\n" not in f["message"] + f["fix"])


class PythonHighTests(AuditCase):
    def test_no_assertion_names_the_function_to_check(self):
        f = self.flagged(self.audit("tests/test_cart.py", """
            from shop.cart import parse_qty

            def test_parse_qty_reads_digits():
                parse_qty("12")
        """), "no-assertion", "test_parse_qty_reads_digits", "high")
        self.assertIn("parse_qty()", f["fix"])

    def test_assertion_helpers_and_fixtures_count_as_assertions(self):
        self.clean(self.audit("tests/test_cart.py", """
            from shop.cart import Cart
            from tests.helpers import run_scenario

            def check_total(cart, want):
                assert cart.total() == want

            def test_total_via_helper():
                check_total(Cart(), 0)

            def test_total_via_shared_helper():
                run_scenario("empty-cart")

            def test_total_via_fixture(cart_checker):
                cart_checker(Cart())

            def test_exit_code_via_check(tmp_path):
                import subprocess
                subprocess.run(["shop", "--version"], check=True)
        """), "no-assertion")

    def test_unknown_base_class_helpers_are_not_flagged(self):
        self.clean(self.audit("tests/test_api.py", """
            from tests.base import ApiTestCase

            class OrdersApiTests(ApiTestCase):
                def test_list_orders(self):
                    self.get_ok("/orders")
        """), "no-assertion")

    def test_trivial_assertions(self):
        findings = self.audit("tests/test_cart.py", """
            import unittest
            from shop.cart import Cart

            def test_total_of_an_empty_cart():
                Cart().total()
                assert True

            def test_total_is_stable():
                total = Cart().total()
                assert total == total

            def test_literals_only():
                assert 2 == 2

            class CartTests(unittest.TestCase):
                def test_cart_is_truthy(self):
                    cart = Cart()
                    self.assertTrue(True)
                    self.assertEqual(cart, cart)
        """)
        for name in ("test_total_of_an_empty_cart", "test_total_is_stable", "test_literals_only",
                     "CartTests.test_cart_is_truthy"):
            self.flagged(findings, "trivial-assertion", name, "high")
        self.clean(findings, "no-assertion")

    def test_real_assertions_next_to_a_trivial_one_are_not_trivial(self):
        self.clean(self.audit("tests/test_cart.py", """
            from shop.cart import Cart

            def test_total_of_an_empty_cart():
                assert Cart().total() == 0
                assert True
        """), "trivial-assertion")

    def test_empty_bodies(self):
        findings = self.audit("tests/test_cart.py", '''
            from shop.cart import Cart

            def test_pass():
                pass

            def test_ellipsis():
                ...

            def test_docstring_only():
                """Checks that an empty cart totals zero."""
        ''')
        for name in ("test_pass", "test_ellipsis", "test_docstring_only"):
            self.flagged(findings, "empty-test", name, "high")
        self.clean(findings, "no-assertion")

    def test_swallowed_failures(self):
        findings = self.audit("tests/test_cart.py", """
            import contextlib
            from shop.cart import Cart

            def test_total_bare_except():
                try:
                    assert Cart().total() == 0
                except Exception:
                    pass

            def test_total_assertion_error_printed():
                try:
                    assert Cart().total() == 0
                except AssertionError as e:
                    print(e)

            def test_total_suppressed():
                with contextlib.suppress(Exception):
                    assert Cart().total() == 0
        """)
        for name in ("test_total_bare_except", "test_total_assertion_error_printed", "test_total_suppressed"):
            self.flagged(findings, "swallowed-failure", name, "high")

    def test_narrow_or_reraising_handlers_are_not_swallowed(self):
        self.clean(self.audit("tests/test_cart.py", """
            from shop.cart import Cart, Closed

            def test_total_narrow_except():
                try:
                    assert Cart().total() == 0
                except Closed:
                    pass

            def test_total_reraises():
                try:
                    assert Cart().total() == 0
                except Exception:
                    Cart.reset()
                    raise

            def test_total_collects_errors():
                errors = []
                try:
                    assert Cart().total() == 0
                except AssertionError as e:
                    errors.append(e)
                assert not errors
        """), "swallowed-failure")


class PythonMediumLowTests(AuditCase):
    def test_skips_without_a_reason(self):
        findings = self.audit("tests/test_cart.py", """
            import unittest
            import pytest
            from shop.cart import Cart

            @pytest.mark.skip
            def test_total_bare_skip():
                assert Cart().total() == 0

            @unittest.skip("")
            def test_total_empty_reason():
                assert Cart().total() == 0

            @pytest.mark.skip(reason="the tax table moves to v2, see #31")
            def test_total_with_reason():
                assert Cart().total() == 0
        """)
        self.flagged(findings, "skipped-without-reason", "test_total_bare_skip", "medium")
        self.flagged(findings, "skipped-without-reason", "test_total_empty_reason")
        self.clean([f for f in findings if f["test"] == "test_total_with_reason"])

    def test_sleep_in_a_test_body(self):
        findings = self.audit("tests/test_cart.py", """
            import time
            from shop.cart import Cart

            def test_cart_expires():
                cart = Cart(ttl=0.1)
                time.sleep(0.5)
                assert cart.expired()
        """)
        f = self.flagged(findings, "sleep-in-test", "test_cart_expires", "medium")
        self.assertEqual(f["line"], 6)

    def test_duplicate_bodies_in_one_scope(self):
        findings = self.audit("tests/test_cart.py", """
            import pytest
            from shop.cart import Cart

            def test_total_starts_at_zero():
                assert Cart().total() == 0

            def test_new_cart_total():
                assert Cart().total() == 0

            class TestEuro:
                def test_total(self):
                    assert self.cart().total() == 0

            class TestDollar:
                def test_total(self):
                    assert self.cart().total() == 0

            @pytest.mark.parametrize("n", [1])
            def test_param_a(n):
                assert Cart(n).total() == 0

            @pytest.mark.parametrize("n", [2])
            def test_param_b(n):
                assert Cart(n).total() == 0
        """)
        f = self.flagged(findings, "duplicate-test", "test_new_cart_total", "medium")
        self.assertIn("test_total_starts_at_zero", f["message"])
        self.assertEqual([x["test"] for x in findings if x["rule"] == "duplicate-test"], ["test_new_cart_total"])

    def test_mock_only(self):
        findings = self.audit("tests/test_mail.py", """
            from unittest import mock
            from shop import mailer

            def test_mock_returns_what_it_was_told():
                send = mock.Mock(return_value=5)
                assert send() == 5

            @mock.patch("shop.mailer.smtp")
            def test_send_calls_smtp(smtp):
                mailer.send("ana@example.com", "hi")
                smtp.sendmail.assert_called_once()
        """)
        self.flagged(findings, "mock-only", "test_mock_returns_what_it_was_told", "medium")
        self.flagged(findings, "mock-only", "test_send_calls_smtp")

    def test_mock_check_with_a_state_check_is_not_mock_only(self):
        self.clean(self.audit("tests/test_mail.py", """
            from unittest import mock
            from shop import mailer

            def test_send_returns_the_message_id():
                smtp = mock.Mock()
                smtp.sendmail.return_value = {}
                result = mailer.send("ana@example.com", "hi", smtp=smtp)
                smtp.sendmail.assert_called_once()
                assert result.ok
        """), "mock-only")

    def test_snapshot_only(self):
        findings = self.audit("tests/test_receipt.py", """
            from shop.receipt import render

            def test_render_matches(snapshot):
                assert render({"total": 3}) == snapshot

            def test_render_with_a_real_check(snapshot):
                out = render({"total": 3})
                assert "Total: 3" in out
                assert out == snapshot
        """)
        self.flagged(findings, "snapshot-only", "test_render_matches", "medium")
        self.clean([f for f in findings if f["test"] == "test_render_with_a_real_check"])

    def test_no_production_reference(self):
        f = self.flagged(self.audit("tests/test_json.py", """
            import json
            import unittest

            class JsonTests(unittest.TestCase):
                def test_dumps_sorts_keys(self):
                    self.assertEqual(json.dumps({"b": 1, "a": 2}, sort_keys=True), '{"a": 2, "b": 1}')
        """), "no-production-reference", severity="medium")
        self.assertEqual(f["file"], "tests/test_json.py")

    def test_project_imports_or_subprocess_are_production_references(self):
        self.clean(self.audit("tests/test_codec.py", """
            import json
            from shop import codec

            def test_codec_matches_json():
                assert codec.dumps({"a": 1}) == json.dumps({"a": 1})
        """), "no-production-reference")
        self.clean(self.audit("tests/test_cli.py", """
            import subprocess
            import sys

            def test_cli_prints_its_version():
                out = subprocess.run([sys.executable, "bin/shop", "--version"], capture_output=True, text=True)
                assert out.stdout.startswith("shop ")
        """), "no-production-reference")

    def test_debug_output_and_vague_names(self):
        findings = self.audit("tests/test_cart.py", """
            from shop.cart import Cart

            def test1():
                assert Cart().total() == 0

            def test_it():
                cart = Cart()
                print(cart.lines)
                assert cart.total() == 0
        """)
        self.flagged(findings, "vague-name", "test1", "low")
        self.flagged(findings, "vague-name", "test_it", "low")
        f = self.flagged(findings, "debug-output", "test_it", "low")
        self.assertEqual(f["line"], 8)


class SmokeTests(AuditCase):
    def test_a_no_crash_test_is_visible_but_does_not_block(self):
        rel = self.write("tests/test_parse.py", """
            from app.parse import parse

            def test_parse_does_not_crash_on_empty_input():
                parse("")
            """)
        found = testaudit.audit([rel], top=self.top)
        self.flagged(found, "smoke-only", severity="medium")
        self.clean(found, "no-assertion")
        self.assertIsNone(testaudit.stop_gate(str(self.top), [str(self.top / rel)]))


class PythonRealWorldTests(AuditCase):
    """Patterns found while auditing real suites (numpy, jsonschema, jinja2, kit-consulting) that must stay clean."""

    def test_a_module_of_template_tests_is_not_a_test_file(self):
        self.clean(self.audit("jinja2/tests.py", '''
            """Built-in template tests used with the ``is`` operator."""

            def test_odd(value):
                """Return true if the variable is odd."""
                return value % 2 == 1
        '''))
        self.flagged(self.audit("shop/tests.py", """
            from django.test import TestCase
            from shop.cart import Cart

            class CartTests(TestCase):
                def test_cart_adds(self):
                    Cart().add(1)
        """), "no-assertion", "CartTests.test_cart_adds")

    def test_checks_that_raise_on_failure_count_as_assertions(self):
        self.clean(self.audit("tests/test_utils.py", """
            import subprocess
            from numpy.testing import assert_equal
            from shop import schema

            ARGS = {"capture_output": True, "check": True}

            class EqualTests:
                _assert_func = staticmethod(assert_equal)

                def test_equal_arrays(self):
                    self._assert_func([1, 2], [1, 2])

            def test_schema_accepts_a_point():
                schema.validator().validate({"x": 1, "y": 2})

            def test_cli_runs(tmp_path):
                subprocess.run(["shop", "--version"], **ARGS)
        """), "no-assertion")

    def test_documented_crash_and_regression_tests_are_not_flagged(self):
        findings = self.audit("tests/test_regression.py", '''
            import numpy as np
            from shop import cli, docs

            def test_hash_runs():
                """Only test hash runs at all."""
                hash(np.dtype("f8"))

            def test_mutated_list():
                obj = [[2, 3]]
                # Does not crash:
                np.array(obj)

            def test_searchsorted_structured():
                # gh-28190
                np.array([(0, 1.0)]).searchsorted(0)

            def test_misaligned_objects_segfault():
                repr(np.zeros((10,), dtype="O,c"))

            def test_guide_commands_parse():
                """argparse exits on an unknown flag, so this fails if the guide shows a flag the CLI lacks."""
                cli.build_parser().parse_args(docs.command().split())

            def test_record_array_with_object_field():
                # Trac #1839
                np.zeros(2)[1]

            def test_empty_string_to_object():
                # Pull request #4722
                np.array(["", ""]).astype(object)

            def test_many_arguments():
                # This used to be limited by NPY_MAXARGS == 32
                np.select([np.array([False])] * 100, [np.array([1])] * 100)

            def test_deep_nonragged_object():
                # None of these should raise, even though they are missing dtype=object
                np.array([[[1]]])

            class TestZeroSize:
                def _sort_all(self, name):
                    # Previously, these would all hang
                    getattr(np.zeros(0), name)()

                def test_sort(self):
                    self._sort_all("sort")

            def test_multinomial_basic():
                np.random.multinomial(100, [0.2, 0.8])
        ''')
        self.assertEqual([f["test"] for f in findings if f["rule"] == "no-assertion"], ["test_multinomial_basic"],
                         _show(findings))

    def test_sleep_in_a_nested_callback_simulates_work(self):
        self.clean(self.audit("tests/test_threads.py", """
            import threading
            import time
            from shop import pool

            def test_pool_is_thread_safe():
                def work():
                    time.sleep(0.001)
                    return 1
                assert pool.run_all([work] * 4) == [1, 1, 1, 1]
        """), "sleep-in-test")

    def test_a_name_that_is_the_function_under_test_is_not_vague(self):
        self.clean(self.audit("tests/test_index_tricks.py", """
            import numpy as np

            def test_c_():
                assert np.c_[1, 2].shape == (1, 2)
        """), "vague-name")


# --------------------------------------------------------------------------- JavaScript / TypeScript

JS_GOOD = r"""
import { describe, expect, it, vi } from 'vitest';
import { createUser, fetchUser, parse } from '../src/user';

const BRACES = '}{';          // a brace in a string must not end a block
const PATTERN = /\{[^}]*\}/g; // nor one in a regular expression

describe('parse', () => {
  it.each([
    ['1', 1],
    ['12', 12],
  ])('reads %s as a number', (input, want) => {
    expect(parse(input)).toBe(want);
  });

  it('keeps braces in templates', () => {
    const name = 'x';
    // a closing brace in a comment: }
    expect(parse(`${name}{}`)).toEqual({ name, braces: BRACES.length, ok: PATTERN.test('{a}') });
  });

  it('rejects letters', async () => {
    await expect(fetchUser('abc')).rejects.toThrow('not a number');
  });
});

describe('createUser', () => {
  it('saves the user and returns it', () => {
    const repo = { save: vi.fn() };
    const user = createUser(repo, 'Ana');
    expect(repo.save).toHaveBeenCalledWith(user);
    expect(user.name).toBe('Ana');
  });

  // flaky against the staging API, see #12
  it.skip('syncs with the server', async () => {
    await createUser(null, 'Bo').sync();
  });

  test('stores an inline snapshot', () => {
    expect(createUser(null, 'Cy').toJSON()).toMatchInlineSnapshot(`{"name": "Cy"}`);
  });
});
"""

JSX_GOOD = r"""
import { render, screen } from '@testing-library/react';
import { Banner } from './Banner';

test('shows the warning text', () => {
  render(<Banner kind="warn">Don't panic {'}'}</Banner>);
  expect(screen.getByText(/Don't panic/)).toBeInTheDocument();
});

test('renders nothing without a message', () => {
  const { container } = render(<Banner />);
  expect(container.firstChild).toBeNull();
});
"""

NODE_TEST_GOOD = r"""
import assert from 'node:assert/strict';
import test from 'node:test';
import { setupSteps } from '../setup.mjs';

test('setup lists the steps in order', () => {
  assert.deepEqual(setupSteps({}).map((s) => s.name), ['Node packages', 'Check the setup']);
});

test('ci skips the git hooks', async (t) => {
  await t.test('without hooks', () => {
    assert.ok(!setupSteps({ ci: true }).some((s) => s.name === 'Git hooks'));
  });
});
"""


class JavaScriptTests(AuditCase):
    def test_realistic_suites_are_clean(self):
        self.clean(self.audit("web/user.test.ts", JS_GOOD))
        self.clean(self.audit("web/Banner.test.jsx", JSX_GOOD))
        self.clean(self.audit("scripts/test/setup.test.mjs", NODE_TEST_GOOD))

    def test_scanner_survives_strings_regexes_comments_and_jsx(self):
        findings = self.audit("web/Note.test.jsx", r"""
            import { render } from '@testing-library/react';
            import { Note } from './Note';

            test('renders tricky text', () => {
              const re = /[}{]+/;                // braces in a regex
              const s = "it('fake', () => {})";  // a fake test in a string
              const tpl = `}${'{'}`;             // braces in a template
              /* it('commented out', () => {}) */
              render(<Note title="it's {fine}">Don't {"}"} worry</Note>);
              expect(re.test(s + tpl)).toBe(true);
            });

            test('is still found after the tricky one', () => {});
        """)
        self.assertEqual([(f["rule"], f["test"], f["line"]) for f in findings],
                         [("empty-test", "is still found after the tricky one", 13)], _show(findings))

    def test_no_assertion(self):
        f = self.flagged(self.audit("web/date.test.js", """
            const { parse } = require('../src/date');

            test('parses the date', () => {
              parse('2026-01-01');
            });
        """), "no-assertion", "parses the date", "high")
        self.assertIn("parse()", f["fix"])
        self.assertEqual(f["line"], 3)

    def test_e2e_and_param_assertions_are_not_flagged(self):
        self.clean(self.audit("e2e/login.spec.ts", """
            import { test } from '@playwright/test';

            test('signs in', async ({ page }) => {
              await page.goto('/login');
              await page.getByRole('button', { name: 'Sign in' }).click();
            });
        """), "no-assertion")
        self.clean(self.audit("test/tap.test.js", """
            const { test } = require('tap');
            const { sum } = require('../sum');
            test('adds', (t) => { t.equal(sum(1, 2), 3); t.end(); });
        """), "no-assertion")

    def test_trivial_assertions(self):
        findings = self.audit("web/sum.test.js", """
            import { sum } from '../src/sum';

            it('works out the total', () => {
              const total = sum([1, 2]);
              expect(true).toBe(true);
            });

            it('compares the total with itself', () => {
              const total = sum([1, 2]);
              expect(total).toEqual(total);
            });

            it('asserts a literal', () => {
              sum([]);
              assert.equal(1, 1);
            });
        """)
        for name in ("works out the total", "compares the total with itself", "asserts a literal"):
            self.flagged(findings, "trivial-assertion", name, "high")
        f = self.flagged(findings, "trivial-assertion", "works out the total")
        self.assertTrue(f["message"].endswith(": expect(true).toBe(true)"), f["message"])

    def test_empty_bodies(self):
        findings = self.audit("web/sum.test.js", """
            import { sum } from '../src/sum';

            it('handles empty input', () => {});
            test('handles one item', function () {
              // TODO
            });
        """)
        self.flagged(findings, "empty-test", "handles empty input", "high")
        self.flagged(findings, "empty-test", "handles one item")

    def test_swallowed_failure(self):
        findings = self.audit("web/parse.test.js", """
            import { parse } from '../src/parse';

            it('parses x', () => {
              try {
                expect(parse('x')).toBe(1);
              } catch (e) {
                console.log(e);
              }
            });

            it('reports bad input', () => {
              try {
                parse('?');
                expect.fail('no error');
              } catch (e) {
                expect(e.message).toBe('bad input');
              }
            });
        """)
        self.flagged(findings, "swallowed-failure", "parses x", "high")
        self.clean([f for f in findings if f["test"] == "reports bad input"])

    def test_skips_without_a_reason(self):
        findings = self.audit("web/mail.test.js", """
            import { send } from '../src/mail';

            it.skip('sends the email', () => {
              expect(send('a')).toBe(true);
            });

            xit('retries', () => {
              expect(send('b')).toBe(true);
            });

            describe.skip('templates', () => {
              it('renders the subject', () => {
                expect(send('c')).toBe(true);
              });
            });

            it.skip('queues when offline', () => { // needs a fake network, see #77
              expect(send('d')).toBe(true);
            });
        """)
        self.flagged(findings, "skipped-without-reason", "sends the email", "medium")
        self.flagged(findings, "skipped-without-reason", "retries")
        self.flagged(findings, "skipped-without-reason", "templates")
        self.clean([f for f in findings if f["test"] in ("queues when offline", "renders the subject")])

    def test_sleep(self):
        findings = self.audit("web/save.test.js", """
            import { save } from '../src/save';
            import { waitFor } from '@testing-library/react';

            it('saves after a pause', async () => {
              save();
              await new Promise((r) => setTimeout(r, 500));
              expect(save.done).toBe(true);
            });

            it('saves eventually', async () => {
              save();
              await waitFor(() => expect(save.done).toBe(true));
            });
        """)
        self.flagged(findings, "sleep-in-test", "saves after a pause", "medium")
        self.clean([f for f in findings if f["test"] == "saves eventually"])

    def test_delays_that_simulate_work_or_yield_are_not_sleeps(self):
        self.clean(self.audit("web/async.test.ts", """
            import { z } from '../src';

            test('async refinements run', async () => {
              const schema = z.number().refine(async () => new Promise((r) => { setTimeout(() => r(false), 500); }));
              expect((await schema.safeParseAsync(4)).success).toBe(false);
            });

            test('a late reader sees the error', async () => {
              const p = z.string().parseAsync(1).catch((e) => e);
              await new Promise((resolve) => setTimeout(resolve, 0));
              expect(await p).toBeInstanceOf(Error);
            });
        """), "sleep-in-test")

    def test_duplicates_only_within_one_describe(self):
        findings = self.audit("web/role.test.js", """
            import { can } from '../src/role';

            describe('admin', () => {
              it('can see the dashboard', () => { expect(can('dash')).toBe(true); });
              it('can open the dashboard', () => { expect(can('dash')).toBe(true); });
            });

            describe('guest', () => {
              it('can see the dashboard', () => { expect(can('dash')).toBe(true); });
            });
        """)
        dups = [f for f in findings if f["rule"] == "duplicate-test"]
        self.assertEqual([(f["test"], f["line"]) for f in dups], [("can open the dashboard", 5)], _show(findings))

    def test_mock_only(self):
        findings = self.audit("web/notify.test.ts", """
            import { vi } from 'vitest';
            import { notify } from '../src/notify';

            it('calls the callback', () => {
              const fn = vi.fn();
              fn(1);
              expect(fn).toHaveBeenCalledWith(1);
            });

            it('returns the mocked value', () => {
              const get = vi.fn().mockReturnValue(5);
              expect(get()).toBe(5);
            });
        """)
        self.flagged(findings, "mock-only", "calls the callback", "medium")
        self.flagged(findings, "mock-only", "returns the mocked value")

    def test_snapshot_only(self):
        findings = self.audit("web/card.test.js", """
            import { renderCard } from '../src/card';

            it('renders the card', () => {
              expect(renderCard({ title: 'A' })).toMatchSnapshot();
            });

            it('renders the title and the card', () => {
              const html = renderCard({ title: 'A' });
              expect(html).toContain('<h2>A</h2>');
              expect(html).toMatchSnapshot();
            });
        """)
        self.flagged(findings, "snapshot-only", "renders the card", "medium")
        self.clean([f for f in findings if f["test"] == "renders the title and the card"])

    def test_no_production_reference(self):
        self.flagged(self.audit("test/math.test.mjs", """
            import assert from 'node:assert';
            import { test } from 'node:test';

            test('adds two numbers', () => {
              assert.equal(Math.max(1, 2), 2);
            });
        """), "no-production-reference", severity="medium")

    def test_type_level_assertions_count(self):
        self.clean(self.audit("src/tests/set.test.ts", """
            import { z } from '../index';
            import * as util from '../helpers/util';

            test('type inference', () => {
              util.assertEqual<z.infer<typeof z.string>, string>(true);
            });

            test('brands are nominal', () => {
              const branded = (_: z.output<typeof z.string>) => {};
              // @ts-expect-error
              branded(42);
            });

            test('readonly in ZodEnumDef', () => {
              type _a = z.ZodEnum<{ readonly a: 'a' }>;
            });
        """), "no-assertion", "trivial-assertion")

    def test_passing_the_test_object_to_a_helper_counts(self):
        self.clean(self.audit("test/equal.test.js", """
            const test = require('tape');
            const { equal } = require('..');

            function runTest(t, suite, fn) {
              for (const s of suite) t.equal(fn(...s.pair), s.result);
            }

            test('URI Equals', (t) => {
              runTest(t, [{ pair: ['a', 'A'], result: true }], equal);
              t.end();
            });
        """), "no-assertion")

    def test_documented_smoke_test_is_not_flagged(self):
        self.clean(self.audit("web/parse.test.js", """
            import { parse } from '../src/parse';

            it('does not throw on empty input', () => {
              parse('');
            });
        """), "no-assertion")

    def test_debug_output_and_vague_names(self):
        findings = self.audit("web/sum.test.js", """
            import { sum } from '../src/sum';

            it('works', () => {
              console.log(sum([1]));
              expect(sum([1])).toBe(1);
            });
        """)
        self.flagged(findings, "vague-name", "works", "low")
        self.flagged(findings, "debug-output", "works", "low")


# --------------------------------------------------------------------------- Java / Kotlin

JAVA_GOOD = r"""
package com.acme.billing;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;

import org.junit.jupiter.api.Disabled;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

class InvoiceServiceTest {
    private final InvoiceRepository repo = mock(InvoiceRepository.class);
    private final InvoiceService service = new InvoiceService(repo);

    @Test
    void totalAddsTheLines() {
        Invoice invoice = service.create("ACME", 2, new Money("10.00"));
        assertThat(invoice.total())
            .isNotNull()
            .isEqualTo(new Money("20.00"));
    }

    @ParameterizedTest
    @ValueSource(ints = {0, -1})
    void createRejectsQuantitiesBelowOne(int qty) {
        assertThatThrownBy(() -> service.create("ACME", qty, new Money("1.00")))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("quantity");
    }

    @Test
    void createSavesTheInvoice() {
        Invoice invoice = service.create("ACME", 1, new Money("5.00"));
        verify(repo).save(invoice);
        assertThat(invoice.status()).isEqualTo(Status.DRAFT);
    }

    @Disabled("waits for the tax service sandbox, see BILL-88")
    @Test
    void createAddsTax() {
    }

    @Test
    void parseReadsBracesInStrings() {
        String text = "{\"amount\": \"}\"}";
        char brace = '}';
        assertThat(service.parse(text + brace)).isEqualTo(new Money("0"));
    }
}
"""

KOTLIN_GOOD = r"""
package com.acme.billing

import io.kotest.matchers.shouldBe
import org.junit.jupiter.api.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith

class MoneyTest {
    @Test
    fun `adds two amounts`() {
        assertEquals(Money(5), Money(2) + Money(3))
    }

    @Test
    fun `formats with braces in a template`() {
        val m = Money(7)
        "${m.format()}{}" shouldBe "7.00 EUR{}"
    }

    @Test
    fun `rejects negative amounts`() {
        assertFailsWith<IllegalArgumentException> { Money(-1) }
    }
}
"""

JAVA_HEAD = """
package com.acme.billing;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

import org.junit.jupiter.api.Disabled;
import org.junit.jupiter.api.Test;
"""


class JavaKotlinTests(AuditCase):
    def java(self, body):
        return self.audit("src/test/java/com/acme/billing/InvoiceTest.java",
                          textwrap.dedent(JAVA_HEAD) + "\nclass InvoiceTest {\n" + textwrap.dedent(body) + "}\n")

    def test_realistic_suites_are_clean(self):
        self.clean(self.audit("src/test/java/com/acme/billing/InvoiceServiceTest.java", JAVA_GOOD))
        self.clean(self.audit("src/test/kotlin/com/acme/billing/MoneyTest.kt", KOTLIN_GOOD))

    def test_high_rules(self):
        findings = self.java("""
            private final InvoiceService service = new InvoiceService();

            @Test
            void createRuns() {
                service.create("ACME", 1);
            }

            @Test
            void totalIsTrue() {
                service.create("ACME", 1);
                assertTrue(true);
            }

            @Test
            void notWrittenYet() {
            }

            @Test
            void totalSwallowed() {
                try {
                    assertEquals(1, service.create("ACME", 1).total());
                } catch (Throwable t) {
                    t.printStackTrace();
                }
            }

            @Test
            void totalCatchesOnlyExceptions() {
                try {
                    assertEquals(1, service.create("ACME", 1).total());
                } catch (Exception e) {
                }
            }
        """)
        f = self.flagged(findings, "no-assertion", "InvoiceTest.createRuns", "high")
        self.assertIn("create()", f["fix"])
        self.flagged(findings, "trivial-assertion", "InvoiceTest.totalIsTrue", "high")
        self.flagged(findings, "empty-test", "InvoiceTest.notWrittenYet", "high")
        self.flagged(findings, "swallowed-failure", "InvoiceTest.totalSwallowed", "high")
        # catch (Exception) does not catch an AssertionError in Java
        self.clean([f for f in findings if f["test"] == "InvoiceTest.totalCatchesOnlyExceptions"])

    def test_medium_and_low_rules(self):
        findings = self.java("""
            private final InvoiceRepository repo = mock(InvoiceRepository.class);
            private final InvoiceService service = new InvoiceService(repo);

            @Disabled
            @Test
            void sendsReminders() {
                assertEquals(2, service.reminders().size());
            }

            @Test
            void waitsForTheQueue() throws Exception {
                service.enqueue("x");
                Thread.sleep(200);
                assertEquals(1, service.processed());
            }

            @Test
            void savesTheInvoice() {
                service.create("ACME", 1);
                verify(repo).save(any());
            }

            @Test
            void test1() {
                System.out.println(service.total());
                assertEquals(0, service.total());
            }

            @Test
            void totalStartsAtZero() {
                assertEquals(0, service.total());
            }

            @Test
            void newInvoiceTotal() {
                assertEquals(0, service.total());
            }
        """)
        self.flagged(findings, "skipped-without-reason", "InvoiceTest.sendsReminders", "medium")
        self.flagged(findings, "sleep-in-test", "InvoiceTest.waitsForTheQueue", "medium")
        self.flagged(findings, "mock-only", "InvoiceTest.savesTheInvoice", "medium")
        self.flagged(findings, "vague-name", "InvoiceTest.test1", "low")
        self.flagged(findings, "debug-output", "InvoiceTest.test1", "low")
        self.flagged(findings, "duplicate-test", "InvoiceTest.newInvoiceTotal", "medium")

    def test_no_production_reference(self):
        self.flagged(self.audit("src/test/java/com/acme/MathTest.java", """
            package com.acme;

            import static org.junit.jupiter.api.Assertions.assertEquals;
            import org.junit.jupiter.api.Test;

            class MathTest {
                @Test
                void addsExactly() {
                    assertEquals(4, Math.addExact(2, 2));
                }
            }
        """), "no-production-reference", severity="medium")

    def test_kotlin_empty_and_missing_assertion(self):
        findings = self.audit("src/test/kotlin/com/acme/CartTest.kt", """
            package com.acme

            import org.junit.jupiter.api.Test

            class CartTest {
                private val cart = Cart()

                @Test
                fun `adds an item`() {
                    cart.add("pear")
                }

                @Test
                fun `removes an item`() {
                }
            }
        """)
        self.flagged(findings, "no-assertion", "CartTest.adds an item", "high")
        self.flagged(findings, "empty-test", "CartTest.removes an item", "high")


# --------------------------------------------------------------------------- Go

GO_PROD = """
package calc

import "errors"

type Value struct{ N int }

func Add(a, b int) int { return a + b }

func Parse(s string) (Value, error) { return Value{}, errors.New("todo") }

func Divide(a, b int) int { return a / b }
"""

GO_GOOD = r"""
package calc

import (
	"testing"

	"github.com/stretchr/testify/require"
)

func TestAdd(t *testing.T) {
	cases := []struct {
		name    string
		a, b    int
		want    int
	}{
		{"positive", 1, 2, 3},
		{"negative", -1, -2, -3},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := Add(tc.a, tc.b); got != tc.want {
				t.Errorf("Add(%d, %d) = %d, want %d", tc.a, tc.b, got, tc.want)
			}
		})
	}
}

func TestParseReadsBraces(t *testing.T) {
	v, err := Parse(`{"n": "}"}`)
	require.NoError(t, err)
	require.Equal(t, 1, v.N)
}

func TestDivideByZeroPanics(t *testing.T) {
	if testing.Short() {
		t.Skip()
	}
	checkPanics(t, func() { Divide(1, 0) })
}

func checkPanics(t *testing.T, fn func()) {
	t.Helper()
	defer func() {
		if recover() == nil {
			t.Fatal("expected a panic")
		}
	}()
	fn()
}
"""


class GoTests(AuditCase):
    def setUp(self):
        super().setUp()
        self.write("calc/calc.go", GO_PROD)

    def test_table_tests_and_helpers_are_clean(self):
        self.clean(self.audit("calc/calc_test.go", GO_GOOD))

    def test_rules(self):
        findings = self.audit("calc/more_test.go", """
            package calc

            import (
            	"fmt"
            	"testing"
            	"time"

            	"github.com/stretchr/testify/assert"
            )

            func TestAddRuns(t *testing.T) {
            	Add(1, 2)
            }

            func TestAddIsOne(t *testing.T) {
            	Add(1, 2)
            	assert.Equal(t, 1, 1)
            }

            func TestDivideLater(t *testing.T) {
            }

            func TestDivideSkipped(t *testing.T) {
            	t.Skip()
            	if Divide(4, 2) != 2 {
            		t.Error("wrong")
            	}
            }

            func TestAddWaits(t *testing.T) {
            	time.Sleep(10 * time.Millisecond)
            	if Add(1, 1) != 2 {
            		t.Fatal("wrong")
            	}
            }

            func Test1(t *testing.T) {
            	fmt.Println(Add(2, 2))
            	if Add(2, 2) != 4 {
            		t.Fatal("wrong")
            	}
            }
        """)
        f = self.flagged(findings, "no-assertion", "TestAddRuns", "high")
        self.assertIn("Add()", f["fix"])
        self.flagged(findings, "trivial-assertion", "TestAddIsOne", "high")
        self.flagged(findings, "empty-test", "TestDivideLater", "high")
        self.flagged(findings, "skipped-without-reason", "TestDivideSkipped", "medium")
        self.flagged(findings, "sleep-in-test", "TestAddWaits", "medium")
        self.flagged(findings, "vague-name", "Test1", "low")
        self.flagged(findings, "debug-output", "Test1", "low")

    def test_mock_only_and_duplicates(self):
        findings = self.audit("calc/store_test.go", """
            package calc

            import "testing"

            func TestStoreGet(t *testing.T) {
            	m := new(MockStore)
            	m.On("Get").Return(1)
            	m.Get()
            	m.AssertExpectations(t)
            }

            func TestAddSmall(t *testing.T) {
            	if Add(1, 1) != 2 {
            		t.Fatal("wrong")
            	}
            }

            func TestAddTiny(t *testing.T) {
            	if Add(1, 1) != 2 {
            		t.Fatal("wrong")
            	}
            }
        """)
        self.flagged(findings, "mock-only", "TestStoreGet", "medium")
        self.flagged(findings, "duplicate-test", "TestAddTiny", "medium")

    def test_no_production_reference(self):
        findings = self.audit("calc/strings_test.go", """
            package calc

            import (
            	"strings"
            	"testing"
            )

            func TestRepeatBuildsTheString(t *testing.T) {
            	if strings.Repeat("a", 3) != "aaa" {
            		t.Fatal("wrong")
            	}
            }
        """)
        self.flagged(findings, "no-production-reference", severity="medium")


# --------------------------------------------------------------------------- PHP

PHP_GOOD = r"""
<?php

namespace Tests\Unit;

use App\Billing\InvalidQuantity;
use App\Billing\Invoice;
use PHPUnit\Framework\Attributes\Test;
use PHPUnit\Framework\TestCase;

final class InvoiceTest extends TestCase
{
    public function testTotalAddsTheLines(): void
    {
        $invoice = new Invoice(['qty' => 2, 'price' => 10]);
        $this->assertSame(20, $invoice->total());
    }

    /** @test */
    public function it_rejects_zero_quantities(): void
    {
        $this->expectException(InvalidQuantity::class);
        new Invoice(['qty' => 0, 'price' => 10]);
    }

    #[Test]
    public function keepsBracesInStrings(): void
    {
        $text = '{"note": "}"}'; # a brace in a string
        $this->assertSame('}', Invoice::fromJson($text)->note());
    }
}
"""

PEST_GOOD = r"""
<?php

use App\Billing\Invoice;

it('adds the lines', function () {
    expect((new Invoice(['qty' => 2, 'price' => 10]))->total())->toBe(20);
});

test('rejects zero quantities', function () {
    new Invoice(['qty' => 0, 'price' => 10]);
})->throws(InvalidArgumentException::class);

it('formats the total', fn () => expect(Invoice::format(5))->toBe('5.00'));
"""


class PhpTests(AuditCase):
    def test_realistic_suites_are_clean(self):
        self.clean(self.audit("tests/Unit/InvoiceTest.php", PHP_GOOD))
        self.clean(self.audit("tests/Feature/InvoicePestTest.php", PEST_GOOD))

    def test_phpunit_rules(self):
        findings = self.audit("tests/Unit/CartTest.php", """
            <?php

            use App\\Cart;
            use PHPUnit\\Framework\\TestCase;

            class CartTest extends TestCase
            {
                public function testAddRuns(): void
                {
                    (new Cart())->add('pear');
                }

                public function testTotalIsTrue(): void
                {
                    (new Cart())->total();
                    $this->assertTrue(true);
                }

                public function testRemove(): void
                {
                }

                public function testTotalSwallowed(): void
                {
                    try {
                        $this->assertSame(0, (new Cart())->total());
                    } catch (\\Exception $e) {
                    }
                }

                public function testCheckoutLater(): void
                {
                    $this->markTestSkipped();
                }

                public function testIt(): void
                {
                    var_dump((new Cart())->total());
                    sleep(1);
                    $this->assertSame(0, (new Cart())->total());
                }
            }
        """)
        f = self.flagged(findings, "no-assertion", "CartTest.testAddRuns", "high")
        self.assertIn("add()", f["fix"])
        f = self.flagged(findings, "trivial-assertion", "CartTest.testTotalIsTrue", "high")
        self.assertTrue(f["message"].endswith(": assertTrue(true)"), f["message"])
        self.flagged(findings, "empty-test", "CartTest.testRemove", "high")
        self.flagged(findings, "swallowed-failure", "CartTest.testTotalSwallowed", "high")
        self.flagged(findings, "skipped-without-reason", "CartTest.testCheckoutLater", "medium")
        self.flagged(findings, "sleep-in-test", "CartTest.testIt", "medium")
        self.flagged(findings, "debug-output", "CartTest.testIt", "low")
        self.flagged(findings, "vague-name", "CartTest.testIt", "low")

    def test_pest_rules(self):
        findings = self.audit("tests/Feature/CartTest.php", """
            <?php

            use App\\Cart;

            it('adds an item', function () {
                expect(true)->toBeTrue();
            });

            it('removes an item', function () {
                expect((new Cart())->remove('x'))->toBe(0);
            })->skip();

            test('empties the cart', function () {
            });
        """)
        self.flagged(findings, "trivial-assertion", "adds an item", "high")
        self.flagged(findings, "skipped-without-reason", "removes an item", "medium")
        self.flagged(findings, "empty-test", "empties the cart", "high")


# --------------------------------------------------------------------------- the Stop hook gate

class StopGateTests(AuditCase):
    def test_high_findings_block_with_at_most_eight_lines(self):
        body = "from shop.cart import Cart\n\n" + "".join(
            f"def test_case_{i}_adds():\n    Cart().add({i})\n\n" for i in range(10))
        body += "def test_case_waits():\n    import time\n    time.sleep(1)\n    assert Cart().total() == 0\n"
        self.write("tests/test_many.py", body)
        msg = testaudit.stop_gate(self.top, ["tests/test_many.py"])
        self.assertIsNotNone(msg)
        lines = msg.splitlines()
        hits = [ln for ln in lines if ln.lstrip().startswith("tests/test_many.py:")]
        self.assertEqual(len(hits), 8, msg)
        self.assertIn("tests/test_many.py:3 test_case_0_adds — no-assertion: ", hits[0])
        self.assertIn(" → ", hits[0])
        self.assertIn("1 medium", msg)
        self.assertIn("dev test audit", msg)
        self.assertIn("fail for a real bug, or delete it", lines[-1])

    def test_no_high_findings_means_no_block(self):
        self.write("tests/test_slow.py", """
            import time
            from shop.cart import Cart

            def test_total_after_a_pause():
                time.sleep(1)
                assert Cart().total() == 0
        """)
        self.assertIsNone(testaudit.stop_gate(self.top, ["tests/test_slow.py"]))
        self.assertIsNone(testaudit.stop_gate(self.top, []))

    def test_gate_ignores_missing_and_non_test_files(self):
        self.write("src/shop.py", "def total():\n    return 0\n")
        self.assertIsNone(testaudit.stop_gate(self.top, ["src/shop.py", "tests/test_deleted.py"]))


# --------------------------------------------------------------------------- changed files

def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=dict(os.environ, **ISOLATE))


class ChangedTestFilesTests(unittest.TestCase):
    def setUp(self):
        self.saved = {k: core.GIT_ENV.get(k) for k in ISOLATE}
        core.GIT_ENV.update(ISOLATE)
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        origin, self.work = root / "origin.git", root / "work"
        _git(root, "init", "-q", "--bare", "-b", "main", str(origin))
        _git(root, "clone", "-q", str(origin), str(self.work))
        self.put("tests/test_old.py", "def test_old_case():\n    assert 1\n")
        self.put("tests/test_gone.py", "def test_gone_case():\n    assert 1\n")
        self.put("src/app.py", "X = 1\n")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-q", "-m", "base")
        _git(self.work, "push", "-q", "origin", "HEAD:main")
        _git(self.work, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.base = subprocess.run(["git", "-C", str(self.work), "rev-parse", "HEAD"], capture_output=True,
                                   text=True, check=True).stdout.strip()
        _git(self.work, "switch", "-q", "-c", "feat/cart")
        self.put("tests/test_committed.py", "def test_committed_case():\n    assert 1\n")
        (self.work / "tests/test_gone.py").unlink()
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-q", "-m", "work")
        self.put("tests/test_staged.py", "def test_staged_case():\n    assert 1\n")
        _git(self.work, "add", "tests/test_staged.py")
        self.put("tests/test_old.py", "def test_old_case():\n    assert 2\n")
        self.put("web/cart.test.ts", "it('adds', () => { expect(1).toBe(1); });\n")
        self.put("src/app.py", "X = 2\n")

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                core.GIT_ENV.pop(k, None)
            else:
                core.GIT_ENV[k] = v
        self._tmp.cleanup()

    def put(self, rel, text):
        p = self.work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def test_branch_staged_unstaged_and_untracked_test_files(self):
        self.assertEqual(testaudit.changed_test_files(self.work), [
            "tests/test_committed.py", "tests/test_old.py", "tests/test_staged.py", "web/cart.test.ts"])

    def test_without_testx_the_local_git_fallback_gives_the_same_files(self):
        real = testaudit._testx
        testaudit._testx = lambda: None
        try:
            self.assertEqual(testaudit.changed_test_files(self.work), [
                "tests/test_committed.py", "tests/test_old.py", "tests/test_staged.py", "web/cart.test.ts"])
            self.assertEqual(testaudit.changed_test_files(self.work, since="HEAD"), [
                "tests/test_old.py", "tests/test_staged.py", "web/cart.test.ts"])
        finally:
            testaudit._testx = real

    def test_since_a_ref(self):
        self.assertEqual(testaudit.changed_test_files(self.work, since=self.base), [
            "tests/test_committed.py", "tests/test_old.py", "tests/test_staged.py", "web/cart.test.ts"])
        self.assertEqual(testaudit.changed_test_files(self.work, since="HEAD"), [
            "tests/test_old.py", "tests/test_staged.py", "web/cart.test.ts"])


# --------------------------------------------------------------------------- the CLI

class CliTests(AuditCase):
    def run_cli(self, *argv):
        p = argparse.ArgumentParser(prog="dev test audit")
        testaudit.add_arguments(p)
        a = p.parse_args(list(argv))
        out = io.StringIO()
        cwd = os.getcwd()
        os.chdir(self.top)
        try:
            with contextlib.redirect_stdout(out):
                rc = testaudit.cmd(a)
        finally:
            os.chdir(cwd)
        return rc, out.getvalue()

    def setUp(self):
        super().setUp()
        self.write("tests/test_cart.py", """
            import time
            from shop.cart import Cart

            def test_cart_adds():
                Cart().add(1)

            def test_cart_waits():
                time.sleep(1)
                assert Cart().total() == 0

            def test_it():
                assert Cart().total() == 0
        """)
        self.write("tests/test_ok.py", """
            from shop.cart import Cart

            def test_total_starts_at_zero():
                assert Cart().total() == 0
        """)

    def test_report_groups_by_file_and_exits_1_on_high(self):
        rc, out = self.run_cli("tests")
        self.assertEqual(rc, 1, out)
        self.assertIn("tests/test_cart.py", out)
        self.assertIn("no-assertion", out)
        self.assertIn("sleep-in-test", out)
        self.assertNotIn("vague-name", out)  # low is below the default --min-severity medium
        self.assertIn("1 high", out)
        self.assertIn("1 medium", out)

    def test_json_rule_filter_and_min_severity(self):
        rc, out = self.run_cli("tests", "--json", "--min-severity", "low")
        data = json.loads(out)
        self.assertEqual(sorted(f["rule"] for f in data["findings"]), ["no-assertion", "sleep-in-test", "vague-name"])
        self.assertEqual(data["counts"], {"high": 1, "medium": 1, "low": 1})
        self.assertEqual(rc, 1)
        rc, out = self.run_cli("tests", "--json", "--rule", "sleep-in-test")
        self.assertEqual([f["rule"] for f in json.loads(out)["findings"]], ["sleep-in-test"])
        self.assertEqual(rc, 0)

    def test_clean_files_exit_0(self):
        rc, out = self.run_cli("tests/test_ok.py")
        self.assertEqual(rc, 0, out)


# --------------------------------------------------------------------------- speed

class SpeedTests(AuditCase):
    def test_two_hundred_files_audit_quickly(self):
        py = PY_GOOD_PYTEST + "".join(f"\n\ndef test_case_{i}_reads_digits():\n    assert parse_qty('{i}') == {i}\n"
                                      for i in range(30))
        for i in range(100):
            self.write(f"tests/test_cart_{i}.py", py)
        for i in range(50):
            self.write(f"web/user_{i}.test.ts", JS_GOOD * 3)
        for i in range(20):
            self.write(f"src/test/java/com/acme/billing/Invoice{i}Test.java", JAVA_GOOD)
        for i in range(20):
            self.write(f"calc/calc_{i}_test.go", GO_GOOD)
        for i in range(10):
            self.write(f"tests/Unit/Invoice{i}Test.php", PHP_GOOD)
        start = time.perf_counter()
        findings = testaudit.audit(["tests", "web", "src", "calc"], top=self.top)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 3.0, f"{elapsed:.2f}s for 200 files")
        self.assertEqual(findings, [], _show(findings[:5]))


if __name__ == "__main__":
    unittest.main()
