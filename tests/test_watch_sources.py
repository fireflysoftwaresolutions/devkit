# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The sources of the one wait outside the plain messages: a question nobody waits on any more (moot), a wake prompt
# that carries what waits, a lane the sweep or the wait's own heartbeat closes, a handoff push that failed, and a
# decision that reaches the developer while the session is idle. Each through devkit's own entry points (the `dev`
# parser, the hooks, the sync, the sweep), with the wait's world scripted as in tests/test_watch.py.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_watch_sources
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

from devkit import approvals, cli, hooks, progress as pg, work  # noqa: E402
from test_progress import launch  # noqa: E402
from test_state_e2e import dev, patch_if  # noqa: E402
from test_watch import Script, WatchCase, watch  # noqa: E402

TANK_REPORT = ("**[Firefly> DevKit · Backend engineer (Tank)]** acme/api · `main` · the API\n"
               "- **Done:** the API\n- **Check:** ✓ 9 API tests pass\n- **Next:** the tech lead")


class MootQuestionTests(WatchCase):
    def test_a_question_whose_member_ended_and_whose_step_closed_is_moot_and_wakes_nobody(self):
        # 26.11.6 kept such a question waiting until the run ended: on the developer's list, and a wake for nothing
        self.run_with_member()
        launch("a2", "devkit:mouse", "Mouse: tests", self.t0)
        pg.crew_ask("Should the API page results?", from_agent="a1", now=self.t0 + 60)
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a1", "agent_type": "devkit:tank",
                                  "last_assistant_message": TANK_REPORT}, False, now=self.t0 + 120)
        run = pg.load_run("claude", "s1")
        self.assertEqual(run["agents"]["a1"]["state"], "done")
        self.assertEqual(pg.waiting_messages(run), [])
        rc, out, _ = dev("watch")
        self.assertEqual(rc, 0, out)
        moot = [ln for ln in out.splitlines() if "M1" in ln]
        self.assertEqual(len(moot), 1, out)
        self.assertIn("moot", moot[0])
        self.world()
        rc, out, err = dev("watch", "--wait", "--timeout", "1")
        self.assertEqual(rc, 3, out + err)  # the tests member still works: the wait waits, but not for M1
        self.assertNotIn("M1", out)

    def test_a_question_answered_while_the_wait_batches_it_prints_nothing(self):
        self.run_with_member()
        self.world(lambda: (pg.crew_ask("Rename the column?", from_agent="a1", now=self.clock.t),
                            pg.crew_answer("M1", "No, keep it", now=self.clock.t)))
        rc, out, err = dev("watch", "--wait", "--timeout", "1")
        self.assertEqual(rc, 3, out + err)
        self.assertNotIn("Rename the column?", out)


class WakePromptTests(WatchCase):
    def test_a_task_notification_prompt_carries_the_decision_that_waits(self):
        # 26.11.6 returned at once for a wake prompt (a member's end, a background task), so a decision that waited
        # stayed unseen until the session's next tool call
        self.run_with_member()
        pg.crew_decision("Keep the v1 endpoint?", options="A: keep | B: drop", recommend="A", from_agent="a1",
                         now=self.t0 + 60)
        payload = {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "cwd": str(self.root),
                   "prompt": "<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n"
                             "</task-notification>"}
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
            hooks.run("prompt", "claude")
        ctx = json.loads(out.getvalue() or "{}").get("hookSpecificOutput", {}).get("additionalContext", "")
        self.assertIn("D1", ctx)
        self.assertIn("Keep the v1 endpoint?", ctx)


