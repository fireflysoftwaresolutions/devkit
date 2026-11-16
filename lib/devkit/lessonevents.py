# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Local lesson observations and deferred recurrence evidence.

The append-only event log records shown, matched failure after exposure (missed), no_observed_repeat, and unknown.
Historical heeded records remain readable as no_observed_repeat, never a success rate. Ending or abandoning a
session resolves pending observations as unknown. Neither kind increases lesson strength.

on_hit accepts demonstrated failures only. With a trusted source_event and project it also queues bounded local
evidence. drain_recurrences applies that evidence during maintenance; the source identity in the lesson makes a
replayed write idempotent. No lesson or git write runs in a hook. Busy or unavailable local storage fails open.

Event appends and pruning share a lock. Outcome folding uses an atomic per-lesson cursor and a FOLD_GRACE window.
Pruning retains outcomes not folded yet. rates reports observations per channel; coverage measures exposure
before recorded recurrence, not prevention effectiveness.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import tempfile
import time
from pathlib import Path

from . import state
from .core import STATE

EVENTS = STATE / "lessons-events.jsonl"
SESSIONS = STATE / "sessions"  # the hooks' session records (progress.SESSIONS): their last write is the last activity
EVENT_KINDS = ("shown", "heeded", "missed", "no_observed_repeat", "unknown")
CHANNELS = ("session", "brief", "call", "output", "refusal")
FOLD_GRACE = 60
# Sessions abandoned without SessionEnd resolve as unknown after one day.
IDLE_END = 86400
TEXT_CUT = 200  # characters kept of an agent or session id
_ID = re.compile(r"L-[0-9A-Za-z]{1,40}")
_RECUR = re.compile(r"\s*recur\s+(L-[0-9A-Za-z]{1,40})\b")


def record(event: str, lesson_id: str, channel: str, agent: str, session: str, now: float = None, *,
           source_event: str = "") -> None:
    """Append one event. Raises ValueError for an unknown event, a lesson id that is not one, or a channel that is
    not one of CHANNELS (a resolution may have none)."""
    if event not in EVENT_KINDS:
        raise ValueError(f"event must be one of {', '.join(EVENT_KINDS)}, not {event!r}")
    lid = str(lesson_id or "")
    if not _ID.fullmatch(lid):
        raise ValueError(f"not a lesson id: {lid[:60]!r}")
    channel = str(channel or "")
    if channel not in CHANNELS and not (channel == "" and event != "shown"):
        raise ValueError(f"channel must be one of {', '.join(CHANNELS)}, not {channel[:40]!r}")
    item = {"ts": int(time.time() if now is None else now), "id": lid, "event": event,
            "channel": channel, "agent": _cut(agent), "session": _cut(session)}
    if source_event:
        item["source_event"] = hashlib.sha256(source_event.encode()).hexdigest()
    line = json.dumps(item) + "\n"
    try:
        path = Path(EVENTS)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with state.hold(path):  # prune swaps the file under the same lock: an append never lands in the old one
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line.encode())
            finally:
                os.close(fd)
    except (OSError, state.StateBusy):
        pass


def _cut(value) -> str:
    return str(value or "")[:TEXT_CUT]


def _events(session: str = None, until: float = None) -> list:
    """The log's events (only those of `session` when given, none after `until`), oldest first; lines that do not
    parse are skipped."""
    try:
        raw = Path(EVENTS).read_text(errors="replace")
    except OSError:
        return []
    needle = json.dumps(_cut(session)) if session is not None else None
    out = []
    for ln in raw.splitlines():
        if needle and needle not in ln:
            continue
        try:
            e = json.loads(ln)
            e["ts"] = int(e["ts"])
        except (ValueError, TypeError, KeyError):
            continue
        if not isinstance(e.get("id"), str) or e.get("event") not in EVENT_KINDS:
            continue
        if session is not None and e.get("session") != _cut(session):
            continue
        if until is not None and e["ts"] > until:
            continue
        out.append(e)
    out.sort(key=lambda e: e["ts"])  # stable: events of one second keep the order they were written in
    return out


