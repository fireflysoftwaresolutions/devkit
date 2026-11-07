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
WHITE, BLACK = (255, 255, 255), (0, 0, 0)


def contrast(a, b) -> float:
    # WCAG 2 contrast ratio of two sRGB colors
    def lum(rgb):
        lin = [c / 255 / 12.92 if c / 255 <= 0.04045 else ((c / 255 + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class BannerThemeTests(unittest.TestCase):
    def banner(self, **env):
        # The banner in truecolor on a wide terminal, with only these theme variables set.
        base = {k: v for k, v in os.environ.items() if k not in ("DEVKIT_THEME", "COLORFGBG")}
        with mock.patch.dict(os.environ, {**base, **env}, clear=True), mock.patch.object(ui, "TRUECOLOR", True), \
                mock.patch.object(ui, "COLOR", True), mock.patch.object(ui, "width", return_value=100):
            return ui.banner("setup")

    def test_an_unknown_theme_keeps_every_banner_color_readable_on_white_and_black(self):
        out = self.banner()
        self.assertIn(ui.WORDMARK[2], ui.ANSI_RE.sub("", out))
        colors = {tuple(map(int, c)) for c in FG.findall(out)}
        self.assertTrue(colors, "the spark keeps its amber")
        for rgb in colors:
            self.assertGreaterEqual(round(contrast(rgb, WHITE), 2), 3, f"{rgb} on white")
            self.assertGreaterEqual(round(contrast(rgb, BLACK), 2), 3, f"{rgb} on black")


if __name__ == "__main__":
    unittest.main()
