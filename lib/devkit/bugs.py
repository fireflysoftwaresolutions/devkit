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
import hashlib
import json
import os
import re
import secrets
import string
import time
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
