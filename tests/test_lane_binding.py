# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Exact lane binding (F2 of the agent and session lifecycle spec, contracts C2 and C3): a crew lane binds to its own
# launch by tool_use_id, through the launch's PostToolUse (tool_response.agentId) or the meta file Claude Code writes
# after SubagentStart, and never by a guess from a queue. Each test replays a case seen live on 2026-11-06 with
# Claude Code's real payload shapes (tests/fixtures/hooks/claude/, captured at F0) and that case's ids and
# descriptions. Run: python3 -m unittest tests.test_lane_binding
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import hooks, dispatch, progress as pg  # noqa: E402
from test_progress import TempState  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hooks" / "claude"
SETTINGS = Path(__file__).resolve().parents[1] / "config" / "claude" / "settings.json"


def fixture(name: str, **over) -> dict:
    d = json.loads((FIXTURES / f"{name}.json").read_text())
    d.update(over)
    return d


class LaneCase(unittest.TestCase):
    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        root = Path(self.state.tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        self.main = root / "projects" / "s1.jsonl"  # the main transcript; subagents' meta files sit next to it
        self.main.parent.mkdir()
        self.main.write_text("")
        for p in (mock.patch.object(hooks, "STATE", root),
                  mock.patch("devkit.guard.check", lambda *a, **k: None)):  # guard.py's own rules are not under test
            p.start()
            self.addCleanup(p.stop)
        self.t0 = time.time() - 600

    def m(self, minutes):
        return self.t0 + minutes * 60

    def common(self, **over):
        return dict(session_id="s1", transcript_path=str(self.main), cwd=str(self.repo), **over)

    # PreToolUse of the Agent call: what the guard records once it allows the launch
    def launch(self, tid, description, kind="general-purpose", parent=None, at=None):
        p = fixture("PreToolUse.Agent.in-subagent" if parent else "PreToolUse.Agent", **self.common(tool_use_id=tid))
        p["tool_input"] = dict(p["tool_input"], description=description, subagent_type=kind)
        if parent:
            p["agent_id"] = parent
        pg.note_agent_launch("claude", p, now=at or self.m(0))

    def start(self, aid, kind="general-purpose", at=None):
        pg.on_subagent("claude", fixture("SubagentStart", **self.common(agent_id=aid, agent_type=kind)), True,
                       now=at or self.m(1))

    # Claude Code writes <session>/subagents/agent-<id>.meta.json after the SubagentStart hooks return (F0, S1)
    def meta(self, aid, tid, description, kind="general-purpose"):
        sub = self.main.with_suffix("") / "subagents"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / f"agent-{aid}.meta.json").write_text(json.dumps({
            "agentType": kind, "description": description, "toolUseId": tid, "spawnDepth": 1,
            "requestShape": "background"}))

    def hook(self, name, payload):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
            hooks.run(name, "claude")
        return out.getvalue()

    # the launch's PostToolUse: a background launch's result names the agent it started
    def result(self, tid, aid, description="", kind="general-purpose"):
        p = fixture("PostToolUse.Agent.background", **self.common(tool_use_id=tid))
        p["tool_response"] = dict(p["tool_response"], agentId=aid, description=description)
        p["tool_input"] = dict(p["tool_input"], description=description, subagent_type=kind)
        return self.hook("post-tool", p)

    # the subagent's first tool call (PreToolUse, guard hook): the meta file exists by then
    def first_tool(self, aid, kind="general-purpose", tool="Bash", file_path=""):
        p = fixture("PreToolUse.Bash", **self.common(agent_id=aid, agent_type=kind))
        if tool != "Bash":
            p.update(tool_name=tool, tool_input={"file_path": str(self.repo / file_path), "old_string": "a",
                                                 "new_string": "b"})
        return self.hook("guard", p)

    def stop(self, aid, kind="general-purpose", at=None):
        p = fixture("SubagentStop", **self.common(agent_id=aid, agent_type=kind))
        p["agent_transcript_path"] = str(self.main.with_suffix("") / "subagents" / f"agent-{aid}.jsonl")
        pg.on_subagent("claude", p, False, now=at or self.m(30))

    def run_(self):
        return pg.load_run("claude", "s1")

    def lane(self, aid):
        return (self.run_().get("agents") or {}).get(aid)

    def step(self, name):
        return next(s for s in self.run_()["plan"] if s["name"] == name)

    def session(self):
        return json.loads(self.state.sess.read_text())

    def to_morpheus(self):
        return [m["text"] for m in self.run_().get("messages") or [] if m.get("to") == "main"]


