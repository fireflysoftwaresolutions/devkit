# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The branch map (spec P-fy3bwa0pshk, R7, R8 and R11): one row per local branch, and the views that show it (the
# handoff note's generated block, `dev status`, `dev overview`, `dev branch ls` and the session-start context line).
# Branch backups (R1 to R4): `dev backup`, `dev branch embargo` and the automatic backup of `dev sync`. A branch
# reaches the remote only when the push starts no CI run, fast-forward, off the default branch, without an embargo
# and after the secret scan; anything else leaves it here with the reason and the fix. (The backup tests live here:
# the guard reads a file named test_backup.py as a backup copy.)
#
# Offline: temporary repositories with a local bare "origin" (the GitHub URL is rewritten to it with insteadOf), and
# for the backups gh replaced by a runner that answers "no pull request". devkit's state, handoff repository, audit
# trail and work items live in a temp folder; the audit is the real audit.record. Nothing reaches a real remote,
# GitHub or devkit's real state. Run: python3 -m unittest tests.test_branches -v
import argparse
import contextlib
import io
import json
import os
import secrets
import string
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import audit, backup, branches, canary, cli, core, crew, gitx, notes, pipelines, progress, registry, work  # noqa: E402,E501

GITHUB_URL = "https://github.com/acme/widget.git"
PID = "acme/widget"
HOUR = 3600
ISOLATE = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Ana Dev",
           "GIT_AUTHOR_EMAIL": "ana@example.com", "GIT_COMMITTER_NAME": "Ana Dev",
           "GIT_COMMITTER_EMAIL": "ana@example.com"}
ROSTER = crew.load(team=False)  # devkit's own roster, from the checkout

EMBARGO_NOTE = """---
status: active
priority: normal
embargo:
  - branch: release/x
    why: "keeps the guard fix off the public remote: it discloses an open hole"
    until: guard refusal rules
    who: keymaker
    when: 2026-11-06T10:00:00Z
---
# acme/widget
"""


def git(cwd, *args, env=None):
    rc, out, err = core.sh(["git", "-C", str(cwd), *args], env=env, timeout=60)
    if rc != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {err or out}")
    return out


class Sandbox(unittest.TestCase):
    # A repository `widget` whose origin is a bare repository, with devkit's state in a temp folder.
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        state, handoff = self.root / "state", self.root / "handoff"
        self.saved = (core.STATE, core.HANDOFF, core.NOTES, registry.STATE, registry.HANDOFF, registry.NOTES,
                      work.HANDOFF, work.ITEMS, audit.AUDIT, progress.RUNS, progress.SESSIONS, canary.STATE,
                      canary.LOG, core.CFG.get("audit"), core.CFG.get("backup"),
                      {k: core.GIT_ENV.get(k) for k in ISOLATE}, os.getcwd())
        core.STATE = registry.STATE = canary.STATE = state
        core.HANDOFF = registry.HANDOFF = work.HANDOFF = handoff
        core.NOTES = registry.NOTES = handoff / "projects"
        work.ITEMS = handoff / "items"
        audit.AUDIT = state / "audit"
        progress.RUNS, progress.SESSIONS = state / "runs", state / "sessions"
        canary.LOG = state / "canary.jsonl"
        core.CFG["audit"] = dict(json.loads(json.dumps(core.DEFAULTS["audit"])), level="all")
        core.CFG.pop("backup", None)
        core.GIT_ENV.update(ISOLATE)
        self.now = time.time()
        self.n = 0
        self.origin = self.root / "origin.git"
        git(self.root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.repo = self.root / "widget"
        git(self.root, "init", "-q", "-b", "main", str(self.repo))
        git(self.repo, "remote", "add", "origin", GITHUB_URL)
        git(self.repo, "config", f"url.{self.origin}.insteadOf", GITHUB_URL)
        self.commit("chore: start the widget", age=48 * HOUR)
        git(self.repo, "push", "-q", "-u", "origin", "main")
        git(self.repo, "remote", "set-head", "origin", "main")

    def tearDown(self):
        (core.STATE, core.HANDOFF, core.NOTES, registry.STATE, registry.HANDOFF, registry.NOTES, work.HANDOFF,
         work.ITEMS, audit.AUDIT, progress.RUNS, progress.SESSIONS, canary.STATE, canary.LOG, cfg_audit, cfg_backup,
         git_env, cwd) = self.saved
        for key, value in (("audit", cfg_audit), ("backup", cfg_backup)):
            if value is None:
                core.CFG.pop(key, None)
            else:
                core.CFG[key] = value
        for k, v in git_env.items():
            if v is None:
                core.GIT_ENV.pop(k, None)
            else:
                core.GIT_ENV[k] = v
        os.chdir(cwd)
        self.tmp.cleanup()

    def commit(self, msg, age=2 * HOUR, repo=None):
        repo = repo or self.repo
        self.n += 1
        (repo / f"f{self.n}.txt").write_text(msg + "\n")
        git(repo, "add", "-A")
        when = f"@{int(self.now - age)} +0000"
        git(repo, "commit", "-q", "-m", msg, env=dict(core.GIT_ENV, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when))
        return git(repo, "rev-parse", "HEAD")

    def scene(self):
        # feat/pushed: on origin with its upstream. release/x: two commits only here, 3 h and 2 h old.
        # feat/inside: its commit is inside release/x. HEAD ends on release/x.
        git(self.repo, "switch", "-q", "-c", "feat/pushed", "main")
        self.commit("feat: add the pushed thing", age=4 * HOUR)
        git(self.repo, "push", "-q", "-u", "origin", "feat/pushed")
        git(self.repo, "switch", "-q", "-c", "release/x", "main")
        self.first = self.commit("feat: add the first release thing", age=3 * HOUR)
        self.commit("feat: add the second release thing", age=2 * HOUR)
        git(self.repo, "branch", "feat/inside", self.first)

    def rows(self, **kw):
        kw.setdefault("prs", [])
        return {r["name"]: r for r in branches.map(self.repo, now=self.now, **kw)}

    def write_note(self, text):
        f = core.note_path(PID)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)


