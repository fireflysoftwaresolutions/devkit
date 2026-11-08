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
TOOLS = {"all": None, "build": "Read, Write, Edit, Grep, Glob, Bash, Skill", "review": "Read, Grep, Glob, Bash, Skill",
         "read": "Read, Grep, Glob, Skill"}  # Skill: every member loads the skills its procedure and brief name
CLAUDE_MODEL = {"strong": "inherit", "standard": "inherit", "fast": "haiku"}
CLAUDE_EFFORT = {"strong": "high", "fast": "low"}  # standard: the session's default effort, like Codex
REQUIRED = ("id", "name", "title", "role", "summary", "personality", "use_when", "skills", "tools", "tier", "returns",
            "goal", "procedure", "devkit", "done_when")
OPTIONAL = ("main", "not_for", "aliases", "keywords")  # keywords: routing words (dev crew route)
TEXT_FIELDS = ("id", "name", "title", "role", "summary", "personality", "returns", "goal")
LIST_FIELDS = ("use_when", "not_for", "skills", "aliases", "keywords", "devkit", "done_when")
PHASE_KEYS = {"phase", "do", "skills", "devkit", "gate", "proof"}
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


def _procedure_errors(proc) -> list:
    """A procedure is a list of phases: {phase, do} and optional skills, devkit (lists), gate and proof (text)."""
    if proc is None:
        return []
    if not isinstance(proc, list) or not proc:
        return ["`procedure` must be a non-empty list of phases"]
    out = []
    for i, x in enumerate(proc, 1):
        if not isinstance(x, dict) or not str(x.get("phase") or "").strip() or not str(x.get("do") or "").strip():
            out.append(f"procedure phase {i} needs `phase` and `do`")
            continue
        extra = set(x) - PHASE_KEYS
        if extra:
            out.append(f"procedure phase {i}: unknown {', '.join(sorted(extra))}")
        for k in ("skills", "devkit"):
            if k in x and not (isinstance(x[k], list) and all(isinstance(s, str) and s.strip() for s in x[k])):
                out.append(f"procedure phase {i}: `{k}` must be a list of text")
        for k in ("gate", "proof"):
            if k in x and not isinstance(x[k], str):
                out.append(f"procedure phase {i}: `{k}` must be text")
    return out


def proofs(m: dict) -> list:
    """The devkit commands a member must have run before it reports (one per phase that names a proof)."""
    return [x["proof"] for x in m.get("procedure") or [] if isinstance(x, dict) and x.get("proof")]


def check(data, known=None, agents=()):
    """-> (errors, warnings) for a roster. Unknown skills are warnings: the skill may come from elsewhere. `agents`
    ("claude", "codex"): also warn about each skill the crew names that is not installed for that agent, with its
    install group and command (selfcheck D8). It reads this Mac's skill folders, so load() leaves it out."""
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
                   or (k in ("use_when", "procedure", "devkit", "done_when") and m[k] == [])]
        if missing:
            errors.append(f"{where}: missing {', '.join(missing)}")
        for k in TEXT_FIELDS:
            if k in m and m[k] is not None and not isinstance(m[k], str):
                errors.append(f"{where}: `{k}` must be text")
        for k in LIST_FIELDS:
            if k in m and m[k] is not None and not (isinstance(m[k], list) and
                                                     all(isinstance(x, str) and x.strip() for x in m[k])):
                errors.append(f"{where}: `{k}` must be a list of text")
        errors += [f"{where}: {e}" for e in _procedure_errors(m.get("procedure"))]
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
    from . import raci
    e2, w2 = raci.check(data)
    if agents and not errors + e2:
        from . import selfcheck
        for agent in agents:
            w2 += [f"{agent}: {f.evidence} · fix: {f.fix}"
                   for f in selfcheck.d8_uninstalled_skills(agent, roster=data)]
    return errors + e2, warnings + w2


def installed_agents() -> tuple:
    """The agents devkit set up on this Mac (their home folder exists), whose installed skills `check` can verify."""
    from . import selfcheck
    return tuple(a for a, (home, _) in selfcheck.AGENT_HOMES.items() if Path(home).is_dir())


def shown_warnings(roster) -> list:
    """What `dev crew` and `dev crew sync` warn about: the roster's warnings, and the skills it names that this Mac
    has not installed for an agent devkit set up."""
    known = list(roster.get("warnings") or [])
    extra = check(roster, agents=installed_agents())[1]
    return known + [w for w in extra if w not in known]


_CACHE = {}


def overlay_path() -> Path:
    """The team's changes to the crew, in the handoff repository (written only by `dev crew set|raci --set`)."""
    return core.HANDOFF / "crew" / "crew.json"


def read_overlay(path=None) -> dict:
    ov = core.read_json(Path(path or overlay_path()), {}) or {}
    ov = ov if isinstance(ov, dict) else {}
    for k in ("members", "activities"):
        ov[k] = ov.get(k) if isinstance(ov.get(k), dict) else {}
    ov["changelog"] = ov.get("changelog") if isinstance(ov.get("changelog"), list) else []
    return ov


def merge(base: dict, ov: dict) -> dict:
    """devkit's roster with the team's changes: fields of existing members and activities are replaced, new ones
    are added (they must be complete)."""
    import copy
    out = copy.deepcopy(base)
    by_id = {m.get("id"): m for m in out.get("members") or []}
    for mid, fields in (ov.get("members") or {}).items():
        if not isinstance(fields, dict):
            continue
        if mid in by_id:
            by_id[mid].update(fields)
        else:
            out.setdefault("members", []).append(dict(fields, id=mid))
    acts = {a.get("id"): a for a in out.get("activities") or [] if isinstance(a, dict)}
    for aid, fields in (ov.get("activities") or {}).items():
        if not isinstance(fields, dict):
            continue
        if aid in acts:
            acts[aid].update(fields)
        else:
            out.setdefault("activities", []).append(dict(fields, id=aid))
    return out


def _stat_key(p: Path):
    try:
        st = p.stat()
        return (str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(p), None, None)


def load(path=None, team: bool = True) -> dict:
    """The validated roster; `warnings` lists what is suspicious but allowed. Raises RosterError.

    With `team` (the default for devkit's own roster), the team's changes from the handoff repository are merged
    on top. When they would break the roster, devkit's roster is used and a warning says why. The generated plugin
    agents use devkit's roster alone (`team=False`), so the devkit checkout never changes."""
    p = Path(path or ROSTER)
    try:
        p.stat()
    except OSError:
        raise RosterError(["the roster file is missing"], p) from None
    team = team and path is None
    key = (_stat_key(p), _stat_key(overlay_path()) if team else None)
    if key in _CACHE:
        return _CACHE[key]
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError) as e:
        raise RosterError([f"not valid JSON ({e})"], p) from None
    errors, warnings = check(data)
    if errors:
        raise RosterError(errors, p)
    if team and overlay_path().exists():
        ov = read_overlay()
        merged = merge(data, ov)
        e2, w2 = check(merged)
        if e2:
            warnings = warnings + [f"the team's crew changes ({core.tilde(overlay_path())}) are not used: "
                                   + "; ".join(e2[:3])]
        else:
            data, warnings = merged, w2
            data["team_changes"] = len(ov.get("changelog") or [])
    data["warnings"] = warnings
    if len(_CACHE) > 8:
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


def who(m, roster=None) -> str:
    """How developers see a member: the role first, then the name ("Backend engineer (Tank)"). `m` is a member or
    any name one answers to: its name, id, alias, agent type, a lane label ("Kid: curl installer") or "main" (the
    main session, "Tech lead (Morpheus)"). Anything else ("devkit", "CI watch", "the developer") stays as written."""
    if not isinstance(m, dict):
        name = str(m or "").strip()
        if not name:
            return ""
        found = next((x for x in members(roster) if x.get("main")), None) if name == "main" else \
            _for_label(name, roster)
        if not found:
            return name
        m = found
    return f"{m['title']} ({m['name']})" if m.get("title") else m["name"]


def task_of(label, roster=None) -> str:
    """A lane or step label without the member's name in front: "Kid: curl installer" -> "curl installer"."""
    label = str(label or "").strip()
    head, sep, rest = label.partition(":")
    return rest.strip() if sep and rest.strip() and member(head, roster) else label


def status_header(m: dict, project="<project>", branch="<branch>", task="<task>") -> str:
    return f"**[Firefly> DevKit · {who(m)}]** {project} · `{branch}` · {task}"


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
           "## Goal", "", _sentence(m["goal"]), "",
           "## When the tech lead sends work to you", ""]
    out += [f"- {x}" for x in m["use_when"]] + [""]
    out += [("Not yours: " + "; ".join(m["not_for"]) if m.get("not_for") else "Not yours: anything outside your brief")
            + ". Say so in your report and leave it to the tech lead.", "", "## Skills to load first", ""]
    if m["skills"]:
        out += ["Load each one with the Skill tool (it may list a plugin skill with its prefix, such as "
                "`devkit:testing`). Without the Skill tool, or when it does not list one, read the skill's SKILL.md.",
                "", ", ".join(f"`{s}`" for s in m["skills"]), ""]
    else:
        out += ["None. Load a skill only when your brief names one.", ""]
    rule = str(roster.get("procedure_rule") or "").strip()  # every member's, with the phases
    out += ["## Your procedure", "",
            "Follow these phases in order. A phase is finished when its gate holds; `proof` marks the devkit command "
            "devkit looks for in your session before it accepts your report." + (f" {_sentence(rule)}" if rule else ""),
            ""]
    for i, x in enumerate(m["procedure"], 1):
        line = f"{i}. **{x['phase']}.** {_sentence(x['do'])}"
        extras = []
        if x.get("skills"):
            extras.append("skills: " + ", ".join(f"`{s}`" for s in x["skills"]))
        if x.get("devkit"):
            extras.append("devkit: " + ", ".join(f"`{c}`" for c in x["devkit"]))
        if x.get("gate"):
            extras.append(f"gate: {x['gate']}")
        if x.get("proof"):
            extras.append(f"proof: `{x['proof']}`")
        out.append(line + (" " + " · ".join(extras) if extras else ""))
    out.append("")
    out += _raci_section(m, roster)
    out += ["## Your devkit commands", "",
            "devkit is how the crew works and talks: use these instead of ad-hoc scripts, files or messages."
            + ("" if shell else " You have no shell: name the command in your report and the tech lead runs it."), ""]
    out += [", ".join(f"`{c}`" for c in m["devkit"]), ""]
    out += ["## How you work with the crew", ""]
    if shell:
        out += ["- Stay inside the files and the scope your brief names. Before you go beyond them, ask the tech lead: "
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
                '`Needs from the tech lead:`.']
    if tools == "review":
        out.append("- You are review-only: you read and run commands such as tests and scans, but you never edit, "
                   "create or delete files, not even to fix what you find.")
    elif tools == "read":
        out.append("- You are read-only: you never edit, create or delete files, and you never commit. Cite "
                   '`path:line` for every fact, mark what you inferred, and say `not found` rather than guess.')
    out += ["- Learn from mistakes with devkit's lesson manager: follow the prevention of the known pitfalls your brief "
            "lists; when you make or find a mistake that could repeat (a refused command you had to redo, a test you "
            "broke, a wrong assumption), record it with where it applies, so devkit recalls it in any language: "
            "`dev lesson add \"…\" --cause \"…\" --prevent \"…\" --file <glob> --command \"<command words>\" "
            "--error \"<text the output shows>\"`. A known one: `dev lesson recur <id> --evidence \"…\"`; one "
            "without anchors: `dev lesson anchor <id> --file … --why \"…\"`." if shell else
            "- Learn from mistakes: follow the prevention of the known pitfalls your brief lists, and name in your "
            "report any mistake that could repeat, with the files, commands or error text it applies to, so the tech "
            "lead records it as a lesson with its anchors.",
            "- Launch a helper agent only for a narrow lookup or a slice of your own files: it gets your tool level "
            "and scope, never more, takes one of the crew slots, and cannot go deeper than progress.max_depth (2). "
            "When you need another specialist, say so in your report or ask the tech lead.", "",
            "## Your report", "",
            "Your final message starts with your own status block; nothing comes before it:", ""]
    out += ["```markdown"] + report_block(m) + ["```", "", f"Then deliver: {_sentence(m['returns'])}", "",
            "You are done when:", ""] + [f"- {x}" for x in m["done_when"]] + ["",
                              'End with `Needs from the tech lead:` and the decisions or questions for the tech lead, '
                              'or `nothing`.',
                              "", "## Language", "", _sentence(roster["language_rule"])]
    return "\n".join(out)


