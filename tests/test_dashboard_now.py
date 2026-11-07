# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Now, the dashboard's first screen (UX spec P-fyka51gico5 3.1; data contract P-fyli4qqn0y7), in headless Chromium.

The server ranks, words and times everything (dashnow.py); these tests check that the page draws the contract's documents
as the spec says: the headline, the Needs you queue, the runs with their landing line, the safety and since lines, every
state, the redirects, and an answered decision leaving the page in place. The documents are the contract's example and
its empty and new-install states, at the contract's clock (2026-11-06 15:15 UTC), with neutral names only."""
import contextlib
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import test_dashboard as T  # noqa: E402  (the browser harness, the fixture and the stand-in server)
from devkit import dashboard  # noqa: E402

CLOCK = 1793978100   # 2026-11-06 15:15 UTC, the contract's example clock
MIN = 60
# runs as the server sends them: today's fields, plus machine, who, waiting_on and eta (the contract's example)
STEP = lambda name, state, est, mins, owner="main", waiting="": {  # noqa: E731
    "name": name, "state": state, "est": est, "min": mins, "owner": owner, "label": "", "persona": "", "after": [], "scope": "", "waiting": waiting}
RUN_PAUSED = {
    "id": "claude-7f3a", "agent": "claude", "session": "7f3a", "project": "acme/lending-core", "goal": "Invoice export", "state": "running",
    "mode": "guided", "branch": "feat/invoice-export", "host": "laptop", "step": 3, "steps": 6, "estimate": 150, "started": CLOCK - 5400,
    "finished": None, "updated": CLOCK - 540, "active_min": 84, "wall_min": 90, "pace": 1.2, "last": "", "next": "",
    "plan": [STEP("design", "done", 15, 12), STEP("contract", "done", 15, 14), STEP("csv writer", "done", 30, 31, "Tank: csv writer"),
             STEP("export endpoint", "blocked", 30, 18, "Tank: export endpoint", "D1"), STEP("docs", "pending", 20, 0, "Ghost: docs"),
             STEP("review", "pending", 20, 0, "Seraph: review")],
    "agents": [], "questions": [], "updates": [],
    "decisions": [{"id": "D1", "ts": 1793977560, "question": "Include cancelled invoices in the export?", "options": ["Leave them out", "Include them"],
                   "recommend": "Leave them out", "irreversible": "", "from_name": "Tank"}],
    "machine": None, "who": None, "waiting_on": {"kind": "decision", "ref": "D1", "since": 1793977560},
    "eta": {"state": "after_answer", "basis": "critical_path", "remaining_s": {"low": 1500, "high": 2400}, "lands_at": None, "over_estimate": None,
            "text": "Lands 25–40 min after you answer D1"}}
RUN_REMOTE = {
    "id": "remote-studio-codex-41c2", "agent": "codex", "session": "41c2", "project": "acme/lending-rules", "goal": "Rules engine migration",
    "state": "running", "mode": "auto", "branch": "feat/rules-engine", "host": "studio", "step": 2, "steps": 5, "estimate": 90, "started": CLOCK - 3600,
    "finished": None, "updated": 1793977980, "active_min": 48, "wall_min": 60, "pace": 1.0, "last": "", "next": "",
    "plan": [STEP("schema", "done", 15, 14), STEP("rules port", "done", 20, 22), STEP("rate tables", "running", 20, 8),
             STEP("parity tests", "running", 20, 4), STEP("cutover", "pending", 15, 0)],
    "agents": [], "questions": [], "updates": [], "decisions": [],
    "machine": "studio", "who": "bob@studio", "seen": 1793977980, "waiting_on": None,
    "eta": {"state": "clock", "basis": "critical_path", "remaining_s": {"low": 2400, "high": 3300}, "lands_at": {"low": 1793980500, "high": 1793981400},
            "over_estimate": None, "text": "Lands 15:55–16:10"}}
ITEM = lambda **kw: dict({"id": "", "rank": 5, "kind": "work", "tone": "ink", "count": 1, "title": "", "clause": "", "lines": [], "since": None,  # noqa: E731
                          "project": None, "run_id": None, "asker": None, "blocks": None, "options": None, "irreversible": None,
                          "type_to_answer": None, "copy": None, "open": None, "url": None}, **kw)
