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
from test_progress import TempState, launch  # noqa: E402

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


class BusyLaneQueueTests(unittest.TestCase):
    # M176, M177 and M180 of release 26.11.16's run: the tech lead assigned case-revert and guard-timeouts to the
    # Senior engineer while his lane worked on launcher-depth; devkit said they wait for a launch of their own, and
    # `dev crew next` said to send them to him (SendMessage). He did the work, but his lane kept launcher-depth's
    # scope, so the guard refused his edits to the new steps' files, and his `dev progress step --step case-revert`
    # stayed in his lane with no error, so both steps stayed pending. A step assigned to a member who works in one
    # live lane joins that lane once its current step closes, takes over the lane's scope then, and closes on the
    # member's own `dev progress step --step`; when devkit cannot bind or close, the member's command fails and says
    # what to do, and dispatch gives the advice progress follows
    REPORT = ("**[Firefly> DevKit · Senior engineer (Neo)]** devkit · `release/26.11.16` · {task}\n- **Done:** {task}\n"
              "- **Check:** ✓ 46 test files pass\n- **Next:** the Code reviewer reviews it")

    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.t0 = time.time() - 3600
        pg.start("Hotfix", plan=["launcher-depth=30m", "case-revert=15m", "guard-timeouts=45m"], now=self.m(0))
        pg.assign("launcher-depth", "Neo", scope="lib/devkit/guard.py,tests/test_guard_launcher_depth.py",
                  why="test", now=self.m(0))
        launch("a_neo", "devkit:neo", "Neo: launcher-depth guard timing fix", at=self.m(1))

    def m(self, minutes):
        return self.t0 + minutes * 60

    def run_(self):
        return pg.load_run("claude", "s1")

    def held(self, name):
        st = next(s for s in self.run_()["plan"] if s["name"] == name)
        return st.get("owner"), st.get("state")

    def assign(self, name, scope, at):
        pg.assign(name, "Neo", scope=scope, why="test", now=at)

    def close(self, name, at, check="✓ 46 guard test files pass"):
        return pg.step(f"{name} committed", which=name, check=check, from_agent="a_neo", now=at)

    def test_a_step_assigned_to_a_busy_member_joins_its_lane_once_its_step_closes(self):
        self.assign("case-revert", "lib/devkit/guard.py,tests/test_gate_tiers.py", self.m(5))
        self.assertEqual(self.held("case-revert"), ("main", "pending"))
        self.close("launcher-depth", self.m(10))
        self.assertEqual(self.held("case-revert"), ("a_neo", "running"))
        self.assertEqual(self.run_()["agents"]["a_neo"]["scope"], ["lib/devkit/guard.py", "tests/test_gate_tiers.py"])

    def test_the_members_close_of_a_step_that_joined_its_lane_closes_it(self):
        self.assign("case-revert", "tests/test_gate_tiers.py", self.m(5))
        self.close("launcher-depth", self.m(10))
        run = self.close("case-revert", self.m(20))
        self.assertEqual(self.held("case-revert"), ("a_neo", "done"))
        self.assertEqual((run["lane_outcome"]["step"], run["lane_outcome"]["closed"]), ("case-revert", True))

    def test_queued_steps_join_one_at_a_time_in_the_order_they_were_assigned(self):
        self.assign("guard-timeouts", "tests/test_guard_launcher_depth.py", self.m(5))
        self.assign("case-revert", "tests/test_gate_tiers.py", self.m(6))
        self.close("launcher-depth", self.m(10))
        self.assertEqual((self.held("guard-timeouts"), self.held("case-revert")),
                         (("a_neo", "running"), ("main", "pending")))
        with self.assertRaises(pg.StepError) as e:
            self.close("case-revert", self.m(15))
        self.assertIn("“guard-timeouts”", str(e.exception))  # the step to close first
        self.close("guard-timeouts", self.m(20))
        self.assertEqual(self.held("case-revert"), ("a_neo", "running"))

    def test_a_members_close_of_a_step_waiting_for_its_lane_fails_and_records_nothing(self):
        self.assign("case-revert", "tests/test_gate_tiers.py", self.m(5))
        updates = list(self.run_()["agents"]["a_neo"].get("updates") or [])
        with self.assertRaises(pg.StepError) as e:
            self.close("case-revert", self.m(8))
        self.assertIn('--step "launcher-depth"', str(e.exception))
        self.assertEqual(self.run_()["agents"]["a_neo"].get("updates") or [], updates)
        self.assertEqual(self.held("case-revert"), ("main", "pending"))

    def test_the_members_command_for_a_step_its_lane_cannot_close_exits_2_and_says_what_to_do(self):
        # M176-M177: the command printed ✓ and exited 0, so the member reported case-revert closed
        from test_progress import member_call, progress_cli_out
        self.assign("case-revert", "tests/test_gate_tiers.py", self.m(5))
        with member_call("a_neo"):  # the member's own call is open, as when its command runs
            rc, out, err = progress_cli_out("step", "case-revert committed", "--step", "case-revert", "--check",
                                            "✓ 67 tests pass")
        self.assertEqual(rc, 2, out)
        self.assertNotIn("✓", out)
        self.assertIn('close “launcher-depth” first (dev progress step "…" --step "launcher-depth" --check "…")', err)
        self.assertEqual(self.held("case-revert"), ("main", "pending"))

    def test_a_members_close_of_a_step_that_is_not_its_own_fails_with_what_to_do(self):
        with self.assertRaises(pg.StepError) as e:
            self.close("case-revert", self.m(8))  # nobody assigned it to the Senior engineer: the tech lead's own
        self.assertIn("“case-revert” is not your step (the tech lead's)", str(e.exception))
        self.assertIn("dev crew ask", str(e.exception))
        self.assertEqual(self.held("case-revert"), ("main", "running"))

    def test_dispatch_gives_the_advice_progress_follows(self):
        self.assign("case-revert", "tests/test_gate_tiers.py", self.m(5))
        _, waiting = dispatch._sorted(self.run_())
        (why,) = [w for s, w in waiting if s["name"] == "case-revert"]
        self.assertIn("once “launcher-depth” closes", why)
        self.assertNotIn("launch of its own", why)
        told = [m["text"] for m in self.run_().get("messages") or [] if m.get("to") == "main"]
        self.assertTrue(any(why in t for t in told), (why, told))

    def test_a_queued_step_waits_for_a_launch_of_its_own_once_the_lane_ends(self):
        # the queue is a promise of the lane while it works: its report closes launcher-depth and ends the lane, and
        # case-revert, which no evidence of the lane names, is offered for a launch of its own
        self.assign("case-revert", "tests/test_gate_tiers.py", self.m(5))
        self.assertEqual((self.run_()["plan"][1].get("waits_lane") or {}).get("lane"), "a_neo")
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a_neo", "agent_type": "devkit:neo",
                                  "last_assistant_message": self.REPORT.format(task="launcher-depth")}, False,
                       now=self.m(30))
        self.assertEqual((self.held("launcher-depth"), self.held("case-revert")),
                         (("a_neo", "done"), ("main", "pending")))
        self.assertNotIn("waits_lane", self.run_()["plan"][1])
        self.assertIn("case-revert", [s["name"] for s in dispatch.ready(self.run_())["launch"]])

    def test_assigning_a_closed_step_again_never_reopens_it_unrecorded(self):
        # B-fyj09eek926: a wider scope for the resumed member's closed step reopened it, with no line in its history
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a_neo", "agent_type": "devkit:neo",
                                  "last_assistant_message": self.REPORT.format(task="launcher-depth")}, False,
                       now=self.m(30))
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a_neo", "agent_type": "devkit:neo"}, True,
                       now=self.m(31))
        self.assign("launcher-depth", "lib/devkit/guard.py,tests/test_gate_tiers.py", self.m(32))
        self.assertEqual(self.held("launcher-depth"), ("a_neo", "done"))
        told = [m["text"] for m in self.run_().get("messages") or [] if m.get("to") == "main"]
        self.assertTrue(any('dev progress reopen "launcher-depth"' in t for t in told), told)
        pg.reopen("launcher-depth", "the guard timing fix needs the gate tier tests too", now=self.m(33))
        self.assertEqual(self.held("launcher-depth"), ("a_neo", "running"))
        self.assertEqual(self.run_()["agents"]["a_neo"]["scope"], ["lib/devkit/guard.py", "tests/test_gate_tiers.py"])

    def test_a_step_reopened_while_its_member_still_works_goes_back_to_their_lane(self):
        # B-fxriigsni7c: the reopened step stayed pending with the tech lead while its member worked on
        self.close("launcher-depth", self.m(10))
        pg.reopen("launcher-depth", "the review found a slow launcher shape", now=self.m(12))
        self.assertEqual(self.held("launcher-depth"), ("a_neo", "running"))


