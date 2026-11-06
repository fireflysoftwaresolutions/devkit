# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The crew's RACI: who does, approves, is consulted on and informed about each kind of work; routing from it; the
refusals at assignment and at the end of a run; and the team's changes in the handoff repository.
Run: python3 -m unittest"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import cli, core, crew, dispatch, progress as pg, raci  # noqa: E402
from test_progress import boundary, launch  # noqa: E402

# Real tasks and the member who should do them (the misroutes the developer found are the first rows).
GOLDEN = [
    ("build a local HTTP server and JSON API for the dashboard", "tank"),
    ("security review of a local web server", "keymaker"),
    ("design the screens for a background work view", "sati"),
    ("implement the dashboard frontend live updates", "trinity"),
    ("find bugs in the dashboard and check that it works as expected", "mouse"),
    ("gap analysis: what is missing in the dashboard for task details", "oracle"),
    ("a curl installer that works through a pipe", "kid"),
    ("fix the failing GitHub Actions workflow", "link"),
    ("add a migration for the invoices table", "dozer"),
    ("upgrade the app from Angular 15 to 19 and modernize the legacy modules", "trainman"),
    ("write the getting started guide", "ghost"),
    ("where is the guard's push limit defined", "sparks"),
    ("production is down after the deploy", "lock"),
    ("the orders endpoint is slow, p95 is 2 seconds", "merovingian"),
    ("check the licenses of our new dependencies", "persephone"),
    ("threat model the login flow", "keymaker"),
    ("try to bypass the guard with an adversarial attack", "smith"),
    ("review the diff before merging", "seraph"),
    ("which vector database should we use, compare the options", "roland"),
    ("prioritize the backlog for the next release", "hamann"),
    ("write acceptance criteria for the export feature", "oracle"),
    ("design the module boundaries and the API contract for billing", "architect"),
    ("add an iOS share extension to the mobile app", "switch"),
    ("add RAG over our docs for the support bot", "rama"),
    ("set up Terraform and Kubernetes for the staging environment", "niobe"),
    ("how many users finished onboarding last month, by cohort", "apoc"),
    ("fix the null pointer crash in checkout", "neo"),
    ("the tests are flaky and coverage is low", "mouse"),
    ("cut release 26.11.3 and publish it", "link"),
    ("plan the work and estimate it", "morpheus"),
    ("critique the dashboard's visual design and usability", "sati"),
    ("diseño de la pantalla de tareas", "sati"),
]


def commands():
    """{command: set of its subcommands or choices} from the real `dev` parser."""
    p = cli.build_parser()
    sub = next(a for a in p._actions if isinstance(a, argparse._SubParsersAction))
    out = {}
    for name, sp in sub.choices.items():
        kids = [a for a in sp._actions if isinstance(a, argparse._SubParsersAction)]
        pos = [a for a in sp._actions if not a.option_strings and a.choices and not isinstance(a, argparse._SubParsersAction)]
        out[name] = set(kids[0].choices) if kids else set(pos[0].choices) if pos else set()
    return out


# a finished member's report: its Check is the evidence its step ends done with (F6, R2-4)
DONE = ("**[Firefly> DevKit · Crew member]** api · `main` · step\n- **Done:** the step\n- **Check:** ✓ its tests pass\n"
        "- **Next:** Morpheus")


