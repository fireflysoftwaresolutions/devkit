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
import shlex
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
                path: Path = None, only: set = None) -> int:
    """`dev install` and `dev ext update --apply`: record what this agent's vendor marketplace plugins are now (or
    only the ids in `only`), so a later change outside devkit shows as drift. -> how many entries were recorded."""
    t = team() if t is None else t
    obs = observe(h)
    now = int(time.time())
    rows = {e.id: obs[e.id] for e in catalog(t, user_layer() if user is None else user, None)
            if e.agent == agent and e.pin_kind == "observed" and (obs.get(e.id) or {}).get("installed")
            and (only is None or e.id in only)}

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
        audit("ext.drift", r["id"], f"{d['kind']}: {d['detail']}", "drift", layer=r.get("layer"))
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



# --------------------------------------------------------------------------- outdated and update (step 4)

class UpstreamError(RuntimeError):
    """An upstream that could not be checked; the message is the reason."""


def _sh(argv, timeout=15):
    return core.sh(argv, timeout=timeout)


class Upstream:
    """What upstream has now, through the developer's own `gh` and `npm` (15 s each). Tests inject a fake."""

    def __init__(self, run=None, timeout: int = 15, h: Homes = None):
        self.run, self.timeout, self.h = run or _sh, timeout, h

    def _json(self, argv):
        try:
            rc, out, err = self.run(argv, timeout=self.timeout)
        except Exception as e:  # a missing tool, a timeout the runner raises
            raise UpstreamError(f"{argv[0]}: {e}") from None
        if rc != 0:
            raise UpstreamError(f"{' '.join(argv[:3])}: {(err or out or f'exit {rc}').strip().splitlines()[-1][:160]}")
        try:
            return json.loads(out)
        except ValueError:
            raise UpstreamError(f"{' '.join(argv[:3])}: not JSON") from None

    def latest(self, e: Ext) -> dict:
        """{"ref", "label", "link"} of what upstream has now: the default branch's commit (a skill: a repository's
        releases often follow another part of it), the npm version, or the marketplace copy's version."""
        if e.pin_kind == "sha":
            sha = str(self._json(["gh", "api", f"repos/{e.source}/commits/HEAD"]).get("sha") or "")
            if not SHA_RE.match(sha):
                raise UpstreamError(f"gh api repos/{e.source}: no commit SHA in the answer")
            return {"ref": sha, "label": "", "link": f"https://github.com/{e.source}/compare/{e.pin[:12]}...{sha[:12]}"}
        if e.pin_kind == "version":
            reg, _, pkg = e.upstream.partition(":")
            if reg != "npm":
                raise UpstreamError(f"no upstream check for {reg or 'this registry'} packages yet")
            doc = self._json(["npm", "view", pkg, "version", "time", "repository", "--json"])
            repo = str((doc.get("repository") or {}).get("url") or "")
            m = re.search(r"github\.com[/:]([\w.\-]+/[\w.\-]+?)(?:\.git)?$", repo)
            link = f"https://github.com/{m.group(1)}/releases" if m else f"https://www.npmjs.com/package/{pkg}"
            return {"ref": str(doc.get("version") or ""), "label": "", "link": link,
                    "published": str((doc.get("time") or {}).get(str(doc.get("version")), ""))}
        if e.pin_kind == "observed" and e.agent == "claude":
            h = self.h or homes()
            f = h.claude / "plugins" / "marketplaces" / e.source / ".claude-plugin" / "marketplace.json"
            doc = _read_json(f)
            if not isinstance(doc, dict):
                raise UpstreamError(f"no copy of the {e.source} marketplace (claude plugin marketplace update)")
            p = next((x for x in doc.get("plugins") or [] if x.get("name") == e.name), None)
            if not p:
                raise UpstreamError(f"{e.name} is not in the {e.source} marketplace any more")
            src = p.get("source") if isinstance(p.get("source"), dict) else {}
            up = e.upstream or (str(src.get("url") or "").replace("https://github.com/", "").removesuffix(".git"))
            ref = str(p.get("version") or src.get("sha") or "")
            if not ref:
                raise UpstreamError(f"the {e.source} marketplace copy gives no version for {e.name}")
            return {"ref": ref, "sha": str(src.get("sha") or ""), "label": "marketplace copy",
                    "link": f"https://github.com/{up}/releases" if up else ""}
        if e.pin_kind == "observed":
            raise UpstreamError("the remote catalog reports only the installed version")
        if e.pin_kind == "url":
            raise UpstreamError(f"remote server at {e.source}: nothing local to pin")
        raise UpstreamError("devkit's own: dev update --check")

    def compare(self, e: Ext, old: str, new: str, full: bool = False) -> dict:
        """Commits, changed files (and their paths) between two refs of a sha-pinned entry; with `full`, also the
        repository's license now."""
        out = {}
        if e.pin_kind == "sha":
            c = self._json(["gh", "api", f"repos/{e.source}/compare/{old}...{new}"])
            out["commits"] = int(c.get("total_commits") or 0)
            out["paths"] = [str(f.get("filename") or "") for f in c.get("files") or []]
            out["files"] = len(out["paths"])
            out["link"] = str(c.get("html_url") or f"https://github.com/{e.source}/compare/{old[:12]}...{new[:12]}")
            if full:
                lic = self._json(["gh", "api", f"repos/{e.source}/license"])
                out["license"] = str(((lic.get("license") or {}).get("spdx_id")) or "")
        return out


