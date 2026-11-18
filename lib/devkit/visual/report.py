# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev visual report`: the report of one visual audit, as HTML and as Markdown.

The HTML report is one file, `reports/VA-…/index.html` in the local store, with the pictures it shows copied next to
it (`img/<sha256>.png`). It opens on the view its scope asks for (journeys, the capture, or the states grid), then
the map, the gallery with the findings drawn on each capture, the findings with their filters, and the pass coverage.
`--md` prints the summary a pull request or the handoff note carries.

Captured text is data (R66). Every string from a record is escaped by the one markup builder below (`_el`), so none
becomes markup, in HTML or in Markdown. The page carries a Content Security Policy in a `meta` tag (R72): nothing but
its own hashed script and style runs, pictures come only from the page's own files and `data:`, and nothing connects,
submits, or changes the base URL. A captured URL is never an `href` or a `src`: hrefs are in-file anchors to ids that
pass a strict pattern, and srcs are `img/<sha256>.png` (or `map/<name>-light.svg`) paths that devkit writes. In a file
page Chromium's `'self'` matches every `file:` URL, so the folder limit holds by construction, not by the policy.
Positions of pins, boxes and crops are classes in the one style block, since the policy blocks `style` attributes.

The records are the spec's formats (captures, findings, the map) and the contract for audits and triage the store
writes; `load` reads them, strips control and bidirectional characters from every string a sync may have brought in,
and checks each picture's SHA-256 before the report shows it.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import html
import json
import os
import re
import tempfile
from pathlib import Path

from .map import clean_record

ID = re.compile(r"^V[ACF]-[0-9a-z]{4,40}$")
SIGNAL_ID = re.compile(r"^V[AC]-[0-9a-z]{4,40}#\d{1,5}$")
SHA = re.compile(r"^[0-9a-f]{64}$")
DIAGRAM = re.compile(r"^[a-z0-9][a-z0-9._-]{0,60}$")
SEVERITIES = ("P0", "P1", "P2", "P3")
OPEN = ("open", "reopened", "fixing", "fixed")
CLOSED = ("wontfix", "duplicate", "not-a-problem")
STATUS_LABEL = {"open": "open", "reopened": "reopened", "fixing": "fixing", "fixed": "fixed", "verified": "verified",
                "wontfix": "won't fix", "duplicate": "duplicate", "not-a-problem": "not a problem"}
HISTORY_VERB = {"open": "opened", "reopened": "reopened", "fixing": "fixing", "fixed": "fixed", "verified": "verified",
                "wontfix": "closed as won't fix", "duplicate": "closed as duplicate",
                "not-a-problem": "closed as not a problem"}
SCREEN_PASSES = (("task", "Task and first look"), ("hierarchy", "Information hierarchy"),
                 ("layout", "Layout and alignment"), ("spacing", "Spacing and rhythm"), ("typography", "Typography"),
                 ("color", "Color and contrast"), ("content", "Content and copy"), ("states", "States"),
                 ("responsive", "Responsive"), ("accessibility", "Accessibility"))
STRUCTURE_PASSES = (("S1", "Journeys"), ("S2", "Reachability"), ("S3", "Navigation and wayfinding"),
                    ("S4", "Context and continuity"), ("S5", "Consistency across screens"),
                    ("S6", "States across the product"))
PASS_NAME = dict(SCREEN_PASSES + STRUCTURE_PASSES)
# the coverage heads on a wide screen; a phone shows the passes' numbers and the key
SHORT_NAME = {"task": "Task", "hierarchy": "Hierarchy", "layout": "Layout", "spacing": "Spacing",
              "typography": "Type", "color": "Color", "content": "Copy", "states": "States",
              "responsive": "Responsive", "accessibility": "A11y"}
RECORDED = ("findings", "clean", "n/a")
VIEWS = {
    "screen": (("capture", "Capture"), ("findings", "Findings")),
    "component": (("states", "States"), ("findings", "Findings")),
    "journey": (("journeys", "Journey"), ("gallery", "Gallery"), ("findings", "Findings")),
    "section": (("journeys", "Journeys"), ("map", "Map"), ("gallery", "Gallery"), ("findings", "Findings"),
                ("coverage", "Coverage")),
    "product": (("journeys", "Journeys"), ("map", "Map"), ("gallery", "Gallery"), ("findings", "Findings"),
                ("coverage", "Coverage")),
}
# the passes each scope counts (R58): structure passes once for the audit, screen passes on each capture
COVERAGE = {
    "screen": ((), SCREEN_PASSES),
    "component": (("S5",), SCREEN_PASSES[1:]),
    "journey": (("S1", "S3", "S4", "S6"), SCREEN_PASSES),
    "section": (tuple(p for p, _ in STRUCTURE_PASSES), SCREEN_PASSES),
    "product": (tuple(p for p, _ in STRUCTURE_PASSES), SCREEN_PASSES),
}
MAX_OUTLINES = 300
CROP_MARGIN = 24
ROOT = Path(__file__).resolve().parents[3]


# --------------------------------------------------------------------------- reading the records

def _read(path: Path):
    # a record as read, without control or bidirectional characters, whatever a sync brought in (R71)
    try:
        return clean_record(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def _read_dir(path: Path) -> list:
    out = []
    for p in sorted(Path(path).glob("V?-*.json")):
        rec = _read(p)
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _people() -> dict:
    roster = _read(ROOT / "config" / "crew.json") or {}
    return {m["id"]: f"{m.get('title') or m['id']} ({m.get('name') or m['id']})"
            for m in roster.get("members", []) if isinstance(m, dict) and m.get("id")}


def _picture_state(store: Path, sha) -> str | None:
    # -> "ok" (in the store, bytes match the name), "mismatch", or None (not on this Mac)
    if not isinstance(sha, str) or not SHA.match(sha):
        return None
    path = store / "blobs" / f"{sha}.png"
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return "ok" if hashlib.sha256(data).hexdigest() == sha else "mismatch"


def _description(store: Path, sha) -> list:
    # the description's elements that the outline and alignment tools draw: id, name, box
    if not isinstance(sha, str) or not SHA.match(sha):
        return []
    try:
        doc = json.loads(gzip.decompress((store / "desc" / f"{sha}.json.gz").read_bytes()))
    except (OSError, ValueError, EOFError, gzip.BadGzipFile):
        return []
    out = []
    for e in (doc.get("elements") or [])[:MAX_OUTLINES]:
        box = e.get("box")
        if not (isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box)):
            continue
        label = e.get("name") or e.get("text") or e.get("role") or ""
        out.append({"id": str(e.get("id") or ""), "name": str(label), "box": [float(v) for v in box]})
    return out


def load(records, store, audit_id: str) -> dict:
    """The audit, its captures and findings, the map, and what the local store holds for them."""
    records, store = Path(records), Path(store)
    if not ID.match(audit_id or ""):
        raise ValueError(f"{audit_id!r} is not an audit id")
    audit = _read(records / "audits" / f"{audit_id}.json")
    if not isinstance(audit, dict):
        raise FileNotFoundError(f"No audit {audit_id} in {records / 'audits'}")
    ids = {audit_id} | {i for i in audit.get("include") or [] if isinstance(i, str) and ID.match(i)}
    captures = [c for c in _read_dir(records / "captures") if c.get("audit") in ids]
    findings = [f for f in _read_dir(records / "findings") if f.get("audit") in ids]
    known = {c.get("id") for c in captures}
    for f in findings:  # a verification capture another audit holds still shows as the finding's After
        vid = (f.get("verification") or {}).get("capture")
        if isinstance(vid, str) and ID.match(vid) and vid not in known:
            rec = _read(records / "captures" / f"{vid}.json")
            if isinstance(rec, dict):
                captures.append(rec)
                known.add(vid)
    shas = set()
    for c in captures:
        shas.add((c.get("image") or {}).get("sha256"))
        shas.add((c.get("element") or {}).get("sha256"))
    for f in findings:  # pixelmatch's difference picture of a verification
        diff = (f.get("verification") or {}).get("diff")
        if isinstance(diff, dict) and isinstance(diff.get("sha256"), str):
            shas.add(diff["sha256"])
    pictures = {s: st for s in shas if (st := _picture_state(store, s))}
    descriptions = {c["id"]: _description(store, (c.get("description") or {}).get("sha256")) for c in captures
                    if c.get("id")}
    diagrams = []
    folder = store / "map" / "diagrams"
    for light in sorted(folder.glob("*-light.svg")) if folder.is_dir() else []:
        name = light.name[:-len("-light.svg")]
        dark = folder / f"{name}-dark.svg"
        if DIAGRAM.match(name) and dark.is_file():
            diagrams.append({"name": name, "light": light, "dark": dark})
    return {"audit": audit, "captures": captures, "findings": findings,
            "map": _read(records / "map" / "current.json"), "pictures": pictures, "store": store,
            "descriptions": descriptions, "diagrams": diagrams, "people": _people()}


# --------------------------------------------------------------------------- the markup builder

class Markup(str):
    """Markup that devkit wrote; every other string is escaped when it is put into the page."""


VOID = {"img", "meta", "input", "br", "source", "hr", "link"}


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _kids(kids) -> str:
    out = []
    for k in kids:
        if k is None or k is False:
            continue
        if isinstance(k, Markup):
            out.append(k)
        elif isinstance(k, (list, tuple)):
            out.append(_kids(k))
        else:
            out.append(_esc(k))
    return "".join(out)


def _el(tag: str, attrs: dict | None = None, *kids) -> Markup:
    parts = []
    for name, value in (attrs or {}).items():
        if value is None or value is False:
            continue
        name = name.rstrip("_").replace("_", "-")
        parts.append(f" {name}" if value is True else f' {name}="{_esc(value)}"')
    if tag in VOID:
        return Markup(f"<{tag}{''.join(parts)}>")
    return Markup(f"<{tag}{''.join(parts)}>{_kids(kids)}</{tag}>")


def _join(items, sep=", ") -> Markup:
    out = []
    for i, item in enumerate(items):
        if i:
            out.append(_esc(sep))
        out.append(_kids([item]))
    return Markup("".join(out))


SP = Markup(" ")


def _anchor(target: str, *kids, **attrs) -> Markup:
    # an in-file link to a record's id, or plain text when the value is not an id
    if isinstance(target, str) and (ID.match(target) or target in ("journeys", "map", "gallery", "findings",
                                                                    "coverage", "capture", "states")):
        return _el("a", {"href": "#" + target, **attrs}, *(kids or (target,)))
    return _el("span", None, *(kids or (str(target),)))


def _cmd(text) -> Markup:
    # a command to copy, on a line of its own that never wraps; it scrolls when it is wider than its box, so it
    # takes the focus for a keyboard to scroll it
    return _el("code", {"class": "cmd", "tabindex": "0"}, text)


# code-like runs in prose: var(…), a custom property, an attribute selector, a class or an id
CODE_RUN = re.compile(r"var\([^()\s]*\)|(?<![\w-])--[A-Za-z0-9][\w-]*|\[[^\]\s]+\]|(?<![\w.#])[.#][A-Za-z_][\w-]*")


def _prose(text):
    # prose with its code-like runs set as code, so the body font never draws `--space-4` as a dash
    if text in (None, ""):
        return text
    s, out, at = str(text), [], 0
    for m in CODE_RUN.finditer(s):
        out += [s[at:m.start()], _el("code", None, m.group())]
        at = m.end()
    out.append(s[at:])
    return Markup(_kids(out))


# --------------------------------------------------------------------------- small helpers