# --------------------------------------------------------------------------- the enforcement matrix

# Every rule a crew member or Morpheus is given, the devkit check that enforces it, and its state: (id, rule, where,
# check, state). The id (`E-…`) is stable, so docs cite it (selfcheck D11) and a reworded rule keeps it. "enforced"
# names the function that does it (selfcheck D1: it imports and a test names it); "planned" names the plan step or
# task that adds it (D2); "guidance" says why it cannot be checked mechanically. `dev crew rules` prints it.
ENFORCEMENT = (
    ("E-status-block", "A member's report starts with its own status block", "SubagentStop: the crew canary",
     "devkit.hooks.subagent_stop", "enforced"),
    ("E-state-lock", "Session and run records never lose a concurrent write",
     "every hook and dev command (one lock per session)", "devkit.state.update", "enforced"),
    ("E-step-records", "A member records its work with dev progress step", "SubagentStop: lane without updates",
     "devkit.progress.lane_unreported", "enforced"),
    ("E-scope", "A member edits only the files of its scope", "PreToolUse: Edit, Write, MultiEdit, apply_patch",
     "devkit.hooks._scope_violation", "enforced"),
    ("E-shell-scope", "A member's file scope also holds for shell writes: for review-only and read-only members, "
     "whose shell runs only the read-only allowlist and whose writes are detected (a builder's shell is not checked "
     "against its scope)", "PreToolUse: the read-only shell allowlist; PostToolUse: write detection",
     "devkit.guard.crew_shell_verdict", "enforced"),
    ("E-read-only", "Review-only and read-only members never edit", "PreToolUse: the edit tools",
     "devkit.hooks._scope_violation", "enforced"),
    ("E-separate-files", "Members working in parallel own separate files",
     "dev progress assign (a scope that overlaps a working member's)", "devkit.progress.assign", "enforced"),
    ("E-main-scope", "The main session does not edit a working member's files",
     "PreToolUse: the edit tools (DEVKIT_SCOPE_OVERRIDE=1 for an emergency)", "devkit.hooks._crew_limits",
     "enforced"),
    ("E-owner", "An ended or stale member holds no file, in any session's run; dev crew owner says who holds a path "
     "exactly as the edit check decides", "PreToolUse: the edit tools, and dev crew owner",
     "devkit.progress.scope_holders", "enforced"),
    ("E-responsible", "A step goes to the member the RACI makes Responsible", "dev progress assign",
     "devkit.raci.check_assignment", "enforced"),
    ("E-any-language", "The RACI holds in any language",
     "a declared kind of work, the member's own kind, then the files (C13)", "devkit.raci.anchor", "enforced"),
    ("E-words-never-refuse", "Words never refuse work: with no declared kind of work and no files, the member's "
     "own kind decides (R2-2)", "dev progress assign", "devkit.raci.check_assignment", "enforced"),
    ("E-catalog", "Sessions get the catalog of ids, and lessons at the tool call they apply to (R2-1)",
     "SessionStart, PreToolUse and PostToolUse", "devkit.hooks.catalog_context", "enforced"),
    ("E-approvals", "A run ends only after each Accountable member approved", "dev progress done",
     "devkit.progress.approvals_owed", "enforced"),
    ("E-no-self-approval", "Nobody approves their own kind of work: the Code reviewer (Seraph) approves code and "
     "configuration, the Tech lead (Morpheus) the rest (R2-3)",
     "dev progress step --approves, a review step's end and dev progress done",
     "devkit.approvals.record", "enforced"),
    ("E-verdict", "An approval is an explicit verdict after the work", "dev progress done",
     "devkit.approvals.end_check", "enforced"),
    ("E-evidence", "A step closes only with its evidence", "dev progress step and SubagentStop",
     "devkit.approvals.ending", "enforced"),
    ("E-gate-findings", "A member the crew gates sent back cannot close its step with the findings open (CR1-F1)",
     "SubagentStop: the crew gates, at the second stop", "F6 step endings and approvals", "planned"),
    ("E-deps", "No step starts before its dependencies are done", "every step transition (start, add, assign, begin, "
     "advance, subagent start, close, launch readiness)", "devkit.progress.deps_open", "enforced"),
    ("E-recheck", "A reopened dependency's finished dependents are rechecked before the run ends", "dev progress done",
     "devkit.approvals.recheck_open", "enforced"),
    ("E-fixable", "A step is reopened or skipped only with a reason, kept in the run's history",
     "dev progress reopen and dev progress skip", "devkit.progress.reopen", "enforced"),
    ("E-parallel", "At most progress.max_parallel members work in parallel (4 by default)",
     "PreToolUse: Agent launches", "devkit.hooks._crew_limits", "enforced"),
    ("E-resumed", "A resumed subagent counts against the parallel limit", "launch records keyed by tool_use_id",
     "devkit.progress.slots", "enforced"),
    ("E-nested", "A member's helpers take a crew slot and stop at progress.max_depth", "PreToolUse: Agent launches, "
     "at every depth", "devkit.progress.launch_verdict", "enforced"),
    ("E-lost-lane", "A member that never reports back loses its step after progress.stale_agent_hours",
     "every hook event on the run and every launch check", "devkit.progress.expire_stale", "enforced"),
    ("E-main-only", "Only the tech lead plans, assigns, decides and changes the mode",
     "dev progress and dev crew from a lane", "devkit.progress._main_only", "enforced"),
    ("E-caller", "The tech lead's own commands cannot be reached through bash -c or scripts",
     "the call ledger (Claude Code; Codex calls are audited until S3)", "devkit.progress.caller", "enforced"),
    ("E-decisions", "A decision that is the developer's waits for the developer in guided mode",
     "dev crew ask --decision", "devkit.progress.crew_decision", "enforced"),
    ("E-irreversible", "Irreversible actions wait for the developer's word", "PreToolUse: the irreversible-action gate",
     "devkit.guard.irreversible_verdict", "enforced"),
    ("E-proofs", "A member runs the proofs its procedure names", "SubagentStop: the crew gates",
     "devkit.gates.crew_gates", "enforced"),
    ("E-skills", "A member loads the skills of each procedure phase it works in and those the skill rules require "
                 "for the files it changes", "SubagentStop: the crew gates",
     "devkit.gates.crew_gates", "enforced"),
    ("E-stubs", "No stubs or untracked TODOs in a member's changes", "SubagentStop: the crew gates",
     "devkit.gates.crew_gates", "enforced"),
    ("E-tests-fail", "A member's new tests can fail", "SubagentStop: the crew gates", "devkit.gates.crew_gates",
     "enforced"),
    ("E-lessons", "A member records a lesson after a repeated refusal", "SubagentStop: the crew gates",
     "devkit.gates.crew_gates", "enforced"),
    ("E-background", "A member's background commands have ended before it reports (Claude Code; the tech lead is "
     "told of a member let through with one still running)", "SubagentStop: the crew gates",
     "devkit.gates.background_work", "enforced"),
    ("E-pitfalls", "Members get the known pitfalls for their task", "dev crew brief and the Claude launch hook",
     "devkit.lessons.for_task", "enforced"),
    ("E-knowledge", "Members get the knowledge of their kind of work", "dev crew brief and the Claude launch hook",
     "devkit.crew.brief_skills", "enforced"),
    ("E-protected", "Agents cannot write devkit's state or configuration",
     "PreToolUse: protected paths, redirects, writers, nested shells", "devkit.guard.check", "enforced"),
    ("E-forged-env", "A forged HOME, DEVKIT_CONFIG or dev link cannot point devkit at fake state",
     "the guard (forged variables, every dev on PATH) and dev at start (devkit.core.ensure_roots)",
     "devkit.guard.forged_env_reason", "enforced"),
    ("E-escape", "Escape hatches are refused to members and audited", "PreToolUse: flags and variables",
     "devkit.guard.crew_shell_verdict", "enforced"),
    ("E-pinned", "The guard runs from a verified copy, not the checkout",
     "dev install (pins a read-only, checksummed release)", "devkit.setup.pin", "enforced"),
    ("E-team-reason", "Team changes to the crew carry a reason", "dev crew set and dev crew raci --set",
     "devkit.crew._team_write", "enforced"),
    ("E-ask", "Questions and decisions go through dev crew ask", "a member's report and messages",
     "a report may also name its questions", "guidance"),
)


def catalog(roster=None, agent: str = "claude") -> str:
    """The ids the model declares, for the session's context (SessionStart and the brief after a compaction): every
    kind of work with its Responsible and Accountable members and skills, the skill rules, and the lesson anchors.
    devkit never guesses them from the developer's words, which can be in any language (crew gap review, C13)."""
    from . import raci, skillguard
    roster = roster or load()

    def short_names(ids):
        return ", ".join(who(member(i, roster) or i, roster) for i in ids or [])
    lines = ["devkit catalog · declare these ids, whatever language the developer writes in; devkit enforces on them "
             "and never on words.",
             "Kinds of work: one per step (`[id]` in a plan spec, as in `api=1h after design [backend]`, or "
             "`--activity <id>` when you assign it) and per brief (`--activity <id>`). Without one, a step takes its "
             "member's Responsible kind of work, then its files'."]
    for a in raci.activities(roster):
        sk = ", ".join((a.get("knowledge") or {}).get("skills") or [])
        lines.append(f"- `{a['id']}` {a['name']} · R {short_names(a.get('R'))} · A {short_names([a.get('A')])}"
                     + (f" · skills {sk}" if sk else ""))
    lines.append("Skills the files you change and the commands you run require (briefs get them; Stop checks them):")
    for r in skillguard.rules():
        need = skillguard.required(r, agent)
        if need:
            when = [f"for {', '.join(r['activities'])}"] if r.get("activities") else []
            when += [f"running {', '.join(r['commands'][:3])}"] if r.get("commands") else []
            when += ["by its files"] if r.get("files") else []
            lines.append(f"- {r['id']}: {', '.join(need)} ({'; '.join(when)})")
    lines.append('Lesson anchors: record where a lesson applies, never its words: dev lesson add "…" --cause "…" '
                 '--prevent "…" --file <glob> --command "<command words>" --activity <id> --error "<text the output '
                 'shows>" --stack <name>. An older lesson gets them with dev lesson anchor <id> … --why "…".')
    return "\n".join(lines)


def enforcement(plan_steps=None) -> list:
    """The matrix as rows, with a check that every enforced rule's function exists and every planned rule names
    a step of `plan_steps` (when given). -> [(rule, where, ref, state, ok)], in the order of ENFORCEMENT (whose
    first field is each row's id)."""
    import importlib
    out = []
    for _, rule, where, ref, state in ENFORCEMENT:
        ok = True
        if state == "enforced":
            mod, _, fn = ref.rpartition(".")
            try:
                ok = callable(getattr(importlib.import_module(mod), fn, None))  # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import (the name comes from ENFORCEMENT, a constant in devkit source)
            except ImportError:
                ok = False
        elif state == "planned" and plan_steps is not None:
            ok = ref in plan_steps
        out.append((rule, where, ref, state, ok))
    return out


