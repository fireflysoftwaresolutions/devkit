# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Instruction canary ("Simon says").

The global instructions (agents/AGENTS.md, section 0) tell every agent to start
the final message of each turn with `[Firefly> DevKit]`. That rule exists ONLY in the
global instructions — it is deliberately not repeated by the session-start
hook — so a reply without the marker means the global instructions were not
loaded or not followed.

The Stop hook calls `check()` after every turn of sessions working under the
dev root, records the result in ~/.local/state/devkit/canary.jsonl, and (Claude)
shows the user a warning when the marker is missing. `dev canary`, the dashboard,
`dev doctor` and PROJECTS.md report the compliance rate.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import core
from .core import DEV, STATE

MARKER = "[Firefly> DevKit]"
# The marker, optionally wrapped in ** or backticks. The legacy `DevKit ·` (before 26.11.0) is still accepted, so
# sessions that loaded the older instructions are not reported as misses.
_MARK = r"(?:\[\s*firefly\s*>\s*devkit(?:\s*·\s*[^\]\n]{1,80})?\s*\]|devkit\b)"  # crew: [Firefly> DevKit · Neo]
CREW_RE = re.compile(r"^[*`_\s]*\[\s*firefly\s*>\s*devkit\s*·\s*([^\]\n]{1,80}?)\s*\]", re.I)
MARKER_RE = re.compile(r"^[*`_\s]*" + _MARK, re.I)
LOG = STATE / "canary.jsonl"


def last_assistant_text(transcript_path: str):
    """Last assistant text block of a Claude Code transcript (JSONL), reading from the end."""
    try:
        p = Path(transcript_path)
        size = p.stat().st_size
        with p.open("rb") as f:
            f.seek(max(0, size - 400_000))
            lines = f.read().decode("utf-8", "ignore").splitlines()
    except (OSError, TypeError):
        return None
    for ln in reversed(lines):
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if e.get("type") == "user" and not e.get("isMeta") and _is_human_turn(e):
            return None  # reached the user's prompt without finding assistant text
        if e.get("type") != "assistant":
            continue
        content = (e.get("message") or {}).get("content")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            texts = [t for t in texts if t.strip()]
            if texts:
                return texts[-1]
    return None


