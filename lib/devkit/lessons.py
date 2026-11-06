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
  review    `dev lessons review` lists recurring, unprevented and stale lessons
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
          "tags", "first_seen", "last_seen", "version", "created_by", "updated", "updated_by")
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


def all_lessons(scope: str = None) -> list:
    base = _dir(scope) if scope else LESSONS
    if not base.exists():
        return []
    return [load(f) for f in sorted(base.rglob("L-*.md"))]


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
        a=None) -> dict:
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

def _score(les: dict) -> float:
    m = les["meta"]
    sev = {"high": 3, "medium": 2, "low": 1}.get(m.get("severity"), 1)
    age_days = max(0.0, (time.time() - work.parse_iso((m.get("last_seen") or "2000-01-01") + "T00:00:00Z")) / 86400)
    status = {"open": 1.0, "prevented": 0.35, "retired": 0.0}.get(m.get("status"), 0.5)
    return status * sev * (1 + min(5, m["occurrences"] - 1)) / (1 + age_days / 60)


def relevant(project: str, initiatives=(), limit: int = 5) -> list:
    """Lessons a session on this project should know: its own, its initiatives', and global ones."""
    pool = all_lessons(project) + [x for s in initiatives for x in all_lessons(f"initiatives/{s}")] + \
        all_lessons("global")
    pool = [x for x in pool if x["meta"].get("status") != "retired"]
    return sorted(pool, key=_score, reverse=True)[:limit]


def context_lines(project: str, initiatives=(), limit: int = 4) -> list:
    out = []
    for les in relevant(project, initiatives, limit):
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
    # 4. reopened or repeatedly blocked tasks
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


def stats_json() -> str:
    return json.dumps(review()["counts"])
