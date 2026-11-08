# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Continuity through compaction, resume and /clear (lifecycle spec C12, "Continuity module contract").

A compaction replaces the conversation with the agent's summary. devkit keeps its own record of the session, so the
run, the crew working now, the decisions and messages waiting, and the developer's latest requests survive it
verbatim, and a summary that left one out is caught.

Records (one lock per session covers them all, state.lock_file; writes go through state: 0600 files, atomic)
  STATE/continuity/<agent>-<sid>.json   checkpoint, transcript offsets, compactions (<= 20), "Not in the summary",
                                        deliveries waiting, cleared_from. Kept 7 days after its last change
                                        (expire), so it outlives SessionEnd and `claude --resume` finds it.
  STATE/continuity/clear-<agent>-<sha256(cwd)[:16]>.json
                                        {old_sid, cwd, ts}: SessionEnd(clear) writes it, the next SessionStart(clear)
                                        in that folder reads it (10 minutes at most).
  STATE/sessions/<agent>-<sid>.json     gets `compactions: {count, members, last}` for dev status and presence.

Hook entry points (hooks.py calls them; each never raises: an error goes to hooks.log and the hook answers as it
would without continuity; no subprocess or git call while a lock is held; progress is called only after the lock)
  on_pre_compact   checkpoint and open the compaction; Claude main: the summarizer's preservation instructions
  on_session_start startup/fork: offset; compact: the continuity brief; resume: the brief; clear: the open-run notice
  on_post_compact  close the compaction; Claude main: compute "Not in the summary" for the next hook
  on_prompt        the prompt waits as pending in the checkpoint; then what on_tool gives the main session
  on_tool          a late compaction's brief or the "Not in the summary" list (main); a member's reminder (member)
  on_stop          keep this turn's prompts when the turn is development work, refresh the checkpoint, save offsets
  is_summarizer    the compaction summarizer's SubagentStop: no lane, nothing to bind
  on_subagent_stop a last read of the member's transcript
  on_session_end   clear: the clear link, and the old checkpoint's prompts go

Prompts (compliance review, change 5, and its second review C1-C5): redacted with audit.redact, cut to 1,000
characters and redacted again; kept only for turns Stop classes as development work (pending until then; dropped
when the classifier fails); held only in checkpoint.prompts ("Not in the summary" keeps a request's prompt_id and
time, and its excerpt is built at delivery from the prompt still there); dropped by every write 7 days after they
were written, and never shown after that; re-sent only to the session's own provider, which also alone sees the text
of decisions and crew messages; a pending prompt only to the session itself; `--provider` only narrows who that is;
`dev session forget` deletes them. Background notifications are never stored.
"""
from __future__ import annotations

import collections
import datetime as dt
import functools
import hashlib
import json
import os
import re
import time
import traceback
import unicodedata
from pathlib import Path

from . import audit, core, progress, state
from .core import STATE

CONT = STATE / "continuity"
SCAN_CAP = 8 << 20  # bytes a hook reads from a transcript at most; past it, the last 8 MB and a gap
MOVE_SAVE = 1 << 20  # an offset that moved this far is saved even without a find
HEAD_CAP = 64 << 10  # bytes read from the head of a member's transcript for its launch brief
PAIR_S = 15 * 60  # a hook or a boundary joins a compaction that started at most this long before it
AFTER_S = 120  # ... or this long after it (a hook that ran just after the boundary was written)
KEEP_DAYS = 7  # a continuity record is deleted this long after its last change (compliance review: approved)
CLEAR_TTL = 600  # seconds a clear link links the next session
LATEST_H = 12  # `--latest` takes records changed in the last 12 hours
CHECKPOINT_MAX = 16384  # bytes of the checkpoint as written (escaped JSON)
BRIEF_MAX = 4096  # UTF-8 bytes of the continuity brief
NOTICE_MAX = 800
MISSING_MAX = 1024
REMINDER_MAX = 1536
INSTRUCTIONS_MAX = 2000  # characters of the summarizer's instructions (Claude cuts them at 10,000)
PROMPT_MAX = 1000
PROMPTS = 5
COMPACTIONS = 20
COUNTED = 50  # boundary uuids remembered, so the same bytes read again never count twice
LANES = 8
DECISIONS = 6
MESSAGES = 6
STEPS = 30
IDS = 12  # decision and message ids a brief lists
STATUS_MAX = 1200
SHINGLE = 4
PRESENT = 0.6  # share of a request's 4-character shingles the summary must hold

Scan = collections.namedtuple("Scan", "boundaries offset gap")
_NEEDLE = b'"compact_boundary"'
_BACKGROUND = ("<task-notification", "[SYSTEM NOTIFICATION", "<system-reminder", "<agent-message")
_PROVIDER = {"claude": "anthropic", "anthropic": "anthropic", "codex": "openai", "openai": "openai",
             "chatgpt": "openai"}
_OWN = re.compile(r"^(?:clear-)?[A-Za-z0-9_.-]+\.json$")  # this module's file names (never a temporary file)
_DONE = ("done", "skipped")


# --------------------------------------------------------------------------- records

def _safe(s) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "", str(s or ""))[:120] or "-"


def record_path(agent: str, sid: str) -> Path:
    return CONT / f"{_safe(agent)}-{_safe(sid)}.json"


def clear_path(agent: str, cwd: str) -> Path:
    return CONT / f"clear-{_safe(agent)}-{hashlib.sha256(str(cwd or '').encode()).hexdigest()[:16]}.json"


def read(agent: str, sid: str):
    """The session's continuity record, without the lock; None when it is missing or does not parse."""
    return state.read(record_path(agent, sid))


def member_transcript(transcript_path: str, agent_id: str) -> str:
    """Claude Code keeps a member's transcript at <main transcript without .jsonl>/subagents/agent-<id>.jsonl."""
    if not transcript_path or not agent_id:
        return ""
    return str(Path(transcript_path).with_suffix("") / "subagents" / f"agent-{_safe(agent_id)}.jsonl")


def _log(msg: str):
    try:
        from . import hooks
        hooks._log(f"continuity {msg}")
    except Exception:
        pass


def _fail_open(default):
    """A hook entry point never raises: the error goes to hooks.log and the hook answers without continuity."""
    def wrap(fn):
        @functools.wraps(fn)
        def run(*a, **k):
            try:
                return fn(*a, **k)
            except Exception:
                _log(f"{fn.__name__}: {traceback.format_exc()[-400:]}")
                return default
        return run
    return wrap


def _fresh(prompts, now: float) -> list:
    """The prompts written in the last 7 days (C4): an older one is never kept or shown, whenever it is read."""
    return [p for p in prompts or [] if now - (p.get("ts") or 0) <= KEEP_DAYS * 86400]


def _update(agent: str, sid: str, fn, now: float, create: bool = True):
    """fn(record) under the session's lock (the run's path names it), then an atomic write that drops the prompts
    older than 7 days (C4). StateBusy propagates: the caller drops its change whole (state audits it) and goes on."""
    def change(r):
        if fn(r) is state.SKIP:
            return state.SKIP
        cp = r.get("checkpoint")
        if isinstance(cp, dict) and cp.get("prompts"):
            cp["prompts"] = _fresh(cp["prompts"], now)
        r.update(v=1, agent=agent, session=sid, updated=round(now, 3))
    with state.hold(progress.run_path(agent, sid)):
        return state.update(record_path(agent, sid), change, create=create)


def _quietly(agent: str, sid: str, fn, now: float, create: bool = True):
    try:
        return _update(agent, sid, fn, now, create)
    except state.StateBusy as e:
        _log(f"{agent}:{sid[:12]} dropped: {e}")
        return None


def _session(agent: str, sid: str) -> dict:
    return state.read(progress.session_path(agent, sid)) or {}


def _live_run(agent: str, sid: str):
    try:
        run = state.read(progress.run_path(agent, sid))
        return progress.normalize(run) if run else None
    except Exception:
        return None


def _meta(r: dict, data: dict, sess: dict):
    r["cwd"] = data.get("cwd") or r.get("cwd") or sess.get("cwd") or ""
    r["project"] = sess.get("project") or r.get("project") or ""
    r["top"] = sess.get("top") or r.get("top") or ""


def _size_of(path):
    try:
        return os.path.getsize(path) if path else None
    except OSError:
        return None


def _epoch(iso):
    try:
        return dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- the reader (C12.1)

def _boundary(raw: bytes):
    try:
        d = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("type") != "system" or d.get("subtype") != "compact_boundary":
        return None
    meta = d.get("compactMetadata") if isinstance(d.get("compactMetadata"), dict) else {}

    def num(k):
        v = meta.get(k)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    return {"uuid": str(d.get("uuid") or "")[:64] or "h-" + hashlib.sha256(raw).hexdigest()[:16],
            "ts": _epoch(d.get("timestamp")), "trigger": str(meta.get("trigger") or "")[:16] or None,
            "pre_tokens": num("preTokens"), "post_tokens": num("postTokens"),
            "dropped_tokens": num("cumulativeDroppedTokens"), "duration_ms": num("durationMs"),
            "agent_id": str(d.get("agentId") or "")[:64] or None}