def _pending(events: list, session: str, agent: str) -> dict:
    """{lesson id: [channels, the latest show last]} of the lessons shown to this agent in this session and not yet
    resolved, in order of first show."""
    session, agent = _cut(session), _cut(agent)
    pending = {}
    for e in events:
        if e.get("session") != session or e.get("agent") != agent:
            continue
        if e["event"] == "shown":
            chans = pending.setdefault(e["id"], [])
            ch = e.get("channel") if e.get("channel") in CHANNELS else ""
            if ch in chans:
                chans.remove(ch)
            chans.append(ch)
        else:
            pending.pop(e["id"], None)
    return pending


def shown_in(session: str, agent: str, now: float = None) -> list:
    """The lessons shown to this agent in this session and not yet resolved, in order of first show."""
    return list(_pending(_events(session, now), session, agent))


def _resolve(event: str, pick, session: str, agent: str, now: float, source_event: str = "") -> list:
    try:
        with state.hold(EVENTS):
            events = _events(session, now)
            pending = _pending(events, session, agent)
            done = pick(pending)
            if source_event:
                key = hashlib.sha256(source_event.encode()).hexdigest()
                seen = {e["id"] for e in events if e.get("source_event") == key and e.get("agent") == _cut(agent)}
                done = [lid for lid in done if lid not in seen]
            for lid in done:
                record(event, lid, pending[lid][-1], agent, session, now, source_event=source_event)
            return done
    except state.StateBusy:
        return []  # the log's lock was not free in time: this hook's events are dropped whole (state audits it)


def on_hit(lesson_ids: list, session: str, agent: str, now: float = None, *,
           source_event: str = "", project: str = "") -> list:
    """The agent hit these lessons' anchors: record `missed` for the ones shown to it earlier in this session and
    still pending, and return them. Lessons it was not shown record nothing."""
    ids = list(dict.fromkeys(str(x) for x in lesson_ids or []))
    if not ids:
        return []
    if source_event:
        ids = _queue_recurrences(ids, session, agent, source_event, project, now)
    return _resolve("missed", lambda pending: [i for i in ids if i in pending], session, agent, now, source_event)


def _recurrences_path() -> Path:
    return Path(EVENTS).with_name("lessons-recurrences.json")


def _queue_recurrences(ids, session, agent, source_event, project, now) -> list:
    """Persist bounded evidence only. Lesson and git writes belong to maintenance."""
    if not session or not agent or not isinstance(source_event, str) or len(source_event) > 500:
        return []
    if not isinstance(project, str) or not re.fullmatch(r"[\w.-]+(?:/[\w.-]+)+", project) or \
            any(part in (".", "..") for part in project.split("/")):
        return []
    events = _events(session, now)
    shown = {e["id"] for e in events
             if e.get("agent") == _cut(agent) and e["event"] == "shown"}
    event_hash = hashlib.sha256(source_event.encode()).hexdigest()
    resolved = {e["id"] for e in events if e.get("agent") == _cut(agent)
                and e.get("source_event") == event_hash}
    accepted = []

    def queue(doc):
        for lid in ids[:20]:
            if lid not in shown or lid in resolved or not _ID.fullmatch(lid):
                continue
            key = hashlib.sha256(json.dumps([session, agent, source_event, lid]).encode()).hexdigest()
            if key in doc:
                accepted.append(lid)  # resume a crash between queue persistence and outcome recording
                continue
            if len(doc) >= 512:
                continue
            doc[key] = {"id": lid, "project": project, "ts": int(time.time() if now is None else now)}
            accepted.append(lid)
        return doc if accepted else state.SKIP
    try:
        state.update(_recurrences_path(), queue, wait=0.1)
    except (OSError, state.StateBusy):
        return []
    return accepted


