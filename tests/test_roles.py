# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Roles in every output (task F-fy2m2mj8b3j, lesson L-fy2lfvacp64): the developer reads a crew member as
# `<Role> (<Name>)` ("Security engineer (The Keymaker)", "Tech lead (Morpheus)") or as the role alone, never as the
# Matrix name alone. One helper, crew.who, gives the form from the roster's `title`; every renderer that names a
# member uses it, and every report template ends with "Needs from the tech lead:". Machine ids in commands
# (`dev crew brief link`) stay as they are. The role check of final messages (canary.bare_names) checks the tech lead
# too. devkit's own roster only (never the team's changes in the developer's handoff repository), and progress state
# in a temporary folder.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_roles
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / "lib"))
sys.path.insert(0, str(TESTS))

from devkit import canary, core, crew, hooks, skillguard, state  # noqa: E402
from devkit import continuity as ct  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_continuity import SID, T0, Base as ContinuityCase  # noqa: E402
from test_crew import TempState as CrewState, run_cli  # noqa: E402
from test_threaded_turn_endings import TurnCase  # noqa: E402

ROSTER = crew.load(team=False)  # devkit's own roster, never the team's changes in the developer's handoff repository


def bare(text):
    return canary.bare_names(text, ROSTER)


class State(CrewState):
    # test_crew's state (progress and audit in a temporary folder, session claude:s1, devkit's default config), and
    # the crew without the team's changes
    def __enter__(self):
        super().__enter__()
        self.overlay = mock.patch.object(crew, "overlay_path", lambda: Path(self.tmp.name) / "no-crew.json")
        self.overlay.start()
        return self

    def __exit__(self, *exc):
        self.overlay.stop()
        return super().__exit__(*exc)


class WhoTests(unittest.TestCase):
    # the one helper takes a member, or any name a member answers to: its name, id, alias, agent type, a lane label
    # ("Kid: curl installer") or "main" (the main session); anything else stays as written
    def test_who_names_the_role_first_from_a_member_or_a_name(self):
        tank = crew.member("tank", ROSTER)
        self.assertEqual(crew.who(tank), "Backend engineer (Tank)")
        for name, form in (("Tank", "Backend engineer (Tank)"), ("keymaker", "Security engineer (The Keymaker)"),
                           ("Kid: curl installer", "DevEx engineer (Kid)"), ("devkit:neo", "Senior engineer (Neo)"),
                           ("scout", "Lookup scout (Sparks)"), ("main", "Tech lead (Morpheus)"),
                           ("Morpheus", "Tech lead (Morpheus)")):
            with self.subTest(name=name):
                self.assertEqual(crew.who(name, ROSTER), form)
        for other in ("devkit", "CI watch", "the developer", ""):
            with self.subTest(other=other):
                self.assertEqual(crew.who(other, ROSTER), other)


class TechLeadTests(unittest.TestCase):
    # the developer's decision: the tech lead is not exempt from the role check
    def test_the_tech_lead_named_alone_is_found_like_any_member(self):
        self.assertEqual(bare("Needs from Morpheus: nothing"), ["Morpheus"])
        self.assertEqual(bare("Morpheus routes it to Tank."), ["Morpheus", "Tank"])
        self.assertEqual(bare("Tech lead (Morpheus) records it. Needs from the tech lead: nothing"), [])

    # a role that holds a name the member answers to ("Lookup scout", alias scout) is still its role form
    def test_a_role_form_whose_title_holds_an_alias_passes(self):
        self.assertEqual(bare("The Lookup scout (Sparks) found it; the scout is fast."), ["Sparks"])
        self.assertEqual(bare("**[Firefly> DevKit · Lookup scout (Sparks)]** devkit · `main` · lookup"), [])