def scan(path, offset: int = 0, cap=SCAN_CAP) -> Scan:
    """The compaction boundaries in a transcript after `offset`: -> Scan(boundaries, offset, gap). It reads at most
    `cap` bytes (None: the whole file, for the dry run); past the cap it reads the last `cap` bytes and reports the
    gap. It stops at the last newline (a half-written line is read next time), starts again at 0 when the file is
    smaller than the offset (rewritten), and counts a line only when its top-level type is system and its subtype
    compact_boundary. A missing file finds nothing."""
    offset = int(offset or 0)
    size = _size_of(path)
    if size is None:
        return Scan([], offset, False)
    if size < offset:
        offset = 0
    start, gap = offset, False
    if cap is not None and size - start > cap:
        start, gap = size - cap, True
    found, end = [], start
    with open(path, "rb") as fh:
        skip = False
        if gap:  # mid-file: the first line is whole only when the byte before it ends a line
            fh.seek(start - 1)
            skip = fh.read(1) != b"\n"
        else:
            fh.seek(start)
        pos, rest = start, b""
        while pos < size:
            chunk = fh.read(min(SCAN_CAP, size - pos))
            if not chunk:
                break
            pos += len(chunk)
            data = rest + chunk
            cut = data.rfind(b"\n")
            if cut < 0:
                rest = data
                continue
            body, rest = data[:cut + 1], data[cut + 1:]
            if skip:
                body, skip = body[body.find(b"\n") + 1:], False
            if _NEEDLE in body:
                for ln in body.split(b"\n"):
                    if _NEEDLE in ln:
                        b = _boundary(ln)
                        if b:
                            found.append(b)
            end = pos - len(rest)
    return Scan(found, end, gap)


# --------------------------------------------------------------------------- compactions

def _pair_of(agent: str, data: dict):
    """-> (key, value): the id that pairs one compaction's hooks, prompt_id on Claude and turn_id on Codex."""
    key = "turn_id" if agent == "codex" else "prompt_id"
    return key, str(data.get(key) or data.get("prompt_id") or data.get("turn_id") or "")[:64] or None


def _new(r: dict, who: str, ts: float, trigger, key: str = "prompt_id", pair=None, seen=(), late=False,
         persona="", custom=False) -> dict:
    cs = r.setdefault("compactions", [])
    counts = r.setdefault("counts", {})
    counts[who] = counts.get(who, 0) + 1
    cid = f"c-{int(ts * 1000)}"
    while any(c.get("id") == cid for c in cs):
        cid += "x"
    c = {"id": cid, "ts": round(ts, 3), "trigger": trigger or "auto", "who": who, "persona": persona, "late": late,
         "seen": list(seen), key: pair, "boundary": None, "pre_tokens": None, "post_tokens": None,
         "dropped_tokens": None, "duration_ms": None, "custom": bool(custom), "closed": False, "n": counts[who]}
    cs.append(c)
    r["compactions"] = cs[-COMPACTIONS:]
    return c


def _join(r: dict, who: str, pair, now: float):
    """The open compaction a hook belongs to: the newest of `who`, not closed, under 15 minutes old, with the same
    prompt_id or turn_id (any, when the hook carries none)."""
    for c in reversed(r.get("compactions") or []):
        if c.get("who") != who or c.get("closed") or now - (c.get("ts") or 0) > PAIR_S:
            continue
        mine = c.get("prompt_id") or c.get("turn_id")
        if pair and mine and mine != pair:
            continue
        return c
    return None


def _seen(c: dict, via: str):
    if via and via not in c["seen"]:
        c["seen"].append(via)


def _fill(c: dict, b: dict):
    c["boundary"] = b["uuid"]
    for k in ("pre_tokens", "post_tokens", "dropped_tokens", "duration_ms"):
        if b.get(k) is not None:
            c[k] = b[k]
    if b.get("trigger") and (not c.get("trigger") or "pre" not in c["seen"]):
        c["trigger"] = b["trigger"]


def _counted(r: dict, b: dict) -> bool:
    return b["uuid"] in (r.get("counted") or []) or any(c.get("boundary") == b["uuid"]
                                                        for c in r.get("compactions") or [])


def _count(r: dict, b: dict):
    r["counted"] = ((r.get("counted") or []) + [b["uuid"]])[-COUNTED:]


def _attach(r: dict, b: dict, who: str, persona: str, now: float, via="boundary"):
    """Record boundary b of `who`: it joins the newest compaction of `who` with no boundary yet that started in the
    15 minutes before it (2 after), else it is a compaction of its own that no hook saw (late).
    -> (entry, created), or (None, False) when this boundary was counted already."""
    if _counted(r, b):
        return None, False
    bts = b.get("ts") or now
    entry = next((c for c in reversed(r.get("compactions") or []) if c.get("who") == who and not c.get("boundary")
                  and bts - PAIR_S <= (c.get("ts") or 0) <= bts + AFTER_S), None)
    created = entry is None
    if created:
        entry = _new(r, who, bts, b.get("trigger"), late=True, persona=persona)
    _fill(entry, b)
    _seen(entry, via)
    _count(r, b)
    return entry, created


def _summary(r: dict) -> dict:
    cs, counts = r.get("compactions") or [], r.get("counts") or {}
    last = cs[-1] if cs else None
    return {"count": counts.get("main", 0), "members": sum(v for k, v in counts.items() if k != "main"),
            "last": {k: last.get(k) for k in ("ts", "trigger", "who", "late")} if last else None}


def _k(n) -> str:
    return str(n) if n < 1000 else f"{round(n / 1000)}k"


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _desc(c: dict) -> str:
    tokens = (f"; {_k(c['pre_tokens'])} → {_k(c['post_tokens'])} tokens"
              if c.get("pre_tokens") is not None and c.get("post_tokens") is not None else "")
    return f"({c.get('trigger') or 'auto'}, {_ordinal(c.get('n') or 1)} time{tokens})"


def _announce(agent: str, sid: str, entries: list, r: dict):
    """After the lock: each compaction recorded for the first time is audited once, the session record gets the
    count, and the run gets the entry when progress has its recorder (the wiring step)."""
    if not entries:
        return
    r = r or {}
    for c in entries:
        tokens = (f"{_k(c['pre_tokens'])} → {_k(c['post_tokens'])} tokens" if c.get("pre_tokens") is not None
                  and c.get("post_tokens") is not None else "tokens not read yet")
        who = "main" if c["who"] == "main" else f"member {c.get('persona') or c['who'][:12]}"
        extra = {"project": r.get("project") or "", "session": f"{agent}:{sid[:12]}"}
        if c["who"] != "main":
            extra.update(agent_id=c["who"], member=c.get("persona") or "")
        audit.record("session.compact", r.get("project") or f"{agent}:{sid[:12]}",
                     f"{c.get('trigger')} · {who} · {tokens}", "late" if c.get("late") else "ok", **extra)
    try:
        summary = _summary(r)
        state.update(progress.session_path(agent, sid), lambda s: s.update(compactions=summary), create=False)
    except state.StateBusy:
        pass
    record = getattr(progress, "record_compaction", None)
    for c in entries if record else ():
        try:
            record(agent, sid, dict(c))
        except Exception:
            _log(f"record_compaction: {traceback.format_exc()[-300:]}")


# --------------------------------------------------------------------------- the checkpoint (C12.2)

def _clean(text: str) -> str:
    """Redacted, cut to 1,000 characters, redacted again: a cut never leaves part of a secret unmatched."""
    return audit.redact(audit.redact(text or "")[:PROMPT_MAX])


def _member(name):
    try:
        from . import crew
        return crew.member(name) if name else None
    except Exception:
        return None


def _who(name, role: str = "") -> str:
    """A crew member as developers read it, role first ("Backend engineer (Tank)"); from a checkpoint's role when
    the roster is unreadable. Fails open to the name."""
    name = str(name or "")
    try:
        from . import crew
        form = crew.who(name)
    except Exception:
        form = name
    return f"{role} ({name})" if form == name and role and name else form


def _on_step(x: dict) -> str:
    """A running lane for the carried text: "Backend engineer (Tank) on step api"."""
    return f"{_who(x.get('persona') or x.get('label') or 'a member', x.get('role'))} on step {x.get('step') or '-'}"


def _owner_name(run: dict, owner) -> str:
    if (owner or "main") == "main":
        return "main"
    lane = (run.get("agents") or {}).get(owner) or {}
    return lane.get("persona") or progress.persona(lane.get("type"), lane.get("label")) or "subagent"


def _current(plan: list) -> str:
    running = [s for s in plan if s.get("state") == "running"]
    mine = [s for s in running if (s.get("owner") or "main") == "main"]
    if mine or running:
        return (mine or running)[0].get("name") or ""
    nxt = _next_steps(plan)
    return nxt[0] if nxt else ""


def _next_steps(plan: list) -> list:
    state_of = {s.get("name"): s.get("state") for s in plan}
    return [s.get("name") for s in plan if s.get("state") == "pending"
            and all(state_of.get(a) in _DONE for a in s.get("after") or [] if a in state_of)]


def _lane_step(run: dict, aid: str) -> str:
    return next((s.get("name") for s in run.get("plan") or [] if s.get("owner") == aid
                 and s.get("state") not in _DONE), "")


def _lane_scope(run: dict, aid: str) -> list:
    try:
        return sorted({g for layer in progress.lane_scope(run, aid) for g in layer})[:8]
    except Exception:
        return []


def _live(run: dict, now=None) -> dict:
    """progress.lane_states of the run: who of the members it lists as running still works, went stale or ended. {}
    when that cannot be read: every such member then counts as working, as before."""
    try:
        return progress.lane_states(run, now)
    except Exception:
        return {}