def _n(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _when(ts) -> str:
    s = str(ts or "")
    return f"{s[:10]} {s[11:16]}" if len(s) >= 16 else s


def _pct(v) -> str:
    try:
        p = float(v) * 100
    except (TypeError, ValueError):
        return "?"
    text = f"{p:.0f}" if p >= 10 else f"{p:.1f}"
    return text[:-2] if text.endswith(".0") else text


def _person(people: dict, by) -> str:
    member = by.get("member") if isinstance(by, dict) else by
    return people.get(member, str(member or "someone"))


def _vt(c: dict) -> str:
    vp = (c.get("recipe") or {}).get("viewport") or [(c.get("image") or {}).get("width"), (c.get("image") or {}).get("fold")]
    theme = (c.get("recipe") or {}).get("theme") or "light"
    return f"{vp[0]}x{vp[1]}-{theme}"


def _vt_label(c: dict) -> str:
    vp = (c.get("recipe") or {}).get("viewport") or [(c.get("image") or {}).get("width"), (c.get("image") or {}).get("fold")]
    theme = (c.get("recipe") or {}).get("theme")
    return f"{vp[0]}×{vp[1]}" + (f" {theme}" if theme else "")


def _num(v, default=0.0) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def _region(f: dict):
    r = (f.get("anchor") or {}).get("region")
    if isinstance(r, list) and len(r) == 4 and all(isinstance(v, (int, float)) for v in r):
        return [float(v) for v in r]
    return None


class _Css:
    """Geometry rules for the one style block: positions as percentages, deduplicated into short class names."""

    def __init__(self):
        self.rules: dict[str, str] = {}

    def cls(self, decls: str) -> str:
        name = self.rules.get(decls)
        if name is None:
            name = f"g{len(self.rules)}"
            self.rules[decls] = name
        return name

    def text(self) -> str:
        return "".join(f".{n}{{{d}}}" for d, n in self.rules.items())


def _f(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".") or "0"


# --------------------------------------------------------------------------- the model

class Report:
    def __init__(self, data: dict):
        self.data = data
        self.audit = data["audit"]
        self.scope = self.audit.get("scope") if self.audit.get("scope") in VIEWS else "screen"
        self.people = data.get("people") or {}
        self.pictures = data.get("pictures") or {}
        self.map = data.get("map") if isinstance(data.get("map"), dict) else None
        self.css = _Css()
        caps = [c for c in data.get("captures") or [] if isinstance(c, dict) and ID.match(str(c.get("id", "")))]
        self.captures = {c["id"]: c for c in caps}
        matrix = self.audit.get("matrix") or {}
        vps = [f"{v[0]}x{v[1]}" for v in matrix.get("viewports") or [] if isinstance(v, list) and len(v) == 2]
        themes = list(matrix.get("themes") or [])

        def order(c):
            vt = _vt(c)
            vp, _, theme = vt.partition("-")
            return (vps.index(vp) if vp in vps else 99, themes.index(theme) if theme in themes else 99,
                    str(c.get("taken")), c["id"])
        self.order = order
        # the captures the passes count and the gallery shows: not a verification's recapture, not an experiment
        self.gallery = sorted((c for c in caps if not c.get("again_of") and not c.get("experiment")),
                              key=lambda c: c["id"])
        # the findings still to work on first, by severity; then the verified ones, then the closed ones
        rank = {s: i for i, s in enumerate(SEVERITIES)}
        state_rank = {"open": 0, "reopened": 0, "fixing": 1, "fixed": 2}
        fs = [f for f in data.get("findings") or [] if isinstance(f, dict) and ID.match(str(f.get("id", "")))]
        fs.sort(key=lambda f: (_group(f), rank.get(f.get("severity"), 9), state_rank.get(f.get("status"), 3), f["id"]))
        self.findings = fs
        self.number = {f["id"]: i for i, f in enumerate(fs, start=1)}
        self.by_id = {f["id"]: f for f in fs}
        self.on_capture: dict[str, list] = {}
        for f in fs:
            self.on_capture.setdefault(f.get("capture"), []).append(f)
        self.nodes = {n["id"]: n for n in (self.map or {}).get("nodes") or [] if isinstance(n, dict) and n.get("id")}
        self.edges = {e["id"]: e for e in (self.map or {}).get("edges") or [] if isinstance(e, dict) and e.get("id")}
        wanted = [j for j in self.audit.get("journeys") or []]
        journeys = [j for j in (self.map or {}).get("journeys") or [] if isinstance(j, dict) and j.get("id")]
        self.journeys = [j for j in journeys if not wanted or j["id"] in wanted]
        self.structure = [s for s in self.audit.get("signals") or [] if isinstance(s, dict)]
        # a screen or component audit of one capture per width and theme opens on the capture itself
        self.single = (self.scope in ("screen", "component") and bool(self.gallery)
                       and len({_vt(c) for c in self.gallery}) == len(self.gallery))

    # counts
    def is_open(self, f):
        return f.get("status") in OPEN

    def counts(self) -> dict:
        fs = self.findings
        untriaged = sum(1 for s in self.structure if not s.get("triage"))
        for c in self.gallery:
            untriaged += sum(1 for s in (c.get("checks") or {}).get("signals") or []
                             if isinstance(s, dict) and not s.get("triage"))
        return {"blocking": sum(1 for f in fs if self.is_open(f) and f.get("severity") in ("P0", "P1")),
                "open": sum(1 for f in fs if self.is_open(f)),
                "verified": sum(1 for f in fs if f.get("status") == "verified"),
                "closed": sum(1 for f in fs if f.get("status") in CLOSED),
                "untriaged": untriaged}

    def verdict(self) -> list:
        # -> [(text, the id it links to or None)]: the counts, or "Not reviewed yet" while there is no finding and
        # passes are open, so a zero never reads as an all-clear; open passes end the line, linked to the coverage
        c = self.counts()
        _, total, open_, _ = self.coverage()
        if not self.findings and open_:
            items = [(f"Not reviewed yet: {open_} of {total} passes are not recorded.", None)]
        else:
            items = [(_n(c["blocking"], "blocking or major finding open", "blocking or major findings open"), None),
                     (f"{c['open']} open", None), (f"{c['verified']} verified", None)]
        if c["untriaged"]:
            items.append((_n(c["untriaged"], "signal not triaged", "signals not triaged"), None))
        if self.findings and open_:
            items.append((f"{open_} of {total} passes not recorded", "coverage"))
        return items

    def untriaged_line(self) -> str | None:
        n = self.counts()["untriaged"]
        if not n:
            return None
        return (f"{_n(n, 'signal is', 'signals are')} not triaged. "
                "Each becomes a finding or is dismissed with a reason.")

    def coverage(self):
        # a pass without its mark that has a finding counts as recorded with findings: the finding is the record
        structure, screen = COVERAGE[self.scope]
        passes = self.audit.get("passes") or {}
        found = {f.get("pass") for f in self.findings}
        found_on = {(f.get("capture"), f.get("pass")) for f in self.findings}

        def cell(marks, p, has_finding):
            mark = marks.get(p) or {}
            result = mark.get("result")
            return p, result if result in RECORDED or not has_finding else "findings", mark.get("note")
        rows = [("structure", None, [cell(passes, p, p in found) for p in structure])] if structure else []
        for c in sorted(self.gallery, key=lambda c: (str(c.get("state")), self.order(c))):
            got = c.get("passes") or {}
            rows.append(("capture", c, [cell(got, p, (c["id"], p) in found_on) for p, _ in screen]))
        total = sum(len(r[2]) for r in rows)
        open_ = sum(1 for r in rows for _, result, _ in r[2] if result not in RECORDED)
        first = next((p for r in rows for p, result, _ in r[2] if result not in RECORDED), None)
        return rows, total, open_, first

    def node_of(self, f: dict):
        a = f.get("anchor") or {}
        if a.get("node"):
            return a["node"]
        if a.get("edge") or a.get("journey"):
            return None
        cap = self.captures.get(f.get("capture")) or {}
        return (cap.get("map") or {}).get("node")

    def person(self, by) -> str:
        return _person(self.people, by)


# --------------------------------------------------------------------------- HTML pieces

def _sev(sev) -> Markup:
    s = sev if sev in SEVERITIES else "P3"
    return _el("span", {"class": f"sev sev-{s.lower()}"}, s)


def _pin_label(r: Report, f: dict) -> str:
    return f"{r.number[f['id']]}, {f.get('severity')}: {f.get('title')}"


def _geo(r: Report, region, width, height, pin=False) -> str:
    x, y, w, h = region
    W, H = max(width, 1), max(height, 1)
    if pin:  # kept inside the frame, so a pin at an edge stays whole and clickable
        px, py = _f(min(max(x / W * 100, 0), 100)), _f(min(max(y / H * 100, 0), 100))
        return r.css.cls(f"left:clamp(12px,{px}%,calc(100% - 12px));top:clamp(12px,{py}%,calc(100% - 12px))")
    return r.css.cls(f"left:{_f(x / W * 100)}%;top:{_f(y / H * 100)}%;width:{_f(w / W * 100)}%;"
                     f"height:{_f(h / H * 100)}%")


def _aspect(r: Report, w, h) -> str:
    return r.css.cls(f"aspect-ratio:{_f(max(_num(w, 1), 1))}/{_f(max(_num(h, 1), 1))}")


def _img(sha: str, w, h, alt="", lazy=True, cls=None) -> Markup:
    return _el("img", {"src": f"img/{sha}.png", "alt": alt, "width": int(_num(w, 1)), "height": int(_num(h, 1)),
                       "loading": "lazy" if lazy else None, "decoding": "async", "class": cls})


def _picture_ok(r: Report, sha) -> bool:
    return r.pictures.get(sha) == "ok"


def _thumb(r: Report, c: dict, label=None, hidden=False) -> Markup:
    # the first viewport of a capture, with its findings as dots; a link to its capture view. Sizes, the fold, and
    # regions are in the regions' unit (CSS px on the web), whatever the picture's scale.
    img = c.get("image") or {}
    W, H = _capture_size(c)
    fold = _num(img.get("fold"), H) or H
    fold = min(fold, H)
    fs = r.on_capture.get(c["id"], [])
    dots = []
    for f in fs:
        reg = _region(f)
        if reg and reg[1] < fold:
            pos = _geo(r, reg, W, fold, pin=True)
        elif reg:
            continue
        else:
            pos = r.css.cls("left:14px;top:14px")
        # the number is drawn by CSS, so the link's text stays its label (the name says the count)
        dots.append(_el("span", {"class": f"dot sev-{str(f.get('severity', 'P3')).lower()} {pos}", "aria-hidden": "true",
                                 "data-n": str(r.number[f["id"]]), "data-filter-item": "",
                                 "data-severity": f.get("severity"), "data-status": _status_group(f)}))
    below = sum(1 for f in fs if (reg := _region(f)) and reg[1] >= fold)
    sha = img.get("sha256")
    note = None
    if _picture_ok(r, sha):
        inner = _el("span", {"class": f"frame {_aspect(r, W, fold)}"}, _img(sha, W, H), dots)
    else:
        note = "Not on this Mac" if not r.pictures.get(sha) else "Does not match its record"
        inner = _el("span", {"class": f"frame missing {_aspect(r, W, fold)}"},
                    _el("span", {"class": "missing-note"}, note), dots)
    count = _n(len(fs), "finding", "findings")
    later = f"{_n(below, 'finding', 'findings')} below the fold" if below else None
    visible = (f"{note} " if note else "") + (label or _vt_label(c)) + (f" {later}" if later else "")
    # the name starts with the visible label (WCAG 2.5.3), then says what the picture is
    name = ", ".join(x for x in (visible, str(c.get("state") or "capture"),
                                 _vt_label(c) if label else None, count) if x)
    # the note wraps as one piece, under the state and size, when the label is wider than the frame
    cap = _el("span", {"class": "thumb-label"}, label or _vt_label(c),
              [" ", _el("span", {"class": "muted"}, later)] if later else None)
    return _el("a", {"class": "thumb", "href": "#" + c["id"], "aria-label": name, "data-vt": _vt(c),
                     "data-filter-item": "", "hidden": hidden}, inner, SP, cap)


def _status_group(f: dict) -> str:
    st = f.get("status")
    return "closed" if st in CLOSED else str(st)


def _group(f: dict) -> int:
    # 0: still to work on (open, reopened, fixing, fixed), 1: verified, 2: closed
    st = f.get("status")
    return 2 if st in CLOSED else 1 if st == "verified" else 0


def _rules(f: dict) -> Markup:
    return _join([_el("code", None, str(x)) for x in f.get("rule") or []])


def _where(r: Report, f: dict) -> list:
    a = f.get("anchor") or {}
    parts = []
    if a.get("journey"):
        jid, _, step = str(a["journey"]).partition("#")
        parts.append(f"{jid} step {step}" if step else jid)
    if a.get("edge"):
        frm, _, to = str(a["edge"]).partition(">")
        parts.append(f"{frm} → {to}")
    node = r.node_of(f)
    if node and not a.get("edge"):
        title = (r.nodes.get(node) or {}).get("title")
        parts.append(f"{title} ({node})" if title else node)
    if a.get("name"):
        parts.append(a["name"])
    return parts


def _css_size(picture: dict, kind=None):
    # a picture's size in the unit of anchor regions and description boxes; None when not numbers. On the web that
    # is CSS px, the picture's pixels over its scale (the device pixel ratio). A document's boxes are the picture's
    # own pixels (its description's coords are page-px), and its scale is PDF points to pixels, so it is not used.
    w, h = picture.get("width"), picture.get("height")
    s = 1 if kind == "document" else picture.get("scale", 1)
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in (w, h, s)):
        return None
    return w / s, h / s


def _capture_size(c: dict):
    # a capture's picture in the unit of its regions: (width, height), (1, 1) when the record holds no size
    img = c.get("image") or {}
    return _css_size(img, c.get("kind")) or (_num(img.get("width"), 1) or 1, _num(img.get("height"), 1) or 1)


def _crop_frame(r: Report, region) -> str:
    _, _, w, h = region
    return _aspect(r, w + 2 * CROP_MARGIN, h + 2 * CROP_MARGIN)


def _crop_picture(r: Report, sha: str, size, region, cls_extra="") -> Markup:
    # the region of a picture with a margin around it; size is the picture's in CSS px
    W, H = size
    x, y, w, h = region
    m = CROP_MARGIN
    cw, ch = w + 2 * m, h + 2 * m
    pos = r.css.cls(f"width:{_f(W / cw * 100)}%;left:{_f(-(x - m) / cw * 100)}%;top:{_f(-(y - m) / ch * 100)}%")
    return _el("span", {"class": f"crop {_crop_frame(r, region)} {cls_extra}".strip()}, _img(sha, W, H, cls=pos))


def _crop(r: Report, c: dict, region, cls_extra="") -> Markup:
    return _crop_picture(r, (c.get("image") or {}).get("sha256"), _capture_size(c), region, cls_extra)


def _difference(r: Report, v: dict, before: dict, after: dict, region) -> tuple:
    # pixelmatch's difference picture of the verification at the box, or, for a verification that has none, the
    # After crop blended over the Before one. -> (the figure's picture, whether it is pixelmatch's)
    diff = v.get("diff")
    if diff is None:
        return _el("span", {"class": "diff"}, _crop(r, before, region), _crop(r, after, region, "over")), False
    sha = diff.get("sha256") if isinstance(diff, dict) and isinstance(diff.get("sha256"), str) else None
    size = _css_size(diff, before.get("kind")) if isinstance(diff, dict) else None
    if size and sha and _picture_ok(r, sha):  # a picture of the store, checked against its name
        return _crop_picture(r, sha, size, region), True
    note = "Not on this Mac" if size and sha and SHA.match(sha) and not r.pictures.get(sha) \
        else "Does not match its record"
    return _el("span", {"class": f"crop missing {_crop_frame(r, region)}"},
               _el("span", {"class": "missing-note"}, note)), False


def _finding_state(r: Report, f: dict) -> Markup | None:
    st, v, hist = f.get("status"), f.get("verification") or {}, [h for h in f.get("history") or [] if isinstance(h, dict)]

    def last(*changes):
        return next((h for h in reversed(hist) if h.get("change") in changes), {})
    if v.get("error"):
        on = f" on commit {v['commit']}" if v.get("commit") else ""
        return _el("p", {"class": "state"}, f"Could not verify: {v['error']}{on}.")
    if st == "fixed" and not v.get("accepted_by"):
        who = r.person(last("fixed").get("by"))
        if not v.get("capture"):  # no after picture to review yet
            return _el("p", {"class": "state"}, f"Fixed by {who}. Not verified yet: ", _cmd(f"dev visual verify {f['id']}"))
        if v.get("check") == "none":
            return _el("p", {"class": "state"}, f"Fixed by {who}. Waiting for a reviewer to accept it.")
    if st == "reopened" and v.get("check") == "fail":
        h = last("reopened")
        why = f": {h['why']}" if h.get("why") else ""
        return _el("p", {"class": "state alert"}, f"Still there after the fix on {_when(h.get('at'))}{why}. Reopened.")
    if st in CLOSED:
        h = last(st)
        of = f" of {f['duplicate_of']}" if f.get("duplicate_of") else ""
        return _el("p", {"class": "state"}, f"Closed as {STATUS_LABEL[st]}{of}: {h.get('why') or 'no reason given'}")
    if st == "verified":
        return _el("p", {"class": "state ok"}, "Verified on the same recipe.")
    return None


