# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The comparison of two captures of one recipe: `dev visual compare`, and the one `dev visual verify` makes.

compare.html compares the pictures in the Chromium of the pinned Playwright (R35): pixelmatch 8.0.0 (pixelmatch.js,
ISC, copied unmodified below its notice; NOTICE records the copied bytes' SHA-256) runs on canvas ImageData with
anti-aliasing detection on (`includeAA: false`) and threshold 0.1, with no pngjs. `render_page` writes one file that
carries pixelmatch, the page's own code and the job, and loads nothing (R72): the job is a JSON data block whose `<`,
`>` and `&` are escaped, so no string in it can end the block or open a tag, and the policy in its `meta` tag allows
only the page's own script and style, by hash, and nothing that connects, submits, frames, or changes the base URL.
The page decodes the pictures from their bytes, so it requests no picture either.

The result is what the report's Before / After / Difference view and a finding's `verification` read: the
difference picture (pixelmatch's: the before picture faded to gray, changed pixels red, anti-aliased ones yellow),
the share of changed pixels inside the finding's box and outside it, and the changed regions outside the box, which
become signals on the after capture. Pictures of different sizes are compared over the larger one: a pixel that only
one picture has counts as changed.

The comparison of two captures also lists the elements that moved, resized, appeared, or disappeared (R36), from the
captures' descriptions: an element is matched by its selector, then by its locator, each with its frame, when that key
singles out one element on both sides and both have the same role. Boxes are whole CSS px, so a change of 1 px is the
rounding of the boxes and no change. An element that moved with its nearest matched ancestor, by the same offset, is not
listed for the move, and neither is an element inside one that appeared or disappeared; an element no key singles
out cannot be told apart and is never listed. A description cut at its element limit lists nothing as gone from it.

Interface:

  job(before, after, anchor, scale, labels=None, band=None, elements=None) -> dict
      the page's input; `elements` holds the counts of changed elements, when the descriptions were compared
  render_page(job) -> str                                                the page, ready to open
  compare_pictures(before, after, anchor=None, scale=1, ...) -> dict     two PNGs; the result with `diff` as PNG bytes
  compare_captures(records, store, before_id, after_id, anchor=None, ...) -> dict
      two capture records: the same recipe, pictures checked against their SHA-256; writes the difference picture
      into the store's blobs and returns it as {sha256, width, height, scale}, with the region signals and `elements`
  element_changes(before, after) -> dict      two descriptions: {compared, why, changes, counts, matched, truncated}
  capture_elements(store, before, after) -> dict      the same from two capture records' descriptions in the store
  element_lines(elements) -> [str]                       the element changes, as `dev visual compare` prints them
  recipe_difference(a, b) -> [field]                                     what keeps two captures from one recipe
  box_scale(record) -> number                    picture pixels per unit of its boxes: the dpr, 1 for a document
  region_signals(regions) -> [Signal]                                    `changed-region` signals, not numbered
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import tempfile
import zlib
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE / "compare.html"
PIXELMATCH = HERE / "pixelmatch.js"

ID = re.compile(r"^VC-[0-9a-z]{4,40}$")
SHA = re.compile(r"^[0-9a-f]{64}$")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
THRESHOLD = 0.1  # R35
BAND = 1024  # rows per pass: three bands of a picture 2,880 px wide hold about 36 MB
CELL = 8  # CSS px: changed pixels closer than about two cells apart join one region
MAX_REGIONS = 20
MAX_SIDE = 65535  # what the pinned Chromium's canvas holds; R74 bounds a picture to 16,384 CSS px high
MAX_AREA = 268_435_456
TIMEOUT = 120  # seconds for one comparison, the browser's start included
MAX_ELEMENTS = 20  # changed elements listed; `counts` counts them all
ROUNDING = 1  # px: a description's boxes are rounded to whole CSS px
CHANGES = ("disappeared", "appeared", "moved", "resized")  # the order the changes are listed in
KEYS = ("selector", "locator")  # what matches an element, in this order
DESCRIPTION = "devkit.visual.description/1"

SLOT = re.compile(r"\{\{(policy|job|pixelmatch)\}\}")
STYLE = re.compile(r"<style>(.*?)</style>", re.S)
MODULE = re.compile(r'<script type="module">(.*?)</script>', re.S)
UNSAFE_IN_SCRIPT = re.compile(r"(?i)</script|<!--|<script")
NO_BROWSER = ("No browser to compare with. devkit uses the Chromium of the pinned Playwright CLI "
              "(config/npm-globals.txt): run dev install --only tools.")


class CompareError(Exception):
    """A comparison that did not run: the message says why and what to do."""


# --------------------------------------------------------------------------- the page

def _png_size(data: bytes) -> tuple[int, int]:
    if not isinstance(data, (bytes, bytearray)) or data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR":
        raise CompareError("A picture to compare is not a PNG file.")
    return struct.unpack(">II", data[16:24])


def _boxes(anchor) -> list:
    if anchor is None:
        return []
    boxes = [anchor] if anchor and isinstance(anchor[0], (int, float)) else list(anchor)
    out = []
    for box in boxes:
        if not (isinstance(box, (list, tuple)) and len(box) == 4
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in box)
                and box[2] > 0 and box[3] > 0):
            raise CompareError(f"The box {box!r} is not x, y, width, height with a width and height above 0.")
        out.append([float(v) if isinstance(v, float) else int(v) for v in box])
    return out


def job(before: bytes, after: bytes, anchor, scale, labels=None, band=None, elements=None) -> dict:
    """The page's input: the two pictures, the finding's box (one [x, y, w, h] in CSS px, or several), the scale, and
    the counts of changed elements ({moved, …}) when the descriptions were compared."""
    for data in (before, after):
        w, h = _png_size(data)
        if not (0 < w <= MAX_SIDE and 0 < h <= MAX_SIDE and w * h <= MAX_AREA):
            raise CompareError(f"A picture to compare is {w}×{h} px; the comparison reads pictures of at most "
                               f"{MAX_SIDE:,} px a side and {MAX_AREA:,} pixels.")
    if not (isinstance(scale, (int, float)) and not isinstance(scale, bool) and 0 < scale <= 8):
        raise CompareError(f"The scale {scale!r} is not a device pixel ratio between 0 and 8.")
    if labels is not None:
        labels = {"before": str(labels["before"]), "after": str(labels["after"])}
    if elements is not None:
        elements = {k: max(0, int(elements.get(k) or 0)) for k in CHANGES}
    return {"before": base64.b64encode(before).decode("ascii"), "after": base64.b64encode(after).decode("ascii"),
            "anchor": _boxes(anchor), "scale": scale, "threshold": THRESHOLD, "band": int(band or BAND),
            "cell": max(1, round(CELL * scale)), "max_regions": MAX_REGIONS, "labels": labels, "elements": elements}


def _json_block(value) -> str:
    # JSON that stays inside <script type="application/json">: `<`, `>` and `&` only occur in strings, where the
    # escapes mean the same characters; ensure_ascii also escapes U+2028 and U+2029
    text = json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii") + "'"


def render_page(page_job: dict) -> str:
    """compare.html with its policy, the job and pixelmatch filled in: one file that loads nothing."""
    template = PAGE.read_text(encoding="utf-8")
    pixelmatch = PIXELMATCH.read_text(encoding="utf-8")
    slots = [m.group(1) for m in SLOT.finditer(template)]
    if sorted(slots) != ["job", "pixelmatch", "policy"]:
        raise CompareError(f"{PAGE} must hold each of its three slots once; it holds {slots}.")
    if UNSAFE_IN_SCRIPT.search(pixelmatch):
        raise CompareError(f"{PIXELMATCH} holds text that would end or change the page's script.")
    styles, modules = STYLE.findall(template), MODULE.findall(template)
    if len(styles) != 1 or len(modules) != 1:
        raise CompareError(f"{PAGE} must hold one style and one module script.")
    script = modules[0].replace("{{pixelmatch}}", pixelmatch)
    policy = (f"default-src 'none'; script-src {_hash(script)}; style-src {_hash(styles[0])}; base-uri 'none'; "
              "form-action 'none'; require-trusted-types-for 'script'")
    fill = {"policy": policy, "job": _json_block(page_job), "pixelmatch": pixelmatch}
    return SLOT.sub(lambda m: fill[m.group(1)], template)


# --------------------------------------------------------------------------- the browser

# Opens the page from its file in the pinned Playwright's Chromium (sandboxed, no crash reporter, service workers
# blocked), waits for the page to finish, and prints what it left with every request and console error it saw.
DRIVER = r"""
const o = JSON.parse(process.argv[1]);
const {chromium} = require(o.core);
(async () => {
  const browser = await chromium.launch({channel: "chromium", headless: true, chromiumSandbox: true,
                                         args: ["--disable-breakpad", "--disable-crash-reporter"]});
  try {
    const context = await browser.newContext({serviceWorkers: "block"});
    const page = await context.newPage();
    const out = {requests: [], errors: []};
    page.on("request", (r) => out.requests.push(r.url()));
    page.on("console", (m) => { if (m.type() === "error") out.errors.push(m.text().slice(0, 200)); });
    page.on("pageerror", (e) => out.errors.push(String(e && e.message).slice(0, 200)));
    await page.goto(o.url);
    await page.waitForSelector("html[data-state=done], html[data-state=error]", {timeout: o.timeout});
    Object.assign(out, await page.evaluate(() => globalThis.devkitCompare));
    process.stdout.write(JSON.stringify(out));
  } finally {
    await browser.close();
  }
})().catch((e) => { process.stderr.write(String(e && e.message || e).slice(0, 2000)); process.exit(1); });
"""


def _playwright(core=None) -> dict:
    node = shutil.which("node")
    if not node:
        raise CompareError(NO_BROWSER)
    if core is None:
        try:
            npm = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=60).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            npm = ""
        core = Path(npm or "/nonexistent") / "@playwright" / "cli" / "node_modules" / "playwright-core"
    if not (Path(core) / "package.json").is_file():
        raise CompareError(NO_BROWSER)
    return {"node": node, "core": str(core)}


