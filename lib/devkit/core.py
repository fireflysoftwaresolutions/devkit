# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Paths, configuration and process helpers shared by every devkit module.

Stdlib only and Python 3.9 compatible, so `dev` runs on any Mac without a
virtualenv (Homebrew python3 or the system /usr/bin/python3).
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

DEVKIT_HOME = Path(__file__).resolve().parents[2]
HOME = Path.home()
COPYRIGHT = "© 2026 Firefly Software Solutions Inc. · Apache License 2.0"
VERSION = (DEVKIT_HOME / "VERSION").read_text().strip() if (DEVKIT_HOME / "VERSION").exists() else "0.0.0"

# Tools are looked up on a predictable PATH even when a hook runs from an app
# (Claude Desktop, Codex app, launchd) whose PATH is minimal.
_EXTRA_PATH = ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", str(HOME / ".local" / "bin")]


def brew_prefix() -> str:
    """Homebrew's prefix: /opt/homebrew on Apple Silicon, /usr/local on Intel Macs (HOMEBREW_PREFIX wins)."""
    env = os.environ.get("HOMEBREW_PREFIX")
    if env and Path(env, "bin", "brew").exists():
        return env
    for p in ("/opt/homebrew", "/usr/local"):
        if Path(p, "bin", "brew").exists():
            return p
    return "/opt/homebrew"


# devkit's own tests only: DEVKIT_TEST_PATH names folders of stand-ins for codex, claude, brew and gh
# (tests/_sandbox.py). In a test sandbox they go first and the caller's PATH follows as given, without _EXTRA_PATH,
# so neither a stand-in nor a tool the test left out is replaced by this Mac's real one. Honored only when the real
# paths of HOME and of every folder named lie below the system's temporary folders, which a real Mac's HOME never
# does; there PATH stays as devkit always set it.
_TEMP_ROOTS = ("/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp", "/var/folders", "/private/var/folders")


def sandbox_path(environ=None) -> list:
    """The folders DEVKIT_TEST_PATH names when devkit runs in a test sandbox (a temporary HOME), else []."""
    env = os.environ if environ is None else environ
    folders = [p for p in (env.get("DEVKIT_TEST_PATH") or "").split(os.pathsep) if p]

    def temporary(p):
        real = os.path.realpath(p)
        return os.path.isabs(p) and any(real.startswith(root + "/") for root in _TEMP_ROOTS)
    home = env.get("HOME") or ""
    return folders if folders and home and temporary(home) and all(temporary(p) for p in folders) else []


os.environ["PATH"] = ":".join(dict.fromkeys((sandbox_path() or _EXTRA_PATH) + os.environ.get("PATH", "").split(":")))

# --------------------------------------------------------------------------- config

from . import configschema
from .configschema import (DEFAULTS, AUDIT_LEVELS, AUDIT_FLOOR_MIN, AUDIT_RULES, PROGRESS_RULES,
                           SLOT_KINDS, audit_config, progress_config)


def config_errors(cfg: dict) -> list:
    """Diagnose every saved configuration key through the shared closed schema."""
    return configschema.validate(cfg)


# `language`: auto, or a locale as the developer writes it: es, es-ES, pt_BR, zh-Hant
LOCALE_RE = re.compile(r"(?i)auto|[a-z]{2,3}(?:[-_][a-z]{2,4})?")


class ConfigError(ValueError):
    """Why `dev config set` changed nothing, and what to do instead."""


def _parse_value(text: str):
    """Parse JSON or a bare string; malformed containers and nonfinite values never become strings."""
    try:
        return configschema.loads(text)
    except configschema.SchemaError as exc:
        stripped = text.strip() if isinstance(text, str) else ""
        if stripped.startswith(("{", "[", '"')) or stripped in ("NaN", "Infinity", "-Infinity") or \
                re.fullmatch(r"-?\d+(?:\.\d+)?[eE][+-]?\d+", stripped):
            raise ConfigError(str(exc)) from None
        return text


def read_config(path=None, *, missing_ok=True, regular_file=False):
    """Read saved overrides strictly; absence is the only implicit empty document."""
    path = Path(path or CONFIG_FILE)
    try:
        if regular_file:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd) as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ConfigError("editor candidate must remain a regular file; configuration unchanged")
                text = stream.read()
        else:
            text = path.read_text()
        doc = configschema.loads(text)
    except FileNotFoundError:
        if missing_ok:
            return {}
        raise ConfigError("editor candidate is missing; configuration unchanged, reopen dev config edit") from None
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path} is not valid configuration JSON; fix it with dev config edit "
                          f"({exc})") from None
    if not isinstance(doc, dict):
        raise ConfigError(f"{path} must hold a JSON object; fix it with dev config edit")
    return doc


def _validate_config(candidate, before=None):
    """Refuse a candidate configuration with its errors, each unknown key named with its repair command. With
    `before`, the saved document a writer changed, an unknown key the writer left as it was does not block the save
    (configschema.tolerated_unknown): the saved configuration stays invalid until it is removed."""
    errors = config_errors(candidate)
    if before is not None:
        errors = [e for e in errors if not configschema.tolerated_unknown(e, before, candidate)]
    if errors:
        raise ConfigError("; ".join(configschema.describe(errors)))
    # Extension provenance stays owned by its adapter; imported lazily after core initialization.
    if (candidate.get("ext") or {}).get("add"):
        from . import ext
        try:
            for entry in candidate["ext"]["add"]:
                ext._user_items(entry)
        except ext.ExtError:
            raise ConfigError("ext.add has invalid extension provenance; check its pinned source and license") from None


