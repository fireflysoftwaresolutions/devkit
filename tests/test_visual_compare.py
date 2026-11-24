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
# The comparison also lists the elements that moved, resized, appeared, or disappeared (R36), from the two captures'
# descriptions (tests/fixtures/visual/compare/store/desc): before, html [0, 0, 60, 40], the heading h1 and the button
# #pay at the places above; after, html 4 px taller, the heading 10 px lower, the button where it was, and the faint
# box as #notice. So #notice appeared, h1 moved 0,10, and html resized from 60×40 to 60×44.
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

from devkit import identity, work  # noqa: E402
from devkit.visual import compare  # noqa: E402
from test_visual_map import presence  # noqa: E402

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


class PictureNotHere(unittest.TestCase):
    # A picture that is not in this Mac's store reads as every other command says it (store.not_here): gone from this
    # Mac's store when this Mac took the capture, else on the Mac that took it, named as the dashboard names it
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="visual-compare-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        shutil.copytree(FIXTURES, self.tmp, dirs_exist_ok=True)
        self.records, self.store = self.tmp / "records", self.tmp / "store"
        for p in (mock.patch.object(identity, "machine", lambda now=None: identity.Machine(
                      "m-test0001", "ana-macbook-pro", {"ana-macbook-pro": [[0, 4e9]]})),
                  mock.patch.object(work, "PRESENCE", self.tmp / "presence")):
            p.start()
            self.addCleanup(p.stop)
        presence(self.tmp / "presence", "bob@m-studio01", "studio")

    def test_a_missing_picture_names_this_macs_store_or_the_mac_that_took_it(self):
        path = self.records / "captures" / f"{BEFORE}.json"
        rec = json.loads(path.read_text())
        (self.store / "blobs" / f"{rec['image']['sha256']}.png").unlink()
        said = {}
        for where in ("m-test0001", "m-studio01"):
            rec["image"]["where"] = f"local:{where}"
            path.write_text(json.dumps(rec))
            with self.assertRaises(compare.CompareError) as cm:
                compare.compare_captures(self.records, self.store, BEFORE, AFTER, BOX)
            said[where] = str(cm.exception)
        again = f"Capture it here: dev visual capture --again {BEFORE}"
        self.assertEqual(said, {"m-test0001": f"The picture of {BEFORE} is no longer in this Mac's store. {again}",
                                "m-studio01": f"The picture of {BEFORE} is not on this Mac: it was taken on another Mac "
                                              f"(m-studio01). {again}"})


# ---------------------------------------------------------------- the elements that changed (R36)

def el(n, role, box, selector=None, locator=None, parent="E1", frame=None):
    # one element of a description, with the fields the comparison reads
    return {"id": f"E{n}", "parent": parent, "frame": frame, "role": role, "name": None, "text": None, "states": {},
            "box": box, "locator": locator, "selector": selector}


def described(*elements, truncated=False):
    return {"schema": "devkit.visual.description/1", "kind": "web", "truncated": truncated, "elements": list(elements)}


def changes(found):
    # -> (change, key, offset, size) of each listed element
    return [(c["change"], c["key"], c["offset"], c["size"]) for c in found["changes"]]


# move-before.html and move-after.html of the acceptance fixtures as the capture describes them at 390 px: #total
# is 20 px lower, #note is gone, and the page 45 px shorter
MOVE_BEFORE = described(el(1, "generic", [0, 0, 390, 224], "html", parent=None),
                        el(2, "heading", [16, 40, 358, 54], "h1", "getByRole('heading', { name: 'Totals' })"),
                        el(3, "paragraph", [16, 118, 358, 27], "#total", "locator('#total')"),
                        el(4, "paragraph", [16, 163, 358, 27], "#note", "locator('#note')"))
MOVE_AFTER = described(el(1, "generic", [0, 0, 390, 179], "html", parent=None),
                       el(2, "heading", [16, 40, 358, 54], "h1", "getByRole('heading', { name: 'Totals' })"),
                       el(3, "paragraph", [16, 138, 358, 27], "#total", "locator('#total')"))


