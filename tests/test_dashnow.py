# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The dashboard's Now data (spec P-fyli4qqn0y7): the Needs-you queue and its headline, landing times, safety, the
# since line and as_of, computed once in Python by lib/devkit/dashnow.py, and the copy commands quoted. Neutral data
# from tests/dashfixture.py at a fixed clock, read in UTC; the suite's hermetic home.
# Run: python3 -m unittest tests.test_dashnow
import copy
import gzip
import http.client
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import dashfixture as fx  # noqa: E402
import now_contract  # noqa: E402
from devkit import dashboard, dashserver, progress as pg  # noqa: E402

try:  # `dev test prove` runs these tests on the code before dashnow.py existed: there each must fail, not vanish
    from devkit import dashnow  # noqa: E402
except ImportError:
    dashnow = None

NOW, HOST, at = fx.NOW, fx.HOST, fx.at
PATH = f"{fx.DEV}/acme/lending-core"
HOSTILE = ("fix/x$(touch${IFS}pwn)", "fix/a`id`")
TOKEN = "now-token-0123456789abcdefghijklmnopqrstuv"
_UTC = fx.utc()


def setUpModule():
    _UTC.__enter__()


def tearDownModule():
    _UTC.__exit__(None, None, None)


def memo():
    return dashnow.LandingMemo(clock=lambda: NOW)


def with_runs(kw, *runs):
    kw["raw_runs"] = list(kw["raw_runs"]) + list(runs)
    kw["briefs"] = dashboard._runs(NOW, HOST, raw=copy.deepcopy(kw["raw_runs"]))
    return kw


def areas(kw):
    return dashnow.areas(**kw, memo=memo())


def ids(doc):
    return [i["id"] for i in doc["attention"]["items"]]


def row(doc, rid):
    return next(i for i in doc["attention"]["items"] if i["id"] == rid)


def statement_run(**kw):
    plan = kw.pop("plan", [fx.step("import", "running", 60, started=NOW - 1200)])
    return fx.raw_run(kw.pop("session", "9c1d"), kw.pop("goal", "Statement import"), "acme/lending-core", plan,
                      started=kw.pop("started", NOW - 1200), **kw)


def shell(cmd, cwd):
    # the copied command, pasted into a shell where `dev` only prints its arguments
    out = subprocess.run(["/bin/sh", "-c", 'dev() { printf "%s\\n" "$@"; }; ' + cmd], cwd=cwd, capture_output=True,
                         text=True, timeout=10)
    return out.stdout.splitlines()


