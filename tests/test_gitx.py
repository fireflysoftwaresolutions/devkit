# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""`dev branch|commit|rebase|push|pr|run|release|issue|git|gh` and the local-CI evidence.

Offline: real temporary repositories with a bare "origin" (the GitHub URL is rewritten to it with insteadOf), and
`gh` replaced by canned JSON. Run: python3 -m unittest tests.test_gitx -v
"""
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import cistatus, core, delivery, gitx, guard  # noqa: E402

GITHUB_URL = "https://github.com/acme/widget.git"
ISOLATE = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Ana Dev",
           "GIT_AUTHOR_EMAIL": "ana@example.com", "GIT_COMMITTER_NAME": "Ana Dev",
           "GIT_COMMITTER_EMAIL": "ana@example.com"}
YY, MM = time.localtime().tm_year % 100, time.localtime().tm_mon
REAL_TEMPLATE_DIR = gitx.TEMPLATE_DIR


class FakeGH:
    """Canned `gh`: the newest matching route wins; every call is recorded."""

    def __init__(self):
        self.calls, self.routes = [], []

    def on(self, prefix, out="", rc=0, err="", when=None):
        self.routes.insert(0, (list(prefix), rc, out if isinstance(out, str) else json.dumps(out), err, when))

    def __call__(self, args, cwd=None, timeout=60):
        args = list(args)
        self.calls.append(args)
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


class Sandbox:
    """A repository `widget` whose origin is a bare repo, devkit state in a temp dir, gh faked."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.saved_git_env = {k: core.GIT_ENV.get(k) for k in ISOLATE}
        core.GIT_ENV.update(ISOLATE)
        self.saved_os = {k: os.environ.pop(k, None) for k in gitx.ACK_VARS + ("DEVKIT_AGENT",)}
        self.saved = (core.STATE, core.HANDOFF, gitx.GH, gitx.EXEC, gitx.SLEEP, gitx._me, gitx._handoff_log,
                      gitx.TEMPLATE_DIR, cistatus.runs_for, delivery.POLICIES)
        core.STATE = self.root / "state"
        delivery.POLICIES = self.root / "handoff" / "policies"
        core.HANDOFF = self.root / "handoff"
        self.gh = gitx.GH = FakeGH()
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
         cistatus.runs_for, delivery.POLICIES) = self.saved
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
        self.assertIsNotNone(guard.branch_violation(gitx.branch_name("claude/fix-it")))

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
            tip = s.commit("feat: add squashed thing")
            git(s.repo, "switch", "-q", "main")
            git(s.repo, "branch", "feat/in-wt", "feat/merged")
            wt = s.root / "wt"
            git(s.repo, "worktree", "add", "-q", str(wt), "feat/in-wt")
            s.gh.on(["pr", "list"], [{"number": 9, "state": "MERGED", "headRefName": "feat/squashed",
                                      "headRefOid": tip, "updatedAt": "2026-11-01T10:00:00Z"}])
            s.branch("feat/current")
            git(s.repo, "fetch", "-q", "--prune", "origin")
            rc, out, _ = dev("branch", "ls", "--json")
            rows = {r["name"]: r for r in json.loads(out)}
            self.assertTrue(rows["feat/merged"]["merged"])
            self.assertFalse(rows["feat/open"]["merged"])
            self.assertTrue(rows["feat/gone"]["gone"])
            self.assertEqual(rows["feat/squashed"]["pr"]["state"], "merged")
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
            rc, _, err = dev("commit", "-m", "fix: keep a", "a.txt")
            self.assertEqual(rc, 0, err)
            self.assertEqual(git(s.repo, "show", "--name-only", "--format=", "HEAD"), "a.txt")
            self.assertIn("?? b.txt", git(s.repo, "status", "--porcelain"))

    def test_refused_on_default_or_agent_branch(self):
        with Sandbox() as s:
            s.write("a.txt", "a")
            rc, _, err = dev("commit", "-m", "fix: x", "a.txt")
            self.assertEqual(rc, 2)
            self.assertIn("dev branch new", err)
            s.branch("codex/tmp-1")
            rc, _, err = dev("commit", "-m", "fix: x", "a.txt")
            self.assertEqual(rc, 2)
            self.assertIn("dev branch rename", err)

    def test_nothing_staged(self):
        with Sandbox() as s:
            s.branch("fix/none")
            self.assertEqual(dev("commit", "-m", "fix: nothing")[0], 1)

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
            s.commit("feat: add x")
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
            self.assertEqual(gitx.push_budget_count(s.ctx(), "feat/x"), 1)

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

    def test_push_budget_applies_and_a_real_local_run_lifts_it(self):
        with Sandbox() as s:
            s.branch("feat/loop")
            for i in range(2):
                s.commit(f"fix: attempt {i}")
                self.assertEqual(dev("push", "--ci-passed")[0], 0)
            s.commit("fix: attempt 3")
            rc, _, err = dev("push", "--ci-passed")
            self.assertEqual(rc, 2)
            self.assertIn("push #3", err)
            os.environ["DEVKIT_LOCAL_CI_PASSED"] = "1"
            try:
                self.assertEqual(dev("push")[0], 0)
            finally:
                os.environ.pop("DEVKIT_LOCAL_CI_PASSED")

    def test_dry_run_pushes_and_records_nothing(self):
        with Sandbox() as s:
            s.branch("feat/dry")
            s.commit("feat: add dry")
            rc, out, err = dev("push", "--dry-run", "--ci-passed")
            self.assertEqual(rc, 0, err)
            self.assertIn("dry run", out)
            self.assertEqual(git(s.origin, "branch", "--list", "feat/dry"), "")
            self.assertFalse((core.STATE / "ci-pushes.json").exists())
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

    def test_no_workflows(self):
        with Sandbox() as s:
            s.branch("feat/none")
            rc, _, err = dev("ci", "--run")
            self.assertEqual(rc, 1)
            self.assertIn("--mark-passed", err)
            self.assertEqual(dev("ci", "--evidence")[0], 2)


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

    def test_create_needs_the_push_and_the_evidence(self):
        with Sandbox() as s:
            s.branch("feat/np")
            s.commit("feat: add np")
            rc, _, err = dev("pr", "create", "--draft")
            self.assertEqual(rc, 2)
            self.assertIn("not on origin", err)
            git(s.repo, "push", "-q", "-u", "origin", "feat/np")
            rc, _, err = dev("pr", "create", "--draft")
            self.assertEqual(rc, 2)
            self.assertIn("no local CI evidence", err)

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
            core.HANDOFF.mkdir(parents=True)
            (core.HANDOFF / "registry.json").write_text(json.dumps({"projects": [
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
            self.assertEqual(dev("gh", "run", "rerun", "5", "--failed")[0], 2)
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



class SpanishTemplateTests(unittest.TestCase):
    def test_spanish_template_sections_are_required(self):
        tpl = (Path(__file__).resolve().parents[1] / "templates" / "github" / "pull_request.es.md").read_text()
        kinds = [k for k, _, _, _ in gitx.sections(tpl)]
        self.assertEqual(kinds, ["what", "changes", "verify", "risk", "links"])
        self.assertEqual(gitx.body_problems(tpl, tpl), ["Qué y por qué", "Cómo verificarlo"])
        filled = tpl.replace("## Qué y por qué\n", "## Qué y por qué\n\nExporta facturas a CSV para Finanzas.\n", 1)
        filled = filled.replace("- CI local: `<comando>`", "- CI local: `./mvnw verify` → 214 tests pasan\n- x", 1)
        self.assertEqual(gitx.body_problems(filled, tpl), [])


if __name__ == "__main__":
    unittest.main()