RECURRENCE_KEEP_DAYS = 30  # queued evidence older than this leaves the queue unapplied


def drain_recurrences(limit: int = 50, now: float = None) -> int:
    """Apply queued evidence idempotently; a crash after the lesson write is safe to replay. An entry that can never
    apply leaves the queue, so the queue's cap never turns new evidence away: a malformed entry, one whose lesson is
    gone (neither active, archived, nor an unreadable file), one whose lesson is outside the project it happened in,
    and any entry older than RECURRENCE_KEEP_DAYS. An entry that fails for a reason that can pass (a busy lock, a
    lesson file that cannot be read now, a failed write) stays, rotated to the back. -> how many were applied."""
    from . import lessons
    now = time.time() if now is None else now
    path = _recurrences_path()
    applied = 0
    known = None

    def drop(key):
        state.update(path, lambda doc: doc if doc.pop(key, None) is not None else state.SKIP)

    with state.hold(path.with_name("lessons-recurrences-drain.json")):
        queued = state.read(path) or {}
        for key, event in list(queued.items())[:max(0, min(limit, 50))]:
            # Rotate before attempting the write: unresolved evidence and interrupted writes must not starve
            # later events. The queue lock is held only for this small mutation, never for lesson work.
            def rotate(doc):
                if key in doc:
                    doc[key] = doc.pop(key)
                return doc
            state.update(path, rotate)
            if not re.fullmatch(r"[a-f0-9]{64}", key) or not isinstance(event, dict):
                drop(key)
                continue
            lid, project, ts = event.get("id"), event.get("project"), event.get("ts")
            if not isinstance(lid, str) or not _ID.fullmatch(lid) or not isinstance(project, str) or \
                    not re.fullmatch(r"[\w.-]+(?:/[\w.-]+)+", project) or \
                    any(part in (".", "..") for part in project.split("/")) or \
                    type(ts) not in (int, float) or now - ts > RECURRENCE_KEEP_DAYS * 86400:
                drop(key)
                continue
            if known is None:  # every lesson id this Mac holds, read once per drain
                known = {x["meta"]["id"] for x in lessons.all_lessons() + lessons.all_lessons(archived=True)}
                known |= {str(f.get("id")) for f in lessons.unreadable()}
            if lid not in known:
                drop(key)
                continue
            try:
                les = lessons.find(lid)
                from . import core
                registry = core.read_json(core.HANDOFF / "registry.json", {}) or {}
                initiatives = [f"initiatives/{i['id']}" for i in registry.get("initiatives", [])
                               if isinstance(i, dict) and i.get("id") and project in (i.get("projects") or [])]
                if les["meta"]["scope"] not in [project, "global"] + initiatives:
                    drop(key)
                    continue
                lessons.recur(les, "Matched failure after lesson exposure", project=project, source_event=key)
            except (ValueError, OSError, state.StateBusy):
                continue
            drop(key)
            applied += 1
    return applied


def on_end(session: str, agent: str, now: float = None) -> list:
    """Ending a lane supplies no behavioral evidence about pending lessons."""
    return _resolve("unknown", list, session, agent, now)


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _last_written(session: str) -> float:
    """When the hooks last wrote this session's record (SESSIONS/<agent>-<session>.json, any agent), or 0."""
    import glob
    try:
        return max((p.stat().st_mtime for p in Path(SESSIONS).glob(f"*-{glob.escape(session)}.json")), default=0)
    except (OSError, ValueError):
        return 0


