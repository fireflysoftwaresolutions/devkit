# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The crew's coordination rules, in one place, as pure functions over a run record: no files, no hooks, and no
clock but the `now` a caller passes. progress.py calls them inside its own transaction, and so do the hooks through
progress.py, so every path that gives, takes, blocks or closes a step reads the same definitions.

- A lane *holds* the steps it owns in state running, and only those: a blocked, unverified, done, skipped or pending
  step holds no lane. Its write scope is the union of the globs of the steps it holds (`scope`), never a stored copy.
- A lane is *busy* while it holds a step that is not a review; review steps share a lane.
- A review is *independent* of its author: no lane of a member that ran a step the review follows takes it
  (`independent`), so nobody reviews, and approves, their own work.
- An *intent* says who the tech lead gave a pending step to: a member (any of its lanes may take it) or one lane.
  It lives on the step, so a lane's end, stop or resume never loses it, and only a claim, a new assignment, the step
  leaving pending or the run's end removes it.
- A free lane *claims* the step its launch names first, then an answered blocked step put back at the front, then
  the steps bound to it, then its member's, each oldest first (`claimable`).
- `check` returns the invariants a run record breaks, by id (I-owner, I-one-step, I-scope, I-separate, I-intent,
  I-claim, I-history, I-independent), so a test can check every action.

The scope glob rules live here too (`segments`, `globs_overlap`), since the overlap check that keeps two working
lanes apart is part of the same contract."""
from __future__ import annotations

import fnmatch

OPEN = ("running",)  # the only state that holds a lane and gives it scope
DONE = ("done", "skipped")
UNCLAIMABLE_BINDING = ("conflict", "unbound", "pending")  # a lane not bound to a launch of its own takes no step


def _plain_review(s: dict) -> bool:
    return s.get("activity") == "review"


def _rv(review):
    return review or _plain_review


# --------------------------------------------------------------------------- lanes and members

def member_of(label: str) -> str:
    """The member a step's label names: "Tank" for "Tank" and for the launch description "Tank: the API"."""
    return (label or "").split(":", 1)[0].strip()


def label_matches(label: str, lane: dict) -> bool:
    """A step assigned to "Neo" belongs to the lane launched as "Neo: build the parser", or as devkit:neo."""
    lab = (label or "").strip().lower()
    if not lab or not isinstance(lane, dict):
        return False
    names = [(lane.get("label") or "").lower(), (lane.get("persona") or "").lower(), (lane.get("type") or "").lower()]
    return any(n and (n == lab or n.startswith(lab + ":") or n.startswith(lab + " ") or n.endswith(":" + lab)
                      or lab in n.split(":")[0]) for n in names)


def can_claim(lane: dict) -> bool:
    """May this lane take a step now? A member's own lane (not a helper's), running, bound to its launch, and not
    over the crew's limits or waiting for a slot."""
    return isinstance(lane, dict) and lane.get("state") == "running" and (lane.get("parent") or "main") == "main" \
        and lane.get("binding") not in UNCLAIMABLE_BINDING and not lane.get("over_limit") \
        and not lane.get("waits_slot")


def held(run: dict, aid: str) -> list:
    """The steps lane `aid` owns in state running, in plan order."""
    return [s for s in (run or {}).get("plan") or [] if s.get("owner") == aid and s.get("state") in OPEN]


def busy(run: dict, aid: str, step: dict | None = None, review=None) -> bool:
    """Does lane `aid` hold a step that keeps it from taking `step`? Any other step it holds, except that reviews
    share a lane: for a review `step`, the lane's other reviews do not count. Without `step`: whether it holds a
    step that is not a review."""
    rv = _rv(review)
    for x in held(run, aid):
        if x is step:
            continue
        if rv(x) and (step is None or rv(step)):
            continue
        return True
    return False


def independent(run: dict, step: dict, member: str) -> bool:
    """May `member` (a crew member's name, as a lane's persona holds it) review `step`? Not when a lane of that member
    ran a step `step` comes after: a review never goes to its author, whichever of the author's lanes asks."""
    me = (member or "").strip().lower()
    if not me:
        return True
    agents = (run or {}).get("agents") or {}
    steps = {x.get("name"): x for x in (run or {}).get("plan") or []}
    for name in step.get("after") or []:
        owner = (steps.get(name) or {}).get("owner") or "main"
        if owner != "main" and ((agents.get(owner) or {}).get("persona") or "").strip().lower() == me:
            return False
    return True


