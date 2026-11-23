# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# A step's --scope is saved in the run and matched against file paths, so `dev crew delegate` and `dev progress
# assign` read it by one rule: paths or globs in the repository, or in this project's plans folder or the scratch
# folder of the member the step goes to. A secret, a URL or remote, a ".." segment, any other path outside every
# checkout and an invisible character are refused, quoting none of the text, and nothing is saved.
# Progress state in a temporary folder (test_progress.TempState).
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_dispatch_scope -v
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import dispatch  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_progress import TempState, repo_with_worktrees  # noqa: E402

T0 = 1_800_000_000.0
SECRET = "ghp_" + "a1B2" * 10  # the shape of a GitHub token


class InvisibleCharacterScopeTests(unittest.TestCase):
    def test_a_scope_with_an_invisible_format_or_line_separator_character_is_refused(self):
        # a right-to-left override (U+202E) shows lib/x.py while the scope holds another name; a line separator
        # (U+2028) splits the scope where it is shown: delegate and assign both saved them
        for scope in ("lib/‮yp.x", "lib/a b.py", "lib/​x.py"):
            with self.subTest(scope=ascii(scope)), TempState():
                pg.start("Feature", plan=["parser=30m"], now=T0)
                with self.assertRaisesRegex(pg.StepError, "repository-relative"):
                    dispatch.delegate("add the export column", to="Ghost", scope=scope, now=T0 + 60)
                with self.assertRaisesRegex(pg.StepError, "repository-relative"):
                    pg.assign("parser", "Neo", scope=scope, now=T0 + 60)
                run = pg.load_run("claude", "s1")
                self.assertEqual([(s["name"], s.get("scope")) for s in run["plan"]], [("parser", None)])


class AssignScopeTests(unittest.TestCase):
    def test_assign_refuses_a_scope_with_a_secret_and_saves_nothing(self):
        # assign saved the scope text unchecked: a token in it went into the run
        with TempState():
            pg.start("Feature", plan=["parser=30m"], now=T0)
            with self.assertRaises(pg.StepError) as e:
                pg.assign("parser", "Neo", scope=f"lib/**,https://u:{SECRET}@h/x", now=T0 + 60)
            self.assertNotIn(SECRET, str(e.exception))
            self.assertIn("secret", str(e.exception))
            self.assertNotIn(SECRET, json.dumps(pg.load_run("claude", "s1")))

    def test_assign_refuses_a_scope_that_is_not_a_repository_path_as_delegate_does(self):
        # a URL, a remote, a ".." segment and a folder outside every checkout of the repository: assign kept them
        with TempState(), tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as scratch:
            main, wt = repo_with_worktrees(d)
            pg.start("Feature", plan=["parser=30m"], now=T0)
            for scope in ("https://example.com/x", "git@host:org/repo", "../other/**", f"{wt}/lib/../tests/**",
                          f"{scratch}/notes/**"):
                with self.subTest(scope=scope):
                    with self.assertRaises(pg.StepError) as by_assign:
                        pg.assign("parser", "Neo", scope=scope, cwd=str(main), now=T0 + 60)
                    with self.assertRaises(pg.StepError) as by_delegate:
                        dispatch.delegate("add the export column", to="Ghost", scope=scope, cwd=str(main),
                                          now=T0 + 60)
                    self.assertIn("repository-relative", str(by_assign.exception))
                    self.assertEqual(str(by_assign.exception), str(by_delegate.exception))
            self.assertEqual([(s["name"], s.get("scope")) for s in pg.load_run("claude", "s1")["plan"]],
                             [("parser", None)])


