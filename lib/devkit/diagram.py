# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""devkit diagrams: a small SVG diagram engine and validator, in the Firefly brand.

    spec (JSON) ──render()──▶ SVG (dark + light) ──check()──▶ problems

Kinds: flow (and architecture), sequence, state, c4, er, timeline. Layout is explicit
(nodes on a grid you choose) and routing is automatic (orthogonal, rounded bends,
ports spread per side, labels on pills at the longest segment). The validator works
on the same layout model the renderer drew, so what it checks is what you see:
overlaps, overflow, edges through nodes, label collisions, canvas bounds, WCAG
contrast, minimum type size, title/desc, and the amber rule (one focal element).

Brand source: fireflysoftwaresolutions/brand-kit docs/diagram-system.md and
data/diagram-system.json (colours by meaning, 8 px grid, Manrope, 6/12 px radii,
1.5 px structure, 1.5–2 px connectors, 8 px arrowheads, labels never below 12 px and
never more than two lines). Stdlib only; Python 3.9+.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

# Manrope advance widths in em, measured with rsvg-convert (tools: devkit tools/brand calibration), per weight.
_W = {
    300: (" !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~·→←↔✦–—…’“”áéíóúñçü",
          [280,335,280,555,555,890,670,225,335,335,390,660,280,335,280,280,555,555,555,555,555,555,555,555,555,555,280,280,660,660,660,500,800,670,670,725,725,610,555,780,725,280,500,670,555,835,725,780,610,780,670,610,555,725,610,890,610,610,610,335,280,335,660,500,335,555,610,555,610,555,280,610,555,225,225,500,225,835,555,555,610,610,335,500,280,555,500,725,500,500,500,335,225,335,660,280,1000,1000,900,605,500,1000,1000,225,390,390,555,555,225,555,555,555,555,555]),
    400: (" !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~·→←↔✦–—…’“”áéíóúñçü",
          [280,280,355,560,560,890,670,195,335,335,390,585,280,335,280,280,560,560,560,560,560,560,560,560,560,560,280,280,585,585,585,560,1015,670,670,725,725,670,615,780,725,280,500,670,560,835,725,780,670,780,725,670,615,725,670,945,670,670,615,280,280,280,470,560,335,560,560,500,560,560,280,560,560,225,225,500,225,835,560,560,560,560,335,500,280,560,500,725,500,500,500,335,260,335,585,280,1000,1000,920,605,560,1000,1000,225,335,335,560,560,280,560,560,560,500,560]),
    600: (" !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~·→←↔✦–—…’“”áéíóúñçü",
          [275,330,475,555,555,890,720,235,330,330,390,585,275,330,275,275,555,555,555,555,555,555,555,555,555,555,330,330,585,585,585,610,975,720,720,720,720,665,610,775,720,275,555,720,610,830,720,775,665,775,720,665,610,720,665,945,665,665,610,330,275,330,585,555,330,555,610,555,610,555,330,610,610,275,275,555,275,890,610,610,610,610,390,555,330,610,555,775,555,555,500,390,280,390,585,275,1000,1000,975,600,555,1000,1000,275,500,500,555,555,275,610,610,610,555,610]),
}

FONT = "Manrope, 'Manrope Variable', system-ui, -apple-system, 'Segoe UI', Arial, sans-serif"
GRID = 8
MARGIN = 64
KICKER_Y = 64
TOP = 104
SAFETY = 1.04          # measured Manrope widths + 4 %: system-font fallback never overflows
MIN_FONT = 12
KINDS = ("flow", "sequence", "state", "c4", "er", "timeline")

# --------------------------------------------------------------------------- text

_TABLES = {}


def _table(weight: int) -> dict:
    w = min(_W, key=lambda k: abs(k - weight))
    if w not in _TABLES:
        chars, vals = _W[w]
        _TABLES[w] = {c: v / 1000 for c, v in zip(chars, vals)}
    return _TABLES[w]


def text_width(s: str, size: float, weight: int = 400, tracking: float = 0.0) -> float:
    tab = _table(weight)
    em = sum(tab.get(ch, 0.62) for ch in s) + tracking * len(s)
    return em * size * SAFETY


def wrap(text: str, size: float, weight: int, max_w: float) -> list:
    """Greedy word wrap. Explicit newlines are kept. Never shrinks the type."""
    out = []
    for para in str(text).split("\n"):
        words, line = para.split(" "), ""
        for w in words:
            cand = (line + " " + w).strip()
            if not line or text_width(cand, size, weight) <= max_w:
                line = cand
            else:
                out.append(line)
                line = w
        out.append(line)
    return out


def esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def snap(v: float, up=True) -> float:
    return (math.ceil(v / GRID) if up else round(v / GRID)) * GRID


# --------------------------------------------------------------------------- colour

def _rgb(h: str):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _hex(rgb) -> str:
    return "#%02X%02X%02X" % tuple(max(0, min(255, int(round(c)))) for c in rgb)


def mix(a: str, b: str, t: float) -> str:
    """a blended toward b by t (0 = a, 1 = b)."""
    ra, rb = _rgb(a), _rgb(b)
    return _hex([ra[i] + (rb[i] - ra[i]) * t for i in range(3)])


def luminance(h: str) -> float:
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(h)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# --------------------------------------------------------------------------- themes (brand-kit diagram-system.json)

CHARCOAL, PAPER, AMBER, INK = "#10110F", "#F3F1EB", "#FFB34A", "#272820"
SLATE, OLIVE = "#6B8795", "#7A8460"

THEMES = {
    "light": {
        "bg": PAPER, "text": INK, "muted": "#62645B", "accent": "#855414", "hairline": "#D8D4CA",
        "band": {"fill": "#EAE7DF", "title": "#62645B", "stroke": "#D8D4CA"},
        "families": {
            "neutral": {"fill": "#FFFFFF", "stroke": "#BFB8AB", "text": INK, "sub": "#62645B"},
            "firefly": {"fill": "#FFF0D8", "stroke": "#F0A33C", "text": INK, "sub": "#855414"},
            "steel": {"fill": "#E6E8E6", "stroke": "#8D9391", "text": INK, "sub": "#596060"},
            "slate": {"fill": mix(SLATE, PAPER, 0.80), "stroke": SLATE, "text": INK, "sub": "#39464B"},
            "olive": {"fill": mix(OLIVE, PAPER, 0.80), "stroke": OLIVE, "text": INK, "sub": "#4A5040"},
            "note": {"fill": "#EAE7DF", "stroke": "#D8D4CA", "text": INK, "sub": "#62645B"},
            "focal": {"fill": AMBER, "stroke": "#F0A33C", "text": INK, "sub": INK},
        },
        "edge": {"data": "#62645B", "api": "#8D9391", "event": "#F0A33C", "primary": "#9D671F", "return": "#62645B"},
    },
    "dark": {
        "bg": CHARCOAL, "text": PAPER, "muted": "#BFB8AB", "accent": AMBER, "hairline": "#383A33",
        "band": {"fill": "#1A1B17", "title": "#BFB8AB", "stroke": "#2E302A"},
        "families": {
            "neutral": {"fill": "#1F201C", "stroke": "#4C4E45", "text": PAPER, "sub": "#BFB8AB"},
            "firefly": {"fill": "#2C2416", "stroke": AMBER, "text": PAPER, "sub": AMBER},
            "steel": {"fill": "#222726", "stroke": "#7C8583", "text": PAPER, "sub": "#A7AEAC"},
            "slate": {"fill": mix(SLATE, CHARCOAL, 0.72), "stroke": "#8EA3AB", "text": PAPER, "sub": "#A4B4B9"},
            "olive": {"fill": mix(OLIVE, CHARCOAL, 0.72), "stroke": "#959C7F", "text": PAPER, "sub": "#B9BF9F"},
            "note": {"fill": "#1A1B17", "stroke": "#383A33", "text": PAPER, "sub": "#BFB8AB"},
            "focal": {"fill": AMBER, "stroke": AMBER, "text": INK, "sub": INK},
        },
        "edge": {"data": "#BFB8AB", "api": "#E6DFCC", "event": AMBER, "primary": AMBER, "return": "#BFB8AB"},
    },
}
EDGE_STYLES = {  # stroke key, width, dash, arrow
    "data": ("data", 1.5, None, "filled"), "solid": ("data", 1.5, None, "filled"),
    "api": ("api", 2, None, "filled"), "dashed": ("data", 1.5, "6 5", "filled"),
    "async": ("data", 1.5, "6 5", "filled"), "optional": ("data", 1.5, "6 5", "filled"),
    "event": ("event", 2, "8 6", "filled"), "return": ("return", 1.5, "6 5", "open"),
    "thick": ("primary", 2.5, None, "filled"), "primary": ("primary", 2.5, None, "filled"),
}


# --------------------------------------------------------------------------- geometry & model

class Rect:
    __slots__ = ("x", "y", "w", "h")

    def __init__(self, x, y, w, h):
        self.x, self.y, self.w, self.h = x, y, w, h

    @property
    def x2(self):
        return self.x + self.w

    @property
    def y2(self):
        return self.y + self.h

    @property
    def cx(self):
        return self.x + self.w / 2

    @property
    def cy(self):
        return self.y + self.h / 2

    def grow(self, d):
        return Rect(self.x - d, self.y - d, self.w + 2 * d, self.h + 2 * d)

    def hits(self, o, pad=0.0) -> bool:
        return not (self.x2 + pad <= o.x or o.x2 + pad <= self.x or self.y2 + pad <= o.y or o.y2 + pad <= self.y)

    def contains(self, o) -> bool:
        return self.x <= o.x and self.y <= o.y and self.x2 >= o.x2 and self.y2 >= o.y2

    def moved(self, dx, dy):
        return Rect(self.x + dx, self.y + dy, self.w, self.h)

    def tup(self):
        return (round(self.x, 1), round(self.y, 1), round(self.w, 1), round(self.h, 1))


def seg_hits_rect(a, b, r: Rect, inset=2.0) -> bool:
    """Axis-aligned segment a→b crosses the rectangle interior (shrunk by inset)."""
    x1, y1, x2, y2 = r.x + inset, r.y + inset, r.x2 - inset, r.y2 - inset
    if abs(a[1] - b[1]) < 0.01:  # horizontal
        y = a[1]
        lo, hi = sorted((a[0], b[0]))
        return y1 < y < y2 and lo < x2 and hi > x1
    if abs(a[0] - b[0]) < 0.01:  # vertical
        x = a[0]
        lo, hi = sorted((a[1], b[1]))
        return x1 < x < x2 and lo < y2 and hi > y1
    # diagonal (straight edges between unaligned ports): sample
    for i in range(1, 40):
        t = i / 40
        px, py = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
        if x1 < px < x2 and y1 < py < y2:
            return True
    return False