EXAMPLE = {
    "attention": {
        "total": 4, "next_up": [], "setup": [], "degraded": [],
        "headline": {"kind": "needs_you", "count": 2, "text": "2 things need you",
                     "line": "Invoice export is paused until you answer D1. CI failed on feat/rules-engine."},
        "items": [
            ITEM(id="decision:claude-7f3a:D1", rank=1, kind="decision", tone="you", title="D1 · Include cancelled invoices in the export?",
                 clause="Invoice export is paused until you answer D1.", lines=["Backend engineer (Tank) asks · pauses Invoice export, 2 crew members wait"],
                 since=1793977560, project="acme/lending-core", run_id="claude-7f3a", asker="Backend engineer (Tank)",
                 blocks={"run": "Invoice export", "lanes": 2},
                 options=[{"key": "A", "text": "Leave them out", "recommended": True}, {"key": "B", "text": "Include them", "recommended": False}],
                 type_to_answer=["approve D1", "approve D1: B"], open={"kind": "run", "id": "claude-7f3a", "anchor": "D1"}),
            ITEM(id="ci_failed:acme/lending-rules:feat/rules-engine", rank=2, kind="ci_failed", tone="alert", title="CI failed · feat/rules-engine · lending-rules",
                 clause="CI failed on feat/rules-engine.", lines=["test · Run tests · AssertionError: 10.01 == 10.00"], since=1793977380,
                 project="acme/lending-rules", copy="dev run view 14 --failed", open={"kind": "ci", "id": "acme/lending-rules#14", "anchor": None},
                 url="https://github.com/acme/lending-rules/actions/runs/14"),
            ITEM(id="pr:acme/lending-core#45", rank=5, kind="pr", tone="ink", title="PR #45: changes requested", clause="PR #45 has changes requested.",
                 lines=["refactor/ledger-ports · lending-core"], since=1793972400, project="acme/lending-core",
                 copy="gh pr view 45 -R acme/lending-core --comments", open={"kind": "pr", "id": "acme/lending-core#45", "anchor": None},
                 url="https://github.com/acme/lending-core/pull/45"),
            ITEM(id="housekeeping", rank=6, kind="housekeeping", tone="ink", count=4, title="Housekeeping", clause="4 housekeeping items.",
                 lines=["2 finished worktrees, 1 expired lock, 1 note behind the code"], open={"kind": "all", "id": "housekeeping", "anchor": None})]},
    "runs": [RUN_PAUSED, RUN_REMOTE],
    "safety": {"totals": {"not_backed_up": 1, "embargoed": 1, "waiting": 1, "unknown_projects": 0},
               "line": {"tone": "caution", "text": "1 branch is only on this Mac: lending-core fix/rounding-error, 3 commits", "oldest": 1793970900,
                        "open": {"kind": "branches", "id": None, "anchor": None}},
               "projects": [{"project": "acme/lending-core", "not_backed_up": 1, "embargoed": 1, "waiting": 1, "oldest": 1793970900, "mapped_at": 1793978060,
                             "rows": [{"branch": "fix/rounding-error", "state": "not_backed_up", "reason": "3 commits only on this Mac", "commits": 3,
                                       "oldest": 1793970900, "copy": "cd -- /Users/ana/Development/acme/lending-core && dev backup -- fix/rounding-error"}]}]},
    "since": {"line": {"from": 1793836800, "text": "4 tasks done · 2 PRs merged · 1 run finished in 1 h 40 (estimate 2 h)",
                       "items": [{"kind": "task_done", "count": 4, "text": "4 tasks done"}, {"kind": "pr_merged", "count": 2, "text": "2 PRs merged"},
                                 {"kind": "run_finished", "count": 1, "text": "1 run finished in 1 h 40 (estimate 2 h)"}]}},
    "as_of": {"registry": {"at": 1793978060, "every_s": 60}, "prs": {"at": 1793977500, "every_s": 900}}}
NEXT_UP = [{"project": "acme/lending-core", "task_id": "T-fx01", "title": "Document the export endpoint", "copy": "dev task -p acme/lending-core start T-fx01"},
           {"project": "acme/lending-rules", "task_id": "T-fx02", "title": "Port the rate tables", "copy": "dev task -p acme/lending-rules start T-fx02"},
           {"project": "acme/ledger", "task_id": "T-fx03", "title": "Split the posting service", "copy": "dev task -p acme/ledger start T-fx03"}]
