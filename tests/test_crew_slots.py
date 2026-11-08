# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Crew slots (spec P-fxtwig4hqvx, R1-R7): how many crew members work at once and why, what each launch costs, the slot
# kept for the developer's own request, the order of the ready steps, a stopped agent's slot, and what dev crew and
# the status block show. Every test runs on devkit's defaults in a temporary state (TempState), never on the
# developer's configuration, quota or handoff repository.
# Run: python3 -m unittest tests.test_crew_slots
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
from devkit import audit, core, crew, dispatch, progress as pg  # noqa: E402
from test_progress import TempState, launch  # noqa: E402


def config(**progress):
    # this test's progress section of devkit's configuration (TempState pins it to the defaults)
    return mock.patch.dict(core.CFG, {"progress": progress})


def usage(five=None, week=None, five_resets=3600, week_resets=86400):
    # what the status line records of the plan's usage windows (hooks.statusline), in the test's quota folder
    now = time.time()
    rec = {"ts": int(now)}
    if five is not None:
        rec["five_hour"] = {"used_percentage": five, "resets_at": int(now + five_resets)}
    if week is not None:
        rec["seven_day"] = {"used_percentage": week, "resets_at": int(now + week_resets)}
    pg.QUOTA.mkdir(parents=True, exist_ok=True)
    (pg.QUOTA / "claude.json").write_text(json.dumps(rec))


def call(label, kind, parent=None, tid="toolu_next"):
    # the PreToolUse event of one Agent launch, as launch_verdict sees it
    data = {"session_id": "s1", "tool_name": "Agent", "tool_use_id": tid,
            "tool_input": {"description": label, "subagent_type": kind, "prompt": "do it"}}
    if parent:
        data["agent_id"] = parent
    return data


def verdict(label, kind):
    return pg.launch_verdict("claude", call(label, kind), pg.load_run("claude", "s1"))


def turn_started(st, at):
    # the developer's latest prompt started the current turn at `at` (UserPromptSubmit records it)
    rec = json.loads(st.sess.read_text())
    rec["turn_started"] = at
    st.sess.write_text(json.dumps(rec))


class ConfigTests(unittest.TestCase):
    # R1: every progress key has its default in core.DEFAULTS, where dev config validate and the docs find it
    def test_core_defaults_hold_every_crew_slot_key(self):
        p = core.DEFAULTS["progress"]
        self.assertEqual((p["max_parallel"], p["quota_percent"], p["max_depth"], p["reserve"],
                          p["stale_agent_minutes"]), (4, 90, 2, 1, 90))
        self.assertEqual(p["quota_steps"], {"50": 1.0, "70": 0.75, "85": 0.5})
        self.assertEqual(p["slot_weights"], {"builder": 1.0, "review": 0.5, "read": 0.5, "scout": 0.0})

    # R1: dev config validate names a value out of its range and a key devkit does not know
    def test_validate_names_a_value_out_of_range_and_an_unknown_key(self):
        errors = core.config_errors({"progress": {"max_parallel": 11, "max_paralel": 3, "quota_steps": {"x": 2}}})
        self.assertIn("progress.max_parallel must be a whole number from 1 to 10 (got 11)", errors)
        self.assertIn("unknown key progress.max_paralel", errors)
        self.assertTrue(any(e.startswith("progress.quota_steps must be") for e in errors), errors)
        self.assertEqual(core.config_errors({"progress": {"max_parallel": 6, "slot_weights": {"review": 1}}}), [])

    # R1: a wrong value never reaches the crew: the key keeps its default
    def test_a_wrong_value_falls_back_to_its_default(self):
        with TempState(), config(max_parallel=0, reserve="two", slot_weights={"review": 0.25}):
            c = pg.cfg()
            self.assertEqual((c["max_parallel"], c["reserve"]), (4, 1))
            self.assertEqual(c["slot_weights"], {"builder": 1.0, "review": 0.25, "read": 0.5, "scout": 0.0})

    # R1: the team's policy (policy/progress.json in the handoff repository) sets a ceiling no machine goes above
    def test_the_team_policy_sets_a_ceiling(self):
        with TempState(), config(max_parallel=6):
            pg.TEAM_POLICY.parent.mkdir(parents=True)
            pg.TEAM_POLICY.write_text(json.dumps({"max_parallel": 3}))
            limit, why = pg.parallel_limit("claude")
            self.assertEqual(limit, 3)
            self.assertIn("the team's ceiling", why)