class QueueTests(unittest.TestCase):
    def test_queue_orders_by_rank_then_lanes_then_oldest_then_id(self):
        d2 = fx.decision("D2", "Keep the old parser?", ["A: Keep it", "B: Drop it"], "A", at(13, 0))
        d1 = fx.decision("D1", "Split the import?", ["A: Split", "B: One file"], "A", at(14, 0))
        kw = with_runs(fx.example(), statement_run(decisions=[d1, d2]))
        kw["jobs"][0].update(state="failed", last={"start": at(6, 0), "end": at(6, 1), "rc": 1, "duration": 40,
                                                   "summary": "2 checks failed"})
        self.assertEqual(ids(areas(kw)), ["decision:claude-7f3a:D1", "decision:claude-9c1d:D2", "decision:claude-9c1d:D1",
                                          "ci_failed:acme/lending-rules:feat/rules-engine", "job_failed:doctor",
                                          "pr:acme/lending-core#45", "housekeeping"])

    def test_pr_row_is_dropped_when_a_ci_failed_row_has_its_branch(self):
        kw = fx.example()
        self.assertTrue(any(a["kind"] == "ci-failing" for a in kw["actions"]))
        self.assertNotIn("pr:acme/lending-rules#51", ids(areas(kw)))
        kw["ci"] = {"watches": [], "complete": True, "total": 0, "watching": []}
        pr = row(areas(kw), "pr:acme/lending-rules#51")
        self.assertEqual((pr["rank"], pr["tone"], pr["title"], pr["clause"]),
                         (5, "alert", "PR #51 is failing CI", "PR #51 is failing CI."))

    def test_pr_row_carries_the_command_and_the_forge_link(self):
        pr = row(fx.document("example"), "pr:acme/lending-core#45")
        self.assertEqual({k: pr[k] for k in ("tone", "lines", "copy", "open", "url", "since")},
                         {"tone": "ink", "lines": ["refactor/ledger-ports · lending-core"],
                          "copy": "gh pr view 45 -R acme/lending-core --comments",
                          "open": {"kind": "pr", "id": "acme/lending-core#45", "anchor": None},
                          "url": "https://github.com/acme/lending-core/pull/45", "since": at(13, 40)})

    def test_housekeeping_is_one_row_that_counts_its_items(self):
        hk = row(fx.document("example"), "housekeeping")
        self.assertEqual({k: hk[k] for k in ("rank", "tone", "count", "title", "clause", "lines", "copy", "open", "since")},
                         {"rank": 6, "tone": "ink", "count": 4, "title": "Housekeeping", "clause": "4 housekeeping items.",
                          "lines": ["2 finished worktrees, 1 expired lock, 1 note behind the code"], "copy": None,
                          "open": {"kind": "all", "id": "housekeeping", "anchor": None}, "since": None})

    def test_decision_row_names_the_asker_options_and_words_to_type_and_has_no_copy(self):
        self.assertEqual(row(fx.document("example"), "decision:claude-7f3a:D1"), {
            "id": "decision:claude-7f3a:D1", "rank": 1, "kind": "decision", "tone": "you", "count": 1,
            "title": "D1@7f3a · Include cancelled invoices in the export?",
            "clause": "Invoice export is paused until you answer D1@7f3a.",
            "lines": ["Backend engineer (Tank) asks · pauses Invoice export, 2 crew members wait"], "since": at(15, 6),
            "project": "acme/lending-core", "run_id": "claude-7f3a", "asker": "Backend engineer (Tank)",
            "blocks": {"run": "Invoice export", "lanes": 2}, "irreversible": None,
            "options": [{"key": "A", "text": "Leave them out", "recommended": True},
                        {"key": "B", "text": "Include them", "recommended": False}],
            "type_to_answer": ["approve D1@7f3a", "approve D1@7f3a: B"], "copy": None,
            "open": {"kind": "run", "id": "claude-7f3a", "anchor": "D1"}, "url": None})

    def test_blocked_lanes_count_crew_lanes_downstream_of_the_decision_but_not_the_lead(self):
        plan = [fx.step("a", "blocked", 10, owner="x", started=NOW - 600, waiting="D1"),
                fx.step("b", owner="y", after=["a"]), fx.step("c", owner="z", after=["b"]),
                fx.step("d", owner="main", after=["c"]), fx.step("e", owner="w"), fx.step("f", owner="x", after=["b"])]
        run = statement_run(plan=plan, decisions=[fx.decision("D1", "Go?", ["A: yes", "B: no"], "A", NOW - 300,
                                                              frm="x", from_name="Neo")])
        it = row(areas(with_runs(fx.welcome(), run)), "decision:claude-9c1d:D1")
        self.assertEqual((it["blocks"], it["lines"]), ({"run": "Statement import", "lanes": 3},
                                                       ["Senior engineer (Neo) asks · pauses Statement import, "
                                                        "3 crew members wait"]))

    def test_grant_row_shows_the_irreversible_action_and_the_grant_line(self):
        d = fx.decision("D1", "Merge PR 142 now?", ["yes", "no"], "yes", at(15, 2), irreversible="merge",
                        target="142")
        it = row(areas(with_runs(fx.welcome(), statement_run(decisions=[d]))), "decision:claude-9c1d:D1")
        self.assertEqual({k: it[k] for k in ("rank", "kind", "tone", "title", "lines", "irreversible", "type_to_answer",
                                             "copy", "options")},
                         {"rank": 1, "kind": "grant", "tone": "you", "title": "Merge PR 142 waits for your grant",
                          "lines": ["Tech lead (Morpheus) asked at 15:02 for Statement import.", "Merge PR 142 now?"],
                          "irreversible": "merge", "type_to_answer": ["grant merge 142"], "copy": None,
                          "options": [{"key": "yes", "text": "yes", "recommended": True},
                                      {"key": "no", "text": "no", "recommended": False}]})

    def test_a_branch_only_on_this_mac_enters_the_queue_after_4_hours(self):
        kw = fx.example()
        self.assertFalse([i for i in ids(areas(kw)) if i.startswith("backup:")])
        kw["reg"]["projects"][0]["branches"][1]["oldest_unpushed"] = at(11, 10)
        it = row(areas(kw), "backup:acme/lending-core:fix/rounding-error")
        self.assertEqual({k: it[k] for k in ("rank", "tone", "title", "clause", "lines", "since", "copy", "open")},
                         {"rank": 4, "tone": "caution", "title": "Only on this Mac · fix/rounding-error · lending-core",
                          "clause": "fix/rounding-error is only on this Mac.", "lines": ["3 commits only on this Mac"],
                          "since": at(11, 10),
                          "copy": f"cd -- {PATH} && dev backup -- fix/rounding-error",
                          "open": {"kind": "branches", "id": "acme/lending-core", "anchor": None}})

    def test_a_failed_job_and_a_signed_out_agent_rank_4(self):
        kw = fx.empty()
        kw["jobs"][0].update(state="failed", last={"start": at(6, 0), "end": at(6, 1), "rc": 1, "duration": 40,
                                                   "summary": "2 checks failed\nsee the log"})
        kw["reg"]["providers"]["claude"]["logged_in"] = False
        kw["actions"] = dashboard.build_actions(kw["reg"], NOW, kw["items"], [])
        doc = areas(kw)
        job, setup = row(doc, "job_failed:doctor"), row(doc, "setup:claude")
        self.assertEqual({k: job[k] for k in ("rank", "tone", "title", "clause", "lines", "since", "copy", "open")},
                         {"rank": 4, "tone": "alert", "title": "The doctor job failed at 06:00",
                          "clause": "The doctor job failed.", "lines": ["2 checks failed"], "since": at(6, 0),
                          "copy": "dev jobs show doctor", "open": {"kind": "job", "id": "doctor", "anchor": None}})
        self.assertEqual({k: setup[k] for k in ("rank", "tone", "title", "copy")},
                         {"rank": 4, "tone": "caution", "title": "Claude Code is not signed in", "copy": "claude"})

    def test_a_blocked_run_and_a_silent_run_rank_2(self):
        blocked = statement_run(state="blocked", updates=[{"ts": at(15, 0), "kind": "blocked",
                                                           "text": "the staging database is down", "next": ""}])
        silent = statement_run(session="5e6f", goal="Fee report", started=at(14, 0),
                               updates=[{"ts": at(14, 30), "kind": "note", "text": "halfway", "next": ""}])
        doc = areas(with_runs(fx.welcome(), blocked, silent))
        b, s = row(doc, "run_blocked:claude-9c1d"), row(doc, "run_stale:claude-5e6f")
        self.assertEqual((b["rank"], b["tone"], b["title"], b["lines"], b["since"], b["open"]),
                         (2, "alert", "Statement import is blocked", ["the staging database is down"], at(15, 0),
                          {"kind": "run", "id": "claude-9c1d", "anchor": None}))
        self.assertEqual((s["rank"], s["title"], s["lines"], s["since"]),
                         (2, "Fee report stopped reporting", ["Tech lead (Morpheus) sent no update since 14:30."],
                          at(14, 30)))

    def test_an_option_without_a_letter_is_typed_whole_on_one_line(self):
        long = ("Ship the export behind a feature flag for the finance team first, then open it to everyone after the "
                "month-end close ran")
        d = fx.decision("D1", "How do we ship it?", [long, "Wait for\nthe audit"], "", NOW - 300)
        it = row(areas(with_runs(fx.welcome(), statement_run(decisions=[d]))), "decision:claude-9c1d:D1")
        self.assertEqual(len(long), 120)
        self.assertEqual(it["type_to_answer"], [f"approve D1@9c1d: {long}", "approve D1@9c1d: Wait for the audit"])

    def test_the_ci_copy_runs_in_the_projects_folder_and_needs_a_local_path(self):
        rid = "ci_failed:acme/lending-rules:feat/rules-engine"
        self.assertEqual(row(fx.document("example"), rid)["copy"],
                         f"cd -- {fx.DEV}/acme/lending-rules && dev run view 14 --failed")
        kw = fx.example()
        kw["reg"]["projects"][1]["path"] = "/tmp/a b $(touch pwn)"
        self.assertEqual(shlex.split(row(areas(kw), rid)["copy"]),
                         ["cd", "--", "/tmp/a b $(touch pwn)", "&&", "dev", "run", "view", "14", "--failed"])
        kw["reg"]["projects"][1]["path"] = None
        self.assertIsNone(row(areas(kw), rid)["copy"])

    def test_without_a_roster_the_main_session_is_the_tech_lead(self):
        d = fx.decision("D1", "Go?", ["A: yes", "B: no"], "A", NOW - 300, frm="main", from_name="")
        silent = statement_run(session="5e6f", goal="Fee report", started=at(14, 0),
                               updates=[{"ts": at(14, 30), "kind": "note", "text": "halfway", "next": ""}])
        kw = with_runs(fx.welcome(), statement_run(decisions=[d]), silent)
        kw["roster"] = None
        doc = areas(kw)
        self.assertEqual(row(doc, "decision:claude-9c1d:D1")["asker"], "The tech lead")
        self.assertEqual(row(doc, "run_stale:claude-5e6f")["lines"], ["The tech lead sent no update since 14:30."])

    def test_the_queue_is_capped_at_200_rows_and_counts_them_all(self):
        decs = [fx.decision(f"D{n}", f"Question {n}?", ["A: yes", "B: no"], "A", NOW - n) for n in range(1, 211)]
        doc = areas(with_runs(fx.welcome(), statement_run(decisions=decs)))
        self.assertEqual((len(doc["attention"]["items"]), doc["attention"]["total"]), (200, 210))
        self.assertEqual(doc["attention"]["headline"]["text"], "210 things need you")


