# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Project aliases and the readable terminal (spec P-fxs316lh3nw, R1, R2, R3 and R11): what is on this Mac, short
# names shared by the team, dev status as Sati designed it and dev config as a report. Every test builds its own
# registry, handoff folder and state folder in a temp directory; nothing reads the developer's projects or config.
import argparse
import contextlib
import io
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

from devkit import canary, cli, core, crew, policy, progress, registry, ui, work  # noqa: E402

ROSTER = crew.load(team=False)  # devkit's own roster, from the checkout


class Sandbox(unittest.TestCase):
    # A temp handoff repository, state folder and two folders that stand for checkouts on this Mac.
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.handoff, self.state = root / "handoff", root / "state"
        self.handoff.mkdir()
        self.state.mkdir()
        self.saved = (core.HANDOFF, work.HANDOFF, core.STATE, registry.STATE, canary.STATE, canary.LOG)
        core.HANDOFF = work.HANDOFF = self.handoff
        core.STATE = registry.STATE = canary.STATE = self.state
        canary.LOG = self.state / "canary.jsonl"
        self.devkit_dir, self.kit_dir = root / "devkit", root / "kit-consulting"
        self.devkit_dir.mkdir()
        self.kit_dir.mkdir()

    def tearDown(self):
        core.HANDOFF, work.HANDOFF, core.STATE, registry.STATE, canary.STATE, canary.LOG = self.saved
        self.tmp.cleanup()

    def project(self, pid, path=None, branch="main", worktrees=(), canonical_ok=True, **main):
        owner, _, name = pid.rpartition("/")
        local = path is not None
        co = dict({"branch": branch, "path": str(path), "modified": 0, "untracked": 0, "ahead": 0, "behind": 0,
                   "unpushed": 0, "last_commit": "fix: a thing", "last_commit_ts": 0}, **main) if local else {}
        return {"id": pid, "owner": owner, "name": name, "path": str(path) if local else None, "local": local,
                "main": co, "worktrees": list(worktrees), "canonical_ok": canonical_ok,
                "canonical": str(path) if local else "", "stack": [], "commands": [], "prs_open": [],
                "prs_known": False, "last_activity": int(time.time()) - 300, "size_kb": None, "status_line": None,
                "remote": "", "default_branch": "main"}

    def reg(self, *projects, issues=()):
        return {"projects": list(projects), "issues": list(issues), "sessions": []}

    def mac(self):
        # This Mac today: devkit and kit-consulting checked out, flyquery only in the portfolio.
        return self.reg(self.project("fireflysoftwaresolutions/devkit", self.devkit_dir, "release/26.11.3",
                                     ahead=78, untracked=1),
                        self.project("fireflysoftwaresolutions/kit-consulting", self.kit_dir, modified=2, behind=169),
                        self.project("fireflyframework/flyquery"))


def run_cli(fn, ns, reg=None):
    out, err = io.StringIO(), io.StringIO()
    rc = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch.object(cli, "_reg", lambda **kw: reg):
        try:
            rc = fn(ns)
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------- R1: what is on this Mac

class LocalTests(Sandbox):
    def test_a_checkout_is_local_and_a_portfolio_project_is_not(self):
        reg = self.mac()
        self.assertEqual([p["name"] for p in reg["projects"] if registry.is_local(p)], ["devkit", "kit-consulting"])

    def test_a_project_whose_folder_is_gone_is_not_local(self):
        p = self.project("acme/gone", Path(self.tmp.name) / "gone")
        self.assertFalse(registry.is_local(p))

    def test_ls_lists_only_this_mac_and_counts_the_portfolio(self):
        rc, out, _ = run_cli(cli.cmd_ls, argparse.Namespace(offline=True, names=False, all=False, json=False),
                             self.mac())
        self.assertEqual(rc, 0)
        self.assertIn("Projects on this Mac · 2", out)
        self.assertIn("kit-consulting", out)
        self.assertNotIn("flyquery", out)
        self.assertIn("1 more project in your portfolio is not on this Mac: dev ls --all", out)

    def test_ls_all_lists_the_portfolio_by_owner_with_the_clone_command(self):
        rc, out, _ = run_cli(cli.cmd_ls, argparse.Namespace(offline=True, names=False, all=True, json=False),
                             self.mac())
        self.assertEqual(rc, 0)
        self.assertIn("In your portfolio, not on this Mac · 1", out)
        self.assertIn("fireflyframework · 1", out)
        self.assertIn("dev clone flyquery", out)

    def test_ls_with_nothing_on_this_mac_says_how_to_get_a_project(self):
        rc, out, _ = run_cli(cli.cmd_ls, argparse.Namespace(offline=True, names=False, all=False, json=False),
                             self.reg(self.project("fireflyframework/flyquery")))
        self.assertIn("No projects on this Mac yet.", out)
        self.assertIn("dev clone <name>", out)

    def test_du_measures_only_projects_on_this_mac(self):
        self.assertEqual([r[0] for r in cli._du_rows(self.mac())], ["devkit", "kit-consulting"])

    def test_status_outside_a_project_lists_this_mac_only(self):
        lines = cli.render_home(cli.home_view(self.mac(), runs=[], name="Andrés"), cols=80)
        text = "\n".join(lines)
        self.assertIn("On this Mac: 2 projects", text)
        self.assertNotIn("flyquery", text)
        self.assertIn("1 more project in your portfolio is not on this Mac: dev ls --all", text)