class MapTests(Sandbox):
    def test_rows_say_where_each_branch_lives(self):
        self.scene()
        wt = self.root / "wt-inside"
        git(self.repo, "worktree", "add", "-q", str(wt), "feat/inside")
        rows = self.rows()
        pushed, rel, inside, main = rows["feat/pushed"], rows["release/x"], rows["feat/inside"], rows["main"]
        self.assertEqual((pushed["where"], pushed["upstream"], pushed["unpushed"]),
                         ("local + origin", "origin/feat/pushed", 0))
        self.assertEqual(pushed["backup"]["state"], "on_remote")
        self.assertEqual((rel["where"], rel["upstream"], rel["unpushed"], rel["base_ahead"]), ("local only", None, 2, 2))
        self.assertTrue(rel["current"])
        self.assertEqual(rel["backup"]["state"], "not_backed_up")
        self.assertIn("2 commits only on this Mac", rel["backup"]["reason"])
        self.assertEqual(rel["oldest_unpushed"], int(self.now - 3 * HOUR))
        self.assertEqual(rel["last_commit"], int(self.now - 2 * HOUR))
        self.assertEqual((inside["merged_into"], inside["backup"]["state"]), ("release/x", "merged"))
        self.assertEqual(core.real(inside["worktree"]), core.real(wt))
        self.assertIsNone(rel["worktree"])
        self.assertTrue(main["default"])
        self.assertEqual(main["backup"]["state"], "on_remote")

    def test_many_branches_cost_no_more_git_commands_than_a_few(self):
        # the session start has 30 s: the map runs a few git commands for the repository, not three per branch
        self.scene()
        calls = []
        real = core.sh

        def counted(args, *a, **kw):
            calls.append(args)
            return real(args, *a, **kw)

        with mock.patch.object(core, "sh", side_effect=counted):
            self.rows()
        few = len(calls)
        for k in range(12):
            git(self.repo, "switch", "-q", "-c", f"feat/more-{k}", "main")
            self.commit(f"feat: add more part {k}")
        calls.clear()
        with mock.patch.object(core, "sh", side_effect=counted):
            rows = self.rows()
        self.assertEqual(len(calls), few)
        self.assertEqual((rows["feat/more-7"]["unpushed"], rows["feat/more-7"]["base_ahead"]), (1, 1))
        self.assertEqual(rows["feat/more-7"]["oldest_unpushed"], int(self.now - 2 * HOUR))
        self.assertEqual(rows["feat/inside"]["merged_into"], "release/x")
        self.assertEqual((rows["release/x"]["unpushed"], rows["release/x"]["merged_into"]), (2, None))

    def test_a_branch_at_the_same_commit_leaves_the_backup_to_the_one_with_the_work(self):
        self.scene()
        git(self.repo, "branch", "feat/copy", "release/x")
        rows = self.rows()
        self.assertEqual((rows["feat/copy"]["merged_into"], rows["feat/copy"]["backup"]["state"]), ("release/x", "merged"))
        self.assertIsNone(rows["release/x"]["merged_into"])
        self.assertEqual(rows["release/x"]["backup"]["state"], "not_backed_up")

    def test_commits_younger_than_after_minutes_wait(self):
        git(self.repo, "switch", "-q", "-c", "feat/fresh", "main")
        self.commit("feat: add a fresh thing", age=5 * 60)
        self.assertEqual(self.rows()["feat/fresh"]["backup"]["state"], "waiting")
        core.CFG["backup"] = {"after_minutes": 1}
        self.assertEqual(self.rows()["feat/fresh"]["backup"]["state"], "not_backed_up")

    def test_the_default_branch_is_never_backed_up(self):
        self.commit("fix: commit straight on main", age=2 * HOUR)
        main = self.rows()["main"]
        self.assertEqual((main["unpushed"], main["backup"]["state"]), (1, "not_backed_up"))
        self.assertIn("default branch", main["backup"]["reason"])

    def test_a_repository_without_a_remote(self):
        solo = self.root / "solo"
        git(self.root, "init", "-q", "-b", "main", str(solo))
        self.commit("chore: start solo", repo=solo)
        row = branches.map(solo, now=self.now, prs=[])[0]
        self.assertEqual((row["where"], row["backup"]["state"]), ("local only", "no_remote"))

    def test_last_backup_and_skipped_reason_come_from_the_audit_trail(self):
        self.scene()
        rec = audit.record("git.backup", "feat/pushed", "origin 1111111..2222222", project=PID, branch="feat/pushed")
        self.assertIsNotNone(rec)
        audit.record("git.backup", "release/x", "origin 1111111..3333333", project="acme/other", branch="release/x")
        audit.record("git.backup-skipped", "release/x", "a push starts CI (ci.yml runs on every push)",
                     project=PID, branch="release/x")
        rows = self.rows()
        self.assertEqual(rows["feat/pushed"]["last_backup"], rec["ts"])
        self.assertEqual(rows["feat/pushed"]["backup"]["state"], "backed_up")
        self.assertIsNone(rows["release/x"]["last_backup"])
        self.assertEqual(rows["release/x"]["backup"]["reason"], "a push starts CI (ci.yml runs on every push)")

    def test_embargo_comes_from_the_note_metadata_and_names_the_member_by_role(self):
        self.scene()
        self.write_note(EMBARGO_NOTE)
        rel = self.rows()["release/x"]
        self.assertEqual(rel["embargo"], {"why": "keeps the guard fix off the public remote: it discloses an open hole",
                                          "until": "guard refusal rules", "who": "keymaker",
                                          "when": "2026-11-06T10:00:00Z"})
        self.assertEqual(rel["backup"]["state"], "embargoed")
        self.assertIn('not backed up: embargoed until "guard refusal rules"', branches.describe(rel, now=self.now))
        detail = "\n".join(branches.ls_lines(list(self.rows().values()), now=self.now, cols=120))
        self.assertIn("Security engineer (The Keymaker)", " ".join(detail.split()))

    def test_pull_request_and_linked_task(self):
        self.scene()
        git(self.repo, "branch", "fix/t-fy0000test1-login", "main")
        prs = [{"number": 41, "state": "OPEN", "headRefName": "feat/pushed", "isDraft": True, "checks": "failing",
                "url": "https://github.com/acme/widget/pull/41", "title": "feat: add the pushed thing",
                "updatedAt": "2026-11-06T10:00:00Z"}]
        for iid, title, links in (("T-fy0000link1", "Ship the release", ["branch:release/x"]),
                                  ("T-fy0000test1", "Fix the login timeout", [])):
            it = {"meta": {"id": iid, "type": "task", "title": title, "scope": PID, "status": "doing",
                           "bucket": "now", "links": links}, "body": "", "changelog": []}
            f = work.item_path(PID, iid)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(work.render(it))
        rows = self.rows(prs=prs)
        self.assertEqual(rows["feat/pushed"]["pr"], {"number": 41, "state": "open", "draft": True, "checks": "failing",
                                                     "url": "https://github.com/acme/widget/pull/41",
                                                     "title": "feat: add the pushed thing"})
        self.assertIn("PR #41 draft · checks failing", branches.describe(rows["feat/pushed"], now=self.now))
        self.assertEqual(rows["release/x"]["task"], {"id": "T-fy0000link1", "title": "Ship the release",
                                                     "status": "doing"})
        self.assertEqual(rows["fix/t-fy0000test1-login"]["task"]["id"], "T-fy0000test1")
        self.assertIsNone(rows["feat/inside"]["task"])


class NoteTests(unittest.TestCase):
    def test_note_set_keeps_the_embargo_list(self):
        out = notes.set_meta(EMBARGO_NOTE, priority="high")
        meta, _ = notes.split_frontmatter(out)
        self.assertEqual(meta["priority"], "high")
        self.assertNotIn("- branch", meta)
        self.assertEqual(notes.embargoes(out), notes.embargoes(EMBARGO_NOTE))
        self.assertEqual(notes.embargoes(out)[0]["why"],
                         "keeps the guard fix off the public remote: it discloses an open hole")


class ViewTests(Sandbox):
    def project(self):
        self.scene()
        git(self.repo, "worktree", "add", "-q", str(self.root / "wt-pushed"), "feat/pushed")
        p = registry.inspect_repo(self.repo, with_prs=False)
        self.assertEqual(p["id"], PID)
        return p

    def test_note_block_has_one_row_per_branch_instead_of_the_worktree_lines(self):
        block = registry.render_auto(self.project())
        self.assertIn("- **Branches:**", block)
        self.assertNotIn("**Worktree**", block)
        rel = next(ln for ln in block.splitlines() if ln.startswith("  - `release/x`"))
        self.assertIn("`release/x` · local only · ↑2 · 2 ahead of main · not backed up: 2 commits only on this Mac", rel)
        self.assertIn(core.fmt_ts(int(self.now - 3 * HOUR)), rel)
        self.assertNotIn("ago", rel)  # absolute times: the note changes only when a branch does
        pushed = next(ln for ln in block.splitlines() if ln.startswith("  - `feat/pushed`"))
        self.assertIn("worktree `", pushed)

    def test_status_has_a_branches_section(self):
        p = self.project()
        reg = {"projects": [p], "issues": [], "sessions": []}
        v = cli.status_view(reg, p, None, runs=[], ci={"runs": [], "provider": "github"}, roster=ROSTER)
        self.assertEqual([r["name"] for r in v["branches"]][0], "release/x")
        text = "\n".join(cli.render_status(v, 120))
        self.assertIn("Branches", text)
        row = next(ln for ln in text.splitlines() if "release/x" in ln and "local only" in ln)
        self.assertIn("↑2 · 2 ahead of main · not backed up: 2 commits only on this Mac", row)

    def test_overview_counts_branches_not_backed_up_and_embargoed(self):
        self.write_note(EMBARGO_NOTE.replace("release/x", "feat/pushed"))  # shares no commit with release/x
        p = self.project()
        p["branches"] = branches.map(self.repo, now=self.now, prs=[])
        reg = {"projects": [p], "issues": [], "initiatives": [], "generated": int(self.now)}
        with mock.patch.object(registry, "build", return_value=reg):
            data = cli.overview_data(7)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.cmd_overview(argparse.Namespace(days=7, json=False, all=True))
        self.assertEqual(data["projects"][0]["branches"],
                         {"total": 4, "not_backed_up": 1, "embargoed": 1, "waiting": 0})
        self.assertIn("4 branches · 1 not backed up · 1 embargoed", out.getvalue())

    def test_branch_ls_prints_the_full_map(self):
        self.scene()
        os.chdir(self.repo)
        parser = cli.build_parser()
        for argv in (["branch", "ls", "--offline"], ["branch", "ls", "--offline", "--json"]):
            a = parser.parse_args(argv)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(a.fn(a), 0)
            if "--json" in argv:
                rows = {r["name"]: r for r in json.loads(out.getvalue())}
                self.assertEqual(rows["release/x"]["backup"]["state"], "not_backed_up")
            else:
                line = next(ln for ln in out.getvalue().splitlines() if "release/x" in ln)
                self.assertIn("local only", line)
                self.assertIn("not backed up", " ".join(out.getvalue().split()))

    def test_context_line_names_what_is_at_risk(self):
        self.scene()
        self.write_note(EMBARGO_NOTE.replace("release/x", "feat/pushed"))  # shares no commit with release/x
        line = branches.context_line(branches.map(self.repo, now=self.now, prs=[]))
        self.assertIn("not backed up: `release/x` (2 commits only on this Mac)", line)
        self.assertIn("embargoed: `feat/pushed`", line)
        git(self.repo, "push", "-q", "-u", "origin", "release/x")
        self.write_note("# acme/widget\n")
        self.assertIn("nothing is only on this Mac", branches.context_line(branches.map(self.repo, prs=[])))


