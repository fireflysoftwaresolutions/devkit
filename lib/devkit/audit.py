# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The audit trail: one record, in one versioned schema, for everything devkit changes or decides.

Recorded: every `dev` command that changes something (tasks, plans, lessons, policies, files through dev rm/mv/cp,
git and GitHub actions, installs, config repairs and locks, jobs, gc, sleep), every refusal of a `dev` command,
blocked settings change and missed status block, every write to the handoff repository, and every scheduled job run.
`audit.level` decides how much (security, changes or all); refusals and overrides are recorded at every level. The
records earlier versions wrote (guard.*, crew.evidence) stay readable and verifiable.

Schema v2 (docs/audit.md, SCHEMA below): v, id (sortable), ts and time, actor, crew (when a crew member acts:
member, role, lane, parent, tool_use_id), project, branch, run, step, action (from ACTIONS), category, severity,
target, detail, outcome (ok, blocked, failed, recorded), why, evidence, ref, prev and hash. Readers accept v1.

Integrity: each record's `hash` is the SHA-256 of its canonical JSON; its `prev` links it to the record before it
(lines that are not v2 records are sealed into that link), and a segment's first record to the end of the segment
before it, so an edit, a removal or an insertion, of a record or of a whole segment, breaks the chain. The manifest
anchors each closed segment by its size, SHA-256 and link; the copy published to the handoff repository anchors it
across machines. `dev audit verify` checks all of them and reports the first broken link. `dev audit reseal`
rewrites records sealed under older cleaning rules, only on a trail that verifies, keeping the old chain heads in a
reseal map that an audit.reseal record backs.

Where (see segments below): ~/.local/state/devkit/audit/ on this machine (append-only, 0600 in a 0700 folder): one
segment per UTC day, closed segments gzip-compressed and listed in manifest.json; month files an earlier devkit wrote
(YYYY-MM.jsonl) are read as they are. The `audit` job publishes each closed segment once to the handoff repository as
audit/<user>@<host>/YYYY-MM/<segment>.jsonl, with the machine's manifest; an earlier devkit's month file goes out as
one file per closed UTC day (YYYY-MM/YYYY-MM-DD.jsonl), each under PUBLISH_LIMIT (a day over it is held:
YYYY-MM-DD.held.json says so), after the one-file copy an even earlier devkit published. Kept for
audit.retention.local_months here and team_months there, never under floor_months (at least 3), and not pruned at all
under audit.hold; the team policy (<handoff>/policy/audit.json) raises any of them. Pruning the handoff copy removes
files from its working copy only: git history keeps every published month. Crew message text, the developer's
approval lines, decision questions, free-text answers, the free text of command lines (mask_command) and home
folders never reach a record written now, and secrets
are redacted as far as the redactor knows their shape; records sealed before these rules are cleaned whenever they
are read (docs/audit.md, Personal data). Query with `dev audit`; export with `dev audit export` (JSONL, CSV, OCSF,
CEF).
"""
from __future__ import annotations

import argparse
import calendar
import contextlib
import contextvars
import csv
import fcntl
import gzip
import hashlib
import io
import json
import os
import re
import socket
import stat
import sys
import time
import zlib
from pathlib import Path

from . import core, notes
from .core import STATE

AUDIT = STATE / "audit"
V = 2

# --------------------------------------------------------------------------- the redactor
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
    # devkit's own call token (identity.CALL_VAR): dead once its call closes, never kept
    (r"\bDEVKIT_CALL=[A-Za-z0-9_.-]+", "DEVKIT_CALL=" + MARK),
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
    # short flags that take a secret in the commands that use them; elsewhere -u, -p and -b are a user id, a port or
    # a branch. curl -u user:password keeps the user
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
    # --password-stdin, printf '%s' x | gh auth login --with-token. The pipe is a single |
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
    """Secrets in `text` replaced by …[redacted]: the _RULES above, then audit.redact.extra (this machine's and the
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
# developer's requests), nor the OS user name inside a home path. Every text field
# passes through here when a record is built, and every record when it is read (lift), so a query or an export
# shows none of them, even from a record sealed before a rule existed.

# another person's home folder too (a v1 record from another machine); /Users/Shared names nobody
_HOMES = re.compile(r"(?<![\w.~-])/(?:Users|home)/(?!Shared(?![^/\s]))[^/\s:'\"`]+")
# actions whose detail is the text of a message: only its length is recorded
MESSAGE_ACTIONS = {"crew.message"}
# the developer's evidence for a decision or a grant, as earlier versions recorded it, and any record of that action
# still written: its detail is the line they typed or the question tool's question and label, so only its length
# is recorded (a hash of a short option is guessable); `answer`
# stays only when it is one of the decision's options. `covers`, the channel, the transcript uuid and the chain say
# the rest. A question tool answer also keeps
# how the click became an option (evidence.match: exact, letter, words or free), whether the question was devkit's
# own (evidence.asked: exact or reworded), and the clicked label when the call offered it (ref.label, the tech
# lead's text like the options, redacted and cut to LABEL_CUT): never the developer's own text, never the question
# (the Security engineer's question audit decision, 26.11.4)
EVIDENCE_ACTIONS = {"crew.evidence"}
LABEL_CUT = 120
# the decision records, by the same rule (M285): crew.decision keeps the decision id, who asked, the option labels
# (ref.options, redacted as usual) and the question's length; crew.decided keeps the answer, and the answer it
# overrides, only when it names one of those options. Both keep the labels, so a record read again checks its
# answers against its own labels and reads back as it was written
DECISION_ACTIONS = {"crew.decision", "crew.decided"}
_ANSWERS = ("answer", "overrides")
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


def evidence_label(label, rx=None) -> str:
    """An offered label as crew.evidence keeps it: redacted, home folders as ~, at most LABEL_CUT characters."""
    rx = _extra_rules(settings()["redact"]["extra"]) if rx is None else rx
    return _clean(" ".join(str(label).split()), rx)[:LABEL_CUT]


def _labels(options, rx) -> str:
    """A decision's option labels as its records keep them: redacted as usual, joined by " | ", at most VALUE_CUT
    characters. Its answers are checked against the same text split again, when written and when read."""
    opts = options.split("|") if isinstance(options, str) else options if isinstance(options, (list, tuple)) else []
    return _clean(" | ".join(t for t in (str(o).strip() for o in opts) if t), rx)[:VALUE_CUT]


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


# --------------------------------------------------------------------------- free text in a command line
#
# A record that keeps a command line keeps only the words known to be safe, and each other word as its length, like a
# crew message: an allowlist fails closed (keep the safe words, never chase the unsafe ones: a masker that looked
# for the free text had five ways around it). The safe words: the
# program and its subcommands; option names; the values of NAME_OPTIONS and of dev's name positionals; ids (T-…, L-…,
# P-…, D<n>, M<n>, R<n>) and plain numbers; shell operators, reserved words and redirections. A line whose program the
# masker cannot find keeps its operators and each word's length. The records go to the handoff repository for 13
# months and are never rewritten.
# mask_command is the one function for it, and _command_field says which records keep a command line and where. The
# tables follow dev's parser (cli.build_parser): tests/test_audit.py fails while a command, a positional or an option
# that takes a value is missing from them, so a new command (dev room, 26.11.6) is added as data. Whether a class is
# right, only the masking tests check. A command missing from COMMAND_ARGS is not masked at all where cli.py records
# it (_command_field), so a new command's line reaches the trail whole until its row is added.

TEXT_MARK = "[text not recorded: {n} characters]"
# the classes of dev's positionals: text is free text; name names something (a member, a branch, a path, a project, a
# number) and stays; select picks an item by its id or number, or by a part of its title, so it stays only as an id or
# a number; pair is key=value (dev note set), whose key stays and whose value only when the key takes one of a fixed
# set (PAIR_VALUES); roster is a crew member's or a kind of work's id from devkit's roster, and free text otherwise;
# target is what `dev session` acts on (a session target, a message or escalation id, a claimed resource, or a
# transcript), which stays only in one of those shapes (PEER_TARGET): a message typed where the target goes is masked
ARG_CLASSES = ("text", "name", "select", "pair", "roster", "target")
PEER_TARGET = re.compile(r"@[0-9a-z-]{1,40}|all|repo|project|(?:project|initiative):[\w.-]+(?:/[\w.-]+)*|"
                         r"[SE]-[0-9a-z]{1,24}|(?:release|handoff|push|branch|item):[\w.-]+(?:/[\w.-]+)*(?:@[\w./-]+)?|"
                         r"custom:[a-z0-9-]{1,40}|[\w./~-]+\.jsonl")
# every dev command, by its path in dev's parser -> its positionals in the parser's order: dest:class, then the
# parser's nargs (? * + ..., nothing for one word). A word past the last positional takes its class when it repeats,
# else it is a select. "..." takes the rest of the line, options included (argparse.REMAINDER)
COMMAND_ARGS = {
    "": "",
    "__complete": "kind:name arg:text?",
    "adopt": "path:name",
    "agents": "action:name? agent:name?",
    "alias": "alias:name? project:name?",
    "audit": "audit_cmd:name? ref:name?",
    "backup": "branches:name*",
    "branch": "", "branch clean": "", "branch embargo": "branch:name?", "branch ls": "", "branch new": "name:name",
    "branch prune": "", "branch rename": "name:name",
    "calver": "",
    "canary": "",
    "ci": "query:name?",
    "claude": "query:name? args:text...",
    "clone": "repo:name",
    "codex": "query:name? args:text...",
    "commit": "paths:name*",
    "bug": "", "bug confirm": "bug:select detour:select", "bug detour": "fingerprint:select", "bug drop": "id:select",
    "bug ls": "", "bug pending": "", "bug report": "fingerprint:select?", "bug search": "words:text+",
    "bug send": "id:select", "bug share": "setting:name", "bug show": "bug:select", "bug sync": "", "bug top": "",
    "completion": "shell:name",
    "config": "config_cmd:name? key:name? value:select?",
    "cp": "paths:name+",
    "crew": "", "crew answer": "id:select text:text*", "crew ask": "text:text*",
    "crew read": "id:select", "crew ack": "id:select",
    "crew brief": "name:name task:text+", "crew decide": "id:select text:text+", "crew decisions": "",
    "crew decline": "step:select+", "crew delegate": "task:text+", "crew graph": "", "crew history": "",
    "crew withdraw": "id:select",
    "crew inbox": "", "crew mode": "mode:name?", "crew next": "", "crew owner": "paths:name+",
    "crew raci": "work:roster?",
    "crew route": "task:text*", "crew rules": "", "crew set": "name:name field:name value:text+",
    "crew show": "name:name+", "crew sync": "",
    "dashboard": "action:name?",
    "delivery": "", "delivery apply": "",
    "desktop": "action:name",
    "diagram": "rest:text...",
    "diff": "args:text*",
    "docker": "action:name",
    "docs": "check:name paths:name*",
    "doctor": "",
    "du": "query:name?",
    "ext": "", "ext ack": "", "ext add": "kind:name name:name", "ext disable": "name:name",
    "ext enable": "name:name", "ext ls": "", "ext outdated": "name:name?", "ext remove": "name:name",
    "ext update": "name:name",
    "files": "path:name?",
    "find": "pattern:text? path:name?",
    "free": "items:name*",
    "gc": "items:name*",
    "gh": "args:text...",
    "git": "args:text...",
    "go": "query:name branch:name?",
    "grep": "pattern:text paths:name*",
    "handoff": "query:name?",
    "handoff-import": "staging:name",
    "help": "",
    "hook": "event:name",
    "impeccable": "args:text...",
    "initiative": "", "initiative add": "slug:name project:name", "initiative ls": "", "initiative new": "slug:name",
    "initiative rm": "slug:name project:name", "initiative show": "slug:name",
    "install": "",
    "issue": "", "issue create": "", "issue ls": "", "issue view": "number:name",
    "jobs": "", "jobs disable": "name:name", "jobs enable": "name:name", "jobs exec": "name:name",
    "jobs install": "", "jobs journal": "", "jobs logs": "name:name", "jobs ls": "", "jobs run": "name:name",
    "jobs set": "name:name", "jobs show": "name:name",
    "lesson": "", "lesson add": "title:text+", "lesson anchor": "selector:select+", "lesson ls": "",
    "lesson prevent": "selector:select+", "lesson recur": "selector:select+", "lesson restore": "selector:select+",
    "lesson retire": "selector:select+", "lesson review": "", "lesson scan": "", "lesson show": "selector:select+",
    "lesson eval": "", "lesson lint": "", "lesson release": "lesson:select", "lesson supersede": "old:select",
    "ls": "",
    "mcp": "",
    "mv": "paths:name+",
    "new": "name:name",
    "note": "set:name pairs:pair+",
    "open": "query:name?",
    "overview": "",
    "plan": "", "plan approve": "path:name", "plan ls": "", "plan new": "title:text+", "plan revise": "path:name",
    "plan supersede": "path:name",
    "plugin": "action:name",
    "pr": "", "pr checks": "number:name?", "pr comment": "number:name?", "pr create": "", "pr diff": "number:name?",
    "pr ls": "", "pr merge": "number:name?", "pr ready": "number:name?", "pr review": "number:name?",
    "pr view": "number:name?",
    "progress": "", "progress add": "spec:text+", "progress assign": "which:select",
    "progress begin": "which:select", "progress block": "text:text+", "progress done": "text:text+",
    "progress confirm": "which:select",
    "progress history": "", "progress note": "text:text+", "progress reopen": "which:select",
    "progress skip": "which:select", "progress start": "goal:text+", "progress step": "text:text+",
    "progress time": "",
    "prompt": "action:name?",
    "push": "",
    "rebase": "",
    "release": "version_word:name?",
    "report": "",
    "rm": "paths:name*",
    "run": "", "run ls": "", "run rerun": "id:name", "run view": "id:name?",
    "sec": "scan:name query:name?",
    "session": "session_cmd:name target:target? text:text*",
    "setup": "what:name?",
    "sleep": "",
    "standup": "",
    "status": "query:name?",
    "stubs": "paths:name*",
    "sync": "",
    "task": "", "task add": "text:text+", "task block": "selector:select+", "task cancel": "selector:select+",
    "task claim": "selector:select+", "task delegate": "selector:select+", "task done": "selector:select+",
    "task edit": "selector:select+", "task history": "selector:select+", "task link": "selector:select+",
    "task ls": "", "task reconcile": "", "task move": "selector:select+", "task release": "selector:select+",
    "task reopen": "selector:select+", "task show": "selector:select+", "task start": "selector:select+",
    "task unblock": "selector:select+",
    "team": "",
    "test": "", "test audit": "paths:name*", "test plan": "", "test prove": "tests:name*", "test run": "paths:name*",
    "time": "",
    "update": "",
    "version": "",
    # dev visual: a source or a start URL can carry a token or a person's name, and the question is free text; record
    # ids (VA-…, VC-…, VF-…), map ids and the map file name something
    "visual": "", "visual capture": "source:text?", "visual check": "record:name",
    "visual compare": "before:name after:name", "visual component": "source:text", "visual describe": "capture:name",
    "visual finding": "", "visual finding add": "capture:name?", "visual finding close": "finding:name",
    "visual finding ls": "", "visual finding set": "finding:name", "visual finding show": "finding:name",
    "visual finding task": "finding:name", "visual journey": "", "visual journey walk": "journey:name?",
    "visual judge": "capture:name", "visual ls": "", "visual map": "", "visual map apply": "file:name",
    "visual map attach": "", "visual map check": "", "visual map component": "", "visual map component add": "id:name",
    "visual map concept": "", "visual map concept add": "name:name", "visual map crawl": "url:text",
    "visual map draw": "", "visual map edge": "", "visual map edge add": "from:name to:name", "visual map journey": "",
    "visual map journey add": "id:name", "visual map node": "", "visual map node add": "id:name",
    "visual map remove": "item:name", "visual map show": "", "visual map state": "",
    "visual map state add": "node:name state:name", "visual marks": "capture:name",
    "visual pass": "record:name pass_name:name", "visual product": "source:text", "visual report": "audit:name?",
    "visual screen": "source:text", "visual section": "url:text", "visual share": "record:name", "visual signal": "",
    "visual signal dismiss": "signal:name?", "visual signal ls": "record:name?", "visual start": "question:text",
    "visual verify": "record:name",
    "watch": "", "watches": "",
    "wt": "", "wt ls": "", "wt new": "branch:name", "wt rm": "target:name",
}
# a command's marker when it is not TEXT_MARK: the one its records held before 26.11.5, so they read back as stored
COMMAND_MARK = {"crew ask": "[message text not recorded: {n} characters]",
                "crew answer": "[message text not recorded: {n} characters]",
                "crew decide": "[free text: {n} characters]"}  # crew.decided keeps an answer that names an option
# dev's options whose value is free text, and those whose value names something (a member, a step, a path, a
# number, a date); an option in neither takes no value. dev visual's URLs, route prefixes, selectors, recipe steps,
# CSS and the page's own words are free text; its record ids, map ids, matrix settings and file paths name something
TEXT_OPTIONS = frozenset({
    "--why", "--check", "--next", "--title", "--body", "-m", "--message", "--log", "--note", "--done", "--evidence",
    "--cause", "--prevent", "--detect", "--fix", "--happened", "--error", "--match", "--retire-when", "--verified",
    "--breaking", "--options", "--flaky", "--plan", "--search", "--grep", "--hint", "--dev", "--text",
    "--cursor", "--request-id", "--command", "--section",
    "--avoid", "--clean", "--css", "--data", "--do", "--element", "--exclude", "--expect", "--impact", "--keeps",
    "--mask", "--na", "--on", "--relates", "--route", "--selector", "--state-do", "--task", "--trigger", "--when",
    "--within"})
NAME_OPTIONS = frozenset({
    "--action", "--activity", "--actor", "--after", "--agent", "--approve", "--approves", "--ask",
    "--assignee", "--at", "--base", "--body-file", "--branch", "--by", "--category", "--commit-template",
    "--generation", "--sha256", "--text-file",
    "--config", "--context", "--cwd", "--days", "--delay", "--depth", "--due", "--estimate", "--every", "--export",
    "--ext", "--fd", "--file", "--files", "--first", "--flush-at", "--fold", "--for", "--format", "--from", "--glob",
    "--hours",
    "--initiative", "--interval", "--kind", "--label", "--lang", "--license", "--limit", "--lines",
    "--max", "--max-per-file", "--member", "--min-severity", "--mode", "--model", "--notes-file", "--only", "--out",
    "--outcome", "--owner", "--path", "--pr", "--priority", "--project", "--projects", "--provider",
    "--recommend", "--ref", "--refs", "--renew", "--repo", "--rule", "--runner", "--runs", "--scope",
    "--session", "--set", "--severity", "--sha", "--since", "--size", "--slug", "--stack", "--state", "--step",
    "--step-timeout", "--steps", "--tag", "--ticket", "--timeout", "--to", "--type", "--undo",
    "--unified", "--until", "--url", "--version", "--weekly", "--worktree", "--write",
    "--again", "--as", "--audit", "--budget", "--choose", "--clock", "--commit", "--contrast", "--covered-by",
    "--decision", "--dpr", "--edge", "--evidence-capture", "--first-look", "--guide", "--include", "--journey",
    "--locale", "--mark", "--node", "--of", "--page", "--pages", "--pass", "--region", "--root", "--states",
    "--status", "--storage-state", "--theme", "--tier", "--timezone", "--viewport", "--vision",
    "-C", "-d", "-e", "-g", "-i", "-n", "-o", "-p", "-t", "-U", "-w"})
# NAME_OPTIONS that a command reads as a flag, or whose value it may leave out: there the next word is no value
NAME_FLAGS = {
    "ci": frozenset({"--runs", "--status"}), "crew ask": frozenset({"--decision"}),
    "crew decisions": frozenset({"--ask"}),
    "dashboard": frozenset({"--path", "--status"}),
    "ext disable": frozenset({"--project"}), "ext enable": frozenset({"--project"}),
    "find": frozenset({"--glob", "-g", "-p"}), "grep": frozenset({"--files", "-i", "-w"}),
    "handoff": frozenset({"--path"}), "pr review": frozenset({"--approve"}), "rm": frozenset({"--undo"}),
}
# NAME_OPTIONS whose value a command reads as free text: there the value is masked. dev visual's --base is a start
# URL, not a branch; its --state and --label are what the page shows; a journey's --actor and --decision describe a
# person and a condition in words. (Its --section, a route prefix, is free text in every command: TEXT_OPTIONS)
NAME_TEXT = {
    "visual capture": frozenset({"--state"}), "visual component": frozenset({"--state"}),
    "visual journey walk": frozenset({"--base", "--state"}), "visual map concept add": frozenset({"--label"}),
    "visual map journey add": frozenset({"--actor", "--decision"}),
    "visual screen": frozenset({"--state"}), "visual start": frozenset({"--state"}),
}
# dev's options that take no value: with TEXT_OPTIONS and NAME_OPTIONS, every option name dev's parser knows
FLAG_OPTIONS = frozenset((
    "--abort --accept --accept-ci --affected --all --all-branches --all-jobs --all-projects --allow-behind --amend "
    "--apply --as-pinned --ast --auto --auto-only --cached --case-sensitive --changed --ci-passed --claim --commands "
    "--comment --comments --continue --count --cross --discard --draft --dry-run --edit --fail-fast --failed --fill "
    "--final --finish --fixed --for-chat --force --force-in-use --force-with-lease --forced-colors --full-path "
    "--github --global --help --history --ignore-case --include-heavy --interactive --isolated-imports --json "
    "--keep-branch --keep-going --later --latest --lift --list --local --mark-passed --markdown --md --members "
    "--merge --mermaid --mine --move --name-only --names --new --no-check --no-copy --no-deps --no-git --no-index "
    "--no-open --no-prose --no-push --no-template --no-validate --now --offline --online --open --outline --outside "
    "--patch --pending --pending-launch --prerelease --preview --public --publish --queue --quiet --ready --rebase "
    "--record --recursive --refresh --regex --regions --remote --remove --replace --request-changes --resume --run "
    "--safe --scheduled --security --sign --skip --skip-heavy --skip-packages --squash --staged --standby --stat "
    "--stdin --stop --strict --syntax --tab --tasks --team --tracked --untraced --untriaged --unverified --unwip --urgent "
    "--verbose --wait --watch --wip --word --yes -F -R -a -f -h -l -r -s -v -y"
).split())
DEV_OPTIONS = TEXT_OPTIONS | NAME_OPTIONS | FLAG_OPTIONS
# dev note set's keys whose value is one of a fixed set: that value stays, every other key's value is masked
PAIR_VALUES = {"status": frozenset(notes.STATUSES), "priority": frozenset(notes.PRIORITIES)}
# other programs' commands whose options hold free text: git and gh messages, titles and bodies. Every other value
# of theirs that is no id or number is masked too; these are listed so a value that starts with - is still a value
_GH_TEXT = frozenset({"--title", "-t", "--body", "-b"})
_GH_RELEASE = frozenset({"--title", "-t", "--notes", "-n"})
OTHER_TEXT_OPTIONS = {
    "git commit": frozenset({"-m", "--message"}), "git tag": frozenset({"-m", "--message"}),
    "git merge": frozenset({"-m", "--message"}), "git stash": frozenset({"-m", "--message"}),
    "git stash push": frozenset({"-m", "--message"}), "git notes": frozenset({"-m", "--message"}),
    "git notes add": frozenset({"-m", "--message"}),
    "gh pr create": _GH_TEXT, "gh pr edit": _GH_TEXT, "gh pr comment": frozenset({"--body", "-b"}),
    "gh pr review": frozenset({"--body", "-b"}), "gh pr merge": frozenset({"--subject", "-t", "--body", "-b"}),
    "gh pr close": frozenset({"--comment", "-c"}), "gh issue create": _GH_TEXT, "gh issue edit": _GH_TEXT,
    "gh issue comment": frozenset({"--body", "-b"}), "gh issue close": frozenset({"--comment", "-c"}),
    "gh release create": _GH_RELEASE, "gh release edit": _GH_RELEASE,
}
_GIT_WORDS = frozenset((
    "add am apply archive bisect blame branch bundle cat-file check-ignore checkout cherry-pick clean clone commit "
    "config describe diff fetch for-each-ref format-patch fsck gc grep help init lfs log ls-files ls-remote "
    "maintenance merge merge-base mv name-rev notes pull push range-diff rebase reflog remote reset restore revert "
    "rev-list rev-parse rm shortlog show show-ref sparse-checkout stash status submodule switch symbolic-ref tag "
    "update-ref version worktree bad drop good list lock move pop prune remove repair save set-url skip start "
    "unlock update").split())
_GH_WORDS = frozenset((
    "alias api attestation auth browse cache codespace completion config extension gist gpg-key issue label org pr "
    "project release repo ruleset run search secret ssh-key status variable workflow archive cancel checkout checks "
    "clone close comment create delete deploy-key develop diff disable download edit enable fork list lock login "
    "logout merge pin ready refresh rename reopen rerun review set setup-git sync token transfer unarchive unlock "
    "unpin update-branch upload view watch").split())
# other programs: their subcommand words and how many make the path; their options before the subcommand that take a
# value; and the options whose value names something (a folder, a repository, a branch)
_OTHER = {"git": (_GIT_WORDS, 2, frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}),
                  frozenset({"-C", "--git-dir", "--work-tree"})),
          "gh": (_GH_WORDS, 2, frozenset({"-R", "--repo"}),
                 frozenset({"-R", "--repo", "--base", "-B", "--head", "--limit", "-L"}))}
