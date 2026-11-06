# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev crew`: devkit's crew of specialist agents, from one roster (config/crew.json).

One source, every surface:
- Claude Code (terminal and Claude Desktop's Code tab): plugin agents in plugin/agents/<id>.md, written by
  `dev crew sync` and spawned as devkit:<id>.
- Codex (CLI and the ChatGPT app): agent roles in ~/.codex/agents/<id>.toml, built by codex_roles().
- Claude Desktop chat and Cowork: the devkit_crew tool of the MCP extension (`dev crew --json --latest`).

Morpheus (`main: true`) is the main session's coordinating role, not a subagent: he gets no agent file.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import core, ui

ROSTER = core.DEVKIT_HOME / "config" / "crew.json"
AGENTS_DIR = core.DEVKIT_HOME / "plugin" / "agents"
CODEX_CONF = core.DEVKIT_HOME / "config" / "codex" / "config.json"
MARK = "<!-- generated from config/crew.json by dev crew sync -->"

# tools level -> the Claude Code agent's `tools` (None: no restriction, the key is omitted)
TOOLS = {"all": None, "build": "Read, Write, Edit, Grep, Glob, Bash", "review": "Read, Grep, Glob, Bash",
         "read": "Read, Grep, Glob"}
CLAUDE_MODEL = {"strong": "inherit", "standard": "inherit", "fast": "haiku"}
CLAUDE_EFFORT = {"strong": "high", "fast": "low"}  # standard: the session's default effort, like Codex
REQUIRED = ("id", "name", "role", "summary", "personality", "use_when", "skills", "tools", "tier", "returns")
OPTIONAL = ("main", "not_for", "aliases", "keywords")  # keywords: routing words (dev crew route)
TEXT_FIELDS = ("id", "name", "role", "summary", "personality", "returns")
LIST_FIELDS = ("use_when", "not_for", "skills", "aliases", "keywords")
LEGACY_ALIASES = {"sparks": ["scout"]}  # agent names older briefs still use; a member's own `aliases` field wins
ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_PREFIXED = re.compile(r"^[a-z0-9][a-z0-9_-]*:(\S+)$")  # devkit:neo -> neo (not "Neo: build the parser")


class RosterError(ValueError):
    def __init__(self, errors, path=None):
        self.errors = list(errors)
        where = _rel(path) if path else "config/crew.json"
        super().__init__(f"{where}: " + "; ".join(self.errors))


# --------------------------------------------------------------------------- roster

def known_skills(home=None):
    """-> (bare skill names, plugin names, devkit's own skills) that devkit can provide to an agent."""
    home = Path(home or core.DEVKIT_HOME)
    local = {f.parent.name for f in (home / "plugin" / "skills").glob("*/SKILL.md")}
    names = set(local)
    for src in (core.read_json(home / "config" / "skills.json", {}) or {}).get("sources") or []:
        names |= set(src.get("skills") or {})
    plugins = {str(p).split("@")[0] for p in
               (core.read_json(home / "config" / "claude" / "plugins.json", {}) or {}).get("plugins") or []}
    return names, plugins, local


def skill_known(name: str, known) -> bool:
    names, plugins, local = known
    if ":" in name:  # a plugin skill: superpowers:test-driven-development, devkit:testing
        plugin, skill = name.split(":", 1)
        return plugin in plugins and (plugin != "devkit" or skill in local)
    return name in names


def _norm(s) -> str:
    s = " ".join(str(s or "").split()).lower()
    m = _PREFIXED.match(s)
    if m:
        s = m.group(1)
    return s[4:] if s.startswith("the ") else s


def aliases(m: dict) -> list:
    return list(m["aliases"]) if isinstance(m.get("aliases"), list) else list(LEGACY_ALIASES.get(m.get("id"), []))


def _keys(m: dict) -> set:
    """Every name a member answers to: id, name, name without "The ", aliases."""
    keys = {_norm(x) for x in (m.get("id"), m.get("name")) if isinstance(x, str) and x.strip()}
    return keys | {_norm(a) for a in aliases(m) if isinstance(a, str)}


def check(data, known=None):
    """-> (errors, warnings) for a roster. Unknown skills are warnings: the skill may come from elsewhere."""
    if not isinstance(data, dict) or not isinstance(data.get("members"), list) or not data["members"]:
        return ["the roster needs a non-empty `members` list"], []
    errors, warnings = [], []
    if not isinstance(data.get("language_rule"), str) or not data["language_rule"].strip():
        errors.append("`language_rule` is missing")
    known = known or known_skills()
    ids, owner, mains = set(), {}, []
    for i, m in enumerate(data["members"], 1):
        if not isinstance(m, dict):
            errors.append(f"member {i} is not an object")
            continue
        mid = m.get("id") if isinstance(m.get("id"), str) and m.get("id") else None
        where = f"`{mid}`" if mid else f"member {i}"
        missing = [k for k in REQUIRED if k not in m or m[k] is None or (k in TEXT_FIELDS and m[k] == "")
                   or (k == "use_when" and m[k] == [])]
        if missing:
            errors.append(f"{where}: missing {', '.join(missing)}")
        for k in TEXT_FIELDS:
            if k in m and m[k] is not None and not isinstance(m[k], str):
                errors.append(f"{where}: `{k}` must be text")
        for k in LIST_FIELDS:
            if k in m and m[k] is not None and not (isinstance(m[k], list) and
                                                     all(isinstance(x, str) and x.strip() for x in m[k])):
                errors.append(f"{where}: `{k}` must be a list of text")
        if mid and not ID_RE.match(mid):
            errors.append(f"{where}: the id must be kebab-case (lowercase words joined by -)")
        if mid in ids:
            errors.append(f"{where}: duplicate id")
        ids.add(mid)
        if "tools" in m and m["tools"] not in TOOLS:
            errors.append(f"{where}: tools must be one of {', '.join(TOOLS)}, not {m['tools']!r}")
        if "tier" in m and m["tier"] not in CLAUDE_MODEL:
            errors.append(f"{where}: tier must be one of {', '.join(CLAUDE_MODEL)}, not {m['tier']!r}")
        if m.get("main"):
            mains.append(mid or where)
        for a in m.get("aliases") or [] if isinstance(m.get("aliases"), list) else []:
            if isinstance(a, str) and not ID_RE.match(a):
                errors.append(f"{where}: alias {a!r} must be kebab-case")
        for key in _keys(m):
            if key in owner and owner[key] != (mid or where):
                errors.append(f"{where}: {key!r} already names {owner[key]}")
            owner.setdefault(key, mid or where)
        for k in sorted(set(m) - set(REQUIRED) - set(OPTIONAL)):
            warnings.append(f"{where}: unknown field `{k}`")
        for s in m.get("skills") or [] if isinstance(m.get("skills"), list) else []:
            if isinstance(s, str) and not skill_known(s, known):
                warnings.append(f"{where}: unknown skill `{s}` (not in plugin/skills, config/skills.json or a "
                                "plugin of config/claude/plugins.json)")
    if len(mains) > 1:
        errors.append("only one member can be `main`, not " + ", ".join(mains))
    return errors, warnings


_CACHE = {}


def load(path=None) -> dict:
    """The validated roster; `warnings` lists what is suspicious but allowed. Raises RosterError."""
    p = Path(path or ROSTER)
    try:
        st = p.stat()
    except OSError:
        raise RosterError(["the roster file is missing"], p) from None
    key = (str(p), st.st_mtime_ns, st.st_size)
    if key in _CACHE:
        return _CACHE[key]
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError) as e:
        raise RosterError([f"not valid JSON ({e})"], p) from None
    errors, warnings = check(data)
    if errors:
        raise RosterError(errors, p)
    data["warnings"] = warnings
    _CACHE.clear()
    _CACHE[key] = data
    return data


