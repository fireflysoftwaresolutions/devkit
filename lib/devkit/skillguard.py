# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Skill enforcement: the right skills are loaded for the work being done (config/skill-rules.json).

1. Catalog  the session's catalog (`hooks.catalog_context`) is an index of the kinds of work the model declares by
            id, whatever language the developer writes in, and points to config/skill-rules.json for the skills each
            one requires. Rules never match the developer's words, which can be in any language.
2. Brief    PreToolUse on Claude's Agent/Task tool appends the required skills to a subagent's brief (updatedInput):
            those of the brief's declared kind of work, else of its member's Responsible one.
3. Finish   Stop refuses to end a turn that changed files matching a rule, or ran a shell command of a rule's
            `commands`, while the rule's skills were never loaded in the session (once per skill per session), with
            the reason and the files or commands. Files on disk outside every repository are scratch work.
Rules also answer for a kind of work (`rules_for_activity`) and for a command the agent runs (`rules_for_command`).

Loaded skills are read from the session transcript: Claude's Skill tool calls and reads of SKILL.md files; Codex
shell commands that read a skill's SKILL.md. Everything fails open.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
from pathlib import Path

from . import core
from .core import CFG

RULES_FILE = core.DEVKIT_HOME / "config" / "skill-rules.json"
SKILL_PATH_RE = re.compile(r"skills/([A-Za-z0-9_.-]+)/SKILL\.md")


def enabled() -> bool:
    return (CFG.get("skills") or {}).get("enforce", True)


def rules() -> list:
    """devkit's rules with the developer's (`skills.rules`) on top; a rule of an optional skill group (`group`)
    only when that group is installed (`skills.groups`)."""
    base = (core.read_json(RULES_FILE, {}) or {}).get("rules", [])
    extra = (CFG.get("skills") or {}).get("rules") or []
    by_id = {r["id"]: r for r in base}
    for r in extra:  # user rules extend or replace devkit's by id
        if r.get("id"):
            by_id[r["id"]] = {**by_id.get(r["id"], {}), **r}
    groups = set((CFG.get("skills") or {}).get("groups") or [])
    return [r for r in by_id.values() if not r.get("group") or r["group"] in groups or "all" in groups]


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


def skill_path(name: str, agent: str = "claude"):
    """The SKILL.md of a skill on this Mac (devkit's own, personal or linked skills, then plugin caches, newest
    version first), or None. `superpowers:writing-plans` and `writing-plans` both work."""
    short_ = short(name)
    own = core.DEVKIT_HOME / "plugin" / "skills" / short_ / "SKILL.md"
    if own.exists() and (":" not in name or name.split(":")[0] == "devkit"):
        return own
    roots = [core.CLAUDE_HOME / "skills"] if agent == "claude" else [core.HOME / ".agents" / "skills", core.CODEX_HOME / "skills"]
    for r in roots:
        f = r / short_ / "SKILL.md"
        if f.exists():
            return f
    cache = core.CLAUDE_HOME / "plugins" / "cache" if agent == "claude" else core.CODEX_HOME / "plugins" / "cache"
    plugin = name.split(":")[0] if ":" in name else "*"
    hits = sorted(cache.glob(f"*/{plugin}/*/skills/{short_}/SKILL.md"), key=lambda p: p.stat().st_mtime, reverse=True) \
        if cache.exists() else []
    return hits[0] if hits else (own if own.exists() else None)


def _match_path(path: str, pattern: str) -> bool:
    p = path.replace("\\", "/")
    if "/" in pattern:
        return fnmatch.fnmatch(p, pattern) or fnmatch.fnmatch(p, "*/" + pattern)
    return fnmatch.fnmatch(Path(p).name, pattern)


def repo_top(path: str):
    """The repository a path belongs to (the nearest folder above it that holds `.git`), or None."""
    d = Path(os.path.abspath(os.path.expanduser(str(path or "")))).parent
    for folder in [d, *d.parents]:
        if (folder / ".git").exists():
            return folder
    return None


def delivered(paths) -> list:
    """The paths a skill rule may judge: a file on disk outside every repository is scratch work (a probe in a
    scratch folder), not delivered work. A path that is not on disk (deleted, or named relative to another folder)
    is judged by its path."""
    return [p for p in paths if not (os.path.exists(str(p)) and repo_top(p) is None)]


