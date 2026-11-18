# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# lib/devkit/visual/axe.cjs runs the pinned axe-core (4.14.0, config/npm-globals.txt) in each frame of the captured
# page, in the capture's isolated JavaScript worlds, and turns each violation into a screen signal; fold.js folds the
# signals of one capture that measure one defect on one element (R68, through lib/devkit/visual/equivalents.json).
# The browser tests run axe.cjs on tests/fixtures/visual/axe/planted.html at 390x844 in the Chromium of the pinned
# Playwright, with the isolated worlds built the way capture.cjs builds them, and read the signals it returns; each
# planted defect is an element with data-expect. Without node, gitleaks, the pinned @playwright/cli, its Chromium, or
# the pinned axe-core, those tests skip and say which is missing; the fold tests need node only.
# Tests carry comments, not docstrings: `dev test prove` leaves out tests with a docstring.
# Run: python3 -m unittest tests.test_visual_axe
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
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()

VISUAL = ROOT / "lib" / "devkit" / "visual"
AXE_CJS, FOLD_JS, CHECKS_JS = VISUAL / "axe.cjs", VISUAL / "fold.js", VISUAL / "checks.js"
EQUIVALENTS = VISUAL / "equivalents.json"
PAGE = ROOT / "tests" / "fixtures" / "visual" / "axe" / "planted.html"
NODE = shutil.which("node")
GITLEAKS = shutil.which("gitleaks")
AXE_SHA256 = "20c09fe157a8a34a30e241aaa1fcdade657734f08ab379ecfbeb7d45cc46e878"  # axe.min.js of axe-core 4.14.0
TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]


def _account_home() -> Path:
    # the run's HOME is a temporary one (tests/_hermetic.py); Playwright's browsers live in the account's own home
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError):
        return Path.home()


def _npm_root() -> str:
    try:
        return subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=60).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _pinned():
    # -> ({core, browsers, axe}, None), or (None, why the browser tests cannot run here)
    if not NODE:
        return None, "node is not on PATH"
    if not GITLEAKS:
        return None, "gitleaks is not installed (the Brewfile pins 8.30.1), and axe.cjs redacts its signals with it"
    pins = (ROOT / "config" / "npm-globals.txt").read_text().split()
    version = next(p for p in pins if p.startswith("@playwright/cli@")).rpartition("@")[2]
    root = Path(_npm_root() or "/nonexistent")
    cli = root / "@playwright" / "cli"
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
    axe = root / "axe-core" / "axe.min.js"
    try:
        digest = hashlib.sha256(axe.read_bytes()).hexdigest()
    except OSError:
        return None, "axe-core is not installed (dev setup installs the pinned 4.14.0)"
    if digest != AXE_SHA256:
        return None, f"{axe} is not axe-core 4.14.0"
    return {"core": str(core), "browsers": cache, "axe": str(axe)}, None


PINNED, NO_PINNED = _pinned()

