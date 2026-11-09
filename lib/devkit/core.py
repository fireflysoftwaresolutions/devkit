# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Paths, configuration and process helpers shared by every devkit module.

Stdlib only and Python 3.9 compatible, so `dev` runs on any Mac without a
virtualenv (Homebrew python3 or the system /usr/bin/python3).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
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
os.environ["PATH"] = ":".join(dict.fromkeys(_EXTRA_PATH + os.environ.get("PATH", "").split(":")))

# --------------------------------------------------------------------------- config

DEFAULTS = {
    "dev_root": "~/Development",
    "github_user": "",              # empty: detected from `gh api user`
    "handoff_repo": "",             # empty: <github_user>/handoff
    "handoff_dir": "",              # empty: <dev_root>/.handoff
    "sync": {"push": True, "interval_minutes": 60},
    "gc": {
        "auto": True,               # the background job applies the safe class automatically
        "grace_hours": 24,          # merged / fully-contained worktrees idle this long are removed
        "idle_artefact_days": 7,    # regenerable build dirs are pruned in worktrees idle this long
        "temp_days": 7,             # temp entries older than this are purged
        "docker": True,             # also prune dangling images and build cache older than 7 days (never volumes)
    },
    "docker": {"cpu": 6, "memory": 12, "disk": 80, "vm_type": "vz", "rosetta": True,
               "mount_type": "virtiofs", "autostart": False},
    "editor": "code",
    "agents": {"claude": True, "codex": True},
    "claude": {"default_mode": "bypassPermissions"},
    "codex": {"mode": "full", "approval_policy": "never", "sandbox_mode": "danger-full-access"},
    "stacks": ["java", "python", "php", "node"],
    # skills.groups: the optional skill groups of config/skills.json this Mac installs. skills.optional: groups never
    # installed by default, which the setup wizard offers when the developer picks one of these tools.
    "skills": {"groups": ["security-deep", "postgres", "observability"], "optional": {"iac": ["terraform"]},
               "enforce": True, "rules": []},
    # ext: the developer's own extensions on top of devkit's (`dev ext add|remove|enable|disable`, spec P-fym583l7sg1)
    "ext": {"add": [], "remove": [], "disable": [], "enable": []},
    "language": "en-US",
    "hosts": ["github"],
    "clouds": [],
    "packages": {"auto": True},
    "ui": {"powerline": False},
    # progress: updates, timing, and the crew's slots (spec P-fxtwig4hqvx). quota_steps scales max_parallel by the
    # plan's usage (5-hour and weekly windows), and from quota_percent one member works at a time; slot_weights is
    # what one launch costs by kind; reserve keeps slots for the developer's own request (docs/configuration.md).
    "progress": {"nudge_minutes": 8, "notify": True, "notify_after_minutes": 10, "slow_tool_seconds": 20,
                 "max_parallel": 4, "max_depth": 2, "quota_percent": 90,
                 "quota_steps": {"50": 1.0, "70": 0.75, "85": 0.5},
                 "slot_weights": {"builder": 1.0, "review": 0.5, "read": 0.5, "scout": 0.0},
                 "reserve": 1, "stale_agent_hours": 6, "stale_agent_minutes": 90},
    "work": {"lock_hours": 8},
    "lock": False,
    "handoff": {"enforce": True},
    "canary": {"enforce": True},
    "dashboard": {"idle_minutes": 30},  # the live dashboard server stops after this long with no page open
    # the audit trail (docs/audit.md). The team policy (<handoff>/policy/audit.json) sets a floor: a machine may
    # record more and keep longer, never less. Only the developer changes these, in a terminal (spec C11).
    # Retention (compliance review): the team copy is read more widely, so it keeps no longer than this machine by
    # default; floor_months is never under AUDIT_FLOOR_MIN. Pruning the handoff copy leaves the months in git history.
    "audit": {
        "publish": True,            # the daily job copies this machine's records to the handoff repository
        "level": "changes",         # security | changes | all (all: also read-only commands)
        "retention": {"local_months": 13, "team_months": 13, "floor_months": 3},
        "hold": False,              # a legal hold: nothing is pruned, here or in the handoff copy
        "forward": "",              # "" | a file (JSONL lines) | "syslog" | "syslog://host:port" (CEF lines)
        "redact": {"extra": []},    # more regular expressions for the redactor
    },
}

