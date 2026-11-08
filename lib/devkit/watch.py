# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""One wait for every devkit event: `dev watch --wait`, the mail the hooks deliver, and the Stop rule that keeps an
idle session wakeable.

devkit tells agents what they must act on through hooks, and a hook runs only when the session does something. An
idle session (a main session that ended its turn, a member blocked on an answer) hears nothing until the developer
writes or a background task ends. `dev watch --wait` is that background task: it blocks until something its caller
must act on arrives for its own session, prints each such event once, one line each, and exits.

The state files are the event record. Each source keeps its own "told once" flag under its own lock, and the hooks
and the wait both take through `mail`, so an event is reported once, whichever reads it first:

  question  crew messages for the consumer (progress._take_messages: `delivered`)
  decision  decisions that wait for the developer (progress.take_decisions: `told`)
  ready     steps that became ready to launch (dispatch.take_ready: `ready_told`)
  ci        CI results of a session without a run (ciwatch.take_events: `delivered`); with a run they are messages
  member    a member that stopped, was lost or ended unverified (a notice approvals writes); one that finished
  notice    the crew limit changed, the handoff push failed (`notices` in the session record: `told`)

The consumer is the main session ("main") or one crew lane (its agent id). Waking events end a wait; notices ride
along with the next delivery and never end one alone: a member's normal finish on Claude Code, whose harness already
wakes the main session, is one, and so are devkit's own notices.

Member and CI text reaches the agent as one quoted span per line, control and escape sequences stripped: every line
the wait prints starts with devkit's own words, so none reads as a grant or an approval. The wait never calls a
model: it watches the state folders (kqueue where the system has it, a stat every two seconds anyway), batches a
burst for two seconds, and every 60 seconds reconciles the session's run, so a member that went silent or a crew
slot a quota window freed turns into an event while the session is idle.
"""
from __future__ import annotations

import json
import os
import re
import select
import signal
import sys
import time
from pathlib import Path

from . import core, state
from .core import STATE

LIVE = STATE / "watch" / "live"  # one record per `dev watch --wait`: `dev watches`
LIVE_DAYS = 3
KINDS = ("question", "decision", "ready", "member", "ci", "notice")
EXIT = {"events": 0, "error": 1, "nothing": 2, "timeout": 3, "superseded": 5}
BACKGROUND_MIN = 240  # a Claude Code background command: one notification when it exits, no 30-minute limit
MONITOR_MIN = 28  # under a Claude Code Monitor, which ends after 30 minutes
FOREGROUND_MIN = 9  # a crew member in the foreground: under Claude Code's 10-minute shell limit
CODEX_MIN = 10  # Codex has no background wake-up: the main session waits in the foreground, again and again
BATCH = 2.0  # seconds a burst of writes settles before the wait reads it
POLL = 2.0  # seconds between two stats of the watched files, with kqueue or without
HEARTBEAT = 60.0  # seconds between two reconciles of the session's run while it waits
MAX_EVENTS = 20  # events one wake takes and prints; the rest waits for the next
NOTICES_MAX = 20  # notices kept in a session record
LINE_MAX = 300  # characters of a member's or CI's text in one line
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028\u2029\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")


def clean(text, limit: int = LINE_MAX) -> str:
    """Text someone else wrote (a member, CI) as one inert span of a line: escape and control sequences dropped, then
    hooks._inline (no backticks, whitespace as one space, at most `limit` characters)."""
    from .hooks import _inline
    return _inline(_CONTROL.sub(" ", _ANSI.sub("", str(text or ""))), limit)


def _ev(kind: str, ident: str, wakes: bool, line: str, ts=None) -> dict:
    return {"kind": kind, "id": str(ident or ""), "wakes": bool(wakes), "line": line, "ts": ts}


# --------------------------------------------------------------------------- the sources, each a peek and a take

def _who(name) -> str:
    from . import crew
    return crew.who(name or "") or str(name or "a crew member")


def _consumer_name(agent: str, sid: str, me: str) -> str:
    """Who a wait is for, in words: "the main session", or the member as Role (Name), from its lane in the session's
    run (a wait knows the member by its agent id only)."""
    if me == "main":
        return "the main session"
    from . import progress
    try:
        lane = ((progress.load_run(agent, sid) or {}).get("agents") or {}).get(me) or {}
    except Exception:
        lane = {}
    return _who(lane.get("persona") or lane.get("label") or me)


def _ci_line(text: str) -> str:
    """A CI result as one line: its head and, for a failure, each run's class, next action and log."""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    head = re.sub(r"^devkit · ", "", lines[0]) if lines else "CI result"
    diag = [ln[2:] for ln in lines[1:] if ln.startswith("- ")]
    out = "devkit · " + clean(head, 200) + "".join(" · " + clean(d.replace("**", ""), 240) for d in diag[:3])
    return out + (" · first error: dev ci status (untrusted CI output)" if "untrusted CI output" in str(text) else "")