# Launches Chromium like the capture driver and builds one isolated world per frame the way capture.cjs does (one CDP
# session for the page's process, Page.createIsolatedWorld for each frame; the planted frame shares the page's
# process). For each scenario it loads the page, runs axe.cjs, and reports what came back, what the page and the
# worlds hold afterwards, and, measured here on its own, where each planted element is in page CSS px. The pinned
# scenario also runs checks.js in the same main world, as the capture does before axe.
DRIVER = r"""
const o = JSON.parse(process.argv[1]);
const {chromium} = require(o.core);
const axe = require(o.axe_cjs);
const {runChecks} = require(o.checks);

async function isolatedWorlds(context, page) {
  const session = await context.newCDPSession(page);
  const tree = (await session.send("Page.getFrameTree")).frameTree;
  const worlds = new Map();
  const add = async (frame, id) => {
    const w = await session.send("Page.createIsolatedWorld", {frameId: id, worldName: "devkit-capture", grantUniveralAccess: false});
    worlds.set(frame, {session, contextId: w.executionContextId, frameId: id, parentSession: frame.parentFrame() ? session : null});
  };
  await add(page.mainFrame(), tree.frame.id);
  for (const child of page.mainFrame().childFrames()) {
    const match = (tree.childFrames || []).find(c => c.frame.name === child.name());
    if (match) await add(child, match.frame.id);
  }
  return worlds;
}

async function inWorld(world, fn, ...args) {
  const r = await world.session.send("Runtime.callFunctionOn", {functionDeclaration: fn.toString(),
    executionContextId: world.contextId, arguments: args.map(value => ({value})), returnByValue: true, awaitPromise: true});
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception ? r.exceptionDetails.exception.description : r.exceptionDetails.text);
  return r.result.value;
}

// the planted elements of one document, each with its page box: the frame's content box is added for a frame
function planted(offset) {
  return Array.from(document.querySelectorAll("[data-expect]"), el => {
    const r = el.getBoundingClientRect();
    const box = [r.left + offset[0], r.top + offset[1], r.right + offset[0], r.bottom + offset[1]];
    return {id: el.id, expect: el.dataset.expect.split(/\s+/), region: [box[0], box[1], box[2] - box[0], box[3] - box[1]].map(Math.round)};
  });
}

// how many elements of this document a selector matches, and the id of the one it matches
function resolve(selectors) {
  return selectors.map(s => { let all = []; try { all = document.querySelectorAll(s); } catch (e) { return {count: -1}; }
    return {count: all.length, id: all.length === 1 ? all[0].id : null}; });
}

(async () => {
  const browser = await chromium.launch({channel: "chromium", headless: true, chromiumSandbox: true,
                                         args: ["--disable-breakpad", "--disable-crash-reporter"]});
  const out = {};
  try {
    for (const sc of o.scenarios) {
      const context = await browser.newContext({viewport: {width: 390, height: 844}, colorScheme: "light",
        reducedMotion: "reduce", locale: "en-US", timezoneId: "UTC", serviceWorkers: "block"});
      const page = await context.newPage();
      await page.goto(sc.url || o.url);
      await page.evaluate(async () => {
        await document.fonts.ready;
        await new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)));
      });
      const worlds = await isolatedWorlds(context, page);
      const main = worlds.get(page.mainFrame());
      const frame = page.mainFrame().childFrames()[0];
      const state = async () => ({main: await page.evaluate(() => [scrollX, scrollY, document.documentElement.outerHTML]),
                                  frame: await frame.evaluate(() => [scrollX, scrollY, document.documentElement.outerHTML])});
      const r = {};
      if (sc.checks) r.checks = await inWorld(main, runChecks, 390);
      const before = await state();
      r.ran = []; r.skipped = {};
      r.signals = await axe.run(page, {worlds, ran: r.ran, skipped: r.skipped, ...(sc.file ? {file: sc.file} : {})});
      r.unchanged = JSON.stringify(before) === JSON.stringify(await state());
      r.page_sees_axe = await page.evaluate(() => typeof window.axe);
      r.worlds_hold_axe = [];
      for (const w of worlds.values()) r.worlds_hold_axe.push(await inWorld(w, () => typeof axe));
      // the frame's content box in page CSS px, measured from its element: border box, then border and padding
      const offset = await page.evaluate(() => {
        const el = document.querySelector("iframe"), b = el.getBoundingClientRect(), s = getComputedStyle(el);
        return [b.left + el.clientLeft + parseFloat(s.paddingLeft) + scrollX, b.top + el.clientTop + parseFloat(s.paddingTop) + scrollY];
      });
      r.planted = {main: await page.evaluate(planted, [await page.evaluate(() => scrollX), await page.evaluate(() => scrollY)]),
                   frame: await frame.evaluate(planted, offset)};
      const sels = r.signals.map(s => s.anchor.selector);
      r.resolved = {main: await page.evaluate(resolve, sels), frame: await frame.evaluate(resolve, sels)};
      out[sc.name] = r;
      await context.close();
    }
  } finally {
    await browser.close();
  }
  process.stdout.write(JSON.stringify(out));
})().catch(e => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
"""