def scope(run: dict, aid: str) -> list:
    """The globs lane `aid` may write: the union of the scopes of the steps it holds, sorted. [] when it holds no
    step, or only steps without a scope; progress.lane_scope tells those two apart for the edit check."""
    return sorted({g for s in held(run, aid) for g in s.get("scope") or [] if isinstance(g, str) and g})


def deps_open(run: dict, s: dict, review=None) -> list:
    """The steps `s` waits for that are not done or skipped, in the order of its `after`. A review does not wait for
    an unverified step: its Check is the verdict on that work. A review that ended with changes requested
    (`verdict` "changes", its ✗) is finished work: the step after it, the fix, does not wait for it, while the work
    it reviewed stays unapproved."""
    steps = {x.get("name"): x for x in (run or {}).get("plan") or []}
    out = [a for a in s.get("after") or [] if a in steps and steps[a].get("state") not in DONE
           and not (steps[a].get("state") == "unverified" and steps[a].get("verdict") == "changes")]
    if any(steps[a].get("state") == "unverified" for a in out) and _rv(review)(s):
        out = [a for a in out if steps[a].get("state") != "unverified"]
    return out


# --------------------------------------------------------------------------- intents

def intent(step: dict) -> dict | None:
    """Who the tech lead gave pending step `step` to -> {"member", "lane", "rev", "ts", "by", "front"}, or None.
    From `step["intent"]`; else from a queue entry of a run written before intents (`waits_lane`: one lane, or the
    exact form, bind to that lane; several lanes become the member's); else, for a pending step assigned by role
    that waits for a launch, the member, from the time it was assigned."""
    if not isinstance(step, dict):
        return None
    it = step.get("intent")
    if isinstance(it, dict):
        return {"member": it.get("member") or member_of(step.get("label")), "lane": it.get("lane") or None,
                "rev": it.get("rev") or 0, "ts": it.get("ts") or 0, "by": it.get("by") or "main",
                "front": bool(it.get("front"))}
    w = step.get("waits_lane")
    member = member_of(step.get("label"))
    if isinstance(w, dict) and (w.get("lane") or w.get("lanes")):
        lanes = [str(x) for x in (w.get("lanes") or [w.get("lane")]) if x]
        return {"member": member, "lane": lanes[0] if len(lanes) == 1 and not w.get("lanes") else None,
                "rev": w.get("revision") or step.get("assignment_revision") or 0, "ts": w.get("ts") or 0,
                "by": "main", "front": False}
    if step.get("state") == "pending" and member and (step.get("owner") or "main") == "main":
        return {"member": member, "lane": None, "rev": step.get("assignment_revision") or 0,
                "ts": step.get("assigned_at") or step.get("added") or 0, "by": "main", "front": False}
    return None


def set_intent(run: dict, step: dict, member: str, lane: str | None, now: float, front: bool = False) -> dict:
    """Record that `step` goes to `member`, to its lane `lane` when one is named. It replaces any earlier intent and
    any legacy queue entry; `front` puts it ahead of the lane's other intents (an answered blocked step)."""
    step.pop("waits_lane", None)
    step["intent"] = {"member": member, "lane": lane or None, "rev": step.get("assignment_revision") or 0,
                      "ts": round(now, 3), "by": "main", "front": bool(front)}
    return step["intent"]


def drop_intent(step: dict):
    step.pop("intent", None)
    step.pop("waits_lane", None)


def queued_lanes(step: dict) -> list:
    """The lanes a pending step is bound to by its intent ([] for a member-wide one or none). A legacy entry for
    several lanes lists them all."""
    w = step.get("waits_lane") if "intent" not in step else None
    if isinstance(w, dict) and w.get("lanes"):
        return [str(x) for x in w["lanes"] if x]
    it = intent(step) if step.get("state", "pending") == "pending" else None
    return [it["lane"]] if it and it.get("lane") else []


