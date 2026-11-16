# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Automatic policy across the developer's workspace: a project checked out at its place in the dev root, whose origin
is exactly that repository, is covered like the session's own project; any other repository needs the typed grant."""
import json
import os
import shutil
import subprocess
import sys
import time
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
        self.listed(OWN, OTHER)
        # the session starts now, after both checkouts: SessionStart records the workspace as it finds it
        self.session(started=time.time(), workspace_checkouts=registry.workspace_baseline())

    def listed(self, *ids):
        """devkit's saved project list (registry.json), as `dev sync` writes it."""
        (self.root / "handoff" / "registry.json").write_text(json.dumps(
            {"projects": [{"id": i, "path": str(self.dev.joinpath(*i.split("/")))} for i in ids]}))

    def session(self, sid="s1", **fields):
        """Change fields of a session record (None removes one)."""
        path = pg.session_path("claude", sid)
        rec = json.loads(path.read_text()) if path.exists() else {"agent": "claude", "session": sid}
        rec.update(fields)
        path.write_text(json.dumps({k: v for k, v in rec.items() if v is not None}))
        return rec

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


class WorkspaceBaselineTests(WorkspaceScopeCase):
    """registry.workspace_baseline: the git folder of each listed project at its own place, as the session starts."""

    def identity(self, path):
        st = os.lstat(Path(path) / ".git")
        return [st.st_dev, st.st_ino]

    def test_each_listed_project_at_its_place_with_the_identity_of_its_git_folder(self):
        linked = self.dev / "acme" / "linked"
        linked.mkdir(parents=True)
        os.symlink(Path(self.other) / ".git", linked / ".git")
        self.listed(OWN, OTHER, "acme/missing", "acme/linked", "../escape", "_local/devkit")
        self.assertEqual(registry.workspace_baseline(), {
            "projects": {OWN: self.identity(self.own), OTHER: self.identity(self.other)}, "partial": False})

    def test_a_missing_or_unreadable_list_is_an_empty_partial_one(self):
        (self.root / "handoff" / "registry.json").unlink()
        self.assertEqual(registry.workspace_baseline(), {"projects": {}, "partial": True})
        for text in ("{not json", '{"projects": "acme/api"}', "[]"):
            with self.subTest(text=text):
                (self.root / "handoff" / "registry.json").write_text(text)
                self.assertEqual(registry.workspace_baseline(), {"projects": {}, "partial": True})

    def test_a_list_past_its_size_keeps_the_projects_it_read_and_says_it_is_partial(self):
        with mock.patch.object(registry, "WORKSPACE_LIST_MAX", 1):
            self.assertEqual(registry.workspace_baseline(),
                             {"projects": {OWN: self.identity(self.own)}, "partial": True})

    def test_a_list_past_its_time_keeps_the_projects_it_read_and_says_it_is_partial(self):
        import itertools
        clock = itertools.chain([0.0, 0.0], itertools.repeat(1000.0))  # the time runs out after the first project
        with mock.patch.object(registry.time, "monotonic", side_effect=lambda: next(clock)):
            found = registry.workspace_baseline()
        self.assertEqual(found, {"projects": {OWN: self.identity(self.own)}, "partial": True})


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


NEW = "signature-organization/widget"