AUDIT_LEVELS = ("security", "changes", "all")  # from the fewest records to the most
# the least floor_months may be: 3 months covers the time it takes to notice an incident and investigate it. Less
# data comes from audit.level security, not from a shorter floor (compliance review, GDPR art. 5(1)(e) and 32)
AUDIT_FLOOR_MIN = 3


def _months_ok(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 1200


def _floor_ok(v) -> bool:
    return _months_ok(v) and v >= AUDIT_FLOOR_MIN


def _patterns_ok(v) -> bool:
    return isinstance(v, list) and all(isinstance(p, str) and p for p in v)


def _bad_patterns(v: list) -> list:
    bad = []
    for p in v:
        try:
            re.compile(p)
        except re.error as e:
            bad.append(f"{p!r} ({e})")
    return bad


def _forward_ok(v) -> bool:
    if not isinstance(v, str):
        return False
    if v in ("", "syslog"):
        return True
    if v.startswith("syslog://"):
        return bool(re.fullmatch(r"syslog://[A-Za-z0-9.-]+:\d{1,5}", v))
    return v.startswith(("/", "~"))


# the audit section's keys -> (the check, what the value must be)
AUDIT_RULES = {
    ("publish",): (lambda v: isinstance(v, bool), "true or false"),
    ("level",): (lambda v: v in AUDIT_LEVELS, "security, changes or all"),
    ("hold",): (lambda v: isinstance(v, bool), "true or false"),
    ("forward",): (_forward_ok, '"" (off), an absolute file path, "syslog" or "syslog://host:port"'),
    ("retention", "local_months"): (_months_ok, "a whole number of months from 1 to 1200"),
    ("retention", "team_months"): (_months_ok, "a whole number of months from 1 to 1200"),
    ("retention", "floor_months"): (_floor_ok, f"a whole number of months from {AUDIT_FLOOR_MIN} to 1200 "
                                               f"({AUDIT_FLOOR_MIN} is the hard minimum)"),
    ("redact", "extra"): (_patterns_ok, "a list of regular expressions"),
}


def audit_config(raw, fill: bool = True):
    """An audit section checked against AUDIT_RULES -> (values, errors). `fill`: every key, a default where the
    value is missing or wrong; else only the keys `raw` sets with a valid value (a team policy's floor)."""
    values = json.loads(json.dumps(DEFAULTS["audit"])) if fill else {}
    errors = []
    if raw is None:
        return values, errors
    if not isinstance(raw, dict):
        return values, ["audit must be an object"]
    groups = {path[0] for path in AUDIT_RULES if len(path) == 2}
    for key, val in raw.items():
        if key in groups:
            if not isinstance(val, dict):
                errors.append(f"audit.{key} must be an object")
                continue
            items = [((key, k), v) for k, v in val.items()]
        else:
            items = [((key,), val)]
        for path, v in items:
            name = "audit." + ".".join(path)
            rule = AUDIT_RULES.get(path)
            if not rule:
                errors.append(f"unknown key {name}")
                continue
            if not rule[0](v):
                errors.append(f"{name} must be {rule[1]} (got {json.dumps(v, ensure_ascii=False)[:40]})")
                continue
            if path == ("redact", "extra") and _bad_patterns(v):  # the valid patterns still apply
                errors += [f"{name}: {b} is not a regular expression" for b in _bad_patterns(v)]
                v = [p for p in v if not _bad_patterns([p])]
            if len(path) == 2:
                values.setdefault(path[0], {})[path[1]] = v
            else:
                values[path[0]] = v
    return values, errors


def _number_in(lo, hi, whole=False):
    def ok(v):
        if isinstance(v, bool) or not isinstance(v, int if whole else (int, float)):
            return False
        return lo <= v <= hi
    return ok


def _steps_ok(v) -> bool:
    """quota_steps: {"<usage percent>": <share of max_parallel, 0 to 1>}, such as {"70": 0.75}."""
    if not isinstance(v, dict):
        return False
    for k, f in v.items():
        try:
            pct = float(k)
        except (TypeError, ValueError):
            return False
        if not 0 <= pct <= 100 or not _number_in(0, 1)(f):
            return False
    return True


SLOT_KINDS = ("builder", "review", "read", "scout")


def _weights_ok(v) -> bool:
    return isinstance(v, dict) and all(k in SLOT_KINDS and _number_in(0, 1)(w) for k, w in v.items())


# the progress section's keys -> (the check, what the value must be)
PROGRESS_RULES = {
    "nudge_minutes": (_number_in(1, 240), "a number of minutes from 1 to 240"),
    "notify": (lambda v: isinstance(v, bool), "true or false"),
    "notify_after_minutes": (_number_in(0, 1440), "a number of minutes from 0 to 1440"),
    "slow_tool_seconds": (_number_in(1, 3600), "a number of seconds from 1 to 3600"),
    "max_parallel": (_number_in(1, 10, whole=True), "a whole number from 1 to 10"),
    "max_depth": (_number_in(1, 5, whole=True), "a whole number from 1 to 5"),
    "quota_percent": (_number_in(1, 100), "a usage share in percent from 1 to 100"),
    "quota_steps": (_steps_ok, 'an object of usage percent to the share of max_parallel from 0 to 1, such as '
                               '{"70": 0.75}'),
    "slot_weights": (_weights_ok, "an object of builder, review, read or scout to a cost from 0 to 1"),
    "reserve": (_number_in(0, 9, whole=True), "a whole number of slots from 0 to 9"),
    "stale_agent_hours": (_number_in(1, 168), "a number of hours from 1 to 168"),
    "stale_agent_minutes": (_number_in(5, 1440), "a number of minutes from 5 to 1440"),
}


def progress_config(raw):
    """A progress section checked against PROGRESS_RULES -> (values, errors): every key, with its default where the
    value is missing or wrong. quota_steps and slot_weights add to their defaults, key by key."""
    values = json.loads(json.dumps(DEFAULTS["progress"]))
    if raw is None:
        return values, []
    if not isinstance(raw, dict):
        return values, ["progress must be an object"]
    errors = []
    for key, v in raw.items():
        rule = PROGRESS_RULES.get(key)
        if not rule:
            errors.append(f"unknown key progress.{key}")
        elif not rule[0](v):
            errors.append(f"progress.{key} must be {rule[1]} (got {json.dumps(v, ensure_ascii=False)[:40]})")
        elif isinstance(values.get(key), dict):
            values[key] = {**values[key], **v}
        else:
            values[key] = v
    return values, errors


def config_errors(cfg: dict) -> list:
    """What is wrong in the checked sections of a devkit configuration (audit, progress). -> [message]"""
    cfg = cfg or {}
    out = audit_config(cfg.get("audit"))[1] if "audit" in cfg else []
    return out + (progress_config(cfg.get("progress"))[1] if "progress" in cfg else [])


class ConfigError(ValueError):
    """Why `dev config set` changed nothing, and what to do instead."""


def _parse_value(text: str):
    """A value as the developer types it: JSON when it parses (6, true, {"70": 0.5}), else the text itself."""
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def config_set(key: str, value: str, path=None, developer=None) -> tuple:
    """`dev config set <key> <value>`: one key of devkit's configuration file, validated, written atomically and
    audited -> (old value, new value). Only the developer sets it, in a terminal (set_by_developer, spec C11, K1):
    an agent's command, a hook and a job are refused, whatever the key. The old value comes from that file and
    devkit's defaults, never from the effective configuration."""
    path = Path(path or CONFIG_FILE)
    try:
        who = set_by_developer() if developer is None else bool(developer)
    except Exception:
        who = False  # a check that cannot decide refuses (fail closed)
    if not who:
        _config_audit(key, "", "refused", why="not the developer's terminal")
        raise ConfigError("dev config set changes devkit's configuration, which only the developer does, in a "
                          "terminal (spec C11, K1). Ask them, with the key, the value and why.")
    parts = [p for p in str(key or "").split(".") if p]
    if not parts or parts[0] not in DEFAULTS or len(parts) > 2 or (len(parts) == 2 and
                                                                     not isinstance(DEFAULTS[parts[0]], dict)):
        raise ConfigError(f"unknown key {key!r}: dev config show lists the keys, docs/configuration.md explains them")
    new = _parse_value(value)
    section, name = parts[0], parts[1] if len(parts) == 2 else None
    if name is not None and name not in DEFAULTS[section] and section in ("progress", "audit"):
        raise ConfigError(f"unknown key {key}: docs/configuration.md lists the {section} keys")
    try:
        doc = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError) as e:
        raise ConfigError(f"{tilde(path)} is not valid JSON ({e}): fix it with dev config edit first") from None
    if not isinstance(doc, dict):
        raise ConfigError(f"{tilde(path)} must hold a JSON object: fix it with dev config edit first")
    own = doc.get(section) if isinstance(doc.get(section), dict) else {}
    if name is None:
        old = doc.get(section, DEFAULTS[section])
        trial = dict(doc, **{section: new})
    else:
        old = own.get(name, DEFAULTS[section].get(name))
        trial = dict(doc, **{section: dict(own, **{name: new})})
    try:
        problems = [e for e in config_errors({section: trial[section]} if section in ("audit", "progress") else {})
                    if key in e or "must be an object" in e]
    except Exception as e:  # a value the checks cannot judge is refused (fail closed)
        problems = [f"{key}: the value could not be checked ({type(e).__name__})"]
    default = DEFAULTS[section].get(name) if name is not None else DEFAULTS[section]
    if not problems and section not in ("audit", "progress") and default is not None and \
            type(new) is not type(default) and not (isinstance(default, float) and isinstance(new, int)):
        problems = [f"{key} must be {type(default).__name__} like its default ({json.dumps(default)})"]
    if problems:
        raise ConfigError("; ".join(problems))
    path = Path(os.path.realpath(path))  # a linked file (dotfiles) stays linked: the write goes to its target
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(trial, indent=2, ensure_ascii=False) + "\n")
        os.chmod(tmp, mode)  # the file keeps its own permissions
        os.replace(tmp, path)
    except OSError as e:
        Path(tmp).unlink(missing_ok=True)
        raise ConfigError(f"could not write {tilde(path)}: {e}") from None
    _config_audit(key, f"{json.dumps(old, ensure_ascii=False)} → {json.dumps(new, ensure_ascii=False)}", "ok")
    return old, new


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


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if CONFIG_FILE.exists():
        try:
            cfg = deep_merge(cfg, json.loads(CONFIG_FILE.read_text()))
        except (OSError, ValueError):
            pass
    root = os.environ.get("DEV_ROOT")
    if root and expand(root) != expand(cfg["dev_root"]):
        if set_by_developer():
            cfg["dev_root"] = root
        else:  # the handoff (policies, plans, tasks) lives below it, so it is configuration like DEVKIT_CONFIG
            _refuse_env("DEV_ROOT", root, f"the dev_root of {CONFIG_FILE}")
    return cfg