class ExactBindingTests(LaneCase):
    def test_a_stale_launch_never_labels_another_members_agent(self):
        # live: Tank's lane took Kid's label, an entry Kid's start had already claimed and the G1 race put back
        pg.start("Work", plan=["curl installer", "dashboard server"], now=self.m(0))
        pg.assign("curl installer", "Kid", now=self.m(0))
        pg.assign("dashboard server", "Tank", now=self.m(0))
        self.launch("toolu_kid", "Kid: curl installer", at=self.m(0))  # its agent never started
        self.launch("toolu_tank", "Tank: dashboard server", at=self.m(5))
        self.start("a_tank", at=self.m(5))
        self.meta("a_tank", "toolu_tank", "Tank: dashboard server")
        self.first_tool("a_tank")
        self.assertEqual(self.lane("a_tank")["label"], "Tank: dashboard server")
        self.assertEqual(self.step("dashboard server")["owner"], "a_tank")
        self.assertEqual((self.step("curl installer")["owner"], self.step("curl installer")["state"]),
                         ("main", "pending"))
        self.assertEqual((self.session().get("launches") or {}).get("toolu_kid", {}).get("state"), "pending")  # still Kid's, untaken

    def test_two_launches_bind_to_their_own_agents_whatever_order_they_start_in(self):
        # live: The Architect's lane took Sati's label; both were launched in one message, in the background
        pg.start("Work", plan=["dashboard screens", "agent lifecycle design"], now=self.m(0))
        pg.assign("dashboard screens", "Sati", now=self.m(0))
        pg.assign("agent lifecycle design", "The Architect", now=self.m(0))
        self.launch("toolu_sati", "Sati: dashboard screens")
        self.launch("toolu_arch", "The Architect: agent lifecycle design")
        self.start("a_arch")  # the Architect starts first
        self.start("a_sati")
        self.result("toolu_arch", "a_arch", "The Architect: agent lifecycle design")
        self.result("toolu_sati", "a_sati", "Sati: dashboard screens")
        self.assertEqual(self.lane("a_arch")["label"], "The Architect: agent lifecycle design")
        self.assertEqual(self.lane("a_sati")["label"], "Sati: dashboard screens")
        self.assertEqual(self.step("agent lifecycle design")["owner"], "a_arch")
        self.assertEqual(self.step("dashboard screens")["owner"], "a_sati")

    def test_an_agent_whose_meta_file_comes_after_its_start_binds_at_its_first_tool_call(self):
        # live: The Keymaker's lane took The Oracle's label (foreground launches: no PostToolUse until they end)
        pg.start("Work", plan=["crew gap review", "lifecycle consult security"], now=self.m(0))
        pg.assign("crew gap review", "The Oracle", now=self.m(0))
        pg.assign("lifecycle consult security", "The Keymaker", now=self.m(0))
        self.launch("toolu_oracle", "The Oracle: crew gap review")
        self.launch("toolu_key", "The Keymaker: lifecycle consult security")
        self.hook("subagent-start", fixture("SubagentStart", **self.common(agent_id="a_key",
                                                                          agent_type="general-purpose")))
        self.meta("a_key", "toolu_key", "The Keymaker: lifecycle consult security")
        self.first_tool("a_key")
        self.hook("subagent-start", fixture("SubagentStart", **self.common(agent_id="a_oracle",
                                                                          agent_type="general-purpose")))
        self.meta("a_oracle", "toolu_oracle", "The Oracle: crew gap review")
        self.first_tool("a_oracle")
        self.assertEqual((self.lane("a_key")["persona"], self.lane("a_oracle")["persona"]),
                         ("The Keymaker", "The Oracle"))
        self.assertEqual(self.step("lifecycle consult security")["owner"], "a_key")
        self.assertEqual(self.step("crew gap review")["owner"], "a_oracle")

    def test_a_bound_launch_cannot_be_claimed_twice(self):
        pg.start("Work", plan=["parser"], now=self.m(0))
        pg.assign("parser", "Neo", scope="lib/parser/**", now=self.m(0))
        self.launch("toolu_neo", "Neo: parser")
        self.start("a_neo")
        self.result("toolu_neo", "a_neo", "Neo: parser")
        self.start("a_forged")  # a second agent whose meta file names the same launch: a forgery or a bug
        self.meta("a_forged", "toolu_neo", "Neo: parser")
        out = self.first_tool("a_forged", tool="Edit", file_path="lib/parser/core.py")
        self.assertEqual(self.lane("a_forged").get("binding"), "conflict")
        self.assertEqual(self.step("parser")["owner"], "a_neo")
        self.assertIn('"permissionDecision": "deny"', out)
        self.assertEqual((self.session().get("launches") or {}).get("toolu_neo", {}).get("agent_id"), "a_neo")


