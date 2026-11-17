# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The registry's word for a pull request's checks (B3 of the pipeline security review). A draft's run skips every job
# that has the draft gate, and `dev pr ready` starts the real run, so a rollup whose every check was skipped means no
# real run yet: "none", never "passing". Pure functions, no state.
#
# A checkout's state (`dev wt ls`, the session-start context, the unpushed-commit issue) by the merge test: commits
# with an equivalent in origin's default branch are merged, not unpushed. Temporary repositories with a local bare
# origin; devkit's state and handoff repository in a temp folder; no process works anywhere and nothing reaches
# GitHub. Run: python3 -m unittest tests.test_registry -v
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import audit, core, registry, work  # noqa: E402

ISOLATE = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Ana Dev",
           "GIT_AUTHOR_EMAIL": "ana@example.com", "GIT_COMMITTER_NAME": "Ana Dev",
           "GIT_COMMITTER_EMAIL": "ana@example.com"}


def git(cwd, *args):
    rc, out, err = core.sh(["git", "-C", str(cwd), *args], timeout=60)
    if rc != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {err or out}")
    return out


class ChecksTests(unittest.TestCase):
    def test_every_check_skipped_is_no_checks_yet_and_a_skip_beside_a_pass_is_passing(self):
        draft_run = [{"name": "tests", "conclusion": "SKIPPED", "status": "COMPLETED"},
                     {"name": "security", "conclusion": "skipped", "status": "COMPLETED"}]
        self.assertEqual(registry._checks(draft_run), "none")
        self.assertEqual(registry._checks([{"conclusion": "SKIPPED"}, {"conclusion": "SUCCESS"}]), "passing")
        self.assertEqual(registry._checks([{"conclusion": "SKIPPED"}, {"status": "QUEUED"}]), "pending")
        self.assertEqual(registry._checks([{"conclusion": "SKIPPED"}, {"conclusion": "FAILURE"}]), "failing")


class NoteTasksBlockTests(unittest.TestCase):
    NOTE = ("# acme/api\n\n## Current state\n\nWrite a note like this:\n\n```markdown\n## Tasks\n- an example\n```\n\n"
            "The parser is done.\n\n## Tasks\nold block\n\n## Decisions and gotchas\n- keep it\n")

    def test_a_tasks_heading_inside_a_code_fence_keeps_the_text_around_it(self):
        with tempfile.TemporaryDirectory() as d:
            note = Path(d) / "api.md"
            note.write_text(self.NOTE)
            with mock.patch.object(registry, "note_path", return_value=note), \
                    mock.patch.object(registry, "render_tasks_block", return_value="NEW BLOCK"), \
                    mock.patch.object(registry, "render_auto", return_value="AUTO"):
                self.assertTrue(registry._update_note({"id": "acme/api"}, create=False))
            text = note.read_text()
        self.assertIn("```markdown\n## Tasks\n- an example\n```\n\nThe parser is done.", text)
        self.assertIn("## Tasks\nNEW BLOCK\n", text)
        self.assertNotIn("old block", text)
        self.assertIn("## Decisions and gotchas\n- keep it", text)