class OutsideFolderScopeTests(unittest.TestCase):
    # Outside the repository a step's scope may name two folders only: this project's plans folder in the handoff
    # repository, where the Software architect and the Technical writer write specs, and the scratch folder of the
    # member the step goes to (scratchpad/<member id>/ in the session scratchpad). Both are judged by their real
    # paths, so a link cannot lead out of them, and the scope keeps the real path it was judged by.
    def setUp(self):
        from devkit import core, hooks
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.d = Path(d.name)
        self.main, _ = repo_with_worktrees(self.d)
        home, tmp = self.d / "claude-home", self.d / "claude-tmp"
        (home / "projects" / "-Users-x").mkdir(parents=True)
        (home / "projects" / "-Users-x" / "s1.jsonl").write_text("")  # the session's own transcript
        for p in (mock.patch.object(core, "HANDOFF", self.d / "handoff"), mock.patch.object(core, "CLAUDE_HOME", home),
                  mock.patch.object(hooks, "_claude_tmp", lambda: tmp)):
            p.start()
            self.addCleanup(p.stop)
        self.plans = self.d / "handoff" / "plans" / "acme" / "api"
        self.pad = tmp / "-Users-x" / "s1" / "scratchpad"
        self.plans.mkdir(parents=True)
        (self.pad / "tank").mkdir(parents=True)  # neo's folder does not exist yet: its launch creates it
        pg.start("Feature", plan=["spec=30m", "parser=30m"], now=T0)

    def scopes(self):
        return {s["name"]: s.get("scope") for s in pg.load_run("claude", "s1")["plan"]}

    def keeps(self, step, member, task, scope):
        # assign and delegate keep the scope, in the real path it was judged by -> what each kept
        pg.assign(step, member, scope=scope, cwd=str(self.main), now=T0 + 60, why="the scope rule under test")
        made = dispatch.delegate(task, to=member, scope=scope, cwd=str(self.main), now=T0 + 60)
        return self.scopes()[step], made["step"].get("scope")

    def refuses(self, step, member, scope):
        before = self.scopes()
        with self.assertRaises(pg.StepError) as by_assign:
            pg.assign(step, member, scope=scope, cwd=str(self.main), now=T0 + 60, why="the scope rule under test")
        with self.assertRaises(pg.StepError) as by_delegate:
            dispatch.delegate("write it", to=member, scope=scope, cwd=str(self.main), now=T0 + 60)
        self.assertEqual(self.scopes(), before)
        for e in (by_assign, by_delegate):  # refused by the rule for outside folders, which says what it allows
            self.assertIn("scratchpad/<member id>/", str(e.exception))
        return str(by_assign.exception), str(by_delegate.exception)

    def test_a_scope_in_this_projects_plans_folder_is_kept(self):
        # bbfe9ba8 refused it with every path outside the repository, so a spec step could not own its plan
        real = os.path.realpath(self.plans)
        into = self.d / "into"
        into.symlink_to(self.plans)  # a link that leads into the folder names the folder's files
        for scope, kept in ((f"{self.plans}/**", f"{real}/**"), (f"{self.plans}/", f"{real}/"),
                            (f"{self.plans}/specs/*.md", f"{real}/specs/*.md"), (f"{into}/**", f"{real}/**")):
            with self.subTest(scope=scope):
                self.assertEqual(self.keeps("spec", "Ghost", "write the spec", scope), ([kept], [kept]))

    def test_a_scope_in_the_assigned_members_own_scratch_folder_is_kept(self):
        real = os.path.join(os.path.realpath(self.pad), "neo")
        for scope, kept in ((f"{self.pad}/neo/**", f"{real}/**"), (f"{self.pad}/neo/probe.log", f"{real}/probe.log")):
            with self.subTest(scope=scope):
                self.assertEqual(self.keeps("parser", "Neo", "probe the parser", scope), ([kept], [kept]))

    def test_another_projects_plans_folder_is_refused(self):
        plans = self.plans.parent  # plans/acme: every name below it but api is another project's
        (plans / "api-old").mkdir()
        for scope in (f"{plans}/web/**", f"{plans}/api-old/**", f"{plans}/ap*/**", f"{plans}/**",
                      f"{plans.parent}/**"):
            with self.subTest(scope=scope):
                self.refuses("spec", "Ghost", scope)

    def test_another_members_or_another_sessions_scratch_folder_is_refused(self):
        other = self.pad.parent.parent / "s2" / "scratchpad" / "neo"  # neo's folder in another session
        for scope in (f"{self.pad}/tank/**", f"{self.pad}/**", f"{self.pad}/ne*/**", f"{self.pad}/neo-x/**",
                      f"{other}/**"):
            with self.subTest(scope=scope):
                self.refuses("parser", "Neo", scope)

    def test_a_link_that_leads_out_of_an_allowed_folder_is_refused(self):
        outside = self.d / "outside"
        outside.mkdir()
        (self.plans / "out").symlink_to(outside)
        (self.pad / "neo").symlink_to(outside)  # the member's folder itself, made a link before its launch
        for step, member, scope in (("spec", "Ghost", f"{self.plans}/out/**"),
                                    ("parser", "Neo", f"{self.pad}/neo/**")):
            with self.subTest(scope=scope):
                self.refuses(step, member, scope)

    def test_the_refusal_names_the_folders_an_outside_scope_may_name_and_quotes_none_of_it(self):
        by_assign, by_delegate = self.refuses("parser", "Neo", f"{self.d}/private-notes/**")
        self.assertEqual(by_assign, by_delegate)
        self.assertIn(f"{os.path.realpath(self.plans)}/", by_assign)
        self.assertIn("scratchpad/<member id>/", by_assign)
        self.assertNotIn("private-notes", by_assign)

    def test_a_codex_sessions_member_scratch_folder_is_kept(self):
        # Codex keeps it in <dev>/.tmp/codex/<session>/scratchpad/<member>/ (the CLI) or in the ChatGPT app's
        # <session folder>/work/scratchpad/<member>/, as the launch hook makes it
        from devkit import core
        home, tmp_root = self.d / "home", self.d / "dev-tmp"
        app = home / "Documents" / "Codex" / "project"
        for p in (mock.patch.object(core, "HOME", home), mock.patch.object(core, "TMP_ROOT", tmp_root)):
            p.start()
            self.addCleanup(p.stop)
        for sid, cwd, pad in (("t1", str(self.main), tmp_root / "codex" / "t1" / "scratchpad"),
                              ("t2", str(app), app / "work" / "scratchpad")):
            with self.subTest(session=sid):
                os.environ["DEVKIT_SESSION"] = f"codex:{sid}"
                pg.session_path("codex", sid).write_text(json.dumps({"agent": "codex", "session": sid, "cwd": cwd,
                                                                     "started": T0, "project": "acme/api"}))
                pg.start("Feature", plan=["parser=30m"], now=T0)
                pg.assign("parser", "Neo", scope=f"{pad}/neo/**", cwd=str(self.main), now=T0 + 60, why="the rule")
                kept = {s["name"]: s.get("scope") for s in pg.load_run("codex", sid)["plan"]}["parser"]
                self.assertEqual(kept, [f"{os.path.realpath(pad)}/neo/**"])
                with self.assertRaises(pg.StepError):
                    pg.assign("parser", "Neo", scope=f"{pad}/tank/**", cwd=str(self.main), now=T0 + 60, why="x")


