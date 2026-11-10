# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Step endings, approvals and the reconciler (lifecycle spec F6 and C7; crew review R2-3 and R2-4).

progress.py keeps the run. This module decides, from the run's own data, how a crew member's step ends, who approves
each finished step and whether they have, and which lanes, launches and runs ended without telling devkit.

Rules
- A member's step ends `done` only with evidence: Check ✓ in the status block of its report, or else a
  `dev progress step --check` from its lane whose Check is not ✗ or ⚠. Otherwise it ends `unverified`, and Morpheus
  closes it with evidence or reopens it (`ending`).
- Each finished step is approved by the Accountable member of its kind of work (`approver`). Nobody approves their
  own work: when the doer is the approver, the Code reviewer (Seraph) approves code and configuration and the Tech
  lead (Morpheus) everything else; when that one did it too, Morpheus, then the developer.
- An approval is an explicit verdict on named steps that finished before it: `dev progress step --check "✓ …"
  --approves "<step>"` (`record`), the Check of a review step on the steps it comes after (`from_review`), or the
  developer's decision, verified by C5 evidence, that names the steps (`dev crew decision --approves`). An earlier
  step of the approver approves nothing; a step finished again after its approval needs a new one; a later
  "changes" verdict takes it back.
- Morpheus approves by ending the run, so the steps he approves are never owed.
- Only the Responsible member takes a kind of work (`takes`); its Accountable or a Consulted member needs --why.
- The run ends only when no approval is owed and no step is flagged recheck, or with --why (`end_check`).
- The reconciler (C7): `reconcile(run, facts, now)` is pure and lists transitions; `apply` makes them, each a
  compare-and-set on the current state, so two hooks that reconcile at once make each change once. `facts` and
  `sweep` read the files and processes, outside any lock.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

MAIN, REVIEWER, DEVELOPER = "morpheus", "seraph", "developer"

# The kinds of work whose result is code or configuration: when their own approver did the work, the Code reviewer
# approves it. Every other kind goes to the Tech lead. A kind of work may name its own `A_fallback` in crew.json.
CODE_AND_CONFIG = frozenset({
    "frontend", "backend", "database", "feature", "debug", "mobile", "ai", "tooling", "testing", "security", "cicd",
    "release", "infrastructure", "migration", "i18n", "dependencies", "accessibility", "sso", "design-system",
    "backup"})
OTHER_WORK = frozenset({
    "plan", "scope", "requirements", "research", "architecture", "ux", "redteam", "review", "compliance", "incident",
    "performance", "data-analysis", "docs", "lookup", "codebase"})

VERDICTS = ("approved", "changes")
EVIDENCE_CHANNELS = ("E1", "E2")  # C5: the question tool's answer, a grant line in the developer's prompt
LIVE = ("running", "blocked")
LIMITS = {"finish_grace": 120,      # seconds a finished agent's SubagentStop may lag its meta file
          "stale_minutes": 90,      # a working lane silent this long is lost (progress.stale_agent_minutes)
          "launch_ttl": 15 * 60}    # a launch whose agent never started fails (progress.LAUNCH_TTL)
AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")


class ApprovalError(ValueError):
    """What is wrong, and what to do instead."""


# --------------------------------------------------------------------------- who does and who approves

def takes(member_id: str, act) -> bool:
    """May this member take this kind of work without --why? Only its Responsible members may."""
    return bool(act) and member_id in (act.get("R") or [])


def approver(act, doer):
    """Who approves a step of this kind of work that `doer` (a member id, or None) did: its Accountable member,
    unless that is the doer; then `A_fallback` or the Code reviewer for code and configuration, the Tech lead for
    the rest; then the Tech lead; then the developer. Never the doer. None without a kind of work."""
    if not act:
        return None
    second = act.get("A_fallback") or (REVIEWER if act.get("id") in CODE_AND_CONFIG else MAIN)
    return next((a for a in (act.get("A"), second, MAIN, DEVELOPER) if a and a != doer), DEVELOPER)


def doer(run: dict, s: dict, roster=None):
    """The member who did a step: Morpheus for the main session's own steps, else its lane's member; None when the
    lane names no member of the roster."""
    owner = s.get("owner") or "main"
    if owner == "main":
        return MAIN
    from . import crew
    lane = (run.get("agents") or {}).get(owner) or {}
    m = crew._for_label(lane.get("persona") or lane.get("label") or s.get("label") or "", roster)
    return m["id"] if m else None


def _who(aid, roster=None) -> str:
    from . import raci
    return "the developer" if aid == DEVELOPER else raci._name(aid, roster)


def _quoted(names) -> str:
    return ", ".join(f"“{n}”" for n in names)


