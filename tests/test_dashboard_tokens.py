# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The dashboard draws its colors through names: tokens (var(--ok)) and status classes (k-warn on a bar, d-risk on a
# dot). A name the page builds from parts ("var(--s-" + tone + ")", "dot k-" + st) is out of sight of a check that
# reads names whole, and a name no theme defines, or a status class without a background, draws nothing: the Tasks
# board's Closed dot, the Health meters and a quota tile's share were transparent that way. These tests read every
# name the page can build from the page template, the styles and the script (lib/devkit/web/), and check that each
# one paints in both themes, from the sources and in headless Chromium. They also check that a control which unsets
# its styles still shows the focus ring to keyboard users, and that a copy button showing its command is named by it.
# Tests carry comments, not docstrings: `dev test prove` leaves out tests with a docstring.
# Run: python3 -m unittest tests.test_dashboard_tokens
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
sys.path.insert(0, str(ROOT / "lib"))

import test_dashboard as T  # noqa: E402  (the color helpers, the fixture and the browser harness)
from devkit import dashboard  # noqa: E402

# a token name built from parts, with or without a name prefix, by concatenation or a template:
# "var(--" + t, "var(--s-" + tone, `var(--s-${tone})`, `var(-- ${t})`, "var(" + "--" + t
BUILT_TOKEN = re.compile(r"""var\(\s*(?:--[\w-]*\s*)?(?:["'`]\s*\+|\$\{)""")
READ_TOKEN = re.compile(r"var\(--([\w-]+)\)")  # a token read whole without a fallback
# a status class built from its prefix: "k-" + k, "dot k-" + st, "dot hd d-" + lv, `dot k-${st}`
BUILT_CLASS = re.compile(r"""["'`](?:[^"'`\n]*\s)?([kd])-["'`]\s*\+\s*""")
BUILT_CLASS_TEMPLATE = re.compile(r"""(?<![\w-])([kd])-\$\{""")
STATUS_CLASS = re.compile(r"(?<![\w-])([kd])-([a-z][\w-]*)")
KEYS, VALUES, TONES = "keys", "values", "tones"
# the script's state tables each family is built from: the k- classes (status fills: segments, squares, dots) from
# the health states (HS: the waffle, stat dots), the CI strip's tones (CIK) and the [tone, word] tables of jobs, CI
# watches and branches (stateDot); the d- classes (dots) from the health levels (HRANK: healthDot), the pull request
# check states (CHECKS: prPill, the PR tables), the severities (SEVL: actionRow) and the provider tones (METER)
FAMILIES = {"k": (("HS", KEYS), ("CIK", VALUES), ("JOBST", TONES), ("CIST", TONES), ("BSTATE", TONES)),
            "d": (("HRANK", KEYS), ("CHECKS", KEYS), ("SEVL", KEYS), ("METER", KEYS))}
HOLLOW = "k-hol"  # the one status class that is a ring by design: no fill, an inset shadow


def script() -> str:
    return re.sub(r"/\*.*?\*/", "", dashboard.JS, flags=re.S)


def line_of(text: str, at: int) -> int:
    return text.count("\n", 0, at) + 1


def operand(js: str, at: int) -> str:
    # what a "+" adds at `at`: a parenthesized expression whole, else a name with its dots and one [index]
    if js[at:at + 1] == "(":
        depth = 0
        for j in range(at, len(js)):
            depth += {"(": 1, ")": -1}.get(js[j], 0)
            if depth == 0:
                return js[at:j + 1]
    m = re.compile(r"[\w$.]+(?:\[[^\]]*\])?").match(js, at)
    return m.group(0) if m else ""


def braced(js: str, at: int) -> str:
    # the expression of a template's ${…} whose { is at `at`
    depth = 0
    for j in range(at, len(js)):
        depth += {"{": 1, "}": -1}.get(js[j], 0)
        if depth == 0:
            return js[at + 1:j]
    return js[at + 1:]


def table(js: str, name: str, part: str) -> set:
    # the keys, word values or [tone, word] tones of the script's `NAME = {…}`
    m = re.search(rf"(?<![\w.]){name} = \{{(.*?)\}};?", js, re.S)
    if not m:
        raise AssertionError(f"the script has no table {name}: name the table this family is built from now")
    body = m.group(1)
    if part == KEYS:
        return set(re.findall(r"([A-Za-z_]\w*)\s*:", body))
    if part == VALUES:
        return set(re.findall(r""":\s*["']([\w-]+)["']""", body))
    return set(re.findall(r"""\[\s*["']([\w-]+)["']""", body))