def _message_event(run: dict, m: dict, me: str) -> dict:
    from . import progress
    frm, mid, who = m.get("from") or "", m.get("id") or "", _who(m.get("from_name"))
    text = clean(m.get("text"))
    if frm == "ci":
        return _ev("ci", mid, True, _ci_line(m.get("text")), m.get("ts"))
    if progress._asks(m):
        if m.get("answered") or m.get("closed"):  # answered or closed before anyone read it: nothing to act on
            return _ev("question", mid, False, "", m.get("ts"))
        if me == "main" and progress.is_moot_message(run, m):
            return _ev("question", mid, False, f"devkit · moot: question {mid} from {who} (“{text}”): "
                                               f"{progress.moot_message_reason(run, m)}", m.get("ts"))
        return _ev("question", mid, True, f"devkit · question {mid} from {who}: “{text}” · answer: dev crew answer "
                                          f"{mid} \"…\"", m.get("ts"))
    if m.get("reply_to") or m.get("kind") == "answer":
        return _ev("question", mid, True, f"devkit · answer {mid} from {who} to {m.get('reply_to') or '?'}: "
                                          f"“{text}”", m.get("ts"))
    return _ev("notice", mid, me != "main", f"devkit · notice {mid} from {who}: “{text}”", m.get("ts"))


def _messages(agent, sid, me, mark, want, limit):
    from . import progress
    run = progress.load_run(agent, sid)
    if not run:
        return [], ""

    def pick(r, m):
        return want(_message_event(r, m, me))
    msgs, text = [], ""
    if mark:
        text = progress.messages_context(agent, sid, me, select=pick, limit=limit, taken=msgs)
    else:
        msgs = progress._take_messages(agent, sid, me, mark=False, select=pick, limit=limit)
    if not msgs:
        return [], ""
    run = progress.normalize(progress.load_run(agent, sid) or run)
    evs = [_message_event(run, m, me) for m in msgs]
    if mark:
        for ev in evs:  # a moot question leaves the record told as moot, with why
            if ev["line"].startswith("devkit · moot:"):
                progress._audit("crew.message-moot", ev["id"], ev["line"][:300], "moot",
                                project=run.get("project") or "", why=ev["line"].split("): ", 1)[-1][:300])
    return evs, text


def _decisions(agent, sid, mark, want):
    from . import progress
    if not want(_ev("decision", "", True, "")):
        return [], ""
    new, text = [], ""
    if mark:
        text = progress.decisions_context(agent, sid, taken=new)
    else:
        new = progress.take_decisions(agent, sid, mark=False)[1]
    if not new:
        return [], ""
    evs = [_ev("decision", d["id"], True, f"devkit · decision {d['id']} from {_who(d.get('from_name'))} waits for "
                                          f"the developer: “{clean(progress.ask_text(d))}” · ask it: dev crew "
                                          f"decisions --ask", d.get("ts")) for d in new]
    return evs, text


def _ready(agent, sid, mark, want):
    from . import dispatch
    if not want(_ev("ready", "", True, "")):
        return [], ""
    r, new, mode = dispatch.take_ready(agent, sid, mark=mark)
    if not new:
        return [], ""
    evs = [_ev("ready", s["name"], True, f"devkit · ready: {clean(s['name'], 120)} → {dispatch.launch_line(s)}",
               s.get("added")) for s in new]
    return evs, dispatch.ready_text(r, new, mode)


def _ci(agent, sid, mark, want):
    from . import progress
    if not want(_ev("ci", "", True, "")):
        return [], ""
    told = [e for e in progress._ci(agent, sid, mark) if isinstance(e, dict)]
    evs = [_ev("ci", e.get("sha") or e.get("ts"), True, _ci_line(e.get("text")), e.get("ts")) for e in told]
    return evs, "\n\n".join(e.get("text") or "" for e in told)


def _lifecycle_events(agent: str, s: dict) -> list:
    """(event, hook text, field, agent id) of the members the session record keeps for the main session: finished
    (a notice on Claude Code, whose harness wakes the main session; waking on Codex) and stopped with background
    work still running (a notice: Claude Code lists such a member until that work ends)."""
    from . import hooks
    out = []
    for aid, who in (s.get("crew_finished") or {}).items():
        out.append((_ev("member", aid, agent == "codex", f"devkit · member: {clean(who or 'a crew member', 80)} "
                                                         f"finished (agent {clean(aid, 80)})"),
                    hooks.crew_finished_notice({"crew_finished": {aid: who}}), "crew_finished", aid))
    for aid, rec in (s.get("crew_background") or {}).items():
        rec = rec if isinstance(rec, dict) else {}
        if not [t for t in rec.get("tasks") or [] if isinstance(t, dict) and t.get("id")]:
            continue
        out.append((_ev("member", aid, False, f"devkit · member: {clean(rec.get('who') or 'a crew member', 80)} "
                                              f"stopped with background work still running (agent {clean(aid, 80)})"),
                    hooks.crew_background_notice({"crew_background": {aid: rec}}), "crew_background", aid))
    return out


