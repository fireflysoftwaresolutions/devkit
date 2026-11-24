# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev branch|commit|rebase|push|pr|run|release|issue|git|gh` and the local-CI evidence.

Offline: real temporary repositories with a bare "origin" (the GitHub URL is rewritten to it with insteadOf), and
`gh` replaced by canned JSON. Run: python3 -m unittest tests.test_gitx -v
"""
import argparse
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import audit, cistatus, core, delivery, gitrules, gitx, registry, ui, work  # noqa: E402

GITHUB_URL = "https://github.com/acme/widget.git"
ISOLATE = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Ana Dev",
           "GIT_AUTHOR_EMAIL": "ana@example.com", "GIT_COMMITTER_NAME": "Ana Dev",
           "GIT_COMMITTER_EMAIL": "ana@example.com"}
YY, MM = time.localtime().tm_year % 100, time.localtime().tm_mon
REAL_TEMPLATE_DIR = gitx.TEMPLATE_DIR


# The JSON fields `gh repo view --json` and `gh pr view --json` accept (gh 2.103): gh refuses any other with
# "Unknown JSON field", so the fake refuses them too. dev pr merge once asked for autoMergeAllowed, which gh's repo
# view does not have, and every merge failed while the tests, which accepted any field, passed.
GH_JSON_FIELDS = {
    ("repo", "view"): set("""archivedAt assignableUsers codeOfConduct contactLinks createdAt defaultBranchRef
        deleteBranchOnMerge description diskUsage forkCount fundingLinks hasDiscussionsEnabled hasIssuesEnabled
        hasProjectsEnabled hasWikiEnabled homepageUrl id isArchived isBlankIssuesEnabled isEmpty isFork
        isInOrganization isMirror isPrivate isSecurityPolicyEnabled isTemplate isUserConfigurationRepository
        issueTemplates issues labels languages latestRelease licenseInfo mentionableUsers mergeCommitAllowed
        milestones mirrorUrl name nameWithOwner openGraphImageUrl owner parent primaryLanguage projects projectsV2
        pullRequestTemplates pullRequests pushedAt rebaseMergeAllowed repositoryTopics securityPolicyUrl
        squashMergeAllowed sshUrl stargazerCount templateRepository updatedAt url usesCustomOpenGraphImage
        viewerCanAdminister viewerDefaultCommitEmail viewerDefaultMergeMethod viewerHasStarred viewerPermission
        viewerPossibleCommitEmails viewerSubscription visibility watchers""".split()),
    ("pr", "view"): set("""additions assignees author autoMergeRequest baseRefName baseRefOid body changedFiles closed
        closedAt closingIssuesReferences comments commits createdAt deletions files fullDatabaseId headRefName
        headRefOid headRepository headRepositoryOwner id isCrossRepository isDraft labels latestReviews
        maintainerCanModify mergeCommit mergeStateStatus mergeable mergedAt mergedBy milestone number
        potentialMergeCommit projectCards projectItems reactionGroups reviewDecision reviewRequests reviews state
        statusCheckRollup title updatedAt url""".split()),
}


class FakeGH:
    """Canned `gh`: the newest matching route wins; every call is recorded. Like gh, it refuses a JSON field that
    `repo view` or `pr view` does not have."""

    def __init__(self):
        self.calls, self.routes = [], []

    def on(self, prefix, out="", rc=0, err="", when=None):
        self.routes.insert(0, (list(prefix), rc, out if isinstance(out, str) else json.dumps(out), err, when))

    def __call__(self, args, cwd=None, timeout=60):
        args = list(args)
        self.calls.append(args)
        known = GH_JSON_FIELDS.get(tuple(args[:2]))
        if known and "--json" in args:
            unknown = [f for f in args[args.index("--json") + 1].split(",") if f not in known]
            if unknown:
                return 1, "", f'Unknown JSON field: "{unknown[0]}"'
        for prefix, rc, out, err, when in self.routes:
            if args[:len(prefix)] == prefix and (when is None or when(args)):
                return rc, out, err
        return 1, "", "no canned response for: gh " + " ".join(args)

    def called(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


def _parser():
    ap = argparse.ArgumentParser(prog="dev")
    sp = ap.add_subparsers(dest="cmd")

    def add(name, fn, help_):
        p = sp.add_parser(name, help=help_, description=help_)
        p.set_defaults(fn=fn)
        return p

    gitx.register(add)
    p = add("ci", lambda a: gitx.ci_command(a, core.gitout(os.getcwd(), "rev-parse", "--show-toplevel")), "ci")
    p.add_argument("--json", action="store_true")
    gitx.add_ci_arguments(p)
    return ap


PARSER = _parser()


def dev(*argv):
    a = PARSER.parse_args([str(x) for x in argv])
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = a.fn(a)
    return rc, out.getvalue(), err.getvalue()


def git(cwd, *args, env=None):
    rc, out, err = core.sh(["git", "-C", str(cwd), *args], env=env, timeout=60)
    if rc != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {err or out}")
    return out


# A workflow that runs on every push: the push budget counts only pushes that start CI
CI_ON_PUSH = {".github/workflows/ci.yml": "on: [push]\njobs:\n  t:\n    runs-on: ubuntu-latest\n    steps:\n"
                                          "      - run: echo hi\n"}


class Sandbox:
    """A repository `widget` whose origin is a bare repo, devkit state in a temp dir, gh faked."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.saved_git_env = {k: core.GIT_ENV.get(k) for k in ISOLATE}
        core.GIT_ENV.update(ISOLATE)
        self.saved_os = {k: os.environ.pop(k, None) for k in ("DEVKIT_FLAKY", "DEVKIT_CROSS_PROJECT", "DEVKIT_AGENT")}
        self.saved = (core.STATE, core.HANDOFF, gitx.GH, gitx.EXEC, gitx.SLEEP, gitx._me, gitx._handoff_log,
                      gitx.TEMPLATE_DIR, cistatus.runs_for, delivery.POLICIES, work.HANDOFF, work.ITEMS, audit.AUDIT)
        core.STATE = self.root / "state"  # with this Mac's projects map: core.registry_cache()
        delivery.POLICIES = self.root / "handoff" / "policies"
        core.HANDOFF = work.HANDOFF = self.root / "handoff"
        core.HANDOFF.mkdir(exist_ok=True)  # this Mac has its handoff repository: dev push reads its embargoes there
        work.ITEMS = self.root / "handoff" / "items"
        audit.AUDIT = self.root / "audit"
        self.gh = gitx.GH = FakeGH()
        real_sh = registry.sh
        self.saved_registry = (registry.sh, registry.STATE)
        registry.STATE = core.STATE  # the PR cache the dashboard reads; its gh calls go to the fake too
        registry.sh = lambda args, **kw: self.gh(list(args)[1:]) if list(args)[:1] == ["gh"] else real_sh(args, **kw)
        self.gh.on(["pr", "view"], rc=1, err="no pull requests found")
        self.gh.on(["pr", "list"], [])
        self.execs = []
        gitx.EXEC = lambda argv, cwd=None, env=None: (self.execs.append((list(argv), cwd)), 0)[1]
        gitx.SLEEP = lambda s: None
        gitx._me = lambda: "ana"
        self.logged = []
        gitx._handoff_log = lambda pid, text: (self.logged.append((pid, text)), True)[1]
        self.cwd = os.getcwd()
        self.origin = self.root / "origin.git"
        git(self.root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.repo = self.root / "widget"
        git(self.root, "init", "-q", "-b", "main", str(self.repo))
        git(self.repo, "remote", "add", "origin", GITHUB_URL)
        git(self.repo, "config", f"url.{self.origin}.insteadOf", GITHUB_URL)
        self.write("README.md", "# widget\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "chore: start the widget")
        git(self.repo, "push", "-q", "-u", "origin", "main")
        git(self.repo, "remote", "set-head", "origin", "main")
        os.chdir(self.repo)
        return self

    def __exit__(self, *exc):
        os.chdir(self.cwd)
        (core.STATE, core.HANDOFF, gitx.GH, gitx.EXEC, gitx.SLEEP, gitx._me, gitx._handoff_log, gitx.TEMPLATE_DIR,
         cistatus.runs_for, delivery.POLICIES, work.HANDOFF, work.ITEMS, audit.AUDIT) = self.saved
        registry.sh, registry.STATE = self.saved_registry
        for k, v in self.saved_git_env.items():
            if v is None:
                core.GIT_ENV.pop(k, None)
            else:
                core.GIT_ENV[k] = v
        for k, v in self.saved_os.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        self.tmp.cleanup()

    def write(self, rel, text, repo=None):
        f = Path(repo or self.repo) / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        return f

    def commit(self, msg, files=None, repo=None):
        repo = repo or self.repo
        for rel, text in (files or {f"f{time.time_ns()}.txt": msg}).items():
            self.write(rel, text, repo)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", msg)
        return git(repo, "rev-parse", "HEAD")

    def other(self):
        """A second clone of origin (another developer)."""
        o = self.root / "other"
        if not o.exists():
            git(self.root, "clone", "-q", str(self.origin), str(o))
        return o

    def advance_main(self, n=1):
        o = self.other()
        git(o, "pull", "-q", "--ff-only", "origin", "main")
        for i in range(n):
            self.commit(f"fix: upstream change {i}", repo=o)
        git(o, "push", "-q", "origin", "main")

    def branch(self, name):
        git(self.repo, "switch", "-q", "-c", name)

    def head(self, rev="HEAD", repo=None):
        return git(repo or self.repo, "rev-parse", rev)

    def ctx(self):
        return gitx.ctx(self.repo)


# --------------------------------------------------------------------------- pure rules


class RuleTests(unittest.TestCase):
    def test_conventional_commit_headers(self):
        for h in ("feat(cli): add branch ls", "fix!: drop python 3.8", "deps: bump gh to 2.103",
                  "docs: explain the push budget", "feat: nested routes for admins", "fix: shared cache eviction",
                  "fix: address parsing of SCP urls", "refactor(core): process the remote once"):
            with self.subTest(h=h):
                self.assertIsNone(gitx.message_violation(h))
        bad = {"Add stuff": "not a Conventional Commit", "feature: add x": "unknown type",
               "Feat: add x": "lowercase", "feat: added x": "imperative", "fix: handles empty input": "imperative",
               "feat: adding x": "imperative", "fix: drop the cache.": "period", "wip: save": "WIP",
               "feat: " + "x" * 80: "max 72", "": "empty"}
        for h, why in bad.items():
            with self.subTest(h=h):
                self.assertIn(why, gitx.message_violation(h) or "")

    def test_branch_names(self):
        self.assertEqual(gitx.branch_name("feat/Invoice Export"), "feat/invoice-export")
        self.assertEqual(gitx.branch_name("feat/invoice export", "proj-142"), "feat/PROJ-142-invoice-export")
        self.assertEqual(gitx.branch_name("fix/login", "123"), "fix/123-login")
        self.assertEqual(gitx.branch_name("feat/PROJ-9 Login form"), "feat/PROJ-9-login-form")
        self.assertEqual(gitx.branch_name("release/26.11.4"), "release/26.11.4")
        self.assertIsNotNone(gitrules.branch_violation(gitx.branch_name("claude/fix-it")))

    def test_strip_attribution(self):
        msg = ("feat: add x\n\nWhy it matters.\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"
               "🤖 Generated with [Claude Code](https://claude.com/claude-code)\nCo-authored-by: GitHub Copilot <x>")
        clean, n = gitx.strip_attribution(msg)
        self.assertEqual(n, 3)
        self.assertEqual(clean, "feat: add x\n\nWhy it matters.")


# --------------------------------------------------------------------------- dev branch


class BranchTests(unittest.TestCase):
    def test_new_branches_from_an_up_to_date_origin(self):
        with Sandbox() as s:
            s.advance_main()
            local_main = s.head("main")
            rc, out, err = dev("branch", "new", "feat/Invoice Export", "--ticket", "proj-7")
            self.assertEqual(rc, 0, err)
            self.assertEqual(git(s.repo, "branch", "--show-current"), "feat/PROJ-7-invoice-export")
            self.assertEqual(s.head(), s.head("origin/main"))
            self.assertNotEqual(s.head(), local_main)
            self.assertIn("from origin/main", out)

    # An item claimed on main, before its branch existed, had no branch link: its lock never renewed while someone
    # worked on the branch, and the merge never closed it.
    def test_a_new_branch_links_the_claimed_item_that_has_no_branch_yet(self):
        from devkit import cli
        for command in ("dev branch new", "dev wt new"):
            with self.subTest(command), Sandbox() as s:
                held = work.claim(work.create("acme/widget", "Export invoices", a=ME), a=ME)
                other = work.claim(work.create("acme/widget", "Refunds", a=ME), a=ME, branch="feat/refunds")
                if command == "dev branch new":
                    rc, said, err = dev("branch", "new", "feat/export")
                    self.assertEqual(rc, 0, err)
                else:
                    p = {"path": str(s.repo), "id": "acme/widget", "remote": True, "default_branch": "main"}
                    err = io.StringIO()
                    with mock.patch.object(cli, "WT_ROOT", s.root / "worktrees"), contextlib.redirect_stderr(err):
                        self.assertTrue(cli.wt_new(p, "feat/export", deps=False))
                    said = err.getvalue()
                self.assertEqual(work.load(held["path"])["meta"]["links"], ["branch:feat/export"])
                self.assertEqual(work.load(other["path"])["meta"]["links"], ["branch:feat/refunds"])
                self.assertIn(f"linked {held['meta']['id']} “Export invoices” to feat/export", said)

    def test_new_refuses_bad_names_and_duplicates(self):
        with Sandbox():
            rc, _, err = dev("branch", "new", "claude/fix-thing")
            self.assertEqual(rc, 2)
            self.assertIn("tool/agent", err)
            self.assertEqual(dev("branch", "new", "feat/a")[0], 0)
            self.assertEqual(dev("branch", "new", "feat/a")[0], 2)

    def test_rename_harness_branch_before_the_first_push_only(self):
        with Sandbox() as s:
            s.branch("claude/abc123")
            rc, out, err = dev("branch", "rename", "fix/login-timeout")
            self.assertEqual(rc, 0, err)
            self.assertEqual(git(s.repo, "branch", "--show-current"), "fix/login-timeout")
            git(s.repo, "push", "-q", "-u", "origin", "fix/login-timeout")
            rc, _, err = dev("branch", "rename", "fix/session-timeout")
            self.assertEqual(rc, 2)
            self.assertIn("already on origin", err)

    def test_rename_detached_head_creates_the_branch(self):
        with Sandbox() as s:
            git(s.repo, "checkout", "-q", "--detach")
            self.assertEqual(dev("branch", "rename", "feat/x")[0], 0)
            self.assertEqual(git(s.repo, "branch", "--show-current"), "feat/x")

    def test_ls_and_clean(self):
        with Sandbox() as s:
            s.branch("feat/merged")
            s.commit("feat: add merged thing")
            git(s.repo, "switch", "-q", "main")
            git(s.repo, "merge", "-q", "--ff-only", "feat/merged")
            git(s.repo, "push", "-q", "origin", "main")
            s.branch("feat/open")
            s.commit("feat: add open thing")
            git(s.repo, "switch", "-q", "main")
            s.branch("feat/gone")
            s.commit("feat: add gone thing")
            git(s.repo, "push", "-q", "-u", "origin", "feat/gone")
            git(s.repo, "push", "-q", "origin", "--delete", "feat/gone")
            git(s.repo, "switch", "-q", "main")
            s.branch("feat/squashed")
            s.commit("feat: add squashed thing")
            tip = s.commit("feat: finish squashed thing")
            git(s.repo, "switch", "-q", "main")
            git(s.repo, "merge", "-q", "--squash", "feat/squashed")  # two commits in one: only its PR tells
            git(s.repo, "commit", "-q", "-m", "feat: add squashed thing (#9)")
            git(s.repo, "push", "-q", "origin", "main")
            git(s.repo, "branch", "feat/in-wt", "feat/merged")
            wt = s.root / "wt"
            git(s.repo, "worktree", "add", "-q", str(wt), "feat/in-wt")
            s.gh.on(["pr", "list"], [{"number": 9, "state": "MERGED", "headRefName": "feat/squashed",
                                      "headRefOid": tip, "isCrossRepository": False,
                                      "mergeCommit": {"oid": s.head("main")}, "updatedAt": "2026-11-01T10:00:00Z"}])
            s.branch("feat/current")
            git(s.repo, "fetch", "-q", "--prune", "origin")
            rc, out, _ = dev("branch", "ls", "--json")
            rows = {r["name"]: r for r in json.loads(out)}  # the branch map
            self.assertEqual(rows["feat/merged"]["merged_into"], "main")
            self.assertIsNone(rows["feat/open"]["merged_into"])
            self.assertTrue(rows["feat/gone"]["upstream_gone"])
            self.assertEqual((rows["feat/squashed"]["merged_how"], rows["feat/squashed"]["merged_pr"]), ("pr", 9))
            self.assertTrue(rows["feat/current"]["current"])
            self.assertEqual(rows["feat/open"]["base_ahead"], 1)
            rc, out, _ = dev("branch", "clean")
            self.assertIn("dry run", out)
            self.assertTrue(gitx._ref_sha(s.repo, "refs/heads/feat/merged"))
            rc, out, err = dev("branch", "clean", "--apply")
            self.assertEqual(rc, 0, err)
            left = git(s.repo, "branch", "--format=%(refname:short)").split()
            self.assertNotIn("feat/merged", left)
            self.assertNotIn("feat/squashed", left)
            for kept in ("feat/open", "feat/gone", "feat/in-wt", "feat/current", "main"):
                self.assertIn(kept, left)
            self.assertIn("exist only here", out)

    def test_clean_deletes_a_branch_merged_by_content(self):
        # main took its commit by cherry-pick (a release branch carried it): no ancestor, no pull request
        with Sandbox() as s:
            s.branch("fix/picked")
            tip = s.commit("fix: keep the picked thing")
            git(s.repo, "switch", "-q", "main")
            s.commit("docs: move main on")  # so the cherry-pick is a new commit
            git(s.repo, "cherry-pick", tip)
            git(s.repo, "push", "-q", "origin", "main")
            rc, out, err = dev("branch", "clean", "--apply")
            self.assertEqual(rc, 0, err)
            self.assertNotIn("fix/picked", git(s.repo, "branch", "--format=%(refname:short)").split())
            self.assertIn("fix/picked  merged by content: 1 commit has an equivalent in origin/main", out)

    def test_clean_offline_finds_a_branch_merged_by_content_without_github(self):
        with Sandbox() as s:
            s.branch("fix/picked")
            tip = s.commit("fix: keep the picked thing")
            git(s.repo, "switch", "-q", "main")
            s.commit("docs: move main on")
            git(s.repo, "cherry-pick", tip)
            git(s.repo, "push", "-q", "origin", "main")
            rc, out, err = dev("branch", "clean", "--offline", "--json")
            self.assertEqual(rc, 0, err)
            self.assertEqual(json.loads(out), {
                "delete": [{"branch": "fix/picked", "why": "merged by content: 1 commit has an equivalent in origin/main"}],
                "keep": [], "applied": False})
            self.assertEqual(s.gh.called("pr"), [])
            self.assertIn("fix/picked", git(s.repo, "branch", "--format=%(refname:short)").split())

    def test_clean_keeps_a_branch_whose_merged_pull_request_came_from_a_fork(self):
        # a fork's pull request can carry any branch name and head; only the repository's own count
        with Sandbox() as s:
            s.branch("fix/victim")
            tip = s.commit("fix: work only this branch has")
            git(s.repo, "switch", "-q", "main")
            s.gh.on(["pr", "list"], [{"number": 9, "state": "MERGED", "headRefName": "fix/victim", "headRefOid": tip,
                                      "isCrossRepository": True, "mergeCommit": {"oid": s.head("main")},
                                      "updatedAt": "2026-11-01T10:00:00Z"}])
            rc, out, err = dev("branch", "clean", "--apply")
            self.assertEqual(rc, 0, err)
            self.assertIn("fix/victim", git(s.repo, "branch", "--format=%(refname:short)").split())
            self.assertIn("fix/victim  PR #9 merged, but 1 commit not in origin/main", out)

    # gitx kept a `branch ls` of its own that the CLI no longer reached (it routes `dev branch ls` to the branch map),
    # whose `merged` took any merged pull request of the branch's name: a name reused after its first PR merged read
    # as merged. `dev branch ls` is the branch map from either entry, and its merge test needs the PR to hold the tip.
    def test_branch_ls_is_the_branch_map_and_a_reused_name_is_not_merged(self):
        with Sandbox() as s:
            s.branch("feat/reuse")
            s.commit("feat: start the second version")
            s.gh.on(["pr", "list"], [{"number": 3, "state": "MERGED", "headRefName": "feat/reuse", "headRefOid": "a" * 40,
                                      "isCrossRepository": False, "mergeCommit": {"oid": "b" * 40},
                                      "updatedAt": "2026-11-01T10:00:00Z", "title": "feat: the first version"}])
            rc, out, err = dev("branch", "ls", "--json")
            self.assertEqual(rc, 0, err)
            row = {r["name"]: r for r in json.loads(out)}["feat/reuse"]
            self.assertNotIn("merged", row)
            self.assertEqual((row["merged_into"], row["merged_pr"]), (None, None))

    def test_ls_table_is_plain_text(self):
        with Sandbox() as s:
            s.branch("feat/a")
            rc, out, _ = dev("branch", "ls", "--offline")
            self.assertEqual(rc, 0)
            self.assertIn("* feat/a", out)
            self.assertNotIn("\x1b[", out)


# --------------------------------------------------------------------------- dev commit


class CommitTests(unittest.TestCase):
    def test_validates_and_strips_attribution(self):
        with Sandbox() as s:
            s.branch("feat/parser")
            s.write("README.md", "# widget\nmore\n")
            rc, _, err = dev("commit", "-a", "-m", "Added things")
            self.assertEqual(rc, 2)
            self.assertIn("Conventional Commit", err)
            rc, out, err = dev("commit", "-a", "-m", "feat(core): add a parser",
                               "--body", "Why: the old one dropped quotes.\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
            self.assertEqual(rc, 0, err)
            body = git(s.repo, "log", "-1", "--format=%B")
            self.assertIn("Why: the old one dropped quotes.", body)
            self.assertNotIn("Co-Authored-By", body)
            self.assertIn("removed 1 AI-attribution", out)
            self.assertIn("feat(core): add a parser", out)

    def test_paths_commit_only_those_files(self):
        with Sandbox() as s:
            s.branch("fix/two")
            s.write("a.txt", "a")
            s.write("b.txt", "b")
            rc, _, err = dev("commit", "-m", "fix: keep a", "--cause", "the commit took b.txt along with a.txt",
                             "a.txt")
            self.assertEqual(rc, 0, err)
            self.assertEqual(git(s.repo, "show", "--name-only", "--format=", "HEAD"), "a.txt")
            self.assertIn("?? b.txt", git(s.repo, "status", "--porcelain"))

    def test_refused_on_default_or_agent_branch(self):
        with Sandbox() as s:
            s.write("a.txt", "a")
            rc, _, err = dev("commit", "-m", "fix: x", "--cause", "the tool wrote on the wrong branch", "a.txt")
            self.assertEqual(rc, 2)
            self.assertIn("dev branch new", err)
            s.branch("codex/tmp-1")
            rc, _, err = dev("commit", "-m", "fix: x", "--cause", "the tool wrote on the wrong branch", "a.txt")
            self.assertEqual(rc, 2)
            self.assertIn("dev branch rename", err)

    def test_nothing_staged(self):
        with Sandbox() as s:
            s.branch("fix/none")
            self.assertEqual(dev("commit", "-m", "fix: nothing", "--cause", "the empty commit was never checked")[0], 1)

    def test_wip_round_trip(self):
        with Sandbox() as s:
            s.branch("feat/wip")
            before = s.head()
            s.write("README.md", "changed\n")
            s.write("new.txt", "new\n")
            rc, out, err = dev("commit", "--wip", "-m", "half done")
            self.assertEqual(rc, 0, err)
            self.assertEqual(git(s.repo, "log", "-1", "--format=%s"), "wip: half done")
            self.assertEqual(git(s.repo, "status", "--porcelain"), "")
            rc, out, err = dev("commit", "--unwip")
            self.assertEqual(rc, 0, err)
            self.assertEqual(s.head(), before)
            st = git(s.repo, "status", "--porcelain")
            self.assertIn(" M README.md", st)
            self.assertIn("?? new.txt", st)
            self.assertEqual(dev("commit", "--unwip")[0], 2)

    def test_a_commit_that_changes_what_the_secret_scan_sees_says_so_and_commits(self):
        # the scan rules and ignore list, and a .gitattributes line that hides a diff from it, take effect for the
        # commits after this one: dev commit says so once, for the developer, and commits
        said = "this commit changes what the secret scan sees"
        with Sandbox() as s:
            s.branch("chore/scan")
            for n, (path, text) in enumerate(((".gitleaks.toml", "[extend]\nuseDefault = true\n"),
                                              (".gitleaksignore", "config.py:github-pat:1\n"),
                                              (".gitattributes", "*.env -diff\n"),
                                              ("fixtures/.gitattributes", "*.bin binary\n"),
                                              (".gitattributes", "*.env -diff\n*.dat diff=hexdump\n")), 1):
                with self.subTest(path=path, text=text):
                    s.write(path, text)
                    rc, out, err = dev("commit", "-m", f"chore: scan setting {n}", path)
                    self.assertEqual(rc, 0, err)
                    self.assertEqual(git(s.repo, "log", "-1", "--format=%s"), f"chore: scan setting {n}")
                    self.assertEqual((out + err).count(said), 1, out + err)
                    self.assertIn(path, (out + err).split(said, 1)[1].splitlines()[0])
            for n, (path, text) in enumerate((("src/app.py", "print('x')\n"),
                                              (".gitattributes", "*.env -diff\n*.dat diff=hexdump\n*.sh text eol=lf\n"),
                                              (".gitattributes", "*.sh text eol=lf\n")), 1):
                with self.subTest(path=path, text=text, said=False):  # no rule that hides a diff is added
                    s.write(path, text)
                    rc, out, err = dev("commit", "-m", f"chore: other change {n}", path)
                    self.assertEqual(rc, 0, err)
                    self.assertNotIn(said, out + err)

    @unittest.skipUnless(core.have("gitleaks"), "gitleaks not installed")
    def test_staged_secret_refused(self):
        with Sandbox() as s:
            s.branch("feat/creds")
            s.write("config.py", 'TOKEN = "ghp_' + "Z8qLm2Vx7Rt4Kp9Wn3Hs6Jd1Fb5Gc0Ya2Ue8" + '"\n')
            rc, _, err = dev("commit", "-m", "feat: add config", "config.py")
            self.assertEqual(rc, 2)
            self.assertIn("gitleaks", err)


# --------------------------------------------------------------------------- dev push and the local-CI evidence


class PushTests(unittest.TestCase):
    def test_refuses_the_default_branch(self):
        with Sandbox() as s:
            s.commit("fix: on main")
            rc, _, err = dev("push", "--ci-passed")
            self.assertEqual(rc, 2)
            self.assertIn("default branch", err)

    def test_needs_evidence_then_pushes_once_with_upstream(self):
        with Sandbox() as s:
            s.branch("feat/x")
            s.commit("feat: add x", CI_ON_PUSH)
            rc, _, err = dev("push")
            self.assertEqual(rc, 2)
            self.assertIn("no local CI evidence", err)
            self.assertIn("dev ci --run", err)
            rc, out, err = dev("push", "--ci-passed")
            self.assertEqual(rc, 0, err)
            self.assertEqual(s.head("feat/x", repo=s.origin), s.head())
            self.assertEqual(git(s.repo, "rev-parse", "--abbrev-ref", "@{u}"), "origin/feat/x")
            self.assertIn("pushed feat/x", out)
            self.assertIn("dev pr create", out)
            ev = gitx.evidence(s.ctx())
            self.assertEqual(ev["how"], "attested")
            rc, out, _ = dev("push")
            self.assertEqual(rc, 0)
            self.assertIn("nothing to push", out)

    def test_marker_from_a_dirty_tree_matches_the_commit_of_exactly_those_files(self):
        with Sandbox() as s:
            s.branch("feat/y")
            s.write("y.py", "print('y')\n")
            rc, out, err = dev("ci", "--mark-passed", "--note", "python3 -m unittest: 3 passed")
            self.assertEqual(rc, 0, err)
            self.assertIn("uncommitted", out)
            self.assertEqual(dev("commit", "-m", "feat: add y", "y.py")[0], 0)
            self.assertEqual(dev("ci", "--evidence")[0], 0)
            self.assertEqual(dev("push")[0], 0)
            s.commit("feat: add z", {"z.py": "z\n"})
            rc, _, err = dev("push")
            self.assertEqual(rc, 2)
            self.assertIn("z.py", err)

    def test_forgotten_file_invalidates_the_evidence(self):
        with Sandbox() as s:
            s.branch("feat/forgot")
            s.write("a.py", "a\n")
            s.write("b.py", "b\n")
            dev("ci", "--mark-passed")
            dev("commit", "-m", "feat: add a", "a.py")
            rc, _, err = dev("push")
            self.assertEqual(rc, 2)
            self.assertIn("b.py", err)

    def test_wip_commits_are_never_pushed(self):
        with Sandbox() as s:
            s.branch("feat/w")
            s.write("w.txt", "w")
            dev("commit", "--wip")
            rc, _, err = dev("push", "--ci-passed")
            self.assertEqual(rc, 2)
            self.assertIn("WIP", err)

    def test_diverged_needs_lease_and_never_drops_other_peoples_commits(self):
        with Sandbox() as s:
            s.branch("feat/z")
            s.commit("feat: add z")
            self.assertEqual(dev("push", "--ci-passed")[0], 0)
            s.write("z2.txt", "z2")
            git(s.repo, "add", "-A")
            self.assertEqual(dev("commit", "--amend", "-m", "feat: add z and z2")[0], 0)
            rc, _, err = dev("push", "--ci-passed")
            self.assertEqual(rc, 2)
            self.assertIn("diverged", err)
            rc, out, err = dev("push", "--force-with-lease", "--ci-passed")
            self.assertEqual(rc, 0, err)
            self.assertIn("force-with-lease", out)
            o = s.other()
            git(o, "fetch", "-q", "origin")
            git(o, "switch", "-q", "-c", "feat/z", "origin/feat/z")
            (o / "bob.txt").write_text("bob")
            git(o, "add", "-A")
            bob = dict(core.GIT_ENV, GIT_AUTHOR_NAME="Bob", GIT_AUTHOR_EMAIL="bob@example.com")
            git(o, "commit", "-q", "-m", "fix: bob's fix", env=bob)
            git(o, "push", "-q", "origin", "feat/z")
            self.assertEqual(dev("commit", "--amend", "-m", "feat: add z, z2")[0], 0)
            rc, _, err = dev("push", "--force-with-lease", "--ci-passed")
            self.assertEqual(rc, 2)
            self.assertIn("Bob", err)

    def test_first_push_behind_the_default_branch_is_refused(self):
        with Sandbox() as s:
            s.branch("feat/old")
            s.commit("feat: add old")
            s.advance_main()
            rc, _, err = dev("push", "--ci-passed")
            self.assertEqual(rc, 2)
            self.assertIn("behind origin/main", err)
            self.assertEqual(dev("push", "--ci-passed", "--allow-behind")[0], 0)

    def test_dev_push_counts_no_hourly_budget_of_pushes_that_start_ci(self):
        with Sandbox() as s:
            s.branch("feat/loop")
            s.commit("ci: run on push", CI_ON_PUSH)
            for i in range(3):
                s.commit(f"fix: attempt {i}")
                rc, _, err = dev("push", "--ci-passed")
                self.assertEqual(rc, 0, err)
            self.assertEqual(s.head("feat/loop", repo=s.origin), s.head())

    def test_dry_run_pushes_and_records_nothing(self):
        with Sandbox() as s:
            s.branch("feat/dry")
            s.commit("feat: add dry")
            rc, out, err = dev("push", "--dry-run", "--ci-passed")
            self.assertEqual(rc, 0, err)
            self.assertIn("dry run", out)
            self.assertEqual(git(s.origin, "branch", "--list", "feat/dry"), "")
            self.assertIsNone(gitx.evidence(s.ctx()))


# --------------------------------------------------------------------------- dev rebase


class RebaseTests(unittest.TestCase):
    def test_rebases_on_fresh_origin(self):
        with Sandbox() as s:
            s.branch("feat/r")
            s.commit("feat: add r")
            s.advance_main(2)
            rc, out, err = dev("rebase")
            self.assertEqual(rc, 0, err)
            self.assertIn("2 new upstream commit(s), 1 of yours replayed", out)
            self.assertEqual(git(s.repo, "merge-base", "HEAD", "origin/main"), s.head("origin/main"))

    def test_conflict_report_then_abort(self):
        with Sandbox() as s:
            s.branch("feat/c")
            s.commit("feat: change readme", {"README.md": "mine\n"})
            o = s.other()
            git(o, "pull", "-q", "origin", "main")
            s.commit("docs: change readme", {"README.md": "theirs\n"}, repo=o)
            git(o, "push", "-q", "origin", "main")
            rc, _, err = dev("rebase")
            self.assertEqual(rc, 1)
            self.assertIn("README.md", err)
            self.assertIn("dev rebase --continue", err)
            self.assertIn("feat: change readme", err)
            rc, out, err = dev("rebase", "--abort")
            self.assertEqual(rc, 0, err)
            self.assertIsNone(gitx._rebase_dir(str(s.repo)))

    def test_dirty_tree_refused_and_default_branch_fast_forwards(self):
        with Sandbox() as s:
            s.write("README.md", "dirty\n")
            rc, _, err = dev("rebase")
            self.assertEqual(rc, 2)
            self.assertIn("dev commit --wip", err)
            git(s.repo, "checkout", "-q", "--", "README.md")
            s.advance_main()
            rc, out, err = dev("rebase")
            self.assertEqual(rc, 0, err)
            self.assertIn("fast-forwarded", out)
            self.assertEqual(s.head(), s.head("origin/main"))


# --------------------------------------------------------------------------- dev ci --run


WORKFLOW = """name: CI
on:
  push:
    branches: [main]
  pull_request:
env:
  GREETING: hello
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python:
          - "3.9"
          - "3.12"
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python }}
      - name: Install
        run: pip install -r requirements.txt
      - name: Lint
        run: test "$GREETING" = hello && test "$CI" = true
      - name: Unit
        working-directory: sub
        run: |
          test -f marker.txt
          echo "unit ok in ${{ github.workspace }}"
      - name: Coverage
        if: always()
        run: codecov --token x
      - name: Export
        run: echo "X=1" >> "$GITHUB_ENV"
%s
  deploy:
    needs: test
    runs-on: ubuntu-latest
    if: github.ref == 'refs/heads/main'
    steps:
      - run: npm publish
"""


class LocalCiTests(unittest.TestCase):
    def setup_ci(self, s, extra=""):
        s.write(".github/workflows/ci.yml", WORKFLOW % extra)
        s.write("sub/marker.txt", "x")
        s.branch("feat/ci")
        s.commit("ci: add the workflow")

    def test_plan_classifies_steps(self):
        with Sandbox() as s:
            self.setup_ci(s)
            jobs = {j["job"]: j for j in gitx.plan(str(s.repo))}
            self.assertEqual(jobs["deploy"]["skip"], "deploy/publish job")
            steps = {st["name"]: st for st in jobs["test"]["steps"]}
            self.assertEqual(steps["Install"]["skip"], "dependency install (your local environment is used)")
            self.assertIsNone(steps["Lint"]["skip"])
            self.assertEqual(steps["Unit"]["cwd"], str(s.repo / "sub"))
            self.assertIn(str(s.repo), steps["Unit"]["run"])
            self.assertIn("side effect", steps["Coverage"]["skip"])
            self.assertIn("plumbing", steps["Export"]["skip"])
            self.assertFalse(any(st["blocking"] for st in jobs["test"]["steps"]))
            rc, out, _ = dev("ci", "--run", "--dry-run")
            self.assertIn("$ test › Lint", out)

    def test_run_records_evidence_that_push_accepts_without_attestation(self):
        with Sandbox() as s:
            self.setup_ci(s)
            rc, out, err = dev("ci", "--run")
            self.assertEqual(rc, 0, out + err)
            self.assertIn("local CI passed: 2 step(s)", out)
            ev = gitx.evidence(s.ctx())
            self.assertEqual(ev["how"], "run")
            self.assertEqual([x["step"] for x in ev["steps"]], ["Lint", "Unit"])
            self.assertEqual(dev("push")[0], 0)

    def test_a_check_that_cannot_run_here_blocks_the_evidence(self):
        with Sandbox() as s:
            self.setup_ci(s, '      - name: Matrix\n        run: echo "${{ matrix.python }}"')
            rc, out, err = dev("ci", "--run")
            self.assertEqual(rc, 1)
            self.assertIn("could not run here", err)
            self.assertIsNone(gitx.evidence(s.ctx()))

    def test_failing_step_shows_the_tail_and_records_nothing(self):
        with Sandbox() as s:
            self.setup_ci(s, '      - name: Boom\n        run: |\n          echo "AssertionError: 1 != 2"\n          exit 3')
            rc, out, err = dev("ci", "--run")
            self.assertEqual(rc, 1)
            self.assertIn("AssertionError: 1 != 2", out)
            self.assertIn("exit 3", out)
            self.assertIsNone(gitx.evidence(s.ctx()))

    # A hosted runner's housekeeping frees disk on a throwaway machine; on this Mac it deletes the developer's files.
    # Steps that delete a path not inside the checkout (literal, ~, a variable that is unset here, a command's output)
    # or prune Docker are skipped, without sudo too; a deletion inside the checkout still runs.
    def test_runner_prep_that_deletes_outside_the_checkout_or_prunes_docker_is_skipped(self):
        with Sandbox() as s:
            prep = {"Free disk": "rm -rf /opt/hostedtoolcache/CodeQL",
                    "Tool cache": 'if [ -d "$AGENT_TOOLSDIRECTORY_UNSET" ]; then\n  rm -rf "$AGENT_TOOLSDIRECTORY_UNSET/"\n'
                                  'fi',
                    "Pip cache": "env FOO=1 nice -n 5 rm -rf ~/.cache/pip",
                    "Go cache": "rm -rf $(go env GOCACHE)",
                    "Elsewhere": "cd /opt && rm -rf ghc",
                    "Up a level": "rm -f ../sibling.txt",
                    "Docker": "docker system prune -af",
                    "Images": "docker image prune --all --force"}
            kept = {"Clean build": "rm -rf build dist *.egg-info", "Workspace": "rm -rf ${{ github.workspace }}/out",
                    "Unit": "test -f sub/marker.txt"}
            steps = "".join(f"      - name: {n}\n        run: |\n" + "".join(f"          {ln}\n" for ln in r.splitlines())
                            for n, r in {**prep, **kept}.items())
            s.write(".github/workflows/ci.yml", "name: CI\non: [pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
                                                "    steps:\n" + steps)
            s.write("sub/marker.txt", "x")
            s.branch("feat/ci")
            s.commit("ci: add the workflow")
            got = {st["name"]: (st["skip"], st["blocking"]) for j in gitx.plan(str(s.repo)) for st in j["steps"]}
            for name in prep:
                with self.subTest(step=name):
                    self.assertRegex(got[name][0] or "", r"^runner prep \(")
                    self.assertFalse(got[name][1])
            self.assertIn("/opt/hostedtoolcache/CodeQL", got["Free disk"][0])
            self.assertIn("docker system prune", got["Docker"][0])
            self.assertEqual({n: got[n] for n in kept}, {n: (None, False) for n in kept})

    # A hosted runner installs its OS packages and browsers; on this Mac the same steps install into the developer's
    # Homebrew, rewrite the shared Playwright browser cache or change the global git config. They are skipped like the
    # housekeeping above, without blocking the evidence; the tests that use the browsers still run.
    def test_os_package_browser_and_global_config_setup_is_runner_prep(self):
        with Sandbox() as s:
            prep = {"Browsers with OS deps": "npx playwright install --with-deps chromium",
                    "Browsers": "npx playwright install chromium",
                    "Python browsers": "uv run python -m playwright install --with-deps",
                    "Browser OS deps": "pnpm exec playwright install-deps",
                    "Apt": "apt-get update",
                    "Brew": "brew install librsvg",
                    "Root": "sudo systemctl start postgresql",
                    "Nested": "xvfb-run bash -c 'npx playwright install chromium && echo ready'",
                    "Line endings": "git config --global core.autocrlf false"}
            kept = {"Browser tests": "npx playwright test --grep @xplat", "Versions": "npx playwright --version",
                    "Brew prefix": "brew --prefix openssl"}
            steps = "".join(f"      - name: {n}\n        run: {r}\n" for n, r in {**prep, **kept}.items())
            s.write(".github/workflows/ci.yml", "name: CI\non: [pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
                                                "    steps:\n" + steps)
            s.branch("feat/ci")
            s.commit("ci: add the workflow")
            got = {st["name"]: (st["skip"], st["blocking"]) for j in gitx.plan(str(s.repo)) for st in j["steps"]}
            for name in prep:
                with self.subTest(step=name):
                    self.assertRegex(got[name][0] or "", r"^runner prep \(")
                    self.assertFalse(got[name][1])
            self.assertIn("OS packages", got["Browsers with OS deps"][0])
            self.assertIn("shared Playwright browser cache", got["Browsers"][0])
            self.assertIn("apt-get update", got["Apt"][0])
            self.assertIn("sudo", got["Root"][0])
            self.assertIn("git config --global", got["Line endings"][0])
            self.assertEqual({n: got[n] for n in kept}, {n: (None, False) for n in kept})
            self.assertIsNone(gitx.runner_setup("git config --global --get core.autocrlf", s.repo))  # a read

    # A Linux runner's desktop setup (fonts into ~/.local/share/fonts, then fc-cache) ran as it is on this Mac: it
    # wrote into the developer's own home folder and failed on the missing fc-cache, so the project never got local
    # evidence. Linux-only system setup and writes into the home folder outside the checkout are runner prep, skipped
    # with a line that says so; writes inside the checkout or into a temporary folder still run. A check that also
    # writes into the home folder (its log piped to tee) is still a check: it runs, never skipped as setup.
    def test_linux_only_setup_and_writes_into_the_home_folder_are_runner_prep(self):
        with Sandbox() as s:
            prep = {"Fonts": "mkdir -p ~/.local/share/fonts && cp assets/fonts/*.ttf ~/.local/share/fonts/ && "
                             "fc-cache -f",
                    "Fonts copied": "mkdir -p ~/.local/share/fonts && cp assets/fonts/*.ttf ~/.local/share/fonts/",
                    "Font cache": "fc-cache -fv",
                    "Locales": "locale-gen en_US.UTF-8",
                    "Home config": 'install -D -m 600 ci/settings.xml "$HOME/.m2/settings.xml"',
                    "Home link": "ln -sf $PWD/bin/tool ~/.local/bin/tool",
                    "Touch": "touch ~/.devkit-ci-marker"}
            logged = {"Pytest log": "python -m pytest 2>&1 | tee ~/pytest.log",
                      "Npm results": "npm test | tee \"$HOME/results.txt\"",
                      "Log beside": "pytest | tee ../log.txt"}  # HOME is the checkout's parent below
            kept = {"Reports": "mkdir -p reports && touch reports/.keep",
                    "Temp": "mkdir -p /tmp/devkit-ci-scratch",
                    "Copy in": "cp ~/.cache/x.txt build/",
                    "Fonts listed": "fc-list", **logged}
            steps = "".join(f"      - name: {n}\n        run: {r}\n" for n, r in {**prep, **kept}.items())
            s.write(".github/workflows/ci.yml", "name: CI\non: [pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
                                                "    steps:\n" + steps)
            s.branch("feat/ci")
            s.commit("ci: add the workflow")
            with mock.patch.dict(os.environ, {"HOME": str(s.root)}):
                got = {st["name"]: (st["skip"], st["blocking"]) for j in gitx.plan(str(s.repo)) for st in j["steps"]}
                self.assertEqual({n: gitx.local_step(r, s.repo) for n, r in logged.items()}, logged)
            for name in prep:
                with self.subTest(step=name):
                    self.assertRegex(got[name][0] or "", r"^runner prep \(")
                    self.assertFalse(got[name][1])
            self.assertIn("fc-cache", got["Font cache"][0])
            self.assertIn("~/.local/share/fonts", got["Fonts"][0])
            self.assertIn("home folder", got["Home config"][0])
            self.assertEqual({n: got[n] for n in kept}, {n: (None, False) for n in kept})
            s.write(".github/workflows/ci.yml", "name: CI\non: [pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
                                                f"    steps:\n      - name: Fonts\n        run: {prep['Fonts']}\n"
                                                "      - name: Unit\n        run: test -f assets/fonts/a.ttf\n")
            s.write("assets/fonts/a.ttf", "font\n")
            s.commit("ci: install the fonts")
            fonts = Path(os.path.expanduser("~/.local/share/fonts"))
            rc, out, err = dev("ci", "--run")
            self.assertEqual(rc, 0, out + err)
            self.assertRegex(out, r"(?m)^  – test › Fonts  .*runner prep \(writes ~/\.local/share/fonts")
            self.assertFalse(fonts.exists())

    # A step that names a Docker context runs only when this Mac's Docker has a context of that name. The built-in
    # `default` is a hosted runner's own engine: here it is whatever DOCKER_HOST points to, which the workflow never
    # chose. Such a step is a real check that could not run here, so it blocks the evidence.
    def test_a_step_that_names_a_docker_context_this_mac_lacks_blocks_the_evidence(self):
        with Sandbox() as s, tempfile.TemporaryDirectory() as cfg:
            meta = Path(cfg) / "contexts" / "meta" / "0f3a"
            meta.mkdir(parents=True)
            (meta / "meta.json").write_text(json.dumps({"Name": "colima", "Endpoints": {"docker": {
                "Host": "unix:///Users/ana/.colima/default/docker.sock"}}}))
            (Path(cfg) / "contexts" / "meta" / "broken").mkdir()
            (Path(cfg) / "contexts" / "meta" / "broken" / "meta.json").write_text("{not json")
            elsewhere = {"Acceptance": "dbus-run-session -- bash -c 'uv run python scripts/acceptance.py --profile pr "
                                       "--docker-context default --block-egress'",
                         "Compose": "DOCKER_CONTEXT=ci-runner docker compose up -d",
                         "Exported": "export DOCKER_CONTEXT=ci-runner",
                         "Use": "docker context use ci-runner",
                         "Flag": "docker --context=ci-runner ps",
                         "Short flag": "docker -c ci-runner ps",
                         "Equals": "python scripts/check.py --docker-context=default"}
            here = {"Mine": "python scripts/check.py --docker-context colima", "Mine too": "docker --context colima ps",
                    "Current": "docker compose up -d", "Unset": 'python scripts/check.py --docker-context "$NO_CTX_SET"'}
            steps = "".join(f"      - name: {n}\n        run: {r}\n" for n, r in {**elsewhere, **here}.items())
            s.write(".github/workflows/ci.yml", "name: CI\non: [pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
                                                "    steps:\n" + steps)
            s.branch("feat/ci")
            s.commit("ci: add the workflow")
            with mock.patch.dict(os.environ, {"DOCKER_CONFIG": cfg}):
                os.environ.pop("NO_CTX_SET", None)
                got = {st["name"]: (st["skip"], st["blocking"]) for j in gitx.plan(str(s.repo)) for st in j["steps"]}
            for name in elsewhere:
                with self.subTest(step=name):
                    self.assertRegex(got[name][0] or "", r"^Docker context \S+ ")
                    self.assertTrue(got[name][1])
            self.assertEqual(got["Acceptance"][0],
                             "Docker context default is a hosted runner's own engine (contexts here: colima)")
            self.assertEqual(got["Compose"][0], "Docker context ci-runner is not set up here (contexts here: colima)")
            self.assertEqual({n: got[n] for n in here}, {n: (None, False) for n in here})

    def test_no_workflows(self):
        with Sandbox() as s:
            s.branch("feat/none")
            rc, _, err = dev("ci", "--run")
            self.assertEqual(rc, 1)
            self.assertIn("--mark-passed", err)
            self.assertEqual(dev("ci", "--evidence")[0], 2)


# A plan job writes the work to do to $GITHUB_OUTPUT, a matrix job runs one leg per planned part, and a gate job checks
# every result. `dev ci --run` skipped the plan's output step as plumbing, judged `if: needs.…` unknown, and ran the
# gate with its needs.* variables empty, so it failed in 0 s.
PLANNED = """name: CI
on: [pull_request]
jobs:
  plan:
    runs-on: ubuntu-latest
    outputs:
      jobs: ${{ steps.parts.outputs.jobs }}
      plan: ${{ steps.parts.outputs.plan }}
      skip: ${{ steps.tested.outputs.skip }}
    steps:
      - uses: actions/checkout@v4
      - name: Already tested on its pull request?
        id: tested
        if: github.event_name == 'push'
        run: echo "skip=true" >> "$GITHUB_OUTPUT"
      - name: Parts
        id: parts
        run: |
          echo "jobs=unit docs" >> "$GITHUB_OUTPUT"
          {
            echo "plan<<EOF"
            echo '{"include": [{"name": "part one", "shard": "1/2"}, {"name": "part two", "shard": "2/2"}]}'
            echo "EOF"
          } >> "$GITHUB_OUTPUT"
      - name: Say the plan
        env:
          PARTS: ${{ steps.parts.outputs.jobs }}
        run: echo "planned $PARTS" >> "$SHARDS_LOG"
  run:
    needs: plan
    if: needs.plan.outputs.jobs != '' && needs.plan.outputs.skip != 'true'
    name: ${{ matrix.name }}
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix: ${{ fromJSON(needs.plan.outputs.plan) }}
    steps:
      - name: Check
        env:
          JOBS: ${{ needs.plan.outputs.jobs }}
        run: echo "${{ matrix.shard }} $JOBS" >> "$SHARDS_LOG"
  check:
    needs: [plan, run]
    if: always()
    runs-on: ubuntu-latest
    steps:
      - name: Every job passed
        env:
          PLAN: ${{ needs.plan.result }}
          RUN: ${{ needs.run.result }}
          SKIP: ${{ needs.plan.outputs.skip }}
        run: |
          echo "plan: $PLAN, jobs: $RUN, skip: ${SKIP:-false}" >> "$SHARDS_LOG"
          [ "$PLAN" = "success" ] && { [ "$RUN" = "success" ] || { [ "$SKIP" = "true" ] && [ "$RUN" = "skipped" ]; }; }
"""


class JobOutputsTests(unittest.TestCase):
    def run_ci(self, s, workflow):
        s.write(".github/workflows/ci.yml", workflow)
        s.branch("feat/planned")
        s.commit("ci: plan the jobs")
        log = s.root / "shards.log"
        with mock.patch.dict(os.environ, {"SHARDS_LOG": str(log)}):
            rc, out, err = dev("ci", "--run")
        return rc, out, err, log.read_text().splitlines() if log.exists() else []

    def test_a_plan_jobs_outputs_drive_the_if_the_matrix_and_the_gate_of_the_jobs_after_it(self):
        with Sandbox() as s:
            rc, out, err, log = self.run_ci(s, PLANNED)
            self.assertEqual(rc, 0, out + err)
            self.assertEqual(log, ["planned unit docs", "1/2 unit docs", "2/2 unit docs",
                                   "plan: success, jobs: success, skip: false"])
            steps = [(x["job"], x["step"]) for x in gitx.evidence(s.ctx())["steps"]]
            self.assertEqual(steps, [("plan", "Parts"), ("plan", "Say the plan"), ("part one", "Check"),
                                     ("part two", "Check"), ("check", "Every job passed")])

    def test_a_job_whose_if_on_a_plan_output_is_false_is_skipped_and_the_gate_sees_it_skipped(self):
        with Sandbox() as s:
            skipping = PLANNED.replace("        if: github.event_name == 'push'\n", "")  # already tested: skip=true
            rc, out, err, log = self.run_ci(s, skipping)
            self.assertEqual(rc, 0, out + err)
            self.assertEqual(log, ["planned unit docs", "plan: success, jobs: skipped, skip: true"])
            self.assertIn("run", out)
            self.assertIn("is false here", out)

    def test_a_needs_value_that_cannot_be_known_here_blocks_the_evidence_and_says_which(self):
        # the matrix job runs on Windows, so its result is not known on this Mac: the gate that reads it is not run
        with Sandbox() as s:
            windows = PLANNED.replace("    name: ${{ matrix.name }}\n    runs-on: ubuntu-latest",
                                      "    name: ${{ matrix.name }}\n    runs-on: windows-latest")
            rc, out, err, log = self.run_ci(s, windows)
            self.assertEqual(rc, 1, out + err)
            self.assertIn("could not run here", err)
            self.assertNotIn("local CI failed", err)
            self.assertEqual(log, ["planned unit docs"])  # the gate did not run with an empty RUN
            self.assertRegex(out, r"check › Every job passed .*needs\.run\.result")
            self.assertIsNone(gitx.evidence(s.ctx()))

    def test_a_resumed_run_keeps_the_plan_step_with_the_outputs_it_recorded(self):
        with Sandbox() as s:
            self.assertEqual(self.run_ci(s, PLANNED)[0], 0)
            rc, out, err = dev("ci", "--run", "--resume")
            self.assertEqual(rc, 0, out + err)
            self.assertIn("plan › Parts  kept: passed on", out)
            self.assertIn("part two › Check  kept: passed on", out)  # the legs come from the recorded plan
            self.assertEqual(gitx.evidence(s.ctx())["how"], "resumed")

    def test_a_matrix_from_an_expression_follows_githubs_include_and_exclude(self):
        legs = gitx._matrix_legs({"os": ["mac", "linux"], "py": [9, 13], "exclude": [{"os": "linux", "py": 13}],
                                  "include": [{"os": "mac", "arch": "arm"}, {"os": "bsd"}, {"py": 9, "os": "x"}]})
        self.assertEqual(legs, [{"os": "mac", "py": 9, "arch": "arm"}, {"os": "mac", "py": 13, "arch": "arm"},
                                {"os": "linux", "py": 9}, {"os": "bsd"}, {"py": 9, "os": "x"}])


def _workflow(steps: dict) -> str:
    """A pull-request workflow with one job whose steps are {name: script}, each script a `run: |` block."""
    body = "".join(f"      - name: {n}\n        run: |\n" + "".join(f"          {ln}\n" for ln in r.splitlines())
                   for n, r in steps.items())
    return "name: CI\non: [pull_request]\njobs:\n  security:\n    runs-on: ubuntu-latest\n    steps:\n" + body


class RunnerSetupInsideACheckTests(unittest.TestCase):
    """A step that installs its scanner and runs it: the scan is a check, only the install is the runner's setup."""

    def plan_steps(self, s, steps: dict) -> dict:
        s.write(".github/workflows/ci.yml", _workflow(steps))
        s.branch("feat/ci")
        s.commit("ci: add the workflow")
        return {st["name"]: st for j in gitx.plan(str(s.repo)) for st in j["steps"]}

    # The scan still runs on this Mac with the setup commands left out: the tools installed here are used.
    def test_a_step_that_installs_its_scanner_keeps_the_scan_without_the_setup(self):
        with Sandbox() as s:
            got = self.plan_steps(s, {
                "Gitleaks": "brew install gitleaks && gitleaks detect --no-git --exit-code 1",
                "Trivy": "sudo apt-get install -y trivy\ntrivy fs --exit-code 1 --severity HIGH,CRITICAL .",
                "Shellcheck": "apt-get update && apt-get install -y shellcheck && shellcheck scripts/*.sh",
                "Browser tests": "npx playwright install --with-deps chromium && npx playwright test",
                "Semgrep": 'git config --global --add safe.directory "$PWD" && semgrep scan --error --config p/default',
                "Fallback": "command -v osv-scanner || brew install osv-scanner  # the runner lacks it\n"
                            "osv-scanner --lockfile=package-lock.json",
                "Installer": "curl -sSfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh"
                             " | sudo sh -s -- -b /usr/local/bin\ntrivy fs .",
                "Pip scanner": "pip install semgrep && semgrep scan --error",
                "Install only": "brew install gitleaks && gitleaks version"})
            kept = {n: (st["skip"], st["blocking"], st["run"], st["dropped"]) for n, st in got.items()}
            self.assertEqual(kept["Gitleaks"], (None, False, "gitleaks detect --no-git --exit-code 1",
                                                ["brew install gitleaks"]))
            self.assertEqual(kept["Trivy"], (None, False, "trivy fs --exit-code 1 --severity HIGH,CRITICAL .",
                                             ["sudo apt-get install -y trivy"]))
            self.assertEqual(kept["Shellcheck"], (None, False, "shellcheck scripts/*.sh",
                                                  ["apt-get update", "apt-get install -y shellcheck"]))
            self.assertEqual(kept["Browser tests"][:3], (None, False, "npx playwright test"))
            self.assertEqual(kept["Semgrep"][:3], (None, False, "semgrep scan --error --config p/default"))
            # a fallback install becomes `true`, so the scan after it still runs and fails when the tool is missing
            self.assertEqual(kept["Fallback"][:3], (None, False, "command -v osv-scanner || true\n"
                                                                 "osv-scanner --lockfile=package-lock.json"))
            self.assertEqual(kept["Installer"][:3], (None, False, "trivy fs ."))
            self.assertEqual(kept["Pip scanner"][:3], (None, False, "semgrep scan --error"))
            # a step that only installs (and prints the version) is still the runner's setup
            self.assertRegex(kept["Install only"][0] or "", r"^runner prep \(brew install ")
            self.assertFalse(kept["Install only"][1])

    # When devkit cannot take the setup out without taking the scan too, the scan is a check that could not run
    # here: it blocks the evidence instead of being skipped as setup.
    def test_a_scan_tangled_with_its_setup_or_run_as_root_blocks_the_evidence(self):
        with Sandbox() as s:
            got = self.plan_steps(s, {
                "Nested": "bash -c 'brew install gitleaks && gitleaks detect --no-git'",
                "Subshell": "(sudo apt-get install -y trivy && trivy fs .)",
                "Heredoc": "brew install gitleaks\ncat > .gitleaks.toml <<'EOF'\ntitle = \"widget\"\nEOF\n"
                           "gitleaks detect --config .gitleaks.toml",
                "Root scan": 'sudo -E env PATH="$PATH" ./scripts/security-scan.sh'})
            for name, st in got.items():
                with self.subTest(step=name):
                    self.assertTrue(st["blocking"], st["skip"])
                    self.assertNotRegex(st["skip"] or "", r"^runner prep")
            self.assertIn("as root", got["Root scan"]["skip"])
            rc, out, err = dev("ci", "--run")
            self.assertEqual(rc, 1)
            self.assertIn("4 check(s) could not run here", err)
            self.assertIsNone(gitx.evidence(s.ctx()))

    # `sudo test …` is a check run as root, not the runner's setup: dev ci --run skipped `sudo test -z "$(gofmt -l .)"`
    # as runner prep, so the format check never ran and nothing said so. A here-string (<<<) was read as a heredoc,
    # so a check fed by one was tangled with the install before it.
    def test_a_test_run_as_root_blocks_and_a_here_string_keeps_its_check(self):
        with Sandbox() as s:
            got = self.plan_steps(s, {"Format": 'sudo test -z "$(gofmt -l .)"', "File": "sudo test -f build/app",
                                      "Bracket": "sudo [ -d /opt/app ]", "Root echo": 'sudo echo "$(cat VERSION)"',
                                      "Here-string": 'brew install jq && jq . <<< "$X"',
                                      "Root setup": "sudo mkdir -p /opt/app"})
        for name in ("Format", "File", "Bracket", "Root echo"):
            with self.subTest(step=name):
                self.assertTrue(got[name]["blocking"], got[name]["skip"])
                self.assertRegex(got[name]["skip"] or "", r"^runs .* as root \(sudo\)")
        here = got["Here-string"]
        self.assertEqual((here["skip"], here["blocking"], here["run"], here["dropped"]),
                         (None, False, 'jq . <<< "$X"', ["brew install jq"]))
        self.assertRegex(got["Root setup"]["skip"] or "", r"^runner prep \(sudo mkdir -p runs as root")

    # A check piped into a home-folder file runs as written, since its log is all the write is. Two pipelines must not:
    # `sudo tee` cannot run here, so skipping the step would record evidence for tests that never ran; and a write
    # into a dotfile (`~/.bashrc`, `~/.npmrc`) would change the developer's own settings. Both block the evidence.
    # The same writes with no check beside them are still the runner's setup.
    def test_a_check_piped_to_sudo_tee_or_into_a_home_dotfile_blocks_the_evidence(self):
        with Sandbox() as s, mock.patch.dict(os.environ, {"HOME": str(s.root)}):
            got = self.plan_steps(s, {"Root log": "python -m pytest 2>&1 | sudo tee ~/pytest.log",
                                      "Root log beside": "pytest | sudo tee -a /tmp/x.log ~/ci/pytest.log",
                                      "Bashrc": "pytest | tee ~/.bashrc",
                                      "Npmrc": 'npm test | tee -a "$HOME/.npmrc"',
                                      "Dot folder": "pytest | tee ~/.config/tool/log.txt",
                                      "Dotfile second": "pytest | tee ~/log ~/.bashrc",
                                      "Root setup": "echo token | sudo tee ~/.npmrc",
                                      "Dot setup": "echo registry=https://example.test | tee ~/.npmrc",
                                      "Log": "pytest | tee ~/pytest.log"})
        for name in ("Root log", "Root log beside", "Bashrc", "Npmrc", "Dot folder", "Dotfile second"):
            with self.subTest(step=name):
                self.assertTrue(got[name]["blocking"], got[name]["skip"])
                self.assertIsNotNone(got[name]["skip"])
        for name in ("Root setup", "Dot setup"):
            with self.subTest(step=name):
                self.assertRegex(got[name]["skip"] or "", r"^runner prep \(")
                self.assertFalse(got[name]["blocking"])
        self.assertEqual((got["Log"]["skip"], got["Log"]["blocking"]), (None, False))

    # A shell redirect writes its target as tee does: `pytest > ~/.bashrc` would overwrite the developer's settings
    # with the test log, so next to a check a redirect into a dotfile or dot folder of the home folder blocks the
    # evidence; with no check beside it, it is the runner's setup (`echo … > ~/.npmrc`). A log in the home folder, in
    # the checkout, or /dev/null still runs.
    def test_a_check_redirected_into_a_home_dotfile_blocks_the_evidence(self):
        with Sandbox() as s, mock.patch.dict(os.environ, {"HOME": str(s.root)}):
            got = self.plan_steps(s, {"Truncate": "pytest > ~/.bashrc",
                                      "Append": "pytest >> ~/.bashrc",
                                      "Errors": "pytest 2> ~/.bashrc",
                                      "Both": 'npm test &> "$HOME/.npmrc"',
                                      "Dot folder": "pytest > ~/.config/tool/log.txt 2>&1",
                                      "Dot setup": "echo registry=https://example.test > ~/.npmrc",
                                      "Home log": "pytest > ~/pytest.log 2>&1",
                                      "Checkout log": "pytest >> pytest.log",
                                      "Discarded": "pytest 2>/dev/null"})
        for name in ("Truncate", "Append", "Errors", "Both", "Dot folder"):
            with self.subTest(step=name):
                self.assertTrue(got[name]["blocking"], got[name]["skip"])
                self.assertRegex(got[name]["skip"] or "", r"into a dotfile of the home folder next to a check")
        self.assertRegex(got["Dot setup"]["skip"] or "", r"^runner prep \(writes ~/\.npmrc into the home folder")
        self.assertFalse(got["Dot setup"]["blocking"])
        for name in ("Home log", "Checkout log", "Discarded"):
            with self.subTest(step=name):
                self.assertEqual((got[name]["skip"], got[name]["blocking"]), (None, False))

    # `sudo tee` outside the home folder cannot run here either: skipping `pytest | sudo tee /tmp/x.log` as setup
    # recorded evidence for tests that never ran, so a check beside it blocks the evidence. Writing a system folder as
    # root (`curl … | sudo tee /etc/apt/…`) is the runner's setup, and so is a root write with no check beside it.
    def test_a_check_piped_to_sudo_tee_outside_a_system_folder_blocks_the_evidence(self):
        with Sandbox() as s:
            got = self.plan_steps(s, {"Temp log": "pytest | sudo tee /tmp/x.log",
                                      "Appended": "python -m pytest 2>&1 | sudo tee -a /var/tmp/pytest.log",
                                      "Checkout log": "pytest | sudo tee pytest.log",
                                      "No file": "pytest | sudo tee",
                                      "Apt key": "curl -fsSL https://example.test/key.asc | sudo tee "
                                                 "/etc/apt/keyrings/example.asc",
                                      "Tool": "curl -fsSL https://example.test/tool | sudo tee /usr/local/bin/tool",
                                      "Root write alone": "echo 1 | sudo tee /tmp/flag"})
        for name in ("Temp log", "Appended", "Checkout log", "No file"):
            with self.subTest(step=name):
                self.assertTrue(got[name]["blocking"], got[name]["skip"])
                self.assertRegex(got[name]["skip"] or "", r"^runs tee as root \(sudo\)")
        for name in ("Apt key", "Tool", "Root write alone"):
            with self.subTest(step=name):
                self.assertRegex(got[name]["skip"] or "", r"^runner prep \(sudo tee ")
                self.assertFalse(got[name]["blocking"])

    # A check's output written as root cannot run here whatever the folder: `pytest | sudo tee /var/log/pytest.log`
    # and `pytest | sudo dd of=/tmp/x.log` were skipped as the runner's setup, which recorded evidence for tests that
    # never ran, and `pytest >| ~/.bashrc` ran as written. A download into a system folder stays the runner's setup.
    def test_a_check_written_as_root_into_any_folder_or_clobbering_a_dotfile_blocks_the_evidence(self):
        with Sandbox() as s, mock.patch.dict(os.environ, {"HOME": str(s.root)}):
            got = self.plan_steps(s, {"Var log": "pytest 2>&1 | sudo tee /var/log/pytest.log",
                                      "Opt": "npm test | sudo tee /opt/results.txt",
                                      "Usr local": "pytest | sudo tee /usr/local/x.log",
                                      "Dd": "pytest | sudo dd of=/tmp/x.log",
                                      "Dd system": "pytest | sudo dd of=/var/log/pytest.log status=none",
                                      "Clobber": "pytest >| ~/.bashrc",
                                      "Docker key": "curl -fsSL https://example.test/gpg | gpg --dearmor | sudo tee "
                                                    "/etc/apt/keyrings/docker.gpg",
                                      "Image": "wget -qO- https://example.test/disk.img | sudo dd of=/opt/disk.img"})
        for name in ("Var log", "Opt", "Usr local", "Dd", "Dd system"):
            with self.subTest(step=name):
                self.assertTrue(got[name]["blocking"], got[name]["skip"])
                self.assertRegex(got[name]["skip"] or "", r"^runs (tee|dd) as root \(sudo\)")
        self.assertTrue(got["Clobber"]["blocking"], got["Clobber"]["skip"])
        self.assertRegex(got["Clobber"]["skip"] or "", r"into a dotfile of the home folder next to a check")
        for name in ("Docker key", "Image"):
            with self.subTest(step=name):
                self.assertRegex(got[name]["skip"] or "", r"^runner prep \(sudo (tee|dd) ")
                self.assertFalse(got[name]["blocking"])

    def fake_tools(self, d: Path) -> Path:
        """A bin folder with a `brew` that leaves a marker when it runs and a `fakescan` that records its arguments
        and exits with the code in fakescan.rc."""
        (d / "bin").mkdir()
        (d / "bin" / "brew").write_text(f"#!/bin/sh\ntouch '{d / 'brew-ran'}'\n")
        (d / "bin" / "fakescan").write_text(f"#!/bin/sh\necho \"$@\" > '{d / 'fakescan.args'}'\n"
                                            f"exit \"$(cat '{d / 'fakescan.rc'}')\"\n")
        for f in (d / "bin").iterdir():
            f.chmod(0o755)
        return d / "bin"

    def test_the_scan_runs_without_its_setup_and_a_finding_fails_the_evidence(self):
        with Sandbox() as s, tempfile.TemporaryDirectory() as t:
            d = Path(t)
            path = f"{self.fake_tools(d)}{os.pathsep}{os.environ.get('PATH', '')}"
            self.plan_steps(s, {"Gitleaks": "brew install fakescan && fakescan detect --exit-code 1"})
            with mock.patch.dict(os.environ, {"PATH": path}):
                (d / "fakescan.rc").write_text("1")
                rc, out, err = dev("ci", "--run")
                self.assertEqual(rc, 1, out + err)
                self.assertIsNone(gitx.evidence(s.ctx()))
                (d / "fakescan.rc").write_text("0")
                rc, out, err = dev("ci", "--run")
            self.assertEqual(rc, 0, out + err)
            self.assertEqual((d / "fakescan.args").read_text().strip(), "detect --exit-code 1")
            self.assertFalse((d / "brew-ran").exists(), "the runner's setup ran on this Mac")
            self.assertIn("brew install fakescan", out)
            self.assertEqual([x["step"] for x in gitx.evidence(s.ctx())["steps"]], ["Gitleaks"])

    def test_a_scanner_missing_here_fails_the_evidence(self):
        with Sandbox() as s, tempfile.TemporaryDirectory() as t:
            d = Path(t)
            path = f"{self.fake_tools(d)}{os.pathsep}{os.environ.get('PATH', '')}"
            self.plan_steps(s, {"Gitleaks": "brew install devkit-absent-scanner && devkit-absent-scanner detect"})
            with mock.patch.dict(os.environ, {"PATH": path}):
                rc, out, err = dev("ci", "--run")
            self.assertEqual(rc, 1, out + err)
            self.assertIn("local CI failed", err)
            self.assertIn("exit 127", out)
            self.assertIn("brew install devkit-absent-scanner", out)
            self.assertFalse((d / "brew-ran").exists())
            self.assertIsNone(gitx.evidence(s.ctx()))


# A retry wrapper the way workflows keep it: it runs the command it is given, and before a second try it repairs the
# runner's package manager as root.
APT_RETRY = """#!/usr/bin/env bash
# Run an install that uses apt again when a mirror is slow: the runner's dpkg is repaired first.
set -u
for try in 1 2; do
  if [ "$try" -gt 1 ]; then
    sudo dpkg --configure -a || true
  fi
  timeout 900 "$@" && exit 0
  echo "apt-retry: try $try of '$*' did not finish"
  sleep 20
done
exit 1
"""


class RunnerSetupNeverRunsAsRootTests(unittest.TestCase):
    """`dev ci --run` on macOS never replays a hosted runner's root setup: fake sudo, timeout and sleep commands on
    the PATH write down every call, and none may happen."""

    def run_steps(self, s, steps: dict, gradlew=False):
        s.write(".github/workflows/ci.yml", _workflow(steps))
        s.write(".github/scripts/apt-retry.sh", APT_RETRY)
        if gradlew:  # installs only off CI, then passes its arguments on
            (s.write("gradlew", '#!/bin/sh\n[ -n "$CI" ] || npm ci\nexec echo "$@"\n')).chmod(0o755)
        s.write("sub/marker.txt", "x")
        s.branch("feat/ci")
        s.commit("ci: add the workflow")
        fake, called = s.root / "fakebin", s.root / "called.log"
        fake.mkdir()
        for tool, rc in (("sudo", 0), ("timeout", 1), ("sleep", 0)):
            (fake / tool).write_text(f'#!/bin/sh\necho "{tool} $*" >> "$CALLED_LOG"\nexit {rc}\n')
            (fake / tool).chmod(0o755)
        path = f"{fake}{os.pathsep}{os.environ.get('PATH', '')}"
        with mock.patch.dict(os.environ, {"PATH": path, "CALLED_LOG": str(called)}):
            rc, out, err = dev("ci", "--run")
        return rc, out, err, called.read_text().splitlines() if called.exists() else []

    # 3fa845cc7d63: dev ci --run ran `sudo rm -rf` and `sudo apt-get` from a runner's free-disk and package steps
    def test_inline_sudo_rm_and_sudo_apt_get_are_skipped_and_never_run(self):
        with Sandbox() as s:
            rc, out, err, called = self.run_steps(s, {
                "Free disk": "sudo rm -rf /usr/share/dotnet /opt/ghc \"$AGENT_TOOLSDIRECTORY\"",
                "Packages": "sudo apt-get update\nsudo apt-get install -y --no-install-recommends libvips-dev",
                "Packages from a mirror": "sudo apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 update && "
                                          "sudo apt-get -o Acquire::Retries=3 install -y libreoffice-writer",
                "Unit": "test -f sub/marker.txt"})
            self.assertEqual(rc, 0, out + err)
            self.assertEqual(called, [])
            self.assertRegex(out, r"security › Free disk .*runner prep \(")
            self.assertRegex(out, r"security › Packages .*runner prep \(")
            self.assertRegex(out, r"security › Packages from a mirror .*runner prep \(apt-get update")

    # A step that hands its install to a retry script: on a Mac without `timeout` the first try fails and the second
    # runs `sudo dpkg` before it. The install it hands over is the runner's setup, so the step is skipped whole.
    def test_an_install_handed_to_a_retry_script_is_skipped_and_its_sudo_never_runs(self):
        with Sandbox() as s:
            rc, out, err, called = self.run_steps(s, {
                "Chromium's system libraries": "bash .github/scripts/apt-retry.sh npx playwright install-deps chromium",
                "LibreOffice": "bash .github/scripts/apt-retry.sh sh -c 'sudo apt-get update && sudo apt-get install "
                               "-y libreoffice-writer'",
                "Unit": "test -f sub/marker.txt",
                "Build": "./gradlew build"}, gradlew=True)
            self.assertEqual(rc, 0, out + err)
            self.assertEqual(called, [])
            self.assertRegex(out, r"Chromium's system libraries .*runner prep \(playwright install-deps")
            self.assertRegex(out, r"LibreOffice .*runner prep \(apt-get update")
            # a script that only passes its arguments on (gradlew's exec "$JAVACMD" "$@") is not read: it runs
            self.assertRegex(out, r"(?m)^\s*✓ security › Build\s")

    def test_a_check_handed_to_a_retry_script_that_runs_sudo_blocks_the_evidence(self):
        with Sandbox() as s:
            rc, out, err, called = self.run_steps(s, {"Unit": "bash .github/scripts/apt-retry.sh grep -q x sub/marker.txt"})
            self.assertEqual(rc, 1, out + err)
            self.assertIn("could not run here", err)
            self.assertEqual(called, [])
            self.assertIsNone(gitx.evidence(s.ctx()))


class ChecksHiddenFromTheCommandReaderTests(unittest.TestCase):
    """A check that runs through `source`/`.` or inside a command or process substitution, after the step installed
    its tool: the shell command reader sees only a reporting command (`test`, `[`, `cat`), so the whole step was
    skipped as the runner's setup and the evidence recorded with no scan. The check now runs here without the setup."""

    def plan_steps(self, s, steps: dict) -> dict:
        s.write(".github/workflows/ci.yml", _workflow(steps))
        s.branch("feat/ci")
        s.commit("ci: add the workflow")
        return {st["name"]: (st["skip"], st["blocking"], st["run"], st["dropped"])
                for j in gitx.plan(str(s.repo)) for st in j["steps"]}

    def test_a_scan_script_run_with_source_or_dot_after_its_install_is_a_check(self):
        with Sandbox() as s:
            got = self.plan_steps(s, {"Source": "brew install gitleaks && source scripts/scan.sh",
                                      "Dot": "brew install gitleaks && . scripts/scan.sh"})
        self.assertEqual(got["Source"], (None, False, "source scripts/scan.sh", ["brew install gitleaks"]))
        self.assertEqual(got["Dot"], (None, False, ". scripts/scan.sh", ["brew install gitleaks"]))

    def test_a_gofmt_check_in_a_command_substitution_after_its_install_is_a_check(self):
        with Sandbox() as s:
            got = self.plan_steps(s, {"Gofmt": 'sudo apt-get install -y golang && test -z "$(gofmt -l .)"',
                                      "Backticks": 'sudo apt-get install -y golang && test -z "`gofmt -l .`"'})
        self.assertEqual(got["Gofmt"], (None, False, 'test -z "$(gofmt -l .)"', ["sudo apt-get install -y golang"]))
        self.assertEqual(got["Backticks"], (None, False, 'test -z "`gofmt -l .`"', ["sudo apt-get install -y golang"]))

    def test_a_scan_in_a_bracket_test_after_its_install_is_a_check(self):
        with Sandbox() as s:
            got = self.plan_steps(s, {"Trivy": 'brew install trivy && [ -z "$(trivy fs --quiet .)" ]'})
        self.assertEqual(got["Trivy"], (None, False, '[ -z "$(trivy fs --quiet .)" ]', ["brew install trivy"]))

    # The command reader drops `> >(…)` from the words altogether, so the substitution is found in the script's text.
    def test_a_scan_in_a_process_substitution_after_its_install_is_a_check(self):
        with Sandbox() as s:
            got = self.plan_steps(s, {"Input": "brew install gitleaks && cat <(gitleaks detect)",
                                      "Output": "brew install gitleaks && cat .gitleaks.toml > >(gitleaks detect --pipe)"})
        self.assertEqual(got["Input"], (None, False, "cat <(gitleaks detect)", ["brew install gitleaks"]))
        self.assertEqual(got["Output"], (None, False, "cat .gitleaks.toml > >(gitleaks detect --pipe)",
                                         ["brew install gitleaks"]))

    # `found=$(…)` exits with the scan's status, and bare backticks are split across the reader's words.
    def test_a_scan_kept_in_a_variable_or_bare_backticks_after_its_install_is_a_check(self):
        with Sandbox() as s:
            got = self.plan_steps(s, {"Assignment": 'brew install gitleaks && found="$(gitleaks detect)"',
                                      "Bare backticks": "brew install gitleaks && echo `gitleaks detect`"})
        self.assertEqual(got["Assignment"], (None, False, 'found="$(gitleaks detect)"', ["brew install gitleaks"]))
        self.assertEqual(got["Bare backticks"], (None, False, "echo `gitleaks detect`", ["brew install gitleaks"]))

    # Setup that runs inside a substitution cannot be left out without the command that uses its output, so next
    # to a check it blocks the evidence. A substitution that only feeds the setup keeps a setup-only step skipped.
    def test_setup_inside_a_substitution_next_to_a_check_blocks_the_evidence(self):
        with Sandbox() as s:
            tangled = {"Install in the check": 'test -z "$(brew install gofmt && gofmt -l .)"',
                       "Install in the input": "cat <(sudo apt-get install -y trivy && trivy fs .)"}
            setup = {"Kernel headers": 'sudo apt-get install -y "linux-headers-$(uname -r)"',
                     "Docker repository": 'echo "deb [arch=$(dpkg --print-architecture)] https://download.docker.com/'
                                          'linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/'
                                          'docker.list',
                     "Go cache": 'rm -rf "$(go env GOCACHE)"'}
            got = self.plan_steps(s, {**tangled, **setup})
        for name in tangled:
            with self.subTest(step=name):
                self.assertTrue(got[name][1], got[name][0])
                self.assertRegex(got[name][0] or "", r"^a check and runner setup \(")
        for name in setup:
            with self.subTest(step=name):
                self.assertRegex(got[name][0] or "", r"^runner prep \(")
                self.assertFalse(got[name][1])


# --------------------------------------------------------------------------- PR body template


class TemplateTests(unittest.TestCase):
    COMMITS = [{"sha": "a1", "subject": "feat(invoice): add csv export", "body": "Task: T-0abc123xyz9\nCloses #12"},
               {"sha": "b2", "subject": "test(invoice): cover empty invoices", "body": ""},
               {"sha": "c3", "subject": "wip: tmp", "body": ""}]
    EV = {"how": "marked", "note": "python3 -m unittest: 42 passed", "sha": "abcdef123456"}

    def test_selection_repo_template_then_devkit_language_then_builtin(self):
        with tempfile.TemporaryDirectory() as d:
            root, tdir = Path(d) / "repo", Path(d) / "templates"
            root.mkdir()
            tdir.mkdir()
            saved = gitx.TEMPLATE_DIR
            try:
                gitx.TEMPLATE_DIR = tdir
                text, src = gitx.pr_template(root)
                self.assertEqual(src, "built-in")
                (tdir / "pull_request.md").write_text("## What and why\n")
                (tdir / "pull_request.es.md").write_text("## Qué y por qué\n")
                self.assertTrue(gitx.pr_template(root, "en-US")[1].endswith("pull_request.md"))
                self.assertTrue(gitx.pr_template(root, "es-ES")[1].endswith("pull_request.es.md"))
                (root / ".github").mkdir()
                (root / ".github" / "pull_request_template.md").write_text("## Summary\n\n## Testing\n")
                text, src = gitx.pr_template(root, "es")
                self.assertEqual(src, ".github/pull_request_template.md")
            finally:
                gitx.TEMPLATE_DIR = saved

    def test_devkit_template_ships_the_five_sections(self):
        text, src = gitx.pr_template(tempfile.gettempdir())
        self.assertTrue(src.endswith("templates/github/pull_request.md"), src)
        kinds = [k for k, *_ in gitx.sections(text)]
        self.assertEqual(kinds, ["what", "changes", "verify", "risk", "links"])

    def test_prefill_changes_links_and_verify(self):
        text, _ = gitx.pr_template(tempfile.gettempdir())
        body = gitx.prefill_body(text, self.COMMITS, self.EV, "fix/12-login", "en-US")
        self.assertIn("- feat(invoice): add csv export\n- test(invoice): cover empty invoices", body)
        self.assertNotIn("wip: tmp", body)
        self.assertIn("- Task: T-0abc123xyz9", body)
        self.assertEqual(body.count("- Closes #12"), 1)
        self.assertIn("- Local CI: python3 -m unittest: 42 passed @ abcdef1", body)
        self.assertNotIn("`<command>`", body)
        self.assertIn("<!-- Two or three plain sentences", body)
        self.assertEqual(gitx.body_problems(body, text), ["What and why"])

    def test_prefill_in_spanish(self):
        text, _ = gitx.pr_template(tempfile.gettempdir())
        ev = {"how": "run", "sha": "abcdef1", "steps": [{"step": "Lint"}, {"step": "Unit"}]}
        body = gitx.prefill_body(text, self.COMMITS, ev, "feat/x", "es")
        self.assertIn("- CI local: `dev ci --run` → superado (2 pasos: Lint, Unit) @ abcdef1", body)

    def test_empty_sections_are_reported(self):
        text, _ = gitx.pr_template(tempfile.gettempdir())
        self.assertEqual(gitx.body_problems(text, text), ["What and why", "How to verify"])
        filled = text.replace("## Changes", "Exports invoices so finance can reconcile them.\n\n## Changes") \
                     .replace("- Manual check:", "- Manual check: opened the CSV in Numbers\n- Old:")
        self.assertEqual(gitx.body_problems(filled, text), [])
        self.assertEqual(gitx.body_problems("just text", text), ["What and why", "How to verify"])
        repo_t = "## Summary\n\n## Notes\n"
        self.assertEqual(gitx.body_problems("## Summary\nDone.\n", repo_t), [])

    def test_squash_message(self):
        body = ("## What and why\n\n<!-- c -->\nExports invoices as CSV so finance can reconcile.\n\n## Changes\n\n- x\n"
                "\n## Links\n\n- Closes #12\n- fixes #3\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n")
        subject, text = gitx.squash_message("feat(invoice): add csv export", body)
        self.assertEqual(subject, "feat(invoice): add csv export")
        self.assertEqual(text, "Exports invoices as CSV so finance can reconcile.\n\nCloses #12\nCloses #3")

    def test_title_from_first_commit_or_branch(self):
        self.assertEqual(gitx.derive_title("feat/x", self.COMMITS), "feat(invoice): add csv export")
        self.assertEqual(gitx.derive_title("feat/PROJ-7-invoice-export", [{"subject": "Update", "body": ""}]),
                         "feat: invoice export")
        self.assertEqual(gitx.derive_title("hotfix/login-loop", []), "fix: login loop")


# --------------------------------------------------------------------------- dev pr


ME = {"user": "ana", "host": "mbp1", "agent": "claude", "session": "s1"}


def _pr(n=7, **kw):
    p = {"number": n, "title": "feat(invoice): add csv export", "state": "OPEN", "isDraft": False,
         "headRefName": "feat/invoice", "baseRefName": "main", "headRefOid": "f" * 40, "reviewDecision": "APPROVED",
         "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "url": f"https://github.com/acme/widget/pull/{n}",
         "statusCheckRollup": [{"conclusion": "SUCCESS"}], "additions": 10, "deletions": 2, "changedFiles": 3,
         "author": {"login": "ana"}, "updatedAt": "2026-11-05T10:00:00Z",
         "body": "## What and why\n\nExports invoices.\n\n## Links\n\n- Closes #12\n"}
    p.update(kw)
    return p


class PrTests(unittest.TestCase):
    def pushed(self, s, note="python3 -m unittest: 42 passed"):
        s.branch("feat/invoice")
        s.commit("feat(invoice): add csv export", {"inv.py": "x\n"})
        s.commit("test(invoice): cover empty invoices", {"test_inv.py": "y\n"})
        dev("ci", "--mark-passed", "--note", note)
        self.assertEqual(dev("push")[0], 0)
        s.gh.on(["pr", "create"], "https://github.com/acme/widget/pull/7\n")

    def test_create_refuses_empty_sections_unless_draft(self):
        with Sandbox() as s:
            self.pushed(s)
            rc, _, err = dev("pr", "create")
            self.assertEqual(rc, 2)
            self.assertIn("“What and why”", err)
            self.assertEqual(s.gh.called("pr", "create"), [])
            rc, out, err = dev("pr", "create", "--draft")
            self.assertEqual(rc, 0, err)
            call = s.gh.called("pr", "create")[0]
            self.assertIn("--draft", call)
            self.assertEqual(call[call.index("--title") + 1], "feat(invoice): add csv export")
            body = call[call.index("--body") + 1]
            self.assertIn("- test(invoice): cover empty invoices", body)
            self.assertIn("- Local CI: python3 -m unittest: 42 passed", body)
            self.assertIn("PR #7 opened as draft", out)

    # devkit's `language` setting is the language of its replies (default auto); pull requests stay in American
    # English unless --lang names another
    def test_create_fills_the_english_template_whatever_the_reply_language_unless_lang_names_one(self):
        def body(*argv):
            rc, out, err = dev("pr", "create", "--draft", *argv)
            self.assertEqual(rc, 0, out + err)
            call = s.gh.called("pr", "create")[0]
            return call[call.index("--body") + 1]
        for language in ("es-ES", "auto"):
            with self.subTest(language=language), Sandbox() as s, mock.patch.dict(core.CFG, {"language": language}):
                self.pushed(s)
                sent = body()
                self.assertIn("## What and why", sent)
                self.assertNotIn("Qué y por qué", sent)
        with Sandbox() as s, mock.patch.dict(core.CFG, {"language": "auto"}):
            self.pushed(s)
            self.assertIn("## Qué y por qué", body("--lang", "es"))

    def test_a_lang_that_is_not_a_locale_never_names_a_template_file(self):
        # --lang becomes part of the template's file name (pull_request.<lang>.md): a path never reaches it
        for given in ("../../x", "es/../../etc", "es-ES.md", "", "auto"):
            with self.subTest(given=given):
                self.assertEqual(gitx.lang_of(argparse.Namespace(lang=given)), "en-US")
        for given in ("es", "es-ES", "pt_BR", "fr-FR"):
            with self.subTest(given=given):
                self.assertEqual(gitx.lang_of(argparse.Namespace(lang=given)), given)

    def test_create_with_a_body_file_is_checked_and_cleaned(self):
        with Sandbox() as s:
            self.pushed(s)
            text, _ = gitx.pr_template(s.repo)
            body = text.replace("## Changes", "Exports invoices as CSV for finance.\n\n## Changes") \
                .replace("## Risk and rollback", "- Ran python3 -m unittest: 42 passed\n\n## Risk and rollback") \
                + "\n🤖 Generated with [Claude Code](https://claude.com/claude-code)\n"
            self.assertEqual(gitx.body_problems(text.replace("## Changes", "Why.\n\n## Changes"), text),
                             ["How to verify"])
            f = s.write("../body.md", body)
            rc, out, err = dev("pr", "create", "--body-file", f)
            self.assertEqual(rc, 0, err)
            sent = s.gh.called("pr", "create")[0]
            sent = sent[sent.index("--body") + 1]
            self.assertIn("Exports invoices as CSV for finance.", sent)
            self.assertNotIn("Generated with", sent)
            self.assertIn("removed 1 AI-attribution", out)

    def test_create_validates_the_title(self):
        with Sandbox() as s:
            self.pushed(s)
            for t, why in (("Added csv export", "Conventional Commit"), ("feat: add csv export.", "period"),
                           ("feat: adds csv export", "imperative")):
                rc, _, err = dev("pr", "create", "--draft", "--title", t)
                self.assertEqual(rc, 2, t)
                self.assertIn(why, err)

    def test_the_evidence_gates_a_ready_pr_and_dev_pr_ready_not_a_draft(self):
        # no workflow runs on a draft here, so opening one costs no CI run: the evidence check moves to `dev pr ready`,
        # which starts the one run; a PR opened ready for review still needs it
        with Sandbox() as s:
            s.branch("feat/np")
            s.commit("feat: add np")
            rc, _, err = dev("pr", "create", "--draft")
            self.assertEqual(rc, 2)
            self.assertIn("not on origin", err)
            git(s.repo, "push", "-q", "-u", "origin", "feat/np")
            s.gh.on(["pr", "create"], "https://github.com/acme/widget/pull/7\n")
            rc, _, err = dev("pr", "create")
            self.assertEqual(rc, 2)
            self.assertIn("no local CI evidence", err)
            self.assertEqual(s.gh.called("pr", "create"), [])
            rc, out, err = dev("pr", "create", "--draft")
            self.assertEqual(rc, 0, err)
            self.assertIn("--draft", s.gh.called("pr", "create")[0])
            self.assertIn("dev pr ready", out)
            text, _ = gitx.pr_template(s.repo)
            body = text.replace("## Changes", "Adds np.\n\n## Changes").replace(
                "## Risk and rollback", "- Ran python3 -m unittest: 42 passed\n\n## Risk and rollback")
            s.gh.on(["pr", "view"], _pr(isDraft=True, title="feat: add np", headRefName="feat/np",
                                        headRefOid=s.head(), body=body))
            s.gh.on(["pr", "ready"], "")
            rc, _, err = dev("pr", "ready")
            self.assertEqual(rc, 2)
            self.assertIn("no local CI evidence", err)
            self.assertEqual(s.gh.called("pr", "ready"), [])
            dev("ci", "--mark-passed", "--note", "python3 -m unittest: 42 passed")
            rc, out, err = dev("pr", "ready")
            self.assertEqual(rc, 0, err)
            self.assertEqual(len(s.gh.called("pr", "ready")), 1)

    # `dev pr ready N` checked the evidence only when the PR's branch was the checkout's, so from another branch it
    # started the one CI run of a head no local run had verified.
    def test_ready_for_another_branchs_pr_needs_evidence_for_its_head(self):
        with Sandbox() as s:
            s.branch("feat/np")
            tip = s.commit("feat: add np")
            git(s.repo, "push", "-q", "-u", "origin", "feat/np")
            text, _ = gitx.pr_template(s.repo)
            body = text.replace("## Changes", "Adds np.\n\n## Changes").replace(
                "## Risk and rollback", "- Ran python3 -m unittest: 42 passed\n\n## Risk and rollback")
            draft = _pr(isDraft=True, title="feat: add np", headRefName="feat/np", headRefOid=tip, body=body)
            s.gh.on(["pr", "ready"], "")
            git(s.repo, "switch", "-q", "main")
            for head, why in ((tip, f"no local CI evidence for {tip[:7]}"),
                              ("e" * 40, "PR #7's head eeeeeee is not in this checkout")):
                with self.subTest(head=head[:7]):
                    s.gh.on(["pr", "view"], dict(draft, headRefOid=head))
                    rc, _, err = dev("pr", "ready", "7", "--ci-passed")  # an attestation is for HEAD only
                    self.assertEqual(rc, 2, err)
                    self.assertIn(why, err)
                    self.assertIn("git switch feat/np", err)
            self.assertEqual(s.gh.called("pr", "ready"), [])
            git(s.repo, "switch", "-q", "feat/np")
            dev("ci", "--mark-passed", "--note", "python3 -m unittest: 42 passed")
            git(s.repo, "switch", "-q", "main")
            s.gh.on(["pr", "view"], draft)
            rc, _, err = dev("pr", "ready", "7")
            self.assertEqual(rc, 0, err)
            self.assertEqual(len(s.gh.called("pr", "ready")), 1)

    def test_a_draft_still_needs_evidence_where_ci_runs_on_drafts(self):
        with Sandbox() as s:
            s.branch("feat/np")
            s.commit("ci: run the tests on pull requests", {".github/workflows/ci.yml": (
                "on:\n  pull_request:\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make test\n")})
            git(s.repo, "push", "-q", "-u", "origin", "feat/np")
            rc, _, err = dev("pr", "create", "--draft")
            self.assertEqual(rc, 2)
            self.assertIn("no local CI evidence", err)
            self.assertIn("ci.yml", err)  # why a draft is not free here
            self.assertEqual(s.gh.called("pr", "create"), [])

    def test_create_reports_an_open_pr(self):
        with Sandbox() as s:
            self.pushed(s)
            s.gh.on(["pr", "list"], [{"number": 5, "url": "https://github.com/acme/widget/pull/5"}],
                    when=lambda a: "--head" in a)
            rc, out, _ = dev("pr", "create")
            self.assertEqual(rc, 0)
            self.assertIn("#5 is already open", out)

    def test_dry_run_prints_the_body_in_the_configured_language(self):
        with Sandbox() as s:
            self.pushed(s)
            rc, out, err = dev("pr", "create", "--dry-run", "--lang", "es")
            self.assertEqual(rc, 0)
            self.assertIn("title: feat(invoice): add csv export", err)  # the summary goes to stderr
            self.assertIn("still empty: Qué y por qué", err)  # Spanish PRs use the Spanish template
            self.assertIn("pull_request.es.md", err)
            self.assertTrue(out.startswith("## Qué y por qué"), out[:80])  # stdout is the body alone: `> body.md`
            self.assertIn("- CI local: python3 -m unittest: 42 passed", out)

    def test_merge_gate(self):
        with Sandbox() as s:
            s.branch("feat/invoice")
            s.gh.on(["repo", "view"], {"squashMergeAllowed": True, "mergeCommitAllowed": False,
                                       "rebaseMergeAllowed": False, "viewerDefaultMergeMethod": "MERGE"})
            s.gh.on(["pr", "merge"], "")
            cases = [(dict(statusCheckRollup=[{"conclusion": "FAILURE"}]), "checks are failing"),
                     (dict(statusCheckRollup=[{"status": "IN_PROGRESS"}]), "still running"),
                     (dict(reviewDecision="REVIEW_REQUIRED"), "approving review"),
                     (dict(reviewDecision="CHANGES_REQUESTED"), "changes were requested"),
                     (dict(isDraft=True), "draft"), (dict(mergeable="CONFLICTING"), "conflicts"),
                     (dict(title="Add csv export"), "Conventional Commit")]
            for kw, why in cases:
                s.gh.on(["pr", "view"], _pr(**kw))
                rc, _, err = dev("pr", "merge", "7")
                self.assertEqual(rc, 2, kw)
                self.assertIn(why, err)
            self.assertEqual(s.gh.called("pr", "merge"), [])
            s.gh.on(["pr", "view"], _pr(body="## What and why\n\nExports invoices.\n\n## Links\n\n- Closes #12\n"
                                             "Co-authored-by: Claude <noreply@anthropic.com>\n"))
            rc, out, err = dev("pr", "merge", "7")
            self.assertEqual(rc, 0, err)
            call = s.gh.called("pr", "merge")[0]
            for flag in ("--squash", "--delete-branch", "--match-head-commit"):
                self.assertIn(flag, call)
            self.assertEqual(call[call.index("--subject") + 1], "feat(invoice): add csv export")
            self.assertEqual(call[call.index("--body") + 1], "Exports invoices.\n\nCloses #12")
            self.assertIn("merged #7 (squash)", out)

    def items_on_the_branch(self):
        """Open items linked to feat/invoice in each open state, a done one, and one on another branch."""
        doing = work.claim(work.create("acme/widget", "Export invoices", a=ME), a=ME, branch="feat/invoice")
        blocked = work.claim(work.create("acme/widget", "Import invoices", a=ME), a=ME, branch="feat/invoice")
        work.update(work.load(blocked["path"]), "block", why="waits for the bank's API", a=ME, status="blocked")
        todo = work.create("acme/widget", "Invoice totals", a=ME, links=["branch:feat/invoice"])
        done = work.release(work.claim(work.create("acme/widget", "Invoice model", a=ME), a=ME, branch="feat/invoice"),
                            a=ME, status="done")
        other = work.claim(work.create("acme/widget", "Refunds", a=ME), a=ME, branch="feat/refunds")
        return [doing, blocked, todo], done, other

    def merge(self, s, *flags):
        s.gh.on(["repo", "view"], {"squashMergeAllowed": True, "viewerDefaultMergeMethod": "SQUASH"})
        s.gh.on(["pr", "merge"], "")
        s.gh.on(["pr", "view"], _pr(mergeCommit={"oid": "abc1234" + "0" * 33}))
        rc, out, err = dev("pr", "merge", "7", *flags)
        self.assertEqual(rc, 0, err)
        return out

    def auto_merge(self, s, after, repo=None, pr=None, code=0):
        """`dev pr merge 7 --auto` on a PR whose checks still run and that the base branch's protection holds (merge
        state BLOCKED); `after`: what GitHub says of the PR once gh returned. -> (stdout, stderr)"""
        s.gh.on(["repo", "view"], {"squashMergeAllowed": True, "viewerDefaultMergeMethod": "SQUASH",
                                   "deleteBranchOnMerge": False})
        s.gh.on(["api", "repos/acme/widget"], dict({"allow_auto_merge": True}, **(repo or {})))
        s.gh.on(["pr", "merge"], "")
        s.gh.on(["pr", "view"], dict(_pr(statusCheckRollup=[{"status": "IN_PROGRESS"}], mergeStateStatus="BLOCKED",
                                         mergeCommit={"oid": "abc1234" + "0" * 33}), **(pr or {})))
        s.gh.on(["pr", "view"], after, when=lambda args: "autoMergeRequest" in args[-1])
        rc, out, err = dev("pr", "merge", "7", "--auto")
        self.assertEqual(rc, code, out + err)
        return out, err

    # gh pr merge --auto merges at once a PR that nothing on GitHub holds (merge state CLEAN, HAS_HOOKS or UNSTABLE):
    # on a base branch that requires no check, PR #19 merged while its checks still ran, and devkit said auto-merge
    # was enabled. --auto waits only for what the base branch requires; with checks running and nothing that holds the
    # merge, it is refused, here and through the gh gate.
    def test_auto_merge_refuses_running_checks_that_nothing_on_github_waits_for(self):
        with Sandbox() as s:
            s.branch("feat/invoice")
            s.gh.on(["repo", "view"], {"squashMergeAllowed": True, "viewerDefaultMergeMethod": "SQUASH"})
            s.gh.on(["pr", "merge"], "")
            for state in ("CLEAN", "UNSTABLE", "HAS_HOOKS", "UNKNOWN"):
                with self.subTest(state):
                    s.gh.on(["pr", "view"], _pr(statusCheckRollup=[{"status": "IN_PROGRESS"}],
                                                mergeStateStatus=state))
                    for argv in (("pr", "merge", "7", "--auto"), ("gh", "pr", "merge", "7", "--auto", "--squash")):
                        rc, _, err = dev(*argv)
                        self.assertEqual(rc, 2, err)
                        self.assertIn("checks are still running and nothing on GitHub holds the merge", err)
            self.assertEqual(s.gh.called("pr", "merge"), [])
            self.assertEqual(s.execs, [])  # the gh gate ran no gh pr merge either

    # gh's repo view has no auto-merge field, so devkit reads the REST API's allow_auto_merge; when it cannot, it
    # says so and merges nothing rather than guessing.
    def test_auto_merge_reads_the_rest_setting_and_merges_nothing_when_it_cannot(self):
        with Sandbox() as s:
            s.branch("feat/invoice")
            self.auto_merge(s, after={"state": "OPEN", "autoMergeRequest": {"enabledAt": "2026-11-18T00:00:00Z"}})
            self.assertTrue(s.gh.called("api", "repos/acme/widget"))
            s.gh.routes = [r for r in s.gh.routes if r[0] != ["api", "repos/acme/widget"]]
            s.gh.calls.clear()
            rc, out, err = dev("pr", "merge", "7", "--auto")
            self.assertEqual(rc, 1, out + err)
            self.assertIn("cannot tell whether the repository allows auto-merge", err)
            self.assertEqual(s.gh.called("pr", "merge"), [])

    def test_auto_merge_refuses_a_repository_that_does_not_allow_it(self):
        with Sandbox() as s:
            s.branch("feat/invoice")
            _, err = self.auto_merge(s, after={"state": "OPEN"}, repo={"allow_auto_merge": False}, code=2)
            self.assertIn("the repository does not allow auto-merge", err)
            self.assertEqual(s.gh.called("pr", "merge"), [])

    def test_auto_merge_on_a_protected_base_waits_for_its_required_checks(self):
        with Sandbox() as s:
            s.branch("feat/invoice")
            linked, _, _ = self.items_on_the_branch()
            out, _ = self.auto_merge(s, after={"state": "OPEN", "autoMergeRequest": {"mergeMethod": "SQUASH"}})
            call = s.gh.called("pr", "merge")[0]
            self.assertIn("--auto", call)
            self.assertNotIn("--delete-branch", call)
            self.assertIn("auto-merge (squash) enabled for #7", out)
            self.assertIn("the checks and reviews the base branch requires", out)
            self.assertEqual([work.load(i["path"])["meta"]["status"] for i in linked], ["doing", "blocked", "todo"])

    # When the checks the base requires had passed by the time gh asked, GitHub merged the PR at once: devkit said
    # "auto-merge enabled", left the merged branch and its items to a later dev task reconcile.
    def test_an_auto_merge_github_did_at_once_is_reported_as_a_merge_with_its_bookkeeping(self):
        with Sandbox() as s:
            s.branch("feat/invoice")
            linked, _, _ = self.items_on_the_branch()
            out, _ = self.auto_merge(s, after={"state": "MERGED", "autoMergeRequest": None})
            self.assertNotIn("enabled", out)
            self.assertIn("merged #7 (squash) into main", out)
            self.assertIn("remote branch kept", out)  # the repository does not delete merged branches
            self.assertIn("closed 3 work item(s) linked to feat/invoice", out)
            for it in linked:
                self.assertEqual(work.load(it["path"])["meta"]["status"], "done")

    def test_an_auto_merge_github_did_not_take_fails(self):
        with Sandbox() as s:
            s.branch("feat/invoice")
            out, err = self.auto_merge(s, after={"state": "OPEN", "autoMergeRequest": None}, code=1)
            self.assertNotIn("enabled for", out)
            self.assertIn("#7 is open with no auto-merge on GitHub", err)

    # A PR whose checks passed and that nothing holds is merged by gh at once, --auto or not: devkit merges it as a
    # plain merge, deleting the remote branch and closing its items, instead of claiming an auto-merge.
    def test_auto_on_a_pr_that_can_merge_now_merges_it_now(self):
        with Sandbox() as s:
            s.branch("feat/invoice")
            linked, _, _ = self.items_on_the_branch()
            out = self.merge(s, "--auto")
            call = s.gh.called("pr", "merge")[0]
            self.assertNotIn("--auto", call)
            self.assertIn("--delete-branch", call)
            self.assertIn("merged #7 (squash) into main", out)
            self.assertIn("nothing on GitHub holds it", out)
            self.assertEqual(work.load(linked[0]["path"])["meta"]["status"], "done")

    # The dashboard and `dev status` read the PR cache, kept for 15 minutes: a PR opened, made ready or merged with dev
    # showed its old state there until the cache ran out.
    def test_the_pr_cache_shows_the_new_state_right_after_dev_pr_create_ready_and_merge(self):
        def github_lists(s, **pr):  # what `gh pr list` answers now, for the registry's refresh only
            s.gh.on(["pr", "list"], [dict({"number": 7, "headRefName": "feat/invoice", "title": "feat: x"}, **pr)],
                    when=lambda args: "200" in args or "50" in args)

        for command in ("create", "ready", "merge"):
            with self.subTest(command), Sandbox() as s:
                cache = registry.STATE / "prs" / "acme__widget.json"
                cache.parent.mkdir(parents=True)
                cache.write_text(json.dumps([] if command == "create" else
                                            [{"number": 7, "state": "OPEN", "isDraft": command == "ready"}]))
                self.pushed(s)
                if command == "create":
                    github_lists(s, state="OPEN", isDraft=False)
                    text, _ = gitx.pr_template(s.repo)
                    body = s.write("../body.md", text.replace("## Changes", "Exports CSV.\n\n## Changes").replace(
                        "## Risk and rollback", "- Ran python3 -m unittest: 42 passed\n\n## Risk and rollback"))
                    rc, _, err = dev("pr", "create", "--body-file", body)
                    want = {"state": "OPEN", "isDraft": False}
                elif command == "ready":
                    git(s.repo, "push", "-q", "-u", "origin", "feat/invoice")
                    text, _ = gitx.pr_template(s.repo)
                    s.gh.on(["pr", "view"], _pr(isDraft=True, headRefOid=s.head(), body=text.replace(
                        "## Changes", "Exports CSV.\n\n## Changes").replace(
                        "## Risk and rollback", "- Ran python3 -m unittest: 42 passed\n\n## Risk and rollback")))
                    s.gh.on(["pr", "ready"], "")
                    github_lists(s, state="OPEN", isDraft=False)
                    rc, _, err = dev("pr", "ready")
                    want = {"state": "OPEN", "isDraft": False}
                else:
                    github_lists(s, state="MERGED", isDraft=False)
                    self.merge(s)
                    rc, err, want = 0, "", {"state": "MERGED", "isDraft": False}
                self.assertEqual(rc, 0, err)
                listed = {p["number"]: p for p in json.loads(cache.read_text())}
                self.assertEqual({k: listed.get(7, {}).get(k) for k in want}, want)

    def squash_on_github(self, s, wt):
        """feat/invoice, checked out in the worktree `wt`, pushed and squash-merged on GitHub as PR #7 (the remote
        branch deleted with it). -> what `gh pr view 7` answers once it is merged"""
        tip = s.commit("feat(invoice): add csv export", repo=wt)
        git(wt, "push", "-q", "-u", "origin", "feat/invoice")
        o = s.other()
        git(o, "fetch", "-q", "origin")
        git(o, "switch", "-q", "main")
        git(o, "pull", "-q", "--ff-only", "origin", "main")
        git(o, "merge", "-q", "--squash", "origin/feat/invoice")
        git(o, "commit", "-q", "-m", "feat(invoice): add csv export (#7)")
        git(o, "push", "-q", "origin", "main")
        git(o, "push", "-q", "origin", "--delete", "feat/invoice")
        return _pr(headRefOid=tip, isCrossRepository=False, mergeCommit={"oid": s.head(repo=o)})

    def test_merge_removes_the_clean_worktree_of_the_merged_branch_and_its_local_branch(self):
        # GitHub deleted the remote branch; the local branch and its worktree stayed until someone cleaned them
        with Sandbox() as s, mock.patch.object(core, "active_cwds", return_value=[]):
            git(s.repo, "branch", "feat/invoice")
            wt = s.root / "wt-invoice"
            git(s.repo, "worktree", "add", "-q", str(wt), "feat/invoice")
            merged = self.squash_on_github(s, wt)
            s.gh.on(["repo", "view"], {"squashMergeAllowed": True, "viewerDefaultMergeMethod": "SQUASH"})
            s.gh.on(["pr", "merge"], "")
            s.gh.on(["pr", "view"], merged)
            rc, out, err = dev("pr", "merge", "7")
            self.assertEqual(rc, 0, err)
            self.assertFalse(wt.exists())
            self.assertEqual(gitx._ref_sha(s.repo, "refs/heads/feat/invoice"), "")
            self.assertIn("worktree", out)
            self.assertIn("merged: PR #7 was merged", out)

    def test_merge_keeps_a_worktree_with_uncommitted_changes_and_says_why(self):
        with Sandbox() as s, mock.patch.object(core, "active_cwds", return_value=[]):
            git(s.repo, "branch", "feat/invoice")
            wt = s.root / "wt-invoice"
            git(s.repo, "worktree", "add", "-q", str(wt), "feat/invoice")
            merged = self.squash_on_github(s, wt)
            (wt / "notes.txt").write_text("not committed\n")
            s.gh.on(["repo", "view"], {"squashMergeAllowed": True, "viewerDefaultMergeMethod": "SQUASH"})
            s.gh.on(["pr", "merge"], "")
            s.gh.on(["pr", "view"], merged)
            rc, out, err = dev("pr", "merge", "7")
            self.assertEqual(rc, 0, err)
            self.assertTrue((wt / "notes.txt").exists())
            # the kept worktree is a warning of its own, never part of the merge's success line
            self.assertIn("merged #7", out)
            self.assertNotIn("kept", out)
            self.assertRegex(err, rf"(?m)^{re.escape(ui.WARN)} kept .*wt-invoice: it has uncommitted")

    def test_merge_closes_the_open_items_linked_to_the_merged_branch(self):
        with Sandbox() as s:
            s.branch("feat/invoice")
            linked, done, other = self.items_on_the_branch()
            done_before = done["path"].read_text()
            self.auto_merge(s, after={"state": "OPEN", "autoMergeRequest": {"mergeMethod": "SQUASH"}})
            self.assertEqual([work.load(i["path"])["meta"]["status"] for i in linked], ["doing", "blocked", "todo"])
            self.merge(s)
            for it in linked:
                with self.subTest(it["meta"]["title"]):
                    after = work.load(it["path"])
                    self.assertEqual((after["meta"]["status"], after["meta"]["claimed_by"],
                                      after["meta"]["lock_expires"]), ("done", "", ""))
                    self.assertIn("merged in PR #7 (abc1234)", after["changelog"][0])
            self.assertEqual(done["path"].read_text(), done_before)
            self.assertEqual(work.load(other["path"])["meta"]["status"], "doing")

    def test_merge_names_the_items_it_closed(self):
        with Sandbox() as s:
            s.branch("feat/invoice")
            linked, done, other = self.items_on_the_branch()
            out = self.merge(s)
            self.assertIn("closed 3 work item(s) linked to feat/invoice", out)
            for it in linked:
                self.assertIn(f"{it['meta']['id']} “{it['meta']['title']}”", out)
            self.assertNotIn(other["meta"]["id"], out)
            self.assertNotIn(done["meta"]["id"], out)

    def test_merge_method(self):
        both = {"squashMergeAllowed": True, "mergeCommitAllowed": True, "viewerDefaultMergeMethod": "MERGE"}
        self.assertEqual(gitx.merge_method(both), ("merge", None))
        self.assertEqual(gitx.merge_method(dict(both, viewerDefaultMergeMethod="")), ("squash", None))
        self.assertIsNone(gitx.merge_method(both, "rebase")[0])
        self.assertEqual(gitx.merge_method({"rebaseMergeAllowed": True}), ("rebase", None))

    def test_checks_summary_with_trimmed_failing_logs(self):
        with Sandbox() as s:
            s.branch("feat/invoice")
            checks = [{"name": "lint", "bucket": "pass", "workflow": "CI", "link": "x"},
                      {"name": "build", "bucket": "pass", "workflow": "CI", "link": "x"},
                      {"name": "test", "bucket": "fail", "workflow": "CI",
                       "link": "https://github.com/acme/widget/actions/runs/111/job/222"}]
            s.gh.on(["pr", "checks"], checks, rc=1)
            log = "\n".join(f"test\tRun tests\t2026-11-05T10:00:{i:02d}.1234567Z line {i}" for i in range(60))
            log += "\ntest\tRun tests\t2026-11-05T10:01:00.0000000Z ##[error]AssertionError: 1 != 2"
            s.gh.on(["run", "view", "111"], log)
            rc, out, _ = dev("pr", "checks", "7", "--watch", "--lines", "10")
            self.assertEqual(rc, 1)
            self.assertIn("2 pass · 1 fail · 0 pending", out)
            self.assertIn("error: AssertionError: 1 != 2", out)
            self.assertNotIn("2026-11-05T", out)
            self.assertLess(len(out.splitlines()), 25)
            watch = [c for c in s.gh.calls if "--watch" in c]
            self.assertEqual(len(watch), 1)
            self.assertEqual(s.gh.called("run", "view", "111")[0][-2:], ["--job", "222"])

    def test_every_check_skipped_waits_for_the_real_run(self):
        # B3: a draft's run skips every job with the draft gate; after `dev pr ready` the real run starts. Until then
        # the checks are "no checks yet": --watch waits and says so, and merge refuses, never green.
        with Sandbox() as s:
            s.branch("feat/invoice")
            s.commit("ci: add the workflow", {".github/workflows/ci.yml": (
                "on: [pull_request]\njobs:\n  t:\n    if: github.event.pull_request.draft == false\n"
                "    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n")})
            s.gh.on(["pr", "checks"], [{"name": "t", "bucket": "skipping", "workflow": "CI", "link": "x"}])
            rc, out, _ = dev("pr", "checks", "7", "--watch")
            self.assertEqual(rc, 8, out)
            self.assertIn("no checks yet for 7: every check was skipped", out)
            self.assertNotIn("✓", out)
            self.assertGreaterEqual(len(s.gh.called("pr", "checks")), 6)  # it waited for the real run to report
            s.gh.on(["repo", "view"], {"squashMergeAllowed": True, "viewerDefaultMergeMethod": "SQUASH"})
            s.gh.on(["pr", "merge"], "")
            s.gh.on(["pr", "view"], _pr(statusCheckRollup=[{"conclusion": "SKIPPED"}, {"conclusion": "SKIPPED"}]))
            rc, _, err = dev("pr", "merge", "7")
            self.assertEqual(rc, 2)
            self.assertIn("every check was skipped, so no real run has reported yet (dev pr checks 7 --watch)", err)
            self.assertEqual(s.gh.called("pr", "merge"), [])

    def test_ls_mine_and_review(self):
        with Sandbox() as s:
            prs = [_pr(1, author={"login": "ana"}), _pr(2, title="fix: y", author={"login": "bob"}),
                   _pr(3, title="docs: z", author={"login": "carol"})]
            s.gh.on(["pr", "list"], prs)
            s.gh.on(["pr", "list"], [{"number": 2}], when=lambda a: "--search" in a)
            rc, out, _ = dev("pr", "ls", "--json")
            data = json.loads(out)
            self.assertEqual([p["number"] for p in data["mine"]], [1])
            self.assertEqual([p["number"] for p in data["review"]], [2])
            self.assertEqual(data["others"], 1)
            rc, out, _ = dev("pr", "ls")
            self.assertIn("mine", out)
            self.assertIn("1 other open PR", out)

    def test_ls_all_projects_from_the_registry_cache(self):
        with Sandbox() as s:
            core.registry_cache().parent.mkdir(parents=True, exist_ok=True)
            core.registry_cache().write_text(json.dumps({"projects": [
                {"id": "acme/api", "prs_open": [{"number": 4, "title": "feat: add api", "author": "ana",
                                                 "checks": "passing", "review": "APPROVED", "updated": None}]}]}))
            s.gh.on(["search", "prs"], [{"repository": {"nameWithOwner": "Acme/Web"}, "number": 8,
                                         "title": "fix: web", "updatedAt": "2026-11-05T10:00:00Z"}])
            rc, out, _ = dev("pr", "ls", "--all-projects", "--json")
            data = json.loads(out)
            self.assertEqual(data["mine"][0]["project"], "acme/api")
            self.assertEqual(data["review"][0]["project"], "acme/web")

    def test_ready_needs_the_sections(self):
        with Sandbox() as s:
            s.branch("feat/invoice")
            text, _ = gitx.pr_template(s.repo)
            s.gh.on(["pr", "view"], _pr(isDraft=True, body=text))
            rc, _, err = dev("pr", "ready")
            self.assertEqual(rc, 2)
            self.assertIn("still empty", err)

    def test_other_hosts_are_not_supported_yet(self):
        with Sandbox() as s:
            git(s.repo, "remote", "set-url", "origin", "https://gitlab.com/acme/widget.git")
            rc, _, err = dev("pr", "ls")
            self.assertEqual(rc, 2)
            self.assertIn("supports GitHub only", err)
            self.assertEqual(s.gh.calls, [])


class PrTitleConventionTests(unittest.TestCase):
    # firefly-weave titles its PRs as plain imperative sentences; the repository's convention wins over devkit's
    # Conventional Commit default. Bots do not count: dependabot's prefix is its own configuration.
    WEAVE_TITLES = ("Keep run statistics and chart them in Studio", "Execute steps in Studio without a platform",
                    "test(agentic): count lease expiry from the observed capacity retry",
                    "Run workflow transactions: settle, undo in reverse and never lose an outcome",
                    "Keep test data in the editor: test input, pinned output and out-of-date marks",
                    "Run local operations out of the box and review every infrastructure change",
                    "Make Studio's CI reliable: start the test host before use, cache contract schemas, retry a busy "
                    "DMG step")

    def sentence_repo(self, s, subject="Export invoices as CSV"):
        merged = [{"title": t, "author": {"login": "ana", "is_bot": False}} for t in self.WEAVE_TITLES]
        merged += [{"title": f"chore(deps): bump {p} from 1.0 to 1.1", "author": {"login": "app/dependabot",
                                                                                  "is_bot": True}}
                   for p in ("certifi", "authlib")]
        s.gh.on(["pr", "list"], merged, when=lambda a: "merged" in a)
        s.branch("feat/invoice-export")
        s.commit(subject, {"inv.py": "x\n"})
        dev("ci", "--mark-passed", "--note", "python3 -m unittest: 42 passed")
        self.assertEqual(dev("push")[0], 0)
        s.gh.on(["pr", "create"], "https://github.com/acme/widget/pull/7\n")

    def test_create_takes_a_plain_sentence_title_where_the_repository_uses_them(self):
        with Sandbox() as s:
            self.sentence_repo(s)
            rc, out, err = dev("pr", "create", "--draft")
            self.assertEqual(rc, 0, err)
            call = s.gh.called("pr", "create")[0]
            self.assertEqual(call[call.index("--title") + 1], "Export invoices as CSV")
            self.assertEqual(gitx.title_convention(s.ctx())["seen"], 7)  # the bots' two are not counted

    def test_create_checks_a_title_against_the_plain_sentence_convention(self):
        with Sandbox() as s:
            self.sentence_repo(s)
            for t, why in (("feat(invoice): add csv export", "plain imperative sentences (6 of the last 7 merged"),
                           ("Added a CSV export", "imperative"), ("export invoices as CSV", "capital letter"),
                           ("Export invoices as CSV.", "period"), ("WIP export", "WIP")):
                with self.subTest(title=t):
                    rc, _, err = dev("pr", "create", "--draft", "--title", t)
                    self.assertEqual(rc, 2, err)
                    self.assertIn(why, err)
            self.assertIn("`Add csv export`", dev("pr", "create", "--draft", "--title", "feat(invoice): add csv export")[2])
            self.assertEqual(s.gh.called("pr", "create"), [])

    def test_a_conventional_commit_on_the_branch_becomes_a_sentence_title_there(self):
        with Sandbox() as s:
            self.sentence_repo(s, subject="feat(invoice): export invoices as CSV")
            rc, _, err = dev("pr", "create", "--draft")
            self.assertEqual(rc, 0, err)
            call = s.gh.called("pr", "create")[0]
            self.assertEqual(call[call.index("--title") + 1], "Export invoices as CSV")

    def test_ready_follows_the_same_title_convention(self):
        with Sandbox() as s:
            self.sentence_repo(s)
            text, _ = gitx.pr_template(s.repo)
            body = text.replace("## Changes", "Exports invoices.\n\n## Changes").replace(
                "## Risk and rollback", "- Ran python3 -m unittest: 42 passed\n\n## Risk and rollback")
            s.gh.on(["pr", "view"], _pr(isDraft=True, title="Export invoices as CSV", headRefName="feat/invoice-export",
                                        headRefOid=s.head(), body=body))
            s.gh.on(["pr", "ready"], "")
            rc, _, err = dev("pr", "ready")
            self.assertEqual(rc, 0, err)
            self.assertEqual(len(s.gh.called("pr", "ready")), 1)

    def test_the_default_branch_history_decides_when_github_lists_no_merged_prs(self):
        with Sandbox() as s:
            for subject in ("Add the widget", "Render the widget's frame", "Fix the frame's border",
                            "Document the widget", "Merge branch 'feat/frame'", "Release 0.1.0a1"):
                s.commit(subject)
            git(s.repo, "push", "-q", "origin", "main")
            got = gitx.title_convention(s.ctx())
            self.assertEqual((got["style"], got["source"], got["seen"], got["plain"]),
                             ("sentence", "commits on main", 6, 5))  # the start commit is conventional; merges skip

    # A repository whose commits are plain sentences had them refused as "not a Conventional Commit": dev commit kept
    # devkit's default where dev pr create already followed the repository's own convention.
    def test_commit_follows_the_plain_sentence_convention_of_the_default_branch(self):
        with Sandbox() as s:
            for subject in ("Add the widget", "Render the widget's frame", "Fix the frame's border",
                            "Document the widget", "Release 0.1.0a1"):
                s.commit(subject)
            git(s.repo, "push", "-q", "origin", "main")
            s.branch("feat/csv")
            s.write("inv.py", "x\n")
            git(s.repo, "add", "inv.py")
            rc, _, err = dev("commit", "-m", "Export invoices as CSV")
            self.assertEqual(rc, 0, err)
            self.assertEqual(git(s.repo, "log", "-1", "--format=%s"), "Export invoices as CSV")
            s.write("inv.py", "y\n")
            git(s.repo, "add", "inv.py")
            for argv in (("commit", "-m", "feat(invoice): export invoices"),
                         ("git", "commit", "-m", "feat(invoice): export invoices")):
                with self.subTest(argv=argv[0]):
                    rc, _, err = dev(*argv)
                    self.assertEqual(rc, 2, err)
                    self.assertIn("writes its commit messages as plain imperative sentences (5 of the last 6 "
                                  "commits on main)", err)
                    self.assertIn("`Export invoices`", err)
            self.assertEqual(s.execs, [])

    def test_conventional_commits_stay_the_default_without_a_consistent_convention(self):
        with Sandbox() as s:
            ctx = s.ctx()
            self.assertEqual(gitx.title_convention(ctx)["style"], "conventional")  # one commit, no merged PR
            mixed = ["feat: add a", "fix: repair b", "Add c", "Render d", "docs: explain e", "Fix f"]
            s.gh.on(["pr", "list"], [{"title": t, "author": {"login": "ana"}} for t in mixed],
                    when=lambda a: "merged" in a)
            self.assertEqual(gitx.title_convention(ctx)["style"], "conventional")
            devkit_like = ["fix(setup): align saved profiles", "chore(release): 26.11.10",
                           "docs: add a security policy and a contributing guide", "fix: hotfix 26.11.7, nothing waits",
                           "fix(agents): read the self-test's log from its byte offset"]
            s.gh.on(["pr", "list"], [{"title": t, "author": {"login": "ana"}} for t in devkit_like],
                    when=lambda a: "merged" in a)
            self.assertEqual(gitx.title_convention(ctx)["style"], "conventional")
            self.assertIn("is not a Conventional Commit", gitx.pr_title_violation(ctx, "Add c") or "")


# --------------------------------------------------------------------------- dev run


class RunTests(unittest.TestCase):
    def test_rerun_needs_a_flaky_reason_and_records_it(self):
        with Sandbox() as s:
            rc, _, err = dev("run", "rerun", "99")
            self.assertEqual(rc, 2)
            self.assertIn("dev ci --run", err)
            s.gh.on(["run", "view"], {"workflowName": "CI", "headBranch": "feat/x", "conclusion": "failure",
                                      "jobs": [{"name": "test", "conclusion": "failure"}]})
            s.gh.on(["run", "rerun"], "")
            rc, out, err = dev("run", "rerun", "99", "--flaky", "test_upload times out on the runner; 10/10 locally")
            self.assertEqual(rc, 0, err)
            self.assertEqual(s.gh.called("run", "rerun")[0], ["run", "rerun", "99", "-R", "acme/widget", "--failed"])
            rec = json.loads((core.STATE / "flaky.jsonl").read_text().splitlines()[-1])
            self.assertEqual(rec["jobs"], ["test"])
            self.assertIn("10/10 locally", s.logged[0][1])

    def test_view_failed_shows_trimmed_logs(self):
        with Sandbox() as s:
            s.gh.on(["run", "view"], {"workflowName": "CI", "number": 4, "conclusion": "failure",
                                      "headBranch": "feat/x", "headSha": "abc1234def", "event": "push",
                                      "url": "u", "jobs": [{"name": "test", "conclusion": "failure", "steps": [
                                          {"name": "Run tests", "conclusion": "failure"}]},
                                          {"name": "lint", "conclusion": "success"}]})
            s.gh.on(["run", "view"], "test\tRun tests\t2026-11-05T10:00:00.1Z FAILED test_x.py::test_y",
                    when=lambda a: "--log-failed" in a)
            rc, out, _ = dev("run", "view", "4", "--failed")
            self.assertEqual(rc, 1)
            self.assertIn("failed at “Run tests”", out)
            self.assertIn("FAILED test_x.py::test_y", out)
            self.assertIn("1 job(s) passed", out)

    def test_trim_log_keeps_the_failing_step_only(self):
        ts = "2026-11-05T10:00:00.1234567Z "
        log = "\n".join(["job\tUNKNOWN STEP\t" + ts + x for x in [
            "##[group]Run actions/checkout@v4", "Syncing repository", "##[endgroup]",
            "##[group]Run npm test", "\x1b[36;1mnpm test\x1b[0m", "ok 1 - parses", "not ok 2 - exports",
            "##[error]Process completed with exit code 1.", "##[group]Run actions/upload-artifact@v4",
            "Artifact uploaded", "[command]/usr/bin/git config --local --unset-all x", "Cleaning up orphan processes"]])
        self.assertEqual(gitx.trim_log(log), ["ok 1 - parses", "not ok 2 - exports",
                                              "error: Process completed with exit code 1."])

    def test_ls_reuses_cistatus(self):
        with Sandbox():
            run = {"id": "42", "name": "CI", "number": 9, "status": "failed", "elapsed": 61, "finished": time.time(),
                   "event": "push", "sha": "abc1234", "branch": "feat/x"}
            cistatus.runs_for = lambda top, branch=None, limit=5, detail=True, max_age=20: \
                {"provider": "github", "repo": "acme/widget", "branch": branch, "runs": [run], "error": None}
            rc, out, _ = dev("run", "ls")
            self.assertEqual(rc, 0)
            self.assertIn("42", out)
            self.assertIn("CI #9 failed", out)


# --------------------------------------------------------------------------- dev release


class ReleaseTests(unittest.TestCase):
    def merged_since_the_last_tag(self, s):
        """feat/old is merged and tagged v<YY>.<MM>.0, then feat/invoice is merged; an item is open on each.
        -> (the item on feat/invoice, the item on feat/old)"""
        new = work.claim(work.create("acme/widget", "Export invoices", a=ME), a=ME, branch="feat/invoice")
        old = work.claim(work.create("acme/widget", "Old work", a=ME), a=ME, branch="feat/old")
        prs = []
        for n, branch in ((5, "feat/old"), (7, "feat/invoice")):
            s.branch(branch)
            s.commit(f"feat: work on {branch}")
            git(s.repo, "switch", "-q", "main")
            git(s.repo, "merge", "-q", "--no-ff", branch, "-m", f"Merge pull request #{n} from acme/{branch}")
            prs.append(merged_pr(n, branch, s.head(), work.now_iso()))
            if branch == "feat/old":
                git(s.repo, "tag", f"v{YY}.{MM}.0")
        git(s.repo, "push", "-q", "origin", "main")
        s.gh.on(["pr", "list"], prs, when=lambda args: "merged" in args)
        dev("ci", "--mark-passed")
        s.gh.on(["run", "list"], [])
        return new, old

    def test_a_merge_commit_names_the_branch_it_merged(self):
        cases = {"Merge pull request #8 from fireflysoftwaresolutions/release/26.11.5": ("release/26.11.5", 8),
                 "Merge branch 'feat/x'": ("feat/x", None),
                 "Merge branch 'feat/x' into main": ("feat/x", None),
                 "Merge branch 'feat/x' into 'main'": ("feat/x", None),
                 "Merge remote-tracking branch 'origin/fix/y'": ("fix/y", None),
                 "feat(invoice): add csv export (#7)": (None, None),
                 "Merge tag 'v1.0'": (None, None)}
        for subject, want in cases.items():
            with self.subTest(subject):
                self.assertEqual(gitx.merged_branch(subject), want)

    def test_release_deletes_the_branches_it_merged_and_names_them(self):
        # feat/old shipped in the previous release; feat/invoice ships now; feat/wip is not merged
        with Sandbox() as s, mock.patch.object(core, "active_cwds", return_value=[]):
            Path(core.HANDOFF).mkdir(parents=True, exist_ok=True)  # its embargo list reads: no embargo
            for n, branch in ((5, "feat/old"), (7, "feat/invoice")):
                s.branch(branch)
                s.commit(f"feat: work on {branch}")
                git(s.repo, "push", "-q", "-u", "origin", branch)
                git(s.repo, "switch", "-q", "main")
                git(s.repo, "merge", "-q", "--no-ff", branch, "-m", f"Merge pull request #{n} from acme/{branch}")
                if branch == "feat/old":
                    git(s.repo, "tag", f"v{YY}.{MM}.0")
            s.branch("feat/wip")
            s.commit("feat: add the unfinished thing")
            git(s.repo, "switch", "-q", "main")
            git(s.repo, "push", "-q", "origin", "main")
            dev("ci", "--mark-passed")
            s.gh.on(["run", "list"], [])
            s.gh.on(["release", "create"], f"https://github.com/acme/widget/releases/tag/v{YY}.{MM}.1\n")
            rc, out, err = dev("release")
            self.assertEqual(rc, 0, err)
            for where in ("remote", "local"):
                self.assertIn(f"deleted {where} feat/invoice (merged: its tip is in origin/main)", out)
            self.assertEqual(gitx._ref_sha(s.repo, "refs/heads/feat/invoice"), "")
            self.assertEqual(gitx._ref_sha(s.origin, "refs/heads/feat/invoice"), "")
            for kept in ("feat/old", "feat/wip"):
                self.assertTrue(gitx._ref_sha(s.repo, f"refs/heads/{kept}"), kept)
            self.assertTrue(gitx._ref_sha(s.origin, "refs/heads/feat/old"))

    def test_release_refuses_while_items_of_branches_merged_since_the_last_tag_are_open(self):
        with Sandbox() as s:
            new, old = self.merged_since_the_last_tag(s)
            for argv in (["release", "--dry-run"], ["release"]):
                with self.subTest(argv=argv):
                    rc, _, err = dev(*argv)
                    self.assertEqual(rc, 2, err)
                    self.assertIn(f"1 open work item(s) linked to branches merged since v{YY}.{MM}.0", err)
                    self.assertIn(f"{new['meta']['id']} “Export invoices” · feat/invoice (PR #7)", err)
                    self.assertNotIn(old["meta"]["id"], err)
                    self.assertIn("--why", err)
            self.assertEqual(git(s.repo, "tag", "--list").split(), [f"v{YY}.{MM}.0"])

    def test_release_with_a_reason_lists_the_open_items_and_records_the_reason(self):
        with Sandbox() as s:
            new, _ = self.merged_since_the_last_tag(s)
            why = "the export ships behind a flag; its item stays open for the docs"
            rc, out, err = dev("release", "--dry-run", "--why", why)
            self.assertEqual(rc, 0, err)
            self.assertIn(f"{new['meta']['id']} “Export invoices” · feat/invoice (PR #7)", out)
            s.gh.on(["release", "create"], f"https://github.com/acme/widget/releases/tag/v{YY}.{MM}.1\n")
            rc, out, err = dev("release", "--why", why)
            self.assertEqual(rc, 0, err)
            self.assertIn(f"released with 1 open work item(s) · why: {why}", out)
            recs = audit.query(days=1, action="dev.release.open-items")
            self.assertEqual([(r["target"], r.get("why")) for r in recs], [(f"v{YY}.{MM}.1", why)])
            self.assertIn(new["meta"]["id"], recs[0]["detail"])

    def test_dry_run_computes_the_next_calver(self):
        with Sandbox() as s:
            dev("ci", "--mark-passed")
            s.gh.on(["run", "list"], [])
            rc, out, err = dev("release", "--dry-run")
            self.assertEqual(rc, 0, err)
            self.assertIn(f"release v{YY}.{MM}.0 (calver)", out)
            git(s.repo, "tag", f"v{YY}.{MM}.0")
            rc, out, _ = dev("release", "--dry-run")
            self.assertIn(f"v{YY}.{MM}.1", out)

    # dev delivery reads tags such as v0.1.0a16 as PEP 440, but dev release read them as SemVer: it could not name the
    # newest release and refused v0.1.0a17 as "not a version", so a pep440 project could not release with devkit.
    def test_a_pep440_project_releases_its_pre_releases(self):
        with Sandbox() as s:
            for v in ("0.1.0a9", "0.1.0a16", "0.1.0a16.post1"):
                s.commit(f"chore: release {v}", {"pyproject.toml": f'[project]\nname = "w"\nversion = "{v}"\n'})
                git(s.repo, "tag", f"v{v}")
            self.assertEqual(gitx.version_scheme(s.repo)[:2], ("pep440", "v0.1.0a16.post1"))
            s.commit("chore: release 0.1.0a17", {"pyproject.toml": '[project]\nname = "w"\nversion = "0.1.0a17"\n'})
            git(s.repo, "push", "-q", "origin", "main")
            dev("ci", "--mark-passed")
            s.gh.on(["run", "list"], [])
            rc, _, err = dev("release", "--dry-run")
            self.assertEqual(rc, 2)
            self.assertIn("own version scheme (latest v0.1.0a16.post1)", err)
            rc, out, err = dev("release", "--dry-run", "--version", "0.1.0a17")
            self.assertEqual(rc, 0, err)
            self.assertIn("release v0.1.0a17 (pep440)", out)
            rc, _, err = dev("release", "--dry-run", "--version", "0.1.0b1")
            self.assertEqual(rc, 2)
            self.assertIn("pyproject.toml says 0.1.0a17, not 0.1.0b1", err)
            rc, _, err = dev("release", "--dry-run", "--version", "0.1.0-alpha")
            self.assertEqual(rc, 2)
            self.assertIn("is not a PEP 440 version", err)

    def test_refusals(self):
        with Sandbox() as s:
            s.gh.on(["run", "list"], [])
            self.assertIn("no local CI evidence", dev("release")[2])
            dev("ci", "--mark-passed")
            s.gh.on(["run", "list"], [{"status": "completed", "conclusion": "failure", "workflowName": "CI"}])
            self.assertIn("CI failed", dev("release")[2])
            s.gh.on(["run", "list"], [{"status": "in_progress", "workflowName": "CI"}])
            self.assertIn("still running", dev("release")[2])
            s.gh.on(["run", "list"], [])
            s.write("README.md", "dirty\n")
            self.assertIn("uncommitted", dev("release")[2])
            git(s.repo, "checkout", "-q", "--", "README.md")
            s.commit("chore: keep a version file", {"VERSION": "1.4.0\n"})
            git(s.repo, "push", "-q", "origin", "main")
            rc, _, err = dev("release")
            self.assertEqual(rc, 2)
            self.assertIn("own version scheme", err)
            rc, _, err = dev("release", "--version", "1.5.0")
            self.assertIn("VERSION says 1.4.0", err)
            s.branch("feat/x")
            self.assertIn("releases are cut from `main`", dev("release")[2])

    def test_release_tags_the_version_in_the_projects_tag_format_and_its_release_workflow_runs_on_it(self):
        # dev release tagged v<version> whatever the format, so a repository whose tags have no v never started the
        # release workflow dev ci init generates for it, which runs on <version> tags only
        from devkit import pipelines
        cases = (("a policy without the v", {"versioning": "calver", "tag_format": "YY.MM.PATCH"}, (), "26.11.4"),
                 ("a policy with the v", {"versioning": "calver", "tag_format": "vYY.MM.PATCH"}, (), "v26.11.4"),
                 ("nothing recorded or tagged", None, (), "v26.11.4"),
                 ("no policy, tags without the v", None, ("26.11.3",), "26.11.4"))
        for name, policy, tags, want in cases:
            other = want[1:] if want.startswith("v") else "v" + want
            with self.subTest(name), Sandbox() as s:
                if policy:
                    f = delivery.POLICIES / "acme" / "widget.json"
                    f.parent.mkdir(parents=True)
                    f.write_text(json.dumps(policy))
                for t in tags:
                    git(s.repo, "tag", t)
                git(s.repo, "push", "-q", "origin", "--tags")
                dev("ci", "--mark-passed")
                s.gh.on(["run", "list"], [])
                s.gh.on(["release", "create"], f"https://github.com/acme/widget/releases/tag/{want}\n")
                rc, out, err = dev("release", "--version", "26.11.4")
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(s.head(f"{want}^{{commit}}", repo=s.origin), s.head())
                self.assertEqual(gitx._ref_sha(s.origin, f"refs/tags/{other}"), "")
                call = s.gh.called("release", "create")[0]
                self.assertEqual((call[call.index("create") + 1], call[call.index("--title") + 1]), (want, want))
                self.assertIn(f"released {want} ·", out)
                workflow = pipelines.generate(s.repo, "github", policy=policy)[0][".github/workflows/release.yml"]
                trigger = re.search(r'tags: \["([^"]+)"\]', workflow).group(1)
                self.assertTrue(pipelines.gh_glob(trigger, want), trigger)
                self.assertFalse(pipelines.gh_glob(trigger, other), trigger)
                rc, _, err = dev("release", "--version", "26.11.4", "--dry-run")
                self.assertEqual(rc, 2, err)
                self.assertIn(f"tag {want} already exists", err)

    def test_a_recorded_tag_format_decides_the_tag_over_the_tags_spelling_and_refuses_the_other_spelling(self):
        # the policy and the repository's tags disagree: the policy names the new tag, and a version the repository
        # already shipped in the other spelling is still refused, so it cannot ship twice
        for fmt, shipped, want, other in (("YY.MM.PATCH", "v26.11.3", "26.11.4", "v26.11.4"),
                                          ("vYY.MM.PATCH", "26.11.3", "v26.11.4", "26.11.4")):
            with self.subTest(fmt), Sandbox() as s:
                f = delivery.POLICIES / "acme" / "widget.json"
                f.parent.mkdir(parents=True)
                f.write_text(json.dumps({"versioning": "calver", "tag_format": fmt}))
                git(s.repo, "tag", shipped)
                git(s.repo, "push", "-q", "origin", "--tags")
                dev("ci", "--mark-passed")
                s.gh.on(["run", "list"], [])
                rc, out, err = dev("release", "--version", "26.11.4", "--dry-run")
                self.assertEqual(rc, 0, out + err)
                self.assertIn(f"dry run: release {want} (calver)", out)
                git(s.repo, "tag", other)
                git(s.repo, "push", "-q", "origin", f"refs/tags/{other}")
                git(s.repo, "tag", "-d", other)  # shipped from another clone: only origin has it
                rc, out, err = dev("release", "--version", "26.11.4", "--dry-run")
                self.assertEqual(rc, 2, out + err)
                self.assertIn(f"tag {other} already exists", err)

    def test_the_recorded_delivery_policy_wins(self):
        with Sandbox() as s:
            dev("ci", "--mark-passed")
            s.gh.on(["run", "list"], [])
            f = delivery.POLICIES / "acme" / "widget.json"
            f.parent.mkdir(parents=True)
            f.write_text(json.dumps({"versioning": "semver", "tag_format": "vMAJOR.MINOR.PATCH"}))
            rc, _, err = dev("release", "--dry-run")
            self.assertEqual(rc, 2)
            self.assertIn("own version scheme", err)
            self.assertEqual(dev("release", "--dry-run", "--version", "1.0.0")[0], 0)
            f.write_text(json.dumps({"versioning": "semver", "release_flow": "release-please"}))
            self.assertIn("release-please", dev("release", "--dry-run", "--version", "1.0.0")[2])
    def test_tags_pushes_and_publishes_with_changelog_notes(self):
        with Sandbox() as s:
            v = f"{YY}.{MM}.0"
            s.commit("chore(release): prepare " + v, {"VERSION": v + "\n", "CHANGELOG.md":
                                                     f"# Changelog\n\n## [{v}] - today\n\n- Add the widget\n\n"
                                                     f"## [26.1.0]\n\n- Old\n"})
            git(s.repo, "push", "-q", "origin", "main")
            dev("ci", "--mark-passed")
            s.gh.on(["run", "list"], [{"status": "completed", "conclusion": "success", "workflowName": "CI"}])
            s.gh.on(["release", "create"], f"https://github.com/acme/widget/releases/tag/v{v}\n")
            rc, out, err = dev("release")
            self.assertEqual(rc, 0, err)
            self.assertEqual(s.head(f"v{v}^{{commit}}", repo=s.origin), s.head())
            call = s.gh.called("release", "create")[0]
            self.assertEqual(call[call.index("--notes") + 1], "- Add the widget")
            self.assertIn("--verify-tag", call)
            self.assertIn("verify the artefact", out)

    def test_python_version_padding_keeps_the_requested_release_tag(self):
        for metadata, requested in (("26.11.8", "26.11.08"), ("26.1.8", "26.01.08"),
                                    ("26.01.08", "26.1.8")):
            with self.subTest(metadata=metadata, requested=requested), Sandbox() as s:
                s.commit("chore(release): prepare Python release", {
                    "pyproject.toml": f'[project]\nname = "widget"\nversion = "{metadata}"\n',
                    "CHANGELOG.md": f"# Changelog\n\n## [{requested}]\n\n- Publish the widget\n"})
                git(s.repo, "push", "-q", "origin", "main")
                dev("ci", "--mark-passed")
                s.gh.on(["run", "list"], [])
                s.gh.on(["release", "create"], f"https://github.com/acme/widget/releases/tag/v{requested}\n")
                rc, out, err = dev("release", "--version", requested, "--dry-run")
                self.assertEqual(rc, 0, err)
                self.assertIn(f"tag v{requested}", out)
                self.assertEqual(git(s.repo, "tag", "--list").split(), [])
                rc, _, err = dev("release", "--version", requested)
                self.assertEqual(rc, 0, err)
                self.assertEqual(git(s.origin, "tag", "--list").split(), [f"v{requested}"])
                self.assertEqual(s.head(f"v{requested}^{{commit}}", repo=s.origin), s.head())
                call = s.gh.called("release", "create")[0]
                self.assertEqual(call[2], f"v{requested}")
                self.assertEqual(call[call.index("--notes") + 1], "- Publish the widget")

    # `dev release 26.11.20` names the version as `--version 26.11.20` does, with or without its leading v; given both
    # ways, the release is refused and nothing is tagged
    def test_the_version_is_also_the_commands_word_and_is_given_once(self):
        with Sandbox() as s:
            v = f"{YY}.{MM}.4"
            s.commit("chore(release): prepare " + v, {"VERSION": v + "\n", "CHANGELOG.md":
                                                     f"# Changelog\n\n## [{v}]\n\n- Add the widget\n"})
            git(s.repo, "push", "-q", "origin", "main")
            dev("ci", "--mark-passed")
            s.gh.on(["run", "list"], [])
            for word in (v, "v" + v):
                with self.subTest(word=word):
                    rc, out, err = dev("release", word, "--dry-run")
                    self.assertEqual(rc, 0, err)
                    self.assertIn(f"tag v{v}", out)
            rc, _, err = dev("release", v, "--version", v, "--dry-run")
            self.assertEqual(rc, 2, err)
            self.assertIn("give the version once", err)
            self.assertEqual(git(s.repo, "tag", "--list").split(), [])

    def test_release_refuses_different_versions_and_non_python_padding(self):
        cases = [("pyproject.toml", f'[project]\nversion = "{version}"\n', version)
                 for version in ("26.11.9", "26.10.8", "25.11.8")]
        cases += [("VERSION", "26.11.8\n", "26.11.8"),
                  ("package.json", '{"version": "26.11.8"}\n', "26.11.8")]
        for filename, content, version in cases:
            with self.subTest(filename=filename, version=version), Sandbox() as s:
                s.commit("chore(release): prepare version", {filename: content})
                git(s.repo, "push", "-q", "origin", "main")
                for flags in (("--dry-run",), ()):
                    rc, _, err = dev("release", "--version", "26.11.08", *flags)
                    self.assertEqual(rc, 2, err)
                    self.assertIn(f"{filename} says {version}, not 26.11.08", err)
                self.assertEqual(git(s.repo, "tag", "--list").split(), [])
                self.assertEqual(git(s.origin, "tag", "--list").split(), [])
                self.assertEqual(s.gh.called("release", "create"), [])


class ReleaseAutomationTests(unittest.TestCase):
    def prepare(self, s):
        version = f"{YY}.{MM}.0"
        s.commit("chore(release): prepare the widget", {
            "VERSION": version + "\n",
            "CHANGELOG.md": f"# Changelog\n\n## [{version}]\n\n- Publish the widget\n"})
        git(s.repo, "push", "-q", "origin", "main")
        dev("ci", "--mark-passed")
        ci = {"databaseId": 749, "status": "completed", "conclusion": "success",
              "workflowName": "CI", "workflowDatabaseId": 7, "event": "push"}
        bot = {"databaseId": 43536834877, "status": "completed", "conclusion": "failure",
               "workflowName": "Dependabot Updates", "event": "dynamic"}
        detail = {"id": 43536834877, "event": "dynamic", "path": "dynamic/dependabot/dependabot-updates",
                  "actor": {"login": "dependabot[bot]"}, "repository": {"full_name": "acme/widget"},
                  "head_repository": {"full_name": "acme/widget"}, "head_sha": s.head()}
        s.gh.on(["run", "list"], [ci, bot])
        s.gh.on(["api", "repos/acme/widget/actions/runs/43536834877"], detail)
        s.gh.on(["release", "create"], f"https://github.com/acme/widget/releases/tag/v{version}\n")
        return version, ci, bot, detail

    def assert_no_release(self, s):
        self.assertEqual(git(s.repo, "tag", "--list").split(), [])
        self.assertEqual(git(s.origin, "tag", "--list").split(), [])
        self.assertEqual(s.gh.called("release", "create"), [])

    def test_release_ignores_verified_update_automation_without_using_its_name(self):
        for status in ("completed", "in_progress"):
            with self.subTest(status=status), Sandbox() as s:
                version, ci, bot, _ = self.prepare(s)
                bot.update(status=status, workflowName="Renamed dependency updater")
                s.gh.on(["run", "list"], [ci, bot])
                rc, out, err = dev("release", "--dry-run")
                self.assertEqual(rc, 0, err)
                self.assertIn("remote CI: passing", out)
                self.assert_no_release(s)
                rc, _, err = dev("release")
                self.assertEqual(rc, 0, err)
                self.assertEqual(s.head(f"v{version}^{{commit}}", repo=s.origin), s.head())
                call = s.gh.called("release", "create")[0]
                self.assertEqual(call[2], f"v{version}")
                self.assertEqual(call[call.index("--notes") + 1], "- Publish the widget")

    def test_release_keeps_unverified_or_spoofed_update_runs(self):
        with Sandbox() as s:
            _, ci, bot, detail = self.prepare(s)
            cases = [(key, {**detail, key: value}) for key, value in (
                ("id", 123), ("id", "43536834877"), ("event", "pull_request"),
                ("path", ".github/workflows/dependabot.yml"), ("actor", {"login": "someone"}),
                ("repository", {"full_name": "other/widget"}),
                ("head_repository", {"full_name": "fork/widget"}), ("head_sha", "0" * 40),
                ("actor", []), ("repository", None), ("head_repository", "acme/widget"))]
            cases += [("missing " + key, {k: v for k, v in detail.items() if k != key}) for key in detail]
            cases += [("null", None), ("array", []), ("malformed JSON", "{broken")]
            for label, response in cases:
                with self.subTest(label=label):
                    s.gh.on(["api", "repos/acme/widget/actions/runs/43536834877"], response)
                    for flags in (("--dry-run",), ()):
                        rc, _, err = dev("release", *flags)
                        self.assertEqual(rc, 2, err)
                        self.assertIn("CI failed", err)
                    self.assert_no_release(s)
            for response in ("", detail):
                with self.subTest(api_error=response):
                    s.gh.on(["api", "repos/acme/widget/actions/runs/43536834877"], response, rc=1,
                            err="API unavailable")
                    self.assertEqual(dev("release")[0], 2)
                    self.assert_no_release(s)
            s.gh.on(["api", "repos/acme/widget/actions/runs/43536834877"], detail)
            for event in ("push", "pull_request", None):
                with self.subTest(list_event=event):
                    s.gh.on(["run", "list"], [ci, {**bot, "event": event}])
                    self.assertEqual(dev("release")[0], 2)
                    self.assert_no_release(s)
            for rid in (None, 0, True, "43536834877", "../749"):
                with self.subTest(list_id=rid):
                    s.gh.on(["run", "list"], [ci, {**bot, "databaseId": rid}])
                    self.assertEqual(dev("release")[0], 2)
                    self.assert_no_release(s)

    def test_verified_update_automation_cannot_replace_passing_repository_ci(self):
        with Sandbox() as s:
            _, ci, bot, _ = self.prepare(s)
            for event in ("push", "pull_request", "dynamic"):
                with self.subTest(event=event):
                    s.gh.on(["run", "list"], [{**ci, "event": event, "conclusion": "failure",
                                              "workflowName": "Dependabot Updates"}, bot])
                    rc, _, err = dev("release")
                    self.assertEqual(rc, 2, err)
                    self.assertIn("CI failed", err)
                    self.assert_no_release(s)
            s.gh.on(["run", "list"], [{**ci, "status": "in_progress", "conclusion": ""}, bot])
            rc, _, err = dev("release")
            self.assertEqual(rc, 2, err)
            self.assertIn("still running", err)
            self.assert_no_release(s)

    def test_a_twin_still_needs_real_passing_pr_jobs_after_excluding_the_updater(self):
        with Sandbox() as s:
            version, ci, bot, _ = self.prepare(s)
            twin = s.head()
            git(s.repo, "commit", "-q", "--allow-empty", "-m", "chore: merge the verified tree")
            git(s.repo, "push", "-q", "origin", "main")
            dev("ci", "--mark-passed")
            s.gh.on(["run", "list"], [{**ci, "status": "in_progress", "conclusion": ""}],
                    when=lambda args: s.head() in args)
            s.gh.on(["run", "view", "749"], {"jobs": [{"name": "test", "status": "in_progress"}]})
            s.gh.on(["run", "list"], [bot], when=lambda args: twin in args)
            rc, _, err = dev("release")
            self.assertEqual(rc, 2, err)
            self.assertIn("still running", err)
            self.assert_no_release(s)
            s.gh.on(["run", "list"], [{**ci, "databaseId": 750, "event": "pull_request"}, bot],
                    when=lambda args: twin in args)
            s.gh.on(["api", "repos/acme/widget/actions/runs/750"],
                    {"id": 750, "repository": {"id": 1}, "head_repository": {"id": 1}})
            s.gh.on(["run", "view", "750"],
                    {"jobs": [{"name": "test", "status": "completed", "conclusion": "success"}]})
            rc, out, err = dev("release")
            self.assertEqual(rc, 0, err)
            self.assertIn(f"tree passed the pull request run on {twin[:7]}", out)
            self.assertEqual(s.head(f"v{version}^{{commit}}", repo=s.origin), s.head())


class ReconcileTests(unittest.TestCase):
    def test_only_merges_that_reached_the_default_branch_on_origin_count(self):
        with Sandbox() as s:
            prs = []
            for n, branch in ((7, "feat/invoice"), (8, "feat/local")):
                s.branch(branch)
                s.commit(f"feat: work on {branch}")
                git(s.repo, "switch", "-q", "main")
                git(s.repo, "merge", "-q", "--no-ff", branch, "-m", f"Merge pull request #{n} from acme/{branch}")
                prs.append(merged_pr(n, branch, s.head()))
                if branch == "feat/invoice":
                    git(s.repo, "push", "-q", "origin", "main")
            s.gh.on(["pr", "list"], prs, when=lambda args: "merged" in args)
            merged = gitx.merged_into_default(s.repo, "main")
            self.assertEqual(list(merged), ["feat/invoice"])
            self.assertEqual((merged["feat/invoice"]["pr"], merged["feat/invoice"]["sha"]),
                             (7, s.head("origin/main")))

    # A squash or rebase merge on GitHub leaves no merge commit that names the branch, and neither does a fast-forward:
    # their items stayed open until someone closed them by hand.
    def test_task_reconcile_closes_the_items_of_a_branch_merged_without_a_merge_commit(self):
        from devkit import cli
        for how in ("squash merge (the PR says merged)", "rebase merge (the PR's merge commit is the branch's tip)"):
            with self.subTest(how), Sandbox() as s:
                it = work.claim(work.create("acme/widget", "Export invoices", a=ME), a=ME, branch="feat/invoice")
                s.branch("feat/invoice")
                tip = s.commit("feat(invoice): add csv export")
                git(s.repo, "switch", "-q", "main")
                if how.startswith("squash"):
                    git(s.repo, "merge", "-q", "--squash", "feat/invoice")
                    git(s.repo, "commit", "-q", "-m", "feat(invoice): add csv export (#7)")
                    landed = s.head()
                else:
                    git(s.repo, "merge", "-q", "--ff-only", "feat/invoice")
                    landed = tip
                pr = 7
                s.gh.on(["pr", "list"], [merged_pr(7, "feat/invoice", landed, work.now_iso()),
                                         merged_pr(3, "feat/other", "0" * 40, "2026-01-10T09:00:00Z")],
                        when=lambda args: "merged" in args)
                git(s.repo, "push", "-q", "origin", "main")
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = cli._task_reconcile(argparse.Namespace(apply=True, json=True), "acme/widget", "acme/widget")
                self.assertEqual(rc, 0)
                self.assertEqual(json.loads(out.getvalue())["changed"], [it["meta"]["id"]])
                after = work.load(it["path"])
                self.assertEqual(after["meta"]["status"], "done")
                self.assertIn(f"feat/invoice merged in {landed[:7]}" + (f" (PR #{pr})" if pr else ""),
                              after["changelog"][0])

    def test_task_reconcile_apply_closes_the_items_of_a_branch_merged_on_origin(self):
        from devkit import cli
        with Sandbox() as s:
            it = work.claim(work.create("acme/widget", "Export invoices", a=ME), a=ME, branch="feat/invoice")
            s.branch("feat/invoice")
            s.commit("feat(invoice): add csv export")
            git(s.repo, "switch", "-q", "main")
            git(s.repo, "merge", "-q", "--no-ff", "feat/invoice", "-m", "Merge pull request #7 from acme/feat/invoice")
            git(s.repo, "push", "-q", "origin", "main")
            s.gh.on(["pr", "list"], [merged_pr(7, "feat/invoice", s.head(), work.now_iso())],
                    when=lambda args: "merged" in args)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cli._task_reconcile(argparse.Namespace(apply=True, json=True), "acme/widget", "acme/widget")
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out.getvalue())["changed"], [it["meta"]["id"]])
            after = work.load(it["path"])
            self.assertEqual(after["meta"]["status"], "done")
            self.assertIn(f"feat/invoice merged in {s.head('origin/main')[:7]} (PR #7)", after["changelog"][0])


def merged_pr(n, branch, merge_sha, merged_at="2026-11-07T10:00:00Z", cross=False):
    return {"number": n, "state": "MERGED", "headRefName": branch, "mergeCommit": {"oid": merge_sha},
            "mergedAt": merged_at, "isCrossRepository": cross, "baseRefName": "main"}


class ForgedMergeTests(unittest.TestCase):
    # A merge commit's subject and date are whatever its author wrote. Inside its own branch an author could forge
    # "Merge branch 'fix/victim-login'" dated 2035, merge its branch into main, and close someone else's work items.
    # Only first-parent merges of the default branch count; on GitHub only with their merged pull request from this
    # repository (its merge commit, its mergedAt); and a commit date in the future counts for nothing.
    def forge(self, s, date="2035-01-01T00:00:00Z"):
        s.branch("feat/attacker")
        s.commit("feat: add a harmless change")
        side = git(s.repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "side")
        forged = git(s.repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-p", side, "-m",
                     "Merge branch 'fix/victim-login'", env=dict(core.GIT_ENV, GIT_COMMITTER_DATE=date))
        git(s.repo, "reset", "-q", "--hard", forged)
        git(s.repo, "switch", "-q", "main")
        git(s.repo, "merge", "-q", "--no-ff", "feat/attacker", "-m", "Merge pull request #12 from acme/feat/attacker")
        git(s.repo, "push", "-q", "origin", "main")
        return s.head()

    def off_github(self, s):
        url = "https://gitlab.example.com/acme/widget.git"
        git(s.repo, "remote", "set-url", "origin", url)
        git(s.repo, "config", f"url.{s.origin}.insteadOf", url)

    def test_a_merge_inside_a_merged_branch_does_not_count_for_the_branch_it_names(self):
        with Sandbox() as s:
            merge = self.forge(s)
            s.gh.on(["pr", "list"], [merged_pr(12, "feat/attacker", merge)], when=lambda args: "merged" in args)
            merged = gitx.merged_into_default(s.repo, "main", ["fix/victim-login"])
            self.assertEqual(list(merged), ["feat/attacker"])

    def test_off_github_a_merge_inside_a_merged_branch_does_not_count_either(self):
        with Sandbox() as s:
            self.off_github(s)
            self.forge(s)
            self.assertEqual(list(gitx.merged_into_default(s.repo, "main", ["fix/victim-login"])), ["feat/attacker"])

    def test_on_github_a_merge_commit_counts_only_with_its_merged_pull_request(self):
        with Sandbox() as s:
            s.branch("feat/invoice")
            s.commit("feat(invoice): add csv export")
            git(s.repo, "switch", "-q", "main")
            git(s.repo, "merge", "-q", "--no-ff", "feat/invoice", "-m", "Merge pull request #7 from acme/feat/invoice")
            git(s.repo, "push", "-q", "origin", "main")
            self.assertEqual(gitx.merged_into_default(s.repo, "main", ["feat/invoice"]), {})
            s.gh.on(["pr", "list"], [merged_pr(7, "feat/invoice", s.head(), "2026-11-07T10:00:00Z")],
                    when=lambda args: "merged" in args)
            got = gitx.merged_into_default(s.repo, "main", ["feat/invoice"])
            self.assertEqual((got["feat/invoice"]["pr"], got["feat/invoice"]["at"]),
                             (7, work.parse_iso("2026-11-07T10:00:00Z")))

    def test_a_pull_request_from_another_repository_does_not_count(self):
        with Sandbox() as s:
            s.branch("fix/victim-login")
            s.commit("fix: work of a fork with the victim's branch name")
            git(s.repo, "switch", "-q", "main")
            git(s.repo, "merge", "-q", "--no-ff", "fix/victim-login", "-m",
                "Merge pull request #13 from mallory/fix/victim-login")
            git(s.repo, "push", "-q", "origin", "main")
            git(s.repo, "branch", "-q", "-D", "fix/victim-login")
            s.gh.on(["pr", "list"], [merged_pr(13, "fix/victim-login", s.head(), cross=True)],
                    when=lambda args: "merged" in args)
            self.assertEqual(gitx.merged_into_default(s.repo, "main", ["fix/victim-login"]), {})

    def test_on_github_a_branch_pointing_into_main_does_not_count_without_a_pull_request(self):
        with Sandbox() as s:
            git(s.repo, "push", "-q", "origin", "main:refs/heads/fix/victim-login")
            git(s.repo, "fetch", "-q", "origin")
            self.assertEqual(gitx.merged_into_default(s.repo, "main", ["fix/victim-login"]), {})

    def test_off_github_a_merge_dated_in_the_future_does_not_count(self):
        with Sandbox() as s:
            self.off_github(s)
            s.branch("feat/invoice")
            s.commit("feat(invoice): add csv export")
            git(s.repo, "switch", "-q", "main")
            future = dict(core.GIT_ENV, GIT_COMMITTER_DATE="2035-01-01T00:00:00Z")
            git(s.repo, "merge", "-q", "--no-ff", "feat/invoice", "-m", "Merge branch 'feat/invoice'", env=future)
            git(s.repo, "push", "-q", "origin", "main")
            git(s.repo, "branch", "-q", "-D", "feat/invoice")
            self.assertEqual(gitx.merged_into_default(s.repo, "main", ["feat/invoice"]), {})

    def test_off_github_a_fast_forward_counts_by_the_branch_tip(self):
        with Sandbox() as s:
            self.off_github(s)
            s.branch("feat/invoice")
            tip = s.commit("feat(invoice): add csv export")
            git(s.repo, "switch", "-q", "main")
            git(s.repo, "merge", "-q", "--ff-only", "feat/invoice")
            git(s.repo, "push", "-q", "origin", "main")
            self.assertEqual(gitx.merged_into_default(s.repo, "main", ["feat/invoice"])["feat/invoice"]["sha"], tip)


# --------------------------------------------------------------------------- dev issue


class IssueTests(unittest.TestCase):
    def test_ls_and_create(self):
        with Sandbox() as s:
            s.gh.on(["issue", "list"], [{"number": 3, "title": "Crash on empty CSV", "labels": [{"name": "bug"}],
                                         "assignees": [], "updatedAt": "2026-11-05T10:00:00Z"}])
            rc, out, _ = dev("issue", "ls")
            self.assertIn("#3", out)
            self.assertIn("bug", out)
            s.gh.on(["issue", "create"], "https://github.com/acme/widget/issues/4\n")
            rc, out, _ = dev("issue", "create", "--title", "Export fails", "--body",
                             "Steps.\nGenerated with Claude Code")
            self.assertEqual(rc, 0)
            call = s.gh.called("issue", "create")[0]
            self.assertEqual(call[call.index("--body") + 1], "Steps.")
            self.assertIn("issue #4", out)


# --------------------------------------------------------------------------- dev git … · dev gh …


class PassthroughTests(unittest.TestCase):
    def test_git_stash_and_force_push_refused(self):
        with Sandbox() as s:
            s.branch("feat/p")
            for argv in (["git", "stash"], ["git", "stash", "pop"], ["git", "push", "--force"],
                         ["git", "push", "origin", "+feat/p"]):
                rc, _, err = dev(*argv)
                self.assertEqual(rc, 2, argv)
            self.assertEqual(dev("git", "stash", "list")[0], 0)
            self.assertEqual(s.execs[-1][0], ["git", "stash", "list"])

    def test_git_push_gets_the_push_rules(self):
        with Sandbox() as s:
            s.branch("feat/p")
            s.commit("feat: add p")
            rc, _, err = dev("git", "push", "origin", "feat/p")
            self.assertEqual(rc, 2)
            self.assertIn("no local CI evidence", err)
            dev("ci", "--mark-passed")
            self.assertEqual(dev("git", "push", "-u", "origin", "feat/p")[0], 0)
            self.assertEqual(s.execs[-1][0], ["git", "push", "-u", "origin", "feat/p"])
            rc, _, err = dev("git", "push", "origin", "HEAD:main")
            self.assertEqual(rc, 2)
            self.assertIn("default branch", err)

    def test_git_commit_gets_the_commit_rules(self):
        with Sandbox() as s:
            s.branch("feat/p")
            s.write("p.txt", "p")
            git(s.repo, "add", "p.txt")
            self.assertEqual(dev("git", "commit", "-m", "Added p")[0], 2)
            self.assertEqual(dev("git", "commit", "-m", "feat: x\n\nCo-Authored-By: Claude <noreply@anthropic.com>")[0], 2)
            self.assertEqual(dev("git", "commit", "-m", "feat: add p")[0], 0)
            self.assertEqual(s.execs[-1][0][:2], ["git", "commit"])

    def test_git_dash_c_and_plain_commands(self):
        with Sandbox() as s:
            os.chdir(s.root)
            self.assertEqual(dev("git", "-C", s.repo, "status", "-s")[0], 0)
            self.assertEqual(s.execs[-1], (["git", "status", "-s"], str(s.repo)))

    def test_gh_policies(self):
        with Sandbox() as s:
            s.branch("feat/invoice")
            self.assertEqual(dev("gh", "pr", "comment", "7", "--body", "🤖 Generated with [Claude Code](x)")[0], 2)
            self.assertEqual(dev("gh", "pr", "merge", "7", "--admin")[0], 2)
            s.gh.on(["pr", "view"], _pr(statusCheckRollup=[{"conclusion": "FAILURE"}]))
            rc, _, err = dev("gh", "pr", "merge", "7", "--squash")
            self.assertEqual(rc, 2)
            self.assertIn("checks are failing", err)
            rc, _, err = dev("gh", "pr", "create", "--title", "x", "--body", "y")
            self.assertEqual(rc, 2)
            self.assertEqual(s.execs, [])
            self.assertEqual(dev("gh", "pr", "list")[0], 0)
            self.assertEqual(s.execs[-1][0], ["gh", "pr", "list"])

    def test_gh_pr_create_opens_a_draft_without_evidence_where_drafts_run_no_ci(self):
        with Sandbox() as s:
            s.branch("feat/invoice")
            s.commit("feat(invoice): add csv export")
            rc, _, err = dev("gh", "pr", "create", "--draft", "--title", "feat(invoice): add csv export", "--body", "y")
            self.assertEqual(rc, 0, err)
            self.assertEqual(s.execs[-1][0][:4], ["gh", "pr", "create", "--draft"])

    def test_gh_release_create_follows_the_recorded_tag_format(self):
        with Sandbox() as s:
            dev("ci", "--mark-passed")
            f = delivery.POLICIES / "acme" / "widget.json"
            f.parent.mkdir(parents=True)
            f.write_text(json.dumps({"versioning": "semver", "tag_format": "vMAJOR.MINOR.PATCH"}))
            rc, _, err = dev("gh", "release", "create", "release-1", "--notes", "x")
            self.assertEqual(rc, 2)
            self.assertIn("`release-1` is not a SemVer tag", err)
            self.assertEqual(s.execs, [])
            self.assertEqual(dev("gh", "release", "create", "v1.0.0", "--notes", "x")[0], 0)
            self.assertEqual(s.execs[-1][0][:4], ["gh", "release", "create", "v1.0.0"])

    def test_dev_gh_runs_a_command_devkit_has_no_rule_for_as_typed(self):
        # a re-run of CI is gh's own: dev gh passes it through (dev run rerun --flaky is devkit's way, with a reason)
        with Sandbox() as s:
            s.branch("feat/rerun")
            rc, _, err = dev("gh", "run", "rerun", "5", "--failed")
            self.assertEqual(rc, 0, err)
            self.assertEqual(s.execs[-1][0], ["gh", "run", "rerun", "5", "--failed"])



class GitPushFlagTests(unittest.TestCase):
    # `dev git push` with the flags that push more than one ref, or delete one: each ref it would send gets the rules
    # of a plain push (no force, never the default branch, the embargoes, local CI evidence, the tag format)

    def push(self, s, *args, embargoed=()):
        held = {b: {"why": "discloses an open hole", "until": "", "who": "", "when": "", "commits": []}
                for b in embargoed}
        err = io.StringIO()
        with mock.patch("devkit.branches.read_embargoes", return_value=(held, "")), contextlib.redirect_stderr(err):
            rc = gitx.git_push_policy(list(args), str(s.repo))
        return rc, err.getvalue()

    def test_a_mirror_push_is_a_force_push(self):
        with Sandbox() as s:
            s.branch("feat/m")
            dev("ci", "--mark-passed")
            rc, err = self.push(s, "--mirror", "origin")
            self.assertEqual(rc, 2)
            self.assertIn("--mirror", err)
            self.assertIn("force", err)

    def test_pushing_every_branch_checks_each_local_branch_against_the_embargoes(self):
        with Sandbox() as s:
            s.branch("feat/secret")
            s.commit("fix: close the parser hole")
            git(s.repo, "switch", "-q", "main")
            s.branch("feat/x")
            dev("ci", "--mark-passed")
            for flag in ("--all", "--branches"):
                with self.subTest(flag=flag):
                    rc, err = self.push(s, flag, "origin", embargoed=["feat/secret"])
                    self.assertEqual(rc, 2)
                    self.assertIn("`feat/secret` is embargoed", err)

    def test_pushing_every_branch_needs_evidence_only_for_the_branches_it_moves(self):
        with Sandbox() as s:
            s.branch("feat/a")
            s.commit("feat: add a")
            dev("ci", "--mark-passed")
            self.assertIsNone(self.push(s, "--all", "origin")[0])  # main is where origin/main is: nothing to check
            s.branch("feat/b")
            s.commit("feat: add b")
            rc, err = self.push(s, "--all", "origin")
            self.assertEqual(rc, 2)
            self.assertIn("no local CI evidence", err)

    def test_pushing_every_branch_never_moves_the_default_branch(self):
        with Sandbox() as s:
            s.commit("fix: straight on main")
            s.branch("feat/a")
            dev("ci", "--mark-passed")
            rc, err = self.push(s, "--all", "origin")
            self.assertEqual(rc, 2)
            self.assertIn("`main` is the default branch", err)

    def test_deleting_the_default_branch_is_refused(self):
        with Sandbox() as s:
            s.branch("feat/d")
            for args in (["origin", ":main"], ["origin", ":refs/heads/main"], ["origin", "--delete", "main"],
                         ["-d", "origin", "main"], ["--delete", "origin", "refs/heads/main"]):
                with self.subTest(args=args):
                    rc, err = self.push(s, *args)
                    self.assertEqual(rc, 2)
                    self.assertIn("`main` is the default branch", err)
            self.assertIsNone(self.push(s, "origin", "--delete", "feat/old")[0])

    def test_pushing_tags_follows_the_recorded_tag_format(self):
        with Sandbox() as s:
            f = delivery.POLICIES / "acme" / "widget.json"
            f.parent.mkdir(parents=True)
            f.write_text(json.dumps({"versioning": "semver", "tag_format": "vMAJOR.MINOR.PATCH"}))
            git(s.repo, "tag", "release-0")
            git(s.repo, "push", "-q", "origin", "refs/tags/release-0")  # on the remote before the policy: not sent
            git(s.repo, "tag", "v1.0.0")
            self.assertIsNone(self.push(s, "--tags", "origin")[0])
            git(s.repo, "tag", "release-1")
            for args in (["--tags", "origin"], ["origin", "refs/tags/release-1"], ["origin", "release-1"]):
                with self.subTest(args=args):
                    rc, err = self.push(s, *args)
                    self.assertEqual(rc, 2)
                    self.assertIn("`release-1` is not a SemVer tag", err)

    def test_follow_tags_follows_the_recorded_tag_format(self):
        # --follow-tags sends the branch and the local tags the remote lacks along with it
        with Sandbox() as s:
            f = delivery.POLICIES / "acme" / "widget.json"
            f.parent.mkdir(parents=True)
            f.write_text(json.dumps({"versioning": "semver", "tag_format": "vMAJOR.MINOR.PATCH"}))
            s.branch("feat/t")
            s.commit("feat: add t")
            dev("ci", "--mark-passed")
            git(s.repo, "tag", "-a", "v1.0.0", "-m", "v1.0.0")
            self.assertIsNone(self.push(s, "--follow-tags", "origin", "feat/t")[0])
            git(s.repo, "tag", "-a", "release-1", "-m", "release 1")
            rc, err = self.push(s, "--follow-tags", "origin", "feat/t")
            self.assertEqual(rc, 2)
            self.assertIn("`release-1` is not a SemVer tag", err)

    def test_a_long_option_git_would_expand_is_refused_with_its_full_name(self):
        # git takes a unique prefix of a long option as the option: the push rules read the options spelled out
        with Sandbox() as s:
            s.branch("feat/p")
            dev("ci", "--mark-passed")
            for short, full in (("--mirr", "--mirror"), ("--al", "--all"), ("--branc", "--branches"),
                                ("--del", "--delete"), ("--ta", "--tags"), ("--pru", "--prune"),
                                ("--follow-t", "--follow-tags"), ("--force-w=main", "--force-with-lease")):
                with self.subTest(option=short):
                    rc, err = self.push(s, short, "origin", "feat/p")
                    self.assertEqual(rc, 2)
                    self.assertIn(f"`{full}`", err)
            self.assertIsNone(self.push(s, "--no-verify", "origin", "feat/p")[0])  # spelled out in full

    def test_a_refspec_with_a_wildcard_is_refused(self):
        with Sandbox() as s:
            s.branch("feat/w")
            dev("ci", "--mark-passed")
            for spec in ("refs/heads/*:refs/heads/*", "feat/*", "refs/tags/*"):
                with self.subTest(spec=spec):
                    rc, err = self.push(s, "origin", spec)
                    self.assertEqual(rc, 2)
                    self.assertIn(f"`{spec}`", err)


class ProcessEnvironmentTests(unittest.TestCase):
    # `dev git commit` and `dev git push` take nothing from their own environment that would skip a check: a variable
    # there came from wherever it was set, not from the command

    def setUp(self):
        p = mock.patch("devkit.security.staged_secrets", return_value=["src/a.py:1 generic-api-key"])
        p.start()
        self.addCleanup(p.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _branch_repo(self, branch):
        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()
        for args in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "init"],
                     ["checkout", "-q", "-b", branch]):
            git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", *args)
        return str(repo)

    def test_dev_git_commit_scans_for_secrets_whatever_its_environment_says(self):
        repo, err = self._branch_repo("fix/x"), io.StringIO()
        with mock.patch.dict(os.environ, {"DEVKIT_SKIP_SECRET_SCAN": "1"}), contextlib.redirect_stderr(err):
            rc = gitx.git_commit_policy(["-m", "fix: x"], repo)
        self.assertEqual(rc, 2)
        self.assertIn("gitleaks found", err.getvalue())

    def test_dev_git_push_never_takes_a_local_ci_run_from_its_environment(self):
        repo, err = self._branch_repo("feat/x"), io.StringIO()
        handoff = Path(self.tmp.name) / "handoff"
        handoff.mkdir()  # this Mac has its handoff repository: dev push reads its embargoes there first
        with mock.patch.dict(os.environ, {"DEVKIT_LOCAL_CI_PASSED": "1"}), \
                mock.patch.object(core, "STATE", Path(self.tmp.name) / "state"), \
                mock.patch.object(core, "HANDOFF", handoff), contextlib.redirect_stderr(err):
            rc = gitx.git_push_policy(["origin", "feat/x"], repo)
        self.assertEqual(rc, 2)
        self.assertIn("no local CI evidence", err.getvalue())