def _compare(r: Report, f: dict) -> Markup | None:
    v = f.get("verification") or {}
    reg = _region(f)
    before, after = r.captures.get(f.get("capture")), r.captures.get(v.get("capture"))
    if not (reg and before and after):
        return None
    if not (_picture_ok(r, (before.get("image") or {}).get("sha256"))
            and _picture_ok(r, (after.get("image") or {}).get("sha256"))):
        return None
    difference, measured = _difference(r, v, before, after, reg)
    numbers = (f"{_pct(v.get('inside'))}% of the pixels in the box changed, {_pct(v.get('outside'))}% outside it."
               if v.get("inside") is not None else None)
    legend = "Changed pixels are red." if measured else None
    return _el("div", {"class": "compare"},
               _el("figure", None, _crop(r, before, reg), _el("figcaption", None, "Before")),
               _el("figure", None, _crop(r, after, reg), _el("figcaption", None, "After")),
               _el("figure", None, difference, _el("figcaption", None, "Difference")),
               _el("p", {"class": "muted"}, numbers, SP if legend else None, legend, SP, "After: ",
                   _anchor(after["id"])) if numbers or legend else None)


def _detail_body(r: Report, f: dict, level: int = 4) -> Markup:
    rows = []

    def row(label, *value):
        if value and any(v not in (None, "", []) for v in value):
            rows.append(_el("div", {"class": "row"}, _el("dt", None, label + ":"), SP, _el("dd", None, *value)))
    row("Who it hurts", f.get("impact"))
    row("Observed", _prose(f.get("evidence")))
    row("Rule", _rules(f) if f.get("rule") else None)
    row("Fix", _prose(f.get("fix")))
    row("Guide", _el("code", None, f["guide"]) if f.get("guide") else None)
    where = _where(r, f)
    row("Where", _join(where, "; ") if where else None)
    row("Capture", _anchor(f.get("capture")) if f.get("capture") else None)
    roots = [g for g in r.findings if g.get("root") == f["id"]]
    row("Root of", _join([_el("a", {"href": "#" + g["id"]}, g.get("title"), f" ({g['id']})") for g in roots]) if roots else None)
    if f.get("root"):
        root = r.by_id.get(f["root"])
        row("Root", _el("a", {"href": "#" + root["id"]}, root.get("title")) if root else str(f["root"]))
    also = [x for x in f.get("evidence_captures") or [] if x != f.get("capture")]
    row("Also seen in", _join([_anchor(x) for x in also]) if also else None)
    hist = [h for h in f.get("history") or [] if isinstance(h, dict)]
    history = _el("ol", {"class": "history"}, [
        _el("li", None, f"{HISTORY_VERB.get(h.get('change'), str(h.get('change')))} by {r.person(h.get('by'))} "
                        f"{_when(h.get('at'))}" + (f": {h['why']}" if h.get("why") else "")) for h in hist]) if hist else None
    return Markup(_el("dl", None, rows) + _kids([_finding_state(r, f), _compare(r, f),
                                                  _el(f"h{level}", {"class": "hh"}, "History") if hist else None,
                                                  history]))


def _status_chip(f: dict) -> Markup | None:
    # every status but open, which is what a finding is until someone works on it
    st = f.get("status")
    if st == "open":
        return None
    return _el("span", {"class": f"status status-{_status_group(f)}"}, STATUS_LABEL.get(st, str(st)))


def _finding_head(r: Report, f: dict) -> Markup:
    return _el("span", {"class": "fhead"},
               _el("span", {"class": f"num sev-{str(f.get('severity', 'P3')).lower()}", "aria-hidden": "true"},
                   str(r.number[f["id"]])),
               _el("span", {"class": "tags"}, _sev(f.get("severity")), _status_chip(f)),
               _el("span", {"class": "ftitle"}, f.get("title")),
               _el("span", {"class": "fmeta"},
                   _el("span", None, PASS_NAME.get(f.get("pass"), str(f.get("pass")))),
                   _el("span", None, (_where(r, f) or [f.get("capture") or ""])[0]),
                   _el("span", None, f"source: {f.get('source')}")))


def _finding_article(r: Report, f: dict) -> Markup:
    schemes = " ".join(sorted({str(x).split(":", 1)[0] for x in f.get("rule") or []}))
    return _el("article", {"class": "finding", "id": f["id"], "data-filter-item": "", "data-severity": f.get("severity"),
                           "data-status": _status_group(f), "data-pass": f.get("pass"), "data-rule": schemes,
                           "data-source": str(f.get("source") or "").split(":", 1)[0]},
               _el("details", None, _el("summary", None, _finding_head(r, f)),
                   _el("div", {"class": "fbody"}, _detail_body(r, f, level=3))))


def _filters(r: Report, view: str, groups: list) -> Markup:
    fieldsets = []
    for name, legend, options in groups:
        if not options:
            continue
        fieldsets.append(_el("fieldset", None, _el("legend", None, legend), [
            _el("label", None, _el("input", {"type": "checkbox", "name": name, "value": value, "checked": True}),
                SP, label) for value, label in options]))
    # on a phone the rail moves into a modal dialog, which makes the page inert, holds the focus, closes on Esc or
    # a tap outside, and gives the focus back to the button that opened it
    title = f"filters-{view}-title"
    rail = _el("div", {"class": "filters", "id": f"filters-{view}", "role": "region", "aria-label": "Filters"},
               _el("div", {"class": "sheet-head"}, _el("h2", {"id": title}, "Filters"),
                   _el("button", {"type": "button", "class": "filters-close"}, "Done")),
               fieldsets, _el("button", {"type": "button", "class": "filters-clear"}, "Clear filters"))
    opener = _el("button", {"type": "button", "class": "filters-open", "data-sheet": f"filters-{view}-sheet",
                            "aria-haspopup": "dialog", "hidden": True}, "Filters")
    sheet = _el("dialog", {"class": "filters-dialog", "id": f"filters-{view}-sheet", "aria-labelledby": title,
                           "closedby": "any"})
    return Markup(opener + rail + sheet)


def _status_options(fs) -> list:
    present = {_status_group(f) for f in fs}
    order = ("open", "reopened", "fixing", "fixed", "verified", "closed")
    return [(s, s) for s in order if s in present]


def _empty_filter() -> Markup:
    return _el("p", {"class": "empty-filter", "hidden": True, "role": "status"},
               _el("span", {"class": "empty-text"}, "No findings match these filters."), SP,
               _el("button", {"type": "button", "class": "clear-filters"}, "Clear filters"))


# --------------------------------------------------------------------------- views

def _header(r: Report) -> Markup:
    a, c = r.audit, r.counts()
    scope = r.scope
    facts = [scope]
    if r.map and scope in ("section", "product", "journey"):
        screens = sum(1 for n in r.nodes.values() if n.get("kind") != "external")
        facts.append(f"{_n(screens, 'screen', 'screens')} (map v{r.map.get('version')})")
    if scope in ("section", "product", "journey"):
        facts.append(_n(len(r.journeys), "journey", "journeys"))
    facts.append(_n(len(r.gallery), "capture", "captures"))
    if a.get("commit"):
        facts.append(f"commit {a['commit']}")
    verdict = r.verdict()
    lines = []
    if (u := r.untriaged_line()):
        lines.append(_el("p", {"class": "line"}, u))
    if scope in ("screen", "component"):
        lines.append(_el("p", {"class": "line"}, f"{scope.capitalize()} audit: no map needed. Its findings attach to "
                                                 "the map when the project has one."))
    nav = _el("nav", {"class": "views", "aria-label": "Views"},
              _el("ul", None, [_el("li", None, _el("a", {"href": "#" + v}, label)) for v, label in VIEWS[scope]]))
    return _el("header", {"class": "top"},
               _el("div", {"class": "brand"}, _el("span", None, "devkit visual report"),
                   _el("span", None, str(a.get("taken") or "")[:10])),
               _el("h1", None, a.get("question") or a.get("id")),
               _el("ul", {"class": "verdict" + (" has-blocking" if c["blocking"] else "")},
                   [_el("li", None, _el("a", {"href": "#" + to}, text) if to else text) for text, to in verdict]),
               _el("ul", {"class": "facts"}, [_el("li", None, x) for x in facts]),
               lines, nav)


def _journeys_view(r: Report) -> Markup:
    title = "Journey" if r.scope == "journey" else "Journeys"
    if not r.journeys:
        return _el("section", {"class": "view", "data-view": "journeys", "id": "journeys", "aria-label": title},
                   _el("p", {"class": "empty"}, "No journeys yet. A journey is the path a user takes to finish a "
                                                "task. Declare one: ",
                       _cmd('dev visual map journey add J:<slug> --task "…" --path "a > b > c"')))
    return _el("section", {"class": "view", "data-view": "journeys", "id": "journeys", "aria-label": title},
               [_journey(r, j) for j in r.journeys])


def _journey_signals(r: Report, jid: str) -> dict:
    out = {}
    for s in r.structure:
        a = s.get("anchor") or {}
        if a.get("journey") == jid and isinstance(a.get("step"), int):
            out.setdefault(a["step"], []).append(s)
    return out


def _journey(r: Report, j: dict) -> Markup:
    steps = [s for s in j.get("steps") or [] if isinstance(s, dict)]
    frames = len(steps) + 1
    caps: dict[int, list] = {}
    for c in r.gallery:
        m = c.get("map") or {}
        if m.get("journey") == j["id"] and isinstance(m.get("step"), int):
            caps.setdefault(m["step"], []).append(c)
    walked = bool(caps)
    sigs = _journey_signals(r, j["id"])
    broken = min((k for k, ss in sigs.items() if any(s.get("check") == "broken-edge" for s in ss)), default=None)
    lost = sorted(k for k, ss in sigs.items() if any(s.get("check") == "lost-context" for s in ss))
    variants = sorted({_vt(c) for cs in caps.values() for c in cs},
                      key=lambda vt: min(r.order(c) for cs in caps.values() for c in cs if _vt(c) == vt))
    default_vt = variants[0] if variants else None
    alert = lambda text: _el("span", {"class": "alert-fact"}, _icon("cross"), text)  # noqa: E731
    facts = [j.get("actor")] if j.get("actor") else []
    steps_fact = _steps_against_budget(frames, j.get("budget"))
    facts.append(alert(steps_fact) if _over_budget(frames, j.get("budget")) else steps_fact)
    if broken:
        facts.append(alert(f"Breaks at step {broken}"))
    elif lost:
        facts.append(alert(f"Loses context at step {lost[0]}"))
    if not walked:
        facts.append("not walked yet")
    items = []
    for k in range(1, frames + 1):
        if broken and k > broken:
            break
        node_id = j.get("start") if k == 1 else _edge_to(r, steps[k - 2].get("edge"))
        node = r.nodes.get(node_id) or {}
        via = steps[k - 2] if k > 1 else None
        status = "broken" if k == broken else ("lost" if k in lost else ("empty" if k not in caps else "ok"))
        frame_caps = sorted(caps.get(k, []), key=r.order)
        if status == "broken":
            target = " ".join(via.get("do") or []) if via else ""
            body = _el("div", {"class": "frame broken-frame"},
                       _icon("cross"), _el("p", None, _el("strong", None, "Broken: "), _el("code", None, target)),
                       _el("p", None, "Steps after this one were not reached."))
        elif frame_caps:
            body = _el("div", {"class": "variants"}, [
                _thumb(r, c, label=str(node.get("title") or node_id), hidden=_vt(c) != default_vt)
                for c in frame_caps])
        else:
            body = _el("div", {"class": "frame empty-frame"}, _el("span", None, "Not captured"))
        marks = []
        if via and via.get("decision"):
            marks.append(_el("p", {"class": "mark decision"}, _icon("diamond"), f"Decision: {via['decision']}"))
        if k in lost:
            marks.append(_el("p", {"class": "mark lost"}, _icon("cross"), "Lost context (S4)"))
        trigger = None
        if via:
            edge = r.edges.get(via.get("edge")) or {}
            name = (edge.get("trigger") or {}).get("name") or " ".join(via.get("do") or [])
            trigger = _el("p", {"class": "via"}, _icon("arrow"), _el("span", None, name))
        items.append(_el("li", {"class": "step", "data-step": str(k), "data-status": status},
                         trigger, body,
                         _el("p", {"class": "step-name"}, _el("span", {"class": "step-n"}, str(k)), SP,
                             str(node.get("title") or node_id or "")),
                         _el("p", {"class": "muted expect"}, via.get("expect")) if via and via.get("expect") else None,
                         marks))
    seen = []
    for k in sorted(sigs):
        for s in sigs[k]:  # a recapture error starts with its own "step N," already
            measured = re.sub(rf"^step {k},\s*", "", str(s.get("measured") or ""), flags=re.I)
            seen.append(_el("li", None, f"Step {k}: {measured}"))
    switch = None
    if len(variants) > 1:
        sid = f"vt-{re.sub(r'[^a-z0-9]+', '-', j['id'].lower())}"
        switch = _el("p", {"class": "switch", "hidden": True}, _el("label", {"for": sid}, "Width and theme"), SP,
                     _el("select", {"id": sid, "class": "vt-switch"}, [
                         _el("option", {"value": vt, "selected": vt == default_vt}, vt.replace("x", "×", 1).replace("-", " "))
                         for vt in variants]))
    return _el("article", {"class": "journey", "data-journey": j["id"]},
               _el("div", {"class": "jhead"}, _el("h2", None, j.get("task") or j["id"]),
                   _el("ul", {"class": "facts"}, [_el("li", {"class": "alert" if isinstance(x, Markup) else None}, x)
                                                  for x in facts]), switch),
               None if walked else _el("p", {"class": "line"}, "Not walked yet: ",
                                       _cmd(f"dev visual journey walk {j['id']}")),
               _el("ol", {"class": "strip"}, items),
               _el("div", {"class": "outcome"},
                   _el("p", None, _el("strong", None, "Expected: "), j.get("expect")) if j.get("expect") else None,
                   _el("div", {"class": "seen"}, _el("strong", None, "Seen:"), _el("ul", None, seen)) if seen else None))


def _over_budget(frames: int, budget) -> bool:
    return isinstance(budget, int) and not isinstance(budget, bool) and frames > budget


