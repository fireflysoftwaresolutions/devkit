# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Know who is editing a file before you edit it, and never let an ended member hold a file (hotfix 26.11.6). A lane
# that another session's run still lists as running holds its files only while it works: an ended member (devkit or
# Claude Code saw it end, whichever session id the end came under) and a stale one (its session's process is gone,
# or it has been silent for progress.stale_agent_minutes) hold nothing. `dev crew owner <path>…` says who holds each
# path and what to do, and agrees with the edit check (hooks._crew_limits) because one function decides both
# (progress.scope_holders). A stop that comes under the conversation's newer session id closes the lane in the run
# that lists it, and `dev session carry` closes ended and stale lanes as it carries the run.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_scope_liveness
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import approvals, cli, continuity, core, crew, dispatch, guard, hooks, progress as pg, work  # noqa: E402
from test_progress import TempState, launch  # noqa: E402

TEAMLESS = crew.load
MIN = 60.0


class OwnerCase(unittest.TestCase):
    # a session s1 (this one) and another session s2 of the same repository, each with Claude Code's transcript
    # folder; s2's process is a live child of the test, so only what the test changes makes s2 look gone
    def setUp(self):
        self.t = TempState().__enter__()
        self.addCleanup(self.t.__exit__, None, None, None)
        self.root = Path(self.t.tmp.name)
        self.repo = self.root / "Development" / "acme" / "api"
        (self.repo / "lib").mkdir(parents=True)
        self.cc = self.root / "claude-projects"
        handoff = self.root / "handoff"
        handoff.mkdir()
        for p in (mock.patch.object(guard, "DEV", str(self.root / "Development")),
                  mock.patch.object(core, "HANDOFF", handoff), mock.patch.object(work, "HANDOFF", handoff),
                  mock.patch.object(crew, "load", lambda *a, **k: TEAMLESS(team=False))):
            p.start()
            self.addCleanup(p.stop)
        self.now = time.time()
        self.proc = subprocess.Popen(["/bin/sleep", "120"])
        self.addCleanup(self.proc.wait)
        self.addCleanup(self.proc.kill)
        self.session("s1", pid=None)
        self.session("s2", pid=self.proc.pid)

    def session(self, sid, pid):
        rec = {"agent": "claude", "session": sid, "started": self.now - 3 * 3600, "project": "acme/api",
               "top": str(self.repo), "transcript": str(self.cc / f"{sid}.jsonl")}
        if pid:
            rec.update(pid=pid, pid_start=approvals.pid_start(pid))
        pg.session_path("claude", sid).write_text(json.dumps(rec))

    def dead_pid(self):
        p = subprocess.Popen(["/usr/bin/true"])
        p.wait()
        return p.pid

    def lane(self, persona, label, scope, active):
        # a lane its run lists as running, last active `active` seconds ago
        return {"state": "running", "persona": persona, "label": label, "scope": scope, "binding": "exact",
                "started": self.now - 2 * 3600, "last_seen": self.now - active,
                "updates": [{"ts": self.now - active, "kind": "note", "text": "working"}]}

    def other_run(self, lanes, steps=()):
        pg.RUNS.mkdir(parents=True, exist_ok=True)
        pg.run_path("claude", "s2").write_text(json.dumps({
            "v": 2, "goal": "Branch cleanup", "state": "running", "agent": "claude", "session": "s2",
            "project": "acme/api", "started": self.now - 3 * 3600, "plan": list(steps), "agents": lanes,
            "history": []}))

    def claude_meta(self, sid, aid, finished, at, log_at):
        # Claude Code's own record of a subagent, beside the transcript of the session it now runs in
        d = self.cc / sid / "subagents"
        d.mkdir(parents=True, exist_ok=True)
        meta, log = d / f"agent-{aid}.meta.json", d / f"agent-{aid}.jsonl"
        meta.write_text(json.dumps({"agentType": "devkit:kid", **({"finished": True} if finished else {})}))
        log.write_text(json.dumps({"type": "assistant", "message": {"content": "working"}}) + "\n")
        os.utime(meta, (at, at))
        os.utime(log, (log_at, log_at))

    def edit(self, rel):
        return hooks._crew_limits("claude", {"session_id": "s1", "tool_name": "Edit", "cwd": str(self.repo),
                                             "tool_input": {"file_path": str(self.repo / rel)}})

    def owner(self, *rels, as_json=True):
        # dev crew owner as the main session's shell runs it, in the repository
        out, err, back = io.StringIO(), io.StringIO(), os.getcwd()
        self.addCleanup(os.chdir, back)
        os.chdir(self.repo)
        with mock.patch.dict(os.environ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            os.environ.pop("DEVKIT_FROM_AGENT", None)
            rc = cli.main(["crew", "owner", *[str(self.repo / r) for r in rels]] + (["--json"] if as_json else []))
        os.chdir(back)
        text = out.getvalue()
        return rc, (json.loads(text) if as_json else text + err.getvalue())


class EditCheckTests(OwnerCase):
    def test_a_working_member_of_another_session_still_refuses_the_edit_and_says_how_to_check_again(self):
        self.other_run({"a_kid": self.lane("Kid", "Kid: branches", ["lib/branches/**"], active=3 * MIN)})
        v = self.edit("lib/branches/prune.py")
        self.assertEqual(v[0] if v else None, "deny")
        self.assertIn("belongs to DevEx engineer (Kid) while they work", v[1])
        self.assertIn("last activity", v[1])
        self.assertIn("dev crew owner lib/branches/prune.py", v[1])
        self.assertIn('dev crew ask --to kid "…"', v[1])

    def test_a_member_that_ended_under_the_conversations_new_session_id_holds_nothing(self):
        # tonight's case: the stop came under s1, so s2's run still lists Kid as running; Claude Code wrote its
        # finished record beside s1's transcript, and nothing in Kid's transcript after it
        self.other_run({"a_kid": self.lane("Kid", "Kid: branches", ["lib/branches/**"], active=25 * MIN)})
        self.claude_meta("s2", "a_kid", finished=False, at=self.now - 2 * 3600, log_at=self.now - 25 * MIN - 1)
        self.claude_meta("s1", "a_kid", finished=True, at=self.now - 25 * MIN, log_at=self.now - 60 * MIN)
        self.assertIsNone(self.edit("lib/branches/prune.py"))
        # resumed after its finish (SendMessage), it writes its transcript again and holds its files again
        self.claude_meta("s1", "a_kid", finished=True, at=self.now - 25 * MIN, log_at=self.now - 30)
        self.assertEqual((self.edit("lib/branches/prune.py") or ("allow",))[0], "deny")

    def test_a_member_silent_past_the_stale_minutes_holds_nothing(self):
        stale = approvals.limits()["stale_minutes"]
        self.other_run({"a_kid": self.lane("Kid", "Kid: branches", ["lib/branches/**"], active=(stale + 1) * MIN)})
        self.assertIsNone(self.edit("lib/branches/prune.py"))
        self.other_run({"a_kid": self.lane("Kid", "Kid: branches", ["lib/branches/**"], active=(stale - 1) * MIN)})
        self.assertEqual((self.edit("lib/branches/prune.py") or ("allow",))[0], "deny")

    def test_a_member_whose_sessions_process_is_gone_holds_nothing(self):
        self.other_run({"a_kid": self.lane("Kid", "Kid: branches", ["lib/branches/**"], active=2 * MIN)})
        self.session("s2", pid=self.dead_pid())
        self.assertIsNone(self.edit("lib/branches/prune.py"))
        pg.session_path("claude", "s2").unlink()  # its session ended: SessionEnd deletes the record
        self.assertIsNone(self.edit("lib/branches/prune.py"))


class OwnerCommandTests(OwnerCase):
    def fixture(self):
        # three lanes in s2's run, one per state, and one member of this session's own run
        stale = approvals.limits()["stale_minutes"]
        self.other_run({"a_kid": self.lane("Kid", "Kid: branches", ["lib/branches/**"], active=4 * MIN),
                        "a_ghost": self.lane("Ghost", "Ghost: docs", ["lib/docs/**"], active=(stale + 5) * MIN),
                        "a_key": self.lane("The Keymaker", "Keymaker: guard", ["lib/guard/**"], active=30 * MIN)},
                       steps=[{"name": "branches", "state": "running", "owner": "a_kid"}])
        self.claude_meta("s1", "a_key", finished=True, at=self.now - 30 * MIN, log_at=self.now - 30 * MIN - 1)
        pg.start("Own work", plan=["parser"], now=self.now)
        pg.assign("parser", "Neo", scope="lib/parser/**", now=self.now)
        launch("a_neo", "devkit:neo", "Neo: parser", self.now)

    def test_the_command_and_the_edit_check_agree_on_every_path(self):
        self.fixture()
        paths = ["lib/branches/a.py", "lib/docs/b.md", "lib/guard/c.py", "lib/parser/d.py", "README.md"]
        refused = [p for p in paths if self.edit(p)]
        held = [p for p in paths if self.owner(p)[0] == 1]
        self.assertEqual(refused, ["lib/branches/a.py", "lib/parser/d.py"])
        self.assertEqual(held, refused)
        rc, out = self.owner(*paths)
        self.assertEqual(rc, 1)
        self.assertEqual([r["free"] for r in out], [False, True, True, False, True])

    def test_each_holder_is_named_with_its_run_session_step_state_and_what_to_do(self):
        self.fixture()
        rc, out = self.owner("lib/branches/a.py", "lib/docs/b.md", "lib/guard/c.py")
        kid, ghost, key = (r["holders"][0] for r in out)
        self.assertEqual((kid["who"], kid["state"], kid["session"], kid["run"], kid["step"]),
                         ("DevEx engineer (Kid)", "working", "s2", "Branch cleanup", "branches"))
        self.assertIn('dev crew ask --to kid "…"', kid["do"])
        self.assertEqual((ghost["who"], ghost["state"]), ("Technical writer (Ghost)", "stale"))
        self.assertIn("silent", ghost["why"])
        self.assertEqual((key["who"], key["state"]), ("Security engineer (The Keymaker)", "ended"))
        self.assertLess(abs(kid["last"] - (self.now - 4 * MIN)), 2)
        rc, text = self.owner("lib/branches/a.py", "lib/docs/b.md", "lib/guard/c.py", as_json=False)
        self.assertEqual(rc, 1)
        self.assertIn("DevEx engineer (Kid) · working", text)
        self.assertIn("Technical writer (Ghost) · stale", text)
        self.assertIn("Security engineer (The Keymaker) · ended", text)

    def test_every_path_free_exits_zero(self):
        self.fixture()
        rc, out = self.owner("lib/docs/b.md", "lib/guard/c.py", "README.md")
        self.assertEqual(rc, 0)
        self.assertTrue(all(r["free"] for r in out))


class StatusTests(OwnerCase):
    def test_dev_status_lists_no_ended_member_and_marks_a_stale_one(self):
        stale = approvals.limits()["stale_minutes"]
        self.other_run({"a_kid": self.lane("Kid", "Kid: branches", ["lib/branches/**"], active=4 * MIN),
                        "a_ghost": self.lane("Ghost", "Ghost: docs", ["lib/docs/**"], active=(stale + 5) * MIN),
                        "a_key": self.lane("The Keymaker", "Keymaker: guard", ["lib/guard/**"], active=30 * MIN)})
        self.claude_meta("s1", "a_key", finished=True, at=self.now - 30 * MIN, log_at=self.now - 30 * MIN - 1)
        crew_now = cli._crew_now([pg.load_run("claude", "s2")], TEAMLESS(team=False), self.now)
        self.assertEqual([(m["who"], m["state"]) for m in crew_now["members"]],
                         [("DevEx engineer (Kid)", "working"), ("Technical writer (Ghost)", "stale")])


class StopUnderANewSessionIdTests(OwnerCase):
    def run_in_s2(self):
        with mock.patch.dict(os.environ, {"DEVKIT_SESSION": "claude:s2"}):
            pg.start("Branch cleanup", plan=["branches"], now=self.now)
            pg.assign("branches", "Kid", scope="lib/branches/**", now=self.now)
        launch("a_kid", "devkit:kid", "Kid: branches", self.now, sid="s2")
        self.assertEqual(pg.load_run("claude", "s2")["agents"]["a_kid"]["state"], "running")

    def test_a_subagent_stop_that_comes_under_another_session_id_closes_the_lane_in_the_run_that_lists_it(self):
        self.run_in_s2()
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a_kid", "agent_type": "devkit:kid",
                                  "last_assistant_message": "done"}, False, now=self.now + 60)
        run = pg.load_run("claude", "s2")
        self.assertEqual(run["agents"]["a_kid"]["state"], "done")
        self.assertTrue(any("claude:s1" in h.get("why", "") for h in run.get("history") or []), run.get("history"))
        self.assertIsNone(self.edit("lib/branches/prune.py"))

    def test_a_task_stop_that_comes_under_another_session_id_stops_the_lane_in_the_run_that_lists_it(self):
        self.run_in_s2()
        pg.lane_stopped("a_kid", "stopped by TaskStop", agent="claude", sid="s1", now=self.now + 60)
        run = pg.load_run("claude", "s2")
        self.assertEqual(run["agents"]["a_kid"]["state"], "stopped")
        self.assertEqual(run["plan"][0]["state"], "pending")


