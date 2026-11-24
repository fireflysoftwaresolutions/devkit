# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Who does what: routing, delegation between crew members, declines and dispatch of ready work."""
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _hermetic  # noqa: E402

_hermetic.activate()  # a probe that imports this module by path gets the suite's temporary home too

from devkit import audit, dispatch, messaging  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_progress import TempState, member_call, repo_with_worktrees  # noqa: E402

T0 = 1_800_000_000.0
SECRET = "ghp_" + "a1B2" * 10  # the shape of a GitHub token, which a message body may not hold


def m(minutes):
    return T0 + minutes * 60


def launch(aid, kind, label, at):
    # Claude Code's sequence: the Agent call (PreToolUse), the agent's start, the call's result naming the agent
    pg.note_agent_launch("claude", {"session_id": "s1", "tool_use_id": f"toolu_{aid}",
                                    "tool_input": {"description": label, "subagent_type": kind}}, now=at)
    pg.on_subagent("claude", {"session_id": "s1", "agent_id": aid, "agent_type": kind}, True, now=at)
    pg.launch_result("claude", {"session_id": "s1", "tool_use_id": f"toolu_{aid}", "tool_response": {"agentId": aid}},
                     now=at)


class RouteTests(unittest.TestCase):
    def best(self, task, files=None):
        return dispatch.route(task, files)[0][0]["id"]

    def test_routes_by_role_and_files(self):
        self.assertEqual(self.best("add a database migration for the invoices table"), "dozer")
        self.assertEqual(self.best("the CI pipeline is slow and flaky"), "link")
        self.assertEqual(self.best("threat model for the new login and secrets handling"), "keymaker")
        self.assertEqual(self.best("review this change before the merge"), "seraph")
        self.assertEqual(self.best("change the button", files=["web/src/app/Button.tsx"]), "trinity")
        self.assertEqual(self.best("update it", files=["terraform/main.tf"]), "niobe")
        self.assertEqual(self.best("write the runbook and release notes"), "ghost")
        self.assertEqual(self.best("something vague nobody owns"), "neo")  # the generalist

    # Kotlin, Markdown and PHP or Python service code route to the member whose skills their rules require: a Spring
    # Kotlin service to the backend engineer (Spring and backend skills), an Android activity to the mobile
    # engineer, a plain note to nobody by its files, and PHP and Python services to the backend engineer with the
    # backend skill
    def test_files_route_to_the_member_their_skill_rules_fit(self):
        from devkit import skillguard
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".git").mkdir()
            files = {"src/main/kotlin/acme/OrderService.kt": "import org.springframework.stereotype.Service\n",
                     "app/src/main/java/com/acme/MainActivity.kt": "import android.app.Activity\n",
                     "routes/api.php": "<?php\nRoute::get('/users', fn () => []);\n",
                     "service/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
                     "notes/ideas.md": "# Ideas\n", "docs/guide.md": "# Guide\n"}
            for rel, text in files.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(text)
            routed = {rel: self.best("update it", files=[rel]) for rel in files}
            needs = {rel: sorted({s for r, _ in skillguard.rules_for_files([str(root / rel)])
                                  for s in skillguard.required(r, "claude")}) for rel in files}
        self.assertEqual(routed, {"src/main/kotlin/acme/OrderService.kt": "tank",
                                  "app/src/main/java/com/acme/MainActivity.kt": "switch", "routes/api.php": "tank",
                                  "service/main.py": "tank", "notes/ideas.md": "neo", "docs/guide.md": "ghost"})
        self.assertEqual({rel: needs[rel] for rel in ("src/main/kotlin/acme/OrderService.kt", "routes/api.php",
                                                      "service/main.py", "docs/guide.md")},
                         {"src/main/kotlin/acme/OrderService.kt": ["backend", "java-springboot"],
                          "routes/api.php": ["backend"], "service/main.py": ["backend"], "docs/guide.md": ["docs"]})

    # the Markdown the technical writer is routed is the Markdown the docs skill rule covers, and the other way round
    def test_the_writers_markdown_is_the_docs_rules(self):
        from devkit import skillguard
        docs = next(r for r in skillguard.rules() if r["id"] == "docs")["files"]
        writer = next(globs for globs, who in dispatch.FILE_HINTS if who == "ghost")
        self.assertEqual(sorted(g for g in writer if g.endswith((".md", ".mdx")) or "/" in g and g != "docs/**"),
                         sorted(g for g in docs if g.endswith((".md", ".mdx")) or "/" in g))

    def test_reasons_are_given(self):
        member, score, why = dispatch.route("add a database migration")[0]
        self.assertGreater(score, 0)
        self.assertTrue(any("migration" in w for w in why), why)


