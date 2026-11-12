# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Lessons: learn from mistakes so they are not repeated, in one project or across all of them.

A lesson is a short, blameless record in the handoff repository: lessons/<scope>/<ID>.md, where scope is a project
id, initiatives/<slug> or `global`. It holds what happened, the root cause, the fix, the prevention (the rule to
follow) and how to detect it early, with evidence (commits, PRs, CI runs) and a versioned changelog.

The loop:
  capture   `dev lesson add`, prompted by signals: reverts and fix commits, repeated guard refusals, canary misses,
            reopened tasks, required-skill reminders (`dev lesson scan`), and the Stop hook after a fix commit
  anchors   each lesson names where it applies, in terms that mean the same in any language: files (globs),
            commands (command words), kinds of work (activity ids), error signatures (tool output) and stack.
            The developer's words never select a lesson (crew gap review, C13)
  recall    the session context lists the project's open lessons; a step gets the lessons of its kind of work and
            files (`for_task`), a tool call those of its command and files (`for_call`), tool output those whose
            error signature it shows (`for_output`)
  recur     `dev lesson recur` counts a repeat; a lesson that recurs is a candidate for automation
  prevent   `dev lesson prevent --by test|lint|guard|skill|ci|rule --ref …`: automation beats prose
  review    `dev lesson review` lists recurring, unprevented and stale lessons
  use       every time a lesson is shown in a session, applied (named in a reply), repeated or looked up, its usage is
            counted (per machine: lessons/usage/<user>@<host>.json, summed across the team)
  sleep     `dev sleep` (nightly, the `sleep` job) consolidates like a brain at night: strength decays unless
            a lesson is seen again; near-duplicates are merged; lessons are archived into lessons/archive/
            (restorable) only by the archive policy (`consolidate`), never to fit a count: storage is unbounded and
            recall has its own limits
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import shlex
import time
import unicodedata
from pathlib import Path

from . import core, state, work
from .core import HANDOFF, STATE

LESSONS = HANDOFF / "lessons"
KINDS = ("bug", "mistake", "process", "tooling", "security", "performance", "design")
SEVERITIES = ("low", "medium", "high")
PREVENTIONS = ("test", "lint", "type", "guard", "skill", "ci", "rule", "doc")
ANCHORS = ("files", "commands", "activities", "errors", "stack")
LISTS = ("tags",) + ANCHORS
# task_ref: the work item recurrence opened; bug_ref: a bug-registry fingerprint or issue; quarantine: why the screen
# holds the lesson out of recall; superseded_by: the lesson that replaced it; author: who wrote it. Optional: a file
# written before them reads them as "".
REFS = ("task_ref", "bug_ref", "quarantine", "superseded_by")
FIELDS = ("id", "title", "scope", "kind", "severity", "status", "occurrences", "prevention", "prevention_ref",
          "retire_when") + REFS + ("tags",) + ANCHORS + ("first_seen", "last_seen", "version", "author", "created_by",
                                                         "updated", "updated_by")
SECTIONS = ("What happened", "Root cause", "Fix", "Prevention", "Detection", "Evidence")
TEXT = ("title",) + SECTIONS  # what agents read, and what the screen judges


def _dir(scope: str) -> Path:
    return LESSONS / scope


def parse(text: str) -> dict:
    """A lesson file -> {meta, extra, sections, changelog}. Only the anchor and tag fields are lists (a title in
    brackets stays text); the record's changelog is the last `## Changelog` outside fenced code (work.split_changelog),
    and a `## ` line inside fenced code belongs to its section. `extra`: front-matter lines devkit does not know,
    kept as they are."""
    front, body = work.split_frontmatter(text)
    meta, extra = work.read_frontmatter(front, FIELDS, LISTS)
    body, changelog = work.split_changelog(body)
    lines = body.split("\n")
    fenced = work._fenced(lines)
    sections, cur = {}, None
    for i, ln in enumerate(lines):
        if ln.startswith("## ") and i not in fenced:
            cur = ln[3:].strip()
            sections[cur] = []
        elif cur:
            sections[cur].append(ln)
    meta["occurrences"] = int(meta.get("occurrences") or 1)
    meta["version"] = int(meta.get("version") or 1)
    for k in REFS + ("author",):
        meta[k] = meta.get(k) or ""
    return {"meta": meta, "extra": extra, "sections": {k: "\n".join(v).strip() for k, v in sections.items()},
            "changelog": changelog}


def _one_line(v) -> str:
    return " ".join(str(v if v is not None else "").split("\n")).rstrip()


def render(lesson: dict) -> str:
    m = lesson["meta"]
    head = ""
    for k in FIELDS:
        v = "[" + ", ".join(m.get(k) or []) + "]" if k in LISTS else _one_line(m.get(k, ""))
        head += f"{k}: {v}".rstrip() + "\n"
    head += "".join(ln + "\n" for ln in lesson.get("extra") or [])
    others = [s for s in lesson["sections"] if s not in SECTIONS]  # a section written by hand stays
    body = f"# {_one_line(m['title'])}\n\n" + "".join(f"## {s}\n\n{lesson['sections'].get(s) or '_—_'}\n\n"
                                                    for s in list(SECTIONS) + others)
    return "---\n" + head + "---\n" + body + "## Changelog\n\n" + "".join(f"- {c}\n" for c in lesson["changelog"])


def load(path: Path) -> dict:
    les = parse(Path(path).read_text())
    les["path"] = Path(path)
    return les


def all_lessons(scope: str = None, archived: bool = False) -> list:
    base = (LESSONS / "archive" / scope if archived else _dir(scope)) if scope else \
        (LESSONS / "archive" if archived else LESSONS)
    if not base.exists():
        return []
    files = sorted(base.rglob("L-*.md"))
    if not archived:
        files = [f for f in files if "archive" not in f.relative_to(LESSONS).parts[:1]]
    out = []
    for f in files:
        try:
            out.append(load(f))
        except FileNotFoundError:  # merged, archived or pulled away by another session since rglob listed it
            continue
    return out


def is_archived(les: dict) -> bool:
    try:
        return Path(les["path"]).relative_to(LESSONS).parts[0] == ARCHIVE
    except (KeyError, ValueError, IndexError):
        return False


def find(selector: str, scope: str = None) -> dict:
    """A lesson by id (exact or unique prefix) or a unique part of its title: active lessons first, then the
    archive, so a mistake that repeats an archived lesson finds it."""
    sel = selector.strip().lower()
    shelves = (all_lessons(scope) or all_lessons(), all_lessons(scope, archived=True) or all_lessons(archived=True))
    for items in shelves:
        exact = [x for x in items if x["meta"]["id"].lower() == sel]
        if exact:
            return exact[0]
    for items in shelves:
        hits = [x for x in items if x["meta"]["id"].lower().startswith(sel) or sel in x["meta"]["title"].lower()]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise ValueError(f"“{selector}” matches {len(hits)} lessons: " + ", ".join(h["meta"]["id"] for h in hits[:5]))
    raise ValueError(f"“{selector}” matches 0 lessons")


def _log(les: dict, a: dict, op: str, why: str = ""):
    m = les["meta"]
    m["updated"], m["updated_by"] = work.now_iso(), f"{a['user']}@{a['host']}"
    les["changelog"].insert(0, f"v{m['version']} · {m['updated']} · {work.actor_str(a)} · {op}" +
                            (f" — why: {why}" if why else ""))


def anchors(les: dict) -> dict:
    """{files, commands, activities, errors, stack: [str]} of a lesson; a lesson written before anchors has none."""
    m = les.get("meta") or {}
    return {k: [str(x) for x in m.get(k)] if isinstance(m.get(k), list) else [] for k in ANCHORS}


