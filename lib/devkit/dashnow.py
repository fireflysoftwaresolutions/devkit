# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The dashboard's Now data (slice 1 of the redesign; contract P-fyli4qqn0y7, screen P-fyka51gico5).

`areas()` turns what `dashboard.collect()` already read into five snapshot areas:
  attention  the Needs-you queue, ranked once here (the page never re-sorts), and the sentence above it
  runs       today's run fields plus where each run waits (`waiting_on`) and when it lands (`eta`); runs of other
             machines from their presence, which can never put a row in the queue
  safety     branches whose commits exist only on this Mac, embargoed or waiting for their backup
  since      today's fields plus the one-line digest of what finished
  as_of      when each slow source was last read, and how often it is refreshed
Pure: no file, git or network read. Each area is built on its own: one that fails becomes {"error": {title,
detail}} and the others still publish.

Landing times are `progress.eta_range`, with one rule added: a step paused on a pending decision counts as running,
so its finished work is subtracted and the time waiting for the developer adds nothing. Ranges are rounded to 5
minutes. `LandingMemo` reuses a run's landing for up to 60 s while the run's facts stay the same.

Units: instants are integer epoch seconds, durations integer seconds in `_s` fields. Text holds clock times
("15:55") and fixed durations ("1 h 40"), never a relative age: the page renders ages from the epochs.
Copied commands quote every interpolated value (shlex.quote, `--` before branch and path arguments where the
command accepts it); a value with a control character gets no command. Links are https on the project's forge host.
"""
from __future__ import annotations

import copy as _copy
import hashlib
import math
import re
import shlex
import threading
import time
from urllib.parse import urlsplit

from . import branches, dashboard, progress

TTL_S = 60                     # a run's landing is reused this long while its facts stay the same
STALE_S = 30 * 60              # no sign of life for this long: the run stopped reporting
BACKUP_QUEUE_S = 4 * 3600      # a branch only on this Mac joins the queue when its oldest commit is this old
CI_WINDOW_S = 24 * 3600        # CI failures this recent enter the queue
DONE_RUN_S = 3600              # finished runs listed this long
ROUND_S = 300                  # landing ranges are rounded to 5 minutes
ITEMS_CAP, RUNS_CAP, REMOTE_RUNS_CAP, ROWS_CAP = 200, 20, 20, 50
TITLE, LINE, CLAUSE = 160, 300, 120
EVERY_S = {"registry": 60,     # dashserver.REGISTRY_MAX_SECONDS: the registry is rebuilt this often with a page open
           "prs": 900}         # registry.prs(max_age=900): the PR cache's age limit
AREA_TITLES = {"attention": "Needs you", "runs": "Runs", "safety": "Safety", "since": "Since yesterday",
               "as_of": "Data age"}

PR_KINDS = {"ci-failing": ("alert", "is failing CI"), "changes-requested": ("ink", "has changes requested"),
            "conflicts": ("ink", "has merge conflicts"), "review-waiting": ("ink", "is waiting for review"),
            "review-requested": ("ink", "waits for your review")}
WORK_KINDS = ("blocked", "overdue", "blocked-task")
HOUSEKEEPING = {"reclaim": ("finished worktree", "finished worktrees"), "expired-lock": ("expired lock", "expired locks"),
                "collision": ("branch with two sessions", "branches with two sessions"),
                "idle-work": ("checkout with idle work", "checkouts with idle work"),
                "stale-note": ("note behind the code", "notes behind the code"),
                "drift": ("configuration drift", "configuration drifts"),
                "layout": ("project outside the layout", "projects outside the layout")}
RUN_GROUP = {"decision": 0, "grant": 0, "blocked": 1, "stale": 2}
STATE_ORDER = {"not_backed_up": 0, "embargoed": 1, "waiting": 2}
RECOMMENDED_RE = re.compile(r"\s*\(recommended\)\s*$", re.IGNORECASE)
OPTION_RE = re.compile(r"^\s*([A-Za-z0-9]{1,3})\s*:\s*(\S.*)$")


# --------------------------------------------------------------------------- small helpers

def _int(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def _line(s, cap: int = LINE) -> str:
    """One line of at most `cap` characters."""
    s = re.sub(r"\s+", " ", str(s if s is not None else "")).strip()
    return s if len(s) <= cap else s[:cap - 1].rstrip() + "…"


def _first_line(s) -> str:
    return next((ln.strip() for ln in str(s or "").splitlines() if ln.strip()), "")


def _clock(t) -> str:
    return time.strftime("%H:%M", time.localtime(t))


def _dur(seconds) -> str:
    """A fixed duration: "35 min", "2 h", "1 h 40"."""
    m = int(round((seconds or 0) / 60))
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h" if not m else f"{h} h {m:02d}"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _floor(x: float) -> int:
    return int(math.floor(round(x / ROUND_S, 6))) * ROUND_S


def _ceil(x: float) -> int:
    return int(math.ceil(round(x / ROUND_S, 6))) * ROUND_S


def _cmd(s):
    """A command to copy, or None when it holds a control character (a newline would run a second command)."""
    if not s or not isinstance(s, str) or any(ord(c) < 32 or ord(c) == 127 for c in s):
        return None
    return s


def _q(v) -> str:
    return shlex.quote(str(v))


def _who(name, roster) -> str | None:
    """A crew member as `<Role> (<Name>)`; the main session as "The tech lead" when the roster cannot be read;
    anyone else as written."""
    name = str(name or "").strip()
    if not name:
        return None
    if roster:
        try:
            from . import crew
            return crew.who(name, roster)
        except Exception:  # an unreadable roster: the name as written
            pass
    return "The tech lead" if name == "main" else name


def _forge_host(p: dict):
    remote = str((p or {}).get("remote") or "")
    m = re.match(r"^[a-z][a-z0-9+.-]*://(?:[^@/]+@)?([^/:]+)", remote) or re.match(r"^[^@/\s]+@([^:/\s]+):", remote)
    if m:
        return m.group(1).lower()
    return "github.com" if (p or {}).get("github") else None


def _url(u, p):
    """`u` when it is https on the project's forge host; None otherwise (javascript:, http:, another host)."""
    if not isinstance(u, str) or not p or _cmd(u) is None or " " in u:
        return None
    try:
        parts = urlsplit(u)
    except ValueError:
        return None
    host = _forge_host(p)
    return u if parts.scheme == "https" and host and (parts.hostname or "").lower() == host else None