class CarryTests(OwnerCase):
    def cleared(self):
        # s1 is the conversation of s2 after /clear (same live process): its continuity record links it
        continuity.CONT.mkdir(parents=True, exist_ok=True)
        continuity.record_path("claude", "s1").write_text(json.dumps({"session": "s1", "cleared_from": "s2"}))

    def test_carry_closes_ended_and_stale_lanes_with_the_reason_and_takes_the_run(self):
        stale = approvals.limits()["stale_minutes"]
        self.other_run({"a_kid": self.lane("Kid", "Kid: branches", ["lib/branches/**"], active=25 * MIN),
                        "a_ghost": self.lane("Ghost", "Ghost: docs", ["lib/docs/**"], active=(stale + 5) * MIN)},
                       steps=[{"name": "branches", "state": "running", "owner": "a_kid"},
                              {"name": "docs", "state": "running", "owner": "a_ghost"}])
        self.claude_meta("s1", "a_kid", finished=True, at=self.now - 25 * MIN, log_at=self.now - 26 * MIN)
        self.cleared()
        out = continuity.carry("claude", "s2", "s1", now=self.now)
        self.assertEqual(out["to"], "s1")
        run = pg.load_run("claude", "s1")
        self.assertEqual({k: v["state"] for k, v in run["agents"].items()}, {"a_kid": "unverified", "a_ghost": "lost"})
        whys = " ".join(h.get("why", "") for h in run["history"] if h["step"].startswith("(lane"))
        self.assertIn("carried to claude:s1", whys)
        self.assertIn("finished", whys)
        self.assertIn("silent", whys)

    def test_carry_refuses_only_for_the_members_that_still_work(self):
        self.other_run({"a_kid": self.lane("Kid", "Kid: branches", ["lib/branches/**"], active=2 * MIN),
                        "a_key": self.lane("The Keymaker", "Keymaker: guard", ["lib/guard/**"], active=40 * MIN)})
        self.claude_meta("s1", "a_key", finished=True, at=self.now - 40 * MIN, log_at=self.now - 41 * MIN)
        self.cleared()
        with self.assertRaises(ValueError) as e:
            continuity.carry("claude", "s2", "s1", now=self.now)
        self.assertIn("Kid still running", str(e.exception))
        self.assertNotIn("Keymaker", str(e.exception))
        self.assertEqual(pg.load_run("claude", "s2")["agents"]["a_kid"]["state"], "running")


