# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Where a crew lane's scope lands: its globs count from the checkout of the run's repository whatever folder the
# session runs in, a relative glob never reaches outside that checkout (a link included), write detection names what
# a shell call wrote, and a member of another session's live run owns the files its scope holds. Real git
# repositories in temporary folders; no command an agent typed runs.
# Run: python3 -m unittest tests.test_lanescope -v
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()  # run alone, this module gets the suite's temporary home too (tests/_hermetic.py)
sys.path.insert(0, str(ROOT / "lib"))

from devkit import core, lanescope  # noqa: E402


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(d):
    repo = Path(d) / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("a = 1\n")
    (repo / "notes.md").write_text("x\n")
    (repo / ".gitignore").write_text("build/\n")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "add", ".")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init")
    return repo


class WriteDetectionTests(unittest.TestCase):

    def test_a_test_run_that_writes_into_src_is_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            repo = _repo(d)
            (repo / "notes.md").write_text("changed before the call\n")
            before = lanescope.worktree_state(str(repo))
            (repo / "src" / "a.py").write_text("a = 2\n")
            (repo / "src" / "new.py").write_text("x = 1\n")
            (repo / "notes.md").write_text("changed again by the call, longer\n")  # a new size, whatever the clock
            (repo / "build").mkdir()
            (repo / "build" / "out.o").write_text("ignored\n")
            self.assertEqual(lanescope.written_paths(before, lanescope.worktree_state(str(repo))),
                             ["notes.md", "src/a.py", "src/new.py"])

    def test_a_reverted_file_counts_and_an_untouched_tree_does_not(self):
        with tempfile.TemporaryDirectory() as d:
            repo = _repo(d)
            (repo / "src" / "a.py").write_text("a = 2\n")
            before = lanescope.worktree_state(str(repo))
            self.assertEqual(lanescope.written_paths(before, lanescope.worktree_state(str(repo))), [])
            _git(repo, "checkout", "--", "src/a.py")
            self.assertEqual(lanescope.written_paths(before, lanescope.worktree_state(str(repo))), ["src/a.py"])

    def test_outside_a_repository_there_is_nothing_to_compare(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(lanescope.worktree_state(d))
            self.assertEqual(lanescope.written_paths(None, None), [])

    def test_a_failing_status_read_is_unknown_and_logged(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(lanescope.core, "sh", side_effect=OSError("git is gone")), \
                mock.patch.object(lanescope, "_log") as log:
            repo = _repo(d)
            self.assertIsNone(lanescope.worktree_state(str(repo)))
        self.assertIn("git is gone", log.call_args[0][0])


class ScopeBaseTests(unittest.TestCase):

    def workspace(self):
        # a workspace laid out like the developer's, built for the test so it holds wherever the tree under test sits
        # (a main checkout, a managed worktree, an exported copy without .git): the run's repository under its
        # two-part id <dev>/<owner>/<repo>, whose origin names that id, and a managed worktree of it below
        # <dev>/.worktrees/<owner>/<repo>/. It sits in the run's home: devkit gives a checkout in a temp folder no
        # project
        d = tempfile.mkdtemp(dir=str(core.HOME))
        self.addCleanup(shutil.rmtree, d, True)
        dev = Path(d) / "Development"
        main = dev / "fireflysoftwaresolutions" / "devkit"
        (main / "lib" / "devkit").mkdir(parents=True)
        (main / "lib" / "devkit" / "hooks.py").write_text("HOOKS = True\n")
        (main / "README.md").write_text("devkit\n")
        _git(main, "init", "-q")
        _git(main, "remote", "add", "origin", "https://github.com/fireflysoftwaresolutions/devkit.git")
        _git(main, "-c", "user.email=t@example.com", "-c", "user.name=t", "add", ".")
        _git(main, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init")
        wt = dev / ".worktrees" / "fireflysoftwaresolutions" / "devkit" / "test-x"
        _git(main, "worktree", "add", "-q", "-b", "test/x", str(wt))
        return dev, "fireflysoftwaresolutions/devkit", main, wt

    def test_scope_globs_are_relative_to_the_runs_repository_whatever_the_session_folder(self):
        dev, project, main, wt = self.workspace()
        path = str(main / "lib" / "devkit" / "hooks.py")
        home = str(core.HOME)
        self.assertFalse(lanescope.scope_holds(path, home, ["lib/devkit/hooks.py"]))  # what the session's cwd gives
        # the run's two-part project id names the main checkout from a session outside it, and a session in the
        # checkout or in its managed worktree counts from the folder it works in
        with mock.patch.object(lanescope, "DEV", dev):
            base = lanescope.scope_base(project, home)
            self.assertEqual(os.path.realpath(base), os.path.realpath(main))
            self.assertTrue(lanescope.scope_holds(path, home, ["lib/devkit/hooks.py"], base=base))
            self.assertFalse(lanescope.scope_holds(path, home, ["tests/**"], base=base))
            self.assertEqual(os.path.realpath(lanescope.scope_base(project, str(main / "lib"))),
                             os.path.realpath(main))
            self.assertEqual(os.path.realpath(lanescope.scope_base(project, str(wt / "lib"))), os.path.realpath(wt))
            self.assertEqual(lanescope.scope_rel(path, home, base), "lib/devkit/hooks.py")

    def test_a_relative_scope_never_holds_a_file_a_link_takes_out_of_the_repository(self):
        # a whole-checkout scope lists the checkout's top-level entries, and a top-level link among them (shared/**
        # for `shared -> elsewhere`) must not hold the files outside it
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as away:
            repo = Path(os.path.realpath(_repo(d)))  # as git names it: /var/folders is /private/var/folders
            (repo / "shared").symlink_to(away)
            (repo / "latest").symlink_to(repo / "src")  # a link that stays inside the repository
            for path in ("shared/notes.md", "shared/new/x.py"):
                with self.subTest(path=path):
                    self.assertFalse(lanescope.scope_holds(path, str(repo), ["shared/**", "**"]))
            self.assertTrue(lanescope.scope_holds("src/a.py", str(repo), ["src/**"]))
            self.assertTrue(lanescope.scope_holds("latest/a.py", str(repo), ["latest/**"]))

    def test_an_absolute_glob_names_a_folder_anywhere(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as plans:
            repo = _repo(d)
            plan = Path(plans) / "devkit" / "plan.md"
            self.assertTrue(lanescope.scope_holds(str(plan), str(repo), [plans + "/devkit/**"]))
            self.assertFalse(lanescope.scope_holds(str(plan), str(repo), [plans + "/other/**"]))

    def test_a_session_in_a_worktree_of_the_runs_repository_counts_from_that_worktree(self):
        with tempfile.TemporaryDirectory() as d:
            repo = _repo(d)
            wt = Path(d) / "wt"
            _git(repo, "worktree", "add", "-q", "-b", "feat/x", str(wt))
            with mock.patch.object(lanescope, "DEV", Path(d)):
                self.assertEqual(os.path.realpath(lanescope.scope_base("repo", str(wt / "src"))),
                                 os.path.realpath(wt))
                self.assertEqual(os.path.realpath(lanescope.scope_base("repo", d)), os.path.realpath(repo))
                self.assertEqual(os.path.realpath(lanescope.scope_base("", str(wt))), os.path.realpath(wt))  # no run


class OwnershipAcrossRunsTests(unittest.TestCase):

    def _runs(self, repo, state="running", session="s-other"):
        return [{"session": session, "agent": "claude", "project": "o/repo", "plan": [
                    {"name": "api", "owner": "a1", "state": "running", "scope": ["src/api/**"]}],
                 "agents": {"a1": {"state": state, "label": "Tank: the API", "persona": "Tank",
                                   "scope": ["src/models.py"]}}}]

    def test_another_sessions_member_owns_the_files_its_scope_holds(self):
        with tempfile.TemporaryDirectory() as d:
            repo = _repo(d)
            runs = self._runs(repo)
            own = lanescope.scope_owner_across(str(repo / "src" / "api" / "x.py"), runs, "s-me",
                                               checkout_of=lambda r: str(repo))
            self.assertEqual(own[0], "Tank")
            self.assertIn("src/api/**", own[1])
            self.assertEqual(lanescope.scope_owner_across(str(repo / "src" / "models.py"), runs, "s-me",
                                                          checkout_of=lambda r: str(repo))[0], "Tank")
            self.assertIsNone(lanescope.scope_owner_across(str(repo / "notes.md"), runs, "s-me",
                                                           checkout_of=lambda r: str(repo)))

    def test_a_finished_lane_its_own_session_or_another_checkout_owns_nothing_here(self):
        with tempfile.TemporaryDirectory() as d:
            repo = _repo(d)
            path = str(repo / "src" / "api" / "x.py")
            self.assertIsNone(lanescope.scope_owner_across(path, self._runs(repo, state="done"), "s-me",
                                                           checkout_of=lambda r: str(repo)))
            self.assertIsNone(lanescope.scope_owner_across(path, self._runs(repo, session="s-me"), "s-me",
                                                           checkout_of=lambda r: str(repo)))
            self.assertIsNone(lanescope.scope_owner_across(path, self._runs(repo), "s-me",
                                                           checkout_of=lambda r: str(Path(d) / "elsewhere")))

    def test_a_run_whose_session_record_cannot_be_read_counts_from_its_main_checkout(self):
        with tempfile.TemporaryDirectory() as d:
            main = Path(d) / "o" / "repo"
            main.mkdir(parents=True)
            (main / ".git").mkdir()
            run = {"agent": "claude", "session": "s-x", "project": "o/repo"}
            with mock.patch.object(lanescope, "DEV", Path(d)), \
                    mock.patch.object(lanescope.core, "read_json", side_effect=ValueError("bad record")), \
                    mock.patch.object(lanescope, "_log") as log:
                self.assertEqual(lanescope.run_checkout(run), str(main))
            self.assertIn("bad record", log.call_args[0][0])


class ProjectTests(unittest.TestCase):

    def test_a_temporary_repository_and_a_word_the_shell_decides_are_no_project(self):
        with tempfile.TemporaryDirectory() as d:
            repo = _repo(d)
            self.assertIsNone(lanescope.current_project(str(repo)))
        self.assertIsNone(lanescope.current_project("$HOME/x"))
        self.assertIsNone(lanescope.current_project(""))

    def test_a_checkout_names_the_project_its_origin_names(self):
        d = tempfile.mkdtemp(dir=str(core.HOME))
        self.addCleanup(shutil.rmtree, d, True)
        main = Path(d) / "acme" / "billing"
        main.mkdir(parents=True)
        _git(main, "init", "-q")
        _git(main, "remote", "add", "origin", "https://github.com/acme/billing.git")
        self.assertEqual(lanescope.current_project(str(main)), "acme/billing")

    def test_a_path_lies_in_a_checkout_or_not(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as plain:
            repo = _repo(d)
            self.assertTrue(lanescope.in_checkout(str(repo / "src" / "new" / "x.py")))
            self.assertFalse(lanescope.in_checkout(str(Path(plain) / "x.py")))


if __name__ == "__main__":
    unittest.main()
