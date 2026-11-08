# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# `dev doctor`'s crew checks (lib/devkit/setup.py): each skill the crew names but this Mac has not installed for an
# agent (selfcheck D8) shows as a failed check with the line that fixes it. The tests use a small roster and agent
# folders of their own, never the developer's. Run: python3 -m unittest tests.test_doctor
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import test_work  # noqa: E402  (TempHandoff: work items in a temporary handoff repository)
from devkit import selfcheck, setup, work  # noqa: E402

A = {"user": "ana", "host": "mbp1", "agent": "claude", "session": "s1"}

ROSTER = {"members": [{"id": "niobe", "skills": ["testing", "terraform-style-guide"],
                       "procedure": [{"phase": "Plan", "skills": ["superpowers:brainstorming"]}]}],
          "activities": [{"id": "iac", "knowledge": {"skills": ["testing"]}}]}


class CrewSkillCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def agent(self, name, *skills):
        # An agent's home and skills folder with these skills installed (`plugin:skill` in the plugin cache).
        home, sdir = self.root / name, self.root / name / "skills"
        for s in skills:
            f = (home / "plugins/cache/mk" / s.split(":")[0] / "1.0/skills" / s.split(":")[1] / "SKILL.md") \
                if ":" in s else sdir / s / "SKILL.md"
            f.parent.mkdir(parents=True)
            f.write_text("x")
        return home, sdir

    def test_a_crew_skill_missing_for_an_agent_fails_with_its_fix(self):
        # iac is turned on here, so its skill is owed (Morpheus, M199: a group the developer asked for must install)
        homes = {"claude": self.agent("claude", "testing", "superpowers:brainstorming")}
        [(group, name, ok, detail, fix)] = setup.crew_skill_checks(["claude"], roster=ROSTER, homes=homes,
                                                                   groups=["iac"])
        self.assertEqual((group, name, ok), ("Agents", "crew skills (claude)", False))
        self.assertIn("terraform-style-guide", detail)
        self.assertIn("1 of 3", detail)
        self.assertIn("skills.groups", fix)
        self.assertIn("dev install --only skills", fix)

    def test_a_skill_of_an_opt_in_group_that_is_off_only_informs(self):
        # Morpheus, M199: the row passes, and says who names the skill and the line that turns its group on
        homes = {"claude": self.agent("claude", "testing", "superpowers:brainstorming")}
        [(_, _, ok, detail, fix)] = setup.crew_skill_checks(["claude"], roster=ROSTER, homes=homes, groups=[])
        self.assertTrue(ok)
        self.assertIn("terraform-style-guide", detail)
        self.assertIn("niobe", detail)
        self.assertIn('add "iac" to skills.groups', detail)
        self.assertEqual(fix, "")

    def test_a_missing_skill_of_a_default_group_fails_even_when_the_group_is_off(self):
        roster = {"members": [{"id": "dozer", "skills": ["supabase-postgres-best-practices"]}], "activities": []}
        homes = {"claude": self.agent("claude")}
        [(_, _, ok, detail, _)] = setup.crew_skill_checks(["claude"], roster=roster, homes=homes, groups=[])
        self.assertFalse(ok)
        self.assertIn("supabase-postgres-best-practices (postgres)", detail)

    def test_every_crew_skill_installed_passes_for_each_agent(self):
        everything = ("testing", "terraform-style-guide", "superpowers:brainstorming")
        homes = {"claude": self.agent("claude", *everything), "codex": self.agent("codex", *everything)}
        rows = setup.crew_skill_checks(["claude", "codex"], roster=ROSTER, homes=homes)
        self.assertEqual([(n, ok) for _, n, ok, _, _ in rows], [("crew skills (claude)", True),
                                                               ("crew skills (codex)", True)])
        self.assertIn("3 skills", rows[0][3])

    def test_a_missing_plugin_skill_names_the_agents_install(self):
        homes = {"codex": self.agent("codex", "testing", "terraform-style-guide")}
        [(_, _, ok, detail, fix)] = setup.crew_skill_checks(["codex"], roster=ROSTER, homes=homes)
        self.assertFalse(ok)
        self.assertIn("superpowers:brainstorming", detail)
        self.assertEqual(fix, "dev install --only codex")


