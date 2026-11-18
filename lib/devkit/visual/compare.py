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

Interface:

  job(before, after, anchor, scale, labels=None, band=None) -> dict      the page's input
  render_page(job) -> str                                                the page, ready to open
  compare_pictures(before, after, anchor=None, scale=1, ...) -> dict     two PNGs; the result with `diff` as PNG bytes
  compare_captures(records, store, before_id, after_id, anchor=None, ...) -> dict
      two capture records: the same recipe, pictures checked against their SHA-256; writes the difference picture
      into the store's blobs and returns it as {sha256, width, height, scale}, with the region signals
  recipe_difference(a, b) -> [field]                                     what keeps two captures from one recipe
  box_scale(record) -> number                    picture pixels per unit of its boxes: the dpr, 1 for a document
  region_signals(regions) -> [Signal]                                    `changed-region` signals, not numbered
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import tempfile
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


def job(before: bytes, after: bytes, anchor, scale, labels=None, band=None) -> dict:
    """The page's input: the two pictures, the finding's box (one [x, y, w, h] in CSS px, or several), the scale."""
    for data in (before, after):
        w, h = _png_size(data)
        if not (0 < w <= MAX_SIDE and 0 < h <= MAX_SIDE and w * h <= MAX_AREA):
            raise CompareError(f"A picture to compare is {w}×{h} px; the comparison reads pictures of at most "
                               f"{MAX_SIDE:,} px a side and {MAX_AREA:,} pixels.")
    if not (isinstance(scale, (int, float)) and not isinstance(scale, bool) and 0 < scale <= 8):
        raise CompareError(f"The scale {scale!r} is not a device pixel ratio between 0 and 8.")
    if labels is not None:
        labels = {"before": str(labels["before"]), "after": str(labels["after"])}
    return {"before": base64.b64encode(before).decode("ascii"), "after": base64.b64encode(after).decode("ascii"),
            "anchor": _boxes(anchor), "scale": scale, "threshold": THRESHOLD, "band": int(band or BAND),
            "cell": max(1, round(CELL * scale)), "max_regions": MAX_REGIONS, "labels": labels}


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
                     work=None, timeout=TIMEOUT) -> dict:
    """Compare two PNG pictures; anchor is the finding's box in CSS px (or None). -> the result, `diff` as PNG bytes.

    The page and the browser's profile live in a new 0700 folder (under `work` when given, a folder of the store),
    deleted when the comparison ends, also after a failure.
    """
    page = render_page(job(before, after, anchor, scale, labels=labels, band=band))
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
        regions_total, signals}; `inside` and `outside` are what a finding's verification holds.
    """
    records, store = Path(records), Path(store)
    before, after = _capture(records, before_id), _capture(records, after_id)
    differ = recipe_difference(before, after)
    if differ:
        raise CompareError(f"{before_id} and {after_id} are captures of different recipes ({', '.join(differ)} "
                           f"differ). Compare captures of one recipe: dev visual capture --again {before_id}")
    scale = box_scale(before)
    pictures = _picture(store, before), _picture(store, after)
    _private_dir(store / "blobs")
    result = compare_pictures(*pictures, anchor, scale, labels={"before": before_id, "after": after_id}, band=band,
                              core=core, work=store, timeout=timeout)
    diff = result.pop("diff")
    sha = hashlib.sha256(diff).hexdigest()
    target = store / "blobs" / f"{sha}.png"
    if not target.exists():
        _write_private(target, diff)
    return {"before": before_id, "after": after_id, "anchor": _boxes(anchor) or None,
            "diff": {"sha256": sha, "width": result["width"], "height": result["height"], "scale": scale},
            "pixels": result["pixels"], "changed": result["changed"], "inside": result["inside"],
            "outside": result["outside"], "regions": result["regions"], "regions_total": result["regions_total"],
            "signals": region_signals(result["regions"])}