class LaneFolderScopeTests(unittest.TestCase):
    # A crew lane that runs a step writes in scratchpad/<member id>-<step>/, so each lane of one member has a folder of
    # its own. A step's scope may name that folder of the step's own member, besides the member's plain
    # scratchpad/<member id>/; another step's folder and another member's lane folder stay refused. The fixture and
    # helpers are OutsideFolderScopeTests', without its tests.
    setUp = OutsideFolderScopeTests.setUp
    scopes = OutsideFolderScopeTests.scopes
    refuses = OutsideFolderScopeTests.refuses

    def test_assign_keeps_the_lane_folder_of_its_step_and_refuses_every_other_lane_folder(self):
        pg.assign("parser", "Neo", scope=f"{self.pad}/neo-parser/**", cwd=str(self.main), now=T0 + 60,
                  why="the scope rule under test")
        self.assertEqual(self.scopes()["parser"], [f"{os.path.realpath(self.pad)}/neo-parser/**"])
        for scope in (f"{self.pad}/neo-spec/**", f"{self.pad}/tank-parser/**", f"{self.pad}/neo-parser-2/**"):
            with self.subTest(scope=scope):  # another step's, another member's, a longer name
                self.refuses("parser", "Neo", scope)

    def test_delegate_keeps_the_lane_folder_of_the_step_it_adds(self):
        made = dispatch.delegate("probe the parser", to="Neo", scope=f"{self.pad}/neo-probe-the-parser/probe.log",
                                 cwd=str(self.main), now=T0 + 60)
        self.assertEqual(made["step"].get("scope"), [f"{os.path.realpath(self.pad)}/neo-probe-the-parser/probe.log"])


if __name__ == "__main__":
    unittest.main()