class RequiredToolsTests(unittest.TestCase):
    # The required-tools row follows the stacks the wizard recorded (config `stacks`): the base tools on every Mac,
    # a stack's tools only when the developer chose it. `have` stands in for PATH, so the developer's Mac never counts.
    JAVA_PHP = {"java", "mvn", "gradle", "jdtls", "php", "composer", "intelephense"}

    def setUp(self):
        # both agents on (the default), whatever the developer's own config says
        patch = mock.patch.dict(setup.CFG, {"agents": {}})
        patch.start()
        self.addCleanup(patch.stop)

    def test_python_and_node_without_java_or_php_tools_pass(self):
        # the Recommended setup: 24 tools before, 7 of them for the Java and PHP stacks nobody chose
        group, name, ok, detail, fix = setup.tools_check(["python", "node"], have=lambda t: t not in self.JAVA_PHP)
        self.assertEqual((group, name, ok), ("Toolchain", "required tools", True))
        self.assertIn("17/17 present", detail)
        self.assertIn("not chosen: Java / Spring, PHP", detail)
        self.assertIn("dev setup", detail)
        self.assertEqual(fix, "")

    def test_java_chosen_without_maven_fails_with_its_install_command(self):
        php = {"php", "composer", "intelephense"}
        _, _, ok, detail, fix = setup.tools_check(["java", "python", "node"],
                                                  have=lambda t: t != "mvn" and t not in php)
        self.assertFalse(ok)
        self.assertIn("missing: mvn", detail)
        self.assertEqual(fix, "brew install maven")

    def test_a_missing_language_server_names_its_npm_install(self):
        _, _, ok, detail, fix = setup.tools_check(["python"], have=lambda t: t != "pyright")
        self.assertFalse(ok)
        self.assertIn("missing: pyright", detail)
        self.assertEqual(fix, "npm install -g pyright")

    def test_an_agent_turned_off_in_config_is_not_required(self):
        # config agents.codex false, as `dev setup` records it: `dev install` skips Codex, so doctor must not want it
        with mock.patch.dict(setup.CFG, {"agents": {"claude": True, "codex": False}}):
            _, _, ok, detail, fix = setup.tools_check(["python"], have=lambda t: t != "codex")
        self.assertTrue(ok)
        self.assertIn("turned off: Codex", detail)
        self.assertEqual(fix, "")

    def test_an_agent_left_on_stays_required_when_the_other_is_off(self):
        with mock.patch.dict(setup.CFG, {"agents": {"claude": False}}):
            _, _, ok, detail, fix = setup.tools_check(["python"], have=lambda t: t not in ("claude", "codex"))
        self.assertFalse(ok)
        self.assertIn("missing: codex", detail)
        self.assertNotIn("claude", detail.split("missing: ")[1].split(" · ")[0])
        self.assertEqual(fix, "brew install --cask codex")


class CrewRaciCheckTests(unittest.TestCase):
    BASE = {"activities": [{"id": "api", "R": ["tank"], "A": "seraph", "C": [], "I": []}]}

    def test_a_raci_error_the_team_added_fails_with_the_command_that_changes_it(self):
        team = {"activities": [{"id": "api", "R": ["tank"], "A": "seraph", "C": ["seraph"], "I": []}]}
        [(group, name, ok, detail, fix)] = setup.crew_raci_checks(base=self.BASE, team=team)
        self.assertEqual((group, name, ok), ("Agents", "crew RACI (team changes)", False))
        self.assertIn("api: A seraph in C", detail)
        self.assertIn("dev crew raci api --set", fix)

    def test_team_changes_without_raci_errors_pass(self):
        [(_, _, ok, _, _)] = setup.crew_raci_checks(base=self.BASE, team=self.BASE)
        self.assertTrue(ok)


def git(top, *args, at=None):
    # git in a test repository, with a fixed author and, with `at`, the commit's date (epoch seconds)
    env = dict(os.environ, GIT_AUTHOR_DATE=f"@{at} +0000", GIT_COMMITTER_DATE=f"@{at} +0000") if at else None
    subprocess.run(["git", "-C", str(top), "-c", "user.name=Ana", "-c", "user.email=ana@example.com", *args], check=True,
                   capture_output=True, env=env)


def run(rid, project, *, started, moved=None, state="running"):
    # a progress run as stored: one step, started at `started`, whose last state change was at `moved`
    agent, session = rid.split("-", 1)
    hist = [{"ts": moved, "step": "build", "from": "pending", "to": "running", "by": "main"}] if moved else []
    return {"v": 2, "agent": agent, "session": session, "project": project, "goal": f"Goal of {session}", "state": state,
            "started": started, "plan": [{"name": "build", "state": "running", "owner": "main", "est": 30,
                                          "started": moved or started}], "history": hist, "decisions": [],
            "messages": [], "agents": {}, "updates": [], "intervals": []}


