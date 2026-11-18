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
When node or gitleaks is missing, nothing is written. Routes become route patterns through redact.cjs's routePattern
(numbers, UUIDs, and long hex values are `:id`, token-shaped segments `:token`; query, user info, and any fragment but
a hash route are dropped).

When the map changes, the captures that match exactly one node by route pattern or page attach to it, and their
findings follow (R60); `attach` also lists the captures that match more than one node and attaches one by hand.
Capture and finding records are the store's (visual/<owner>/<repo>/captures/VC-….json, findings/VF-….json); this
module sets only their map fields and adds to their history.

The `dev visual map` commands call these functions; `by` names who acts (the crew member or the user) and defaults to
the configured GitHub user, then $USER.
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
REDACT_TIMEOUT = 120  # seconds for one batch: node's start, gitleaks over every string of the map
NODE_KINDS = ("screen", "overlay", "page", "slide", "external")
EDGE_KINDS = ("nav", "action", "submit", "redirect", "back", "next", "external")
ITEM_ID = re.compile(r"[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9_-]+)*")  # nodes and components: invoices.list
JOURNEY_ID = re.compile(r"J:[a-z0-9][a-z0-9-]{0,59}")
NAME = re.compile(r"\w[\w ,.'-]{0,59}")  # states and concepts: "loading", "export failed", "Invoice"
PROJECT_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
CAPTURE_ID = re.compile(r"VC-[0-9a-z]{1,40}")
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
    "node": {"id", "title", "route", "page", "kind", "final", "section", "states"},
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


def _read(path: Path):
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    try:
        doc = json.loads(text)
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


def _run_node(args: list, payload) -> str:
    try:
        p = subprocess.run([_node(), *args], input=json.dumps(payload), capture_output=True, text=True,
                           timeout=REDACT_TIMEOUT, cwd=str(REDACT.parent))
    except subprocess.TimeoutExpired:
        raise MapError(f"The map's text was not checked for secrets within {REDACT_TIMEOUT} s. Nothing was written.")
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
    for part in re.findall(r"[^/#]+", route or ""):
        if not part.startswith(":"):
            return part
    return None


def _put_node(m, version, nid, title, route=None, page=None, kind=None, final=False, section=None,
              origin="declared", example=None) -> str:
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


def add_node(project, node_id, title, *, route=None, page=None, kind=None, final=False, section=None, by=None,
             why=None) -> dict:
    """`dev visual map node add`: a declared node, or new fields for one the map has. `route`: a URL or path, stored
    as its route pattern; `page`: a document page or slide number."""
    _where(_node_id(node_id), route, page)
    pattern = _route(route)
    return change(project, lambda m, v: _put_node(m, v, node_id, title, route=pattern, page=page, kind=kind,
                                                  final=final, section=section), why, by)


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
                      final=n.get("final", False), section=n.get("section"))
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
