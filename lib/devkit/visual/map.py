# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The product map of visual diagnosis: one versioned graph of a project's screens, overlays, pages, and slides
(nodes), the ways between them with what triggers each (edges), the journeys through it, the state machines of the
nodes that need them, and the concepts and components the product shows.

Where it lives (the handoff repository, so the team and the next session see it):

  visual/<owner>/<repo>/map/current.json   the map, with its version and changelog
  visual/<owner>/<repo>/map/v<n>.json      every version as it was written, never changed afterwards

Every change writes the next version (R42): `change` reads the map, applies one change, and `save` writes it, both
holding this Mac's lock on the map. A map that changed elsewhere since it was read (a teammate's version that a sync
brought in while a crawl ran) is refused with the version and the Mac that wrote it, never merged.

Every string written to the map passes redact.cjs first (R48), because the map syncs without a capture: `save` sends
all of them in one batch to `node redact.cjs --json`, which strips control and bidirectional characters (R71), turns
a string with a secret into `[redacted: <rule>]` (R14, gitleaks), and redacts credentials in the URLs inside it (R15).
A string that is an identifier (a node, edge, journey, or component id, a state or concept name) cannot be replaced
without breaking what points at it, so a change whose identifier the redaction would alter is refused instead.
When node or gitleaks is missing, nothing is written. A map read back has its control and bidirectional characters
stripped (`clean_record`, which the store and the report read their records through too), whatever a sync brought in. Routes become route patterns through redact.cjs's routePattern
(numbers, UUIDs, and long hex values are `:id`, token-shaped segments `:token`; query, user info, and any fragment but
a hash route are dropped).

`crawl` builds or updates the map from a start URL with lib/devkit/visual/crawl.cjs (R48, R70): same origin, GET only,
breadth first, with its limits and side-effect guards. A crawled node or edge joins the declared one at its route; a
crawl never deletes a declared node or edge, and one that a complete crawl no longer finds is marked `missing since
v<n>` (R49). A node can list the query parameters that make a separate screen at its path (`params: ["tab"]`), so
/settings?tab=billing and /settings?tab=profile are two nodes; `route_of` gives any URL's route the same way.

When the map changes, the captures that match exactly one node by route pattern or page attach to it, and their
findings follow (R60); `attach` also lists the captures that match more than one node and attaches one by hand.
Capture and finding records are the store's (visual/<owner>/<repo>/captures/VC-….json, findings/VF-….json); this
module sets only their map fields and adds to their history.

The `dev visual map` commands call these functions; `by` names who acts (the crew member or the user) and defaults to
the configured GitHub user, then $USER. The structure checks of `dev visual map check` (R52, R53) close the module:
`check` turns the map, the audit's walks, and its captures into structure signals, numbered on the audit (VA-…#n);
lib/devkit/visual/mapdiagram.py draws the map and its journeys.
"""
from __future__ import annotations

import copy
import difflib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .. import core, identity

REDACT = Path(__file__).resolve().with_name("redact.cjs")
CRAWLER = Path(__file__).resolve().with_name("crawl.cjs")
CRAWL_TIMEOUT = 600  # seconds: a crawl's own bound (R74); the process gets two more minutes to stop and redact
REDACT_TIMEOUT = 120  # seconds for one batch: node's start, gitleaks over every string of the map
NODE_KINDS = ("screen", "overlay", "page", "slide", "external")
EDGE_KINDS = ("nav", "action", "submit", "redirect", "back", "next", "external")
ITEM_ID = re.compile(r"[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9_-]+)*")  # nodes and components: invoices.list
JOURNEY_ID = re.compile(r"J:[a-z0-9][a-z0-9-]{0,59}")
NAME = re.compile(r"\w[\w ,.'-]{0,59}")  # states and concepts: "loading", "export failed", "Invoice"
PROJECT_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
CAPTURE_ID = re.compile(r"VC-[0-9a-z]{1,40}")
# C0 and C1 control characters but tab and newline, and the bidirectional controls (R71)
CONTROLS = re.compile("[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
ROLE_NAME = (re.compile(r'role=([a-z]+)\[name=(["\'])(.*)\2\]'), re.compile(r'([a-z]+) (["\'])(.*)\2'))
BUSY = "Another devkit command is changing the map. Run this one again when it ends."

# The strings of a map that are identifiers: other records point at them, so redaction may not replace them.
_ID_PATHS = {
    ("start",): "The start node", ("nodes", "*", "id"): "A node id", ("nodes", "*", "kind"): "A node kind",
    ("nodes", "*", "origin"): "A node's origin", ("nodes", "*", "states", "initial"): "A state name",
    ("nodes", "*", "states", "states", "*"): "A state name",
    ("nodes", "*", "states", "events", "*", "from"): "A state name",
    ("nodes", "*", "states", "events", "*", "to"): "A state name",
    ("edges", "*", "id"): "An edge id", ("edges", "*", "from"): "An edge's node",
    ("edges", "*", "to"): "An edge's node",
    ("edges", "*", "kind"): "An edge kind", ("edges", "*", "origin"): "An edge's origin",
    ("journeys", "*", "id"): "A journey id", ("journeys", "*", "start"): "A journey's node",
    ("journeys", "*", "end"): "A journey's node", ("journeys", "*", "origin"): "A journey's origin",
    ("journeys", "*", "steps", "*", "edge"): "A journey's edge", ("concepts", "*", "name"): "A concept name",
    ("concepts", "*", "relates", "*", "to"): "A concept name", ("components", "*", "id"): "A component id",
    ("changelog", "*", "at"): "A change's time",
}
_APPLY_FIELDS = {
    "": {"start", "nodes", "edges", "journeys", "concepts", "components"},
    "node": {"id", "title", "route", "page", "kind", "final", "section", "states", "params"},
    "edge": {"from", "to", "trigger", "kind", "when"},
    "journey": {"id", "actor", "task", "path", "do", "decisions", "expect", "budget", "keeps"},
    "concept": {"name", "relates", "labels"},
    "component": {"id", "selector"},
    "states": {"initial", "states", "events"},
}
# routePattern of every path in the JSON list on stdin, each stripped of control characters first (R71), so a token
# that a control character splits still has its shape
_ROUTE_JS = ("const r = require(process.argv[1]);"
             "const paths = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
             "process.stdout.write(JSON.stringify(paths.map((p) => r.routePattern(r.strip(p).text))));")
# crawl.cjs's routeOf of every URL in the JSON list on stdin, with the screens of the map in argv[2]
_ROUTE_OF_JS = ("const c = require(process.argv[1]); const screens = JSON.parse(process.argv[2]);"
                "const urls = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
                "process.stdout.write(JSON.stringify(urls.map((u) => c.routeOf(u, screens))));")


class MapError(ValueError):
    """devkit refuses a map change or read; the message says why and what to do. Nothing was written."""


class MapConflict(MapError):
    """The map changed elsewhere since it was read; the message names the version and the Mac."""


# --------------------------------------------------------------------------- where the map lives

def root(project: str) -> Path:
    """visual/<project>/ in the handoff repository. `project`: the project id, owner/repo (or host/group/…/repo)."""
    parts = str(project or "").split("/")
    if len(parts) < 2 or not all(PROJECT_PART.fullmatch(p) for p in parts):
        raise MapError(f'"{project}" is not a project id such as owner/repo.')
    return core.HANDOFF / "visual" / Path(*parts)


def map_dir(project: str) -> Path:
    return root(project) / "map"


def empty() -> dict:
    """The map of a project that has none yet: version 0, nothing in it."""
    return {"version": 0, "partial": False, "start": None, "nodes": [], "edges": [], "journeys": [], "concepts": [],
            "components": [], "changelog": []}


def clean_record(value):
    """A record or map as read: every string and key without control or bidirectional characters (R71), so a file
    a sync brought in, or a teammate edited by hand, never prints a terminal escape or reorders what a reader sees."""
    if isinstance(value, dict):
        return {CONTROLS.sub("", k) if isinstance(k, str) else k: clean_record(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_record(v) for v in value]
    return CONTROLS.sub("", value) if isinstance(value, str) else value


def _read(path: Path):
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    try:
        doc = clean_record(json.loads(text))
    except ValueError:
        doc = None
    if not isinstance(doc, dict) or not isinstance(doc.get("version"), int):
        raise MapError(f"{core.tilde(path)} is not a map devkit can read. Restore it from the handoff repository's "
                       f"history: git log -- {path.name}")
    return {**empty(), **doc}


def load(project: str, version: int = None) -> dict:
    """The current map, or version `version` (map/v<n>.json); the empty map when the project has none."""
    d = map_dir(project)
    current = _read(d / "current.json")
    if version is None:
        return current or empty()
    if current is not None and current["version"] == version:
        return current
    old = _read(d / f"v{int(version)}.json")
    if old is None:
        at = current["version"] if current else 0
        raise MapError(f"No map v{version}: the map is at v{at}." if at else
                       f"No map v{version}: the project has no map yet.")
    return old


# --------------------------------------------------------------------------- redaction through redact.cjs (R48)

def _node() -> str:
    exe = shutil.which("node")
    if not exe:
        raise MapError("node is not installed, so the map's text cannot be checked for secrets. Nothing was written. "
                       "Install it: dev install --only tools.")
    return exe


def _run_node(args: list, payload, timeout: int = REDACT_TIMEOUT, late: str = None) -> str:
    try:
        p = subprocess.run([_node(), *args], input=json.dumps(payload), capture_output=True, text=True,
                           timeout=timeout, cwd=str(REDACT.parent))
    except subprocess.TimeoutExpired:
        raise MapError(late or f"The map's text was not checked for secrets within {timeout} s. Nothing was written.")
    except OSError as e:
        raise MapError(f"node did not start ({e.strerror}), so the map's text was not checked for secrets. "
                       "Nothing was written.")
    if p.returncode != 0:
        lines = [line.strip() for line in p.stderr.splitlines() if line.strip()]
        said = next((line for line in lines if line.startswith("✗")), lines[-1] if lines else "")
        said = said.lstrip("✗").strip() or f"redact.cjs ended with exit {p.returncode}."
        raise MapError(said.replace("Nothing was recorded.", "Nothing was written."))
    return p.stdout


def redact_strings(strings: list) -> dict:
    """Every string through `node redact.cjs --json`. -> {"strings": [...], "secret_hits": n, "controls_stripped": n,
    "urls_redacted": n}; MapError when node or gitleaks is missing or failed."""
    out = json.loads(_run_node([str(REDACT), "--json"], {"strings": list(strings)}))
    if not isinstance(out, dict) or not isinstance(out.get("strings"), list) or len(out["strings"]) != len(strings):
        raise MapError("redact.cjs answered with something devkit cannot read. Nothing was written.")
    return out


def route_patterns(paths: list) -> list:
    """The route pattern of each URL or path, from redact.cjs's routePattern after its control characters are
    stripped: /invoices/1204 -> /invoices/:id, /reset/<a JWT>?api_key=k -> /reset/:token, /#/a/7?x=1 -> /#/a/:id."""
    paths = list(paths)
    if not paths:
        return []
    if not all(isinstance(p, str) and p.strip() for p in paths):
        raise MapError("A route is a URL or a path, such as /invoices/:id.")
    out = json.loads(_run_node(["-e", _ROUTE_JS, str(REDACT)], paths))
    if not isinstance(out, list) or len(out) != len(paths) or not all(isinstance(p, str) for p in out):
        raise MapError("redact.cjs answered with something devkit cannot read. Nothing was written.")
    return out