class ProvisionalLaneTests(LaneCase):
    def test_a_lane_is_provisional_until_bound_and_binds_before_the_guard_decides(self):
        pg.start("Feature", plan=["parser"], now=self.m(0))
        pg.assign("parser", "Neo", scope="lib/parser/**", now=self.m(0))
        self.launch("toolu_neo", "Neo: parser")
        self.start("a_neo")
        lane = self.lane("a_neo")
        self.assertEqual((lane.get("binding"), lane.get("scope") or []), ("pending", []))  # no scope widening
        self.assertEqual((self.step("parser")["owner"], self.step("parser")["state"]), ("main", "pending"))
        self.meta("a_neo", "toolu_neo", "Neo: parser")
        out = self.first_tool("a_neo", tool="Edit", file_path="lib/ui/app.ts")  # its first call, outside its files
        self.assertIn("outside Senior engineer (Neo)'s scope", out)
        self.assertEqual((self.lane("a_neo").get("binding"), self.step("parser")["owner"]), ("exact", "a_neo"))

    def test_a_failed_launch_is_removed_and_never_labels_the_next_agent(self):
        # F0, S6: a failed launch gives PostToolUseFailure with its tool_use_id, and no SubagentStart
        pg.start("Work", plan=["curl installer", "dashboard server"], now=self.m(0))
        pg.assign("curl installer", "Kid", now=self.m(0))
        pg.assign("dashboard server", "Tank", now=self.m(0))
        fail = fixture("PostToolUseFailure.Agent", **self.common())
        self.launch(fail["tool_use_id"], "Kid: curl installer")
        self.hook("post-tool-failure", fail)
        self.assertNotIn(fail["tool_use_id"], self.session().get("launches") or {})
        self.launch("toolu_tank", "Tank: dashboard server")
        self.start("a_tank")
        self.result("toolu_tank", "a_tank", "Tank: dashboard server")
        self.assertEqual(self.lane("a_tank")["label"], "Tank: dashboard server")
        self.assertEqual(self.step("curl installer")["owner"], "main")

    def test_an_agent_whose_launch_record_was_lost_gets_a_restricted_lane(self):
        # Seraph M69 (1): the launch record was dropped (state busy at the Agent call), so the meta file names a
        # launch devkit has no record of. Such an agent is not a helper: it reads, takes no step, no scope and no
        # slot, and the loss is audited, never "no lane, no limits"
        from devkit import audit
        pg.start("Feature", plan=["parser"], now=self.m(0))
        pg.assign("parser", "Neo", scope="lib/parser/**", now=self.m(0))
        self.start("a_neo")
        self.meta("a_neo", "toolu_lost", "Neo: parser")
        out = self.first_tool("a_neo", tool="Edit", file_path="lib/parser/core.py")
        self.assertIn('"permissionDecision": "deny"', out)
        lane = self.lane("a_neo")
        self.assertEqual((lane.get("binding"), lane.get("scope") or [], lane.get("persona")), ("unbound", [], "Neo"))
        self.assertEqual((self.step("parser")["owner"], self.step("parser")["state"]), ("main", "pending"))
        self.assertEqual(pg.lane_tools(self.run_(), "a_neo"), "read")
        self.assertNotIn("a_neo", [k for k, _ in pg.slots(self.run_())])
        self.assertEqual([r["target"] for r in audit.records(1, action="crew.unbound")], ["Neo"])

    def test_morpheus_assigning_a_step_to_an_unbound_lane_lifts_the_restriction(self):
        pg.start("Feature", plan=["parser"], now=self.m(0))
        pg.assign("parser", "Neo", scope="lib/parser/**", now=self.m(0))
        self.start("a_neo")
        self.meta("a_neo", "toolu_lost", "Neo: parser")
        self.first_tool("a_neo")
        pg.assign("parser", "Neo", now=self.m(2))  # Morpheus links the step to the running lane
        self.assertEqual((self.step("parser")["owner"], self.lane("a_neo")["binding"]), ("a_neo", "confirmed"))
        self.assertNotIn('"deny"', self.first_tool("a_neo", tool="Edit", file_path="lib/parser/core.py"))
        self.assertIn("outside Senior engineer (Neo)'s scope",
                      self.first_tool("a_neo", tool="Edit", file_path="lib/ui/app.ts"))

    def test_a_launch_records_its_tool_use_id_and_its_parent(self):
        self.launch("toolu_fg", "F0 fg probe")
        self.launch("toolu_nested", "F0 nested probe", parent="aaf06cf734666aa86")  # launched inside a subagent
        launches = self.session().get("launches") or {}
        self.assertEqual({k: (v["label"], v["parent"], v["state"]) for k, v in launches.items()},
                         {"toolu_fg": ("F0 fg probe", "main", "pending"),
                          "toolu_nested": ("F0 nested probe", "aaf06cf734666aa86", "pending")})