def _idle_ends(now: float, through: int) -> int:
    """Record `unknown` for the lessons still pending in each session idle for IDLE_END: no lesson event of it, and no
    write of its record, since then. The resolution is dated `through`, so this fold counts it, and carries the
    channel of the latest show, as on_end's does. -> how many it recorded."""
    try:
        with state.hold(EVENTS):
            events = _events(until=now)
            last, pairs = {}, {}
            for e in events:
                s = e.get("session") or ""
                last[s] = max(last.get(s, 0), e["ts"])
                pairs.setdefault((s, e.get("agent") or ""), None)
            n = 0
            for session, agent in pairs:
                if not session or now - max(last[session], _last_written(session)) < IDLE_END:
                    continue
                for lid, chans in _pending(events, session, agent).items():
                    record("unknown", lid, chans[-1], agent, session, through)
                    n += 1
            return n
    except state.StateBusy:
        return 0


def fold(now: float = None) -> dict:
    """Fold observations once; historical heeded becomes no_observed_repeat and idle sessions become unknown."""
    from . import lessons
    now = time.time() if now is None else now
    through = int(now - FOLD_GRACE)
    _idle_ends(now, through)
    events = [e for e in _events(until=through) if e["event"] != "shown" and _ID.fullmatch(e["id"])]
    totals = {"heeded": 0, "missed": 0, "unknown": 0, "no_observed_repeat": 0, "lessons": 0}
    if not events:
        return totals

    def add(usage: dict):
        cursor = {lid: _int(x.get("folded_last")) for lid, x in usage.items() if isinstance(x, dict)}
        added = {}
        for e in events:
            if e["ts"] <= cursor.get(e["id"], 0):
                continue
            a = added.setdefault(e["id"], {})
            kind = "no_observed_repeat" if e["event"] == "heeded" else e["event"]
            a[kind] = a.get(kind, 0) + 1
            a[kind + "_last"] = max(a.get(kind + "_last", 0), e["ts"])
        if not added:
            return state.SKIP
        for lid, a in added.items():
            entry = usage.get(lid) if isinstance(usage.get(lid), dict) else {}
            for kind in ("no_observed_repeat", "unknown", "missed"):
                if a.get(kind):
                    entry[kind] = _int(entry.get(kind)) + a[kind]
                    entry[kind + "_last"] = max(_int(entry.get(kind + "_last")), a[kind + "_last"])
                    totals[kind] += a[kind]
            entry["folded_last"] = max(cursor.get(lid, 0), through)
            usage[lid] = entry
        totals["lessons"] = len(added)
        return usage

    state.update(lessons.USAGE_LOCAL, add)
    return totals


def _unfolded(e: dict, cursor: dict) -> bool:
    """An outcome fold has not added to the usage counts yet (fold's own test, against its cursor)."""
    return e.get("event") in EVENT_KINDS[1:] and isinstance(e.get("id"), str) and \
        bool(_ID.fullmatch(e["id"])) and e["ts"] > cursor.get(e["id"], 0)


def prune(days: int = 45, now: float = None) -> int:
    """Remove the events older than `days` from the log, and return how many lines went. An outcome fold has not
    counted yet stays, however old (call fold first to let it go), and so does every event of the window; a line
    that does not parse as an event (no reader can use it) goes. Under the log's lock, which record also takes: the
    kept lines go to a temporary file in the log's folder (0600) that replaces the log atomically. Nothing to remove
    writes nothing; a log that is missing, unreadable or whose lock stays busy (state.WAIT) removes nothing (0)."""
    if days < 0:
        raise ValueError(f"days must be 0 or more, not {days}")
    cutoff = (time.time() if now is None else now) - days * 86400
    from . import lessons
    usage = state.read(lessons.USAGE_LOCAL) or {}
    cursor = {lid: _int(x.get("folded_last")) for lid, x in usage.items() if isinstance(x, dict)}
    path = Path(EVENTS)
    try:
        with state.hold(path):
            try:
                lines = path.read_bytes().decode("utf-8", "replace").splitlines(keepends=True)
            except FileNotFoundError:
                return 0
            kept = []
            for ln in lines:
                try:
                    e = json.loads(ln)
                    e["ts"] = int(e["ts"])
                except (ValueError, TypeError, KeyError):
                    continue
                if e["ts"] >= cutoff or _unfolded(e, cursor):
                    kept.append(ln if ln.endswith("\n") else ln + "\n")
            removed = len(lines) - len(kept)
            if not removed:
                return 0
            fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.{os.getpid()}.", suffix=".tmp", dir=str(path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.writelines(kept)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return removed
    except (OSError, state.StateBusy):
        return 0


