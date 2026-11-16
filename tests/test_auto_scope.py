# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Automatic policy across the developer's workspace: a project checked out at its place in the dev root, whose origin
is exactly that repository, is covered like the session's own project; any other repository needs the typed grant."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import core, guard, progress as pg, registry  # noqa: E402

OWN = "fireflysoftwaresolutions/devkit"
OTHER = "signature-organization/devkit"


class WorkspaceScopeCase(unittest.TestCase):
    def setUp(self):
        from tests.test_evidence import EvidenceCase
        self.fixture = EvidenceCase()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = Path(os.path.realpath(self.fixture.root))
        self.dev = self.root / "Development"
        self.dev.mkdir()
        self.config = {"work": {"mode": "auto"}}
        self.real_sh = core.sh
        (self.root / "handoff" / "projects").mkdir(parents=True)  # no embargo anywhere
        for patch in (mock.patch.object(core, "config_snapshot", create=True, side_effect=lambda: (self.config, [])),
                      mock.patch.object(core, "DEV", self.dev),
                      mock.patch.object(core, "HANDOFF", self.root / "handoff"),
                      mock.patch.object(core, "NOTES", self.root / "handoff" / "projects"),
                      mock.patch.object(core, "sh", side_effect=self.sh),
                      # the project of a folder, read from its origin as devkit does (the real one skips temp folders)
                      mock.patch.object(guard, "current_project", side_effect=self.project_at),
                      mock.patch.object(guard, "irreversible_setting", return_value="enforce")):
            patch.start()
            self.addCleanup(patch.stop)
        session = json.loads(self.fixture.state.sess.read_text())
        self.fixture.state.sess.write_text(json.dumps(dict(session, project=OWN)))
        run = pg.load_run("claude", "s1")
        run["project"] = OWN
        pg._save(run, touch_session=False)
        self.own = self.checkout(self.dev / OWN, f"https://github.com/{OWN}.git")
        self.other = self.checkout(self.dev / OTHER, f"git@github.com:{OTHER}.git")

    def sh(self, argv, **kw):
        if argv[0] == "gh":
            return 0, '{"login":"alice","id":123}', ""
        return self.real_sh(argv, **kw)

    @staticmethod
    def project_at(cwd):
        top = core.gitout(cwd, "rev-parse", "--show-toplevel")
        return core.project_identity(Path(top), core.gitout(top, "config", "--get", "remote.origin.url"))[0] \
            if top else None

    @staticmethod
    def git(repo, *args):
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                       check=True, capture_output=True)

    def checkout(self, path, origin):
        path.mkdir(parents=True)
        self.git(path, "init", "-q", "-b", "main")
        (path / "package.json").write_text(json.dumps({"name": "widget", "version": "1.4.0",
                                                       "scripts": {"release": "node release.js"}}))
        self.git(path, "add", "-A")
        self.git(path, "commit", "-q", "-m", "init")
        self.git(path, "remote", "add", "origin", origin)
        self.git(path, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.git(path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.git(path, "checkout", "-q", "-b", "feat/x")
        return str(path)

    def verdict(self, cmd, cwd, at=2):
        from tests.test_evidence import m
        return guard.irreversible_verdict(cmd, cwd, agent="claude", session="s1", now=m(at))

    def records(self, action="guard.irreversible"):
        return [r for f in sorted((self.fixture.root / "audit").glob("*.jsonl"))
                for r in map(json.loads, f.read_text().splitlines()) if r["action"] == action]


class WorkspaceCheckoutTests(WorkspaceScopeCase):
    """registry.workspace_checkout: the one place that says whether a repository is a project of the workspace."""

    def test_a_project_at_its_place_whose_origin_is_that_repository(self):
        self.assertEqual(registry.workspace_checkout(OTHER), self.dev / OTHER)
        self.assertEqual(registry.workspace_checkout("Signature-Organization/DevKit"), self.dev / OTHER)

    def test_a_repository_with_no_checkout_in_the_workspace(self):
        self.assertIsNone(registry.workspace_checkout("acme/outside"))

    def test_a_checkout_whose_origin_names_another_repository(self):
        self.git(self.other, "remote", "set-url", "origin", "https://github.com/attacker/x.git")
        self.assertIsNone(registry.workspace_checkout(OTHER))
        self.assertIsNone(registry.workspace_checkout("attacker/x"), "a checkout counts only at its own place")

    def test_a_checkout_whose_origin_pushes_somewhere_else_too(self):
        self.git(self.other, "remote", "set-url", "--add", "--push", "origin", f"git@github.com:{OTHER}.git")
        self.assertEqual(registry.workspace_checkout(OTHER), self.dev / OTHER)
        self.git(self.other, "remote", "set-url", "--add", "--push", "origin", "https://github.com/attacker/x.git")
        self.assertIsNone(registry.workspace_checkout(OTHER))

    def test_an_origin_that_is_a_local_path_names_no_repository(self):
        self.git(self.other, "remote", "set-url", "origin", OTHER)  # a relative folder, not GitHub
        self.assertIsNone(registry.workspace_checkout(OTHER))

    def test_a_checkout_without_an_origin(self):
        self.git(self.other, "remote", "rename", "origin", "upstream")
        self.assertIsNone(registry.workspace_checkout(OTHER))

    def test_a_linked_folder_leading_out_of_the_workspace(self):
        outside = self.root / "outside" / "devkit"
        self.checkout(outside, f"https://github.com/{OTHER}.git")
        import shutil
        shutil.rmtree(self.dev / OTHER)
        os.symlink(outside, self.dev / OTHER)
        self.assertIsNone(registry.workspace_checkout(OTHER))

    def test_a_worktree_or_bare_folder_is_not_a_main_checkout(self):
        bare = self.dev / "acme" / "bare"
        bare.mkdir(parents=True)
        (bare / ".git").write_text("gitdir: /elsewhere\n")
        self.assertIsNone(registry.workspace_checkout("acme/bare"))

    def test_reserved_folders_and_malformed_names_are_never_projects(self):
        for repo in ("_local/devkit", ".worktrees/devkit", "../signature-organization/devkit", "devkit", "",
                     "signature-organization/../signature-organization/devkit", "signature-organization//devkit",
                     None, pg.LOCAL_SCOPE):
            with self.subTest(repo=repo):
                self.assertIsNone(registry.workspace_checkout(repo))

    def test_another_git_host_uses_its_host_folder(self):
        self.checkout(self.dev / "gitlab.com" / "group" / "sub" / "svc", "git@gitlab.com:group/sub/svc.git")
        self.assertEqual(registry.workspace_checkout("gitlab.com/group/sub/svc"),
                         self.dev / "gitlab.com" / "group" / "sub" / "svc")


class WorkspaceAuthorizationTests(WorkspaceScopeCase):
    """The main session's routine delivery in another project of the workspace, judged end to end."""

    COMMANDS = ("git push origin feat/x", "gh pr create --fill", "dev pr create", "dev pr merge 12",
                "dev release --version 26.11.5", "npm run release")

    def test_routine_delivery_in_a_workspace_project_needs_no_grant(self):
        for k, cmd in enumerate(self.COMMANDS):
            with self.subTest(cmd=cmd):
                acts = guard.irreversible_actions(cmd, self.other)
                self.assertEqual([a["repo"] for a in acts], [OTHER], acts)
                self.assertIsNone(self.verdict(cmd, self.other, 2 + k))
                record = self.records()[-1]
                self.assertEqual((record["outcome"], record["ref"]["source"], record["ref"]["repo"]),
                                 ("ok", "policy-auto", OTHER))

    def test_naming_a_workspace_project_from_the_own_checkout_needs_no_grant(self):
        for cmd in (f"gh pr merge 12 -R {OTHER}", f"gh release create v1.2.0 -R {OTHER}",
                    f"git -C {self.other} push origin feat/x"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.verdict(cmd, self.own))

    def test_the_audit_record_names_the_session_project_and_the_target(self):
        self.assertIsNone(self.verdict(f"gh pr merge 12 -R {OTHER}", self.own))
        gate = self.records()[-1]
        self.assertEqual((gate["ref"]["repo"], gate["ref"]["origin_repo"]), (OTHER, OWN))
        policy = self.records("crew.authorization")[-1]
        self.assertEqual((policy["project"], policy["ref"]["origin_repo"], policy["ref"]["operation"]),
                         (OTHER, OWN, "merge"))

    def test_a_repository_outside_the_workspace_needs_the_typed_grant_and_says_why(self):
        outside = self.checkout(self.root / "elsewhere" / "svc", "https://github.com/acme/outside.git")
        denied = self.verdict("git push origin feat/x", outside)
        self.assertIsNotNone(denied)
        self.assertIn("not a project of your workspace", denied[1])
        self.assertIn("grant push feat/x --repo acme/outside", denied[1])
        self.fixture.typed("grant push feat/x --repo acme/outside", self._m(3))
        self.assertIsNone(self.verdict("git push origin feat/x", outside, 4))
        self.assertEqual(self.records()[-1]["ref"]["source"], "E2")

    def test_a_named_repository_whose_workspace_checkout_points_elsewhere_needs_the_grant(self):
        self.git(self.other, "remote", "set-url", "origin", "https://github.com/attacker/x.git")
        denied = self.verdict(f"gh pr merge 12 -R {OTHER}", self.own)
        self.assertIsNotNone(denied)
        self.assertIn(f"grant merge 12 --repo {OTHER}", denied[1])

    def test_guided_mode_still_needs_the_typed_grant(self):
        self.config = {"work": {"mode": "guided"}}
        denied = self.verdict(f"gh pr merge 12 -R {OTHER}", self.own)
        self.assertIsNotNone(denied)
        self.assertIn(f"grant merge 12 --repo {OTHER}", denied[1])

    def test_a_member_still_routes_the_operation_to_the_tech_lead(self):
        why = guard._member_gate(f"gh pr merge 12 -R {OTHER}", self.own, "tank", "a_tank", "claude", "s1", None,
                                 self._m(2))
        self.assertIsNotNone(why)
        self.assertIn("tech lead", why)
        decision = pg.authorization_decision("claude", "s1", action="push", target="feat/x", repo=OTHER,
                                              lane="tank")
        self.assertNotEqual(decision["outcome"], "allow")

    def test_the_developers_own_decision_classes_stay_the_developers(self):
        for action, target in (("embargo", "feat/x"), ("bug-send", "7"), ("bug-share", "7"),
                               ("lesson-release", "L-example"), ("claim-break", "push:feat/x"),
                               ("developer-only", "embargo/feat/x")):
            with self.subTest(action=action):
                decision = pg.authorization_decision("claude", "s1", action=action, target=target, repo=OTHER)
                self.assertNotEqual(decision["outcome"], "allow")

    @staticmethod
    def _m(minutes):
        from tests.test_evidence import m
        return m(minutes)


if __name__ == "__main__":
    unittest.main()