# --------------------------------------------------------------------------- how a member's step ends

def glyph(check) -> str:
    """A Check's verdict glyph: ✓, ✗, ⚠ or – ("" when there is no Check). A Check without one reads as ✓, as the
    status block shows it."""
    t = str(check or "").strip()
    if not t:
        return ""
    if t[0] in "✓✗⚠–":
        return t[0]
    return "–" if t == "-" or t.startswith("- ") else "✓"


def ending(lane: dict, report: str = "") -> tuple:
    """How a member's step ends at its SubagentStop -> (state, why, check): "done" with the evidence's Check, or
    "unverified" with the reason and "". `report`: the member's last message, whose status block's Check decides
    first; else the last `dev progress step --check` of its lane."""
    from . import canary
    said = (canary.parse_status(report or "") or {}).get("check") or ""
    g = glyph(said)
    if g in ("✗", "⚠"):
        return "unverified", f"its report's Check is {said[:120]}", ""
    if g == "✓":
        return "done", f"its report's Check: {said[:120]}", said
    checks = [str(u.get("check") or "").strip() for u in lane.get("updates") or [] if u.get("kind") == "step"]
    checks = [c for c in checks if c]
    if checks and glyph(checks[-1]) == "✓":
        return "done", f"its last dev progress step --check: {checks[-1][:120]}", checks[-1]
    if checks:
        return "unverified", f"its last dev progress step --check is {checks[-1][:120]}", ""
    return "unverified", "no evidence: no Check ✓ in its report and no dev progress step --check from its lane", ""


# --------------------------------------------------------------------------- approvals

def verdict_of(check) -> str:
    """An approval's verdict from its Check: ✓ approves, ✗ or ⚠ asks for changes. A Check that starts with none of
    them (no Check, "–", or words alone such as "tests not run") is no verdict: an approval needs the mark itself."""
    g = str(check or "").strip()[:1]
    if g == "✓":
        return "approved"
    if g in ("✗", "⚠"):
        return "changes"
    raise ApprovalError("an approval needs a Check that starts with its mark: --check \"✓ <what you checked>\" "
                        "approves, --check \"✗ <what to change>\" asks for changes")


def split_steps(text) -> list:
    """"api, screens; cli" -> ["api", "screens", "cli"]."""
    items = text if isinstance(text, (list, tuple)) else re.split(r"[,;]", str(text or ""))
    return [str(x).strip() for x in items if str(x or "").strip()]


def _steps(run: dict, names) -> list:
    """The plan steps named (exact name, any case, or 1-based number). ApprovalError for an unknown one."""
    plan = run.get("plan") or []
    out, unknown = [], []
    for n in split_steps(names):
        s = plan[int(n) - 1] if n.isdigit() and 1 <= int(n) <= len(plan) else \
            next((x for x in plan if str(x.get("name") or "").lower() == n.lower()), None)
        if s is None:
            unknown.append(n)
        elif all(s is not x for x in out):
            out.append(s)
    if unknown:
        raise ApprovalError(f"no step {_quoted(unknown)} in the plan: " +
                            "; ".join(f"{i}. {x.get('name')}" for i, x in enumerate(plan, 1)))
    if not out:
        raise ApprovalError("name the steps it approves: --approves \"<step>[, <step>]\"")
    return out


def decision_steps(run: dict, text) -> list:
    """The step names a decision approves (`dev crew decision --approves "…"`), checked against the plan."""
    return [s["name"] for s in _steps(run, text)]


def _yes(answer) -> bool:
    return str(answer or "").strip().lower().startswith(("approve", "yes"))


def _answer_words(d: dict) -> str:
    """A decision's answer without its option letter: "A" or "A: approve" -> "approve" (options carry letters)."""
    from .progress import option_of
    text = str(option_of(d, d.get("answer")) or d.get("answer") or "")
    head, sep, rest = text.partition(":")
    return rest.strip() if sep and len(head.strip()) == 1 and head.strip().isalpha() else text


def verdicts(run: dict) -> list:
    """Every verdict on the run's steps, oldest first: its approvals, and the developer's decided decisions that
    name steps and are verified by C5 evidence (a "developer" typed without evidence is not one)."""
    out = [a for a in run.get("approvals") or [] if isinstance(a, dict)]
    for d in run.get("decisions") or []:
        if d.get("approves") and d.get("state") == "decided" and d.get("by") == DEVELOPER and \
                d.get("verified") is True:
            out.append({"ts": d.get("decided") or d.get("ts") or 0, "by": DEVELOPER, "steps": list(d["approves"]),
                        "verdict": "approved" if _yes(_answer_words(d)) else "changes", "via": "decision",
                        "decision": d.get("id")})
    return sorted(out, key=lambda a: a.get("ts") or 0)


