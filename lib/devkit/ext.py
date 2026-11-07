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

import hashlib
import json
import os
import re
import shlex
import time
import unicodedata
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
    apps: tuple = (Path("/Applications/ChatGPT.app"), Path("/Applications/Codex.app"))  # the Codex app (F2)


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


# M2: a local MCP server runs through npx or uvx, and the pin binds the package they run: the first word after the
# runner's options (uvx: or its --from). Only options that cannot add or swap a package come before it.
MCP_RUNNERS = {"npx": ("npm", NPM_EXACT, frozenset({"-y", "--yes", "-q", "--quiet"})),
               "uvx": ("pypi", PYPI_EXACT, frozenset({"-q", "--quiet"}))}
# variables that make node, python or the dynamic loader run other code, or npm and uv fetch from another registry
CODE_ENV = re.compile(r"^(?:NODE_OPTIONS|NODE_PATH|PYTHONPATH|PYTHONSTARTUP|PYTHONHOME|PYTHONUSERBASE|DYLD_\w*|LD_\w*"
                      r"|NPM_CONFIG_\w*|UV_INDEX\w*|UV_DEFAULT_INDEX|UV_EXTRA_INDEX_URL|PIP_INDEX_URL|"
                      r"PIP_EXTRA_INDEX_URL)$", re.I)


def mcp_pin(eid: str, spec: dict) -> tuple:
    """(pin kind, pin, source, upstream) of an MCP server; ExtError unless it is an https URL, or npx or uvx running a
    package at an exact version with no variable that loads other code."""
    if not isinstance(spec, dict):
        raise ExtError(f"{eid}: an MCP server is an object with command and args, or a url")
    url = spec.get("url")
    if url:
        u = urlparse(str(url))
        if u.scheme != "https" or not u.hostname:
            raise ExtError(f"{eid}: a remote MCP server needs an https URL, not {url}")
        return "url", str(url), u.hostname, ""
    cmd, args = str(spec.get("command") or ""), [str(a) for a in spec.get("args") or []]
    if cmd not in MCP_RUNNERS:
        raise ExtError(f"{eid}: a local MCP server runs through npx or uvx, so the pinned package is what runs; got "
                       f"{cmd or 'no command'}")
    env = spec.get("env") or {}
    bad = sorted(k for k in (env if isinstance(env, dict) else {}) if CODE_ENV.match(str(k)))
    if bad:
        raise ExtError(f"{eid}: env {', '.join(bad)} makes the runtime load other code or another registry, so the "
                       f"pinned package would not be all that runs")
    registry, exact, flags = MCP_RUNNERS[cmd]
    i, pkg = 0, ""
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if cmd == "uvx" and a == "--from" and i + 1 < len(args):
            pkg, i = args[i + 1], i + 2
            break
        if cmd == "uvx" and a.startswith("--from="):
            pkg, i = a.split("=", 1)[1], i + 1
            break
        if a not in flags:
            raise ExtError(f"{eid}: `{cmd} {a}` may run another package than the pinned one; only "
                           f"{', '.join(sorted(flags))} may come before it")
        i += 1
    if not pkg:
        pkg = args[i] if i < len(args) else ""
    m = exact.match(pkg)
    if not m:
        form = "pkg@1.2.3" if cmd == "npx" else "pkg==1.2.3, or --from pkg==1.2.3 <command>"
        raise ExtError(f"{eid}: an MCP server needs an exact version on the package {cmd} runs ({form}) or an https "
                       f"url; got {' '.join([cmd] + args)}")
    return "version", m.group(2), f"{registry} {m.group(1)}", f"{registry}:{m.group(1)}"


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
        if agent not in AGENTS or not re.match(r"^[\w.][\w.\-]*@[\w.][\w.\-]*$", pid):
            raise ExtError(f"{label}: a plugin needs agent (claude or codex) and id name@marketplace (not starting "
                           f"with a dash)")
        return [_plugin(agent, pid, meta, "user", True)]
    name = str(item.get("name") or "")
    # L1: never a word that reads as an option; and a name is a folder in the shared skills folder, so never `.`, `..`
    # or a hidden one
    if not re.fullmatch(r"\w[\w.\-]*", name):
        raise ExtError(f"{label}: needs a name of letters, digits, dot, dash or underscore that starts with a letter, "
                       f"digit or underscore (not a dot or a dash)")
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


def _devkits_skill_names() -> set:
    """The skill names devkit itself puts in the shared skills folder, which no overlay skill may take: the skills in
    devkit's own tree, and playwright-cli (the packages step installs it there)."""
    from . import setup
    return set(setup.local_skills(core.DEVKIT_HOME)) | {"playwright-cli"}


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
    # one skill name is one folder in the shared skills directory for both agents: two sources with the same name
    # would replace each other's files (and the second would inherit what devkit trusts the first for, such as
    # impeccable's engine), so a name belongs to one source, whatever its group or agents; APFS ignores case, so
    # names are compared casefolded (Playwright-CLI is the playwright-cli folder)
    named = {}  # casefolded name -> (the name as skills.json spells it, its repository)
    for src in (t.get("skills") or {}).get("sources") or []:
        for name in src.get("skills") or {}:
            if name.casefold() in named:
                raise ExtError(f"skills.json: the skill {name} comes from both {named[name.casefold()][1]} and "
                               f"{src.get('repo')}; one name is one folder, so keep one source for it")
            named[name.casefold()] = (name, src.get("repo"))
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
    required = {e.id for e in out if e.required}  # M1: from the team layer, before an overlay entry can replace one
    added = [e for item in user["add"] for e in _user_items(item)]
    reserved = {n.casefold(): n for n in _devkits_skill_names()} if any(e.kind == "skill" for e in added) else {}
    mine = {}
    for e in added:
        if e.kind != "skill":
            continue
        key = e.name.casefold()
        if key in reserved:
            raise ExtError(f"ext.add skill {e.name}: devkit puts its own skill {reserved[key]} in that folder, and one "
                           f"name is one folder for both agents (in any case); give yours another name")
        if key in named:
            raise ExtError(f"ext.add skill {e.name}: the team's skill {named[key][0]} (from {named[key][1]}) has that "
                           f"name, and one name is one folder for both agents (in any case); give yours another name, "
                           f"or change the team entry on a branch of the devkit checkout (dev ext add --team)")
        if mine.setdefault(key, (e.name, e.source)) != (e.name, e.source) or \
                sum(1 for x in added if x.id == e.id) > 1:
            raise ExtError(f"ext.add skill {e.name}: the overlay names it twice; keep one entry")
    ids = {e.id for e in added}
    # a user entry replaces a team one with the same id (`dev ext add` refuses that; a hand-written overlay may), and
    # a required team entry stays required
    out = [e for e in out if e.id not in ids] + [Ext(**{**asdict(e), "required": True}) if e.id in required else e
                                                 for e in added]
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


