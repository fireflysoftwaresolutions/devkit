# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""What devkit manages in Claude Code and Codex, and keeping it that way.

  diff()      desired state (devkit config) vs what is installed, per target, as a list of Drift
  repair()    restore the managed keys only (fast, no package installs), log what was restored
  lock()      Claude Code: install a managed-settings drop-in that user settings cannot override (sudo)
  unlock()

The source of truth is the devkit repository (config/claude/*, config/codex/*,
agents/) plus ~/.config/devkit/config.json. Changing an agent setting that devkit
manages means changing devkit's configuration and running `dev install` (or
`dev config repair`); edits made directly in the agents are detected and reverted.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from . import core
from .core import AGENTS_HOME, CLAUDE_HOME, CODEX_HOME, STATE, read_json, tilde

MANAGED_DIR = Path("/Library/Application Support/ClaudeCode/managed-settings.d")
MANAGED_FILE = MANAGED_DIR / "50-devkit.json"
MISSING = object()


class Drift:
    __slots__ = ("target", "key", "current", "desired")

    def __init__(self, target, key, current, desired):
        self.target, self.key, self.current, self.desired = target, key, current, desired

    def as_dict(self):
        return {"target": self.target, "key": self.key,
                "current": None if self.current is MISSING else self.current, "desired": self.desired,
                "missing": self.current is MISSING}


def _flatten(d: dict, prefix=()):
    for k, v in d.items():
        if isinstance(v, dict) and v:
            yield from _flatten(v, prefix + (k,))
        else:
            yield prefix + (k,), v


def _get(d, path):
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return MISSING
        cur = cur[p]
    return cur


def _key(path) -> str:
    return ".".join(path)


# --------------------------------------------------------------------------- desired state

def claude_desired():
    from .setup import load_conf
    frag = load_conf("claude/settings.json")
    plugins = load_conf("claude/plugins.json")
    flat = {path: v for path, v in _flatten({k: v for k, v in frag.items() if k != "hooks"})}
    on, off = _effective_plugins(plugins)
    for p in on:
        flat[("enabledPlugins", p)] = True
    for p in off:
        flat[("enabledPlugins", p)] = False
    hooks = {event: [h["command"] for g in groups for h in g["hooks"]] for event, groups in frag["hooks"].items()}
    return flat, hooks


def _effective_plugins(plugins: dict) -> tuple:
    """(enabled, disabled) Claude plugin keys after devkit's team default and the developer's `ext` overlay (spec
    P-fym583l7sg1 R8): repair and the lock never undo a plugin the developer turned off with `dev ext disable`."""
    from . import ext
    t = ext.team()
    t["claude"] = plugins
    on, off = ext.effective_claude_plugins(t, ext.user_layer())
    return sorted(on), sorted(off)


def codex_desired():
    from .setup import load_conf
    conf = load_conf("codex/config.json")
    plugins = load_conf("codex/plugins.json")
    flat = {(k,): v for k, v in conf["settings"].items()}
    for path, v in _flatten(conf.get("upsert", {})):
        flat[path] = v
    from . import ext
    t = ext.team()
    t["codex"] = plugins
    for name in ext.effective_mcp("codex", t):  # the team's and the overlay's (spec P-fym583l7sg1 R8)
        flat[("mcp_servers", name)] = "present"
    flat[("projects", str(core.DEV), "trust_level")] = "trusted"
    return flat


# --------------------------------------------------------------------------- diff

def diff_claude(settings=None):
    s = settings if settings is not None else (read_json(CLAUDE_HOME / "settings.json", {}) or {})
    flat, hooks = claude_desired()
    out = [Drift("claude", _key(p), _get(s, p), v) for p, v in flat.items() if _get(s, p) != v]
    cur_hooks = s.get("hooks") or {}
    for event, cmds in hooks.items():
        have = [h.get("command") for g in cur_hooks.get(event, []) for h in g.get("hooks", [])]
        for c in cmds:
            if c not in have:
                out.append(Drift("claude", f"hooks.{event}", MISSING, c))
    return out


def diff_codex(rpc=None):
    from .codexrpc import CodexError, CodexRPC
    own = rpc is None
    try:
        rpc = rpc or CodexRPC(timeout=20)
        cfg = rpc.read().get("config", {}) or {}
        hooks = [h for h in rpc.hooks(core.HOME) if str(h.get("sourcePath")) == str(CODEX_HOME / "hooks.json")]
    except CodexError as e:
        return [Drift("codex", "app-server", str(e), "reachable")]
    finally:
        if own and rpc:
            rpc.close()
    out = []
    for p, v in codex_desired().items():
        cur = _get(cfg, p)
        if v == "present":
            if cur is MISSING or cur is None:
                out.append(Drift("codex", _key(p), MISSING, "configured"))
        elif cur != v:
            out.append(Drift("codex", _key(p), cur, v))
    from .setup import load_conf
    want_events = set(load_conf("codex/hooks.json")["hooks"])
    have = {h.get("eventName", "").lower(): h.get("trustStatus") for h in hooks}
    for ev in want_events:
        k = ev[0].lower() + ev[1:]
        st = have.get(k.lower()) or have.get(k)
        if st != "trusted":
            out.append(Drift("codex", f"hooks.{ev}", st or MISSING, "trusted"))
    return out


def diff_instructions():
    """The generated rules files that differ from what devkit writes now, for the agents turned on in `dev setup`: a
    turned-off agent gets none of devkit's rules, so its file is no drift."""
    from .setup import render_instructions
    want = render_instructions()
    agents = core.CFG.get("agents") or {}
    out = []
    for agent, f in (("claude", CLAUDE_HOME / "CLAUDE.md"), ("codex", CODEX_HOME / "AGENTS.md")):
        if not agents.get(agent, True):
            continue
        cur = f.read_text() if f.exists() else MISSING
        if cur != want:
            out.append(Drift("instructions", tilde(f), "edited" if cur is not MISSING else MISSING, "devkit-generated"))
    return out


def diff_skills():
    managed = read_json(STATE / "skills-managed.json", []) or []
    if not isinstance(managed, dict):  # before 26.11.0: every skill went to both agents
        managed = {n: ["claude", "codex"] for n in managed}
    out = []
    for agent, home in (("claude", CLAUDE_HOME / "skills"), ("codex", AGENTS_HOME / "skills")):
        for n, agents in managed.items():
            if agent in agents and not (home / n / "SKILL.md").exists():
                out.append(Drift("skills", f"{tilde(home)}/{n}", MISSING, "linked"))
    return out


def diff_git():
    u = core.gh_user()
    if not u.get("login"):
        return []
    name = u.get("name") or u["login"]
    email = u.get("email") or f"{u['id']}+{u['login']}@users.noreply.github.com"
    out = []
    for key, want in (("user.name", name), ("user.email", email),
                      ("core.excludesFile", str(core.runtime_home() / "config" / "git" / "ignore"))):
        cur = core.sh(["git", "config", "--global", key])[1]
        if cur != want:
            out.append(Drift("git", key, cur or MISSING, want))
    return out


def project_overrides(root=None) -> list:
    """Project-level agent settings under ~/Development that switch devkit off: [(file, what)]."""
    from . import guard
    root = Path(root or core.DEV)
    out = []
    for pattern in (".claude/settings.json", ".claude/settings.local.json", ".codex/config.toml"):
        for depth in ("*/*/", "*/*/*/", "_local/*/", ".worktrees/*/*/*/"):
            for f in root.glob(depth + pattern):
                try:
                    text = f.read_text(errors="ignore")
                except OSError:
                    continue
                for rx, what in guard.DEVKIT_OFF:
                    if rx.search(text):
                        out.append((str(f), what))
    return out


def diff(include_codex=True):
    agents = core.CFG.get("agents", {})
    out = (diff_claude() if agents.get("claude", True) else []) + diff_instructions() + diff_skills() + diff_git()
    if include_codex and agents.get("codex", True) and core.have("codex"):
        out += diff_codex()
    return out


# --------------------------------------------------------------------------- repair

def _log(msg: str):
    from . import audit
    audit.record("config", msg[:80], msg)
    d = STATE / "logs"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "config.log").open("a") as f:
        f.write(f"{core.fmt_ts(time.time())} {msg}\n")


def _older_refuses(what: str, quiet: bool = False) -> bool:
    """True when this copy is older than the release `dev install` pinned (core.older_copy_skip records it): it then
    writes no devkit-managed setting, and says so unless quiet."""
    import sys
    old = core.older_copy_skip(what)
    if old and not quiet:
        print(f"devkit {old['running']} runs here, older than devkit {old['pinned']} that `dev install` pinned, so it "
              f"changes no devkit-managed setting. Run the installed `dev` ({tilde(core.dev_link())}) instead.",
              file=sys.stderr)
    return bool(old)


def repair(quiet=False, include_codex=True) -> list:
    """Restore every drifted managed key. Returns the list of Drift that was repaired. A copy older than the pinned
    release repairs nothing: what it would restore are its own, older values. The pinned release first retires the
    other pinned copies (core.retire_other_pins), so a session that still runs one runs the pinned release instead."""
    from . import setup
    if _older_refuses("dev config repair", quiet):
        return []
    if core.is_current_pin():
        try:
            core.retire_other_pins()
        except OSError as e:  # the repair goes on; dev doctor names the copies still running their own code
            from . import audit
            audit.record("install.retire", tilde(core.pin_root()), f"could not retire the other copies: {e}", "failed")
    drifts = diff(include_codex=include_codex)
    if not drifts:
        return []
    targets = {d.target for d in drifts}
    if "claude" in targets:
        setup.write_claude_settings()
    if "instructions" in targets:
        setup.write_agent_instructions(quiet=True)
    if "skills" in targets:
        setup.install_skills(quiet=True)
    if "git" in targets:
        setup.setup_git(quiet=True)
    if "codex" in targets:
        setup.write_codex_config(quiet=True)
    for d in drifts:
        cur = "missing" if d.current is MISSING else json.dumps(d.current, ensure_ascii=False)
        _log(f"repaired {d.target} {d.key}: {cur} -> {json.dumps(d.desired, ensure_ascii=False)}")
    try:
        from . import gc
        gc._log(f"config · restored {len(drifts)} devkit-managed setting(s): "
                + ", ".join(sorted({f'{d.target}:{d.key}' for d in drifts}))[:300])
    except Exception:
        pass
    return drifts


# --------------------------------------------------------------------------- lock (Claude managed settings)

def managed_payload() -> dict:
    from .setup import load_conf
    frag = load_conf("claude/settings.json")
    plugins = load_conf("claude/plugins.json")
    # fastModePerSessionOptIn stays a user setting: once managed, Claude Code refuses `/fast on` outside a terminal.
    payload = {k: v for k, v in frag.items() if k not in ("fastModePerSessionOptIn",)}
    on, off = _effective_plugins(plugins)
    payload["enabledPlugins"] = {**{p: True for p in on}, **{p: False for p in off}}
    payload["_devkit"] = {"version": core.VERSION, "source": str(core.DEVKIT_HOME),
                          "note": "Managed by devkit. Change devkit's configuration, then `dev config lock` again."}
    return payload


def lock_status() -> str:
    if not MANAGED_FILE.exists():
        return "off"
    cur = read_json(MANAGED_FILE, {}) or {}
    want = managed_payload()
    cur.pop("_devkit", None)
    want.pop("_devkit", None)
    return "on" if cur == want else "stale"


def lock() -> int:
    """Install the managed-settings drop-in (asks for the admin password via sudo)."""
    if _older_refuses("dev config lock"):
        return 1
    payload = json.dumps(managed_payload(), indent=2, ensure_ascii=False) + "\n"
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        tf.write(payload)
        tmp = tf.name
    rc = subprocess.call(["sudo", "/bin/sh", "-c",
                          f'mkdir -p "{MANAGED_DIR}" && install -m 0644 -o root -g wheel "{tmp}" "{MANAGED_FILE}"'])
    os.unlink(tmp)
    if rc == 0:
        from . import setup
        setup.write_claude_settings(include_hooks=False)  # hooks now come from the managed tier only
        _log("lock on: managed-settings drop-in installed")
    return rc


def unlock() -> int:
    if _older_refuses("dev config unlock"):
        return 1
    rc = subprocess.call(["sudo", "rm", "-f", str(MANAGED_FILE)])
    if rc == 0:
        from . import setup
        setup.write_claude_settings()
        _log("lock off: managed-settings drop-in removed")
    return rc