# --------------------------------------------------------------------------- R2: aliases and matching

class AliasTests(Sandbox):
    def test_the_alias_is_the_repository_name_when_it_is_unique(self):
        reg = self.reg(self.project("acme/api", self.devkit_dir), self.project("other/api"),
                       self.project("acme/devkit", self.kit_dir))
        self.assertEqual(registry.alias_map(reg["projects"]),
                         {"acme/api": "acme/api", "other/api": "other/api", "acme/devkit": "devkit"})

    def test_a_set_alias_is_kept_in_the_handoff_repository_and_listed(self):
        reg = self.mac()
        registry.set_alias("kit", "fireflysoftwaresolutions/devkit", reg["projects"], by="ancongui")
        saved = json.loads((self.handoff / "aliases.json").read_text())
        self.assertEqual(saved["aliases"]["kit"]["project"], "fireflysoftwaresolutions/devkit")
        self.assertEqual(registry.alias_map(reg["projects"])["fireflysoftwaresolutions/devkit"], "kit")
        rc, out, _ = run_cli(cli.cmd_alias, argparse.Namespace(alias=None, project=None, move=False), reg)
        self.assertEqual(rc, 0)
        self.assertIn("kit", out)
        self.assertIn("ancongui", out)
        self.assertIn("repository name", out)  # kit-consulting keeps its default alias

    def test_an_alias_in_use_is_refused_with_the_command_that_moves_it(self):
        reg = self.mac()
        registry.set_alias("kit", "fireflysoftwaresolutions/kit-consulting", reg["projects"])
        with self.assertRaises(registry.AliasError) as e:
            registry.set_alias("kit", "fireflysoftwaresolutions/devkit", reg["projects"])
        self.assertEqual(str(e.exception), "“kit” is already the alias of kit-consulting. Pick another, or move it: "
                                           "dev alias kit devkit --move.")

    def test_move_takes_the_alias_and_rm_gives_the_repository_name_back(self):
        reg = self.mac()
        registry.set_alias("kit", "fireflysoftwaresolutions/kit-consulting", reg["projects"])
        registry.set_alias("kit", "fireflysoftwaresolutions/devkit", reg["projects"], move=True)
        amap = registry.alias_map(reg["projects"])
        self.assertEqual(amap["fireflysoftwaresolutions/devkit"], "kit")
        self.assertEqual(amap["fireflysoftwaresolutions/kit-consulting"], "kit-consulting")
        self.assertEqual(registry.remove_alias("kit"), "fireflysoftwaresolutions/devkit")
        self.assertEqual(registry.alias_map(reg["projects"])["fireflysoftwaresolutions/devkit"], "devkit")

    def test_invalid_aliases_are_refused(self):
        reg = self.mac()
        for bad in ("", "Kit/x", "-kit", "k it", "a" * 41, "kit-consulting"):
            with self.subTest(alias=bad), self.assertRaises(registry.AliasError):
                registry.set_alias(bad, "fireflysoftwaresolutions/devkit", reg["projects"])

    def test_kit_finds_the_name_that_starts_with_it(self):
        m = registry.match(self.mac(), "kit")
        self.assertEqual([p["name"] for p in m["projects"]], ["kit-consulting"])
        self.assertEqual(m["level"], 3)
        self.assertFalse(m["exact"])

    def test_an_exact_alias_wins_over_names(self):
        reg = self.mac()
        registry.set_alias("kit", "fireflysoftwaresolutions/devkit", reg["projects"])
        m = registry.match(reg, "KIT")
        self.assertEqual([p["name"] for p in m["projects"]], ["devkit"])
        self.assertTrue(m["exact"])

    def test_within_a_level_the_project_on_this_mac_wins(self):
        reg = self.reg(self.project("acme/api", self.devkit_dir), self.project("other/api"))
        self.assertEqual([p["id"] for p in registry.find(reg, "api")], ["acme/api"])

    def test_a_tie_lists_every_match_with_its_command_and_exits_2(self):
        reg = self.mac()
        with mock.patch.object(cli, "_ARGV", ["go", "i"]):
            rc, out, err = run_cli(cli.cmd_go, argparse.Namespace(query="i"), reg)
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("“i” matches 2 projects", err)
        self.assertIn("dev go devkit", err)
        self.assertIn("dev go kit-consulting", err)
        self.assertIn("fireflysoftwaresolutions/devkit · on this Mac", err)

    def test_go_names_the_project_it_picked_when_the_match_is_not_exact(self):
        rc, out, err = run_cli(cli.cmd_go, argparse.Namespace(query="kit"), self.mac())
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), str(self.kit_dir))
        self.assertIn("kit → kit-consulting", err)

    def test_go_to_a_project_not_on_this_mac_gives_the_clone_command(self):
        rc, out, err = run_cli(cli.cmd_go, argparse.Namespace(query="flyquery"), self.mac())
        self.assertEqual(rc, 2)
        self.assertIn("dev clone fireflyframework/flyquery", err)

    # `dev __complete aliases` (what older shell files ask for) gives alias:project lines; the zsh and bash scripts
    # themselves are tested in tests/test_cli_completion.py
    def test_completion_offers_aliases(self):
        (self.handoff / "registry.json").write_text(json.dumps(self.mac()))
        rc, out, _ = run_cli(cli.cmd_complete, argparse.Namespace(kind="aliases"))
        self.assertIn("devkit:fireflysoftwaresolutions/devkit", out.splitlines())