class DelegationTests(unittest.TestCase):
    def test_delegate_to_a_working_member_goes_to_its_lane(self):
        with TempState():
            pg.start("Feature", plan=["parser=30m"], now=m(0))
            launch("a1", "devkit:neo", "Neo: parser", m(1))
            launch("a2", "devkit:dozer", "Dozer: schema", m(1))
            r = dispatch.delegate("add a nullable column for the export date", to="Dozer", from_agent="a1",
                                  scope="db/**", now=m(2))
            self.assertEqual(r["member"]["id"], "dozer")
            self.assertEqual(r["lane"], "a2")
            run = pg.load_run("claude", "s1")
            step = run["plan"][-1]
            self.assertEqual((step["owner"], step["state"], step["requested_by"]), ("a2", "running", "a1"))
            self.assertIn("db/**", run["agents"]["a2"]["scope"])
            mail = pg.on_tool_done("claude", {"session_id": "s1", "agent_id": "a2"})
            self.assertIn("Senior engineer (Neo) delegates", mail)
            self.assertIn("preview; read: dev crew read", mail)
            body = pg.crew_read("M1", from_agent="a2")
            self.assertTrue(body["complete"])
            self.assertIn("dev crew decline", body["text"])

    def test_delegate_keeps_an_absolute_scope_repository_relative(self):
        # like dev progress assign (L-fzi4pkv6bbh): the edit advice matches a member's files relative to the checkout
        # that holds them, so an absolute scope inside one is kept in that form, and one devkit cannot place is refused
        with TempState(), tempfile.TemporaryDirectory() as d:
            main, wt = repo_with_worktrees(d)
            pg.start("Feature", plan=["parser=30m"], now=m(0))
            launch("a1", "devkit:neo", "Neo: parser", m(1))
            launch("a2", "devkit:dozer", "Dozer: schema", m(1))
            dispatch.delegate("add a nullable column for the export date", to="Dozer", from_agent="a1",
                              scope=f"{wt}/db/**", cwd=str(main), now=m(2))
            run = pg.load_run("claude", "s1")
            self.assertEqual(run["plan"][-1]["scope"], ["db/**"])
            self.assertIn("db/**", run["agents"]["a2"]["scope"])
            told = [x["text"] for x in run.get("messages") or [] if x["from"] == "devkit"]
            self.assertTrue(any(f"{wt}/db/** → db/**" in t for t in told), told)
            with self.assertRaises(pg.StepError) as e:
                dispatch.delegate("index the export date", to="Dozer", from_agent="a1", scope=f"{d}/**",
                                  cwd=str(main), now=m(3))
            self.assertIn("2 checkouts", str(e.exception))
            self.assertEqual(len(pg.load_run("claude", "s1")["plan"]), 2)  # the refused step was not added

    def test_delegate_to_an_idle_member_asks_morpheus_to_launch_it(self):
        with TempState():
            pg.start("Feature", plan=["parser=30m"], now=m(0))
            launch("a1", "devkit:neo", "Neo: parser", m(1))
            r = dispatch.delegate("review the parser before merge", to="auto", from_agent="a1", now=m(2))
            self.assertEqual(r["member"]["id"], "seraph")
            self.assertIsNone(r["lane"])
            ctx = pg.on_tool_done("claude", {"session_id": "s1"})
            self.assertIn("Senior engineer (Neo) delegates", ctx)
            self.assertIn("devkit · ready:", ctx)
            self.assertIn("dev crew brief seraph", ctx)
            self.assertNotIn("devkit · ready:", pg.on_tool_done("claude", {"session_id": "s1"}) or "")  # told once

    def test_a_step_back_to_pending_is_announced_ready_to_launch_again(self):
        with TempState():
            pg.start("Feature", plan=["schema=30m"], now=m(0))
            pg.assign("schema", "Dozer", now=m(0))
            self.assertIn("schema", dispatch.ready_context("claude", "s1"))
            pg.begin("schema", now=m(1))
            pg.step("schema migrated", which="schema", check="✓ migration runs", now=m(20))
            pg.reopen("schema", "the column type is wrong", now=m(21))  # back to pending, still the Dozer's
            self.assertIn("schema", dispatch.ready_context("claude", "s1"))

    def test_delegate_and_decline_notices_never_wait_for_the_tech_leads_answer(self):
        with TempState():
            pg.start("Feature", plan=["parser=30m"], now=m(0))
            launch("a1", "devkit:neo", "Neo: parser", m(1))
            launch("a2", "devkit:dozer", "Dozer: schema", m(1))
            dispatch.delegate("review the parser before merge", to="auto", from_agent="a1", now=m(2))
            step = dispatch.delegate("add the export column", to="Dozer", now=m(2))["step"]["name"]
            dispatch.decline(step, "it needs a data migration plan first", from_agent="a2", now=m(3))
            pg.on_tool_done("claude", {"session_id": "s1"})  # both delivered to the tech lead
            self.assertEqual(pg.waiting_messages(pg.load_run("claude", "s1")), [])

    def test_dependencies_and_capacity_decide_what_is_ready(self):
        with TempState():
            pg.start("Feature", plan=["design=30m", "api=1h after design", "ui=1h after design"], now=m(0))
            pg.assign("api", "Tank", now=m(0))
            pg.assign("ui", "Trinity", now=m(0))
            r = dispatch.ready(pg.load_run("claude", "s1"))
            self.assertEqual(r["launch"], [])
            self.assertEqual(sorted(w for _, w in r["waiting"]), ["after design", "after design"])
            pg.step("design done", check="ADR-12 written", now=m(30))
            r = dispatch.ready(pg.load_run("claude", "s1"))
            self.assertEqual(sorted(s["name"] for s in r["launch"]), ["api", "ui"])  # both, in parallel
            for i, who in enumerate(("neo", "mouse", "dozer", "link")):  # all four slots busy
                launch(f"b{i}", f"devkit:{who}", f"{who.title()}: x", m(31))
            r = dispatch.ready(pg.load_run("claude", "s1"))
            self.assertEqual((r["launch"], len(r["queued"])), ([], 2))

    def test_a_plan_with_several_ready_steps_and_free_slots_says_to_launch_their_members_in_parallel(self):
        # the tech lead used 2 of 10 slots at kickoff and ran 5 small steps one after another: dev progress start and
        # dev progress add print the ready steps with the member the RACI makes Responsible for each
        with TempState():
            run = pg.start("Feature", plan=["api=30m [backend]", "ui=30m [frontend]", "docs=20m after api [docs]"],
                           now=m(0))
            text = "\n".join(dispatch.parallel_hint(run, now=m(0)))
            self.assertIn("2 steps are ready", text)
            self.assertIn("launch them in parallel, in one message", text)
            self.assertIn("api · Backend engineer (Tank) · dev progress assign api --to tank --activity backend", text)
            self.assertIn("ui · Frontend engineer (Trinity) · dev progress assign ui --to trinity --activity frontend",
                          text)
            self.assertNotIn("docs", text)  # it waits for api
            for i, who in enumerate(("neo", "mouse", "dozer", "link")):  # every slot busy: nothing to launch now
                launch(f"b{i}", f"devkit:{who}", f"{who.title()}: x", m(1))
            self.assertEqual(dispatch.parallel_hint(pg.load_run("claude", "s1"), now=m(1)), [])

    def test_the_parallel_hint_reads_only_a_declared_kind_of_work_as_the_tech_lead_starts_steps(self):
        # no step declares its kind of work, so they are the tech lead's own and the first one runs as his; the hint
        # gave all three, the running one too, to the members their words suggest
        with TempState():
            run = pg.start("Feature", plan=["build the REST API endpoint=30m", "build the React component=30m",
                                            "write the README docs=20m"], now=m(0))
            self.assertEqual(run["plan"][0]["state"], "running")
            self.assertEqual(dispatch.parallel_hint(run, now=m(0)), [])

    def test_the_parallel_hint_leaves_out_a_step_the_tech_lead_began(self):
        with TempState():
            pg.start("Feature", plan=["api=30m [backend]", "ui=30m [frontend]", "docs=20m [docs]"], now=m(0))
            run = pg.begin("api", now=m(1))
            text = "\n".join(dispatch.parallel_hint(run, now=m(1)))
            self.assertIn("2 steps are ready", text)
            self.assertNotIn("api ·", text)
            self.assertIn("ui · Frontend engineer (Trinity)", text)
            self.assertIn("docs · Technical writer (Ghost)", text)

    def test_a_parallel_hint_that_fails_prints_nothing_after_the_run_is_saved(self):
        # dev progress start prints the hint after it saved the run: an error in it must not fail the command
        with TempState():
            run = pg.start("Feature", plan=["api=30m [backend]", "ui=30m [frontend]"], now=m(0))
            with mock.patch.object(pg, "slot_budget", side_effect=OSError("the quota record cannot be read")):
                self.assertEqual(dispatch.parallel_hint(run, now=m(0)), [])

    def test_one_ready_step_needs_no_parallel_hint(self):
        with TempState():
            run = pg.start("Feature", plan=["api=30m [backend]", "docs=20m after api [docs]"], now=m(0))
            self.assertEqual(dispatch.parallel_hint(run, now=m(0)), [])

    def test_decline_returns_the_step_to_morpheus(self):
        with TempState():
            pg.start("Feature", plan=["parser=30m"], now=m(0))
            launch("a2", "devkit:dozer", "Dozer: schema", m(1))
            dispatch.delegate("add the export column", to="Dozer", now=m(2))
            step = pg.load_run("claude", "s1")["plan"][-1]["name"]
            with self.assertRaises(pg.StepError):
                dispatch.decline(step, "", from_agent="a2", now=m(3))  # a reason is required
            s = dispatch.decline(step, "it needs a data migration plan first", from_agent="a2", now=m(3))
            self.assertEqual((s["state"], s["owner"], s["label"]), ("pending", "main", ""))
            ctx = pg.on_tool_done("claude", {"session_id": "s1"})
            self.assertIn("declined", ctx)

    def test_a_delegation_whose_notice_is_refused_saves_nothing_however_often_it_is_tried(self):
        # the step was saved before the notice failed (here a secret in the task), so nobody was told of it and a
        # second try added “… (2)”
        with TempState():
            pg.start("Feature", plan=["parser=30m"], now=m(0))
            launch("a1", "devkit:neo", "Neo: parser", m(1))
            launch("a2", "devkit:dozer", "Dozer: schema", m(1))
            sent = len(pg.load_run("claude", "s1").get("messages") or [])
            for i in range(2):
                with self.assertRaisesRegex(messaging.MessageError, "secret"):
                    dispatch.delegate(f"rotate the key {SECRET}", to="Dozer", from_agent="a1", now=m(2 + i))
            run = pg.load_run("claude", "s1")
            self.assertEqual([s["name"] for s in run["plan"]], ["parser"])
            self.assertEqual(len(run.get("messages") or []), sent)
            self.assertEqual(audit.query(action="crew.delegate"), [])

    def test_a_delegated_task_with_a_secret_past_what_its_notice_quotes_is_refused_whole(self):
        # the notice quotes the task's first 200 characters, which alone were checked, while the step keeps 1,500
        with TempState():
            pg.start("Feature", plan=["parser=30m"], now=m(0))
            launch("a1", "devkit:neo", "Neo: parser", m(1))
            launch("a2", "devkit:dozer", "Dozer: schema", m(1))
            task = "add the export column " + "and its index " * 15 + SECRET
            with self.assertRaisesRegex(messaging.MessageError, "secret"):
                dispatch.delegate(task, to="Dozer", from_agent="a1", now=m(2))
            self.assertEqual([s["name"] for s in pg.load_run("claude", "s1")["plan"]], ["parser"])

    def test_a_delegated_task_with_a_secret_is_refused_when_nobody_is_told(self):
        # the tech lead delegates to a member with no lane at work: no notice, so nothing checked the saved task
        with TempState():
            pg.start("Feature", plan=["parser=30m"], now=m(0))
            with self.assertRaisesRegex(messaging.MessageError, "secret"):
                dispatch.delegate(f"rotate the key {SECRET}", to="Dozer", now=m(2))
            self.assertEqual([s["name"] for s in pg.load_run("claude", "s1")["plan"]], ["parser"])

    def test_a_delegation_refused_by_a_full_message_store_leaves_no_record_of_the_claim_it_never_saved(self):
        # the Database engineer's free lane claimed the step, and the audit trail kept that claim, before the notice
        # to the lane was refused: a claim of a step the run never saved
        with TempState():
            pg.start("Feature", plan=["parser=30m"], now=m(0))
            launch("a1", "devkit:neo", "Neo: parser", m(1))
            launch("a2", "devkit:dozer", "Dozer: schema", m(1))
            claims = len(audit.query(action="crew.claim"))
            full = len(messaging.encoded(pg.load_run("claude", "s1")))
            with mock.patch.object(messaging, "STORE_MAX", full), \
                    self.assertRaisesRegex(messaging.MessageError, "capacity"):
                dispatch.delegate("add the export column", to="Dozer", from_agent="a1", now=m(2))
            self.assertEqual([s["name"] for s in pg.load_run("claude", "s1")["plan"]], ["parser"])
            self.assertEqual(len(audit.query(action="crew.claim")), claims)
            dispatch.delegate("add the export column", to="Dozer", from_agent="a1", now=m(3))  # saved: recorded
            self.assertEqual(len(audit.query(action="crew.claim")), claims + 1)

    def test_a_delegated_scope_with_a_secret_is_refused_and_saves_nothing(self):
        # --scope was saved unchecked: a token in a URL went into the run as the step's scope
        with TempState():
            pg.start("Feature", plan=["parser=30m"], now=m(0))
            with self.assertRaises(pg.StepError) as e:
                dispatch.delegate("add the export column", to="Ghost", scope=f"https://u:{SECRET}@h/x", now=m(2))
            self.assertNotIn(SECRET, str(e.exception))
            run = pg.load_run("claude", "s1")
            self.assertEqual([s["name"] for s in run["plan"]], ["parser"])
            self.assertNotIn(SECRET, json.dumps(run))

    def test_a_delegated_scope_that_is_not_a_repository_path_is_refused(self):
        with TempState(), tempfile.TemporaryDirectory() as d:
            pg.start("Feature", plan=["parser=30m"], now=m(0))
            for scope in ("https://example.com/x", "git@host:org/repo", "../other/**", "lib/**, docs/a\nb.md",
                          f"{d}/notes/**"):
                with self.subTest(scope=scope), self.assertRaisesRegex(pg.StepError, "repository-relative"):
                    dispatch.delegate("add the export column", to="Ghost", scope=scope, now=m(2))
            self.assertEqual([s["name"] for s in pg.load_run("claude", "s1")["plan"]], ["parser"])

    def test_a_decline_whose_notice_is_refused_leaves_the_step_with_its_member(self):
        # the step went back to the tech lead before the notice failed, so the tech lead was never told
        with TempState():
            pg.start("Feature", plan=["parser=30m"], now=m(0))
            launch("a2", "devkit:dozer", "Dozer: schema", m(1))
            dispatch.delegate("add the export column", to="Dozer", now=m(2))
            step = pg.load_run("claude", "s1")["plan"][-1]["name"]
            with self.assertRaisesRegex(messaging.MessageError, "secret"):
                dispatch.decline(step, f"the key {SECRET} is wrong", from_agent="a2", now=m(3))
            s = pg.load_run("claude", "s1")["plan"][-1]
            self.assertEqual((s["owner"], s["state"], s["label"]), ("a2", "running", "Dozer"))
            self.assertEqual(audit.query(action="crew.decline"), [])

    def test_a_forged_lane_variable_cannot_decline_another_lanes_step(self):
        # a member's child shell gave the lane variable the owner's id; no open call of that lane backs it, so the
        # decline acts for no lane and the step stays the owner's. The owner, with its call open, declines it
        with TempState():
            pg.start("Feature", plan=["parser=30m"], now=m(0))
            launch("a2", "devkit:dozer", "Dozer: schema", m(1))
            launch("a3", "devkit:neo", "Neo: parser", m(1))
            dispatch.delegate("add the export column", to="Dozer", now=m(2))
            step = pg.load_run("claude", "s1")["plan"][-1]["name"]
            with member_call("a3"), mock.patch.dict(os.environ, {"DEVKIT_FROM_AGENT": "a2"}):
                with self.assertRaises(ValueError):
                    dispatch.decline(step, "not mine to keep", from_agent=os.environ["DEVKIT_FROM_AGENT"], now=m(3))
            self.assertNotEqual(pg.load_run("claude", "s1")["plan"][-1]["owner"], "main")  # still the owner's
            with member_call("a2"):
                s = dispatch.decline(step, "it needs a migration plan", from_agent=os.environ["DEVKIT_FROM_AGENT"],
                                     now=m(4))
            self.assertEqual((s["state"], s["owner"]), ("pending", "main"))

    def test_a_command_that_holds_its_audit_records_holds_none_of_another_threads(self):
        # the held records were one list for the whole process: a record another thread wrote during a delegation
        # waited for that command's end, and was dropped with the records of a delegation that was refused
        with TempState():
            seen = None
            with self.assertRaises(RuntimeError):
                with pg._audits_held():
                    t = threading.Thread(target=pg._audit, args=("crew.delegate", "from another thread", "x"))
                    t.start()
                    t.join()
                    seen = [r["target"] for r in audit.query(action="crew.delegate")]
                    raise RuntimeError("the notice was refused")
            self.assertEqual(seen, ["from another thread"])
            self.assertEqual([r["target"] for r in audit.query(action="crew.delegate")], ["from another thread"])

    def test_route_the_whole_plan(self):
        with TempState():
            pg.start("Feature", plan=["requirements and acceptance criteria", "database migration",
                                      "pipeline caching"], now=m(0))
            out = dispatch.assign_plan(apply=True, now=m(1))
            self.assertEqual([x[1]["id"] for x in out], ["oracle", "dozer", "link"])
            run = pg.load_run("claude", "s1")
            self.assertEqual([s["label"] for s in run["plan"]], ["The Oracle", "Dozer", "Link"])


if __name__ == "__main__":
    unittest.main()