def _steps_against_budget(frames: int, budget) -> str:
    if _over_budget(frames, budget):
        return f"{frames} steps, {frames - budget} over the budget of {budget}"
    return f"{frames} steps, budget {budget}" if budget else _n(frames, "step", "steps")


def _edge_to(r: Report, edge_id) -> str:
    edge = r.edges.get(edge_id)
    if edge:
        return edge.get("to")
    return str(edge_id or "").partition(">")[2]


def _icon(name: str) -> Markup:
    paths = {"arrow": "M2 8h11M9 4l4 4-4 4", "cross": "M4 4l8 8M12 4l-8 8", "diamond": "M8 2l6 6-6 6-6-6z"}
    return Markup(f'<svg class="icon icon-{name}" viewBox="0 0 16 16" aria-hidden="true" focusable="false">'
                  f'<path d="{paths[name]}"/></svg>')


def _map_view(r: Report) -> Markup:
    m = r.map
    if not m:
        return _el("section", {"class": "view", "data-view": "map", "id": "map", "aria-label": "Map"},
                   _el("p", {"class": "empty"}, "No map yet. Crawl one: ", _cmd("dev visual map crawl <url>")))
    lines = []
    if m.get("partial"):
        crawled = sum(1 for n in r.nodes.values() if n.get("origin") == "crawled" and n.get("kind") != "external")
        left = f", with {m['links_left']} links left" if isinstance(m.get("links_left"), int) else " before it followed every link"
        lines.append(_el("p", {"class": "line"}, f"Partial map: the crawl stopped at {crawled} screens{left}. "
                                                 "Orphans and dead ends may be wrong."))
    sections: dict[str, list] = {}
    for n in r.nodes.values():
        sections.setdefault(str(n.get("section") or "other"), []).append(n)
    open_on_node: dict[str, list] = {}
    open_on_edge: dict[str, list] = {}
    for f in r.findings:
        if not r.is_open(f):
            continue
        edge = (f.get("anchor") or {}).get("edge")
        if edge:
            open_on_edge.setdefault(edge, []).append(f)
        node = r.node_of(f)
        if node:
            open_on_node.setdefault(node, []).append(f)
    caps_on_node: dict[str, list] = {}
    for c in r.gallery:
        node = (c.get("map") or {}).get("node")
        if node:
            caps_on_node.setdefault(node, []).append(c)
    on_journey: dict[str, dict] = {}
    for j in r.journeys:
        steps = [s for s in j.get("steps") or [] if isinstance(s, dict)]
        on_journey.setdefault(j.get("start"), {}).setdefault(j["id"], []).append(1)
        for k, s in enumerate(steps, start=2):
            on_journey.setdefault(_edge_to(r, s.get("edge")), {}).setdefault(j["id"], []).append(k)

    def journeys_of(node_id):
        got = on_journey.get(node_id) or {}
        return "; ".join(f"{jid} {'step' if len(ks) == 1 else 'steps'} {', '.join(map(str, ks))}"
                         for jid, ks in got.items()) or "—"

    def chips(fs):
        return [_el("a", {"href": "#" + f["id"], "class": "chip",
                          "aria-label": f"{r.number[f['id']]} {f.get('severity')}: {f.get('title')}"},
                    _el("span", {"class": f"num sev-{str(f.get('severity', 'P3')).lower()}"}, str(r.number[f["id"]])),
                    SP, _el("span", None, f.get("severity")))
                for f in fs] or "—"
    diagrams = []
    for d in r.data.get("diagrams") or []:
        diagrams.append(_el("figure", {"class": "diagram"}, Markup(
            f'<picture><source srcset="map/{_esc(d["name"])}-dark.svg" media="(prefers-color-scheme: dark)">'
            f'<img src="map/{_esc(d["name"])}-light.svg" alt="{_esc("Map diagram: " + d["name"])}" loading="lazy">'
            f'</picture>'), _el("figcaption", None, d["name"])))
    head = _el("thead", None, _el("tr", None, [_el("th", {"scope": "col"}, h) for h in
                                               ("Screen", "Route", "Open findings", "Captures", "On journeys", "Notes")]))
    bodies = []
    for name in sorted(sections):
        in_section = sum(1 for n in sections[name] if n.get("kind") != "external")
        out = len(sections[name]) - in_section
        count = _n(in_section, "screen", "screens") + (f", {_n(out, 'external link', 'external links')}" if out else "")
        rows = [_el("tr", {"class": "section-row"},
                    _el("th", {"colspan": "6", "scope": "colgroup"}, name, _el("span", {"class": "muted"}, f" · {count}")))]
        for n in sorted(sections[name], key=lambda n: n["id"]):
            fs = open_on_node.get(n["id"], [])
            notes = [n.get("kind")] if n.get("kind") not in (None, "screen") else []
            if n.get("missing_since"):
                notes.append(f"Missing since v{n['missing_since']}")
            if n.get("final"):
                notes.append("final")
            if n.get("origin") == "declared":
                notes.append("declared")
            route = n.get("route") or n.get("example")
            rows.append(_el("tr", {"data-node": n["id"]},
                            _el("th", {"scope": "row"}, n.get("title") or n["id"], _el("code", {"class": "id"}, n["id"])),
                            _el("td", {"data-label": "Route"}, _el("code", None, route) if route else "—"),
                            _el("td", {"data-label": "Open findings"},
                                _el("span", {"class": "count"}, _n(len(fs), "open", "open")) if fs else None, chips(fs)),
                            _el("td", {"data-label": "Captures"},
                                str(len(caps_on_node.get(n["id"], []))) if caps_on_node.get(n["id"]) else "—"),
                            _el("td", {"data-label": "On journeys"}, journeys_of(n["id"])),
                            _el("td", {"data-label": "Notes"}, ", ".join(notes) or "—")))
        bodies.append(_el("tbody", None, rows))
    nodes = _el("div", {"class": "table-wrap"}, _el("table", {"class": "map-table"},
                _el("caption", None, "Screens"), head, bodies))
    erows = []
    for e in sorted(r.edges.values(), key=lambda e: e["id"]):
        t = e.get("trigger") or {}
        fs = open_on_edge.get(e["id"], [])
        erows.append(_el("tr", {"data-edge": e["id"]},
                         _el("td", {"class": "edge-name"}, _el("code", None, e.get("from")), " → ",
                             _el("code", None, e.get("to"))),
                         _el("td", {"data-label": "Trigger"}, f"{t.get('role') or ''} “{t.get('name') or ''}”".strip(),
                             _el("code", {"class": "id"}, t["selector"]) if t.get("selector") else None),
                         _el("td", {"data-label": "Kind"}, e.get("kind") or "—"),
                         _el("td", {"data-label": "When"}, e.get("when") or "—"),
                         _el("td", {"data-label": "Open findings"}, chips(fs))))
    edges = _el("div", {"class": "table-wrap"}, _el("table", {"class": "map-table"},
                _el("caption", None, _n(len(erows), "edge", "edges")),
                _el("thead", None, _el("tr", None, [_el("th", {"scope": "col"}, h) for h in
                                                    ("From → to", "Trigger", "Kind", "When", "Open findings")])),
                _el("tbody", None, erows)))
    screens = sum(1 for n in r.nodes.values() if n.get("kind") != "external")
    external = len(r.nodes) - screens
    section_list = _el("p", {"class": "line"}, _n(screens, "screen", "screens")
                       + (f" and {_n(external, 'external link', 'external links')}" if external else "")
                       + f", in {_n(len(sections), 'section', 'sections')}: {', '.join(sorted(sections))}.")
    drawing = diagrams or _el("p", {"class": "line"}, "No drawing yet. Draw it: ", _cmd("dev visual map draw"))
    return _el("section", {"class": "view", "data-view": "map", "id": "map", "aria-label": "Map"},
               lines, section_list, drawing, nodes, edges)


def _gallery_view(r: Report) -> Markup:
    states: dict[str, list] = {}
    for c in r.gallery:
        states.setdefault(str(c.get("state") or "capture"), []).append(c)
    columns = []
    for c in sorted(r.gallery, key=r.order):
        if _vt(c) not in [v for v, _ in columns]:
            columns.append((_vt(c), _vt_label(c)))
    rows = []
    for name, cs in states.items():
        nodes = sorted({(r.nodes.get((c.get("map") or {}).get("node")) or {}).get("title") or
                        (c.get("map") or {}).get("node") or "" for c in cs} - {""})
        cells = []
        for vt, _ in columns:
            here = sorted((c for c in cs if _vt(c) == vt), key=r.order)
            cells.append(_el("td", {"data-filter-item": "", "data-vt": vt}, [_thumb(r, c) for c in here]))
        rows.append(_el("tr", {"class": "state", "data-state": name},
                        _el("th", {"scope": "row"}, _el("span", {"class": "sname"}, name),
                            _el("span", {"class": "muted"}, ", ".join(nodes)) if nodes else None), cells))
    grid = _el("div", {"class": "table-wrap"}, _el("table", {"class": "gallery"},
               _el("thead", None, _el("tr", None, _el("th", {"scope": "col"}, "State"),
                                      [_el("th", {"scope": "col", "data-filter-item": "", "data-vt": vt}, label)
                                       for vt, label in columns])),
               _el("tbody", None, rows)))
    lines = []
    if r.scope in ("section", "product"):
        loose = sum(1 for c in r.gallery if not (c.get("map") or {}).get("node"))
        if loose:
            lines.append(_el("p", {"class": "line"},
                             "1 capture is not on the map yet. It attaches when the map has its screen." if loose == 1 else
                             f"{loose} captures are not on the map yet. They attach when the map has their screen."))
    vts = []
    for c in sorted(r.gallery, key=r.order):
        if (_vt(c), _vt_label(c)) not in vts:
            vts.append((_vt(c), _vt_label(c)))
    groups = [("severity", "Severity", [(s, s) for s in SEVERITIES if any(f.get("severity") == s for f in r.findings)]),
              ("status", "Status", _status_options(r.findings)),
              ("vt", "Width and theme", vts)]
    if not r.gallery:
        return _el("section", {"class": "view", "data-view": "gallery", "id": "gallery", "aria-label": "Gallery"},
                   _no_captures(r))
    return _el("section", {"class": "view with-rail", "data-view": "gallery", "id": "gallery", "aria-label": "Gallery"},
               _filters(r, "gallery", groups),
               _el("div", {"class": "main"}, lines, grid, _empty_filter(),
                   _coverage(r) if r.scope == "journey" else None))


def _no_captures(r: Report) -> Markup:
    return _el("p", {"class": "empty"}, "No captures in this audit yet. Take the first: ",
               _cmd(f"dev visual capture <url> --audit {r.audit.get('id')}"))


def _single_view(r: Report, name: str, label: str) -> Markup:
    # one capture per width and theme: the view is the capture itself, its picture at its CSS width with its pins,
    # tools and findings, one width and theme at a time, the others a link away
    caps = sorted(r.gallery, key=r.order)
    switch = _el("nav", {"class": "vt-links", "aria-label": "Width and theme"},
                 _el("ul", None, [_el("li", None, _el("a", {"href": "#" + c["id"]}, _vt_label(c))) for c in caps])
                 ) if len(caps) > 1 else None
    return _el("section", {"class": "view", "data-view": name, "id": name, "aria-label": label, "data-single": True},
               switch, [_capture_page(r, c, inline=True) for c in caps], _no_findings(r), _coverage(r))


def _capture_view(r: Report) -> Markup:
    # the screen scope's first view: its captures in a row per width and theme, the findings beside them
    if not r.gallery:
        return _el("section", {"class": "view", "data-view": "capture", "id": "capture", "aria-label": "Capture"},
                   _no_captures(r))
    if r.single:
        return _single_view(r, "capture", "Capture")
    side = _el("div", {"class": "side"}, _el("h2", None, _n(len(r.findings), "finding", "findings")),
               _el("ol", {"class": "flist"}, [_el("li", None, _el("a", {"href": "#" + f["id"]}, _finding_head(r, f)))
                                              for f in r.findings]) if r.findings else _no_findings(r))
    return _el("section", {"class": "view", "data-view": "capture", "id": "capture", "aria-label": "Capture"},
               _el("div", {"class": "split"}, _el("div", {"class": "thumbs big"},
                                                 [_thumb(r, c) for c in sorted(r.gallery, key=r.order)]), side),
               _coverage(r))


def _states_view(r: Report) -> Markup:
    # the component scope's first view: states as columns, widths and themes as rows, the element cropped
    if not r.gallery:
        return _el("section", {"class": "view", "data-view": "states", "id": "states", "aria-label": "States"},
                   _no_captures(r))
    if r.single:
        return _single_view(r, "states", "States")
    states = list((r.audit.get("matrix") or {}).get("states") or [])
    for c in r.gallery:
        if c.get("state") not in states:
            states.append(c.get("state"))
    rows: dict[str, dict] = {}
    labels = {}
    for c in sorted(r.gallery, key=r.order):
        rows.setdefault(_vt(c), {})[c.get("state")] = c
        labels[_vt(c)] = _vt_label(c)
    body = []
    for vt, cells in rows.items():
        tds = []
        for st in states:
            c = cells.get(st)
            if not c:
                tds.append(_el("td", None, "—"))
                continue
            el = c.get("element") or {}
            box = el.get("box") if isinstance(el.get("box"), list) and len(el.get("box")) == 4 else None
            sha = el.get("sha256")
            fs = r.on_capture.get(c["id"], [])
            dots = []
            for f in fs:
                reg = _region(f)
                pos = r.css.cls("left:6px;top:6px")
                if reg and box:
                    cw, ch = box[2] + 2 * CROP_MARGIN, box[3] + 2 * CROP_MARGIN
                    pos = _geo(r, [reg[0] - box[0] + CROP_MARGIN, reg[1] - box[1] + CROP_MARGIN, 0, 0], cw, ch, pin=True)
                dots.append(_el("span", {"class": f"dot sev-{str(f.get('severity', 'P3')).lower()} {pos}",
                                         "aria-hidden": "true", "data-n": str(r.number[f["id"]])}))
            if _picture_ok(r, sha) and box:
                cw, ch = box[2] + 2 * CROP_MARGIN, box[3] + 2 * CROP_MARGIN
                pic = _el("span", {"class": f"frame {_aspect(r, cw, ch)}"}, _img(sha, cw, ch), dots)
            else:
                pic = _el("span", {"class": "frame missing"}, _el("span", {"class": "missing-note"}, "Not on this Mac"), dots)
            tds.append(_el("td", None, _el("a", {"class": "cell-link", "href": "#" + c["id"],
                                                 "aria-label": f"{st}, {labels[vt]}, {_n(len(fs), 'finding', 'findings')}"},
                                           pic)))
        body.append(_el("tr", None, _el("th", {"scope": "row"}, labels[vt]), tds))
    grid = _el("div", {"class": "table-wrap"}, _el("table", {"class": "grid"},
               _el("caption", None, r.audit.get("element") or "Component"),
               _el("thead", None, _el("tr", None, _el("th", None, ""), [_el("th", {"scope": "col"}, s) for s in states])),
               _el("tbody", None, body)))
    return _el("section", {"class": "view", "data-view": "states", "id": "states", "aria-label": "States"},
               grid, _no_findings(r) if not r.findings else None, _coverage(r))