def rules_for_files(paths) -> list:
    """[(rule, [the paths it matches])]: a path matches a rule's `files` globs by name; a path that matches its
    `content_files` and none of its `content_exclude` globs matches when its text matches `content`. A rule with a
    `project` condition keeps only the paths of repositories whose project files match it (`project_uses`)."""
    hits, seen = [], {}
    for r in rules():
        files = [p for p in paths if any(_match_path(p, g) for g in r.get("files") or [])]
        if r.get("content") and r.get("content_files"):
            rx = re.compile(r["content"], re.I)
            skip = r.get("content_exclude") or []
            for p in paths:
                if p in files or not any(_match_path(p, g) for g in r["content_files"]) or \
                        any(_match_path(p, g) for g in skip):
                    continue
                try:
                    if rx.search(Path(p).read_text(errors="ignore")[:400_000]):
                        files.append(p)
                except OSError:
                    pass
        elif r.get("content") and files:  # files match only when their content matches too (e.g. Spring code)
            rx = re.compile(r["content"], re.I)
            files = [p for p in files if _read_matches(p, rx)]
        if files and r.get("project"):
            files = [p for p in files if project_uses(p, r["project"], seen)]
        if files:
            hits.append((r, files))
    return hits


def project_uses(path: str, spec: dict, seen=None) -> bool:
    """A rule's `project` condition: a project file of the path's repository (`files`, globs at the repository's
    top) has text that matches `content`, such as a project that uses Postgres. False outside a repository.
    `seen` caches the answer per repository and condition."""
    top = repo_top(path)
    if top is None:
        return False
    key = (str(top), spec.get("content") or "", tuple(spec.get("files") or ()))
    if seen is not None and key in seen:
        return seen[key]
    rx = re.compile(spec.get("content") or r"(?!)", re.I)
    found = False
    for g in spec.get("files") or []:
        for f in sorted(top.glob(g)):
            try:
                if f.is_file() and rx.search(f.read_text(errors="ignore")[:200_000]):
                    found = True
                    break
            except OSError:
                continue
        if found:
            break
    if seen is not None:
        seen[key] = found
    return found


def _read_matches(p, rx) -> bool:
    try:
        return bool(rx.search(Path(p).read_text(errors="ignore")[:400_000]))
    except OSError:
        return True  # deleted or unreadable: judge by the path


def rules_for_activity(activity_id: str) -> list:
    """The rules for a declared kind of work (an id from the crew's RACI)."""
    return [r for r in rules() if activity_id and activity_id in (r.get("activities") or [])]


def rules_for_command(command: str) -> list:
    """The rules whose command words a shell command runs ("gh workflow run ci.yml" → workflows); text inside quotes
    is not a command."""
    return [r for r, _ in rules_for_commands([command])]


def rules_for_commands(commands) -> list:
    """[(rule, [its command words the shell ran])] for shell commands. What a command runs is read the way the crew
    gates read it: quoted text and heredoc bodies are dropped, each simple command stands alone, a program is its
    base name (`bin/dev test` runs `dev test`), and an option written with its value after `=` is the option alone
    (`goose -dir=db/migrations up` runs `goose -dir`). The words may follow a wrapper (`uv run pytest`)."""
    from .gates import _invocations
    runs = [[_command_word(w) for w in words] for words in _invocations([str(c) for c in commands or []])]
    out = []
    for r in rules():
        ran = [c for c in r.get("commands") or [] if any(_has(words, c.split()) for words in runs)]
        if ran:
            out.append((r, ran))
    return out


def _command_word(word: str) -> str:
    """A word of a shell command as rules name it: an option with its value after `=` by its flag part, read before
    the base name so the value's path is not taken for the word (`-dir=db/migrations` is `-dir`, not `migrations`);
    any other word by its base name without the version `go run` and `npx` put after `@`
    (`…/cmd/goose@latest` is `goose`; a name that starts with `@`, an npm scope, stays whole)."""
    if word.startswith("-"):
        return os.path.basename(word.split("=", 1)[0])
    return re.sub(r"(?<=.)@[^@/]*\Z", "", os.path.basename(word))


def _same_word(word: str, want: str) -> bool:
    """A command word is the rule's word; an option a rule writes with one dash (`-dir`, as Go's flag package
    names it) is the same option written with two (`--dir`), which that package reads alike."""
    return word == want or (want[:1] == "-" and want[:2] != "--" and word == "-" + want)