def _slots(value, path=()):
    """Every string in a map, as (container, key or index, path); list positions are "*" in the path."""
    items = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
    for key, item in items:
        where = path + ((key,) if isinstance(value, dict) else ("*",))
        if isinstance(item, str):
            yield value, key, where
        else:
            yield from _slots(item, where)


def _redact_map(m: dict) -> dict:
    slots = list(_slots(m))
    before = [container[key] for container, key, _ in slots]
    after = redact_strings(before)["strings"]
    for (container, key, where), old, new in zip(slots, before, after):
        if new == old:
            continue
        if where in _ID_PATHS:
            hit = re.match(r"\[redacted: ([^\]]+)\]$", str(new))
            why = f"gitleaks: {hit.group(1)}" if hit else "control characters or a credential in a URL"
            raise MapError(f"{_ID_PATHS[where]} holds text the map cannot store ({why}). Choose another. "
                           "Nothing was written.")
        container[key] = new
    return m


# --------------------------------------------------------------------------- versions (R42)

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _who() -> str:
    return core.CFG.get("github_user") or os.environ.get("USER") or "unknown"


def _check(m: dict) -> None:
    """The map's references hold: unique ids, edges between nodes, journeys over edges, labels on nodes."""
    ids = [n["id"] for n in m["nodes"]]
    edges = [e["id"] for e in m["edges"]]
    for what, seen in (("node", ids), ("edge", edges), ("journey", [j["id"] for j in m["journeys"]]),
                       ("component", [c["id"] for c in m["components"]]),
                       ("concept", [c["name"] for c in m["concepts"]])):
        twice = sorted({i for i in seen if seen.count(i) > 1})
        if twice:
            raise MapError(f"The map would hold the {what} {', '.join(twice)} twice. Nothing was written.")
    known = set(ids)
    for e in m["edges"]:
        if e["from"] not in known or e["to"] not in known or e["id"] != f"{e['from']}>{e['to']}":
            raise MapError(f"The edge {e['id']} joins nodes the map does not have. Nothing was written.")
    for j in m["journeys"]:
        if j["start"] not in known or j["end"] not in known or any(s["edge"] not in edges for s in j["steps"]):
            raise MapError(f"The journey {j['id']} goes over nodes or edges the map does not have. "
                           "Nothing was written.")
    for c in m["concepts"]:
        if any(nid not in known for nid in c["labels"]):
            raise MapError(f"The concept {c['name']} labels a node the map does not have. Nothing was written.")
    for n in m["nodes"]:
        machine = n.get("states")
        if machine and (machine["initial"] not in machine["states"] or
                        any(ev["from"] not in machine["states"] or ev["to"] not in machine["states"]
                            for ev in machine["events"])):
            raise MapError(f"The states of {n['id']} name a state they do not have. Nothing was written.")
    if m["start"] is not None and m["start"] not in known:
        raise MapError(f"The start node {m['start']} is not in the map. Nothing was written.")


def save(project: str, m: dict, base: int, why: str, by: str = None) -> dict:
    """Write `m` as the version after `base`, the version it was read at: every string through redact.cjs, then
    map/v<n>.json and map/current.json, each in one step. Refused (MapConflict) when the map is no longer at `base`:
    a change written elsewhere since the read is never overwritten. -> the map as written."""
    why = str(why or "").strip()
    if not why:
        raise MapError('Say why the map changes: --why "…".')
    d = map_dir(project)
    current_file = d / "current.json"
    with core.file_lock(current_file) as held:
        if not held:
            raise MapError(BUSY)
        current = _read(current_file)
        at = current["version"] if current else 0
        if at != base:
            host = ((current or {}).get("changelog") or [{}])[-1].get("host") or "another Mac"
            raise MapConflict(f"The map changed to v{at} on {host}. Run the command again on v{at}.")
        new = copy.deepcopy({**empty(), **m})
        new["version"] = base + 1
        new["changelog"] = list((current or {}).get("changelog") or []) + [
            {"v": base + 1, "at": _now(), "by": by or _who(), "host": identity.hostname(), "why": why}]
        _check(new)
        new = _redact_map(new)
        text = json.dumps(new, indent=2, ensure_ascii=False) + "\n"
        version_file = d / f"v{new['version']}.json"
        if version_file.exists() and version_file.read_text() != text:
            raise MapError(f"{core.tilde(version_file)} exists already, and an earlier version is never changed. "
                           "Nothing was written.")
        d.mkdir(parents=True, exist_ok=True)
        core.replace_text(version_file, text)
        core.replace_text(current_file, text)
    return new


def change(project: str, fn, why: str = None, by: str = None) -> dict:
    """One change to the map: read it, let fn(map, version) change it in place (`version` is the one being
    written; fn returns what it did, the changelog's why when `why` is not given), save it as that version, then
    attach captures and findings (R60), all while holding the map's lock. -> {"map": …, "attach": attach()'s}."""
    with core.file_lock(map_dir(project) / "current.json") as held:
        if not held:
            raise MapError(BUSY)
        m = load(project)
        base = m["version"]
        work = copy.deepcopy(m)
        did = fn(work, base + 1)
        new = save(project, work, base, why or did, by)
    return {"map": new, "attach": attach(project, by=by, current=new)}


# --------------------------------------------------------------------------- lookups

def _nearest(name: str, names: list) -> str:
    near = difflib.get_close_matches(name, names, n=1, cutoff=0.6)
    return f" Nearest: {near[0]}." if near else ""


def _node_of(m: dict, nid: str) -> dict:
    for n in m["nodes"]:
        if n["id"] == nid:
            return n
    if not m["version"] and not m["nodes"]:
        raise MapError(f'No node "{nid}": the project has no map yet. Add nodes with dev visual map node add.')
    raise MapError(f'No node "{nid}" in map v{m["version"]}.{_nearest(str(nid), [n["id"] for n in m["nodes"]])} '
                   "List them with dev visual map show.")


def _put(items: list, key: str, record: dict) -> str:
    """Add `record` to `items`, or replace the one with the same `key`, keeping the version it arrived in."""
    for i, old in enumerate(items):
        if old[key] == record[key]:
            if "since" in old:
                record["since"] = old["since"]
            items[i] = record
            return "changed"
    items.append(record)
    return "added"


def _text(value, what: str, required: bool = True):
    text = str(value).strip() if value is not None else ""
    if required and not text:
        raise MapError(f"Give {what}.")
    return text or None


def match_nodes(m: dict, route: str = None, page: int = None) -> list:
    """The ids of the nodes a capture belongs to: by its route pattern (web, redact.cjs's routePattern) or its page
    number (documents). A capture belongs to a node only when exactly one matches (R50, R60)."""
    if route is not None:
        return [n["id"] for n in m["nodes"] if n.get("route") == route]
    if page is not None:
        return [n["id"] for n in m["nodes"] if n.get("page") == page]
    return []


# --------------------------------------------------------------------------- declared items (R43-R47, R61)

def _node_id(nid) -> str:
    if not (isinstance(nid, str) and len(nid) <= 80 and ITEM_ID.fullmatch(nid)):
        raise MapError(f'"{nid}" is not a node id: use lowercase letters, digits, dots, dashes, and underscores, '
                       "such as invoices.list.")
    return nid


