# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# tools/dashboard_shots.py takes the dashboard's pictures for the docs. A full Chrome lays a page out 500 px wide at
# least, whatever its window size, so a 390 px `chrome --screenshot` was a 500 px page cropped to 390. The tool sets the
# viewport itself and stops when the page's innerWidth is not the width asked for. Tests carry comments, not
# docstrings: `dev test prove` leaves out tests with a docstring. Run: python3 -m unittest tests.test_dashboard_shots
import os
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import dashboard_shots as DS  # noqa: E402
from tests import test_dashboard as T  # noqa: E402

# a white page with a red square pinned to its right edge: in a true 390 px layout it is in the picture's top right
EDGE = ('<!doctype html><html><head><meta charset="utf-8"><style>html, body { margin: 0; background: #fff }'
        ' #edge { position: fixed; top: 0; right: 0; width: 24px; height: 24px; background: #f00 }</style></head>'
        '<body><div id="edge"></div></body></html>')


def png_pixels(png: bytes):
    """-> (width, height, pixel(x, y) -> (r, g, b)) of an 8-bit RGB or RGBA PNG."""
    pos, idat, w, h, ctype = 8, b"", 0, 0, 0
    while pos < len(png):
        n, kind = struct.unpack(">I4s", png[pos:pos + 8])
        if kind == b"IHDR":
            w, h, _, ctype = struct.unpack(">IIBB", png[pos + 8:pos + 18])
        elif kind == b"IDAT":
            idat += png[pos + 8:pos + 8 + n]
        pos += 12 + n
    bpp = 4 if ctype == 6 else 3
    raw, stride, prev, rows = zlib.decompress(idat), w * bpp, bytearray(w * bpp), []
    for y in range(h):
        kind, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a, b, c = (line[i - bpp] if i >= bpp else 0), prev[i], (prev[i - bpp] if i >= bpp else 0)
            p = a + b - c
            paeth = a if abs(p - a) <= abs(p - b) and abs(p - a) <= abs(p - c) else b if abs(p - b) <= abs(p - c) else c
            line[i] = (line[i] + (0, a, b, (a + b) // 2, paeth)[kind]) & 255
        rows.append(bytes(line))
        prev = line
    return w, h, lambda x, y: tuple(rows[y][x * bpp:x * bpp + 3])


@unittest.skipUnless(T.CHROME, T.NO_CHROME)
class TrueViewport(unittest.TestCase):
    # a 390 px shot is a 390 px layout: the picture is 390 by the height asked, and the square pinned to the page's
    # right edge is in its top right corner (in a 500 px layout cropped to 390 that corner is white)
    def test_a_narrow_shot_lays_the_page_out_at_that_width(self):
        with tempfile.TemporaryDirectory() as t:
            page, out = Path(t) / "edge.html", Path(t) / "edge-390-light.png"
            page.write_text(EDGE, encoding="utf-8")
            DS.shoot(page.as_uri(), out, 390, 300, dict(os.environ))
            w, h, px = png_pixels(out.read_bytes())
        self.assertEqual((w, h), (390, 300))
        r, g, b = px(390 - 12, 12)
        self.assertTrue(r > 200 and g < 60 and b < 60, f"the top right corner is {(r, g, b)}, not the red square")


@unittest.skipUnless(T.CHROME, T.NO_CHROME)
class PageBuiltElsewhere(unittest.TestCase):
    # --page shoots a page built elsewhere (the docs' demo workspace): one picture per route, width and theme, named
    # <route>-<width>-<theme>.png, each drawn in its theme from the first paint
    def test_page_option_shoots_each_route_width_and_theme(self):
        themed = EDGE.replace("</style>", ' html[data-theme="dark"], html[data-theme="dark"] body { background: #000 }</style>')
        with tempfile.TemporaryDirectory() as t:
            page, out = Path(t) / "demo.html", Path(t) / "shots"
            page.write_text(themed, encoding="utf-8")
            DS.main([str(out), "--page", str(page), "--route", "edge=#/x", "--width", "390", "--theme", "dark", "light",
                     "--height", "300"])
            shots = sorted(f.name for f in out.iterdir())
            dark = png_pixels((out / "edge-390-dark.png").read_bytes())
            light = png_pixels((out / "edge-390-light.png").read_bytes())
        self.assertEqual(shots, ["edge-390-dark.png", "edge-390-light.png"])
        self.assertEqual((dark[0], dark[1], dark[2](20, 150), light[2](20, 150)), (390, 300, (0, 0, 0), (255, 255, 255)))


class WidthCheck(unittest.TestCase):
    # a page laid out at another width than the one asked for stops the run, naming the picture and both widths
    def test_a_page_of_another_width_stops_the_run(self):
        with self.assertRaises(SystemExit) as stop:
            DS.check_width(500, 390, Path("home-390-dark.png"))
        self.assertIn("home-390-dark.png", str(stop.exception))
        self.assertIn("500 px wide, not 390", str(stop.exception))
        DS.check_width(390, 390, Path("home-390-dark.png"))


if __name__ == "__main__":
    unittest.main()
