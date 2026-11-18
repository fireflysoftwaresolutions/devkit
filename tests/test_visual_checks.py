# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# lib/devkit/visual/checks.js is devkit's in-page check script: the capture driver runs its runChecks with
# page.evaluate on the page state it captured, and gets one signal per measured defect. The page
# tests/fixtures/visual/defects.html plants the defects: each planted one is an element with data-expect, the checks
# it must fire, and no signal may come from anywhere else. The page runs in the Chromium of the pinned Playwright
# (config/npm-globals.txt), launched and set up the way the capture driver does it; without node, the pinned
# @playwright/cli, or its Chromium, the tests skip and say which is missing. Tests carry comments, not docstrings:
# `dev test prove` leaves out tests with a docstring. Run: python3 -m unittest tests.test_visual_checks
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKS_JS = ROOT / "lib" / "devkit" / "visual" / "checks.js"
FIXTURE = ROOT / "tests" / "fixtures" / "visual" / "defects.html"
# the checks checks.js runs, with the pass each one reports to; data-expect may also name other sources' signals
PASSES = {"overlap": "layout", "page-overflow": "responsive", "element-overflow": "layout", "truncation": "content",
          "tap-target": "accessibility", "min-text-size": "typography", "layout-shift": "states",
          "broken-image": "content", "offscreen": "responsive"}
RULE = re.compile(r"(wcag:\d\.\d+\.\d+|nielsen:(10|[1-9])|platform:[a-z0-9-]+|ds:[a-z0-9-]+)")


def _account_home() -> Path:
    # the run's HOME is a temporary one (tests/_hermetic.py); Playwright's browsers live in the account's own home
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError):
        return Path.home()


def pinned_playwright():
    # -> ({node, core, browsers}, None), or (None, why the browser tests cannot run here)
    node = shutil.which("node")
    if not node:
        return None, "node is not on PATH"
    pins = (ROOT / "config" / "npm-globals.txt").read_text().split()
    version = next(p for p in pins if p.startswith("@playwright/cli@")).rpartition("@")[2]
    try:
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=60).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        root = ""
    cli = Path(root or "/nonexistent") / "@playwright" / "cli"
    try:
        installed = json.loads((cli / "package.json").read_text())["version"]
    except (OSError, ValueError, KeyError):
        return None, f"the pinned @playwright/cli {version} is not installed (dev setup installs it)"
    if installed != version:
        return None, f"@playwright/cli {installed} is installed, not the pinned {version}"
    core = cli / "node_modules" / "playwright-core"
    try:
        browsers = json.loads((core / "browsers.json").read_text())["browsers"]
        revision = next(b["revision"] for b in browsers if b["name"] == "chromium")
    except (OSError, ValueError, KeyError, StopIteration):
        return None, f"the pinned @playwright/cli {version} has no playwright-core with a Chromium revision"
    cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(
        _account_home() / ("Library/Caches" if sys.platform == "darwin" else ".cache") / "ms-playwright")
    if not (Path(cache) / f"chromium-{revision}").is_dir():
        return None, f"Chromium {revision}, the pinned Playwright's browser, is not installed in {cache}"
    return {"node": node, "core": str(core), "browsers": cache}, None


PLAYWRIGHT, NO_PLAYWRIGHT = pinned_playwright()