class RosterTests(unittest.TestCase):
    def test_the_shipped_roster_and_raci_are_complete(self):
        r = crew.load(team=False)
        self.assertEqual(r["warnings"], [])
        acts = raci.activities(r)
        self.assertGreaterEqual(len(acts), 20)
        for a in acts:
            with self.subTest(activity=a["id"]):
                self.assertIsInstance(a["A"], str)  # exactly one approver
                self.assertTrue(a["R"])
        for m in crew.members(r, include_main=False):
            with self.subTest(member=m["id"]):
                self.assertTrue(raci.roles_of(m["id"], r)["R"], "every member does at least one kind of work")
                self.assertGreaterEqual(len(m["procedure"]), 2)
                self.assertTrue(m["goal"] and m["done_when"])

    def test_every_devkit_command_a_member_names_exists(self):
        known = commands()
        for m in crew.members(crew.load(team=False)):
            for c in m["devkit"] + [c for x in m["procedure"] for c in x.get("devkit") or []] + crew.proofs(m):
                words = c.split()
                with self.subTest(member=m["id"], command=c):
                    self.assertEqual(words[0], "dev")
                    self.assertIn(words[1], known)
                    if len(words) > 2 and not words[2].startswith("-") and known[words[1]]:
                        self.assertIn(words[2], known[words[1]])

    def test_a_broken_raci_is_an_error(self):
        r = json.loads(crew.ROSTER.read_text())
        bad = dict(r, activities=[dict(r["activities"][0], A=["morpheus", "hamann"]),
                                  dict(r["activities"][1], R=["nobody"])] + r["activities"][2:])
        errors, _ = crew.check(bad)
        self.assertIn("`A` must be exactly one member", " ".join(errors))
        self.assertIn("R `nobody` is not a crew member", " ".join(errors))
        orphan = dict(r, activities=[a for a in r["activities"] if a["R"] != ["ghost"]])
        self.assertIn("`ghost` is Responsible for no activity", " ".join(crew.check(orphan)[0]))

    def test_a_team_change_that_adds_a_raci_hygiene_error_is_refused(self):
        # the rules of selfcheck.raci_errors (R2-9), on what a change adds: devkit's own roster's known findings
        # (D7) stay D7's, and the team cannot add more, such as the approver also consulted
        r = json.loads(crew.ROSTER.read_text())
        self.assertEqual(raci.check(r)[0], [])
        research = next(a for a in r["activities"] if a["id"] == "research")
        changed = dict(r, activities=[dict(a, C=a["C"] + [a["A"]]) if a is research else a for a in r["activities"]])
        errors = raci.check(changed)[0]
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("architect approves `research` and is consulted on it", errors[0])


class RoutingTests(unittest.TestCase):
    def test_real_tasks_go_to_the_member_the_raci_makes_responsible(self):
        r = crew.load(team=False)
        for text, want in GOLDEN:
            with self.subTest(task=text):
                top = raci.best(text, r)
                self.assertIsNotNone(top, "no clear kind of work")
                self.assertIn(want, top[0]["R"])

    def test_route_puts_the_responsible_member_first_with_its_approver(self):
        with mock.patch.object(dispatch, "_roster", lambda: crew.members(crew.load(team=False), include_main=False)):
            ranked = dispatch.route("build a local HTTP server and JSON API for the dashboard")
        self.assertEqual(ranked[0][0]["id"], "tank")
        self.assertIn("RACI R: Back-end services and APIs", ranked[0][2])
        self.assertIn("approves: Code reviewer (Seraph)", ranked[0][2])

    def test_an_unclear_task_is_not_routed_by_the_raci(self):
        self.assertIsNone(raci.best("look at this", crew.load(team=False)))