def members(roster=None, include_main=True) -> list:
    roster = roster or load()
    return [m for m in roster["members"] if include_main or not m.get("main")]


def member(name_or_id, roster=None):
    """A member by id, name, name without "The ", alias or agent type (devkit:neo), any case. None if unknown."""
    want = _norm(name_or_id)
    if not want:
        return None
    return next((m for m in members(roster) if want in _keys(m)), None)


def _for_label(label, roster=None):
    """The member behind a lane or step label: "Neo", "Neo: build the parser", "devkit:neo"."""
    label = str(label or "").strip()
    return member(label, roster) or (member(label.split(":")[0], roster) if ":" in label else None)


def _for_lane(lane: dict, roster=None):
    return (member(lane.get("persona"), roster) or member(lane.get("type"), roster)
            or _for_label(lane.get("label"), roster))


# --------------------------------------------------------------------------- rendering

def _lc(s: str) -> str:
    """"Senior software engineer" -> "senior software engineer"; "AI and ML engineer" stays."""
    return s[:1].lower() + s[1:] if len(s) > 1 and s[1].islower() else s


def _sentence(s: str) -> str:
    s = s.strip()
    return s if s.endswith((".", "!", "?")) else s + "."


def _display(m, alias=None) -> str:
    return f"{m['name']} (alias: {alias})" if alias else m["name"]


def _shell(m) -> bool:
    return m["tools"] in ("all", "build", "review")


def description(m: dict, alias=None) -> str:
    """What Claude Code and Codex route on: who, what, and when to send work (and when not)."""
    d = f"{_display(m, alias)}, {_lc(m['role'])}. {_sentence(m['summary'])} Use when: {'; '.join(m['use_when'])}."
    if m.get("not_for"):
        d += f" Not for: {'; '.join(m['not_for'])}."
    return d


def status_header(m: dict, project="<project>", branch="<branch>", task="<task>") -> str:
    return f"**[Firefly> DevKit · {m['name']}]** {project} · `{branch}` · {task}"


def report_block(m: dict, **header) -> list:
    return [status_header(m, **header),
            "- **Done:** <what you did or found, in one sentence>",
            "- **Check:** <✓ | ⚠ | ✗> <the evidence: tests run, command output>",
            "- **Next:** <the next step, and who takes it>"]


def body(m: dict, roster=None, alias=None) -> str:
    """The agent's instructions: the same text for the Claude Code agent and the Codex role."""
    roster = roster or load()
    shell, tools = _shell(m), m["tools"]
    out = [f"You are {_display(m, alias)}, the crew's {_lc(m['role'])}. {_sentence(m['personality'])} Let that "
           "shape your judgment and tone, always professionally: no role-play and no film quotes.", "",
           "## Mission", "", _sentence(m["summary"]), "",
           "## When Morpheus sends work to you", ""]
    out += [f"- {x}" for x in m["use_when"]] + [""]
    out += [("Not yours: " + "; ".join(m["not_for"]) if m.get("not_for") else "Not yours: anything outside your brief")
            + ". Say so in your report and leave it to Morpheus.", "", "## Skills to load first", ""]
    if m["skills"]:
        out += ["Load each one with the Skill tool (it may list a plugin skill with its prefix, such as "
                "`devkit:testing`). Without the Skill tool, or when it does not list one, read the skill's SKILL.md.",
                ""] + [f"- `{s}`" for s in m["skills"]] + [""]
    else:
        out += ["None. Load a skill only when your brief names one.", ""]
    out += ["## How you work", ""]
    if shell:
        out += ["- Stay inside the files and the scope your brief names. Before you go beyond them, ask Morpheus: "
                '`dev crew ask "<question>"`. Any crew member may ask anything; reply to a message with '
                '`dev crew answer <id> "<text>"`.',
                '- Record each finished step: `dev progress step "<what finished>" --check "<evidence>"`. devkit '
                "routes it to your own lane automatically.",
                "- Follow the global rules: tests that can fail for a real bug; `dev test` (the failed tests first, "
                "then what a change can break) instead of whole-suite reruns; no commits unless your brief says so; "
                "the guard's verdicts stand, so do it the way the guard says and never work around it."]
    else:
        out += ["- Stay inside the files and the scope your brief names. You have no shell, so `dev progress` and "
                "`dev crew ask` are not available to you: put your progress in your report and your questions under "
                '`Needs from Morpheus:`.']
    if tools == "review":
        out.append("- You are review-only: you read and run commands such as tests and scans, but you never edit, "
                   "create or delete files, not even to fix what you find.")
    elif tools == "read":
        out.append("- You are read-only: you never edit, create or delete files, and you never commit. Cite "
                   '`path:line` for every fact, mark what you inferred, and say `not found` rather than guess.')
    out += ["- You cannot launch other agents. When you need another specialist, say so in your report or ask "
            "Morpheus.", "",
            "## Your report", "",
            "Your final message starts with your own status block; nothing comes before it:", ""]
    out += ["```markdown"] + report_block(m) + ["```", "", f"Then deliver: {_sentence(m['returns'])}", "",
                              'End with `Needs from Morpheus:` and the decisions or questions for Morpheus, or `nothing`.',
                              "", "## Language", "", _sentence(roster["language_rule"])]
    return "\n".join(out)