class EndedTankCase(OwnerCase):
    # this session's run still lists the Backend engineer (Tank) as running, but Claude Code recorded him finished
    # beside this session's transcript ten minutes ago, after his last activity: `dev crew owner` and the Crew line
    # show him ended (Code reviewer, second pass)
    def ended_tank(self, plan, label, assign=None, mode=""):
        pg.start("Export", plan=plan, now=self.now - 3600, mode=mode)
        if assign:
            pg.assign(assign, "Tank", scope="lib/api/**", now=self.now - 3600)
        launch("a_tank", "devkit:tank", label, self.now - 3600)
        self.claude_meta("s1", "a_tank", finished=True, at=self.now - 10 * MIN, log_at=self.now - 11 * MIN)
        run = pg.load_run("claude", "s1")
        self.assertEqual(run["agents"]["a_tank"]["state"], "running")
        self.assertEqual(pg.lane_states(run, self.now)["a_tank"]["state"], "ended")

    def stale_tank(self, plan, label, assign=None, mode=""):
        # the same lane, silent past progress.stale_agent_minutes, with nothing that says it ended
        quiet = (approvals.limits()["stale_minutes"] + 20) * MIN
        pg.start("Export", plan=plan, now=self.now - quiet, mode=mode)
        if assign:
            pg.assign(assign, "Tank", scope="lib/api/**", now=self.now - quiet)
        launch("a_tank", "devkit:tank", label, self.now - quiet)
        run = pg.load_run("claude", "s1")
        self.assertEqual(run["agents"]["a_tank"]["state"], "running")
        self.assertEqual(pg.lane_states(run, self.now)["a_tank"]["state"], "stale")

    def lead_mail(self):
        # what reaches the tech lead with his next tool call
        return pg.on_tool_done("claude", {"session_id": "s1"}) or ""

    def mail_of(self, aid):
        return pg.on_tool_done("claude", {"session_id": "s1", "agent_id": aid}) or ""