def _lint():
    """The capture checks (lessonlint), imported when a lesson is written."""
    from . import lessonlint
    return lessonlint


def _refuse(errors: list) -> None:
    if errors:
        raise ValueError("; ".join(errors))


def _clean_anchors(**given) -> dict:
    """The anchors given, validated: one line each, no commas (the front matter's list separator), activity ids the
    crew's RACI knows, and the anchor gate (lessonlint.check_anchors: no bare common program as a command, no short or
    stoplisted error). Only anchors being added are judged: a lesson written before the gate still loads."""
    out = {}
    for k in ANCHORS:
        items = []
        for x in given.get(k) or []:
            x = " ".join(str(x).split())
            if not x:
                continue
            if "," in x:
                raise ValueError(f"an anchor cannot contain a comma: {x!r} (give each one on its own flag)")
            if k == "activities":
                from . import raci
                x = raci.resolve_activity(x)["id"]
            if x not in items:
                items.append(x)
        out[k] = items
    _refuse(_lint().check_anchors(out))
    return out


class Duplicate(ValueError):
    """`add` found a lesson, active or archived, that records the same mistake: an error or command anchor in common.
    `lesson`: that lesson."""

    def __init__(self, les: dict):
        self.lesson = les
        lid = les["meta"]["id"]
        where = ", archived" if is_archived(les) else ""
        super().__init__(f"looks like {lid} (“{les['meta']['title'][:80]}”{where}): run `dev lesson recur {lid} "
                         f"--evidence …`, or add `--new --why …` if it is another mistake")


def same_record(scope: str, anchored: dict) -> list:
    """The lessons of this scope and the global ones, active or archived, that share an error anchor (any case) or a
    command anchor with these anchors: the active ones first, then by occurrences. A superseded lesson is left out
    (its successor is the one to recur)."""
    errs = {e.casefold() for e in anchored.get("errors") or []}
    cmds = set(anchored.get("commands") or [])
    if not (errs or cmds):
        return []
    scopes = [scope] + ([] if scope == "global" else ["global"])
    hits, seen = [], set()
    for archived in (False, True):
        for s in scopes:
            for les in all_lessons(s, archived=archived):
                m, an = les["meta"], anchors(les)
                if m["id"] in seen or m.get("superseded_by") or m.get("scope") != s:
                    continue
                if errs & {e.casefold() for e in an["errors"]} or cmds & set(an["commands"]):
                    seen.add(m["id"])
                    hits.append((archived, -m["occurrences"], les))
    return [les for _, _, les in sorted(hits, key=lambda h: h[:2])]


def author_of(a: dict, role: str = "") -> str:
    """Who writes a lesson: a person as user@host; an agent as `<role or agent> · <agent>:<session> · user@host`."""
    person = f"{a['user']}@{a['host']}"
    if (a.get("agent") or "human") == "human":
        return f"{role} · {person}" if role else person
    run = f"{a['agent']}:{a['session']}" if a.get("session") else a["agent"]
    return " · ".join((role or f"{a['agent']} agent", run, person))


def _screen(fields: dict) -> str:
    """The reason the screen (lessonlint.screen) holds this text out of recall, or ""."""
    return _lint().screen({k: v for k, v in fields.items() if v}) if fields else ""


def add(scope: str, title: str, kind: str = "mistake", severity: str = "medium", happened: str = "", cause: str = "",
        fix: str = "", prevention: str = "", detection: str = "", evidence=None, tags=None, why: str = "",
        a=None, retire_when: str = "", files=None, commands=None, activities=None, errors=None, stack=None,
        new: bool = False, author: str = "") -> dict:
    """Record a lesson. Refused (ValueError) by the anchor gate, the capture shape (lessonlint.check_shape), or a
    lesson of this scope or the global ones, active or archived, that shares an error or command anchor (Duplicate;
    `new=True` with a `why` records it anyway). Text the screen judges unsafe is recorded but held: `quarantine`
    says why, and recall leaves it out until the developer releases it. `author`: the writer's role (an agent) or
    name; the agent, session and machine are added."""
    anchored = _clean_anchors(files=files, commands=commands, activities=activities, errors=errors, stack=stack)
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")
    if not (cause.strip() and prevention.strip()):
        raise ValueError("a lesson needs the root cause (--cause) and the prevention (--prevent): what makes it not "
                         "happen again")
    evidence = [str(e) for e in evidence or []]
    _refuse(_lint().check_shape(title, happened, evidence))
    twins = same_record(scope, anchored)
    if twins and not new:
        raise Duplicate(twins[0])
    if twins and not why.strip():
        raise ValueError(f"it looks like {twins[0]['meta']['id']}: say why it is another mistake (--why \"…\")")
    a = a or work.actor()
    lid = "L-" + work.new_id("task")[2:]
    now, day = work.now_iso(), today()
    sections = {"What happened": happened, "Root cause": cause, "Fix": fix, "Prevention": prevention,
                "Detection": detection, "Evidence": "\n".join(f"- {e}" for e in evidence)}
    les = {"meta": {"id": lid, "title": title.strip(), "scope": scope, "kind": kind, "severity": severity,
                    "status": "open", "occurrences": 1, "prevention": "rule", "prevention_ref": "",
                    "retire_when": retire_when.strip(), "task_ref": "", "bug_ref": "",
                    "quarantine": _screen({"title": title, **sections}), "superseded_by": "",
                    "tags": list(tags or []), **anchored, "first_seen": day, "last_seen": day, "version": 1,
                    "author": author_of(a, author.strip()), "created_by": f"{a['user']}@{a['host']}", "updated": now,
                    "updated_by": f"{a['user']}@{a['host']}"},
           "extra": [], "sections": sections,
           "changelog": [f"v1 · {now} · {work.actor_str(a)} · recorded" +
                         (f" as another mistake than {twins[0]['meta']['id']}" if twins else "") +
                         (f" — why: {why}" if why else "")]}
    path = _dir(scope) / f"{lid}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    work.write_record(path, render(les))
    les["path"] = path
    work._commit([path], f"{lid}: lesson “{title[:60]}” ({scope}, {work.actor_str(a)})")
    return les


def today(ts: float = None) -> str:
    """The local date, as the handoff repository writes dates."""
    return time.strftime("%Y-%m-%d", time.localtime(time.time() if ts is None else ts))


def _lock_of(lid: str) -> Path:
    """The path whose lock (state.hold) serializes the writers of one lesson: STATE/lessons/locks/rec-<id>.lock."""
    return _state_file("lessons") / f"{lid}.md"


def update(les: dict, op, why: str = "", a=None, apply=None, paths=(), **changes) -> dict:
    """Change a lesson and record it, under the lesson's lock: re-read the file, apply the changes (and those
    `apply(fresh lesson) -> dict` computes from what is on disk, so concurrent writers never undo each other), then
    replace the file atomically, refusing to drop any line this change does not edit (work.write_record). `op` may
    be a function of the fresh lesson. Text that changes is screened: when the screen holds it, `quarantine` says
    why (only `release` clears it). The commit, with any other `paths` the change moved or removed, runs after the
    lock is released."""
    a = a or work.actor()
    path = Path(les["path"])
    with state.hold(_lock_of(les["meta"]["id"])):
        les = load(path)
        if apply:
            changes = {**changes, **apply(les)}
        if callable(op):
            op = op(les)
        m, edited, changed = les["meta"], [], {}
        for k, v in changes.items():
            if k in les["sections"] or k in SECTIONS:
                old = les["sections"].get(k) or ""
                if old != v:
                    edited += old.splitlines()
                    changed[k] = v
                les["sections"][k] = v
            else:
                if k == "title" and m.get("title") != v:
                    edited.append(f"# {m.get('title')}")
                    changed[k] = v
                m[k] = v
        held = _screen(changed)
        if held:
            m["quarantine"] = held
        m["version"] = int(m.get("version") or 1) + 1
        _log(les, a, op + (f"; held: {held}" if held else ""), why)
        work.write_record(path, render(les), edited)
    les["path"] = path
    work._commit([path] + _committable(paths),
                 f"{m['id']}: {op} v{m['version']} ({work.actor_str(a)})" + (f" — {why}" if why else ""))
    return les


