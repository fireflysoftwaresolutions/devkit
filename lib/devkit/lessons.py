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
from pathlib import Path

from . import core, state, work
from .core import HANDOFF, STATE

LESSONS = HANDOFF / "lessons"
KINDS = ("bug", "mistake", "process", "tooling", "security", "performance", "design")
SEVERITIES = ("low", "medium", "high")
PREVENTIONS = ("test", "lint", "type", "guard", "skill", "ci", "rule", "doc")
ANCHORS = ("files", "commands", "activities", "errors", "stack")
LISTS = ("tags",) + ANCHORS
FIELDS = ("id", "title", "scope", "kind", "severity", "status", "occurrences", "prevention", "prevention_ref",
          "retire_when", "tags") + ANCHORS + ("first_seen", "last_seen", "version", "created_by", "updated",
                                              "updated_by")
SECTIONS = ("What happened", "Root cause", "Fix", "Prevention", "Detection", "Evidence")


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
    return [load(f) for f in files]


def find(selector: str, scope: str = None) -> dict:
    sel = selector.strip().lower()
    items = all_lessons(scope) or all_lessons()
    exact = [x for x in items if x["meta"]["id"].lower() == sel]
    if exact:
        return exact[0]
    hits = [x for x in items if x["meta"]["id"].lower().startswith(sel) or sel in x["meta"]["title"].lower()]
    if len(hits) == 1:
        return hits[0]
    raise ValueError(f"“{selector}” matches {len(hits)} lessons" + (": " + ", ".join(h["meta"]["id"] for h in hits[:5])
                                                                    if hits else ""))


def _log(les: dict, a: dict, op: str, why: str = ""):
    m = les["meta"]
    m["updated"], m["updated_by"] = work.now_iso(), f"{a['user']}@{a['host']}"
    les["changelog"].insert(0, f"v{m['version']} · {m['updated']} · {work.actor_str(a)} · {op}" +
                            (f" — why: {why}" if why else ""))


def anchors(les: dict) -> dict:
    """{files, commands, activities, errors, stack: [str]} of a lesson; a lesson written before anchors has none."""
    m = les.get("meta") or {}
    return {k: [str(x) for x in m.get(k)] if isinstance(m.get(k), list) else [] for k in ANCHORS}


def _clean_anchors(**given) -> dict:
    """The anchors given, validated: one line each, no commas (the front matter's list separator), and activity ids
    the crew's RACI knows."""
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
    return out


def add(scope: str, title: str, kind: str = "mistake", severity: str = "medium", happened: str = "", cause: str = "",
        fix: str = "", prevention: str = "", detection: str = "", evidence=None, tags=None, why: str = "",
        a=None, retire_when: str = "", files=None, commands=None, activities=None, errors=None, stack=None) -> dict:
    anchored = _clean_anchors(files=files, commands=commands, activities=activities, errors=errors, stack=stack)
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")
    if not (cause.strip() and prevention.strip()):
        raise ValueError("a lesson needs the root cause (--cause) and the prevention (--prevent): what makes it not "
                         "happen again")
    a = a or work.actor()
    lid = "L-" + work.new_id("task")[2:]
    now, day = work.now_iso(), today()
    les = {"meta": {"id": lid, "title": title.strip(), "scope": scope, "kind": kind, "severity": severity,
                    "status": "open", "occurrences": 1, "prevention": "rule", "prevention_ref": "",
                    "retire_when": retire_when.strip(),
                    "tags": list(tags or []), **anchored, "first_seen": day, "last_seen": day, "version": 1,
                    "created_by": f"{a['user']}@{a['host']}", "updated": now, "updated_by": f"{a['user']}@{a['host']}"},
           "extra": [],
           "sections": {"What happened": happened, "Root cause": cause, "Fix": fix, "Prevention": prevention,
                        "Detection": detection, "Evidence": "\n".join(f"- {e}" for e in evidence or [])},
           "changelog": [f"v1 · {now} · {work.actor_str(a)} · recorded" + (f" — why: {why}" if why else "")]}
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