# --------------------------------------------------------------------------- R3: dev status

def saved(line, ts=None, branch="release/26.11.3", path=None):
    return {"ts": ts or int(time.time()) - 60, "agent": "claude", "line": line, "branch": branch, "path": path}


LONG = ("devkit · release/26.11.3 ❯ Where we stand and what's left ❯ confirmed the 6-member limit is active, accepted "
        "the Keymaker's guard rules and the Architect's compaction design, and filled every slot. ❯ ✓ devkit now "
        "allows 6 · ✓ guard rules: 982 tests pass on both Pythons ❯ ▸ 42 of 70 steps ❯ crew: Backend engineer (Tank) "
        "▸ F4 ❯ you → approve the push of the release branch. ❯ next → F5 after Tank's F4; Kid builds the terminal "
        "work after Sati's design.")


def live_run(project, now, title=True):
    plan = [{"name": f"s{i}", "state": "done", "est": 10} for i in range(3)]
    plan += [{"name": "F4 caller identity", "title": "knowing who ran each command" if title else None,
              "state": "running", "owner": "lane1", "label": "Tank", "est": 30},
             {"name": "F5 evidence and C13 hooks", "state": "running", "owner": "lane2", "label": "Neo", "est": 30}]
    return {"v": 2, "project": project, "goal": "Release 26.11.3", "state": "running", "started": now - 3600,
            "updates": [{"ts": now - 120, "kind": "step", "text": "x"}], "intervals": [], "plan": plan,
            "agents": {"lane1": {"label": "Tank: F4 caller identity", "persona": "Tank", "state": "running",
                                 "started": now - 1800, "updates": []},
                       "lane2": {"label": "Neo: F5 evidence", "persona": "Neo", "state": "running",
                                 "started": now - 1800, "updates": []}},
            "decisions": [], "messages": [], "steps": 5, "step": 3}