def _base_license(text: str) -> str:
    return re.split(r" \(|;", str(text or ""))[0].strip()


def outdated(cat: list, upstream, name: str = None, agent: str = None, observed: dict = None) -> list:
    """One row per checked entry: {"id", "name", "kind", "agent", "current", "latest", "link", "status", "reason"};
    status is outdated, current, unchecked (with the reason) or unpinned. With `name`: also `detail` (commits, files,
    license change, host)."""
    picked = select(cat, name, agent) if name else [e for e in cat if not agent or e.agent == agent]
    observed = observe() if observed is None else observed
    rows, seen, compared = [], {}, {}
    for e in picked:
        if e.pin_kind == "local":
            continue
        o = observed.get(e.id) or {}
        cur = e.pin if e.pin_kind in ("sha", "version") else _vs(o) if e.pin_kind == "observed" else e.pin
        r = {"id": e.id, "name": e.name, "kind": e.kind, "agent": e.agent, "pin_kind": e.pin_kind,
             "current": cur, "latest": "", "link": "", "status": "", "reason": ""}
        if e.pin_kind == "url":
            r.update(status="unpinned", reason=f"remote server at {e.source}: nothing local to pin")
            rows.append(r)
            continue
        key = (e.pin_kind, e.source, e.upstream, e.name if e.pin_kind == "observed" else "", e.agent
               if e.pin_kind == "observed" else "")
        if key not in seen:
            try:
                seen[key] = upstream.latest(e)
            except UpstreamError as err:
                seen[key] = str(err)
        up = seen[key]
        if isinstance(up, str):  # the same upstream failed for an entry before: the same reason
            r.update(status="unchecked", reason=up)
            rows.append(r)
            continue
        latest = up["ref"]
        r.update(latest=latest, label=up.get("label", ""), link=up.get("link", ""))
        if e.pin_kind == "observed":
            same = latest in (o.get("version"), o.get("sha")) or (up.get("sha") and up["sha"] == o.get("sha"))
            r["status"] = "current" if same else "outdated"
        else:
            r["status"] = "current" if latest == e.pin else "outdated"
        if e.pin_kind == "sha" and r["status"] == "outdated":  # one comparison per repository and pair of refs
            ck = (e.source, e.pin, latest, bool(name))
            if ck not in compared:
                try:
                    compared[ck] = upstream.compare(e, e.pin, latest, full=bool(name))
                except UpstreamError as err:
                    compared[ck] = {"error": str(err)}
            d = dict(compared[ck])
            if "error" not in d:
                prefix = str(e.spec.get("path") or "").rstrip("/") + "/"
                d["skill_files"] = sum(1 for p in d.get("paths") or [] if p.startswith(prefix))
                if not d["skill_files"]:
                    r.update(status="current", reason=f"upstream moved {d.get('commits', 0)} commit(s); this "
                                                      f"skill's folder did not change")
            r["detail"] = d
            r["link"] = d.get("link") or r["link"]
        if name and r["status"] == "outdated":
            d = r.setdefault("detail", {})
            if d.get("license") and _base_license(d["license"]) != _base_license(e.license):
                d["license_change"] = f"{_base_license(e.license)} → {d['license']}"
        rows.append(r)
    return rows


def select(cat: list, name: str, agent: str = None, kinds=None) -> list:
    """The entries a name selects: a full id, or a short name (or name@marketplace) for every match of one kind.
    ExtError when nothing matches or the name matches two kinds."""
    if name in {e.id for e in cat}:
        return [e for e in cat if e.id == name]
    hits = [e for e in cat if (e.name == name or e.spec.get("id") == name) and (not agent or e.agent == agent)
            and (not kinds or e.kind in kinds)]
    if not hits:
        raise ExtError(f"no extension named {name}" + (f" for {agent}" if agent else "") + ": dev ext ls lists them")
    if len({e.kind for e in hits}) > 1:
        raise ExtError(f"{name} names more than one kind: " + ", ".join(sorted(e.id for e in hits)) +
                       "; use the full id")
    return hits


