# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The crew's RACI: for each kind of work (an activity), who does it (R, Responsible), who approves it (A, Accountable:
exactly one member), who is consulted before or during it (C), and who is informed after it (I).

It lives in `config/crew.json` (`activities`), with the team's changes from the handoff repository on top
(`crew/crew.json`, see `crew.load`). Morpheus classifies a task with it (`classify`), routes it to the Responsible
member, refuses an assignment that breaks it (`check_assignment`), and does not end a run until every Accountable
member has approved the work they own (`approvals_missing`)."""
from __future__ import annotations

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


def check_assignment(member_id: str, text: str, roster=None):
    """Why giving this task to this member breaks the RACI, or None. Only a task that is clearly one kind of work
    is checked; a member Responsible, Accountable or Consulted for it, or Responsible for a kind of work that scores
    close to it, may take it."""
    ranked = classify(text, roster, limit=len(activities(roster)) or 1)
    if not ranked or ranked[0][1] < MIN_SCORE:
        return None
    top, score, hits = ranked[0]
    if member_id in (top.get("R") or []) or member_id == top.get("A") or member_id in (top.get("C") or []):
        return None
    mine = max((s for a, s, _ in ranked if member_id in (a.get("R") or [])), default=0)
    if mine and mine >= score - MARGIN:
        return None
    own = [_lower(a["name"]) for a in roles_of(member_id, roster)["R"]]
    return (f"“{text}” is {_lower(top['name'])} work ({', '.join(hits[:3])}): {names(top.get('R'), roster)} does it "
            f"and {_name(top.get('A'), roster)} approves it (the crew's RACI). {_name(member_id, roster)} is "
            f"Responsible for {', '.join(own) or 'nothing'}. Assign it with `--to {handle((top.get('R') or [''])[0], roster)}`, "
            f"split the step, or override with --why \"<reason>\".")


def step_activity(step: dict, roster=None):
    """The activity a plan step was classified as (stored on the step), or classified now from its name."""
    aid = step.get("activity")
    if aid:
        return activity(aid, roster)
    b = best(" ".join(str(step.get(k) or "") for k in ("name", "desc", "what")), roster)
    return b[0] if b else None


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