class TwoLaneQueueTests(unittest.TestCase):
    # Weave: the tech lead assigned a step to a member who worked in two lanes; devkit said it waits for a launch of
    # its own, and it stayed the tech lead's after one of the lanes freed and did the work, so the member's
    # `dev progress step --step` was refused and the step bound only to a fresh launch that named it
    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.t0 = time.time() - 3600
        pg.start("Hotfix", plan=["parser=30m", "graph=30m", "docs=15m"], now=self.m(0))
        pg.assign("parser", "Neo", why="test", now=self.m(0))
        launch("a_neo1", "devkit:neo", "Neo: parser fix", at=self.m(1))
        pg.assign("graph", "Neo", why="test", now=self.m(1))
        launch("a_neo2", "devkit:neo", "Neo: graph command", at=self.m(2))

    def m(self, minutes):
        return self.t0 + minutes * 60

    def held(self, name):
        st = next(s for s in pg.load_run("claude", "s1")["plan"] if s["name"] == name)
        return st.get("owner"), st.get("state")

    def test_a_step_for_a_member_busy_in_two_lanes_joins_the_first_lane_that_frees(self):
        pg.assign("docs", "Neo", why="test", now=self.m(5))
        self.assertEqual(self.held("docs"), ("main", "pending"))
        pg.step("graph committed", which="graph", check="✓ 12 tests pass", from_agent="a_neo2", now=self.m(10))
        self.assertEqual(self.held("docs"), ("a_neo2", "running"))
        pg.step("docs written", which="docs", check="✓ dev docs check clean", from_agent="a_neo2", now=self.m(20))
        self.assertEqual(self.held("docs"), ("a_neo2", "done"))

    def test_the_advice_for_two_lanes_says_how_to_give_the_step_to_one_of_them(self):
        pg.assign("docs", "Neo", why="test", now=self.m(5))
        told = [m["text"] for m in pg.load_run("claude", "s1").get("messages") or [] if m.get("to") == "main"]
        advice = next((t for t in told if "“docs”" in t), "")
        self.assertIn('dev progress assign "docs" --to "Neo: parser fix"', advice)
        self.assertIn('--to "Neo: graph command"', advice)
        self.assertNotIn("launch of its own", advice)