class CheckoutStateTests(unittest.TestCase):
    # The repository `widget` on main, its origin a bare repository; each feature lives in its own worktree.
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(core.real(tmp.name))
        state, handoff = self.root / "state", self.root / "handoff"
        for patch in (mock.patch.object(core, "STATE", state), mock.patch.object(registry, "STATE", state),
                      mock.patch.object(core, "HANDOFF", handoff), mock.patch.object(registry, "HANDOFF", handoff),
                      mock.patch.object(core, "NOTES", handoff / "projects"),
                      mock.patch.object(registry, "NOTES", handoff / "projects"),
                      mock.patch.object(work, "HANDOFF", handoff), mock.patch.object(work, "ITEMS", handoff / "items"),
                      mock.patch.object(audit, "AUDIT", state / "audit"),
                      mock.patch.object(core, "CLAUDE_HOME", self.root / ".claude"),
                      mock.patch.dict(core.GIT_ENV, ISOLATE),
                      mock.patch.object(core, "active_cwds", return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)
        self.n = 0
        origin = self.root / "origin.git"
        git(self.root, "init", "-q", "--bare", "-b", "main", str(origin))
        self.repo = self.root / "widget"
        git(self.root, "init", "-q", "-b", "main", str(self.repo))
        git(self.repo, "remote", "add", "origin", str(origin))
        self.commit(self.repo, "chore: start the widget")
        git(self.repo, "push", "-q", "-u", "origin", "main")
        git(self.repo, "remote", "set-head", "origin", "main")

    def commit(self, where, msg):
        self.n += 1
        (where / f"f{self.n}.txt").write_text(msg + "\n")
        git(where, "add", "-A")
        git(where, "commit", "-q", "-m", msg)
        return git(where, "rev-parse", "HEAD")

    def feature(self, name, *msgs, base="main"):
        path = self.root / ("wt-" + name.replace("/", "-"))
        git(self.repo, "worktree", "add", "-q", "-b", name, str(path), base)
        return [self.commit(path, m) for m in msgs]

    def land(self, *shas):
        # main takes the commits the way a release branch carries them: cherry-picked onto a newer main, so the shas
        # differ (on the same parent in the same second, git would rebuild the very same commit)
        self.commit(self.repo, "chore: move main on")
        for sha in shas:
            git(self.repo, "cherry-pick", sha)
        git(self.repo, "push", "-q", "origin", "main")

    def inspect(self):
        p = registry.inspect_repo(self.repo, with_prs=False)
        return p, {w["branch"]: w for w in p["worktrees"]}

    def on_github(self):
        # origin as GitHub names it, the local bare repository underneath: the registry reads its cached PR list
        url = "https://github.com/acme/widget.git"
        git(self.repo, "config", f"url.{self.root / 'origin.git'}.insteadOf", url)
        git(self.repo, "remote", "set-url", "origin", url)

    def squash(self, name, n):
        # GitHub squash-merges the branch's pull request into main (and deletes its remote branch: it never had one)
        git(self.repo, "merge", "-q", "--squash", name)
        git(self.repo, "commit", "-q", "-m", f"feat: land {name} (#{n})")
        git(self.repo, "push", "-q", "origin", "main")
        return git(self.repo, "rev-parse", "HEAD")

    def cache(self, prs):
        f = registry.STATE / "prs" / "acme__widget.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(prs))

    def test_a_worktree_merged_by_content_reads_merged_not_unpushed(self):
        # dev wt ls said "2 unpushed" for a branch whose commits main had cherry-picked
        self.land(*self.feature("fix/picked", "fix: keep the first thing", "fix: keep the second thing"))
        w = self.inspect()[1]["fix/picked"]
        self.assertEqual((w["unpushed"], w.get("merged")), (2, "content"))
        self.assertEqual(registry._state(w), "merged by content")

    def test_a_branch_carried_by_a_merged_branch_is_merged_and_one_only_in_local_main_is_not(self):
        inner = self.feature("fix/inner", "fix: keep the inner thing")
        outer = self.feature("fix/outer", "fix: keep the outer thing", base="fix/inner")
        self.land(*inner, *outer)
        self.feature("feat/local", "feat: add the thing main has only here")
        git(self.repo, "merge", "-q", "--ff-only", "feat/local")  # main is ahead of origin/main
        wts = self.inspect()[1]
        self.assertEqual([registry._state(wts[b]) for b in ("fix/inner", "fix/outer", "feat/local")],
                         ["merged by content", "merged by content", "1 unpushed"])

    def test_only_commits_without_an_equivalent_in_main_count_as_unpushed(self):
        landed, _ = self.feature("fix/half", "fix: keep the landed thing", "fix: keep the stranded thing")
        self.land(landed)
        w = self.inspect()[1]["fix/half"]
        self.assertEqual((w.get("merged"), registry._state(w)), (None, "1 unpushed"))

    def test_a_worktree_merged_by_content_raises_no_unpushed_commits_issue(self):
        self.land(*self.feature("fix/picked", "fix: keep the picked thing"))
        self.feature("feat/wip", "feat: add the thing nobody merged")
        p, wts = self.inspect()
        later = time.time() + 3 * 86400  # both idle three days
        issues = [i for b in ("fix/picked", "feat/wip") for i in registry.work_issues(p["id"], wts[b], now=later)]
        self.assertEqual(issues, [("work", f"{p['id']}: 1 unpushed commit(s) on feat/wip")])

    def test_a_worktree_squashed_by_its_own_pull_request_reads_merged_by_it_once_the_cache_has_its_head(self):
        # dev wt ls said "2 unpushed" for a branch whose pull request GitHub had squash-merged
        self.on_github()
        tip = self.feature("feat/squashed", "feat: add part one", "feat: add part two")[-1]
        landed = self.squash("feat/squashed", 7)
        pr = {"number": 7, "state": "MERGED", "headRefName": "feat/squashed", "title": "land it", "url": "",
              "updatedAt": "2026-11-07T10:00:00Z", "isDraft": False}
        self.cache([pr])  # as devkit 26.11.5 cached it: no head, no merge commit, so nothing new is said
        self.assertEqual(registry._state(self.inspect()[1]["feat/squashed"]), "2 unpushed")
        self.cache([dict(pr, headRefOid=tip, isCrossRepository=False, mergeCommit={"oid": landed})])
        w = self.inspect()[1]["feat/squashed"]
        self.assertEqual((w["unpushed"], w["merged"], w["merged_pr"]), (2, "pr", 7))
        self.assertEqual(registry._state(w), "merged by PR #7")

    def test_a_worktree_squashed_by_its_own_pull_request_raises_no_unpushed_commits_issue_and_a_forks_does(self):
        self.on_github()
        prs = []
        for n, (name, cross) in enumerate((("feat/ours", False), ("fix/theirs", True)), start=7):
            tip = self.feature(name, f"feat: add part one of {name}", f"feat: add part two of {name}")[-1]
            prs.append({"number": n, "state": "MERGED", "headRefName": name, "headRefOid": tip,
                        "isCrossRepository": cross, "mergeCommit": {"oid": self.squash(name, n)}})
        self.cache(prs)
        p, wts = self.inspect()
        later = time.time() + 3 * 86400  # both idle three days
        issues = [i for b in ("feat/ours", "fix/theirs") for i in registry.work_issues(p["id"], wts[b], now=later)]
        self.assertEqual(issues, [("work", f"{p['id']}: 2 unpushed commit(s) on fix/theirs")])


class PlansForTests(unittest.TestCase):
    # A project's plans by title: the front-matter title of a plan `dev plan new` writes (its body has no `# title`,
    # the front matter holds it), the top heading of a plan another tool wrote, and the file name of one with neither.
    def test_a_plan_is_named_by_its_front_matter_title_then_its_heading_then_its_file(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(registry, "HANDOFF", Path(d)):
            plans = Path(d) / "plans" / "acme" / "api"
            plans.mkdir(parents=True)
            (plans / "2026-11-11-invoice-export.md").write_text(
                "---\nid: P-x\ntitle: Invoice export\nkind: plan\n---\n## Summary\n\nExport invoices.\n\n"
                "## Changelog\n\n- v1 · created\n")
            (plans / "superpowers-spec.md").write_text("# Event store\n\n## Goal\n\nKeep events.\n")
            (plans / "notes.md").write_text("Loose notes.\n")
            titles = sorted(p["title"] for p in registry.plans_for("acme/api"))
        self.assertEqual(titles, ["Event store", "Invoice export", "notes"])


class GeneratedFilesTests(unittest.TestCase):
    # The handoff repository took a commit from every `dev sync` on every Mac: registry.json (this Mac's 1 MB cache,
    # read by no other Mac) 9,355 times, the projects map for a new time in its header, and each note's auto block
    # rewritten by Macs that do not even have the project. A real handoff repository in a temp folder, with devkit's
    # state, notes and work items there too; nothing is pushed and no background sync starts.
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(core.real(tmp.name))
        state, self.handoff = self.root / "state", self.root / "handoff"
        from devkit import canary
        for patch in (mock.patch.object(core, "STATE", state), mock.patch.object(registry, "STATE", state),
                      mock.patch.object(core, "REGISTRY", state / "registry.json"),
                      mock.patch.object(core, "HANDOFF", self.handoff),
                      mock.patch.object(registry, "HANDOFF", self.handoff),
                      mock.patch.object(core, "NOTES", self.handoff / "projects"),
                      mock.patch.object(registry, "NOTES", self.handoff / "projects"),
                      mock.patch.object(registry, "INITIATIVES", self.handoff / "initiatives"),
                      mock.patch.object(work, "HANDOFF", self.handoff),
                      mock.patch.object(work, "ITEMS", self.handoff / "items"),
                      mock.patch.object(work, "SESSIONS", self.handoff / "sessions"),
                      mock.patch.object(work, "request_sync", lambda *a, **k: False),
                      mock.patch.object(audit, "AUDIT", state / "audit"),
                      mock.patch.object(canary, "summary_line", return_value="Claude 9/10 (90%)"),
                      mock.patch.dict(core.GIT_ENV, ISOLATE)):
            patch.start()
            self.addCleanup(patch.stop)
        git(self.root, "init", "-q", "-b", "main", str(self.handoff))
        (self.handoff / ".gitignore").write_text(".DS_Store\n*.tmp\n")
        (self.handoff / "registry.json").write_text('{"host": "mbp", "projects": []}\n')
        note = self.handoff / "projects" / "acme" / "weave.md"
        note.parent.mkdir(parents=True)
        note.write_text("# acme/weave\n\n## Current state\n\nThe parser is done.\n")
        git(self.handoff, "add", "-A")
        git(self.handoff, "commit", "-q", "-m", "start: as an older devkit left it, registry.json tracked")

    def reg(self, at: float = 1794900000.0) -> dict:
        """The projects map of a sync at `at`: one project with a note and no checkout on this Mac."""
        p = registry.note_only_projects(set())[0]
        p["last_activity"] = int(at) - 7200
        return {"host": "mbp", "generated": at, "issues": [], "projects": [p], "sessions": []}

    def test_a_sync_writes_the_projects_map_to_the_state_folder_and_leaves_the_handoff_copy_as_git_has_it(self):
        # The copy an older devkit committed stays tracked and unchanged: this Mac neither rewrites nor untracks it, so
        # a Mac still on 26.11.19, which commits its own copy, never trades the file with this one.
        older = (self.handoff / "registry.json").read_text()
        registry.write_outputs(self.reg())
        self.assertTrue(core.REGISTRY.is_file())
        self.assertEqual(json.loads(core.REGISTRY.read_text())["generated"], 1794900000.0)
        later = self.reg(at=1794900000.0 + 86400)
        later["projects"][0]["stack"] = ["python"]
        changed = registry.write_outputs(later)
        self.assertEqual(json.loads(core.REGISTRY.read_text())["projects"][0]["stack"], ["python"])
        self.assertNotIn("registry.json", changed)  # the files of the handoff repository that changed
        self.assertEqual((self.handoff / "registry.json").read_text(), older)
        self.assertEqual(git(self.handoff, "ls-files", "--", "registry.json"), "registry.json")
        self.assertEqual(git(self.handoff, "status", "--porcelain", "--", "registry.json"), "")
        ignore = (self.handoff / ".gitignore").read_text()
        self.assertTrue(ignore.startswith(".DS_Store\n*.tmp\n"), ignore)  # the repository's own rules stay first
        self.assertFalse(registry.ensure_handoff_ignore(self.handoff))  # the managed block stays one block

    def test_a_handoff_made_before_its_template_ignored_temporary_files_ignores_them_after_a_sync(self):
        # in this Mac's own exclude file: a shared .gitignore rule would be rewritten by Macs on an older devkit
        (self.handoff / ".gitignore").write_text(".DS_Store\n")
        registry.write_outputs(self.reg())
        (self.handoff / "items").mkdir(exist_ok=True)
        (self.handoff / "items" / ".T-abc.md.k2j3h4.tmp").write_text("half a record")
        self.assertEqual(git(self.handoff, "status", "--porcelain", "--untracked-files=all", "--", "items"), "")
        self.assertNotIn("tmp", (self.handoff / ".gitignore").read_text())
        registry.write_outputs(self.reg())  # once
        self.assertEqual((self.handoff / ".git" / "info" / "exclude").read_text().splitlines().count(".*.tmp"), 1)

    def test_the_readers_take_the_state_folders_map_and_the_handoff_copy_only_before_the_first_sync(self):
        from devkit import bugs, completion, dashserver, prompt, sleep
        clone = self.root / "Development" / "acme" / "weave"
        clone.mkdir(parents=True)
        # before this Mac's first sync of a devkit that keeps the map in its state folder: the handoff copy
        self.assertEqual(prompt._registry(build=False), {"host": "mbp", "projects": []})
        self.assertEqual(completion._registry(), {"host": "mbp", "projects": []})
        (self.handoff / "registry.json").write_text(json.dumps({"host": "other-mac", "projects": [
            {"id": "acme/elsewhere", "path": str(self.root), "last_activity": int(time.time())}]}))
        watched = dashserver.fingerprint(dashserver.watched_paths())
        core.REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        core.REGISTRY.write_text(json.dumps({"host": "mbp", "projects": [
            {"id": "acme/weave", "path": str(clone), "last_activity": int(time.time()) - 60}]}))
        self.assertEqual([p["id"] for p in prompt._registry(build=False)["projects"]], ["acme/weave"])
        self.assertEqual([p["id"] for p in completion._registry()["projects"]], ["acme/weave"])
        self.assertEqual(sleep.checkouts(), {"acme/weave": clone})
        self.assertIn("weave", bugs.deny_words())
        self.assertNotIn("elsewhere", bugs.deny_words())
        self.assertNotEqual(dashserver.fingerprint(dashserver.watched_paths()), watched)  # the page refreshes
        # a map that cannot be read is no map: never the handoff copy of another Mac
        core.REGISTRY.write_text("{")
        self.assertEqual(completion._registry(), {})

    def test_a_projects_map_whose_only_change_is_a_time_is_not_written_again(self):
        registry.write_outputs(self.reg())
        before = (self.handoff / "PROJECTS.md").read_bytes()
        later = self.reg(at=1794900000.0 + 3600)  # a new header time and a new last activity, nothing else
        changed = registry.write_outputs(later)
        self.assertNotIn("PROJECTS.md", changed)
        self.assertEqual((self.handoff / "PROJECTS.md").read_bytes(), before)
        self.assertEqual(json.loads(core.REGISTRY.read_text())["generated"], later["generated"])
        later["projects"][0]["stack"] = ["python"]
        self.assertIn("PROJECTS.md", registry.write_outputs(later))
        self.assertIn("| python |", (self.handoff / "PROJECTS.md").read_text())

    def test_write_generated_skips_a_text_that_differs_only_in_its_times(self):
        f = self.root / "TASKS.md"
        f.write_text("# Tasks\n_Generated on mbp · 2026-11-16 23:09_\n- export invoices\n")
        self.assertFalse(registry.write_generated(f, "# Tasks\n_Generated on mbp · 2026-11-17 08:41_\n- export invoices\n"))
        self.assertEqual(f.read_text(), "# Tasks\n_Generated on mbp · 2026-11-16 23:09_\n- export invoices\n")
        self.assertTrue(registry.write_generated(f, "# Tasks\n_Generated on mbp · 2026-11-17 08:41_\n- import invoices\n"))
        self.assertEqual(f.read_text(), "# Tasks\n_Generated on mbp · 2026-11-17 08:41_\n- import invoices\n")
        self.assertTrue(registry.write_generated(f, "# Tasks\n_Generated on mbp_\n- import invoices\n"))  # a time gone
        self.assertTrue(registry.write_generated(self.root / "ACTIVITY.md", "# Activity\n"))  # no file yet

    def test_a_mac_without_the_clone_leaves_the_notes_auto_block_as_the_mac_with_it_wrote_it(self):
        clone = self.root / "Development" / "acme" / "weave"
        clone.mkdir(parents=True)
        on_the_clones_mac = {"id": "acme/weave", "path": str(clone), "canonical_ok": True, "stack": ["python"],
                             "commands": ["make test"], "main": None, "worktrees": []}
        self.assertTrue(registry.update_note(on_the_clones_mac))
        note = self.handoff / "projects" / "acme" / "weave.md"
        written = note.read_bytes()
        here = next(p for p in registry.note_only_projects(set()) if p["id"] == "acme/weave")
        self.assertFalse(registry.update_note(here))
        self.assertEqual(note.read_bytes(), written)
        # the tasks block is shared: this Mac still writes it, around the other Mac's auto block
        work.create("acme/weave", "Export invoices", why="finance asked", a=work.actor("human", user="ana"))
        self.assertTrue(registry.update_note(here))
        text = note.read_text()
        self.assertIn("Export invoices", text)
        block = lambda t: t[t.index(registry.AUTO_START):t.index(registry.AUTO_END)]  # noqa: E731
        self.assertEqual(block(text), block(written.decode()))
        self.assertIn("`make test`", block(text))


if __name__ == "__main__":
    unittest.main()
