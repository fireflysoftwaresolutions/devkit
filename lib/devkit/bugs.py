# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""devkit's own bugs: recognize a failure of devkit itself, look it up in the public registry, share what helps.

When devkit fails (a `dev` command raises, a hook raises or times out, the guard refuses what it should allow), the
failure gets a fingerprint: a short hash of its kind, exception class and devkit stack frames (`module.function`, no
line numbers), or of the guard's refusal id and the public command word. Each hit on this machine is an occurrence
with a random one-time code; the marker line `devkit-bug: <fingerprint> <code>` names it in tool output, and only a
marker whose code matches an occurrence recorded here, in this session, in the last ten minutes, means anything.

Safety rules:
- Local state lives under STATE/bugs (occurrences, cache, outbox, consent) and STATE/crashes; nothing here is sent
  by itself.
- A source checkout keeps only the stack frames of functions that the latest release already had, so a developer's
  own functions never leave this machine (they become `<local>`).
- Standard library only, Python 3.9 compatible; hooks read the cache and never call the network.
"""
from __future__ import annotations

import ast
import functools
import getpass
import hashlib
import json
import os
import platform
import re
import secrets
import socket
import string
import sys
import time
import unicodedata
from pathlib import Path

from . import core

BUGS = core.STATE / "bugs"
CRASHES = core.STATE / "crashes"
SOURCE = core.DEVKIT_HOME  # the devkit copy that runs; frames under its lib/devkit are devkit's own
KINDS = ("crash", "hook", "guard", "command")
IMPACT = {"blocked": 3.0, "detoured": 1.0, "minor": 0.3}
LOCAL = "<local>"
MAX_FRAMES = 5
MATCH_WINDOW = 600  # seconds an occurrence code stays valid for a marker in tool output
MARKER_RE = re.compile(r"^devkit-bug: ([0-9a-f]{12}) ([a-z0-9]{6})\r?$", re.M)
_CODE_CHARS = string.ascii_lowercase + string.digits
_TAIL_BYTES = 262144  # recent occurrences are at the end of the file; a marker check reads no more than this


# --------------------------------------------------------------------------- fingerprints and frames

def fingerprint(kind: str, *, exc_class: str = "", frames: tuple = (), refusal_id: str = "", word: str = "") -> str:
    """12 hex characters of sha256 over `kind|exc_class|frames…|refusal_id|word`: the same failure gives the same
    value on every machine, and on every version where its functions or refusal id keep their names."""
    text = "|".join([kind, exc_class, *frames, refusal_id, word])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def install_channel() -> str:
    """`release` when devkit runs from a pinned copy (a folder of core.pin_root()), else `checkout`."""
    here = Path(os.path.realpath(SOURCE))
    return "release" if Path(os.path.realpath(core.pin_root())) in here.parents else "checkout"


def devkit_frames(tb) -> tuple:
    """The devkit frames of a traceback as `module.function`, innermost first, at most five. Frames whose file is not
    under lib/devkit are left out. In a source checkout, a frame whose function the latest release did not have is
    `<local>`, so a developer's own code is never named."""
    lib = os.path.realpath(SOURCE / "lib" / "devkit") + os.sep
    found = []
    while tb is not None:
        code = tb.tb_frame.f_code
        path = os.path.realpath(code.co_filename)
        if path.startswith(lib) and path.endswith(".py"):
            rel = path[len(lib):-3].replace(os.sep, "/")
            module = rel[:-len("/__init__")] if rel.endswith("/__init__") else rel
            found.append((module.replace("/", "."), code.co_name, f"lib/devkit/{rel}.py"))
        tb = tb.tb_next
    found = found[::-1][:MAX_FRAMES]
    checkout = install_channel() == "checkout"
    out = []
    for module, name, rel in found:
        if checkout:
            released = _released_functions(rel)
            if released is None or not (name.startswith("<") or name in released):
                out.append(LOCAL)
                continue
        out.append(f"{module}.{name}")
    return tuple(out)


def _source_version() -> str:
    try:
        return (SOURCE / "VERSION").read_text().strip() or core.VERSION
    except OSError:
        return core.VERSION


def _released_functions(rel: str):
    """The function names `rel` (lib/devkit/<module>.py) defined at the latest release tag, or None when the file
    did not exist there or the tag cannot be read. Kept per tag in BUGS/release-functions.json."""
    tag = f"v{_source_version()}"
    cache_file = BUGS / "release-functions.json"
    cache = core.read_json(cache_file, {}) or {}
    if cache.get("tag") != tag or not isinstance(cache.get("files"), dict):
        cache = {"tag": tag, "files": {}}
    files = cache["files"]
    if rel in files:
        return None if files[rel] is None else set(files[rel])
    rc, out, _ = core.git(SOURCE, "show", f"{tag}:{rel}")
    if rc != 0:
        if core.git(SOURCE, "rev-parse", "-q", "--verify", f"refs/tags/{tag}")[0] != 0:
            return None  # no such tag here (yet): nothing is kept, and nothing is remembered
        files[rel] = None  # the file is newer than the release
    else:
        try:
            tree = ast.parse(out)
        except SyntaxError:
            return None
        files[rel] = sorted({n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))})
    _write_json(cache_file, cache)
    return None if files[rel] is None else set(files[rel])


# --------------------------------------------------------------------------- local files

