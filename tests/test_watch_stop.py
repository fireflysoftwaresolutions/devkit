# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The Stop rule of the one wait: a main session that ends its turn while a crew member works (its questions and
# results can arrive while it is idle) must leave a wait that wakes it, and one `dev watch --wait` covers the CI rule
# of a push too. Driven through the Stop hook as Claude Code and Codex fire it, with the transcript as each writes it.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_watch_stop
import contextlib
import io
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import canary, cistatus, ciwatch, hooks, progress as pg  # noqa: E402
from test_hooks_wiring import GIT_ENV, WiringCase, git  # noqa: E402
from test_progress import launch  # noqa: E402
from test_state_e2e import patch_if  # noqa: E402
from test_watch import watch  # noqa: E402

LINE = "**[Firefly> DevKit]** api · `feat/x` · Ship\n- **Done:** x\n- **Check:** ✓ y\n- **Next:** the crew reports"


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + ".000Z"


class StopRuleCase(WiringCase):
    # A session in acme/api on feat/x whose Backend engineer works on the API step.
    def setUp(self):
        super().setUp()
        self.now = time.time()
        for agent in ("claude", "codex"):
            pg.session_path(agent, "s1").write_text(json.dumps({"agent": agent, "session": "s1", "started": self.now,
                                                                "top": str(self.repo), "project": "acme/api"}))
        self.transcript = self.root / "t.jsonl"
        self.records = [{"type": "user", "timestamp": iso(self.now - 60),
                         "message": {"role": "user", "content": "build the API with the crew"}}]
        self.write()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for p in (mock.patch.object(hooks, "_spawn", lambda *a, **k: None),  # the Stop hook's background dev sync
                  mock.patch.object(ciwatch, "LIVE", self.root / "ci-live"),
                  mock.patch.object(ciwatch, "WATCH", self.root / "ci-watch"),
                  mock.patch.object(canary, "STATE", self.root),
                  mock.patch.object(canary, "check", lambda *a, **k: (True, "acme/api", LINE)),
                  mock.patch("devkit.scope.decide", lambda *a, **k: True)):
            self.stack.enter_context(p)
        patch_if(self.stack, watch, "LIVE", self.root / "watch-live")

    def write(self):
        self.transcript.write_text("\n".join(json.dumps(r) for r in self.records) + "\n")

    def member_works(self, agent="claude"):
        os.environ["DEVKIT_SESSION"] = f"{agent}:s1"
        self.addCleanup(os.environ.__setitem__, "DEVKIT_SESSION", "claude:s1")
        pg.start("API", plan=["api"], now=self.now - 300)
        pg.assign("api", "Tank", now=self.now - 300)
        if agent == "claude":
            launch("a1", "devkit:tank", "Tank: api", self.now - 240)
        else:  # Codex: spawn_agent, then the agent's start
            pg.note_agent_launch("codex", {"session_id": "s1", "tool_use_id": "call_1",
                                           "tool_input": {"description": "Tank: api", "agent_type": "devkit:tank"}},
                                 now=self.now - 240)
            pg.on_subagent("codex", {"session_id": "s1", "agent_id": "a1", "agent_type": "devkit:tank"}, True,
                           now=self.now - 240)

    def shell(self, cmd, tid, result, background=False, at=None):
        at = at or self.now - 30
        inp = dict({"command": cmd}, **({"run_in_background": True} if background else {}))
        self.records += [
            {"type": "assistant", "timestamp": iso(at), "cwd": str(self.repo), "message": {"role": "assistant",
             "content": [{"type": "tool_use", "id": tid, "name": "Bash", "input": inp}]}},
            {"type": "user", "timestamp": iso(at), "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": result}]}}]
        self.write()

    def stop(self, agent="claude", **extra):
        out = io.StringIO()
        payload = dict({"session_id": "s1", "hook_event_name": "Stop", "cwd": str(self.repo),
                        "transcript_path": str(self.transcript), "last_assistant_message": LINE}, **extra)
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out), \
                mock.patch("devkit.guard.check", lambda *a, **k: None):
            hooks.run("stop", agent)
        text = out.getvalue().strip()
        return json.loads(text) if text else {}