def _projects(reg) -> list:
    return [p for p in (reg or {}).get("projects") or [] if isinstance(p, dict) and p.get("id")]


def _project_of(reg, ref):
    """A project by id or forge slug."""
    if not ref:
        return None
    for p in _projects(reg):
        if ref in (p.get("id"), p.get("github")):
            return p
    return None


def _name(p, ref="") -> str:
    return (p or {}).get("name") or str(ref or "").split("/")[-1]


def _open(kind, ident=None, anchor=None) -> dict:
    return {"kind": kind, "id": ident, "anchor": anchor}


def _item(**kw) -> dict:
    it = {"id": None, "rank": 6, "kind": None, "tone": "ink", "title": "", "lines": [], "clause": "", "since": None,
          "count": 1, "project": None, "run_id": None, "asker": None, "blocks": None, "options": None,
          "irreversible": None, "type_to_answer": None, "copy": None, "open": _open("all"), "url": None}
    it.update(kw)
    it["title"] = _line(it["title"], TITLE)
    it["clause"] = _line(it["clause"], CLAUSE)
    it["lines"] = [_line(x, LINE) for x in it["lines"] if x][:3]
    it["since"] = _int(it["since"])
    return it


def _order(it: dict):
    since = it.get("since")
    return (it["rank"], -((it.get("blocks") or {}).get("lanes") or 0), since is None, since or 0, it["id"])


# --------------------------------------------------------------------------- landing

class LandingMemo:
    """A run's landing, reused for up to `ttl_s` seconds while its key stays the same: a new key (a step finished, a
    decision answered, a new sign of life) recomputes at once. Thread-safe; one per process (LANDING)."""

    def __init__(self, ttl_s: float = TTL_S, clock=time.time):
        self.ttl_s, self.clock = ttl_s, clock
        self._memo, self._lock = {}, threading.Lock()

    def get(self, run_id: str, key, compute) -> dict:
        now = self.clock()
        with self._lock:
            hit = self._memo.get(run_id)
            if hit and hit[0] == key and now - hit[1] < self.ttl_s:
                return _copy.deepcopy(hit[2])
        value = compute()
        with self._lock:
            if len(self._memo) > 500:  # runs come and go: forget the ones not asked for lately
                self._memo = {k: v for k, v in self._memo.items() if now - v[1] < self.ttl_s}
            self._memo[run_id] = (key, now, _copy.deepcopy(value))
        return value


LANDING = LandingMemo()


def _pending(run: dict) -> list:
    """The decisions that need the developer (progress.pending_decisions): pending and not moot, oldest first."""
    return sorted(progress.pending_decisions(run), key=lambda d: (d.get("ts") or 0, str(d.get("id"))))


def _moot(run: dict, roster) -> list:
    """A run's moot decisions (asked by a lane that ended, and no step waits on them), each with its own line: shown
    on the run, never in the queue."""
    return [{"id": _line(progress.shown_id(run, d.get("id")), 20), "question": _line(d.get("question"), TITLE),
             "asker": _who(d.get("from_name") or d.get("from"), roster), "since": _int(d.get("ts")),
             "text": _line(progress.moot_text(run, d))}
            for d in sorted(progress.moot_decisions(run), key=lambda d: (d.get("ts") or 0, str(d.get("id"))))]


def _questions(run: dict) -> list:
    """The crew's questions to the tech lead that wait for an answer (progress.waiting_messages); notices never. Each
    id names its run (progress.shown_id): the page belongs to no session."""
    return [dict({k: m.get(k) for k in ("ts", "from_name", "text")}, id=progress.shown_id(run, m.get("id")))
            for m in progress.waiting_messages(run)][-20:]


def _steps(run: dict) -> list:
    return [s for s in run.get("plan") or [] if isinstance(s, dict)]


def _blocked(run: dict) -> bool:
    pending = {d.get("id") for d in _pending(run)}
    return run.get("state") == "blocked" or any(s.get("state") == "blocked" and s.get("waiting") not in pending
                                                for s in _steps(run))


def waiting_on(run: dict, now: float):
    """What a run of this Mac waits on: {kind: decision|grant|blocked|stale, ref, since}, the first that applies
    (the oldest pending decision; the latest `blocked` update; no sign of life for 30 min); None when it moves."""
    if not isinstance(run, dict) or run.get("state") == "done":
        return None
    pend = _pending(run)
    if pend:
        d = pend[0]
        return {"kind": "grant" if d.get("irreversible") else "decision",
                "ref": _line(progress.shown_id(run, d.get("id")), 20),
                "since": _int(d.get("ts"))}
    if _blocked(run):
        u = next((u for u in reversed(run.get("updates") or []) if isinstance(u, dict) and u.get("kind") == "blocked"),
                 None) or {}
        return {"kind": "blocked", "ref": _line(u.get("text"), LINE) or None, "since": _int(u.get("ts"))}
    seen = dashboard.run_seen(run)
    if seen and now - seen >= STALE_S:
        return {"kind": "stale", "ref": None, "since": seen}
    return None


def landing_key(run: dict, wo=None) -> tuple:
    """What a landing depends on besides the clock: the latest sign of life, each step's state, the pending
    decisions, and what the run waits on."""
    return (dashboard.run_seen(run), tuple((str(s.get("name")), str(s.get("state"))) for s in _steps(run)),
            tuple(str(d.get("id")) for d in _pending(run)), (wo or {}).get("kind"))


