# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The brand renderer (tools/brand/render.py) sets the hero's text in Manrope, rasterized by rsvg-convert. When the
# renderer cannot see the font it silently draws Helvetica, Times or another fallback, and the banners ship wrong. A
# line set in Manrope must not come out as wide as the same line in a fallback family, whatever HOME and Pango backend
# the caller's environment has: the suite runs with a temporary HOME (tests/_hermetic.py), where ~/Library/Fonts is
# empty. Tests carry comments, not docstrings: `dev test prove` leaves out tests with a docstring.
# Run: python3 -m unittest tests.test_brand_render
import os
import pwd
import shutil
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "brand"))
import render  # noqa: E402

LINE = "Every repo in its place. Every agent on the same page."
FALLBACKS = ("Helvetica", "Times", "No Such Sans, sans-serif")


def manrope_installed() -> bool:
    homes = {Path(pwd.getpwuid(os.getuid()).pw_dir), Path.home()}
    dirs = [Path("/System/Library/Fonts"), Path("/Library/Fonts")] + [h / "Library" / "Fonts" for h in homes]
    return any(d.is_dir() and any(d.glob("Manrope*")) for d in dirs)


def ink_right_edge(png: bytes) -> int:
    """The x of the rightmost dark pixel of an 8-bit RGB or RGBA PNG, -1 when there is none."""
    pos, idat, w, h, ctype = 8, b"", 0, 0, 0
    while pos < len(png):
        n, kind = struct.unpack(">I4s", png[pos:pos + 8])
        if kind == b"IHDR":
            w, h, _, ctype = struct.unpack(">IIBB", png[pos + 8:pos + 18])
        elif kind == b"IDAT":
            idat += png[pos + 8:pos + 8 + n]
        pos += 12 + n
    bpp = 4 if ctype == 6 else 3
    raw, stride, prev, right = zlib.decompress(idat), w * bpp, bytearray(w * bpp), -1
    for y in range(h):
        kind, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a, b, c = (line[i - bpp] if i >= bpp else 0), prev[i], (prev[i - bpp] if i >= bpp else 0)
            p = a + b - c
            paeth = a if abs(p - a) <= abs(p - b) and abs(p - a) <= abs(p - c) else b if abs(p - b) <= abs(p - c) else c
            line[i] = (line[i] + (0, a, b, (a + b) // 2, paeth)[kind]) & 255
        right = max([right] + [x for x in range(w) if line[x * bpp] < 128])
        prev = line
    return right


class Backend:
    # the Pango backend the caller's environment chooses (None: Pango's default, CoreText on macOS)
    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.saved = os.environ.get("PANGOCAIRO_BACKEND")
        os.environ.pop("PANGOCAIRO_BACKEND", None)
        if self.name:
            os.environ["PANGOCAIRO_BACKEND"] = self.name

    def __exit__(self, *exc):
        os.environ.pop("PANGOCAIRO_BACKEND", None)
        if self.saved is not None:
            os.environ["PANGOCAIRO_BACKEND"] = self.saved


@unittest.skipUnless(shutil.which("rsvg-convert") and manrope_installed(),
                     "needs rsvg-convert (brew install librsvg) and Manrope (brew install --cask font-manrope)")
class ManropeIsFound(unittest.TestCase):
    def width(self, family: str, weight: int) -> int:
        with tempfile.TemporaryDirectory() as t:
            svg, png = Path(t) / "line.svg", Path(t) / "line.png"
            svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="1400" height="90"><rect width="1400" height="90" '
                           f'fill="#fff"/><text x="0" y="66" font-family="{family}" font-size="48" font-weight="{weight}">'
                           f"{LINE}</text></svg>")
            render.run("rsvg-convert", "-w", 1400, "-h", 90, svg, "-o", png)
            return ink_right_edge(png.read_bytes())

    # the hero's tagline (400) and byline (650) in Manrope, under the suite's temporary HOME, with Pango's default
    # backend and with fontconfig: never as wide as the same line in a fallback family
    def test_a_manrope_line_is_not_set_in_a_fallback_font(self):
        fell = {}
        for backend in (None, "fc"):
            with Backend(backend):
                for weight in (400, 650):
                    got = {f: self.width(f, weight) for f in ("Manrope",) + FALLBACKS}
                    same = [f for f in FALLBACKS if got[f] == got["Manrope"]]
                    if same:
                        fell[f"{backend or 'default'} backend, weight {weight}"] = (same, got)
        self.assertEqual(fell, {})


@unittest.skipUnless(shutil.which("rsvg-convert") and shutil.which("fc-match"), "needs rsvg-convert and fontconfig")
class MissingFont(unittest.TestCase):
    # a brand family fontconfig cannot find stops the render with the font's name and how to install it, before any
    # picture is drawn in a fallback
    def test_render_stops_when_a_brand_font_is_missing(self):
        with self.assertRaises(SystemExit) as stop:
            render.check_fonts(("No Such Sans",))
        self.assertIn("No Such Sans", str(stop.exception))
        self.assertIn("brew install", str(stop.exception))


if __name__ == "__main__":
    unittest.main()