def _committable(paths) -> list:
    """The paths a commit can name: those that exist, and removed ones git tracks (git add refuses the others)."""
    out = []
    for p in paths:
        p = Path(p)
        if p.exists():
            out.append(p)
        elif (work.HANDOFF / ".git").exists():
            try:
                rel = str(p.relative_to(work.HANDOFF))
            except ValueError:
                continue
            if core.git(work.HANDOFF, "ls-files", "--error-unmatch", "--", rel)[0] == 0:
                out.append(p)
    return out


def _copies(les: dict):
    """(active path, archived path) of a lesson's id in its scope."""
    name, scope = f"{les['meta']['id']}.md", les["meta"]["scope"]
    return _dir(scope) / name, LESSONS / ARCHIVE / scope / name


def _settle(les: dict, a=None) -> dict:
    """One id, one file. When a lesson is both active and archived (a restore that raced an archive, or a repeat
    recorded on another machine while it was archived), keep the copy with the most history (version, then
    occurrences; the active one on a tie) at the active path, remove the other, and record it. -> the lesson."""
    act, arc = _copies(les)
    if not (act.exists() and arc.exists()):
        return les
    with state.hold(_lock_of(les["meta"]["id"])):
        if not (act.exists() and arc.exists()):
            return load(act if act.exists() else arc)
        mine, other = load(act), load(arc)
        rank = (lambda x: (x["meta"]["version"], x["meta"]["occurrences"]))
        if rank(other) > rank(mine):
            work._replace(act, arc.read_text())
            op = f"kept the archived copy (v{other['meta']['version']}) over the active one (v{mine['meta']['version']})"
            changes = {"status": status_before_archive(other)}
        else:
            op = f"kept the active copy (v{mine['meta']['version']}); removed the archived one " \
                 f"(v{other['meta']['version']})"
            changes = {}
        arc.unlink()
    return update(load(act), op, "one lesson, one file", a, paths=[arc], **changes)


def _unarchive(les: dict) -> tuple:
    """Move an archived lesson's file back to its scope, under its lock (a second writer finds it moved).
    -> (the lesson at its active path, the archived path to commit)."""
    act, arc = _copies(les)
    with state.hold(_lock_of(les["meta"]["id"])):
        if arc.exists() and not act.exists():
            act.parent.mkdir(parents=True, exist_ok=True)
            arc.rename(act)
    les["path"] = act
    return les, arc


def recur(les: dict, evidence: str = "", why: str = "", a=None, project: str = "") -> dict:
    """Count a repeat, from the lesson as it is on disk (two agents recording the same repeat both count). An
    archived lesson comes back to its scope. A retired or prevented lesson that happens again is open again: its
    prevention did not hold, and recall shows only open lessons. The changelog line says which prevention failed.
    Every third occurrence opens a work item to automate the prevention (`automate`); `project`: where a global
    lesson's item goes (the project the repeat happened in)."""
    day = today()
    les, moved = _settle(les, a), ()
    if is_archived(les):
        les, arc = _unarchive(les)
        moved = (arc,)

    def what(fresh):
        m = fresh["meta"]
        was = status_before_archive(fresh) if moved else m.get("status")
        op = f"happened again (#{m['occurrences'] + 1})"
        if was == "prevented":
            ref = f" ({m['prevention_ref']})" if m.get("prevention_ref") else ""
            op += f"; reopened: its prevention by {m.get('prevention') or 'rule'}{ref} did not hold"
        elif was == "retired":
            op += "; reopened: it was retired"
        return op + ("; restored from the archive" if moved else "")

    def bump(fresh):
        m = fresh["meta"]
        ev = fresh["sections"].get("Evidence") or ""
        if evidence:
            ev = (ev + "\n" if ev and ev != "_—_" else "") + f"- {day}: {evidence}"
        return {"occurrences": m["occurrences"] + 1, "last_seen": day, "Evidence": ev,
                "status": "open" if moved or m.get("status") in ("retired", "prevented") else m.get("status")}
    les = update(les, what, why or evidence, a, apply=bump, paths=moved)
    if les["meta"]["occurrences"] % AUTOMATE_EVERY == 0:
        les = automate(les, a, project)
    return les


AUTOMATE_EVERY = 3  # every third occurrence asks for automation
PREVENTION_GRACE_DAYS = 7  # a repeat sooner than this after `prevent` means the prevention failed


def about_devkit(les: dict) -> bool:
    """Is the lesson about devkit itself, so its fix is a devkit change? Its scope is devkit's repository, or an
    anchor names a `dev` command, devkit's code or an error that says devkit."""
    m, an = les["meta"], anchors(les)
    return m.get("scope") == core.DEVKIT_REPO or any(_words_of(c)[:1] == ["dev"] for c in an["commands"]) or \
        any(str(f).startswith(("lib/devkit/", "bin/dev", "plugin/")) for f in an["files"]) or \
        any("devkit" in e.casefold() for e in an["errors"])


def _item_open(ref: str, scope: str) -> bool:
    if not ref:
        return False
    try:
        return work.find(ref, scope)["meta"].get("status") in work.OPEN
    except ValueError:
        return False


def automate(les: dict, a=None, project: str = "") -> dict:
    """Open one work item to automate a recurring lesson's prevention (a bug when the lesson is about devkit, a task
    otherwise) in the lesson's scope, or in `project` for a global lesson, and record it as `task_ref`. Nothing while
    the item already recorded is open, or for a global lesson with no project."""
    m = les["meta"]
    scope = project if m["scope"] == "global" else m["scope"]
    if not scope or _item_open(m.get("task_ref"), scope):
        return les
    kind, n = ("bug" if about_devkit(les) else "task"), m["occurrences"]
    prevention = (les["sections"].get("Prevention") or "").strip()
    rule = prevention.splitlines()[0][:160] if prevention and prevention != "_—_" else ""
    why = f"lesson {m['id']} happened {n} times and its prevention is still a {m.get('prevention') or 'rule'}"
    body = work.task_body(kind, why=why + (f": {rule}" if rule else ""), done=[
        f"a test, lint, guard rule, skill or CI check stops it, recorded with `dev lesson prevent {m['id']} --by … "
        f"--ref …`"])
    it = work.create(scope, f"Automate the prevention of {m['id']}: {m['title'][:60]}", kind=kind, body=body, why=why,
                     a=a, links=[f"lesson:{m['id']}"])
    return update(les, f"work item {it['meta']['id']} opened to automate it (#{n})", why, a,
                  task_ref=it["meta"]["id"])