def _has(words, want) -> bool:
    return bool(want) and any(all(_same_word(w, x) for w, x in zip(words[i:i + len(want)], want))
                              for i in range(len(words) - len(want) + 1))


DECLARED = re.compile(r"^Kind of work: .*\(`([a-z0-9-]+)`\)\s*$", re.M)  # the line `dev crew brief` writes
OWNED = re.compile(r"^You own only these files: (.+?)\.(?: |$)", re.M)  # up to the sentence end


def brief_activity(brief: str, description: str = ""):
    """The kind of work a subagent's brief is for: the one it declares, else the Responsible one of the crew member
    its description names ("Tank: the API"). None for a helper that is no crew member."""
    from . import crew, raci
    m = DECLARED.search(brief or "")
    if m and raci.activity(m.group(1)):
        return raci.activity(m.group(1))
    who = crew._for_label((description or "").split(":")[0].strip()) if description else None
    if not who or who.get("main"):
        return None
    return raci.anchor("", member=who["id"], files=owned_files(brief))[0]


def owned_files(brief: str) -> list:
    """The file scope a brief gives its member (the line `dev crew brief` writes)."""
    m = OWNED.search(brief or "")
    return re.findall(r"`([^`]+)`", m.group(1)) if m else []


def required(rule: dict, agent: str) -> list:
    return [short(s) for s in (rule.get("skills") or {}).get(agent, [])]


def how_to_load(agent: str) -> str:
    return "with the Skill tool" if agent == "claude" else "by reading its SKILL.md (listed among your skills)"


# --------------------------------------------------------------------------- transcript scan

def scan(agent: str, transcript: str, offset: int = 0):
    """-> (loaded skill names, changed file paths, new offset) from the transcript since `offset`."""
    loaded, files, _, offset = read(agent, transcript, offset)
    return loaded, files, offset