class Model:
    """What the renderer drew, for the validator: rectangles, paths, texts with their colours."""

    def __init__(self, kind, title, desc):
        self.kind, self.title, self.desc = kind, title, desc
        self.nodes = {}       # id -> Rect
        self.groups = {}      # id -> (Rect, set(member ids), title Rect or None)
        self.edges = []       # (id, from, to, [points], colour, width)
        self.labels = []      # (Rect, owner) edge / message label pills
        self.texts = []       # (text, size, weight, fg, bg, Rect)
        self.marks = []       # (what, colour, bg)  non-text marks that carry meaning
        self.reserved = []    # (Rect, what) kicker, legend, caption
        self.focal = 0
        self.primary_edges = []
        self.wrapped = []     # (owner, lines, max_lines, max_w, widths)
        self.W = self.H = 0

    def shift(self, dx, dy):
        self.nodes = {k: r.moved(dx, dy) for k, r in self.nodes.items()}
        self.groups = {k: (r.moved(dx, dy), m, t.moved(dx, dy) if t else None) for k, (r, m, t) in self.groups.items()}
        self.edges = [(i, f, t, [(x + dx, y + dy) for x, y in pts], c, w) for i, f, t, pts, c, w in self.edges]
        self.labels = [(r.moved(dx, dy), o) for r, o in self.labels]
        self.texts = [(s, z, wt, fg, bg, r.moved(dx, dy)) for s, z, wt, fg, bg, r in self.texts]

    def bbox(self) -> Rect:
        rs = list(self.nodes.values()) + [g[0] for g in self.groups.values()] + [r for r, _ in self.labels]
        rs += [r for *_, r in self.texts]
        xs, ys = [], []
        for r in rs:
            xs += [r.x, r.x2]
            ys += [r.y, r.y2]
        for *_, pts, _, _ in self.edges:
            for x, y in pts:
                xs.append(x)
                ys.append(y)
        if not xs:
            return Rect(0, 0, 0, 0)
        return Rect(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


# --------------------------------------------------------------------------- SVG primitives

class Canvas:
    def __init__(self, theme: str, model: Model):
        self.t = THEMES[theme]
        self.theme = theme
        self.m = model
        self.back, self.mid, self.front = [], [], []   # paint order: groups, edges, nodes/labels

    def text(self, layer, x, y, s, size=16, weight=600, fill=None, anchor="start", bg=None, tracking=0.0,
             upper=False, record=True):
        s = str(s).upper() if upper else str(s)
        fill = fill or self.t["text"]
        ls = f' letter-spacing="{tracking * size:.2f}"' if tracking else ""
        layer.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{weight}" fill="{fill}" '
                     f'text-anchor="{anchor}"{ls}>{esc(s)}</text>')
        if record:
            w = text_width(s, size, weight, tracking)
            x0 = x - w / 2 if anchor == "middle" else x - w if anchor == "end" else x
            self.m.texts.append((s, size, weight, fill, bg or self.t["bg"], Rect(x0, y - size * 0.8, w, size)))