class ReviewLaneTests(unittest.TestCase):
    # Weave, five review steps in one run: a review step closes only with its reviewer's report (once the crew gates
    # pass it), so a reviewer's lane never freed for the next review assigned to it, and one launch that named two
    # reviews bound neither. The steps stayed the tech lead's, the reviewer's `dev progress step --step` was refused,
    # and its report closed none of them
    REPORT = ("**[Firefly> DevKit · Code reviewer (Seraph)]** devkit · `hotfix` · review-api and review-ui\n"
              "- **Done:** reviewed review-api and review-ui\n- **Check:** ✓ approve: 21 tests read and run\n"
              "- **Next:** the tech lead merges")

    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.t0 = time.time() - 3600
        pg.start("Hotfix", plan=["api=30m", "ui=30m", "review-api=20m after api", "review-ui=20m after ui"],
                 now=self.m(0))
        pg.assign("api", "Tank", why="test", now=self.m(0))
        launch("a_tank", "devkit:tank", "Tank: api", at=self.m(1))
        pg.assign("ui", "Trinity", why="test", now=self.m(0))
        launch("a_tri", "devkit:trinity", "Trinity: ui", at=self.m(1))
        pg.step("api committed", which="api", check="✓ 12 tests pass", from_agent="a_tank", now=self.m(10))
        pg.step("ui committed", which="ui", check="✓ 9 tests pass", from_agent="a_tri", now=self.m(11))

    def m(self, minutes):
        return self.t0 + minutes * 60

    def held(self, name):
        st = next(s for s in pg.load_run("claude", "s1")["plan"] if s["name"] == name)
        return st.get("owner"), st.get("state")

    def stop(self, at):
        from test_progress import gates_passed
        gates_passed("a_ser", at=at - 1)
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a_ser", "agent_type": "devkit:seraph",
                                  "last_assistant_message": self.REPORT}, False, now=at)

    def test_a_review_assigned_to_a_reviewer_holding_a_review_joins_its_lane_and_closes_with_its_report(self):
        pg.assign("review-api", "Seraph", why="test", activity="review", now=self.m(12))
        launch("a_ser", "devkit:seraph", "Seraph: review-api", at=self.m(13))
        pg.assign("review-ui", "Seraph", why="test", activity="review", now=self.m(14))
        self.assertEqual(self.held("review-ui"), ("a_ser", "running"))
        pg.step("review-ui approved", which="review-ui", check="✓ approve", from_agent="a_ser", now=self.m(20))
        self.stop(self.m(25))
        self.assertEqual((self.held("review-api"), self.held("review-ui")), (("a_ser", "done"), ("a_ser", "done")))

    def test_one_launch_that_names_two_reviews_binds_both(self):
        pg.assign("review-api", "Seraph", why="test", activity="review", now=self.m(12))
        pg.assign("review-ui", "Seraph", why="test", activity="review", now=self.m(12))
        launch("a_ser", "devkit:seraph", "Seraph: review-api and review-ui", at=self.m(13))
        self.assertEqual((self.held("review-api"), self.held("review-ui")),
                         (("a_ser", "running"), ("a_ser", "running")))
        self.stop(self.m(25))
        self.assertEqual((self.held("review-api"), self.held("review-ui")), (("a_ser", "done"), ("a_ser", "done")))


