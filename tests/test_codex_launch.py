# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Codex crew launches: the launch record, the crew slots, the depth limit, the binding to a plan step, and the
member's required skills, all made at SubagentStart.

Codex CLI 0.167.0 fires no PreToolUse for `collaboration.spawn_agent`, so nothing can refuse or rewrite a Codex launch
before its agent starts. What it gives (captured on 2026-11-09): SubagentStart with `agent_id` and `agent_type`, its
`transcript_path` the new agent's own rollout, whose session_meta names the spawn (`source.subagent.thread_spawn`:
parent thread, depth, `agent_path` /root/<task_name>); the parent's rollout holds the `spawn_agent` call (`task_name`
in plain text, the brief encrypted) and a `SubAgentActivity` item that joins the call to the agent; and SubagentStart
output accepts `hookSpecificOutput.additionalContext`. The record shapes below are copied from that capture.

Every hook runs as Codex runs it: `dev hook <event> --agent codex` with the payload on stdin, in a sandbox Mac
(tests/test_acceptance_crew_sessions.py, tests/_sandbox.py).

Run: python3 -m unittest tests.test_codex_launch -v (and with /usr/bin/python3, for Python 3.9)
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_acceptance_crew_sessions import Crew  # noqa: E402  (activates the hermetic net and the sandbox)

LEAD = "01a1c190-2665-7ca1-9a5d-046a95cee8bc"  # the Codex tech lead's thread (UUIDv7, like Codex's own)
CLAUDE_LEAD = "7b2e0c4d-1a2b-4c3d-8e9f-0a1b2c3d4e5f"
TANK = "01a1c190-64c7-7ac1-81c6-000000000001"
TANK_2 = "01a1c190-64c7-7ac1-81c6-000000000005"  # a second Backend engineer, spawned in the same turn
TRINITY = "01a1c190-64c7-7ac1-81c6-000000000002"
HELPER = "01a1c190-64c7-7ac1-81c6-000000000003"
REVIEWER = "01a1c190-64c7-7ac1-81c6-000000000004"
BRIEF = "gAAAAABq8gpXjj5jSrk7DNZsjERW3HLAzwcFxC3osbfOZq1eLFOwX9OY0PNTLmLRTZ-w5dfnusBVXc2tjIulRwmdOH9H81mHvCMeanY="
SUBAGENT_START_KEYS = {"continue", "hookSpecificOutput", "stopReason", "suppressOutput", "systemMessage"}


def _line(rec: dict) -> str:
    return json.dumps(rec) + "\n"


