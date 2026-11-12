# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Lesson outcome events: did a lesson that reached an agent change what it did next?

The log is STATE/lessons-events.jsonl, local to this machine and append-only, one JSON object per line:

    {"ts": int, "id": "L-…", "event": "shown|heeded|missed", "channel": "…", "agent": "…", "session": "…"}

    shown    the hook showed the lesson to an agent, after its filters; channel: session, brief, call, output or refusal
    missed   the same agent, in the same session, then hit the lesson's refusal, command or error anchor (on_hit)
    heeded   the session or the crew lane ended without that (on_end), or the session has been idle for IDLE_END
             (fold: a session that ends without SessionEnd, after a crash or a closed terminal, resolves no lesson)

A lesson shown to an agent is pending until one of the two resolves it; shown again later, it is pending again.
The resolution's channel is the one of the latest show; rates() credits every channel that showed it.

    record(event, lesson_id, channel, agent, session, now=None)    append one event
    shown_in(session, agent, now=None) -> [id]                     shown and not yet resolved, in order of first show
    on_hit(lesson_ids, session, agent, now=None) -> [id]           record `missed` for the ones pending; return them
    on_end(session, agent, now=None) -> [id]                       record `heeded` for the rest; return them
    fold(now=None) -> {heeded, missed, lessons}                    resolve idle sessions, then add heeded and missed
                                                                   to the usage counts
    prune(days=45, now=None) -> int                                remove the events older than the window, but the
                                                                   outcomes fold has not counted; the lines removed
    rates(days=30) -> {days, channels, most_missed}                heeded rate per channel; the most missed lessons
    coverage(days=14) -> {days, recurrences, shown_before, unknown, coverage, items}
                                                                   recorded recurrences shown earlier in the session

Writes: each event is one os.write of one line to a file opened with O_APPEND, under the log's lock (state.hold), so
concurrent hooks never interleave or lose lines, and prune, which replaces the file under the same lock, never drops
an append. on_hit and on_end read and append under that lock (it is re-entrant), so two hooks resolving the same
lesson at once record it once. fold writes through state.update on lessons.USAGE_LOCAL, the lock lessons.track
uses. A write that fails (a full disk, a read-only home, a lock that stays busy) is dropped: the hooks that call
these never fail on it.

The usage counts: lessons.track already counts `shown` and `shown_<channel>` when the hook shows a lesson, so fold
adds only `heeded` and `missed`, with `heeded_last` and `missed_last`. Its cursor is `folded_last` in each lesson's
entry, written in the same atomic replace, so a second fold never counts an event twice; older readers sum the
counts and take the maximum of every `*_last` key, so the file stays compatible. Events of the last FOLD_GRACE
seconds wait for the next fold, so an event a hook is still writing is never skipped.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

from . import state
from .core import STATE

EVENTS = STATE / "lessons-events.jsonl"
SESSIONS = STATE / "sessions"  # the hooks' session records (progress.SESSIONS): their last write is the last activity
EVENT_KINDS = ("shown", "heeded", "missed")
CHANNELS = ("session", "brief", "call", "output", "refusal")
FOLD_GRACE = 60
# A session idle this long ended without SessionEnd (a crash, a closed terminal, a killed process): fold marks the
# lessons still pending in it heeded. A day, because a session resumed the next morning keeps its id and a miss there
# still counts, while the nightly fold resolves a forgotten one within two nights.
IDLE_END = 86400
TEXT_CUT = 200  # characters kept of an agent or session id
_ID = re.compile(r"L-[0-9A-Za-z]{1,40}")
_RECUR = re.compile(r"\s*recur\s+(L-[0-9A-Za-z]{1,40})\b")


def record(event: str, lesson_id: str, channel: str, agent: str, session: str, now: float = None) -> None:
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
    line = json.dumps({"ts": int(time.time() if now is None else now), "id": lid, "event": event,
                       "channel": channel, "agent": _cut(agent), "session": _cut(session)}) + "\n"
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


def _resolve(event: str, pick, session: str, agent: str, now: float) -> list:
    try:
        with state.hold(EVENTS):
            pending = _pending(_events(session, now), session, agent)
            done = pick(pending)
            for lid in done:
                record(event, lid, pending[lid][-1], agent, session, now)
            return done
    except state.StateBusy:
        return []  # the log's lock was not free in time: this hook's events are dropped whole (state audits it)


def on_hit(lesson_ids: list, session: str, agent: str, now: float = None) -> list:
    """The agent hit these lessons' anchors: record `missed` for the ones shown to it earlier in this session and
    still pending, and return them. Lessons it was not shown record nothing."""
    ids = list(dict.fromkeys(str(x) for x in lesson_ids or []))
    if not ids:
        return []
    return _resolve("missed", lambda pending: [i for i in ids if i in pending], session, agent, now)


def on_end(session: str, agent: str, now: float = None) -> list:
    """The session or the crew lane ended: record `heeded` for every lesson still pending, and return them."""
    return _resolve("heeded", list, session, agent, now)


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
    """Record `heeded` for the lessons still pending in each session idle for IDLE_END: no lesson event of it, and no
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
                    record("heeded", lid, chans[-1], agent, session, through)
                    n += 1
            return n
    except state.StateBusy:
        return 0


def fold(now: float = None) -> dict:
    """Add the heeded and missed events older than FOLD_GRACE seconds to the usage counts (lessons.USAGE_LOCAL),
    each event once, after the lessons pending in sessions idle for IDLE_END are resolved as heeded (_idle_ends).
    -> {"heeded": n, "missed": n, "lessons": lessons changed}. Nothing to add writes nothing."""
    from . import lessons
    now = time.time() if now is None else now
    through = int(now - FOLD_GRACE)
    _idle_ends(now, through)
    events = [e for e in _events(until=through) if e["event"] in ("heeded", "missed") and _ID.fullmatch(e["id"])]
    totals = {"heeded": 0, "missed": 0, "lessons": 0}
    if not events:
        return totals

    def add(usage: dict):
        cursor = {lid: _int(x.get("folded_last")) for lid, x in usage.items() if isinstance(x, dict)}
        added = {}
        for e in events:
            if e["ts"] <= cursor.get(e["id"], 0):
                continue
            a = added.setdefault(e["id"], {})
            a[e["event"]] = a.get(e["event"], 0) + 1
            a[e["event"] + "_last"] = max(a.get(e["event"] + "_last", 0), e["ts"])
        if not added:
            return state.SKIP
        for lid, a in added.items():
            entry = usage.get(lid) if isinstance(usage.get(lid), dict) else {}
            for kind in ("heeded", "missed"):
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
    return e.get("event") in ("heeded", "missed") and isinstance(e.get("id"), str) and \
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
    """How often a shown lesson was heeded, per channel, over the last `days`, and the lessons missed most:
    {"days", "channels": {channel: {shown, heeded, missed, heeded_rate}}, "most_missed": [{id, shown, heeded,
    missed}]}. A resolution counts for every channel that showed the lesson in its session before it; heeded_rate is
    heeded / (heeded + missed), None while nothing is resolved."""
    now = time.time()
    since = now - days * 86400
    channels = {c: {"shown": 0, "heeded": 0, "missed": 0} for c in CHANNELS}
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
        for ch in by:
            if ch in channels:
                channels[ch][e["event"]] += 1
        counts = per.setdefault(e["id"], {"shown": 0, "heeded": 0, "missed": 0})
        counts[e["event"]] += 1
    for c in channels.values():
        done = c["heeded"] + c["missed"]
        c["heeded_rate"] = round(c["heeded"] / done, 3) if done else None
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
