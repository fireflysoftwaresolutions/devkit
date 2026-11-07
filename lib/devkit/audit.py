# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The audit trail: one record, in one versioned schema, for everything devkit changes or decides.

Recorded: every `dev` command that changes something (tasks, plans, lessons, policies, files through dev rm/mv/cp,
git and GitHub actions, installs, config repairs and locks, jobs, gc, sleep), every guard refusal, blocked settings
change and missed status block, every write to the handoff repository, and every scheduled job run. `audit.level`
decides how much (security, changes or all); refusals, overrides and safeguard changes are recorded at every level.

Schema v2 (docs/audit.md, SCHEMA below): v, id (sortable), ts and time, actor, crew (when a crew member acts:
member, role, lane, parent, tool_use_id), project, branch, run, step, action (from ACTIONS), category, severity,
target, detail, outcome (ok, blocked, failed, recorded), why, evidence, ref, prev and hash. Readers accept v1.

Integrity: each record's `hash` is the SHA-256 of its canonical JSON; its `prev` links it to the record before it in
the same month file (lines that are not v2 records are sealed into that link), so an edit, a removal or an insertion
breaks the chain. The copy published to the handoff repository is the anchor: a local file must extend it.
`dev audit verify` checks both and reports the first broken link.

Where: ~/.local/state/devkit/audit/YYYY-MM.jsonl on this machine (append-only, 0600 in a 0700 folder), published
daily by the `audit` job to the handoff repository as audit/<user>@<host>/YYYY-MM.jsonl. Kept for
audit.retention.local_months here and team_months there, never under floor_months (at least 3), and not pruned at all
under audit.hold; the team policy (<handoff>/policy/audit.json) raises any of them. Pruning the handoff copy removes
files from its working copy only: git history keeps every published month. Crew message text, the developer's
approval lines, decision questions, free-text answers and home folders never reach a record written now, and secrets
are redacted as far as the redactor knows their shape; records sealed before these rules are cleaned whenever they
are read (docs/audit.md, Personal data). Query with `dev audit`; export with `dev audit export` (JSONL, CSV, OCSF,
CEF).
"""
from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import io
import json
import os
import re
import socket
import stat
import time
from pathlib import Path

from . import core
from .core import STATE

AUDIT = STATE / "audit"
V = 2

# --------------------------------------------------------------------------- the redactor (spec K7, R8)
#
# One redactor for everything devkit stores: audit records, CI log excerpts, hook captures and checkpoints. Each
# rule keeps what names the secret (the scheme, the user, the first four characters) and drops the secret itself; a
# value already redacted is never matched again, so redact(redact(x)) == redact(x) and SECRET finds nothing in it.

MARK = "…[redacted]"
_NOT_DONE = r"(?!…?\[redacted\])"  # also the bare "[redacted]" other stores write: a value already gone
# a key that names a secret, alone or inside a longer name (AWS_SECRET_ACCESS_KEY, client_secret, apiKey). What
# follows the keyword must start a new part of the name (_, -, or a capital in camelCase), so secrets:, tokens:,
# secretary: and passwordless: stay. No leading \w*: the match starts at the keyword and keeps the text before it.
_KEY = (r"(?:secret|token|passw(?:or)?d|passphrase|credentials?|api[_-]?key|access[_-]?key|private[_-]?key|"
        r"account[_-]?key)(?:(?:[_-]|(?-i:[A-Z](?=[a-z])))\w{0,64})?")
# what a command passes as one argument: a quoted value whole, else up to the next space
_ARG = _NOT_DONE + r"(?:\"[^\"\n]{1,256}\"|'[^'\n]{1,256}'|[^\s\"']{1,256})"
# the arguments between a command and the flag that carries its secret: the same command only, and not far
_ARGS = r"[^\n|;&]{0,200}?"
# Every rule runs in linear time: no nested unbounded quantifiers, and a bounded length wherever a match could
# otherwise scan far from each starting point.
_RULES = [
    # a private key loses its whole body, up to its END line (or the end of the text when it was cut); PGP keys end
    # their header with BLOCK
    (r"-----BEGIN (?P<kt>[A-Z0-9 ]*)PRIVATE KEY(?P<kb>(?: BLOCK)?)-----" + _NOT_DONE +
     r"(?s:.*?)(?:-----END (?P=kt)PRIVATE KEY(?P=kb)-----|\Z)", r"-----BEGIN \g<kt>PRIVATE KEY\g<kb>-----" + MARK),
    # a password in a URL: https://user:secret@host, redis://:secret@host. The user may be empty and the password may
    # hold / or @: it runs to the last @ before the host. host:port/path is not a password
    (r"(?i:\b(?P<u>[a-z][a-z0-9+.-]{0,32}://[^/\s:@]{0,256}:)" + _NOT_DONE +
     r"(?!\d{1,5}(?:[/?#\s]|\Z))\S{1,256}?@(?=[^\s/@?#]*(?:[/?#\s]|\Z)))", r"\g<u>" + MARK + "@"),
    (r"(?i:\b(?P<b>bearer)\s+" + _NOT_DONE + r"[A-Za-z0-9._~+/=-]{8,})", r"\g<b> " + MARK),
    (r"(?i:\b(?P<ba>authorization\s*[:=]\s*basic)\s+" + _NOT_DONE + r"[A-Za-z0-9+/=]{8,})", r"\g<ba> " + MARK),
    (r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}", "eyJ" + MARK),  # a JWT
    (r"(?i:\b(?:gh[pousr]_[0-9a-z]{20,}|github_pat_[0-9a-z_]{20,}|xox[abpr]-[0-9a-z-]{10,}|sk-[0-9a-z_-]{20,}|"
     r"AKIA[0-9A-Z]{16}|npm_[0-9a-z]{36,}|pypi-[0-9a-z_-]{16,}|AIza[0-9a-z_-]{35}|"
     r"[sr]k_(?:live|test)_[0-9a-z]{16,}|glpat-[0-9a-z_-]{20,}|hf_[0-9a-z]{30,}|ya29\.[0-9a-z_-]{20,}|"
     r"dckr_pat_[0-9a-z_-]{20,}))", None),  # tokens (Stripe, GitLab, Hugging Face, Google OAuth, Docker): keep 4
    (r"(?i:(?P<sw>hooks\.slack\.com/services/)" + _NOT_DONE + r"[a-z0-9_/-]{8,})", r"\g<sw>" + MARK),
    # a command-line flag that takes a secret: --password x, --github-token=x (not --password-stdin, --token-file, nor
    # gh's --with-token, which reads standard input)
    (r"(?i:(?P<f>--(?!with-token\b)(?:[a-z0-9]{1,32}-){0,4}(?:password|passwd|token|secret|api-?key|access-key|"
     r"private-key))"
     r"(?P<fs>[ =][\"']?)" + _NOT_DONE + r"[^\s…\"'-][^\s\"']*)", r"\g<f>\g<fs>" + MARK),
    # short flags that take a secret in the commands that use them (review batch 3); elsewhere -u, -p and -b are a
    # user id, a port or a branch. curl -u user:password keeps the user
    (r"(?P<cu>\bcurl\b" + _ARGS + r"\s(?:-[uU]|--user|--proxy-user)(?:\s+|=)?[\"']?[^\s:\"']{1,128}:)" + _NOT_DONE +
     r"[^\s\"']{1,256}", r"\g<cu>" + MARK),
    # sshpass -p password: -f, -d and -P take an argument of their own, -e and -v none
    (r"(?P<sp>\bsshpass(?:\s+(?:-[fdP]\s*[^\s-]\S{0,255}|-[evhV]\b)){0,4}\s+-p\s*)" + _ARG, r"\g<sp>" + MARK),
    # the MySQL and MariaDB clients: -p<password> attached (-p alone asks for it, and -p name names the database)
    (r"(?P<my>\b(?:mysql(?:dump|admin|import|show|check|pump)?|mariadb(?:-dump|-admin)?)\b" + _ARGS +
     r"\s-p)(?=[^\s-])" + _ARG, r"\g<my>" + MARK),
    (r"(?P<dl>\b(?:(?:docker|podman|buildah|nerdctl|oras|skopeo)\s+login|helm\s+registry\s+login)\b" + _ARGS +
     r"\s-p(?:\s+|=)?)" + _ARG, r"\g<dl>" + MARK),
    (r"(?P<gs>\bgh\s+secret\s+set\b" + _ARGS + r"\s(?:--body|-b)(?:\s+|=)?)" + _ARG, r"\g<gs>" + MARK),
    # a value echoed into a command that reads its secret from standard input: echo -n x | docker login
    # --password-stdin, printf '%s' x | gh auth login --with-token (review batch 4). The pipe is a single |
    (r"(?P<ec>\b(?:echo(?:\s+-[neE]{1,3}){0,3}|printf(?:\s+(?:'%s[^'\n]{0,8}'|\"%s[^\"\n]{0,8}\"))?)\s+)" +
     _NOT_DONE + r"(?:\"[^\"\n]{1,256}\"|'[^'\n]{1,256}'|[^\s\"'|;&]{1,256})" +
     r"(?=[ \t]{0,16}\|[^\n|;&]{0,200}?\s--(?:password-stdin|with-token)\b)", r"\g<ec>" + MARK),
    # GitHub's `Authorization: token X`: X has a digit, or is long, so "token required" stays
    (r"(?i:\b(?P<at>authorization\s*[:=]\s*[\"']?token)\s+" + _NOT_DONE +
     r"(?:(?=[a-z._~+/=-]{0,255}[0-9])[a-z0-9._~+/=-]{8,}|[a-z0-9._~+/=-]{20,}))", r"\g<at> " + MARK),
    # key = "value", "key": "value", key: 'value': a quoted value loses everything up to its closing quote
    (r"(?i:(?P<k>" + _KEY + r")(?P<s>[\"']?\s*[=:]\s*[\"'])" + _NOT_DONE +
     r"(?:(?<=\")[^\"\n]{1,512}|(?<=')[^'\n]{1,512}))", r"\g<k>\g<s>" + MARK),
    # key=value, KEY: value
    (r"(?i:(?P<k>" + _KEY + r")(?P<s>[\"']?\s*[=:]\s*)" + _NOT_DONE + r"[^\s…\"']\S*)", r"\g<k>\g<s>" + MARK),
]
_COMPILED = [(re.compile(p), rep) for p, rep in _RULES]
_BACKREFS = {"kt": "[A-Z0-9 ]*", "kb": "(?: BLOCK)?"}


def _plain(pattern: str) -> str:
    """A rule's pattern without its named groups, so that every rule fits in one expression."""
    pattern = re.sub(r"\(\?P=([a-z]+)\)", lambda m: _BACKREFS[m.group(1)], pattern)
    return re.sub(r"\(\?P<[a-z]+>", "(?:", pattern)