def _path_d(pts, r=12.0) -> str:
    """Orthogonal polyline with rounded bends."""
    if len(pts) < 2:
        return ""
    d = [f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"]
    for i in range(1, len(pts) - 1):
        (x0, y0), (x1, y1), (x2, y2) = pts[i - 1], pts[i], pts[i + 1]
        l1 = math.hypot(x1 - x0, y1 - y0)
        l2 = math.hypot(x2 - x1, y2 - y1)
        rr = min(r, l1 / 2, l2 / 2)
        if rr < 0.5 or l1 == 0 or l2 == 0:
            d.append(f"L{x1:.1f},{y1:.1f}")
            continue
        ax, ay = x1 - (x1 - x0) / l1 * rr, y1 - (y1 - y0) / l1 * rr
        bx, by = x1 + (x2 - x1) / l2 * rr, y1 + (y2 - y1) / l2 * rr
        d.append(f"L{ax:.1f},{ay:.1f} Q{x1:.1f},{y1:.1f} {bx:.1f},{by:.1f}")
    d.append(f"L{pts[-1][0]:.1f},{pts[-1][1]:.1f}")
    return " ".join(d)


def _simplify(pts):
    out = []
    for p in pts:
        p = (round(p[0], 2), round(p[1], 2))
        if out and abs(out[-1][0] - p[0]) < 0.01 and abs(out[-1][1] - p[1]) < 0.01:
            continue
        out.append(p)
    i = 1
    while i < len(out) - 1:
        a, b, c = out[i - 1], out[i], out[i + 1]
        if (abs(a[0] - b[0]) < 0.01 and abs(b[0] - c[0]) < 0.01) or (abs(a[1] - b[1]) < 0.01 and abs(b[1] - c[1]) < 0.01):
            out.pop(i)
        else:
            i += 1
    return out


def _arrow(pts, colour, width, kind="filled"):
    (x0, y0), (x1, y1) = pts[-2], pts[-1]
    L = math.hypot(x1 - x0, y1 - y0) or 1
    dx, dy = (x1 - x0) / L, (y1 - y0) / L
    size = 8 if width <= 2 else 10
    bx, by = x1 - dx * size, y1 - dy * size
    px, py = -dy * size / 2, dx * size / 2
    if kind == "open":
        return (f'<polyline points="{bx + px:.1f},{by + py:.1f} {x1:.1f},{y1:.1f} {bx - px:.1f},{by - py:.1f}" '
                f'fill="none" stroke="{colour}" stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round"/>'), (bx, by)
    return (f'<polygon points="{x1:.1f},{y1:.1f} {bx + px:.1f},{by + py:.1f} {bx - px:.1f},{by - py:.1f}" '
            f'fill="{colour}"/>'), (bx + dx * 0.5, by + dy * 0.5)


# --------------------------------------------------------------------------- flow layout (flow, state, c4)

SIDES = {"right": (1, 0), "left": (-1, 0), "bottom": (0, 1), "top": (0, -1)}
C4_TYPES = {"person": ("actor", "neutral", "Person"), "system": ("box", "neutral", "Software system"),
            "container": ("box", "neutral", "Container"), "component": ("box", "neutral", "Component"),
            "external": ("box", "steel", "External system"), "database": ("store", "slate", "Database"),
            "queue": ("pill", "olive", "Queue")}


def _node_box(n, cw, kind):
    """Size a node from its text. Returns (w, h, label_lines, sub_lines)."""
    shape = n.get("shape", "pill" if kind == "state" else "box")
    if n.get("initial") or n.get("final"):
        return 24, 24, [], []
    span = n.get("span", [1, 1]) if isinstance(n.get("span"), list) else [n.get("span", 1), 1]
    max_w = n.get("width") or min(cw * span[0] - 40, 280 if shape != "decision" else 220)
    label_lines = wrap(n.get("label", n.get("id")), 16, 600, max_w - 32)
    sub = n.get("sub", "")
    sub_lines = wrap(sub, 13, 400, max_w - 32) if sub else []
    tw = max([text_width(s, 16, 600) for s in label_lines] + [text_width(s, 13, 400) for s in sub_lines] + [0])
    w = n.get("width") or max(136, snap(tw + 40))
    h = 16 + 20 * len(label_lines) + ((4 + 18 * len(sub_lines)) if sub_lines else 0) + 16
    if shape == "actor":
        h += 34
    if shape == "store":
        h += 12
    if shape == "decision":
        w, h = snap(w * 1.45), snap(h * 1.55)
    return w, max(56, snap(h)), label_lines, sub_lines


def _sides_for(a: Rect, b: Rect, route=None):
    dx, dy = b.cx - a.cx, b.cy - a.cy
    horiz = abs(dx) >= abs(dy) * 0.9
    if route == "hv":
        return ("right" if dx >= 0 else "left"), ("top" if dy > 0 else "bottom")
    if route == "vh":
        return ("bottom" if dy >= 0 else "top"), ("left" if dx > 0 else "right")
    if horiz:
        return ("right" if dx >= 0 else "left"), ("left" if dx >= 0 else "right")
    return ("bottom" if dy >= 0 else "top"), ("top" if dy >= 0 else "bottom")


def _port(r: Rect, side: str, offset: float):
    if side == "right":
        return (r.x2, r.cy + offset)
    if side == "left":
        return (r.x, r.cy + offset)
    if side == "bottom":
        return (r.cx + offset, r.y2)
    return (r.cx + offset, r.y)


def _route(p0, s0, q0, s1, via=None):
    d0, d1 = SIDES[s0], SIDES[s1]
    p1 = (p0[0] + d0[0] * 16, p0[1] + d0[1] * 16)
    q1 = (q0[0] + d1[0] * 16, q0[1] + d1[1] * 16)
    pts = [p0, p1]
    if via:
        cur, horiz = p1, d0[0] != 0
        for v in list(via) + [q1]:
            if abs(cur[0] - v[0]) > 0.01 and abs(cur[1] - v[1]) > 0.01:
                pts.append((v[0], cur[1]) if horiz else (cur[0], v[1]))
                horiz = not horiz
            pts.append(v)
            cur = v
    else:
        h0, h1 = d0[0] != 0, d1[0] != 0
        if h0 and h1:
            if abs(p1[1] - q1[1]) < 0.5:
                pts.append(q1)
            else:
                facing = (d0[0] > 0 and q1[0] >= p1[0]) or (d0[0] < 0 and q1[0] <= p1[0])
                mx = snap((p1[0] + q1[0]) / 2, False) if facing else (max(p1[0], q1[0]) if d0[0] > 0 else min(p1[0], q1[0]))
                pts += [(mx, p1[1]), (mx, q1[1]), q1]
        elif not h0 and not h1:
            if abs(p1[0] - q1[0]) < 0.5:
                pts.append(q1)
            else:
                facing = (d0[1] > 0 and q1[1] >= p1[1]) or (d0[1] < 0 and q1[1] <= p1[1])
                my = snap((p1[1] + q1[1]) / 2, False) if facing else (max(p1[1], q1[1]) if d0[1] > 0 else min(p1[1], q1[1]))
                pts += [(p1[0], my), (q1[0], my), q1]
        elif h0 and not h1:
            pts += [(q1[0], p1[1]), q1]
        else:
            pts += [(p1[0], q1[1]), q1]
    pts.append(q0)
    return _simplify(pts)


def _draw_node(cv: Canvas, n, r: Rect, lines, subs, kind):
    t = cv.t
    fam_name = "focal" if n.get("emphasis") else n.get("family", "neutral")
    fam = t["families"].get(fam_name, t["families"]["neutral"])
    shape = n.get("shape", "pill" if kind == "state" else "box")
    sw = 2 if n.get("emphasis") else 1.5
    dash = ' stroke-dasharray="6 4"' if n.get("dashed") else ""
    L = cv.front
    if n.get("initial"):
        L.append(f'<circle cx="{r.cx:.1f}" cy="{r.cy:.1f}" r="10" fill="{t["text"]}"/>')
        cv.m.marks.append(("initial state", t["text"], t["bg"]))
        return
    if n.get("final"):
        L.append(f'<circle cx="{r.cx:.1f}" cy="{r.cy:.1f}" r="11" fill="none" stroke="{t["text"]}" stroke-width="2"/>'
                 f'<circle cx="{r.cx:.1f}" cy="{r.cy:.1f}" r="6" fill="{t["text"]}"/>')
        cv.m.marks.append(("final state", t["text"], t["bg"]))
        return
    text_top = r.y
    if shape in ("box", "pill", "note"):
        rx = r.h / 2 if shape == "pill" else 6
        if shape == "note":
            f = 14
            L.append(f'<path d="M{r.x:.1f},{r.y + 6:.1f} Q{r.x:.1f},{r.y:.1f} {r.x + 6:.1f},{r.y:.1f} L{r.x2 - f:.1f},{r.y:.1f} '
                     f'L{r.x2:.1f},{r.y + f:.1f} L{r.x2:.1f},{r.y2 - 6:.1f} Q{r.x2:.1f},{r.y2:.1f} {r.x2 - 6:.1f},{r.y2:.1f} '
                     f'L{r.x + 6:.1f},{r.y2:.1f} Q{r.x:.1f},{r.y2:.1f} {r.x:.1f},{r.y2 - 6:.1f} Z" fill="{fam["fill"]}" '
                     f'stroke="{fam["stroke"]}" stroke-width="1"/>'
                     f'<path d="M{r.x2 - f:.1f},{r.y:.1f} L{r.x2 - f:.1f},{r.y + f:.1f} L{r.x2:.1f},{r.y + f:.1f}" fill="none" '
                     f'stroke="{fam["stroke"]}" stroke-width="1"/>')
        else:
            L.append(f'<rect x="{r.x:.1f}" y="{r.y:.1f}" width="{r.w:.1f}" height="{r.h:.1f}" rx="{rx:.1f}" '
                     f'fill="{fam["fill"]}" stroke="{fam["stroke"]}" stroke-width="{sw}"{dash}/>')
    elif shape == "store":
        e = 8
        L.append(f'<path d="M{r.x:.1f},{r.y + e:.1f} A{r.w / 2:.1f},{e} 0 0 1 {r.x2:.1f},{r.y + e:.1f} L{r.x2:.1f},{r.y2 - e:.1f} '
                 f'A{r.w / 2:.1f},{e} 0 0 1 {r.x:.1f},{r.y2 - e:.1f} Z" fill="{fam["fill"]}" stroke="{fam["stroke"]}" '
                 f'stroke-width="{sw}"/><path d="M{r.x:.1f},{r.y + e:.1f} A{r.w / 2:.1f},{e} 0 0 0 {r.x2:.1f},{r.y + e:.1f}" '
                 f'fill="none" stroke="{fam["stroke"]}" stroke-width="{sw}"/>')
        text_top = r.y + 2 * e
    elif shape == "decision":
        L.append(f'<polygon points="{r.cx:.1f},{r.y:.1f} {r.x2:.1f},{r.cy:.1f} {r.cx:.1f},{r.y2:.1f} {r.x:.1f},{r.cy:.1f}" '
                 f'fill="{fam["fill"]}" stroke="{fam["stroke"]}" stroke-width="{sw}" stroke-linejoin="round"/>')
    elif shape == "actor":
        hx, hy = r.cx, r.y + 12
        if fam_name == "neutral":
            fam = dict(fam, stroke=t["muted"])
        L.append(f'<circle cx="{hx:.1f}" cy="{hy:.1f}" r="9" fill="{fam["fill"]}" stroke="{fam["stroke"]}" stroke-width="{sw}"/>'
                 f'<path d="M{hx - 17:.1f},{r.y + 40:.1f} Q{hx - 17:.1f},{r.y + 24:.1f} {hx:.1f},{r.y + 24:.1f} '
                 f'Q{hx + 17:.1f},{r.y + 24:.1f} {hx + 17:.1f},{r.y + 40:.1f} Z" fill="{fam["fill"]}" stroke="{fam["stroke"]}" '
                 f'stroke-width="{sw}"/>')
        text_top = r.y + 34
    bg = t["bg"] if shape == "actor" else fam["fill"]
    block = 20 * len(lines) + ((4 + 18 * len(subs)) if subs else 0)
    y = text_top + ((r.y2 - text_top) - block) / 2
    for i, s in enumerate(lines):
        cv.text(L, r.cx, y + 15 + 20 * i, s, 16, 600, fam["text"] if shape != "actor" else t["text"], "middle", bg)
    y += 20 * len(lines) + 4
    for i, s in enumerate(subs):
        cv.text(L, r.cx, y + 13 + 18 * i, s, 13, 400, fam["sub"] if shape != "actor" else t["muted"], "middle", bg)
    cv.m.wrapped.append((n["id"], lines + subs, 2, r.w - 24, [text_width(s, 16, 600) for s in lines]
                         + [text_width(s, 13, 400) for s in subs]))
    if n.get("emphasis"):
        cv.m.focal += 1


def _axis(centres: dict, v: float) -> float:
    """Centre coordinate for a (possibly fractional) column/row index."""
    lo, hi = math.floor(v), math.ceil(v)
    keys = sorted(centres)
    if lo in centres and hi in centres:
        return centres[lo] + (centres[hi] - centres[lo]) * (v - lo)
    k = min(keys, key=lambda c: abs(c - v))
    step = (centres[keys[-1]] - centres[keys[0]]) / max(1, keys[-1] - keys[0]) if len(keys) > 1 else 240
    return centres[k] + (v - k) * step


def _auto_grid(spec, kind):
    nodes = spec.get("nodes", [])
    size = {n["id"]: _node_box(n, 360, kind)[:2] for n in nodes}
    if spec.get("equal_widths") == "all":
        dots = {n["id"] for n in nodes if n.get("initial") or n.get("final") or n.get("shape") in ("actor", "decision")}
        wmax = max([w for k, (w, _) in size.items() if k not in dots] + [0])
        size = {k: ((wmax if k not in dots else w), h) for k, (w, h) in size.items()}
    pos = {n["id"]: (n.get("col", 0), n.get("row", 0)) for n in nodes}
    cols = sorted({math.floor(c) for c, _ in pos.values()} | {math.ceil(c) for c, _ in pos.values()})
    rows = sorted({math.floor(r) for _, r in pos.values()} | {math.ceil(r) for _, r in pos.values()})
    colw = {c: max([size[i][0] for i, (cc, _) in pos.items() if abs(cc - c) < 0.51] + [136]) for c in cols}
    rowh = {r: max([size[i][1] for i, (_, rr) in pos.items() if abs(rr - r) < 0.51] + [56]) for r in rows}
    grouped = {m for g in spec.get("groups", []) for m in g.get("nodes", [])}
    gcol = {math.floor(pos[m][0]) for m in grouped if m in pos}
    grow = {math.floor(pos[m][1]) for m in grouped if m in pos}

    def lab(e):
        ls = wrap(e["label"], 13, 400, 200) if e.get("label") else []
        return (max([text_width(x, 13, 400) for x in ls] + [0]) + 16, 18 * len(ls) + 6 if ls else 0)
    gapx = {c: 64 for c in cols}
    gapy = {r: 40 for r in rows}
    for e in spec.get("edges", []):
        a, b = pos.get(e["from"]), pos.get(e["to"])
        if a is None or b is None:  # group endpoints: use the members' extent
            g = next((g for g in spec.get("groups", []) if g["id"] in (e["from"], e["to"])), None)
            if not g:
                continue
            ms = [pos[m] for m in g.get("nodes", []) if m in pos]
            other = a or b
            cmin, cmax = min(c for c, _ in ms), max(c for c, _ in ms)
            rmin, rmax = min(r for _, r in ms), max(r for _, r in ms)
            gp = (other[0] if other and cmin <= other[0] <= cmax else (cmin + cmax) / 2,   # straight edge when the
                  other[1] if other and rmin <= other[1] <= rmax else (rmin + rmax) / 2)   # other end is in range
            a = a or gp
            b = b or gp
        w, h = lab(e)
        if abs(a[0] - b[0]) >= 0.5:
            for c in range(math.floor(min(a[0], b[0])), math.ceil(max(a[0], b[0]))):
                if c in gapx:
                    gapx[c] = max(gapx[c], w + 56)
        if abs(a[1] - b[1]) >= 0.5:
            for r in range(math.floor(min(a[1], b[1])), math.ceil(max(a[1], b[1]))):
                if r in gapy:
                    gapy[r] = max(gapy[r], h + 48)
    member_of = {}
    for g in spec.get("groups", []):
        for m in g.get("nodes", []):
            member_of[m] = g["id"]

    def boundary(axis, k):
        """A group edge lies between line k and k+1 when neighbours across it are not in the same group."""
        here = [i for i, p in pos.items() if abs(p[axis] - k) < 0.51]
        there = [i for i, p in pos.items() if abs(p[axis] - (k + 1)) < 0.51]
        other = 1 - axis
        for a in here:
            for b in there:
                if abs(pos[a][other] - pos[b][other]) < 0.51 and member_of.get(a) != member_of.get(b):
                    return True
        return False
    for c in cols:
        if boundary(0, c):
            gapx[c] += 48
    for r in rows:
        if boundary(1, r):
            gapy[r] += 56
    if any(abs(p[1] - rows[0]) < 0.51 and i in member_of for i, p in pos.items()):
        pass  # the group title row above the first row is added by the group rectangle itself
    xs, x = {}, 0.0
    for c in cols:
        xs[c] = x + colw[c] / 2
        x += colw[c] + gapx[c]
    ys, y = {}, 0.0
    for r in rows:
        ys[r] = y + rowh[r] / 2
        y += rowh[r] + gapy[r]
    return xs, ys, max(colw.values()) + 64, max(rowh.values()) + 56


def layout_flow(spec, cv: Canvas, kind="flow"):
    grid = spec.get("grid", {})
    cw, ch = grid.get("col"), grid.get("row")
    xpos = ypos = None
    if not cw or not ch:  # auto grid: each column/row sized to its own nodes, gaps sized to the labels that cross them
        xpos, ypos, cw0, ch0 = _auto_grid(spec, kind)
        if cw:
            xpos = None   # a fixed column width wins
        if ch:
            ypos = None   # a fixed row height wins
        cw, ch = cw or cw0, ch or ch0
    nodes = []
    for n in spec.get("nodes", []):
        n = dict(n)
        if kind == "c4" and n.get("type") in C4_TYPES:
            shape, fam, tag = C4_TYPES[n["type"]]
            n.setdefault("shape", shape)
            n.setdefault("family", fam)
            tech = n.get("tech")
            n["sub"] = " · ".join(x for x in [f"[{tag}{': ' + tech if tech else ''}]", n.get("sub", "")] if x)
        nodes.append(n)
    rects, text = {}, {}
    col_w = {}
    every = spec.get("equal_widths", True) == "all"

    def wkey(n):
        return "all" if every else (n.get("col", 0), json.dumps(n.get("span", 1)))
    if spec.get("equal_widths", True):
        for n in nodes:
            if n.get("initial") or n.get("final") or n.get("shape") in ("actor", "decision"):
                continue
            col_w[wkey(n)] = max(col_w.get(wkey(n), 0), _node_box(n, cw, kind)[0])
    for n in nodes:
        w, h, lines, subs = _node_box(n, cw, kind)
        key = wkey(n)
        if key in col_w and not n.get("width") and not (n.get("initial") or n.get("final")) \
                and n.get("shape") not in ("actor", "decision"):
            w = col_w[key]
        span = n.get("span", [1, 1]) if isinstance(n.get("span"), list) else [n.get("span", 1), 1]
        cx = _axis(xpos, n.get("col", 0) + (span[0] - 1) / 2) if xpos else (n.get("col", 0) + span[0] / 2) * cw
        cy = _axis(ypos, n.get("row", 0) + (span[1] - 1) / 2) if ypos else (n.get("row", 0) + span[1] / 2) * ch
        rects[n["id"]] = Rect(snap(cx, False) - w / 2, snap(cy, False) - h / 2, w, h)  # centres on the grid: rows align
        text[n["id"]] = (lines, subs)
    # groups
    groups = {}
    for g in spec.get("groups", []):
        mem = [rects[m] for m in g.get("nodes", []) if m in rects]
        if not mem:
            continue
        x0, y0 = min(r.x for r in mem) - 24, min(r.y for r in mem) - 24 - 28
        x1, y1 = max(r.x2 for r in mem) + 24, max(r.y2 for r in mem) + 24
        need = text_width(g.get("title", "").upper(), 12, 600, 0.04) + 40
        if x1 - x0 < need:
            x0, x1 = (x0 + x1) / 2 - need / 2, (x0 + x1) / 2 + need / 2
        gr = Rect(snap(x0, False), snap(y0, False), snap(x1 - x0), snap(y1 - y0))
        tr = Rect(gr.x + 16, gr.y + 12, text_width(g.get("title", "").upper(), 12, 600, 0.04), 16)
        groups[g["id"]] = (gr, set(g.get("nodes", [])), tr, g)
    # edges: sides first, then spread ports per side
    edges = []
    ends = dict(rects, **{gid: g[0] for gid, g in groups.items()})  # edges may start or end on a whole group
    for i, e in enumerate(spec.get("edges", [])):
        if e["from"] not in ends or e["to"] not in ends:
            raise ValueError(f"edge {i}: unknown node or group {e['from'] if e['from'] not in ends else e['to']}")
        a, b = ends[e["from"]], ends[e["to"]]
        s0, s1 = _sides_for(a, b, e.get("route"))
        s0, s1 = e.get("from_side", s0), e.get("to_side", s1)
        edges.append({"e": e, "i": i, "s0": s0, "s1": s1})
    slots = {}
    for ed in edges:
        for end, nid, side, other in (("0", ed["e"]["from"], ed["s0"], ed["e"]["to"]),
                                      ("1", ed["e"]["to"], ed["s1"], ed["e"]["from"])):
            slots.setdefault((nid, side), []).append((ed, end, ends[other]))
    for (nid, side), lst in slots.items():
        r = ends[nid]
        horiz_side = side in ("top", "bottom")
        lst.sort(key=lambda t: t[2].cx if horiz_side else t[2].cy)
        span_len = (r.w if horiz_side else r.h) - 24
        n = len(lst)
        step = min(24, span_len / max(1, n)) if n > 1 else 0
        for k, (ed, end, _) in enumerate(lst):
            off = (k - (n - 1) / 2) * step
            ed["off" + end] = off
    for ed in edges:
        e = ed["e"]
        a, b = ends[e["from"]], ends[e["to"]]
        p0 = _port(a, ed["s0"], ed.get("off0", 0))
        q0 = _port(b, ed["s1"], ed.get("off1", 0))
        horiz = ed["s0"] in ("left", "right") and ed["s1"] in ("left", "right")
        vert = ed["s0"] in ("top", "bottom") and ed["s1"] in ("top", "bottom")
        if not e.get("via") and e.get("route") != "straight" and (horiz or vert):
            k = 1 if horiz else 0
            lo_a, hi_a = (a.y, a.y2) if horiz else (a.x, a.x2)
            lo_b, hi_b = (b.y, b.y2) if horiz else (b.x, b.x2)
            node_a, node_b = e["from"] in rects, e["to"] in rects   # prefer a node's centre over a group's
            cands = ([q0[k]] if node_b else []) + ([p0[k]] if node_a else []) + [p0[k], q0[k]]
            for c in cands:
                if lo_a + 12 <= c <= hi_a - 12 and lo_b + 12 <= c <= hi_b - 12:
                    p0 = (p0[0], c) if horiz else (c, p0[1])
                    q0 = (q0[0], c) if horiz else (c, q0[1])
                    break
        if e.get("route") == "straight":
            if ed["s0"] in ("left", "right"):
                y = (max(a.y, b.y) + min(a.y2, b.y2)) / 2
                p0, q0 = (p0[0], y), (q0[0], y)
            else:
                x = (max(a.x, b.x) + min(a.x2, b.x2)) / 2
                p0, q0 = (x, p0[1]), (x, q0[1])
        via = [((v[0] + 0.5) * cw, (v[1] + 0.5) * ch) for v in e.get("via", [])] or None
        ed["pts"] = _route(p0, ed["s0"], q0, ed["s1"], via)
    # record in model
    m = cv.m
    for gid, (gr, mem, tr, g) in groups.items():
        m.groups[gid] = (gr, mem, tr)
    for nid, r in rects.items():
        m.nodes[nid] = r
    for ed in edges:
        e = ed["e"]
        style = EDGE_STYLES.get(e.get("style", "data"), EDGE_STYLES["data"])
        m.edges.append((e.get("id", f"{e['from']}->{e['to']}"), e["from"], e["to"], ed["pts"],
                        cv.t["edge"][style[0]], style[1]))
        if style[0] == "primary":
            m.primary_edges.append((e["from"], e["to"]))
    return {"nodes": nodes, "rects": rects, "text": text, "groups": groups, "edges": edges, "cw": cw, "ch": ch}


def draw_flow(spec, cv: Canvas, L, kind="flow"):
    t = cv.t
    for gid, (gr, mem, tr, g) in L["groups"].items():
        dash = ' stroke-dasharray="6 4"' if g.get("dashed") else ""
        cv.back.append(f'<rect x="{gr.x:.1f}" y="{gr.y:.1f}" width="{gr.w:.1f}" height="{gr.h:.1f}" rx="12" '
                       f'fill="{t["band"]["fill"]}" stroke="{t["band"]["stroke"]}" stroke-width="1"{dash}/>')
        cv.text(cv.back, tr.x, tr.y + 12, g.get("title", ""), 12, 600, t["band"]["title"], "start", t["band"]["fill"],
                0.04, upper=True)
    fan_out, fan_in = {}, {}
    for ed in L["edges"]:
        fan_out[(ed["e"]["from"], ed["s0"])] = fan_out.get((ed["e"]["from"], ed["s0"]), 0) + 1
        fan_in[(ed["e"]["to"], ed["s1"])] = fan_in.get((ed["e"]["to"], ed["s1"]), 0) + 1
    for ed in L["edges"]:
        e = dict(ed["e"])
        if "label_at" in e:
            e["_label_at"] = e["label_at"]
        elif fan_out[(e["from"], ed["s0"])] > 1 and fan_in[(e["to"], ed["s1"])] == 1:
            e["_label_at"] = "target"
        elif fan_in[(e["to"], ed["s1"])] > 1 and fan_out[(e["from"], ed["s0"])] == 1:
            e["_label_at"] = "source"
        _draw_edge(cv, e, ed["pts"], L["groups"])
    for n in L["nodes"]:
        lines, subs = L["text"][n["id"]]
        _draw_node(cv, n, L["rects"][n["id"]], lines, subs, kind)


def _label_bg(cv, x, y, groups):
    for gid, (gr, mem, tr, g) in (groups or {}).items():
        if gr.x < x < gr.x2 and gr.y < y < gr.y2:
            return cv.t["band"]["fill"]
    return cv.t["bg"]


def _draw_edge(cv: Canvas, e, pts, groups=None, ends=None):
    t = cv.t
    style = EDGE_STYLES.get(e.get("style", "data"), EDGE_STYLES["data"])
    colour, width, dash, arrow = t["edge"][style[0]], style[1], style[2], style[3]
    if e.get("arrow") == "none":
        arrow = None
    draw_pts = list(pts)
    heads = []
    if arrow:
        h, base = _arrow(pts, colour, width, arrow)
        heads.append(h)
        draw_pts[-1] = base if arrow == "filled" else draw_pts[-1]
    if e.get("both") and arrow:
        h, base = _arrow(list(reversed(pts)), colour, width, arrow)
        heads.append(h)
        draw_pts[0] = base if arrow == "filled" else draw_pts[0]
    dash_attr = f' stroke-dasharray="{dash}" stroke-linecap="butt"' if dash else ' stroke-linecap="round"'
    cv.mid.append(f'<path d="{_path_d(draw_pts)}" fill="none" stroke="{colour}" stroke-width="{width}" '
                  f'stroke-linejoin="round"{dash_attr}/>' + "".join(heads))
    cv.m.marks.append((f"edge {e.get('from')}→{e.get('to')}", colour, _label_bg(cv, pts[0][0], pts[0][1], groups)))
    if ends:
        cv.mid.append(ends)
    if e.get("label"):
        segs = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
        lw = max(text_width(x, 13, 400) for x in wrap(e["label"], 13, 400, 200)) + 40
        seglen = lambda sg: math.hypot(sg[1][0] - sg[0][0], sg[1][1] - sg[0][1])  # noqa: E731
        fit = [sg for sg in segs if seglen(sg) >= lw] or [max(segs, key=seglen)]
        prefer = e.get("_label_at")
        if prefer == "target":
            a, b = fit[-1]
        elif prefer == "source":
            a, b = fit[0]
        else:
            a, b = max(fit, key=seglen)
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        lines = wrap(e["label"], 13, 400, 200)
        w = max(text_width(s, 13, 400) for s in lines) + 16
        h = 18 * len(lines) + 6
        bg = _label_bg(cv, mx, my, groups)
        r = Rect(mx - w / 2, my - h / 2, w, h)
        cv.mid.append(f'<rect x="{r.x:.1f}" y="{r.y:.1f}" width="{r.w:.1f}" height="{r.h:.1f}" rx="{min(11, h / 2):.1f}" fill="{bg}"/>')
        for i, s in enumerate(lines):
            cv.text(cv.mid, mx, r.y + 16 + 18 * i, s, 13, 400, t["muted"], "middle", bg)
        cv.m.labels.append((r, e.get("id", f"{e.get('from')}->{e.get('to')}")))


# --------------------------------------------------------------------------- sequence

def layout_sequence(spec, cv: Canvas):
    t, m = cv.t, cv.m
    parts = spec.get("participants", [])
    boxes = []
    for p in parts:
        lines = wrap(p.get("label", p["id"]), 16, 600, 200)
        subs = wrap(p["sub"], 13, 400, 200) if p.get("sub") else []
        w = max(136, snap(max([text_width(s, 16, 600) for s in lines] + [text_width(s, 13, 400) for s in subs]) + 40))
        h = max(56, snap(32 + 20 * len(lines) + ((4 + 18 * len(subs)) if subs else 0)))
        boxes.append((p, w, h, lines, subs))
    idx = {p["id"]: i for i, p in enumerate(parts)}
    gaps = [max(b0[1] / 2 + b1[1] / 2 + 40, 160) for b0, b1 in zip(boxes, boxes[1:])]
    for msg in spec.get("messages", []):
        if "from" in msg and "to" in msg and msg["from"] != msg["to"] and msg.get("label"):
            i, j = sorted((idx[msg["from"]], idx[msg["to"]]))
            need = max(text_width(s, 13, 400) for s in wrap(msg["label"], 13, 400, 320)) + 56
            have = sum(gaps[i:j])
            if have < need:
                for k in range(i, j):
                    gaps[k] += (need - have) / (j - i)
        if msg.get("from") == msg.get("to") and msg.get("label"):
            i = idx[msg["from"]]
            need = 56 + text_width(msg["label"], 13, 400) + 16
            if i < len(gaps) and gaps[i] < need:
                gaps[i] = need
    xs = [0.0]
    for g in gaps:
        xs.append(xs[-1] + snap(g))
    head_h = max(b[2] for b in boxes) if boxes else 56
    y = head_h + 40
    rows = []
    for msg in spec.get("messages", []):
        if "divider" in msg:
            rows.append(("divider", msg, y + 8))
            y += 56
        elif "note" in msg:
            over = msg.get("over") or [parts[0]["id"]]
            lines = wrap(msg["note"], 13, 400, 300)
            h = snap(18 * len(lines) + 20)
            rows.append(("note", msg, y, lines, h, over))
            y += h + 24
        elif msg.get("from") == msg.get("to"):
            rows.append(("self", msg, y + 16))
            y += 72
        else:
            lines = wrap(msg.get("label", ""), 13, 400, 320) if msg.get("label") else []
            y += 18 * max(0, len(lines) - 1)
            rows.append(("msg", msg, y + 16, lines))
            y += 56
    end_y = y + 8
    return {"parts": parts, "boxes": boxes, "xs": xs, "head_h": head_h, "rows": rows, "end_y": end_y, "idx": idx}


def draw_sequence(spec, cv: Canvas, L):
    t, m = cv.t, cv.m
    xs, idx = L["xs"], L["idx"]
    for (p, w, h, lines, subs), x in zip(L["boxes"], xs):
        r = Rect(x - w / 2, 0, w, L["head_h"])   # one header height: tops and lifeline starts line up
        n = dict(p)
        n.setdefault("family", "neutral")
        _draw_node(cv, n, r, lines, subs, "flow")
        m.nodes[p["id"]] = r
        cv.back.append(f'<line x1="{x:.1f}" y1="{r.y2:.1f}" x2="{x:.1f}" y2="{L["end_y"]:.1f}" stroke="{t["hairline"]}" '
                       f'stroke-width="1.5" stroke-dasharray="4 4"/>')
    left = xs[0] - L["boxes"][0][1] / 2 if L["boxes"] else 0
    right = xs[-1] + L["boxes"][-1][1] / 2 if L["boxes"] else 0
    for row in L["rows"]:
        kind, msg = row[0], row[1]
        if kind == "divider":
            y = row[2]
            cv.back.append(f'<line x1="{left:.1f}" y1="{y:.1f}" x2="{right:.1f}" y2="{y:.1f}" stroke="{t["hairline"]}" stroke-width="1"/>')
            label = str(msg["divider"]).upper()
            w = text_width(label, 12, 600, 0.04) + 20
            r = Rect(left, y - 11, w, 22)
            cv.mid.append(f'<rect x="{r.x:.1f}" y="{r.y:.1f}" width="{r.w:.1f}" height="{r.h:.1f}" rx="11" fill="{t["band"]["fill"]}"/>')
            cv.text(cv.mid, r.x + 10, y + 4.5, label, 12, 600, t["band"]["title"], "start", t["band"]["fill"], 0.04)
            m.labels.append((r, f"divider {msg['divider']}"))
        elif kind == "note":
            y, lines, h, over = row[2], row[3], row[4], row[5]
            xo = [xs[idx[o]] for o in over]
            x0, x1 = min(xo), max(xo)
            w = max(x1 - x0 + 120, max(text_width(s, 13, 400) for s in lines) + 32)
            r = Rect((x0 + x1) / 2 - w / 2, y, w, h)
            _draw_node(cv, {"id": f"note{y}", "shape": "note", "family": "note", "label": ""}, r, [], [], "flow")
            for i, s in enumerate(lines):
                cv.text(cv.front, r.cx, r.y + 22 + 18 * i, s, 13, 400, t["families"]["note"]["sub"], "middle",
                        t["families"]["note"]["fill"])
            m.nodes[f"note@{y:.0f}"] = r
        elif kind == "self":
            y = row[2]
            x = xs[idx[msg["from"]]]
            pts = [(x, y), (x + 40, y), (x + 40, y + 28), (x, y + 28)]
            e = dict(msg)
            e.pop("label", None)
            _draw_edge(cv, e, pts)
            m.edges.append((f"self {msg['from']}", msg["from"], msg["from"], pts, t["edge"]["data"], 1.5))
            if msg.get("label"):
                lw = text_width(msg["label"], 13, 400)
                r = Rect(x + 48, y + 14 - 11, lw + 16, 22)
                cv.mid.append(f'<rect x="{r.x:.1f}" y="{r.y:.1f}" width="{r.w:.1f}" height="{r.h:.1f}" rx="11" fill="{t["bg"]}"/>')
                cv.text(cv.mid, r.x + 8, y + 18.5, msg["label"], 13, 400, t["muted"], "start")
                m.labels.append((r, f"self {msg['from']}"))
        else:
            y, lines = row[2], row[3]
            x0, x1 = xs[idx[msg["from"]]], xs[idx[msg["to"]]]
            pts = [(x0, y), (x1, y)]
            e = {"from": msg["from"], "to": msg["to"], "style": msg.get("style", "data")}
            _draw_edge(cv, e, pts)
            style = EDGE_STYLES.get(e["style"], EDGE_STYLES["data"])
            m.edges.append((f"{msg['from']}->{msg['to']}@{y:.0f}", msg["from"], msg["to"], pts, t["edge"][style[0]], style[1]))
            if style[0] == "primary":
                m.primary_edges.append((msg["from"], msg["to"]))
            if lines:
                mx = (x0 + x1) / 2
                w = max(text_width(s, 13, 400) for s in lines) + 16
                h = 18 * len(lines) + 4
                r = Rect(mx - w / 2, y - 8 - h, w, h)
                cv.mid.append(f'<rect x="{r.x:.1f}" y="{r.y:.1f}" width="{r.w:.1f}" height="{r.h:.1f}" rx="9" fill="{t["bg"]}"/>')
                for i, s in enumerate(lines):
                    cv.text(cv.mid, mx, r.y + 15 + 18 * i, s, 13, 400, t["muted"], "middle")
                m.labels.append((r, f"{msg['from']}->{msg['to']}@{y:.0f}"))


# --------------------------------------------------------------------------- ER

CARD = {"1": ("bar", "bar"), "0..1": ("circle", "bar"), "1..*": ("bar", "crow"), "0..*": ("circle", "crow"),
        "*": ("none", "crow"), "n": ("bar", "crow")}


def _er_end(cv, pt, prev, card, colour):
    """Cardinality marker at an entity end: outer symbol further out, inner (crow/bar) at the border."""
    (x0, y0), (x1, y1) = prev, pt
    L = math.hypot(x1 - x0, y1 - y0) or 1
    dx, dy = (x1 - x0) / L, (y1 - y0) / L
    px, py = -dy, dx
    out = []
    outer, inner = CARD.get(str(card), ("bar", "bar"))
    if inner == "crow":
        bx, by = x1 - dx * 14, y1 - dy * 14
        for s in (-1, 0, 1):
            out.append(f'<line x1="{bx:.1f}" y1="{by:.1f}" x2="{x1 + px * 8 * s:.1f}" y2="{y1 + py * 8 * s:.1f}" '
                       f'stroke="{colour}" stroke-width="1.5" stroke-linecap="round"/>')
    else:
        cx, cy = x1 - dx * 10, y1 - dy * 10
        out.append(f'<line x1="{cx + px * 7:.1f}" y1="{cy + py * 7:.1f}" x2="{cx - px * 7:.1f}" y2="{cy - py * 7:.1f}" '
                   f'stroke="{colour}" stroke-width="1.5" stroke-linecap="round"/>')
    ox, oy = x1 - dx * 22, y1 - dy * 22
    if outer == "bar":
        out.append(f'<line x1="{ox + px * 7:.1f}" y1="{oy + py * 7:.1f}" x2="{ox - px * 7:.1f}" y2="{oy - py * 7:.1f}" '
                   f'stroke="{colour}" stroke-width="1.5" stroke-linecap="round"/>')
    elif outer == "circle":
        out.append(f'<circle cx="{ox - dx * 2:.1f}" cy="{oy - dy * 2:.1f}" r="5" fill="{cv.t["bg"]}" stroke="{colour}" stroke-width="1.5"/>')
    return "".join(out)


def layout_er(spec, cv: Canvas):
    grid = spec.get("grid", {})
    cw, ch = grid.get("col", 300), grid.get("row", 220)
    ents, rects = spec.get("entities", []), {}
    for e in ents:
        fields = e.get("fields", [])
        nw = text_width(e.get("name", e["id"]), 16, 600) + 40
        fw = max([text_width(f.get("name", ""), 13, 600) + text_width(f.get("type", ""), 13, 400) + 72 for f in fields] + [0])
        w = max(184, snap(max(nw, fw)))
        h = snap(40 + 26 * len(fields) + 8)
        cx, cy = (e.get("col", 0) + 0.5) * cw, (e.get("row", 0) + 0.5) * ch
        rects[e["id"]] = Rect(snap(cx - w / 2, False), snap(cy - h / 2, False), w, h)
    edges = []
    for i, rel in enumerate(spec.get("relations", [])):
        a, b = rects[rel["from"]], rects[rel["to"]]
        s0, s1 = _sides_for(a, b, rel.get("route"))
        s0, s1 = rel.get("from_side", s0), rel.get("to_side", s1)
        p0, q0 = _port(a, s0, 0), _port(b, s1, 0)
        if s0 in ("left", "right") and s1 in ("left", "right") and a.y < b.y2 and b.y < a.y2:
            y = (max(a.y, b.y) + min(a.y2, b.y2)) / 2
            p0, q0 = (p0[0], y), (q0[0], y)
        if s0 in ("top", "bottom") and s1 in ("top", "bottom") and a.x < b.x2 and b.x < a.x2:
            x = (max(a.x, b.x) + min(a.x2, b.x2)) / 2
            p0, q0 = (x, p0[1]), (x, q0[1])
        pts = _route(p0, s0, q0, s1)
        edges.append((rel, pts))
        cv.m.edges.append((f"{rel['from']}->{rel['to']}", rel["from"], rel["to"], pts, cv.t["edge"]["data"], 1.5))
    for k, r in rects.items():
        cv.m.nodes[k] = r
    return {"ents": ents, "rects": rects, "edges": edges}


def draw_er(spec, cv: Canvas, L):
    t = cv.t
    for rel, pts in L["edges"]:
        e = {"from": rel["from"], "to": rel["to"], "label": rel.get("label"), "arrow": "none",
             "style": rel.get("style", "data")}
        colour = t["edge"]["data"]
        ends = _er_end(cv, pts[-1], pts[-2], rel.get("to_card", "1"), colour) + \
            _er_end(cv, pts[0], pts[1], rel.get("from_card", "1"), colour)
        _draw_edge(cv, e, pts, None, ends)
    for e in L["ents"]:
        r = L["rects"][e["id"]]
        fam = t["families"]["focal" if e.get("emphasis") else e.get("family", "neutral")]
        if e.get("emphasis"):
            cv.m.focal += 1
        cv.front.append(f'<rect x="{r.x:.1f}" y="{r.y:.1f}" width="{r.w:.1f}" height="{r.h:.1f}" rx="6" fill="{t["families"]["neutral"]["fill"]}" '
                        f'stroke="{fam["stroke"]}" stroke-width="1.5"/>'
                        f'<path d="M{r.x:.1f},{r.y + 40:.1f} L{r.x:.1f},{r.y + 6:.1f} Q{r.x:.1f},{r.y:.1f} {r.x + 6:.1f},{r.y:.1f} '
                        f'L{r.x2 - 6:.1f},{r.y:.1f} Q{r.x2:.1f},{r.y:.1f} {r.x2:.1f},{r.y + 6:.1f} L{r.x2:.1f},{r.y + 40:.1f} Z" '
                        f'fill="{fam["fill"]}" stroke="{fam["stroke"]}" stroke-width="1.5"/>')
        cv.text(cv.front, r.cx, r.y + 26, e.get("name", e["id"]), 16, 600, fam["text"], "middle", fam["fill"])
        nf = t["families"]["neutral"]
        for i, f in enumerate(e.get("fields", [])):
            y = r.y + 40 + 26 * i + 22
            tag = "PK" if f.get("pk") else "FK" if f.get("fk") else ""
            if tag:
                cv.text(cv.front, r.x + 14, y, tag, 12, 600, t["accent"] if f.get("pk") else nf["sub"], "start", nf["fill"])
            cv.text(cv.front, r.x + 44, y, f.get("name", ""), 13, 600, nf["text"], "start", nf["fill"])
            cv.text(cv.front, r.x2 - 14, y, f.get("type", ""), 13, 400, nf["sub"], "end", nf["fill"])


# --------------------------------------------------------------------------- timeline

def layout_timeline(spec, cv: Canvas):
    items = spec.get("items", [])
    cards = []
    for it in items:
        lines = wrap(it.get("label", ""), 16, 600, 220)
        subs = wrap(it.get("sub", ""), 13, 400, 220) if it.get("sub") else []
        w = max(152, snap(max([text_width(s, 16, 600) for s in lines] + [text_width(s, 13, 400) for s in subs]
                              + [text_width(it.get("date", ""), 13, 600)]) + 32))
        h = snap(16 + 18 + 20 * len(lines) + ((4 + 18 * len(subs)) if subs else 0) + 12)
        cards.append((it, w, h, lines, subs))
    step = max([c[1] for c in cards] + [160]) / 2 + 24
    xs = [i * step for i in range(len(items))]
    up_h = max([c[2] for i, c in enumerate(cards) if i % 2 == 0] + [0])
    down_h = max([c[2] for i, c in enumerate(cards) if i % 2 == 1] + [0])
    axis_y = up_h + 48
    return {"cards": cards, "xs": xs, "axis_y": axis_y, "down_h": down_h}


def draw_timeline(spec, cv: Canvas, L):
    t, m = cv.t, cv.m
    xs, ay = L["xs"], L["axis_y"]
    if not xs:
        return
    x0, x1 = xs[0] - 40, xs[-1] + 40
    cv.back.append(f'<line x1="{x0:.1f}" y1="{ay:.1f}" x2="{x1:.1f}" y2="{ay:.1f}" stroke="{t["edge"]["data"]}" stroke-width="2" '
                   f'stroke-linecap="round"/>')
    m.edges.append(("axis", "", "", [(x0, ay), (x1, ay)], t["edge"]["data"], 2))
    m.marks.append(("timeline axis", t["edge"]["data"], t["bg"]))
    for i, ((it, w, h, lines, subs), x) in enumerate(zip(L["cards"], xs)):
        up = i % 2 == 0
        y = ay - 32 - h if up else ay + 32
        r = Rect(x - w / 2, y, w, h)
        fam = t["families"]["focal" if it.get("emphasis") else it.get("family", "neutral")]
        if it.get("emphasis"):
            m.focal += 1
        cv.back.append(f'<line x1="{x:.1f}" y1="{ay:.1f}" x2="{x:.1f}" y2="{(r.y2 if up else r.y):.1f}" stroke="{t["hairline"]}" stroke-width="1"/>')
        dot_fill = AMBER if it.get("emphasis") else t["bg"]
        cv.front.append(f'<circle cx="{x:.1f}" cy="{ay:.1f}" r="7" fill="{dot_fill}" stroke="{t["edge"]["data"]}" stroke-width="2"/>')
        cv.front.append(f'<rect x="{r.x:.1f}" y="{r.y:.1f}" width="{r.w:.1f}" height="{r.h:.1f}" rx="6" fill="{fam["fill"]}" '
                        f'stroke="{fam["stroke"]}" stroke-width="1.5"/>')
        cv.text(cv.front, r.x + 16, r.y + 26, it.get("date", ""), 13, 600, fam["sub"], "start", fam["fill"])
        for k, s in enumerate(lines):
            cv.text(cv.front, r.x + 16, r.y + 48 + 20 * k, s, 16, 600, fam["text"], "start", fam["fill"])
        yb = r.y + 48 + 20 * len(lines) + 2
        for k, s in enumerate(subs):
            cv.text(cv.front, r.x + 16, yb + 18 * k, s, 13, 400, fam["sub"], "start", fam["fill"])
        m.nodes[f"item{i}"] = r


# --------------------------------------------------------------------------- frame and assembly

def _legend_items(spec, t):
    out = []
    for it in spec.get("legend", []):
        if "style" in it:
            st = EDGE_STYLES.get(it["style"], EDGE_STYLES["data"])
            col = t["edge"][st[0]]
            dash = f' stroke-dasharray="{st[2]}"' if st[2] else ""
            sw = (f'<line x1="0" y1="0" x2="24" y2="0" stroke="{col}" stroke-width="{st[1]}"{dash}/>', col)
        else:
            fam = t["families"].get(it.get("family", "neutral"), t["families"]["neutral"])
            edge = fam["stroke"] if contrast(fam["stroke"], t["bg"]) >= 3 else fam["sub"]
            sw = (f'<rect x="0" y="-7" width="24" height="14" rx="3" fill="{fam["fill"]}" stroke="{edge}" stroke-width="1.5"/>',
                  edge)
        out.append((it["label"], sw))
    return out


def build(spec: dict, theme: str = "light"):
    """Lay out and render a spec. -> (svg string, Model)."""
    kind = spec.get("kind", "flow")
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r} (one of {', '.join(KINDS)})")
    m = Model(kind, spec.get("title", ""), spec.get("desc", ""))
    cv = Canvas(theme, m)
    if kind in ("flow", "state", "c4"):
        L = layout_flow(spec, cv, kind)
        draw_flow(spec, cv, L, kind)
    elif kind == "sequence":
        L = layout_sequence(spec, cv)
        draw_sequence(spec, cv, L)
    elif kind == "er":
        L = layout_er(spec, cv)
        draw_er(spec, cv, L)
    else:
        L = layout_timeline(spec, cv)
        draw_timeline(spec, cv, L)
    t = cv.t
    bb = m.bbox()
    top = TOP if (spec.get("title") or spec.get("legend")) else MARGIN
    dx, dy = MARGIN - bb.x, top - bb.y
    m.shift(dx, dy)
    legend = _legend_items(spec, t)
    leg_w = sum(text_width(lbl, 13, 400) + 24 + 8 + 24 for lbl, _ in legend)
    kick_w = text_width(spec.get("title", "").upper(), 12, 600, 0.04) + 16
    W = snap(max(bb.w + 2 * MARGIN, kick_w + leg_w + 2 * MARGIN + 32, 480))
    H = top + bb.h
    cap_lines = wrap(spec.get("caption", ""), 18, 300, W - 2 * MARGIN) if spec.get("caption") else []
    if cap_lines:
        H += 32 + 26 * len(cap_lines)
    H = snap(H + MARGIN)
    m.W, m.H = W, H
    out = [f'<rect width="{W}" height="{H}" fill="{t["bg"]}"/>']
    frame = []
    if spec.get("title"):
        frame.append(f'<circle cx="{MARGIN + 4}" cy="{KICKER_Y + 10}" r="4" fill="{AMBER}"/>')
        title_bg = t["bg"]
        frame.append(f'<text x="{MARGIN + 16}" y="{KICKER_Y + 14.5}" font-size="12" font-weight="600" fill="{t["muted"]}" '
                     f'letter-spacing="0.48">{esc(spec["title"].upper())}</text>')
        m.texts.append((spec["title"].upper(), 12, 600, t["muted"], title_bg,
                        Rect(MARGIN + 16, KICKER_Y + 4, kick_w - 16, 12)))
        m.reserved.append((Rect(MARGIN, KICKER_Y, kick_w, 20), "kicker"))
    if legend:
        x = W - MARGIN
        items = []
        for lbl, (sw, col) in reversed(legend):
            tw = text_width(lbl, 13, 400)
            x -= tw
            items.append(f'<text x="{x:.1f}" y="{KICKER_Y + 14.5}" font-size="13" font-weight="400" fill="{t["muted"]}">{esc(lbl)}</text>')
            m.texts.append((lbl, 13, 400, t["muted"], t["bg"], Rect(x, KICKER_Y + 4, tw, 13)))
            x -= 8 + 24
            items.append(f'<g transform="translate({x:.1f},{KICKER_Y + 10})">{sw}</g>')
            m.marks.append((f"legend {lbl}", col, t["bg"]))
            x -= 24
        frame += items
        m.reserved.append((Rect(x, KICKER_Y, W - MARGIN - x, 20), "legend"))
    if cap_lines:
        y0 = H - MARGIN - 26 * (len(cap_lines) - 1)
        for i, s in enumerate(cap_lines):
            frame.append(f'<text x="{MARGIN}" y="{y0 + 26 * i:.1f}" font-size="18" font-weight="300" fill="{t["muted"]}">{esc(s)}</text>')
            m.texts.append((s, 18, 300, t["muted"], t["bg"], Rect(MARGIN, y0 + 26 * i - 14, text_width(s, 18, 300), 18)))
        m.reserved.append((Rect(MARGIN, y0 - 20, W - 2 * MARGIN, 26 * len(cap_lines) + 4), "caption"))
    body = "\n".join(cv.back + cv.mid + cv.front)
    title, desc = esc(spec.get("title") or spec.get("name", "diagram")), esc(spec.get("desc", ""))
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" '
           f'aria-labelledby="dk-title dk-desc">\n<title id="dk-title">{title}</title>\n<desc id="dk-desc">{desc}</desc>\n'
           + "\n".join(out) + f'\n<g font-family="{esc(FONT)}">\n' + "\n".join(frame)
           + f'\n<g transform="translate({dx:.1f},{dy:.1f})">\n{body}\n</g>\n</g>\n</svg>\n')
    return svg, m