def release(les: dict, why: str, a=None) -> dict:
    """Let a held lesson back into recall (the developer's call: `dev lesson release`, granted in the guard): one
    held in its front matter, or one the screen holds as loaded (held_reasons). The release counts for the text the
    developer read, on this machine (RELEASED): an edit of that text, or a copy of the file on another machine, is
    screened again."""
    if not (why or "").strip():
        raise ValueError("releasing a held lesson needs the reason: --why \"…\"")
    held = les["meta"].get("quarantine") or held_reasons([les]).get(les["meta"]["id"], "")
    if not held:
        raise ValueError(f"{les['meta']['id']} is not held")
    _remember_release(les)
    if not les["meta"].get("quarantine"):
        return update(les, f"released on this machine (the screen held it as loaded: {held})", why, a)
    return update(les, f"released (it was held: {held})", why, a, quarantine="")


def supersede(old: dict, new: dict, why: str, a=None) -> dict:
    """Mark `old` replaced by `new`, so two contradicting lessons never both reach an agent. `new` must be a lesson
    recall can show: active, not retired, not held, not itself superseded."""
    o, n = old["meta"], new["meta"]
    if not (why or "").strip():
        raise ValueError("say why the new lesson replaces the old one: --why \"…\"")
    if o["id"] == n["id"]:
        raise ValueError("a lesson cannot supersede itself")
    fresh = load(new["path"])["meta"]
    if is_archived(new) or fresh.get("status") == "retired":
        raise ValueError(f"{n['id']} is {'archived' if is_archived(new) else 'retired'}: restore it first")
    if fresh.get("quarantine"):
        raise ValueError(f"{n['id']} is held ({fresh['quarantine']}): it can replace a lesson once the developer "
                         f"releases it")
    if fresh.get("superseded_by"):
        raise ValueError(f"{n['id']} is itself superseded by {fresh['superseded_by']}")
    old = update(old, f"superseded by {n['id']}", why, a, superseded_by=n["id"])
    update(new, f"supersedes {o['id']}", why, a)
    return old


def prevent(les: dict, by: str, ref: str, why: str, a=None) -> dict:
    if by not in PREVENTIONS:
        raise ValueError(f"--by must be one of {', '.join(PREVENTIONS)}")
    if not why.strip():
        raise ValueError("say why this prevents it: --why \"…\"")
    return update(les, f"prevented by {by}: {ref}", why, a, status="prevented", prevention=by, prevention_ref=ref)


def set_anchors(les: dict, why: str, a=None, **given) -> dict:
    """Add anchors to a lesson (the backfill of lessons written before anchors existed)."""
    if not (why or "").strip():
        raise ValueError("say why: --why \"…\"")
    new = _clean_anchors(**given)
    if not any(new.values()):
        raise ValueError("name at least one anchor: --file, --command, --activity, --error or --stack")
    have = anchors(les)
    merged = {k: have[k] + [x for x in new[k] if x not in have[k]] for k in ANCHORS}
    return update(les, "anchored: " + "; ".join(f"{k} {', '.join(v)}" for k, v in new.items() if v), why, a, **merged)


def retire(les: dict, why: str, a=None) -> dict:
    if not why.strip():
        raise ValueError("retiring a lesson needs the reason: --why \"…\"")
    return update(les, "retired", why, a, status="retired")


# --------------------------------------------------------------------------- recall

# --------------------------------------------------------------------------- usage and strength

USAGE_LOCAL = STATE / "lessons-usage.json"
HALF_LIFE_DAYS = 45.0


def _usage_local() -> dict:
    return state.read(USAGE_LOCAL) or {}


def track(ids, kind: str, channel: str = "") -> None:
    """Count a use of these lessons on this machine: `kind` and `<kind>_last`, and `<kind>_<channel>` when a channel
    is given. kind: shown | recur | lookup | applied | heeded | missed; channel: session | brief | call | output |
    refusal. Written through state.update (the file's lock, then an atomic replace), so concurrent hooks all count;
    a file that does not parse is never written over (the count is dropped, and state audits it)."""
    if not ids:
        return
    now = int(time.time())

    def bump(u):
        for lid in ids:
            e = u.setdefault(lid, {})
            e[kind] = int(e.get(kind) or 0) + 1
            e[f"{kind}_last"] = now
            if channel:
                e[f"{kind}_{channel}"] = int(e.get(f"{kind}_{channel}") or 0) + 1
    try:
        state.update(USAGE_LOCAL, bump, indent=1)
    except (state.StateBusy, OSError):
        pass


def publish_usage(a=None) -> bool:
    """lessons/usage/<user>@<host>.json: this machine's counts, so the team's usage can be summed (written by sync)."""
    a = a or work.actor("human")
    f = LESSONS / "usage" / f"{a['user']}@{a['host']}.json"
    data = _usage_local()
    if not data:
        return False
    old = core.read_json(f, None)
    if old == data:
        return False
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    return True


def usage() -> dict:
    """Team-wide usage: counts summed over machines, latest timestamps kept."""
    total = {}
    files = list((LESSONS / "usage").glob("*.json")) if (LESSONS / "usage").exists() else []
    mine = _usage_local()
    sources = [core.read_json(f, {}) or {} for f in files] or [mine]
    for src in sources:
        for lid, e in src.items():
            t = total.setdefault(lid, {})
            for k, v in e.items():
                t[k] = max(int(t.get(k) or 0), int(v)) if k.endswith("_last") else int(t.get(k) or 0) + int(v)
    return total


def _state_file(name: str) -> Path:
    return STATE / name


def _day_ts(day: str) -> float:
    """A `YYYY-MM-DD` date (local, like the rest of the handoff) -> its local midnight in epoch seconds; 0.0 when
    unreadable."""
    try:
        return time.mktime(time.strptime(str(day or "").strip(), "%Y-%m-%d"))
    except (ValueError, OverflowError):
        return 0.0


SEEN_BY = ("recur", "applied", "heeded", "missed")  # usage kinds that are sightings: occurrences and outcomes


def last_seen(les: dict, use: dict = None) -> float:
    """When the lesson was last seen: its last occurrence, or the last outcome of showing it (applied, heeded,
    missed). Editing its text, anchoring, merging or preventing it is not a sighting."""
    e = (use or {}).get(les["meta"]["id"], {})
    return max([_day_ts(les["meta"].get("last_seen"))] + [float(e.get(f"{k}_last") or 0) for k in SEEN_BY])


def strength(les: dict, use: dict = None, now: float = None) -> float:
    """How alive a lesson is: severity × occurrences × the outcomes of showing it, decaying with a 45-day half-life
    from when it was last seen (last_seen). Heeded (shown, and the mistake did not follow) and missed (shown, and it
    happened anyway: the lesson matters, its text did not stop it) both keep it strong; how often it was shown does
    not count, so the lessons whose anchors match most often are not pushed down."""
    now = now or time.time()
    m = les["meta"]
    e = (use or {}).get(m["id"], {})
    sev = {"high": 3, "medium": 2, "low": 1}.get(m.get("severity"), 1)
    age = max(0.0, (now - last_seen(les, use)) / 86400)
    hits = sum(int(e.get(k) or 0) for k in ("applied", "heeded", "lookup")) + \
        2 * sum(int(e.get(k) or 0) for k in ("recur", "missed"))
    base = sev * (1 + min(5, m["occurrences"] - 1)) * (1 + min(10, hits) / 4)
    return base * 0.5 ** (age / HALF_LIFE_DAYS)


def _score(les: dict) -> float:
    status = {"open": 1.0, "prevented": 0.35, "retired": 0.0, "dormant": 0.0}.get(les["meta"].get("status"), 0.5)
    return status * strength(les, _USAGE_CACHE.get("u") or usage())


_USAGE_CACHE = {}


