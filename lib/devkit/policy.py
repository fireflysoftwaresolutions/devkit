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
import re
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
    """(enabled, disabled) Claude plugin keys after devkit's team default and the developer's `ext` overlay:
    repair and the lock never undo a plugin the developer turned off with `dev ext disable`."""
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
    for name in ext.effective_mcp("codex", t):  # the team's and the overlay's
        flat[("mcp_servers", name)] = "present"
    flat[("projects", str(core.DEV), "trust_level")] = "trusted"
    return flat


# --------------------------------------------------------------------------- diff

def claude_hooks() -> dict:
    """devkit's Claude Code hooks as `dev install` writes them (setup.merge_hooks): {event: {command: (matcher, hook)}},
    the hook with its type, command and timeout."""
    from .setup import load_conf
    return {event: {h["command"]: (g.get("matcher"), h) for g in groups for h in g["hooks"]}
            for event, groups in load_conf("claude/settings.json")["hooks"].items()}


def _same_matcher(a, b) -> bool:
    """Do two hook matchers match the same? Claude Code reads no matcher, "" and "*" all as every one."""
    return a == b or (a in (None, "", "*") and b in (None, "", "*"))


def diff_claude(settings=None):
    """The user settings' devkit-managed keys that differ from devkit's. A devkit hook counts only as devkit writes it
    (claude_hooks): in a group of its event with its matcher, with the same type, timeout and no other field. Any
    other matcher, type or timeout, longer or shorter, is drift, and so is any other field, "async": false included:
    under a matcher that never matches, as another type, cut short or run in the background it would stop nothing.
    So is any disableAllHooks but false (hooks_off), which turns every hook off."""
    s = settings if settings is not None else (read_json(CLAUDE_HOME / "settings.json", {}) or {})
    flat, hooks = claude_desired()
    out = [Drift("claude", _key(p), _get(s, p), v) for p, v in flat.items() if _get(s, p) != v]
    off = s.get("disableAllHooks", False)
    if off is not False:
        out.append(Drift("claude", "disableAllHooks", off, False))
    cur_hooks = s.get("hooks") or {}
    written = claude_hooks()
    for event, cmds in hooks.items():
        groups = cur_hooks.get(event, [])
        have = [(g.get("matcher"), h) for g in groups for h in g.get("hooks", [])]
        for c in cmds:
            matcher, want = written.get(event, {}).get(c) or (None, {"type": "command", "command": c})
            if any(_same_matcher(m, matcher) and h == want for m, h in have):
                continue
            found = [dict(h, matcher=m) for m, h in have if isinstance(h, dict) and h.get("command") == c]
            out.append(Drift("claude", f"hooks.{event}", found or MISSING, c))
    return out


def diff_codex(rpc=None):
    """Codex's managed config keys that differ, and each event whose devkit hooks are not all listed and trusted. A
    hook counts as devkit's only when it is declared exactly as config/codex/hooks.json renders it (the identity
    codexrpc.split_hooks matches), so another hook on the same event never decides devkit's row."""
    from .codexrpc import CodexError, CodexRPC, declared_hooks, hook_id, split_hooks
    own = rpc is None
    try:
        rpc = rpc or CodexRPC(timeout=20)
        cfg = rpc.read().get("config", {}) or {}
        listed = rpc.hooks(core.HOME)
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
    ours = load_conf("codex/hooks.json")["hooks"]
    mine, _ = split_hooks(listed, CODEX_HOME / "hooks.json", ours)
    status = {hook_id(h): h.get("trustStatus") for h in mine}
    for ev, groups in ours.items():
        bad = [status.get(i) or MISSING for i in sorted(declared_hooks({ev: groups}), key=repr)
               if status.get(i) != "trusted"]
        if bad:
            out.append(Drift("codex", f"hooks.{ev}", bad[0], "trusted"))
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


# --------------------------------------------------------------------------- settings that switch devkit off

# what a settings file says that turns devkit's hooks or plugin off: (pattern, what it does)
DEVKIT_OFF = [
    (re.compile(r'"disableAllHooks"\s*:\s*true'),
     "disables every hook, including devkit's context, crew gates and canary"),
    (re.compile(r'"devkit@firefly-devkit"\s*:\s*false'), "disables the devkit plugin"),
    # TOML spells the key bare, quoted, dotted (features.hooks) or in an inline table ({ hooks = false })
    (re.compile(r'(?:^|[\n{,])[ \t]*(?:[\w"\'-]+[ \t]*\.[ \t]*)*["\']?(?:codex_)?hooks["\']?[ \t]*=[ \t]*false\b'),
     "turns Codex hooks off"),
]