def _write_private(path: Path, data: bytes):
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _private_dir(path: Path):
    if not path.is_dir():
        _private_dir(path.parent)
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)


def compare_pictures(before: bytes, after: bytes, anchor=None, scale=1, *, labels=None, band=None, core=None,
                     work=None, timeout=TIMEOUT, elements=None) -> dict:
    """Compare two PNG pictures; anchor is the finding's box in CSS px (or None). -> the result, `diff` as PNG bytes.

    The page and the browser's profile live in a new 0700 folder (under `work` when given, a folder of the store),
    deleted when the comparison ends, also after a failure.
    """
    page = render_page(job(before, after, anchor, scale, labels=labels, band=band, elements=elements))
    pw = _playwright(core)
    folder = Path(tempfile.mkdtemp(prefix="compare-", dir=work))
    try:
        path = folder / "compare.html"
        _write_private(path, page.encode("utf-8"))
        profile = folder / "profile"
        profile.mkdir(mode=0o700)
        opts = {"core": pw["core"], "url": path.as_uri(), "timeout": int(timeout * 1000)}
        try:
            out = subprocess.run([pw["node"], "-e", DRIVER, json.dumps(opts)], capture_output=True, text=True,
                                 timeout=timeout + 30, cwd=str(profile), env=dict(os.environ, TMPDIR=str(profile)))
        except subprocess.TimeoutExpired:
            raise CompareError(f"The comparison did not finish in {timeout} s. Nothing was compared.") from None
        url = path.as_uri()
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if out.returncode != 0:
        why = (out.stderr.strip().splitlines() or ["no message"])[0][:300]
        raise CompareError(f"Chromium could not run the compare page: {why}. Check the browser: dev doctor.")
    data = json.loads(out.stdout)
    other = sorted({u for u in data.get("requests", []) if u != url})
    if other:
        raise CompareError(f"The compare page asked for {len(other)} other resource(s); nothing was compared.")
    if data.get("errors"):
        raise CompareError(f"The compare page logged an error: {data['errors'][0]}")
    if data.get("state") != "done":
        raise CompareError(f"Could not compare the pictures: {data.get('message') or 'the page did not finish'}.")
    result = data["result"]
    result["diff"] = base64.b64decode(result["diff"])
    for share in ("inside", "outside"):  # JavaScript writes a share of 1 or 0 as an integer
        if result.get(share) is not None:
            result[share] = float(result[share])
    return result