class WorkspaceArrivalTests(WorkspaceScopeCase):
    """A checkout the session brought into the workspace (cloned, moved or replaced after it started) is no project
    of the workspace for automatic policy: the developer's typed grant opens it."""

    _m = staticmethod(WorkspaceAuthorizationTests._m)

    def assert_arrived(self, cwd, repo, cmd="git push origin feat/x", grant="push feat/x", at=2):
        denied = self.verdict(cmd, cwd, at)
        self.assertIsNotNone(denied, f"{cmd} in {cwd}")
        self.assertIn("was not in devkit's project list when this session started", denied[1])
        self.assertIn(f"grant {grant} --repo {repo}", denied[1])
        self.assertEqual(self.records()[-1]["ref"]["result"], "deny")
        return denied

    def test_a_checkout_from_before_the_session_runs_with_no_grant(self):
        self.assertIsNone(self.verdict("git push origin feat/x", self.other))
        self.assertEqual(self.records()[-1]["ref"]["source"], "policy-auto")

    def test_a_checkout_cloned_during_the_session_needs_the_grant_and_opens_with_it(self):
        new = self.checkout(self.dev / NEW, f"git@github.com:{NEW}.git")
        self.listed(OWN, OTHER, NEW)  # a sync during the session changes nothing: the list is the session start's
        self.assertEqual(registry.workspace_checkout(NEW), self.dev / NEW, "a well-formed workspace checkout")
        self.assert_arrived(new, NEW)
        self.fixture.typed(f"grant push feat/x --repo {NEW}", self._m(3))
        self.assertIsNone(self.verdict("git push origin feat/x", new, 4))
        self.assertEqual(self.records()[-1]["ref"]["source"], "E2")

    def test_an_existing_checkout_moved_to_a_new_project_folder_needs_the_grant(self):
        # the creation time of a git folder survives a move, so only the list of the session start tells
        moved = self.dev / "signature-organization" / "new-repo"
        os.rename(self.other, moved)
        self.git(moved, "remote", "set-url", "origin", "git@github.com:signature-organization/new-repo.git")
        self.assertEqual(registry.workspace_checkout("signature-organization/new-repo"), moved)
        started = json.loads(self.fixture.state.sess.read_text())["started"]
        self.assertLess(os.lstat(moved / ".git").st_birthtime, started, "its creation time predates the session")
        self.assert_arrived(str(moved), "signature-organization/new-repo")
        self.assert_arrived(self.own, "signature-organization/new-repo",
                            cmd="gh pr merge 12 -R signature-organization/new-repo", grant="merge 12")

    def test_a_checkout_replaced_at_its_place_needs_the_grant(self):
        shutil.rmtree(self.other)
        self.checkout(self.dev / OTHER, f"git@github.com:{OTHER}.git")
        self.assert_arrived(self.other, OTHER)

    def test_a_session_record_without_the_list_needs_the_grant(self):
        # an older devkit wrote no list, or one that did not say whether it was whole: the creation time of a git
        # folder can be set back, so nothing else tells a checkout from before the session
        ident = os.lstat(Path(self.other) / ".git")
        for listed in (None, {OTHER: [ident.st_dev, ident.st_ino]}, {"projects": "all"}):
            with self.subTest(listed=listed):
                self.session(workspace_checkouts=listed)
                denied = self.assert_arrived(self.other, OTHER)
                self.assertIn("holds no project list from its start", denied[1])

    def test_a_project_the_partial_list_did_not_reach_needs_the_grant(self):
        self.listed(OTHER, OWN)
        with mock.patch.object(registry, "WORKSPACE_LIST_MAX", 1):
            self.session(workspace_checkouts=registry.workspace_baseline())
        self.assertIsNone(self.verdict("git push origin feat/x", self.other), "the part it read is covered")
        self.listed(OWN, OTHER)
        with mock.patch.object(registry, "WORKSPACE_LIST_MAX", 1):
            self.session(workspace_checkouts=registry.workspace_baseline())
        denied = self.assert_arrived(self.other, OTHER, at=3)
        self.assertIn("stopped before it", denied[1])

    def test_a_new_session_id_after_a_compaction_keeps_the_first_list(self):
        first = json.loads(self.fixture.state.sess.read_text())
        new = self.checkout(self.dev / NEW, f"git@github.com:{NEW}.git")
        # Claude Code gave the conversation a new id (s1) after the clone: its record lists the clone, and the record
        # it superseded (s0) holds the list of the conversation's start
        self.session("s0", started=first["started"] - 600, superseded_by="s1", project=OWN,
                     workspace_checkouts=first["workspace_checkouts"])
        self.listed(OWN, OTHER, NEW)
        self.session(started=time.time(), workspace_checkouts=registry.workspace_baseline())
        self.assert_arrived(new, NEW)
        self.assertIsNone(self.verdict("git push origin feat/x", self.other, 3))

    def test_a_program_the_guard_cannot_classify_runs_in_a_workspace_project_with_its_record(self):
        cmd = "$(echo gh) pr merge 12"
        acts = guard.irreversible_actions(cmd, self.other)
        self.assertEqual([(a["action"], a["repo"]) for a in acts], [("unknown", OTHER)])
        self.assertIsNone(self.verdict(cmd, self.other))
        record = self.records()[-1]
        self.assertEqual((record["outcome"], record["ref"]["source"], record["ref"]["irreversible"]),
                         ("ok", "policy-auto", "unknown"))
        self.assertEqual((record["ref"]["repo"], record["ref"]["origin_repo"], record["ref"]["kind"]),
                         (OTHER, OWN, "unclassified:opaque"))

    def test_a_program_the_guard_cannot_classify_keeps_the_rules_of_the_workspace(self):
        cmd = "$(echo gh) pr merge 12"
        target = guard.irreversible_actions(cmd, self.other)[0]["target"]
        new = self.checkout(self.dev / NEW, f"git@github.com:{NEW}.git")
        outside = self.checkout(self.root / "elsewhere" / "svc", "https://github.com/acme/outside.git")
        for k, (cwd, why) in enumerate(((new, "was not in devkit's project list when this session started"),
                                        (outside, "not a project of your workspace"))):
            with self.subTest(cwd=cwd):
                denied = self.verdict(cmd, cwd, 2 + 3 * k)
                self.assertIsNotNone(denied)
                self.assertIn(why, denied[1])
                import re
                line = re.search(r"typing this line on its own: `(grant unknown [^`]+)`", denied[1]).group(1)
                self.assertTrue(line.startswith(f"grant unknown {target}"), line)
                self.fixture.typed(line, self._m(3 + 3 * k))  # the line the refusal names opens that command
                self.assertIsNone(self.verdict(cmd, cwd, 4 + 3 * k))
                self.assertEqual(self.records()[-1]["ref"]["source"], "E2")
        self.config = {"work": {"mode": "guided"}}
        self.assertIsNotNone(self.verdict(cmd, self.other), "guided mode asks the developer")
        self.config = {"work": {"mode": "auto"}}
        why = guard._member_gate(cmd, self.other, "tank", "a_tank", "claude", "s1", None, self._m(2))
        self.assertIsNotNone(why, "a member routes it to the tech lead")