def _notice_events(s: dict) -> list:
    out = []
    for n in s.get("notices") or []:
        if isinstance(n, dict) and not n.get("told"):
            kind = n.get("kind") if n.get("kind") in KINDS else "notice"
            out.append((_ev(kind, n.get("key"), kind == "member", f"devkit · {kind}: {clean(n.get('text'), 400)}",
                            n.get("ts")), n))
    return out


def _session_items(agent, sid, mark, want):
    """The members and notices the session record keeps for the main session, taken in one locked write."""
    from . import progress
    f = progress.session_path(agent, sid)
    s = state.read(f) or {}
    if not (s.get("crew_finished") or s.get("crew_background") or any(
            isinstance(n, dict) and not n.get("told") for n in s.get("notices") or [])):
        return [], ""  # the usual case, without the lock

    def pick(doc):
        life = [x for x in _lifecycle_events(agent, doc) if want(x[0])]
        notes = [x for x in _notice_events(doc) if want(x[0])]
        return life, notes
    if not mark:
        life, notes = pick(s)
        return [x[0] for x in life] + [x[0] for x in notes], ""
    taken = []

    def take(doc):
        life, notes = pick(doc)
        if not (life or notes):
            return state.SKIP
        for ev, text, field, aid in life:
            (doc.get(field) or {}).pop(aid, None)
            taken.append((ev, text))
        for ev, n in notes:
            n["told"] = round(time.time())
            taken.append((ev, ev["line"]))
    try:
        state.update(f, take, create=False)
    except state.StateBusy:
        return [], ""  # audited by state; what it keeps is told at the next hook or wake
    return [ev for ev, _ in taken], "\n".join(t for _, t in taken if t)


def _sources(agent, sid, me, mark, want, limit) -> list:
    """[(events, hook text)] of every source of the consumer, in delivery order, each taken (`mark`) or peeked."""
    if me != "main":
        return [_messages(agent, sid, me, mark, want, limit)]
    out = []
    for take in (lambda n: _messages(agent, sid, me, mark, want, n), lambda n: _decisions(agent, sid, mark, want),
                 lambda n: _ready(agent, sid, mark, want), lambda n: _ci(agent, sid, mark, want),
                 lambda n: _session_items(agent, sid, mark, want)):
        left = limit - sum(len(e) for e, _ in out)
        if left <= 0:
            break
        try:
            out.append(take(left))
        except state.StateBusy:
            continue  # audited by state; that source tells at the next call
    return out


def _wanted(only):
    kinds = {k for k in (only or ()) if k}
    return lambda ev: not kinds or ev["kind"] in kinds


def _collect(agent, sid, me, mark=True, only=None, wakes_only=False, limit=MAX_EVENTS) -> list:
    ok = _wanted(only)
    if not wakes_only:
        return _sources(agent, sid, me, mark, ok, limit)
    if not any(ev["wakes"] for evs, _ in _sources(agent, sid, me, False, ok, limit) for ev in evs):
        return []
    # what wakes first, then what rides along with it: a hook that took the waking events meanwhile leaves the
    # notices where they are
    woke = _sources(agent, sid, me, mark, lambda ev: ok(ev) and ev["wakes"], limit)
    n = sum(len(e) for e, _ in woke)
    if not n:
        return []
    return woke + _sources(agent, sid, me, mark, lambda ev: ok(ev) and not ev["wakes"], limit - n)


def mail(agent: str, sid: str, me: str = "main", mark: bool = True, only=None, wakes_only: bool = False) -> str:
    """The one place the hooks and the wait get what is new for `me` (the main session or a crew lane's agent id), as
    the hook's context text; each source marks its own items told in the same locked write. `only`: the kinds to
    take (KINDS); `wakes_only`: nothing unless something wakes, then the notices with it."""
    return "\n\n".join(t for _, t in _collect(agent, sid, me, mark, only, wakes_only) if t)


def pending(agent: str, sid: str, me: str = "main") -> list:
    """A peek, marking nothing: [{"kind", "id", "wakes", "line"}] of what the next take would give `me` (a question
    answered or closed before anyone read it is taken with no line)."""
    return [ev for evs, _ in _collect(agent, sid, me, mark=False, limit=10 ** 6) for ev in evs if ev["line"]]


# --------------------------------------------------------------------------- what may still arrive

