# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The crew coordination faults of devkit 26.11.25 (2026-11-19), on Codex's host path: a Codex tech lead's thread,
its members spawned with spawn_agent and bound at SubagentStart by their task_name (Codex sends no PreToolUse for the
spawn), each member command a shell call through PreToolUse and PostToolUse that its own rollout also records as a
CommandExecution item, and notices that reach a member at its next PostToolUse. Real `dev` processes and hooks, as
tests/test_codex_launch.py drives them. The Claude Code variants are in tests/test_coordination_lanes.py and
tests/test_coordination_identity.py. Run: python3 -m unittest tests.test_coordination_codex"""
import json
import os
import shlex
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_acceptance_crew_sessions import HOOK_BOOT  # noqa: E402
from test_codex_launch import LEAD, TANK, CodexCrew, _line, _now  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _hermetic  # noqa: E402

_hermetic.activate()  # a probe that imports this module by path gets the suite's temporary home too

from devkit import messaging  # noqa: E402

SERAPH = "01a1c190-64c7-7ac1-81c6-000000000007"
REPORT = ("**[Firefly> DevKit · {role}]** api · `feat/invoice-export` · {task}\n- **Done:** {task}\n"
          "- **Check:** ✓ 12 tests pass\n- **Next:** the tech lead merges it.\n\nNeeds from the tech lead: nothing")


class CodexCoordination(CodexCrew):
    def run_lead(self, plan: str, *assign):
        """A Codex tech lead's run for `plan`, each (step, member, scope) assigned by role."""
        self.config(max_parallel=3, reserve=0)
        s = self.codex(LEAD, self.api)
        s.ok("progress", "start", "Invoice export", "--plan", plan)
        for step, name, scope in assign:
            s.ok("progress", "assign", step, "--to", name, "--scope", scope)
        return s

    TURN = "01a1f737-3cdb-7402-bf18-000000000003"

    def agent_rollout(self, s, aid: str, member: str, task: str, parent: str = "", depth: int = 1, source=None):
        """The spawned agent's own rollout as Codex 0.167.2 writes it (tests/test_gates_codex_proofs.py): a
        session_meta that is not a fork (no forked_from_id, history_mode paginated, source.subagent.thread_spawn),
        its turn_context, then task_started, every record with its ordinal."""
        path = super().agent_rollout(s, aid, member, task, parent, depth, source)
        meta = json.loads(path.read_text())
        meta["payload"].pop("forked_from_id", None)
        meta["payload"].update(cli_version="0.167.2", history_mode="paginated", multi_agent_version="v2")
        path.write_text("")
        self.record(aid, "session_meta", meta["payload"])
        self.record(aid, "turn_context", {"turn_id": self.TURN, "cwd": str(s.repo), "approval_policy": "never",
                                          "sandbox_policy": {"type": "danger-full-access"}, "model": "gpt-6-luna"})
        self.record(aid, "event_msg", {"type": "task_started", "turn_id": self.TURN})
        return path

    def record(self, aid: str, kind: str, payload: dict):
        path = self.rollout(aid)
        ordinal = len(path.read_text().splitlines()) if path.exists() else 0
        with path.open("a") as fh:
            fh.write(_line({"timestamp": _now(), "ordinal": ordinal, "type": kind, "payload": payload}))

    def member(self, s, aid: str, member: str, *args) -> dict:
        """A member's `dev` command: its shell call through the hooks, and what its own rollout records for it, as
        Codex 0.167.2 does in code mode: the exec call, the CommandExecution item of the command as it ran (the
        PreToolUse hook's rewrite, devkit's stamps in front of it) with its thread, folder and exit code, then the
        call's output."""
        r = s.dev(*args, member=aid, member_type=member)
        ran, call = r["ran"], f"call_{r['call']['tool_use_id'][-6:]}"
        self.record(aid, "response_item", {"type": "custom_tool_call", "status": "completed", "call_id": call,
                                           "name": "exec",
                                           "input": f"text(await tools.exec_command({{cmd:{json.dumps(ran)}}}))\n"})
        self.record(aid, "event_msg", {"type": "item_completed", "thread_id": aid, "turn_id": self.TURN, "item": {
            "type": "CommandExecution", "id": r["call"]["tool_use_id"], "process_id": "99924",
            "command": ["/bin/zsh", "-lc", ran], "cwd": f"file://{s.repo}",
            "parsed_cmd": [{"type": "unknown", "cmd": ran}], "source": "unified_exec_startup",
            "status": "completed" if r["rc"] == 0 else "failed", "exit_code": r["rc"],
            "aggregated_output": (r["out"] + r["err"])[-2000:], "duration": {"secs": 0, "nanos": 2208}}})
        self.record(aid, "response_item", {"type": "custom_tool_call_output", "call_id": call, "output": [
            {"type": "input_text", "text": "Script completed\nWall time 0.3 seconds\nOutput:\n"}]})
        return r

    def stop(self, s, aid: str, member: str, report: str, again: bool = False) -> dict:
        """SubagentStop as Codex sends it, the member's own rollout as its transcript. The crew gates credit a
        `dev` command only when `dev` on the hook's PATH is devkit's own bin/dev, so this hook gets one first."""
        trusted = self.home / "devkit-bin"
        if not trusted.exists():
            trusted.mkdir()
            (trusted / "dev").symlink_to(ROOT / "bin" / "dev")
        env = {name: os.pathsep.join(p for p in (str(trusted), s.host.env.get(name, "")) if p)
               for name in ("PATH", "DEVKIT_TEST_PATH")}
        payload = s.payload("SubagentStop", agent_id=aid, agent_type=member, model="gpt-6-luna",
                            stop_hook_active=again, agent_transcript_path=str(self.rollout(aid)),
                            last_assistant_message=report)
        return s._hook_result(s.host.send([sys.executable, "-I", "-S", "-c", HOOK_BOOT, ROOT / "lib", "subagent-stop",
                                           s.agent, s.mac.spawns], s.repo, env=env, stdin=json.dumps(payload)))

    def held(self, s, name: str) -> tuple:
        st = self.step(s, name)
        return st.get("owner"), st.get("state")