class WorkspaceDecisionCostTests(WorkspaceScopeCase):
    """One guard verdict asks the workspace about a repository once; tracing a conversation's first session record
    reads only the records written since it began, and refuses when it cannot finish."""

    def count(self, name):
        calls = []
        real = getattr(registry, name)
        patch = mock.patch.object(registry, name, side_effect=lambda *a: calls.append(a) or real(*a))
        patch.start()
        self.addCleanup(patch.stop)
        return calls

    def test_one_verdict_reads_the_checkout_of_a_repository_once(self):
        checks = self.count("workspace_checkout")
        self.assertIsNone(self.verdict("git push origin feat/x", self.other))
        self.assertEqual(len(checks), 1, "authorization_decision runs in the gate and twice in use_grants")
        self.assertIsNone(self.verdict("git push origin feat/x", self.other, 3))
        self.assertEqual(len(checks), 2, "the next verdict reads it again")

    def test_a_change_between_verdicts_counts_at_the_next_one(self):
        self.assertIsNone(self.verdict("git push origin feat/x", self.other))
        self.git(self.other, "remote", "set-url", "origin", "https://github.com/attacker/x.git")
        self.assertIsNotNone(self.verdict("git push origin feat/x", self.other, 3))

    def records_since(self, began, stale=40):
        """`stale` other records last written before the session began, and their paths."""
        paths = []
        for k in range(stale):
            path = pg.session_path("claude", f"old{k}")
            path.write_text(json.dumps({"agent": "claude", "session": f"old{k}", "started": began - 7200,
                                        "superseded_by": f"old{k + 1}"}))
            os.utime(path, (began - 3600, began - 3600))
            paths.append(str(path))
        return paths

    def test_the_first_record_is_traced_through_records_written_since_the_session_began(self):
        first = json.loads(self.fixture.state.sess.read_text())
        stale = self.records_since(first["started"])
        self.session("s0", started=first["started"] - 600, superseded_by="s1", project=OWN,
                     workspace_checkouts={"projects": {}, "partial": False})
        read = []
        real = core.read_json
        with mock.patch.object(core, "read_json", side_effect=lambda p, *a: read.append(str(p)) or real(p, *a)):
            found = pg._first_record("claude", "s1", first)
        self.assertEqual(found["session"], "s0")
        self.assertEqual([p for p in read if p in stale], [], "records last written before the session are not read")

    def test_a_trace_past_its_bound_refuses_with_the_reason(self):
        first = json.loads(self.fixture.state.sess.read_text())
        for k in range(3):
            self.session(f"busy{k}", started=first["started"])
        with mock.patch.object(pg, "FIRST_RECORD_READS", 2):
            self.assertIsNone(pg._first_record("claude", "s1", first))
            denied = self.verdict("git push origin feat/x", self.other)
        self.assertIsNotNone(denied)
        self.assertIn("could not trace this conversation's first session record", denied[1])
        self.assertIn(f"grant push feat/x --repo {OTHER}", denied[1])


class UnknownGrantLineTests(unittest.TestCase):
    """The grant line a refusal of a program the guard cannot classify names, for each kind of repository."""

    ODD = "gitlab.com/a/b/c/d/e/f/g"  # deeper than a typed --repo may name

    def reason(self, kind, repo):
        why = {"unread": ("unread", "/tmp/big.sh"), "built": ("built", "getattr"),
               "unsure": ("unsure", "a GraphQL query"), "opaque": None}[kind]
        return guard._main_reason({"action": "unknown", "why": why, "words": "x", "target": "x-1", "repo": repo,
                                   "tier": 2})

    def test_a_repository_a_typed_line_cannot_name_says_where_to_type_it(self):
        for kind in ("unread", "built", "unsure", "opaque"):
            with self.subTest(kind=kind):
                why = self.reason(kind, self.ODD)
                self.assertIn("`grant unknown x-1`", why)
                self.assertIn(f"{self.ODD} is neither: type the line in a session started in {self.ODD}", why)

    def test_a_named_repository_or_this_mac_needs_no_such_note(self):
        for kind in ("unread", "opaque"):
            with self.subTest(kind=kind):
                self.assertIn("`grant unknown x-1 --repo acme/api`", self.reason(kind, "acme/api"))
                self.assertNotIn("is neither", self.reason(kind, "acme/api"))
                self.assertNotIn("is neither", self.reason(kind, pg.LOCAL_SCOPE))


if __name__ == "__main__":
    unittest.main()
