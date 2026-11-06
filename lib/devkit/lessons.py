# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""Lessons: learn from mistakes so they are not repeated, in one project or across all of them.

A lesson is a short, blameless record in the handoff repository: lessons/<scope>/<ID>.md, where scope is a project
id, initiatives/<slug> or `global`. It holds what happened, the root cause, the fix, the prevention (the rule to
follow) and how to detect it early, with evidence (commits, PRs, CI runs) and a versioned changelog.

The loop:
  capture   `dev lesson add`, prompted by signals: reverts and fix commits, repeated guard refusals, canary misses,
            reopened tasks, required-skill reminders (`dev lessons scan`), and the Stop hook after a fix commit
  recall    the session context lists the open lessons that apply (project, its initiatives, global)
  recur     `dev lesson recur` counts a repeat; a lesson that recurs is a candidate for automation
  prevent   `dev lesson prevent --by test|lint|guard|skill|ci|rule --ref …`: automation beats prose
  review    `dev lesson review` lists recurring, unprevented and stale lessons
  use       every time a lesson is shown in a session, applied (named in a reply), repeated or looked up, its usage is
            counted (per machine: lessons/usage/<user>@<host>.json, summed across the team)
  sleep     `dev sleep` (daily, from the background sync) consolidates like a brain at night: strength decays unless
            a lesson is used; weak, prevented or retired lessons are forgotten into lessons/archive/ (restorable);
            near-duplicates are merged; the active set per scope is capped
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import core, work
from .core import HANDOFF, STATE

LESSONS = HANDOFF / "lessons"
KINDS = ("bug", "mistake", "process", "tooling", "security", "performance", "design")
SEVERITIES = ("low", "medium", "high")
PREVENTIONS = ("test", "lint", "type", "guard", "skill", "ci", "rule", "doc")
FIELDS = ("id", "title", "scope", "kind", "severity", "status", "occurrences", "prevention", "prevention_ref",
          "retire_when", "tags", "first_seen", "last_seen", "version", "created_by", "updated", "updated_by")
SECTIONS = ("What happened", "Root cause", "Fix", "Prevention", "Detection", "Evidence")


def _dir(scope: str) -> Path:
    return LESSONS / scope


def parse(text: str) -> dict:
    meta, body = {}, text
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            for ln in text[4:end].splitlines():
                k, _, v = ln.partition(":")
                v = v.strip()
                meta[k.strip()] = [x.strip() for x in v[1:-1].split(",") if x.strip()] \
                    if v.startswith("[") and v.endswith("]") else v
            body = text[end + 5:]
    changelog = []
    if "\n## Changelog" in body:
        body, _, log = body.partition("\n## Changelog")
        changelog = [ln[2:].strip() for ln in log.splitlines() if ln.startswith("- ")]
    sections, cur = {}, None
    for ln in body.splitlines():
        if ln.startswith("## "):
            cur = ln[3:].strip()
            sections[cur] = []
        elif cur:
            sections[cur].append(ln)
    meta["occurrences"] = int(meta.get("occurrences") or 1)
    meta["version"] = int(meta.get("version") or 1)
    return {"meta": meta, "sections": {k: "\n".join(v).strip() for k, v in sections.items()}, "changelog": changelog}


def render(lesson: dict) -> str:
    m = lesson["meta"]
    head = "".join(f"{k}: {'[' + ', '.join(m.get(k) or []) + ']' if k == 'tags' else m.get(k, '')}".rstrip() + "\n"
                   for k in FIELDS)
    body = f"# {m['title']}\n\n" + "".join(f"## {s}\n\n{lesson['sections'].get(s) or '_—_'}\n\n" for s in SECTIONS)
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


