# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev setup`: the installation and configuration wizard.

Detects the machine and the agents, asks only what matters (with good defaults), shows the plan, then installs
the toolchain for the chosen stacks and applies the configuration (`dev install`), and finishes with `dev doctor`.

In a terminal it is interactive (see tui.py): a welcome that shows what this Mac already has, then "Recommended
setup" (the agents, the stacks when none were found, then the review) or "Customize" (one step per section), a review
whose numbers reopen a section, the install steps that ask nothing, then a sign-in panel for each agent that is not
signed in, and a summary that says what is done, what is left and the command for each item left. Ctrl+C stops the
step in front of the person and asks what next; the run is saved after every step, and `dev setup --resume` (or plain
`dev setup`) continues it. Re-running it starts from the saved config and shows what would change. When stdin or
stdout is not a terminal, with TERM=dumb, or with `--yes`, it uses the plain line prompts; `--yes` accepts every
default (non-interactive installs). `dev setup --preview` draws every screen with sample data and changes nothing.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from . import core, tui, ui
from .core import CONFIG_FILE, DEVKIT_HOME, STATE

STACKS = {"java": "Java / Spring (JDK, Maven, Gradle, jdtls)", "python": "Python (uv, pyright)",
          "php": "PHP (php, Composer, intelephense)", "node": "Node / TypeScript / Angular / React (pnpm, tsc, ng)"}
# The package groups every Mac gets. Each agent is installed only when it is chosen, in its own step (Claude Code
# from its installer, Codex as the `codex` group's cask), never by the groups everyone gets.
BASE_GROUPS = ("core", "docker", "docs", "security")
# A first run on a Mac with no repositories preselects these, and the wizard always asks (the developer's choice,
# 2026-11-06): a light, fast first install; Java and PHP are one answer away.
FIRST_STACKS = ("python", "node")
CLAUDE_MODES = {"bypassPermissions": "no prompts; the devkit guard protects the workspace (recommended)",
                "acceptEdits": "edits without prompts, commands ask", "default": "ask before every tool"}
CODEX_MODES = {"full": ("never", "danger-full-access", "no prompts, full access; the devkit guard protects (recommended)"),
               "workspace": ("on-request", "workspace-write", "sandboxed to the project; asks when it needs more")}
CLAUDE_INSTALLER = "curl -fsSL https://claude.ai/install.sh | bash"  # Claude Code's official native installer
# The commands `dev doctor` requires, by package group (the `# @group` of the Brewfile and npm-globals.txt), with what
# installs each: ("brew" | "cask", name) from the Brewfile, ("npm", name) from npm-globals.txt, ("script", command).
# The base groups are required on every Mac; a stack's group only when the developer chose it (config `stacks`).
DOCTOR_TOOLS = {
    "core": {"git": ("brew", "git"), "gh": ("brew", "gh"), "python3": ("brew", "python"), "node": ("brew", "node"),
             "npm": ("brew", "node"), "jq": ("brew", "jq"), "rg": ("brew", "ripgrep"), "fzf": ("brew", "fzf")},
    "docker": {"docker": ("brew", "docker"), "colima": ("brew", "colima")},
    "claude": {"claude": ("script", CLAUDE_INSTALLER)}, "codex": {"codex": ("cask", "codex")},
    "java": {"java": ("brew", "openjdk"), "mvn": ("brew", "maven"), "gradle": ("brew", "gradle"),
             "jdtls": ("brew", "jdtls")},
    "python": {"uv": ("brew", "uv"), "pyright": ("npm", "pyright")},
    "php": {"php": ("brew", "php"), "composer": ("brew", "composer"), "intelephense": ("npm", "intelephense")},
    "node": {"pnpm": ("brew", "pnpm"), "typescript-language-server": ("npm", "typescript-language-server"),
             "ng": ("npm", "@angular/cli")},
}
# HTTPS with the browser: no questions about protocols or tokens, and git signs in through gh.
GH_LOGIN = ["gh", "auth", "login", "-h", "github.com", "-p", "https", "-w", "-s", "repo,read:org,workflow,gist"]


class Answers(dict):
    pass


def agent_groups(agents) -> list:
    """The package groups of the agents turned on in `agents` ({"claude": bool, "codex": bool}): what `dev install
    --only tools` and `dev doctor` expect for them."""
    agents = agents or {}
    return [k for k in ("claude", "codex") if agents.get(k, True)]


def agent_choice(a) -> str:
    """The agent question's answer for these answers: "both", "claude" or "codex"."""
    return "both" if a.get("use_claude") and a.get("use_codex") else "codex" if a.get("use_codex") else "claude"


def default_agents(d) -> tuple:
    """(use Claude Code, use Codex) to start from: the saved answer; else the agents signed in on this Mac (one
    signed in: that one only); else both."""
    saved = (d.get("config") or {}).get("agents")
    if isinstance(saved, dict):
        return bool(saved.get("claude", True)), bool(saved.get("codex", True))
    signed = [k for k in ("claude", "codex") if ((d.get("providers") or {}).get(k) or {}).get("logged_in")]
    if len(signed) == 1:
        return signed == ["claude"], signed == ["codex"]
    return True, True


def _commit_template_default() -> bool:
    """The commit template question's default: the recorded answer, yes before the first one (spec P-fy3bwa0pshk R9)."""
    from . import setup as setup_mod
    return setup_mod.commit_template_answer() != "no"


def _template_args(a) -> list:
    """The commit template answer for `dev install`, whose git step applies and records it: the wizard writes no
    devkit state itself."""
    return ["--commit-template", "yes" if a.get("commit_template", True) else "no"]


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


def run(assume_yes=False, skip_packages=False, resume=False) -> int:
    """`dev setup`: the interactive wizard in a terminal; the line prompts otherwise (and always with --yes). A saved
    setup that did not finish is offered first ("Pick up where you left off?"); `resume` (`dev setup --resume`)
    continues it, or says there is nothing to continue."""
    if not assume_yes and tui.available():
        return interactive(tui.Terminal(), skip_packages, resume)
    return _run_lines(assume_yes, skip_packages, resume)


def interactive(term, skip_packages=False, resume=False) -> int:
    """The interactive front end on `term`: the resume screen when a saved setup is unfinished, else the questions."""
    run, old = SetupRun.saved()
    if resume and (run is None or (not old and not run.unfinished_run())):
        term.write("\n" + "\n".join(nothing_to_continue(run, term.size()[0])) + "\n\n")
        return 0
    w = _Wizard(term, skip_packages if run is None else bool(run.data.get("skip_packages")) or skip_packages)
    if run is not None and (old or run.unfinished_run()):
        return w.run(saved_run=run, old=old)
    return w.run()


def nothing_to_continue(run, width=200) -> list:
    """W-18: `dev setup --resume` with no unfinished setup, wrapped to the width."""
    if run is None:
        lines = ["Nothing to continue: no setup is saved on this Mac.", "To set up devkit, run dev setup."]
    else:
        lines = [f"Nothing to continue: the last setup finished on {_day(run.data.get('updated'), weekday=False)}.",
                 "To change your setup, run dev setup."]
    return ["  " + x for line in lines for x in tui.wrap(line, width - 3)]


def _day(ts, weekday=True) -> str:
    """'Monday, November 9 at 10:42' (or without the weekday) in this Mac's time zone."""
    t = time.localtime(float(ts or 0))
    return (time.strftime("%A, ", t) if weekday else "") + f"{time.strftime('%B', t)} {t.tm_mday} at " \
        + time.strftime("%H:%M", t)


def again_command() -> str:
    """How to start the setup again: the installer's own copy when it started this run (DEVKIT_INSTALLER, set by
    install.sh), never a URL; else dev setup."""
    inst = os.environ.get("DEVKIT_INSTALLER", "").strip()
    return str(core.tilde(Path(inst))) if inst else "dev setup"


def local_installer() -> str:
    """The local install.sh to name when the person may run the installer again, or "" when there is none."""
    inst = os.environ.get("DEVKIT_INSTALLER", "").strip()
    if inst:
        return str(core.tilde(Path(inst)))
    f = core.source_home() / "install.sh"
    return str(core.tilde(f)) if f.is_file() else ""


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

AGENT_LINE = {"both": "Claude Code and Codex", "claude": "Claude Code only", "codex": "Codex only"}


def _run_lines(assume_yes=False, skip_packages=False, resume=False) -> int:
    saved, old = SetupRun.saved()
    unfinished = saved is not None and not old and saved.unfinished_run()
    if resume and not unfinished:
        print("\n".join(nothing_to_continue(saved)))
        return 0
    if unfinished:
        return _resume_lines(saved, assume_yes)
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
              f"{' '.join(GH_LOGIN)}, then {again_command()} again.")
        return 2

    a = Answers()
    p.section("Workspace")
    a["root"] = p.ask("Development root", cfg["dev_root"])
    login = d["gh"]["login"]
    a["handoff"] = p.ask("Private handoff repository", cfg.get("handoff_repo") or f"{login}/handoff")

    p.section("Agents")
    use = default_agents(d)
    pick = p.choose("Which coding agents do you want to use?", AGENT_LINE,
                    agent_choice({"use_claude": use[0], "use_codex": use[1]}))
    a["use_claude"], a["use_codex"] = pick in ("both", "claude"), pick in ("both", "codex")
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
    d.setdefault("docker", detect_docker())
    a["start_docker"] = True
    if d["docker"].get("colima") is not None and not d["docker"].get("running"):
        a["start_docker"] = p.yesno(START_DOCKER, True)

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
        print(f"  {ui.WARN} Nothing changed. Run {again_command()} when you are ready.")
        return 1
    w = _Wizard(tui.Terminal(), skip_packages, lines=True)
    w.d, w.a, w.run_state = d, a, SetupRun.start(a, skip_packages)
    return w.apply()


def _resume_lines(run, assume_yes=False) -> int:
    """`dev setup --resume` (or `dev setup` with an unfinished setup) in the line prompts: the saved answers, and
    every step that is not done, after a quick check of the ones that are."""
    d = detect()
    if not d["gh"].get("login"):
        print(f"\n  {ui.FAIL} devkit needs your GitHub account, and you are not signed in. Run "
              f"{' '.join(GH_LOGIN)}, then dev setup --resume.")
        return 2
    d.setdefault("docker", detect_docker())
    left = [s for s in run.steps if s.get("state") != "done"]
    print(f"  {ui.ARROW} Continuing the setup that stopped on {_day(run.data.get('updated'))}"
          + (f", at \"{left[0]['title']}\"" if left else "") + ".")
    w = _Wizard(tui.Terminal(), bool(run.data.get("skip_packages")), lines=True)
    w.d, w.a, w.run_state = d, Answers(run.answers), run
    return w.apply()


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


def install_packages(stacks, with_claude=True, extra=(), with_codex=False) -> int:
    """`dev install --only tools`: brew bundle and npm for the groups everyone gets, the stacks and `extra`; Codex's
    cask with `with_codex`, Claude Code's installer with `with_claude` when it is missing."""
    groups = set(BASE_GROUPS) | set(stacks) | set(extra or ()) | ({"codex"} if with_codex else set())
    print(f"\n  {ui.ARROW} Homebrew packages for: {', '.join(sorted(groups))}")
    brew = filtered(DEVKIT_HOME / "Brewfile", groups)
    with tempfile.NamedTemporaryFile("w", suffix=".Brewfile", delete=False) as tf:
        tf.write("\n".join(brew) + "\n")
        bf = tf.name
    rc = subprocess.call(["brew", "bundle", f"--file={bf}", "--no-upgrade"], env=dict(os.environ, HOMEBREW_NO_ASK="1"))
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


