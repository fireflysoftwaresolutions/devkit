# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Shell completion for `dev` (lib/devkit/completion.py): the zsh and bash scripts are generated from dev's parser and
# checked in (shell/completions/_dev and dev.bash), and live values come from `dev __complete <kind>`.
#
# The zsh tests run zsh's real completion system: `zsh -f -i` in a pseudo-terminal (zsh/zpty), compinit, then
# shell/devkit.zsh as ~/.zshrc sources it; each case types a line and presses Tab, and compadd (the builtin every
# completer ends in) also prints what it adds. Live values come from bin/dev, run with this test's own home: a registry
# with a project on this Mac (a git repository with a worktree) and one that is not, work items, two live runs and a
# plan. The bash tests call the generated _dev with COMP_WORDS, as bash does on Tab.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_cli_completion
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

from devkit import cli, completion, core, setup  # noqa: E402

ZSH = shutil.which("zsh")
BASH = shutil.which("bash")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Za-z0-9]|\x1b[=>]|\r")

ITEM = """---
id: {id}
type: task
title: {title}
scope: acme/widget
status: {status}
bucket: {bucket}
priority: normal
created: {created}
updated: {created}
---
# {title}
"""

# The child shell: zsh's completion system, devkit.zsh, and a Tab key that marks where its output begins and ends.
INIT = r"""
PS1='%% ' RPS1=
unsetopt AUTO_LIST AUTO_MENU MENU_COMPLETE BEEP
autoload -Uz compinit && compinit -u -D
source {devkit_zsh}
_dev_wait=10
compadd() {{
  if [[ ${{@[1,(i)(-|--)]}} == *-(O|A|D)\ * ]]; then
    builtin compadd "$@"
    return
  fi
  local -a _hits _dscr
  local _i=${{@[(i)-d]}} _d
  if (( _i < $# )); then
    _d=${{@[_i+1]}}
    if [[ $_d == \(* ]]; then eval "_dscr=$_d"; else _dscr=("${{(@P)_d}}"); fi
  fi
  builtin compadd -A _hits -D _dscr "$@"
  for (( _i = 1; _i <= $#_hits; _i++ )); do print -r -- "DK-M:${{_hits[_i]}}|${{_dscr[_i]}}"; done
  builtin compadd "$@"
}}
_dk_tab() {{ print -r -- $'\nDK-BEGIN'; zle complete-word; print -r -- $'\nDK-END' }}
zle -N _dk_tab
bindkey '^I' _dk_tab
bindkey '^M' undefined-key
cd {cwd}
print DK-READY
"""

DRIVER = r"""
zmodload zsh/zpty || exit 3
zpty dk zsh -f -i
zpty -w dk "source ${(q)1}"
local out typed
zpty -r dk out '*DK-READY*' || exit 4
shift
for typed in "$@"; do
  zpty -w -n dk "$typed"$'\t'
  zpty -r dk out '*DK-END*' || exit 5
  print -r -- "DK-CASE:$typed"
  print -r -- "$out"
  zpty -w -n dk $'\C-u'
done
zpty -d dk
"""


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   check=True, capture_output=True)


