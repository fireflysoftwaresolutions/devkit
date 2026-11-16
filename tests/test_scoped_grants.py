# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
import contextlib
import io
import json
from unittest import mock

from tests.test_evidence import EvidenceCase, m
from tests.test_codex_grants import CodexCase, SID, T0
from devkit import audit, cli, core, crew, guard, progress as pg


class ScopedGrants(EvidenceCase):
    def use(self, repo="acme/web", **more):
        return pg.use_grant("claude", "s1", action="release", target="v26.11.4", repo=repo, now=m(3), **more)

    def test_typed_scope_opens_only_named_repository_once(self):
        entries = self.typed("grant release v26.11.4 --repo acme/web", m(1))
        self.assertIn("--repo acme/web", pg.evidence_lines(entries))
        self.assertIsNone(self.use("acme/api"))
        self.assertIsNotNone(self.use())
        self.assertIsNone(self.use())
        self.assertEqual(json.loads(self.state.sess.read_text())["project"], "acme/api")

    def test_confirmation_rejects_tampered_scope_action_and_target(self):
        entries = self.typed("grant release v26.11.4 --repo acme/web", m(1))
        self.assertEqual(len(entries), 1)
        self.assertTrue(pg.confirmed(entries[0], "s1"))
        for changed in ({"repo": "acme/api"}, {"covers": "merge v26.11.4"}, {"covers": "release v26.11.5"}):
            with self.subTest(changed=changed):
                self.assertFalse(pg.confirmed(dict(entries[0], **changed), "s1"))
        rec = next(r for r in audit.query(action="crew.evidence"))
        self.assertIsNone(pg.evidence_check(rec, "", 1))
        rec["ref"]["repo"] = "acme/api"
        self.assertIsNotNone(pg.evidence_check(rec, "", 1))

    def test_default_scope_is_verified_against_the_startup_project(self):
        entries = self.typed("grant release v26.11.4", m(1))
        self.assertFalse(pg.confirmed(dict(entries[0], repo="acme/web"), "s1"))
        self.assertIsNone(self.use())
        self.assertIsNotNone(self.use("acme/api"))

    def test_decision_scope_is_in_exact_question_and_performing_label(self):
        d = pg.crew_decision("Release v26.11.4 for acme/web?", options="yes | no", recommend="yes",
                             irreversible="release", target="v26.11.4", repo="acme/web", now=m(1))
        self.assertIn("--repo acme/web", pg.ask_text(d))
        labels = pg.ask_labels(d)
        self.assertIn("--repo acme/web", labels[0])
        self.answered(pg.ask_text(d), labels[0], m(2), options=labels, header=d["id"])
        self.assertTrue(pg.crew_decide(d["id"], "yes", now=m(2) + 1)["verified"])
        record = next(r for r in audit.query(action="crew.evidence"))
        self.assertIsNone(pg.evidence_check(record, "", 1))
        record["ref"]["repo"] = "acme/api"
        self.assertIsNotNone(pg.evidence_check(record, "", 1))
        self.assertIsNone(self.use("acme/api"))
        self.assertIsNotNone(self.use())
        self.assertIsNone(self.use())

    def test_invalid_explicit_scope_never_records_a_decision(self):
        for repo in ("", "acme", "acme/*", "../web", "https://github.com/acme/web", "acme/web --force"):
            with self.subTest(repo=repo), self.assertRaises(pg.StepError):
                pg.crew_decision("Release v26.11.4?", irreversible="release", target="v26.11.4", repo=repo)
        self.assertEqual(pg.load_run("claude", "s1").get("decisions", []), [])

    def test_a_repository_on_another_git_host_can_be_named(self):
        # devkit names a repository on another host by host and path (git.example.com/acme/api, a GitLab group path)
        for repo in ("git.example.com/acme/api", "gitlab.com/group/sub/api"):
            with self.subTest(repo=repo):
                self.assertEqual(pg._parse_grant(f"grant push feat/x --repo {repo}"), ("push", "feat/x", repo))
        for repo in ("git.example.com/acme/../api", "git.example.com//api", "/acme/api", "a/b/c/d/e/f/g/h"):
            with self.subTest(repo=repo):
                self.assertIsNone(pg._parse_grant(f"grant push feat/x --repo {repo}"))

    def test_invalid_typed_scopes_are_rejected_and_valid_scope_still_expires(self):
        for suffix in ("", "acme", "acme/*", "../web", "https://github.com/acme/web", "acme/web --repo acme/api"):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.typed(f"grant release v26.11.4 --repo {suffix}", m(1)), [])
        entry = self.typed("grant release v26.11.4 --repo acme/web", m(2))[0]
        self.assertIsNotNone(self.use(consume=False))
        self.assertIsNone(pg.use_grant("claude", "s1", action="release", target="v26.11.4", repo="acme/web",
                                      now=entry["expires"] + 1))

    def test_same_prompt_grants_are_consumed_in_the_selected_scope(self):
        self.typed("grant release v26.11.4 --repo acme/api\ngrant release v26.11.4 --repo acme/web", m(1))
        self.assertNotEqual(self.use(consume=False)["id"], self.use("acme/api", consume=False)["id"])
        actions = [{"action": "release", "target": "v26.11.4", "repo": repo, "tier": 2,
                    "words": "dev release --version 26.11.4"} for repo in ("acme/web", "acme/api")]
        with mock.patch.object(core, "load_config", return_value={"safeguards": {"irreversible_main": "enforce"}}):
            self.assertIsNone(guard._main_gate(actions, "claude", "s1", None, m(2)))
        self.assertIsNone(self.use())
        self.assertIsNone(self.use("acme/api"))

    def test_cross_project_grant_cannot_become_a_startup_project_preapproval(self):
        self.typed("grant merge 12 --repo acme/web", m(1))
        run = pg.set_mode("auto", approve="merge 12", now=m(2))
        self.assertEqual(run.get("preapprovals", []), [])
        pg.set_mode("guided", now=m(2.5))
        self.assertIsNotNone(pg.use_grant("claude", "s1", action="merge", target="12", repo="acme/web", now=m(3)))

    @mock.patch.object(core, "sh", return_value=(0, '{"login":"alice","id":123}', ""))
    def test_auto_decisions_require_exact_scope_but_no_human_preapproval(self, network):
        self.typed("grant merge 12", m(1))
        pg.set_mode("auto", approve="merge 12", now=m(2))
        for repo, target in (("acme/web", "12"), ("acme/api", "")):
            with self.subTest(repo=repo, target=target), self.assertRaises(pg.StepError):
                pg.crew_decision("Merge the requested PR?", options="yes | no", recommend="yes",
                                 irreversible="merge", target=target, repo=repo, now=m(3))
        same = pg.crew_decision("Merge 12?", options="yes | no", recommend="yes", irreversible="merge",
                                target="12", repo="acme/api", now=m(3))
        self.assertEqual((same["state"], same["by"]), ("decided", "auto"))
        declined = pg.crew_decision("Merge 12?", options="yes | no", recommend="no", irreversible="merge",
                                    target="12", repo="acme/api", now=m(3))
        self.assertEqual((declined["state"], declined["answer"]), ("decided", "no"))
        self.assertFalse(pg.approves_action(declined))
        expired = pg.crew_decision("Merge 12?", options="yes | no", recommend="yes", irreversible="merge",
                                   target="12", repo="acme/api", now=m(2 + 8 * 60 + 1))
        self.assertEqual(expired["state"], "decided")
        self.assertIsNotNone(pg.use_grant("claude", "s1", action="merge", target="12", repo="acme/api", now=m(4)))
        exhausted = pg.crew_decision("Merge 12?", options="yes | no", recommend="yes", irreversible="merge",
                                     target="12", repo="acme/api", now=m(5))
        self.assertEqual(exhausted["state"], "decided")

    def test_cli_records_scope_without_changing_the_run_project(self):
        args = cli.build_parser().parse_args(["crew", "ask", "--decision", "--irreversible", "release",
                                              "--target", "v26.11.4", "--repo", "acme/web", "Release v26.11.4?"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(crew._ask_decision(args, None), 0)
        run = pg.load_run("claude", "s1")
        self.assertEqual((run["project"], run["decisions"][0]["repo"]), ("acme/api", "acme/web"))

    def test_decision_scope_cannot_change_after_the_question_was_answered(self):
        d = pg.crew_decision("Release v26.11.4?", options="yes | no", recommend="yes", irreversible="release",
                             target="v26.11.4", repo="acme/web", now=m(1))
        labels = pg.ask_labels(d)
        self.answered(pg.ask_text(d), labels[0], m(2), options=labels, header=d["id"])
        run = pg.load_run("claude", "s1")
        run["decisions"][0]["repo"] = "acme/api"
        pg._save(run, touch_session=False)
        with self.assertRaises(pg.StepError):
            pg.crew_decide(d["id"], "yes", now=m(2) + 1)

    def test_a_host_qualified_scope_gets_a_grant_line_that_works(self):
        # a repository on another git host is named by host and path: the refusal's line, typed, opens exactly it
        scope = "gitlab.example/team/subgroup/api"
        why = guard._main_reason({"action": "release", "target": "v26.11.4", "repo": scope, "tier": 2,
                                  "words": "dev release --version 26.11.4"})
        self.assertIn(f"grant release v26.11.4 --repo {scope}", why)
        self.assertTrue(self.typed(f"grant release v26.11.4 --repo {scope}", m(1)))
        self.assertIsNone(pg.use_grant("claude", "s1", action="release", target="v26.11.4", repo="acme/api",
                                       consume=False, now=m(2)))
        self.assertIsNotNone(pg.use_grant("claude", "s1", action="release", target="v26.11.4", repo=scope, now=m(2)))

    def test_mismatch_diagnostic_names_both_scopes_without_consuming(self):
        self.typed("grant release v26.11.4", m(1))
        action = {"action": "release", "target": "v26.11.4", "repo": "acme/web", "tier": 2,
                  "words": "dev release --version 26.11.4"}
        with mock.patch.object(core, "load_config", return_value={"safeguards": {"irreversible_main": "enforce"}}):
            denied = guard._main_gate([action], "claude", "s1", None, m(2))
        self.assertIn("recorded approval scope: acme/api", denied[1])
        self.assertIn("current execution scope: acme/web", denied[1])
        self.assertIn("grant release v26.11.4 --repo acme/web", denied[1])
        self.assertIsNotNone(self.use("acme/api"))


class ScopedCodexGrants(CodexCase):
    def test_codex_confirmation_and_audit_bind_the_scope(self):
        entries = self.typed("grant release v26.11.9 --repo acme/web", T0)
        self.assertEqual(len(entries), 1)
        self.assertTrue(pg.confirmed(entries[0], SID))
        self.assertFalse(pg.confirmed(dict(entries[0], repo="acme/api"), SID))
        rec = next(r for r in audit.query(action="crew.evidence"))
        self.assertIsNone(pg.evidence_check(rec, "", 1))
        rec["ref"]["repo"] = "acme/api"
        self.assertIsNotNone(pg.evidence_check(rec, "", 1))
        rec["ref"].pop("repo")
        self.assertIsNotNone(pg.evidence_check(rec, "", 1))