# --------------------------------------------------------------------------- branch backups (R1 to R4)

CI_PR_AND_MAIN = """name: ci
on:
  pull_request:
  push:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-24.04
    steps:
      - run: echo test
"""
CI_EVERY_PUSH = CI_PR_AND_MAIN.replace("  pull_request:\n  push:\n    branches: [main]\n", "  push:\n")
CI_DRAFT_GATED = CI_PR_AND_MAIN.replace("    runs-on:", "    if: github.event.pull_request.draft == false\n    runs-on:")
HAS_GITLEAKS = core.have("gitleaks")
REAL_VISIBILITY = backup._visibility  # BackupCase replaces it: no test reads GitHub unless it says so


class BackupCase(Sandbox):
    def setUp(self):
        super().setUp()
        self.saved_gh = gitx.GH
        gitx.GH = lambda args, cwd=None, timeout=60: (0, "[]", "")  # GitHub has no pull request for any branch
        Path(core.HANDOFF).mkdir(parents=True, exist_ok=True)  # this Mac has its handoff repository
        # a private repository, and no developer at a terminal: no test asks a question or reads GitHub
        for patch in (mock.patch.dict(os.environ, {"DEVKIT_NO_AUTOSYNC": "1"}),
                      mock.patch.object(backup, "_visibility", return_value="private"),
                      mock.patch.object(core, "set_by_developer", return_value=False)):
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        gitx.GH = self.saved_gh
        super().tearDown()

    def ci(self, text):
        wf = self.repo / ".github" / "workflows"
        wf.mkdir(parents=True, exist_ok=True)
        (wf / "ci.yml").write_text(text)
        git(self.repo, "add", "-A")
        when = f"@{int(self.now - 47 * HOUR)} +0000"
        git(self.repo, "commit", "-q", "-m", "ci: add the workflow",
            env=dict(core.GIT_ENV, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when))
        git(self.repo, "push", "-q", "origin", "main")

    def branch(self, name, *ages):
        git(self.repo, "switch", "-q", "-c", name, "main")
        return [self.commit(f"feat: add part {k} of {name}", age=age) for k, age in enumerate(ages)]

    def on_origin(self, name):
        rc, out, _ = core.sh(["git", "-C", str(self.origin), "rev-parse", "--verify", "-q", f"refs/heads/{name}"])
        return out if rc == 0 else ""

    def dev(self, *argv):
        os.chdir(self.repo)
        a = cli.build_parser().parse_args(list(argv))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = a.fn(a)
        return rc, " ".join((out.getvalue() + err.getvalue()).split())

    def project(self):
        p = registry.inspect_repo(self.repo, with_prs=False)
        p.update(prs_known=True, prs_open=[])  # as the registry has them when gh answers
        return p

    def records(self, action):
        out = []
        for f in sorted(Path(audit.AUDIT).glob("*.jsonl")):
            out += [r for r in map(json.loads, f.read_text().splitlines()) if r.get("action") == action]
        return out


@unittest.skipUnless(HAS_GITLEAKS, "the secret scan needs gitleaks")
class SyncBackupTests(BackupCase):
    def test_sync_backs_up_a_branch_older_than_after_minutes_and_audits_it(self):
        self.ci(CI_PR_AND_MAIN)
        tip = self.branch("feat/report", 2 * HOUR, HOUR)[-1]
        p = self.project()
        self.assertEqual({r["name"]: r["backup"]["state"] for r in p["branches"]}["feat/report"], "not_backed_up")
        done = backup.sync({"projects": [p]})
        self.assertEqual([(r["branch"], r["state"]) for r in done], [("feat/report", "pushed")])
        self.assertEqual(self.on_origin("feat/report"), tip)
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "feat/report@{u}"), "origin/feat/report")
        rec = self.records("git.backup")[-1]
        self.assertEqual((rec["project"], rec["branch"], rec["target"], rec["outcome"]),
                         (PID, "feat/report", "origin/feat/report", "ok"))
        self.assertIn(f"..{tip[:12]} (2 commits)", rec["detail"])
        self.assertEqual({r["name"]: r["backup"]["state"] for r in p["branches"]}["feat/report"], "backed_up")

    def test_sync_pushes_only_the_commits_older_than_after_minutes(self):
        self.ci(CI_PR_AND_MAIN)
        old, _ = self.branch("feat/fresh", 2 * HOUR, 5 * 60)
        backup.sync({"projects": [self.project()]})
        self.assertEqual(self.on_origin("feat/fresh"), old)
        row = {r["name"]: r for r in branches.map(self.repo, prs=[])}["feat/fresh"]
        self.assertEqual((row["unpushed"], row["backup"]["state"]), (1, "waiting"))

    def test_backup_auto_false_turns_the_sync_backup_off(self):
        self.ci(CI_PR_AND_MAIN)
        self.branch("feat/report", 2 * HOUR)
        core.CFG["backup"] = {"auto": False}
        self.assertEqual(backup.sync({"projects": [self.project()]}), [])
        self.assertEqual(self.on_origin("feat/report"), "")

    def test_an_embargo_keeps_the_branch_and_the_branches_inside_it_off_the_remote(self):
        self.ci(CI_PR_AND_MAIN)
        first = self.branch("release/x", 3 * HOUR, 2 * HOUR)[0]
        git(self.repo, "branch", "feat/inside", first)
        rc, out = self.dev("branch", "embargo", "release/x", "--why", "keeps the guard fix off the public remote",
                           "--until", "guard refusal rules")
        self.assertEqual(rc, 0, out)
        rec = notes.embargoes(core.note_path(PID).read_text())[0]
        self.assertEqual((rec["branch"], rec["why"], rec["until"]),
                         ("release/x", "keeps the guard fix off the public remote", "guard refusal rules"))
        self.assertEqual(backup.sync({"projects": [self.project()]}), [])  # the map says why: no backup takes it
        self.assertEqual(self.on_origin("release/x"), "")
        rows = {r["name"]: r for r in branches.map(self.repo, prs=[])}
        self.assertIn('not backed up: embargoed until "guard refusal rules"', branches.describe(rows["release/x"]))
        self.assertIn("held by the embargo on release/x", branches.describe(rows["feat/inside"]))
        rc, out = self.dev("backup", "feat/inside")
        self.assertEqual(rc, 1)
        self.assertIn('its commits are in release/x, which is embargoed until "guard refusal rules"', out)
        self.assertEqual(self.on_origin("feat/inside"), "")
        rc, out = self.dev("branch", "embargo", "--lift", "release/x")
        self.assertEqual((rc, branches.embargoes(PID)), (0, {}))
        self.assertEqual([r["detail"][:4] for r in self.records("git.embargo")], ["set:", "lift"])
        self.assertEqual(self.dev("backup", "feat/inside")[0], 0)
        self.assertEqual(self.on_origin("feat/inside"), first)