# N5: Codex's MCP servers, read from the `mcp_servers` region of config.toml so that a form the reader does not know
# fails closed. TOML can write a server in many ways (dotted and quoted keys, inline tables, keys at the root or under
# `[mcp_servers]`, quoted names); the reader reads only the one-line `bare_key = value` lines under a
# `[mcp_servers.<name>]` header or its sub-tables, with a value JSON reads (a basic string, a number, true or false,
# an array of them) or a literal string. Every other line in the region is unread: one it can give to a server takes
# that server out of the app's; one it cannot (a multi-line string, a header or root key it cannot parse) takes them
# all. A server's digest is the raw text of its lines, so any change to them shows. On Python 3.11+ tomllib reads the
# file too, and a server it reads differently, or one only it sees, is unread as well.
_TOML_PART = r'(?:[A-Za-z0-9_-]+|"[^"\\\n]*"|\'[^\'\n]*\')'
_TOML_KEY = rf"{_TOML_PART}(?:[ \t]*\.[ \t]*{_TOML_PART})*"
_TOML_HEADER = re.compile(rf"^[ \t]*(\[\[?)[ \t]*({_TOML_KEY})[ \t]*(\]\]?)[ \t]*(?:#.*)?$")
_TOML_PAIR = re.compile(rf"^[ \t]*({_TOML_KEY})[ \t]*=[ \t]*(.*?)[ \t]*$")
_TOML_TAIL = re.compile(r"^[ \t]*(?:#.*)?$")
UNREAD_DETAIL = ("Codex's config.toml writes it in a form devkit does not read ({where}), so devkit cannot tell what "
                 "runs: write it as `codex mcp add` does, or remove it")


def _toml_parts(key: str) -> tuple:
    return tuple(p[1:-1] if p[:1] in "\"'" else p for p in re.findall(_TOML_PART, key))


def _toml_value(text: str):
    """A one-line value the reader reads, or _UNREAD."""
    m = re.match(r"^'([^'\n]*)'(.*)$", text)
    if m:
        return m.group(1) if _TOML_TAIL.match(m.group(2)) else _UNREAD
    try:
        v, end = json.JSONDecoder().raw_decode(text)
    except ValueError:
        return _UNREAD
    plain = (lambda x: isinstance(x, (str, bool, int)) or (isinstance(x, float) and x == x and abs(x) != float("inf"))
             or (isinstance(x, list) and all(plain(y) for y in x)))
    return v if plain(v) and _TOML_TAIL.match(text[end:]) else _UNREAD


_UNREAD = object()


_DELIMS = ('"""', "'''")


def _string_end(s: str, delim: str, start: int) -> int:
    """The index just after the delimiter that closes a multi-line string in `s`, from `start` (a basic string's
    escapes skipped; up to two more quotes are its content), or -1."""
    i = start
    while i < len(s):
        if delim == '"""' and s[i] == "\\":
            i += 2
            continue
        if s.startswith(delim, i):
            e = i + 3
            while e < len(s) and e < i + 5 and s[e] == delim[0]:
                e += 1
            return e
        i += 1
    return -1


def _toml_owner(path):
    """Whom a line in table `path` belongs to: a server's name, "" for the region with no server (the root,
    `[mcp_servers]`, or after a header the reader cannot parse: path None), None for a table outside the region."""
    if path is None or len(path) < 2 and path in ((), ("mcp_servers",)):
        return ""
    return path[1] if path[0] == "mcp_servers" else None


def codex_mcp_servers(text: str) -> tuple:
    """-> ({name: {"table", "raw", "unread"}}, loose): each server's table as read, its raw lines and the lines of it
    the reader does not read ("line 12"), and the unread lines of the region no server owns (N5)."""
    servers, loose, cur, inside = {}, [], (), None
    srv = (lambda name: servers.setdefault(name, {"table": {}, "raw": [], "unread": []}))

    def unread(who, where, s):
        if who:
            srv(who)["raw"].append(s)
            srv(who)["unread"].append(where)
        elif who == "":
            loose.append(where)

    for n, ln in enumerate(text.splitlines(), 1):
        s, where = ln.strip(), f"line {n}"
        if inside:  # in a multi-line string: its lines belong to the key that opened it
            unread(inside[1], where, s)
            end = _string_end(s, inside[0], 0)
            if end >= 0:
                inside = None
                if any(d in s[end:] for d in _DELIMS):
                    loose.append(where)
                    cur = None
            continue
        if not s or s.startswith("#"):
            continue
        m = _TOML_PAIR.match(ln)
        if any(d in s for d in _DELIMS):
            value = m.group(2) if m else ""
            d = next((x for x in _DELIMS if value.startswith(x)), None)
            if not d:  # a delimiter inside another value, or in an array: where the string ends is not plain
                loose.append(where)
                cur = None
                continue
            who = _toml_owner(None if cur is None else cur + _toml_parts(m.group(1)))
            unread(who, where, s)
            end = _string_end(value, d, 3)
            if end < 0:
                inside = (d, who)
            elif any(x in value[end:] for x in _DELIMS):
                loose.append(where)
                cur = None
            continue
        if s.startswith("["):
            h = _TOML_HEADER.match(ln)
            if not h or len(h.group(1)) != len(h.group(3)):
                loose.append(where)  # a header the reader cannot parse may name the region
                cur = None
                continue
            cur = _toml_parts(h.group(2))
            who = _toml_owner(cur)
            if who:
                srv(who)["raw"].append(s)
                if h.group(1) == "[[" or "\"" in h.group(2) or "'" in h.group(2):
                    srv(who)["unread"].append(where)
                table = srv(who)["table"]
                for part in cur[2:]:  # a sub-table exists once its header does, as TOML reads it
                    table = table.setdefault(part, {}) if isinstance(table, dict) else None
            elif who == "" and h.group(1) == "[[":
                loose.append(where)
            continue
        if not m or cur is None:  # a value's continuation, or a line the reader does not know
            unread(_toml_owner(cur), where, s)
            continue
        key = _toml_parts(m.group(1))
        who = _toml_owner(cur + key)
        if not who:
            if who == "":
                loose.append(where)  # `mcp_servers = {…}`: an inline table of servers
            continue
        entry = srv(who)
        entry["raw"].append(s)
        value = _toml_value(m.group(2))
        table = entry["table"]
        for part in cur[2:]:
            table = table.setdefault(part, {}) if isinstance(table, dict) else None
        if len(cur) < 2 or len(key) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]+", m.group(1).strip()) \
                or value is _UNREAD or not isinstance(table, dict):
            entry["unread"].append(where)
            continue
        table[key[0]] = value
    _toml_check(text, servers, loose)
    return servers, loose


def _toml_check(text: str, servers: dict, loose: list):
    """On Python 3.11+, tomllib's reading of the region must match the line reader's (N5)."""
    try:
        import tomllib
    except ImportError:  # Python 3.9 and 3.10: the line reader alone, which fails closed
        return
    try:
        real = tomllib.loads(text).get("mcp_servers", {})
    except tomllib.TOMLDecodeError:
        loose.append("the file is not valid TOML")
        return
    if not isinstance(real, dict):
        loose.append("mcp_servers is no table")
        return
    for name in set(real) - set(servers):
        loose.append(f"a server only TOML sees ({len(name)} characters)")
    for name, entry in servers.items():
        if not entry["unread"] and real.get(name) != entry["table"]:
            entry["unread"].append("TOML reads it differently")