class ResolvedTests(unittest.TestCase):
    # a row leaves the queue once the event that resolves it is recorded
    def test_a_later_green_run_on_the_same_branch_and_repo_clears_its_ci_failure(self):
        kw = fx.example()
        failed = next(w for w in kw["ci"]["watches"] if w["branch"] == "feat/rules-engine")
        kw["ci"]["watches"].insert(0, dict(failed, state="passed", ts=at(15, 10), failures=[]))
        self.assertNotIn("ci_failed:acme/lending-rules:feat/rules-engine", ids(areas(kw)))

    def test_a_decision_whose_asking_lane_ended_leaves_the_queue_and_shows_as_moot_on_its_run(self):
        # the Backend engineer's lane ended, and its step went back to the tech lead: nothing waits on D1 now
        run = fx.invoice_run()
        run["agents"]["a-tank"].update(state="done", finished=at(15, 10))
        run["plan"][3].update(state="pending", owner="main")
        kw = fx._inputs(fx.registry_example(), [run, fx.docs_run()], fx.items_example(), fx.ci_example(),
                        fx.jobs_example(), [], fx.pr_cache())
        doc = areas(kw)
        self.assertFalse([i for i in ids(doc) if i.startswith("decision:")])
        mine = next(r for r in doc["runs"] if r["id"] == "claude-7f3a")
        self.assertEqual((mine["waiting_on"], mine["decisions"]), (None, []))
        self.assertEqual(mine["moot"], [{"id": "D1@7f3a", "question": "Include cancelled invoices in the export?",
                                         "asker": "Backend engineer (Tank)", "since": at(15, 6),
                                         "text": "Moot: Backend engineer (Tank) ended, and no step waits on D1."}])

    def test_a_runs_questions_are_the_questions_still_waiting_for_an_answer(self):
        run = statement_run()
        run["messages"] = [
            {"id": "M1", "ts": at(15, 0), "from": "devkit", "to": "main", "text": "Neo started", "delivered": True,
             "from_name": "devkit", "to_name": "Morpheus", "kind": "notice"},
            {"id": "M2", "ts": at(15, 1), "from": "a1", "to": "main", "text": "May I touch the schema?",
             "delivered": True, "from_name": "Neo", "to_name": "Morpheus", "kind": "question"},
            {"id": "M3", "ts": at(15, 2), "from": "a1", "to": "main", "text": "Split the file?", "delivered": True,
             "from_name": "Neo", "to_name": "Morpheus", "kind": "question", "answered": True}]
        doc = areas(with_runs(fx.welcome(), run))
        mine = next(r for r in doc["runs"] if r["id"] == "claude-9c1d")
        self.assertEqual([q["id"] for q in mine["questions"]], ["M2@9c1d"])  # the page names each question's run