def add(scope: str, title: str, kind: str = "mistake", severity: str = "medium", happened: str = "", cause: str = "",
        fix: str = "", prevention: str = "", detection: str = "", evidence=None, tags=None, why: str = "",
        a=None, retire_when: str = "") -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")
    if not (cause.strip() and prevention.strip()):
        raise ValueError("a lesson needs the root cause (--cause) and the prevention (--prevent): what makes it not "
                         "happen again")
    a = a or work.actor()
    lid = "L-" + work.new_id("task")[2:]
    now = work.now_iso()
    les = {"meta": {"id": lid, "title": title.strip(), "scope": scope, "kind": kind, "severity": severity,
                    "status": "open", "occurrences": 1, "prevention": "rule", "prevention_ref": "",
                    "retire_when": retire_when.strip(),
                    "tags": list(tags or []), "first_seen": now[:10], "last_seen": now[:10], "version": 1,
                    "created_by": f"{a['user']}@{a['host']}", "updated": now, "updated_by": f"{a['user']}@{a['host']}"},
           "sections": {"What happened": happened, "Root cause": cause, "Fix": fix, "Prevention": prevention,
                        "Detection": detection, "Evidence": "\n".join(f"- {e}" for e in evidence or [])},
           "changelog": [f"v1 · {now} · {work.actor_str(a)} · recorded" + (f" — why: {why}" if why else "")]}
    path = _dir(scope) / f"{lid}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(les))
    les["path"] = path
    work._commit([path], f"{lid}: lesson “{title[:60]}” ({scope}, {work.actor_str(a)})")
    return les


def update(les: dict, op: str, why: str = "", a=None, **changes) -> dict:
    a = a or work.actor()
    les = load(les["path"])
    m = les["meta"]
    for k, v in changes.items():
        if k in les["sections"] or k in SECTIONS:
            les["sections"][k] = v
        else:
            m[k] = v
    m["version"] = int(m.get("version") or 1) + 1
    _log(les, a, op, why)
    les["path"].write_text(render(les))
    work._commit([les["path"]], f"{m['id']}: {op} v{m['version']} ({work.actor_str(a)})" + (f" — {why}" if why else ""))
    return les


def recur(les: dict, evidence: str = "", why: str = "", a=None) -> dict:
    ev = les["sections"].get("Evidence") or ""
    if evidence:
        ev = (ev + "\n" if ev and ev != "_—_" else "") + f"- {work.now_iso()[:10]}: {evidence}"
    return update(les, f"happened again (#{les['meta']['occurrences'] + 1})", why or evidence, a,
                  occurrences=les["meta"]["occurrences"] + 1, last_seen=work.now_iso()[:10], Evidence=ev,
                  status="open" if les["meta"].get("status") == "retired" else les["meta"].get("status"))


def prevent(les: dict, by: str, ref: str, why: str, a=None) -> dict:
    if by not in PREVENTIONS:
        raise ValueError(f"--by must be one of {', '.join(PREVENTIONS)}")
    if not why.strip():
        raise ValueError("say why this prevents it: --why \"…\"")
    return update(les, f"prevented by {by}: {ref}", why, a, status="prevented", prevention=by, prevention_ref=ref)


def retire(les: dict, why: str, a=None) -> dict:
    if not why.strip():
        raise ValueError("retiring a lesson needs the reason: --why \"…\"")
    return update(les, "retired", why, a, status="retired")


# --------------------------------------------------------------------------- recall

# --------------------------------------------------------------------------- usage and strength

USAGE_LOCAL = STATE / "lessons-usage.json"
HALF_LIFE_DAYS = 45.0


def _usage_local() -> dict:
    return core.read_json(USAGE_LOCAL, {}) or {}


def track(ids, kind: str):
    """Count a use of these lessons on this machine. kind: shown | applied | recur | lookup."""
    if not ids:
        return
    u = _usage_local()
    now = int(time.time())
    for lid in ids:
        e = u.setdefault(lid, {})
        e[kind] = int(e.get(kind) or 0) + 1
        e[f"{kind}_last"] = now
    try:
        USAGE_LOCAL.parent.mkdir(parents=True, exist_ok=True)
        USAGE_LOCAL.write_text(json.dumps(u, indent=1))
    except OSError:
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