def expand(p) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(str(p))))


# ---- who set DEVKIT_CONFIG or DEV_ROOT (spec C11, K1)
#
# An agent's command can set them, and so can a nested session it starts; devkit would then read a configuration, a
# handoff and approvals nobody gave. devkit takes a DEVKIT_CONFIG outside ~/.config/devkit/config.json, or a DEV_ROOT
# other than the configured dev_root, only from the developer: a process in a terminal (it has a controlling
# terminal), with no agent among its parent processes and no agent's marker in its environment. The agents' shell
# commands and devkit's hooks have no controlling terminal and run below the agent, so theirs never count; devkit
# then reads the standard file, says so on stderr and drops the variable for the processes it starts. The guard
# refuses the same assignments in an agent's command text; this check covers what the text does not show.

AGENT_MARKERS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PID", "CODEX_SANDBOX", "CODEX_THREAD_ID",
                 "DEVKIT_SESSION", "DEVKIT_AGENT", "DEVKIT_FROM_AGENT")
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


def _refuse_env(name: str, value: str, instead: str):
    print(f"devkit: ignoring {name}={value}: devkit takes it only from the developer, in a terminal, not from an "
          f"agent's command or hook (spec C11). Using {instead}.", file=sys.stderr)
    os.environ.pop(name, None)