def _args_version(args) -> str:
    for a in args or []:
        m = NPM_EXACT.match(str(a)) or PYPI_EXACT.match(str(a))
        if m:
            return m.group(2)
    return ""


def observe(h: Homes = None) -> dict:
    """What is installed now, by catalog id: {"installed", "version", "sha", "enabled", "ref", "path"}; a skill folder
    devkit did not put in an agent's skills folder has "unmanaged", and a managed skill whose folder is not devkit's
    copy has "foreign". Reads only the agents' files, so it is fast and safe in a hook."""
    h = h or homes()
    out = {}
    inst = (_read_json(h.claude / "plugins" / "installed_plugins.json") or {}).get("plugins") or {}
    enabled = (_read_json(h.claude / "settings.json") or {}).get("enabledPlugins") or {}
    for pid, entries in inst.items():
        rows = [e for e in (entries if isinstance(entries, list) else []) if isinstance(e, dict)]
        e = next((x for x in rows if x.get("scope") == "user"), rows[0] if rows else None)
        if e:
            name, _, market = str(pid).partition("@")
            version = str(e.get("version") or "")
            path = str(e.get("installPath") or h.claude / "plugins" / "cache" / market / name / version)
            out[plugin_id("claude", pid)] = {"installed": True, "version": version,
                                             "sha": str(e.get("gitCommitSha") or ""), "path": path,
                                             "enabled": enabled.get(pid) if pid in enabled else None}
    for name, spec in ((_read_json(h.claude_json) or {}).get("mcpServers") or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        out[f"mcp:claude:{name}"] = {"installed": True, "version": _args_version(spec.get("args")),
                                     "url": spec.get("url") or "", "command": str(spec.get("command") or "")}
    try:
        text = (h.codex / "config.toml").read_text()
    except (OSError, UnicodeDecodeError):
        text = ""
    toml = _toml_sections(text)
    cache = h.codex / "plugins" / "cache"
    if cache.is_dir():
        for market in sorted(p for p in cache.iterdir() if p.is_dir()):
            for plug in sorted(p for p in market.iterdir() if p.is_dir()):
                versions = sorted((v.name for v in plug.iterdir() if v.is_dir()), key=_version_key)
                if not versions:
                    continue
                pid = f"{plug.name}@{market.name}"
                flag = (toml.get(f'plugins."{pid}"') or {}).get("enabled")
                o = {"installed": True, "version": versions[-1], "sha": "", "path": str(plug / versions[-1]),
                     "enabled": None if flag is None else flag == "true"}
                if market.name == RUNTIME:
                    o["app"] = "record"  # F2: the app downloads it, so only dev install's record vouches for it
                elif market.name == BUNDLED and _bundled(h.apps, plug.name):
                    # N6: the app copies its bundled plugin into the cache, so a copy of the bundle's files vouches
                    # for itself; one the app rewrote, or anyone changed, only once dev install recorded it
                    o["app"] = "bundle" if _bundle_copy(h.apps, plug.name, plug / versions[-1]) else "record"
                out[plugin_id("codex", pid)] = o
    servers, loose = codex_mcp_servers(text)
    for name, entry in servers.items():
        tbl = entry["table"]
        args = tbl.get("args") if isinstance(tbl.get("args"), list) else []
        cmd = tbl.get("command") if isinstance(tbl.get("command"), str) else ""
        url = tbl.get("url") if isinstance(tbl.get("url"), str) else ""
        o = {"installed": True, "version": _args_version(args), "url": url, "command": cmd,
             "args": [str(a) for a in args],
             "spec": "sha256:" + hashlib.sha256("\n".join(entry["raw"]).encode()).hexdigest()}
        env = tbl.get("env") if isinstance(tbl.get("env"), dict) else {}
        passed = tbl.get("env_vars") if isinstance(tbl.get("env_vars"), list) else []  # taken from Codex's own
        if entry["unread"]:
            o["unread"] = entry["unread"][0]
        elif not loose and _app_server(cmd, set(env) | {str(v) for v in passed}, h.apps):
            o["app"] = "record"  # N4: only dev install's record of its table vouches for it
        out[f"mcp:codex:{name}"] = o
    if loose:  # N5: a line no server owns could change any of them, so none is the app's, and the file is drift
        out[CONFIG_ROW] = {"installed": True, "version": "", "url": "", "command": "", "unread": loose[0]}
    skill_homes = (("claude", h.claude / "skills"), ("codex", h.agents_skills))
    if h.skills_share.is_dir():
        for d in sorted(p for p in h.skills_share.iterdir() if p.is_dir()):
            try:
                first = (d / ".devkit-source").read_text().split()
            except OSError:
                continue
            repo, _, ref = first[0].rpartition("@") if first else ("", "", "")
            for agent, home in skill_homes:
                linked = (home / d.name / "SKILL.md").exists()
                o = {"installed": linked, "ref": ref, "repo": repo}
                if linked and os.path.realpath(home / d.name) != os.path.realpath(d):
                    o["foreign"] = True  # L4: the agent loads another folder under devkit's skill name
                out[f"skill:{agent}:{d.name}"] = o
    share = os.path.realpath(h.skills_share) + os.sep
    for agent, home in skill_homes:  # L4: skill folders devkit did not put there
        try:
            entries = sorted(home.iterdir()) if home.is_dir() else []
        except OSError:
            entries = []
        for p in entries:
            sid = f"skill:{agent}:{p.name}"
            if sid in out or not (p / "SKILL.md").exists():
                continue
            if (os.path.realpath(p) + os.sep).startswith(share):
                continue  # devkit's own folder, such as playwright-cli from the packages step
            out[sid] = {"installed": True, "unmanaged": True, "path": str(p)}
    return out


# What the agents write into a plugin's folder while they run: left out of its digest. A runtime file outside these
# names is a change.
RUNTIME_FILES = frozenset({".in_use", ".orphaned_at", "__pycache__", ".DS_Store"})


def content_digest(path) -> str:
    """M3: sha256 of a plugin's install folder (every file's relative path and bytes, every link's target, in a fixed
    order; RUNTIME_FILES left out), so the lock does not rest on version and SHA fields an agent can rewrite. '' when
    the folder is missing."""
    root = Path(path)
    if not root.is_dir():
        return ""
    h = hashlib.sha256()
    for dp, dns, fns in os.walk(root):
        keep = []
        for name in sorted(dns):
            p = Path(dp, name)
            if name in RUNTIME_FILES:
                continue
            if p.is_symlink():  # os.walk does not enter it: its target is the content
                h.update(f"{p.relative_to(root).as_posix()} L {os.readlink(p)}\n".encode())
            else:
                keep.append(name)
        dns[:] = keep
        for name in sorted(fns):
            if name in RUNTIME_FILES:
                continue
            p = Path(dp, name)
            rel = p.relative_to(root).as_posix()
            try:
                body = f"L {os.readlink(p)}" if p.is_symlink() else f"F {hashlib.sha256(p.read_bytes()).hexdigest()}"
            except OSError as e:
                body = f"E {type(e).__name__}"
            h.update(f"{rel} {body}\n".encode())
    return "sha256:" + h.hexdigest()


def read_lock(path: Path = None) -> dict:
    doc = _read_json(path or lock_file())
    return doc if isinstance(doc, dict) else {}


# --------------------------------------------------------------------------- status rows

def _row(e: Ext, layer: str = None) -> dict:
    r = asdict(e)
    r.pop("spec", None)
    r["layer"] = layer or e.layer
    return r


# What ships with the Codex app (H1, F2), listed with layer `agent`. Agents can write Codex's plugin cache and its
# config.toml, so a folder name, a path prefix or a program's place in the bundle proves nothing. observe() marks an
# entry `app` when the app may vouch for it: `bundle` for a cached plugin of its bundled marketplace whose files are
# all byte-equal to the bundle's (N6), which needs nothing more; `record` for a plugin of its runtime marketplace,
# a bundled plugin the app rewrote, and an MCP server that runs from the bundle's Resources with no interpreter and no
# environment that loads other code: those count as the app's only once `dev install` recorded them (N4), and it names
# each the first time. The record holds a version and file digest, or a server's raw text, so a later change is drift.
# A server written in a form the reader does not read is never the app's (N5). Claude Code ships none.
BUNDLED, RUNTIME = "openai-bundled", "openai-primary-runtime"
CONFIG_ROW = "mcp:codex:(config.toml)"  # N5: what no server owns in config.toml's mcp_servers region
# programs that run whatever code their arguments, input or environment name: never the app's own server
APP_INTERPRETERS = re.compile(r"^(?:node(?:js)?[\d.]*|bun|deno|python[\d.]*w?|ruby|perl[\d.]*|php|lua|osascript|env|"
                              r"corepack|npx|npm|pnpm|yarn|uvx?|codex|claude|(?:ba|z|da|k|c|tc|fi)?sh)$", re.I)
OUTSIDE_DETAIL = {"plugin": "installed, but not in devkit's catalog: dev ext add it, or remove it",
                  "mcp": "an MCP server devkit does not manage: dev ext add it, or remove it",
                  "skill": "a skill folder devkit did not put there: dev ext add it, or remove the folder"}


def _bundle_dirs(apps, name: str) -> list:
    if name in ("", ".", "..") or "/" in name:
        return []
    dirs = (Path(app) / "Contents" / "Resources" / "plugins" / BUNDLED / "plugins" / name for app in apps)
    return [d for d in dirs if d.is_dir()]


def _bundled(apps, name: str) -> bool:
    """The app bundle itself holds the bundled marketplace's plugin `name` (F2)."""
    return bool(_bundle_dirs(apps, name))


def _bundle_copy(apps, name: str, path) -> bool:
    """N6: the cached plugin at `path` is a copy of the bundle's plugin `name`: it holds at least one file, every
    entry in it is a regular file (no link), and each is byte-equal to the bundle's file at the same path (the app
    copies a subset). Sizes first, so a changed file costs no read."""
    root = Path(path)
    files = []
    for dp, dns, fns in os.walk(root):
        for d in dns:
            if Path(dp, d).is_symlink():
                return False
        for f in fns:
            p = Path(dp, f)
            if p.is_symlink() or not p.is_file():
                return False
            files.append(p.relative_to(root))
    if not files:
        return False
    for src in _bundle_dirs(apps, name):
        pairs = [(root / rel, src / rel) for rel in files]
        try:
            if all(b.is_file() and a.stat().st_size == b.stat().st_size for a, b in pairs) and \
                    all(a.read_bytes() == b.read_bytes() for a, b in pairs):
                return True
        except OSError:
            continue
    return False


def _app_server(cmd: str, env_keys, apps) -> bool:
    """An MCP server the app runs from its own bundle (F2): the command, resolved (links, `..`), is a file in the
    bundle's Resources (Contents/MacOS holds the Electron binary, which runs as node); it is no interpreter, agent or
    package runner, whose arguments say what runs; and its environment loads no other code."""
    if not os.path.isabs(cmd):
        return False
    real = os.path.realpath(cmd)
    if any(APP_INTERPRETERS.match(os.path.basename(p)) for p in (cmd, real)):
        return False
    if any(CODE_ENV.match(k) or k.upper() == "ELECTRON_RUN_AS_NODE" for k in env_keys):
        return False
    return os.path.isfile(real) and any(
        real.startswith(os.path.realpath(Path(app) / "Contents" / "Resources") + os.sep) for app in apps)


def _app_drift(o: dict, lk: dict) -> list:
    """What changed in an entry that ships with the app since `dev install` recorded it (none before it did)."""
    if not lk:
        return []
    if "spec" in o:
        return [] if lk.get("spec") == o["spec"] else [
            {"kind": "changed", "detail": "its command, arguments or environment changed outside devkit"}]
    if lk.get("version") != o.get("version"):
        return [{"kind": "changed", "detail": f"{_vs(lk)} → {_vs(o)} outside devkit", "from": _vs(lk), "to": _vs(o)}]
    if o.get("path") and content_digest(o["path"]) != lk.get("digest"):
        return [{"kind": "changed", "detail": f"the files of {_vs(o)} changed outside devkit (same version)",
                 "from": _vs(lk), "to": _vs(o)}]
    return []


def _project_value(v) -> tuple:
    """(on, file) of one project setting: project_plugins() gives {"on", "file"}; a plain bool is the shared file."""
    if isinstance(v, dict):
        return bool(v.get("on")), str(v.get("file") or ".claude/settings.json")
    return bool(v), ".claude/settings.json"


def _extra_row(rid: str, layer: str, why: str, state: str, o: dict, enabled: bool, drift: list) -> dict:
    """A row for an entry outside the catalog: retired, installed outside devkit, or enabled by a project."""
    kind, agent, name = rid.split(":", 2)
    return {"id": rid, "kind": kind, "agent": agent, "name": name.partition("@")[0], "source": name.partition("@")[2],
            "pin": "", "pin_kind": "", "license": "", "why": why, "layer": layer, "required": False,
            "enabled": enabled, "group": "", "upstream": "", "state": state, "version": str(o.get("version") or ""),
            "sha": str(o.get("sha") or ""), "locked": None, "drift": drift}


def status(cat: list, observed: dict, lock: dict = None, project: dict = None, retired=(),
           managed: dict = None) -> list:
    """One row per catalog entry, plus retired entries still installed, entries installed outside the catalog (both
    agents: plugins, MCP servers and skill folders), what ships with the agent's app, and project-enabled plugins
    outside the catalog. Each row has `state`, `enabled` (after the managed and project layers), `version`, `sha` and
    `drift` (kinds: missing, pin, changed, enabled, required-off, retired, not-in-catalog). `project`:
    project_plugins(); `managed`: the managed-settings lock's enabledPlugins, which a project cannot override."""
    lock, project, managed = lock or {}, project or {}, managed or {}
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
        if installed and e.kind == "skill" and o.get("foreign"):
            drift.append({"kind": "changed", "detail": "the agent's folder for it is not devkit's copy: dev install "
                                                       "links it again"})
        if installed and e.kind == "mcp" and e.pin_kind == "version" and o.get("version") and o["version"] != e.pin:
            drift.append({"kind": "pin", "detail": f"{o['version']} installed, {e.pin} pinned"})
        if installed and o.get("unread"):  # N5: devkit cannot tell what runs
            drift.append({"kind": "changed", "detail": UNREAD_DETAIL.format(where=o["unread"])})
        if installed and e.pin_kind == "observed" and lk:
            if (lk.get("version"), lk.get("sha") or r["sha"]) != (r["version"], r["sha"]):
                drift.append({"kind": "changed", "detail": f"{_vs(lk)} → {_vs(r)} outside devkit",
                              "from": _vs(lk), "to": _vs(r)})
            elif lk.get("digest") and "path" in o and content_digest(o["path"]) != lk["digest"]:  # M3
                drift.append({"kind": "changed", "detail": f"the files of {_vs(r)} changed outside devkit (same "
                                                           f"version and SHA)", "from": _vs(lk), "to": _vs(r)})
        if installed and e.kind == "plugin" and o.get("enabled") is not None and bool(o["enabled"]) != e.enabled:
            drift.append({"kind": "enabled", "detail": f"{'on' if o['enabled'] else 'off'} in the agent's settings, "
                                                       f"{'on' if e.enabled else 'off'} in devkit's"})
        on, where = e.enabled, ""
        pid = e.spec.get("id") if e.agent == "claude" and e.kind == "plugin" else None
        if pid in managed:
            on, r["layer_on"] = bool(managed[pid]), "managed"
        elif pid in project:  # H2: the project's own value, from the file that sets it
            on, where = _project_value(project[pid])
            r["layer_on"] = "project"
        if e.required and not installed:
            drift.append({"kind": "required-off", "detail": "required, but not installed"})
        elif e.required and not on:
            drift.append({"kind": "required-off", "detail": f"required, but off in this project's {where}" if where
                          else "required, but off"})
        elif e.required and o.get("enabled") is False:
            drift.append({"kind": "required-off", "detail": "required, but off in the agent's settings"})
        r["enabled"] = on
        r["drift"] = drift
        rows.append(r)
    listed = {e.id for e in cat}
    for rid in sorted(set(retired) - listed):
        o = observed.get(rid) or {}
        if o.get("installed"):
            rows.append(_extra_row(rid, "retired", "retired from devkit's defaults",
                                   STATE_WORD.get(rid.split(":")[0], "installed"), o, False,
                                   [{"kind": "retired", "detail": "still installed: dev install removes it"}]))
            listed.add(rid)
    for rid in sorted(observed):  # H1, L4: what is installed outside the catalog
        o = observed[rid]
        if rid in listed or not o.get("installed") or rid.count(":") < 2:
            continue
        kind, agent, name = rid.split(":", 2)
        if kind not in OUTSIDE_DETAIL or (kind == "skill" and not o.get("unmanaged")):
            continue  # a managed skill of a group this Mac does not install: the skills step's business
        app, lk = (o.get("app") if agent == "codex" else ""), lock.get(rid)
        if app == "bundle" or (app and lk):  # F2, N4: what the bundle's own files or dev install's record vouch for
            rows.append(_extra_row(rid, "agent", f"ships with the {agent} app", STATE_WORD[kind], o,
                                   o.get("enabled") is not False, _app_drift(o, lk)))
        else:
            detail = UNREAD_DETAIL.format(where=o["unread"]) if o.get("unread") else OUTSIDE_DETAIL[kind]
            rows.append(_extra_row(rid, "outside", "installed outside devkit", STATE_WORD[kind], o,
                                   o.get("enabled") is not False, [{"kind": "not-in-catalog", "detail": detail}]))
        listed.add(rid)
    for pid, v in sorted(project.items()):
        rid = plugin_id("claude", pid)
        on, where = _project_value(v)
        if on and rid not in listed:
            rows.append(_extra_row(rid, "project", f"enabled by this project's {where}", "project", {}, True,
                                   [{"kind": "not-in-catalog", "detail": f"this project's {where} enables a plugin "
                                                                         f"devkit does not manage"}]))
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


PROJECT_SETTINGS = (".claude/settings.json", ".claude/settings.local.json")  # the later one wins, as in Claude Code


def project_plugins(cwd: str = None) -> dict:
    """`enabledPlugins` of the project's own Claude settings (H2: the shared .claude/settings.json and the personal
    .claude/settings.local.json, which wins) -> {name@marketplace: {"on", "file"}}."""
    try:
        top = core.gitout(cwd or os.getcwd(), "rev-parse", "--show-toplevel")
    except OSError:  # the folder was removed
        top = ""
    if not top:
        return {}
    out = {}
    for rel in PROJECT_SETTINGS:
        doc = _read_json(Path(top) / rel)
        ep = doc.get("enabledPlugins") if isinstance(doc, dict) else None
        for k, v in (ep.items() if isinstance(ep, dict) else ()):
            if isinstance(k, str):
                out[k] = {"on": bool(v), "file": rel}
    return out


def managed_plugins() -> dict:
    """`enabledPlugins` of devkit's managed-settings drop-in (`dev config lock`, root-owned): what no project or user
    setting overrides. {} when the lock is off."""
    from . import policy
    doc = _read_json(policy.MANAGED_FILE)
    ep = doc.get("enabledPlugins") if isinstance(doc, dict) else None
    return {k: bool(v) for k, v in ep.items() if isinstance(k, str)} if isinstance(ep, dict) else {}


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


def _edit_lock(fn, path: Path = None) -> bool:
    """Change the lock under devkit's state lock; `fn` edits the document in place. -> False when the lock is busy or
    does not parse, so nothing was written."""
    from . import state
    try:
        state.update(path or lock_file(), fn, indent=2)
    except state.StateBusy:
        return False
    return True



# --------------------------------------------------------------------------- lock and drift (step 3)

REPORTED = ("changed", "pin", "required-off", "enabled", "retired", "not-in-catalog")  # `missing`: doctor's own checks


def drift_seen_file() -> Path:
    return core.STATE / "ext-drift.json"


def record_lock(agent: str, h: Homes = None, by: str = "dev install", t: dict = None, user: dict = None,
                path: Path = None, only: set = None, accept: bool = True) -> int:
    """`dev install` and `dev ext update --apply`: record what this agent's vendor marketplace plugins, and what ships
    with its app (F2), are now (or only the ids in `only`): version, SHA and a digest of their files (M3); for an MCP
    server, a digest of its table. A later change outside devkit then shows as drift. Accepting a change made outside
    devkit, or recording what ships with the app for the first time, is the developer's (F1 of re-review four): only
    when core.set_by_developer() holds, here in the writer too, since a program can call it past dev install; `accept`
    False narrows it further. Otherwise both stay as drift. No check inside a process proves that a person typed the
    command (a process an agent starts in screen, tmux or a Terminal window passes it), so each acceptance is also
    made visible (F1-R4 part 1): one `ext.accept` audit record with the entry, the terminal and the parent processes,
    and an entry in the acceptance list that the session start and `dev status` show until the developer runs
    `dev ext ack`. Compared with the lock under its state lock, so nothing changes between the check and the record.
    -> how many were recorded."""
    now = int(time.time())
    rows = _lockable(agent, h, t, user, only)
    count, accepted = [0], []
    try:
        accept = bool(accept) and core.set_by_developer()
    except Exception:
        accept = False  # a check that cannot decide vouches for nothing (fail closed)

    def put(doc):
        writes = []  # (eid, new record, the acceptance's text or "")
        for eid, o in rows.items():
            new = {"pin": "observed", "version": o.get("version", ""), "sha": o.get("sha", ""),
                   "digest": content_digest(o["path"]) if o.get("path") else "", "at": now, "by": by}
            if "spec" in o:
                new["spec"] = o["spec"]
            lk = doc.get(eid)
            change = _lock_change(lk, new) if lk else ""
            first_app = bool(o.get("app") and not lk)
            if not accept and (first_app or change):
                continue
            what = f"{_app_line(agent, eid, o)}; first record by {by}" if first_app else \
                f"{_what(agent, eid, o)}{':' if 'spec' in o else ''} {change}; accepted by {by}" if change else ""
            writes.append((eid, new, what))
        accepted[:] = [(eid, what) for eid, _, what in writes if what]
        # the acceptance list first, while the lock is held: an acceptance it cannot list is no acceptance (fail
        # closed), so the change stays as drift
        listed = _note_accepted(accepted, now, by, path) if accepted else True
        for eid, new, what in writes:
            if what and not listed:
                continue
            doc[eid] = new
            count[0] += 1
    if not _edit_lock(put, path):
        return 0
    return count[0]


def accepted_file(lock: Path = None) -> Path:
    """The acceptance list, next to the lock it belongs to (a temporary lock keeps its own list)."""
    return (lock or lock_file()).with_name("ext-accepted.json")


def _tty() -> str:
    for fd in (0, 1, 2):
        try:
            return os.ttyname(fd)
        except OSError:
            continue
    return ""


def _parents(limit: int = 8) -> list:
    """The parent processes' command lines, nearest first, each cut to 80 characters; [] when `ps` gives none."""
    try:
        chain = core._ancestry() or []
    except Exception:
        chain = []
    return [c if len(c) <= 80 else c[:79] + "…" for c in chain[:limit]]


def _note_accepted(items: list, now: int, by: str, lock: Path = None) -> bool:
    """Add each accepted entry to the acceptance list and write one `ext.accept` audit record for it, with the
    terminal and the parent processes it was accepted from (F1-R4 part 1). The audit records are written only once
    the list has been read under its lock. -> False when the list is busy or does not parse: then nothing is
    accepted, and the refusal is audited."""
    from . import audit, state
    tty, parents = _tty(), _parents()
    where = f"tty {tty or 'none'}; parents: {' ← '.join(parents) or 'unknown'}"
    if lock and Path(lock) != lock_file():
        where += f"; lock {lock}"

    def add(doc):
        out = []
        for eid, what in items:
            rec = audit.record("ext.accept", eid, f"{what}; {where}", "accepted")
            out.append({"id": eid, "what": what, "at": now, "by": by, "tty": tty, "parents": parents,
                        "record": (rec or {}).get("id", "")})
        # never trimmed: a flood of acceptances must not push an earlier one out of the developer's sight
        doc["pending"] = list(doc.get("pending") or []) + out
    try:
        state.update(accepted_file(lock), add, indent=1)
    except (state.StateBusy, OSError) as e:  # busy, unparsable, or not writable (a full disk): fail closed
        for eid, what in items:
            audit.record("ext.accept", eid, f"{what}; not accepted: the acceptance list could not be written ({e}), "
                                            f"so it stays as drift; {where}", "refused")
        return False
    try:  # the developer sees it now, on the desktop, not only at the next session start: a security notice, which
        # DEVKIT_NO_NOTIFY and progress.notify do not turn off (an agent can set either)
        from . import progress
        more = f" (and {len(items) - 1} more)" if len(items) > 1 else ""
        progress.notify("extension accepted", f"{_shown(items[0][1])}{more}; tty {_shown(tty, 40) or 'none'}. "
                                              f"Not you? dev audit --action ext.accept", security=True)
    except Exception:
        pass
    return True


def pending_acceptances(lock: Path = None) -> list:
    """What was accepted since the developer last acknowledged (`dev ext ack`), oldest first."""
    doc = _read_json(accepted_file(lock))
    items = doc.get("pending") if isinstance(doc, dict) else None
    return [p for p in items or [] if isinstance(p, dict) and p.get("id")]


# Unicode's Default_Ignorable_Code_Point ranges (DerivedCoreProperties.txt): characters that render as nothing, such
# as the variation selectors, the combining grapheme joiner, the Hangul fillers and the tag characters
_IGNORABLE = ((0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5),
              (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164),
              (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
              (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF))
_IGNORABLE_RE = re.compile("[" + "".join(f"{re.escape(chr(a))}-{re.escape(chr(b))}" for a, b in _IGNORABLE) + "]")


def _shown(text, width: int = 100) -> str:
    """One entry as the session line, `dev status` and the desktop notice show it, cut to about `width` characters:
    no control characters (an escape sequence must not redraw the terminal or hide the rest), no format characters
    (Unicode category Cf: the bidirectional controls that reorder what the developer reads, the zero-width ones that
    split a name invisibly, and the tag characters that carry text only a model reads), no lone surrogates (category
    Cs, which no program can be started with), and no other character Unicode says renders as nothing (_IGNORABLE:
    the variation selectors, the combining grapheme joiner, the Hangul fillers). Letters, marks, digits,
    punctuation and symbols of every script stay, so a name reads as written. Whitespace becomes one space. The audit
    trail keeps the full text."""
    s = "".join(c for c in str(text or "") if c.isspace() or unicodedata.category(c) not in ("Cc", "Cf", "Cs"))
    s = re.sub(r"\s+", " ", _IGNORABLE_RE.sub("", s)).strip()
    return s if len(s) <= width else s[:width - 1].rstrip() + "…"


def accepted_text(items: list, limit: int = 4) -> str:
    def one(p):
        when = time.strftime("%d %b %H:%M", time.localtime(int(p.get("at") or 0)))
        return f"{_shown(p.get('what') or p['id'])} ({when}, tty {_shown(p.get('tty'), 40) or 'none'})"
    more = f"; and {len(items) - limit} more" if len(items) > limit else ""
    return "; ".join(one(p) for p in items[:limit]) + more


def accepted_line(lock: Path = None):
    """The session start's line about what was accepted since the developer's last acknowledgement, or None. A
    terminal an agent opens can look like the developer's, so every acceptance shows until the developer says it was
    theirs."""
    items = pending_acceptances(lock)
    if not items:
        return None
    return (f"devkit · extensions accepted since your last session: {accepted_text(items)}. A process an agent "
            f"starts can look like your terminal: if you did not run these, remove the entry and review the audit "
            f"trail (`dev audit --action ext.accept`). The developer acknowledges them with `dev ext ack`, in a "
            f"terminal.")


def last_ack_line(lock: Path = None):
    """The session start's line about the last `dev ext ack`, once: when and from which terminal the acceptances were
    confirmed, so a confirmation the developer did not make shows. None when there is none or it was shown."""
    from . import state
    last = {}

    def take(doc):
        a = doc.get("last_ack")
        if not isinstance(a, dict) or a.get("shown"):
            return state.SKIP
        last.update(a)
        a["shown"] = True
    try:
        state.update(accepted_file(lock), take, create=False, indent=1)
    except state.StateBusy:
        return None
    if not last:
        return None
    when = time.strftime("%d %b %H:%M", time.localtime(int(last.get("at") or 0)))
    return (f"devkit · the last `dev ext ack` confirmed {int(last.get('count') or 0)} acceptance(s) on {when} from "
            f"tty {_shown(last.get('tty'), 40) or 'none'}. If that was not you, review `dev audit --action ext` and "
            f"the extensions it confirmed (`dev ext ls`).")


def _lock_change(lk: dict, new: dict) -> str:
    """How an entry's record differs from what it is now (`new`, as record_lock() writes it), or ''."""
    if "spec" in new:
        return "its command, arguments or environment changed" if lk.get("spec") != new["spec"] else ""
    if (lk.get("version"), lk.get("sha")) != (new.get("version", ""), new.get("sha", "")):
        return f"{_vs(lk)} → {_vs(new)}"
    if lk.get("digest") and new.get("digest") and new["digest"] != lk["digest"]:
        return f"{_vs(new)}: its files changed (same version and SHA)"
    return ""


def _what(agent: str, eid: str, o: dict) -> str:
    kind, _, rest = eid.split(":", 2)
    return f"{rest.partition('@')[0]} ({agent}{' app' if o.get('app') else ''} {kind})"


def _lockable(agent: str, h: Homes = None, t: dict = None, user: dict = None, only: set = None) -> dict:
    """{id: observed} of this agent's installed vendor marketplace plugins and of what ships with its app (observe()
    marks it `app`), outside the catalog (or only the ids in `only`)."""
    t = team() if t is None else t
    obs = observe(h)
    cat = catalog(t, user_layer() if user is None else user, None)
    out = {e.id: obs[e.id] for e in cat if e.agent == agent and e.pin_kind == "observed"
           and (obs.get(e.id) or {}).get("installed")}
    listed = {e.id for e in cat}
    out.update({rid: o for rid, o in obs.items() if o.get("app") and o.get("installed") and rid not in listed
                and rid.split(":")[1:2] == [agent]})
    return {k: v for k, v in out.items() if only is None or k in only}


def lock_changes(agent: str, h: Homes = None, t: dict = None, user: dict = None, path: Path = None) -> list:
    """L3: what record_lock() would accept for this agent, one line per changed entry ("superpowers (claude plugin)
    7.0.0 bb92a77 → 7.0.1 414603b"), so `dev install` names it before it records it."""
    lock, out = read_lock(path), []
    for eid, o in sorted(_lockable(agent, h, t, user).items()):
        lk = lock.get(eid)
        if not lk:
            continue
        new = {"version": o.get("version", ""), "sha": o.get("sha", ""), **({"spec": o["spec"]} if "spec" in o else {}),
               "digest": content_digest(o["path"]) if o.get("path") and lk.get("digest") else ""}
        change = _lock_change(lk, new)
        if change:
            out.append(f"{_what(agent, eid, o)}{':' if 'spec' in o else ''} {change}")
    return out


def _new_app(agent: str, h: Homes = None, t: dict = None, user: dict = None, path: Path = None) -> dict:
    lock = read_lock(path)
    return {rid: o for rid, o in sorted(_lockable(agent, h, t, user).items()) if o.get("app") and rid not in lock}


def new_app_entries(agent: str, h: Homes = None, t: dict = None, user: dict = None, path: Path = None) -> list:
    """F2: the names of what ships with the agent's app that record_lock() would record for the first time: from then
    on its record is what vouches for the runtime marketplace's plugins and the app's MCP servers."""
    return sorted(rid.split(":", 2)[2].partition("@")[0] for rid in _new_app(agent, h, t, user, path))


def _app_line(agent: str, rid: str, o: dict) -> str:
    """One entry that ships with the app as the developer reads it: a plugin with its version, an MCP server with the
    command and arguments it runs."""
    run = shlex.join([o["command"], *o.get("args", [])]) if o.get("command") else o.get("url", "")
    return f"{_what(agent, rid, o)}: {run}" if "spec" in o else f"{_what(agent, rid, o)} {_vs(o)}"


def new_app_lines(agent: str, h: Homes = None, t: dict = None, user: dict = None, path: Path = None) -> list:
    """new_app_entries() as `dev install` names them, one line each: a plugin with its version, an MCP server with
    the command and arguments it runs (F1 of re-review four: the developer sees what the record will vouch for)."""
    return [_app_line(agent, rid, o) for rid, o in _new_app(agent, h, t, user, path).items()]


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
                  retired_ids(t), managed_plugins())


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
    """`dev status`: extensions accepted since the developer's last acknowledgement, and extensions changed outside
    devkit, as "Needs you" items."""
    items = pending_acceptances()
    out = [{"kind": "devkit", "text": f"Extensions accepted since your last session: {accepted_text(items, 3)}.",
            "command": "dev ext ack"}] if items else []
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
            ("required-off", "required extensions on", "dev install puts a missing one back; dev config lock keeps "
                                                        "required plugins on whatever a project's .claude/settings "
                                                        "say (the developer, in a terminal)"),
            ("changed", "vendor plugins match the lock", "review with dev ext outdated <name>; accept with dev install "
                                                         "or dev ext update <name> --apply"),
            ("pin", "installed at their pins", "dev install --only skills,claude,codex"),
            ("enabled", "enabled as devkit says", "dev config repair"),
            ("retired", "retired extensions removed", "dev install --only claude,codex"),
            ("not-in-catalog", "installed extensions in the catalog", "dev ext add it, or remove it (the developer, "
                                                                      "in a terminal; a project plugin: from the "
                                                                      "project's .claude/settings files)")):
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

    def on_default_branch(self, e: Ext, sha: str) -> str:
        """M4: GitHub's compare status of the default branch against `sha`: behind or identical when the default
        branch contains the commit; ahead or diverged for one it does not, such as a fork's commit GitHub serves
        under the parent repository's name."""
        branch = str(self._json(["gh", "api", f"repos/{e.source}"]).get("default_branch") or "")
        if not branch:
            raise UpstreamError(f"gh api repos/{e.source}: no default branch in the answer")
        return str(self._json(["gh", "api", f"repos/{e.source}/compare/{branch}...{sha}"]).get("status") or "")

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
        r.update(latest=latest, latest_sha=up.get("sha", ""), label=up.get("label", ""), link=up.get("link", ""))
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
        if e.pin_kind == "sha":  # M4: only a commit of the default branch, never a fork's served under this name
            try:
                where = up.on_default_branch(e, new)
            except UpstreamError as err:
                ui.err(f"{e.id}: could not check that {new[:12]} is on {e.source}'s default branch ({err}); nothing "
                       f"changed")
                rc = 1
                continue
            if where not in ("behind", "identical"):
                seen = where or "no status"
                ui.err(f"{e.id}: {new[:12]} is not on {e.source}'s default branch (GitHub compare: {seen}). GitHub "
                       f"serves a fork's commits under the parent's name; pin a commit of the default branch. Nothing "
                       f"changed")
                rc = 1
                continue
        r = _review(e, new, up, info.get("label", ""), info.get("link", ""))
        print("\n".join(detail_lines(r)))
        if (r.get("detail") or {}).get("error"):  # M4: no review, no change
            ui.err(f"{e.id}: the review above could not be read, so nothing changed; try again, or check the compare "
                   f"link yourself and name the commit with --to")
            rc = 1
            continue
        f.write_text(bump(f.read_text(), e, new))
        audit.record("ext.update", e.id, f"{e.pin} → {new} in {f.relative_to(home)}", "ok", why=a.why.strip(),
                     layer="team")
        print(f"changed {f.relative_to(home)}: {e.name} {_short(e.pin)} → {_short(new)}")
    if done_ and rc == 0:
        print("next: commit it (dev commit --why …) and open a PR; the Security engineer reviews dependency changes")
    return rc


