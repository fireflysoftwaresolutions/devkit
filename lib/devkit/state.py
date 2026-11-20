# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Serialized, atomic writes of the session and run records.

STATE/sessions/<agent>-<sid>.json and STATE/runs/<agent>-<sid>.json belong to one session. The hooks and the `dev`
commands change them from separate processes, and the dashboard server reads them from threads, so every write goes
through this module:

    read(path)                      -> dict | None  no lock; None when the record is missing or does not parse
    hold(path, wait=None)           the record's lock, as a context manager; re-entrant per thread
    load(path)                      -> dict | None  for a writer that holds the lock: None when missing; raises
                                                    StateBusy when the record exists but does not parse
    update(path, fn, create=True)   -> dict | None  lock, re-read, fn(doc), atomic replace
    patch(path, before, after, create=False) -> dict | None
                                    for code that reads early and writes late: lock, re-read, and apply only what
                                    changed between `before` and `after`
    replace(path, doc)              atomic replace, under the lock (taken if the caller does not hold it); the caller
                                    read the record inside the same hold
    remove(path, with_lock=False)   delete the record under its lock; with_lock also deletes the lock file (below)
    set_aside(path, name)           rename the record to `name` in its folder, under its lock
    held(path)                      -> int: the id of this thread's hold on the record, 0 when it holds none

Invariants
- One lock per session covers its records (session, run, continuity): <state>/locks/rec-<agent>-<sid>.lock (an
  exclusive `flock`). It is re-entrant per thread (a depth count), so a writer that holds it can call another
  writer; another thread or process waits. Callers run no subprocess, git or network call while they hold it.
- A lock file is deleted only by remove(with_lock=True), while its flock is held, and only when that hold is the
  outermost one, no record it covers is left, and the path still names the file locked. A waiter that opened the
  file before checks after its flock that the path still names the file it locked, and otherwise locks the file the
  path names now, so it never holds a file nobody else sees. A waiter of an older devkit copy does not check: only a
  caller whose waiters never write a missing record (create=False) deletes the lock file. The canonical message
  store (messaging-v2) keeps its lock files: its acquisition refuses a lock replaced while it waits.
- Waits are bounded by WAIT seconds: 10 for `dev` commands, 2 in hooks (hooks.run sets HOOK_WAIT). On timeout
  StateBusy is raised and `state.busy` is audited: the event is dropped whole, never half-applied.
- A record that exists but does not parse is never written over: the read is tried 3 times within about 50 ms,
  then StateBusy (`state.unreadable` in the audit).
- create=False: a missing record stays missing (a hook that arrives after SessionEnd deleted the session never
  brings it back). Nothing is written and None is returned.
- Writes are atomic: a temporary file unique to the writer (`.<name>.<pid>.<random>.tmp`, created 0600) in the
  record's folder (made 0700), then os.replace. Durable messaging writes fsync the file and its parent directory.
- patch compares dicts key by key, recursively (added, changed, deleted); any other value (a list, a scalar) is one
  whole value. Keys the writer did not change are never written. A list changed on both sides keeps the fresh value,
  and a change inside a dict that another writer deleted is dropped; both are audited as `state.conflict`. Lists that
  several processes append to change through update, with the append as fn.
