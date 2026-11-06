# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The crew's RACI: for each kind of work (an activity), who does it (R, Responsible), who approves it (A, Accountable:
exactly one member), who is consulted before or during it (C), and who is informed after it (I).

It lives in `config/crew.json` (`activities`), with the team's changes from the handoff repository on top
(`crew/crew.json`, see `crew.load`). Morpheus classifies a task with it (`classify`), routes it to the Responsible
member, refuses an assignment that breaks it (`check_assignment`), and does not end a run until every Accountable
member has approved the work they own (`approvals_missing`).

Developers write in any language, so a step's kind of work rests on anchors that do not depend on words (`anchor`):
an explicit activity id (`--activity`, `[id]` in a plan spec), then the assigned member's Responsible activity, then
the files in the step's scope (each kind of work's own `files` globs, and `dispatch.FILE_HINTS`). The English
`signals` are only the last, fallback guess."""
from __future__ import annotations

import fnmatch
import re

LETTERS = ("R", "A", "C", "I")
MIN_SCORE = 3   # one phrase or two words; below it a task is not clearly one kind of work: no routing by
                # the RACI and no refusals
MARGIN = 2      # a refusal needs the member's own best activity to score this much lower than the winner


def _roster(roster=None) -> dict:
    if roster is not None:
        return roster
    from . import crew
    return crew.load()


def activities(roster=None) -> list:
    return [a for a in _roster(roster).get("activities") or [] if isinstance(a, dict)]


def activity(aid: str, roster=None):
    return next((a for a in activities(roster) if a.get("id") == aid), None)


def check(data: dict):
    """-> (errors, warnings) for the `activities` of a roster."""
    errors, warnings = [], []
    acts = data.get("activities")
    if acts is None:
        return ["the roster needs `activities` (the RACI)"], []
    if not isinstance(acts, list) or not acts:
        return ["`activities` must be a non-empty list"], []
    ids = {m.get("id") for m in data.get("members") or [] if isinstance(m, dict)}
    main = next((m.get("id") for m in data.get("members") or [] if isinstance(m, dict) and m.get("main")), None)
    seen, responsible = set(), set()
    for i, a in enumerate(acts, 1):
        if not isinstance(a, dict):
            errors.append(f"activity {i} is not an object")
            continue
        aid = a.get("id") or f"activity {i}"
        if aid in seen:
            errors.append(f"activity `{aid}`: duplicate id")
        seen.add(aid)
        if not isinstance(a.get("name"), str) or not a["name"].strip():
            errors.append(f"activity `{aid}`: missing `name`")
        if not isinstance(a.get("A"), str) or not a["A"]:
            errors.append(f"activity `{aid}`: `A` must be exactly one member")
        elif a["A"] not in ids:
            errors.append(f"activity `{aid}`: A `{a['A']}` is not a crew member")
        for k in ("R", "C", "I"):
            v = a.get(k)
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                errors.append(f"activity `{aid}`: `{k}` must be a list of member ids")
                continue
            for x in v:
                if x not in ids:
                    errors.append(f"activity `{aid}`: {k} `{x}` is not a crew member")
        if isinstance(a.get("R"), list):
            if not a["R"]:
                errors.append(f"activity `{aid}`: `R` needs at least one member")
            responsible.update(a["R"])
        sig = a.get("signals")
        if not isinstance(sig, list) or not sig or not all(isinstance(s, str) and s.strip() for s in sig):
            errors.append(f"activity `{aid}`: `signals` must be a non-empty list of words or phrases")
        if "files" in a and not (isinstance(a["files"], list) and all(isinstance(g, str) and g.strip()
                                                                       for g in a["files"])):
            errors.append(f"activity `{aid}`: `files` must be a list of globs (repository paths)")
        if "no_files" in a and not isinstance(a["no_files"], str):
            errors.append(f"activity `{aid}`: `no_files` must be the reason, as text")
    for mid in sorted(ids - responsible - {main}):
        errors.append(f"`{mid}` is Responsible for no activity: give it one in `activities`, or remove it")
    return errors, warnings


def norm(text: str) -> str:
    """Lowercase words for matching: letters, digits, and the joiners of terms such as ci/cd, next.js, a11y."""
    t = str(text or "").lower()
    t = re.sub(r"[^a-z0-9ñáéíóúü./+#-]+", " ", t)
    return " " + " ".join(w.strip(".-/") or w for w in t.split()) + " "


_PATTERNS = {}


def _pattern(signal: str):
    p = _PATTERNS.get(signal)
    if p is None:
        p = _PATTERNS[signal] = re.compile(r"(?<![a-z0-9])" + re.escape(norm(signal).strip()) + r"(?![a-z0-9])")
    return p


def weight(signal: str) -> int:
    """A phrase says more than a word."""
    return 3 if re.search(r"[\s/-]", signal.strip()) else 2


def classify(text: str, roster=None, limit: int = 3) -> list:
    """-> [(activity, score, [signals found])], best first. The score adds each signal's weight once."""
    t = norm(text)
    out = []
    for i, a in enumerate(activities(roster)):
        hits = [s for s in a.get("signals") or [] if _pattern(s).search(t)]
        # a phrase that contains a word that also matched counts once, as the phrase
        hits = [s for s in hits if not any(s != o and len(o) > len(s) and _pattern(s).search(norm(o)) for o in hits)]
        if hits:
            out.append((a, sum(weight(s) for s in hits), hits, i))
    out.sort(key=lambda x: (-x[1], x[3]))
    return [(a, score, hits) for a, score, hits, _ in out[:limit]]


