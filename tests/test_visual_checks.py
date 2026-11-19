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
import hashlib
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
          "broken-image": "content", "offscreen": "responsive", "focus-visible": "accessibility",
          "near-miss-alignment": "layout", "off-scale-spacing": "spacing"}
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
      if (o.settle) await page.waitForFunction(() => document.documentElement.dataset.settled === "yes");
    }
    await page.evaluate(async () => {
      await document.fonts.ready;
      await new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)));
    });
    // the state a capture may find: a field the steps focused, and the page scrolled
    if (o.focus) await page.focus(o.focus);
    if (o.scroll) await page.evaluate(y => scrollTo(0, y), o.scroll);
    const state = () => page.evaluate(() => ({x: scrollX, y: scrollY, html: document.documentElement.outerHTML,
                                              focus: document.activeElement && document.activeElement.outerHTML,
                                              ring: document.activeElement && document.activeElement.matches(":focus-visible"),
                                              outline: document.activeElement && getComputedStyle(document.activeElement).outlineStyle}));
    const before = await state();
    // runChecks takes two arguments, so its source goes in an expression, as the capture driver's CDP call sends it
    const signals = await page.evaluate(`(${runChecks})(${JSON.stringify(o.width)}, ${JSON.stringify(o.options)})`);
    const after = await state();
    const where = await page.evaluate(signals => signals.map(s => {
      const all = document.querySelectorAll(s.anchor.selector), el = all[0];
      const plant = el && el.closest("[data-expect]"), r = el && el.getBoundingClientRect();
      const other = s.other && document.querySelectorAll(s.other.selector);
      return {matches: all.length, plant: plant ? plant.id : null, id: el ? el.id : null,
              box: r ? [r.left + scrollX, r.top + scrollY, r.width, r.height].map(Math.round) : null,
              text: el ? el.innerText.replace(/\s+/g, " ").trim() : null,
              other: other ? {matches: other.length, id: other.length ? other[0].id : null,
                              text: other.length ? other[0].innerText.replace(/\s+/g, " ").trim() : null} : null};
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
      if (!q(".tag")) return {};  // a page of its own, not defects.html
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


def drive(width, height, theme="light", csp=None, shot=None, page=None, options=None, focus=None, scroll=None):
    # -> (result, None) or (None, why the driver failed); Playwright's profile goes to a 0700 folder of its own.
    # `page` is the HTML of a page of the test's own, loaded instead of defects.html; `options` is runChecks's second
    # argument; `focus` a selector the page focuses and `scroll` the y it scrolls to before the checks run
    tmp = tempfile.mkdtemp(prefix="visual-checks-")
    try:
        url = FIXTURE.as_uri()
        if page is not None:
            (Path(tmp) / "page.html").write_text(page)
            url = (Path(tmp) / "page.html").as_uri()
        opts = {"core": PLAYWRIGHT["core"], "checks": str(CHECKS_JS), "url": url, "file": str(FIXTURE),
                "width": width, "height": height, "theme": theme, "csp": csp, "shot": shot, "settle": page is None,
                "options": options, "focus": focus, "scroll": scroll}
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
    PAGE = OPTIONS = FOCUS = SCROLL = None

    @classmethod
    def setUpClass(cls):
        cls.result, cls.error = drive(cls.WIDTH, cls.HEIGHT, cls.THEME, cls.CSP, page=cls.PAGE, options=cls.OPTIONS,
                                      focus=cls.FOCUS, scroll=cls.SCROLL)

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
        self.assertEqual(advice, {"plant-tap-target": True, "plant-target-size": True,
                                  "plant-target-size-input": True, "plant-min-text-size-body": True,
                                  "plant-min-text-size": False})

    def test_the_checks_leave_the_captured_state_as_it_was(self):
        r = self.run_page()
        self.assertEqual(r["before"], r["after"])

    def test_a_near_miss_is_anchored_on_the_card_that_is_off_and_names_the_one_it_almost_meets(self):
        [(s, w)] = self.signals("near-miss-alignment")
        self.assertEqual((w["text"], w["other"]["text"], w["other"]["matches"]), ("Paid invoices", "Open invoices", 1))
        self.assertEqual((s["measured"], s["rule"]), ("left edge 2 px off", ["ds:near-miss-alignment"]))

    def test_a_focus_that_shows_nothing_breaks_wcag_2_4_7(self):
        [(s, w)] = self.signals("focus-visible")
        self.assertEqual((w["text"], s["rule"]), ("Archive", ["wcag:2.4.7"]))


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class DefectsWithAFocusedFieldScrolledDown(_Page):
    # a capture after steps: a field has the focus and the page is scrolled; the focus check moves the focus through
    # the first 30 stops, and must give it back, with its ring, and leave the scroll where it was
    FOCUS, SCROLL = "#city", 1200

    def test_the_focus_and_the_scroll_come_back_as_they_were(self):
        r = self.run_page()
        self.assertIn('id="city"', r["before"]["focus"])
        self.assertEqual(r["before"]["y"], 1200)
        self.assertEqual(r["before"], r["after"])
        self.assertEqual([w["text"] for _, w in self.signals("focus-visible")], ["Archive"])


# A page of its own for focus visibility, near-miss alignment, and off-scale spacing (R31), at 1440 px so the phone
# advice stays quiet. Focus: rings drawn with a shadow, a border, on the wrapper (focus-within, for each of its two
# fields), on a pseudo-element, and after a transition all count; a button with none fails, and so does #first, which
# is first in tab order (tabindex 1) though last in the page. Of a radio group only the first button is a stop (the
# others show no ring and must not be reached), and the hidden, disabled, and tabindex=-1 buttons are no stops at all,
# so #late is the 30th stop and fails, while #later, the 31st, is left alone. One stop scrolls the page to the top
# when it gets the focus, as some pages do. Alignment: each .nm group is two or three sibling blocks; only the planted
# ones are off by 1-3 px at the start edge or, both being full width, at the end edge: centered blocks, a ragged end,
# inline blocks, and an absolutely placed block are not. Spacing: every element is reset to 0, so only the values set
# here count; with the scale 2, 4, 8, 16 px, the 10 px padding and the 12 px gap are off it.
FILLERS = "\n".join(f'<button type="button">Filler {n}</button>' for n in range(1, 20))
FOCUS_AND_LAYOUT_PAGE = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Focus and layout</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font: 16px/1.5 sans-serif; padding: 16px; min-block-size: 3000px; }}
  section {{ margin-block-end: 16px; }}
  button, input {{ font: inherit; padding: 8px; border: 1px solid #555; background: #fff; }}
  .shadow:focus {{ outline: none; box-shadow: 0 0 0 3px #1f4fb8; }}
  .border:focus {{ outline: none; border-color: #1f4fb8; }}
  .wrap:focus-within {{ box-shadow: 0 0 0 3px #1f4fb8; }}
  .wrap input:focus {{ outline: none; }}
  .pseudo {{ position: relative; }}
  .pseudo:focus {{ outline: none; }}
  .pseudo::after {{ content: ""; position: absolute; inset: -4px; border: 2px solid transparent; }}
  .pseudo:focus::after {{ border-color: #1f4fb8; }}
  .slow {{ box-shadow: 0 0 0 0 #1f4fb8; transition: box-shadow 0.4s linear; }}
  .slow:focus {{ outline: none; box-shadow: 0 0 0 3px #1f4fb8; }}
  .bare:focus, #first:focus, #late:focus, #later:focus, .not-a-stop:focus {{ outline: none; }}
  .nm > div {{ block-size: 24px; background: #ddd; margin-block-end: 8px; }}
  .start-2 {{ margin-inline-start: 2px; }}
  .start-4 {{ margin-inline-start: 4px; }}
  .end-2 {{ margin-inline-end: 2px; }}
  .center > div {{ margin-inline: auto; }}
  .w101 {{ inline-size: 101px; }}
  .w104 {{ inline-size: 104px; }}
  .w103 {{ inline-size: 103px; }}
  .nm > span {{ display: inline-block; inline-size: 40px; block-size: 24px; background: #ddd; }}
  .abs {{ position: relative; block-size: 64px; }}
  .abs > div {{ position: absolute; inset-inline-start: 2px; inset-block-start: 32px; inline-size: 40px; }}
  .ok {{ padding: 8px 16px; }}
  .off {{ padding-inline: 10px; }}
  .gap {{ display: flex; gap: 12px; }}
  .em {{ padding: 0.5em; }}
  .pct {{ margin-inline-start: 10%; }}
  .auto {{ inline-size: 200px; margin-inline: auto; }}
  .pull {{ margin-block-start: -8px; }}
</style></head>
<body>
<section>
  <button type="button" class="shadow">Shadow</button>
  <button type="button" class="border">Border</button>
  <span class="wrap"><input aria-label="Wrapped"> <input aria-label="Wrapped too"></span>
  <button type="button" class="pseudo">Pseudo</button>
  <button type="button" class="slow">Slow</button>
  <button type="button" class="bare" id="bare" data-expect="focus-visible">Bare</button>
  <button type="button" class="not-a-stop" hidden>Hidden</button>
  <button type="button" class="not-a-stop" disabled>Disabled</button>
  <button type="button" class="not-a-stop" tabindex="-1">Out of order</button>
  <input type="radio" name="size" aria-label="Small"><input type="radio" name="size" class="not-a-stop"
    aria-label="Medium"><input type="radio" name="size" class="not-a-stop" aria-label="Large">
  {FILLERS}
  <button type="button" onfocus="window.scrollTo(0, 0)">Back to the top</button>
  <button type="button" id="late" data-expect="focus-visible">Late</button>
  <button type="button" id="later">Later</button>
</section>
<section>
  <div class="nm" id="nm-start" data-expect="near-miss-alignment"><div></div><div class="start-2"></div></div>
  <div class="nm"><div></div><div class="start-4"></div></div>
  <div class="nm" id="nm-odd" data-expect="near-miss-alignment"><div class="start-2"></div><div></div><div></div></div>
  <div class="nm" id="nm-end" data-expect="near-miss-alignment"><div></div><div class="end-2"></div></div>
  <div class="nm center"><div class="w101"></div><div class="w104"></div></div>
  <div class="nm"><div class="w101"></div><div class="w103"></div></div>
  <div class="nm"><span></span> <span class="start-2"></span></div>
  <div class="nm abs"><div></div><div style="position: static"></div></div>
</section>
<section>
  <div class="ok">On the scale</div>
  <div class="off" id="off" data-expect="off-scale-spacing">Ten pixels</div>
  <div class="gap" id="gap" data-expect="off-scale-spacing"><span>A</span><span>B</span></div>
  <div class="em">Half an em</div>
  <div class="pct">Ten percent</div>
  <div class="auto">Centered</div>
  <div class="pull">Pulled up</div>
</section>
<button type="button" id="first" tabindex="1" data-expect="focus-visible">First</button>
</body></html>
"""


class _FocusAndLayout(_Page):
    WIDTH, HEIGHT, PAGE = 1440, 900, FOCUS_AND_LAYOUT_PAGE

    def assert_planted(self, check):
        got = Counter(w["plant"] for _, w in self.signals(check))
        self.assertEqual(got, Counter(self.planted(check)), f"{check}: planted elements and their signals")


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class FocusAndLayoutWithoutAScale(_FocusAndLayout):

    def test_only_a_focus_that_shows_nothing_in_the_first_30_stops_fails(self):
        self.assert_planted("focus-visible")

    def test_only_the_blocks_off_by_1_to_3_px_are_near_misses(self):
        self.assert_planted("near-miss-alignment")

    def test_the_odd_block_out_is_the_one_anchored(self):
        # two of the three blocks share their start edge, so the third is the one that is off
        [(s, _)] = [(s, w) for s, w in self.signals("near-miss-alignment") if w["plant"] == "nm-odd"]
        self.assertEqual(s["anchor"]["selector"], "#nm-odd > div:nth-of-type(1)")

    def test_an_end_edge_near_miss_says_so(self):
        [(s, _)] = [(s, w) for s, w in self.signals("near-miss-alignment") if w["plant"] == "nm-end"]
        self.assertEqual(s["measured"], "right edge 2 px off")

    def test_without_a_scale_spacing_is_not_checked(self):
        self.assertEqual(self.signals("off-scale-spacing"), [])

    def test_the_focus_comes_back_to_the_page_as_it_was(self):
        r = self.run_page()
        self.assertEqual(r["before"], r["after"])


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class FocusAndLayoutScrolledDown(_FocusAndLayout):
    SCROLL = 600

    def test_a_stop_that_scrolls_the_page_on_focus_leaves_it_where_it_was(self):
        r = self.run_page()
        self.assertEqual((r["before"]["y"], r["after"]["y"]), (600, 600))


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class SpacingOnADeclaredScale(_FocusAndLayout):
    OPTIONS = {"spacing": [2, 4, 8, 16]}

    def test_only_margins_paddings_and_gaps_off_the_scale_fire(self):
        # 0.5em is 8 px; a percentage, an auto margin, and a pull of -8 px are no step off the scale
        self.assert_planted("off-scale-spacing")

    def test_the_signal_names_each_value_off_the_scale_and_the_scale(self):
        got = {w["plant"]: (s["measured"], s["threshold"]) for s, w in self.signals("off-scale-spacing")}
        self.assertEqual(got, {"off": ("padding-right 10 px, padding-left 10 px", "on the scale: 2, 4, 8, 16 px"),
                               "gap": ("row-gap 12 px, column-gap 12 px", "on the scale: 2, 4, 8, 16 px")})


# The scale of a page without DESIGN.md: its five most used values. After the reset, the values set here are 8 px
# (10 uses), 16 px (8), 24 px (4), 4 px (2), and 32 px (2); 10 px is used once, first in the page, and is off the
# scale.
SCALE_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Scale</title>
<style>
  * { margin: 0; padding: 0; }
  p { margin-block-end: 8px; }
  .card { padding: 16px; }
  .band { padding-block: 24px; }
  .grid { display: grid; gap: 32px; }
  .nudge { margin-inline-start: 4px; }
  .odd { padding-inline-start: 10px; }
</style></head>
<body>
<div class="odd" id="odd" data-expect="off-scale-spacing">G</div>
<p>1</p><p>2</p><p>3</p><p>4</p><p>5</p><p>6</p><p>7</p><p>8</p><p>9</p><p>10</p>
<div class="card">A</div><div class="card">B</div>
<div class="band">C</div><div class="band">D</div>
<div class="grid"><span class="nudge">E</span><span class="nudge">F</span></div>
</body></html>
"""


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class SpacingOnThePagesScale(_Page):
    WIDTH, HEIGHT, PAGE, OPTIONS = 1440, 900, SCALE_PAGE, {"spacing": "page"}

    def test_the_scale_is_the_five_most_used_values(self):
        got = [(w["plant"], s["measured"], s["threshold"]) for s, w in self.signals("off-scale-spacing")]
        self.assertEqual(got, [("odd", "padding-left 10 px", "on the scale: 4, 8, 16, 24, 32 px")])


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class DefectsAtDesktopWidth(_Page):
    WIDTH, HEIGHT, THEME = 1440, 900, "dark"

    def test_phone_advice_stays_quiet_at_desktop_width(self):
        got = sorted((s["check"], w["plant"]) for s, w in self.signals() if
                     s["check"] in ("tap-target", "min-text-size"))
        self.assertEqual(got, [("min-text-size", "plant-min-text-size")])

    def test_focus_and_alignment_fire_only_on_their_plants_in_the_dark_at_desktop_width(self):
        got = sorted((s["check"], w["plant"]) for s, w in self.signals() if
                     s["check"] in ("focus-visible", "near-miss-alignment"))
        self.assertEqual(got, [("focus-visible", "plant-focus-visible"), ("near-miss-alignment", "plant-near-miss")])


# A page of its own for what no check may look at, at phone width. Visually hidden text (the screen-reader-only
# patterns: clip-path inset(50%) on a 1 px box, as devkit's own report writes it; clip rect(0 0 0 0); a full-size box
# clipped away; a 1×1 box that clips its overflow) sits 60 px in and 8 px down on a visible label, so its text box
# crosses the label's without either holding the other; one visible tag placed the same way is the overlap that must
# still fire. Each row clips what runs past the page's edge. Masked elements (data-visual-mask, an
# element inside one, a password field, a card number field, and an element the request masks with its own
# selector, .account) are truncated, tiny, or overlapping; none may give a signal, and the account text may appear
# in none. One plain truncated paragraph and one small field are the controls that still fire.
SECRET = "Balance 98,765.43 on account DE89 3704 0044 0532 0130 00"
HIDDEN_AND_MASKED_PAGE = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Hidden and masked</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font: 16px/1.5 sans-serif; padding: 16px; }}
  .row {{ position: relative; block-size: 48px; margin-block-end: 16px; overflow: hidden; }}
  .row > .over {{ position: absolute; inset-inline-start: 60px; inset-block-start: 8px; white-space: nowrap; }}
  .sr {{ position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }}
  .sr-only {{ position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden;
             clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }}
  .gone {{ clip-path: inset(50%); }}
  .tiny {{ position: absolute; width: 1px; height: 1px; overflow: hidden; white-space: nowrap; }}
  .cut {{ inline-size: 120px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-block-end: 16px; }}
  input {{ font: inherit; display: block; inline-size: 200px; block-size: 44px; margin-block-end: 16px; }}
  input.small {{ block-size: 20px; }}
</style></head>
<body>
<div class="row"><span>Visible label one</span><span class="sr over">Hidden words that run under the label</span></div>
<div class="row"><span>Visible label two</span><span class="sr-only over">Hidden words that run under it</span></div>
<div class="row"><span>Visible label three</span><span class="over"><span class="gone">Clipped away words here</span></span></div>
<div class="row"><span>Visible label four</span><span class="tiny over">Words in a box of one pixel</span></div>
<div class="row" id="plant-overlap" data-expect="overlap"><span>Visible label five</span><span class="over">Tag</span></div>
<p class="cut" data-visual-mask>{SECRET}</p>
<div data-visual-mask><p class="cut">{SECRET}</p></div>
<p class="cut account">{SECRET}</p>
<div class="row" data-visual-mask><span>Masked label</span><span class="over">{SECRET}</span></div>
<input type="password" class="small" aria-label="Password">
<input autocomplete="cc-number" class="small" aria-label="Card number">
<p class="cut" id="plant-truncation" data-expect="truncation">A plain paragraph that is far too long for its box</p>
<input class="small" id="plant-tap-target" data-expect="tap-target" aria-label="City">
</body></html>
"""


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class HiddenAndMasked(_Page):
    PAGE, OPTIONS = HIDDEN_AND_MASKED_PAGE, {"mask": [".account"]}

    def test_visually_hidden_text_overlaps_nothing_and_a_visible_tag_still_does(self):
        self.assertEqual([w["plant"] for _, w in self.signals("overlap")], ["plant-overlap"])

    def test_a_masked_element_and_anything_inside_it_give_no_signal(self):
        stray = [(s["check"], s["anchor"]["selector"]) for s, w in self.signals() if not w["plant"]]
        self.assertEqual(stray, [], "signals on masked or hidden elements")
        self.assertEqual(sorted(w["plant"] for _, w in self.signals()),
                         ["plant-overlap", "plant-tap-target", "plant-truncation"])

    def test_no_signal_carries_a_masked_elements_text(self):
        self.assertNotIn("98,765.43", json.dumps(self.run_page()["signals"]))

    def test_the_checks_mask_what_the_capture_driver_masks(self):
        # runChecks's own copy of the mask rules, since its source is sent alone, against capture.cjs's list
        rules = lambda path, name: re.search(name + r"\s*=\s*\[(.*?)\];", path.read_text(), re.S).group(1)
        quoted = lambda text: re.findall(r"'([^']*)'", text)  # both lists are single-quoted selectors
        self.assertEqual(quoted(rules(CHECKS_JS, "MASK_RULES")),
                         quoted(rules(ROOT / "lib" / "devkit" / "visual" / "capture.cjs", "MASK_RULES")))


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class DefectsUnderAStrictPolicy(_Page):
    CSP = "script-src 'none'"

    def test_the_checks_run_where_the_page_may_run_no_script(self):
        r = self.run_page()
        self.assertNotIn("data-settled", r["after"]["html"].split(">", 1)[0], "the policy blocks the page's script")
        self.assertEqual(sorted(w["plant"] for _, w in self.signals("overlap")),
                         ["plant-overlap", "plant-overlap-mixed", "plant-overlap-text"])


# a label in a larger type and, beside it in the same line, smaller inline code that wraps onto the next line: the
# code's two line boxes leave the label alone, though the box around both lines crosses the label's top. A chip laid
# over the lower half of the code's second line is the overlap that must still fire (the class is not .tag, which
# names defects.html's look-alikes to the driver).
WRAPPED_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Wrapped inline code</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font: 16px/1.5 sans-serif; padding: 16px; }
  .row { position: relative; inline-size: 300px; margin-block-end: 32px; }
  label { font-size: 20px; }
  code { font: 14px/1.5 monospace; }
  .chip { position: absolute; inset-inline-start: 40px; inset-block-start: 42px; font-size: 12px; background: #ffffff; }
</style></head>
<body>
<div class="row"><label for="cmd">Command</label> <code id="cmd">dev visual capture http://127.0.0.1:4200/</code></div>
<div class="row" id="plant-overlap" data-expect="overlap"><label>Run</label>
<code>dev visual capture http://127.0.0.1:4200/</code><span class="chip">New</span></div>
</body></html>
"""


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class WrappedInlineText(_Page):
    PAGE = WRAPPED_PAGE

    def test_a_wrapped_line_crosses_only_what_lies_on_its_own_line(self):
        r = self.run_page()
        overlaps = [(w["plant"], w["text"], w["other"]["text"]) for s, w in self.signals("overlap")]
        code = "dev visual capture http://127.0.0.1:4200/"
        self.assertEqual(overlaps, [("plant-overlap", code, "New")], r["signals"])


# an image that does not load, with an id and a test id that each name it alone
TEST_ID_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Test ids</title></head>
<body><main><img id="hero" data-testid="hero-picture" src="missing.png" alt="The hero" width="80" height="60"></main>
</body></html>
"""


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class SelectorOrder(_Page):
    PAGE = TEST_ID_PAGE

    def test_an_anchor_names_its_element_by_its_test_id_before_its_id(self):
        # devkit's order, as capture.cjs writes selectors: the test id, the id, then a path
        [(s, w)] = self.signals("broken-image")
        self.assertEqual((s["anchor"]["selector"], w["matches"], w["id"]), ('[data-testid="hero-picture"]', 1, "hero"))


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class FocusWalkLeftOut(_FocusAndLayout):
    # the capture driver runs the checks without the focus walk first, and the walk alone after axe and the detector
    OPTIONS = {"focus": False}

    def test_without_the_walk_no_focus_is_measured_and_the_other_checks_run(self):
        self.assertEqual(self.signals("focus-visible"), [])
        self.assert_planted("near-miss-alignment")


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class FocusWalkAlone(_FocusAndLayout):
    OPTIONS, SCROLL = {"focus": "only"}, 600

    def test_the_walk_alone_measures_only_the_focus_and_leaves_the_page_as_it_was(self):
        self.assertEqual({s["check"] for s, _ in self.signals()}, {"focus-visible"})
        self.assert_planted("focus-visible")
        r = self.run_page()
        self.assertEqual(r["before"], r["after"])


CAPTURE_CJS = ROOT / "lib" / "devkit" / "visual" / "capture.cjs"
AXE_SHA256 = "20c09fe157a8a34a30e241aaa1fcdade657734f08ab379ecfbeb7d45cc46e878"  # axe.min.js of axe-core 4.14.0


def _capture_ready():
    # -> why a capture cannot run here, or None: the capture needs gitleaks, and these tests the pinned axe-core
    if not PLAYWRIGHT:
        return NO_PLAYWRIGHT
    if not shutil.which("gitleaks"):
        return "gitleaks is not installed (the Brewfile pins 8.30.1), and the capture cannot run without it"
    try:
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=60).stdout.strip()
        data = (Path(root) / "axe-core" / "axe.min.js").read_bytes()
    except (OSError, subprocess.TimeoutExpired):
        return "axe-core is not installed in the global npm folder (dev install --only tools)"
    if hashlib.sha256(data).hexdigest() != AXE_SHA256:
        return "the global axe-core is not the pinned 4.14.0"
    return None


NO_CAPTURE = _capture_ready()

# a menu that opens when its button gets the focus: the page changes its title and adds an image without a text
# alternative, which axe reports; the second button shows no ring, which the focus walk reports
OPENS_ON_FOCUS_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>At rest</title>
<style>
  body { font: 16px/1.5 Arial, sans-serif; margin: 24px; }
  button { font: inherit; padding: 8px 16px; }
  #menu:focus-visible { outline: 3px solid #000; outline-offset: 2px; }
  #bare:focus { outline: none; }
</style></head>
<body><main><h1>Invoices</h1>
<p><button id="menu" type="button">Open the menu</button> <button id="bare" type="button">Archive</button></p>
</main>
<script>
  document.getElementById("menu").addEventListener("focus", () => {
    document.title = "Walked";
    if (document.getElementById("late")) return;
    const img = document.createElement("img");
    img.id = "late";
    img.width = 24;
    img.height = 24;
    img.src = "data:image/svg+xml," + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="24" ' +
      'height="24"><rect width="24" height="24" fill="#c00"/></svg>');
    document.querySelector("main").append(img);
  });
</script></body></html>
"""

# stands in for `dev impeccable detect --cdp <port> …`: it writes the titles of the pages the port lists into
# ORDER_LOG, which is the page state the detector reads, and finds nothing
TITLES_DETECTOR = r"""
const http = require('http');
const fs = require('fs');
const args = process.argv.slice(2);
const port = Number(args[args.indexOf('--cdp') + 1]);
http.get(`http://127.0.0.1:${port}/json/list`, (res) => {
  let body = '';
  res.on('data', (chunk) => { body += chunk; });
  res.on('end', () => {
    const titles = JSON.parse(body).filter((t) => t.type === 'page').map((t) => t.title);
    fs.writeFileSync(process.env.ORDER_LOG, JSON.stringify(titles));
    process.stdout.write('[]');
  });
}).on('error', (e) => { process.stderr.write(String(e)); process.exit(1); });
"""


@unittest.skipIf(NO_CAPTURE, NO_CAPTURE)
class TheFocusWalkRunsLast(unittest.TestCase):
    # one capture of a page that changes when a control gets the focus, with the checks and a stand-in detector
    @classmethod
    def setUpClass(cls):
        top = Path(tempfile.mkdtemp(prefix="visual-checks-order-"))
        cls.top = top
        tmp, store = top / "tmp", top / "store"
        tmp.mkdir(mode=0o700)
        page, detector, cls.log = top / "page.html", top / "detector.cjs", top / "titles.json"
        page.write_text(OPENS_ON_FOCUS_PAGE)
        detector.write_text(TITLES_DETECTOR)
        recipe = {"viewport": [390, 844], "dpr": 1, "theme": "light", "locale": "en-US", "timezone": "UTC",
                  "clock": None, "full_page": True, "steps": [], "storage_state": None,
                  "emulation": {"forced_colors": False, "contrast": None, "vision": None}, "css": None}
        request = {"id": "VC-test", "source": {"url": page.as_uri()}, "recipe": recipe, "state": "planted",
                   "element": None, "mask": [], "remote": False, "check": True, "store": str(store),
                   "impeccable": {"command": [shutil.which("node"), str(detector)], "version": "test-detector"},
                   "core": PLAYWRIGHT["core"]}
        env = dict(os.environ, TMPDIR=str(tmp), PLAYWRIGHT_BROWSERS_PATH=PLAYWRIGHT["browsers"],
                   ORDER_LOG=str(cls.log))
        p = subprocess.run([PLAYWRIGHT["node"], str(CAPTURE_CJS), "--json"], input=json.dumps(request),
                           capture_output=True, text=True, timeout=180, env=env, cwd=str(top))
        cls.record, cls.error = (json.loads(p.stdout), None) if p.returncode == 0 else (None, p.stderr[-2000:])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.top, ignore_errors=True)

    def checks(self):
        if self.error:
            self.fail(f"the capture failed: {self.error}")
        return self.record["checks"]

    def test_axe_reads_the_page_before_the_walk_moves_the_focus(self):
        # the walk ran (it reports the button that shows no ring), and the image its focus added came after axe
        signals = self.checks()["signals"]
        self.assertIn("axe", self.checks()["ran"])
        self.assertEqual([s["anchor"]["selector"] for s in signals if s["check"] == "focus-visible"], ["#bare"])
        self.assertEqual([s for s in signals if s["anchor"]["selector"] == "#late"], [])

    def test_the_detector_reads_the_page_before_the_walk_moves_the_focus(self):
        self.assertIn("impeccable", self.checks()["ran"])
        self.assertEqual(json.loads(self.log.read_text()), ["At rest"])


if __name__ == "__main__":
    unittest.main()