class ReportTemplateTests(unittest.TestCase):
    # every agent definition and brief ends the report with "Needs from the tech lead:", and its prose names the
    # tech lead by the role, so a member that follows it is never sent back
    def test_every_agent_body_ends_with_needs_from_the_tech_lead(self):
        for m in crew.members(ROSTER, include_main=False):
            with self.subTest(member=m["id"]):
                body = crew.body(m, ROSTER)
                self.assertIn("End with `Needs from the tech lead:`", body)
                self.assertEqual(bare(body.split("\n", 1)[1]), [])  # after "You are <name>, the crew's <role>."

    def test_the_brief_ends_with_needs_from_the_tech_lead(self):
        for mid in ("neo", "sparks"):  # with a shell, and without one
            with self.subTest(member=mid), tempfile.TemporaryDirectory() as d:
                text = crew.brief_text(crew.member(mid, ROSTER), "Build the CSV parser.", "lib/parser/**",
                                       cwd=d, roster=ROSTER)
                self.assertIn("`Needs from the tech lead:`", text)
                self.assertNotIn("Morpheus", text)

    def test_a_brief_from_the_agent_tool_gets_the_members_role_in_its_report_header(self):
        with mock.patch.object(skillguard, "installed", lambda agent: set()), \
                mock.patch.object(skillguard, "enabled", lambda: True), \
                mock.patch.object(crew, "load", lambda *a, **k: ROSTER):
            out = skillguard.on_agent_brief("claude", {"prompt": "Rework the layout.",
                                                       "description": "Trinity: rework the layout"})
        self.assertIn("**[Firefly> DevKit · Frontend engineer (Trinity)]** <project>", out["prompt"])
        self.assertIn("ask the tech lead with `dev crew ask", out["prompt"])
        self.assertNotIn("Morpheus", out["prompt"])

    def test_the_shipped_agent_files_say_needs_from_the_tech_lead(self):
        for f in sorted((TESTS.parent / "plugin" / "agents").glob("*.md")):
            with self.subTest(agent=f.name):
                text = f.read_text()
                self.assertIn("`Needs from the tech lead:`", text)
                self.assertEqual(bare(text.split("\nYou are ", 1)[1].split("\n", 1)[1]), [])