def claimable(run: dict, aid: str, launch_named=None, member_wide: bool = True, review=None) -> dict | None:
    """The step lane `aid` takes now, or None: of the pending steps whose dependencies are done and whose intent
    names this lane, or its member with no lane (unless `member_wide` is False: a resume whose member has a new
    launch on its way leaves those to that launch; nor a lane launched as a follow-up of a finished step,
    `follow_up`), and that the lane is free for (busy), never a review of its member's own work (independent), the
    step its launch names
    (`launch_named`: a step or its name), then a front intent, then a lane-bound one, then a member-wide one, each
    oldest first, then in plan order."""
    lane = ((run or {}).get("agents") or {}).get(aid)
    if not can_claim(lane):
        return None
    agents = run.get("agents") or {}
    named = launch_named.get("name") if isinstance(launch_named, dict) else launch_named
    member_wide = member_wide and not lane.get("follow_up")  # a follow-up of finished work takes no member's step
    found = []
    for i, s in enumerate(run.get("plan") or []):
        if s.get("state") != "pending":
            continue
        it = intent(s)
        if not it:
            continue
        if it["lane"]:
            other = agents.get(it["lane"]) or {}
            # a step bound to another lane goes to this one only when its launch names it and that lane ended
            if it["lane"] != aid and not (named and s.get("name") == named and other.get("state") != "running"
                                          and label_matches(it["member"], lane)):
                continue
        elif not member_wide or not label_matches(it["member"], lane):
            continue
        if deps_open(run, s, review) or busy(run, aid, s, review):
            continue
        if _rv(review)(s) and not independent(run, s, lane.get("persona") or ""):
            continue  # the author's own work: another member reviews it
        rank = 0 if named and s.get("name") == named else 1 if it["front"] else 2 if it["lane"] else 3
        found.append((rank, it["ts"] or 0, i, s))
    return min(found, key=lambda t: t[:3])[3] if found else None


def claim(run: dict, aid: str, now: float, why: str, launch_named=None, member_wide: bool = True,
          review=None) -> dict | None:
    """Lane `aid` takes the step `claimable` chooses: pending -> running in that lane, its intent removed, with a
    history entry. -> the step, or None."""
    s = claimable(run, aid, launch_named, member_wide, review)
    if s is None:
        return None
    s.update(owner=aid, state="running", started=s.get("started") or now)
    drop_intent(s)
    _history(run, s, "pending", "running", "devkit", why, now)
    return s


# --------------------------------------------------------------------------- blocking

def block(run: dict, step: dict, decision_id: str, now: float, why: str = "", by: str = "main"):
    """A running step waits for a decision (or the tech lead's `dev progress block`): blocked, its owner kept (the
    lane the answer returns it to), and it no longer holds that lane or gives it scope."""
    old = step.get("state")
    step["state"] = "blocked"
    if decision_id:
        step["waiting"] = decision_id
    _history(run, step, old, "blocked", by, why or (f"waiting for {decision_id}" if decision_id else "blocked"), now)


def unblock(run: dict, step: dict, lane_live: bool, now: float, why: str = "", review=None) -> str:
    """The decision a blocked step waited for is answered or withdrawn. Its lane still works and is free: it runs
    there again. Its lane works on another step: it waits first in that lane's intents. Its lane is gone: it waits
    first in its member's intents, for that member's next lane (never back to the tech lead). The tech lead's own
    blocked step runs again. -> "running" | "pending"."""
    step.pop("waiting", None)
    owner = step.get("owner") or "main"
    why = why or "the decision it waited for is answered"
    if owner == "main" or (lane_live and not busy(run, owner, step, review)):
        step["state"] = "running"
        _history(run, step, "blocked", "running", "main", why, now)
        return "running"
    step.update(state="pending", owner="main", started=None)
    if lane_live:
        set_intent(run, step, member_of(step.get("label")), owner, now, front=True)
        _history(run, step, "blocked", "pending", "main", f"{why}; its lane works on another step: it joins that "
                 f"lane first once that step closes", now)
    else:
        set_intent(run, step, member_of(step.get("label")), None, now, front=True)
        _history(run, step, "blocked", "pending", "main", f"{why}; its lane ended: it goes first to its member's "
                 f"next lane", now)
    return "pending"


# --------------------------------------------------------------------------- outcomes and invariants

def states(run: dict) -> dict:
    """{step name: state}, taken before a change for `changes`."""
    return {s.get("name"): s.get("state") for s in (run or {}).get("plan") or []}


def changes(before: dict, run: dict) -> list:
    """The steps whose state differs from `before` -> [(name, from, to)] in plan order (from None for a new step)."""
    return [(s.get("name"), before.get(s.get("name")), s.get("state")) for s in (run or {}).get("plan") or []
            if before.get(s.get("name")) != s.get("state")]


HISTORY_MARKS: dict = {}  # {how, tid} of the call this process runs for, set once (progress.mark_calls)