# finds any secret the rules know; the fixture tests assert it finds nothing in redacted text
SECRET = re.compile("|".join("(?:" + _plain(p) + ")" for p, _ in _RULES))
_EXTRA_CACHE: dict = {}


def _extra_rules(patterns) -> list:
    key = tuple(patterns or ())
    if key not in _EXTRA_CACHE:
        rules = []
        for p in key:
            try:
                rules.append(re.compile(p))
            except re.error:
                pass  # reported by `dev audit config` and `dev config validate`
        _EXTRA_CACHE[key] = rules
    return _EXTRA_CACHE[key]


def redact(text, extra=None) -> str:
    """Secrets in `text` replaced by …[redacted]: the K7 patterns, then audit.redact.extra (this machine's and the
    team policy's). `extra`: compiled patterns, when the caller already has them."""
    s = text if isinstance(text, str) else (str(text) if text else "")
    for rx, rep in _COMPILED:
        s = rx.sub(rep if rep is not None else (lambda m: m.group(0)[:4] + MARK), s)
    for rx in (extra if extra is not None else _extra_rules(settings()["redact"]["extra"])):
        s = rx.sub(MARK, s)
    return s


# --------------------------------------------------------------------------- personal data in a record (GDPR)
#
# A record says who acted and what changed. It never holds what people wrote to each other (crew messages quote the
# developer's requests), nor the OS user name inside a home path (compliance review, section 2). Every text field
# passes through here when a record is built, and every record when it is read (lift), so a query or an export
# shows none of them, even from a record sealed before a rule existed.

# another person's home folder too (a v1 record from another machine); /Users/Shared names nobody
_HOMES = re.compile(r"(?<![\w.~-])/(?:Users|home)/(?!Shared(?![^/\s]))[^/\s:'\"`]+")
# actions whose detail is the text of a message: only its length is recorded
MESSAGE_ACTIONS = {"crew.message"}
# the developer's evidence for a decision or a grant (compliance review 2, section 3): its detail is the line they
# typed or the question tool's question and label, so only its length is recorded (a hash of a short option is
# guessable: The Keymaker, M287); `answer` stays only when it is one of the decision's options. `covers`, the
# channel, the transcript uuid (which verify checks) and the chain say the rest
EVIDENCE_ACTIONS = {"crew.evidence"}
# the decision records, by the same rule (M285): crew.decision keeps the decision id, who asked, the option labels
# (ref.options, redacted as usual) and the question's length; crew.decided keeps the answer, and the answer it
# overrides, only when it names one of those options. Both keep the labels, so a record read again checks its
# answers against its own labels and reads back as it was written
DECISION_ACTIONS = {"crew.decision", "crew.decided"}
_ANSWERS = ("answer", "overrides")
# `dev crew ask|answer` arguments that name someone or something; every other argument is message text
_CREW_NAMES = ("--to", "--recommend", "--irreversible", "--target")
# what replaces a text: a record read again keeps it as it is, so cleaning on read never counts a marker's length
_UNTEXT = re.compile(r"\[message text not recorded: \d+\+? characters\]")
_UNASKED = re.compile(r"\[question not recorded: \d+\+? characters\]")
_FREE = re.compile(r"\[free text: \d+\+? characters\]")
# 6677b15 also wrote the line's SHA-256: such a marker reads back with its length only
_UNLINE = re.compile(r"\[approval line not recorded: (\d+) characters(?:, sha256 [0-9a-f]{64})?\]")
# the question tool adds this to the recommended option's label (progress._RECOMMENDED)
_RECOMMENDED = re.compile(r"\s*\(recommended\)\s*$", re.IGNORECASE)


def _home(s: str) -> str:
    """The home folder (this machine's, or any /Users/<name> and /home/<name>) as ~."""
    h = str(core.HOME).rstrip("/")
    if len(h) > 1 and h in s:
        s = re.sub(re.escape(h) + r"(?![\w.-])", "~", s)
    return _HOMES.sub("~", s) if "/" in s else s


def _clean(text, rx) -> str:
    return _home(redact(text, rx))


def _untext(n: int, cut: bool) -> str:
    return f"[message text not recorded: {n}{'+' if cut else ''} characters]"


def _unline(s: str) -> str:
    m = _UNLINE.fullmatch(s)
    return f"[approval line not recorded: {m.group(1) if m else len(s)} characters]"


def _norm(text) -> str:
    return " ".join(str(text).split()).casefold()


def _option(answer, options) -> str:
    """An answer as a record may keep it: the decision's option it names, read as progress.option_of reads it (the
    label in any case or spacing, with the question tool's "(Recommended)", or its letter: "B" for "B: reject"),
    else its length only. A length marker stays as it is."""
    s = str(answer)
    if _FREE.fullmatch(s):
        return s
    said = _norm(_RECOMMENDED.sub("", s))
    for o in options if isinstance(options, (list, tuple)) else []:
        o = str(o)
        if said and (said == _norm(o) or (":" in o and said == _norm(o.split(":", 1)[0]))):
            return o
    return f"[free text: {len(s)} characters]"


def _labels(options, rx) -> str:
    """A decision's option labels as its records keep them: redacted as usual, joined by " | ", at most 300
    characters. Its answers are checked against the same text split again, when written and when read."""
    opts = options.split("|") if isinstance(options, str) else options if isinstance(options, (list, tuple)) else []
    return _clean(" | ".join(t for t in (str(o).strip() for o in opts) if t), rx)[:300]


def _decided(rec: dict) -> dict:
    """A decision record's answers (crew.decided's detail, ref.answer, ref.overrides), each the option it names
    among the labels the record keeps, else its length only."""
    ref = rec.get("ref") if isinstance(rec.get("ref"), dict) else {}
    labels = [p for p in str(ref.get("options") or "").split(" | ") if p]
    if rec.get("action") == "crew.decided" and isinstance(rec.get("detail"), str) and rec["detail"]:
        rec["detail"] = _option(rec["detail"], labels)
    for k in _ANSWERS:
        if isinstance(ref.get(k), str) and ref[k]:
            ref[k] = _option(ref[k], labels)
    return rec


def _crew_args(target: str) -> str:
    """`dev crew ask|answer|decide …` as cli.py records it (arguments joined by spaces) without the message or the
    answer: the subcommand, the message or decision id, the naming arguments, and the length of the rest."""
    toks = target.split(" ")
    i = next((n for n, t in enumerate(toks) if t and not t.startswith("-")), None)
    if i is None or toks[i] not in ("ask", "answer", "decide"):
        return target
    if toks[i] == "decide":  # its options are unknown here; crew.decided keeps the answer, checked against them
        keep, answer = toks[:i + 2], " ".join(toks[i + 2:])
        if not answer or _FREE.fullmatch(answer):
            return target  # nothing to hide, or already recorded this way
        return " ".join(keep + [f"[free text: {len(answer)}{'+' if len(target) >= 300 else ''} characters]"])
    keep, text, n = toks[:i + 1], [], i + 1
    if toks[i] == "answer" and n < len(toks) and toks[n] and not toks[n].startswith("-"):
        keep.append(toks[n])  # the id of the message it answers
        n += 1
    while n < len(toks):
        t = toks[n]
        if t in _CREW_NAMES and n + 1 < len(toks):
            keep += [t, toks[n + 1]]
            n += 2
            continue
        if t == "--decision" or ("=" in t and t.split("=", 1)[0] in _CREW_NAMES):
            keep.append(t)
        else:
            text.append(t)
        n += 1
    body = " ".join(text)
    if _UNTEXT.fullmatch(body):
        return target  # already recorded this way
    return " ".join(keep + ([_untext(len(body), len(target) >= 300)] if body else []))  # cli.py cuts at 300


def _field(action: str, name: str, text, rx) -> str:
    """A record's target or detail as it may be stored: no message text, no approval line, no secret, no home
    folder. A text already replaced by its marker stays as it is."""
    s = text if isinstance(text, str) else str(text if text is not None else "")
    if name == "detail" and action in MESSAGE_ACTIONS:
        if _UNTEXT.fullmatch(s):
            return s
        return _untext(len(s), len(s) >= 300) if s else ""  # progress cuts a message at 300 before it gets here
    if name == "detail" and action in EVIDENCE_ACTIONS:
        return _unline(s) if s else ""
    if name == "detail" and action == "crew.decision":
        if not s or _UNASKED.fullmatch(s):
            return s
        return f"[question not recorded: {len(s)}{'+' if len(s) >= 300 else ''} characters]"  # progress cuts at 300
    if name == "target" and action == "dev.crew":
        s = _crew_args(s)
    return _clean(s, rx)


# --------------------------------------------------------------------------- settings (R3, R4, R7)

LEVELS = core.AUDIT_LEVELS


def policy_file() -> Path:
    return core.HANDOFF / "policy" / "audit.json"