class ElementChanges(unittest.TestCase):
    def test_vd_ac_59_the_moved_element_has_its_offset_and_the_removed_one_its_selector(self):
        found = compare.element_changes(MOVE_BEFORE, MOVE_AFTER)
        self.assertEqual(found["changes"], [
            {"change": "disappeared", "by": "selector", "key": "#note", "role": "paragraph",
             "before": {"element": "E4", "box": [16, 163, 358, 27]}, "after": None, "offset": None, "size": None},
            {"change": "moved", "by": "selector", "key": "#total", "role": "paragraph",
             "before": {"element": "E3", "box": [16, 118, 358, 27]},
             "after": {"element": "E3", "box": [16, 138, 358, 27]}, "offset": [0, 20], "size": [0, 0]},
            {"change": "resized", "by": "selector", "key": "html", "role": "generic",
             "before": {"element": "E1", "box": [0, 0, 390, 224]},
             "after": {"element": "E1", "box": [0, 0, 390, 179]}, "offset": [0, 0], "size": [0, -45]}])
        self.assertEqual({k: found[k] for k in ("compared", "why", "counts", "matched", "truncated")},
                         {"compared": True, "why": None, "matched": 3, "truncated": False,
                          "counts": {"disappeared": 1, "appeared": 0, "moved": 1, "resized": 1}})

    def test_an_element_without_its_selector_is_matched_by_its_locator(self):
        # after the change, no short selector singles the note out any more; its locator still does
        note = "getByText('Paid by bank transfer.')"
        found = compare.element_changes(described(el(4, "paragraph", [16, 163, 358, 27], "#note", note)),
                                        described(el(7, "paragraph", [16, 118, 358, 27], None, note)))
        self.assertEqual([(c["change"], c["by"], c["key"], c["offset"]) for c in found["changes"]],
                         [("moved", "locator", note, [0, -45])])

    def test_the_same_selector_on_another_role_is_another_element(self):
        found = compare.element_changes(described(el(2, "paragraph", [16, 40, 200, 20], "#status")),
                                        described(el(2, "button", [16, 40, 200, 20], "#status")))
        self.assertEqual(changes(found), [("disappeared", "#status", None, None), ("appeared", "#status", None, None)])

    def test_an_element_that_moves_with_its_parent_is_not_listed_and_one_that_moves_within_it_is(self):
        # the card moves 30 px down: its title goes with it, its price 10 px further
        before = described(el(2, "region", [0, 100, 390, 200], "#card"),
                           el(3, "heading", [16, 116, 200, 30], "#card h2", parent="E2"),
                           el(4, "paragraph", [16, 160, 200, 20], "#price", parent="E2"))
        after = described(el(2, "region", [0, 130, 390, 200], "#card"),
                          el(3, "heading", [16, 146, 200, 30], "#card h2", parent="E2"),
                          el(4, "paragraph", [16, 200, 200, 20], "#price", parent="E2"))
        self.assertEqual(changes(compare.element_changes(before, after)),
                         [("moved", "#card", [0, 30], [0, 0]), ("moved", "#price", [0, 40], [0, 0])])

    def test_the_elements_inside_a_removed_or_an_added_element_are_not_listed_again(self):
        before = described(el(2, "dialog", [20, 100, 350, 200], "#confirm"),
                           el(3, "button", [40, 250, 100, 32], "#confirm-ok", parent="E2"))
        after = described(el(2, "banner", [0, 0, 390, 60], "#offer"),
                          el(3, "link", [16, 20, 120, 20], "#offer a", parent="E2"))
        self.assertEqual(changes(compare.element_changes(before, after)),
                         [("disappeared", "#confirm", None, None), ("appeared", "#offer", None, None)])

    def test_a_change_of_1_px_is_the_rounding_of_the_boxes(self):
        found = compare.element_changes(described(el(2, "button", [16, 40, 100, 32], "#save")),
                                        described(el(2, "button", [17, 41, 101, 31], "#save")))
        self.assertEqual((found["changes"], found["matched"]), ([], 1))

    def test_an_element_its_selector_does_not_single_out_in_a_frame_is_keyed_with_the_frame(self):
        # #ok on the page and #ok in the first frame are two elements; the one in the frame moved
        before = described(el(2, "button", [16, 40, 80, 32], "#ok"), el(5, "button", [16, 400, 80, 32], "#ok", frame=1))
        after = described(el(2, "button", [16, 40, 80, 32], "#ok"), el(5, "button", [16, 430, 80, 32], "#ok", frame=1))
        self.assertEqual([(c["change"], c["key"], c["after"]["element"]) for c in
                          compare.element_changes(before, after)["changes"]], [("moved", "#ok", "E5")])

    def test_an_element_whose_key_two_elements_share_is_not_compared(self):
        # two Save buttons without a selector share one locator: which one moved cannot be told
        save = "getByRole('button', { name: 'Save' })"
        before = described(el(2, "button", [16, 40, 80, 32], None, save),
                           el(3, "button", [16, 400, 80, 32], None, save))
        after = described(el(2, "button", [16, 40, 80, 32], None, save), el(3, "button", [16, 450, 80, 32], None, save))
        self.assertEqual(compare.element_changes(before, after)["changes"], [])

    def test_a_description_cut_at_its_limit_lists_nothing_as_gone_from_it(self):
        # the after description stops before the footer: the footer is not listed as disappeared; the banner, which
        # the whole before description lacks, did appear
        before = described(el(2, "contentinfo", [0, 2000, 390, 80], "footer"))
        after = described(el(3, "banner", [0, 0, 390, 60], "#offer"), truncated=True)
        found = compare.element_changes(before, after)
        self.assertEqual((changes(found), found["truncated"]), ([("appeared", "#offer", None, None)], True))

    def test_at_most_20_changes_are_listed_from_the_top_and_the_counts_hold_them_all(self):
        rows = range(25)
        before = described(*[el(n + 2, "listitem", [16, 100 + 40 * n, 300, 32], f"#row{n}") for n in rows])
        after = described(*[el(n + 2, "listitem", [16, 104 + 40 * n + 4 * n, 300, 32], f"#row{n}") for n in rows])
        found = compare.element_changes(before, after)
        self.assertEqual([c["key"] for c in found["changes"]], [f"#row{n}" for n in range(20)])
        self.assertEqual(found["counts"], {"disappeared": 0, "appeared": 0, "moved": 25, "resized": 0})


