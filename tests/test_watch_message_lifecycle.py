# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Unanswered questions remain actionable when a Codex lane leaves unfinished work.
import copy
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import crew, progress as pg, state, watch
from tests.test_watch import WatchCase


class UnfinishedQuestionTests(WatchCase):
    def test_codex_late_answer_stays_with_its_child_instead_of_another_same_role_task(self):
        self.run_with_member()
        original = pg.load_run("claude", "s1")
        session = state.read(pg.session_path("claude", "s1"))
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent), mock.patch.dict(os.environ, {"DEVKIT_SESSION": agent + ":s1"}):
                run = dict(copy.deepcopy(original), agent=agent, messages=[])
                state.update(pg.session_path(agent, "s1"), lambda d: d.update(dict(session, agent=agent)))
                state.update(pg.run_path(agent, "s1"), lambda d: d.update(run))
                pg.crew_ask("Review my completed parser diagnosis", from_agent="a1", now=self.t0 + 10)
                run = pg.load_run(agent, "s1")
                run["agents"]["a2"] = dict(run["agents"]["a1"], label="Tank: tests", started=self.t0 + 20)
                run["agents"]["a1"].update(state="done", finished=self.t0 + 15)
                run["plan"][0]["state"] = "unverified"
                run["plan"][1].update(owner="a2", state="running", started=self.t0 + 20)
                state.update(pg.run_path(agent, "s1"), lambda d: d.update(run))
                answer = pg.crew_answer("M1", "The parser diagnosis is accepted", now=self.t0 + 30)
                expected = "a1" if agent == "codex" else "a2"
                self.assertEqual(answer["to"], expected)
                delivered = pg._take_messages(agent, "s1", expected)
                self.assertEqual([m.get("reply_to") for m in delivered], ["M1"])
                self.assertEqual(pg._take_messages(agent, "s1", "a2" if agent == "codex" else "a1"), [])

    def test_codex_interruption_keeps_question_with_the_work_returned_to_the_lead(self):
        self.run_with_member()
        run = dict(pg.load_run("claude", "s1"), agent="codex", messages=[])
        session = state.read(pg.session_path("claude", "s1"))
        state.update(pg.run_path("codex", "s1"), lambda d: d.update(run))
        state.update(pg.session_path("codex", "s1"), lambda d: d.update(dict(session, agent="codex")))
        with mock.patch.dict(os.environ, {"DEVKIT_SESSION": "codex:s1"}):
            pg.crew_ask("May I edit the parser to finish api?", from_agent="a1", now=self.t0 + 60)
            pg.lane_stopped("a1", "Interrupted before scope was granted", agent="codex", sid="s1", now=self.t0 + 90)
            run = pg.load_run("codex", "s1")
            self.assertEqual((run["plan"][0]["state"], run["plan"][0]["owner"]), ("pending", "main"))
            self.assertEqual([m["id"] for m in pg.waiting_messages(run)], ["M1"])
            question = run["messages"][0]
            self.assertEqual(question["steps"], ["api"])
            for finished in ("done", "skipped"):
                run["plan"][0]["state"] = finished
                self.assertTrue(pg.is_moot_message(run, question))
                self.assertEqual(pg.waiting_messages(run), [])

    def test_codex_pending_work_keeps_question_open_until_done_or_skipped(self):
        question = {"id": "M1", "from": "a1", "to": "main", "kind": "question", "text": "Need scope"}
        for agent in ("claude", "codex"):
            for status in ("pending", "unverified", "running", "blocked", "done", "skipped"):
                with self.subTest(agent=agent, status=status):
                    run = {"agent": agent, "state": "running", "messages": [question],
                           "agents": {"a1": {"state": "done", "persona": "Neo"}},
                           "plan": [{"name": "parser", "owner": "a1", "state": status}]}
                    expected = status in ("done", "skipped") if agent == "codex" else status not in ("running", "blocked")
                    self.assertEqual(pg.is_moot_message(run, question), expected)

    def test_codex_question_survives_an_unverified_report_and_one_time_delivery(self):
        self.run_with_member()
        original = pg.load_run("claude", "s1")
        session = state.read(pg.session_path("claude", "s1"))
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent), mock.patch.dict(os.environ, {"DEVKIT_SESSION": agent + ":s1"}):
                run = dict(copy.deepcopy(original), agent=agent, messages=[])
                state.update(pg.session_path(agent, "s1"), lambda d: d.update(dict(session, agent=agent)))
                state.update(pg.run_path(agent, "s1"), lambda d: d.update(run))
                pg.crew_ask("May I edit the parser to finish api?", from_agent="a1", now=self.t0 + 60)
                report = ("**[Firefly> DevKit · Backend engineer (Tank)]** acme/api · `main` · api\n"
                          "- **Done:** api remains blocked on parser scope\n"
                          "- **Check:** ⚠ two regressions remain\n"
                          "- **Next:** the tech lead answers the scope question")
                pg.on_subagent(agent, {"session_id": "s1", "agent_id": "a1", "agent_type": "devkit:tank",
                                      "last_assistant_message": report}, False, now=self.t0 + 120)
                run = pg.load_run(agent, "s1")
                self.assertEqual(run["plan"][0]["state"], "unverified")
                events, _ = watch._messages(agent, "s1", "main", True, lambda e: True, 10, emit=lambda s: None)
                question = next(e for e in events if e["id"] == "M1")
                if agent == "claude":
                    self.assertIn("moot", question["line"])
                    self.assertFalse(question["wakes"])
                    self.assertNotIn("steps", run["messages"][0])
                    continue
                self.assertNotIn("moot", question["line"])
                self.assertTrue(question["wakes"])
                run = pg.load_run(agent, "s1")
                self.assertTrue(run["messages"][0]["delivered"])
                self.assertEqual([m["id"] for m in pg.waiting_messages(run)], ["M1"])
                self.assertEqual([m["id"] for m in crew.open_messages(run)], ["M1"])
                self.assertEqual(pg._take_messages(agent, "s1", "main"), [])
                pg.crew_answer("M1", "Parser scope is assigned; resume api", now=self.t0 + 180)
                self.assertEqual(pg.waiting_messages(pg.load_run(agent, "s1")), [])
