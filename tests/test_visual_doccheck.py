# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# lib/devkit/visual/doccheck.js checks one page of a document or a deck (R32): text too small to read, text inside
# the safe margin, text lines drawn over each other, a slide with too many words, and text whose contrast with its
# background is too low. It reads the page's description (its text lines, each with its box in picture px) and its
# rendered picture. These tests build both here, with sizes and
# colors worked out by hand: an A4 slide (842 x 595 pt) rendered 1920 px wide, as the capture renders a landscape
# page, and an A4 page (595 x 842 pt) rendered 1240 px wide. A line of text is drawn as stripes of its color with a
# blended column beside each, the way anti-aliased glyphs sit on their background. They need node only.
# Tests carry comments, not docstrings: `dev test prove` leaves out tests with a docstring.
# Run: python3 -m unittest tests.test_visual_doccheck
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCCHECK = ROOT / "lib" / "devkit" / "visual" / "doccheck.js"
NODE = shutil.which("node")

# Paints the picture the test describes (a background, then each painted box: a solid color, stripes of text, or a
# gradient from top to bottom), runs check(), and prints the signals.
DRIVER = r"""
const {check} = require(process.argv[1]);
const o = JSON.parse(require("fs").readFileSync(0, "utf8"));
let image = null;
if (o.image) {
  const {width, height} = o.image;
  const data = new Uint8Array(width * height * 4);
  const put = (x, y, c) => { const i = (y * width + x) * 4; data[i] = c[0]; data[i + 1] = c[1]; data[i + 2] = c[2]; data[i + 3] = 255; };
  const each = ([x0, y0, w, h], fn) => { for (let y = y0; y < y0 + h; y++) for (let x = x0; x < x0 + w; x++) fn(x, y); };
  const mix = (a, b) => a.map((v, i) => Math.round((v + b[i]) / 2));
  each([0, 0, width, height], (x, y) => put(x, y, o.image.background));
  for (const p of o.image.paint || []) {
    if (p.gradient) each(p.box, (x, y) => put(x, y, p.gradient.map(v => Math.round(v + (y - p.box[1]) * 2))));
    if (p.pixel) put(p.pixel[0], p.pixel[1], p.color);
    if (p.text) {
      // glyph-like stripes: 2 px of ink and 1 px blended with what lies under it, every 6 px, inside the line
      const under = (x, y) => { const i = (y * width + x) * 4; return [data[i], data[i + 1], data[i + 2]]; };
      const [x0, y0, w, h] = p.box;
      for (let x = x0 + 2; x + 3 <= x0 + w; x += 6) {
        for (let y = y0 + 2; y < y0 + h - 2; y++) {
          put(x + 2, y, mix(p.text, under(x + 2, y)));
          put(x, y, p.text);
          put(x + 1, y, p.text);
        }
      }
    }
  }
  image = {width, height, data};
}
process.stdout.write(JSON.stringify(check(o.description, {...o.page, ...(image ? {image} : {})})));
"""

SLIDE = {"size": [1920, 1357], "scale": 1920 / 842}  # A4 landscape, as the capture renders it
PAPER = {"size": [1240, 1754], "scale": 1240 / 595}  # A4 portrait
WHITE, INK, FAINT, GRAY = [255, 255, 255], [0x1D, 0x24, 0x33], [0xB4, 0xBA, 0xC4], [0x80, 0x80, 0x80]


def line(n, box, text="A line of text"):
    # one text line of a document description, as capture.cjs builds it from PDFKit's lines
    return {"id": f"E{n}", "parent": None, "frame": None, "role": "text", "name": None, "text": text, "states": {},
            "box": box, "locator": None, "selector": None, "url": None, "placeholder": None, "cursor": None,
            "style": None, "above_fold": True, "component": None, "redacted": None}


def run_check(lines, page, paint=None):
    # -> the signals check() gives for these lines on this page; `paint` draws the picture (None: no picture)
    spec = {"description": {"schema": "devkit.visual.description/1", "kind": "document", "elements": lines},
            "page": page}
    if paint is not None:
        spec["image"] = {"width": page["size"][0], "height": page["size"][1], "background": WHITE, "paint": paint}
    out = subprocess.run([NODE, "-e", DRIVER, str(DOCCHECK)], input=json.dumps(spec), capture_output=True, text=True,
                         timeout=120)
    if out.returncode != 0:
        raise AssertionError(f"doccheck.js failed: {out.stderr.strip()[-1500:]}")
    return json.loads(out.stdout)


def by_check(signals, check):
    return [(s["anchor"]["element"], s["measured"], s["threshold"]) for s in signals if s["check"] == check]


