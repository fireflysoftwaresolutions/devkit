# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Who is speaking: one definition for every devkit command that reads or writes state kept per consumer (the main
session, or one crew member's lane): `dev watch`, the call ledger's readers, session-to-session messages and claims.

A hook knows who calls from its own payload: Claude Code and Codex pass `agent_id` to every hook fired inside a crew
member, and nothing for the main session (from_hook). A `dev` command has no payload. It takes, in order, the first
rule that applies (speaker):

1. no agent session runs it (progress.resolve_session finds none, or devkit read the process's parents, none is a
   session's, and it carries no agent's marker, as in the developer's own terminal beside a session that just
   worked in the repository): the developer's own shell, `human`;
2. DEVKIT_FROM_AGENT=<agent id>, which the PreToolUse hook puts in front of a member's lane calls: that lane;
3. DEVKIT_SPEAKER=main, which the PreToolUse hook puts in front of the main session's own `dev watch` and
   `dev session` calls: the main session, in a session not guessed by recency. Variables only narrow: when 2 also
   holds, the lane wins, and so does a member whose own open call typed exactly this command (a member the guard
   should have stopped setting it);
4. the call ledger: an open call of a member whose text holds this command makes it that member's (the most
   restrictive one, as progress.caller decides); open calls of main's only, in a session not guessed by recency,
   make it main's;
5. no crew member of the session's run may still be speaking (no running lane that has not ended, no launch whose
   agent has not started), in a session not guessed by recency: the main session, which nobody else could be;
