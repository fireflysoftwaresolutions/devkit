# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The map and journey diagrams of visual diagnosis (`dev visual map draw`, R54, R55), drawn with devkit's diagram
engine (lib/devkit/diagram.py, the code behind `dev diagram render` and `dev diagram check`).

What it draws:

  map                  the whole map as a flow, grouped by section, when it has at most 24 screens and passes the check
  map.<section>        one section at a time, when the whole map is too dense to pass the check
  journey.<slug>       each journey: its screens only, its path as the one primary path

Where they go: the specs into the handoff repository (visual/<owner>/<repo>/map/diagrams/<name>.json), the SVGs
into the local store (~/.local/state/devkit/visual/<owner>/<repo>/map/diagrams/<name>-light.svg and -dark.svg,
folders 0700 and files 0600, R10), where the report finds them. Every string in a spec is a map string, which passed
redact.cjs when the map was written (R48).

Layout (R55): a screen's column is its shortest distance in edges from the start (from the section's entry for a
section); screens the start cannot reach start their own columns from 0. Rows keep each section in its own band, so
the section groups never overlap, and order the screens of a band's column by two barycenter passes (the mean row
of their neighbors in the column before, then in the column after), which reduces crossings. An edge back to an
earlier column is dashed. Each edge is then added with the first routing that keeps the diagram passing the
validator; a diagram that cannot pass is never written. A section that cannot pass on its own is left to the
report's table of screens and edges.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .. import core, diagram
from . import map as vmap
from .store import store_dir  # the project's local store, ~/.local/state/devkit/visual/<owner>/<repo>

MAX_NODES = 24  # screens in one diagram (R54)
_LABEL = 28     # characters of an edge label
_TITLE = 40     # characters of a screen's title
_SUB = 32       # characters of a screen's route
_NAME = re.compile(r"^(map|map\.[a-z0-9][a-z0-9._-]{0,55}|journey\.[a-z0-9][a-z0-9-]{0,59})$")
# routings tried for an edge, in order: one to the next column enters its target from the left; one to the same or
# an earlier column may go round either side
_AHEAD = ({"from_side": "right", "to_side": "left"}, {"from_side": "bottom", "to_side": "left"},
          {"from_side": "top", "to_side": "left"})
_BACK = ({}, {"from_side": "bottom", "to_side": "bottom"}, {"from_side": "top", "to_side": "top"},
         {"from_side": "right", "to_side": "right"}, {"from_side": "left", "to_side": "left"})
_LABEL_AT = ({}, {"label_at": "source"}, {"label_at": "target"})