@unittest.skipUnless(HAS_GITLEAKS, "the secret scan needs gitleaks")
class EmbargoHoldsTests(BackupCase):
    # B2 of the pipeline security review: the embargo failed open. It now holds the commits, not just the name: the
    # note records the embargoed branch's tips, so a branch it was merged into keeps them off the remote after the
    # embargoed branch is deleted. A handoff note devkit cannot read means no backup, never "no embargo". Lifting an
    # embargo is the developer's own decision.
    def embargo(self, name):
        rc, out = self.dev("branch", "embargo", name, "--why", "discloses an open hole", "--until", "the release")
        self.assertEqual(rc, 0, out)

    def test_the_embargo_follows_its_commits_after_a_merge_and_the_branch_is_deleted(self):
        self.ci(CI_PR_AND_MAIN)
        first = self.branch("fix/hole", 3 * HOUR)[0]
        self.embargo("fix/hole")
        later = self.commit("fix: close the second path", age=2 * HOUR)  # after the embargo was set
        self.branch("feat/other", 2 * HOUR)
        backup.run(self.repo, ["feat/other"])  # any backup run records the embargoed branch's new tip
        git(self.repo, "switch", "-q", "-c", "release/x", "main")
        self.commit("chore: start the release", age=2 * HOUR)
        git(self.repo, "merge", "-q", "--no-ff", "-m", "chore: integrate the fix", "fix/hole")
        git(self.repo, "branch", "-q", "-D", "fix/hole")
        self.assertEqual(self.sync(), [])  # the map holds it as embargoed: the sync does not even try
        self.assertEqual(self.on_origin("release/x"), "")
        self.assertEqual(set(branches.read_embargoes(PID)[0]["fix/hole"]["commits"]), {first, later})
        res = backup.run(self.repo, ["release/x"])[0]
        self.assertEqual(res["reason"], 'its commits are in fix/hole, which is embargoed until "the release"')
        row = {r["name"]: r for r in branches.map(self.repo, prs=[])}["release/x"]
        self.assertEqual((row["embargo_via"], row["backup"]["state"]), ("fix/hole", "embargoed"))
        self.assertIn("held by the embargo on fix/hole", branches.describe(row))
        self.assertEqual(self.dev("branch", "embargo", "--lift", "fix/hole")[0], 0)
        self.assertEqual(backup.run(self.repo, ["release/x"])[0]["state"], "pushed")

    def test_a_handoff_note_devkit_cannot_read_means_no_backup(self):
        self.ci(CI_PR_AND_MAIN)
        self.branch("feat/x", 2 * HOUR)
        broken = "---\nstatus: active\nembargo:\n  - branch: feat/x\n    why: an open hole\n"  # its block never ends
        self.write_note(broken)
        res = backup.run(self.repo, ["feat/x"])[0]
        self.assertEqual(res["state"], "skipped")
        self.assertIn("devkit cannot read the handoff note", res["reason"])
        self.assertIn("so it cannot tell which branches are embargoed", res["reason"])
        self.write_note(EMBARGO_NOTE)
        os.chmod(core.note_path(PID), 0)  # the temp folder's cleanup removes it at any mode
        self.assertIn("devkit cannot read the handoff note", backup.run(self.repo, ["feat/x"])[0]["reason"])
        os.chmod(core.note_path(PID), 0o600)
        p = self.project()
        core.HANDOFF = core.NOTES = self.root / "no-handoff"  # this Mac has no handoff repository
        self.assertEqual([(r["branch"], r["state"]) for r in backup.sync({"projects": [p]})], [("feat/x", "skipped")])
        self.assertEqual(self.on_origin("feat/x"), "")

    def test_only_the_developer_lifts_an_embargo(self):
        self.branch("fix/hole", HOUR)
        self.embargo("fix/hole")
        with mock.patch.object(backup, "crew_member", return_value="link"):
            rc, out = self.dev("branch", "embargo", "--lift", "fix/hole")
        self.assertEqual(rc, 2)
        self.assertIn("dev branch embargo --lift is the developer's own decision", out)
        self.assertEqual(list(branches.embargoes(PID)), ["fix/hole"])
        self.assertEqual([r["detail"][:4] for r in self.records("git.embargo")], ["set:"])

    def sync(self):
        return [(r["branch"], r["state"]) for r in backup.sync({"projects": [self.project()]})]


@unittest.skipUnless(HAS_GITLEAKS, "the secret scan needs gitleaks")
class BackupCommandTests(BackupCase):
    def test_a_push_that_starts_ci_is_not_made_and_the_message_says_both_ways_forward(self):
        self.ci(CI_EVERY_PUSH)
        tip = self.branch("feat/x", HOUR)[-1]
        rc, out = self.dev("backup")
        self.assertEqual(rc, 1)
        self.assertIn("feat/x not backed up: a push to this branch starts CI (ci.yml runs on every push)", out)
        self.assertIn("dev backup --accept-ci feat/x", out)
        self.assertIn("branches-ignore", out)
        self.assertEqual(self.on_origin("feat/x"), "")
        skipped = self.records("git.backup-skipped")[-1]
        self.assertEqual((skipped["branch"], skipped["outcome"]), ("feat/x", "recorded"))
        rc, out = self.dev("backup", "--accept-ci")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.on_origin("feat/x"), tip)
        budget = json.loads((core.STATE / "ci-pushes.json").read_text())
        self.assertEqual([len(v) for k, v in budget.items() if k.endswith("|feat/x")], [1])

    def test_a_diverged_remote_stops_the_backup_with_the_commands(self):
        self.ci(CI_PR_AND_MAIN)
        self.branch("feat/x", 2 * HOUR)
        git(self.repo, "push", "-q", "-u", "origin", "feat/x")
        other = self.root / "other"
        git(self.root, "clone", "-q", "-b", "feat/x", str(self.origin), str(other))
        theirs = self.commit("feat: add their part", repo=other)
        git(other, "push", "-q", "origin", "feat/x")
        self.commit("feat: add my part", age=HOUR)
        rc, out = self.dev("backup")
        self.assertEqual(rc, 1)
        self.assertIn("origin/feat/x has diverged from feat/x", out)
        self.assertIn("git rebase origin/feat/x", out)
        self.assertIn("dev push --force-with-lease", out)
        self.assertEqual(self.on_origin("feat/x"), theirs)

    def test_the_default_branch_and_commits_with_a_secret_stay_here(self):
        self.ci(CI_PR_AND_MAIN)
        self.commit("fix: commit straight on main")
        rc, out = self.dev("backup", "main")
        self.assertEqual(rc, 1)
        self.assertIn("main is the default branch, which is never backed up", out)
        git(self.repo, "switch", "-q", "-c", "feat/key", "main")
        token = "ghp_" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(36))
        (self.repo / "settings.py").write_text(f'GITHUB_TOKEN = "{token}"\n')
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "feat: read the settings")
        rc, out = self.dev("backup")
        self.assertEqual(rc, 1)
        self.assertIn("feat/key not backed up: the secret scan found 1 finding(s)", out)
        self.assertNotIn(token, out)
        self.assertEqual(self.on_origin("feat/key"), "")

    def test_a_draft_pull_request_takes_a_backup_only_when_its_jobs_skip_drafts(self):
        draft = [{"headRefName": "feat/d", "number": 7, "isDraft": True, "state": "OPEN"}]
        self.ci(CI_PR_AND_MAIN)
        self.branch("feat/d", HOUR)
        res = backup.run(self.repo, ["feat/d"], prs=draft, prs_known=True)[0]
        self.assertEqual((res["state"], res["reason"]),
                         ("skipped", "a push to this branch starts CI (ci.yml runs on draft PR #7)"))
        git(self.repo, "switch", "-q", "main")
        self.ci(CI_DRAFT_GATED)
        git(self.repo, "switch", "-q", "feat/d")
        git(self.repo, "rebase", "-q", "main")
        res = backup.run(self.repo, ["feat/d"], prs=draft, prs_known=True)[0]
        self.assertEqual((res["state"], res["reason"]), ("pushed", "ci.yml skips every job on draft PR #7"))
        self.assertEqual(backup.run(self.repo, ["feat/d"], prs=[], prs_known=False)[0]["state"], "nothing")

    def test_a_security_fix_on_a_public_repository_waits_for_an_embargo_or_public(self):
        self.ci(CI_PR_AND_MAIN)
        git(self.repo, "switch", "-q", "-c", "fix/hole", "main")
        tip = self.commit("fix(security): close the path traversal in the upload handler")
        # this public repository's branches: backed up
        branches.set_backup_policy(PID, "on", "open source", "ana", visibility="public")
        with mock.patch.object(backup, "_visibility", return_value="public"):
            rc, out = self.dev("backup")
            self.assertEqual(rc, 1)
            self.assertIn("it may carry a security fix", out)
            self.assertIn("dev branch embargo fix/hole", out)
            self.assertEqual(self.on_origin("fix/hole"), "")
            self.assertEqual(self.dev("backup", "--public")[0], 0)
        self.assertEqual(self.on_origin("fix/hole"), tip)


