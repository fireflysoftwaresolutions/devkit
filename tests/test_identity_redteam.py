# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The Red team tester's probes of the call token (2026-11-19), one test each: what a crew member tried to pass as the
# tech lead or as another member, and must not. On Codex: forging CODEX_THREAD_ID after dropping its token, under
# either approval policy. On Claude Code: taking the tech lead's token from the process list and using it first from
# its own shell, and running the tech lead's token below a nested agent devkit did not recognize. The fixtures are
# tests.test_identity_token's (Claude Code) and tests.test_codex_identity_token's (Codex 0.167 exec payloads).
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_identity_redteam -v
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
sys.path.insert(0, str(ROOT / "lib"))

from devkit import core, identity  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_codex_identity_token import SID, SUB, SUB2, CodexTokenCase  # noqa: E402
from test_identity_token import AGENT, INNER, NESTED, TokenCase  # noqa: E402
from test_progress import launch  # noqa: E402

NPX_CODEX = "node /Users/u/.npm/_npx/9f/node_modules/.bin/codex exec review"


class CodexThreadForgeryTests(CodexTokenCase):
    def per_agent(self):
        # a member's honest token call with its own thread id: devkit learns that members carry their own ids
        _, token, _ = self.exec("dev crew inbox", aid=SUB)
        self.assertEqual(self.dev("crew", "inbox", token=token, env=self.thread(SUB))[0], 0)
        self.assertEqual(self.sess().get("codex_threads"), "per-agent")

    def test_asking_policy_a_member_forging_the_root_thread_is_refused_and_audited(self):
        self.per_agent()
        self.never_asks = False
        self.exec("python3 x.py; true", aid=SUB)  # compound: Codex may ask, so no rewrite and no token
        rc, out = self.lead(env=self.thread(SID))  # x.py runs dev with CODEX_THREAD_ID=<root>
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("caller.no-token")), 1)

    def test_never_asks_a_member_dropping_its_token_and_forging_the_root_is_refused(self):
        self.per_agent()
        _, token, _ = self.exec("python3 x.py", aid=SUB)  # stamped; x.py drops DEVKIT_CALL
        self.assertTrue(token)
        rc, out = self.lead(env=self.thread(SID))
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())

    def test_a_member_forging_another_lanes_thread_cannot_close_its_step(self):
        with mock.patch.object(pg, "caller", lambda *a, **k: {"caller": "main", "how": "test"}):
            pg.assign("ui", "Trinity", why="the test's assignment")
        launch(SUB2, "trinity", "Trinity: ui", sid=SID, agent="codex")
        for never_asks in (True, False):
            with self.subTest(never_asks=never_asks):
                self.never_asks = never_asks
                self.exec("python3 x.py", aid=SUB)  # x.py drops the token and the lane variable
                rc, out = self.dev("progress", "step", "ui finished", "--step", "ui", "--check", "✓ all green",
                                   env=self.thread(SUB2))
                self.assertEqual(rc, 2, out)
                self.assertEqual(next(s for s in self.run_()["plan"] if s["name"] == "ui")["state"], "running")

    def test_the_thread_rule_takes_a_lane_only_with_its_own_open_call_and_audits_it(self):
        self.never_asks = False
        self.exec("dev progress note 'api half done' | tail -1", aid=SUB)  # the member's own open call
        rc, out = self.dev("progress", "note", "api half done", env=self.thread(SUB))
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.records("caller.thread")), 1)
        rc, out = self.dev("progress", "note", "something else", env=self.thread(SUB))  # no call of its holds it
        self.assertEqual(rc, 2, out)

    def test_a_members_rewrite_never_carries_the_roots_session_id(self):
        for never_asks, cmd in ((False, "dev progress note x"), (True, "dev crew inbox"), (True, "git status")):
            with self.subTest(never_asks=never_asks, cmd=cmd):
                self.never_asks = never_asks
                _, _, new = self.exec(cmd, aid=SUB)
                self.assertNotIn(SID, new)
        _, _, new = self.exec("dev crew inbox")  # the main thread's keeps it
        self.assertIn(f"DEVKIT_SESSION=codex:{SID}", new)


class ClaudeTokenTheftTests(TokenCase):
    def test_a_member_using_the_tech_leads_token_first_from_its_own_shell_is_refused(self):
        self.plan_with_link()
        _, token, _ = self.hook("pytest -q tests")  # the tech lead's long call runs no dev command
        self.hook("python3 do.py", aid="a_link")  # the member's script reads the stamp from ps
        self.chain = [(self.shell, self.lines[self.shell]), (AGENT, "claude")]  # the member's own shell
        rc, out = self.lead(token)
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("caller.token-foreign-use")), 1)
        self.chain = None  # the tech lead's own call, later: still the tech lead's
        rc, out = self.lead(token)
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.added())

    def test_agents_started_through_node_and_other_hosts_are_agents(self):
        for line in (NPX_CODEX, "node /Users/u/.npm/_npx/9f/node_modules/.bin/claude -p review",
                     "gemini -p review", "opencode run review", "/opt/homebrew/bin/gemini", "node /x/bin/opencode"):
            with self.subTest(line):
                self.assertTrue(core.is_agent_process(line))
        self.assertFalse(core.is_agent_process("node /x/node_modules/.bin/vite"))

    def test_the_tech_leads_token_below_an_npx_codex_is_refused(self):
        self.plan_with_link()
        _, token, _ = self.hook("npx -y @openai/codex exec 'review the diff'")
        own = self.parents_of(token)
        self.chain = [(INNER, "/bin/zsh -lc dev"), (NESTED, NPX_CODEX)] + own
        rc, out = self.lead(token)
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("caller.foreign-agent")), 1)

    def test_a_claude_token_carried_by_a_codex_thread_is_refused(self):
        self.plan_with_link()
        _, token, _ = self.hook("codex exec 'review the diff'")
        rc, out = self.lead(token, env={identity.THREAD_VAR: "01a1f737-3cd7-7000-8000-000000000001"})
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("caller.foreign-agent")), 1)

    def test_a_tokenless_member_cannot_reorder_launches(self):
        self.plan_with_link()
        self.hook("dev crew next --first ci-redirect --why urgent")  # the tech lead's open call
        self.call("python3 -I x.py", aid="a_link")  # a member's call without its token
        who = pg.caller(argv=["crew", "next", "--first", "ci-redirect", "--why", "urgent"])
        self.assertEqual(who.get("caller"), "unknown")
        self.assertIn("could not verify", who.get("why") or "")

    def parents_of(self, token):
        with mock.patch.dict(os.environ, {identity.CALL_VAR: token}):
            return self.parents()


if __name__ == "__main__":
    unittest.main()