def render_claude(m: dict, roster=None, alias=None) -> str:
    """plugin/agents/<id>.md for Claude Code (spawned as devkit:<id>)."""
    roster = roster or load()
    if m.get("main"):
        raise ValueError(f"{m['name']} is the main session's role, not a subagent: he has no agent file")
    fm = ["---", f"name: {alias or m['id']}",
          "description: " + json.dumps(description(m, alias), ensure_ascii=False)]  # quoted: it contains ": "
    if TOOLS[m["tools"]]:
        fm.append(f"tools: {TOOLS[m['tools']]}")
    fm.append(f"model: {CLAUDE_MODEL[m['tier']]}")
    if m["tier"] in CLAUDE_EFFORT:
        fm.append(f"effort: {CLAUDE_EFFORT[m['tier']]}")
    if m["tier"] == "fast":
        fm.append("omitClaudeMd: true")  # mechanical lookups: the body carries every rule they need
    return "\n".join(fm + ["---", MARK, body(m, roster, alias)]) + "\n"


def codex_role(m: dict, roster=None, codex=None, alias=None) -> dict:
    """~/.codex/agents/<id>.toml as a dict, in the key order setup.write_codex_agents writes. The model follows the
    tier: fast = the `fast` profile's model and effort, strong = the `deep` profile's model at high effort,
    standard = the session's (no keys)."""
    roster = roster or load()
    if m.get("main"):
        raise ValueError(f"{m['name']} is the main session's role, not a subagent: he has no Codex role")
    profiles = ((core.read_json(CODEX_CONF, {}) or {}) if codex is None else codex).get("profiles") or {}
    role = {"name": alias or m["id"], "description": description(m, alias),
            "developer_instructions": body(m, roster, alias)}
    if m["tier"] == "fast":
        fast = profiles.get("fast") or {}
        role.update({k: fast[k] for k in ("model", "model_reasoning_effort") if fast.get(k)})
    elif m["tier"] == "strong":
        if (profiles.get("deep") or {}).get("model"):
            role["model"] = profiles["deep"]["model"]
        role["model_reasoning_effort"] = "high"
    if m["tools"] == "read":  # no shell needed: Codex's read-only sandbox
        role["sandbox_mode"] = "read-only"
    # review members keep a shell (tests, scans, dev progress, dev crew ask); devkit's guard refuses their edits
    return role


def codex_roles(roster=None, codex=None) -> dict:
    """{role name: role} for every subagent and alias (scout -> Sparks), for ~/.codex/agents."""
    roster = roster or load()
    out = {}
    for m in members(roster, include_main=False):
        out[m["id"]] = codex_role(m, roster, codex)
        for a in aliases(m):
            out[a] = codex_role(m, roster, codex, alias=a)
    return out


# --------------------------------------------------------------------------- dev crew sync

def agent_files(roster=None) -> dict:
    """{file name: text} of every generated Claude Code agent, aliases included."""
    roster = roster or load()
    out = {}
    for m in members(roster, include_main=False):
        out[f"{m['id']}.md"] = render_claude(m, roster)
        for a in aliases(m):
            out[f"{a}.md"] = render_claude(m, roster, alias=a)
    return dict(sorted(out.items()))


def is_generated(text: str) -> bool:
    """True when the body's first line is the generated mark (only such files are ever removed)."""
    if not text.startswith("---\n"):
        return False
    end = text.find("\n---\n", 3)
    return end != -1 and text[end + 5:].lstrip("\n").split("\n", 1)[0].strip() == MARK


def _rel(path) -> str:
    try:
        return str(Path(path).resolve().relative_to(core.DEVKIT_HOME.resolve()))
    except ValueError:
        return str(path)


def sync(check: bool = False, roster=None, agents_dir=None) -> list:
    """Write plugin/agents/<id>.md for every subagent and remove generated files no longer in the roster.
    -> what changed (or, with check=True, what differs) as "new|update|remove <path>"."""
    roster = roster or load()
    d = Path(agents_dir or AGENTS_DIR)
    want = agent_files(roster)
    out = []
    for name, text in want.items():
        f = d / name
        cur = f.read_text() if f.exists() else None
        if cur == text:
            continue
        out.append(f"{'new' if cur is None else 'update'} {_rel(f)}")
        if not check:
            core.write_if_changed(f, text)
    for f in sorted(d.glob("*.md")) if d.exists() else []:
        if f.name in want:
            continue
        try:
            generated = is_generated(f.read_text())
        except OSError:
            continue
        if generated:  # never a file someone wrote by hand
            out.append(f"remove {_rel(f)}")
            if not check:
                f.unlink()
    return out


# --------------------------------------------------------------------------- live state (progress runs)

def live_run(latest: bool = False):
    """This session's progress run, or with latest=True the most recent running run on this Mac."""
    from . import progress
    if latest:
        return next((r for r in progress.active_runs() if r.get("state") == "running"), None)
    agent, sid = progress.current_session()
    return progress.load_run(agent, sid)


def open_messages(run: dict) -> list:
    """Messages not delivered yet, and questions nobody has answered (an answer is closed once delivered)."""
    return [m for m in (run or {}).get("messages") or []
            if not m.get("delivered") or (not m.get("answered") and not m.get("reply_to"))]