@unittest.skipUnless(HAS_GITLEAKS, "the secret scan needs gitleaks")
class PolicyTests(BackupCase):
    # The developer's decision (2026-11-06, refined): each project has a backup policy, on, off or ask, kept in its
    # handoff note. Without one, private repositories are on and public ones ask. With ask, dev backup asks the
    # developer once, in a terminal, and records the answer as on or off; dev sync and agents are never asked and
    # never publish. dev backup --public pushes once whatever the policy; it and setting the policy are the
    # developer's own actions.
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(backup, "_visibility", return_value="public")
        patch.start()
        self.addCleanup(patch.stop)
        self.ci(CI_PR_AND_MAIN)

    def backup(self, *argv):
        # `dev backup …` through devkit's own parser (cli.py takes the arguments from backup.register)
        return self.dev("backup", *argv)

    @contextlib.contextmanager
    def terminal(self, answer):
        # the developer at their own terminal, typing `answer`
        with mock.patch.object(core, "set_by_developer", return_value=True), \
                mock.patch.object(sys, "stdin", mock.Mock(isatty=lambda: True)), \
                mock.patch("builtins.input", return_value=answer) as asked:
            yield asked

    def never_asked(self):
        return mock.patch("builtins.input", side_effect=AssertionError("nobody may be asked here"))

    def sync(self):
        with self.never_asked():
            return [(r["branch"], r["state"]) for r in backup.sync({"projects": [self.project()]})]

    def test_sync_never_publishes_a_public_repository_whose_policy_asks(self):
        self.branch("feat/report", 2 * HOUR)
        with self.never_asked():
            done = backup.sync({"projects": [self.project()]})
        self.assertEqual([(r["branch"], r["state"]) for r in done], [("feat/report", "skipped")])
        self.assertEqual(done[0]["reason"], "the backup policy of acme/widget is ask, the default for a public "
                                            "repository, and the developer has not answered")
        self.assertIn("dev backup, run by the developer in a terminal, asks once", done[0]["fix"][0])
        self.assertEqual((self.on_origin("feat/report"), branches.backup_policy(PID)), ("", None))

    def test_an_agent_or_piped_input_is_never_asked_and_pushes_nothing(self):
        self.branch("feat/x", HOUR)
        with self.never_asked():
            rc, out = self.backup()
        self.assertEqual(rc, 1)
        self.assertIn("feat/x not backed up: the backup policy of acme/widget is ask", out)
        with mock.patch.object(core, "set_by_developer", return_value=True), self.never_asked():
            self.assertFalse(backup.developer_here())  # stdin is not a terminal: an answer could be piped in
            self.assertEqual(self.backup()[0], 1)
        self.assertEqual((self.on_origin("feat/x"), branches.backup_policy(PID)), ("", None))

    def test_the_developers_yes_records_on_and_dev_sync_follows_it(self):
        tip = self.branch("feat/x", HOUR)[-1]
        with self.terminal("y") as asked:
            rc, out = self.backup()
        self.assertEqual((rc, asked.call_count, self.on_origin("feat/x")), (0, 1, tip), out)
        self.assertIn("github.com/acme/widget is public: a backup publishes each branch there", out)
        self.assertEqual(branches.backup_policy(PID)["policy"], "on")
        self.assertIn("backup_policy:", core.note_path(PID).read_text())  # in the handoff note: every Mac, the team
        rec = self.records("dev.policy")[-1]
        self.assertEqual((rec["target"], rec["detail"]), (f"backup/{PID}", "on: the developer's answer to dev backup"))
        self.assertIn("Backup policy of acme/widget: on, set by", out)
        other = self.branch("feat/y", 2 * HOUR)[-1]
        self.assertEqual(self.sync(), [("feat/y", "pushed")])
        self.assertEqual(self.on_origin("feat/y"), other)

    def test_a_no_records_off_once_and_keeps_every_branch_here(self):
        self.branch("feat/x", 2 * HOUR)
        with self.terminal("n"):
            rc, out = self.backup()
        self.assertEqual(rc, 1)
        self.assertIn("feat/x not backed up: the backup policy of acme/widget is off, set by", out)
        with self.terminal("y") as asked:  # answered once: no second question
            self.assertEqual(self.backup()[0], 1)
        self.assertEqual((asked.call_count, branches.backup_policy(PID)["policy"]), (0, "off"))
        self.assertEqual(self.sync(), [("feat/x", "skipped")])
        row = {r["name"]: r for r in branches.map(self.repo, prs=[])}["feat/x"]
        self.assertEqual(row["backup"]["reason"], "the project's backup policy is off (dev backup policy)")
        self.assertEqual(self.on_origin("feat/x"), "")

    def test_the_policy_command_shows_and_sets_it_and_on_backs_up_a_public_repository_hourly(self):
        tip = self.branch("feat/oss", 2 * HOUR)[-1]
        rc, out = self.backup("policy")
        self.assertEqual(rc, 0)
        self.assertIn("acme/widget: backup policy ask, the default for a public repository", out)
        self.assertEqual(self.backup("policy", "on")[0], 2)  # no reason, no change
        self.assertEqual(self.backup("policy", "sometimes", "--why", "x")[0], 2)
        self.assertEqual(self.backup("policy", "on", "-p", "acme/nothing", "--why", "x")[0], 2)
        self.assertEqual(branches.backup_policy(PID), None)
        rc, out = self.backup("policy", "on", "--why", "open source: the code is public anyway")
        self.assertEqual(rc, 0, out)
        self.assertEqual(branches.backup_policy(PID)["why"], "open source: the code is public anyway")
        self.assertEqual(self.sync(), [("feat/oss", "pushed")])
        self.assertEqual(self.on_origin("feat/oss"), tip)
        rows = branches.map(self.repo, prs=[])
        self.assertIn("Backup policy: on, set by", " ".join(branches.status_lines(rows, cols=200)))
        self.assertIn("backup policy: on, set by", "\n".join(branches.note_lines(rows)))
        self.assertEqual(self.backup("policy", "off", "-p", PID, "--why", "a client asked")[0], 0)
        self.branch("feat/later", 2 * HOUR)
        self.assertEqual(self.sync(), [("feat/later", "skipped")])

    def test_an_on_set_while_private_stops_the_hourly_backup_once_the_repository_is_public(self):
        # the policy keeps the visibility it was decided for; public now (this class's setUp), so dev sync publishes
        # nothing until the developer answers again
        self.branch("feat/x", 2 * HOUR)
        with mock.patch.object(backup, "_visibility", return_value="private"):
            self.assertEqual(self.backup("policy", "on", "--why", "a client's private repository")[0], 0)
        self.assertEqual(branches.backup_policy(PID)["visibility"], "private")
        with self.never_asked():
            done = backup.sync({"projects": [self.project()]})
        self.assertEqual([(r["branch"], r["state"]) for r in done], [("feat/x", "skipped")])
        self.assertIn("was set while the repository was private, and it is public now", done[0]["reason"])
        self.assertEqual(self.on_origin("feat/x"), "")
        self.assertEqual(branches.backup_policy(PID)["policy"], "on")  # nothing changed without the developer

    def test_the_developer_answers_again_in_a_terminal_and_the_answer_keeps_the_new_visibility(self):
        tip = self.branch("feat/x", 2 * HOUR)[-1]
        branches.set_backup_policy(PID, "on", "private then", "ana", visibility="private")
        with self.terminal("y") as asked:
            rc, out = self.backup()
        self.assertEqual((rc, asked.call_count, self.on_origin("feat/x")), (0, 1, tip), out)
        self.assertEqual(branches.backup_policy(PID)["visibility"], "public")
        self.branch("feat/y", 2 * HOUR)
        self.assertEqual(self.sync(), [("feat/y", "pushed")])  # decided for public: no second question

    def test_an_on_recorded_without_its_visibility_asks_again_on_a_public_repository(self):
        # a record from before devkit kept the visibility: it may have been decided while the repository was private
        self.branch("feat/x", 2 * HOUR)
        branches.set_backup_policy(PID, "on", "recorded by an older devkit", "ana")
        self.assertEqual(self.sync(), [("feat/x", "skipped")])
        with mock.patch.object(backup, "_visibility", return_value="private"):
            self.assertEqual(self.sync(), [("feat/x", "pushed")])  # private: the record still holds

    def test_an_on_recorded_without_its_visibility_waits_while_the_visibility_is_unknown(self):
        # gh fails or the machine is offline: the on may have been decided while the repository was private
        self.branch("feat/x", 2 * HOUR)
        branches._edit_records(PID, branches.POLICY_KEY, lambda recs: [
            {"policy": "on", "why": "recorded by an older devkit", "who": "ana", "when": "2026-11-01T10:00:00Z"}])
        with mock.patch.object(backup, "_visibility", return_value=None), self.never_asked():
            done = backup.sync({"projects": [self.project()]})
        self.assertEqual([(r["branch"], r["state"]) for r in done], [("feat/x", "skipped")])
        self.assertIn("of a visibility devkit did not record", done[0]["reason"])
        self.assertEqual(self.on_origin("feat/x"), "")

    def test_an_answer_given_while_the_visibility_is_unknown_holds_until_it_is_known_public(self):
        tip = self.branch("feat/x", 2 * HOUR)[-1]
        with mock.patch.object(backup, "_visibility", return_value=None):
            with self.terminal("y") as asked:
                rc, out = self.backup()
            self.assertEqual((rc, asked.call_count, self.on_origin("feat/x")), (0, 1, tip), out)
            self.assertEqual(branches.backup_policy(PID)["visibility"], "unknown")
            self.branch("feat/y", 2 * HOUR)
            self.assertEqual(self.sync(), [("feat/y", "pushed")])  # still unknown: no second question
        self.branch("feat/z", 2 * HOUR)
        self.assertEqual(self.sync(), [("feat/z", "skipped")])  # public now (this class's setUp): asked again

    def test_an_on_holds_only_for_the_visibility_it_was_decided_for(self):
        legacy, unknown = {"policy": "on", "visibility": ""}, {"policy": "on", "visibility": "unknown"}
        for rec, now, outgrown in ((legacy, None, True), (legacy, "", True), (legacy, "public", True),
                                   (legacy, "private", False), (unknown, None, False), (unknown, "public", True),
                                   (unknown, "internal", False), ({"policy": "on", "visibility": "public"}, "public",
                                                                  False),
                                   ({"policy": "on", "visibility": "private"}, "public", True),
                                   ({"policy": "off", "visibility": ""}, None, False), (None, None, False)):
            with self.subTest(rec=rec, now=now):
                self.assertEqual(branches.outgrown_on(rec, now), outgrown)

    def test_public_pushes_once_whatever_the_policy_and_a_crew_member_cannot_use_it(self):
        tip = self.branch("feat/x", HOUR)[-1]
        self.assertEqual(self.backup("policy", "off", "--why", "a client's fork")[0], 0)
        with mock.patch.object(backup, "crew_member", return_value="link"):
            rc, out = self.backup("--public")
            self.assertEqual(rc, 2)
            self.assertIn("dev backup --public is the developer's own decision", out)
            self.assertEqual(self.backup("policy", "on", "--why", "x")[0], 2)
        self.assertEqual((self.on_origin("feat/x"), branches.backup_policy(PID)["policy"]), ("", "off"))
        rc, out = self.backup("--public")
        self.assertEqual((rc, self.on_origin("feat/x")), (0, tip), out)
        self.assertEqual(branches.backup_policy(PID)["policy"], "off")  # once: the policy stays


