# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Handoff notes: structure, parsing and safe programmatic edits.

A note (`~/Development/.handoff/projects/<project id>.md`) is plain Markdown that
humans, Claude and Codex all read and write:

    ---                                   frontmatter (dev note set k=v)
    status: active                        active | paused | blocked | done | archived
    priority: normal                      high | normal | low
    milestone: v2.4 release
    due: 2026-12-01
    tags: [payments, api]
    ---
    # owner/repo
    <!-- dev:auto:start --> … <!-- dev:auto:end -->   generated facts (dev sync)
    ## Purpose
    ## Current state
    ## Tasks                               dev task add|done|start|ls
    ### Now / ### Next / ### Later / ### Done
    ## Decisions and gotchas
    ## Links
    ## Session log                         newest first; dev adds one line per session

Every function here takes and returns note text (no I/O) so it is easy to test.
"""
from __future__ import annotations

import re
import time

AUTO_START = "<!-- dev:auto:start -->"
AUTO_END = "<!-- dev:auto:end -->"
LOG_HEADER = "## Session log"
TASKS_HEADER = "## Tasks"
BUCKETS = ("Now", "Next", "Later", "Done")
STATUSES = ("active", "paused", "blocked", "done", "archived")
PRIORITIES = ("high", "normal", "low")
META_KEYS = ("status", "priority", "milestone", "due", "tags", "progress")
INITIATIVE_KEYS = ("kind", "title", "status", "priority", "owner", "milestone", "due", "progress", "projects", "tags",
                   "source")
TASK_RE = re.compile(r"^(\s*)- \[([ xX~])\] (.+)$")

TEMPLATE = """---
status: active
priority: normal
milestone:
due:
tags: []
---
# {title}

{auto_start}
{auto_end}

## Purpose
_What this project is, who uses it, where it runs (1-3 lines)._

## Current state
_What works, what is half done, what is broken. Keep it true; rewrite rather than append._

## Tasks
### Now
### Next
### Later
### Done

## Decisions and gotchas
- _Non-obvious facts a newcomer (human or agent) would otherwise rediscover the hard way._

## Links
- _Docs, environments, dashboards, related repositories._

{log_header}
<!-- Newest first, one line per session. devkit adds branch, commits and the agent's DevKit status line automatically. -->
"""


INITIATIVE_TEMPLATE = """---
kind: initiative
title: {title}
status: active
priority: normal
owner: {owner}
milestone:
due:
progress:
projects: [{projects}]
tags: []
source: []
---
# {title}

{auto_start}
{auto_end}

## Goal
_The outcome, for whom, and how we will know it is done (1-3 sentences)._

## Scope
- _In scope / out of scope._

## Current state
_What is done and verified, what is half done, what is broken — across all member projects._

## Workstreams
{workstreams}
## Tasks
### Now
### Next
### Later
### Done

## Decisions and gotchas
- _Cross-project decisions and contracts (APIs, events, versions) every member must respect._

## Risks and open questions
- _What could stop the initiative; what still needs a decision._

## Links
- _Specs, environments, boards, related initiatives._

{log_header}
<!-- Newest first, one line per session or milestone. -->
"""


def new_initiative(title: str, owner: str = "", projects=()) -> str:
    ws = "".join(f"### {p}\n_Role in the initiative, state, next step._\n\n" for p in projects) or \
        "### <owner/repo>\n_Role in the initiative, state, next step._\n\n"
    return INITIATIVE_TEMPLATE.format(title=title, owner=owner, projects=", ".join(projects), workstreams=ws,
                                      auto_start=AUTO_START, auto_end=AUTO_END, log_header=LOG_HEADER)


def new_note(title: str) -> str:
    return TEMPLATE.format(title=title, auto_start=AUTO_START, auto_end=AUTO_END, log_header=LOG_HEADER)


def is_placeholder(line: str) -> bool:
    s = line.strip().lstrip("-").strip()
    return not s or (s.startswith("_") and s.endswith("_"))


# --------------------------------------------------------------------------- frontmatter

def split_frontmatter(text: str):
    """-> (meta dict, body text). Tolerant: no frontmatter gives ({}, text)."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    meta = {}
    for ln in text[4:end].splitlines():
        if ":" not in ln or ln.lstrip().startswith("#"):
            continue
        k, _, v = ln.partition(":")
        v = v.split(" #", 1)[0].strip()
        if v.startswith("[") and v.endswith("]"):
            meta[k.strip()] = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
        else:
            meta[k.strip()] = v.strip("'\"")
    return meta, text[end + 5:]


