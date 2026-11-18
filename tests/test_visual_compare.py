# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# lib/devkit/visual/compare.py compares a before and an after capture of one recipe: it renders compare.html, one
# self-contained page that carries pixelmatch 8.0.0 (lib/devkit/visual/pixelmatch.js, ISC, copied unmodified below
# its header) and the two pictures, runs it in the Chromium of the pinned Playwright, and returns the difference
# picture, the share of changed pixels inside the finding's box and outside it, and the changed regions outside the
# box as signals. NOTICE records the SHA-256 of the copied pixelmatch bytes.
#
# The fixture pictures (tests/fixtures/visual/compare/store/blobs, named by SHA-256) are drawn so each count follows
# from their geometry, at scale 1:
#   before, 60×40, white: a black heading at x 30, y 5, 20×4; a red (200,30,30) button at x 5, y 30, 20×6.
#   after,  60×44, white: the heading 10 px lower (y 15); the button blue (30,120,200); a (250,250,250) box at
#           x 2, y 2, 10×3, under pixelmatch's 0.1 threshold; and 4 more rows at the bottom, gray (207,207,207):
#           one of the eight colors pixelmatch puts behind a transparent pixel, so pixelmatch alone would count
#           only about 7 in 8 of them; a pixel only one picture has counts as changed.
# The box is the button, [5, 30, 20, 6]: 120 pixels, all changed. Outside it: the heading's old and new places,
# 80 + 80 pixels, and the 4 rows only the after picture has, 4×60 = 240 pixels, so 400 of the 2,640 − 120 = 2,520
# pixels outside the box changed. The faint box changes nothing.
#
# The browser tests run in the Chromium of the pinned Playwright (config/npm-globals.txt) and skip, saying what is
# missing, without it. Tests carry comments, not docstrings: `dev test prove` leaves out tests with a docstring.
# Run: python3 -m unittest tests.test_visual_compare
import base64
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
sys.path.insert(0, str(ROOT / "lib"))

from devkit.visual import compare  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "visual" / "compare"
PIXELMATCH = ROOT / "lib" / "devkit" / "visual" / "pixelmatch.js"
NOTICE = ROOT / "NOTICE"
UPSTREAM_SHA256 = "5b8583627704b4e1fe4fea472c283c8ca03d10ae4c94e5e029ea3b271a998ee6"  # index.js of pixelmatch@8.0.0
BEFORE, AFTER = "VC-g34c0b4f0re", "VC-g34c0aft3r"
BOX = [5, 30, 20, 6]
# an end tag for the data block, and `<!--<script ` that would make the parser read the block's own end tag as text
HOSTILE = {"before": "</script><script>alert(1)</script>", "after": "<!--<script x><img src=x onerror=alert(2)>"}


def _split_header(data: bytes):
    end = data.index(b"*/\n") + 3
    return data[:end], data[end:]


def _blob(name: str) -> bytes:
    rec = json.loads((FIXTURES / "records" / "captures" / f"{name}.json").read_text())
    return (FIXTURES / "store" / "blobs" / f"{rec['image']['sha256']}.png").read_bytes()


