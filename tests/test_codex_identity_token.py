# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The call token on Codex (the CLI and the ChatGPT app): where Codex never asks, every exec carries the token as on
# Claude Code; where it asks, only one plain devkit call does, in its variable-list form, and the token never costs a
# rewrite Codex would apply. A tokenless command may name its lane by CODEX_THREAD_ID, and the root thread maps to the
# tech lead only after a member's token call showed that members carry their own thread id. The payloads are Codex
# 0.167's exec_command (`cmd`, `workdir`, `yield_time_ms`, `timeout_ms`, `shell`), with the thread ids and rollout
# records of tests/codexrollout.py.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_codex_identity_token -v
import contextlib
import io
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
sys.path.insert(0, str(ROOT / "lib"))

from devkit import audit, hooks, identity, state  # noqa: E402
from devkit import progress as pg  # noqa: E402
from codexrollout import OTHER, SID, SUB, Rollout  # noqa: E402
from test_identity_token import LANE, TokenCase  # noqa: E402
from test_progress import launch  # noqa: E402

SUB2 = "01a1c19a-9a8b-7c6d-8e5f-4a3b2c1d0e9f"  # a second spawned agent of the same thread
CODEX = 70101


class CodexTokenCase(TokenCase):
    agent = "codex"
    agent_line = "codex"
    agent_pid = CODEX
    never_asks = True

    def shell_line(self, new: str) -> str:
        # the exec's shell as ps shows it: Codex runs the command with the user's shell, -lc
        return f"/bin/zsh -lc {new}"

    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(hooks, "_codex_never_asks", lambda data: self.never_asks),
                  mock.patch.dict(os.environ, {"DEVKIT_SESSION": f"codex:{SID}", "SHELL": "/bin/zsh"})):
            p.start()
            self.addCleanup(p.stop)
        (pg.SESSIONS / f"codex-{SID}.json").write_text(json.dumps({"agent": "codex", "session": SID,
                                                                    "started": time.time()}))
        with mock.patch.object(pg, "caller", lambda *a, **k: {"caller": "main", "how": "test"}):
            pg.start("Codex work", plan=["api", "ui"])
            pg.assign("api", "Tank", why="the test's assignment")
        launch(SUB, "tank", "Tank: api", sid=SID, agent="codex")

    def exec(self, cmd, aid=None, **inp):
        return self.hook(cmd, aid=aid, sid=SID, tool="exec_command", key="cmd", **inp)

    def run_(self, sid=SID, agent="codex"):
        return pg.load_run(agent, sid)

    def sess(self):
        return state.read(pg.session_path("codex", SID)) or {}

    def thread(self, tid, token="", **env):
        return {identity.THREAD_VAR: tid, **env}


class CodexStampTests(CodexTokenCase):
    def test_codex_rewrite_carries_call_token_when_never_asks(self):
        _, token, new = self.exec("git status")
        self.assertTrue(new.startswith(f" export DEVKIT_CALL={token}; readonly DEVKIT_CALL; "), new)
        self.assertEqual(self.last.get("permissionDecision"), "allow")
        rc, out = self.lead(token)
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.added())

    def test_codex_plain_call_token_under_asking_policy(self):
        self.never_asks = False
        _, token, new = self.exec("dev progress add extra")
        self.assertTrue(new.startswith(f"DEVKIT_SPEAKER=main DEVKIT_SESSION=codex:{SID} DEVKIT_CALL={token} dev "), new)
        self.assertEqual(identity.strip_token(new), f"DEVKIT_SPEAKER=main DEVKIT_SESSION=codex:{SID} dev progress add "
                                                    f"extra")
        self.assertTrue(hooks._plain_devkit_call(new))
        self.assertEqual(self.last.get("permissionDecision"), "allow")
        rc, out = self.lead(token)
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.added())

    def test_codex_member_lane_route_keeps_its_rewrite_with_the_token_or_without_it(self):
        self.never_asks = False
        _, token, new = self.exec("dev progress note 'api half done'", aid=SUB)
        self.assertTrue(token and f"DEVKIT_FROM_AGENT={SUB}" in new, new)
        self.assertEqual(self.last.get("permissionDecision"), "allow")
        with mock.patch.object(hooks, "_DEVKIT_VARIABLE", hooks.re.compile(
                r"(?:DEVKIT_FROM_AGENT|DEVKIT_SESSION|DEVKIT_SPEAKER)=[A-Za-z0-9_.:-]+\Z")):
            _, token, new = self.exec("dev progress note 'api done'", aid=SUB)
        self.assertEqual(token, "")
        self.assertIn(f"DEVKIT_FROM_AGENT={SUB}", new)  # the lane route still goes out
        self.assertEqual(self.last.get("permissionDecision"), "allow")
        self.assertEqual(len(self.records("calls.token-skipped")), 1)

    def test_codex_fish_shell_gets_no_readonly(self):
        _, token, new = self.exec("git status", shell="/opt/homebrew/bin/fish")
        self.assertEqual((token, new), ("", ""))
        _, token, new = self.exec("dev crew inbox", shell="/opt/homebrew/bin/fish")
        self.assertIn(f" DEVKIT_CALL={token} dev crew inbox", new)
        self.assertNotIn("readonly", new)

    def test_codex_exec_entry_dies_at_its_yield(self):
        data, token, _ = self.exec("dev ci --run", yield_time_ms=10_000, timeout_ms=3_600_000)
        self.assertTrue(token)
        self.assertEqual(pg.call_entry(data)["ttl"], 15.0)