def executions(agent: str, transcript: str, cwd: str = "") -> list:
    """Completed shell receipts, separate from requested commands used for skill and edit attribution.

    Tool results must correlate with their call; code-mode scripts need native command items, not merely parsed
    JavaScript. A completed failing process proves performance, but its `ok` is false. Unknown, denied and still
    running calls produce no receipt. No state is written and no missing caller identity is manufactured.
    """
    from . import gates
    from .hooks import SHELL_TOOLS
    from .scope import item_command
    records = gates._records(transcript)
    calls, sessions, tasks, reads, receipts = {}, {}, {}, {}, []
    overridden = set()
    code_default = True
    if agent == "codex":
        from .codexlaunch import session_meta
        meta = session_meta(transcript)
        thread = meta.get("id")
        if not isinstance(thread, str) or not thread:
            return []
        boundary = meta.get("subagent_history_start_ordinal")
        if meta.get("forked_from_id") and (type(boundary) is not int or boundary < 0):
            return []
        truncated = not records or records[0].get("type") != "session_meta"
        if not truncated:
            records = records[fork_start([json.dumps(r) for r in records]):]
        cwd = str(meta.get("cwd") or cwd)
    else:
        thread, boundary = "", None

    def finish(call, code, output, ok=True):
        # ts: when the call started (its own record's time), None when no record gives it
        receipts.append({"command": call["command"], "cwd": call["cwd"], "ok": code == 0 and ok,
                         "exit_code": code, "output": output, "context_ok": call.get("context_ok", True),
                         "ts": call.get("ts")})

    def invocation(inp, here, rec):
        command = _shell_command(inp)
        default = not any(key in inp for key in ("shell", "env", "environment", "PATH"))
        if not default:
            overridden.add(command)
        return {"command": command, "cwd": str(inp.get("workdir") or inp.get("cwd") or here),
                "context_ok": default, "ts": _epoch(rec.get("timestamp"))}

    def default_code_context(script):
        # Keep execution context that exec_calls deliberately drops for ordinary attribution. An unreadable
        # argument or an override cannot borrow host-default resolution from a later native command item.
        from . import scope
        if not isinstance(script, str):
            return False
        src, at, seen = script or "", 0, False
        while at < len(src):
            match = scope._JS_SCAN_RE.search(src, at)
            if not match:
                break
            token = match.group(0)
            if token in ("//", "/*"):
                at = scope._js_skip(src, match.start())
            elif token in ("'", '"', "`") or match.group(1) == "apply_patch":
                start = match.start() if token in ("'", '"', "`") else match.end()
                _, end = scope._js_string(src, start)
                at = end if end > start else len(src)
            else:
                fields, end = scope._js_object(src, match.end())
                if fields is None or any(key in fields for key in ("shell", "env", "environment", "PATH")):
                    return False
                seen, at = True, max(end, match.end())
        return seen

    for rec in records:
        if agent == "codex":
            if boundary is not None and (type(rec.get("ordinal")) is not int or rec["ordinal"] < boundary):
                continue
            p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
            if p.get("thread_id") not in (None, thread):
                continue
            if rec.get("type") == "turn_context" and isinstance(p.get("cwd"), str):
                cwd = p["cwd"]
            item = p.get("item") if rec.get("type") == "event_msg" and p.get("type") == "item_completed" else None
            if isinstance(item, dict) and item.get("type") == "CommandExecution":
                code = item.get("exit_code")
                ran = item_command(rec, [])
                if item.get("id") and item.get("status") in ("completed", "failed") and type(code) is int \
                        and ran and ran[1]:
                    argv = item.get("command")
                    shells = _default_shells()
                    default = isinstance(argv, list) and len(argv) == 3 and isinstance(argv[0], str) \
                        and argv[1] in ("-c", "-lc") \
                        and os.path.realpath(argv[0]) in shells and ran[0] not in overridden \
                        and code_default and not any(key in item for key in ("shell", "env", "environment", "PATH"))
                    finish({"command": ran[0], "cwd": ran[1], "context_ok": default},
                           code, str(item.get("aggregated_output") or ""),
                           item["status"] == "completed")
                continue
            if rec.get("type") != "response_item":
                continue
            name = str(p.get("name") or "").split(".")[-1]
            use = p.get("call_id")
            if name == "exec" and p.get("type") == "custom_tool_call":
                code_default = default_code_context(p.get("input"))
            if p.get("type") == "function_call" and use:
                try:
                    inp = json.loads(p.get("arguments") or "{}")
                except (TypeError, ValueError):
                    continue
                if not isinstance(inp, dict):
                    continue
                if name in SHELL_TOOLS:
                    calls[use] = invocation(inp, cwd, rec)
                elif name == "write_stdin" and str(inp.get("session_id")) in sessions:
                    calls[use] = sessions[str(inp["session_id"])]
            elif p.get("type") == "function_call_output" and use in calls:
                output = p.get("output")
                try:
                    result = json.loads(output) if isinstance(output, str) else output
                except ValueError:
                    result = None
                if isinstance(result, dict):
                    code = result.get("exit_code")
                    if type(code) is int:
                        finish(calls[use], code, str(result.get("output") or ""), result.get("status") != "failed")
                    elif result.get("session_id"):
                        sessions[str(result["session_id"])] = calls[use]
                elif isinstance(output, str):
                    # Older Codex writes the execution envelope as text, distinct from its Output section.
                    envelope, _, body = output.partition("Output:")
                    done = re.search(r"(?m)^Process exited with code (-?\d+)\s*$", envelope)
                    pending = re.search(r"(?m)^Process running with session ID (\w+)\s*$", envelope)
                    if done and not gates.DENIAL_RE.match(output):
                        finish(calls[use], int(done[1]), body)
                    elif pending:
                        sessions[pending[1]] = calls[use]
            continue

        for note in gates._notifications(rec):
            task = gates._tag(note, "task-id")
            status = gates._tag(note, "status")
            if task in tasks and status in ("completed", "failed"):
                finish(tasks.pop(task), 0 if status == "completed" else 1, note)
        content = (rec.get("message") or {}).get("content")
        for c in content if isinstance(content, list) else []:
            if not isinstance(c, dict):
                continue
            if rec.get("type") == "assistant" and c.get("type") == "tool_use":
                inp = c.get("input") or {}
                if c.get("name") in SHELL_TOOLS and c.get("id"):
                    calls[c["id"]] = invocation(inp, cwd, rec)
                elif c.get("name") in gates.READ_TOOLS and c.get("id"):
                    reads[c["id"]] = gates._task_of(inp)
            elif rec.get("type") == "user" and c.get("type") == "tool_result":
                call = calls.get(c.get("tool_use_id"))
                output = gates._text(c.get("content"))
                if gates.DENIAL_RE.match(output):
                    continue
                result = rec.get("toolUseResult")
                result = result if isinstance(result, dict) else {}
                task = reads.get(c.get("tool_use_id"))
                if task in tasks:
                    code = gates._tag(output, "exit_code")
                    status = gates._tag(output, "status") or result.get("status")
                    if re.fullmatch(r"-?\d+", code):
                        finish(tasks.pop(task), int(code), output, status != "failed")
                    elif status in ("completed", "failed"):
                        finish(tasks.pop(task), 0 if status == "completed" else 1, output)
                    continue
                if not call:
                    continue
                pending = gates.BG_LAUNCH_RE.match(output)
                task = pending[1] if pending else result.get("backgroundTaskId")
                if task:
                    tasks[str(task)] = call
                    continue
                code = result.get("exitCode", result.get("exit_code"))
                if type(code) is not int:
                    code = 1 if c.get("is_error") else 0
                finish(call, code, output, not c.get("is_error"))
    return receipts