def _approved(entries: list, s: dict, who: str) -> bool:
    """Is `who`'s last verdict on this step, given after it finished, an approval? A step with no finish time is
    never approved: no verdict can be shown to come after its work."""
    if not s.get("finished"):
        return False
    last = None
    for a in entries:
        if a.get("by") == who and s.get("name") in (a.get("steps") or []) and (a.get("ts") or 0) >= s["finished"]:
            last = a.get("verdict")
    return last == "approved"


def owed(run: dict, doer_of=None, roster=None) -> list:
    """[(approver id, [step names])] still to approve the run's finished steps, in plan order. `doer_of(step)`: who
    did it (default `doer`). Morpheus is never listed: he approves by ending the run."""
    from . import raci
    doer_of = doer_of or (lambda s: doer(run, s, roster))
    entries = verdicts(run)
    out = {}
    for s in run.get("plan") or []:
        if s.get("state") != "done":
            continue
        who = approver(raci.step_activity(s, roster), doer_of(s))
        if who and who != MAIN and not _approved(entries, s, who):
            out.setdefault(who, []).append(s.get("name") or "?")
    return sorted(out.items())


def _developer_evidence(run: dict, e, targets) -> dict:
    """The C5 entry behind a direct developer approval (progress.take_evidence), checked: the developer's own
    answer, after the work it approves, and backing no other approval."""
    e = e if isinstance(e, dict) else {}
    if e.get("channel") not in EVIDENCE_CHANNELS or not e.get("transcript_uuid"):
        raise ApprovalError("the developer approves only with their own answer (C5 evidence: the question tool, or a "
                            "line in their prompt): dev crew decision \"Approve …?\" --options \"approve|changes\" "
                            "--approves \"<step>\", then record their answer with dev crew decide")
    if (e.get("ts") or 0) < max((s.get("finished") or 0) for s in targets):
        raise ApprovalError("the developer answered before the work finished, so that answer approves nothing: ask "
                            "again now")
    uid = e["transcript_uuid"]
    if any((a.get("evidence") or {}).get("transcript_uuid") == uid for a in run.get("approvals") or []
           if isinstance(a, dict)):
        raise ApprovalError("that answer of the developer already backs another approval: each answer counts once")
    return {k: e.get(k) for k in ("channel", "ts", "covers", "transcript_uuid")}


def record(run: dict, by: str, steps, verdict: str, now: float, via: str = "step", check: str = "", evidence=None,
           doer_of=None, roster=None) -> dict:
    """Record `by`'s verdict ("approved" or "changes") on the named steps, in the run -> the entry. Refused, whole,
    when a step is not done, has no kind of work (so no approver), when `by` did it (self-approval) or is not its
    approver, and for the developer without fresh C5 evidence (`evidence`, from progress.take_evidence)."""
    from . import raci
    if verdict not in VERDICTS:
        raise ApprovalError(f"the verdict is one of {', '.join(VERDICTS)}")
    doer_of = doer_of or (lambda s: doer(run, s, roster))
    targets = _steps(run, steps)
    problems = []
    for s in targets:
        if s.get("state") != "done":
            problems.append(f"“{s['name']}” is {s.get('state') or 'pending'}, not done: Morpheus closes it with "
                            f"evidence first (dev progress step --step \"{s['name']}\" --check \"…\")")
            continue
        if not s.get("finished"):
            problems.append(f"“{s['name']}” has no finish time, so no approval can be shown to come after its "
                            f"work: dev progress reopen \"{s['name']}\" --why \"…\", then close it with evidence")
            continue
        did = doer_of(s)
        should = approver(raci.step_activity(s, roster), did)
        if should is None:  # no kind of work: devkit cannot tell who approves it, so nobody may (Seraph, batch 4)
            problems.append(f"“{s['name']}” has no kind of work, so devkit cannot tell who approves it: declare one "
                            f"(dev progress assign \"{s['name']}\" --to <member> --activity <id>), then approve it")
        elif did == by:
            problems.append(f"self-approval: {_who(by, roster)} did “{s['name']}”, and {_who(should, roster)} "
                            f"approves it")
        elif should and should != by:
            problems.append(f"{_who(should, roster)} approves “{s['name']}”, not {_who(by, roster)}")
    if problems:
        raise ApprovalError("; ".join(problems))
    # the exact time: rounded, an approval moments after the finish could fall before it
    entry = {"ts": now, "by": by, "steps": [s["name"] for s in targets], "verdict": verdict, "via": via}
    if check:
        entry["check"] = str(check)[:300]
    if by == DEVELOPER:
        entry["evidence"] = _developer_evidence(run, evidence, targets)
    run["approvals"] = [a for a in run.get("approvals") or []][-399:] + [entry]
    return entry