def _write_json(path: Path, data, mode: int = 0o600) -> bool:
    """Write JSON atomically (a temporary file, then a rename), private to the owner. False when it cannot."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, separators=(",", ":"), sort_keys=True)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _create(path: Path, text: str, mode: int = 0o600) -> bool:
    """Create `path` whole, or not at all: a temporary file linked into place. False when `path` already exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.link(tmp, path)
        return True
    except FileExistsError:
        return False
    finally:
        os.unlink(tmp)


def _append_line(path: Path, obj) -> bool:
    """Append one JSON line with a single write under O_APPEND, so lines from concurrent writers never interleave."""
    data = (json.dumps(obj, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            return os.write(fd, data) == len(data)
        finally:
            os.close(fd)
    except OSError:
        return False


def _tail_records(path: Path, limit: int = _TAIL_BYTES) -> list:
    """The JSON lines at the end of `path` (at most `limit` bytes), oldest first; a torn or foreign line is skipped."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            data = fh.read()
    except OSError:
        return []
    lines = data.split(b"\n")
    if size > limit:
        lines = lines[1:]  # the first line may start mid-record
    out = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


# --------------------------------------------------------------------------- this machine and its occurrences

def install_id() -> str:
    """A random id for this machine's devkit, created once (0600). It never leaves the machine as such."""
    path = BUGS / "install-id"
    try:
        value = path.read_text().strip()
        if re.fullmatch(r"[0-9a-f]{32}", value):
            return value
    except OSError:
        pass
    value = secrets.token_hex(16)
    if path.exists():  # unreadable or damaged: replace it whole
        _write_text(path, value + "\n")
        return value
    if not _create(path, value + "\n"):  # another process created it first: theirs wins
        return path.read_text().strip()
    return value


def _write_text(path: Path, text: str, mode: int = 0o600):
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def record_occurrence(kind: str, fp: str, *, impact: str = "blocked", command: str = "", event: str = "",
                      refusal_id: str = "", word: str = "", exc_class: str = "", frames: tuple = (),
                      session: str = "") -> dict:
    """Record one hit of `fp` on this machine with a fresh one-time code, in BUGS/occurrences.jsonl. -> the record,
    with `saved` False when the state folder could not be written (the code then matches nothing)."""
    rec = {"ts": round(time.time(), 3), "kind": kind, "fingerprint": fp,
           "code": "".join(secrets.choice(_CODE_CHARS) for _ in range(6)), "impact": impact, "session": session,
           "version": core.VERSION, "command": command, "event": event, "refusal_id": refusal_id, "word": word,
           "exc_class": exc_class, "frames": list(frames)}
    saved = _append_line(BUGS / "occurrences.jsonl", rec)
    return {**rec, "saved": saved}


def occurrence_for(fp: str, code: str, session: str, within_s: int = MATCH_WINDOW, now: float = None):
    """The occurrence of `fp` with this `code`, recorded in this `session` in the last `within_s` seconds, or None.
    A marker that names anything else (another session, an old or unknown code) means nothing."""
    if not (fp and code and session):
        return None
    now = time.time() if now is None else now
    for rec in reversed(_tail_records(BUGS / "occurrences.jsonl")):
        if rec.get("fingerprint") == fp and rec.get("code") == code:
            ts = rec.get("ts")
            fresh = isinstance(ts, (int, float)) and now - within_s <= ts <= now + 60
            return rec if fresh and rec.get("session") == session else None
    return None


# --------------------------------------------------------------------------- privacy: what a report may carry
#
# A report is built from fields devkit produces itself, each checked against a closed vocabulary or a strict
# pattern; a value that does not fit is left empty (or `<local>` for a frame). Free text (a title, a description, a
# detour's free step, an exception message) is templated or scrubbed against this machine's private words and is
# always held for the developer's review before its first send.

REPORT_KEYS = ("version", "channel", "macos", "python", "agent", "surface", "command", "exc_class", "frames",
               "refusal_id", "word", "event", "exit", "duration", "fingerprint", "impact", "kind")
AGENTS = ("claude", "codex")
_VERSION_RE = re.compile(r"(\d{2}\.\d{1,2}\.\d+)(?:[-+][0-9A-Za-z.-]*)?")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_FRAME_RE = re.compile(r"[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*\.(?:<[a-z]+>|[A-Za-z_][A-Za-z0-9_]{0,63})")
_REFUSAL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}(?:[:.][A-Za-z0-9_-]{1,40})?")
_FP_RE = re.compile(r"[0-9a-f]{12}")
_DURATIONS = ((1, "<1s"), (10, "1-10s"), (60, "10-60s"), (600, "1-10m"))
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9._+-]{0,30}")
# the guard's sets of command words it reasons about by name; with the common tools below, the public vocabulary
_GUARD_WORD_SETS = ("COMMAND_WORDS", "PREFIX_CMDS", "_FILTERS", "_CHECK_TOOLS", "_PATH_TAKERS", "_POSIX_SHELLS",
                    "_READERS", "_CREATORS", "_CHANGERS", "_COPIERS", "_MOVERS", "_SHELLS", "_KNOWN_PROGRAMS",
                    "_JS_RUNTIMES", "_AGENT_CLIS", "_EXPORTERS")
_COMMON_WORDS = frozenset("""git gh dev sed awk gawk find xargs python python3 pip pip3 uv uvx node npm npx pnpm yarn
    bun deno brew make docker colima kubectl terraform curl wget open osascript perl ruby php composer mvn mvnw gradle
    gradlew java go cargo yq jq claude codex ssh scp rsync tar zip unzip gzip kill pkill launchctl defaults plutil
    chmod rm mv cp ln mkdir touch cat head tail grep rg ls echo printf env sudo bash sh zsh pytest ruff mypy tsc
    eslint""".split())