def strength(les: dict, use: dict = None, now: float = None) -> float:
    """How alive a lesson is: severity × evidence, refreshed by use, decaying with a 45-day half-life."""
    now = now or time.time()
    m = les["meta"]
    e = (use or {}).get(m["id"], {})
    sev = {"high": 3, "medium": 2, "low": 1}.get(m.get("severity"), 1)
    last = max(work.parse_iso((m.get("last_seen") or "2000-01-01") + "T00:00:00Z"),
               work.parse_iso(m.get("updated") or "2000-01-01T00:00:00Z"),
               float(e.get("applied_last") or 0), float(e.get("recur_last") or 0), float(e.get("lookup_last") or 0))
    age = max(0.0, (now - last) / 86400)
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
    # 4. corrections from the developer (queued by the prompt hook)
    if SIGNALS_FILE.exists():
        for ln in SIGNALS_FILE.read_text().splitlines()[-200:]:
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if r.get("ts", 0) >= since and (not project or r.get("project") in ("", project)):
                out.append(("correction", r.get("text", "")[:120], 'dev lesson add "<what the agent got wrong>" …'))
    # 5. reopened or repeatedly blocked tasks
    if project:
        for it in work.all_items(project):
            reopen = [c for c in it.get("changelog") or [] if " reopen" in c or "unblock" in c]
            if len(reopen) >= 2 and it["meta"]["id"] not in known:
                out.append(("task", f"{it['meta']['id']} “{it['meta']['title']}” went back {len(reopen)} times",
                            "dev lesson add … --evidence \"task " + it["meta"]["id"] + "\""))
    return out


CORRECTION = re.compile(r"(?i)^\s*(no[,.!]|that'?s (wrong|not)|you forgot|te (olvidaste|dije)|otra vez|de nuevo|"
                        r"eso (no|est[aá] mal)|not like that|así no|asi no|wrong\b|incorrect)")
SIGNALS_FILE = STATE / "lesson-signals.jsonl"


def note_correction(prompt: str, project: str = ""):
    """The developer corrected the agent: queue it for `dev lesson scan` (never written as a lesson automatically)."""
    if not prompt or not CORRECTION.search(prompt[:200]):
        return False
    try:
        SIGNALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with SIGNALS_FILE.open("a") as fh:
            fh.write(json.dumps({"ts": int(time.time()), "project": project, "text": prompt.strip()[:200]}) + "\n")
    except OSError:
        return False
    return True


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


def stats_json() -> str:
    return json.dumps(review()["counts"])


# --------------------------------------------------------------------------- sleep: consolidate and forget

ARCHIVE = "archive"
MAX_ACTIVE_PER_SCOPE = 25


def _words(les: dict) -> set:
    text = " ".join([les["meta"]["title"], les["sections"].get("Root cause", ""), les["sections"].get("Prevention", "")])
    stop = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "is", "it", "be", "not", "that",
            "this", "by", "at", "as", "was", "are", "when", "from", "its", "into", "so"}
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower()) if w not in stop}


def similarity(a: dict, b: dict) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def archive(les: dict, why: str, a=None) -> dict:
    """Forget: move to lessons/archive/<scope>/ with the reason (git keeps it; `dev lesson restore` brings it back)."""
    a = a or work.actor("devkit")
    les = update(les, "forgotten", why, a, status=les["meta"].get("status") if les["meta"].get("status") == "retired"
                 else "dormant")
    dest = LESSONS / ARCHIVE / les["meta"]["scope"] / les["path"].name
    dest.parent.mkdir(parents=True, exist_ok=True)
    core.git(HANDOFF, "mv", str(les["path"]), str(dest))
    if les["path"].exists():  # not tracked yet
        les["path"].rename(dest)
    les["path"] = dest
    work._commit([dest], f"{les['meta']['id']}: forgotten (archived) — {why}")
    return les


def restore(selector: str, why: str, a=None) -> dict:
    items = all_lessons(archived=True)
    hits = [x for x in items if x["meta"]["id"].lower() == selector.lower() or selector.lower() in x["meta"]["title"].lower()]
    if len(hits) != 1:
        raise ValueError(f"“{selector}” matches {len(hits)} archived lessons")
    les = hits[0]
    dest = _dir(les["meta"]["scope"]) / les["path"].name
    dest.parent.mkdir(parents=True, exist_ok=True)
    core.git(HANDOFF, "mv", str(les["path"]), str(dest))
    if les["path"].exists():
        les["path"].rename(dest)
    les["path"] = dest
    return update(les, "restored", why or "needed again", a, status="open")