"""
from __future__ import annotations

import fcntl
import errno
import itertools
import json
import os
import re
import secrets
import sys
import stat
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

WAIT = 10.0  # seconds a `dev` command waits for a record's lock; it runs under the shell tool's timeout
HOOK_WAIT = 2.0  # seconds a hook waits; hooks.run sets WAIT to it
SAY_BUSY = True  # say on stderr that a `dev` command is waiting; hooks.run turns it off
SKIP = object()  # update's fn returns it to write nothing

_GONE = object()
_local = threading.local()
_ids = itertools.count(1)
_tight = set()  # record folders already made 0700 in this process


class StateBusy(ValueError):
    """The record's lock was not free in time, or the record does not parse. A ValueError, so every `dev` command
    reports it as a plain error, as it does a refused step; hooks catch it and fail open."""


class StateCorrupt(StateBusy):
    """load_bounded: the record's bytes are not a JSON object. Only this, not a busy lock, a record too large to
    read or an unsafe file, says that no reader can parse it."""


def _audit(action: str, target: str, detail: str, outcome: str):
    try:
        from . import audit
        audit.record(action, target, detail, outcome)
    except Exception:
        pass


# --------------------------------------------------------------------------- the lock

def lock_file(path) -> Path:
    """The lock of a record: <state>/locks/rec-<agent>-<sid>.lock for the three records of a session (their
    folders, sessions/, runs/ and continuity/, share a parent); next to the record for a record kept anywhere
    else."""
    path = Path(path)
    root = path.parent.parent if path.parent.name in ("sessions", "runs", "continuity") else path.parent
    return root / "locks" / f"rec-{re.sub(r'[^A-Za-z0-9_.-]', '', path.stem)[:120] or '-'}.lock"


def _holds() -> dict:
    t = getattr(_local, "holds", None)
    if t is None:
        t = _local.holds = {}
    return t


def held(path) -> int:
    e = _holds().get(str(lock_file(path)))
    return e[2] if e else 0


def _names(lock: Path, fd: int) -> bool:
    """Does the path `lock` still name the file open at `fd`?"""
    try:
        a, b = os.fstat(fd), os.stat(str(lock))
    except OSError:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _acquire(lock: Path, wait: float, what: str) -> int:
    canonical = "messaging-v2" in lock.parts
    t0 = time.monotonic()
    deadline, said = t0 + wait, False
    while True:
        with ExitStack() as stack:
            if canonical:
                directory = stack.enter_context(_directory(lock.parent, create=True))
                fd = _open_leaf(directory, lock.name, os.O_RDWR | os.O_CREAT)
            else:
                lock.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                if canonical:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                        raise StateBusy("unsafe state lock: not an owned single-link regular file")
                    os.fchmod(fd, 0o600)
                pause = 0.002
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError as exc:
                        if exc.errno not in (errno.EACCES, errno.EAGAIN):
                            raise
                        now = time.monotonic()
                        if now >= deadline:
                            _audit("state.busy", what, f"the record's lock was not free within {wait:g} s", "dropped")
                            raise StateBusy(f"{what} is busy: another devkit process held it for {wait:g} s; try again")
                        if SAY_BUSY and not said and now - t0 > 0.5:
                            print(f"devkit: {what} is busy (another devkit process is writing it); waiting…",
                                  file=sys.stderr)
                            said = True
                        time.sleep(min(pause, max(0.0, deadline - now)))
                        pause = min(pause * 2, 0.05)
                if canonical:
                    current = os.stat(lock.name, dir_fd=directory, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino) or current.st_nlink != 1:
                        raise StateBusy("unsafe state lock: replaced during acquisition")
                    return fd
                if _names(lock, fd):
                    return fd
            except BaseException:
                os.close(fd)
                raise
            # remove(with_lock=True) deleted the lock file between this open and the flock: the file locked here is
            # one nobody else sees, so lock the file the path names now, within the same wait
            os.close(fd)
            if time.monotonic() >= deadline:
                _audit("state.busy", what, f"the record's lock file kept changing for {wait:g} s", "dropped")
                raise StateBusy(f"{what} is busy: its lock file kept changing for {wait:g} s; try again")


@contextmanager
def hold(path, wait: float | None = None):
    """The record's lock for this thread, for `with`. Yields the hold's id. Re-entrant: an inner hold of the same
    session (either of its records) just counts. Raises StateBusy when it is not free within `wait` (WAIT)."""
    lock = lock_file(path)
    holds, key = _holds(), str(lock)
    e = holds.get(key)
    if e:
        e[1] += 1
        try:
            yield e[2]
        finally:
            e[1] -= 1
        return
    with ExitStack() as stack:
        if "messaging-v2" in Path(path).parts:
            parent = _absolute(Path(path).parent)
            directory = stack.enter_context(_directory(parent, create=True))
            pins = _directories()
            previous = pins.get(parent)
            pins[parent] = directory
            def unpin():
                if previous is None:
                    pins.pop(parent, None)
                else:
                    pins[parent] = previous
            stack.callback(unpin)
        fd = _acquire(lock, WAIT if wait is None else wait, Path(path).name)
        e = holds[key] = [fd, 1, next(_ids)]
        try:
            yield e[2]
        finally:
            del holds[key]
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


# --------------------------------------------------------------------------- reading and writing

def read(path):
    """The record, without the lock (readers): None when it is missing or does not parse."""
    try:
        if "messaging-v2" in Path(path).parts:
            return load_bounded(path, None)
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def load(path):
    """The record for a writer that holds its lock: None when it does not exist. A record that exists but does not
    parse (a writer outside this module was cut off halfway) is tried 3 times, then StateBusy: never treated as
    empty, so it is never written over."""
    path = Path(path)
    if "messaging-v2" in path.parts:
        return load_bounded(path, None)
    for i in range(3):
        try:
            doc = json.loads(path.read_text())
            if isinstance(doc, dict):
                return doc
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            pass
        if i < 2:
            time.sleep(0.02)
    _audit("state.unreadable", path.name, "the record does not parse; it is not written over", "dropped")
    raise StateBusy(f"{path.name} does not parse; devkit does not write over it")


def _absolute(path):
    path = Path(path).absolute()
    if ".." in path.parts:
        raise StateBusy("unsafe state path: parent traversal")
    # Expand only known root-managed macOS aliases, then no-follow walk their targets too.
    if len(path.parts) > 1 and path.parts[1] in ("var", "tmp", "etc"):
        alias = Path("/") / path.parts[1]
        info = alias.lstat()
        if stat.S_ISLNK(info.st_mode) and info.st_uid == 0 and \
                os.readlink(alias) in ("private/" + alias.name, "/private/" + alias.name):
            path = Path("/private") / Path(*path.parts[1:])
    return path


def _directories():
    if not hasattr(_local, "directories"):
        _local.directories = {}
    return _local.directories


_TRUSTED = {}  # devkit's state root as given -> the real folder it resolves to, resolved once per process


def _trusted_root(path):
    """(the state root, its real folder) when `path` is the state root (core.STATE) or inside it, else (None, None).
    The root and what lies above it (a home folder reached through a link, /Users/me -> /Volumes/Data/me) are the
    system's and the user's, so they are resolved once and followed; every part below the root is walked without
    following a link."""
    from . import core
    root = _absolute(core.STATE)
    if path != root and root not in path.parents:
        return None, None
    if str(root) not in _TRUSTED:
        _TRUSTED[str(root)] = Path(os.path.realpath(root))
    return root, _TRUSTED[str(root)]


def _open_base(path, pins, create):
    """(the folder the walk starts from, its handle): a pinned ancestor, else the state root as it resolves, else /."""
    base = next((p for p in (path,) + tuple(path.parents) if p in pins), None)
    if base is not None:
        return base, os.dup(pins[base])
    root, real = _trusted_root(path)
    if root is None:
        base = Path(path.anchor)
        return base, os.open(str(base), os.O_RDONLY | os.O_DIRECTORY)
    if create:
        os.makedirs(real, 0o700, exist_ok=True)
    fd = os.open(str(real), os.O_RDONLY | os.O_DIRECTORY)
    if os.fstat(fd).st_uid not in (0, os.getuid()):
        os.close(fd)
        raise StateBusy("unsafe state directory: owner mismatch")
    return root, fd


@contextmanager
def _directory(path, *, create=False):
    """Walk with no-follow directory handles; never resolve a checked ancestor again."""
    path = _absolute(path)
    pins = _directories()
    base, fd = _open_base(path, pins, create)
    active = "messaging-v2" in base.parts
    try:
        for name in path.relative_to(base).parts:
            active = active or name == "messaging-v2"
            if create:
                try:
                    os.mkdir(name, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            try:
                child = os.open(name, flags, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise StateBusy("unsafe state path: symbolic link or non-directory") from None
                raise
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if info.st_uid not in (0, os.getuid()) or (active and info.st_uid != os.getuid()):
                raise StateBusy("unsafe state directory: owner mismatch")
            if create and active:
                os.fchmod(fd, 0o700)
        yield fd
    finally:
        os.close(fd)


def _record_info(directory, name):
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise StateBusy("unsafe state record: not an owned regular file")


def _open_leaf(directory, name, flags):
    try:
        return os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise StateBusy("unsafe state path: symbolic link or non-directory") from None
        raise


def secure_path(path):
    """Validate a path for callers; actual canonical I/O pins its own directory handles."""
    path = Path(path)
    try:
        with _directory(path.parent) as directory:
            info = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
                raise StateBusy("unsafe state path: symbolic link or owner mismatch")
    except FileNotFoundError:
        pass


def private_directories(path):
    """Create and tighten only the canonical namespace, never an unrelated ancestor."""
    if "messaging-v2" in Path(path).parts:
        with _directory(path, create=True):
            pass


def touch(path, *, times=None):
    """Create or timestamp a canonical notification marker through its pinned parent."""
    path = Path(path)
    with _directory(path.parent, create=True) as directory:
        fd = _open_leaf(directory, path.name, os.O_WRONLY | os.O_CREAT)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise StateBusy("unsafe state marker: not an owned single-link regular file")
            os.fchmod(fd, 0o600)
            os.utime(fd, times)
        finally:
            os.close(fd)


def load_bounded(path, maximum):
    """A strict bounded read: corruption and capacity never masquerade as absence."""
    try:
        with _directory(Path(path).parent) as directory:
            fd = _open_leaf(directory, Path(path).name, os.O_RDONLY)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        # Atomic replacement can unlink this already-open inode; its complete contents remain safe to read.
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink > 1:
            raise StateBusy("unsafe state record")
        raw = stream.read() if maximum is None else stream.read(maximum + 1)
    if maximum is not None and len(raw) > maximum:
        raise StateBusy("capacity: existing state exceeds the read limit; retained unchanged")
    try:
        doc = json.loads(raw)
        if not isinstance(doc, dict):
            raise ValueError()
        return doc
    except (ValueError, UnicodeError):
        _audit("state.unreadable", Path(path).name, "the record does not parse; retained unchanged", "dropped")
        raise StateCorrupt("corrupt_state: record does not parse; retained unchanged") from None


def _write(path: Path, doc: dict, indent, durable=False, max_bytes=None):
    text = json.dumps(doc, indent=indent, ensure_ascii=not durable,
                      separators=(",", ":") if durable and indent is None else None)
    if max_bytes is not None and len(text.encode("utf-8")) > max_bytes:
        raise StateBusy("capacity: record exceeds the storage limit")
    if durable or "messaging-v2" in path.parts:
        with _directory(path.parent, create=True) as directory:
            _record_info(directory, path.name)
            os.fchmod(directory, 0o700)
            tmp = f".{path.name}.{os.getpid()}.{secrets.token_hex(16)}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text)
                    if durable:
                        fh.flush()
                        os.fsync(fh.fileno())
                os.replace(tmp, path.name, src_dir_fd=directory, dst_dir_fd=directory)
                if durable:
                    os.fsync(directory)
            except BaseException:
                try:
                    os.unlink(tmp, dir_fd=directory)
                except OSError:
                    pass
                raise
        return
    d = path.parent
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    if str(d) not in _tight:
        try:
            os.chmod(d, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions (0700 is owner-only: this tightens the folder)
        except OSError:
            pass
        _tight.add(str(d))
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.{os.getpid()}.", suffix=".tmp", dir=str(d))  # 0600
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def replace(path, doc: dict, indent=None, *, durable=False, max_bytes=None):
    """Write the whole record atomically. The caller holds the lock and read the record inside that hold (update and
    patch do both); without a hold this takes the lock for the write only."""
    path = Path(path)
    with hold(path):
        _write(path, doc, indent, durable=durable, max_bytes=max_bytes)


def _covered(path: Path) -> list:
    """The records one lock covers: a session's three records share theirs (lock_file), any other record has its
    own."""
    if path.parent.name in ("sessions", "runs", "continuity"):
        return [path.parent.parent / folder / path.name for folder in ("sessions", "runs", "continuity")]
    return [path]


def remove(path, *, with_lock: bool = False):
    """Delete the record under its lock, so no writer that already read it writes it back. with_lock=True also
    deletes its lock file, still under the flock, when this call took the lock (an outer hold of the caller goes on
    after it, so its lock stays), no record the lock covers is left, and the path still names the file locked; never
    in the canonical message store. Only for records whose waiters never write a missing one (create=False): a waiter
    of an older devkit copy does not check, after its flock, that the path still names the file it locked."""
    path = Path(path)
    lock = lock_file(path)
    with hold(path):
        try:
            if "messaging-v2" in path.parts:
                with _directory(path.parent) as directory:
                    _record_info(directory, path.name)
                    os.unlink(path.name, dir_fd=directory)
            else:
                path.unlink()
        except FileNotFoundError:
            pass
        e = _holds().get(str(lock))
        if with_lock and e and e[1] == 1 and "messaging-v2" not in lock.parts and \
                not any(os.path.lexists(p) for p in _covered(path)) and _names(lock, e[0]):
            try:
                os.unlink(lock)
            except FileNotFoundError:
                pass


def set_aside(path, name: str) -> Path:
    """Rename the record to `name` in its folder, under its lock, so the next writer starts a new record and the old
    bytes stay for a person to look at. The lock file stays: a waiter that opened it may still lock it. -> the new
    path."""
    path = Path(path)
    with hold(path):
        if "messaging-v2" in path.parts:
            with _directory(path.parent) as directory:
                _record_info(directory, path.name)
                os.rename(path.name, name, src_dir_fd=directory, dst_dir_fd=directory)
        else:
            os.rename(path, path.with_name(name))
    return path.with_name(name)


def update(path, fn, *, wait: float | None = None, create: bool = True, indent=None, durable=False, max_bytes=None):
    """Lock, re-read, fn(doc), atomic replace. -> the record written, or None when it is missing and create is
    False. fn changes doc in place or returns a new dict; it returns SKIP to write nothing. A missing record starts
    as {} with create=True."""
    path = Path(path)
    with hold(path, wait):
        doc = load_bounded(path, max_bytes) if max_bytes is not None else load(path)
        if doc is None:
            if not create:
                return None
            doc = {}
        out = fn(doc)
        if out is SKIP:
            return doc
        if isinstance(out, dict):
            doc = out
        _write(path, doc, indent, durable=durable, max_bytes=max_bytes)
        return doc


def diff(before: dict, after: dict, at: tuple = ()) -> list:
    """What changed from before to after: [(op, keys, old, new)], op "dict" (a dict this writer created), "set" or
    "del"; keys is the path of dict keys to the value."""
    ops = []
    for k, new in after.items():
        old = before.get(k, _GONE)
        if isinstance(new, dict) and (old is _GONE or isinstance(old, dict)):
            if old is _GONE:
                ops.append(("dict", at + (k,), _GONE, None))
            ops += diff({} if old is _GONE else old, new, at + (k,))
        elif old is _GONE or old != new:
            ops.append(("set", at + (k,), old, new))
    ops += [("del", at + (k,), old, _GONE) for k, old in before.items() if k not in after]
    return ops


def apply(doc: dict, ops: list) -> list:
    """Apply diff's changes to the fresh record. -> the key paths left as the other writer made them."""
    conflicts = []
    for op, keys, old, new in ops:
        parent = doc
        for k in keys[:-1]:
            parent = parent.get(k, _GONE) if isinstance(parent, dict) else _GONE
        if not isinstance(parent, dict):  # a dict on the way was deleted or replaced meanwhile: that change wins
            conflicts.append(keys)
            continue
        k = keys[-1]
        cur = parent.get(k, _GONE)
        if op == "dict":
            if cur is _GONE:
                parent[k] = {}
            elif not isinstance(cur, dict):
                conflicts.append(keys)
        elif op == "del":
            parent.pop(k, None)
        elif (isinstance(old, list) or isinstance(new, list)) and cur is not _GONE and cur != old and cur != new:
            conflicts.append(keys)  # a list changed on both sides: the fresh value stays
        else:
            parent[k] = new
    return conflicts


def patch(path, before, after, *, wait: float | None = None, create: bool = False, indent=None):
    """For code that read the record early and writes late: under the lock, re-read it and apply only what changed
    between `before` (a copy taken when it was read) and `after`. -> the record written, or None when nothing
    changed or the record is missing (create=False)."""
    ops = diff(before or {}, after or {})
    if not ops:
        return None
    path = Path(path)
    with hold(path, wait):
        doc = load(path)
        if doc is None:
            if not create:
                return None
            doc = {}
        conflicts = apply(doc, ops)
        _write(path, doc, indent)
    if conflicts:
        _audit("state.conflict", path.name, "kept the other writer's value: " +
               ", ".join(".".join(str(x) for x in c) for c in conflicts)[:400], "merged")
    return doc
