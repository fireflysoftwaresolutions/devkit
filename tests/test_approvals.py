# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Step endings, approvals and the reconciler: lifecycle spec F6 and C7, crew review R2-3 and R2-4.
# Run: python3 -m unittest tests.test_approvals
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from devkit import approvals as ap, audit, core, crew, progress as pg, raci  # noqa: E402

ROSTER = crew.load(team=False)  # devkit's own roster, never the team's changes in the developer's handoff repository
T = 1_800_000_000.0  # a fixed clock
LANE = {"tank": "a-tank", "trinity": "a-trinity", "kid": "a-kid", "seraph": "a-seraph", "keymaker": "a-keymaker",
        "hamann": "a-hamann", "link": "a-link"}
REPORT = ("**[Firefly> DevKit · Tank]** devkit · `release/26.11.3` · the API\n- **Done:** built the API\n"
          "- **Check:** {}\n- **Next:** Seraph reviews it\n")


def lanes():
    return {aid: {"persona": crew.member(mid, ROSTER)["name"], "state": "done", "updates": [], "started": T - 3600}
            for mid, aid in LANE.items()}


def step(name, by, activity, at=T, state="done", after=()):
    return {"name": name, "state": state, "owner": "main" if by == "morpheus" else LANE[by], "activity": activity,
            "started": at - 600, "finished": at if state == "done" else None, "after": list(after)}


def run_of(*steps, **extra):
    return {"v": 2, "agent": "claude", "session": "s1", "goal": "Release", "state": "running", "plan": list(steps),
            "agents": lanes(), "intervals": [], **extra}


def developer_roster():
    # the developer's decision R2-9: scope is approved by the developer once F6 accepts "developer" as an approver
    r = copy.deepcopy(ROSTER)
    raci.activity("scope", r)["A"] = "developer"
    return r