def built_suffixes(js: str) -> dict:
    # prefix -> the suffixes the script can give it: every whole class (SDOT's "d-ok", "dot d-live"), the words inside
    # each built class's expression ("d-" + (tone === "risk" ? "risk" : "warn")), and the tones of the family's tables
    out = {"k": set(), "d": set()}
    for p, s in STATUS_CLASS.findall(js):
        out[p].add(s)
    sites = [(m.group(1), operand(js, m.end())) for m in BUILT_CLASS.finditer(js)]
    sites += [(m.group(1), braced(js, m.end() - 1)) for m in BUILT_CLASS_TEMPLATE.finditer(js)]
    for p, expr in sites:
        out[p] |= set(re.findall(r"""["'`]([a-z][\w-]*)["'`]""", expr))
    for p, tables in FAMILIES.items():
        for name, part in tables:
            out[p] |= table(js, name, part)
    return out


def declarations(body: str) -> dict:
    return {k.strip(): v.strip() for k, v in (d.split(":", 1) for d in body.split(";") if ":" in d)}


def base_rules(css: str) -> dict:
    # status class -> the declarations of the rules that name it alone (.k-warn, .d-risk), merged in source order
    out = {}
    for sel, body in T.css_rules(css):
        for s in sel.split(","):
            m = re.fullmatch(r"\s*\.([kd]-[\w-]+)\s*", s)
            if m:
                out.setdefault(m.group(1), {}).update(declarations(body))
    return out


def color_in(theme: dict, value):
    # the opaque color a declared value is in a theme, or None when it draws nothing there
    if value is None:
        return None
    m = re.fullmatch(r"var\(--([\w-]+)\)", value.strip())
    if m:
        if m.group(1) not in theme:
            return None
        value = T.resolve(theme, m.group(1))
    v = value.strip().lower()
    if v in ("none", "transparent") or re.fullmatch(r"rgba\([^)]*,\s*0(?:\.0*)?\s*\)", v):
        return None
    try:
        return T.rgb(v)
    except AssertionError:
        return None


class TokenNameTests(unittest.TestCase):
    # The check for built names reads each way the review found it blind to, and leaves names written whole alone
    def test_the_check_sees_each_way_of_building_a_token_name(self):
        built = ['"var(--" + tone + ")"', '"var(--s-" + tone + ")"', "`var(--s-${tone})`", '"var(" + "--" + t + ")"',
                 "`var(-- ${t})`", "'var(--s-' +\n  tone", "`var(${name})`"]
        self.assertEqual([b for b in built if not BUILT_TOKEN.search(b)], [])
        self.assertEqual(BUILT_TOKEN.findall('"var(--ok)" "fill:var(--s-ok, var(--ok))" "width:var(--fs-1)"'), [])

    # The page template and the script read every token by its whole name, so the check below sees each one
    def test_the_page_and_the_script_name_every_token_they_read_whole(self):
        found = [f"{where}:{line_of(text, m.start())}: {text[m.start():m.start() + 40]!r}"
                 for where, text in (("page.html", dashboard.HTML), ("the script", dashboard.JS))
                 for m in BUILT_TOKEN.finditer(text)]
        self.assertEqual(found, [], "token names built from parts")

    # Every token the styles, the script or the page template read without a fallback is defined in each theme (or
    # by the page itself, such as --row): one only the light theme defines is transparent in the dark one
    def test_every_token_the_page_reads_is_defined_in_both_themes(self):
        page = set(re.findall(r"--([\w-]+)\s*:", dashboard.CSS + dashboard.HTML)) | \
            set(re.findall(r"""["'\s;{]--([\w-]+):""", dashboard.JS))
        read = set(READ_TOKEN.findall(dashboard.CSS + dashboard.JS + dashboard.HTML))
        for name, text in (("light", dashboard.LIGHT), ("dark", dashboard.DARK)):
            self.assertEqual(sorted(read - page - set(T.theme_tokens(text))), [], f"tokens the {name} theme lacks")