class WorkStateTests(unittest.TestCase):
    # dev doctor's "work state" row and the dashboard's Health list (selfcheck.work_state): what is stale in the
    # current project's work items and runs, each with the command that fixes it; reading only, never writing
    def setUp(self):
        self.t = int(time.time())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.top = Path(self.tmp.name) / "api"
        self.top.mkdir()
        git(self.top, "init", "-q", "-b", "main")
        git(self.top, "commit", "-q", "--allow-empty", "-m", "start", at=self.t - 50 * 3600)
        git(self.top, "commit", "-q", "--allow-empty", "-m", "release", at=self.t - 30 * 3600)
        git(self.top, "tag", "v26.11.5")
        self.h = test_work.TempHandoff().__enter__()
        self.addCleanup(self.h.__exit__)

    def merge(self, branch, pr, at):
        git(self.top, "checkout", "-q", "-b", branch)
        git(self.top, "commit", "-q", "--allow-empty", "-m", "work", at=at - 60)
        git(self.top, "checkout", "-q", "main")
        git(self.top, "merge", "-q", "--no-ff", branch, "-m", f"Merge pull request #{pr} from acme/{branch}", at=at)

    def backlog(self):
        # a doing item with no lock, an open item of a branch merged after its claim, and two items with one title
        lockless = work.create("acme/api", "Fix login", a=A)
        lockless["meta"]["status"] = "doing"
        work.write_record(lockless["path"], work.render(lockless))
        merged = work.claim(work.create("acme/api", "Export invoices", a=A), a=A, branch="feat/export")
        dups = [work.create("acme/api", "Add the CSV export", a=A), work.create("acme/api", "add the  CSV export.", a=A)]
        self.merge("feat/export", 7, self.t + 60)
        return lockless, merged, dups

    def test_stale_items_are_listed_with_the_command_that_fixes_them_and_nothing_is_written(self):
        lockless, merged, dups = self.backlog()
        files = {p: p.read_text() for p in (work.ITEMS / "acme" / "api").glob("*.md")}
        r = selfcheck.work_state("acme/api", self.top, runs=[], now=self.t)
        self.assertEqual([x["kind"] for x in r["rows"]], ["lockless_doing", "merged", "duplicates"])
        rows = {x["kind"]: x for x in r["rows"]}
        self.assertEqual(rows["lockless_doing"]["text"], f"1 doing without a lock: {lockless['meta']['id']} Fix login")
        self.assertEqual(rows["merged"]["text"], f"1 open, but its branch is merged: {merged['meta']['id']} Export invoices "
                                                 "(feat/export, PR #7)")
        self.assertEqual([rows[k]["fix"] for k in ("lockless_doing", "merged")], ["dev task -p acme/api reconcile --apply"] * 2)
        first, second = sorted(d["meta"]["id"] for d in dups)
        self.assertEqual(rows["duplicates"]["fix"], f'dev task -p acme/api cancel {second} --why "duplicate of {first}"')
        self.assertEqual(r["count"], 3)                                                  # items, title groups and runs
        self.assertEqual({p: p.read_text() for p in files}, files)                       # reading only

    def test_a_run_is_stale_after_lock_hours_without_a_step_change_or_when_it_began_before_the_latest_release(self):
        t = self.t
        runs = [run("claude-quiet", "acme/api", started=t - 20 * 3600, moved=t - 10 * 3600),
                run("claude-pre", "acme/api", started=t - 40 * 3600, moved=t - 3600),
                run("claude-fresh", "acme/api", started=t - 2 * 3600, moved=t - 1800),
                run("claude-web", "acme/web", started=t - 40 * 3600),
                run("claude-shipped", "acme/api", started=t - 40 * 3600, state="done")]
        r = selfcheck.work_state("acme/api", self.top, runs=runs, now=t, lock_hours=8)
        self.assertEqual([x["text"] for x in r["rows"]],
                         ["Run “Goal of pre” (claude-pre): began before v26.11.5, the latest release",
                          "Run “Goal of quiet” (claude-quiet): no step change for 10 h"])
        self.assertEqual({x["fix"] for x in r["rows"]}, {'dev progress done "<outcome>" in its session, or dev progress step'})

    def test_doctor_shows_the_work_state_row_with_its_fixes_and_passes_when_nothing_is_stale(self):
        self.backlog()
        quiet = run("claude-quiet", "acme/api", started=self.t - 20 * 3600, moved=self.t - 10 * 3600)
        [(group, name, ok, detail, fix)] = selfcheck.work_state_checks(self.top, "acme/api", runs=[quiet], now=self.t)
        self.assertEqual((group, name, ok), ("Workspace", "work state", False))
        self.assertTrue(detail.startswith("acme/api: 1 doing without a lock"), detail)
        self.assertIn("no step change for 10 h", detail)
        self.assertEqual(fix.split(" · ")[0], "dev task -p acme/api reconcile --apply")
        self.assertIn("dev progress done", fix)
        [(_, _, ok, detail, fix)] = selfcheck.work_state_checks(self.top, "acme/web", runs=[quiet], now=self.t)
        self.assertEqual((ok, detail, fix), (True, "acme/web: no stale work items or runs", ""))

    # the dashboard's Health view: the same rows for each project cloned here that has open work, and git read at most
    # once per project in five minutes (the page rebuilds its data on every file change)
    def test_the_dashboard_lists_each_projects_work_state_and_reads_git_once_per_project(self):
        from devkit import dashboard
        self.backlog()
        quiet = run("claude-quiet", "acme/api", started=self.t - 20 * 3600, moved=self.t - 10 * 3600)
        reg = {"projects": [{"id": "acme/api", "path": str(self.top), "local": True, "git": True},
                            {"id": "acme/idle", "path": str(self.top), "local": True, "git": True},
                            {"id": "acme/remote", "path": None, "local": False, "git": True}]}
        items = dashboard.read_items(self.h.root, self.t)
        calls = []
        real = selfcheck.merges
        memo = {}
        with mock.patch.object(selfcheck, "merges", lambda top: calls.append(top) or real(top)):
            ws = dashboard.work_drift(reg, items, runs=[quiet], now=self.t, memo=memo)
            again = dashboard.work_drift(reg, items, runs=[quiet], now=self.t + 60, memo=memo)
        self.assertEqual([p["project"] for p in ws["projects"]], ["acme/api"])          # acme/idle has no open work
        self.assertEqual([x["kind"] for x in ws["projects"][0]["rows"]], ["lockless_doing", "merged", "duplicates", "run"])
        self.assertEqual((ws["checked"], ws["count"]), (1, 4))
        self.assertEqual(again["projects"], ws["projects"])
        self.assertEqual(len(calls), 1)


