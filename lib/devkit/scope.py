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

from .canary import CODEX_TURN_START
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
    """The start of a turn: a real user prompt (not a tool result) in a Claude transcript, or the event a Codex
    rollout opens each turn with (canary.CODEX_TURN_START)."""
    if rec.get("type") == "user":
        msg = rec.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            return True
        if isinstance(content, list):
            return not any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content)
    p = rec.get("payload") or {}
    return rec.get("type") == "event_msg" and p.get("type") in CODEX_TURN_START


# Codex code mode (on by default since 0.167): the model calls one tool, `exec`, whose input is JavaScript that awaits
# nested tools, `text(await tools.exec_command({cmd: "git push"}))` and `tools.apply_patch("*** Begin Patch ...")`.
_EXEC_CALL_RE = re.compile(r"\btools\s*\.\s*(exec_command|apply_patch)\s*\(\s*")
_JS_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0", "\n": ""}
_JS_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")


def _js_string(src: str, i: int):
    """(value, end) of the JavaScript string literal at src[i]; (None, i) when there is none (or it never ends), and
    (None, end) for a template literal with a ${...} substitution, whose value exists only at run time."""
    q = src[i:i + 1]
    if q not in ("'", '"', "`"):
        return None, i
    out, j, built = [], i + 1, False
    while j < len(src):
        c = src[j]
        if c == q:
            if built:
                return None, j + 1
            return "".join(out).encode("utf-16", "surrogatepass").decode("utf-16", "replace"), j + 1
        if c == "\\" and j + 1 < len(src):
            n = src[j + 1]
            hexits = {"u": 4, "x": 2}.get(n, 0)
            if hexits and re.fullmatch(r"[0-9a-fA-F]+", src[j + 2:j + 2 + hexits] or "-"):
                out.append(chr(int(src[j + 2:j + 2 + hexits], 16)))
                j += 2 + hexits
                continue
            out.append(_JS_ESCAPES.get(n, n))
            j += 2
            continue
        if q == "`" and src.startswith("${", j):  # skip the substitution, braces balanced
            built, j, depth = True, j + 2, 1
            while j < len(src) and depth:
                depth += {"{": 1, "}": -1}.get(src[j], 0)
                j += 1
            continue
        out.append(c)
        j += 1
    return None, i


def _js_field(src: str, i: int, field: str):
    """The string value of `field` in the object literal at src[i] ("{"), or None (no such field, or not a literal
    string)."""
    if src[i:i + 1] != "{":
        return None
    j, depth = i + 1, 0
    while j < len(src):
        while j < len(src) and (src[j].isspace() or src[j] == ","):
            j += 1
        if src[j:j + 1] in ("}", ""):
            return None
        key, end = _js_string(src, j)
        if key is None:
            m = _JS_IDENT_RE.match(src, j)
            if not m:
                return None
            key, end = m.group(0), m.end()
        j = end
        while j < len(src) and src[j].isspace():
            j += 1
        if src[j:j + 1] != ":":
            return None  # a shorthand or spread property: the value is not in the text
        j += 1
        while j < len(src) and src[j].isspace():
            j += 1
        value, end = _js_string(src, j)
        if key == field:
            return value
        if value is not None:
            j = end
            continue
        while j < len(src):  # any other value: up to the next comma or the closing brace at this depth
            c = src[j]
            if c in "'\"`":
                _, end = _js_string(src, j)
                j = end if end > j else j + 1
                continue
            if c in "([{":
                depth += 1
            elif c in ")]}":
                if depth == 0:
                    break
                depth -= 1
            elif c == "," and depth == 0:
                break
            j += 1
    return None


def exec_calls(script: str) -> list:
    """The nested shell and patch calls of a Codex code-mode `exec` script, in order: [("exec_command", {"cmd": …}),
    ("apply_patch", {"input": patch})]. A call whose argument is not a literal (a variable, a template built at run
    time) is left out: its text is not in the transcript."""
    out = []
    for m in _EXEC_CALL_RE.finditer(script or ""):
        if m.group(1) == "apply_patch":
            patch, _ = _js_string(script, m.end())
            if patch is not None:
                out.append(("apply_patch", {"input": patch}))
            continue
        cmd = _js_field(script, m.end(), "cmd")
        if cmd is not None:
            out.append(("exec_command", {"cmd": cmd}))
    return out


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
            if p.get("name") == "exec" and isinstance(args, str):  # code mode: the script's nested calls
                out["tools"].add("exec")
                calls.extend(exec_calls(args))
                args = None
            elif isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"input": args}
            if args is not None:
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
    try:  # the payload's final message first (Codex sends it, and Claude Code does too), else the transcript's
        from . import canary
        text = data.get("last_assistant_message") or (canary.last_assistant_text(path) if path else "")
        declared = canary.has_marker(text or "")
    except Exception:
        declared = False
    last = sess.get("scope_last")
    dev = is_dev_turn(turn_actions(path), bool(sess.get("top")), declared, None if last is None else last == "dev")
    sess["scope_last"] = "dev" if dev else "other"
    return dev
