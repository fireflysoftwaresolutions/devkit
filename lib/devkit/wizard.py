# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev setup`: the installation and configuration wizard.

Detects the machine and the agents, asks only what matters (with good defaults), shows the plan, then installs
the toolchain for the chosen stacks and applies the configuration (`dev install`), and finishes with `dev doctor`.

In a terminal it is interactive (see tui.py): a welcome that shows what this Mac already has, then "Recommended
setup" (straight to the review) or "Customize" (one step per section), a review with "Change a section", and the
apply steps with a spinner and ✓/✗ each. Re-running it starts from the saved config and shows what would change.
When stdin or stdout is not a terminal, with TERM=dumb, or with `--yes`, it uses the plain line prompts; `--yes`
accepts every default (non-interactive installs).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from . import core, tui, ui
from .core import CONFIG_FILE, DEVKIT_HOME, STATE

STACKS = {"java": "Java / Spring (JDK, Maven, Gradle, jdtls)", "python": "Python (uv, pyright)",
          "php": "PHP (php, Composer, intelephense)", "node": "Node / TypeScript / Angular / React (pnpm, tsc, ng)"}
BASE_GROUPS = ("core", "docker", "agents", "docs", "security")
# A first run on a Mac with no repositories preselects these, and the wizard always asks (the developer's choice,
# 2026-11-06): a light, fast first install; Java and PHP are one answer away.
FIRST_STACKS = ("python", "node")
CLAUDE_MODES = {"bypassPermissions": "no prompts; the devkit guard protects the workspace (recommended)",
                "acceptEdits": "edits without prompts, commands ask", "default": "ask before every tool"}
CODEX_MODES = {"full": ("never", "danger-full-access", "no prompts, full access; the devkit guard protects (recommended)"),
               "workspace": ("on-request", "workspace-write", "sandboxed to the project; asks when it needs more")}
CLAUDE_INSTALLER = "curl -fsSL https://claude.ai/install.sh | bash"  # Claude Code's official native installer
# HTTPS with the browser: no questions about protocols or tokens, and git signs in through gh.
GH_LOGIN = ["gh", "auth", "login", "-h", "github.com", "-p", "https", "-w", "-s", "repo,read:org,workflow,gist"]


class Answers(dict):
    pass


def _commit_template_default() -> bool:
    """The commit template question's default: the recorded answer, yes before the first one (spec P-fy3bwa0pshk R9)."""
    from . import setup as setup_mod
    return setup_mod.commit_template_answer() != "no"


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
    """The interactive wizard in a terminal; the line prompts otherwise (and always with --yes)."""
    if not assume_yes and tui.available():
        return _Wizard(tui.Terminal(), skip_packages).run()
    return _run_lines(assume_yes, skip_packages)


# --------------------------------------------------------------------------- shared by both front ends

def _setup_cfg(saved) -> dict:
    """devkit's defaults with the saved config on top: what the wizard reads, never the running config."""
    return core.deep_merge(core.DEFAULTS, saved or {})


def offered_groups(clouds, saved) -> list:
    """The optional skill groups (`skills.optional`) the wizard offers for these tools: ["iac"] for terraform."""
    return core.offered_skill_groups(clouds, _setup_cfg(saved))


def saved_group_answers(saved) -> dict:
    """{group: on} for each optional group the saved setup was offered (its tools were picked and skills.groups was
    saved): a rerun keeps a decline. Groups never offered have no answer, and their offer starts at yes."""
    groups = ((saved or {}).get("skills") or {}).get("groups")
    if groups is None:
        return {}
    return {g: g in groups for g in offered_groups((saved or {}).get("clouds") or [], saved)}


def accepted_groups(a, saved) -> list:
    """The optional groups offered for the answers' tools that the developer accepted (yes unless answered no)."""
    return [g for g in offered_groups(a["clouds"], saved) if (a.get("skill_groups") or {}).get(g, True)]


def skill_question(group: str, clouds, saved) -> str:
    """The offer's question, naming the tools and the group's skills from config/skills.json."""
    when = ((_setup_cfg(saved).get("skills") or {}).get("optional") or {}).get(group) or []
    try:
        sources = json.loads((DEVKIT_HOME / "config" / "skills.json").read_text()).get("sources") or []
    except (OSError, ValueError):
        sources = []
    names = [n for s in sources if isinstance(s, dict) and s.get("group") == group for n in s.get("skills") or {}]
    tools = ", ".join(_label(k) for k in clouds if k in when) or group
    return f"Add the agent skills for {tools}" + (f" ({', '.join(names)})" if names else "") + "?"


def _skill_groups(saved, a):
    """skills.groups to save: the saved (else devkit's default) groups, each optional group offered for the tools
    picked set as answered; None when none was offered, so the default groups stay unpinned."""
    offered = offered_groups(a["clouds"], saved)
    if not offered:
        return None
    groups = list((_setup_cfg(saved).get("skills") or {}).get("groups") or [])
    on = set(accepted_groups(a, saved))
    return [g for g in groups if g not in offered or g in on] + [g for g in offered if g in on and g not in groups]


def build_config(saved, a, login) -> dict:
    """The config to save: the saved one with the answers applied (the keys `dev setup` has always written, and
    skills.groups when an optional skill group was offered)."""
    groups = _skill_groups(saved, a)
    return core.deep_merge(saved or {}, {
        "dev_root": a["root"], "github_user": login, "handoff_repo": a["handoff"],
        "agents": {"claude": a["use_claude"], "codex": a["use_codex"]},
        "claude": {"default_mode": a["claude_mode"]},
        "codex": {"mode": a["codex_mode"], "approval_policy": CODEX_MODES[a["codex_mode"]][0],
                  "sandbox_mode": CODEX_MODES[a["codex_mode"]][1]},
        "stacks": a["stacks"], "lock": a["lock"], "hosts": a["hosts"], "clouds": a["clouds"],
        "docker": {"cpu": a["cpu"], "memory": a["mem"], "disk": a["disk"], "autostart": a["autostart"]},
        "gc": {"auto": a["gc_auto"]}, "sync": {"interval_minutes": a["interval"]},
        "ai": {"enabled": bool(a.get("ai")), "asked": True},
        **({"skills": {"groups": groups}} if groups is not None else {}),
    })


AI_QUESTION = ("Let background jobs ask a small model (Claude Haiku or Codex's gpt-6-luna, at most 20 calls a day), "
               "for example to merge duplicate lessons written in different languages?")
AI_HELP = ("Off by default. Without it, devkit compares where lessons apply (files, commands, kinds of work, errors) "
           "instead. Change it later with ai.enabled in devkit's config.")


def ask_ai(p, saved) -> bool:
    """Background AI is opt-in, and the wizard asks about it once: a saved answer is kept, not asked again."""
    prev = (saved or {}).get("ai") or {}
    if prev.get("asked"):
        return bool(prev.get("enabled"))
    return p.yesno(AI_QUESTION, False)


def plan_rows(a, skip_packages, saved=None) -> list:
    """The plan as the line prompts print it."""
    cm = a["codex_mode"]
    skills = accepted_groups(a, saved)
    return [("config", str(core.tilde(CONFIG_FILE))), ("root", a["root"]),
            ("handoff", f"{a['handoff']} → {a['root']}/.handoff"),
            ("Claude Code", f"{a['claude_mode']}" if a["use_claude"] else "skip"),
            ("Codex", f"{cm} ({CODEX_MODES[cm][0]} / {CODEX_MODES[cm][1]})" if a["use_codex"] else "skip"),
            ("stacks", ", ".join(a["stacks"])),
            ("git hosts", ", ".join(a["hosts"]) + f" · commit template {'on' if a.get('commit_template', True) else 'off'}"),
            ("clouds", (", ".join(a["clouds"]) or "none") + (f" · agent skills: {', '.join(skills)}" if skills
                                                             else "")),
            ("packages", "skip" if skip_packages else "Homebrew + npm for the stacks, hosts and clouds"),
            ("Docker VM", f"{a['cpu']} CPU · {a['mem']} GB · {a['disk']} GB" + (" · at login" if a["autostart"] else "")),
            ("automation", f"sync every {a['interval']} min · safe cleanup {'on' if a['gc_auto'] else 'report only'}"
                           f" · background AI {'on' if a.get('ai') else 'off'}"),
            ("lock", "managed settings (sudo)" if a["lock"] else "off (drift is still blocked and repaired)")]