_FRACTION = re.compile(r"(?<=T\d\d:\d\d:\d\d)\.(\d*)")


def _epoch(stamp):
    """A transcript record's `timestamp` (ISO 8601 in UTC, as Claude Code and Codex write it) in epoch seconds; None
    when the record has none or it does not parse."""
    from datetime import datetime, timezone
    if not isinstance(stamp, str) or not stamp:
        return None
    # Python 3.9 and 3.10 read only a 3- or 6-digit fraction of a second (and take an empty one): write it as 6
    # digits, so every Python reads the same time, and refuse an empty fraction everywhere
    frac = _FRACTION.search(stamp)
    if frac:
        if not frac.group(1):
            return None
        stamp = stamp[:frac.start()] + "." + (frac.group(1) + "000000")[:6] + stamp[frac.end():]
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()


def _default_shells() -> set:
    """The real paths of the shells Codex runs a command with by default: the system's sh, bash and zsh, and the
    developer's login shell ($SHELL, such as fish or Homebrew's bash) when it resolves to a regular executable file."""
    login = os.path.realpath(os.environ.get("SHELL") or "/bin/sh")
    candidates = ("/bin/sh", "/bin/bash", "/bin/zsh", login)
    return {os.path.realpath(p) for p in candidates if os.path.isfile(p) and os.access(p, os.X_OK)}


def _shell_command(inp) -> str:
    cmd = (inp or {}).get("command") or (inp or {}).get("cmd") or ""
    return " ".join(map(str, cmd)) if isinstance(cmd, list) else str(cmd)


def fork_start(lines: list) -> int:
    """The index of a Codex rollout's first line of its own. A spawned agent's rollout opens with a copy of its
    parent's history (its session_meta names forked_from_id), whose commands, skills and edits are the parent's: the
    agent's own records start at its first task_started at or after the subagent_history_start_ordinal its
    session_meta gives (Codex 0.167), past the end of the rollout when it has not started yet. 0 for a rollout that
    is not forked or gives no start."""
    try:
        first = json.loads(lines[0]) if lines else {}
    except ValueError:
        return 0
    meta = first.get("payload") if isinstance(first, dict) and first.get("type") == "session_meta" else None
    start = meta.get("subagent_history_start_ordinal") if isinstance(meta, dict) and meta.get("forked_from_id") \
        else None
    if not isinstance(start, int) or isinstance(start, bool):
        return 0
    for i, ln in enumerate(lines):
        if '"task_started"' not in ln:
            continue
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        p = rec.get("payload") if isinstance(rec, dict) and rec.get("type") == "event_msg" else None
        if not isinstance(p, dict) or p.get("type") != "task_started":
            continue
        ordinal = rec.get("ordinal")
        if (ordinal if isinstance(ordinal, int) and not isinstance(ordinal, bool) else i) >= start:
            return i
    return len(lines)