_URL_TEXT = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>`]+|\bwww\.[^\s'\"<>`]+")
_EMAIL_TEXT = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_PATH_TEXT = re.compile(r"[^\s'\"()<>\[\]{},;`]*[/\\][^\s'\"()<>\[\]{},;`]*")
_QUOTED_TEXT = re.compile(r"'[^'\n]*'|\"[^\"\n]*\"|`[^`\n]*`")
_RUN_TEXT = re.compile(r"[A-Za-z0-9_+/=-]{16,}")
_HEX_RUN = re.compile(r"[0-9A-Fa-f]{16,}")
_NUM_TEXT = re.compile(r"\d+")
_TEMPLATE_MAX = 200

_PUBLIC_HOSTS = frozenset({"github.com", "gitlab.com", "bitbucket.org", "dev.azure.com", "ssh.dev.azure.com",
                           "vs-ssh.visualstudio.com"})
_GENERIC_WORDS = frozenset({"main", "master", "develop", "trunk", "gh-pages", "head", "origin", "local", "localhost",
                            "localdomain", "lan", "home", "users", "development", "wip", "feat", "fix", "refactor",
                            "perf", "docs", "test", "build", "chore", "deps", "style", "revert", "hotfix", "release",
                            "integration"})
_SECURITY_TEXT = re.compile(r"\b(bypass\w*|allow\w*|inject\w*|token\w*|secret\w*|exploit\w*)\b", re.I)


@functools.lru_cache(maxsize=1)
def _public_words() -> frozenset:
    from . import guard
    words = set(_COMMON_WORDS)
    for name in _GUARD_WORD_SETS:
        value = getattr(guard, name, ())
        if isinstance(value, (set, frozenset, tuple, list)):
            words |= {w for w in value if isinstance(w, str)}
    return frozenset(w for w in words if _WORD_RE.fullmatch(w))


def public_word(word: str) -> str:
    """A command word as a report may carry it: the word itself when the guard knows it (`sed`, `git`, `python3`),
    `<path>` for a path, else `<program>`."""
    w = (word or "").strip()
    if not w:
        return ""
    if "/" in w or "\\" in w or w.startswith((".", "~")):
        return "<path>"
    return w if w in _public_words() else "<program>"


@functools.lru_cache(maxsize=1)
def _dev_tree() -> dict:
    """{dev subcommand: (its parser, {its subcommand: parser})} from devkit's own parser."""
    import argparse
    from . import cli

    def subs(p) -> dict:
        for act in p._actions:
            if isinstance(act, argparse._SubParsersAction):
                return dict(act.choices)
        return {}
    return {name: (p, subs(p)) for name, p in subs(cli.build_parser()).items() if not name.startswith("_")}


def _public_command(command) -> str:
    """The first one or two words of a `dev` command as devkit's parser names them, or ''."""
    words = str(command or "").split()
    tree = _dev_tree()
    if not words or words[0] not in tree:
        return ""
    if len(words) > 1 and words[1] in tree[words[0]][1]:
        return f"{words[0]} {words[1]}"
    return words[0]


@functools.lru_cache(maxsize=1)
def _surfaces() -> frozenset:
    from .canary import SURFACES
    return frozenset(SURFACES.values()) | {"ChatGPT app", "unknown"}


@functools.lru_cache(maxsize=1)
def _hook_events() -> frozenset:
    from .hooks import HANDLERS
    return frozenset(HANDLERS)


def _public_version(value) -> str:
    for v in (value, core.VERSION):
        m = _VERSION_RE.fullmatch(str(v or "").strip())
        if m:
            return m.group(1)
    return ""


def _duration(value) -> str:
    if isinstance(value, str):
        return value if value in {b for _, b in _DURATIONS} | {">10m"} else ""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return ""
    return next((b for limit, b in _DURATIONS if value < limit), ">10m")


def _frames(value) -> list:
    frames = value if isinstance(value, (list, tuple)) else []
    return [f if isinstance(f, str) and (f == LOCAL or _FRAME_RE.fullmatch(f)) else LOCAL for f in frames][:MAX_FRAMES]


def _fits(value, pattern) -> str:
    return value if isinstance(value, str) and pattern.fullmatch(value) else ""


def build_report(occ: dict) -> dict:
    """A report from an occurrence: only REPORT_KEYS, each value checked against its vocabulary or pattern. Nothing
    else in `occ` (a working folder, arguments, a message) is ever copied."""
    exit_code = occ.get("exit")
    mac = platform.mac_ver()[0].split(".")[0]
    return {
        "version": _public_version(occ.get("version")),
        "channel": install_channel(),
        "macos": mac if mac.isdigit() else "",
        "python": f"{sys.version_info[0]}.{sys.version_info[1]}",
        "agent": occ.get("agent") if occ.get("agent") in AGENTS else "",
        "surface": occ.get("surface") if occ.get("surface") in _surfaces() else "",
        "command": _public_command(occ.get("command")),
        "exc_class": _fits(occ.get("exc_class"), _IDENT_RE),
        "frames": _frames(occ.get("frames")),
        "refusal_id": _fits(occ.get("refusal_id"), _REFUSAL_RE),
        "word": public_word(occ.get("word") if isinstance(occ.get("word"), str) else ""),
        "event": occ.get("event") if occ.get("event") in _hook_events() else "",
        "exit": exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) and 0 <= exit_code < 256
        else None,
        "duration": _duration(occ.get("duration")),
        "fingerprint": _fits(occ.get("fingerprint"), _FP_RE),
        "impact": occ.get("impact") if occ.get("impact") in IMPACT else "blocked",
        "kind": occ.get("kind") if occ.get("kind") in KINDS else "",
    }