def run_view(run: dict, roster=None, now=None):
    """A run's crew state: the mode and its approvals, who works now (lanes), which plan steps are assigned to whom,
    the open messages and the decisions waiting for the developer. None without a run."""
    from . import progress
    if not run:
        return None
    roster = roster or load()
    run = progress.normalize(run)
    b = progress.brief(run, now)
    raw = list((run.get("agents") or {}).items())  # brief() lists the lanes in this order
    lanes = []
    for (aid, lane), a in zip(raw, b.get("agents") or []):
        m = _for_lane(lane, roster)
        lanes.append({"member": m["name"] if m else (lane.get("persona") or ""), "id": m["id"] if m else "",
                      "label": a.get("label"), "state": a.get("state"), "min": a.get("min"),
                      "scope": lane.get("scope") or []})
    names = {aid: lane.get("persona") or lane.get("label") or "subagent" for aid, lane in raw}
    assigned = []
    for i, s in enumerate(run.get("plan") or [], 1):
        if not s.get("label"):
            continue
        m = _for_label(s["label"], roster)
        owner = s.get("owner") or "main"
        assigned.append({"step": i, "name": s.get("name"), "to": s["label"], "member": m["name"] if m else s["label"],
                         "scope": s.get("scope") or [], "state": s.get("state"),
                         "lane": names.get(owner, "") if owner != "main" else ""})
    return {"goal": b.get("goal"), "project": run.get("project"), "state": b.get("state"),
            "headline": b.get("headline"), "eta_min": b.get("eta_min"), "mode": progress.mode_of(run),
            "approved": list(run.get("approved") or []), "lanes": lanes, "assigned": assigned,
            "messages": [_msg(m) for m in open_messages(run)],
            "decisions": [_decision(d) for d in progress.pending_decisions(run)], "ready": ready_view(run, roster)}


def ready_view(run: dict, roster=None) -> dict:
    """dispatch.ready() as plain data: what Morpheus can launch now, what is queued or waiting and why, who is busy."""
    from . import dispatch, progress
    r = dispatch.ready(run)
    step = lambda s: {"step": s["name"], "member": s.get("label") or "",  # noqa: E731
                      "scope": s.get("scope") or [], "requested_by": s.get("requested_by") or ""}
    return {"free": r["free"], "max_parallel": progress.cfg()["max_parallel"],
            "launch": [dict(step(s), launch=dispatch.launch_line(s)) for s in r["launch"]],
            "queued": [dict(step(s), launch=dispatch.launch_line(s)) for s in r["queued"]],
            "waiting": [dict(step(s), why=why) for s, why in r["waiting"]],
            "busy": [{"member": (_for_lane(lane, roster) or {}).get("name") or lane.get("persona") or "",
                      "label": lane.get("label") or "", "scope": lane.get("scope") or []} for _, lane in r["busy"]]}


def _when(ts) -> str:
    return time.strftime("%H:%M", time.localtime(ts)) if ts else ""


def _msg(m: dict) -> dict:
    out = {k: m.get(k) for k in ("id", "from_name", "to_name", "text", "delivered", "answered", "reply_to")}
    out["time"] = _when(m.get("ts"))
    return out


def _decision(d: dict) -> dict:
    out = {k: d.get(k) for k in ("id", "from_name", "question", "options", "recommend", "irreversible", "state",
                                 "answer", "by", "overrides", "note")}
    out["time"] = _when(d.get("ts"))
    return out


def _option(d: dict, answer: str) -> str:
    """"A" -> "A: rewrite the parser" when the options spell it out."""
    a = (answer or "").strip()
    return next((o for o in d.get("options") or [] if a and o.lower().startswith(a.lower()) and len(o) > len(a)), a)


# --------------------------------------------------------------------------- text

def _mins(x) -> str:
    from . import progress
    return progress.fmt_min(x or 0)


def roster_text(active=None, roster=None) -> str:
    """The roster table for `dev crew`. active: lanes ({label, state, min}, from progress.brief(run)["agents"] or
    run_view) to show who is working now."""
    roster = roster or load()
    busy = set()
    for lane in active or []:
        m = member(lane.get("member"), roster) or _for_label(lane.get("label"), roster)
        if m and lane.get("state") == "running":
            busy.add(m["id"])
    rows = []
    for m in members(roster):
        when = ("main session: " if m.get("main") else "") + "; ".join(m["use_when"])
        rows.append([(ui.ACTIVE + " " if m["id"] in busy else "  ") + m["id"], m["name"], m["role"], when,
                     m["tier"], m["tools"]])
    out = [ui.table(rows, ["  ID", "NAME", "ROLE", "WHEN TO USE", "TIER", "TOOLS"], max_widths={2: 32, 3: 44})]
    if active:
        out.append(ui.section("Working now"))
        for lane in active:
            m = member(lane.get("member"), roster) or _for_label(lane.get("label"), roster)
            dot = ui.ACTIVE if lane.get("state") == "running" else ui.IDLE
            scope = f" · scope {', '.join(lane['scope'])}" if lane.get("scope") else ""
            out.append(f"  {dot} {(m['name'] if m else lane.get('label') or 'subagent'):<18} "
                       f"{lane.get('state') or '-':<8} {_mins(lane.get('min')):>6}  {lane.get('label') or ''}{scope}")
    return "\n".join(out)


def mode_line(view: dict) -> str:
    approved = ", ".join(view.get("approved") or []) or "nothing"
    if view.get("mode") == "auto":
        return (f"  Mode auto · the crew goes on with recommended options and records them · pre-approved "
                f"irreversible actions: {approved}")
    return "  Mode guided · decisions that are the developer's wait for the developer"