SHOWN = 80  # how much of a value from a settings file a refusal or a doctor row prints

# the string part is bounded: a longer one, or a value cut short, still reads as something other than false
_HOOKS_OFF_JSON = re.compile(r'"disableAllHooks"\s*:\s*("(?:[^"\\]|\\.){0,%d}"|[^,}\s]{1,%d})' % (SHOWN, SHOWN))


def shown(value: str, n: int = SHOWN) -> str:
    """A value from a settings file as a refusal or a doctor row prints it: at most n characters, cut with an ellipsis."""
    value = str(value)
    return value if len(value) <= n else value[:n - 1] + "…"


def hooks_off(text: str):
    """What a Claude settings file's text sets disableAllHooks to, as JSON text cut to SHOWN characters, when it is
    anything but false, which turns every hook off, devkit's included; None when it is false or not set. Text Python's
    parser refuses (cut short, nested deeper than core.JSON_MAX_DEPTH, an integer of more digits than it converts) is
    read for the key, \\u escapes undone, and fails closed: Claude Code (Node's JSON.parse) may still read the file, and
    the text cannot tell the top level from a nested object or a string, so any value but false, wherever it stands,
    reads as hooks off. A false there (nested, or inside a string) cannot hide a top-level true; a false alarm is
    possible only for a file Python cannot parse."""
    try:
        data = core.load_json(text or "")
    except ValueError:
        return next((shown(v) for v in _HOOKS_OFF_JSON.findall(_unescape(text)) if v != "false"), None)
    if not isinstance(data, dict):
        return None
    off = data.get("disableAllHooks", False)
    return None if off is False else shown(json.dumps(off))


# the DEVKIT_* keys a settings or shell startup file sets; DEV_ROOT counts with them: the handoff (policies, plans,
# tasks) lives below it
_KEY = r"(DEVKIT_[A-Za-z0-9_]*|DEV_ROOT)"
_KEY_JSON = re.compile(r'"' + _KEY + r'"\s*:\s*("(?:[^"\\]|\\.)*"|[^,}\s]+)')
_KEY_TOML = re.compile(r'(?:^|[\s{,])["\']?' + _KEY + r'["\']?\s*=\s*("[^"\n]*"|\'[^\'\n]*\'|[^\s,}]+)', re.M)
_KEY_SHELL = re.compile(r"(?:^|[;&|\s])(?:export\s+|declare\s+-\w+\s+|typeset\s+-\w+\s+|readonly\s+)?" + _KEY +
                        r"=(\S*)|(?:^|[;&|\s])(?:setenv|set\s+-\w+|launchctl\s+setenv)\s+" + _KEY + r"\s+(\S*)", re.M)


def _is_key(name) -> bool:
    return str(name).startswith("DEVKIT_") or str(name) == "DEV_ROOT"


def _unescape(text) -> str:
    # "DEVKIT_X" is the same JSON key as "DEVKIT_X"
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), str(text or ""))


def devkit_env(text: str, toml: bool = False, shell: bool = False) -> dict:
    """The DEVKIT_* variables (and DEV_ROOT) a settings or shell startup file's text sets, {name: value}."""
    if not text:
        return {}
    if shell:
        body = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        return {(m.group(1) or m.group(3)): (m.group(2) if m.group(1) else m.group(4)) or ""
                for m in _KEY_SHELL.finditer(body)}
    if not toml:
        try:
            data = core.load_json(text)
        except ValueError:  # not JSON (comments, cut short) or nested too deep: read the keys from the text
            data = None
        if isinstance(data, dict):
            env = data.get("env")
            return {k: json.dumps(v) for k, v in env.items() if _is_key(k)} if isinstance(env, dict) else {}
    out = {}
    for m in (_KEY_TOML if toml else _KEY_JSON).finditer(_unescape(text)):
        v = m.group(2)
        try:
            v = json.dumps(core.load_json(v))
        except ValueError:
            pass
        out[m.group(1)] = v
    return out


def project_overrides(root=None) -> list:
    """Project-level agent settings under ~/Development that switch devkit off: [(file, what)]."""
    root = Path(root or core.DEV)
    out = []
    for pattern in (".claude/settings.json", ".claude/settings.local.json", ".codex/config.toml"):
        for depth in ("*/*/", "*/*/*/", "_local/*/", ".worktrees/*/*/*/"):
            for f in root.glob(depth + pattern):
                try:
                    text = f.read_text(errors="ignore")
                except OSError:
                    continue
                for rx, what in DEVKIT_OFF:
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