def _no_findings(r: Report):
    rows, total, open_, first = r.coverage()
    if r.findings:
        return None
    if open_:
        return _el("p", {"class": "empty"}, f"No findings yet. {open_} of {total} passes are not recorded. Work "
                                            f"through them in order, starting with {PASS_NAME.get(first, first)}.")
    return _el("p", {"class": "empty"}, f"No findings: every pass is recorded clean on all "
                                        f"{_n(len(r.gallery), 'capture', 'captures')}.")


def _all_closed(r: Report):
    c = r.counts()
    if r.findings and not c["open"]:
        return _el("p", {"class": "line ok"}, f"Every finding is closed: {c['verified']} verified on the same evidence, "
                                              f"{c['closed']} closed with reasons.")
    return None


def _findings_view(r: Report) -> Markup:
    fs = r.findings
    passes = [(p, PASS_NAME[p]) for p, _ in STRUCTURE_PASSES + SCREEN_PASSES if any(f.get("pass") == p for f in fs)]
    schemes = sorted({str(x).split(":", 1)[0] for f in fs for x in f.get("rule") or []})
    sources = sorted({str(f.get("source") or "").split(":", 1)[0] for f in fs} - {""})
    groups = [("severity", "Severity", [(s, s) for s in SEVERITIES if any(f.get("severity") == s for f in fs)]),
              ("status", "Status", _status_options(fs)), ("pass", "Pass", passes),
              ("rule", "Rule", [(s, s) for s in schemes]), ("source", "Source", [(s, s) for s in sources])]
    models = []
    for c in sorted(r.captures.values(), key=lambda c: c["id"]):
        for s in (c.get("checks") or {}).get("signals") or []:
            t = s.get("triage") if isinstance(s, dict) else None
            if isinstance(t, dict) and t.get("as") == "dismissed" and t.get("model") not in (None, "", "human"):
                models.append(_el("li", None, _el("code", None, f"{c['id']}#{s.get('n')}"),
                                  f" {s.get('check')} dismissed by {r.person(t.get('by'))} with {t.get('model')} "
                                  f"{_when(t.get('at'))}: {t.get('why') or ''}"))
        for jd in c.get("judgment") or []:
            if isinstance(jd, dict) and jd.get("model") not in (None, "", "human"):
                models.append(_el("li", None, _anchor(c["id"]),
                                  f" {PASS_NAME.get(jd.get('pass'), jd.get('pass'))} judged by {r.person(jd.get('by'))} "
                                  f"with {jd.get('model')} {_when(jd.get('at'))}: {jd.get('note') or ''}"))
    for s in r.structure:
        t = s.get("triage")
        if isinstance(t, dict) and t.get("as") == "dismissed" and t.get("model") not in (None, "", "human"):
            models.append(_el("li", None, _el("code", None, f"{r.audit.get('id')}#{s.get('n')}"),
                              f" {s.get('check')} dismissed by {r.person(t.get('by'))} with {t.get('model')}: "
                              f"{t.get('why') or ''}"))
    untriaged = []
    for s in r.structure:
        if not s.get("triage"):
            untriaged.append(_el("li", None, _el("code", None, f"{r.audit.get('id')}#{s.get('n')}"),
                                 f" {s.get('check')} ({PASS_NAME.get(s.get('pass'), s.get('pass'))}): {s.get('measured')}"))
    for c in r.gallery:
        for s in (c.get("checks") or {}).get("signals") or []:
            if isinstance(s, dict) and not s.get("triage"):
                untriaged.append(_el("li", None, _el("code", None, f"{c['id']}#{s.get('n')}"), " ",
                                     _anchor(c["id"], _vt_label(c)),
                                     f" {s.get('axe_rule') or s.get('check')}: {s.get('measured')} "
                                     f"(threshold {s.get('threshold')})"))
    main = _el("div", {"class": "main"},
               _all_closed(r), _no_findings(r) if not fs else None,
               [_finding_group(r, k, [f for f in fs if _group(f) == k]) for k in (0, 1, 2)] if fs else None,
               _empty_filter() if fs else None,
               _el("section", {"class": "signals"}, _el("h2", None, "Signals not triaged"),
                   _el("ul", None, untriaged)) if untriaged else None,
               _el("section", {"class": "models"}, _el("h2", None, "Made by a model"),
                   _el("p", {"class": "muted"}, "Signal dismissals and judgments a model made, by member and model."),
                   _el("ul", None, models)) if models else None)
    return _el("section", {"class": "view" + (" with-rail" if fs else ""), "data-view": "findings", "id": "findings",
                           "aria-label": "Findings"},
               _filters(r, "findings", groups) if fs else None, main)


def _finding_group(r: Report, k: int, fs: list):
    # the findings still to work on, then "Verified (n)" and "Closed (n)" with their heading
    if not fs:
        return None
    head = None if k == 0 else _el("h2", {"class": "ghead"}, f"{('Verified', 'Closed')[k - 1]} ({len(fs)})")
    return _el("div", {"class": "fgroup", "data-group": ("open", "verified", "closed")[k]}, head,
               _el("div", {"class": "flist"}, [_finding_article(r, f) for f in fs]))


def _coverage(r: Report) -> Markup:
    rows, total, open_, _ = r.coverage()
    structure, screen = COVERAGE[r.scope]
    first = 1 if screen[0][0] == "task" else 2

    def cell(p, result, note):
        res = {"findings": "findings", "clean": "clean", "n/a": "na"}.get(result, "open")
        text = f"{PASS_NAME.get(p, p)}: " + {"findings": "findings", "clean": "clean", "na": "n/a",
                                             "open": "not recorded"}[res]
        return _el("td", {"class": "cell", "data-result": res, "title": note or None},
                   _el("span", {"class": "mark-" + res, "aria-hidden": "true"}), _el("span", {"class": "sr"}, text))

    def col(label, name, short=None):
        return _el("th", {"scope": "col", "title": name},
                   _el("span", {"class": "short", "aria-hidden": "true"}, short) if short else None,
                   _el("span", {"class": "n" if short else None, "aria-hidden": "true"}, label),
                   _el("span", {"class": "sr"}, name))
    tables = []
    for kind, c, cells in rows:
        if kind == "structure":
            tables.append(_el("div", {"class": "table-wrap"}, _el("table", {"class": "cov"},
                _el("caption", None, "Structure passes, once for the audit"),
                _el("thead", None, _el("tr", None, [col(p, PASS_NAME[p]) for p, _, _ in cells])),
                _el("tbody", None, _el("tr", None, [cell(*x) for x in cells])))))
    body = []
    for kind, c, cells in rows:
        if kind == "capture":
            body.append(_el("tr", None, _el("th", {"scope": "row"}, _anchor(c["id"], c.get("state") or c["id"]),
                                            _el("span", {"class": "muted"}, f" {_vt_label(c)}")),
                            [cell(*x) for x in cells]))
    if body:
        tables.append(_el("div", {"class": "table-wrap"}, _el("table", {"class": "cov"},
            _el("caption", None, "Screen passes, on each capture"),
            _el("thead", None, _el("tr", None, _el("th", {"scope": "col"}, "Capture"),
                                   [col(str(i), name, SHORT_NAME.get(p))
                                    for i, (p, name) in enumerate(screen, start=first)])),
            _el("tbody", None, body))))
    names = _el("p", {"class": "muted names"}, "; ".join(f"{i} {name}" for i, (_, name) in enumerate(screen, start=first)))
    skipped = [c for c in r.gallery if c.get("kind", "web") == "web" and ((c.get("checks") or {}).get("skipped") or {}).get("axe")]
    axe = None
    if skipped:
        n = len(skipped)
        axe = _el("p", {"class": "line"}, f"The axe accessibility rules did not run on {_n(n, 'capture', 'captures')}: "
                                          "axe-core is not installed (or its file does not match 4.14.0). Contrast and "
                                          f"the 24 px target minimum were not checked on {'it' if n == 1 else 'them'}; "
                                          "devkit's other checks ran.")
    legend = _el("ul", {"class": "legend"}, [_el("li", None, _el("span", {"class": f"mark-{k}", "aria-hidden": "true"}),
                                                 SP, t) for k, t in (("findings", "findings"), ("clean", "clean"),
                                                                     ("na", "n/a"), ("open", "not recorded"))])
    # the verdict's link leads here; section and product audits have the Coverage view of that id
    return _el("div", {"class": "coverage", "id": None if "coverage" in dict(VIEWS[r.scope]) else "coverage"},
               _el("h2", None, "Pass coverage"),
               _el("p", {"class": "line"}, f"{open_} of {total} passes are not recorded." if open_ else
                   f"All {total} passes are recorded."),
               legend, names if body else None, tables, axe)


def _coverage_view(r: Report) -> Markup:
    return _el("section", {"class": "view", "data-view": "coverage", "id": "coverage", "aria-label": "Coverage"},
               _coverage(r))


def _capture_page(r: Report, c: dict, inline=False) -> Markup:
    # the picture shows at its size in CSS px; pins, boxes, outlines, alignment lines, and the fold are placed in the
    # regions' unit, never in the picture's device pixels
    img = c.get("image") or {}
    W, H = _capture_size(c)
    fold = _num(img.get("fold"), 0)
    sha = img.get("sha256")
    fs = r.on_capture.get(c["id"], [])
    marks, pins = [], []
    loose = 0
    for f in fs:
        reg = _region(f)
        sev = str(f.get("severity", "P3")).lower()
        if reg:
            marks.append(_el("span", {"class": f"fbox sev-{sev} {_geo(r, reg, W, H)}", "data-f": f["id"]}))
            pos = _geo(r, reg, W, H, pin=True)
        else:
            pos = r.css.cls(f"left:{_f(14 / max(W, 1) * 100 + loose * 0)}%;top:{_f(14 / max(H, 1) * 100)}%;"
                            f"margin-left:{loose * 30}px")
            loose += 1
        pins.append(_el("button", {"type": "button", "class": f"pin sev-{sev} {pos}", "data-f": f["id"],
                                   "aria-label": _pin_label(r, f)}, str(r.number[f["id"]])))
    outlines, align = [], []
    elements = (r.data.get("descriptions") or {}).get(c["id"]) or []
    for e in elements:
        outlines.append(_el("span", {"class": f"box {_geo(r, e['box'], W, H)}"},
                            _el("span", {"class": "olabel"}, f"{e['id']} {e['name']}")))
    for axis, idx in (("v", 0), ("v", 2), ("h", 1)):
        edges: dict[int, int] = {}
        for e in elements:
            b = e["box"]
            v = round(b[0] + b[2]) if idx == 2 else round(b[idx])
            edges[v] = edges.get(v, 0) + 1
        for v, n in sorted(edges.items()):
            if n >= 3 and len(align) < 40:
                pos = (f"left:{_f(v / max(W, 1) * 100)}%" if axis == "v" else f"top:{_f(v / max(H, 1) * 100)}%")
                align.append(_el("span", {"class": f"aline {axis} {r.css.cls(pos)}"}))
    state = r.pictures.get(sha)
    who = c.get("by") or {}
    if state == "ok":
        picture = _img(sha, W, H, alt=f"Capture {c['id']} of {c.get('state') or 'a screen'}, {_vt_label(c)}", lazy=True)
        note = None
    else:
        picture = None
        if state == "mismatch":
            text = ["The picture in this Mac's store does not match the capture's record. Capture it again: ",
                    _cmd(f"dev visual capture --again {c['id']}")]
        else:
            text = [f"Captured on {who.get('host') or 'another Mac'} by {r.person(who)}. The picture stays on that "
                    "Mac. Capture it here: ", _cmd(f"dev visual capture --again {c['id']}")]
        note = _el("p", {"class": "missing-note"}, text)
    fold_line = (_el("span", {"class": f"fold {r.css.cls('top:' + _f(fold / max(H, 1) * 100) + '%')}"},
                     _el("span", None, f"fold {int(fold)} px")) if 0 < fold < H else None)
    shot = _el("div", {"class": f"shot show-fold {r.css.cls('max-width:' + _f(W) + 'px')}"},
               _el("div", {"class": f"canvas {_aspect(r, W, H)}" + ("" if picture else " missing")},
                   picture, note, _el("div", {"class": "outlines"}, outlines) if outlines else None,
                   _el("div", {"class": "align"}, align) if align else None, fold_line,
                   _el("div", {"class": "marks"}, marks), pins))
    tools = _el("div", {"class": "tools", "role": "toolbar", "aria-label": "Tools", "hidden": True}, [
        _el("button", {"type": "button", "data-tool": t, "aria-pressed": "true" if t == "fold" else "false",
                       "disabled": t in ("outlines", "align") and not elements}, label)
        for t, label in (("squint", "Squint"), ("gray", "Grayscale"), ("outlines", "Outlines"),
                         ("align", "Alignment"), ("fold", "Fold"))])
    back = {"screen": "capture", "component": "states"}.get(r.scope, "gallery")
    node = (c.get("map") or {}).get("node")
    facts = [_vt_label(c), c["id"]]
    if node:
        facts.append((r.nodes.get(node) or {}).get("title") or node)
    if (c.get("map") or {}).get("journey"):
        facts.append(f"{c['map']['journey']} step {c['map'].get('step')}")
    if c.get("again_of"):
        facts.append(f"recaptured from {c['again_of']}")
    labels = []
    if c.get("kind") == "image":
        labels.append(_el("p", {"class": "line"}, "External picture, no recipe. Its findings are verified by eye "
                                                  "against a new capture."))
    if c.get("experiment"):
        labels.append(_el("p", {"class": "line"}, "Experiment capture: it proves a cause and never verifies a finding."))
    flist = _el("ol", {"class": "flist"}, [
        _el("li", None, _el("button", {"type": "button", "class": "fpick", "data-f": f["id"]}, _finding_head(r, f)))
        for f in fs]) if fs else _el("p", {"class": "muted"}, "No findings on this capture.")
    details = [_el("div", {"class": "fdetail", "data-detail": f["id"], "hidden": True},
                   _el("div", {"class": "dhead"}, _el("h3", None, f.get("title")),
                       _el("button", {"type": "button", "class": "fclose"}, "Back to the capture")),
                   _detail_body(r, f),
                   _el("p", None, _el("a", {"href": "#" + f["id"]}, "Open in Findings")))
               for f in fs]
    side = _el("aside", {"class": "side", "aria-label": "Findings on this capture"},
               _el("h3", None, _n(len(fs), "finding", "findings") + " on this capture"), flist, details,
               _recipe(r, c))
    return _el("section", {"class": "capture-view", "id": c["id"], "aria-label": f"Capture {c['id']}"},
               _el("div", {"class": "chead"}, None if inline else _el(
                   "a", {"href": "#" + back, "class": "back"}, _icon("arrow"), dict(VIEWS[r.scope]).get(back, "Gallery")),
                   _el("h2", None, c.get("state") or c["id"]),
                   _el("ul", {"class": "facts"}, [_el("li", None, x) for x in facts])),
               labels, _el("div", {"class": "split " + r.css.cls(
                   f"grid-template-columns:minmax(0,{_f(W + 26)}px) minmax(300px,400px)")},
                   _el("div", {"class": "pic"}, tools, shot), side))


