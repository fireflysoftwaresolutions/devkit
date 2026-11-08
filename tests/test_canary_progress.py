# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The status block's Progress bullet (the developer, 2026-11-08: "the canary message must always print the progress
# step number of total steps"). While the session has a running or blocked `dev progress` run, the Stop hook sends
# back, once per turn, a final message whose block has no Progress bullet or whose step counts disagree with the run,
# with the exact line to use. Without an active run no Progress bullet is needed.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_canary_progress
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / "lib"))
sys.path.insert(0, str(TESTS))

from devkit import canary, hooks, scope  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_progress import TempState  # noqa: E402

T0 = 1_800_000_000.0
NOW = T0 + 30 * 60


def block(progress=None, header="**[Firefly> DevKit]** devkit · `hotfix/26.11.7` · Hotfix"):
    lines = [header, "- **Done:** fixed the guard", "- **Check:** ✓ 12 tests pass"]
    if progress is not None:
        lines.append(f"- **Progress:** {progress}")
    return "\n".join(lines + ["- **Next:** the review", "", "Details below."])


def run_(state="running"):
    # 8 steps: 2 done, 3 skipped, 1 running, 2 pending -> "2 of 5 steps · 3 skipped · now 6. guard findings"
    states = ["done", "skipped", "done", "skipped", "skipped", "running", "pending", "pending"]
    names = ["plan", "a", "b", "c", "d", "guard findings", "docs", "review"]
    plan = [dict(pg._new_step(n, 10), state=st) for n, st in zip(names, states)]
    return {"v": 2, "goal": "Hotfix", "state": state, "started": T0, "estimate": 120, "plan": plan,
            "intervals": [[T0, None, "turn", "main"]], "agents": {}, "updates": []}


class ProgressReasonTests(unittest.TestCase):
    def reason(self, text, run):
        with TempState():
            return canary.progress_reason(text, run, NOW)

    def test_no_progress_bullet_is_needed_without_an_active_run(self):
        self.assertEqual(self.reason(block(), None), "")
        self.assertEqual(self.reason(block(), run_("done")), "")

    def test_a_block_without_the_bullet_goes_back_with_the_line_to_use(self):
        for state in ("running", "blocked"):
            why = self.reason(block(), run_(state))
            self.assertIn("no Progress bullet", why, state)
            self.assertIn("`- **Progress:** 2 of 5 steps · 3 skipped · now 6. guard findings · ", why, state)

    def test_counts_that_take_skipped_steps_as_done_go_back(self):
        why = self.reason(block("5/8 · 30m active of ~2h00m"), run_())
        self.assertIn("says 5 of 8 steps, but the run has 2 of 5", why)
        self.assertIn("`- **Progress:** 2 of 5 steps · 3 skipped", why)

    def test_the_runs_counts_pass_in_either_form_and_in_another_language(self):
        for progress in ("2 of 5 steps · 3 skipped · now 6. guard findings · ETA ~40m", "2/5 · ETA ~40m",
                         "1h05m active · 2 de 5 pasos · ahora 6. guard findings"):
            self.assertEqual(self.reason(block(progress), run_()), "", progress)

    def test_a_progress_bullet_without_a_step_count_goes_back(self):
        why = self.reason(block("57 commits on the branch, none pushed · usage at 80%"), run_())
        self.assertIn("no step count", why)
        self.assertIn("`- **Progress:** 2 of 5 steps", why)

    def test_a_message_without_the_status_block_is_told_the_progress_line_too(self):
        why = self.reason("Waiting for the review.", run_())
        self.assertIn("`- **Progress:** 2 of 5 steps", why)