def render(spec: dict, theme: str = "light") -> str:
    return build(spec, theme)[0]


# --------------------------------------------------------------------------- validator

def _connected_components(pairs):
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b in pairs:
        parent[find(a)] = find(b)
    return len({find(x) for x in parent})


def validate(m: Model, theme: str = "light") -> list:
    """Problems in a laid-out model. Each problem: '<code>: <message>'."""
    P = []
    if not m.title:
        P.append("a11y: missing title (spec.title)")
    if not m.desc:
        P.append("a11y: missing description (spec.desc)")
    canvas = Rect(0, 0, m.W, m.H)
    live = Rect(MARGIN - 8, KICKER_Y - 8, m.W - 2 * MARGIN + 16, m.H - KICKER_Y - MARGIN + 16)
    nodes = list(m.nodes.items())
    for i, (a, ra) in enumerate(nodes):
        for b, rb in nodes[i + 1:]:
            if ra.hits(rb, 8):
                P.append(f"overlap: nodes {a} and {b} overlap or sit closer than 8 px")
    groups = list(m.groups.items())
    for i, (a, (ga, ma, _)) in enumerate(groups):
        for b, (gb, mb, _) in groups[i + 1:]:
            if ga.hits(gb) and not (ga.contains(gb) or gb.contains(ga)):
                P.append(f"overlap: groups {a} and {b} overlap without containment")
        for nid, rn in m.nodes.items():
            if nid not in ma and ga.hits(rn) and not ga.contains(rn):
                P.append(f"overlap: node {nid} straddles group {a}")
            elif nid not in ma and ga.contains(rn):
                inner = [g for g, (gr, mem, _) in m.groups.items() if g != a and nid in mem and ga.contains(gr)]
                if not inner:
                    P.append(f"overlap: node {nid} sits inside group {a} but is not a member")
    for owner, lines, max_lines, max_w, widths in m.wrapped:
        label_lines = len(lines)
        if label_lines > max_lines * 2 or any(w > max_w + 0.5 for w in widths):
            P.append(f"overflow: text of {owner} does not fit its box")
    for eid, fa, ta, pts, colour, width in m.edges:
        for nid, r in m.nodes.items():
            if nid in (fa, ta):
                continue
            for a, b in zip(pts, pts[1:]):
                if seg_hits_rect(a, b, r):
                    P.append(f"crossing: edge {eid} runs through node {nid}")
                    break
        for lr, owner in m.labels:
            if owner == eid:
                continue
            for a, b in zip(pts, pts[1:]):
                if seg_hits_rect(a, b, lr, 1):
                    P.append(f"crossing: edge {eid} runs through the label of {owner}")
                    break
        for gid, (gr, mem, tr) in m.groups.items():
            if tr is None:
                continue
            for a, b in zip(pts, pts[1:]):
                if seg_hits_rect(a, b, tr, 0):
                    P.append(f"crossing: edge {eid} runs through the title of group {gid}")
                    break
    labels = m.labels
    for i, (ra, oa) in enumerate(labels):
        for rb, ob in labels[i + 1:]:
            if ra.hits(rb, 2):
                P.append(f"label: labels of {oa} and {ob} overlap")
        for nid, rn in m.nodes.items():
            if ra.hits(rn, 2):
                P.append(f"label: label of {oa} overlaps node {nid}")
        for gid, (gr, mem, tr) in m.groups.items():
            if tr and ra.hits(tr, 2):
                P.append(f"label: label of {oa} overlaps the title of group {gid}")
        for rr, what in m.reserved:
            if ra.hits(rr):
                P.append(f"label: label of {oa} overlaps the {what}")
    for s, size, weight, fg, bg, r in m.texts:
        if size < MIN_FONT:
            P.append(f"type: “{s[:30]}” is {size}px (minimum {MIN_FONT})")
        large = size >= 24 or (size >= 18.66 and weight >= 700)
        need = 3.0 if large else 4.5
        c = contrast(fg, bg)
        if c < need:
            P.append(f"contrast: “{s[:30]}” {fg} on {bg} is {c:.2f}:1 (needs {need}:1)")
        if not canvas.contains(r.grow(-0.5)) or not live.contains(r):
            P.append(f"bounds: “{s[:30]}” leaves the live area")
    for what, colour, bg in m.marks:
        c = contrast(colour, bg)
        if c < 3.0:
            P.append(f"contrast: {what} {colour} on {bg} is {c:.2f}:1 (non-text marks need 3:1)")
    for nid, r in m.nodes.items():
        if not live.contains(r):
            P.append(f"bounds: node {nid} leaves the live area")
    focal = m.focal + (_connected_components(m.primary_edges) if m.primary_edges else 0)
    if focal > 1:
        P.append(f"amber: {focal} focal elements (emphasised nodes + primary paths); keep one")
    return sorted(set(P), key=P.index)