class StatusClassTests(unittest.TestCase):
    def setUp(self):
        self.themes = {"light": T.theme_tokens(dashboard.LIGHT), "dark": T.theme_tokens(dashboard.DARK)}
        self.rules = base_rules(dashboard.CSS)

    # Each status class paints in both themes: a background (a segment, a legend square, a dot are HTML) and, for a k-
    # class, a fill (the waffle's squares and the job strip's bars are SVG). .k-warn once set only the fill, so the
    # quota tile's share, an HTML segment, was transparent. The hollow class is a ring: its inset shadow shows
    def test_every_status_class_paints_in_both_themes(self):
        blank = []
        for cls, d in sorted(self.rules.items()):
            for name, theme in self.themes.items():
                if cls == HOLLOW:
                    if color_in(theme, (re.search(r"var\(--[\w-]+\)", d.get("box-shadow", "")) or [None])[0]) is None:
                        blank.append(f"{name}: .{cls} shows no ring")
                    continue
                if color_in(theme, d.get("background", d.get("background-color"))) is None:
                    blank.append(f"{name}: .{cls} has no background")
                if cls.startswith("k-") and color_in(theme, d.get("fill")) is None:
                    blank.append(f"{name}: .{cls} has no fill")
        self.assertEqual(blank, [])

    # The check above sees the classes the styles define; this one sees the classes the script builds. The suffixes
    # come from the classes it names whole, the words inside each built class's expression and the state tables each
    # family is built from (FAMILIES). A tone with no rule ("d-" + "good" before .d-good) is transparent
    def test_every_status_class_the_script_builds_has_a_rule(self):
        suffixes = built_suffixes(script())
        missing = sorted(f"{p}-{s}" for p, ss in suffixes.items() for s in ss if f"{p}-{s}" not in self.rules)
        self.assertEqual(missing, [], "status classes the script builds with no rule of their own")
        # the sources the suffixes come from are read: the families' tables and the built sites
        self.assertLessEqual({"ok", "warn", "risk", "quiet", "done", "none", "mark", "hol"}, suffixes["k"])
        self.assertLessEqual({"passing", "failing", "pending", "unknown", "info", "good", "warning", "critical", "live"},
                             suffixes["d"])

    # The suffix reader finds the built classes the review named, and the words of their expressions
    def test_the_suffix_reader_sees_each_way_of_building_a_status_class(self):
        js = ('const HS = {ok: "On track"}, HRANK = {risk: 0}; const CIK = {passing: "ok"}; const CHECKS = {pending: "x"};'
              'const SEVL = {info: "x"}; const METER = {good: "var(--ok)"}; const JOBST = {off: ["hol", "Off"]};'
              'const CIST = {none: ["mark", "x"]}; const BSTATE = {merged: ["done", "x"]};'
              'h("i", {class: "k-" + (tone === "risk" ? "risk" : "quiet")}); h("span", {class: "dot k-" + st});'
              'h("span", {class: "dot hd d-" + (lv || "live")}); h("span", {class: `dot k-${a ? "none" : "warn"}`});'
              'h("span", {class: "dot d-failing"});')
        self.assertEqual(built_suffixes(js), {"k": {"ok", "risk", "quiet", "none", "warn", "hol", "mark", "done"},
                                              "d": {"risk", "pending", "info", "good", "live", "failing"}})


class FixturePage:
    # the fixture's page, once for a class, and a probe of it at 1440 px
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.page = dashboard.page(T.fixture_data(Path(cls.tmp.name) / "handoff", T.NOW))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def probe(self, steps, budget=12000):
        out = T.browse(self.page, steps, 1440, "#/now", budget=budget, now=T.NOW)
        self.assertIsNone(out.get("probeError") or out.get("pageError"))
        return out


@unittest.skipUnless(T.CHROME, T.NO_CHROME)
class StatusClassPaintTests(FixturePage, unittest.TestCase):
    # Every view of the fixture, in headless Chromium: each element with a k- or d- class draws a color in light and
    # dark, an HTML element its background and an SVG one its fill; the hollow class on HTML shows its ring instead.
    # This sees what the sources cannot: a rule that a more specific one overrides, a token that resolves to nothing
    WALK = """const bad = [], seen = new Set();
        const blank = v => !v || v === "none" || v === "transparent" || /^rgba\\([^)]*,\\s*0\\)$/.test(v);
        for (const r of ["#/now", "#/now?all=1", "#/tasks", "#/background", "#/team", "#/projects", "#/projects/acme/widget",
                         "#/projects/acme/widget?tab=prs", "#/projects/acme/widget?tab=branches", "#/initiatives", "#/initiatives/lending",
                         "#/activity", "#/explorer", "#/health", "#/search?q=widget"]) {
          await go(r);
          for (const theme of ["light", "dark"]) { document.documentElement.dataset.theme = theme; await tick(40);
            for (const el of $$("[class]")) for (const c of String(el.getAttribute("class")).split(/\\s+/)) if (/^[kd]-[a-z]/.test(c)) {
              const svg = el instanceof SVGElement, s = getComputedStyle(el), paint = svg ? s.fill : s.backgroundColor;
              seen.add(c + (svg ? " svg" : ""));
              if (c === "k-hol" && !svg ? s.boxShadow === "none" : blank(paint)) bad.push([r, theme, c, svg ? "svg" : "html", paint]); } } }
        out.bad = bad; out.seen = [...seen].sort();"""

    def test_every_status_class_on_every_view_paints_in_both_themes(self):
        out = self.probe(self.WALK, budget=20000)
        self.assertEqual(out["bad"], [])
        # the walk drew the families it is about: fills on HTML and SVG, and dots
        self.assertLessEqual({"k-ok", "k-warn", "k-risk", "k-none", "k-risk svg", "d-risk", "d-warn", "d-good"}, set(out["seen"]))