class StatusTests(Sandbox):
    def view(self, reg=None, line=LONG, runs=(), ci=None, saved_rec=None, **kw):
        reg = reg or self.mac()
        p = reg["projects"][0]
        p["status_line"] = saved_rec or (saved(line) if line else None)
        return cli.status_view(reg, p, None, runs=list(runs), ci=ci or {"runs": [], "provider": "github"},
                               roster=ROSTER, **kw)

    def test_needs_you_comes_first_then_right_now_the_last_update_and_details(self):
        lines = cli.render_status(self.view(), cols=80)
        heads = [ln.strip().split(" ·")[0] for ln in lines if ln.strip() in ("Needs you", "Right now", "Details")
                 or ln.strip().startswith(("Needs you", "Last update"))]
        self.assertEqual([h.split(" from")[0] for h in heads], ["Needs you", "Right now", "Last update", "Details"])
        self.assertIn("More: dev status --all (tasks and plans), dev status --json (for tools)", lines[-1])

    def test_the_you_bullet_moves_up_and_live_crew_replaces_the_saved_crew(self):
        text = "\n".join(cli.render_status(self.view(), cols=80))
        needs = text.split("Right now")[0]
        self.assertIn("approve the push of the release branch.", needs)
        last = " ".join(text.split("Last update")[1].split("Details")[0].split())
        self.assertNotIn("Backend engineer (Tank)", last)
        self.assertNotIn("42 of 70", last)
        self.assertIn("Next", last)

    def test_nothing_needs_you_is_said_in_words(self):
        line = LONG.replace(" ❯ you → approve the push of the release branch.", "")
        self.assertIn("✓  Nothing needs you right now.", "\n".join(cli.render_status(self.view(line=line), cols=80)))

    def test_crew_rows_show_plain_step_titles_never_codes(self):
        now = time.time()
        text = "\n".join(cli.render_status(self.view(runs=[live_run("fireflysoftwaresolutions/devkit", now)]),
                                           cols=120))
        self.assertIn("Backend engineer (Tank) on knowing who ran each command", text)
        self.assertIn("(Neo) on evidence and hooks", text)
        crew_part = text.split("Right now")[1].split("Last update")[0]
        for code in ("F4", "F5", "C13"):
            self.assertNotIn(code, crew_part)
        self.assertIn("3 of 5 steps done", crew_part)

    def test_nobody_working_is_said_when_no_run_reported_in_30_minutes(self):
        now = time.time()
        run = live_run("fireflysoftwaresolutions/devkit", now - 3 * 3600)
        self.assertFalse(registry.run_working(run, now))
        self.assertTrue(registry.run_working(live_run("x", now), now))
        text = "\n".join(cli.render_status(self.view(runs=[run]), cols=80))
        self.assertIn("Crew      – nobody is working now", text)

    def test_status_fits_80_columns_and_wraps_under_the_value_column(self):
        lines = cli.render_status(self.view(), cols=80)
        self.assertLessEqual(max(ui.visible_len(ln) for ln in lines), 80)
        done = next(i for i, ln in enumerate(lines) if ln.startswith("  Done "))
        self.assertTrue(lines[done + 1].startswith(" " * 12) and lines[done + 1].strip())
        self.assertIn("Kid builds the terminal work after Sati's design.", " ".join(ln.strip() for ln in lines))

    def test_the_update_names_the_copy_it_came_from(self):
        reg = self.mac()
        kit = reg["projects"][1]
        copy = {"path": str(Path(self.tmp.name) / "kit-consulting-ci"), "branch": "fix/chat-subagent-progress",
                "location": "stray"}
        kit["worktrees"] = [copy]
        kit["status_line"] = saved("kit-consulting · fix/chat-subagent-progress ❯ Initiatives ❯ registered it ❯ ✓ ok "
                                   "❯ next → release", branch="main")
        v = cli.status_view(reg, kit, None, runs=[], ci={"runs": []}, roster=ROSTER)
        text = " ".join("\n".join(cli.render_status(v, 80)).split())
        self.assertIn("Last update from Claude, 1 min ago, in the kit-consulting-ci copy (fix/chat-subagent-progress)",
                      text)

    def test_a_layout_problem_needs_you_with_the_adopt_command(self):
        reg = self.mac()
        kit = reg["projects"][1]
        kit["canonical_ok"] = False
        v = cli.status_view(reg, kit, None, runs=[], ci={"runs": []}, roster=ROSTER)
        text = "\n".join(cli.render_status(v, 80))
        self.assertIn("Needs you · 1", text)
        self.assertIn("outside devkit's folder layout", text)
        self.assertIn(f"dev adopt {core.tilde(self.kit_dir)}", text)

    def test_a_crew_decision_needs_you_with_what_to_type(self):
        now = time.time()
        run = live_run("fireflysoftwaresolutions/devkit", now)
        run["decisions"] = [{"id": "D1", "state": "pending", "from_name": "Tank", "recommend": "yes",
                             "question": "keep the old /v1 endpoint for one more release?", "ts": now - 60}]
        text = " ".join("\n".join(cli.render_status(self.view(runs=[run], line=None), 80)).split())
        self.assertIn("Needs you · 1 ! The backend engineer (Tank) asks: keep the old /v1 endpoint for one more "
                      "release? Tank recommends yes. Answer in the chat with the session: approve D1: yes", text)

    def test_a_project_not_on_this_mac_gives_the_clone_command(self):
        reg = self.mac()
        v = cli.status_view(reg, reg["projects"][2], None, runs=[], ci={"runs": []}, roster=ROSTER)
        text = " ".join("\n".join(cli.render_status(v, 80)).split())
        self.assertIn("Checks – not on this Mac", text)
        self.assertIn("Folder not on this Mac. Get it: dev clone fireflyframework/flyquery", text)
        self.assertNotIn("Branch", text)

    def test_checks_that_cannot_be_read_say_so(self):
        v = self.view(ci={"runs": [], "error": "HTTP 401: bad credentials", "provider": "github"})
        self.assertIn("Checks    – could not read the checks: HTTP 401: bad credentials",
                      "\n".join(cli.render_status(v, 80)))

    def test_json_returns_the_same_data(self):
        reg = self.mac()
        reg["projects"][0]["status_line"] = saved(LONG)
        with mock.patch.object(progress, "active_runs", lambda *a, **k: []), \
                mock.patch.object(cli, "_ci_for", lambda *a, **k: {"runs": []}):
            rc, out, _ = run_cli(cli.cmd_status, argparse.Namespace(query="devkit", json=True, all=False), reg)
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["alias"], "devkit")
        self.assertEqual(data["needs_you"][0]["text"], "approve the push of the release branch.")
        self.assertIn("Kid builds the terminal work", data["last_update"]["next"])