def _decode_png(data: bytes):
    # -> (width, height, rows of RGBA tuples); 8-bit RGB or RGBA, what Chromium writes
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    p, idat, width, height, kind = 8, [], 0, 0, 6
    while p < len(data):
        (n,), tag = struct.unpack(">I", data[p:p + 4]), data[p + 4:p + 8]
        body = data[p + 8:p + 8 + n]
        if tag == b"IHDR":
            width, height, depth, kind = struct.unpack(">IIBB", body[:10])
            assert depth == 8 and kind in (2, 6)
        elif tag == b"IDAT":
            idat.append(body)
        p += 12 + n
    bpp = 4 if kind == 6 else 3
    raw, stride, rows, prev = zlib.decompress(b"".join(idat)), width * bpp, [], bytearray(width * bpp)
    for y in range(height):
        f, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b, c = prev[i], prev[i - bpp] if i >= bpp else 0
            if f == 1:
                line[i] = (line[i] + a) & 255
            elif f == 2:
                line[i] = (line[i] + b) & 255
            elif f == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 255
            elif f == 4:
                q = a + b - c
                pa, pb, pc = abs(q - a), abs(q - b), abs(q - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append([tuple(line[x * bpp:x * bpp + bpp]) + ((255,) if bpp == 3 else ()) for x in range(width)])
        prev = line
    return width, height, rows


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags, self.text, self._in = [], {}, None

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag in ("script", "style"):
            self._in = (tag, dict(attrs).get("type") or dict(attrs).get("id") or "")
            self.text[self._in] = ""

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._in = None

    def handle_data(self, data):
        if self._in:
            self.text[self._in] += data


def _csp_hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode() + "'"


# ---------------------------------------------------------------- the copied pixelmatch and NOTICE (VD-AC-67, R35)

class PixelmatchAndNotice(unittest.TestCase):
    def test_vd_ac_67_the_bytes_after_the_header_are_pixelmatch_8_0_0_unmodified(self):
        _, body = _split_header(PIXELMATCH.read_bytes())
        self.assertEqual(hashlib.sha256(body).hexdigest(), UPSTREAM_SHA256)

    def test_vd_ac_67_notice_records_the_hash_of_the_copied_bytes(self):
        _, body = _split_header(PIXELMATCH.read_bytes())
        text = NOTICE.read_text()
        entry = text[text.index("lib/devkit/visual/pixelmatch.js"):]
        recorded = re.search(r"\b[0-9a-f]{64}\b", entry[:800])
        self.assertIsNotNone(recorded, "NOTICE names pixelmatch.js but records no SHA-256 after it")
        self.assertEqual(recorded.group(0), hashlib.sha256(body).hexdigest())

    def test_vd_ac_67_the_header_carries_pixelmatch_isc_notice(self):
        header, _ = _split_header(PIXELMATCH.read_bytes())
        text = header.decode("utf-8")
        self.assertTrue(text.startswith("/*\n"))
        self.assertEqual(text.count("\n"), 21)
        self.assertIn("Copyright (c) 2025, Mapbox", text)
        self.assertIn("Permission to use, copy, modify, and/or distribute this software", text)
        self.assertIn('THE SOFTWARE IS PROVIDED "AS IS"', text)


# ---------------------------------------------------------------- the page, as written (R72)

class ThePage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.job = compare.job(_blob(BEFORE), _blob(AFTER), BOX, 1, labels=HOSTILE)
        cls.page = compare.render_page(cls.job)
        cls.parsed = _Tags()
        cls.parsed.feed(cls.page)

    def test_the_job_stays_data_whatever_its_labels_hold(self):
        scripts = [a.get("type") for t, a in self.parsed.tags if t == "script"]
        self.assertEqual(scripts, ["application/json", "module"])
        self.assertNotIn("img", [t for t, _ in self.parsed.tags])
        block = self.parsed.text[("script", "application/json")]
        # Python's parser ends a script at the first `</script`; a browser also tracks `<!--` and `<script` in it, so
        # the block must hold no `<` at all
        self.assertNotIn("<", block)
        self.assertEqual(json.loads(block), self.job)

    def test_the_policy_allows_only_the_page_own_script_and_style(self):
        metas = [a for t, a in self.parsed.tags if t == "meta" and a.get("http-equiv") == "Content-Security-Policy"]
        self.assertEqual(len(metas), 1)
        policy = {d.split()[0]: d.split()[1:] for d in metas[0]["content"].split(";") if d.strip()}
        self.assertEqual(policy["default-src"], ["'none'"])
        self.assertEqual(policy["script-src"], [_csp_hash(self.parsed.text[("script", "module")])])
        self.assertEqual(policy["style-src"], [_csp_hash(self.parsed.text[("style", "")])])
        self.assertEqual(policy["base-uri"], ["'none'"])
        self.assertEqual(policy["form-action"], ["'none'"])
        self.assertFalse({"connect-src", "img-src", "frame-src", "worker-src"} & set(policy))

    def test_the_page_names_no_file_or_address_outside_itself(self):
        linked = [(t, k, v) for t, a in self.parsed.tags for k, v in a.items()
                  if k in ("src", "href", "srcset", "action", "poster", "data", "formaction")]
        self.assertEqual(linked, [])
        self.assertNotRegex(self.page, r"(?i)\bimport\s*\(|\bimport\s+[\w{*]|\bfetch\s*\(|XMLHttpRequest|new\s+Worker")

    def test_the_page_carries_pixelmatch_with_its_notice(self):
        script = self.parsed.text[("script", "module")]
        self.assertIn(PIXELMATCH.read_text(), script)


# ---------------------------------------------------------------- refusals, before any browser starts

class Refusals(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="visual-compare-"))
        shutil.copytree(FIXTURES, self.tmp, dirs_exist_ok=True)
        self.records, self.store = self.tmp / "records", self.tmp / "store"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _edit(self, name, change):
        path = self.records / "captures" / f"{name}.json"
        rec = json.loads(path.read_text())
        change(rec)
        path.write_text(json.dumps(rec))

    def test_captures_of_different_recipes_are_refused(self):
        self._edit(AFTER, lambda r: r["recipe"].update(viewport=[390, 844], theme="dark"))
        with self.assertRaises(compare.CompareError) as cm:
            compare.compare_captures(self.records, self.store, BEFORE, AFTER, BOX)
        self.assertIn("different recipes", str(cm.exception))
        self.assertIn("viewport, theme", str(cm.exception))

    def test_an_experiment_differs_from_its_recipe_only_by_its_css(self):
        self._edit(AFTER, lambda r: r["recipe"].update(css=".pay{color:blue}"))
        self.assertEqual(compare.recipe_difference(
            json.loads((self.records / "captures" / f"{BEFORE}.json").read_text()),
            json.loads((self.records / "captures" / f"{AFTER}.json").read_text())), [])

    def test_a_picture_whose_bytes_differ_from_its_record_is_refused(self):
        rec = json.loads((self.records / "captures" / f"{AFTER}.json").read_text())
        blob = self.store / "blobs" / f"{rec['image']['sha256']}.png"
        blob.write_bytes(blob.read_bytes() + b"\0")
        before = sorted(p.name for p in (self.store / "blobs").iterdir())
        with self.assertRaises(compare.CompareError) as cm:
            compare.compare_captures(self.records, self.store, BEFORE, AFTER, BOX)
        self.assertIn(f"The picture of {AFTER} in the store does not match its record", str(cm.exception))
        self.assertEqual(sorted(p.name for p in (self.store / "blobs").iterdir()), before)


