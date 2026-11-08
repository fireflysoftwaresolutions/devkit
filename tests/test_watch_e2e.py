# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# End to end: an idle main session hears every devkit event through one wait. The tech lead's turn ends through the
# Stop hook (sent back once while members work and nothing is armed); then, again and again, `dev watch --wait` runs
# through devkit's own parser in a thread, blocks on its file-change function, and the test lands one event through
# devkit's own entry points while the session is idle: a member's question, a decision, a step that a finished step
# makes ready, a review step made ready the same way, a member stopped with TaskStop, a CI result, a quota window that
# frees a slot for a queued step, and a member the scheduled sweep finds lost. Each ends exactly one wait with exit 0
# and its line printed once. A failed handoff push and a message the developer types mid-turn wake nothing: the push
# rides along with the next wake, and the message counts as the developer's latest request at the next hook. At the
# end, the tech lead's next tool call repeats none of it, and no wait is left live.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_watch_e2e
import contextlib
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import approvals, canary, ciwatch, cli, continuity, core, hooks, progress as pg, state, work  # noqa: E402
from test_progress import launch  # noqa: E402
from test_state_e2e import Clock, HookSandbox, dev, patch_if  # noqa: E402
from test_watch import watch  # noqa: E402

LINE = "**[Firefly> DevKit]** widget · `feat/x` · Release\n- **Done:** briefed the crew\n- **Check:** – none yet\n" \
       "- **Progress:** 0 of 5 steps · 10 min of ~1h50m\n- **Next:** the crew works"


def report(role, name, what):
    return (f"**[Firefly> DevKit · {role} ({name})]** acme/widget · `feat/x` · {what}\n- **Done:** {what}\n"
            f"- **Check:** ✓ 12 tests pass\n- **Next:** the tech lead")


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + ".000Z"


class Gate:
    # The wait's file-change function, opened from the test's thread: the wait blocks here until the test has landed
    # an event and opens the gate, which says a file changed.
    def __init__(self):
        self.blocked, self.go = threading.Event(), threading.Event()

    def __call__(self, seconds):
        self.blocked.set()
        opened = self.go.wait(15)
        self.go.clear()
        return opened

    def land(self, action):
        if not self.blocked.wait(15):
            raise AssertionError("the wait never blocked for a file change")
        self.blocked.clear()
        action()
        self.go.set()