class HeadlineTests(unittest.TestCase):
    def headline(self, kw):
        return areas(kw)["attention"]["headline"]

    def test_needs_you_counts_ranks_1_to_4_and_names_the_next_clause(self):
        self.assertEqual(fx.document("example")["attention"]["headline"],
                         {"kind": "needs_you", "count": 2, "text": "2 things need you",
                          "line": "Invoice export is paused until you answer D1@7f3a. CI failed on feat/rules-engine."})

    def test_a_lone_decision_says_so(self):
        kw = fx.example()
        kw["ci"] = {"watches": [], "complete": True, "total": 0, "watching": []}
        self.assertEqual(self.headline(kw), {"kind": "needs_you", "count": 1, "text": "1 decision waits for you",
                                             "line": "Invoice export is paused until you answer D1@7f3a."})

    def test_a_lone_grant_says_grant(self):
        d = fx.decision("D1", "Merge PR 142 now?", ["yes", "no"], "yes", at(15, 2), irreversible="merge", target="142")
        self.assertEqual(self.headline(with_runs(fx.welcome(), statement_run(decisions=[d]))),
                         {"kind": "needs_you", "count": 1, "text": "1 grant waits for you",
                          "line": "Statement import is paused until you answer D1@9c1d."})

    # An escalation between two agent sessions waits for the developer like a decision: with nothing else waiting,
    # the headline never says "Nothing needs you" (the Between sessions area shows it with the line to type)
    def test_a_lone_escalation_between_sessions_needs_you(self):
        kw = fx.empty()
        kw["escalations"] = [{"id": "E-g3h1abc", "since": at(14, 0), "question": "Ship tonight?",
                              "type": "settle E-g3h1abc: B"}]
        self.assertEqual(self.headline(kw), {"kind": "needs_you", "count": 1, "text": "1 escalation waits for you",
                                             "line": "Two sessions wait for your choice on E-g3h1abc."})

    def test_an_escalation_counts_with_the_decisions_and_comes_first(self):
        kw = fx.example()
        kw["escalations"] = [{"id": "E-g3h1abc", "since": at(14, 0)}]
        self.assertEqual(self.headline(kw), {
            "kind": "needs_you", "count": 3, "text": "3 things need you",
            "line": "Two sessions wait for your choice on E-g3h1abc. Invoice export is paused until you answer D1@7f3a."})

    def test_urgent_ci_failure_names_the_branch_and_the_first_error(self):
        kw = fx.example()
        kw["raw_runs"], kw["briefs"] = [], []
        self.assertEqual(self.headline(kw), {"kind": "urgent", "count": 1, "text": "CI failed on feat/rules-engine",
                                             "line": "test · Run tests · AssertionError: 10.01 == 10.00"})

    def test_urgent_failed_job(self):
        kw = fx.empty()
        kw["jobs"][0].update(state="failed", last={"start": at(6, 0), "end": at(6, 1), "rc": 1, "duration": 40,
                                                   "summary": "2 checks failed"})
        self.assertEqual(self.headline(kw), {"kind": "urgent", "count": 1, "text": "The doctor job failed",
                                             "line": "2 checks failed"})

    def test_urgent_silent_and_blocked_runs(self):
        silent = statement_run(updates=[{"ts": at(14, 30), "kind": "note", "text": "halfway", "next": ""}],
                               started=at(14, 0))
        self.assertEqual(self.headline(with_runs(fx.empty(), silent))["text"], "Statement import stopped reporting")
        blocked = statement_run(state="blocked", updates=[{"ts": at(15, 0), "kind": "blocked",
                                                           "text": "the staging database is down", "next": ""}])
        self.assertEqual(self.headline(with_runs(fx.empty(), blocked)),
                         {"kind": "urgent", "count": 1, "text": "Statement import is blocked",
                          "line": "the staging database is down"})

    def test_look_when_only_ranks_3_and_4_wait(self):
        kw = fx.empty()
        kw["reg"]["projects"][0]["branches"].append(fx._branch("fix/old", "not_backed_up", "2 commits only on this Mac",
                                                               2, at(9, 0)))
        self.assertEqual(self.headline(kw), {"kind": "look", "count": 1, "text": "Nothing urgent. 1 thing to look at.",
                                             "line": "fix/old is only on this Mac."})

    def test_clear_running_says_when_the_first_run_lands(self):
        kw = with_runs(fx.empty(), statement_run())
        kw["team"] = [fx.remote_presence()]
        self.assertEqual(self.headline(kw), {"kind": "clear_running", "count": 0, "text": "Nothing needs you",
                                             "line": "2 runs are going. The first lands about 15:40."})

    def test_clear_idle_names_the_next_task_and_lists_next_up(self):
        a = fx.document("empty")["attention"]
        self.assertEqual(a["headline"], {"kind": "clear_idle", "count": 0, "text": "Nothing needs you",
                                         "line": "Nothing is running. Next in lending-core: Document the export endpoint."})
        self.assertEqual(a["next_up"], [
            {"project": "acme/lending-core", "task_id": "T-fz1doc", "title": "Document the export endpoint",
             "copy": "dev task -p acme/lending-core start T-fz1doc"},
            {"project": "acme/lending-rules", "task_id": "T-fz2rate", "title": "Add the rate table",
             "copy": "dev task -p acme/lending-rules start T-fz2rate"}])

    def test_welcome_without_projects_lists_the_setup(self):
        a = fx.document("welcome")["attention"]
        self.assertEqual(a["headline"], {"kind": "welcome", "count": 0, "text": "Welcome to devkit",
                                         "line": "Nothing is tracked yet. Clone a project with dev clone <owner/repo>, "
                                                 "or start one with dev new <name>."})
        self.assertEqual([(s["key"], s["done"]) for s in a["setup"]], [("agents", True), ("project", False),
                                                                        ("sync", True)])
        self.assertEqual(fx.document("example")["attention"]["setup"], [])

    def test_the_welcome_setup_rows_do_not_repeat_the_headline(self):
        setup = fx.document("welcome")["attention"]["setup"]
        self.assertEqual([(s["key"], s["text"], s["copy"]) for s in setup],
                         [("agents", "An agent is signed in", None), ("project", "Add your first project", None),
                          ("sync", "The first sync ran", None)])