def is_review(s: dict, roster=None) -> bool:
    """The one rule for a review step (spec F6; the Code reviewer's batch 5): its kind of work is review, or the
    member it is assigned to works review-only (crew.json `tools`). Such a step starts on the unverified work it
    follows (progress.deps_open), and its Check is its member's verdict on that work (from_review,
    progress._verified_by_review). A RACI or roster that cannot be read makes no step a review step."""
    from . import crew, raci
    try:
        act = raci.step_activity(s, roster)
        if act and act.get("id") == "review":
            return True
        m = crew._for_label(s.get("label") or "", roster) if s.get("label") else None
    except Exception:
        return False
    return bool(m and m.get("tools") == "review")


def from_review(run: dict, s: dict, check: str, now: float, doer_of=None, roster=None):
    """A review step (is_review) ended with this Check: its doer's verdict on the steps it comes after that they
    approve -> the entry, or None when it is not a review, has no verdict, or follows none of theirs."""
    from . import raci
    if not is_review(s, roster):
        return None
    doer_of = doer_of or (lambda x: doer(run, x, roster))
    who = doer_of(s)
    try:
        verdict = verdict_of(check)
    except ApprovalError:
        return None
    by_name = {x.get("name"): x for x in run.get("plan") or []}
    mine = [x["name"] for x in (by_name.get(n) for n in s.get("after") or [])
            if x and x.get("state") == "done" and doer_of(x) != who and
            approver(raci.step_activity(x, roster), doer_of(x)) == who]
    if not who or not mine:
        return None
    return record(run, who, mine, verdict, now, via="review", check=check, doer_of=doer_of, roster=roster)


def recheck_open(run: dict) -> list:
    """[(step name, [reopened steps it used])]: steps flagged recheck (progress._recheck_dependents), not skipped."""
    return [(s["name"], list(s["recheck"])) for s in run.get("plan") or []
            if s.get("recheck") and s.get("state") != "skipped"]


def message(owed_, rechecks=(), roster=None) -> str:
    """Why the run cannot end yet, and how to settle each part."""
    from . import raci
    parts = []
    if owed_:
        parts.append("the work is not approved yet (the crew's RACI): " +
                     "; ".join(f"{_who(a, roster)} approves {_quoted(steps)}" for a, steps in owed_) + ".")
        members = [(a, steps) for a, steps in owed_ if a != DEVELOPER]
        if members:
            a, steps = members[0]
            parts.append(f"A member approves with a review step after the work (dev progress add \"review=20m after "
                         f"{steps[0]}\", then dev progress assign review --to {raci.handle(a, roster)}), or with dev "
                         f"progress step \"…\" --check \"✓ …\" --approves \"<step>\".")
        mine = dict(owed_).get(DEVELOPER)
        if mine:
            parts.append(f"The developer approves through a decision: dev crew decision \"Approve "
                         f"{_quoted(mine)}?\" --options \"approve|changes\" --approves \"{', '.join(mine)}\", then dev "
                         f"crew decide with their answer.")
    if rechecks:
        parts.append("To recheck: " + "; ".join(f"“{n}” used {_quoted(d)}, reopened since" for n, d in rechecks) +
                     f". Reopen and redo it, or confirm it still holds: dev progress confirm \"{rechecks[0][0]}\" "
                     f"--why \"…\".")
    parts.append("Or end with --why \"<reason>\".")
    return " ".join(parts)


def end_check(run: dict, why: str = "", now: float | None = None, doer_of=None, roster=None) -> dict:
    """Before `dev progress done`: ApprovalError naming each approval owed and each step to recheck; with `why`, the
    override to record (run["raci_overrides"]). {} when nothing stands in the way."""
    o, rc = owed(run, doer_of, roster), recheck_open(run)
    if not (o or rc):
        return {}
    if not str(why or "").strip():
        raise ApprovalError(message(o, rc, roster))
    return {"ts": round(now or time.time()), "step": "(end of run)", "why": why, "owed": [[a, s] for a, s in o],
            "recheck": [[n, d] for n, d in rc]}


# --------------------------------------------------------------------------- the reconciler (C7)

def limits() -> dict:
    """LIMITS, with `progress.stale_agent_minutes` from devkit's configuration."""
    from . import core
    out = dict(LIMITS)
    try:
        out["stale_minutes"] = float((core.CFG.get("progress") or {}).get("stale_agent_minutes") or
                                     LIMITS["stale_minutes"])
    except (TypeError, ValueError):
        pass
    return out