# git's and gh's options that take no value and that the masker knows, for the commands old guard refusals quote:
# with the options above, the names that stay after free text in a joined argument list
_OTHER_FLAGS = {
    "git": frozenset((
        "--force -f --force-with-lease --force-if-includes --delete -d -D --tags --all --mirror --prune "
        "--set-upstream -u --no-verify --hard --soft --mixed --keep --amend -a --no-edit --quiet -q --dry-run "
        "--ff-only --no-ff --squash --rebase --abort --continue").split()),
    "gh": frozenset((
        "--squash --merge --rebase --delete-branch -d --admin --auto --disable-auto --draft --fill --web -w --yes -y "
        "--prerelease --latest --generate-notes --private --public --internal --confirm --paginate -X --method -f -F "
        "--field --raw-field --input --description --homepage --disable-wiki --disable-issues --add-readme --clone "
        "--json").split())}
_KNOWN = {"dev": DEV_OPTIONS, "devkit": DEV_OPTIONS,
          **{p: frozenset().union(o[2], o[3], _OTHER_FLAGS[p],
                                  *(v for k, v in OTHER_TEXT_OPTIONS.items() if k.split()[0] == p))
             for p, o in _OTHER.items()}}
_KNOWN_ANY = frozenset().union(*_KNOWN.values())  # eval's or a shell's words: any program's
# a shell line's words before its program, which stay: reserved words, assignments, and the programs that run another
# one, with their options (those listed here take the next word as their value) and the plain words before the
# program (timeout's duration)
_RESERVED = frozenset({"!", "{", "}", "if", "then", "elif", "else", "do", "while", "until", "fi", "done", "esac"})
_WRAPPERS = {
    "env": (frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string", "-P"}), 0),
    "sudo": (frozenset({"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt", "-C", "--close-from", "-D",
                        "--chdir", "-r", "--role", "-t", "--type", "-T", "--command-timeout", "-U", "--other-user",
                        "-R", "--chroot"}), 0),
    "timeout": (frozenset({"-s", "--signal", "-k", "--kill-after"}), 1),
    "nice": (frozenset({"-n", "--adjustment"}), 0),
    "xargs": (frozenset({"-a", "--arg-file", "-d", "--delimiter", "-E", "-I", "-J", "-L", "--max-lines", "-n",
                         "--max-args", "-P", "--max-procs", "-R", "-S", "-s", "--max-chars"}), 0),
    "exec": (frozenset({"-a"}), 0),
    "time": (frozenset({"-f", "--format", "-o", "--output"}), 0),
    "command": (frozenset(), 0), "builtin": (frozenset(), 0), "nohup": (frozenset(), 0),
    "uv run": (frozenset({"--with", "--with-editable", "--with-requirements", "--python", "-p", "--project",
                          "--directory", "--package", "--extra", "--group", "--only-group", "--env-file", "--index",
                          "--index-url", "--extra-index-url", "--default-index"}), 0),
}
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
# the long options these shells take before their -c (bash's own, and zsh's common option names): another long
# option there keeps only its dashes (`--[text not recorded: 11 characters]`), since a made-up one can name anything
_SHELL_LONG = frozenset({
    "--debug", "--debugger", "--dump-po-strings", "--dump-strings", "--help", "--init-file", "--login",
    "--noediting", "--noprofile", "--norc", "--posix", "--pretty-print", "--protected", "--rcfile", "--restricted",
    "--verbose", "--version", "--wordexp", "--interactive", "--no-rcs", "--no-globalrcs", "--emacs", "--vi",
    "--errexit", "--xtrace", "--nounset", "--pipefail", "--privileged", "--shinstdin", "--no-norcs"})
_PYTHON = re.compile(r"python(?:3(?:\.\d+)?)?")
_PYTHON_VALUED = frozenset({"-W", "-X", "--check-hash-based-pycs"})
# the programs whose name a record keeps: a word at
# command position, or a refusal's one-word quote, stays only when it is on this list; any other is recorded as its
# length, since a lowercase word there can be a client's name, a file or a password. A program run by its path keeps
# its base name, and only when the list has it. The list is fixed, so a record masks the same way on every machine (no
# PATH lookup): devkit's own programs, the read-only allowlist (readonly.py) with its checks, runners and wrappers
# (tests fail while one is missing here), the programs that run another one, and common developer tools
PROGRAMS = frozenset((
    # devkit, git and the forges
    "dev devkit git gh glab hub git-lfs "
    # shells, their builtins and reserved words that devkit's shell reader reads as programs
    "sh bash zsh dash ksh fish csh tcsh source . alias cd pushd popd dirs pwd echo printf read set unset export local "
    "declare typeset readonly wait sleep true false test [ [[ type which command builtin hash history exit return "
    "break continue shift trap ulimit umask getopts let eval exec for select case in esac function coproc "
    # the programs that run another one
    "env sudo doas timeout nice nohup xargs time caffeinate script watch parallel sem arch flock unbuffer chronic "
    # readers and text tools
    "cat head tail less more bat ls stat file wc grep egrep fgrep rg ag ack jq yq diff diff3 cmp comm patch md5 md5sum "
    "shasum sha1sum sha256sum cksum du df realpath readlink basename dirname hexdump xxd od strings column cut tr nl "
    "fold paste tree lsof whoami id uname printenv date sort uniq find sed awk gawk mawk nawk seq expr bc tee base64 "
    "col mdfind mdls plutil uptime groups locale getconf sysctl "
    # files
    "touch mkdir mkfifo truncate rm rmdir unlink shred srm ln chmod chown chgrp chflags xattr setfacl cp gcp mv gmv "
    "install ditto rsync scp sftp tar zip unzip gzip gunzip bzip2 xz zstd open "
    # interpreters, package managers and runners
    "python python3 pypy pypy3 pip pip3 pipx uv uvx poetry pipenv pdm hatch rye conda node nodejs npm npx pnpm pnpx "
    "yarn bun bunx deno tsx ts-node perl ruby gem bundle bundler rake rails irb php composer artisan lua tclsh Rscript "
    "osascript java javac mvn mvnw gradle gradlew kotlin kotlinc scala sbt go gofmt cargo rustc rustup dotnet swift "
    "swiftc clang clang++ gcc g++ cc make cmake ninja bazel just "
    # checks: tests, types, linters, formatters and scanners
    "pytest py.test tox nox mypy pyright ruff black isort flake8 pylint coverage vitest jest mocha playwright cypress "
    "tsc eslint prettier biome stylelint shellcheck shfmt markdownlint markdownlint-cli2 vale semgrep gitleaks "
    "osv-scanner trivy grype syft hadolint actionlint ng nx turbo vite next webpack rollup esbuild "
    # containers, clouds and services
    "docker docker-compose podman colima kubectl helm kind minikube terraform tofu ansible aws gcloud az fly vercel "
    "netlify psql mysql sqlite3 redis-cli mongosh "
    # the network, processes and this Mac
    "curl wget ssh ping dig nslookup openssl gpg kill pkill pgrep ps top htop tmux screen brew defaults launchctl "
    "security sw_vers xcodebuild xcrun codesign spctl hdiutil diskutil pmset pbcopy pbpaste say man "
    # editors and agents
    "code cursor vim vi nvim nano emacs claude codex").split())


def _listed(name: str) -> bool:
    """Is this program's name on devkit's list (PROGRAMS, or python with its version)?"""
    return name in PROGRAMS or bool(_PYTHON.fullmatch(name))


_PUNCT = r"[!-/:-@\[-^{-~]"  # ASCII punctuation, without _ and the backtick
_SIGN = re.compile(_PUNCT + r"{1,6}")  # a value of punctuation only, such as xargs' {}: it names nothing
_NICE = re.compile(r"-\d{1,6}")  # nice -10, xargs -0: an option that is a number
_FLAG = re.compile(r"-[A-Za-z]{1,4}|--[a-z0-9][a-z0-9-]{0,39}")  # an option's name alone, or a cluster of flags
# devkit's own `…`, alone or in punctuation ("…", $(…), <(…)): no word of yours
_FIXED = re.compile(_PUNCT + r"{0,6}…" + _PUNCT + r"{0,6}")
_SAFE = re.compile(r"[TFBSPL]-[0-9a-z]{1,24}|[DMR]\d{1,6}|\d{1,6}|-")  # an id, a number of up to 6 digits, stdin
_SAFE_TARGET = re.compile(r"\d{1,3}|-|/dev/(?:null|stdout|stderr|tty)")  # a redirection's file that names nothing
_LONG = re.compile(r"--[a-z0-9][a-z0-9-]{0,39}")  # an option's name
_SETTING = re.compile(r"DEVKIT_[A-Z0-9_]+=")
_SHORT_VALUE = re.compile(r"[A-Za-z0-9_.:-]{1,32}")  # the value of a DEVKIT_* setting that stays: one short token
_PAIR = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,39}=")
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
# a marker in a command line, kept as it is: records read again, and those sealed with the crew markers
_MARKER = re.compile(r"\[(?:[a-z]+ )?text(?: not recorded)?: \d+\+? characters\]")
_PARTIAL = re.compile(r"\[[a-z ]*(?::(?: \d*\+?(?: [a-z]*)?)?)?\Z")  # a marker cut at the end of a field
_HEREDOC = re.compile(r"<<(-?)[ \t]*\\?(['\"]?)([A-Za-z0-9_.-]+)\2")  # <<EOF, <<-'EOF', <<"EOF", <<\EOF
_REDIR = re.compile(r"\d*(?:<<<|>>|>\||>&|<&|<>|>|<)|&>>?")
_MASKED_LONG = re.compile(r"--" + _MARKER.pattern)  # a shell's long option recorded as its length (_SHELL_LONG)
_ARGV = re.compile(r"(?:" + _MARKER.pattern + r"|[^\s\[]+|\[)+")  # a marker stays whole, alone or after --why=
# what progress.open_calls adds after an unclosed call's command
_UNCLOSED = re.compile(r"(?s)(.*?)( \(no result within [0-9.]+ s\))\Z")


def _single(s: str, i: int):
    """s[i] is a single quote -> (index after the closing one, the text inside, cut)."""
    j = s.find("'", i + 1)
    return (len(s), s[i + 1:], True) if j < 0 else (j + 1, s[i + 1:j], False)


_ANSI_C = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
           "\\": "\\", "'": "'", '"': '"', "?": "?"}
_ANSI_NUM = (("x", 16, 2), ("u", 16, 4), ("U", 16, 8))


def _ansi_c(s: str, i: int):
    """s[i] is the quote of an ANSI-C $'...' word -> (index after the closing one, the text the shell passes, cut).
    Its escapes are read as bash reads them, so \\' does not end it and \\x20 or \\n is the space or line break the
    program gets."""
    out, i, n = [], i + 1, len(s)
    while i < n:
        c = s[i]
        if c == "'":
            return i + 1, "".join(out), False
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        e = s[i + 1]
        if e in _ANSI_C:
            out.append(_ANSI_C[e])
            i += 2
            continue
        num = next(((base, most) for k, base, most in _ANSI_NUM if e == k), None)
        if num or e in "01234567":  # \xHH, \uHHHH, \UHHHHHHHH, or up to three octal digits
            base, most, j = (num[0], num[1], i + 2) if num else (8, 3, i + 1)
            allowed, k = "01234567" if base == 8 else "0123456789abcdefABCDEF", j
            while k < n and k - j < most and s[k] in allowed:
                k += 1
            if k > j:
                try:
                    out.append(chr(int(s[j:k], base)))
                except (ValueError, OverflowError):
                    out.append(s[i:k])
                i = k
                continue
        if e == "c" and i + 2 < n:  # \cX: the control character of X
            out.append(chr(ord(s[i + 2]) & 0x1F))
            i += 3
            continue
        out.append(s[i:i + 2])  # an escape bash does not know stays as it is written
        i += 2
    return n, "".join(out), True


def _double(s: str, i: int):
    """s[i] is a double quote -> (index after the closing one, the text inside, cut). A command substitution inside
    is read whole, with its quotes and heredocs."""
    out, i, n = [], i + 1, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            out.append(s[i + 1] if s[i + 1] in '"\\$`' else s[i:i + 2])
            i += 2
        elif c == '"':
            return i + 1, "".join(out), False
        elif s.startswith("$(", i):
            j, cut = _subst(s, i + 2)
            out.append(s[i:j])
            if cut:
                break
            i = j
        else:
            out.append(c)
            i += 1
    return n, "".join(out), True


def _subst(s: str, i: int):
    """i is just after "$(" -> (index after its closing parenthesis, cut)."""
    depth, n, waiting = 1, len(s), []
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
        elif c == "'":
            i = _single(s, i)[0]
        elif c == '"':
            i = _double(s, i)[0]
        elif c == "\n" and waiting:
            i = _bodies(s, i + 1, waiting)[0]
            waiting = []
        elif c == "<" and not s.startswith("<<<", i) and _HEREDOC.match(s, i):
            m = _HEREDOC.match(s, i)
            waiting.append(m)
            i = m.end()
        elif c in "()":
            depth += 1 if c == "(" else -1
            i += 1
            if depth == 0:
                return i, False
        else:
            i += 1
    return n, True


def _bodies(s: str, i: int, waiting: list):
    """The bodies of the heredocs `waiting` (their operators' matches), from i, the line after the operators ->
    (index after the last one's delimiter line, [(start, end, cut)])."""
    out, n = [], len(s)
    for m in waiting:
        strip, delim, start = m.group(1) == "-", m.group(3), i
        while True:
            j = s.find("\n", i)
            line = s[i:n if j < 0 else j]
            if (line.lstrip("\t") if strip else line) == delim:
                out.append((start, max(start, i - 1), False))
                i = n if j < 0 else j + 1
                break
            if j < 0:
                out.append((start, n, True))  # no delimiter: the command was cut inside the body
                i = n
                break
            i = j + 1
    return i, out


def _word(s: str, i: int):
    """The shell word that starts at i -> (end, its text without quotes, cut)."""
    out, n, cut = [], len(s), False
    while i < n and not cut:
        c = s[i]
        if c in " \t\n;&|()<>":
            break
        if c == "[" and _MARKER.match(s, i):  # a marker stays whole, alone or after --why=
            j = _MARKER.match(s, i).end()
            out.append(s[i:j])
            i = j
        elif c == "\\":
            out.append(s[i + 1:i + 2] if s[i + 1:i + 2] != "\n" else "")
            i += 2
        elif c == "'":
            i, v, cut = _single(s, i)
            out.append(v)
        elif c == '"':
            i, v, cut = _double(s, i)
            out.append(v)
        elif s.startswith("$'", i):
            i, v, cut = _ansi_c(s, i + 1)
            out.append(v)
        elif s.startswith("$(", i):
            j, cut = _subst(s, i + 2)
            out.append(s[i:j])
            i = j
        elif c == "`":
            j = s.find("`", i + 1)
            cut = j < 0
            j = n if cut else j + 1
            out.append(s[i:j])
            i = j
        else:
            out.append(c)
            i += 1
    return i, "".join(out), cut


def _shell_tokens(s: str) -> list:
    """A shell command line -> [(kind, start, end, text, cut)]: kind word, mark (a marker), op (what ends a simple
    command), redir, target (a redirection's file), body (a heredoc's lines or a here-string) or comment."""
    toks, i, n, waiting, expect = [], 0, len(s), [], None
    while i < n:
        c = s[i]
        if c in " \t":
            i += 1
        elif c == "\n":
            toks.append(("op", i, i + 1, c, False))
            i += 1
            if waiting:
                i, bodies = _bodies(s, i, waiting)
                toks += [("body", a, b, s[a:b], cut) for a, b, cut in bodies]
                waiting = []
        elif c == "#":
            j = s.find("\n", i)
            j = n if j < 0 else j
            toks.append(("comment", i, j, s[i:j], False))
            i = j
        elif s.startswith(("&&", "||", ";;", "|&"), i):
            toks.append(("op", i, i + 2, s[i:i + 2], False))
            i += 2
        elif (m := _MARKER.match(s, i)) and (m.end() == n or s[m.end()] in " \t\n;&|()<>"):
            # a marker alone; one that starts a word ([…]/python3) is read with the word
            toks.append((expect or "mark", i, m.end(), m.group(), False))
            i, expect = m.end(), None
        elif c == "<" and not s.startswith("<<<", i) and _HEREDOC.match(s, i):
            m = _HEREDOC.match(s, i)
            waiting.append(m)
            toks.append(("redir", i, m.end(), m.group(), False))
            i = m.end()
        elif _REDIR.match(s, i):
            m = _REDIR.match(s, i)
            toks.append(("redir", i, m.end(), m.group(), False))
            i, expect = m.end(), ("body" if m.group().endswith("<<<") else "target")
        elif c in ";&|()":
            toks.append(("op", i, i + 1, c, False))
            i += 1
        else:
            j, v, cut = _word(s, i)
            toks.append((expect or "word", i, j, v, cut))
            i, expect = j, None
    return toks


def _span(ws: list, mark: str, length: int, limit: int, line: str = ""):
    """Masked words -> (start, end, marker): their length as the command passed them, joined by spaces, with + when
    the line was cut inside them. ctx, the callers' last arguments: (length, limit, line)."""
    start, end = ws[0][1], ws[-1][2]
    n = sum(len(w[3]) for w in ws) + len(ws) - 1
    cut = any(w[4] for w in ws) or bool(limit and length == limit and end >= length)
    return start, end, mark.format(n=f"{n}{'+' if cut else ''}")


def command_slots(path: str) -> list:
    """A dev command's positionals, from COMMAND_ARGS: [(dest, class, nargs)]; nargs is "" for one word."""
    out = []
    for spec in (COMMAND_ARGS.get(path) or "").split():
        dest, _, rest = spec.partition(":")
        cls = rest.rstrip("?*+.")
        out.append((dest, cls, rest[len(cls):]))
    return out


def _opt_like(v: str) -> bool:
    """Is this word an option (-x, -xvalue, --name, --name=value)? A word with a space is a value, as argparse reads
    it, unless it is --name=value."""
    if len(v) < 2 or v[0] != "-":
        return False
    bare = _MARKER.sub("", v)  # a value masked before (-m[text not recorded: …]) keeps the option an option
    if v[1] == "-":
        return bool(_LONG.fullmatch(v.partition("=")[0])) and ("=" in v or not any(c.isspace() for c in bare))
    return v[1].isalpha() and not any(c.isspace() for c in bare)


def _keeps(v: str, form: str) -> bool:
    """May this value stay where a name stays? A name is one word: a value with a space, a tab or a line break is
    free text in every form (a quoted shell word, an ANSI-C $'...' word once its escapes are read, an argument of
    dev's list, a marker glued to other text), so it is masked; so is an empty one."""
    return bool(v) and not any(c.isspace() for c in v)


def _kept(v: str) -> bool:
    """A word that stays wherever it is: a marker (a record read again) or devkit's own `…`."""
    return bool(_MARKER.fullmatch(v) or _FIXED.fullmatch(v))


_ROSTER = []  # devkit's roster ids, read once


def _roster_ids() -> frozenset:
    """The ids in devkit's own roster (config/crew.json), in lower case: its members, their aliases and its kinds of
    work. The team's changes are left out, so a record masks the same way on every machine. Unreadable: none."""
    if not _ROSTER:
        ids = set()
        try:
            data = json.loads((core.DEVKIT_HOME / "config" / "crew.json").read_text())
            for m in data.get("members") or []:
                if isinstance(m, dict):
                    aliases = m.get("aliases")
                    ids.update([m.get("id")] + (aliases if isinstance(aliases, list) else []))
            ids.update(a.get("id") for a in data.get("activities") or [] if isinstance(a, dict))
        except (OSError, ValueError, AttributeError):
            ids = set()
        _ROSTER.append(frozenset(i.lower() for i in ids if isinstance(i, str) and i))
    return _ROSTER[0]


_RACI_PAIR = re.compile(r"[RACIraci]=([A-Za-z0-9_,-]*)")


def _raci_pairs(v: str) -> bool:
    """Is this dev crew raci --set value ("R=tank,neo A=seraph") only R, A, C or I and ids of devkit's roster?"""
    parts = [p for p in re.split(r"[\s;]+", v) if p]
    ids = _roster_ids()
    return bool(parts) and all(bool(m) and all(x.lower() in ids for x in m.group(1).split(",") if x)
                               for m in map(_RACI_PAIR.fullmatch, parts))


def _raci_set_acts(words: list, i: int, acts: list, mark: str) -> int:
    """words[i] is dev crew raci's --set: its value stays when it is R/A/C/I pairs of roster ids, and so does each
    such pair after it, a value that holds spaces read again from a joined line. -> the index after them"""
    v, n = words[i][3], len(words)
    acts[i] = None
    if "=" in v:
        if not _raci_pairs(v.partition("=")[2]):
            acts[i] = ("tail", "--set=", mark)
            return i + 1
        i += 1
    elif i + 1 < n:
        w = words[i + 1]
        if not (w[0] == "mark" or _raci_pairs(w[3])):
            acts[i + 1] = ("mask", ("w", i + 1), mark)
            return i + 2
        acts[i + 1] = None
        i += 2
    else:
        return i + 1
    while i < n and words[i][0] == "word" and _raci_pairs(words[i][3]):
        acts[i] = None
        i += 1
    return i