def can_arrive(agent: str, sid: str, now=None, me: str = "main") -> list:
    """Why events may still come for `me` -> [reason], [] when none can. The main session: a member that works, a
    launch whose agent has not started, a step queued for a crew slot, a CI run this session sent whose devkit watch
    still runs. A member: its own question that waits for an answer, while its session lives."""
    from . import progress
    now = now or time.time()
    sess = state.read(progress.session_path(agent, sid)) or {}
    if not sess or sess.get("ended"):
        return []
    run = progress.load_run(agent, sid)
    live = isinstance(run, dict) and run.get("state") in ("running", "blocked")
    out = []
    if me != "main":
        if live:
            asked = [m.get("id") for m in progress._open_questions(progress.normalize(run)) if m.get("from") == me]
            if asked:
                out.append(f"your question{'s' if len(asked) > 1 else ''} {', '.join(asked)} wait"
                           f"{'' if len(asked) > 1 else 's'} for an answer")
        return out
    if live:
        run = progress.normalize(run)
        busy = [_who(lane.get("persona") or lane.get("label") or aid)
                for aid, lane in progress.working_agents(run, now)]
        if busy:
            out.append(f"{len(busy)} crew member{'s still work' if len(busy) > 1 else ' still works'} "
                       f"({', '.join(busy)})")
        launches = [v for v in (sess.get("launches") or {}).values()
                    if isinstance(v, dict) and v.get("state") == "pending" and now - (v.get("ts") or now) < 900]
        if launches:
            out.append(f"{len(launches)} launch{'es are' if len(launches) > 1 else ' is'} pending")
        try:
            from . import dispatch
            queued = dispatch.ready(run, now)["queued"]
        except Exception:
            queued = []
        if queued and not busy:
            out.append(f"{len(queued)} step{'s wait' if len(queued) > 1 else ' waits'} for a crew slot "
                       f"({', '.join(s['name'] for s in queued[:3])})")
    try:
        from . import ciwatch
        ci = [w for w in ciwatch.watches(now) if w.get("by") == "devkit" and w.get("status") == "live"
              and (w.get("agent"), w.get("session")) == (agent, sid)]
    except Exception:
        ci = []
    out += [f"CI of {w.get('ref')} @ {(w.get('sha') or '')[:7]} has not finished" for w in ci]
    return out


# --------------------------------------------------------------------------- the waits: a record each

def _alive(pid) -> bool:
    from .ciwatch import _alive as alive
    return alive(pid)


def _records() -> list:
    out = []
    for f in sorted(LIVE.glob("*.json")) if LIVE.exists() else []:
        rec = state.read(f)
        if isinstance(rec, dict) and rec.get("id"):
            out.append(rec)
    return out


def _update(rid: str, **fields):
    if not rid:
        return
    try:
        state.update(LIVE / f"{rid}.json", lambda doc: doc.update(fields), create=False)
    except Exception:
        pass  # a wait never fails for its record


def _is_live(rec: dict, now: float) -> bool:
    return rec.get("state") == "live" and _alive(rec.get("pid")) and now < (rec.get("expires") or 0) + 60


def register(agent: str, sid: str, me: str, pid, timeout_min: float, now=None) -> str:
    """Record a wait, and mark every older live wait of the same session and consumer superseded: one live wait per
    consumer, so the older one ends with one line (exit 5). -> its id, "" when it cannot be written."""
    now = now or time.time()
    rid = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"global-{agent}-{sid[:16]}-{me[:24]}-{pid}-{int(now)}-"
                                           f"{os.urandom(3).hex()}")[:200]
    for rec in _records():
        if (rec.get("agent"), rec.get("session"), rec.get("consumer")) == (agent, sid, me) and \
                rec.get("state") == "live":
            _update(rec["id"], state="superseded", superseded_by=rid, ended=round(now))
    rec = {"id": rid, "by": "agent", "kind": "global", "agent": agent, "session": sid, "consumer": me, "pid": pid,
           "started": round(now), "expires": round(now + timeout_min * 60), "timeout_min": timeout_min,
           "state": "live", "last_event": "", "last_ts": None}
    try:
        LIVE.mkdir(parents=True, exist_ok=True)
        state.update(LIVE / f"{rid}.json", lambda doc: rec)
    except Exception:
        return ""
    return rid


def records(now=None) -> list:
    """Every recorded wait to show, newest first, each with its "status": live (its process runs, before its expiry),
    expired (it stopped before anything arrived: a timeout, a kill), superseded (a newer wait took over) or ended
    (it printed what arrived). A record older than LIVE_DAYS is removed."""
    now = now or time.time()
    out = []
    for rec in _records():
        if now - (rec.get("started") or 0) > LIVE_DAYS * 86400:
            (LIVE / f"{rec['id']}.json").unlink(missing_ok=True)
            continue
        st = rec.get("state")
        status = ("live" if _is_live(rec, now) else "expired") if st == "live" else st
        out.append(dict(rec, status=status))
    return sorted(out, key=lambda r: -(r.get("started") or 0))