class LandingTests(unittest.TestCase):
    def test_a_run_waiting_on_a_decision_lands_after_the_answer_without_the_wait(self):
        # 10 of the blocked step's 30 minutes are done: 20 + 15 + 5 = 40 minutes left, x0.68 and x1.4, to 5 min
        self.assertEqual(dashnow.landing(fx.invoice_run(), NOW),
                         {"state": "after_answer", "basis": "critical_path", "remaining_s": {"low": 1500, "high": 3600},
                          "lands_at": None, "over_estimate": None, "text": "Lands 25–60 min after you answer D1@7f3a"})

    def test_half_an_hour_of_waiting_leaves_the_range_unchanged(self):
        self.assertEqual(dashnow.landing(fx.invoice_run(), NOW + 1800)["remaining_s"], {"low": 1500, "high": 3600})

    def test_a_running_run_lands_at_its_eta_range_rounded_to_5_minutes(self):
        # 20 of 60 minutes done: 40 left, 27.2-56 min from 15:15 -> 15:40-16:15
        self.assertEqual(dashnow.landing(statement_run(), NOW),
                         {"state": "clock", "basis": "critical_path", "remaining_s": {"low": 1500, "high": 3600},
                          "lands_at": {"low": at(15, 40), "high": at(16, 15)}, "over_estimate": None,
                          "text": "Lands 15:40–16:15"})

    def test_a_run_over_one_and_a_half_times_its_estimate_says_so(self):
        run = statement_run(plan=[fx.step("import", "running", 120, started=NOW - 170 * 60)], started=NOW - 170 * 60,
                            estimate=120, updates=[{"ts": NOW - 120, "kind": "note", "text": "rows 9 of 10", "next": ""}])
        self.assertEqual(dashnow.landing(run, NOW),
                         {"state": "clock", "basis": "critical_path", "remaining_s": {"low": 600, "high": 1800},
                          "lands_at": {"low": at(15, 25), "high": at(15, 45)},
                          "over_estimate": {"estimate_s": 7200, "now_s": 11280},
                          "text": "Lands 15:25–15:45, later than planned (estimate 2 h, now about 3 h 10)."})

    def test_a_run_without_estimates_has_no_landing_time(self):
        run = statement_run(plan=[], estimate=0)
        self.assertEqual(dashnow.landing(run, NOW),
                         {"state": "none", "basis": None, "remaining_s": None, "lands_at": None, "over_estimate": None,
                          "text": "No estimate, so no landing time."})

    def test_a_silent_run_has_an_unknown_landing_time(self):
        run = statement_run(started=at(14, 0), updates=[{"ts": at(14, 40), "kind": "note", "text": "x", "next": ""}])
        self.assertEqual((dashnow.landing(run, NOW)["state"], dashnow.landing(run, NOW)["text"]),
                         ("unknown", "The landing time is unknown."))
        self.assertEqual(dashnow.waiting_on(run, NOW), {"kind": "stale", "ref": None, "since": at(14, 40)})

    def test_a_blocked_run_says_why_and_has_no_landing_time(self):
        run = statement_run(state="blocked", updates=[{"ts": at(15, 0), "kind": "blocked",
                                                       "text": "the staging database is down", "next": ""}])
        e = dashnow.landing(run, NOW)
        self.assertEqual((e["state"], e["remaining_s"], e["text"]),
                         ("blocked", None, "Blocked: the staging database is down. No landing time until it moves."))

    def test_a_finished_run_says_when_and_how_long(self):
        self.assertEqual(dashnow.landing(fx.docs_run(), NOW)["text"], "Finished 13:00 in 1 h 40")

    def test_another_machines_run_lands_from_its_presence_until_it_goes_quiet(self):
        run = next(r for r in fx.document("example")["runs"] if r["machine"])
        self.assertEqual({k: run[k] for k in ("goal", "project", "machine", "who", "seen", "waiting_on")},
                         {"goal": "Rules engine migration", "project": "acme/lending-rules", "machine": "studio",
                          "who": "bob@studio", "seen": at(15, 13), "waiting_on": None})
        self.assertRegex(run["id"], r"^remote-studio-codex-[0-9a-f]{4}$")
        self.assertEqual((run["eta"]["remaining_s"], run["eta"]["lands_at"], run["eta"]["text"]),
                         ({"low": 2400, "high": 3300}, {"low": at(15, 55), "high": at(16, 10)}, "Lands 15:55–16:10"))
        kw = fx.empty()
        kw["team"] = [fx.remote_presence(updated=at(14, 40))]
        quiet = areas(kw)["runs"][0]
        self.assertEqual((quiet["eta"]["state"], quiet["waiting_on"]), ("unknown", {"kind": "stale", "ref": None,
                                                                                     "since": at(14, 40)}))

    def test_runs_order_waiting_blocked_silent_running_then_done(self):
        blocked = statement_run(session="b1", state="blocked", started=at(14, 50),
                                updates=[{"ts": at(15, 0), "kind": "blocked", "text": "x", "next": ""}])
        silent = statement_run(session="s1", started=at(14, 0),
                               updates=[{"ts": at(14, 30), "kind": "note", "text": "x", "next": ""}])
        older = statement_run(session="r1", started=NOW - 3600, updates=[{"ts": NOW - 60, "kind": "note", "text": "x",
                                                                         "next": ""}])
        done = statement_run(session="d1", state="done", started=at(14, 0), finished=at(15, 0))
        kw = with_runs(fx.example(), blocked, silent, older, done)
        got = ["remote" if r["machine"] else r["id"] for r in areas(kw)["runs"]]
        self.assertEqual(got, ["claude-7f3a", "claude-b1", "claude-s1", "remote", "claude-r1", "claude-d1"])

    def test_a_remote_run_keeps_its_id_between_updates_and_two_sessions_get_two(self):
        p = fx.remote_presence()
        p["sessions"].append(dict(p["sessions"][0], started=at(14, 30)))
        kw = fx.empty()
        kw["team"] = [p]
        first = [r["id"] for r in areas(kw)["runs"]]
        kw["team"] = [fx.remote_presence(updated=at(15, 14))]
        self.assertEqual(len(set(first)), 2)
        self.assertIn(areas(kw)["runs"][0]["id"], first)

    def test_every_run_names_its_branch_and_this_macs_runs_null(self):
        runs = fx.document("example")["runs"]
        self.assertEqual([(r["machine"], r.get("branch", "<missing>")) for r in runs],
                         [(None, None), ("studio", "feat/rules-engine")])

    def test_this_macs_own_presence_adds_no_second_copy_of_its_run(self):
        runs = fx.document("example")["runs"]
        self.assertEqual([(r["goal"], r["machine"]) for r in runs],
                         [("Invoice export", None), ("Rules engine migration", "studio")])

    def test_runs_from_other_machines_are_capped_at_20(self):
        p = fx.remote_presence()
        p["sessions"] = [dict(p["sessions"][0], started=at(14, 25) + n) for n in range(25)]
        kw = fx.empty()
        kw["team"] = [p]
        self.assertEqual(len(areas(kw)["runs"]), 20)


class MemoTests(unittest.TestCase):
    def test_the_same_key_is_computed_once_within_60_seconds(self):
        t, calls = [NOW], []
        m = dashnow.LandingMemo(ttl_s=60, clock=lambda: t[0])

        def compute():
            calls.append(t[0])
            return {"n": len(calls)}
        m.get("r", ("k",), compute)
        t[0] = NOW + 59
        self.assertEqual(m.get("r", ("k",), compute), {"n": 1})
        self.assertEqual(m.get("r", ("k2",), compute), {"n": 2})
        t[0] = NOW + 59 + 60
        self.assertEqual(m.get("r", ("k2",), compute), {"n": 3})

    def test_answering_the_decision_recomputes_the_landing_at_once(self):
        m = memo()
        kw = fx.example()
        first = next(r for r in dashnow.areas(**kw, memo=m)["runs"] if r["id"] == "claude-7f3a")
        run = kw["raw_runs"][0]
        run["decisions"][0].update(state="decided", answer="A", by="developer", decided=NOW)
        run["plan"][3].update(state="running")
        run["plan"][3].pop("waiting")
        second = next(r for r in dashnow.areas(**kw, memo=m)["runs"] if r["id"] == "claude-7f3a")
        self.assertEqual((first["eta"]["state"], second["eta"]["state"]), ("after_answer", "clock"))