class FocusRingSourceTests(unittest.TestCase):
    # WCAG 2.4.7: `all: unset` takes the browser's focus outline away with every other style, and a rule like
    # `.hbar button` outranks the page's own :focus-visible ring, so each control that unsets its styles draws the ring
    # again from the focus token (the Health Disk panel's project names had none)
    def test_every_control_that_unsets_its_styles_draws_the_focus_ring_again(self):
        rules = T.css_rules(dashboard.CSS)
        unset = [s.strip() for sel, body in rules if re.search(r"(?:^|;)\s*all\s*:\s*unset\b", body) for s in sel.split(",")]
        ring = {s.strip()[:-len(":focus-visible")] for sel, body in rules
                if re.search(r"(?:^|;)\s*outline\s*:[^;]*\bsolid\b[^;]*var\(--focus\)", body)
                for s in sel.split(",") if s.strip().endswith(":focus-visible")}
        self.assertTrue(unset)
        self.assertEqual(sorted(s for s in unset if s not in ring), [], "controls that unset their styles with no focus ring")


@unittest.skipUnless(T.CHROME, T.NO_CHROME)
class RankedBarFocusTests(FixturePage, unittest.TestCase):
    # WCAG 2.4.7: a project name in a ranked bar list (Health's Disk panel, Activity's Most active) is a button that
    # unsets its styles; focused from the keyboard it shows the page's focus ring, 2 px of --focus, light and dark
    def test_a_ranked_bars_project_button_shows_the_focus_ring_in_both_themes(self):
        out = self.probe("""out.t = {};
            for (const hs of ["#/health", "#/activity"]) { await go(hs);
              for (const theme of ["light", "dark"]) { document.documentElement.dataset.theme = theme; await tick(40);
                const b = $("main .hbar button"); b.focus({focusVisible: true}); await tick(40); const s = getComputedStyle(b);
                out.t[hs + " " + theme] = {visible: b.matches(":focus-visible"), style: s.outlineStyle, width: s.outlineWidth, color: s.outlineColor,
                  focus: getComputedStyle(document.documentElement).getPropertyValue("--focus").trim()}; b.blur(); } }""")
        self.assertEqual(len(out["t"]), 4, out)
        for where, t in out["t"].items():
            self.assertEqual((t["visible"], t["style"], t["width"]), (True, "solid", "2px"), (where, t))
            self.assertEqual(T.rgb(t["color"]), T.rgb(t["focus"]), (where, t))


@unittest.skipUnless(T.CHROME, T.NO_CHROME)
class CopyButtonNameTests(FixturePage, unittest.TestCase):
    # WCAG 2.5.3: a copy button that shows its command (a short one: dev gc --apply on Health, dev clone <owner/repo>
    # on an initiative's projects) has a name that starts with that command, so a person who says what they see
    # reaches it by voice; the name still says the button copies it
    def test_a_copy_button_that_shows_its_command_is_named_by_it_first(self):
        out = self.probe("""out.names = [];
            for (const hs of ["#/health", "#/initiatives/lending?tab=projects"]) { await go(hs);
              for (const b of $$("main button.btn[aria-label]")) out.names.push([text(b), b.getAttribute("aria-label")]); }""")
        shown = {vis: name for vis, name in out["names"] if vis.startswith("dev ")}
        self.assertLessEqual({"dev gc --apply", "dev clone <owner/repo>"}, set(shown), out)
        for vis, name in out["names"]:
            self.assertTrue(name.lower().startswith(vis.lower()), (vis, name))
        for vis, name in shown.items():
            self.assertIn("copy", name[len(vis):].lower(), (vis, name))


if __name__ == "__main__":
    unittest.main()