def armed(agent: str, sid: str, me: str = "main", transcript=None, now=None) -> list:
    """The live waits of this consumer -> [{"id", "how"}]: a recorded `dev watch --wait` whose process runs; with the
    session's transcript, the main session's background command or Monitor running one, as the whole command
    (ciwatch.alone), that no notification or stop has ended (Claude Code), or one run in the foreground in this turn
    (Codex, which has no background wake-up)."""
    now = now or time.time()
    out = [{"id": r["id"], "how": "record"} for r in _records()
           if (r.get("agent"), r.get("session"), r.get("consumer")) == (agent, sid, me) and _is_live(r, now)]
    if transcript and me == "main":
        try:
            from .ciwatch import _shell_calls
            calls, turn = _shell_calls(transcript, now)
        except Exception:
            calls, turn = [], 0
        for i, c in enumerate(calls):
            if not c.get("global"):
                continue
            if c["wake"] != "foreground" and c["live"] and c.get("alone"):
                out.append({"id": c.get("task") or c.get("id") or "", "how": c["wake"]})
            elif c["wake"] == "foreground" and agent == "codex" and i >= turn:
                out.append({"id": c.get("id") or "", "how": "foreground"})
    return out


def _chained(transcript, now) -> bool:
    """Does a live background command or Monitor of the transcript run `dev watch --wait` chained to other work, so
    it wakes the session only when all of it ends?"""
    if not transcript:
        return False
    try:
        from .ciwatch import _shell_calls
        calls, _turn = _shell_calls(transcript, now)
    except Exception:
        return False
    return any(c.get("global") and c["wake"] != "foreground" and c["live"] and not c.get("alone") for c in calls)


def default_timeout(agent: str, me: str) -> float:
    if me != "main":
        return FOREGROUND_MIN
    return CODEX_MIN if agent == "codex" else BACKGROUND_MIN


def again(agent: str, me: str) -> str:
    """The command that arms this consumer's wait again."""
    if me != "main":
        return f"dev watch --wait --for me --timeout {FOREGROUND_MIN}"
    return f"dev watch --wait --timeout {CODEX_MIN}" if agent == "codex" else "dev watch --wait"


# --------------------------------------------------------------------------- the wait

class Changes:
    """The wait's default file-change function: changes(seconds) blocks up to `seconds` and says whether one of
    `files` changed (inode, size or modification time). A state write replaces its file by a rename, so the folders
    are watched (kqueue where the system has it); the files are statted every `poll` seconds in any case. After a
    change, `batch` seconds more let a burst of writes land as one."""

    def __init__(self, files, use_kqueue=None, batch: float = BATCH, poll: float = POLL):
        self.files = [Path(f) for f in files]
        self.batch, self.poll = batch, poll
        self.kq, self.fds = None, []
        if use_kqueue is not False and hasattr(select, "kqueue"):
            try:
                self.kq = select.kqueue()
                events = []
                for d in sorted({str(f.parent) for f in self.files}):
                    Path(d).mkdir(parents=True, exist_ok=True)
                    fd = os.open(d, os.O_RDONLY)
                    self.fds.append(fd)
                    events.append(select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                                                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                                                fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_ATTRIB))
                self.kq.control(events, 0, 0)
            except (OSError, ValueError):
                self.close()
        self.seen = self._stat()

    def _stat(self) -> tuple:
        out = []
        for f in self.files:
            try:
                st = f.stat()
                out.append((st.st_ino, st.st_size, st.st_mtime_ns))
            except OSError:
                out.append(None)
        return tuple(out)

    def _changed(self) -> bool:
        now = self._stat()
        if now == self.seen:
            return False
        self.seen = now
        return True

    def __call__(self, seconds: float) -> bool:
        end = time.monotonic() + max(0.0, float(seconds))
        while True:
            if self._changed():
                if self.batch:
                    time.sleep(self.batch)
                    self.seen = self._stat()
                return True
            left = end - time.monotonic()
            if left <= 0:
                return False
            if self.kq:
                try:
                    self.kq.control(None, 8, min(left, self.poll))
                except (OSError, InterruptedError):
                    time.sleep(min(left, self.poll))
            else:
                time.sleep(min(left, self.poll))

    def close(self):
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds = []
        if self.kq:
            try:
                self.kq.close()
            except OSError:
                pass
        self.kq = None


def _heartbeat(agent: str, sid: str, now: float):
    """While the session is idle: its run's reconcile (lanes that went silent, stopped or finished unverified, launches
    that never started), and the crew limit, whose change frees or holds a queued step."""
    from . import progress
    try:
        progress.reconcile_hook(agent, {"session_id": sid}, now=now)
    except Exception:
        pass
    try:
        progress.parallel_limit(agent, now)
    except Exception:
        pass


def _ended(agent: str, sid: str) -> bool:
    from . import progress
    s = state.read(progress.session_path(agent, sid))
    return not s or bool(s.get("ended"))