def _cut(text, size: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= size else text[:size - 1].rstrip() + "…"


def _section_of(n: dict) -> str:
    """A screen's section; the root route has none, so its id's first part names it (home)."""
    return n.get("section") or n["id"].split(".")[0]


def _slug(text) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", str(text or "other").lower()).strip("-._")[:55] or "other"


# --------------------------------------------------------------------------- layout (R55)

def _live_edges(m: dict, ids) -> list:
    keep = set(ids)
    return [e for e in m["edges"] if not e.get("missing_since") and e["from"] in keep and e["to"] in keep]


def layout(m: dict, ids: list, start, edges: list = None, bands: bool = True) -> dict:
    """Grid positions {node id: (col, row)} for the nodes `ids` of map `m`, over `edges` (the map's edges between
    them, by default): columns by distance from `start`, rows by section band and barycenter order."""
    nodes = {n["id"]: n for n in m["nodes"]}
    ids = [i for i in ids if i in nodes]
    edges = [e for e in (edges if edges is not None else _live_edges(m, ids))
             if e["from"] != e["to"] and e["from"] in ids and e["to"] in ids]
    ahead = {i: [] for i in ids}
    for e in edges:
        if e["kind"] != "back":
            ahead[e["from"]].append(e["to"])
    col = {}
    for root in [start] + ids if start in ahead else ids:
        if root in col:
            continue
        col[root], todo = 0, [root]
        for nid in todo:
            for nxt in ahead[nid]:
                if nxt not in col:
                    col[nxt] = col[nid] + 1
                    todo.append(nxt)
    reached = set(vmap._distances(m, ids, start)) if start in ahead else set()
    band_of = {i: _section_of(nodes[i]) if bands else "" for i in ids}
    first = {}
    for k, i in enumerate(ids):  # bands in order of their first screen; those the start cannot reach go last
        key = (i not in reached, col[i], k)
        first[band_of[i]] = min(first.get(band_of[i], key), key)
    order = sorted(first, key=first.get)
    cells = {}
    for i in ids:
        cells.setdefault((band_of[i], col[i]), []).append(i)
    offset, top = {}, 0
    for b in order:
        offset[b] = top
        top += max(len(v) for (bb, _), v in cells.items() if bb == b)
    near = {i: set() for i in ids}
    for e in edges:
        near[e["from"]].add(e["to"])
        near[e["to"]].add(e["from"])

    def rows():
        return {i: offset[b] + k for (b, _), cell in cells.items() for k, i in enumerate(cell)}
    last = max(col.values()) if col else 0
    for side, columns in ((-1, range(1, last + 1)), (1, range(last - 1, -1, -1))):
        for c in columns:
            at = rows()
            for (b, cc), cell in cells.items():
                if cc != c:
                    continue

                def centre(i, at=at, c=c, side=side):
                    seen = [at[j] for j in near[i] if col[j] == c + side]
                    beside = [at[j] for j in near[i] if col[j] == c and band_of[j] != band_of[i]]
                    return (sum(seen) / len(seen) if seen else at[i],
                            sum(beside) / len(beside) if beside else at[i])
                cell.sort(key=centre)
    at = rows()
    return {i: (col[i], at[i]) for i in ids}


# --------------------------------------------------------------------------- specs

def _node(n: dict, pos, sub=None, emphasis=False) -> dict:
    where = n.get("route") or (f"page {n['page']}" if n.get("page") else "")
    if n.get("missing_since"):
        where = f"missing since v{n['missing_since']}"
    elif n.get("kind") == "overlay":
        where = f"overlay · {where}" if where else "overlay"
    out = {"id": n["id"], "label": _cut(n.get("title") or n["id"], _TITLE), "sub": _cut(sub or where, _SUB),
           "col": pos[0], "row": pos[1]}
    if not out["sub"]:
        del out["sub"]
    if n.get("kind") == "external":
        out.update(family="steel", shape="pill")
    if n.get("missing_since"):
        out["dashed"] = True
    if emphasis:
        out["emphasis"] = True
    return out


def _label(e: dict) -> str:
    t = e.get("trigger") or {}
    return _cut(t.get("name") or t.get("selector") or e["kind"], _LABEL)


def _passing(spec: dict, edges: list):
    """`spec` with `edges` added one at a time, each with the first routing variant that keeps the light theme free
    of problems; then the whole diagram checked in both themes. -> the spec, or None when it cannot pass."""
    spec = dict(spec, edges=[])
    if spec.get("legend") and diagram.check(spec, themes=("light",)):
        spec.pop("legend")
    if diagram.check(spec, themes=("light",)):
        return None
    for e, back in edges:
        for sides in (_BACK if back else _AHEAD):
            for at in _LABEL_AT:
                trial = dict(spec, edges=spec["edges"] + [{**e, **sides, **at}])
                if not diagram.check(trial, themes=("light",)):
                    spec = trial
                    break
            else:
                continue
            break
        else:
            return None
    return None if diagram.check(spec) else spec


def _map_spec(m: dict, ids: list, start, name: str, title: str, desc: str, groups: bool):
    nodes = {n["id"]: n for n in m["nodes"]}
    grid = layout(m, ids, start)
    edges = []
    for e in _live_edges(m, ids):
        if e["from"] == e["to"]:
            continue
        back = grid[e["to"]][0] < grid[e["from"]][0]
        edges.append(({"from": e["from"], "to": e["to"], "label": _label(e), **({"style": "dashed"} if back else {})},
                      back or grid[e["to"]][0] == grid[e["from"]][0]))
    edges.sort(key=lambda x: x[1])
    spec = {"kind": "flow", "name": name, "title": title, "desc": desc,
            "nodes": [_node(nodes[i], grid[i], emphasis=i == start) for i in ids]}
    if groups:
        sections = {}
        for i in ids:
            sections.setdefault(_section_of(nodes[i]), []).append(i)
        spec["groups"] = [{"id": f"section:{s}", "title": s, "nodes": members} for s, members in sections.items()]
    if any(e.get("style") == "dashed" for e, _ in edges):
        spec["legend"] = [{"label": "back to an earlier screen", "style": "dashed"}]
    return _passing(spec, edges)


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def map_specs(m: dict, section: str = None) -> dict:
    """The map's diagram specs: the whole map when it passes, otherwise one per section; `section` (a route prefix
    or a section name) draws that section only. -> {"specs": [spec, …], "table": [sections left to the table],
    "message": the too-dense message or None}"""
    nodes = [n for n in m["nodes"]]
    ids = [n["id"] for n in nodes]
    sections = {}
    for n in nodes:
        sections.setdefault(_section_of(n), []).append(n["id"])
    edges = len([e for e in _live_edges(m, ids) if e["from"] != e["to"]])
    if section is None and len(ids) <= MAX_NODES:
        whole = _map_spec(m, ids, m["start"], "map", "Product map",
                          f"The product map: {_count(len(ids), 'screen', 'screens')} and "
                          f"{_count(edges, 'edge', 'edges')}, grouped by section, starting at {m['start']}. "
                          "Dashed edges go back to an earlier screen.", groups=len(sections) > 1)
        if whole:
            return {"specs": [whole], "table": [], "message": None}
    if section is not None:
        wanted = str(section).strip()
        picked = [s for s in sections if s == wanted.strip("/")] or \
            [s for s, members in sections.items() if any(vmap._within(n, wanted) for n in nodes if n["id"] in members)]
        if not picked:
            raise vmap.MapError(f'No section "{wanted}" in map v{m["version"]}. Its sections: '
                                f'{", ".join(sections)}.')
        sections = {s: sections[s] for s in picked}
    reach = vmap._distances(m, ids, m["start"]) if m["start"] else {}
    specs, drawn, table = [], [], []
    for s, members in sections.items():
        entry = min(members, key=lambda i: (reach.get(i, len(ids) + 1), members.index(i)))
        spec = None
        if len(members) <= MAX_NODES:
            inside = len([e for e in _live_edges(m, members) if e["from"] != e["to"]])
            spec = _map_spec(m, members, entry, f"map.{_slug(s)}", f"Product map · {s}",
                             f"The {s} section of the product map: {_count(len(members), 'screen', 'screens')} and "
                             f"{_count(inside, 'edge', 'edges')} within it, starting at {entry}. Dashed edges go "
                             "back to an earlier screen.", groups=False)
        if spec:
            specs.append(spec)
            drawn.append(s)
        else:
            table.append(s)
    message = None
    if section is None:
        message = f"⚠ The map has {_count(len(ids), 'screen', 'screens')}: too many for one clear diagram"
        if drawn:
            message += (f". Drew {_count(len(drawn), 'section', 'sections')} instead ({', '.join(drawn)}); each "
                        "passed dev diagram check")
        if table:
            one = len(table) == 1
            which = f"section{'' if one else 's'} {', '.join(table)}"
            if drawn:
                message += (f". The {which} {'is' if one else 'are'} too dense to draw on {'its' if one else 'their'} "
                            f"own: the report shows {'its' if one else 'their'} screens and edges as a table.")
            else:
                message += (f", and {which} {'is' if one else 'are'} too dense to draw on {'its' if one else 'their'} "
                            f"own. The report shows {'its' if one else 'their'} screens and edges as a table.")
        else:
            message += "."
    return {"specs": specs, "table": table, "message": message}


def journey_spec(m: dict, journey_id: str):
    """The journey's diagram: its screens only, each with the steps it is at, and its path as the one primary path.
    -> the spec, or None when it cannot pass the check."""
    j = next((j for j in m["journeys"] if j["id"] == journey_id), None)
    if j is None:
        raise vmap.MapError(f'No journey "{journey_id}" in map v{m["version"]}.'
                            f'{vmap._nearest(str(journey_id), [x["id"] for x in m["journeys"]])}')
    nodes = {n["id"]: n for n in m["nodes"]}
    path = vmap._journey_nodes(j)
    ids = list(dict.fromkeys(path))
    by_id = {e["id"]: e for e in m["edges"]}
    steps = [by_id[s["edge"]] for s in j["steps"] if s["edge"] in by_id]
    grid = layout(m, ids, j["start"], edges=steps, bands=False)
    at = {}
    for k, nid in enumerate(path, start=1):
        at.setdefault(nid, []).append(str(k))
    edges, seen = [], set()
    for e in steps:
        if e["from"] == e["to"] or e["id"] in seen:
            continue
        seen.add(e["id"])
        back = grid[e["to"]][0] <= grid[e["from"]][0]
        edges.append(({"from": e["from"], "to": e["to"], "label": _label(e), "style": "primary"}, back))
    edges.sort(key=lambda x: x[1])
    frames = len(path)
    budget = f"; its budget is {j['budget']}" if j.get("budget") else ""
    spec = {"kind": "flow", "name": f"journey.{j['id'][2:]}", "title": _cut(j.get("task") or j["id"], 60),
            "desc": f"{j['id']}, {j.get('actor') or 'the user'}: {j.get('task') or ''}. It takes {frames} steps over "
                    f"{_count(len(ids), 'screen', 'screens')}{budget}; each screen names the steps it is at.",
            "nodes": [_node(nodes[i], grid[i], sub=f"step{'s' if len(at[i]) > 1 else ''} {', '.join(at[i])}")
                      for i in ids]}
    return _passing(spec, edges)


# --------------------------------------------------------------------------- files

def _private_write(path: Path, text: str) -> None:
    """Write a file of the local store: folders it creates get mode 0700, the file 0600 (R10)."""
    for folder in reversed([path.parent, *path.parent.parents]):
        if not folder.exists():
            folder.mkdir(mode=0o700)
            os.chmod(folder, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions (0700 is owner-only, R10)
    if not path.exists():
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
    core.replace_text(path, text)
    os.chmod(path, 0o600)


def draw(project: str, *, journey: str = None, section: str = None, m: dict = None, store: Path = None) -> dict:
    """`dev visual map draw`: write the map's diagrams (the whole map, or its sections) and every journey's, or only
    the journey or section named. A full draw removes the diagrams the map no longer has. -> {"diagrams": [{"name",
    "spec", "light", "dark"}], "table": [sections left to the report's table], "message": the too-dense message or
    None, "failed": [journeys that could not be drawn]}"""
    m = m or vmap.load(project)
    if not m["nodes"]:
        raise vmap.MapError("The project has no map yet. Crawl one with dev visual map crawl <url>, or declare "
                            "screens with dev visual map node add.")
    specs, table, message, failed = [], [], None, []
    if journey is None:
        drawn = map_specs(m, section)
        specs, table, message = drawn["specs"], drawn["table"], drawn["message"]
    if section is None:
        for j in m["journeys"] if journey is None else [{"id": journey}]:
            spec = journey_spec(m, j["id"])
            (specs.append(spec) if spec else failed.append(j["id"]))
    folder = vmap.map_dir(project) / "diagrams"
    pictures = (store or store_dir(project)) / "map" / "diagrams"
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for spec in specs:
        name = spec["name"]
        spec_file = folder / f"{name}.json"
        core.replace_text(spec_file, json.dumps(spec, indent=2, ensure_ascii=False) + "\n")
        files = {}
        for theme in ("light", "dark"):
            files[theme] = pictures / f"{name}-{theme}.svg"
            _private_write(files[theme], diagram.render(spec, theme))
        out.append({"name": name, "spec": spec_file, "light": files["light"], "dark": files["dark"]})
    if journey is None and section is None:
        keep = {d["name"] for d in out}
        for f in list(folder.glob("*.json")) + (list(pictures.glob("*.svg")) if pictures.is_dir() else []):
            name = re.sub(r"-(light|dark)$", "", f.stem)
            if _NAME.match(name) and name not in keep:
                f.unlink()
    return {"diagrams": out, "table": table, "message": message, "failed": failed}