def settings() -> dict:
    """The effective audit settings: this machine's config over the defaults, raised by the team policy's floor.
    Every field of the policy only records more or keeps longer: a policy asking for less changes nothing (R7)."""
    own, errors = core.audit_config(core.CFG.get("audit") or {})
    raw = core.read_json(policy_file(), None) if policy_file().exists() else None
    team, terrs = core.audit_config(raw, fill=False) if raw is not None else ({}, [])
    errors += [f"{core.tilde(policy_file())}: {e}" for e in terrs]
    r, tr = own["retention"], team.get("retention") or {}
    floor = max(r["floor_months"], tr.get("floor_months", 0))
    r.update(floor_months=floor, local_months=max(r["local_months"], tr.get("local_months", 0), floor),
             team_months=max(r["team_months"], tr.get("team_months", 0), floor))
    if team.get("level") and LEVELS.index(team["level"]) > LEVELS.index(own["level"]):
        own["level"] = team["level"]
    own["hold"] = own["hold"] or team.get("hold") is True
    own["publish"] = own["publish"] or team.get("publish") is True
    own["forward"] = own["forward"] or team.get("forward") or ""
    extra = list(own["redact"]["extra"])
    own["redact"] = {"extra": extra + [p for p in (team.get("redact") or {}).get("extra") or [] if p not in extra]}
    own["errors"] = errors
    own["policy"] = str(policy_file()) if raw is not None else ""
    return own


# --------------------------------------------------------------------------- the schema and the registry (R1)

CATEGORIES = ("change", "decision", "refusal", "security", "lifecycle", "job", "release", "data", "config")
SEVERITIES = ("info", "notice", "warning", "critical")
OUTCOMES = ("ok", "blocked", "failed", "recorded")
# categories kept at every level, with any blocked outcome, any `why` (an override) and audit's own records
ALWAYS = {"refusal", "security", "config"}

# every action devkit records, by its dotted prefix -> (category, severity); the longest prefix wins
ACTIONS = {
    "dev": ("change", "info"),
    "dev.config": ("config", "notice"), "dev.jobs": ("config", "notice"), "dev.policy": ("config", "notice"),
    "dev.install": ("lifecycle", "notice"), "dev.update": ("lifecycle", "notice"),
    "dev.uninstall": ("lifecycle", "warning"),
    "dev.release": ("release", "notice"), "dev.delivery": ("release", "info"),
    "dev.rm": ("data", "notice"), "dev.mv": ("data", "info"), "dev.cp": ("data", "info"),
    "dev.audit": ("data", "notice"), "dev.gc": ("job", "info"), "dev.sleep": ("job", "info"),
    "dev.sync": ("job", "info"),
    "audit": ("lifecycle", "notice"), "audit.config": ("config", "notice"), "audit.export": ("data", "notice"),
    "audit.verify": ("security", "info"), "audit.publish": ("security", "critical"),
    "guard": ("refusal", "warning"), "guard.ask": ("decision", "notice"), "guard.allow": ("decision", "info"),
    "git.backup": ("data", "info"), "git.backup-skipped": ("data", "notice"), "git.embargo": ("decision", "notice"),
    "stubs.gate": ("refusal", "warning"), "tests.gate": ("refusal", "warning"),
    "stop": ("refusal", "notice"), "stop.prevented": ("lifecycle", "info"),
    "config": ("config", "notice"), "config.blocked": ("security", "warning"),
    "canary": ("lifecycle", "notice"),
    "crew": ("decision", "info"), "crew.gate": ("refusal", "warning"),
    "crew.decision": ("decision", "notice"), "crew.decided": ("decision", "notice"), "crew.mode": ("decision", "notice"),
    "crew.start": ("lifecycle", "info"), "crew.resume": ("lifecycle", "info"), "crew.bind": ("lifecycle", "info"),
    "crew.finish": ("lifecycle", "info"), "crew.lost": ("lifecycle", "warning"),
    "crew.launch-failed": ("lifecycle", "warning"), "crew.unreported": ("lifecycle", "notice"),
    "crew.bind-conflict": ("security", "warning"), "crew.unbound": ("security", "notice"),
    "plan": ("decision", "info"), "raci.override": ("decision", "notice"),
    "handoff": ("data", "info"),
    "job": ("job", "info"), "jobs": ("config", "notice"), "ci.watch": ("job", "info"), "ops": ("job", "info"),
    "state": ("lifecycle", "warning"), "install": ("lifecycle", "notice"),
    "caller": ("security", "warning"), "caller.unverified": ("security", "notice"),
    "calls": ("lifecycle", "notice"), "progress": ("decision", "notice"), "session": ("lifecycle", "info"),
    "run": ("lifecycle", "notice"),  # a run orphaned by its ended session, or taken back by its resumed one
    "scope.write": ("security", "notice"),  # a review or read-only lane's shell call wrote files (F7); a caller sure
    #                                         of it passes severity="warning"
}

# a caller's keyword -> where it goes in the record; callers pass nothing else (anything else is dropped)
EXTRAS = {
    "why": "why", "project": "project", "branch": "branch", "run": "run", "step": "step",
    "category": "category", "severity": "severity",
    "member": "crew.member", "role": "crew.role", "agent_id": "crew.lane", "lane": "crew.lane",
    "parent": "crew.parent", "tool_use_id": "crew.tool_use_id", "launch": "crew.tool_use_id",
    "command": "evidence.command", "commit": "evidence.commit", "sha": "evidence.commit", "check": "evidence.check",
    "exit": "evidence.exit", "duration": "evidence.duration", "cwd": "evidence.cwd", "tool": "evidence.tool",
    "rule": "evidence.rule", "rules": "evidence.rule", "count": "evidence.count", "file": "evidence.file",
    "channel": "evidence.channel", "transcript_uuid": "evidence.transcript_uuid", "verified": "evidence.check",
    "flag": "evidence.flag", "paths": "evidence.file", "certainty": "evidence.check",
    "id": "ref.id", "by": "ref.by", "was": "ref.was", "type": "ref.type", "result": "ref.result",
    "answer": "ref.answer", "asked_by": "ref.asked_by", "irreversible": "ref.irreversible", "mode": "ref.mode",
    "overrides": "ref.overrides", "routed": "ref.routed", "session": "ref.session",
    # the irreversible-action gate (guard._gate_record): tier 1 or 2, setting audit or enforce, the grant's id, its
    # source (E1, E2, step or preapproval) and the owner/repo acted on (gh -R can name another)
    "tier": "ref.tier", "setting": "ref.setting", "grant": "ref.grant", "source": "ref.source", "repo": "ref.repo",
    # the decision's options, read by build to keep an answer that names one; crew.decision and crew.decided keep
    # them as ref.options, crew.evidence never stores them
    "options": "",
}
RAISE = {"tampered": "critical"}  # a caller's outcome word that makes the record more severe
EVIDENCE_KEYS = {p.split(".", 1)[1] for p in EXTRAS.values() if p.startswith("evidence.")}

# field -> (type, required); `dev audit` and docs/audit.md describe the same fields
SCHEMA = {
    "v": (int, True), "id": (str, True), "ts": (float, True), "time": (str, True), "actor": (dict, True),
    "crew": (dict, False), "project": (str, False), "branch": (str, False), "run": (str, False),
    "step": (str, False), "action": (str, True), "category": (str, True), "severity": (str, True),
    "target": (str, True), "detail": (str, True), "outcome": (str, True), "why": (str, False),
    "evidence": (dict, False), "ref": (dict, False), "prev": (str, True), "hash": (str, True),
}
_CUT = {"target": 300, "detail": 600, "why": 300, "run": 80}
_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford's base 32: ids sort by time


def registered(action: str) -> bool:
    parts = str(action or "").split(".")
    return any(".".join(parts[:i]) in ACTIONS for i in range(len(parts), 0, -1))


def kind(action: str):
    """(category, severity) of an action, from the longest registered prefix; ("change", "info") if none."""
    parts = str(action or "").split(".")
    for i in range(len(parts), 0, -1):
        hit = ACTIONS.get(".".join(parts[:i]))
        if hit:
            return hit
    return "change", "info"


def outcome_of(raw):
    """A caller's outcome -> (ok | blocked | failed | recorded, exit code or None, the raw text when it says more)."""
    s = str(raw if raw is not None else "ok").strip()
    low = s.lower()
    m = re.fullmatch(r"(?:exit|failed)\s*\(?(-?\d+)\)?", low)
    if m:
        code = int(m.group(1))
        return ("ok" if code == 0 else "failed"), code, None
    if low in ("ok", "allow", "allowed", "passed", "success", "done", ""):
        return "ok", None, None
    if low in ("blocked", "deny", "denied", "refused", "block"):
        return "blocked", None, (None if low == "blocked" else s[:40])
    if low in ("failed", "fail", "error", "timeout", "timed out", "dropped", "cancelled", "launch-failed"):
        return "failed", None, (None if low == "failed" else s[:40])
    return "recorded", None, (None if low == "recorded" else s[:40])


_LAST_ID = [0, 0]  # this process's last (milliseconds, random part): ids it makes always increase


def new_id(ts: float) -> str:
    """A ULID: 48 bits of milliseconds and 80 random bits, in Crockford's base 32 (26 characters)."""
    ms, rnd = int(ts * 1000), int.from_bytes(os.urandom(10), "big")
    if ms <= _LAST_ID[0]:
        ms, rnd = _LAST_ID[0], (_LAST_ID[1] + 1) % (1 << 80)
    _LAST_ID[:] = [ms, rnd]
    n = (ms << 80) | rnd
    return "".join(_B32[(n >> (5 * i)) & 31] for i in range(25, -1, -1))


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int(round(ts * 1000)) % 1000:03d}Z"


def check_record(rec: dict) -> list:
    """What breaks schema v2 in one record: [problem]."""
    out = []
    for k, (typ, need) in SCHEMA.items():
        if k not in rec:
            if need:
                out.append(f"{k} is missing")
            continue
        ok = isinstance(rec[k], (int, float)) and not isinstance(rec[k], bool) if typ is float else \
            isinstance(rec[k], typ) and not isinstance(rec[k], bool)
        if not ok:
            out.append(f"{k} is {type(rec[k]).__name__}, not {typ.__name__}")
    out += [f"unknown field {k}" for k in rec if k not in SCHEMA]
    for k, allowed in (("category", CATEGORIES), ("severity", SEVERITIES), ("outcome", OUTCOMES)):
        if rec.get(k) not in allowed:
            out.append(f"{k} {rec.get(k)!r} is not one of {', '.join(allowed)}")
    if not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", str(rec.get("id", ""))):
        out.append("id is not 26 characters of Crockford base 32")
    return out