def best(text: str, roster=None):
    """The activity a task clearly is, or None: (activity, score, signals)."""
    ranked = classify(text, roster, limit=2)
    if not ranked or ranked[0][1] < MIN_SCORE:
        return None
    if len(ranked) > 1 and ranked[1][1] == ranked[0][1]:
        return None  # a tie: two kinds of work; Morpheus splits it
    return ranked[0]


def roles_of(member_id: str, roster=None) -> dict:
    """{"R": [activity], "A": [...], "C": [...], "I": [...]} for one member."""
    out = {k: [] for k in LETTERS}
    for a in activities(roster):
        if member_id in (a.get("R") or []):
            out["R"].append(a)
        if a.get("A") == member_id:
            out["A"].append(a)
        if member_id in (a.get("C") or []):
            out["C"].append(a)
        if member_id in (a.get("I") or []):
            out["I"].append(a)
    return out


def _name(mid: str, roster=None) -> str:
    """The role first, then the name: "Backend engineer (Tank)"."""
    m = next((x for x in _roster(roster).get("members") or [] if x.get("id") == mid), None)
    if not m:
        return mid
    return f"{m['title']} ({m['name']})" if m.get("title") else m.get("name") or mid


def handle(mid: str, roster=None) -> str:
    """What a command takes for a member: its name ("Seraph", "The Keymaker" → its id when it has spaces)."""
    m = next((x for x in _roster(roster).get("members") or [] if x.get("id") == mid), None)
    if not m:
        return mid
    return m["name"] if " " not in m["name"] else m["id"]


def names(ids, roster=None) -> str:
    ids = list(ids or [])
    n = [_name(x, roster) for x in ids]
    return n[0] if len(n) == 1 else ", ".join(n[:-1]) + " and " + n[-1] if n else "nobody"


def _lower(s: str) -> str:
    """“Back-end services and APIs” → “back-end services and APIs” (acronyms stay)."""
    return s[:1].lower() + s[1:] if s[:2] != s[:2].upper() else s


def _takes(member_id: str, act: dict) -> bool:
    """May this member take this kind of work: Responsible, Accountable or Consulted for it?"""
    return member_id in (act.get("R") or []) or member_id == act.get("A") or member_id in (act.get("C") or [])


def resolve_activity(ref: str, roster=None) -> dict:
    """An activity by its id or its name ("backend", "Back-end services and APIs"), or ValueError naming the ids."""
    r = str(ref or "").strip()
    a = activity(r.lower(), roster) or next((x for x in activities(roster)
                                          if str(x.get("name") or "").lower() == r.lower()), None)
    if not a:
        raise ValueError(f"no kind of work “{ref}”: use one of {', '.join(x['id'] for x in activities(roster))}")
    return a


_TAG = re.compile(r"\[([A-Za-z0-9_-]+)\]")


def split_activity(spec: str):
    """A plan spec's explicit kind of work: "api=1h after design [backend]" -> ("api=1h after design", "backend")."""
    spec = str(spec or "")
    m = _TAG.search(spec)
    if not m:
        return spec.strip(), ""
    return re.sub(r"\s+", " ", spec[:m.start()] + " " + spec[m.end():]).strip(), m.group(1).lower()


def member_activity(member_id: str, text: str = "", roster=None):
    """The kind of work a member's step is: its only Responsible activity, else the one the words name best, else its
    first."""
    own = roles_of(member_id, roster)["R"]
    if len(own) <= 1:
        return own[0] if own else None
    ids = {a["id"] for a in own}
    return next((a for a, _, _ in classify(text, roster, limit=len(activities(roster))) if a["id"] in ids), own[0])


def _glob_hit(path: str, globs) -> bool:
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(path.split("/")[-1], g) for g in globs)


def file_activities(files, roster=None, text: str = "") -> list:
    """[(activity, number of files)] the files in a step's scope say, most files first. A file counts for the kind
    of work whose own `files` globs name it (config/crew.json, first match), and for the kind of work of the member
    whose files they are (`dispatch.FILE_HINTS`); an ADR is architecture work and a document. On a tie, a kind of
    work's own globs come first."""
    from .dispatch import FILE_HINTS
    acts = activities(roster)
    counts, order = {}, []
    for f in files or []:
        f = str(f or "").strip()
        if not f:
            continue
        own = next((a for a in acts if _glob_hit(f, a.get("files") or [])), None)
        who = next((w for globs, w in FILE_HINTS if _glob_hit(f, globs)), None)
        hinted = member_activity(who, text, roster) if who else None
        for a in (own, hinted):
            if a and not (a is hinted and own and own["id"] == a["id"]):
                if a["id"] not in counts:
                    order.append(a)
                counts[a["id"]] = counts.get(a["id"], 0) + 1
    return sorted(((a, counts[a["id"]]) for a in order), key=lambda x: -x[1])