class CrewCommandTests(unittest.TestCase):
    # `dev crew`, `dev crew next`, messages, decisions, delegation and routing name members role first
    def test_the_crew_view_names_lanes_assignments_and_messages_role_first(self):
        with State() as t:
            pg.start("Parser", plan=["parse", "review"])
            pg.assign("review", "Seraph", scope="lib/**")
            t.launch()
            pg.crew_ask("Empty lines?", from_agent="a1")
            pg.crew_decision("Accept empty lines?", options="A: yes, B: no", from_agent="a1")
            out = run_cli("crew")[1]
        for needle in ("2. review → Code reviewer (Seraph) · pending", "M1 Senior engineer (Neo) → Tech lead "
                       "(Morpheus) · not delivered: Empty lines?", "D1 Senior engineer (Neo): Accept empty lines?",
                       "Senior engineer (Neo)", "Tech lead (Morpheus)"):
            self.assertIn(needle, out)
        self.assertEqual(bare(out), [])

    def test_ask_answer_and_inbox_name_both_ends_role_first(self):
        with State() as t:
            pg.start("Parser", plan=["parse"])
            t.launch()
            asked = run_cli("crew", "ask", "Empty lines?", from_agent="a1")[1]
            inbox = run_cli("crew", "inbox")[1]
            answered = run_cli("crew", "answer", "M1", "Yes")[1]
            theirs = run_cli("crew", "inbox", from_agent="a1")[1]
        self.assertIn("M1 to Tech lead (Morpheus): Empty lines?", asked)
        self.assertIn("M1 from Senior engineer (Neo): Empty lines?", inbox)
        self.assertIn("M2 to Senior engineer (Neo), the answer to M1: Yes", answered)
        self.assertIn("M2 from Tech lead (Morpheus): Yes (answer to M1)", theirs)
        for out in (asked, inbox, answered, theirs):
            self.assertEqual(bare(out), [], out)

    def test_next_names_the_busy_members_role_first_without_their_name_in_the_label(self):
        with State() as t:
            pg.start("Feature", plan=["docs=10m"])
            pg.assign("docs", "Ghost")
            t.launch("b1", "devkit:mouse", "Mouse: other work")
            out = run_cli("crew", "next")[1]
        self.assertIn("Technical writer (Ghost): docs", out)
        self.assertIn("QA engineer (Mouse) · other work", out)
        self.assertEqual(bare(out), [])

    def test_delegate_and_decline_name_the_member_and_the_tech_lead_by_role(self):
        with State() as t:
            pg.start("Feature", plan=["parser=30m"])
            t.launch("a1", "devkit:neo", "Neo: parser")
            queued = run_cli("crew", "delegate", "add the export column", "--to", "Dozer", from_agent="a1")[1]
            t.launch("a2", "devkit:seraph", "Seraph: review")
            handed = run_cli("crew", "delegate", "review the parser", "--to", "Seraph", from_agent="a1")[1]
            declined = run_cli("crew", "decline", "review the parser", "--why", "needs a plan", from_agent="a2")[1]
        self.assertIn("step “add the export column” for Database engineer (Dozer)", queued)
        self.assertIn("The tech lead launches Database engineer (Dozer) when it is ready: dev crew next", queued)
        self.assertIn("in the Code reviewer (Seraph)'s lane now (they got a message)", handed)
        self.assertIn("handed “review the parser” back to the tech lead: needs a plan", declined)
        for out in (queued, handed, declined):
            self.assertEqual(bare(out), [], out)

    def test_the_roster_route_decisions_and_show_name_members_role_first(self):
        with State() as t:
            pg.start("Parser", plan=["parse"])
            t.launch()
            pg.crew_decision("Accept empty lines?", options="A: yes, B: no", from_agent="a1")
            roster = run_cli("crew")[1]
            route = run_cli("crew", "route", "build the JSON API for the orders service")[1]
            decisions = run_cli("crew", "decisions")[1]
            show = run_cli("crew", "show", "keymaker")[1]
        self.assertIn("Security engineer (The Keymaker)", roster)
        self.assertIn("Backend engineer (Tank)", route)
        self.assertIn("Senior engineer (Neo)", decisions)
        self.assertIn("Security engineer (The Keymaker)", show.strip().splitlines()[0])
        for out in (roster, route, decisions, show):
            self.assertEqual(bare(out), [], out)


class SessionContextTests(unittest.TestCase):
    # the session-start context: the catalog names who does and approves each kind of work role first
    def test_the_catalog_names_the_responsible_and_accountable_members_role_first(self):
        text = crew.catalog(ROSTER)
        self.assertIn("R Backend engineer (Tank) · A Code reviewer (Seraph)", text)
        self.assertEqual(bare(text), [])
        self.assertLessEqual(len(text), hooks.CATALOG_MAX)

    def test_the_route_hint_calls_the_session_the_tech_lead(self):
        text = hooks.morpheus_hint("build the JSON API server for the dashboard and implement the frontend live "
                                   "updates", ROSTER)
        self.assertTrue(text.startswith("devkit · you are the Tech lead (Morpheus): "), text[:80])
        self.assertEqual(bare(text), [])

    def test_a_scope_refusal_names_the_owner_role_first_and_asks_by_id(self):
        with State() as t, tempfile.TemporaryDirectory() as repo:
            pg.start("Feature", plan=["parser"])
            pg.assign("parser", "Neo", scope="lib/parser/**")
            t.launch("a1", "devkit:neo", "Neo: parser")
            verdict = hooks._crew_limits("claude", {"session_id": "s1", "tool_name": "Edit", "cwd": repo,
                                                    "tool_input": {"file_path": os.path.join(repo, "lib/parser/a.py")}})
        self.assertIn("belongs to Senior engineer (Neo) while they work", verdict[1])
        self.assertIn('dev crew ask --to neo "…"', verdict[1])