def bump(text: str, e: Ext, new: str) -> str:
    """The config file `text` with only `e`'s pin set to `new`, in the file's own format (indent 2, a final
    newline). An unchanged pin gives back the same bytes."""
    doc = json.loads(text)
    if e.kind == "skill":
        hits = [s for s in doc.get("sources") or [] if s.get("repo") == e.source and s.get("ref") == e.pin and
                e.name in (s.get("skills") or {})]
        if len(hits) != 1:
            raise ExtError(f"{e.id}: no single source {e.source}@{e.pin[:12]} lists it in skills.json")
        hits[0]["ref"] = new
    elif e.kind == "mcp" and e.pin_kind == "version":
        spec = (doc.get("mcp") or {}).get(e.name)
        if not spec:
            raise ExtError(f"{e.id}: not in this plugins.json")
        pkg = e.upstream.partition(":")[2]
        sep = "==" if e.upstream.startswith("pypi:") else "@"
        old = f"{pkg}{sep}{e.pin}"
        if old not in spec.get("args", []):
            raise ExtError(f"{e.id}: {old} is not in its args")
        spec["args"] = [f"{pkg}{sep}{new}" if a == old else a for a in spec["args"]]
    else:
        raise ExtError(f"{e.id}: a {e.pin_kind} entry has no team pin to bump")
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def _config_file(home: Path, e: Ext) -> Path:
    return home / "config" / ("skills.json" if e.kind == "skill" else f"{e.agent}/plugins.json")


def team_at(home: Path) -> dict:
    """The team layer as the checkout at `home` has it (raw: placeholders stay; the catalog does not need them)."""
    out = {}
    for key, rel in (("claude", "claude/plugins.json"), ("codex", "codex/plugins.json"), ("skills", "skills.json")):
        out[key] = _read_json(home / "config" / rel) or {}
    return out