DEPLOY_ON_RELEASE = """name: deploy
on:
  push:
    branches: ['release/**']
jobs:
  deploy:
    runs-on: ubuntu-24.04
    environment: production
    steps:
      - run: echo deploy
"""


@unittest.skipUnless(HAS_GITLEAKS, "the secret scan needs gitleaks")
class PushTargetTests(BackupCase):
    # Follow-ups of the pipeline security review: a backup never pushes a long-lived or deploying branch, it checks
    # exactly the commits the push target lacks (a commit on another remote is still sent), and it reads visibility
    # and pull requests on the remote the push goes to, not on origin.
    def remote(self, name, url):
        bare = self.root / f"{name}.git"
        git(self.root, "init", "-q", "--bare", "-b", "main", str(bare))
        git(self.repo, "remote", "add", name, url)
        git(self.repo, "config", f"url.{bare}.insteadOf", url)
        return bare

    def test_long_lived_and_deploying_branches_are_never_backed_up(self):
        self.ci(CI_PR_AND_MAIN)
        for name in ("gh-pages", "develop", "master"):
            git(self.repo, "branch", name, "main")
        git(self.repo, "switch", "-q", "-c", "chore/deploy", "main")
        (self.repo / ".github" / "workflows" / "deploy.yml").write_text(DEPLOY_ON_RELEASE)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "ci: deploy release branches")
        git(self.repo, "switch", "-q", "-c", "release/x")
        self.commit("feat: the release part")
        got = {r["branch"]: r for r in backup.run(self.repo, ["gh-pages", "develop", "master", "release/x"],
                                                  accept_ci=True, public_ok=True)}
        for name in ("gh-pages", "develop", "master"):
            self.assertEqual((got[name]["state"], got[name]["reason"]),
                             ("skipped", f"{name} is a long-lived branch, which a backup never pushes"))
        self.assertEqual((got["release/x"]["state"], got["release/x"]["reason"]),
                         ("skipped", "deploy.yml deploys from it (job deploy, environment production): a push to it "
                                     "may deploy"))
        self.assertEqual([self.on_origin(n) for n in got], ["", "", "", ""])

    def test_commits_on_another_remote_are_still_scanned_and_checked(self):
        self.ci(CI_PR_AND_MAIN)
        self.remote("mirror", "https://github.com/acme/widget-mirror.git")
        git(self.repo, "switch", "-q", "-c", "feat/key", "main")
        token = "ghp_" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(36))
        (self.repo / "settings.py").write_text(f'GITHUB_TOKEN = "{token}"\n')
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "feat: read the settings")
        git(self.repo, "push", "-q", "mirror", "feat/key")  # on the mirror, not on origin, where the backup goes
        res = backup.run(self.repo, ["feat/key"])[0]
        self.assertEqual(res["state"], "skipped")
        self.assertIn("the secret scan found 1 finding(s)", res["reason"])
        self.assertNotIn(token, json.dumps(res))
        git(self.repo, "switch", "-q", "-c", "feat/wip", "main")
        self.commit("wip: half of it")
        git(self.repo, "push", "-q", "mirror", "feat/wip")
        self.assertIn("WIP commit", backup.run(self.repo, ["feat/wip"])[0]["reason"])
        self.assertEqual((self.on_origin("feat/key"), self.on_origin("feat/wip")), ("", ""))

    def test_an_embargo_holds_commits_that_only_another_remote_has(self):
        self.ci(CI_PR_AND_MAIN)
        self.remote("mirror", "https://github.com/acme/widget-mirror.git")
        self.branch("fix/hole", 2 * HOUR)
        self.assertEqual(self.dev("branch", "embargo", "fix/hole", "--why", "an open hole", "--until", "the fix")[0], 0)
        git(self.repo, "push", "-q", "mirror", "fix/hole")  # a private mirror has it; origin, where backups go, not
        git(self.repo, "switch", "-q", "-c", "release/x", "fix/hole")
        self.commit("chore: prepare the release", age=HOUR)
        res = backup.run(self.repo, ["release/x"])[0]
        self.assertEqual((res["state"], res["reason"]),
                         ("skipped", 'its commits are in fix/hole, which is embargoed until "the fix"'))
        self.assertEqual(self.on_origin("release/x"), "")

    def test_visibility_and_pull_requests_come_from_the_remote_the_push_goes_to(self):
        self.ci(CI_PR_AND_MAIN)
        fork = self.remote("fork", "https://github.com/ana/widget.git")
        self.branch("feat/x", HOUR)
        git(self.repo, "config", "branch.feat/x.remote", "fork")
        git(self.repo, "config", "branch.feat/x.merge", "refs/heads/feat/x")
        asked = []

        def gh(args, cwd=None, timeout=60):
            asked.append(args)
            if args[:2] == ["repo", "view"]:
                return 0, {"acme/widget": "PRIVATE", "ana/widget": "PUBLIC"}.get(args[2], ""), ""
            if args[:2] == ["pr", "list"] and "ana/widget" in args:
                return 0, json.dumps([{"headRefName": "feat/x", "number": 3, "isDraft": False, "state": "OPEN"}]), ""
            return 0, "[]", ""
        gitx.GH = gh
        with mock.patch.object(backup, "_visibility", REAL_VISIBILITY):
            res = backup.run(self.repo, ["feat/x"])[0]
            self.assertEqual((res["remote"], res["state"]), ("fork", "skipped"))
            self.assertIn("is ask, the default for a public repository", res["reason"])
            branches.set_backup_policy(PID, "on", "the fork is public on purpose", "ana", visibility="public")
            res = backup.run(self.repo, ["feat/x"])[0]
        self.assertEqual(res["reason"], "a push to this branch starts CI (ci.yml runs on PR #3)")
        self.assertIn(["repo", "view", "ana/widget", "--json", "visibility", "-q", ".visibility"], asked)
        rc, out, _ = core.sh(["git", "-C", str(fork), "rev-parse", "--verify", "-q", "refs/heads/feat/x"])
        self.assertNotEqual(rc, 0)


class BackupWithoutScannerTests(BackupCase):
    def test_no_gitleaks_means_no_push(self):
        self.ci(CI_PR_AND_MAIN)
        self.branch("feat/x", HOUR)
        with mock.patch.object(core, "have", side_effect=lambda tool: tool != "gitleaks"):
            res = backup.run(self.repo, ["feat/x"], prs=[], prs_known=True)[0]
        self.assertEqual(res["state"], "skipped")
        self.assertIn("needs gitleaks", res["reason"])
        self.assertEqual(self.on_origin("feat/x"), "")