class MemberReportHookTests(TurnCase):
    # the SubagentStop hook's send-backs give the role form and the tech lead's report line, and a report that
    # still says "Needs from Morpheus:" is sent back once for the role
    def subagent_stop(self, aid, report):
        tr = Path(self.tmp.name) / f"agent-{aid}.jsonl"
        tr.write_text("")
        (Path(self.tmp.name) / f"agent-{aid}.meta.json").write_text(json.dumps({"toolUseId": f"toolu_{aid}",
                                                                              "description": "Tank: the API"}))
        data = dict(self.data, agent_id=aid, agent_type="general-purpose", agent_transcript_path=str(tr),
                    last_assistant_message=report)
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(data))), contextlib.redirect_stdout(out), \
                mock.patch.dict(core.CFG, {"crew": {"gates": {"enforce": False}}}):
            hooks.run("subagent-stop", "claude")
        return json.loads(out.getvalue()) if out.getvalue().strip() else {}

    def setUp(self):
        super().setUp()
        sf = hooks._session_file("claude", "s1")
        sf.parent.mkdir(parents=True, exist_ok=True)
        state.replace(sf, dict(self.sess, agent="claude"))

    def test_a_report_without_its_block_is_told_the_role_header_and_the_tech_leads_line(self):
        out = self.subagent_stop("a1", "Built the API.")
        self.assertIn("**[Firefly> DevKit · Backend engineer (Tank)]**", out["reason"])
        self.assertIn('"Needs from the tech lead:"', out["reason"])
        self.assertNotIn("Morpheus", out["reason"])

    def test_a_report_that_names_the_tech_lead_alone_is_sent_back_for_the_role(self):
        report = ("**[Firefly> DevKit · Backend engineer (Tank)]** api · `main` · the API\n- **Done:** built it\n"
                  "- **Check:** ✓ 12 tests\n- **Next:** review\n\nNeeds from Morpheus: nothing")
        out = self.subagent_stop("a1", report)
        self.assertEqual(out.get("decision"), "block")
        self.assertIn("Tech lead (Morpheus)", out["reason"])
        fixed = report.replace("Needs from Morpheus", "Needs from the tech lead")
        self.assertEqual(self.subagent_stop("a2", fixed), {})


class ContinuityTests(ContinuityCase):
    # what devkit carries through a compaction or /clear names the crew role first
    def test_the_brief_names_the_crew_and_the_senders_role_first(self):
        self.make_run()  # decision D1 from Tank, message M1 from Neo, lanes Tank (api) and Neo (ui)
        self.session(SID, status="")
        out = ct.brief("claude", SID, reason="manual", chat=True, provider="claude", now=T0)
        for needle in ("- Backend engineer (Tank) · step api", "- Senior engineer (Neo) · step ui",
                       "decision D1 from Backend engineer (Tank)", "M1 from Senior engineer (Neo)"):
            self.assertIn(needle, out)
        self.assertEqual(bare(out), [])

    def test_the_clear_notice_and_the_summary_instructions_name_the_crew_role_first(self):
        run = self.make_run()
        notice = ct._notice("claude", SID, T0)
        keep = ct._instructions(pg.normalize(run), {})
        self.assertIn("Running: Backend engineer (Tank) on step api, Senior engineer (Neo) on step ui", notice)
        self.assertIn("the crew working now: Backend engineer (Tank) on step api, Senior engineer (Neo) on step ui",
                      keep)
        for out in (notice, keep):
            self.assertEqual(bare(out), [], out)

    def test_a_members_reminder_names_it_role_first_and_ends_with_the_tech_leads_line(self):
        self.make_run(lanes=("Tank", "Kid"))
        text = ct._reminder("claude", SID, "a-tank", "")
        self.assertIn("You are the crew's Backend engineer (Tank), working for the tech lead.", text)
        self.assertIn("Needs from the tech lead: <what you need, or nothing>.", text)
        self.assertEqual(bare(text), [])


if __name__ == "__main__":
    unittest.main()