def consolidate(dry_run: bool = False, now: float = None, a=None) -> list:
    """One night's sleep. -> [(action, lesson id, detail)]; with dry_run nothing changes."""
    now = now or time.time()
    use = usage()
    acts = []
    items = all_lessons()

    def days(iso):
        return (now - work.parse_iso(iso)) / 86400 if iso else 9999

    # 1. merge near-duplicates (same scope, or a project lesson that repeats a global one)
    gone = set()
    for i, x in enumerate(items):
        for y in items[i + 1:]:
            if x["meta"]["id"] in gone or y["meta"]["id"] in gone:
                continue
            same = x["meta"]["scope"] == y["meta"]["scope"] or "global" in (x["meta"]["scope"], y["meta"]["scope"])
            if same and similarity(x, y) >= 0.6:
                keep, drop = sorted((x, y), key=lambda z: (z["meta"]["scope"] != "global", -strength(z, use, now)))
                acts.append(("merge", drop["meta"]["id"], f"into {keep['meta']['id']} ({similarity(x, y):.0%} alike)"))
                gone.add(drop["meta"]["id"])
                if not dry_run:
                    ev = "\n".join(v for v in (keep["sections"].get("Evidence"), drop["sections"].get("Evidence"),
                                                f"- merged {drop['meta']['id']}") if v and v != "_—_")
                    keep_ = update(keep, f"merged {drop['meta']['id']}", "near-duplicate", a,
                                   occurrences=keep["meta"]["occurrences"] + drop["meta"]["occurrences"], Evidence=ev)
                    keep["meta"] = keep_["meta"]
                    archive(drop, f"merged into {keep['meta']['id']}", a)
    items = [x for x in items if x["meta"]["id"] not in gone]
    # 2. forget what no longer helps
    for x in items:
        m, st = x["meta"], strength(x, use, now)
        idle = min(days((m.get("last_seen") or "2000-01-01") + "T00:00:00Z"), days(m.get("updated")))
        reason = None
        if m.get("status") == "retired" and days(m.get("updated")) > 30:
            reason = "retired more than 30 days ago"
        elif m.get("status") == "prevented" and idle > 90 and st < 1.0:
            reason = f"prevented by {m.get('prevention')} and unused for {int(idle)} days: the automation remembers it"
        elif m.get("status") == "open" and idle > 180 and st < 0.5:
            reason = f"not seen or used for {int(idle)} days (strength {st:.2f})"
        if reason:
            acts.append(("forget", m["id"], reason))
            if not dry_run:
                archive(x, reason, a)
    # 3. cap the active set per scope (weakest go dormant first)
    by_scope = {}
    for x in items:
        if x["meta"]["id"] not in {i for _, i, _ in acts}:
            by_scope.setdefault(x["meta"]["scope"], []).append(x)
    for scope, xs in by_scope.items():
        if len(xs) > MAX_ACTIVE_PER_SCOPE:
            for x in sorted(xs, key=lambda z: strength(z, use, now))[:len(xs) - MAX_ACTIVE_PER_SCOPE]:
                acts.append(("forget", x["meta"]["id"], f"over the {MAX_ACTIVE_PER_SCOPE}-lesson cap for {scope}"))
                if not dry_run:
                    archive(x, f"over the active cap for {scope}", a)
    # 4. a pattern seen in two or more projects belongs in the global lessons
    proj = [x for x in items if x["meta"]["scope"] not in ("global",) and not x["meta"]["scope"].startswith("initiatives/")
            and x["meta"]["id"] not in {i for _, i, _ in acts}]
    for i, x in enumerate(proj):
        for y in proj[i + 1:]:
            if x["meta"]["scope"] != y["meta"]["scope"] and similarity(x, y) >= 0.5:
                acts.append(("promote", x["meta"]["id"], f"also seen in {y['meta']['scope']} ({y['meta']['id']}): "
                             f"consider one global lesson"))
    return acts