def config_file(environ=None, home=None) -> Path:
    """The configuration file devkit reads: DEVKIT_CONFIG when it names the standard place or the developer set it
    in a terminal (set_by_developer), else ~/.config/devkit/config.json."""
    env = os.environ if environ is None else environ
    std = Path(home or HOME) / ".config" / "devkit" / "config.json"
    val = env.get("DEVKIT_CONFIG")
    if not val or os.path.realpath(expand(val)).casefold() == os.path.realpath(std).casefold():
        return std
    if set_by_developer(env):
        return expand(val)
    _refuse_env("DEVKIT_CONFIG", val, str(std))
    return std


CONFIG_FILE = config_file()
CFG = load_config()
DEV = expand(CFG["dev_root"])
HANDOFF = expand(CFG["handoff_dir"]) if CFG.get("handoff_dir") else DEV / ".handoff"
WT_ROOT = DEV / ".worktrees"
TMP_ROOT = DEV / ".tmp"
LOCAL = DEV / "_local"
NOTES = HANDOFF / "projects"
LOGDIR = HANDOFF / "log"
STATE = HOME / ".local" / "state" / "devkit"
# the repository whose issues hold devkit's public bug registry; nothing else names it, so a rebrand changes one line
DEVKIT_REPO = "fireflysoftwaresolutions/devkit"
RESERVED_TOP = {HANDOFF.name, ".worktrees", ".tmp", "_local"}
CLAUDE_HOME = HOME / ".claude"
CODEX_HOME = expand(os.environ.get("CODEX_HOME", str(HOME / ".codex")))
AGENTS_HOME = HOME / ".agents"