# --------------------------------------------------------------------------- the elements that changed (R36)

def _box(value) -> bool:
    return (isinstance(value, (list, tuple)) and len(value) == 4
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value))


def _elements(description) -> list:
    found = (description or {}).get("elements") if isinstance(description, dict) else None
    return [e for e in found or () if isinstance(e, dict) and isinstance(e.get("id"), str) and _box(e.get("box"))]


def _key(element: dict, field: str):
    value = element.get(field)
    return (field, element.get("frame"), value) if isinstance(value, str) and value else None


def _uncompared(why: str) -> dict:
    return {"compared": False, "why": why, "changes": [], "counts": dict.fromkeys(CHANGES, 0), "matched": 0,
            "truncated": False}


def element_changes(before: dict, after: dict) -> dict:
    """What moved, resized, appeared, or disappeared between two descriptions (devkit.visual.description/1).

    -> {compared: True, why: None, changes: [the first MAX_ELEMENTS], counts: {disappeared, appeared, moved,
        resized}, matched: elements found in both, truncated: a description was cut at its element limit}; each change
        is {change, by: "selector" | "locator", key, role, before: {element, box} | None, after: … | None,
        offset: [dx, dy] | None, size: [dw, dh] | None}, listed disappeared, appeared, moved, resized, each from the
        top of the page. An element that moved and resized is `moved`; one that resized and moved only with its
        ancestor is `resized`.
    """
    before, after = (d if isinstance(d, dict) else {} for d in (before, after))
    sides = _elements(before), _elements(after)
    counts = [Counter(k for e in els for k in (_key(e, f) for f in KEYS) if k) for els in sides]
    single = {k: e for e in sides[1] for k in (_key(e, f) for f in KEYS) if k and counts[1][k] == 1}
    pairs, taken = {}, set()  # before id -> (after element, the field that matched); the after ids matched
    for field in KEYS:
        for e in sides[0]:
            k = _key(e, field)
            if e["id"] in pairs or not k or counts[0][k] != 1 or counts[1][k] != 1:
                continue
            other = single[k]
            if other["id"] not in taken and other.get("role") == e.get("role"):
                pairs[e["id"]] = (other, field)
                taken.add(other["id"])

    found = []
    by_id = {e["id"]: e for e in sides[0]}
    delta = {bid: [a - b for a, b in zip(other["box"], by_id[bid]["box"])] for bid, (other, _) in pairs.items()}

    def ancestors(element, index):
        seen, parent = set(), element.get("parent")
        while isinstance(parent, str) and parent in index and parent not in seen:
            seen.add(parent)
            yield parent
            parent = index[parent].get("parent")

    def entry(change, field, b, a):
        side = b or a
        return {"change": change, "by": field, "key": side.get(field), "role": side.get("role"),
                "before": {"element": b["id"], "box": list(b["box"])} if b else None,
                "after": {"element": a["id"], "box": list(a["box"])} if a else None,
                "offset": [a["box"][0] - b["box"][0], a["box"][1] - b["box"][1]] if a and b else None,
                "size": [a["box"][2] - b["box"][2], a["box"][3] - b["box"][3]] if a and b else None}

    for bid, (other, field) in pairs.items():
        dx, dy, dw, dh = delta[bid]
        carrier = next((p for p in ancestors(by_id[bid], by_id) if p in pairs), None)
        carried = carrier is not None and abs(dx - delta[carrier][0]) <= ROUNDING \
            and abs(dy - delta[carrier][1]) <= ROUNDING
        moved = max(abs(dx), abs(dy)) > ROUNDING and not carried
        if moved or max(abs(dw), abs(dh)) > ROUNDING:
            found.append(entry("moved" if moved else "resized", field, by_id[bid], other))

    # an element of one side only is listed when a key singles it out there and no key of it is shared by two
    # elements of the other side; nothing is gone from a description cut at its limit, and nothing inside a listed
    # element is listed again
    def one_side(change, own, mine, theirs, matched):
        index, listed = {e["id"]: e for e in own}, {}
        for e in own:
            keys = [k for k in (_key(e, f) for f in KEYS) if k and mine[k] == 1]
            if e["id"] not in matched and keys and all(theirs[k] <= 1 for k in keys):
                listed[e["id"]] = (e, keys[0][0])
        for e, field in listed.values():
            if not any(p in listed for p in ancestors(e, index)):
                found.append(entry(change, field, *((e, None) if change == "disappeared" else (None, e))))

    if not after.get("truncated"):
        one_side("disappeared", sides[0], counts[0], counts[1], pairs)
    if not before.get("truncated"):
        one_side("appeared", sides[1], counts[1], counts[0], taken)

    def order(c):
        box = (c["before"] if c["change"] == "disappeared" else c["after"])["box"]
        return CHANGES.index(c["change"]), box[1], box[0], str(c["key"])

    found.sort(key=order)
    return {"compared": True, "why": None, "changes": found[:MAX_ELEMENTS],
            "counts": {k: sum(1 for c in found if c["change"] == k) for k in CHANGES}, "matched": len(pairs),
            "truncated": bool(before.get("truncated") or after.get("truncated"))}