def read(agent: str, transcript: str, offset: int = 0):
    """-> (loaded skill names, changed file paths, shell commands, new offset) from the transcript since
    `offset`."""
    from .hooks import SHELL_TOOLS
    from .scope import exec_calls, item_command
    loaded, files, commands, parsed = set(), [], [], []  # parsed: exec-script commands no record matched yet
    try:
        p = Path(transcript)
        size = p.stat().st_size
        if offset > size:
            offset = 0
        with p.open("rb") as fh:
            fh.seek(offset)
            data = fh.read().decode("utf-8", "ignore")
    except (OSError, TypeError):
        return loaded, files, commands, offset
    lines = data.splitlines()
    if agent != "claude" and offset == 0:  # a spawned Codex agent's rollout: from its own first record
        lines = lines[fork_start(lines):]
    for ln in lines:
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
                if name in SHELL_TOOLS:
                    commands.append(_shell_command(inp))
                if name == "Bash":  # cat/sed of a SKILL.md counts as loading it
                    for m in SKILL_PATH_RE.finditer(inp.get("command") or ""):
                        loaded.add(m.group(1).lower())
        else:
            if "function_call" not in ln and "custom_tool_call" not in ln and "exec_command" not in ln and \
                    "CommandExecution" not in ln:
                continue
            for m in SKILL_PATH_RE.finditer(ln.replace("\\/", "/")):
                loaded.add(m.group(1).lower())
            for m in re.finditer(r"\*\*\* (?:Add|Update) File: ([^\\\n\"]+)", ln):
                files.append(m.group(1).strip())
            try:
                rec = json.loads(ln)
            except ValueError:
                continue
            pl = rec.get("payload") if isinstance(rec, dict) else None
            ran = item_command(rec, parsed) if isinstance(rec, dict) else None
            if ran:  # a command the exec scripts read so far did not name (built at run time)
                commands.append(ran[0])
                continue
            if isinstance(pl, dict) and pl.get("type") in ("custom_tool_call", "function_call") and \
                    pl.get("name") == "exec" and isinstance(pl.get("input"), str):  # code mode: its nested calls
                nested = [_shell_command(inp) for name, inp in exec_calls(pl["input"]) if name in SHELL_TOOLS]
                parsed.extend(nested)
                commands.extend(nested)
                continue
            if isinstance(pl, dict) and pl.get("type") == "function_call" and pl.get("name") in SHELL_TOOLS:
                args = pl.get("arguments")
                try:
                    args = json.loads(args) if isinstance(args, str) else args
                except ValueError:
                    args = {}
                commands.append(_shell_command(args if isinstance(args, dict) else {}))
    return loaded, files, commands, offset + len(data.encode("utf-8"))


# --------------------------------------------------------------------------- hook entry points

def on_agent_brief(agent: str, tool_input: dict, project: str = "", activity: str = "", files=None):
    """A crew brief gets the skills its work requires and the known pitfalls (lessons) for its task: Claude's
    Agent/Task call, and a Codex spawn at its SubagentStart (codexlaunch), whose brief is encrypted, so its kind of
    work (`activity`) and its files (`files`) come from the plan step its launch bound to. -> updated tool input, or
    None."""
    if not enabled() or agent not in ("claude", "codex"):
        return None
    brief = tool_input.get("prompt") or ""
    if "devkit · required skills" in brief:
        return None
    have = installed(agent)
    need = []
    try:  # the brief's kind of work: declared, or its member's (the crew's RACI), never its words
        from . import raci
        act = (raci.activity(activity) if activity else None) or \
            brief_activity(brief, tool_input.get("description") or "")
    except Exception:
        act = None
    if act:
        cands = [(short(sk), f"what devkit knows about {raci._lower(act['name'])}")
                 for sk in (act.get("knowledge") or {}).get("skills") or []]
        cands += [(s, r["why"]) for r in rules_for_activity(act["id"]) for s in required(r, agent)]
        for s, why in cands:
            if s in have and not re.search(rf"\b{re.escape(s)}\b", brief, re.I) and s not in [x for x, _ in need]:
                need.append((s, why))
    extra = ""
    if need:
        seen = dict.fromkeys(need)
        load = "with the Skill tool and follow it (if the Skill tool is not available, read its SKILL.md under " \
               "~/.claude)" if agent == "claude" else f"{how_to_load(agent)} and follow it"
        extra = f"\n\n## devkit · required skills\nBefore you start, load each of these {load}:\n" + \
                "\n".join(f"- `{s}` — {why}" for s, why in seen) + \
                "\nSay in your report which ones you loaded and how you applied them."
    if project and "devkit · known pitfalls" not in brief and "## Known pitfalls" not in brief:
        try:
            from . import lessons, notes  # the project's initiatives' lessons are its own too
            picked = lessons.for_task(project, activity=(act or {}).get("id"),
                                      files=owned_files(brief) if files is None else list(files),
                                      initiatives=notes.initiatives_of(project))
        except Exception:
            picked = []
        if picked:
            extra += ("\n\n## devkit · known pitfalls\nLessons from earlier mistakes in this work; follow their "
                      "prevention. If one happens again, run `dev lesson recur <id> --evidence \"…\"`; a new mistake "
                      "that could repeat gets `dev lesson add \"…\" --happened \"…\" --cause \"…\" "
                      "--prevent \"…\" --file <glob> --command \"<command words>\" --error \"<output text>\"`.\n"
                      + "\n".join(lessons.pitfall_lines(picked)))
            lessons.track([x["meta"]["id"] for x in picked], "shown", "brief")  # the hook records them for the member
    if "Firefly> DevKit" not in brief:  # every crew member reports the same way (the SubagentStop hook checks it)
        name = (tool_input.get("description") or "").split(":")[0].strip()[:30] or "Agent"
        try:  # the member's role first, as the developer reads every member ("Frontend engineer (Trinity)")
            from . import crew
            name = crew.who(name)
        except Exception:
            pass
        extra += (f"\n\n## devkit · your report\nStart your final report with your status block: **[Firefly> DevKit · "
                  f"{name}]** <project> · `<branch>` · <task>, then - **Done:**, - **Check:** (✓/⚠/✗ + "
                  f"evidence) and - **Next:**. Record progress with `dev progress step \"…\"` (it goes to your own "
                  f"lane) and ask the tech lead with `dev crew ask \"…\"` when you need a decision.")
    if not extra:
        return None
    return {**tool_input, "prompt": brief + extra}