def devkit_roots(home=None) -> list:
    """devkit's state, configuration and install folders: ~/.local/state/devkit, ~/.config/devkit and
    ~/.local/share/devkit. What lies below them decides what the guard and the gates allow (spec C11)."""
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
        return json.loads(out) if rc == 0 and out else None
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
                    return False
                time.sleep(0.25)

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            self.fd.close()
        except (OSError, ValueError):
            pass


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


def write_if_changed(path: Path, text: str) -> bool:
    path = Path(path)
    if path.exists() and path.read_text() == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
    return True


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
# its manifest) whose manifest names the same version and has the checksum the record keeps. Both live in
# ~/.local/share/devkit, which no agent may write (the guard), so a forged record cannot stop a repair; and a record
# that does not hold up counts as no pin, so this copy repairs as before.

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
# repair writes its own, older hooks, guard and instructions back. Its code cannot be changed, but its bin/dev reads
# the code next to its real path (os.path.realpath), so `dev install` and a repair by the pinned release turn each
# other copy's bin/dev into a link to the pinned release's bin/dev. Every hook, job and repair of an older session
# then runs the installed release. The copy's own bin/dev stays beside it (bin/.dev.retired), so a rollback can put
# it back (restore_pin). The releases folder is the guard's to protect, so no agent sets or undoes a link.

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
