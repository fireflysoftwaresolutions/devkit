# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""`dev setup`: the installation and configuration wizard.

Detects the machine and the agents, asks only what matters (with good defaults),
shows the plan, then installs the toolchain for the chosen stacks and applies the
configuration (`dev install`), and finishes with `dev doctor`. `--yes` accepts
every default (non-interactive installs); re-running it edits an existing setup.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import core, ui
from .core import CONFIG_FILE, DEVKIT_HOME, HOME, STATE

STACKS = {"java": "Java / Spring (JDK, Maven, Gradle, jdtls)", "python": "Python (uv, pyright)",
          "php": "PHP (php, Composer, intelephense)", "node": "Node / TypeScript / Angular / React (pnpm, tsc, ng)"}
BASE_GROUPS = ("core", "docker", "agents", "docs", "security")
CLAUDE_MODES = {"bypassPermissions": "no prompts; the devkit guard protects the workspace (recommended)",
                "acceptEdits": "edits without prompts, commands ask", "default": "ask before every tool"}
CODEX_MODES = {"full": ("never", "danger-full-access", "no prompts, full access; the devkit guard protects (recommended)"),
               "workspace": ("on-request", "workspace-write", "sandboxed to the project; asks when it needs more")}


class Answers(dict):
    pass


def _hw():
    def sysctl(k):
        return core.sh(["sysctl", "-n", k])[1]
    try:
        cpus = int(sysctl("hw.ncpu"))
        ram = int(sysctl("hw.memsize")) // (1024 ** 3)
    except ValueError:
        cpus, ram = 8, 16
    return cpus, ram, core.sh(["sw_vers", "-productVersion"])[1], core.sh(["uname", "-m"])[1]


class Prompter:
    def __init__(self, assume_yes: bool):
        self.yes = assume_yes or not sys.stdin.isatty()
        self.n = 0

    def section(self, title: str):
        self.n += 1
        print(f"\n  {ui.bold(ui.cyan(f'{self.n:>2}'))} {ui.bold(title)}")

    def ask(self, q: str, default: str) -> str:
        if self.yes:
            print(f"     {q} {ui.dim('→')} {default}")
            return default
        a = input(f"     {q} {ui.dim('[' + str(default) + ']')} ").strip()
        return a or default

    def yesno(self, q: str, default: bool) -> bool:
        d = "Y/n" if default else "y/N"
        if self.yes:
            print(f"     {q} {ui.dim('→')} {'yes' if default else 'no'}")
            return default
        a = input(f"     {q} {ui.dim('[' + d + ']')} ").strip().lower()
        return default if not a else a in ("y", "yes", "s", "si", "sí")

    def choose(self, q: str, options: dict, default: str) -> str:
        keys = list(options)
        for i, k in enumerate(keys, 1):
            mark = ui.cyan("›") if k == default else " "
            print(f"     {mark} {i}. {ui.bold(k)} {ui.dim('— ' + str(options[k]))}")
        if self.yes:
            print(f"     {q} {ui.dim('→')} {default}")
            return default
        while True:
            a = input(f"     {q} {ui.dim('[' + str(keys.index(default) + 1) + ']')} ").strip()
            if not a:
                return default
            if a.isdigit() and 1 <= int(a) <= len(keys):
                return keys[int(a) - 1]
            if a in options:
                return a

    def multi(self, q: str, options: dict, defaults: list) -> list:
        keys = list(options)
        for i, k in enumerate(keys, 1):
            print(f"     {ui.green('■') if k in defaults else ui.dim('□')} {i}. {ui.bold(k)} {ui.dim('— ' + options[k])}")
        if self.yes:
            print(f"     {q} {ui.dim('→')} {', '.join(defaults)}")
            return defaults
        a = input(f"     {q} {ui.dim('[' + ','.join(str(keys.index(d) + 1) for d in defaults) + ']')} ").strip()
        if not a:
            return defaults
        picked = [keys[int(x) - 1] for x in a.replace(" ", "").split(",") if x.isdigit() and 1 <= int(x) <= len(keys)]
        return picked or defaults