def wait(agent: str, sid: str, me: str, timeout_min: float, only=None, out=None, clock=None, changes=None) -> int:
    """`dev watch --wait`: block until at least one event `me` must act on arrives for this session, print each once
    (one line, at most MAX_EVENTS, then the notices that ride along) and exit 0. -> 0 events printed, 2 nothing can
    arrive (said at once), 3 the timeout or a kill (arm it again), 5 a newer wait of the same consumer took over, 1
    no session. The wait is recorded (`dev watches`) and each wake audited (watch.wake)."""
    from . import progress
    out = out or sys.stdout
    clock = clock or time.time

    def emit(line):
        out.write(line + "\n")
        out.flush()
    if not progress.session_path(agent, sid).exists():
        emit(f"watch: no devkit session {agent}:{sid[:12]} on this Mac: run it from the agent session that waits")
        return EXIT["error"]
    start = clock()
    rid = register(agent, sid, me, os.getpid(), timeout_min, now=start)
    arm = again(agent, me)

    def finish(state_, line, code):
        _update(rid, state=state_, ended=round(clock()), last_event=line[:300], last_ts=round(clock()))
        emit(line)
        return code

    def killed(signum, frame):
        finish("expired", f"watch: stopped before anything arrived · arm again: {arm}", 0)
        raise SystemExit(EXIT["timeout"])
    saved = {}
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            saved[sig] = signal.signal(sig, killed)
        except (ValueError, OSError):  # not the main thread: the record shows it expired once its process is gone
            pass
    watcher = None
    try:
        delivered = _deliver(agent, sid, me, only, emit, rid, arm, start, clock)
        if delivered is not None:
            return delivered
        why = can_arrive(agent, sid, clock(), me)
        if not why:
            return finish("ended", ("watch: nothing can arrive for you: none of your questions waits for an answer."
                                    if me != "main" else
                                    "watch: nothing can arrive for this session: no crew member works, no launch is "
                                    "pending, no step waits for a slot and no CI run is unfinished.")
                          + " End the turn without a wait.", EXIT["nothing"])
        progress_files = [progress.run_path(agent, sid), progress.session_path(agent, sid), LIVE / f"{rid}.json"]
        watcher = changes or Changes(progress_files)
        deadline, beat = start + timeout_min * 60, clock()
        while True:
            rec = state.read(LIVE / f"{rid}.json") or {}
            if rec.get("state") == "superseded":
                emit("watch: a newer wait of this session took over; this one ends")
                return EXIT["superseded"]
            if _ended(agent, sid):
                return finish("ended", "watch: the session ended", EXIT["nothing"])
            now = clock()
            if now >= deadline:
                return finish("expired", f"watch: nothing arrived in {timeout_min:g} min · arm again: {arm}",
                              EXIT["timeout"])
            changed = watcher(max(0.0, min(deadline - now, HEARTBEAT - (now - beat))))
            if clock() - beat >= HEARTBEAT:
                beat = clock()
                _heartbeat(agent, sid, beat)
                changed = True
            if changed and (state.read(LIVE / f"{rid}.json") or {}).get("state") != "superseded":
                delivered = _deliver(agent, sid, me, only, emit, rid, arm, start, clock)
                if delivered is not None:
                    return delivered
    finally:
        if changes is None and callable(getattr(watcher, "close", None)):
            watcher.close()
        for sig, handler in saved.items():
            signal.signal(sig, handler)


def _deliver(agent, sid, me, only, emit, rid, arm, start, clock):
    """Take what wakes this consumer, with what rides along, and print it -> 0, or None when nothing woke it."""
    taken = [ev for evs, _ in _collect(agent, sid, me, True, only, wakes_only=True) for ev in evs if ev["line"]]
    if not taken:
        return None
    for ev in taken:
        emit(ev["line"])
    more = [ev for ev in pending(agent, sid, me) if not only or ev["kind"] in only]
    if more:
        emit(f"more: {len(more)} event{'s' if len(more) > 1 else ''} wait · dev watch shows them; the next wait takes "
             "them at once")
    n = len(taken)
    tail = f"watch: {n} event{'s' if n > 1 else ''} · " + (f"arm again: {arm}" if me == "main" else
                                                          f"continue with it; wait again only while still blocked: "
                                                          f"{arm}")
    emit(tail)
    now = clock()
    ts = [float(ev["ts"]) for ev in taken if isinstance(ev.get("ts"), (int, float))]
    _update(rid, state="ended", ended=round(now), last_event=taken[0]["line"][:300], last_ts=round(now))
    try:  # the wake latency: from the oldest event's write to this exit
        from . import audit
        kinds = ", ".join(sorted({ev["kind"] for ev in taken}))
        latency = round(now - min(ts), 1) if ts else None
        audit.record("watch.wake", f"{agent}:{sid[:12]} for {me}", f"{kinds} · {n} event{'s' if n > 1 else ''} · "
                     f"waited {round(now - start)} s", "ok", count=n, duration=latency, session=sid)
    except Exception:
        pass
    return EXIT["events"]


# --------------------------------------------------------------------------- notices from outside the run