def run_text(view: dict) -> str:
    """The mode, assigned steps, open messages and pending decisions of a run_view."""
    out = [mode_line(view)]
    ready = view.get("ready") or {}
    if ready.get("launch"):
        out.append(f"  Ready to launch: {', '.join(s['member'] + ' (' + s['step'] + ')' for s in ready['launch'])}"
                   " · dev crew next")
    if view.get("decisions"):
        out.append(ui.section("Decisions waiting for the developer"))
        for d in view["decisions"]:
            extra = [f"options {' | '.join(d['options'])}" if d.get("options") else "",
                     f"recommends {d['recommend']}" if d.get("recommend") else "",
                     f"irreversible: {d['irreversible']}" if d.get("irreversible") else "", d.get("note") or ""]
            out.append(f"  {d['id']} {d.get('from_name') or '?'}: {d.get('question')}"
                       + "".join(f" · {x}" for x in extra if x))
        out.append(ui.dim('  ask the developer, then: dev crew decide <id> "<answer>"'))
    if view.get("assigned"):
        out.append(ui.section("Assigned steps"))
        for s in view["assigned"]:
            where = f"lane {s['lane']}" if s.get("lane") else ("waiting for its agent" if s.get("state") != "done"
                                                              else "")
            scope = f" · scope {', '.join(s['scope'])}" if s.get("scope") else ""
            out.append(f"  {s['step']}. {s['name']} → {s['member']} · {s.get('state') or '-'}"
                       + (f" · {where}" if where else "") + scope)
    if view.get("messages"):
        out.append(ui.section("Open messages"))
        for m in view["messages"]:
            state = "not delivered" if not m.get("delivered") else "waiting for an answer"
            out.append(f"  {m['id']} {m.get('from_name') or '?'} → {m.get('to_name') or '?'} · {state}"
                       + (f" · answer to {m['reply_to']}" if m.get("reply_to") else "") + f": {m.get('text')}")
    return "\n".join(out)


def _spawn_lines(m: dict) -> list:
    return [f"Claude Code  Agent tool · subagent_type: devkit:{m['id']} · description: \"{m['name']}: <the task in "
            "3-5 words>\"",
            f"Codex        spawn_agent · agent type: {m['id']}"]


def show_text(m: dict, roster=None) -> str:
    roster = roster or load()
    tier = {"strong": "the session's model at high effort", "standard": "the session's model and effort",
            "fast": "the small fast model at low effort"}[m["tier"]]
    tools = {"all": "read, write, run commands and the web", "build": "read, write and run commands (no web)",
             "review": "read and run commands, never edit", "read": "read and search only"}[m["tools"]]
    out = [ui.section(f"{m['name']} · {m['role']}"), ui.kv("id", m["id"] + (f"  (aliases: {', '.join(aliases(m))})"
                                                                            if aliases(m) else "")),
           ui.kv("summary", m["summary"]), ui.kv("personality", m["personality"]),
           ui.kv("use when", "; ".join(m["use_when"]))]
    if m.get("not_for"):
        out.append(ui.kv("not for", "; ".join(m["not_for"])))
    if m.get("keywords"):
        out.append(ui.kv("keywords", ", ".join(m["keywords"])))
    out += [ui.kv("skills", ", ".join(m["skills"]) or "none"), ui.kv("returns", m["returns"]),
            ui.kv("tier", f"{m['tier']}: {tier}"), ui.kv("tools", f"{m['tools']}: {tools}")]
    if m.get("main"):
        out.append(ui.kv("spawn", "the main session's role, not a subagent (no agent file)"))
        return "\n".join(out)
    c, x = codex_role(m, roster), CLAUDE_MODEL[m["tier"]]
    out += [ui.kv("Claude Code", f"devkit:{m['id']} · model {x}" + (f" · effort {CLAUDE_EFFORT[m['tier']]}"
                                                                     if m["tier"] in CLAUDE_EFFORT else "")
                  + f" · tools {TOOLS[m['tools']] or 'all'} · {_rel(AGENTS_DIR / (m['id'] + '.md'))}"),
            ui.kv("Codex", f"agent type {m['id']} · model {c.get('model', 'the session’s')}"
                  + (f" · effort {c['model_reasoning_effort']}" if c.get("model_reasoning_effort") else "")
                  + (f" · sandbox {c['sandbox_mode']}" if c.get("sandbox_mode") else "")
                  + f" · ~/.codex/agents/{m['id']}.toml"),
            ui.kv("brief", f'dev crew brief {m["id"]} "<task>" --scope "<globs>"')]
    return "\n".join(out)


def _dq(s) -> str:
    """A double-quoted shell word."""
    return '"' + re.sub(r'(["\\$`])', r"\\\1", str(s)) + '"'


def brief_text(m: dict, task: str, scope: str = "", step=None, roster=None, cwd=None) -> str:
    """What `dev crew brief` prints: the spawn lines, the lane link and the brief to paste as the prompt."""
    roster = roster or load()
    if m.get("main"):
        raise ValueError(f"{m['name']} is the main session's role, not a subagent: brief a specialist instead")
    cwd = cwd or os.getcwd()
    top = core.gitout(cwd, "rev-parse", "--show-toplevel")
    project = Path(top).name if top else "<project>"
    branch = (core.gitout(cwd, "rev-parse", "--abbrev-ref", "HEAD") if top else "") or "<branch>"
    globs = [g.strip() for g in (scope or "").split(",") if g.strip()]
    head = [ui.section(f"Brief for {m['name']} ({m['role']})")] + [f"  {ln}" for ln in _spawn_lines(m)]
    if step is not None and str(step).strip():
        w = str(step).strip()
        head.append(f"  Link the lane dev progress assign {w if w.isdigit() else _dq(w)} --to {_dq(m['name'])}"
                    + (f" --scope {_dq(','.join(globs))}" if globs else "") + "   (run it before you spawn)")
    head.append(ui.dim("  ── paste everything below as the prompt ──"))
    b = [f"You are {m['name']}, the crew's {_lc(m['role'])} (agent devkit:{m['id']}). Follow your agent "
         "instructions; this brief adds the task.", "",
         "## Task", "", task.strip(), "", "## Scope", ""]
    if globs:
        b.append(f"You own only these files: {', '.join(f'`{g}`' for g in globs)}. Ask Morpheus before you change "
                 'anything else: `dev crew ask "may I change <file>? why: <reason>"`.')
    else:
        b.append("The files this task needs and nothing beyond them. Ask Morpheus before you widen it: "
                 '`dev crew ask "<question>"`.')
    b += ["", "## Skills to load first", ""]
    b.append(("Load each with the Skill tool, or read its SKILL.md when the Skill tool does not list it: "
              + ", ".join(f"`{s}`" for s in m["skills"]) + ".") if m["skills"] else "None.")
    b += ["", "## Return", "", _sentence(m["returns"]), "",
          "## Report format", "", "Your final message starts with this block; nothing comes before it:", ""]
    b += report_block(m, project=project, branch=branch, task="<the task, in a few words>")
    b += ["", 'Then what you deliver (above), then `Needs from Morpheus:` with your decisions or questions, or '
          '`nothing`.', "", "## Commands", ""]
    if not _shell(m):
        b.append("You have no shell, so Morpheus runs these for you: put your progress in your report and your "
                 'questions under `Needs from Morpheus:`.')
    b += ['- After each finished step: `dev progress step "<what finished>" --check "<evidence>"`',
          '- A question for Morpheus (or another crew member with `--to <name>`): `dev crew ask "<question>"`',
          '- Answer a message: `dev crew answer <id> "<text>"`']
    return "\n".join(head) + "\n\n" + "\n".join(b)