def _is_human_turn(e) -> bool:
    content = (e.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return any(isinstance(c, dict) and c.get("type") == "text" for c in content)
    return False


def miss_reason(text: str) -> str:
    """Why a final message failed the canary, in words the agent can act on at once."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    first = re.sub(r"\s+", " ", lines[0].strip())[:90] if lines else ""
    at = next((i for i, ln in enumerate(lines) if MARKER_RE.match(ln.strip())), None)
    if at:
        return (f"Your final message has the status block, but {at} line(s) come before it, starting with \"{first}\". "
                "Nothing goes above the block, not a verdict, a correction or a comment on the last command.")
    return (f"Your final message has no status block; it starts with \"{first}\". Every final message starts with it, "
            "including short answers and updates while background work runs.")


def has_marker(text: str) -> bool:
    first = next((ln for ln in (text or "").splitlines() if ln.strip()), "")
    return bool(MARKER_RE.match(first.strip()))


# The role check (the developer, M346; lesson L-fy2lfvacp64): a final message names a crew member as
# `<role> (<Name>)`, the role in any language, so the Matrix names never stand alone. It reads the structure only: the
# roster's name inside parentheses right after other words. Code blocks, code spans and links are not prose.
_NOT_PROSE = (re.compile(r"(?ms)^[ \t]*(`{3,}|~{3,}).*?(?:^[ \t]*\1[ \t]*$|\Z)"),  # fenced code blocks
              re.compile(r"(`+)[^\n]*?\1"),  # code spans, commands among them
              re.compile(r"\b[a-z][a-z0-9+.-]*://\S+"))  # links
_ROLE_BEFORE = re.compile(r"[\w)\]*]\s*[(（]$")  # "Backend engineer (", "バックエンドエンジニア（"
_ROLE_AFTER = re.compile(r"[)）]")


def _prose(text: str) -> str:
    for pat in _NOT_PROSE:
        text = pat.sub(" ", text)
    return text


def _name_patterns(roster=None) -> list:
    """[(compiled pattern, member name)] for every crew member, the main one too (the developer's decision: the
    tech lead is "Tech lead (Morpheus)" or the role alone, and reports end with "Needs from the tech lead:"): the
    name and each alias as written, a leading "The " optional or lowercase. A name inside a path, a file name or a
    longer word is not one."""
    from . import crew
    out = []
    for m in crew.members(roster):
        for n in [m.get("name")] + list(crew.aliases(m)):
            if not isinstance(n, str) or not n.strip():
                continue
            n = n.strip()
            core_ = n[4:] if n.startswith("The ") else n
            pat = r"\s+".join(re.escape(w) for w in core_.split())
            if core_ != n:
                pat = r"(?:[Tt]he\s+)?" + pat
            out.append((re.compile(r"(?<![\w/\\.@-])" + pat + r"(?![\w/\\@-]|\.\w)"), m["name"]))
    return out


def bare_names(text: str, roster=None) -> list:
    """The crew members the prose of a final message names outside `<role> (<Name>)` -> their roster names, in the
    order they first appear. The status block's header and Crew line are prose too. [] when the roster is
    unreadable (fails open)."""
    try:
        from . import crew
        pats = _name_patterns(roster)
        forms = [crew.who(m) for m in crew.members(roster) if m.get("title")]
    except Exception:
        return []
    prose = _prose(text or "")
    for form in forms:  # a title may hold a name the member answers to: "Lookup scout (Sparks)", alias scout
        prose = prose.replace(form, " " * len(form))
    found = []
    for pat, name in pats:
        for hit in pat.finditer(prose):
            if _ROLE_BEFORE.search(prose[:hit.start()]) and _ROLE_AFTER.match(prose, hit.end()):
                continue
            found.append((hit.start(), name))
    return list(dict.fromkeys(name for _, name in sorted(found)))


def roles_reason(names: list, roster=None) -> str:
    """What to fix, with each name's role form from the roster ("Backend engineer (Tank)")."""
    from . import crew
    forms = []
    for n in names:
        m = crew.member(n, roster)
        forms.append(crew.who(m) if m else f"<role> ({n})")
    return (f"names crew members without their role: {', '.join(names)}. Write each one as <role> (<Name>), the role "
            f"in the message's language, for example {', '.join(forms)}, or the role alone; the status block's "
            f"header and Crew line too. Names in code spans and commands are fine.")


STATUS_RE = re.compile(r"^[*`_\s]*" + _MARK + r"[*`\s]*[·:—-]?\s*(.*)$", re.I)


SEP = " ❯ "
GLYPHS = "✓⚠✗–"
BULLET_RE = re.compile(r"^\s*[-*•]\s+\**\s*(done|check|progress|crew|you|next)\s*\**\s*:\s*\**\s*(.*)$", re.I)


def _clean(s: str) -> str:
    return re.sub(r"\*\*|`", "", s or "").strip(" ·*").strip()


def parse_status(text: str):
    """The status block -> {project, branch, task, done, check, progress, crew, you, next}, or None without the
    marker. Progress, Crew and You are optional.

    **[Firefly> DevKit]** api · `main` · Invoice export
    - **Done:** …   - **Check:** ✓ …   - **Progress:** …   - **Crew:** …   - **You:** …   - **Next:** …"""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return None
    m = STATUS_RE.match(lines[0].strip())
    if not m:
        return None
    head = m.group(1).strip()
    out = {"project": "", "branch": "", "task": "", "done": "", "check": "", "progress": "", "crew": "", "you": "",
           "next": "", "legacy": False}
    for ln in lines[1:9]:
        b = BULLET_RE.match(ln)
        if not b:
            break
        out[b.group(1).lower()] = _clean(b.group(2))
    if "❯" in head or not any(out[k] for k in ("done", "check", "next")):
        out["legacy"] = True  # one-line forms used before the status block
        out["done"] = _clean(re.sub(r"`\s+`", SEP, head))
        return out
    parts = [p.strip() for p in _clean(head).split(" · ")]
    out["project"], out["branch"] = (parts + ["", ""])[:2]
    out["task"] = " · ".join(parts[2:])
    return out


def status_text(text: str) -> str:
    """One line for logs and views: `api · main ❯ Invoice export ❯ <done> ❯ ✓ <check> [❯ <progress>] ❯ next → <next>`.
    Older one-line forms are returned as written."""
    p = parse_status(text)
    if not p:
        return ""
    if p["legacy"]:
        line = re.sub(r"\s*❯\s*", SEP, p["done"])
        return line.strip(" ·*❯").strip()
    parts = [f"{p['project'] or '-'} · {p['branch'] or '-'}", p["task"] or "-", p["done"], p["check"]]
    if p["progress"]:
        parts.append("▸ " + p["progress"])
    if p.get("crew"):
        parts.append("crew: " + p["crew"])
    if p.get("you"):
        parts.append("you → " + p["you"])
    if p["next"]:
        parts.append("next → " + p["next"])
    return SEP.join(x for x in parts if x)


def segments(line: str) -> list:
    """A saved status line as segments (current and segmented forms split on ❯, the oldest on ·)."""
    sep = "❯" if "❯" in line else "·"
    return [x.strip() for x in (line or "").split(sep) if x.strip()]


def format_issues(text: str) -> list:
    """What the status block misses against the format in the global instructions (empty = well formed)."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    first = lines[0].strip() if lines else ""
    issues = []
    if not re.match(r"^\*\*\[Firefly> DevKit\]\*\*", first):
        issues.append("marker not written exactly as **[Firefly> DevKit]**")
    p = parse_status(text) or {}
    if p.get("legacy") or not p:
        issues.append("one line instead of the header and Done / Check / Next bullets")
        return issues
    if not (p.get("project") and p.get("branch")):
        issues.append("header lacks `<project> · <branch>`")
    for k in ("done", "check", "next"):
        if not p.get(k):
            issues.append(f"no **{k.capitalize()}:** bullet")
    if p.get("check") and p["check"][:1] not in GLYPHS:
        issues.append("Check does not start with ✓, ⚠, ✗ or –")
    if any(len(p.get(k) or "") > 160 for k in ("done", "check", "crew", "you", "next")):
        issues.append("a bullet is longer than one plain sentence")
    if re.search(r"\bT-[0-9a-z]{8,}\b", " ".join(p.get(k) or "" for k in ("task", "done", "next"))):
        issues.append("internal task IDs in the block (use the task's title)")
    return issues


SURFACES = {"cli": "terminal", "claude-desktop": "Claude Desktop", "claude-vscode": "VS Code", "sdk-ts": "SDK",
            "sdk-py": "SDK", "sdk-cli": "claude -p", "codex_cli_rs": "terminal", "codex_exec": "codex exec", "codex_vscode": "VS Code"}


def surface(agent: str, data: dict = None) -> str:
    """Where the session runs: the terminal, Claude Desktop's Code tab, the ChatGPT/Codex app, an IDE.
    Claude Code says it in CLAUDE_CODE_ENTRYPOINT; Codex in the transcript's session_meta originator."""
    if agent == "claude":
        ep = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "")
        return SURFACES.get(ep, ep or "unknown")
    tp = (data or {}).get("transcript_path")
    try:
        with open(tp) as fh:
            meta = (json.loads(fh.readline()).get("payload") or {})
        orig = meta.get("originator") or ""
    except (OSError, TypeError, ValueError):
        return "unknown"
    if orig in SURFACES:
        return SURFACES[orig]
    return "ChatGPT app" if "desktop" in orig.lower() or "chatgpt" in orig.lower() else (orig or "unknown")


