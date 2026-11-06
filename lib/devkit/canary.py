# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
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
_MARK = r"(?:\[\s*firefly\s*>\s*devkit\s*\]|devkit\b)"
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


def has_marker(text: str) -> bool:
    first = next((ln for ln in (text or "").splitlines() if ln.strip()), "")
    return bool(MARKER_RE.match(first.strip()))


STATUS_RE = re.compile(r"^[*`_\s]*" + _MARK + r"[*`\s]*[·:—-]?\s*(.*)$", re.I)


SEP = " ❯ "
GLYPHS = "✓⚠✗–"
BULLET_RE = re.compile(r"^\s*[-*•]\s+\**\s*(done|check|progress|next)\s*\**\s*:\s*\**\s*(.*)$", re.I)


def _clean(s: str) -> str:
    return re.sub(r"\*\*|`", "", s or "").strip(" ·*").strip()


def parse_status(text: str):
    """The status block -> {project, branch, task, done, check, progress, next}, or None without the marker.

    **[Firefly> DevKit]** api · `main` · Invoice export
    - **Done:** …   - **Check:** ✓ …   - **Progress:** …   - **Next:** …"""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return None
    m = STATUS_RE.match(lines[0].strip())
    if not m:
        return None
    head = m.group(1).strip()
    out = {"project": "", "branch": "", "task": "", "done": "", "check": "", "progress": "", "next": "",
           "legacy": False}
    for ln in lines[1:7]:
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
    if any(len(p.get(k) or "") > 160 for k in ("done", "check", "next")):
        issues.append("a bullet is longer than one plain sentence")
    if re.search(r"\bT-[0-9a-z]{8,}\b", " ".join(p.get(k) or "" for k in ("task", "done", "next"))):
        issues.append("internal task IDs in the block (use the task's title)")
    return issues


SURFACES = {"cli": "terminal", "claude-desktop": "Claude Desktop", "claude-vscode": "VS Code", "sdk-ts": "SDK",
            "sdk-py": "SDK", "codex_cli_rs": "terminal", "codex_exec": "codex exec", "codex_vscode": "VS Code"}


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
        if r.get("corrected"):
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


def save_status(pid, agent: str, line: str, branch=None):
    if not pid or not line:
        return
    d = STATE / "status"
    d.mkdir(parents=True, exist_ok=True)
    (d / (pid.replace("/", "__") + ".json")).write_text(json.dumps(
        {"ts": round(time.time()), "agent": agent, "line": line[:700], "branch": branch}))


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


def summary(days: int = 7) -> dict:
    """{agent: (ok, total)} over the window, first replies only (corrections are counted apart)."""
    out = {}
    for r in [r for r in records(days) if not r.get("corrected")]:
        ok, tot = out.get(r["agent"], (0, 0))
        out[r["agent"]] = (ok + (1 if r["ok"] else 0), tot + 1)
    return out


def format_rate(days: int = 7):
    """(well formed, scored) over replies that carried the marker."""
    rs = [r for r in records(days) if r.get("ok") and "format_ok" in r]
    return sum(1 for r in rs if r["format_ok"]), len(rs)


def summary_line(days: int = 7) -> str:
    s = summary(days)
    if not s:
        return "no tracked replies yet"
    surf = by_surface(days)

    def where(a):
        parts = [(sf, ok, n) for (ag, sf), (ok, n) in sorted(surf.items()) if ag == a and sf != "unknown"]
        return f" [{', '.join(f'{sf} {ok}/{n}' for sf, ok, n in parts)}]" if len(parts) > 1 or (
            parts and parts[0][0] != "terminal") else ""
    out = " · ".join(f"{a.capitalize()} {ok}/{tot} ({100 * ok // tot}%){where(a)}" for a, (ok, tot) in sorted(s.items()))
    good, scored = format_rate(days)
    fixed = corrected(days)
    return out + (f" · format {100 * good // scored}%" if scored else "") + (f" · {fixed} corrected after a reminder"
                                                                            if fixed else "")


def prune(max_days: int = 90):
    if not LOG.exists():
        return
    keep = records(max_days)
    LOG.write_text("".join(json.dumps(r) + "\n" for r in keep))