# ---------------------------------------------------------------- in the browser

def _account_home() -> Path:
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
        npm_root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=60).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        npm_root = ""
    cli = Path(npm_root or "/nonexistent") / "@playwright" / "cli"
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

# Opens a written compare page from its file, logs every request, console message and dialog, and reads what a
# reader would: the state the page reached and its status line. With VISUAL_COMPARE_SHOTS set to a folder, it also
# saves a picture of the page at each width and theme.
DRIVER = r"""
const o = JSON.parse(process.argv[1]);
const {chromium} = require(o.core);
(async () => {
  const browser = await chromium.launch({channel: "chromium", headless: true, chromiumSandbox: true,
                                         args: ["--disable-breakpad", "--disable-crash-reporter"]});
  try {
    const out = {requests: [], console: [], dialogs: [], views: {}};
    for (const [w, h, theme] of [[1440, 900, "light"], [1440, 900, "dark"], [390, 844, "light"], [390, 844, "dark"]]) {
      const context = await browser.newContext({viewport: {width: w, height: h}, colorScheme: theme});
      const page = await context.newPage();
      page.on("request", r => out.requests.push(r.url()));
      page.on("console", m => out.console.push(m.type() + ": " + m.text()));
      page.on("pageerror", e => out.console.push("pageerror: " + e.message));
      page.on("dialog", d => { out.dialogs.push(d.message()); d.dismiss(); });
      await page.goto(o.url);
      await page.waitForSelector("html[data-state=done], html[data-state=error]", {timeout: 30000});
      out.views[w + " " + theme] = await page.evaluate(() => ({
        state: document.documentElement.dataset.state,
        status: document.getElementById("status").textContent,
        elements: document.querySelectorAll("body *").length,
        scrollWidth: document.documentElement.scrollWidth,
      }));
      if (o.shots) await page.screenshot({path: `${o.shots}/compare-${w}-${theme}.png`});
      await context.close();
    }
    process.stdout.write(JSON.stringify(out));
  } finally {
    await browser.close();
  }
})().catch(e => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
"""


