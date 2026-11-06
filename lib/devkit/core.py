# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""Paths, configuration and process helpers shared by every devkit module.

Stdlib only and Python 3.9 compatible, so `dev` runs on any Mac without a
virtualenv (Homebrew python3 or the system /usr/bin/python3).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import time
from pathlib import Path

DEVKIT_HOME = Path(__file__).resolve().parents[2]
HOME = Path.home()
COPYRIGHT = "© 2026 Firefly Software Solutions Inc. All rights reserved."
VERSION = (DEVKIT_HOME / "VERSION").read_text().strip() if (DEVKIT_HOME / "VERSION").exists() else "0.0.0"

# Tools are looked up on a predictable PATH even when a hook runs from an app
# (Claude Desktop, Codex app, launchd) whose PATH is minimal.
_EXTRA_PATH = ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", str(HOME / ".local" / "bin")]
os.environ["PATH"] = ":".join(dict.fromkeys(_EXTRA_PATH + os.environ.get("PATH", "").split(":")))

# --------------------------------------------------------------------------- config

CONFIG_FILE = Path(os.environ.get("DEVKIT_CONFIG", str(HOME / ".config" / "devkit" / "config.json")))

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
    "lock": False,
    "handoff": {"enforce": True},
    "canary": {"enforce": True},
}


def deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if CONFIG_FILE.exists():
        try:
            cfg = deep_merge(cfg, json.loads(CONFIG_FILE.read_text()))
        except (OSError, ValueError):
            pass
    if os.environ.get("DEV_ROOT"):
        cfg["dev_root"] = os.environ["DEV_ROOT"]
    return cfg


def expand(p) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(str(p))))


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