def _paused(run: dict) -> dict:
    """A shallow copy in which each step blocked on a pending decision counts as running."""
    pending = {d.get("id") for d in _pending(run)}
    plan = [dict(s, state="running") if isinstance(s, dict) and s.get("state") == "blocked"
            and s.get("waiting") in pending else s for s in run.get("plan") or []]
    return dict(run, plan=plan)


def _has_estimate(run: dict) -> bool:
    return bool(run.get("estimate")) or any((s.get("est") or 0) > 0 for s in _steps(run))


def _eta(state, basis=None, remaining=None, lands=None, over=None, text="") -> dict:
    return {"state": state, "basis": basis, "remaining_s": remaining, "lands_at": lands, "over_estimate": over,
            "text": text}


def _remaining(lo_min: float, hi_min: float) -> dict:
    low = max(ROUND_S, _floor(lo_min * 60))
    return {"low": low, "high": max(low, _ceil(hi_min * 60))}


def _lands(base: float, lo_min: float, hi_min: float) -> dict:
    low = _floor(base + lo_min * 60)
    return {"low": low, "high": max(low, _ceil(base + hi_min * 60))}


def _span(rem: dict) -> str:
    lo, hi = rem["low"], rem["high"]
    if lo == hi:
        return f"about {_dur(lo)}"
    if hi <= 3600:
        return f"{lo // 60}–{hi // 60} min"
    return f"{_dur(lo)}–{_dur(hi)}"


def _lands_text(lands: dict) -> str:
    a, b = _clock(lands["low"]), _clock(lands["high"])
    return f"Lands about {a}" if a == b else f"Lands {a}–{b}"


def landing(run: dict, now: float) -> dict:
    """The `eta` of one run of this Mac (spec table: clock, after_answer, blocked, unknown, none, done)."""
    if run.get("state") == "done":
        fin = run.get("finished") or now
        return _eta("done", text=f"Finished {_clock(fin)} in {_dur(progress.active_minutes(run, now=fin) * 60)}")
    wo = waiting_on(run, now)
    kind = (wo or {}).get("kind")
    if kind == "blocked":
        return _eta("blocked", text=f"Blocked: {(wo.get('ref') or 'no reason given').rstrip('.')}. "
                                    "No landing time until it moves.")
    if kind == "stale":
        return _eta("unknown", text="The landing time is unknown.")
    if not _has_estimate(run):
        return _eta("none", text="No estimate, so no landing time.")
    basis = "critical_path" if _steps(run) else "estimate"
    if kind in ("decision", "grant"):
        rem = _remaining(*progress.eta_range(_paused(run), now))
        return _eta("after_answer", basis, rem, text=f"Lands {_span(rem)} after you answer {wo['ref']}")
    lo, hi = progress.eta_range(run, now)
    lands = _lands(now, lo, hi)
    text, over = _lands_text(lands), None
    est = run.get("estimate_cal") or run.get("estimate") or 0
    total = progress.active_minutes(run, now=now) + progress.eta(run, now)
    if est and total > 1.5 * est:
        over = {"estimate_s": int(round(est * 60)), "now_s": int(round(total * 60))}
        text += (f", later than planned (estimate {_dur(est * 60)}, "
                 f"now about {_dur(round(total / 5) * 5 * 60)}).")
    return _eta("clock", basis, _remaining(lo, hi), lands, over, text)


# --------------------------------------------------------------------------- runs

BRIEF_TEXT = ("goal", "state", "headline", "last", "next", "mode", "agent", "branch", "project")
BRIEF_NUM = ("step", "steps", "estimate", "estimate_cal", "eta_min", "eta_lo", "eta_hi", "active_min", "wall_min",
             "pace", "total_min")                  # minutes, counts and a pace
BRIEF_EPOCH = ("started", "updated", "finished")   # instants
MINUTES_MAX = 1_000_000                            # about 2 years: a larger duration from another Mac is not real
EPOCH_SPAN_S = 10 * 365 * 86400                    # an instant from another Mac further than ~10 years from now


def _num(v, lo: float = 0, hi: float = MINUTES_MAX):
    """A finite number in [lo, hi], or None. Another Mac's presence is untrusted: a NaN, 1e308 or an instant
    centuries away would make its landing time raise."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        return None
    return v


def _remote_brief(b: dict, now: float) -> dict:
    """A run brief from another machine's presence, kept to today's fields with their types and sane ranges
    (untrusted input)."""
    span = (now - EPOCH_SPAN_S, now + EPOCH_SPAN_S)
    out = {k: _line(b.get(k), LINE) if isinstance(b.get(k), str) else None for k in BRIEF_TEXT}
    out.update({k: _num(b.get(k)) for k in BRIEF_NUM})
    out.update({k: _num(b.get(k), *span) for k in BRIEF_EPOCH})
    out["plan"] = [{"name": _line(s.get("name"), 120), "state": _line(s.get("state"), 20), "est": _num(s.get("est")),
                    "min": _num(s.get("min")), "owner": _line(s.get("owner"), 120)}
                   for s in (b.get("plan") if isinstance(b.get("plan"), list) else [])[:200] if isinstance(s, dict)]
    # a lane as dev status shows it (dashboard.page_lane): working, stale ("Stopped reporting") or ended (not working)
    out["agents"] = [dashboard.page_lane({"label": _line(a.get("label"), 120), "state": _line(a.get("state"), 20),
                                          "min": _num(a.get("min"))})
                     for a in (b.get("agents") if isinstance(b.get("agents"), list) else [])[:50] if isinstance(a, dict)]
    out.update(decisions=[], questions=[], updates=[], moot=[])
    return out


def _remote_run(s, h: str, user: str, updated, now: float):
    """One session of another machine as a run, or None when it has no live progress."""
    b = s.get("progress") if isinstance(s, dict) else None
    if not isinstance(b, dict) or not b.get("goal") or b.get("state") not in ("running", "blocked", "done"):
        return None
    if b.get("state") == "done" and (not updated or now - updated > DONE_RUN_S):
        return None
    run = _remote_brief(b, now)
    agent, project = _line(s.get("agent"), 40) or "agent", _line(s.get("project"), 200) or None
    key = f"{user}@{h}|{agent}|{project}|{s.get('started')}"
    slug = re.sub(r"[^a-z0-9]+", "-", f"{h}-{agent}".lower()).strip("-")
    run.update(id=f"remote-{slug}-{hashlib.blake2b(key.encode(), digest_size=2).hexdigest()}", agent=agent,
               project=project, branch=_line(s.get("branch"), 200) or None, machine=_line(h, 80),
               who=_line(f"{user}@{h}", 120), host=_line(h, 80), seen=updated,
               started=_int(run.get("started")) or _int(s.get("started")))
    run["waiting_on"], run["eta"] = _remote_landing(run, updated, now)
    return run


def _remote_runs(team, host: str, now: float) -> list:
    """Runs of other machines, from their presence (sessions[].progress): never a decision, never a queue row. A
    session that cannot be read is skipped alone; this Mac's runs and the other sessions still show."""
    from . import work
    out = []
    for person in team or []:
        if not isinstance(person, dict):
            continue
        h, user = str(person.get("host") or ""), str(person.get("user") or "")
        if not h or h.lower() == str(host or "").lower():
            continue
        updated = int(work.parse_iso(person.get("updated") or "")) or None
        for s in person.get("sessions") or []:
            try:
                run = _remote_run(s, h, user, updated, now)
            except Exception:  # an unforeseen shape in one record from another Mac: skip that session only
                continue
            if run:
                out.append(run)
    out.sort(key=lambda r: -(r.get("started") or 0))
    return out[:REMOTE_RUNS_CAP]