def _where(nid: str, route, page):
    if (route is None) == (page is None):
        raise MapError(f'Give node {nid} a route (--route "/invoices/:id") or a page (--page 3): one of the two.')
    if page is not None and (isinstance(page, bool) or not isinstance(page, int) or page < 1):
        raise MapError(f"The page of node {nid} is a number from 1.")


def _section(route):
    for part in re.findall(r"[^/#]+", (route or "").split("?")[0]):
        if not part.startswith(":"):
            return part
    return None


def _params(nid, params):
    names = [str(p).strip() for p in params or ()]
    if not all(re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", n) for n in names):
        raise MapError(f"The screen parameters of node {nid} are query parameter names, such as --params tab.")
    return list(dict.fromkeys(names))


def _put_node(m, version, nid, title, route=None, page=None, kind=None, final=False, section=None,
              origin="declared", example=None, params=None) -> str:
    _node_id(nid)
    _where(nid, route, page)
    kind = kind or ("page" if page is not None else "screen")
    if kind not in NODE_KINDS:
        raise MapError(f"A node is a {', '.join(NODE_KINDS[:-1])}, or {NODE_KINDS[-1]}, not {kind}.")
    old = next((n for n in m["nodes"] if n["id"] == nid), None)
    record = {"id": nid, "title": _text(title, f'node {nid} a title (--title "…")'), "kind": kind, "route": route,
              "page": page, "section": _text(section, "", required=False) or _section(route), "origin": origin,
              "since": version, "missing_since": None, "final": bool(final), "example": example}
    if old and old.get("states"):
        record["states"] = old["states"]
    params = _params(nid, params) if params is not None else (old or {}).get("params")
    if params:
        record["params"] = params
    did = _put(m["nodes"], "id", record)
    if m["start"] is None:
        m["start"] = nid
    return f"node {nid} {did}"


def parse_trigger(trigger) -> dict:
    """An edge's trigger: {"role", "name", "selector"} from `button "Export"`, `role=button[name="Export"]`, a CSS
    selector, or such a dict."""
    if isinstance(trigger, dict):
        unknown = set(trigger) - {"role", "name", "selector"}
        if unknown:
            raise MapError(f"A trigger has a role, a name, and a selector, not {', '.join(sorted(unknown))}.")
        return {k: _text(trigger.get(k), "", required=False) for k in ("role", "name", "selector")}
    text = _text(trigger, "", required=False)
    for form in ROLE_NAME:
        hit = form.fullmatch(text or "")
        if hit:
            return {"role": hit.group(1), "name": hit.group(3), "selector": None}
    return {"role": None, "name": None, "selector": text}


def _put_edge(m, version, source, target, trigger, kind=None, when=None, origin="declared") -> str:
    _node_of(m, source)
    _node_of(m, target)
    kind = kind or "nav"
    if kind not in EDGE_KINDS:
        raise MapError(f"An edge is {', '.join(EDGE_KINDS[:-1])}, or {EDGE_KINDS[-1]}, not {kind}.")
    found = parse_trigger(trigger)
    if not any(found.values()):
        raise MapError('Give the edge its trigger: the control\'s role and name (--trigger \'button "Export"\') or '
                       "its selector.")
    eid = f"{source}>{target}"
    record = {"id": eid, "from": source, "to": target, "kind": kind, "trigger": found,
              "when": _text(when, "", required=False), "origin": origin, "since": version, "missing_since": None}
    return f"edge {eid} {_put(m['edges'], 'id', record)}"


def _steps_do(do, count: int) -> list:
    if not do:
        return [[] for _ in range(count)]
    if len(do) != count:
        raise MapError(f"--do gives {len(do)} steps and the path has {count}: one --do for each step.")
    out = []
    for step in do:
        actions = [step] if isinstance(step, str) else list(step)
        if not all(isinstance(a, str) and a.strip() for a in actions):
            raise MapError("Each --do is a recipe step, such as \"click role=button[name='Export']\".")
        out.append([a.strip() for a in actions])
    return out


def _put_journey(m, version, jid, actor, task, path, do=None, decisions=(), expect=None, budget=None, keeps=(),
                 origin="declared") -> str:
    jid = jid if str(jid).startswith("J:") else f"J:{jid}"
    if not JOURNEY_ID.fullmatch(jid):
        raise MapError(f'"{jid}" is not a journey id: J: and lowercase letters, digits, and dashes, such as '
                       "J:export-month.")
    nodes = [p.strip() for p in path.split(">")] if isinstance(path, str) else [str(p).strip() for p in path or ()]
    if len(nodes) < 2 or not all(nodes):
        raise MapError('A journey\'s path names two nodes or more: --path "home > invoices.list".')
    for nid in nodes:
        _node_of(m, nid)
    edges = {e["id"] for e in m["edges"]}
    pairs = list(zip(nodes, nodes[1:]))
    for a, b in pairs:
        if f"{a}>{b}" not in edges:
            raise MapError(f'No edge {a}>{b} in map v{m["version"]}. Add it first: '
                           f'dev visual map edge add {a} {b} --trigger "…"')
    steps = [{"edge": f"{a}>{b}", "do": d, "expect": None, "decision": None}
             for (a, b), d in zip(pairs, _steps_do(do, len(pairs)))]
    points = decisions.items() if isinstance(decisions, dict) else (
        str(d).partition(":")[::2] for d in decisions or ())
    for nid, condition in points:
        nid = nid.strip()
        condition = _text(condition, f'the decision at {nid} its condition ("<node>: <condition>")')
        step = next((s for s, (a, _) in zip(steps, pairs) if a == nid), None)
        if step is None:
            raise MapError(f"{nid} is not a step of {jid}'s path, so it cannot hold a decision.")
        step["decision"] = condition
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int) or budget < 1):
        raise MapError("A journey's budget is a number of steps, 1 or more.")
    keeps = [_text(k, "each value the journey keeps a name (--keeps month)") for k in keeps or ()]
    record = {"id": jid, "actor": _text(actor, f'journey {jid} its actor (--actor "…")'),
              "task": _text(task, f'journey {jid} its task (--task "…")'), "start": nodes[0], "end": nodes[-1],
              "budget": budget, "keeps": keeps, "expect": _text(expect, "", required=False), "origin": origin,
              "since": version, "steps": steps}
    return f"journey {jid} {_put(m['journeys'], 'id', record)}"


def _name(value, what: str) -> str:
    text = str(value or "").strip()
    if not NAME.fullmatch(text):
        raise MapError(f'"{text}" is not a {what}: up to 60 letters, digits, spaces, and , . \' - characters.')
    return text


def _put_state(m, nid, state, from_state=None, on=None) -> str:
    node = _node_of(m, nid)
    state = _name(state, "state name")
    machine = node.get("states") or {"initial": state, "states": [], "events": []}
    if from_state is None and on is None:
        did = f"state {state} of {nid} added"
    else:
        if from_state is None or not str(on or "").strip():
            raise MapError('--from and --on go together: the state the event leaves and the event '
                           '(--from loading --on "invoices loaded").')
        if from_state not in machine["states"] and from_state != state:
            raise MapError(f'No state "{from_state}" on {nid}.{_nearest(str(from_state), machine["states"])}')
        event = {"from": from_state, "to": state, "on": str(on).strip()}
        if event not in machine["events"]:
            machine["events"].append(event)
        did = f"state {state} of {nid} added, reached from {from_state} on {event['on']}"
    if state not in machine["states"]:
        machine["states"].append(state)
    node["states"] = machine
    return did


def _put_concept(m, name, relates=(), labels=()) -> str:
    name = _name(name, "concept name")
    concept = next((c for c in m["concepts"] if c["name"] == name), None)
    if concept is None:
        concept = {"name": name, "relates": [], "labels": {}}
        m["concepts"].append(concept)
    for rel in relates or ():
        if isinstance(rel, dict):
            other, how = rel.get("to", ""), rel.get("as", "")
        else:
            other, _, how = str(rel).partition(":")
        other = _name(other, "concept name")
        link = {"to": other, "as": _text(how, f'the relation to {other} a name ("Customer:belongs to")')}
        if link not in concept["relates"]:
            concept["relates"].append(link)
    pairs = labels.items() if isinstance(labels, dict) else (str(lb).partition("=")[::2] for lb in labels or ())
    for nid, shown in pairs:
        nid = nid.strip()
        _node_of(m, nid)
        concept["labels"][nid] = _text(shown, f'the label of {name} on {nid} its text ("{nid}=Invoice")')
    return f"concept {name} declared"


def _put_component(m, cid, selector) -> str:
    if not (isinstance(cid, str) and len(cid) <= 80 and ITEM_ID.fullmatch(cid)):
        raise MapError(f'"{cid}" is not a component id: use lowercase letters, digits, dots, dashes, and underscores, '
                       "such as ui.button.primary.")
    record = {"id": cid, "selector": _text(selector, f'component {cid} its selector pattern (--selector "…")')}
    return f"component {cid} {_put(m['components'], 'id', record)}"


def _route(route):
    return route_patterns([route])[0] if route is not None else None