@unittest.skipUnless(NODE, "node is not on PATH")
class TextSize(unittest.TestCase):
    def test_slide_text_under_18_pt_of_a_960_pt_slide_is_too_small(self):
        # 18 pt of a 960 pt wide slide is 1.875% of its width: 36 px of 1920; on this 842 pt page a line of 18 pt is
        # 42 px high (41.04 px, its box rounded out to whole pixels), 16 pt 37 px, and 9 pt 21 px
        lines = [line(1, [164, 400, 580, 42]), line(2, [164, 500, 520, 37]), line(3, [164, 640, 145, 21])]
        self.assertEqual(by_check(run_check(lines, SLIDE), "min-text-size"),
                         [("E3", "21 px at 1920 px wide", "36 px at 1920 px wide (18 pt on a 960 pt wide slide)")])

    def test_document_text_under_9_pt_is_too_small(self):
        # 9 pt is the 12 CSS px of the screen rule at print size; at 1240 / 595 px a point, 8 pt is 17 px, 10 pt 21 px
        lines = [line(1, [150, 300, 400, 21]), line(2, [150, 400, 300, 17])]
        self.assertEqual(by_check(run_check(lines, PAPER), "min-text-size"), [("E2", "8.2 pt", "9 pt")])


@unittest.skipUnless(NODE, "node is not on PATH")
class SafeMargin(unittest.TestCase):
    def test_a_line_inside_the_5_percent_margin_names_its_nearest_edge(self):
        # 5% of 1920 px is 96 px, of 1357 px 68 px; a line exactly 96 px from the left edge is inside the safe area,
        # and E6, in the bottom left corner, is nearer the bottom
        lines = [line(1, [4, 880, 313, 41]), line(2, [1600, 1000, 260, 41]), line(3, [164, 1300, 300, 41]),
                 line(4, [96, 200, 300, 41]), line(5, [-20, 300, 300, 41]), line(6, [30, 1320, 300, 30])]
        self.assertEqual(by_check(run_check(lines, SLIDE), "safe-margin"), [
            ("E1", "4 px from the left edge", "96 px (5% of the page width)"),
            ("E2", "60 px from the right edge", "96 px (5% of the page width)"),
            ("E3", "16 px from the bottom edge", "68 px (5% of the page height)"),
            ("E5", "past the left edge", "96 px (5% of the page width)"),
            ("E6", "7 px from the bottom edge", "68 px (5% of the page height)")])


@unittest.skipUnless(NODE, "node is not on PATH")
class OverlappingLines(unittest.TestCase):
    def test_two_lines_whose_boxes_cross_by_more_than_2_px_overlap(self):
        # E2 starts 30 px below E1's top, so the two 42 px lines cross by 12 px over their common 580 px; E4 starts
        # 2 px above E3's bottom and E6 2 px left of E5's right edge, which is the rounding of the boxes to whole
        # pixels; E8 is drawn inside E7, the whole of it over E7's text
        lines = [line(1, [164, 300, 580, 42]), line(2, [164, 330, 580, 42]), line(3, [164, 400, 580, 42]),
                 line(4, [164, 440, 580, 42]), line(5, [900, 600, 300, 42]), line(6, [1198, 600, 300, 42]),
                 line(7, [164, 700, 580, 42]), line(8, [300, 705, 100, 30])]
        overlaps = [s for s in run_check(lines, SLIDE) if s["check"] == "overlap"]
        self.assertEqual([(s["anchor"]["element"], s["other"], s["pass"], s["rule"], s["measured"], s["threshold"])
                          for s in overlaps], [
            ("E1", {"element": "E2", "region": [164, 330, 580, 42], "selector": None}, "layout", ["nielsen:8"],
             "580×12 px intersection", "2 px"),
            ("E7", {"element": "E8", "region": [300, 705, 100, 30], "selector": None}, "layout", ["nielsen:8"],
             "100×30 px intersection", "2 px")])


TEN = "one two three four five six seven eight nine ten"


@unittest.skipUnless(NODE, "node is not on PATH")
class WordsPerSlide(unittest.TestCase):
    def test_a_slide_over_40_words_is_too_wordy_and_a_slide_of_40_or_a_portrait_page_is_not(self):
        # four lines of ten words and one of a single word: 41 words, counted across the lines; the anchor is the
        # region the lines cover, x 164 to 1064 and y 260 to 642. Without its last line the slide holds 40, the
        # limit; and a portrait page is a document, not a slide, with no limit
        lines = [line(n, [164, 200 + 60 * n, 900, 42], TEN) for n in range(1, 5)] + [line(5, [164, 600, 120, 42],
                                                                                          "eleven")]
        wordy = [s for s in run_check(lines, SLIDE) if s["check"] == "words-per-slide"]
        self.assertEqual([(s["anchor"], s["pass"], s["rule"], s["measured"], s["threshold"]) for s in wordy],
                         [({"element": None, "region": [164, 260, 900, 382], "selector": None}, "content",
                           ["ds:words-per-slide"], "41 words", "40 words")])
        self.assertEqual(by_check(run_check(lines[:4], SLIDE), "words-per-slide"), [])
        self.assertEqual(by_check(run_check(lines, PAPER), "words-per-slide"), [])

    def test_bullets_and_dashes_are_not_words_and_a_number_is_one(self):
        # 40 words, then a line with a bullet, a number written with a comma and a point, and a dash: 41 words
        lines = [line(n, [164, 200 + 60 * n, 900, 42], TEN) for n in range(1, 5)] + [line(5, [164, 600, 300, 42],
                                                                                          "\u2022 1,240.00 \u2013")]
        self.assertEqual(by_check(run_check(lines, SLIDE), "words-per-slide"), [(None, "41 words", "40 words")])

    def test_a_line_the_description_does_not_hold_whole_makes_the_count_a_lower_bound(self):
        # the description cuts a line at 120 characters (119 and an ellipsis): this one keeps 20 words of more; a
        # line with a secret holds "[redacted: <rule>]", not its words, and counts as one: 30 + 20 + 1 = at least 51
        cut = ("alpha " * 20)[:119] + "\u2026"
        secret = dict(line(5, [164, 600, 600, 42], "[redacted: aws-access-token]"), redacted="secret:aws-access-token")
        lines = [line(n, [164, 200 + 60 * n, 900, 42], TEN) for n in range(1, 4)] + [line(4, [164, 440, 1600, 42],
                                                                                          cut), secret]
        self.assertEqual(by_check(run_check(lines, SLIDE), "words-per-slide"), [(None, "at least 51 words",
                                                                                  "40 words")])