class WaitRuleTests(StopRuleCase):
    def test_claude_code_sends_back_a_turn_that_ends_with_a_member_working_and_nothing_armed(self):
        # 26.11.6 let the turn end: the member's question then waited until the developer happened to write
        self.member_works()
        out = self.stop()
        self.assertEqual(out.get("decision"), "block", out)
        self.assertIn("Backend engineer (Tank)", out["reason"])
        self.assertIn("run `dev watch --wait` with Bash run_in_background", out["reason"])
        self.assertNotIn("dev watch --wait", self.stop(stop_hook_active=True).get("reason") or "")  # once a turn

    def test_codex_is_told_to_wait_in_the_foreground(self):
        self.member_works("codex")
        out = self.stop("codex")
        self.assertEqual(out.get("decision"), "block", out)
        self.assertIn("run `dev watch --wait --timeout 10` in the foreground", out["reason"])
        # Codex's transcript: the developer's message, then the shell call that waited in the foreground
        self.records += [{"type": "event_msg", "timestamp": iso(self.now - 20),
                          "payload": {"type": "user_message", "message": "go on"}},
                         {"type": "response_item", "timestamp": iso(self.now - 10),
                          "payload": {"type": "function_call", "name": "exec_command", "call_id": "call_w",
                                      "arguments": json.dumps({"cmd": "dev watch --wait --timeout 10"})}}]
        self.write()
        self.assertNotIn("dev watch --wait", self.stop("codex").get("reason") or "")  # waited in this turn

    def test_a_live_background_wait_lets_the_turn_end(self):
        self.member_works()
        self.assertIn("dev watch --wait", self.stop().get("reason") or "")  # nothing armed yet
        self.shell("dev watch --wait", "toolu_w", "Command running in background with ID: bw1. Output is being "
                                                  "written to: /tmp/bw1.output", background=True)
        self.assertNotIn("dev watch --wait", self.stop().get("reason") or "")

    def test_a_wait_chained_to_other_work_is_not_armed_and_the_turn_is_sent_back_until_one_runs_alone(self):
        self.member_works()
        self.shell("dev watch --wait 2>&1 | tail -3; unset CLAUDECODE FIREFLY_DOCS_CHAT; npm run release -- "
                   "--no-check > /tmp/release.log 2>&1; echo done; ls dist/release | tail -5", "toolu_c",
                   "Command running in background with ID: bc1. Output is being written to: /tmp/bc1.output",
                   background=True)
        self.assertEqual(watch.armed("claude", "s1", "main", str(self.transcript)), [])
        out = self.stop()
        self.assertEqual(out.get("decision"), "block", out)
        self.assertIn("A wait chained to other work wakes you only when all of it ends: run the wait alone and do "
                      "the rest after it wakes you", out["reason"])
        self.shell("dev watch --wait 2>&1 | tail -3", "toolu_w", "Command running in background with ID: bw1. "
                   "Output is being written to: /tmp/bw1.output", background=True)
        self.assertEqual([w["id"] for w in watch.armed("claude", "s1", "main", str(self.transcript))], ["bw1"])
        self.assertNotIn("dev watch --wait", self.stop().get("reason") or "")

    def test_a_wait_is_asked_only_once_something_can_arrive(self):
        os.environ["DEVKIT_SESSION"] = "claude:s1"
        pg.start("API", plan=["api"], now=self.now - 300)  # a run, but no member works and nothing is launched
        pg.assign("api", "Tank", now=self.now - 300)
        self.assertNotIn("dev watch --wait", self.stop().get("reason") or "")
        launch("a1", "devkit:tank", "Tank: api", self.now - 60)
        self.assertIn("dev watch --wait", self.stop().get("reason") or "")


class CiRuleTests(StopRuleCase):
    # A push in the turn, its CI still running, and devkit's own background watch of that commit live
    def setUp(self):
        super().setUp()
        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, env=GIT_ENV)
        git(self.repo, "remote", "add", "origin", str(remote))
        git(self.repo, "push", "-q", "origin", "HEAD:refs/heads/main")
        git(self.repo, "switch", "-qc", "feat/x")
        (self.repo / "README.md").write_text("feature\n")
        git(self.repo, "commit", "-qam", "feature")
        git(self.repo, "push", "-q", "-u", "origin", "feat/x")
        self.sha = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                                  check=True).stdout.strip()
        running = {"provider": "github", "repo": "acme/api",
                   "runs": [{"id": "9", "name": "ci", "number": 14, "sha": self.sha[:7], "status": "running"}]}
        self.stack.enter_context(mock.patch.object(cistatus, "runs_for", lambda *a, **k: running))
        self.shell("git push origin feat/x", "toolu_p", "ok")
        ciwatch.register({"top": str(self.repo), "ref": "feat/x", "sha": self.sha, "kind": "branch"}, "devkit",
                         "claude", "s1", os.getpid(), 90, now=self.now - 20)

    def test_a_live_global_wait_covers_the_ci_rule_of_a_push(self):
        # 26.11.6 asked for a `dev ci watch --wait` of the commit even when a wait for every event was armed
        self.assertIn("dev ci watch --wait", self.stop().get("reason") or "")  # nothing armed: the CI rule asks
        self.shell("dev watch --wait", "toolu_w", "Command running in background with ID: bw1. Output is being "
                                                  "written to: /tmp/bw1.output", background=True)
        self.assertEqual(self.stop(), {})


if __name__ == "__main__":
    unittest.main()