def _opt_acts(words: list, i: int, acts: list, form: str, text, names, mark: str) -> int:
    """words[i] is an option. Its name stays; its value (the next word, whatever it looks like, or what follows = or
    a short option's letter) stays when the option names something, and is masked otherwise. -> the index after the
    option and its value"""
    v = words[i][3]
    if v.startswith("--"):
        name, eq, val = v.partition("=")
        if eq:
            if not (name in names and _keeps(val, form)):
                acts[i] = ("tail", name + "=", mark)
            return i + 1
        if (name in text or name in names) and i + 1 < len(words):
            keep = name in names and (words[i + 1][0] == "mark" or _keeps(words[i + 1][3], form))
            acts[i + 1] = None if keep else ("mask", ("w", i + 1), mark)
            return i + 2
        return i + 1
    for k in range(1, len(v)):  # -m, a short option with its value attached (-mfix), or a cluster of flags (-am)
        opt, rest = "-" + v[k], v[k + 1:]
        if opt in text or opt in names:
            if rest:  # its value, attached: kept only for the first letter's option, as a name
                if not (k == 1 and opt in names and _keeps(rest, form)):
                    acts[i] = ("tail", v[:k + 1], mark)
                return i + 1
            if i + 1 < len(words):
                keep = opt in names and (words[i + 1][0] == "mark" or _keeps(words[i + 1][3], form))
                acts[i + 1] = None if keep else ("mask", ("w", i + 1), mark)
                return i + 2
            return i + 1
        if len(v) > 4 or not v[1:].isalpha():  # a word, or the value of an option the tables do not know: only -x
            acts[i] = ("tail", v[:2], mark) if len(v) > 2 else acts[i]
            return i + 1
    return i + 1


def _pos_act(w, cls: str, form: str, mark: str, group):
    """A positional by its class: None when it stays, else how it is masked."""
    v = w[3]
    if w[0] == "mark" or (cls == "name" and _keeps(v, form)) or (cls in ("select", "pair") and _SAFE.fullmatch(v)) \
            or (cls == "roster" and v.lower() in _roster_ids()) or (cls == "target" and PEER_TARGET.fullmatch(v)):
        return None
    m = _PAIR.match(v) if cls == "pair" else None
    if m:  # a value stays only from its key's fixed set
        val = v[m.end():]
        return None if val in PAIR_VALUES.get(m.group()[:-1], ()) or _kept(val) else ("tail", m.group(), mark)
    return ("mask", group, mark)


def _dev_acts(words: list, i: int, acts: list, form: str) -> None:
    """dev's arguments from words[i]: its command path, then its options and positionals, each by its class."""
    path, slots, pos, flat, rest, n = "", [], 0, False, False, len(words)
    while i < n:
        w = words[i]
        v, plain = w[3], w[0] == "word" and not flat
        mark = COMMAND_MARK.get(path, TEXT_MARK)
        if plain and v == "--":
            flat, acts[i] = True, None
            i += 1
        elif plain and path == "crew raci" and v.partition("=")[0] == "--set":
            i = _raci_set_acts(words, i, acts, mark)
        elif plain and _opt_like(v):
            if rest:  # a REMAINDER holds another program's words: no value of theirs names something here
                acts[i] = None
                i = _opt_acts(words, i, acts, form, frozenset(), frozenset(), mark)
                pos = max(pos, len(slots) - 1)  # argparse gives everything after an option to the REMAINDER
            else:
                acts[i] = None
                own = NAME_TEXT.get(path, frozenset())
                i = _opt_acts(words, i, acts, form, TEXT_OPTIONS | own,
                              NAME_OPTIONS - NAME_FLAGS.get(path, frozenset()) - own, mark)
        elif plain and not rest and pos == 0 and f"{path} {v}".strip() in COMMAND_ARGS:
            acts[i] = "path"
            if not path and v in _OTHER:  # dev git …, dev gh …: git's and gh's own arguments
                _other_acts(v, words, i + 1, acts, form)
                return
            path = f"{path} {v}".strip()
            slots = command_slots(path)
            rest = any(nargs == "..." for _, _, nargs in slots)
            i += 1
        else:
            last = len(slots) - 1
            repeats = last >= 0 and slots[last][2] in ("*", "+", "...")
            cls = slots[pos][1] if pos <= last else (slots[last][1] if repeats else "select")
            group = ("slot", last) if repeats and pos >= last else ("w", i)
            acts[i] = _pos_act(w, cls, form, mark, group)
            pos += 1
            i += 1


def _other_acts(prog: str, words: list, i: int, acts: list, form: str) -> None:
    """git's or gh's arguments from words[i]: options before the subcommand, the subcommand's known words, then
    options and positionals. A positional stays only as an id or a number."""
    known, depth, before, names = _OTHER[prog]
    path, n = [], len(words)
    while i < n and len(path) < depth:
        w = words[i]
        if w[0] == "word" and not path and _opt_like(w[3]):
            acts[i] = None
            i = _opt_acts(words, i, acts, form, before - names, names, TEXT_MARK)
        elif w[0] == "word" and w[3] in known:
            acts[i] = "path"
            path.append(w[3])
            i += 1
        else:
            break
    text, flat = OTHER_TEXT_OPTIONS.get(" ".join([prog] + path), frozenset()), False
    while i < n:
        w = words[i]
        v, plain = w[3], w[0] == "word" and not flat
        if plain and v == "--":
            flat, acts[i] = True, None
            i += 1
        elif plain and _opt_like(v):
            acts[i] = None
            i = _opt_acts(words, i, acts, form, text, names - text, TEXT_MARK)
        else:
            acts[i] = None if w[0] == "mark" or _SAFE.fullmatch(v) else ("mask", ("other", prog), TEXT_MARK)
            i += 1


def _names_nothing(v: str) -> bool:
    """A value that stays where only a value that names nothing may: a marker or devkit's `…`, an id, a number of up
    to six digits, or punctuation only (xargs' {})."""
    return bool(_kept(v) or _SAFE.fullmatch(v) or _SIGN.fullmatch(v))


def _value_act(w, j: int):
    """A wrapper's option value or duration (sudo -u root, timeout 60): it stays only when it names nothing."""
    return None if w[0] == "mark" or _names_nothing(w[3]) else ("mask", ("w", j), TEXT_MARK)


def _flag_act(w, j: int):
    """An option of python or a shell before what it runs (python3 -I, bash -euc, bash --rcfile=…): its name stays,
    and a value attached to it only when it names nothing."""
    v = w[3]
    if w[0] == "mark" or _FLAG.fullmatch(v) or _NICE.fullmatch(v):
        return None
    head = v.partition("=")[0] + "=" if v.startswith("--") and "=" in v else v[:2]
    if (head[1:2].isalpha() or _LONG.fullmatch(head[:-1])) and len(v) > len(head):
        return None if _names_nothing(v[len(head):]) else ("tail", head, TEXT_MARK)
    return ("mask", ("w", j), TEXT_MARK)


def _bare(w, line: str) -> bool:
    """Is this word written as it reads, with no quote or escape? A word of an argument list always is."""
    return w[1] < 0 or line[w[1]:w[2]] == w[3]


def _folder(w, extra: list) -> None:
    """A program or a wrapper run by its path (/opt/x/bin/dev): its folder becomes its length
    in `extra`, and its base name stays."""
    folder = w[3][:w[3].rfind("/")]
    if w[1] >= 0 and folder and not _kept(folder):
        extra.append(_span([(w[0], w[1], w[1] + len(folder), folder, False)], TEXT_MARK, 0, 0))


def _assign_act(v: str):
    """An assignment: its name stays; its value only as one short DEVKIT_* word."""
    head = _ASSIGN.match(v).group()
    value = v[len(head):]
    return None if not value or (_SETTING.match(v) and _SHORT_VALUE.fullmatch(value)) else ("tail", head, TEXT_MARK)


def _program(words: list, acts=None, line: str = "", names=None):
    """A simple command's program, at command position: after the leading reserved words, the assignments, and the
    programs that run another one with their options, the options' values and timeout's duration. Anywhere else, dev
    is a word like any other, and so is a program run by a quoted or escaped path. -> (the command word's index,
    len(words) when there is none; its kind: dev, git, gh, devkit (python -m devkit), sh (a shell's -c), eval, or None
    for another program; the index of python's -m or of the shell's -c text, else -1). `acts` gets what becomes of
    the words before the program, and `names` the index of each program that runs another one."""
    n, j, lead = len(words), 0, True
    acts = [None] * n if acts is None else acts

    def plain(k):
        return k < n and words[k][0] == "word"

    while plain(j):
        v = words[j][3]
        if lead and v in _RESERVED:
            acts[j], j = None, j + 1
            continue
        lead = False
        if _ASSIGN.match(v):
            acts[j], j = _assign_act(v), j + 1
            continue
        base = v.rsplit("/", 1)[-1]
        wrap = "uv run" if base == "uv" and plain(j + 1) and words[j + 1][3] == "run" else base
        if wrap not in _WRAPPERS or (base != v and not _bare(words[j], line)):
            break
        valued, before = _WRAPPERS[wrap]
        for k in range(j, j + len(wrap.split())):
            acts[k] = None
        if names is not None:
            names.append(j)
        j += len(wrap.split())
        while plain(j) and len(words[j][3]) > 1 and words[j][3][0] == "-":  # its options: their names stay
            o = words[j][3]
            acts[j], j = None, j + 1
            if o == "--":
                break
            if o.startswith("--"):
                name, eq, val = o.partition("=")
                if not _LONG.fullmatch(name):
                    acts[j - 1] = ("mask", ("w", j - 1), TEXT_MARK)
                elif eq:
                    acts[j - 1] = None if _names_nothing(val) else ("tail", name + "=", TEXT_MARK)
                elif name in valued and j < n:
                    acts[j], j = _value_act(words[j], j), j + 1
            elif o[:2] in valued:  # -u root, -uroot, -I{}
                if len(o) > 2:
                    acts[j - 1] = None if _names_nothing(o[2:]) else ("tail", o[:2], TEXT_MARK)
                elif j < n:
                    acts[j], j = _value_act(words[j], j), j + 1
            elif _NICE.fullmatch(o):  # nice -10, xargs -0
                pass
            elif len(o) <= 4 and o[1:].isalpha():  # flags (-Ei), the last one perhaps taking the next word
                if "-" + o[-1] in valued and j < n:
                    acts[j], j = _value_act(words[j], j), j + 1
            else:  # a word, or a value the tables do not know: only -x stays
                acts[j - 1] = ("tail", o[:2], TEXT_MARK)
        for _ in range(before):  # timeout's duration
            if j < n:
                acts[j], j = _value_act(words[j], j), j + 1
    if j >= n or not plain(j):
        return j, None, -1  # no command word, or one masked before
    v = words[j][3]
    base, m = v.rsplit("/", 1)[-1], j + 1
    if base != v and not _bare(words[j], line):
        return j, None, -1  # a program run by a quoted or escaped path: the masker cannot tell which
    if base in ("dev", "git", "gh"):
        return j, base, -1
    if v == "eval":
        return j, "eval", -1
    if _PYTHON.fullmatch(base):  # python -m devkit, after python's own options
        while plain(m) and len(words[m][3]) > 1 and words[m][3][0] == "-" and words[m][3] not in ("-c", "-m", "--"):
            m += 2 if words[m][3] in _PYTHON_VALUED else 1
        found = plain(m + 1) and words[m][3] == "-m" and words[m + 1][3] == "devkit"
        return (j, "devkit", m) if found else (j, None, -1)
    if base in _SHELLS:
        dash_c = False
        while plain(m) and (_opt_like(words[m][3]) or _MASKED_LONG.fullmatch(words[m][3])):
            dash_c = dash_c or (not words[m][3].startswith("--") and "c" in words[m][3][1:])
            m += 2 if words[m][3] in ("-o", "-O") else 1
        if dash_c:
            return j, "sh", m if m < n else -1
    return j, None, -1


def _nested(w, depth: int, line: str, extra: list):
    """A nested shell's text (sh -c '…', eval '…'): masked as a shell line of its own inside the same quotes, or as
    its length when it is not one quoted string. -> None when it stays (masked in place, in `extra`), else a mask"""
    if w[0] == "mark":
        return None
    raw = line[w[1]:w[2]] if w[1] >= 0 else ""
    if depth < 3 and not w[4] and len(raw) >= 2:
        inner = _mask_line(w[3], "shell", depth + 1)
        if raw[0] == raw[-1] == "'" and raw.count("'") == 2 and "'" not in inner:
            rep = "'" + inner + "'"
        elif raw[0] == '"' and _double(raw, 0) == (len(raw), w[3], False):
            rep = '"' + re.sub(r'([\\"$`])', r"\\\1", inner) + '"'
        else:
            rep = None
        if rep is not None:
            if rep != raw:
                extra.append((w[1], w[2], rep))
            return None
    return ("mask", ("w", w[1]), TEXT_MARK)


def _simple_acts(words: list, form: str, depth: int, line: str, extra: list) -> list:
    """One simple command's words -> what becomes of each: None or "path" (the program and its subcommands) stays,
    ("mask", group, marker) is masked with the masked words next to it in the same group, ("tail", head, marker)
    keeps `head` and masks the rest of the word, ("head", tail, marker) keeps `tail` and masks what comes before it.
    Before the program, the reserved words, an assignment's name and the wrappers with their option names stay. A
    program the masker does not know keeps its name only when devkit's list has it, written as it reads, and each of
    its arguments becomes its length. A program or a wrapper run by its path keeps its base name, not its folder.
    `extra` gets the spans of a nested shell's text and of those folders."""
    n = len(words)
    acts = [("mask", ("w", j), TEXT_MARK) for j in range(n)]
    names = []
    j, kind, s = _program(words, acts, line, names)
    for k in names + ([j] if kind else []):
        if "/" in words[k][3]:
            _folder(words[k], extra)
    if j >= n:
        return acts
    if kind is None:  # its name stays only from devkit's list, its base name when it runs by its path
        v = words[j][3]
        base = v.rsplit("/", 1)[-1]
        if words[j][0] == "mark":
            acts[j] = None
        elif _bare(words[j], line) and _listed(base):  # 2a: a name written with quotes stays in neither case
            acts[j] = None if base == v else ("head", "/" + base, TEXT_MARK)
        return acts
    acts[j] = "path"
    if kind == "devkit":  # python and its own option names stay as they are before -m devkit
        acts[j] = None
        for k in range(j + 1, s):
            acts[k] = _flag_act(words[k], k) if words[k][3].startswith("-") else _value_act(words[k], k)
        acts[s] = acts[s + 1] = "path"
        _dev_acts(words, s + 2, acts, form)
    elif kind == "dev":
        _dev_acts(words, j + 1, acts, form)
    elif kind in _OTHER:
        _other_acts(kind, words, j + 1, acts, form)
    elif kind == "sh":
        for m in range(j + 1, s if s >= 0 else n):
            v = words[m][3]
            if _MASKED_LONG.fullmatch(v):  # recorded before
                acts[m] = None
            elif not _opt_like(v):
                acts[m] = ("mask", ("w", m), TEXT_MARK)
            elif v.startswith("--") and v.partition("=")[0] not in _SHELL_LONG:
                acts[m] = ("tail", "--", TEXT_MARK)
            else:
                acts[m] = _flag_act(words[m], m)
        # its -c is part of the program, as a subcommand is: the options before it keep their decision in a joined
        # quote (_strict), so the shell reads the same when the record is read again
        cs = [m for m in range(j + 1, s if s >= 0 else n) if acts[m] is None and not words[m][3].startswith("--")
              and "c" in words[m][3][1:]]
        if cs:
            acts[cs[-1]] = "path"
        if s >= 0:
            acts[s] = _nested(words[s], depth, line, extra)
    elif n - j == 2:  # eval and its text in one word
        acts[j + 1] = _nested(words[j + 1], depth, line, extra)
    elif n - j > 2:  # eval's words are a command line of their own
        acts[j + 1:] = _simple_acts(words[j + 1:], form, depth + 1, line, extra)
    return acts


def _masked_part(w, a) -> str:
    """The part of a word that a ("mask" | "tail" | "head", …) decision masks."""
    return w[3] if a[0] == "mask" else w[3][len(a[1]):] if a[0] == "tail" else w[3][:len(w[3]) - len(a[1])]


def _masks_text(words: list, acts: list) -> bool:
    """Do these decisions mask a word of the line that is no marker yet?"""
    for w, a in zip(words, acts):
        if isinstance(a, tuple) and w[1] >= 0 and w[0] == "word":
            v = _masked_part(w, a)
            if v and not _kept(v):
                return True
    return False


def _strict(words: list, acts: list, nested=frozenset(), line: str = "") -> list:
    """A joined argument list that holds free text: a word cannot tell where an argument ends. Free text starts after
    the program's last subcommand, so up to it each word keeps its decision (the program reads the same again), and
    after it only the option names the masker knows for the program (dev's parser; git's and gh's in the tables),
    markers, ids and numbers of up to six digits stay. nested: where a nested shell's text starts, masked in place."""
    if "path" not in acts:
        return acts
    known = _KNOWN.get(_program(words, None, line)[1], _KNOWN_ANY)
    last = max(k for k, a in enumerate(acts) if a == "path")
    mark = next((a[2] for a in reversed(acts) if isinstance(a, tuple)), TEXT_MARK)
    out = []
    for k, (w, a) in enumerate(zip(words, acts)):
        v = w[3]
        if k <= last:
            out.append(a)
        elif w[0] == "mark" or _kept(v) or _SAFE.fullmatch(v) or (w[1] >= 0 and w[1] in nested):
            out.append(None)
        elif _opt_like(v):  # a known name stays, and a cluster of known flags (-am); a value after = or a letter not
            if v.startswith("--"):
                name, eq, _ = v.partition("=")
                head = (name + "=" if eq else v) if name in known else None
            elif len(v) <= 4 and v[1:].isalpha() and all("-" + c in known for c in v[1:]):
                head = v
            else:
                head = v[:2] if v[:2] in known else None
            out.append(("mask", ("strict",), mark) if head is None else None if head == v else ("tail", head, mark))
        else:
            out.append(("mask", ("strict",), mark))
    return out


def _spans_of(words: list, acts: list, ctx) -> list:
    """The spans that replace the masked words: next to each other in the same group, words make one marker. A
    marker and devkit's own `…` stay."""
    spans, run, key, mark = [], [], None, TEXT_MARK
    for w, a in zip(words, acts):
        masked = isinstance(a, tuple) and w[1] >= 0 and w[0] == "word"
        if masked and a[0] == "mask" and not _kept(w[3]):
            if run and (a[1] != key or ctx[2][run[-1][2]:w[1]].strip()):  # only spaces between: no redirection
                spans.append(_span(run, mark, *ctx))
                run = []
            run.append(w)
            key, mark = a[1], a[2]
            continue
        if run:
            spans.append(_span(run, mark, *ctx))
            run = []
        value = _masked_part(w, a) if masked and a[0] in ("tail", "head") else ""
        if value and not _kept(value):
            raw = ctx[2][w[1]:w[2]]
            if a[0] == "tail":
                at = w[1] + len(a[1]) if raw.startswith(a[1]) else w[1]
                spans.append(_span([(w[0], at, w[2], value, w[4])], a[2], *ctx))
            elif raw.endswith(a[1]):  # the program's base name stays after its masked folder
                spans.append(_span([(w[0], w[1], w[2] - len(a[1]), value, w[4])], a[2], *ctx))
            else:  # quoted: the whole word
                spans.append(_span([w], a[2], *ctx))
    if run:
        spans.append(_span(run, mark, *ctx))
    return spans


def _free_span(tok, length: int, limit: int, line: str = ""):
    """A heredoc body, a here-string or a comment: free text whatever the command. -> its span, or None"""
    kind, start, end, text, cut = tok
    if kind == "comment":
        start += len(text) - len(text[1:].lstrip(" \t"))
        text = text[1:].lstrip(" \t")
    if not text or _kept(text):
        return None
    return _span([(kind, start, end, text, cut)], TEXT_MARK, length, limit)


def _mask_line(s: str, form: str, depth: int = 0, prefix=(), limit: int = 0, elems=None) -> str:
    """mask_command's work, which may raise. elems: dev's argument list, which `s` joins by spaces. form "quote" reads
    a shell line whose words may be joined arguments (what a guard refusal quotes): each simple command that holds
    free text keeps only what _strict keeps."""
    ctx, extra = (len(s), limit, s), []
    if form == "argv":
        words = [("word", -1, -1, str(p), False) for p in prefix]
        if elems is None:
            words += [("mark" if _MARKER.fullmatch(m.group()) else "word", m.start(), m.end(), m.group(), False)
                      for m in _ARGV.finditer(s)]
        else:
            at = 0
            for e in elems:
                words.append(("mark" if _MARKER.fullmatch(e) else "word", at, at + len(e), e, False))
                at += len(e) + 1
        acts = _simple_acts(words, "argv" if elems is None else "list", depth, s, extra)
        if elems is None and _masks_text(words, acts):
            acts = _strict(words, acts, {e[0] for e in extra}, s)
        spans = _spans_of(words, acts, ctx)
    else:
        spans, seg = [], []
        for tok in _shell_tokens(s) + [("op", len(s), len(s), "", False)]:
            if tok[0] == "op":
                words = [t for t in seg if t[0] in ("word", "mark")]
                acts = _simple_acts(words, "shell", depth, s, extra)
                if form == "quote" and _masks_text(words, acts):
                    acts = _strict(words, acts, {e[0] for e in extra}, s)
                spans += _spans_of(words, acts, ctx)
                seg = []
            elif tok[0] in ("body", "comment"):
                spans += [sp for sp in [_free_span(tok, *ctx)] if sp]
            elif tok[0] == "target":
                if not (_SAFE_TARGET.fullmatch(tok[3]) or _kept(tok[3])):
                    spans.append(_span([tok], TEXT_MARK, *ctx))
            else:
                seg.append(tok)
    out, last = s, len(s) + 1
    for start, end, marker in sorted(spans + extra, reverse=True):
        if not 0 <= start <= end <= last:  # spans that overlap: mask_command keeps the line's length only
            raise ValueError(f"masked spans overlap at {start}")
        if not _kept(s[start:end]):
            out = out[:start] + marker + out[end:]
            last = start
    return out


def mask_command(line, form: str = "shell", prefix=(), limit: int = 0) -> str:
    """A command line as a record may keep it: the words known to be safe stay (the program and its subcommands,
    option names, the values that name something, ids and numbers, operators and redirections) and every other word
    becomes TEXT_MARK (or its command's COMMAND_MARK) with its length, words of one text together; so does a heredoc
    body, a here-string and a comment. The program is the word at command position: a line whose program is not dev,
    git, gh, or one that runs them, keeps its operators, the program's name when devkit's list has it (PROGRAMS), and
    each other word's length. A program run by its path keeps its base name, not its folder.

    form: "shell" for the text an agent ran (quotes, operators, redirections, heredocs); "quote" for what a guard
    refusal quotes, a shell line whose words may be joined arguments; "argv" for dev's arguments, `prefix` holding the
    words before them ("dev", "crew"): a list from cli.py, one argument each, or the text cli.py joined by spaces
    before 26.11.5. In joined words, after a free text only the known option names, markers, ids and numbers stay.
    limit: where the caller cut the line, so a text that reaches it is recorded as N+ characters. A marker already
    there stays as it is, so a record reads back as it was stored. Never raises: a line it cannot read is kept as its
    length only."""
    elems = [str(x) for x in line] if isinstance(line, (list, tuple)) else None
    s = " ".join(elems) if elems is not None else line if isinstance(line, str) else str(line if line is not None else "")
    if not s:
        return s
    try:
        if elems is not None:  # a list holds whole arguments: no length in it was cut
            return _mask_line(s, "argv", 0, prefix, 0, elems)
        return _mask_line(s, form, 0, prefix, limit)
    except Exception:
        return TEXT_MARK.format(n=len(s))