def _history(run: dict, s: dict, old, new, by: str, why: str, now: float):
    run.setdefault("history", []).append({"ts": round(now), "step": s.get("name"), "from": old, "to": new,
                                          "by": by or "main", "why": (why or "")[:300], **HISTORY_MARKS})
    run["history"] = run["history"][-200:]


def check(run: dict, now: float, review=None) -> list:
    """The invariants run record `run` breaks -> ["I-owner: …", …], [] when it is sound."""
    out = []
    plan, agents = (run or {}).get("plan") or [], (run or {}).get("agents") or {}
    live = {k for k, v in agents.items() if isinstance(v, dict) and v.get("state") == "running"}
    rv = _rv(review)
    for s in plan:
        owner = s.get("owner") or "main"
        if s.get("state") in OPEN and owner != "main" and owner not in live:
            out.append(f"I-owner: “{s.get('name')}” runs in lane {owner}, which is "
                       f"{(agents.get(owner) or {}).get('state') or 'unknown'}")
        if s.get("state") != "pending" and "intent" in s:
            out.append(f"I-intent: {s.get('state')} “{s.get('name')}” still carries an intent")
    for aid in sorted(agents):
        lane = agents[aid]
        builds = [s.get("name") for s in held(run, aid) if not rv(s)]
        if len(builds) > 1:
            out.append(f"I-one-step: lane {aid} runs {', '.join(builds)}")
        for s in held(run, aid):
            if rv(s) and isinstance(lane, dict) and not independent(run, s, lane.get("persona") or ""):
                out.append(f"I-independent: lane {aid} reviews “{s.get('name')}”, which follows work its member did")
        if isinstance(lane, dict) and plan and ("handed_over" in lane or
                                                ("scope" in lane and sorted(lane["scope"] or []) != scope(run, aid))):
            out.append(f"I-scope: lane {aid} keeps a stored scope or hand-over that its running steps do not give")
        if aid in live:
            s = claimable(run, aid, review=review)
            if s is not None:
                out.append(f"I-claim: lane {aid} is free while “{s.get('name')}” waits for it")
    ids = sorted(live)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            ga, gb = _scoped(run, a), _scoped(run, b)
            clash = [(x, y) for x, ca in ga for y, cb in gb if (not ca or not cb or ca == cb)
                     and not x.startswith(("/", "~")) and globs_overlap(x, y)]
            if clash:
                out.append(f"I-separate: lanes {a} and {b} both hold {clash[0][0]} / {clash[0][1]}")
    count = sum(1 for s in plan if s.get("state") in DONE)
    if (run or {}).get("step", count) != count:
        out.append(f"I-history: the run counts {run.get('step')} closed steps, the plan has {count}")
    last = {}
    for h in (run or {}).get("history") or []:
        last[h.get("step")] = h
    for s in plan:
        h = last.get(s.get("name"))
        if h and h.get("to") != s.get("state") and h.get("to") != h.get("from"):
            out.append(f"I-history: “{s.get('name')}” is {s.get('state')}, its last history entry says {h.get('to')}")
    return out


def _scoped(run: dict, aid: str) -> list:
    return [(g, s.get("checkout") or "") for s in held(run, aid) for g in s.get("scope") or [] if isinstance(g, str)]


# --------------------------------------------------------------------------- scope globs
# A glob is matched segment by segment: `*`, `?` and `[…]` match inside one segment and never cross `/`, and `**`
# as a whole segment matches any number of segments, none included. A glob that names a folder holds everything
# under it: one that ends with `/` or `**`, one whose last segment has a wildcard (`src/*` matches the folder
# src/studio), and a literal name that is a folder. A literal name that is a file (`docs/crew.md`) holds only
# itself, so no other glob can reach a path under it.

def _wild(seg: str) -> bool:
    return any(c in seg for c in "*?[")


def segments(glob: str, folder: bool = True) -> list:
    """A glob as its segments, ending with the "**" that holds what lies under a folder it names; `folder` False for
    a glob whose literal last segment is a file."""
    segs = [s for s in str(glob or "").split("/") if s not in ("", ".")]
    if segs and segs[-1] == "**":
        return segs
    return segs + ["**"] if folder or str(glob or "").endswith("/") or (segs and _wild(segs[-1])) else segs