class ApprovalTests(unittest.TestCase):
    def test_an_approvers_earlier_step_does_not_approve_later_work(self):
        # G10: today any step Seraph finished in the run counts as his review, even one done before the work
        run = run_of(step("parser", "tank", "backend", at=T - 600),
                     step("review parser", "seraph", "review", at=T, after=["parser"]),
                     step("api", "tank", "backend", at=T + 600))
        ap.record(run, "seraph", ["parser"], "approved", T, roster=ROSTER)
        self.assertEqual(ap.owed(run, roster=ROSTER), [("seraph", ["api"])])

    def test_one_review_approves_only_the_steps_it_follows(self):
        # CR1-F2: one Seraph review counted as his approval of all 17 finished steps of the live run
        run = run_of(step("api", "tank", "backend"), step("screens", "trinity", "frontend"),
                     step("cli", "kid", "tooling"), step("review api", "seraph", "review", at=T + 60, after=["api"]))
        ap.from_review(run, run["plan"][3], "✓ read the diff, 40 tests pass", T + 60, roster=ROSTER)
        self.assertEqual(ap.owed(run, roster=ROSTER), [("seraph", ["screens", "cli"])])

    def test_a_step_whose_accountable_did_it_still_owes_an_approval(self):
        # K3 and R2-3: the Keymaker approves CI/CD; when he does that work himself, the Code reviewer approves it
        run = run_of(step("pipeline", "keymaker", "cicd"))
        self.assertEqual(ap.owed(run, roster=ROSTER), [("seraph", ["pipeline"])])
        ap.record(run, "seraph", ["pipeline"], "approved", T + 60, roster=ROSTER)
        self.assertEqual(ap.owed(run, roster=ROSTER), [])

    def test_work_done_by_its_own_approver_goes_to_the_next_one(self):
        # R2-3: Seraph approves code and configuration, Morpheus everything else; and since nobody approves their
        # own work, Seraph's own code goes to Morpheus and Morpheus's own documentation to the developer
        act = lambda aid: raci.activity(aid, ROSTER)  # noqa: E731
        self.assertEqual([ap.approver(act("backend"), "tank"), ap.approver(act("cicd"), "keymaker"),
                          ap.approver(act("scope"), "hamann"), ap.approver(act("backend"), "seraph"),
                          ap.approver(act("docs"), "morpheus")],
                         ["seraph", "seraph", "morpheus", "morpheus", "developer"])

    def test_every_kind_of_work_says_who_approves_it_when_its_approver_did_it(self):
        # a kind of work added to config/crew.json is code or configuration (Seraph) or other work (Morpheus)
        for a in raci.activities(ROSTER):
            with self.subTest(activity=a["id"]):
                self.assertTrue(a.get("A_fallback") or (a["id"] in ap.CODE_AND_CONFIG) != (a["id"] in ap.OTHER_WORK))

    def test_self_approval_is_refused(self):
        run = run_of(step("api", "tank", "backend"), step("pipeline", "keymaker", "cicd"))
        with self.assertRaisesRegex(ap.ApprovalError, "Tank.* did “api”"):
            ap.record(run, "tank", ["api"], "approved", T + 60, roster=ROSTER)
        with self.assertRaisesRegex(ap.ApprovalError, "Keymaker.* did “pipeline”.*Seraph"):
            ap.record(run, "keymaker", ["pipeline"], "approved", T + 60, roster=ROSTER)
        self.assertEqual(run.get("approvals") or [], [])

    def test_a_step_with_no_kind_of_work_takes_no_approval_from_anyone(self):
        # Seraph, review batch 4: with no kind of work approver() names no one, and record took any member's verdict
        run = run_of(step("x7", "tank", ""))
        self.assertIsNone(ap.approver(raci.step_activity(run["plan"][0], ROSTER), "tank"))
        for who in ("link", "seraph"):
            with self.subTest(member=who), self.assertRaisesRegex(ap.ApprovalError, "“x7” has no kind of work"):
                ap.record(run, who, ["x7"], "approved", T + 60, roster=ROSTER)
        self.assertNotIn("approvals", run)

    def test_only_the_steps_approver_may_approve_it(self):
        run = run_of(step("api", "tank", "backend"), step("ui", "trinity", "frontend", state="unverified"))
        with self.assertRaisesRegex(ap.ApprovalError, "Seraph.* approves “api”"):
            ap.record(run, "link", ["api"], "approved", T + 60, roster=ROSTER)
        with self.assertRaisesRegex(ap.ApprovalError, "“ui” is unverified"):
            ap.record(run, "seraph", ["ui"], "approved", T + 60, roster=ROSTER)

    def test_an_approval_counts_only_after_the_work_it_approves(self):
        run = run_of(step("api", "tank", "backend"))
        ap.record(run, "seraph", ["api"], "changes", T + 60, roster=ROSTER)
        self.assertEqual(ap.owed(run, roster=ROSTER), [("seraph", ["api"])])  # changes requested: still owed
        ap.record(run, "seraph", ["api"], "approved", T + 120, roster=ROSTER)
        self.assertEqual(ap.owed(run, roster=ROSTER), [])
        run["plan"][0]["finished"] = T + 600  # reopened and done again after the approval
        self.assertEqual(ap.owed(run, roster=ROSTER), [("seraph", ["api"])])

    def test_the_developer_approves_through_a_verified_decision(self):
        roster = developer_roster()
        run = run_of(step("scope", "hamann", "scope"))
        d = {"id": "D1", "state": "decided", "answer": "approve", "by": "developer", "verified": False,
             "decided": T + 60, "approves": ["scope"]}
        run["decisions"] = [d]
        self.assertEqual(ap.owed(run, roster=roster), [("developer", ["scope"])])  # "developer" typed, no evidence
        d["verified"] = True
        self.assertEqual(ap.owed(run, roster=roster), [])

    def test_a_direct_developer_approval_needs_fresh_developer_evidence_used_once(self):
        roster = developer_roster()
        run = run_of(step("scope", "hamann", "scope"))
        e = {"ts": T + 30, "channel": "E2", "covers": "D2", "answer": "approve", "transcript_uuid": "u-1",
             "used": T + 31}
        for bad in (dict(e, channel="tool"), dict(e, ts=T - 60)):  # a tool result; evidence from before the work
            with self.assertRaisesRegex(ap.ApprovalError, "developer"):
                ap.record(run, "developer", ["scope"], "approved", T + 31, evidence=bad, roster=roster)
        ap.record(run, "developer", ["scope"], "approved", T + 31, evidence=e, roster=roster)
        self.assertEqual(ap.owed(run, roster=roster), [])
        with self.assertRaisesRegex(ap.ApprovalError, "already"):
            ap.record(run, "developer", ["scope"], "approved", T + 40, evidence=e, roster=roster)

    def test_an_approver_may_not_take_its_own_kind_of_work(self):
        # R2-3: only the Responsible member takes a kind of work; its Accountable or a Consulted member needs --why
        cicd = raci.activity("cicd", ROSTER)
        self.assertEqual([m for m in ("link", "keymaker", "niobe") if ap.takes(m, cicd)], ["link"])