def _value(v, extra):
    if isinstance(v, (list, tuple, set)):
        v = ", ".join(str(x) for x in v)
    if isinstance(v, bool) or v is None or isinstance(v, (int, float)):
        return v
    return _clean(str(v), extra)[:300]


def _place(rec: dict, key: str, v, extra) -> None:
    """Put a caller's keyword where EXTRAS says; `evidence` may be a dict of evidence keys. Others are dropped."""
    if key == "evidence" and isinstance(v, dict):
        for k, x in v.items():
            if k in EVIDENCE_KEYS and x not in (None, ""):
                rec.setdefault("evidence", {})[k] = _value(x, extra)
        return
    where = EXTRAS.get(key)
    if not where or v in (None, "", [], {}):
        return
    if "." in where:
        group, field = where.split(".", 1)
        rec.setdefault(group, {}).setdefault(field, _value(v, extra))
    else:
        rec[where] = _value(v, extra)


def actor() -> dict:
    sess = os.environ.get("DEVKIT_SESSION", "")
    return {"user": core.CFG.get("github_user") or os.environ.get("USER", "?"),
            "host": socket.gethostname().split(".")[0].lower(),
            "agent": os.environ.get("DEVKIT_AGENT") or ("claude" if os.environ.get("CLAUDECODE") else "human"),
            "session": sess.split(":", 1)[-1][:12] if sess else ""}


def _member(name: str):
    """A crew member's (id, title) from the roster; the name itself, lowercased, when it is not on it."""
    try:
        from . import crew
        m = crew.member(name)
    except Exception:
        m = None
    if m:
        return m["id"], m.get("title") or ""
    return re.sub(r"\s+", "-", str(name or "").strip().casefold()), ""


def _where(cwd=None):
    """(project id, branch) of the repository around `cwd`, from the files alone (no git process)."""
    try:
        here = Path(cwd or os.getcwd())
    except OSError:
        return "", ""
    for top in (here, *here.parents):
        dot = top / ".git"
        if not dot.exists():
            continue
        try:
            gitdir = dot
            if dot.is_file():
                gitdir = Path(dot.read_text().split("gitdir:", 1)[1].strip())
                gitdir = gitdir if gitdir.is_absolute() else (top / gitdir).resolve()
                main = gitdir.parents[2] if gitdir.parent.name == "worktrees" else top
            else:
                main = top
            head = (gitdir / "HEAD").read_text().strip()
        except (OSError, IndexError):
            return "", ""
        branch = head[16:] if head.startswith("ref: refs/heads/") else head[:12]
        try:
            project = str(main.resolve().relative_to(core.DEV.resolve()))
        except ValueError:
            project = main.name
        return project, branch
    return "", ""


def context(agent_id: str = "") -> dict:
    """Where a record belongs: project, branch, run and step from the session and its run (the F2/F4 ledger), and the
    crew member when one acts (DEVKIT_FROM_AGENT in its commands, or the agent id a hook passes)."""
    out: dict = {}
    env = os.environ
    aid = str(agent_id or env.get("DEVKIT_FROM_AGENT") or "")
    sess = env.get("DEVKIT_SESSION", "")
    if ":" in sess:
        from . import progress
        agent, sid = sess.split(":", 1)
        s = core.read_json(progress.session_path(agent, sid), {}) or {}
        out.update(project=str(s.get("project") or ""), branch=str(s.get("branch") or ""))
        run = core.read_json(progress.run_path(agent, sid), None)
        if isinstance(run, dict) and run.get("goal"):
            out["project"] = out["project"] or str(run.get("project") or "")
            out["run"] = str(run["goal"])[:80]
            agents = run.get("agents") or {}
            key = aid if aid in agents else next((k for k in agents if len(aid) >= 8 and k.startswith(aid)), None)
            owner = key or ("main" if not aid else None)
            if key:
                lane = agents[key]
                chain = progress.lane_chain(run, key) or [key]
                head = agents.get(chain[-1]) or lane
                mid, role = _member(head.get("persona") or lane.get("persona") or "")
                out["crew"] = {k: v for k, v in (("member", mid), ("role", role), ("lane", key),
                                                 ("parent", str(lane.get("parent") or "main")),
                                                 ("tool_use_id", str(lane.get("launch") or ""))) if v}
            step = next((x.get("name") for x in run.get("plan") or []
                         if owner and (x.get("owner") or "main") == owner and x.get("state") == "running"), "")
            if step:
                out["step"] = str(step)
    if aid and "crew" not in out:
        out["crew"] = {"lane": aid}
    if not out.get("project"):
        out["project"], branch = _where()
        out["branch"] = out.get("branch") or branch
    return {k: v for k, v in out.items() if v}


def build(action: str, target="", detail="", outcome="ok", extra: dict = None, cfg: dict = None,
          now: float = None) -> dict:
    """One v2 record, without its chain fields (prev, hash)."""
    cfg = cfg or settings()
    rx = _extra_rules(cfg["redact"]["extra"])
    ts = round(now or time.time(), 3)
    extra = dict(extra or {})
    options = extra.pop("options", None)
    if str(action) in EVIDENCE_ACTIONS and extra.get("answer") not in (None, ""):
        extra["answer"] = _option(extra["answer"], options)
    out, code, raw = outcome_of(outcome)
    cat, sev = kind(action)
    rec = {"v": V, "id": new_id(ts), "ts": ts, "time": iso(ts), "actor": actor(), "action": str(action)[:80],
           "category": cat, "severity": sev, "target": _field(str(action), "target", target or "", rx)[:300],
           "detail": _field(str(action), "detail", detail or "", rx)[:600], "outcome": out}
    for k, v in extra.items():
        _place(rec, k, v, rx)
    if str(action) in DECISION_ACTIONS:
        labels = _labels(options, rx)
        if labels:
            rec.setdefault("ref", {})["options"] = labels
        _decided(rec)
    if code is not None:
        rec.setdefault("evidence", {}).setdefault("exit", code)
    if raw:
        rec.setdefault("ref", {}).setdefault("result", raw)
    where = context(str((rec.get("crew") or {}).get("lane") or ""))
    for k in ("project", "branch", "run", "step"):  # the run goal and step names are free text, like target
        if where.get(k) and not rec.get(k):
            rec[k] = _clean(where[k], rx)
    crew = {**(rec.get("crew") or {}), **(where.get("crew") or {})}  # the run's lane wins over a caller's guess
    if crew.get("member") and "role" not in crew:
        crew["member"], role = _member(crew["member"])
        if role:
            crew["role"] = role
    if crew:
        rec["crew"] = crew
    if rec.get("category") not in CATEGORIES:
        rec["category"] = cat
    if rec.get("severity") not in SEVERITIES:
        rec["severity"] = sev
    floor = RAISE.get(str(raw or "").lower()) or ("warning" if out in ("failed", "blocked") else "info")
    if SEVERITIES.index(rec["severity"]) < SEVERITIES.index(floor):
        rec["severity"] = floor
    for k, n in _CUT.items():
        if isinstance(rec.get(k), str):
            rec[k] = rec[k][:n]
    return rec


def kept(rec: dict, level: str) -> bool:
    """Does `level` keep this record? Refusals, security and config events, blocked outcomes, overrides (a `why`)
    and audit's own records are kept at every level."""
    if level in ("changes", "all"):
        return True
    return (rec.get("category") in ALWAYS or rec.get("outcome") == "blocked" or bool(rec.get("why"))
            or str(rec.get("action", "")).startswith("audit."))


# --------------------------------------------------------------------------- writing and the chain (R2)

CHECKS: list = []  # more checks for verify: fn(record, path, line) -> problem text or None (spec C5 adds one)