class StepChoiceTests(LaneCase):
    def test_a_launch_binds_to_the_step_its_description_names_not_the_members_first(self):
        # live, B-fxo6hnp1nbt: Neo's graph launch and The Keymaker's K1 launch each took their member's first step
        pg.start("Work", plan=["plan dependency engine", "session graph command", "lifecycle consult security",
                               "K1 protect devkit state and config"], now=self.m(0))
        for name, who in (("plan dependency engine", "Neo"), ("session graph command", "Neo"),
                          ("lifecycle consult security", "The Keymaker"),
                          ("K1 protect devkit state and config", "The Keymaker")):
            pg.assign(name, who, now=self.m(0), why="the live plan's assignment")
        self.launch("toolu_graph", "Neo: session graph command")
        self.launch("toolu_k1", "The Keymaker: K1 protected state")
        for aid, tid, d in (("a_graph", "toolu_graph", "Neo: session graph command"),
                            ("a_k1", "toolu_k1", "The Keymaker: K1 protected state")):
            self.start(aid)
            self.result(tid, aid, d)
        self.assertEqual(self.step("session graph command")["owner"], "a_graph")
        self.assertEqual(self.step("K1 protect devkit state and config")["owner"], "a_k1")
        self.assertEqual(self.step("plan dependency engine")["owner"], "main")
        self.assertEqual(self.step("lifecycle consult security")["owner"], "main")

    def test_a_launch_that_names_none_of_the_members_steps_takes_none_and_morpheus_is_told(self):
        pg.start("Work", plan=["F2 exact lane binding"], now=self.m(0))
        pg.assign("F2 exact lane binding", "Tank", now=self.m(0), why="the live plan's assignment")
        self.launch("toolu_consult", "Tank: backend consultation")
        self.start("a_consult")
        self.result("toolu_consult", "a_consult", "Tank: backend consultation")
        self.assertEqual((self.step("F2 exact lane binding")["owner"], self.lane("a_consult").get("binding")),
                         ("main", "exact"))
        self.assertTrue(any("F2 exact lane binding" in t and "dev progress assign" in t for t in self.to_morpheus()))