class CodexCrew(Crew):
    """A Codex tech lead's thread and its spawns, written as Codex 0.167.0 writes them."""

    def rollout(self, thread: str) -> Path:
        return self.home / ".codex" / "sessions" / "2026" / "11" / "09" / f"rollout-2026-11-09T10-00-55-{thread}.jsonl"

    def parent_rollout(self, s, parent: str = "") -> Path:
        """The rollout of the thread that spawns: the tech lead's (the session's transcript) or a member's."""
        path = self.rollout(parent) if parent else s.transcript
        if not path.exists() or not path.read_text().strip():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_line({"timestamp": "2026-11-09T18:00:39.934Z", "type": "session_meta", "payload": {
                "session_id": s.sid, "id": parent or s.sid, "cli_version": "0.167.0", "originator": "codex_exec",
                "source": "exec"}}))
        return path

    def spawn_call(self, s, member: str, task: str, call: str, parent: str = ""):
        """The spawn_agent call in the spawning thread's rollout: task_name in plain text, the brief encrypted."""
        with self.parent_rollout(s, parent).open("a") as fh:
            fh.write(_line({"timestamp": "2026-11-09T18:00:55.217Z", "type": "response_item", "payload": {
                "type": "function_call", "status": "completed", "id": f"fc_{call}", "name": "spawn_agent",
                "namespace": "collaboration", "call_id": call,
                "arguments": json.dumps({"task_name": task, "agent_type": member, "message": BRIEF})}}))

    def spawn_started(self, s, aid: str, task: str, call: str, parent: str = ""):
        """Codex's record that the call started the agent (`SubAgentActivity`, its id the call's)."""
        with self.parent_rollout(s, parent).open("a") as fh:
            fh.write(_line({"timestamp": "2026-11-09T18:00:55.326Z", "type": "event_msg", "payload": {
                "type": "item_completed", "thread_id": parent or s.sid, "turn_id": "01a1c178-28b7-7fa1-a67c-2bd8b7433d66",
                "item": {"type": "SubAgentActivity", "model": "gpt-6-luna", "reasoning_effort": "low", "id": call,
                         "kind": "started", "agent_thread_id": aid, "agent_path": f"/root/{task}"}}}))
            fh.write(_line({"timestamp": "2026-11-09T18:00:55.336Z", "type": "response_item", "payload": {
                "type": "function_call_output", "call_id": call, "output": json.dumps({"task_name": f"/root/{task}"})}}))

    def agent_rollout(self, s, aid: str, member: str, task: str, parent: str = "", depth: int = 1, source=None):
        """The new agent's own rollout, which opens with its session_meta (a spawn: source.subagent.thread_spawn)."""
        path = self.rollout(aid)
        path.parent.mkdir(parents=True, exist_ok=True)
        where = f"/root/{task}" if not parent else f"/root/{parent[-4:]}/{task}"
        src = source if source is not None else {"subagent": {"thread_spawn": {
            "parent_thread_id": parent or s.sid, "depth": depth, "agent_path": where, "agent_nickname": "Halley",
            "agent_role": member}}}
        path.write_text(_line({"timestamp": "2026-11-09T18:00:55.270Z", "type": "session_meta", "payload": {
            "session_id": s.sid, "id": aid, "forked_from_id": parent or s.sid, "parent_thread_id": parent or s.sid,
            "cwd": str(s.repo), "originator": "codex_exec", "cli_version": "0.167.0", "source": src,
            "thread_source": "subagent", "agent_role": member, "agent_path": where}}))
        return path

    def subagent_start(self, s, aid: str, member: str) -> dict:
        """SubagentStart as Codex 0.167.0 sends it: the transcript is the new agent's own rollout."""
        return s.hook("subagent-start", s.payload("SubagentStart", agent_id=aid, agent_type=member,
                                                   model="gpt-6-luna", transcript_path=str(self.rollout(aid))))

    def spawn(self, s, aid: str, member: str, task: str, parent: str = "", depth: int = 1) -> dict:
        """The whole launch as Codex records it, then SubagentStart -> what the hook printed."""
        call = f"call_{aid[-6:]}"
        self.spawn_call(s, member, task, call, parent)
        self.agent_rollout(s, aid, member, task, parent, depth)
        self.spawn_started(s, aid, task, call, parent)
        return self.subagent_start(s, aid, member)

    def session_doc(self, s) -> dict:
        return json.loads((self.state / "sessions" / f"{s.agent}-{s.sid}.json").read_text())

    def launch_of(self, s, aid: str) -> dict:
        return next((dict(v, key=k) for k, v in (self.session_doc(s).get("launches") or {}).items()
                     if v.get("agent_id") == aid), {})

    def step(self, s, name: str) -> dict:
        return next(st for st in s.run_doc()["plan"] if st["name"] == name)

    def lead(self, plan="api=30m [backend]; ui=30m [frontend]", **progress):
        """A Codex tech lead with a run whose steps are assigned to Tank (api) and Trinity (ui)."""
        self.config(**{"max_parallel": 2, "reserve": 0, **progress})
        s = self.codex(LEAD, self.api)
        s.ok("progress", "start", "Invoice export", "--plan", plan)
        for step, name in (("api", "Tank"), ("ui", "Trinity")):
            if f"{step}=" in plan:
                s.ok("progress", "assign", step, "--to", name, "--scope", f"{step}/**")
        return s

    @staticmethod
    def context(out: dict) -> str:
        return ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext") or ""


