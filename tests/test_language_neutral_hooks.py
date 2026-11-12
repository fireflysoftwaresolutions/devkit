# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# C13 in the hooks (crew gap review 2, R2-1 and R2-2; plan step 62 "language-neutral hooks"): the session gets the
# catalog of ids it declares; lessons are recalled just in time from what a call touches and what its output shows,
# never from words; words never refuse an assignment; devkit's own headless calls get no hook work; a lane that has
# not met its launch yet may only read; and every refusal the hooks record names the crew member it refused.
# Run: python3 -m unittest tests.test_language_neutral_hooks
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import audit, crew, hooks, lessons, progress as pg, raci, work  # noqa: E402
from test_progress import TempState  # noqa: E402

A = {"user": "ana", "host": "mbp1", "agent": "claude", "session": "s1"}
TEAMLESS = crew.load


def roster():
    return TEAMLESS(team=False)


class HookCase(unittest.TestCase):
    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.root = Path(self.state.tmp.name)
        self.handoff = tempfile.TemporaryDirectory()
        self.addCleanup(self.handoff.cleanup)
        h = Path(self.handoff.name)
        for p in (mock.patch.object(hooks, "STATE", self.root), mock.patch.object(pg, "_ci", lambda *a: ""),
                  mock.patch.object(lessons, "LESSONS", h / "lessons"), mock.patch.object(work, "HANDOFF", h),
                  mock.patch.object(lessons, "USAGE_LOCAL", h / "usage.json"),
                  mock.patch.object(crew, "load", lambda *a, **k: TEAMLESS(team=False))):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("DEVKIT_HEADLESS", None)

    def hook(self, event, payload, real_guard=False):
        out = io.StringIO()
        guard_check = mock.patch("devkit.guard.check", lambda *a, **k: None) if not real_guard else \
            contextlib.nullcontext()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out), guard_check:
            hooks.run(event, "claude")
        return out.getvalue()

    def context(self, out):
        return json.loads(out)["hookSpecificOutput"].get("additionalContext") or "" if out.strip() else ""

    def lesson(self, title, **anchors):
        return lessons.add("acme/api", title, cause="cause of " + title, prevention="prevention of " + title, a=A,
                           happened="it happened: " + title,
                           **anchors)["meta"]["id"]

    def bash(self, cmd, aid=None, tid="toolu_1"):
        d = {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_use_id": tid,
             "tool_input": {"command": cmd}, "cwd": str(self.root)}
        if aid:
            d["agent_id"] = aid
        return d


class CatalogTests(HookCase):
    def test_session_start_gives_the_catalog_with_every_kind_of_work(self):
        ctx = "## devkit · workspace context\nProject: **acme/api**\nHandoff note: x\n\n--- handoff note ---\nNOTE"
        with mock.patch.object(hooks, "context_for", lambda cwd, agent: ctx), \
                mock.patch.object(hooks, "_spawn", lambda *a, **k: None), \
                mock.patch("devkit.work.request_sync", lambda *a, **k: None):
            out = self.hook("session-start", {"session_id": "s9", "hook_event_name": "SessionStart",
                                              "source": "startup", "cwd": str(self.root)})
        for a in raci.activities(roster()):
            self.assertIn(f"`{a['id']}`", out)
        self.assertLess(out.index("devkit catalog"), out.index("--- handoff note ---"))  # the note may be cut, not it

    def test_the_catalog_is_bounded(self):
        many = [{"id": f"kind{i}", "name": "a kind of work with a long description to fill the line"}
                for i in range(500)]
        with mock.patch.object(raci, "activities", lambda *a, **k: many):
            text = hooks.catalog_context("claude")
        self.assertLessEqual(len(text), hooks.CATALOG_MAX)
        self.assertIn("`kind0`", text)
        self.assertRegex(text, r"\n- … \d+ more: `dev crew raci`\n")  # the cut says how many kinds it left out
        self.assertIn("dev crew raci", text.splitlines()[-1])

    def test_the_catalog_is_an_index_that_points_to_the_rest(self):
        text = hooks.catalog_context("claude")
        self.assertIn("`dev crew raci", text)
        self.assertIn("`dev crew rules`", text)
        for flag in ("--activity", "--file", "--command", "--error", "--stack"):  # where a lesson applies
            self.assertIn(flag, text)
        for detail in (" · R ", " · A ", "Skills the files you change"):  # those stay behind the commands
            self.assertNotIn(detail, text)