class AreaTests(unittest.TestCase):
    def test_safety_lists_the_projects_at_risk_with_totals_and_one_line(self):
        self.assertEqual(fx.document("example")["safety"], {
            "totals": {"not_backed_up": 1, "embargoed": 1, "waiting": 1, "unknown_projects": 0},
            "line": {"tone": "caution", "text": "1 branch is only on this Mac: lending-core fix/rounding-error, 3 commits",
                     "oldest": at(13, 15), "open": {"kind": "branches", "id": None, "anchor": None}},
            "projects": [{"project": "acme/lending-core", "not_backed_up": 1, "embargoed": 1, "waiting": 1,
                          "oldest": at(13, 15), "mapped_at": fx.GENERATED, "rows": [
                              {"branch": "fix/rounding-error", "state": "not_backed_up",
                               "reason": "3 commits only on this Mac", "commits": 3, "oldest": at(13, 15),
                               "copy": f"cd -- {PATH} && dev backup -- fix/rounding-error"},
                              {"branch": "hotfix/auth-header", "state": "embargoed",
                               "reason": 'embargoed until "the advisory is published"', "commits": 2,
                               "oldest": 1793960000, "copy": None},
                              {"branch": "feat/rate-limits", "state": "waiting",
                               "reason": "commits younger than 30 min wait for the backup", "commits": 1,
                               "oldest": 1793977000, "copy": None}]}]})

    def test_safety_line_in_plural_when_every_branch_is_out_and_without_a_map(self):
        kw = fx.empty()
        p1, p2 = kw["reg"]["projects"]
        p1["branches"].append(fx._branch("fix/a", "not_backed_up", "1 commit only on this Mac", 1, at(14, 0)))
        p2["branches"].append(fx._branch("spike/b", "no_remote", "no remote is configured", 2, at(13, 0)))
        s = areas(kw)["safety"]
        self.assertEqual((s["line"]["tone"], s["line"]["text"], s["line"]["oldest"]),
                         ("caution", "2 branches are only on this Mac, in 2 projects", at(13, 0)))
        spike = s["projects"][1]["rows"][0]
        self.assertEqual((spike["state"], spike["copy"]), ("not_backed_up", None))  # no remote: nothing to back up to
        self.assertEqual(areas(fx.empty())["safety"]["line"]["text"], "Every branch is on the remote.")
        kw = fx.empty()
        for p in kw["reg"]["projects"]:
            p["branches"] = None
        s = areas(kw)["safety"]
        self.assertEqual((s["line"]["tone"], s["line"]["text"], s["totals"]["unknown_projects"]),
                         ("unknown", "No branch map yet. dev sync makes one.", 2))

    def test_the_default_branch_gets_no_backup_command_since_dev_backup_never_pushes_it(self):
        kw = fx.empty()
        kw["reg"]["projects"][0]["branches"][0].update(
            backup={"state": "not_backed_up", "reason": "2 commits only on this Mac"}, unpushed=2,
            oldest_unpushed=at(9, 0))
        doc = areas(kw)
        safety_row, queue_row = doc["safety"]["projects"][0]["rows"][0], row(doc, "backup:acme/lending-core:main")
        self.assertEqual((safety_row["branch"], safety_row["state"], safety_row["copy"], queue_row["copy"]),
                         ("main", "not_backed_up", None, None))

    def test_with_no_projects_the_safety_line_says_there_is_nothing_to_back_up(self):
        self.assertEqual(fx.document("welcome")["safety"]["line"],
                         {"tone": "ok", "text": "No projects yet, so no branches to back up.", "oldest": None,
                          "open": {"kind": "branches", "id": None, "anchor": None}})

    def test_since_counts_every_task_done_and_pr_merged_past_the_lists_caps(self):
        core = "acme/lending-core"
        done = [fx.item(f"T-fzd{n:02d}", f"Task {n}", core, status="done", updated=at(10, 0) + n) for n in range(31)]
        cache = {core: [{"number": 100 + n, "state": "MERGED", "title": f"PR {n}", "author": fx.USER,
                         "url": f"https://github.com/{core}/pull/{100 + n}", "updatedAt": fx.iso(at(11, 0) + n)}
                        for n in range(21)]}
        since = dashboard.since_yesterday(fx.registry_empty(), cache, NOW, done, [], fx.ME)
        self.assertEqual((len(since["done"]), len(since["merged"]), since.get("counts")),
                         (30, 20, {"done": 31, "merged": 21}))
        kw = fx.empty()
        kw["since"] = since
        s = areas(kw)["since"]
        self.assertEqual((s["counts"], s["line"]["text"]), ({"done": 31, "merged": 21}, "31 tasks done · 21 PRs merged"))

    def test_since_line_lists_what_finished(self):
        self.assertEqual(fx.document("example")["since"]["line"], {
            "from": at(0, 0, 5), "text": "4 tasks done · 2 PRs merged · 1 run finished in 1 h 40 (estimate 2 h)",
            "items": [{"kind": "task_done", "count": 4, "text": "4 tasks done"},
                      {"kind": "pr_merged", "count": 2, "text": "2 PRs merged"},
                      {"kind": "run_finished", "count": 1, "text": "1 run finished in 1 h 40 (estimate 2 h)"}]})

    def test_as_of_gives_each_slow_source_its_last_read_and_refresh(self):
        self.assertEqual(fx.document("example")["as_of"], {"registry": {"at": fx.GENERATED, "every_s": 60},
                                                           "prs": {"at": fx.PRS_AT, "every_s": 900}})

    def test_an_area_that_raises_becomes_an_error_and_the_others_still_publish(self):
        boom = RuntimeError("git said token=" + "ghp_" + "a1" * 20 + "\nand more")
        with mock.patch.object(dashnow, "_safety", side_effect=boom):
            doc = fx.document("example")
        self.assertEqual(set(doc["safety"]), {"error"})
        self.assertEqual(doc["safety"]["error"]["title"], "Safety could not be read")
        self.assertNotIn("a1a1a1", doc["safety"]["error"]["detail"])
        self.assertNotIn("\n", doc["safety"]["error"]["detail"])
        self.assertEqual(now_contract.check(doc), [])
        self.assertEqual(doc["attention"]["headline"]["kind"], "needs_you")


