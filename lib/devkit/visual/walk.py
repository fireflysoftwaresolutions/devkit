# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The journey walk of visual diagnosis (`dev visual journey walk`, R51): perform a declared journey's steps in one
browser session, record each step reached, and take its captures.

`walk` runs the journey in crawl.cjs's walk mode, which uses capture.cjs's own step runner, from the start node's
URL on the app's base (the address the last crawl recorded, or the one given). The walk record it returns is what
`dev visual map check` reads (map.py, "structure checks"):

  {"journey": "J:export-month", "map_version": 4, "broken": null | {"step", "edge", "do", "error"},
   "steps": [{"step": 1, "node": "home", "edge": null, "status": 200, "redirects": 0, "keeps": {"month": false},
              "captures": ["VC-…", …]}, …]}

Step 1 is the start screen and step k the screen the k-1th edge reaches. `keeps` says whether each value the journey
keeps was on the page at that step; the value is the one the journey's own steps set (`fill #month 2026-10` sets
month), tested in the live page in memory and kept only as present or absent (R16). A step whose trigger is not
found ends the walk: `broken` names it, and the steps before it are the ones recorded.

When a `capture` callable is given (the store's capture with its project and audit bound), every reached step is
captured in each viewport and theme of the audit's matrix, with a recipe that replays the journey's steps up to that
step from the start URL, so each capture reproduces its step's state on its own. `save` keeps the walk record under
the audit in the handoff repository (visual/<owner>/<repo>/walks/VA-…/<journey>.json); `load` reads an audit's walks.
`cmd` is `dev visual journey walk`: it opens a journey audit when none is open, walks, captures each step through
the store, and keeps the walk. With `--path` instead of a declared journey, `declare_path` first walks the path once
without captures and declares, as one map version, its nodes (with the routes and headings the walk reached), its
edges (each triggered by its step), and the journey; a walk that breaks declares nothing. A journey whose steps are
pages of a document (the storyline a deck's map declares) has no browser steps: `walk` and `cmd` refuse it with the
command that captures its pages, `dev visual product <file>.pdf`.
"""
from __future__ import annotations

import functools
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .. import core
from . import map as vmap

WALK_TIMEOUT = 300  # seconds for the browser part of one walk: every step has its own 10 s bound in capture.cjs
AUDIT_ID = re.compile(r"VA-[0-9a-z]{1,40}")
DEFAULT_VIEWPORT = [1280, 800]  # capture.cjs's default


def _journey(m: dict, journey_id: str) -> dict:
    jid = journey_id if str(journey_id).startswith("J:") else f"J:{journey_id}"
    for j in m["journeys"]:
        if j["id"] == jid:
            return j
    near = vmap._nearest(jid, [j["id"] for j in m["journeys"]])
    raise vmap.MapError(f'No journey "{jid}" in map v{m["version"]}.{near} List them with dev visual map show.')


def _document_pages(m: dict, j: dict) -> None:
    """Refuse a journey whose steps are pages of a document (a deck's storyline, docmap.py): it is read in order, so
    it has no browser steps to walk, and its pages are captured with `dev visual product <file>.pdf`."""
    ids = [j["start"]] + [s["edge"].partition(">")[2] for s in j["steps"]]
    pages = [n for n in m["nodes"] if n["id"] in ids and n.get("page") is not None]
    if not pages:
        return
    files = sorted({str(n["document"]) for n in pages if n.get("document")})
    file = files[0] if len(files) == 1 else None
    raise vmap.MapError(f"{j['id']} reads the pages of {file or 'a document'} in order, and a document has no browser "
                        f"steps to walk. Capture its pages instead: dev visual product {file or '<file>.pdf'}")


def _base(m: dict, base_url) -> str:
    base = str(base_url or m.get("base") or "").strip()
    if not base:
        raise vmap.MapError("The map has no address of the app from a crawl. Give the address the app runs on: "
                            "--base http://127.0.0.1:4200")
    return base


def _origin(base: str) -> str:
    parts = urlsplit(base)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else base.rstrip("/")


def _start_url(m: dict, j: dict, base_url) -> str:
    base = _origin(_base(m, base_url))
    node = next(n for n in m["nodes"] if n["id"] == j["start"])
    where = node.get("example") or node.get("route")
    if not where or ":" in where.split("?")[0]:
        raise vmap.MapError(f"The start node {node['id']} has the route {node.get('route')} and no example URL, so "
                            "the walk cannot open it. Start the journey on a screen with a fixed route.")
    return base + (where if where.startswith("/") else f"/{where}")


def _split_target(text: str):
    """A recipe step's target and the rest, as capture.cjs splits them: the target ends at the first space outside
    quotes and brackets."""
    depth, quote, i = 0, None, 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        elif ch.isspace() and depth <= 0:
            return text[:i], text[i:].strip()
        i += 1
    return text, ""


def kept_values(j: dict) -> dict:
    """The value of each name the journey keeps: the value of its first `fill` or `select` step whose target names
    it (`fill #month 2026-10` keeps month as 2026-10), or of a `goto` URL's query parameter of that name; None when
    no step sets it."""
    out = {}
    for name in j.get("keeps") or ():
        word = re.compile(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", re.I)
        value = None
        for action in (a for step in j["steps"] for a in step["do"]):
            verb, _, rest = action.strip().partition(" ")
            if verb in ("fill", "select"):
                target, given = _split_target(rest.strip())
                if word.search(target) and given:
                    hit = re.fullmatch(r"(['\"])(.*)\1", given, re.S)
                    value = hit.group(2) if hit else given
            elif verb == "goto":
                query = parse_qs(urlsplit(rest.strip()).query)
                value = query[name][-1] if query.get(name) else None
            if value is not None:
                break
        out[name] = value
    return out


def _viewport(value) -> list:
    """[width, height] from [w, h] or "WxH"."""
    if isinstance(value, str):
        hit = re.fullmatch(r"\s*([0-9]+)\s*x\s*([0-9]+)\s*", value.lower())
        if not hit:
            raise vmap.MapError(f"{value} is not a viewport: give it as <width>x<height> in CSS px, such as 1440x900.")
        return [int(hit.group(1)), int(hit.group(2))]
    return [int(value[0]), int(value[1])]


def _browser_walk(j, start, screens, viewports, themes, storage_state, remote, core) -> dict:
    """crawl.cjs's walk of journey `j` from `start`, in the first viewport and theme: {steps, broken}, each step with
    its route and title besides what the walk record keeps."""
    request = {"mode": "walk", "start": start, "steps": [list(s["do"]) for s in j["steps"]], "keeps": kept_values(j),
               "screens": screens, "viewport": _viewport((viewports or [DEFAULT_VIEWPORT])[0]),
               "theme": (themes or ["light"])[0], "storage_state": str(storage_state) if storage_state else None,
               "remote": bool(remote), "core": core}
    return vmap.run_crawler(request, WALK_TIMEOUT)


def walk(m: dict, journey_id: str, *, base_url=None, viewports=None, themes=None, capture=None, storage_state=None,
         remote=False, core=None) -> dict:
    """`dev visual journey walk J:<slug>`: walk the journey of map `m` on the app at `base_url` (default: the map's
    base, from the last crawl) and return its walk record (above). The walk itself runs in the first viewport and
    theme; `capture(source, steps=, state=, node=, journey=, step=, viewports=, themes=)` -> [capture records] is
    called once for each reached step, and the ids of the records it returns go into the step. A capture that raises
    ends the walk with that error. `core`: the playwright-core folder (default: the pinned @playwright/cli's)."""
    j = _journey(m, journey_id)
    _document_pages(m, j)
    start = _start_url(m, j, base_url)
    do = [list(s["do"]) for s in j["steps"]]
    walked = _browser_walk(j, start, vmap.screens(m), viewports, themes, storage_state, remote, core)
    nodes = [j["start"]] + [s["edge"].partition(">")[2] for s in j["steps"]]
    steps = []
    for s in walked.get("steps") or ():
        k = s["step"]
        steps.append({"step": k, "node": nodes[k - 1], "edge": j["steps"][k - 2]["edge"] if k > 1 else None,
                      "status": s.get("status"), "redirects": s.get("redirects") or 0, "keeps": s.get("keeps") or {},
                      "captures": []})
    broken = walked.get("broken")
    if broken:
        broken = {"step": broken["step"], "edge": j["steps"][broken["step"] - 2]["edge"], "do": broken.get("do"),
                  "error": broken.get("error")}
    record = {"journey": j["id"], "map_version": m["version"], "broken": broken, "steps": steps}
    if capture is not None:
        for s in steps:
            k = s["step"]
            replay = [action for actions in do[:k - 1] for action in actions]
            state = j["steps"][k - 2].get("expect") if k > 1 else None
            records = capture(start, steps=replay, state=state, node=s["node"], journey=j["id"], step=k,
                              viewports=viewports, themes=themes)
            s["captures"] = [r["id"] for r in records or ()]
    return record


def message(record: dict, m: dict, signal: str = None) -> str:
    """The terminal line of a walk: what was walked and captured, or where the journey broke and the signal that
    records it (`signal`: its reference, VA-…#<n>, once `dev visual map check` numbered it)."""
    steps = record.get("steps") or []
    captures = sum(len(s.get("captures") or ()) for s in steps)
    did = "captured" if captures else "walked"
    b = record.get("broken")
    if not b:
        return f"✓ {record['journey']} · {len(steps)} steps {did}" + (f" · {captures} captures" if captures else "")
    on = steps[-1]["node"] if steps else next((j["start"] for j in m["journeys"] if j["id"] == record["journey"]), "")
    reached = (f"Steps 1–{len(steps)} were {did}" if len(steps) > 1 else f"Step 1 was {did}") if steps else \
        "No step was reached"
    return (f"✗ {record['journey']} step {b['step']} ({b['do']}) {b['error']} on {on}. {reached}; the journey is "
            f"broken at step {b['step']}" + (f" (signal {signal})" if signal else "") + ".")


def _path_journey(names: list) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", f"{names[0]} to {names[-1]}".lower()).strip("-")
    return f"J:{slug[:59].rstrip('-')}"


def _trigger(action: str):
    """An edge's trigger from the step that performs it: the target of `click role=link[name="Invoices"]` is the
    link named Invoices; any other target is its selector."""
    rest = action.strip().partition(" ")[2]
    target, _ = _split_target(rest.strip())
    return vmap.parse_trigger(target or action)


def declare_path(project: str, path, do, *, base_url=None, journey_id=None, viewport=None, theme=None,
                 storage_state=None, remote=False, by=None, core=None) -> tuple:
    """`dev visual journey walk --path "a > b > c" --do <step> …` (R57, R59): walk the path once, without captures,
    and declare what it reached as one map version: each node the map lacks, with the route and heading of the
    screen the walk reached for it; each edge the map lacks, triggered by its step (`nav` when the step loaded a
    document, `action` when it stayed on the page); and the journey (`journey_id`, or J:<first>-to-<last>). One
    --do for each step. A walk that breaks declares nothing. -> (the map, the journey id)"""
    names = [x.strip() for x in (path.split(">") if isinstance(path, str) else path or ())]
    if len(names) < 2 or not all(names):
        raise vmap.MapError('A path names two nodes or more: --path "home > invoices.list".')
    do = list(do or ())
    if len(do) != len(names) - 1:
        raise vmap.MapError(f"--do gives {len(do)} steps and the path has {len(names) - 1}: one --do for each step.")
    m = vmap.load(project)
    base = _base(m, base_url)
    jid = (journey_id if str(journey_id).startswith("J:") else f"J:{journey_id}") if journey_id else \
        _path_journey(names)
    if any(j["id"] == jid for j in m["journeys"]):
        raise vmap.MapError(f"{jid} is declared already. Walk it without --path: dev visual journey walk {jid}")
    pairs = list(zip(names, names[1:]))
    j = {"id": jid, "start": names[0], "steps": [{"edge": f"{a}>{b}", "do": [d]} for (a, b), d in zip(pairs, do)],
         "keeps": []}
    known = {n["id"]: n for n in m["nodes"]}
    start = _start_url(m, j, base) if names[0] in known else base
    walked = _browser_walk(j, start, vmap.screens(m), [viewport] if viewport else None, [theme] if theme else None,
                           storage_state, remote, core)
    steps = walked.get("steps") or []
    b = walked.get("broken")
    if b:
        raise vmap.MapError(f"{jid} step {b['step']} ({b['do']}) {b['error']} on {names[len(steps) - 1]}. Nothing was "
                            "declared: fix the step, then walk the path again.")
    nodes, edges = [], []
    for name, s in zip(names, steps):
        if name not in known and all(n["id"] != name for n in nodes):
            nodes.append({"id": name, "title": s.get("title") or s["route"], "route": s["route"]})
    have = {e["id"] for e in m["edges"]}
    for k, (a, b2) in enumerate(pairs):
        if f"{a}>{b2}" not in have and all((e["from"], e["to"]) != (a, b2) for e in edges):
            edges.append({"from": a, "to": b2, "trigger": _trigger(do[k]),
                          "kind": "nav" if steps[k + 1].get("status") is not None else "action"})
    doc = {"nodes": nodes, "edges": edges,
           "journeys": [{"id": jid, "actor": "user", "task": f"walk {' > '.join(names)}", "path": names,
                         "do": [[d] for d in do]}]}
    out = vmap.apply(project, doc, why=f"journey walk --path {' > '.join(names)}", by=by)
    return out["map"], jid


def cmd(a) -> int:
    """`dev visual journey walk` (R51, R57): walk a declared journey (J:<slug>), or a path declared on the way
    (--path with one --do for each step), in the session's audit or a new journey audit; capture each step reached
    in the audit's matrix (or --viewport and --theme), and keep the walk under the audit for `dev visual map check`.
    The app's address is --base, or the map's from its last crawl. `a`: the command's parsed arguments, with
    `project_id`. -> the exit code: 1 when the journey broke at a step."""
    from .. import ui
    from . import commands, store  # the command line's helpers; this module is imported by it, never the reverse
    pid = a.project_id
    path = getattr(a, "path", None)
    if not path and not getattr(a, "journey", None):
        raise vmap.MapError("Name the journey to walk: dev visual journey walk J:<slug>, or walk a path: --path "
                            '"<node> > <node>" --do "<step>" … List the journeys with dev visual map show.')
    if a.do and not path:
        raise vmap.MapError("A declared journey has its own steps; --do goes with --path.")
    m = vmap.load(pid)
    names = [x.strip() for x in path.split(">")] if path else None
    jid = _journey(m, a.journey)["id"] if not path else (
        (a.journey if str(a.journey).startswith("J:") else f"J:{a.journey}") if a.journey else _path_journey(names))
    task = _journey(m, jid)["task"] if not path else f"walk {' > '.join(names)}"
    if not path:
        _document_pages(m, _journey(m, jid))  # refused before an audit opens, whatever --base says
    base = getattr(a, "base", None)
    _base(m, base)  # refused before an audit opens: the walk needs the app's address
    if not path:
        _start_url(m, _journey(m, jid), base)
    aid, _ = commands._ensure_audit(pid, a, "journey", task, journeys=[jid])
    viewports = a.viewport or store.load(pid, aid)["matrix"]["viewports"]
    themes = a.theme or store.load(pid, aid)["matrix"]["themes"]
    if path:
        m, jid = declare_path(pid, path, a.do, base_url=base, journey_id=jid, viewport=_viewport(viewports[0]),
                              theme=themes[0], storage_state=a.storage_state, remote=a.remote, by=store.who())
        print(f"{ui.OK} map v{m['version']} · {jid} declared along {' > '.join(names)}")
    options = commands._capture_options(a)
    for key in ("state", "node", "steps", "viewports", "themes", "audit"):
        options.pop(key)
    record = walk(m, jid, base_url=base, viewports=[_viewport(v) for v in viewports], themes=themes,
                  capture=functools.partial(_store_capture, store.capture, pid, aid, options, a.viewport, a.theme),
                  storage_state=a.storage_state, remote=a.remote)
    save(pid, aid, record)
    caps = [store.load(pid, cid) for s in record["steps"] for cid in s["captures"]]
    if record["broken"]:
        commands._print_captures(caps, aid, "fix the journey's step, then walk it again; dev visual map check {audit} "
                                 "records the broken step as a signal")
        ui.err(message(record, m).removeprefix("✗ "))
        return 1
    print(message(record, m))
    commands._print_captures(caps, aid, "dev visual map check {audit}, then screen passes 1–10 on each step")
    return 0


def _store_capture(capture, project, audit_id, options, sizes, looks, source, **step):
    """One step's captures through the store: in the audit's matrix, or the --viewport and --theme given (`sizes`,
    `looks`), which the store reads in its own form."""
    step.pop("viewports", None)
    step.pop("themes", None)
    return capture(project, source, audit=audit_id, viewports=sizes, themes=looks, **options, **step)


def _folder(project: str, audit_id: str) -> Path:
    if not AUDIT_ID.fullmatch(str(audit_id)):
        raise vmap.MapError(f'"{audit_id}" is not an audit id such as VA-g33z0k2m4q.')
    return vmap.root(project) / "walks" / audit_id


def save(project: str, audit_id: str, record: dict) -> Path:
    """Keep the walk record of one journey under its audit, replacing an earlier walk of that journey. -> its
    path."""
    path = _folder(project, audit_id) / f"{record['journey'].partition(':')[2]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    core.replace_text(path, json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    return path


def load(project: str, audit_id: str) -> list:
    """The audit's walk records, one per journey, by journey id; [] when it has none. They sync from teammates like
    the other records, so each is read with its control and bidirectional characters stripped (R71)."""
    out = []
    for path in sorted(_folder(project, audit_id).glob("*.json")):
        try:
            doc = vmap.clean_record(core.load_json(path.read_text()))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and doc.get("journey"):
            out.append(doc)
    return out