class ElementLines(unittest.TestCase):
    def test_each_change_is_a_line_with_its_selector_and_its_numbers(self):
        self.assertEqual(compare.element_lines(compare.element_changes(MOVE_BEFORE, MOVE_AFTER)), [
            "disappeared  #note  was at 16,163, 358×27 px (E4)",
            "moved        #total  by 0,20 px (E3 → E3)",
            "resized      html  from 390×224 to 390×179 px (E1 → E1)"])
        both = compare.element_changes(described(el(2, "region", [0, 100, 300, 100], ".card")),
                                       described(el(2, "region", [0, 120, 300, 140], ".card")))
        added = compare.element_changes(described(), described(el(2, "generic", [2, 2, 10, 3], "#notice")))
        self.assertEqual(compare.element_lines(both) + compare.element_lines(added), [
            "moved        .card  by 0,20 px, resized from 300×100 to 300×140 px (E2 → E2)",
            "appeared     #notice  at 2,2, 10×3 px (E2)"])

    def test_the_lines_say_when_nothing_changed_more_changed_or_nothing_was_compared(self):
        same = compare.element_changes(MOVE_AFTER, MOVE_AFTER)
        self.assertEqual(compare.element_lines(same),
                         ["elements: none moved, resized, appeared, or disappeared (3 found in both by selector or "
                          "locator)"])
        rows = range(23)
        many = compare.element_changes(
            described(*[el(n + 2, "listitem", [16, 100 + 40 * n, 300, 32], f"#row{n}") for n in rows]),
            described(*[el(n + 2, "listitem", [16, 140 + 40 * n, 300, 32], f"#row{n}") for n in rows], truncated=True))
        self.assertEqual(compare.element_lines(many)[20:], [
            "and 3 more changed elements",
            "a description was cut at its element limit: elements past it are not listed as appeared or disappeared"])
        missing = {"compared": False, "why": "the description of VC-g34c0aft3r is not on this Mac", "changes": [],
                   "counts": {"disappeared": 0, "appeared": 0, "moved": 0, "resized": 0}, "matched": 0,
                   "truncated": False}
        self.assertEqual(compare.element_lines(missing),
                         ["elements not compared: the description of VC-g34c0aft3r is not on this Mac"])


class ElementsFromTheStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="visual-compare-elements-"))
        shutil.copytree(FIXTURES, self.tmp, dirs_exist_ok=True)
        self.store = self.tmp / "store"
        self.recs = [json.loads((self.tmp / "records" / "captures" / f"{c}.json").read_text()) for c in (BEFORE, AFTER)]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_descriptions_are_read_from_the_store_and_checked_against_their_records(self):
        found = compare.capture_elements(self.store, *self.recs)
        self.assertEqual(changes(found), [("appeared", "#notice", None, None), ("moved", "h1", [0, 10], [0, 0]),
                                          ("resized", "html", [0, 0], [0, 4])])

    def test_a_description_missing_or_changed_leaves_the_elements_uncompared_and_says_why(self):
        path = self.store / "desc" / f"{self.recs[1]['description']['sha256']}.json.gz"
        path.write_bytes(path.read_bytes() + b"\0")
        changed = compare.capture_elements(self.store, *self.recs)
        path.unlink()
        missing = compare.capture_elements(self.store, *self.recs)
        self.assertEqual([(f["compared"], f["why"], f["changes"]) for f in (changed, missing)], [
            (False, f"the description of {AFTER} in the store does not match its record", []),
            (False, f"the description of {AFTER} is not on this Mac", [])])


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
        page.write_text(compare.render_page(compare.job(_blob(BEFORE), _blob(AFTER), BOX, 1, labels=HOSTILE,
                                                        elements=cls.result["elements"]["counts"])))
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

    def test_the_result_lists_the_changed_elements_beside_the_pixels(self):
        self.assertEqual(changes(self.result["elements"]), [("appeared", "#notice", None, None),
                                                            ("moved", "h1", [0, 10], [0, 0]),
                                                            ("resized", "html", [0, 0], [0, 4])])

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
                                              "regions. Elements: 1 appeared, 1 moved, 1 resized.")
                self.assertEqual(v["elements"], 5)  # main, h1, p, and the two scripts: no element from a label
                self.assertLessEqual(v["scrollWidth"], int(view.split()[0]))


def _status_of(test, page_job):
    # -> the views {"<width> <theme>": {state, status, …}} the driver reads from a page made of this job
    tmp = Path(tempfile.mkdtemp(prefix="visual-compare-copy-"))
    test.addCleanup(shutil.rmtree, tmp, True)
    page = tmp / "page.html"
    page.write_text(compare.render_page(page_job))
    profile = tmp / "profile"
    profile.mkdir(mode=0o700)
    out = subprocess.run([PLAYWRIGHT["node"], "-e", DRIVER, json.dumps({"core": PLAYWRIGHT["core"], "url": page.as_uri()})],
                         capture_output=True, text=True, timeout=180, cwd=str(profile),
                         env=dict(os.environ, TMPDIR=str(profile), PLAYWRIGHT_BROWSERS_PATH=PLAYWRIGHT["browsers"]))
    if out.returncode != 0:
        test.fail(out.stderr.strip()[-2000:])
    result = json.loads(out.stdout)
    test.assertEqual(result["console"], [])
    return result["views"]


@unittest.skipIf(PLAYWRIGHT is None, NO_PLAYWRIGHT)
class TheStatusLine(unittest.TestCase):
    # what the page says when devkit wrote a job it cannot read, and when no pixel changed but elements moved

    def test_a_job_devkit_wrote_wrong_names_its_fields_and_says_to_report_it(self):
        for fields, said in ((("threshold",), "the job's threshold is not valid"),
                             (("scale", "threshold"), "the job's scale and threshold are not valid")):
            broken = compare.job(_blob(BEFORE), _blob(AFTER), BOX, 1)
            for field in fields:
                broken[field] = 99
            with self.subTest(fields):
                for view, v in _status_of(self, broken).items():
                    self.assertEqual((view, v["state"], v["status"]),
                                     (view, "error", f"Could not compare the pictures: {said}. This is a devkit bug: "
                                                     "report it with dev bug report."))

    def test_no_pixel_changed_but_elements_moved_reads_as_one_sentence(self):
        same = compare.job(_blob(BEFORE), _blob(BEFORE), BOX, 1, labels={"before": "VC-a", "after": "VC-b"},
                           elements={"disappeared": 0, "appeared": 0, "moved": 2, "resized": 1})
        for view, v in _status_of(self, same).items():
            self.assertEqual((view, v["state"], v["status"]),
                             (view, "done", "Compared VC-a with VC-b: no pixel changed, but 2 elements moved and "
                                            "1 resized."))


if __name__ == "__main__":
    unittest.main()