class ResumeTests(LaneCase):
    def test_a_resumed_agent_keeps_its_own_lane_and_takes_no_queued_launch(self):
        # live, B-fxnghfz0rjj: Tank's resumed lane took Trinity's label and step, and Morpheus's next message to
        # Tank went to it as "Trinity"
        pg.start("Work", plan=["dashboard server", "dashboard ui"], now=self.m(0))
        pg.assign("dashboard server", "Tank", now=self.m(0))
        pg.assign("dashboard ui", "Trinity", now=self.m(0))
        self.launch("toolu_tank", "Tank: dashboard server")
        self.start("a_tank")
        self.result("toolu_tank", "a_tank", "Tank: dashboard server")
        self.meta("a_tank", "toolu_tank", "Tank: dashboard server")
        self.stop("a_tank", at=self.m(20))
        self.stop("a_tank", at=self.m(20) + 1)  # the same stop twice: it never brings the lane back
        self.assertEqual(self.lane("a_tank")["state"], "done")
        pg.reopen("dashboard server", "the review found a bug", now=self.m(21))  # back to Morpheus, still Tank's
        self.launch("toolu_trinity", "Trinity: dashboard UI", at=self.m(21))
        self.start("a_tank", at=self.m(22))  # SendMessage resumes Tank: no launch of its own
        lane = self.lane("a_tank")
        self.assertEqual((lane["persona"], lane["label"], lane["state"]), ("Tank", "Tank: dashboard server",
                                                                           "running"))
        self.assertEqual(self.step("dashboard ui")["owner"], "main")
        # its own reopened step waits for it, and it holds no other: the resumed lane takes it (B-fxriigsni7c)
        self.assertEqual(self.step("dashboard server")["owner"], "a_tank")
        self.assertEqual((self.session().get("launches") or {}).get("toolu_trinity", {}).get("state"), "pending")
        self.start("a_trinity", at=self.m(22))
        self.result("toolu_trinity", "a_trinity", "Trinity: dashboard UI")
        self.assertEqual(self.step("dashboard ui")["owner"], "a_trinity")

    def test_a_resumed_lane_leaves_its_step_to_a_queued_launch_of_its_member(self):
        # the tech lead launches a new Tank for the reopened step while the old Tank lane is resumed for something
        # else: the step waits for the launch that names it, never for the resumed lane
        pg.start("Work", plan=["dashboard server"], now=self.m(0))
        pg.assign("dashboard server", "Tank", now=self.m(0))
        self.launch("toolu_tank", "Tank: dashboard server")
        self.start("a_tank")
        self.result("toolu_tank", "a_tank", "Tank: dashboard server")
        self.meta("a_tank", "toolu_tank", "Tank: dashboard server")
        self.stop("a_tank", at=self.m(20))
        pg.reopen("dashboard server", "the review found a bug", now=self.m(21))
        self.launch("toolu_tank2", "Tank: fix the dashboard server", at=self.m(21))
        self.start("a_tank", at=self.m(22))  # SendMessage resumes the old lane meanwhile
        self.assertEqual(self.lane("a_tank")["state"], "running")
        self.assertEqual(self.step("dashboard server")["owner"], "main")

    def test_a_lane_resumed_hours_after_it_started_is_not_taken_for_lost(self):
        pg.start("Work", plan=["dashboard server"], now=self.m(0))
        hours_ago = time.time() - 7 * 3600  # past stale_agent_hours (6) since the first start
        self.launch("toolu_tank", "Tank: dashboard server", at=hours_ago)
        self.start("a_tank", at=hours_ago)
        self.result("toolu_tank", "a_tank", "Tank: dashboard server")
        self.stop("a_tank", at=hours_ago + 600)
        self.start("a_tank", at=time.time())  # resumed now
        pg.on_turn_end("claude", "s1", {})  # the main turn's end sweeps the run for lost lanes
        self.assertEqual(self.lane("a_tank")["state"], "running")

    def test_a_message_to_a_member_reaches_their_working_lane_not_a_finished_one(self):
        # live: Morpheus's M36 "to Tank" went to Tank's finished dashboard lane
        pg.start("Work", plan=["dashboard server", "F2 exact lane binding"], now=self.m(0))
        pg.assign("dashboard server", "Tank", now=self.m(0))
        self.launch("toolu_old", "Tank: dashboard server")
        self.start("a_old")
        self.result("toolu_old", "a_old", "Tank: dashboard server")
        self.stop("a_old", at=self.m(10))
        pg.assign("F2 exact lane binding", "Tank", now=self.m(11), why="the live plan's assignment")
        self.launch("toolu_f2", "Tank: F2 exact lane binding", at=self.m(11))
        self.start("a_f2", at=self.m(11))
        self.result("toolu_f2", "a_f2", "Tank: F2 exact lane binding")
        m = pg.crew_ask("Also M34, please", to="Tank", now=self.m(12))
        self.assertEqual((m["to"], m["to_name"]), ("a_f2", "Tank"))