def add_node(project, node_id, title, *, route=None, page=None, kind=None, final=False, section=None, params=None,
             by=None, why=None) -> dict:
    """`dev visual map node add`: a declared node, or new fields for one the map has. `route`: a URL or path, stored
    as its route pattern; `page`: a document page or slide number; `params`: the query parameters that make a
    separate screen at the route's path (["tab"]: /settings?tab=billing is its own node), which the route keeps."""
    _where(_node_id(node_id), route, page)
    if params is not None:
        params = _params(node_id, params)
        if params and route is None:
            raise MapError(f"Screen parameters belong to a route: give node {node_id} --route with --params.")
    pattern = _route(route) if not params else route_of({"nodes": [{"route": _route(route), "params": params}]},
                                                        route)
    return change(project, lambda m, v: _put_node(m, v, node_id, title, route=pattern, page=page, kind=kind,
                                                  final=final, section=section, params=params), why, by)


def add_edge(project, source, target, *, trigger, kind=None, when=None, by=None, why=None) -> dict:
    """`dev visual map edge add`: a declared edge from `source` to `target` with its trigger and, optionally, the
    condition under which it exists (`when`)."""
    return change(project, lambda m, v: _put_edge(m, v, source, target, trigger, kind=kind, when=when), why, by)


def add_journey(project, journey_id, *, actor, task, path, do=None, decisions=(), expect=None, budget=None,
                keeps=(), by=None, why=None) -> dict:
    """`dev visual map journey add`: the journey along `path` ("a > b > c", or a list), each step one edge the map
    has. `do`: one entry per step, a recipe step or a list of them; `decisions`: "<node>: <condition>" (or a dict),
    held by the step that leaves that node; `keeps`: the values the journey must keep."""
    return change(project, lambda m, v: _put_journey(m, v, journey_id, actor, task, path, do=do, decisions=decisions,
                                                     expect=expect, budget=budget, keeps=keeps), why, by)


def add_state(project, node_id, state, *, from_state=None, on=None, by=None, why=None) -> dict:
    """`dev visual map state add`: a state of the node's state machine (the first one is its initial state), and
    with `from_state` and `on` the event that leads to it."""
    return change(project, lambda m, v: _put_state(m, node_id, state, from_state=from_state, on=on), why, by)


def add_concept(project, name, *, relates=(), labels=(), by=None, why=None) -> dict:
    """`dev visual map concept add`: a concept, its relations ("Customer:belongs to"), and the label each node
    shows for it ("invoices.list=Invoice"); declaring it again adds to what it has."""
    return change(project, lambda m, v: _put_concept(m, name, relates=relates, labels=labels), why, by)


def add_component(project, component_id, *, selector, by=None, why=None) -> dict:
    """`dev visual map component add`: a component and its selector pattern (R61)."""
    return change(project, lambda m, v: _put_component(m, component_id, selector), why, by)


# --------------------------------------------------------------------------- removal

def _uses(m, nid=None, eid=None) -> list:
    out = []
    for j in m["journeys"]:
        edges = [s["edge"] for s in j["steps"]]
        if (eid and eid in edges) or (nid and (nid in (j["start"], j["end"]) or
                                               any(nid in e.split(">") for e in edges))):
            out.append(j["id"])
    return out


def _refuse_used(what: str, journeys: list):
    if journeys:
        raise MapError(f"{what} is in journey {journeys[0]}. Remove the journey first: "
                       f'dev visual map remove {journeys[0]} --why "…"')


def _remove(m: dict, item: str) -> str:
    item = str(item or "").strip()
    if item.startswith("J:"):
        if not any(j["id"] == item for j in m["journeys"]):
            raise MapError(f'No journey "{item}" in map v{m["version"]}.'
                           f'{_nearest(item, [j["id"] for j in m["journeys"]])}')
        m["journeys"] = [j for j in m["journeys"] if j["id"] != item]
        return f"journey {item} removed"
    if ">" in item:
        if not any(e["id"] == item for e in m["edges"]):
            raise MapError(f'No edge "{item}" in map v{m["version"]}.{_nearest(item, [e["id"] for e in m["edges"]])}')
        _refuse_used(item, _uses(m, eid=item))
        m["edges"] = [e for e in m["edges"] if e["id"] != item]
        return f"edge {item} removed"
    if "#" in item:
        nid, _, state = item.partition("#")
        machine = _node_of(m, nid).get("states") or {"states": []}
        if state not in machine["states"]:
            raise MapError(f'No state "{state}" on {nid}.{_nearest(state, machine["states"])}')
        machine["states"].remove(state)
        machine["events"] = [ev for ev in machine["events"] if state not in (ev["from"], ev["to"])]
        if machine["initial"] == state:
            machine["initial"] = machine["states"][0] if machine["states"] else None
        if not machine["states"]:
            del _node_of(m, nid)["states"]
        return f"state {state} of {nid} removed"
    kinds = [k for k, names in (("node", [n["id"] for n in m["nodes"]]),
                                ("component", [c["id"] for c in m["components"]]),
                                ("concept", [c["name"] for c in m["concepts"]])) if item in names]
    if not kinds:
        names = [n["id"] for n in m["nodes"]] + [c["id"] for c in m["components"]] + [c["name"] for c in m["concepts"]]
        raise MapError(f'Nothing called "{item}" in map v{m["version"]}.{_nearest(item, names)} '
                       "List them with dev visual map show.")
    if len(kinds) > 1:
        raise MapError(f'"{item}" names a {kinds[0]} and a {kinds[1]}: remove one with dev visual map apply.')
    if kinds == ["node"]:
        _refuse_used(item, _uses(m, nid=item))
        m["nodes"] = [n for n in m["nodes"] if n["id"] != item]
        m["edges"] = [e for e in m["edges"] if item not in (e["from"], e["to"])]
        for c in m["concepts"]:
            c["labels"].pop(item, None)
        if m["start"] == item:
            m["start"] = None
    elif kinds == ["component"]:
        m["components"] = [c for c in m["components"] if c["id"] != item]
    else:
        m["concepts"] = [c for c in m["concepts"] if c["name"] != item]
        for c in m["concepts"]:
            c["relates"] = [r for r in c["relates"] if r["to"] != item]
    return f"{kinds[0]} {item} removed"


def remove(project, item, *, why, by=None) -> dict:
    """`dev visual map remove`: a node (with its edges and labels), an edge (from>to), a journey (J:…), a state
    (node#state), a component, or a concept. Refused while a journey goes over the node or edge."""
    if not str(why or "").strip():
        raise MapError('Say why it goes: --why "…".')
    return change(project, lambda m, v: _remove(m, item), why, by)


# --------------------------------------------------------------------------- a declared map from a file

def _fields(item, kind: str) -> dict:
    if not isinstance(item, dict):
        raise MapError(f"Each {kind or 'map'} in the file is a JSON object.")
    unknown = set(item) - _APPLY_FIELDS[kind]
    if unknown:
        raise MapError(f"A {kind or 'declared map'} has no field {', '.join(sorted(unknown))}; it takes "
                       f"{', '.join(sorted(_APPLY_FIELDS[kind]))}.")
    return item


def _put_states(m, nid, machine) -> None:
    machine = _fields(machine, "states")
    for state in machine.get("states") or ():
        _put_state(m, nid, state)
    for ev in machine.get("events") or ():
        ev = ev if isinstance(ev, dict) else {}
        _put_state(m, nid, ev.get("to"), from_state=ev.get("from"), on=ev.get("on"))
    if machine.get("initial") is not None:
        node = _node_of(m, nid)
        if machine["initial"] not in (node.get("states") or {}).get("states", ()):
            raise MapError(f'The initial state "{machine["initial"]}" of {nid} is not one of its states.')
        node["states"]["initial"] = machine["initial"]


def apply(project, doc: dict, *, why, by=None) -> dict:
    """`dev visual map apply <file.json>`: a declared map, or additions to the map, as one version. `doc` holds any
    of start, nodes ({id, title, route | page, kind, final, section, states}), edges ({from, to, trigger, kind,
    when}), journeys ({id, actor, task, path, do, decisions, expect, budget, keeps}), concepts ({name, relates,
    labels}), and components ({id, selector}), with the meanings of the add functions. All or nothing."""
    _fields(doc, "")
    nodes = [_fields(n, "node") for n in doc.get("nodes") or ()]
    patterns = iter(route_patterns([n["route"] for n in nodes if n.get("route") is not None]))
    routes = [next(patterns) if n.get("route") is not None else None for n in nodes]

    def fn(m, version):
        for n, route in zip(nodes, routes):
            _put_node(m, version, n.get("id"), n.get("title"), route=route, page=n.get("page"), kind=n.get("kind"),
                      final=n.get("final", False), section=n.get("section"), params=n.get("params"))
            if n.get("states") is not None:
                _put_states(m, n["id"], n["states"])
        for e in (_fields(e, "edge") for e in doc.get("edges") or ()):
            _put_edge(m, version, e.get("from"), e.get("to"), e.get("trigger"), kind=e.get("kind"), when=e.get("when"))
        for j in (_fields(j, "journey") for j in doc.get("journeys") or ()):
            _put_journey(m, version, j.get("id", ""), j.get("actor"), j.get("task"), j.get("path"), do=j.get("do"),
                         decisions=j.get("decisions") or (), expect=j.get("expect"), budget=j.get("budget"),
                         keeps=j.get("keeps") or ())
        for c in (_fields(c, "concept") for c in doc.get("concepts") or ()):
            _put_concept(m, c.get("name"), relates=c.get("relates") or (), labels=c.get("labels") or ())
        for c in (_fields(c, "component") for c in doc.get("components") or ()):
            _put_component(m, c.get("id"), c.get("selector"))
        if doc.get("start") is not None:
            m["start"] = _node_of(m, doc["start"])["id"]
        counts = [f"{len(doc.get(k) or ())} {k}" for k in ("nodes", "edges", "journeys", "concepts", "components")
                  if doc.get(k)]
        return "declared map applied: " + ", ".join(counts or ["no items"])

    return change(project, fn, why, by)