def _raci_section(m: dict, roster) -> list:
    """The member's place in the crew's RACI, in the words it acts on."""
    from . import raci
    r = raci.roles_of(m["id"], roster)
    if not any(r.values()):
        return []
    out = ["## Your place in the crew's RACI", ""]
    for a in r["R"]:
        line = f"- **You do** {raci._lower(a['name'])}."
        if a.get("A") and a["A"] != m["id"]:
            line += (f" {raci.names([a['A']], roster)} approves it: say so in your report, so the tech lead schedules "
                     "the review.")
        if a.get("C"):
            line += (f" Consult {raci.names([x for x in a['C'] if x != m['id']], roster)} before decisions that affect "
                     "them (`dev crew ask --to <name>`).")
        out.append(line)
    approves = [f"- **You approve** {raci._lower(a['name'])} done by {raci.names(a.get('R'), roster)}."
                for a in r["A"] if m["id"] not in (a.get("R") or [])]
    if approves:  # said once: a reviewer approves a dozen kinds of work, and its agent file must stay short
        out += approves + ["- **When you approve,** review the work with evidence, and approve it or send it back "
                           "with the fixes."]
    if r["C"]:
        out.append("- **You are consulted on** " + "; ".join(raci._lower(a["name"]) for a in r["C"])
                   + ": answer questions from the crew promptly (`dev crew answer <id> \"<text>\"`).")
    if r["I"]:
        out.append("- **You are informed about** " + "; ".join(raci._lower(a["name"]) for a in r["I"]) + ".")
    return out + [""]


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
    """{file name: text} of every generated Claude Code agent, aliases included (devkit's roster: the plugin ships
    in the devkit checkout; the team's changes reach the members through their briefs)."""
    roster = roster or load(team=False)
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
    roster = roster or load(team=False)
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
    """Messages not delivered yet, and questions nobody has answered. A notice (CI watch, a step or launch notice, a
    decision's result) and an answer close once delivered."""
    from . import progress
    msgs = (run or {}).get("messages") or []
    waiting = {m.get("id") for to in {m.get("to") for m in msgs} for m in progress.waiting_messages(run, to)}
    return [m for m in msgs if not m.get("delivered") or m.get("id") in waiting]


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
                      "who": who(m) if m else (lane.get("persona") or lane.get("label") or ""),
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
                         "who": who(m) if m else s["label"],
                         "scope": s.get("scope") or [], "state": s.get("state"),
                         "lane": names.get(owner, "") if owner != "main" else ""})
    return {"goal": b.get("goal"), "project": run.get("project"), "state": b.get("state"),
            "headline": b.get("headline"), "eta_min": b.get("eta_min"), "mode": progress.mode_of(run),
            "approved": list(run.get("approved") or []), "lanes": lanes, "assigned": assigned,
            "messages": [_msg(m) for m in open_messages(run)],
            "decisions": [_decision(d) for d in progress.pending_decisions(run)],
            "moot": [dict(_decision(d), text=progress.moot_text(run, d)) for d in progress.moot_decisions(run)],
            "ready": ready_view(run, roster)}


def _who_label(label, roster=None) -> str:
    """crew.who for a label; dispatch.py still calls it by this name."""
    return who(label or "", roster)