EMPTY = {
    "attention": {"total": 0, "items": [], "next_up": NEXT_UP, "setup": [], "degraded": [],
                  "headline": {"kind": "clear_idle", "count": 0, "text": "Nothing needs you",
                               "line": "Nothing is running. Next in lending-core: Document the export endpoint."}},
    "runs": [],
    "safety": {"totals": {"not_backed_up": 0, "embargoed": 0, "waiting": 0, "unknown_projects": 0},
               "line": {"tone": "ok", "text": "Every branch is on the remote.", "oldest": None, "open": {"kind": "branches", "id": None, "anchor": None}},
               "projects": []},
    "since": {"line": {"from": 1793836800, "items": [], "text": ""}},
    "as_of": {"registry": {"at": 1793978060, "every_s": 60}, "prs": {"at": 1793977500, "every_s": 900}}}
WELCOME = {
    "attention": {"total": 0, "items": [], "next_up": [], "degraded": [],
                  "setup": [{"key": "agents", "done": True, "text": "An agent is signed in", "copy": None},
                            {"key": "project", "done": False, "text": "No project is tracked yet", "copy": None},
                            {"key": "sync", "done": False, "text": "devkit has not synced yet", "copy": "dev sync"}],
                  "headline": {"kind": "welcome", "count": 0, "text": "Welcome to devkit",
                               "line": "Nothing is tracked yet. Clone a project with dev clone <owner/repo>, or start one with dev new <name>."}},
    "runs": [],
    "safety": {"totals": {"not_backed_up": 0, "embargoed": 0, "waiting": 0, "unknown_projects": 0},
               "line": {"tone": "unknown", "text": "No branch map yet. dev sync makes one.", "oldest": None,
                        "open": {"kind": "branches", "id": None, "anchor": None}}, "projects": []},
    "since": {"line": {"from": 1793836800, "items": [], "text": ""}},
    "as_of": {"registry": {"at": 1793978060, "every_s": 60}, "prs": {"at": 1793977500, "every_s": 900}}}
DOCS = {"example": EXAMPLE, "empty": EMPTY, "welcome": WELCOME}

# the page's clock is the document's: every `new Date()` and `Date.now()` reads it (relative ages, the date line)
FREEZE = ('<script>(() => { const T = %d * 1000, R = Date; class F extends R { constructor(...a) { super(...(a.length ? a : [T])); } '
          'static now() { return T; } } window.Date = F; })();</script>')


def doc(name: str) -> dict:
    """A copy of one of the contract's documents (Now's five areas)."""
    return json.loads(json.dumps(DOCS[name]))


def with_doc(data: dict, name: str) -> dict:
    """The page's data with Now's areas from a document: new areas replace, `since` keeps today's fields and gains `line`."""
    d, now = json.loads(json.dumps(data)), doc(name)
    d["since"] = dict(d.get("since") or {}, **now.pop("since"))
    d.update(now, generated=CLOCK)
    return d


def frozen(page: str, clock: int = CLOCK) -> str:
    return page.replace("<head>", "<head>" + FREEZE % clock, 1)