def _npm_globals(groups) -> list:
    """The npm packages of these groups (config/npm-globals.txt)."""
    return filtered(DEVKIT_HOME / "config" / "npm-globals.txt", groups)


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
    dk = cfg["docker"]
    use_claude, use_codex = default_agents(d if detected else {"config": dict(saved, agents=cfg.get("agents"))})
    claude_mode = (cfg.get("claude") or {}).get("default_mode", "bypassPermissions")
    codex_mode = (cfg.get("codex") or {}).get("mode", "full")
    cpus, ram = max(1, int(d.get("cpus") or 8)), max(2, int(d.get("ram") or 16))
    login = (d.get("gh") or {}).get("login", "")
    return Answers(
        root=cfg["dev_root"], handoff=cfg.get("handoff_repo") or f"{login}/handoff",
        use_claude=use_claude, use_codex=use_codex,
        claude_mode=claude_mode if claude_mode in CLAUDE_MODES else "bypassPermissions",
        codex_mode=codex_mode if codex_mode in CODEX_MODES else "full", stacks=stacks,
        hosts=list(dict.fromkeys(["github"] + list(cfg.get("hosts") or []) + (seen["hosts"] if detected else []))),
        clouds=list(dict.fromkeys(list(cfg.get("clouds") or []) + (seen["clouds"] if detected else []))),
        cpu=min(int(dk.get("cpu") or max(2, min(8, cpus // 3))), cpus),
        mem=min(int(dk.get("memory") or max(4, min(16, ram // 5))), ram),
        disk=int(dk.get("disk") or 80), autostart=bool(dk.get("autostart")),
        gc_auto=bool(cfg["gc"].get("auto", True)), interval=int(cfg["sync"].get("interval_minutes") or 60),
        lock=bool(cfg.get("lock")) and use_claude, start_docker=True,
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
# What each agent's account needs, in two clauses so the agent question can break the line between them. Plan names
# change over time: check them at each release.
AGENT_NEEDS = {"claude": ("a Claude Pro, Max, Team or Enterprise plan,", "or an Anthropic Console account"),
               "codex": ("a ChatGPT Plus, Pro, Business, Edu or Enterprise plan,", "or an OpenAI API key")}
PANEL_NEEDS = {"claude": "Claude Code needs your Claude account: a Pro, Max, Team or Enterprise plan, or an "
                         "Anthropic Console account.",
               "codex": "Codex needs your ChatGPT account (a Plus, Pro, Business, Edu or Enterprise plan) or an "
                        "OpenAI API key."}
AGENT_ONLY = {"claude": "Anthropic's agent, in the terminal and Claude Desktop.",
              "codex": "OpenAI's agent, in the terminal and the ChatGPT app."}
REMOVE_APP = {"claude": "rm -rf ~/.local/bin/claude ~/.local/share/claude", "codex": "brew uninstall --cask codex"}
# The command that signs in later (the summary's "To finish"), and each way to sign in from the panel.
SIGN_IN = {"claude": ["claude", "auth", "login"], "codex": ["codex", "login"]}
DEVICE_SIGN_IN = ["codex", "login", "--device-auth"]  # Codex's sign-in with a code: a link and a code for any device
SIGN_IN_WAYS = {"claude": {"browser": ["claude", "auth", "login", "--claudeai"], "sso": ["claude", "auth", "login", "--sso"],
                           "console": ["claude", "auth", "login", "--console"]},
                "codex": {"browser": ["codex", "login"], "code": DEVICE_SIGN_IN}}
WAY_WORDS = {"browser": ("Sign in with the browser", "browser"), "code": ("Sign in with a code", "code"),
             "sso": ("Sign in with your company's single sign-on", "single sign-on"),
             "console": ("Sign in with an Anthropic Console account", "Console"),
             "skip": ("Skip for now", "later"), "again": ("Try again", "try again")}
SKIPPED_BY_CHOICE = "you chose Skip for now"
NO_TERMINAL = "no terminal to sign in from"
START_DOCKER = "Start Docker when the setup ends?"


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


CRASH_EXIT = 70  # bugs.CRASH_EXIT: a `dev` command that crashed printed devkit's crash notice on stderr
_CRASH_START = re.compile(r"^devkit bug [0-9a-f]{12}\b")
_CRASH_FILE = re.compile(r"^\s*Details: (\S+)")


class _Exit(int):
    """A child's exit code, as an int, with how it ended: the last lines it wrote on stderr, the signal that stopped
    it ("" when none), and whether its time limit stopped it."""

    def __new__(cls, rc, err=(), signal_name="", timed_out=False):
        self = super().__new__(cls, rc)
        self.err, self.signal, self.timed_out = [x for x in err if x], signal_name, timed_out
        return self

    @property
    def crash(self) -> list:
        """devkit's crash notice (`dev` printed it on stderr and exited 70), its first line first; [] when none."""
        start = next((n for n, x in enumerate(self.err) if _CRASH_START.match(x)), None)
        return self.err[start:] if start is not None else []

    @property
    def crash_file(self) -> str:
        """The crash file the notice names (the traceback, kept on this Mac), or ""."""
        return next((m.group(1) for m in map(_CRASH_FILE.match, self.crash) if m), "")


def _exec(argv, on_line=None, log=None, env=None, timeout=None) -> _Exit:
    """Run a command with stdout and stderr captured apart, as they come: each line goes to the log and to
    `on_line`; stderr's last lines stay with the exit code. A command still running after `timeout` seconds is
    stopped. Ctrl-C stops the command, then reaches the caller."""
    argv = [str(x) for x in argv]
    if log:
        log.write(f"\n$ {' '.join(argv)}\n")
    try:
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env=env)
    except OSError as e:
        if log:
            log.write(f"{e}\n")
        if on_line:
            on_line(str(e))
        return _Exit(127, [str(e)])
    err, pending = deque(maxlen=40), {p.stdout.fileno(): b"", p.stderr.fileno(): b""}
    errfd = p.stderr.fileno()

    def emit(raw: bytes, fd: int):
        line = raw.decode("utf-8", "replace")
        if log:
            log.write(line + "\n")
        shown = line.split("\r")[-1]
        if fd == errfd:
            err.append(tui.clean(shown))
        if on_line:
            on_line(shown)
    sel = selectors.DefaultSelector()
    for f in (p.stdout, p.stderr):
        sel.register(f, selectors.EVENT_READ)
    deadline, timed_out, quiet_since = (time.monotonic() + timeout) if timeout else None, False, None
    try:
        while sel.get_map():
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                timed_out = True
                _stop(p)
                break
            events = sel.select(0.5 if left is None else min(left, 0.5))
            if not events and p.poll() is not None:  # it exited; a process it left behind may hold the pipes open
                quiet_since = quiet_since or time.monotonic()
                if time.monotonic() - quiet_since > 2:
                    break
                continue
            quiet_since = None
            for key, _ in events:
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    sel.unregister(key.fileobj)
                    continue
                *lines, pending[key.fd] = (pending[key.fd] + chunk).split(b"\n")
                for raw in lines:
                    emit(raw, key.fd)
        for fd, rest in pending.items():  # a last line without its newline
            if rest:
                emit(rest, fd)
        rc = p.wait()
    except BaseException:
        _stop(p)
        raise
    finally:
        sel.close()
        p.stdout.close()
        p.stderr.close()
    name = ""
    if rc < 0:
        try:
            name = signal.Signals(-rc).name
        except ValueError:
            name = f"signal {-rc}"
    return _Exit(rc, err, name, timed_out)


def _stop(p):
    """Stop a child: SIGTERM, then SIGKILL when it is still there after 5 seconds."""
    if p.poll() is not None:
        return
    p.terminate()
    try:
        p.wait(5)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()


def _ended(rc) -> str:
    """How a child ended, in words."""
    if getattr(rc, "timed_out", False):
        return "stopped at its time limit"
    if getattr(rc, "signal", ""):
        return f"stopped by {rc.signal}"
    if rc < 0:
        try:
            return f"stopped by {signal.Signals(-rc).name}"
        except ValueError:
            return f"stopped by signal {-rc}"
    if rc == CRASH_EXIT and getattr(rc, "crash", None):
        return f"devkit crashed (exit code {int(rc)})"
    return f"exit code {int(rc)}"


def _reasons(rc, tail=(), part="", n=4) -> list:
    """Why a child failed, for a step's detail: the part it was in, then devkit's crash notice and crash file, or the
    last lines it wrote on stderr, or else its last output lines."""
    out = [f"in {part}"] if part else []
    crash = getattr(rc, "crash", [])
    if crash:
        return out + [crash[0]] + ([f"Details: {rc.crash_file}"] if rc.crash_file else [])
    return out + ((getattr(rc, "err", []) or [x for x in tail if x])[-n:])


@contextmanager
def _brewfile(lines):
    with tempfile.NamedTemporaryFile("w", suffix=".Brewfile", delete=False) as tf:
        tf.write("\n".join(lines) + "\n")
    try:
        yield tf.name
    finally:
        os.unlink(tf.name)


def _dev(*args) -> list:
    """A `dev` command with the flags of its shebang (-I -S), and -u: -I ignores PYTHONUNBUFFERED, and a child killed
    by a signal loses whatever its block-buffered output still held."""
    return [sys.executable, "-I", "-S", "-u", str(DEVKIT_HOME / "bin" / "dev"), *args]


def _child_env() -> dict:
    """The environment of the wizard's children: no colors, and Homebrew never stops to ask (`HOMEBREW_NO_ASK`,
    Homebrew's documented switch for its default ask mode)."""
    return dict(os.environ, PYTHONUNBUFFERED="1", NO_COLOR="1", HOMEBREW_NO_ASK="1")


# --------------------------------------------------------------------------- the terminal, for commands that talk

SIGN_IN_TIMEOUT = 600   # a browser sign-in still open after 10 minutes is stopped
DEVICE_TIMEOUT = 900    # a device-code sign-in gets 15 minutes
DOCKER_TIMEOUT = 900    # `dev docker up` starts a VM; after 15 minutes it is stopped and can be run later


def terminal_device():
    """This Mac's terminal by its own device name (/dev/ttys003), for a command that talks to the person, or None
    without a terminal. Never /dev/tty, its alias: macOS's kqueue refuses a descriptor opened from /dev/tty with
    EINVAL, and Claude Code's runtime watches its input with kqueue, so it crashed when the installer, run through a
    pipe, gave it /dev/tty."""
    for fd in (0, 1, 2):
        try:
            if os.isatty(fd):
                name = os.ttyname(fd)
                if name and name != "/dev/tty":
                    return name
        except OSError:
            pass
    rc, out, _ = core.sh(["ps", "-o", "tty=", "-p", str(os.getpid())], timeout=5)
    name = out.strip()
    if rc == 0 and re.fullmatch(r"tty[\w.]+", name or "") and os.path.exists("/dev/" + name):
        return "/dev/" + name
    return None


class _Ended(str):
    """How a command that talked to the person ended: "ok", "failed", "crashed" (a signal other than Ctrl-C),
    "interrupted" (Ctrl-C), "timeout", "missing" (not installed) or "no-terminal" (nothing ran); `error` is the last
    line it wrote on stderr."""

    def __new__(cls, outcome, error=""):
        self = super().__new__(cls, outcome)
        self.error = error
        return self


def _copy_size(src: int, dst: int):
    """Give the pseudo-terminal `dst` the window size of the terminal `src`."""
    try:
        import fcntl
        import termios as _termios
        fcntl.ioctl(dst, _termios.TIOCSWINSZ, fcntl.ioctl(src, _termios.TIOCGWINSZ, b"\0" * 8))
    except (ImportError, OSError):
        pass


def _attached(argv, timeout=None, device=None, clock=time.monotonic) -> _Ended:
    """Run a command that talks to the person (a sign-in) on the terminal, with a time limit -> how it ended.

    Its stdin and stdout are the terminal device itself, also when the installer arrived through a pipe. Its stderr
    is a pseudo-terminal whose output is copied to that terminal as it comes, so the command still writes to a
    terminal and the wizard keeps its last error line for the reason. Ctrl-C reaches the command too and ends only it.
    `clock` measures the time limit (tests pass a fake one)."""
    device = device or terminal_device()
    if not device:
        return _Ended("no-terminal")
    try:
        fd = os.open(device, os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return _Ended("no-terminal")
    master = slave = None
    try:
        import pty
        master, slave = pty.openpty()
        _copy_size(fd, slave)
    except OSError:
        master = slave = None
    tail = deque(maxlen=20)

    def relay():
        buf = b""
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            try:
                os.write(fd, chunk)
            except OSError:
                pass
            *lines, buf = (buf + chunk).split(b"\n")
            tail.extend(x for x in (tui.clean(ln.decode("utf-8", "replace").split("\r")[0]) for ln in lines) if x)
        last = tui.clean(buf.decode("utf-8", "replace"))
        if last:
            tail.append(last)
    reader = None
    try:
        try:
            p = subprocess.Popen([str(a) for a in argv], stdin=fd, stdout=fd, stderr=slave if slave is not None else fd)
        except OSError:
            return _Ended("missing")
        if slave is not None:
            os.close(slave)  # only the command holds it now: the relay reads to the end when the command exits
            slave = None
            reader = threading.Thread(target=relay, daemon=True)
            reader.start()
        deadline = None if timeout is None else clock() + timeout
        outcome = None
        try:
            while True:
                try:
                    rc = p.wait(0.2)
                    break
                except subprocess.TimeoutExpired:
                    if deadline is not None and clock() >= deadline:
                        _stop(p)
                        outcome = "timeout"
                        rc = p.returncode
                        break
        except KeyboardInterrupt:  # the command got the same Ctrl-C: give it a moment to stop by itself
            try:
                p.wait(3)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                _stop(p)
            outcome, rc = "interrupted", p.returncode
        if reader is not None:
            reader.join(2)
        if outcome is None:
            outcome = "ok" if rc == 0 else "interrupted" if rc in (-signal.SIGINT, 130) else \
                "crashed" if rc is not None and rc < 0 else "failed"
        return _Ended(outcome, tail[-1] if tail else "")
    finally:
        for x in (slave, master, fd):
            if x is not None:
                try:
                    os.close(x)
                except OSError:
                    pass


# --------------------------------------------------------------------------- why a step failed, in one line

_NOISE = [re.compile(p) for p in (
    r"[█▄▀╗╔╝╚║═░▒▓]",                                   # the banner's letters, progress bars
    r"^v?\d+\.\d+(\.\d+)?\b",                           # a version line
    r"©|\bCopyright\b|\bApache License\b|\bSPDX\b",     # a copyright line
    r"^▸ ",                                              # a part header of dev install
    r"^✓ ",                                              # a line that reports success
    r"^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏|/\\-]+$",                          # a spinner frame
    r"^\s*[#=\-.]{4,}\s*\d*%?\s*$",                      # a text progress bar
    r"^\s*\d{1,3}(\.\d+)?%\s*$",                         # a percentage alone
    r"Every repo in its place",                         # the tagline
)]


def _meaningful(line: str) -> bool:
    """A line that can be a reason: not empty, not a banner, version, copyright, part header, spinner or bar."""
    line = tui.clean(line)
    return bool(line) and not any(p.search(line) for p in _NOISE)


def _reason(rc, tail=(), cmd="the command") -> str:
    """Why a command failed, in one line, first match wins: a line it marks as an error (✗ …); the last meaningful
    line of its stderr, then of its stdout; else that it gave no reason."""
    marked = [tui.clean(x)[2:].strip() for x in tail if tui.clean(x).startswith("✗ ")]
    if marked:
        return marked[-1]
    for line in list(reversed(getattr(rc, "err", []) or [])) + list(reversed(list(tail))):
        if _meaningful(line):
            line = tui.clean(line)
            if getattr(rc, "signal", "") and int(rc) < 0:
                return f"{line} ({cmd} was stopped by {rc.signal})"
            return line
    if getattr(rc, "timed_out", False):
        return f"{cmd} did not finish in time and was stopped."
    if getattr(rc, "signal", ""):
        return f"{cmd} was stopped by {rc.signal} and gave no reason."
    return f"{cmd} stopped with exit code {int(rc)} and gave no reason."


# What `dev install` was doing in each of its parts, for "devkit hit a bug while …".
PART_VERBS = (("Pinned copy", "pinning its copy"), ("devkit configuration", "writing its configuration"),
              ("dev command", "setting up the dev command"), ("Commit template", "setting up the commit template"),
              ("Git identity", "setting up git"), ("Handoff repository", "setting up your handoff repository"),
              ("Global agent instructions", "writing the agents' rules"), ("Skills", "linking the skills"),
              ("Claude Code", "setting up Claude Code"), ("Codex", "setting up Codex"),
              ("Tools for your stacks", "installing tools"), ("Docker", "setting up Docker"),
              ("Documentation checks", "setting up the documentation checks"),
              ("Scheduled jobs", "setting up the scheduled jobs"), ("Background sync", "setting up the sync"),
              ("Claude Desktop", "setting up Claude Desktop"), ("Prompt helper", "setting up the prompt helper"),
              ("zsh helpers", "setting up the zsh helpers"), ("Pinned copies", "cleaning up old copies"))


def _crash_reason(rc, part: str) -> tuple:
    """(reason, crash file) for a `dev` command that crashed (exit 70 with devkit's notice): the exception from the
    crash file the notice names, else the notice's first line."""
    verb = next((v for k, v in PART_VERBS if part.startswith(k)), "")
    path, exc = rc.crash_file, ""
    if path:
        try:
            lines = [x for x in Path(os.path.expanduser(path)).read_text(encoding="utf-8").splitlines() if x.strip()]
            exc = tui.clean(lines[-1]) if lines else ""
        except OSError:
            exc = ""
    if not exc and rc.crash:
        exc = tui.clean(rc.crash[0]).split(": ", 1)[-1]
    return f"devkit hit a bug{' while ' + verb if verb else ''}: {exc or 'see the bug report'}", path


# --------------------------------------------------------------------------- the apply steps and where they stand

RUN_STATES = ("pending", "running", "done", "failed", "skipped")
ALWAYS_RUN = ("doctor",)  # a resumed run checks the setup again, after the steps it finished
REDO_ON_CHANGE = ("save", "configure", "doctor")  # steps done with the answers, run again when they change
REQUIRED_HINT = "devkit cannot be ready without this step."


def answers_changed(before, after) -> bool:
    """Whether `after` changes an answer that a saved run recorded in `before`. A key `before` lacks is the default
    of a question a newer devkit asks, not a change."""
    def norm(v):
        return json.loads(json.dumps(v))
    return any(norm(after.get(k)) != norm(v) for k, v in (before or {}).items())


class SetupRun:
    """One setup's apply steps and where each stands, in ~/.local/state/devkit/setup-run.json (0600), written after
    every change: the answers, whether packages were skipped, and per step its id, title, whether it is required,
    its state (pending, running, done, failed, skipped), why it is not done, the command that finishes it, and what
    it did (detail). `dev setup` and `dev setup --resume` read it and run every step that is not done, with the same
    answers. Its outcome is derived from the steps: a step pending is a paused run; every step done is ready; a
    required step not done is not ready; else ready with items left."""

    VERSION = 1

    def __init__(self, data: dict):
        self.data = data

    @staticmethod
    def path() -> Path:
        return STATE / "setup-run.json"

    @classmethod
    def start(cls, answers, skip_packages=False) -> SetupRun:
        now = round(time.time(), 3)
        return cls({"version": cls.VERSION, "started": now, "updated": now, "answers": dict(answers),
                    "skip_packages": bool(skip_packages), "steps": []})

    @classmethod
    def load(cls):
        """The saved run, or None when there is none or it cannot be read."""
        run, old = cls.saved()
        return None if old else run

    @classmethod
    def saved(cls) -> tuple:
        """(the saved run, whether another devkit version wrote it), or (None, False). A run of another version keeps
        its answers only: its steps cannot be trusted."""
        data = core.read_json(cls.path(), None)
        if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
            return None, False
        if data.get("version") != cls.VERSION or not isinstance(data.get("steps"), list):
            return cls(dict(data, version=cls.VERSION, steps=[])), True
        return cls(data), False

    @property
    def answers(self) -> dict:
        return self.data["answers"]

    @property
    def steps(self) -> list:
        return self.data["steps"]

    def plan(self, specs):
        """The steps of this apply, in order ([(id, title, required, fix)]), keeping what a saved run recorded."""
        old = {s.get("id"): s for s in self.steps if isinstance(s, dict)}
        self.data["steps"] = [dict(old.get(i) or {"state": "pending", "reason": "", "fix": ""}, id=i, title=t,
                                   required=bool(r)) for i, t, r, _ in specs]
        for s in self.steps:
            if s.get("state") not in RUN_STATES or s["state"] == "running":
                s["state"] = "pending"
        self.save()

    def step(self, sid: str) -> dict:
        return next((s for s in self.steps if s["id"] == sid), {})

    def state(self, sid: str) -> str:
        return self.step(sid).get("state", "pending")

    def set(self, sid: str, state: str, reason: str = "", fix: str = "", **extra):
        s = self.step(sid)
        if s:
            s.update(state=state, reason=reason, fix=fix, **extra)
            self.save()

    def unfinished(self) -> list:
        return [s for s in self.steps if s.get("state") != "done"]

    def outcome(self) -> str:
        """paused | ready | not-ready | left (ready with items left)."""
        states = [s.get("state") for s in self.steps]
        if not states or any(x in ("pending", "running") for x in states):
            return "paused"
        if all(x == "done" for x in states):
            return "ready"
        if any(s.get("required") and s.get("state") != "done" for s in self.steps):
            return "not-ready"
        return "left"

    def unfinished_run(self) -> bool:
        """A paused or not-ready run: `dev setup` offers to continue it."""
        return self.outcome() in ("paused", "not-ready")

    def save(self):
        """Atomically, readable by this account only; a run that cannot be saved still runs."""
        self.data["updated"] = round(time.time(), 3)
        f = self.path()
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(self.data, indent=1, ensure_ascii=False) + "\n")
            os.replace(tmp, f)
        except OSError:
            pass


class _Step:
    """One apply step: what it does (`run(checklist, index)` -> None when done, else a _Fail), whether devkit can be
    ready without it, the steps it needs, what its failure means, the command that finishes it and what that command
    does, a quick check that a step done earlier still holds, and its stage (3: installs, 4: sign-ins and checks)."""

    def __init__(self, sid, title, required, run, fix, does, means="", needs=(), check=None, cmd="", stage=3):
        self.sid, self.title, self.required, self.run, self.fix, self.does = sid, title, required, run, fix, does
        self.means, self.needs, self.check, self.cmd, self.stage = means, tuple(needs), check, cmd, stage


class _Fail:
    """Why a step did not finish: the reason line, what it means when it differs from the step's own meaning, and
    the crash file of a devkit bug."""

    def __init__(self, reason: str, means: str = "", crash: str = ""):
        self.reason, self.means, self.crash = reason, means, crash


class _Quit(Exception):
    """The person quit before Apply (W-6): nothing changed."""


# --------------------------------------------------------------------------- interactive wizard: flow

class _Wizard:
    """Welcome → Recommended (agents, stacks when none were found) or Customize → review (its numbers reopen a
    section) → the install steps → the sign-ins → the summary. `lines`: the same apply steps for the line front end,
    printed one line each, with line prompts."""

    def __init__(self, term, skip_packages=False, lines=False):
        self.term, self.skip, self.lines = term, skip_packages, lines
        self.screen = tui.Screen(term)
        self.d, self.a, self.saved_a, self.baseline, self.saved_file = None, None, None, None, False
        self.run_state = None  # SetupRun: the apply steps and where they stand (dev setup --resume reads it)
        self._pk = {}
        self.total = STEPS     # the steps of the path taken: "Step 2 of 3"
        self.extra = {}        # per step id: what to record with it when it is done
        self.by_id = {}
        self.log = None

    # ---------------------------------------------------------------- entry
    def run(self, saved_run=None, old=False) -> int:
        try:
            with self.term.session():
                outcome = self._interactive(saved_run, old)
        except (_Quit, tui.Cancelled, KeyboardInterrupt):
            outcome = "quit"
        if outcome == "quit":
            self.term.write(f"\n  Nothing changed. Run {tui.bold(again_command())} when you are ready.\n\n")
            return 1
        if outcome == "later":
            self.term.write(f"\n  Your setup is saved as it is. To continue it, run {tui.bold('dev setup --resume')}."
                            "\n\n")
            return 130
        if outcome == "no-github":
            self.term.write(f"\n  {tui.bad('✗')} devkit needs your GitHub account, and you are not signed in. Run "
                            f"{tui.bold(' '.join(GH_LOGIN))}, then {tui.bold(again_command())} again.\n\n")
            return 2
        return self.apply()

    def _interactive(self, saved_run=None, old=False):
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
        self.saved_a = tui_defaults(self.d, detected=False) if self.d["config"] else None
        if saved_run is not None:
            try:
                pick = self.screen.ask(self.resume_prompt(saved_run, old), self.intro)
            except (tui.Cancelled, KeyboardInterrupt):
                return "later"
            if pick in ("continue", "review"):
                self.a = self._fill(saved_run.answers)
                self.run_state = saved_run
                if pick == "continue":
                    return "apply"
                self.total, self.baseline = 1, Answers(self.a)
                return self.review_loop()
        self.a = tui_defaults(self.d)
        mode = "recommended"
        while True:
            mode = self.ask(self.welcome_prompt(mode), self.welcome)
            if mode == "recommended":
                if self.recommended() is tui.BACK:
                    continue
                break
            self.total = STEPS
            if self.walk() is not tui.BACK:
                break
        self.baseline = self.saved_a or Answers(json.loads(json.dumps(self.a)))
        return self.review_loop()

    def _fill(self, answers) -> Answers:
        """Saved answers, with the answers a newer devkit asks filled in from today's defaults."""
        a = tui_defaults(self.d)
        a.update(answers or {})
        return a

    def ask(self, prompt, static):
        """A question screen. Ctrl-C asks first (W-6): Keep going returns to the same question as it was; Quit, or a
        second Ctrl-C, ends the setup with nothing changed."""
        while True:
            try:
                return self.screen.ask(prompt, static)
            except (tui.Cancelled, KeyboardInterrupt):
                q = tui.Confirm("Quit the setup?", True, help="Nothing has changed on this Mac. Your answers are not "
                                                              "saved.", back=False, yes="Keep going", no="Quit",
                                hints=["←→ switch", "enter confirm", "ctrl-c quit"])
                try:
                    keep = self.screen.ask(q, static)
                except (tui.Cancelled, KeyboardInterrupt):
                    keep = False
                if not keep:
                    raise _Quit()

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

    def recommended(self):
        """The recommended path: the agent question on a first run, the stacks when none were found; Esc goes back
        one question, and from the first to the welcome."""
        path = (["agents"] if not self.d.get("config") else []) + (["stacks"] if self.first_stacks() else [])
        self.total = len(path) + 1
        i = 0
        while i < len(path):
            n, key = i + 1, path[i]
            if key == "agents":
                res = self.ask(self.agents_prompt(), lambda n=n: self.frame(n, "Agents", total=self.total))
                if res is not tui.BACK:
                    self._set_agents(res)
            else:
                res = self.ask(self.questions("stacks")[0].make(), lambda n=n: self.frame(n, "Stacks", total=self.total))
                if res is not tui.BACK:
                    self.a["stacks"] = res
            if res is tui.BACK:
                if i == 0:
                    return tui.BACK
                i -= 1
                continue
            i += 1
        return None

    def _set_agents(self, v):
        self.a.update(use_claude=v in ("both", "claude"), use_codex=v in ("both", "codex"))
        if not self.a["use_claude"]:
            self.a["lock"] = False

    # ---------------------------------------------------------------- frames
    def top(self, right="esc back · ctrl-c quits") -> list:
        width = self.term.size()[0]
        left, right = f"  {ui.logo_inline()}  {tui.dim('setup')}", tui.dim(right)
        gap = width - 1 - tui.vlen(left) - tui.vlen(right)
        return [left + (" " * gap + right if gap >= 2 else ""), ""]

    def frame(self, n, title, answered=(), total=None, right="esc back · ctrl-c quits") -> list:
        out = self.top(right) + tui.step_header(n, total or STEPS, title, self.term.size()[0]) + [""]
        for label, value in answered:
            out.append(f"  {tui.ok('✓')} {tui.dim(label)}  {value}")
        return out + ([""] if answered else [])

    def banner(self) -> list:
        """The banner; on a small window its one-line form, split in two when even that is too wide."""
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
        mac = " · ".join(x for x in (f"macOS {tui.clean(d['macos'])}" if d["macos"] else "", _arch(d["arch"]),
                                     f"{d['cpus']} CPUs", f"{d['ram']} GB memory") if x)
        rows.append((good if d["brew"] else attn, "Mac", mac + (" · Homebrew" if d["brew"] else " · no Homebrew yet")))
        login = tui.clean(d["gh"].get("login", ""))
        rows.append((good, "GitHub", f"signed in as {login}") if login else (fail, "GitHub", "not signed in"))
        for k in ("claude", "codex"):
            x = d["providers"].get(k) or {}
            extra = " · ".join(tui.clean(v) for v in (x.get("version"), f"plan {_plan(x)}" if x.get("plan") else "") if v)
            if x.get("logged_in"):
                rows.append((good, AGENT_NAMES[k], "signed in" + (f" · {extra}" if extra else "")))
            elif x.get("installed"):
                rows.append((attn, AGENT_NAMES[k], "installed, not signed in · you sign in at the end"))
            else:
                rows.append((none_, AGENT_NAMES[k], "not installed · devkit can install it"))
        sf = d["stacks_found"]
        if sf["counts"]:
            order = sorted(sf["counts"], key=lambda k: (-sf["counts"][k], list(STACKS).index(k)))
            rows.append((good, "Stacks", ", ".join(_label(k) for k in order)
                         + f" · found in {sf['repos']} repositor{'ies' if sf['repos'] != 1 else 'y'}"))
        else:
            rows.append((none_, "Stacks", f"none found in {core.tilde(core.DEV)} · you choose them next"))
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
        width = self.term.size()[0]
        name = tui.clean(d["gh"].get("name") or "")
        lines = self.banner() + [""]
        lines.append(f"  {tui.bold(ui.greeting(name))} " + ("Let's set up devkit on this Mac." if first else
                                                             "devkit is already set up here."))
        if first:
            lines += ["  " + x for x in tui.wrap(
                "devkit sets up Claude Code and Codex with shared rules and a safety guard, and keeps your project "
                "notes, tasks and plans in a private GitHub repository.", width - 5)]
        lines += ["  " + tui.dim(x) for x in tui.wrap(
            "Nothing changes until you choose Apply." if first else
            "Your saved choices are the starting point; nothing changes until you choose Apply.", width - 5)]
        lines += ["", "  " + tui.bold("On this Mac")]
        for mark, what, text in self.found():  # each row's text wraps under its column, never cut
            parts = _wrap_words(text, width - 19)
            lines += [f"  {mark} {tui.pad(what, 12)} {parts[0]}"] + [" " * 17 + x for x in parts[1:]]
        return lines + [""]

    def welcome_prompt(self, default="recommended"):
        time_line = ["", "  " + tui.dim("About 3 minutes of questions, then 10 to 20 minutes of installing on its own.")]
        if self.d["config"]:
            opts = [tui.Option("recommended", "Keep current setup",
                               desc="Your saved choices, plus anything new found here; then the review."),
                    tui.Option("customize", "Customize", desc="Walk through the 7 sections, starting from your saved choices.")]
            title = "How do you want to continue?"
        else:
            opts = [tui.Option("recommended", "Recommended setup",
                               desc="Choose your agents and stacks, then review everything." if self.first_stacks()
                               else "Choose your agents, then review everything."),
                    tui.Option("customize", "Customize",
                               desc="Go through all 7 sections; each answer starts at the recommended value.")]
            title = "How do you want to set up devkit?"
        return tui.Select(title, opts, default, back=False, foot=lambda w: [ln for x in time_line
                                                                            for ln in _wrapped(x, w)])

    def agents_prompt(self):
        """W-3: one of three answers, never none, with what each agent's account needs always in view."""
        pv = self.d.get("providers") or {}

        def found(k):
            x = pv.get(k) or {}
            return ("installed · signed in", "ok") if x.get("logged_in") else \
                ("installed, not signed in", "warn") if x.get("installed") else ""

        def only(k):
            other = "codex" if k == "claude" else "claude"
            rest = (f"devkit does not set up {AGENT_NAMES[other]}." if (pv.get(other) or {}).get("installed")
                    else f"{AGENT_NAMES[other]} is not installed.")
            return f"{AGENT_ONLY[k]} {rest}"
        opts = [tui.Option("both", "Claude Code and Codex", badge="recommended",
                           desc="Use either one in any project. You sign in to each at the end."),
                tui.Option("claude", "Claude Code only", badge=found("claude"), desc=only("claude")),
                tui.Option("codex", "Codex only", badge=found("codex"), desc=only("codex"))]
        return tui.Select("Which coding agents do you want to use?", opts, agent_choice(self.a),
                          help="devkit gives each one the same rules, safety guard and skills.",
                          foot=_needs_lines, badge_right=True)

    def resume_prompt(self, run, old=False):
        """W-17: where the saved setup stopped, each step as it stands, and Continue, Review or Start over."""
        left = [s for s in run.steps if s.get("state") != "done"]
        at = f", at \"{left[0]['title']}\"" if left else ""
        body = [_row(_mark(s), s["title"], _why(s) if s.get("state") != "done" else s.get("detail") or "")
                for s in run.steps] + ([""] if run.steps else [])
        help_ = [f"The setup stopped on {_day(run.data.get('updated'))}{at}."]
        if old:
            help_.append("This setup was saved by an older devkit; check your answers, then apply.")
        opts = ([] if old else [tui.Option("continue", "Continue", desc=f"Runs the {len(left)} step"
                                           f"{'s' if len(left) != 1 else ''} left with your saved answers.")]) + [
            tui.Option("review", "Review my answers first", desc="Opens the review. Change anything, then apply."),
            tui.Option("over", "Start over", desc="Asks every question again. What is installed stays installed.")]
        return tui.Select("Pick up where you left off?", opts, help=help_, body=body, back=False)

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
                _Q("agents", "Agents", self.agents_prompt, store=self._set_agents,
                   show=lambda: AGENT_LINE[agent_choice(a)]),
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
                _Q("start_docker", "Start at the end", lambda: tui.Confirm(
                    START_DOCKER, a.get("start_docker", True),
                    help="Docker then runs right away. It also needs about 2 minutes the first time."),
                   show=yes_no("start_docker"), when=lambda: not d["docker"]["running"]),
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
                    help="Writes a managed-settings file that other tools cannot change; needs your admin password "
                         "at the end. Without it, devkit still finds and repairs drift."),
                   show=yes_no("lock"), when=lambda: a["use_claude"]),
            ]
        raise KeyError(key)

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
            res = self.ask(q.make(), lambda: self.frame(i + 1, title, answered))
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

    def reopen(self, i):
        """A section opened from the review: its answers, or the section unchanged when Esc leaves it."""
        before = json.loads(json.dumps(self.a))
        if self.section(i) is tui.BACK:
            self.a = Answers(before)

    def section_prompt(self):
        rows = dict(self.review_rows(self.a))
        return tui.Select("Which section do you want to change?",
                          [tui.Option(i, title, note=tui.strip(rows.get(title, ""))) for i, (_, title) in enumerate(SECTIONS)],
                          help="You come back to the review when the section is done.")

    # ---------------------------------------------------------------- review
    def review_loop(self):
        """The review until Apply or Quit: 1 to 7 (or Change a section) open a section and come back here."""
        while True:
            act = self.ask(self.review_prompt(), self.review)
            if isinstance(act, int):
                self.reopen(act)
            elif act == "change":
                k = self.ask(self.section_prompt(), lambda: self.frame(self.total, "Review", total=self.total))
                if k is not tui.BACK:
                    self.reopen(k)
            else:
                return act

    def review_rows(self, a) -> list:
        skills = accepted_groups(a, self.d.get("config"))
        agents = [f"Claude Code ({CLAUDE_UI[a['claude_mode']][0].lower()})"] if a["use_claude"] else []
        agents += [f"Codex ({CODEX_SHORT[a['codex_mode']]})"] if a["use_codex"] else []
        starts = [x for x, on in (("starts at the end", a.get("start_docker", True)
                                   and not (self.d.get("docker") or {}).get("running")),
                                  ("starts at login", a["autostart"])) if on]
        return [
            ("Workspace", f"{a['root']} · handoff {a['handoff']}"),
            ("Agents", ", ".join(agents)),
            ("Stacks", ", ".join(_label(k) for k in a["stacks"]) or "none"),
            ("Git hosts and CI/CD", ", ".join(_label(k) for k in a["hosts"])
             + f" · commit template {'on' if a['commit_template'] else 'off'}"),
            ("Clouds", (", ".join(_label(k) for k in a["clouds"]) or "none")
             + (f" · agent skills: {', '.join(skills)}" if skills else "")),
            ("Docker", " · ".join([f"{a['cpu']} CPUs", f"{a['mem']} GB memory", f"{a['disk']} GB disk"] + starts)),
            ("Automation", f"sync every {a['interval']} min · disk cleanup {'on' if a['gc_auto'] else 'off'}"
                           f" · AI {'on' if a.get('ai') else 'off'}"
                           + (" · settings lock on" if a["use_claude"] and a["lock"] else "")),
        ]

    def _removal_lines(self, width) -> list:
        """Under the Agents row, for an agent the saved setup had and these answers turn off: what devkit removes from
        it, what stays, and how to remove the app (devkit does not)."""
        out, lead = [], " " * 26
        for k in ("claude", "codex"):
            if self.saved_a and self.saved_a.get("use_" + k) and not self.a.get("use_" + k):
                name = AGENT_NAMES[k]
                text = (f"devkit removes its rules, hooks and skills from {name}. {name} and your own settings "
                        f"stay. To remove the app: {REMOVE_APP[k]}")
                lines = tui.wrap(text, width - 31)
                out += [lead + tui.warn("!") + " " + lines[0]] + [lead + "  " + x for x in lines[1:]]
        return out

    def packages(self):
        a = self.a
        k = (tuple(a["stacks"]), tuple(a["hosts"]), tuple(a["clouds"]))
        if k not in self._pk:
            self._pk = {k: package_status(package_groups(a["stacks"], a["hosts"], a["clouds"]))}
        return self._pk[k]

    def package_counts(self) -> tuple:
        """(packages to install, packages already installed) for the answers."""
        if self.skip:
            return 0, 0
        status = self.packages()
        total = sum(len(pk) for _, pk in status)
        have = sum(1 for _, pk in status for _, _, inst in pk if inst)
        return total - have, have

    def _agents_to_install(self) -> list:
        return [k for k in ("claude", "codex") if self.a.get("use_" + k) and not core.have(k)]

    def _agents_to_sign_in(self) -> list:
        pv = self.d.get("providers") or {}
        return [k for k in ("claude", "codex") if self.a.get("use_" + k) and not (pv.get(k) or {}).get("logged_in")]

    def review(self) -> list:
        a = self.a
        width = self.term.size()[0]
        out = self.frame(self.total, "Review", total=self.total, right="ctrl-c quits")
        rows = self.review_rows(a)
        base = self.baseline or {}
        was = dict(self.review_rows(base)) if base else {}
        changed = [k for k, v in rows if k in was and was[k] != v]
        if self.saved_a:
            out.append("  " + (tui.bold(f"{len(changed)} change{'s' if len(changed) != 1 else ''} from your saved setup")
                               if changed else tui.dim("No changes from your saved setup. Applying runs dev install "
                                                       "again, which repairs anything that drifted.")))
            out.append("")
        kw, lead = 20, " " * 25

        def value(v) -> list:  # a value wrapped under its column, never cut, never broken at a hyphen
            return _wrap_words(v, width - 28)
        for n, (k, v) in enumerate(rows, 1):
            parts = value(v)
            out.append(f"  {tui.dim(str(n))}  {tui.warn(tui.pad(k, kw)) if k in changed else tui.pad(k, kw)} {parts[0]}")
            out += [lead + " " + x for x in parts[1:]]
            if k in changed:
                out += [lead + " " + tui.dim(x) for x in value("was " + was[k])]
            if k == "Agents":
                out += self._removal_lines(width)
        todo, have = self.package_counts()
        pk = ("skipped (--skip-packages)" if self.skip else
              f"{todo} to install · ✓ {have} already installed" if todo else
              f"✓ all {have} installed" if have else "nothing to install")
        parts = value(pk)
        out += ["", f"     {tui.pad('Packages', kw)} {parts[0]}"] + [lead + " " + x for x in parts[1:]]
        agents = self._agents_to_install()
        if agents:
            out += [lead + " " + x for x in value(
                f"+ {' and '.join(AGENT_NAMES[k] for k in agents)}, from "
                f"{'their own installers' if len(agents) > 1 else 'its own installer'}")]
        parts = _path_lines("", str(core.tilde(CONFIG_FILE)), width - 28)
        out += [f"     {tui.pad('Config file', kw)} {parts[0]}"] + [lead + " " + x for x in parts[1:]] + [""]
        mins = max(5, -(-todo * 20 // 300) * 5) if todo else 0
        work = (f"about {mins} minutes of installing that needs nothing from you," if mins else
                "about 2 minutes of setting up that needs nothing from you,")
        who = self._agents_to_sign_in()
        then = (f"then you sign in to {' and '.join(AGENT_NAMES[k] for k in who)} (about 2 minutes)." if who else
                "then devkit checks the setup.")
        out += ["  " + x for x in tui.wrap(f"After you apply: {work} {then}", width - 5)] + [""]
        return out

    def review_prompt(self):
        return tui.Select("Apply this plan?", [
            tui.Option("apply", "Apply", desc="Save your choices, install what is missing, set up the agents, sign in."),
            tui.Option("change", "Change a section", desc="Or press 1 to 7 to open that section. You come back here "
                                                          "after it."),
            tui.Option("quit", "Quit without changes", desc="Nothing on this Mac changes.")], back=False,
            hints=["1–7 change a section", "↑↓ move", "enter choose"], digits={str(n): n - 1 for n in range(1, 8)})

    # ---------------------------------------------------------------- apply
    def apply(self) -> int:
        """Stage 3, the install steps (they ask nothing), then stage 4, the sign-ins and the check, then the summary
        and its menu -> the exit code: 0 ready (items may be left), 1 a required step is not done, 130 paused."""
        a, term = self.a, self.term
        self.log = _Log()
        steps = self.plan_steps()
        self.by_id = {s.sid: s for s in steps}
        run = self.run_state or SetupRun.start(a, self.skip)
        self.run_state = run
        changed = answers_changed(run.answers, a)
        run.data["answers"] = dict(a)
        run.plan([(s.sid, s.title, s.required, s.fix) for s in steps])
        if changed:  # answers changed on the review: save them, and set up and check devkit with them, again
            for sid in REDO_ON_CHANGE:
                run.set(sid, "pending")
        if self.lines:
            term.write(f"\n  {tui.bold('Applying your plan')}\n")
        else:
            term.write(f"\n  {ui.logo_inline()}  {tui.dim('setup')}\n\n  {tui.bold('Applying your plan')}\n"
                       f"  {tui.dim('This runs on its own. Ctrl+C skips or stops a step.')}\n\n")
        paused = self._run_stages(run)
        return self._finish(run, paused)  # it closes the log: the summary's menu may run more steps into it

    def _run_stages(self, run, only=None) -> bool:
        """Run the steps of both stages (only those in `only`, when given) -> whether the person stopped the setup."""
        steps = [s for s in self.by_id.values() if only is None or s.sid in only]
        install = [s for s in steps if s.stage == 3]
        who = [k for k in ("claude", "codex") if f"signin-{k}" in self.by_id]
        footer = ["", "  " + tui.dim(f"Then you sign in to {' and '.join(AGENT_NAMES[k] for k in who)}." if who else
                                     "Then devkit checks the setup.")]
        if install:
            cl = self._checklist([s.title for s in install], footer)
            try:
                with self._quiet(), cl:
                    for i, st in enumerate(install):
                        if self._one(cl, i, st, run) == "stop":
                            return True
            except (KeyboardInterrupt, tui.Cancelled):  # Ctrl-C between two steps: the next one stops
                st = next((s for s in install if run.state(s.sid) not in ("done", "failed", "skipped")), None)
                if st:
                    run.set(st.sid, "failed" if st.required else "skipped", "interrupted", st.fix)
                return True
        return self._stage4([s for s in steps if s.stage == 4], run)

    def _checklist(self, labels, footer=()):
        return tui.LineChecklist(self.term, labels) if self.lines else tui.Checklist(self.term, labels, footer=footer)

    def _quiet(self):
        return contextlib.nullcontext() if self.lines else self.term.quiet()

    def plan_steps(self) -> list:
        """The apply steps for these answers, in order, with what each needs (the step table of the spec)."""
        a, d = self.a, self.d
        use = [k for k in ("claude", "codex") if a.get("use_" + k)]
        names = " and ".join(AGENT_NAMES[k] for k in use) or "The agents"
        steps = [_Step("save", "Save your choices", True, self._save, "dev setup --resume", "saves your choices",
                       "Nothing else can run without your choices.", check=CONFIG_FILE.exists)]
        plan = None if self.skip else self._package_plan()
        if plan and plan["brew"]:
            steps.append(_Step("brew", "Install Homebrew packages", False, lambda cl, i: self._brew(cl, i, plan),
                               "dev install --only tools", "installs the missing tools", "Some tools are missing.",
                               needs=("save",), check=lambda: self._bundle_ok(plan), cmd="brew bundle"))
        if plan and plan["npm"]:
            names_npm = " ".join(plan["npm"])
            steps.append(_Step("npm", "Install npm packages", False, lambda cl, i: self._npm(cl, i, plan),
                               "npm install -g " + names_npm, "installs the npm packages",
                               "Language servers and tools from npm are missing: " + ", ".join(plan["npm"]) + ".",
                               needs=("save",), check=lambda: self._npm_ok(plan), cmd="npm"))
        for k in use:
            if not core.have(k):
                name = AGENT_NAMES[k]
                steps.append(_Step(k, f"Install {name}", True, lambda cl, i, k=k: self._install_agent(cl, i, k),
                                   CLAUDE_INSTALLER if k == "claude" else "brew install --cask codex",
                                   f"installs {name}", f"{name} is not installed, so it cannot sign in.",
                                   needs=("save",), check=lambda k=k: core.have(k),
                                   cmd="Claude Code's installer" if k == "claude" else "brew install"))
        steps.append(_Step("configure", "Set up devkit", True, self._configure, "dev setup --resume",
                           "tries Set up devkit again, then the rest",
                           f"{names} {'do' if len(use) > 1 else 'does'} not have devkit's rules and safety guard yet.",
                           needs=("save",), cmd="dev install"))
        dk = d.get("docker") or {}
        if a.get("start_docker", True) and not dk.get("running") and (dk.get("colima") or (plan and plan["brew"])):
            steps.append(_Step("docker", "Start Docker", False, self._docker, "dev docker up", "starts Docker",
                               "Docker is not running; projects that use containers wait until it is.",
                               needs=("brew",), check=lambda: detect_docker()["running"], cmd="dev docker up"))
        for k in use:
            name = AGENT_NAMES[k]
            steps.append(_Step(f"signin-{k}", f"Sign in to {name}", False, None, " ".join(SIGN_IN[k]),
                               f"signs you in to {name}", f"{name} is installed but cannot work until you sign in.",
                               needs=(k,), check=lambda k=k: self._signed_in(k), stage=4))
        if a.get("lock") and a.get("use_claude"):
            steps.append(_Step("lock", "Lock Claude Code settings", False, None, "dev config lock",
                               "locks Claude Code settings",
                               "Claude Code settings are not locked; devkit still finds and repairs changes.",
                               needs=("configure",), stage=4))
        steps.append(_Step("doctor", "Check the setup", False, self._doctor, "dev doctor", "checks the setup",
                           "devkit did not check the setup.", cmd="dev doctor", stage=4))
        return steps

    def _blocked(self, st, run):
        """The first step `st` needs that is in the plan and not done, or None."""
        return next((self.by_id[n] for n in st.needs if n in self.by_id and run.state(n) != "done"), None)

    def _still(self, st) -> bool:
        """A step done in an earlier run: its quick check still holds (a check that cannot run counts as holding)."""
        try:
            return st.check is None or bool(st.check())
        except Exception:  # the check itself failed: keep the step done; the final check catches real drift
            return True

    def _one(self, cl, i, st, run) -> str:
        """One step: kept when done earlier and still holding, waiting when a step it needs is not done, else run."""
        if run.state(st.sid) == "done" and st.sid not in ALWAYS_RUN and self._still(st):
            cl.start(i)
            cl.finish(i, "ok", run.step(st.sid).get("detail") or "done earlier")
            return "next"
        need = self._blocked(st, run)
        if need is not None:
            why = f"waits for {need.title}"
            cl.finish(i, "skip", why)
            run.set(st.sid, "skipped", why, st.fix)
            return "next"
        return self._attempt(cl, i, st, run)

    def _attempt(self, cl, i, st, run) -> str:
        """Run a step until it ends: done, failed (with why, what it means and the fix), or stopped by the person,
        who then skips it, tries it again or stops the setup -> "next" or "stop"."""
        while True:
            run.set(st.sid, "running")
            self.extra.pop(st.sid, None)
            try:
                fail = st.run(cl, i)
            except (KeyboardInterrupt, tui.Cancelled):
                choice = self._interrupted(cl, i, st)
                if choice == "again":
                    continue
                if choice == "skip":
                    cl.finish(i, "skip", "skipped by you")
                    run.set(st.sid, "skipped", "interrupted", st.fix)
                    return "next"
                cl.finish(i, "warn", "stopped by you")
                run.set(st.sid, "failed" if st.required else "skipped", "interrupted", st.fix)
                return "stop"
            except Exception as e:  # the step's own error: that step's failure, never the end of the setup
                fail = _Fail(f"{type(e).__name__}: {e}")
            if fail is None:
                run.set(st.sid, "done", detail=tui.clean(cl.items[i].detail), **self.extra.get(st.sid, {}))
                return "next"
            cl.finish(i, "fail", "failed", self._fail_lines(st, fail))
            run.set(st.sid, "failed", tui.clean(fail.reason), st.fix, means=fail.means or st.means, crash=fail.crash)
            return "next"

    def _fail_lines(self, st, fail) -> list:
        """W-9: the reason (one row, cut with …), what it means, the fix, and the bug report of a devkit bug."""
        width = self.term.size()[0]
        out = [tui.dim(f"Reason: {tui.clean(fail.reason)}")]
        for text in (f"This means: {fail.means or st.means}",
                     f"Fix: {st.fix}" + (" tries this step again." if st.fix == "dev setup --resume" else "")):
            parts = tui.wrap(text, width - 9)
            out += [tui.dim(parts[0])] + [tui.dim("  " + x) for x in parts[1:]]
        if fail.crash:
            out += [tui.dim(x) for x in _path_lines("Bug report saved: ", fail.crash, width - 7)]
        return out

    def _interrupted(self, cl, i, st) -> str:
        """W-10: Ctrl-C stopped this step -> "skip", "again" or "stop". A required step cannot be skipped; a second
        Ctrl-C stops the setup. The line front end has no menu: an optional step is skipped, a required one stops."""
        if self.lines:
            return "stop" if st.required else "skip"
        cl.pause()
        detail = tui.clean(cl.items[i].detail)
        above = [f"  {tui.warn('!')} {tui.pad(st.title, cl.label_width())}  {tui.dim('stopped' + (' · ' + detail if detail else ''))}", ""]
        opts = ([] if st.required else [tui.Option("skip", "Skip it and go on",
                                                   desc=f"The rest of the setup runs. {st.fix} finishes it later.")])
        opts += [tui.Option("again", "Try it again", desc="Starts this step again." + (
                     " What is already installed is kept." if st.sid in ("brew", "npm") else "")),
                 tui.Option("stop", "Stop the setup here",
                            desc="Your progress is saved. dev setup --resume continues from here.")]
        prompt = tui.Select(f'You stopped "{st.title}".', opts, help=REQUIRED_HINT if st.required else "",
                            back=False, hints=["↑↓ move", "enter choose", "ctrl-c stop the setup"])
        try:
            return tui.ask_block(self.term, prompt, above)
        except (tui.Cancelled, KeyboardInterrupt):
            return "stop"
        finally:
            cl.resume()

    # ---------------------------------------------------------------- stage 4: the sign-ins, the lock, the check
    def _stage4(self, steps, run) -> bool:
        signins = [s for s in steps if s.sid.startswith("signin-")]
        if signins:
            self._sign_ins(signins, run)
        for st in steps:
            if st.sid.startswith("signin-"):
                continue
            if st.sid == "lock":
                self._lock_step(st, run)
                continue
            cl = self._checklist([st.title])
            with self._quiet(), cl:
                try:
                    if self._one(cl, 0, st, run) == "stop":
                        return True
                except (KeyboardInterrupt, tui.Cancelled):
                    run.set(st.sid, "skipped", "interrupted", st.fix)
                    return True
        return False

    def _status(self) -> dict:
        from . import providers
        try:
            return providers.status(include_account=False)
        except Exception:  # an agent's status that cannot be read counts as not signed in
            return {}

    def _signed_in(self, k) -> bool:
        return bool((self._status().get(k) or {}).get("logged_in"))

    def can_ask(self) -> bool:
        """Whether a sign-in or a menu can ask the person: a terminal to run the sign-in on, and, in the line front
        end, a terminal to read the answer from."""
        if not terminal_device():
            return False
        if self.lines:
            try:
                return sys.stdin.isatty()
            except (AttributeError, ValueError):
                return False
        return True

    def _say(self, state, label, detail=""):
        """One line of stage 4, in the checklist's columns."""
        mark = tui.Checklist.MARKS[state]()
        self.term.write(tui.fit(f"  {mark} {tui.pad(label, 30)} {tui.dim(detail)}".rstrip(), self.term.size()[0] - 1)
                        + "\n")

    def _sign_ins(self, steps, run):
        """W-11 to W-14: each chosen agent that is installed and not signed in gets its panel; one signed in shows its
        line; one whose install did not finish waits for it. Esc on a panel goes back to the previous agent's."""
        status, ask = self._status(), self.can_ask()
        panels = []
        for st in steps:
            k = st.sid.split("-", 1)[1]
            x = status.get(k) or {}
            need = self._blocked(st, run)
            if run.state(st.sid) == "done" and x.get("logged_in"):
                continue
            if x.get("logged_in"):
                continue
            if need is not None or not x.get("installed") or not ask:
                continue
            panels.append(st)
        heading = "Now you: sign in" if panels else "Sign-ins"
        self.term.write(f"\n  {tui.bold(heading)}" + (f" {tui.dim('· about 2 minutes')}" if panels else "") + "\n\n")
        for st in steps:
            k = st.sid.split("-", 1)[1]
            x, name = status.get(k) or {}, AGENT_NAMES[k]
            need = self._blocked(st, run)
            if x.get("logged_in"):
                detail = _signed(x)
                run.set(st.sid, "done", detail=detail, plan=_plan(x))
                self._say("ok", name, detail)
            elif need is not None:
                why = f"waits for {need.title}"
                run.set(st.sid, "skipped", why, st.fix)
                self._say("skip", name, why)
            elif not x.get("installed"):
                why = f"{name} is not installed"
                run.set(st.sid, "skipped", why, st.fix)
                self._say("skip", name, why)
            elif not ask:
                run.set(st.sid, "skipped", NO_TERMINAL, st.fix)
                self._say("skip", name, NO_TERMINAL)
        idx = 0
        while idx < len(panels):
            st = panels[idx]
            prev = panels[idx - 1] if idx else None
            back = prev.sid.split("-", 1)[1] if prev is not None and run.state(prev.sid) != "done" else None
            if self._panel_flow(st, run, idx + 1, len(panels), back) is tui.BACK:
                idx -= 1
                continue
            idx += 1

    def _panel_flow(self, st, run, number=1, total=1, back=None):
        """One agent's panel until it is signed in or skipped -> BACK when Esc asks for the previous agent."""
        k = st.sid.split("-", 1)[1]
        name, reason, way = AGENT_NAMES[k], None, None
        while True:
            prompt = self._panel(k, reason, way, number, total, back)
            line = (f"     Sign in to {name}: " if reason is None else
                    f"     {name} is not signed in ({reason.rstrip('.')}). ")
            try:
                choice = self._choose(prompt, line)
            except (tui.Cancelled, KeyboardInterrupt):
                choice = "skip"
            if choice is tui.BACK:
                return tui.BACK
            if choice == "skip":
                run.set(st.sid, "skipped", SKIPPED_BY_CHOICE, st.fix)
                self._say("skip", name, f"skipped · sign in later with {st.fix}")
                return None
            way = way if choice == "again" else choice
            got = self._sign_in_with(k, way)
            x = self._status().get(k) or {}
            if x.get("logged_in"):
                detail = _signed(x)
                run.set(st.sid, "done", detail=detail, plan=_plan(x))
                self._say("ok", name, detail)
                return None
            reason = self._sign_in_reason(k, way, got)
            if self.log:
                self.log.write(f"\n{name} sign-in ({way}): {got} {getattr(got, 'error', '')}\n")

    def _panel(self, k, reason=None, way=None, number=1, total=1, back=None):
        """W-11 and W-12 (the ways to sign in, Skip for now; never Yes or No), and W-14 after a sign-in that did not
        end signed in: the reason, Try again, the other way, Skip for now."""
        name = AGENT_NAMES[k]
        later = " ".join(SIGN_IN[k])
        common = dict(back=bool(back), back_hint=f"esc back to {AGENT_NAMES[back]}" if back else "esc back",
                      badge_right=True, hints=["↑↓ move", "enter choose"],
                      corner=f"{number} of {total}" if total > 1 else "")
        if reason is None:
            remote = k == "codex" and bool(os.environ.get("SSH_CONNECTION"))
            ways = ["code", "browser"] if remote else list(SIGN_IN_WAYS[k])
            opts = [tui.Option(w, WAY_WORDS[w][0], desc=WAY_DESC[(k, w)], badge="recommended" if n == 0 else "")
                    for n, w in enumerate(ways)]
            opts.append(tui.Option("skip", "Skip for now", desc=f"devkit stays set up. {name} works after you sign in "
                                                               f"later."))
            help_ = [PANEL_NEEDS[k]] + (["You are connected to this Mac remotely, so its browser is not in front of "
                                         "you."] if remote else [])
            return tui.Select(f"Sign in to {name}", opts, help=help_, **common)
        other = ({"browser": "code", "code": "browser"} if k == "codex" else
                 {"browser": "sso"}).get(way, "browser")
        opts = [tui.Option("again", "Try again", desc=AGAIN_DESC.get(way, "Starts the sign-in again.")),
                tui.Option(other, WAY_WORDS[other][0], desc=OTHER_DESC[(k, other)]),
                tui.Option("skip", "Skip for now", desc=f"Sign in later with {later}.")]
        return tui.Select(f"{name} is not signed in yet", opts, help=[f"Reason: {reason}"], mark=tui.warn("!"),
                          **common)

    def _choose(self, prompt, line=""):
        """Ask a menu: inline under the screen; in the line front end, as one line of numbered choices."""
        if not self.lines:
            return tui.ask_block(self.term, prompt)
        opts = prompt.options
        shorts = [WAY_WORDS[o.value][1] if o.value in WAY_WORDS else o.label[:1].lower() + o.label[1:] for o in opts]
        ask = (line or f"     {prompt.title}: ") + ", ".join(f"{n} {s}" for n, s in enumerate(shorts, 1)) \
            + f" [{prompt.i + 1}] "
        a = input(ask).strip()
        if a.isdigit() and 1 <= int(a) <= len(opts):
            return opts[int(a) - 1].value
        return prompt.value

    def _waiting_lines(self, k, way) -> list:
        """W-13: what happens next, above the agent's own lines."""
        width, name = self.term.size()[0], AGENT_NAMES[k]
        if way == "code":
            text = ("Open the link below on any device, sign in, and enter the code shown. This waits up to 15 "
                    "minutes; Ctrl+C stops it and lets you choose again.")
        elif k == "claude":
            page = {"browser": "the claude.ai sign-in page", "sso": "your company's sign-in page",
                    "console": "the Anthropic Console sign-in page"}.get(way, "the sign-in page")
            text = (f"Your browser should show {page} now. If it did not open, Claude Code prints a link below: open "
                    "it on any device, and if it gives you a code, paste it here. This waits up to 10 minutes; "
                    "Ctrl+C stops it and lets you choose again.")
        else:
            text = ("Your browser should show the ChatGPT sign-in page now. If it did not open, open the link below "
                    "on this Mac. This waits up to 10 minutes; Ctrl+C stops it and lets you choose again.")
        return ["", "  " + tui.bold(f"Signing in to {name}")] + ["  " + x for x in tui.wrap(text, width - 5)] + \
            ["  " + tui.dim("─" * max(10, width - 5))]

    def _sign_in_with(self, k, way):
        self.term.write("\n".join(self._waiting_lines(k, way)) + "\n")
        timeout = DEVICE_TIMEOUT if way == "code" else SIGN_IN_TIMEOUT
        ctx = contextlib.nullcontext() if self.lines else self.term.suspended()
        with ctx:
            return _attached(SIGN_IN_WAYS[k][way], timeout)

    @staticmethod
    def _sign_in_reason(k, way, got) -> str:
        """W-14: why the agent is still not signed in, read from how its command ended and its own status."""
        name = AGENT_NAMES[k]
        err = getattr(got, "error", "")
        if got == "interrupted":
            return "you stopped the sign-in."
        if got == "timeout":
            return f"the sign-in page was not finished within {15 if way == 'code' else 10} minutes."
        if got == "ok":
            return f"{name} finished, but it still reports that you are not signed in."
        if got == "crashed":
            return f"{name} closed unexpectedly{': ' + err if err else ''}. The full output is in the log."
        if got == "missing":
            return f"{name} is not installed."
        if got == "no-terminal":
            return "there is no terminal to sign in from."
        return f"{name} stopped with an error: {err}" if err else f"{name} stopped with an error."

    def _lock_step(self, st, run):
        """W-15: the admin password for the settings lock, asked at the end; three wrong passwords leave it unlocked,
        never a failure of the setup."""
        need = self._blocked(st, run)
        if need is not None:
            run.set(st.sid, "skipped", f"waits for {need.title}", st.fix)
            self._say("skip", st.title, f"waits for {need.title}")
            return
        if run.state(st.sid) == "done":
            self._say("ok", st.title, "done earlier")
            return
        if not self.can_ask():
            run.set(st.sid, "skipped", NO_TERMINAL, st.fix)
            self._say("skip", st.title, NO_TERMINAL)
            return
        prompt = tui.Select("Lock Claude Code settings", [
            tui.Option("now", "Enter my password now"),
            tui.Option("skip", "Skip for now", desc="devkit still finds and repairs changed settings. Lock later with "
                                                    "dev config lock.")],
            help="macOS asks for the password you use to log in to this Mac. Nothing appears while you type it; that "
                 "is normal.", back=False)
        try:
            choice = self._choose(prompt, "     Lock Claude Code settings: ")
        except (tui.Cancelled, KeyboardInterrupt):
            choice = "skip"
        if choice == "skip":
            run.set(st.sid, "skipped", SKIPPED_BY_CHOICE, st.fix)
            self._say("skip", st.title, "skipped · lock later with dev config lock")
            return
        ctx = contextlib.nullcontext() if self.lines else self.term.suspended()
        with ctx:
            got = _attached(_dev("config", "lock"), SIGN_IN_TIMEOUT)
        if got == "ok":
            run.set(st.sid, "done", detail="managed settings written")
            self._say("ok", st.title, "managed settings written")
        else:
            run.set(st.sid, "skipped", "not locked", st.fix)
            self._say("warn", st.title, "not locked · dev config lock")

    # ---------------------------------------------------------------- the steps
    def _package_plan(self) -> dict:
        from . import toolchain
        a = self.a
        groups = set(BASE_GROUPS) | set(a["stacks"]) | set(toolchain.groups({"hosts": a["hosts"], "clouds": a["clouds"]}))
        return {"groups": groups, "brew": filtered(DEVKIT_HOME / "Brewfile", groups), "npm": _npm_globals(groups),
                "post": [k for k, v in toolchain.ALL.items() if v[4] in groups]}

    def _bundle_ok(self, plan) -> bool:
        """Continue's quick check of the Homebrew packages: `brew bundle check`, nothing installed."""
        with _brewfile(plan["brew"]) as bf:
            return _exec(["brew", "bundle", "check", f"--file={bf}", "--no-upgrade"], None, self.log,
                         env=_child_env(), timeout=300) == 0

    @staticmethod
    def _npm_ok(plan) -> bool:
        root = _npm_root()
        return root is None or all((root / n).exists() for n in plan["npm"])

    def _save(self, cl, i):
        cl.start(i)
        new = build_config(self.d["config"], self.a, self.d["gh"]["login"])
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n")
        self.saved_file = True
        cl.finish(i, "ok", str(core.tilde(CONFIG_FILE)))
        return None

    def _brew(self, cl, i, plan):
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
            rc = _exec(["brew", "bundle", f"--file={bf}", "--no-upgrade"], line, self.log, env=_child_env())
        notes = [tui.clean(x) for x in toolchain.postinstall(plan["post"])]
        if rc == 0:
            self.extra["brew"] = {"count": len(todo)}
            cl.finish(i, "ok", f"{len(todo)} installed" if todo else "all installed", notes)
            return None
        return _Fail(_reason(rc, tail, "brew bundle"),
                     means=f"Some tools are missing: {', '.join(failed)}." if failed else "")

    def _npm(self, cl, i, plan):
        npm, tail = plan["npm"], deque(maxlen=20)
        cl.start(i, ", ".join(npm))
        rc = _exec(["npm", "install", "-g", "--no-fund", "--no-audit", *npm],
                   lambda s: tail.append(tui.clean(s)) if tui.clean(s) else None, self.log, env=_child_env())
        if "@playwright/cli" in npm and core.have("playwright-cli"):
            cl.update("Playwright CLI skill")
            install_playwright_skill()
        if rc == 0:
            self.extra["npm"] = {"count": len(npm)}
            cl.finish(i, "ok", f"{len(npm)} package{'s' if len(npm) != 1 else ''}")
            return None
        return _Fail(_reason(rc, tail, "npm"))

    def _install_agent(self, cl, i, k):
        tail = deque(maxlen=20)
        argv = ["/bin/bash", "-c", CLAUDE_INSTALLER] if k == "claude" else ["brew", "install", "--cask", "codex"]
        cl.start(i, "official installer" if k == "claude" else "Homebrew")
        rc = _exec(argv, lambda s: tail.append(tui.clean(s)) if tui.clean(s) else None, self.log, env=_child_env())
        if rc == 0:
            cl.finish(i, "ok", "installed")
            return None
        return _Fail(_reason(rc, tail, "Claude Code's installer" if k == "claude" else "brew install"))

    def _configure(self, cl, i, log=None):
        """`dev install`. Its ✗ lines are problems in parts it went on from (dev doctor checks them again); an exit
        code other than 0 means it stopped: the reason is its crash, its error line, or what it last wrote."""
        parts, fails, after = [], [], deque(maxlen=20)
        cl.start(i, "dev install")

        def line(s):
            s = tui.clean(s)
            if not s:
                return
            if s.startswith("▸ "):
                parts.append(s[2:])
                cl.update(s[2:])
                return
            if parts:  # what came after the first part's header: never the banner
                after.append(s)
            if s.startswith("✗ "):
                fails.append(s[2:])
        rc = _exec(_dev("install", *_template_args(self.a)), line, log if log is not None else self.log,
                   env=_child_env())
        if rc not in (0, None):
            if int(rc) == CRASH_EXIT and getattr(rc, "crash", None):
                reason, crash = _crash_reason(rc, parts[-1] if parts else "")
                return _Fail(reason, crash=crash)
            return _Fail(_reason(rc, list(after), "dev install"))
        if fails:
            cl.finish(i, "warn", f"{len(fails)} problem{'s' if len(fails) != 1 else ''}; dev doctor checks them",
                      [tui.bad("✗ ") + f for f in fails[:4]])
        else:
            cl.finish(i, "ok", "rules, guard, hooks and skills")
        return None

    def _docker(self, cl, i):
        cl.start(i, "Colima")
        dk = detect_docker()
        if dk["running"]:
            cl.finish(i, "ok", "Colima is already running")
            return None
        if not dk["colima"]:
            return _Fail("Colima is not installed.")
        cl.update(f"{self.a['cpu']} CPUs · {self.a['mem']} GB")
        tail = deque(maxlen=20)
        rc = _exec(_dev("docker", "up"), lambda s: tail.append(tui.clean(s)) if tui.clean(s) else None, self.log,
                   env=_child_env(), timeout=DOCKER_TIMEOUT)
        if rc == 0:
            cl.finish(i, "ok", f"Colima · {self.a['cpu']} CPUs · {self.a['mem']} GB")
            return None
        return _Fail(_reason(rc, tail, "dev docker up"))

    def _doctor(self, cl, i, log=None, report=None):
        res = report if report is not None else {}
        res.update({"passed": None, "total": None, "fails": [], "rc": 1})
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
                res["fails"].append(m.group(2))
        rc = _exec(_dev("doctor"), line, log if log is not None else self.log, env=_child_env())
        res["rc"] = int(rc)
        if res["total"] is None:
            return _Fail(_reason(rc, tail, "dev doctor"))
        n = len(res["fails"])
        self.extra["doctor"] = {"failing": n}
        if not n:
            cl.finish(i, "ok", f"all {res['total']} checks pass" if res["total"] != 1 else "the check passes")
        else:
            cl.finish(i, "warn", f"{res['passed']} of {res['total']} checks pass")
        return None

    # ---------------------------------------------------------------- the summary
    def _finish(self, run, paused) -> int:
        """W-16 and its menu (sign in now, try the failed steps again), redrawn after each -> the exit code."""
        while True:
            lines, menu = summary_lines(run, self.term.size()[0], paused, self.log.path if self.log else None,
                                        can_sign_in=self.can_ask())
            self.term.write("\n" + "\n".join(lines) + "\n\n")
            if paused or not menu or not self.can_ask():
                break
            prompt = tui.Select("What now?", [tui.Option(v, label, desc=desc) for v, label, desc in menu], back=False)
            try:
                choice = self._choose(prompt, "     What now? ")
            except (tui.Cancelled, KeyboardInterrupt):
                choice = "finish"
            if choice == "finish":
                break
            if choice.startswith("signin:"):
                st = self.by_id.get("signin-" + choice.split(":", 1)[1])
                if st:
                    self._panel_flow(st, run)
                continue
            failed = {s["id"] for s in run.steps if s.get("state") == "failed"}
            waiting = {s["id"] for s in run.steps if s.get("state") == "skipped"
                       and str(s.get("reason", "")).startswith("waits for ")}
            for sid in failed | waiting:
                run.set(sid, "pending")
            self.log = self.log or _Log()
            paused = self._run_stages(run, only=failed | waiting | {"doctor"})
        if self.log:
            self.log.close()
        return {"paused": 130, "not-ready": 1}.get(run.outcome(), 0)


# --------------------------------------------------------------------------- the summary (W-16), shared by both ends

CLAUDE_SHORT = {"bypassPermissions": "no prompts", "acceptEdits": "asks before commands", "default": "asks for everything"}
CODEX_SHORT = {"full": "no prompts", "workspace": "sandboxed"}
WAY_DESC = {("claude", "browser"): "Your browser opens claude.ai. Sign in there, then come back here.",
            ("claude", "sso"): "For Claude Team or Enterprise accounts that sign in through your company.",
            ("claude", "console"): "For API billing instead of a Claude plan.",
            ("codex", "browser"): "Your browser opens a ChatGPT page. Sign in there, then come back here.",
            ("codex", "code"): "For when the browser cannot open on this Mac: you get a link and a code to enter on "
                               "any device."}
AGAIN_DESC = {"browser": "Opens the browser again.", "code": "Shows a new link and code.",
              "sso": "Opens your company's sign-in page again.", "console": "Opens the Console sign-in again."}
OTHER_DESC = {("codex", "code"): "For when the browser cannot open on this Mac.",
              ("codex", "browser"): "Your browser opens a ChatGPT page.",
              ("claude", "sso"): "For Claude Team or Enterprise accounts that sign in through your company.",
              ("claude", "browser"): "Your browser opens claude.ai."}


def _arch(arch) -> str:
    return {"arm64": "Apple silicon", "x86_64": "Intel"}.get(tui.clean(arch), tui.clean(arch))


def _plan(x) -> str:
    """An agent's plan as people say it: "max" -> "Max"."""
    p = tui.clean(x.get("plan") or "")
    return p[:1].upper() + p[1:] if p else ""


def _signed(x) -> str:
    return "signed in" + (f" · plan {_plan(x)}" if _plan(x) else "")


def _wrapped(line: str, width: int) -> list:
    """A line of a prompt's footer, wrapped to the width with its indent kept."""
    if not line.strip():
        return [line]
    lead = len(line) - len(line.lstrip(" "))
    return [" " * lead + x for x in tui.wrap(line.strip(), width - lead)]


def _wrap_words(text: str, width: int) -> list:
    """Text wrapped at spaces only: a name with a hyphen (ana-dev/handoff) or a long path stays whole."""
    if tui.vlen(text) <= width:
        return [text]
    return textwrap.wrap(text, max(10, width), break_on_hyphens=False, break_long_words=False) or [text]


def _path_lines(prefix: str, path: str, width: int) -> list:
    """A path after `prefix` on one line when it fits; else broken after a slash, the file name on a line of its own,
    never cut."""
    if len(prefix) + len(path) <= width:
        return [prefix + path]
    folder, _, name = path.rpartition("/")
    lines, cur = [], prefix
    for part in (folder + "/").split("/")[:-1]:
        piece = part + "/"
        if len(cur) + len(piece) > width and cur.strip():
            lines.append(cur.rstrip())
            cur = "  "
        cur += piece
    return lines + [cur.rstrip(), f"  {name}"]


def _needs_lines(width: int) -> list:
    """W-3's "What each one needs", under the options: one clause a line when they fit, else wrapped."""
    out = ["", "  What each one needs"]
    room = width - 17
    for k in ("claude", "codex"):
        parts = AGENT_NEEDS[k]
        rows = list(parts) if all(len(p) <= room for p in parts) else tui.wrap(" ".join(parts), room)
        out.append(f"    {tui.pad(AGENT_NAMES[k], 11)}  {rows[0]}")
        out += [" " * 17 + x for x in rows[1:]]
    return out + ["", "  " + tui.dim("You can change this later with dev setup.")]


def _mark(s) -> str:
    state = s.get("state")
    return {"done": tui.ok("✓"), "failed": tui.bad("✗"), "skipped": tui.dim("–"), "running": tui.warn("!")}.get(
        state, tui.dim("○"))


def _why(s) -> str:
    """Why a step is not done, in the person's words."""
    reason = tui.clean(s.get("reason") or "")
    if s.get("state") == "pending":
        return ""
    if reason == "interrupted":
        return "stopped by you" if s.get("state") == "failed" else "skipped by you"
    return reason


def _row(mark, title, detail, width=80) -> str:
    return f"  {mark} {tui.pad(title, 30)} {tui.dim(detail) if detail else ''}".rstrip()


def _rows(mark, title, detail, width, narrow, cut=False) -> list:
    """One summary item: name and detail on one line; at narrow widths, the detail under the name."""
    if narrow:
        out = [f"  {mark} {title}"]
        if detail:
            out += [tui.fit("      " + detail, width - 1)] if cut else \
                ["      " + x for x in tui.wrap(detail, width - 7)]
        return out
    line = _row(mark, title, detail)
    return [tui.fit(line, width - 1) if cut else line]


def _done_rows(run) -> list:
    """(title, detail) for what the run did, merged the way the person thinks of it."""
    by = {s["id"]: s for s in run.steps}
    done = {k for k, s in by.items() if s.get("state") == "done"}
    rows = []
    if "save" in done:
        rows.append(("Saved your choices", by["save"].get("detail") or str(core.tilde(CONFIG_FILE))))
    pk = [x for x in ("brew", "npm") if x in done]
    if pk:
        n = sum(int(by[x].get("count") or 0) for x in pk)
        where = " and ".join({"brew": "Homebrew", "npm": "npm"}[x] for x in pk)
        rows.append((f"Installed {n} package{'s' if n != 1 else ''}", where) if n else ("Packages", "all installed"))
    inst = [k for k in ("claude", "codex") if k in done]
    if inst:
        rows.append(("Installed " + " and ".join(AGENT_NAMES[k] for k in inst), ""))
    if "configure" in done:
        rows.append(("Set up devkit", by["configure"].get("detail") or "rules, guard, hooks and skills"))
    chosen = [k for k in ("claude", "codex") if f"signin-{k}" in by]
    signed = [k for k in chosen if f"signin-{k}" in done]
    if signed and len(signed) == len(chosen):
        rows.append(("Signed in", ", ".join(AGENT_NAMES[k] + (f" ({by['signin-' + k]['plan']})"
                                                             if by["signin-" + k].get("plan") else "") for k in signed)))
    else:
        for k in signed:
            plan = by["signin-" + k].get("plan")
            rows.append((f"Signed in to {AGENT_NAMES[k]}", f"plan {plan}" if plan else ""))
    if "docker" in done:
        rows.append(("Started Docker", by["docker"].get("detail") or "Colima"))
    if "lock" in done:
        rows.append(("Locked Claude Code settings", ""))
    if "doctor" in done:
        rows.append(("Checked the setup", by["doctor"].get("detail") or ""))
    return rows


def summary_lines(run, width, paused=False, log=None, can_sign_in=True, installer=None) -> tuple:
    """W-16: the headline for the outcome, what is done, what is left with why, the command for each item left, and
    the log -> (lines, the menu's [(value, label, description)] or None)."""
    narrow = width < 72
    installer = local_installer() if installer is None else installer
    log_line = f"The full log of this setup: {log if isinstance(log, str) else core.tilde(log or STATE / 'logs' / 'setup.log')}"
    steps = run.steps
    if paused or run.outcome() == "paused":
        n = sum(1 for s in steps if s.get("state") == "done")
        out = [f"  {tui.warn('!')} {tui.bold(f'Setup paused. {n} of {len(steps)} steps are done.')}",
               "  Your answers and progress are saved.", "",
               "  To continue, open a new Terminal window and run:", "    dev setup --resume"]
        if installer:
            out += ["  or run the installer again:"] + ["    " + x for x in _path_lines("", installer, width - 5)]
        return out, None
    by_title = {s["title"]: s for s in steps}
    failed_required = [s for s in steps if s.get("required") and s.get("state") != "done"]
    blocked = {s["title"] for s in failed_required}

    def waits_on_required(s):
        r = str(s.get("reason") or "")
        return s.get("state") == "skipped" and r.startswith("waits for ") and r[len("waits for "):] in blocked
    needs = failed_required + [s for s in steps if waits_on_required(s)]
    rank = {"skipped": 0, "failed": 1}
    left = sorted((s for s in steps if s.get("state") != "done" and s not in needs),
                  key=lambda s: rank.get(s.get("state"), 2))
    doctor = next((s for s in steps if s["id"] == "doctor"), {})
    if doctor.get("state") == "done" and doctor.get("failing") and not left and not needs:
        left = [dict(doctor, state="attention", title="Check the setup",
                     reason=f"{doctor['failing']} check{'s' if doctor['failing'] != 1 else ''} need attention")]
    done = _done_rows(run)

    def item(s):
        mark = tui.warn("!") if s.get("state") == "attention" else _mark(s)
        return _rows(mark, s["title"], _why(s) if s.get("state") != "attention" else s["reason"], width, narrow,
                     cut=s.get("state") == "failed")

    def done_block(head=True):
        out = (["  " + tui.bold("Done")] if head else [])
        for title, detail in done:
            out += _rows(tui.ok("✓"), title, detail, width, narrow)
        return out
    out = []
    if failed_required:
        n = len(failed_required)
        out += [f"  {tui.bad('✗')} {tui.bold(f'devkit is not ready yet. {n} step' + ('s' if n != 1 else '') + ' failed.')}", ""]
        out += ["  " + tui.bold("Needs attention")] + [ln for s in needs for ln in item(s)]
        if done:
            out += [""] + done_block()
        if left:
            out += ["", "  " + tui.bold("Left for later")] + [ln for s in left for ln in item(s)]
    elif left:
        n = len(left)
        out += [f"  {tui.ok('✓')} {tui.bold('devkit is ready.')} "
                f"{n} thing{'s are' if n != 1 else ' is'} left for later.", ""]
        if done:
            out += done_block() + [""]
        out += ["  " + tui.bold("Left for later")] + [ln for s in left for ln in item(s)]
    else:
        agents = [k for k in ("claude", "codex") if f"signin-{k}" in {s["id"] for s in steps}] or ["claude", "codex"]
        out += [f"  {tui.ok('✓')} {tui.bold('devkit is ready.')}", ""] + done_block(head=False)
        out += ["", "  " + tui.bold("Start working")]
        for n, step in enumerate(("Open a new Terminal window, so the dev command is loaded.",
                                  f"dev new my-app, then dcd my-app, then {' or '.join(agents)}.",
                                  "dev dashboard shows your projects and tasks in the browser."), 1):
            parts = _wrap_words(step, width - 6)
            out += [f"  {n}. {parts[0]}"] + [f"     {x}" for x in parts[1:]]
        out += [""]
        out += [ln for ln in _wrapped("  " + log_line, width - 1)]
        return [tui.fit(x, width - 1) for x in out], None
    commands = []
    for s in (needs if failed_required else []) + left:
        if waits_on_required(s):
            continue
        fix = s.get("fix") or (by_title.get(s["title"], {}).get("fix"))
        st_does = SUMMARY_DOES.get(s["id"], "")
        if s["id"] in ("signin-claude", "signin-codex"):
            st_does = f"signs you in to {AGENT_NAMES[s['id'].split('-', 1)[1]]}"
        if fix == "dev setup --resume":
            st_does = f"tries {s['title']} again, then the rest"
        if fix and fix not in [c for c, _ in commands]:
            commands.append((fix, st_does))
    out += ["", "  To finish, open a new Terminal window, then run:"]
    cw = 30 if not narrow else max(len(c) for c, _ in commands) + 5 if commands else 10
    for cmd, does in commands:
        if len(cmd) <= cw and 5 + cw + len(does) <= width - 1:
            out.append(f"    {tui.pad(cmd, cw)} {tui.dim(does)}".rstrip())
        elif len(cmd) <= cw:  # narrow: what the command does goes under it
            out += [f"    {cmd}"] + ["      " + tui.dim(x) for x in tui.wrap(does, width - 7)] if does else [f"    {cmd}"]
        else:
            out += [f"    {cmd}"] + ([f"    {' ' * min(cw, 8)} {tui.dim(does)}"] if does else [])
        if cmd == "codex login" and any(s.get("reason") == NO_TERMINAL for s in left):
            out.append(f"    {' ' * cw} {tui.dim('(or: codex login --device-auth, with a code)')}")
    crash = next((s.get("crash") for s in needs + left if s.get("crash")), "")
    if crash:
        out += _wrapped("  If it fails the same way, send this file to your devkit contact:", width - 1)
        out += ["    " + x for x in _path_lines("", crash, width - 5)]
    out += [""] + _wrapped("  " + log_line, width - 1)
    menu = []
    signable = [s for s in left if s["id"].startswith("signin-") and s.get("state") == "skipped"
                and not str(s.get("reason", "")).startswith("waits for ") and can_sign_in]
    retry = [s for s in steps if s.get("state") == "failed"]
    if failed_required:
        menu.append(("retry", f"Try {failed_required[0]['title']} again", ""))
    menu.append(("finish", "Finish", "Close the setup. The commands above work at any time."))
    menu += [(f"signin:{s['id'].split('-', 1)[1]}", f"Sign in to {AGENT_NAMES[s['id'].split('-', 1)[1]]} now", "")
             for s in signable]
    if not failed_required and retry:
        menu.append(("retry", f"Try {retry[0]['title']} again" if len(retry) == 1 else "Try the failed steps again",
                     ""))
    return [tui.fit(x, width - 1) if "Reason" in x else x for x in out], (menu if len(menu) > 1 else None)


SUMMARY_DOES = {"brew": "installs the missing tools", "npm": "installs the npm packages", "claude": "installs Claude Code",
                "codex": "installs Codex", "configure": "sets up devkit", "docker": "starts Docker",
                "lock": "locks Claude Code settings", "doctor": "shows what needs attention"}


# --------------------------------------------------------------------------- dev setup --preview

def _preview_run(states: dict, extra=None) -> SetupRun:
    """A run with these step states, for the preview's summaries."""
    titles = {"save": "Save your choices", "brew": "Install Homebrew packages", "npm": "Install npm packages",
              "claude": "Install Claude Code", "codex": "Install Codex", "configure": "Set up devkit",
              "docker": "Start Docker", "signin-claude": "Sign in to Claude Code", "signin-codex": "Sign in to Codex",
              "lock": "Lock Claude Code settings", "doctor": "Check the setup"}
    fixes = {"brew": "dev install --only tools", "npm": "npm install -g pyright", "claude": CLAUDE_INSTALLER,
             "codex": "brew install --cask codex", "configure": "dev setup --resume", "docker": "dev docker up",
             "signin-claude": "claude auth login", "signin-codex": "codex login", "lock": "dev config lock",
             "doctor": "dev doctor", "save": "dev setup --resume"}
    stamp = time.mktime((2026, 11, 9, 10, 42, 0, 0, 0, -1))
    steps = []
    for sid, state in states.items():
        s = {"id": sid, "title": titles[sid], "required": sid in ("save", "claude", "codex", "configure"),
             "state": state, "reason": "", "fix": fixes[sid] if state != "done" else ""}
        s.update((extra or {}).get(sid, {}))
        steps.append(s)
    return SetupRun({"version": SetupRun.VERSION, "started": stamp - 600, "updated": stamp, "answers": {},
                     "skip_packages": False, "steps": steps})


class _PreviewTerm(tui.Terminal):
    def __init__(self, width):
        import io
        super().__init__(fin=io.StringIO(), fout=io.StringIO(), reader=tui.KeyReader.from_bytes(b""))
        self.width = width

    def size(self):
        return self.width, 200


def preview(width=None, color=None) -> dict:
    """`dev setup --preview`: every screen of the setup drawn with fixed sample data, at `width` columns ->
    {screen: text}. It runs nothing and writes nothing; the tests and the visual review read it."""
    width = width or shutil.get_terminal_size((80, 24)).columns
    style, ui_color = dict(vars(tui.STYLE)), (ui.COLOR, ui.TRUECOLOR)
    if color is not None:
        tui.set_color(color)
        ui.COLOR, ui.TRUECOLOR = bool(color), bool(color) and ui.TRUECOLOR
    try:
        return _preview(width)
    finally:
        tui.STYLE.__dict__.update(style)
        ui.COLOR, ui.TRUECOLOR = ui_color


def _preview(width) -> dict:
    term = _PreviewTerm(width)
    w = _Wizard(term)
    w.d = {"cpus": 10, "ram": 16, "macos": "26.2", "arch": "arm64", "brew": True,
           "gh": {"login": "ana-dev", "name": "Ana Dev"},
           "providers": {"claude": {"installed": False}, "codex": {"installed": False}}, "config": None,
           "seen": {"hosts": ["github"], "clouds": [], "evidence": {}}, "stacks_found": {"counts": {}, "repos": 0},
           "docker": {"colima": False, "running": False}}
    w.a = Answers(root="~/Development", handoff="ana-dev/handoff", use_claude=True, use_codex=True,
                  claude_mode="bypassPermissions", codex_mode="full", stacks=["python", "node"], hosts=["github"],
                  clouds=[], cpu=3, mem=4, disk=80, autostart=False, gc_auto=True, interval=60, lock=False, ai=False,
                  skill_groups={}, commit_template=True, start_docker=True)
    w._pk = {(tuple(w.a["stacks"]), tuple(w.a["hosts"]), tuple(w.a["clouds"])):
             [("core", [(f"tool{i}", "brew", False) for i in range(36)] + [(f"have{i}", "brew", True) for i in range(4)])]}
    w._agents_to_install = lambda: ["claude", "codex"]
    w.total, w.baseline = 3, Answers(w.a)

    def page(static, prompt=None):
        lines, _ = tui.Screen(term).compose(static, prompt)
        return lines

    def block(prompt, above=()):
        return [tui.fit(x, width - 1) for x in list(above) + prompt.lines(width - 1)]

    def checklist(items, footer=()):
        cl = tui.Checklist(term, [t for t, *_ in items], animate=False, footer=footer)
        now = time.time()
        for it, (_, state, detail, secs, notes) in zip(cl.items, items):
            it.state, it.detail, it.notes = state, detail, list(notes)
            if state != "pending":
                it.t0, it.t1 = now - secs, (None if state == "running" else now)
        return [tui.fit(x, width - 1) for x in cl.render(width)]

    head = ["", f"  {ui.logo_inline()}  {tui.dim('setup')}", "", f"  {tui.bold('Applying your plan')}",
            f"  {tui.dim('This runs on its own. Ctrl+C skips or stops a step.')}", ""]
    then = ["", "  " + tui.dim("Then you sign in to Claude Code and Codex.")]
    pending = [("Install npm packages", "pending", "", 0, ()), ("Install Claude Code", "pending", "", 0, ()),
               ("Install Codex", "pending", "", 0, ()), ("Set up devkit", "pending", "", 0, ()),
               ("Start Docker", "pending", "", 0, ())]
    configure = _Step("configure", "Set up devkit", True, None, "dev setup --resume", "",
                      "Claude Code and Codex do not have devkit's rules and safety guard yet.")
    fail = w._fail_lines(configure, _Fail("devkit hit a bug while pinning its copy: KeyError: 'version'",
                                          crash="~/.local/state/devkit/crashes/20261109-104207-1a2b3c4d5e6f.txt"))
    screens = {}
    screens["W-2"] = page(w.welcome, w.welcome_prompt())
    screens["W-3"] = page(w.frame(1, "Agents", total=3), w.agents_prompt())
    screens["W-6"] = page(w.frame(2, "Stacks", total=3), tui.Confirm(
        "Quit the setup?", True, help="Nothing has changed on this Mac. Your answers are not saved.", back=False,
        yes="Keep going", no="Quit", hints=["←→ switch", "enter confirm", "ctrl-c quit"]))
    screens["W-7"] = page(w.review, w.review_prompt())
    screens["W-8"] = head + checklist([("Save your choices", "ok", "~/.config/devkit/config.json", 0, ()),
                                       ("Install Homebrew packages", "running", "ripgrep · 12 of 36", 134, ())]
                                      + pending, then)
    screens["W-9"] = head + checklist([
        ("Save your choices", "ok", "~/.config/devkit/config.json", 0, ()),
        ("Install Homebrew packages", "ok", "36 installed", 242, ()), ("Install npm packages", "ok", "6 packages", 38, ()),
        ("Install Claude Code", "ok", "installed", 21, ()), ("Install Codex", "ok", "installed", 12, ()),
        ("Set up devkit", "fail", "failed", 41, fail), ("Start Docker", "running", "3 CPUs · 4 GB", 18, ())], then)
    menu = tui.Select('You stopped "Install Homebrew packages".', [
        tui.Option("skip", "Skip it and go on", desc="The rest of the setup runs. dev install --only tools finishes it "
                                                      "later."),
        tui.Option("again", "Try it again", desc="Starts this step again. What is already installed is kept."),
        tui.Option("stop", "Stop the setup here", desc="Your progress is saved. dev setup --resume continues from "
                                                       "here.")], back=False,
        hints=["↑↓ move", "enter choose", "ctrl-c stop the setup"])
    screens["W-10"] = checklist([("Save your choices", "ok", "~/.config/devkit/config.json", 0, ())]) + block(
        menu, [f"  {tui.warn('!')} {tui.pad('Install Homebrew packages', 25)}  "
               f"{tui.dim('stopped · 20 of 36 installed')}", ""])
    signin = ["", f"  {tui.bold('Now you: sign in')} {tui.dim('· about 2 minutes')}", ""]
    screens["W-11"] = signin + block(w._panel("claude", number=1, total=2))
    screens["W-12"] = block(w._panel("codex", number=2, total=2, back="claude"))
    screens["W-13"] = w._waiting_lines("codex", "browser") + [  # then Codex's own lines, as the terminal wraps them
        ln for x in ("  Starting local login server on http://localhost:1455.",
                     "  If your browser did not open, navigate to this URL to authenticate:",
                     "  https://auth.openai.com/oauth/authorize?…") for ln in _wrapped(x, width - 1)]
    screens["W-14"] = block(w._panel("codex", "you stopped the sign-in.", "browser", 2, 2, back="claude"))
    screens["W-15"] = block(tui.Select("Lock Claude Code settings", [
        tui.Option("now", "Enter my password now"),
        tui.Option("skip", "Skip for now", desc="devkit still finds and repairs changed settings. Lock later with "
                                                "dev config lock.")],
        help="macOS asks for the password you use to log in to this Mac. Nothing appears while you type it; that is "
             "normal.", back=False))
    log = "~/.local/state/devkit/logs/setup.log"
    base = {"save": {"detail": "~/.config/devkit/config.json"}, "brew": {"count": 36}, "npm": {"count": 6},
            "configure": {"detail": "rules, guard, hooks and skills"}, "signin-claude": {"plan": "Max"},
            "signin-codex": {"plan": "Business"}, "docker": {"detail": "Colima · 3 CPUs · 4 GB"},
            "doctor": {"detail": "all 58 checks pass"}}
    order = ("save", "brew", "npm", "claude", "codex", "configure", "docker", "signin-claude", "signin-codex",
             "doctor")
    ready = _preview_run({k: "done" for k in order}, base)
    left = _preview_run(dict({k: "done" for k in order}, docker="failed", **{"signin-codex": "skipped"}), dict(
        base, docker={"reason": "Colima did not start: the VM disk is locked"},
        **{"signin-codex": {"reason": SKIPPED_BY_CHOICE}, "doctor": {"detail": "56 of 58 checks pass"}}))
    broken = _preview_run(dict({k: "done" for k in order if k != "docker"}, configure="failed", lock="skipped"),
                          dict(base, configure={"reason": "devkit hit a bug while pinning its copy: KeyError: 'version'",
                                                "crash": "~/.local/state/devkit/crashes/20261109-104207-1a2b3c4d5e6f.txt"},
                               lock={"reason": "waits for Set up devkit"}))
    paused = _preview_run({"save": "done", "brew": "done", "npm": "skipped", "claude": "done", "codex": "done",
                           "configure": "pending", "signin-claude": "pending", "doctor": "pending"},
                          {"npm": {"reason": "interrupted"}, "brew": {"detail": "36 installed"}})
    for name, run in (("W-16 ready", ready), ("W-16 ready with items left", left), ("W-16 not ready", broken),
                      ("W-16 paused", paused)):
        lines, menu = summary_lines(run, width, log=log)
        screens[name] = lines
        if menu and name == "W-16 ready with items left":
            screens["W-16 menu"] = block(tui.Select("What now?", [tui.Option(v, lb, desc=d) for v, lb, d in menu],
                                                    back=False))
    screens["W-17"] = page(w.intro, w.resume_prompt(paused))
    screens["W-18"] = nothing_to_continue(ready, width)
    plain = not tui.STYLE.color
    return {k: "\n".join((tui.strip(x) if plain else x).rstrip() for x in v).strip("\n") for k, v in screens.items()}


def print_preview(width=None) -> int:
    """Print every screen of `preview()` under its name."""
    width = width or shutil.get_terminal_size((80, 24)).columns
    for name, text in preview(width).items():
        print(f"\n{tui.dim('── ' + name + ' ' + '─' * max(0, width - len(name) - 5))}\n\n{text}")
    print()
    return 0
