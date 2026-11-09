# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Lesson lint: what a lesson must look like before it is recorded, what holds it back from recall, and whether its
prevention still exists.

    check_anchors(anchors) -> [str]       the anchor gate: a command anchor that is one bare common program, and an
                                          error anchor shorter than 12 characters or on the stoplist, are refused
    check_shape(title, happened, evidence) -> [str]
                                          the capture shape: the title is the rule, one line of at most 80
                                          characters, not starting with "["; what happened or evidence is given
    screen(fields) -> str                 the reason to hold (quarantine) a lesson, or "": text that tells agents to
                                          override the developer or the guard, a secret-like value, a URL, a pipe
                                          into a shell, a command substitution, a backtick command, download-and-run,
                                          a path in the agents' or devkit's configuration folders, or invisible
                                          characters
    lint(lessons, repo_root=None) -> [{id, kind, problem, fix}]
                                          recorded lessons that recall cannot use well: broad, missing and wrong
                                          anchors, file anchors that match no file, no root cause, held lessons
    prevention_gone(lesson, repo_root) -> str
                                          why the test, guard rule, skill or lint rule a prevented lesson names is
                                          gone, or "" (also "" when the reference names nothing that can be checked)

Every function takes plain data (the lesson dicts lessons.parse returns) and is pure but for reading files; nothing
here writes. The error and reason texts are single lines: callers join them, store a reason in front matter
(`quarantine: <reason>`), and print fixes as they are.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import unicodedata
from pathlib import Path

# --------------------------------------------------------------------------- the anchor gate

BARE_PROGRAMS = frozenset(("python3", "python", "git", "dev", "cp", "mkdir", "sed", "cat", "cd", "ls", "echo", "bash",
                           "sh", "node", "npm"))
_PYTHON = re.compile(r"python\d*(?:\.\d+)?")
ERROR_MIN = 12
# error anchors that match ordinary output: the spec's list, and the generic messages any shell or OS prints
ERROR_STOPLIST = frozenset(("file(s)", "still fail", "error", "failed", "not found", "permission denied",
                            "no such file or directory", "command not found", "operation not permitted",
                            "traceback (most recent call last)"))
_EDGE = " \t.,:;!?\"'`“”‘’"


def _words(command: str) -> list:
    try:
        return shlex.split(command or "")
    except ValueError:
        return (command or "").split()


def _bare(word: str) -> bool:
    name = word.rsplit("/", 1)[-1]
    return name in BARE_PROGRAMS or bool(_PYTHON.fullmatch(name))


def _norm_error(text: str) -> str:
    return " ".join(str(text).split()).casefold().strip(_EDGE)


# what a hint shows as a command anchor that names the risky use of a bare program
EXAMPLES = {"python3": "python3 -c", "python": "python -c", "bash": "bash -c", "sh": "sh -c", "node": "node -e",
            "git": "git push --force", "dev": "dev progress step", "npm": "npm run <script>", "sed": "sed -i"}


def _command_errors(command: str) -> list:
    words = _words(command)
    if len(words) == 1 and _bare(words[0]):
        name = words[0].rsplit("/", 1)[-1]
        example = EXAMPLES.get(name, f"{name} <the flag or argument that goes wrong>")
        return [f"the command anchor `{command}` alone matches every {name} call: anchor the words that go wrong "
                f"too, such as `{example}`"]
    return []


def _error_errors(error: str) -> list:
    text = " ".join(str(error).split())
    if _norm_error(text) in ERROR_STOPLIST:
        return [f"the error anchor `{text}` matches ordinary output: anchor the distinctive part of the message"]
    if len(text) < ERROR_MIN:
        return [f"the error anchor `{text}` is {len(text)} characters: give at least {ERROR_MIN} characters of the "
                f"text the output shows"]
    return []


def check_anchors(anchors: dict) -> list:
    """The anchor gate for new anchors: [error text], [] when they pass. Files, kinds of work and stack are not judged
    here (lessons._clean_anchors checks kinds of work against the RACI); lint lists broad file anchors."""
    anchors = anchors or {}
    out = []
    for c in anchors.get("commands") or []:
        if str(c).strip():
            out += _command_errors(str(c).strip())
    for e in anchors.get("errors") or []:
        if str(e).strip():
            out += _error_errors(e)
    return out


# --------------------------------------------------------------------------- the capture shape

