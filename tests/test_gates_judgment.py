# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The crew gates judge every stop of a member, on Claude Code and on Codex: a send-back is keyed by the lane's life
(its start, or its last resume), so a member sent back once is let through on its next stop of that life, but that
stop is still judged. A clean ✓ then writes the pass token, so a review step ends done and its approval counts; a stop
let through with findings writes none, and its findings are kept for the tech lead (`gates.let_through`). A resumed
lane is a new life, which may be sent back once again.

Run: python3 -m unittest tests.test_gates_judgment -v"""
import json
import os
import shlex
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))
from devkit import audit, gates  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_acceptance_crew_sessions import HOOK_BOOT  # noqa: E402
from test_approvals_token import StopCase  # noqa: E402
from test_codex_launch import LEAD, TANK, CodexCrew, _line, _now  # noqa: E402

SUBAGENT_STOP_KEYS = {"continue", "decision", "reason", "stopReason", "suppressOutput", "systemMessage"}
SERAPH = "01a1c190-64c7-7ac1-81c6-000000000009"
CODEX_REPORT = ("**[Firefly> DevKit · {role}]** api · `feat/invoice-export` · {task}\n- **Done:** {task}\n"
                "- **Check:** ✓ 12 tests pass\n- **Next:** the tech lead merges it.\n\nNeeds from the tech lead: nothing")


class ClaudeStopJudgmentTests(StopCase):
    # The Code reviewer holds "review"; each stop goes through Claude Code's SubagentStop hook
    def session(self) -> dict:
        return json.loads(self.session_file().read_text())

    def test_a_clean_second_stop_after_a_send_back_passes_the_review(self):
        self.assertEqual(json.loads(self.stop() or "{}").get("decision"), "block")  # no dev progress step
        self.assertEqual(self.stop(self.passing(), stop_hook_active=True), "")  # fixed: same life, judged again
        why = [h["why"] for h in self.run_()["history"] if h["step"] == "review"][-1]
        self.assertEqual(self.plan()["review"]["state"], "done", why)
        self.assertEqual(pg.approvals_owed(self.run_()), [])

    def test_findings_on_a_second_stop_of_the_same_life_are_kept_for_the_tech_lead_without_a_token(self):
        self.assertEqual(json.loads(self.stop() or "{}").get("decision"), "block")
        self.assertEqual(self.stop(stop_hook_active=True), "")  # still no dev progress step: let through
        kept = gates.let_through(self.session(), "a_seraph")
        self.assertEqual([(f["rule"], f["item"]) for f in kept], [("proofs", "dev progress step")])
        self.assertNotIn("a_seraph", self.tokens())
        self.assert_review_approves_nothing()
        recs = audit.records(1, action="crew.gate")
        self.assertEqual([(r["outcome"], (r.get("ref") or {}).get("result")) for r in recs],
                         [("blocked", None), ("recorded", "let through")])
        self.assertIn("proofs: dev progress step", recs[1]["detail"])

    def test_a_resumed_lane_with_findings_is_sent_back_once_more(self):
        self.stop()
        self.stop(stop_hook_active=True)
        pg.reopen("review", why="the reviewer is resumed for a second verdict", now=self.at(30))
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a_seraph", "agent_type": "devkit:seraph"}, True,
                       now=self.at(31))
        out = json.loads(self.stop() or "{}")  # a new life: its first stop with findings goes back
        self.assertEqual(out.get("decision"), "block", out)
        self.assertIn("the crew gates found 1 thing missing", out.get("reason", ""))

    def test_a_resumed_reviewer_that_records_its_approval_passes_the_review(self):
        # sent back in its first life and let through without its step; resumed, it records the review with
        # --step review --approves api and a ✓ check, and reports ✓: that stop is judged again and approves the work
        self.stop()
        self.stop(stop_hook_active=True)
        self.assert_review_approves_nothing()
        pg.reopen("review", why="the reviewer is resumed to record its verdict", now=self.at(30))
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a_seraph", "agent_type": "devkit:seraph"}, True,
                       now=self.at(31))
        pg.step("review done", which="review", check="✓ read the diff, 9 tests pass", from_agent="a_seraph",
                approves="api", now=self.at(32))
        tr = self.transcript('dev progress step "review done" --step review --approves api '
                             '--check "✓ read the diff, 9 tests pass"')
        self.assertEqual(self.stop(tr), "")
        why = [h["why"] for h in self.run_()["history"] if h["step"] == "review"][-1]
        self.assertEqual(self.plan()["review"]["state"], "done", why)
        self.assertEqual(pg.approvals_owed(self.run_()), [])

    def test_a_judge_given_to_the_gates_decides_the_stop(self):
        # the seam a model-based test drives: the judge's findings, not the real gates, decide the verdict
        sess = {}
        data = {"session_id": "s1", "agent_id": "a_seraph", "agent_type": "devkit:seraph",
                "agent_transcript_path": str(self.transcript()), "last_assistant_message": self.REPORT}
        self.assertIsNone(gates.check("Seraph", data, sess, judge=lambda *a, **k: []))
        self.assertIn("a_seraph", sess.get(gates.PASSED) or {})


class CodexStopJudgmentTests(CodexCrew):
    # The same judgment on Codex's path: SubagentStart binds the member by its task_name, every member command is a
    # shell call its own rollout records as a CommandExecution item, and SubagentStop names that rollout. A spawned
    # agent's rollout is written as Codex 0.167.0 writes it: its session_meta says where its own history starts
    # (subagent_history_start_ordinal), its own turn opens with task_started, every record has its ordinal, and a
    # CommandExecution item names its folder
    def agent_rollout(self, s, aid: str, member: str, task: str, parent: str = "", depth: int = 1, source=None):
        path = super().agent_rollout(s, aid, member, task, parent, depth, source)
        meta = json.loads(path.read_text())
        meta["ordinal"] = 0
        meta["payload"]["subagent_history_start_ordinal"] = 1
        turn = {"timestamp": _now(), "ordinal": 1, "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "01a1c178-651e-7bc1-a532-a2415b26e099"}}
        path.write_text(_line(meta) + _line(turn))
        return path

    def member(self, s, aid: str, member: str, *args) -> dict:
        r = s.dev(*args, member=aid, member_type=member)
        cmd = r["ran"]  # as it ran: with the stamps devkit's PreToolUse put in front of it, as Codex records it
        self.assertIn(" ".join(["dev"] + [shlex.quote(str(a)) for a in args]), cmd)
        item = {"type": "CommandExecution", "id": r["call"]["tool_use_id"], "command": ["/bin/zsh", "-lc", cmd],
                "cwd": f"file://{s.repo}", "status": "completed" if r["rc"] == 0 else "failed",
                "exit_code": r["rc"], "aggregated_output": (r["out"] + r["err"])[-2000:]}
        path = self.rollout(aid)
        ordinal = len(path.read_text().splitlines())
        with path.open("a") as fh:
            fh.write(_line({"timestamp": _now(), "ordinal": ordinal, "type": "event_msg", "payload": {
                "type": "item_completed", "turn_id": "01a1c178-28b7-7fa1-a67c-2bd8b7433d66", "item": item}}))
        self.assertEqual(r["rc"], 0, r["err"])
        return r

    def stop(self, s, aid: str, member: str, report: str, again: bool = False) -> dict:
        """SubagentStop as Codex 0.167.0 sends it: the member's own rollout as its transcript. The crew gates credit
        a `dev` command only when `dev` on the hook's PATH is devkit's own bin/dev, as on a Mac devkit installed:
        the sandbox's commands run through a stand-in launcher, so this hook gets a folder first on PATH whose
        `dev` is the checkout's bin/dev."""
        trusted = self.home / "devkit-bin"
        if not trusted.exists():
            trusted.mkdir()
            (trusted / "dev").symlink_to(ROOT / "bin" / "dev")
        env = {name: os.pathsep.join(p for p in (str(trusted), s.host.env.get(name, "")) if p)
               for name in ("PATH", "DEVKIT_TEST_PATH")}
        payload = s.payload("SubagentStop", agent_id=aid, agent_type=member, model="gpt-6-luna",
                            stop_hook_active=again, agent_transcript_path=str(self.rollout(aid)),
                            last_assistant_message=report)
        out = s._hook_result(s.host.send([sys.executable, "-I", "-S", "-c", HOOK_BOOT, ROOT / "lib", "subagent-stop",
                                          s.agent, s.mac.spawns], s.repo, env=env, stdin=json.dumps(payload)))
        self.assertEqual(set(out) - SUBAGENT_STOP_KEYS, set(), out)  # what Codex's output schema accepts
        return out

    def review_state(self, s) -> str:
        return next(st for st in s.run_doc()["plan"] if st["name"] == "review")["state"]

    def test_a_codex_reviewers_clean_second_stop_after_a_send_back_passes_the_review(self):
        self.config(max_parallel=3, reserve=0)
        s = self.codex(LEAD, self.api)
        s.ok("progress", "start", "Invoice export", "--plan", "api=30m [backend]; review=20m [review] after api")
        s.ok("progress", "assign", "api", "--to", "Tank", "--scope", "api/**")
        self.spawn(s, TANK, "tank", "api")
        self.member(s, TANK, "tank", "progress", "step", "api committed", "--step", "api", "--check", "✓ 12 pass")
        self.stop(s, TANK, "tank", CODEX_REPORT.format(role="Backend engineer (Tank)", task="api"))
        s.ok("progress", "assign", "review", "--to", "Seraph")
        self.spawn(s, SERAPH, "seraph", "review")
        report = CODEX_REPORT.format(role="Code reviewer (Seraph)", task="review")
        first = self.stop(s, SERAPH, "seraph", report)
        self.assertEqual(first.get("decision"), "block", first)
        self.assertIn("the crew gates found", first.get("reason", ""))
        self.member(s, SERAPH, "seraph", "progress", "step", "review done", "--step", "review", "--check",
                    "✓ read the diff, 12 tests pass")
        self.assertEqual(self.stop(s, SERAPH, "seraph", report, again=True), {})
        why = [h["why"] for h in s.run_doc()["history"] if h["step"] == "review"][-1]
        self.assertEqual(self.review_state(s), "done", why)


if __name__ == "__main__":
    unittest.main()