def canonical(rec: dict) -> bytes:
    return json.dumps({k: v for k, v in rec.items() if k != "hash"}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def digest(rec: dict) -> str:
    return hashlib.sha256(canonical(rec)).hexdigest()


def seal(last_hash: str, tail: bytes) -> str:
    """The `prev` of a record: the hash of the last v2 record before it, with the lines between sealed into it."""
    return hashlib.sha256(last_hash.encode() + tail).hexdigest() if tail else last_hash


def _v2(line: bytes):
    if b'"v":2' not in line and b'"v": 2' not in line:
        return None
    try:
        r = json.loads(line)
    except ValueError:
        return None
    return r if isinstance(r, dict) and r.get("v") == V and isinstance(r.get("hash"), str) else None


def _tail(fh):
    """(hash of the last v2 record, the bytes after it) of an open month file."""
    fh.seek(0, 2)
    pos, buf = fh.tell(), b""
    while True:
        start = max(0, pos - 65536)
        fh.seek(start)
        buf = fh.read(pos - start) + buf
        pos = start
        lines = buf.split(b"\n")
        for i in range(len(lines) - 1, -1 if pos == 0 else 0, -1):  # lines[0] may be cut unless the file starts there
            r = _v2(lines[i])
            if r is not None:
                return r["hash"], b"\n".join(lines[i + 1:])
        if pos == 0:
            return "", buf


def _private_dir(d: Path):
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    if stat.S_IMODE(d.stat().st_mode) != 0o700:
        os.chmod(d, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions (0700 is owner-only: this tightens the folder)


def month_file(ts: float) -> Path:
    return AUDIT / time.strftime("%Y-%m.jsonl", time.localtime(ts))


def _append(rec: dict) -> dict:
    """Chain and append one record under the file's lock (writers in other processes wait for it)."""
    _private_dir(AUDIT)
    path = month_file(rec["ts"])
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a+b") as fh:
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            size = os.fstat(fd).st_size
            if size:
                fh.seek(size - 1)
                if fh.read(1) != b"\n":
                    fh.write(b"\n")
                    fh.flush()
            last, tail = _tail(fh)
            rec["prev"] = seal(last, tail)
            rec["hash"] = digest(rec)
            fh.write((json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            fh.flush()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    return rec


def _fingerprint(cfg: dict) -> dict:
    return {k: cfg[k] for k in ("level", "publish", "hold", "forward", "retention", "redact")}


def _flat(d: dict, at: str = "") -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(_flat(v, f"{at}{k}."))
        else:
            out[f"{at}{k}"] = v
    return out


def _show(v) -> str:
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, list):
        return f"{len(v)} pattern(s)"
    return str(v) if v != "" else "off"


def _note_settings(cfg: dict):
    """A change of the effective audit settings is recorded before anything else (R7): the settings a machine starts
    with are recorded when they differ from the defaults."""
    cur = _fingerprint(cfg)
    mark = AUDIT / ".settings.json"
    old = core.read_json(mark, None)
    if old == cur:
        return
    base = old if isinstance(old, dict) else _fingerprint({**core.DEFAULTS["audit"]})
    a, b = _flat(base), _flat(cur)
    changes = [f"{k}: {_show(a.get(k))} → {_show(b.get(k))}" for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
    if changes:
        rec = _append(build("audit.config", "audit settings", "; ".join(changes), "ok", cfg=cfg))
        _forward(rec, cfg)
    _private_dir(AUDIT)
    tmp = mark.with_name(f".settings.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(cur))
    os.replace(tmp, mark)


def record(action: str, target: str = "", detail: str = "", outcome: str = "ok", **extra):
    """Append one audit record (schema v2), chained to the one before it. -> the record, or None when the level
    leaves it out. Never raises."""
    try:
        cfg = settings()
        _note_settings(cfg)
        rec = build(action, target, detail, outcome, extra, cfg)
        if not kept(rec, cfg["level"]):
            return None
        rec = _append(rec)
        _forward(rec, cfg)
        return rec
    except Exception:
        return None


def _forward(rec: dict, cfg: dict):
    """audit.forward: append the record to a file (JSONL) or send it to syslog (CEF). Never raises."""
    target = cfg.get("forward") or ""
    if not target:
        return
    try:
        if target == "syslog" or target.startswith("syslog://"):
            line = _syslog_line(rec)
            if target == "syslog":
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                    s.connect("/var/run/syslog")
                    s.send(line)
            else:
                host, port = target[len("syslog://"):].rsplit(":", 1)
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.settimeout(2)
                    s.sendto(line, (host, int(port)))
            return
        path = core.expand(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "ab") as fh:
            fh.write((json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
    except Exception:
        pass


# --------------------------------------------------------------------------- which commands are recorded (R4)

# dev commands that only read; everything else is audited (and these too at audit.level all)
READ_ONLY = {"help", "ls", "go", "status", "open", "overview", "standup", "dashboard", "team", "report", "time",
             "files", "find", "grep", "diff", "canary", "du", "doctor", "version", "agents", "calver", "audit",
             "__complete", "hook", "guard", "plugin", "docs", "diagram", "stubs", "mcp",
             "test"}  # dev test run|plan|prove|audit: runs and reads tests (prove in a temp copy), changes nothing
READ_SUB = {("task", "ls"), ("task", "show"), ("task", "history"), ("task", None), ("plan", "ls"), ("plan", None),
            ("lesson", "ls"), ("lesson", "show"), ("lesson", "scan"), ("lesson", "review"), ("lesson", None),
            ("progress", None), ("progress", "time"), ("delivery", None), ("initiative", "ls"), ("initiative", "show"),
            ("wt", "ls"), ("config", "diff"), ("config", "status"), ("config", "show"), ("config", "validate"),
            ("jobs", "ls"), ("jobs", None), ("jobs", "show"), ("jobs", "logs"), ("jobs", "journal"), ("pr", "ls"),
            ("pr", "view"), ("pr", "checks"), ("pr", "diff"), ("run", "ls"), ("run", "view"), ("branch", "ls"),
            ("issue", "ls"), ("issue", "view"), ("sec", "scan"), ("desktop", "status"), ("crew", None),
            ("crew", "show"), ("crew", "brief"), ("crew", "inbox"), ("crew", "decisions"), ("crew", "next"),
            ("ci", None), ("ci", "status"), ("ci", "audit"), ("session", "brief"), ("session", "compactions"),
            ("session", None), ("alias", None)}
# `ci audit --fix` edits workflow files and `ci init` writes a pipeline (unless --dry-run): both are audited;
# `alias` alone lists the aliases, and `free` (or `gc`) without item numbers, --safe or --apply prints the plan
# crew sync, ask, answer, decide, delegate, decline, mode <guided|auto> and route --apply change state;
# `crew mode` alone and `crew route` without --apply only read
PLUMBING = {"__complete", "hook"}  # shell completion and the agents' hooks: never a command of their own


def audited(argv: list) -> bool:
    """Should this dev invocation be recorded?"""
    if not argv:
        return False
    if argv[0] not in PLUMBING and settings()["level"] == "all":
        return True
    return _changes(argv)


def _changes(argv: list) -> bool:
    cmd = argv[0]
    sub = next((x for x in argv[1:] if not x.startswith("-")), None)
    if cmd in READ_ONLY:
        return False
    if cmd == "sleep" and "--dry-run" in argv:
        return False
    if cmd in ("free", "gc"):  # the plan only reads; item numbers, --safe and --apply delete
        return any(x in ("--apply", "--safe") or x.isdigit() for x in argv[1:])
    if cmd == "crew" and sub == "route":
        return "--apply" in argv
    if cmd == "ci":
        if sub == "audit" and "--fix" in argv:
            return True
        if sub == "init":
            return "--dry-run" not in argv
        if sub not in (None, "status", "audit"):
            return False  # `dev ci <project>` shows another project's pipeline
    if cmd == "crew" and (sub == "sync" and "--check" in argv or sub == "mode" and argv[-1] == "mode"):
        return False
    if (cmd, sub) in READ_SUB or (cmd, None) in READ_SUB and sub is None:
        return False
    if any(x in ("--dry-run", "-n") for x in argv) and cmd in ("rm", "mv", "cp", "release", "push", "pr"):
        return False
    return True


# --------------------------------------------------------------------------- reading (R1 v1, R6)

_V1_CORE = {"ts", "user", "host", "agent", "session", "action", "target", "detail", "outcome"}


def _reclean(r: dict, rx) -> dict:
    """A v2 record as a reader sees it: its text fields cleaned with today's rules, which records sealed before them
    did not get (crew message text and home folders before 26.11.3, secrets the redactor learned since). The stored
    line and its hash stay as they are, so `verify` still checks them; a view that differs is marked `cleaned`."""
    action, out = str(r.get("action") or ""), dict(r)
    for k in ("target", "detail"):
        if isinstance(r.get(k), str):
            out[k] = _field(action, k, r[k], rx)[:_CUT[k]]
    for k in ("why", "project", "branch", "run", "step"):
        if isinstance(r.get(k), str):
            out[k] = _clean(r[k], rx)[:_CUT.get(k)]
    for group in ("evidence", "ref", "crew"):
        if isinstance(r.get(group), dict):
            out[group] = {k: _clean(v, rx)[:300] if isinstance(v, str) else v for k, v in r[group].items()}
    if action in EVIDENCE_ACTIONS and not _UNLINE.fullmatch(str(r.get("detail") or "")) and \
            (out.get("ref") or {}).get("answer"):
        out["ref"] = {**out["ref"], "answer": _option(out["ref"]["answer"], None)}  # never checked against options
    if action in DECISION_ACTIONS:
        _decided(out)  # out's ref is its own copy; a record sealed without labels keeps no answer as said
    if out != r:
        out["cleaned"] = True
    return out


def lift(r: dict, line: bytes = b"", extra=None) -> dict:
    """A record as schema v2 reads it, cleaned as a record is when it is written today: no secret, message text,
    approval line or home folder, so no query or export shows them. v2 records keep their fields (`cleaned` when
    that changed one: their hash covers the stored line); v1 records are mapped (v stays 1, no chain).
    `extra`: the compiled audit.redact.extra patterns (default: the built-in rules only)."""
    rx = extra if extra is not None else []
    if r.get("v") == V:
        return _reclean(r, rx)
    ts = float(r.get("ts") or 0)
    out, code, raw = outcome_of(r.get("outcome"))
    cat, sev = kind(r.get("action"))
    action = str(r.get("action") or "")
    rec = {"v": 1, "id": "V1" + hashlib.sha256(line or json.dumps(r, sort_keys=True).encode()).hexdigest()[:24].upper(),
           "ts": ts, "time": iso(ts), "actor": {k: str(r.get(k) or "") for k in ("user", "host", "agent", "session")},
           "action": action, "category": cat, "severity": sev,
           "target": _field(action, "target", r.get("target") or "", rx),
           "detail": _field(action, "detail", r.get("detail") or "", rx), "outcome": out}
    for k, v in r.items():
        if k not in _V1_CORE:
            _place(rec, k, v, rx)
    if action in DECISION_ACTIONS:
        _decided(rec)  # v1 kept no labels
    if code is not None:
        rec.setdefault("evidence", {}).setdefault("exit", code)
    if raw:
        rec.setdefault("ref", {}).setdefault("result", raw)
    if (rec.get("crew") or {}).get("member"):
        rec["crew"]["member"] = _member(rec["crew"]["member"])[0]
    if out in ("failed", "blocked") and SEVERITIES.index(sev) < SEVERITIES.index("warning"):
        rec["severity"] = "warning"
    return rec


def team_dir() -> Path:
    a = actor()
    return core.HANDOFF / "audit" / f"{a['user']}@{a['host']}"


def _files(team: bool = False) -> list:
    files = sorted((core.HANDOFF / "audit").rglob("*.jsonl")) if team and (core.HANDOFF / "audit").exists() else []
    return files + (sorted(AUDIT.glob("*.jsonl")) if AUDIT.exists() else [])


def _read(team: bool = False):
    seen = set()
    try:
        rx = _extra_rules(settings()["redact"]["extra"])
    except Exception:
        rx = []
    for f in _files(team):
        try:
            data = f.read_bytes()
        except OSError:
            continue
        for ln in data.splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            rec = lift(r, ln, rx)
            key = rec["id"]
            if key in seen:
                continue
            seen.add(key)
            yield rec


def when(text: str, end: bool = False):
    """'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM[:SS]' (local time) -> epoch; `end` makes a date mean the end of its day."""
    s = str(text or "").strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            t = time.mktime(time.strptime(s, fmt))
        except ValueError:
            continue
        return t + 86400 - 0.001 if end and fmt == "%Y-%m-%d" else t
    raise ValueError(f"“{text}” is not a date: YYYY-MM-DD or YYYY-MM-DDTHH:MM")


def query(since=None, until=None, days=None, action=None, actor_=None, project=None, member=None, category=None,
          severity=None, outcome=None, grep=None, team=False) -> list:
    """Records that match every filter, oldest first. `severity`: that level and above; `grep`: a regular
    expression (any case) over the action, target, detail and why; `member`: a crew member's name or id."""
    lo = when(since) if since else (time.time() - days * 86400 if days else None)
    hi = when(until, end=True) if until else None
    want_member = _member(member)[0] if member else None
    min_sev = SEVERITIES.index(severity) if severity in SEVERITIES else 0
    want_out = outcome_of(outcome)[0] if outcome else None
    rx = None
    if grep:
        try:
            rx = re.compile(grep, re.I)
        except re.error:
            rx = re.compile(re.escape(grep), re.I)
    out = []
    for r in _read(team):
        ts = r.get("ts") or 0
        if (lo and ts < lo) or (hi and ts > hi):
            continue
        if action and not r["action"].startswith(action):
            continue
        if actor_:
            a = r.get("actor") or {}
            if actor_ not in f"{a.get('user')}@{a.get('host')} {a.get('agent')}":
                continue
        if project and not (r.get("project") == project or str(r.get("project", "")).startswith(project.rstrip("/") + "/")):
            continue
        if want_member and (r.get("crew") or {}).get("member") != want_member:
            continue
        if category and r.get("category") != category:
            continue
        if min_sev and SEVERITIES.index(r.get("severity", "info")) < min_sev:
            continue
        if want_out and r.get("outcome") != want_out:
            continue
        if rx and not rx.search(" ".join(str(r.get(k) or "") for k in ("action", "target", "detail", "why"))):
            continue
        out.append(r)
    return sorted(out, key=lambda r: r.get("ts") or 0)


def records(days: float = 7, action: str = None, actor_: str = None, team: bool = False) -> list:
    """The old reader's API: the last `days`, by action prefix and actor."""
    return query(days=days, action=action, actor_=actor_, team=team)


def show(ref: str, team: bool = True):
    """The record with this id, or the only one whose id starts with it; None otherwise."""
    ref = str(ref or "").strip().upper()
    if not ref:
        return None
    hits = []
    for r in _read(team):
        if r["id"] == ref:
            return r
        if r["id"].startswith(ref):
            hits.append(r)
    return hits[0] if len(hits) == 1 else None


def stats(recs: list) -> dict:
    out = {"total": len(recs), "categories": {}, "severities": {}, "outcomes": {}, "days": {}}
    for r in recs:
        c = r.get("category", "change")
        out["categories"][c] = out["categories"].get(c, 0) + 1
        out["severities"][r.get("severity")] = out["severities"].get(r.get("severity"), 0) + 1
        out["outcomes"][r.get("outcome")] = out["outcomes"].get(r.get("outcome"), 0) + 1
        day = out["days"].setdefault(time.strftime("%Y-%m-%d", time.localtime(r.get("ts") or 0)), {})
        day[c] = day.get(c, 0) + 1
    return out


# --------------------------------------------------------------------------- verify (R2)

def _load_checks():
    """The modules that add CHECKS: progress checks developer evidence against the transcripts (spec C5)."""
    try:
        from . import progress  # noqa: F401
    except Exception:
        pass


def verify_file(path: Path) -> dict:
    """The chain of one month file: its first broken link, and what the CHECKS find in the intact records."""
    _load_checks()
    res = {"path": str(path), "records": 0, "v1": 0, "first": None, "checks": []}
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        res["first"] = {"path": str(path), "line": 0, "id": "", "kind": "unreadable", "problem": str(e)}
        return res
    last, tail = "", b""
    for n, raw in enumerate(data.splitlines(keepends=True), 1):
        body = raw.rstrip(b"\r\n")
        try:
            r = json.loads(body) if body.strip() else None
        except ValueError:
            r = None
        if isinstance(r, dict) and r.get("v") == V:
            res["records"] += 1
            if res["first"]:
                continue
            where = {"path": str(path), "line": n, "id": str(r.get("id") or "")}
            if r.get("hash") != digest(r):
                res["first"] = {**where, "kind": "edited", "problem": "the record does not match its hash: it was "
                                "changed after it was written"}
            elif r.get("prev") != seal(last, tail):
                res["first"] = {**where, "kind": "unlinked", "problem": "the record does not link to the one before "
                                "it: a record was removed, inserted or changed before it"}
            else:
                for chk in list(CHECKS):
                    try:
                        problem = chk(r, path, n)
                    except Exception as e:
                        problem = f"the check failed: {e}"
                    if problem:
                        res["checks"].append({**where, "kind": "check", "problem": str(problem)})
                last, tail = r["hash"], b""
            continue
        if isinstance(r, dict):
            res["v1"] += 1
        tail += raw
    return res


def _diverges(local: bytes, published: bytes):
    """None when the local file extends the published copy, else the first local line that differs (1-based)."""
    if local.startswith(published):
        return None
    a, b = local.splitlines(), published.splitlines()
    n = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    return n + 1


def verify(team: bool = True) -> dict:
    """Check every local month file's chain and, with `team`, the handoff copies and that each local file extends its
    published copy (the anchor). -> {ok, first, files, records, unpublished}; `unpublished`: v2 records chained but
    not yet in the published copy, so not yet anchored."""
    out = {"ok": True, "first": None, "files": [], "records": 0, "unpublished": 0 if team else None}
    local = sorted(AUDIT.glob("*.jsonl")) if AUDIT.exists() else []
    files = local + ([f for f in sorted((core.HANDOFF / "audit").rglob("*.jsonl"))]
                     if team and (core.HANDOFF / "audit").exists() else [])
    problems = []
    for f in files:
        r = verify_file(f)
        out["files"].append(r)
        out["records"] += r["records"]
        problems += ([r["first"]] if r["first"] else []) + r["checks"]
    if team:
        mine = team_dir()
        for f in local:
            pub = mine / f.name
            data = f.read_bytes()
            if not pub.exists():
                out["unpublished"] += sum(1 for ln in data.splitlines() if _v2(ln))
                continue
            p = pub.read_bytes()
            line = _diverges(data, p)
            if line:
                problems.append({"path": str(f), "line": line, "id": "", "kind": "diverged",
                                 "problem": f"the local file differs from the published copy {core.tilde(pub)} from "
                                            f"line {line}: records were changed or removed after they were published"})
            else:
                out["unpublished"] += sum(1 for ln in data[len(p):].splitlines() if _v2(ln))
    out["first"] = problems[0] if problems else None
    out["problems"] = problems
    out["ok"] = not problems
    return out


# --------------------------------------------------------------------------- publish and prune (R2 anchor, R3)

# GitHub refuses a pushed file over 100 MB, and from then on every handoff push, so no month copy is published past
# this. A stopgap for 26.11.3: 26.11.4 publishes daily gzipped segments instead of whole months.
PUBLISH_LIMIT = 45 * 1024 * 1024
HELD_FIX = ("nothing is lost: every record stays in this machine's trail and dev audit verify checks it; devkit "
            "26.11.4 publishes daily segments instead of whole months, so run dev update once it is released")


def _mb(n: int) -> str:
    return core.human_kb(n / 1024)


def _held(f: Path, size: int) -> dict:
    # a month file over PUBLISH_LIMIT: what the job's record, dev audit stats and dev doctor say about it
    pub = team_dir() / f.name
    published = pub.stat().st_size if pub.exists() else 0
    return {"month": f.stem, "path": str(f), "bytes": size, "published": published, "limit": PUBLISH_LIMIT,
            "text": f"{f.stem} is not published: {_mb(size)} here, over the {_mb(PUBLISH_LIMIT)} limit for a "
                    f"handoff copy (GitHub refuses files over 100 MB, and then every handoff push); its handoff "
                    f"copy stays at {_mb(published)}",
            "fix": HELD_FIX}


def _to_publish() -> list:
    return sorted(AUDIT.glob("*.jsonl"))[-3:] if AUDIT.exists() else []  # this machine's last three month files


def held() -> list:
    # the month files the audit job leaves unpublished because they passed PUBLISH_LIMIT
    if not settings()["publish"]:
        return []
    out = []
    for f in _to_publish():
        try:
            size = f.stat().st_size
        except OSError:
            continue
        if size > PUBLISH_LIMIT:
            out.append(_held(f, size))
    return out


def publish_checks() -> list:
    # dev doctor's rows (group, name, ok, detail, fix): one per held month, else one that passes; none when publish
    # is off
    if not settings()["publish"]:
        return []
    rows = [("Automation", "audit handoff copy", False, h["text"], h["fix"]) for h in held()]
    return rows or [("Automation", "audit handoff copy", True,
                     f"every month under the {_mb(PUBLISH_LIMIT)} limit for a handoff copy", "")]


def publish() -> list:
    """Copy this machine's last three month files into the handoff repository (audit/<user>@<host>/). A local file
    that no longer extends its published copy is never published over it, and one over PUBLISH_LIMIT is not
    published at all (its copy stays as it was). -> files changed."""
    if not settings()["publish"] or not AUDIT.exists():
        return []
    dest, changed = team_dir(), []
    for f in _to_publish():
        target = dest / f.name
        size = f.stat().st_size  # measured before reading, so a held file is never loaded on the daily run
        if size > PUBLISH_LIMIT:
            record("audit.publish", core.tilde(f), _held(f, size)["text"], "blocked", severity="warning")
            continue
        data = f.read_bytes()
        old = target.read_bytes() if target.exists() else b""
        if old == data:
            continue
        if old and not data.startswith(old):
            record("audit.publish", core.tilde(f), f"not published: it no longer extends {core.tilde(target)} "
                   f"(line {_diverges(data, old)}); run dev audit verify", "blocked", severity="critical")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        changed.append(target)
    return changed


def months_back(now: float, n: int) -> str:
    y, m = time.localtime(now)[:2]
    total = y * 12 + (m - 1) - n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _plan_prune(now: float, cfg: dict) -> dict:
    r = cfg["retention"]
    cut, tcut = months_back(now, r["local_months"]), months_back(now, r["team_months"])
    month = re.compile(r"\d{4}-\d{2}")
    local = [f for f in sorted(AUDIT.glob("*.jsonl")) if month.fullmatch(f.stem) and f.stem < cut] \
        if AUDIT.exists() and not cfg["hold"] else []
    mine = team_dir()
    team = [f for f in sorted(mine.glob("*.jsonl")) if month.fullmatch(f.stem) and f.stem < tcut] \
        if mine.exists() and not cfg["hold"] else []
    return {"keeps_from": cut, "team_keeps_from": tcut, "local": local, "team": team}


def prune(now: float = None) -> dict:
    """Remove local month files older than audit.retention.local_months and this machine's handoff copies older than
    team_months (never under floor_months; nothing under audit.hold), and record what was removed (audit.prune).
    The handoff copies leave the working copy only: git history keeps them (GIT_HISTORY).
    -> {removed: [months], team: [paths, for the handoff commit], records, held}"""
    now = now or time.time()
    cfg = settings()
    r = cfg["retention"]
    out = {"removed": [], "team": [], "records": 0, "held": cfg["hold"]}
    if cfg["hold"]:
        record("audit.prune", "local and team copies", "audit.hold is on (a legal hold): nothing pruned", "ok")
        return out
    plan = _plan_prune(now, cfg)
    for f in plan["local"]:
        try:
            n = sum(1 for ln in f.read_bytes().splitlines() if ln.strip())
            f.unlink()
        except OSError:
            continue
        out["removed"].append(f.stem)
        out["records"] += n
    for f in plan["team"]:
        try:
            f.unlink()
        except OSError:
            continue
        out["team"].append(f)
    if out["removed"] or out["team"]:
        parts = []
        if out["removed"]:
            parts.append(f"removed {len(out['removed'])} file(s), {out['records']} record(s) here: "
                         f"{', '.join(out['removed'])}; kept {r['local_months']} month(s) (from {plan['keeps_from']})")
        if out["team"]:
            parts.append(f"removed {len(out['team'])} handoff copy(ies): {', '.join(f.stem for f in out['team'])}; "
                         f"kept {r['team_months']} month(s) (from {plan['team_keeps_from']})")
        record("audit.prune", "local and team copies", "; ".join(parts), "ok", count=out["records"])
    return out


def _size(files) -> int:
    total = 0
    for f in files:
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total


# what team_months really removes, said wherever retention is shown (compliance review, section 1)
GIT_HISTORY = ("git history keeps every published month until the handoff repository's history is rewritten: "
               "pruning removes month files from its working copy only")


def config_report(now: float = None) -> dict:
    """`dev audit config`: the effective settings, where records live, their sizes and the next prune."""
    now = now or time.time()
    cfg = settings()
    local = sorted(AUDIT.glob("*.jsonl")) if AUDIT.exists() else []
    mine = team_dir()
    team = sorted(mine.glob("*.jsonl")) if mine.exists() else []
    plan = _plan_prune(now, cfg)
    at = None
    try:
        from . import jobs
        j = jobs.jobs().get("audit")
        at = jobs.next_run(j, now) if j else None
    except Exception:
        pass
    return {"settings": {k: v for k, v in cfg.items() if k not in ("errors", "policy")},
            "errors": cfg["errors"], "policy": cfg["policy"] or f"{policy_file()} (none)",
            "local": {"path": str(AUDIT), "files": len(local), "bytes": _size(local)},
            "team": {"path": str(mine), "files": len(team), "bytes": _size(team), "history": GIT_HISTORY},
            "next_prune": {"at": at, "held": cfg["hold"], "keeps_from": plan["keeps_from"],
                           "team_keeps_from": plan["team_keeps_from"],
                           "would_remove": [f.stem for f in plan["local"]],
                           "would_remove_team": [f.stem for f in plan["team"]]}}


# --------------------------------------------------------------------------- export (R5)

FORMATS = ("jsonl", "csv", "ocsf", "cef")
CSV_COLUMNS = ["id", "time", "user", "host", "agent", "session", "member", "role", "lane", "project", "branch", "run",
               "step", "action", "category", "severity", "outcome", "target", "detail", "why", "evidence", "ref",
               "hash"]
VENDOR = "Firefly Software Solutions"
OCSF_VERSION = "1.3.0"
_OCSF_SEVERITY = {"info": 1, "notice": 2, "warning": 3, "critical": 5}
_OCSF_STATUS = {"ok": (1, "Success"), "failed": (2, "Failure"), "blocked": (2, "Failure"), "recorded": (99, "Other")}
_CEF_SEVERITY = {"info": 2, "notice": 4, "warning": 6, "critical": 9}
_SYSLOG_SEVERITY = {"info": 6, "notice": 5, "warning": 4, "critical": 2}


def _cell(v) -> str:
    s = json.dumps(v, ensure_ascii=False, sort_keys=True) if isinstance(v, (dict, list)) else ("" if v is None else str(v))
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s  # a spreadsheet never runs a formula from it


def to_csv(recs: list) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for r in recs:
        a, c = r.get("actor") or {}, r.get("crew") or {}
        row = {**{k: a.get(k) for k in ("user", "host", "agent", "session")},
               **{k: c.get(k) for k in ("member", "role", "lane")}, **r}
        w.writerow([_cell(row.get(k)) for k in CSV_COLUMNS])
    return buf.getvalue()


def to_ocsf(r: dict) -> dict:
    """One record as an OCSF Base Event (class 0, activity Other); devkit's own fields are under `unmapped`."""
    sid, sname = _OCSF_STATUS.get(r.get("outcome"), (0, "Unknown"))
    a = r.get("actor") or {}
    sev = r.get("severity", "info")
    return {"activity_id": 99, "activity_name": r.get("action"), "category_uid": 0, "category_name": "Uncategorized",
            "class_uid": 0, "class_name": "Base Event", "type_uid": 99, "type_name": "Base Event: Other",
            "severity_id": _OCSF_SEVERITY.get(sev, 0), "severity": sev.capitalize(), "status_id": sid,
            "status": sname, "status_detail": r.get("outcome"), "time": int(round((r.get("ts") or 0) * 1000)),
            "message": f"{r.get('action')} {r.get('target') or ''}".strip()[:300],
            "metadata": {"version": OCSF_VERSION, "uid": r.get("id"), "log_name": "devkit audit",
                         "original_time": r.get("time"),
                         "product": {"name": "devkit", "vendor_name": VENDOR, "version": core.VERSION}},
            "actor": {"user": {"name": a.get("user") or ""}, "session": {"uid": a.get("session") or ""},
                      "app_name": a.get("agent") or ""},
            "device": {"hostname": a.get("host") or "", "type_id": 0},
            "unmapped": {k: r[k] for k in ("category", "project", "branch", "run", "step", "crew", "target", "detail",
                                           "why", "evidence", "ref", "prev", "hash", "v", "cleaned")
                         if r.get(k) not in (None, "")}}


def _cef_head(s) -> str:
    return str(s or "").replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _cef_ext(s) -> str:
    return str(s or "").replace("\\", "\\\\").replace("=", "\\=").replace("\r", "\\r").replace("\n", "\\n")


def to_cef(r: dict) -> str:
    a, c = r.get("actor") or {}, r.get("crew") or {}
    ext = [("rt", int(round((r.get("ts") or 0) * 1000))), ("suser", a.get("user")), ("shost", a.get("host")),
           ("act", r.get("action")), ("cat", r.get("category")), ("outcome", r.get("outcome")),
           ("msg", r.get("detail")), ("cs1Label", "id"), ("cs1", r.get("id")), ("cs2Label", "project"),
           ("cs2", r.get("project")), ("cs3Label", "member"), ("cs3", c.get("member")), ("cs4Label", "step"),
           ("cs4", r.get("step")), ("cs5Label", "why"), ("cs5", r.get("why")), ("cs6Label", "hash"),
           ("cs6", r.get("hash"))]
    head = ["CEF:0", VENDOR, "devkit", core.VERSION, r.get("action"), (r.get("target") or r.get("action"))[:128],
            _CEF_SEVERITY.get(r.get("severity"), 2)]
    return "|".join(_cef_head(x) if i else str(x) for i, x in enumerate(head)) + "|" + \
        " ".join(f"{k}={_cef_ext(v)}" for k, v in ext if v not in (None, ""))


def _syslog_line(r: dict) -> bytes:
    pri = 13 * 8 + _SYSLOG_SEVERITY.get(r.get("severity"), 6)  # facility 13: log audit
    stamp = time.strftime("%b %d %H:%M:%S", time.localtime(r.get("ts") or time.time()))
    return f"<{pri}>{stamp} {(r.get('actor') or {}).get('host') or 'localhost'} devkit: {to_cef(r)}".encode("utf-8")


def export(fmt: str = "jsonl", **filters) -> str:
    """The records that match `filters` (as query takes them) in one format: jsonl (as stored), csv, ocsf
    (one OCSF event per line) or cef (one CEF line per record)."""
    return render(fmt, query(**filters))


def render(fmt: str, recs: list) -> str:
    if fmt not in FORMATS:
        raise ValueError(f"--format takes {', '.join(FORMATS)}")
    if fmt == "csv":
        return to_csv(recs)
    if fmt == "ocsf":
        lines = [json.dumps(to_ocsf(r), ensure_ascii=False) for r in recs]
    elif fmt == "cef":
        lines = [to_cef(r) for r in recs]
    else:
        lines = [json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in recs]
    return "".join(ln + "\n" for ln in lines)


def export_to(fmt: str, path, **filters) -> int:
    """Write an export to `path` (0600: records hold names and hosts), record it (audit.export). -> records."""
    recs = query(**filters)
    text = render(fmt, recs)
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.chmod(path, 0o600)
    n = len(recs)
    record("audit.export", core.tilde(path), f"{fmt} · " + ", ".join(f"{k}={v}" for k, v in filters.items() if v),
           "ok", count=n, file=core.tilde(path))
    return n


# --------------------------------------------------------------------------- the command: dev audit

COMMANDS = ("", "verify", "export", "show", "stats", "config")


def add_arguments(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    p.add_argument("audit_cmd", nargs="?", default="", metavar="verify|export|show|stats|config",
                   help="verify the chain, export records, show one record, count them, or show the settings")
    p.add_argument("ref", nargs="?", help="show: the record's id (or a unique start of it)")
    p.add_argument("--days", type=float, help="the last N days (default 7, unless --since or --until)")
    p.add_argument("--since", help="from this date (YYYY-MM-DD or YYYY-MM-DDTHH:MM, local time)")
    p.add_argument("--until", help="up to this date, included")
    p.add_argument("--action", help="only actions starting with this, e.g. guard, job, task, rm")
    p.add_argument("--actor", help="user@host or agent")
    p.add_argument("-p", "--project", help="a project id, e.g. owner/repo")
    p.add_argument("--member", help="a crew member, e.g. Tank")
    p.add_argument("--category", choices=CATEGORIES)
    p.add_argument("--severity", choices=SEVERITIES, help="this severity and above")
    p.add_argument("--outcome", choices=OUTCOMES)
    p.add_argument("--grep", help="a regular expression over the action, target, detail and why")
    p.add_argument("--team", action="store_true", help="every machine (from the handoff repository)")
    p.add_argument("--format", help="table, json or csv; export: jsonl, csv, ocsf or cef")
    p.add_argument("--json", action="store_true", help="the same as --format json")
    p.add_argument("-o", "--out", help="export: write to this file (0600) instead of the terminal")
    p.add_argument("-n", "--lines", type=int, default=60)
    p.add_argument("--publish", action="store_true", help=argparse.SUPPRESS)  # the audit job
    p.add_argument("--quiet", action="store_true")
    return p


def _filters(a) -> dict:
    days = a.days if a.days is not None else (None if (a.since or a.until) else 7)
    return {"since": a.since, "until": a.until, "days": days, "action": a.action, "actor_": a.actor,
            "project": a.project, "member": a.member, "category": a.category, "severity": a.severity,
            "outcome": a.outcome, "grep": a.grep, "team": a.team}


def _who(r: dict) -> str:
    a, c = r.get("actor") or {}, r.get("crew") or {}
    return f"{a.get('user')}@{a.get('host')} {a.get('agent')}" + (f" · {c['member']}" if c.get("member") else "")


def cmd_audit(a) -> int:
    from . import ui
    act = a.audit_cmd or ""
    if a.publish:
        changed = publish()
        pruned = prune()
        paths = changed + pruned["team"]
        if paths:
            from . import work
            work._commit(paths, f"audit: {len(changed)} file(s) from {actor()['host']}"
                         + (f", {len(pruned['team'])} pruned" if pruned["team"] else ""))
        if not a.quiet:
            print(f"{ui.OK} audit published ({len(changed)} file(s) changed)"
                  + (f" · pruned {', '.join(pruned['removed'])}" if pruned["removed"] else ""))
            for h in held():
                print(f"  {ui.FAIL} {h['text']}\n      {ui.ARROW} {h['fix']}")
        return 0
    if act not in COMMANDS:
        ui.err(f"unknown audit command “{act}”: dev audit [verify|export|show <id>|stats|config]")
        return 2
    try:
        if act == "verify":
            return _cmd_verify(a, ui)
        if act == "export":
            return _cmd_export(a, ui)
        if act == "show":
            r = show(a.ref or "")
            if not r:
                ui.err(f"no record “{a.ref or ''}” (an id, or a unique start of one: dev audit --format json)")
                return 1
            print(json.dumps(r, indent=2, ensure_ascii=False))
            return 0
        if act == "config":
            return _cmd_config(a, ui)
        recs = query(**_filters(a))
    except ValueError as e:
        ui.err(str(e))
        return 2
    if act == "stats":
        st = stats(recs)
        st["held"] = held()
        if a.json or a.format == "json":
            print(json.dumps(st, indent=1))
            return 0
        print(ui.section(f"Audit statistics · {st['total']} record(s)"))
        for h in st["held"]:
            print(f"  {ui.FAIL} {h['text']}\n      {ui.ARROW} {h['fix']}")
        print(ui.table([[c, n] for c, n in sorted(st["categories"].items(), key=lambda kv: -kv[1])],
                       ["CATEGORY", "RECORDS"]))
        cats = [c for c in CATEGORIES if c in st["categories"]]
        print(ui.table([[d] + [row.get(c, 0) for c in cats] for d, row in sorted(st["days"].items())],
                       ["DAY"] + [c.upper() for c in cats]))
        return 0
    fmt = "json" if a.json else (a.format or "table")
    if fmt not in ("table", "json", "csv"):
        ui.err("--format takes table, json or csv (export: dev audit export --format jsonl|csv|ocsf|cef)")
        return 2
    if fmt == "json":
        print(json.dumps(recs, indent=1, ensure_ascii=False))
        return 0
    if fmt == "csv":
        print(to_csv(recs), end="")
        return 0
    f = _filters(a)
    span = f"last {f['days']:g} days" if f["days"] else " – ".join(x for x in (a.since or "", a.until or "") if x)
    print(ui.section(f"Audit · {span}" + (" · team" if a.team else "") + f" · {len(recs)} record(s)"))
    for r in recs[-a.lines:]:
        bad = r.get("outcome") in ("blocked", "failed")
        print(f"  {ui.dim(core.fmt_ts(r['ts']))} {(ui.red if bad else ui.cyan)(r.get('action', '')[:28]):<28} "
              f"{r.get('target', '')[:70]}  {ui.dim(_who(r))}" + (f"  {ui.red(r['outcome'])}" if bad else ""))
    print(ui.dim("\n  dev audit show <id> · dev audit stats · dev audit verify · dev audit export --format "
                 "jsonl|csv|ocsf|cef · dev audit config"))
    return 0


def _cmd_verify(a, ui) -> int:
    res = verify(team=True)
    for f in res["files"]:
        bad = f["first"] or f["checks"]
        print(f"  {ui.FAIL if bad else ui.OK} {core.tilde(f['path'])}  {f['records']} record(s)"
              + (f", {f['v1']} v1 line(s) outside the chain" if f["v1"] else ""))
    if res["ok"]:
        print(f"{ui.OK} the chain holds: {res['records']} record(s)"
              + (f" · {res['unpublished']} not yet published, so not yet anchored" if res["unpublished"] else ""))
    else:
        p = res["first"]
        print(f"{ui.FAIL} {core.tilde(p['path'])} line {p['line']}" + (f" ({p['id']})" if p.get("id") else "")
              + f": {p['problem']}")
        if len(res["problems"]) > 1:
            print(ui.dim(f"  and {len(res['problems']) - 1} more problem(s) in other files or checks"))
    record("audit.verify", f"{len(res['files'])} file(s)", "the chain holds" if res["ok"] else
           f"{res['first']['kind']} at {core.tilde(res['first']['path'])}:{res['first']['line']}",
           "ok" if res["ok"] else "failed", count=res["records"],
           **({} if res["ok"] else {"severity": "critical"}))
    return 0 if res["ok"] else 1


def _cmd_export(a, ui) -> int:
    fmt = a.format or "jsonl"
    if fmt not in FORMATS:
        ui.err(f"dev audit export --format takes {', '.join(FORMATS)}")
        return 2
    f = _filters(a)
    if a.out:
        n = export_to(fmt, a.out, **f)
        print(f"{ui.OK} {n} record(s) → {core.tilde(Path(a.out).expanduser())} ({fmt})")
        return 0
    recs = query(**f)
    print(render(fmt, recs), end="")
    record("audit.export", "stdout", fmt, "ok", count=len(recs))
    return 0


def _cmd_config(a, ui) -> int:
    c = config_report()
    if a.json or a.format == "json":
        print(json.dumps(c, indent=1, ensure_ascii=False))
        return 0
    s = c["settings"]
    r = s["retention"]
    print(ui.section("Audit settings (effective)"))
    print(ui.kv("level", s["level"], 16))
    print(ui.kv("retention", f"{r['local_months']} months here · {r['team_months']} in the handoff copy · "
                             f"floor {r['floor_months']}", 16))
    print(ui.kv("hold", "on: nothing is pruned" if s["hold"] else "off", 16))
    print(ui.kv("publish", "daily, to the handoff repository" if s["publish"] else "off", 16))
    print(ui.kv("forward", s["forward"] or "off", 16))
    print(ui.kv("redact.extra", f"{len(s['redact']['extra'])} pattern(s)", 16))
    print(ui.kv("team policy", core.tilde(c["policy"]), 16))
    print(ui.kv("records", f"{core.tilde(c['local']['path'])} · {c['local']['files']} file(s) · "
                           f"{core.human_kb(c['local']['bytes'] / 1024)}", 16))
    print(ui.kv("handoff copy", f"{core.tilde(c['team']['path'])} · {c['team']['files']} file(s) · "
                                f"{core.human_kb(c['team']['bytes'] / 1024)}", 16))
    print(ui.kv("git history", c["team"]["history"], 16))
    n = c["next_prune"]
    when_ = core.fmt_ts(n["at"]) if n["at"] else "with the next audit job"
    print(ui.kv("next prune", "held" if n["held"] else f"{when_}: keeps {n['keeps_from']} on here, "
                f"{n['team_keeps_from']} on in the handoff copy"
                + (f"; removes {', '.join(n['would_remove'])}" if n["would_remove"] else ""), 16))
    for e in c["errors"]:
        print(f"  {ui.FAIL} {e} (the default is used)")
    print(ui.dim("\n  Only the developer changes audit.*, in a terminal: dev config edit. The team policy "
                 "(policy/audit.json in the handoff repository) can only record more or keep longer."))
    return 0