class WakeEveryEventTests(HookSandbox):
    def setUp(self):
        super().setUp()
        self.t0 = time.time()
        self.clock = Clock(self.t0)
        self.gate = Gate()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        s = self.stack
        patch_if(s, watch, "LIVE", self.root / "watch-live")
        patch_if(s, watch, "time", self.clock)
        patch_if(s, watch, "Changes", lambda *a, **k: self.gate)
        for name in ("WATCH", "REPORTED", "LIVE"):
            s.enter_context(mock.patch.object(ciwatch, name, self.root / "ci" / name.lower()))
        s.enter_context(mock.patch.object(canary, "STATE", self.root))
        s.enter_context(mock.patch.object(canary, "check", lambda *a, **k: (True, "acme/widget", LINE)))
        s.enter_context(mock.patch("devkit.scope.decide", lambda *a, **k: True))
        s.enter_context(mock.patch.object(hooks, "_spawn_sync", lambda *a, **k: None))  # no background dev sync
        s.enter_context(mock.patch.dict(core.CFG, {"progress": {"max_parallel": 6, "reserve": 0}}))
        self.handoff_that_cannot_push()
        self.transcript = self.root / "t.jsonl"
        self.transcript.write_text(json.dumps({"type": "user", "timestamp": iso(self.t0 - 60),
                                               "message": {"role": "user", "content": "ship the release"}}) + "\n")
        state.update(pg.session_path("claude", "s1"), lambda d: d.update(turn_started=self.t0 - 60,
                                                                        transcript=str(self.transcript)))
        # the plan: the API, then its tests, then their review; the pipeline; the parser
        pg.start("Release", plan="api=30m; tests=20m after api; review=10m after tests; pipeline=20m; parser=30m",
                 now=self.t0 - 600, mode="guided")
        for step, who in (("api", "Tank"), ("tests", "Mouse"), ("review", "Seraph"), ("pipeline", "Link"),
                          ("parser", "Neo")):
            pg.assign(step, who, why="the plan", now=self.t0 - 590)
        launch("a_tank", "devkit:tank", "Tank: api", self.t0 - 500)
        launch("a_link", "devkit:link", "Link: pipeline", self.t0 - 500)
        launch("a_neo", "devkit:neo", "Neo: parser", self.t0 - 500)
        self.post_tool("toolu_0")  # the tech lead's last tool call of the turn takes what was new then

    def handoff_that_cannot_push(self):
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Ana",
                   GIT_AUTHOR_EMAIL="ana@example.com", GIT_COMMITTER_NAME="Ana", GIT_COMMITTER_EMAIL="ana@example.com")

        def git(*args, cwd=None):
            subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)
        remote, self.handoff = self.root / "handoff.git", self.root / "handoff-repo"
        git("init", "-q", "--bare", "-b", "main", str(remote))
        git("clone", "-q", str(remote), str(self.handoff))
        (self.handoff / "PROJECTS.md").write_text("# Projects\n")
        git("add", "-A", cwd=self.handoff)
        git("commit", "-q", "-m", "start", cwd=self.handoff)
        git("push", "-q", "-u", "origin", "HEAD:main", cwd=self.handoff)
        git("remote", "set-url", "--push", "origin", str(self.root / "gone.git"), cwd=self.handoff)
        self.stack.enter_context(mock.patch.object(work, "HANDOFF", self.handoff))
        self.stack.enter_context(mock.patch.dict(os.environ, env))
        patch_if(self.stack, work, "push_failed_file", lambda: self.root / "handoff-push-failed.json")

    def post_tool(self, tid, transcript=True):
        payload = self.bash_done("git status", tid=tid)
        if transcript:
            payload["transcript_path"] = str(self.transcript)
        return self.hook("post-tool", payload)

    def stop(self, **extra):
        out = self.hook("stop", dict({"session_id": "s1", "hook_event_name": "Stop", "cwd": str(self.root),
                                      "transcript_path": str(self.transcript), "last_assistant_message": LINE},
                                     **extra)).strip()
        return json.loads(out) if out else {}

    def arm(self):
        box = {}

        def run():
            box["rc"], box["out"], box["err"] = dev("watch", "--wait", "--timeout", "240")
        t = threading.Thread(target=run)
        t.start()
        return t, box

    def wake(self, *actions):
        # one wait: arm it, land each action while it blocks, and return how it ended
        t, box = self.arm()
        for action in actions:
            self.gate.land(action)
        t.join(30)
        self.assertFalse(t.is_alive(), "the wait did not end")
        return box["rc"], box["out"] + box.get("err", "")

    def test_one_wait_wakes_an_idle_session_once_for_each_devkit_event(self):
        t, now = self.t0, lambda: self.clock.t
        out = self.stop()
        with self.subTest("the turn that ends while the crew works, with nothing armed, goes back once"):
            self.assertEqual(out.get("decision"), "block", out)
            self.assertIn("run `dev watch --wait` with Bash run_in_background", out["reason"])
        woken = []

        def expect(name, rc, text, needle):
            hits = [ln for ln in text.splitlines() if needle in ln]
            with self.subTest(name):
                self.assertEqual(rc, 0, text)
                self.assertEqual(len(hits), 1, text)
            woken.append(name)

        # 1. a member's question, while the turn's Stop passes because the wait is armed
        th, box = self.arm()
        self.gate.land(lambda: self.assertEqual(self.stop(), {}))
        self.gate.land(lambda: pg.crew_ask("May I rename the amount column?", from_agent="a_tank", now=now()))
        th.join(30)
        expect("a member's question", box["rc"], box["out"], "question M1 from Backend engineer (Tank)")
        pg.crew_answer("M1", "Yes, rename it", now=now())

        # 2. a failed handoff push (it wakes nothing), then a decision that waits for the developer
        def push_fails():
            (self.handoff / "notes.md").write_text("a change\n")
            cli.commit_push(push=True, quiet=True)
        rc, text = self.wake(push_fails, lambda: pg.crew_decision(
            "Ship the pipeline change before the API?", options="A: yes | B: after", recommend="B",
            from_agent="a_link", now=now()))
        expect("a decision for the developer", rc, text, "decision D1 from Release engineer (Link)")
        with self.subTest("the failed push rides along with that wake, once"):
            self.assertEqual(sum("handoff push failed" in ln for ln in text.splitlines()), 1, text)

        # 3. a mid-turn message (no wake), then the API step closes and its tests step becomes ready
        def typed():
            with self.transcript.open("a") as fh:
                fh.write(json.dumps({"type": "attachment", "timestamp": iso(now()), "attachment": {
                    "type": "queued_command", "prompt": "Also add the docs step", "commandMode": "prompt",
                    "origin": {"kind": "human"}, "timestamp": iso(now())}}) + "\n")
        rc, text = self.wake(typed, lambda: pg.on_subagent(
            "claude", {"session_id": "s1", "agent_id": "a_tank", "agent_type": "devkit:tank",
                       "last_assistant_message": report("Backend engineer", "Tank", "the API")}, False, now=now()))
        expect("a step a finished step makes ready", rc, text, "ready: tests")
        self.assertNotIn("Also add the docs step", text)
        self.post_tool("toolu_1")  # the tech lead acts: its next hook reads the message typed mid-turn
        with self.subTest("the mid-turn message is the developer's latest request"):
            sess = state.read(pg.session_path("claude", "s1"))
            self.assertGreaterEqual(sess.get("request_ts") or 0, int(t))  # the transcript keeps whole seconds here
            prompts = [p["text"] for p in (state.read(continuity.record_path("claude", "s1")) or {}).get(
                "checkpoint", {}).get("prompts", [])]
            self.assertIn("Also add the docs step", prompts)
        launch("a_mouse", "devkit:mouse", "Mouse: tests", now())

        # 4. the tests step closes and the review step becomes ready
        rc, text = self.wake(lambda: pg.on_subagent(
            "claude", {"session_id": "s1", "agent_id": "a_mouse", "agent_type": "devkit:mouse",
                       "last_assistant_message": report("QA engineer", "Mouse", "the tests")}, False, now=now()))
        expect("a review step made ready", rc, text, "ready: review")

        # 5. a member stopped with TaskStop (the PostToolUse hook hands its result to progress)
        rc, text = self.wake(lambda: pg.agent_stopped("claude", {
            "session_id": "s1", "tool_name": "TaskStop", "tool_input": {"task_id": "a_link"},
            "tool_response": {"task_id": "a_link", "task_type": "local_agent"}}, now=now()))
        expect("a member stopped", rc, text, "Release engineer (Link) was stopped")

        # 6. a CI result the background watch delivers
        res = {"ts": round(now()), "repo": "widget", "branch": "feat/x", "sha": "abc1234", "state": "failed",
               "minutes": 4, "started": round(now()) - 240,
               "runs": [{"name": "CI", "number": 7, "status": "failed", "conclusion": "failure"}],
               "diagnoses": [{"run": "CI", "number": 7, "conclusion": "failure", "class": "test",
                              "action": "reproduce it locally with the failing test", "url": "https://ci/7",
                              "failed_jobs": [{"name": "unit", "step": "pytest"}],
                              "excerpt": "AssertionError: amount"}]}
        rc, text = self.wake(lambda: ciwatch.deliver("claude", "s1", res))
        expect("a CI result", rc, text, "on feat/x @ abc1234")
        with self.subTest("the CI log excerpt stays out of the line, marked as untrusted"):
            self.assertNotIn("AssertionError", text)
            self.assertIn("untrusted CI output", text)

        # 7. the 5-hour window runs high, the docs step waits for a slot; the window resets and frees it
        quota = pg.QUOTA / "claude.json"
        pg.QUOTA.mkdir(parents=True, exist_ok=True)
        quota.write_text(json.dumps({"ts": int(time.time()), "five_hour": {"used_percentage": 96,
                                                                           "resets_at": int(time.time() + 3600)}}))
        pg.add_step("docs=20m", now=now())
        pg.assign("docs", "Ghost", why="the developer asked for it", now=now())
        self.assertEqual(pg.parallel_limit("claude")[0], 1)

        def window_resets():
            quota.write_text(json.dumps({"ts": int(time.time()), "five_hour": {"used_percentage": 96,
                                                                               "resets_at": int(time.time() - 60)}}))
            self.clock.t += 61  # the wait's heartbeat reads the crew limit again
        rc, text = self.wake(window_resets)
        expect("a slot the quota window frees", rc, text, "ready: docs")

        # 8. the scheduled sweep finds the Senior engineer silent past the limit
        rc, text = self.wake(lambda: approvals.sweep(now=time.time() + 2 * 3600, alive=lambda *a, **k: True))
        expect("a member the sweep lost", rc, text, "Senior engineer (Neo) was lost")

        with self.subTest("every waking source ended exactly one wait"):
            self.assertEqual(len(woken), 8, woken)
        final = self.post_tool("toolu_9")
        with self.subTest("the tech lead's next tool call repeats none of them"):
            for told in ("May I rename the amount column?", "Ship the pipeline change", "ready to launch",
                         "was stopped", "abc1234", "crew slots", "was lost", "handoff push failed"):
                self.assertNotIn(told, final)
        rc, listed, _ = dev("watches")
        with self.subTest("no wait is left live"):
            self.assertEqual(rc, 0, listed)
            rows = [ln.split()[0] for ln in listed.split("CI watches")[0].splitlines()[1:] if ln.strip()]
            self.assertNotIn("live", rows, listed)


if __name__ == "__main__":
    unittest.main()