def _iso(ts) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts or 0))


def _last_activity(lane: dict, f: dict) -> float:
    seen = [lane.get("started") or 0, lane.get("resumed_at") or 0, f.get("last_seen") or f.get("transcript_mtime") or 0,
            f.get("active") or 0]  # active: its last activity anywhere (progress.agent_facts), when the sweep read it
    return max(seen + [u.get("ts") or 0 for u in lane.get("updates") or []])


def _lane_move(aid, frm, to, why, at=None) -> dict:
    return {"kind": "lane", "id": aid, "from": frm, "to": to, "why": why, "at": at}


def _step_move(s, to, owner, why) -> dict:
    return {"kind": "step", "id": s["name"], "from": s.get("state"), "to": to, "owner_from": s.get("owner") or "main",
            "owner": owner, "why": why}


def stop_moves(run: dict, aid: str, why: str, at=None) -> list:
    """A lane that stopped before its report (TaskStop, an interruption): the lane ends stopped, which frees its slot,
    and each open step it owns (running, or blocked on a decision) waits for a new launch, back with Morpheus, unless
    a live lane of the same member holds it (progress.live_holder). [] when the lane is not working or lost."""
    from .progress import live_holder
    lane = (run.get("agents") or {}).get(aid) or {}
    if lane.get("state") not in ("running", "lost"):
        return []
    moves = [_lane_move(aid, lane["state"], "stopped", why, at=at)]
    for s in run.get("plan") or []:
        if s.get("owner") == aid and s.get("state") in ("running", "blocked"):
            holder = live_holder(run, aid, s)
            moves.append(_step_move(s, "running", holder, f"{why}; a live lane of its member holds it") if holder
                         else _step_move(s, "pending", "main", f"{why}; the step waits for a new launch"))
    return moves


def reconcile(run: dict, facts: dict, now: float, lim: dict | None = None) -> list:
    """The transitions the facts call for (C7), without changing the run. Facts: per lane `meta_finished` (when its
    meta file said finished), `meta_running`, `stopped_at` (when its own transcript ended with its interruption),
    `ended_how` (what saw the end, when it was not the meta file beside the session's own transcript), `last_seen`
    (its last hook), `transcript_mtime` and `active` (its last activity anywhere); the session's `ended` and `alive`;
    the session's `launches`.
    Rules: a working lane whose meta finished `finish_grace` ago with no SubagentStop is unverified, its steps too
    (a step that started after that finish, which nobody worked on, waits for a new launch instead), and frees its
    slot; one whose transcript ends with its interruption stopped (stop_moves); one silent past `stale_minutes`, or
    whose session ended while its meta does not say it runs, is lost, and its steps wait for an owner; a lost lane
    seen again works again and takes back the steps it lost; the run of an ended session is orphaned; a launch
    pending past `launch_ttl` fails."""
    lim = {**LIMITS, **(lim or {})}
    facts = facts or {}
    fl, sess = facts.get("lanes") or {}, facts.get("session") or {}
    from .progress import _label_matches
    ended = sess.get("ended") is not None or sess.get("alive") is False
    plan = run.get("plan") or []
    moves = []
    for aid, lane in sorted((run.get("agents") or {}).items()):
        f, st = fl.get(aid) or {}, lane.get("state")
        mine = [s for s in plan if s.get("owner") == aid and s.get("state") == "running"]
        lost = [s for s in plan if s.get("lost_from") == aid and s.get("state") == "pending"  # and not reassigned
                and (s.get("owner") or "main") == "main" and (not s.get("label") or _label_matches(s["label"], lane))]
        if lane.get("over_limit"):  # a Codex launch over the crew's limits gave its step back: it never takes it again
            lost = []
        if st == "running":
            mf = f.get("meta_finished")
            silent = now - _last_activity(lane, f)
            if mf is not None and now - mf >= lim["finish_grace"]:
                how = f" ({f['ended_how']})" if f.get("ended_how") else ""
                why = f"finished at {_iso(mf)} with no SubagentStop{how}: no report, so no evidence"
                moves.append(_lane_move(aid, st, "unverified", why, at=mf))
                late = f"it started after its member finished at {_iso(mf)}: nobody worked on it, so it waits for a " \
                       f"new launch"  # delegated or assigned into a lane whose member had already ended
                moves += [_step_move(s, "pending", "main", late) if (s.get("started") or 0) > mf
                          else _step_move(s, "unverified", aid, why) for s in mine]
            elif f.get("stopped_at") is not None:
                how = f.get("ended_how") or "its transcript ends with its interruption"
                moves += stop_moves(run, aid, f"interrupted at {_iso(f['stopped_at'])} ({how}), before its report",
                                    at=f["stopped_at"])
            elif silent > lim["stale_minutes"] * 60 or (ended and not f.get("meta_running")):
                why = (f"silent for {silent / 60:.0f} minutes" if silent > lim["stale_minutes"] * 60 else
                       "its session ended") + "; the step waits for a new owner"
                moves.append(_lane_move(aid, st, "lost", why, at=_last_activity(lane, f)))
                moves += [_step_move(s, "pending", "main", why) for s in mine]
            else:
                moves += [_step_move(s, "running", aid, "its member works again") for s in lost]
        elif st == "lost" and (f.get("last_seen") or 0) > (lane.get("lost_at") or 0):
            moves.append(_lane_move(aid, st, "running", f"recovered: seen at {_iso(f['last_seen'])}",
                                    at=f["last_seen"]))
            moves += [_step_move(s, "running", aid, "its member works again") for s in lost]
    if ended and run.get("state") in LIVE:
        moves.append({"kind": "run", "id": run.get("goal") or "run", "from": run["state"], "to": "orphaned",
                      "why": sess.get("why") or "its session ended"})
    for tid, rec in sorted((facts.get("launches") or {}).items()):
        if rec.get("state") == "pending" and now - (rec.get("ts") or now) > lim["launch_ttl"]:
            moves.append({"kind": "launch", "id": tid, "from": "pending", "to": "failed",
                          "why": f"its agent did not start within {lim['launch_ttl'] // 60:.0f} minutes"})
    return moves


