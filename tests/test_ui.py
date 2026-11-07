# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The banner (lib/devkit/ui.py) on a terminal whose theme devkit cannot tell: macOS Terminal sets no COLORFGBG, so
# the banner must read on a light and on a dark background alike. The tests force colors (24-bit, or 256 as in macOS
# Terminal) and clear the theme variables; they never read or write the terminal. Run: python3 -m unittest tests.test_ui
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import ui  # noqa: E402

FG = re.compile(r"\x1b\[(?:1;)?38;2;(\d+);(\d+);(\d+)m")
FG_RUN = re.compile(r"\x1b\[(?:1;)?38;2;(\d+);(\d+);(\d+)m([^\x1b]*)")
WHITE, BLACK = (255, 255, 255), (0, 0, 0)
CUBE = (0, 95, 135, 175, 215, 255)  # the xterm 256-color cube's levels


def contrast(a, b) -> float:
    # WCAG 2 contrast ratio of two sRGB colors
    def lum(rgb):
        lin = [c / 255 / 12.92 if c / 255 <= 0.04045 else ((c / 255 + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def xterm(n: int):
    # The RGB of a 256-color index (the cube and the gray ramp); "palette" for 0-15, which the terminal's theme decides
    if n < 16:
        return "palette"
    if n >= 232:
        return (8 + 10 * (n - 232),) * 3
    n -= 16
    return CUBE[n // 36], CUBE[n // 6 % 6], CUBE[n % 6]


def inks(text: str) -> list:
    # (ink, run) for each visible run of text, with the foreground in force: an (r, g, b) for a 24-bit or 256-color one,
    # "palette" for one of the 16 colors the terminal's theme decides (30-37, 90-97), None for its own text color
    parts = re.split(r"\x1b\[([0-9;]*)m", text)
    ink, runs = None, [(None, parts[0])]
    for params, run in zip(parts[1::2], parts[2::2]):
        codes = [int(c or 0) for c in params.split(";")]
        while codes:
            code = codes.pop(0)
            if code in (38, 48):  # 5;n or 2;r;g;b, for the foreground (38) or the background (48)
                color = xterm(codes.pop(0)) if codes.pop(0) == 5 else tuple(codes.pop(0) for _ in range(3))
                ink = color if code == 38 else ink
            elif code in (0, 39):
                ink = None
            elif 30 <= code <= 37 or 90 <= code <= 97:
                ink = "palette"
        runs.append((ink, run))
    return [(ink, run) for ink, run in runs if run.strip()]


class BannerThemeTests(unittest.TestCase):
    def banner(self, cols=100, truecolor=True, **env):
        # The banner in color (24-bit, or 256 colors), `cols` wide, with only these theme variables set. At 40 columns it
        # is the one-line logo, which is also the header of every setup wizard screen.
        base = {k: v for k, v in os.environ.items() if k not in ("DEVKIT_THEME", "COLORFGBG")}
        with mock.patch.dict(os.environ, {**base, **env}, clear=True), mock.patch.object(ui, "TRUECOLOR", truecolor), \
                mock.patch.object(ui, "COLOR", True), mock.patch.object(ui, "width", return_value=cols):
            return ui.banner("setup")

    def test_an_unknown_theme_keeps_every_banner_color_readable_on_white_and_black(self):
        self.assertIn(ui.WORDMARK[2], ui.ANSI_RE.sub("", self.banner()))
        for cols in (100, 40):
            with self.subTest(cols=cols):
                colors = {tuple(map(int, c)) for c in FG.findall(self.banner(cols))}
                self.assertTrue(colors, "the spark keeps its amber")
                for rgb in colors:
                    self.assertGreaterEqual(round(contrast(rgb, WHITE), 2), 3, f"{rgb} on white")
                    self.assertGreaterEqual(round(contrast(rgb, BLACK), 2), 3, f"{rgb} on black")

    def test_a_light_theme_keeps_the_spark_and_the_tagline_readable_on_white(self):
        # COLORFGBG 0;15: black on white. The tagline is text (4.5:1), the spark a mark (3:1).
        for cols in (100, 40):
            with self.subTest(cols=cols):
                runs = [(tuple(map(int, rgb)), text)
                        for *rgb, text in FG_RUN.findall(self.banner(cols, COLORFGBG="0;15"))]
                spark = [rgb for rgb, text in runs if "✦" in text]
                tagline = [rgb for rgb, text in runs if ui.TAGLINE in text]
                self.assertTrue(spark, "the spark keeps a color")
                for rgb in spark:
                    self.assertGreaterEqual(round(contrast(rgb, WHITE), 2), 3, f"spark {rgb} on white")
                for rgb in tagline:
                    self.assertGreaterEqual(round(contrast(rgb, WHITE), 2), 4.5, f"tagline {rgb} on white")

    def test_without_truecolor_a_light_or_unknown_theme_keeps_the_logo_and_the_wordmark_readable(self):
        # 256 colors, as in macOS Terminal. The terminal's own cyan and yellow can be too light on white, so the text keeps
        # the terminal's text color and the spark takes a color of its own that reads on white, and on black too when the
        # theme is unknown: 4.5:1 for text, 3:1 for a mark. 100 columns: the wordmark; 40: the one-line logo.
        for theme, env, backgrounds in (("unknown", {}, (WHITE, BLACK)), ("light", {"COLORFGBG": "0;15"}, (WHITE,))):
            for cols in (100, 40):
                with self.subTest(theme=theme, cols=cols):
                    runs = inks(self.banner(cols, truecolor=False, **env))
                    for ink, run in runs:
                        self.assertNotEqual(ink, "palette", f"{run.strip()!r} takes a color of the terminal's palette")
                        for bg in backgrounds if ink else ():
                            floor = 4.5 if re.search(r"\w", run) else 3
                            self.assertGreaterEqual(round(contrast(ink, bg), 2), floor, f"{run.strip()!r} {ink} on {bg}")
                    self.assertTrue([ink for ink, run in runs if "✦" in run and ink], "the spark keeps a color")


if __name__ == "__main__":
    unittest.main()
