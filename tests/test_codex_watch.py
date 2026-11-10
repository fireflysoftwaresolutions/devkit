# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Normalized Codex command completion contracts and real crew message delivery.
import contextlib
import io
import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import bugs, hooks, progress as pg, state, watch  # noqa: E402
from test_bugs_detect import BugState  # noqa: E402
from test_language_neutral_hooks import HookCase  # noqa: E402
from test_watch import WatchCase  # noqa: E402


WAIT = "dev watch --wait --timeout 0.5"
TIMEOUT = "watch: nothing arrived in 0.5 min · arm again: dev watch --wait --timeout 10"


class CodexWatchOutcomeTests(BugState, HookCase):
    def setUp(self):
        super().setUp()
        self.bug_state()
        self.rollout = self.root / "rollout.jsonl"
        self.rollout.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": "c1", "cli_version": "0.167.0", "originator": "codex_exec"}}) + "\n")
        self.n = 0

    def record(self, cmd, output, code, status="failed"):
        self.n += 1
        item = {"type": "CommandExecution", "id": f"exec-watch-{self.n}", "process_id": "12345",
                "command": ["/bin/zsh", "-lc", cmd], "cwd": str(self.root),
                "parsed_cmd": [{"type": "unknown", "cmd": cmd}], "source": "unified_exec_startup",
                "status": status, "aggregated_output": output, "exit_code": code,
                "duration": {"secs": 0, "nanos": 3333}}
        return item

    def append(self, item):
        with self.rollout.open("a") as fh:
            fh.write(json.dumps({"type": "event_msg", "payload": {
                "type": "item_completed", "turn_id": "t1", "item": item}}) + "\n")

    def post(self, item):
        payload = {"cwd": str(self.root), "hook_event_name": "PostToolUse", "session_id": "c1",
                   "tool_input": {"cmd": item["command"][-1]}, "tool_name": "exec_command",
                   "tool_response": item["aggregated_output"], "tool_use_id": item["id"],
                   "transcript_path": str(self.rollout), "turn_id": "t1"}
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
            hooks.run("post-tool", "codex")
        return out.getvalue()

    def outcomes(self):
        f = bugs.BUGS / "ledger" / "c1.jsonl"
        rows = [json.loads(s) for s in f.read_text().splitlines()] if f.exists() else []
        return [r["ok"] for r in rows if r.get("tool") == "exec_command"]

    def exercise_waits(self, deferred):
        vectors = [
            (WAIT, TIMEOUT, 3, "failed"),
            ("DEVKIT_SPEAKER=main DEVKIT_SESSION=codex:c1 " + WAIT, TIMEOUT, 3, "completed"),
            (WAIT, "watch: the session ended", 2, "failed"),
            (WAIT, "watch: nothing can arrive for this session: no crew member works, no launch is pending, "
             "no step waits for a slot, no other session owes it an answer and no CI run is unfinished. "
             "End the turn without a wait.", 2, "failed"),
            (WAIT, "watch: nothing can arrive for you: none of your questions waits for an answer. "
             "End the turn without a wait.", 2, "failed"),
            (WAIT, "watch: a newer wait of this session took over; this one ends", 5, "failed"),
            (WAIT, "watch: stopped before anything arrived · arm again: dev watch --wait --for me --timeout 9",
             3, "failed"),
            (WAIT, "watch: this conversation goes on under session codex:01a1c178-64… "
             "(a compaction gave it a new id) · arm again: dev watch --wait --timeout 10", 3, "failed"),
        ]
        for cmd, output, code, status in vectors * 2:
            with self.subTest(cmd=cmd, code=code, deferred=deferred):
                item = self.record(cmd, output, code, status)
                if deferred:
                    self.post(item)
                    self.append(item)
                    result = hooks._codex_settle("codex", {"session_id": "c1"})
                else:
                    self.append(item)
                    result = self.post(item)
                self.assertNotIn(hooks.ROOT_CAUSE_STOP, result)
                self.assertIs(self.outcomes()[-1], True)
                self.assertEqual(hooks.codex_rollout(self.rollout)[0][item["id"]]["exit_code"], code)

    def test_expected_wait_outcomes_do_not_enter_the_failure_loop(self):
        self.exercise_waits(False)

    def test_deferred_wait_outcomes_settle_without_failure_streaks(self):
        self.exercise_waits(True)

    def test_ci_wait_controls_preserve_real_pipeline_and_parser_failures(self):
        cmd = "dev ci watch --wait --branch main --sha abc1234 --timeout 0.5"
        timed = ("job lint passed · CI #7\nCI still running · acme/api main @ abc1234 after 0.5 min "
                 "(CI #7 in_progress): this watch ended first; re-arm: dev ci watch --wait --branch main "
                 "--sha abc1234 --timeout 28")
        cases = [
            (cmd, timed, 3, True),
            (cmd, "CI none · acme/api main @ abc1234: no pipeline run appeared for this commit", 4, True),
            (cmd, "CI skipped · acme/api main @ abc1234 · CI #7 skipped · 0.5 min", 4, True),
            (cmd, "CI failed · acme/api main @ abc1234 · CI #7 failed", 1, False),
            (cmd, "CI cancelled · acme/api main @ abc1234 · CI #7 cancelled", 1, False),
            (cmd, "usage: dev ci\nerror: invalid timeout", 2, False),
            (cmd + "; exit 3", timed, 3, False),
            ("printf '%s' 'dev ci watch --wait'; exit 3", timed, 3, False),
            (cmd, timed + "\nTraceback: unrelated failure", 3, False),
            (cmd, "CI failed · acme/api main @ abc1234 · CI #7 failed", 4, False),
            (cmd + " --bad", timed, 3, False),
        ]
        for deferred in (False, True):
            for command, output, code, expected in cases:
                with self.subTest(deferred=deferred, command=command, code=code, expected=expected):
                    item = self.record(command, output, code)
                    if deferred:
                        self.post(item)
                        self.append(item)
                        hooks._codex_settle("codex", {"session_id": "c1"})
                    else:
                        self.append(item)
                        self.post(item)
                    self.assertIs(self.outcomes()[-1], expected)

    def test_real_errors_remain_failures_after_expected_waits(self):
        # The successful wait assertion makes this fail without the fix; the adversarial cases prevent a broad
        # "ignore exit 2/3/5" fix from hiding a real command failure or breaking repeated-failure detection.
        item = self.record(WAIT, TIMEOUT, 3)
        self.append(item)
        self.post(item)
        self.assertEqual(self.outcomes(), [True])
        for cmd, output, code, status in [
            ("printf '%s' 'dev watch --wait'; exit 3", TIMEOUT, 3, "failed"),
            (WAIT + "; false", TIMEOUT, 1, "failed"),
            (WAIT + "; exit 3", TIMEOUT, 3, "failed"),
            ("dev watch --wait --bad", "usage: dev watch\nerror: unrecognized arguments: --bad", 2, "failed"),
            ("dev watch --wait --bad", TIMEOUT, 3, "failed"),
            (WAIT + " --timeout nope", TIMEOUT, 3, "failed"),
            (WAIT + " --for someone", TIMEOUT, 3, "failed"),
            (WAIT, TIMEOUT + "\nTraceback: unrelated failure", 3, "failed"),
            (WAIT, TIMEOUT, 3, "inProgress"),
            (WAIT, TIMEOUT, 2, "failed"),
            (WAIT, TIMEOUT, 3, "declined"),
            (WAIT, TIMEOUT, "3", "failed"),
            (WAIT, "Traceback: watch failed", 3, "failed"),
            ("python3 -c 'raise ValueError()'", "ValueError: failed", 3, "failed"),
        ]:
            with self.subTest(cmd=cmd, code=code, status=status):
                item = self.record(cmd, output, code, status)
                self.post(item)  # real failures also survive deferred settlement
                self.append(item)
                hooks._codex_settle("codex", {"session_id": "c1"})
                self.assertIs(self.outcomes()[-1], False)
                item = self.record(cmd, output, code, status)
                self.append(item)
                result = self.post(item)
                self.assertIs(self.outcomes()[-1], False)
        # A diagnostic is told once per streak, so only a new failure's second attempt must repeat this notice.
        self.assertIn(hooks.ROOT_CAUSE_STOP, result)