def _interval(run: dict, aid: str, at: float, open_: bool):
    ivs = run.setdefault("intervals", [])
    if open_:
        if not any(iv[1] is None and iv[2] == "agent" and iv[3] == aid for iv in ivs):
            ivs.append([round(at, 1), None, "agent", aid])
        return
    for iv in ivs:
        if iv[1] is None and iv[2] == "agent" and iv[3] == aid:
            iv[1] = round(max(at, iv[0]), 1)


def apply(run: dict, moves: list, now: float) -> list:
    """Make the lane, step and run transitions on this (freshly read) run, each only while its `from` still holds
    -> the transitions made. Launch transitions belong to the session record (`apply_session`)."""
    agents, made = run.get("agents") or {}, []
    steps = {s.get("name"): s for s in run.get("plan") or []}
    for m in moves:
        if m["kind"] == "lane":
            lane = agents.get(m["id"])
            if not lane or lane.get("state") != m["from"]:
                continue
            lane["state"] = m["to"]
            if m["to"] == "running":  # recovered: its silence counts from when it was seen again
                lane.update(finished=None, recovered=round(now, 1), resumed_at=round(m.get("at") or now, 1))
                _interval(run, m["id"], now, True)
            else:
                lane["finished"] = round(m.get("at") or now, 1)
                _interval(run, m["id"], lane["finished"], False)
                if m["to"] == "lost":
                    lane["lost_at"] = now  # exact: rounded up, a hook moments later would not recover it
            run.setdefault("history", []).append({
                "ts": round(now), "step": f"(lane {lane.get('persona') or lane.get('label') or m['id']})",
                "from": m["from"], "to": m["to"], "by": "devkit", "why": m["why"][:300]})
            run["history"] = run["history"][-200:]
        elif m["kind"] == "step":
            s = steps.get(m["id"])
            if not s or s.get("state") != m["from"] or (s.get("owner") or "main") != m["owner_from"]:
                continue
            s.update(state=m["to"], owner=m["owner"])
            if m["to"] == "pending" and m["owner"] == "main":
                s["lost_from"] = m["owner_from"]
                s.pop("waiting", None)  # back with Morpheus: no decision blocks it any more
            else:
                s.pop("lost_from", None)
            run.setdefault("history", []).append({"ts": round(now), "step": s["name"], "from": m["from"],
                                                  "to": m["to"], "by": "devkit", "why": m["why"][:300]})
            run["history"] = run["history"][-200:]
        elif m["kind"] == "run":
            if run.get("state") != m["from"]:
                continue
            run.update(state=m["to"], orphaned={"ts": round(now), "why": m["why"]})
        else:
            continue
        made.append(m)
    return made


def apply_session(sess: dict, moves: list, now: float) -> list:
    """Make the launch transitions on this (freshly read) session record -> the transitions made."""
    made = []
    for m in moves:
        rec = (sess.get("launches") or {}).get(m["id"]) if m["kind"] == "launch" else None
        if rec and rec.get("state") == m["from"]:
            rec.update(state=m["to"], failed=round(now, 1), why=m["why"])
            made.append(m)
    return made