def notice(agent: str, sid: str, kind: str, key: str, text: str) -> bool:
    """A notice for the session's main consumer from a source outside its run (a lane the sweep closed, the crew
    limit, a handoff push): kept in the session record until a hook or a wait tells it, once. Idempotent by key:
    the same fact twice is one notice. A "member" notice wakes the wait; any other rides along. -> it was added"""
    from . import progress
    f = progress.session_path(agent, sid)
    if not f.exists():
        return False
    added = []

    def add(s):
        items = [n for n in s.get("notices") or [] if isinstance(n, dict)]
        if any(n.get("key") == key for n in items):
            return state.SKIP
        items.append({"key": str(key)[:160], "kind": kind if kind in KINDS else "notice",
                      "text": str(text or "")[:600], "ts": round(time.time()), "told": False})
        while len(items) > NOTICES_MAX:
            old = next((i for i, n in enumerate(items) if n.get("told")), 0)
            items.pop(old)
        s["notices"] = items
        added.append(True)
    try:
        state.update(f, add, create=False)
    except Exception:
        return False
    return bool(added)


def notice_all(kind: str, key: str, text: str, agent: str | None = None) -> int:
    """notice() to the main session of every live run on this Mac (of `agent`'s runs only, when given). -> how many"""
    from . import progress
    n = 0
    for r in progress.active_runs():
        if r.get("state") in ("running", "blocked") and r.get("session") and (not agent or r.get("agent") == agent):
            n += notice(r.get("agent") or "claude", r["session"], kind, key, text)
    return n


def lane_notices(run: dict, moves: list):
    """approvals' transitions: each lane that stopped, was lost or ended unverified is a member notice for the main
    session of its run, with the steps that moved with it."""
    if not isinstance(run, dict) or not run.get("session"):
        return
    words = {"stopped": "was stopped", "lost": "was lost", "unverified": "ended unverified"}
    for m in moves or []:
        if m.get("kind") != "lane" or m.get("to") not in words:
            continue
        aid = m.get("id") or ""
        lane = (run.get("agents") or {}).get(aid) or {}
        steps = [x for x in moves if x.get("kind") == "step" and x.get("owner_from") == aid]
        waits = [x["id"] for x in steps if x.get("to") == "pending"]
        held = [x["id"] for x in steps if x.get("to") == "running"]
        bad = [x["id"] for x in steps if x.get("to") == "unverified"]
        tail = "".join([f"; {'its steps' if len(waits) > 1 else 'its step'} {', '.join(waits)} "
                        f"wait{'' if len(waits) > 1 else 's'} for a new launch" if waits else "",
                        f"; a live lane of its member holds {', '.join(held)}" if held else "",
                        f"; {', '.join(bad)} unverified: no report, so no evidence" if bad else ""])
        why = str(m.get("why") or "").split(";")[0].strip()
        text = f"{_who(lane.get('persona') or lane.get('label') or aid)} {words[m['to']]}" + \
               (f" ({why})" if why else "") + tail
        notice(run.get("agent") or "claude", run["session"], "member",
               f"lane:{aid}:{m['to']}:{int(m.get('at') or time.time())}", text)


# --------------------------------------------------------------------------- the end of a turn

def stop_gate(agent: str, data: dict, now=None) -> str | None:
    """The Stop hook, for the main session, once per turn. A turn that ends while events can still arrive (a member
    works, a launch is pending, a step waits for a slot, CI this session sent has not finished) must leave a wait
    that wakes the session: on Claude Code a live `dev watch --wait` (its record, or a background command or Monitor
    of the transcript); on Codex one run in the foreground in this turn. -> the message that sends the turn back
    once, or None."""
    if data.get("stop_hook_active") or data.get("agent_id"):
        return None
    now = now or time.time()
    sid = data.get("session_id") or ""
    why = can_arrive(agent, sid, now)
    if not why or armed(agent, sid, "main", data.get("transcript_path"), now):
        return None
    head = "devkit: " + "; ".join(why) + ": their questions and results can arrive while you are idle, and nothing " \
                                         "wakes you."
    if agent != "codex" and _chained(data.get("transcript_path"), now):
        from .ciwatch import CHAINED
        head += " " + CHAINED
    if agent == "codex":
        return (head + f" Codex has no background wake-up: run `dev watch --wait --timeout {CODEX_MIN}` in the "
                "foreground now and act on what it prints, or end the turn saying what still runs and that you check "
                "it at the developer's next message.")
    return (head + " Before you end the turn, run `dev watch --wait` with Bash run_in_background, name its task id, and "
            "arm it again after each wake. One wait covers the CI results too.")


# --------------------------------------------------------------------------- dev watch, dev watches

def _consumer(a) -> str:
    """The caller's own consumer. A call the hooks routed to a crew lane (DEVKIT_FROM_AGENT) is that member's. Else
    `--for me` is the member the call ledger names, refused when it names none; and a wait without it is always the
    main session's: a member's open `dev watch --wait` in the ledger must never turn the tech lead's wait into the
    member's, which would take the member's answers and leave the tech lead's events waking nobody."""
    from . import progress
    for_ = getattr(a, "for_", None)
    if for_ not in (None, "me"):
        raise ValueError("--for takes `me`: a wait is always for its caller (the main session, or the member's own "
                         "lane)")
    routed = os.environ.get("DEVKIT_FROM_AGENT") or None
    if routed:
        return routed
    if for_ == "me":
        lane = progress._as_lane(None)
        if not lane:
            raise ValueError("--for me is for a crew member's own wait, and devkit cannot tell which member calls: "
                             "run it as a command of its own from the member's lane")
        return lane
    return "main"