class AssignmentTests(unittest.TestCase):
    # two hours ago: devkit's sweep for lost subagents compares lanes with the real clock
    T0 = int(__import__("time").time()) // 60 * 60 - 7200

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        from devkit import audit
        self.saved = (pg.RUNS, pg.TIMING, pg.PENDING, pg.SESSIONS, pg._publish, audit.AUDIT,
                      os.environ.get("DEVKIT_SESSION"))
        pg.RUNS, pg.TIMING, pg.SESSIONS = root / "runs", root / "timing", root / "sessions"
        pg.PENDING = pg.TIMING / "pending"
        pg._publish = lambda: None
        audit.AUDIT = root / "audit"
        os.environ["DEVKIT_SESSION"] = "claude:s1"
        os.environ["DEVKIT_NO_NOTIFY"] = "1"
        for p in boundary(root):  # Morpheus's calls, devkit's default configuration
            p.start()
            self.addCleanup(p.stop)
        pg.SESSIONS.mkdir(parents=True)
        (pg.SESSIONS / "claude-s1.json").write_text(json.dumps({"agent": "claude", "session": "s1",
                                                                 "started": self.T0, "project": "acme/api"}))

    def tearDown(self):
        from devkit import audit
        pg.RUNS, pg.TIMING, pg.PENDING, pg.SESSIONS, pg._publish, audit.AUDIT, env = self.saved
        if env is None:
            os.environ.pop("DEVKIT_SESSION", None)
        else:
            os.environ["DEVKIT_SESSION"] = env
        self.tmp.cleanup()

    def m(self, minutes):
        return self.T0 + minutes * 60

    def test_a_member_who_is_not_responsible_is_refused_and_an_override_is_recorded(self):
        pg.start("Dashboard", plan=["build the JSON API server", "dashboard audit"], now=self.m(0))
        with self.assertRaises(pg.StepError) as e:  # the declared kind of work decides, never the step's words (R2-2)
            pg.assign("build the JSON API server", "Kid", activity="backend", now=self.m(1))
        self.assertIn("Backend engineer (Tank) does it and Code reviewer (Seraph) approves it", str(e.exception))
        run = pg.assign("build the JSON API server", "Tank", now=self.m(1))
        self.assertEqual(next(s for s in run["plan"] if s["name"].startswith("build"))["activity"], "backend")
        run = pg.assign("dashboard audit", "Sati", now=self.m(1))  # no clear kind of work: no refusal
        run = pg.assign("build the JSON API server", "Kid", activity="backend", why="Tank is out; Kid pairs with Seraph",
                        now=self.m(2))
        self.assertEqual(run["raci_overrides"][-1]["why"], "Tank is out; Kid pairs with Seraph")

    def test_a_starting_subagent_gets_its_own_launch_label_not_a_stale_one(self):
        pg.start("Work", plan=["installer", "dashboard server"], now=self.m(0))
        pg.assign("dashboard server", "Tank", now=self.m(0))
        launch = lambda tid, d, at: pg.note_agent_launch("claude", {  # noqa: E731
            "session_id": "s1", "tool_use_id": tid, "tool_input": {"description": d, "subagent_type": "general-purpose"}},
            now=at)
        launch("toolu_kid", "Kid: curl installer", self.m(0))  # its SubagentStart never came
        launch("toolu_tank", "Tank: dashboard server", self.m(31))
        launch("toolu_tank", "Tank: dashboard server", self.m(31) + 1)  # the same launch recorded twice: one entry
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "t1", "agent_type": "general-purpose"}, True,
                       now=self.m(31) + 2)
        pg.launch_result("claude", {"session_id": "s1", "tool_use_id": "toolu_tank", "tool_response": {"agentId": "t1"}},
                         now=self.m(31) + 2)
        run = pg.load_run("claude", "s1")
        self.assertEqual(run["agents"]["t1"]["label"], "Tank: dashboard server")
        self.assertEqual(run["plan"][1]["owner"], "t1")
        sess = json.loads((pg.SESSIONS / "claude-s1.json").read_text())
        self.assertEqual({k: (v["state"], v["agent_id"]) for k, v in sess["launches"].items()},
                         {"toolu_kid": ("pending", None), "toolu_tank": ("started", "t1")})  # the duplicate: not kept
        self.assertEqual([k for k, _ in pg.slots(run, sess, now=self.m(31) + 3)], ["t1"])  # the stale one holds none
        # two launches at once: each subagent claims the record its own meta file names
        launch("toolu_sati", "Sati: screens", self.m(40))
        launch("toolu_ghost", "Ghost: docs", self.m(40))
        with tempfile.TemporaryDirectory() as d:
            main = Path(d) / "s1.jsonl"
            main.write_text("")
            (Path(d) / "s1" / "subagents").mkdir(parents=True)
            (Path(d) / "s1" / "subagents" / "agent-g1.meta.json").write_text(json.dumps(
                {"toolUseId": "toolu_ghost", "description": "Ghost: docs"}))
            pg.on_subagent("claude", {"session_id": "s1", "agent_id": "g1", "agent_type": "general-purpose",
                                      "transcript_path": str(main)}, True, now=self.m(41))
        self.assertEqual(pg.load_run("claude", "s1")["agents"]["g1"]["label"], "Ghost: docs")

    def test_parallel_launches_each_find_their_own_record_from_the_main_transcript(self):
        """Claude Code's SubagentStart may carry only the main transcript: the launch record sits in
        <session>/subagents/agent-<id>.meta.json next to it, whatever order the subagents start in."""
        pg.start("Work", plan=["dashboard screens", "agent lifecycle design"], now=self.m(0))
        pg.assign("agent lifecycle design", "The Architect", now=self.m(0))
        for aid, d in (("x1", "Sati: dashboard screens"), ("y2", "The Architect: agent lifecycle")):
            pg.note_agent_launch("claude", {"session_id": "s1", "tool_use_id": f"toolu_{aid}",
                                            "tool_input": {"description": d, "subagent_type": "general-purpose"}})
        with tempfile.TemporaryDirectory() as d:
            main = Path(d) / "s1.jsonl"
            main.write_text("")
            sub = Path(d) / "s1" / "subagents"
            sub.mkdir(parents=True)
            for aid, desc in (("x1", "Sati: dashboard screens"), ("y2", "The Architect: agent lifecycle")):
                (sub / f"agent-{aid}.meta.json").write_text(json.dumps({"toolUseId": f"toolu_{aid}",
                                                                        "description": desc}))
            for aid in ("y2", "x1"):  # the Architect starts first
                pg.on_subagent("claude", {"session_id": "s1", "agent_id": aid, "agent_type": "general-purpose",
                                          "transcript_path": str(main)}, True, now=self.m(1))
        run = pg.load_run("claude", "s1")
        self.assertEqual(run["agents"]["y2"]["label"], "The Architect: agent lifecycle")
        self.assertEqual(run["agents"]["x1"]["label"], "Sati: dashboard screens")
        self.assertEqual(next(s for s in run["plan"] if s["name"] == "agent lifecycle design")["owner"], "y2")

    def test_a_step_waits_for_its_dependencies_even_when_its_member_is_already_working(self):
        pg.start("Work", plan=["lifecycle design", "journey design=1h after lifecycle design"], now=self.m(0))
        pg.assign("lifecycle design", "The Architect", now=self.m(0))
        launch("a1", "devkit:architect", "The Architect: lifecycle", self.m(1))
        run = pg.assign("journey design", "The Architect", now=self.m(2))
        journey = run["plan"][1]
        self.assertEqual((journey["state"], journey.get("owner") or "main"), ("pending", "main"))  # waits

    def test_a_step_reassigned_away_from_a_running_member_leaves_its_lane(self):
        pg.start("Work", plan=["installer", "dashboard server"], now=self.m(0))
        pg.assign("installer", "Kid", now=self.m(0))
        launch("a1", "devkit:kid", "Kid: installer", self.m(1))
        run = pg.assign("dashboard server", "Kid", now=self.m(2))  # a mistake: Kid's running lane takes it
        self.assertEqual(run["plan"][1]["owner"], "a1")
        run = pg.assign("dashboard server", "Tank", now=self.m(3))
        self.assertEqual((run["plan"][1]["owner"], run["plan"][1]["state"], run["plan"][1]["label"]),
                         ("main", "pending", "Tank"))
        self.assertIn("reassigned from Kid to Tank", [c.get("why") for c in run.get("history") or []][-1] or "")

    def test_the_run_ends_only_when_the_accountable_member_approved(self):
        # each member stops with its report, whose Check is its evidence (F6); the review follows the work it approves
        pg.start("API", plan=["build the JSON API server", "review after build the JSON API server"], now=self.m(0))
        pg.assign("build the JSON API server", "Tank", now=self.m(0))
        launch("a1", "devkit:tank", "Tank: build the JSON API server", self.m(1))
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a1", "agent_type": "devkit:tank",
                                  "last_assistant_message": DONE}, False, now=self.m(20))
        with self.assertRaises(pg.StepError) as e:
            pg.step("API shipped", kind="done", force=True, now=self.m(22))
        self.assertIn("Code reviewer (Seraph) approves “build the JSON API server”", str(e.exception))
        pg.assign("review", "Seraph", now=self.m(22))
        launch("a2", "devkit:seraph", "Seraph: review", self.m(23))
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a2", "agent_type": "devkit:seraph",
                                  "last_assistant_message": DONE}, False, now=self.m(30))
        run = pg.step("API shipped", kind="done", now=self.m(31))
        self.assertEqual(run["state"], "done")

    def test_ending_without_the_approval_needs_a_reason(self):
        pg.start("API", plan=["build the JSON API server"], now=self.m(0))
        pg.assign("build the JSON API server", "Tank", now=self.m(0))
        launch("a1", "devkit:tank", "Tank: build the JSON API server", self.m(1))
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a1", "agent_type": "devkit:tank",
                                  "last_assistant_message": DONE}, False, now=self.m(9))
        run = pg.step("API shipped", kind="done", why="a spike, thrown away", now=self.m(10))
        self.assertEqual(run["state"], "done")
        self.assertEqual(run["raci_overrides"][-1]["owed"], [["seraph", ["build the JSON API server"]]])


