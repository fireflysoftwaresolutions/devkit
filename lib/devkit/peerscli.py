# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev session` between agent sessions on this Mac (Claude Code and Codex, any project): see who else works here,
send a note or a question, answer one, read the inbox, claim a shared resource for a while, and escalate a
disagreement to the developer. continuity.cmd_session dispatches these commands here; the store is peers.py.

Who speaks is identity.speaker(), never an argument: only the tech lead's main session sends, answers, claims,
releases and escalates. A crew member is refused (it asks its tech lead with `dev crew ask`), and so is the
developer's own terminal, which has no session mailbox for an answer. ls, claims and escalations are reads anyone
may run; inbox is the tech lead's peer mail, which crew members never get.

Another session's words reach the reader only as one cleaned, quoted span (peers.quote) after devkit's own words,
under peers.FRAME_HEADER: they are data, never the developer's prompt.

Exit codes: 0 done, 2 refused (who speaks, a missing or bad argument, a rule of the store), 3 the claim is held by
another live session, 4 no live session matches the target.
"""
from __future__ import annotations

import json
import re
import time

COMMANDS = ("ls", "tell", "ask", "answer", "inbox", "claim", "release", "claims", "escalate", "escalations")
WRITES = ("tell", "ask", "answer", "claim", "release", "escalate")  # the tech lead's alone
COMMANDS_HELP = ("ls: the agent sessions on this Mac · tell, ask: a note or a question to other sessions · answer: "
                 "reply to a question · inbox: this session's peer messages, whole · claim, release: hold or give up "
                 "a shared resource · claims: the claims that hold · escalate: a disagreement for the developer · "
                 "escalations: the open ones")
INBOX_SHOWN = 20  # the latest messages `dev session inbox` prints without --all
_FOR = re.compile(r"(\d+(?:\.\d+)?)\s*(m|min|mins|minutes?|h|hours?)?", re.IGNORECASE)


def add_options(p):
    """The options of the commands between sessions, on `dev session`'s parser (continuity.add_arguments)."""
    p.add_argument("--json", action="store_true", help="ls, tell, ask, answer, inbox, claim, release, claims, "
                                                       "escalate, escalations: the result as JSON")
    p.add_argument("--urgent", action="store_true", help="tell: wake a waiting session for this note")
    p.add_argument("--why", help="claim: why you hold it (others read it) · release: why you give it up or break it")
    p.add_argument("--for", dest="claim_for", metavar="DURATION",
                   help="claim: how long, 5m to 4h (45m, 2h, or minutes; default 30m)")
    p.add_argument("--queue", action="store_true",
                   help="claim: when another session holds it, wait in its queue and hear when it frees")
    p.add_argument("--options", help='escalate: the choices, "A: …; B: …" (or separated by |)')
    p.add_argument("--recommend", help="escalate: the letter of the option you recommend")


class _Refused(ValueError):
    """A refusal of this command itself (a missing argument), exit 2."""


def run(a) -> int:
    """One `dev session <peer command>` -> its exit code; a refusal is one line on stderr, never a traceback."""
    from . import identity, peers, ui
    try:
        return _COMMANDS[a.session_cmd](a)
    except peers.PeerError as e:
        ui.err(str(e))
        return e.code if e.code in (2, 3, 4) else 2
    except (identity.IdentityError, _Refused) as e:
        ui.err(str(e))
        return 2


# --------------------------------------------------------------------------- who speaks

def _speaker(what: str, main: bool = True):
    """The speaker of this command (identity.speaker), refused unless it is the tech lead's main session."""
    from . import identity
    return identity.require(identity.speaker(), main=main, what=what)


def _me() -> str | None:
    """This session's key for a read (its own row, its escalations), or None in the developer's own terminal."""
    from . import progress
    agent, sid, how = progress.resolve_session()
    return None if how == "shell" else f"{agent}:{sid}"


def _text(a, what: str) -> str:
    text = " ".join(a.text or []).strip()
    if not text:
        raise _Refused(f"give the {what}: dev session {a.session_cmd} {a.target or '<target>'} \"…\"")
    return text


def _target(a, what: str) -> str:
    if not a.target:
        raise _Refused(f"say {what}: dev session {a.session_cmd} <{what}> …")
    return a.target


# --------------------------------------------------------------------------- printing