def _fit(s: str, n: int) -> str:
    """s cut to n characters, without a marker or a word cut in two at the end: neither would read back as it was
    (`git` cut to `gi` is no program the masker knows)."""
    if len(s) <= n:
        return s
    cut = s[:n]
    i = cut.rfind("[")
    if i >= 0 and _PARTIAL.match(cut, i):
        return cut[:i].rstrip()
    if s[n].isspace() or cut[-1].isspace():
        return cut
    words = list(_ARGV.finditer(cut))  # a word, a marker counting as one
    return cut[:words[-1].start()].rstrip() if words else cut


# guard records whose target is no command line: a branch, PR or tag; a rule's name; a tool's name
_NOT_COMMAND = frozenset({"guard.irreversible", "guard.failopen", "guard.off"})
# the tools whose refused call the hooks record by the file's path
_PATH_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit", "Read"})
# guard.irreversible records whose detail is the guard's own error message, not the command's words
_GATE_ERRORS = frozenset({"gate-error", "record-error"})
_TICKS = re.compile(r"`+")
_ITEM = re.compile(r"((?:[TFBSPL]-[0-9a-z]{1,24}|[DMR]\d{1,6}): [a-z][a-z-]{0,23})(?=\s|\Z)")  # `T-…: done`


def _quotes(s: str) -> list:
    """A refusal's text -> its parts in order: ("prose", text) and ("quote", fence, text, closed). A quote is a
    Markdown code span: a run of backticks opens it and the next run of the same length closes it, so a fenced value
    in an old guard refusal reads as one quote whatever backticks it holds. A run that nothing closes quotes the
    rest of the text: a field cut inside a quote."""
    parts, i, n = [], 0, len(s)
    while i < n and (m := _TICKS.search(s, i)):
        if m.start() > i:
            parts.append(("prose", s[i:m.start()]))
        close = re.compile(f"(?<!`){m.group()}(?!`)").search(s, m.end())
        end = close.start() if close else n
        parts.append(("quote", m.group(), s[m.end():end], bool(close)))
        i = close.end() if close else n
    if i < n:
        parts.append(("prose", s[i:]))
    return parts


def _free(body: str) -> bool:
    """Is this quote free text: is the program of its first command (its first word, after reserved words,
    assignments and the programs that run another one) a word that devkit's list does not have, as it reads?"""
    words = []
    for tok in _shell_tokens(body):
        if tok[0] == "op" and words:
            break
        if tok[0] in ("word", "mark"):
            words.append(tok)
    j, kind, _ = _program(words, None, body)
    if j >= len(words) or words[j][0] != "word" or kind:
        return False
    return not (_bare(words[j], body) and _listed(words[j][3].rsplit("/", 1)[-1]))


def _quoted(body: str) -> str:
    """What a guard refusal quotes in backticks, as its record keeps it: only punctuation and devkit's `…` stay;
    a quote whose first word is not a program on devkit's list is free text, recorded as its length; anything
    else is masked as a shell line, so its operators stay, a one-word quote stays only as a program on the list, and
    once it holds free text only known option names stay. It reads only the quote, so a stored detail reads back as
    it was stored."""
    if re.fullmatch(f"(?:{_PUNCT}|…){{1,12}}", body):
        return body
    try:
        if _free(body):
            return TEXT_MARK.format(n=len(body))
    except Exception:
        return TEXT_MARK.format(n=len(body))
    return mask_command(body, "quote")


def _refusal(s: str) -> str:
    """A guard refusal as its record keeps it: each quote masked (_quoted), and the prose between two quotes that
    touches both, which a value with a backtick of its own left behind when a refusal quoted it in single backticks
    (as in `a`b`c`, records written before 26.11.5), recorded as its length. The rest of the prose stays."""
    parts, out = _quotes(s), []
    for k, p in enumerate(parts):
        if p[0] == "quote":
            out.append(p[1] + _quoted(p[2]) + (p[1] if p[3] else ""))
        elif 0 < k < len(parts) - 1 and not (p[1][0].isspace() or p[1][-1].isspace() or _kept(p[1])
                                             or re.fullmatch(r"[^\w\s`]+", p[1])):
            out.append(TEXT_MARK.format(n=len(p[1])))  # between two quotes and touching both
        else:
            out.append(p[1])
    return "".join(out)


def _command_field(action: str, name: str, about=None):
    """(form, prefix, limit) when the `name` field of an `action` record holds a command line, else None. about: the
    record's tool and irreversible action class, which tell a refused file's path and an unparsed command."""
    about = about if isinstance(about, dict) else {}
    head, _, rest = action.partition(".")
    if name == "target":
        if head == "dev" and rest in COMMAND_ARGS and rest:  # dev.policy's and dev.config.set's targets are names
            return "argv", ("dev", rest), 300  # cli.py: dev's arguments
        if head == "job":
            return "argv", ("dev",), 0  # jobs.py: the job's dev arguments
        if head == "guard" and rest and action not in _NOT_COMMAND and str(about.get("tool") or "") not in _PATH_TOOLS:
            return "shell", (), 200  # the records of the former guard: the call refused, asked or let through
    elif action == "guard.irreversible" and about.get("rule") not in _GATE_ERRORS:
        # the former gate's words of the irreversible command, cut at 200; the shell text when it could not parse it
        return ("shell", (), 200) if about.get("irreversible") == "unparsed" else ("argv", (), 200)
    elif action == "calls.unclosed":
        return "shell", (), 200  # progress.open_calls: the call, cut at 200, and how long it waited
    return None


def _command(text, rx) -> str:
    """evidence.command as a record keeps it."""
    return _fit(_clean(mask_command(str(text)), rx), VALUE_CUT)


def _item(s: str) -> str:
    """A handoff commit's message as its record keeps it: the item's id and the operation (`T-…: done`), and the rest
    only as its length; a message without them only as its length (titles and reasons are free text)."""
    if not s or _MARKER.fullmatch(s):
        return s
    m = _ITEM.match(s)
    keep, rest = (m.group(1), s[m.end():].strip()) if m else ("", s)
    if rest and not _MARKER.fullmatch(rest):
        rest = TEXT_MARK.format(n=len(rest))
    return f"{keep} {rest}".strip()


def _length(s: str) -> str:
    """Free text that names something only people know (a run's goal, a step's name) as its length; a marker stays."""
    return s if not s or _kept(s) else TEXT_MARK.format(n=len(s))


def _step(s: str) -> str:
    """A record's step: its number in the run's plan stays; a name (what records held before, or a caller passed)
    becomes its length."""
    return s if s.isdigit() else _length(s)