# --------------------------------------------------------------------------- captures and findings (R50, R60)

def _record(path: Path):
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _write_record(path: Path, doc: dict) -> None:
    core.replace_text(path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def _component_for(m: dict, selectors: list):
    """The one map component whose selector pattern lists one of `selectors` (whitespace aside), or None. Matching
    against the page happens at capture time, in the description's `component`; this matches what records hold."""
    def norm(s):
        return " ".join(str(s).split())
    wanted = {norm(s) for s in selectors if s}
    found = [c["id"] for c in m["components"] if wanted & {norm(p) for p in c["selector"].split(",")}]
    return found[0] if len(found) == 1 else None


def attach(project, *, choose: dict = None, by: str = None, current: dict = None) -> dict:
    """Attach the project's unattached captures to the one node their route pattern or page matches, and their
    findings with them; a finding whose selector is in one map component's pattern gets that component (R60).
    `choose`: {capture id: node id} for the captures that match more than one node, attached by hand. Each
    attachment goes into the record's history. -> {"attached": [{capture, node}], "ambiguous": [{capture, nodes}],
    "findings": [{finding, node, component}]}"""
    m = current or load(project)
    out = {"attached": [], "ambiguous": [], "findings": []}
    base = root(project)
    choose = dict(choose or {})
    for cid, nid in choose.items():
        _node_of(m, nid)
        if not CAPTURE_ID.fullmatch(str(cid)) or not (base / "captures" / f"{cid}.json").is_file():
            raise MapError(f"No capture {cid} in {project}.")
    if not m["version"]:
        return out
    who, at, v = by or _who(), _now(), m["version"]
    capture_maps = {}
    for path in sorted((base / "captures").glob("VC-*.json")):
        with core.file_lock(path) as held:
            doc = _record(path) if held else None
            if doc is None:
                continue
            cid = doc.get("id") or path.stem
            placed = doc.get("map") if isinstance(doc.get("map"), dict) else {}
            capture_maps[cid] = (placed, doc.get("element"))
            if cid in choose:
                nid = choose[cid]
            elif placed.get("node"):
                continue
            else:
                source = doc.get("source") if isinstance(doc.get("source"), dict) else {}
                found = match_nodes(m, route=source.get("route"), page=source.get("page"))
                if len(found) != 1:
                    if found:
                        out["ambiguous"].append({"capture": cid, "nodes": found})
                    continue
                nid = found[0]
            states = (_node_of(m, nid).get("states") or {}).get("states", ())
            placed = {"node": None, "state": None, "version": None, "journey": None, "step": None, "attached": None,
                      **placed, "node": nid, "version": v, "attached": at}
            if placed["state"] is None and doc.get("state") in states:
                placed["state"] = doc["state"]
            doc["map"] = placed
            doc.setdefault("history", []).append({"at": at, "by": who, "change": f"attached to {nid} (map v{v})"})
            _write_record(path, doc)
            capture_maps[cid] = (placed, doc.get("element"))
            out["attached"].append({"capture": cid, "node": nid})
    for path in sorted((base / "findings").glob("VF-*.json")):
        with core.file_lock(path) as held:
            doc = _record(path) if held else None
            if doc is None or not isinstance(doc.get("anchor"), dict):
                continue
            anchor, changes = doc["anchor"], []
            placed, element = capture_maps.get(doc.get("capture"), ({}, None))
            if not anchor.get("node") and placed.get("node"):
                anchor["node"] = placed["node"]
                anchor["state"] = anchor.get("state") or placed.get("state")
                changes.append(f"attached to {placed['node']} (map v{v})")
            component = None if anchor.get("component") else _component_for(
                m, [anchor.get("selector"), (element or {}).get("selector") if isinstance(element, dict) else None])
            if component:
                anchor["component"] = component
                changes.append(f"attached to component {component} (map v{v})")
            if not changes:
                continue
            doc.setdefault("history", []).extend({"at": at, "by": who, "change": c} for c in changes)
            _write_record(path, doc)
            out["findings"].append({"finding": doc.get("id") or path.stem, "node": anchor.get("node"),
                                    "component": anchor.get("component")})
    return out


# --------------------------------------------------------------------------- the crawl (R48, R49, R70)

def run_crawler(request: dict, timeout: int) -> dict:
    """`node crawl.cjs --json` with `request` (a crawl or a walk); -> its result. MapError with crawl.cjs's message
    when it fails, refuses the request, or does not finish within `timeout` seconds."""
    out = _run_node([str(CRAWLER), "--json"], request, timeout=timeout,
                    late=f"The browser did not finish within {timeout} s. Nothing was written.")
    try:
        doc = json.loads(out)
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        raise MapError("crawl.cjs answered with something devkit cannot read. Nothing was written.")
    return doc


def screens(m: dict) -> dict:
    """What makes a separate screen in the map, as crawl.cjs's routeOf takes it: the query parameters each path's
    nodes list ({"/settings": ["tab"]}) and the routes of overlays with a plain fragment (/invoices#export)."""
    params, overlays = {}, []
    for n in m.get("nodes") or ():
        route = n.get("route") or ""
        path = re.split(r"[?#]", route, maxsplit=1)[0]
        for name in n.get("params") or ():
            if name not in params.setdefault(path, []):
                params[path].append(name)
        fragment = route.partition("#")[2]
        if fragment and not fragment.startswith("/"):
            overlays.append(f"{path}#{fragment}")
    return {"params": params, "overlays": overlays}


def route_of(m: dict, url: str) -> str:
    """The route of a URL in this map: its route pattern, with the query parameters a node lists for its path
    (/settings?tab=billing) and a plain fragment only when a node is at it (an overlay). The capture records'
    `source.route` and the crawl use it, so both match nodes the same way."""
    out = json.loads(_run_node(["-e", _ROUTE_OF_JS, str(CRAWLER), json.dumps(screens(m))], [str(url)]))
    if not isinstance(out, list) or len(out) != 1 or not isinstance(out[0], str):
        raise MapError("crawl.cjs answered with something devkit cannot read. Nothing was written.")
    return out[0]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", str(text).lower()).strip("-_") or "page"


def _crawled_id(c: dict, taken: set) -> str:
    """A node id from a crawled node's route: / is home, /invoices/:id is invoices.id, /settings?tab=billing is
    settings.tab-billing, an external origin external.<host>; a number follows an id already taken."""
    if c["kind"] == "external":
        base = "external." + _slug(re.sub(r"^[a-z]+://", "", c["route"]))
    else:
        path, _, fragment = c["route"].partition("#")
        path, _, query = path.partition("?")
        parts = [x.lstrip(":") for x in path.split("/") + fragment.split("/") if x.lstrip(":")]
        if query:
            parts.append(query)
        base = ".".join(_slug(x) for x in parts) or "home"
    nid, n = base, 2
    while nid in taken:
        nid, n = f"{base}-{n}", n + 1
    return nid


def _merge_crawl(m: dict, found: dict, version: int, within=None) -> dict:
    """Merge crawl.cjs's nodes and edges into `m` (R48, R49): a crawled node joins the node the map has at its route
    (a screen before an overlay), a declared node or edge keeps its declared fields, and after a complete crawl
    (not partial) every crawled node or edge it did not find, within `within` when given, is marked missing since
    `version`. -> {"new_nodes", "new_edges", "missing", "start"}"""
    ids, taken = {}, {n["id"] for n in m["nodes"]}
    new_nodes, new_edges, seen_nodes, seen_edges = [], [], set(), set()
    for c in found.get("nodes") or ():
        same = sorted((n for n in m["nodes"] if n.get("route") == c["route"] and
                       (n["kind"] == "external") == (c["kind"] == "external")), key=lambda n: n["kind"] != "screen")
        if same:
            n = same[0]
            if n.get("origin") == "crawled":
                n.update(title=c.get("title") or c["route"], status=c.get("status"), skipped=c.get("skipped"),
                         missing_since=None)
                n["example"] = n.get("example") or c.get("example")
        else:
            nid = _crawled_id(c, taken)
            taken.add(nid)
            n = {"id": nid, "title": c.get("title") or c["route"], "kind": c["kind"], "route": c["route"],
                 "page": None, "section": "external" if c["kind"] == "external" else _section(c["route"]),
                 "origin": "crawled", "since": version, "missing_since": None, "final": False,
                 "example": c.get("example"), "status": c.get("status"), "skipped": c.get("skipped")}
            m["nodes"].append(n)
            new_nodes.append(nid)
        ids[c["key"]] = n["id"]
        seen_nodes.add(n["id"])
    for e in found.get("edges") or ():
        a, b = ids.get(e.get("from")), ids.get(e.get("to"))
        if not a or not b or a == b:
            continue
        eid = f"{a}>{b}"
        fields = {"kind": e["kind"] if e.get("kind") in EDGE_KINDS else "nav",
                  "trigger": parse_trigger(e.get("trigger") or {}), "status": e.get("status"),
                  "redirects": e.get("redirects") or 0}
        old = next((x for x in m["edges"] if x["id"] == eid), None)
        if old is None:
            m["edges"].append({"id": eid, "from": a, "to": b, "kind": fields["kind"], "trigger": fields["trigger"],
                               "when": None, "origin": "crawled", "since": version, "missing_since": None,
                               "status": fields["status"], "redirects": fields["redirects"]})
            new_edges.append(eid)
        elif old.get("origin") == "crawled":
            old.update(fields, missing_since=None)
        seen_edges.add(eid)
    missing = []
    if not found.get("partial"):
        nodes = {n["id"]: n for n in m["nodes"]}

        def inside(n):
            return within is None or _within(n, within)
        for n in m["nodes"]:
            if n.get("origin") == "crawled" and n["id"] not in seen_nodes and not n.get("missing_since") and inside(n):
                n["missing_since"] = version
                missing.append(n["id"])
        for e in m["edges"]:
            if (e.get("origin") == "crawled" and e["id"] not in seen_edges and not e.get("missing_since") and
                    inside(nodes[e["from"]]) and inside(nodes[e["to"]])):
                e["missing_since"] = version
    start = ids.get(found.get("start"))
    if m["start"] is None:
        m["start"] = start or (new_nodes[0] if new_nodes else None)
    m["base"] = found.get("origin")
    m["partial"] = bool(found.get("partial")) or (bool(m.get("partial")) if within else False)
    return {"new_nodes": new_nodes, "new_edges": new_edges, "missing": missing, "start": start}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _crawl_lines(m: dict, found: dict, merged: dict, attached: dict, max_pages: int) -> list:
    """The terminal copy of a crawl: what it found, what it left alone, and the limit that stopped it. The command
    adds its own next line."""
    nodes = {n["id"]: n for n in m["nodes"]}
    screens_ = [n for n in m["nodes"] if n["kind"] == "screen" and not n.get("missing_since")]
    edges = [e for e in m["edges"] if not e.get("missing_since")]
    new_screens = [i for i in merged["new_nodes"] if nodes[i]["kind"] == "screen"]
    head = (f"✓ map v{m['version']} · {_plural(len(screens_), 'screen')} ({len(new_screens)} new) · "
            f"{_plural(len(edges), 'edge')} ({len(merged['new_edges'])} new)")
    gone = {}
    for n in m["nodes"]:
        if n["kind"] == "screen" and n.get("missing_since"):
            gone.setdefault(n["missing_since"], []).append(n.get("route") or n["id"])
    for since, routes in sorted(gone.items()):
        head += f" · {_plural(len(routes), 'screen')} missing since v{since}: {', '.join(routes)}"
    names, left = found.get("not_clicked") or [], found.get("left") or 0
    second = (f"  stopped at depth {found.get('depth')} · {_plural(left, 'link')} left · "
              f"{_plural(found.get('blocked_count') or 0, 'request')} blocked (not GET) · "
              f"{_plural(len(names), 'control')} not clicked" + (f" ({', '.join(names)})" if names else ""))
    if found.get("excluded"):
        second += f" · {_plural(found['excluded'], 'link')} excluded"
    lines = [head, second]
    stopped = found.get("stopped_by")
    if stopped == "pages":
        lines.append(f"⚠ The crawl stopped at {max_pages} screens with {_plural(left, 'link')} left. The map is "
                     "marked partial. Narrow it with --within, or raise --max.")
    elif stopped == "depth":
        lines.append(f"⚠ The crawl stopped at depth {found.get('depth')} with {_plural(left, 'link')} left. The map "
                     "is marked partial. Narrow it with --within, or raise --depth.")
    elif stopped == "time":
        lines.append(f"⚠ The crawl stopped at its time bound of {(found.get('timeout_s') or CRAWL_TIMEOUT) // 60} "
                     f"minutes with {_plural(left, 'link')} left. The map is marked partial. Narrow it with --within.")
    outside = found.get("external") or 0
    host = re.sub(r"^[a-z]+://", "", str(found.get("origin") or ""))
    if outside == 1:
        lines.append(f"– 1 link leads outside {host}; it is recorded as an external edge and not followed.")
    elif outside:
        lines.append(f"– {outside} links lead outside {host}; they are recorded as external edges and not followed.")
    said = []
    if attached.get("attached"):
        said.append(f"attached {_plural(len(attached['attached']), 'earlier capture')} to their screens")
    if attached.get("ambiguous"):
        said.append(f"{_plural(len(attached['ambiguous']), 'capture')} match more than one screen: "
                    "dev visual map attach")
    if said:
        lines.append("  " + " · ".join(said))
    return lines


def crawl(project, url, *, within=None, depth=3, max_pages=60, avoid=(), exclude=(), storage_state=None, steps=(),
          remote=False, timeout_s=CRAWL_TIMEOUT, by=None, why=None, core=None) -> dict:
    """`dev visual map crawl`, and the crawl of `dev visual section` and `product` (R48, R49, R59, R70): crawl the
    app from `url` with crawl.cjs and write what it found as the next map version.

    `within`: a route prefix the crawl stays under; `depth` and `max_pages`: its limits (a crawl that reaches one
    marks the map partial); `avoid`: words added to devkit's (the project's visual.crawl.avoid and --avoid);
    `exclude`: path globs never requested; `storage_state`: a Playwright storage-state file; `steps`: recipe steps
    run on the start page first; `remote`: allow a host that is not this Mac (R17); `core`: the playwright-core
    folder (default: the pinned @playwright/cli's). -> {"map", "attach", "summary"}: summary holds `lines`, the
    terminal copy, and the counts behind it."""
    request = {"mode": "crawl", "start": str(url), "within": within, "depth": depth, "max": max_pages,
               "avoid": list(avoid or ()), "exclude": list(exclude or ()), "screens": screens(load(project)),
               "storage_state": str(storage_state) if storage_state else None, "steps": list(steps or ()),
               "remote": bool(remote), "timeout_s": timeout_s, "core": core}
    found = run_crawler(request, timeout_s + 120)
    merged = {}

    def fn(m, version):
        merged.update(_merge_crawl(m, found, version, within))
        start = next((n.get("route") for n in m["nodes"] if n["id"] == merged["start"]), None) or str(url)
        return (f"crawl from {start}: {_plural(len(merged['new_nodes']), 'new node')}, "
                f"{_plural(len(merged['new_edges']), 'new edge')}")

    out = change(project, fn, why, by)
    keep = ("partial", "left", "stopped_by", "blocked", "blocked_count", "not_clicked", "excluded", "external",
            "websockets", "timed_out")
    summary = {**{k: found.get(k) for k in keep}, **merged,
               "lines": _crawl_lines(out["map"], found, merged, out["attach"], max_pages)}
    return {**out, "summary": summary}


# --------------------------------------------------------------------------- structure checks (R52, R53)
#
# `dev visual map check` runs these on the map, the audit's walked journeys, and its captures. A walk record (what
# `dev visual journey walk` keeps of one journey) reads:
#
#   {"journey": "J:export-month", "map_version": 4, "broken": null | {"step", "edge", "do", "error"},
#    "steps": [{"step": 1, "node": "home", "edge": null, "status": 200, "redirects": 0, "keeps": {"month": false}},
#              …]}
#
# Steps are numbered as the journey strip shows them: step 1 is the start screen, and step k the screen the k-1th
# edge reaches. `status` is the HTTP status of the document the step loaded (null when the step stays on the page),
# `redirects` the redirects it passed, `keeps` whether each value the journey keeps was on the page at that step
# (present or absent only: the walk tests the live page in memory, R16). `broken` is the step whose trigger the
# walk did not find. A crawled edge may carry `status` and `redirects` the same way.

STRUCTURE_PASSES = ("S1", "S2", "S3", "S4", "S5", "S6")
SCOPE_PASSES = {"journey": ("S1", "S3", "S4", "S6"), "section": STRUCTURE_PASSES, "product": STRUCTURE_PASSES}
DEPTH = 3  # edges from the start (or the section's entry) beyond which a journey or frequent node is too deep
_CHECKS = {  # check: (pass, rules), in the order the signals are listed
    "journey-steps": ("S1", ["map:journey-steps"]), "broken-edge": ("S1", ["map:broken-edge", "nielsen:9"]),
    "redirect-chain": ("S1", ["map:redirect-chain"]), "orphan": ("S2", ["map:orphan"]),
    "dead-end": ("S2", ["map:dead-end", "nielsen:3"]), "depth": ("S2", ["map:depth"]),
    "missing-node": ("S2", ["map:missing"]), "loop": ("S3", ["map:loop"]),
    "label-mismatch": ("S3", ["wcag:2.4.4", "nielsen:4"]), "lost-context": ("S4", ["map:lost-context", "nielsen:6"]),
    "concept-naming": ("S5", ["nielsen:4"]), "component-drift": ("S5", ["ds:component-drift", "nielsen:4"]),
    "missing-state": ("S6", ["map:missing-state"]),
}
_STOP = frozenset("a an and at by for from in into of on or the to with your my our this that".split())
_LOOKS = (("font_size", "font size", " px"), ("font_weight", "font weight", ""), ("color", "text color", ""),
          ("background", "background", ""), ("height", "height", " px"))


def _signal(check, measured, threshold=None, *, journey=None, step=None, edge=None, node=None, state=None) -> dict:
    """A structure signal in the audit's signal shape; `n` is given when the audit numbers it."""
    pass_, rules = _CHECKS[check]
    return {"n": None, "check": check, "pass": pass_, "rule": list(rules),
            "anchor": {"journey": journey, "step": step, "edge": edge, "node": node, "state": state},
            "measured": measured, "threshold": threshold, "triage": None, "duplicates": []}


def signal_key(s: dict) -> tuple:
    """What makes two structure signals the same signal: the check and where it is anchored."""
    a = s.get("anchor") if isinstance(s.get("anchor"), dict) else {}
    return (s.get("check"), a.get("journey"), a.get("step"), a.get("edge"), a.get("node"), a.get("state"))


def ref(audit_id: str, s: dict) -> str:
    """A structure signal's reference: VA-…#<n>."""
    return f"{audit_id}#{s['n']}"


def _within(n: dict, prefix: str) -> bool:
    route, base = n.get("route") or "", prefix.rstrip("/") or "/"
    if route == base or route.startswith(base.rstrip("/") + "/") or route.startswith(base + "#"):
        return True
    parts = [p for p in base.split("/") if p]
    return len(parts) == 1 and n.get("section") == parts[0]


def _distances(m: dict, ids, start) -> dict:
    """Shortest number of edges from `start` to each node of `ids`, over the edges between them that still exist,
    the browser's back button aside."""
    ahead = {i: [] for i in ids}
    for e in m["edges"]:
        if not e.get("missing_since") and e["kind"] != "back" and e["from"] in ahead and e["to"] in ahead:
            ahead[e["from"]].append(e["to"])
    if start not in ahead:
        return {}
    dist, todo = {start: 0}, [start]
    for nid in todo:
        for nxt in ahead[nid]:
            if nxt not in dist:
                dist[nxt] = dist[nid] + 1
                todo.append(nxt)
    return dist


def _journey_nodes(j: dict) -> list:
    return [j["start"]] + [s["edge"].partition(">")[2] for s in j["steps"]]


def _scope(m: dict, audit: dict) -> dict:
    """The nodes, edges, and journeys an audit's scope covers, where orphans and depth are measured from, and the
    passes it runs (R58, R59)."""
    scope = audit.get("scope") or "product"
    live = [n for n in m["nodes"] if not n.get("missing_since")]
    edges = [e for e in m["edges"] if not e.get("missing_since")]
    out = {"passes": SCOPE_PASSES.get(scope, ()), "nodes": [n["id"] for n in live], "all": [n["id"] for n in m["nodes"]],
           "start": m["start"], "entry": None, "journeys": list(m["journeys"])}
    if scope == "journey":
        wanted = audit.get("journeys") or [j["id"] for j in m["journeys"]]
        out["journeys"] = [j for j in m["journeys"] if j["id"] in wanted]
        on = {nid for j in out["journeys"] for nid in _journey_nodes(j)}
        out["nodes"] = [i for i in out["nodes"] if i in on]
        out["all"] = [i for i in out["all"] if i in on]
        steps = {s["edge"] for j in out["journeys"] for s in j["steps"]}
        out["edges"] = [e for e in edges if e["id"] in steps]
        return out
    if scope == "section" and audit.get("within"):
        inside = {n["id"] for n in m["nodes"] if _within(n, str(audit["within"]))}
        out["all"] = [i for i in out["all"] if i in inside]
        out["nodes"] = [i for i in out["nodes"] if i in inside]
        reach = _distances(m, [n["id"] for n in live], m["start"])
        exact = [n["id"] for n in live if n["id"] in inside and n.get("route") == str(audit["within"]).rstrip("/")]
        near = sorted(out["nodes"], key=lambda i: (reach.get(i, len(live) + 1), out["nodes"].index(i)))
        out["start"] = out["entry"] = (exact or near or [None])[0]
        out["journeys"] = [j for j in m["journeys"] if set(_journey_nodes(j)) <= inside]
    out["edges"] = [e for e in edges if e["from"] in out["nodes"] and e["to"] in out["nodes"]]
    return out


def _walk_steps(j: dict, w: dict) -> list:
    """The walk's steps, each with the node and edge it was at (the journey's own when the walk did not say)."""
    declared = _journey_nodes(j)
    out = []
    for s in w.get("steps") or ():
        if not isinstance(s, dict) or not isinstance(s.get("step"), int):
            continue
        k = s["step"]
        node = s.get("node") or (declared[k - 1] if 0 < k <= len(declared) else None)
        edge = s.get("edge") or (j["steps"][k - 2]["edge"] if 1 < k <= len(j["steps"]) + 1 else None)
        out.append({**s, "node": node, "edge": edge})
    return sorted(out, key=lambda s: s["step"])


def _words(text) -> set:
    """The words of a label or heading, for comparing them: lowercase, a plural s dropped, and without small words
    unless the text has nothing else."""
    words = re.findall(r"\w+", str(text or "").lower())
    words = [w for w in words if w not in _STOP] or words
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words}