def _run_placeholder(m) -> str:
    run = m.group(0)
    if run.isalpha() and run.islower() and not _HEX_RUN.fullmatch(run):
        return run  # a long ordinary word, not a token
    return "<hex>"


@functools.lru_cache(maxsize=8)
def _deny_re(deny: frozenset):
    words = sorted((w for w in deny if w), key=len, reverse=True)
    return re.compile("|".join(re.escape(w) for w in words), re.I) if words else None


def template_message(msg: str, deny: frozenset = None) -> str:
    """An exception message with its values replaced, in this order: URLs, e-mail addresses, paths (anything with
    `/` or `\\`), quoted strings, hex or base64 runs of 16 or more characters, numbers. '' when a word of the deny
    list (default: deny_words()) is still in it: the whole template is dropped, never patched."""
    s = _URL_TEXT.sub("<url>", str(msg or ""))
    s = _EMAIL_TEXT.sub("<email>", s)
    s = _PATH_TEXT.sub("<path>", s)
    s = _QUOTED_TEXT.sub("<str>", s)
    s = _RUN_TEXT.sub(_run_placeholder, s)
    s = _NUM_TEXT.sub("<num>", s)
    s = " ".join(s.split())[:_TEMPLATE_MAX]
    rx = _deny_re(deny_words() if deny is None else frozenset(deny))
    return "" if rx is not None and rx.search(s) else s


def scrub_text(text: str, deny: frozenset) -> tuple:
    """Free text with URLs, e-mail addresses, paths, tokens and this machine's private words replaced -> (scrubbed,
    held). `held` is True for any non-empty text: free text always waits for the developer's review before its
    first send."""
    if not text or not str(text).strip():
        return "", False
    s = _URL_TEXT.sub("<url>", str(text).strip())
    s = _EMAIL_TEXT.sub("<email>", s)
    s = _PATH_TEXT.sub("<path>", s)
    s = _RUN_TEXT.sub(_run_placeholder, s)
    rx = _deny_re(frozenset(deny or ()))
    if rx is not None:
        s = rx.sub("<private>", s)
    return s, True


def deny_words() -> frozenset:
    """This machine's private words, in lower case: project ids, owners and repository names of the projects map,
    their local branch names and remote URLs, the handoff repository's project ids and initiative slugs, the user and
    host names and the home folder. Words shorter than 3 characters, devkit's own repository, public hosts, default
    branches and branch type prefixes are left out."""
    devkit_parts = set(core.DEVKIT_REPO.lower().split("/")) | {core.DEVKIT_REPO.lower()}
    words = set()

    def add(value, split: str = ""):
        value = str(value or "").strip().lower()
        if not value:
            return
        parts = [value] + ([p for p in value.split(split) if p] if split else [])
        for p in parts:
            if len(p) >= 3 and p not in _GENERIC_WORDS and p not in _PUBLIC_HOSTS and p not in devkit_parts \
                    and not re.fullmatch(r"[\d.]+", p):
                words.add(p)

    def add_remote(url):
        add(url)
        parsed = core.parse_remote(url or "")
        if parsed:
            host, owner_path, repo = parsed
            add(host)
            add(owner_path, "/")
            add(repo)

    reg = core.read_json(core.HANDOFF / "registry.json", {}) or {}
    for p in reg.get("projects") or [] if isinstance(reg, dict) else []:
        if not isinstance(p, dict):
            continue
        add(p.get("id"), "/")
        add(p.get("owner"), "/")
        add(p.get("name"))
        add_remote(p.get("remote"))
        for b in (p.get("branches") or []) + (p.get("worktrees") or []):
            if isinstance(b, dict):
                add(b.get("name") or b.get("branch"), "/")
    projects = core.HANDOFF / "projects"
    for f in projects.rglob("*.md") if projects.is_dir() else []:
        add(f.relative_to(projects).with_suffix("").as_posix(), "/")
    initiatives = core.HANDOFF / "initiatives"
    for d in initiatives.iterdir() if initiatives.is_dir() else []:
        if d.is_dir():
            add(d.name)
    try:
        add(getpass.getuser())
    except Exception:  # getuser raises when no user name can be found: then there is none to deny
        pass
    add(socket.gethostname(), ".")
    home = Path.home()
    add(str(home))
    add(home.name)
    return frozenset(words)


def is_security(occ: dict, text: str = "") -> bool:
    """True when a failure must go to private vulnerability reporting, never to Issues: frames in the guard or the
    crew scope code or the guard hook, a guard bug that allowed something, a guard fail-open, a report filed as a
    security report, or text about bypassing, allowing, injecting, tokens, secrets or exploits."""
    for f in occ.get("frames") or ():
        if isinstance(f, str) and (f.split(".", 1)[0] in ("guard", "scope") or f.startswith(("hooks._guard",
                                                                                           "hooks.guard"))):
            return True
    if occ.get("kind") == "guard" and occ.get("outcome") == "allow":
        return True
    if occ.get("event") in ("guard", "guard.failopen") or occ.get("failopen") or occ.get("security"):
        return True
    texts = [text] + [occ.get(k) for k in ("title", "text", "description", "template")]
    return any(isinstance(t, str) and _SECURITY_TEXT.search(t) for t in texts)


