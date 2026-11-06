# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The devkit segment in the shell prompt (spec P-fxs316lh3nw R9): the cache devkit's hooks and commands write
# (lib/devkit/prompt.py), the zsh function that reads it with builtins only (shell/devkit.zsh) and its 5 ms budget,
# and `dev setup prompt` for Starship and plain zsh. Every test runs in a temp HOME with its own state and handoff
# folders: none reads or writes the developer's ~/.zshrc, ~/.config/starship.toml or devkit state.
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import audit, cistatus, ciwatch, core, progress, prompt, registry, setup  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEVKIT_ZSH = ROOT / "shell" / "devkit.zsh"
ZSH = shutil.which("zsh")
STARSHIP = shutil.which("starship")
UNSET = "<unset>"

# Starship's Pastel Powerline preset, cut to what the block depends on: the format with the git group, and the
# git modules' background.
PASTEL = '''"$schema" = 'https://starship.rs/config-schema.json'

format = """
[](#9A348E)\\
$username\\
[](bg:#DA627D fg:#9A348E)\\
$directory\\
[](fg:#DA627D bg:#FCA17D)\\
$git_branch\\
$git_status\\
[](fg:#FCA17D bg:#86BBD8)\\
$time\\
[ ](fg:#33658A)\\
"""

[directory]
style = "bg:#DA627D"
format = "[ $path ]($style)"

[git_branch]
symbol = ""
style = "bg:#FCA17D"
format = '[ $symbol $branch ]($style)'

[git_status]
style = "bg:#FCA17D"
format = '[$all_status$ahead_behind ]($style)'
'''


def toml_ok(text):
    try:
        import tomllib
    except ImportError:  # Python 3.9 and 3.10: the text checks in each test still apply
        return None
    return tomllib.loads(text)