class HostileInputTests(unittest.TestCase):
    def kw_with_branches(self, *names):
        kw = fx.empty()
        for n in names:
            kw["reg"]["projects"][0]["branches"].append(fx._branch(n, "not_backed_up", "1 commit only on this Mac", 1,
                                                                   at(14, 0)))
        return kw

    def test_branch_names_with_shell_syntax_are_quoted_in_the_copied_command(self):
        rows = areas(self.kw_with_branches(*HOSTILE))["safety"]["projects"][0]["rows"]
        with tempfile.TemporaryDirectory() as tmp:
            for r in rows:
                cmd = r["copy"].replace(f"cd -- {PATH}", f"cd -- {shlex.quote(tmp)}")
                self.assertEqual(shell(cmd, tmp), ["backup", "--", r["branch"]])
            self.assertEqual(os.listdir(tmp), [])

    def test_a_value_with_a_newline_gets_no_copy(self):
        rows = areas(self.kw_with_branches("fix/a\nrm -rf x"))["safety"]["projects"][0]["rows"]
        self.assertEqual([r["copy"] for r in rows], [None])

    def test_links_other_than_https_on_the_projects_forge_are_dropped(self):
        for url in ("javascript:alert(1)", "http://github.com/acme/lending-core/pull/45",
                    "https://evil.example/acme/lending-core/pull/45"):
            kw = fx.example()
            kw["reg"]["projects"][0]["prs_open"][0]["url"] = url
            kw["actions"] = dashboard.build_actions(kw["reg"], NOW, kw["items"], dashboard.collisions([], kw["items"], NOW))
            self.assertIsNone(row(areas(kw), "pr:acme/lending-core#45")["url"], url)

    def test_presence_from_another_machine_cannot_create_a_queue_row(self):
        p = fx.remote_presence()
        p["sessions"][0]["progress"].update(state="blocked", decisions=[{"id": "D1", "state": "pending"}])
        kw = fx.empty()
        kw["team"] = [p]
        doc = areas(kw)
        self.assertEqual((doc["attention"]["items"], doc["runs"][0]["machine"]), ([], "studio"))

    def test_a_remote_number_that_is_no_real_time_drops_only_that_value(self):
        # another Mac's presence, synced through the handoff repository: NaN, 1e308 and an instant 3 billion years out
        cases = {"eta_hi 1e308": (dict(eta_lo=10, eta_hi=1e308), "none", "No estimate, so no landing time."),
                 "eta_lo NaN": (dict(eta_lo=json.loads("NaN"), eta_hi=20), "none", "No estimate, so no landing time."),
                 "done, updated 1e20": (dict(state="done", updated=1e20), "done", "Finished 15:13 in 48 min")}
        for name, (change, state, text) in cases.items():
            with self.subTest(name):
                p = fx.remote_presence()
                p["sessions"][0]["progress"].update(change)
                self.assertTrue(dashboard.valid_presence(p))
                kw = fx.example()
                kw["team"] = [p]
                runs = areas(kw)["runs"]
                self.assertIsInstance(runs, list, runs)
                self.assertEqual([r["id"] for r in runs if not r["machine"]], ["claude-7f3a"])
                remote = next(r for r in runs if r["machine"])
                self.assertEqual((remote["eta"]["state"], remote["eta"]["text"]), (state, text))
                self.assertNotRegex(json.dumps(runs), r"\bNaN\b|\bInfinity\b")  # the page's JSON.parse refuses them

    def test_a_remote_session_that_breaks_skips_only_itself(self):
        p = fx.remote_presence()
        broken = dict(p["sessions"][0]["progress"], goal="Broken")
        p["sessions"].append(dict(p["sessions"][0], started=at(14, 30), progress=broken))
        kw = fx.example()
        kw["team"] = [p]
        real = dashnow._remote_brief

        def brief(b, *rest):  # an unforeseen shape in one session's record
            if b.get("goal") == "Broken":
                raise TypeError("unsupported operand")
            return real(b, *rest)
        with mock.patch.object(dashnow, "_remote_brief", side_effect=brief):
            runs = areas(kw)["runs"]
        self.assertIsInstance(runs, list, runs)
        self.assertEqual([r["goal"] for r in runs], ["Invoice export", "Rules engine migration"])

    def test_a_presence_file_nested_too_deep_for_json_is_skipped(self):
        # 100,000 nested brackets: 200 KB, under the 256 KB cap; Python 3.9's json raises RecursionError on it
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "presence"
            d.mkdir()
            (d / "bob@studio.json").write_text(json.dumps(fx.remote_presence(updated=int(time.time()) - 60)))
            (d / "deep@nest.json").write_text("[" * 100_000 + "]" * 100_000)
            self.assertEqual([p["user"] for p in dashboard.read_presence(Path(tmp), {})], ["bob"])

    def test_oversize_and_malformed_presence_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "presence"
            d.mkdir()
            good = fx.remote_presence(updated=int(time.time()) - 60)
            (d / "bob@studio.json").write_text(json.dumps(good))
            big = dict(good, user="eve", sessions=[dict(good["sessions"][0], status="x" * 300_000)])
            (d / "eve@big.json").write_text(json.dumps(big))
            (d / "mal@formed.json").write_text(json.dumps(dict(good, user="mal", sessions="not a list")))
            (d / "num@sessions.json").write_text(json.dumps(dict(good, user="num", sessions=5)))
            (d / "not@json.json").write_text("{")
            self.assertEqual([p["user"] for p in dashboard.read_presence(Path(tmp), {})], ["bob"])
            reg = {"team": [good, big, dict(good, user=7)]}
            self.assertEqual([p["user"] for p in dashboard.read_presence(Path(tmp), reg)], ["bob"])