class EndedMemberTakesNoStepTests(EndedTankCase):
    # so no new step goes to his lane and the files he held are free
    def test_a_step_delegated_to_an_ended_member_waits_for_a_new_launch(self):
        self.ended_tank(["api"], "Tank: api", assign="api")
        r = dispatch.delegate("add a nullable column for the export date", to="Tank", now=self.now)
        self.assertIsNone(r["lane"])
        run = pg.load_run("claude", "s1")
        step = run["plan"][-1]
        self.assertEqual((step["owner"], step["state"]), ("main", "pending"))
        self.assertIn(step["name"], [s["name"] for s in dispatch._sorted(run, self.now)[0]])  # dev crew next has it

    def test_a_step_assigned_to_an_ended_member_waits_for_a_new_launch(self):
        # his launch names the step and his lane holds no other one, so only his end keeps the step from his lane
        self.ended_tank(["export endpoint"], "Tank: export endpoint")
        pg.assign("export endpoint", "Tank", now=self.now)
        step = pg.load_run("claude", "s1")["plan"][0]
        self.assertEqual((step["owner"], step["state"]), ("main", "pending"))

    def test_a_scope_only_an_ended_member_held_is_free_to_assign(self):
        self.ended_tank(["api", "parser"], "Tank: api", assign="api")
        pg.assign("parser", "Neo", scope="lib/api/**", now=self.now)
        self.assertEqual(pg.load_run("claude", "s1")["plan"][1]["scope"], ["lib/api/**"])
        self.assertEqual(self.owner("lib/api/routes.py")[0], 0)  # as dev crew owner says


class MessageToAMemberWhoEndedTests(EndedTankCase):
    # a message for a member that ended or went stale waits for their next launch, as for a member with no lane,
    # and the sender is told; it never waits silently in the dead lane, where nobody reads it
    def test_a_message_for_a_member_who_ended_reaches_their_next_launch_and_the_sender_is_told(self):
        self.ended_tank(["api"], "Tank: api", assign="api")
        m = pg.crew_ask("Add the export date to the API", to="Tank", now=self.now)
        told = self.lead_mail()
        self.assertIn("Backend engineer (Tank) ended: Claude Code recorded it finished", told)
        self.assertIn(f"{m['id']} waits for their next launch: launch them again", told)
        launch("a_tank2", "devkit:tank", "Tank: export date", self.now + 60)
        self.assertIn("Add the export date to the API", self.mail_of("a_tank2"))

    def test_a_message_for_a_member_who_went_stale_reaches_their_next_launch_and_the_sender_is_told(self):
        self.stale_tank(["api"], "Tank: api", assign="api")
        m = pg.crew_ask("Add the export date to the API", to="Tank", now=self.now)
        told = self.lead_mail()
        self.assertIn("Backend engineer (Tank) went stale: silent for", told)
        self.assertIn(f"{m['id']} waits for their next launch", told)
        launch("a_tank2", "devkit:tank", "Tank: export date", self.now + 60)
        self.assertIn("Add the export date to the API", self.mail_of("a_tank2"))

    def test_a_member_who_asks_one_that_ended_is_told_to_ask_the_tech_lead_to_launch_them(self):
        self.ended_tank(["api", "ui"], "Tank: api", assign="api")
        pg.assign("ui", "Trinity", scope="web/**", now=self.now - 60)
        launch("a_tri", "devkit:trinity", "Trinity: ui", self.now - 60)
        m = pg.crew_ask("Which field holds the export date?", to="Tank", from_agent="a_tri", now=self.now)
        told = self.mail_of("a_tri")
        self.assertIn(f"{m['id']} waits for their next launch: ask the tech lead to launch them again", told)
        self.assertNotIn("waits for their next launch", self.lead_mail())  # the notice is the sender's, not his

    def test_a_message_for_a_member_goes_to_their_working_lane_not_to_a_newer_one_that_ended(self):
        # two lanes of the Backend engineer (Tank): the older one still works, the newer one ended
        pg.start("Export", plan=["api", "csv"], now=self.now - 20 * MIN)
        launch("a_old", "devkit:tank", "Tank: api", self.now - 20 * MIN)
        launch("a_new", "devkit:tank", "Tank: csv", self.now - 10 * MIN)
        self.claude_meta("s1", "a_new", finished=True, at=self.now - 2 * MIN, log_at=self.now - 3 * MIN)
        self.assertEqual({k: v["state"] for k, v in pg.lane_states(pg.load_run("claude", "s1"), self.now).items()},
                         {"a_old": "working", "a_new": "ended"})
        pg.crew_ask("Add the export date to the API", to="Tank", now=self.now)
        self.assertIn("Add the export date to the API", self.mail_of("a_old"))
        self.assertNotIn("waits for their next launch", self.lead_mail())