def anchor(text: str = "", member=None, files=None, activity=None, roster=None):
    """The kind of work a step is -> (activity, source), source one of explicit, member, files, words; (None, "")
    when nothing says. In order: an explicit activity id; the assigned member's Responsible activity; the files in
    scope; the words. A member given work its words clearly name as someone else's (an override, recorded with
    --why) does not change the work's kind, so the work's own approver still signs."""
    if activity:
        try:
            return resolve_activity(activity, roster), "explicit"
        except ValueError:
            pass
    if member and check_assignment(member, text, roster, files=files) is None:
        a = member_activity(member, text, roster)
        if a:
            return a, "member"
    fa = file_activities(files, roster, text)
    if fa:
        return fa[0][0], "files"
    b = best(text, roster)
    return (b[0], "words") if b else (None, "")


def check_assignment(member_id: str, text: str, roster=None, activity=None, files=None):
    """Why giving this task to this member breaks the RACI, or None. In order: an explicit activity decides (the
    member must be Responsible, Accountable or Consulted for it); files in scope that are the member's own kind of
    work confirm it; otherwise a task whose words clearly are one kind of work is checked, and a member Responsible
    for a kind of work that scores close to it may take it."""
    if activity:
        try:
            act = resolve_activity(activity, roster)
        except ValueError as e:
            return str(e)
        if _takes(member_id, act):
            return None
        return _refusal(member_id, f"“{text}” is marked as {_lower(act['name'])} work (--activity {act['id']})",
                        act, roster)
    if any(_takes(member_id, a) for a, _ in file_activities(files, roster, text)):
        return None
    ranked = classify(text, roster, limit=len(activities(roster)) or 1)
    if not ranked or ranked[0][1] < MIN_SCORE:
        return None
    top, score, hits = ranked[0]
    if _takes(member_id, top):
        return None
    mine = max((s for a, s, _ in ranked if member_id in (a.get("R") or [])), default=0)
    if mine and mine >= score - MARGIN:
        return None
    return _refusal(member_id, f"“{text}” is {_lower(top['name'])} work ({', '.join(hits[:3])})", top, roster)


def _refusal(member_id: str, what: str, top: dict, roster=None) -> str:
    own = [_lower(a["name"]) for a in roles_of(member_id, roster)["R"]]
    return (f"{what}: {names(top.get('R'), roster)} does it and {_name(top.get('A'), roster)} approves it (the crew's "
            f"RACI). {_name(member_id, roster)} is Responsible for {', '.join(own) or 'nothing'}. Assign it with "
            f"`--to {handle((top.get('R') or [''])[0], roster)}`, split the step, or override with --why \"<reason>\".")


def step_activity(step: dict, roster=None):
    """The kind of work a plan step is: the one stored on it, else from its anchors (`anchor`): the member its label
    names, the files in its scope, then its words."""
    aid = step.get("activity")
    if aid:
        a = activity(aid, roster)
        if a:
            return a
    mid = None
    if step.get("label"):
        from . import crew
        m = crew._for_label(step["label"], roster)
        mid = m["id"] if m else None
    text = " ".join(str(step.get(k) or "") for k in ("name", "desc", "what", "task") if step.get(k))
    return anchor(text, member=mid, files=step.get("scope"), roster=roster)[0]


def approvals_missing(run: dict, members_of_steps, roster=None) -> list:
    """[(approver id, [step names])] for the Accountable members who have not approved the work they own.

    A done step needs its activity's A to have finished a step of their own in the same run (their review), unless
    the A is the member who did the step or Morpheus (the main session, who signs off by ending the run).
    `members_of_steps(step) -> member id or None` says who did each step."""
    done = [s for s in run.get("plan") or [] if s.get("state") == "done"]
    did = {}
    for s in done:
        mid = members_of_steps(s)
        if mid:
            did.setdefault(mid, []).append(s)
    from . import crew
    main = next((m["id"] for m in crew.members(roster) if m.get("main")), "morpheus")
    owed = {}
    for s in done:
        a = step_activity(s, roster)
        if not a:
            continue
        acc = a.get("A")
        who = members_of_steps(s) or main
        if not acc or acc in (who, main):
            continue
        if any(x is not s for x in did.get(acc, [])):
            continue
        owed.setdefault(acc, []).append(s.get("name") or "?")
    return sorted(owed.items())


def matrix(roster=None) -> list:
    """Rows for a table: activity, R, A, C, I (member names)."""
    return [[a["name"], names(a.get("R"), roster), _name(a.get("A"), roster), names(a.get("C"), roster) if a.get("C")
             else "–", names(a.get("I"), roster) if a.get("I") else "–"] for a in activities(roster)]


def markdown(roster=None) -> str:
    rows = ["| Kind of work | Responsible (does it) | Accountable (approves) | Consulted | Informed |",
            "| --- | --- | --- | --- | --- |"]
    rows += ["| " + " | ".join(r) + " |" for r in matrix(roster)]
    return "\n".join(rows)