def by_surface(days: int = 7) -> dict:
    """{(agent, surface): (ok, total)} over the window, first replies only."""
    out = {}
    for r in records(days):
        if r.get("corrected") or r.get("kind") == "crew":
            continue
        k = (r.get("agent"), r.get("surface") or "unknown")
        ok, n = out.get(k, (0, 0))
        out[k] = (ok + bool(r.get("ok")), n + 1)
    return out


def check(agent: str, data: dict):
    """-> None when not applicable, else (ok, project_id, status_line). Records the result."""
    if data.get("stop_hook_active"):
        return None
    cwd = data.get("cwd") or os.getcwd()
    if not core.is_under(cwd, DEV):
        return None
    text = data.get("last_assistant_message")
    if not text:
        text = last_assistant_text(data.get("transcript_path"))
    if not text:
        return None
    ok = has_marker(text)
    top = core.gitout(cwd, "rev-parse", "--show-toplevel")
    pid = None
    if top and not core.is_temp(top):
        from .registry import main_of
        main = main_of(Path(top)) or Path(top)
        pid = core.project_identity(main, core.gitout(main, "config", "--get", "remote.origin.url"))[0]
    line = status_text(text) if ok else ""
    rec = {"ts": round(time.time()), "agent": agent, "session": (data.get("session_id") or "")[:12],
           "project": pid, "ok": ok, "surface": surface(agent, data)}
    if ok:
        rec["status"] = line[:500]
        issues = format_issues(text)
        rec["format_ok"] = not issues
        if issues:
            rec["format"] = issues[:3]
    else:
        rec["excerpt"] = re.sub(r"\s+", " ", text.strip())[:120]
        rec["instructions_loaded"] = instructions_loaded(agent, data.get("session_id") or "")
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass
    return ok, pid, line