def _headings(captures, descriptions) -> dict:
    """The top headings each node showed in the audit's captures: {node id: [text, …]}."""
    out = {}
    for c in captures:
        node = (c.get("map") or {}).get("node")
        outline = ((descriptions or {}).get(c.get("id")) or {}).get("outline") or []
        tops = [h.get("text") for h in outline if isinstance(h, dict) and h.get("text")]
        level1 = [h.get("text") for h in outline if isinstance(h, dict) and h.get("level") == 1 and h.get("text")]
        for text in (level1 or tops[:1]):
            if node and text not in out.setdefault(node, []):
                out[node].append(text)
    return out


def _listing(items: list) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _journey_checks(m, sc, walks) -> dict:
    """S1, S3 (loops), and S4 on the journeys the scope covers: {check: [signal, …]}."""
    edges = {e["id"]: e for e in m["edges"]}
    out = {c: [] for c in ("journey-steps", "broken-edge", "redirect-chain", "loop", "lost-context")}
    for j in sc["journeys"]:
        w = walks.get(j["id"])
        if w is None:
            continue
        jid, frames = j["id"], len(j["steps"]) + 1
        if j.get("budget") and frames > j["budget"]:
            out["journey-steps"].append(_signal("journey-steps", f"{jid} takes {frames} steps; its budget is "
                                                f"{j['budget']}", str(j["budget"]), journey=jid))
        steps = _walk_steps(j, w)
        broken = []
        for s in steps:
            if isinstance(s.get("status"), int) and s["status"] >= 400:
                broken.append((s["step"], _signal("broken-edge", f"step {s['step']} answered {s['status']}",
                                                  journey=jid, step=s["step"], edge=s["edge"], node=s["node"])))
            if isinstance(s.get("redirects"), int) and s["redirects"] > 1:
                out["redirect-chain"].append(_signal("redirect-chain", f"step {s['step']} passed {s['redirects']} "
                                                     "redirects", "1", journey=jid, step=s["step"], edge=s["edge"],
                                                     node=s["node"]))
        stop = w.get("broken") if isinstance(w.get("broken"), dict) else None
        if stop and isinstance(stop.get("step"), int):
            said = ", ".join(str(x) for x in (f"step {stop['step']}", stop.get("do"), stop.get("error")) if x)
            edge = stop.get("edge") or (j["steps"][stop["step"] - 2]["edge"]
                                        if 1 < stop["step"] <= len(j["steps"]) + 1 else None)
            broken.append((stop["step"], _signal("broken-edge", said, journey=jid, step=stop["step"], edge=edge)))
        out["broken-edge"] += [s for _, s in sorted(broken, key=lambda b: b[0])]
        last, here = {}, None
        for s in steps:
            nid = s["node"]
            if nid != here and nid in last and not (edges.get(s["edge"]) or {}).get("when"):
                out["loop"].append(_signal("loop", f"{jid} returns to {nid} at step {s['step']} after leaving it at "
                                           f"step {last[nid] + 1}", journey=jid, step=s["step"], edge=s["edge"],
                                           node=nid))
            last[nid], here = s["step"], nid
        for value in j.get("keeps") or ():
            chosen = None
            for s in steps:
                present = (s.get("keeps") or {}).get(value)
                if present is True and chosen is None:
                    chosen = s["step"]
                elif present is False and chosen is not None:
                    out["lost-context"].append(_signal("lost-context", f"the {value} chosen in step {chosen} is "
                                                       f"absent at step {s['step']}", journey=jid, step=s["step"],
                                                       node=s["node"]))
                    break
    return out


