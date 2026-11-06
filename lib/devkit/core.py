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
    "language": "en-US",
    "hosts": ["github"],
    "clouds": [],
    "packages": {"auto": True},
    "ui": {"powerline": False},
    "progress": {"nudge_minutes": 8, "notify": True, "notify_after_minutes": 10, "slow_tool_seconds": 20},
    "work": {"lock_hours": 8},
    "lock": False,
    "handoff": {"enforce": True},
    "canary": {"enforce": True},
    "dashboard": {"idle_minutes": 30},  # the live dashboard server stops after this long with no page open
    # the audit trail (docs/audit.md). The team policy (<handoff>/policy/audit.json) sets a floor: a machine may
    # record more and keep longer, never less. Only the developer changes these, in a terminal (spec C11).
    "audit": {
        "publish": True,            # the daily job copies this machine's records to the handoff repository
        "level": "changes",         # security | changes | all (all: also read-only commands)
        "retention": {"local_months": 13, "team_months": 24, "floor_months": 3},
        "hold": False,              # a legal hold: nothing is pruned, here or in the handoff copy
        "forward": "",              # "" | a file (JSONL lines) | "syslog" | "syslog://host:port" (CEF lines)
        "redact": {"extra": []},    # more regular expressions for the redactor
    },
}

AUDIT_LEVELS = ("security", "changes", "all")  # from the fewest records to the most


def _months_ok(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 1200


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
    ("retention", "floor_months"): (_months_ok, "a whole number of months from 1 to 1200"),
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


def config_errors(cfg: dict) -> list:
    """What is wrong in the checked sections of a devkit configuration (today: audit). -> [message]"""
    return audit_config((cfg or {}).get("audit"))[1] if "audit" in (cfg or {}) else []


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
                os.chmod(root, 0o700)
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
    """The devkit copy that hooks, the installed `dev`, the status line and the jobs run: this copy when it is pinned,
    else the pinned copy that `dev install` recorded. Settings written from a checkout point there too, never at the
    checkout. Before the first pinned install, the copy that runs now."""
    if is_pinned(DEVKIT_HOME):
        return DEVKIT_HOME
    path = install_record().get("path")
    return Path(path) if path and is_pinned(path) else DEVKIT_HOME


def source_home() -> Path:
    """The devkit checkout that `dev install` pins and `dev update` updates: this copy when it is a checkout; for a
    pinned copy, the checkout it was made from."""
    if not is_pinned(DEVKIT_HOME):
        return DEVKIT_HOME
    rec = install_record()
    src = rec.get("source") if rec.get("path") and real(rec["path"]) == real(DEVKIT_HOME) else None
    src = src or (read_json(DEVKIT_HOME / PIN_MANIFEST, {}) or {}).get("source")
    return Path(src) if src else DEVKIT_HOME