_RUN_REF = re.compile(r"r-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _run(s: str) -> str:
    """A record's run: its id (r-<uuid>, as crew message refs name it) stays; a goal (what records held before, or a
    caller passed) becomes its length."""
    return s if _RUN_REF.fullmatch(s) else _length(s)


# the actions whose target is a run's goal (run.*, progress.replace) or a step's name (plan.*), and those whose detail
# quotes one in «…» or “…”: free text, kept as its length
_NAMED_TARGET = re.compile(r"(?:plan|run)\.[a-z-]+|progress\.replace")
_NAMED_DETAIL = frozenset({"session.adopt", "crew.confirm", "crew.scope-handed"})
# a crew launch's records: the detail is the launch's description (free text, `Tank: the API`), and the target falls
# back to it when the launch names no member; devkit's own note stays
_LAUNCH_ACTIONS = frozenset({"crew.start", "crew.bind", "crew.resume", "crew.queued"})
_LAUNCH_LABEL = frozenset({"crew.start", "crew.bind", "crew.resume"})
_LAUNCH_NOTES = frozenset({"waits for its launch"})
_NAMED_QUOTE = re.compile(r"«([^«»]*)»|“([^“”]*)”")


def _named(s: str) -> str:
    """A detail with each goal or step name it quotes as its length."""
    return _NAMED_QUOTE.sub(lambda m: m.group()[0] + _length(m.group(1) if m.group(1) is not None else m.group(2))
                            + m.group()[-1], s)


def _field(action: str, name: str, text, rx, about=None) -> str:
    """A record's target or detail as it may be stored: no message text, no approval line, no free text of a command
    line, no secret, no home folder. A text already replaced by its marker stays as it is."""
    about = about if isinstance(about, dict) else {}
    form = _command_field(action, name, about)
    if isinstance(text, (list, tuple)) and form:  # cli.py: dev's arguments, one word each
        return _fit(_clean(mask_command(list(text), *form), rx), _CUT[name])
    s = " ".join(map(str, text)) if isinstance(text, (list, tuple)) else text if isinstance(text, str) else \
        str(text if text is not None else "")
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
    if name == "detail" and action == "crew.delegate":  # what the member should do (dispatch.delegate, cut at 300)
        return s if not s or _MARKER.fullmatch(s) else TEXT_MARK.format(n=f"{len(s)}{'+' if len(s) >= 300 else ''}")
    if name == "detail" and action.startswith("handoff."):  # work._commit: the commit's message
        return _clean(_item(s), rx)
    if name == "target" and _NAMED_TARGET.fullmatch(action):
        return _length(s)
    if name == "detail" and action in _LAUNCH_LABEL:
        return s if s in _LAUNCH_NOTES else _length(s)
    if name == "target" and action in _LAUNCH_ACTIONS and any(c.isspace() for c in s) and not _member(s)[1]:
        return _length(s)  # a roster name (The Keymaker), a subagent type and a lane id stay; a description does not
    if name == "detail" and action in _NAMED_DETAIL:
        return _clean(_named(s), rx)
    if name == "detail" and action.partition(".")[0] == "guard" and not form:
        # the refusal the agent reads quotes the command's words in backticks: each quoted command, masked
        return _fit(_clean(_refusal(s), rx), _CUT[name])
    if form and not (name == "target" and about.get("tool") and s == about.get("tool")):  # a tool's own name stays
        m = _UNCLOSED.match(s) if action == "calls.unclosed" else None
        s = mask_command(m.group(1), *form) + m.group(2) if m else mask_command(s, *form)
        return _fit(_clean(s, rx), _CUT[name])
    return _clean(s, rx)


# --------------------------------------------------------------------------- settings

LEVELS = core.AUDIT_LEVELS


def policy_file() -> Path:
    return core.HANDOFF / "policy" / "audit.json"


def settings() -> dict:
    """The effective audit settings: this machine's config over the defaults, raised by the team policy's floor.
    Every field of the policy only records more or keeps longer: a policy asking for less changes nothing."""
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


# --------------------------------------------------------------------------- the schema and the registry

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
    "branch": ("data", "notice"),  # branch.pruned: a merged branch deleted (remote, local, its worktree) and how merged
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
    "watch": ("job", "info"),  # watch.wake: a `dev watch --wait` printed what arrived
    "state": ("lifecycle", "warning"), "install": ("lifecycle", "notice"),
    # dev ext: a change to the plugins, skills and MCP servers that run (supply chain), and drift found
    "ext": ("security", "notice"), "ext.drift": ("security", "warning"),
    # an extension recorded or a change accepted (shown until the developer acknowledges it), the acknowledgement
    "ext.accept": ("security", "warning"), "ext.ack": ("security", "notice"),
    "caller": ("security", "warning"), "caller.unverified": ("security", "notice"),
    "codex.thread-shared": ("security", "notice"),  # a member's command carries another caller's Codex thread
    "calls": ("lifecycle", "notice"), "progress": ("decision", "notice"), "session": ("lifecycle", "info"),
    "run": ("lifecycle", "notice"),  # a run orphaned by its ended session, or taken back by its resumed one
    "scope.write": ("security", "notice"),  # a review or read-only lane's shell call wrote files; a caller sure
    #                                         of it passes severity="warning"
    # sessions on one Mac (peers.py): a note, question or answer sent (never its text), a refusal, a claim taken,
    # renewed, queued or released, a claim broken with the developer's grant, an escalation and its settlement, and
    # the hourly sweep
    "peer": ("lifecycle", "info"), "peer.refused": ("refusal", "notice"), "peer.claim": ("decision", "info"),
    "peer.release": ("decision", "info"), "peer.break": ("security", "warning"),
    "peer.escalate": ("decision", "notice"), "peer.settle": ("decision", "notice"), "peer.sweep": ("job", "info"),
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
    "match": "evidence.match", "asked": "evidence.asked", "label": "ref.label", "labels": "ref.labels",
    "flag": "evidence.flag", "paths": "evidence.file", "certainty": "evidence.check",
    "id": "ref.id", "by": "ref.by", "was": "ref.was", "type": "ref.type", "result": "ref.result",
    "answer": "ref.answer", "asked_by": "ref.asked_by", "irreversible": "ref.irreversible", "mode": "ref.mode",
    "overrides": "ref.overrides", "routed": "ref.routed", "session": "ref.session",
    "since": "ref.since",  # crew.evidence of a message typed mid-turn: the running turn's start (its window opens there)
    "refusal_id": "ref.refusal_id",  # an old guard refusal's stable id, or hooks:<rule> for the hook's own
    # the former irreversible-action gate's records: tier 1 or 2, setting audit or enforce, the grant's id, its
    # source (the developer's answer, a step's allowance or a standing approval) and the owner/repo acted on
    "tier": "ref.tier", "setting": "ref.setting", "grant": "ref.grant", "source": "ref.source", "repo": "ref.repo",
    "operation": "ref.operation", "mode_source": "ref.mode_source", "policy_digest": "ref.policy_digest",
    "origin_repo": "ref.origin_repo", "account": "ref.account", "principal_kind": "ref.principal_kind",
    # the decision's options, read by build to keep an answer that names one; crew.decision and crew.decided keep
    # them as ref.options, crew.evidence never stores them
    "options": "",
    "layer": "ref.layer",  # dev ext: the layer a change or drift is in (team, user, project, retired)
    # peers.py: the claimed resource, the sessions a message or an escalation went to (their keys), and its kind
    "resource": "ref.resource", "recipients": "ref.recipients", "kind": "ref.kind",
    # identity.py's records of a call's caller: the agent of the session, the command's parents up to its agent, and
    # the lane variable and Codex thread it carried
    "host_agent": "ref.agent", "ancestors": "ref.ancestors", "lane_var": "ref.lane_var", "thread": "ref.thread",
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
# characters a record keeps of each ref and evidence value: a decision's options and labels too, so a value this long
# may have been cut, and progress refuses a decision whose labels would not fit
VALUE_CUT = 300
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
    return _clean(str(v), extra)[:VALUE_CUT]


def _place(rec: dict, key: str, v, extra) -> None:
    """Put a caller's keyword where EXTRAS says; `evidence` may be a dict of evidence keys. Others are dropped."""
    if key == "evidence" and isinstance(v, dict):
        for k, x in v.items():
            if k in EVIDENCE_KEYS and x not in (None, ""):
                rec.setdefault("evidence", {})[k] = _command(x, extra) if k == "command" else _value(x, extra)
        return
    where = EXTRAS.get(key)
    if not where or v in (None, "", [], {}):
        return
    if where == "evidence.command":
        rec.setdefault("evidence", {}).setdefault("command", _command(v, extra))
        return
    if where in ("run", "step"):  # a run's goal, a step's name: free text
        rec[where] = _run(str(v)) if where == "run" else _step(str(v))
        return
    if "." in where:
        group, field = where.split(".", 1)
        rec.setdefault(group, {}).setdefault(field, _value(v, extra))
    else:
        rec[where] = _value(v, extra)


_HOOK_IDENTITY = contextvars.ContextVar("devkit_audit_hook_identity", default=False)
_THREAD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


@contextlib.contextmanager
def hook_identity():
    """Keep a hook's explicit payload identity ahead of inherited shell markers, including on nested hooks."""
    token = _HOOK_IDENTITY.set(True)
    try:
        yield
    finally:
        _HOOK_IDENTITY.reset(token)


def _codex_identity():
    """Codex audit metadata only: (root session, owned native child), or None for the existing identity path.
    A native marker never grants permission. Registered Claude ancestry wins over an inherited Codex marker;
    a different Codex root needs its process, explicit stamp and ownership of the native child to agree."""
    thread = os.environ.get("CODEX_THREAD_ID", "")
    if _HOOK_IDENTITY.get() or _FINDING or not _THREAD_ID.fullmatch(thread):
        return None
    _FINDING.append(True)  # records emitted during lookup keep the variables alone, as _found_session does
    try:
        from . import progress
        agent, sid, how = progress.resolve_session()
        if agent == "claude" and how == "ancestry":
            return None
        if (agent == "codex" and how == "ancestry" and sid != thread and _THREAD_ID.fullmatch(sid)
                and os.environ.get("DEVKIT_SESSION") == f"codex:{sid}"):
            run = core.read_json(progress.run_path("codex", sid), {})
            if (isinstance(run, dict) and run.get("agent") == "codex" and run.get("session") == sid
                    and isinstance((run.get("agents") or {}).get(thread), dict)):
                return sid, thread
    except Exception:
        pass  # uncertain provenance can name the native thread, never another session's run
    finally:
        _FINDING.pop()
    return thread, ""


def actor() -> dict:
    """The hook's explicit identity, the native Codex command's metadata, or the existing shell/process identity.
    This label does not change the caller identity used by command restriction checks. `host` is this Mac's machine
    id (identity.machine), the same whatever the hostname, so this Mac's records stay in one team folder; `label` is
    the hostname of the moment, which the SIEM exports name as the host (_hostname)."""
    from . import identity
    m = identity.machine()
    sess = os.environ.get("DEVKIT_SESSION", "")
    agent = os.environ.get("DEVKIT_AGENT") or ("claude" if os.environ.get("CLAUDECODE") else "")
    codex = _codex_identity()
    if codex:
        agent, sess = "codex", f"codex:{codex[0]}"
    elif not sess:
        agent, sess = _found_session() or (agent, "")
    return {"user": core.CFG.get("github_user") or os.environ.get("USER", "?"),
            "host": m.id, "label": m.label, "agent": agent or "human",
            "session": sess.split(":", 1)[-1][:12] if sess else ""}


_FOUND = {}  # (pid, folder, sessions folder) -> the session identity found, once per process and folder
_FINDING = []  # identity's own lookup may audit (an expired call): its records take the variables alone


def _found_session():
    """The agent session that runs this process when no variable names it: identity.speaker's agent and session
    (its process, call ledger or checkout), or None for the developer's own terminal and on any error."""
    if _FINDING:
        return None
    try:
        from . import progress
        k = (os.getpid(), os.getcwd(), str(progress.SESSIONS))
    except Exception:
        return None
    if k not in _FOUND:
        _FINDING.append(True)
        try:
            from . import identity
            sp = identity.speaker()
            _FOUND[k] = None if sp.how == "human" else (sp.agent, f"{sp.agent}:{sp.session}")
        except Exception:
            _FOUND[k] = None
        finally:
            _FINDING.pop()
    return _FOUND[k]


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
    """Where the caller works, as a record of it is filed (_context): its crew member when one runs the command, by
    its lane mark or the call ledger (_caller), and UNKNOWN_CREW when devkit cannot tie it to a lane, so a member's
    command without a lane mark is never read as the tech lead's (the actor of `dev branch embargo`). Native Codex
    record metadata does not alter it."""
    return _context(agent_id)


UNKNOWN_CREW = {"member": "unknown"}  # a record whose caller devkit cannot tie to a lane: `dev audit --member unknown`


def _caller(agent: str, sid: str) -> str:
    """Who makes a record that names no lane, in the session `agent:sid`: "main", a crew member's lane, or "" when
    devkit cannot tie the record to one. A hook knows from its payload (identity.from_hook: agent_id is a member's
    lane, its absence the main session). A `dev` command asks identity: its lane mark, the call ledger, or the main
    session when no crew member can be speaking. The hook stamps a member's lane only on its `dev progress|crew|watch|
    session` calls, so a member's `dev commit` reaches here with no lane named, and only the ledger tells it from
    the tech lead's. The audit never guesses: another session than the record's, a command that open calls of more
    than one caller hold (identity gives those to the member, for permissions), or a record made while identity
    looks (an expired call it audits) all give ""."""
    if _FINDING:
        return ""
    from . import identity
    payload = getattr(sys.modules.get("devkit.hooks"), "_PAYLOAD", None) if _HOOK_IDENTITY.get() else None
    if isinstance(payload, dict) and payload.get("session_id"):
        return identity.from_hook(agent, payload).lane if str(payload["session_id"]) == sid else ""
    _FINDING.append(True)
    try:
        from . import progress
        found = identity._find()
        sp = found.speaker
        if sp.how in ("human", "unknown") or (sp.agent, sp.session) != (agent, sid):
            return ""
        if sp.how == "ledger" and len({str(c.get("caller") or "main") for c in progress.open_calls(sid)
                                       if c.get("tool_use_id") in found.calls}) > 1:
            return ""
        return sp.lane
    except Exception:
        return ""
    finally:
        _FINDING.pop()


def _run_ref(run: dict) -> str:
    """The run's id as its records name it (r-<uuid>): it tells runs apart and names nothing. A run whose id cannot be
    read: its goal, which the record keeps only as its length."""
    try:
        from . import messaging
        return "r-" + messaging.run_id(run)
    except Exception:
        return str(run.get("goal") or "")


def _context(agent_id: str = "", *, native: bool = False) -> dict:
    """Where a record belongs: project, branch, run and step from the session and its run, and the
    crew member when one acts. A caller that named no lane is found (_caller): a member's record carries its own
    lane and step, the tech lead's the main lane's running step, and one devkit cannot tie to a lane no step and
    UNKNOWN_CREW. Only record construction opts into native Codex metadata, whose thread keeps the main session."""
    out: dict = {}
    env = os.environ
    codex = _codex_identity() if native else None
    aid = str(agent_id or (codex[1] if codex else "") or env.get("DEVKIT_FROM_AGENT") or "")
    who = aid  # the caller's lane as named; inside a run, as _caller finds it when none is named
    sess = f"codex:{codex[0]}" if codex else env.get("DEVKIT_SESSION", "")
    if ":" in sess:
        from . import progress
        agent, sid = sess.split(":", 1)
        s = core.read_json(progress.session_path(agent, sid), {}) or {}
        if codex and (not isinstance(s, dict) or s.get("agent") != agent or s.get("session") != sid):
            s = {}
        out.update(project=str(s.get("project") or ""), branch=str(s.get("branch") or ""))
        run = core.read_json(progress.run_path(agent, sid), None)
        if codex and (not isinstance(run, dict) or run.get("agent") != agent or run.get("session") != sid):
            run = None
        if isinstance(run, dict) and run.get("goal"):
            out["project"] = out["project"] or str(run.get("project") or "")
            out["run"] = _run_ref(run)
            agents = run.get("agents") or {}
            if not who:  # the Codex thread keeps the main session for a caller without a lane
                who = "main" if codex else _caller(agent, sid)
            key = who if who in agents else next((k for k in agents if len(who) >= 8 and k.startswith(who)), None)
            owner = key or ("main" if who == "main" else None)
            if key:
                lane = agents[key]
                chain = progress.lane_chain(run, key) or [key]
                head = agents.get(chain[-1]) or lane
                mid, role = _member(head.get("persona") or lane.get("persona") or "")
                out["crew"] = {k: v for k, v in (("member", mid), ("role", role), ("lane", key),
                                                 ("parent", str(lane.get("parent") or "main")),
                                                 ("tool_use_id", str(lane.get("launch") or ""))) if v}
            elif not who:
                out["crew"] = dict(UNKNOWN_CREW)
            step = next((n for n, x in enumerate(run.get("plan") or [], 1) if isinstance(x, dict) and owner
                         and (x.get("owner") or "main") == owner and x.get("state") == "running"), 0)
            if step:
                out["step"] = str(step)  # its number in the plan: the name is free text
    if who and (who != "main" or aid) and "crew" not in out and not codex:
        out["crew"] = {"lane": who}
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
    if str(action) in EVIDENCE_ACTIONS and extra.get("label") not in (None, ""):
        extra["label"] = evidence_label(extra["label"], rx)
    out, code, raw = outcome_of(outcome)
    cat, sev = kind(action)
    about = {"tool": extra.get("tool"), "irreversible": extra.get("irreversible"), "rule": extra.get("rule")}
    rec = {"v": V, "id": new_id(ts), "ts": ts, "time": iso(ts), "actor": actor(), "action": str(action)[:80],
           "category": cat, "severity": sev, "target": _field(str(action), "target", target or "", rx, about)[:300],
           "detail": _field(str(action), "detail", detail or "", rx, about)[:600], "outcome": out}
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
    where = _context(str((rec.get("crew") or {}).get("lane") or ""), native=True)
    for k in ("project", "branch", "run", "step"):  # the run by its id, the step by its number: never their names
        if where.get(k) and not rec.get(k):
            rec[k] = _run(where[k]) if k == "run" else _step(where[k]) if k == "step" else _clean(where[k], rx)
    found = where.get("crew") or {}
    if found == UNKNOWN_CREW and rec.get("crew"):
        found = {}  # a record that names its crew member keeps it: only who made the record is unknown
    crew = {**(rec.get("crew") or {}), **found}  # the run's lane wins over a caller's guess
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


# --------------------------------------------------------------------------- writing and the chain

CHECKS: list = []  # more checks for verify: fn(record, path, line) -> problem text, a Note or None


class Note(str):
    """What a check says about a record that is no problem: it could not check the record, for example. verify
    reports it, and the chain still holds."""


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


# --------------------------------------------------------------------------- segments
#
# The trail is a chain of units. An earlier devkit wrote one file per local month (YYYY-MM.jsonl), each its own chain.
# Now each record goes to the open segment, YYYY-MM-DD.NN.jsonl at the top of AUDIT: one per UTC day, the next
# sequence past SEGMENT_LIMIT, and a record whose clock went back stays in the open one. The first record of a
# segment links to the end of the unit before it, so the chain runs across segments; the first segment after a month
# file links to that file's end at that moment (the manifest's `origin`), since an earlier devkit still running may
# append to it. The audit job closes a segment once its UTC day has passed (close): it is compressed to
# YYYY-MM/<name>.jsonl.gz and listed in manifest.json with its size, SHA-256, record count, time span, records per day
# and per action, chain head and link. Readers skip the closed units a query rules out (_reaches). A month file is
# closed the same way once its month has passed.

SEGMENT_LIMIT = 16 * 1024 * 1024
_SEGMENT = re.compile(r"(\d{4}-\d{2}-\d{2})\.(\d{2,4})")  # a segment's name: its UTC day and its sequence that day
_MONTH = re.compile(r"\d{4}-\d{2}")  # a month file's name, and a month folder's
# at audit.level changes these, when they went well, are counted, and one summary per UTC day closes the day's segment
QUIET = frozenset({"dev.sync", "handoff.push"})
_HELD: dict = {}  # lock file -> how many times this process holds it
_READ_ERRORS = (OSError, EOFError, zlib.error)  # a unit that cannot be read, or a compressed one that is cut


def _utc_day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _unit_key(name: str) -> tuple:
    """Chain order: month files first, each its own chain, by month; then segments by day and sequence."""
    m = _SEGMENT.fullmatch(name)
    return (1, m.group(1), int(m.group(2))) if m else (0, name, 0)


def _filesize(path: Path) -> int:
    try:
        return os.stat(path).st_size
    except OSError:
        return -1


@contextlib.contextmanager
def _lock(name: str = ".lock"):
    """The audit folder's lock `name`: `.lock` to append and roll over, `.job.lock` to close, publish, prune and
    reseal. A process that holds it already (close, publish and reseal record what they do) does not wait on itself."""
    path = str(AUDIT / name)
    if _HELD.get(path):
        _HELD[path] += 1
        try:
            yield
        finally:
            _HELD[path] -= 1
        return
    _private_dir(AUDIT)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _HELD[path] = 1
        try:
            yield
        finally:
            _HELD.pop(path, None)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _write_file(path: Path, data: bytes, private: bool = True):
    """Write a file whole or not at all; `private`: mode 0600 in a 0700 folder, as the local trail is."""
    if private:
        _private_dir(path.parent)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _manifest_file(root: Path = None) -> Path:
    return (root or AUDIT) / "manifest.json"


def _unit_file(root: Path, name: str, published: bool = False) -> Path:
    """Where a closed unit lives, from its name alone: YYYY-MM/<name>.jsonl.gz here, YYYY-MM/<name>.jsonl in the
    handoff copy. A manifest's `file` is written for people and never read, so an edited manifest cannot point a
    reader, a reseal or a prune at a file outside the audit folder."""
    return root / name[:7] / (f"{name}.jsonl" if published else f"{name}.jsonl.gz")


def _read_manifest(path: Path) -> dict:
    """A manifest -> {v, units: [entry], origin?, start?}: empty when there is none, with `error` when it cannot be
    read (then nothing writes over it)."""
    try:
        m = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {"v": 1, "units": []}
    except (OSError, ValueError) as e:
        return {"v": 1, "units": [], "error": str(e)}
    if not isinstance(m, dict) or not isinstance(m.get("units"), list):
        return {"v": 1, "units": [], "error": "it is not a manifest"}
    m["units"] = [e for e in m["units"] if isinstance(e, dict) and isinstance(e.get("name"), str)
                  and (_SEGMENT.fullmatch(e["name"]) or _MONTH.fullmatch(e["name"]))]
    return m


def _manifest_bytes(m: dict) -> bytes:
    out = {k: v for k, v in m.items() if k != "error"}
    out["v"] = 1
    out["units"] = sorted(m["units"], key=lambda e: _unit_key(e["name"]))
    return (json.dumps(out, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _write_manifest(path: Path, m: dict, private: bool = True):
    if m.get("error"):
        raise OSError(f"{core.tilde(path)} cannot be read ({m['error']}): it is not written over")
    _write_file(path, _manifest_bytes(m), private)


def _units(root: Path = None, man: dict = None) -> list:
    """This Mac's trail in chain order -> [{name, kind (month | day), path, closed, entry}]: the closed units the
    manifest lists, the plain files at the top of the audit folder (the open segment, segments not closed yet, month
    files of an earlier devkit), and a compressed file the manifest does not list (closed, no entry). A compressed
    file the manifest does not list, next to a plain file of its name, is a close the job did not finish: the plain
    file is the unit, and the next close writes the compressed one again from it."""
    root = root or AUDIT
    man = _read_manifest(_manifest_file(root)) if man is None else man
    out, listed = [], {}
    for e in man["units"]:
        u = {"name": e["name"], "kind": "day" if _SEGMENT.fullmatch(e["name"]) else "month",
             "path": _unit_file(root, e["name"]), "closed": True, "entry": e}
        listed[e["name"]] = u
        out.append(u)
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    plain = {n[:-6] for n in names if n.endswith(".jsonl")}
    for n in names:
        stem = n[:-6] if n.endswith(".jsonl") else ""
        if stem and (_SEGMENT.fullmatch(stem) or _MONTH.fullmatch(stem)):
            done = listed.get(stem)
            if done and done["entry"].get("bytes") == _filesize(root / n) and _same(root / n, done["entry"]):
                continue  # closed: the job has not removed the plain copy yet
            out.append({"name": stem, "kind": "day" if _SEGMENT.fullmatch(stem) else "month", "path": root / n,
                        "closed": False, "entry": None})
        elif _MONTH.fullmatch(n) and (root / n).is_dir():
            for g in sorted(os.listdir(root / n)):
                gs = g[:-9] if g.endswith(".jsonl.gz") else ""
                if gs and gs not in listed and gs not in plain and (_SEGMENT.fullmatch(gs) or _MONTH.fullmatch(gs)):
                    out.append({"name": gs, "kind": "day" if _SEGMENT.fullmatch(gs) else "month",
                                "path": root / n / g, "closed": True, "entry": None})
    return sorted(out, key=lambda u: (_unit_key(u["name"]), not u["closed"]))


def _same(path: Path, entry: dict) -> bool:
    """Is this plain file the very unit a manifest entry describes (its SHA-256)?"""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest() == entry.get("sha256")
    except OSError:
        return False


def read_trail(path) -> bytes:
    """A unit's bytes, as they were written: a compressed one (.gz) decompressed."""
    p = Path(path)
    data = p.read_bytes()
    return gzip.decompress(data) if p.name.endswith(".gz") else data


def open_trail(path):
    """A unit opened to read its lines one at a time, in binary: a compressed one (.gz) decompressed as it is read."""
    p = Path(path)
    return gzip.open(p, "rb") if p.name.endswith(".gz") else open(p, "rb")


def _open_segment():
    """The newest plain segment at the top of the audit folder -> (path, day, sequence), or None: only it takes
    records. Under the append lock. A plain segment the manifest lists already is one a close did not finish
    removing: it never takes a record, and it is removed when it is the very unit listed (_drop_leftover)."""
    while True:
        best = None
        try:
            names = os.listdir(AUDIT)
        except OSError:
            return None
        for n in names:
            m = _SEGMENT.fullmatch(n[:-6]) if n.endswith(".jsonl") else None
            if m and (best is None or (m.group(1), int(m.group(2))) > best[1:]):
                best = (AUDIT / n, m.group(1), int(m.group(2)))
        if best is None or not _unit_file(AUDIT, best[0].name[:-6]).is_file():
            return best  # the usual case: no compressed file of its name, so no close has begun
        e = next((e for e in _read_manifest(_manifest_file())["units"] if e["name"] == best[0].name[:-6]), None)
        if e is None:
            return best  # the close stopped before it listed the unit: the plain file is still the unit
        if not _drop_leftover(best[0], e):
            return None  # listed, and not that unit: the next record starts a segment after it, and verify reports it


def _drop_leftover(path: Path, entry: dict) -> bool:
    """Remove a plain file the manifest lists already when it is the very unit listed (a close that stopped before it
    removed it) -> whether it was removed. Under the append lock, and the file's own lock, which an earlier devkit's
    writer takes to append to its month file."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if os.fstat(fd).st_size != entry.get("bytes") or not _same(path, entry):
            return False
        os.unlink(path)
        return True
    finally:
        os.close(fd)  # and with it the lock


def _append(rec: dict, now: float = None) -> dict:
    """Chain and append one record to the open segment, under the folder's lock (writers in other processes wait for
    it). `now`: the clock, time.time() by default."""
    with _lock():
        return _write_record(rec, time.time() if now is None else now)


def _write_record(rec: dict, now: float, flush: bool = True) -> dict:
    """Append under the lock: to the open segment while it is the record's UTC day (or a later one) and under
    SEGMENT_LIMIT, else to a new segment whose first record links to the end of the unit before it."""
    day = max(_utc_day(rec["ts"]), _utc_day(now))
    seg = _open_segment()
    if seg and seg[1] >= day and _filesize(seg[0]) < SEGMENT_LIMIT:
        path, link = seg[0], None
    else:
        path, link = _roll(seg, day, now, flush)
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a+b") as fh:
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        size = os.fstat(fd).st_size
        if size:
            fh.seek(size - 1)
            if fh.read(1) != b"\n":
                fh.write(b"\n")
                fh.flush()
        if link is None or size:
            link = seal(*_tail(fh))
        rec["prev"] = link
        rec["hash"] = digest(rec)
        fh.write((json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        fh.flush()
    return rec


def _roll(seg, day: str, now: float, flush: bool) -> tuple:
    """A new segment for `day` -> (its path, the link its first record takes): the end of the unit before it, the open
    segment's or the newest closed one's; for the first segment, the newest month file's (`origin`). A new day first
    writes the summaries of the days the open segment holds."""
    if seg and flush and day > seg[1]:
        _flush_quiet(seg[1], now)
        seg = _open_segment()
    man = _read_manifest(_manifest_file())
    closed = next((e for e in reversed(man["units"]) if _SEGMENT.fullmatch(e["name"])), None)
    keys = [(seg[1], seg[2])] if seg else []
    if closed:
        m = _SEGMENT.fullmatch(closed["name"])
        keys.append((m.group(1), int(m.group(2))))
    top = max(keys) if keys else None
    day, seq = (top[0], top[1] + 1) if top and top[0] >= day else (day, 1)
    if seg and (not keys or (seg[1], seg[2]) == top):
        with open(seg[0], "rb") as fh:
            link = seal(*_tail(fh))
    elif closed:
        link = str(closed.get("link") or "")
    elif isinstance(man.get("start"), dict):
        link = str(man["start"].get("link") or "")
    else:
        link = _origin(man)
    return AUDIT / f"{day}.{seq:02d}.jsonl", link


def _origin(man: dict) -> str:
    """The first segment's link: the end of the newest month file an earlier devkit wrote, as it is now (an earlier
    devkit still running may append to it later), recorded in the manifest as `origin`; "" when there is none."""
    months = [u for u in _units(man=man) if u["kind"] == "month"]
    if not months:
        return ""
    u = months[-1]
    if u["closed"] and u["entry"]:
        size, link = int(u["entry"].get("bytes") or 0), str(u["entry"].get("link") or "")
    else:
        with open(u["path"], "rb") as fh:
            fcntl.flock(fh, fcntl.LOCK_SH)  # an earlier devkit's writer holds it while it appends
            try:
                size = os.fstat(fh.fileno()).st_size
                link = seal(*_tail(fh))
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
    if not man.get("error"):
        man["origin"] = {"unit": u["name"], "bytes": size, "link": link}
        _write_manifest(_manifest_file(), man)
    return link


def _entry(name: str, data: bytes) -> dict:
    """A closed unit's manifest entry: where it is, its size and SHA-256 (of the uncompressed bytes), its v2 records
    and other lines, the first and last record time, records per UTC day and per action, and its chain head and link
    (what the next segment's first `prev` is)."""
    records = v1 = 0
    lo = hi = None
    days, actions = {}, {}
    for ln in data.splitlines():
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(r, dict):
            continue
        if r.get("v") == V and isinstance(r.get("hash"), str):
            records += 1
        else:
            v1 += 1
        ts = _stored_ts(r)
        lo, hi = (ts, ts) if lo is None else (min(lo, ts), max(hi, ts))
        d = _utc_day(ts)
        days[d] = days.get(d, 0) + 1
        a = str(r.get("action") or "")
        actions[a] = actions.get(a, 0) + 1
    head, tail = _chain_end(data, len(data))
    return {"name": name, "kind": "day" if _SEGMENT.fullmatch(name) else "month",
            "file": f"{name[:7]}/{name}.jsonl.gz", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "records": records, "v1": v1, "lo": lo, "hi": hi, "days": days, "actions": actions, "head": head,
            "link": seal(head, tail)}


def close(now: float = None, force: bool = False) -> list:
    """Close what is done: the open segment once its UTC day has passed (after that day's summaries), every other
    plain segment, and each month file of an earlier devkit once its month has passed. Each is compressed to
    YYYY-MM/<name>.jsonl.gz, listed in the manifest, then its plain file removed. `force` closes the open segment too
    (a reseal does, so its record publishes at once). A close the job did not finish is finished: a plain file whose
    compressed file the manifest does not list yet is closed again from the start, and one the manifest lists already
    is removed. -> the compressed files, in chain order."""
    if not AUDIT.is_dir():
        return []
    now = time.time() if now is None else now
    today, month = _utc_day(now), time.strftime("%Y-%m", time.localtime(now))
    out = []
    with _lock(".job.lock"):
        with _lock():
            _flush_quiet(_utc_day(now - 86400), now)
            man = _read_manifest(_manifest_file())
            for e in [] if man.get("error") else man["units"]:
                if (AUDIT / f"{e['name']}.jsonl").is_file():
                    _drop_leftover(AUDIT / f"{e['name']}.jsonl", e)
        _forward_summaries()
        if man.get("error"):
            return []
        units = _units(man=man)
        done = {e["name"] for e in man["units"]}  # listed: a compressed file alone may be a close that stopped
        # a month file an earlier devkit wrote again after it was closed stays plain: closing it would replace the
        # closed one
        plain = [u for u in units if not u["closed"] and u["name"] not in done]
        newest = max((u for u in plain if u["kind"] == "day"), key=lambda u: _unit_key(u["name"]), default=None)
        for u in plain:
            due = u["name"] < month if u["kind"] == "month" else (u is not newest or force or u["name"][:10] < today)
            gz = _close_unit(u) if due else None
            if gz:
                out.append(gz)
    return out


def _close_unit(u: dict):
    """Compress one plain unit, list it in the manifest, and remove it. It is compressed before any lock, so writers
    do not wait on that; then, under the append lock (this devkit's writers) and the file's own lock (an earlier
    devkit's writer appending to its month file), it is read again when a record joined it meanwhile, and no record
    can join it from that read to its removal. The append lock comes first, the order _origin and reseal take them
    in. -> the compressed file, or None when it cannot be read."""
    name, path = u["name"], u["path"]
    gz = _unit_file(AUDIT, name)
    try:
        data = path.read_bytes()
    except OSError:
        return None
    _write_file(gz, gzip.compress(data, 6, mtime=0))
    with _lock():
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if os.fstat(fd).st_size != len(data):
                data = path.read_bytes()
                _write_file(gz, gzip.compress(data, 6, mtime=0))
            man = _read_manifest(_manifest_file())
            man["units"] = [e for e in man["units"] if e["name"] != name] + [_entry(name, data)]
            _write_manifest(_manifest_file(), man)
            path.unlink()
        finally:
            os.close(fd)  # and with it the file's lock
    return gz


# --------------------------------------------------------------------------- records summarized at audit.level changes

def _quiet_file() -> Path:
    return AUDIT / ".quiet.json"


def _is_quiet(rec: dict, level: str) -> bool:
    """dev.sync and handoff.push that went well, at the default level: counted, not written one by one."""
    return level == "changes" and rec.get("action") in QUIET and rec.get("outcome") == "ok" and not rec.get("why")


def _count_quiet(rec: dict):
    """Count a record _is_quiet keeps out, by UTC day, action and agent, until its day's summary is written."""
    with _lock():
        q = core.read_json(_quiet_file(), None)
        q = q if isinstance(q, dict) else {}
        ts, a = rec["ts"], rec.get("actor") or {}
        c = q.setdefault(_utc_day(ts), {}).setdefault(rec["action"], {"n": 0, "first": ts, "last": ts, "agents": {}})
        c["n"] += 1
        c["first"], c["last"] = min(c["first"], ts), max(c["last"], ts)
        agent = str(a.get("agent") or "human")
        c["agents"][agent] = c["agents"].get(agent, 0) + 1
        c["actor"] = {k: str(a.get(k) or "") for k in ("user", "host", "label")}
        _write_file(_quiet_file(), json.dumps(q, sort_keys=True).encode())


def _summary(day: str, action: str, c: dict, ts: float) -> dict:
    """The record of a day's counted dev.sync or handoff.push: how many, by which agents, under the machine's user."""
    ts = round(ts, 3)
    cat, sev = kind(action)
    n = c["n"]
    a = c.get("actor") if isinstance(c.get("actor"), dict) else {}
    agents = c.get("agents") if isinstance(c.get("agents"), dict) else {}
    who = _clean(", ".join(f"{k} {v}" for k, v in sorted(agents.items())), [])[:200]
    return {"v": V, "id": new_id(ts), "ts": ts, "time": iso(ts),
            "actor": {"user": str(a.get("user") or ""), "host": str(a.get("host") or ""),
                      "label": str(a.get("label") or ""), "agent": "job", "session": ""},
            "action": action, "category": cat, "severity": sev, "target": f"{day} (UTC)",
            "detail": f"{n} ok that day, summarized at audit.level changes ({who}); a failure is recorded on its own",
            "outcome": "ok", "evidence": {"count": n}, "ref": {"type": "summary"}}


_UNFORWARDED: list = []  # summaries _flush_quiet wrote under the append lock, for audit.forward once it is released


def _forward_summaries(cfg: dict = None):
    """Send the summaries written under the append lock to audit.forward, after the lock: a slow syslog never holds
    up the writers. Never raises."""
    while _UNFORWARDED:
        rec = _UNFORWARDED.pop(0)
        try:
            _forward(rec, cfg or settings())
        except Exception:
            pass


def _flush_quiet(through: str, now: float):
    """Under the append lock: write the summaries of every counted UTC day up to `through`, then forget their counts.
    Each goes to the segment of its day, or after it when that one is closed, and to audit.forward once the lock is
    released (_forward_summaries)."""
    q = core.read_json(_quiet_file(), None)
    if not isinstance(q, dict) or not q:
        return
    done = sorted(d for d in q if d <= through)
    for d in done:
        try:
            start = calendar.timegm(time.strptime(d, "%Y-%m-%d"))
        except ValueError:
            start = None  # not a day: its counts are dropped
        counts = q[d] if isinstance(q[d], dict) and start is not None else {}
        for action, c in sorted(counts.items()):
            if action in QUIET and isinstance(c, dict) and isinstance(c.get("n"), int) and c["n"] > 0:
                # its time is the last one it counts, kept within its own day, so it closes that day's segment
                last = c.get("last")
                ts = float(last) if isinstance(last, (int, float)) and not isinstance(last, bool) else now
                ts = min(max(ts, start), start + 86399.999)
                _UNFORWARDED.append(_write_record(_summary(d, action, c, ts), ts, flush=False))
        del q[d]
    if q:
        _write_file(_quiet_file(), json.dumps(q, sort_keys=True).encode())
    elif done:
        with contextlib.suppress(OSError):
            _quiet_file().unlink()


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
    """A change of the effective audit settings is recorded before anything else: the settings a machine starts
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


_RESERVED_IDS: set = set()  # ids reserve_id() handed out in this process that no record has taken yet


def reserve_id() -> str:
    """A record id handed out before its record is written, for a caller that names the record in a file it must
    write first: the extension acceptance list names its ext.accept record, and the record is written only once the
    list is, so a list that cannot be written leaves no record of an acceptance. Only record(..., reserved_id=)
    takes it, and only once: a caller cannot give a record an id of its own or one already used."""
    rid = new_id(time.time())
    _RESERVED_IDS.add(rid)
    return rid


def record(action: str, target: str = "", detail: str = "", outcome: str = "ok", *, reserved_id: str = "",
           **extra):
    """Append one audit record (schema v2), chained to the one before it. `reserved_id`: an id reserve_id() gave
    this process, which the record takes; any other value is ignored. -> the record, or None when the level leaves it
    out. Never raises."""
    try:
        cfg = settings()
        _note_settings(cfg)
        rec = build(action, target, detail, outcome, extra, cfg)
        if reserved_id and reserved_id in _RESERVED_IDS:
            _RESERVED_IDS.discard(reserved_id)
            rec["id"] = reserved_id
        if not kept(rec, cfg["level"]):
            return None
        if _is_quiet(rec, cfg["level"]):
            _count_quiet(rec)
            return None
        rec = _append(rec)
        _forward_summaries(cfg)  # the first record of a new day wrote the summaries of the days before it
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


# --------------------------------------------------------------------------- which commands are recorded

# dev commands that only read; everything else is audited (and these too at audit.level all)
READ_ONLY = {"help", "ls", "go", "status", "open", "overview", "standup", "dashboard", "team", "report", "time",
             "files", "find", "grep", "diff", "canary", "du", "doctor", "version", "agents", "calver", "audit",
             "__complete", "completion", "hook", "plugin", "docs", "diagram", "stubs", "mcp",
             "test",  # dev test run|plan|prove|audit: runs and reads tests (prove in a temp copy), changes nothing
             "watch", "watches"}  # a wait only tells what is new, once, and audits each wake itself (watch.wake)
READ_SUB = {("task", "ls"), ("task", "show"), ("task", "history"), ("task", None), ("plan", "ls"), ("plan", None),
            ("lesson", "ls"), ("lesson", "show"), ("lesson", "scan"), ("lesson", "review"), ("lesson", None),
            ("progress", None), ("progress", "time"), ("delivery", None), ("initiative", "ls"), ("initiative", "show"),
            ("wt", "ls"), ("config", "diff"), ("config", "status"), ("config", "show"), ("config", "validate"),
            ("jobs", "ls"), ("jobs", None), ("jobs", "show"), ("jobs", "logs"), ("jobs", "journal"), ("pr", "ls"),
            ("pr", "view"), ("pr", "checks"), ("pr", "diff"), ("run", "ls"), ("run", "view"), ("branch", "ls"),
            ("issue", "ls"), ("issue", "view"), ("sec", "scan"), ("desktop", "status"), ("crew", None),
            ("crew", "show"), ("crew", "brief"), ("crew", "inbox"), ("crew", "decisions"), ("crew", "next"),
            ("crew", "owner"),
            ("ci", None), ("ci", "status"), ("ci", "audit"), ("session", "brief"), ("session", "compactions"),
            ("session", None), ("session", "ls"), ("session", "inbox"), ("session", "claims"),
            ("session", "escalations"), ("alias", None)}
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
    if cmd == "branch" and sub == "prune":  # the listing only reads; --apply deletes
        return "--apply" in argv
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


# --------------------------------------------------------------------------- reading (version 1 records too)

_V1_CORE = {"ts", "user", "host", "agent", "session", "action", "target", "detail", "outcome"}


def _reclean(r: dict, rx) -> dict:
    """A v2 record as a reader sees it: its text fields cleaned with today's rules, which records sealed before them
    did not get (crew message text and home folders before 26.11.3, secrets the redactor learned since). The stored
    line and its hash stay as they are, so `verify` still checks them; a view that differs is marked `cleaned`."""
    action, out = str(r.get("action") or ""), dict(r)
    evidence, ref = (r.get(k) if isinstance(r.get(k), dict) else {} for k in ("evidence", "ref"))
    about = {"tool": evidence.get("tool"), "irreversible": ref.get("irreversible"), "rule": evidence.get("rule")}
    for k in ("target", "detail"):
        if isinstance(r.get(k), str):
            out[k] = _field(action, k, r[k], rx, about)[:_CUT[k]]
    for k in ("why", "project", "branch", "run", "step"):
        if isinstance(r.get(k), str):
            out[k] = _run(r[k]) if k == "run" else _step(r[k]) if k == "step" else _clean(r[k], rx)[:_CUT.get(k)]
    for group in ("evidence", "ref", "crew"):
        if isinstance(r.get(group), dict):
            out[group] = {k: (_command(v, rx) if group == "evidence" and k == "command" else _clean(v, rx)[:VALUE_CUT])
                          if isinstance(v, str) else v for k, v in r[group].items()}
    if action in EVIDENCE_ACTIONS and not _UNLINE.fullmatch(str(r.get("detail") or "")) and \
            (out.get("ref") or {}).get("answer"):
        out["ref"] = {**out["ref"], "answer": _option(out["ref"]["answer"], None)}  # never checked against options
    if action in DECISION_ACTIONS:
        _decided(out)  # out's ref is its own copy; a record sealed without labels keeps no answer as said
    if out != r:
        out["cleaned"] = True
    return out


def _v1_id(r: dict, line: bytes = b"") -> str:
    """The id a v1 record reads with: it stored none, so its line's hash."""
    return "V1" + hashlib.sha256(line or json.dumps(r, sort_keys=True).encode()).hexdigest()[:24].upper()


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
    rec = {"v": 1, "id": _v1_id(r, line),
           "ts": ts, "time": iso(ts), "actor": {k: str(r.get(k) or "") for k in ("user", "host", "agent", "session")},
           "action": action, "category": cat, "severity": sev,
           "target": _field(action, "target", r.get("target") or "", rx, r),
           "detail": _field(action, "detail", r.get("detail") or "", rx, r), "outcome": out}
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


def _reaches(entry, lo=None, hi=None, action=None) -> bool:
    """Can a closed unit hold a record of `lo`..`hi` (epoch) and `action` (a prefix)? Its manifest entry says: the
    first and last record time, and the actions it holds. A unit with no entry is always read."""
    if not isinstance(entry, dict):
        return True
    first, last, acts = entry.get("lo"), entry.get("hi"), entry.get("actions")
    if lo and isinstance(last, (int, float)) and last < lo:
        return False
    if hi and isinstance(first, (int, float)) and first > hi:
        return False
    return not (action and isinstance(acts, dict) and not any(str(a).startswith(action) for a in acts))


def _team_files(lo=None, hi=None, action=None) -> list:
    """Every machine's files in the handoff copy, in path order, but the published segments its manifest rules out."""
    root = core.HANDOFF / "audit"
    if not root.is_dir():
        return []
    skip = set()
    for mf in root.glob("*/manifest.json"):
        skip |= {_unit_file(mf.parent, e["name"], published=True) for e in _read_manifest(mf)["units"]
                 if not _reaches(e, lo, hi, action)}
    return [f for f in sorted(root.rglob("*.jsonl")) if f not in skip]


def trail_files(team: bool = False, lo=None, hi=None, action=None, root: Path = None) -> list:
    """The trail's files a reader needs, in read order: with `team`, every machine's handoff copy first, then this
    Mac's units in chain order (`root`: the audit folder, AUDIT by default). A closed unit whose time span or actions
    rule out `lo`..`hi` (epoch) or `action` (a prefix) is left out, and so is one whose file is gone. Read each with
    read_trail, or line by line with open_trail."""
    out = _team_files(lo, hi, action) if team else []
    for u in _units(root):
        if u["closed"] and (not _reaches(u["entry"], lo, hi, action) or not u["path"].is_file()):
            continue
        out.append(u["path"])
    return out


def _files(team: bool = False) -> list:
    return trail_files(team)


def _stored_ts(r: dict) -> float:
    """A stored record's time as lift reads it: a v1 record's text time as a number, none (or no number) as 0."""
    if r.get("v") == V:
        return r.get("ts") or 0
    try:
        return float(r.get("ts") or 0)
    except (TypeError, ValueError):
        return 0


def _stored(team: bool = False, lo=None, hi=None, action=None, keep=None):
    """Every stored record once, not lifted, as (record, line): the first copy of an id read decides, as a published
    day repeats this Mac's lines. `lo`, `hi` (epoch) and `action` (a prefix) are checked on the stored time and
    action, which lift does not change (a v1 record's as lift maps them), and pick the closed units to read; `keep`,
    on a v2 record only, checks the fields lift leaves as they are (see _checks). Cleaning is most of a query's time,
    so a reader lifts only what passes."""
    seen = set()
    for f in trail_files(team, lo, hi, action):
        try:
            data = read_trail(f)
        except _READ_ERRORS:
            continue
        for ln in data.splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            v2 = r.get("v") == V
            key = r["id"] if v2 else _v1_id(r, ln)
            if key in seen:
                continue
            seen.add(key)
            if lo or hi:
                ts = _stored_ts(r)
                if (lo and ts < lo) or (hi and ts > hi):
                    continue
            if action and not (r["action"] if v2 else str(r.get("action") or "")).startswith(action):
                continue
            if keep and v2 and not keep(r):
                continue
            yield r, ln


def _redact_rules() -> list:
    """The compiled audit.redact.extra patterns a reader cleans with; none when the settings cannot be read."""
    try:
        return _extra_rules(settings()["redact"]["extra"])
    except Exception:
        return []


def _read(team: bool = False, lo=None, hi=None, action=None, keep=None):
    """Every record once, lifted, of those _stored passes."""
    rx = _redact_rules()
    for r, ln in _stored(team, lo, hi, action, keep):
        yield lift(r, ln, rx)


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


def _window(since=None, until=None, days=None) -> tuple:
    """(lo, hi) in epoch seconds, None where open: --since (or the last `days`) and --until, a date's whole day."""
    lo = when(since) if since else (time.time() - days * 86400 if days else None)
    return lo, (when(until, end=True) if until else None)


def _checks(actor_=None, project=None, member=None, category=None, severity=None, outcome=None, grep=None) -> tuple:
    """The filters past the time and the action, as two checks of a lifted record -> (same, rest). `same` reads the
    fields lift never changes (category, severity, outcome and actor: it cleans text, never these), so it also holds
    on a stored v2 record, before lift; `rest` reads the fields cleaning can change (the project, the member and the
    text --grep reads: a home folder becomes ~), on the lifted record only."""
    want_member = _member(member)[0] if member else None
    min_sev = SEVERITIES.index(severity) if severity in SEVERITIES else 0
    want_out = outcome_of(outcome)[0] if outcome else None
    rx = None
    if grep:
        try:
            rx = re.compile(grep, re.I)
        except re.error:
            rx = re.compile(re.escape(grep), re.I)

    def same(r: dict) -> bool:
        if actor_:
            a = r.get("actor") or {}
            if actor_ not in f"{a.get('user')}@{a.get('host')} {a.get('agent')}":
                return False
        if category and r.get("category") != category:
            return False
        if min_sev and SEVERITIES.index(r.get("severity", "info")) < min_sev:
            return False
        return not (want_out and r.get("outcome") != want_out)

    def rest(r: dict) -> bool:
        if project and not (r.get("project") == project or str(r.get("project", "")).startswith(project.rstrip("/") + "/")):
            return False
        if want_member and (r.get("crew") or {}).get("member") != want_member:
            return False
        return not (rx and not rx.search(" ".join(str(r.get(k) or "") for k in ("action", "target", "detail", "why"))))

    return same, rest


def query(since=None, until=None, days=None, action=None, actor_=None, project=None, member=None, category=None,
          severity=None, outcome=None, grep=None, team=False) -> list:
    """Records that match every filter, oldest first. `severity`: that level and above; `grep`: a regular
    expression (any case) over the action, target, detail and why; `member`: a crew member's name or id. The time,
    the action and, on a v2 record, the fields lift never changes are checked before a record is lifted."""
    lo, hi = _window(since, until, days)
    same, rest = _checks(actor_, project, member, category, severity, outcome, grep)
    out = [r for r in _read(team, lo, hi, action, same) if same(r) and rest(r)]  # same again: a v1 record, lifted
    return sorted(out, key=lambda r: r.get("ts") or 0)


def latest(n: int, since=None, until=None, days=None, action=None, actor_=None, project=None, member=None,
           category=None, severity=None, outcome=None, grep=None, team=False) -> tuple:
    """(how many records match the filters, the newest `n` of them oldest first): what the table view shows, the same
    as (len(q), q[-n:]) for q = query(...). With only the time and action filters, it finds the matches on the stored
    records and lifts only the ones it returns: a heavy week's table cleans its 60 rows, not the week's 50,000."""
    if actor_ or project or member or category or severity or outcome or grep:
        recs = query(since, until, days, action, actor_, project, member, category, severity, outcome, grep, team)
        return len(recs), recs[-n:]
    lo, hi = _window(since, until, days)
    found = sorted(_stored(team, lo, hi, action), key=lambda p: _stored_ts(p[0]))  # stable: read order on a tie
    rx = _redact_rules()
    return len(found), [lift(r, ln, rx) for r, ln in found[-n:]]


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


# --------------------------------------------------------------------------- verify

def _bridged(prev, want: str, bridges: dict) -> bool:
    """Does `prev` name the old end of a unit a reseal rewrote, whose new end is `want` (through one reseal or more)?"""
    seen, x = set(), prev
    while isinstance(x, str) and x in bridges and x not in seen:
        seen.add(x)
        x = bridges[x]
        if x == want:
            return True
    return False


def verify_file(path: Path, chain: dict = None) -> dict:
    """The chain of one unit (a segment, a month file, a published day): its first broken link, and what the CHECKS
    find in the intact records: their problems (`checks`) and their notes (`notes`, no problem). `chain` carries the
    chain in from the unit before it, and is updated to this unit's end: `last` (the hash of the last v2 record),
    `tail` (the lines after it), `link` (the `prev` a held day leaves when lines outside the chain ended it, or that
    the first segment starts from), `carry` (a line the file before ended in the middle of) and `more` (another file
    follows). It may also carry `bridges` ({a resealed unit's old link: its new one}, which a link may still name)
    and `reseals` (filled with {reseal id: evidence.check} of the audit.reseal records read). A compressed unit is
    read decompressed; `sha256` and `bytes` describe its bytes."""
    res = {"path": str(path), "records": 0, "v1": 0, "first": None, "checks": [], "notes": []}
    c = chain if chain is not None else {}
    try:
        own = read_trail(path)
    except _READ_ERRORS as e:
        res["first"] = {"path": str(path), "line": 0, "id": "", "kind": "unreadable", "problem": str(e)}
        return res
    res["sha256"], res["bytes"] = hashlib.sha256(own).hexdigest(), len(own)
    data = c.get("carry", b"") + own
    last, tail, link = c.get("last", ""), c.get("tail", b""), c.get("link")
    bridges, reseals = c.get("bridges") or {}, c.get("reseals")
    lines = data.splitlines(keepends=True)
    carry = lines.pop() if c.get("more") and lines and not lines[-1].endswith((b"\n", b"\r")) else b""
    for n, raw in enumerate(lines, 1):
        body = raw.rstrip(b"\r\n")
        try:
            r = json.loads(body) if body.strip() else None
        except ValueError:
            r = None
        if isinstance(r, dict) and r.get("v") == V:
            res["records"] += 1
            # the link it must have; None when a held day left a link and lines outside the chain follow it here
            want = seal(last, tail) if link is None else (None if tail else link)
            last, tail, link = str(r.get("hash") or ""), b"", None  # the chain goes on from this record, intact or not
            if res["first"]:
                continue
            where = {"path": str(path), "line": n, "id": str(r.get("id") or "")}
            if r.get("hash") != digest(r):
                res["first"] = {**where, "kind": "edited", "problem": "the record does not match its hash: it was "
                                "changed after it was written"}
            elif want is not None and r.get("prev") != want and not _bridged(r.get("prev"), want, bridges):
                res["first"] = {**where, "kind": "unlinked", "problem": "the record does not link to the one before "
                                "it: a record was removed, inserted or changed before it"}
            else:
                if reseals is not None and r.get("action") == "audit.reseal" and r.get("outcome") == "ok":
                    reseals[str((r.get("ref") or {}).get("id") or "")] = str((r.get("evidence") or {}).get("check"))
                if want is None:
                    res["notes"].append({**where, "kind": "note", "note": "its link to the held day before it cannot "
                                         "be checked here: lines outside the chain come between them"})
                for chk in list(CHECKS):
                    try:
                        problem = chk(r, path, n)
                    except Exception as e:
                        problem = f"the check failed: {e}"
                    if isinstance(problem, Note):
                        res["notes"].append({**where, "kind": "note", "note": str(problem)})
                    elif problem:
                        res["checks"].append({**where, "kind": "check", "problem": str(problem)})
            continue
        if isinstance(r, dict):
            res["v1"] += 1
        tail += raw
    if chain is not None:
        chain.update(last=last, tail=tail, link=link, carry=carry)
    return res


_DAY_PART = re.compile(r"(\d{4}-\d{2}-\d{2})(\.jsonl|\.held\.json)")  # in a month folder: a day's file, a held day


def _parts(where: Path, month: str) -> list:
    """What a machine's folder publishes of one month, in chain order -> [(kind, path)]: ("file", the one-file copy
    YYYY-MM.jsonl), then per day ("file", YYYY-MM/YYYY-MM-DD.jsonl) or ("held", YYYY-MM/YYYY-MM-DD.held.json). A
    held day's record wins over a file of the same day."""
    whole, folder = where / f"{month}.jsonl", where / month
    parts = [("file", whole)] if whole.is_file() else []
    days = {}
    for p in (folder.iterdir() if folder.is_dir() else ()):
        m = _DAY_PART.fullmatch(p.name)
        if m and m.group(1).startswith(month + "-") and p.is_file():
            if m.group(2) == ".held.json":
                days[m.group(1)] = ("held", p)
            else:
                days.setdefault(m.group(1), ("file", p))
    return parts + [days[d] for d in sorted(days)]


def _held_info(path: Path):
    """A held day's record (what _held_day wrote), or None when it cannot be read."""
    try:
        info = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    ok = isinstance(info, dict) and isinstance(info.get("bytes"), int) and isinstance(info.get("sha256"), str)
    return info if ok else None


def _verify_parts(parts: list) -> list:
    """One machine's month in the handoff repository, verified as one chain across its files -> a result per file,
    in order. A held day was never published: the chain goes on from the head its record names, and a note says the
    day is checked on its own machine only."""
    out, chain = [], {}
    for i, (kind, path) in enumerate(parts):
        if kind == "file":
            chain["more"] = i + 1 < len(parts) and parts[i + 1][0] == "file"
            out.append(verify_file(path, chain))
            continue
        res = {"path": str(path), "records": 0, "v1": 0, "first": None, "checks": [], "notes": []}
        info = _held_info(path)
        if info is None:
            res["first"] = {"path": str(path), "line": 0, "id": "", "kind": "unreadable",
                            "problem": "the record of a held day cannot be read"}
            chain = {}
        else:
            res["notes"].append({"path": str(path), "line": 0, "id": "", "kind": "note",
                                 "note": f"{info.get('day')} is held on its machine ({_mb(info['bytes'])}, over the "
                                         f"{_mb(PUBLISH_LIMIT)} limit for one handoff file): its "
                                         f"{info.get('records', 0)} record(s) are checked there only"})
            ended = bool(info.get("tail_bytes"))  # lines outside the chain ended the day: only their seal is known
            chain = {"last": str(info.get("head") or ""), "tail": b"", "link": str(info.get("link") or "") if ended
                     else None}
        out.append(res)
    return out


def _team_segments(where: Path, man: dict) -> list:
    """The segments a machine's folder in the handoff copy publishes, in chain order: those its manifest lists, and a
    segment file it does not list (no entry)."""
    out = [{"name": e["name"], "kind": "day", "path": _unit_file(where, e["name"], published=True), "closed": True,
            "entry": e}
           for e in man["units"] if _SEGMENT.fullmatch(e["name"])]
    listed = {u["name"] for u in out}
    for f in sorted(where.glob("*/*.jsonl")):
        if _SEGMENT.fullmatch(f.name[:-6]) and _MONTH.fullmatch(f.parent.name) and f.name[:-6] not in listed:
            out.append({"name": f.name[:-6], "kind": "day", "path": f, "closed": True, "entry": None})
    return sorted(out, key=lambda u: _unit_key(u["name"]))


def _verify_team(root: Path) -> tuple:
    """Every copy in the handoff repository, in path order -> (a result per file, problems of no one file). A month
    published as daily files is one chain across its one-file copy (when it has one) and its days; each machine's
    published segments are one chain, checked against its manifest and reseal maps."""
    units, single, extra = {}, [], []
    for f in sorted(root.rglob("*.jsonl")) + sorted(root.rglob("*.held.json")):
        m = _DAY_PART.fullmatch(f.name)
        if _SEGMENT.fullmatch(f.name[:-6]) and _MONTH.fullmatch(f.parent.name):
            continue  # a published segment: its machine's chain below
        if m and _MONTH.fullmatch(f.parent.name) and m.group(1).startswith(f.parent.name + "-"):
            units[str(f.parent.parent / f"{f.parent.name}.jsonl")] = f.parent
        elif f.name.endswith(".jsonl"):
            single.append(f)
    units.update({str(f): f for f in single if str(f) not in units})
    out = []
    for key in sorted(units):
        u = units[key]
        out += _verify_parts(_parts(u.parent, u.name)) if u.is_dir() else [verify_file(u)]
    for where in sorted(d for d in root.iterdir() if d.is_dir()):
        man = _read_manifest(_manifest_file(where))
        segs = _team_segments(where, man)
        if segs or man["units"] or man.get("error"):
            res, more = _verify_trail(segs, man, where)
            out += res
            extra += more
    return out, extra


def _load_maps(folder: Path) -> tuple:
    """The reseal maps in `folder` -> ({old link: new link}, [(map file, reseal id, SHA-256 of the file)])."""
    bridges, maps = {}, []
    for f in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            raw = f.read_bytes()
            m = json.loads(raw)
        except (OSError, ValueError):
            maps.append((f, "", ""))
            continue
        for pair in (m.get("links") if isinstance(m, dict) else None) or []:
            if isinstance(pair, list) and len(pair) == 2 and all(isinstance(x, str) for x in pair):
                bridges[pair[0]] = pair[1]
        maps.append((f, str(m.get("id") or "") if isinstance(m, dict) else "", hashlib.sha256(raw).hexdigest()))
    return bridges, maps


def _problem(path, kind: str, text: str, line: int = 0, rid: str = "") -> dict:
    return {"path": str(path), "line": line, "id": rid, "kind": kind, "problem": text}


def _check_entry(res: dict, u: dict, chain: dict):
    """A closed unit against its manifest entry: every field the job wrote from the unit's bytes when it closed it
    (size, SHA-256, records and v1 lines, time span, records per day and per action, kind, chain head and link) but
    `file`, which no reader follows, and that the link is where its chain ends. Readers skip closed units by the
    time span and actions, so an edited index must fail here."""
    e = u["entry"]
    if not e or res["first"] and res["first"]["kind"] == "unreadable":
        return
    try:
        want = _entry(u["name"], read_trail(u["path"]))
    except _READ_ERRORS:
        return
    off = [k for k in want if k != "file" and e.get(k) != want[k]]
    if e.get("link") != seal(chain.get("last", ""), chain.get("tail", b"")) and "link" not in off:
        off.append("link")
    if off:
        res["checks"].append(_problem(u["path"], "manifest", f"{u['name']} differs from its manifest entry "
                                      f"({', '.join(off)}): the file or the manifest changed after it was closed"))


def _verify_trail(units: list, man: dict, where: Path) -> tuple:
    """One machine's trail: this Mac's units, or a machine's published segments -> (a result per unit, problems of
    no one unit). Month files are each their own chain; segments are one chain across, from the manifest's `start`
    (the units before it were pruned or not published), else its `origin`, else nothing. A closed unit must match its
    manifest entry, and a listed unit must exist. A link to a resealed unit's old end holds only through a reseal map
    (`where`/reseal/) that an audit.reseal record backs."""
    extra = []
    if man.get("error"):
        extra.append(_problem(_manifest_file(where), "unreadable", f"the manifest cannot be read: {man['error']}"))
    bridges, maps = _load_maps(where / "reseal")
    reseals, results, chain = {}, [], None
    start = man.get("start") if isinstance(man.get("start"), dict) else man.get("origin")
    for u in units:
        if u["kind"] == "day" and chain is None:
            chain = {"last": "", "tail": b"", "link": str(start.get("link") or "")} if isinstance(start, dict) else {}
        if u["closed"] and not u["path"].is_file():
            results.append({"path": str(u["path"]), "records": 0, "v1": 0, "checks": [], "notes": [],
                            "first": _problem(u["path"], "missing", f"{u['name']} is in the manifest, but its file is "
                                                                    "gone")})
            if u["kind"] == "day" and u["entry"]:
                chain = {"last": "", "tail": b"", "link": str(u["entry"].get("link") or "")}
            continue
        c = chain if u["kind"] == "day" else {}
        c.update(bridges=bridges, reseals=reseals, more=False)
        res = verify_file(u["path"], c)
        _check_entry(res, u, c)
        if u["closed"] and not u["entry"] and not res["first"]:
            res["checks"].append(_problem(u["path"], "manifest", f"{u['name']} is closed, but the manifest does not "
                                                                 "list it"))
        results.append(res)
        origin = man.get("origin") if isinstance(man.get("origin"), dict) else {}
        if u["kind"] == "month" and origin.get("unit") == u["name"] and not res["first"] and \
                not any(x is not u and x["name"] == u["name"] for x in units[:units.index(u)]):
            # the first unit of that name: a month file an earlier devkit wrote again later is a chain of its own
            try:
                data = read_trail(u["path"])
            except _READ_ERRORS:
                continue
            n = int(origin.get("bytes") or 0)
            if n > len(data) or seal(*_chain_end(data, n)) != origin.get("link"):
                extra.append(_problem(u["path"], "unlinked", f"the first segment links to {u['name']} as it was at "
                                      f"byte {n}, and the file there no longer ends that way"))
    for f, rid, sha in maps:
        if not rid or reseals.get(rid) != f"sha256:{sha}":
            extra.append(_problem(f, "reseal", f"no audit.reseal record backs the reseal map {f.name}: it was changed, "
                                               "or written by something other than dev audit reseal"))
    return results, extra


def _v2_count(data: bytes) -> int:
    return sum(1 for ln in data.splitlines() if _v2(ln))


def _anchor(f: Path, data: bytes, parts: list):
    """How far a local month file is anchored by what this machine published of it -> (offset, records of held
    days, problem or None). The parts must match the local bytes in order; the last one may be a copy published
    while the month was shorter."""
    off, held_n = 0, 0
    for kind, path in parts:
        if kind == "held":
            info = _held_info(path)
            n = info["bytes"] if info else 0
            if not info or hashlib.sha256(data[off:off + n]).hexdigest() != info["sha256"]:
                line = data.count(b"\n", 0, off) + 1
                return off, held_n, {"path": str(f), "line": line, "id": "", "kind": "diverged",
                                     "problem": f"the local file differs from what {core.tilde(path)} records of the "
                                                f"day it held, from line {line}: records were changed or removed "
                                                f"after that day was held"}
            off, held_n = off + n, held_n + int(info.get("records") or 0)
            continue
        try:
            p = path.read_bytes()
        except OSError as e:
            return off, held_n, {"path": str(path), "line": 0, "id": "", "kind": "unreadable", "problem": str(e)}
        if not data.startswith(p, off):
            line = data.count(b"\n", 0, off) + _diverges(data[off:], p)
            return off, held_n, {"path": str(f), "line": line, "id": "", "kind": "diverged",
                                 "problem": f"the local file differs from the published copy {core.tilde(path)} from "
                                            f"line {line}: records were changed or removed after they were published"}
        off += len(p)
    return off, held_n, None


def _diverges(local: bytes, published: bytes):
    """None when the local file extends the published copy, else the first local line that differs (1-based)."""
    if local.startswith(published):
        return None
    a, b = local.splitlines(), published.splitlines()
    n = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    return n + 1


def _anchors(local: list, out: dict) -> list:
    """This Mac's units against what it published (the anchor) -> problems; counts in out["unpublished"] the v2
    records not published yet. A month file must start with its published parts, in order; a closed segment must be
    the very segment the handoff copy's manifest lists."""
    problems, mine = [], team_dir()
    pub = {e["name"]: e for e in _read_manifest(_manifest_file(mine))["units"]}
    for u in local:
        if u["closed"] and not u["path"].is_file():
            continue
        e, p = u["entry"], pub.get(u["name"])
        if u["kind"] == "day" and e and p:
            if p.get("sha256") != e.get("sha256"):
                copy = core.tilde(_unit_file(mine, u["name"], published=True))
                problems.append(_problem(u["path"], "diverged", f"{u['name']} differs from its published copy {copy}: "
                                         "records were changed or removed after they were published"))
            continue
        try:
            data = read_trail(u["path"])
        except _READ_ERRORS:
            continue
        late = any(x["name"] == u["name"] for x in local[:local.index(u)])  # a month file written again after it closed
        if u["kind"] == "day" or late:
            out["unpublished"] += _v2_count(data)
            continue
        off, held_n, problem = _anchor(u["path"], data, _parts(mine, u["name"]))
        if problem:
            problems.append(problem)
        else:  # a held day's records are anchored nowhere, so they count as not yet published
            out["unpublished"] += held_n + _v2_count(data[off:])
    return problems


def verify(team: bool = True) -> dict:
    """Check this Mac's trail (each unit's chain, the chain across segments, each closed segment against the manifest,
    the reseal maps) and, with `team`, the handoff copies and that each local unit is what it published (the anchor).
    -> {ok, first, files, records, unpublished, problems, notes}; `unpublished`: v2 records chained but not yet in the
    published copy, so not yet anchored; `notes`: what the checks say that is no problem."""
    out = {"ok": True, "first": None, "files": [], "records": 0, "unpublished": 0 if team else None}
    man = _read_manifest(_manifest_file())
    local = _units(man=man)
    results, extra = _verify_trail(local, man, AUDIT)
    if team and (core.HANDOFF / "audit").exists():
        more, team_extra = _verify_team(core.HANDOFF / "audit")
        results += more
        extra += team_extra
    problems = []
    for r in results:
        out["files"].append(r)
        out["records"] += r["records"]
        problems += ([r["first"]] if r["first"] else []) + r["checks"]
    problems += extra
    if team:
        problems += _anchors(local, out)
    out["first"] = problems[0] if problems else None
    out["problems"] = problems
    out["notes"] = [n for f in out["files"] for n in f.get("notes") or []]
    out["ok"] = not problems
    return out


# --------------------------------------------------------------------------- publish and prune

# GitHub refuses a pushed file over 100 MB, and from then on every handoff push, so no file over this is ever
# published: each month goes out as one file per UTC day, and a single day over it is held (_held_day).
PUBLISH_LIMIT = 45 * 1024 * 1024
LIMIT_WHY = "GitHub refuses files over 100 MB, and then every handoff push"


def _mb(n: int) -> str:
    return core.human_kb(n / 1024)


def _pending(month: str, f: Path, size: int) -> dict:
    # a month over PUBLISH_LIMIT that the audit job has not published by day yet (the first job after the update)
    return {"month": month, "day": None, "path": str(f), "bytes": size, "limit": PUBLISH_LIMIT,
            "text": f"{month} is not published yet: {_mb(size)} here, over the {_mb(PUBLISH_LIMIT)} limit for one "
                    f"handoff file ({LIMIT_WHY}), so the audit job publishes it as one file per UTC day",
            "fix": "run dev jobs run audit to publish it now; until then its records stay in this machine's local "
                   "trail. If the job still leaves it, run dev audit verify: it names the published file the month "
                   "no longer extends"}


def _held_text(month: str, day: str, size: int) -> str:
    return (f"{day} is not published: {_mb(size)} that day, over the {_mb(PUBLISH_LIMIT)} limit for one handoff "
            f"file ({LIMIT_WHY}); the other days of {month} publish as usual")


def _held_entry(month: str, f: Path, day: str, size: int, records: int) -> dict:
    # one day over PUBLISH_LIMIT: what the job's record, dev audit stats and dev doctor say about it
    return {"month": month, "day": day, "path": str(f), "bytes": size, "records": records, "limit": PUBLISH_LIMIT,
            "text": _held_text(month, day, size),
            "fix": f"its {records} record(s) stay in this machine's local trail, where dev audit verify checks them; "
                   f"to see what filled that day, run dev audit stats --since {day} --until {day}"}


def _to_publish() -> list:
    """This Mac's last three month files of an earlier devkit, plain or closed, one unit per month -> [unit]. A
    segment is never over PUBLISH_LIMIT (SEGMENT_LIMIT is far under it), so only these are published by day."""
    months = {}
    for u in _units():
        if u["kind"] == "month":
            months.setdefault(u["name"], u)
    return [months[m] for m in sorted(months)[-3:]]


def _unit_size(u: dict) -> int:
    e = u.get("entry") or {}
    return int(e["bytes"]) if u["closed"] and isinstance(e.get("bytes"), int) else _filesize(u["path"])


def _day_of(line: bytes):
    """(the UTC day of a line's record, or None when it has no time; 1 when it is a v2 record, else 0)."""
    if not line.startswith(b"{"):
        return None, 0
    try:
        r = json.loads(line)
    except ValueError:
        return None, 0
    if not isinstance(r, dict):
        return None, 0
    v2 = 1 if r.get("v") == V and isinstance(r.get("hash"), str) else 0
    ts = r.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None, v2
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(ts)), v2
    except (OverflowError, OSError, ValueError):
        return None, v2


def _days(data: bytes, month: str, start: int = 0) -> list:
    """A month file's bytes from `start` on, one run per UTC day -> [(day, begin, end, v2 records)]. A run starts at
    the first record of a later day. Lines without a time, and records of an earlier day (a clock set back), stay in
    the run they follow, so the runs are contiguous and joined they give the bytes back, one chain. Days stay within
    the file's month, which is the local one: a record of a late evening can carry the next month's UTC date. Lines
    before the first record with a time join its run; with no such record there is no run yet."""
    y, m = int(month[:4]), int(month[5:7])
    lo, hi = f"{month}-01", f"{month}-{calendar.monthrange(y, m)[1]:02d}"
    runs, day, begin, n, pos = [], None, start, 0, start
    for line in data[start:].splitlines(keepends=True):
        d, v2 = _day_of(line)
        if d is not None:
            d = min(max(d, lo), hi)
            if day is None:
                day = d
            elif d > day:
                runs.append((day, begin, pos, n))
                day, begin, n = d, pos, 0
        n += v2
        pos += len(line)
    if day is not None:
        runs.append((day, begin, pos, n))
    return runs


def _chain_end(data: bytes, end: int):
    """(the hash of the last v2 record before byte `end`, the bytes after it up to `end`): where the chain stands."""
    e = end
    while e > 0:
        s = data.rfind(b"\n", 0, e - 1) + 1
        r = _v2(data[s:e])
        if r is not None:
            return r["hash"], data[e:end]
        e = s
    return "", data[:end]


def _held_day(data: bytes, day: str, begin: int, end: int, records: int) -> dict:
    """What the handoff copy keeps of a day over PUBLISH_LIMIT instead of its records: its size, count and SHA-256,
    which anchor it on this machine, and where the chain stands at its end, so verify goes on to the next day."""
    head, tail = _chain_end(data, end)
    return {"v": 1, "day": day, "bytes": end - begin, "records": records,
            "sha256": hashlib.sha256(data[begin:end]).hexdigest(), "head": head, "link": seal(head, tail),
            "tail_bytes": len(tail), "limit": PUBLISH_LIMIT}


def _write(target: Path, data: bytes):
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)


