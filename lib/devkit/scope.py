# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Is this turn development work?

devkit's process (the status block, required skills, progress nudges, lesson reminders) is for development:
writing, reviewing, debugging, testing, building, deploying or releasing software, operating developer tools,
repositories, CI and devkit, and questions about a codebase. Anything else (a recipe, a general question,
consulting work done with the Firefly consulting kit) gets a normal answer and devkit stays out of the way.

Decided at Stop from what the turn did, never from the developer's words, which can be in any language (crew gap
review, C13):
  declared  the final message starts with the status block: the model declared development work
  commands  development commands (commits, pushes, tags, test and build runs, `dev`)
  edits     files changed inside a repository
  tools     repository tools (reading, searching, editing, a shell) in a session inside a repository
  none      a reply with no tool call and no status block ("how is it going?", "continue") takes the previous turn's
            decision; the first one inside a repository is development, so the canary scores its missing block
Consulting work done through the Firefly consulting kit's skills counts only through development commands, so a deck
built inside a repository stays consulting work.
"""
from __future__ import annotations

import json
import os
import re

from .hooks import SHELL_TOOLS  # Bash, Monitor and Codex's shells

CONSULTING_SKILL_RE = re.compile(r"\bfirefly-(consult|deck|document|pitch|cobrand|review|event|storytelling|proposal|"
                                 r"research|problem-solving|business-design|transformation|architecture|"
                                 r"tech-documentation|house-style)\b")

STRONG_CMD_RE = re.compile(
    r"\bgit\s+(-C\s+\S+\s+)?(commit|push|merge|tag|rebase|cherry-pick|revert|checkout\s+-b|switch\s+-c|worktree)\b|"
    r"\bgh\s+(pr|release|run|workflow)\b|"
    r"(^|[\s;&|(])(pytest|jest|vitest|mocha|tsc|eslint|ruff|mypy|phpunit|make|docker|kubectl|terraform|helm|"
    r"\./mvnw|mvn|\./gradlew|gradle|cargo|go\s+(test|build)|npm\s+(test|run|ci|install)|pnpm|yarn|composer|"
    r"python3?\s+-m\s+(unittest|pytest|compileall)|uv\s+(run|sync)|ng\s+(build|test|serve|generate))\b|"
    r"\bdev\s+(commit|push|pr|release|branch|rebase|ci|sec|docs|install|setup|wt|delivery|lesson|task|plan|"
    r"progress)\b")
WEAK_CMD_RE = re.compile(r"\bgit\b|\bdev\b|\bgh\b|\bnode\b|\bnpm\b|\bpython3?\b|\brg\b|\bgrep\b")
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"}
REPO_TOOLS = EDIT_TOOLS | set(SHELL_TOOLS) | {"Read", "Grep", "Glob", "LSP", "NotebookRead", "read_file", "list_dir",
                                       "grep", "view_image"}


def _tail_lines(path: str, max_bytes: int = 3_000_000) -> list:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()
            return fh.read().decode("utf-8", "replace").splitlines()
    except (OSError, TypeError):
        return []


def _is_prompt(rec: dict) -> bool:
    """A real user prompt (not a tool result) in a Claude or Codex transcript."""
    if rec.get("type") == "user":
        msg = rec.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            return True
        if isinstance(content, list):
            return not any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content)
    p = rec.get("payload") or {}
    return p.get("type") == "user_message"


def turn_actions(transcript_path: str) -> dict:
    """What the last turn did: {"edits": [paths], "commands": [str], "skills": [names], "tools": {names}}."""
    out = {"edits": [], "commands": [], "skills": [], "tools": set()}
    recs = []
    for ln in _tail_lines(transcript_path):
        try:
            recs.append(json.loads(ln))
        except ValueError:
            continue
    start = max((i for i, r in enumerate(recs) if _is_prompt(r)), default=-1)
    for r in recs[start + 1:]:
        calls = []
        for c in ((r.get("message") or {}).get("content") or []) if r.get("type") == "assistant" else []:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                calls.append((c.get("name") or "", c.get("input") or {}))
        p = r.get("payload") or {}
        if p.get("type") in ("function_call", "custom_tool_call"):
            args = p.get("arguments") if p.get("type") == "function_call" else p.get("input")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"input": args}
            calls.append((p.get("name") or "", args or {}))
        for name, inp in calls:
            out["tools"].add(name)
            if name in EDIT_TOOLS:
                fp = inp.get("file_path") or inp.get("notebook_path") or ""
                if not fp and isinstance(inp.get("input"), str):  # Codex apply_patch envelope
                    fp = " ".join(re.findall(r"\*\*\* (?:Add|Update|Delete) File: (\S+)", inp["input"]))
                out["edits"].append(fp)
            elif name in SHELL_TOOLS:
                cmd = inp.get("command") or inp.get("cmd") or ""
                out["commands"].append(" ".join(cmd) if isinstance(cmd, list) else str(cmd))
            elif name == "Skill":
                out["skills"].append(str(inp.get("skill") or ""))
    return out


def _in_repo(path: str) -> bool:
    d = os.path.dirname(os.path.abspath(os.path.expanduser(path))) if path else ""
    while d and d != os.path.dirname(d):
        if os.path.exists(os.path.join(d, ".git")):
            return True
        d = os.path.dirname(d)
    return False


def is_dev_turn(actions: dict, in_repo: bool = False, declared: bool = False, previous=None) -> bool:
    """What the turn did -> development work or not. `in_repo`: the session works inside a repository; `declared`:
    the final message starts with the status block; `previous`: the decision for this session's last turn (None: no
    turn yet). A reply with no tool call and no block gives no signal ("how is it going?", "continue"): it takes the
    previous turn's decision, and the first one inside a repository counts as development, so the canary scores it."""
    cmds = actions.get("commands") or []
    if not declared and not actions.get("tools") and not cmds and not actions.get("edits"):
        return bool(in_repo) if previous is None else bool(previous)
    strong = any(STRONG_CMD_RE.search(c) for c in cmds)
    consulting = any(CONSULTING_SKILL_RE.search(s or "") for s in actions.get("skills") or []) or \
        any("firefly" in t.lower() and t.startswith("mcp__") for t in actions.get("tools") or ())
    if consulting:
        return strong
    if declared or strong:
        return True
    if any(e and _in_repo(e) for e in actions.get("edits") or []):
        return True
    if any(WEAK_CMD_RE.search(c) for c in cmds):
        return True
    return in_repo and bool(set(actions.get("tools") or ()) & REPO_TOOLS)


def decide(data: dict, sess: dict) -> bool:
    """The Stop hook's question: should devkit's process (status block, skills) apply to the turn that just ended?
    The answer is kept in the session record (`scope_last`) for the next turn that gives no signal."""
    if os.environ.get("DEVKIT_SCOPE") == "all":
        return True
    path = data.get("transcript_path")
    try:
        from . import canary
        declared = canary.has_marker(canary.last_assistant_text(path) or "") if path else False
    except Exception:
        declared = False
    last = sess.get("scope_last")
    dev = is_dev_turn(turn_actions(path), bool(sess.get("top")), declared, None if last is None else last == "dev")
    sess["scope_last"] = "dev" if dev else "other"
    return dev