class LessonRecallTests(HookCase):
    def test_pre_tool_use_names_the_lesson_a_command_is_anchored_on(self):
        lid = self.lesson("Repaired the schema history instead of fixing the migration",
                          commands=["./mvnw flyway:repair"])
        out = self.hook("guard", self.bash("./mvnw flyway:repair -Dflyway.cleanDisabled=true", aid="a_tank"))
        self.assertIn(lid, self.context(out))
        self.assertEqual(self.hook("guard", self.bash("./mvnw flyway:repair", aid="a_tank", tid="toolu_2")), "")

    def test_a_refusal_names_the_lesson_of_the_call_it_refuses(self):
        lid = self.lesson("Force push lost a teammate's commits", commands=["git push --force"])
        for aid in (None, "a_tank"):  # the guard refuses it to everyone, and names the lesson each time
            with self.subTest(aid=aid):
                denied = json.loads(self.hook("guard", self.bash("git push --force origin y", aid=aid),
                                              real_guard=True))["hookSpecificOutput"]
                self.assertEqual(denied["permissionDecision"], "deny")
                self.assertIn(lid, denied["permissionDecisionReason"])

    def test_an_edit_names_the_lesson_its_file_is_anchored_on(self):
        lid = self.lesson("Edited an applied migration", files=["db/migrations/**"])
        out = self.hook("guard", {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Edit",
                                  "tool_use_id": "toolu_e", "cwd": str(self.root), "tool_input": {
                                      "file_path": str(self.root / "db" / "migrations" / "V3__orders.sql"),
                                      "old_string": "a", "new_string": "b"}})
        self.assertIn(lid, self.context(out))

    def test_post_tool_use_names_the_lesson_its_output_shows(self):
        lid = self.lesson("Edited an applied migration", errors=["checksum mismatch"])
        out = self.hook("post-tool", dict(self.bash("./mvnw verify"), hook_event_name="PostToolUse", tool_response={
            "stdout": "[ERROR] Validate failed: Migration checksum mismatch for migration version 3", "stderr": ""}))
        self.assertIn(lid, self.context(out))
        db = self.lesson("Ran the integration tests without Docker", errors=["Could not find a valid Docker"])
        failed = self.hook("post-tool-failure", dict(self.bash("./mvnw verify", tid="toolu_9"), error="Exit code 1\n"
                                                     "IllegalStateException: Could not find a valid Docker environment",
                                                     hook_event_name="PostToolUseFailure"))
        self.assertEqual(json.loads(failed)["hookSpecificOutput"]["hookEventName"], "PostToolUseFailure")
        self.assertIn(db, self.context(failed))
        self.assertNotIn(lid, self.context(failed))  # recalled a moment ago: once an hour is enough
        self.assertNotIn(lid, self.context(self.hook("post-tool", dict(
            self.bash("./mvnw verify", tid="toolu_10"), hook_event_name="PostToolUse",
            tool_response={"stdout": "BUILD SUCCESS", "stderr": ""}))))


class HeadlessTests(HookCase):
    def test_hooks_do_nothing_in_a_headless_call(self):
        with mock.patch.dict(os.environ, {"DEVKIT_HEADLESS": "1"}):
            for event in ("session-start", "prompt", "post-tool", "stop", "subagent-start", "subagent-stop"):
                with self.subTest(event=event):
                    self.assertEqual(self.hook(event, {"session_id": "s7", "cwd": str(self.root), "prompt": "hi"}),
                                     "")
            self.assertFalse((self.root / "sessions" / "claude-s7.json").exists())
            out = self.hook("guard", self.bash("echo '{}' | dev hook prompt --agent claude"), real_guard=True)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")  # the guard still judges