# --------------------------------------------------------------------------- JSON (dev crew --json, devkit_crew)

def as_json(roster=None, view=None) -> dict:
    roster = roster or load()
    out = []
    for m in members(roster):
        x = {k: m.get(k) for k in ("id", "name", "role", "main", "summary", "use_when", "not_for", "keywords",
                                   "skills", "tools", "tier", "returns")}
        x["aliases"] = aliases(m)
        if not m.get("main"):
            x["spawn"] = {"claude_code": f"devkit:{m['id']}", "codex": m["id"]}
        out.append({k: v for k, v in x.items() if v not in (None, [], False)})
    return {"members": out, "run": view, "warnings": roster.get("warnings") or []}


# --------------------------------------------------------------------------- CLI

def _from_agent():
    return os.environ.get("DEVKIT_FROM_AGENT") or None  # the hooks set it inside a subagent: its own lane


def _ask_decision(a, me) -> int:
    from . import progress
    if a.to:
        ui.err("a decision goes to the developer through Morpheus: drop --to")
        return 2
    d = progress.crew_decision(" ".join(a.text), options=a.options or "", recommend=a.recommend or "",
                               irreversible=a.irreversible or "", from_agent=me)
    if d["state"] == "decided":
        print(f"{ui.OK} auto mode: go ahead with {_option(d, d['answer'])} ({d['id']}, recorded; the developer can "
              "revisit it with dev crew decisions)")
        return 0
    print(f"{ui.WARN} {d['id']}: {d['question']}" + (f" · options {' | '.join(d['options'])}" if d["options"] else "")
          + (f" · recommends {d['recommend']}" if d["recommend"] else "")
          + (f" · irreversible: {d['irreversible']}" if d["irreversible"] else ""))
    if d.get("note"):
        print(ui.dim(f"  {d['note']}"))
    if me:
        print(f"waiting for the developer's decision {d['id']}: stop here and end your report with your status block "
              f"(Check: – waiting for {d['id']}; Needs from Morpheus: {d['id']}). Morpheus resumes you with the answer")
    else:
        print("ask the developer now (AskUserQuestion / request_user_input) with the decision template, then: "
              f"dev crew decide {d['id']} \"<answer>\"")
    return 0


def _decide(a, me) -> int:
    from . import progress
    if me:
        ui.err("only Morpheus records the developer's answers: stop and end your report with "
               f"\"Needs from Morpheus: {a.id}\"")
        return 2
    d = progress.crew_decide(a.id, " ".join(a.text))
    print(f"{ui.OK} {d['id']} decided by the {d['by']}: {_option(d, d['answer'])}"
          + (f" (overrides the automatic choice {d['overrides']})" if d.get("overrides") else ""))
    if d.get("from") and d["from"] != "main":
        print(f"resume {d.get('from_name') or 'the crew member'} with SendMessage / send_input and the answer "
              "(a message with it is queued for them)")
    return 0


def _decisions(a) -> int:
    from . import progress
    run = live_run()
    decs = [d for d in (run or {}).get("decisions") or [] if not a.pending or d.get("state") == "pending"]
    if not decs:
        print("  no pending decisions" if a.pending else "  no decisions in this session's run")
        return 0
    rows = []
    for d in decs:
        answer = _option(d, d.get("answer")) + (f" (was {d['overrides']})" if d.get("overrides") else "")
        by = "auto · review" if d.get("by") == "auto" else (d.get("by") or "")
        rows.append([d["id"], d.get("from_name") or "?", d.get("question") or "", d.get("state") or "",
                     answer or "-", by or "-"])
    print(ui.table(rows, ["ID", "FROM", "QUESTION", "STATE", "ANSWER", "BY"], max_widths={2: 56, 4: 36}))
    mode = progress.mode_of(run)
    print(ui.dim(f"  mode {mode} · override an automatic decision: dev crew decide <id> \"<other answer>\""))
    return 0


def _mode(a, me) -> int:
    from . import progress
    if not a.mode:
        if a.approve:
            ui.err("--approve goes with auto: dev crew mode auto --approve \"push, pr\"")
            return 2
        run = live_run()
        view = {"mode": progress.mode_of(run), "approved": list((run or {}).get("approved") or [])}
        print(mode_line(view) + ("" if run else "\n" + ui.dim("  no progress run in this session yet: the default")))
        print(ui.dim(f"  irreversible actions auto mode can pre-approve: {', '.join(progress.IRREVERSIBLE)}"))
        return 0
    if me:
        ui.err("only Morpheus sets the mode, on the developer's word: ask him with dev crew ask")
        return 2
    run = progress.set_mode(a.mode, approve=a.approve or "")
    print(f"{ui.OK} " + mode_line({"mode": run.get("mode"), "approved": run.get("approved")}).strip())
    if a.approve and a.mode == "guided":
        print(ui.dim("  approvals apply to auto mode only: none recorded"))
    return 0


def _split(csv) -> list:
    return [x.strip() for x in (csv or "").split(",") if x.strip()]