class CodexLaneTests(CodexCoordination):
    def test_a_codex_members_checked_record_without_step_closes_its_lanes_only_running_step(self):
        s = self.run_lead("api=30m [backend]; ui=30m [frontend]", ("api", "Tank", "api/**"))
        self.spawn(s, TANK, "tank", "api")
        self.assertEqual(self.held(s, "api"), (TANK, "running"))
        r = self.member(s, TANK, "tank", "progress", "step", "the endpoint streams CSV", "--check", "✓ 12 tests pass")
        self.assertEqual(r["rc"], 0, r["err"])
        self.assertIn("✓", r["out"])
        self.assertEqual(self.held(s, "api"), (TANK, "done"), r["out"])

    def test_a_codex_lane_whose_step_closes_takes_the_first_of_two_steps_assigned_to_its_member(self):
        s = self.run_lead("api=30m [backend]; export=30m [backend]; import=30m [backend]",
                          ("api", "Tank", "api/**"), ("export", "Tank", "export/**"), ("import", "Tank", "import/**"))
        self.spawn(s, TANK, "tank", "api")
        self.assertEqual(self.held(s, "api"), (TANK, "running"))
        r = self.member(s, TANK, "tank", "progress", "step", "api committed", "--step", "api", "--check", "✓ 12 pass")
        self.assertEqual(r["rc"], 0, r["err"])
        self.assertEqual((self.held(s, "export"), self.held(s, "import")), ((TANK, "running"), ("main", "pending")))

    def test_a_blocked_codex_step_frees_the_lane_and_the_member_hears_of_its_next_step(self):
        s = self.run_lead("api=30m [backend]; export=30m [backend]", ("api", "Tank", "api/**"))
        self.spawn(s, TANK, "tank", "api")
        s.ok("progress", "block", "the API contract waits for the developer", "--step", "api")
        s.ok("progress", "assign", "export", "--to", "Tank", "--scope", "export/**")
        self.assertEqual(self.held(s, "export"), (TANK, "running"))
        heard = self.member_hears(s, TANK, "tank")  # the member's next PostToolUse
        self.assertNotIn("joins your lane once", heard)

    def test_a_resumed_codex_lane_takes_the_one_step_assigned_to_its_member_by_role(self):
        s = self.run_lead("api=30m [backend]; export=30m [backend]", ("api", "Tank", "api/**"))
        self.spawn(s, TANK, "tank", "api")
        self.member(s, TANK, "tank", "progress", "step", "api committed", "--step", "api", "--check", "✓ 12 pass")
        self.stop(s, TANK, "tank", REPORT.format(role="Backend engineer (Tank)", task="api"))
        s.ok("progress", "assign", "export", "--to", "Tank", "--scope", "export/**")
        self.subagent_start(s, TANK, "tank")  # resumed: Codex starts the same thread again
        self.assertEqual(self.held(s, "export"), (TANK, "running"))
        self.assertEqual(s.run_doc()["agents"][TANK].get("scope"), ["export/**"])

    def test_a_resumed_codex_reviewers_clean_report_approves_after_an_earlier_send_back(self):
        # the review lanes that ended unverified on Codex: the reviewer was sent back once, resumed with
        # SubagentStart, recorded its review with --approves and a ✓ Check, and its clean report was never judged
        s = self.run_lead("api=30m [backend]; review=20m [review] after api", ("api", "Tank", "api/**"))
        self.spawn(s, TANK, "tank", "api")
        self.member(s, TANK, "tank", "progress", "step", "api committed", "--step", "api", "--check", "✓ 12 pass")
        self.stop(s, TANK, "tank", REPORT.format(role="Backend engineer (Tank)", task="api"))
        s.ok("progress", "assign", "review", "--to", "Seraph")
        self.spawn(s, SERAPH, "seraph", "review")
        report = REPORT.format(role="Code reviewer (Seraph)", task="review")
        self.assertEqual(self.stop(s, SERAPH, "seraph", report).get("decision"), "block")  # no step recorded yet
        self.stop(s, SERAPH, "seraph", report, again=True)  # let through
        self.assertEqual(self.held(s, "review")[1], "unverified")
        s.ok("progress", "reopen", "review", "--why", "the reviewer gives the verdict again")
        self.subagent_start(s, SERAPH, "seraph")  # resumed: Codex starts the same thread again
        self.assertEqual(self.held(s, "review"), (SERAPH, "running"))
        r = self.member(s, SERAPH, "seraph", "progress", "step", "review done", "--step", "review", "--check",
                        "✓ read the diff, 12 tests pass", "--approves", "api")
        self.assertEqual(r["rc"], 0, r["err"])
        self.stop(s, SERAPH, "seraph", report)
        why = [h["why"] for h in s.run_doc()["history"] if h["step"] == "review"][-1]
        self.assertEqual(self.held(s, "review")[1], "done", why)