class CodexThreadTests(CodexTokenCase):
    never_asks = False  # Codex asks: a compound command goes out as it is, with no token

    def test_codex_root_thread_without_evidence_refuses_with_the_plain_call_fix(self):
        _, token, new = self.exec("dev progress add extra | tail -3")
        self.assertEqual((token, new), ("", ""))
        rc, out = self.lead(env=self.thread(SID))
        self.assertEqual(rc, 2, out)
        self.assertIn("run it as one plain dev command", out)
        self.assertFalse(self.added())

    def test_codex_member_token_with_the_root_thread_turns_the_root_mapping_off(self):
        self.never_asks = True
        _, token, _ = self.exec("dev crew inbox", aid=SUB)
        self.assertEqual(self.dev("crew", "inbox", token=token, env=self.thread(SID))[0], 0)
        self.assertEqual(self.sess().get("codex_threads"), "shared")
        self.assertEqual(len(self.records("codex.thread-shared")), 1)
        rc, out = self.lead(env=self.thread(SID))
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())

    def test_codex_member_token_with_its_own_thread_turns_the_root_mapping_on(self):
        _, token, _ = self.exec("dev crew inbox", aid=SUB)  # one plain call: its token in the variable-list form
        self.assertTrue(token)
        self.assertEqual(self.dev("crew", "inbox", token=token, env=self.thread(SUB))[0], 0)
        self.assertEqual(self.sess().get("codex_threads"), "per-agent")
        self.exec("dev progress add extra | tail -3")  # the tech lead's own open call holds the command
        rc, out = self.lead(env=self.thread(SID))
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.added())

    def test_codex_member_token_with_another_lanes_thread_is_a_conflict(self):
        self.never_asks = True
        launch(SUB2, "trinity", "Trinity: ui", sid=SID, agent="codex")
        _, token, _ = self.exec("dev progress note x", aid=SUB)
        rc, out = self.dev("progress", "note", "x", token=token, env=self.thread(SUB2))
        self.assertEqual(rc, 2, out)
        self.assertEqual(len(self.records("caller.conflict")), 1)

    def test_codex_compound_command_uses_the_thread_rule_for_a_lane(self):
        _, token, new = self.exec("dev progress note 'api half done' | tail -1", aid=SUB)
        self.assertEqual(token, "")
        rc, out = self.dev("progress", "note", "api half done", env=self.thread(SUB))
        self.assertEqual(rc, 0, out)
        self.assertIn("api half done", [u.get("text") for u in self.run_()["agents"][SUB].get("updates") or []])

    def test_codex_unknown_thread_refuses_a_state_change_with_the_plain_call_fix(self):
        self.exec("dev progress add extra | tail -3")
        rc, out = self.lead(env=self.thread(OTHER))
        self.assertEqual(rc, 2, out)
        self.assertIn("run it as one plain dev command", out)
        self.assertEqual(len(self.records("caller.no-token")), 1)


class CodexCallLifeTests(CodexTokenCase):
    def test_codex_yielded_exec_token_dies_at_post_tool_use(self):
        data, token, _ = self.exec("sleep 60; dev progress add extra", yield_time_ms=10_000)
        self.closed(data)  # the exec yielded and its PostToolUse came; it goes on in a write_stdin session
        rc, out = self.lead(token)
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("caller.stale-token")), 1)

    def test_codex_command_execution_thread_mismatch_is_audited(self):
        data, token, new = self.exec("dev crew inbox")
        self.assertEqual(self.speak(token).lane, "main")  # its dev call resolved the tech lead
        self.assertTrue(identity.used_path(SID, data["tool_use_id"]).exists())
        rollout = Rollout(self.root / ".codex", SID)
        rollout.add("event_msg", {"type": "item_completed", "thread_id": SUB, "turn_id": "t1",
                                  "item": {"type": "CommandExecution", "id": data["tool_use_id"], "exit_code": 0,
                                           "status": "completed", "command": ["/bin/zsh", "-lc", new],
                                           "aggregated_output": ""}}, time.time())
        with contextlib.redirect_stdout(io.StringIO()):
            hooks.post_tool_payload("codex", dict(data, hook_event_name="PostToolUse", transcript_path=str(
                rollout.path), tool_input={"cmd": new}, tool_response={"output": ""}))
        found = self.records("caller.mismatch")
        self.assertEqual(len(found), 1)
        self.assertIn(SUB, found[0]["detail"])

    def test_codex_tech_leads_token_resolves_main_while_a_members_call_holds_the_same_words(self):
        self.exec("python3 -I -c \"print('dev progress add extra')\"", aid=SUB)
        _, token, _ = self.exec("dev progress add extra 2>&1 | tail -3")
        rc, out = self.lead(token)
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.added())

    def test_codex_ack_with_a_nested_read_resolves_the_tech_lead(self):
        ref = pg.crew_ask("Report: " + "x" * 400, from_agent=SUB)["id"]
        _, token, new = self.exec(f'dev crew ack {ref} --sha256 "$(dev crew read {ref} --json | jq -r .digest)" '
                                  f'--generation 1')
        self.assertIsNone(LANE.search(new))
        rc, out = self.dev("crew", "read", ref, "--json", token=token)
        self.assertEqual(rc, 0, out)
        page = json.loads(out)
        rc, out = self.dev("crew", "ack", ref, "--sha256", page["digest"], "--generation", str(page["generation"]),
                           token=token)
        self.assertEqual(rc, 0, out)
        self.assertIn("main", next(m for m in self.run_()["messages"] if m["id"] == ref).get("receipts") or {})


if __name__ == "__main__":
    unittest.main()