class ReplyToAMemberWhoEndedTests(EndedTankCase):
    # an answer (dev crew answer) and a decision's result (dev crew decide) went to the lane that asked, by its id,
    # even after that lane ended: the member's next launch never read them, and nobody was told (Code reviewer, final
    # pass). They follow the rule of any message now: the lane that asked while its member works, else the member's
    # working lane or next launch, and the sender is told
    def test_an_answer_for_a_member_who_ended_reaches_their_next_launch_and_the_answerer_is_told(self):
        self.ended_tank(["api"], "Tank: api", assign="api")
        q = pg.crew_ask("Which date format does the export use?", from_agent="a_tank", now=self.now - 15 * MIN)
        a = pg.crew_answer(q["id"], "ISO 8601, in UTC", now=self.now)
        told = self.lead_mail()
        self.assertIn("Backend engineer (Tank) ended: Claude Code recorded it finished", told)
        self.assertIn(f"{a['id']} waits for their next launch: launch them again", told)
        stored = next(m for m in pg.load_run("claude", "s1")["messages"] if m["id"] == a["id"])
        self.assertEqual(stored["reply_to"], q["id"])  # still the answer to his question
        launch("a_tank2", "devkit:tank", "Tank: export date", self.now + 60)
        self.assertIn("ISO 8601, in UTC", self.mail_of("a_tank2"))

    def test_a_decision_result_for_a_member_whose_lane_was_lost_reaches_their_next_launch(self):
        # his lane went quiet while his step waited on D1: the answer sends the step back for a new launch, and that
        # launch reads the answer
        pg.start("Export", plan=["api"], now=self.now - MIN, mode="guided")
        pg.assign("api", "Tank", scope="lib/api/**", now=self.now - MIN)
        launch("a_tank", "devkit:tank", "Tank: api", self.now - MIN)
        pg.crew_decision("Skip empty lines or reject the file?", options="A: skip | B: reject", recommend="A",
                         from_agent="a_tank", now=self.now)
        self.assertEqual(pg.load_run("claude", "s1")["plan"][0]["state"], "blocked")
        later = self.now + (pg.cfg()["stale_agent_hours"] + 1) * 3600
        run = pg.load_run("claude", "s1")
        self.assertTrue(pg.expire_stale(run, now=later))
        pg._save(run, touch_session=False)
        pg.crew_decide("D1", "B: reject", now=later)
        self.assertEqual(pg.load_run("claude", "s1")["plan"][0]["state"], "pending")
        told = self.lead_mail()
        self.assertIn("Backend engineer (Tank) ended: its lane is lost", told)
        self.assertIn("waits for their next launch: launch them again (dev crew brief tank", told)
        launch("a_tank2", "devkit:tank", "Tank: api", later + MIN)
        self.assertIn("Decision D1 — Skip empty lines or reject the file? → B: reject", self.mail_of("a_tank2"))

    def test_an_answer_and_a_decision_result_for_a_member_who_works_go_to_the_lane_that_asked(self):
        # two working lanes of the Backend engineer (Tank): the replies go to the older one, which asked, and not by
        # his name to his newest lane
        pg.start("Export", plan=["api", "csv"], now=self.now - 20 * MIN, mode="guided")
        launch("a_old", "devkit:tank", "Tank: api", self.now - 20 * MIN)
        launch("a_new", "devkit:tank", "Tank: csv", self.now - 10 * MIN)
        q = pg.crew_ask("Which date format does the export use?", from_agent="a_old", now=self.now - 5 * MIN)
        pg.crew_decision("Skip empty lines or reject the file?", options="A: skip | B: reject", recommend="A",
                         from_agent="a_old", now=self.now - 5 * MIN)
        pg.crew_answer(q["id"], "ISO 8601, in UTC", now=self.now)
        pg.crew_decide("D1", "B: reject", now=self.now)
        mail = self.mail_of("a_old")
        self.assertIn("ISO 8601, in UTC", mail)
        self.assertIn("Decision D1 — Skip empty lines or reject the file? → B: reject", mail)
        self.assertEqual(self.mail_of("a_new"), "")
        self.assertNotIn("waits for their next launch", self.lead_mail())


