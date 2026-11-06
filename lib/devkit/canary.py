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


def status_text(text: str) -> str:
    """The status line without the marker, segments joined by ` ❯ `.

    `[Firefly> DevKit]` `api ⎇ main` `T-1` ❯ fixed ❯ ✓ tests ❯ next → push
        -> "api ⎇ main ❯ T-1 ❯ fixed ❯ ✓ tests ❯ next → push"
    The legacy form (`DevKit · api@main · fixed · …`) is kept as written."""
    first = next((ln for ln in (text or "").splitlines() if ln.strip()), "")
    m = STATUS_RE.match(first.strip())
    if not m:
        return ""
    line = m.group(1).strip()
    if line.startswith("`") or "❯" in line:
        line = re.sub(r"`\s+`", SEP, line)                  # adjacent pills become segments
        line = re.sub(r"`\s*❯\s*", SEP, line)
    line = re.sub(r"\*\*|`", "", line)                     # bold / code markers, not underscores inside ids
    line = re.sub(r"\s*❯\s*", SEP, line)
    return line.strip(" ·*❯").strip()


def segments(line: str) -> list:
    """A saved status line as segments (new format split on ❯, legacy on ·)."""
    sep = "❯" if "❯" in line else "·"
    return [x.strip() for x in (line or "").split(sep) if x.strip()]


def format_issues(text: str) -> list:
    """What the status line misses against the format in the global instructions (empty = well formed)."""
    first = next((ln for ln in (text or "").splitlines() if ln.strip()), "").strip()
    issues = []
    if not re.match(r"^[*_\s]*`\[Firefly> DevKit\]`", first):
        issues.append("marker not written exactly as the `[Firefly> DevKit]` pill")
    segs = segments(status_text(first))
    if len(segs) < 4:
        issues.append("fewer than the expected segments (location, task, outcome, verification, next)")
    if segs and not re.search(r"\S\s*⎇\s*\S", segs[0]):
        issues.append("first pill is not `<project> ⎇ <branch>`")
    if not any(x[:1] in GLYPHS for x in segs):
        issues.append("no verification segment starting with ✓, ⚠, ✗ or –")
    if not any(x.lower().startswith("next") for x in segs):
        issues.append("no `next → …` segment")
    if len(first) > 260:
        issues.append("longer than one line (about 180 characters)")
    return issues


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
           "project": pid, "ok": ok}
    if ok:
        rec["status"] = line[:200]
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
        {"ts": round(time.time()), "agent": agent, "line": line[:300], "branch": branch}))


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


def summary(days: int = 7) -> dict:
    """{agent: (ok, total)} over the window."""
    out = {}
    for r in records(days):
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
    out = " · ".join(f"{a.capitalize()} {ok}/{tot} ({100 * ok // tot}%)" for a, (ok, tot) in sorted(s.items()))
    good, scored = format_rate(days)
    return out + (f" · format {100 * good // scored}%" if scored else "")


def prune(max_days: int = 90):
    if not LOG.exists():
        return
    keep = records(max_days)
    LOG.write_text("".join(json.dumps(r) + "\n" for r in keep))