@contextlib.contextmanager
def utc():
    """Chrome (a child process) renders clock times in UTC, as the contract's example does."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old


@unittest.skipUnless(T.CHROME, T.NO_CHROME)
class NowScreenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = T.fixture_data(Path(cls.tmp.name) / "handoff")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def page(self, name="example", change=None):
        d = with_doc(self.base, name)
        if change:
            change(d)
        return frozen(dashboard.page(d))

    def probe(self, steps, name="example", width=1440, hash_="#/now", change=None):
        with utc():
            out = T.browse(self.page(name, change), steps, width, hash_)
        self.assertIsNone(out.get("probeError"), out.get("probeError"))
        self.assertIsNone(out.get("pageError"), out.get("pageError"))
        return out

    # AC-1: the headline counts ranks 1-4, the line names the paused run and D1, and the first row has the question,
    # the asker role first, what it pauses, the wait, the options with the recommendation and the words to type.
    def test_a_decision_leads_with_its_question_options_and_the_words_to_type_and_no_copy_button(self):
        out = self.probe("""out.h1 = text($("main h1")); out.line = text($(".now-line")); const r = $(".now-item");
            out.key = r.getAttribute("data-key"); out.you = r.classList.contains("you"); out.row = text(r);
            out.kbd = $$("kbd", r).map(text); out.copies = $$("button", r).map(b => b.getAttribute("aria-label"));
            out.open = $$("a", r).map(text); out.amber = getComputedStyle(r, "::before").backgroundColor;
            out.others = $$(".now-item").slice(1).filter(x => getComputedStyle(x, "::before").backgroundColor === out.amber).length;""")
        self.assertEqual(out["h1"], "2 things need you")
        self.assertEqual(out["line"], "Invoice export is paused until you answer D1. CI failed on feat/rules-engine. · a copy from 15:15; it does not update.")
        self.assertEqual((out["key"], out["you"]), ("decision:claude-7f3a:D1", True))
        for part in ("D1 · Include cancelled invoices in the export?", "9 min", "Backend engineer (Tank) asks · pauses Invoice export, 2 crew members wait",
                     "Options: A Leave them out (recommended) · B Include them", "In the session, type: approve D1 or approve D1: B"):
            self.assertIn(part, out["row"])
        self.assertEqual(out["kbd"], ["approve D1", "approve D1: B"])
        self.assertEqual(out["copies"], [])                                                     # consent is typed, never pasted
        self.assertEqual(out["open"], ["Open the run"])
        self.assertEqual(out["others"], 0)                                                      # the only amber bar is the first row's

    def test_the_queue_shows_five_rows_in_the_servers_order_then_all(self):
        def more(d):
            items = d["attention"]["items"]
            items[2:2] = [ITEM(id="review_wait:claude-7f3a", rank=3, kind="review_wait", tone="caution", title="3 steps wait for review · oldest 2 h 10 min",
                               lines=["Code reviewer (Seraph) owes 2, Product owner (Councillor Hamann) owes 1.", "Ask the tech lead to start the reviews."],
                               open={"kind": "run", "id": "claude-7f3a", "anchor": "approvals"}),
                          ITEM(id="job_failed:doctor", rank=4, kind="job_failed", tone="alert", title="The doctor job failed at 06:00",
                               lines=["2 checks failed"], copy="dev jobs show doctor", open={"kind": "job", "id": "doctor", "anchor": None}),
                          ITEM(id="backup:acme/lending-core:fix/rounding-error", rank=4, kind="backup", tone="caution",
                               title="fix/rounding-error is only on this Mac · lending-core", lines=["3 commits, the oldest 5 h ago"],
                               copy="cd -- /Users/ana/Development/acme/lending-core && dev backup -- fix/rounding-error",
                               open={"kind": "branches", "id": "acme/lending-core", "anchor": None})]
            d["attention"]["total"] = len(items)
            d["attention"]["headline"].update(count=5, text="5 things need you")   # the headline counts the rows of ranks 1-4
        out = self.probe("""out.keys = $$(".now-item").map(r => r.getAttribute("data-key")); out.all = text($(".now-all"));
            out.href = $(".now-all").getAttribute("href"); out.generic = text($('[data-key="review_wait:claude-7f3a"]'));
            out.glyphs = $$(".now-item").map(r => ($(".now-glyph", r) || {getAttribute: () => ""}).getAttribute("aria-label"));
            await go("#/now?all=1"); out.full = $$(".now-item").map(r => r.getAttribute("data-key")); out.allTitle = text($("main h1"));""", change=more)
        self.assertEqual(out["keys"], ["decision:claude-7f3a:D1", "ci_failed:acme/lending-rules:feat/rules-engine", "review_wait:claude-7f3a",
                                       "job_failed:doctor", "backup:acme/lending-core:fix/rounding-error"])
        self.assertEqual((out["all"], out["href"]), ("All 7", "#/now?all=1"))
        self.assertIn("3 steps wait for review · oldest 2 h 10 min", out["generic"])            # a kind this page does not know: a plain row
        self.assertIn("Ask the tech lead to start the reviews.", out["generic"])
        self.assertIn("Open the run", out["generic"])
        self.assertEqual(out["glyphs"], ["Waits for you", "Broken", "At risk", "Broken", "At risk"])
        self.assertEqual(len(out["full"]), 7)
        self.assertEqual(out["allTitle"], "Everything that needs you")

    def test_an_area_that_failed_or_is_stale_says_so_where_its_rows_would_be(self):
        def broken(d):
            d["runs"] = {"error": {"title": "The runs could not be read", "detail": "progress: the run file of claude-7f3a is not JSON"}}
            d["attention"]["degraded"] = [{"source": "prs", "detail": "Pull requests could not be read: gh did not answer (exit 1).", "as_of": CLOCK - 23 * MIN}]
            d["as_of"]["prs"] = {"at": CLOCK - 23 * MIN, "every_s": 300}
        out = self.probe("""out.queue = text($(".now-queue")); out.runs = text($(".now-runs")); out.tag = text($(".now-queue .now-stale"));""", change=broken)
        self.assertIn("Pull requests could not be read: gh did not answer (exit 1). Shown: the results from 14:52.", out["queue"])
        self.assertEqual(out["tag"], "as of 14:52 · 23 min old")
        self.assertIn("The runs could not be read", out["runs"])
        self.assertIn("progress: the run file of claude-7f3a is not JSON", out["runs"])

    # AC-2: the sentence, its line, and the top three Now tasks with the command to start each.
    def test_nothing_needs_you_and_nothing_runs_offers_the_next_three_tasks(self):
        out = self.probe("""out.h1 = text($("main h1")); out.line = text($(".now-line")); out.cols = [!!$(".now-queue"), !!$(".now-runs")];
            out.next = $$(".now-next li").map(text); out.copies = $$(".now-next button").map(b => b.getAttribute("aria-label"));
            out.safety = text($(".now-safety")); out.since = text($(".now-since"));""", name="empty")
        self.assertEqual(out["h1"], "Nothing needs you")
        self.assertTrue(out["line"].startswith("Nothing is running. Next in lending-core: Document the export endpoint."), out["line"])
        self.assertEqual(out["cols"], [False, False])                                          # the two columns become one block
        self.assertEqual(len(out["next"]), 3)
        self.assertIn("Document the export endpoint", out["next"][0])
        self.assertIn("lending-core", out["next"][0])
        self.assertEqual(out["copies"], ["Copy: dev task -p acme/lending-core start T-fx01", "Copy: dev task -p acme/lending-rules start T-fx02",
                                         "Copy: dev task -p acme/ledger start T-fx03"])
        self.assertEqual(out["safety"], "Every branch is on the remote.")
        self.assertEqual(out["since"], "Since yesterday: nothing finished.")

    def test_quiet_and_first_run_states_have_their_copy(self):
        def running(d):
            d["attention"].update(items=[], total=0, headline={"kind": "clear_running", "count": 0, "text": "Nothing needs you",
                                                              "line": "2 runs are going. The first lands about 15:55."})
        out = self.probe("""out.queue = text($(".now-queue")); out.blocks = $$(".now-run").length;""", change=running)
        self.assertEqual(out["queue"], "Needs you Nothing waits for you.")
        self.assertEqual(out["blocks"], 2)
        out = self.probe("""out.h1 = text($("main h1")); out.setup = $$(".now-setup li").map(text); out.safety = text($(".now-safety"));""", name="welcome")
        self.assertEqual(out["h1"], "Welcome to devkit")
        self.assertEqual(out["setup"][0], "Done An agent is signed in")
        self.assertEqual(out["setup"][1], "To do No project is tracked yet")
        self.assertTrue(out["setup"][2].startswith("To do devkit has not synced yet"), out["setup"][2])
        self.assertEqual(out["safety"], "No branch map yet. dev sync makes one.")

    # AC-4 (landing line on Now): the server's words, the page's age prefixes, and the bracket's range.
    def test_each_landing_state_has_its_line_and_its_bracket(self):
        def run(rid, goal, eta, **kw):
            return dict(json.loads(json.dumps(RUN_REMOTE)), id=rid, goal=goal, machine=None, who=None, eta=dict({"basis": None, "remaining_s": None,
                        "lands_at": None, "over_estimate": None}, **eta), **kw)

        def five(d):
            d["runs"] = [json.loads(json.dumps(RUN_PAUSED)),
                         run("r-blocked", "Ledger ports", {"state": "blocked", "text": "Blocked: the export endpoint fails its contract test. No landing time until it moves."},
                             waiting_on={"kind": "blocked", "ref": None, "since": CLOCK - 600}),
                         run("r-stale", "Fee rules", {"state": "unknown", "text": "The landing time is unknown."}, waiting_on={"kind": "stale", "ref": None, "since": CLOCK - 18 * MIN}),
                         run("r-clock", "Rate tables", {"state": "clock", "basis": "estimate", "remaining_s": {"low": 2400, "high": 3300},
                                                        "lands_at": {"low": CLOCK + 2400, "high": CLOCK + 3300}, "text": "Lands 15:55–16:10"}),
                         run("r-none", "Audit export", {"state": "none", "text": "No estimate, so no landing time."}, plan=[], active_min=48)]

        def two(d):
            d["runs"] = [run("r-over", "Statement batch", {"state": "clock", "basis": "estimate", "remaining_s": {"low": 3900, "high": 5400},
                                                           "lands_at": {"low": CLOCK + 3900, "high": CLOCK + 5400}, "over_estimate": {"estimate_s": 7200, "now_s": 10800},
                                                           "text": "Lands 16:20–16:45, later than planned (estimate 2 h, now about 3 h)."}),
                         run("r-done", "Docs refresh", {"state": "done", "text": "Finished 14:20 in 35 min"}, state="done", finished=CLOCK - 55 * MIN)]
        steps = """out.lands = $$(".now-run").map(b => text($(".now-lands", b))); out.brackets = $$(".now-run").map(b => text($(".now-bracket", b)) || "");
            out.more = text($(".now-more")); out.done = $$(".now-done").map(text); out.labels = $$(".now-run .now-strip").map(s => s.getAttribute("aria-label"));"""
        out = self.probe(steps, change=five)
        self.assertEqual(out["lands"], ["Lands 25–40 min after you answer D1", "Blocked: the export endpoint fails its contract test. No landing time until it moves.",
                                        "No update for 18 min. The landing time is unknown.", "Lands 15:55–16:10"])
        self.assertEqual(out["brackets"], ["+25–40 min", "", "", "15:55–16:10"])
        self.assertEqual(out["more"], "1 more run")                                            # at most four blocks
        self.assertEqual(out["labels"][0], "3 of 6 steps done; waiting for your answer to D1. Lands 25–40 min after you answer D1")
        out = self.probe(steps, change=two)
        self.assertEqual(out["lands"], ["Lands 16:20–16:45, later than planned (estimate 2 h, now about 3 h)."])
        self.assertEqual(out["done"], ["Docs refresh · finished 14:20 in 35 min"])              # done in the last hour: one line
        out = self.probe(steps, change=lambda d: d.update(runs=[run("r-none", "Audit export", {"state": "none", "text": "No estimate, so no landing time."},
                                                                  plan=[], active_min=48)]))
        self.assertEqual(out["lands"], ["48 min so far. No estimate, so no landing time."])

    def test_the_strip_draws_at_most_eight_steps_and_counts_the_rest(self):
        def long(d):
            plan = [STEP(f"step {i}", "done", 10, 10) for i in range(90)] + [STEP("wire", "running", 10, 2)] + [STEP(f"open {i}", "pending", 10, 0) for i in range(30)]
            d["runs"] = [dict(d["runs"][1], plan=plan, step=90, steps=121)]
        out = self.probe("""const s = $(".now-run .now-strip"); out.glyphs = $$(".now-step", s).map(g => g.getAttribute("data-state")); out.rest = text($(".now-rest", s));""", change=long)
        self.assertEqual(out["glyphs"], ["done", "running", "pending", "pending", "pending", "pending", "pending", "pending"])
        self.assertEqual(out["rest"], "+89 done")

    def test_runs_name_where_they_run_and_open_todays_run_drawer(self):
        out = self.probe("""out.blocks = $$(".now-run").map(text); out.team = text($(".now-team"));
            $(".now-run a").click(); await tick(200); out.open = $("#modal").open; out.title = text($("#modal-title")); out.hash = location.hash;""")
        self.assertIn("Invoice export · lending-core", out["blocks"][0])
        self.assertIn("feat/invoice-export · guided · Claude Code", out["blocks"][0])
        self.assertIn("3 of 6 steps · paused on you 9 min", out["blocks"][0])
        self.assertIn("feat/rules-engine · auto · Codex · on studio · last update 2 min ago", out["blocks"][1])
        self.assertIn("2 of 5 steps · 48 min of about 1 h 30", out["blocks"][1])
        self.assertEqual(out["team"], "bob@studio works on widget")
        self.assertEqual((out["open"], out["title"]), (True, "Invoice export"))                 # today's drawer until the run page ships
        self.assertEqual(out["hash"], "#/now?modal=run%3Aclaude-7f3a")

    # AC-16 on Now: every crew member's name follows its role.
    def test_crew_members_read_role_first(self):
        out = self.probe("""const m = $("main").innerText, bad = [];
            for (const [n, r] of [["Tank", "Backend engineer"], ["Sati", "UX designer"], ["Trinity", "Frontend engineer"], ["Ghost", "Technical writer"],
                                  ["Seraph", "Code reviewer"], ["Morpheus", "Tech lead"], ["Link", "Release engineer"], ["Sparks", "Lookup scout"]]) {
              const re = new RegExp("\\\\b" + n + "\\\\b", "g"); let x; while ((x = re.exec(m))) if (!m.slice(0, x.index).endsWith(r + " (")) bad.push(m.slice(Math.max(0, x.index - 30), x.index + n.length)); }
            out.bad = bad; out.has = m.includes("Backend engineer (Tank)");""")
        self.assertTrue(out["has"])
        self.assertEqual(out["bad"], [])

    def test_old_links_and_chords_open_now(self):
        out = self.probe("""out.first = location.hash; out.tab = $$("#tabs .tab").map(a => [a.getAttribute("data-view"), text(a)])[0];
            await go("#/actions"); out.actions = location.hash; out.title = text($("main h1"));
            await go("#/tasks"); key(document, "g"); key(document, "o"); await tick(200); out.go = location.hash;
            await go("#/tasks"); key(document, "g"); key(document, "n"); await tick(200); out.gn = location.hash;""", hash_="#/overview")
        self.assertEqual(out["first"], "#/now")
        self.assertEqual(out["tab"], ["now", "Now"])
        self.assertEqual((out["actions"], out["title"]), ("#/now?all=1", "Everything that needs you"))
        self.assertEqual((out["go"], out["gn"]), ("#/now", "#/now"))

    def test_a_consent_line_never_reaches_the_clipboard_and_commands_copy_as_written(self):
        out = self.probe("""const got = []; navigator.clipboard.writeText = t => { got.push(t); return Promise.resolve(); };
            for (const b of $$(".now-item button, .now-item kbd")) b.click(); await tick(100); out.got = got;""")
        self.assertEqual(out["got"], ["dev run view 14 --failed", "gh pr view 45 -R acme/lending-core --comments"])
        self.assertFalse(any("approve" in g for g in out["got"]))

    def test_the_safety_and_since_lines_close_the_page(self):
        out = self.probe("""out.safety = text($(".now-safety")); out.href = $(".now-safety a").getAttribute("href"); out.since = text($(".now-since"));
            out.date = text($(".now-date"));""")
        self.assertEqual(out["safety"], "1 branch is only on this Mac: lending-core fix/rounding-error, 3 commits, the oldest 2 h ago. Branches")
        self.assertEqual(out["href"], "#/projects/acme/lending-core?tab=branches")
        self.assertIn("Since yesterday: 4 tasks done · 2 PRs merged · 1 run finished in 1 h 40 (estimate 2 h)", out["since"])
        self.assertEqual(out["date"], "Friday, Nov 6 · 15:15")

    def test_on_a_phone_the_queue_comes_first_and_nothing_scrolls_sideways(self):
        out = self.probe("""out.page = [document.documentElement.scrollWidth, document.documentElement.clientWidth];
            out.order = [$(".now-queue").getBoundingClientRect().top, $(".now-runs").getBoundingClientRect().top];""", width=390)
        self.assertLessEqual(out["page"][0], out["page"][1])
        self.assertLess(out["order"][0], out["order"][1])

    def test_before_the_first_snapshot_now_shows_shapes_and_says_it_loads(self):
        out = self.probe("""out.shapes = $$(".now-loading .now-shape").length; out.sr = text($(".now-loading .sr")); out.spin = $$(".spinner,[role=progressbar]").length;""",
                         change=lambda d: [d.pop(k) for k in ("attention", "safety")])
        self.assertGreaterEqual(out["shapes"], 5)
        self.assertEqual((out["sr"], out["spin"]), ("Loading what needs you.", 0))


@unittest.skipUnless(T.CHROME, T.NO_CHROME)
class NowLiveTests(unittest.TestCase):
    """AC-15, the page's half: a snapshot without D1 redraws Now in place, keeping the scroll position, the focus and an open drawer."""
    serve = T.LivePageTests.serve

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.data = dict(with_doc(T.fixture_data(Path(cls.tmp.name) / "handoff"), "example"), live=True, snapshot=1)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def answered(self, steps, hash_, width):
        after = doc("example")
        after["attention"]["items"].pop(0)
        after["attention"].update(total=3, headline={"kind": "urgent", "count": 1, "text": "CI failed on feat/rules-engine",
                                                     "line": "test · Run tests · AssertionError: 10.01 == 10.00"})
        run = after["runs"][0]
        run.update(waiting_on=None, decisions=[dict(run["decisions"][0], state="answered")], eta={"state": "clock", "basis": "critical_path",
                   "remaining_s": {"low": 1500, "high": 2400}, "lands_at": {"low": CLOCK + 1500, "high": CLOCK + 2400}, "over_estimate": None, "text": "Lands 15:40–15:55"})
        run["plan"][3]["state"] = "running"
        streams = []

        def routes(path):
            if path == "/":
                return 200, "text/html", frozen(dashboard.page(self.data)).replace("</body>", T.PROBE % steps + "</body>").encode()
            if path.startswith("/api/v1/events"):          # the first stream is quiet; the reconnect brings the snapshot without D1
                streams.append(path)
                return 200, "text/event-stream", (b"retry: 1000\n\n" if len(streams) == 1 else
                                                  b'retry: 60000\n\nid: 2\nevent: snapshot\ndata: {"version": 2, "changed": ["attention", "runs"]}\n\n')
            if path.startswith("/api/v1/dashboard?only=attention,runs"):
                return 200, "application/json", json.dumps({"attention": after["attention"], "runs": after["runs"], "snapshot": 2, "live": True}).encode()
            if path == "/api/v1/files":
                return 200, "application/json", b'{"files": [], "meta": {}}'
            if path == "/api/v1/health":
                return 200, "application/json", json.dumps({"ok": True, "version": self.data["version"]}).encode()
            return 404, "application/problem+json", b'{"title": "Not found"}'
        with utc():
            out = T.chrome(self.serve(routes) + "/" + hash_, width, budget=20000)
        self.assertIsNone(out.get("probeError"), out.get("probeError"))
        self.assertIsNone(out.get("pageError"), out.get("pageError"))
        return out

    def test_an_answered_decision_leaves_now_and_the_scroll_and_focus_stay(self):
        out = self.answered("""const b = $('[data-key="ci_failed:acme/lending-rules:feat/rules-engine"] button'); b.focus(); window.scrollTo(0, 240);
            out.before = [$$(".now-item").length, window.scrollY]; await tick(8000);
            out.keys = $$(".now-item").map(r => r.getAttribute("data-key")); out.h1 = text($("main h1")); out.y = window.scrollY;
            out.focus = document.activeElement.getAttribute("aria-label"); out.lands = text($(".now-run .now-lands"));""", "#/now", 390)
        self.assertEqual(out["before"], [4, 240])
        self.assertEqual(out["keys"], ["ci_failed:acme/lending-rules:feat/rules-engine", "pr:acme/lending-core#45", "housekeeping"])
        self.assertEqual(out["h1"], "CI failed on feat/rules-engine")
        self.assertEqual(out["y"], 240)
        self.assertEqual(out["focus"], "Copy: dev run view 14 --failed")
        self.assertEqual(out["lands"], "Lands 15:40–15:55")

    def test_an_answered_decision_keeps_an_open_drawer(self):
        out = self.answered("""out.before = [$("#modal").open, $$(".now-item").map(r => r.getAttribute("data-key"))]; await tick(8000);
            out.open = $("#modal").open; out.title = text($("#modal-title")); out.keys = $$(".now-item").map(r => r.getAttribute("data-key"));""",
                            "#/now?modal=since%3Adone", 1440)
        self.assertEqual(out["before"], [True, ["decision:claude-7f3a:D1", "ci_failed:acme/lending-rules:feat/rules-engine", "pr:acme/lending-core#45", "housekeeping"]])
        self.assertEqual((out["open"], out["title"]), (True, "Tasks completed since yesterday"))
        self.assertEqual(out["keys"], ["ci_failed:acme/lending-rules:feat/rules-engine", "pr:acme/lending-core#45", "housekeeping"])