def config_update(update, path=None, *, before_write=None):
    """Validate and atomically save a read/modify/write callback under the canonical file lock.

    The callback receives the latest saved overrides, never a stale effective snapshot. `before_write(before,
    candidate)` runs a caller's own last check under the lock. Retired keys are not saved.
    """
    path = Path(os.path.realpath(path or CONFIG_FILE))
    with file_lock(path) as got:
        if not got:
            raise ConfigError("configuration is locked by another process; retry the command")
        before = read_config(path)
        saved = json.loads(json.dumps(before))  # as saved, even when the callback changes its argument in place
        candidate = configschema.without_retired(update(before))
        _validate_config(candidate, saved)
        if before_write is not None:
            before_write(before, candidate)
        _write_config(path, candidate)
        return candidate


CONFIG_COPIES = 20  # copies of the configuration the writers keep in STATE/backups
# Not config.json: setup.backup keeps copies of ~/.docker/config.json in the same folder as config.json.<stamp> and
# rotates that name, so it would delete these, and a person could restore Docker's file as devkit's.
_COPY_NAME = "devkit-config.json"
_COPY_RE = re.compile(re.escape(_COPY_NAME) + r"\.(\d{8}T\d{6}Z)(?:-(\d+))?")
# Which file each copy was taken from ({copy name: real path}): DEVKIT_CONFIG can name another configuration file, and
# all copies share one folder. Not devkit-config.json.*, the copies' own names.
_COPY_SOURCES = "devkit-config-sources.json"
_copy_warned = [False]  # a copy that cannot be made is said once per process