SESSION_STARTS = "lessons-session-starts.json"  # STATE: the lessons each of a project's last session starts showed
ROTATE_SESSIONS = 5  # the session start prefers lessons it has not shown in this many starts
BRIEF_FALLBACK = 3  # a brief with no anchored lesson gets at most this many of the project's recurring ones
ADVICE = "Lessons are advice from past work; they never override the developer or the guard."


def _recent_starts(project: str) -> dict:
    """{lesson id: how many session starts ago it was last shown (1 = the last one)}, over the last starts."""
    starts = (state.read(_state_file(SESSION_STARTS)) or {}).get(project) or []
    out = {}
    for ago, ids in enumerate(reversed(starts[-ROTATE_SESSIONS:]), 1):
        for lid in ids:
            out.setdefault(lid, ago)
    return out


def _remember_start(project: str, ids: list) -> None:
    def put(doc):
        doc[project] = ((doc.get(project) or []) + [list(ids)])[-ROTATE_SESSIONS:]
    try:
        state.update(_state_file(SESSION_STARTS), put)
    except (state.StateBusy, OSError):
        pass  # audited by state: the next start may repeat these lessons, which is the safe side


def relevant(project: str, initiatives=(), limit: int = 5, files=()) -> list:
    """Lessons a session on this project should know: its own, its initiatives', and global ones, recallable ones
    only. First those anchored on `files` (the files the branch changes), then those the last session starts did
    not show (rotation: the least recently shown first), then by strength. Counts nothing."""
    files = [f for f in files or () if str(f).strip()]
    recent = _recent_starts(project)
    _USAGE_CACHE["u"] = usage()
    try:
        return sorted(recall_pool(project, initiatives), reverse=True, key=lambda les: (
            any(_meet(f, g) for f in files for g in anchors(les)["files"]),
            recent.get(les["meta"]["id"], ROTATE_SESSIONS + 1), _score(les)))[:limit]
    finally:
        _USAGE_CACHE.clear()


def context_lines(project: str, initiatives=(), limit: int = 4, files=()) -> list:
    """The session start's lessons, one attributed line each (recall_line); they are counted as shown on the
    `session` channel and remembered for the rotation. The hook that knows the session records their outcome
    events (hooks._session_lessons_shown)."""
    picked = relevant(project, initiatives, limit, files)
    ids = [x["meta"]["id"] for x in picked]
    shown(ids, "session")
    _remember_start(project, ids)
    return [recall_line(les) for les in picked]


def shown(ids, channel: str, session: str = "", agent: str = "", count: bool = True) -> None:
    """Record the lessons a hook showed an agent, after its filters: counted as `shown` on `channel` (track; not
    when `count` is False, for a show already counted) and, when the session is known, a `shown` outcome event for
    that agent (lessonevents), which its later hits (missed) or the end of its turn or lane (heeded) resolve. A
    write that fails is dropped: the hooks never fail on it."""
    ids = [x for x in dict.fromkeys(ids or ()) if x]
    if not ids:
        return
    if count:
        track(ids, "shown", channel)
    if session:
        from . import lessonevents
        for lid in ids:
            try:
                lessonevents.record("shown", lid, channel, agent or "main", session)
            except ValueError:
                pass  # not a lesson id or a channel the log knows: nothing to resolve later


# The high-trust channels (a refusal, a call's context, a brief) carry a lesson's title and the first line of its
# prevention, each on one line and cut, and who recorded it: never the sections that explain what ran, where a command
# substitution may stand (lessonlint.screen exempts them). `dev lesson show` prints the whole lesson.
RECALL_TITLE_CHARS = 120
RECALL_PREVENTION_CHARS = 160
RECALL_BY_CHARS = 60


def _line(text, limit: int) -> str:
    """One line of plain text: NFKC, no control or format characters (hidden text, direction marks), spaces
    collapsed, at most `limit` characters (a cut ends in "…")."""
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = " ".join("".join(ch if unicodedata.category(ch)[0] != "C" else " " for ch in s).split())
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def recorded_by(les: dict) -> str:
    """The role or person who recorded a lesson."""
    m = les["meta"]
    return (m.get("author") or "").split(" · ")[0].strip() or m.get("created_by") or "unknown"


def recall_line(les: dict) -> str:
    """A lesson as agents read it: attributed data, never an instruction of its own.
    `Lesson L-… (recorded by <role or person>, <date>): <title> — prevention: <first line of the prevention>`, each
    part on one line and cut (RECALL_*_CHARS)."""
    m = les["meta"]
    prevention = (les["sections"].get("Prevention") or "").strip()
    rule = _line(prevention.splitlines()[0], RECALL_PREVENTION_CHARS) if prevention and prevention != "_—_" else ""
    return f"Lesson {m['id']} (recorded by {_line(recorded_by(les), RECALL_BY_CHARS)}, " \
           f"{_line(m.get('first_seen') or '?', 20)}): {_line(m['title'], RECALL_TITLE_CHARS)}" + \
        (f" — prevention: {rule}" if rule else "")


# The screen runs again when a lesson is loaded for recall: lessons sync through the shared handoff repository, so a
# file edited by hand or pulled from a teammate never passed `add`. Its verdict is kept by a hash of the text it
# judged (SCREENED), with the screen's own code as the salt, so recall screens a lesson again only when its text or
# the screen changes; a release counts for the text the developer read, on this machine (RELEASED).
SCREENED = "lessons-screen.json"  # STATE: {"salt": …, "verdicts": {text hash: reason or ""}}
RELEASED = "lessons-released.json"  # STATE: {text hash: lesson id} the developer released here
SCREENED_MAX = 2000


def _screen_fields(les: dict) -> dict:
    """What the screen judges, and the high-trust channels may show: the title, the sections, who recorded it."""
    m = les["meta"]
    out = {"title": m.get("title") or ""}
    out.update({k: les["sections"].get(k) or "" for k in SECTIONS})
    out["recorded by"] = recorded_by(les)
    return out


def text_hash(les: dict) -> str:
    return hashlib.sha256(json.dumps(_screen_fields(les), sort_keys=True).encode()).hexdigest()[:32]


def _screen_salt() -> str:
    """What a verdict depends on besides the text: the screen's code and what it reads (the guard's hatches, the
    audit's secret patterns)."""
    parts = []
    for name in ("lessonlint.py", "guard.py", "audit.py"):
        try:
            st = (Path(__file__).parent / name).stat()
            parts.append(f"{st.st_size}:{st.st_mtime_ns}")
        except OSError:
            parts.append("-")
    return "|".join(parts)


def held_reasons(lessons_: list) -> dict:
    """{lesson id: why recall leaves it out} for these lessons: the hold in its front matter, else the screen's
    verdict on its text as loaded, unless the developer released that text on this machine. Lessons recall may show
    are left out of the result. New verdicts are kept (SCREENED); a busy or unreadable record only costs a screen."""
    salt = _screen_salt()
    doc = state.read(_state_file(SCREENED)) or {}
    verdicts = dict(doc.get("verdicts") or {}) if doc.get("salt") == salt else {}
    released = state.read(_state_file(RELEASED)) or {}
    new, out = {}, {}
    for les in lessons_:
        m = les["meta"]
        if m.get("quarantine"):
            out[m["id"]] = m["quarantine"]
            continue
        h = text_hash(les)
        if h not in verdicts:
            verdicts[h] = new[h] = _screen(_screen_fields(les))
        if verdicts[h] and h not in released:
            out[m["id"]] = verdicts[h]
    if new:
        def keep(d):
            old = d.get("verdicts") if d.get("salt") == salt and isinstance(d.get("verdicts"), dict) else {}
            merged = {**old, **new}
            d.clear()
            d.update(salt=salt, verdicts=dict(list(merged.items())[-SCREENED_MAX:]))
        try:
            state.update(_state_file(SCREENED), keep)
        except (state.StateBusy, OSError):
            pass
    return out