def _published_bytes(parts: list) -> int:
    total = 0
    for kind, path in parts:
        info = _held_info(path) if kind == "held" else None
        total += info["bytes"] if info else (path.stat().st_size if kind == "file" else 0)
    return total


def _held_days(u: dict, size: int) -> list:
    """The days of a month published by day that are over PUBLISH_LIMIT: those held, and those not published yet
    (the open day too), read from where the published files end, so dev doctor reads no more than that."""
    month, f = u["name"], u["path"]
    parts = _parts(team_dir(), month)
    out = []
    for kind, path in parts:
        info = _held_info(path) if kind == "held" else None
        if info:
            out.append(_held_entry(month, f, str(info.get("day") or path.name[:10]), info["bytes"],
                                   int(info.get("records") or 0)))
    done = _published_bytes(parts)
    if size - done <= PUBLISH_LIMIT:
        return out
    try:
        if u["closed"]:
            rest = read_trail(f)[done:]
        else:
            with open(f, "rb") as fh:
                fh.seek(done)
                rest = fh.read()
    except _READ_ERRORS:
        return out
    return out + [_held_entry(month, f, day, end - begin, n) for day, begin, end, n in _days(rest, month)
                  if end - begin > PUBLISH_LIMIT]


def held() -> list:
    """What the audit job leaves unpublished because of PUBLISH_LIMIT: a month over it that the job has not
    published by day yet, and each day over it."""
    if not settings()["publish"]:
        return []
    out = []
    for u in _to_publish():
        size = _unit_size(u)
        if size < 0:
            continue
        if (team_dir() / u["name"]).is_dir():
            out += _held_days(u, size)
        elif size > PUBLISH_LIMIT:
            out.append(_pending(u["name"], u["path"], size))
    return out