class EndingTests(unittest.TestCase):
    def test_a_member_step_ends_done_only_with_evidence(self):
        # CR1-F1: today SubagentStop closes every step the member owns as done, with no evidence
        lane = {"updates": []}
        self.assertEqual(ap.ending(lane, "")[0], "unverified")
        self.assertEqual(ap.ending(lane, REPORT.format("✓ 12 tests pass"))[0], "done")
        lane["updates"] = [{"ts": T, "kind": "step", "text": "parser built", "check": "12 tests pass"}]
        self.assertEqual(ap.ending(lane, "")[0], "done")

    def test_a_failed_or_partial_report_leaves_the_step_unverified(self):
        lane = {"updates": [{"ts": T, "kind": "step", "text": "parser built", "check": "12 tests pass"}]}
        for check in ("✗ 3 tests fail", "⚠ not run on Python 3.9"):
            with self.subTest(check=check):
                self.assertEqual(ap.ending(lane, REPORT.format(check))[0], "unverified")


class EndOfRunTests(unittest.TestCase):
    def test_the_end_of_the_run_names_every_approval_owed(self):
        run = run_of(step("api", "tank", "backend"), step("pipeline", "keymaker", "cicd"))
        with self.assertRaisesRegex(ap.ApprovalError, "Seraph.* approves “api”, “pipeline”"):
            ap.end_check(run, now=T + 60, roster=ROSTER)
        over = ap.end_check(run, why="reviewed in the PR", now=T + 60, roster=ROSTER)
        self.assertEqual(over["owed"], [["seraph", ["api", "pipeline"]]])

    def test_a_step_flagged_recheck_blocks_the_end_of_the_run(self):
        run = run_of(step("api", "tank", "backend"), step("screens", "trinity", "frontend", after=["api"]))
        ap.record(run, "seraph", ["api", "screens"], "approved", T + 60, roster=ROSTER)
        run["plan"][1]["recheck"] = ["api"]  # api was reopened after screens used it (progress._recheck_dependents)
        with self.assertRaisesRegex(ap.ApprovalError, "“screens”.*“api”"):
            ap.end_check(run, now=T + 120, roster=ROSTER)
        over = ap.end_check(run, why="screens only reads the API's types", now=T + 120, roster=ROSTER)
        self.assertEqual(over["recheck"], [["screens", ["api"]]])