def _lanes(run: dict, now=None, states=None) -> list:
    """The members the run lists as running that still work or went stale (progress.lane_states), each with its
    `state` (working or stale) and `why`. One that ended is left out: _ended_lanes names it."""
    states = _live(run, now) if states is None else states
    out = []
    for aid, lane in progress.running_agents(run):
        lv = states.get(aid) or {"state": "working", "why": ""}
        if lv["state"] == "ended":
            continue
        persona = lane.get("persona") or progress.persona(lane.get("type"), lane.get("label")) or ""
        m = _member(persona) or {}
        out.append({"agent_id": aid, "persona": persona, "role": m.get("title") or "", "step": _lane_step(run, aid),
                    "scope": _lane_scope(run, aid), "label": (lane.get("label") or "")[:80],
                    "started": lane.get("started"), "binding": lane.get("binding") or "", "state": lv["state"],
                    "why": (lv.get("why") or "")[:160]})
    return out[:LANES]


def _ended_lanes(run: dict, now=None, states=None) -> list:
    """The members the run lists as running that ended without their run hearing it (progress.lane_states), for
    the "Ended since" line: [{persona, role, step}]."""
    states = _live(run, now) if states is None else states
    out = []
    for aid, lane in progress.running_agents(run):
        if (states.get(aid) or {}).get("state") != "ended":
            continue
        persona = lane.get("persona") or progress.persona(lane.get("type"), lane.get("label")) or ""
        out.append({"persona": persona, "role": (_member(persona) or {}).get("title") or "",
                    "label": (lane.get("label") or "")[:80], "step": _lane_step(run, aid)})
    return out[:LANES]


def _decisions(run: dict) -> list:
    return [{"id": d.get("id"), "from_name": d.get("from_name") or "", "question": (d.get("question") or "")[:800],
             "options": list(d.get("options") or [])[:6], "recommend": d.get("recommend") or ""}
            for d in progress.pending_decisions(run)][-DECISIONS:]


def _open_messages(run: dict) -> list:
    """The questions to the tech lead nobody has answered yet (progress.waiting_messages): never a notice, nor a
    question a reply answers."""
    return progress.waiting_messages(run, "main")


def _messages(run: dict) -> list:
    return [{"id": m.get("id"), "from_name": m.get("from_name") or "", "to": m.get("to_name") or m.get("to") or "",
             "text": (m.get("text") or "")[:1500]} for m in _open_messages(run)][-MESSAGES:]


def _run_part(run: dict) -> dict:
    plan = run.get("plan") or []
    return {"goal": (run.get("goal") or "")[:200], "state": run.get("state"), "mode": run.get("mode") or "guided",
            "current": _current(plan),
            "steps": [{"name": s.get("name"), "state": s.get("state"), "owner": _owner_name(run, s.get("owner")),
                       "after": list(s.get("after") or [])[:6], "activity": s.get("activity") or ""} for s in plan]}


def _git(cwd, sess: dict) -> dict:
    """{branch, head, dirty, unpushed}: four git calls (never under a lock); the session record's branch and HEAD
    when the folder is no repository."""
    g = {"branch": sess.get("branch") or "", "head": (sess.get("head") or "")[:12], "dirty": None, "unpushed": None}
    if not cwd or not os.path.isdir(cwd):
        return g
    head = core.gitout(cwd, "rev-parse", "--short", "HEAD", timeout=5)
    if not head:
        return g
    g.update(head=head, branch=core.gitout(cwd, "branch", "--show-current", timeout=5) or g["branch"])
    g["dirty"] = len([x for x in core.gitout(cwd, "status", "--porcelain", timeout=5).splitlines() if x.strip()])
    up = core.gitout(cwd, "rev-list", "--count", "@{u}..HEAD", timeout=5)
    g["unpushed"] = int(up) if up.isdigit() else None
    return g


def _tasks(agent: str, sid: str, project: str) -> list:
    """The handoff tasks this session claimed (read at Stop only; PreCompact reuses the last ones)."""
    if not project:
        return []
    try:
        from . import work
        tail, out = f"({agent}:{sid[:12]})", []
        for i in work.all_items(project):
            m = i.get("meta") or {}
            if (m.get("claimed_by") or "").endswith(tail) and m.get("status") not in ("done", "cancelled") \
                    and not work.lock_holder(i)[1]:
                out.append({"id": m.get("id"), "title": (m.get("title") or "")[:120]})
        return out[:8]
    except Exception:
        return []


def _build(reason: str, now: float, cwd: str, sess: dict, run, git: dict, tasks: list, prompts: list) -> dict:
    return {"v": 1, "reason": reason, "ts": round(now, 3), "cwd": cwd, "top": sess.get("top") or "",
            "project": sess.get("project") or "", "run": _run_part(run) if run else None,
            "lanes": _lanes(run, now) if run else [], "decisions": _decisions(run) if run else [],
            "messages": _messages(run) if run else [], "tasks": tasks or [],
            "status": (sess.get("status") or "")[:STATUS_MAX], "git": git, "prompts": prompts or []}


def _fits(cp) -> bool:
    return len(json.dumps(cp).encode()) <= CHECKPOINT_MAX


def _cut(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n - 1] + "…"


def _fit(cp: dict) -> dict:
    """The checkpoint within 16 KB as written, by the fixed drop order (spec "Checkpoint"); the newest prompt is
    never cut below 1,000 characters. Past the order, older prompts go oldest first, then crew texts and steps, so
    the bound holds for any input."""
    run = cp.get("run") or {}
    prompts = cp.get("prompts") or []

    def done_to_count():
        steps = run.get("steps") or []
        done = [s for s in steps if s.get("state") in _DONE]
        if done:
            run["steps"] = [s for s in steps if s.get("state") not in _DONE]
            run["done"] = run.get("done", 0) + len(done)

    def cut_messages():
        for m in cp.get("messages") or []:
            m["text"] = _cut(m.get("text"), 120)

    def cut_questions():
        for d in cp.get("decisions") or []:
            d["question"] = _cut(d.get("question"), 160)

    def status_header():
        cp["status"] = (cp.get("status") or "").split("\n", 1)[0]

    def cap_lists():
        cp["lanes"] = (cp.get("lanes") or [])[:LANES]
        if run.get("steps"):
            run["steps"] = run["steps"][:STEPS]

    def drop_older():
        while len(prompts) > 1 and not _fits(cp):
            prompts.pop(0)

    def bare():
        for m in cp.get("messages") or []:
            m["text"] = ""
        for d in cp.get("decisions") or []:
            d.update(question="", options=[])
        run["steps"] = []
        cp["status"] = ""

    order = [done_to_count, cut_messages, cut_questions, status_header]
    order += [functools.partial(lambda p: p.update(text=_cut(p.get("text"), 300)), p) for p in prompts[:-1]]
    order += [cap_lists, drop_older, bare]
    for step in order:
        if _fits(cp):
            break
        step()
    return cp


def _checkpoint(agent, sid, reason, now, data, tasks=None, keep=None):
    """-> fn(record) that writes the checkpoint: the live records and git read now (outside the lock), the prompts
    as the record holds them when the lock is taken. keep(prompts) decides which prompts stay."""
    sess = _session(agent, sid)
    cwd = data.get("cwd") or sess.get("cwd") or ""
    git = _git(sess.get("top") or cwd, sess)
    run = _live_run(agent, sid)

    def write(r):
        prev = r.get("checkpoint") or {}
        prompts = [dict(p) for p in prev.get("prompts") or []]
        cp = _build(reason, now, cwd, sess, run, git, prev.get("tasks") if tasks is None else tasks,
                    keep(prompts) if keep else prompts)
        r["checkpoint"] = _fit(cp)
        _meta(r, data, sess)
    return write


# --------------------------------------------------------------------------- "Not in the summary" (C12.3, C13)

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return re.sub(r"\s+", " ", s).strip()


def _summary_body(summary: str) -> str:
    s = re.sub(r"(?is)<analysis>.*?(?:</analysis>|(?=<summary>)|\Z)", " ", summary or "")
    m = re.search(r"(?is)<summary>(.*?)(?:</summary>|\Z)", s)
    return m.group(1) if m else s


_CLOSE = {"「": "」", "『": "』"}


def _quoted(text: str) -> list:
    """Spans between an opening and a closing quote: ASCII double quotes, any Unicode initial/final quote pair
    (“…”, «…», ‘…’, ‹…›), and 「…」 『…』."""
    out = [m.group(1) for m in re.finditer(r'"([^"\n]{1,200})"', text)]
    i = 0
    while i < len(text):
        ch = text[i]
        if unicodedata.category(ch) == "Pi" or ch in _CLOSE:
            close = _CLOSE.get(ch)
            j = i + 1
            while j < len(text) and j - i <= 201 and not (text[j] == close if close else
                                                          unicodedata.category(text[j]) == "Pf"):
                j += 1
            if j < len(text) and j - i <= 201 and j > i + 1:
                out.append(text[i + 1:j])
                i = j
        i += 1
    return out


def _strip(token: str) -> str:
    edge = lambda c: unicodedata.category(c).startswith("P") or c == "`"  # noqa: E731
    a, b = 0, len(token)
    while a < b and edge(token[a]):
        a += 1
    while b > a and edge(token[b - 1]):
        b -= 1
    return token[a:b]