def _copy_sources(folder: Path) -> dict:
    try:
        data = json.loads((folder / _COPY_SOURCES).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def config_copies(path=None) -> list:
    """The copies of the configuration the writers kept in STATE/backups, oldest first: devkit-config.json.<UTC
    YYYYMMDDTHHMMSSZ>, and -1, -2 for more in the same second (sorted by number, so -10 comes after -9). With `path`,
    only the copies of that file; a copy kept before devkit recorded its source counts as one of the default
    configuration file (~/.config/devkit/config.json)."""
    folder = STATE / "backups"
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    found = sorted((m.group(1), int(m.group(2) or 0), name) for name in names for m in [_COPY_RE.fullmatch(name)] if m)
    if path is not None:
        want, default = os.path.realpath(str(path)), os.path.realpath(str(config_file({})))
        sources = _copy_sources(folder)
        found = [f for f in found if sources.get(f[2], default) == want]
    return [folder / name for _, _, name in found]


def _record_copy_source(folder: Path, copy: Path, source: Path):
    """Record that `copy` is a copy of `source`, and forget the copies rotation deleted. Raises OSError."""
    sources = {n: src for n, src in _copy_sources(folder).items() if (folder / n).exists()}
    sources[copy.name] = os.path.realpath(str(source))
    fd, tmp = tempfile.mkstemp(prefix=f".{_COPY_SOURCES}.", dir=str(folder))
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(sources, out, indent=1, sort_keys=True)
        os.replace(tmp, folder / _COPY_SOURCES)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def config_restore_hint(path=None) -> str:
    """`restore with: cp <copy> <config>` for the newest kept copy of that file that is valid configuration, or ""
    when none is. The copy taken before `dev config edit` repaired a broken file is itself broken, so it is never
    offered, and neither is a copy of another configuration file."""
    target = Path(path or CONFIG_FILE)
    for copy in reversed(config_copies(target)):
        try:
            if not config_errors(read_config(copy, missing_ok=False)):
                return f"restore with: cp {shlex.quote(str(copy))} {shlex.quote(str(target))}"
        except ConfigError:
            continue
    return ""


def _keep_config_copy(path: Path, data: bytes):
    """Copy the saved configuration to STATE/backups before a writer replaces it with `data`, and keep the newest
    CONFIG_COPIES. Only devkit's configuration file is copied, and only when it exists and its bytes change. A copy
    can hold extension credentials, so the folder is the owner's alone (0700) and each copy is created 0600, never
    through a link already there. A copy that cannot be made never stops the write; it is said once on stderr.
    -> the copy, or None."""
    if path != Path(os.path.realpath(CONFIG_FILE)):
        return None
    folder = STATE / "backups"
    try:
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            return None
        if current == data:
            return None
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(folder, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions (0700 is owner-only: this tightens the backups folder)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        start = 0  # past the highest number this second: a number rotation freed would sort as the oldest copy
        for kept in config_copies():
            m = _COPY_RE.fullmatch(kept.name)
            if m.group(1) == stamp:
                start = int(m.group(2) or 0) + 1
        for n in range(start, start + 1000):
            copy = folder / f"{_COPY_NAME}.{stamp}{f'-{n}' if n else ''}"
            try:
                fd = os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                break
            except FileExistsError:
                continue
        else:
            raise OSError(f"no free copy name for {_COPY_NAME}.{stamp} in {folder}")
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(current)
                out.flush()
                os.fsync(out.fileno())
            for old in config_copies()[:-CONFIG_COPIES]:
                old.unlink(missing_ok=True)
            _record_copy_source(folder, copy, path)
        except OSError:
            copy.unlink(missing_ok=True)  # a partial copy, or one of no known file, is no restore point
            raise
        return copy
    except OSError as exc:
        if not _copy_warned[0]:
            _copy_warned[0] = True
            print(f"devkit: could not keep a copy of {tilde(path)} before this change ({exc}); the change goes "
                  f"ahead without one. Make {tilde(folder)} a folder you can write to so the next change keeps one.",
                  file=sys.stderr)
        return None


def _write_config(path, candidate):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    data = (json.dumps(candidate, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()
    _keep_config_copy(path, data)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except OSError as exc:
        raise ConfigError(f"could not save {path}; configuration unchanged, fix the filesystem error and retry "
                          f"({exc})") from None
    finally:
        Path(temporary).unlink(missing_ok=True)


def config_edit(editor, path=None):
    """Edit a private candidate, validate it, then replace unchanged saved bytes atomically.

    A concurrent writer wins safely: the editor must reopen the latest file rather than erase that update.
    Invalid saved JSON can be repaired here because the original is copied without parsing. Retired keys the
    candidate still holds are not saved.
    """
    path = Path(os.path.realpath(path or CONFIG_FILE))
    path.parent.mkdir(parents=True, exist_ok=True)
    before = path.read_bytes() if path.exists() else None
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.edit.", suffix=".json", dir=str(path.parent))
    saved = False
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(before if before is not None else b"{}\n")
        if editor(Path(temporary)) != 0:
            raise ConfigError("editor did not finish successfully; configuration unchanged")
        candidate = configschema.without_retired(read_config(temporary, missing_ok=False, regular_file=True))
        _validate_config(candidate)
        with file_lock(path) as got:
            if not got:
                raise ConfigError("configuration is locked; retry dev config edit")
            current = path.read_bytes() if path.exists() else None
            if current != before:
                raise ConfigError("configuration changed while editing; reopen dev config edit to keep both changes")
            _write_config(path, candidate)
        _config_audit("configuration", "validated editor candidate", "ok")
        saved = True
    except (ConfigError, OSError) as exc:
        recovery = ""
        try:
            recovery_fd = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                if stat.S_ISREG(os.fstat(recovery_fd).st_mode):
                    os.fchmod(recovery_fd, 0o600)
                    recovery = f"; your edited candidate is retained at {temporary} for recovery"
            finally:
                os.close(recovery_fd)
        except OSError:
            recovery = "; candidate is missing or inaccessible; reopen dev config edit"
        raise ConfigError(str(exc) + recovery) from None
    finally:
        if saved:
            Path(temporary).unlink(missing_ok=True)


def config_set(key: str, value: str, path=None) -> tuple:
    """Set a schema-supported path, validate the complete candidate, and audit only metadata."""
    new = _parse_value(value)
    previous = []
    def change(doc):
        old = deep_merge(DEFAULTS, doc)
        for part in key.split("."):
            old = old.get(part) if isinstance(old, dict) else None
        previous.append(old)
        try:
            return configschema.set_value(doc, key, new)
        except configschema.SchemaError as exc:
            raise ConfigError(str(exc)) from None
    config_update(change, path)
    digest = hashlib.sha256(json.dumps(new, sort_keys=True, allow_nan=False).encode()).hexdigest()
    _config_audit(key, json.dumps({"value_type": type(new).__name__, "value_digest": digest}), "ok")
    return previous[0], new


def _config_audit(key: str, detail: str, outcome: str, **extra):
    try:
        from . import audit
        audit.record("dev.config.set", str(key)[:120], detail[:300], outcome, **extra)
    except Exception:
        pass


def deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def offered_skill_groups(tools, cfg=None) -> list:
    """The optional skill groups (`skills.optional`) the setup wizard offers for the cloud and infrastructure tools
    the developer picked (toolchain.CLOUDS keys): ["iac"] for terraform. Offered, never installed by default."""
    picked = set(tools or [])
    optional = ((cfg if cfg is not None else CFG).get("skills") or {}).get("optional") or {}
    return [g for g, when in optional.items() if picked & set(when or [])]


def config_snapshot(path=None) -> tuple:
    """Fresh effective values and explicit diagnostics; with any error the mode is guided."""
    try:
        raw = read_config(path)
    except ConfigError as exc:
        cfg = configschema.effective({})[0]
        cfg["work"]["mode"] = "guided"
        return cfg, [str(exc)]
    cfg, errors = configschema.effective(raw)
    # Domain provenance checks run after initialization, without importing adapters while core loads.
    if "CFG" in globals() and not errors:
        try:
            _validate_config(raw)
        except ConfigError as exc:
            errors.append(str(exc))
    return cfg, errors


def load_config() -> dict:
    cfg, errors = config_snapshot()
    if errors:
        hint = config_restore_hint()
        print("devkit: invalid configuration; run dev config show or dev config edit: " +
              "; ".join(configschema.describe(errors)) + (f"; {hint}" if hint else ""), file=sys.stderr)
        cfg.setdefault("work", {})["mode"] = "guided"
    root = os.environ.get("DEV_ROOT")
    if root:
        cfg["dev_root"] = root
    return cfg


def retired_settings(path=None) -> list:
    """One line per retired key the saved configuration still holds (configschema.RETIRED); none when it cannot be
    read, which `dev config validate` reports on its own."""
    try:
        return configschema.retired_notes(read_config(path))
    except ConfigError:
        return []


def expand(p) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(str(p))))


# ---- whether a person runs this process at a terminal
#
# A command asks a question only when someone can answer it: a process in a terminal (it has a controlling terminal),
# with no agent among its parent processes and no agent's marker in its environment. The agents' shell commands and
# devkit's hooks have no controlling terminal and run below the agent, so they never get a question; they get the
# command's default instead. Nothing here decides what a caller may do.

AGENT_MARKERS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PID", "CODEX_SANDBOX", "CODEX_THREAD_ID",
                 "DEVKIT_SESSION", "DEVKIT_AGENT", "DEVKIT_FROM_AGENT", "DEVKIT_SPEAKER")
_LOOKUP = object()


def is_agent_process(args: str) -> bool:
    """True when a command line is an agent: Claude Code, Codex, or their desktop apps (by the executable's name)."""
    words = (args or "").split()
    if not words:
        return False
    exe = os.path.basename(words[0]).casefold()
    if exe in ("claude", "codex") or "/claude/versions/" in words[0]:
        return True
    return exe.startswith("node") and any("claude-code" in w or "@openai/codex" in w for w in words[1:3])


def _ancestry():
    """Command lines of this process's parents, nearest first, up to launchd; None when `ps` gives no answer."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,args="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    procs = {}
    for ln in out.splitlines():
        parts = ln.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            procs[int(parts[0])] = (int(parts[1]), parts[2] if len(parts) > 2 else "")
    if not procs:
        return None
    chain, pid = [], os.getppid()
    while pid > 1 and pid in procs and len(chain) < 64:
        pid, args = procs[pid]
        chain.append(args)
    return chain


def _has_terminal() -> bool:
    try:
        os.close(os.open("/dev/tty", os.O_RDONLY))
        return True
    except OSError:
        return False


def set_by_developer(environ=None, ancestry=_LOOKUP, terminal=None) -> bool:
    """True when this process runs in the developer's terminal: no agent marker in its environment, no agent among
    its parents (ancestry: their command lines; None when unknown), and a controlling terminal."""
    env = os.environ if environ is None else environ
    if any(env.get(k) for k in AGENT_MARKERS):
        return False
    chain = _ancestry() if ancestry is _LOOKUP else ancestry
    if chain is None or any(is_agent_process(a) for a in chain):
        return False
    return _has_terminal() if terminal is None else bool(terminal)


def config_file(environ=None, home=None) -> Path:
    """The configuration file devkit reads: DEVKIT_CONFIG when set, else ~/.config/devkit/config.json."""
    env = os.environ if environ is None else environ
    val = env.get("DEVKIT_CONFIG")
    return expand(val) if val else Path(home or HOME) / ".config" / "devkit" / "config.json"


STATE = HOME / ".local" / "state" / "devkit"  # before load_config, whose error names a kept copy in STATE/backups
CONFIG_FILE = config_file()
CFG = load_config()
DEV = expand(CFG["dev_root"])
HANDOFF = expand(CFG["handoff_dir"]) if CFG.get("handoff_dir") else DEV / ".handoff"
WT_ROOT = DEV / ".worktrees"
TMP_ROOT = DEV / ".tmp"
LOCAL = DEV / "_local"
NOTES = HANDOFF / "projects"
LOGDIR = HANDOFF / "log"
# the repository whose issues hold devkit's public bug registry; nothing else names it, so a rebrand changes one line
DEVKIT_REPO = "fireflysoftwaresolutions/devkit"
RESERVED_TOP = {HANDOFF.name, ".worktrees", ".tmp", "_local"}
CLAUDE_HOME = HOME / ".claude"
CODEX_HOME = expand(os.environ.get("CODEX_HOME", str(HOME / ".codex")))
AGENTS_HOME = HOME / ".agents"


def devkit_roots(home=None) -> list:
    """devkit's state, configuration and install folders: ~/.local/state/devkit, ~/.config/devkit and
    ~/.local/share/devkit."""
    h = Path(home) if home else HOME
    return [h / ".local" / "state" / "devkit", h / ".config" / "devkit", h / ".local" / "share" / "devkit"]


def ensure_roots(home=None):
    """Create devkit's three roots, readable and writable by the developer's account only (0700): a root's mode
    protects every file below it from the Mac's other accounts. devkit calls it whenever it starts, `dev install`
    included. Never raises: a root it cannot create or change keeps what it has."""
    for root in devkit_roots(home):
        try:
            root.mkdir(parents=True, exist_ok=True)
            if stat.S_IMODE(root.stat().st_mode) != 0o700:
                os.chmod(root, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions (0700 is owner-only: this tightens the root)
        except OSError:
            pass


ensure_roots()

# --------------------------------------------------------------------------- shell

GIT_ENV = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C")


def sh(args, cwd=None, timeout=30, env=None, input=None):
    """Run a command; never raises. Returns (rc, stdout, stderr)."""
    try:
        p = subprocess.run([str(a) for a in args], cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=env or GIT_ENV, input=input)
        return p.returncode, p.stdout.rstrip("\n"), p.stderr.strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return 127, "", str(e)


def git(path, *args, timeout=30):
    if path is None:
        return 128, "", "unknown directory"
    return sh(["git", "-C", str(path), *args], timeout=timeout)


def gitout(path, *args, timeout=30) -> str:
    rc, out, _ = git(path, *args, timeout=timeout)
    return out if rc == 0 else ""


def have(tool: str) -> bool:
    from shutil import which
    return which(tool) is not None


# --------------------------------------------------------------------------- github identity

def _cached(name: str, max_age: float, producer):
    f = STATE / "cache" / f"{name}.json"
    try:
        if f.exists() and time.time() - f.stat().st_mtime < max_age:
            return json.loads(f.read_text())
    except (OSError, ValueError):
        pass
    value = producer()
    if value is not None:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(value))
        return value
    try:
        return json.loads(f.read_text()) if f.exists() else None
    except (OSError, ValueError):
        return None


def gh_user() -> dict:
    def produce():
        rc, out, _ = sh(["gh", "api", "user", "--jq", "{login, id, name, email}"], timeout=15)
        if rc != 0 or not out:
            return None
        try:
            user = json.loads(out)
        except ValueError:  # gh printed a notice or an error page instead of the user: no account is known
            return None
        return user if isinstance(user, dict) else None
    return _cached("gh-user", 86400, produce) or {}


def github_user() -> str:
    return CFG.get("github_user") or gh_user().get("login", "")


def handoff_repo() -> str:
    return CFG.get("handoff_repo") or (f"{github_user()}/handoff" if github_user() else "")


def owners() -> set:
    """GitHub accounts this developer belongs to: the top-level folders of DEV."""
    def produce():
        rc, out, _ = sh(["gh", "api", "user/orgs", "--paginate", "--jq", ".[].login"], timeout=20)
        return out.split() if rc == 0 else None
    orgs = _cached("gh-orgs", 86400, produce) or []
    user = github_user()
    return {o.lower() for o in orgs + ([user] if user else [])}


# --------------------------------------------------------------------------- remotes & layout

DEFAULT_HOST = (CFG.get("default_host") or "github.com").lower()
HOST_DIRS = {k.lower(): v for k, v in (CFG.get("host_dirs") or {}).items()}

_URL_RE = re.compile(r"^(?:https?|ssh|git)://(?:[^@/\s]+@)?(?P<host>[^/:\s]+)(?::\d+)?/(?P<path>[^\s]+?)(?:\.git)?/?$")
_HOSTPATH_RE = re.compile(r"^(?P<host>[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)/(?P<path>[^\s]+?)(?:\.git)?/?$")
_SCP_RE = re.compile(r"^(?:[^@/\s]+@)?(?P<host>[A-Za-z0-9.-]+):(?P<path>[^\s]+?)(?:\.git)?/?$")
_SHORT_RE = re.compile(r"^(?P<owner>[A-Za-z0-9][A-Za-z0-9-]*)/(?P<repo>[A-Za-z0-9._-]+?)(?:\.git)?$")
_PATH_NOISE = {"_git", "scm", "v3"}  # Azure DevOps (_git, v3) and Bitbucket Server (scm) URL segments


def parse_remote(url: str):
    """Any git remote -> (host, owner_path, repo) or None.

    'owner/repo' (default host) · 'https://host/a/b/repo.git' · 'git@host:a/b/repo.git' · 'host.tld/a/repo'.
    GitLab subgroups give a nested owner_path ('group/sub'); Azure DevOps gives 'org/project'.
    """
    url = (url or "").strip()
    m = _SHORT_RE.match(url)
    if m:
        return DEFAULT_HOST, m.group("owner"), m.group("repo")
    for rx in (_URL_RE, _HOSTPATH_RE, _SCP_RE):
        m = rx.match(url)
        if m:
            host = m.group("host").lower()
            if host.startswith("ssh.") and host.count(".") >= 2:
                host = host[4:]
            parts = [p for p in m.group("path").split("/") if p and p not in _PATH_NOISE]
            if len(parts) < 2:
                return None
            return host, "/".join(parts[:-1]), parts[-1]
    return None


def host_dir(host: str) -> str:
    return HOST_DIRS.get(host.lower(), host.lower())


def pid_for(host: str, owner: str, repo: str) -> str:
    """Project id = path below the dev root: 'owner/repo' on the default host, else '<host>/<owner path>/<repo>'."""
    path = f"{owner}/{repo}".lower()
    return path if host == DEFAULT_HOST else f"{host_dir(host)}/{path}"


def project_identity(main_path: Path, remote: str):
    """-> (project id, owner, repo); '_local/<name>' when the repo has no parseable remote."""
    r = parse_remote(remote)
    if r:
        return pid_for(*r), r[1], r[2]
    return f"_local/{Path(main_path).name}", None, None


def clone_source(spec: str):
    """`dev clone` argument -> (pid, url, host) or None."""
    r = parse_remote(spec)
    if not r:
        return None
    host, owner, repo = r
    url = spec if ("://" in spec or ("@" in spec and ":" in spec)) else f"https://{host}/{owner}/{repo}.git"
    return pid_for(host, owner, repo), url, host


def canonical_path(pid: str) -> Path:
    return DEV / pid


def note_path(pid: str) -> Path:
    return NOTES / (pid + ".md")


def real(p) -> str:
    return os.path.realpath(os.path.expanduser(str(p)))


def is_under(path, root) -> bool:
    p, r = real(path), real(root)
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def temp_roots() -> list:
    roots = {"/tmp", "/private/tmp", "/var/folders", "/private/var/folders", str(TMP_ROOT)}
    for k in ("TMPDIR", "CLAUDE_CODE_TMPDIR"):
        if os.environ.get(k):
            roots.add(os.environ[k])
    return sorted({real(r) for r in roots})


def is_temp(path) -> bool:
    return any(is_under(path, r) for r in temp_roots())


def claude_slug(path) -> str:
    """Claude Code's per-project folder name under ~/.claude/projects and the temp dir."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def tilde(p) -> str:
    s, h = str(p), str(HOME)
    return "~" + s[len(h):] if s == h or s.startswith(h + "/") else s


# --------------------------------------------------------------------------- processes

_CWDS = None


def active_cwds() -> list:
    """Working directories of every process of this user (agents, shells, editors, servers)."""
    global _CWDS
    if _CWDS is None:
        _, out, _ = sh(["lsof", "-a", "-u", str(os.getuid()), "-d", "cwd", "-Fn"], timeout=20)
        _CWDS = [ln[1:] for ln in out.splitlines() if ln.startswith("n/")]
    return _CWDS


def is_active(path) -> bool:
    """True when any process (agent session, shell, editor, dev server) works inside `path`."""
    p = real(path)
    return any(c == p or c.startswith(p + "/") for c in active_cwds())


# --------------------------------------------------------------------------- misc

class Lock:
    """Advisory file lock in STATE/locks. `with Lock('sync', wait=30) as ok:`."""

    def __init__(self, name: str, wait: float = 0):
        self.path = STATE / "locks" / f"{name}.lock"
        self.wait, self.fd = wait, None

    def __enter__(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = open(self.path, "w")
        deadline = time.time() + self.wait
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                if time.time() >= deadline:
                    self.fd.close()  # busy: a caller that enters by hand and leaves on False never exits
                    self.fd = None
                    return False
                time.sleep(0.25)

    def __exit__(self, *exc):
        if self.fd is None:  # never held, or already left: nothing to unlock or close
            return
        fd, self.fd = self.fd, None  # a caller that leaves twice (an early return, then its finally) unlocks once
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except (OSError, ValueError):
            pass
        finally:
            fd.close()


def fmt_ts(ts) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


def ago(ts) -> str:
    if not ts:
        return "-"
    s = max(0, time.time() - ts)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{int(s // n)}{unit} ago"
    return "just now"


def human_kb(kb) -> str:
    if kb is None:
        return "-"
    for unit, n in (("TB", 1024 ** 3), ("GB", 1024 ** 2), ("MB", 1024)):
        if kb >= n:
            return f"{kb / n:.1f} {unit}"
    return f"{int(kb)} KB"


def du_kb(path, timeout=180, exclude=None):
    args = ["du", "-sk"] + (["-I", exclude] if exclude else []) + [str(path)]
    rc, out, _ = sh(args, timeout=timeout)
    try:
        return int(out.split()[0])
    except (IndexError, ValueError):
        return None


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def registry_cache() -> Path:
    """This Mac's projects map: the cache every `dev sync` writes (registry.write_outputs) and devkit's readers open
    (read_registry), in the state folder devkit has at the call, so a state folder set after import (a test's, a HOME
    changed later) holds it. Each Mac keeps its own; devkit 26.11.19 and older kept it in the handoff repository."""
    return STATE / "registry.json"


def registry_file() -> Path:
    """The projects map a reader opens: registry_cache(), or, before this Mac's first `dev sync` writes it, the copy
    devkit 26.11.19 and older kept in the handoff repository."""
    own, old = registry_cache(), HANDOFF / "registry.json"
    return old if not own.exists() and old.exists() else own


def read_registry(default=None):
    """This Mac's projects map as its last `dev sync` wrote it (registry_file). -> the map, or `default` when there is
    none or it cannot be read."""
    return read_json(registry_file(), default)


def replace_text(path, text: str) -> None:
    """Write `text` to `path` in one step: a temporary file of this writer's own in the same folder (mkstemp, so two
    processes writing the same file never share one), flushed to disk, then renamed over `path`. A reader sees the
    old text or the new one, never part of either. The file keeps its permissions; a new one gets what a plain write
    gives it (the umask applies). The temporary name ends in `.tmp`, which the handoff repository ignores."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            mask = os.umask(0)
            os.umask(mask)
            mode = 0o666 & ~mask
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_if_changed(path: Path, text: str) -> bool:
    """Write `text` to `path` unless it already holds it (replace_text). -> whether it wrote. Safe while other
    processes write the same file: the last rename wins whole. A caller that builds `text` from what it read (a
    read-modify-write) holds the file's lock around both (file_lock, or the lock its writers share)."""
    path = Path(path)
    if path.exists() and path.read_text() == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_text(path, text)
    return True


_FILE_LOCKS = threading.local()  # per thread: {real path: depth} of the file locks it holds


class _LockWait(Exception):
    """The timer of _flock_within ran out."""


def _flock_within(fd: int, wait: float) -> bool:
    """Take an exclusive flock on `fd` within `wait` seconds. -> whether it is held. On the main thread it waits in
    the kernel (a blocking flock, ended by a SIGALRM timer), where a waiter gets the lock when it is released, even
    from a process that takes it again at once; polling would rarely catch that moment. Other threads, which cannot
    take signals, poll. A timer the caller set keeps its deadline: when it is due during the wait its handler runs
    then, and a handler that raises ends the wait; one that returns lets the wait go on. Afterwards the caller's
    timer has the time it has left and its repeat."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        pass
    if threading.current_thread() is not threading.main_thread() or wait <= 0:
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            time.sleep(0.002)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                pass
        return False
    import signal
    started = time.monotonic()
    deadline = started + wait
    pending, period = signal.getitimer(signal.ITIMER_REAL)  # a timer the caller had set
    before = signal.getsignal(signal.SIGALRM)
    before = signal.SIG_DFL if before is None else before  # None: a handler set outside Python
    due = [started + pending if pending else None]  # when the caller's timer is due next

    def next_tick():  # the end of the wait, or the caller's timer when its Python handler is due first
        first = min(deadline, due[0]) if due[0] is not None and callable(before) else deadline
        signal.setitimer(signal.ITIMER_REAL, max(first - time.monotonic(), 0.001))

    def ran_out(signum, frame):
        now = time.monotonic()
        if due[0] is not None and now >= due[0] and callable(before):  # the caller's timer: its handler has it,
            due[0] = now + period if period else None  # and its repeat goes on
            next_tick()
            before(signum, frame)  # a caller's handler that raises ends the wait here
        if now >= deadline:
            raise _LockWait()
        next_tick()

    signal.signal(signal.SIGALRM, ran_out)
    next_tick()
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return True
    except _LockWait:
        return False
    finally:
        late = None
        while True:  # ignore our ticks first: one still on its way is dropped, and never runs ran_out after this
            try:
                signal.signal(signal.SIGALRM, signal.SIG_IGN)
                break
            except _LockWait:
                pass
            except BaseException as e:  # the caller's handler raised on that tick: raised once its timer is back
                late = e
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, before)
        if due[0] is not None:  # the caller's timer, with the time it has left and its repeat
            signal.setitimer(signal.ITIMER_REAL, max(due[0] - time.monotonic(), 0.001), period)
        if late is not None:
            raise late


@contextlib.contextmanager
def file_lock(path, wait: float = 30.0):
    """This Mac's lock on one file, held across a read-modify-write so that another devkit process changing the same
    file waits for it instead of writing over it. An advisory lock (flock) on STATE/locks/files/<hash of the path>,
    released by the system if the process dies; reentrant within a thread. -> whether it is held: False when another
    process kept it for `wait` seconds. On macOS the path's letter case is ignored, as the volume ignores it by
    default: Plan.md and plan.md are one file, so they take one lock (two files that differ only in case on a
    case-sensitive volume then share one, which costs a wait; two locks on one file would lose a write)."""
    key = os.path.realpath(str(path))
    if sys.platform == "darwin":
        key = key.lower()
    held = getattr(_FILE_LOCKS, "held", None)
    if held is None:
        held = _FILE_LOCKS.held = {}
    if held.get(key):
        held[key] += 1
        try:
            yield True
        finally:
            held[key] -= 1
        return
    lock = STATE / "locks" / "files" / f"{hashlib.sha256(key.encode()).hexdigest()[:24]}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = _lock_file(lock, wait)
    if fd is None:
        yield False
        return
    held[key] = 1
    try:
        yield True
    finally:
        held.pop(key, None)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _names(lock: Path, fd: int) -> bool:
    """Does the path `lock` still name the file open at `fd`?"""
    try:
        a, b = os.fstat(fd), os.stat(lock)
    except OSError:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _lock_file(lock: Path, wait: float):
    """An open descriptor of the lock file `lock` holding its flock, or None when another process kept it for `wait`
    seconds. `dev sleep` removes a lock file nobody holds (remove_idle_lock): one removed between this open and its
    flock is locked by nobody else, so once the flock is held the path must still name it; else the lock is taken
    again on the file the path names now."""
    deadline = time.monotonic() + wait
    while True:
        fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            got = _flock_within(fd, max(0.0, deadline - time.monotonic()))
            if got and _names(lock, fd):
                return fd
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)
        if not got:
            return None


def remove_idle_lock(lock: Path) -> bool:
    """Remove one of file_lock's lock files that no process holds: only while holding its flock, taken without
    waiting, and only while the path still names the file locked (file_lock, which re-checks after its flock, then
    never holds a removed one). -> whether it was removed."""
    try:
        fd = os.open(str(lock), os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False  # a process holds it now
        if not _names(lock, fd):
            return False
        os.unlink(lock)
        return True
    finally:
        os.close(fd)  # the flock goes with it


# --------------------------------------------------------------------------- the pinned install (spec C10)
#
# `dev install` copies one commit of the devkit checkout, read-only and with a manifest of checksums, to
# ~/.local/share/devkit/releases/<version>-<sha>/ and records it in ~/.local/share/devkit/install.json. The agents'
# hooks, the status line, ~/.local/bin/dev and the scheduled jobs run that copy, so an edit in the checkout reaches
# them only through the next `dev install`. `bin/dev` run from the checkout still runs the checkout.
# The paths are functions of HOME so that tests can move HOME.

PIN_MANIFEST = ".devkit-manifest.json"


def share_dir() -> Path:
    return HOME / ".local" / "share" / "devkit"


def pin_root() -> Path:
    return share_dir() / "releases"


def install_record_file() -> Path:
    return share_dir() / "install.json"


def dev_link() -> Path:
    """The `dev` a shell finds: the link that `dev install` points at the pinned copy's bin/dev."""
    return HOME / ".local" / "bin" / "dev"


def dev_commands() -> list:
    """Every place a shell can find `dev`: the link and `dev` in each other folder on PATH (real folder paths), except
    folders in the workspace or a temp folder, where a repository's own bin/dev is code under review."""
    out, seen = [], set()
    for d in [str(dev_link().parent)] + os.environ.get("PATH", "").split(os.pathsep):
        if not d or not os.path.isabs(d):
            continue
        rd = real(d)
        if rd in seen or is_under(rd, DEV) or is_temp(rd):
            continue
        seen.add(rd)
        out.append(Path(rd) / "dev")
    return out


def install_record() -> dict:
    """What `dev install` installed last: version, commit, path of the pinned copy, source checkout ({} if none)."""
    rec = read_json(install_record_file(), {})
    return rec if isinstance(rec, dict) else {}


def is_pinned(path=None) -> bool:
    """True when `path` (default: the code that runs now) is a pinned copy: a folder of pin_root() with a manifest."""
    p = Path(path or DEVKIT_HOME)
    return p.resolve().parent == pin_root().resolve() and (p / PIN_MANIFEST).is_file()


def runtime_home() -> Path:
    """The devkit copy that hooks, the installed `dev`, the status line and the jobs run: this copy when it is pinned
    and not older than the release `dev install` recorded, else the pinned copy that `dev install` recorded. Settings
    written from a checkout or an older copy point there too, never at the checkout or back at the older copy. Before
    the first pinned install, the copy that runs now."""
    if is_pinned(DEVKIT_HOME) and not older_copy():
        return DEVKIT_HOME
    path = install_record().get("path")
    return Path(path) if path and is_pinned(path) else DEVKIT_HOME


# An older devkit never overwrites a newer install. A Claude Code or Codex session keeps the hooks it started with,
# so after `dev install` pins a newer release, the sessions that were already open still run the older copy. That copy
# sees the newer settings as drift; before this check it put its own back (hooks, CLAUDE.md, AGENTS.md, the git
# ignore). Now every writer of devkit-managed configuration asks older_copy() first, and an older copy changes
# nothing: it records config.repair-skipped with both versions, and the session is told to restart.
#
# install.json is believed only when the copy it names holds up: a pinned copy (a folder of the releases folder, with
# its manifest) whose manifest names the same version and has the checksum the record keeps. A record that does not
# hold up counts as no pin, so this copy repairs as before.

RELEASE_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]*)?")  # 26.11.5, 26.12.0-dev
_PIN_SEEN = {}  # (install.json stat, manifest stat) -> pinned_release(); install.json changes only with dev install


def version_key(version):
    """A release version ("26.11.5", or "26.12.0-dev" for an unverified pin) as numbers to compare -> (26, 11, 5),
    or None when it is not one."""
    m = RELEASE_VERSION_RE.fullmatch(str(version or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def _stat_key(path: Path):
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size, st.st_ino


def pinned_release() -> dict:
    """The release install.json pins, when the record holds up -> {version, path, name}, else {}."""
    rec_f = install_record_file()
    rec = install_record()
    path = rec.get("path")
    if not path or not is_pinned(path) or version_key(rec.get("version")) is None:
        return {}
    man_f = Path(path) / PIN_MANIFEST
    key = (str(rec_f), _stat_key(rec_f), str(man_f), _stat_key(man_f))
    if key in _PIN_SEEN:
        return dict(_PIN_SEEN[key])
    try:
        raw = man_f.read_bytes()
        man = json.loads(raw)
    except (OSError, ValueError):
        return {}
    import hashlib
    holds = isinstance(man, dict) and man.get("version") == rec["version"] and \
        hashlib.sha256(raw).hexdigest() == rec.get("manifest_sha256")
    out = {"version": rec["version"], "path": Path(path), "name": rec.get("name") or Path(path).name} if holds else {}
    _PIN_SEEN.clear()
    _PIN_SEEN[key] = out
    return dict(out)


def older_copy():
    """When the copy that runs now is older than the release install.json pins -> {running, pinned, path}; else None
    (equal, newer, nothing pinned, a record that does not hold up, or a version that is not a release)."""
    pin = pinned_release()
    running, pinned = version_key(VERSION), version_key(pin.get("version"))
    if not pin or running is None or running >= pinned:
        return None
    return {"running": VERSION, "pinned": pin["version"], "path": str(pin["path"])}


def older_copy_words(old: dict) -> str:
    """What a session hears once when it runs an older devkit than the one installed."""
    return (f"devkit {old['running']} runs in this session, but devkit {old['pinned']} is installed, so this session's "
            f"devkit changes no devkit-managed setting (the installed release keeps them). Restart the session to "
            f"load devkit {old['pinned']}'s hooks.")


def older_copy_skip(what: str):
    """For a writer of devkit-managed configuration: when this copy is older than the pinned release, record
    config.repair-skipped (what was skipped, both versions) and return older_copy(); the writer then changes nothing.
    None otherwise, with nothing recorded."""
    old = older_copy()
    if old:
        from . import audit
        audit.record("config.repair-skipped", what,
                     f"devkit {old['running']} runs here and install.json pins devkit {old['pinned']}: the older copy "
                     f"left {what} as it is", "skipped")
    return old


def release_home() -> Path:
    """The copy background `dev` commands run: the release install.json pins (when the record holds up), else this
    copy. A hook of an older session then starts the installed release, never itself."""
    pin = pinned_release()
    return pin["path"] if pin else DEVKIT_HOME


# The other copies in the releases folder are kept for `dev install --version` (a rollback), but a copy older than
# the version check above has no check at all: a session that started before `dev install` still runs it, and its
# repair writes its own, older hooks and instructions back. Its code cannot be changed, but its bin/dev reads
# the code next to its real path (os.path.realpath), so `dev install` and a repair by the pinned release turn each
# other copy's bin/dev into a link to the pinned release's bin/dev. Every hook, job and repair of an older session
# then runs the installed release. The copy's own bin/dev stays beside it (bin/.dev.retired), so a rollback can put
# it back (restore_pin).

RETIRED_DEV = ".dev.retired"


def is_current_pin() -> bool:
    """True when the copy that runs now is the release install.json pins (and the record holds up)."""
    pin = pinned_release()
    return bool(pin) and real(pin["path"]) == real(DEVKIT_HOME)


def retired_to(path):
    """The copy whose code another pinned copy's bin/dev runs once retired (a link) -> its folder, else None."""
    dev = Path(path) / "bin" / "dev"
    return Path(os.path.realpath(dev)).parent.parent if dev.is_symlink() else None


def _with_writable(folder: Path, fn):
    """Run fn() with `folder` (0555 in a pinned copy) writable by its owner for the moment; its mode comes back."""
    mode = stat.S_IMODE(folder.stat().st_mode)
    os.chmod(folder, mode | stat.S_IWUSR)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions (the owner's write, for one rename, then the read-only mode again)
    try:
        return fn()
    finally:
        os.chmod(folder, mode)


def retire_other_pins() -> list:
    """Point bin/dev of every other pinned copy at the pinned release's bin/dev (see above) -> the names changed now.
    Nothing when install.json does not hold up: a link to a copy nobody verified would be worse than none."""
    pin = pinned_release()
    target = Path(pin["path"]) / "bin" / "dev" if pin else None
    if not target or target.is_symlink() or not target.is_file():
        return []
    out = []
    for d in sorted(pin_root().iterdir()):
        if d.name.startswith(".") or d.is_symlink() or not d.is_dir() or real(d) == real(pin["path"]) \
                or not is_pinned(d):
            continue
        dev, keep = d / "bin" / "dev", d / "bin" / RETIRED_DEV
        if dev.is_symlink() and os.readlink(dev) == str(target):
            continue

        def link(dev=dev, keep=keep):
            if not dev.is_symlink() and not os.path.lexists(keep):
                os.link(dev, keep)  # the copy's own bin/dev, for a rollback
            tmp = dev.with_name(f".dev.{os.getpid()}.tmp")
            if os.path.lexists(tmp):
                tmp.unlink()
            os.symlink(str(target), tmp)
            if hasattr(os, "lchmod"):  # macOS: the link reads 0555 like the rest of the copy
                os.lchmod(tmp, 0o555)
            os.replace(tmp, dev)  # one rename: a hook that starts now runs one bin/dev or the other, never none
        _with_writable(d / "bin", link)
        out.append(d.name)
    if out:
        from . import audit
        audit.record("install.retire", ", ".join(out), f"their bin/dev runs the pinned release {pin['name']} now, so "
                     f"a session that started before the install no longer writes their older settings back")
    return out


def restore_pin(path) -> bool:
    """Undo retire_other_pins for one copy, before `dev install` uses it again: its own bin/dev back, when the one
    kept beside it is what its manifest names. -> whether it was put back."""
    d = Path(path)
    dev, keep = d / "bin" / "dev", d / "bin" / RETIRED_DEV
    if not dev.is_symlink() or not keep.is_file() or keep.is_symlink():
        return False
    want = ((read_json(d / PIN_MANIFEST, {}) or {}).get("files") or {}).get("bin/dev")
    import hashlib
    if not want or hashlib.sha256(keep.read_bytes()).hexdigest() != want:
        return False
    _with_writable(d / "bin", lambda: os.replace(keep, dev))
    return True


def source_home() -> Path:
    """The devkit checkout that `dev install` pins and `dev update` updates: this copy when it is a checkout; for a
    pinned copy, the checkout it was made from."""
    if not is_pinned(DEVKIT_HOME):
        return DEVKIT_HOME
    rec = install_record()
    src = rec.get("source") if rec.get("path") and real(rec["path"]) == real(DEVKIT_HOME) else None
    src = src or (read_json(DEVKIT_HOME / PIN_MANIFEST, {}) or {}).get("source")
    return Path(src) if src else DEVKIT_HOME