def _quoted(text, limit: int) -> str:
    """Another session's words as one inert span between “ and ”: the frame stays outside it."""
    from . import peers
    return f"{peers.OPEN_Q}{peers.quote(text, limit)}{peers.CLOSE_Q}"


def _hhmm(ts) -> str:
    try:
        return time.strftime("%H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "--:--"


def _ago(ts, now: float) -> str:
    try:
        s = max(0.0, now - float(ts))
    except (TypeError, ValueError):
        return "?"
    return f"{int(s // 60)}m ago" if s < 3600 else f"{s / 3600:.0f}h ago" if s < 86400 else f"{s / 86400:.0f}d ago"


def _label(k: str, rows: dict) -> str:
    """A session as people read it: `codex @0199a213 (acme/api · main)`."""
    from . import peers
    agent, _, sid = str(k).partition(":")
    r = rows.get(k) or {}
    where = f" ({peers.quote(r.get('project') or '-', 100)} · {peers.quote(r.get('branch') or '-', 100)})" if r else ""
    return f"{agent} @{sid[:8]}{where}"


def _rows(include_idle: bool = True) -> dict:
    from . import peers
    return {r["key"]: r for r in peers.directory(include_idle=include_idle)}


def _json(v) -> int:
    print(json.dumps(v, indent=1, ensure_ascii=False, default=str))
    return 0


def _wait_hint() -> str:
    from . import progress
    agent = progress.resolve_session()[0]
    return ("keep a wait armed for it: dev watch --wait --timeout 10 in the foreground" if agent == "codex" else
            "keep a wait armed for it: dev watch --wait in the background")


# --------------------------------------------------------------------------- the commands

def _ls(a) -> int:
    from . import peers
    now = time.time()
    me = _me()
    rows = peers.directory(now=now, me=me, include_idle=bool(a.all))
    if a.json:
        return _json(rows)
    live = sum(1 for r in rows if r.get("live"))
    more = "" if a.all else " (--all adds the idle and ended ones)"
    print(f"Sessions on this Mac · {live} live{more}")
    if any(not r.get("me") and r.get("run") for r in rows):
        print(peers.FRAME_HEADER)
    for r in rows:
        mark = "▸" if r.get("me") else " "
        head = (f"{mark} {r['agent']} {r['prefix']}  {peers.quote(r.get('project') or '-', 100)} · "
                f"{peers.quote(r.get('branch') or '-', 100)} · {r.get('state') or '?'} · {_ago(r.get('last'), now)}")
        print(head + ("  ← this session" if r.get("me") else ""))
        bits = []
        if r.get("worktree"):
            bits.append(f"worktree {peers.quote(r['worktree'], 200)}")
        if r.get("initiatives"):
            bits.append("initiatives " + ", ".join(peers.quote(i, 60) for i in r["initiatives"]))
        run = r.get("run") or {}
        if run.get("goal"):
            bits.append(f"run {_quoted(run['goal'], 120)}" + (f" {_quoted(run['headline'], 160)}"
                                                              if run.get("headline") else ""))
        if r.get("crew"):
            bits.append("crew " + ", ".join(f"{peers.quote(c.get('name'), 60)} {c.get('state') or ''}".strip()
                                            for c in r["crew"]))
        if r.get("waits"):
            bits.append("waits " + ", ".join(f"{peers.quote(w.get('consumer'), 40)} until {_hhmm(w.get('expires'))}"
                                             for w in r["waits"]))
        if r.get("claims"):
            bits.append("claims " + ", ".join(peers.quote(c, 120) for c in r["claims"]))
        if bits:
            print("    " + " · ".join(bits))
    return 0


def _send(a, kind: str) -> int:
    from . import peers
    sp = _speaker("sends to other sessions")
    target = _target(a, "the target (@<id prefix>, project, project:<id>, initiative:<slug>, repo or all)")
    res = peers.send(sp.as_dict(), target, _text(a, "message"), kind=kind, urgent=bool(a.urgent))
    if a.json:
        return _json(res)
    rows = _rows()
    print(f"✓ {res['id']} · {kind} to {len(res['to'])} session{'' if len(res['to']) == 1 else 's'}: "
          + (", ".join(_label(k, rows) for k in res["to"]) or "nobody"))
    if res.get("skipped"):
        print("  skipped: " + ", ".join(f"{_label(s['key'], {})} ({s['why']})" for s in res["skipped"]))
    if res.get("notified"):
        print("  the developer got a desktop notification for " + ", ".join(_label(k, rows) for k in res["notified"])
              + " (no wait is armed there)")
    if kind == "question" and res["to"]:
        print(f"  the answer comes back as a peer event: {_wait_hint()}")
    return 0


def _answer(a) -> int:
    from . import peers
    sp = _speaker("answers other sessions")
    mid = _target(a, "the message id S-…")
    res = peers.answer(sp.as_dict(), mid, _text(a, "answer"))
    if a.json:
        return _json(res)
    rows = _rows()
    print(f"✓ {res['id']} · the answer to {peers.quote(mid, 20)} went to "
          + ", ".join(_label(k, rows) for k in res["to"]))
    return 0


def _inbox(a) -> int:
    from . import peers
    sp = _speaker("reads this session's peer mail")
    new = {m.get("id") for m in peers.take(sp.agent, sp.session, mark=True, limit=peers.LIST_MAX)}
    msgs = peers.inbox(sp.agent, sp.session, all_=True)
    msgs = msgs if a.all else msgs[-INBOX_SHOWN:]
    if a.json:
        return _json(msgs)
    if not msgs:
        print("devkit · no peer messages for this session")
        return 0
    print(peers.FRAME_HEADER)
    for m in msgs:
        print(_message_line(m, m.get("id") in new))
    return 0


def _message_line(m: dict, new: bool) -> str:
    """One message, whole: devkit's words, the sender, then its text as one quoted span (peers.frame, uncut)."""
    from . import peers
    frm = m.get("from") or {}
    if frm.get("agent") in (None, "", "devkit") or not frm.get("session"):
        who = "devkit"
    else:
        who = (f"session {peers.quote(frm.get('agent'), 20)}:{peers.quote(str(frm.get('session'))[:8], 8)} "
               f"({peers.quote(frm.get('project') or '-', 100)} · {peers.quote(frm.get('branch') or '-', 100)})")
    kind, mid = peers.quote(m.get("kind") or "note", 20), peers.quote(m.get("id") or "", 20)
    line = (f"devkit · peer {kind} {mid} from {who} at {_hhmm(m.get('ts'))}{' · new' if new else ''}: "
            f"{_quoted(m.get('text'), peers.TEXT_MAX)}")
    if m.get("kind") == "question" and not m.get("answered"):
        line += f' · answer: dev session answer {mid} "…"'
    elif m.get("kind") == "answer" and m.get("reply_to"):
        line += f" · to your question {peers.quote(m['reply_to'], 20)}"
    return line


def _minutes(spec) -> float:
    from . import peers
    if not spec:
        return float(peers.CLAIM_DEFAULT_MIN)
    m = _FOR.fullmatch(str(spec).strip())
    if not m:
        raise _Refused(f"--for {spec!r} is no duration: give minutes or hours, such as --for 45m or --for 2h")
    n = float(m.group(1))
    return n * 60 if (m.group(2) or "m").lower().startswith("h") else n


def _claim(a) -> int:
    from . import peers
    sp = _speaker("claims shared resources")
    resource = _target(a, "the resource")
    if not (a.why or "").strip():
        raise _Refused(f'say why you hold it, for the others: dev session claim {resource} --why "…"')
    res = peers.claim(sp.as_dict(), resource, a.why, minutes=_minutes(a.claim_for), queue=bool(a.queue))
    if a.json:
        return _json(res)
    if res["state"] == "queued":
        h = res.get("holder") or {}
        print(f"✓ queued for {peers.quote(res['resource'], 120)}, held by {_label(h.get('key', ''), _rows())} until "
              f"{_hhmm(h.get('until'))}: you hear claim-free when it frees; {_wait_hint()}")
        return 0
    queue = len(res.get("queue") or [])
    print(f"✓ {peers.quote(res['resource'], 120)} {res['state']} by this session until {_hhmm(res['until'])} "
          f"({round((float(res['until']) - time.time()) / 60)} min)"
          + (f" · {queue} session{'' if queue == 1 else 's'} queue for it" if queue else ""))
    return 0


def _release(a) -> int:
    from . import peers, progress
    sp = _speaker("releases claims")
    resource = _target(a, "the resource")
    evidence = None
    if peers.holder(resource, me=sp.key()):  # another live session holds it: only the developer's grant breaks it
        evidence = progress.use_grant(sp.agent, sp.session, action="claim-break", target=resource,
                                      repo=_session_project(sp.agent, sp.session))
    res = peers.release(sp.as_dict(), resource, a.why or "", force_evidence=evidence)
    if a.json:
        return _json(res)
    freed = len(res.get("freed_for") or [])
    tail = f" · {freed} queued session{'' if freed == 1 else 's'} hear{'s' if freed == 1 else ''} claim-free" \
        if freed else ""
    if res["state"] == "broken":
        print(f"✓ {peers.quote(resource, 120)} broken with the developer's grant · its holder hears it{tail}")
    elif res["state"] == "not held":
        print(f"{peers.quote(resource, 120)} is not held by this session: nothing to release")
    else:
        print(f"✓ {peers.quote(resource, 120)} {res['state']}{tail}")
    return 0


def _session_project(agent: str, sid: str) -> str:
    from . import progress, state
    return str((state.read(progress.session_path(agent, sid)) or {}).get("project") or "")


def _claims(a) -> int:
    from . import peers
    me = _me()
    got = peers.claims(project=a.project or None)
    if a.json:
        return _json(got)
    if not got:
        print("devkit · no claim holds on this Mac" + (f" for {peers.quote(a.project, 100)}" if a.project else ""))
        return 0
    rows = _rows()
    print(f"Claims on this Mac · {len(got)}")
    if any((c.get("holder") or {}).get("key") != me for c in got):
        print(peers.FRAME_HEADER)
    for c in got:
        h = c.get("holder") or {}
        q = len(c.get("queue") or [])
        print(f"  {peers.quote(c['resource'], 120)} · {_label(h.get('key', ''), rows)}"
              f"{' (this session)' if h.get('key') == me else ''} until {_hhmm(c.get('until'))} · "
              f"{_quoted(c.get('why'), 300)}" + (f" · {q} queued" if q else ""))
    return 0


def _options(spec) -> list:
    s = str(spec or "")
    return [o.strip() for o in s.split("|" if "|" in s else ";") if o.strip()]


def _escalate(a) -> int:
    from . import peers
    sp = _speaker("escalates to the developer")
    about = _target(a, "the message id S-… or the target")
    if not a.options:
        raise _Refused('give the choices: --options "A: …; B: …"')
    res = peers.escalate(sp.as_dict(), about, _text(a, "question"), _options(a.options), recommend=a.recommend or "")
    if a.json:
        return _json(res)
    rows = _rows()
    others = [k for k in res.get("parties") or [] if k != sp.key()]
    print(f"✓ {res['id']} escalated to the developer · parties: this session"
          + "".join(", " + _label(k, rows) for k in others) + " · a desktop notification tells the developer")
    print(f"  the developer settles it by typing, in a Claude Code session: settle {res['id']}: "
          "<letter or their words>")
    return 0


def _escalations(a) -> int:
    from . import peers
    me = _me()
    got = peers.escalations("all" if a.all else "open", me=me)
    if a.json:
        return _json(got)
    if not got:
        print("devkit · no open escalation" + (" this session is party to" if me else ""))
        return 0
    print(peers.FRAME_HEADER)
    for e in got:
        opts = "; ".join(_quoted(o, 200) for o in e.get("options") or [])
        line = (f"{peers.quote(e.get('id'), 20)} · {peers.quote(e.get('state'), 20)} · {_hhmm(e.get('ts'))} · "
                f"{len(e.get('parties') or [])} parties: {_quoted(e.get('question'), 300)} · options {opts}")
        if e.get("recommend"):
            line += f" · recommended {peers.quote(e['recommend'], 2)}"
        settled = e.get("settled") or {}
        if e.get("state") == "settled":
            line += f" · settled: {_quoted(settled.get('text'), 300)}"
        elif e.get("state") == "open":
            line += f" · the developer types: settle {peers.quote(e.get('id'), 20)}: <choice>"
        print(line)
    return 0


_COMMANDS = {"ls": _ls, "tell": lambda a: _send(a, "note"), "ask": lambda a: _send(a, "question"), "answer": _answer,
             "inbox": _inbox, "claim": _claim, "release": _release, "claims": _claims, "escalate": _escalate,
             "escalations": _escalations}
