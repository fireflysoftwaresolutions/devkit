# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Serialized, atomic writes of the session and run records (lifecycle spec C1, decision D1).

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
    remove(path)                    delete the record under its lock
    held(path)                      -> int: the id of this thread's hold on the record, 0 when it holds none

Invariants
- One lock per session covers its records (session, run, continuity): <state>/locks/rec-<agent>-<sid>.lock (an
  exclusive `flock`). It is re-entrant per thread (a depth count), so a writer that holds it can call another
  writer; another thread or process waits. Callers run no subprocess, git or network call while they hold it.
- Waits are bounded by WAIT seconds: 10 for `dev` commands, 2 in hooks (hooks.run sets HOOK_WAIT). On timeout
  StateBusy is raised and `state.busy` is audited: the event is dropped whole, never half-applied.
- A record that exists but does not parse is never written over: the read is tried 3 times within about 50 ms,
  then StateBusy (`state.unreadable` in the audit).
- create=False: a missing record stays missing (a hook that arrives after SessionEnd deleted the session never
  brings it back). Nothing is written and None is returned.
- Writes are atomic: a temporary file unique to the writer (`.<name>.<pid>.<random>.tmp`, created 0600) in the
  record's folder (made 0700), then os.replace. No fsync: a crash loses at most the last event.
- patch compares dicts key by key, recursively (added, changed, deleted); any other value (a list, a scalar) is one
  whole value. Keys the writer did not change are never written. A list changed on both sides keeps the fresh value,
  and a change inside a dict that another writer deleted is dropped; both are audited as `state.conflict`. Lists that
  several processes append to change through update, with the append as fn.
"""
from __future__ import annotations

import fcntl
import itertools
import json
import os
import re
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

WAIT = 10.0  # seconds a `dev` command waits for a record's lock; it runs under the shell tool's timeout
HOOK_WAIT = 2.0  # seconds a hook waits (C1); hooks.run sets WAIT to it
SAY_BUSY = True  # say on stderr that a `dev` command is waiting; hooks.run turns it off
SKIP = object()  # update's fn returns it to write nothing

_GONE = object()
_local = threading.local()
_ids = itertools.count(1)
_tight = set()  # record folders already made 0700 in this process


class StateBusy(ValueError):
    """The record's lock was not free in time, or the record does not parse. A ValueError, so every `dev` command
    reports it as a plain error, as it does a refused step; hooks catch it and fail open."""


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


def _acquire(lock: Path, wait: float, what: str) -> int:
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
    t0 = time.monotonic()
    deadline, pause, said = t0 + wait, 0.002, False
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            now = time.monotonic()
            if now >= deadline:
                os.close(fd)
                _audit("state.busy", what, f"the record's lock was not free within {wait:g} s", "dropped")
                raise StateBusy(f"{what} is busy: another devkit process held it for {wait:g} s; try again")
            if SAY_BUSY and not said and now - t0 > 0.5:
                print(f"devkit: {what} is busy (another devkit process is writing it); waiting…", file=sys.stderr)
                said = True
            time.sleep(min(pause, max(0.0, deadline - now)))
            pause = min(pause * 2, 0.05)


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
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def load(path):
    """The record for a writer that holds its lock: None when it does not exist. A record that exists but does not
    parse (a writer outside this module was cut off halfway) is tried 3 times, then StateBusy: never treated as
    empty, so it is never written over."""
    path = Path(path)
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


def _write(path: Path, doc: dict, indent):
    text = json.dumps(doc, indent=indent)  # before any file is touched: a value that cannot be written changes nothing
    d = path.parent
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    if str(d) not in _tight:
        try:
            os.chmod(d, 0o700)
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


def replace(path, doc: dict, indent=None):
    """Write the whole record atomically. The caller holds the lock and read the record inside that hold (update and
    patch do both); without a hold this takes the lock for the write only."""
    path = Path(path)
    with hold(path):
        _write(path, doc, indent)


def remove(path):
    """Delete the record under its lock, so no writer that already read it writes it back."""
    path = Path(path)
    with hold(path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def update(path, fn, *, wait: float | None = None, create: bool = True, indent=None):
    """Lock, re-read, fn(doc), atomic replace. -> the record written, or None when it is missing and create is
    False. fn changes doc in place or returns a new dict; it returns SKIP to write nothing. A missing record starts
    as {} with create=True."""
    path = Path(path)
    with hold(path, wait):
        doc = load(path)
        if doc is None:
            if not create:
                return None
            doc = {}
        out = fn(doc)
        if out is SKIP:
            return doc
        if isinstance(out, dict):
            doc = out
        _write(path, doc, indent)
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