class ConfigSetTests(unittest.TestCase):
    # R1: `dev config set <key> <value>`, the developer's own command in a terminal
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "config.json"
        self.file.write_text(json.dumps({"editor": "vim"}))
        p = mock.patch.object(audit, "AUDIT", Path(tmp.name) / "audit")  # never the real audit trail
        p.start()
        self.addCleanup(p.stop)

    def test_the_developer_sets_a_key_validated_written_and_audited(self):
        self.assertEqual(core.config_set("progress.max_parallel", "6", path=self.file, developer=True), (4, 6))
        self.assertEqual(json.loads(self.file.read_text()), {"editor": "vim", "progress": {"max_parallel": 6}})
        rec = [r for r in audit.records(1, "dev.config.set")]
        self.assertEqual([(r["target"], r["detail"]) for r in rec], [("progress.max_parallel", "4 → 6")])

    def test_an_agents_attempt_is_refused_and_writes_nothing(self):
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}), self.assertRaises(core.ConfigError) as e:
            core.config_set("progress.max_parallel", "6", path=self.file)
        self.assertIn("only the developer", str(e.exception))
        self.assertEqual(json.loads(self.file.read_text()), {"editor": "vim"})

    def test_a_value_out_of_range_or_an_unknown_key_is_refused_before_anything_is_written(self):
        for key, value, says in (("progress.max_parallel", "11", "from 1 to 10"),
                                 ("progress.max_paralel", "3", "unknown key"), ("nosuch", "1", "unknown key")):
            with self.subTest(key=key), self.assertRaises(core.ConfigError) as e:
                core.config_set(key, value, path=self.file, developer=True)
            self.assertIn(says, str(e.exception))
        self.assertEqual(json.loads(self.file.read_text()), {"editor": "vim"})


class QuotaCurveTests(unittest.TestCase):
    # R2 (acceptance): at 87 % of the 5-hour window with max_parallel 6, three members work at once, and devkit says why
    def test_87_percent_of_the_5_hour_window_with_six_slots_gives_three(self):
        with TempState(), config(max_parallel=6):
            usage(five=87)
            self.assertEqual(pg.parallel_limit("claude"), (3, "5-hour usage at 87 %"))

    # R2: the curve replaces the cliff: full below 70 %, three quarters from 70 %, half from 85 %, one from 90 %
    def test_the_curve_steps_down_with_usage(self):
        with TempState():
            got = []
            for used in (40, 69, 70, 85, 90):
                usage(five=used)
                got.append(pg.parallel_limit("claude")[0])
            self.assertEqual(got, [4, 4, 3, 2, 1])

    # R2: the weekly window lowers the limit too, until it resets; an expired reading says nothing
    def test_the_weekly_window_counts_until_it_resets(self):
        with TempState():
            usage(five=10, week=72)
            self.assertEqual(pg.parallel_limit("claude"), (3, "weekly usage at 72 %"))
            usage(five=10, week=72, week_resets=-60)
            self.assertEqual(pg.parallel_limit("claude"), (4, ""))


class WeightTests(unittest.TestCase):
    # R3 (acceptance): two review-only members and a builder fit in two slots; one more builder does not
    def test_two_reviewers_and_a_builder_fit_in_two_slots(self):
        with TempState(), config(max_parallel=2, reserve=0):
            pg.start("Work", plan=["api", "review", "audit"])
            launch("a_tank", "devkit:tank", "Tank: api")
            launch("a_seraph", "devkit:seraph", "Seraph: review")
            self.assertIsNone(verdict("Agent Smith: audit", "devkit:smith"))
            launch("a_smith", "devkit:smith", "Agent Smith: audit")
            self.assertEqual(pg.slot_budget("claude", pg.load_run("claude", "s1"))["used"], 2.0)
            self.assertIn("2 of 2 crew slots are in use", verdict("Neo: docs", "devkit:neo"))

    # R3: a scout lookup costs no slot, so it runs while the crew is full
    def test_a_scout_lookup_runs_while_the_crew_is_full(self):
        with TempState(), config(max_parallel=1):
            pg.start("Work", plan=["api"])
            launch("a_tank", "devkit:tank", "Tank: api")
            self.assertIsNone(verdict("Sparks: where is parse_minutes", "devkit:scout"))
            self.assertIsNotNone(verdict("Neo: docs", "devkit:neo"))