class DecisionOfAMemberWhoEndedTests(EndedTankCase):
    # a decided (dev crew decide) or withdrawn (dev crew withdraw) decision put the step it blocked back to running
    # under the lane that asked it, even when Claude Code had recorded that member finished, or he had gone stale,
    # while the run still listed his lane as running: nobody launched the step, dev crew next skipped it, and the sweep
    # later closed it as unverified (Senior engineer, probe after the reply fix). The step waits for a new launch now,
    # with the tech lead and its scope; a member who still works gets it back running (test_progress,
    # test_guided_waits_for_the_developer_and_resumes, test_withdrawing_a_decision_lets_the_asking_lane_work_again_…)
    def asks_d1(self, at):
        # the Backend engineer (Tank) asks D1 in guided mode, and his step "api" waits on it
        pg.crew_decision("Skip empty lines or reject the file?", options="A: skip | B: reject", recommend="A",
                         from_agent="a_tank", now=at)
        self.assertEqual(pg.load_run("claude", "s1")["plan"][0]["state"], "blocked")

    def waits_for_a_new_launch(self, why):
        run = pg.load_run("claude", "s1")
        step = run["plan"][0]
        self.assertEqual((step["state"], step["owner"], step.get("scope"), "waiting" in step),
                         ("pending", "main", ["lib/api/**"], False))
        self.assertIn("api", [s["name"] for s in dispatch._sorted(run, self.now)[0]])  # dev crew next offers it
        h = [h for h in run["history"] if h["step"] == "api"][-1]
        self.assertEqual((h["from"], h["to"]), ("blocked", "pending"))
        self.assertIn(why, h["why"])

    def test_deciding_for_a_member_who_ended_sends_his_step_back_for_a_new_launch(self):
        self.ended_tank(["api"], "Tank: api", assign="api", mode="guided")
        self.asks_d1(self.now - 15 * MIN)
        pg.crew_decide("D1", "B: reject", now=self.now)
        self.waits_for_a_new_launch("its owner Backend engineer (Tank) had ended: Claude Code recorded it finished")

    def test_deciding_for_a_member_who_went_stale_sends_his_step_back_for_a_new_launch(self):
        quiet = (approvals.limits()["stale_minutes"] + 20) * MIN
        self.stale_tank(["api"], "Tank: api", assign="api", mode="guided")
        self.asks_d1(self.now - quiet + MIN)  # his last word, long before the answer
        self.assertEqual(pg.lane_states(pg.load_run("claude", "s1"), self.now, own=True)["a_tank"]["state"], "stale")
        pg.crew_decide("D1", "B: reject", now=self.now)
        self.waits_for_a_new_launch("its owner Backend engineer (Tank) had gone stale: silent for")

    def test_withdrawing_the_decision_of_a_member_who_ended_tells_his_next_launch_and_the_tech_lead(self):
        self.ended_tank(["api"], "Tank: api", assign="api", mode="guided")
        self.asks_d1(self.now - 15 * MIN)
        pg.crew_withdraw("D1", "the export keeps empty lines", now=self.now)
        self.waits_for_a_new_launch("its owner Backend engineer (Tank) had ended: Claude Code recorded it finished")
        told = self.lead_mail()
        self.assertIn("Backend engineer (Tank) ended: Claude Code recorded it finished", told)
        self.assertIn("waits for their next launch: launch them again (dev crew brief tank", told)
        launch("a_tank2", "devkit:tank", "Tank: api", self.now + MIN)
        self.assertIn("D1 was withdrawn by the tech lead: the export keeps empty lines", self.mail_of("a_tank2"))

    def test_withdrawing_the_decision_of_a_member_whose_lane_was_lost_tells_the_launch_that_takes_his_step(self):
        # his lane went quiet while his step waited on D1 (the run closed it as lost): withdrawing D1 ran the step
        # again under that lost lane, and told nobody
        pg.start("Export", plan=["api"], now=self.now - MIN, mode="guided")
        pg.assign("api", "Tank", scope="lib/api/**", now=self.now - MIN)
        launch("a_tank", "devkit:tank", "Tank: api", self.now - MIN)
        self.asks_d1(self.now)
        later = self.now + (pg.cfg()["stale_agent_hours"] + 1) * 3600
        run = pg.load_run("claude", "s1")
        self.assertTrue(pg.expire_stale(run, now=later))
        pg._save(run, touch_session=False)
        pg.crew_withdraw("D1", "the export keeps empty lines", now=later)
        self.waits_for_a_new_launch("its owner Backend engineer (Tank) had ended: its lane is lost")
        self.assertIn("Backend engineer (Tank) ended: its lane is lost", self.lead_mail())
        launch("a_tank2", "devkit:tank", "Tank: api", later + MIN)
        self.assertIn("D1 was withdrawn by the tech lead: the export keeps empty lines", self.mail_of("a_tank2"))

    def test_a_step_decided_moments_after_its_member_ended_stays_with_the_tech_lead_at_the_next_hook(self):
        # within approvals' finish_grace the reconciler still waits for his SubagentStop and counts his lane as
        # working: a step marked as lost from that lane would go back to running under it at the tech lead's next
        # tool call, and end unverified with the lane once the grace is over
        pg.start("Export", plan=["api"], now=self.now - 20 * MIN, mode="guided")
        pg.assign("api", "Tank", scope="lib/api/**", now=self.now - 20 * MIN)
        launch("a_tank", "devkit:tank", "Tank: api", self.now - 20 * MIN)
        self.asks_d1(self.now - 2 * MIN)
        self.claude_meta("s1", "a_tank", finished=True, at=self.now - 30, log_at=self.now - 31)
        pg.crew_decide("D1", "B: reject", now=self.now)
        pg.reconcile_hook("claude", {"session_id": "s1"}, now=self.now + 10)
        self.waits_for_a_new_launch("its owner Backend engineer (Tank) had ended: Claude Code recorded it finished")


class StepOfAMemberWhoEndedTests(EndedTankCase):
    # a step whose owner ended or went stale closes with the tech lead's evidence, like one of his own, and the run's
    # history says its owner had ended (a step whose owner still works is still refused: test_progress,
    # test_a_subagents_step_is_not_closed_from_outside)
    def test_a_step_whose_owner_ended_closes_with_the_tech_leads_evidence(self):
        self.ended_tank(["export api"], "Tank: export api", assign="export api")
        step = pg.load_run("claude", "s1")["plan"][0]
        self.assertEqual((step["owner"], step["state"]), ("a_tank", "running"))
        run = pg.step("export api endpoint returns the CSV", which="export api", check="tests.test_export: 4 OK",
                      now=self.now)
        self.assertEqual(run["plan"][0]["state"], "done")
        h = run["history"][-1]
        self.assertEqual((h["step"], h["from"], h["to"], h["by"]), ("export api", "running", "done", "main"))
        self.assertIn("its owner Backend engineer (Tank) had ended: Claude Code recorded it finished", h["why"])
        self.assertIn("tests.test_export: 4 OK", h["why"])

    def test_a_step_whose_owner_went_stale_closes_with_the_tech_leads_evidence(self):
        self.stale_tank(["export api"], "Tank: export api", assign="export api")
        run = pg.step("export api endpoint returns the CSV", which="export api", check="tests.test_export: 4 OK",
                      now=self.now)
        self.assertEqual(run["plan"][0]["state"], "done")
        self.assertIn("its owner Backend engineer (Tank) had gone stale: silent for", run["history"][-1]["why"])