def _recipe(r: Report, c: dict) -> Markup:
    rec = c.get("recipe")
    who = c.get("by") or {}
    src = c.get("source") or {}
    rows = []

    def row(label, *value):
        if value and any(v not in (None, "", []) for v in value):
            rows.append(_el("div", {"class": "row"}, _el("dt", None, label + ":"), SP, _el("dd", None, *value)))
    row("Source", _el("code", None, src.get("url") or src.get("file")) if (src.get("url") or src.get("file")) else None)
    if rec:
        steps = [s for s in rec.get("steps") or []]
        row("Steps", _el("ol", {"class": "steps"}, [_el("li", None, _el("code", None, s)) for s in steps]) if steps else None)
        vp = rec.get("viewport") or []
        row("Viewport", f"{vp[0]}×{vp[1]} at {rec.get('dpr') or 1}x" if len(vp) == 2 else None)
        row("Theme", rec.get("theme"))
        row("Locale", f"{rec.get('locale')}, {rec.get('timezone')}" if rec.get("locale") else None)
        row("Data", rec.get("data"))
        row("Signed in with", _el("code", None, rec["storage_state"]) if rec.get("storage_state") else None)
        row("Extra CSS", _el("code", None, rec["css"]) if rec.get("css") else None)
    env = c.get("env") or {}
    row("Taken", f"{_when(c.get('taken'))} by {r.person(who)}" + (f" on {who['host']}" if who.get("host") else ""))
    row("Commit", _el("code", None, env["commit"]) if env.get("commit") else None)
    signals = [s for s in (c.get("checks") or {}).get("signals") or [] if isinstance(s, dict)]
    if signals:
        row("Signals", f"{len(signals)}, {sum(1 for s in signals if not s.get('triage'))} not triaged")
    again = _el("p", None, "Reproduce: ", _cmd(f"dev visual capture --again {c['id']}")) if rec else None
    return _el("div", {"class": "recipe"}, _el("h3", None, "Recipe"), again, _el("dl", None, rows))


# --------------------------------------------------------------------------- page