def _description(store: Path, rec: dict):
    """-> (the capture's description, None), or (None, why it cannot be read): its bytes must match its record."""
    cid, sha = rec.get("id"), (rec.get("description") or {}).get("sha256")
    if not isinstance(sha, str) or not SHA.match(sha):
        return None, f"{cid} has no description"
    try:
        data = (store / "desc" / f"{sha}.json.gz").read_bytes()
    except OSError:
        return None, f"the description of {cid} is not on this Mac"
    if hashlib.sha256(data).hexdigest() != sha:
        return None, f"the description of {cid} in the store does not match its record"
    try:
        desc = json.loads(gzip.decompress(data))
    except (OSError, EOFError, ValueError, zlib.error):
        desc = None
    if not isinstance(desc, dict) or desc.get("schema") != DESCRIPTION or not isinstance(desc.get("elements"), list):
        return None, f"the description of {cid} is not a {DESCRIPTION} description"
    return desc, None


def capture_elements(store, before: dict, after: dict) -> dict:
    """element_changes of two capture records, from their descriptions in the store's desc/ folder; when one cannot
    be read, {compared: False, why} and nothing listed."""
    found = []
    for rec in (before, after):
        desc, why = _description(Path(store), rec)
        if why:
            return _uncompared(why)
        found.append(desc)
    return element_changes(*found)