def _identifier(tok: str) -> bool:
    if any(c.isdigit() for c in tok):
        return True
    if any(c in "_-/" for c in tok[1:-1]):
        return True
    return any(a.islower() and b.isupper() for a, b in zip(tok, tok[1:]))


def anchors(text: str) -> list:
    """A request's structural anchors, in order and without repeats: code spans, quoted spans, URLs and paths,
    tokens with a digit, identifier-shaped tokens (an inner _ - /, or a lower-to-upper change of case). No word
    lists: the same rules for every language."""
    out, seen = [], set()
    cands = [m.group(1) for m in re.finditer(r"`([^`\n]{1,200})`", text)] + _quoted(text)
    cands += [_strip(m.group(0)) for m in re.finditer(r"https?://\S+", text)]
    cands += [t for t in (_strip(x) for x in text.split()) if len(t) > 1 and _identifier(t)]
    for c in cands:
        n = _norm(c)
        if n and n not in seen:
            seen.add(n)
            out.append(c.strip())
    return out


def _shingles(s: str) -> set:
    s = re.sub(r"\s+", "", s)
    if len(s) <= SHINGLE:
        return {s} if s else set()
    return {s[i:i + SHINGLE] for i in range(len(s) - SHINGLE + 1)}


def _excerpt(text: str, n: int = 120) -> str:
    return _cut(re.sub(r"\s+", " ", text or "").strip(), n)


def _has_id(body: str, ident: str) -> bool:
    n = _norm(ident)
    return bool(n) and re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", body) is not None


def _gaps(summary: str, items: list) -> list:
    """[(item, index of the first anchor it misses, or None)] for the items the summary leaves out (`missing`)."""
    body = _norm(_summary_body(summary))
    have = _shingles(body)
    out = []
    for it in items or []:
        if "text" in it:
            text = it.get("text") or ""
            gone = next((i for i, a in enumerate(anchors(text)) if _norm(a) not in body), None)
            sh = _shingles(_norm(text))
            share = len(sh & have) / len(sh) if sh else 1.0
            if gone is not None or share < PRESENT:
                out.append((it, gone))
        elif not any(_has_id(body, i) for i in it.get("ids") or []):
            out.append((it, None))
    return out


def _request_label(label: str, text: str, gone) -> str:
    found = anchors(text) if gone is not None else []
    return f"{label} «{_excerpt(text)}»" + (f" (missing `{found[gone]}`)" if gone is not None and
                                              0 <= gone < len(found) else "")


def missing(summary: str, items: list) -> list:
    """The items the summary leaves out, as display labels. An item is {"label", "ids": [...]} (present when one
    id appears exactly, as a whole word) or {"label", "text"} (a request: present when every anchor appears and at
    least 60% of its 4-character shingles do). Text is NFKC, casefolded, whitespace collapsed; only the <summary>
    body counts, never the <analysis>. Pure: no vocabulary, so it works the same in every language."""
    return [_request_label(it["label"], it.get("text") or "", gone) if "text" in it else it["label"]
            for it, gone in _gaps(summary, items)]


def _refs(summary: str, items: list) -> list:
    """What the record keeps of the items the summary leaves out (C1): a request by its prompt_id and time and the
    index of the anchor it misses, never its text (that stays in checkpoint.prompts); any other item by its label."""
    return [{"request": it.get("prompt_id") or "", "ts": it.get("ts"), "anchor": gone} if "text" in it
            else {"label": it["label"]} for it, gone in _gaps(summary, items)]


def _missing_now(rec: dict, refs: list, now: float) -> str:
    """The "Not in the summary" text, built at delivery from the prompts the checkpoint still holds: a request
    dropped since (not development work, /clear, 7 days) is left out, text and line."""
    prompts = _fresh((rec.get("checkpoint") or {}).get("prompts"), now)
    labels = []
    for ref in refs or []:
        if "label" in ref:
            labels.append(ref["label"])
            continue
        p = next((x for x in prompts if x.get("ts") == ref.get("ts") and
                  (x.get("prompt_id") or "") == (ref.get("request") or "")), None)
        if p:
            when = time.strftime("%H:%M", time.localtime(p.get("ts") or 0))
            labels.append(_request_label(f"request of {when}", p.get("text") or "", ref.get("anchor")))
    return _missing_text(labels)


def _items(rec: dict, run, now=None) -> list:
    """What a summary must keep, live first: the members that still work or went stale, the developer's requests,
    the decisions, the messages and tasks waiting, and the current step."""
    cp = rec.get("checkpoint") or {}
    items = []
    lanes = _lanes(run, now) if run else cp.get("lanes") or []
    for x in lanes:
        m = _member(x.get("persona")) or {}
        ids = [x.get("persona"), m.get("name"), m.get("id"), re.sub(r"^The ", "", m.get("name") or "")]
        items.append({"label": _on_step(x), "ids": [i for i in ids if i]})
    for p in cp.get("prompts") or []:
        items.append({"label": f"request of {time.strftime('%H:%M', time.localtime(p.get('ts') or 0))}",
                      "text": p.get("text") or "", "prompt_id": p.get("prompt_id") or "", "ts": p.get("ts")})
    decs = _decisions(run) if run else cp.get("decisions") or []
    msgs = _messages(run) if run else cp.get("messages") or []
    items += [{"label": f"decision {d['id']}", "ids": [d["id"]]} for d in decs if d.get("id")]
    items += [{"label": f"message {m['id']}", "ids": [m["id"]]} for m in msgs if m.get("id")]
    items += [{"label": f"task {t['id']}", "ids": [t["id"]]} for t in cp.get("tasks") or [] if t.get("id")]
    cur = _current(run.get("plan") or []) if run else (cp.get("run") or {}).get("current")
    if cur:
        items.append({"label": f"step {cur}", "ids": [cur]})
    return items


def _missing_text(labels: list) -> str:
    if not labels:
        return ""
    head = "devkit · Not in the summary: re-read these from devkit's record: "
    out = head
    for i, lab in enumerate(labels):
        part = ("" if i == 0 else " · ") + lab
        if len((out + part).encode()) > MISSING_MAX - 8:
            return out + " · …"
        out += part
    return out


# --------------------------------------------------------------------------- the continuity brief (C12.3)

def _bytes(s: str) -> int:
    return len(s.encode())


def _cut_bytes(s: str, n: int) -> str:
    b = s.encode()
    if len(b) <= n:
        return s
    return b[:max(0, n - 3)].decode("utf-8", "ignore") + "…"


def _quote(text: str, n: int) -> str:
    """Text that others wrote, shown as data: one line, cut, between « »."""
    return "«" + _excerpt(text, n).replace("«", "‹").replace("»", "›") + "»"


def _prov(name):
    return _PROVIDER.get(str(name or "").lower())


def _shows(agent: str, provider, chat: bool) -> bool:
    """May this reader see the session's free text (requests, decision questions, crew messages)? `provider` None
    is the session itself, unless the reader is a chat surface, which must name its provider; otherwise only the
    session's own provider, and an unknown one never."""
    if provider is None:
        return not chat
    p = _prov(provider)
    return p is not None and p == _prov(agent)


def _header(rec: dict, reason: str, agent: str, sid: str) -> str:
    c = next((x for x in reversed(rec.get("compactions") or []) if x.get("who") == "main"), None)
    d = (" " + _desc(c)) if c else ""
    if reason == "late":
        return (f"devkit · this conversation was compacted{d} and no devkit hook saw it in time. devkit's record "
                "wins over the summary:")
    if reason == "resume":
        return (f"devkit · this session was resumed{' after a compaction' + d if c else ''}. devkit's record wins "
                "over what you remember:")
    if reason == "manual":
        return (f"devkit · continuity brief for {agent}:{sid[:12]}{', last compaction' + d if c else ''}. "
                "devkit's record wins over a summary:")
    return f"devkit · this conversation was compacted{d}. devkit's record wins over the summary:"