class RunEndWithAMemberWhoEndedTests(EndedTankCase):
    # dev progress done refused while any running step had a crew member as owner, even one that had ended or gone
    # stale (the tech lead, on the previous session's run): only a member that still works, or one devkit knows
    # nothing about, holds the run's end; a step whose owner ended closes with the run, and its history says so
    def end_history(self, run, name):
        return [h for h in run["history"] if h["step"] == name and h["to"] == "done"][-1]

    def test_the_run_ends_when_the_owner_of_its_running_step_ended(self):
        self.ended_tank(["export api"], "Tank: export api", assign="export api")
        run = pg.step("export shipped", kind="done", why="the review is out of this test", now=self.now)
        self.assertEqual((run["state"], run["plan"][0]["state"]), ("done", "done"))
        h = self.end_history(run, "export api")
        self.assertEqual((h["from"], h["by"]), ("running", "main"))
        self.assertIn("its owner Backend engineer (Tank) had ended: Claude Code recorded it finished", h["why"])
        self.assertIn("export shipped", h["why"])

    def test_the_run_ends_when_the_owner_of_its_running_step_went_stale(self):
        self.stale_tank(["export api"], "Tank: export api", assign="export api")
        run = pg.step("export shipped", kind="done", why="the review is out of this test", now=self.now)
        self.assertEqual((run["state"], run["plan"][0]["state"]), ("done", "done"))
        self.assertIn("its owner Backend engineer (Tank) had gone stale: silent for",
                      self.end_history(run, "export api")["why"])

    def test_only_a_member_that_works_or_that_devkit_knows_nothing_about_holds_the_run_end(self):
        # the Backend engineer (Tank) ended, the Senior engineer (Neo) works, and the csv step names a lane this run
        # has no record of: unknown, which counts as working
        self.ended_tank(["export api", "parser", "csv"], "Tank: export api", assign="export api")
        pg.assign("parser", "Neo", now=self.now - MIN)
        launch("a_neo", "devkit:neo", "Neo: parser", self.now - MIN)
        run = pg.load_run("claude", "s1")
        run["plan"][2].update(owner="a_gone", state="running", started=self.now - MIN)
        pg.run_path("claude", "s1").write_text(json.dumps(run))
        with self.assertRaises(pg.StepError) as e:  # neither --force nor --why ends a member's work
            pg.step("export shipped", kind="done", force=True, why="out of time", now=self.now)
        self.assertIn("“parser” by Senior engineer (Neo)", str(e.exception))
        self.assertIn("“csv”", str(e.exception))
        self.assertNotIn("export api", str(e.exception))
        run = pg.load_run("claude", "s1")
        self.assertEqual((run["state"], [s["state"] for s in run["plan"]]), ("running", ["running"] * 3))


class OwnRunTests(OwnerCase):
    # the caller's own run: the caller works in its session, so that session works even when its record is missing,
    # as scope_holders reads it (own=True); assign, delegate, a message and a step close read their own run so too
    # (Code reviewer, recheck)
    def working_tank_without_the_sessions_record(self):
        pg.start("Export", plan=["api", "csv"], now=self.now - 10 * MIN)
        pg.assign("api", "Tank", scope="lib/api/**", now=self.now - 10 * MIN)
        launch("a_tank", "devkit:tank", "Tank: api", self.now - 10 * MIN)
        pg.session_path("claude", "s1").unlink()

    def test_delegate_gives_the_step_to_this_sessions_working_member_when_the_sessions_record_is_missing(self):
        self.working_tank_without_the_sessions_record()
        r = dispatch.delegate("add a nullable column for the export date", to="Tank", now=self.now)
        self.assertEqual(r["lane"], "a_tank")

    def test_this_sessions_working_member_still_works_when_the_sessions_record_is_missing(self):
        self.working_tank_without_the_sessions_record()
        with self.assertRaises(pg.StepError) as e:
            pg.assign("csv", "Neo", scope="lib/api/export.py", now=self.now)
        self.assertIn("overlaps the files Backend engineer (Tank) owns while working", str(e.exception))
        self.assertEqual(pg.crew_ask("Add the export date", to="Tank", now=self.now)["to"], "a_tank")
        with self.assertRaises(pg.StepError) as e:
            pg.step("api endpoint done", which="api", now=self.now)
        self.assertIn("still working on it", str(e.exception))


class SweepTests(OwnerCase):
    def test_the_sweep_closes_a_lane_claude_code_recorded_finished_beside_the_conversations_newer_transcript(self):
        # tonight's old run: the sweep read Claude Code's records only beside its own session's transcript, where the
        # copy still says running; beside s1's transcript (the conversation's newer session id) it says finished
        self.other_run({"a_kid": self.lane("Kid", "Kid: branches", ["lib/branches/**"], active=25 * MIN)},
                       steps=[{"name": "branches", "state": "running", "owner": "a_kid"}])
        self.claude_meta("s2", "a_kid", finished=False, at=self.now - 2 * 3600, log_at=self.now - 25 * MIN - 1)
        self.claude_meta("s1", "a_kid", finished=True, at=self.now - 25 * MIN, log_at=self.now - 60 * MIN)
        approvals.sweep(now=self.now)
        run = pg.load_run("claude", "s2")
        self.assertEqual((run["agents"]["a_kid"]["state"], run["plan"][0]["state"]), ("unverified", "unverified"))
        why = " ".join(h.get("why", "") for h in run.get("history") or [] if h.get("step") == "(lane Kid)")
        self.assertIn("Claude Code recorded it finished", why)

    def test_the_sweep_keeps_a_member_that_works_beside_the_conversations_newer_transcript(self):
        # its run has not heard from it for longer than the stale minutes, but its own transcript, beside s1's, was
        # written a minute ago: it still works, so its lane stays running and keeps its step
        stale = approvals.limits()["stale_minutes"]
        self.other_run({"a_kid": self.lane("Kid", "Kid: branches", ["lib/branches/**"], active=(stale + 5) * MIN)},
                       steps=[{"name": "branches", "state": "running", "owner": "a_kid"}])
        self.claude_meta("s1", "a_kid", finished=False, at=self.now - 2 * 3600, log_at=self.now - MIN)
        approvals.sweep(now=self.now)
        run = pg.load_run("claude", "s2")
        self.assertEqual((run["agents"]["a_kid"]["state"], run["plan"][0]["state"]), ("running", "running"))


class WhoStillWorksCase(OwnerCase):
    def fixture(self):
        # s2's run lists three members as running: Kid works, Ghost went quiet past the stale minutes, and Claude
        # Code recorded the Keymaker finished beside s1's transcript
        stale = approvals.limits()["stale_minutes"]
        self.other_run({"a_kid": self.lane("Kid", "Kid: branches", ["lib/branches/**"], active=4 * MIN),
                        "a_ghost": self.lane("Ghost", "Ghost: docs", ["lib/docs/**"], active=(stale + 5) * MIN),
                        "a_key": self.lane("The Keymaker", "Keymaker: guard", ["lib/guard/**"], active=30 * MIN)},
                       steps=[{"name": "branches", "state": "running", "owner": "a_kid"},
                              {"name": "docs", "state": "running", "owner": "a_ghost"},
                              {"name": "guard", "state": "running", "owner": "a_key"}])
        self.claude_meta("s1", "a_key", finished=True, at=self.now - 30 * MIN, log_at=self.now - 30 * MIN - 1)
        return pg.load_run("claude", "s2")