# --------------------------------------------------------------------------- detours: their form
#
# A detour is one to five steps, each a hint from a fixed set of templates with typed slots, a `dev` command made of
# devkit's own subcommands and value-free flags, or free text of at most 200 characters. The same validator runs
# here and in the registry workflow.

HINTS = {
    "use-tool": "Use the {tool} tool instead of `{word}`.",
    "from-root": "Run `dev {dev}` from the repository root.",
    "one-at-a-time": "Run each `dev {dev}` command on its own, not chained with other commands.",
    "scratch-tool": "Write scratch files with the {tool} tool in the session scratchpad.",
    "sync-retry": "Run `dev bug sync`, then retry `dev {dev}`.",
    "ask-update": "Ask the developer to run `dev update`: the fix is in {version}.",
}
HINT_TOOLS = ("Edit", "Write", "Read", "Grep", "Glob")
MAX_STEPS = 5
MAX_TEXT = 200
_SLOT_RE = re.compile(r"\{(\w+)\}")
_HINT_VERSION_RE = re.compile(r"\d{2}\.\d{1,2}\.\d+")
_URLISH = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://|\bwww\.", re.I)
_SHELL_FORMS = ("|", ">", "<", ";", "&&", "$(", "`")
_PROTECTED_PATH = re.compile(r"\.claude\b|\.codex\b|\.local/share/devkit|\.config/devkit", re.I)
_BIDI = frozenset({"R", "AL", "RLE", "RLO", "RLI", "LRE", "LRO", "LRI", "PDF", "PDI", "FSI"})


def _char_problem(c: str) -> str:
    """'' for printable ASCII or a letter; else what is wrong with the character (control, right-to-left and
    zero-width characters included)."""
    if " " <= c <= "~":
        return ""
    if unicodedata.bidirectional(c) in _BIDI:
        return "a right-to-left character"
    return "" if unicodedata.category(c).startswith("L") else f"the character U+{ord(c):04X}"


def _text_problems(value: str) -> list:
    """What makes a step's text unsafe to show an agent: a URL, a shell form, a protected path, or a character
    outside printable ASCII and letters."""
    out = []
    if _URLISH.search(value):
        out.append("a URL")
    out += [f"`{f}`" for f in _SHELL_FORMS if f in value]
    if _PROTECTED_PATH.search(value):
        out.append("a path under an agent's or devkit's configuration folder")
    bad = next((p for p in map(_char_problem, value) if p), "")
    return out + ([bad] if bad else [])


def _value_free_flags(parser) -> set:
    return {o for act in parser._actions if act.option_strings and act.nargs == 0 for o in act.option_strings} - \
        {"-h", "--help"}


def _dev_path_ok(value) -> bool:
    """A `dev` subcommand path of one or two words, as devkit's parser names it."""
    return isinstance(value, str) and 1 <= len(value.split()) <= 2 and " ".join(value.split()) == value and \
        _public_command(value) == value


def _dev_step_problems(value: str) -> list:
    words = value.split()
    tree = _dev_tree()
    if not words or words[0] not in tree:
        return ["not a dev subcommand"]
    parser, subs = tree[words[0]]
    rest = words[1:]
    if rest and rest[0] in subs:
        parser, rest = subs[rest[0]], rest[1:]
    flags = _value_free_flags(parser)
    return [f"`{w}` is not a flag of this command that takes no value" for w in rest if w not in flags]


_SLOT_CHECKS = {
    "tool": lambda v: v in HINT_TOOLS,
    "word": lambda v: isinstance(v, str) and bool(v) and public_word(v) == v,
    "dev": _dev_path_ok,
    "version": lambda v: isinstance(v, str) and bool(_HINT_VERSION_RE.fullmatch(v)),
}


def _step_problems(step) -> list:
    if not isinstance(step, dict):
        return ["not a step object"]
    if not all(isinstance(v, str) for v in step.values()):
        return ["every value must be text"]
    out = [p for v in step.values() for p in _text_problems(v)]
    if "hint" in step:
        template = HINTS.get(step["hint"])
        if template is None:
            return out + [f"unknown hint {step['hint']!r}"]
        slots = set(_SLOT_RE.findall(template))
        if set(step) != slots | {"hint"}:
            return out + [f"hint {step['hint']} takes exactly: {', '.join(sorted(slots)) or 'no slots'}"]
        out += [f"slot {k} has a value it does not allow" for k in sorted(slots) if not _SLOT_CHECKS[k](step[k])]
    elif set(step) == {"dev"}:
        out += _dev_step_problems(step["dev"])
    elif set(step) == {"text"}:
        if not step["text"].strip() or len(step["text"]) > MAX_TEXT:
            out.append(f"free text must have 1 to {MAX_TEXT} characters")
    else:
        out.append("a step is one of {hint, slots…}, {dev} or {text}")
    return out


def validate_steps(steps: list) -> list:
    """The problems of a detour's steps, as error strings; [] when it is valid (1 to 5 well-formed steps)."""
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        return [f"a detour has 1 to {MAX_STEPS} steps"]
    return [f"step {i}: {p}" for i, step in enumerate(steps, 1) for p in _step_problems(step)]


def render_step(step: dict) -> str:
    """A valid step as the sentence an agent reads."""
    if "hint" in step:
        return HINTS[step["hint"]].format(**{k: v for k, v in step.items() if k != "hint"})
    if "dev" in step:
        return f"Run `dev {step['dev']}`."
    return step["text"]