def _remember_release(les: dict) -> None:
    lid, h = les["meta"]["id"], text_hash(les)

    def put(d):
        d[h] = lid
    state.update(_state_file(RELEASED), put)


def _meet(a: str, b: str) -> bool:
    """Could two paths or globs name the same file? ("db/migrations/**" and "/repo/db/migrations/V3.sql": yes)"""
    a, b = str(a or "").strip(), str(b or "").strip()
    if not a or not b:
        return False
    for x, y in ((a, b), (b, a)):
        if fnmatch.fnmatch(x, y) or fnmatch.fnmatch(x, "*/" + y.lstrip("/")):
            return True
    pa, pb = re.split(r"[*?\[]", a, maxsplit=1)[0], re.split(r"[*?\[]", b, maxsplit=1)[0]
    if pb != b and pb and (a.startswith(pb) or ("/" + pb) in a):
        return True
    return pa != a and bool(pa) and (b.startswith(pa) or ("/" + pa) in b)


def _words_of(command: str) -> list:
    """A command's words; quoted text stays one word, so a message that mentions a command is not that command."""
    try:
        return shlex.split(command or "")
    except ValueError:
        return (command or "").split()


def runs(command: str, anchor: str) -> bool:
    """Does the command run the anchor's command words, in order and together ("git push --force")?"""
    have, want = _words_of(command), _words_of(anchor)
    return bool(want) and any(have[i:i + len(want)] == want for i in range(len(have) - len(want) + 1))


def _usable(les: dict) -> bool:
    """Open (a prevented one is remembered by its automation), not held in its front matter, not superseded, with a
    root cause on record and at least one anchor (`dev lesson lint` lists the others)."""
    m = les["meta"]
    cause = (les["sections"].get("Root cause") or "").strip()
    return m.get("status") == "open" and not m.get("quarantine") and not m.get("superseded_by") and \
        cause not in ("", "_—_") and any(anchors(les).values())


def recall_pool(project: str, initiatives=()) -> list:
    """The lessons recall may show: the project's, its initiatives' and the global ones that are usable (_usable)
    and that the screen does not hold as loaded (held_reasons). A hook that asks more than one question of the same
    call loads it once and passes it (`pool`)."""
    pool = [x for x in all_lessons(project) + [y for s in initiatives for y in all_lessons(f"initiatives/{s}")]
            + all_lessons("global") if _usable(x)]
    held = held_reasons(pool)
    return [x for x in pool if x["meta"]["id"] not in held]


def _pick(scored: list, limit: int) -> list:
    """The best of [(score, lesson)]: by score, then strength. Counts nothing: the caller tracks what it shows."""
    _USAGE_CACHE["u"] = usage()
    try:
        ranked = sorted(((sc, _score(les), les) for sc, les in scored), key=lambda x: (-x[0], -x[1]))
    finally:
        _USAGE_CACHE.clear()
    return [x[2] for x in ranked[:limit]]


def for_task(project: str, activity=None, files=None, limit: int = 4, initiatives=()) -> list:
    """The open lessons a step should know: those of its kind of work and of the files in its scope (the project's,
    its initiatives' and the global ones), and the project's high-severity lessons whatever the step. Never by the
    step's words. When no anchor matches, the recurring lessons (seen twice or more), at most 3, so every brief
    carries the mistakes that repeat. Counts nothing: the caller tracks what it shows (channel `brief`)."""
    files = [f for f in files or [] if str(f).strip()]
    pool, scored = recall_pool(project, initiatives), []
    for les in pool:
        an = anchors(les)
        sc = (3 if activity and activity in an["activities"] else 0) + \
            (2 if any(_meet(f, g) for f in files for g in an["files"]) else 0)
        if sc or (les["meta"].get("scope") == project and les["meta"].get("severity") == "high"):
            scored.append((sc, les))
    if not any(sc for sc, _ in scored):
        have = {les["meta"]["id"] for _, les in scored}
        scored += [(0, les) for les in pool if les["meta"]["occurrences"] >= 2 and les["meta"]["id"] not in have]
        limit = min(limit, BRIEF_FALLBACK)
    return _pick(scored, limit)


def for_call(project: str, files=(), command: str = "", limit: int = 3, initiatives=(), pool=None) -> list:
    """The open lessons a tool call should know just in time: those whose command anchor it runs, or whose files it
    touches. Counts nothing: the caller tracks what it shows (channel `call` or `refusal`)."""
    scored = []
    for les in recall_pool(project, initiatives) if pool is None else pool:
        an = anchors(les)
        sc = (3 if command and any(runs(command, c) for c in an["commands"]) else 0) + \
            (2 if any(_meet(f, g) for f in files or () for g in an["files"]) else 0)
        if sc:
            scored.append((sc, les))
    return _pick(scored, limit)


def for_output(project: str, output: str, limit: int = 3, initiatives=(), pool=None) -> list:
    """The open lessons whose error signature a tool's output shows (case does not matter). Counts nothing: the
    caller tracks what it shows (channel `output`)."""
    text = (output or "").casefold()
    scored = [(3, les) for les in (recall_pool(project, initiatives) if pool is None else pool)
              if text and any(e.casefold() in text for e in anchors(les)["errors"])]
    return _pick(scored, limit)


def pitfall_lines(lessons_: list) -> list:
    """The advice line (ADVICE), then one attributed line per lesson (`- ` + recall_line); [] for no lessons."""
    return [ADVICE] + [f"- {recall_line(les)}" for les in lessons_] if lessons_ else []


def search(query: str, scope: str = None) -> list:
    q = query.lower()
    return [x for x in all_lessons(scope) if q in (x["meta"]["title"] + " " + " ".join(x["sections"].values())).lower()]


# --------------------------------------------------------------------------- signals

def signals(project: str = None, repo=None, days: int = 14) -> list:
    """Things worth a lesson that do not have one yet: [(kind, detail, suggested command)]."""
    out = []
    since = time.time() - days * 86400
    known = " ".join(" ".join(x["sections"].values()) + " " + x["meta"]["title"] for x in all_lessons())
    # 1. reverts and fix commits in the repository
    if repo:
        log = core.gitout(repo, "log", f"--since={days}.days", "--format=%h%x09%s", timeout=30)
        for ln in log.splitlines():
            sha, _, subj = ln.partition("\t")
            if sha in known:
                continue
            if subj.lower().startswith("revert"):
                out.append(("revert", f"{sha} {subj}", f'dev lesson add "<what went wrong>" --evidence "commit {sha}" …'))
            elif re.match(r"^fix(\(.+\))?!?:", subj):
                out.append(("fix", f"{sha} {subj}", f'dev lesson add "<the mistake behind it>" --kind bug '
                                                     f'--evidence "commit {sha}" … (skip if trivial)'))
    # 2. repeated guard refusals
    hl = STATE / "logs" / "hooks.log"
    if hl.exists():
        counts = {}
        for ln in hl.read_text(errors="ignore").splitlines()[-4000:]:
            m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}) guard \w+ deny: (.{0,70})", ln)
            if m and time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M")) >= since:
                key = re.sub(r"`[^`]*`", "`…`", m.group(2))[:60]
                counts[key] = counts.get(key, 0) + 1
        for key, n in sorted(counts.items(), key=lambda x: -x[1]):
            if n >= 3 and key[:30] not in known:
                out.append(("guard", f"refused {n}× in {days} days: {key}",
                            'dev lesson add "<why agents keep doing this>" --kind process --global …'))
    # 3. canary misses
    try:
        from . import canary
        misses = [r for r in canary.records(days) if not r.get("ok") and (not project or r.get("project") == project)]
        if len(misses) >= 2:
            out.append(("canary", f"{len(misses)} replies without the status block in {days} days",
                        "dev doctor; check the instructions load (dev canary)"))
    except Exception:
        pass
    # 4. reopened or repeatedly blocked tasks (the developer's corrections are recorded by the model itself)
    if project:
        for it in work.all_items(project):
            reopen = [c for c in it.get("changelog") or [] if " reopen" in c or "unblock" in c]
            if len(reopen) >= 2 and it["meta"]["id"] not in known:
                out.append(("task", f"{it['meta']['id']} “{it['meta']['title']}” went back {len(reopen)} times",
                            "dev lesson add … --evidence \"task " + it["meta"]["id"] + "\""))
    return out