def _route(a) -> int:
    from . import dispatch
    task = " ".join(a.task or []).strip()
    if a.apply and not a.plan:
        ui.err("--apply goes with --plan: dev crew route --plan --apply")
        return 2
    if a.plan:
        if task:
            ui.err("--plan routes the plan's open steps: drop the task, or route it alone without --plan")
            return 2
        rows = dispatch.assign_plan(apply=a.apply)
        if not rows:
            print("  every open plan step already has a member")
            return 0
        for s, m, score, why in rows:
            print(f"  {s['name']} → {m['name']} ({score:g}) · {'; '.join(why)}")
        if a.apply:
            print(f"{ui.OK} assigned {len(rows)} step(s); who to launch now: dev crew next")
        else:
            print(ui.dim("  assign them: dev crew route --plan --apply"))
        return 0
    if not task:
        ui.err('say what the task is: dev crew route "<task>" [--files a,b]')
        return 2
    ranked = dispatch.route(task, _split(a.files))
    if not ranked:
        print(f"  no crew member fits: name one with dev crew delegate {_dq(task)} --to <name>")
        return 0
    print(ui.table([[i, m["name"], m["id"], f"{score:g}", "; ".join(why)] for i, (m, score, why) in
                    enumerate(ranked, 1)], ["#", "MEMBER", "ID", "SCORE", "WHY"], max_widths={4: 72}))
    best = ranked[0][0]
    print(f"  launch: dev crew brief {best['id']} {_dq(task)}")
    print(ui.dim(f"  or add it to the plan: dev crew delegate {_dq(task)} --to {best['id']}"))
    return 0


def _delegate(a, me) -> int:
    from . import dispatch
    r = dispatch.delegate(" ".join(a.task), to=a.to or "auto", files=_split(a.files), scope=a.scope or "",
                          after=a.after or "", est=a.estimate or "", from_agent=me)
    m, s = r["member"], r["step"]
    how = f" (routed: {r['routed'][0][1]:g} · {'; '.join(r['routed'][0][2])})" if r.get("routed") else ""
    print(f"{ui.OK} step “{s['name']}” for {m['name']}{how}"
          + (f" · after {', '.join(s['after'])}" if s.get("after") else "")
          + (f" · scope {', '.join(s['scope'])}" if s.get("scope") else ""))
    if r.get("lane"):
        print(f"in {m['name']}'s lane now (they got a message)")
    else:
        print(f"Morpheus launches {m['name']} when it is ready: dev crew next")
    return 0


def _decline(a, me) -> int:
    from . import dispatch
    s = dispatch.decline(" ".join(a.step), a.why or "", from_agent=me)
    print(f"{ui.OK} handed “{s['name']}” back to Morpheus: {a.why}")
    print(ui.dim(f"  Morpheus routes it again: dev crew route {_dq(s['name'])}"))
    return 0


def _next() -> int:
    from . import progress
    run = live_run()
    if not run:
        print('  no progress run in this session: dev progress start "<goal>" --plan "<step>; <step>"')
        return 0
    v, mode = ready_view(run), progress.mode_of(run)
    print(ui.section("Next" + (f" · {run.get('goal')}" if run.get("goal") else "")))
    approved = ", ".join(run.get("approved") or [])
    print(f"  {v['free']} of {v['max_parallel']} crew slots free · mode {mode}"
          + (f" · pre-approved: {approved}" if mode == "auto" and approved else ""))
    if v["launch"]:
        print(ui.section("Ready to launch"))
        for s in v["launch"]:
            print(f"  {s['member']}: {s['step']}\n    {s['launch']}")
    if v["queued"]:
        print(ui.section("Queued until a slot frees"))
        print("\n".join(f"  {s['member']}: {s['step']}" for s in v["queued"]))
    if v["waiting"]:
        print(ui.section("Waiting"))
        print("\n".join(f"  {s['member']}: {s['step']} · {s['why']}" for s in v["waiting"]))
    if v["busy"]:
        print(ui.section("Busy"))
        print("\n".join(f"  {ui.ACTIVE} {b['member'] or 'subagent'} · {b['label']}"
                        + (f" · scope {', '.join(b['scope'])}" if b["scope"] else "") for b in v["busy"]))
    if not (v["launch"] or v["queued"] or v["waiting"] or v["busy"]):
        print("  nothing assigned to the crew: dev crew route --plan, or dev crew delegate \"<task>\"")
    if mode == "auto" and v["launch"]:
        print("auto mode: launch all ready members now, in one message")
    return 0


def _messages(a, act) -> int:
    from . import progress
    me = _from_agent()
    try:
        if act == "route":
            return _route(a)
        if act == "delegate":
            return _delegate(a, me)
        if act == "decline":
            return _decline(a, me)
        if act == "next":
            return _next()
        if act == "mode":
            return _mode(a, me)
        if act == "decide":
            return _decide(a, me)
        if act == "decisions":
            return _decisions(a)
        if act == "ask" and a.decision:
            return _ask_decision(a, me)
        if act == "ask":
            m = progress.crew_ask(" ".join(a.text), to=a.to, from_agent=me)
            print(f"{ui.OK} {m['id']} to {m.get('to_name') or 'Morpheus'}: {m['text']}")
            print(ui.dim("  it reaches them with their next tool call or prompt; the answer reaches you the same way"))
        elif act == "answer":
            m = progress.crew_answer(a.id, " ".join(a.text), from_agent=me)
            print(f"{ui.OK} {m['id']} to {m.get('to_name') or '?'} (answer to {a.id}): {m['text']}")
        else:
            msgs = progress.crew_inbox(from_agent=me)
            if not msgs:
                print("  no new messages")
                return 0
            for m in msgs:
                print(f"  {m['id']} from {m.get('from_name') or '?'}: {m['text']}"
                      + (f" (answer to {m['reply_to']})" if m.get("reply_to") else ""))
            print(ui.dim('  answer with: dev crew answer <id> "<text>"'))
    except ValueError as e:
        ui.err(str(e))
        return 2
    return 0