class SavedStatusTests(Sandbox):
    def test_the_whole_status_block_is_saved(self):
        line = LONG + " ❯ next → " + "the red-team review, then the release " * 30
        canary.save_status("acme/api", "claude", line, "main", path="/tmp/x")
        rec = json.loads((self.state / "status" / "acme__api.json").read_text())
        self.assertEqual(rec["line"], line)
        self.assertEqual(rec["path"], "/tmp/x")

    def test_a_huge_block_is_bounded_at_a_word(self):
        canary.save_status("acme/api", "claude", "word " * 5000, "main")
        rec = json.loads((self.state / "status" / "acme__api.json").read_text())
        self.assertLessEqual(len(rec["line"]), canary.STATUS_MAX)
        self.assertTrue(rec["line"].endswith("word …"))


class LiveSessionTests(Sandbox):
    def test_a_session_is_live_while_its_recorded_process_runs(self):
        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        d = self.state / "sessions"
        d.mkdir()
        (d / "claude-s1.json").write_text(json.dumps({"agent": "claude", "session": "s1", "top": str(repo),
                                                      "pid": os.getpid()}))
        (d / "claude-s2.json").write_text(json.dumps({"agent": "claude", "session": "s2", "top": str(repo)}))
        with mock.patch.object(core, "_CWDS", []), mock.patch.object(progress, "RUNS", self.state / "runs"), \
                mock.patch.object(core, "is_temp", lambda p: False):  # the repo sits in a temp folder
            live = {s["session"]: s["active"] for s in registry.live_sessions()}
        self.assertEqual(live, {"s1": True, "s2": False})


# --------------------------------------------------------------------------- R11: dev config as a report

HOOK = "'/Users/me/.local/share/devkit/pins/26.11.3/bin/dev' hook post-tool-failure --agent claude"
HOOKS_CONF = {"PostToolUseFailure": [{"matcher": "Agent|Task|Bash", "hooks": [{"type": "command", "command": HOOK,
                                                                               "timeout": 10}]}]}