class TaskBranchTests(BackupCase):
    def test_task_start_links_the_branch_it_runs_on(self):
        self.branch("feat/report", HOUR)
        it = work.create(PID, "Ship the report", why="the developer asked")
        os.chdir(self.repo)  # `dev task start` claims with the branch of the checkout it runs in
        self.assertEqual((cli._task_branch(PID), cli._task_branch("acme/other")), ("feat/report", ""))
        work.claim(it, branch=cli._task_branch(PID))
        self.assertIn("branch:feat/report", work.load(it["path"])["meta"]["links"])
        rows = {r["name"]: r for r in branches.map(self.repo, prs=[])}
        self.assertEqual(rows["feat/report"]["task"]["id"], it["meta"]["id"])


# --------------------------------------------------------------------------- closing merged branches
# One merge test for every branch: merged into the default branch as an ancestor, by its merged pull request (any
# method), or by content (every commit has an equivalent there, git cherry all "-"). `dev branch prune` lists every
# local and remote branch with its state and, with --apply, deletes the merged ones that nothing protects.


class MergeCase(Sandbox):
    def feature(self, name, *msgs, base="main", push=False, age=2 * HOUR):
        then = dict(core.GIT_ENV, GIT_COMMITTER_DATE=f"@{int(self.now - age)} +0000")  # the reflog keeps this time
        git(self.repo, "switch", "-q", "-c", name, base, env=then)
        tips = [self.commit(m, age=age) for m in msgs]
        if push:
            git(self.repo, "push", "-q", "-u", "origin", name)
        git(self.repo, "switch", "-q", "main")
        return tips

    def land(self, *picks):
        # commits reach origin/main the way a release branch carries them: cherry-picked, so the shas differ
        git(self.repo, "switch", "-q", "main")
        for sha in picks:
            git(self.repo, "cherry-pick", "--allow-empty", sha)
        git(self.repo, "push", "-q", "origin", "main")

    def squash(self, branch):
        git(self.repo, "switch", "-q", "main")
        git(self.repo, "merge", "-q", "--squash", branch)
        git(self.repo, "commit", "-q", "-m", f"feat: land {branch} (#7)")
        git(self.repo, "push", "-q", "origin", "main")

    def check(self, name, prs=None, ref=None):
        return branches.merge_test(self.repo, name, "origin/main", ref=ref, prs=prs)


class MergeTestTests(MergeCase):
    def test_a_branch_whose_tip_is_in_the_default_branch_is_merged_as_an_ancestor(self):
        self.feature("feat/ff", "feat: add the fast-forward thing")
        git(self.repo, "merge", "-q", "--ff-only", "feat/ff")
        git(self.repo, "push", "-q", "origin", "main")
        m = self.check("feat/ff")
        self.assertEqual((m["merged"], m["how"]), (True, "ancestor"))
        self.assertIn("in origin/main", branches.merge_words(m, "origin/main"))

    def test_a_branch_whose_commits_were_cherry_picked_is_merged_by_content(self):
        tips = self.feature("fix/picked", "fix: keep the first thing", "fix: keep the second thing")
        self.land(*tips)
        m = self.check("fix/picked")
        self.assertEqual((m["merged"], m["how"], m["ahead"]), (True, "content", 2))
        self.assertIn("2 commits have an equivalent in origin/main", branches.merge_words(m, "origin/main"))

    def pr(self, branch, head, merge=None, cross=False, n=7):
        return {"number": n, "state": "MERGED", "headRefName": branch, "headRefOid": head,
                "mergeCommit": {"oid": merge or git(self.repo, "rev-parse", "origin/main")}, "isCrossRepository": cross}

    def test_a_squash_merged_branch_is_merged_by_its_pull_request(self):
        tips = self.feature("feat/squashed", "feat: add part one", "feat: add part two")
        self.squash("feat/squashed")
        self.assertEqual(self.check("feat/squashed")["merged"], False)  # git cherry cannot see a squash
        m = self.check("feat/squashed", prs=[self.pr("feat/squashed", tips[-1])])
        self.assertEqual((m["merged"], m["how"], m["pr"]), (True, "pr", 7))
        self.assertEqual(branches.merge_words(m, "origin/main"), "merged: PR #7 was merged")

    def test_a_commit_after_the_merged_pull_request_keeps_the_branch_unmerged(self):
        tips = self.feature("feat/later", "feat: add part one")
        self.squash("feat/later")
        git(self.repo, "switch", "-q", "feat/later")
        self.commit("feat: add the part nobody merged")
        git(self.repo, "switch", "-q", "main")
        m = self.check("feat/later", prs=[self.pr("feat/later", tips[-1])])
        self.assertEqual((m["merged"], m["how"], m["ahead"]), (False, None, 2))

    def test_a_pull_request_from_another_repository_does_not_count(self):
        # a fork's PR may carry the name and head of someone else's branch
        tips = self.feature("fix/victim", "fix: work of the victim", "fix: more work of the victim")
        self.squash("fix/victim")
        m = self.check("fix/victim", prs=[self.pr("fix/victim", tips[-1], cross=True)])
        self.assertEqual((m["merged"], m["how"]), (False, None))

    def test_a_pull_request_merged_into_another_branch_does_not_count(self):
        tips = self.feature("feat/stacked", "feat: add part one", "feat: add part two")
        git(self.repo, "switch", "-q", "-c", "feat/base", "main")
        git(self.repo, "merge", "-q", "--squash", "feat/stacked")
        git(self.repo, "commit", "-q", "-m", "feat: land feat/stacked on feat/base (#7)")
        into_base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "switch", "-q", "main")
        m = self.check("feat/stacked", prs=[self.pr("feat/stacked", tips[-1], merge=into_base)])
        self.assertEqual((m["merged"], m["how"]), (False, None))

    def test_one_commit_without_an_equivalent_keeps_the_branch_unmerged(self):
        tips = self.feature("fix/half", "fix: keep the landed thing", "fix: keep the stranded thing")
        self.land(tips[0])
        m = self.check("fix/half")
        self.assertEqual((m["merged"], m["how"], m["missing"]), (False, None, 1))
        self.assertIn("1 of 2 commits not in origin/main", branches.merge_words(m, "origin/main"))

    def test_the_branch_map_counts_a_branch_merged_by_content_as_merged_so_no_backup_pushes_it_again(self):
        # the hourly backup pushed cherry-picked branches back to origin after the tech lead deleted them
        self.land(*self.feature("fix/picked", "fix: keep the picked thing", "fix: keep the other thing"))
        row = self.rows()["fix/picked"]
        self.assertEqual((row["merged_into"], row["backup"]["state"]), ("main", "merged"))
        self.assertIn("by content", row["backup"]["reason"])

    def test_a_remote_branch_is_tested_by_its_remote_ref(self):
        tips = self.feature("fix/remote", "fix: keep the remote thing", push=True)
        git(self.repo, "branch", "-q", "-D", "fix/remote")
        self.land(*tips)
        m = self.check("fix/remote", ref="refs/remotes/origin/fix/remote")
        self.assertEqual((m["merged"], m["how"]), (True, "content"))