TITLE_MAX = 80
_LINE_BREAKS = re.compile(r"[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")


def check_shape(title: str, happened: str, evidence: list) -> list:
    """The capture shape: [error text], [] when it fits."""
    t = str(title or "").strip()
    out = []
    if not t:
        out.append("a lesson needs a title: the rule to follow, in at most 80 characters")
    else:
        if _LINE_BREAKS.search(t):
            out.append("the title must be one line: put the story in --happened")
        if len(t) > TITLE_MAX:
            out.append(f"the title is {len(t)} characters: make it the rule to follow in at most {TITLE_MAX} "
                       f"(the story goes in --happened)")
        if t.startswith("["):
            out.append("the title cannot start with “[”: the front matter would read it as a list")
    if not str(happened or "").strip() and not any(str(e or "").strip() for e in evidence or []):
        out.append("say what happened (--happened) or give the evidence (--evidence): a commit, PR, run or the "
                   "command and its output")
    return out


# --------------------------------------------------------------------------- screening

# characters that hide or reorder text: zero-width, direction controls, the BOM, and Unicode tag characters
_HIDDEN = re.compile("[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\U000e0000-\U000e007f]")
_DASHES = str.maketrans({c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"})
_OVERRIDE = re.compile(
    r"\bignore\s+(?:all\s+|any\s+)?(?:the\s+|your\s+)?(?:previous|prior|above|earlier|preceding)\b"
    r"|\bdisregard\s+(?:all\s+|any\s+)?(?:the\s+|your\s+)?(?:previous|prior|above|instructions|rules|developer|user"
    r"|guard)\b"
    r"|\balways\s+allow\b"
    r"|\b(?:disable|turn\s+off|switch\s+off|bypass|skip|circumvent|deactivate|silence)\s+(?:the\s+|all\s+|any\s+"
    r"|devkit'?s?\s+|its\s+)*(?:guard|hooks?|sandbox|verification|permission\s+checks?|safeguards?)\b"
    r"|\b(?:override|overrule|disobey)\s+(?:the\s+)?(?:developer|user|guard)\b"
    r"|--no-verify\b|--dangerously-[\w-]+|\bdangerously(?:disable|skip|bypass)\w*|\bbypasspermissions\b",
    re.I)
_SHELLS = r"(?:(?:ba|z|da|k|c|tc|fi)?sh|python[\d.]*|perl|ruby|node|php|pwsh|powershell|iex)"
_DOWNLOAD_RUN = re.compile(
    r"<\(\s*(?:curl|wget|fetch|iwr|irm)\b"
    r"|\b(?:curl|wget|fetch)\b[^\n]{0,200}?(?:&&|;|\|\|)\s*(?:sudo\s+)?(?:" + _SHELLS + r"|chmod\s+\+x|source|\.)\s"
    r"|\biex\b\s*\(?\s*(?:iwr|irm|invoke-webrequest|invoke-restmethod|new-object)\b",
    re.I)
_PIPE_SHELL = re.compile(r"(?<!\|)\|(?!\|)\s*(?:(?:sudo|env|xargs|exec|command)\b[^|;&\n]{0,80}?\s)?(?:/[\w./-]*/)?"
                         + _SHELLS + r"\b", re.I)
_SUBST = re.compile(r"\$\(")
_BACKTICK = re.compile(r"`[^`]+`")
_URL = re.compile(r"\b[a-z][a-z0-9+.-]{1,15}://\S|\bwww\.[a-z0-9-]+\.[a-z]", re.I)
_CONFIG = re.compile(r"(?:~|\$HOME|\$\{HOME\}|/Users/[^/\s]+|/home/[^/\s]+|/root)/(?:\.claude(?:\.json|/|\b)|\.codex\b"
                     r"|\.local/share/devkit\b|\.config/devkit\b)")
# (what the reason says, the test), in the order a reason is chosen
SCREENS = (
    ("has invisible or direction-changing characters", _HIDDEN.search),
    ("has a secret-like value", None),  # the audit redactor's patterns, loaded when first used
    ("tells agents to override the developer or the guard", _OVERRIDE.search),
    ("downloads and runs code", _DOWNLOAD_RUN.search),
    ("pipes into a shell", _PIPE_SHELL.search),
    ("has a command substitution", _SUBST.search),
    ("has a backtick command", _BACKTICK.search),
    ("has a URL", _URL.search),
    ("names a path in the agents' or devkit's configuration folders", _CONFIG.search),
)


def _secret(text: str):
    from .audit import SECRET
    return SECRET.search(text)


def _strings(value) -> list:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [x for v in value for x in _strings(v)]
    return []


def _field(key) -> str:
    return " ".join(str(key).split()).lstrip("[")[:40] or "text"


def screen(fields: dict) -> str:
    """Why a lesson must be held instead of recalled, as one line ("Prevention pipes into a shell"), or "". Every
    string value is screened, whatever its key, and so are lists of strings. Text is compared after NFKC
    normalization (fullwidth letters become plain ones) with Unicode dashes read as "-". The reason never quotes the
    text it found, so a secret is never copied into the lesson's front matter."""
    texts = [(key, s) for key, value in (fields or {}).items() for s in _strings(value) if s]
    for what, test in SCREENS:
        for key, raw in texts:
            text = raw if test is _HIDDEN.search else unicodedata.normalize("NFKC", raw).translate(_DASHES)
            if (test or _secret)(text):
                return f"{_field(key)} {what}"
    return ""


# --------------------------------------------------------------------------- lint

SCRATCH = frozenset(("scratchpad", "scratch", ".tmp", "tmp"))
_SKIP_DIRS = frozenset((".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache",
                        ".ruff_cache", ".tox"))
WALK_MAX = 100_000  # files a walk lists; past it, an unmatched glob is not reported
_GLOB = re.compile(r"[*?\[]")
_EVERYTHING = re.compile(r"[*/.?]*")
NOT_LINTED = ("retired", "dormant")


def _walk(root: Path):
    """[relative posix path] of the files under root (skipping VCS, dependency and cache folders), and whether the
    list is complete."""
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in _SKIP_DIRS]
        rel = Path(d).relative_to(root).as_posix()
        for f in files:
            out.append(f if rel == "." else f"{rel}/{f}")
            if len(out) >= WALK_MAX:
                return out, False
    return out, True


def _repo(repo_root) -> Path | None:
    """The repository folder to check against; None when none is given or it is not on this machine."""
    return Path(repo_root) if repo_root and Path(repo_root).is_dir() else None


def _scratch(anchor: str) -> bool:
    """A scratch pattern (a folder of SCRATCH anywhere in it), or a path outside the repository."""
    a = anchor.strip()
    if a.startswith(("/", "~", "$")):
        return True  # outside the repository: not this check's business
    return any(part in SCRATCH for part in a.split("/") if part and not _GLOB.search(part))


def _matches(anchor: str, path: str) -> bool:
    a = anchor[2:] if anchor.startswith("./") else anchor
    a = a.rstrip("/") if not _GLOB.search(a) else a
    if path == a or path.startswith(a + "/") or path.endswith("/" + a):
        return True
    if fnmatch.fnmatchcase(path, a) or fnmatch.fnmatchcase(path, "*/" + a):
        return True
    return a.startswith("**/") and fnmatch.fnmatchcase(path, a[3:])


def _file_matches(anchor: str, root: Path, listing) -> bool | None:
    """Does a file anchor name a file under root? None when it cannot tell (the walk stopped early)."""
    a = anchor[2:] if anchor.startswith("./") else anchor
    if not _GLOB.search(a) and (root / a).exists():
        return True
    files, complete = listing()
    if any(_matches(a, p) for p in files):
        return True
    return False if complete else None


def _plain(text: str) -> str:
    return " ".join(re.sub(r"[`'\"“”‘’]", " ", str(text)).split()).casefold()


def _lesson_text(les: dict) -> str:
    sections = les.get("sections") or {}
    return _plain(" ".join([str((les.get("meta") or {}).get("title") or "")] +
                           [str(v) for v in sections.values() if v and v != "_—_"]))


def _activity_known(activity: str) -> bool | None:
    try:
        from . import raci
        raci.resolve_activity(activity)
        return True
    except ValueError:
        return False
    except Exception:
        return None  # the roster could not be read: say nothing


def _finding(les_id: str, kind: str, problem: str, fix: str) -> dict:
    return {"id": les_id, "kind": kind, "problem": problem, "fix": fix}


def lint(lessons: list, repo_root=None) -> list:
    """Findings for recorded lessons, in their order: [{id, kind, problem, fix}], kind one of broad, missing, wrong,
    no-file, no-cause, quarantined. Retired and archived (dormant) lessons are past recall and skipped. File anchors
    are checked against the files under repo_root (none when it is None); scratch patterns and paths outside the
    repository are not."""
    root = _repo(repo_root)
    cache = {}

    def listing(base: Path):
        if base not in cache:
            cache[base] = _walk(base)
        return lambda: cache[base]

    def no_file(anchor: str) -> bool:
        """The anchor matches no file in the repository, nor in the handoff repository (lessons, plans, notes),
        which recall also sees; ".handoff/" in front of it names the handoff repository."""
        here = _file_matches(anchor, root, listing(root))
        if here is not False:
            return False
        handoff = _handoff()
        if not handoff.is_dir() or handoff == root:
            return True
        a = anchor[2:] if anchor.startswith("./") else anchor
        a = a[len(".handoff/"):] if a.startswith(".handoff/") else a
        return _file_matches(a, handoff, listing(handoff)) is False

    out = []
    for les in lessons or []:
        m = les.get("meta") or {}
        if m.get("status") in NOT_LINTED:
            continue
        lid = str(m.get("id") or "?")
        anchor = f"dev lesson anchor {lid}"
        an = {k: [str(x).strip() for x in (m.get(k) or []) if str(x).strip()] if isinstance(m.get(k), list) else []
              for k in ("files", "commands", "activities", "errors", "stack")}
        if str(m.get("quarantine") or "").strip():
            out.append(_finding(lid, "quarantined", f"held, not recalled: {m['quarantine']}",
                                f"check its text, then the developer releases it: dev lesson release {lid}"))
        cause = str((les.get("sections") or {}).get("Root cause") or "").strip()
        if not cause or cause == "_—_":
            out.append(_finding(lid, "no-cause", "has no root cause, so it is not recalled",
                                f"record why it happened, or retire it: dev lesson retire {lid} --why …"))
        if not any(an.values()):
            out.append(_finding(lid, "missing", "has no anchors, so it is not recalled",
                                f"{anchor} --file … --command … --error … (where the mistake happens)"))
            continue
        text = _lesson_text(les)
        for c in an["commands"]:
            words = _words(c)
            if _command_errors(c):
                out.append(_finding(lid, "broad", _command_errors(c)[0],
                                    f'{anchor} --command "{words[0]} <the words that go wrong>", then drop `{c}`'))
            elif len(words) > 1 and all(_bare(w) for w in words):
                out.append(_finding(lid, "wrong", f"the command anchor `{c}` names several programs in one: it "
                                                  f"matches only that exact sequence",
                                    f"{anchor} with one --command each, then drop `{c}`"))
            elif _plain(c) not in text:
                out.append(_finding(lid, "wrong", f"the command anchor `{c}` is named nowhere in the lesson: is it "
                                                  f"this lesson's mistake?",
                                    f"if not, {anchor} --command with the command that went wrong, then drop `{c}`"))
        for e in an["errors"]:
            for problem in _error_errors(e):
                out.append(_finding(lid, "broad", problem,
                                    f'{anchor} --error "<the distinctive part of the message>", then drop it'))
        for a in an["activities"]:
            if _activity_known(a) is False:
                out.append(_finding(lid, "wrong", f"the kind of work `{a}` is not in the crew's RACI",
                                    f"{anchor} --activity <an id from dev crew raci>, then drop `{a}`"))
        for f in an["files"]:
            if _EVERYTHING.fullmatch(f):
                out.append(_finding(lid, "broad", f"the file anchor `{f}` matches every file",
                                    f"{anchor} --file <the folder or files the mistake is in>, then drop `{f}`"))
            elif root and not _scratch(f) and no_file(f):
                out.append(_finding(lid, "no-file", f"the file anchor `{f}` matches no file in the repository or "
                                                    f"the handoff repository",
                                    f"{anchor} --file with the current path, then drop `{f}`"))
    return out


# --------------------------------------------------------------------------- prevention references

DEVKIT_LIB = Path(__file__).resolve().parent
DEVKIT_HOME = DEVKIT_LIB.parents[1]
TEXT_MAX = 512 * 1024  # bytes read of one file when a name is searched for
SEARCH_MAX = 64 * 1024 * 1024  # bytes read in all; past it, a name not found is not reported
_PATH = re.compile(r"(?P<path>(?:\./)?[\w.@+-]+(?:/[\w.@+-]+)+)(?P<sym>(?:::?[\w.]+)+)?")
_TEST_DIRS = ("tests", "test", "spec", "__tests__")
_SYMBOL = re.compile(r"[A-Z][A-Z0-9_]{2,}|[A-Z][a-z0-9]+[A-Z]\w*|test_\w+")
_HEX = re.compile(r"[0-9a-f]{7,40}")
_RULE_ID = re.compile(r"[A-Z][A-Z0-9_]{2,}|[a-z0-9]+(?:-[a-z0-9]+)+|[A-Za-z]{1,4}-[0-9A-Za-z]{3,}")
_SKILL = re.compile(r"(?:(?P<plugin>[a-z0-9][a-z0-9-]*):)?(?P<name>[a-z0-9][a-z0-9-]*)")
_LINT_ID = re.compile(r"[A-Za-z][\w./-]{1,80}")


def _parts(ref: str) -> list:
    """A reference's parts: text in parentheses and quotes is a comment, and , ; · + & "and" separate parts."""
    ref = re.sub(r"\([^()]*\)|'[^'\n]*'|\"[^\"\n]*\"|“[^”\n]*”", " ", ref)
    return [p.strip() for p in re.split(r"[,;·]|\s[+&]\s|\sand\s", ref) if p.strip()]


def _is_path(token: str) -> bool:
    """A repository or handoff relative file, with or without :names after it (not a URL, not an absolute path)."""
    m = _PATH.fullmatch(token)
    if not m or "://" in token or token.startswith(("/", "~")):
        return False
    path = m.group("path")
    return bool(re.search(r"\.[A-Za-z0-9]{1,8}$", path)) or path.split("/", 1)[0] in _TEST_DIRS


def _read(path: Path, limit: int | None = TEXT_MAX) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read(limit if limit else -1).decode("utf-8", "ignore")
    except OSError:
        return ""


def _has_word(text: str, word: str) -> bool:
    return bool(re.search(r"(?<![\w])" + re.escape(word) + r"(?![\w])", text))


def _is_test_file(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return (any(p in _TEST_DIRS for p in rel.split("/")[:-1]) or name.startswith("test_")
            or re.search(r"(?:_test\.\w+|\.(?:test|spec)\.\w+)$", name) is not None)


def _search(root: Path, word: str, only=None) -> bool | None:
    """Is the word in a file under root (only the files `only` accepts)? None when the search stopped early."""
    files, complete = _walk(root)
    budget = SEARCH_MAX
    for rel in files:
        if only and not only(rel):
            continue
        text = _read(root / rel)
        budget -= len(text)
        if _has_word(text, word):
            return True
        if budget <= 0:
            return None
    return False if complete else None


def _handoff() -> Path:
    from . import lessons
    return Path(lessons.LESSONS).parent


def _locate(rel: str, root) -> Path | None:
    """A reference's path: in the repository, else in the handoff repository; None when in neither."""
    rel = rel[2:] if rel.startswith("./") else rel
    for base in ([root] if root else []) + [_handoff()]:
        p = base / rel
        if p.exists():
            return p
        if rel.split("/", 1)[0] in _TEST_DIRS and not re.search(r"\.\w+$", rel) and p.with_suffix(".py").exists():
            return p.with_suffix(".py")
    return None


def _symbols_gone(path: Path, rel: str, symbols: list) -> str:
    text = _read(path) if path.is_file() else ""
    for s in symbols:
        if text and not _has_word(text, s):
            return f"{s} is no longer in {rel}"
    return ""


def _path_gone(token: str, rest: list, root) -> str:
    m = _PATH.fullmatch(token)
    rel, sym = (m.group("path"), m.group("sym") or "") if m else (token, "")
    found = _locate(rel, root)
    if not found:
        return f"{rel} no longer exists" if root else ""
    symbols = [s for s in re.split(r"::?|\.", sym) if s and not s.isdigit()]
    symbols += [s for t in rest for s in t.split(".") if _SYMBOL.fullmatch(s)]
    return _symbols_gone(found, rel, symbols)


def _dotted_gone(token: str, root) -> str:
    """tests.test_x.Class.test_y: the longest prefix that is a module file, then the names in it."""
    parts = token.split(".")
    for i in range(len(parts), 0, -1):
        rel = "/".join(parts[:i]) + ".py"
        if (root / rel).is_file():
            return _symbols_gone(root / rel, rel, parts[i:])
    guess = next(("/".join(parts[:i + 1]) + ".py" for i, p in enumerate(parts) if i and p.startswith("test")),
                 "/".join(parts[:2]) + ".py")
    return f"{guess} no longer exists"


def _rule_ids() -> set:
    ids = set()

    def collect(x):
        if isinstance(x, dict):
            if isinstance(x.get("id"), str):
                ids.add(x["id"])
            for v in x.values():
                collect(v)
        elif isinstance(x, list):
            for v in x:
                collect(v)

    base = _handoff() / "lessons" / "rules"
    for f in sorted(base.rglob("*.json")) if base.exists() else []:
        try:
            collect(json.loads(f.read_text()))
        except (OSError, ValueError):
            continue
    return ids


def _guard_rule_gone(token: str) -> str:
    source = _read(DEVKIT_LIB / "guard.py", None) + _read(DEVKIT_LIB / "lessonrules.py", None)
    if token in _rule_ids() or _has_word(source, token):
        return ""
    return f"guard rule {token} no longer exists (not in devkit's guard nor in the lesson rules)"


def _skill_gone(token: str, root) -> str:
    m = _SKILL.fullmatch(token)
    if not m or (m.group("plugin") and m.group("plugin") != "devkit"):
        return ""  # another plugin's skill: devkit cannot see its install
    name = m.group("name")
    if (DEVKIT_HOME / "plugin" / "skills" / name / "SKILL.md").is_file():
        return ""
    rules = _read(DEVKIT_HOME / "config" / "skill-rules.json")
    if f'"{name}"' in rules:
        return ""
    if root and any(p.endswith(f"skills/{name}/SKILL.md") or p == f"{name}/SKILL.md" for p in _walk(root)[0]):
        return ""
    return f"skill {name} no longer exists (not in the repository's or devkit's skills)"


def _part_gone(part: str, kind: str, root) -> str:
    tokens = [t.rstrip(".:") for t in part.split()]
    paths = [i for i, t in enumerate(tokens) if _is_path(t)]
    for n, i in enumerate(paths):
        reason = _path_gone(tokens[i], tokens[i + 1:paths[n + 1] if n + 1 < len(paths) else None], root)
        if reason:
            return reason
    for t in tokens:
        if re.fullmatch(r"tests?(?:\.\w+)+", t) and root:
            reason = _dotted_gone(t, root)
            if reason:
                return reason
    if paths:
        return ""
    if root:
        for t in tokens:
            name = t.rstrip(".:")
            if re.fullmatch(r"test_\w+", name) and _search(root, name, _is_test_file) is False:
                return f"no test named {name} in the repository any more"
    if len(tokens) != 1 or _HEX.fullmatch(tokens[0]):
        return ""  # free text or a commit: nothing to check
    token = tokens[0].rstrip(".:")
    if kind == "guard" and _RULE_ID.fullmatch(token):
        return _guard_rule_gone(token)
    if kind == "skill":
        return _skill_gone(token, root)
    if kind == "lint" and root and _LINT_ID.fullmatch(token):
        return f"lint rule {token} is no longer named in the repository" if _search(root, token) is False else ""
    return ""


def prevention_gone(lesson: dict, repo_root) -> str:
    """Why the prevention a lesson names no longer exists, or "". The reference (prevention_ref) is read as parts
    separated by , ; · + & or "and", with text in parentheses or quotes left out. A part can name a file (repository
    or handoff relative, with ::names or :names after it, or class and test names after a space), a dotted test id
    (tests.test_x.Class.test_y), a test name (test_…), and, alone, a guard rule id (in devkit's guard or the lesson
    rules), a skill (the repository's skills, devkit's skills, or devkit's skill rules) or a lint rule (named
    somewhere in the repository). The first part that is gone gives the reason. Commit hashes, other plugins' skills,
    free text, and repository paths when repo_root is None are not judged."""
    m = (lesson or {}).get("meta") or {}
    ref = str(m.get("prevention_ref") or "").strip()
    if not ref:
        return ""
    root = _repo(repo_root)
    kind = str(m.get("prevention") or "")
    for part in _parts(ref):
        reason = _part_gone(part, kind, root)
        if reason:
            return reason
    return ""