def audit_moves(run: dict, moves: list):
    """One audit record per transition made (crew.<state>, plan.<state>, run.<state>, crew.launch-<state>), and for
    each lane that stopped, was lost or ended unverified a notice that wakes the run's main session
    (watch.lane_notices): the hooks and the sweep reach here after the run's lock is released."""
    try:
        from . import watch
        watch.lane_notices(run, moves)
    except Exception:
        pass  # a notice never fails a transition
    try:
        from . import audit
    except Exception:
        return
    prefix = {"lane": "crew.", "step": "plan.", "run": "run.", "launch": "crew.launch-"}
    for m in moves:
        audit.record(prefix.get(m["kind"], "crew.") + m["to"], str(m["id"])[:200], m.get("why") or "", m["to"],
                     project=run.get("project") or "", by="devkit", was=m.get("from") or "")


INTERRUPTED = "[Request interrupted by user"  # Claude Code's record when an agent is stopped or interrupted
TAIL_BYTES = 4096  # an interruption record is under 1 KB; a longer last line is something else


def interrupted_at(path):
    """When the agent whose transcript this is was stopped: the time of its last record when that record is Claude
    Code's interruption (TaskStop, Esc), and None when the agent wrote anything after it (it works again), its last
    record is another one, or the file cannot be read."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - TAIL_BYTES))
            tail = fh.read().decode("utf-8", "replace")
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    lines = [ln for ln in tail.split("\n") if ln.strip()]
    if not lines or (size > TAIL_BYTES and len(lines) < 2):
        return None  # the window starts inside the last record: too long to be an interruption
    try:
        rec = json.loads(lines[-1])
    except ValueError:
        return None
    msg = rec.get("message") if isinstance(rec, dict) else None
    if not isinstance(msg, dict) or rec.get("type") != "user":
        return None
    content = msg.get("content")
    texts = [content] if isinstance(content, str) else \
        [c.get("text") for c in content or [] if isinstance(c, dict) and c.get("type") == "text"]
    if len(texts) != 1 or not str(texts[0] or "").startswith(INTERRUPTED):
        return None
    try:
        import datetime as dt
        return dt.datetime.fromisoformat(str(rec["timestamp"]).replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        return mtime


def meta_reader(transcript_path):
    """`meta(aid)` for `facts`, or None without a main transcript: Claude Code's
    `<main transcript>/subagents/agent-<aid>.meta.json` (its `finished`, and the file's modification time as when),
    the modification time of the agent's own transcript, and when that transcript says it was stopped
    (interrupted_at). {} when the agent has no meta file."""
    t = str(transcript_path or "")
    if not t.endswith(".jsonl"):
        return None
    folder = Path(t[:-len(".jsonl")]) / "subagents"

    def read(aid: str) -> dict:
        if not AGENT_ID.fullmatch(str(aid or "")):
            return {}
        p = folder / f"agent-{aid}.meta.json"
        try:
            mtime, doc = p.stat().st_mtime, json.loads(p.read_text())
        except (OSError, ValueError):
            return {}
        log = folder / f"agent-{aid}.jsonl"
        try:
            tmt = log.stat().st_mtime
        except OSError:
            tmt = None
        finished = isinstance(doc, dict) and doc.get("finished") is True
        return {"finished": finished, "mtime": mtime, "transcript_mtime": tmt,
                "stopped": None if finished or tmt is None else interrupted_at(log)}
    return read


def facts(run: dict, sess, now: float | None = None, meta=None, alive=None, seen=None) -> dict:
    """What the reconciler needs, read outside any lock. `sess`: the session record, None when it is gone (SessionEnd
    deletes it). `meta(aid)`: a lane's meta file (`meta_reader`). `alive(pid, pid_start)`: whether the session's
    process still runs (`pid_alive`); None leaves it unknown, as in the session's own hooks. `seen(aid)`:
    progress.agent_facts, what devkit and Claude Code saw of the agent anywhere on this Mac (its lane in every run,
    its records beside every session transcript devkit knows): a running lane whose member ended there, after its
    last activity, ends as if its own meta file said so (`ended_how` says what saw it), and its last activity
    anywhere (`active`) counts for its silence. After a compaction gives the conversation a new session id, Claude
    Code writes the agent's records beside the new transcript, where the meta file beside the run's own never is."""
    now = now or time.time()
    subs = (sess or {}).get("subagents") or {}
    lanes = {}
    for aid, lane in (run.get("agents") or {}).items():
        if lane.get("state") not in ("running", "lost"):
            continue
        m = (meta(aid) if meta else None) or {}
        lanes[aid] = {"meta_finished": (m.get("mtime") or now) if m.get("finished") else None,
                      "meta_running": bool(m) and not m.get("finished") and m.get("stopped") is None,
                      "stopped_at": m.get("stopped"),
                      "last_seen": lane.get("last_seen") or (subs.get(aid) or {}).get("last_seen"),
                      "transcript_mtime": m.get("transcript_mtime")}
        if seen is None:
            continue
        from .progress import lane_ended
        g = seen(aid)
        lanes[aid]["active"] = g.get("active") or None
        end = lane_ended(run, lane, g) if lane.get("state") == "running" else None
        if end and lanes[aid]["meta_finished"] is None and lanes[aid]["stopped_at"] is None:
            interrupted = "interruption" in end["how"]
            lanes[aid].update(meta_running=False, ended_how=end["how"],
                              **({"stopped_at": end["at"]} if interrupted else {"meta_finished": end["at"]}))
    if sess is None:
        session = {"ended": now, "alive": None, "why": "its session record is gone (the session ended)"}
    elif sess.get("ended"):
        e = sess["ended"]
        session = {"ended": (e.get("ts") if isinstance(e, dict) else e) or now, "alive": None,
                   "why": f"its session ended ({e.get('reason')})" if isinstance(e, dict) and e.get("reason")
                   else "its session ended"}
    else:
        pid = sess.get("pid")
        up = alive(pid, sess.get("pid_start")) if (pid and alive) else None
        session = {"ended": None, "alive": up, "why": "its session's process is gone" if up is False else ""}
    launches = {tid: {"ts": r.get("ts"), "state": r.get("state")}
                for tid, r in ((sess or {}).get("launches") or {}).items() if isinstance(r, dict)}
    return {"lanes": lanes, "session": session, "launches": launches}


def pid_start(pid) -> str:
    """When a process started, as `ps` says it ("Fri Nov  6 10:26:14 2026", spaces folded); "" when it cannot tell.
    Recorded with the pid at SessionStart (C8, `pid_start`), so a reused pid is never taken for the session."""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True, timeout=2,
                             env={**os.environ, "LC_ALL": "C"}).stdout
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return ""
    return " ".join(out.split())


def pid_alive(pid, started=None):
    """Does this process still run, and is it the one that started at `started` (pid_start)? None when devkit
    cannot tell."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return None
    if not started:
        return True
    now_start = pid_start(pid)
    return (now_start == " ".join(str(started).split())) if now_start else None


def sweep(runs_dir=None, sessions_dir=None, now: float | None = None, alive=pid_alive) -> dict:
    """`dev sync --scheduled`: reconcile every live run on this Mac (C7), each on its freshly read record ->
    {run file name: [transitions made]}. The facts are read before the run's lock is taken, with what devkit and
    Claude Code saw of each agent anywhere on this Mac (facts, `seen`), so a lane whose member ended under the
    conversation's newer session id closes too, with the reason in the run's history."""
    from . import progress, state
    now = now or time.time()
    runs_dir = Path(runs_dir or progress.RUNS)
    sessions_dir = Path(sessions_dir or progress.SESSIONS)
    lim, out = limits(), {}
    stops = sessions_dir == Path(progress.SESSIONS)  # pending stops live beside devkit's own session records
    files = sorted(runs_dir.glob("*.json")) if runs_dir.is_dir() else []
    # what devkit and Claude Code saw of each agent anywhere on this Mac: every run here, and Claude Code's records
    # beside every session transcript devkit knows (the run's own session may no longer be the conversation's)
    seen = progress.agent_facts([r for r in (state.read(f) for f in files) if isinstance(r, dict)],
                                progress._known_transcripts(sessions_dir))
    for f in files:
        run = state.read(f)
        if not isinstance(run, dict) or run.get("state") not in LIVE:
            continue
        if stops and run.get("agent") and run.get("session") and progress.apply_stops(run["agent"], run["session"]):
            run = state.read(f)  # a member's report that found the lock busy, before its lane is judged without it
            if not isinstance(run, dict) or run.get("state") not in LIVE:
                continue
        sess = state.read(sessions_dir / f.name)
        fx = facts(run, sess, now, meta=meta_reader((sess or {}).get("transcript")), alive=alive, seen=seen)
        if not reconcile(run, fx, now, lim):
            continue
        made = []

        def change(doc, fx=fx, made=made):
            made.extend(apply(doc, reconcile(doc, fx, now, lim), now))
            return doc if made else state.SKIP
        try:
            state.update(f, change, create=False, indent=1)
        except state.StateBusy:
            continue  # audited by state; the next sweep tries again
        if made:
            out[f.name] = made
            audit_moves(run, made)
    return out