def render_frontmatter(meta: dict) -> str:
    lines = ["---"]
    order = INITIATIVE_KEYS if meta.get("kind") == "initiative" else META_KEYS
    for k in list(order) + [k for k in meta if k not in order]:
        v = meta.get(k, [] if k == "tags" else "")
        if isinstance(v, list):
            v = "[" + ", ".join(v) + "]"
        lines.append(f"{k}: {v}".rstrip())
    return "\n".join(lines) + "\n---\n"


def set_meta(text: str, **kv) -> str:
    meta, body = split_frontmatter(text)
    for k, v in kv.items():
        if k == "status" and v not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        if k == "priority" and v not in PRIORITIES:
            raise ValueError(f"priority must be one of {', '.join(PRIORITIES)}")
        if k == "due" and v and not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            raise ValueError("due must be YYYY-MM-DD")
        if k in ("tags", "projects", "source") and isinstance(v, str):
            v = [t.strip() for t in v.split(",") if t.strip()]
        if k == "progress" and v not in ("", None) and not str(v).rstrip("%").isdigit():
            raise ValueError("progress must be a number from 0 to 100 (or empty to compute it from tasks)")
        meta[k] = v
    for k, default in (("status", "active"), ("priority", "normal")):
        meta.setdefault(k, default)
    return render_frontmatter(meta) + body.lstrip("\n")


# --------------------------------------------------------------------------- sections

def sections(text: str) -> dict:
    """Level-2 sections of the body (auto block and frontmatter excluded) -> {title: content}."""
    _, body = split_frontmatter(text)
    if AUTO_START in body and AUTO_END in body:
        pre, rest = body.split(AUTO_START, 1)
        body = pre + rest.split(AUTO_END, 1)[1]
    out, cur, buf = {}, None, []
    for ln in body.splitlines():
        if ln.startswith("## "):
            if cur:
                out[cur] = "\n".join(buf).strip("\n")
            cur, buf = ln[3:].strip(), []
        elif cur:
            buf.append(ln)
    if cur:
        out[cur] = "\n".join(buf).strip("\n")
    return out


def meaningful(section_text: str) -> str:
    lines = [ln for ln in (section_text or "").splitlines()
             if not is_placeholder(ln) and not ln.strip().startswith("<!--")]
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------- tasks

def _task_meta(text: str) -> dict:
    m = {"refs": re.findall(r"(?<![\w/])#(\d+)\b", text)}
    d = re.search(r"\bdue:(\d{4}-\d{2}-\d{2})\b", text)
    if d:
        m["due"] = d.group(1)
    if re.search(r"(^|\s)!high\b", text):
        m["priority"] = "high"
    who = re.search(r"(^|\s)@([\w-]+)", text)
    if who:
        m["owner"] = who.group(2)
    return m


def tasks(text: str) -> list:
    """All tasks in the Tasks section: [{bucket, text, done, state, index}] (index counts open tasks, from 1)."""
    out, bucket, in_tasks, n = [], "Next", False, 0
    for ln in text.splitlines():
        if ln.startswith("## "):
            in_tasks = ln.strip() == TASKS_HEADER
            continue
        if not in_tasks:
            continue
        if ln.startswith("### "):
            b = ln[4:].strip().capitalize()
            bucket = b if b in BUCKETS else bucket
            continue
        m = TASK_RE.match(ln)
        if not m and out and ln.startswith("  ") and ln.strip() and not ln.lstrip().startswith(("- ", "* ", "#")):
            out[-1]["text"] += " " + ln.strip()  # a task wrapped over several lines
            continue
        if m:
            mark = m.group(2).lower()
            done = mark == "x"
            t = {"bucket": "Done" if done else bucket, "text": m.group(3).strip(), "done": done,
                 "state": "done" if done else ("doing" if mark == "~" or bucket == "Now" else "todo")}
            t.update(_task_meta(t["text"]))
            if not done:
                n += 1
                t["index"] = n
            out.append(t)
    return out


