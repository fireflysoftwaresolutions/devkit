# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Lesson rules: warnings made from a lesson that keeps repeating.

A rule names one lesson and one pattern. A call rule matches the shell commands an agent runs; an output rule matches
the text a tool printed. A rule only warns: the call runs, and the PreToolUse hook adds the lesson's title and
prevention to its context (lesson_rule_match, lesson_rule_context). A hit is counted on this machine (record_hit).

A rule file written by an older devkit may say `block`; it warns like any other (its `stored_mode` keeps what the file
says). And no rule may match devkit, `git status` or a file tool (PROBES), where it would warn on every call: load()
ignores it and lists it in PROBLEMS.

Rules live in the handoff repository, so the team shares them: lessons/rules/<scope>.json, one file per lesson scope
(`acme/api`, `global`, `initiatives/<slug>`), changed as state.update changes a record (lock, re-read, atomic replace;
the lock stays in the state folder, out of the shared repository) and committed like a lesson:

    {"version": 1, "rules": [{"id": "LR-…", "lesson": "L-…", "kind": "call", "match": "python3 -c", "mode": "warn",
                              "created": "…", "created_by": "…", "history": ["…"]}]}

The id is "LR-" and 10 hex characters of a hash of the rule's scope, lesson, kind and pattern, so the same rule has
the same id on every machine, and a pattern edited by hand no longer matches its id: load() ignores it.

Patterns are never regular expressions. A call pattern is a sequence of command words, matched as consecutive words
of the command, with `*` inside a word for any characters (`git push --force*`) and a word that is only `*` for any
one word. Its first word is literal, so every rule names a program. An output pattern is literal text, matched without
case. A synced file therefore cannot make devkit backtrack or run code, and a file or rule that fails validation is
ignored and listed in PROBLEMS, so a teammate's bad rule never breaks a hook.

The text an agent reads comes from the lesson itself, at the moment the rule fires: a lesson that is held by the
screen, superseded, retired or gone turns its rule off.

    propose(lesson, match="", error="") -> rule      validate, then write (ValueError with the reason)
    load(scopes) -> [rule]                           the valid rules of these scopes; PROBLEMS the rest
    check_call(command, scopes) -> ("", None) | ("warn", rule)
    check_output(text, scopes) -> [rule]             the output rules the text shows
    record_hit(rule_id, missed) -> None              count a hit, and an output rule's miss, on this machine
    describe(rule) -> str                            the lesson's title and prevention, capped
    lesson_rule_match(tool, tool_input, cwd) -> {}   the rule a tool call hits, kept for the hook
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import time
import unicodedata
from pathlib import Path

from . import core, lessons, state
from .core import STATE

VERSION = 1
KINDS = ("call", "output")
MODES = ("warn",)  # what a new rule does
_FILE_MODES = ("warn", "block")  # what a rule file may say: an older devkit wrote `block`, which now warns
HITS = STATE / "lessons-rule-hits.json"  # local counts: hits and misses per rule, never synced
WINDOW_DAYS = 14  # a proposed pattern must match none of the commands that succeeded here in this window

MAX_FILE_BYTES = 256 * 1024
MAX_RULES = 200  # per file; the rest are ignored and reported
PATTERN_CHARS, PATTERN_WORDS, WORD_CHARS = 200, 12, 80
ERROR_CHARS = 200
COMMAND_CHARS, COMMAND_WORDS = 20_000, 4_000
OUTPUT_CHARS = 50_000
TITLE_CHARS, PREVENTION_CHARS = 120, 300
NEST = 2  # shell levels read inside `sh -c "…"` and `eval`

_ID = re.compile(r"LR-[0-9a-f]{10}")
_LESSON_ID = re.compile(r"L-[0-9A-Za-z]{1,40}")
_SCOPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,99}){0,2}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_OPERATORS = frozenset(";|&()<>")
_SHELLS = frozenset(("sh", "bash", "zsh", "dash", "ksh", "fish"))
_BACKTICKS = re.compile(r"`([^`\n]{1,400})`")

# Calls no rule may match: devkit itself, a look at the repository, and the agents' file tools, where a rule would warn
# on every call (a rule only reads shell commands, but a pattern that names a file tool would still say the wrong
# thing).
PROBES = ("dev", "dev status", "dev help", "dev lesson ls", "dev progress note working", "dev crew ask question",
          "dev watch --wait", "git status", "git status --short", "git diff", "git log --oneline", "ls", "pwd",
          "cat README.md") + tuple(f"{t}{a}" for t in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Glob", "Grep",
                                                      "LS", "apply_patch") for a in ("", " README.md"))