def _only(a) -> list:
    kinds = [k.strip() for k in (getattr(a, "only", None) or "").split(",") if k.strip()]
    bad = [k for k in kinds if k not in KINDS]
    if bad:
        raise ValueError(f"--only takes {', '.join(KINDS)}: not {', '.join(bad)}")
    return kinds


def cmd_watch(a) -> int:
    """`dev watch` (a peek: what waits for you, what can still arrive, the live waits) and `dev watch --wait`."""
    from . import progress
    agent, sid, how = progress.resolve_session()
    if how == "shell":
        print("watch: no agent session here: dev watch runs in the agent session that waits (Claude Code, Codex)")
        return EXIT["error"]
    try:
        me, only = _consumer(a), _only(a)
    except ValueError as e:
        print(f"watch: {e}")
        return EXIT["error"]
    if getattr(a, "wait", False):
        timeout = float(a.timeout) if getattr(a, "timeout", None) else default_timeout(agent, me)
        return wait(agent, sid, me, timeout, only=only)
    now = time.time()
    evs = [ev for ev in pending(agent, sid, me) if not only or ev["kind"] in only]
    run = progress.normalize(progress.load_run(agent, sid) or {})
    moot = progress.moot_messages(run) if run and me == "main" else []
    why = can_arrive(agent, sid, now, me)
    live = armed(agent, sid, me, None, now)
    if getattr(a, "json", False):
        print(json.dumps({"session": f"{agent}:{sid}", "consumer": me, "events": evs, "can_arrive": why,
                          "armed": live, "moot": [{"id": m.get("id"), "why": progress.moot_message_reason(run, m)}
                                                  for m in moot]}, ensure_ascii=False))
        return 0
    print(f"watch · {agent}:{sid[:12]} · for {_consumer_name(agent, sid, me)}")
    print("\n".join(f"  {ev['line']}" + ("" if ev["wakes"] else "  (rides along)") for ev in evs) or "  nothing new")
    seen = {ev["id"] for ev in evs}
    for m in moot:  # told already, and still unanswered: nobody waits on it any more
        if m.get("id") not in seen:
            print(f"  devkit · moot: question {m.get('id')} from {_who(m.get('from_name'))} "
                  f"(“{clean(m.get('text'))}”): {progress.moot_message_reason(run, m)}")
    print("can still arrive: " + ("; ".join(why) if why else "nothing"))
    print("armed: " + (", ".join(w["id"] for w in live) if live else f"no wait · arm one: {again(agent, me)}"))
    return 0


def cmd_watches(a, out=None) -> int:
    """`dev watches`: every recorded wait (global and CI), live, expired or superseded, with the command to arm it
    again, and what can still arrive for this session."""
    from . import ciwatch, progress
    out = out or sys.stdout
    now = time.time()
    ws = [w for w in records(now) if w["status"] != "ended"]
    cis = ciwatch.watches(now)
    agent, sid, how = progress.resolve_session()
    why = can_arrive(agent, sid, now) if how != "shell" else []
    if getattr(a, "json", False):
        out.write(json.dumps({"waits": ws, "ci": cis, "can_arrive": why}, ensure_ascii=False) + "\n")
        out.flush()
        return 0

    def at(ts):
        return time.strftime("%H:%M", time.localtime(ts)) if ts else "-"
    lines = ["Waits: dev watch --wait (live, expired before anything arrived, superseded by a newer one)"]
    for w in ws:
        who = _consumer_name(w.get("agent") or "", w.get("session") or "", w.get("consumer") or "main")
        lines.append(f"  {w['status']:<10} {w.get('agent')} session {(w.get('session') or '-')[:8]} · for {who} · "
                     f"started {at(w.get('started'))}" + (f" · expires {at(w.get('expires'))}"
                                                          if w["status"] == "live" else ""))
        if w["status"] in ("expired", "superseded"):
            lines.append(f"             arm again: {again(w.get('agent') or '', w.get('consumer') or 'main')}")
    if not ws:
        lines.append("  none")
    lines.append("CI watches: dev ci watches")
    lines += [f"  {w['status']:<10} {w['repo']} {w['ref']} @ {w['sha'][:7]} · by "
              f"{'devkit' if w.get('by') == 'devkit' else 'the agent'}" for w in cis] or ["  none"]
    if how != "shell":
        lines.append("Can still arrive for this session: " + ("; ".join(why) if why else "nothing"))
    out.write("\n".join(lines) + "\n")
    out.flush()
    return 0