# --------------------------------------------------------------------------- payloads: what a client posts

SCHEMA_VERSION = 1
PAYLOAD_TYPES = ("report", "vote", "detour", "confirm", "failed")
TOOL_NAMES = ("Bash", "Edit", "Write", "Read", "Grep", "Glob", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch",
              "Agent", "Task", "Monitor", "exec_command", "shell", "apply_patch", "write_stdin")
EVIDENCE_COUNTS = ("successes", "failures", "attempts")
_COMMON_FIELDS = {"devkit_bug", "type", "fingerprint", "version"}
_FREE_FIELDS = ("title", "text", "template")
_FIELDS = {
    "report": _COMMON_FIELDS | set(REPORT_KEYS) | set(_FREE_FIELDS),
    "vote": _COMMON_FIELDS | {"impact", "window"},
    "detour": _COMMON_FIELDS | {"impact", "steps", "evidence"},
    "confirm": _COMMON_FIELDS | {"detour_id"},
    "failed": _COMMON_FIELDS | {"detour_id", "impact"},
}
_REQUIRED = {"report": {"impact", "kind"}, "vote": {"impact", "window"}, "detour": {"steps", "evidence"},
             "confirm": {"detour_id"}, "failed": {"detour_id"}}
_WINDOW_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DETOUR_ID_RE = re.compile(r"d\d{1,3}")


def _evidence_problems(ev) -> list:
    if not isinstance(ev, dict) or not set(ev) <= set(EVIDENCE_COUNTS) | {"tools"}:
        return [f"evidence holds only {', '.join(EVIDENCE_COUNTS)} and tools"]
    out = [f"evidence {k} must be a count" for k in EVIDENCE_COUNTS
           if k in ev and (isinstance(ev[k], bool) or not isinstance(ev[k], int) or not 0 <= ev[k] <= 10000)]
    tools = ev.get("tools", [])
    if not isinstance(tools, list) or len(tools) > 10 or any(t not in TOOL_NAMES for t in tools):
        out.append("evidence tools must be agent tool names")
    return out


def _free_text_ok(v) -> bool:
    return isinstance(v, str) and bool(v.strip()) and len(v) <= MAX_TEXT and not any(map(_char_problem, v))


_REPORT_CHECKS = {
    "channel": lambda v: v in ("release", "checkout"),
    "macos": lambda v: isinstance(v, str) and (v == "" or (v.isdigit() and len(v) <= 3)),
    "python": lambda v: isinstance(v, str) and (v == "" or re.fullmatch(r"\d\.\d{1,2}", v) is not None),
    "agent": lambda v: v in AGENTS or v == "",
    "surface": lambda v: v == "" or v in _surfaces(),
    "command": lambda v: v == "" or (isinstance(v, str) and _public_command(v) == v),
    "exc_class": lambda v: v == "" or _fits(v, _IDENT_RE) == v,
    "frames": lambda v: isinstance(v, list) and len(v) <= MAX_FRAMES and _frames(v) == v,
    "refusal_id": lambda v: v == "" or _fits(v, _REFUSAL_RE) == v,
    "word": lambda v: v in ("", "<path>", "<program>") or (isinstance(v, str) and public_word(v) == v),
    "event": lambda v: v == "" or v in _hook_events(),
    "exit": lambda v: v is None or (isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 256),
    "duration": lambda v: v == "" or (isinstance(v, str) and _duration(v) == v),
    "kind": lambda v: v in KINDS,
    "title": _free_text_ok,
    "text": _free_text_ok,
    "template": _free_text_ok,
}


def validate_payload(obj: dict) -> list:
    """The problems of a client payload, as error strings; [] when it is valid. Unknown fields are rejected."""
    if not isinstance(obj, dict):
        return ["a payload is a JSON object"]
    if obj.get("devkit_bug") != SCHEMA_VERSION or isinstance(obj.get("devkit_bug"), bool):
        return [f"devkit_bug must be {SCHEMA_VERSION}"]
    kind = obj.get("type")
    if kind not in PAYLOAD_TYPES:
        return [f"type must be one of {', '.join(PAYLOAD_TYPES)}"]
    out = [f"unknown field {k}" for k in sorted(set(obj) - _FIELDS[kind])]
    out += [f"missing field {k}" for k in sorted((_COMMON_FIELDS | _REQUIRED[kind]) - set(obj))]
    if "fingerprint" in obj and not _fits(obj["fingerprint"], _FP_RE):
        out.append("fingerprint must be 12 lowercase hex characters")
    if "version" in obj and not _fits(obj["version"], _HINT_VERSION_RE):
        out.append("version must look like 26.11.7")
    if "impact" in obj and obj["impact"] not in IMPACT:
        out.append(f"impact must be one of {', '.join(IMPACT)}")
    if "window" in obj and not _fits(obj["window"], _WINDOW_RE):
        out.append("window must be a day, YYYY-MM-DD")
    if "detour_id" in obj and not _fits(obj["detour_id"], _DETOUR_ID_RE):
        out.append("detour_id must look like d1")
    if "steps" in obj:
        out += validate_steps(obj["steps"])
    if "evidence" in obj:
        out += _evidence_problems(obj["evidence"])
    if kind == "report":
        out += [f"report field {k} has a value it does not allow" for k, ok in _REPORT_CHECKS.items()
                if k in obj and k not in _COMMON_FIELDS and not ok(obj[k])]
    return out