PROBLEMS: list = []  # what the last load() ignored, one line each: "<file>: <why>"


# --------------------------------------------------------------------------- words and patterns

def words(command: str, depth: int = 0) -> list:
    """A command's words as the shell splits them: quotes join, `;`, `&&`, `|` and the other operators stand alone, a
    comment is dropped. The command a shell runs from `sh -c "…"` or `eval …` is read too (NEST levels), after a `;`
    so a pattern never matches across two commands. Text that does not tokenize falls back to splitting on spaces."""
    text = str(command or "")[:COMMAND_CHARS]
    try:
        lx = shlex.shlex(text, posix=True, punctuation_chars=True)
        lx.whitespace_split = True
        out = list(lx)
    except ValueError:
        out = text.split()
    out = out[:COMMAND_WORDS]
    if depth >= NEST:
        return out
    inner = []
    for i, w in enumerate(out):
        base = os.path.basename(w)
        if base in _SHELLS:
            j = i + 1
            while j < len(out) and out[j].startswith("-") and not _runs_text(out[j]):
                j += 1
            if j + 1 < len(out) and _runs_text(out[j]):
                inner += [";"] + words(out[j + 1], depth + 1)
        elif w == "eval" and i + 1 < len(out):
            end = next((k for k in range(i + 1, len(out)) if out[k] and set(out[k]) <= _OPERATORS), len(out))
            inner += [";"] + words(" ".join(out[i + 1:end]), depth + 1)
    return (out + inner)[:COMMAND_WORDS]


def _runs_text(option: str) -> bool:
    """Is this a shell's `-c` option (`-c`, `-lc`, `-ec`), which runs the next word as a command?"""
    return option.startswith("-") and not option.startswith("--") and "c" in option[1:]


def _glob(pat: str, word: str) -> bool:
    """Does `word` match `pat`, where `*` stands for any characters? Two pointers, no regular expression: linear in
    practice and never worse than len(pat) * len(word)."""
    p = w = 0
    star, mark = -1, 0
    while w < len(word):
        if p < len(pat) and pat[p] != "*" and pat[p] == word[w]:
            p, w = p + 1, w + 1
        elif p < len(pat) and pat[p] == "*":
            star, mark, p = p, w, p + 1
        elif star >= 0:
            p, mark = star + 1, mark + 1
            w = mark
        else:
            return False
    while p < len(pat) and pat[p] == "*":
        p += 1
    return p == len(pat)


def _first_hits(first: str, word: str) -> bool:
    return word == first or ("/" not in first and os.path.basename(word) == first)


def _at(pattern: list, ws: list, i: int) -> bool:
    if i + len(pattern) > len(ws) or not _first_hits(pattern[0], ws[i]):
        return False
    return all(_glob(p, ws[i + k]) for k, p in enumerate(pattern[1:], 1))


def runs(pattern: list, command_words: list) -> bool:
    """Does a command (its words) run this pattern (its words)? The pattern's words are consecutive command words;
    its first word is literal and also matches a program named by its path (`/usr/bin/python3` for `python3`)."""
    return bool(pattern) and any(_at(pattern, command_words, i) for i in range(len(command_words)))


def _pattern_words(match: str) -> list:
    try:
        return shlex.split(match)
    except ValueError:
        return match.split()


def pattern_problem(match: str) -> str:
    """Why a call pattern cannot be a rule, or "". Shape only: propose() checks it against the lesson and the
    commands that succeeded here."""
    if not isinstance(match, str) or not match.strip():
        return "the pattern is empty"
    if len(match) > PATTERN_CHARS:
        return f"the pattern is longer than {PATTERN_CHARS} characters"
    if _CONTROL.search(match):
        return "the pattern holds a control character or a new line"
    ws = _pattern_words(match.strip())
    if not ws or len(ws) > PATTERN_WORDS:
        return f"a pattern has 1 to {PATTERN_WORDS} words"
    if any(not w or len(w) > WORD_CHARS for w in ws):
        return f"each word of the pattern has 1 to {WORD_CHARS} characters"
    first = ws[0]
    if "*" in first or first.startswith("-") or set(first) & _OPERATORS:
        return "the first word of a pattern is a program, written out: no `*`, option or operator"
    literal = [w for w in ws if w.strip("*")]
    from . import lessonlint
    gate = lessonlint.check_anchors({"commands": [" ".join(shlex.quote(w) for w in literal)]})
    if gate:
        return gate[0]
    return ""