def brief(agent: str, sid: str, reason: str = "manual", chat: bool = False, now=None, provider=None,
          caller=None) -> str:
    """The continuity brief, at most 4 KB: header, latest requests (newest first, verbatim), run, crew working now,
    waiting, last status block, git and the rule. The run, lanes, decisions, messages and git come from the live
    records; the checkpoint gives the prompts and the status and stands in for a live record it cannot read.
    Request, decision and message text is shown only to the session's own provider (`_shows`; `provider` None: the
    session itself), a pending request only to the session itself, and a request older than 7 days to nobody;
    another reader gets the count and times, ids and senders. `chat` cuts each request to 200 characters. `caller`:
    the (agent, session id) that reads it (None: the session itself); a reader other than the session sees each
    decision and message id with the session it belongs to (progress.shown_id). "" when there is nothing to carry
    over."""
    now = now or time.time()
    rec = read(agent, sid) or {}
    cp = rec.get("checkpoint") or {}
    run = _live_run(agent, sid)
    itself = provider is None and not chat
    prompts = [p for p in _fresh(cp.get("prompts"), now) if itself or not p.get("pending")]
    if not run and not cp.get("run") and not prompts:
        return ""
    sess = _session(agent, sid)
    show = _shows(agent, provider, chat)
    # live first, the checkpoint for what it cannot read
    if run:
        plan = run.get("plan") or []
        goal, rstate, mode = run.get("goal") or "", run.get("state"), run.get("mode") or "guided"
        cur, nxt = _current(plan), _next_steps(plan)
        done = len([s for s in plan if s.get("state") in _DONE])
        states = _live(run, now)
        lanes = [dict(x, minutes=int(max(0, now - (x.get("started") or now)) / 60)) for x in _lanes(run, now, states)]
        ended = _ended_lanes(run, now, states)
        decs = [{"id": d["id"], "from_name": d.get("from_name") or "", "question": d.get("question") or "",
                 "options": d.get("options") or [], "recommend": d.get("recommend") or ""}
                for d in progress.pending_decisions(run)]
        msgs = [{"id": m.get("id"), "from_name": m.get("from_name") or "", "text": m.get("text") or ""}
                for m in _open_messages(run)]
    else:
        r = cp.get("run") or {}
        steps = r.get("steps") or []
        goal, rstate, mode, cur = r.get("goal") or "", r.get("state"), r.get("mode") or "guided", r.get("current")
        nxt = _next_steps(steps)
        done = r.get("done", 0) + len([s for s in steps if s.get("state") in _DONE])
        lanes = [dict(x, minutes=int(max(0, now - (x.get("started") or now)) / 60)) for x in cp.get("lanes") or []]
        ended = []
        decs, msgs = cp.get("decisions") or [], cp.get("messages") or []
    me, reader = {"agent": agent, "session": sid}, caller or (agent, sid)
    decs = [dict(d, id=progress.shown_id(me, d.get("id"), reader)) for d in decs]
    msgs = [dict(m, id=progress.shown_id(me, m.get("id"), reader)) for m in msgs]
    git = _git(sess.get("top") or rec.get("cwd") or "", sess)
    if not git.get("branch") and cp.get("git"):
        git = cp["git"]
    status = (cp.get("status") or sess.get("status") or "").split("\n", 1)[0]
    nxt = [n for n in nxt if n != cur][:3]
    relaunch = "send_input" if agent == "codex" else "SendMessage"
    newest_first = prompts[::-1]

    def render(o) -> str:
        out = [_header(rec, reason, agent, sid)]
        if newest_first and show:
            note = "newest first; requests redacted and cut" if chat else "newest first, verbatim"
            out.append(f"Latest requests ({note}):")
            for i, p in enumerate(newest_first):
                if i and i > len(newest_first) - 1 - o["drop"]:
                    continue
                text = p.get("text") or ""
                if chat:
                    text = _cut(text, 200)
                elif i and o["older"]:
                    text = _cut(text, o["older"])
                elif not i and o["cap"]:
                    text = _cut_bytes(text[:PROMPT_MAX], 2000)
                when = time.strftime("%H:%M", time.localtime(p.get("ts") or now))
                out.append(f"- {when} " + text.replace("\n", "\n    "))
        elif newest_first:
            times = ", ".join(time.strftime("%H:%M", time.localtime(p.get("ts") or now)) for p in newest_first)
            out.append(f"Latest requests: {len(newest_first)} requests ({times}); their text stays with the "
                       "session's own provider.")
        if goal or cur:
            line = f"Run «{_excerpt(goal, 120)}» ({rstate or 'running'}, {mode} mode): current step {cur or '-'}"
            if o["next"] and nxt:
                line += f"; next: {', '.join(nxt)}"
            out.append(line + (f"; {done} step{'s' if done != 1 else ''} done." if done else "."))
        if lanes:
            out.append("Crew working now:")
            for x in lanes:
                stale = f" · stale: {x.get('why') or 'no recent activity'}" if x.get("state") == "stale" else ""
                out.append(f"- {_who(x.get('persona') or x.get('label') or 'a member', x.get('role'))} · step "
                           f"{x.get('step') or '-'} · {x['minutes']} min{stale}")
            if any(x.get("state", "working") == "working" for x in lanes):
                out.append(f"Their results arrive as notifications: do not relaunch them; {relaunch} continues one.")
        if ended:
            out.append("Ended since, with no report in the run: " + ", ".join(_on_step(x) for x in ended)
                       + ". Check their work, then close or reopen their steps.")
        if decs:
            parts = []
            for d in decs[-IDS:]:
                s = f"decision {d['id']} from {_who(d.get('from_name')) or '?'}"
                if o["texts"]:
                    opts = " | ".join(d.get("options") or [])
                    s += f" {_quote(d.get('question'), 200)}" + (f" ({opts}" + (
                        f"; recommended {d['recommend']}" if d.get("recommend") else "") + ")" if opts else "")
                parts.append(s)
            out.append("Waiting for the developer: " + "; ".join(parts) + ".")
        if msgs:
            parts = [f"{m['id']} from {_who(m.get('from_name')) or '?'}"
                     + (f" {_quote(m.get('text'), 160)}" if o["texts"] else "") for m in msgs[-IDS:]]
            more = f" (+{len(msgs) - IDS} older)" if len(msgs) > IDS else ""
            out.append("Crew messages waiting: " + "; ".join(parts) + more + ".")
        if status and o["status"]:
            out.append(f"Last status block (keep its task title): {status[:300]}")
        if o["git"]:
            g = [f"branch {git.get('branch') or '-'}"] + ([f"HEAD {git['head']}"] if git.get("head") else [])
            g += [f"{git['dirty']} uncommitted"] if git.get("dirty") is not None else []
            g += [f"{git['unpushed']} unpushed"] if git.get("unpushed") is not None else []
            out.append("Git: " + " · ".join(g) + ".")
        else:
            out.append(f"Git: branch {git.get('branch') or '-'}.")
        out.append("Rule: your final message starts with the status block.")
        return "\n".join(out)

    o = {"texts": show, "status": True, "next": True, "git": True, "older": None, "drop": 0, "cap": False}
    steps = [("texts", False), ("status", False), ("next", False), ("git", False), ("older", 200)]
    steps += [("drop", i) for i in range(1, len(newest_first))] + [("cap", True)]
    text = render(o)
    for k, v in steps:
        if _bytes(text) <= BRIEF_MAX:
            break
        o[k] = v
        text = render(o)
    return _cut_bytes(text, BRIEF_MAX)


def _notice(agent: str, old_sid: str, now: float) -> str:
    """After /clear: the old session's open run, with the commands to continue it here or close it. No prompts:
    the developer cleared the conversation."""
    run = _live_run(agent, old_sid)
    if not run or run.get("state") not in ("running", "blocked"):
        return ""
    plan = run.get("plan") or []
    lanes = ", ".join(_on_step(x) for x in _lanes(run, now) if x["state"] == "working")
    out = [f"devkit · you cleared the conversation. The previous session ({agent}:{old_sid[:12]}) has an open run "
           f"«{_excerpt(run.get('goal'), 100)}», current step {_current(plan) or '-'}."]
    if lanes:
        out.append(f"Running: {lanes}; their results go to that session, and carry waits until they report.")
    out.append("To continue it here: `dev session carry`. To close it: `dev session carry`, then "
               "`dev progress done \"<outcome>\" --why \"…\"`.")
    return _cut_bytes(" ".join(out), NOTICE_MAX)