CRITERIA_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, user-scalable=no">
<title>Criteria</title></head>
<body><p id="spacing" style="letter-spacing: 0.05em !important">Amounts in euros</p>
<iframe title="Notes" srcdoc="<!doctype html><html lang='en'><body><p>Notes</p></body></html>"></iframe></body></html>
"""


def drive(scenarios):
    # -> (result by scenario, None) or (None, why the driver failed); Playwright's profile goes to a folder of its own
    tmp = tempfile.mkdtemp(prefix="visual-axe-")
    try:
        os.chmod(tmp, 0o700)
        opts = {"core": PINNED["core"], "axe_cjs": str(AXE_CJS), "checks": str(CHECKS_JS), "url": PAGE.as_uri(),
                "scenarios": scenarios}
        env = dict(os.environ, TMPDIR=tmp, PLAYWRIGHT_BROWSERS_PATH=PINNED["browsers"])
        try:
            out = subprocess.run([NODE, "-e", DRIVER, json.dumps(opts)], capture_output=True, text=True,
                                 timeout=180, env=env, cwd=tmp)
        except subprocess.TimeoutExpired:
            return None, "the browser did not finish in 180 s"
        if out.returncode != 0:
            return None, out.stderr.strip()[-2000:]
        return json.loads(out.stdout), None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def where(result, i):
    # -> (document, planted id) the selector of signal i leads to: the frame's or the main page's, by its region
    s = result["signals"][i]
    for doc in ("main", "frame"):
        hit = result["resolved"][doc][i]
        if hit["count"] == 1:
            plant = next((p for p in result["planted"][doc] if p["id"] == hit["id"]), None)
            if plant and plant["region"] == s["anchor"]["region"]:
                return doc, plant["id"]
    return None, None


@unittest.skipUnless(PINNED, NO_PINNED)
class AxeOnThePlantedPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = Path(tempfile.mkdtemp(prefix="visual-axe-files-"))
        cls.tmp = tmp
        changed = tmp / "changed" / "axe.min.js"
        changed.parent.mkdir()
        # a file that is not the pinned one: injected, it would define axe in the worlds
        changed.write_text("window.axe = {run: async () => ({violations: [], incomplete: []})};\n")
        cls.results, cls.error = drive([
            {"name": "pinned", "checks": True},
            {"name": "changed", "file": str(changed)},
            {"name": "missing", "file": str(tmp / "nowhere" / "axe.min.js")},
        ])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def result(self, name="pinned"):
        if self.error:
            self.fail(f"axe.cjs did not run on the page: {self.error}")
        return self.results[name]

    def test_each_planted_defect_gives_one_signal_of_its_axe_rule(self):
        # a violation is a signal; a node axe could not decide is a review signal, given as advice
        r = self.result()
        got = Counter((*where(r, i), s["axe_rule"], s.get("advice", False)) for i, s in enumerate(r["signals"]))
        want = Counter((doc, p["id"], e.split(":", 1)[1], e.startswith("axe-review:"))
                       for doc in ("main", "frame") for p in r["planted"][doc]
                       for e in p["expect"] if e.startswith(("axe:", "axe-review:")))
        self.assertEqual(got, want)

    def test_a_node_axe_could_not_decide_says_why_with_axes_message_key(self):
        r = self.result()
        due = next(s for i, s in enumerate(r["signals"]) if where(r, i) == ("main", "due"))
        self.assertEqual((due["measured"], due["threshold"], due["advice"]), ("needs review (bgGradient)", "4.5:1", True))

    def test_a_signal_carries_the_criterion_of_its_wcag_tag_and_the_pass_it_reports_to(self):
        r = self.result()
        by_rule = {s["axe_rule"]: s for s in r["signals"] if not s.get("advice")}
        expected = {"color-contrast": (["wcag:1.4.3"], "color"), "target-size": (["wcag:2.5.8"], "accessibility"),
                    "button-name": (["wcag:4.1.2"], "accessibility"), "image-alt": (["wcag:1.1.1"], "accessibility")}
        for rule, (refs, pass_) in expected.items():
            with self.subTest(rule):
                self.assertEqual(by_rule[rule]["check"], "axe")
                self.assertEqual(by_rule[rule]["rule"], refs)
                self.assertEqual(by_rule[rule]["pass"], pass_)

    def test_measured_and_threshold_come_from_axes_numbers(self):
        # #aaaaaa and #b3b3b3 on white are 2.3204:1 and 2.0966:1, which axe cuts to two decimals; the dismiss
        # button is 16x16 px, 2 px from Export, so its safe clickable space is 2 x (8 + 2) = 20 px across
        r = self.result()
        got = {where(r, i): (s["measured"], s["threshold"]) for i, s in enumerate(r["signals"])}
        self.assertEqual(got[("main", "vat")], ("2.32:1", "4.5:1"))
        self.assertEqual(got[("frame", "paid")], ("2.09:1", "4.5:1"))
        self.assertEqual(got[("main", "dismiss")],
                         ("16×16 px, 20 px safe clickable space", "24×24 px, or 24 px safe clickable space"))
        self.assertEqual(got[("main", "settings")], (None, None))

    def test_a_signal_keeps_only_the_fields_a_signal_may_carry(self):
        # R27: axe's html, failureSummary and target never leave the page; the anchor is devkit's own selector
        for s in self.result()["signals"]:
            with self.subTest(s["axe_rule"]):
                self.assertLessEqual(set(s), {"check", "pass", "rule", "axe_rule", "anchor", "measured", "threshold",
                                              "advice"})
                self.assertEqual(set(s["anchor"]), {"element", "region", "selector", "name"})

    def test_each_anchor_is_the_element_box_and_a_selector_that_matches_it_alone(self):
        r = self.result()
        for i, s in enumerate(r["signals"]):
            with self.subTest(s["axe_rule"]):
                self.assertIsNotNone(where(r, i)[1], f"{s['anchor']} leads to no planted element")

    def test_the_frame_is_checked_once_and_without_the_rules_of_the_top_document(self):
        # the frame's document has no title: document-title is a rule of the top document only
        r = self.result()
        in_frame = [s["axe_rule"] for i, s in enumerate(r["signals"]) if where(r, i)[0] == "frame"]
        self.assertEqual(in_frame, ["color-contrast"])
        self.assertNotIn("document-title", [s["axe_rule"] for s in r["signals"]])

    def test_axe_runs_in_the_isolated_worlds_and_leaves_the_page_as_it_was(self):
        r = self.result()
        self.assertEqual(r["ran"], ["axe"])
        self.assertEqual(r["skipped"], {})
        self.assertEqual(r["page_sees_axe"], "undefined")
        self.assertEqual(r["worlds_hold_axe"], ["object", "object"])
        self.assertTrue(r["unchanged"], "axe.cjs changed the page, its scroll, or the frame")

    def test_a_file_that_is_not_the_pinned_one_is_never_injected(self):
        r = self.result("changed")
        self.assertEqual((r["signals"], r["ran"], r["skipped"]), ([], [], {"axe": "hash mismatch"}))
        self.assertEqual(r["worlds_hold_axe"], ["undefined", "undefined"])

    def test_a_missing_file_is_reported_as_not_installed(self):
        r = self.result("missing")
        self.assertEqual((r["signals"], r["ran"], r["skipped"]), ([], [], {"axe": "not installed"}))

    def test_axe_and_checks_js_on_one_element_fold_into_the_axe_signal(self):
        # the image that does not load and has no alt: axe image-alt and checks.js broken-image, one criterion (1.1.1)
        r = self.result()
        checks = {s["check"]: s for s in r["checks"]}
        axe = {s["axe_rule"]: s for s in r["signals"]}
        broken, alt = checks["broken-image"], axe["image-alt"]
        self.assertEqual((broken["anchor"]["region"], broken["anchor"]["selector"]),
                         (alt["anchor"]["region"], alt["anchor"]["selector"]))
        description = {"elements": [{"id": "E9", "role": "img", "name": None, "frame": None,
                                     "box": alt["anchor"]["region"], "selector": alt["anchor"]["selector"]}]}
        folded = run_fold(r["checks"] + r["signals"], description)
        self.assertEqual(len(folded), len(r["checks"]) + len(r["signals"]) - 1)
        kept = [s for s in folded if s["check"] in ("broken-image", "axe") and s["anchor"]["region"] == alt["anchor"]["region"]]
        self.assertEqual([(s["check"], s.get("axe_rule")) for s in kept], [("axe", "image-alt")])
        self.assertEqual([(d["check"], d["rule"]) for d in kept[0]["duplicates"]], [("broken-image", ["nielsen:9"])])


# --------------------------------------------------------------------------- the pass of an axe rule

@unittest.skipUnless(PINNED, NO_PINNED)
class AxeOnAPageOfOtherCriteria(unittest.TestCase):
    # a page whose defects map to the responsive (1.4.4) and typography (1.4.12) passes; no rule of axe 4.14.0
    # carries 1.4.10, so that mapping is covered by PassByCriterion alone
    @classmethod
    def setUpClass(cls):
        tmp = Path(tempfile.mkdtemp(prefix="visual-axe-criteria-"))
        try:
            page = tmp / "criteria.html"
            page.write_text(CRITERIA_PAGE)
            cls.results, cls.error = drive([{"name": "criteria", "url": page.as_uri()}])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_zoom_and_text_spacing_report_to_the_responsive_and_typography_passes(self):
        if self.error:
            self.fail(f"axe.cjs did not run on the page: {self.error}")
        by_rule = {s["axe_rule"]: (s["rule"], s["pass"]) for s in self.results["criteria"]["signals"]}
        self.assertEqual(by_rule["meta-viewport"], (["wcag:1.4.4"], "responsive"))
        self.assertEqual(by_rule["avoid-inline-spacing"], (["wcag:1.4.12"], "typography"))


PASS_FOR = r"""
const {passFor} = require(process.argv[1]);
process.stdout.write(JSON.stringify(JSON.parse(process.argv[2]).map(passFor)));
"""


def pass_for(*criteria_lists):
    # -> the pass axe.cjs gives each list of WCAG criteria
    out = subprocess.run([NODE, "-e", PASS_FOR, str(AXE_CJS), json.dumps(criteria_lists)], capture_output=True,
                         text=True, timeout=60)
    if out.returncode != 0:
        raise AssertionError(f"axe.cjs failed: {out.stderr.strip()[-1500:]}")
    return json.loads(out.stdout)


@unittest.skipUnless(NODE, "node is not on PATH")
class PassByCriterion(unittest.TestCase):
    # the spec's mapping: 1.4.4 and 1.4.10 responsive, 1.4.12 typography, other 1.4.x color, 2.5.8 and the others
    # accessibility
    def test_resize_text_and_reflow_report_to_responsive(self):
        self.assertEqual(pass_for(["1.4.4"], ["1.4.10"]), ["responsive", "responsive"])

    def test_text_spacing_reports_to_typography(self):
        self.assertEqual(pass_for(["1.4.12"]), ["typography"])

    def test_the_other_distinguishable_criteria_report_to_color(self):
        self.assertEqual(pass_for(["1.4.1"], ["1.4.3"], ["1.4.11"], ["1.4.2"]), ["color"] * 4)

    def test_target_size_and_the_other_criteria_report_to_accessibility(self):
        # 1.4 alone is not a 1.4.x criterion; a rule with no criterion reports to accessibility too
        self.assertEqual(pass_for(["2.5.8"], ["4.1.2"], ["1.1.1"], ["2.4.4", "4.1.2"], []), ["accessibility"] * 5)

    def test_a_rule_with_several_criteria_reports_to_the_first_that_is_not_accessibility(self):
        self.assertEqual(pass_for(["4.1.2", "1.4.4"], ["1.4.12", "1.4.3"], ["1.4.3", "1.4.12"]),
                         ["responsive", "typography", "color"])


# --------------------------------------------------------------------------- fold (R68)

FOLD = r"""
const {fold} = require(process.argv[1]);
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(fold(input.signals, input.description)));
"""


def run_fold(signals, description=None):
    out = subprocess.run([NODE, "-e", FOLD, str(FOLD_JS)], input=json.dumps({"signals": signals, "description": description}),
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise AssertionError(f"fold.js failed: {out.stderr.strip()[-1500:]}")
    return json.loads(out.stdout)


def sig(check, rule, element=None, region=(10, 20, 30, 40), selector="#a", **extra):
    return {"check": check, "pass": extra.pop("pass_", "layout"), "rule": list(rule),
            "anchor": {"element": element, "region": list(region) if region else None, "selector": selector,
                       "name": None}, "measured": extra.pop("measured", None), "threshold": extra.pop("threshold", None),
            **extra}


def desc(*elements):
    # a description with elements (id, role, box): what fold reads to resolve anchors and roles
    return {"elements": [{"id": e, "role": role, "name": None, "frame": None, "box": list(box), "selector": None}
                         for e, role, box in elements]}


@unittest.skipUnless(NODE, "node is not on PATH")
class Fold(unittest.TestCase):
    def test_one_criterion_on_one_element_keeps_the_axe_signal_and_lists_the_others(self):
        signals = [sig("impeccable", ["impeccable:low-contrast"], "E4", measured="2.9:1 (need 4.5:1)"),
                   sig("truncation", ["nielsen:6"], "E4"),
                   sig("axe", ["wcag:1.4.3"], "E4", axe_rule="color-contrast", measured="2.9:1", threshold="4.5:1")]
        out = run_fold(signals, desc(("E4", "paragraph", (10, 20, 30, 40))))
        self.assertEqual([(s["check"], s.get("axe_rule")) for s in out], [("truncation", None), ("axe", "color-contrast")])
        self.assertEqual(out[1]["duplicates"], [{"check": "impeccable", "rule": ["impeccable:low-contrast"],
                                                 "measured": "2.9:1 (need 4.5:1)", "threshold": None}])
        self.assertEqual(out[0]["duplicates"], [])

    def test_without_axe_devkit_is_kept_before_impeccable(self):
        signals = [sig("impeccable", ["impeccable:low-contrast"], "E2"), sig("contrast", ["wcag:1.4.3"], "E2")]
        out = run_fold(signals)
        self.assertEqual([s["check"] for s in out], ["contrast"])
        self.assertEqual([d["check"] for d in out[0]["duplicates"]], ["impeccable"])

    def test_signals_on_different_elements_or_criteria_outside_the_table_stay_apart(self):
        signals = [sig("axe", ["wcag:1.4.3"], "E1", axe_rule="color-contrast"),
                   sig("impeccable", ["impeccable:low-contrast"], "E2"),
                   sig("axe", ["wcag:4.1.2"], "E3", axe_rule="button-name"),
                   sig("tap-target", ["platform:touch-target-44"], "E3", advice=True),
                   sig("axe", ["wcag:1.4.3"], "E4", axe_rule="color-contrast"),
                   sig("impeccable", ["impeccable:gray-on-color"], "E4")]
        out = run_fold(signals)
        self.assertEqual(len(out), 6)
        self.assertTrue(all(s["duplicates"] == [] for s in out))

    def test_an_overlap_folds_with_target_size_only_when_both_boxes_are_controls(self):
        target = sig("axe", ["wcag:2.5.8"], "E1", axe_rule="target-size")
        overlap = sig("overlap", ["wcag:2.5.8"], "E1", other={"element": None, "region": [50, 20, 30, 40],
                                                              "selector": "#b", "name": None})
        for role, folded in (("button", True), ("paragraph", False)):
            with self.subTest(other=role):
                out = run_fold([overlap, target], desc(("E1", "button", (10, 20, 30, 40)), ("E2", role, (50, 20, 30, 40))))
                self.assertEqual(len(out), 1 if folded else 2)

    def test_the_same_check_and_rule_on_one_element_fold_into_the_first(self):
        first = sig("overlap", ["nielsen:8"], "E5", measured="4×18 px intersection",
                    other={"element": None, "region": [0, 0, 5, 5], "selector": "#x", "name": None})
        second = sig("overlap", ["nielsen:8"], "E5", measured="3×9 px intersection",
                     other={"element": None, "region": [9, 9, 5, 5], "selector": "#y", "name": None})
        other_rule = sig("overlap", ["wcag:2.5.8"], "E5")
        out = run_fold([first, second, other_rule])
        self.assertEqual([s["measured"] for s in out], ["4×18 px intersection", None])
        self.assertEqual(out[0]["duplicates"], [{"check": "overlap", "rule": ["nielsen:8"], "measured": "3×9 px intersection",
                                                 "threshold": None, "other": second["other"]}])

    def test_without_an_element_id_the_description_box_or_the_selector_names_the_element(self):
        # the axe signal is anchored by its box alone; the description puts E7 there. Two signals with neither an
        # element nor a selector are never folded, nor are two with one selector at two places
        a = sig("axe", ["wcag:1.4.3"], None, axe_rule="color-contrast", selector="p:nth-of-type(2)")
        b = sig("impeccable", ["impeccable:low-contrast"], "E7", selector=".muted")
        self.assertEqual(len(run_fold([a, b], desc(("E7", "paragraph", (10, 20, 30, 40))))), 1)
        self.assertEqual(len(run_fold([a, b])), 2)
        loose = [sig("console", ["nielsen:9"], None, region=None, selector=None, pass_="states") for _ in range(2)]
        self.assertEqual(len(run_fold(loose)), 2)
        apart = [sig("contrast", ["wcag:1.4.3"], None, region=(0, 0, 9, 9), selector="#m"),
                 sig("impeccable", ["impeccable:low-contrast"], None, region=(0, 50, 9, 9), selector="#m")]
        self.assertEqual(len(run_fold(apart)), 2)

    def test_a_folded_signal_brings_its_own_duplicates_along(self):
        carried = {"check": "impeccable", "rule": ["impeccable:low-contrast"], "measured": "3:1", "threshold": None}
        signals = [sig("contrast", ["wcag:1.4.3"], "E4", duplicates=[carried]),
                   sig("axe", ["wcag:1.4.3"], "E4", axe_rule="color-contrast")]
        out = run_fold(signals)
        self.assertEqual([d["check"] for d in out[0]["duplicates"]], ["contrast", "impeccable"])
        self.assertEqual(out[0]["duplicates"][1], carried)

    def test_folding_twice_changes_nothing(self):
        signals = [sig("impeccable", ["impeccable:low-contrast"], "E4"),
                   sig("axe", ["wcag:1.4.3"], "E4", axe_rule="color-contrast"),
                   sig("contrast", ["wcag:1.4.3"], "E4")]
        once = run_fold(signals)
        self.assertEqual(run_fold(once), once)
        self.assertEqual([d["check"] for d in once[0]["duplicates"]], ["contrast", "impeccable"])


class EquivalenceTable(unittest.TestCase):
    @unittest.skipUnless(PINNED, NO_PINNED)
    def test_every_axe_rule_in_the_table_is_a_rule_of_the_pinned_axe_for_its_criterion(self):
        table = json.loads(EQUIVALENTS.read_text())
        script = (r"const {chromium} = require(process.argv[1]); (async () => { const b = await chromium.launch({channel:"
                  r" 'chromium', headless: true, chromiumSandbox: true}); const p = await b.newPage(); await p.evaluate("
                  r"require('fs').readFileSync(process.argv[2], 'utf8')); process.stdout.write(JSON.stringify(await "
                  r"p.evaluate(t => axe.getRules(t).map(r => [r.ruleId, r.tags]), JSON.parse(process.argv[3])))); "
                  r"await b.close(); })().catch(e => { console.error(e); process.exit(1); });")
        tmp = tempfile.mkdtemp(prefix="visual-axe-rules-")
        self.addCleanup(shutil.rmtree, tmp, True)
        env = dict(os.environ, TMPDIR=tmp, PLAYWRIGHT_BROWSERS_PATH=PINNED["browsers"])
        out = subprocess.run([NODE, "-e", script, PINNED["core"], PINNED["axe"], json.dumps(TAGS)], capture_output=True,
                             text=True, timeout=120, env=env, cwd=tmp)
        self.assertEqual(out.returncode, 0, out.stderr[-1500:])
        rules = dict(json.loads(out.stdout))
        for criterion, row in table["criteria"].items():
            tag = "wcag" + criterion.replace(".", "")
            for rule in row["axe"]:
                with self.subTest(criterion=criterion, rule=rule):
                    self.assertIn(rule, rules)
                    self.assertIn(tag, rules[rule])

    def test_every_devkit_check_in_the_table_is_one_devkit_runs(self):
        # the checks of checks.js and the description's non-semantic-control and the documents' contrast
        table = json.loads(EQUIVALENTS.read_text())
        known = set(re.findall(r'signal\("([a-z-]+)"', CHECKS_JS.read_text())) | {"contrast", "non-semantic-control"}
        for criterion, row in table["criteria"].items():
            for check in row["devkit"]:
                with self.subTest(criterion=criterion, check=check):
                    self.assertIn(check, known)


if __name__ == "__main__":
    unittest.main()