def update(les: dict, op, why: str = "", a=None, apply=None, **changes) -> dict:
    """Change a lesson and record it, under the lesson's lock: re-read the file, apply the changes (and those
    `apply(fresh lesson) -> dict` computes from what is on disk, so concurrent writers never undo each other), then
    replace the file atomically, refusing to drop any line this change does not edit (work.write_record). `op` may
    be a function of the fresh lesson. The commit to the handoff repository runs after the lock is released."""
    a = a or work.actor()
    path = Path(les["path"])
    with state.hold(_lock_of(les["meta"]["id"])):
        les = load(path)
        if apply:
            changes = {**changes, **apply(les)}
        if callable(op):
            op = op(les)
        m, edited = les["meta"], []
        for k, v in changes.items():
            if k in les["sections"] or k in SECTIONS:
                old = les["sections"].get(k) or ""
                if old != v:
                    edited += old.splitlines()
                les["sections"][k] = v
            else:
                if k == "title" and m.get("title") != v:
                    edited.append(f"# {m.get('title')}")
                m[k] = v
        m["version"] = int(m.get("version") or 1) + 1
        _log(les, a, op, why)
        work.write_record(path, render(les), edited)
    les["path"] = path
    work._commit([path], f"{m['id']}: {op} v{m['version']} ({work.actor_str(a)})" + (f" — {why}" if why else ""))
    return les


def recur(les: dict, evidence: str = "", why: str = "", a=None) -> dict:
    """Count a repeat, from the lesson as it is on disk (two agents recording the same repeat both count). A retired
    or prevented lesson that happens again is open again: its prevention did not hold, and recall shows only open
    lessons. The changelog line says which prevention failed."""
    day = today()

    def what(fresh):
        m = fresh["meta"]
        op = f"happened again (#{m['occurrences'] + 1})"
        if m.get("status") == "prevented":
            ref = f" ({m['prevention_ref']})" if m.get("prevention_ref") else ""
            op += f"; reopened: its prevention by {m.get('prevention') or 'rule'}{ref} did not hold"
        elif m.get("status") == "retired":
            op += "; reopened: it was retired"
        return op

    def bump(fresh):
        m = fresh["meta"]
        ev = fresh["sections"].get("Evidence") or ""
        if evidence:
            ev = (ev + "\n" if ev and ev != "_—_" else "") + f"- {day}: {evidence}"
        return {"occurrences": m["occurrences"] + 1, "last_seen": day, "Evidence": ev,
                "status": "open" if m.get("status") in ("retired", "prevented") else m.get("status")}
    return update(les, what, why or evidence, a, apply=bump)


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
    """How alive a lesson is: severity × evidence, refreshed by use, decaying with a 45-day half-life."""
    now = now or time.time()
    m = les["meta"]
    e = (use or {}).get(m["id"], {})
    sev = {"high": 3, "medium": 2, "low": 1}.get(m.get("severity"), 1)
    age = max(0.0, (now - last_seen(les, use)) / 86400)
    hits = int(e.get("applied") or 0) + 2 * int(e.get("recur") or 0) + int(e.get("lookup") or 0)
    shown = int(e.get("shown") or 0)
    ignored = max(0, shown - 3 * (hits + 1))  # shown often but never used: it is not helping
    base = sev * (1 + min(5, m["occurrences"] - 1)) * (1 + min(10, hits) / 4) / (1 + min(20, ignored) / 10)
    return base * 0.5 ** (age / HALF_LIFE_DAYS)


def _score(les: dict) -> float:
    status = {"open": 1.0, "prevented": 0.35, "retired": 0.0, "dormant": 0.0}.get(les["meta"].get("status"), 0.5)
    return status * strength(les, _USAGE_CACHE.get("u") or usage())


_USAGE_CACHE = {}


def relevant(project: str, initiatives=(), limit: int = 5) -> list:
    """Lessons a session on this project should know: its own, its initiatives', and global ones."""
    pool = all_lessons(project) + [x for s in initiatives for x in all_lessons(f"initiatives/{s}")] + \
        all_lessons("global")
    pool = [x for x in pool if x["meta"].get("status") == "open"]  # prevented: the automation remembers it
    _USAGE_CACHE["u"] = usage()
    try:
        return sorted(pool, key=_score, reverse=True)[:limit]
    finally:
        _USAGE_CACHE.clear()