def detect() -> dict:
    from . import providers
    cpus, ram, macos, arch = _hw()
    gh = core.gh_user() if core.have("gh") else {}
    try:
        prov = providers.status(include_account=False)
    except Exception:
        prov = {"claude": {"installed": core.have("claude")}, "codex": {"installed": core.have("codex")}}
    return {"cpus": cpus, "ram": ram, "macos": macos, "arch": arch, "brew": core.have("brew"), "gh": gh,
            "providers": prov, "config": core.read_json(CONFIG_FILE, None)}


def run(assume_yes=False, skip_packages=False) -> int:
    print(ui.banner("setup"))
    name = core.gh_user().get("name", "")
    print(f"  {ui.bold(ui.greeting(name))} This wizard sets up devkit on this Mac. Press Enter to keep the value in "
          f"[brackets]; nothing changes until you confirm.")
    d = detect()
    p = Prompter(assume_yes)
    cfg = core.deep_merge(core.DEFAULTS, d["config"] or {})

    p.section("This machine")
    pv = d["providers"]
    print(f"     macOS {d['macos']} · {d['arch']} · {d['cpus']} CPU · {d['ram']} GB RAM · Homebrew "
          f"{'yes' if d['brew'] else 'no'}")
    print(f"     GitHub: {ui.green(d['gh'].get('login')) if d['gh'].get('login') else ui.red('not signed in')}")
    for k in ("claude", "codex"):
        x = pv.get(k) or {}
        state = (ui.green("signed in") if x.get("logged_in") else ui.yellow("installed, not signed in")) \
            if x.get("installed") else ui.dim("not installed")
        print(f"     {('Claude Code' if k == 'claude' else 'Codex'):<12} {state}"
              + (f" · {x.get('version')}" if x.get("version") else "") + (f" · plan {x.get('plan')}" if x.get("plan") else ""))
    if not d["gh"].get("login"):
        print(f"\n  {ui.FAIL} Sign in to GitHub first: gh auth login -s repo,read:org,workflow,gist")
        return 2

    p.section("Workspace")
    root = p.ask("Development root", cfg["dev_root"])
    login = d["gh"]["login"]
    handoff = p.ask("Private handoff repository", cfg.get("handoff_repo") or f"{login}/handoff")

    p.section("Agents")
    use_claude = p.yesno("Configure Claude Code (CLI + Desktop)?", True)
    use_codex = p.yesno("Configure Codex (CLI + ChatGPT app)?", True)
    claude_mode = cfg.get("claude", {}).get("default_mode", "bypassPermissions")
    if use_claude:
        claude_mode = p.choose("Claude Code permission mode", CLAUDE_MODES, claude_mode)
    codex_mode = cfg.get("codex", {}).get("mode", "full")
    if use_codex:
        codex_mode = p.choose("Codex autonomy", {k: v[2] for k, v in CODEX_MODES.items()}, codex_mode)

    p.section("Stacks (toolchains, language servers, skills)")
    stacks = p.multi("Stacks you develop in (comma-separated numbers)", STACKS, cfg.get("stacks") or list(STACKS))

    from . import toolchain
    seen = toolchain.detect()
    p.section("Git hosts and CI/CD (CLIs, sign-in checks, `dev ci status`)")
    if seen["evidence"]:
        print("     " + ui.dim("found on this Mac: " + "; ".join(f"{k} ({v})" for k, v in seen["evidence"].items())))
    hosts = p.multi("Where your repositories and pipelines live (comma-separated numbers)",
                    {k: v[0] for k, v in toolchain.HOSTS.items()},
                    list(dict.fromkeys((cfg.get("hosts") or []) + seen["hosts"])) or ["github"])
    hosts = list(dict.fromkeys(["github"] + hosts))
    p.section("Cloud and infrastructure (CLIs and sign-in checks)")
    clouds = p.multi("Clouds and infrastructure tools you use (numbers, or empty for none)",
                     {k: v[0] for k, v in toolchain.CLOUDS.items()},
                     list(dict.fromkeys((cfg.get("clouds") or []) + seen["clouds"])))

    p.section("Docker on Colima")
    dk = cfg["docker"]
    cpu = int(p.ask("VM CPUs", str(dk.get("cpu") or max(2, min(8, d["cpus"] // 3)))))
    mem = int(p.ask("VM memory (GB)", str(dk.get("memory") or max(4, min(16, d["ram"] // 5)))))
    disk = int(p.ask("VM disk, grows as needed (GB)", str(dk.get("disk") or 80)))
    autostart = p.yesno("Start Colima at login?", bool(dk.get("autostart")))

    p.section("Automation")
    gc_auto = p.yesno("Reclaim disk automatically (merged worktrees, idle build output, stale temp)?", cfg["gc"]["auto"])
    interval = int(p.ask("Background sync interval (minutes)", str(cfg["sync"]["interval_minutes"])))
    lock = False
    if use_claude:
        lock = p.yesno("Lock Claude Code settings with a managed-settings file (needs your admin password)?",
                       bool(cfg.get("lock")))

    new = core.deep_merge(d["config"] or {}, {
        "dev_root": root, "github_user": login, "handoff_repo": handoff,
        "agents": {"claude": use_claude, "codex": use_codex},
        "claude": {"default_mode": claude_mode},
        "codex": {"mode": codex_mode, "approval_policy": CODEX_MODES[codex_mode][0],
                  "sandbox_mode": CODEX_MODES[codex_mode][1]},
        "stacks": stacks, "lock": lock, "hosts": hosts, "clouds": clouds,
        "docker": {"cpu": cpu, "memory": mem, "disk": disk, "autostart": autostart},
        "gc": {"auto": gc_auto}, "sync": {"interval_minutes": interval},
    })

    p.section("Plan")
    rows = [("config", str(core.tilde(CONFIG_FILE))), ("root", root), ("handoff", f"{handoff} → {root}/.handoff"),
            ("Claude Code", f"{claude_mode}" if use_claude else "skip"),
            ("Codex", f"{codex_mode} ({CODEX_MODES[codex_mode][0]} / {CODEX_MODES[codex_mode][1]})" if use_codex else "skip"),
            ("stacks", ", ".join(stacks)), ("git hosts", ", ".join(hosts)), ("clouds", ", ".join(clouds) or "none"),
            ("packages", "skip" if skip_packages else "Homebrew + npm for the stacks, hosts and clouds"),
            ("Docker VM", f"{cpu} CPU · {mem} GB · {disk} GB" + (" · at login" if autostart else "")),
            ("automation", f"sync every {interval} min · safe cleanup {'on' if gc_auto else 'report only'}"),
            ("lock", "managed settings (sudo)" if lock else "off (drift is still blocked and repaired)")]
    for k, v in rows:
        print(f"     {ui.dim(k.ljust(12))} {v}")
    if d["config"] and d["config"] != new:
        changed = sorted(k for k in set(new) | set(d["config"]) if new.get(k) != d["config"].get(k))
        print(f"     {ui.dim('changes'.ljust(12))} {', '.join(changed)}")
    if not p.yesno("Apply this plan?", True):
        print(f"  {ui.WARN} nothing changed")
        return 1

    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n")
    print(f"\n  {ui.OK} saved {core.tilde(CONFIG_FILE)}")
    if not skip_packages:
        rc = install_packages(stacks, use_claude, extra=toolchain.groups({"hosts": hosts, "clouds": clouds}))
        if rc != 0:
            print(f"  {ui.WARN} some packages failed; continuing with the configuration")
    py = sys.executable
    subprocess.call([py, str(DEVKIT_HOME / "bin" / "dev"), "install"])
    _post_install(p, use_claude, use_codex)
    if lock:
        subprocess.call([py, str(DEVKIT_HOME / "bin" / "dev"), "config", "lock"])
    print(f"\n  {ui.ARROW} Verifying …")
    rc = subprocess.call([py, str(DEVKIT_HOME / "bin" / "dev"), "doctor"])
    print(f"\n  {ui.bold('Next:')} open a new terminal (or `exec zsh`), then `dev` — and in any project, start "
          f"Claude Code or Codex as usual. `dev agents test` runs a live check of both agents.\n")
    return rc


def _post_install(p, use_claude, use_codex):
    """Offer the interactive steps a script cannot do alone: sign-ins and starting Docker."""
    from . import providers
    st = providers.status(include_account=False)
    if use_claude and st["claude"].get("installed") and not st["claude"].get("logged_in"):
        if p.yesno("Claude Code is not signed in. Sign in now (opens the browser)?", True) and not p.yes:
            subprocess.call(["claude", "auth", "login"])
    if use_codex and st["codex"].get("installed") and not st["codex"].get("logged_in"):
        if p.yesno("Codex is not signed in. Sign in now (opens the browser)?", True) and not p.yes:
            subprocess.call(["codex", "login"])
    if core.have("colima") and core.sh(["colima", "status"], timeout=20)[0] != 0:
        if p.yesno("Start Docker (Colima VM) now?", not p.yes):
            subprocess.call([sys.executable, str(DEVKIT_HOME / "bin" / "dev"), "docker", "up"])


# --------------------------------------------------------------------------- packages

def filtered(path: Path, groups) -> list:
    """Lines of a Brewfile / npm list that belong to the selected `# @group` sections."""
    out, cur = [], "core"
    for ln in path.read_text().splitlines():
        s = ln.strip()
        if s.startswith("# @"):
            cur = s[3:].split()[0]
            continue
        if not s or s.startswith("#"):
            continue
        if cur in groups:
            out.append(s)
    return out


def missing_packages(groups) -> list:
    """Brewfile entries of these groups that are not installed (fast: `brew bundle check`)."""
    brew = filtered(DEVKIT_HOME / "Brewfile", groups)
    with tempfile.NamedTemporaryFile("w", suffix=".Brewfile", delete=False) as tf:
        tf.write("\n".join(brew) + "\n")
        bf = tf.name
    rc, out, err = core.sh(["brew", "bundle", "check", f"--file={bf}", "--verbose", "--no-upgrade"], timeout=300)
    os.unlink(bf)
    if rc == 0:
        return []
    return [ln.split("→")[-1].strip() for ln in (out + "\n" + err).splitlines()
            if "needs to be installed" in ln or "is not installed" in ln] or ["(some packages)"]


def install_packages(stacks, with_claude=True, extra=()) -> int:
    groups = set(BASE_GROUPS) | set(stacks) | set(extra or ())
    print(f"\n  {ui.ARROW} Homebrew packages for: {', '.join(sorted(groups))}")
    brew = filtered(DEVKIT_HOME / "Brewfile", groups)
    with tempfile.NamedTemporaryFile("w", suffix=".Brewfile", delete=False) as tf:
        tf.write("\n".join(brew) + "\n")
        bf = tf.name
    rc = subprocess.call(["brew", "bundle", f"--file={bf}", "--no-upgrade"])
    os.unlink(bf)
    npm = [x for x in filtered(DEVKIT_HOME / "config" / "npm-globals.txt", groups)]
    if npm:
        print(f"\n  {ui.ARROW} npm globals: {', '.join(npm)}")
        rc |= subprocess.call(["npm", "install", "-g", "--no-fund", "--no-audit", *npm])
        if "@playwright/cli" in npm and core.have("playwright-cli"):
            subprocess.call(["playwright-cli", "install", "--skills"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    from . import toolchain
    for line in toolchain.postinstall([k for k, v in toolchain.ALL.items() if v[4] in groups]):
        print(f"  {ui.OK} {line}")
    if with_claude and not core.have("claude"):
        print(f"\n  {ui.ARROW} Claude Code (native installer)")
        rc |= subprocess.call(["/bin/bash", "-c", "curl -fsSL https://claude.ai/install.sh | bash"])
    return rc