def instructions_loaded(agent: str, sid: str):
    """True/False when the InstructionsLoaded hook recorded (or not) the global file; None when unknown (Codex)."""
    f = STATE / "instructions" / f"{agent}-{sid}.jsonl"
    if agent != "claude" or not f.exists():
        return None
    want = str(core.CLAUDE_HOME / "CLAUDE.md")
    return any(want in ln for ln in f.read_text().splitlines())


STATUS_MAX = 8000  # characters of a saved status block: a real block is far shorter; this only bounds a runaway one


def bounded(line: str, limit: int = STATUS_MAX) -> str:
    """The whole line when it fits; else cut at the last word that fits, with "…" (never inside a word)."""
    line = line or ""
    if len(line) <= limit:
        return line
    cut = line[:limit - 2]
    cut = cut[:cut.rfind(" ")] if " " in cut else cut
    return cut.rstrip() + " …"


def save_status(pid, agent: str, line: str, branch=None, path=None):
    """The latest status block of a project, whole (dev status shows every bullet). `path` is the checkout the
    session worked in, so dev status can say when an update came from a worktree or a copy."""
    if not pid or not line:
        return
    d = STATE / "status"
    d.mkdir(parents=True, exist_ok=True)
    rec = {"ts": round(time.time()), "agent": agent, "line": bounded(line), "branch": branch}
    if path:
        rec["path"] = str(path)
    (d / (pid.replace("/", "__") + ".json")).write_text(json.dumps(rec))


def parse_line(line: str) -> dict:
    """A saved status line -> {project, branch, task, done, check, progress, crew, you, next}; {} when it is not in
    the status-block form (the older one-line forms)."""
    segs = segments(line or "")
    if not (segs and " · " in segs[0] and len(segs) >= 3):
        return {}
    where, task, rest = segs[0], segs[1], segs[2:]
    project, _, branch = where.partition(" · ")
    out = {"project": project.strip(), "branch": branch.strip(), "task": "" if task == "-" else task, "done": "",
           "check": "", "progress": "", "crew": "", "you": "", "next": ""}
    done = []
    for x in rest:
        low = x.lower()
        if low.startswith("next →"):
            out["next"] = x[len("next →"):].strip()
        elif low.startswith("you →"):
            out["you"] = x.split("→", 1)[1].strip()
        elif low.startswith("crew:"):
            out["crew"] = x[len("crew:"):].strip()
        elif x.startswith("▸") and not out["progress"]:
            out["progress"] = x[1:].strip()
        elif x[:1] in GLYPHS and not out["check"]:
            out["check"] = x
        else:
            done.append(x)
    out["done"] = " ".join(done)
    return out


def miss_message(agent: str, sid: str) -> str:
    loaded = instructions_loaded(agent, sid)
    why = (" ~/.claude/CLAUDE.md was loaded, so the reply ignored the global instructions." if loaded else
           " ~/.claude/CLAUDE.md was NOT loaded in this session — run `dev doctor`." if loaded is False else "")
    return (f"⚠ {MARKER} canary: this reply did not start with “{MARKER}”.{why} `dev canary` shows the history.")


def records(days: int = 7) -> list:
    if not LOG.exists():
        return []
    cutoff = time.time() - days * 86400
    out = []
    for ln in LOG.read_text().splitlines():
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get("ts", 0) >= cutoff:
            out.append(r)
    return out


def record_corrected(agent: str, sid: str, pid, line: str = ""):
    """A missed reply that the agent corrected after the Stop hook's reminder (kept apart from first-time results)."""
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(json.dumps({"ts": round(time.time()), "agent": agent, "session": (sid or "")[:12], "project": pid,
                                "ok": True, "corrected": True, "status": (line or "")[:200]}) + "\n")
    except OSError:
        pass


def corrected(days: int = 7) -> int:
    return sum(1 for r in records(days) if r.get("corrected"))