class CodexCallerTests(CodexCoordination):
    # On Codex the tech lead's own commands were "unverified", which main-only commands allow, so a member's open
    # wait or quoting question never took them; the tech lead's compound acknowledgement was refused
    def codex_policy(self, policy: str):
        """Codex's config.toml in the test's own home, as dev setup writes it on a Mac where Codex never asks."""
        home = self.home / ".codex"
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text(f'approval_policy = "{policy}"\n')

    def compound_ack(self):
        """The tech lead acknowledges a member's report with a digest read by a nested dev crew read in the same
        command -> (the command's result, the message as the messaging store keeps it)."""
        s = self.run_lead("api=30m [backend]", ("api", "Tank", "api/**"))
        self.spawn(s, TANK, "tank", "api")
        self.member(s, TANK, "tank", "crew", "ask", "Report: " + "x" * 400)
        ref = next(m["id"] for m in s.run_doc()["messages"] if m.get("to") == "main")
        r = s.sh(f'dev crew ack {ref} --sha256 "$(dev crew read {ref} --json | python3 -I -c \'import json,sys; '
                 f'print(json.load(sys.stdin)["digest"])\')" --generation 1')
        store = self.state / "messaging-v2" / "runs" / f"{messaging.run_id(s.run_doc())}.json"
        return r, next(m for m in json.loads(store.read_text())["messages"] if m["id"] == ref)

    def test_the_codex_tech_lead_acknowledges_with_a_digest_read_in_the_same_command(self):
        self.codex_policy("never")  # Codex never asks: every shell call carries its call token
        r, message = self.compound_ack()
        self.assertEqual(r["rc"], 0, r["out"] + r["err"])
        self.assertIn("main", message.get("receipts") or {})

    def test_a_codex_compound_ack_that_codex_may_ask_about_names_the_plain_call_fix(self):
        # with no config.toml Codex may ask about the call, so devkit gives it no token, and a compound command
        # cannot tell which thread runs it: it is refused, saying to run the ack as one plain dev command
        r, message = self.compound_ack()
        self.assertNotEqual(r["rc"], 0, r["out"])
        self.assertIn("run it as one plain dev command", r["out"] + r["err"])
        self.assertNotIn("main", message.get("receipts") or {})


if __name__ == "__main__":
    unittest.main()