def _reach_checks(m, sc) -> dict:
    """S2 on the scope's nodes: orphans, dead ends, depth, and what the latest crawl no longer found."""
    nodes = {n["id"]: n for n in m["nodes"]}
    out = {c: [] for c in ("orphan", "dead-end", "depth", "missing-node")}
    start = sc["start"]
    dist = _distances(m, sc["nodes"], start) if start else {}
    origin = "the start" if not sc["entry"] else f"{start}, the section's entry"
    on_journey = {nid for j in sc["journeys"] for nid in _journey_nodes(j)}
    for nid in sc["nodes"]:
        n = nodes[nid]
        if start and nid not in dist and n["kind"] != "external":
            out["orphan"].append(_signal("orphan", f"{nid} has no link from {origin}", node=nid))
        ways = [e for e in m["edges"] if e["from"] == nid and e["to"] != nid and e["kind"] != "back"
                and not e.get("missing_since") and not (nodes.get(e["to"]) or {}).get("missing_since")]
        if not ways and not n.get("final") and n["kind"] != "external" and not n.get("skipped"):
            out["dead-end"].append(_signal("dead-end", f"{nid} has no way on but the browser's back button",
                                           node=nid))
        # a deck's pages are read in order, so their distance from the first page is not a problem to report
        if (nid in on_journey or n.get("frequent")) and dist.get(nid, 0) > DEPTH and n["kind"] not in ("page", "slide"):
            out["depth"].append(_signal("depth", f"{nid} is {dist[nid]} edges from {origin}", str(DEPTH), node=nid))
    for nid in sc["all"]:
        if nodes[nid].get("missing_since"):
            out["missing-node"].append(_signal("missing-node", f"{nid} is missing since v{nodes[nid]['missing_since']}: "
                                               "the latest crawl did not find it", node=nid))
    inside = set(sc["all"])
    for e in m["edges"]:
        if e.get("missing_since") and e["from"] in inside and e["to"] in inside:
            out["missing-node"].append(_signal("missing-node", f"{e['id']} is missing since v{e['missing_since']}: "
                                               "the latest crawl did not find it", edge=e["id"]))
    return out