def _n(v) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}"


def _place(box) -> str:
    return f"{_n(box[0])},{_n(box[1])}, {_n(box[2])}×{_n(box[3])} px"


def _size(box) -> str:
    return f"{_n(box[2])}×{_n(box[3])}"


def element_lines(elements: dict) -> list:
    """One line a changed element, then what the list leaves out; or why the elements were not compared."""
    if not elements.get("compared"):
        return [f"elements not compared: {elements.get('why')}"]
    lines = []
    for c in elements["changes"]:
        head, b, a = f"{c['change']:<12} {c['key']}", c["before"], c["after"]
        if c["change"] == "disappeared":
            lines.append(f"{head}  was at {_place(b['box'])} ({b['element']})")
        elif c["change"] == "appeared":
            lines.append(f"{head}  at {_place(a['box'])} ({a['element']})")
        else:
            ids = f"({b['element']} → {a['element']})"
            resized = f"from {_size(b['box'])} to {_size(a['box'])} px"
            if c["change"] == "resized":
                lines.append(f"{head}  {resized} {ids}")
            else:
                also = f", resized {resized}" if max(abs(v) for v in c["size"]) > ROUNDING else ""
                lines.append(f"{head}  by {_n(c['offset'][0])},{_n(c['offset'][1])} px{also} {ids}")
    total = sum(elements["counts"].values())
    more = total - len(elements["changes"])
    if not total:
        lines.append(f"elements: none moved, resized, appeared, or disappeared ({elements['matched']} found in both "
                     "by selector or locator)")
    elif more:
        lines.append(f"and {more} more changed element{'' if more == 1 else 's'}")
    if elements.get("truncated"):
        lines.append("a description was cut at its element limit: elements past it are not listed as appeared or "
                     "disappeared")
    return lines


# --------------------------------------------------------------------------- captures

RECIPE_EXCEPT = ("css",)  # an experiment capture (R37) is its recipe with CSS added