class ReconcileTests(unittest.TestCase):
    def working(self, **lane):
        run = run_of(step("api", "tank", "backend", state="running"))
        run["agents"]["a-tank"].update(state="running", binding="exact", **lane)
        return run

    def test_a_finished_meta_without_subagent_stop_frees_the_slot(self):
        # G8: today the lane holds a crew slot for stale_agent_hours (6 h)
        run = self.working()
        self.assertEqual(ap.reconcile(run, {"lanes": {"a-tank": {"meta_finished": T - 60}}}, T), [])  # 2 min grace
        before = copy.deepcopy(run)
        moves = ap.reconcile(run, {"lanes": {"a-tank": {"meta_finished": T - 150}}}, T)
        self.assertEqual(run, before)  # reconcile is pure
        self.assertTrue(ap.apply(run, moves, T))
        self.assertEqual(pg.slots(run, {}, now=T), [])
        self.assertEqual((run["agents"]["a-tank"]["state"], run["plan"][0]["state"]), ("unverified", "unverified"))
        self.assertEqual(ap.apply(run, moves, T), [])  # two hooks at once: each transition is a compare-and-set

    def test_a_silent_lane_is_lost_and_recovers_at_its_next_hook(self):
        run = self.working(started=T - 95 * 60, updates=[{"ts": T - 91 * 60, "kind": "note", "text": "running"}])
        self.assertEqual(ap.reconcile(run, {}, T - 120), [])  # silent 89 minutes
        ap.apply(run, ap.reconcile(run, {}, T), T)  # silent 91 minutes: lost, its step waits for an owner
        self.assertEqual((run["agents"]["a-tank"]["state"], run["plan"][0]["state"], run["plan"][0]["owner"]),
                         ("lost", "pending", "main"))
        back = copy.deepcopy(run)
        ap.apply(run, ap.reconcile(run, {"lanes": {"a-tank": {"last_seen": T + 60}}}, T + 60), T + 60)
        self.assertEqual((run["agents"]["a-tank"]["state"], run["plan"][0]["state"], run["plan"][0]["owner"]),
                         ("running", "running", "a-tank"))
        self.assertEqual(ap.reconcile(run, {}, T + 120), [])  # its silence counts from when it was seen again
        back["plan"][0]["label"] = "Neo"  # Morpheus gave the step to someone else meanwhile: it stays his
        ap.apply(back, ap.reconcile(back, {"lanes": {"a-tank": {"last_seen": T + 60}}}, T + 60), T + 60)
        self.assertEqual((back["agents"]["a-tank"]["state"], back["plan"][0]["owner"]), ("running", "main"))

    def test_a_launch_pending_past_its_ttl_fails(self):
        run = run_of()
        sess = {"launches": {"toolu_1": {"ts": T - 16 * 60, "state": "pending", "label": "Tank: the API"},
                             "toolu_2": {"ts": T - 60, "state": "pending", "label": "Kid: the hooks"}}}
        ap.apply_session(sess, ap.reconcile(run, ap.facts(run, sess, T), T), T)
        self.assertEqual([sess["launches"][t]["state"] for t in ("toolu_1", "toolu_2")], ["failed", "pending"])


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.runs, self.sessions = root / "runs", root / "sessions"
        self.runs.mkdir()
        self.sessions.mkdir()
        self.patches = [mock.patch.object(audit, "AUDIT", root / "audit"),  # never the real audit trail
                        mock.patch.dict(core.CFG, {"progress": {}})]  # devkit's defaults, never the developer's
        for p in self.patches:
            p.start()
        run = run_of(step("api", "tank", "backend", state="running"))
        run["agents"]["a-tank"].update(state="running", binding="exact", started=T - 60)
        (self.runs / "claude-s1.json").write_text(json.dumps(run))

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def saved(self):
        return json.loads((self.runs / "claude-s1.json").read_text())

    def test_the_scheduled_sweep_closes_the_run_of_an_ended_session_only(self):
        # G8: SessionEnd deletes the session record and the run stays running; dev sync --scheduled closes it
        me = os.getpid()
        record = self.sessions / "claude-s1.json"
        record.write_text(json.dumps({"pid": me, "pid_start": ap.pid_start(me)}))
        self.assertEqual((ap.sweep(self.runs, self.sessions, now=T), self.saved()["state"]), ({}, "running"))
        record.unlink()
        ap.sweep(self.runs, self.sessions, now=T)
        self.assertEqual((self.saved()["state"], self.saved()["agents"]["a-tank"]["state"]), ("orphaned", "lost"))

    def test_the_sweep_frees_the_lane_of_an_agent_whose_meta_file_says_finished(self):
        # Claude Code marks <session>/subagents/agent-<id>.meta.json finished; today devkit ignores it
        folder = Path(self.tmp.name, "s1", "subagents")
        folder.mkdir(parents=True)
        meta = folder / "agent-a-tank.meta.json"
        meta.write_text(json.dumps({"description": "Tank: the API", "finished": True}))
        os.utime(str(meta), (T - 300, T - 300))
        me = os.getpid()
        (self.sessions / "claude-s1.json").write_text(json.dumps({
            "pid": me, "pid_start": ap.pid_start(me), "transcript": str(Path(self.tmp.name, "s1.jsonl"))}))
        ap.sweep(self.runs, self.sessions, now=T)
        self.assertEqual((self.saved()["state"], self.saved()["agents"]["a-tank"]["state"]), ("running", "unverified"))

    def test_a_reused_pid_is_not_alive(self):
        # pids are reused: a session is alive only when its pid's start time is the one recorded at SessionStart
        me = os.getpid()
        self.assertEqual([ap.pid_alive(me, ap.pid_start(me)), ap.pid_alive(me, "Mon Jan  1 00:00:00 2001")],
                         [True, False])


class OptionalSkillGroupTests(unittest.TestCase):
    def test_iac_is_offered_for_terraform_and_never_installed_by_default(self):
        # M128: Niobe's Terraform skills are group `iac`; the wizard offers it for infrastructure work only
        groups = {s.get("group") for s in json.loads((ROOT / "config" / "skills.json").read_text())["sources"]}
        self.assertIn("iac", groups)
        self.assertNotIn("iac", core.DEFAULTS["skills"]["groups"])
        offered = [core.offered_skill_groups(c, core.DEFAULTS) for c in (["aws", "terraform"], ["aws"])]
        self.assertEqual(offered, [["iac"], []])


if __name__ == "__main__":
    unittest.main()