def _brief_head(path: str) -> str:
    """The first 800 characters of a member's launch brief: the first user entry of its transcript (the head, at
    most 64 KB), redacted."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(HEAD_CAP)
    except (OSError, TypeError):
        return ""
    for raw in head.split(b"\n"):
        try:
            d = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(d, dict) or d.get("type") != "user":
            continue
        content = (d.get("message") or {}).get("content")
        if isinstance(content, list):
            content = " ".join(b.get("text") or "" for b in content if isinstance(b, dict))
        if isinstance(content, str) and content.strip():
            return _excerpt(audit.redact(content), 800)
    return ""


def _reminder(agent: str, sid: str, aid: str, path: str) -> str:
    """A member's reminder after its own compaction, at most 1.5 KB: role and name, step and scope, the procedure
    and where it was, the head of its launch brief (as data), and the report format."""
    run = _live_run(agent, sid) or {}
    lane = (run.get("agents") or {}).get(aid) or {}
    persona = lane.get("persona") or progress.persona(lane.get("type"), lane.get("label")) or ""
    m = _member(persona) or {}
    name, title = m.get("name") or persona or "a crew member", m.get("title") or ""
    step, scope = _lane_step(run, aid), _lane_scope(run, aid)
    phases = [p.get("phase") for p in m.get("procedure") or [] if p.get("phase")]
    last = next((u.get("text") for u in reversed(lane.get("updates") or []) if u.get("text")), "")
    sess = _session(agent, sid)
    try:
        from . import crew
        header = crew.status_header(m, project=sess.get("project") or "<project>",
                                    branch=sess.get("branch") or "<branch>", task=step or "<task>") if m else ""
    except Exception:
        header = ""
    header = header or "**[Firefly> DevKit · <role> (<name>)]** <project> · `<branch>` · <task>"
    lines = ["devkit · your own context was compacted. You are " + (f"the crew's {title} ({name})" if title else name)
             + ", working for the tech lead. devkit's record of your lane:"]
    if step or scope:
        lines.append(f"- step {step or '-'}" + (f" · scope {', '.join(scope)}" if scope else ""))
    if phases or last:
        lines.append(("- your procedure: " + " → ".join(phases) if phases else "- your procedure: -")
                     + (f"; where you were: {_quote(last, 200)}" if last else ""))
    tail = (f"- your report starts with {header}, then - **Done:**, - **Check:**, - **Next:**, and ends with "
            "Needs from the tech lead: <what you need, or nothing>.")
    head = _brief_head(path)
    if head:
        room = REMINDER_MAX - _bytes("\n".join(lines + [tail])) - 40
        lines.append("- your launch brief began: " + _quote(_cut_bytes(head, max(0, room)), 800))
    return _cut_bytes("\n".join(lines + [tail]), REMINDER_MAX)


def _instructions(run, git: dict, now=None) -> str:
    """PreCompact's text for Claude's summarizer (main session): what to keep, by id. No request text."""
    lines = ["devkit · keep these in the summary, verbatim; devkit checks the summary against its record after "
             "the compaction:",
             "- every request of the developer's that is still open, word for word, with its paths, ids, versions "
             "and quoted text;"]
    if run:
        plan = run.get("plan") or []
        cur, nxt = _current(plan), [n for n in _next_steps(plan) if n != _current(plan)][:3]
        lines.append(f"- the run «{_excerpt(run.get('goal'), 120)}»: current step {cur or '-'}"
                     + (f"; next: {', '.join(nxt)}" if nxt else "") + ";")
        lanes = _lanes(run, now)
        if lanes:
            works = any(x["state"] == "working" for x in lanes)
            lines.append("- the crew working now: " + ", ".join(
                _on_step(x) + (" (stale)" if x["state"] == "stale" else "") for x in lanes)
                + (". They are still running: their results arrive as notifications;" if works else ";"))
        decs = [d["id"] for d in progress.pending_decisions(run)][-IDS:]
        msgs = [m.get("id") for m in _open_messages(run)][-IDS:]
        if decs or msgs:
            lines.append("- waiting: " + "; ".join(x for x in (
                "decisions " + ", ".join(decs) if decs else "", "crew messages " + ", ".join(msgs) if msgs else "")
                if x) + ";")
    if git.get("branch"):
        lines.append(f"- the branch {git['branch']}" + (f" (HEAD {git['head']})" if git.get("head") else "")
                     + " and the release state;")
    lines.append("- the rule: every final message starts with the status block.")
    return _cut("\n".join(lines), INSTRUCTIONS_MAX)


# --------------------------------------------------------------------------- detection and delivery

def _detect_and_take(agent: str, sid: str, who: str, path: str, now: float, persona: str = "") -> list:
    """Read `who`'s transcript after its offset, record each new boundary, and take what waits for `who` (each
    once). -> the deliveries [{kind, who, for, text?}]. The lock is taken only when there is something to write."""
    rec = read(agent, sid) or {}
    saved = (rec.get("offsets") or {}).get(who)
    waiting = [d for d in rec.get("deliver") or [] if d.get("who") == who]
    got, first = None, None
    if path and agent == "claude":
        if saved is None and who == "main":
            first = _size_of(path)  # first sight of the main transcript: start at its end
        else:
            got = scan(path, saved or 0)
    moved = got and (got.offset - (saved or 0) >= MOVE_SAVE or got.offset < (saved or 0))  # far, or rewritten
    if not waiting and first is None and not (got and got.boundaries) and not moved:
        return []
    fresh, taken = [], []

    def change(r):
        offs = r.setdefault("offsets", {})
        if first is not None:
            offs.setdefault(who, first)
        if got:
            for b in got.boundaries:
                if who == "main" and b.get("agent_id"):
                    continue
                entry, created = _attach(r, b, who, persona, now)
                if not entry:
                    continue
                if created:
                    fresh.append(entry)
                if who != "main" or "start" not in entry["seen"]:
                    kind = "reminder" if who != "main" else "brief"
                    r.setdefault("deliver", []).append({"kind": kind, "who": who, "for": entry["id"]})
            offs[who] = got.offset
        keep = []
        for d in r.get("deliver") or []:
            (taken if d.get("who") == who else keep).append(d)
        r["deliver"] = keep[-20:]
    r = _update(agent, sid, change, now)
    _announce(agent, sid, fresh, r)
    return taken


def _render(agent: str, sid: str, taken: list, path: str, now: float) -> str:
    out, kinds, rec = [], set(), None
    for d in taken:
        k = d.get("kind")
        if k in kinds and k != "missing":
            continue  # one brief or reminder renders everything there is to know
        kinds.add(k)
        if k == "brief":
            out.append(brief(agent, sid, reason="late", now=now))
        elif k == "reminder":
            out.append(_reminder(agent, sid, d["who"], path))
        elif k == "missing" and d.get("items"):
            rec = rec if rec is not None else (read(agent, sid) or {})
            out.append(_missing_now(rec, d["items"], now))
    return "\n\n".join(x for x in out if x)


# --------------------------------------------------------------------------- hook entry points

@_fail_open("")
def on_pre_compact(agent: str, data: dict, now=None) -> str:
    """PreCompact. Main session: write the checkpoint (the last tasks: nothing reads the handoff repository during a
    compaction) and open the compaction; Claude: return the summarizer's instructions. A member: open its
    compaction, print nothing (its stdout is ignored). Never blocks: the hook always exits 0."""
    now = now or time.time()
    sid = data.get("session_id") or ""
    if not sid:
        return ""
    key, pair = _pair_of(agent, data)
    trigger = str(data.get("trigger") or "auto")[:16]
    aid = data.get("agent_id")
    fresh = []
    if aid:
        persona = _owner_name(_live_run(agent, sid) or {}, aid)

        def member(r):
            c = _join(r, aid, pair, now)
            if c:
                _seen(c, "pre")
            else:
                fresh.append(_new(r, aid, now, trigger, key, pair, ["pre"], persona=persona))
        _announce(agent, sid, fresh, _quietly(agent, sid, member, now))
        return ""
    write = _checkpoint(agent, sid, "pre-compact", now, data)

    def main(r):
        write(r)
        c = _join(r, "main", pair, now)
        if c:
            _seen(c, "pre")
        else:
            fresh.append(_new(r, "main", now, trigger, key, pair, ["pre"],
                              custom=bool((data.get("custom_instructions") or "").strip())))
    r = _quietly(agent, sid, main, now)
    _announce(agent, sid, fresh, r)
    if agent != "claude":
        return ""
    sess = _session(agent, sid)
    cp = (r or read(agent, sid) or {}).get("checkpoint") or {}
    return _instructions(_live_run(agent, sid), cp.get("git") or _git("", sess), now)


@_fail_open("")
def on_session_start(agent: str, data: dict, now=None) -> str:
    """SessionStart, before the workspace context. startup and fork: the main offset is the transcript's size.
    compact: join or record the compaction with the boundary's tokens, then the brief. resume: re-seed the session
    record's count, then the brief. clear: link the old session by the clear link and give the open-run notice."""
    now = now or time.time()
    sid = data.get("session_id") or ""
    src = str(data.get("source") or "startup").lower()
    path = data.get("transcript_path")
    if not sid:
        return ""
    sess = _session(agent, sid)
    if src == "compact":
        key, pair = _pair_of(agent, data)
        rec = read(agent, sid) or {}
        saved = (rec.get("offsets") or {}).get("main")
        got = scan(path, saved if saved is not None else max(0, (_size_of(path) or 0) - MOVE_SAVE)) \
            if path and agent == "claude" else None
        bounds = [b for b in (got.boundaries if got else []) if not b.get("agent_id")]
        fresh = []

        def change(r):
            c = _join(r, "main", pair, now)
            newest = bounds[-1] if bounds and not _counted(r, bounds[-1]) else None
            if c:
                _seen(c, "start")
            else:
                c = _new(r, "main", now, (newest or {}).get("trigger"), key, pair, ["start"])
                fresh.append(c)
            if newest and not c.get("boundary"):
                _fill(c, newest)
                _count(r, newest)
            for b in bounds[:-1]:  # older boundaries: compactions no hook saw
                e, created = _attach(r, b, "main", "", now)
                if created:
                    fresh.append(e)
            if got:
                r.setdefault("offsets", {})["main"] = got.offset
            _meta(r, data, sess)
        _announce(agent, sid, fresh, _quietly(agent, sid, change, now))
        return brief(agent, sid, reason="compact", now=now)
    if src == "resume":
        rec = read(agent, sid)
        if not rec:
            size = _size_of(path)
            _quietly(agent, sid, lambda r: (r.setdefault("offsets", {}).setdefault("main", size or 0),
                                            _meta(r, data, sess)), now)
            return ""
        if rec.get("counts"):
            try:
                summary = _summary(rec)
                state.update(progress.session_path(agent, sid), lambda s: s.update(compactions=summary), create=False)
            except state.StateBusy:
                pass
        return brief(agent, sid, reason="resume", now=now)
    size = _size_of(path) or 0
    cwd = data.get("cwd") or ""
    link = state.read(clear_path(agent, cwd)) if src == "clear" and cwd else None
    old = (link or {}).get("old_sid")
    linked = bool(old and old != sid and now - float(link.get("ts") or 0) <= CLEAR_TTL)

    def start(r):
        r.setdefault("offsets", {})["main"] = size
        if linked:
            r["cleared_from"] = old
        _meta(r, data, sess)
    _quietly(agent, sid, start, now)
    if link:
        try:
            state.remove(clear_path(agent, cwd))
        except state.StateBusy:
            pass
    return _notice(agent, old, now) if linked else ""


