# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# One project per session: a session in a project's checkout writes only that project's handoff files through dev,
# as it does with a file tool. The guard refuses the dev commands that would write another project's plans, session
# log, note, lessons or delivery policy (-p, a project named as the argument, a plan path below another project's
# folder) and the ones aimed at an initiative the project is not a member of, for Claude Code's Bash and Codex's
# exec_command and shell. Each refusal has an allowed twin: the same command for the session's own project, a command
# that only reads, a portfolio-level session, or the developer's recorded hatch. The guard reads command text and
# paths: no command runs.
# Run: python3 -m unittest tests.test_guard_projects -v
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
sys.path.insert(0, str(ROOT / "lib"))

from devkit import core, guard  # noqa: E402

OWN, OTHER = "acme/widget", "acme/gadget"
HATCH = "DEVKIT" + "_CROSS_PROJECT"  # built here: a test module that spells the hatch as text is refused to run


def payloads(cmd):
    """The same command as each agent sends it: Claude Code's Bash, Codex's exec_command and its shell tool."""
    return [("Bash", {"command": cmd}, "claude"), ("exec_command", {"cmd": cmd}, "codex"),
            ("shell", {"command": ["bash", "-lc", cmd]}, "codex")]


class OneProjectPerSession(unittest.TestCase):

    def setUp(self):
        d = tempfile.mkdtemp(dir=str(core.HOME))
        self.addCleanup(shutil.rmtree, d, True)
        self.top, self.other_top = Path(d) / "acme" / "widget", Path(d) / "acme" / "gadget"
        for top, pid in ((self.top, OWN), (self.other_top, OTHER)):
            top.mkdir(parents=True)
            for args in (["init", "-q"], ["remote", "add", "origin", f"https://github.com/{pid}.git"]):
                subprocess.run(["git", "-C", str(top), *args], check=True, capture_output=True)
            self.assertEqual(guard.current_project(str(top)), pid)
        self.made = []
        self.initiative("platform", [OWN])
        self.initiative("payments", [OTHER])
        self.plans = {k: self.file(f"plans/{rel}/2026-11-01-design.md", "# Design\n")
                      for k, rel in (("own", OWN), ("other", OTHER), ("member", "initiatives/platform"),
                                     ("stranger", "initiatives/payments"))}

    def file(self, rel, text):
        f = core.HANDOFF / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        self.addCleanup(f.unlink)
        return f

    def initiative(self, slug, members):
        self.file(f"initiatives/{slug}/README.md",
                  f"---\nkind: initiative\ntitle: {slug}\nprojects: [{', '.join(members)}]\n---\n# {slug}\n")

    def verdicts(self, cmd, cwd=None):
        """{agent payload: "allow" | "deny"} for `cmd` run in the session's checkout (or `cwd`)."""
        out = {}
        for tool, inp, agent in payloads(cmd):
            v = guard.check(tool, inp, str(cwd or self.top), agent)
            out[tool] = v[0] if v else "allow"
        return out

    def assert_rulings(self, refused, allowed, cwd=None):
        for cmd in refused:
            with self.subTest(refused=cmd):
                self.assertEqual(set(self.verdicts(cmd, cwd).values()), {"deny"}, self.verdicts(cmd, cwd))
        for cmd in allowed:
            with self.subTest(allowed=cmd):
                self.assertEqual(set(self.verdicts(cmd, cwd).values()), {"allow"}, self.verdicts(cmd, cwd))

    def test_a_plan_for_another_project_is_refused_and_one_for_its_own_is_not(self):
        p = self.plans
        self.assert_rulings(
            refused=[f"dev plan new 'Export design' -p {OTHER}", f"dev plan -p {OTHER} new x",
                     f"dev plan new x --project={OTHER}", f"dev plan new x -p {OWN} -p {OTHER}",
                     f"dev plan revise {p['other']} --why y", f"dev plan approve {p['other']}",
                     f"dev plan supersede {p['other']} --why y", "dev plan new x -i payments",
                     f"dev plan revise {p['stranger']} --why y",
                     f"cd {self.top} && dev plan revise ../../../{p['other'].relative_to(core.HOME)} --why y"],
            allowed=[f"dev plan new 'Export design' -p {OWN}", "dev plan new x",
                     f"dev plan revise {p['own']} --why y", f"dev plan approve {p['own']}",
                     f"dev plan revise {p['member']} --why y", "dev plan new x -i platform",
                     f"dev plan ls -p {OTHER}", f"dev plan -p {OTHER}"])

    def test_a_plan_path_found_only_at_run_time_is_refused(self):
        # "$P" may name any plan: the guard cannot tell whose it is
        self.assert_rulings(refused=['dev plan revise "$P" --why y', "dev plan approve $(ls plans/*.md)"],
                            allowed=[f"dev plan revise {self.plans['own']} --why y"])

    def test_the_session_log_and_note_of_another_project_are_refused(self):
        self.assert_rulings(
            refused=[f"dev handoff {OTHER} --log 'finished the export'", f"dev handoff {OTHER} --edit",
                     f"dev handoff --lo done {OTHER}", f"dev handoff --log=done {OTHER}",
                     f"dev handoff {OTHER.upper()[:-1]}x --log done"],
            allowed=[f"dev handoff {OWN} --log 'finished the export'", "dev handoff --log done",
                     f"dev handoff {OWN.upper()} --log done", f"dev handoff {OTHER}", f"dev handoff {OTHER} --path"])

    def test_lessons_and_the_delivery_policy_of_another_project_are_refused(self):
        self.assert_rulings(
            refused=[f"dev lesson add 'x' --cause c --prevent p -p {OTHER}", f"dev lesson recur L-1 -p {OTHER}",
                     f"dev lesson -p {OTHER} retire L-1 --why w", f"dev delivery apply -p {OTHER} --why w",
                     f"dev delivery -p {OTHER} apply --why w", "dev lesson add 'x' -i payments",
                     f"dev note set status=blocked -p {OTHER}", f"dev task add x -p {OTHER} --why y"],
            allowed=[f"dev lesson ls -p {OTHER}", f"dev lesson -p {OTHER} lint", f"dev lesson show L-1 -p {OTHER}",
                     f"dev delivery -p {OTHER}", f"dev delivery apply -p {OWN} --why w",
                     f"dev lesson add 'x' --cause c --prevent p -p {OWN}", "dev lesson add 'x' -i platform",
                     f"dev note set status=blocked -p {OWN}"])

    def test_the_session_decides_the_project_not_the_folder_a_command_moves_to(self):
        # the session started in acme/widget: a cd, pushd, env -C, subshell or shell line that moves the command to
        # another folder does not make another project's handoff files its own
        p, o = self.plans, self.other_top
        self.assert_rulings(
            refused=[f"cd /tmp && dev plan new x -p {OTHER}", f"cd ~ && dev handoff {OTHER} --log x",
                     f'cd "$D" && dev plan new x -p {OTHER}', f'cd "$D" && dev handoff {OTHER} --log x',
                     'cd "$D" && dev handoff --log x', f"cd {core.DEV} && dev lesson recur L-1 -p {OTHER}",
                     f"cd {core.HANDOFF} && dev plan revise plans/{OTHER}/2026-11-01-design.md --why y",
                     "cd ../gadget && dev handoff --log x", "cd ../gadget && dev task add x --why y",
                     "pushd ../gadget && dev handoff --log x", f"env -C {o} dev handoff --log x",
                     "(cd ../gadget; dev handoff --log x)", "sh -c 'cd ../gadget && dev handoff --log x'",
                     f"cd ~ && echo x > {p['other'].parent}/z.md"],
            allowed=[f"cd /tmp && dev plan new x -p {OWN}", f"cd ~ && dev handoff {OWN} --log x",
                     f'cd "$D" && dev plan new x -p {OWN}', f'cd "$D" && dev handoff {OWN} --log x',
                     f"cd {core.DEV} && dev lesson recur L-1 -p {OWN}",
                     f"cd {core.HANDOFF} && dev plan revise plans/{OWN}/2026-11-01-design.md --why y",
                     "cd ../widget && dev handoff --log x", "cd . && dev task add x --why y",
                     f"pushd {self.top} && dev handoff --log x", f"env -C {self.top} dev handoff --log x",
                     "(cd .; dev handoff --log x)", "sh -c 'cd . && dev handoff --log x'",
                     f"cd ~ && echo x > {p['own'].parent}/z.md", "cd ../gadget && dev handoff",
                     "cd ../gadget && dev plan ls"])

    def test_a_cd_to_the_checkouts_top_folder_found_by_git_keeps_the_sessions_project(self):
        # git rev-parse prints the top folder of the checkout the shell is in: the guard finds it by walking up to
        # the .git folder, as git does, without running anything; quoted and unquoted forms are read alike
        sub = self.top / "lib" / "deep"
        sub.mkdir(parents=True)
        writes = ["dev task add x --why y", "dev task done T-1 --why y", "dev handoff --log x",
                  "dev lesson add 'x' --cause c --prevent p", "dev plan new x"]
        moves = ['cd "$(git rev-parse --show-toplevel)"', "cd $(git rev-parse --show-toplevel)",
                 'cd "`git rev-parse --show-toplevel`"', 'cd "$(git rev-parse --show-cdup)"',
                 'cd "$(git rev-parse --git-common-dir)/.."', 'pushd "$(git rev-parse --show-toplevel)"',
                 'cd "$(git rev-parse --show-toplevel)/lib"']
        allowed = [f"{m} && {w}" for m in moves for w in writes]
        self.assert_rulings(refused=[], allowed=allowed)
        self.assert_rulings(refused=[], allowed=allowed, cwd=sub)  # a session started below the top folder
        # the twins: a substitution that lands in another project, one the guard cannot work out, and one whose git
        # the command itself changes (its environment, PATH or a function) stay refused
        self.assert_rulings(
            refused=['cd ../gadget && cd "$(git rev-parse --show-toplevel)" && dev handoff --log x',
                     'cd ../gadget && cd $(git rev-parse --show-toplevel) && dev task add x --why y',
                     'cd "$(git -C ../gadget rev-parse --show-toplevel)" && dev handoff --log x',
                     "cd $(git -C ../gadget rev-parse --show-toplevel) && dev handoff --log x",
                     'cd "$(echo ../gadget)" && dev handoff --log x', 'cd "$D" && dev handoff --log x',
                     'export GIT_WORK_TREE=../gadget; cd "$(git rev-parse --show-toplevel)" && dev handoff --log x',
                     'GIT_DIR=../gadget/.git cd "$(git rev-parse --show-toplevel)" && dev handoff --log x',
                     'git() { echo ../gadget; }; cd "$(git rev-parse --show-toplevel)" && dev handoff --log x',
                     'PATH=/tmp/bin:$PATH; cd "$(git rev-parse --show-toplevel)" && dev handoff --log x',
                     'cd "$(git rev-parse --show-toplevel)$X" && dev handoff --log x'],
            allowed=['cd "$(git rev-parse --show-toplevel)" && dev handoff --log x', "cd ../gadget && dev handoff",
                     'cd "$(echo ../gadget)" && dev plan ls'])

    def test_show_cdup_is_read_as_the_shell_builds_the_word(self):
        # at the top folder git prints nothing for --show-cdup, so "$(…)/abs" is the absolute folder /abs (bash and
        # zsh went to /tmp from `cd "$(git rev-parse --show-cdup)/tmp"`); below it git prints `../../`, and the same
        # word is a folder below the top
        sub = self.top / "lib" / "deep"
        sub.mkdir(parents=True)
        at_other, at_own = f'cd "$(git rev-parse --show-cdup){self.other_top}"', \
            f'cd "$(git rev-parse --show-cdup){self.top}"'
        lib = 'cd "$(git rev-parse --show-cdup)/lib" && dev task add x --why y'  # /lib at the top: no project
        self.assert_rulings(refused=[f"{at_other} && dev handoff --log x", f"{at_other} && dev task add x --why y"],
                            allowed=[f"{at_own} && dev handoff --log x", f"{at_other} && dev handoff", lib])
        self.assert_rulings(refused=[], allowed=[f"{at_other} && dev handoff --log x", lib], cwd=sub)

    def test_a_git_folder_is_worked_out_only_for_the_cd_that_starts_the_command(self):
        # whatever runs before the cd may replace git: zsh runs a function named 'git' or one set in
        # functions[git] (both printed FAKE for `git rev-parse --show-toplevel`), and a sourced file may export
        # GIT_DIR, so the folder stays unknown and a write is refused
        top = 'cd "$(git rev-parse --show-toplevel)"'
        self.assert_rulings(
            refused=[f"'git'() {{ echo ../gadget; }}; {top} && dev handoff --log x",
                     f"functions[git]='echo ../gadget'; {top} && dev handoff --log x",
                     f". ./env.sh; {top} && dev handoff --log x", f"source ./env.sh && {top} && dev task add x --why y",
                     f"ls; {top} && dev handoff --log x", f"X=1 {top} && dev handoff --log x",
                     f"cd . && {top} && dev handoff --log x"],
            allowed=[f"{top} && dev handoff --log x", f"  {top} && dev task add x --why y",
                     f"{top} && cd lib && dev handoff --log x", f"ls; {top} && dev handoff"])

    def test_a_cd_option_is_not_the_folder_it_moves_to(self):
        # -L, -P, -e, -@ and `--` come before the folder; pushd -n moves none, and pushd +N turns to one not shown
        self.assert_rulings(
            refused=["cd -P ../gadget && dev handoff --log x", "cd -L ../gadget && dev task add x --why y",
                     "cd -- ../gadget && dev handoff --log x", "cd -Pe ../gadget && dev plan new x",
                     "pushd -- ../gadget && dev handoff --log x", "pushd +1 && dev handoff --log x",
                     'cd -P "$(git -C ../gadget rev-parse --show-toplevel)" && dev handoff --log x'],
            allowed=["cd -P . && dev handoff --log x", "cd -L ../widget && dev task add x --why y",
                     "cd -- . && dev handoff --log x", "pushd -n ../gadget && dev handoff --log x",
                     'cd -P "$(git rev-parse --show-toplevel)" && dev handoff --log x'])

    def test_a_checkout_whose_git_config_moves_its_work_tree_is_not_worked_out(self):
        # core.worktree points git's top folder elsewhere, and an included file may hold it: the guard does not
        # follow either, so the folder stays unknown; the same checkout without it is worked out
        cmd = 'cd "$(git rev-parse --show-toplevel)" && dev handoff --log x'
        self.assert_rulings(refused=[], allowed=[cmd])
        subprocess.run(["git", "-C", str(self.top), "config", "core.worktree", str(self.other_top)], check=True)
        self.assert_rulings(refused=[cmd], allowed=[])

    def test_a_codex_workdir_is_where_the_command_runs_not_whose_session_it_is(self):
        o = str(self.other_top)
        for inp, want in (({"cmd": "dev handoff --log x", "workdir": o}, "deny"),
                          ({"cmd": f"dev plan new x -p {OTHER}", "workdir": "/tmp"}, "deny"),
                          ({"command": ["bash", "-lc", f"dev plan new x -p {OTHER}"], "workdir": "/tmp"}, "deny"),
                          ({"cmd": f"echo x > {self.plans['other'].parent}/z.md", "workdir": "/tmp"}, "deny"),
                          ({"cmd": "dev handoff --log x", "workdir": str(self.top)}, "allow"),
                          ({"cmd": f"dev plan new x -p {OWN}", "workdir": "/tmp"}, "allow"),
                          ({"command": ["bash", "-lc", "dev handoff --log x"], "workdir": "."}, "allow"),
                          ({"cmd": f"echo x > {self.plans['own'].parent}/z.md", "workdir": "/tmp"}, "allow"),
                          # a relative path is read from the workdir, as the command reads it
                          ({"cmd": "echo x > z.md", "workdir": str(self.plans["other"].parent)}, "deny"),
                          ({"cmd": "echo x > z.md", "workdir": str(self.plans["own"].parent)}, "allow"),
                          ({"cmd": "rm -rf Development", "workdir": str(core.HOME)}, "deny"),
                          ({"cmd": "rm -rf Development", "workdir": "/tmp"}, "allow")):
            tool = "shell" if "command" in inp else "exec_command"
            with self.subTest(inp=inp):
                v = guard.check(tool, inp, str(self.top), "codex")
                self.assertEqual(v[0] if v else "allow", want, v)

    def test_the_refusal_names_a_session_of_that_project_not_its_folder(self):
        for cmd in (f"dev plan new x -p {OTHER}", f"dev handoff {OTHER} --log x", "cd ../gadget && dev handoff --log x",
                    f"dev plan revise {self.plans['other']} --why y"):
            with self.subTest(cmd=cmd):
                v = guard.check("Bash", {"command": cmd}, str(self.top), "claude")
                self.assertEqual(v[0], "deny")
                self.assertNotIn("from that project's folder", v[1])
                self.assertIn("dev session tell", v[1])


    def test_words_that_arrive_at_run_time_cannot_name_the_project(self):
        # xargs, find -exec and parallel add words the command line does not show: a write may be another project's
        o, own = self.plans["other"], self.plans["own"]
        self.assert_rulings(
            refused=[f"echo {OTHER} | xargs dev handoff --log x", f"echo {o} | xargs dev plan revise --why y",
                     f"find {o.parent} -name a.md -exec dev plan revise {{}} --why y \\;",
                     f"find {o.parent} -name a.md -exec dev plan approve {{}} +",
                     f"parallel dev plan approve ::: {o}", f"echo x | xargs dev task add y --why z",
                     "dev plan revise {} --why y"],
            allowed=[f"echo {OTHER} | xargs dev handoff", f"echo {o} | xargs dev plan ls",
                     f"find {o.parent} -name a.md -exec dev lesson show {{}} \\;", "parallel dev lesson show ::: L-1",
                     f"find {own.parent} -name a.md -exec cat {{}} \\;", f"dev plan revise {own} --why y"])

    def test_the_own_project_named_by_alias_or_folder_is_its_own(self):
        # dev reads a project value as an id, a folder of its checkout, or a name or alias (registry.match)
        self.assert_rulings(
            refused=["dev handoff gadget --log x", "dev plan new x -p gadget", "dev handoff ../gadget --log x",
                     f"dev plan new x -p {self.other_top}", "dev handoff wid* --log x"],
            allowed=["dev handoff widget --log x", "dev handoff . --log x", "dev plan new x -p widget",
                     "dev plan new x -p WIDGET", f"dev plan new x -p {self.top}"])
        # a part of a name: dev matches it against the projects it finds when it runs, which may hold one cloned
        # since the guard's registry was written (a `wid` project would take the write), so it is never the own one
        self.assert_rulings(
            refused=["dev handoff wid --log x", "dev plan new x -p widg", "dev task add x -p WID --why y",
                     "dev handoff idge --log x", "dev lesson recur L-1 -p dget"],
            allowed=["dev handoff wid", "dev plan ls -p wid", f"dev handoff {OWN.upper()} --log x"])
        for cmd in ("dev handoff wid --log x", "dev plan new x -p widg"):
            with self.subTest(hint=cmd):
                why = guard.check("Bash", {"command": cmd}, str(self.top), "claude")[1]
                self.assertIn(f"name this session's project as `{OWN}`", why)
        # the team's aliases and the projects dev sync last recorded: each alias names its own project only
        aliases = {"wdg": {"project": OWN, "by": "t", "at": "2026-11-01"},
                   "gdg": {"project": OTHER, "by": "t", "at": "2026-11-02"}}
        self.file("aliases.json", json.dumps({"version": 1, "aliases": aliases}))
        known = [{"id": OTHER, "name": "gadget", "worktrees": []},
                 {"id": "acme/widgets", "name": "widgets", "worktrees": []}]
        self.file("registry.json", json.dumps({"projects": known}))
        self.assert_rulings(refused=["dev handoff gdg --log x", "dev plan new x -p gadget", "dev handoff wid --log x"],
                            allowed=["dev handoff wdg --log x", "dev plan new x -p widget"])

    def test_a_substitution_inside_the_command_hides_none_of_its_words(self):
        # an unquoted $(…) is one word of the dev command: the options after it are still the command's own
        self.assert_rulings(
            refused=[f"dev handoff {OTHER} $(true) --log x", f"dev task add x $(true) -p {OTHER} --why y",
                     f"dev plan new x $(true) -p {OTHER}", f"dev lesson recur L-1 $(true) -p {OTHER}",
                     f"dev delivery apply $(true) -p {OTHER} --why w", f"dev handoff $(echo {OTHER}) --log x"],
            allowed=[f"dev handoff {OWN} $(true) --log x", f"dev task add x $(true) -p {OWN} --why y",
                     'dev plan new "Plan $(date +%F)"', 'dev handoff --log "$(git log -1 --format=%s)"',
                     f"dev plan ls $(true) -p {OTHER}"])

    def test_a_portfolio_session_and_the_developers_hatch_may_write_any_project(self):
        cmds = [f"dev plan new x -p {OTHER}", f"dev handoff {OTHER} --log done", f"dev lesson recur L-1 -p {OTHER}",
                f"env -C {self.other_top} dev handoff --log done"]
        self.assert_rulings(refused=[], allowed=cmds, cwd=core.DEV)  # a session started in ~/Development
        for cmd in cmds:
            with self.subTest(hatch=cmd):
                v = guard.check("Bash", {"command": f"{HATCH}=1 {cmd}"}, str(self.top), "claude")
                self.assertIsNone(v)
                self.assertEqual([(b["flag"], b["rule"]) for b in guard.last_bypasses()], [(HATCH, "cross-project")])


if __name__ == "__main__":
    unittest.main()