# Launches Chromium like the capture driver (headless, sandboxed, no crash reporter; one context with the recipe's
# viewport, color scheme, reduced motion, locale, time zone, and service workers blocked), loads the page, waits for
# its layout shift to be painted, then fonts and two frames, and runs checks.js on that state. It prints the signals,
# where each anchor's selector leads, the planted elements, and the page's state before and after the checks.
DRIVER = r"""
const o = JSON.parse(process.argv[1]);
const fs = require("fs");
const {chromium} = require(o.core);
const {runChecks} = require(o.checks);
(async () => {
  const browser = await chromium.launch({channel: "chromium", headless: true, chromiumSandbox: true,
                                         args: ["--disable-breakpad", "--disable-crash-reporter"]});
  try {
    const context = await browser.newContext({viewport: {width: o.width, height: o.height}, colorScheme: o.theme,
      reducedMotion: "reduce", locale: "en-US", timezoneId: "UTC", serviceWorkers: "block"});
    const page = await context.newPage();
    if (o.csp) {
      await page.route("http://fixture.test/**", route => route.request().url().endsWith("/defects.html")
        ? route.fulfill({contentType: "text/html", headers: {"content-security-policy": o.csp},
                         body: fs.readFileSync(o.file, "utf8")})
        : route.abort());
      await page.goto("http://fixture.test/defects.html");
    } else {
      await page.goto(o.url);
      await page.waitForFunction(() => document.documentElement.dataset.settled === "yes");
    }
    await page.evaluate(async () => {
      await document.fonts.ready;
      await new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)));
    });
    const state = () => page.evaluate(() => ({x: scrollX, y: scrollY, html: document.documentElement.outerHTML,
                                              focus: document.activeElement && document.activeElement.outerHTML}));
    const before = await state();
    const signals = await page.evaluate(runChecks, o.width);
    const after = await state();
    const where = await page.evaluate(signals => signals.map(s => {
      const all = document.querySelectorAll(s.anchor.selector), el = all[0];
      const plant = el && el.closest("[data-expect]"), r = el && el.getBoundingClientRect();
      return {matches: all.length, plant: plant ? plant.id : null,
              box: r ? [r.left + scrollX, r.top + scrollY, r.width, r.height].map(Math.round) : null,
              text: el ? el.innerText.replace(/\s+/g, " ").trim() : null};
    }), signals);
    const planted = await page.evaluate(() => Array.from(document.querySelectorAll("[data-expect]"),
      el => ({id: el.id, expect: el.dataset.expect.split(/\s+/)})));
    const page_width = await page.evaluate(() => document.documentElement.scrollWidth);
    // how far the look-alikes that overlap on purpose cross real text: [width, height] of the intersection
    const lookalikes = await page.evaluate(() => {
      const text = el => {
        const nodes = Array.from(el.childNodes).filter(n => n.nodeType === 3 && n.data.trim()), range = document.createRange();
        range.setStartBefore(nodes[0]);
        range.setEndAfter(nodes[nodes.length - 1]);
        return range.getBoundingClientRect();
      };
      const cross = (a, b) => [Math.min(a.right, b.right) - Math.max(a.left, b.left),
                               Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top)].map(Math.round);
      const q = s => document.querySelector(s);
      return {"the fixed tag over the heading": cross(q(".tag").getBoundingClientRect(), text(q("h1"))),
              "the aria-hidden mark over the quotation": cross(text(q(".mark")), text(q("blockquote p"))),
              "the floating label inside its field": cross(text(q(".float label")), q(".float input").getBoundingClientRect()),
              "the badge on its button": cross(text(q(".badge")), q(".inbox").getBoundingClientRect())};
    });
    if (o.shot) await page.screenshot({path: o.shot, fullPage: true, animations: "disabled", caret: "hide"});
    process.stdout.write(JSON.stringify({signals, where, planted, before, after, page_width, lookalikes}));
  } finally {
    await browser.close();
  }
})().catch(e => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
"""