class SpanishTemplateTests(unittest.TestCase):
    def test_spanish_template_sections_are_required(self):
        tpl = (Path(__file__).resolve().parents[1] / "templates" / "github" / "pull_request.es.md").read_text()
        kinds = [k for k, _, _, _ in gitx.sections(tpl)]
        self.assertEqual(kinds, ["what", "changes", "verify", "risk", "links"])
        self.assertEqual(gitx.body_problems(tpl, tpl), ["Qué y por qué", "Cómo verificarlo"])
        filled = tpl.replace("## Qué y por qué\n", "## Qué y por qué\n\nExporta facturas a CSV para Finanzas.\n", 1)
        filled = filled.replace("- CI local: `<comando>`", "- CI local: `./mvnw verify` → 214 tests pasan\n- x", 1)
        self.assertEqual(gitx.body_problems(filled, tpl), [])


# --------------------------------------------------------------------------- dev release's session claim

class ReleaseClaimTests(unittest.TestCase):
    # dev release holds release:<project> for its session while it runs (peers.claim), so two sessions on this Mac
    # never tag one project at once, and gives it back whatever the release did. Another session's claim stops an
    # agent's release, which is told who holds it and how to break a stale claim; the developer's own terminal and a
    # dry run are only told.
    REL = "release:acme/api"

    def setUp(self):
        from test_peers import PeerCase
        self.peers = PeerCase("three_sessions")
        self.peers.setUp()
        self.addCleanup(self.peers.doCleanups)
        self.a, self.b, self.c = self.peers.three_sessions()

    def run_release(self, sp, body, dry_run=False):
        # cmd_release around its body: the checkout, the speaker and the release itself are the test's
        from types import SimpleNamespace
        from devkit import identity
        c = SimpleNamespace(pid="acme/api", top=str(self.peers.root), branch="main", default="main")
        a = SimpleNamespace(dry_run=dry_run, version=None, why=None)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(gitx, "ctx", lambda: c), mock.patch.object(identity, "speaker", lambda *x, **k: sp), \
                mock.patch.object(gitx, "_release", body), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = gitx.cmd_release(a)
        return rc, out.getvalue() + err.getvalue()

    def test_dev_release_holds_its_sessions_claim_while_it_runs_and_gives_it_back(self):
        from devkit import identity, peers
        from test_peers import CODEX_A, CLAUDE_A
        main = identity.Speaker("claude", CLAUDE_A, "main", "stamped", True)
        held = []

        def failing(a, c):
            held.append(peers.holder(self.REL, me=self.b))  # held for the session while the release runs
            return 1
        self.assertEqual(self.run_release(main, failing)[0], 1)
        self.assertEqual((held[0]["key"], held[0]["why"]), (self.a, "dev release runs"))
        self.assertIsNone(peers.holder(self.REL))
        # a crew member of a Codex session releases: devkit holds the claim for its session, and gives it back
        member = identity.Speaker("codex", CODEX_A, "a_link", "payload", True)
        held.clear()
        self.assertEqual(self.run_release(member, lambda a, c: held.append(peers.holder(self.REL, me=self.a)) or 0)[0],
                         0)
        self.assertEqual(held[0]["key"], self.b)
        self.assertIsNone(peers.holder(self.REL))

    def test_another_sessions_claim_stops_an_agents_release_and_names_how_to_break_it(self):
        # two sessions never tag one project at once; any session can break a claim it knows is stale
        from devkit import identity, peers
        from test_peers import CLAUDE_A, CLAUDE_B, CODEX_A, sender
        peers.claim(sender(CLAUDE_B), self.REL, "their release", minutes=30)
        ran = []
        for sp in (identity.Speaker("claude", CLAUDE_A, "main", "stamped", True),
                   identity.Speaker("codex", CODEX_A, "a_link", "payload", True)):
            with self.subTest(speaker=sp.agent):
                rc, text = self.run_release(sp, lambda a, c: ran.append(1) or 0)
                self.assertEqual((rc, ran), (3, []))
                self.assertIn(f"{self.REL} is claimed by session claude:7be0a41d", text)
                self.assertIn(f"dev session release {self.REL}", text)
                self.assertEqual(peers.holder(self.REL)["key"], self.c)  # their claim stays theirs
        # an agent's dry run still runs, and says the real release would be refused
        main = identity.Speaker("claude", CLAUDE_A, "main", "stamped", True)
        rc, text = self.run_release(main, lambda a, c: ran.append(2) or 0, dry_run=True)
        self.assertEqual((rc, ran), (0, [2]))
        self.assertIn("dev release refuses until it is free", text)

    def test_a_crash_gives_the_claim_back_a_claim_the_session_held_stays_and_a_dry_run_takes_none(self):
        from devkit import identity, peers
        from test_peers import CLAUDE_A, CLAUDE_B, sender
        main = identity.Speaker("claude", CLAUDE_A, "main", "stamped", True)

        def crashing(a, c):
            raise RuntimeError("gh release create crashed")
        with self.assertRaises(RuntimeError):
            self.run_release(main, crashing)
        self.assertIsNone(peers.holder(self.REL))
        mine = peers.claim(sender(CLAUDE_A), self.REL, "the whole release train", minutes=120)
        self.assertEqual(self.run_release(main, lambda a, c: 0)[0], 0)
        h = peers.holder(self.REL)
        self.assertEqual((h["key"], h["why"], h["until"]), (self.a, "the whole release train", mine["until"]))
        peers.release(sender(CLAUDE_A), self.REL)
        held = []
        self.run_release(main, lambda a, c: held.append(peers.holder(self.REL, me=self.b)) or 0, dry_run=True)
        self.assertEqual(held, [None])
        # the developer's own terminal takes no claim either, and is told who holds one
        peers.claim(sender(CLAUDE_B), self.REL, "their release", minutes=30)
        human = identity.Speaker("human", "shell-4242", "", "human", True)
        rc, text = self.run_release(human, lambda a, c: 0)
        self.assertEqual(rc, 0)
        self.assertIn(f"{self.REL} is claimed by session claude:7be0a41d", text)
        self.assertEqual(peers.holder(self.REL)["key"], self.c)


if __name__ == "__main__":
    unittest.main()
