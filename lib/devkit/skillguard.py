# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Skill enforcement: the right skills are loaded for the work being done (config/skill-rules.json).

1. Prompt   UserPromptSubmit names the skills a request needs (rules matching the prompt) that are not loaded yet.
2. Brief    PreToolUse on Claude's Agent/Task tool appends the required skills to a subagent's brief (updatedInput).
3. Finish   Stop refuses to end a turn that changed files matching a rule while the rule's skills were never
            loaded in the session (once per skill per session), with the reason and the files.

Loaded skills are read from the session transcript: Claude's Skill tool calls and reads of SKILL.md files; Codex
shell commands that read a skill's SKILL.md. Everything fails open.
"""
from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path

from . import core
from .core import CFG

RULES_FILE = core.DEVKIT_HOME / "config" / "skill-rules.json"
SKILL_PATH_RE = re.compile(r"skills/([A-Za-z0-9_.-]+)/SKILL\.md")


def enabled() -> bool:
    return (CFG.get("skills") or {}).get("enforce", True)


def rules() -> list:
    base = (core.read_json(RULES_FILE, {}) or {}).get("rules", [])
    extra = (CFG.get("skills") or {}).get("rules") or []
    by_id = {r["id"]: r for r in base}
    for r in extra:  # user rules extend or replace devkit's by id
        if r.get("id"):
            by_id[r["id"]] = {**by_id.get(r["id"], {}), **r}
    return list(by_id.values())


def short(name: str) -> str:
    """`devkit:docs` / `frontend-design:frontend-design` -> `docs` / `frontend-design`."""
    return (name or "").split(":")[-1].strip().lower()


def installed(agent: str) -> set:
    """Skill names this agent can load (personal skills, linked skills and plugin skills on disk)."""
    roots = [core.CLAUDE_HOME / "skills", core.CLAUDE_HOME / "plugins" / "cache"] if agent == "claude" else \
        [core.HOME / ".agents" / "skills", core.CODEX_HOME / "skills", core.CODEX_HOME / "plugins" / "cache"]
    out = set()
    for root in roots:
        if not root.exists():
            continue
        for f in root.glob("**/SKILL.md") if "cache" in root.parts else root.glob("*/SKILL.md"):
            out.add(f.parent.name.lower())
    return out


def _match_path(path: str, pattern: str) -> bool:
    p = path.replace("\\", "/")
    if "/" in pattern:
        return fnmatch.fnmatch(p, pattern) or fnmatch.fnmatch(p, "*/" + pattern)
    return fnmatch.fnmatch(Path(p).name, pattern)


def rules_for_files(paths) -> list:
    hits = []
    for r in rules():
        files = [p for p in paths if any(_match_path(p, g) for g in r.get("files") or [])]
        if r.get("content") and r.get("content_files"):
            rx = re.compile(r["content"], re.I)
            for p in paths:
                if p in files or not any(_match_path(p, g) for g in r["content_files"]):
                    continue
                try:
                    if rx.search(Path(p).read_text(errors="ignore")[:400_000]):
                        files.append(p)
                except OSError:
                    pass
        elif r.get("content") and files:  # files match only when their content matches too (e.g. Spring code)
            rx = re.compile(r["content"], re.I)
            files = [p for p in files if _read_matches(p, rx)]
        if files:
            hits.append((r, files))
    return hits


def _read_matches(p, rx) -> bool:
    try:
        return bool(rx.search(Path(p).read_text(errors="ignore")[:400_000]))
    except OSError:
        return True  # deleted or unreadable: judge by the path


def rules_for_text(text: str) -> list:
    out = []
    for r in rules():
        if r.get("prompt") and re.search(r["prompt"], text or "", re.I):
            out.append(r)
    return out


def required(rule: dict, agent: str) -> list:
    return [short(s) for s in (rule.get("skills") or {}).get(agent, [])]


def how_to_load(agent: str) -> str:
    return "with the Skill tool" if agent == "claude" else "by reading its SKILL.md (listed among your skills)"


# --------------------------------------------------------------------------- transcript scan

def scan(agent: str, transcript: str, offset: int = 0):
    """-> (loaded skill names, changed file paths, new offset) from the transcript since `offset`."""
    loaded, files = set(), []
    try:
        p = Path(transcript)
        size = p.stat().st_size
        if offset > size:
            offset = 0
        with p.open("rb") as fh:
            fh.seek(offset)
            data = fh.read().decode("utf-8", "ignore")
    except (OSError, TypeError):
        return loaded, files, offset
    for ln in data.splitlines():
        if agent == "claude":
            if '"tool_use"' not in ln:
                continue
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            for c in (d.get("message") or {}).get("content") or []:
                if not isinstance(c, dict) or c.get("type") != "tool_use":
                    continue
                name, inp = c.get("name"), c.get("input") or {}
                if name == "Skill" and inp.get("skill"):
                    loaded.add(short(inp["skill"]))
                fp = inp.get("file_path") or inp.get("notebook_path") or ""
                if name == "Read" and fp.endswith("SKILL.md"):
                    loaded.add(Path(fp).parent.name.lower())
                if name in ("Write", "Edit", "MultiEdit", "NotebookEdit") and fp:
                    files.append(fp)
                if name == "Bash":  # cat/sed of a SKILL.md counts as loading it
                    for m in SKILL_PATH_RE.finditer(inp.get("command") or ""):
                        loaded.add(m.group(1).lower())
        else:
            if "function_call" not in ln and "custom_tool_call" not in ln and "exec_command" not in ln:
                continue
            for m in SKILL_PATH_RE.finditer(ln.replace("\\/", "/")):
                loaded.add(m.group(1).lower())
            for m in re.finditer(r"\*\*\* (?:Add|Update) File: ([^\\\n\"]+)", ln):
                files.append(m.group(1).strip())
    return loaded, files, offset + len(data.encode("utf-8"))


# --------------------------------------------------------------------------- hook entry points

def on_prompt(agent: str, prompt: str, sess: dict) -> str:
    if not enabled():
        return ""
    have = installed(agent)
    loaded = set(sess.get("skills_loaded") or [])
    lines = []
    for r in rules_for_text(prompt):
        need = [s for s in required(r, agent) if s not in loaded and s in have]
        if need:
            lines.append(f"- {', '.join(need)} — {r['why']}")
    if not lines:
        return ""
    return ("devkit · skills this request needs (load each " + how_to_load(agent) + " before you start, and "
            "follow it; if a subagent does the work, its brief must name them):\n" + "\n".join(dict.fromkeys(lines)))


def on_agent_brief(agent: str, tool_input: dict, project: str = ""):
    """Claude Agent/Task: the brief gets the skills its work requires and the known pitfalls (lessons) for its task.
    -> updated tool input, or None."""
    if not enabled() or agent != "claude":
        return None
    brief = tool_input.get("prompt") or ""
    if "devkit · required skills" in brief:
        return None
    have = installed("claude")
    need = []
    for r in rules_for_text(brief + " " + (tool_input.get("description") or "")):
        for s in required(r, "claude"):
            if s in have and not re.search(rf"\b{re.escape(s)}\b", brief, re.I):
                need.append((s, r["why"]))
    extra = ""
    if need:
        seen = dict.fromkeys(need)
        extra = "\n\n## devkit · required skills\nBefore you start, load each of these with the Skill tool and follow " \
                "it (if the Skill tool is not available, read its SKILL.md under ~/.claude):\n" + \
                "\n".join(f"- `{s}` — {why}" for s, why in seen) + \
                "\nSay in your report which ones you loaded and how you applied them."
    if project and "devkit · known pitfalls" not in brief and "## Known pitfalls" not in brief:
        try:
            from . import lessons
            picked = lessons.for_task(project, brief[:4000] + " " + (tool_input.get("description") or ""))
        except Exception:
            picked = []
        if picked:
            extra += ("\n\n## devkit · known pitfalls\nLessons from earlier mistakes in this work; follow their "
                      "prevention. If one happens again, run `dev lesson recur <id> --evidence \"…\"`; a new mistake "
                      "that could repeat gets `dev lesson add \"…\" --cause \"…\" --prevent \"…\"`.\n"
                      + "\n".join(lessons.pitfall_lines(picked)))
    if "Firefly> DevKit" not in brief:  # every crew member reports the same way (the SubagentStop hook checks it)
        name = (tool_input.get("description") or "").split(":")[0].strip() or "Agent"
        extra += (f"\n\n## devkit · your report\nStart your final report with your status block: **[Firefly> DevKit · "
                  f"{name[:30]}]** <project> · `<branch>` · <task>, then - **Done:**, - **Check:** (✓/⚠/✗ + "
                  f"evidence) and - **Next:**. Record progress with `dev progress step \"…\"` (it goes to your own "
                  f"lane) and ask Morpheus with `dev crew ask \"…\"` when you need a decision.")
    if not extra:
        return None
    return {**tool_input, "prompt": brief + extra}


def on_stop(agent: str, data: dict, sess: dict):
    """-> reason to continue the turn (missing skills for the files it changed), or None. Updates `sess`."""
    if not enabled():
        return None
    transcript = data.get("transcript_path")
    if not transcript:
        return None
    loaded, files, offset = scan(agent, transcript, int(sess.get("skills_offset") or 0))
    sess["skills_offset"] = offset
    sess["skills_loaded"] = sorted(set(sess.get("skills_loaded") or []) | loaded)
    changed = sorted(set(sess.get("files_changed") or []) | set(files))
    sess["files_changed"] = changed[-300:]
    if data.get("stop_hook_active") or not files:
        return None
    have = installed(agent)
    nudged = set(sess.get("skills_nudged") or [])
    missing = []
    for r, matched in rules_for_files(files):
        for s in required(r, agent):
            if s in have and s not in sess["skills_loaded"] and s not in nudged:
                missing.append((s, r["why"], matched))
    if not missing:
        return None
    sess["skills_nudged"] = sorted(nudged | {m[0] for m in missing})
    parts = []
    for s, why, matched in missing:
        names = ", ".join(Path(f).name for f in matched[:3]) + (" …" if len(matched) > 3 else "")
        parts.append(f"`{s}` ({why}) for {names}")
    return ("devkit: you changed files that need skills you have not loaded in this session: " + "; ".join(parts) +
            f". Load each {how_to_load(agent)}, review your change against it, fix what it finds, then finish.")


def coverage(agent: str) -> list:
    """Rules whose required skills are not installed for this agent: [(rule id, [missing])]."""
    have = installed(agent)
    out = []
    for r in rules():
        miss = [s for s in required(r, agent) if s not in have]
        if miss:
            out.append((r["id"], miss))
    return out