def rates(days: int = 30) -> dict:
    """Observations per channel; legacy heeded_rate remains None rather than claiming effectiveness."""
    now = time.time()
    since = now - days * 86400
    channels = {c: {"shown": 0, "no_observed_repeat": 0, "unknown": 0, "heeded": 0, "missed": 0}
                for c in CHANNELS}
    per = {}
    shows = {}
    for e in _events():
        key = (e.get("session"), e.get("agent"), e["id"])
        inside = since <= e["ts"] <= now
        if e["event"] == "shown":
            ch = e.get("channel")
            shows.setdefault(key, [])
            if ch not in shows[key]:
                shows[key].append(ch)
            by = [ch]
        else:  # the channels that showed it in this cycle; the event's own when the shows are older than the log
            by = shows.pop(key, None) or [e.get("channel")]
        if not inside:
            continue
        outcome = "no_observed_repeat" if e["event"] == "heeded" else e["event"]
        for ch in by:
            if ch in channels:
                channels[ch][outcome] += 1
        counts = per.setdefault(e["id"], {"shown": 0, "heeded": 0, "missed": 0,
                                         "no_observed_repeat": 0, "unknown": 0})
        counts[outcome] += 1
    for c in channels.values():
        c["heeded_rate"] = None  # retained for older readers; observation is never effectiveness
    most = sorted(({"id": lid, **c} for lid, c in per.items() if c["missed"]),
                  key=lambda x: (-x["missed"], -x["shown"], x["id"]))
    return {"days": days, "channels": channels, "most_missed": most}


def _sid12(session) -> str:
    """A session id as the audit records it: without its agent prefix, 12 characters."""
    return str(session or "").split(":", 1)[-1][:12]


def coverage(days: int = 14) -> dict:
    """Recall coverage for `dev lesson eval`: each lesson recurrence recorded on this machine in the last `days`
    (a `dev lesson recur` in the local audit records, with its session), and whether the lesson was shown in that
    session before it, by any agent. A recurrence with no session is counted as unknown and left out of the rate.
    -> {"days", "recurrences", "shown_before", "unknown", "coverage" (shown_before / judged, None when none),
    "items": [{id, ts, session, shown_before, channels}]}."""
    from . import audit
    shows = {}
    for e in _events():
        if e["event"] == "shown":
            shows.setdefault((e["id"], _sid12(e.get("session"))), []).append((e["ts"], e.get("channel")))
    known = {lid for lid, _ in shows}
    items = []
    for r in audit.query(days=days, action="dev.lesson"):
        m = _RECUR.match(str(r.get("target") or ""))
        if r.get("outcome") != "ok" or not m:
            continue
        lid = m.group(1)
        if lid not in known:  # `dev lesson recur` takes an id prefix: the one shown lesson it names
            like = [k for k in known if k.startswith(lid)]
            lid = like[0] if len(like) == 1 else lid
        ts = float(r.get("ts") or 0)
        sid = _sid12((r.get("actor") or {}).get("session"))
        before = [ch for t, ch in shows.get((lid, sid), []) if t <= ts] if sid else []
        items.append({"id": lid, "ts": int(ts), "session": sid, "shown_before": bool(before) if sid else None,
                      "channels": list(dict.fromkeys(before))})
    judged = [x for x in items if x["shown_before"] is not None]
    hit = sum(1 for x in judged if x["shown_before"])
    return {"days": days, "recurrences": len(items), "shown_before": hit, "unknown": len(items) - len(judged),
            "coverage": round(hit / len(judged), 3) if judged else None, "items": items}