def checkout_refusal(home: Path):
    """Why the team configuration in `home` may not be changed here, or None: the pinned copy, a detached HEAD, or
    the default branch."""
    if core.is_pinned(home):
        return f"{core.tilde(home)} is a pinned copy, which never changes: run this in the devkit checkout, on a branch"
    branch = core.gitout(home, "symbolic-ref", "-q", "--short", "HEAD")
    if not branch:
        return f"{core.tilde(home)} is not on a branch (detached HEAD): dev branch new chore/bump-<name> first"
    default = core.gitout(home, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD").partition("/")[2] or "main"
    if branch in (default, "main", "master"):
        return f"{branch} is the default branch: a pin bump goes through a branch and a PR (dev branch new chore/bump-<name>)"
    return None


def detail_lines(r: dict) -> list:
    """The review detail printed before any update (the Security engineer's condition 4)."""
    out = [f"{r['name']} ({r['agent']} {r['kind']}): {r['current'][:12]} → {r['latest'][:12]}"
           + (f" ({r['label']})" if r.get("label") else "")]
    d = r.get("detail") or {}
    if r.get("link"):
        out.append(f"  compare: {r['link']}")
    if "commits" in d:
        out.append(f"  {d['commits']} commit(s), {'300 or more' if d['files'] >= 300 else d['files']} file(s) changed"
                   + (f", {d['skill_files']} in this skill's folder" if "skill_files" in d else ""))
    if d.get("license_change"):
        out.append(f"  ⚠ license changed: {d['license_change']}")
    elif d.get("license"):
        out.append(f"  license: {d['license']}")
    if d.get("error"):
        out.append(f"  ⚠ the comparison could not be read: {d['error']}")
    return out


def upstream() -> Upstream:
    return Upstream()


def runner():
    """The command runner for vendor CLIs (`claude plugin`, `codex plugin`); tests inject a fake."""
    return lambda argv, timeout=300: core.sh(argv, timeout=timeout)


def merge_agents(rows: list) -> list:
    """One row for the same entry in both agents (a skill both get): agent `claude+codex`."""
    out, at = [], {}
    for r in (dict(x) for x in rows):
        key = (r["kind"], r["name"], r["current"], r["latest"], r["status"])
        if key in at and r["pin_kind"] in ("sha", "version"):
            at[key]["agent"] = "+".join(sorted(set(at[key]["agent"].split("+")) | {r["agent"]}))
            at[key].setdefault("ids", [at[key]["id"]]).append(r["id"])
            continue
        at[key] = r
        out.append(r)
    return out


def _short(ref: str) -> str:
    return ref[:8] if SHA_RE.match(ref or "") else ref


def outdated_lines(rows: list) -> list:
    rows = merge_agents(rows)
    old = [r for r in rows if r["status"] == "outdated"]
    width = max([len(r["name"]) for r in old] + [8])
    lines = [f"{r['name']:<{width}}  {r['kind']:<6} {r['agent']:<12} {_short(r['current'])}"
             + (f" ({r['label']})" if r.get("label") and r["pin_kind"] != "observed" else "")
             + f" → {_short(r['latest'])}  {r.get('link') or ''}".rstrip() for r in old]
    current = [r for r in rows if r["status"] == "current"]
    if current:
        lines.append(f"{len(current)} up to date")
    for r in rows:
        if r["status"] == "unpinned":
            lines.append(f"{r['name']} ({r['agent']} {r['kind']}) is unpinned: {r['reason']}")
    unchecked = [r for r in rows if r["status"] == "unchecked"]
    if unchecked:
        lines.append(f"{len(unchecked)} could not be checked: " +
                     "; ".join(f"{r['name']} ({r['agent']}): {r['reason']}" for r in unchecked))
    return lines


def cmd_outdated(a) -> int:
    from . import ui
    try:
        cat = catalog(team(), user_layer(), skill_groups())
        rows = outdated(cat, upstream(), a.name, a.agent)
    except ExtError as e:
        ui.err(str(e))
        return 2
    if a.json:
        print(json.dumps(rows, indent=1, ensure_ascii=False))
    else:
        for ln in outdated_lines(rows):
            print(ui.yellow(ln) if "could not be checked" in ln else ln)
        for r in rows if a.name else []:
            if r["status"] == "outdated":
                print("\n".join(detail_lines(r)))
    return 1 if any(r["status"] == "unchecked" for r in rows) else 0


def _review(e: Ext, latest: str, up, label: str = "", link: str = "") -> dict:
    r = {"id": e.id, "name": e.name, "kind": e.kind, "agent": e.agent, "pin_kind": e.pin_kind, "current": e.pin,
         "latest": latest, "label": label, "link": link, "status": "outdated"}
    try:
        d = up.compare(e, e.pin, latest, full=True)
        if e.kind == "skill" and "paths" in d:
            prefix = str(e.spec.get("path") or "").rstrip("/") + "/"
            d["skill_files"] = sum(1 for p in d["paths"] if p.startswith(prefix))
        if d.get("license") and _base_license(d["license"]) != _base_license(e.license):
            d["license_change"] = f"{_base_license(e.license)} → {d['license']}"
        r["detail"], r["link"] = d, d.get("link") or link
    except UpstreamError as err:
        r["detail"] = {"error": str(err)}
    return r


def cmd_update(a) -> int:
    """`dev ext update <name> [--to <ref>] --why "…"`: bump a team pin in the devkit checkout, after printing the
    review detail. With --apply: update a vendor marketplace plugin on this Mac (the developer's, in a terminal)."""
    from . import audit, ui
    if not (a.why or "").strip():
        ui.err('dev ext update needs --why "<reason>": it goes in the audit trail and the commit')
        return 2
    if a.apply:
        return _apply_update(a)
    home = core.source_home()
    refusal = checkout_refusal(home)
    if refusal:
        ui.err(refusal)
        return 1
    try:
        cat = catalog(team_at(home), {}, {"all"})
        pinnable = [e for e in cat if e.pin_kind in ("sha", "version")]
        hits = select(pinnable, a.name, a.agent) if any(a.name in (e.id, e.name) for e in pinnable) else \
            select(cat, a.name, a.agent)
    except ExtError as e:
        ui.err(str(e))
        return 2
    pinned = [e for e in hits if e.pin_kind in ("sha", "version")]
    if not pinned:
        ui.err(f"{a.name} is {hits[0].pin_kind}: devkit's config has no pin for it. A vendor marketplace plugin is "
               f"updated on this Mac with dev ext update {a.name} --apply --why \"…\", in a terminal")
        return 2
    if a.to and pinned[0].pin_kind == "sha" and not SHA_RE.match(a.to):
        ui.err("--to needs a full 40-character commit SHA (not a tag, branch or short SHA)")
        return 2
    if a.to and pinned[0].pin_kind == "version" and not re.match(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$", a.to):
        ui.err("--to needs an exact version such as 1.13.2")
        return 2
    up, done_, rc = upstream(), set(), 0
    for e in pinned:
        f = _config_file(home, e)
        key = (str(f), e.source, e.pin, e.name if e.kind == "mcp" else "")
        if key in done_:
            continue
        done_.add(key)
        try:
            info = {"ref": a.to} if a.to else up.latest(e)
        except UpstreamError as err:
            ui.err(f"{e.id}: the newest upstream could not be checked ({err}); name it with --to")
            rc = 1
            continue
        new = info["ref"]
        if new == e.pin:
            print(f"{e.name} ({e.agent} {e.kind}) is already at {_short(new)}")
            continue
        r = _review(e, new, up, info.get("label", ""), info.get("link", ""))
        print("\n".join(detail_lines(r)))
        f.write_text(bump(f.read_text(), e, new))
        audit.record("ext.update", e.id, f"{e.pin} → {new} in {f.relative_to(home)}", "ok", why=a.why.strip(),
                     layer="team")
        print(f"changed {f.relative_to(home)}: {e.name} {_short(e.pin)} → {_short(new)}")
    if done_ and rc == 0:
        print("next: commit it (dev commit --why …) and open a PR; the Security engineer reviews dependency changes")
    return rc


def _apply_update(a) -> int:
    from . import audit, ui
    try:
        cat = catalog(team(), user_layer(), skill_groups())
        seen = [e for e in cat if e.pin_kind == "observed"]
        hits = select(seen, a.name, a.agent) if any(a.name in (e.id, e.name, e.spec.get("id")) for e in seen) else \
            select(cat, a.name, a.agent)
    except ExtError as e:
        ui.err(str(e))
        return 2
    obs = [e for e in hits if e.pin_kind == "observed"]
    if not obs:
        ui.err(f"{a.name} is pinned in devkit's config ({hits[0].pin_kind}): bump the pin with dev ext update "
               f"{a.name} --to <ref> --why \"…\" on a branch of the devkit checkout, then dev install")
        return 2
    up, run, rc = upstream(), runner(), 0
    for row in outdated(obs, up, observed=observe()):
        print("\n".join(detail_lines(row)) if row["status"] == "outdated" else
              f"{row['name']} ({row['agent']}): {row['status']}" + (f" ({row['reason']})" if row["reason"] else ""))
    for e in obs:
        argv = ["claude", "plugin", "update", e.spec["id"]] if e.agent == "claude" else \
            ["codex", "plugin", "add", e.spec["id"]]
        (ok, msg), = apply([("apply", e.id, argv)], run,
                           audit=lambda *x, **k: audit.record(*x, why=a.why.strip(), **k))
        if not ok:
            ui.err(msg)
            rc = 1
            continue
        record_lock(e.agent, by="dev ext update --apply", only={e.id})
        now = observe().get(e.id) or {}
        print(f"updated {e.name} ({e.agent}): now {_vs(now)}, recorded as accepted"
              + (" · restart the agent to load it" if e.agent == "claude" else ""))
    return rc



# --------------------------------------------------------------------------- the developer's own settings (step 5)

def effective_claude_plugins(t: dict, user: dict = None) -> tuple:
    """(enabled, disabled) `name@marketplace` keys for Claude Code after the team and user layers (spec R8): what
    `enabledPlugins`, `dev config repair` and the managed lock write. An invalid overlay falls back to the team's."""
    try:
        cat = catalog(t, user, None)
    except ExtError:
        cat = catalog(t, {}, None)
    plugins = [e for e in cat if e.kind == "plugin" and e.agent == "claude"]
    return ({e.spec["id"] for e in plugins if e.enabled}, {e.spec["id"] for e in plugins if not e.enabled})


def effective(agent: str, kind: str, t: dict = None, user: dict = None, groups=None) -> list:
    """This agent's enabled entries of one kind after layering: what `dev install` installs."""
    t = team() if t is None else t
    try:
        cat = catalog(t, user_layer() if user is None else user, skill_groups() if groups is None else groups)
    except ExtError:
        cat = catalog(t, {}, skill_groups() if groups is None else groups)
    return [e for e in cat if e.agent == agent and e.kind == kind and e.enabled]


def overlay_skill_sources(user: dict = None) -> list:
    """The overlay's skills as config/skills.json sources, for the skills step."""
    out = []
    for item in (user_layer() if user is None else user)["add"]:
        if isinstance(item, dict) and item.get("kind") == "skill":
            _user_items(item)  # validates
            out.append({"repo": item["repo"], "ref": item["ref"], "license": item["license"], "why": item["why"],
                        "agents": list(item.get("agents") or AGENTS), "skills": {item["name"]: item["path"]}})
    return out


def entry_argv(verb: str, e: Ext) -> list:
    """The one command that applies `verb` (add, remove, enable, disable) to `e` on this machine, or [] when the
    skills step does it."""
    pid = e.spec.get("id")
    if e.kind == "plugin" and e.agent == "claude":
        return {"add": ["claude", "plugin", "install", pid, "-s", "user", "-y"],
                "enable": ["claude", "plugin", "enable", pid, "-s", "user"],
                "disable": ["claude", "plugin", "disable", pid, "-s", "user"],
                "remove": ["claude", "plugin", "uninstall", pid, "-s", "user"]}[verb]
    if e.kind == "plugin":  # Codex has no plugin switch: off means removed
        return ["codex", "plugin", "add", pid] if verb in ("add", "enable") else ["codex", "plugin", "remove", pid]
    if e.kind == "mcp":
        if verb in ("remove", "disable"):
            return ["claude", "mcp", "remove", "-s", "user", e.name] if e.agent == "claude" else \
                ["codex", "mcp", "remove", e.name]
        s = e.spec
        if e.agent == "claude":
            return ["claude", "mcp", "add", "-s", "user", "--transport", "http", e.name, s["url"]] if s.get("url") \
                else ["claude", "mcp", "add", "-s", "user", e.name, "--", s["command"], *s.get("args", [])]
        return ["codex", "mcp", "add", e.name, "--url", s["url"]] if s.get("url") else \
            ["codex", "mcp", "add", e.name, "--", s["command"], *s.get("args", [])]
    return []


def overlay_change(user: dict, verb: str, ids: list, item: dict = None) -> dict:
    """The overlay after one change: `add` an item, or `remove|enable|disable` catalog ids. Never mutates `user`."""
    u = {k: list(user.get(k) or []) for k in ("add", "remove", "disable", "enable")}
    if verb == "add":
        key = _item_key(item)
        u["add"] = [x for x in u["add"] if _item_key(x) != key] + [item]
        u["remove"] = [x for x in u["remove"] if x not in ids]
    elif verb == "remove":
        mine = [x for x in u["add"] if set(_item_ids(x)) & set(ids)]
        u["add"] = [x for x in u["add"] if x not in mine]
        u["remove"] = sorted(set(u["remove"]) | set(ids))
    elif verb in ("enable", "disable"):
        other = "disable" if verb == "enable" else "enable"
        u[other] = [x for x in u[other] if x not in ids]
        u[verb] = sorted(set(u[verb]) | set(ids))
    return u


def _item_key(item) -> tuple:
    return (item.get("kind"), item.get("agent"), item.get("id") or item.get("name"))


def _item_ids(item) -> list:
    try:
        return [e.id for e in _user_items(item)]
    except ExtError:
        return []


def effective_mcp(agent: str, t: dict = None, user: dict = None) -> dict:
    """{name: spec} of this agent's enabled MCP servers after layering: what setup adds and repair expects."""
    return {e.name: dict(e.spec) for e in effective(agent, "mcp", t, user, {"all"})}


def effective_plugin_ids(agent: str, t: dict = None, user: dict = None) -> list:
    return [e.spec["id"] for e in effective(agent, "plugin", t, user, {"all"})]


def effective_skill_sources(t: dict = None, user: dict = None) -> list:
    """config/skills.json sources after layering (groups are left to the skills step): team sources without the
    skills the overlay removed or disabled, split by agent where only one agent keeps a skill, plus the overlay's."""
    t = team() if t is None else t
    user = user_layer() if user is None else user
    try:
        cat = catalog(t, user, None)
    except ExtError:
        cat, user = catalog(t, {}, None), {}
    on = {e.id for e in cat if e.kind == "skill" and e.enabled}
    out = []
    for src in (t.get("skills") or {}).get("sources") or []:
        base = {k: v for k, v in src.items() if k not in ("skills", "agents")}
        whole, split = {}, []
        for name, rel in (src.get("skills") or {}).items():
            agents = [a for a in (src.get("agents") or AGENTS) if f"skill:{a}:{name}" in on]
            if not agents:
                continue
            if sorted(agents) == sorted(src.get("agents") or AGENTS):
                whole[name] = rel
            else:
                split.append({**base, "agents": agents, "skills": {name: rel}})
        if whole:
            out.append({**base, **({"agents": src["agents"]} if src.get("agents") else {}), "skills": whole})
        out += split
    for s in overlay_skill_sources(user):
        name = next(iter(s["skills"]))
        agents = [a for a in s["agents"] if f"skill:{a}:{name}" in on]
        if agents:
            out.append({**s, "agents": agents})
    return out


def install_skills():
    """The skills step (fetch at the pinned commit, link into both agents), for an overlay skill change."""
    from . import setup
    setup.install_skills(quiet=True)


def _item_from_args(a) -> dict:
    item = {"kind": a.kind, "license": a.license or "", "why": a.why or ""}
    if a.kind == "plugin":
        item.update(id=a.name, agent=a.agent or "")
    elif a.kind == "mcp":
        cmd = shlex.split(a.command or "")
        item["name"] = a.name
        if a.url:
            item["url"] = a.url
        elif cmd:
            item.update(command=cmd[0], args=cmd[1:])
        item["agents"] = [a.agent] if a.agent else list(AGENTS)
    else:
        item.update(name=a.name, repo=a.repo or "", ref=a.ref or "", path=a.path or "",
                    agents=[a.agent] if a.agent else list(AGENTS))
    return item


def cmd_change(a) -> int:
    """`dev ext add|remove|enable|disable`: the developer's overlay (or --team: the repo config; --project: the
    project's Claude settings), then that one entry applied on this Mac."""
    from . import audit, ui
    verb = a.ext_cmd
    if not (a.why or "").strip():
        ui.err(f'dev ext {verb} needs --why "<reason>": it goes in the audit trail')
        return 2
    if getattr(a, "team", False):
        return _team_change(a)
    if getattr(a, "project", False):
        return _project_change(a)
    t, user = team(), user_layer()
    try:
        if verb == "add":
            item = _item_from_args(a)
            ids = [e.id for e in _user_items(item)]
            new = overlay_change(user, "add", ids, item)
        else:
            hits = select(catalog(t, user, None), a.name, a.agent)
            req = [e.id for e in hits if e.required]
            if verb in ("remove", "disable") and req:
                ui.err(f"{', '.join(req)} is required (devkit itself or a security plugin): it cannot be "
                       f"{'removed' if verb == 'remove' else 'disabled'}")
                return 2
            ids = [e.id for e in hits]
            new = overlay_change(user, verb, ids)
        after = {e.id: e for e in catalog(t, new, None)}
    except ExtError as e:
        ui.err(str(e))
        return 2
    try:
        core.config_set("ext", json.dumps(new))
    except core.ConfigError as e:
        ui.err(str(e))
        return 1
    targets = [after[i] for i in ids if i in after] if verb != "remove" else hits
    say = lambda *x, **k: audit.record(*x, why=a.why.strip(), layer="user", **k)  # noqa: E731
    acts = [(verb, e.id, entry_argv(verb, e)) for e in targets if entry_argv(verb, e)]
    rc = 0
    for ok, msg in apply(acts, runner(), audit=say):
        print(msg) if ok else ui.err(msg)
        rc = rc if ok else 1
    skills = [e for e in targets if e.kind == "skill"]
    for e in skills:
        say(f"ext.{verb}", e.id, "the skills step links or unlinks it", "ok")
    if skills:
        install_skills()
        print(f"skills relinked: {', '.join(sorted({e.name for e in skills}))}")
    print(f"recorded in your overlay (ext.{verb} in ~/.config/devkit/config.json): dev install and dev config "
          f"repair keep it")
    from . import policy
    if any(e.kind == "plugin" and e.agent == "claude" for e in targets) and policy.lock_status() != "off":
        print("the managed-settings lock forces devkit's plugin set: run dev config lock again to include this")
    return rc


def _project_change(a) -> int:
    from . import audit, ui
    if a.ext_cmd not in ("enable", "disable"):
        ui.err("--project changes only enable and disable, for Claude Code plugins in devkit's catalog")
        return 2
    try:
        hits = [e for e in select(catalog(team(), user_layer(), None), a.name, "claude", kinds=("plugin",))]
    except ExtError as e:
        ui.err(f"{e} (--project takes only Claude Code plugins in devkit's catalog)")
        return 2
    if a.ext_cmd == "disable" and any(e.required for e in hits):
        ui.err(f"{hits[0].id} is required: it cannot be disabled in a project")
        return 2
    acts = [(a.ext_cmd, e.id, ["claude", "plugin", a.ext_cmd, e.spec["id"], "-s", "project"]) for e in hits]
    rc = 0
    for ok, msg in apply(acts, runner(), audit=lambda *x, **k: audit.record(*x, why=a.why.strip(), layer="project",
                                                                             **k)):
        print(msg + " (this project's .claude/settings.json)") if ok else ui.err(msg)
        rc = rc if ok else 1
    return rc


def _team_change(a) -> int:
    """--team: add or remove an entry in the devkit checkout's team configuration, on a branch."""
    from . import audit, ui
    if a.ext_cmd not in ("add", "remove"):
        ui.err("--team takes add and remove; to ship a plugin off by default, edit `disabled` in the agent's "
               "plugins.json on a branch")
        return 2
    home = core.source_home()
    refusal = checkout_refusal(home)
    if refusal:
        ui.err(refusal)
        return 1
    try:
        if a.ext_cmd == "add":
            item = _item_from_args(a)
            entries = _user_items(item)
            changed = _team_add(home, item, entries)
        else:
            hits = [e for e in select(catalog(team_at(home), {}, {"all"}), a.name, a.agent) if e.layer == "team"]
            if any(e.required for e in hits):
                ui.err(f"{hits[0].id} is required: it cannot be removed from devkit's defaults")
                return 2
            changed = _team_remove(home, hits)
        catalog(team_at(home), {}, {"all"})  # the result must still be a valid catalog
    except ExtError as e:
        ui.err(str(e))
        return 2
    for f, eid in changed:
        audit.record(f"ext.{a.ext_cmd}", eid, f"team default, {f}", "ok", why=a.why.strip(), layer="team")
        print(f"{'added to' if a.ext_cmd == 'add' else 'removed from'} {f}: {eid}")
    print("next: commit it (dev commit --why …) and open a PR; the Security engineer reviews it; dev install then "
          + ("installs it" if a.ext_cmd == "add" else "uninstalls it on every Mac (retired)"))
    return 0


def _rw(home: Path, rel: str):
    f = home / "config" / rel
    return f, json.loads(f.read_text())


def _save(f: Path, doc: dict):
    f.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def _team_add(home: Path, item: dict, entries: list) -> list:
    meta = {"why": item["why"], "license": item["license"]}
    out = []
    if item["kind"] == "skill":
        f, doc = _rw(home, "skills.json")
        src = {"repo": item["repo"], "ref": item["ref"], "license": item["license"], "why": item["why"],
               "skills": {item["name"]: item["path"]}}
        if sorted(item.get("agents") or AGENTS) != sorted(AGENTS):
            src["agents"] = list(item["agents"])
        doc["sources"].append(src)
        _save(f, doc)
        return [(str(f.relative_to(home)), e.id) for e in entries]
    for e in entries:
        f, doc = _rw(home, f"{e.agent}/plugins.json")
        if e.kind == "plugin":
            if e.spec["id"] not in doc.setdefault("plugins", []):
                doc["plugins"].append(e.spec["id"])
            doc.setdefault("meta", {})[e.spec["id"]] = meta
            doc["retired"] = [r for r in doc.get("retired") or [] if r != e.spec["id"]]
        else:
            doc.setdefault("mcp", {})[e.name] = dict(e.spec)
            doc.setdefault("meta", {})[f"mcp:{e.name}"] = meta
            doc["retired"] = [r for r in doc.get("retired") or [] if r != f"mcp:{e.name}"]
        if not doc["retired"]:
            doc.pop("retired")
        _save(f, doc)
        out.append((str(f.relative_to(home)), e.id))
    return out


def _team_remove(home: Path, hits: list) -> list:
    out = []
    for e in hits:
        if e.kind == "skill":
            f, doc = _rw(home, "skills.json")
            for src in doc["sources"]:
                if src.get("repo") == e.source and src.get("ref") == e.pin and e.name in src.get("skills", {}):
                    src["skills"].pop(e.name)
            doc["sources"] = [s for s in doc["sources"] if s.get("skills")]
        else:
            f, doc = _rw(home, f"{e.agent}/plugins.json")
            key = e.spec["id"] if e.kind == "plugin" else f"mcp:{e.name}"
            if e.kind == "plugin":
                for lst in ("plugins", "disabled"):
                    doc[lst] = [p for p in doc.get(lst) or [] if p != key]
            else:
                (doc.get("mcp") or {}).pop(e.name, None)
            (doc.get("meta") or {}).pop(key, None)
            doc["retired"] = sorted(set(doc.get("retired") or []) | {key})
        _save(f, doc)
        if (str(f.relative_to(home)), e.id) not in out:
            out.append((str(f.relative_to(home)), e.id))
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
    return {"ls": cmd_ls, "outdated": cmd_outdated, "update": cmd_update, "add": cmd_change, "remove": cmd_change,
            "enable": cmd_change, "disable": cmd_change}[a.ext_cmd](a)


def register(add):
    """Wire `dev ext` into the parser. `add(name, fn, help)` returns an argparse subparser. No abbreviated options:
    the guard reads `--apply` and the other words exactly."""
    p = add("ext", cmd_ext, "Third-party plugins, skills and MCP servers for Claude Code and Codex: ls, outdated, "
                            "update")
    sp = p.add_subparsers(dest="ext_cmd", required=True)
    q = sp.add_parser("ls", allow_abbrev=False,
                      help="every extension: source, pin, installed and enabled state, license, layer")
    q.add_argument("--agent", choices=AGENTS, help="only this agent's extensions")
    q.add_argument("--all", action="store_true", help="also the skill groups this Mac does not install")
    q.add_argument("--json", action="store_true", help="every field, for tools")
    q.add_argument("-v", "--verbose", action="store_true", help="add the one-line why of each entry")
    q = sp.add_parser("outdated", allow_abbrev=False,
                      help="the newest upstream of each entry against its pin, with the changelog or compare link")
    q.add_argument("name", nargs="?", help="one entry: also its commits, changed files and license change")
    q.add_argument("--agent", choices=AGENTS)
    q.add_argument("--json", action="store_true")
    q = sp.add_parser("update", allow_abbrev=False,
                      help="bump a team pin in the devkit checkout (on a branch), or --apply a vendor plugin update "
                           "on this Mac (the developer, in a terminal)")
    q.add_argument("name", help="a name or a full id (dev ext ls)")
    q.add_argument("--to", help="the new ref: a full commit SHA or an exact version (default: the newest upstream)")
    q.add_argument("--why", help="the reason, recorded in the audit trail; required")
    q.add_argument("--agent", choices=AGENTS)
    q.add_argument("--apply", action="store_true", help="update a vendor marketplace plugin on this Mac and accept "
                                                        "it (the developer, in a terminal)")
    q = sp.add_parser("add", allow_abbrev=False,
                      help="add your own plugin, skill or MCP server (your overlay; --team: devkit's defaults)")
    q.add_argument("kind", choices=KINDS)
    q.add_argument("name", help="a skill or MCP server name, or a plugin id name@marketplace")
    q.add_argument("--agent", choices=AGENTS, help="only this agent (a plugin needs it; default: both)")
    q.add_argument("--repo", help="skill: owner/repo on GitHub")
    q.add_argument("--ref", help="skill: the full 40-character commit SHA")
    q.add_argument("--path", help="skill: its folder in the repository")
    q.add_argument("--url", help="MCP server: an https URL (remote server)")
    q.add_argument("--license", help="its license, such as MIT (required)")
    q.add_argument("--why", help="why you add it (required, audited)")
    q.add_argument("--team", action="store_true", help="add it to devkit's defaults in the checkout, on a branch")
    q.add_argument("--command", help='MCP server: the command line, quoted, with an exact version, such as '
                                     '"npx -y pkg@1.2.3" or "uvx pkg==1.2.3"')
    for verb, text in (("remove", "remove an extension (your overlay; --team: retire it from devkit's defaults)"),
                       ("enable", "turn an extension on (your overlay; --project: this project's Claude settings)"),
                       ("disable", "turn an extension off (your overlay; --project: this project's Claude settings)")):
        q = sp.add_parser(verb, allow_abbrev=False, help=text)
        q.add_argument("name", help="a name or a full id (dev ext ls)")
        q.add_argument("--agent", choices=AGENTS)
        q.add_argument("--why", help="the reason (required, audited)")
        if verb == "remove":
            q.add_argument("--team", action="store_true", help="retire it from devkit's defaults in the checkout")
        else:
            q.add_argument("--project", action="store_true", help="Claude Code plugins: this project only")