def publish_checks() -> list:
    # dev doctor's rows (group, name, ok, detail, fix): one per month or day held, else one that passes; none when
    # publish is off
    if not settings()["publish"]:
        return []
    rows = [("Automation", "audit handoff copy", False, h["text"], h["fix"]) for h in held()]
    daily = [u["name"] for u in _to_publish() if (team_dir() / u["name"]).is_dir()]
    return rows or [("Automation", "audit handoff copy", True,
                     f"every handoff file under the {_mb(PUBLISH_LIMIT)} limit"
                     + (f"; {', '.join(daily)} published as one file per UTC day" if daily else ""), "")]


def _publish_days(u: dict, dest: Path, segments: bool = False) -> list:
    """Publish a month file of an earlier devkit as one file per UTC day in <dest>/YYYY-MM/, after the one-file copy
    an earlier devkit published (which stays as it is). Only closed days go out: a day is closed once a record of a
    later day follows it, once the month has ended here, or, for its last day, once this devkit writes segments
    (`segments`): a record an earlier devkit appends after that gets its day file written again. Each is written
    once; a day over PUBLISH_LIMIT is held, and its .held.json keeps the chain going for the days after it. A local
    file that no longer extends what was published is never published over it. -> files changed."""
    month, f = u["name"], u["path"]
    parts = _parts(dest, month)
    try:
        data = read_trail(f)
    except _READ_ERRORS:
        return []
    _, _, problem = _anchor(f, data, parts)
    if problem:
        record("audit.publish", core.tilde(f), f"not published: {problem['problem']}; run dev audit verify",
               "blocked", severity="critical")
        return []
    folder = dest / month
    folder.mkdir(parents=True, exist_ok=True)  # this month is published by day, even before its first day closes
    whole = dest / f"{month}.jsonl"
    runs = _days(data, month, whole.stat().st_size if whole.is_file() else 0)
    if runs and month >= time.strftime("%Y-%m") and not segments:
        runs.pop()  # the open day: a record may still join it
    changed = []
    for day, begin, end, n in runs:
        seg, stub = folder / f"{day}.jsonl", folder / f"{day}.held.json"
        if end - begin > PUBLISH_LIMIT:
            info = _held_day(data, day, begin, end, n)
            old = _held_info(stub)
            if old and (old.get("bytes"), old.get("sha256")) == (info["bytes"], info["sha256"]):
                continue
            _write(stub, (json.dumps(info, indent=1) + "\n").encode())
            changed.append(stub)
            record("audit.publish", core.tilde(f), _held_text(month, day, end - begin), "blocked",
                   severity="warning")
        elif not (seg.is_file() and seg.stat().st_size == end - begin):  # the anchor matched what is there
            _write(seg, data[begin:end])
            changed.append(seg)
    return changed


def _publish_segments(dest: Path, now: float) -> list:
    """Publish each closed segment of the last three months once, as <dest>/YYYY-MM/<name>.jsonl (plain, so an
    earlier devkit reads it), and <dest>/manifest.json with their entries. A published segment whose bytes differ
    from this Mac's is never written over: the job records audit.publish blocked and critical. -> files changed."""
    man = _read_manifest(_manifest_file())
    mf = _manifest_file(dest)
    tm = _read_manifest(mf)
    if tm.get("error"):
        record("audit.publish", core.tilde(mf), f"not published: {tm['error']}; run dev audit verify", "blocked",
               severity="critical")
        return []
    keep, pub, changed = months_back(now, 2), {e["name"]: e for e in tm["units"]}, []
    order = [e for e in man["units"] if _SEGMENT.fullmatch(e["name"])]
    for i, e in enumerate(order):
        name = e["name"]
        if name[:7] < keep:
            continue
        rel = f"{name[:7]}/{name}.jsonl"
        target = dest / rel
        if target.is_file():
            if hashlib.sha256(target.read_bytes()).hexdigest() != e.get("sha256"):
                record("audit.publish", core.tilde(target), f"not published over: {core.tilde(target)} differs from "
                       f"this Mac's closed segment {name}; run dev audit verify", "blocked", severity="critical")
                continue
        else:
            try:
                data = read_trail(_unit_file(AUDIT, name))
            except _READ_ERRORS:
                continue
            if hashlib.sha256(data).hexdigest() != e.get("sha256"):
                record("audit.publish", core.tilde(_unit_file(AUDIT, name)), f"not published: {name} "
                       "differs from its manifest entry; run dev audit verify", "blocked", severity="critical")
                continue
            _write(target, data)
            changed.append(target)
        if not pub and not tm.get("start"):  # the segments before this one are not in the handoff copy
            if i:
                tm["start"] = {"unit": order[i - 1]["name"], "link": str(order[i - 1].get("link") or "")}
            elif isinstance(man.get("start"), dict):  # nor here: prune removed them before the first publish
                tm["start"] = {"unit": str(man["start"].get("unit") or ""), "link": str(man["start"].get("link") or "")}
        pub[name] = {**e, "file": rel}
    if not pub:
        return changed
    new = dict(tm, units=list(pub.values()))
    if isinstance(man.get("origin"), dict):
        new["origin"] = man["origin"]
    if _manifest_bytes(new) != (mf.read_bytes() if mf.is_file() else b""):
        _write(mf, _manifest_bytes(new))
        changed.append(mf)
    return changed


def publish(now: float = None) -> list:
    """Publish this machine's last three months into the handoff repository (audit/<user>@<host>/): month files of
    an earlier devkit one file per UTC day (_publish_days), and each closed segment once (_publish_segments). The
    audit job closes what is done first (close). -> files changed."""
    if not settings()["publish"] or not AUDIT.exists():
        return []
    now = time.time() if now is None else now
    with _lock(".job.lock"):
        dest, changed = team_dir(), []
        segments = any(u["kind"] == "day" for u in _units())
        for u in _to_publish():
            changed += _publish_days(u, dest, segments)
        changed += _publish_segments(dest, now)
    return changed


def months_back(now: float, n: int) -> str:
    y, m = time.localtime(now)[:2]
    total = y * 12 + (m - 1) - n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _plan_prune(now: float, cfg: dict) -> dict:
    r = cfg["retention"]
    cut, tcut = months_back(now, r["local_months"]), months_back(now, r["team_months"])
    month = _MONTH
    local = [u for u in _units() if u["name"][:7] < cut] if AUDIT.exists() and not cfg["hold"] else []
    mine = team_dir()
    team = [f for f in sorted(mine.glob("*.jsonl")) if month.fullmatch(f.stem) and f.stem < tcut] \
        if mine.exists() and not cfg["hold"] else []
    if mine.exists() and not cfg["hold"]:  # the months published by day: every file of the month's folder
        team += [p for d in sorted(mine.iterdir()) if d.is_dir() and month.fullmatch(d.name) and d.name < tcut
                 for p in sorted(d.iterdir()) if p.is_file()]
    if not cfg["hold"]:
        team += [f for d in _former_team_dirs() for f in sorted(d.glob("*.jsonl"))
                 if month.fullmatch(f.stem) and f.stem < tcut and _this_macs_copy(f)]
    return {"keeps_from": cut, "team_keeps_from": tcut, "local": local, "team": team}


def _former_team_dirs() -> list:
    """This user's handoff folders under the hostnames this Mac carried (audit/<user>@<hostname>/), where devkit
    published before the machine id. A DHCP name (`mac-55`) may also be another Mac's folder: _this_macs_copy
    decides per file."""
    from . import identity
    a, m = actor(), identity.machine()
    root = core.HANDOFF / "audit"
    return [root / f"{a['user']}@{name}" for name in sorted(m.aliases)
            if name != m.id and (root / f"{a['user']}@{name}").is_dir()]


def _this_macs_copy(f: Path) -> bool:
    """Was this published month file this Mac's? A copy is a prefix of one Mac's own month file, so its last record
    tells: written under this Mac's id, or under a hostname this Mac carried at that time."""
    from . import identity
    try:
        lines = [ln for ln in f.read_bytes().splitlines() if ln.strip()]
        last = json.loads(lines[-1]) if lines else {}
    except (OSError, ValueError):
        return False
    a = last.get("actor") if isinstance(last, dict) else None
    if not isinstance(a, dict) or not isinstance(last.get("ts"), (int, float)):
        return False
    host = str(a.get("host") or "")
    return host == identity.machine().id or identity.was_this_mac(host, float(last["ts"]))


def prune(now: float = None) -> dict:
    """Remove this Mac's units (month files and segments) of months older than audit.retention.local_months and its
    handoff copies older than team_months (never under floor_months; nothing under audit.hold), and record what was
    removed (audit.prune). Each manifest drops their entries and keeps, as `start`, the link of the last segment
    removed, so the first one kept still verifies. A reseal map goes with the audit.reseal record that backs it, here
    and in the handoff copy (verify refuses a map that no record backs); the maps go first, so a prune that stops
    halfway leaves none behind. The handoff copies leave the working copy only: git history keeps them
    (GIT_HISTORY). -> {removed: [months], team: [paths, for the handoff commit], records, held, maps: [reseal ids]}"""
    now = now or time.time()
    cfg = settings()
    r = cfg["retention"]
    out = {"removed": [], "team": [], "records": 0, "held": cfg["hold"], "maps": []}
    if cfg["hold"]:
        record("audit.prune", "local and team copies", "audit.hold is on (a legal hold): nothing pruned", "ok")
        return out
    with _lock(".job.lock"):
        plan = _plan_prune(now, cfg)
        retired, team_maps = set(), []
        for where, files in ((AUDIT, [u["path"] for u in plan["local"]]), (team_dir(), plan["team"])):
            for rid in _reseal_ids(files):
                f = where / "reseal" / f"{rid}.json"
                try:
                    f.unlink()
                except OSError:
                    continue
                retired.add(rid)
                if where != AUDIT:
                    team_maps.append(f)
        out["maps"] = sorted(retired)
        gone = []
        for u in plan["local"]:
            e = u["entry"] or {}
            try:
                if "records" in e:
                    n, link = int(e["records"]) + int(e.get("v1") or 0), e.get("link")
                else:
                    data = read_trail(u["path"])
                    n, link = sum(1 for ln in data.splitlines() if ln.strip()), seal(*_chain_end(data, len(data)))
                u["path"].unlink()
            except _READ_ERRORS:
                continue
            gone.append((u, link))
            out["records"] += n
        out["removed"] = sorted({u["name"][:7] for u, _ in gone})
        with _lock():  # a writer's first segment writes the manifest's origin under it
            _forget(_manifest_file(), [(u["name"], link) for u, link in gone], private=True)
        pruned = []
        for f in plan["team"]:
            try:
                f.unlink()
            except OSError:
                continue
            pruned.append(f)
        out["team"] = pruned + team_maps
        names = {f.name[:-6] for f in pruned if f.name.endswith(".jsonl") and _SEGMENT.fullmatch(f.name[:-6])}
        if names and _manifest_file(team_dir()).is_file():
            mf = _manifest_file(team_dir())
            tm = _read_manifest(mf)
            _forget(mf, [(e["name"], e.get("link")) for e in tm["units"] if e["name"] in names], private=False)
            out["team"].append(mf)
        for d in sorted({f.parent for f in pruned if _MONTH.fullmatch(f.parent.name)}
                        | {u["path"].parent for u, _ in gone if _MONTH.fullmatch(u["path"].parent.name)}):
            with contextlib.suppress(OSError):
                d.rmdir()  # a month's folder, once its days are gone
    if out["removed"] or out["team"] or out["maps"]:
        parts = []
        if out["removed"]:
            parts.append(f"removed {len(gone)} file(s), {out['records']} record(s) here: "
                         f"{', '.join(out['removed'])}; kept {r['local_months']} month(s) (from {plan['keeps_from']})")
        if pruned:
            months = sorted({f.name[:7] for f in pruned})
            parts.append(f"removed {len(pruned)} handoff file(s) of {', '.join(months)}; "
                         f"kept {r['team_months']} month(s) (from {plan['team_keeps_from']})")
        if out["maps"]:
            parts.append(f"retired reseal map(s) {', '.join(out['maps'])}: the audit.reseal record that backs each one "
                         "was pruned")
        record("audit.prune", "local and team copies", "; ".join(parts), "ok", count=out["records"])
    return out