class CodexLaunchRecord(CodexCrew):
    """A Codex spawn gets the launch record and the lane a Claude Code Agent call gets."""

    def test_a_codex_launch_binds_to_the_step_its_task_name_names(self):
        s = self.lead()
        self.spawn(s, TANK, "tank", "api")
        lane = s.run_doc()["agents"][TANK]
        self.assertEqual((lane["binding"], lane["label"], lane["parent"], lane["depth"], lane["persona"]),
                         ("exact", "Tank: api", "main", 1, "Tank"))
        self.assertEqual((self.step(s, "api")["owner"], self.step(s, "api")["state"]), (TANK, "running"))
        self.assertEqual(self.step(s, "ui")["owner"], "main")  # Trinity's step waits for Trinity's own launch
        rec = self.launch_of(s, TANK)
        self.assertEqual((rec["key"], rec["state"], rec["type"], rec["parent"]),
                         (f"call_{TANK[-6:]}", "started", "tank", "main"))

    def test_a_task_name_in_snake_case_binds_the_step_whose_words_it_names(self):
        s = self.lead(plan="invoice csv=30m [backend]")
        s.ok("progress", "assign", "invoice csv", "--to", "Tank")
        self.spawn(s, TANK, "tank", "invoice_csv")
        self.assertEqual(self.step(s, "invoice csv")["owner"], TANK)

    def test_a_codex_launch_holds_a_crew_slot(self):
        s = self.lead()
        self.spawn(s, TANK, "tank", "api")
        self.assertIn("1 of 2 slots in use", s.ok("crew", "next"))

    def test_a_launch_binds_from_the_parents_rollout_before_its_own_is_written(self):
        s = self.lead()
        call = "call_parent_only"
        self.spawn_call(s, "tank", "api", call)
        self.spawn_started(s, TANK, "api", call)
        self.rollout(TANK).parent.mkdir(parents=True, exist_ok=True)
        self.rollout(TANK).write_text("")  # Codex has not written the agent's session_meta yet
        self.subagent_start(s, TANK, "tank")
        self.assertEqual(self.launch_of(s, TANK)["key"], call)
        self.assertEqual(self.step(s, "api")["owner"], TANK)

    def test_a_launch_takes_the_newest_unbound_spawn_of_its_agent_type_when_nothing_names_it(self):
        s = self.lead()
        self.spawn(s, TRINITY, "trinity", "ui")  # an earlier spawn, already bound to its agent
        self.spawn_call(s, "tank", "api", "call_tank_newest")  # no SubAgentActivity yet, no session_meta
        self.subagent_start(s, TANK, "tank")
        self.assertEqual(self.launch_of(s, TANK)["key"], "call_tank_newest")
        self.assertEqual(self.launch_of(s, TRINITY)["key"], f"call_{TRINITY[-6:]}")  # untouched

    def test_two_starts_at_once_with_two_spawns_of_their_type_waiting_take_neither_and_the_lead_is_told(self):
        s = self.lead(plan="api=30m [backend]; db=30m [backend]")
        s.ok("progress", "assign", "db", "--to", "Tank", "--scope", "db/**")
        self.spawn_call(s, "tank", "api", "call_tank_api")  # Codex wrote both calls, no SubAgentActivity yet,
        self.spawn_call(s, "tank", "db", "call_tank_db")  # and neither agent's session_meta
        rids = []
        for aid in (TANK, TANK_2):
            self.rollout(aid).parent.mkdir(parents=True, exist_ok=True)
            self.rollout(aid).write_text("")
            rids.append(s.hook("subagent-start", s.payload("SubagentStart", agent_id=aid, agent_type="tank",
                                                            model="gpt-6-luna", transcript_path=str(self.rollout(aid))),
                               wait=False))
        for rid in rids:
            s._hook_result(rid)
        self.assertEqual([self.launch_of(s, aid).get("key") for aid in (TANK, TANK_2)],
                         [f"codex-{TANK}", f"codex-{TANK_2}"])  # neither guessed: one would take the other's task
        self.assertEqual([self.step(s, name)["owner"] for name in ("api", "db")], ["main", "main"])
        told = [m["text"] for m in s.messages() if m.get("to") == "main" and "could not tell which" in m["text"]]
        self.assertEqual(sorted(aid for aid in (TANK, TANK_2) for t in told if aid in t), sorted([TANK, TANK_2]), told)
        self.assertTrue(all("“api”" in t and "“db”" in t for t in told), told)

    def test_of_two_spawns_of_its_type_waiting_a_start_takes_the_one_its_own_task_names(self):
        s = self.lead(plan="api=30m [backend]; db=30m [backend]")
        self.spawn_call(s, "tank", "api", "call_tank_api")
        self.spawn_call(s, "tank", "db", "call_tank_db")
        self.agent_rollout(s, TANK, "tank", "api")  # its session_meta names its task; no SubAgentActivity yet
        self.subagent_start(s, TANK, "tank")
        self.assertEqual(self.launch_of(s, TANK)["key"], "call_tank_api")

    def test_codex_own_helper_takes_no_launch_from_the_spawn_waiting_for_its_agent(self):
        s = self.lead()
        self.spawn_call(s, "", "list_files", "call_worker")  # a spawn of Codex's default role, its agent not started
        self.agent_rollout(s, REVIEWER, "default", "review", source={"subagent": "review"})
        self.assertEqual(self.subagent_start(s, REVIEWER, "default"), {})  # Codex's own review helper starts first
        self.agent_rollout(s, HELPER, "default", "list_files")
        self.spawn_started(s, HELPER, "list_files", "call_worker")
        self.subagent_start(s, HELPER, "default")
        self.assertEqual(self.launch_of(s, REVIEWER), {})
        self.assertNotIn(REVIEWER, s.run_doc().get("agents") or {})
        self.assertEqual(self.launch_of(s, HELPER)["key"], "call_worker")

    def test_a_second_start_of_the_same_agent_records_no_second_launch(self):
        s = self.lead()
        self.spawn(s, TANK, "tank", "api")
        again = self.subagent_start(s, TANK, "tank")  # the tech lead sent it a follow-up: a new turn starts
        self.assertEqual(again, {})
        self.assertEqual(sum(1 for v in self.session_doc(s)["launches"].values() if v.get("agent_id") == TANK), 1)


