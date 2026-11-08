# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The setup wizard's colors (lib/devkit/tui.py) on a terminal whose theme devkit cannot tell: macOS Terminal sets no
# COLORFGBG, so focus and the primary action must read on a light and on a dark background alike. The tests clear
# the theme variables and render prompts to strings; they never read or write the terminal.
# Run: python3 -m unittest tests.test_tui
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import tui  # noqa: E402

SGR = re.compile(r"\x1b\[([0-9;]*)m([^\x1b]*)")
WHITE, BLACK = (255, 255, 255), (0, 0, 0)
CUBE = (0, 95, 135, 175, 215, 255)  # the xterm 256-color cube's levels


def contrast(a, b) -> float:
    # WCAG 2 contrast ratio of two sRGB colors
    def lum(rgb):
        lin = [c / 255 / 12.92 if c / 255 <= 0.04045 else ((c / 255 + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def foreground(params: str):
    # The RGB of an SGR's 24-bit or 256-color foreground (cube and gray ramp), None when it sets none
    p = params.split(";")
    for i, v in enumerate(p):
        if v == "38" and p[i + 1:i + 2] == ["2"]:
            return tuple(int(x) for x in p[i + 2:i + 5])
        if v == "38" and p[i + 1:i + 2] == ["5"]:
            n = int(p[i + 2])
            if n >= 232:
                return (8 + 10 * (n - 232),) * 3
            n -= 16
            return CUBE[n // 36], CUBE[n // 6 % 6], CUBE[n % 6]
    return None


def colored_runs(text: str) -> list:
    return [(rgb, run) for params, run in SGR.findall(text) for rgb in [foreground(params)] if rgb and run]


class UnknownThemeTests(unittest.TestCase):
    def wizard(self, truecolor: bool) -> str:
        # A focused list, a yes/no question, a text field and the step bar, drawn with no theme variable set.
        env = {k: v for k, v in os.environ.items() if k not in ("DEVKIT_THEME", "COLORFGBG")}
        with mock.patch.dict(os.environ, env, clear=True):
            tui.set_color(True, truecolor)
            try:
                select = tui.Select("Stacks", [tui.Option("python", "Python", desc="uv, pyright"),
                                               tui.Option("node", "Node")])
                return "\n".join(select.lines(80) + tui.Confirm("Install now?").lines(80)
                                 + tui.Text("Handoff repository").lines(80) + tui.step_header(3, 8, "Agents", 80))
            finally:
                tui.set_color()

    def test_focus_and_actions_read_on_white_and_on_black(self):
        for truecolor in (True, False):
            with self.subTest(truecolor=truecolor):
                runs = colored_runs(self.wizard(truecolor))
                self.assertTrue(any(tui.POINTER in run for _, run in runs), "the focused option keeps a colored pointer")
                for rgb, run in runs:
                    floor = 4.5 if re.search(r"\w", run) else 3
                    for bg, name in ((WHITE, "white"), (BLACK, "black")):
                        self.assertGreaterEqual(round(contrast(rgb, bg), 2), floor, f"{run.strip()!r} {rgb} on {name}")


if __name__ == "__main__":
    unittest.main()