_RESEAL_ID = re.compile(r"[0-9A-Z]{26}")  # a reseal's id, which names its map: a ULID


def _reseal_ids(files) -> list:
    """The ids of the reseal maps that the audit.reseal records in these files back, in order."""
    ids = []
    for f in files:
        try:
            data = read_trail(f)
        except _READ_ERRORS:
            continue
        for ln in data.splitlines() if b'"audit.reseal"' in data else ():
            try:
                rec = json.loads(ln) if b'"audit.reseal"' in ln else None
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("action") != "audit.reseal" or rec.get("outcome") != "ok":
                continue
            ref = rec.get("ref") if isinstance(rec.get("ref"), dict) else {}
            rid = str(ref.get("id") or "")
            if _RESEAL_ID.fullmatch(rid) and rid not in ids:
                ids.append(rid)
    return ids


def _forget(mf: Path, gone: list, private: bool):
    """Drop the pruned units [(name, link)] from a manifest, and keep as its `start` the last pruned segment and its
    link, which the first segment kept links to."""
    if not gone or not mf.is_file():
        return
    m = _read_manifest(mf)
    if m.get("error"):
        return
    names = {n for n, _ in gone}
    m["units"] = [e for e in m["units"] if e["name"] not in names]
    segs = sorted(((n, link) for n, link in gone if _SEGMENT.fullmatch(n)), key=lambda p: _unit_key(p[0]))
    old = m.get("start") if isinstance(m.get("start"), dict) else {}
    if segs and _unit_key(segs[-1][0]) > _unit_key(str(old.get("unit") or "0")):
        m["start"] = {"unit": segs[-1][0], "link": str(segs[-1][1] or "")}
    _write_manifest(mf, m, private)


def _size(files) -> int:
    total = 0
    for f in files:
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total


# what team_months really removes, said wherever retention is shown
GIT_HISTORY = ("git history keeps every published month until the handoff repository's history is rewritten: "
               "pruning removes month files from its working copy only")


def config_report(now: float = None) -> dict:
    """`dev audit config`: the effective settings, where records live, their sizes and the next prune."""
    now = now or time.time()
    cfg = settings()
    local = [u["path"] for u in _units() if u["path"].is_file()]  # open and closed segments, month files
    mine = team_dir()
    team = sorted(mine.rglob("*.jsonl")) if mine.exists() else []  # one-file copies and the days of each month
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
                           "would_remove": sorted({u["name"][:7] for u in plan["local"]}),
                           "would_remove_team": [f.stem for f in plan["team"]]}}


# --------------------------------------------------------------------------- reseal
#
# Records sealed before today's cleaning rules can hold what the rules now keep out (message text, home folders, free
# text of a command line); reading cleans them, and `dev audit reseal --apply` rewrites them on disk. It verifies
# first and refuses a trail that does not verify, since a rewrite would hide what broke it. It writes the reseal map,
# reseal/<id>.json, before it rewrites anything: each rewritten unit's old and new size, SHA-256, chain head and
# link, and the ids it changed. A unit's records are chained again from the first change; a segment keeps its first
# link, to the old end of the unit before it, and verify accepts such a link only through a map that an audit.reseal
# record backs (its evidence.check is the map's SHA-256). This Mac's handoff copy is rewritten to match; git history
# keeps the old lines until the handoff repository's history is purged, which is the developer's call.

RESEAL_AGENT = ("dev audit reseal --apply rewrites the stored audit trail: run it from your own terminal, not an "
                "agent's session")

# the fields devkit's own controls read back from stored records: the embargo check (branches.embargo_trails), which
# reseal maps a record backs (verify and prune), and where a pruned chain starts (verify). A reseal never rewrites
# them, whatever audit.redact.extra matches: that list includes the team policy, which any teammate can commit, and
# a pattern must not lift an embargo or unhook the chain's anchors. None: every field of the record.
_CONTROLS = {"git.embargo": ("branch", "target", "detail", "project", "evidence.commit"),
             "audit.reseal": ("ref.id", "evidence.check"), "audit.prune": None}


def _keep_controls(stored: dict, clean: dict) -> dict:
    """`clean` with the fields devkit's controls read put back as `stored` holds them (_CONTROLS)."""
    action = stored.get("action")
    if action not in _CONTROLS:
        return clean
    if _CONTROLS[action] is None:
        return dict(stored)
    out = dict(clean)
    for f in _CONTROLS[action]:
        group, _, key = f.partition(".")
        if not key:
            if group in stored:
                out[group] = stored[group]
        elif isinstance(stored.get(group), dict) and key in stored[group]:
            out[group] = {**(out.get(group) if isinstance(out.get(group), dict) else {}), key: stored[group][key]}
    return out


def _reclean_v1(r: dict, rx) -> dict:
    """A v1 line as today's rules store its text, in its own shape: what lift shows of it, kept on disk."""
    action, out = str(r.get("action") or ""), {}
    for k, v in r.items():
        if k in ("target", "detail") and isinstance(v, (str, list, tuple)):
            out[k] = _field(action, k, v, rx, r)
        elif not isinstance(v, str) or k in _V1_CORE:
            out[k] = v
        elif EXTRAS.get(k) in ("run", "step"):
            out[k] = _run(v) if EXTRAS[k] == "run" else _step(v)
        elif EXTRAS.get(k) == "evidence.command":
            out[k] = _command(v, rx)
        else:
            out[k] = _clean(v, rx)[:VALUE_CUT]
    if action in DECISION_ACTIONS:  # v1 kept no labels: an answer stays only as its length
        if action == "crew.decided" and isinstance(out.get("detail"), str) and out["detail"]:
            out["detail"] = _option(out["detail"], [])
        for k in _ANSWERS:
            if isinstance(out.get(k), str) and out[k]:
                out[k] = _option(out[k], [])
    return out


def _reseal_bytes(data: bytes, kind: str, rx) -> tuple:
    """A unit's bytes with each record cleaned by today's rules -> (new bytes, ids of the v2 records changed, v1 lines
    changed, [(old line length, new line length)]). Every v2 record from the first change on is chained again. A
    segment's first record keeps its link to the unit before it; a month file's chain starts at its first line."""
    out, ids, v1n, lens = [], [], 0, []
    last, tail, first, changed = "", b"", True, False
    for raw in data.splitlines(keepends=True):
        body = raw.rstrip(b"\r\n")
        end = raw[len(body):] or b"\n"
        try:
            r = json.loads(body) if body.strip() else None
        except ValueError:
            r = None
        new = raw
        if isinstance(r, dict) and r.get("v") == V and isinstance(r.get("hash"), str):
            clean = _reclean(r, rx)
            clean.pop("cleaned", None)
            clean = _keep_controls(r, clean)
            want = r.get("prev") if first and kind == "day" else seal(last, tail)
            first = False
            if clean != r:
                ids.append(str(r.get("id") or ""))
            if clean != r or changed or want != r.get("prev"):
                changed = True
                clean["prev"], clean["hash"] = want, None
                clean["hash"] = digest(clean)
                new = json.dumps(clean, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + end
                last = clean["hash"]
            else:
                last = r["hash"]
            tail = b""
        else:
            if isinstance(r, dict):
                clean = _keep_controls(r, _reclean_v1(r, rx))
                if clean != r:
                    v1n, changed = v1n + 1, True
                    new = json.dumps(clean, ensure_ascii=False).encode("utf-8") + end
            tail += new
        out.append(new)
        lens.append((len(raw), len(new)))
    return b"".join(out), ids, v1n, lens


def _offsets(lens: list) -> dict:
    """{old offset of each line's start and of the end: its new offset}."""
    om, a, b = {0: 0}, 0, 0
    for x, y in lens:
        a, b = a + x, b + y
        om[a] = b
    return om


def _reseal_plan(rx, closed=None) -> list:
    """Each unit of this Mac whose records today's rules change -> [{unit, old, new, ids, lines, lens}]; `closed`:
    only the closed units (True) or only the plain ones (False)."""
    work, names = [], set()
    for u in _units():
        late = u["name"] in names  # a month file written again after it closed: published nowhere
        names.add(u["name"])
        if (closed is not None and u["closed"] != closed) or (u["closed"] and not u["path"].is_file()):
            continue
        try:
            old = read_trail(u["path"])
        except _READ_ERRORS:
            continue
        new, ids, v1n, lens = _reseal_bytes(old, u["kind"], rx)
        if new != old:
            work.append({"unit": u, "old": old, "new": new, "ids": ids, "lines": v1n, "lens": lens, "late": late})
    return work


def _ends(data: bytes) -> dict:
    head, tail = _chain_end(data, len(data))
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "head": head, "link": seal(head, tail)}


def _team_parts(u: dict, dest: Path) -> list:
    """The files of this Mac's handoff copy that hold a slice of unit `u` -> [(kind, path)]."""
    if u["kind"] == "month":
        return _parts(dest, u["name"])
    pub = {e["name"]: e for e in _read_manifest(_manifest_file(dest))["units"]}
    e = pub.get(u["name"])
    f = _unit_file(dest, u["name"], published=True)
    return [("file", f)] if e and f.is_file() else []


def _reseal_team(w: dict, dest: Path) -> list:
    """Rewrite this Mac's published copies of one resealed unit to match it, slice by slice. -> files changed."""
    if w.get("late"):
        return []
    u, old, new, om, changed = w["unit"], w["old"], w["new"], _offsets(w["lens"]), []
    off = 0
    for kind, path in _team_parts(u, dest):
        info = _held_info(path) if kind == "held" else None
        n = (info["bytes"] if info else 0) if kind == "held" else _filesize(path)
        a, b = off, off + n
        off = b
        if a not in om or b not in om:
            continue  # not a whole slice of lines: verify reports the copy as diverged
        part = new[om[a]:om[b]]
        if kind == "held":
            if info and part != old[a:b]:
                held_info = _held_day(new, str(info.get("day") or path.name[:10]), om[a], om[b], _v2_count(part))
                _write(path, (json.dumps(held_info, indent=1) + "\n").encode())
                changed.append(path)
        elif part != path.read_bytes():
            _write(path, part)
            changed.append(path)
    if u["kind"] == "day":
        mf = _manifest_file(dest)
        tm = _read_manifest(mf)
        hit = [e for e in tm["units"] if e["name"] == u["name"]]
        if hit and not tm.get("error"):
            hit[0].update({**_entry(u["name"], new), "file": f"{u['name'][:7]}/{u['name']}.jsonl"})
            _write(mf, _manifest_bytes(tm))
            changed.append(mf)
    return changed


def _moved_origin(m: dict, work: list):
    """A manifest's `origin` when it is in a resealed month file -> (the same point in its new bytes, [old link, new
    link] to bridge the link the first segment still names), or (None, None) when the reseal leaves it as it is."""
    o = m.get("origin") if isinstance(m.get("origin"), dict) else None
    w = next((w for w in work if o and w["unit"]["name"] == o.get("unit") and not w.get("late")), None)
    om = _offsets(w["lens"]) if w else {}
    n = int(o.get("bytes") or 0) if o else 0
    if not w or n not in om:
        return None, None
    link = seal(*_chain_end(w["new"], om[n]))
    return {"unit": o["unit"], "bytes": om[n], "link": link}, ([str(o.get("link") or ""), link]
                                                               if link != o.get("link") else None)


def _rewrite_in_place(fd: int, data: bytes):
    os.lseek(fd, 0, os.SEEK_SET)
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]
    os.ftruncate(fd, len(data))
    os.fsync(fd)


def reseal(apply: bool = False, now: float = None) -> dict:
    """`dev audit reseal`: what a reseal would rewrite (`apply` false), or the reseal itself, which runs only from the
    developer's own terminal and only on a trail that verifies. -> {ok, refused?, units: [{name, path, records,
    lines, team}], map, id, changed: [handoff files written]}."""
    from . import backup
    now = time.time() if now is None else now
    out = {"ok": True, "units": [], "map": "", "id": "", "changed": []}
    # the developer's own terminal: a terminal on stdin, and no agent marker or agent among the parents. The actor's
    # `agent` label is not proof: a session sets it with DEVKIT_AGENT
    if apply and not backup.developer_here():
        record("audit.reseal", "local trail", RESEAL_AGENT, "blocked")
        return {**out, "ok": False, "refused": RESEAL_AGENT}
    rx, dest = _redact_rules(), team_dir()
    with _lock(".job.lock"):
        res = verify(team=True)
        if not res["ok"]:
            p = res["first"]
            why = (f"the chain does not verify ({p['kind']} at {core.tilde(p['path'])}"
                   + (f":{p['line']}" if p.get("line") else "") + "): a reseal would hide it; run dev audit verify "
                   "and settle what it reports first")
            if apply:
                record("audit.reseal", "local trail", why, "blocked", severity="critical")
            return {**out, "ok": False, "refused": why}
        if not apply:
            out["units"] = [{"name": w["unit"]["name"], "path": str(w["unit"]["path"]), "records": len(w["ids"]),
                             "lines": w["lines"], "team": 0 if w["late"] else len(_team_parts(w["unit"], dest))}
                            for w in _reseal_plan(rx)]
            return out
        # closed units change only under the job lock, held here: they are planned and compressed before writers wait
        closed = _reseal_plan(rx, closed=True)
        for w in closed:
            w["gz"] = gzip.compress(w["new"], 6, mtime=0)
        with _lock():
            held = []
            for u in _units():  # an earlier devkit's writer waits on its month file's lock, then appends after
                if not u["closed"]:
                    fd = os.open(u["path"], os.O_RDWR)
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    held.append((u["path"], fd))
            try:
                work = sorted(closed + _reseal_plan(rx, closed=False), key=lambda w: _unit_key(w["unit"]["name"]))
                if work:
                    out.update(_reseal_apply(work, dict(held), dest, now))
            finally:
                for _, fd in held:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)
    if not out["units"]:
        return out
    n, lines = sum(u["records"] for u in out["units"]), sum(u["lines"] for u in out["units"])
    record("audit.reseal", f"{len(out['units'])} file(s)", f"re-sealed {n} record(s) and {lines} older line(s) with "
           f"today's rules; the old chain heads are in {core.tilde(out['map'])}", "ok", count=n,
           check=f"sha256:{hashlib.sha256(Path(out['map']).read_bytes()).hexdigest()}", id=out["id"])
    close(now, force=True)  # the record publishes at once, so every machine can check the map
    out["changed"] += publish(now)
    return out


def _reseal_apply(work: list, fds: dict, dest: Path, now: float) -> dict:
    """Write the reseal map, then each unit and this Mac's handoff copy of it (under both locks of the folder and
    each plain file's own). -> {units, map, id, changed}."""
    rid = new_id(now)
    links, rows, team = [], [], []
    for w in work:
        u = w["unit"]
        before, after = _ends(w["old"]), _ends(w["new"])
        if before["link"] != after["link"]:
            links.append([before["link"], after["link"]])
        rows.append({"name": u["name"], "kind": u["kind"], "records": len(w["ids"]), "lines": w["lines"],
                     "ids": w["ids"], "old": before, "new": after})
    man = _read_manifest(_manifest_file())
    origin, pair = _moved_origin(man, work)
    if pair:
        links.append(pair)
    rmap = {"v": 1, "id": rid, "time": iso(now), "devkit": core.VERSION, "units": rows, "links": links}
    data = (json.dumps(rmap, indent=1, sort_keys=True) + "\n").encode()
    path = AUDIT / "reseal" / f"{rid}.json"
    _write_file(path, data)  # the old heads are kept before anything is rewritten
    for w in work:
        u = w["unit"]
        if u["closed"]:
            _write_file(u["path"], w["gz"])
            man["units"] = [{**e, **_entry(u["name"], w["new"])} if e["name"] == u["name"]
                            else e for e in man["units"]]
        else:
            _rewrite_in_place(fds[u["path"]], w["new"])
    if origin:
        man["origin"] = origin
    _write_manifest(_manifest_file(), man)
    if dest.is_dir():
        for w in work:
            team += _reseal_team(w, dest)
        mf = _manifest_file(dest)
        tm = _read_manifest(mf)
        moved, _ = _moved_origin(tm, work)
        if moved and not tm.get("error"):
            tm["origin"] = moved
            _write(mf, _manifest_bytes(tm))
            team.append(mf)
        _write(dest / "reseal" / f"{rid}.json", data)
        team.append(dest / "reseal" / f"{rid}.json")
    units = [{"name": r["name"], "path": str(w["unit"]["path"]), "records": r["records"], "lines": r["lines"],
              "team": 0 if w["late"] else len(_team_parts(w["unit"], dest))} for r, w in zip(rows, work)]
    return {"units": units, "map": str(path), "id": rid, "changed": sorted(set(team))}


# --------------------------------------------------------------------------- export

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


def _hostname(a: dict) -> str:
    """The host a SIEM shows for a record's actor: its hostname of the time (`label`), or `host` for a record from
    before the machine id, which named the Mac by its hostname."""
    return str(a.get("label") or a.get("host") or "")


def _device_id(a: dict) -> str:
    """The record's machine id, for the SIEM's device id field; "" for a record that names a hostname only."""
    from . import identity
    host = str(a.get("host") or "")
    return host if identity.MACHINE_ID.fullmatch(host) else ""


def to_ocsf(r: dict) -> dict:
    """One record as an OCSF Base Event (class 0, activity Other); devkit's own fields are under `unmapped`. The
    device is the hostname of the time, and its uid the machine id when the record has one."""
    sid, sname = _OCSF_STATUS.get(r.get("outcome"), (0, "Unknown"))
    a = r.get("actor") or {}
    sev = r.get("severity", "info")
    device = {"hostname": _hostname(a), "type_id": 0}
    if _device_id(a):
        device["uid"] = _device_id(a)
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
            "device": device,
            "unmapped": {k: r[k] for k in ("category", "project", "branch", "run", "step", "crew", "target", "detail",
                                           "why", "evidence", "ref", "prev", "hash", "v", "cleaned")
                         if r.get(k) not in (None, "")}}


def _cef_head(s) -> str:
    return str(s or "").replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _cef_ext(s) -> str:
    return str(s or "").replace("\\", "\\\\").replace("=", "\\=").replace("\r", "\\r").replace("\n", "\\n")


def to_cef(r: dict) -> str:
    a, c = r.get("actor") or {}, r.get("crew") or {}
    ext = [("rt", int(round((r.get("ts") or 0) * 1000))), ("suser", a.get("user")), ("shost", _hostname(a)),
           ("deviceExternalId", _device_id(a)),
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
    return f"<{pri}>{stamp} {_hostname(r.get('actor') or {}) or 'localhost'} devkit: {to_cef(r)}".encode("utf-8")


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

COMMANDS = ("", "verify", "export", "show", "stats", "config", "reseal")


def add_arguments(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    p.add_argument("audit_cmd", nargs="?", default="", metavar="verify|export|show|stats|config|reseal",
                   help="verify the chain, export records, show one record, count them, show the settings, or "
                        "re-seal records sealed under older cleaning rules")
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
    p.add_argument("-n", "--lines", type=int, default=60,
                   help="table: show the newest N matches (default 60); the header counts every match, and json, "
                        "csv and export return them all")
    p.add_argument("--apply", action="store_true",
                   help="reseal: rewrite the records, from your own terminal (without it, show what it would change)")
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
        close()
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
        ui.err(f"unknown audit command “{act}”: dev audit [verify|export|show <id>|stats|config|reseal]")
        return 2
    try:
        if act == "verify":
            return _cmd_verify(a, ui)
        if act == "reseal":
            return _cmd_reseal(a, ui)
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
        fmt = "json" if a.json else (a.format or "table")
        if act != "stats" and fmt not in ("table", "json", "csv"):
            ui.err("--format takes table, json or csv (export: dev audit export --format jsonl|csv|ocsf|cef)")
            return 2
        if act != "stats" and fmt == "table":  # the newest -n rows, and the count of every match
            total, recs = latest(a.lines, **_filters(a))
        else:
            recs = query(**_filters(a))
            total = len(recs)
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
    if fmt == "json":
        print(json.dumps(recs, indent=1, ensure_ascii=False))
        return 0
    if fmt == "csv":
        print(to_csv(recs), end="")
        return 0
    f = _filters(a)
    span = f"last {f['days']:g} days" if f["days"] else " – ".join(x for x in (a.since or "", a.until or "") if x)
    print(ui.section(f"Audit · {span}" + (" · team" if a.team else "") + f" · {total} record(s)"))
    for r in recs:
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
    for n in res["notes"]:
        print(f"  {ui.WARN} {core.tilde(n['path'])}" + (f" line {n['line']}" if n["line"] else "") + f": {n['note']}")
    record("audit.verify", f"{len(res['files'])} file(s)", "the chain holds" if res["ok"] else
           f"{res['first']['kind']} at {core.tilde(res['first']['path'])}:{res['first']['line']}",
           "ok" if res["ok"] else "failed", count=res["records"],
           **({} if res["ok"] else {"severity": "critical"}))
    return 0 if res["ok"] else 1


def _cmd_reseal(a, ui) -> int:
    res = reseal(apply=a.apply)
    if not res["ok"]:
        ui.err(res["refused"])
        return 1
    for u in res["units"]:
        print(f"  {core.tilde(u['path'])}  {u['records']} record(s), {u['lines']} older line(s)"
              + (" re-sealed" if a.apply else " to re-seal"))
    team = sum(u["team"] for u in res["units"])
    if not res["units"]:
        print(f"{ui.OK} nothing to re-seal: every stored record already follows today's rules")
        return 0
    if not a.apply:
        if team:
            print(f"  {team} file(s) of this Mac's handoff copy follow them")
        print("Nothing changed. To re-seal, run from your own terminal: dev audit reseal --apply")
        return 0
    if res["changed"]:
        from . import work
        work._commit(res["changed"], f"audit: reseal from {actor()['host']}")
    print(f"{ui.OK} re-sealed; the old chain heads are in {core.tilde(res['map'])}")
    print(ui.dim("  git history still holds the old lines until the handoff repository's history is purged "
                 "(docs/audit.md, What git keeps)"))
    return 0


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
    print(ui.dim("\n  Change audit.* with dev config set or dev config edit. The team policy "
                 "(policy/audit.json in the handoff repository) can only record more or keep longer."))
    return 0