class LostLaneTests(WatchCase):
    def lost_line(self, out):
        return [ln for ln in out.splitlines() if ln.startswith("devkit · member:")]

    def test_a_lane_the_sweep_loses_is_one_notice_that_wakes_the_wait(self):
        # 26.11.6 moved the lane to lost and its step back to pending, and told nobody while the session was idle
        self.run_with_member()
        later = self.t0 + 2 * 3600
        made = approvals.sweep(now=later, alive=lambda *a, **k: True)
        self.assertTrue(made, "the sweep marked nothing lost")
        approvals.sweep(now=later + 60, alive=lambda *a, **k: True)  # a second sweep finds nothing new
        self.clock.t = later + 120
        self.world()
        rc, out, err = dev("watch", "--wait", "--only", "member", "--timeout", "1")
        self.assertEqual(rc, 0, out + err)
        lines = self.lost_line(out)
        self.assertEqual(len(lines), 1, out)
        self.assertIn("Backend engineer (Tank) was lost", lines[0])
        self.assertIn("api", lines[0])
        self.assertNotIn("was lost", self.post_tool_main())

    def test_the_heartbeat_closes_a_silent_lane_while_the_session_is_idle_and_wakes_the_wait(self):
        # nothing else runs: the wait's own reconcile, once a minute, finds the member silent past the limit
        self.run_with_member()
        self.clock.t = self.t0 + 85 * 60  # still within progress.stale_agent_minutes (90)
        self.world()
        rc, out, err = dev("watch", "--wait", "--timeout", "30")
        self.assertEqual(rc, 0, out + err)
        lines = self.lost_line(out)
        self.assertEqual(len(lines), 1, out)
        self.assertIn("was lost", lines[0])
        self.assertLess(self.clock.t, self.t0 + 100 * 60)  # woken by its heartbeat, not at its timeout


class HandoffPushTests(WatchCase):
    def setUp(self):
        super().setUp()
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Ana",
                   GIT_AUTHOR_EMAIL="ana@example.com", GIT_COMMITTER_NAME="Ana", GIT_COMMITTER_EMAIL="ana@example.com")

        def git(*args, cwd=None):
            subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)
        remote, self.handoff = self.root / "handoff.git", self.root / "handoff"
        git("init", "-q", "--bare", "-b", "main", str(remote))
        git("clone", "-q", str(remote), str(self.handoff))
        (self.handoff / "PROJECTS.md").write_text("# Projects\n")
        git("add", "-A", cwd=self.handoff)
        git("commit", "-q", "-m", "start", cwd=self.handoff)
        git("push", "-q", "-u", "origin", "HEAD:main", cwd=self.handoff)
        git("remote", "set-url", "--push", "origin", str(self.root / "gone.git"), cwd=self.handoff)  # pushes fail
        self.stack.enter_context(mock.patch.object(work, "HANDOFF", self.handoff))
        self.stack.enter_context(mock.patch.dict(os.environ, env))
        patch_if(self.stack, work, "push_failed_file", lambda: self.root / "handoff-push-failed.json")

    def test_a_failed_handoff_push_reaches_the_session_once(self):
        # 26.11.6 wrote handoff-push-failed.json, which `dev status` shows; no agent heard of it during the session
        self.run_with_member()
        (self.handoff / "notes.md").write_text("a change\n")
        cli.commit_push(push=True, quiet=True)
        self.assertTrue((self.root / "handoff-push-failed.json").exists())
        first, second = self.post_tool_main("t1"), self.post_tool_main("t2")
        self.assertEqual(first.count("handoff push failed"), 1, first)
        self.assertNotIn("handoff push failed", second)


class IdleDecisionTests(WatchCase):
    def test_a_decision_that_arrives_while_the_session_is_idle_notifies_the_desktop(self):
        # the developer hears of it even when nobody looks at the terminal; a decision asked in a turn does not
        self.run_with_member()
        shown = []
        self.stack.enter_context(mock.patch.object(pg, "notify", lambda title, text, **k: shown.append((title, text))))
        pg.state.update(pg.session_path("claude", "s1"), lambda s: s.update(turn_started=None))  # the turn ended
        pg.crew_decision("Drop the legacy table?", options="A: drop | B: keep", recommend="B", from_agent="a1",
                         now=self.t0 + 60)
        self.assertEqual(len(shown), 1, shown)
        self.assertIn("D1", shown[0][0] + shown[0][1])
        pg.state.update(pg.session_path("claude", "s1"), lambda s: s.update(turn_started=self.t0 + 90))
        pg.crew_decision("Rename the index?", options="A: yes | B: no", recommend="A", from_agent="a1",
                         now=self.t0 + 120)
        self.assertEqual(len(shown), 1, shown)


if __name__ == "__main__":
    unittest.main()