CSS = r"""
:root{color-scheme:light dark;
--paper:#f3f1eb;--paper-2:#fbfaf7;--paper-3:#e9e6de;--ink:#272820;--ink-2:#62645b;--strong:#10110f;--line:#d8d4ca;
--line-soft:#e3dfd6;--amber:#ffb34a;--amber-ink:#855414;--red:#d03b3b;--red-text:#a52a2a;--green:#2f7d3f;
--surface:var(--paper);--raised:var(--paper-2);--sunken:var(--paper-3);--focus:var(--amber-ink);
--s0:#d03b3b;--s1:#e8961c;--s2:#62645b;--s3:#a29d90;
--font:"Manrope","Manrope Variable",system-ui,-apple-system,"Segoe UI",Arial,sans-serif;
--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--surface:#10110f;--raised:#191a17;--sunken:#22231f;--ink:#f3f1eb;
--ink-2:#bfb8ab;--strong:#ffffff;--line:#474a42;--line-soft:#2b2d28;--red-text:#f29b95;--green:#6fbf7f;
--focus:var(--amber);--s0:#ef5b5b;--s1:#ffb34a;--s2:#bfb8ab;--s3:#77796f}}
*,*::before,*::after{box-sizing:border-box}
[hidden]{display:none!important}
html{background:var(--surface);color:var(--ink);font:15px/1.5 var(--font);font-variant-ligatures:no-contextual;
-webkit-text-size-adjust:100%}
body{margin:0;min-height:100vh;background:var(--surface)}
::selection{background:var(--amber);color:#10110f}
a{color:inherit;text-decoration-thickness:1px;text-underline-offset:3px}
a:hover{text-decoration-thickness:2px}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:3px}
code{font:13px/1.4 var(--mono);overflow-wrap:anywhere}
.cmd{display:block;contain:inline-size;margin-top:4px;padding:2px 0;white-space:nowrap;overflow-wrap:normal;
overflow-x:auto;scrollbar-width:thin}
h1,h2,h3,h4{font-weight:600;line-height:1.25;margin:0;text-wrap:balance}
h1{font-size:28px;letter-spacing:-.01em;max-width:40ch}
h2{font-size:19px}h3,h4{font-size:15px}
p{margin:0}
ul,ol{margin:0;padding:0;list-style:none}
.muted{color:var(--ink-2)}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}
.icon{width:16px;height:16px;flex:none;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}
.top{padding:24px 32px 0;border-bottom:1px solid var(--line);background:var(--raised)}
.brand{display:flex;justify-content:space-between;gap:16px;color:var(--ink-2);font-size:13px;margin-bottom:12px}
.verdict,.facts{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 20px;margin-top:8px}
.verdict{font-size:15px;font-weight:600;color:var(--strong)}
.verdict.has-blocking li:first-child{background:var(--amber);color:#10110f;padding:1px 10px;border-radius:4px}
.verdict a{color:var(--ink-2);font-weight:500}
.facts{font-size:13px;color:var(--ink-2)}
.line{margin-top:10px;font-size:13px;color:var(--ink)}
.line.ok{color:var(--green)}
.views{position:relative;margin-top:16px;overflow-x:auto;scrollbar-width:thin}
.views ul{display:flex;gap:4px}
.views a{display:block;padding:10px 12px;text-decoration:none;color:var(--ink-2);border-bottom:2px solid transparent;white-space:nowrap}
.views a:hover{color:var(--ink)}
.views a[aria-current=page]{color:var(--strong);border-bottom-color:var(--strong);font-weight:600}
.views a:focus-visible{outline-offset:-2px}
@property --fade-start{syntax:"<length>";inherits:false;initial-value:0px}
@property --fade-end{syntax:"<length>";inherits:false;initial-value:0px}
.views{--fade-size:24px;scroll-padding-inline:var(--fade-size);transition:--fade-start .2s,--fade-end .2s;
mask:linear-gradient(#000,#000) no-clip subtract,
 linear-gradient(to right,#000,transparent) 0 0/var(--fade-start) 100% no-repeat no-clip,
 linear-gradient(to right,transparent,#000) 100% 0/var(--fade-end) 100% no-repeat no-clip}
.views.more-start{--fade-start:var(--fade-size)}
.views.more-end{--fade-end:var(--fade-size)}
main{padding:24px 32px 64px}
.view+.view,.capture-view+.capture-view,.view+.capture-view{margin-top:48px}
.js .view+.view,.js .capture-view{margin-top:0}
.empty{padding:24px 0;max-width:72ch}
.with-rail{display:grid;grid-template-columns:200px minmax(0,1fr);gap:32px;align-items:start}
.filters{position:sticky;top:16px;display:flex;flex-direction:column;gap:16px;font-size:13px}
.filters fieldset{border:0;margin:0;padding:0;display:flex;flex-direction:column;gap:4px}
.filters legend{font-weight:600;color:var(--strong);margin-bottom:4px;padding:0}
.filters label{display:flex;align-items:center;gap:6px;min-height:28px;cursor:pointer}
.filters input{accent-color:var(--strong);width:16px;height:16px;margin:0}
.sheet-head{display:none}
button{font:inherit;color:inherit}
.dhead{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.fclose{flex:none;white-space:nowrap}
.filters-clear,.clear-filters,.filters-open,.filters-close,.fclose,.tools button{background:var(--raised);border:1px solid var(--line);
border-radius:6px;padding:6px 12px;min-height:36px;cursor:pointer}
.filters-clear:hover,.clear-filters:hover,.filters-open:hover,.tools button:hover{background:var(--sunken)}
.filters-clear{align-self:flex-start}
.empty-filter{padding:24px 0;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.state+.state{margin-top:28px}
.state h3{margin-bottom:10px}
.thumbs{display:flex;gap:16px;overflow-x:auto;padding:4px 4px 8px;align-items:flex-start}
.thumb{display:flex;flex-direction:column;gap:6px;text-decoration:none;flex:none;width:min-content}
.thumb .frame{height:180px;width:auto}
.thumbs.big .thumb .frame{height:300px}
.frame{position:relative;display:block;overflow:hidden;outline:1px solid var(--line);border-radius:4px;background:var(--sunken)}
.frame img{position:absolute;inset:0 auto auto 0;width:100%;height:auto;display:block}
.frame.missing{display:grid;place-items:center;min-width:120px;padding:8px;text-align:center}
.missing-note{font-size:13px;color:var(--ink-2);max-width:40ch}
.thumb-label{font-size:13px;color:var(--ink-2)}
.thumb>.frame{align-self:flex-start}
.thumb-label .muted{white-space:nowrap}
.thumb:hover .frame{outline-color:var(--ink-2)}
.dot,.pin,.num{position:absolute;display:grid;place-items:center;border-radius:50%;font-weight:600;
background:#fbfaf7;color:#10110f;font-variant-numeric:tabular-nums;line-height:1}
.dot{width:18px;height:18px;font-size:11px;transform:translate(-50%,-50%);box-shadow:0 0 0 2px var(--ring),0 0 0 3px #fff,0 0 0 4px #10110f}
.dot::before{content:attr(data-n)}
.num{position:static;width:22px;height:22px;font-size:12px;flex:none;box-shadow:0 0 0 2px var(--ring)}
.sev-p0{--ring:var(--s0)}.sev-p1{--ring:var(--s1)}.sev-p2{--ring:var(--s2)}.sev-p3{--ring:var(--s3)}
.sev{display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:600;padding:1px 7px;border:1px solid var(--line);border-radius:999px;white-space:nowrap}
.sev::before{content:"";width:8px;height:8px;border-radius:50%;background:var(--ring)}
.tags{display:inline-flex;gap:6px;align-items:center}
.status{font-size:12px;font-weight:600;padding:1px 7px;border:1px solid var(--line);border-radius:999px;white-space:nowrap;
color:var(--ink-2)}
.status-reopened{color:var(--red-text);border-color:currentColor}
.status-verified{color:var(--green);border-color:currentColor}
.fgroup+.fgroup{margin-top:28px}
.ghead{font-size:15px;padding-bottom:4px;border-bottom:1px solid var(--line);color:var(--ink-2)}
.journey+.journey{margin-top:40px;padding-top:32px;border-top:1px solid var(--line)}
.jhead{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 24px;margin-bottom:14px}
.jhead .facts{margin:0}
.facts li.alert{color:var(--red-text);font-weight:600}
.alert-fact{display:inline-flex;align-items:center;gap:4px}
.alert-fact .icon{width:12px;height:12px}
.switch{margin-left:auto;font-size:13px;display:flex;gap:8px;align-items:center}
.switch select{font:inherit;padding:4px 8px;border:1px solid var(--line);border-radius:6px;background:var(--raised);color:var(--ink)}
.strip{display:flex;gap:0;overflow-x:auto;padding:4px 4px 12px;align-items:flex-start}
.step{flex:none;display:flex;flex-direction:column;gap:6px;width:min-content;position:relative}
.step+.step{padding-left:84px}
.via{position:absolute;left:0;top:42px;width:84px;display:flex;flex-direction:column;align-items:center;gap:2px;
font-size:12px;color:var(--ink-2);text-align:center;padding:0 6px;overflow-wrap:anywhere}
.via .icon{width:28px;height:16px}
.step .thumb .frame{height:120px}
.step .thumb-label{display:none}
.step-name{font-size:13px;font-weight:600;white-space:nowrap}
.step-n{display:inline-grid;place-items:center;min-width:20px;height:20px;border-radius:50%;border:1px solid var(--line);font-size:11px;margin-right:2px}
.expect{font-size:12px;max-width:24ch}
.mark{font-size:12px;display:flex;gap:4px;align-items:center;max-width:26ch}
.mark.lost{color:var(--red-text);font-weight:600}
.mark .icon{width:12px;height:12px}
.empty-frame,.broken-frame{height:120px;width:192px;display:flex;flex-direction:column;justify-content:center;gap:6px;padding:12px;
border:1px dashed var(--line);border-radius:4px;font-size:13px;color:var(--ink-2)}
.broken-frame{border:2px solid var(--red);color:var(--ink);width:240px;height:auto;min-height:120px}
.broken-frame .icon{color:var(--red);width:20px;height:20px}
.outcome{margin-top:6px;font-size:13px;display:flex;flex-direction:column;gap:4px;max-width:90ch}
.seen{display:flex;gap:6px;align-items:baseline}
.seen li{color:var(--red-text)}
.table-wrap{position:relative;overflow-x:auto;margin-top:20px;max-width:100%;min-width:0}
.coverage>*,.main{min-width:0}
.section-row th{padding-top:20px;font-size:15px;color:var(--strong);border-bottom-color:var(--line)}
.map-table{table-layout:fixed}
.map-table code{overflow-wrap:break-word}
.map-table th:first-child,.map-table td:first-child{width:22%}
.count{display:block;margin-bottom:4px}
.gallery th,.gallery td{padding:0 20px 24px 0;border:0}
.gallery thead th{padding-bottom:10px}
.gallery th[scope=row]{width:150px}
.sname{display:block;font-weight:600;color:var(--strong)}
.names{font-size:13px;max-width:90ch}
table{border-collapse:collapse;font-size:13px;width:100%}
caption{text-align:left;font-weight:600;font-size:15px;color:var(--strong);padding-bottom:8px}
th,td{text-align:left;vertical-align:top;padding:8px 12px 8px 0;border-bottom:1px solid var(--line-soft)}
thead th{color:var(--ink-2);font-weight:600;border-bottom-color:var(--line)}
tbody th{font-weight:600}
.id{display:block;color:var(--ink-2);font-size:12px;font-weight:400}
.chip{display:inline-flex;align-items:center;gap:4px;margin:0 10px 4px 0;text-decoration:none;font-size:12px;font-weight:600}
.chip .num{width:20px;height:20px;font-size:11px}
.split{display:grid;grid-template-columns:minmax(0,1fr) 380px;gap:32px;align-items:start}
.chead{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 20px;margin-bottom:16px}
.chead .facts{margin:0}
.vt-links ul{display:flex;flex-wrap:wrap;gap:4px 16px;margin-bottom:16px;font-size:13px}
.vt-links a{color:var(--ink-2)}
.vt-links a[aria-current=true]{color:var(--strong);font-weight:600}
.back{display:inline-flex;gap:6px;align-items:center;font-size:13px;text-decoration:none;color:var(--ink-2)}
.back .icon{transform:scaleX(-1)}
.tools{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
.tools button{min-height:32px;padding:4px 10px;font-size:13px}
.tools button[aria-pressed=true]{background:var(--strong);color:var(--surface);border-color:var(--strong)}
.tools button:disabled{opacity:.45;cursor:not-allowed}
.shot{position:relative;padding:12px}
.canvas{position:relative;width:100%;background:var(--sunken);outline:1px solid var(--line)}
.canvas>img{position:absolute;inset:0;width:100%;height:100%;display:block}
.canvas.missing{display:grid;place-items:center;padding:24px;text-align:center}
.shot.squint .canvas>img{filter:blur(6px)}
.shot.gray .canvas>img{filter:grayscale(1)}
.shot.squint.gray .canvas>img{filter:blur(6px) grayscale(1)}
.outlines,.align{position:absolute;inset:0;display:none;pointer-events:none}
.shot.show-outlines .outlines,.shot.show-align .align{display:block}
.box{position:absolute;outline:1px solid #2563eb;background:rgba(37,99,235,.06)}
.olabel{position:absolute;left:0;bottom:100%;font:11px/1.3 var(--mono);background:#2563eb;color:#fff;padding:0 3px;white-space:nowrap;max-width:240px;overflow:hidden;text-overflow:ellipsis}
.aline{position:absolute;background:#db2777}
.aline.v{top:0;bottom:0;width:1px}.aline.h{left:0;right:0;height:1px}
.fold{position:absolute;left:0;right:0;border-top:1px dashed var(--ink-2);display:none}
.fold span{position:absolute;right:4px;top:2px;font-size:11px;background:var(--surface);color:var(--ink-2);padding:0 4px}
.shot.show-fold .fold{display:block}
.marks{position:absolute;inset:0;pointer-events:none;overflow:hidden}
.fbox{position:absolute;border:2px solid var(--ring);outline:1px solid #fff;box-shadow:0 0 0 2px #10110f inset;transition:opacity .15s}
.pin{width:26px;height:26px;font-size:13px;border:0;padding:0;cursor:pointer;transform:translate(-50%,-50%);
box-shadow:0 0 0 2px var(--ring),0 0 0 3px #fff,0 0 0 4px #10110f;transition:transform .12s,opacity .15s}
.pin:hover{transform:translate(-50%,-50%) scale(1.12)}
.pin:focus-visible{outline:2px solid var(--amber);outline-offset:4px}
/* a phone or a touch screen: the pin is a 44 px target, its 26 px circle drawn inside it */
@media (max-width:600px),(pointer:coarse){
 .pin{width:44px;height:44px;background:none;box-shadow:none}
 .pin::before{content:"";position:absolute;inset:9px;z-index:-1;border-radius:50%;background:#fbfaf7;
 box-shadow:0 0 0 2px var(--ring),0 0 0 3px #fff,0 0 0 4px #10110f}
 .pin:focus-visible{outline-offset:-5px}
}
.shot.has-selection .fbox:not(.is-selected),.shot.has-selection .pin:not(.is-selected){opacity:.25}
.shot.has-selection .fbox.is-selected{box-shadow:0 0 0 100vmax rgba(16,17,15,.55)}
.side{display:flex;flex-direction:column;gap:12px;min-width:0}
.flist{display:flex;flex-direction:column}
.flist>li+li,.flist>.finding+.finding{border-top:1px solid var(--line-soft)}
.fpick,.flist>li>a{display:block;width:100%;text-align:left;background:none;border:0;padding:10px 0;cursor:pointer;text-decoration:none}
.fpick[aria-pressed=true]{background:var(--sunken)}
.fhead{display:grid;grid-template-columns:auto auto 1fr;gap:4px 10px;align-items:center}
.ftitle{font-weight:600;color:var(--strong)}
.fmeta{grid-column:3;display:flex;flex-wrap:wrap;gap:0 10px;font-size:12px;color:var(--ink-2)}
.finding details>summary{list-style:none;cursor:pointer;padding:12px 0}
.finding details>summary::-webkit-details-marker{display:none}
.finding details[open]>summary .ftitle{text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:3px}
.fbody{padding:0 0 20px 34px;max-width:80ch}
.fdetail{border-top:1px solid var(--line);padding-top:12px;display:flex;flex-direction:column;gap:8px}
dl{margin:0;display:flex;flex-direction:column;gap:6px}
dl .row{display:block}
dt{display:inline;font-weight:600;color:var(--strong)}
dd{display:inline;margin:0}
.steps{list-style:decimal;padding-left:20px;display:inline-flex;flex-direction:column;vertical-align:top}
.state{margin-top:10px;font-weight:600}
.state.alert{color:var(--red-text)}
.state.ok{color:var(--green)}
.history{margin-top:4px;font-size:13px;color:var(--ink-2);display:flex;flex-direction:column;gap:2px}
.hh{margin-top:12px;font-size:15px}
.compare{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:12px}
.compare figure{margin:0;display:flex;flex-direction:column;gap:4px;min-width:0}
.compare figcaption{font-size:12px;font-weight:600;color:var(--ink-2)}
.compare>p{grid-column:1/-1;font-size:13px}
.crop{position:relative;display:block;overflow:hidden;width:100%;outline:1px solid var(--line);background:var(--sunken)}
.crop img{position:absolute;height:auto;max-width:none}
.diff{position:relative;display:block}
.diff .over{position:absolute;inset:0}
.diff .over img{mix-blend-mode:difference}
.crop.missing{display:grid;place-items:center;padding:4px;text-align:center;overflow:visible}
.recipe{border-top:1px solid var(--line);padding-top:12px;display:flex;flex-direction:column;gap:8px;font-size:13px}
.signals,.models{margin-top:32px;display:flex;flex-direction:column;gap:8px;font-size:13px}
.signals ul,.models ul{display:flex;flex-direction:column;gap:6px}
.coverage{margin-top:32px;display:flex;flex-direction:column;gap:8px}
.legend{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:13px;color:var(--ink-2)}
.legend li{display:flex;align-items:center;gap:6px}
.cov th,.cov td{padding:6px 8px 6px 0;white-space:nowrap}
.cov{width:auto}
.cov caption{white-space:nowrap}
.cov thead th[title],.cov td.cell{text-align:center;padding-left:6px;padding-right:6px}
.cov th[scope=row]{padding-right:24px}
.cov td.cell{width:28px;text-align:center}
.cov th .short+.n,.names{display:none}
[class^=mark-]{display:inline-block;width:12px;height:12px;border-radius:50%;vertical-align:middle}
.mark-findings{background:var(--ink)}
.mark-clean{border:2px solid var(--green);border-radius:2px;transform:rotate(45deg) scale(.8)}
.mark-na{height:2px;width:12px;border-radius:0;background:var(--ink-2)}
.mark-open{border:1.5px solid var(--ink-2)}
.grid td{min-width:120px}
.grid .frame{width:180px}
.grid .frame img{position:static}
.grid .dot{position:absolute}
.cell-link{display:inline-block}
.filters-open{display:none}
.filters-dialog{margin:auto 0 0;width:100%;max-width:100%;max-height:80vh;padding:16px 16px 24px;border:0;
border-top:1px solid var(--line);background:var(--raised);color:var(--ink);box-shadow:0 -8px 24px rgba(16,17,15,.18)}
.filters-dialog:modal{overscroll-behavior:contain}
.filters-dialog::backdrop{background:rgba(16,17,15,.45);overflow:hidden;overscroll-behavior:contain}
.js .filters-dialog .filters{display:flex;position:static}
.filters-dialog .sheet-head{display:flex;justify-content:space-between;align-items:center}
@media (max-width:899px){
 .top{padding:16px 16px 0}main{padding:16px 16px 48px}
 h1{font-size:22px}
 .with-rail{display:block}
 .js .filters-open{display:inline-flex;align-items:center;margin-bottom:12px}
 .js .filters{display:none}
 .cov th .short{display:none}.cov th .short+.n{display:inline}.names{display:block}
 .split{display:block}.side{margin-top:20px}
 .strip{flex-direction:column;gap:20px;overflow:visible}
 .step,.step+.step{padding-left:0;width:100%;display:grid;grid-template-columns:auto 1fr;gap:4px 14px}
 .step>.variants,.step>.frame{grid-row:1/span 4}
 .step[data-status=broken]{display:flex;flex-direction:column}
 .step[data-status=broken]>.step-name{order:-1}
 .step[data-status=broken]>.via{order:-2}
 .gallery,.gallery tbody{display:block}
 .gallery thead{display:none}
 .gallery tr.state{display:grid;grid-auto-flow:column;grid-auto-columns:max-content;overflow-x:auto;padding-bottom:16px}
 .gallery tr.state>th{grid-row:1;grid-column:1/span 12;width:auto;padding:0 0 8px}
 .gallery tr.state>td{grid-row:2;padding:0 12px 0 0}
 .via{position:static;width:auto;flex-direction:row;padding:0;text-align:left;justify-content:flex-start}
 .via .icon{transform:rotate(90deg);width:16px}
 .step .thumb .frame{height:120px}
 .empty-frame{width:120px;height:76px}
 .broken-frame{width:100%}
 .compare{grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}
 .cov th[scope=row]{white-space:normal;min-width:110px}
 .cov td.cell{width:24px;padding-right:4px}
 .thumb .frame{height:150px}
 .thumbs.big .thumb .frame{height:220px}
 .fhead{grid-template-columns:auto 1fr}.fhead .tags{grid-column:2;justify-self:start}.ftitle,.fmeta{grid-column:1/-1}
 .fbody{padding-left:0}
 .fdetail:not([hidden]){position:fixed;inset:0;z-index:20;overflow:auto;background:var(--surface);padding:16px;border:0}
}
@media (max-width:599px){
 .map-table,.map-table tbody,.map-table tr,.map-table th,.map-table td{display:block;width:auto}
 .map-table thead{display:none}
 .map-table tr{padding:10px 0;border-bottom:1px solid var(--line-soft)}
 .map-table th,.map-table td,.map-table th:first-child,.map-table td:first-child{width:auto;padding:2px 0;border:0}
 .map-table .section-row{padding:16px 0 4px;border-bottom-color:var(--line)}
 .map-table td[data-label]{display:grid;grid-template-columns:8.5em minmax(0,1fr);gap:8px}
 .map-table td[data-label]::before{content:attr(data-label);color:var(--ink-2);font-weight:600}
 .map-table td[data-label]>*{grid-column:2}
 .map-table .edge-name{font-weight:600}
 .compare{grid-template-columns:minmax(0,1fr);gap:12px}
}
@media (max-width:600px){
 html{font-size:16px}
 .verdict,h3,h4{font-size:16px}
 .line{font-size:16px}
 .brand,.facts,.fmeta,.thumb-label,.history,.recipe,.legend,.names,.missing-note,.filters,table,.signals,.models,
 .outcome,.expect,.mark,.via,.step-name,.back,.vt-links ul,.compare>p,.compare figcaption{font-size:14px}
 .step-n{font-size:12px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

JS = r"""
(() => {
  const d = document, body = d.body;
  d.documentElement.classList.add("js");
  const views = Array.from(d.querySelectorAll("section[data-view]"));
  const caps = Array.from(d.querySelectorAll("section.capture-view"));
  const nav = d.querySelector("nav.views");
  const links = Array.from(d.querySelectorAll("nav.views a"));
  d.querySelectorAll(".tools, .switch, .filters-open").forEach(el => { el.hidden = false; });
  let current = null;
  // the current tab in view in its scrolling row, clear of the fade at either edge
  function reveal(a) {
    if (!nav || !a || nav.scrollWidth <= nav.clientWidth) return;
    const pad = 24, left = a.offsetLeft - pad, right = a.offsetLeft + a.offsetWidth + pad;
    if (left < nav.scrollLeft) nav.scrollLeft = Math.max(0, left);
    else if (right > nav.scrollLeft + nav.clientWidth) nav.scrollLeft = right - nav.clientWidth;
  }
  // the tab row fades an edge while more tabs lie beyond it: its first and last tabs, watched in the row
  const tabs = nav ? nav.querySelectorAll("li") : [];
  if (tabs.length > 1 && "IntersectionObserver" in window) {
    const first = tabs[0], last = tabs[tabs.length - 1];
    const watch = new IntersectionObserver(entries => {
      for (const {target, intersectionRatio} of entries) {
        nav.classList.toggle(target === first ? "more-start" : "more-end", intersectionRatio < 1);
      }
    }, {root: nav, threshold: 1, rootMargin: "1px"});
    watch.observe(first);
    watch.observe(last);
  }
  function show() {
    const id = decodeURIComponent(location.hash.slice(1));
    const target = id ? d.getElementById(id) : null;
    let cap = target ? target.closest("section.capture-view") : null;
    // a capture inside a view (one capture per width and theme) shows with its view
    let view = target ? target.closest("section[data-view]") : null;
    if (!view && !cap) view = views.find(v => v.dataset.view === body.dataset.open) || views[0];
    if (!cap && view && view.hasAttribute("data-single")) {
      const inside = Array.from(view.querySelectorAll("section.capture-view"));
      cap = inside.find(c => !c.hidden) || inside[0] || null;
    }
    views.forEach(v => { v.hidden = v !== view; });
    caps.forEach(c => { c.hidden = c !== cap; });
    current = cap || view;
    links.forEach(a => {
      if (view && a.getAttribute("href") === "#" + view.dataset.view) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    d.querySelectorAll(".vt-links a").forEach(a => {
      if (cap && a.getAttribute("href") === "#" + cap.id) a.setAttribute("aria-current", "true");
      else a.removeAttribute("aria-current");
    });
    reveal(links.find(a => a.hasAttribute("aria-current")));
    if (target && target.matches("article.finding")) {
      const det = target.querySelector("details");
      if (det) det.open = true;
      target.scrollIntoView({block: "start"});
    } else if (!target || target === view || target === cap) {
      scrollTo(0, 0);
    } else {
      target.scrollIntoView({block: "start"});  // a part of a view, such as its coverage
    }
  }
  addEventListener("hashchange", show);
  show();

  // the capture view: tools and the selected finding
  function select(section, fid) {
    const shot = section.querySelector(".shot");
    section.querySelectorAll(".fbox, .pin").forEach(el => el.classList.toggle("is-selected", el.dataset.f === fid));
    section.querySelectorAll(".fpick").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.f === fid)));
    section.querySelectorAll(".fdetail").forEach(p => { p.hidden = p.dataset.detail !== fid; });
    if (shot) shot.classList.toggle("has-selection", !!fid);
    if (fid) section.dataset.selected = fid; else delete section.dataset.selected;
  }
  d.addEventListener("click", e => {
    const tool = e.target.closest(".tools button");
    if (tool) {
      const shot = tool.closest(".pic").querySelector(".shot");
      const on = tool.getAttribute("aria-pressed") !== "true";
      tool.setAttribute("aria-pressed", String(on));
      shot.classList.toggle({squint: "squint", gray: "gray", outlines: "show-outlines", align: "show-align",
                             fold: "show-fold"}[tool.dataset.tool], on);
      return;
    }
    const close = e.target.closest(".fclose");
    if (close) {
      const section = close.closest("section.capture-view");
      const fid = section.dataset.selected;
      select(section, null);
      const pin = fid && section.querySelector(`.pin[data-f="${fid}"]`);
      if (pin) pin.focus();
      return;
    }
    const pick = e.target.closest(".pin, .fpick");
    if (pick) {
      const section = pick.closest("section.capture-view");
      select(section, section.dataset.selected === pick.dataset.f ? null : pick.dataset.f);
      return;
    }
    if (e.target.closest(".filters-open")) { openSheet(e.target.closest(".filters-open")); return; }
    if (e.target.closest(".filters-close")) {
      const sheet = e.target.closest("dialog");
      if (sheet) sheet.close();
      return;
    }
    const clear = e.target.closest(".filters-clear, .clear-filters");
    if (clear) {
      const view = clear.closest("section[data-view]");
      view.querySelectorAll(".filters input").forEach(i => { i.checked = true; });
      apply(view);
    }
  });

  // filters: an item shows when each group that names it has one of its values checked
  function apply(view) {
    const groups = {};
    view.querySelectorAll(".filters input").forEach(i => {
      (groups[i.name] = groups[i.name] || {all: [], on: []}).all.push(i.value);
      if (i.checked) groups[i.name].on.push(i.value);
    });
    let shown = 0, total = 0;
    view.querySelectorAll("[data-filter-item]").forEach(el => {
      const ok = Object.entries(groups).every(([name, g]) => {
        const v = el.dataset[name];
        if (v === undefined) return true;
        return v.split(" ").some(x => g.on.includes(x));
      });
      el.hidden = !ok;
      if (el.matches("article.finding, .dot")) { total++; if (ok) shown++; }
    });
    // a group of findings (verified, closed) shows while one of its findings does
    view.querySelectorAll(".fgroup").forEach(g => { g.hidden = !g.querySelector("article.finding:not([hidden])"); });
    const narrowed = Object.entries(groups).filter(([, g]) => g.on.length < g.all.length);
    const opener = view.querySelector(".filters-open");
    if (opener) opener.textContent = narrowed.length ? `Filters (${narrowed.length})` : "Filters";
    const empty = view.querySelector(".empty-filter");
    if (empty) {
      // a group with nothing selected says so; otherwise the line names what is selected
      const legend = name => view.querySelector(`.filters input[name="${name}"]`).closest("fieldset")
        .querySelector("legend").textContent.toLowerCase();
      const none = narrowed.filter(([, g]) => !g.on.length).map(([name]) => legend(name));
      const text = none.length ? `No ${none.join(" or ")} is selected. Select one, or`
        : `No findings match ${narrowed.map(([, g]) => g.on.join(" or ")).join(" and ")}.`;
      empty.querySelector(".empty-text").textContent = text;
      empty.querySelector(".clear-filters").textContent = none.length ? "clear filters" : "Clear filters";
      empty.hidden = !(total && !shown);
    }
  }
  d.addEventListener("change", e => {
    if (e.target.closest(".filters")) apply(e.target.closest("section[data-view]"));
    if (e.target.matches(".vt-switch")) {
      const j = e.target.closest(".journey");
      j.querySelectorAll(".variants .thumb").forEach(t => { t.hidden = t.dataset.vt !== e.target.value; });
    }
  });
  // the phone sheet: the view's filters rail moves into its modal dialog while it is open, and back when it
  // closes (Done, Esc, or a tap outside); the browser gives the focus back to the Filters button
  function openSheet(btn) {
    const sheet = d.getElementById(btn.dataset.sheet);
    const rail = sheet && sheet.closest("section[data-view]").querySelector(".filters");
    if (!rail || sheet.open) return;
    sheet.append(rail);
    sheet.showModal();
    const first = rail.querySelector("input");
    if (first) first.focus();
  }
  d.querySelectorAll("dialog.filters-dialog").forEach(sheet => sheet.addEventListener("close", () => {
    const rail = sheet.querySelector(".filters");
    if (rail) sheet.before(rail);
  }));
  matchMedia("(min-width: 900px)").addEventListener("change", e => {
    if (e.matches) d.querySelectorAll("dialog.filters-dialog[open]").forEach(sheet => sheet.close());
  });

  // keyboard: j and k move between findings, Esc goes back
  d.addEventListener("keydown", e => {
    if (e.target.closest("dialog")) return;  // the dialog handles its own keys, Esc included
    if (e.target.closest("input, select, textarea") && e.key !== "Escape") return;
    if (e.key === "Escape") {
      if (current && current.matches("section.capture-view")) {
        const back = current.querySelector(".back");
        if (current.dataset.selected) select(current, null);
        else if (back) location.hash = "#" + back.getAttribute("href").slice(1);
        return;
      }
      const open = current && current.querySelector("details[open]");
      if (open) { open.open = false; open.querySelector("summary").focus(); }
      return;
    }
    if ((e.key !== "j" && e.key !== "k") || e.metaKey || e.ctrlKey || e.altKey || !current) return;
    const items = Array.from(current.querySelectorAll(current.matches("section.capture-view") ? ".pin" :
                                                      "article.finding:not([hidden]) summary"));
    if (!items.length) return;
    const at = items.indexOf(d.activeElement);
    const next = items[(at + (e.key === "j" ? 1 : -1) + items.length) % items.length];
    next.focus();
    if (next.matches(".pin")) select(current, next.dataset.f);
    e.preventDefault();
  });
})();
"""


def _policy(script: str, style: str) -> str:
    def h(s):
        return "'sha256-" + base64.b64encode(hashlib.sha256(s.encode("utf-8")).digest()).decode() + "'"
    return (f"default-src 'none'; script-src {h(script)}; style-src {h(style)}; img-src 'self' data:; "
            "base-uri 'none'; form-action 'none'")


def render_html(data: dict) -> str:
    r = Report(data)
    views = []
    for v, _ in VIEWS[r.scope]:
        views.append({"journeys": _journeys_view, "map": _map_view, "gallery": _gallery_view,
                      "findings": _findings_view, "coverage": _coverage_view, "capture": _capture_view,
                      "states": _states_view}[v](r))
    inline = {c["id"] for c in r.gallery} if r.single else set()
    pages = [_capture_page(r, c) for c in sorted(r.captures.values(), key=lambda c: c["id"]) if c["id"] not in inline]
    header = _header(r)
    main = _el("main", {"id": "main"}, views, pages)
    style = CSS + r.css.text()
    title = f"{r.audit.get('question') or r.audit.get('id')} · visual report"
    head = Markup(
        '<meta charset="utf-8">'
        + _el("meta", {"http-equiv": "Content-Security-Policy", "content": _policy(JS, style)})
        + '<meta name="viewport" content="width=device-width, initial-scale=1">'
          '<meta name="color-scheme" content="light dark">'
          '<meta name="referrer" content="no-referrer">'
        + _el("title", None, title)
        + Markup(f"<style>{style}</style>"))
    first = VIEWS[r.scope][0][0]
    return ("<!doctype html>\n" + _el("html", {"lang": "en"}, _el("head", None, head),
                                      _el("body", {"data-open": first, "data-scope": r.scope},
                                          header, main, Markup(f"<script>{JS}</script>"))) + "\n")


# --------------------------------------------------------------------------- Markdown

_MD_SPECIAL = re.compile(r"([\\`*_\[\]|~!#])")
_URLISH = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*:(?=\S)|www\.|[\w.+-]+@[\w-]+\.[\w.-]+|\]\(")


def _md_code(s, table=True) -> str:
    s = re.sub(r"\s+", " ", str(s)).strip()
    run = max((len(m) for m in re.findall(r"`+", s)), default=0)
    fence = "`" * (run + 1)
    pad = " " if s.startswith("`") or s.endswith("`") else ""
    out = f"{fence}{pad}{s}{pad}{fence}"
    return out.replace("|", "\\|") if table else out


def _md_text(s, table=True) -> str:
    # prose: escaped, with each URL-like word in a code span so nothing links or loads
    out = []
    for word in re.split(r"(\s+)", re.sub(r"[\r\n]+", " ", str(s or ""))):
        if not word.strip():
            out.append(word)
        elif _URLISH.search(word):
            out.append(_md_code(word, table))
        else:
            w = word.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            out.append(_MD_SPECIAL.sub(r"\\\1", w))
    return "".join(out)


def render_md(data: dict) -> str:
    r = Report(data)
    a = r.audit
    lines = [f"# {_md_text(a.get('question') or a.get('id'), table=False)}", "",
             ", ".join(text for text, _ in r.verdict()), ""]
    facts = [f"{r.scope.capitalize()} audit {a.get('id')}"]
    if r.map and r.scope in ("section", "product", "journey"):
        facts.append(f"map v{r.map.get('version')}")
    facts.append(_n(len(r.gallery), "capture", "captures"))
    if a.get("commit"):
        facts.append(f"commit {a['commit']}")
    lines += [", ".join(facts) + ".", ""]
    if r.scope in ("section", "product", "journey") and r.journeys:
        lines += ["## Journeys", ""]
        for j in r.journeys:
            frames = len([s for s in j.get("steps") or [] if isinstance(s, dict)]) + 1
            sigs = _journey_signals(r, j["id"])
            walked = any((c.get("map") or {}).get("journey") == j["id"] for c in r.gallery)
            text = _steps_against_budget(frames, j.get("budget"))
            broken = min((k for k, ss in sigs.items() if any(s.get("check") == "broken-edge" for s in ss)), default=None)
            lost = sorted(k for k, ss in sigs.items() if any(s.get("check") == "lost-context" for s in ss))
            if not walked:
                text = "not walked yet"
            elif broken:
                text += f", breaks at step {broken}"
            if walked and lost:
                text += f", loses context at step {lost[0]}"
            lines.append(f"- {_md_text(j.get('task') or j['id'], table=False)}: {text}")
        lines.append("")
    open_ = [f for f in r.findings if r.is_open(f)]
    lines += ["## Open findings", ""]
    if open_:
        lines += ["| Id | Severity | Title | Where | Capture | Rule | Fix |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for f in open_:
            where = []
            for part in _where(r, f):
                where.append(_md_code(part) if part == (f.get("anchor") or {}).get("name") else _md_text(part))
            lines.append("| " + " | ".join([
                f["id"], str(f.get("severity")), _md_text(f.get("title")), "; ".join(where) or "—",
                _md_text(f.get("capture") or "—"), ", ".join(_md_code(x) for x in f.get("rule") or []) or "—",
                _md_text(f.get("fix") or "—")]) + " |")
    else:
        lines.append("None.")
    lines.append("")
    verified = [f for f in r.findings if f.get("status") == "verified"]
    if verified:
        lines += ["## Verified", "", "| Id | Severity | Title | Verified on |", "| --- | --- | --- | --- |"]
        for f in verified:
            lines.append(f"| {f['id']} | {f.get('severity')} | {_md_text(f.get('title'))} | "
                         f"{_md_text((f.get('verification') or {}).get('capture') or '—')} |")
        lines.append("")
    if (u := r.untriaged_line()):
        lines += [u, ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- writing

def _write_private(path: Path, data: bytes):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _private_dir(path: Path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def write(records, store, audit_id: str) -> Path:
    """Write reports/<audit>/index.html in the store, with the pictures it shows next to it. -> its path."""
    data = load(records, store, audit_id)
    store = Path(store)
    page = render_html(data)
    out = store / "reports" / audit_id
    _private_dir(store / "reports")
    _private_dir(out)
    _private_dir(out / "img")
    wanted = {sha for sha, state in data["pictures"].items() if state == "ok" and f"img/{sha}.png" in page}
    for old in (out / "img").glob("*.png"):
        if old.stem not in wanted:
            old.unlink()
    for sha in wanted:
        target = out / "img" / f"{sha}.png"
        if not target.exists():
            _write_private(target, (store / "blobs" / f"{sha}.png").read_bytes())
    if data.get("diagrams"):
        _private_dir(out / "map")
        for d in data["diagrams"]:
            for theme in ("light", "dark"):
                _write_private(out / "map" / f"{d['name']}-{theme}.svg", Path(d[theme]).read_bytes())
    path = out / "index.html"
    _write_private(path, page.encode("utf-8"))
    return path


def markdown(records, store, audit_id: str) -> str:
    """The Markdown summary of the audit, for a pull request or the handoff note."""
    return render_md(load(records, store, audit_id))