class CodexLaunchLimits(CodexCrew):
    """A Codex launch over the crew's limits cannot be refused before it starts: devkit records it, marks its lane,
    gives its step back to the tech lead, and tells the member to stop and report."""

    def test_a_launch_over_the_slot_limit_is_marked_and_the_member_told_to_stop(self):
        s = self.lead(max_parallel=1)
        self.spawn(s, TANK, "tank", "api")
        told = self.context(self.spawn(s, TRINITY, "trinity", "ui"))
        self.assertIn("1 of 1 crew slots are in use (Backend engineer (Tank))", told)
        self.assertIn("stop now and report", told)
        lane = s.run_doc()["agents"][TRINITY]
        self.assertIn("1 of 1 crew slots are in use", lane["over_limit"]["why"])
        self.assertEqual((self.step(s, "ui")["owner"], self.step(s, "ui")["state"]), ("main", "pending"))
        notice = [m["text"] for m in s.messages() if m.get("to") == "main" and "over the crew's limit" in m["text"]]
        self.assertEqual(len(notice), 1, s.messages())
        self.assertIn("Frontend engineer (Trinity)", notice[0])
        self.assertEqual([r["target"] for r in self.audit("crew.over-limit")], ["Trinity"])

    def test_the_notice_of_a_launch_out_of_order_says_why_in_whole_sentences(self):
        s = self.lead(max_parallel=1)
        s.prompt("Carry on with the plan")  # a later turn: the plan's steps are no longer this turn's request
        self.spawn(s, TRINITY, "trinity", "ui")  # “api” comes first in the plan, and one slot is all there is
        notice = [m["text"] for m in s.messages() if m.get("to") == "main" and "over the crew's limit" in m["text"]]
        self.assertEqual(len(notice), 1, s.messages())
        self.assertIn("which devkit cannot refuse for a Codex launch before its agent starts: “api” comes first",
                      notice[0])
        self.assertIn('--why "…". It was told to stop and report', notice[0])

    def test_an_over_limit_member_may_read_but_not_edit(self):
        s = self.lead(max_parallel=1)
        self.spawn(s, TANK, "tank", "api")
        self.spawn(s, TRINITY, "trinity", "ui")
        patch = "*** Begin Patch\n*** Add File: ui/view.ts\n+export {};\n*** End Patch\n"
        g = s.hook("guard", s.payload("PreToolUse", tool_name="apply_patch", tool_use_id="exec-patch-1",
                                      tool_input={"command": patch}, agent_id=TRINITY, agent_type="trinity"))
        spec = g.get("hookSpecificOutput") or {}
        self.assertEqual(spec.get("permissionDecision"), "deny", g)
        self.assertIn("over the crew's limit", spec.get("permissionDecisionReason", ""))
        self.assertEqual(s.sh("git status --short", member=TRINITY, member_type="trinity")["denied"], "")

    def test_a_member_within_the_limit_edits_its_steps_scope_and_nothing_else(self):
        s = self.lead(max_parallel=1)
        self.spawn(s, TANK, "tank", "api")
        verdicts = {}
        for path in ("api/view.ts", "ui/view.ts"):
            patch = f"*** Begin Patch\n*** Add File: {path}\n+export {{}};\n*** End Patch\n"
            g = s.hook("guard", s.payload("PreToolUse", tool_name="apply_patch", tool_use_id=f"exec-{path}",
                                          tool_input={"command": patch}, agent_id=TANK, agent_type="tank"))
            spec = g.get("hookSpecificOutput") or {}
            verdicts[path] = (spec.get("permissionDecision"), spec.get("permissionDecisionReason") or "")
        self.assertNotEqual(verdicts["api/view.ts"][0], "deny", verdicts)
        self.assertEqual(verdicts["ui/view.ts"][0], "deny", verdicts)
        self.assertIn("ui/view.ts is outside Backend engineer (Tank)'s scope (api/**)", verdicts["ui/view.ts"][1])

    def test_a_nested_spawn_below_the_depth_limit_is_marked(self):
        s = self.lead(max_depth=1)
        self.spawn(s, TANK, "tank", "api")
        told = self.context(self.spawn(s, HELPER, "sparks", "find_callers", parent=TANK, depth=2))
        self.assertIn("2 levels below the tech lead; the limit is 1", told)
        lane = s.run_doc()["agents"][HELPER]
        self.assertEqual((lane["parent"], lane["depth"]), (TANK, 2))
        self.assertIn("progress.max_depth", lane["over_limit"]["why"])