def _remote_landing(run: dict, updated, now: float):
    if run["state"] == "done":
        fin = _int(run.get("updated")) or updated
        return None, _eta("done", text=f"Finished {_clock(fin)} in {_dur((run.get('active_min') or 0) * 60)}")
    if not updated or now - updated >= STALE_S:
        return {"kind": "stale", "ref": None, "since": updated}, _eta("unknown", text="The landing time is unknown.")
    if run["state"] == "blocked":
        reason = run.get("last") or None
        return ({"kind": "blocked", "ref": reason, "since": _int(run.get("updated")) or updated},
                _eta("blocked", text=f"Blocked: {(reason or 'no reason given').rstrip('.')}. "
                                     "No landing time until it moves."))
    lo, hi = run.get("eta_lo"), run.get("eta_hi")
    if lo is None or hi is None:
        return None, _eta("none", text="No estimate, so no landing time.")
    lands = _lands(updated, lo, hi)
    return None, _eta("clock", "critical_path" if run.get("plan") else "estimate", _remaining(lo, hi), lands, None,
                      _lands_text(lands))


def _run_order(r: dict):
    if r.get("state") == "done":
        return (4, -(r.get("finished") or r.get("updated") or 0))
    group = RUN_GROUP.get((r.get("waiting_on") or {}).get("kind"), 3)
    return (group, -(r.get("started") or 0))


def _runs(raw_runs, briefs, now, host, team, memo, roster=None) -> list:
    by_id = {dashboard.run_id(r): r for r in raw_runs or [] if isinstance(r, dict)}
    out = []
    for b in (briefs or [])[:RUNS_CAP]:
        raw = by_id.get(b.get("id"))
        if raw is None:
            continue
        wo = waiting_on(raw, now)
        eta = memo.get(b["id"], landing_key(raw, wo), lambda raw=raw: landing(raw, now))
        decisions = [dict({k: d.get(k) for k in ("ts", "question", "options", "recommend", "irreversible",
                                                  "from_name")}, id=progress.shown_id(raw, d.get("id")))
                     for d in _pending(raw)]
        out.append(dict(b, machine=None, who=None, branch=None, waiting_on=wo, eta=eta, decisions=decisions,
                        questions=_questions(raw), moot=_moot(raw, roster)))
    out += _remote_runs(team, host, now)
    return sorted(out, key=_run_order)


# --------------------------------------------------------------------------- safety

