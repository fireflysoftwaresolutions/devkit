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
from devkit import approvals, cli, continuity, core, crew, guard, hooks, progress as pg, work  # noqa: E402
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
            "goal": "Branch cleanup", "state": "running", "agent": "claude", "session": "s2", "project": "acme/api",
            "started": self.now - 3 * 3600, "plan": list(steps), "agents": lanes, "history": []}))

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
    def test_carry_closes_ended_and_stale_lanes_with_the_reason_and_takes_the_run(self):
        stale = approvals.limits()["stale_minutes"]
        self.other_run({"a_kid": self.lane("Kid", "Kid: branches", ["lib/branches/**"], active=25 * MIN),
                        "a_ghost": self.lane("Ghost", "Ghost: docs", ["lib/docs/**"], active=(stale + 5) * MIN)},
                       steps=[{"name": "branches", "state": "running", "owner": "a_kid"},
                              {"name": "docs", "state": "running", "owner": "a_ghost"}])
        self.claude_meta("s1", "a_kid", finished=True, at=self.now - 25 * MIN, log_at=self.now - 26 * MIN)
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
        with self.assertRaises(ValueError) as e:
            continuity.carry("claude", "s2", "s1", now=self.now)
        self.assertIn("Kid still running", str(e.exception))
        self.assertNotIn("Keymaker", str(e.exception))
        self.assertEqual(pg.load_run("claude", "s2")["agents"]["a_kid"]["state"], "running")


if __name__ == "__main__":
    unittest.main()
