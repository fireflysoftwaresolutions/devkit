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
   holds, the lane wins, and so does a member whose own open call typed exactly this command (a member that set
   the variable itself);
4. the call ledger: an open call of a member whose text holds this command makes it that member's (the most
   restrictive one, as progress.caller decides, which is right for a permission; a command that acts as its
   caller's lane refuses instead, naming them all: acting_lane); open calls of main's only, in a session not guessed
   by recency, make it main's;
5. no crew member of the session's run may still be speaking (no running lane that has not ended, no launch whose
   agent has not started), in a session not guessed by recency: the main session, which nobody else could be;
6. otherwise devkit does not know (`unknown`), and a command that needs to know refuses. A session found only by
   recency (an agent's command whose process no session record holds) always ends here or at a member.

A wait (consumer) reads the ledger more strictly: a call counts only when it typed this very `dev` command, nothing
more, so the tech lead's `dev watch --wait` never becomes a member's because the member's open
`dev watch --wait --timeout 9` holds the same words; and when two members' open calls typed it alike, it is neither's
(a Codex crew, whose rewrite may carry no lane mark, waits with one command for every member).

Codex marks a spawned agent's hooks with its agent_id, but the main thread's hooks carry none, so a Codex speaker that
devkit takes for main by these rules is not verified (`verified` False); progress.caller reports it as `unverified`.

The call token comes before every rule above (rule 0). The PreToolUse hook gives each shell call a token,
DEVKIT_CALL=<tool_use_id>.<nonce>, after its call ledger entry holds the nonce's SHA-256 and the session record lists
the call as one the hook opened (call_token). A `dev` command that carries one is the entry's caller (`main` or the
member's agent id, how "token") when all of these hold (_verify): the entry is in the folder of the session this
process runs in and names it, it is open and listed, its hash matches; the nearest agent process above this command is
the entry's, and a Claude Code token carries no Codex thread id its call did not; the call's shell (the shell closest
to that agent above this command) carries this very token in its own command line, where the hook writes it, or,
when that shell replaced itself with the command it ran last, the call's own text runs this very dev command; this
command runs in that shell's process session (a detached process never does). A token another shell read from the
process list therefore names nobody in the thief's shell. A command resolves its token once and keeps the verdict for
its lifetime. A failed token refuses a state change (STATE_CHANGING) and lets a read or a wait fall to the rules
above. In a session whose hook stamps tokens, a state change without one is refused (caller.no-token): text matching
never decides it. On Codex, where Codex may ask (the hook cannot stamp every call), a call without a token may name
its lane by CODEX_THREAD_ID (rule 0b) when an open call of that lane holds the command; the root thread maps to main
only after a member's token call showed that members carry thread ids of their own. Any process can set the variable,
so where the hook stamps every call (stamps_all) the thread rule never decides a state change.
"""
from __future__ import annotations

import errno
import hashlib
import hmac
import json
import math
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field, replace

LANE_VAR = "DEVKIT_FROM_AGENT"  # a crew member's lane call, stamped by the PreToolUse hook
SPEAKER_VAR = "DEVKIT_SPEAKER"  # "main": the main session's own `dev watch` or `dev session` call, stamped likewise
CALL_VAR = "DEVKIT_CALL"  # the call token: <tool_use_id>.<nonce>, stamped on every shell call by the PreToolUse hook
THREAD_VAR = "CODEX_THREAD_ID"  # the Codex thread that runs a command, exported by Codex
LAUNCH_WAIT = 900  # seconds a launch whose agent has not started still counts as someone who may speak
CALLS_KEPT = 512  # tool_use_ids a session record keeps of its open calls, and of its closed ones
_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,120}\.[A-Za-z0-9_-]{22}")
_TID = re.compile(r"[A-Za-z0-9_-]{1,120}")

# The commands that change a run, a session's records, the crew's records, work items or plans (whether or not they
# ask who calls): a call token decides who runs them, and in a session whose hook stamps tokens nothing else does. A
# subcommand that does both (`crew mode`, `crew raci`, `crew next`) is here when one of its forms changes state.
# Everything else (watch, the READS below, and the commands that ask nobody) is a read or a wait.
STATE_CHANGING = {
    "progress": frozenset({"start", "step", "block", "done", "note", "add", "assign", "begin", "reopen", "skip",
                           "confirm"}),
    "crew": frozenset({"ask", "answer", "decide", "withdraw", "mode", "delegate", "decline", "route", "ack", "next",
                       "raci", "set"}),
    "session": frozenset({"tell", "ask", "answer", "claim", "release", "escalate", "ack", "carry", "forget",
                          "replay"}),
    "task": frozenset({"add", "start", "claim", "release", "done", "block", "unblock", "cancel", "reopen", "link",
                       "delegate", "move", "edit", "reconcile"}),
    "plan": frozenset({"new", "revise", "approve", "supersede"}),
}
# The commands that ask who calls only to know whose records to read or wait on: a failed token lets them fall to the
# rules above. tests/test_identity_coverage.py runs every coordination subcommand and holds both sets to what it finds.
READS = {
    "progress": frozenset({"history"}),
    "crew": frozenset({"read", "inbox", "decisions", "owner"}),
    "session": frozenset({"ls", "read", "inbox", "brief"}),
    "task": frozenset({"ls"}),
}

STALE = ("this call's identity has ended (its call closed, or ran past its time limit); a background command's "
         "identity ends when it starts: run it in the foreground")
NO_TOKEN = ("devkit could not verify this call's identity (it carries no call token): run it again as a command of "
            "its own")
BAD_TOKEN = "devkit could not verify this call's identity (its call token does not match its call): run it again"
FOREIGN = "this call's token belongs to another session or agent, so it names nobody here: run it again in your own call"
FOREIGN_USE = "this call's token is in use by another shell, so it names nobody: run it again in your own call"
PLAIN_CALL = ("devkit could not verify which Codex thread runs this: run it as one plain dev command (no pipeline, "
              "script or other command around it)")
CONFLICT = "this call's token and its lane variable or thread name different callers"
UNPINNED = ("devkit could not tell which agent process runs this call, so its token names nobody for a state change: "
            "run it again as a command of its own")


@dataclass(frozen=True)
class Speaker:
    agent: str  # "claude" | "codex" | "human"
    session: str  # the session id ("shell-<ppid>" for a developer's shell)
    lane: str  # "main" | a crew member's agent_id | "" (not known)
    how: str  # "payload" | "token" | "text-pinned" | "thread" | "stamped" | "ledger" | "sole" | "human" | "unknown"
    verified: bool  # False for a Codex main session (its hooks carry no mark) and for anyone devkit cannot name
    why: str = field(default="", compare=False)  # why devkit refuses this caller a state change ("" when it does not)

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
    members: tuple = ()  # the members whose open calls hold this command

    @property
    def refusal(self) -> str:
        """Why a state change is refused to this caller (a failed or missing call token), "" when it is not."""
        return self.speaker.why


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


def acting_lane(cwd: str | None = None) -> str:
    """The lane a command that acts as its caller's lane takes (progress._as_lane): the speaker's lane, read without
    rule 5 ("main", a member's agent id, or "" when devkit cannot tell). IdentityError when the open calls of several
    members hold this command and no lane mark names one of them: the speaker is then the most restricted of them,
    which is right for a permission (every one of them is a member) but would read or write in another member's lane;
    the refusal names them all. IdentityError too when the call's token failed or is missing where one is due
    (_Found.refusal), and when devkit cannot name the caller while a crew member may be speaking: an unknown caller
    never acts as the tech lead."""
    got = _find(cwd, None, None, settle=False)
    if got.refusal:
        raise IdentityError(got.refusal)
    if got.speaker.how == "unknown":
        from . import progress
        if state_changing(sys.argv[1:]) and _may_speak(progress, got.agent, got.speaker.session, None, None):
            raise IdentityError("devkit cannot tell who runs this command, and only the tech lead or a crew member "
                                "in its own lane may: run the dev command as a command of its own, not through a "
                                "script or another shell")
        return ""
    if got.speaker.how != "ledger" or len(got.members) < 2:
        return got.speaker.lane
    from . import crew, progress
    agents = (progress.load_run(got.agent, got.speaker.session) or {}).get("agents") or {}
    names = [crew.who((agents.get(m) or {}).get("persona") or (agents.get(m) or {}).get("label") or m)
             for m in got.members]
    raise IdentityError(f"the open calls of {len(names)} crew members run this same command ({', '.join(names)}) and "
                        "no lane mark says which one calls, so devkit acts as none of them: run it again as a command "
                        "of its own, in your lane")


def require(sp: Speaker, *, main: bool = False, what: str = "") -> Speaker:
    """`sp`, or IdentityError: devkit cannot tell who speaks; with `main`, a crew member, or the developer's own
    terminal, which has no agent session to act for. `what` says what the command does ("sends to other sessions")."""
    does = what or "runs this command"
    if sp.why and main:
        raise IdentityError(f"only the tech lead {does}, and {sp.why}; a crew member asks them with dev crew ask "
                            "\"…\"")
    if sp.why:
        raise IdentityError(f"devkit refuses who {does}: {sp.why}")
    if sp.how == "unknown":
        raise IdentityError(f"devkit cannot tell whether the tech lead or a crew member {does}: run the dev command "
                            "as a command of its own, not in a pipeline, a script or another shell")
    if main and sp.how == "human":
        raise IdentityError(f"the developer's own terminal has no agent session, and only the tech lead {does}: "
                            "type it in the agent session instead")
    if main and sp.lane != "main":
        raise IdentityError(f"only the tech lead {does}: ask them with dev crew ask \"…\"")
    return sp


def verified_lane(lane: str, cwd: str | None = None, now: float | None = None) -> str:
    """`lane` (a lane variable a `dev progress` or `dev crew` call passes) when an open call of this session belongs
    to that same lane, or IdentityError. A command can give the variable another member's id in ways its text does
    not show (a child shell's `print -v`, `${(P)v::=…}`, `declare -n`), and a variable only narrows when the call
    ledger shows that member at work: without one of its calls open, the call has no lane identity and acts as no
    one. Codex selects its session by the lane and refuses one that belongs to none (progress.resolve_session)."""
    from . import progress
    got = _find(cwd, None, now, settle=False)
    if got.speaker.how in ("token", "text-pinned"):
        if got.speaker.lane != lane:
            raise IdentityError(f"{CONFLICT}: run the dev command as a command of your own, in your lane")
        return lane
    if got.refusal:
        raise IdentityError(got.refusal)
    agent, sid, how = progress.resolve_session(cwd, sys.argv[1:])
    if agent == "codex":
        return lane
    if how != "shell" and any(str(c.get("caller")) == lane for c in progress.open_calls(sid, now)):
        return lane
    raise IdentityError(f"the lane variable names {lane[:40]}, and no open call of this session is that crew "
                        "member's, so devkit cannot tell who runs this command: run the dev command as a command of "
                        "your own, in your lane")


def consumer(for_me: bool, cwd: str | None = None) -> str:
    """Whose wait `dev watch` is: "main" or a crew member's lane. `--for me` is a member's own wait, refused when it
    resolves to the main session; without it, the speaker's own (a member's call makes it the member's). A speaker
    devkit cannot name is refused: guessing would let one wait take the other's events."""
    got = _find(cwd, None, None, exact=True)
    sp = got.speaker
    if sp.how == "human":
        raise IdentityError("no agent session here: dev watch runs in the agent session that waits (Claude Code, "
                            "Codex)")
    if not sp.lane and len(got.members) > 1:
        raise IdentityError(f"several crew members ({len(got.members)}) run this very wait at once and no lane mark "
                            "says which one calls, so devkit gives it to none of them (it would take one member's "
                            "events for another). Read your answer with dev crew inbox --all, or wait again in a "
                            "moment")
    if for_me:
        if sp.lane == "main":
            raise IdentityError("--for me is for a crew member's own wait, and this call is the tech lead's: run "
                                "dev watch --wait without --for me")
        if not sp.lane:
            raise IdentityError(sp.why or "--for me is for a crew member's own wait, and devkit cannot tell which "
                                          "member calls: run it as a command of its own from the member's lane")
        return sp.lane
    if not sp.lane:  # the reason the text pin gave (a contested or ambiguous call), else the general one
        raise IdentityError(sp.why or "devkit cannot tell whether the tech lead or a crew member runs this wait: run "
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
    changes = state_changing(args)
    token = _token_verdict(agent, sid, args, now)
    if token is not None and token.caller:  # rule 0: the call token names its caller
        members = (token.caller,) if token.caller != "main" else ()
        return _Found(Speaker(agent, sid, token.caller, "token", True), agent, how, (token.tid,), token.caller,
                      members)
    if token is not None and changes:
        return _Found(Speaker(agent, sid, "", "unknown", False, token.why), agent, how, (), "")
    text = _text_resolved(agent, sid, args, changes, now, strict=exact) if token is None else None
    if text is not None and text.caller:  # rule 0c: a plain dev call the hook left as it was, pinned by the kernel
        members = (text.caller,) if text.caller != "main" else ()
        return _Found(Speaker(agent, sid, text.caller, "text-pinned", True), agent, how, (text.tid,), text.caller,
                      members)
    if text is not None and (changes or exact):  # a wait takes one caller's events: checked like a state change
        return _Found(Speaker(agent, sid, "", "unknown", False, text.why), agent, how, (), "")
    if token is None and agent == "codex":  # rule 0b: the Codex thread that runs the command
        thread = _thread_rule(agent, sid, args, changes, now)
        if thread:
            return _Found(Speaker(agent, sid, thread, "thread", True), agent, how, (), thread,
                          (thread,) if thread != "main" else ())
    if token is None and changes and _token_session(agent, sid):
        _no_token(agent, sid, args)
        why = PLAIN_CALL if agent == "codex" else NO_TOKEN
        return _Found(Speaker(agent, sid, "", "unknown", False, why), agent, how, (), "")
    verified_main = agent != "codex"
    open_ = progress.open_calls(sid, now)
    hits = [c for c in open_ if (_typed_exactly(c, args) if exact else
                                 progress._names_args(c, args) or _runs_parsed(c, args))]
    hits = [c for c in hits if not _ran_elsewhere(c, args)]
    typed = hits if exact else [c for c in open_ if _typed_exactly(c, args)]
    members = sorted({str(c.get("caller")) for c in hits if c.get("caller") != "main"})
    run = None
    if members:
        run = progress.load_run(agent, sid) or {}
        # a wait two members typed alike is neither's: the most restricted one is right for a permission, but a wait
        # given to it would take that member's events and leave the other's waiting for nobody
        ledger = "" if exact and len(members) > 1 else max(
            members, key=lambda m: (progress.TOOL_LEVELS.index(progress.lane_tools(run, m) or "read"),
                                    ((run.get("agents") or {}).get(m) or {}).get("depth") or 0))
    else:
        ledger = "main" if hits and how != "recency" else ""
    calls = tuple(c.get("tool_use_id") for c in hits)
    lane = (os.environ.get(LANE_VAR) or "").strip()[:80]

    def found(lane_, how_, verified):
        return _Found(Speaker(agent, sid, lane_, how_, verified), agent, how, calls, ledger, tuple(members))
    if lane:
        # the hook's stamp names the member whose command it is; a command that gave the variable another lane's id
        # before this call is told apart by the call ledger: the open calls that typed this command are other
        # members' only, so the caller is the most restricted of them, never the lane the variable names (a lane
        # with only the tech lead's call keeps the stamp, which narrows)
        if members and lane not in members:
            return found(ledger, "ledger", True) if ledger else found("", "unknown", False)
        return found(lane, "stamped", True)
    guessed = how == "recency"  # a session devkit only guessed never makes its caller the tech lead (rules 3 to 5)
    if os.environ.get(SPEAKER_VAR) == "main" and not guessed and not any(c.get("caller") != "main" for c in typed):
        # a member's own call of this very command beats the stamp; for a wait, any process can set the variable (a
        # member's script), so it counts only when an open call of the tech lead's types this very wait
        if not exact or typed:
            return found("main", "stamped", verified_main)
        _speaker_unbacked(agent, sid, args)
    if ledger:
        return found(ledger, "ledger", verified_main if ledger == "main" else True)
    if settle and not guessed and not _may_speak(progress, agent, sid, run, now):
        return found("main", "sole", verified_main)
    return found("", "unknown", False)


def _speaker_unbacked(agent: str, sid: str, args: list):
    """A wait whose speaker mark says "the tech lead" while no open call of the tech lead's types it: audited once per
    process (caller.speaker-unbacked); the mark is then ignored."""
    key = ("speaker", os.getpid(), agent, sid, tuple(args))
    if key in _NOTED:
        return
    _NOTED.add(key)
    _audit_identity("caller.speaker-unbacked", _Verdict("", detail=f"{SPEAKER_VAR}=main on a wait no open call of the "
                                                                   "tech lead's types"), agent, sid, args, "flagged")


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
    words is another command. A cut text counts by what it kept, or by the exact argument list of a `dev` command it
    ran (progress.args_hash, recorded before the cut)."""
    from . import progress
    if args and progress.args_hash(args) in (entry.get("dev_args") or []):
        return True
    args = [a for a in args if a]
    if not args:
        return False
    words = r"\s+".join(f"(?:{re.escape(a)}|'{re.escape(a)}'|\"{re.escape(a)}\")" for a in args)
    return re.search(_DEV + r"\s+" + words + _END, str(entry.get("cmd") or "")) is not None


# What a call's text proves when its program is plain: every program in it devkit reads (shellparse.shell_calls),
# and each one either only filters text or is a devkit coordination command that starts no other `dev` command.
_INERT_PROGRAMS = frozenset({"cd", "pushd", "popd", "tail", "head", "grep", "egrep", "fgrep", "rg", "wc", "cat", "jq",
                             "sort", "uniq", "cut", "tr", "echo", "printf", "true", ":"})
_INERT_DEV = frozenset({"watch", "session", "crew", "progress"})
_EXPANDS = re.compile(r"[$`*?\[{~]")  # a word the shell may turn into other words


def _may_be(words: list, args: list) -> bool:
    """Could a `dev` call of these words (quotes removed) run with `args`? A word the shell expands may become any
    words, so only the words before the first such one are compared."""
    for w, a in zip(words, args):
        if _EXPANDS.search(w):
            return True
        if w != a:
            return False
    return len(words) == len(args) or any(_EXPANDS.search(w) for w in words[len(args):])


def _ran_elsewhere(entry: dict, args: list) -> bool:
    """Does this open call provably not run `dev <args>`, although its text holds the words? It does not when devkit
    read every program it runs and each is a filter or a devkit coordination call that cannot be this one: a member's
    `dev crew ask "…dev progress begin x…"` quotes the tech lead's command and runs another. A cut or redacted text
    proves it only by its own record: it writes no file (reads) and none of its `dev` calls is this one (dev_args).
    False whenever devkit cannot tell (an interpreter, a script, a shell fed text, an unreadable command)."""
    from . import progress, shellparse
    if entry.get("cut") or entry.get("dev_args"):
        return bool(entry.get("reads")) and progress.args_hash(args) not in (entry.get("dev_args") or [])
    try:
        calls = shellparse.shell_calls(str(entry.get("cmd") or ""), None)
    except Exception:
        return False
    if calls is None:
        return False
    for argv, code in calls:
        name = os.path.basename(argv[0]) if argv else ""
        if code is not None:
            return False
        if name == "dev":
            words = shellparse.dev_args(argv)
            pos = shellparse.dev_words(words) if words is not None else []
            if not pos or pos[0] not in _INERT_DEV or _EXPANDS.search(pos[0]) or _may_be(words, args):
                return False
        elif name not in _INERT_PROGRAMS:
            return False
    return True


def _expansion_parts(word: str) -> list:
    """A word as the shell reads it, split into (True, expansion) and (False, literal text): `$name`, `${…}`, `$(…)`
    with its nested parentheses and quotes, and backticks."""
    out, i, lit = [], 0, ""
    while i < len(word):
        c = word[i]
        if c == "`":
            j = word.find("`", i + 1)
            j = len(word) if j < 0 else j + 1
        elif c == "$" and word[i + 1:i + 2] == "(":
            depth, j, quote = 0, i + 1, ""
            while j < len(word):
                ch = word[j]
                if quote:
                    quote = "" if ch == quote else quote
                elif ch in "'\"":
                    quote = ch
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            j = min(j + 1, len(word))
        elif c == "$" and word[i + 1:i + 2] == "{":
            j = word.find("}", i)
            j = len(word) if j < 0 else j + 1
        elif c == "$" and re.match(r"[A-Za-z_]", word[i + 1:i + 2]):
            j = re.match(r"\$[A-Za-z_]\w*", word[i:]).end() + i
        elif c == "$" and word[i + 1:i + 2] and word[i + 1] in "0123456789@*#?$!-":
            j = i + 2
        else:
            lit += c
            i += 1
            continue
        if lit:
            out.append((False, lit))
            lit = ""
        out.append((True, word[i:j]))
        i = j
    if lit:
        out.append((False, lit))
    return out


def _runs_parsed(entry: dict, args: list) -> bool:
    """Does the call run `dev` with these arguments where it typed an expansion in their place, read the way the shell
    reads the command (shellparse.shell_calls)? Only for the commands that read or acknowledge the caller's own
    messages (progress.EXPANDED_COMMANDS): `dev crew ack REF --sha256 "$(dev crew read REF --json | jq -r .digest)"`
    holds the digest only once the substitution ran, and its nested parentheses end no expansion early."""
    from . import progress, shellparse
    text = str(entry.get("cmd") or "")
    if entry.get("cut") or tuple(args[:2]) not in progress.EXPANDED_COMMANDS or not any(c in text for c in "$`"):
        return False
    try:
        calls = shellparse.shell_calls(text, None) or []
    except Exception:
        return False
    for argv, _ in calls:
        words = shellparse.dev_args(argv)
        if not words or len(words) != len(args) or words[:2] != args[:2]:
            continue
        if all(w == a or re.fullmatch("".join(".*" if e else re.escape(t) for e, t in _expansion_parts(w)), a, re.S)
               for w, a in zip(words, args)):
            return True
    return False


# --------------------------------------------------------------------------- the call token
# The hook side (hooks._pre_tool): new_call_token for a shell call, progress.call_opened with its hash, note_opened under
# the session hold, then the stamp. A `dev` command: _token_verdict, once per process (_RESOLVED).

def state_changing(args) -> bool:
    """Does `dev <args>` change run state (STATE_CHANGING)?"""
    from . import shellparse
    try:
        words = shellparse.dev_words([str(a) for a in args or []])
    except Exception:
        return False
    return len(words) > 1 and words[1] in STATE_CHANGING.get(words[0], ())


def new_call_token(tid) -> tuple:
    """(DEVKIT_CALL's value, the SHA-256 hex digest of its nonce) for a shell call's tool_use_id; ("", "") for an id a
    token cannot carry. The nonce is 128 random bits, never stored."""
    import secrets
    if not isinstance(tid, str) or not _TID.fullmatch(tid):
        return "", ""
    nonce = secrets.token_urlsafe(16)
    return f"{tid}.{nonce}", hashlib.sha256(nonce.encode()).hexdigest()


def parse_token(value):
    """(tool_use_id, nonce) of a DEVKIT_CALL value, or None for anything else: rejected, never cleaned up."""
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        return None
    tid, _, nonce = value.rpartition(".")
    return (tid, nonce) if tid and not tid.startswith(".") else None



def detached_env(env=None) -> dict:
    """The environment for a process devkit starts detached (start_new_session): `env`, default this process's,
    without the call token. Such a worker never acts for the call that started it."""
    return {k: v for k, v in (os.environ if env is None else env).items() if k != CALL_VAR}


RUNNER_DROPS = (CALL_VAR, LANE_VAR, SPEAKER_VAR)


def runner_env(env=None) -> dict:
    """The environment for the project's own code that a devkit runner starts (the steps of dev ci --run, the test
    commands of dev test and dev test prove): `env`, default this process's, without the call token, the lane
    variable and the speaker mark. That code is not the call that ran the runner, and a crew member may have written
    it: a dev command in it names nobody, so its state change is refused in a session whose calls carry tokens
    (caller.no-token)."""
    return {k: v for k, v in (os.environ if env is None else env).items() if k not in RUNNER_DROPS}


def strip_token(cmd):
    """A shell command without the call token the PreToolUse hook put in front of it, as devkit keeps command text
    (the ledger, the records of closed calls, the audit trail, notices)."""
    if not isinstance(cmd, str):
        return cmd
    cmd = re.sub(r"\A ?(?:\(set -C; printf '%s\\n' \"\$\$\" > .+?\) 2>/dev/null \|\| mv -f .+? 2>/dev/null \|\| "
                 r"\(: > .+?\) 2>/dev/null; )?export DEVKIT_CALL=[A-Za-z0-9_.-]+; readonly DEVKIT_CALL; ", "", cmd)
    return re.sub(r"\A((?:DEVKIT_[A-Z_]+=[A-Za-z0-9_.:-]+ )*)DEVKIT_CALL=[A-Za-z0-9_.-]+ ", r"\1", cmd)


def codex_ttl(inp: dict):
    """The life of a Codex exec call's entry, in seconds, from its own limits: it ends at the exec's yield (five
    seconds after `yield_time_ms`) or its `timeout_ms`, never later than progress.CALL_TTL. None when the input
    gives neither."""
    from . import progress
    limits = []
    for k, extra in (("yield_time_ms", 5.0), ("timeout_ms", 0.0)):
        v = (inp or {}).get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            limits.append(v / 1000 + extra)
    return round(min(limits + [progress.CALL_TTL]), 3) if limits else None


def _session_update(path, fn):
    from . import state
    return state.update(path, fn, create=False)


def note_opened(agent: str, sid: str, tid: str = "", every: bool = False) -> bool:
    """The session record lists `tid` among the calls the hook opened (at most CALLS_KEPT) and is a token session
    (call_token). `every`: the hook stamps every shell call of this session (Claude Code, or Codex where it never
    asks), so a tokenless state change never needs the Codex thread rule there (stamps_all, kept for the session).
    False when the record is busy or missing: the call then gets no token."""
    from . import progress, state
    if not tid and _token_session(agent, sid):
        return True

    def put(d):
        if tid:
            d["calls_opened"] = ([t for t in d.get("calls_opened") or [] if t != tid] + [tid])[-CALLS_KEPT:]
        d["call_token"] = 1
        if every:
            d["stamps_all"] = 1
    try:
        _session_update(progress.session_path(agent, sid), put)
    except (state.StateBusy, OSError, ValueError):
        return False
    doc = state.read(progress.session_path(agent, sid)) or {}
    return not tid or tid in (doc.get("calls_opened") or [])


def _drop_marks(sid: str, tid: str):
    """Call `tid`'s pin, its contested marker and contest flag, and its use marks go."""
    if not tid:
        return
    pin = pin_path(sid, tid)
    for f in (used_path(sid, tid), uses_path(sid, tid), pin, pin.with_name(pin.name + ".contested")):
        try:
            f.unlink()
        except OSError:
            pass
    try:  # the contest flag, in the session's folder opened without following a link
        folder = ledger_folder(sid)
    except OSError:
        return
    try:
        os.unlink(contest_path("-", tid).name, dir_fd=folder)
    except OSError:
        pass
    finally:
        os.close(folder)


def note_closed(agent: str, sid: str, tid: str, entry: dict | None = None, ended: str = ""):
    """The call ended: its tid moves from the opened calls to the closed ones (at most CALLS_KEPT), and its pin and use
    marks go (a kept background call keeps them until its record goes, _kept_background). `entry`: its ledger entry;
    `ended`: how it ended when it got no result ("refused", "failed", "expired"; "" when it ran). A text call that
    ended with no result after a state change ran under its name is audited (calls.text-use-without-result): the
    call the developer denied or never let run cannot explain that change."""
    from . import progress, state
    if ended and isinstance(entry, dict) and entry.get("pin_by") == "text":
        changed = [u for u in read_uses(sid, tid) if u.get("change")]
        if changed:
            try:
                from . import audit
                # the caller goes in the detail: the record keeps no keyword audit.EXTRAS does not declare
                audit.record("calls.text-use-without-result", tid[:80] or "-",
                             f"{len(changed)} state change(s) ran under this call's name, and the call ended with no "
                             f"result ({ended[:40]}); its caller: {str(entry.get('caller') or 'main')[:80]}",
                             "flagged", tool_use_id=tid[:80], session=str(sid)[:80], host_agent=agent,
                             command=str(entry.get("cmd") or "")[:300])
            except Exception:
                pass
    if not os.path.lexists(background_path(sid, tid)):
        _drop_marks(sid, tid)

    def put(d):
        if tid in (d.get("calls_opened") or []):
            d["calls_opened"] = [t for t in d["calls_opened"] if t != tid]
            d["calls_closed"] = ([t for t in d.get("calls_closed") or [] if t != tid] + [tid])[-CALLS_KEPT:]
    try:
        _session_update(progress.session_path(agent, sid), put)
    except (state.StateBusy, OSError, ValueError):
        pass


def end_session(agent: str, sid: str):
    """The session ended (SessionEnd) before its record goes: each call still open leaves the ledger and ends like one
    that got no result (note_closed, "session-ended"), so a state change that ran under a call the developer denied
    is audited (calls.text-use-without-result) even when the session ends before that call expires."""
    from . import progress
    for e in progress.open_calls(sid):
        _end_call(agent, sid, e, "session-ended")


def _end_call(agent: str, sid: str, e: dict, ended: str):
    """An open call that will get no result leaves the ledger, and its identity ends with it (note_closed). A contested
    one is audited as such (audit_contested), as at its PostToolUse."""
    from . import progress
    tid = str(e.get("tool_use_id") or "")
    if not tid:
        return
    mark = _contest_mark(e)  # while its ledger entry keeps a sweep off the flag
    progress.call_closed({"tool_use_id": tid, "session_id": sid, "hook_event_name": "SessionEnd"}, ended=ended)
    audit_contested(e, sid, tid, mark or _contest_mark(e), ended)  # again: a contest made while it closed
    if e.get("pin_by"):
        note_closed(str(e.get("agent") or agent), sid, tid, entry=e, ended=ended)


def audit_contested(entry: dict, sid: str, tid: str, mark: str, ended: str = ""):
    """A stamped call that ends marked contested (`mark`, _contest_mark) is audited (calls.pin-contested, flagged): at
    its PostToolUse (hooks._pin_audits), and when it ends with no result: its session ended, its time ran out, or its
    agent went without its SessionEnd (`ended`, named in the record). The caller reads the mark before the call's
    ledger entry goes, and again after. Two processes that end one call (its PostToolUse and a SessionEnd, two
    sweeps) write one audit: the first to make the call's audit claim (audit_claim_path) writes it. An audit that
    fails is recorded as a swallowed devkit error (bugs.swallowed), never lost silently."""
    if not mark or not entry.get("pin") or not _claim_audit(sid, tid):
        return
    why = ("another process wrote this call's pin before its shell did" if mark == "pin"
           else "this call is marked contested: its contest flag is in the ledger")
    if ended:
        why += f" (the call ended: {ended[:40]})"
    try:
        from . import audit
        audit.record("calls.pin-contested", tid[:80] or "-", why, "flagged", tool_use_id=tid[:80],
                     session=str(sid)[:80], host_agent=str(entry.get("agent") or ""),
                     command=str(entry.get("cmd") or "")[:300])
    except Exception as e:  # noqa: BLE001 - the audit never stops a call's end
        try:
            from . import bugs
            bugs.swallowed({"": "post-tool", "session-ended": "session-end"}.get(ended, "identity"), e)
        except Exception:  # noqa: BLE001 - nor does the record of its failure
            pass


def audit_claim_path(sid: str, tid: str):
    """Call `tid`'s audit claim: CALLS/<sid>/.<tid>.audited (a dot-file: no reader of the ledger takes it for an
    entry), made by the first process that audits the call's contest. It outlives the call's other marks, so a second
    end that read the call before the first removed it finds the claim; the sweep removes it like a contest flag
    (_sweep_flags)."""
    from . import progress
    return progress.CALLS / progress._safe_name(sid) / f".{progress._safe_name(tid)}.audited"


def _claim_audit(sid: str, tid: str) -> bool:
    """True when this process is the first to audit call `tid`'s contest: it made the claim (O_EXCL), in the session's
    folder opened without following a link. True too when no claim can be made there (no folder, a link, a full
    disk): an audit written twice is better than none."""
    if not tid:
        return True
    try:
        folder = ledger_folder(sid)
    except OSError:
        return True
    try:
        os.close(os.open(audit_claim_path("-", tid).name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=folder))
        return True
    except FileExistsError:
        return False
    except OSError:
        return True
    finally:
        os.close(folder)


def sweep_gone(now: float | None = None) -> int:
    """The calls a session left open when its agent went without its SessionEnd (killed, crashed): in every session's
    ledger, each open call whose agent process no longer runs ends like one still open when its session ends
    (_end_call, "session-gone"), so a state change that ran under it is audited (calls.text-use-without-result); a
    call past its time ends there as expired (progress.open_calls). A call whose agent pid another process took since
    waits for its time. Then the contest flags the ended calls left behind go (_sweep_flags). Run at every session
    start and by `dev sync`. -> the calls it ended"""
    from . import progress
    reads = [_LIST_MAX]  # the names a sweep lists and the ledger entries it reads, shared by all its folders
    sids = []
    try:  # a session folder that is a link is never one: nothing it points to is read or removed
        with os.scandir(progress.CALLS) as found:
            for d in found:
                if reads[0] <= 0:
                    break
                reads[0] -= 1
                if d.is_dir(follow_symlinks=False) and not d.name.startswith("."):
                    sids.append(d.name)
    except OSError:
        return 0
    sids.sort()
    if sids:  # each sweep starts at another folder, so one flooded folder never starves the ones after it
        at = _sweep_start(len(sids)) % len(sids)
        sids = sids[at:] + sids[:at]
    n, budget = 0, [_SWEEP_MAX]
    for sid in sids:
        if reads[0] <= 0:
            break
        for e in progress.open_calls(sid, now, budget=reads):
            pid = e.get("agent_pid")
            if isinstance(pid, int) and not progress._pid_running(pid):
                _end_call(str(e.get("agent") or ""), sid, e, "session-gone")
                n += 1
        if budget[0] > 0:
            _sweep_flags(progress.CALLS / sid, now, budget, reads)
    return n


def _sweep_start(n: int) -> int:
    """Where a sweep of `n` session folders starts (sweep_gone): a random one."""
    import random
    return random.randrange(n)  # nosemgrep: python.lang.security.audit.insecure-random  (an order, no secret)


FLAG_GRACE_S = 60  # seconds a sweep leaves a contest flag after it was made and after its call closed
_TREE_DEPTH = 16  # levels of a folder planted at a flag's name the sweep removes; a deeper one stays
_SWEEP_MAX = 1000  # flags a sweep looks at and entries of planted folders it removes, at most: the rest waits
_LIST_MAX = 10 * _SWEEP_MAX  # names a sweep lists and ledger entries it reads, at most: the rest waits


def _sweep_flags(folder, now: float | None = None, budget: list | None = None, reads: list | None = None):
    """The contest flags (contest_path) a session's ledger folder kept after their calls ended: each `.<tid>.contested`
    whose call has no ledger entry and no kept record goes, whatever is there: a file, a link (never followed), or a
    folder planted at its name, which the call's end cannot unlink (_drop_marks). A flag made, or whose call closed
    (the session's record of closed shell calls), less than FLAG_GRACE_S before `now` stays: the close's audit reads
    it right after the call's ledger entry goes (hooks._pin_audits), and the record is written before the entry goes.
    A date more than FLAG_GRACE_S after `now` is no sign of a fresh flag (an mtime anyone can set): that flag goes.
    The folder is opened once, never through a link (O_NOFOLLOW), and every name in it is reached from that open
    folder (dir_fd): a folder that is a link is left, and one swapped for a link meanwhile is never followed. `budget`
    ([count], shared by the sweep's folders): the flags looked at and planted entries removed, at most _SWEEP_MAX;
    `reads` ([count], shared too): the names listed, at most _LIST_MAX; what is left past either waits for the next
    sweep. A call's audit claim (audit_claim_path) goes by the same rules."""
    import stat
    now = now or time.time()
    budget = budget if budget is not None else [_SWEEP_MAX]
    reads = reads if reads is not None else [_LIST_MAX]
    suffixes = (".contested", ".audited")
    try:
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            return
        names = []
        with os.scandir(fd) as listed:
            for d in listed:
                if reads[0] <= 0:
                    break
                reads[0] -= 1
                suffix = next((x for x in suffixes if d.name.endswith(x)), "")
                if suffix and d.name.startswith(".") and len(d.name) > len(suffix) + 1:
                    names.append((d.name, suffix))
        closed = unread = object()
        for name, suffix in names:
            if budget[0] <= 0:
                break
            budget[0] -= 1
            tid = name[1:-len(suffix)]
            if _lexists_at(fd, f"{tid}.json") or _lexists_at(fd, f".{tid}.background"):
                continue
            try:
                st = os.lstat(name, dir_fd=fd)
            except OSError:
                continue
            if abs(now - st.st_mtime) < FLAG_GRACE_S:
                continue
            if closed is unread:  # read once per folder, and only when a flag is old enough to go
                closed = _closed_lately(folder.name, now)
            if closed is None or tid in closed:
                continue
            try:
                if stat.S_ISDIR(st.st_mode):
                    _remove_tree_at(fd, name, _TREE_DEPTH, budget)
                else:
                    os.unlink(name, dir_fd=fd)
            except OSError:
                pass
    except OSError:
        return
    finally:
        os.close(fd)


def _closed_lately(sid: str, now: float):
    """The calls that closed less than FLAG_GRACE_S before `now` (the session's record of closed shell calls; a close
    dated more than FLAG_GRACE_S after `now` does not count), as a set of tool_use_ids. None when the record is there
    and cannot be read as one (no read permission, broken, not a file, a link): the sweep then leaves every flag."""
    from . import progress
    try:
        return {str(c.get("tool_use_id")) for c in progress.shells_since(sid, now - FLAG_GRACE_S, now, strict=True)
                if (c.get("closed") or 0) < now + FLAG_GRACE_S}
    except Exception:  # noqa: BLE001 - a record that cannot be read: the flags stay, as for calls that just closed
        return None


def _lexists_at(fd: int, name: str) -> bool:
    try:
        os.lstat(name, dir_fd=fd)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True  # it may be there: the sweep leaves the flag


def _remove_tree_at(parent: int, name: str, depth: int, budget: list | None = None):
    """Remove folder `name` of the open folder `parent` and all it holds, never through a link: each folder is opened
    with O_NOFOLLOW from its parent's descriptor, and a link inside is unlinked, never followed. A tree deeper than
    `depth` levels stays (OSError), and so does what is left once `budget` ([count]) is spent: each entry removed
    takes one."""
    import stat
    budget = budget if budget is not None else [_SWEEP_MAX]
    if depth <= 0:
        raise OSError(errno.ELOOP, "folder too deep", name)
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.ENOTDIR, "not a folder", name)
        for child in os.listdir(fd):
            if budget[0] <= 0:
                raise OSError(errno.EAGAIN, "the sweep's budget is spent", name)
            budget[0] -= 1
            if stat.S_ISDIR(os.lstat(child, dir_fd=fd).st_mode):
                _remove_tree_at(fd, child, depth - 1, budget)
            else:
                os.unlink(child, dir_fd=fd)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent)


@dataclass(frozen=True)
class _Verdict:
    tid: str
    caller: str = ""  # who the token names when it holds: "main" or a member's agent id
    code: str = ""  # the audit action when it fails
    why: str = ""  # the refusal of a state change
    detail: str = ""
    ancestors: tuple = ()
    unlisted: bool = False  # a verified token use its call's use list could not hold (calls.use-unrecorded)


_RESOLVED: dict = {}  # {(pid, token, ledger folder, agent, session): _Verdict}: a process resolves its token once
_NOTED: set = set()  # the tokenless state changes this process already audited


def _forget():
    """Forget the verdicts this process reached (tests: each dev command is a process of its own)."""
    _RESOLVED.clear()
    _NOTED.clear()


def _token_verdict(agent: str, sid: str, args: list, now):
    """The verdict on this process's call token, None when it carries none. Reached once per process and kept, so a
    long command keeps its caller after its call closed; a failure is audited then."""
    from . import progress
    value = os.environ.get(CALL_VAR)
    if not value:
        return None
    key = (os.getpid(), value, str(progress.CALLS), agent, sid)
    if key not in _RESOLVED:
        _RESOLVED[key] = v = _verify(agent, sid, value, now, args)
        if v.code and (state_changing(args) or v.code not in ("caller.stale-token", "caller.token-unpinned")):
            _audit_identity(v.code, v, agent, sid, args, "deny" if state_changing(args) else "flagged")
        if v.unlisted:  # after the verdict is kept: the audit record resolves its caller through it
            _audit_identity("calls.use-unrecorded", replace(v, detail=(
                "its call's use list could not be written, so the end of the call cannot count this use "
                "(caller.token-unlisted-use)")), agent, sid, args, "flagged")
    return _RESOLVED[key]


def _chain() -> list:
    """This process's parents, nearest first, as (pid, command line) (progress._ancestor_pids)."""
    from . import progress
    pids = progress._ancestor_pids()
    lines = (progress._ANCESTRY.get(os.getpid()) or {}).get("args") or {}
    return [(p, lines.get(p, "")) for p in pids]


# The call's pin binds its token to one process: the call's own shell, by kernel facts only (its pid, its parent and
# its start time), never by a command line, which any process can forge. The stamp writes the shell's pid as its
# first action, under noclobber, into a per-user 0700 folder a sandboxed shell may write (pin_dir); a pin that was
# already there marks the call contested, and then the token names nobody. For the plain form (`DEVKIT_CALL=<v> dev
# …`, where no stamp runs first) the dev process pins the process right below the agent itself (O_EXCL).
_PIN_TEXT = re.compile(r"\d{1,10}\n?")


def pin_dir():
    """The per-user folder of call pins: devkit-calls-<uid>-<state> in the user's own $TMPDIR (macOS: /var/folders/…/T/,
    which Codex's workspace-write sandbox and Claude Code's bash sandbox let a shell write), one per devkit state
    folder; never the shared /tmp: devkit's state folder when $TMPDIR is unset or is /tmp."""
    from pathlib import Path
    from . import core, progress
    base = os.environ.get("TMPDIR") or ""
    if not base or os.path.realpath(base).rstrip("/") in ("/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp"):
        return progress.CALLS / ".pins"
    return Path(base) / f"devkit-calls-{os.getuid()}-{hashlib.sha256(str(core.STATE).encode()).hexdigest()[:8]}"


def pin_path(sid: str, tid: str):
    """Call `tid`'s pin: <pin_dir>/<a hash of the session id>/<tid>.pin, beside its `.contested` marker. The session
    folder is named by a hash, so a crew member's stamped command never shows the root thread's id."""
    from . import progress
    return pin_dir() / hashlib.sha256(str(sid).encode()).hexdigest()[:16] / f"{progress._safe_name(tid)}.pin"


def make_pin_folder(sid: str):
    """The session's pin folder, 0700 like the per-user one above it (the hook makes it before the stamp writes)."""
    import stat
    folder = pin_path(sid, "x").parent
    for d in (folder.parent, folder):
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        # checked and set through one descriptor opened without following a link: a link swapped in after a check
        # never redirects the mode change
        try:
            fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as e:
            raise OSError(f"the pin folder {d} is not a folder of this user's own") from e
        try:
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():  # another user's folder planted there
                raise OSError(f"the pin folder {d} is not a folder of this user's own")
            os.fchmod(fd, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions (0700 is owner-only: this tightens the folder)
        finally:
            os.close(fd)
    return folder


def pin_stamp(path) -> str:
    """The stamp's first action, for sh, bash and zsh (Codex's -lc too): the shell's own pid into the pin, in a
    subshell under noclobber so the user's command never runs with it, errors to /dev/null; a pin already there
    moves a pin already there onto the contested name (mv -f replaces a link planted there), and writes the marker
    when there was none to move: the call is then contested, never silently another process's."""
    import shlex
    pin, contested = shlex.quote(str(path)), shlex.quote(f"{path}.contested")
    return (f"(set -C; printf '%s\\n' \"$$\" > {pin}) 2>/dev/null || mv -f {pin} {contested} 2>/dev/null || "
            f"(: > {contested}) 2>/dev/null; ")


def _pid() -> int:
    return os.getpid()


def _proc(pid):
    """(parent pid, start time in seconds with microseconds) of process `pid`, from the kernel: proc_pidinfo on macOS,
    /proc on Linux. None when it has exited or the system does not say."""
    import struct
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if sys.platform == "darwin":
        import ctypes
        try:
            lib = _proc.lib = getattr(_proc, "lib", None) or ctypes.CDLL("/usr/lib/libproc.dylib")
            buf = ctypes.create_string_buffer(136)  # struct proc_bsdinfo (PROC_PIDTBSDINFO)
            if lib.proc_pidinfo(ctypes.c_int(pid), ctypes.c_int(3), ctypes.c_uint64(0), buf, ctypes.c_int(136)) != 136:
                return None
        except (OSError, AttributeError):
            return None
        sec, usec = struct.unpack_from("QQ", buf, 120)
        return struct.unpack_from("I", buf, 16)[0], sec + usec / 1e6
    try:
        with open(f"/proc/{pid}/stat") as fh:
            stat = fh.read()
        fields = stat[stat.rindex(")") + 2:].split()
        with open("/proc/stat") as fh:
            boot = next(float(ln.split()[1]) for ln in fh if ln.startswith("btime"))
        return int(fields[1]), boot + int(fields[19]) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, StopIteration, IndexError):
        return None


def _argv(pid):
    """The argument list of process `pid` as the kernel keeps it (sysctl KERN_PROCARGS2 on macOS, /proc/<pid>/cmdline
    on Linux): each argument whole, never `ps`'s joined and escaped line. None when it cannot be read."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if sys.platform == "darwin":
        import ctypes
        import struct
        try:
            libc = _argv.libc = getattr(_argv, "libc", None) or ctypes.CDLL("/usr/lib/libc.dylib", use_errno=True)
            mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
            size = ctypes.c_size_t(0)
            if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or not size.value:
                return None
            buf = ctypes.create_string_buffer(size.value)
            if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
                return None
        except (OSError, AttributeError):
            return None
        raw = buf.raw[:size.value]
        if len(raw) < 4:
            return None
        argc = struct.unpack_from("i", raw, 0)[0]
        rest = raw[4:]
        at = rest.find(b"\0")  # the executable's path, then NUL padding, then argc arguments
        if at < 0:
            return None
        while at < len(rest) and rest[at:at + 1] == b"\0":
            at += 1
        parts = rest[at:].split(b"\0")
        if argc < 0 or len(parts) < argc:
            return None
        return [p.decode("utf-8", "surrogateescape") for p in parts[:argc]]
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    return [p.decode("utf-8", "surrogateescape") for p in raw.split(b"\0")[:-1]] if raw else None


def _children(parents) -> list:
    """The processes whose parent is one of `parents`, from the process table (progress._ps)."""
    from . import progress
    want = {p for p in parents if isinstance(p, int)}
    return [pid for pid, (ppid, _) in progress._ps().items() if ppid in want]


def _read_pin(path):
    """The pid a pin names; None when there is no pin; -1 when it holds anything but digits."""
    try:
        with open(path) as fh:
            text = fh.read(32)
    except FileNotFoundError:
        return None
    except OSError:
        return -1
    return int(text) if _PIN_TEXT.fullmatch(text) else -1


# The pinned process writes its pin (the shell, as the stamp's first action, once its snapshot or login profile ran)
# or is the call's process its dev command pins: the pin is born after that process started and soon after. A pid the
# system gave to a newer process (the call's shell exited before its token was first used) started after the pin.
PIN_BIRTH_S = 20.0  # a slow login profile (Codex's -lc) runs before the stamp
PIN_CLOCK_S = 0.05  # the start time's and the file time's rounding


def _born(path):
    """When the pin was written: its birth time where the system keeps one (macOS), else its modification time (it
    is written once). None when it cannot be read."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return float(getattr(st, "st_birthtime", 0) or st.st_mtime)


def _write_pin(path, pid: int) -> bool:
    """The dev process pins the plain form's call to `pid` itself: True when this write made the pin (O_EXCL)."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        return False
    with os.fdopen(fd, "w") as fh:
        fh.write(f"{int(pid)}\n")
    return True


def used_path(sid: str, tid: str):
    """What the first verified use of call `tid`'s token saw: CALLS/<sid>/.<tid>.used (a dot-file: no reader of the
    ledger takes it for an entry), {pid, start, inode} of its pin. Later uses compare with it; the PostToolUse hook
    reads it (caller.mismatch, calls.pin-replaced, caller.token-unexplained-use)."""
    from . import progress
    return progress.CALLS / progress._safe_name(sid) / f".{progress._safe_name(tid)}.used"


def _read_used(sid: str, tid: str) -> dict:
    try:
        doc = json.loads(used_path(sid, tid).read_text() or "{}")
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _mark_used(sid: str, tid: str, pid=None, start=None, inode=None):
    """Record the first verified use: the pinned pid, its start time and the pin's inode (O_EXCL: the first one)."""
    try:
        fd = os.open(used_path(sid, tid), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        return
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps({"pid": pid, "start": start, "inode": inode}))


def uses_path(sid: str, tid: str):
    """The verified uses of call `tid`'s token, one JSON line each, beside its use mark: CALLS/<sid>/.<tid>.uses."""
    from . import progress
    return progress.CALLS / progress._safe_name(sid) / f".{progress._safe_name(tid)}.uses"


def _listed(entry: dict, args: list) -> bool:
    """Is this dev command one of the call text's own: typed with these arguments, or with an expansion in place of
    some (a loop's "$s", a substitution), as the shell reads the text? A dev command that a script, a test or a git
    hook the call started runs is not."""
    from . import shellparse
    if _typed_exactly(entry, args):
        return True
    text = str(entry.get("cmd") or "")
    if entry.get("cut") or not any(c in text for c in "$`"):
        return False
    try:
        calls = shellparse.shell_calls(text, None) or []
    except Exception:
        return False
    for argv, _ in calls:
        words = shellparse.dev_args(argv)
        if words and len(words) == len(args) and all(
                w == a or re.fullmatch("".join(".*" if e else re.escape(t) for e, t in _expansion_parts(w)), a, re.S)
                for w, a in zip(words, args)):
            return True
    return False


def _note_use(sid: str, tid: str, entry: dict, args: list) -> bool:
    """Append this verified use to its call's list: its arguments' hash (never their text), whether it changes state,
    and whether the call's text runs it (_listed). Appended (O_APPEND), so each dev command of one call keeps its own
    line; the PostToolUse hook audits the changes the text does not run (caller.token-unlisted-use). Never through a
    link: one planted there is removed and the list made anew. False when the use could not be recorded."""
    from . import progress
    line = json.dumps({"use": progress.args_hash(args), "change": bool(state_changing(args)),
                       "listed": _listed(entry, args)}) + "\n"
    path = uses_path(sid, tid)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        except OSError:
            if not os.path.islink(path):
                return False
            try:
                os.unlink(path)
            except OSError:
                return False
            continue
        try:
            return os.write(fd, line.encode()) == len(line.encode())
        except OSError:
            return False
        finally:
            os.close(fd)
    return False


def read_uses(sid: str, tid: str) -> list:
    """The verified uses _note_use appended for call `tid`, oldest first ([] when none)."""
    try:
        lines = uses_path(sid, tid).read_text().splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            doc = json.loads(ln)
        except ValueError:
            continue
        if isinstance(doc, dict):
            out.append(doc)
    return out


def _session_of(pid):
    """The process session of `pid` (0: this process), None when the system does not say."""
    try:
        return os.getsid(pid)
    except OSError:
        return None


def _read_entry(sid: str, tid: str):
    """The ledger entry of call `tid` in session `sid`'s folder only, None when it has none."""
    from . import core, progress
    e = core.read_json(progress.CALLS / progress._safe_name(sid) / f"{tid}.json", None)
    return e if isinstance(e, dict) else None


def _verify(agent: str, sid: str, value: str, now, args=()) -> _Verdict:
    """The checks of rule 0, in order (the module's docstring)."""
    from . import core, progress, state
    parsed = parse_token(value)
    if not parsed:
        return _Verdict("", code="caller.mismatch", why=BAD_TOKEN, detail="a malformed call token")
    tid, nonce = parsed
    sess = state.read(progress.session_path(agent, sid)) or {}
    opened, closed = sess.get("calls_opened") or [], sess.get("calls_closed") or []
    entry, kept = _read_entry(sid, tid), False
    if entry is not None and entry.get("closed"):  # its call closed; only its record could not be written
        entry = None
    if entry is None and tid in closed and not state_changing(args):
        # a background call closes as soon as it starts: its kept record names its caller for its reads and waits
        entry = next((e for e in _kept_background(sid, now or time.time()) if str(e.get("tool_use_id")) == tid), None)
        kept = entry is not None
    if entry is None:
        if tid in opened or tid in closed:
            return _Verdict(tid, code="caller.stale-token", why=STALE, detail="its call has closed")
        return _Verdict(tid, code="caller.foreign-session", why=FOREIGN,
                        detail=f"no call of session {str(sid)[:40]} has this token")
    if str(entry.get("session")) != str(sid) or str(entry.get("agent")) != str(agent):
        return _Verdict(tid, code="caller.foreign-session", why=FOREIGN, detail="its entry names another session")
    if not kept and (now or time.time()) - (entry.get("ts") or 0) > (entry.get("ttl") or progress.CALL_TTL):
        return _Verdict(tid, code="caller.stale-token", why=STALE, detail="its call ran past its time limit")
    if tid not in opened and not kept:
        return _Verdict(tid, code="calls.forged", why=BAD_TOKEN, detail="the hook never opened this call")
    want = entry.get("token_sha256")
    if not isinstance(want, str) or not hmac.compare_digest(want, hashlib.sha256(nonce.encode()).hexdigest()):
        return _Verdict(tid, code="caller.mismatch", why=BAD_TOKEN, detail="the token does not match its entry")
    caller = str(entry.get("caller") or "main")[:80]
    chain = _chain()
    pids = [p for p, _ in chain]
    agents = [p for p, line in chain if core.is_agent_process(line)]
    owner = entry.get("agent_pid")
    above = tuple(pids[:pids.index(owner) + 1] if owner in pids else pids)
    if not isinstance(owner, int):  # the hook could not tell which agent runs the call: no binding to a process
        _unpinned_once(agent, sid, tid, above)
        return _Verdict(tid, code="caller.token-unpinned", why=UNPINNED, ancestors=above,
                        detail="its call's agent process is not known, so its token binds to no process")
    if not agents:
        return _Verdict(tid, code="caller.stale-token", why=STALE, detail="no agent process runs it (detached)",
                        ancestors=above)
    if agents[0] != owner:
        return _Verdict(tid, code="caller.foreign-agent", why=FOREIGN, ancestors=above,
                        detail=f"its nearest agent process is {agents[0]}, its call's is {owner}")
    pinned = _pinned(sid, tid, entry, pids[:pids.index(owner)], above)
    if isinstance(pinned, _Verdict):
        return pinned
    if agent == "claude" and str(entry.get("host_thread") or "") != (os.environ.get(THREAD_VAR) or "").strip():
        return _Verdict(tid, code="caller.foreign-agent", why=FOREIGN, ancestors=above,
                        detail="it carries a Codex thread id its Claude Code call did not: another agent runs it")
    lane = (os.environ.get(LANE_VAR) or "").strip()[:80]
    if lane and lane != caller:
        return _Verdict(tid, code="caller.conflict", why=CONFLICT, ancestors=above,
                        detail=f"the token names {caller}, the lane variable {lane}")
    if agent == "codex":
        clash = _thread_evidence(agent, sid, caller, owner, tid)
        if clash:
            return _Verdict(tid, code="caller.conflict", why=CONFLICT, ancestors=above, detail=clash)
    # the token binds the use; the list only audits it when the call ends, so a use it cannot hold is recorded
    return _Verdict(tid, caller=caller, ancestors=above, unlisted=not _note_use(sid, tid, entry, list(args)))


def _pinned(sid: str, tid: str, entry: dict, below: list, above: tuple):
    """The call's pin, checked (the module's notes on the pin): the pinned pid, or the _Verdict that refuses."""
    from pathlib import Path

    def no(code, detail, why=FOREIGN_USE):
        return _Verdict(tid, code=code, why=why, ancestors=above, detail=detail)
    path = Path(str(entry.get("pin") or ""))
    if not entry.get("pin"):
        return no("caller.token-foreign-use", "its call recorded no pin")
    mark = _contest_mark(entry)
    if mark:
        return no("calls.pin-contested", "another process wrote its call's pin first: its token names nobody"
                  if mark == "pin" else "this call is marked contested: its token names nobody")
    pinned = _read_pin(path)
    if pinned == -1:
        return no("calls.pin-malformed", "its call's pin holds something other than a pid")
    me = _pid()
    if pinned is None:
        if entry.get("pin_by") != "dev":
            return no("caller.token-foreign-use", "its call's shell never pinned its token")
        # the plain form: the call's own process, right below the agent or below the loop that launches its calls
        # (its hooks' parent), which is dev itself when the shell exec'd it
        own = [p for p in below if p != entry.get("launcher_pid")]
        target = own[-1] if own else me
        pinned = target if _write_pin(path, target) else _read_pin(path)
        if not isinstance(pinned, int) or pinned < 0:
            return no("calls.pin-malformed", "its call's pin could not be read")
    if pinned != me and pinned not in below:
        return no("caller.token-foreign-use", f"its call is pinned to process {pinned}, and this command runs in "
                                              f"{me} below {list(below)}, not at or below it")
    info = _proc(pinned)
    if info is None:
        return no("caller.stale-token", f"its pinned process {pinned} has exited", STALE)
    parent, start = info
    if parent not in (entry.get("agent_pid"), entry.get("launcher_pid")):
        return no("caller.token-foreign-use", f"its pinned process {pinned} is not a call shell of the agent "
                                              f"(its parent is {parent})")
    ts, ttl = float(entry.get("ts") or 0), float(entry.get("ttl") or 0)
    if start < ts - 0.001 or start > ts + max(ttl, 1.0):
        return no("calls.pin-early", f"its pinned process {pinned} started at {start:.6f}, outside its call "
                                     f"({ts:.3f} for {ttl:g} s)")
    born = _born(path)
    if born is None or born < start - PIN_CLOCK_S or born > start + PIN_BIRTH_S:
        return no("caller.token-foreign-use", f"its pin was written at {born or 0:.6f}, outside the first "
                                              f"{PIN_BIRTH_S:g} s of its pinned process {pinned} (started "
                                              f"{start:.6f}): the pid went to another process after it")
    used = _read_used(sid, tid)
    if used and (used.get("pid") != pinned or used.get("start") != start):
        return no("caller.token-foreign-use", f"its pin named process {used.get('pid')} at its first use, and now "
                                              f"{pinned}: another process took that pid")
    if pinned != me and _session_of(0) != _session_of(pinned):
        return no("caller.stale-token", "it runs in a process session of its own (detached)", STALE)
    if not used:
        try:
            inode = os.stat(path).st_ino
        except OSError:
            inode = None
        _mark_used(sid, tid, pinned, start, inode)
    return pinned


# --------------------------------------------------------------------------- the text pin
# Where the agent checks the developer's permission rules on the command it runs (Claude Code outside
# bypassPermissions; Codex where it asks and would not apply devkit's rewrite), the hook leaves a plain dev call as
# the agent wrote it, so an allow rule such as Bash(dev progress:*) matches it, and records its ledger entry with
# pin_by "text". The dev command then names its caller by kernel facts, never by text alone (how "text-pinned"):
#   - exactly one open text entry of this session types this very dev command (_is_exactly); two, from any callers,
#     refuse a state change (identical concurrent commands);
#   - the nearest agent process above this command is the entry's, and the call's process sits right below it (or
#     below the loop that launches its calls): on Claude Code the call's shell with this command as its child, on
#     Codex that shell or the dev process it replaced itself with;
#   - the call's shell carries the entry's exact command in its own argument list, read from the kernel, in the
#     host's form (Claude Code: `eval '<command>' … && pwd -P >| …-cwd`; Codex: `<shell> -lc <command>`), compared by
#     the SHA-256 the entry keeps of the whole command;
#   - the hook listed the call as opened in the session record (a kept background call: as closed);
#   - that process started after the call opened and within progress.APPROVAL_S (the developer may take minutes to
#     approve a prompted call; its tool timeout counts from its start), in this command's process session; and it is
#     the call's by order: no later call of the session opened before it started, unless that later call has a shell
#     of its own: another process right below the agent that runs that call's own command and started before any
#     further call opened (or each such further call has a shell of its own too), so a later call's shell that
#     replaced itself with an earlier call's form vouches for nobody. A later call whose shell is not there could own
#     this process, and so could a closed one that ran in the background or got no clean result: the call is then
#     contested (_later_calls). A Claude Code foreground call that got its clean result owns no running process;
#   - on Codex, a dev process that replaced the call's shell (no shell is left to show the command) in a session
#     where a member may run carries the caller's own thread id (_thread_check). Any process can set CODEX_THREAD_ID,
#     so this keeps callers from being mixed up and proves nothing against a member that sets it: the checks above
#     and below are what hold;
#   - for a state change or a wait: no other process right below the agent, started in the call's time, claims the
#     same command (a second claimant), and the call's pin, written by its first dev command with O_EXCL, names that
#     same process; either one marks the call contested, which refuses it for everyone and is audited.
# A read that fails these checks falls to the ledger rules (text equality decides reads only); a wait or a state
# change names nobody. A plain call runs exactly one dev command: a second verified use of it is contested. A
# background call's entry closes as soon as it starts; its record is kept for BACKGROUND_KEEP_S for its reads and
# waits only (keep_background), with its pin and use marks, and a state change in it is refused with the foreground
# fix. A text call that ends with no result (refused, failed, expired, still open when the session ends, or left open
# by an agent killed before its session ended, sweep_gone) after a state change ran under its name is audited
# (calls.text-use-without-result).
BACKGROUND_KEEP_S = 60.0
AMBIGUOUS = ("{n} open calls of this session run this very command, so devkit cannot tell whose it is. An earlier one "
             "may still run, wait for approval, or have been denied at its prompt, which devkit is not told: it stays "
             "open until its time runs out, in about {m} minute(s). Run the command again then, or run a different one")
NOT_THE_CALL = ("devkit could not tie this command to its call (it does not run as that call's own dev command): run "
                "it again as a command of its own")
CONTESTED = "another process claimed this call's command first, so it names nobody: run it again in your own call"
UNRECORDED = ("devkit could not record this command's use of its call (the call's use list could not be written), so "
              "it changed nothing: run it again as a command of its own")
_CLAUDE_EVAL = re.compile(r"""(?:^|[\s;&|(])eval '((?:[^']|'"'"')*)'(?: \\?< /dev/null)? && pwd -P >\| \S+\Z""",
                          re.S)


def text_command(agent: str, argv) -> str | None:
    """The command a call's shell runs, from its kernel argument list in the host's form: Claude Code's
    `<shell> -c <script>` whose script ends with `eval '<command>' [< /dev/null] && pwd -P >| <cwd file>` (Claude Code
    2.1.330), Codex's `<shell> -lc <command>` or `-c` (Codex 0.167). None for any other form."""
    if not isinstance(argv, list) or len(argv) < 3 or os.path.basename(str(argv[0])) not in ("zsh", "bash", "sh"):
        return None
    if agent == "codex":
        return argv[-1] if len(argv) == 3 and argv[1] in ("-lc", "-c") else None
    if argv[1] not in ("-c", "-lc") and argv[1:3] != ["-c", "-l"]:
        return None
    m = _CLAUDE_EVAL.search(argv[-1])
    return m.group(1).replace("'\"'\"'", "'") if m else None


def _holds(agent: str, argv, entry: dict) -> bool:
    """Does a process's argument list run exactly the entry's whole command (its SHA-256, as the entry keeps it)?"""
    cmd = text_command(agent, argv)
    want = entry.get("cmd_sha256")
    return cmd is not None and isinstance(want, str) and hmac.compare_digest(
        hashlib.sha256(cmd.encode()).hexdigest(), want)


def _runs_dev(argv, args: list) -> bool:
    """Is a process's argument list a `dev` command with exactly these arguments (a shell that replaced itself with
    it)?"""
    if not isinstance(argv, list) or len(argv) <= len(args):
        return False
    head = argv[:len(argv) - len(args)] if args else argv
    return argv[len(argv) - len(args):] == list(args) and bool(head) and os.path.basename(str(head[-1])) == "dev"


def background_path(sid: str, tid: str):
    """A background shell call's kept record: CALLS/<sid>/.<tid>.background (a dot-file: no reader of the ledger takes it
    for an open call)."""
    from . import progress
    return progress.CALLS / progress._safe_name(sid) / f".{progress._safe_name(tid)}.background"


def ledger_folder(sid: str) -> int:
    """Session `sid`'s folder in the ledger, open for reading and never through a link (O_NOFOLLOW): a descriptor the
    caller closes, through which every name in it is read, written and removed (dir_fd). OSError when it is missing,
    a link or anything but a folder."""
    import stat
    from . import progress
    fd = os.open(progress.CALLS / progress._safe_name(sid), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    if not stat.S_ISDIR(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(errno.ENOTDIR, "not a folder", str(sid))
    return fd


def keep_background(entry: dict):
    """The PostToolUse of a background shell call (it closes as soon as it starts), text-pinned or carrying its token:
    its entry is kept for BACKGROUND_KEEP_S so the dev command it starts can still name its caller for a read or a
    wait (never a state change). Written in the session's folder opened without following a link (ledger_folder)."""
    if entry.get("pin_by") not in ("text", "shell", "dev") or not entry.get("background"):
        return
    name = background_path("-", str(entry.get("tool_use_id") or "")).name
    try:
        folder = ledger_folder(str(entry.get("session") or ""))
    except OSError:
        return
    try:
        for _ in range(2):  # a file or link already there is removed, never written through
            try:
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
            except FileExistsError:
                try:
                    os.unlink(name, dir_fd=folder)
                except OSError:
                    return
                continue
            except OSError:
                return
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(entry))
            return
    finally:
        os.close(folder)


def _read_kept(path, dir_fd: int | None = None):
    """A kept background record, never through a link, and only a regular file (a FIFO is never waited on); None when
    it cannot be read as one. `dir_fd`: the open folder `path` (a name) is in."""
    import stat
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        with os.fdopen(fd) as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                return None
            e = json.loads(fh.read(1 << 20))
    except (OSError, ValueError):
        return None
    return e if isinstance(e, dict) else None


def _kept_background(sid: str, now: float) -> list:
    """The background shell calls of session `sid` kept for their reads and waits; older ones go. The session's folder
    is opened once, never through a link (ledger_folder), and every record is read and removed through it."""
    out = []
    try:
        folder = ledger_folder(sid)
    except OSError:
        return []
    try:
        names = [n for n in os.listdir(folder) if n.startswith(".") and n.endswith(".background")]
        for name in names:
            e = _read_kept(name, folder)
            if e is None or now - float(e.get("ts") or 0) > BACKGROUND_KEEP_S:
                try:
                    os.unlink(name, dir_fd=folder)
                except OSError:
                    pass
                if e is not None:  # its pin and use marks, kept with it, go with it
                    _drop_marks(sid, str(e.get("tool_use_id") or ""))
                continue
            out.append(e)
    except OSError:
        return out
    finally:
        os.close(folder)
    return out


def contest_path(sid: str, tid: str):
    """Call `tid`'s contest flag in the ledger, for when its pin's marker cannot be made: CALLS/<sid>/.<tid>.contested
    (a dot-file: no reader of the ledger takes it for an entry). It goes with the call's other marks (_drop_marks), and
    one left behind goes at the next sweep (_sweep_flags)."""
    from . import progress
    return progress.CALLS / progress._safe_name(sid) / f".{progress._safe_name(tid)}.contested"


def _contest(entry: dict) -> bool:
    """Mark a text call contested, which refuses it for every later claimant: its pin's `.contested` marker, made where
    the name is, never through a link (O_EXCL, O_NOFOLLOW; anything already there, a link included, is the marker
    already: _text_verdict and _pinned refuse on it, os.path.lexists). When the marker cannot be made, the call's
    contest flag in the ledger (contest_path), made the same way, which they refuse on too (a kept background call's
    claims read it as well). The call's ledger entry is never rewritten: its close may remove it at any moment, and a
    rewrite would bring it back as an open call. False when neither could be made: the claimant is still refused, and
    a later one finds no contest, though the folders that refused these files refuse its pin or its use record too."""
    sid, tid = str(entry.get("session") or ""), str(entry.get("tool_use_id") or "")
    pin = str(entry.get("pin") or "")
    for path in ([f"{pin}.contested"] if pin else []) + ([str(contest_path(sid, tid))] if sid and tid else []):
        try:
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
            return True
        except FileExistsError:  # it is contested already
            return True
        except OSError:
            pass
    return False


def _contest_mark(entry: dict) -> str:
    """Where a call is marked contested: "pin" for its pin's marker (another process wrote its pin first, or a claim
    contested it), "ledger" for its contest flag in the ledger, "" when it is not (a link counts, dangling or not: one
    planted there is contest enough)."""
    pin = str(entry.get("pin") or "")
    sid, tid = str(entry.get("session") or ""), str(entry.get("tool_use_id") or "")
    if pin and os.path.lexists(f"{pin}.contested"):
        return "pin"
    return "ledger" if sid and tid and os.path.lexists(contest_path(sid, tid)) else ""


def _was_contested(entry: dict) -> bool:
    """A call already marked contested: its pin's marker or its contest flag in the ledger (_contest_mark)."""
    return bool(_contest_mark(entry))


def _types(entry: dict, args: list) -> bool:
    """Does a text call's entry type this very dev command? The hook records one only for a single plain dev call, so
    its text is that command (_is_exactly); a text the ledger cut or redacted counts by the exact argument list it
    kept the hash of (progress.args_hash)."""
    from . import progress
    if entry.get("cut") or entry.get("dev_args"):
        return bool(args) and progress.args_hash(args) in (entry.get("dev_args") or [])
    return _is_exactly(entry, args)


def _text_resolved(agent: str, sid: str, args: list, changes: bool, now, strict: bool = False):
    """Rule 0c, once per process (_RESOLVED): the text pin's verdict, None when no text call of this session types
    this command (the other rules then apply). `strict`: a wait, checked like a state change. A failure is audited, as
    a denial for a state change or a wait."""
    from . import progress
    strict = bool(changes or strict)
    key = ("text", os.getpid(), tuple(args), str(progress.CALLS), agent, sid, strict)
    if key not in _RESOLVED:
        _RESOLVED[key] = v = _text_verdict(agent, sid, args, changes, now, strict)
        if v is not None and v.code and (strict or v.code != "calls.text-ambiguous"):
            _audit_identity(v.code, v, agent, sid, args, "deny" if strict else "flagged")
    return _RESOLVED[key]


def _later_calls(agent: str, sid: str, entry: dict, start: float, call: int, parents: set, now) -> int:
    """How many later calls of the session could own process `call`, which started at `start`: the calls that opened
    after the entry's and before that process started, less each one that has a shell of its own (_own_shells). A
    closed call counts as an open one unless it is a Claude Code foreground call that got its clean result (its shell
    has exited, so it owns no running process); a closed call whose record keeps no command counts always."""
    calls = _calls_since(sid, entry, now)
    upto = start + 0.001  # ledger times are kept to the millisecond
    if not any(c["opened"] <= upto and not c["exited"] for c in calls):
        return 0
    owned = _own_shells(agent, calls, [p for p in _children(parents) if p != call])
    return sum(1 for i, c in enumerate(calls) if c["opened"] <= upto and not owned[i])


def _calls_since(sid: str, entry: dict, now) -> list:
    """The session's calls that opened at or after the entry's, other than the entry, oldest first: {opened, sha,
    exited, dup}. `dup`: another such call, or another open call of the session, types the same command, so whose
    shell is whose cannot be told."""
    from . import progress
    ts, tid = float(entry.get("ts") or 0), str(entry.get("tool_use_id") or "")
    # a closed call whose record could not be written stays among them, as one that may still own a running shell
    opened = [e for e in progress.open_calls(sid, now, closed_too=True) if not e.get("edit_paths")]
    out = [{"opened": float(e.get("ts") or 0), "sha": str(e.get("cmd_sha256") or ""), "exited": False}
           for e in opened if str(e.get("tool_use_id")) != tid and float(e.get("ts") or 0) >= ts]
    closed = []
    for c in progress.shells_since(sid, ts, now):
        o = float(c.get("opened") or 0)
        if c.get("edit_paths") or o < ts or str(c.get("tool_use_id") or "") == tid:
            continue
        if not c.get("tool_use_id") and o == ts and c.get("caller") == entry.get("caller"):
            continue  # a record that keeps no tool_use_id: the entry's own, closed as a background call
        closed.append({"opened": o, "sha": str(c.get("cmd_sha256") or ""), "exited": c.get("exited") is True})
    out += closed
    shas = [str(e.get("cmd_sha256") or "") for e in opened] + [c["sha"] for c in closed if not c["exited"]]
    for c in out:
        c["dup"] = shas.count(c["sha"]) > 1
    return sorted(out, key=lambda c: c["opened"])


def _own_shells(agent: str, calls: list, procs: list) -> list:
    """Which of `calls` (oldest first) has a shell of its own among `procs`, the other processes right below the agent:
    a process that runs exactly that call's command and started after the call opened, and before any other call
    opened unless each such call has a shell of its own too, each process for one call only. A process that started
    once another call had opened could be that call's (a decoy: a later call's shell that replaced itself with this
    call's form), so it vouches for this call only when every call opened in between is accounted for. A call whose
    shell exited counts as having one."""
    facts = {}
    for p in procs:
        info = _proc(p)
        if info is not None:
            facts[p] = (info[1], _argv(p))
    owned, used = [c["exited"] for c in calls], set()
    for i in range(len(calls) - 1, -1, -1):  # the latest first: whom a process could belong to is known then
        c = calls[i]
        if owned[i] or c["dup"] or not c["sha"]:
            continue
        for p in sorted(facts, key=lambda q: facts[q][0]):
            st, argv = facts[p]
            if p in used or st < c["opened"] - 0.001 or not _holds(agent, argv, {"cmd_sha256": c["sha"]}):
                continue
            if all(owned[j] for j in range(len(calls)) if j != i and c["opened"] <= calls[j]["opened"] <= st + 0.001):
                owned[i] = True
                used.add(p)
                break
    return owned


def _thread_check(agent: str, sid: str, caller: str, now) -> str:
    """A Codex dev process that replaced its call's shell: '' when no member may run in the session, or when
    CODEX_THREAD_ID names the caller (the member's own thread; the root thread for the tech lead once members showed
    threads of their own, codex_threads per-agent); else why it does not. A guard against mix-ups, not proof: any
    process can set the variable."""
    from . import progress, state
    run = progress.load_run(agent, sid) or {}
    if not (run.get("agents") or {}) and not _may_speak(progress, agent, sid, run, now):
        return ""
    thread = (os.environ.get(THREAD_VAR) or "").strip()
    if not thread:
        return "a member may run in this session, and the dev process that replaced its call's shell carries no thread id"
    if caller != "main":
        return "" if thread == caller else f"its thread {thread[:40]} is not its caller's"
    sess = state.read(progress.session_path(agent, sid)) or {}
    if thread == sid and sess.get("codex_threads") == "per-agent":
        return ""
    return f"its thread {thread[:40]} does not show the tech lead (members' threads: " \
           f"{sess.get('codex_threads') or 'not seen yet'})"


def _text_verdict(agent: str, sid: str, args: list, changes: bool, now, strict: bool = False):
    """The checks of the text pin, in order (the notes above). `strict`: a state change or a wait."""
    from pathlib import Path
    from . import core, progress, state
    now = now or time.time()
    found, ends = {}, {}
    for kept, calls in ((False, progress.open_calls(sid, now)), (True, _kept_background(sid, now))):
        for e in calls:
            if e.get("pin_by") == "text" and _types(e, args):
                tid_ = str(e.get("tool_use_id") or "")
                found.setdefault(tid_, e)
                ends.setdefault(tid_, float(e.get("ts") or 0) + (BACKGROUND_KEEP_S if kept else progress.call_limit(e)))
    if not found:
        return None
    if len(found) > 1:  # open and kept background calls alike: a foreground and a background wait of one text
        # how long until the calls before the newest are gone: a call denied at its prompt gets no result, so it
        # stays open for its whole time (progress.call_limit)
        newest = max(found, key=lambda t: float(found[t].get("ts") or 0))
        wait = max(ends[t] for t in found if t != newest) - now
        minutes = max(1, math.ceil(wait / 60))
        return _Verdict("", code="calls.text-ambiguous", why=AMBIGUOUS.format(n=len(found), m=minutes),
                        detail=f"{len(found)} calls run this very command; the earlier ones close in {wait:.0f} s")
    entry = next(iter(found.values()))
    tid = str(entry.get("tool_use_id") or "")

    def no(code, detail, why=NOT_THE_CALL, ancestors=()):
        return _Verdict(tid, code=code, why=why, detail=detail, ancestors=ancestors)
    if str(entry.get("session")) != str(sid) or str(entry.get("agent")) != str(agent):
        return no("caller.foreign-session", "its entry names another session", FOREIGN)
    sess = state.read(progress.session_path(agent, sid)) or {}
    if tid not in (sess.get("calls_opened") or []) and not (
            entry.get("background") and tid in (sess.get("calls_closed") or [])):
        return no("calls.forged", "the hook never opened this call")
    if entry.get("background") and changes:
        return no("caller.stale-token", "a background call's state change", STALE)
    chain = _chain()
    pids = [p for p, _ in chain]
    owner, launcher = entry.get("agent_pid"), entry.get("launcher_pid")
    above = tuple(pids[:pids.index(owner) + 1] if owner in pids else pids)
    if not isinstance(owner, int):
        return no("caller.token-unpinned", "its call's agent process is not known", UNPINNED, above)
    agents = [p for p, line in chain if core.is_agent_process(line)]
    if not agents:
        return no("caller.stale-token", "no agent process runs it (detached)", STALE, above)
    if agents[0] != owner:
        return no("caller.foreign-agent", f"its nearest agent process is {agents[0]}, its call's is {owner}", FOREIGN,
                  above)
    me = _pid()
    own = [p for p in pids[:pids.index(owner)] if p != launcher]
    if agent == "codex":  # the call's shell, or the dev process it replaced itself with
        if len(own) > 1:
            return no("caller.text-not-the-call", f"it runs {len(own)} processes below its call's agent", ancestors=above)
        call = own[0] if own else me
    else:  # Claude Code's call shell never replaces itself: this command is its child
        if len(own) != 1:
            return no("caller.text-not-the-call", f"it runs {len(own)} processes below its call's agent", ancestors=above)
        call = own[0]
    if call != me and not _holds(agent, _argv(call), entry):
        return no("caller.text-mismatch", f"its call's process {call} does not run the call's command", ancestors=above)
    if call == me and not _runs_dev(_argv(me), args):
        return no("caller.text-mismatch", "this process is not the call's dev command", ancestors=above)
    info = _proc(call)
    if info is None:
        return no("caller.stale-token", f"its call's process {call} has exited", STALE, above)
    parent, start = info
    if parent not in (owner, launcher):
        return no("caller.text-not-the-call", f"its call's process {call} is not a call shell of the agent (its parent "
                                              f"is {parent})", ancestors=above)
    # the shell starts once the developer approved the call (its tool timeout counts from then)
    ts, ttl = float(entry.get("ts") or 0), float(progress.APPROVAL_S)
    if start < ts - 0.001 or start > ts + ttl:
        return no("calls.pin-early", f"its call's process {call} started at {start:.6f}, outside its call "
                                     f"({ts:.3f} for {ttl:g} s)", ancestors=above)
    parents = {owner, launcher} - {None}
    def contested(detail):  # marked for every later claimant; a contest that could be recorded nowhere says so
        stuck = _contest(entry) if strict else True
        return no("calls.pin-contested", detail if stuck else f"{detail}; the contest could not be recorded",
                  CONTESTED, above)
    later = _later_calls(agent, sid, entry, start, call, parents, now)
    if later:
        return contested(f"{later} later call(s) of this session opened before its call's process {call} started, "
                         f"and could own it")
    if call != me and _session_of(0) != _session_of(call):
        return no("caller.stale-token", "it runs in a process session of its own (detached)", STALE, above)
    if agent == "claude" and str(entry.get("host_thread") or "") != (os.environ.get(THREAD_VAR) or "").strip():
        return no("caller.foreign-agent", "it carries a Codex thread id its Claude Code call did not", FOREIGN, above)
    caller = str(entry.get("caller") or "main")[:80]
    lane = (os.environ.get(LANE_VAR) or "").strip()[:80]
    if lane and lane != caller:
        return no("caller.conflict", f"the call names {caller}, the lane variable {lane}", CONFLICT, above)
    if agent == "codex":
        clash = _thread_evidence(agent, sid, caller, owner, tid)
        if clash:
            return no("caller.conflict", clash, CONFLICT, above)
        if call == me:
            why = _thread_check(agent, sid, caller, now)
            if why:
                return no("caller.text-no-thread", why, ancestors=above)
    if strict:
        path = Path(str(entry.get("pin") or ""))
        if not entry.get("pin"):
            return no("caller.token-foreign-use", "its call recorded no pin", ancestors=above)
        if _was_contested(entry):
            return no("calls.pin-contested", "another process claimed this call first", CONTESTED, above)
        for other in _children(parents):
            if other in (call, me):
                continue
            o = _proc(other)
            if o is None or not ts - 0.001 <= o[1] <= ts + ttl:
                continue
            argv = _argv(other)
            if _holds(agent, argv, entry) or (agent == "codex" and _runs_dev(argv, args)):
                return contested(f"process {other} claims this call's command too")
        pinned = call if _write_pin(path, call) else _read_pin(path)
        if pinned is None or pinned == -1:  # no pin could be written or read: nobody claimed it, nothing contested
            return no("calls.pin-malformed", "its call's pin could not be written or read", ancestors=above)
        if pinned != call:
            return contested(f"its call is pinned to process {pinned}, and this command runs in {call}")
        used = _read_used(sid, tid)
        if used and (used.get("pid") != call or used.get("start") != start):
            return contested(f"its pin named process {used.get('pid')} at its first use")
        if read_uses(sid, tid):  # a plain call runs exactly one dev command: a second one is not the call's
            return contested("its call already ran its one dev command")
        if not used:
            try:
                inode = os.stat(path).st_ino
            except OSError:
                inode = None
            _mark_used(sid, tid, call, start, inode)
        if not _note_use(sid, tid, entry, list(args)):  # the record of its use refuses a second one: none, no change
            return no("calls.use-unrecorded", "its call's use list could not be written", UNRECORDED, above)
    return _Verdict(tid, caller=caller, ancestors=above)


def _unpinned_once(agent: str, sid: str, tid: str, above: tuple):
    """A token whose entry names no agent process binds to its session only: audited once per session."""
    from . import progress, state
    if (state.read(progress.session_path(agent, sid)) or {}).get("token_unpinned"):
        return
    try:
        _session_update(progress.session_path(agent, sid), lambda d: d.update(token_unpinned=1))
    except (state.StateBusy, OSError, ValueError):
        return
    _audit_identity("caller.token-unpinned", _Verdict(tid, detail="its call's agent process is not known",
                                                      ancestors=above), agent, sid, sys.argv[1:], "flagged")


def _thread_evidence(agent: str, sid: str, caller: str, owner, tid: str = "") -> str:
    """A Codex call whose token holds: CODEX_THREAD_ID must not name another known thread ('' when it agrees, else
    the conflict). A member's verified token call records whether Codex gives members their own thread id (per-agent)
    or the root's (shared, which turns the root mapping off for the session for good)."""
    from . import progress
    thread = (os.environ.get(THREAD_VAR) or "").strip()
    if not thread:
        return ""
    lanes = set(((progress.load_run(agent, sid) or {}).get("agents") or {}))
    if caller == "main":
        return f"the token names the main thread, CODEX_THREAD_ID the lane {thread[:40]}" if thread in lanes else ""
    if thread != caller and thread in lanes:
        return f"the token names {caller[:40]}, CODEX_THREAD_ID the lane {thread[:40]}"
    if isinstance(owner, int) and thread in (caller, sid):
        _record_threads(agent, sid, "per-agent" if thread == caller else "shared", tid, caller)
    return ""


def _record_threads(agent: str, sid: str, seen: str, tid: str = "", caller: str = ""):
    """Record what a member's verified call showed of Codex's thread ids; "shared" is audited with that member's call
    (its tid and lane), since one such call turns the root mapping off for the session."""
    from . import progress, state
    path = progress.session_path(agent, sid)
    before = (state.read(path) or {}).get("codex_threads")
    if before == "shared" or before == seen:
        return
    try:
        _session_update(path, lambda d: d.update(codex_threads=seen) if d.get("codex_threads") != "shared" else None)
    except (state.StateBusy, OSError, ValueError):
        return
    if seen == "shared":
        _audit_identity("codex.thread-shared", _Verdict(tid, detail=f"the command of member {caller[:80]} carries the "
                                                                 f"root thread's id: the root thread no longer maps "
                                                                 f"to the tech lead"),
                        agent, sid, sys.argv[1:], "flagged")


def _thread_rule(agent: str, sid: str, args: list, changes: bool, now=None) -> str:
    """Rule 0b, a Codex command without a token. CODEX_THREAD_ID equal to a running lane's agent id names that lane;
    the session's own (root) thread names the tech lead only once a member's token call showed members carry thread
    ids of their own (codex_threads per-agent). Any child process can set the variable, so for a state change it
    counts only where Codex may ask (a session whose hook stamps every call never needs it: stamps_all), and only
    when an open call of that same lane (or the tech lead's, for the root) holds this very command. Every resolution
    is audited (caller.thread). '' otherwise."""
    from . import progress, state
    thread = (os.environ.get(THREAD_VAR) or "").strip()
    if not thread:
        return ""
    sess = state.read(progress.session_path(agent, sid)) or {}
    if changes and sess.get("stamps_all"):
        return ""
    run = progress.load_run(agent, sid) or {}
    lane = (run.get("agents") or {}).get(thread)
    who = thread if isinstance(lane, dict) and lane.get("state") in ("running", None) else \
        "main" if thread == sid and sess.get("codex_threads") == "per-agent" else ""
    if not who:
        return ""
    if changes and not any(str(c.get("caller")) == who and _is_exactly(c, args) for c in progress.open_calls(sid, now)):
        return ""  # a state change needs the caller's own call to be this very dev command, typed alone
    key = ("thread", os.getpid(), agent, sid, who, tuple(args))
    if key not in _NOTED:  # once per command (and never again from the audit record's own lookup of its actor)
        _NOTED.add(key)
        _audit_identity("caller.thread", _Verdict("", detail=f"CODEX_THREAD_ID names {who[:40]}, and its own open "
                                                             f"call is this very command" if changes else
                                                  f"CODEX_THREAD_ID names {who[:40]} for a read"),
                        agent, sid, args, "flagged" if changes else "allowed")
    return who


def _is_exactly(entry: dict, args: list) -> bool:
    """Is the call this one dev command and nothing else, as the shell reads it: one program, `dev` with exactly these
    arguments? A call that only mentions the words (`gh pr comment … 'dev progress add x'`), runs more, or was cut
    is not."""
    from . import shellparse
    if entry.get("cut") or not args:
        return False
    try:
        calls = shellparse.shell_calls(str(entry.get("cmd") or ""), None)
    except Exception:
        return False
    return bool(calls) and len(calls) == 1 and shellparse.dev_args(calls[0][0]) == list(args)


def _token_session(agent: str, sid: str) -> bool:
    """Does the hook stamp call tokens in this session (call_token in its record)?"""
    from . import progress, state
    return bool((state.read(progress.session_path(agent, sid)) or {}).get("call_token"))


def _no_token(agent: str, sid: str, args: list):
    key = (os.getpid(), agent, sid, tuple(args))
    if key in _NOTED:
        return
    _NOTED.add(key)
    _audit_identity("caller.no-token", _Verdict("", detail="a state change without a call token in a session whose "
                                                           "calls carry one"), agent, sid, args, "deny")


def _audit_identity(action: str, v: _Verdict, agent: str, sid: str, args, outcome: str):
    """One identity record with its fixed fields: the token's tid, the session and agent, the dev arguments (redacted
    and cut like the ledger's text), the parents up to the agent, the lane variable and the Codex thread, and the
    outcome. Never the nonce."""
    from . import progress
    try:
        from . import audit
        text = " ".join(["dev"] + [str(a) for a in args or []])[:progress.CALL_TEXT]
        audit.record(action, v.tid or "-", v.detail, outcome, tool_use_id=v.tid, session=str(sid)[:80],
                     host_agent=agent, command=text, ancestors=list(v.ancestors) or [p for p, _ in _chain()][:16],
                     lane_var=(os.environ.get(LANE_VAR) or "")[:80], thread=(os.environ.get(THREAD_VAR) or "")[:80])
    except Exception:
        pass


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


_ACTOR_RX = re.compile(r'"actor":\s*\{([^{}]*)\}')  # a record's actor: a flat object
_HOST_RX = re.compile(r'"host":\s*"([^"\\]*)"')
_LABEL_RX = re.compile(r'"label":\s*"([^"\\]*)"')
_TS_RX = re.compile(r'"ts":\s*([0-9]+(?:\.[0-9]+)?)')


def _audit_name(line: str) -> str:
    """The hostname an audit record names its Mac by: the actor's `host`, or its `label` when `host` is the machine
    id (records since the id). Read inside the actor only, so a `label` elsewhere (a decision's) is never a name."""
    a = _ACTOR_RX.search(line)
    h = _HOST_RX.search(a.group(1)) if a else None
    name = short_host(h.group(1)) if h else ""
    if MACHINE_ID.fullmatch(name):
        lb = _LABEL_RX.search(a.group(1))
        name = short_host(lb.group(1)) if lb else ""
    return name


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
                name, t = _audit_name(line), _TS_RX.search(line)
                if not name or not t or name in ("?", "localhost") or MACHINE_ID.fullmatch(name):
                    continue  # a record with neither a hostname nor a label says nothing about this Mac's names
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
            written = _write_machine(state, path, name, now)
        finally:
            _WRITING.on = False
        # a busy lock: another devkit process may have created the record meanwhile; its id beats the hostname
        doc = written or (doc if _valid(doc) else state.read(path))
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