def ready_view(run: dict, roster=None) -> dict:
    """dispatch.ready() as plain data: what Morpheus can launch now, what is queued or waiting and why, who is busy."""
    from . import dispatch, progress
    r = dispatch.ready(run)
    step = lambda s: {"step": s["name"], "member": s.get("label") or "", "who": _who_label(s.get("label"), roster),  # noqa: E731
                      "scope": s.get("scope") or [], "requested_by": s.get("requested_by") or "",
                      "order": r["why"].get(s["name"], "")}
    b = r["slots"]
    return {"free": r["free"], "max_parallel": r["limit"],  # the effective limit: lower while the quota is short
            "slots": {"used": b["used"], "limit": b["limit"], "max": b["max"], "reserve": b["reserve"],
                      "why": b["why"], "text": progress.slots_text(b)},
            "launch": [dict(step(s), launch=dispatch.launch_line(s)) for s in r["launch"]],
            "queued": [dict(step(s), launch=dispatch.launch_line(s)) for s in r["queued"]],
            "waiting": [dict(step(s), why=why) for s, why in r["waiting"]],
            "busy": [{"member": (_for_lane(lane, roster) or {}).get("name") or lane.get("persona") or "",
                      "who": _who_label(lane.get("persona") or lane.get("label"), roster),
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
        rows.append([(ui.ACTIVE + " " if m["id"] in busy else "  ") + who(m), m["id"], when, m["tier"], m["tools"]])
    out = [ui.table(rows, ["  MEMBER", "ID", "WHEN TO USE", "TIER", "TOOLS"], max_widths={0: 36, 2: 48})]
    if active:
        out.append(ui.section("Working now"))
        for lane in active:
            m = member(lane.get("member"), roster) or _for_label(lane.get("label"), roster)
            dot = ui.ACTIVE if lane.get("state") == "running" else ui.IDLE
            scope = f" · scope {', '.join(lane['scope'])}" if lane.get("scope") else ""
            out.append(f"  {dot} {(who(m) if m else lane.get('label') or 'subagent'):<30} "
                       f"{lane.get('state') or '-':<8} {_mins(lane.get('min')):>6}  "
                       f"{task_of(lane.get('label'), roster)}{scope}")
    return "\n".join(out)


def mode_line(view: dict) -> str:
    approved = ", ".join(view.get("approved") or []) or "nothing"
    if view.get("mode") == "auto":
        return (f"  Mode auto · the crew goes on with recommended options and records them · pre-approved "
                f"irreversible actions: {approved}")
    return "  Mode guided · decisions that are the developer's wait for the developer"


def slots_line(sl: dict) -> str:
    """'Slots: 1 of 3 slots in use (lowered from 6: 5-hour usage at 87 %) · 1 kept for the developer's own request'"""
    keep = f" · {sl['reserve']} kept for the developer's own request" if sl.get("reserve") else ""
    return f"Slots: {sl['text']}{keep}"


def run_text(view: dict) -> str:
    """The mode, assigned steps, open messages and pending decisions of a run_view."""
    out = [mode_line(view)]
    ready = view.get("ready") or {}
    if ready.get("slots"):
        out.append(f"  {slots_line(ready['slots'])}")
    if ready.get("launch"):
        out.append(f"  Ready to launch: {', '.join((s.get('who') or s['member']) + ': ' + s['step'] for s in ready['launch'])}"
                   " · dev crew next")
    for d in view.get("moot") or []:  # not the developer's: their own line, never in the section below
        out.append(f"  {d.get('text')} It closes with the run, or: dev crew withdraw {d['id']} --why \"…\"")
    if view.get("decisions"):
        out.append(ui.section("Decisions waiting for the developer"))
        for d in view["decisions"]:
            extra = [f"options {' | '.join(d['options'])}" if d.get("options") else "",
                     f"recommends {d['recommend']}" if d.get("recommend") else "",
                     f"irreversible: {d['irreversible']}" if d.get("irreversible") else "", d.get("note") or ""]
            out.append(f"  {d['id']} {who(d.get('from_name')) or '?'}: {d.get('question')}"
                       + "".join(f" · {x}" for x in extra if x))
        out.append(ui.dim('  ask the developer, then: dev crew decide <id> "<answer>"'))
    if view.get("assigned"):
        out.append(ui.section("Assigned steps"))
        for s in view["assigned"]:
            where = f"lane {s['lane']}" if s.get("lane") else ("waiting for its agent" if s.get("state") != "done"
                                                              else "")
            scope = f" · scope {', '.join(s['scope'])}" if s.get("scope") else ""
            out.append(f"  {s['step']}. {s['name']} → {s.get('who') or s['member']} · {s.get('state') or '-'}"
                       + (f" · {where}" if where else "") + scope)
    if view.get("messages"):
        out.append(ui.section("Open messages"))
        for m in view["messages"]:
            state = "not delivered" if not m.get("delivered") else "waiting for an answer"
            out.append(f"  {m['id']} {who(m.get('from_name')) or '?'} → {who(m.get('to_name')) or '?'} · {state}"
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
    out = [ui.section(who(m) + (f" · {_lc(m['role'])}" if m["role"] != m.get("title") else "")),
           ui.kv("id", m["id"] + (f"  (aliases: {', '.join(aliases(m))})" if aliases(m) else "")),
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


def _brief_activity(m: dict, task: str, step=None, roster=None, activity=None, globs=None):
    """The kind of work a brief is for, from language-neutral anchors (`raci.anchor`): the declared one (--activity),
    else the plan step's, else the member's Responsible one and the files in its scope."""
    from . import raci
    if activity:
        return raci.resolve_activity(activity, roster)  # a wrong id is refused, not guessed
    try:
        if step is not None and str(step).strip():
            from . import progress
            run = progress.normalize(progress.load_run(*progress.current_session()) or {})
            s = progress._find_step(run, step)
            if s:
                a = raci.step_activity(s, roster)
                if a:
                    return a
        return raci.anchor(task, member=m["id"], files=globs, roster=roster)[0]
    except Exception:
        return None


def brief_skills(m: dict, task: str = "", globs=None, agent: str = "claude", activity: str = ""):
    """The skills a member loads for one task: its own, plus those devkit's skill rules (config/skill-rules.json)
    require for its kind of work and the files in its scope, so a Spring task brings the Java skills and a Python
    one does not. Never from the task's words. -> (skills, [why])."""
    out, why = list(m.get("skills") or []), []
    try:
        from . import skillguard
        rules = skillguard.rules_for_activity(activity) + [r for r, _ in skillguard.rules_for_files(list(globs or []))]
        for r in rules:
            add = [s for s in skillguard.required(r, agent) if s not in out]
            if add:
                out += add
                why.append(f"{', '.join(add)}: {r.get('id') or r.get('name') or 'rule'}")
    except Exception:
        pass
    return out, why


def _skill_lines(m: dict, skills: list, why: list, globs, roster) -> list:
    """The brief's skills, the ones the crew gates check first: the skills of the member's procedure phases
    (gates.gated_skills, each with its phases) and those the skill rules require for the files of its scope; then the
    rest its role or kind of work lists. A brief never calls a gated skill optional."""
    from . import gates, skillguard
    path = lambda s: f": `{core.tilde(p)}`" if (p := skillguard.skill_path(s)) else ""  # noqa: E731
    phases = {}
    try:
        for g in gates.gated_skills(m, roster):
            phases.setdefault(g["skill"], [])
            if g.get("phase") and g["phase"] not in phases[g["skill"]]:
                phases[g["skill"]].append(g["phase"])
        for r, _ in skillguard.rules_for_files(list(globs or [])):
            for s in skillguard.required(r, "claude"):
                phases.setdefault(s, [])
    except Exception:
        phases = {}  # a gate that cannot be read lists every skill alike, never one less
    if not skills and not phases:
        return ["None."]
    out = []
    if phases:
        out += ["The crew gates check these before you stop: load each with the Skill tool, or read its SKILL.md at "
                "the path shown when the Skill tool does not list it, before you work in its phase:"]
        out += [f"- `{s}`" + (f" ({' and '.join(ph)} phase{'s' if len(ph) > 1 else ''})" if ph else
                              " (the files of your scope)") + path(s) for s, ph in phases.items()]
        out.append("They check them in each phase you work in, plus the skills devkit's skill rules require for the "
                   "files you change (devkit names those when you edit).")
    rest = [s for s in skills if s not in phases]
    if rest:
        out += (["", "Also listed for your role or kind of work; load them first too (the gates do not check them)"]
                if phases else ["Load each with the Skill tool, or read its SKILL.md at the path shown when the "
                                "Skill tool does not list it"])
        out[-1] += (f" ({'; '.join(why)})" if why else "") + ":"
        out += [f"- `{s}`" + path(s) for s in rest]
    return out


def brief_text(m: dict, task: str, scope: str = "", step=None, roster=None, cwd=None, activity=None) -> str:
    """What `dev crew brief` prints: the spawn lines, the lane link and the brief to paste as the prompt."""
    roster = roster or load()
    if m.get("main"):
        raise ValueError(f"{m['name']} is the main session's role, not a subagent: brief a specialist instead")
    cwd = cwd or os.getcwd()
    top = core.gitout(cwd, "rev-parse", "--show-toplevel")
    project = Path(top).name if top else "<project>"
    pid = ""
    if top:
        from .registry import main_of
        main_ = main_of(Path(top)) or Path(top)
        pid = core.project_identity(main_, core.gitout(main_, "config", "--get", "remote.origin.url"))[0] or ""
    branch = (core.gitout(cwd, "rev-parse", "--abbrev-ref", "HEAD") if top else "") or "<branch>"
    globs = [g.strip() for g in (scope or "").split(",") if g.strip()]
    head = [ui.section(f"Brief for {who(m)}")] + [f"  {ln}" for ln in _spawn_lines(m)]
    if step is not None and str(step).strip():
        w = str(step).strip()
        head.append(f"  Link the lane dev progress assign {w if w.isdigit() else _dq(w)} --to {_dq(m['name'])}"
                    + (f" --scope {_dq(','.join(globs))}" if globs else "") + "   (run it before you spawn)")
    head.append(ui.dim("  ── paste everything below as the prompt ──"))
    b = [f"You are {m['name']}, the crew's {_lc(m['role'])} (agent devkit:{m['id']}). Follow your agent "
         "instructions; this brief adds the task.", "",
         "## Task", "", task.strip(), "", "## Scope", ""]
    if globs:
        b.append(f"You own only these files: {', '.join(f'`{g}`' for g in globs)}. Ask the tech lead before you "
                 'change anything else: `dev crew ask "may I change <file>? why: <reason>"`.')
    else:
        b.append("The files this task needs and nothing beyond them. Ask the tech lead before you widen it: "
                 '`dev crew ask "<question>"`.')
    # the guard lets a member write only in its own folder of the session scratchpad (guard.scratch_refusal)
    b += ["", f"Your scratch folder is `scratchpad/{m['id']}/` in the session scratchpad: put logs, probes and other "
              "temporary files there, never at the scratchpad's top level or in another member's folder."]
    from . import raci
    act = _brief_activity(m, task, step, roster, activity=activity, globs=globs)
    if act:  # the declaration the launch hook and the crew gates read, whatever language the task is written in
        b += ["", f"Kind of work: {act['name']} (`{act['id']}`)"]
    b += ["", "## Skills to load first", ""]
    skills, why = brief_skills(m, task, globs, activity=(act or {}).get("id") or "")
    know = (act or {}).get("knowledge") or {}
    for s in know.get("skills") or []:
        if s not in skills:
            skills.append(s)
            why.append(f"{s}: {raci._lower(act['name'])}")
    b += _skill_lines(m, skills, why, globs, roster)
    if know.get("docs"):
        b += ["", "## Read first", "", f"devkit's guides for {raci._lower(act['name'])}; follow them:"]
        b += [f"- `{core.tilde(core.DEVKIT_HOME / d)}`" for d in know["docs"] if (core.DEVKIT_HOME / d).exists()]
    if m.get("procedure"):
        b += ["", "## Procedure", "", "Follow your procedure's phases (your agent instructions); before you report, "
              "devkit checks that you ran: " + (", ".join(f"`{p}`" for p in proofs(m)) or "nothing in particular") + "."]
        if str(roster.get("procedure_rule") or "").strip():  # the crew gates check it at SubagentStop
            b.append(_sentence(roster["procedure_rule"]))
    try:
        from . import lessons
        picked = lessons.for_task(pid, activity=(act or {}).get("id"), files=globs) if pid else []
    except Exception:
        picked = []
    if picked:
        b += ["", "## Known pitfalls", "", "Lessons from earlier mistakes in this work; follow their prevention:"] + \
            lessons.pitfall_lines(picked)
    b += ["", "## Return", "", _sentence(m["returns"]), "",
          "## Report format", "", "Your final message starts with this block; nothing comes before it:", ""]
    b += report_block(m, project=project, branch=branch, task="<the task, in a few words>")
    b += ["", 'Then what you deliver (above), then `Needs from the tech lead:` with your decisions or questions, or '
          '`nothing`.', "", "## Commands", ""]
    if not _shell(m):
        b.append("You have no shell, so the tech lead runs these for you: put your progress in your report and your "
                 'questions under `Needs from the tech lead:`.')
    b += ['- After each finished step: `dev progress step "<what finished>" --check "<evidence>"`',
          '- A question for the tech lead (or another crew member with `--to <name>`): `dev crew ask "<question>"`',
          '- Answer a message: `dev crew answer <id> "<text>"`']
    return "\n".join(head) + "\n\n" + "\n".join(b)


# --------------------------------------------------------------------------- JSON (dev crew --json, devkit_crew)

def as_json(roster=None, view=None) -> dict:
    roster = roster or load()
    out = []
    for m in members(roster):
        x = {k: m.get(k) for k in ("id", "name", "title", "role", "main", "summary", "goal", "use_when", "not_for", "keywords",
                                   "skills", "procedure", "devkit", "done_when", "tools", "tier", "returns")}
        x["aliases"] = aliases(m)
        if not m.get("main"):
            x["spawn"] = {"claude_code": f"devkit:{m['id']}", "codex": m["id"]}
        out.append({k: v for k, v in x.items() if v not in (None, [], False)})
    raci = [{k: a.get(k) for k in ("id", "name", "R", "A", "C", "I")} for a in roster.get("activities") or []]
    return {"members": out, "raci": raci, "team_changes": roster.get("team_changes") or 0, "run": view,
            "warnings": roster.get("warnings") or []}


# --------------------------------------------------------------------------- the session graph (dev crew graph)
#
# What runs in a session, as one graph: the main session (Morpheus) at the root; its progress run; the run's steps
# as a DAG (an `after` edge goes from the step waited for to the step that waits); the crew lanes (subagents), each
# under the steps it owns and a nested subagent under the lane that launched it (the lane's `parent`: that lane's
# id); the handoff work items the run links (a task id in the goal, a step's name, description, task or `links`),
# otherwise the project's Now tasks; and what waits: decisions for the developer, approvals owed, open messages.
# session_graph() builds the nodes and edges (--json); graph_text() draws the tree, graph_mermaid() a flowchart.

TASK_ID_RE = re.compile(r"\b[TFBS]-[0-9a-z]{6,}\b")
_GLYPH = {"done": "✓", "running": "▸", "ready": "◇", "waiting": "○", "blocked": "✗", "skipped": "–", "recheck": "!"}
_PAINT = {"done": "green", "running": "cyan", "ready": "blue", "waiting": "gray", "blocked": "red", "skipped": "dim",
          "recheck": "yellow"}
_MERMAID_CLASS = {"done": "fill:#e6f4ea,stroke:#1e8e3e,color:#0d3b1e",
                  "running": "fill:#e8f0fe,stroke:#1a73e8,color:#0b2a5b",
                  "ready": "fill:#f1f3f4,stroke:#1a73e8,color:#202124,stroke-dasharray: 4 4",
                  "waiting": "fill:#f1f3f4,stroke:#9aa0a6,color:#3c4043",
                  "blocked": "fill:#fce8e6,stroke:#d93025,color:#5b0f0a",
                  "skipped": "fill:#ffffff,stroke:#bdc1c6,color:#5f6368,stroke-dasharray: 4 4",
                  "recheck": "fill:#fef7e0,stroke:#f9ab00,color:#4a3500"}
NO_RUN = ('dev progress start "<goal>" --plan "design=30m; build=1h after design" --estimate 2h')


def _step_state(run, s) -> str:
    """A plan step's state for the graph: a pending step is `waiting` (a dependency is open) or `ready`."""
    from . import progress
    st = s.get("state") or "pending"
    if st == "pending":
        return "waiting" if progress.deps_open(run, s) else "ready"
    return st


def _no_cycle(agents: dict, aid: str) -> bool:
    """A lane's chain of parents ends (a run edited into a loop of parents is drawn flat)."""
    seen, p = {aid}, agents[aid].get("parent")
    while p in agents:
        if p in seen:
            return False
        seen.add(p)
        p = agents[p].get("parent")
    return True


def _work_items(run, plan, step_ids) -> tuple:
    """(items, {item id: [node ids]}, source): the work items the run links ("linked"), else the project's open Now
    tasks ("now"), else none. An id that is not an item of the project is not a link."""
    from . import work
    try:
        items = work.all_items(run.get("project")) if run.get("project") else []
    except Exception:
        items = []  # no handoff repository on this machine: the graph has no work items
    ids = {i["meta"].get("id", ""): i for i in items}

    def resolve(tok):
        if tok in ids:
            return tok
        hits = [k for k in ids if k.startswith(tok)]
        return hits[0] if len(hits) == 1 else None

    links = {}
    for target, texts in [("run", [run.get("goal")])] + [
            (step_ids[i], [s.get(k) for k in ("name", "desc", "task", "label")]
             + (s.get("links") if isinstance(s.get("links"), list) else [s.get("links")]))
            for i, s in enumerate(plan)]:
        for tok in TASK_ID_RE.findall(" ".join(str(x) for x in texts if x)):
            iid = resolve(tok)
            if iid and target not in links.setdefault(iid, []):
                links[iid].append(target)
    if links:
        return [ids[k] for k in links], links, "linked"
    now = [i for i in work.ordered_open(items) if i["meta"].get("bucket") == "now"]
    return now, {}, "now" if now else "none"


def session_graph(run, roster=None, now=None) -> dict:
    """The session as nodes and edges, for `dev crew graph --json`, the tree and the flowchart.

    Node kinds: session (Morpheus), run, step (state ready | waiting | running | blocked | done | skipped, plus the
    steps whose reopening it must `recheck`), lane (a subagent), task (a handoff work item), decision, approval,
    message. Edge kinds: runs (session → run), step (run → each step), after (the step waited for → the step that
    waits), works_on (lane → the step it owns), launched (session or parent lane → lane), tracks (task → run or
    step), asks (who asks → decision), approves (approval → step), sends and to (sender → message → recipient).
    {"run": None, "nodes": [], …} without a run."""
    from . import progress, raci
    if not run:
        return {"session": None, "run": None, "nodes": [], "edges": [], "work_items": "none", "hint": NO_RUN}
    roster = roster or load()
    now = now or time.time()
    run = progress.normalize(dict(run))
    plan, agents = run.get("plan") or [], run.get("agents") or {}
    lead = next((m for m in members(roster) if m.get("main")), None)
    step_ids = [f"s{i}" for i in range(1, len(plan) + 1)]
    by_name = {}
    for i, s in enumerate(plan):
        by_name.setdefault(s.get("name"), step_ids[i])
    lane_ids = {aid: f"lane{i}" for i, aid in enumerate(agents, 1)}
    addr = lambda a: "main" if a == "main" else lane_ids.get(a)  # noqa: E731
    session = {"id": "main", "kind": "session", "label": lead["name"] if lead else "Morpheus",
               "who": who(lead) if lead else "Morpheus", "agent": run.get("agent") or "",
               "session": run.get("session") or "", "project": run.get("project") or ""}
    b = progress.brief(run, now)
    run_node = {"id": "run", "kind": "run", "label": run.get("goal") or "", "goal": run.get("goal") or "",
                "state": run.get("state") or "", "mode": progress.mode_of(run),
                "approved": list(run.get("approved") or []), "headline": b.get("headline"),
                "eta_min": b.get("eta_min"), "eta_lo": b.get("eta_lo"), "eta_hi": b.get("eta_hi"),
                "steps": len(plan), "closed": sum(1 for s in plan if s.get("state") in progress.DONE)}
    nodes, edges = [session, run_node], [{"from": "main", "to": "run", "kind": "runs"}]
    edge = lambda f, t, k: edges.append({"from": f, "to": t, "kind": k})  # noqa: E731
    for i, s in enumerate(plan):
        sid, owner = step_ids[i], s.get("owner") or "main"
        lane = agents.get(owner) if owner != "main" else None
        m = _for_label(s["label"], roster) if s.get("label") else (_for_lane(lane, roster) if lane else lead)
        try:
            act = raci.step_activity(s, roster)
        except Exception:
            act = None  # a broken RACI never hides the graph
        after = list(dict.fromkeys(by_name[a] for a in s.get("after") or [] if a in by_name))  # a hand edit may repeat
        waiting = list(dict.fromkeys(by_name[a] for a in progress.deps_open(run, s) if a in by_name))
        nodes.append({"id": sid, "kind": "step", "ref": i + 1, "label": s.get("name") or "",
                      "state": _step_state(run, s), "recheck": list(s.get("recheck") or []),
                      "decision": s.get("waiting") or "",
                      "owner": who(m) if m else (s.get("label") or (lane or {}).get("persona") or "subagent"),
                      "member": m["id"] if m else "", "lane": lane_ids.get(owner),
                      "activity": (act or {}).get("id") or "", "activity_name": (act or {}).get("name") or "",
                      "after": after, "waiting_for": waiting,
                      "est": s.get("est"), "min": round(progress.step_minutes(run, s, now), 1),
                      "scope": s.get("scope") or []})
        edge("run", sid, "step")
        for a in after:
            edge(a, sid, "after")
        if owner in lane_ids:
            edge(lane_ids[owner], sid, "works_on")
    for aid, lane in agents.items():
        m = _for_lane(lane, roster)
        parent = lane.get("parent")
        parent = lane_ids[parent] if parent in lane_ids and parent != aid and _no_cycle(agents, aid) else None
        nodes.append({"id": lane_ids[aid], "kind": "lane", "ref": aid,
                      "label": lane.get("label") or lane.get("type") or aid[:8],
                      "who": who(m) if m else (lane.get("persona") or lane.get("type") or "subagent"),
                      "member": m["id"] if m else "", "type": lane.get("type") or "",
                      "state": lane.get("state") or "", "decision": lane.get("waiting") or "",
                      "min": round(progress.active_minutes(run, lane.get("started"), lane.get("finished") or now,
                                                           now, aid), 1),
                      "parent": parent, "steps": [step_ids[i] for i, s in enumerate(plan) if s.get("owner") == aid],
                      "scope": lane.get("scope") or []})
        edge(parent or "main", lane_ids[aid], "launched")
    items, links, source = _work_items(run, plan, step_ids)
    for k, it in enumerate(items, 1):
        meta = it["meta"]
        nodes.append({"id": f"task{k}", "kind": "task", "ref": meta.get("id"), "label": meta.get("title") or "",
                      "type": meta.get("type") or "", "status": meta.get("status") or "",
                      "bucket": meta.get("bucket") or "", "linked": source == "linked",
                      "tracks": links.get(meta.get("id"), [])})
        for target in links.get(meta.get("id"), []):
            edge(f"task{k}", target, "tracks")
    for k, d in enumerate(progress.pending_decisions(run), 1):
        nodes.append({"id": f"dec{k}", "kind": "decision", "ref": d.get("id"), "label": d.get("question") or "",
                      "from": d.get("from_name") or "", "options": d.get("options") or [],
                      "recommend": d.get("recommend") or "", "irreversible": d.get("irreversible") or "",
                      "note": d.get("note") or ""})
        edge(addr(d.get("from") or "main") or "main", f"dec{k}", "asks")
    for k, (mid, names) in enumerate(progress.approvals_owed(run), 1):
        m = member(mid, roster)
        steps = [by_name[n] for n in names if n in by_name]
        nodes.append({"id": f"appr{k}", "kind": "approval", "ref": mid, "label": f"{who(m) if m else mid} approves",
                      "who": who(m) if m else mid, "member": mid, "steps": steps})
        for sid in steps:
            edge(f"appr{k}", sid, "approves")
    for k, msg in enumerate(open_messages(run), 1):
        nodes.append({"id": f"msg{k}", "kind": "message", "ref": msg.get("id"), "label": msg.get("text") or "",
                      "from": msg.get("from_name") or "", "to": msg.get("to_name") or "",
                      "state": "not delivered" if not msg.get("delivered") else "waiting for an answer",
                      "reply_to": msg.get("reply_to") or "", "time": _when(msg.get("ts"))})
        if addr(msg.get("from")):
            edge(addr(msg.get("from")), f"msg{k}", "sends")
        if addr(msg.get("to")):
            edge(f"msg{k}", addr(msg.get("to")), "to")
    return {"session": session, "run": run_node, "nodes": nodes, "edges": edges, "work_items": source}


def _paint(state: str, text: str) -> str:
    return getattr(ui, _PAINT.get(state, "dim"))(text)


def _step_parts(n: dict, nodes: dict) -> list:
    """[head, state, owner, kind of work] of a step node; the state's words carry it without color."""
    flag = "recheck" if n["recheck"] and n["state"] in ("done", "running") else n["state"]
    state = n["state"]
    if state == "waiting":
        state = "waiting for " + ", ".join(str(nodes[x]["ref"]) for x in n["waiting_for"])
    elif state == "blocked" and n.get("decision"):
        state = f"blocked: waiting for decision {n['decision']}"
    if n["recheck"]:
        state += f" · recheck (reopened: {', '.join(n['recheck'])})"
    return [f"{_paint(flag, _GLYPH.get(flag, '·'))} {n['ref']}. {n['label']}", _paint(flag, state), n["owner"]] + \
        ([f"[{n['activity']}]"] if n["activity"] else [])


def _lane_line(n: dict) -> str:
    state = f"waiting for decision {n['decision']}" if n.get("decision") else (n["state"] or "-")
    label = task_of(n["label"])
    label = "" if label in (n["who"], n.get("type")) or member(label) else label
    return " · ".join(x for x in (f"{ui.ACTIVE if n['state'] == 'running' else ui.IDLE} {n['who']}", label, state,
                                  _mins(n["min"])) if x)


def _tree(children, prefix: str) -> list:
    """[(lines, [children])] drawn with ├─ └─ │; a node's further lines continue under its first."""
    out = []
    for i, (lines, kids) in enumerate(children):
        last = i == len(children) - 1
        lines = [lines] if isinstance(lines, str) else lines
        ext = prefix + ui.dim("   " if last else "│  ")
        out.append(prefix + ui.dim("└─ " if last else "├─ ") + lines[0])
        out += [ext + ln for ln in lines[1:]]
        out += _tree(kids, ext)
    return out


def graph_text(g: dict, latest: bool = False) -> str:
    """The session graph as a terminal tree (dev crew graph)."""
    if not g.get("run"):
        where = "No running progress run on this Mac" if latest else "No progress run in this session"
        return (f"  {where}, so there is no graph to draw.\n  Start one: {NO_RUN}"
                + ("" if latest else "\n" + ui.dim("  another session's run: dev crew graph --latest")))
    from . import progress
    nodes = {n["id"]: n for n in g["nodes"]}
    kind = lambda k: [n for n in g["nodes"] if n["kind"] == k]  # noqa: E731
    s, r, lanes = g["session"], g["run"], kind("lane")
    kids = {}
    for ln in lanes:
        kids.setdefault(ln["parent"], []).append(ln)
    expanded = set()

    def lane_node(ln):
        first = ln["id"] not in expanded
        expanded.add(ln["id"])
        return _lane_line(ln), [lane_node(c) for c in kids.get(ln["id"], [])] if first else []

    try:
        order = progress.topo_order([{"name": n["id"], "after": n["after"]} for n in kind("step")])
    except progress.StepError:
        order = [{"name": n["id"]} for n in kind("step")]  # a run edited into a cycle: plan order
    steps = []
    for x in order:
        n = nodes[x["name"]]
        parts = _step_parts(n, nodes)
        rest = parts[2:] + (["after " + ", ".join(str(nodes[a]["ref"]) for a in n["after"])] if n["after"] else [])
        line = " · ".join(parts[:2] + [ui.dim(" · ".join(rest))])
        steps.append((line, [lane_node(ln) for ln in lanes if n["id"] in ln["steps"]]))
    loose = [ln for ln in lanes if not ln["steps"] and not ln["parent"]]
    if loose:
        steps.append(("Lanes with no step", [lane_node(ln) for ln in loose]))
    if not steps:
        steps.append((ui.dim('no plan steps: dev progress add "<step>=30m"'), []))
    top = [([ui.bold(f"Run · {r['goal']}"), f"{r['state']} · mode {r['mode']} · {r['headline']}"], steps)]
    tasks = kind("task")
    if tasks:
        name = lambda t: "the run" if t == "run" else f"step {nodes[t]['ref']}"  # noqa: E731
        rows = [(" · ".join([t["ref"], t["status"], t["label"]] + (
            [ui.dim("tracks " + ", ".join(name(x) for x in t["tracks"]))] if t["tracks"] else [])), [])
            for t in tasks[:8]]
        if len(tasks) > 8:
            rows.append((ui.dim(f"+{len(tasks) - 8} more: dev task"), []))
        top.append(("Work items linked to the run" if g["work_items"] == "linked"
                     else f"Now tasks of {s['project']} (the run links none)", rows))
    decisions = kind("decision")
    if decisions:
        top.append(("Decisions waiting for the developer" + ui.dim(' · dev crew decide <id> "<answer>"'), [
            (" · ".join(x for x in (f"{d['ref']}", f"{who(d['from']) or '?'} asks: {ui.trunc(d['label'], 120)}",
                                    "options " + " | ".join(d["options"]) if d["options"] else "",
                                    f"recommends {d['recommend']}" if d["recommend"] else "",
                                    f"irreversible: {d['irreversible']}" if d["irreversible"] else "", d["note"])
                       if x), []) for d in decisions]))
    approvals = kind("approval")
    if approvals:
        top.append(("Approvals owed", [(f"{a['who']} approves " + ", ".join(
            f"{nodes[x]['ref']}. {nodes[x]['label']}" for x in a["steps"]), []) for a in approvals]))
    messages = kind("message")
    if messages:
        top.append(("Open messages", [(f"{x['ref']} · {who(x['from']) or '?'} → {who(x['to']) or '?'} · "
                                       f"{x['state']}"
                                       + (f" · answer to {x['reply_to']}" if x["reply_to"] else "")
                                       + f": {ui.trunc(x['label'], 120)}", []) for x in messages]))
    legend = "  ".join(_paint(k, _GLYPH[k]) + " " + k for k in ("done", "running", "ready", "waiting", "blocked",
                                                                 "skipped", "recheck"))
    head = [ui.section(f"Session graph · {s['project'] or '-'}"),
            "  " + legend + ui.dim(" · [kind of work] · after: the steps it waits for"),
            f"  {ui.ACTIVE} {s['who']} · main session · {s['agent']} {s['session'][:8]}"]
    return "\n".join(head + _tree(top, "  ") + [ui.dim("\n  dev crew graph --mermaid | --json · dev crew next: who to "
                                                       "launch now")])


def _mm(text, limit: int = 90) -> str:
    """Text inside a Mermaid label's quotes: one line, entity codes for what Mermaid or HTML would read (the
    caller joins lines with <br/>)."""
    s = ui.trunc(" ".join(ui.ANSI_RE.sub("", str(text or "")).split()), limit)
    for a, b in (("#", "#35;"), ('"', "#quot;"), ("<", "#lt;"), (">", "#gt;"), ("`", "#96;")):
        s = s.replace(a, b)
    return s


def graph_mermaid(g: dict) -> str:
    """The session graph as a Mermaid flowchart that renders on GitHub: steps as nodes, dependencies as edges, lanes
    as subgraphs holding the steps they own (a nested lane inside its parent's)."""
    if not g.get("run"):
        return f'flowchart TD\n  main["{_mm("No progress run: " + NO_RUN, 120)}"]'
    nodes = {n["id"]: n for n in g["nodes"]}
    kind = lambda k: [n for n in g["nodes"] if n["kind"] == k]  # noqa: E731
    s, r, lanes = g["session"], g["run"], kind("lane")
    out = ["flowchart TD", f'  main(["{_mm(s["who"] + " · main session")}"])',
           f'  run["{_mm("Run: " + r["goal"])}<br/>{_mm(r["state"] + " · " + r["mode"] + " · " + r["headline"])}"]',
           "  main --> run"]
    placed = set()

    def step_def(n, pad):
        placed.add(n["id"])
        parts = _step_parts(n, nodes)
        return f'{pad}{n["id"]}["{_mm(parts[0], 70)}<br/>{_mm(" · ".join(parts[1:]))}"]'

    def subgraph(ln, pad):
        body = [step_def(nodes[x], pad + "  ") for x in ln["steps"] if x not in placed]
        for child in [c for c in lanes if c["parent"] == ln["id"]]:
            body += subgraph(child, pad + "  ")
        return [f'{pad}subgraph {ln["id"]}["{_mm(_lane_line(ln))}"]'] + (
            body or [f'{pad}  {ln["id"]}i["{_mm("no plan step")}"]']) + [f"{pad}end"]

    for ln in lanes:
        if not ln["parent"]:
            out += subgraph(ln, "  ")
    out += [step_def(n, "  ") for n in kind("step") if n["id"] not in placed]
    for n in kind("step"):
        out += [f"  {a} --> {n['id']}" for a in n["after"]] or [f"  run -.-> {n['id']}"]
    for t in kind("task"):
        out.append(f'  {t["id"]}("{_mm(t["ref"] + " · " + t["status"] + " · " + t["label"])}")')
        out += [f"  {t['id']} -.- {x}" for x in t["tracks"]]
    for d in kind("decision"):
        out.append(f'  {d["id"]}{{{{"{_mm(d["ref"] + " · waits for the developer: " + d["label"])}"}}}}')
        blocked = [n["id"] for n in kind("step") if n.get("decision") == d["ref"]]
        out += [f"  {d['id']} -.- {x}" for x in blocked] or [f"  main -.- {d['id']}"]
    for a in kind("approval"):
        out.append(f'  {a["id"]}>"{_mm(a["label"])}"]')
        out += [f"  {a['id']} -.-> {x}" for x in a["steps"]]
    flags = [(n["id"], "recheck" if n["recheck"] and n["state"] in ("done", "running") else n["state"])
             for n in kind("step")]
    out += [f"  classDef {c} {_MERMAID_CLASS[c]}" for c in _MERMAID_CLASS if c in {f for _, f in flags}]
    out += [f"  class {sid} {f}" for sid, f in flags if f in _MERMAID_CLASS]
    return "\n".join(out)


def _graph(a, roster) -> int:
    latest = bool(getattr(a, "latest", False) or getattr(a, "graph_latest", False))
    g = session_graph(live_run(latest=latest), roster)
    if getattr(a, "graph_json", False) or getattr(a, "json", False):
        print(json.dumps(g, ensure_ascii=False, indent=1))
    elif getattr(a, "mermaid", False):
        print(graph_mermaid(g))
    else:
        print(graph_text(g, latest))
    return 0


# --------------------------------------------------------------------------- CLI

def _from_agent():
    return os.environ.get("DEVKIT_FROM_AGENT") or None  # the hooks set it inside a subagent: its own lane


def _ask_decision(a, me) -> int:
    from . import progress
    if a.to:
        ui.err("a decision goes to the developer through the tech lead: drop --to")
        return 2
    opts, rec = progress.decision_shape(" ".join(a.text), a.options or "", a.recommend or "", a.irreversible or "",
                                        a.target or "", getattr(a, "approves", "") or "",
                                        performs=getattr(a, "performs", "") or "")
    d = progress.crew_decision(" ".join(a.text), options=opts, recommend=rec, irreversible=a.irreversible or "",
                               from_agent=me, target=a.target or "", approves=getattr(a, "approves", "") or "",
                               performs=getattr(a, "performs", "") or "")
    if d["state"] == "decided":
        print(f"{ui.OK} auto mode: go ahead with {_option(d, d['answer'])} ({d['id']}, recorded; the developer can "
              "revisit it with dev crew decisions)")
        return 0
    print(f"{ui.WARN} {d['id']}: {d['question']}" + (f" · options {' | '.join(d['options'])}" if d["options"] else "")
          + (f" · recommends {d['recommend']}" if d["recommend"] else "")
          + (f" · irreversible: {d['irreversible']}" if d["irreversible"] else "")
          + (f" · only “{d['performs']}” opens it" if d.get("performs") else ""))
    if d.get("note"):
        print(ui.dim(f"  {d['note']}"))
    if me:
        print(f"waiting for the developer's decision {d['id']}: stop here and end your report with your status block "
              f"(Check: – waiting for {d['id']}; Needs from the tech lead: {d['id']}). The tech lead resumes you "
              "with the answer")
    else:
        print("ask the developer now in one question call (dev crew decisions --ask prints it), then: "
              f"dev crew decide {d['id']} \"<answer>\"")
    return 0


def _decide(a, me) -> int:
    from . import progress
    if me:
        ui.err("only the tech lead records the developer's answers: stop and end your report with "
               f"\"Needs from the tech lead: {a.id}\"")
        return 2
    d = progress.crew_decide(a.id, " ".join(a.text))
    tail = f": {_option(d, d['answer'])}" + (f" (overrides the automatic choice {d['overrides']})"
                                             if d.get("overrides") else "")
    if d.get("verified"):  # who decided is computed from the developer's evidence (spec C5), never typed
        how = "the question tool" if (d.get("evidence") or {}).get("channel") == "E1" else "their prompt"
        print(f"{ui.OK} {d['id']} decided by the developer (through {how}){tail}")
    else:
        print(f"{ui.WARN} {d['id']} recorded by the tech lead, not verified by the developer{tail}")
        print(ui.dim(f"  devkit saw no answer of the developer to {d['id']} after it was shown; for their own "
                     f"answer: dev crew decisions --ask {d['id']}"))
    if d.get("from") and d["from"] != "main":
        print(f"resume {who(d.get('from_name')) or 'the crew member'} with SendMessage / send_input and the answer "
              "(a message with it is queued for them)")
    return 0


def _withdraw(a, me) -> int:
    from . import progress
    if me:
        ui.err("only the tech lead withdraws a decision: if yours no longer matters, say so in your report or with "
               "dev crew ask")
        return 2
    d = progress.crew_withdraw(a.id, a.why or "")
    print(f"{ui.OK} {d['id']} withdrawn: {d['why']}")
    print(ui.dim("  in the run's history and the audit trail; it is not the developer's answer and opens nothing"))
    return 0


def _ask(run, ids, as_json=False) -> int:
    """The decisions as question tool calls the tech lead can send as they are or reword (spec C5: the developer's
    answer to a question that names D<n> up front, with an option's letter, is their evidence E1)."""
    from . import progress
    try:
        p = progress.ask_calls(run, ids)
    except progress.StepError as e:
        ui.err(str(e))
        return 2
    if as_json:
        print(json.dumps(p, ensure_ascii=False, indent=1))
        return 0
    asked = [q["header"] for c in p["calls"] for q in c["questions"]]
    if not asked and not p["typed"]:
        print("  no decisions wait for the developer")
        return 0
    n = len(p["calls"])
    if n:
        print(f"Ask the developer in {'one question call' if n == 1 else f'{n} question calls, one after the other'}"
              " (AskUserQuestion, at most 4 questions each, an irreversible decision alone in its call). Reword a "
              "reversible question as you like and add context; keep “D<n> ·” up front (or the header D<n>) and "
              "each option's letter.")
    for i, c in enumerate(p["calls"], 1):
        if n > 1:
            print(f"Call {i} of {n}:")
        for q in c["questions"]:
            print(f"  {q['question']}")
            print(f"    header {q['header']} · " + " | ".join(o["label"] for o in q["options"]))
    decs = [progress._decision(run, x) for x in asked + p["typed"]]
    print("Or the developer types a line of its own: " + "; ".join(
        f"approve {d['id']}" + (": <option>" if d.get("options") else "") for d in decs))
    for d in decs:
        if d.get("irreversible"):
            perf = progress.performing_option(d)
            print(ui.dim(f"  {d['id']} leads to an irreversible {d['irreversible']}: send devkit's exact call, "
                         "word for word: one question, its own header, labels only (no description, no preview); "
                         "a reworded question or label, another question in the call, or the developer's own text "
                         "counts for nothing. "
                         + (f"Only “{perf}” opens it" if perf else "No option opens it" if d.get("options")
                            else "Only approve or yes opens it")))
    print(ui.dim("  the tool input as JSON: dev crew decisions --ask --json"))
    return 0


def _decisions(a) -> int:
    from . import progress
    run = live_run()
    if getattr(a, "ask", None) is not None:
        return _ask(run, a.ask, getattr(a, "ask_json", False))
    if getattr(a, "ask_json", False):
        ui.err("--json goes with --ask: dev crew decisions --ask --json")
        return 2
    decs = progress.pending_decisions(run) if a.pending else [d for d in (run or {}).get("decisions") or []]
    if not decs:
        print("  no pending decisions" if a.pending else "  no decisions in this session's run")
        return 0
    rows = []
    for d in decs:
        answer = _option(d, d.get("answer")) + (f" (was {d['overrides']})" if d.get("overrides") else "")
        if d.get("state") in ("withdrawn", "closed"):  # the reason stands where an answer would
            answer = d.get("why") or d.get("reason") or ""
        by = "auto · review" if d.get("by") == "auto" else (d.get("by") or "") + \
            (" · unverified" if d.get("verified") is False else " ✓" if d.get("verified") else "")
        rows.append([d["id"], who(d.get("from_name")) or "?", d.get("question") or "",
                     progress.decision_state(run, d), answer or "-", by or "-"])
    print(ui.table(rows, ["ID", "FROM", "QUESTION", "STATE", "ANSWER", "BY"], max_widths={2: 56, 4: 36}))
    mode = progress.mode_of(run)
    print(ui.dim(f"  mode {mode} · override an automatic decision: dev crew decide <id> \"<other answer>\""))
    return 0


def _mode(a, me) -> int:
    from . import progress
    if not a.mode:
        if a.approve:
            ui.err("--approve goes with auto: dev crew mode auto --approve \"push feat/*, pr feat/*\"")
            return 2
        run = live_run()
        view = {"mode": progress.mode_of(run), "approved": list((run or {}).get("approved") or [])}
        print(mode_line(view) + ("" if run else "\n" + ui.dim("  no progress run in this session yet: the default")))
        print(ui.dim("  irreversible actions auto mode can pre-approve, each with its target (push feat/*): "
                     + ", ".join(x for x in progress.IRREVERSIBLE if x not in progress.NEVER_PREAPPROVED)))
        return 0
    if me:
        ui.err("only the tech lead sets the mode, on the developer's word: ask with dev crew ask")
        return 2
    run = progress.set_mode(a.mode, approve=a.approve or "")
    print(f"{ui.OK} " + mode_line({"mode": run.get("mode"), "approved": run.get("approved")}).strip())
    unbacked = progress.unbacked_approvals(run)
    if unbacked:  # a pre-approval needs the developer's own grant (spec C5)
        verb = "it opens" if len(unbacked) == 1 else "they open"
        print(f"{ui.WARN} no grant of the developer for {', '.join(unbacked)}: {verb} nothing until they type a line "
              f"of its own, such as “grant {unbacked[0]}”, and you run this again")
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
            print(f"  {s['name']} → {who(m)} ({score:g}) · {'; '.join(why)}")
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
    print(ui.table([[i, who(m), m["id"], f"{score:g}", "; ".join(why)]
                    for i, (m, score, why) in enumerate(ranked, 1)],
                   ["#", "MEMBER", "ID", "SCORE", "WHY"], max_widths={4: 72}))
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
    print(f"{ui.OK} step “{s['name']}” for {who(m)}{how}"
          + (f" · after {', '.join(s['after'])}" if s.get("after") else "")
          + (f" · scope {', '.join(s['scope'])}" if s.get("scope") else ""))
    if r.get("lane"):
        print(f"in the {who(m)}'s lane now (they got a message)")
    else:
        print(f"The tech lead launches {who(m)} when it is ready: dev crew next")
    return 0


def _decline(a, me) -> int:
    from . import dispatch
    s = dispatch.decline(" ".join(a.step), a.why or "", from_agent=me)
    print(f"{ui.OK} handed “{s['name']}” back to the tech lead: {a.why}")
    print(ui.dim(f"  The tech lead routes it again: dev crew route {_dq(s['name'])}"))
    return 0


def _next(a=None, me=None) -> int:
    from . import dispatch, progress
    first, why = (getattr(a, "first", None) or "", getattr(a, "why", None) or "") if a is not None else ("", "")
    if first or why:
        if not (first and why):
            ui.err('launch a step ahead of the order with both: dev crew next --first "<step>" --why "…"')
            return 2
        s = dispatch.order_why(first, why, from_agent=me)
        print(f"{ui.OK} “{s['name']}” may go ahead of the order: {why} (recorded and audited)")
        return 0
    run = live_run()
    if not run:
        print('  no progress run in this session: dev progress start "<goal>" --plan "<step>; <step>"')
        return 0
    if not me:
        dispatch.record_order(run)  # the order Morpheus launches in, kept on the run (spec R5)
    v, mode = ready_view(run), progress.mode_of(run)
    print(ui.section("Next" + (f" · {run.get('goal')}" if run.get("goal") else "")))
    approved = ", ".join(run.get("approved") or [])
    print(f"  {slots_line(v['slots'])} · mode {mode}"
          + (f" · pre-approved: {approved}" if mode == "auto" and approved else ""))
    if v["launch"]:
        print(ui.section("Ready to launch, in this order"))
        for s in v["launch"]:
            print(f"  {s.get('who') or s['member']}: {s['step']} · {s['order']}\n    {s['launch']}")
    if v["queued"]:
        print(ui.section("Queued until a slot frees, in this order"))
        print("\n".join(f"  {s.get('who') or s['member']}: {s['step']} · {s['order']}" for s in v["queued"]))
    if v["launch"] or v["queued"]:
        print(ui.dim('  launch in this order; to launch a step ahead of it: dev crew next --first "<step>" --why "…"'))
    if v["waiting"]:
        print(ui.section("Waiting"))
        print("\n".join(f"  {s.get('who') or s['member']}: {s['step']} · {s['why']}" for s in v["waiting"]))
    if v["busy"]:
        print(ui.section("Busy"))
        print("\n".join(f"  {ui.ACTIVE} {b.get('who') or b['member'] or 'subagent'} · {task_of(b['label'])}"
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
            return _next(a, me)
        if act == "mode":
            return _mode(a, me)
        if act == "decide":
            return _decide(a, me)
        if act == "withdraw":
            return _withdraw(a, me)
        if act == "decisions":
            return _decisions(a)
        if act == "ask" and a.decision:
            return _ask_decision(a, me)
        if act == "ask":
            m = progress.crew_ask(" ".join(a.text), to=a.to, from_agent=me)
            print(f"{ui.OK} {m['id']} to {who(m.get('to_name') or 'main')}: {m['text']}")
            print(ui.dim("  it reaches them with their next tool call or prompt; the answer reaches you the same way"))
        elif act == "answer":
            m = progress.crew_answer(a.id, " ".join(a.text), from_agent=me)
            print(f"{ui.OK} {m['id']} to {who(m.get('to_name')) or '?'}, the answer to {a.id}: {m['text']}")
        else:
            msgs = progress.crew_inbox(from_agent=me)
            if not msgs:
                print("  no new messages")
                return 0
            for m in msgs:
                print(f"  {m['id']} from {who(m.get('from_name')) or '?'}: {m['text']}"
                      + (f" (answer to {m['reply_to']})" if m.get("reply_to") else ""))
            print(ui.dim('  answer with: dev crew answer <id> "<text>"'))
    except ValueError as e:
        ui.err(str(e))
        return 2
    return 0


EDITABLE = ("name", "title", "role", "summary", "personality", "goal", "returns", "use_when", "not_for", "skills", "procedure",
            "devkit", "done_when", "keywords", "tools", "tier")


def _team_write(ov: dict, what: str, why: str, by: str = "") -> Path:
    """Validate the team's changes merged on devkit's roster, then write them, the README and one commit."""
    from . import work
    if not why.strip():
        raise ValueError("a change to the crew needs its reason: --why \"<reason>\"")
    base = json.loads(ROSTER.read_text())
    errors, _ = check(merge(base, ov))
    if errors:
        raise ValueError("that change would break the crew: " + "; ".join(errors[:4]))
    ov.setdefault("changelog", []).append({"at": work.now_iso(), "by": by or work.actor_str(work.actor()),
                                           "what": what, "why": why.strip()})
    f = overlay_path()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(ov, indent=2, ensure_ascii=False) + "\n")
    _CACHE.clear()
    readme = f.parent / "README.md"
    readme.write_text(team_readme(load()))
    _commit_team([f, readme], f"crew: {what}\n\nwhy: {why.strip()}")
    return f


def _commit_team(paths, message: str):
    """One commit per crew change in the handoff repository (the audit trail); `dev sync` pushes it."""
    from . import audit, work
    h = core.HANDOFF
    if not (h / ".git").exists():
        return
    rel = [str(Path(p).relative_to(h)) for p in paths]
    core.git(h, "add", "--", *rel)
    if core.git(h, "diff", "--cached", "--quiet")[0] != 0:
        core.git(h, "commit", "-q", "-m", message, "--", *rel)
        audit.record("handoff.crew", ", ".join(rel), message.split("\n")[0][:300])
    work.request_sync("crew change")


def set_member(name: str, field: str, value: str, why: str, by: str = "") -> dict:
    """Change one field of a member for the whole team (the handoff repository). List fields take "a; b; c"."""
    m = member(name)
    if not m:
        raise ValueError(f"no crew member {name!r}: dev crew lists them")
    if field not in EDITABLE:
        raise ValueError(f"`{field}` cannot be changed; these can: {', '.join(EDITABLE)}")
    if field == "procedure":  # a JSON list of phases: [{"phase": "…", "do": "…", "devkit": ["dev test"], "proof": "…"}]
        try:
            v = json.loads(value)
        except ValueError as e:
            raise ValueError(f"procedure takes a JSON list of phases ({e})") from None
        bad = _procedure_errors(v)
        if bad:
            raise ValueError("; ".join(bad))
    else:
        v = [x.strip() for x in value.split(";") if x.strip()] if field in LIST_FIELDS else value.strip()
    ov = read_overlay()
    ov["members"].setdefault(m["id"], {})[field] = v
    _team_write(ov, f"{m['name']}: {field}", why, by)
    return member(m["id"])


def set_raci(activity_id: str, changes: dict, why: str, by: str = "") -> dict:
    """Change the RACI of one kind of work for the whole team: {"R": [ids], "A": id, "C": [...], "I": [...]}."""
    from . import raci
    act = raci.activity(activity_id)
    if not act:
        raise ValueError(f"no kind of work {activity_id!r}: dev crew raci lists them")
    ov = read_overlay()
    fields = ov["activities"].setdefault(act["id"], {})
    for k, v in changes.items():
        if k not in raci.LETTERS:
            raise ValueError(f"`{k}` is not R, A, C or I")
        ids = [member(x)["id"] if member(x) else x for x in ([v] if isinstance(v, str) else v)]
        fields[k] = ids[0] if k == "A" else ids
    _team_write(ov, f"RACI {act['id']}: " + ", ".join(f"{k}={','.join(fields[k]) if isinstance(fields[k], list) else fields[k]}"
                                                      for k in changes), why, by)
    return raci.activity(act["id"])


def team_readme(roster=None) -> str:
    """The crew as the team runs it, for people reading the handoff repository (generated)."""
    from . import raci
    roster = roster or load()
    out = ["# The crew", "", "_Generated by `dev crew` from devkit's roster and this folder's `crew.json` (the team's "
           "changes). Change it with `dev crew set <member> <field> \"…\" --why \"…\"` or `dev crew raci <work> --set "
           "\"R=… A=…\" --why \"…\"`; never by hand._", "",
           "The Tech lead (Morpheus), the main session, leads every development request: the tech lead routes each "
           "kind of work to the member who does it (R), and the work is not done until the member who approves it "
           "(A) has.", "",
           "## Members", "", "| Member | Role | Goal |", "| --- | --- | --- |"]
    out += [f"| {who(m)} | {m['role']} | {m.get('goal', '')} |" for m in members(roster)]
    out += ["", "## RACI", "", "R does the work, A approves it (one member), C is consulted, I is informed.", "",
            raci.markdown(roster), "", "## Changes", ""]
    log = read_overlay().get("changelog") or []
    out += [f"- {c.get('at', '')} · {c.get('by', '')} · {c.get('what', '')} — why: {c.get('why', '')}"
            for c in reversed(log[-50:])] or ["- none yet: the team runs devkit's roster as shipped"]
    return "\n".join(out) + "\n"


def _raci_cmd(a) -> int:
    from . import raci
    if a.set:
        if not a.work:
            ui.err('name the kind of work: dev crew raci <work> --set "R=tank A=seraph" --why "…"')
            return 2
        changes = {}
        for part in a.set.replace(";", " ").split():
            k, _, v = part.partition("=")
            changes[k.strip().upper()] = [x for x in v.split(",") if x]
        try:
            act = set_raci(a.work, changes, a.why or "")
        except ValueError as e:
            ui.err(str(e))
            return 2
        print(f"{ui.OK} {act['name']}: R {raci.names(act['R'])} · A {raci.names([act['A']])} · "
              f"C {raci.names(act.get('C')) if act.get('C') else '–'} · I {raci.names(act.get('I')) if act.get('I') else '–'}")
        print(ui.dim(f"  recorded in {core.tilde(overlay_path())} for the whole team"))
        return 0
    if a.work:
        m = member(a.work)
        if m:
            r = raci.roles_of(m["id"])
            for k, label in (("R", "does"), ("A", "approves"), ("C", "is consulted on"), ("I", "is informed about")):
                if r[k]:
                    print(f"  {who(m)} {label}: " + "; ".join(x["name"] for x in r[k]))
            return 0
        act = raci.activity(a.work)
        if not act:
            ranked = raci.classify(a.work)
            if not ranked:
                ui.err(f"no member or kind of work {a.work!r}, and no RACI signal in it: dev crew raci lists them")
                return 2
            act = ranked[0][0]
            print(ui.dim(f"  “{a.work}” reads as {act['name']} ({', '.join(ranked[0][2][:4])})"))
        rows = [[act["id"], act["name"], raci.names(act["R"]), raci.names([act["A"]]),
                 raci.names(act.get("C")) if act.get("C") else "–", raci.names(act.get("I")) if act.get("I") else "–"]]
    else:
        rows = [[x["id"], x["name"], raci.names(x["R"]), raci.names([x["A"]]),
                 raci.names(x.get("C")) if x.get("C") else "–", raci.names(x.get("I")) if x.get("I") else "–"]
                for x in raci.activities()]
    if a.markdown:
        print(raci.markdown())
        return 0
    print(ui.table(rows, ["ID", "KIND OF WORK", "R · DOES IT", "A · APPROVES", "C · CONSULTED", "I · INFORMED"],
                   max_widths={1: 30, 2: 30, 3: 30, 4: 40, 5: 30}))
    print(ui.dim('  dev crew raci <member|work> · dev crew raci <work> --set "R=… A=… C=… I=…" --why "…" · '
                 "dev crew history"))
    return 0


def _owner_cmd(a) -> int:
    """`dev crew owner <path>…`: who holds each path now, as the edit check decides it (progress.scope_holders):
    exit 0 when every path is free, 1 while a working member holds one. Reads only."""
    from . import progress
    cwd, now = os.getcwd(), time.time()
    agent, sid = progress.current_session(cwd)
    me = progress._as_lane(os.environ.get("DEVKIT_FROM_AGENT"), cwd) or "main"
    rows = []
    for p in a.paths:
        holders = progress.scope_holders(p, agent, sid, cwd, me=me, now=now)
        rows.append({"path": p, "free": not any(h["state"] == "working" for h in holders), "holders": holders})
    if a.owner_json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        print(progress.owner_text(rows, now))
    return 0 if all(r["free"] for r in rows) else 1


def cmd_crew(a) -> int:
    act = getattr(a, "crew_cmd", None)
    if act in ("ask", "answer", "inbox", "decide", "withdraw", "decisions", "mode", "route", "delegate", "decline",
               "next"):
        return _messages(a, act)
    try:
        roster = load()
    except RosterError as e:
        ui.err(str(e))
        return 2
    if act == "raci":
        return _raci_cmd(a)
    if act == "owner":
        return _owner_cmd(a)
    if act == "graph":
        return _graph(a, roster)
    if act == "set":
        try:
            m = set_member(a.name, a.field, " ".join(a.value), a.why or "")
        except ValueError as e:
            ui.err(str(e))
            return 2
        print(f"{ui.OK} {m['name']}: {a.field} changed for the whole team ({core.tilde(overlay_path())})")
        print(ui.dim("  the members get it in their briefs; Codex roles with dev install --only codex"))
        return 0
    if act == "rules":
        try:
            from . import progress
            run = progress.normalize(progress.load_run(*progress.current_session()) or {})
            steps = {s["name"] for s in run.get("plan") or []} or None
        except Exception:
            steps = None
        rows = enforcement(steps)
        mark = {"enforced": ui.OK, "planned": ui.WARN, "guidance": "·"}
        print(ui.table([[row[0], mark[st] + " " + st, rule, where, ref + ("" if ok else "  ✗ missing")]
                        for row, (rule, where, ref, st, ok) in zip(ENFORCEMENT, rows)],
                       ["ID", "STATE", "RULE", "WHERE", "CHECK OR PLAN STEP"], max_widths={2: 52, 3: 36, 4: 40}))
        n = {s: sum(1 for r in rows if r[3] == s) for s in ("enforced", "planned", "guidance")}
        print(ui.dim(f"  {n['enforced']} enforced · {n['planned']} planned · {n['guidance']} guidance"))
        return 0 if all(r[4] for r in rows) else 1
    if act == "history":
        log = read_overlay().get("changelog") or []
        if not log:
            print("  no team changes: the crew runs devkit's roster as shipped")
        for c in reversed(log[-40:]):
            print(f"  {c.get('at', '')}  {c.get('by', '')}  {c.get('what', '')}\n    why: {c.get('why', '')}")
        return 0
    if act == "sync":
        changes = sync(check=a.check, roster=load(team=False))
        for w in shown_warnings(roster):
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
            print(brief_text(m, " ".join(a.task), a.scope or "", a.step, roster, activity=a.activity))
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
    for w in shown_warnings(roster):
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
    x = sp.add_parser("raci", help="who does, approves, is consulted on and informed about each kind of work")
    x.add_argument("work", nargs="?", help="a kind of work (backend), a member (tank), or a task to classify")
    x.add_argument("--set", help='change it for the whole team: "R=tank,neo A=seraph C=keymaker I=trinity"')
    x.add_argument("--why", help="the reason (required with --set)")
    x.add_argument("--markdown", action="store_true", help="the whole matrix as a Markdown table")
    x = sp.add_parser("set", help="change a member's definition for the whole team (recorded in the handoff repo)")
    x.add_argument("name", help="the member: tank, Oracle")
    x.add_argument("field", help=", ".join(EDITABLE))
    x.add_argument("value", nargs="+", help='the new value; list fields take "a; b; c"; procedure takes JSON')
    x.add_argument("--why", required=True, help="the reason, kept in the crew's history")
    sp.add_parser("history", help="the team's changes to the crew, with who and why")
    sp.add_parser("rules", help="every crew rule, the devkit check that enforces it, and its state")
    x = sp.add_parser("graph", help="the live session as one graph: the tech lead, the run's steps and dependencies, "
                                    "the crew lanes and nested subagents, work items, and what waits")
    x.add_argument("--latest", dest="graph_latest", action="store_true",
                   help="the most recent running progress run on this Mac instead of this session's")
    x.add_argument("--mermaid", action="store_true", help="a Mermaid flowchart (renders on GitHub)")
    x.add_argument("--json", dest="graph_json", action="store_true",
                   help="the graph as nodes and edges, for the dashboard and tools")
    x = sp.add_parser("show", help="everything about one member")
    x.add_argument("name", nargs="+", help="id or name, e.g. neo, Oracle, \"The Keymaker\"")
    x = sp.add_parser("brief", help="a ready-to-paste brief for a member, with the spawn line")
    x.add_argument("name", help="id or name, e.g. neo or \"The Oracle\"")
    x.add_argument("task", nargs="+", help="what to do")
    x.add_argument("--scope", help="the files the member owns, comma-separated globs")
    x.add_argument("--step", help="the plan step it does (number or part of its name): prints the assign command")
    x.add_argument("--activity", help="the kind of work, an id from the crew's RACI: it picks the brief's skills, "
                                      "knowledge and pitfalls, in any language")
    x = sp.add_parser("sync", help="write plugin/agents/<id>.md from config/crew.json")
    x.add_argument("--check", action="store_true", help="only report the files that differ (exit 1 if any)")
    x = sp.add_parser("ask", help="a question to the tech lead (default) or a crew member; --decision: one for the "
                                  "developer")
    x.add_argument("text", nargs="+", help="the question or request")
    x.add_argument("--to", help="the tech lead (default), a crew name or a lane label")
    x.add_argument("--decision", action="store_true",
                   help="a decision point: in auto mode the recommendation is taken and recorded; otherwise it waits "
                        "for the developer")
    x.add_argument("--options", help="2 to 4 options, each with its letter, separated by |: \"A: rewrite the parser "
                                     "| B: patch it\" (an irreversible approval without them is A: yes | B: no)")
    x.add_argument("--recommend", help="the letter of the option you recommend, e.g. A (the developer sees it first)")
    x.add_argument("--irreversible", help="the irreversible action it leads to: " + ", ".join(_irreversible()))
    x.add_argument("--target", help="what that action acts on (a branch, a tag, a PR number): the developer's "
                                    "answer opens that action on that target once")
    x.add_argument("--performs", help="with --irreversible and --options: the option that performs the action; only "
                                      "the developer's choice of it opens the action (default: the one option that "
                                      "reads yes or approve, else the recommended one)")
    x.add_argument("--approves", help="with --decision: the finished steps the developer's answer approves, "
                                      "\"api, screens\" (checked against the plan)")
    x = sp.add_parser("answer", help="answer a crew message by its id")
    x.add_argument("id", help="the message id, e.g. M3")
    x.add_argument("text", nargs="+")
    sp.add_parser("inbox", help="messages for you not delivered yet")
    x = sp.add_parser("mode", help="guided (the developer decides) or auto (the crew goes on and records); "
                                   "no argument: the current mode")
    x.add_argument("mode", nargs="?", choices=["guided", "auto"])
    x.add_argument("--approve", help="irreversible actions auto mode may take, each with its target, comma-separated, "
                                     "e.g. \"push feat/*, release v26.11.4\"; each needs the developer's grant")
    x = sp.add_parser("decide", help="record the developer's answer to a decision (the tech lead only)")
    x.add_argument("id", help="the decision id, e.g. D3")
    x.add_argument("text", nargs="+", help="the answer")
    x = sp.add_parser("withdraw", help="close a pending decision that no longer matters, with the reason (the tech "
                                       "lead only; never the developer's answer)")
    x.add_argument("id", help="the decision id, e.g. D3")
    x.add_argument("--why", required=True, help="why it no longer matters (the run's history and the audit keep it)")
    x = sp.add_parser("decisions", help="this run's decisions; automatic ones are marked for review, and one whose "
                                        "asking lane ended and that blocks no step as moot")
    x.add_argument("--pending", action="store_true", help="only those waiting for the developer (moot ones are not)")
    x.add_argument("--ask", nargs="*", metavar="ID", help="the question tool calls that ask the developer these "
                   "decisions, every pending one without IDs: at most 4 per call, the recommended option first. "
                   "Reword a reversible one as you like, keeping “D<n> ·” up front and the option letters (their "
                   "evidence, spec C5); send an irreversible one word for word, alone in its call: it counts only in "
                   "devkit's exact call")
    x.add_argument("--json", dest="ask_json", action="store_true", help="with --ask: the tool input as JSON")
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
    x = sp.add_parser("decline", help="hand a delegated step back to the tech lead, with the reason")
    x.add_argument("step", nargs="+", help="the step (number or part of its name)")
    x.add_argument("--why", help="why you cannot take it")
    x = sp.add_parser("owner", help="who holds these files before you edit them: the crew member, run, session, step, "
                                    "state (working, stale, ended) and what to do; exit 1 while a working member "
                                    "holds one (reads only)")
    x.add_argument("paths", nargs="+", help="the files you are about to edit or assign")
    x.add_argument("--json", dest="owner_json", action="store_true", help="each path with its holders as JSON")
    x = sp.add_parser("next", help="the dispatcher: who to launch now and in what order, what is queued or waiting "
                                   "and why, who is busy, and the crew's slots")
    x.add_argument("--first", help="the tech lead: launch this step ahead of the order (with --why, recorded)")
    x.add_argument("--why", help="why that step goes ahead of the order")


def _irreversible() -> tuple:
    from . import progress
    return getattr(progress, "IRREVERSIBLE", ())