def _branch_maps(reg) -> list:
    """[(project, rows or None)] for the projects cloned here; None: no branch map yet."""
    out = []
    for p in _projects(reg):
        if p.get("local") is False or p.get("git") is False:
            continue
        rows = p.get("branches")
        out.append((p, [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else None))
    return out


def _backup_cmd(path, branch):
    if not path or not branch:
        return None
    return _cmd(f"cd -- {_q(path)} && dev backup -- {_q(branch)}")


def _risk_rows(p: dict, rows: list) -> list:
    out = []
    for r in rows:
        b = r.get("backup") if isinstance(r.get("backup"), dict) else {}
        st = b.get("state")
        if st in branches.AT_RISK:
            state = "not_backed_up"
        elif r.get("embargo") or st == "embargoed":
            state = "embargoed"
        elif st == "waiting":
            state = "waiting"
        else:
            continue
        # dev backup never pushes the default branch (backup._one skips it), so it gets no command
        out.append({"branch": _line(r.get("name"), 200), "state": state, "reason": _line(b.get("reason"), LINE),
                    "commits": _int(r.get("unpushed")), "oldest": _int(r.get("oldest_unpushed")),
                    "copy": _backup_cmd(p.get("path"), r.get("name"))
                    if st == "not_backed_up" and not r.get("default") else None})
    out.sort(key=lambda x: (STATE_ORDER[x["state"]], x["oldest"] is None, x["oldest"] or 0, x["branch"]))
    return out


def _safety(reg) -> dict:
    totals = {"not_backed_up": 0, "embargoed": 0, "waiting": 0, "unknown_projects": 0}
    projects, mapped, first = [], 0, None
    for p, rows in _branch_maps(reg):
        if rows is None:
            totals["unknown_projects"] += 1
            continue
        mapped += 1
        c = branches.counts(rows)
        for k in ("not_backed_up", "embargoed", "waiting"):
            totals[k] += c[k]
        risk = _risk_rows(p, rows)
        if not risk:
            continue
        nb = [x for x in risk if x["state"] == "not_backed_up"]
        first = first or (nb and (p, nb[0]))
        projects.append({"project": p["id"], "not_backed_up": c["not_backed_up"], "embargoed": c["embargoed"],
                         "waiting": c["waiting"], "oldest": min((x["oldest"] for x in nb if x["oldest"]), default=None),
                         "mapped_at": _int(reg.get("generated")), "rows": risk[:ROWS_CAP]})
    n = totals["not_backed_up"]
    oldest = min((x["oldest"] for p in projects for x in p["rows"] if x["state"] == "not_backed_up" and x["oldest"]),
                 default=None)
    if n == 1 and first:
        p, r = first
        text = f"1 branch is only on this Mac: {_name(p)} {r['branch']}" + \
               (f", {_plural(r['commits'], 'commit', 'commits')}" if r["commits"] else "")
        line = {"tone": "caution", "text": text, "oldest": r["oldest"]}
    elif n:
        m = sum(1 for p in projects if p["not_backed_up"])
        line = {"tone": "caution", "text": f"{n} branches are only on this Mac, in {_plural(m, 'project', 'projects')}",
                "oldest": oldest}
    elif not _projects(reg):
        line = {"tone": "ok", "text": "No projects yet, so no branches to back up.", "oldest": None}
    elif not mapped:
        line = {"tone": "unknown", "text": "No branch map yet. dev sync makes one.", "oldest": None}
    else:
        line = {"tone": "ok", "text": "Every branch is on the remote.", "oldest": None}
    line["text"] = _line(line["text"], LINE)
    line["open"] = _open("branches")
    return {"totals": totals, "line": line, "projects": projects}


# --------------------------------------------------------------------------- the queue

def _options(d: dict) -> list:
    rec = str(d.get("recommend") or "").strip().lower()
    parsed = []
    for o in d.get("options") or []:
        text = RECOMMENDED_RE.sub("", str(o)).strip()
        m = OPTION_RE.match(text)
        # an option without a letter is answered by its whole text: one line, never cut with "…" (no option would match)
        key, label = (m.group(1), m.group(2).strip()) if m else (re.sub(r"\s+", " ", text)[:LINE].rstrip(), text)
        parsed.append((key, label, text))
    pick = next((i for i, (k, _, _) in enumerate(parsed) if rec and k.lower() == rec), None)
    if pick is None:
        pick = next((i for i, (_, _, t) in enumerate(parsed) if rec and t.lower().startswith(rec)), None)
    return [{"key": k, "text": _line(lab, TITLE), "recommended": i == pick} for i, (k, lab, _) in enumerate(parsed)]


def _type_to_answer(did: str, opts: list) -> list:
    if not opts:
        return [f"approve {did}"]
    if any(o["recommended"] for o in opts):
        return [f"approve {did}"] + [f"approve {did}: {o['key']}" for o in opts if not o["recommended"]]
    return [f"approve {did}: {o['key']}" for o in opts]


def _lanes_waiting(run: dict, did: str) -> int:
    """Distinct crew lanes (not the tech lead) whose open step waits on the decision or on such a step."""
    open_ = [s for s in _steps(run) if s.get("state") not in progress.DONE]
    names = {s.get("name") for s in open_ if s.get("waiting") == did}
    grew = True
    while grew:
        more = {s.get("name") for s in open_ if s.get("name") not in names and set(s.get("after") or []) & names}
        names |= more
        grew = bool(more)
    lanes = {s.get("owner") for s in open_ if s.get("name") in names}
    lanes |= {k for k, a in (run.get("agents") or {}).items() if isinstance(a, dict) and a.get("waiting") == did}
    return len({x for x in lanes if x and x not in progress.NOT_A_LANE})


def _decision_items(run: dict, rid: str, roster) -> list:
    goal = _line(run.get("goal") or "the run", 80)
    out = []
    for d in _pending(run):
        # `did` is what the run's own session reads (the item's key, the run's anchor, the typed approve line);
        # `shown` names the run too, since the page belongs to no session (progress.shown_id)
        did, shown = _line(d.get("id"), 20), _line(progress.shown_id(run, d.get("id")), 20)
        asker = _who(d.get("from_name") or d.get("from") or "main", roster)
        lanes = _lanes_waiting(run, d.get("id"))
        common = dict(id=f"decision:{rid}:{did}", rank=1, tone="you", since=d.get("ts"), project=run.get("project"),
                      run_id=rid, asker=asker, blocks={"run": goal, "lanes": lanes}, options=_options(d),
                      clause=f"{goal} is paused until you answer {shown}.", open=_open("run", rid, did))
        act, target = _line(d.get("irreversible"), 40), _line(d.get("target"), 120)
        if act:
            what = f"{act.replace('-', ' ').capitalize()} {('PR ' + target) if target.isdigit() else target}".strip()
            out.append(_item(**common, kind="grant", irreversible=act, title=f"{what} waits for your grant",
                             lines=[f"{asker} asked at {_clock(d.get('ts') or 0)} for {goal}.", d.get("question")],
                             type_to_answer=[f"grant {act} {target}"] if target and " " not in target
                             else _type_to_answer(did, common["options"])))
            continue
        waits = f", {_plural(lanes, 'crew member waits', 'crew members wait')}" if lanes else ""
        out.append(_item(**common, kind="decision", title=f"{shown} · {d.get('question') or ''}",
                         lines=[f"{asker} asks · pauses {goal}{waits}"],
                         type_to_answer=_type_to_answer(did, common["options"])))
    return out


def _last_reporter(run: dict, roster) -> str:
    """Who gave the run's latest sign of life: a crew lane, or the tech lead."""
    best, who = max([run.get("started") or 0] + [u.get("ts") or 0 for u in run.get("updates") or []
                                                 if isinstance(u, dict)]), "main"
    for lane in (run.get("agents") or {}).values():
        if not isinstance(lane, dict):
            continue
        ts = max([lane.get(k) or 0 for k in ("started", "finished", "last_seen")]
                 + [u.get("ts") or 0 for u in lane.get("updates") or [] if isinstance(u, dict)])
        if ts > best and lane.get("persona"):
            best, who = ts, lane["persona"]
    return _who(who, roster) or "The tech lead"


def _run_items(raw_runs, now, roster) -> list:
    out = []
    for run in raw_runs or []:
        if not isinstance(run, dict) or run.get("state") == "done":
            continue
        rid, goal = dashboard.run_id(run), _line(run.get("goal") or "A run", 80)
        out += _decision_items(run, rid, roster)
        wo = waiting_on(run, now)
        kind = (wo or {}).get("kind")
        if kind == "blocked":
            out.append(_item(id=f"run_blocked:{rid}", rank=2, kind="run_blocked", tone="alert", title=f"{goal} is blocked",
                             clause=f"{goal} is blocked.", lines=[wo.get("ref")], since=wo.get("since"),
                             project=run.get("project"), run_id=rid, open=_open("run", rid)))
        elif kind == "stale":
            out.append(_item(id=f"run_stale:{rid}", rank=2, kind="run_stale", tone="alert",
                             title=f"{goal} stopped reporting", clause=f"{goal} stopped reporting.",
                             lines=[f"{_last_reporter(run, roster)} sent no update since {_clock(wo['since'])}."],
                             since=wo["since"], project=run.get("project"), run_id=rid, open=_open("run", rid)))
    return out


CI_SETTLED = ("passed", "failed")  # a watch that timed out or found no run says nothing about the failure before it


def _ci_items(reg, ci, now) -> list:
    """A row per repository and branch whose newest settled watch failed in the last day: a later green run on the
    same branch and repository clears the failure."""
    watches = ci.get("watches") if isinstance(ci, dict) else ci
    out, seen = [], set()
    for w in sorted([w for w in watches or [] if isinstance(w, dict)], key=lambda w: -(w.get("ts") or 0)):
        repo, branch, ts = str(w.get("repo") or ""), str(w.get("branch") or ""), w.get("ts") or 0
        if w.get("state") not in CI_SETTLED or (repo, branch) in seen:
            continue
        seen.add((repo, branch))  # the newest settled result of the branch decides
        if w.get("state") != "failed" or now - ts > CI_WINDOW_S:
            continue
        p = _project_of(reg, repo)
        f = next((x for x in w.get("failures") or [] if isinstance(x, dict)), {})
        job = next((j for j in f.get("jobs") or [] if isinstance(j, dict)), {})
        err = dashboard._redact(_first_line(f.get("excerpt") or w.get("first_error")))
        line = " · ".join(_line(x, 200) for x in (job.get("name"), job.get("step"), err) if x)
        rid = str(f.get("id") or "")
        num = rid if rid.isdigit() else str(f.get("number") or "")
        # dev run view reads the current folder's repository: the command first goes to the project's own folder
        path = (p or {}).get("path") if (p or {}).get("local") is not False else None
        out.append(_item(id=f"ci_failed:{repo}:{branch}", rank=2, kind="ci_failed", tone="alert",
                         title=f"CI failed · {branch} · {_name(p, repo)}", clause=f"CI failed on {branch}.",
                         lines=[line], since=ts, project=(p or {}).get("id") or repo or None,
                         copy=_cmd(f"cd -- {_q(path)} && dev run view {rid} --failed")
                         if path and rid.isdigit() else None,
                         open=_open("ci", f"{repo}#{num}" if num.isdigit() else repo), url=_url(f.get("url"), p)))
    return out


def _job_items(jobs) -> list:
    out = []
    for j in jobs or []:
        if not isinstance(j, dict) or j.get("state") != "failed":
            continue
        name = _line(j.get("name"), 40)
        # the failed run that stands (dashboard.jobs_summary): a run skipped after it is the last run, not the failure
        fail = j.get("failure") or j.get("last")
        last = fail if isinstance(fail, dict) else {}
        ts = last.get("start") or last.get("end")
        ok = bool(dashboard.JOB_RE.fullmatch(name))
        out.append(_item(id=f"job_failed:{name}", rank=4, kind="job_failed", tone="alert",
                         title=f"The {name} job failed" + (f" at {_clock(ts)}" if ts else ""),
                         clause=f"The {name} job failed.", lines=[_first_line(dashboard._redact(last.get("summary")))],
                         since=ts, copy=f"dev jobs show {name}" if ok else None,
                         open=_open("job", name if ok else None)))
    return out


def _backup_items(reg, now) -> list:
    out = []
    for p, rows in _branch_maps(reg):
        for r in _risk_rows(p, rows or []):
            if r["state"] != "not_backed_up" or not r["oldest"] or now - r["oldest"] < BACKUP_QUEUE_S:
                continue
            out.append(_item(id=f"backup:{p['id']}:{r['branch']}", rank=4, kind="backup", tone="caution",
                             title=f"Only on this Mac · {r['branch']} · {_name(p)}",
                             clause=f"{r['branch']} is only on this Mac.", lines=[r["reason"]], since=r["oldest"],
                             project=p["id"], copy=r["copy"], open=_open("branches", p["id"])))
    return out


def _action_items(reg, actions, ci_rows) -> list:
    """Today's action list: sign-in (rank 4), PRs and work (rank 5), housekeeping as one row (rank 6)."""
    out, seen, hk = [], set(), {}
    ci_keys = {(r["id"].split(":", 2)[1], r["id"].split(":", 2)[2]) for r in ci_rows}
    for a in actions or []:
        if not isinstance(a, dict):
            continue
        kind, p = a.get("kind"), _project_of(reg, a.get("project"))
        if kind == "sign-in":
            ref = re.sub(r"[^a-z0-9]+", "-", str(a.get("provider") or a.get("cmd") or "agent").lower()).strip("-")
            out.append(_item(id=f"setup:{ref}", rank=4, kind="setup", tone="caution", title=a.get("title"),
                             clause=f"{_line(a.get('title'), CLAUSE - 1)}.", lines=[a.get("detail")],
                             copy=_cmd(a.get("cmd")), open=_open("all", "setup")))
        elif kind in PR_KINDS:
            repo, n, branch = a.get("repo") or a.get("project") or "", a.get("number"), a.get("branch") or ""
            iid = f"pr:{repo}#{n}"
            if iid in seen or (repo, branch) in ci_keys:
                continue
            seen.add(iid)
            tone, verb = PR_KINDS[kind]
            out.append(_item(id=iid, rank=5, kind="pr", tone=tone, title=a.get("title"), clause=f"PR #{n} {verb}.",
                             lines=[" · ".join(x for x in (branch, _name(p, repo)) if x)], since=a.get("ts"),
                             project=a.get("project"), copy=_cmd(a.get("cmd")), open=_open("pr", f"{repo}#{n}"),
                             url=_url(a.get("url"), p)))
        elif kind in WORK_KINDS:
            digest = hashlib.blake2b(str(a.get("title")).encode(), digest_size=4).hexdigest()
            out.append(_item(id=f"work:{kind}:{a.get('project') or '-'}:{digest}", rank=5, kind="work", tone="ink",
                             title=a.get("title"), clause=f"{_line(a.get('title'), CLAUSE - 1)}.",
                             lines=[a.get("detail")], since=a.get("ts"), project=a.get("project"),
                             copy=_cmd(a.get("cmd")), open=_open("all", "work")))
        elif kind in HOUSEKEEPING:
            n, rank = hk.get(kind, (0, a.get("rank") or 99))
            hk[kind] = (n + max(1, int(a.get("count") or 1)), rank)
    if hk:
        total = sum(n for n, _ in hk.values())
        parts = [_plural(n, *HOUSEKEEPING[k]) for k, (n, _) in sorted(hk.items(), key=lambda kv: (-kv[1][0], kv[1][1]))]
        out.append(_item(id="housekeeping", rank=6, kind="housekeeping", tone="ink", title="Housekeeping", count=total,
                         clause=f"{_plural(total, 'housekeeping item', 'housekeeping items')}.",
                         lines=[", ".join(parts)], open=_open("all", "housekeeping")))
    return out


# --------------------------------------------------------------------------- headline, next up, setup

PRIORITY = {"high": 0, "normal": 1, "low": 2}


def _next_up(reg, items) -> list:
    by_scope = {}
    for it in items or []:
        if isinstance(it, dict) and it.get("bucket") == "now" and it.get("status") in ("todo", "doing"):
            by_scope.setdefault(it.get("scope"), []).append(it)
    out = []
    for p in sorted(_projects(reg), key=lambda p: -(p.get("last_activity") or 0)):
        its = sorted(by_scope.get(p["id"]) or [], key=lambda i: (PRIORITY.get(i.get("priority"), 1),
                                                                 str(i.get("created") or ""), str(i.get("id"))))
        if its:
            t, tid = its[0], str(its[0].get("id") or "")
            ok = bool(dashboard.ITEM_ID_RE.fullmatch(tid))
            out.append({"project": p["id"], "task_id": tid if ok else None, "title": _line(t.get("title"), TITLE),
                        "copy": _cmd(f"dev task -p {_q(p['id'])} start {_q(tid)}") if ok else None})
        else:  # a project that keeps its tasks in the handoff note only
            now_tasks = (((p.get("note_summary") or {}).get("tasks") or {}).get("now") or [])
            if now_tasks:
                out.append({"project": p["id"], "task_id": None, "title": _line(now_tasks[0], TITLE), "copy": None})
        if len(out) == 3:
            break
    return out


def _setup(reg) -> list:
    provs = [v for v in ((reg or {}).get("providers") or {}).items() if isinstance(v[1], dict)]
    signed = any(v.get("installed") and v.get("logged_in") is not False for _, v in provs)
    installed = [k for k, v in provs if v.get("installed")]
    sign_in = "claude" if "claude" in installed else "codex login" if "codex" in installed else None
    synced = bool((reg or {}).get("generated"))
    return [{"key": "agents", "done": signed,
             "text": "An agent is signed in" if signed else "Sign in to Claude Code or Codex",
             "copy": None if signed else sign_in},
            {"key": "project", "done": False, "text": "Add your first project", "copy": None},
            {"key": "sync", "done": synced, "text": "The first sync ran" if synced else "Run the first sync",
             "copy": None if synced else "dev sync"}]


URGENT = {"ci_failed": lambda it: f"CI failed on {it['id'].split(':', 2)[2]}",
          "job_failed": lambda it: f"The {it['id'].split(':', 1)[1]} job failed",
          "run_stale": lambda it: it["title"], "run_blocked": lambda it: it["title"]}


def _headline(items, total_rows, runs, raw_runs, reg, next_up) -> dict:
    top = [it for it in items if it["rank"] <= 4]
    count = sum(1 for it in total_rows if it["rank"] <= 4)
    if any(it["rank"] == 1 for it in top):
        text = f"{count} things need you" if count != 1 else \
            "1 grant waits for you" if top[0]["kind"] == "grant" else "1 decision waits for you"
        line = " ".join(it["clause"] for it in top[:2])
        return {"kind": "needs_you", "count": count, "text": text, "line": _line(line)}
    urgent = next((it for it in top if it["kind"] in URGENT), None)
    if urgent:
        return {"kind": "urgent", "count": count, "text": _line(URGENT[urgent["kind"]](urgent), TITLE),
                "line": (urgent["lines"] or [""])[0]}
    if top:
        return {"kind": "look", "count": count,
                "text": f"Nothing urgent. {_plural(count, 'thing', 'things')} to look at.", "line": top[0]["clause"]}
    if isinstance(runs, list):
        going = [r for r in runs if r.get("state") != "done"]
    else:  # the runs area failed: count this Mac's runs, without landing times
        going = [r for r in raw_runs or [] if isinstance(r, dict) and r.get("state") in ("running", "blocked")]
    if going:
        lands = [((r.get("eta") or {}).get("lands_at") or {}).get("low") for r in going]
        first = min([x for x in lands if x], default=None)
        line = f"{_plural(len(going), 'run is', 'runs are')} going."
        if first:
            line += f" {'It' if len(going) == 1 else 'The first'} lands about {_clock(first)}."
        return {"kind": "clear_running", "count": 0, "text": "Nothing needs you", "line": line}
    if not _projects(reg):
        return {"kind": "welcome", "count": 0, "text": "Welcome to devkit",
                "line": "Nothing is tracked yet. Clone a project with dev clone <owner/repo>, or start one with "
                        "dev new <name>."}
    line = "Nothing is running."
    if next_up:
        p = _project_of(reg, next_up[0]["project"])
        line += f" Next in {_name(p, next_up[0]['project'])}: {next_up[0]['title']}."
    return {"kind": "clear_idle", "count": 0, "text": "Nothing needs you", "line": _line(line)}


def _degraded(reg, ci, now) -> list:
    out = []
    for i in (reg or {}).get("issues") or []:
        if isinstance(i, (list, tuple)) and len(i) == 2 and i[0] == "error":
            out.append({"source": "registry", "detail": _line(dashboard._redact(i[1])),
                        "as_of": _int((reg or {}).get("generated"))})
    watches = ci.get("watches") if isinstance(ci, dict) else ci
    for w in watches or []:
        if isinstance(w, dict) and w.get("error") and now - (w.get("ts") or 0) <= CI_WINDOW_S:
            out.append({"source": "ci", "detail": _line(dashboard._redact(w["error"])), "as_of": _int(w.get("ts"))})
    return out[:10]


def _attention(reg, raw_runs, actions, ci, jobs, items, runs, now, roster) -> dict:
    ci_rows = _ci_items(reg, ci, now)
    rows = (_run_items(raw_runs, now, roster) + ci_rows + _job_items(jobs) + _backup_items(reg, now)
            + _action_items(reg, actions, ci_rows))
    rows.sort(key=_order)
    shown = rows[:ITEMS_CAP]
    next_up = _next_up(reg, items)
    head = _headline(shown, rows, runs, raw_runs, reg, next_up)
    return {"headline": head, "items": shown, "total": len(rows),
            "next_up": next_up if head["kind"] == "clear_idle" else [],
            "setup": _setup(reg) if head["kind"] == "welcome" else [], "degraded": _degraded(reg, ci, now)}


# --------------------------------------------------------------------------- since, as_of

def _since(since, raw_runs) -> dict:
    since = dict(since or {})
    frm = _int(since.get("from")) or 0
    out = []
    # since_yesterday counts before it caps its lists (30 tasks, 20 PRs): the line uses the counts
    counts = since.get("counts") if isinstance(since.get("counts"), dict) else {}
    done = counts.get("done", len(since.get("done") or []))
    merged = counts.get("merged", len(since.get("merged") or []))
    since["counts"] = {"done": done, "merged": merged}
    finished = [r for r in raw_runs or [] if isinstance(r, dict) and r.get("state") == "done"
                and (r.get("finished") or 0) >= frm]
    if done:
        out.append({"kind": "task_done", "count": done, "text": _plural(done, "task done", "tasks done")})
    if merged:
        out.append({"kind": "pr_merged", "count": merged, "text": _plural(merged, "PR merged", "PRs merged")})
    if len(finished) == 1:
        r = finished[0]
        est = r.get("estimate_cal") or r.get("estimate")
        text = f"1 run finished in {_dur(progress.active_minutes(r, now=r['finished']) * 60)}" + \
               (f" (estimate {_dur(est * 60)})" if est else "")
        out.append({"kind": "run_finished", "count": 1, "text": text})
    elif finished:
        out.append({"kind": "run_finished", "count": len(finished), "text": f"{len(finished)} runs finished"})
    since["line"] = {"from": frm, "items": out, "text": _line(" · ".join(x["text"] for x in out))}
    return since


def _as_of(reg, prs_at) -> dict:
    return {"registry": {"at": _int((reg or {}).get("generated")), "every_s": EVERY_S["registry"]},
            "prs": {"at": _int(prs_at), "every_s": EVERY_S["prs"]}}


def _guarded(name: str, build):
    try:
        return build()
    except Exception as e:  # one area failing never takes the others down
        return {"error": {"title": f"{AREA_TITLES[name]} could not be read",
                          "detail": _line(dashboard._redact(f"{type(e).__name__}: {e}"))}}


def areas(reg, raw_runs, briefs, actions, ci, jobs, items, since, team, now, host, memo=LANDING, *, roster=None,
          prs_at=None) -> dict:
    """The five Now areas. `raw_runs`: this Mac's progress runs; `briefs`: today's run fields for them
    (dashboard._runs); `actions`: dashboard.build_actions; `ci`: dashboard.ci_watches; `jobs`:
    dashboard.jobs_summary; `items`: dashboard.read_items; `since`: dashboard.since_yesterday; `team`: presence
    (dashboard.read_presence); `roster`: crew.load(), for names as `<Role> (<Name>)`; `prs_at`: the PR cache's last
    read."""
    runs = _guarded("runs", lambda: _runs(raw_runs, briefs, now, host, team, memo, roster))
    return {"attention": _guarded("attention", lambda: _attention(reg, raw_runs, actions, ci, jobs, items, runs, now,
                                                                  roster)),
            "runs": runs,
            "safety": _guarded("safety", lambda: _safety(reg)),
            "since": _guarded("since", lambda: _since(since, raw_runs)),
            "as_of": _guarded("as_of", lambda: _as_of(reg, prs_at))}