# --------------------------------------------------------------------------- line prompts (fallback, --yes)

def _run_lines(assume_yes=False, skip_packages=False) -> int:
    print(ui.banner("setup"))
    name = core.gh_user().get("name", "")
    print(f"  {ui.bold(ui.greeting(name))} This wizard sets up devkit on this Mac. Press Enter to keep the value in "
          f"[brackets]; nothing changes until you confirm.")
    d = detect()
    p = Prompter(assume_yes)
    if p.yes and not assume_yes:
        print(f"  {ui.WARN} No terminal to answer from, so every question takes its default (as with --yes). "
              f"To choose, run {ui.bold('dev setup')} in a terminal.")
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
        print(f"\n  {ui.FAIL} devkit needs your GitHub account, and you are not signed in. Run "
              f"{' '.join(GH_LOGIN)}, then dev setup again.")
        return 2

    a = Answers()
    p.section("Workspace")
    a["root"] = p.ask("Development root", cfg["dev_root"])
    login = d["gh"]["login"]
    a["handoff"] = p.ask("Private handoff repository", cfg.get("handoff_repo") or f"{login}/handoff")

    p.section("Agents")
    a["use_claude"] = p.yesno("Configure Claude Code (CLI + Desktop)?", True)
    a["use_codex"] = p.yesno("Configure Codex (CLI + ChatGPT app)?", True)
    a["claude_mode"] = cfg.get("claude", {}).get("default_mode", "bypassPermissions")
    if a["use_claude"]:
        a["claude_mode"] = p.choose("Claude Code permission mode", CLAUDE_MODES, a["claude_mode"])
    a["codex_mode"] = cfg.get("codex", {}).get("mode", "full")
    if a["use_codex"]:
        a["codex_mode"] = p.choose("Codex autonomy", {k: v[2] for k, v in CODEX_MODES.items()}, a["codex_mode"])

    p.section("Stacks (toolchains, language servers, skills)")
    found = [k for k in STACKS if ((d.get("stacks_found") or {}).get("counts") or {}).get(k)]
    a["stacks"] = p.multi("Stacks you develop in (comma-separated numbers)", STACKS,
                          (d["config"] or {}).get("stacks") or found or list(FIRST_STACKS))

    from . import toolchain
    seen = toolchain.detect()
    p.section("Git hosts and CI/CD (CLIs, sign-in checks, `dev ci status`)")
    if seen["evidence"]:
        print("     " + ui.dim("found on this Mac: " + "; ".join(f"{k} ({v})" for k, v in seen["evidence"].items())))
    hosts = p.multi("Where your repositories and pipelines live (comma-separated numbers)",
                    {k: v[0] for k, v in toolchain.HOSTS.items()},
                    list(dict.fromkeys((cfg.get("hosts") or []) + seen["hosts"])) or ["github"])
    a["hosts"] = list(dict.fromkeys(["github"] + hosts))
    from . import setup as setup_mod
    a["commit_template"] = p.yesno(setup_mod.COMMIT_TEMPLATE_QUESTION, setup_mod.commit_template_answer() != "no")
    p.section("Cloud and infrastructure (CLIs and sign-in checks)")
    a["clouds"] = p.multi("Clouds and infrastructure tools you use (numbers, or empty for none)",
                          {k: v[0] for k, v in toolchain.CLOUDS.items()},
                          list(dict.fromkeys((cfg.get("clouds") or []) + seen["clouds"])))
    a["skill_groups"] = saved_group_answers(d["config"])
    for g in offered_groups(a["clouds"], d["config"]):  # optional skill groups, for the tools just picked
        a["skill_groups"][g] = p.yesno(skill_question(g, a["clouds"], d["config"]), a["skill_groups"].get(g, True))

    p.section("Docker on Colima")
    dk = cfg["docker"]
    a["cpu"] = int(p.ask("VM CPUs", str(dk.get("cpu") or max(2, min(8, d["cpus"] // 3)))))
    a["mem"] = int(p.ask("VM memory (GB)", str(dk.get("memory") or max(4, min(16, d["ram"] // 5)))))
    a["disk"] = int(p.ask("VM disk, grows as needed (GB)", str(dk.get("disk") or 80)))
    a["autostart"] = p.yesno("Start Colima at login?", bool(dk.get("autostart")))

    p.section("Automation")
    a["gc_auto"] = p.yesno("Reclaim disk automatically (merged worktrees, idle build output, stale temp)?", cfg["gc"]["auto"])
    a["interval"] = int(p.ask("Background sync interval (minutes)", str(cfg["sync"]["interval_minutes"])))
    a["ai"] = ask_ai(p, d["config"] or {})
    a["lock"] = False
    if a["use_claude"]:
        a["lock"] = p.yesno("Lock Claude Code settings with a managed-settings file (needs your admin password)?",
                            bool(cfg.get("lock")))

    new = build_config(d["config"], a, login)

    p.section("Plan")
    for k, v in plan_rows(a, skip_packages, d["config"]):
        print(f"     {ui.dim(k.ljust(12))} {v}")
    if d["config"] and d["config"] != new:
        changed = sorted(k for k in set(new) | set(d["config"]) if new.get(k) != d["config"].get(k))
        print(f"     {ui.dim('changes'.ljust(12))} {', '.join(changed)}")
    if not p.yesno("Apply this plan?", True):
        print(f"  {ui.WARN} nothing changed")
        return 1

    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n")
    setup_mod.record_commit_template("yes" if a["commit_template"] else "no")  # the git step of `dev install` applies it
    print(f"\n  {ui.OK} saved {core.tilde(CONFIG_FILE)}")
    if not skip_packages:
        rc = install_packages(a["stacks"], a["use_claude"], extra=toolchain.groups({"hosts": a["hosts"], "clouds": a["clouds"]}))
        if rc != 0:
            print(f"  {ui.WARN} some packages failed; continuing with the configuration")
    py = sys.executable
    subprocess.call([py, str(DEVKIT_HOME / "bin" / "dev"), "install"])
    _post_install(p, a["use_claude"], a["use_codex"])
    if a["lock"]:
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


def install_playwright_skill() -> bool:
    """Install the Playwright CLI's agent skill into devkit's shared skills folder.

    `playwright-cli install --skills` writes `.claude/skills/playwright-cli` (and edits `.gitignore`) in its working
    directory, so it runs in a throwaway folder under the workspace temp root, never in the user's repository; only
    the skill is kept, in `~/.local/share/devkit/skills/playwright-cli` (replacing an older copy)."""
    from . import setup
    tmp = Path(core.TMP_ROOT) / f"playwright-skill-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        tmp.mkdir(parents=True)
        rc = subprocess.call(["playwright-cli", "install", "--skills"], cwd=str(tmp), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        src = tmp / ".claude" / "skills" / "playwright-cli"
        if rc != 0 or not src.is_dir():
            return False
        dest = Path(setup.SKILLS_SHARE) / "playwright-cli"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        elif dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest, symlinks=True)
        return True
    except OSError:
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
            install_playwright_skill()
    from . import toolchain
    for line in toolchain.postinstall([k for k, v in toolchain.ALL.items() if v[4] in groups]):
        print(f"  {ui.OK} {line}")
    if with_claude and not core.have("claude"):
        print(f"\n  {ui.ARROW} Claude Code (native installer)")
        rc |= subprocess.call(["/bin/bash", "-c", CLAUDE_INSTALLER])
    return rc


_BREW_RE = re.compile(r'^(brew|cask)\s+"([^"]+)"')
_NPM_ROOT = []


def _npm_root():
    if not _NPM_ROOT:
        rc, out, _ = core.sh(["npm", "root", "-g"], timeout=15) if core.have("npm") else (1, "", "")
        _NPM_ROOT.append(Path(out.strip()) if rc == 0 and out.strip() else None)
    return _NPM_ROOT[0]


def package_groups(stacks, hosts=(), clouds=()) -> list:
    """The package groups these choices install, in install order."""
    from . import toolchain
    extra = toolchain.groups({"hosts": list(hosts), "clouds": list(clouds)})
    return list(dict.fromkeys(list(BASE_GROUPS) + [s for s in STACKS if s in stacks]
                              + [s for s in stacks if s not in STACKS] + extra))


def package_status(groups) -> list:
    """[(group, [(name, kind, installed)])] for the Brewfile and npm packages of these groups. A package listed in
    two groups counts in the first. Local checks only: Homebrew's Cellar, opt and Caskroom, npm's global folder."""
    prefix = Path(os.environ.get("HOMEBREW_PREFIX") or ("/opt/homebrew" if Path("/opt/homebrew").exists() else "/usr/local"))
    npm_root, seen, out = _npm_root(), set(), []
    for g in groups:
        pk = []
        for ln in filtered(DEVKIT_HOME / "Brewfile", {g}):
            m = _BREW_RE.match(ln)
            if not m or m.group(2) in seen:
                continue
            kind, name = m.group(1), m.group(2).split("/")[-1]
            seen.add(m.group(2))
            where = [prefix / "Caskroom" / name] if kind == "cask" else [prefix / "Cellar" / name, prefix / "opt" / name]
            pk.append((name, kind, any(p.exists() for p in where)))
        for name in filtered(DEVKIT_HOME / "config" / "npm-globals.txt", {g}):
            if name in seen:
                continue
            seen.add(name)
            pk.append((name, "npm", bool(npm_root) and (npm_root / name).exists()))
        if pk:
            out.append((g, pk))
    return out


# --------------------------------------------------------------------------- detection for the interactive wizard

# registry.detect_stack ids → the wizard's stacks
_STACK_OF = {"java": "java", "python": "python", "php": "php", "laravel": "php", "symfony": "php",
             "angular": "node", "nextjs": "node", "react": "node", "nestjs": "node", "typescript": "node", "node": "node"}


def detect_stacks() -> dict:
    """{"counts": {stack: repositories}, "repos": n} from the repositories in the workspace."""
    from . import registry
    try:
        mains, plain, _ = registry.discover()
    except Exception:
        return {"counts": {}, "repos": 0}
    counts = {}
    for root in list(mains) + list(plain):
        try:
            kinds = {_STACK_OF.get(s.split("/")[0]) for s in registry.detect_stack(Path(root))} - {None}
        except OSError:
            continue
        for k in kinds:
            counts[k] = counts.get(k, 0) + 1
    return {"counts": counts, "repos": len(mains) + len(plain)}


def detect_docker() -> dict:
    colima = core.have("colima")
    return {"colima": colima, "running": colima and core.sh(["colima", "status"], timeout=20)[0] == 0}


def _parallel(jobs: dict, timeout: float = 90) -> dict:
    """Run the jobs in daemon threads (so a slow CLI never holds up exit); {name: result or exception}."""
    results = {}

    def one(k, fn):
        try:
            results[k] = fn()
        except Exception as e:  # each part is optional; the wizard falls back to defaults
            results[k] = e
    threads = [threading.Thread(target=one, args=(k, fn), daemon=True) for k, fn in jobs.items()]
    for t in threads:
        t.start()
    end = time.time() + timeout
    for t in threads:
        while t.is_alive() and time.time() < end:
            t.join(0.1)  # short joins keep Ctrl-C responsive
    return results


def survey() -> dict:
    """detect(), plus hosts and clouds, stacks in the workspace and Docker; the slow parts run in parallel."""
    from . import providers, toolchain
    r = _parallel({"hw": _hw, "gh": lambda: core.gh_user() if core.have("gh") else {},
                   "providers": lambda: providers.status(include_account=False), "seen": toolchain.detect,
                   "stacks": detect_stacks, "docker": detect_docker})

    def get(k, fallback):
        v = r.get(k)
        return fallback if v is None or isinstance(v, Exception) else v
    cpus, ram, macos, arch = get("hw", (8, 16, "", ""))
    return {"cpus": cpus, "ram": ram, "macos": macos, "arch": arch, "brew": core.have("brew"), "gh": get("gh", {}),
            "providers": get("providers", {"claude": {"installed": core.have("claude")},
                                           "codex": {"installed": core.have("codex")}}),
            "config": core.read_json(CONFIG_FILE, None),
            "seen": get("seen", {"hosts": ["github"], "clouds": [], "evidence": {}}),
            "stacks_found": get("stacks", {"counts": {}, "repos": 0}),
            "docker": get("docker", {"colima": core.have("colima"), "running": False})}


def tui_defaults(d, detected=True) -> Answers:
    """The starting answers: the saved config where there is one; on a first run, what this Mac shows (stacks in
    the workspace, hosts and clouds found); devkit's defaults for the rest. detected=False: the saved setup alone."""
    saved = d.get("config") or {}
    cfg = core.deep_merge(core.DEFAULTS, saved)
    seen = d.get("seen") or {"hosts": [], "clouds": []}
    found = (d.get("stacks_found") or {}).get("counts") or {}
    if "stacks" in saved or not detected:
        stacks = list(cfg.get("stacks") or [])
    else:
        stacks = [k for k in STACKS if found.get(k)] or list(FIRST_STACKS)
    agents, dk = cfg.get("agents") or {}, cfg["docker"]
    claude_mode = (cfg.get("claude") or {}).get("default_mode", "bypassPermissions")
    codex_mode = (cfg.get("codex") or {}).get("mode", "full")
    cpus, ram = max(1, int(d.get("cpus") or 8)), max(2, int(d.get("ram") or 16))
    login = (d.get("gh") or {}).get("login", "")
    return Answers(
        root=cfg["dev_root"], handoff=cfg.get("handoff_repo") or f"{login}/handoff",
        use_claude=bool(agents.get("claude", True)), use_codex=bool(agents.get("codex", True)),
        claude_mode=claude_mode if claude_mode in CLAUDE_MODES else "bypassPermissions",
        codex_mode=codex_mode if codex_mode in CODEX_MODES else "full", stacks=stacks,
        hosts=list(dict.fromkeys(["github"] + list(cfg.get("hosts") or []) + (seen["hosts"] if detected else []))),
        clouds=list(dict.fromkeys(list(cfg.get("clouds") or []) + (seen["clouds"] if detected else []))),
        cpu=min(int(dk.get("cpu") or max(2, min(8, cpus // 3))), cpus),
        mem=min(int(dk.get("memory") or max(4, min(16, ram // 5))), ram),
        disk=int(dk.get("disk") or 80), autostart=bool(dk.get("autostart")),
        gc_auto=bool(cfg["gc"].get("auto", True)), interval=int(cfg["sync"].get("interval_minutes") or 60),
        lock=bool(cfg.get("lock")) and bool(agents.get("claude", True)),
        ai=bool((cfg.get("ai") or {}).get("enabled", False)), skill_groups=saved_group_answers(saved),
        commit_template=_commit_template_default())


# --------------------------------------------------------------------------- interactive wizard: words

SECTIONS = [("workspace", "Workspace"), ("agents", "Agents"), ("stacks", "Stacks"), ("hosts", "Git hosts and CI/CD"),
            ("clouds", "Clouds"), ("docker", "Docker"), ("automation", "Automation")]
STEPS = len(SECTIONS) + 1  # the sections, then the review
STACK_UI = {"java": ("Java / Spring", "", "JDK, Maven, Gradle and the jdtls language server"),
            "python": ("Python", "", "uv and the pyright language server"),
            "php": ("PHP", "", "PHP, Composer and the Intelephense language server"),
            "node": ("Node / TypeScript", "Angular, React", "pnpm, TypeScript, the Angular CLI and the Playwright CLI")}
CLAUDE_UI = {"bypassPermissions": ("No prompts", "Works without asking; the devkit guard blocks unsafe commands."),
             "acceptEdits": ("Ask before commands", "Edits files without asking; asks before it runs a command."),
             "default": ("Ask for everything", "Asks before every tool it uses.")}
CODEX_UI = {"full": ("No prompts, full access", "Works without asking; the devkit guard blocks unsafe commands."),
            "workspace": ("Sandboxed to the project", "Writes only inside the project; asks when it needs more.")}
HOST_HELP = {"github": "Always included: your handoff repository and the gh CLI live here.",
             "gitlab": "Installs glab; sign in with glab auth login.",
             "bitbucket": "No official CLI; export BITBUCKET_TOKEN for pipeline status.",
             "azure-devops": "Installs the Azure CLI and its azure-devops extension."}
AGENT_NAMES = {"claude": "Claude Code", "codex": "Codex"}
SIGN_IN = {"claude": ["claude", "auth", "login"], "codex": ["codex", "login"]}


def _label(key: str) -> str:
    from . import toolchain
    if key in STACK_UI:
        return STACK_UI[key][0]
    return toolchain.ALL[key][0].split(" · ")[0] if key in toolchain.ALL else key


def _int_in(lo: int, hi: int):
    def check(v):
        try:
            n = int(v.strip())
        except ValueError:
            n = None
        return None if n is not None and lo <= n <= hi else f"Enter a whole number from {lo} to {hi}."
    return check


def _valid_root(v: str):
    v = v.strip()
    if not v or tui.clean(v) != v:
        return "Enter a folder, like ~/Development."
    p = core.expand(v)
    if not p.is_absolute():
        return "Use a full path, like ~/Development."
    if p.exists() and not p.is_dir():
        return "That path is a file. Choose a folder."
    return None


_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$")


def _valid_repo(login: str):
    def check(v):
        return None if _REPO_RE.match(v.strip()) else f"Use owner/name, like {login or 'you'}/handoff."
    return check


def _code(text: str) -> str:
    """`commands` in bold, the rest as is."""
    return re.sub(r"`([^`]+)`", lambda m: tui.bold(m.group(1)), text)


class _Q:
    """One question of a section: its answer key, a short label for the answered list, and a prompt factory."""

    def __init__(self, key, label, make, store=None, show=None, when=None):
        self.key, self.label, self.make, self.store, self.show, self.when = key, label, make, store, show, when


class _Log:
    """The full output of an interactive run (`~/.local/state/devkit/logs/setup.log`, private to the user)."""

    def __init__(self):
        self.path = STATE / "logs" / "setup.log"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.chmod(str(self.path), 0o600)
            self.f = os.fdopen(fd, "w", encoding="utf-8")
        except OSError:
            self.f = None

    def write(self, s: str):
        if self.f:
            self.f.write(s)
            self.f.flush()

    def close(self):
        if self.f:
            self.f.close()


def _exec(argv, on_line=None, log=None, env=None) -> int:
    """Run a command with its output captured: each line goes to the log and to `on_line`."""
    argv = [str(x) for x in argv]
    if log:
        log.write(f"\n$ {' '.join(argv)}\n")
    try:
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)
    except OSError as e:
        if log:
            log.write(f"{e}\n")
        if on_line:
            on_line(str(e))
        return 127
    try:
        for raw in p.stdout:
            line = raw.decode("utf-8", "replace")
            if log:
                log.write(line)
            if on_line:
                on_line(line.rstrip("\n").split("\r")[-1])
        return p.wait()
    except BaseException:
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
        raise


@contextmanager
def _brewfile(lines):
    with tempfile.NamedTemporaryFile("w", suffix=".Brewfile", delete=False) as tf:
        tf.write("\n".join(lines) + "\n")
    try:
        yield tf.name
    finally:
        os.unlink(tf.name)


def _dev(*args) -> list:
    return [sys.executable, str(DEVKIT_HOME / "bin" / "dev"), *args]


def _child_env() -> dict:
    return dict(os.environ, PYTHONUNBUFFERED="1", NO_COLOR="1")


# --------------------------------------------------------------------------- interactive wizard: flow

class _Wizard:
    """Welcome → Recommended or Customize → review (with "Change a section") → apply → doctor and next steps."""

    def __init__(self, term, skip_packages=False):
        self.term, self.skip = term, skip_packages
        self.screen = tui.Screen(term)
        self.d, self.a, self.saved_a, self.saved_file = None, None, None, False
        self._pk = {}

    # ---------------------------------------------------------------- entry
    def run(self) -> int:
        try:
            with self.term.session():
                outcome = self._interactive()
        except (tui.Cancelled, KeyboardInterrupt):
            self.term.write(f"\n  {tui.warn('!')} Setup cancelled. Nothing changed.\n\n")
            return 130
        if outcome == "quit":
            self.term.write(f"\n  Nothing changed. Run {tui.bold('dev setup')} again when you are ready.\n\n")
            return 1
        if outcome == "no-github":
            self.term.write(f"\n  {tui.bad('✗')} devkit needs your GitHub account, and you are not signed in. Run "
                            f"{tui.bold(' '.join(GH_LOGIN))}, then {tui.bold('dev setup')} again.\n\n")
            return 2
        return self.apply()

    def _interactive(self):
        self.screen.busy(self.intro, "Looking at this Mac: agents, GitHub, your repositories, Docker …", self._survey)
        while not self.d["gh"].get("login"):
            pick = self.screen.ask(tui.Select("Sign in to GitHub to continue", [
                tui.Option("login", "Sign in to GitHub", desc="Runs gh auth login here, then continues the setup."),
                tui.Option("quit", "Quit", desc="Nothing changes. Run dev setup again after you sign in.")],
                back=False), self.welcome)
            if pick == "quit":
                return "no-github"
            with self.term.suspended():
                subprocess.call(GH_LOGIN)
            self.screen.busy(self.intro, "Checking the GitHub sign-in …", self._refresh_github)
        self.a = tui_defaults(self.d)
        self.saved_a = tui_defaults(self.d, detected=False) if self.d["config"] else None
        mode = "recommended"
        while True:
            mode = self.screen.ask(self.welcome_prompt(mode), self.welcome)
            if mode == "recommended" and self.first_stacks():
                if self.section([k for k, _ in SECTIONS].index("stacks")) is tui.BACK:
                    continue  # Esc on the stacks question: back to the welcome screen
                break
            if mode == "recommended" or self.walk() is not tui.BACK:
                break
        while True:
            act = self.screen.ask(self.review_prompt(), self.review)
            if act != "change":
                return act
            k = self.screen.ask(self.section_prompt(), lambda: self.frame(STEPS, "Review"))
            if k is not tui.BACK:
                self.section(k)

    def first_stacks(self) -> bool:
        """A first run with no repositories to learn from: the recommended setup still asks for the toolchains."""
        return not (self.d.get("config") or {}).get("stacks") and \
            not ((self.d.get("stacks_found") or {}).get("counts") or {})

    def _survey(self):
        self.d = survey()

    def _refresh_github(self):
        try:
            (STATE / "cache" / "gh-user.json").unlink()
        except OSError:
            pass
        self.d["gh"] = core.gh_user() if core.have("gh") else {}

    # ---------------------------------------------------------------- frames
    def top(self) -> list:
        width = self.term.size()[0]
        left, right = f"  {ui.logo_inline()}  {tui.dim('setup')}", tui.dim("ctrl-c quits")
        gap = width - 1 - tui.vlen(left) - tui.vlen(right)
        return [left + (" " * gap + right if gap >= 2 else ""), ""]

    def frame(self, n, title, answered=()) -> list:
        out = self.top() + tui.step_header(n, STEPS, title, self.term.size()[0]) + [""]
        for label, value in answered:
            out.append(f"  {tui.ok('✓')} {tui.dim(label)}  {value}")
        return out + ([""] if answered else [])

    def banner(self) -> list:
        """The Firefly banner; on a small window its one-line form, split in two when even that is too wide."""
        width, height = self.term.size()
        lines = ui.banner("setup", framed=height >= 40 and width >= ui.FRAME_INNER + 6).strip("\n").split("\n")
        if len(lines) == 1 and tui.vlen(lines[0]) > width - 1:
            lines = [f"  {ui.logo_inline()}  {tui.dim(f'v{core.VERSION} · setup')}", f"  {tui.dim(core.COPYRIGHT)}"]
        return lines

    def intro(self) -> list:
        return self.banner() + [""]

    def found(self) -> list:
        """(mark, what, text) rows for what this Mac already has."""
        d, rows = self.d, []
        good, attn, none_, fail = tui.ok("✓"), tui.warn("!"), tui.dim("–"), tui.bad("✗")
        mac = " · ".join(x for x in (f"macOS {tui.clean(d['macos'])}" if d["macos"] else "", tui.clean(d["arch"]),
                                     f"{d['cpus']} CPUs", f"{d['ram']} GB") if x)
        mac = mac.replace(f"{d['ram']} GB", f"{d['ram']} GB memory")
        rows.append((good if d["brew"] else attn, "Mac", mac + (" · Homebrew" if d["brew"] else " · no Homebrew yet")))
        login = tui.clean(d["gh"].get("login", ""))
        rows.append((good, "GitHub", f"signed in as {login}") if login else (fail, "GitHub", "not signed in"))
        for k in ("claude", "codex"):
            x = d["providers"].get(k) or {}
            extra = " · ".join(tui.clean(v) for v in (x.get("version"), f"plan {x['plan']}" if x.get("plan") else "") if v)
            if x.get("logged_in"):
                rows.append((good, AGENT_NAMES[k], "signed in" + (f" · {extra}" if extra else "")))
            elif x.get("installed"):
                rows.append((attn, AGENT_NAMES[k], "installed, not signed in · you can sign in at the end"))
            else:
                rows.append((none_, AGENT_NAMES[k], "not installed · devkit installs it"))
        sf = d["stacks_found"]
        if sf["counts"]:
            order = sorted(sf["counts"], key=lambda k: (-sf["counts"][k], list(STACKS).index(k)))
            rows.append((good, "Stacks", ", ".join(_label(k) for k in order)
                         + f" · found in {sf['repos']} repositor{'ies' if sf['repos'] != 1 else 'y'}"))
        else:
            rows.append((none_, "Stacks", f"none found in {core.tilde(core.DEV)} · you can choose them"))
        rows.append((good, "Git hosts", ", ".join(_label(k) for k in d["seen"]["hosts"]) or "GitHub"))
        clouds = d["seen"]["clouds"]
        rows.append((good, "Clouds", ", ".join(_label(k) for k in clouds)) if clouds else (none_, "Clouds", "none found"))
        dk = d["docker"]
        rows.append((good, "Docker", "Colima running") if dk["running"] else
                    (good, "Docker", "Colima installed · VM stopped") if dk["colima"] else
                    (none_, "Docker", "not installed · devkit installs Colima"))
        return rows

    def welcome(self) -> list:
        d = self.d
        first = not d["config"]
        name = tui.clean(d["gh"].get("name") or "")
        lines = self.banner() + [""]
        lines.append(f"  {tui.bold(ui.greeting(name))} " + ("Let's set up devkit on this Mac." if first else
                                                             "devkit is already set up here."))
        if first:
            lines += ["  devkit sets up Claude Code and Codex with shared rules and a safety guard,",
                      "  and keeps your project notes, tasks, and plans in a private GitHub",
                      "  repository: your handoff repository."]
        lines.append("  " + tui.dim("Nothing changes until you apply the plan at the end." if first else
                                    "Your saved choices are the starting point; nothing changes until you apply."))
        lines += ["", "  " + tui.bold("On this Mac")]
        lines += [f"  {mark} {tui.pad(what, 12)} {text}" for mark, what, text in self.found()]
        return lines + [""]

    def welcome_prompt(self, default="recommended"):
        if self.d["config"]:
            opts = [tui.Option("recommended", "Keep current setup",
                               desc="Your saved choices, plus anything new found here; then the review."),
                    tui.Option("customize", "Customize", desc="Walk through the 7 sections, starting from your saved choices.")]
            title = "How do you want to continue?"
        else:
            opts = [tui.Option("recommended", "Recommended setup",
                               desc="Use what was found on this Mac and go straight to the review."),
                    tui.Option("customize", "Customize",
                               desc="Walk through the 7 sections; every answer starts at the recommended value.")]
            title = "How do you want to set up devkit?"
        return tui.Select(title, opts, default, back=False)

    # ---------------------------------------------------------------- sections
    def questions(self, key) -> list:
        a, d = self.a, self.d
        login = d["gh"].get("login", "")
        yes_no = (lambda k: (lambda: "yes" if a[k] else "no"))
        if key == "workspace":
            return [
                _Q("root", "Development folder", lambda: tui.Text(
                    "Development folder", a["root"], validate=_valid_root,
                    help="Every repository lives here, sorted by owner: <folder>/<owner>/<repo>.")),
                _Q("handoff", "Handoff repository", lambda: tui.Text(
                    "Handoff repository", a["handoff"], validate=_valid_repo(login),
                    help="A private GitHub repository for project notes, tasks and plans, synced across sessions and "
                         "Macs. Enter a shared one (owner/name) to work as a team.")),
            ]
        if key == "agents":
            return [
                _Q("agents", "Agents", lambda: tui.MultiSelect(
                    "Agents to configure", [self._agent_option(k) for k in ("claude", "codex")],
                    [k for k in ("claude", "codex") if a["use_" + k]],
                    help="devkit gives each agent the same rules, guard, hooks and skills."),
                   store=lambda v: a.update(use_claude="claude" in v, use_codex="codex" in v),
                   show=lambda: ", ".join(AGENT_NAMES[k] for k in ("claude", "codex") if a["use_" + k]) or "none"),
                _Q("claude_mode", "Claude Code permissions", lambda: tui.Select(
                    "Claude Code permissions", [tui.Option(k, CLAUDE_UI[k][0], note=k, desc=CLAUDE_UI[k][1],
                                                           badge="recommended" if k == "bypassPermissions" else "")
                                                for k in CLAUDE_MODES], a["claude_mode"],
                    help="How much Claude Code may do without asking you."),
                   show=lambda: CLAUDE_UI[a["claude_mode"]][0], when=lambda: a["use_claude"]),
                _Q("codex_mode", "Codex autonomy", lambda: tui.Select(
                    "Codex autonomy", [tui.Option(k, CODEX_UI[k][0], note=f"{v[0]} · {v[1]}", desc=CODEX_UI[k][1],
                                                  badge="recommended" if k == "full" else "")
                                       for k, v in CODEX_MODES.items()], a["codex_mode"],
                    help="How much Codex may do without asking you."),
                   show=lambda: CODEX_UI[a["codex_mode"]][0], when=lambda: a["use_codex"]),
            ]
        if key == "stacks":
            counts = d["stacks_found"]["counts"]
            preselected = counts and not (d["config"] or {}).get("stacks")
            return [_Q("stacks", "Stacks", lambda: tui.MultiSelect(
                "Stacks you work in", [tui.Option(k, STACK_UI[k][0], note=STACK_UI[k][1], desc=STACK_UI[k][2],
                                                  badge=(f"found in {counts[k]} repo{'s' if counts[k] != 1 else ''}", "ok")
                                                  if counts.get(k) else "") for k in STACKS],
                a["stacks"], help="Each stack adds its toolchain, language server and agent skills."
                + (" The stacks found in your repositories are selected." if preselected else "")),
                show=lambda: ", ".join(_label(k) for k in a["stacks"]) or "none")]
        if key == "hosts":
            from . import toolchain
            return [_Q("hosts", "Git hosts", lambda: tui.MultiSelect(
                "Where your code and pipelines live",
                [self._tool_option(k, HOST_HELP.get(k, ""), locked=k == "github") for k in toolchain.HOSTS],
                a["hosts"], help="devkit installs each host's CLI, checks the sign-in and shows its pipelines in "
                                 "dev ci status."),
                store=lambda v: a.update(hosts=list(dict.fromkeys(["github"] + v))),
                show=lambda: ", ".join(_label(k) for k in a["hosts"])),
                _Q("commit_template", "Commit template", lambda: tui.Confirm(
                    "Open new commit messages with devkit's template?", a["commit_template"],
                    help="Sets commit.template in each repository that has none, in its own git config: the header "
                         "rule, then why, what changed and how it was verified, then the Refs and BREAKING CHANGE "
                         "trailers. Repositories with a template of their own keep it."),
                   show=yes_no("commit_template"))]
        if key == "clouds":
            from . import toolchain
            saved = d.get("config")
            optional = list((_setup_cfg(saved).get("skills") or {}).get("optional") or {})
            return [_Q("clouds", "Clouds", lambda: tui.MultiSelect(
                "Clouds and infrastructure tools",
                [self._tool_option(k, self._cloud_help(k)) for k in toolchain.CLOUDS], a["clouds"],
                help="devkit installs each CLI and checks the sign-in. Leave them all unselected if you use none."),
                show=lambda: ", ".join(_label(k) for k in a["clouds"]) or "none")] + [
                _Q("skill_groups", f"Agent skills · {g}", lambda g=g: tui.Confirm(
                    skill_question(g, a["clouds"], saved), a["skill_groups"].get(g, True),
                    help=f"The {g} group of config/skills.json, for the tools you picked; devkit installs it for "
                         "Claude Code and Codex. Change it later with skills.groups in devkit's config."),
                   store=lambda v, g=g: a["skill_groups"].update({g: bool(v)}),
                   show=lambda g=g: "yes" if a["skill_groups"].get(g, True) else "no",
                   when=lambda g=g: g in offered_groups(a["clouds"], saved)) for g in optional]
        if key == "docker":
            cpus, ram = d["cpus"], d["ram"]
            return [
                _Q("cpu", "VM CPUs", lambda: tui.Text(
                    "VM CPUs", a["cpu"], validate=_int_in(1, cpus),
                    help=f"Colima runs Docker in a small Linux VM. This Mac has {cpus} CPUs; the VM uses them "
                         f"only while containers run."), store=lambda v: a.update(cpu=int(v))),
                _Q("mem", "VM memory", lambda: tui.Text(
                    "VM memory (GB)", a["mem"], validate=_int_in(2, max(2, ram)),
                    help=f"This Mac has {ram} GB of memory."), store=lambda v: a.update(mem=int(v)),
                   show=lambda: f"{a['mem']} GB"),
                _Q("disk", "VM disk", lambda: tui.Text(
                    "VM disk (GB)", a["disk"], validate=_int_in(10, 4000),
                    help="The disk grows as needed, up to this size."), store=lambda v: a.update(disk=int(v)),
                   show=lambda: f"{a['disk']} GB"),
                _Q("autostart", "Start at login", lambda: tui.Confirm(
                    "Start Colima at login?", a["autostart"],
                    help="Docker is then ready after a restart, without running dev docker up."),
                   show=yes_no("autostart")),
            ]
        if key == "automation":
            return [
                _Q("gc_auto", "Reclaim disk space", lambda: tui.Confirm(
                    "Reclaim disk space automatically?", a["gc_auto"],
                    help="Removes merged worktrees, idle build output and old temp files. Work that is not pushed "
                         "is never touched."), show=yes_no("gc_auto")),
                _Q("interval", "Sync interval", lambda: tui.Text(
                    "Background sync interval (minutes)", a["interval"], validate=_int_in(5, 1440),
                    help="How often devkit refreshes the project map and pull requests and pushes the handoff "
                         "repository."), store=lambda v: a.update(interval=int(v)),
                   show=lambda: f"every {a['interval']} min"),
                _Q("ai", "Background AI", lambda: tui.Confirm(AI_QUESTION, a["ai"], help=AI_HELP),
                   show=yes_no("ai"), when=lambda: not ((d.get("config") or {}).get("ai") or {}).get("asked")),
                _Q("lock", "Lock Claude Code settings", lambda: tui.Confirm(
                    "Lock Claude Code settings?", a["lock"],
                    help="Writes a managed-settings file that other tools cannot change; needs your admin password. "
                         "Without it, devkit still finds and repairs drift."),
                   show=yes_no("lock"), when=lambda: a["use_claude"]),
            ]
        raise KeyError(key)

    def _agent_option(self, k):
        x = self.d["providers"].get(k) or {}
        state = (("signed in", "ok") if x.get("logged_in") else ("installed, not signed in", "warn")
                 if x.get("installed") else "devkit installs it")
        notes = {"claude": ("CLI and Desktop", "Anthropic's coding agent; devkit configures ~/.claude."),
                 "codex": ("CLI and ChatGPT app", "OpenAI's coding agent; devkit configures ~/.codex through its API.")}
        return tui.Option(k, AGENT_NAMES[k], note=notes[k][0], badge=state, desc=notes[k][1])

    def _cloud_help(self, k):
        from . import toolchain
        label, cli, _, how, _ = toolchain.ALL[k]
        return f"Installs {cli}" + (f"; sign in with {how}." if how and how != "-" and not how.startswith("get ") else ".")

    def _tool_option(self, k, help_text, locked=False):
        from . import toolchain
        name, _, note = toolchain.ALL[k][0].partition(" · ")
        ev = self.d["seen"]["evidence"].get(k)
        desc = (f"Found: {tui.clean(ev)}. " if ev else "") + help_text
        return tui.Option(k, name, note=note, desc=desc, locked=locked,
                          badge="always" if locked else (("found", "ok") if ev else ""))

    def walk(self):
        i = 0
        while i < len(SECTIONS):
            if self.section(i) is tui.BACK:
                if i == 0:
                    return tui.BACK
                i -= 1
            else:
                i += 1
        return None

    def section(self, i):
        """Ask one section's questions; Esc goes back a question (or out of the section, returning BACK)."""
        key, title = SECTIONS[i]
        qs, answered, visited, j = self.questions(key), [], [], 0
        while j < len(qs):
            q = qs[j]
            if q.when and not q.when():
                j += 1
                continue
            res = self.screen.ask(q.make(), lambda: self.frame(i + 1, title, answered))
            if res is tui.BACK:
                if not visited:
                    return tui.BACK
                j = visited.pop()
                answered.pop()
                continue
            (q.store or (lambda v, k=q.key: self.a.__setitem__(k, v)))(res)
            answered.append((q.label, q.show() if q.show else str(self.a[q.key])))
            visited.append(j)
            j += 1
        if not self.a["use_claude"]:
            self.a["lock"] = False
        return None

    def section_prompt(self):
        rows = dict(self.review_rows(self.a))
        return tui.Select("Which section do you want to change?",
                          [tui.Option(i, title, note=tui.strip(rows.get(title, ""))) for i, (_, title) in enumerate(SECTIONS)],
                          help="You come back to the review when the section is done.")

    # ---------------------------------------------------------------- review
    def review_rows(self, a) -> list:
        skills = accepted_groups(a, self.d.get("config"))
        agents = [f"Claude Code ({CLAUDE_UI[a['claude_mode']][0].lower()})"] if a["use_claude"] else []
        agents += [f"Codex ({CODEX_UI[a['codex_mode']][0].lower()})"] if a["use_codex"] else []
        return [
            ("Workspace", f"{a['root']} · handoff {a['handoff']}"),
            ("Agents", ", ".join(agents) or "none"),
            ("Stacks", ", ".join(_label(k) for k in a["stacks"]) or "none"),
            ("Git hosts and CI/CD", ", ".join(_label(k) for k in a["hosts"])
             + f" · commit template {'on' if a['commit_template'] else 'off'}"),
            ("Clouds", (", ".join(_label(k) for k in a["clouds"]) or "none")
             + (f" · agent skills: {', '.join(skills)}" if skills else "")),
            ("Docker", f"{a['cpu']} CPUs · {a['mem']} GB memory · {a['disk']} GB disk"
                       + (" · starts at login" if a["autostart"] else "")),
            ("Automation", f"sync every {a['interval']} min · disk cleanup {'on' if a['gc_auto'] else 'report only'}"
                           f" · background AI {'on' if a.get('ai') else 'off'}"
                           + (f" · settings lock {'on' if a['lock'] else 'off'}" if a["use_claude"] else "")),
        ]

    def packages(self):
        a = self.a
        k = (tuple(a["stacks"]), tuple(a["hosts"]), tuple(a["clouds"]))
        if k not in self._pk:
            self._pk = {k: package_status(package_groups(a["stacks"], a["hosts"], a["clouds"]))}
        return self._pk[k]

    def package_lines(self, compact=False) -> list:
        kw = 20
        head = f"  {tui.dim(tui.pad('Packages', kw))} "
        if self.skip:
            return [head + "skipped (--skip-packages)"]
        status = self.packages()
        total = sum(len(pk) for _, pk in status)
        have = sum(1 for _, pk in status for _, _, inst in pk if inst)
        todo = total - have
        lines = [head + (f"{todo} to install · {tui.ok(f'✓ {have}')} already installed" if todo else
                         tui.ok(f"✓ all {total} installed"))]
        claude = self.a["use_claude"] and not core.have("claude")
        if compact:
            return lines + ([f"  {' ' * kw} + Claude Code from its official installer"] if claude else [])
        full = [g for g, pk in status if all(i for _, _, i in pk)]
        part = [(g, pk) for g, pk in status if g not in full]
        gw = max((len(g) for g, _ in part), default=6)
        if full:
            n = sum(len(pk) for g, pk in status if g in full)
            lines.append(f"  {' ' * kw} {tui.ok('✓')} {', '.join(full)}  {tui.dim(f'all {n} installed')}")
        for g, pk in part:
            inst = [n for n, _, i in pk if i]
            miss = [n for n, _, i in pk if not i]
            count = f"{len(miss)} to install" + (f", {len(inst)} installed" if inst else "")
            lines.append(f"  {' ' * kw} {tui.dim('+')} {tui.pad(g, gw)}  {tui.dim(count + ':')} " + ", ".join(miss))
        if claude:
            lines.append(f"  {' ' * kw} {tui.dim('+')} {tui.pad('agents', gw)}  Claude Code, from its official installer")
        return lines

    def review(self, compact=None) -> list:
        a = self.a
        width, height = self.term.size()
        out = self.frame(STEPS, "Review")
        rows = self.review_rows(a)
        was = dict(self.review_rows(self.saved_a)) if self.saved_a else {}
        changed = [k for k, v in rows if k in was and was[k] != v]
        if self.saved_a:
            out.append("  " + (tui.bold(f"{len(changed)} change{'s' if len(changed) != 1 else ''} from your saved setup")
                               if changed else tui.dim("No changes from your saved setup. Applying runs dev install "
                                                       "again, which repairs anything that drifted.")))
            out.append("")
        kw = 20
        for k, v in rows:
            out.append(f"  {tui.warn(tui.pad(k, kw)) if k in changed else tui.dim(tui.pad(k, kw))} {v}")
            if k in changed:
                out.append(f"  {' ' * kw} {tui.dim('was ' + was[k])}")
        pk = self.package_lines(compact=bool(compact))
        out += [""] + pk + [f"  {tui.dim(tui.pad('Config file', kw))} {core.tilde(CONFIG_FILE)}", ""]
        if compact is None and len(out) + 12 > height:
            return self.review(compact=True)
        return out

    def review_prompt(self):
        return tui.Select("Apply this plan?", [
            tui.Option("apply", "Apply", desc="Save your choices, install what is missing, configure the agents, "
                                              "then run dev doctor."),
            tui.Option("change", "Change a section", desc="Edit one section, then come back here."),
            tui.Option("quit", "Quit without changes", desc="Leave now; nothing on this Mac changes.")], back=False)

    # ---------------------------------------------------------------- apply
    def apply(self) -> int:
        a, d, term = self.a, self.d, self.term
        width = term.size()[0]
        log = _Log()
        term.write(f"\n  {ui.logo_inline()}  {tui.dim('setup')}\n\n  {tui.bold('Applying your plan')}\n")
        for k, v in self.review_rows(a):
            term.write(tui.fit(f"  {tui.dim(tui.pad(k, 20))} {v}", width - 1) + "\n")
        term.write("\n")
        steps = [("Save your choices", self._save)]
        plan = None if self.skip else self._package_plan()
        if plan and plan["brew"]:
            steps.append(("Install Homebrew packages", lambda cl, i: self._brew(cl, i, plan, log)))
        if plan and plan["npm"]:
            steps.append(("Install npm packages", lambda cl, i: self._npm(cl, i, plan, log)))
        if plan and plan["claude"]:
            steps.append(("Install Claude Code", lambda cl, i: self._claude(cl, i, log)))
        steps.append(("Configure devkit", lambda cl, i: self._configure(cl, i, log)))
        if a["use_claude"] or a["use_codex"]:
            steps.append(("Check agent sign-in", self._sign_in))
        if not d["docker"]["running"]:
            steps.append(("Start Docker", lambda cl, i: self._docker(cl, i, log)))
        if a["lock"]:
            steps.append(("Lock Claude Code settings", self._lock))
        report = {}
        steps.append(("Check the setup", lambda cl, i: report.update(self._doctor(cl, i, log))))
        try:
            with term.quiet(), tui.Checklist(term, [s[0] for s in steps]) as cl:
                for i, (_, fn) in enumerate(steps):
                    fn(cl, i)
        except (KeyboardInterrupt, tui.Cancelled):
            where = f"Your choices are saved in {core.tilde(CONFIG_FILE)}; run dev setup again to finish." \
                if self.saved_file else "Nothing changed."
            term.write(f"\n  {tui.warn('!')} Setup stopped. {where}\n\n")
            log.close()
            return 130
        log.close()
        return self._finish(cl, report, log)

    def _package_plan(self) -> dict:
        from . import toolchain
        a = self.a
        groups = set(BASE_GROUPS) | set(a["stacks"]) | set(toolchain.groups({"hosts": a["hosts"], "clouds": a["clouds"]}))
        return {"groups": groups, "brew": filtered(DEVKIT_HOME / "Brewfile", groups),
                "npm": filtered(DEVKIT_HOME / "config" / "npm-globals.txt", groups),
                "post": [k for k, v in toolchain.ALL.items() if v[4] in groups],
                "claude": a["use_claude"] and not core.have("claude")}

    def _save(self, cl, i):
        cl.start(i)
        new = build_config(self.d["config"], self.a, self.d["gh"]["login"])
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n")
        from . import setup as setup_mod
        setup_mod.record_commit_template("yes" if self.a["commit_template"] else "no")  # `dev install` applies it
        self.saved_file = True
        cl.finish(i, "ok", core.tilde(CONFIG_FILE))

    @staticmethod
    def _tail(tail) -> list:
        return [tui.dim(x) for x in list(tail)[-4:]]

    def _brew(self, cl, i, plan, log):
        from . import toolchain
        todo = [n for _, pk in self.packages() for n, kind, inst in pk if kind != "npm" and not inst]
        cl.start(i, f"{len(todo)} to install" if todo else "checking")
        done, failed, tail = [0], [], deque(maxlen=20)

        def line(s):
            s = tui.clean(s)
            if not s:
                return
            tail.append(s)
            m = re.match(r"^Installing (\S+) has failed", s)
            if m:
                failed.append(m.group(1))
                return
            m = re.match(r"^Installing (\S+)", s)
            if m:
                done[0] += 1
                cl.update(f"{m.group(1)} · {done[0]} of {len(todo)}" if todo else m.group(1))
        with _brewfile(plan["brew"]) as bf:
            rc = _exec(["brew", "bundle", f"--file={bf}", "--no-upgrade"], line, log)
        notes = [tui.clean(x) for x in toolchain.postinstall(plan["post"])]
        if rc == 0:
            cl.finish(i, "ok", f"{len(todo)} installed" if todo else "all installed", notes)
        else:
            cl.finish(i, "fail", ("failed: " + ", ".join(failed)) if failed else f"brew bundle exited with {rc}",
                      self._tail(tail) + notes + [f"Fix: run {tui.bold('dev install --only tools')} to try again."])

    def _npm(self, cl, i, plan, log):
        npm, tail = plan["npm"], deque(maxlen=20)
        cl.start(i, ", ".join(npm))
        rc = _exec(["npm", "install", "-g", "--no-fund", "--no-audit", *npm],
                   lambda s: tail.append(tui.clean(s)) if tui.clean(s) else None, log)
        if "@playwright/cli" in npm and core.have("playwright-cli"):
            cl.update("Playwright CLI skill")
            install_playwright_skill()
        if rc == 0:
            cl.finish(i, "ok", f"{len(npm)} packages")
        else:
            cl.finish(i, "fail", f"npm exited with {rc}",
                      self._tail(tail) + [f"Fix: {tui.bold('npm install -g ' + ' '.join(npm))}"])

    def _claude(self, cl, i, log):
        tail = deque(maxlen=20)
        cl.start(i, "official installer")
        rc = _exec(["/bin/bash", "-c", CLAUDE_INSTALLER], lambda s: tail.append(tui.clean(s)) if tui.clean(s) else None, log)
        if rc == 0:
            cl.finish(i, "ok", "installed")
        else:
            cl.finish(i, "fail", f"installer exited with {rc}",
                      self._tail(tail) + [f"Fix: run {tui.bold(CLAUDE_INSTALLER)}"])

    def _configure(self, cl, i, log):
        parts, fails, notes, tail = [], [], [], deque(maxlen=20)
        cl.start(i, "dev install")

        def line(s):
            s = tui.clean(s)
            if not s:
                return
            tail.append(s)
            if s.startswith("▸ "):
                parts.append(s[2:])
                cl.update(s[2:])
            elif s.startswith("✗ "):
                fails.append(s[2:])
            elif s.startswith("! "):
                notes.append(s[2:])
        rc = _exec(_dev("install"), line, log, env=_child_env())
        if fails or rc not in (0, None):
            cl.finish(i, "fail", f"{len(fails) or 1} problem{'s' if len(fails) > 1 else ''} in {len(parts)} parts",
                      [tui.bad("✗ ") + f for f in fails[:6]] + ([] if fails else self._tail(tail))
                      + [f"Fix: run {tui.bold('dev install')} again; {tui.bold('dev install --only <step>')} repeats "
                         f"one step."])
        else:
            cl.finish(i, "ok", f"{len(parts)} parts" if parts else "done", [tui.warn("! ") + n for n in notes[:3]])

    def _sign_in(self, cl, i):
        from . import providers
        want = [k for k in ("claude", "codex") if self.a["use_" + k]]
        cl.start(i, ", ".join(AGENT_NAMES[k] for k in want))
        st = providers.status(include_account=False)
        need = [k for k in want if (st.get(k) or {}).get("installed") and not (st.get(k) or {}).get("logged_in")]
        if need:
            cl.pause()
            for k in need:
                if tui.ask_inline(self.term, tui.Confirm(f"{AGENT_NAMES[k]} is not signed in. Sign in now? It opens "
                                                         f"the browser.", True, back=False)):
                    with self.term.suspended():
                        subprocess.call(SIGN_IN[k])
            cl.resume()
            st = providers.status(include_account=False)
        still = [k for k in want if not ((st.get(k) or {}).get("installed") and (st.get(k) or {}).get("logged_in"))]
        if not still:
            cl.finish(i, "ok", "signed in: " + ", ".join(AGENT_NAMES[k] for k in want))
        else:
            cl.finish(i, "warn", "not signed in: " + ", ".join(AGENT_NAMES[k] for k in still),
                      [f"Later: {tui.bold(' '.join(SIGN_IN[k]))}" for k in still])

    def _docker(self, cl, i, log):
        cl.start(i, "Colima")
        dk = detect_docker()
        if dk["running"]:
            return cl.finish(i, "ok", "Colima is already running")
        if not dk["colima"]:
            return cl.finish(i, "skip", "Colima is not installed",
                             [f"Later: {tui.bold('dev install --only tools')}, then {tui.bold('dev docker up')}"])
        cl.pause()
        yes = tui.ask_inline(self.term, tui.Confirm("Start Docker (the Colima VM) now?", True, back=False))
        cl.resume()
        if not yes:
            return cl.finish(i, "skip", "later: dev docker up")
        cl.start(i, f"{self.a['cpu']} CPUs · {self.a['mem']} GB")
        tail = deque(maxlen=20)
        rc = _exec(_dev("docker", "up"), lambda s: tail.append(tui.clean(s)) if tui.clean(s) else None, log,
                   env=_child_env())
        if rc == 0:
            cl.finish(i, "ok", "Colima running")
        else:
            cl.finish(i, "fail", f"dev docker up exited with {rc}",
                      self._tail(tail) + [f"Fix: {tui.bold('dev docker up')}; see docs/docker.md"])

    def _lock(self, cl, i):
        cl.pause()
        self.term.write(f"  {tui.accent('?')} devkit needs your admin password to write the managed-settings file.\n")
        with self.term.suspended():
            rc = subprocess.call(_dev("config", "lock"))
        cl.resume()
        cl.start(i)
        cl.finish(i, "ok", "managed settings written") if rc == 0 else \
            cl.finish(i, "fail", f"dev config lock exited with {rc}", [f"Fix: {tui.bold('dev config lock')}"])

    def _doctor(self, cl, i, log) -> dict:
        res = {"passed": None, "total": None, "fails": [], "rc": 1}
        tail, group = deque(maxlen=20), [""]
        cl.start(i, "dev doctor")

        def line(raw):
            raw = tui.plain(raw)
            if raw.strip():
                tail.append(raw.strip())
            m = re.search(r"(\d+)/(\d+) checks passed", raw)
            if m:
                res["passed"], res["total"] = int(m.group(1)), int(m.group(2))
                return
            if raw.startswith("▸ "):
                group[0] = raw[2:].strip()
                return
            m = re.match(r"^  ([✓✗]) (.*)$", raw)
            if m and m.group(1) == "✗":
                body = m.group(2)
                res["fails"].append([group[0], body[:34].rstrip(), body[35:].strip() if len(body) > 34 else "", ""])
                return
            m = re.match(r"^\s{4,}→ (.*)$", raw)
            if m and res["fails"]:
                res["fails"][-1][3] = m.group(1).strip()
        res["rc"] = _exec(_dev("doctor"), line, log, env=_child_env())
        if res["total"] is None:
            cl.finish(i, "fail", "dev doctor did not finish", self._tail(tail) + [f"Fix: run {tui.bold('dev doctor')}"])
        elif not res["fails"]:
            cl.finish(i, "ok", f"all {res['total']} checks pass" if res["total"] != 1 else "the check passes")
        else:
            cl.finish(i, "warn", f"{res['passed']} of {res['total']} checks pass")
        return res

    def _finish(self, cl, report, log) -> int:
        term, width = self.term, self.term.size()[0]
        fails = report.get("fails") or []
        problems = [it for it in cl.items if it.state == "fail"]
        if fails:
            term.write(f"\n  {tui.warn('!')} {tui.bold(f'{len(fails)} check' + ('s' if len(fails) != 1 else '') + ' need attention')}\n")
            nw = max(len(f[1]) for f in fails[:8])
            for _, name, detail, fix in fails[:8]:
                term.write(tui.fit(f"    {tui.bad('✗')} {tui.pad(name, nw)}  {tui.dim(detail)}", width - 1) + "\n")
                if fix:
                    term.write(tui.fit(f"      {tui.dim('→')} {tui.clean(fix)}", width - 1) + "\n")
            if len(fails) > 8:
                term.write(f"    {tui.dim(f'… and {len(fails) - 8} more; run dev doctor to see them all')}\n")
        if not fails and not problems and report.get("total") is not None:
            term.write(f"\n  {tui.ok('✓')} {tui.bold('devkit is ready.')}\n")
        else:
            term.write(f"\n  {tui.bold('devkit is set up.')} {tui.dim('Fix the items above, then run dev doctor again.')}\n")
        agents = [k for k in ("claude", "codex") if self.a["use_" + k]]
        third = {2: "Start `claude` or `codex` in any project; `dev agents test` checks both.",
                 1: f"Start `{agents[0] if agents else ''}` in any project; `dev agents test` checks it.",
                 0: "Run `dev doctor` at any time to check the setup."}[len(agents)]
        steps = ["Open a new terminal, or run `exec zsh`, so the `dev` command is loaded.",
                 "Run `dev` to see your projects; `dev clone <owner>/<repo>` brings in more.", third]
        term.write(f"\n  {tui.bold('Next')}\n")
        for n, s in enumerate(steps, 1):
            term.write(tui.fit(f"  {tui.dim(f'{n}.')} {_code(s)}", width - 1) + "\n")
        term.write(f"\n  {tui.dim('The full output of this run is in ' + core.tilde(log.path))}\n\n")
        return report.get("rc", 1) if report.get("total") is not None else 1