@unittest.skipUnless(NODE, "node is not on PATH")
class Contrast(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # by the diagram validator's formula: #b4bac4 on white is 1.9514:1, #808080 3.9494:1, #1d2433 15.528:1.
        # E1, E2 and E7 are large text (18 pt, 42 px), E3 is 16 pt; E4 sits on a gradient, E5 has no ink at all, and
        # E7 is faint text with one black pixel in it, which must not decide its color
        cls.lines = [line(1, [164, 300, 580, 42]), line(2, [164, 400, 580, 42]), line(3, [164, 500, 520, 37]),
                     line(4, [164, 600, 520, 41]), line(5, [164, 700, 520, 41]), line(6, [164, 800, 520, 41]),
                     line(7, [164, 900, 580, 42])]
        cls.signals = run_check(cls.lines, SLIDE, [
            {"box": [164, 300, 580, 42], "text": FAINT}, {"box": [164, 400, 580, 42], "text": GRAY},
            {"box": [164, 500, 520, 37], "text": GRAY}, {"gradient": [120, 140, 160], "box": [164, 600, 520, 41]},
            {"box": [164, 600, 520, 41], "text": INK}, {"box": [164, 800, 520, 41], "text": INK},
            {"box": [164, 900, 580, 42], "text": FAINT}, {"pixel": [300, 920], "color": [0, 0, 0]}])

    def test_faint_text_fails_with_its_ratio_and_large_text_needs_3_to_1(self):
        self.assertEqual(by_check(self.signals, "contrast"),
                         [("E1", "1.95:1", "3:1"), ("E3", "3.94:1", "4.5:1"), ("E7", "1.95:1", "3:1")])

    def test_text_on_a_picture_or_a_gradient_is_left_for_review(self):
        unknown = [s for s in self.signals if s["check"] == "contrast-unknown"]
        self.assertEqual([(s["anchor"]["element"], s["rule"], s.get("advice")) for s in unknown],
                         [("E4", ["wcag:1.4.3"], True)])

    def test_without_the_picture_contrast_is_not_checked(self):
        checks = {s["check"] for s in run_check(self.lines, SLIDE)}
        self.assertEqual(checks & {"contrast", "contrast-unknown"}, set())


@unittest.skipUnless(NODE, "node is not on PATH")
class BadInput(unittest.TestCase):
    def test_a_page_without_its_size_or_scale_is_refused(self):
        script = ("const {check} = require(process.argv[1]); const d = {elements: []};"
                  "for (const page of [{}, {size: [1920, 1357]}, {scale: 2}, {size: [0, 0], scale: 2}]) {"
                  "  try { check(d, page); console.log('accepted'); } catch (e) { console.log(e.message); } }")
        out = subprocess.run([NODE, "-e", script, str(DOCCHECK)], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.splitlines(),
                         ["check takes the page: {size: [width, height] in px, scale: px per point}."] * 4)


@unittest.skipUnless(NODE, "node is not on PATH")
class SignalShape(unittest.TestCase):
    def test_a_signal_has_the_screen_shape_its_line_as_anchor_and_no_page_text(self):
        signals = run_check([line(1, [4, 640, 145, 21], text="Fees may apply")], SLIDE, [])
        self.assertEqual([(s["check"], s["pass"], s["rule"]) for s in signals],
                         [("min-text-size", "typography", ["ds:min-text-size"]),
                          ("safe-margin", "layout", ["ds:safe-margin"])])
        for s in signals:
            self.assertLessEqual(set(s), {"check", "pass", "rule", "anchor", "measured", "threshold", "advice"})
            self.assertEqual(s["anchor"], {"element": "E1", "region": [4, 640, 145, 21], "selector": None})
            self.assertNotIn("Fees", json.dumps(s))


if __name__ == "__main__":
    unittest.main()