class BranchCleanupCheckTests(unittest.TestCase):
    # dev doctor reads GitHub's delete_branch_on_merge for every GitHub project on this Mac in one GraphQL call: each
    # one with it off that the developer administers fails with the command that turns it on; one they do not
    # administer is only named (they cannot change it). gh is replaced.
    PROJECTS = [("acme/api", "acme/api"), ("acme/web", "acme/web"), ("other/lib", "other/lib")]

    def checks(self, data):
        calls = []

        def gh(args):
            calls.append(list(args))
            return 0, json.dumps({"data": data}), ""
        return setup.branch_cleanup_checks(self.PROJECTS, gh=gh), calls

    def test_a_project_with_it_off_fails_with_the_command_that_turns_it_on(self):
        rows, calls = self.checks({"r0": {"deleteBranchOnMerge": False, "viewerPermission": "ADMIN"},
                                   "r1": {"deleteBranchOnMerge": True, "viewerPermission": "ADMIN"},
                                   "r2": {"deleteBranchOnMerge": True, "viewerPermission": "WRITE"}})
        failing = [(name, fix) for _, name, ok, _, fix in rows if not ok]
        self.assertEqual(failing, [("delete merged branches · acme/api",
                                    'dev delivery apply -p acme/api --why "delete the branch of each merged PR"')])
        self.assertEqual(len(calls), 1)

    def test_a_project_you_do_not_administer_is_named_without_failing(self):
        rows, _ = self.checks({"r0": {"deleteBranchOnMerge": True, "viewerPermission": "ADMIN"},
                               "r1": {"deleteBranchOnMerge": True, "viewerPermission": "MAINTAIN"},
                               "r2": {"deleteBranchOnMerge": False, "viewerPermission": "WRITE"}})
        self.assertEqual([ok for _, _, ok, _, _ in rows], [True])
        self.assertIn("off in other/lib, which you do not administer", rows[0][3])


if __name__ == "__main__":
    unittest.main()
