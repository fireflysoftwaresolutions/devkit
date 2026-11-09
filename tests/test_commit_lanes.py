#!/usr/bin/env python3
# Crew members who work in one checkout share one git index. `dev commit` in a crew lane commits only the staged files
# inside the lane's step scope and leaves the other staged files exactly as they were, for their owner (lesson
# L-g1ha6djvhfp: a guard commit carried the Technical writer's staged files). The main session and `dev commit <paths>`
# behave as before. Every test works on a temporary repository with devkit's state in a temp folder, git's global and
# system config switched off, and fixed commit dates.

import argparse
import contextlib
import io
import json
import os
import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core, gitx, progress  # noqa: E402

ISOLATE = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Ana Dev",
           "GIT_AUTHOR_EMAIL": "ana@example.com", "GIT_COMMITTER_NAME": "Ana Dev",
           "GIT_COMMITTER_EMAIL": "ana@example.com", "GIT_AUTHOR_DATE": "2026-11-08T12:00:00Z",
           "GIT_COMMITTER_DATE": "2026-11-08T12:00:00Z"}
SID = "s-lanes"
KEY, GHOST, HELPER = "a-keymaker", "a-ghost", "a-helper"  # the lanes' agent ids
BRANCH = "fix/commit-lanes"
CAUSE = "the guard read its rules file again on every call"  # a fix commit names its root cause


def _parser():
    ap = argparse.ArgumentParser(prog="dev")
    sp = ap.add_subparsers(dest="cmd")

    def add(name, fn, help_):
        p = sp.add_parser(name, help=help_)
        p.set_defaults(fn=fn)
        return p

    gitx.register(add)
    return ap


PARSER = _parser()


def git(cwd, *args):
    rc, out, err = core.sh(["git", "-C", str(cwd), *args], timeout=60)
    if rc != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {err or out}")
    return out