class MorpheusTests(unittest.TestCase):
    """Morpheus routes every development request, and the crew talks through devkit."""

    def test_a_request_with_several_kinds_of_work_names_each_member_and_approver(self):
        from devkit import hooks
        r = crew.load(team=False)
        text = hooks.morpheus_hint("build the JSON API server for the dashboard and implement the frontend live "
                                   "updates", r)
        self.assertIn("you are Morpheus", text)
        self.assertIn("back-end services and APIs → Backend engineer (Tank) does it, Code reviewer (Seraph) approves", text)
        self.assertIn("front-end implementation → Frontend engineer (Trinity) does it", text)
        self.assertIn("Plan one step per kind of work", text)
        self.assertEqual(hooks.morpheus_hint("please continue", r), "")
        meta = hooks.morpheus_hint("every member needs its own procedure: developing a backend, a frontend, testing "
                                   "and ci/cd differ, and so do designing an architecture and documenting", r)
        self.assertIn("mentions", meta)  # examples of many kinds of work: no single route
        self.assertNotIn("this request is CI/CD", meta)
        self.assertEqual(hooks.morpheus_hint("look at this one thing", r), "")

    def test_the_status_block_shows_the_crew_and_what_the_developer_must_decide(self):
        from devkit import canary
        run = {"goal": "Dashboard", "state": "running", "project": "acme/api", "cwd": "/",
               "plan": [{"name": "the API", "owner": "a1", "state": "running", "est": 30},
                        {"name": "the screens", "owner": "a2", "state": "done", "est": 20}],
               "agents": {"a1": {"persona": "Tank", "label": "Tank: the API", "state": "running", "started": 1},
                          "a2": {"persona": "Sati", "label": "Sati: the screens", "state": "done", "started": 1},
                          "h1": {"label": "compaction", "state": "done", "started": 1}},
               "decisions": [{"id": "D1", "question": "Read-only or with actions?", "state": "open", "mode": "guided"}],
               "updates": [{"kind": "step", "text": "contract written", "check": "✓ 4 tests", "next": "the API"}]}
        with mock.patch.object(pg, "pending_decisions", lambda r: r["decisions"]):
            block = pg.render_update(run, branch="main", now=100)
        self.assertIn("- **Crew:** Backend engineer (Tank) ▸ the API · UX designer (Sati) ✓ the screens", block)
        self.assertIn("- **You:** decide “Read-only or with actions?”", block)
        parsed = canary.parse_status(block)
        self.assertEqual(parsed["crew"], "Backend engineer (Tank) ▸ the API · UX designer (Sati) ✓ the screens")
        self.assertEqual(parsed["next"], "the API")  # Next still parses after the new bullets
        self.assertEqual(canary.format_issues(block), [])
        self.assertIn("you → decide", canary.status_text(block))

    def test_the_plan_check_waits_while_crew_members_are_working(self):
        run = {"agents": {"a1": {"persona": "Sati", "state": "running"}, "h1": {"label": "compaction", "state": "running"},
                          "a2": {"persona": "Tank", "state": "done"}}}
        self.assertEqual(pg.crew_working(run), ["Sati"])  # helpers and finished members do not count
        run["agents"]["a1"]["state"] = "done"
        self.assertEqual(pg.crew_working(run), [])

    def test_a_member_that_never_reported_through_devkit_owes_a_step(self):
        now = 10_000
        run = {"plan": [{"name": "the API", "owner": "a1", "state": "running"}],
               "agents": {"a1": {"persona": "Tank", "started": now - 600, "updates": []},
                          "a2": {"persona": "Sparks", "started": now - 600, "updates": []}}}
        self.assertTrue(pg.lane_unreported(run, "a1", now))
        run["agents"]["a1"]["updates"] = [{"kind": "step", "text": "contract written"}]
        self.assertFalse(pg.lane_unreported(run, "a1", now))
        run["plan"].append({"name": "where is X", "owner": "a2", "state": "running"})
        self.assertFalse(pg.lane_unreported(run, "a2", now))  # read-only: it reports in its message
        run["agents"]["a1"].update(updates=[], started=now - 60)
        self.assertFalse(pg.lane_unreported(run, "a1", now))  # a minute of work: nothing owed yet