class SlotTests(LaneCase):
    def setUp(self):
        super().setUp()
        pg.start("Work", plan=["a", "b", "c", "d", "e"], now=self.m(0))
        self.spawn = fixture("PreToolUse.Agent", **self.common(tool_use_id="toolu_next"))

    def limit(self):
        return hooks._crew_limits("claude", self.spawn) or ("allow", "")

    def test_launches_take_a_slot_before_their_agents_start(self):
        for i, who in enumerate(("Neo", "Trinity", "Tank", "Dozer")):
            self.launch(f"toolu_{i}", f"{who}: part {i}")
        verdict = self.limit()
        self.assertEqual(verdict[0], "deny")
        self.assertIn("the limit is 4", verdict[1])
        self.assertEqual(dispatch.ready(self.run_())["free"], 0)

    def test_a_launch_whose_agent_never_starts_frees_its_slot_after_15_minutes(self):
        # Seraph M78 (1): a launch refused later by another hook, or whose agent never starts, holds a slot only
        # for LAUNCH_TTL
        now = time.time()
        for i, who in enumerate(("Neo", "Trinity", "Tank", "Dozer")):
            self.launch(f"toolu_{i}", f"{who}: part {i}", at=now - 14 * 60)
        self.assertIsNotNone(pg.launch_verdict("claude", self.spawn, self.run_(), now=now))
        self.assertIsNone(pg.launch_verdict("claude", self.spawn, self.run_(), now=now + 2 * 60))

    def test_a_helper_gets_no_lane_and_takes_no_slot(self):
        for i, who in enumerate(("Neo", "Trinity", "Tank")):
            self.launch(f"toolu_{i}", f"{who}: part {i}")
            self.start(f"a{i}")
            self.result(f"toolu_{i}", f"a{i}", f"{who}: part {i}")
        self.start("a_helper")  # compaction or a prompt suggestion: nobody launched it
        self.assertIsNone(self.lane("a_helper"))
        self.assertEqual(self.limit()[0], "allow")

    def test_a_resumed_lane_takes_a_slot_again(self):
        for i, who in enumerate(("Neo", "Trinity", "Tank", "Dozer")):
            self.launch(f"toolu_{i}", f"{who}: part {i}")
            self.start(f"a{i}")
            self.result(f"toolu_{i}", f"a{i}", f"{who}: part {i}")
        self.stop("a3")
        self.assertEqual(self.limit()[0], "allow")
        self.start("a3", at=self.m(31))  # SendMessage resumes Dozer
        self.assertEqual(self.limit()[0], "deny")


class SettingsTests(unittest.TestCase):
    def test_claude_settings_send_a_failed_agent_launch_to_devkit(self):
        entries = json.loads(SETTINGS.read_text())["hooks"].get("PostToolUseFailure") or []
        import re
        cmds = [h["command"] for e in entries if re.fullmatch(e.get("matcher") or ".*", "Agent")
                and re.fullmatch(e.get("matcher") or ".*", "Task") for h in e["hooks"]]
        self.assertTrue(any(c.endswith("hook post-tool-failure --agent claude") for c in cmds), cmds)
        self.assertIn("post-tool-failure", hooks.HANDLERS)

    def test_claude_settings_send_a_stopped_task_to_devkit(self):
        # Claude Code fires no SubagentStop for an agent TaskStop ends: devkit frees its lane at the stop call's result,
        # which reaches it only when the post-tool hook's matcher names TaskStop (a Release engineer lane stayed running
        # and held its slot for hours without it)
        import re
        entries = json.loads(SETTINGS.read_text())["hooks"].get("PostToolUse") or []
        cmds = [h["command"] for e in entries if re.fullmatch(e.get("matcher") or ".*", "TaskStop") for h in e["hooks"]]
        self.assertTrue(any(c.endswith("hook post-tool --agent claude") for c in cmds), cmds)


if __name__ == "__main__":
    unittest.main()