class CodexMemberContext(CodexCrew):
    """A Codex brief is encrypted, so the member's required skills, known pitfalls and report format reach it as
    SubagentStart context: the text a Claude Code member gets appended to its brief."""

    def setUp(self):
        super().setUp()
        for name in ("backend", "testing", "devsecops"):  # installed for Codex on this Mac
            f = self.home / ".agents" / "skills" / name / "SKILL.md"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n")

    def test_the_members_skills_and_report_format_arrive_as_subagent_start_context(self):
        s = self.lead()
        out = self.spawn(s, TANK, "tank", "api")
        self.assertEqual(set(out) - SUBAGENT_START_KEYS, set(), out)  # Codex refuses any other key
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SubagentStart")
        told = self.context(out)
        self.assertIn("## devkit · required skills", told)
        self.assertIn("by reading its SKILL.md", told)
        for name in ("backend", "testing", "devsecops"):
            self.assertIn(f"- `{name}` — ", told)
        self.assertIn("## devkit · your report", told)
        self.assertIn("**[Firefly> DevKit · Backend engineer (Tank)]**", told)

    def test_a_helper_no_crew_member_runs_gets_only_the_report_format(self):
        s = self.lead()
        out = self.spawn(s, HELPER, "worker", "list_files")
        told = self.context(out)
        self.assertNotIn("required skills", told)
        self.assertIn("## devkit · your report", told)


class ClaudeLaunchUnchanged(CodexCrew):
    """A Claude Code launch keeps its PreToolUse path: the record, the brief's skills, nothing at SubagentStart. Only
    a Codex start carries the member's context."""

    def test_a_claude_launch_is_recorded_at_its_agent_call_and_only_a_codex_start_prints_context(self):
        x = self.lead()
        self.assertIn("## devkit · your report", self.context(self.spawn(x, TANK, "tank", "api")))
        c = self.claude(CLAUDE_LEAD, self.api)
        c.ok("progress", "start", "Invoice export", "--plan", "api=30m [backend]")
        c.ok("progress", "assign", "api", "--to", "Tank", "--scope", "api/**")
        call = dict(tool_name="Agent", tool_use_id="toolu_claude_1",
                    tool_input={"description": "Tank: api", "subagent_type": "devkit:tank", "prompt": "Build the API."})
        g = c.hook("guard", c.payload("PreToolUse", **call))
        self.assertIn("## devkit · your report", g["hookSpecificOutput"]["updatedInput"]["prompt"])
        out = c.hook("subagent-start", c.payload("SubagentStart", agent_id="claude-tank-1", agent_type="devkit:tank"))
        self.assertEqual(out, {})
        c.hook("post-tool", c.payload("PostToolUse", tool_response={"agentId": "claude-tank-1"}, **call))
        self.assertEqual(self.launch_of(c, "claude-tank-1")["key"], "toolu_claude_1")
        self.assertEqual(self.step(c, "api")["owner"], "claude-tank-1")


if __name__ == "__main__":
    unittest.main()