def error_problem(error: str) -> str:
    if not isinstance(error, str) or not error.strip():
        return "the error text is empty"
    if len(error) > ERROR_CHARS:
        return f"the error text is longer than {ERROR_CHARS} characters"
    if _CONTROL.search(error):
        return "the error text holds a control character or a new line"
    from . import lessonlint
    gate = lessonlint.check_anchors({"errors": [error]})
    return gate[0] if gate else ""


# --------------------------------------------------------------------------- files

def rules_dir() -> Path:
    return Path(lessons.LESSONS) / "rules"


def rule_file(scope: str) -> Path:
    """lessons/rules/<scope>.json. Raises ValueError for a scope that is not a lesson scope (`..`, an absolute path)."""
    if not isinstance(scope, str) or not _SCOPE.fullmatch(scope) or ".." in scope:
        raise ValueError(f"not a lesson scope: {str(scope)[:80]!r}")
    return rules_dir() / f"{scope}.json"


def rule_id(scope: str, lesson_id: str, kind: str, pattern: str) -> str:
    key = json.dumps({"scope": scope, "lesson": lesson_id, "kind": kind, "pattern": pattern}, sort_keys=True)
    return "LR-" + hashlib.sha256(key.encode()).hexdigest()[:10]


def _pattern_of(rule: dict) -> str:
    return rule.get("match") if rule.get("kind") == "call" else rule.get("error")


def _rule_problem(rule, scope: str) -> str:
    if not isinstance(rule, dict):
        return "a rule is not an object"
    rid = rule.get("id")
    if not isinstance(rid, str) or not _ID.fullmatch(rid):
        return f"rule id {str(rid)[:20]!r} is not LR- and 10 hex characters"
    if rule.get("kind") not in KINDS:
        return f"{rid}: kind must be call or output"
    if rule.get("mode") not in _FILE_MODES:
        return f"{rid}: mode must be warn (a rule never allows)"
    if not isinstance(rule.get("lesson"), str) or not _LESSON_ID.fullmatch(rule["lesson"]):
        return f"{rid}: no lesson id"
    pattern = _pattern_of(rule)
    why = pattern_problem(pattern) if rule["kind"] == "call" else error_problem(pattern)
    if why:
        return f"{rid}: {why}"
    if rule_id(scope, rule["lesson"], rule["kind"], pattern) != rid:
        return f"{rid}: the id does not match the rule (edited by hand?)"
    refused = probe_refused(pattern) if rule["kind"] == "call" else ""
    if refused:
        return f"{rid}: it would match `{refused}`; a rule never matches devkit, git status or the file tools"
    return ""


def probe_refused(match: str) -> str:
    """The first call of PROBES a call pattern matches, or ""."""
    pat = _pattern_words(match)
    return next((p for p in PROBES if runs(pat, words(p))), "")


def _read_file(path: Path):
    """(doc, problem) of one rule file; doc None when the whole file is ignored."""
    try:
        if not path.is_file():
            return None, "not a regular file"
        if path.stat().st_size > MAX_FILE_BYTES:
            return None, f"larger than {MAX_FILE_BYTES} bytes"
        doc = core.load_json(path.read_bytes()[:MAX_FILE_BYTES])  # a teammate's push: depth-bounded
    except (OSError, ValueError) as e:
        return None, f"cannot be read ({type(e).__name__})"
    if not isinstance(doc, dict) or not isinstance(doc.get("rules"), list):
        return None, "not {\"version\": 1, \"rules\": [...]}"
    if doc.get("version") != VERSION:
        return None, f"version {str(doc.get('version'))[:10]!r}, this devkit reads {VERSION}"
    return doc, ""


def _scope_of(path: Path) -> str:
    rel = path.relative_to(rules_dir()).as_posix()
    return rel[:-5] if rel.endswith(".json") else rel