def _entry_ts(entry: str) -> float:
    parts = entry.split(" · ")
    return work.parse_iso(parts[1].strip()) if len(parts) > 1 else 0.0


def prevention_failure(les: dict):
    """When the lesson happened again within 7 days of its latest prevention: {by, ref, days}; else None (also when
    a newer prevention replaced the one that failed)."""
    log = les["changelog"]  # newest first
    for i, entry in enumerate(log):
        op = _op(entry).partition(" — why: ")[0]
        if op.startswith("prevented by"):
            return None
        if "reopened: its prevention by" in op:
            for older in log[i + 1:]:
                was = _op(older).partition(" — why: ")[0]
                if was.startswith("prevented by"):
                    days = (_entry_ts(entry) - _entry_ts(older)) / 86400
                    if days > PREVENTION_GRACE_DAYS:
                        return None
                    by, _, ref = was[len("prevented by"):].partition(":")
                    return {"by": by.strip(), "ref": ref.strip(), "days": round(days, 1)}
            return None
    return None


def review(scope: str = None) -> dict:
    """What to act on: `automate` (open lessons agents missed twice or more after seeing them, most missed first),
    `prevention_failed` (a repeat within 7 days of `prevent`: [{lesson, by, ref, days}]), `held` (lessons recall
    leaves out because the screen holds them, in their front matter or as loaded: [{lesson, reason}]; the developer
    releases one with `dev lesson release`), `recurring` (open, seen twice or more), `stale` (retire or confirm),
    and the counts."""
    items = all_lessons(scope)
    why = held_reasons([x for x in items if x["meta"].get("status") != "retired"])
    held = [{"lesson": x, "reason": why[x["meta"]["id"]]} for x in items if x["meta"]["id"] in why]
    now, use = time.time(), usage()
    missed = (lambda x: int((use.get(x["meta"]["id"]) or {}).get("missed") or 0))
    automate_ = sorted((x for x in items if x["meta"].get("status") == "open" and missed(x) >= 2),
                       key=lambda x: -missed(x))
    failed = []
    for x in items:
        f = prevention_failure(x)
        if f:
            failed.append(dict(lesson=x, **f))
    recurring = [x for x in items if x["meta"]["occurrences"] >= 2 and x["meta"].get("status") == "open"]
    stale = [x for x in items if x["meta"].get("status") == "open" and
             now - work.parse_iso((x["meta"].get("last_seen") or "2000-01-01") + "T00:00:00Z") > 120 * 86400]
    counts = {}
    for x in items:
        counts[x["meta"].get("status")] = counts.get(x["meta"].get("status"), 0) + 1
    return {"automate": automate_, "prevention_failed": failed, "held": held, "recurring": recurring, "stale": stale,
            "counts": counts, "total": len(items)}


def fixed_in_session(top: str, start_head: str) -> list:
    """Fix and revert commits made since the session started (for the Stop hook's one-time reminder)."""
    if not top or not start_head:
        return []
    log = core.gitout(top, "log", f"{start_head}..HEAD", "--format=%h%x09%s", timeout=20)
    return [ln for ln in log.splitlines() if re.match(r"^\w+\t(fix(\(.+\))?!?:|revert)", ln, re.I)]


# --------------------------------------------------------------------------- sleep: consolidate and forget

ARCHIVE = "archive"
CAP_REASON = "over the active cap"  # why an older devkit archived a lesson to fit 25 per scope
CAP_RESTORE = "lessons-cap-restore.json"  # STATE: the marker of the one-time restore of those lessons
VERDICTS = "lessons-merge-verdicts.json"  # STATE: the model's merge verdicts, by pair hash
YOUNG_DAYS = 30  # a lesson younger than this is never archived by the policy


def _anchor_set(les: dict) -> set:
    return {f"{k}:{x}" for k, v in anchors(les).items() for x in v}


