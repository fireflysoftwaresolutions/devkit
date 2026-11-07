# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev impeccable <verb> [args…]`: run the impeccable engine devkit installed, without a shell.

The impeccable skill's launcher (scripts/impeccable, a sh script) runs the engine it finds beside the path it was run
by. When the shell runs a typed path, it can reach another file than the one the guard reads by the same words (a
link, `..` after `cd`, a quoted `~`, a glob, a folder moved earlier in the same command; the Code reviewer's reviews
of 11fd55e, 452dd51 and 1e606b7), so the guard trusts no typed launcher path. This command does the launcher's job
from devkit's own paths instead:

1. The engine is `<devkit's skills folder>/impeccable/scripts/bin/<os>-<arch>/impeccable` (setup.SKILLS_SHARE) with no
   link below that folder: never a path from the current folder, an argument or the environment.
2. The install record of the skills step (setup.engines_record()) names that file, and the file's SHA-256 matches the
   record. It is checked before every run, on the bytes read from one open descriptor, and the file must still be
   that same file (device, inode, size and times) just before it starts.
3. The engine runs with an argument list (no shell), in the caller's folder, with the environment the launcher gives
   it: IMPECCABLE_SKILL_DIR, the skill folder, and IMPECCABLE_SELF, the name it prints in the commands it suggests
   (`dev impeccable`); plus IMPECCABLE_NO_UPDATE_CHECK=1 and IMPECCABLE_NO_TELEMETRY=1. Every other IMPECCABLE_*
   variable is dropped: IMPECCABLE_BIN picks another engine, and the others move its downloads, caches and plugins.