def load(scopes) -> list:
    """The valid rules of these scopes, in file order, each with its `scope`. A file or rule that fails validation is
    left out and described in PROBLEMS, which this call resets; it never raises for what a file holds. A rule's `mode`
    is what it does here, `warn`, whatever its file says; `stored_mode` is what its file says."""
    del PROBLEMS[:]
    out, seen = [], set()
    for scope in dict.fromkeys(scopes or ()):
        try:
            path = rule_file(scope)
        except ValueError as e:
            PROBLEMS.append(str(e))
            continue
        if not path.exists():
            continue
        doc, why = _read_file(path)
        name = f"lessons/rules/{scope}.json"
        if doc is None:
            PROBLEMS.append(f"{name}: {why}; its rules are ignored")
            continue
        if len(doc["rules"]) > MAX_RULES:
            PROBLEMS.append(f"{name}: more than {MAX_RULES} rules; the rest are ignored")
        for rule in doc["rules"][:MAX_RULES]:
            why = _rule_problem(rule, scope)
            if why:
                PROBLEMS.append(f"{name}: {why}; the rule is ignored")
                continue
            if rule["id"] in seen:
                continue
            seen.add(rule["id"])
            out.append(dict(rule, scope=scope, mode="warn", stored_mode=rule["mode"]))
    return out


def all_scopes() -> list:
    """The scopes that have a rule file (for listing and lookup by id)."""
    base = rules_dir()
    if not base.is_dir():
        return []
    return [_scope_of(p) for p in sorted(base.rglob("*.json")) if _SCOPE.fullmatch(_scope_of(p))]


# --------------------------------------------------------------------------- the lesson behind a rule

def _lesson_of(rule: dict):
    """The rule's lesson (active, else archived), or None when it is gone or unreadable."""
    lid, scope = rule.get("lesson") or "", rule.get("scope") or ""
    base = Path(lessons.LESSONS)
    for path in (base / scope / f"{lid}.md", base / lessons.ARCHIVE / scope / f"{lid}.md"):
        try:
            if path.is_file():
                return lessons.load(path)
        except (OSError, ValueError, KeyError):
            return None
    return None


def _clean(text, limit: int) -> str:
    """One line, no control or format characters (hidden text, direction marks), at most `limit` characters."""
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = "".join(ch if unicodedata.category(ch)[0] != "C" else " " for ch in s)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def _active_text(rule: dict):
    """(title, prevention) of the rule's lesson, or None when the rule is off: the lesson is gone, held by the screen
    (its front matter or the screen now), superseded or retired."""
    les = _lesson_of(rule)
    return _text_of(les) if les else None


def _text_of(les: dict):
    """(title, prevention) of a lesson as a rule shows it, or None when the lesson may not be shown."""
    m = les["meta"]
    if m.get("quarantine") or m.get("superseded_by") or m.get("status") == "retired":
        return None
    prevention = ((les.get("sections") or {}).get("Prevention") or "").strip()
    prevention = "" if prevention == "_—_" else prevention.split("\n\n")[0]
    title = m.get("title") or ""
    from . import lessonlint
    if lessonlint.screen({"title": title, "Prevention": prevention}):
        return None
    return _clean(title, TITLE_CHARS), _clean(prevention, PREVENTION_CHARS)


def describe(rule: dict) -> str:
    """The lesson's id, title and prevention, capped: all a warning or a refusal says about the lesson. "" when the
    rule is off."""
    text = _active_text(rule)
    if not text:
        return ""
    title, prevention = text
    return f"lesson {rule['lesson']}: {title}" + (f" — prevention: {prevention}" if prevention else "")


# --------------------------------------------------------------------------- checks

def check_call(command: str, scopes) -> tuple:
    """The rule a shell command hits: ("warn", rule) for the first one it hits whose lesson is active (describe),
    else ("", None). Reads only; never raises for what a rule file holds."""
    rules = [r for r in load(scopes) if r["kind"] == "call"]
    if not rules or not str(command or "").strip():
        return "", None
    by_first = {}
    for r in rules:
        pat = _pattern_words(r["match"])
        by_first.setdefault(pat[0], []).append((r, pat))
    ws = words(command)
    hit = {}
    for i, w in enumerate(ws):
        for key in dict.fromkeys((w, os.path.basename(w))):
            for r, pat in by_first.get(key, ()):
                if r["id"] not in hit and _at(pat, ws, i):
                    hit[r["id"]] = r
    return next((("warn", r) for r in hit.values() if describe(r)), ("", None))


def check_output(text: str, scopes) -> list:
    """The output rules whose error text the output shows (without case), with an active lesson."""
    out = str(text or "")[-OUTPUT_CHARS:].casefold()
    if not out.strip():
        return []
    return [r for r in load(scopes) if r["kind"] == "output" and r["error"].casefold() in out and describe(r)]


