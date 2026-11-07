# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev ext`: the third-party plugins, skills and MCP servers devkit installs for Claude Code and Codex (spec
P-fym583l7sg1).

  catalog()   every extension devkit manages, after layering: team ∪ user.add − user.remove − retired
  observe()   what is installed, read from the agents' own files (no process, no network: hooks may call it)
  status()    one row per extension for `dev ext ls` and `dev doctor`, with its drift
  plan()      the commands that bring this machine to the catalog (retire, for now)
  apply()     a thin executor for plan(): runs each command through the injected runner and audits it

The team layer is config/claude/plugins.json, config/codex/plugins.json and config/skills.json; `meta` there says why
each entry is here, its license and its upstream, and `retired` lists what `dev install` removes. Ids are
`plugin:<agent>:<name@marketplace>`, `skill:<agent>:<name>` and `mcp:<agent>:<name>`.

Pins: `sha` (a 40-hex commit), `version` (an exact npm or PyPI version), `observed` (a vendor marketplace plugin:
devkit records what got installed), `url` (a remote MCP server: nothing local to pin) and `local` (devkit's own).
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from . import core

KINDS = ("plugin", "skill", "mcp")
AGENTS = ("claude", "codex")
LOCAL_MARKETPLACES = frozenset({"firefly-devkit"})
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
NPM_EXACT = re.compile(r"^(@?[A-Za-z0-9][\w.\-]*(?:/[\w.\-]+)?)@(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?)$")
PYPI_EXACT = re.compile(r"^([A-Za-z0-9][\w.\-]*(?:\[[\w,\-]+\])?)==(\d+(?:\.\d+)+(?:(?:a|b|rc|\.post|\.dev)\d+)?)$")
STATE_WORD = {"plugin": "installed", "skill": "linked", "mcp": "added"}
UNPINNED = frozenset({"observed", "url"})


class ExtError(ValueError):
    """An extension entry that breaks a rule; the message names the entry and the rule."""


@dataclass(frozen=True)
class Ext:
    id: str
    kind: str
    agent: str
    name: str
    source: str
    pin: str
    pin_kind: str
    license: str
    why: str
    layer: str
    required: bool
    enabled: bool
    group: str = ""
    upstream: str = ""
    spec: dict = field(default_factory=dict, compare=False, hash=False)  # what installs it: mcp spec, skill source


@dataclass(frozen=True)
class Homes:
    """Where the agents keep what they installed. Tests pass temporary ones."""
    claude: Path
    codex: Path
    agents_skills: Path
    skills_share: Path
    claude_json: Path


def homes() -> Homes:
    return Homes(claude=core.CLAUDE_HOME, codex=core.CODEX_HOME, agents_skills=core.AGENTS_HOME / "skills",
                 skills_share=core.HOME / ".local" / "share" / "devkit" / "skills",
                 claude_json=core.HOME / ".claude.json")


def lock_file() -> Path:
    return core.STATE / "ext-lock.json"


# --------------------------------------------------------------------------- the catalog

def team() -> dict:
    """The team layer, from the devkit copy that runs (the pinned install)."""
    from .setup import load_conf
    return {"claude": load_conf("claude/plugins.json"), "codex": load_conf("codex/plugins.json"),
            "skills": load_conf("skills.json")}


def user_layer(cfg: dict = None) -> dict:
    """The developer's overlay (`ext` in devkit's configuration), with every list present."""
    raw = (core.CFG if cfg is None else cfg).get("ext") or {}
    raw = raw if isinstance(raw, dict) else {}
    return {k: list(raw.get(k) or []) for k in ("add", "remove", "disable", "enable")}


def skill_groups(cfg: dict = None) -> set:
    return set(((core.CFG if cfg is None else cfg).get("skills") or {}).get("groups") or [])


def plugin_id(agent: str, pid: str) -> str:
    return f"plugin:{agent}:{pid}"


def _team_ref(agent: str, ref: str) -> str:
    """A team `retired` or `meta` key (`name@marketplace` or `mcp:<name>`) as a catalog id."""
    return f"mcp:{agent}:{ref[4:]}" if ref.startswith("mcp:") else plugin_id(agent, ref)


def retired_ids(t: dict) -> set:
    return {_team_ref(agent, r) for agent in AGENTS for r in ((t.get(agent) or {}).get("retired") or [])}


def mcp_pin(eid: str, spec: dict) -> tuple:
    """(pin kind, pin, source, upstream) of an MCP server; ExtError unless it has an exact version or an https URL."""
    if not isinstance(spec, dict):
        raise ExtError(f"{eid}: an MCP server is an object with command and args, or a url")
    url = spec.get("url")
    if url:
        u = urlparse(str(url))
        if u.scheme != "https" or not u.hostname:
            raise ExtError(f"{eid}: a remote MCP server needs an https URL, not {url}")
        return "url", str(url), u.hostname, ""
    for a in spec.get("args") or []:
        m = NPM_EXACT.match(str(a))
        if m:
            return "version", m.group(2), f"npm {m.group(1)}", f"npm:{m.group(1)}"
        m = PYPI_EXACT.match(str(a))
        if m:
            return "version", m.group(2), f"pypi {m.group(1)}", f"pypi:{m.group(1)}"
    raise ExtError(f"{eid}: an MCP server needs an exact version in its args (pkg@1.2.3 or pkg==1.2.3) or an https "
                   f"url; got {' '.join(map(str, [spec.get('command', '')] + list(spec.get('args') or [])))}")


def _plugin(agent, pid, meta, layer, enabled) -> Ext:
    name, _, market = pid.partition("@")
    local = market in LOCAL_MARKETPLACES
    return Ext(id=plugin_id(agent, pid), kind="plugin", agent=agent, name=name, source=market,
               pin="" if not local else core.VERSION, pin_kind="local" if local else "observed",
               license=str(meta.get("license") or ""), why=str(meta.get("why") or ""), layer=layer,
               required=bool(meta.get("required")), enabled=enabled, upstream=str(meta.get("upstream") or ""),
               spec={"id": pid})


def _mcp(agent, name, spec, meta, layer) -> Ext:
    eid = f"mcp:{agent}:{name}"
    kind_, pin, source, upstream = mcp_pin(eid, spec)
    return Ext(id=eid, kind="mcp", agent=agent, name=name, source=source, pin=pin, pin_kind=kind_,
               license=str(meta.get("license") or ""), why=str(meta.get("why") or ""), layer=layer,
               required=bool(meta.get("required")), enabled=True, upstream=str(meta.get("upstream") or upstream),
               spec={k: v for k, v in spec.items() if k in ("command", "args", "url", "env")})


def _skill(agent, name, src, rel, layer) -> Ext:
    return Ext(id=f"skill:{agent}:{name}", kind="skill", agent=agent, name=name, source=src["repo"],
               pin=src["ref"], pin_kind="sha", license=str(src.get("license") or ""),
               why=str(src.get("why") or src.get("_why") or ""), layer=layer, required=bool(src.get("required")),
               enabled=True, group=str(src.get("group") or ""), upstream=src["repo"],
               spec={"repo": src["repo"], "ref": src["ref"], "path": rel})


def _user_items(item) -> list:
    """The Ext entries of one overlay `add` item; ExtError for a floating ref, an unpinned MCP server, an http URL, a
    missing license or why."""
    if not isinstance(item, dict):
        raise ExtError(f"ext.add: {item!r} is not an object")
    kind = item.get("kind")
    label = f"ext.add {kind or '?'} {item.get('name') or item.get('id') or '?'}"
    if kind not in KINDS:
        raise ExtError(f"{label}: kind must be one of {', '.join(KINDS)}")
    for key in ("license", "why"):
        if not str(item.get(key) or "").strip():
            raise ExtError(f"{label}: needs a {key}")
    meta = {"license": item["license"], "why": item["why"]}
    if kind == "plugin":
        agent, pid = item.get("agent"), str(item.get("id") or "")
        if agent not in AGENTS or not re.match(r"^[\w.\-]+@[\w.\-]+$", pid):
            raise ExtError(f"{label}: a plugin needs agent (claude or codex) and id name@marketplace")
        return [_plugin(agent, pid, meta, "user", True)]
    name = str(item.get("name") or "")
    if not re.match(r"^[\w.\-]+$", name):
        raise ExtError(f"{label}: needs a name of letters, digits, dot, dash or underscore")
    agents = item.get("agents") or list(AGENTS)
    if not set(agents) <= set(AGENTS):
        raise ExtError(f"{label}: agents must be claude and/or codex")
    if kind == "mcp":
        spec = {k: item[k] for k in ("command", "args", "url", "env") if k in item}
        return [_mcp(a, name, spec, meta, "user") for a in agents]
    repo, ref, path = str(item.get("repo") or ""), str(item.get("ref") or ""), str(item.get("path") or "")
    if not re.match(r"^[\w.\-]+/[\w.\-]+$", repo):
        raise ExtError(f"{label}: a skill needs repo owner/name")
    if not SHA_RE.match(ref):
        raise ExtError(f"{label}: a skill needs ref, a full 40-character commit SHA (not a tag, branch or short SHA)")
    if not path or path.startswith("/") or ".." in Path(path).parts:
        raise ExtError(f"{label}: a skill needs path, relative to the repository")
    src = {"repo": repo, "ref": ref, "license": item["license"], "why": item["why"]}
    return [_skill(a, name, src, path, "user") for a in agents]


def catalog(t: dict, user: dict = None, groups=None) -> list:
    """Every extension devkit manages, after layering. `groups`: the skill groups this Mac installs ({"all"} for
    every group; None for every group too). Raises ExtError on an invalid entry."""
    user = {k: list((user or {}).get(k) or []) for k in ("add", "remove", "disable", "enable")}
    out = []
    for agent in AGENTS:
        conf = t.get(agent) or {}
        meta = conf.get("meta") or {}
        off = list(conf.get("disabled") or [])
        for pid in list(conf.get("plugins") or []) + [p for p in off if p not in (conf.get("plugins") or [])]:
            out.append(_plugin(agent, pid, meta.get(pid) or {}, "team", pid not in off))
        for name, spec in (conf.get("mcp") or {}).items():
            out.append(_mcp(agent, name, spec, meta.get(f"mcp:{name}") or {}, "team"))
    for src in (t.get("skills") or {}).get("sources") or []:
        g = src.get("group")
        if g and groups is not None and g not in groups and "all" not in groups:
            continue
        if not SHA_RE.match(str(src.get("ref") or "")):
            raise ExtError(f"skills.json {src.get('repo')}: ref must be a full 40-character commit SHA")
        for name, rel in (src.get("skills") or {}).items():
            for agent in src.get("agents") or AGENTS:
                out.append(_skill(agent, name, src, rel, "team"))
    retired = retired_ids(t)
    out = [e for e in out if e.id not in retired]
    added = [e for item in user["add"] for e in _user_items(item)]
    ids = {e.id for e in added}
    out = [e for e in out if e.id not in ids] + added  # a user entry replaces a team one with the same id
    required = {e.id for e in out if e.required}
    removed = set(user["remove"]) - required
    off, on = set(user["disable"]) - required, set(user["enable"])
    result = []
    for e in out:
        if e.id in removed:
            continue
        if e.id in off or e.id in on:
            e = Ext(**{**asdict(e), "enabled": e.id not in off})
        result.append(e)
    return result


# --------------------------------------------------------------------------- what is installed

def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _version_key(v: str):
    return tuple(int(x) if x.isdigit() else -1 for x in re.split(r"[.\-+]", v))


def _toml_sections(text: str) -> dict:
    """The top-level `[a.b]` sections of a TOML file and their `key = value` lines (values as raw text). Enough for
    the plugin and MCP tables Codex writes; not a TOML parser."""
    out, cur = {}, None
    for ln in text.splitlines():
        s = ln.strip()
        m = re.match(r"^\[([^\[\]]+)\]\s*(?:#.*)?$", s)
        if m:
            cur = m.group(1).strip()
            out.setdefault(cur, {})
            continue
        m = re.match(r"^([A-Za-z0-9_\-\"]+)\s*=\s*(.+)$", s)
        if m and cur is not None:
            out[cur][m.group(1).strip('"')] = m.group(2).strip()
    return out


def _args_version(args) -> str:
    for a in args or []:
        m = NPM_EXACT.match(str(a)) or PYPI_EXACT.match(str(a))
        if m:
            return m.group(2)
    return ""


def observe(h: Homes = None) -> dict:
    """What is installed now, by catalog id: {"installed", "version", "sha", "enabled", "ref"}. Reads only the agents'
    files, so it is fast and safe in a hook."""
    h = h or homes()
    out = {}
    inst = (_read_json(h.claude / "plugins" / "installed_plugins.json") or {}).get("plugins") or {}
    enabled = (_read_json(h.claude / "settings.json") or {}).get("enabledPlugins") or {}
    for pid, entries in inst.items():
        rows = [e for e in (entries if isinstance(entries, list) else []) if isinstance(e, dict)]
        e = next((x for x in rows if x.get("scope") == "user"), rows[0] if rows else None)
        if e:
            out[plugin_id("claude", pid)] = {"installed": True, "version": str(e.get("version") or ""),
                                             "sha": str(e.get("gitCommitSha") or ""),
                                             "enabled": enabled.get(pid) if pid in enabled else None}
    for name, spec in ((_read_json(h.claude_json) or {}).get("mcpServers") or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        out[f"mcp:claude:{name}"] = {"installed": True, "version": _args_version(spec.get("args")),
                                     "url": spec.get("url") or ""}
    try:
        toml = _toml_sections((h.codex / "config.toml").read_text())
    except OSError:
        toml = {}
    cache = h.codex / "plugins" / "cache"
    if cache.is_dir():
        for market in sorted(p for p in cache.iterdir() if p.is_dir()):
            for plug in sorted(p for p in market.iterdir() if p.is_dir()):
                versions = sorted((v.name for v in plug.iterdir() if v.is_dir()), key=_version_key)
                if not versions:
                    continue
                pid = f"{plug.name}@{market.name}"
                flag = (toml.get(f'plugins."{pid}"') or {}).get("enabled")
                out[plugin_id("codex", pid)] = {"installed": True, "version": versions[-1], "sha": "",
                                                "enabled": None if flag is None else flag == "true"}
    for sec, kv in toml.items():
        if sec.startswith("mcp_servers.") and sec.count(".") == 1:
            try:
                args = json.loads(kv.get("args", "[]"))
            except ValueError:
                args = []
            out[f"mcp:codex:{sec.split('.', 1)[1]}"] = {"installed": True, "version": _args_version(args),
                                                       "url": kv.get("url", "").strip('"')}
    if h.skills_share.is_dir():
        for d in sorted(p for p in h.skills_share.iterdir() if p.is_dir()):
            try:
                first = (d / ".devkit-source").read_text().split()
            except OSError:
                continue
            repo, _, ref = first[0].rpartition("@") if first else ("", "", "")
            for agent, home in (("claude", h.claude / "skills"), ("codex", h.agents_skills)):
                linked = (home / d.name / "SKILL.md").exists()
                out[f"skill:{agent}:{d.name}"] = {"installed": linked, "ref": ref, "repo": repo}
    return out


def read_lock(path: Path = None) -> dict:
    doc = _read_json(path or lock_file())
    return doc if isinstance(doc, dict) else {}


# --------------------------------------------------------------------------- status rows

def _row(e: Ext, layer: str = None) -> dict:
    r = asdict(e)
    r.pop("spec", None)
    r["layer"] = layer or e.layer
    return r


def status(cat: list, observed: dict, lock: dict = None, project: dict = None, retired=()) -> list:
    """One row per catalog entry, plus retired entries still installed and project-enabled plugins outside the
    catalog. Each row has `state`, `enabled` (after the project layer), `version`, `sha` and `drift` (kinds:
    missing, pin, changed, enabled, required-off, retired, not-in-catalog)."""
    lock, project = lock or {}, project or {}
    rows = []
    for e in cat:
        o = observed.get(e.id) or {}
        r = _row(e)
        installed = bool(o.get("installed"))
        r["state"] = STATE_WORD[e.kind] if installed else "missing"
        r["version"], r["sha"] = str(o.get("version") or ""), str(o.get("sha") or "")
        lk = lock.get(e.id) or {}
        r["locked"] = {"version": lk.get("version", ""), "sha": lk.get("sha", "")} if lk else None
        drift = []
        if not installed and e.enabled:
            drift.append({"kind": "missing", "detail": f"not {STATE_WORD[e.kind]}"})
        if installed and e.kind == "skill" and o.get("ref") and o["ref"] != e.pin:
            drift.append({"kind": "pin", "detail": f"{o['ref'][:8]} installed, {e.pin[:8]} pinned"})
        if installed and e.kind == "mcp" and e.pin_kind == "version" and o.get("version") and o["version"] != e.pin:
            drift.append({"kind": "pin", "detail": f"{o['version']} installed, {e.pin} pinned"})
        if installed and e.pin_kind == "observed" and lk and \
                (lk.get("version"), lk.get("sha") or r["sha"]) != (r["version"], r["sha"]):
            drift.append({"kind": "changed", "detail": f"{_vs(lk)} → {_vs(r)} outside devkit",
                          "from": _vs(lk), "to": _vs(r)})
        if installed and e.kind == "plugin" and o.get("enabled") is not None and bool(o["enabled"]) != e.enabled:
            drift.append({"kind": "enabled", "detail": f"{'on' if o['enabled'] else 'off'} in the agent's settings, "
                                                       f"{'on' if e.enabled else 'off'} in devkit's"})
        on = e.enabled
        if e.agent == "claude" and e.kind == "plugin" and e.spec.get("id") in project:
            on = bool(project[e.spec["id"]]) or e.required
            r["layer_on"] = "project"
        if e.required and (not on or o.get("enabled") is False):
            drift.append({"kind": "required-off", "detail": "required, but off"})
        r["enabled"] = on
        r["drift"] = drift
        rows.append(r)
    have = {e.id for e in cat}
    for rid in sorted(set(retired) - have):
        o = observed.get(rid) or {}
        if o.get("installed"):
            kind, agent, name = rid.split(":", 2)
            rows.append({"id": rid, "kind": kind, "agent": agent, "name": name.partition("@")[0],
                         "source": name.partition("@")[2], "pin": "", "pin_kind": "", "license": "", "why":
                         "retired from devkit's defaults", "layer": "retired", "required": False, "enabled": False,
                         "group": "", "upstream": "", "state": STATE_WORD.get(kind, "installed"),
                         "version": str(o.get("version") or ""), "sha": str(o.get("sha") or ""), "locked": None,
                         "drift": [{"kind": "retired", "detail": "still installed: dev install removes it"}]})
    for pid, on in sorted(project.items()):
        rid = plugin_id("claude", pid)
        if on and rid not in have:
            rows.append({"id": rid, "kind": "plugin", "agent": "claude", "name": pid.partition("@")[0],
                         "source": pid.partition("@")[2], "pin": "", "pin_kind": "", "license": "", "why":
                         "enabled by this project's .claude/settings.json", "layer": "project", "required": False,
                         "enabled": True, "group": "", "upstream": "", "state": "project", "version": "", "sha": "",
                         "locked": None, "drift": [{"kind": "not-in-catalog",
                                                    "detail": "a project setting enables a plugin devkit does not "
                                                              "manage"}]})
    return rows


def _vs(d: dict) -> str:
    v, s = str(d.get("version") or ""), str(d.get("sha") or "")[:7]
    return " ".join(x for x in (v, s) if x) or "?"


def pin_text(r: dict) -> str:
    """The PIN column: the pin kind and what it fixes; ⚠ for what devkit cannot pin."""
    k = r.get("pin_kind")
    if k == "sha":
        return f"sha {str(r.get('pin'))[:8]}"
    if k == "version":
        return f"version {r.get('pin')}"
    if k == "url":
        return f"⚠ url {urlparse(str(r.get('pin'))).hostname or r.get('pin')}"
    if k == "observed":
        lk = r.get("locked") or {}
        seen = _vs(lk) if lk else _vs(r)
        return f"⚠ observed {seen}" if seen != "?" else "⚠ observed (not installed)"
    if k == "local":
        return f"local {r.get('pin')}"
    return "-"


def project_plugins(cwd: str = None) -> dict:
    """`enabledPlugins` of the project's own Claude settings (the repository's .claude/settings.json)."""
    try:
        top = core.gitout(cwd or os.getcwd(), "rev-parse", "--show-toplevel")
    except OSError:  # the folder was removed
        top = ""
    if not top:
        return {}
    ep = (_read_json(Path(top) / ".claude" / "settings.json") or {}).get("enabledPlugins") or {}
    return {k: bool(v) for k, v in ep.items() if isinstance(k, str)}


# --------------------------------------------------------------------------- plan and apply

def _retire_argv(rid: str) -> list:
    kind, agent, name = rid.split(":", 2)
    if kind == "plugin":
        return ["claude", "plugin", "uninstall", name, "-s", "user"] if agent == "claude" else \
            ["codex", "plugin", "remove", name]
    if kind == "mcp":
        return ["claude", "mcp", "remove", "-s", "user", name] if agent == "claude" else \
            ["codex", "mcp", "remove", name]
    return []  # a skill: the skills step unlinks what it no longer manages


def plan(cat: list, observed: dict, retired=()) -> list:
    """(verb, id, argv) to bring this machine to the catalog: each retired entry that is still installed and that no
    overlay keeps."""
    keep = {e.id for e in cat}
    acts = []
    for rid in sorted(set(retired) - keep):
        argv = _retire_argv(rid)
        if argv and (observed.get(rid) or {}).get("installed"):
            acts.append(("retire", rid, argv))
    return acts


def apply(actions: list, run, audit=None, lock_path: Path = None) -> list:
    """Run each planned command (`run(argv, timeout)` -> (rc, out, err)), write one `ext.<verb>` audit record per
    entry and drop retired entries from the lock. -> (ok, message) per action."""
    if audit is None:
        from . import audit as _audit
        audit = _audit.record
    msgs, gone = [], []
    for verb, eid, argv in actions:
        rc, out, err = run(argv, timeout=300)
        ok = rc == 0
        detail = " ".join(argv) + ("" if ok else f": {(err or out).strip()[-200:]}")
        audit(f"ext.{verb}", eid, detail, "ok" if ok else f"exit {rc}")
        msgs.append((ok, f"{verb} {eid}" + ("" if ok else f" failed: {(err or out).strip()[-200:]}")))
        if ok and verb == "retire":
            gone.append(eid)
    if gone:
        def drop(doc):
            for i in gone:
                doc.pop(i, None)
        _edit_lock(drop, lock_path)
    return msgs


def retire(agent: str, run, h: Homes = None, t: dict = None, user: dict = None) -> list:
    """`dev install`: uninstall this agent's retired entries that are still installed and that no overlay keeps.
    -> apply()'s (ok, message) list."""
    t = team() if t is None else t
    cat = catalog(t, user_layer() if user is None else user, None)
    acts = [a for a in plan(cat, observe(h), retired_ids(t)) if a[1].split(":")[1] == agent]
    return apply(acts, run)


def retired_plugin_keys(agent: str = "claude", t: dict = None, user: dict = None) -> set:
    """The `name@marketplace` keys of this agent's retired plugins that no overlay keeps: `enabledPlugins` drops
    them."""
    t = team() if t is None else t
    try:
        keep = {e.id for e in catalog(t, user_layer() if user is None else user, None)}
    except ExtError:  # an invalid overlay keeps nothing; settings are still written (dev config repair)
        keep = set()
    return {r.split(":", 2)[2] for r in retired_ids(t) - keep if r.startswith(f"plugin:{agent}:")}


def _edit_lock(fn, path: Path = None):
    """Change the lock under devkit's state lock; `fn` edits the document in place."""
    from . import state
    try:
        state.update(path or lock_file(), fn, indent=2)
    except state.StateBusy:
        pass



# --------------------------------------------------------------------------- lock and drift (step 3)

REPORTED = ("changed", "pin", "required-off", "enabled", "retired", "not-in-catalog")  # `missing`: doctor's own checks


def drift_seen_file() -> Path:
    return core.STATE / "ext-drift.json"


def record_lock(agent: str, h: Homes = None, by: str = "dev install", t: dict = None, user: dict = None,
                path: Path = None) -> int:
    """`dev install` and `dev ext update --apply`: record what this agent's vendor marketplace plugins are now, so a
    later change outside devkit shows as drift. -> how many entries were recorded."""
    t = team() if t is None else t
    obs = observe(h)
    now = int(time.time())
    rows = {e.id: obs[e.id] for e in catalog(t, user_layer() if user is None else user, None)
            if e.agent == agent and e.pin_kind == "observed" and (obs.get(e.id) or {}).get("installed")}

    def put(doc):
        for eid, o in rows.items():
            doc[eid] = {"pin": "observed", "version": o.get("version", ""), "sha": o.get("sha", ""), "at": now,
                        "by": by}
    _edit_lock(put, path)
    return len(rows)


def report_drift(rows: list, audit=None, seen: Path = None) -> list:
    """Write one `ext.drift` audit record for each drift not reported before, and forget the drifts that ended.
    -> [(row, drift)] reported now."""
    from . import state
    if audit is None:
        from . import audit as _audit
        audit = _audit.record
    current = {f"{r['id']}|{d['kind']}|{d['detail']}": (r, d) for r in rows for d in r.get("drift") or []
               if d["kind"] in REPORTED}
    new = []

    def upd(doc):
        for k in [k for k in doc if k not in current]:
            doc.pop(k)
        for k, rd in current.items():
            if k not in doc:
                doc[k] = int(time.time())
                new.append(rd)
    try:
        state.update(seen or drift_seen_file(), upd, indent=1)
    except state.StateBusy:
        return []
    for r, d in new:
        audit("ext.drift", r["id"], f"{d['kind']}: {d['detail']}", "drift", layer=r.get("layer"),
              **({"from": d["from"], "to": d["to"]} if d.get("from") else {}))
    return new


def current_rows(cwd: str = None, h: Homes = None) -> list:
    t = team()
    return status(catalog(t, user_layer(), skill_groups()), observe(h), read_lock(), project_plugins(cwd),
                  retired_ids(t))


def drift_text(rows: list, limit: int = 4) -> str:
    items = [(r, d) for r in rows for d in r.get("drift") or [] if d["kind"] in REPORTED]
    if not items:
        return ""
    parts = [f"{r['name']} ({r['agent']} {r['kind']}) {d['detail']}" for r, d in items[:limit]]
    more = f"; and {len(items) - limit} more" if len(items) > limit else ""
    return "; ".join(parts) + more


def session_line(cwd: str = None, h: Homes = None):
    """The session start's line about extensions that changed outside devkit, or None. Files only: no process, no
    network. Writes each new drift's `ext.drift` record."""
    try:
        rows = current_rows(cwd, h)
    except ExtError as e:
        return f"devkit · the extension catalog is invalid: {e}. The developer fixes ~/.config/devkit/config.json (ext)."
    report_drift(rows)
    text = drift_text(rows)
    if not text:
        return None
    return (f"devkit · extensions changed outside devkit: {text}. `dev ext ls` shows them; the developer reviews "
            f"with `dev ext outdated <name>` and accepts with `dev install` (or `dev ext update <name> --apply`), in "
            f"a terminal.")


def status_needs(cwd: str = None) -> list:
    """`dev status`: extensions changed outside devkit, as "Needs you" items."""
    out = []
    try:
        rows = current_rows(cwd)
    except ExtError as e:
        return out + [{"kind": "devkit", "text": f"the extension catalog is invalid: {e}", "command": "dev ext ls"}]
    report_drift(rows)
    text = drift_text(rows, limit=3)
    if text:
        out.append({"kind": "devkit", "text": f"Extensions changed outside devkit: {text}.", "command": "dev ext ls"})
    return out


def doctor_checks(cwd: str = None) -> list:
    """`dev doctor`'s Extensions group: (group, name, ok, detail, fix)."""
    g = "Extensions"
    try:
        rows = current_rows(cwd)
    except ExtError as e:
        return [(g, "catalog", False, str(e), "fix ext in ~/.config/devkit/config.json (dev config edit)")]
    report_drift(rows)

    def of(kind):
        return [(r, d) for r in rows for d in r.get("drift") or [] if d["kind"] == kind]

    def names(items):
        return ", ".join(f"{r['name']} ({r['agent']}): {d['detail']}" for r, d in items[:4])
    n = len(rows)
    unpinned = sum(1 for r in rows if r.get("pin_kind") in UNPINNED)
    out = [(g, "catalog", True, f"{n} extensions, {unpinned} unpinned (observed or url)", "")]
    for kind, name, fix in (
            ("required-off", "required extensions on", "dev ext enable <name>, then dev install"),
            ("changed", "vendor plugins match the lock", "review with dev ext outdated <name>; accept with dev install "
                                                         "or dev ext update <name> --apply"),
            ("pin", "installed at their pins", "dev install --only skills,claude,codex"),
            ("enabled", "enabled as devkit says", "dev config repair"),
            ("retired", "retired extensions removed", "dev install --only claude,codex"),
            ("not-in-catalog", "project plugins in the catalog", "remove it from the project's .claude/settings.json, "
                                                                 "or dev ext add it")):
        items = of(kind)
        out.append((g, name, not items, names(items) if items else "none", fix))
    return out


# --------------------------------------------------------------------------- dev ext

def _columns(rows: list, cols: list) -> list:
    width = {c: max([len(c)] + [len(str(r.get(c.lower(), ""))) for r in rows]) for c in cols}
    lines = ["  ".join(c.ljust(width[c]) for c in cols).rstrip()]
    for r in rows:
        lines.append("  ".join(str(r.get(c.lower(), "")).ljust(width[c]) for c in cols).rstrip())
    return lines


def rows_for(agent: str = None, all_groups: bool = False, cwd: str = None) -> list:
    t = team()
    cat = catalog(t, user_layer(), None if all_groups else skill_groups())
    rows = status(cat, observe(), read_lock(), project_plugins(cwd), retired_ids(t))
    return [r for r in rows if not agent or r["agent"] == agent]


def cmd_ls(a) -> int:
    from . import ui
    try:
        rows = rows_for(a.agent, a.all)
    except ExtError as e:
        ui.err(f"the extension catalog is invalid: {e}")
        return 1
    if a.json:
        print(json.dumps(rows, indent=1, ensure_ascii=False))
        return 0
    table = [{"name": r["name"], "kind": r["kind"], "agent": r["agent"], "source": r["source"], "pin": pin_text(r),
              "state": r["state"], "on": "yes" if r["enabled"] else "no",
              "license": re.split(r" \(|;", r["license"])[0] or "-",
              "layer": r["layer"]} for r in rows]
    cols = ["NAME", "KIND"] + ([] if a.agent else ["AGENT"]) + ["SOURCE", "PIN", "STATE", "ON", "LICENSE", "LAYER"]
    lines = _columns(table, cols)
    print(lines[0])
    for r, ln in zip(rows, lines[1:]):
        print(ui.yellow(ln) if r["drift"] else ln)
        if a.verbose and r.get("why"):
            print("    " + ui.dim(r["why"]))
        for d in r["drift"]:
            print("    " + ui.yellow(f"⚠ {d['kind']}: {d['detail']}"))
    unpinned = sum(1 for r in rows if r.get("pin_kind") in UNPINNED)
    print(ui.dim(f"{len(rows)} extensions · {unpinned} unpinned (observed or url) · dev ext ls -v adds why"))
    return 0


def cmd_ext(a) -> int:
    if a.ext_cmd == "ls":
        return cmd_ls(a)
    return 2


def register(add):
    """Wire `dev ext` into the parser. `add(name, fn, help)` returns an argparse subparser."""
    p = add("ext", cmd_ext, "Third-party plugins, skills and MCP servers for Claude Code and Codex: ls")
    sp = p.add_subparsers(dest="ext_cmd", required=True)
    q = sp.add_parser("ls", help="every extension: source, pin, installed and enabled state, license, layer")
    q.add_argument("--agent", choices=AGENTS, help="only this agent's extensions")
    q.add_argument("--all", action="store_true", help="also the skill groups this Mac does not install")
    q.add_argument("--json", action="store_true", help="every field, for tools")
    q.add_argument("-v", "--verbose", action="store_true", help="add the one-line why of each entry")