class BuildActionsQuotingTests(unittest.TestCase):
    def test_hostile_values_are_quoted_and_ordinary_names_unchanged(self):
        reg = fx.registry_example()
        core = reg["projects"][0]
        core["worktrees"] = [fx._wt(HOSTILE[0], 41)]
        core["worktrees"][0]["path"] = "/tmp/wt $(touch pwn)"
        core["prs_open"].append(dict(fx._pr(46, HOSTILE[0], updated=at(14, 0)), mergeable="CONFLICTING"))
        cmds = {a["kind"]: a["cmd"] for a in dashboard.build_actions(reg, NOW, [], [])}
        self.assertEqual(shlex.split(cmds["reclaim"]), ["dev", "wt", "rm", HOSTILE[0]])
        self.assertEqual(shlex.split(cmds["conflicts"]), ["cd", "/tmp/wt $(touch pwn)", "&&", "git", "fetch", "origin",
                                                          "&&", "git", "rebase", "origin/main"])
        ordinary = {a["kind"]: a["cmd"] for a in fx.example()["actions"]}
        self.assertEqual((ordinary["changes-requested"], ordinary["reclaim"], ordinary["expired-lock"]),
                         ("gh pr view 45 -R acme/lending-core --comments", "dev gc --apply",
                          'dev task -p acme/lending-rules release T-fz3lock --why "lock expired"'))

    def test_a_quoted_reclaim_command_runs_no_command_substitution(self):
        reg = fx.registry_example()
        reg["projects"][0]["worktrees"] = [fx._wt(HOSTILE[0], 41)]
        cmd = next(a["cmd"] for a in dashboard.build_actions(reg, NOW, [], []) if a["kind"] == "reclaim")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(shell(cmd, tmp), ["wt", "rm", HOSTILE[0]])
            self.assertEqual(os.listdir(tmp), [])


class WiringTests(unittest.TestCase):
    # collect() reads this Mac's runs (progress files in a temporary state folder) and serves the Now areas
    def setUp(self):
        from test_progress import TempState
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.handoff = Path(self.state.tmp.name) / "handoff"
        self.handoff.mkdir()
        pg.start("Invoice export", plan=["export api", "export screen"])
        pg.crew_decision("Include cancelled invoices?", options="A: Leave them out | B: Include them", recommend="A")
        self.reg = {"projects": [], "generated": int(time.time()), "host": "testmac", "github_user": "ana",
                    "team": [], "issues": [], "sessions": [], "providers": {}}

    def snapshot(self):
        snap = dashserver.Snapshot(build=lambda: self.reg, paths=lambda: [pg.RUNS], enrich=lambda reg: {},
                                   collect=lambda reg, now=None: dashboard.collect(reg, handoff=self.handoff, now=now))
        snap.refresh(force=True)
        return snap

    def test_collect_serves_the_five_now_areas_next_to_todays_actions(self):
        d = dashboard.collect(self.reg, handoff=self.handoff)
        doc = {k: d[k] for k in now_contract.AREAS}
        self.assertEqual(now_contract.check(doc), [])
        self.assertEqual(ids(doc), ["decision:claude-s1:D1"])
        self.assertEqual((d["runs"][0]["waiting_on"]["kind"], d["runs"][0]["eta"]["state"]), ("decision", "after_answer"))
        self.assertIn("line", d["since"])
        self.assertIsInstance(d["actions"], list)

    def test_answering_d1_removes_its_row_and_tells_open_pages_attention_changed(self):
        snap = self.snapshot()
        before = snap.version
        self.assertEqual(ids(snap.data), ["decision:claude-s1:D1"])
        pg.crew_decide("D1", "A")
        self.assertTrue(snap.refresh())
        self.assertEqual(ids(snap.data), [])
        self.assertIn("attention", snap.changed_since(before))

    def test_the_dashboard_endpoint_serves_the_now_areas_by_name_with_auth_etag_and_gzip(self):
        snap, ready, apps = self.snapshot(), threading.Event(), []
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(8)
        port = sock.getsockname()[1]
        t = threading.Thread(target=dashserver.serve, args=(sock, TOKEN, snap), daemon=True,
                             kwargs={"poll": 0.05, "idle_check": 0.05, "idle": 30, "handoff": self.handoff,
                                     "ready": lambda app: (apps.append(app), ready.set())})
        t.start()
        self.assertTrue(ready.wait(5))
        self.addCleanup(lambda: (apps[0].shutdown(), t.join(5)))

        def get(headers):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", "/api/v1/dashboard?only=attention,runs,safety,since,as_of",
                      headers=dict({"Host": f"127.0.0.1:{port}"}, **headers))
            r = c.getresponse()
            body, hdrs = r.read(), {k.lower(): v for k, v in r.getheaders()}
            c.close()
            return r.status, hdrs, body
        status, hdrs, body = get({"Authorization": f"Bearer {TOKEN}", "Accept-Encoding": "gzip"})
        doc = json.loads(gzip.decompress(body) if hdrs.get("content-encoding") == "gzip" else body)
        self.assertEqual(status, 200)
        self.assertEqual(set(doc) - {"live", "snapshot"}, set(now_contract.AREAS))
        self.assertEqual(now_contract.check(doc), [])
        self.assertEqual(get({"Authorization": f"Bearer {TOKEN}", "If-None-Match": hdrs["etag"]})[0], 304)
        self.assertEqual(get({})[0], 401)


class ContractTests(unittest.TestCase):
    def test_each_state_keeps_the_contract_and_matches_its_golden(self):
        if os.environ.get("NOW_GOLDENS") == "write":
            fx.write_goldens()
        for state in fx.STATES:
            with self.subTest(state=state):
                doc = fx.document(state)
                self.assertEqual(now_contract.check(doc), [])
                self.assertEqual(fx.dumps(doc), fx.golden_path(state).read_text())

    def test_the_contract_check_finds_a_copied_consent_a_missing_key_and_an_unrounded_range(self):
        doc = fx.document("example")
        doc["attention"]["items"][0]["copy"] = "approve D1"
        del doc["attention"]["items"][1]["url"]
        doc["runs"][0]["eta"]["remaining_s"]["low"] = 1501
        errs = "\n".join(now_contract.check(doc))
        self.assertIn("attention.items[0]: has both type_to_answer and copy", errs)
        self.assertIn("attention.items[1]: misses url", errs)
        self.assertIn("runs[0].eta.remaining_s: is not rounded to 5 minutes", errs)

    def test_the_contract_check_wants_a_branch_on_runs_counts_on_since_and_whole_option_keys(self):
        doc = fx.document("example")
        doc["runs"][0].pop("branch", None)
        doc["since"].pop("counts", None)
        doc["attention"]["items"][0]["options"][1]["key"] = "Include the cancelled…"
        errs = "\n".join(now_contract.check(doc))
        self.assertIn("runs[0]: misses branch", errs)
        self.assertIn("since: misses counts", errs)
        self.assertIn("attention.items[0].options[1].key: ends with …", errs)


if __name__ == "__main__":
    unittest.main()