class ReserveTests(unittest.TestCase):
    # R4: while the crew works, the last slot waits for the developer's own request: queued plan work from an
    # earlier turn does not take it; the launch for what the developer just asked does
    def test_the_last_slot_waits_for_the_developers_own_request(self):
        with TempState() as st, config(max_parallel=2):
            now = time.time()
            pg.start("Work", plan="api=30m; docs=30m", now=now - 3600)
            pg.assign("api", "Tank", why="test")
            pg.assign("docs", "Ghost", why="test")
            launch("a_tank", "devkit:tank", "Tank: api")
            turn_started(st, now - 60)
            r = dispatch.ready(pg.load_run("claude", "s1"))
            self.assertEqual(([s["name"] for s in r["launch"]], [s["name"] for s in r["queued"]]), ([], ["docs"]))
            self.assertIn("progress.reserve", verdict("Ghost: docs", "devkit:ghost") or "")
            self.assertIsNone(verdict("Neo: fix the login timeout", "devkit:neo"))


class ReserveOfferTests(unittest.TestCase):
    # R4: dev crew next offers the reserved slot to the step the developer asked for in this turn, as the guard lets
    # its launch take it; the plan's older steps stay queued
    def test_dev_crew_next_offers_the_reserved_slot_to_the_developers_new_step(self):
        with TempState() as st, config(max_parallel=2):
            now = time.time()
            pg.start("Work", plan="api=30m; docs=30m", now=now - 3600)
            pg.assign("api", "Tank", why="test")
            pg.assign("docs", "Ghost", why="test")
            launch("a_tank", "devkit:tank", "Tank: api")
            turn_started(st, now - 60)
            pg.add_step("login fix=30m")  # what the developer asked for in this turn
            pg.assign("login fix", "Neo", why="test")
            r = dispatch.ready(pg.load_run("claude", "s1"))
            self.assertEqual(([s["name"] for s in r["launch"]], [s["name"] for s in r["queued"]]),
                             (["login fix"], ["docs"]))
            self.assertIsNone(verdict("Neo: login fix", "devkit:neo"))


class OrderTests(unittest.TestCase):
    def plan(self, st, spec, members):
        now = time.time()
        pg.start("Work", plan=spec, now=now - 3600)
        for name, who in members.items():
            pg.assign(name, who, why="test")
        turn_started(st, now - 60)

    # R5: the ready steps rank by critical path, then the developer's latest request, then age, each with its reason
    def test_ready_steps_rank_by_critical_path_then_latest_request_then_age(self):
        with TempState() as st, config(max_parallel=6, reserve=0):
            self.plan(st, "api docs=20m; orders api=30m; orders screen=60m after orders api; lint script=20m",
                      {"api docs": "Ghost", "orders api": "Tank", "orders screen": "Trinity", "lint script": "Kid"})
            pg.add_step("login fix=20m")  # what the developer asked for in this turn
            pg.assign("login fix", "Neo", why="test")
            run = pg.load_run("claude", "s1")
            self.assertEqual([s["name"] for s in dispatch.ready(run)["launch"]],
                             ["orders api", "login fix", "api docs", "lint script"])
            why = dict((s["name"], reason) for s, reason in dispatch.order(run))
            self.assertIn("critical path: 2 steps", why["orders api"])
            self.assertIn("the developer's latest request", why["login fix"])
            self.assertIn("waiting since", why["api docs"])

    # R5: Morpheus launches in that order, unless he records why (dev crew next --first <step> --why "…")
    def test_a_launch_out_of_order_needs_a_recorded_why(self):
        with TempState() as st, config(max_parallel=2):
            self.plan(st, "orders api=30m; orders screen=60m after orders api; api docs=20m",
                      {"orders api": "Tank", "api docs": "Ghost"})
            refused = verdict("Ghost: api docs", "devkit:ghost") or ""
            self.assertIn("“orders api” comes first", refused)
            self.assertIn("dev crew next --first", refused)
            dispatch.order_why("api docs", "the developer reads the docs before the API today")
            self.assertIsNone(verdict("Ghost: api docs", "devkit:ghost"))
            self.assertEqual([r["target"] for r in audit.records(1, "crew.order-override")], ["api docs"])