def context_lines(project: str, initiatives=(), limit: int = 4) -> list:
    out = []
    picked = relevant(project, initiatives, limit)
    track([x["meta"]["id"] for x in picked], "shown")
    for les in picked:
        m = les["meta"]
        rule = (les["sections"].get("Prevention") or "").splitlines()[0][:160]
        tag = "" if m.get("status") == "open" else f" [{m.get('status')} by {m.get('prevention')}]"
        out.append(f"{m['id']} ({m['scope']}, {m['severity']}, ×{m['occurrences']}){tag}: {m['title']} — {rule}")
    return out


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


def _pool(project: str, initiatives=()) -> list:
    return [x for x in all_lessons(project) + [y for s in initiatives for y in all_lessons(f"initiatives/{s}")]
            + all_lessons("global") if x["meta"].get("status") == "open"]


def _pick(project: str, scored: list, limit: int) -> list:
    """The best of [(score, lesson)], each counted as shown."""
    _USAGE_CACHE["u"] = usage()
    try:
        ranked = sorted(((sc, _score(les), les) for sc, les in scored), key=lambda x: (-x[0], -x[1]))
    finally:
        _USAGE_CACHE.clear()
    picked = [x[2] for x in ranked[:limit]]
    track([x["meta"]["id"] for x in picked], "shown")
    return picked


def for_task(project: str, activity=None, files=None, limit: int = 4, initiatives=()) -> list:
    """The open lessons a step should know: those of its kind of work and of the files in its scope (the project's,
    its initiatives' and the global ones), and the project's high-severity lessons whatever the step. Never by the
    step's words. -> lessons (each counted as "shown")."""
    files = [f for f in files or [] if str(f).strip()]
    scored = []
    for les in _pool(project, initiatives):
        an = anchors(les)
        sc = (3 if activity and activity in an["activities"] else 0) + \
            (2 if any(_meet(f, g) for f in files for g in an["files"]) else 0)
        if sc or (les["meta"].get("scope") == project and les["meta"].get("severity") == "high"):
            scored.append((sc, les))
    return _pick(project, scored, limit)


def for_call(project: str, files=(), command: str = "", limit: int = 3, initiatives=()) -> list:
    """The open lessons a tool call should know just in time: those whose command anchor it runs, or whose files it
    touches."""
    scored = []
    for les in _pool(project, initiatives):
        an = anchors(les)
        sc = (3 if command and any(runs(command, c) for c in an["commands"]) else 0) + \
            (2 if any(_meet(f, g) for f in files or () for g in an["files"]) else 0)
        if sc:
            scored.append((sc, les))
    return _pick(project, scored, limit)


def for_output(project: str, output: str, limit: int = 3, initiatives=()) -> list:
    """The open lessons whose error signature a tool's output shows (case does not matter)."""
    text = (output or "").casefold()
    scored = [(3, les) for les in _pool(project, initiatives)
              if text and any(e.casefold() in text for e in anchors(les)["errors"])]
    return _pick(project, scored, limit)


def pitfall_lines(lessons_: list) -> list:
    """One line per lesson: id, what happened, and its prevention."""
    out = []
    for les in lessons_:
        m = les["meta"]
        rule = (les["sections"].get("Prevention") or "").splitlines()[0][:160] if les["sections"].get("Prevention") else ""
        out.append(f"- {m['id']} ({m['severity']}, ×{m['occurrences']}): {m['title']}" + (f" — prevention: {rule}" if rule else ""))
    return out


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


def review(scope: str = None) -> dict:
    """Recurring but unprevented (automate them), stale (retire or confirm), and the counts."""
    items = all_lessons(scope)
    now = time.time()
    recurring = [x for x in items if x["meta"]["occurrences"] >= 2 and x["meta"].get("status") == "open"]
    stale = [x for x in items if x["meta"].get("status") == "open" and
             now - work.parse_iso((x["meta"].get("last_seen") or "2000-01-01") + "T00:00:00Z") > 120 * 86400]
    counts = {}
    for x in items:
        counts[x["meta"].get("status")] = counts.get(x["meta"].get("status"), 0) + 1
    return {"recurring": recurring, "stale": stale, "counts": counts, "total": len(items)}


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
    _move(les, LESSONS / ARCHIVE / les["meta"]["scope"] / les["path"].name)
    work._commit([les["path"]], f"{les['meta']['id']}: forgotten (archived) — {why}")
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
    """Move an archived lesson back to its scope and record it."""
    _move(les, _dir(les["meta"]["scope"]) / les["path"].name)
    return update(les, op, why, a, **changes)


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