def names_folder(glob: str, kind=None) -> bool:
    """Can glob `glob` name a folder? Yes when it ends with `/` or `**`, or its last segment has a wildcard. A
    literal last segment is a folder when `kind(glob)` says "dir", a file when it says "file"; with no answer (no
    checkout at hand, or nothing there yet), a name with a suffix (`crew.md`) is a file, one without a folder."""
    g = str(glob or "")
    segs = [s for s in g.split("/") if s not in ("", ".")]
    if not segs or g.endswith("/") or segs[-1] == "**" or _wild(segs[-1]):
        return True
    k = kind(g.rstrip("/")) if kind else None
    if k in ("dir", "file"):
        return k == "dir"
    return "." not in segs[-1][1:]


def segments_match(segs: list, pat: list) -> bool:
    memo = {}

    def go(i, j):
        if (i, j) not in memo:
            if j == len(pat):
                memo[(i, j)] = i == len(segs)
            elif pat[j] == "**":
                memo[(i, j)] = go(i, j + 1) or (i < len(segs) and go(i + 1, j))
            else:
                memo[(i, j)] = i < len(segs) and fnmatch.fnmatchcase(segs[i], pat[j]) and go(i + 1, j + 1)
        return memo[(i, j)]
    return go(0, 0)


def _glob_tokens(seg: str) -> list:
    """One segment of a glob as fnmatch reads it: "*", "?", ("set", "[…]") or ("lit", <one character>)."""
    out, i, n = [], 0, len(seg)
    while i < n:
        c = seg[i]
        if c in "*?":
            if not (c == "*" and out and out[-1] == "*"):
                out.append(c)
            i += 1
            continue
        if c == "[":
            j = i + 1 + (seg[i + 1:i + 2] == "!")
            j += seg[j:j + 1] == "]"  # a ] first in the set is one of its characters
            end = seg.find("]", j)
            if end >= 0:
                out.append(("set", seg[i:end + 1]))
                i = end + 1
                continue
        out.append(("lit", c))  # an unclosed [ is a literal, as fnmatch reads it
        i += 1
    return out


def _one_char_both(x, y) -> bool:
    """Can one character match glob tokens x and y (neither "*")? Two sets count as sharing one: an overlap is the
    answer that keeps two lanes apart."""
    if "?" in (x, y):
        return True
    if x[0] == "lit" and y[0] == "lit":
        return x[1] == y[1]
    if x[0] == "lit" or y[0] == "lit":
        lit, other = (x, y) if x[0] == "lit" else (y, x)
        return fnmatch.fnmatchcase(lit[1], other[1])
    return True


def _segment_overlap(p: str, q: str) -> bool:
    """Can one path segment match both segment patterns p and q?"""
    a, b, memo = _glob_tokens(p), _glob_tokens(q), {}

    def go(i, j):
        if (i, j) not in memo:
            r = i == len(a) and j == len(b)
            if not r and i < len(a) and a[i] == "*":  # it matches nothing, or one more character of q's
                r = go(i + 1, j) or (j < len(b) and b[j] != "*" and go(i, j + 1))
            if not r and j < len(b) and b[j] == "*":
                r = go(i, j + 1) or (i < len(a) and a[i] != "*" and go(i + 1, j))
            if not r and i < len(a) and j < len(b) and "*" not in (a[i], b[j]):
                r = _one_char_both(a[i], b[j]) and go(i + 1, j + 1)
            memo[(i, j)] = r
        return memo[(i, j)]
    return go(0, 0)


def segments_overlap(a: list, b: list) -> bool:
    """Can one path match both glob segment lists?"""
    memo = {}

    def go(i, j):
        if (i, j) not in memo:
            r = i == len(a) and j == len(b)
            if not r and i < len(a) and a[i] == "**":  # it spans nothing, or one more segment of b's
                r = go(i + 1, j) or (j < len(b) and go(i, j + 1))
            if not r and j < len(b) and b[j] == "**":
                r = go(i, j + 1) or (i < len(a) and go(i + 1, j))
            if not r and i < len(a) and j < len(b) and "**" not in (a[i], b[j]):
                r = _segment_overlap(a[i], b[j]) and go(i + 1, j + 1)
            memo[(i, j)] = r
        return memo[(i, j)]
    return go(0, 0)


def globs_overlap(a: str, b: str, kind=None) -> bool:
    """Could two scope globs hold the same file? True when one path matches both, compared without case. `kind`:
    the checkout's answer for a literal name ("dir", "file" or None), so a file such as `docs/crew.md` is no folder
    another glob could reach into (names_folder)."""
    a, b = str(a or ""), str(b or "")
    if not a or not b:
        return False
    return segments_overlap(segments(a.casefold(), names_folder(a, kind)),
                            segments(b.casefold(), names_folder(b, kind)))