class StoppedAgentTests(unittest.TestCase):
    # R6 (B-fxsvxz9n78p): TaskStop fires no SubagentStop, so the stopped agent's slot is free at the stop call's result;
    # its step waits for a new launch, and the agent takes its slot back when it works again (SendMessage)
    def test_a_stopped_agent_frees_its_slot_at_the_stop_calls_result(self):
        with TempState():
            pg.start("Work", plan=["api"])
            pg.assign("api", "Tank")
            launch("a_tank", "devkit:tank", "Tank: api")
            pg.reconcile_hook("claude", {
                "session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "TaskStop",
                "tool_input": {"task_id": "a_tank"},
                "tool_response": {"message": "Successfully stopped task: a_tank (Tank: api)", "task_id": "a_tank",
                                  "task_type": "local_agent", "command": "Tank: api"}})
            run = pg.load_run("claude", "s1")
            self.assertEqual((run["agents"]["a_tank"]["state"], run["plan"][0]["state"]), ("stopped", "pending"))
            self.assertEqual(pg.slots(run), [])
            self.assertEqual([r["target"] for r in audit.records(1, "crew.stopped")], ["a_tank"])
            pg.bind("claude", {"session_id": "s1", "agent_id": "a_tank", "agent_type": "devkit:tank"})
            self.assertEqual([k for k, _ in pg.slots(pg.load_run("claude", "s1"))], ["a_tank"])


class LaneStoppedTests(unittest.TestCase):
    # A Release engineer lane stopped by TaskStop stayed running for hours and held a crew slot: a stopped lane ends
    # as stopped, frees its slot, and its step waits for a new launch, unless a live lane of the same member holds it
    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.t0 = time.time() - 3600
        pg.start("Work", plan=["ciwatch reports once"], now=self.t0)
        pg.assign("ciwatch reports once", "Link", now=self.t0)
        launch("a_link", "devkit:link", "Link: ciwatch reports once", at=self.t0 + 60)

    def test_a_stopped_lane_frees_its_slot_and_its_step_waits_for_a_new_launch(self):
        made = pg.lane_stopped("a_link", "stopped by the tech lead: wrong brief", agent="claude", sid="s1",
                               now=self.t0 + 120)
        run = pg.load_run("claude", "s1")
        step = run["plan"][0]
        self.assertEqual(run["agents"]["a_link"]["state"], "stopped")
        self.assertEqual((step["state"], step["owner"], step["label"]), ("pending", "main", "Link"))
        self.assertEqual(pg.slots(run), [])
        self.assertIn("wrong brief", next(h["why"] for h in run["history"] if h["step"] == "ciwatch reports once"
                                          and h["to"] == "pending"))
        self.assertEqual([r["target"] for r in audit.records(1, "crew.stopped")], ["a_link"])
        self.assertTrue(made)
        self.assertIn("ciwatch reports once", dispatch.ready_context("claude", "s1"))  # announced ready to launch

    def test_a_stopped_lanes_step_stays_with_a_live_lane_of_its_member_that_names_it(self):
        launch("a_link2", "devkit:link", "Link: ciwatch reports once", at=self.t0 + 90)  # relaunched before the stop
        pg.lane_stopped("a_link", "stopped by the tech lead: a second launch took over", agent="claude", sid="s1",
                        now=self.t0 + 120)
        run = pg.load_run("claude", "s1")
        self.assertEqual((run["plan"][0]["state"], run["plan"][0]["owner"]), ("running", "a_link2"))
        self.assertEqual([k for k, _ in pg.slots(run)], ["a_link2"])


class VisibleTests(unittest.TestCase):
    # R7 (acceptance): dev crew and the status block's Crew line say how many slots are in use, and why the limit is
    # lower than progress.max_parallel
    def test_dev_crew_and_the_crew_line_show_the_slots_and_why(self):
        with TempState(), config(max_parallel=6):
            usage(five=87)
            pg.start("Work", plan=["api"])
            pg.assign("api", "Tank")
            launch("a_tank", "devkit:tank", "Tank: api")
            run = pg.load_run("claude", "s1")
            shown = "1 of 3 slots in use (lowered from 6: 5-hour usage at 87 %)"
            self.assertIn(shown, crew.run_text(crew.run_view(run)))
            line = next(ln for ln in pg.render_update(run, branch="b").splitlines() if ln.startswith("- **Crew:**"))
            self.assertIn(shown, line)

    # R7: every change of the effective limit is audited once, with its reason
    def test_each_change_of_the_limit_is_audited_once(self):
        with TempState():
            for used in (10, 87, 87, 10):
                usage(five=used)
                pg.parallel_limit("claude")
            self.assertEqual([r["detail"] for r in audit.records(1, "crew.limit")],
                             ["4 → 2: 5-hour usage at 87 %", "2 → 4: progress.max_parallel"])


if __name__ == "__main__":
    unittest.main()