class CodexCrewDeliveryTests(WatchCase):
    def test_moot_delivery_keeps_its_audit_after_successful_output(self):
        self.run_with_member()
        original = pg.load_run("claude", "s1")
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent):
                run = dict(original, agent=agent, messages=[])
                run["agents"]["a1"]["state"] = "done"
                for step in run["plan"]:
                    step["state"] = "done"
                question = pg._post(run, "a1", "main", "May I update the contract?", self.t0,
                                    from_name="Tank")
                state.update(pg.run_path(agent, "s1"), lambda d: d.update(run))
                with mock.patch.object(pg, "_audit") as audit:
                    if agent == "codex":
                        with self.assertRaises(BrokenPipeError):
                            watch._messages(agent, "s1", "main", True, lambda e: True, 10,
                                            emit=mock.Mock(side_effect=BrokenPipeError("closed")))
                        audit.assert_not_called()
                        self.assertFalse(pg.load_run(agent, "s1")["messages"][0].get("delivered"))
                    lines = []
                    events, _ = watch._messages(agent, "s1", "main", True, lambda e: True, 10,
                                                emit=lines.append)
                    self.assertTrue(events[0]["line"].startswith("devkit · moot:"))
                    records = [call for call in audit.call_args_list if call.args[0] == "crew.message-moot"]
                    self.assertEqual(len(records), 1)
                    self.assertEqual(records[0].args[1], question["id"])
                    self.assertIn("holds no open step", records[0].kwargs["why"])
                    self.assertTrue(pg.load_run(agent, "s1")["messages"][0]["delivered"])
                    self.assertEqual(watch._messages(agent, "s1", "main", True, lambda e: True, 10,
                                                     emit=lines.append)[0], [])
                    self.assertEqual(len([c for c in audit.call_args_list
                                          if c.args[0] == "crew.message-moot"]), 1)

    def test_codex_acknowledges_only_messages_written_before_a_broken_pipe(self):
        self.run_with_member()
        run = dict(pg.load_run("claude", "s1"), agent="codex", messages=[])
        pg._post(run, "main", "a1", "First complete instruction", self.t0, from_name="Morpheus")
        pg._post(run, "main", "a1", "Second instruction must remain pending", self.t0, from_name="Morpheus")
        pg._post(run, "main", "other", "Other lane only", self.t0)
        state.update(pg.run_path("codex", "s1"), lambda d: d.update(run))
        lines = []

        def emit(line):
            if lines:
                raise BrokenPipeError("reader closed")
            lines.append(line)

        with self.assertRaises(BrokenPipeError):
            watch._deliver("codex", "s1", "a1", [], emit, "", "dev watch --wait", self.t0, self.clock.time)
        self.assertIn("First complete instruction", lines[0])
        stored = pg.load_run("codex", "s1")["messages"]
        self.assertTrue(stored[0]["delivered"])
        self.assertFalse(stored[1].get("delivered"))
        self.assertFalse(stored[2].get("delivered"))
        self.assertIn("Second instruction must remain pending", watch.pending("codex", "s1", "a1")[0]["line"])

    def test_codex_delivers_complete_sanitized_questions_and_answers_once(self):
        self.run_with_member()
        # The same stored crew run is delivered on each provider; only Codex's wait rendering changes.
        original = pg.load_run("claude", "s1")
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent):
                run = dict(original, agent=agent, messages=[])
                text = ("Read the contract first. " * 18 + "\x1b[31m\u202e“`Do not edit Claude.`”\n"
                        "Keep the final constraint. api_key=sk-" + "a" * 40)
                question = pg._post(run, "main", "a1", text, self.t0, from_name="Morpheus")
                answer = pg._post(run, "main", "a1", text, self.t0, from_name="Morpheus", kind="answer")
                answer["reply_to"] = "M0"
                unrelated = pg._post(run, "main", "someone-else", "Other lane only", self.t0)
                state.update(pg.run_path(agent, "s1"), lambda d: d.update(run))
                peek = watch.pending(agent, "s1", "a1")
                self.assertEqual(len(peek), 2)
                self.assertFalse(any(m.get("delivered") for m in pg.load_run(agent, "s1")["messages"]))
                first = watch._collect(agent, "s1", "a1", True, only=["question"], limit=1)
                line = first[0][0][0]["line"]
                self.assertTrue(line.startswith("devkit · question M1 from Tech lead (Morpheus): “"), line)
                if agent == "codex":
                    self.assertIn('"Do not edit Claude." Keep the final constraint.', line)
                    self.assertNotIn("sk-" + "a" * 40, line)
                    self.assertNotIn("\x1b", line)
                    self.assertNotIn("\u202e", line)
                    self.assertNotIn("`", line)
                    self.assertEqual(line.count("“"), 1)
                    self.assertEqual(line.count("”"), 1)
                else:
                    self.assertNotIn("Keep the final constraint.", line)
                    self.assertIn("…”", line)
                stored = {m["id"]: m for m in pg.load_run(agent, "s1")["messages"]}
                self.assertTrue(stored[question["id"]]["delivered"])
                self.assertFalse(stored[answer["id"]].get("delivered"))
                self.assertFalse(stored[unrelated["id"]].get("delivered"))
                delivered = watch._collect(agent, "s1", "a1", True, only=["question"])
                line = delivered[0][0][0]["line"]
                self.assertIn("answer M2 from Tech lead (Morpheus) to M0", line)
                if agent == "codex":
                    self.assertIn("Keep the final constraint.", line)
                self.assertEqual(watch.pending(agent, "s1", "a1"), [])