def _edge_checks(m, sc) -> dict:
    """S1 on crawled edges (the status and redirects the crawl saw)."""
    out = {"broken-edge": [], "redirect-chain": []}
    for e in sc["edges"]:
        if isinstance(e.get("status"), int) and e["status"] >= 400:
            out["broken-edge"].append(_signal("broken-edge", f"{e['id']} leads to a page that answers {e['status']}",
                                              edge=e["id"]))
        if isinstance(e.get("redirects"), int) and e["redirects"] > 1:
            out["redirect-chain"].append(_signal("redirect-chain", f"{e['id']} passes {e['redirects']} redirects", "1",
                                                 edge=e["id"]))
    return out


def _label_checks(m, sc, headings) -> list:
    """S3: a link or button whose name shares no word with the title or the captured heading of where it leads. A
    target whose route has a parameter (/customers/:id) is titled by its type while the link names one record
    (Northwind), so it is compared only with the headings captured on it. A redacted name, and a target the crawl
    did not open (an avoided link, a page that answered 404), are not compared."""
    nodes = {n["id"]: n for n in m["nodes"]}
    out = []
    for e in sc["edges"]:
        name = (e.get("trigger") or {}).get("name")
        if not name or e["from"] == e["to"] or e["kind"] in ("back", "redirect", "external"):
            continue
        target = nodes[e["to"]]
        if name.startswith("[redacted") or target.get("skipped"):
            continue  # a redacted name says nothing; a node the crawl did not open has no heading of its own
        seen = headings.get(e["to"], [])
        if not seen and any(p.startswith(":") for p in re.split(r"[/#]", target.get("route") or "")):
            continue
        if _words(name) & set().union(_words(target.get("title")), *(_words(h) for h in seen)):
            continue
        role = (e.get("trigger") or {}).get("role")
        heading = f' (heading "{seen[0]}")' if seen and seen[0] != target.get("title") else ""
        out.append(_signal("label-mismatch", f'{role + " " if role else ""}"{name}" leads to '
                                             f'"{target.get("title")}"{heading}', edge=e["id"]))
    return out


def _concept_checks(m, sc) -> list:
    """S5: one concept shown with more than one label across the scope's screens."""
    order = {nid: i for i, nid in enumerate(sc["nodes"])}
    out = []
    for c in m["concepts"]:
        labels = sorted(((nid, text) for nid, text in (c.get("labels") or {}).items() if nid in order),
                        key=lambda x: order[x[0]])
        shown = {}
        for nid, text in labels:
            shown.setdefault(" ".join(str(text).split()).casefold(), []).append((nid, text))
        if len(shown) < 2:
            continue
        main = max(shown, key=lambda k: (len(shown[k]), -order[shown[k][0][0]]))
        odd = next(nid for nid, text in labels if " ".join(str(text).split()).casefold() != main)
        said = _listing([f'"{group[0][1]}" on {", ".join(nid for nid, _ in group)}' for group in shown.values()])
        out.append(_signal("concept-naming", f"{c['name']} is {said}", node=odd))
    return out


def _component_checks(m, sc, captures, descriptions) -> list:
    """S5: one map component whose font size, weight, text color, background, or height differs between screens,
    from the styles the captures' descriptions hold."""
    order = {nid: i for i, nid in enumerate(sc["nodes"])}
    out = []
    for comp in m["components"]:
        looks = {}
        for c in captures:
            node = (c.get("map") or {}).get("node")
            if node not in order or node in looks:
                continue
            el = next((el for el in ((descriptions or {}).get(c.get("id")) or {}).get("elements") or []
                       if isinstance(el, dict) and el.get("component") == comp["id"]), None)
            if el:
                style, box = el.get("style") or {}, el.get("box") or [None] * 4
                looks[node] = {"font_size": style.get("font_size"), "font_weight": style.get("font_weight"),
                               "color": str(style.get("color") or "").lower() or None,
                               "background": str(style.get("background") or "").lower() or None,
                               "height": box[3] if len(box) == 4 else None}
        if len(looks) < 2:
            continue
        nodes = sorted(looks, key=order.get)
        usual = {}
        for key, _, _ in _LOOKS:
            values = [looks[n][key] for n in nodes]
            usual[key] = max(values, key=lambda v: (values.count(v), -values.index(v)))
        for nid in nodes:
            odd = [(key, label, unit) for key, label, unit in _LOOKS if looks[nid][key] != usual[key]]
            if not odd:
                continue
            same = [n for n in nodes if all(looks[n][k] == usual[k] for k, _, _ in odd)]
            here = ", ".join(f"{label} {looks[nid][k]}{unit}" for k, label, unit in odd)
            there = ", ".join(f"{usual[k]}{unit}" for k, _, unit in odd)
            out.append(_signal("component-drift", f"{comp['id']} on {nid}: {here}; on {', '.join(same)}: {there}",
                               node=nid))
    return out


def _state_checks(m, sc, captures) -> list:
    """S6: each state of a node's state machine with no capture in the audit."""
    seen = {((c.get("map") or {}).get("node"), (c.get("map") or {}).get("state")) for c in captures}
    nodes = {n["id"]: n for n in m["nodes"]}
    out = []
    for nid in sc["nodes"]:
        for state in (nodes[nid].get("states") or {}).get("states") or ():
            if (nid, state) not in seen:
                out.append(_signal("missing-state", f"{nid} has no capture in the state {state}", node=nid,
                                   state=state))
    return out


def check(m: dict, *, audit: dict = None, walks=(), captures=(), descriptions: dict = None) -> list:
    """`dev visual map check`: the structure signals of the map, in pass order (S1–S6), numbered on the audit.

    `audit`: the audit record; its `scope` picks the passes (journey: S1, S3, S4, S6 on its journeys' nodes;
    section: S1–S6 within `within`, orphans and depth from the section's entry; product, the default: S1–S6 on the
    whole map; screen and component: none), and its `signals` keep their numbers: a signal found again keeps its `n`
    and its triage, a new one takes the next number. `walks`: walk records (above); `captures`: the audit's capture
    records; `descriptions`: {capture id: description}, for the headings and component styles they show."""
    audit = audit or {}
    sc = _scope(m, audit)
    if not sc["passes"]:
        return []
    captures = [c for c in captures or () if isinstance(c, dict)]
    by_journey = {w["journey"]: w for w in walks or () if isinstance(w, dict) and w.get("journey")}
    found = _journey_checks(m, sc, by_journey)
    for key, items in _edge_checks(m, sc).items():
        found[key] += items
    found.update(_reach_checks(m, sc))
    found["label-mismatch"] = _label_checks(m, sc, _headings(captures, descriptions))
    found["concept-naming"] = _concept_checks(m, sc)
    found["component-drift"] = _component_checks(m, sc, captures, descriptions)
    found["missing-state"] = _state_checks(m, sc, captures)
    signals = [s for name, (pass_, _) in _CHECKS.items() if pass_ in sc["passes"] for s in found.get(name, ())]
    earlier = {signal_key(s): s for s in audit.get("signals") or () if isinstance(s, dict)}
    n = max([s["n"] for s in earlier.values() if isinstance(s.get("n"), int)] + [0])
    for s in signals:
        old = earlier.get(signal_key(s))
        if old and isinstance(old.get("n"), int):
            s["n"], s["triage"] = old["n"], old.get("triage")
        else:
            n += 1
            s["n"] = n
    return signals


def merge(stored: list, found: list) -> list:
    """The audit's structure signals after a check: those found again (with their number and triage) and the new
    ones from `found`, and every earlier signal no longer found, unchanged, so no triage is lost. Ordered by
    number."""
    keys = {signal_key(s) for s in found}
    kept = [s for s in stored or () if isinstance(s, dict) and signal_key(s) not in keys]
    return sorted([*found, *kept], key=lambda s: s.get("n") or 0)
