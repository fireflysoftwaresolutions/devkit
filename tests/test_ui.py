# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The banner (lib/devkit/ui.py) on a terminal whose theme devkit cannot tell: macOS Terminal sets no COLORFGBG, so
# the banner must read on a light and on a dark background alike. The tests force truecolor and clear the theme
# variables; they never read or write the terminal. Run: python3 -m unittest tests.test_ui
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


def contrast(a, b) -> float:
    # WCAG 2 contrast ratio of two sRGB colors
    def lum(rgb):
        lin = [c / 255 / 12.92 if c / 255 <= 0.04045 else ((c / 255 + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class BannerThemeTests(unittest.TestCase):
    def banner(self, cols=100, **env):
        # The banner in truecolor, `cols` wide, with only these theme variables set. At 40 columns it is the one-line
        # logo, which is also the header of every setup wizard screen.
        base = {k: v for k, v in os.environ.items() if k not in ("DEVKIT_THEME", "COLORFGBG")}
        with mock.patch.dict(os.environ, {**base, **env}, clear=True), mock.patch.object(ui, "TRUECOLOR", True), \
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


if __name__ == "__main__":
    unittest.main()