class ProgressGateTests(unittest.TestCase):
    # canary.progress_gate: the Stop hook's rule, on the session's own run, once per turn
    def setUp(self):
        self.state = TempState()
        self.state.__enter__()
        pg.start("Hotfix", plan=["plan=10m", "api=20m", "docs=10m"], now=T0)
        pg.step("plan done", check="written", now=T0 + 600)  # 1 of 3 steps, api running

    def tearDown(self):
        self.state.__exit__(None, None, None)

    def gate(self, text, sess, active=False):
        data = {"session_id": "s1", "last_assistant_message": text, "stop_hook_active": active}
        return canary.progress_gate("claude", data, sess, NOW)

    def test_the_rule_reads_the_sessions_run_and_tells_once_per_turn(self):
        sess = {"turn_started": NOW - 60}
        why = self.gate(block(), sess)
        self.assertIn("`- **Progress:** 1 of 3 steps · now 2. api · ", why)
        self.assertEqual(sess["progress_told"], NOW - 60)
        self.assertEqual(self.gate(block(), sess), "")  # the same turn: never a second time
        sess["turn_started"] = NOW  # the next turn is checked again
        self.assertIn("no Progress bullet", self.gate(block(), sess))

    def test_the_reply_to_a_send_back_is_not_checked_again(self):
        self.assertEqual(self.gate(block(), {"turn_started": NOW - 60}, active=True), "")

    def test_a_block_with_the_runs_line_passes(self):
        line = pg.headline(pg.load_run("claude", "s1"), NOW)
        self.assertEqual(self.gate(block(line), {"turn_started": NOW - 60}), "")

    def test_the_rule_is_off_with_canary_enforce(self):
        with mock.patch.dict(hooks.core.CFG, {"canary": {"enforce": False}}):
            self.assertEqual(self.gate(block(), {"turn_started": NOW - 60}), "")


class StopHookTests(unittest.TestCase):
    # hooks.stop joins the rule to the turn's send-back (Claude Code and Codex run the same hook)
    def setUp(self):
        self.state = TempState()
        self.state.__enter__()

    def tearDown(self):
        self.state.__exit__(None, None, None)

    def stop(self, agent, text):
        # a development turn of <agent>:s1 that started a minute ago, with a run of two steps
        sf = pg.SESSIONS / f"{agent}-s1.json"
        sf.write_text(json.dumps({"agent": agent, "session": "s1", "started": time.time() - 600,
                                  "turn_started": time.time() - 60}))
        with mock.patch.dict("os.environ", {"DEVKIT_SESSION": f"{agent}:s1"}):
            pg.start("Hotfix", plan=["plan=10m", "api=20m"])
        data = {"session_id": "s1", "cwd": self.state.tmp.name, "last_assistant_message": text}
        out = io.StringIO()
        with mock.patch.object(scope, "decide", lambda data, sess: True), \
                mock.patch.object(hooks, "_session_file", lambda a, sid: pg.SESSIONS / f"{a}-{sid}.json"), \
                mock.patch.object(hooks, "STATE", Path(self.state.tmp.name)), \
                mock.patch.object(hooks, "_continuity", lambda *a, **k: None), \
                mock.patch.object(hooks, "_spawn_sync", lambda args: None), \
                mock.patch.object(canary, "LOG", Path(self.state.tmp.name) / "canary.jsonl"), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(data))), contextlib.redirect_stdout(out):
            hooks.stop(agent)
        return json.loads(out.getvalue() or "{}")

    def test_a_final_message_without_the_progress_bullet_is_sent_back_on_both_agents(self):
        for agent in ("claude", "codex"):
            out = self.stop(agent, block())
            self.assertEqual(out.get("decision"), "block", agent)
            self.assertIn("- **Progress:** 0 of 2 steps · now 1. plan · ", out.get("reason", ""), agent)


class ClosingReminderTests(unittest.TestCase):
    # PostToolUse, after a call that can end the turn: the block the reminder shows carries the run's Progress line
    def test_the_reminder_shows_the_progress_line_of_the_active_run(self):
        with TempState(), tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "api"
            subprocess.run(["git", "init", "-q", "-b", "feat/export", str(repo)], check=True)
            pg.start("Hotfix", plan=["plan=10m", "api=20m"])
            sess = {"project": "acme/api", "top": str(repo), "turn_started": time.time() - 60, "turn_class": "dev"}
            launch = {"session_id": "s1", "tool_name": "Agent", "tool_input": {"description": "Ghost: docs"}}
            text = hooks.closing_reminder(launch, sess, time.time(), "claude")
        self.assertIn("**[Firefly> DevKit]** api · `feat/export` · <task>", text)
        self.assertIn("- **Progress:** 0 of 2 steps · now 1. plan · ", text)


if __name__ == "__main__":
    unittest.main()