def cmd_crew(a) -> int:
    act = getattr(a, "crew_cmd", None)
    if act in ("ask", "answer", "inbox", "decide", "decisions", "mode", "route", "delegate", "decline", "next"):
        return _messages(a, act)
    try:
        roster = load()
    except RosterError as e:
        ui.err(str(e))
        return 2
    if act == "sync":
        changes = sync(check=a.check, roster=roster)
        for w in roster["warnings"]:
            ui.warn(w)
        n = len(agent_files(roster))
        if a.check:
            if changes:
                print("\n".join(f"  {c}" for c in changes))
                print(ui.dim("  plugin/agents differs from config/crew.json: run dev crew sync"))
                return 1
            print(f"{ui.OK} plugin/agents matches config/crew.json ({n} agent files)")
            return 0
        for c in changes:
            print(f"{ui.OK} {c}")
        print(f"{ui.OK} {n} agent files match config/crew.json" + ("" if changes else " (nothing to change)"))
        if changes:
            print(ui.dim("  Claude Code and Codex load an installed copy of the plugin: dev install --only claude,codex"))
        return 0
    if act in ("show", "brief"):
        name = " ".join(a.name) if isinstance(a.name, list) else a.name
        m = member(name, roster)
        if not m:
            ui.err(f"no crew member {name!r}: dev crew lists them")
            return 2
        if act == "show":
            print(show_text(m, roster))
            return 0
        try:
            print(brief_text(m, " ".join(a.task), a.scope or "", a.step, roster))
        except ValueError as e:
            ui.err(str(e))
            return 2
        return 0
    view = run_view(live_run(latest=a.latest), roster)
    if a.json:
        print(json.dumps(as_json(roster, view), ensure_ascii=False, indent=1))
        return 0
    print(ui.section(f"Crew · {len(members(roster))} members"
                     + (f" · {view['goal']}" if view and view.get("goal") else "")))
    print(roster_text(view["lanes"] if view else None, roster))
    if view:
        text = run_text(view)
        if text:
            print(text)
    for w in roster["warnings"]:
        ui.warn(w)
    print(ui.dim('\n  dev crew route "<task>" · dev crew next · dev crew show <name> · dev crew brief <name> "<task>" '
                 "--scope \"<globs>\" --step <n>\n  spawn as devkit:<id> (Claude Code) or agent type <id> (Codex)"))
    return 0


def register(add):
    """Wire `dev crew` into the `dev` parser. `add(name, fn, help)` returns an argparse subparser."""
    p = add("crew", cmd_crew, "devkit's crew of specialist agents: roster, routing, who works now, briefs, messages, "
                              "decisions, sync")
    p.add_argument("--json", action="store_true", help="the roster and the live crew state as JSON")
    p.add_argument("--latest", action="store_true",
                   help="the most recent running progress run on this Mac instead of this session's")
    sp = p.add_subparsers(dest="crew_cmd")
    x = sp.add_parser("show", help="everything about one member")
    x.add_argument("name", nargs="+", help="id or name, e.g. neo, Oracle, \"The Keymaker\"")
    x = sp.add_parser("brief", help="a ready-to-paste brief for a member, with the spawn line")
    x.add_argument("name", help="id or name, e.g. neo or \"The Oracle\"")
    x.add_argument("task", nargs="+", help="what to do")
    x.add_argument("--scope", help="the files the member owns, comma-separated globs")
    x.add_argument("--step", help="the plan step it does (number or part of its name): prints the assign command")
    x = sp.add_parser("sync", help="write plugin/agents/<id>.md from config/crew.json")
    x.add_argument("--check", action="store_true", help="only report the files that differ (exit 1 if any)")
    x = sp.add_parser("ask", help="a question to Morpheus (default) or a crew member; --decision: one for the developer")
    x.add_argument("text", nargs="+", help="the question or request")
    x.add_argument("--to", help="Morpheus (default), a crew name or a lane label")
    x.add_argument("--decision", action="store_true",
                   help="a decision point: in auto mode the recommendation is taken and recorded; otherwise it waits "
                        "for the developer")
    x.add_argument("--options", help="the options, separated by |: \"A: rewrite the parser | B: patch it\"")
    x.add_argument("--recommend", help="the option you recommend, e.g. A")
    x.add_argument("--irreversible", help="the irreversible action it leads to: " + ", ".join(_irreversible()))
    x = sp.add_parser("answer", help="answer a crew message by its id")
    x.add_argument("id", help="the message id, e.g. M3")
    x.add_argument("text", nargs="+")
    sp.add_parser("inbox", help="messages for you not delivered yet")
    x = sp.add_parser("mode", help="guided (the developer decides) or auto (the crew goes on and records); "
                                   "no argument: the current mode")
    x.add_argument("mode", nargs="?", choices=["guided", "auto"])
    x.add_argument("--approve", help="irreversible actions auto mode may take, comma-separated, e.g. \"push, pr\"")
    x = sp.add_parser("decide", help="record the developer's answer to a decision (Morpheus only)")
    x.add_argument("id", help="the decision id, e.g. D3")
    x.add_argument("text", nargs="+", help="the answer")
    x = sp.add_parser("decisions", help="this run's decisions; automatic ones are marked for review")
    x.add_argument("--pending", action="store_true", help="only those waiting for the developer")
    x = sp.add_parser("route", help="who should do a task, ranked with the reasons; --plan: a member for each open step")
    x.add_argument("task", nargs="*", help="the task, in a sentence")
    x.add_argument("--files", help="the files it touches, comma-separated (they weigh in the ranking)")
    x.add_argument("--plan", action="store_true", help="propose a member for every plan step nobody owns")
    x.add_argument("--apply", action="store_true", help="with --plan: assign them")
    x = sp.add_parser("delegate", help="add a step for another crew member (routed automatically unless --to)")
    x.add_argument("task", nargs="+", help="what they should do")
    x.add_argument("--to", help="a crew name or id, or auto (default)")
    x.add_argument("--scope", help="the files they own, comma-separated globs")
    x.add_argument("--after", help="the step it waits for (number or part of its name)")
    x.add_argument("--estimate", help="e.g. 30m, 1h")
    x.add_argument("--files", help="the files it touches, comma-separated (for routing)")
    x = sp.add_parser("decline", help="hand a delegated step back to Morpheus, with the reason")
    x.add_argument("step", nargs="+", help="the step (number or part of its name)")
    x.add_argument("--why", help="why you cannot take it")
    sp.add_parser("next", help="the dispatcher: who to launch now, what is queued or waiting and why, who is busy")


def _irreversible() -> tuple:
    from . import progress
    return getattr(progress, "IRREVERSIBLE", ())