class ProcedureTests(unittest.TestCase):
    """Each member follows its own procedure, governed by devkit: skills per phase, commands, gates and proofs."""

    def test_every_member_has_a_procedure_with_its_skills_and_the_engineers_prove_their_tests(self):
        r = crew.load(team=False)
        for m in crew.members(r):
            with self.subTest(member=m["id"]):
                used = {s for x in m["procedure"] for s in x.get("skills") or []}
                self.assertLessEqual(used, set(m["skills"]))  # every phase's skill is in the member's list
        for mid in ("neo", "trinity", "tank", "dozer", "kid"):
            self.assertIn("dev test", crew.proofs(crew.member(mid, r)))
        self.assertIn("dev docs check", crew.proofs(crew.member("ghost", r)))
        self.assertIn("dev sec scan", crew.proofs(crew.member("keymaker", r)))
        self.assertIn("superpowers:writing-plans", crew.member("morpheus", r)["skills"])

    def test_the_crew_has_no_gaps_between_members(self):
        """The crew works as one mechanism: whoever approves has an approval phase, whoever must consult has the
        channel to do it, and everyone with a shell reports through devkit."""
        r = crew.load(team=False)
        for m in crew.members(r):
            roles = raci.roles_of(m["id"], r)
            cmds = {c for x in m["procedure"] for c in x.get("devkit") or []}
            phases = " ".join(x["phase"].lower() for x in m["procedure"])
            with self.subTest(member=m["id"]):
                if any(m["id"] not in (a.get("R") or []) for a in roles["A"]):
                    self.assertRegex(phases, r"review|approve|verdict|integrate", "approves without an approval phase")
                if any(a.get("C") for a in roles["R"]):
                    self.assertIn("dev crew ask", cmds, "must consult others but has no channel")
                if m["tools"] != "read":
                    self.assertIn("dev progress step", cmds, "does not report through devkit")

    def test_claude_and_codex_get_the_same_procedure_and_raci(self):
        r = crew.load(team=False)
        roles = crew.codex_roles(r)
        for m in crew.members(r, include_main=False):
            with self.subTest(member=m["id"]):
                text = json.dumps(roles[m["id"]])
                for needle in ("## Your procedure", "## Your place in the crew's RACI", m["procedure"][0]["phase"]):
                    self.assertIn(needle, text)
                    self.assertIn(needle, crew.agent_files(r)[f"{m['id']}.md"])

    def test_a_brief_carries_the_knowledge_of_its_kind_of_work_with_the_skill_files(self):
        brief = crew.brief_text(crew.member("tank"), "write the missing tests for the orders API")
        self.assertIn("- `testing`: `", brief)  # the testing skill, with where its SKILL.md is
        self.assertIn("superpowers:test-driven-development", brief)
        self.assertIn("## Read first", brief)
        self.assertIn("docs/testing.md", brief)  # devkit's own testing guide
        self.assertIn("Skill", crew.TOOLS["build"])  # and the tool to load them
        self.assertIn("Skill", crew.TOOLS["review"])

    def test_the_brief_adds_stack_skills_from_the_skill_rules_only_when_the_task_needs_them(self):
        r = crew.load(team=False)
        tank = crew.member("tank", r)
        spring, why = crew.brief_skills(tank, "add a Spring Boot endpoint", ["src/main/java/**/*.java"])
        self.assertIn("java-springboot", spring)
        self.assertTrue(why)
        python, _ = crew.brief_skills(tank, "add a JSON endpoint to the Python server", ["lib/devkit/dashserver.py"])
        self.assertNotIn("java-springboot", python)
        self.assertNotIn("java-springboot", tank["skills"])