def load(spec_or_path):
    if isinstance(spec_or_path, dict):
        return spec_or_path
    return json.loads(Path(spec_or_path).read_text())


def check(spec_or_path, themes=("light", "dark")) -> list:
    """Validate a spec in every theme. -> problems (empty list = clean)."""
    spec = load(spec_or_path)
    out = []
    for th in themes:
        try:
            _, m = build(spec, th)
        except (KeyError, ValueError) as e:
            return [f"spec: {e}"]
        out += [f"[{th}] {p}" if p.startswith("contrast") else p for p in validate(m, th)]
    return sorted(set(out), key=out.index)


_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _shape_bbox(tag, el):
    try:
        if tag == "rect":
            x, y = float(el.get("x", 0)), float(el.get("y", 0))
            return Rect(x, y, float(el.get("width", 0)), float(el.get("height", 0)))
        if tag == "circle":
            cx, cy, r = float(el.get("cx", 0)), float(el.get("cy", 0)), float(el.get("r", 0))
            return Rect(cx - r, cy - r, 2 * r, 2 * r)
        nums = [float(n) for n in _NUM.findall(el.get("d") or el.get("points") or "")]
        if tag == "path":  # drop arc radii/flags is overkill; extents of all numbers are a safe over-estimate
            pass
        xs, ys = nums[0::2], nums[1::2]
        if xs and ys:
            return Rect(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
    except (TypeError, ValueError):
        pass
    return None


def check_svg(path) -> list:
    """Checks for any SVG file: viewBox, title, no scripts/external refs, type size, and text contrast measured
    against the filled shape painted underneath each text (painter's order, translate() transforms)."""
    P = []
    raw = Path(path).read_text(errors="ignore")
    if "<!DOCTYPE" in raw or "<!ENTITY" in raw:
        return ["svg: DOCTYPE/ENTITY declarations are not allowed (entity expansion risk)"]
    try:
        # Safe: DTDs and entities are rejected above, and ElementTree never fetches external resources.
        root = ET.fromstring(raw)  # nosemgrep: python.lang.security.use-defused-xml-parse.use-defused-xml-parse
    except ET.ParseError as e:
        return [f"svg: not well-formed ({e})"]
    ns = "{http://www.w3.org/2000/svg}"
    if not root.get("viewBox"):
        P.append("svg: no viewBox")
    if root.find(f"{ns}title") is None and root.find("title") is None:
        P.append("a11y: no <title>")
    painted = []   # (Rect in canvas coords, fill)

    def walk(el, dx, dy):
        tag = el.tag.replace(ns, "")
        tr = el.get("transform", "")
        m = re.match(r"\s*translate\(\s*(-?[\d.]+)[ ,]+(-?[\d.]+)\s*\)", tr)
        if m:
            dx, dy = dx + float(m.group(1)), dy + float(m.group(2))
        if tag in ("script", "foreignObject"):
            P.append(f"svg: contains <{tag}>")
        href = el.get("href") or el.get("{http://www.w3.org/1999/xlink}href") or ""
        if href and not href.startswith("#") and not href.startswith("data:"):
            P.append(f"svg: external reference {href[:60]}")
        fill = el.get("fill", "")
        if tag in ("rect", "circle", "path", "polygon") and fill.startswith("#"):
            bb = _shape_bbox(tag, el)
            if bb:
                painted.append((bb.moved(dx, dy), fill))
        if tag == "text":
            try:
                size = float(el.get("font-size", "16"))
            except ValueError:
                size = 16
            if size < MIN_FONT:
                P.append(f"type: text “{(el.text or '')[:30]}” is {size}px")
            tf = el.get("fill", "")
            try:
                x, y = float(el.get("x", 0)) + dx, float(el.get("y", 0)) + dy - size * 0.35
            except ValueError:
                x, y = dx, dy
            under = next((fl for bb, fl in reversed(painted) if bb.x <= x <= bb.x2 and bb.y <= y <= bb.y2), None)
            if under and tf.startswith("#"):
                c = contrast(tf, under)
                if c < (3.0 if size >= 24 else 4.5):
                    P.append(f"contrast: “{(el.text or '')[:30]}” {tf} on {under} is {c:.2f}:1")
        for ch in el:
            walk(ch, dx, dy)
    walk(root, 0.0, 0.0)
    return sorted(set(P), key=P.index)


# --------------------------------------------------------------------------- files

def write(spec_path, out_dir=None, png=False) -> list:
    """Render <spec>.json to <name>-dark.svg and <name>-light.svg (+ PNG previews). -> written paths."""
    spec_path = Path(spec_path)
    spec = load(spec_path)
    name = spec.get("name") or spec_path.stem
    out = Path(out_dir) if out_dir else spec_path.parent
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for th in ("dark", "light"):
        p = out / f"{name}-{th}.svg"
        p.write_text(render(spec, th))
        paths.append(p)
        if png and shutil.which("rsvg-convert"):
            q = p.with_suffix(".png")
            subprocess.run(["rsvg-convert", "-z", "2", str(p), "-o", str(q)], check=False)
            paths.append(q)
    return paths


SCAFFOLDS = {
    "flow": {"kind": "flow",
             "nodes": [{"id": "a", "label": "Client", "col": 0, "row": 0},
                       {"id": "b", "label": "API", "sub": "validates and routes", "col": 1, "row": 0, "emphasis": True},
                       {"id": "c", "label": "Database", "shape": "store", "family": "slate", "col": 2, "row": 0}],
             "edges": [{"from": "a", "to": "b", "label": "HTTPS"}, {"from": "b", "to": "c", "label": "SQL"}]},
    "sequence": {"kind": "sequence",
                 "participants": [{"id": "u", "label": "User"}, {"id": "s", "label": "Service"}, {"id": "d", "label": "DB"}],
                 "messages": [{"from": "u", "to": "s", "label": "request"}, {"from": "s", "to": "d", "label": "query"},
                              {"from": "d", "to": "s", "label": "rows", "style": "return"},
                              {"from": "s", "to": "u", "label": "response", "style": "return"}]},
    "state": {"kind": "state",
              "nodes": [{"id": "start", "initial": True, "col": 0, "row": 0}, {"id": "draft", "label": "Draft", "col": 1, "row": 0},
                        {"id": "review", "label": "In review", "col": 2, "row": 0}, {"id": "end", "final": True, "col": 3, "row": 0}],
              "edges": [{"from": "start", "to": "draft"}, {"from": "draft", "to": "review", "label": "submit"},
                        {"from": "review", "to": "end", "label": "approve"}]},
    "c4": {"kind": "c4",
           "nodes": [{"id": "user", "type": "person", "label": "Customer", "col": 0, "row": 0},
                     {"id": "web", "type": "container", "label": "Web app", "tech": "Angular", "col": 1, "row": 0},
                     {"id": "api", "type": "container", "label": "API", "tech": "Spring Boot", "col": 2, "row": 0},
                     {"id": "db", "type": "database", "label": "Orders", "tech": "PostgreSQL", "col": 2, "row": 1},
                     {"id": "pay", "type": "external", "label": "Payments", "col": 3, "row": 0}],
           "groups": [{"id": "sys", "title": "Shop", "nodes": ["web", "api", "db"], "dashed": True}],
           "edges": [{"from": "user", "to": "web", "label": "uses"}, {"from": "web", "to": "api", "label": "JSON/HTTPS"},
                     {"from": "api", "to": "db", "label": "JDBC"}, {"from": "api", "to": "pay", "label": "charges"}]},
    "er": {"kind": "er",
           "entities": [{"id": "customer", "name": "customer", "col": 0, "row": 0,
                         "fields": [{"name": "id", "type": "uuid", "pk": True}, {"name": "email", "type": "text"}]},
                        {"id": "order", "name": "order", "col": 1, "row": 0,
                         "fields": [{"name": "id", "type": "uuid", "pk": True}, {"name": "customer_id", "type": "uuid", "fk": True}]}],
           "relations": [{"from": "customer", "to": "order", "from_card": "1", "to_card": "0..*", "label": "places"}]},
    "timeline": {"kind": "timeline",
                 "items": [{"date": "2026-11", "label": "First release"}, {"date": "2026-12", "label": "Second", "emphasis": True},
                           {"date": "2027-01", "label": "Third"}]},
}


def scaffold(kind: str, name: str) -> dict:
    if kind not in SCAFFOLDS:
        raise ValueError(f"unknown kind {kind!r} (one of {', '.join(KINDS)})")
    spec = json.loads(json.dumps(SCAFFOLDS[kind]))
    spec = {"name": name, "title": name.replace("-", " ").capitalize(),
            "desc": "Describe what the diagram shows, in one or two sentences, for readers who cannot see it.", **spec}
    return spec


# --------------------------------------------------------------------------- command line

def main(argv=None) -> int:
    """dev diagram render <spec|dir> [--out DIR] [--png] · check <spec|svg|dir> · new <kind> <name> [--dir DIR]"""
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog="dev diagram", description="Branded SVG diagrams from JSON specs, validated.")
    sp = ap.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("render", help="render spec(s) to <name>-dark.svg and <name>-light.svg")
    r.add_argument("target", help="a spec .json or a directory of specs")
    r.add_argument("--out", help="output directory (default: next to the spec, or ../images/diagrams for docs/diagrams)")
    r.add_argument("--png", action="store_true", help="also write 2x PNG previews (needs rsvg-convert)")
    c = sp.add_parser("check", help="validate spec(s) or SVG file(s); exit 1 on problems")
    c.add_argument("target")
    n = sp.add_parser("new", help="scaffold a spec")
    n.add_argument("kind", choices=KINDS)
    n.add_argument("name")
    n.add_argument("--dir", default=".")
    a = ap.parse_args(argv)
    if a.cmd == "new":
        p = Path(a.dir) / f"{a.name}.json"
        if p.exists():
            print(f"{p} exists", file=sys.stderr)
            return 2
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(scaffold(a.kind, a.name), indent=2, ensure_ascii=False) + "\n")
        print(p)
        return 0
    target = Path(a.target)
    files = sorted(target.glob("*.json" if a.cmd == "render" else "*")) if target.is_dir() else [target]
    files = [f for f in files if f.suffix in (".json", ".svg")]
    if target.is_dir():  # a folder may hold other JSON (brand font data, configs): only diagram specs count
        def is_spec(f):
            if f.suffix != ".json":
                return True
            try:
                data = json.loads(f.read_text())
            except (OSError, ValueError):
                return False
            return isinstance(data, dict) and "kind" in data and any(k in data for k in ("nodes", "lanes", "steps",
                                                                                        "states", "entities", "items",
                                                                                        "actors", "messages", "events"))
        files = [f for f in files if is_spec(f)]
    rc = 0
    for f in files:
        if a.cmd == "render":
            if f.suffix != ".json":
                continue
            out = a.out or (f.parent.parent / "images" / "diagrams" if f.parent.name == "diagrams" else f.parent)
            probs = check(f)
            for p in write(f, out, png=a.png):
                print(p)
            for pr in probs:
                print(f"  ! {f.name}: {pr}", file=sys.stderr)
            rc |= 1 if probs else 0
        else:
            probs = check(f) if f.suffix == ".json" else check_svg(f)
            print(f"{'ok ' if not probs else 'FAIL'} {f}")
            for pr in probs:
                print(f"     {pr}")
            rc |= 1 if probs else 0
    return rc


if __name__ == "__main__":
    import sys
    sys.exit(main())