def _developer_only(verb: str, a, what: str = "changes what runs in every session on this Mac") -> bool:
    """L2: what changes the extensions on this Mac outside devkit's configuration (the vendor CLIs, the project's
    settings) is the developer's, in a terminal (core.set_by_developer), as an overlay change is through
    core.config_set. False, after the refusal is printed and audited, for anyone else."""
    from . import audit, ui
    try:
        ok = core.set_by_developer()
    except Exception:
        ok = False  # a check that cannot decide refuses (fail closed)
    if not ok:
        audit.record(f"ext.{verb}", str(a.name or ""), "not the developer's terminal", "refused",
                     why=str(a.why or "").strip())
        ui.err(f"dev ext {verb} {what}, which only the developer does, in a terminal (spec P-fym583l7sg1, K1). Ask "
               f"them, with the command and why.")
    return ok


def _apply_update(a) -> int:
    from . import audit, ui
    if not _developer_only("apply", a):
        return 1
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
    up, run, rc, reviewed = upstream(), runner(), 0, {}
    for row in outdated(obs, up, observed=observe()):
        reviewed[row["id"]] = row
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
        now = observe().get(e.id) or {}
        row = reviewed.get(e.id) or {}
        want = set()
        if row.get("status") in ("outdated", "current"):
            want = {row.get("latest"), row.get("latest_sha")} - {"", None}
        if not want & {now.get("version"), now.get("sha")}:  # L3: accept only what the review above showed
            ui.err(f"{e.name} ({e.agent}) is now {_vs(now)}, which is not what was reviewed above "
                   f"({' '.join(sorted(want)) or 'nothing could be reviewed'}): not recorded as accepted, so it shows "
                   f"as drift. Check it with dev ext outdated {e.name}; dev install accepts it")
            rc = 1
            continue
        if not record_lock(e.agent, by="dev ext update --apply", only={e.id}):
            ui.err(f"updated {e.name} ({e.agent}): now {_vs(now)}, but not recorded as accepted: the lock or the "
                   f"acceptance list is busy or does not parse ({core.tilde(accepted_file())}), so it shows as drift. "
                   f"Run dev install in a terminal to accept it")
            rc = 1
            continue
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
    for item in (user_layer() if user is None else user).get("add") or []:  # {}: an overlay that failed validation
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
            ours = sorted(set(ids) & {e.id for e in catalog(t, {}, None)})
            if ours:
                ui.err(f"{', '.join(ours)} is in devkit's defaults already: dev ext enable or disable it, or change "
                       f"the team entry on a branch of the devkit checkout (dev ext add --team)")
                return 2
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
    if not _developer_only(a.ext_cmd, a):
        return 1
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
    rows = status(cat, observe(), read_lock(), project_plugins(cwd), retired_ids(t), managed_plugins())
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