def ensure_structure(text: str, title: str) -> str:
    """Upgrade an older note in place: frontmatter, Tasks section (from legacy 'Next steps'), Links."""
    if not text.strip():
        return new_note(title)
    meta, body = split_frontmatter(text)
    changed = not meta
    if TASKS_HEADER not in body:
        legacy = re.search(r"^## Next steps\n(.*?)(?=^## |\Z)", body, flags=re.M | re.S)
        items = []
        if legacy:
            for ln in legacy.group(1).splitlines():
                s = re.sub(r"^\s*(\d+\.|-)\s*", "", ln).strip()
                if s and not is_placeholder(s):
                    items.append(f"- [ ] {s}")
            block = "## Tasks\n### Now\n### Next\n" + ("\n".join(items) + "\n" if items else "") + "### Later\n### Done\n\n"
            body = body[:legacy.start()] + block + body[legacy.end():]
        else:
            anchor = body.find("## Decisions and gotchas")
            block = "## Tasks\n### Now\n### Next\n### Later\n### Done\n\n"
            body = body[:anchor] + block + body[anchor:] if anchor != -1 else body.rstrip("\n") + "\n\n" + block
        changed = True
    if "\n## Links" not in body:
        anchor = body.find(LOG_HEADER)
        block = "## Links\n- _Docs, environments, dashboards, related repositories._\n\n"
        body = body[:anchor] + block + body[anchor:] if anchor != -1 else body + "\n" + block
        changed = True
    if not changed:
        return text
    meta.setdefault("status", "active")
    meta.setdefault("priority", "normal")
    return render_frontmatter(meta) + body.lstrip("\n")


def _tasks_bounds(lines):
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == TASKS_HEADER)
    except StopIteration:
        return None, None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return start, end


def _bucket_line(lines, start, end, bucket):
    for i in range(start + 1, end):
        if lines[i].strip().lower() == f"### {bucket.lower()}":
            return i
    lines.insert(end, f"### {bucket}")
    return end


def add_task(text: str, task: str, bucket: str = "Next") -> str:
    bucket = bucket.capitalize()
    if bucket not in BUCKETS[:3]:
        raise ValueError("bucket must be Now, Next or Later")
    lines = text.splitlines()
    start, end = _tasks_bounds(lines)
    if start is None:
        raise ValueError("note has no Tasks section")
    i = _bucket_line(lines, start, end, bucket)
    j = i + 1
    while j < len(lines) and TASK_RE.match(lines[j]):
        j += 1
    lines.insert(j, f"- [ ] {task.strip()}")
    return "\n".join(lines) + "\n"


def _find_open(lines, start, end, selector):
    n = 0
    candidates = []
    for i in range(start, end):
        m = TASK_RE.match(lines[i])
        if m and m.group(2).lower() != "x":
            n += 1
            if (str(selector).isdigit() and int(selector) == n) or \
                    (not str(selector).isdigit() and str(selector).lower() in m.group(3).lower()):
                candidates.append(i)
    if not candidates:
        raise ValueError(f"no open task matches “{selector}”")
    if len(candidates) > 1:
        raise ValueError(f"“{selector}” matches {len(candidates)} tasks; use the number from `dev task`")
    return candidates[0]


def move_task(text: str, selector, bucket: str, done: bool = False) -> str:
    lines = text.splitlines()
    start, end = _tasks_bounds(lines)
    if start is None:
        raise ValueError("note has no Tasks section")
    i = _find_open(lines, start, end, selector)
    item = TASK_RE.match(lines[i]).group(3).strip()
    del lines[i]
    start, end = _tasks_bounds(lines)
    if done:
        item = f"{item} ✓ {time.strftime('%Y-%m-%d')}"
        bi = _bucket_line(lines, start, end, "Done")
        lines.insert(bi + 1, f"- [x] {item}")
    else:
        bi = _bucket_line(lines, start, end, bucket)
        j = bi + 1
        while j < len(lines) and TASK_RE.match(lines[j]):
            j += 1
        lines.insert(j, f"- [ ] {item}")
    return "\n".join(lines) + "\n"