class AssignmentTests(unittest.TestCase):
    def test_words_never_refuse_an_assignment(self):
        r = roster()
        self.assertIsNone(raci.check_assignment("ghost", "add the login API endpoint with tests", r))
        self.assertIsNone(raci.check_assignment("ghost", "añade el endpoint de login con pruebas", r))
        self.assertIsNotNone(raci.check_assignment("ghost", "x", r, activity="backend"))  # a declared kind decides

    def test_an_undeclared_step_takes_its_members_own_kind_of_work_in_any_language(self):
        r = roster()  # so its approver is the member's kind's, whatever the words name
        for text in ("add the login API endpoint with tests", "añade el endpoint de login con pruebas"):
            with self.subTest(text=text):
                self.assertEqual(raci.anchor(text, member="ghost", roster=r), (raci.activity("docs", r), "member"))


class LaneTests(HookCase):
    def test_a_lane_waiting_for_its_launch_is_read_only(self):
        pg.start("Work", plan=["parser"])
        pg.note_agent_launch("claude", {"session_id": "s1", "tool_use_id": "toolu_neo",
                                        "tool_input": {"description": "Neo: parser", "subagent_type": "general-purpose"}})
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "h1", "agent_type": "general-purpose"}, True)
        run = pg.load_run("claude", "s1")
        self.assertEqual(run["agents"]["h1"]["binding"], "pending")
        self.assertEqual((pg.lane_tools(run, "h1"), pg.lane_scope(run, "h1")), ("read", []))
        why = hooks._scope_violation("claude", {"session_id": "s1", "agent_id": "h1", "tool_name": "Edit",
                                                "cwd": str(self.root), "tool_input": {"file_path": "lib/x.py"}})
        self.assertIn("read", why or "")


class RefusalRecordTests(HookCase):
    def test_guard_refusals_name_the_crew_member(self):
        pg.start("Work", plan=["parser"])
        self.hook("guard", self.bash("echo '{}' | dev hook prompt --agent claude", aid="a_tank"), real_guard=True)
        # a lane call that drops the member's lane variable stays refused for a crew member, and its refusal names
        # the member's lane; a lane call inside a substitution runs with the lane stamped (Claude Code) since the
        # stamp exists, so it records no refusal
        self.hook("guard", self.bash("env -i dev progress step x --check y", aid="a_neo", tid="toolu_2"))
        self.hook("guard", self.bash('echo "$(dev progress step x --check y)"', aid="a_mouse", tid="toolu_3"))
        lanes = {(r.get("crew") or {}).get("lane") for r in audit.records(1, action="guard.deny")}
        self.assertTrue({"a_tank", "a_neo"} <= lanes, lanes)
        self.assertNotIn("a_mouse", lanes)


class RouteHintTests(HookCase):
    # the prompt hook reminds the tech lead to declare the kinds of work by catalog id, the same for a Spanish
    # request as for an English one, and guesses no route from the words
    def test_the_prompt_reminds_the_tech_lead_the_same_in_every_language(self):
        def reminder(sid, prompt):
            ctx = self.context(self.hook("prompt", {"session_id": sid, "hook_event_name": "UserPromptSubmit",
                                                    "cwd": str(self.root), "prompt": prompt}))
            return next((p for p in ctx.split("\n\n") if "you are the Tech lead (Morpheus)" in p), "")
        en = reminder("s1", "build the JSON API server for the dashboard and implement the frontend live updates")
        es = reminder("s2", "construye el servidor de la API JSON del panel y la parte de interfaz en vivo")
        self.assertIn("declare the kinds of work in this request by their ids from the catalog", en)
        self.assertEqual(en, es)
        self.assertNotIn("Backend engineer (Tank)", en)


if __name__ == "__main__":
    unittest.main()