class EnforcementTests(unittest.TestCase):
    def test_every_enforced_rule_points_at_real_code_and_every_planned_one_is_named(self):
        rows = crew.enforcement()
        self.assertGreaterEqual(len(rows), 25)
        for rule, where, ref, state, ok in rows:
            with self.subTest(rule=rule):
                self.assertIn(state, ("enforced", "planned", "guidance"))
                self.assertTrue(ok, f"{ref} does not exist")
                self.assertTrue(where and ref)
        missing = crew.enforcement({"other step"})  # a planned rule whose step is not in the plan is flagged
        self.assertTrue(any(not ok for _, _, _, state, ok in missing if state == "planned"))


class TeamCrewTests(unittest.TestCase):
    """The team's changes live in the handoff repository: versioned, with who and why, and merged everywhere."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.handoff = Path(self.tmp.name) / "handoff"
        subprocess.run(["git", "init", "-q", str(self.handoff)], check=True)
        from devkit import audit
        self.p = [mock.patch.object(core, "HANDOFF", self.handoff),
                  mock.patch.object(audit, "AUDIT", Path(self.tmp.name) / "audit"),
                  mock.patch.dict(os.environ, {"GIT_AUTHOR_NAME": "ana", "GIT_AUTHOR_EMAIL": "ana@example.com",
                                               "GIT_COMMITTER_NAME": "ana", "GIT_COMMITTER_EMAIL": "ana@example.com",
                                               "DEVKIT_NO_AUTOSYNC": "1"})]
        for x in self.p:
            x.start()
        crew._CACHE.clear()

    def tearDown(self):
        for x in self.p:
            x.stop()
        crew._CACHE.clear()
        self.tmp.cleanup()

    def test_a_raci_change_is_recorded_committed_and_used(self):
        act = crew.set_raci("frontend", {"R": ["trinity", "neo"], "C": ["sati"]}, "Neo pairs on the dashboard UI",
                            by="ana@mbp")
        self.assertEqual(act["R"], ["trinity", "neo"])
        self.assertIsNone(raci.check_assignment("neo", "implement the dashboard frontend live updates"))
        ov = json.loads((self.handoff / "crew" / "crew.json").read_text())
        self.assertEqual(ov["changelog"][-1]["why"], "Neo pairs on the dashboard UI")
        self.assertIn("| Front-end implementation | Frontend engineer (Trinity) and Senior engineer (Neo) | Code reviewer (Seraph) | UX designer (Sati) |",
                      (self.handoff / "crew" / "README.md").read_text())
        log = subprocess.run(["git", "-C", str(self.handoff), "log", "--format=%s"], capture_output=True,
                             text=True).stdout
        self.assertIn("crew: RACI frontend", log)

    def test_a_member_change_reaches_the_brief_and_a_change_without_a_reason_is_refused(self):
        crew.set_member("tank", "procedure", json.dumps([{"phase": "OpenAPI first", "do": "Write the OpenAPI file first.",
                                                         "devkit": ["dev test"], "proof": "dev test"}]),
                        "the team's API standard", by="ana@mbp")
        self.assertEqual(crew.member("tank")["procedure"][0]["phase"], "OpenAPI first")
        self.assertIn("**OpenAPI first.** Write the OpenAPI file first.", crew.body(crew.member("tank"), crew.load()))
        with self.assertRaises(ValueError):  # a phase needs what to do
            crew.set_member("tank", "procedure", json.dumps([{"phase": "x"}]), "broken")
        with self.assertRaises(ValueError):
            crew.set_member("tank", "goal", "Anything", "")
        with self.assertRaises(ValueError) as e:  # validated before it is written
            crew.set_raci("backend", {"A": ["nobody"]}, "typo")
        self.assertIn("would break the crew", str(e.exception))

    def test_the_generated_plugin_agents_ignore_the_team_changes(self):
        crew.set_member("tank", "goal", "A different goal.", "team wording", by="ana@mbp")
        self.assertNotIn("A different goal.", crew.agent_files()["tank.md"])
        self.assertIn("A different goal.", crew.body(crew.member("tank"), crew.load()))

    def test_the_guard_refuses_direct_edits(self):
        from devkit import guard
        why = guard.handoff_violation("crew/crew.json", str(self.handoff))
        self.assertIn("dev crew set", why)


if __name__ == "__main__":
    unittest.main()