Anything else is refused with the fix, `dev install --only skills`. Outside the developer's terminal
(core.set_by_developer) the command runs only the forms on the agents' allowlist, spelled exactly
(guard.impeccable_developer_call): context with --target only, detect on local paths and URLs on this Mac, doctor
with --json or --target, live, generate, document, extract and init with no other words, and the skill's helpers in
the forms that stay inside the project or talk to the live server (guard.IMPECCABLE_FORMS); and only in a project
checkout of the workspace, with each --target and --file inside it through its links (outside_project). Which of them
a crew lane may run is the guard's decision (guard.IMPECCABLE_READS and IMPECCABLE_READ_HELPERS for read-only
lanes)."""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import subprocess

from . import core, ui

FIX = "dev install --only skills"
SELF = "dev impeccable"
SETTINGS = {"IMPECCABLE_NO_UPDATE_CHECK": "1", "IMPECCABLE_NO_TELEMETRY": "1"}


class Refused(Exception):
    """Why `dev impeccable` does not run the engine."""


def _identity(st) -> tuple:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def verified_engine() -> tuple:
    """(engine path, skill folder, the engine's identity) of the engine devkit installed, checked as steps 1 and 2 of
    the module's docstring say; raises Refused otherwise."""
    from . import setup
    plat = setup.engine_platform()
    if not plat:
        u = os.uname()
        raise Refused(f"impeccable has no engine for this platform ({u.sysname} {u.machine})")
    record = setup.engines_record()
    rec = core.read_json(record, {})
    rec = rec.get("impeccable") if isinstance(rec, dict) else None
    digest = str(rec.get("sha256") or "") if isinstance(rec, dict) else ""
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise Refused(f"{core.tilde(record)} has no record of the impeccable engine, so its digest cannot be checked")
    skill = os.path.join(os.path.realpath(setup.SKILLS_SHARE), "impeccable")
    engine = os.path.join(skill, "scripts", "bin", plat, "impeccable")
    named = setup.SKILLS_SHARE / "impeccable" / "scripts" / "bin" / plat / "impeccable"
    if os.path.realpath(named) != engine:
        raise Refused(f"{core.tilde(named)} leads to {core.tilde(os.path.realpath(named))} through a link, and devkit "
                      f"runs only the engine in its own skills folder")
    if os.path.realpath(str(rec.get("engine") or "")) != engine:
        raise Refused(f"the install record names the engine {core.tilde(str(rec.get('engine') or '?'))}, not the one "
                      f"in devkit's skills folder ({core.tilde(engine)})")
    try:
        fd = os.open(engine, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as e:
        raise Refused(f"{core.tilde(engine)} cannot be read ({e.strerror or e})") from None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise Refused(f"{core.tilde(engine)} is not a regular file")
        h = hashlib.sha256()
        for chunk in iter(lambda: os.read(fd, 1 << 20), b""):
            h.update(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if _identity(after) != _identity(before):
        raise Refused(f"{core.tilde(engine)} changed while its digest was checked")
    if h.hexdigest() != digest:
        raise Refused(f"{core.tilde(engine)} has the SHA-256 {h.hexdigest()[:12]}…, but the install record says "
                      f"{digest[:12]}…")
    return engine, skill, _identity(after)


def engine_env(skill, environ=None) -> dict:
    """The engine's environment (step 3 of the module's docstring): `environ` (default: this process's) without any
    IMPECCABLE_* variable, plus the launcher's two variables and devkit's settings."""
    env = {k: v for k, v in (os.environ if environ is None else environ).items() if not k.startswith("IMPECCABLE_")}
    env.update(IMPECCABLE_SKILL_DIR=str(skill), IMPECCABLE_SELF=SELF, **SETTINGS)
    return env


def _wait(proc) -> int:
    """The engine's exit code, as a shell reports it (128 + the signal that ended it). A Ctrl-C reaches the engine
    too, since it runs in the same process group: dev waits for it to end, and ends it at a second Ctrl-C."""
    try:
        rc = proc.wait()
    except KeyboardInterrupt:
        try:
            rc = proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            rc = proc.wait()
    return rc if rc >= 0 else 128 - rc


def _developers(call: str, refusal: str, args) -> bool:
    """True in the developer's terminal (core.set_by_developer). A call off the agents' allowlist
    (guard.impeccable_developer_call) may write the agents' settings, hooks or skill folders, which changes what runs
    in every later session, reach the internet, write outside the project, skip one of the skill's gates, or run a
    verb a newer engine added, so anyone else gets `refusal`, printed and audited, and False."""
    from . import audit
    try:
        ok = core.set_by_developer()
    except Exception:
        ok = False  # a check that cannot decide refuses (fail closed)
    if not ok:
        audit.record("dev.impeccable", " ".join(args)[:200], f"{call}: not the developer's terminal", "refused")
        ui.err(refusal)
    return ok


def _checkout_top(folder) -> str:
    """The top folder of the git checkout the real path `folder` is in: the nearest folder up from it with a `.git`
    entry (a folder, or the file of a worktree or a submodule), found without git, so neither GIT_DIR nor GIT_WORK_TREE
    moves it; '' outside any checkout."""
    d = folder
    while True:
        if os.path.lexists(os.path.join(d, ".git")):
            return d
        up = os.path.dirname(d)
        if up == d:
            return ""
        d = up


def _inside(path, root) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def outside_project(args, here) -> tuple:
    """('', '') when the engine, run with the words `args` in the folder `here`, writes only in a project checkout;
    else (what to name in the audit, why, for stderr). The real path of `here` is in a checkout whose top folder is
    below the workspace (core.DEV, never the workspace itself) and outside the handoff repository; each --target and
    --file (guard.impeccable_paths), resolved from that folder through its links (os.path.realpath), stays in that top
    folder. This sees the folder and the words as the engine gets them, after the shell's `cd` and expansions, which
    the guard can only read as typed (the tech lead's decision on the Code reviewer's finding: `cd ~/.claude && dev
    impeccable live --target settings.json` passed the guard)."""
    from .guard import impeccable_paths
    real = os.path.realpath(here)
    top = _checkout_top(real)
    dev, handoff = os.path.realpath(core.DEV), os.path.realpath(core.HANDOFF)
    if not top or top == dev or not _inside(top, dev) or _inside(top, handoff):
        return "folder", (f"dev impeccable: {core.tilde(real)} is not in a project checkout of the workspace "
                          f"({core.tilde(core.DEV)}/<owner>/<repo>, never the handoff repository), and the engine writes "
                          f"in the folder it runs in. Run it from your project's folder; the developer runs it anywhere, "
                          f"in a terminal.")
    for value in impeccable_paths(args):
        p = os.path.realpath(os.path.join(real, value))
        if not _inside(p, top):
            return value, (f"dev impeccable: `{value}` leads to {core.tilde(p)}, outside the project checkout "
                           f"{core.tilde(top)} (through a link or a `..`), and the engine reads or writes there. Give a "
                           f"path inside the project; the developer runs it with any path, in a terminal.")
    return "", ""


def run(args, cwd=None) -> int:
    """Run the verified engine with the words `args` in `cwd` (default: the current folder) -> its exit code, or 1
    when it is refused (the reason and the fix on stderr). Outside the developer's terminal, the words must be on the
    agents' allowlist and the engine must write only in a project checkout (outside_project)."""
    from .guard import impeccable_developer_call, impeccable_refusal
    args = [str(a) for a in args]
    try:
        here = cwd or os.getcwd()
    except OSError as e:
        ui.err(f"dev impeccable: the current folder cannot be read ({e.strerror or e}); run it from your project")
        return 1
    call, _ = impeccable_developer_call(args)  # the words the engine gets: dev's parser already dropped a first `--`
    refusal = impeccable_refusal(args) if call else ""
    if not call:
        call, refusal = outside_project(args, here)
    if call and not _developers(call, refusal, args):
        return 1
    try:
        engine, skill, identity = verified_engine()
        if _identity(os.stat(engine, follow_symlinks=False)) != identity:
            raise Refused(f"{core.tilde(engine)} changed after its digest was checked")
    except (Refused, OSError) as e:
        ui.err(f"dev impeccable: {e}. Reinstall the engine with `{FIX}`.")
        return 1
    try:
        proc = subprocess.Popen([engine, *args], cwd=here, env=engine_env(skill))
    except OSError as e:
        ui.err(f"dev impeccable: {core.tilde(engine)} did not start ({e.strerror or e}). Reinstall the engine with "
               f"`{FIX}`.")
        return 1
    return _wait(proc)


def cmd_impeccable(a) -> int:
    return run(a.args or [])


def register(add):
    p = add("impeccable", cmd_impeccable, "Run the impeccable design engine devkit installed: devkit's own copy, its "
                                          "digest checked before every run, no shell")
    p.add_argument("args", nargs=argparse.REMAINDER, help="the verb and its arguments, passed to the engine as they "
                                                          "are: context, detect, doctor, init, live, …")