class ProgressViewTests(WhoStillWorksCase):
    def test_dev_progress_counts_only_the_members_that_still_work(self):
        head = pg.headline(self.fixture(), self.now)
        self.assertTrue(head.endswith(" · 1 agent working · 1 agent stale"), head)

    def test_the_crew_slots_hold_only_the_members_that_still_work(self):
        self.assertEqual(pg.slots(self.fixture(), {}, self.now), [("a_kid", "Kid")])

    def test_the_crew_line_marks_stale_and_ended_members(self):
        line = pg.crew_summary(self.fixture(), now=self.now)
        parts = line.split(" · ")
        # the Keymaker's stop never reached the run, so no report did either: ended, not done
        self.assertEqual(parts[:3], ["DevEx engineer (Kid) ▸ branches", "Technical writer (Ghost) ✗ docs (stale)",
                                     "Security engineer (The Keymaker) ⚠ guard (ended, no report)"])
        self.assertRegex(parts[3], r"^1 of \d+ slots in use")

    def test_a_lane_with_no_recorded_activity_in_a_run_with_no_start_still_works(self):
        # nothing says when it last worked, and its run names no session: unknown, never stale (L-g0ilzimdm63)
        run = {"v": 2, "plan": [], "agents": {"a1": {"persona": "Neo", "label": "Neo: parser", "state": "running"}}}
        self.assertEqual([k for k, _ in pg.working_agents(run, self.now)], ["a1"])
        self.assertTrue(pg.headline(run, self.now).endswith(" · 1 agent working"), pg.headline(run, self.now))

    def test_dev_crew_next_names_only_the_members_that_still_work_as_busy(self):
        busy = crew.ready_view(self.fixture(), TEAMLESS(team=False))["busy"]
        self.assertEqual([b["who"] for b in busy], ["DevEx engineer (Kid)"])


class ContinuityTests(WhoStillWorksCase):
    def test_the_brief_after_a_compaction_lists_members_that_work_marks_stale_ones_and_names_ended_ones_apart(self):
        self.fixture()
        text = continuity.brief("claude", "s2", reason="resume", now=self.now)
        crew_part = text.split("Crew working now:\n", 1)[1].split("\nTheir results", 1)[0].splitlines()
        self.assertEqual(len(crew_part), 2, crew_part)
        self.assertTrue(crew_part[0].startswith("- DevEx engineer (Kid) · step branches"), crew_part)
        self.assertNotIn("stale", crew_part[0])
        self.assertTrue(crew_part[1].startswith("- Technical writer (Ghost) · step docs"), crew_part)
        self.assertIn("stale: silent for", crew_part[1])
        ended = next(ln for ln in text.splitlines() if ln.startswith("Ended since"))
        self.assertIn("Security engineer (The Keymaker) on step guard", ended)

    def test_the_pre_compact_record_keeps_only_the_members_that_still_work(self):
        run = self.fixture()
        text = continuity.on_pre_compact("claude", {"session_id": "s2", "cwd": str(self.repo), "trigger": "manual"},
                                         now=self.now)
        crew_line = next(ln for ln in text.splitlines() if ln.startswith("- the crew working now: "))
        self.assertIn("DevEx engineer (Kid) on step branches", crew_line)
        self.assertIn("Technical writer (Ghost) on step docs (stale)", crew_line)
        self.assertNotIn("Keymaker", crew_line)
        lanes = continuity.read("claude", "s2")["checkpoint"]["lanes"]
        self.assertEqual([(x["persona"], x["state"]) for x in lanes], [("Kid", "working"), ("Ghost", "stale")])
        self.assertEqual(run["agents"]["a_key"]["state"], "running")  # the record of the run itself is the sweep's


class DashboardTests(WhoStillWorksCase):
    def test_the_dashboard_shows_working_stale_and_ended_members_as_dev_status_does(self):
        # the brief (presence, dev crew) names each member's live state; the page draws a stale member as it draws
        # a lost one ("Stopped reporting"), and an ended one apart from "Working now"
        from devkit import dashboard
        run = self.fixture()
        self.assertEqual({a["label"]: (a["state"], a.get("live")) for a in pg.brief(run, self.now)["agents"]},
                         {"Kid: branches": ("running", "working"), "Ghost: docs": ("stale", "stale"),
                          "Keymaker: guard": ("ended", "ended")})
        (b,) = dashboard._runs(self.now, raw=[run])
        self.assertEqual([(a["label"], a["state"], sorted(a)) for a in b["agents"]],
                         [("Kid: branches", "running", ["label", "min", "state"]),
                          ("Ghost: docs", "lost", ["label", "min", "state"]),
                          ("Keymaker: guard", "ended", ["label", "min", "state"])])
        self.assertTrue(b["headline"].endswith(" · 1 agent working · 1 agent stale"), b["headline"])
        lanes = dashboard.run_detail(dashboard.run_id(run), now=self.now)["lanes"]
        self.assertEqual({x["persona"]: x["state"] for x in lanes},
                         {"Kid": "running", "Ghost": "lost", "The Keymaker": "ended"})

    def test_another_macs_stale_member_shows_as_stopped_reporting(self):
        # a brief published by another Mac names the lane state "stale"; the Now page draws it as "lost"
        from devkit import dashnow
        b = dashnow._remote_brief({"goal": "Branch cleanup", "state": "running",
                                   "agents": [{"label": "Ghost: docs", "state": "stale", "min": 5},
                                              {"label": "Kid: branches", "state": "running", "min": 3}]}, self.now)
        self.assertEqual([(a["label"], a["state"]) for a in b["agents"]],
                         [("Ghost: docs", "lost"), ("Kid: branches", "running")])


if __name__ == "__main__":
    unittest.main()