@_fail_open(None)
def on_post_compact(agent: str, data: dict, now=None) -> None:
    """PostCompact: close the compaction. Claude main session: compare compact_summary with devkit's record and keep
    "Not in the summary" for the next hook (this hook's output is display only). Codex has no summary."""
    now = now or time.time()
    sid = data.get("session_id") or ""
    if not sid:
        return None
    key, pair = _pair_of(agent, data)
    who = data.get("agent_id") or "main"
    summary = data.get("compact_summary") if agent == "claude" and who == "main" else None
    refs = None
    if isinstance(summary, str) and summary.strip():
        refs = _refs(summary, _items(read(agent, sid) or {}, _live_run(agent, sid), now))[:20]
    fresh = []

    def change(r):
        c = _join(r, who, pair, now)
        if not c:
            c = _new(r, who, now, data.get("trigger"), key, pair, [])
            fresh.append(c)
        _seen(c, "post")
        c["closed"] = True
        if refs is not None:
            r["missing"] = {"for": c["id"], "ts": round(now, 3), "items": refs}
            if refs:
                r.setdefault("deliver", []).append({"kind": "missing", "who": "main", "for": c["id"], "items": refs})
    _announce(agent, sid, fresh, _quietly(agent, sid, change, now))
    return None


@_fail_open("")
def on_prompt(agent: str, data: dict, now=None) -> str:
    """UserPromptSubmit (after hooks.prompt's filters): the prompt waits in the checkpoint as pending until Stop
    classes the turn (an earlier prompt still pending was never classed, and goes); then the main session gets
    what waits for it (a late brief, the "Not in the summary" list), once."""
    now = now or time.time()
    sid = data.get("session_id") or ""
    if not sid:
        return ""
    raw = data.get("prompt") or ""
    if raw.strip() and not raw.lstrip().startswith(_BACKGROUND):
        entry = {"ts": round(now, 3), "prompt_id": str(data.get("prompt_id") or data.get("turn_id") or "")[:64],
                 "text": _clean(raw), "pending": True}
        sess = _session(agent, sid)

        def add(r):
            cp = r.setdefault("checkpoint", {})
            # a prompt still pending belongs to a turn no Stop classed (interrupted or lost): it was never found
            # to be development work, so it goes (compliance review, change 5)
            kept = [p for p in cp.get("prompts") or [] if not p.get("pending")]
            cp["prompts"] = (kept + [entry])[-PROMPTS:]
            r["checkpoint"] = _fit(cp)
            _meta(r, data, sess)
        _quietly(agent, sid, add, now)
    return _deliver_main(agent, sid, data, now)


def _deliver_main(agent: str, sid: str, data: dict, now: float) -> str:
    try:
        taken = _detect_and_take(agent, sid, "main", data.get("transcript_path"), now)
    except state.StateBusy as e:
        _log(f"{agent}:{sid[:12]} dropped: {e}")
        return ""
    return _render(agent, sid, taken, data.get("transcript_path"), now)


@_fail_open("")
def on_tool(agent: str, data: dict, now=None) -> str:
    """PostToolUse. Main session: a compaction only the transcript shows is recorded late and its brief given; the
    "Not in the summary" list too. A member (agent_id): a boundary in its own transcript gives it its reminder.
    Each once."""
    now = now or time.time()
    sid = data.get("session_id") or ""
    aid = data.get("agent_id")
    if not sid:
        return ""
    if not aid:
        return _deliver_main(agent, sid, data, now)
    path = member_transcript(data.get("transcript_path"), aid)
    persona = _owner_name(_live_run(agent, sid) or {}, aid)
    taken = _detect_and_take(agent, sid, aid, path, now, persona=persona)
    return _render(agent, sid, taken, path, now)


@_fail_open(None)
def on_stop(agent: str, data: dict, sess=None, dev_turn=None, now=None) -> None:
    """Stop, when the turn is not sent back. The turn's pending prompts stay only when the classifier said
    development work (`dev_turn` True; False or None when scope.decide raised: they go). Then the checkpoint is
    refreshed (the claimed tasks too) and the main offset saved; a boundary no hook saw is recorded late, and its
    brief waits for the next prompt."""
    now = now or time.time()
    sid = data.get("session_id") or ""
    if not sid:
        return None
    sess = sess if isinstance(sess, dict) else _session(agent, sid)
    tasks = _tasks(agent, sid, sess.get("project") or "")

    def keep(prompts):
        out = []
        for p in prompts:
            if p.get("pending"):
                if dev_turn is not True:
                    continue
                p.pop("pending", None)
            out.append(p)
        return out
    write = _checkpoint(agent, sid, "stop", now, data, tasks=tasks, keep=keep)
    path = data.get("transcript_path")
    rec = read(agent, sid) or {}
    saved = (rec.get("offsets") or {}).get("main")
    got = scan(path, saved) if path and agent == "claude" and saved is not None else None
    size = _size_of(path) if saved is None else None
    fresh = []

    def change(r):
        write(r)
        offs = r.setdefault("offsets", {})
        if got:
            for b in got.boundaries:
                if b.get("agent_id"):
                    continue
                entry, created = _attach(r, b, "main", "", now)
                if entry and created:
                    fresh.append(entry)
                    r.setdefault("deliver", []).append({"kind": "brief", "who": "main", "for": entry["id"]})
            offs["main"] = got.offset
        elif size is not None:
            offs.setdefault("main", size)
    _announce(agent, sid, fresh, _quietly(agent, sid, change, now))
    return None


def is_summarizer(agent: str, data: dict, sess) -> bool:
    """The compaction summarizer's SubagentStop (spec F2 amendment): agent_type "", no transcript of its own, no
    launch and no lane names it. subagent_stop returns at once for it: nothing is written or bound."""
    try:
        aid = data.get("agent_id") or ""
        if data.get("agent_type", None) != "" or not aid:
            return False
        p = data.get("agent_transcript_path")
        if p and os.path.exists(p):
            return False
        sess = sess if isinstance(sess, dict) else {}
        if aid in (sess.get("subagents") or {}):
            return False
        return not any(isinstance(x, dict) and x.get("agent_id") == aid for x in (sess.get("launches") or {}).values())
    except Exception:
        return False


@_fail_open(None)
def on_subagent_stop(agent: str, data: dict, now=None) -> None:
    """SubagentStop (after progress.on_subagent): a last read of the member's transcript; a compaction no hook saw
    is recorded late. The member has finished, so no reminder waits for it, and its offset goes."""
    now = now or time.time()
    sid, aid = data.get("session_id") or "", data.get("agent_id") or ""
    if not sid or not aid or agent != "claude":
        return None
    path = data.get("agent_transcript_path") or member_transcript(data.get("transcript_path"), aid)
    rec = read(agent, sid) or {}
    got = scan(path, (rec.get("offsets") or {}).get(aid) or 0)
    if not got.boundaries and aid not in (rec.get("offsets") or {}):
        return None
    persona = _owner_name(_live_run(agent, sid) or {}, aid)
    fresh = []

    def change(r):
        for b in got.boundaries:
            entry, created = _attach(r, b, aid, persona, now)
            if entry and created:
                fresh.append(entry)
        (r.get("offsets") or {}).pop(aid, None)
        r["deliver"] = [d for d in r.get("deliver") or [] if d.get("who") != aid]
    _announce(agent, sid, fresh, _quietly(agent, sid, change, now, create=False))
    return None


@_fail_open(None)
def on_session_end(agent: str, data: dict, now=None) -> None:
    """SessionEnd, before the sync. reason clear: the clear link for the next session in this folder, and the old
    checkpoint's prompts go (compliance review, change 5). Other reasons: nothing; the record outlives the session
    so that a resume finds it."""
    now = now or time.time()
    sid, cwd = data.get("session_id") or "", data.get("cwd") or ""
    if (data.get("reason") or "") != "clear" or not sid or not cwd:
        return None
    state.replace(clear_path(agent, cwd), {"old_sid": sid, "cwd": cwd, "ts": round(now, 3)})

    def drop(r):
        if not (r.get("checkpoint") or {}).get("prompts"):
            return state.SKIP
        r["checkpoint"]["prompts"] = []
    _quietly(agent, sid, drop, now, create=False)
    return None


# --------------------------------------------------------------------------- commands and readers

def latest(cwd=None, project=None, now=None):
    """-> (agent, sid) of the newest continuity record of the project (`project`, or the one at `cwd`) that has an
    open run or requests and changed in the last 12 hours; None when there is none."""
    now = now or time.time()
    project = project or (progress._project(cwd) if cwd else "")
    if not project or not CONT.is_dir():
        return None
    best = None
    for f in CONT.iterdir():
        if not _OWN.match(f.name) or f.name.startswith("clear-"):
            continue
        r = state.read(f)
        if not r or r.get("project") != project or not r.get("agent") or not r.get("session"):
            continue
        changed = r.get("updated") or 0
        if now - changed > LATEST_H * 3600:
            continue
        run = state.read(progress.run_path(r["agent"], r["session"])) or {}
        if run.get("state") not in ("running", "blocked") and not (r.get("checkpoint") or {}).get("prompts"):
            continue
        if best is None or changed > best[0]:
            best = (changed, r["agent"], r["session"])
    return (best[1], best[2]) if best else None