# --------------------------------------------------------------------------- the registry record
#
# The registry workflow keeps one comment per bug, written by its bot account: a summary line, the marker, and one
# fenced JSON block. Backticks, angle brackets and @ are written as \u escapes, so no value can close the fence or
# the marker, mention someone, or add a second block. Clients read exactly one block, and only from the bot.

RECORD_MARK = "<!-- devkit-bug v1 -->"
BOT_LOGIN = "github-actions[bot]"
TRUSTED = frozenset({"OWNER", "MEMBER"})
BOOSTED_COMPONENTS = ("hooks", "progress", "commit")
WINDOW_DAYS = 30
HALF_LIFE_DAYS = 14
MAX_DETOURS = 20
MAX_MARKS = 50
MAX_VERSIONS = 20
_BLOCK_RE = re.compile(r"^```json\n(.*?)\n```$", re.M | re.S)
_SUMMARY_SAFE = re.compile(r"[^A-Za-z0-9._ -]")
_TITLE_SAFE = re.compile(r"[^A-Za-z0-9._ :-]")


def _json_block(obj) -> str:
    text = json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    for ch, esc in (("`", "\\u0060"), ("<", "\\u003c"), (">", "\\u003e"), ("@", "\\u0040")):
        text = text.replace(ch, esc)
    return f"```json\n{text}\n```"


def _one_block(body):
    if not isinstance(body, str) or body.count("```") != 2:
        return None
    blocks = _BLOCK_RE.findall(body)
    if len(blocks) != 1:
        return None
    try:
        obj = json.loads(blocks[0])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def render_record(rec: dict) -> str:
    """A record as the bot's comment: a summary line for people, the marker, and one escaped JSON block."""
    def safe(v):
        return _SUMMARY_SAFE.sub("", str(v if v is not None else ""))[:40]
    people = len(rec.get("voters") or {})
    summary = (f"**Bug {safe(rec.get('fingerprint'))}** · {safe(rec.get('kind'))} · {safe(rec.get('component'))} · "
               f"{people} {'person' if people == 1 else 'people'} · score {float(rec.get('score') or 0):.1f} · "
               f"{safe(rec.get('status'))}" + (f" · fixed in {safe(rec['fixed_in'])}" if rec.get("fixed_in") else ""))
    return f"{summary}\n\n{RECORD_MARK}\n{_json_block(rec)}"


def parse_record(body: str):
    """The record of a bot comment: exactly one marker, then exactly one JSON block naming a fingerprint; else None."""
    if not isinstance(body, str) or body.count(RECORD_MARK) != 1:
        return None
    rec = _one_block(body[body.index(RECORD_MARK):]) if body.count("```") == 2 else None
    if rec is None or rec.get("devkit_bug") != SCHEMA_VERSION or not _fits(rec.get("fingerprint"), _FP_RE):
        return None
    return rec


def render_payload(payload: dict) -> str:
    """A client payload as the body of an issue or comment: one escaped JSON block."""
    return _json_block(payload)


def parse_payload(body: str):
    """The payload of an issue or comment body: exactly one JSON block with devkit_bug; else None. The caller
    still validates it."""
    obj = _one_block(body)
    return obj if obj is not None and "devkit_bug" in obj else None


def title_for(report: dict) -> str:
    """The generated title of a bug, from its report's allowlisted fields only."""
    kind, cmd = report.get("kind"), report.get("command")
    exc = report.get("exc_class") or ""
    if kind == "crash":
        title = (f"crash in dev {cmd}" + (f": {exc}" if exc else "")) if cmd else f"crash: {exc or 'unknown'}"
    elif kind == "hook":
        title = f"hook {report.get('event') or 'unknown'}: {exc or 'timeout'}"
    elif kind == "guard":
        title = f"guard refusal {report.get('refusal_id') or 'unknown'}" + (
            f" on {report['word']}" if report.get("word") else "")
    else:
        title = f"dev {cmd} misbehaves" if cmd else "a dev command misbehaves"
    return _TITLE_SAFE.sub("", title)[:100]


def component_of(report: dict) -> str:
    """The part of devkit a bug is in: the `dev` command's first word, `hooks` for a hook, `guard` for a refusal,
    else the module of its innermost devkit frame."""
    if report.get("command"):
        return report["command"].split()[0]
    if report.get("kind") == "hook":
        return "hooks"
    if report.get("kind") == "guard":
        return "guard"
    frame = next((f for f in report.get("frames") or [] if f != LOCAL), "")
    return frame.rsplit(".", 1)[0] if frame else str(report.get("kind") or "")


def trust_of(detour: dict, confirmations: list) -> str:
    """`retired` when members' failure marks outnumber trusted confirmations; `verified` when an OWNER or MEMBER
    other than the author confirmed it; `team` when every step is a hint, the author is OWNER or MEMBER and evidence
    is present; else `proposed`."""
    author = detour.get("author_hash")
    confirmed = {c.get("author_hash") for c in confirmations or []
                 if isinstance(c, dict) and c.get("author_assoc") in TRUSTED and c.get("author_hash")
                 and c.get("author_hash") != author}
    failed = {f.get("author_hash") for f in detour.get("failures") or []
              if isinstance(f, dict) and f.get("author_assoc") in TRUSTED and f.get("author_hash")}
    if len(failed) > len(confirmed):
        return "retired"
    if confirmed:
        return "verified"
    steps = detour.get("steps") or []
    if steps and all(isinstance(s, dict) and "hint" in s for s in steps) and \
            detour.get("author_assoc") in TRUSTED and detour.get("evidence"):
        return "team"
    return "proposed"