def similarity(a: dict, b: dict) -> float:
    """How alike two lessons' anchors are (0 when either has none): never their words."""
    wa, wb = _anchor_set(a), _anchor_set(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def _text(les: dict) -> str:
    return "\n".join(x for x in (les["meta"]["title"], les["sections"].get("Root cause", ""),
                                 les["sections"].get("Prevention", "")) if x and x != "_—_")


def _pair_key(x: dict, y: dict) -> str:
    """The hash of a pair of lessons as the model would judge them: both ids and texts, in either order."""
    a, b = sorted([(x["meta"]["id"], _text(x)), (y["meta"]["id"], _text(y))])
    return hashlib.sha256("\0".join(a + b).encode()).hexdigest()[:24]


def _remember_verdict(key: str, same: bool) -> None:
    def put(doc):
        doc[key] = {"same": bool(same), "ts": int(time.time())}
    try:
        state.update(_state_file(VERDICTS), put)
    except (state.StateBusy, OSError):
        pass  # audited by state: the pair is asked again on a later night, within the model's budget


def same_mistake(x: dict, y: dict, threshold: float = 0.6):
    """-> (same?, why). By the anchors: alike, and the same error signature or command, so a shared file or kind of
    work alone never makes two lessons one. When background AI is on (`ai`), the model decides the pairs the anchors
    leave open, in any language, within its daily budget, but only pairs that share an error or command anchor, and
    each pair once: its verdict is remembered by the pair's hash (ids and texts) in STATE/lessons-merge-verdicts.json."""
    sim = similarity(x, y)
    sig = {i for i in _anchor_set(x) & _anchor_set(y) if i.startswith(("errors:", "commands:"))}
    if sim >= threshold and sig:
        return True, f"{sim:.0%} same anchors"
    if not sig:
        return False, f"{sim:.0%} same anchors, no error or command in common"
    key = _pair_key(x, y)
    known = (state.read(_state_file(VERDICTS)) or {}).get(key)
    if isinstance(known, dict) and "same" in known:
        same = bool(known["same"])
        return same, ("the same mistake" if same else "different") + ", as the model said before"
    from . import ai
    verdict = ai.same_mistake(_text(x), _text(y))
    if verdict is None:
        return False, f"{sim:.0%} same anchors"
    _remember_verdict(key, verdict)
    return verdict, "the same mistake, says the model" if verdict else "different, says the model"


def archive(les: dict, why: str, a=None) -> dict:
    """Forget: move to lessons/archive/<scope>/ with the reason (git keeps it; `dev lesson restore` brings it back)."""
    a = a or work.actor("devkit")
    les = update(les, "forgotten", why, a, status=les["meta"].get("status") if les["meta"].get("status") == "retired"
                 else "dormant")
    src = les["path"]
    _move(les, LESSONS / ARCHIVE / les["meta"]["scope"] / les["path"].name)
    work._commit([les["path"]] + _committable([src]), f"{les['meta']['id']}: forgotten (archived) — {why}")
    return les


def _move(les: dict, dest: Path) -> None:
    """Move a lesson's file (git keeps the history when it is tracked)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    core.git(HANDOFF, "mv", str(les["path"]), str(dest))
    if les["path"].exists():  # not tracked yet
        les["path"].rename(dest)
    les["path"] = dest


def restore(selector: str, why: str, a=None) -> dict:
    items = all_lessons(archived=True)
    hits = [x for x in items if x["meta"]["id"].lower() == selector.lower() or selector.lower() in x["meta"]["title"].lower()]
    if len(hits) != 1:
        raise ValueError(f"“{selector}” matches {len(hits)} archived lessons")
    return _bring_back(hits[0], "restored", why or "needed again", a, status="open")


def _bring_back(les: dict, op: str, why: str, a=None, **changes) -> dict:
    """Move an archived lesson back to its scope and record it; when the same id is active too, keep one file."""
    act, _ = _copies(les)
    if act.exists():
        return update(_settle(les, a), op, why, a, **changes)
    src = les["path"]
    _move(les, act)
    return update(les, op, why, a, paths=[src], **changes)


def _op(entry: str) -> str:
    """The operation of a changelog entry (`v3 · <when> · <who> · <op>[ — why: …]`)."""
    parts = entry.split(" · ", 3)
    return parts[3] if len(parts) == 4 else ""


def archive_reason(les: dict) -> str:
    """Why the lesson was last archived (the why of its latest `forgotten` entry), or ""."""
    for entry in les["changelog"]:  # newest first
        op = _op(entry)
        if op.startswith("forgotten"):
            return op.partition(" — why: ")[2].strip()
    return ""


def status_before_archive(les: dict) -> str:
    """The status a lesson had when it was last archived, read from its changelog: archiving made it dormant."""
    st = before = "open"
    for entry in reversed(les["changelog"]):  # oldest first
        op = _op(entry)
        if op.startswith("forgotten"):
            before = st
        elif op.startswith("prevented by"):
            st = "prevented"
        elif op.startswith("retired"):
            st = "retired"
        elif op.startswith("restored") or "reopened" in op:
            st = "open"
    return before


def _restore_capped(dry_run: bool, now: float, a, acts: list) -> list:
    """Once: bring back every lesson an older devkit archived only to fit its per-scope cap, with the status it had.
    -> the lessons restored (in a dry run, as they would be, their files unmoved). The marker
    STATE/lessons-cap-restore.json makes it run once."""
    marker = _state_file(CAP_RESTORE)
    if marker.exists():
        return []
    active = {x["meta"]["id"] for x in all_lessons()}
    out = []
    for les in all_lessons(archived=True):
        if les["meta"]["id"] in active or not archive_reason(les).startswith(CAP_REASON):
            continue
        before = status_before_archive(les)
        acts.append(("restore", les["meta"]["id"], f"archived only by the old per-scope cap; back as {before}"))
        if dry_run:
            les["meta"]["status"] = before
        else:
            les = _bring_back(les, "restored", "archived only by the old per-scope cap, which is gone", a,
                              status=before)
        out.append(les)
    if not dry_run:
        state.update(marker, lambda d: d.update(ts=int(now), restored=[x["meta"]["id"] for x in out]))
    return out


def _archive_why(les: dict, use: dict, now: float, kept: set):
    """The archive policy: why to archive this lesson tonight, or None. Never a lesson younger than 30 days, one
    seen twice or more, or one a merge kept in the same run; otherwise a retired lesson 30 days after its retirement,
    a prevented one idle 90 days and weak (strength below 1.0), an open one idle 180 days, weak (below 0.5), seen
    once and older than 30 days."""
    m = les["meta"]
    if m["id"] in kept or m["occurrences"] >= 2 or now - _day_ts(m.get("first_seen")) < YOUNG_DAYS * 86400:
        return None
    idle = (now - last_seen(les, use)) / 86400
    st = strength(les, use, now)
    if m.get("status") == "retired" and (now - work.parse_iso(m.get("updated"))) / 86400 > 30:
        return "retired more than 30 days ago"
    if m.get("status") == "prevented" and idle > 90 and st < 1.0:
        return f"prevented by {m.get('prevention')} and unused for {int(idle)} days: the automation remembers it"
    if m.get("status") == "open" and idle > 180 and st < 0.5:
        return f"not seen or used for {int(idle)} days (strength {st:.2f})"
    return None


def consolidate(dry_run: bool = False, now: float = None, a=None) -> list:
    """One night's sleep. -> [(action, lesson id, detail)]; with dry_run nothing changes. Storage is unbounded: a
    lesson leaves only by the archive policy (`_archive_why`) or as the dropped side of a merge."""
    now = now or time.time()
    use = usage()
    acts = []
    restored = _restore_capped(dry_run, now, a, acts)
    items = all_lessons()
    if dry_run:
        items += restored

    # 1. merge near-duplicates (same scope, or a project lesson that repeats a global one)
    gone, kept = set(), set()
    for i, x in enumerate(items):
        for y in items[i + 1:]:
            if x["meta"]["id"] in gone or y["meta"]["id"] in gone:
                continue
            same = x["meta"]["scope"] == y["meta"]["scope"] or "global" in (x["meta"]["scope"], y["meta"]["scope"])
            dup, why = same_mistake(x, y) if same else (False, "")
            if dup:
                keep, drop = sorted((x, y), key=lambda z: (z["meta"]["scope"] != "global", -strength(z, use, now)))
                acts.append(("merge", drop["meta"]["id"], f"into {keep['meta']['id']} ({why})"))
                gone.add(drop["meta"]["id"])
                kept.add(keep["meta"]["id"])
                if not dry_run:
                    ev = "\n".join(v for v in (keep["sections"].get("Evidence"), drop["sections"].get("Evidence"),
                                                f"- merged {drop['meta']['id']}") if v and v != "_—_")
                    both = anchors(keep)
                    for k, v in anchors(drop).items():
                        both[k] += [i for i in v if i not in both[k]]
                    keep_ = update(keep, f"merged {drop['meta']['id']}", "near-duplicate", a,
                                   occurrences=keep["meta"]["occurrences"] + drop["meta"]["occurrences"], Evidence=ev,
                                   **both)
                    keep["meta"] = keep_["meta"]
                    archive(drop, f"merged into {keep['meta']['id']}", a)
    items = [x for x in items if x["meta"]["id"] not in gone]
    # 2. archive by the policy, never to fit a count
    forgotten = set()
    for x in items:
        reason = _archive_why(x, use, now, kept)
        if reason:
            acts.append(("forget", x["meta"]["id"], reason))
            forgotten.add(x["meta"]["id"])
            if not dry_run:
                archive(x, reason, a)
    # 3. a pattern seen in two or more projects belongs in the global lessons
    proj = [x for x in items if x["meta"]["scope"] not in ("global",) and not x["meta"]["scope"].startswith("initiatives/")
            and x["meta"]["id"] not in forgotten]
    for i, x in enumerate(proj):
        for y in proj[i + 1:]:
            if x["meta"]["scope"] != y["meta"]["scope"] and same_mistake(x, y, 0.5)[0]:
                acts.append(("promote", x["meta"]["id"], f"also seen in {y['meta']['scope']} ({y['meta']['id']}): "
                             f"consider one global lesson"))
    return acts
