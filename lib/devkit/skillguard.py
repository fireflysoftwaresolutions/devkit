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
    return [r for r in by_id.values() if not r.get("group") or r["group"] in groups]


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
    gates read it: quoted text and heredoc bodies are dropped, each simple command stands alone, and a program is
    its base name (`bin/dev test` runs `dev test`). The words may follow a wrapper (`uv run pytest`)."""
    from .gates import _invocations
    runs = [[os.path.basename(w) for w in words] for words in _invocations([str(c) for c in commands or []])]
    out = []
    for r in rules():
        ran = [c for c in r.get("commands") or [] if any(_has(words, c.split()) for words in runs)]
        if ran:
            out.append((r, ran))
    return out


def _has(words, want) -> bool:
    return bool(want) and any(words[i:i + len(want)] == want for i in range(len(words) - len(want) + 1))


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


def _shell_command(inp) -> str:
    cmd = (inp or {}).get("command") or (inp or {}).get("cmd") or ""
    return " ".join(map(str, cmd)) if isinstance(cmd, list) else str(cmd)


def read(agent: str, transcript: str, offset: int = 0):
    """-> (loaded skill names, changed file paths, shell commands, new offset) from the transcript since
    `offset`."""
    from .hooks import SHELL_TOOLS
    loaded, files, commands = set(), [], []
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
                if name in SHELL_TOOLS:
                    commands.append(_shell_command(inp))
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
            try:
                rec = json.loads(ln)
            except ValueError:
                continue
            pl = rec.get("payload") if isinstance(rec, dict) else None
            if isinstance(pl, dict) and pl.get("type") == "function_call" and pl.get("name") in SHELL_TOOLS:
                args = pl.get("arguments")
                try:
                    args = json.loads(args) if isinstance(args, str) else args
                except ValueError:
                    args = {}
                commands.append(_shell_command(args if isinstance(args, dict) else {}))
    return loaded, files, commands, offset + len(data.encode("utf-8"))


# --------------------------------------------------------------------------- hook entry points

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
    try:  # the brief's kind of work: declared, or its member's (the crew's RACI), never its words
        from . import raci
        act = brief_activity(brief, tool_input.get("description") or "")
    except Exception:
        act = None
    if act:
        cands = [(short(sk), f"what devkit knows about {raci._lower(act['name'])}")
                 for sk in (act.get("knowledge") or {}).get("skills") or []]
        cands += [(s, r["why"]) for r in rules_for_activity(act["id"]) for s in required(r, "claude")]
        for s, why in cands:
            if s in have and not re.search(rf"\b{re.escape(s)}\b", brief, re.I) and s not in [x for x, _ in need]:
                need.append((s, why))
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
            picked = lessons.for_task(project, activity=(act or {}).get("id"), files=owned_files(brief))
        except Exception:
            picked = []
        if picked:
            extra += ("\n\n## devkit · known pitfalls\nLessons from earlier mistakes in this work; follow their "
                      "prevention. If one happens again, run `dev lesson recur <id> --evidence \"…\"`; a new mistake "
                      "that could repeat gets `dev lesson add \"…\" --cause \"…\" --prevent \"…\"`.\n"
                      + "\n".join(lessons.pitfall_lines(picked)))
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
    """Rules whose required skills are not installed for this agent: [(rule id, [missing])]."""
    have = installed(agent)
    out = []
    for r in rules():
        miss = [s for s in required(r, agent) if s not in have]
        if miss:
            out.append((r["id"], miss))
    return out