@unittest.skipUnless(ZSH, "zsh is not installed")
class Sandbox(unittest.TestCase):
    # A temp HOME (state and config below it), a temp handoff folder and checkouts made of bare .git entries.
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(core.real(self.tmp.name))
        self.home = self.root / "home"
        self.state = self.home / ".local" / "state" / "devkit"
        self.handoff = self.root / "handoff"
        for d in (self.home, self.state, self.handoff):
            d.mkdir(parents=True)
        (self.handoff / "registry.json").write_text(json.dumps({"projects": []}))
        self.saved = (prompt.STATE, setup.STATE, core.HANDOFF, ciwatch.WATCH, cistatus.CACHE, progress.RUNS,
                      audit.AUDIT)
        prompt.STATE = setup.STATE = self.state
        core.HANDOFF = self.handoff
        ciwatch.WATCH = self.state / "ci" / "watch"
        cistatus.CACHE = self.state / "cache" / "ci"
        progress.RUNS = self.state / "runs"
        audit.AUDIT = self.state / "audit"

    def tearDown(self):
        (prompt.STATE, setup.STATE, core.HANDOFF, ciwatch.WATCH, cistatus.CACHE, progress.RUNS,
         audit.AUDIT) = self.saved
        self.tmp.cleanup()

    # ---- fixtures

    def checkout(self, name, branch="main"):
        d = self.root / "dev" / name
        (d / ".git").mkdir(parents=True)
        (d / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
        return d

    def worktree(self, main, name, branch):
        wt = self.root / "worktrees" / name
        wt.mkdir(parents=True)
        gd = main / ".git" / "worktrees" / name
        gd.mkdir(parents=True)
        (gd / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
        (gd / "commondir").write_text("../..\n")
        (wt / ".git").write_text(f"gitdir: {gd}\n")
        return wt

    def project(self, pid, path, worktrees=()):
        owner, _, name = pid.rpartition("/")
        return {"id": pid, "owner": owner, "name": name, "path": str(path) if path else None,
                "local": path is not None, "remote": f"https://github.com/{pid}.git",
                "worktrees": [{"path": str(w), "location": "canonical"} for w in worktrees]}

    def live_run(self, pid, lanes=(), decisions=(), last=None):
        # lanes: (state, heartbeat); `last` is the run's newest report (default: a minute ago)
        now = time.time()
        last = now - 60 if last is None else last
        return {"project": pid, "state": "running", "started": min(last, now - 3600), "updates": [{"ts": last}],
                "plan": [], "agents": {f"lane{i}": {"state": st, "started": min(last, now - 3600), "last_seen": hb,
                                                    "updates": []} for i, (st, hb) in enumerate(lanes)},
                "decisions": [{"id": f"D{i + 1}", "state": s, "question": "keep /v1?"} for i, s in enumerate(decisions)]}

    def mode(self, m):
        (self.state / "prompt").mkdir(parents=True, exist_ok=True)
        (self.state / "prompt" / "mode").write_text(m + "\n")

    def zsh(self, script, *args, env=None, pre=""):
        base = {"HOME": str(self.home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
                "TERM": "xterm-256color"}
        out = subprocess.run([ZSH, "-f", "-c", f"{pre}\nsource {prompt.zsh_quote(DEVKIT_ZSH)}\n{script}", "zsh", *args],
                             env=dict(base, **(env or {})), capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def probe(self, *where, env=None, mode="starship"):
        # where: (folder, columns); -> [(DEVKIT_PROMPT, DEVKIT_PROMPT_ASK)], UNSET for a variable that is not set
        self.mode(mode)
        out = self.zsh('for spec in "$@"; do\n  cd -q -- "${spec%|*}"; COLUMNS=${spec##*|}\n  _devkit_prompt\n'
                       f'  print -r -- "${{DEVKIT_PROMPT-{UNSET}}}|${{DEVKIT_PROMPT_ASK-{UNSET}}}"\ndone',
                       *[f"{d}|{c}" for d, c in where], env=env)
        return [tuple(line.split("|", 1)) for line in out.splitlines()]


class CacheAndSegment(Sandbox):
    # The segment appears in every checkout of a devkit project, and nowhere else.
    def test_segment_in_each_checkout_and_nothing_outside_devkit_projects(self):
        main = self.checkout("devkit", "release/26.11.3")
        wt = self.worktree(main, "feat-x", "feat/x")
        deep = main / "lib" / "devkit"
        deep.mkdir(parents=True)
        plain = self.checkout("not-a-devkit-project")
        reg = {"projects": [self.project("acme/devkit", main, [wt]), self.project("acme/away", None)]}
        self.assertEqual(prompt.refresh(reg=reg, runs=[]), ["acme/devkit"])
        got = self.probe((deep, 120), (wt, 120), (plain, 120), (self.root, 120))
        self.assertEqual(got, [("✦ devkit", UNSET), ("✦ devkit", UNSET), (UNSET, UNSET), (UNSET, UNSET)])

    # Without the developer's answer (no mode file), or after a no, sourcing devkit.zsh adds nothing to the prompt;
    # after a yes, the function runs first of the precmd functions.
    def test_prompt_hook_only_after_the_developer_agrees(self):
        check = 'print -r -- "${+functions[_devkit_prompt]} ${precmd_functions[1]:--}"'
        self.assertEqual(self.zsh(check).strip(), "0 -")
        self.mode("off")
        self.assertEqual(self.zsh(check).strip(), "0 -")
        self.mode("starship")
        out = self.zsh('print -r -- "${+functions[_devkit_prompt]} $precmd_functions"', pre="precmd_functions=(theme)")
        self.assertEqual(out.strip(), "1 _devkit_prompt theme")

    # A hook in a worktree made after the last `dev sync` finds its project through .git's commondir.
    def test_hook_refresh_finds_a_worktree_the_registry_does_not_know(self):
        main = self.checkout("devkit")
        reg = {"projects": [self.project("acme/devkit", main)]}
        prompt.refresh(reg=reg, runs=[])
        wt = self.worktree(main, "fix-y", "fix/y")
        self.assertEqual(self.probe((wt, 120)), [(UNSET, UNSET)])
        self.assertEqual(prompt.refresh(wt, reg=reg, runs=[], force=True), ["acme/devkit"])
        self.assertEqual(self.probe((wt, 120), (main, 120)), [("✦ devkit", UNSET), ("✦ devkit", UNSET)])

    # Crew: members of a working run (registry.run_working) whose heartbeat is newer than 15 minutes, counted at
    # prompt time, so a member that went quiet after the cache was written drops out without a new refresh.
    def test_crew_counts_fresh_heartbeats_in_working_runs_only(self):
        now = time.time()
        main, other = self.checkout("devkit"), self.checkout("kit")
        quiet = self.live_run("acme/kit", lanes=[("running", now - 30)], last=now - 2400)
        self.assertFalse(registry.run_working(quiet, now))
        busy = self.live_run("acme/devkit", lanes=[("running", now - 60), ("running", now - 600),
                                              ("running", now - 1200), ("done", now - 30)])
        reg = {"projects": [self.project("acme/devkit", main), self.project("acme/kit", other)]}
        prompt.refresh(reg=reg, runs=[busy, quiet])
        self.assertEqual(self.probe((main, 120), (main, 80), (other, 120)),
                         [("✦ devkit · 2 working", UNSET), ("✦ devkit ▸2", UNSET), ("✦ kit", UNSET)])
        written = now - 300  # the cache was written 5 minutes ago: one heartbeat was 11 min old then, 16 min now
        busy = self.live_run("acme/devkit", lanes=[("running", written - 660), ("running", now - 30)])
        prompt.refresh(reg=reg, runs=[busy], now=written)
        self.assertEqual(self.probe((main, 120)), [("✦ devkit · 1 working", UNSET)])

    # A question waits for the developer: its own chip, gone as soon as it is answered.
    def test_question_chip_until_the_decision_is_answered(self):
        main = self.checkout("devkit")
        reg = {"projects": [self.project("acme/devkit", main)]}
        prompt.refresh(reg=reg, runs=[self.live_run("acme/devkit", decisions=["pending", "decided"])])
        self.assertEqual(self.probe((main, 120), (main, 80)),
                         [("✦ devkit", "! 1 question for you"), ("✦ devkit", "!1")])
        prompt.refresh(main, reg=reg, runs=[self.live_run("acme/devkit", decisions=["pending", "pending"])], force=True)
        self.assertEqual(self.probe((main, 120)), [("✦ devkit", "! 2 questions for you")])
        prompt.refresh(main, reg=reg, runs=[self.live_run("acme/devkit", decisions=["decided", "decided"])], force=True)
        self.assertEqual(self.probe((main, 120)), [("✦ devkit", UNSET)])

    # Checks: the newer of `dev ci watch`'s result and the runs devkit read last, for the branch HEAD is on now.
    def test_checks_follow_the_branch_and_the_newest_result(self):
        main = self.checkout("devkit", "main")
        reg = {"projects": [self.project("acme/devkit", main)]}
        prompt.refresh(reg=reg, runs=[])
        self.assertEqual(self.probe((main, 120)), [("✦ devkit", UNSET)])  # nothing pushed: no checks part
        ciwatch.WATCH.mkdir(parents=True)
        (ciwatch.WATCH / f"{ciwatch._key('acme/devkit', 'main')}.json").write_text(json.dumps(
            {"ts": time.time() - 100, "state": "passed", "repo": "acme/devkit", "branch": "main"}))
        prompt.refresh(reg=reg, runs=[])
        self.assertEqual(self.probe((main, 120), (main, 80)), [("✦ devkit · checks ✓", UNSET), ("✦ devkit ✓", UNSET)])
        cistatus.CACHE.mkdir(parents=True)
        runs = cistatus.CACHE / "runs-github-acme__devkit-main-1-1.json"
        for status, glyph in (("running", "…"), ("failed", "✗")):
            runs.write_text(json.dumps({"runs": [{"branch": "main", "status": status, "started": time.time()}]}))
            prompt.refresh(reg=reg, runs=[])
            self.assertEqual(self.probe((main, 120)), [(f"✦ devkit · checks {glyph}", UNSET)])
        (main / ".git" / "HEAD").write_text("ref: refs/heads/feat/other\n")  # git switch, and no devkit event yet
        self.assertEqual(self.probe((main, 120)), [("✦ devkit", UNSET)])

    # `dev ci watch` records a result: once the developer turned the helper on, the prompt shows it at once; before
    # that, nothing is written. Through the real audit trail (lesson L-fxzc02legsi: no stubbed recorder).
    def test_ci_watch_result_reaches_the_prompt_once_turned_on(self):
        main = self.checkout("devkit", "main")
        (self.handoff / "registry.json").write_text(json.dumps({"projects": [self.project("acme/devkit", main)]}))
        res = {"ts": round(time.time()), "state": "failed", "provider": "github", "repo": "acme/devkit",
               "branch": "main", "sha": "abc1234", "minutes": 3.0, "error": None, "runs": [], "diagnoses": []}
        with mock.patch.dict(os.environ, {"DEVKIT_NO_NOTIFY": "1"}):
            ciwatch.record(str(main), res)
            self.assertFalse(prompt.cache_zsh().exists())
            self.mode("starship")
            ciwatch.record(str(main), res)
        self.assertEqual(self.probe((main, 80)), [("✦ devkit ✗", UNSET)])
        self.assertTrue(list(audit.AUDIT.rglob("*")), "ci.watch went to the real audit trail")

    # A cache older than 24 hours shows only the alias: never a crew, a check or a question it cannot vouch for.
    def test_stale_cache_shows_only_the_alias(self):
        main = self.checkout("devkit", "main")
        reg = {"projects": [self.project("acme/devkit", main)]}
        old = time.time() - 25 * 3600
        prompt.refresh(reg=reg, runs=[self.live_run("acme/devkit", lanes=[("running", old)], decisions=["pending"],
                                               last=old)], now=old)
        self.assertEqual(self.probe((main, 120)), [("✦ devkit", UNSET)])

    # Aliases over 16 characters are cut; a `%` stays plain text in the variable and literal in a zsh prompt.
    def test_long_alias_cut_and_percent_never_a_prompt_sequence(self):
        long_, pct = self.checkout("long"), self.checkout("pct")
        (self.handoff / "aliases.json").write_text(json.dumps(
            {"aliases": {"a-very-long-alias-name": {"project": "acme/long", "at": "2026-11-06"}}}))
        reg = {"projects": [self.project("acme/long", long_), self.project("acme/50%off", pct)]}
        prompt.refresh(reg=reg, runs=[])
        self.assertEqual(self.probe((long_, 120), (pct, 120)),
                         [("✦ a-very-long-alia…", UNSET), ("✦ acme/50%off", UNSET)])
        self.mode("zsh")
        script = ("cd -q -- $1; COLUMNS=120; PROMPT='%# '\nfor f in $precmd_functions; do $f; done\n"
                  "for f in $precmd_functions; do $f; done\nprint -r -- \"${(%%)PROMPT}\"")
        colored = self.zsh(script, str(pct)).rstrip("\n")
        self.assertIn("\x1b[", colored)
        self.assertIn("acme/50%off", colored)
        self.assertEqual(colored.count("acme/50%off"), 1, "the segment is put in front once")
        plain = self.zsh(script, str(pct), env={"NO_COLOR": "1"}).rstrip("\n")
        self.assertEqual(plain, "✦ acme/50%off % ")  # the segment, then PROMPT's own '%# '

    # The zsh function and prompt.segment() are one rule: same text for every state, width and locale.
    def test_zsh_function_and_python_rule_agree(self):
        now = time.time()
        a, b, c, d = (self.checkout(n, "main") for n in ("a", "b", "c", "d"))
        (c / ".git" / "HEAD").write_text("ref: refs/heads/moved\n")
        e = {"alias": "devkit", "ts": int(now), "crew": [int(now + 600), int(now + 700), int(now - 600)], "ask": 1,
             "checks": "passed", "head": str(a / ".git" / "HEAD"), "ref": "ref: refs/heads/main"}
        cases = {str(a): e,
                 str(b): dict(e, alias="kit-consulting-sandbox", crew=[], ask=2, checks="failed",
                              head=str(b / ".git" / "HEAD")),
                 str(c): dict(e, checks="running", head=str(c / ".git" / "HEAD")),
                 str(d): dict(e, ts=int(now - 90000), head=str(d / ".git" / "HEAD"))}
        doc = {"projects": {k: {"alias": v["alias"], "ts": v["ts"], "crew": v["crew"], "ask": v["ask"],
                                "roots": {k: {"head": v["head"], "ref": v["ref"], "checks": v["checks"]}}}
                            for k, v in cases.items()}}
        prompt._atomic(prompt.cache_zsh(), prompt.render_zsh(doc))
        for lang, utf8 in (("en_US.UTF-8", True), ("C", False)):
            where = [(k, cols) for k in cases for cols in (80, 120)]
            got = self.probe(*where, env={"LANG": lang})
            want = [tuple(x or UNSET for x in prompt.segment(cases[k], cols, now, utf8)) for k, cols in where]
            self.assertEqual(got, want, lang)

    # The budget (R9): under 5 ms per prompt, with 40 projects of 3 checkouts each and the shell 8 folders deep;
    # also when the cache changed before every prompt, so the function reads it again each time.
    def test_a_prompt_costs_under_5_ms(self):
        now = time.time()
        projects = {}
        for i in range(40):
            roots = {}
            for j in range(3):
                d = self.checkout(f"p{i}-{j}", "main")
                roots[str(d)] = {"head": str(d / ".git" / "HEAD"), "ref": "ref: refs/heads/main", "checks": "passed"}
            projects[f"acme/p{i}"] = {"alias": f"p{i}", "ts": int(now), "crew": [int(now + 600)] * 6, "ask": 1,
                                      "roots": roots}
        prompt._atomic(prompt.cache_zsh(), prompt.render_zsh({"projects": projects}))
        deep = self.root / "dev" / "p20-1" / "a" / "b" / "c" / "d" / "e" / "f" / "g" / "h"
        deep.mkdir(parents=True)
        self.mode("zsh")
        out = self.zsh(
            "cd -q -- $1; COLUMNS=120\n"
            "for f in $precmd_functions; do $f; done\n"
            "float t0=$EPOCHREALTIME\n"
            "repeat 300; do for f in $precmd_functions; do $f; done; done\n"
            "float steady=$(( (EPOCHREALTIME - t0) * 1000.0 / 300 ))\n"
            "t0=$EPOCHREALTIME\n"
            "repeat 60; do _dk_psig=; for f in $precmd_functions; do $f; done; done\n"
            "float reread=$(( (EPOCHREALTIME - t0) * 1000.0 / 60 ))\n"
            'print -r -- "$steady $reread $DEVKIT_PROMPT|$DEVKIT_PROMPT_ASK"', str(deep))
        steady, reread, seg = out.strip().split(" ", 2)
        self.assertEqual(seg, "✦ p20 · 6 working · checks ✓|! 1 question for you")
        self.assertLess(float(steady), 5.0, f"a prompt took {float(steady):.3f} ms")
        self.assertLess(float(reread), 5.0, f"a prompt that reads the cache again took {float(reread):.3f} ms")


class SetupPrompt(Sandbox):
    def setUp(self):
        super().setUp()
        self.toml = self.home / ".config" / "starship.toml"
        self.zshrc = self.home / ".zshrc"
        self.said = []

    def setup_prompt(self, answer="", **kw):
        return setup.setup_prompt(ask=lambda q: answer, zshrc=self.zshrc, starship=self.toml, env={},
                                  say=self.said.append, **kw)

    def backups(self, name):
        return sorted((self.state / "backups").glob(f"{name}.*"))

    # Starship: the marked block, the references after $git_status in the same background, a backup first; a
    # second run changes nothing and --remove gives the file back as it was.
    def test_starship_block_after_git_status_backed_up_and_removable(self):
        self.toml.parent.mkdir(parents=True)
        self.toml.write_text(PASTEL)
        self.zshrc.write_text('eval "$(starship init zsh)"\n')
        self.assertEqual(self.setup_prompt(), 0)
        text = self.toml.read_text()
        self.assertIn("$git_status\\\n${env_var.DEVKIT_PROMPT}\\\n${env_var.DEVKIT_PROMPT_ASK}\\\n"
                      "[](fg:#FCA17D bg:#86BBD8)\\", text)
        self.assertIn('style = "bg:#FCA17D fg:#10110F"', text)
        self.assertIn('style = "bg:#C0392B fg:#F3F1EB bold"', text)
        self.assertEqual(text.count(setup.PROMPT_START), 1)
        parsed = toml_ok(text)
        if parsed is not None:
            self.assertEqual(parsed["env_var"]["DEVKIT_PROMPT"]["variable"], "DEVKIT_PROMPT")
            self.assertEqual(parsed["env_var"]["DEVKIT_PROMPT_ASK"]["variable"], "DEVKIT_PROMPT_ASK")
        self.assertEqual([b.read_text() for b in self.backups("starship.toml")], [PASTEL])
        self.assertEqual(prompt.mode(), "starship")
        self.assertEqual(self.zshrc.read_text(), 'eval "$(starship init zsh)"\n')
        self.assertEqual(self.setup_prompt(), 0)
        self.assertEqual(self.toml.read_text(), text)
        self.assertEqual(self.setup_prompt(remove=True), 0)
        self.assertEqual(self.toml.read_text(), PASTEL)
        self.assertEqual(prompt.mode(), "off")

    # No consent, no change: the file, its backups and the shell stay as they are.
    def test_starship_untouched_when_the_developer_says_no(self):
        self.toml.parent.mkdir(parents=True)
        self.toml.write_text(PASTEL)
        self.zshrc.write_text('eval "$(starship init zsh)"\n')
        self.assertEqual(self.setup_prompt("n"), 0)
        self.assertEqual(self.toml.read_text(), PASTEL)
        self.assertEqual(self.backups("starship.toml"), [])
        self.assertEqual(prompt.mode(), "off")

    # The developer's later choices stand: references they moved stay where they put them, and `dev install` never
    # adds back a block they took out by hand.
    def test_starship_keeps_moved_references_and_install_never_re_adds_the_block(self):
        self.toml.parent.mkdir(parents=True)
        self.toml.write_text(PASTEL)
        self.zshrc.write_text('eval "$(starship init zsh)"\n')
        self.setup_prompt()
        moved = self.toml.read_text().replace("${env_var.DEVKIT_PROMPT}\\\n${env_var.DEVKIT_PROMPT_ASK}\\\n", "")
        moved = moved.replace("$username\\\n", "$username\\\n${env_var.DEVKIT_PROMPT}\\\n${env_var.DEVKIT_PROMPT_ASK}\\\n")
        self.toml.write_text(moved)
        with setup.quiet_mode(True):
            setup.install_prompt(starship=self.toml)
        self.assertEqual(self.toml.read_text(), moved)
        by_hand = setup.starship_without_prompt(moved)
        self.assertEqual(by_hand, PASTEL)
        self.toml.write_text(by_hand)
        with setup.quiet_mode(True):
            setup.install_prompt(starship=self.toml)
        self.assertEqual(self.toml.read_text(), PASTEL)

    # A theme without backgrounds and without git modules: the references go first, in plain amber and red; a
    # config without `format` gets only the block (Starship's default format shows env_var modules).
    def test_starship_plain_theme_and_default_format(self):
        self.toml.parent.mkdir(parents=True)
        self.zshrc.write_text('eval "$(starship init zsh)"\n')
        plain = 'format = "$directory$character"\n\n[character]\nsuccess_symbol = ">"\n'
        self.toml.write_text(plain)
        self.assertEqual(self.setup_prompt(), 0)
        text = self.toml.read_text()
        self.assertTrue(text.startswith('format = "${env_var.DEVKIT_PROMPT}${env_var.DEVKIT_PROMPT_ASK}'
                                        '$directory$character"\n'), text)
        self.assertIn('style = "bold fg:#FFB34A"', text)
        self.assertIn('style = "bold fg:#C0392B"', text)
        self.assertEqual(self.setup_prompt(remove=True), 0)
        self.assertEqual(self.toml.read_text(), plain)
        bare = '[character]\nsuccess_symbol = ">"\n'
        self.toml.write_text(bare)
        self.assertEqual(self.setup_prompt(), 0)
        text = self.toml.read_text()
        self.assertTrue(text.startswith(bare + "\n" + setup.PROMPT_START), text)
        self.assertNotIn("format =", text.split(setup.PROMPT_START)[0])

    # A config that already defines the variables itself is left alone, with the reason.
    def test_starship_refuses_a_table_it_did_not_write(self):
        self.toml.parent.mkdir(parents=True)
        self.zshrc.write_text('eval "$(starship init zsh)"\n')
        own = '[env_var.DEVKIT_PROMPT]\nvariable = "DEVKIT_PROMPT"\n'
        self.toml.write_text(own)
        self.assertEqual(self.setup_prompt(), 1)
        self.assertEqual(self.toml.read_text(), own)
        self.assertEqual(self.backups("starship.toml"), [])
        self.assertIn("outside devkit's block", "\n".join(self.said))

    # Starship escapes `%` for zsh itself, so the variable must hold plain text: escaped once, not twice.
    @unittest.skipUnless(STARSHIP, "starship is not installed")
    def test_starship_escapes_percent_once(self):
        cfg = self.root / "starship.toml"
        cfg.write_text(setup.starship_with_prompt('format = "$git_branch"\n', cfg))
        out = subprocess.run([STARSHIP, "prompt"], capture_output=True, text=True, timeout=60, cwd=self.root,
                             env={"HOME": str(self.home), "PATH": os.environ.get("PATH", ""), "STARSHIP_SHELL": "zsh",
                                  "STARSHIP_CONFIG": str(cfg), "DEVKIT_PROMPT": "✦ acme/50%off"}).stdout
        self.assertIn("acme/50%%off", out)
        self.assertNotIn("50%%%%off", out)

    # Plain zsh: devkit's own block in shell/devkit.zsh is turned on; ~/.zshrc and Starship's file are not touched.
    def test_plain_zsh_turns_on_devkits_block_and_leaves_zshrc_alone(self):
        rc = "PROMPT='%~ %# '\n"
        self.zshrc.write_text(rc)
        self.assertEqual(self.setup_prompt("y"), 0)
        self.assertEqual(prompt.mode(), "zsh")
        self.assertEqual(self.zshrc.read_text(), rc)
        self.assertFalse(self.toml.exists())
        main = self.checkout("devkit")
        prompt.refresh(reg={"projects": [self.project("acme/devkit", main)]}, runs=[])
        out = self.zsh("cd -q -- $1; COLUMNS=80; PROMPT='%# '\nfor f in $precmd_functions; do $f; done\n"
                       'print -r -- "${(%%)PROMPT}"', str(main), env={"NO_COLOR": "1"})
        self.assertEqual(out.rstrip("\n"), "✦ devkit % ")

    # oh-my-posh and Powerlevel10k draw their own prompt: devkit sets the variables and points to the snippet.
    def test_oh_my_posh_and_powerlevel10k_get_the_snippet_not_a_file_change(self):
        for line, kind in (('eval "$(oh-my-posh init zsh)"\n', "oh-my-posh"),
                           ("source ~/powerlevel10k/powerlevel10k.zsh-theme\n", "powerlevel10k")):
            self.zshrc.write_text(line)
            self.said.clear()
            self.assertEqual(self.setup_prompt(), 0)
            self.assertEqual(prompt.mode(), kind)
            self.assertEqual(self.zshrc.read_text(), line)
            self.assertFalse(self.toml.exists())
            self.assertIn(setup.PROMPT_DOCS, "\n".join(self.said))


class Doctor(unittest.TestCase):
    # Low disk: doctor names dev free, the command users see (Hamann's decision (b)), not dev gc --apply.
    def test_low_disk_fix_is_dev_free(self):
        group, name, ok, detail, fix = setup.free_disk_check(12.4)
        self.assertFalse(ok)
        self.assertEqual((fix, detail), ("dev free", "12 GB free"))
        self.assertTrue(setup.free_disk_check(64)[2])


if __name__ == "__main__":
    unittest.main()