def find(rule_id_: str):
    """The rule with this id in any scope, or None."""
    for r in load(all_scopes()):
        if r["id"] == rule_id_:
            return r
    return None


def _lock_of(scope: str) -> Path:
    """The path whose lock (state.hold) serializes the writers of one rule file. It lives in the state folder, as a
    lesson's does: a lock next to the file would land in the shared handoff repository."""
    return STATE / "lessons" / ("rules-" + re.sub(r"[^A-Za-z0-9_.-]", "_", scope) + ".json")


def _write(scope: str, fn) -> dict:
    """Change one rule file the way state.update changes a record: under its lock, re-read, fn(doc) (state.SKIP
    writes nothing), atomic replace (work._replace). A file that exists but is not a rule file this devkit reads is
    never written over: ValueError."""
    from . import work
    path = rule_file(scope)
    with state.hold(_lock_of(scope)):
        if path.exists():
            doc, why = _read_file(path)
            if doc is None:
                raise ValueError(f"lessons/rules/{scope}.json {why}; fix or remove it before adding a rule")
        else:
            doc = {"version": VERSION, "rules": []}
        out = fn(doc)
        if out is state.SKIP:
            return doc
        path.parent.mkdir(parents=True, exist_ok=True)
        work._replace(path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        return doc


def _commit(scope: str, message: str) -> None:
    from . import work
    try:
        work._commit([rule_file(scope)], message)
    except Exception:
        pass  # the rule is written; the next sync commits it


def record_hit(rule_id_: str, missed: bool) -> None:
    """Count a hit of the rule on this machine (STATE/lessons-rule-hits.json), and a miss when the agent was shown the
    lesson earlier in its session and the mistake showed again (an output rule). A miss never changes what the rule
    does: rules only warn. Unknown ids and busy files are dropped: the hook that calls this never fails on it."""
    if not isinstance(rule_id_, str) or not _ID.fullmatch(rule_id_):
        return
    now = int(time.time())

    def count(doc):
        e = doc.setdefault(rule_id_, {"hits": 0, "misses": 0})
        e["hits"] = int(e.get("hits") or 0) + 1
        e["misses"] = int(e.get("misses") or 0) + (1 if missed else 0)
        e["last"] = now

    try:
        state.update(HITS, count)
    except (OSError, state.StateBusy, ValueError):
        pass


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _who() -> str:
    from . import work
    a = work.actor()
    return f"{a['user']}@{a['host']} ({a['agent']})"


# --------------------------------------------------------------------------- proposing a rule

def _mistake_texts(lesson: dict) -> list:
    """The commands the lesson recorded: its command anchors, the backtick spans of What happened and Evidence, and
    each line of Evidence."""
    an = lessons.anchors(lesson)
    secs = lesson.get("sections") or {}
    out = list(an["commands"])
    for key in ("What happened", "Evidence"):
        out += _BACKTICKS.findall(secs.get(key) or "")
    out += [ln.strip(" -*\t") for ln in (secs.get("Evidence") or "").splitlines() if ln.strip()]
    return [x for x in out if x and x != "_—_"]


def _audit_commands(days: int, now: float) -> list:
    """(ts, command) of the `dev` commands that succeeded on this machine (its local audit records)."""
    from . import audit
    out = []
    for r in audit.query(days=days):
        action = str(r.get("action") or "")
        if action.startswith("dev.") and r.get("outcome") == "ok" and (r.get("ts") or 0) >= now - days * 86400:
            out.append((r.get("ts") or 0, " ".join(["dev"] + action[4:].split(".") + [str(r.get("target") or "")])))
    return out


def _timing_commands(days: int, now: float) -> list:
    """(ts, command) of the shell calls this machine's hooks timed after they succeeded (STATE/timing, slow calls
    only, the first 160 characters)."""
    from .hooks import SHELL_TOOLS
    out = []
    base = STATE / "timing"
    files = sorted(base.glob("*.jsonl"))[-2:] if base.is_dir() else []
    for f in files:
        try:
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for ln in lines:
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            what = r.get("what") if isinstance(r, dict) else None
            if r.get("kind") != "tool" or not isinstance(what, str) or (r.get("ts") or 0) < now - days * 86400:
                continue
            tool, _, cmd = what.partition(": ")
            if tool in SHELL_TOOLS and cmd:
                out.append((r.get("ts") or 0, cmd))
    return out


def recent_successes(days: int = WINDOW_DAYS, now: float = None) -> list:
    """(ts, command) of the commands that succeeded on this machine in the last `days`: the local audit records'
    `dev` commands and the shell calls the hooks timed."""
    now = time.time() if now is None else now
    return _audit_commands(days, now) + _timing_commands(days, now)


def _ledger_successes(days: int, now: float) -> dict:
    """{command key: ts} of the shell calls that succeeded in this machine's sessions in the last `days`: the
    session ledgers the hooks keep (bugs.ledger_append), which hold each call's command key (bugs.command_key of its
    command words), never its text."""
    from . import bugs
    base = Path(bugs.BUGS) / "ledger"
    out = {}
    try:
        files = [f for f in base.glob("*.jsonl") if f.stat().st_mtime >= now - days * 86400] if base.is_dir() else []
    except OSError:
        return out
    for f in files:
        for r in bugs._tail_records(f):
            ts = r.get("ts") if isinstance(r.get("ts"), (int, float)) else 0
            key = r.get("cmd")
            if r.get("ok") is True and isinstance(key, str) and key and ts >= now - days * 86400:
                out[key] = max(ts, out.get(key, 0))
    return out


def _ledger_match(pat: list, lesson: dict, days: int = WINDOW_DAYS, now: float = None):
    """(ts, command) when a session ledger shows a successful shell call the pattern refuses, else None. The ledger
    keeps keys, not text, so the commands compared are the ones the pattern names: its words alone or with one
    quoted argument (`git commit -m 'wip'`), and each recorded mistake it matches, each alone or after `cd <folder>
    &&` (a key leaves out paths, so one folder stands for all)."""
    from . import bugs
    from .hooks import command_words
    now = time.time() if now is None else now
    seen = _ledger_successes(days, now)
    if not seen:
        return None
    cands = []
    if not any("*" in w for w in pat):
        base = " ".join(shlex.quote(w) for w in pat)
        cands += [base, base + " ''", base + ' ""']
    cands += [t for t in _mistake_texts(lesson) if runs(pat, words(t))]
    for cmd in cands + [f"cd . && {c}" for c in cands]:
        key = bugs.command_key(command_words(cmd))
        if key and key in seen:
            return seen[key], cmd
    return None


def propose(lesson: dict, match: str = "", error: str = "") -> dict:
    """Validate a warning rule for this lesson and write it to lessons/rules/<scope>.json; -> the rule. Exactly one of
    a call pattern (`match`) or an output text (`error`). Raises ValueError with the reason when:
    the lesson is held by the screen, superseded or retired; the pattern is badly shaped (pattern_problem,
    error_problem); it does not match the lesson's recorded mistake (its command or error anchors, or its evidence);
    a call pattern would match devkit, `git status` or a file tool (PROBES); or it matches a command that succeeded
    on this machine in the last WINDOW_DAYS days (recent_successes, and the session ledgers' shell calls). The same
    rule proposed again returns the one on file."""
    m = (lesson or {}).get("meta") or {}
    lid, scope = str(m.get("id") or ""), str(m.get("scope") or "")
    if not _LESSON_ID.fullmatch(lid):
        raise ValueError("not a lesson")
    rule_file(scope)
    if m.get("quarantine"):
        raise ValueError(f"{lid} is held by the screen ({m['quarantine']}); a held lesson makes no rule")
    if m.get("superseded_by") or m.get("status") == "retired":
        raise ValueError(f"{lid} is {'superseded' if m.get('superseded_by') else 'retired'}; it makes no rule")
    match, error = (match or "").strip(), (error or "").strip()
    if bool(match) == bool(error):
        raise ValueError("give one pattern: --match \"<command words>\" or --error \"<output text>\"")
    kind, pattern = ("call", match) if match else ("output", error)
    if kind == "call":
        why = pattern_problem(match)
        if why:
            raise ValueError(f"the pattern: {why}")
        pat = _pattern_words(match)
        if not any(runs(pat, words(t)) for t in _mistake_texts(lesson)):
            raise ValueError(f"the pattern matches none of {lid}'s recorded commands (its command anchors or its "
                             f"evidence); record the command that went wrong first")
        refused = probe_refused(match)
        if refused:
            raise ValueError(f"the pattern would match `{refused}`; a rule never matches devkit, git status or "
                             f"the file tools")
        found = next(((ts, cmd) for ts, cmd in recent_successes() if runs(pat, words(cmd))), None) or \
            _ledger_match(pat, lesson)
        if found:
            day = time.strftime("%Y-%m-%d", time.localtime(found[0] or 0))
            raise ValueError(f"the pattern matches a command that succeeded here on {day}: "
                             f"`{_clean(found[1], 80)}`; make it name only the form that goes wrong")
    else:
        why = error_problem(error)
        if why:
            raise ValueError(f"the error text: {why}")
        secs = lesson.get("sections") or {}
        seen = lessons.anchors(lesson)["errors"] + [secs.get("Evidence") or "", secs.get("What happened") or ""]
        if not any(error.casefold() in str(s).casefold() for s in seen):
            raise ValueError(f"the error text appears in none of {lid}'s error anchors or evidence")
    if not _text_of(lesson):
        raise ValueError(f"{lid}'s title or prevention is held by the screen; a rule would show nothing")
    rid = rule_id(scope, lid, kind, pattern)
    made = {"id": rid, "lesson": lid, "kind": kind, ("match" if kind == "call" else "error"): pattern, "mode": "warn",
            "created": _now_iso(), "created_by": _who(), "history": [f"{_now_iso()} · {_who()} · warn: proposed"]}
    result = {}

    def change(doc):
        for r in doc["rules"]:
            if isinstance(r, dict) and r.get("id") == rid:
                result.update(r)
                return state.SKIP
        if len(doc["rules"]) >= MAX_RULES:
            raise ValueError(f"lessons/rules/{scope}.json already holds {MAX_RULES} rules")
        doc["rules"].append(made)
        result.update(made)
        return None

    _write(scope, change)
    _commit(scope, f"{rid}: warn rule for {lid} ({kind}: {_clean(pattern, 60)})")
    return dict(result, scope=scope, mode="warn", stored_mode=result.get("mode"))


def scopes_for(project: str = "", initiatives=()) -> list:
    """The scopes whose rules apply to a project: its own, its initiatives', and the global ones."""
    return [s for s in [project] + [f"initiatives/{i}" for i in initiatives or ()] + ["global"] if s]


def problems_text(limit: int = 3) -> str:
    """PROBLEMS as one line, for a log or a listing."""
    return "; ".join(PROBLEMS[:limit]) + (f" (+{len(PROBLEMS) - limit} more)" if len(PROBLEMS) > limit else "")


# --------------------------------------------------------------------------- matching a tool call

# The PreToolUse hook asks lesson_rule_match for each shell call: the rule it hits is kept in LAST_LESSON_RULE, and the
# hook adds its text to the call's context (lesson_rule_context) and records the hit (lesson_rule_hit). A failure, a
# bad rule file or a lookup over LESSON_RULE_BUDGET seconds finds nothing and goes to the hooks log: the call runs
# as it would without lesson rules.
LESSON_RULE_BUDGET = 0.3
LESSON_TEXT_CHARS = 600
LAST_LESSON_RULE: dict = {}  # the last call's lesson rule: {"mode": "warn", "rule": {…}, "text": "…"}, {} for none
_RULE_PROBLEMS = "lessons-rule-problems.json"  # STATE: when each bad rule file was last recorded (once a day)


def _log(msg: str) -> None:
    """One line in the hooks log; never raises."""
    try:
        from . import hooks
        hooks._log(f"lesson rules: {msg}")
    except Exception:
        pass


def _lesson_rule_scopes(cwd) -> list:
    """The rule scopes for a call in `cwd`: its project's, the initiatives' it is a member of, and the global ones.
    Only scopes that have a rule file; the project is looked up only when a project or initiative file exists."""
    present = all_scopes()
    if not present:
        return []
    out = []
    if any(s != "global" for s in present):
        from .lanescope import current_project
        from .notes import initiative_members
        project = current_project(cwd)
        if project:
            out += [s for s in present if s == project]
            out += [s for s in present if s.startswith("initiatives/") and project in
                    initiative_members(s.split("/", 1)[1])]
    return out + [s for s in present if s == "global"]


def _lesson_rule_lookup(commands: list, cwd) -> dict:
    """{"mode", "rule", "text", "problems"} of the first lesson rule the commands hit, within LESSON_RULE_BUDGET: a
    lookup that is still running then is abandoned (it only reads) and raises TimeoutError."""
    import threading
    box = {}

    def look():
        try:
            scopes = _lesson_rule_scopes(cwd)
            mode, rule, problems = "", None, []
            for cmd in commands:
                m, r = check_call(cmd, scopes) if scopes else ("", None)
                problems += [p for p in PROBLEMS if p not in problems]
                if r is not None:
                    mode, rule = m, r
                    break
            box["out"] = {"mode": mode, "rule": rule, "problems": problems, "text": describe(rule) if rule else ""}
        except BaseException as e:  # noqa: B036 (the thread reports every failure to the caller)
            box["error"] = e

    worker = threading.Thread(target=look, name="devkit-lesson-rules", daemon=True)
    worker.start()
    worker.join(LESSON_RULE_BUDGET)
    if worker.is_alive():
        raise TimeoutError(f"lesson rules took longer than {LESSON_RULE_BUDGET}s")
    if "error" in box:
        raise box["error"]
    return box.get("out") or {}


def _report_rule_problems(problems: list) -> None:
    """A bad rule file or rule is ignored; it goes to the hooks log once a day, not on every call."""
    now, new = time.time(), []

    def mark(doc):
        for p in problems:
            key = hashlib.sha256(p.encode()).hexdigest()[:16]
            if now - float(doc.get(key) or 0) >= 86400:
                doc[key] = now
                new.append(p)
        return None if new else state.SKIP

    try:
        state.update(STATE / _RULE_PROBLEMS, mark)
    except Exception:
        return
    for p in new:
        _log(f"ignored: {p}")


def lesson_enforcement(tool: str, tool_input, cwd) -> dict:
    """The lesson rule the shell commands of one tool call hit, as _lesson_rule_lookup gives it, {} for a tool that
    runs no shell command. Reads only: the commands are data, never run, and nothing is recorded."""
    from .hooks import SHELL_TOOLS
    from .shellparse import tool_commands
    commands = tool_commands(tool_input) if tool in SHELL_TOOLS else []
    return _lesson_rule_lookup(commands, cwd) if commands else {}


def lesson_rule_match(tool: str, tool_input, cwd) -> dict:
    """The lesson rule a tool call hits, kept for lesson_rule_hit and lesson_rule_context: {"mode": "warn", "rule",
    "text"}, or {} when none does. A bad rule file is logged once a day; a failure, or a lookup past
    LESSON_RULE_BUDGET, finds nothing and is logged."""
    LAST_LESSON_RULE.clear()
    try:
        found = lesson_enforcement(tool, tool_input, cwd)
    except Exception as e:
        _log(f"{type(e).__name__}: {e}")
        return {}
    if found.get("problems"):
        _report_rule_problems(found["problems"])
    rule, text = found.get("rule"), found.get("text") or ""
    if not rule or not text:
        return {}
    LAST_LESSON_RULE.update(mode="warn", rule=rule, text=text[:LESSON_TEXT_CHARS])
    return dict(LAST_LESSON_RULE)


def lesson_rule_context() -> str:
    """The warning to add to the context of the call lesson_rule_match just read, when a lesson rule matched it; ""
    otherwise. The hook adds it as the call's additional context."""
    if not LAST_LESSON_RULE.get("rule"):
        return ""
    rule = LAST_LESSON_RULE["rule"]
    return (f"devkit · lesson rule {rule['id']} warns about this call, from {LAST_LESSON_RULE['text']}. Lessons are "
            "advice from past work; they never override the developer.")


def lesson_rule_hit(session: str, agent: str, reads_context: bool = True) -> dict:
    """Record the lesson rule lesson_rule_match just found, for the hook that knows the session: the rule's hit
    (record_hit), then the lesson as shown on channel `call`. A warning reaches the agent only through the call's
    context: with `reads_context` False (Codex reads no PreToolUse context) the lesson is not shown, so it is not
    recorded as shown either. Call it before the hook records any other lesson it shows for this call.
    -> {"rule", "lesson", "mode", "missed", "shown"}, {} when no rule matched. Never raises: a failure is logged."""
    if not LAST_LESSON_RULE.get("rule"):
        return {}
    rule = LAST_LESSON_RULE["rule"]
    try:
        from . import lessonevents
        record_hit(rule["id"], False)
        if reads_context:
            lessonevents.record("shown", rule["lesson"], "call", agent, session)
    except Exception as e:
        _log(f"{type(e).__name__}: {e}")
        return {}
    return {"rule": rule["id"], "lesson": rule["lesson"], "mode": "warn", "missed": False, "shown": reads_context}