def cmd_ack(a) -> int:
    """`dev ext ack`: the developer confirms the acceptances listed since the last one were theirs (F1-R4 part 1).
    Only in the developer's terminal (the guard refuses it to agents as well), and audited as `ext.ack`."""
    from . import audit, state, ui
    a.name = getattr(a, "name", None) or "acceptances"
    a.why = getattr(a, "why", None) or ""
    if not _developer_only("ack", a, "confirms that the extensions accepted since the last session were the "
                                      "developer's"):
        return 1
    items = pending_acceptances()
    if not items:
        print("Nothing to acknowledge: no extension was accepted since the last acknowledgement.")
        return 0
    done = {(p["id"], p.get("at"), p.get("record")) for p in items}
    tty, parents = _tty(), _parents()
    where = f"tty {tty or 'none'}; parents: {' ← '.join(parents) or 'unknown'}"

    def drop(doc):
        doc["pending"] = [p for p in doc.get("pending") or [] if isinstance(p, dict) and
                          (p.get("id"), p.get("at"), p.get("record")) not in done]
        # the next session start shows this acknowledgement once, with where it came from (an agent's terminal can
        # pass the developer check too)
        doc["last_ack"] = {"at": int(time.time()), "count": len(items), "tty": tty, "parents": parents,
                           "shown": False}
    try:
        state.update(accepted_file(), drop, indent=1)
    except state.StateBusy:
        ui.err("The acceptance list is busy (another dev command writes it). Run `dev ext ack` again in a moment.")
        return 1
    for p in items:
        audit.record("ext.ack", p["id"], f"acknowledged: {p.get('what') or p['id']}; {where}", "ok",
                     id=p.get("record") or "")
        print(f"  ✓ {p.get('what') or p['id']}")
    print(f"{len(items)} acceptance(s) acknowledged; the audit trail keeps them (`dev audit --action ext`).")
    return 0


def cmd_ext(a) -> int:
    return {"ls": cmd_ls, "outdated": cmd_outdated, "update": cmd_update, "add": cmd_change, "remove": cmd_change,
            "enable": cmd_change, "disable": cmd_change, "ack": cmd_ack}[a.ext_cmd](a)


def register(add):
    """Wire `dev ext` into the parser. `add(name, fn, help)` returns an argparse subparser. No abbreviated options:
    the guard reads `--apply` and the other words exactly."""
    p = add("ext", cmd_ext, "Third-party plugins, skills and MCP servers for Claude Code and Codex: ls, outdated, "
                            "update, ack")
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
    sp.add_parser("ack", allow_abbrev=False,
                  help="confirm that the extensions accepted since your last session were yours (the developer, in a "
                       "terminal; audited)")