class ConfigTests(Sandbox):
    def report(self, drift, record=None, check=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.config_report(drift, record or {}, HOOKS_CONF, check=check, cols=80)
        return rc, out.getvalue()

    def test_a_new_hook_reads_as_not_installed_yet_with_its_fields_and_exits_0(self):
        rc, out = self.report([policy.Drift("claude", "hooks.PostToolUseFailure", policy.MISSING, HOOK)])
        self.assertEqual(rc, 0)
        self.assertIn("devkit settings: 1 to install", out)
        self.assertIn("+  New in this devkit version, not installed yet", out)
        self.assertIn("Event     PostToolUseFailure", out)
        self.assertIn("Matches   Agent, Task, Bash", out)
        self.assertIn("Runs      dev hook post-tool-failure --agent claude (10 s limit)", out)
        self.assertIn("frees its crew slot", out)
        self.assertIn("dev install", out)
        self.assertLessEqual(max(ui.visible_len(ln) for ln in out.splitlines()), 80)

    def test_check_prints_one_line_and_exits_1(self):
        rc, out = self.report([policy.Drift("claude", "hooks.PostToolUseFailure", policy.MISSING, HOOK)], check=True)
        self.assertEqual(rc, 1)
        self.assertEqual(out.strip(), "1 setting differs from what devkit installs: run dev config for details.")
        self.assertEqual(self.report([], check=True), (0, ""))

    def test_the_install_record_tells_removed_from_new(self):
        rec = {"claude": {"hooks": {"PostToolUseFailure": [cli._norm_cmd(HOOK)]}}}
        rc, out = self.report([policy.Drift("claude", "hooks.PostToolUseFailure", policy.MISSING, HOOK)], rec)
        self.assertIn("Removed outside devkit", out)
        self.assertIn("dev config repair", out)
        self.assertNotIn("New in this devkit version", out)

    def test_a_value_changed_by_hand_shows_both_values(self):
        rec = {"claude": {"keys": {"permissions.defaultMode": "acceptEdits"}}}
        rc, out = self.report([policy.Drift("claude", "permissions.defaultMode", "bypassPermissions", "acceptEdits")],
                              rec)
        self.assertEqual(rc, 0)
        self.assertIn("~  Changed outside devkit", out)
        self.assertIn("devkit's value   acceptEdits", out)
        self.assertIn("installed now    bypassPermissions", out)

    def test_instructions_from_an_older_devkit_read_as_new_and_hand_edits_as_changed(self):
        from devkit import setup
        older = Path(self.tmp.name) / "CLAUDE.md"
        older.write_text(f"{setup.GENERATED_MARK} 26.11.2 from ~/x/agents/AGENTS.md -->\n# rules\n")
        mine = Path(self.tmp.name) / "AGENTS.md"
        mine.write_text("# my own rules\n")
        drift = [policy.Drift("instructions", str(older), "edited", "devkit-generated"),
                 policy.Drift("instructions", str(mine), "edited", "devkit-generated")]
        with mock.patch.object(setup, "render_instructions", lambda: "# what this version writes\n"):
            rc, out = self.report(drift)
        text = " ".join(out.split())
        self.assertIn("1 to install, 1 changed outside devkit", text)
        self.assertIn("come from devkit 26.11.2; this version writes newer ones.", text)
        self.assertIn("were edited by hand", text)
        self.assertNotIn("devkit-generated", text)

    def test_no_difference_says_everything_matches(self):
        rc, out = self.report([])
        self.assertEqual(rc, 0)
        self.assertIn("All devkit settings are installed and match.", out)

    def test_install_records_what_it_installed(self):
        from devkit import setup
        with mock.patch.object(setup, "run_install", lambda **kw: 0), \
                mock.patch.object(policy, "claude_desired", lambda: ({("cleanupPeriodDays",): 30},
                                                                    {"Stop": [HOOK.replace("post-tool-failure", "stop")]})):
            rc, _, _ = run_cli(cli.cmd_install, argparse.Namespace(why="", unverified=False, only=None, version=None,
                                                                   as_pinned=True))
        self.assertEqual(rc, 0)
        rec = json.loads((self.state / "config-installed.json").read_text())
        self.assertEqual(rec["claude"]["hooks"]["Stop"], ["dev hook stop --agent claude"])
        self.assertEqual(rec["claude"]["keys"]["cleanupPeriodDays"], 30)


if __name__ == "__main__":
    unittest.main()