@unittest.skipIf(PLAYWRIGHT is None, NO_PLAYWRIGHT)
class InTheBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="visual-compare-browser-"))
        shutil.copytree(FIXTURES, cls.tmp, dirs_exist_ok=True)
        env = {"PLAYWRIGHT_BROWSERS_PATH": PLAYWRIGHT["browsers"]}
        with mock.patch.dict(os.environ, env):
            # bands of 7 rows, so the 44 rows take seven passes and the band edges are covered
            cls.result = compare.compare_captures(cls.tmp / "records", cls.tmp / "store", BEFORE, AFTER, BOX,
                                                  core=PLAYWRIGHT["core"], band=7)
        page = cls.tmp / "page.html"
        page.write_text(compare.render_page(compare.job(_blob(BEFORE), _blob(AFTER), BOX, 1, labels=HOSTILE)))
        profile = cls.tmp / "profile"
        profile.mkdir(mode=0o700)
        opts = {"core": PLAYWRIGHT["core"], "url": page.as_uri(), "shots": os.environ.get("VISUAL_COMPARE_SHOTS")}
        out = subprocess.run([PLAYWRIGHT["node"], "-e", DRIVER, json.dumps(opts)], capture_output=True, text=True,
                             timeout=180, cwd=str(profile),
                             env=dict(os.environ, TMPDIR=str(profile), **env))
        cls.page_url = page.as_uri()
        cls.error = None if out.returncode == 0 else out.stderr.strip()[-2000:]
        cls.browser = json.loads(out.stdout) if out.returncode == 0 else None

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_counts_match_the_fixture_geometry(self):
        r = self.result
        self.assertEqual((r["pixels"], r["changed"]), (2640, 520))
        self.assertEqual(r["inside"], 1.0)
        self.assertAlmostEqual(r["outside"], 400 / 2520, places=12)

    def test_the_changed_regions_outside_the_box_are_the_added_rows_and_the_moved_heading(self):
        self.assertEqual(self.result["regions"], [{"box": [0, 40, 60, 4], "pixels": 240},
                                                  {"box": [30, 5, 20, 14], "pixels": 160}])
        self.assertEqual(self.result["regions_total"], 2)

    def test_vd_ac_21_each_region_outside_the_box_becomes_a_signal(self):
        self.assertEqual([(s["check"], s["pass"], s["anchor"]["region"], s["measured"]) for s in self.result["signals"]],
                         [("changed-region", "layout", [0, 40, 60, 4], "240 pixels changed"),
                          ("changed-region", "layout", [30, 5, 20, 14], "160 pixels changed")])

    def test_the_difference_picture_is_stored_and_red_where_pixels_changed(self):
        diff = self.result["diff"]
        path = self.tmp / "store" / "blobs" / f"{diff['sha256']}.png"
        data = path.read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), diff["sha256"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        width, height, rows = _decode_png(data)
        self.assertEqual((width, height, diff["width"], diff["height"]), (60, 44, 60, 44))
        red = (255, 0, 0, 255)
        for x, y in [(10, 32), (35, 6), (35, 16), (0, 42), (59, 43)]:  # button, heading's old and new place, new rows
            self.assertEqual(rows[y][x], red, (x, y))
        for x, y in [(4, 3), (55, 25), (2, 38)]:  # the faint box, and pixels that did not change: gray, never red
            r, g, b, _ = rows[y][x]
            self.assertTrue(r == g == b, (x, y, rows[y][x]))

    def test_an_anti_aliased_edge_that_sharpens_counts_as_unchanged(self):
        # edge-before.png and edge-after.png, 7×7: black at x 0-2, white at x 4-6; at x 3 a gray (128) column before
        # and white after. Each gray pixel has darker and brighter neighbors and at most two equal ones, and its
        # darkest neighbor has 3+ equal siblings in both pictures: pixelmatch calls it anti-aliasing, paints it
        # yellow, and does not count it. One row per pass: the rows around each band must give the same answer.
        with mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": PLAYWRIGHT["browsers"]}):
            r = compare.compare_pictures((FIXTURES / "edge-before.png").read_bytes(),
                                         (FIXTURES / "edge-after.png").read_bytes(), core=PLAYWRIGHT["core"], band=1)
        self.assertEqual((r["pixels"], r["changed"], r["regions"]), (49, 0, []))
        _, _, rows = _decode_png(r["diff"])
        self.assertEqual({rows[y][3] for y in range(7)}, {(255, 255, 0, 255)})

    def test_the_page_loads_nothing_outside_its_file_and_logs_no_error(self):
        if self.error:
            self.fail(self.error)
        self.assertEqual(sorted(set(self.browser["requests"])), [self.page_url])
        self.assertEqual(self.browser["console"], [])
        self.assertEqual(self.browser["dialogs"], [])

    def test_the_page_shows_its_result_with_the_labels_as_text(self):
        if self.error:
            self.fail(self.error)
        for view, v in self.browser["views"].items():
            with self.subTest(view=view):
                self.assertEqual(v["state"], "done")
                self.assertEqual(v["status"], f"Compared {HOSTILE['before']} with {HOSTILE['after']}: 520 of 2,640 "
                                              "pixels changed. Inside the box: 100%. Outside it: 15.9%, in 2 "
                                              "regions.")
                self.assertEqual(v["elements"], 5)  # main, h1, p, and the two scripts: no element from a label
                self.assertLessEqual(v["scrollWidth"], int(view.split()[0]))


if __name__ == "__main__":
    unittest.main()