def prune_done(text: str, keep: int = 15) -> str:
    """Keep the Done list short: the newest `keep` items stay in the note (git keeps the rest)."""
    lines = text.splitlines()
    start, end = _tasks_bounds(lines)
    if start is None:
        return text
    done_idx = []
    for i in range(start, end):
        m = TASK_RE.match(lines[i])
        if m and m.group(2).lower() == "x":
            done_idx.append(i)
    if len(done_idx) <= keep:
        return text
    for i in reversed(done_idx[keep:]):
        del lines[i]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- summary for registry / dashboard

def workstreams(text: str) -> dict:
    """### subsections of ## Workstreams -> {project id: text}."""
    body = sections(text).get("Workstreams", "")
    out, cur, buf = {}, None, []
    for ln in body.splitlines():
        if ln.startswith("### "):
            if cur:
                out[cur] = meaningful("\n".join(buf))
            cur, buf = ln[4:].strip(), []
        elif cur:
            buf.append(ln)
    if cur:
        out[cur] = meaningful("\n".join(buf))
    return {k: v for k, v in out.items() if not k.startswith("<")}


def progress_of(meta: dict, ts: list):
    """-> (percent or None, source). Manual `progress:` in frontmatter wins; else done / (done + open) tasks."""
    raw = str(meta.get("progress") or "").strip().rstrip("%")
    if raw.isdigit():
        return max(0, min(100, int(raw))), "manual"
    done = len([t for t in ts if t["done"]])
    total = len(ts)
    return (round(100 * done / total), "tasks") if total else (None, "none")


def summarize(text: str) -> dict:
    meta, _ = split_frontmatter(text)
    sec = sections(text)
    ts = tasks(text)
    open_ = [t for t in ts if not t["done"]]
    pct, psrc = progress_of(meta, ts)
    sess = []
    for ln in (sec.get("Session log") or "").splitlines():
        if ln.startswith("- "):
            sess.append(re.sub(r"\s*<!--.*?-->\s*$", "", ln[2:]).strip())
    projects = meta.get("projects") or []
    if isinstance(projects, str):
        projects = [p.strip() for p in projects.split(",") if p.strip()]
    source = meta.get("source") or []
    if isinstance(source, str):
        source = [x.strip() for x in source.split(",") if x.strip()]
    return {
        "kind": meta.get("kind") or "project",
        "title": meta.get("title") or "",
        "owner": meta.get("owner") or "",
        "projects": [p.lower() for p in projects],
        "source": source,
        "progress": pct, "progress_source": psrc,
        "goal": meaningful(sec.get("Goal", "")),
        "scope": meaningful(sec.get("Scope", "")),
        "workstreams": workstreams(text),
        "risks": meaningful(sec.get("Risks and open questions", "")),
        "status": meta.get("status") or "active",
        "priority": meta.get("priority") or "normal",
        "milestone": meta.get("milestone") or "",
        "due": meta.get("due") or "",
        "tags": meta.get("tags") or [],
        "purpose": meaningful(sec.get("Purpose", "")),
        "state": meaningful(sec.get("Current state", "")),
        "decisions": meaningful(sec.get("Decisions and gotchas", "")),
        "links": meaningful(sec.get("Links", "")),
        "tasks": {
            "now": [t["text"] for t in open_ if t["bucket"] == "Now"],
            "next": [t["text"] for t in open_ if t["bucket"] == "Next"],
            "later": [t["text"] for t in open_ if t["bucket"] == "Later"],
            "done_recent": [t["text"] for t in ts if t["done"]][:8],
            "open": len(open_), "done": len([t for t in ts if t["done"]]),
            "overdue": [t["text"] for t in open_ if t.get("due") and t["due"] < time.strftime("%Y-%m-%d")],
        },
        "sessions": sess[:12],
    }