class PruneCase(MergeCase):
    # gh answers `pr list` with self.prs; the handoff repository is there (no embargo); no process works anywhere
    def setUp(self):
        super().setUp()
        self.prs = []
        self.saved_gh = gitx.GH
        gitx.GH = self.gh
        Path(core.HANDOFF).mkdir(parents=True, exist_ok=True)
        for patch in (mock.patch.dict(os.environ, {"DEVKIT_NO_AUTOSYNC": "1"}),
                      mock.patch.object(core, "active_cwds", return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        gitx.GH = self.saved_gh
        super().tearDown()

    def gh(self, args, cwd=None, timeout=60):
        if list(args[:2]) == ["pr", "list"]:
            return 0, json.dumps(self.prs), ""
        return 1, "", "no canned answer for gh " + " ".join(args)

    def dev(self, *argv):
        os.chdir(self.repo)
        a = cli.build_parser().parse_args(list(argv))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = a.fn(a)
        return rc, " ".join((out.getvalue() + err.getvalue()).split())

    def local(self, name):
        rc, out, _ = core.sh(["git", "-C", str(self.repo), "rev-parse", "--verify", "-q", f"refs/heads/{name}"])
        return out if rc == 0 else ""

    def on_origin(self, name):
        rc, out, _ = core.sh(["git", "-C", str(self.origin), "rev-parse", "--verify", "-q", f"refs/heads/{name}"])
        return out if rc == 0 else ""

    def worktree(self, branch):
        path = self.root / ("wt-" + branch.replace("/", "-"))
        git(self.repo, "worktree", "add", "-q", str(path), branch)
        return path

    def records(self, action):
        out = []
        for f in sorted(Path(audit.AUDIT).glob("*.jsonl")):
            out += [r for r in map(json.loads, f.read_text().splitlines()) if r.get("action") == action]
        return out

    def scene(self):
        # feat/ff: fast-forwarded into main, also on origin. fix/picked: cherry-picked into main, only here, in a clean
        # worktree. feat/open: on origin with open PR #9. feat/wip: one commit main lacks. develop: a permanent
        # branch at main. feat/here: the branch checked out, at main.
        self.feature("feat/ff", "feat: add the fast-forward thing", push=True)
        git(self.repo, "merge", "-q", "--ff-only", "feat/ff")
        git(self.repo, "push", "-q", "origin", "main")
        self.land(*self.feature("fix/picked", "fix: keep the picked thing"))
        self.wt = self.worktree("fix/picked")
        open_tip = self.feature("feat/open", "feat: add the open thing", push=True)[-1]
        self.prs = [{"number": 9, "state": "OPEN", "headRefName": "feat/open", "headRefOid": open_tip,
                     "updatedAt": "2026-11-07T10:00:00Z"}]
        self.feature("feat/wip", "feat: add the unfinished thing")
        git(self.repo, "branch", "develop", "main")
        git(self.repo, "switch", "-q", "-c", "feat/here", "main")


class PruneTests(PruneCase):
    def test_without_apply_it_lists_every_branch_with_its_state_and_changes_nothing(self):
        self.scene()
        before = git(self.repo, "for-each-ref", "--format=%(refname) %(objectname)")
        origin_before = git(self.origin, "for-each-ref", "--format=%(refname) %(objectname)")
        rc, out = self.dev("branch", "prune")
        self.assertEqual(rc, 0, out)
        for needle in ("local feat/ff merged: its tip is in origin/main",
                       "remote feat/ff merged: its tip is in origin/main",
                       "local fix/picked merged by content: 1 commit has an equivalent in origin/main",
                       "remote feat/open open PR #9",
                       "local feat/wip not merged: 1 commit not in origin/main",
                       "local develop protected: a permanent branch",
                       "local feat/here protected: the current branch",
                       "local main protected: the default branch",
                       "dry run"):
            self.assertIn(needle, out)
        self.assertEqual(git(self.repo, "for-each-ref", "--format=%(refname) %(objectname)"), before)
        self.assertEqual(git(self.origin, "for-each-ref", "--format=%(refname) %(objectname)"), origin_before)
        self.assertTrue(self.wt.is_dir())

    def test_apply_deletes_the_merged_local_and_remote_branches_and_the_clean_worktree(self):
        self.scene()
        rc, out = self.dev("branch", "prune", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.local("feat/ff"), self.on_origin("feat/ff")), ("", ""))
        self.assertEqual(self.local("fix/picked"), "")
        self.assertFalse(self.wt.exists())
        for kept in ("feat/open", "feat/wip", "develop", "feat/here", "main"):
            self.assertTrue(self.local(kept), kept)
        self.assertTrue(self.on_origin("feat/open"))
        self.assertIn("deleted local fix/picked and its worktree", out)

    def test_each_deletion_is_audited_with_how_the_branch_was_merged(self):
        self.scene()
        self.dev("branch", "prune", "--apply")
        got = sorted((r["target"], r["detail"]) for r in self.records("branch.pruned"))
        self.assertEqual([t for t, _ in got], ["feat/ff", "feat/ff", "fix/picked"])
        details = " | ".join(d for _, d in got)
        self.assertIn("remote branch deleted, merged: its tip is in origin/main", details)
        self.assertIn("local branch deleted, merged: its tip is in origin/main", details)
        self.assertIn("local branch and its worktree deleted, merged by content", details)
        self.assertEqual({r["project"] for r in self.records("branch.pruned")}, {PID})

    def test_json_lists_the_state_and_how_of_each_branch(self):
        self.scene()
        rc, out = self.dev("branch", "prune", "--json")
        rows = {(r["where"], r["name"]): r for r in json.loads(out)["branches"]}
        self.assertEqual({k: (rows[k]["state"], rows[k]["how"], rows[k]["action"]) for k in (
            ("local", "fix/picked"), ("remote", "feat/open"), ("local", "feat/wip"), ("local", "develop"))},
            {("local", "fix/picked"): ("merged", "content", "delete"), ("remote", "feat/open"): ("open_pr", None, "keep"),
             ("local", "feat/wip"): ("not_merged", None, "keep"), ("local", "develop"): ("protected", "ancestor", "keep")})
        self.assertEqual(rows[("local", "feat/wip")]["ahead"], 1)

    def test_remote_only_leaves_the_local_branches(self):
        self.scene()
        rc, out = self.dev("branch", "prune", "--remote", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.on_origin("feat/ff"), "")
        self.assertTrue(self.local("feat/ff"))
        self.assertTrue(self.local("fix/picked"))

    def test_local_only_leaves_the_remote_branches(self):
        self.scene()
        rc, out = self.dev("branch", "prune", "--local", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.on_origin("feat/ff"))
        self.assertEqual(self.local("feat/ff"), "")

    def test_a_merged_branch_whose_worktree_has_uncommitted_changes_is_kept(self):
        self.land(*self.feature("fix/picked", "fix: keep the picked thing"))
        wt = self.worktree("fix/picked")
        (wt / "draft.txt").write_text("not committed\n")
        rc, out = self.dev("branch", "prune", "--apply")
        self.assertTrue(self.local("fix/picked"))
        self.assertTrue((wt / "draft.txt").exists())
        self.assertIn("local fix/picked kept: its worktree has uncommitted changes", out)

    def test_a_merged_branch_with_a_live_session_in_its_worktree_is_kept(self):
        self.land(*self.feature("fix/picked", "fix: keep the picked thing"))
        wt = self.worktree("fix/picked")
        sessions = Path(registry.STATE) / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / "s1.json").write_text(json.dumps({"agent": "claude", "session": "s1", "top": str(wt),
                                                      "pid": os.getpid(), "started": int(self.now)}))
        rc, out = self.dev("branch", "prune", "--apply")
        self.assertTrue(self.local("fix/picked"))
        self.assertTrue(wt.is_dir())
        self.assertIn("local fix/picked kept: a live session works in its worktree", out)

    def test_an_embargoed_merged_branch_is_kept(self):
        self.write_note(EMBARGO_NOTE)
        self.land(*self.feature("release/x", "feat: add the embargoed thing"))
        rc, out = self.dev("branch", "prune", "--apply")
        self.assertTrue(self.local("release/x"))
        self.assertIn("local release/x protected: embargoed", out)

    def test_a_merged_branch_with_an_open_pull_request_is_kept(self):
        tip = self.feature("feat/reopened", "feat: add the reopened thing", push=True)[-1]
        git(self.repo, "merge", "-q", "--ff-only", "feat/reopened")
        git(self.repo, "push", "-q", "origin", "main")
        self.prs = [{"number": 11, "state": "OPEN", "headRefName": "feat/reopened", "headRefOid": tip}]
        self.dev("branch", "prune", "--apply")
        self.assertTrue(self.local("feat/reopened"))
        self.assertTrue(self.on_origin("feat/reopened"))

    def test_a_remote_branch_that_moved_after_the_listing_is_kept(self):
        self.feature("feat/ff", "feat: add the fast-forward thing", push=True)
        git(self.repo, "merge", "-q", "--ff-only", "feat/ff")
        git(self.repo, "push", "-q", "origin", "main")
        os.chdir(self.repo)
        plan = branches.prune_plan(self.repo, where=("remote",))
        other = self.root / "other"
        git(self.root, "clone", "-q", str(self.origin), str(other))
        git(other, "switch", "-q", "feat/ff")
        self.commit("feat: add what a teammate pushed meanwhile", repo=other)
        git(other, "push", "-q", "origin", "feat/ff")
        moved = git(other, "rev-parse", "HEAD")
        results = branches.prune(plan)
        self.assertEqual(self.on_origin("feat/ff"), moved)
        self.assertEqual([r["result"] for r in results if r["name"] == "feat/ff"], ["failed"])

    def test_a_new_branch_without_commits_of_its_own_is_kept(self):
        git(self.repo, "branch", "feat/started", "main")
        rc, out = self.dev("branch", "prune", "--apply")
        self.assertTrue(self.local("feat/started"))
        self.assertIn("local feat/started kept: created", out)
        self.assertIn("no commits yet", out)

    def test_branch_prune_without_apply_is_not_audited_and_with_apply_it_is(self):
        self.assertFalse(audit._changes(["branch", "prune"]))
        self.assertFalse(audit._changes(["branch", "prune", "--json"]))
        self.assertTrue(audit._changes(["branch", "prune", "--apply"]))


if __name__ == "__main__":
    unittest.main()