class Fixture:
    # A home of its own: ~/Development/acme/widget (a git repository with main, feat/login and feat/ui, and a
    # worktree for feat/login), the handoff repository's registry, items and plans, and devkit's state with two live
    # runs: one of widget and one of another project.
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        dev = self.home / "Development"
        self.handoff = dev / ".handoff"
        self.widget = dev / "acme" / "widget"
        self.wt = dev / ".worktrees" / "acme" / "widget" / "feat-login"
        self.widget.mkdir(parents=True)
        git(self.widget, "init", "-q", "-b", "main")
        git(self.widget, "commit", "-q", "--allow-empty", "-m", "first commit")
        git(self.widget, "branch", "feat/ui")
        git(self.widget, "worktree", "add", "-q", "-b", "feat/login", str(self.wt))
        self.write(self.handoff / "registry.json", json.dumps({"projects": [
            {"id": "acme/widget", "name": "widget", "path": str(self.widget), "local": True,
             "worktrees": [{"branch": "feat/login", "path": str(self.wt)}]},
            {"id": "acme/gadget", "name": "gadget", "path": str(dev / "acme" / "gadget"), "local": False,
             "worktrees": []}],
            "initiatives": [{"id": "launch", "title": "The launch"}]}))
        items = self.handoff / "items" / "acme" / "widget"
        self.write(items / "T-aaaa1111.md", ITEM.format(id="T-aaaa1111", title="Fix the login", status="todo",
                                                       bucket="now", created="2026-11-01T00:00:00Z"))
        self.write(items / "T-bbbb2222.md", ITEM.format(id="T-bbbb2222", title="Ship the beta", status="done",
                                                       bucket="next", created="2026-10-01T00:00:00Z"))
        self.write(self.handoff / "plans" / "acme" / "widget" / "2026-11-01-login.md",
                   "---\nid: P-1\ntitle: Login flow\nstatus: draft\nversion: 1\n---\n# Login flow\n")
        runs = self.home / ".local" / "state" / "devkit" / "runs"
        now = time.time()
        self.write(runs / "claude-s1.json", json.dumps({
            "project": "acme/widget", "state": "running", "started": now - 60,
            "plan": [{"name": "design", "state": "done", "owner": "main"},
                     {"name": "api contract", "state": "running", "owner": "tank", "label": "Backend engineer (Tank)"}]}))
        self.write(runs / "claude-s2.json", json.dumps({
            "project": "acme/gadget", "state": "running", "started": now,
            "plan": [{"name": "gadget step", "state": "running", "owner": "main"}]}))
        self.bin = self.home / "bin"
        self.write(self.bin / "dev", f'#!/bin/sh\nexec "{sys.executable}" -I -S "{REPO / "bin" / "dev"}" "$@"\n')
        (self.bin / "dev").chmod(0o755)

    @staticmethod
    def write(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def env(self, **extra):
        return {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin", "TERM": "xterm-256color",
                "LANG": "en_US.UTF-8", "DEVKIT_NO_NOTIFY": "1", **extra}

    def dev(self, *args, cwd=None):
        return subprocess.run([str(self.bin / "dev"), *args], capture_output=True, text=True, timeout=60,
                              env=self.env(), cwd=str(cwd or self.widget))


def cases(output: str) -> dict:
    # {typed line: {offered value: its description}} from the driver's output
    out, cur, inside = {}, None, False
    for raw in output.splitlines():
        line = ANSI.sub("", raw)
        if line.startswith("DK-CASE:"):
            cur, inside = line[8:], False
            out[cur] = {}
        elif "DK-BEGIN" in line:
            inside = True
        elif "DK-END" in line:
            inside = False
        elif inside and cur is not None and "DK-M:" in line:
            hit, _, desc = line.split("DK-M:", 1)[1].partition("|")
            out[cur].setdefault(hit, desc)
    return out


@unittest.skipUnless(ZSH, "zsh is not installed")
class ZshCompletion(unittest.TestCase):
    TYPED = ("dev ", "dev task ", "dev task add --t", "dev task add --type ", "dev task done ", "dev task show ",
             "dev go ", "dev go widget ", "dev progress step --step ", "dev crew show ", "dev wt new --base ",
             "dev setup ", "dev ci ", "dev audit ", "dev install --only ", "dev completion ", "dcd ", "dcd widget ",
             "dev plan revise ", "dev task -i ", "dev go gadget ", "dcd gadget ")

    @classmethod
    def setUpClass(cls):
        cls.fx = Fixture()
        init = cls.fx.home / "init.zsh"
        init.write_text(INIT.format(devkit_zsh=REPO / "shell" / "devkit.zsh", cwd=cls.fx.widget))
        r = subprocess.run([ZSH, "-f", "-c", DRIVER, "driver", str(init), *cls.TYPED], capture_output=True,
                           text=True, timeout=120, env=cls.fx.env(), errors="replace")
        if r.returncode == 3:
            raise unittest.SkipTest("zsh has no zpty module")
        if r.returncode:
            raise AssertionError(f"the zsh driver exited {r.returncode}: {r.stderr[-2000:]}")
        cls.got = cases(r.stdout)

    @classmethod
    def tearDownClass(cls):
        cls.fx.tmp.cleanup()

    def offered(self, typed):
        self.assertIn(typed, self.got, f"no Tab ran for {typed!r}")
        return self.got[typed]

    # Every command the parser shows, with its help as the description; the ones the old hand-written file missed
    # are there, and the harness-only ones (dev hook, dev __complete) are not.
    def test_dev_offers_every_visible_command_with_its_description(self):
        got = self.offered("dev ")
        self.assertLessEqual({"backup", "free", "handoff-import", "mcp", "completion", "task", "go"}, set(got))
        self.assertFalse({"hook", "__complete"} & set(got))
        self.assertIn("Work items of a project", got["task"])

    # Subcommands, options and choices come from the parser.
    def test_subcommands_options_and_choices(self):
        self.assertLessEqual({"add", "start", "done", "move", "ls"}, set(self.offered("dev task ")))
        self.assertIn("--type", self.offered("dev task add --t"))
        self.assertEqual(set(self.offered("dev task add --type ")), {"task", "feature", "bug", "spike"})
        self.assertLessEqual({"prompt", "commit-template", "shell"}, set(self.offered("dev setup ")))
        self.assertEqual(set(self.offered("dev completion ")), {"zsh", "bash"})
        self.assertLessEqual({"verify", "export", "show", "stats", "config"}, set(self.offered("dev audit ")))

    # A comma list takes the words its help names, the new install step included.
    def test_comma_list_values(self):
        self.assertLessEqual({"config", "tools", "launchd", "prompt", "shell"}, set(self.offered("dev install --only ")))

    # Task ids of the project you are in: open ones to act on, done ones too where a done item makes sense.
    def test_task_ids_of_the_project_you_are_in(self):
        done = self.offered("dev task done ")
        self.assertIn("T-aaaa1111", done)
        self.assertIn("Fix the login", done["T-aaaa1111"])
        self.assertNotIn("T-bbbb2222", done)
        self.assertLessEqual({"T-aaaa1111", "T-bbbb2222"}, set(self.offered("dev task show ")))

    # Projects by alias, the ones on this Mac first, with the id as description; the worktrees of the one named, not
    # of the one you are in (gadget has none here).
    def test_projects_and_their_worktrees(self):
        go = self.offered("dev go ")
        self.assertLessEqual({"widget", "gadget"}, set(go))
        self.assertIn("acme/widget", go["widget"])
        self.assertIn("not on this Mac", go["gadget"])
        self.assertEqual(set(self.offered("dev go widget ")), {"feat/login"})
        self.assertLessEqual({"widget", "gadget"}, set(self.offered("dcd ")))
        self.assertEqual(set(self.offered("dcd widget ")), {"feat/login"})
        self.assertEqual(set(self.offered("dev go gadget ")), set())
        self.assertEqual(set(self.offered("dcd gadget ")), set())

    # Plan steps of this project's live run (not another project's), crew members by id with their role, branches
    # of the repository you are in, plans and initiatives.
    def test_steps_crew_branches_plans_and_initiatives(self):
        steps = self.offered("dev progress step --step ")
        self.assertEqual(set(steps), {"design", "api\\ contract"})  # inserted as one word
        self.assertIn("Backend engineer (Tank)", steps["api\\ contract"])
        crew = self.offered("dev crew show ")
        self.assertIn("link", crew)
        self.assertIn("Release engineer (Link)", crew["link"])
        self.assertLessEqual({"main", "feat/login", "feat/ui"}, set(self.offered("dev wt new --base ")))
        plans = self.offered("dev plan revise ")
        self.assertEqual(len(plans), 1)
        self.assertIn("Login flow", next(iter(plans.values())))
        self.assertIn("launch", self.offered("dev task -i "))

    # `dev ci` takes a verb or a project.
    def test_fixed_words_and_live_values_together(self):
        self.assertLessEqual({"status", "audit", "init", "watch", "widget"}, set(self.offered("dev ci ")))


@unittest.skipUnless(ZSH, "zsh is not installed")
class ZshDeadline(unittest.TestCase):
    # A Tab never waits for a slow `dev __complete`: past _dev_wait it offers what it has (nothing yet), the command
    # finishes in the background, a later Tab takes its values, and the next one within _dev_ttl runs nothing.
    def test_a_slow_command_does_not_hold_the_tab_and_its_values_arrive_later(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            gate, count = d / "gate", d / "count"
            os.mkfifo(gate)
            (d / "dev").write_text(f'#!/bin/sh\necho run >> "{count}"\n[ -p "{gate}" ] && cat "{gate}" >/dev/null\n'
                                   "printf 'T-1:now · one\\n'\n")
            (d / "dev").chmod(0o755)
            script = (f"source {REPO / 'shell' / 'completions' / '_dev'}\n"
                      "zmodload zsh/datetime zsh/zselect\n"
                      "_dev_words=(dev task done '') _dev_wait=0.1\n"
                      "float t0=$EPOCHREALTIME\n"
                      "_dev_values tasks\n"
                      'print -r -- "first ${#reply} $(( EPOCHREALTIME - t0 ))"\n'
                      f"print go > {gate}\n"
                      "local f=${_dev_pending[(R)*]}\n"
                      "repeat 1000; do [[ -e $f ]] && break; zselect -t 1; done\n"
                      "_dev_values tasks\n"
                      'print -r -- "second ${(j:,:)reply}"\n'
                      "_dev_values tasks\n"
                      'print -r -- "third ${(j:,:)reply}"\n'
                      f'print -r -- "runs $(wc -l < {count})"\n')
            r = subprocess.run([ZSH, "-f", "-c", script], capture_output=True, text=True, timeout=60,
                               env={"HOME": str(d), "PATH": f"{d}:/usr/bin:/bin"})
            lines = dict(ln.split(" ", 1) for ln in r.stdout.splitlines() if " " in ln)
            self.assertEqual(r.returncode, 0, r.stderr)
            n, waited = lines["first"].split()
            self.assertEqual(n, "0")
            self.assertLess(float(waited), 1.0, "the Tab waited for the command")
            self.assertEqual(lines["second"], "T-1:now · one")
            self.assertEqual(lines["third"], "T-1:now · one")
            self.assertEqual(lines["runs"].strip(), "1", "a cached value ran the command again")


@unittest.skipUnless(ZSH, "zsh is not installed")
class ZshTabCost(unittest.TestCase):
    BUDGET_MS = 5.0  # the prompt helper's budget

    # A Tab whose values are cached runs no program: 300 cached lookups average under the budget, so only the first
    # Tab of a kind in a folder pays for `dev __complete`.
    def test_a_cached_tab_costs_under_the_budget(self):
        fx = Fixture()
        self.addCleanup(fx.tmp.cleanup)
        script = (f"source {REPO / 'shell' / 'completions' / '_dev'}\nzmodload zsh/datetime\n"
                  "_dev_words=(dev task done '') _dev_wait=10\n"
                  "float t0=$EPOCHREALTIME\n_dev_values tasks\nfloat first=$(( (EPOCHREALTIME - t0) * 1000 ))\n"
                  "t0=$EPOCHREALTIME\nrepeat 300; do _dev_values tasks; done\n"
                  "float cached=$(( (EPOCHREALTIME - t0) * 1000 / 300 ))\n"
                  'print -r -- "$first $cached ${#reply}"\n')
        r = subprocess.run([ZSH, "-f", "-c", script], capture_output=True, text=True, timeout=60, env=fx.env(),
                           cwd=str(fx.widget))
        first, cached, n = r.stdout.split()
        self.assertEqual(n, "1", r.stderr)
        self.assertLess(float(cached), self.BUDGET_MS,
                        f"a cached Tab took {float(cached):.3f} ms (the first, which runs dev, {float(first):.1f} ms)")


@unittest.skipUnless(BASH, "bash is not installed")
class BashCompletion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = Fixture()

    @classmethod
    def tearDownClass(cls):
        cls.fx.tmp.cleanup()

    def complete(self, *words):
        # COMPREPLY for `dev <words…>` with the cursor on the last word, as bash fills COMP_WORDS on Tab
        quoted = " ".join("'" + w.replace("'", "'\\''") + "'" for w in ("dev", *words))
        script = (f"source {REPO / 'shell' / 'completions' / 'dev.bash'}\n_dev_wait=10\n"
                  f"COMP_WORDS=({quoted}); COMP_CWORD={len(words)}; _dev; printf '%s\\n' \"${{COMPREPLY[@]}}\"\n")
        r = subprocess.run([BASH, "--norc", "--noprofile", "-c", script], capture_output=True, text=True,
                           timeout=60, env=self.fx.env(), cwd=str(self.fx.widget))
        self.assertEqual(r.returncode, 0, r.stderr)
        return {ln for ln in r.stdout.splitlines() if ln}

    # The same parser and live values as zsh: commands, subcommands, choices, comma lists and task ids.
    def test_commands_choices_and_live_values(self):
        self.assertIn("task", self.complete("ta"))
        self.assertNotIn("hook", self.complete("h"))
        self.assertLessEqual({"add", "done"}, self.complete("task", ""))
        self.assertEqual(self.complete("task", "add", "--type", ""), {"task", "feature", "bug", "spike"})
        self.assertIn("config", self.complete("install", "--only", "con"))
        self.assertEqual(self.complete("task", "done", ""), {"T-aaaa1111"})
        self.assertEqual(self.complete("go", "widget", ""), {"feat/login"})
        self.assertEqual(self.complete("go", "gadget", ""), set())
        self.assertIn("api\\ contract", self.complete("progress", "step", "--step", ""))


class LiveValues(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = Fixture()

    @classmethod
    def tearDownClass(cls):
        cls.fx.tmp.cleanup()

    # bin/dev answers `dev __complete` without building the parser, as value:description lines (a colon in a value
    # escaped), or value<TAB>description for the fzf pickers; an unknown kind falls through to the full command.
    def test_complete_lines_and_the_tab_form(self):
        r = self.fx.dev("__complete", "projects")
        self.assertEqual(r.stdout.splitlines()[:2], ["widget:acme/widget", "gadget:acme/gadget · not on this Mac"])
        r = self.fx.dev("__complete", "places", "--tab")
        self.assertIn(f"{self.fx.wt}\twidget · feat/login", r.stdout.splitlines())
        self.assertEqual(completion.render([("a:b", "c")]), "a\\:b:c\n")
        r = self.fx.dev("__complete", "nonsense")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown kind", r.stderr)

    # dev go <project> <branch>: the worktree's path, by its branch or a unique part of it; no match lists them.
    def test_dev_go_to_a_worktree(self):
        r = self.fx.dev("go", "widget", "login")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, str(self.fx.wt)))
        r = self.fx.dev("go", "widget", "nope")
        self.assertEqual(r.returncode, 2)
        self.assertIn("feat/login", r.stderr)


class Generated(unittest.TestCase):
    # The checked-in scripts are what the parser generates now (selfcheck D13 says the same in CI), and the check
    # catches a script that differs from the parser.
    def test_checked_in_scripts_match_the_parser_and_drift_is_caught(self):
        root = completion.tree()
        self.assertEqual(completion.drift(REPO, root), [])
        with tempfile.TemporaryDirectory() as d:
            for rel, _ in completion.SCRIPTS.values():
                (Path(d) / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(REPO / rel, Path(d) / rel)
            stale = Path(d) / "shell" / "completions" / "_dev"
            stale.write_text(stale.read_text().replace("'completion:", "'completions:"))
            self.assertEqual([f[0] for f in completion.drift(d, root)], ["shell/completions/_dev"])

    # A rule whose argument the parser no longer has is reported, so a rename cannot silently drop a completion.
    def test_a_rule_without_its_argument_is_reported(self):
        ap = cli.Parser(prog="dev")
        go = ap.add_subparsers(dest="cmd").add_parser("go")
        go.add_argument("query")
        unused = completion.unused_rules(completion.tree(ap))
        self.assertIn((("go",), "branch", "worktrees:@1"), unused)
        self.assertNotIn((("go",), "query", "projects"), unused)

    # Help text becomes a one-line description: the first clause outside parentheses, cut at a word.
    def test_short_descriptions(self):
        self.assertEqual(completion.short("What CI runs (to run it locally first), `dev ci status`: live"),
                         "What CI runs")
        self.assertEqual(completion.short("project (default: the one you are in)"),
                         "project (default: the one you are in)")
        self.assertEqual(completion.short("word " * 30, width=20), "word word word word…")


class ShellHelpersConsent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state = mock.patch.object(core, "STATE", Path(self.tmp.name))
        state.start()
        self.addCleanup(state.stop)
        self.said = []

    # Consent is the developer's: in an agent's call, neither --yes nor a piped answer turns the helpers on, and
    # nothing is asked; turning them off needs no consent.
    def test_an_agents_yes_is_not_consent(self):
        def never(q):
            raise AssertionError("an agent's call is never asked")
        with mock.patch.object(core, "set_by_developer", return_value=False):
            for kw in ({"assume_yes": True}, {}):
                self.assertEqual(setup.setup_shell(ask=never, say=self.said.append, **kw), 2)
                self.assertEqual(completion.helpers_mode(), "")
            self.assertIn("in your own terminal", "\n".join(self.said))
            self.assertEqual(setup.setup_shell(remove=True, say=self.said.append), 0)
        self.assertEqual(completion.helpers_mode(), "off")

    # The developer's yes turns them on and a no records off; `dev install` keeps the answer and, with no terminal
    # (a job, an agent), asks nothing and changes nothing.
    def test_the_developers_answer_is_recorded_and_install_keeps_it(self):
        with mock.patch.object(core, "set_by_developer", return_value=True):
            with setup.quiet_mode(True):
                setup.install_shell()
            self.assertEqual(completion.helpers_mode(), "")
            self.assertEqual(setup.setup_shell(ask=lambda q: "n", say=self.said.append), 0)
            self.assertEqual(completion.helpers_mode(), "off")
            self.assertEqual(setup.setup_shell(ask=lambda q: "", say=self.said.append), 0)
            self.assertEqual(completion.helpers_mode(), "on")


@unittest.skipUnless(ZSH, "zsh is not installed")
class ZshHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = Fixture()
        fzf = cls.fx.bin / "fzf"
        fzf.write_text("#!/bin/sh\nhead -n 1\n")  # picks the first candidate
        fzf.chmod(0o755)

    @classmethod
    def tearDownClass(cls):
        cls.fx.tmp.cleanup()

    def shell(self, mode, body):
        helpers = self.fx.home / ".local" / "state" / "devkit" / "shell" / "helpers"
        helpers.parent.mkdir(parents=True, exist_ok=True)
        helpers.write_text(mode + "\n")
        script = f"bindkey -M emacs '^Xb' backward-word\nsource {REPO / 'shell' / 'devkit.zsh'}\n{body}"
        r = subprocess.run([ZSH, "-f", "-i", "-c", script], capture_output=True, text=True, timeout=60,
                           env=self.fx.env(), cwd=str(self.fx.widget), stdin=subprocess.DEVNULL)
        return r.stdout

    # With the developer's yes and fzf installed: Ctrl-X i and p pick a work item and a project, a key the developer
    # bound keeps their binding, dpick prints the picked value and dcd alone goes to the picked place.
    def test_on_binds_free_keys_and_picks(self):
        out = self.shell("on", "bindkey -M emacs '^Xi'; bindkey -M emacs '^Xp'; bindkey -M emacs '^Xb'\n"
                               "print -r -- pick=$(dpick task)\ndcd && print -r -- here=$PWD\n")
        self.assertIn('"^Xi" devkit-pick-task', out)
        self.assertIn('"^Xp" devkit-pick-project', out)
        self.assertIn('"^Xb" backward-word', out)
        self.assertIn("pick=T-aaaa1111", out)
        self.assertIn(f"here={self.fx.widget}", out)

    # Off (or never answered): no key changes and no pickers; dcd <alias> works either way.
    def test_off_changes_no_keys(self):
        out = self.shell("off", "bindkey -M emacs '^Xi'\nprint -r -- dpick=$+functions[dpick]\n"
                                "dcd widget && print -r -- here=$PWD\n")
        self.assertIn('"^Xi" undefined-key', out)
        self.assertIn("dpick=0", out)
        self.assertIn(f"here={self.fx.widget}", out)


if __name__ == "__main__":
    unittest.main()