def carry(agent: str, from_sid: str, to_sid: str, now=None) -> dict:
    """Move the open run of `from_sid` (the session /clear ended) to `to_sid`, under both sessions' locks. Its lanes
    whose member ended or went stale close first, with the reason in the run's history (progress.close_quiet_lanes);
    refused while a member of that run still works (its results go to the old session) and when this session already
    has an open run. A run the reconciler orphaned when that session ended runs again here, with the reason in its
    history (progress.take_back). Audited as session.carry."""
    now = now or time.time()
    if not from_sid or from_sid == to_sid:
        raise ValueError("nothing to carry: name the cleared session with --from <session id>")
    src, dst = progress.run_path(agent, from_sid), progress.run_path(agent, to_sid)
    a, b = sorted((src, dst), key=str)
    closed = []
    with state.hold(a), state.hold(b):
        run = state.load(src)
        if not run:
            raise ValueError(f"no run to carry in {agent}:{from_sid[:12]}")
        closed = progress.close_quiet_lanes(run, f"carried to {agent}:{to_sid[:12]}", now)
        busy = [lane.get("persona") or lane.get("label") or k for k, lane in progress.running_agents(run)]
        if busy:
            raise ValueError(f"{', '.join(busy)} still running in {agent}:{from_sid[:12]}: carry the run once they "
                             "have reported (their results go to that session)")
        there = state.load(dst)
        if there and there.get("state") in ("running", "blocked"):
            raise ValueError(f"this session already has an open run «{there.get('goal')}»: close it first "
                             "(dev progress done)")
        run.update(session=to_sid, carried_from=from_sid, carried_at=round(now))
        why = f"carried to {agent}:{to_sid[:12]}"
        taken = progress.take_back(run, why, now)
        state.replace(dst, run, indent=1)
        state.remove(src)
    if closed:
        from . import approvals
        approvals.audit_moves(run, closed)
    if taken:
        progress.audit_taken_back(run, why)
    audit.record("session.carry", run.get("project") or f"{agent}:{to_sid[:12]}",
                 f"run «{_excerpt(run.get('goal'), 80)}» from {agent}:{from_sid[:12]} to {agent}:{to_sid[:12]}",
                 "ok", project=run.get("project") or "", session=f"{agent}:{to_sid[:12]}")
    return {"from": from_sid, "to": to_sid, "goal": run.get("goal"), "steps": len(run.get("plan") or [])}


def expire(now=None) -> int:
    """For the scheduled `dev sync`: delete continuity records unchanged for 7 days and clear links older than 10
    minutes. -> how many."""
    now = now or time.time()
    n = 0
    for f in sorted(CONT.iterdir()) if CONT.is_dir() else []:
        if not _OWN.match(f.name):
            continue
        try:
            if f.name.startswith("clear-"):
                age, limit = now - float((state.read(f) or {}).get("ts") or f.stat().st_mtime), CLEAR_TTL
            else:
                age, limit = now - f.stat().st_mtime, KEEP_DAYS * 86400
            if age > limit:
                state.remove(f)
                n += 1
        except (OSError, ValueError):
            pass
    return n


def forget(session=None, all_: bool = False) -> dict:
    """`dev session forget`: delete one session's continuity record and its clear links, or every one (--all).
    Audited as session.forget, with counts only."""
    recs = links = 0
    targets = []
    if CONT.is_dir():
        if all_:
            targets = [f for f in sorted(CONT.iterdir()) if _OWN.match(f.name)]
        else:
            agent, _, sid = str(session or "").partition(":")
            if not agent or not sid:
                raise ValueError("--session takes <agent>:<session id>")
            targets = [record_path(agent, sid)] + [
                f for f in sorted(CONT.glob(f"clear-{_safe(agent)}-*.json"))
                if (state.read(f) or {}).get("old_sid") == sid]
    for f in targets:
        if f.exists():
            state.remove(f)
            if f.name.startswith("clear-"):
                links += 1
            else:
                recs += 1
    target = "all" if all_ else str(session)[:40]
    audit.record("session.forget", target, f"{recs} record(s), {links} clear link(s)", "ok", count=recs + links)
    return {"records": recs, "links": links}


def add_arguments(p):
    """`dev session …` (cli.py: continuity.add_arguments(add("session", continuity.cmd_session, …)))."""
    p.add_argument("session_cmd", choices=("brief", "carry", "compactions", "forget"),
                   metavar="brief|carry|compactions|forget",
                   help="brief: what devkit kept for a session · carry: move the run here after /clear · "
                        "compactions: count a transcript's compactions (reads only) · forget: delete the records")
    p.add_argument("transcript", nargs="?", help="compactions: the transcript (.jsonl) to read")
    p.add_argument("--session", help="<agent>:<session id> (brief: default this session; forget: that session)")
    p.add_argument("--latest", action="store_true",
                   help="brief: the project's newest session with an open run or requests (last 12 hours)")
    p.add_argument("-p", "--project", help="brief --latest: a project id, e.g. owner/repo (default: this folder's)")
    p.add_argument("--for-chat", action="store_true",
                   help="brief: for a chat surface (devkit_resume): each request cut to 200 characters")
    p.add_argument("--provider", help="brief: who will read it (claude, codex, anthropic, openai); request text "
                                      "only for the session's own provider, otherwise their count and times")
    p.add_argument("--from", dest="from_sid", help="carry: the cleared session (default: the one /clear ended)")
    p.add_argument("--members", action="store_true", help="compactions: the members' transcripts too")
    p.add_argument("--all", action="store_true", help="forget: every continuity record and clear link here")
    return p


def _is_codex(path: str) -> bool:
    return "/.codex/" in str(path) or Path(path).name.startswith("rollout-")


def _print_compactions(path) -> int:
    got = scan(path, 0, cap=None)
    print(f"{core.tilde(path)}: {len(got.boundaries)} compaction(s)")
    for b in got.boundaries:
        tokens = (f"{_k(b['pre_tokens'])} → {_k(b['post_tokens'])} tokens" if b.get("pre_tokens") is not None
                  and b.get("post_tokens") is not None else "tokens not recorded")
        print(f"  {core.fmt_ts(b.get('ts'))} · {b.get('trigger') or '?'} · {tokens}")
    return len(got.boundaries)


def _reader(given, chat: bool, cwd: str):
    """The provider `dev session brief` reads for (C3). An agent's command reads for that agent's own provider, and
    --provider only narrows it: naming another one shows that reader nothing, never the other provider's text. A
    chat surface (--for-chat) must name its provider: devkit_resume takes it from the MCP server, never the model.
    The developer's shell: --provider as given (none: the session itself)."""
    if chat and not given:
        return None  # brief(chat=True, provider=None) shows no text
    who = progress.caller(cwd)
    own = who.get("agent") if who.get("caller") not in ("human", None) else None
    if not own:
        return given
    if given and _prov(given) != _prov(own):
        return "-"  # no provider: nothing to show
    return own


def cmd_session(a) -> int:
    """dev session brief | carry | compactions | forget. A refusal (a crew member's call, which only Morpheus may
    make, or a missing argument) is one line on stderr and exit 2, as with the other dev commands, never a
    traceback."""
    from . import ui
    try:
        return _session_command(a)
    except ValueError as e:  # progress.StepError is one
        ui.err(str(e))
        return 2


def _session_command(a) -> int:
    cwd = os.getcwd()
    act = a.session_cmd
    if act == "compactions":
        if not a.transcript:
            raise ValueError("give the transcript: dev session compactions <path.jsonl>")
        if _is_codex(a.transcript):
            print("devkit: reading compactions from a Codex rollout is not supported on Codex yet (its compaction "
                  "item is checked live after 2026-11-09)")
            return 0
        if not os.path.isfile(a.transcript):
            raise ValueError(f"no transcript at {a.transcript}")
        _print_compactions(a.transcript)
        if a.members:
            folder = Path(a.transcript).with_suffix("") / "subagents"
            total = 0
            for f in sorted(folder.glob("agent-*.jsonl")) if folder.is_dir() else []:
                total += len(scan(f, 0, cap=None).boundaries)
            print(f"members: {total} compaction(s)")
        return 0
    progress._main_only(None, {"brief": "reads a session's continuity brief", "carry": "moves a run between sessions",
                               "forget": "deletes continuity records"}[act], cwd)
    if act == "brief":
        me = progress.resolve_session(cwd)  # the reader: another session's ids carry their session
        if a.latest:
            hit = latest(cwd=cwd, project=a.project)
            if not hit:
                print("devkit: no session of this project has an open run or requests in the last 12 hours")
                return 0
            agent, sid = hit
        elif a.session and ":" in a.session:
            agent, sid = a.session.split(":", 1)
        else:
            agent, sid, how = me
            if how == "shell":
                raise ValueError("no agent session here: give --session <agent>:<session id> or --latest")
        print(brief(agent, sid, reason="manual", chat=a.for_chat, provider=_reader(a.provider, a.for_chat, cwd),
                    caller=me[:2]) or "devkit: nothing to carry over for that session")
        return 0
    if act == "carry":
        agent, to_sid, how = progress.resolve_session(cwd)
        if how == "shell":
            raise ValueError("dev session carry runs inside the agent session that takes the run over")
        from_sid = a.from_sid or (read(agent, to_sid) or {}).get("cleared_from")
        out = carry(agent, from_sid, to_sid)
        print(f"✓ run «{out['goal']}» moved from {agent}:{from_sid[:12]} to this session ({out['steps']} steps)")
        return 0
    if not a.all and not a.session:
        raise ValueError("say which: --session <agent>:<session id> or --all")
    out = forget(session=a.session, all_=a.all)
    print(f"✓ forgot {out['records']} record(s), {out['links']} clear link(s)")
    return 0