def check_subagent(agent: str, data: dict, sess: dict, pid=None, member: str = "") -> bool:
    """SubagentStop: does the crew member's report start with its own status block (**[Firefly> DevKit · Neo]**)?
    Records the result next to the main sessions' results (kind "crew"). -> ok"""
    text = data.get("last_assistant_message") or last_assistant_text(data.get("agent_transcript_path")) or ""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    ok = bool(lines) and bool(MARKER_RE.match(lines[0].strip()))
    rec = {"ts": round(time.time()), "agent": agent, "kind": "crew", "session": (data.get("session_id") or "")[:12],
           "project": pid, "ok": ok, "surface": sess.get("surface") or surface(agent, data),
           "member": member or (data.get("agent_type") or "subagent"), "subagent": (data.get("agent_id") or "")[:16]}
    if ok:
        rec["status"] = status_text(text)[:300]
    else:
        rec["excerpt"] = re.sub(r"\s+", " ", text.strip())[:120]
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass
    return ok


def by_member(days: int = 7) -> dict:
    """{crew member: (ok, total)}: the reports subagents ended with."""
    out = {}
    for r in records(days):
        if r.get("kind") == "crew" and not r.get("corrected") and not r.get("internal"):
            ok, n = out.get(r.get("member") or "subagent", (0, 0))
            out[r.get("member") or "subagent"] = (ok + bool(r.get("ok")), n + 1)
    return out


def stop_blocks(days: int = 7) -> dict:
    """The Stop hook's blocks and the reminders that made one unnecessary (threaded turn endings, R4) ->
    {"blocked": {(day, rule): n}, "prevented": {(day, rule): n}}, from the audit trail. The day is the developer's
    local one, as in the deviations list (the record's `time` is UTC)."""
    from . import audit
    out = {"blocked": {}, "prevented": {}}
    for action, key in (("stop.block", "blocked"), ("stop.prevented", "prevented")):
        for r in audit.records(days, action):
            rule = (r.get("evidence") or {}).get("rule") or str(r.get("detail") or "?").split(" · ")[0]
            k = (time.strftime("%Y-%m-%d", time.localtime(r.get("ts") or 0)), rule)
            out[key][k] = out[key].get(k, 0) + 1
    return out


def stop_block_lines(days: int = 7) -> list:
    """`dev canary`'s Stop hook section: the turns sent back per day and rule, and the reminders that prevented one."""
    st = stop_blocks(days)
    out = []
    for key, head in (("blocked", "sent back"), ("prevented", "prevented by a reminder")):
        by_day = {}
        for (day, rule), n in sorted(st[key].items()):
            by_day.setdefault(day, []).append(f"{rule} {n}")
        out += [f"  {head:<24} {day}  " + " · ".join(parts) for day, parts in sorted(by_day.items())]
    return out or ["  none: no turn was sent back by the Stop hook"]


def summary(days: int = 7) -> dict:
    """{agent: (ok, total)} over the window: main sessions' first replies (corrections and crew reports apart)."""
    out = {}
    for r in [r for r in records(days) if not r.get("corrected") and r.get("kind") != "crew"]:
        ok, tot = out.get(r["agent"], (0, 0))
        out[r["agent"]] = (ok + (1 if r["ok"] else 0), tot + 1)
    return out


def format_rate(days: int = 7):
    """(well formed, scored) over replies that carried the marker."""
    rs = [r for r in records(days) if r.get("ok") and "format_ok" in r]
    return sum(1 for r in rs if r["format_ok"]), len(rs)


def summary_line(days: int = 7) -> str:
    s = summary(days)
    if not s and not by_member(days):
        return "no tracked replies yet"
    surf = by_surface(days)

    def where(a):
        parts = [(sf, ok, n) for (ag, sf), (ok, n) in sorted(surf.items()) if ag == a and sf != "unknown"]
        return f" [{', '.join(f'{sf} {ok}/{n}' for sf, ok, n in parts)}]" if len(parts) > 1 or (
            parts and parts[0][0] != "terminal") else ""
    out = " · ".join(f"{a.capitalize()} {ok}/{tot} ({100 * ok // tot}%){where(a)}" for a, (ok, tot) in sorted(s.items()))
    good, scored = format_rate(days)
    fixed = corrected(days)
    crew = by_member(days)
    if crew:
        cok, cn = sum(v[0] for v in crew.values()), sum(v[1] for v in crew.values())
        out += f" · crew {cok}/{cn}"
    return out + (f" · format {100 * good // scored}%" if scored else "") + (f" · {fixed} corrected after a reminder"
                                                                            if fixed else "")


def prune(max_days: int = 90):
    if not LOG.exists():
        return
    keep = records(max_days)
    LOG.write_text("".join(json.dumps(r) + "\n" for r in keep))