6. otherwise devkit does not know (`unknown`), and a command that needs to know refuses. A session found only by
   recency (an agent's command whose process no session record holds) always ends here or at a member.

A wait (consumer) reads the ledger more strictly: a call counts only when it typed this very `dev` command, nothing
more, so the tech lead's `dev watch --wait` never becomes a member's because the member's open
`dev watch --wait --timeout 9` holds the same words.

Codex marks a spawned agent's hooks with its agent_id, but the main thread's hooks carry none, so a Codex speaker that
devkit takes for main is not verified (`verified` False); progress.caller keeps reporting Codex as `unverified`.
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from dataclasses import dataclass

LANE_VAR = "DEVKIT_FROM_AGENT"  # a crew member's lane call, stamped by the PreToolUse hook
SPEAKER_VAR = "DEVKIT_SPEAKER"  # "main": the main session's own `dev watch` or `dev session` call, stamped likewise
LAUNCH_WAIT = 900  # seconds a launch whose agent has not started still counts as someone who may speak


@dataclass(frozen=True)
class Speaker:
    agent: str  # "claude" | "codex" | "human"
    session: str  # the session id ("shell-<ppid>" for a developer's shell)
    lane: str  # "main" | a crew member's agent_id | "" (not known)
    how: str  # "payload" | "stamped" | "ledger" | "sole" | "human" | "unknown"
    verified: bool  # False for a Codex main session (its hooks carry no mark) and for anyone devkit cannot name

    def key(self) -> str:
        return f"{self.agent}:{self.session}"

    def as_dict(self) -> dict:
        return {"agent": self.agent, "session": self.session, "lane": self.lane, "how": self.how,
                "verified": self.verified}


class IdentityError(ValueError):
    """devkit refuses: the speaker is not who this command needs, or devkit cannot tell who it is."""


@dataclass(frozen=True)
class _Found:
    speaker: Speaker
    agent: str  # the agent as resolve_session names it (DEVKIT_AGENT or "human" for a shell)
    session_how: str  # resolve_session's how: ancestry, ledger, env, recency, shell
    calls: tuple  # tool_use_ids of the session's open calls whose text holds this command
    ledger: str  # who the ledger names: "main", a member's agent id, or "" (nobody, or main in a guessed session)


def from_hook(agent: str, data: dict) -> Speaker:
    """Who a hook acts for, from its own payload: `agent_id` is a crew member's lane, its absence the main session."""
    sid = str((data or {}).get("session_id") or "")
    aid = str((data or {}).get("agent_id") or "")[:80]
    if aid:
        return Speaker(agent, sid, aid, "payload", True)
    return Speaker(agent, sid, "main", "payload", agent != "codex")


def speaker(cwd: str | None = None, argv=None, now: float | None = None) -> Speaker:
    """Who runs this `dev` command (the rules in the module's docstring). `argv`: its arguments, default
    sys.argv[1:], which the call ledger matches."""
    return _find(cwd, argv, now).speaker


def require(sp: Speaker, *, main: bool = False, what: str = "") -> Speaker:
    """`sp`, or IdentityError: devkit cannot tell who speaks; with `main`, a crew member, or the developer's own
    terminal, which has no agent session to act for. `what` says what the command does ("sends to other sessions")."""
    does = what or "runs this command"
    if sp.how == "unknown":
        raise IdentityError(f"devkit cannot tell whether the tech lead or a crew member {does}: run the dev command "
                            "as a command of its own, not in a pipeline, a script or another shell")
    if main and sp.how == "human":
        raise IdentityError(f"the developer's own terminal has no agent session, and only the tech lead {does}: "
                            "type it in the agent session instead")
    if main and sp.lane != "main":
        raise IdentityError(f"only the tech lead {does}: ask them with dev crew ask \"…\"")
    return sp


def consumer(for_me: bool, cwd: str | None = None) -> str:
    """Whose wait `dev watch` is: "main" or a crew member's lane. `--for me` is a member's own wait, refused when it
    resolves to the main session; without it, the speaker's own (a member's call makes it the member's). A speaker
    devkit cannot name is refused: guessing would let one wait take the other's events."""
    sp = _find(cwd, None, None, exact=True).speaker
    if sp.how == "human":
        raise IdentityError("no agent session here: dev watch runs in the agent session that waits (Claude Code, "
                            "Codex)")
    if for_me:
        if sp.lane == "main":
            raise IdentityError("--for me is for a crew member's own wait, and this call is the tech lead's: run "
                                "dev watch --wait without --for me")
        if not sp.lane:
            raise IdentityError("--for me is for a crew member's own wait, and devkit cannot tell which member calls: "
                                "run it as a command of its own from the member's lane")
        return sp.lane
    if not sp.lane:
        raise IdentityError("devkit cannot tell whether the tech lead or a crew member runs this wait: run "
                            "`dev watch --wait` as a command of its own (a crew member adds --for me)")
    return sp.lane


# --------------------------------------------------------------------------- the rules

def _find(cwd=None, argv=None, now=None, exact: bool = False, settle: bool = True) -> _Found:
    """Every rule, in order. `exact`: a ledger call counts only when it typed this very command (a wait). `settle`
    False stops before rule 5, which reads the run's lanes: progress.caller and _as_lane never need it."""
    from . import progress
    args = [str(a) for a in (sys.argv[1:] if argv is None else argv)]
    agent, sid, how = progress.resolve_session(cwd, args)
    if how == "shell":
        return _Found(Speaker("human", sid, "", "human", True), agent, how, (), "")
    verified_main = agent != "codex"
    open_ = progress.open_calls(sid, now)
    hits = [c for c in open_ if (_typed_exactly(c, args) if exact else progress._names_args(c, args))]
    typed = hits if exact else [c for c in open_ if _typed_exactly(c, args)]
    members = sorted({str(c.get("caller")) for c in hits if c.get("caller") != "main"})
    run = None
    if members:
        run = progress.load_run(agent, sid) or {}
        ledger = max(members, key=lambda m: (progress.TOOL_LEVELS.index(progress.lane_tools(run, m) or "read"),
                                             ((run.get("agents") or {}).get(m) or {}).get("depth") or 0))
    else:
        ledger = "main" if hits and how != "recency" else ""
    calls = tuple(c.get("tool_use_id") for c in hits)
    lane = (os.environ.get(LANE_VAR) or "").strip()[:80]

    def found(lane_, how_, verified):
        return _Found(Speaker(agent, sid, lane_, how_, verified), agent, how, calls, ledger)
    if lane:
        return found(lane, "stamped", True)
    guessed = how == "recency"  # a session devkit only guessed never makes its caller the tech lead (rules 3 to 5)
    if os.environ.get(SPEAKER_VAR) == "main" and not guessed and not any(c.get("caller") != "main" for c in typed):
        return found("main", "stamped", verified_main)  # a member's own call of this very command beats the stamp
    if ledger:
        return found(ledger, "ledger", verified_main if ledger == "main" else True)
    if settle and not guessed and not _may_speak(progress, agent, sid, run, now):
        return found("main", "sole", verified_main)
    return found("", "unknown", False)


def _may_speak(progress, agent: str, sid: str, run, now) -> bool:
    """Could anyone but the main session be speaking in this session? A lane its run lists as running that has not
    ended (a stale one may still run commands), or a launch whose agent has not started yet."""
    now = now or time.time()
    run = progress.load_run(agent, sid) if run is None else run
    if isinstance(run, dict) and run.get("state") in ("running", "blocked"):
        states = progress.lane_states(run, now, own=True)
        if any((states.get(k) or {}).get("state") != "ended" for k, _ in progress.running_agents(run)):
            return True
    from . import state
    sess = state.read(progress.session_path(agent, sid)) or {}
    return any(isinstance(v, dict) and v.get("state") == "pending" and now - (v.get("ts") or now) < LAUNCH_WAIT
               for v in (sess.get("launches") or {}).values())


_DEV = r"(?:^|(?<=[\s;&|(){}`'\"]))['\"]?(?:[^\s;&|(){}`'\"]*/)?dev['\"]?"  # `dev`, bare, with a path, quoted
_END = r"(?=\s*(?:$|[;&|(){}<>`'\"]|\d*[<>]))"  # what may follow the last argument: the end, an operator, a quote


def _typed_exactly(entry: dict, args: list) -> bool:
    """Does the call's text run `dev` with exactly these arguments, each as typed or in quotes, followed by nothing
    but the end of that command (a pipe, a redirection, `;`, `&&`, a closing quote)? A longer `dev` call with the same
    words is another command. A cut text counts by what it kept."""
    args = [a for a in args if a]
    if not args:
        return False
    words = r"\s+".join(f"(?:{re.escape(a)}|'{re.escape(a)}'|\"{re.escape(a)}\")" for a in args)
    return re.search(_DEV + r"\s+" + words + _END, str(entry.get("cmd") or "")) is not None


# --------------------------------------------------------------------------- this Mac
# macOS changes the hostname with the network (`mac-158`, `Mac-158.lan`, `andress-mbp-2` in one day), so devkit names
# a Mac by a random id created once in its local state (STATE/machine.json, 0600) and keeps the hostname as a label
# for display only. The id is independent of the bug registry's install id (bugs.install_id), which never leaves the
# Mac, while this one is published in the handoff; it holds nothing about the hardware or the person.
#
# Records written before the id existed name the Mac by its hostname of the time (`ana@mac-158`). The record keeps
# each hostname this Mac carried with the periods it carried it (aliases): seeded once from this Mac's own audit
# log, whose every record was written here with its time, then extended whenever devkit runs. was_this_mac reads a
# legacy name as this Mac's only for a record written while this Mac carried it, so a DHCP name that another Mac
# carries later or carried before (`mac-55`) never makes that Mac's records this one's.

MACHINE_ID = re.compile(r"m-[0-9a-z]{8}")
_REFRESH = 300  # seconds between two writes of the same period's end
# A period's margins. Before it starts: clocks a few seconds apart. After it ends: the end is written at most every
# _REFRESH seconds, so this Mac may have carried the name up to that long after it, plus the clocks. Nothing wider:
# another Mac may take the name the moment this one leaves it, and old devkit never matched that record either.
MARGIN_BEFORE = 60
MARGIN_AFTER = _REFRESH + 60
_PERIODS_KEPT = 200  # periods kept per hostname; older ones merge, so a name never loses its whole history


@dataclass(frozen=True)
class Machine:
    id: str  # "m-" and 8 random base36 characters, stable for this Mac's devkit state
    label: str  # the current short hostname, lowercase: for display only
    aliases: dict  # hostname -> [[first, last], …] seconds since the epoch: when this Mac carried that name


def short_host(name) -> str:
    """A hostname as devkit compares it: the first label, lowercase (`Mac-158.lan` -> `mac-158`)."""
    return str(name or "").strip().split(".")[0].lower()


def hostname() -> str:
    import socket
    return short_host(socket.gethostname())


def _machine_file():
    from . import core
    return core.STATE / "machine.json"


def _new_id() -> str:
    import secrets
    return "m-" + "".join(secrets.choice("0123456789abcdefghijklmnopqrstuvwxyz") for _ in range(8))


_HOST_RX = re.compile(r'"host":\s*"([^"\\]*)"')
_TS_RX = re.compile(r'"ts":\s*([0-9]+(?:\.[0-9]+)?)')


def _seen_in_audit() -> dict:
    """The hostnames in this Mac's own audit log (STATE/audit/*.jsonl), each with the periods it ran under it: a run
    of consecutive records under one name is one period."""
    from . import core
    out, last = {}, None
    files = sorted((core.STATE / "audit").glob("*.jsonl")) if (core.STATE / "audit").is_dir() else []
    for f in files:
        try:
            fh = f.open("r", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                h, t = _HOST_RX.search(line), _TS_RX.search(line)
                name = short_host(h.group(1)) if h else ""
                if not name or not t or name in ("?", "localhost") or MACHINE_ID.fullmatch(name):
                    continue  # a record that names this Mac by its machine id says nothing about its hostnames
                ts = float(t.group(1))
                if name == last and out[name]:
                    out[name][-1][1] = max(out[name][-1][1], ts)
                else:
                    out.setdefault(name, []).append([ts, ts])
                last = name
    return out


def _valid(doc) -> bool:
    return isinstance(doc, dict) and isinstance(doc.get("id"), str) and MACHINE_ID.fullmatch(doc["id"]) is not None \
        and isinstance(doc.get("aliases"), dict)


def _record_name(doc: dict, name: str, now: float) -> None:
    periods = doc["aliases"].setdefault(name, [])
    if doc.get("current") == name and periods:
        periods[-1][1] = max(periods[-1][1], now)
    else:
        periods.append([now, now])
        if len(periods) > _PERIODS_KEPT:
            periods[:2] = [[periods[0][0], periods[1][1]]]
    doc["current"] = name


def _created(now: float, name: str) -> dict:
    """A new record: a fresh id, the names the audit log shows, and the hostname of this moment counted from when this
    Mac took it. When the audit log ends under that name, its last period there runs on to now; when it ends under
    another name, this Mac took the name after that last record. Only with no audit record at all does the name
    count from the start, as devkit read it until now: nothing tells when this Mac took it. A DHCP name (`mac-55`)
    another Mac carried before this one took it so stays that Mac's."""
    seen = _seen_in_audit()
    doc = {"version": 1, "id": _new_id(), "created": int(now), "aliases": seen}
    ends = [(p[1], n) for n, periods in seen.items() for p in periods]
    if not ends:
        seen[name] = [[0, now]]
    else:
        last_end, last_name = max(ends)
        periods = seen.setdefault(name, [])
        if last_name == name:
            next(p for p in periods if p[1] == last_end)[1] = now
        else:
            periods.append([last_end, now])
    doc["current"] = name
    return doc


def _fresh(doc, name: str, now: float) -> bool:
    periods = (doc.get("aliases") or {}).get(name) or []
    return doc.get("current") == name and bool(periods) and now - periods[-1][1] < _REFRESH


def machine(now: float | None = None) -> Machine:
    """This Mac: its stable id, the hostname as label, and the hostnames it carried (aliases). The first call creates
    the record; a call under a new hostname adds it to the aliases. A record that does not parse is replaced whole
    (a new id). When the state cannot be written (a busy lock, a read-only folder), the record read stands, and with
    none the hostname itself is the id, as devkit named a Mac before. A call made while this thread already writes
    the record (the state module audits a busy lock, and the audit names this Mac) only reads it."""
    from . import state
    now = time.time() if now is None else now
    name = hostname() or "localhost"
    path = _machine_file()
    doc = state.read(path)
    if not (_valid(doc) and _fresh(doc, name, now)) and not getattr(_WRITING, "on", False):
        _WRITING.on = True
        try:
            doc = _write_machine(state, path, name, now) or doc
        finally:
            _WRITING.on = False
    if not _valid(doc):
        return Machine(id=name, label=name, aliases={name: [[0, now]]})
    return Machine(id=doc["id"], label=name, aliases={k: [list(p) for p in v] for k, v in doc["aliases"].items()
                                                      if isinstance(v, list)})


_WRITING = threading.local()  # set while this thread writes the machine record


def _write_machine(state, path, name: str, now: float):
    """Record `name` as this Mac's hostname now, creating the record (or replacing a damaged one) first. -> the record
    written, or None when the state cannot be written."""
    def put(d):
        if not _valid(d):
            return _created(now, name)
        _record_name(d, name, now)
        return d
    try:
        if path.exists() and not _valid(state.read(path)):
            with state.hold(path):
                if not _valid(state.read(path)):
                    state.replace(path, _created(now, name), indent=1)
        return state.update(path, put, indent=1)
    except (state.StateBusy, OSError):
        return None


def was_this_mac(name: str, at: float | None = None, m: Machine | None = None) -> bool:
    """Was a record that names its machine `name` (a hostname, as records before the machine id did), written at
    `at` (seconds; now when None), written by this Mac? Yes when this Mac carried that hostname then. A machine id is
    never a former name: ids are compared exactly by the caller."""
    name = short_host(name)
    if not name or MACHINE_ID.fullmatch(name):
        return False
    m = m or machine()
    at = time.time() if at is None else float(at)
    return any(p[0] - MARGIN_BEFORE <= at <= p[1] + MARGIN_AFTER for p in m.aliases.get(name) or [] if len(p) == 2)