class Lanes(unittest.TestCase):
    # One checkout on a fix branch; a run in which the Security engineer's lane owns lib/** and the Technical writer's
    # lane owns plugin/commands/**, and an ad-hoc helper has no scope.
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        state = self.root / "state"
        self.saved = (core.STATE, core.HANDOFF, progress.RUNS, progress.SESSIONS, progress.CALLS,
                      {k: core.GIT_ENV.get(k) for k in [*ISOLATE, "GIT_INDEX_FILE"]}, os.getcwd())
        core.STATE = state
        core.HANDOFF = self.root / "handoff"
        progress.RUNS, progress.SESSIONS, progress.CALLS = state / "runs", state / "sessions", state / "calls"
        core.GIT_ENV.update(ISOLATE)
        core.GIT_ENV.pop("GIT_INDEX_FILE", None)
        env = {k: v for k, v in os.environ.items() if k not in ("DEVKIT_FROM_AGENT", "GIT_INDEX_FILE")}
        env["DEVKIT_SESSION"] = f"claude:{SID}"
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.repo = self.root / "widget"
        git(self.root, "init", "-q", "-b", "main", str(self.repo))
        for rel, text in (("README.md", "# widget\n"), ("lib/guard.py", "RULES = 1\n"), ("lib/old.py", "OLD = 1\n"),
                          ("plugin/commands/ship.md", "# Ship\n")):
            self.write(rel, text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "chore: start")
        git(self.repo, "switch", "-q", "-c", BRANCH)
        progress.RUNS.mkdir(parents=True)
        run = {"v": 2, "agent": "claude", "session": SID, "goal": "Hotfix", "state": "running", "intervals": [],
               "plan": [{"name": "guard", "owner": KEY, "state": "running", "scope": ["lib/**"]},
                        {"name": "docs", "owner": GHOST, "state": "running", "scope": ["plugin/commands/**"]}],
               "agents": {KEY: {"persona": "The Keymaker", "state": "running", "parent": "main"},
                          GHOST: {"persona": "Ghost", "state": "running", "parent": "main"},
                          HELPER: {"label": "general-purpose", "state": "running", "parent": "main"}}}
        (progress.RUNS / f"claude-{SID}.json").write_text(json.dumps(run))
        os.chdir(self.repo)

    def tearDown(self):
        self.env.stop()
        core.STATE, core.HANDOFF, progress.RUNS, progress.SESSIONS, progress.CALLS, env, cwd = self.saved
        for k, v in env.items():
            if v is None:
                core.GIT_ENV.pop(k, None)
            else:
                core.GIT_ENV[k] = v
        os.chdir(cwd)
        self.tmp.cleanup()

    def write(self, rel, text):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def dev(self, who, *argv):
        """`dev <argv>` as the shell call of `who` (a lane's agent id, or main): the call ledger holds it while it
        runs, as the hooks record it, and DEVKIT_FROM_AGENT is not set (`dev commit` is not a routed lane call)."""
        argv = [str(x) for x in argv]
        calls = progress.CALLS / SID
        calls.mkdir(parents=True, exist_ok=True)
        entry = calls / "toolu_1.json"
        entry.write_text(json.dumps({"tool_use_id": "toolu_1", "caller": who, "ts": time.time(), "ttl": 3600,
                                     "cmd": "dev " + " ".join(shlex.quote(x) for x in argv)}))
        out, err = io.StringIO(), io.StringIO()
        try:
            with mock.patch.object(sys, "argv", ["dev", *argv]), contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                a = PARSER.parse_args(argv)
                rc = a.fn(a)
        finally:
            entry.unlink()
        return rc, out.getvalue(), err.getvalue()

    def committed(self):
        """HEAD's changes as {(status, path)}."""
        out = git(self.repo, "show", "--name-status", "--no-renames", "--format=", "HEAD")
        return {tuple(ln.split("\t", 1)) for ln in out.splitlines() if ln.strip()}

    def staged(self):
        return git(self.repo, "diff", "--cached", "--name-only").splitlines()

    def entries(self, *paths):
        """The shared index's entries for these paths (mode, blob, stage, path)."""
        return git(self.repo, "ls-files", "-s", "--", *paths)

    def ghost_stages(self):
        """The Technical writer stages a changed and a new file of their step."""
        self.write("plugin/commands/ship.md", "# Ship\n\nPush once.\n")
        self.write("plugin/commands/new.md", "# New\n")
        git(self.repo, "add", "plugin/commands/ship.md", "plugin/commands/new.md")
        return self.entries("plugin/commands/ship.md", "plugin/commands/new.md")


