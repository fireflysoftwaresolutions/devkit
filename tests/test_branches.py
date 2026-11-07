# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The branch map (spec P-fy3bwa0pshk, R7, R8 and R11): one row per local branch, and the views that show it (the
# handoff note's generated block, `dev status`, `dev overview`, `dev branch ls` and the session-start context line).
#
# Offline: temporary repositories with a local bare "origin" (the GitHub URL is rewritten to it with insteadOf).
# devkit's state, handoff repository, audit trail and work items live in a temp folder; nothing reaches a real
# remote, GitHub or devkit's real state. Run: python3 -m unittest tests.test_branches -v
import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import audit, branches, canary, cli, core, crew, notes, progress, registry, work  # noqa: E402

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
        self.write_note(EMBARGO_NOTE.replace("release/x", "feat/inside"))
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
        self.write_note(EMBARGO_NOTE.replace("release/x", "feat/inside"))
        line = branches.context_line(branches.map(self.repo, now=self.now, prs=[]))
        self.assertIn("not backed up: `release/x` (2 commits only on this Mac)", line)
        self.assertIn("embargoed: `feat/inside`", line)
        git(self.repo, "push", "-q", "-u", "origin", "release/x")
        self.write_note("# acme/widget\n")
        self.assertIn("nothing is only on this Mac", branches.context_line(branches.map(self.repo, prs=[])))


if __name__ == "__main__":
    unittest.main()