def on_stop(agent: str, data: dict, sess: dict):
    """-> reason to continue the turn (missing skills for the files it changed and the commands it ran), or None.
    Updates `sess`."""
    if not enabled():
        return None
    transcript = data.get("transcript_path")
    if not transcript:
        return None
    loaded, files, commands, offset = read(agent, transcript, int(sess.get("skills_offset") or 0))
    sess["skills_offset"] = offset
    sess["skills_loaded"] = sorted(set(sess.get("skills_loaded") or []) | loaded)
    changed = sorted(set(sess.get("files_changed") or []) | set(files))
    sess["files_changed"] = changed[-300:]
    if data.get("stop_hook_active") or not (files or commands):
        return None
    have = installed(agent)
    nudged = set(sess.get("skills_nudged") or [])
    missing = {}  # skill -> (why, what needs it)
    hits = [(r, ", ".join(Path(f).name for f in m[:3]) + (" …" if len(m) > 3 else ""))
            for r, m in rules_for_files(delivered(files))]
    hits += [(r, ", ".join(f"`{c}`" for c in ran)) for r, ran in rules_for_commands(commands)]
    for r, what in hits:
        for s in required(r, agent):
            if s in have and s not in sess["skills_loaded"] and s not in nudged and s not in missing:
                missing[s] = (r["why"], what)
    if not missing:
        return None
    sess["skills_nudged"] = sorted(nudged | set(missing))
    parts = [f"`{s}` ({why}) for {what}" for s, (why, what) in missing.items()]
    return ("devkit: your turn changed files or ran commands that need skills you have not loaded in this session: " +
            "; ".join(parts) + f". Load each {how_to_load(agent)}, review your work against it, fix what it finds, "
            "then finish.")


def coverage(agent: str) -> list:
    """Missing rule skills in the selected installation: [(rule id, [missing])].

    An omitted optional group is not an installation failure. Task-specific rules and their required skills stay
    unchanged, so selecting work that needs that capability still names the skill.
    """
    from . import ext, selfcheck
    have = installed(agent)
    groups = set((CFG.get("skills") or {}).get("groups") or [])
    applicability = ext.skill_applicability(agent)

    def selected(skill):
        if skill in applicability:
            return applicability[skill]
        group = selfcheck.skill_group(skill)
        return "all" in groups or group in groups | {"core", "unknown"} or group.startswith("plugin ")

    out = []
    for r in rules():
        miss = [s for s in required(r, agent) if s not in have and selected(s)]
        if miss:
            out.append((r["id"], miss))
    return out