class LaneCommitTests(Lanes):
    def test_a_lane_commits_only_its_staged_files_and_the_other_lane_keeps_its_own_staged(self):
        theirs = self.ghost_stages()
        self.write("lib/guard.py", "RULES = 2\n")
        git(self.repo, "add", "lib/guard.py")
        git(self.repo, "rm", "-q", "lib/old.py")
        self.write("lib/guard.py", "RULES = 3  # not staged yet\n")
        rc, out, err = self.dev(KEY, "commit", "-m", "fix(guard): read the rules once", "--cause", CAUSE)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.committed(), {("M", "lib/guard.py"), ("D", "lib/old.py")})
        self.assertEqual(git(self.repo, "show", "HEAD:lib/guard.py"), "RULES = 2")  # what was staged, not the edit
        self.assertEqual(sorted(self.staged()), ["plugin/commands/new.md", "plugin/commands/ship.md"])
        self.assertEqual(self.entries("plugin/commands/ship.md", "plugin/commands/new.md"), theirs)
        self.assertIn("plugin/commands/ship.md", out)
        self.assertIn("plugin/commands/new.md", out)

    def test_a_hook_that_restages_a_lane_file_leaves_no_staged_revert_in_the_shared_index(self):
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nprintf 'RULES = 9\\n' > lib/guard.py\ngit add lib/guard.py\n")
        hook.chmod(0o755)
        theirs = self.ghost_stages()
        self.write("lib/guard.py", "RULES = 2\n")
        git(self.repo, "add", "lib/guard.py")
        rc, out, err = self.dev(KEY, "commit", "-m", "fix(guard): read the rules once", "--cause", CAUSE)
        self.assertEqual(rc, 0, err)
        self.assertEqual(git(self.repo, "show", "HEAD:lib/guard.py"), "RULES = 9")  # the hook's fix was committed
        self.assertEqual(sorted(self.staged()), ["plugin/commands/new.md", "plugin/commands/ship.md"])
        self.assertEqual(self.entries("plugin/commands/ship.md", "plugin/commands/new.md"), theirs)

    def test_a_lane_with_nothing_of_its_own_staged_is_refused_and_nothing_moves(self):
        theirs = self.ghost_stages()
        head = git(self.repo, "rev-parse", "HEAD")
        with mock.patch.dict(os.environ, {"DEVKIT_FROM_AGENT": KEY}):
            rc, out, err = self.dev(KEY, "commit", "-m", "fix(guard): read the rules once", "--cause", CAUSE)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("lib/**", err)  # the scope it looked in
        self.assertIn("plugin/commands/ship.md", err)  # what it left, and why
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(self.entries("plugin/commands/ship.md", "plugin/commands/new.md"), theirs)

    def test_a_lane_without_a_scope_is_refused_and_told_to_name_its_paths(self):
        self.ghost_stages()
        head = git(self.repo, "rev-parse", "HEAD")
        rc, out, err = self.dev(HELPER, "commit", "-m", "fix(guard): read the rules once", "--cause", CAUSE)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("<paths>", err)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(sorted(self.staged()), ["plugin/commands/new.md", "plugin/commands/ship.md"])

    def test_all_in_a_lane_stages_only_the_tracked_changes_inside_its_scope(self):
        self.write("plugin/commands/ship.md", "# Ship\n\nDraft.\n")  # the Technical writer's unstaged work
        self.write("lib/guard.py", "RULES = 2\n")
        rc, out, err = self.dev(KEY, "commit", "-a", "-m", "fix(guard): read the rules once", "--cause", CAUSE)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.committed(), {("M", "lib/guard.py")})
        self.assertEqual(self.staged(), [])  # their change was not staged for them
        self.assertEqual(git(self.repo, "diff", "--name-only"), "plugin/commands/ship.md")

    def test_the_main_session_commits_everything_staged_including_what_a_lane_left(self):
        self.ghost_stages()
        self.write("README.md", "# widget\n\nTwo lanes.\n")
        git(self.repo, "add", "README.md")
        self.write("lib/guard.py", "RULES = 2\n")
        git(self.repo, "add", "lib/guard.py")
        self.assertEqual(self.dev(KEY, "commit", "-m", "fix(guard): read the rules once", "--cause", CAUSE)[0],
                         0)
        rc, out, err = self.dev("main", "commit", "-m", "docs: describe the two lanes")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.committed(), {("M", "README.md"), ("M", "plugin/commands/ship.md"),
                                            ("A", "plugin/commands/new.md")})
        self.assertEqual(self.staged(), [])

    def test_named_paths_in_a_lane_commit_exactly_those_paths_as_before(self):
        # unchanged behavior, kept by the change: named paths are committed even outside the lane's scope
        theirs = self.ghost_stages()
        self.write("lib/guard.py", "RULES = 2\n")
        self.write("README.md", "# widget\n\nNamed.\n")
        rc, out, err = self.dev(KEY, "commit", "-m", "fix(guard): read the rules once", "--cause", CAUSE, "lib/guard.py",
                                 "README.md")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.committed(), {("M", "lib/guard.py"), ("M", "README.md")})
        self.assertEqual(self.entries("plugin/commands/ship.md", "plugin/commands/new.md"), theirs)


if __name__ == "__main__":
    unittest.main()