class NowSectionTests(unittest.TestCase):
    """The section file rule (contract, module layout): a fragment inside the app's closure, names that start with its section,
    one registration line, styles on .now- classes, placed between the app and boot()."""
    WEB = Path(dashboard.__file__).resolve().parent / "web" / "sections"

    def test_the_section_keeps_to_its_names_its_classes_and_its_place(self):
        js = (self.WEB / "now.js").read_text(encoding="utf-8")
        css = (self.WEB / "now.css").read_text(encoding="utf-8")
        names = re.findall(r"^(?:async\s+)?(?:function\s*\*?\s*|const\s+|let\s+|var\s+|class\s+)([A-Za-z_$][\w$]*)", js, re.M)
        self.assertGreater(len(names), 5)
        self.assertEqual([n for n in names if not re.match(r"^(?:viewNow\w*|now[A-Z]\w*|NOW_[A-Z0-9_]+)$", n)], [])
        self.assertEqual(re.findall(r"^VIEWFN\.\w+ = \w+;$", js, re.M), ["VIEWFN.now = viewNow;"])
        selectors = [s.strip() for sel, _b in T.css_rules(css) for s in sel.split(",") if not sel.startswith("@")]
        self.assertGreater(len(selectors), 10)
        self.assertEqual([s for s in selectors if ".now-" not in s], [])
        self.assertLess(dashboard.JS.index("const VIEWFN = {"), dashboard.JS.index(js))
        self.assertLess(dashboard.JS.index(js), dashboard.JS.index("(function boot() {"))
        self.assertIn(css, dashboard.CSS)


if __name__ == "__main__":
    unittest.main()
