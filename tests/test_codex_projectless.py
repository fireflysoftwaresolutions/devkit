# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Regressions for a Codex app chat whose host cwd is outside the development workspace.

Observed on 2026-11-10: the app session record keeps its generated Documents/Codex cwd,
while tools.exec_command runs dev progress in a managed repository workdir. The resulting
run knows its project, but the session does not. These are isolated reproductions, not
claims of live hook delivery. Live guard audit 01M73GHDNQFYYPQCJ6CQK49MVE separately
confirms that this app normalizes the nested shell call to Bash.
"""
import json
import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))
from devkit import canary, core, hooks, peers, progress, scope  # noqa: E402
from test_progress import TempState, repo_with_worktrees  # noqa: E402
import test_hooks_welcome  # noqa: E402

BLOCK = ("**[Firefly> DevKit]** api · `main` · Check the repository\n"
         "- **Done:** inspected the repository\n- **Check:** ✓ git status completed\n- **Next:** review")


def post_tool_event(root, agent, payload):
    output = io.StringIO()
    with mock.patch.object(hooks, "STATE", root), mock.patch.object(core, "DEV", root / "Development"), \
            mock.patch.object(hooks, "_continuity", return_value=None), \
            mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(output):
        hooks.post_tool(agent)


class ProjectlessSessionTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.root = Path(self.state.tmp.name).resolve()
        self.dev = self.root / "Development"
        self.dev.mkdir()
        self.repo, self.worktree = repo_with_worktrees(self.dev)
        self.host = self.root / "Documents" / "Codex" / "chat"
        self.host.mkdir(parents=True)
        self.session = progress.SESSIONS / "codex-projectless.json"
        self.session.write_text(json.dumps({"agent": "codex", "session": "projectless",
                                            "cwd": str(self.host), "top": "", "project": ""}))
        for patcher in (mock.patch.dict(os.environ, {"DEVKIT_SESSION": "codex:projectless"}),
                        mock.patch.object(canary, "STATE", self.root),
                        mock.patch.object(canary, "LOG", self.root / "canary.jsonl"),
                        mock.patch.object(canary, "DEV", self.dev),
                        # This temporary fixture stands for the real managed checkout, not a scratch repo.
                        mock.patch.object(canary.core, "temp_roots", return_value=[])):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_starting_a_run_in_a_repository_binds_a_projectless_session(self):
        empty = json.loads(self.session.read_text())
        existing = dict(empty, project="acme/other", top=str(self.repo), branch="main", head="old")
        self.session.write_text(json.dumps(existing))
        progress.start("Inspect the existing project", cwd=str(self.worktree), plan=["debug"])
        after = json.loads(self.session.read_text())
        self.assertEqual({k: after[k] for k in existing}, existing)
        self.session.write_text(json.dumps(empty))
        progress.run_path("codex", "projectless").unlink()
        run = progress.start("Inspect the repository", cwd=str(self.worktree), plan=["debug"])
        self.assertTrue(run["project"])
        session = json.loads(self.session.read_text())
        self.assertEqual(session.get("project"), run["project"])
        self.assertEqual(session.get("top"), str(self.worktree))
        self.assertEqual(session.get("cwd"), str(self.host))
        self.assertEqual(session.get("branch"), "fix/x0")
        self.assertEqual(session.get("head"), core.gitout(self.worktree, "rev-parse", "HEAD"))
        facts = peers._View().git("codex:projectless")
        self.assertEqual(facts.get("branch"), "fix/x0")
        self.assertEqual(facts.get("worktree"), str(self.worktree))
        peers.refresh("codex", "projectless")
        self.assertEqual(json.loads(self.session.read_text()).get("worktree"), str(self.worktree))
        # Claude keeps its established session binding and cwd-first peer lookup.
        legacy = progress.session_path("claude", "legacy")
        legacy.write_text(json.dumps(dict(empty, agent="claude", session="legacy")))
        with mock.patch.dict(os.environ, {"DEVKIT_SESSION": "claude:legacy"}):
            progress.start("Inspect the repository", cwd=str(self.worktree), plan=["debug"])
        self.assertEqual(json.loads(legacy.read_text()).get("top"), "")
        self.assertEqual(json.loads(legacy.read_text()).get("project"), "")
        legacy.write_text(json.dumps({"agent": "claude", "session": "legacy", "cwd": str(self.repo),
                                      "top": str(self.worktree)}))
        self.assertEqual(peers._View().git("claude:legacy").get("worktree"), str(self.repo))
        peers.refresh("claude", "legacy")
        self.assertEqual(json.loads(legacy.read_text()).get("worktree"), str(self.repo))

    def test_a_successful_main_tool_binds_its_own_checkout_without_a_progress_run(self):
        original = json.loads(self.session.read_text())
        file = self.worktree / "notes.txt"
        file.write_text("Reviewed\n")
        for tool, inp in (("exec_command", {"cmd": "git status", "workdir": str(self.worktree)}),
                          ("Write", {"file_path": str(file), "content": "Reviewed\n"})):
            with self.subTest(tool=tool):
                self.session.write_text(json.dumps(original))
                data = {"session_id": "projectless", "cwd": str(self.host), "tool_name": tool,
                        "tool_use_id": "short-call", "tool_input": inp, "tool_response": ""}
                post_tool_event(self.root, "codex", data)
                bound = json.loads(self.session.read_text())
                self.assertEqual(bound.get("top"), str(self.worktree))
                self.assertEqual(bound.get("cwd"), str(self.host))
                self.assertFalse(progress.run_path("codex", "projectless").exists())
                self.assertIsNotNone(canary.check("codex", dict(data, last_assistant_message=BLOCK)))

        data = {"session_id": "projectless", "cwd": str(self.host), "tool_name": "exec_command",
                "tool_use_id": "short-call", "tool_input": {"cmd": "git status", "workdir": str(self.worktree)}}
        for updates in ({"agent_id": "child"}, {"session_id": "unknown"}, {"cwd": str(self.repo)},
                        {"tool_input": {"cmd": "git status", "workdir": str(self.host)}}):
            with self.subTest(updates=updates):
                self.session.write_text(json.dumps(original))
                post_tool_event(self.root, "codex", dict(data, **updates))
                self.assertEqual(json.loads(self.session.read_text()).get("top"), "")
        self.session.write_text(json.dumps(dict(original, cwd=None)))
        post_tool_event(self.root, "codex", dict(data, cwd=None))
        self.assertEqual(json.loads(self.session.read_text()).get("top"), "")
        transcript = self.root / "short-task.jsonl"
        for status in ("failed", "declined"):
            self.session.write_text(json.dumps(original))
            transcript.write_text(json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "CommandExecution", "id": "short-call", "status": status,
                "exit_code": 1 if status == "declined" else None}}}) + "\n")
            post_tool_event(self.root, "codex", dict(data, transcript_path=str(transcript)))
            self.assertEqual(json.loads(self.session.read_text()).get("top"), "")
        legacy = progress.session_path("claude", "legacy")
        legacy.write_text(json.dumps(dict(original, agent="claude", session="legacy")))
        post_tool_event(self.root, "claude", dict(data, session_id="legacy"))
        self.assertEqual(json.loads(legacy.read_text()).get("top"), "")
        # A registered repository is never replaced by a later successful call in another checkout.
        self.session.write_text(json.dumps(dict(original, top=str(self.repo), project="acme/existing")))
        post_tool_event(self.root, "codex", data)
        self.assertEqual(json.loads(self.session.read_text()).get("top"), str(self.repo))

    def test_a_deferred_success_binds_only_after_its_own_command_finishes(self):
        transcript = self.root / "deferred.jsonl"
        transcript.write_text(json.dumps({"type": "session_meta", "payload": {"cli_version": "0.167.0"}}) + "\n")
        data = {"session_id": "projectless", "cwd": str(self.host), "tool_name": "exec_command",
                "tool_use_id": "deferred-call", "transcript_path": str(transcript),
                "tool_input": {"cmd": "git status", "workdir": str(self.worktree)}}
        post_tool_event(self.root, "codex", data)
        self.assertEqual(json.loads(self.session.read_text()).get("top"), "")
        with transcript.open("a") as f:
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "CommandExecution", "id": "deferred-call", "status": "completed", "exit_code": 0}}}) + "\n")
        with mock.patch.object(hooks, "STATE", self.root), mock.patch.object(core, "DEV", self.dev):
            hooks._codex_settle("codex", data)
        self.assertEqual(json.loads(self.session.read_text()).get("top"), str(self.worktree))

    def test_an_executed_failing_command_binds_without_becoming_a_success(self):
        transcript = self.root / "failed-test.jsonl"
        data = {"session_id": "projectless", "cwd": str(self.host), "tool_name": "exec_command",
                "tool_use_id": "failed-test", "transcript_path": str(transcript),
                "tool_input": {"cmd": "python3 -m unittest tests.test_example", "workdir": str(self.worktree)},
                "tool_response": "FAILED (failures=1)"}
        for status in ("completed", "failed"):
            with self.subTest(status=status):
                item = {"type": "CommandExecution", "id": "failed-test", "status": status, "exit_code": 1}
                transcript.write_text(json.dumps({"type": "event_msg", "payload": {
                    "type": "item_completed", "item": item}}) + "\n")
                self.session.write_text(json.dumps({"agent": "codex", "session": "projectless", "cwd": str(self.host)}))
                self.assertFalse(hooks.codex_outcome(item))
                post_tool_event(self.root, "codex", data)
                self.assertEqual(json.loads(self.session.read_text()).get("top"), str(self.worktree))
                # Deferred completion uses the same executed-versus-success distinction.
                self.session.write_text(json.dumps({"agent": "codex", "session": "projectless", "cwd": str(self.host)}))
                with mock.patch.object(hooks, "STATE", self.root), mock.patch.object(core, "DEV", self.dev):
                    hooks._codex_defer("codex", data)
                    hooks._codex_settle("codex", data)
                self.assertEqual(json.loads(self.session.read_text()).get("top"), str(self.worktree))
                self.assertFalse(hooks.codex_outcome(item))

    def test_development_reply_is_scored_with_a_registered_repository_outside_host_cwd(self):
        other = self.session.with_name("codex-other.json")
        other.write_text(json.dumps({"agent": "codex", "session": "other", "top": str(self.worktree),
                                     "project": progress._project(str(self.worktree))}))
        data = {"cwd": str(self.host), "session_id": "projectless", "last_assistant_message": BLOCK}
        self.assertIsNone(canary.check("codex", data))
        self.assertFalse(canary.LOG.exists())
        session = json.loads(self.session.read_text())
        session.update(top=str(self.worktree), project=progress._project(str(self.worktree)))
        self.session.write_text(json.dumps(session))
        data = {"cwd": str(self.host), "session_id": "projectless", "last_assistant_message": BLOCK}
        self.assertTrue(scope.decide(data, session))
        result = canary.check("codex", data)
        self.assertIsNotNone(result, "a development turn is dropped because the app host cwd is outside DEV")
        self.assertEqual(result[:2], (True, session["project"]))
        records = [json.loads(line) for line in canary.LOG.read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["ok"])
        session["scope_last"] = "other"
        self.session.write_text(json.dumps(session))
        data = {"cwd": str(self.host), "session_id": "projectless", "last_assistant_message": "Recipe: mix flour."}
        self.assertIsNone(canary.check("codex", data))
        self.assertEqual(len(canary.LOG.read_text().splitlines()), 1)
        legacy = progress.session_path("claude", "legacy")
        legacy.write_text(json.dumps(dict(session, agent="claude", session="legacy", scope_last="development")))
        self.assertIsNone(canary.check("claude", dict(data, session_id="legacy", last_assistant_message=BLOCK)))
        self.assertEqual(len(canary.LOG.read_text().splitlines()), 1)

    def test_resuming_after_a_slot_frees_clears_the_old_capacity_refusal(self):
        run = progress.start("Inspect the repository", cwd=str(self.worktree), plan=["debug"])
        lane = {"state": "done", "binding": "exact", "launch": "call-member", "persona": "neo",
                "type": "neo", "label": "Neo: debug", "parent": "main", "depth": 1,
                "started": 1, "finished": 2,
                "over_limit": {"ts": 1, "why": "1 of 1 crew slots are in use"}}
        run["agents"]["member"] = lane
        progress._lane_event(run, "member", "neo", "call-member", {}, "exact", 3,
                             False, False, own=True, sess={})
        self.assertEqual(lane["state"], "running")
        self.assertNotIn("waits_slot", lane)
        self.assertNotIn("over_limit", lane,
                         "a readmitted lane remains forbidden to edit by its old launch marker")
        run["agent"] = "claude"
        lane.update(state="done", over_limit={"ts": 1, "why": "1 of 1 crew slots are in use"})
        progress._lane_event(run, "member", "neo", "call-member", {}, "exact", 4,
                             False, False, own=True, sess={})
        self.assertEqual(lane["state"], "running")
        self.assertIn("over_limit", lane, "Claude retains its existing readmission state handling")

    def test_a_free_slot_does_not_readmit_a_lane_over_the_depth_limit(self):
        run = progress.start("Inspect the repository", cwd=str(self.worktree), plan=["debug"])
        run["agents"]["parent"] = {"state": "done", "depth": 2, "parent": "main"}
        lane = {"state": "done", "binding": "exact", "launch": "call-member", "persona": "neo",
                "type": "neo", "label": "Neo: debug", "parent": "parent", "depth": 3,
                "started": 1, "finished": 2, "over_limit": {"ts": 1, "why": "progress.max_depth"}}
        run["agents"]["member"] = lane
        progress._lane_event(run, "member", "neo", "call-member", {}, "exact", 3,
                             False, False, own=True, sess={})
        self.assertEqual(lane["state"], "done")
        self.assertIn("over_limit", lane)
        self.assertIn("progress.max_depth", lane["waits_slot"]["why"])
        run["agent"] = "claude"
        progress._lane_event(run, "member", "neo", "call-member", {}, "exact", 4,
                             False, False, own=True, sess={})
        self.assertEqual(lane["state"], "running", "Claude retains its existing capacity-only resume check")
        self.assertIn("over_limit", lane)

    def test_stop_scores_and_rejects_a_projectless_development_reply_without_its_block(self):
        # Reconstructed contracts use the fields in the captured Claude fixtures and Codex 0.167 tests.
        # They replay the handlers; they are not captures of this app's raw hook envelope.
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                sid = "stop-" + agent
                session = progress.session_path(agent, sid)
                session.write_text(json.dumps({"agent": agent, "session": sid, "cwd": str(self.host),
                                                "top": str(self.worktree), "project": progress._project(self.worktree)}))
                transcript = self.root / (agent + ".jsonl")
                if agent == "codex":
                    events = [{"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}},
                              {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
                               "call_id": "call-1", "input": 'text(await tools.exec_command({cmd:"git status"}));'}}]
                else:
                    events = [{"type": "user", "message": {"role": "user", "content": "Inspect the repository"}},
                              {"type": "assistant", "message": {"role": "assistant", "content": [
                                  {"type": "tool_use", "name": "Bash", "id": "tool-1",
                                   "input": {"command": "git status"}}]}}]
                transcript.write_text("".join(json.dumps(e) + "\n" for e in events))
                data = {"session_id": sid, "hook_event_name": "Stop", "cwd": str(self.host),
                        "transcript_path": str(transcript), "last_assistant_message": "Inspected the repository.",
                        "stop_hook_active": False}
                output = io.StringIO()
                with mock.patch.object(hooks, "STATE", self.root), \
                        mock.patch.object(hooks, "_continuity", return_value=None), \
                        mock.patch.object(hooks, "_spawn_sync", return_value=None), \
                        mock.patch("sys.stdin", io.StringIO(json.dumps(data))), contextlib.redirect_stdout(output):
                    hooks.stop(agent)
                result = json.loads(output.getvalue() or "{}")
                if agent == "claude":
                    self.assertNotEqual(result.get("decision"), "block")
                    self.assertFalse(canary.LOG.exists())
                    continue
                self.assertEqual(result.get("decision"), "block")
                self.assertIn("status block", result.get("reason", ""))
                records = [json.loads(line) for line in canary.LOG.read_text().splitlines()]
                self.assertEqual(records[-1]["agent"], agent)
                self.assertFalse(records[-1]["ok"])
                self.assertEqual(records[-1]["project"], progress._project(self.worktree))


class BoundSessionContextTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_hooks_welcome.WelcomeHook()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def test_resume_and_compaction_inject_the_bound_checkout_without_changing_host_cwd(self):
        host = self.fixture.root / "Documents" / "Codex" / "chat"
        host.mkdir(parents=True)
        for source in ("resume", "compact"):
            with self.subTest(source=source):
                sid = "bound-" + source
                path = progress.session_path("codex", sid)
                path.write_text(json.dumps({"agent": "codex", "session": sid, "cwd": str(host),
                                            "project": self.fixture.pid, "top": str(self.fixture.repo), "branch": "main"}))
                result = self.fixture.start("codex", sid, cwd=host, source=source)
                context = result["hookSpecificOutput"]["additionalContext"]
                self.assertIn(f"Project: **{self.fixture.pid}**", context)
                self.assertIn("main", context)
                self.assertNotIn("not inside a project", context)
                self.assertEqual(json.loads(path.read_text())["cwd"], str(host))
                legacy = progress.session_path("claude", sid)
                legacy.write_text(json.dumps({"agent": "claude", "session": sid, "cwd": str(host),
                                               "project": self.fixture.pid, "top": str(self.fixture.repo)}))
                rc, context = self.fixture.run_hook("claude", sid, cwd=host, source=source)
                self.assertEqual(rc, 0)
                self.assertIn("not inside a project", context)
                self.assertNotIn(f"Project: **{self.fixture.pid}**", context)
                self.assertEqual(json.loads(legacy.read_text())["cwd"], str(host))


if __name__ == "__main__":
    unittest.main()