class BackgroundCloseTests(unittest.TestCase):
    # kit-consulting: members closed review-suite-fixes and client-editor-build with ✓ while their own dev test prove
    # and test script still ran in the background, so the steps after them started on work nobody had verified
    PROVE = "dev test prove --runs 1 tests/test_api.py"

    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.t0 = time.time() - 3600
        pg.start("Hotfix", plan=["api=30m"], now=self.t0)
        pg.assign("api", "Neo", why="test", now=self.t0)
        launch("a_neo", "devkit:neo", "Neo: api", at=self.t0 + 60)
        main = Path(self.state.tmp.name) / "session.jsonl"
        main.write_text("")
        pg.state.update(pg.session_path("claude", "s1"), lambda s: s.update(transcript=str(main)))
        self.member = main.with_suffix("") / "subagents" / "agent-a_neo.jsonl"  # where Claude Code keeps it
        self.member.parent.mkdir(parents=True)

    def write(self, *records):
        with self.member.open("a") as fh:
            fh.write("".join(json.dumps(r) + "\n" for r in records))

    def held(self):
        st = pg.load_run("claude", "s1")["plan"][0]
        return st.get("owner"), st.get("state")

    def close(self, at):
        return pg.step("api committed", which="api", check="✓ 12 tests pass", from_agent="a_neo", now=at)

    def test_a_members_close_waits_until_its_background_work_ended(self):
        self.write({"type": "assistant", "message": {"content": [
                       {"type": "tool_use", "id": "toolu_bg", "name": "Bash",
                        "input": {"command": self.PROVE, "run_in_background": True}}]}},
                   {"type": "user", "message": {"content": [
                       {"type": "tool_result", "tool_use_id": "toolu_bg",
                        "content": "Command running in background with ID: b1. Output is being written to: /tmp/b1"}]}})
        with self.assertRaises(pg.StepError) as e:
            self.close(self.t0 + 600)
        self.assertIn(self.PROVE, str(e.exception))
        self.assertEqual(self.held(), ("a_neo", "running"))
        self.write({"type": "user", "message": {"content": "<task-notification><task-id>b1</task-id><tool-use-id>"
                                                           "toolu_bg</tool-use-id><status>completed</status>"
                                                           "</task-notification>"}})
        self.close(self.t0 + 700)
        self.assertEqual(self.held(), ("a_neo", "done"))


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