def score(rec: dict, now: float) -> float:
    """Each voter of the last 30 days counts with the weight of their worst impact (blocked 3, detoured 1, minor
    0.3), halved every 14 days since their vote; crashes and hook failures on the paths every session runs (hooks,
    dev progress, dev commit) count 1.5 times."""
    total = 0.0
    for v in (rec.get("voters") or {}).values():
        if not isinstance(v, dict) or not isinstance(v.get("ts"), (int, float)) or v.get("impact") not in IMPACT:
            continue
        age = max(0.0, now - v["ts"]) / 86400
        if age <= WINDOW_DAYS:
            total += IMPACT[v["impact"]] * 0.5 ** (age / HALF_LIFE_DAYS)
    if rec.get("kind") in ("hook", "crash") and rec.get("component") in BOOSTED_COMPONENTS:
        total *= 1.5
    return total


def _worse(a, b):
    return a if IMPACT.get(a, 0) >= IMPACT.get(b, 0) else b


def _new_record(report: dict, now: float) -> dict:
    facts = ("command", "exc_class", "frames", "refusal_id", "word", "event")
    return {"devkit_bug": SCHEMA_VERSION, "fingerprint": report["fingerprint"], "kind": report["kind"],
            "component": component_of(report), "title": title_for(report), "first_version": report["version"],
            "versions": [], "voters": {}, "impacts": {k: 0 for k in IMPACT}, "score": 0.0, "last_seen": now,
            "status": "open", "fixed_in": "", "detours": [], "report": {k: report[k] for k in facts if report.get(k)}}


def _add_vote(rec: dict, who: str, impact: str, version: str, trusted: bool, now: float):
    voters = rec.setdefault("voters", {})
    old = voters.get(who) or {}
    voters[who] = {"impact": _worse(old.get("impact"), impact), "ts": now}
    for h in [h for h, v in voters.items() if now - (v.get("ts") or 0) > 90 * 86400]:
        del voters[h]  # a voter quiet for 90 days no longer counts anywhere
    versions = rec.setdefault("versions", [])
    if version not in versions:
        versions.append(version)
        versions.sort(key=lambda v: core.version_key(v) or (0,))
        del versions[:-MAX_VERSIONS]
    rec["last_seen"] = now
    fixed, seen_at = core.version_key(rec.get("fixed_in")), core.version_key(version)
    if fixed and seen_at and seen_at >= fixed:
        regressed = rec.setdefault("regression", [])
        if who not in regressed:
            regressed.append(who)
        if trusted or len(regressed) >= 3:
            rec["status"] = "regressed"


def fold(rec: dict, payload: dict, *, author_hash: str, author_assoc: str, now: float) -> dict:
    """The record after one payload, without changing `rec`. A report creates the record or counts as a vote; a
    vote counts its author once, with their worst impact; a detour adds new steps; a confirm or failed mark changes
    the detour's trust. An invalid payload, one for another fingerprint, or one about an unknown detour changes
    nothing. The per-account daily cap is the workflow's."""
    rec = json.loads(json.dumps(rec or {}))
    if validate_payload(payload) or not author_hash or (rec and rec.get("fingerprint") != payload["fingerprint"]):
        return rec
    kind, version, trusted = payload["type"], payload["version"], author_assoc in TRUSTED
    if not rec:
        if kind != "report":
            return rec
        rec = _new_record(payload, now)
    detours = rec.setdefault("detours", [])
    if kind in ("report", "vote"):
        _add_vote(rec, author_hash, payload["impact"], version, trusted, now)
    elif kind == "detour":
        canon = json.dumps(payload["steps"], sort_keys=True)
        if len(detours) < MAX_DETOURS and all(json.dumps(d.get("steps"), sort_keys=True) != canon for d in detours):
            n = max([int(d["id"][1:]) for d in detours if _fits(d.get("id"), _DETOUR_ID_RE)] + [0]) + 1
            detours.append({"id": f"d{n}", "steps": payload["steps"], "since": version, "until": "",
                            "author_hash": author_hash, "author_assoc": author_assoc,
                            "author_kind": "member" if trusted else "other", "evidence": payload["evidence"],
                            "confirmations": [], "failures": [], "flagged": False, "ts": now})
    else:
        d = next((d for d in detours if d.get("id") == payload["detour_id"]), None)
        if d is None:
            return rec
        mark = {"author_hash": author_hash, "author_assoc": author_assoc, "ts": now}
        if kind == "confirm":
            marks = d.setdefault("confirmations", [])
            if trusted and author_hash != d.get("author_hash") and len(marks) < MAX_MARKS and \
                    all(m.get("author_hash") != author_hash for m in marks):
                marks.append(mark)
        else:
            marks = d.setdefault("failures", [])
            if len(marks) < MAX_MARKS and all(m.get("author_hash") != author_hash for m in marks):
                marks.append(mark)
            if not trusted:
                d["flagged"] = True  # a failure mark from outside the team only asks a maintainer to look
    for d in detours:
        d["trust"] = trust_of(d, d.get("confirmations"))
    cut = now - WINDOW_DAYS * 86400
    rec["impacts"] = {k: sum(1 for v in rec.get("voters", {}).values() if v.get("impact") == k and v.get("ts", 0) >= cut)
                      for k in IMPACT}
    rec["score"] = round(score(rec, now), 4)
    return rec