def drive(width, height, theme="light", csp=None, shot=None):
    # -> (result, None) or (None, why the driver failed); Playwright's profile goes to a 0700 folder of its own
    tmp = tempfile.mkdtemp(prefix="visual-checks-")
    try:
        opts = {"core": PLAYWRIGHT["core"], "checks": str(CHECKS_JS), "url": FIXTURE.as_uri(), "file": str(FIXTURE),
                "width": width, "height": height, "theme": theme, "csp": csp, "shot": shot}
        env = dict(os.environ, TMPDIR=tmp, PLAYWRIGHT_BROWSERS_PATH=PLAYWRIGHT["browsers"])
        try:
            out = subprocess.run([PLAYWRIGHT["node"], "-e", DRIVER, json.dumps(opts)], capture_output=True, text=True,
                                 timeout=120, env=env, cwd=tmp)
        except subprocess.TimeoutExpired:
            return None, "the browser did not finish in 120 s"
        if out.returncode != 0:
            return None, out.stderr.strip()[-2000:]
        return json.loads(out.stdout), None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class _Page(unittest.TestCase):
    WIDTH, HEIGHT, THEME, CSP = 390, 844, "light", None

    @classmethod
    def setUpClass(cls):
        cls.result, cls.error = drive(cls.WIDTH, cls.HEIGHT, cls.THEME, cls.CSP)

    def run_page(self):
        if self.error:
            self.fail(f"checks.js did not run on the page: {self.error}")
        return self.result

    def signals(self, check=None):
        # the signals, each with where its selector leads, of one check or of all
        r = self.run_page()
        return [(s, w) for s, w in zip(r["signals"], r["where"]) if check in (None, s["check"])]

    def planted(self, check):
        return [p["id"] for p in self.run_page()["planted"] if check in p["expect"]]


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class DefectsAtPhoneWidth(_Page):

    def test_each_planted_defect_gives_one_signal_of_its_check(self):
        for check in PASSES:
            with self.subTest(check=check):
                got = Counter(w["plant"] for _, w in self.signals(check) if w["plant"])
                self.assertEqual(got, Counter(self.planted(check)), f"{check}: planted elements and their signals")

    def test_no_signal_comes_from_outside_the_planted_defects(self):
        stray = [(s["check"], s["anchor"]["selector"]) for s, w in self.signals() if not w["plant"]]
        self.assertEqual(stray, [], "signals on elements that plant no defect")

    def test_the_look_alikes_cross_the_text_they_must_leave_alone(self):
        # without this, the silence on these look-alikes would prove nothing about the rules that exempt them
        for name, (w, h) in self.run_page()["lookalikes"].items():
            with self.subTest(name):
                self.assertTrue(w > 2 and h > 2, f"{name} crosses it by {w}×{h} px")

    def test_a_signal_carries_its_check_pass_rule_measure_and_anchor(self):
        for s, w in self.signals():
            with self.subTest(check=s["check"], selector=s["anchor"]["selector"]):
                self.assertEqual(s["pass"], PASSES[s["check"]])
                self.assertTrue(s["rule"] and all(RULE.fullmatch(r) for r in s["rule"]), s["rule"])
                self.assertTrue(s["measured"] and s["threshold"], (s["measured"], s["threshold"]))
                self.assertEqual(w["matches"], 1, "the selector matches the anchored element alone")
                region = s["anchor"]["region"]
                self.assertEqual(len(region), 4)
                self.assertTrue(all(abs(a - b) <= 1 for a, b in zip(region, w["box"])), (region, w["box"]))

    def test_a_signal_takes_no_name_from_the_page(self):
        # the anchor's name comes from the redacted description, never from a check's output
        for s, _ in self.signals():
            self.assertLessEqual(set(s["anchor"]), {"region", "selector"}, s["anchor"])
            self.assertNotIn("name", s)

    def test_an_overlap_with_a_control_breaks_target_spacing_and_text_on_text_does_not(self):
        # a control over text is anchored on the control, as target spacing is about the control
        rules = {w["plant"]: (s["rule"], s["anchor"]["selector"].rsplit(" > ", 1)[-1])
                 for s, w in self.signals("overlap")}
        self.assertEqual(rules, {"plant-overlap": (["wcag:2.5.8"], "button:nth-of-type(1)"),
                                 "plant-overlap-mixed": (["wcag:2.5.8"], "button"),
                                 "plant-overlap-text": (["nielsen:8"], "p:nth-of-type(1)")})

    def test_an_overlap_names_the_other_element(self):
        for s, w in self.signals("overlap"):
            other = s["other"]
            self.assertTrue(other["selector"] and len(other["region"]) == 4, other)
            self.assertNotEqual(other["selector"], s["anchor"]["selector"])

    def test_truncation_keeps_the_full_text_up_to_120_characters(self):
        texts = {w["plant"]: (s["text"], w["text"]) for s, w in self.signals("truncation")}
        cut, full = texts["plant-truncation"]
        self.assertGreater(len(full), 120)
        self.assertEqual(cut, full[:119] + "…")
        kept, full = texts["plant-truncation-clamp"]
        self.assertLessEqual(len(full), 120)
        self.assertEqual(kept, full)

    def test_page_overflow_measures_the_document_width(self):
        [(s, _)] = self.signals("page-overflow")
        self.assertEqual((s["measured"], s["threshold"]), (f"{self.run_page()['page_width']} px", "390 px"))

    def test_layout_shift_measures_the_cumulative_score(self):
        [(s, _)] = self.signals("layout-shift")
        self.assertGreater(float(s["measured"]), 0.1)
        self.assertEqual(s["threshold"], "0.1")

    def test_phone_size_rules_are_advice(self):
        advice = {w["plant"]: s.get("advice", False) for s, w in self.signals() if
                  s["check"] in ("tap-target", "min-text-size")}
        self.assertEqual(advice, {"plant-tap-target": True, "plant-min-text-size-body": True,
                                  "plant-min-text-size": False})

    def test_the_checks_leave_the_captured_state_as_it_was(self):
        r = self.run_page()
        self.assertEqual(r["before"], r["after"])


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class DefectsAtDesktopWidth(_Page):
    WIDTH, HEIGHT, THEME = 1440, 900, "dark"

    def test_phone_advice_stays_quiet_at_desktop_width(self):
        got = sorted((s["check"], w["plant"]) for s, w in self.signals() if
                     s["check"] in ("tap-target", "min-text-size"))
        self.assertEqual(got, [("min-text-size", "plant-min-text-size")])


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class DefectsUnderAStrictPolicy(_Page):
    CSP = "script-src 'none'"

    def test_the_checks_run_where_the_page_may_run_no_script(self):
        r = self.run_page()
        self.assertNotIn("data-settled", r["after"]["html"].split(">", 1)[0], "the policy blocks the page's script")
        self.assertEqual(sorted(w["plant"] for _, w in self.signals("overlap")),
                         ["plant-overlap", "plant-overlap-mixed", "plant-overlap-text"])


if __name__ == "__main__":
    unittest.main()