def recipe_difference(a: dict, b: dict) -> list:
    """The fields that keep two capture records from being captures of one recipe ([] when they are)."""
    fields = []
    if a.get("kind") != b.get("kind"):
        fields.append("kind")
    ra, rb = a.get("recipe") or {}, b.get("recipe") or {}
    fields += [k for k in dict.fromkeys([*ra, *rb]) if k not in RECIPE_EXCEPT and ra.get(k) != rb.get(k)]
    if (a.get("image") or {}).get("scale") != (b.get("image") or {}).get("scale") and "dpr" not in fields:
        fields.append("scale")
    return fields


def _capture(records: Path, cid: str) -> dict:
    if not ID.match(cid or ""):
        raise CompareError(f"{cid!r} is not a capture id (VC-…).")
    try:
        rec = json.loads((records / "captures" / f"{cid}.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CompareError(f"No capture {cid} in {records / 'captures'}.") from None
    except ValueError:
        raise CompareError(f"The record of {cid} is not valid JSON.") from None
    if not isinstance(rec, dict) or rec.get("id") != cid:
        raise CompareError(f"The record {cid}.json does not hold capture {cid}.")
    return rec


def _picture(store: Path, rec: dict) -> bytes:
    cid, sha = rec["id"], (rec.get("image") or {}).get("sha256")
    if not isinstance(sha, str) or not SHA.match(sha):
        raise CompareError(f"The record of {cid} names no picture.")
    try:
        data = (store / "blobs" / f"{sha}.png").read_bytes()
    except OSError:
        raise CompareError(f"The picture of {cid} is not on this Mac. Capture it here: "
                           f"dev visual capture --again {cid}") from None
    if hashlib.sha256(data).hexdigest() != sha:
        raise CompareError(f"The picture of {cid} in the store does not match its record (SHA-256 {sha[:12]}…). "
                           "Nothing was compared.")
    return data


def box_scale(rec: dict):
    """Picture pixels per unit of the capture's boxes: a web capture's device pixel ratio (its boxes are in CSS px);
    1 for a document page, whose boxes are already in picture pixels (its `scale` turns PDF points into pixels)."""
    if rec.get("kind") == "document":
        return 1
    return (rec.get("image") or {}).get("scale") or 1


def region_signals(regions: list) -> list:
    """A `changed-region` signal for each region outside the box: what verify adds to the after capture."""
    return [{"check": "changed-region", "pass": "layout", "rule": [],
             "anchor": {"element": None, "region": list(r["box"]), "selector": None, "name": None},
             "measured": f"{r['pixels']:,} pixels changed", "threshold": "0 pixels"} for r in regions]


def compare_captures(records, store, before_id: str, after_id: str, anchor=None, *, core=None, band=None,
                     timeout=TIMEOUT) -> dict:
    """Compare the pictures of two captures of one recipe and keep the difference picture in the store.

    -> {before, after, anchor, diff: {sha256, width, height, scale}, pixels, changed, inside, outside, regions,
        regions_total, signals, elements}; `inside` and `outside` are what a finding's verification holds, and
        `elements` is capture_elements' (the descriptions are read only after the refusals).
    """
    records, store = Path(records), Path(store)
    before, after = _capture(records, before_id), _capture(records, after_id)
    differ = recipe_difference(before, after)
    if differ:
        raise CompareError(f"{before_id} and {after_id} are captures of different recipes ({', '.join(differ)} "
                           f"differ). Compare captures of one recipe: dev visual capture --again {before_id}")
    scale = box_scale(before)
    pictures = _picture(store, before), _picture(store, after)
    elements = capture_elements(store, before, after)
    _private_dir(store / "blobs")
    result = compare_pictures(*pictures, anchor, scale, labels={"before": before_id, "after": after_id}, band=band,
                              core=core, work=store, timeout=timeout,
                              elements=elements["counts"] if elements["compared"] else None)
    diff = result.pop("diff")
    sha = hashlib.sha256(diff).hexdigest()
    target = store / "blobs" / f"{sha}.png"
    if not target.exists():
        _write_private(target, diff)
    return {"before": before_id, "after": after_id, "anchor": _boxes(anchor) or None,
            "diff": {"sha256": sha, "width": result["width"], "height": result["height"], "scale": scale},
            "pixels": result["pixels"], "changed": result["changed"], "inside": result["inside"],
            "outside": result["outside"], "regions": result["regions"], "regions_total": result["regions_total"],
            "signals": region_signals(result["regions"]), "elements": elements}
