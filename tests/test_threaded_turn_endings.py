# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Threaded turn endings (spec P-fxtfdwpv9k9): what the Stop gate checks reaches the agent before the turn ends, so
# the Stop hook does not have to send the turn back. R1 one checklist from the Stop gate's own functions; R2 given at
# the closing calls and at every commit, once per item per turn; R3 the handoff gate blocks only at 5 commits, an
# hour, or a milestone, and never a member; R4 every block audited as stop.block and every reminder the turn then
# satisfied as stop.prevented, shown per rule per day by `dev canary`; R5 the status block stays enforced. Also the
# developer's role rule (M346): a final message or a member's report that names a crew member outside
# `<role> (<Name>)` is sent back once per turn (per member for reports); the detection itself is tests/test_canary.py.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_threaded_turn_endings
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

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / "lib"))
sys.path.insert(0, str(TESTS))

from devkit import audit, canary, core, hooks, skillguard, state  # noqa: E402
from test_handoff_check import NOTE, commit  # noqa: E402
from test_language_neutral_hooks import HookCase  # noqa: E402

EMAIL = "ana@example.com"
DONE = ("**[Firefly> DevKit]** api · `main` · export\n- **Done:** the export.\n- **Check:** ✓ 12 tests\n"
        "- **Next:** the docs.")


class TurnCase(HookCase):
    # HookCase's temporary state (progress, audit, hooks.STATE, lessons), plus a repository with the developer's
    # identity, the project's handoff note written three hours ago, and the canary's records in the test's folder
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()  # as git names it: /private/var/…
        self.repo, self.note = root / "api", root / "acme__api.md"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", EMAIL], check=True)
        self.note.write_text(NOTE)
        self.now = time.time()
        self.t0 = int(self.now) - 3600 * 3  # the note was last written three hours ago
        os.utime(self.note, (self.t0, self.t0))
        for p in (mock.patch.object(hooks, "HANDOFF_CHECK", root / "handoff-check"),
                  mock.patch.object(hooks, "_session_project", lambda data, sess: "acme/api"),
                  mock.patch.object(core, "note_path", lambda pid: self.note),
                  mock.patch.object(canary, "LOG", root / "canary.jsonl"),
                  mock.patch.object(hooks, "HANDOFF", root / "handoff"),
                  mock.patch("devkit.work.request_sync", lambda *a, **k: None),
                  # the repository sits in a temporary folder, which devkit skips everywhere else
                  mock.patch.object(core, "is_temp", lambda p, real=core.is_temp: False if str(p).startswith(
                      str(self.repo)) else real(p)),
                  mock.patch.object(audit, "policy_file", lambda: root / "no-policy.json"),
                  mock.patch.dict(core.CFG, {"handoff": {"enforce": True}, "audit": {}})):
            p.start()
            self.addCleanup(p.stop)
        self.sess = {"project": "acme/api", "top": str(self.repo), "turn_started": self.now - 600,
                     "turn_class": "dev", "session": "s1"}
        self.data = {"session_id": "s1", "cwd": str(self.repo)}

    def commits(self, n, minutes_ago=5):
        for i in range(n):
            commit(self.repo, f"feat: part {i}", int(self.now) - minutes_ago * 60 + i, email=EMAIL)

    def shell(self, cmd):
        return dict(self.data, tool_name="Bash", tool_input={"command": cmd})

    def stop(self, message, told=(), carried=True, again=False):
        # the Stop hook on this turn; `carried`: whether the final message carried the status block (the canary's
        # verdict, which skips folders outside ~/Development); `again`: a later stop of the same turn, on the session
        # record the first one left
        sf = hooks._session_file("claude", "s1")
        sf.parent.mkdir(parents=True, exist_ok=True)
        if not again:
            state.replace(sf, dict(self.sess, agent="claude",
                                   checklist_told={"turn": self.sess["turn_started"], "rules": list(told)}))
        payload = dict(self.data, hook_event_name="Stop", last_assistant_message=message, stop_hook_active=False)
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out), \
                mock.patch("devkit.scope.decide", lambda *a: True), \
                mock.patch.object(skillguard, "on_stop", lambda *a: None), \
                mock.patch.object(hooks, "_spawn_sync", lambda *a, **k: None), \
                mock.patch.object(canary, "save_status", lambda *a, **k: None), \
                mock.patch.object(canary, "check", lambda agent, data: (carried, "acme/api", message if carried else "")):
            hooks.run("stop", "claude")
        return json.loads(out.getvalue()) if out.getvalue().strip() else {}


class ChecklistTests(TurnCase):
    def test_r1_the_checklist_names_the_commits_newer_than_the_note_and_nothing_once_it_is_updated(self):
        self.commits(2)
        items = dict(hooks.turn_checklist("claude", self.data, self.sess))
        self.assertIn("2 commits since the handoff note of acme/api", items["handoff"])
        self.note.write_text(NOTE.replace("Export works for CSV.", "Export works for CSV and PDF."))
        self.assertNotIn("handoff", dict(hooks.turn_checklist("claude", self.data, self.sess)))

    def test_r2_a_commit_gets_the_open_items_once_per_turn(self):
        self.commits(1)
        first = hooks.closing_reminder(self.shell("git commit -m 'feat: part'"), self.sess, self.now, "claude")
        self.assertIn("1 commit since the handoff note of acme/api", first)
        self.assertEqual(hooks.closing_reminder(self.shell("dev commit -m x"), self.sess, self.now + 5, "claude"), "")
        self.sess["turn_started"] = self.now + 60  # the next turn
        self.assertIn("since the handoff note", hooks.closing_reminder(self.shell("dev commit -m y"), self.sess,
                                                                         self.now + 90, "claude"))

    def test_r2_r5_a_closing_call_gets_the_status_block_and_the_open_items(self):
        self.commits(1)
        text = hooks.closing_reminder(self.shell("dev push"), self.sess, self.now, "claude")
        self.assertIn("**[Firefly> DevKit]** api · `main` · <task>", text)
        self.assertIn("1 commit since the handoff note", text)
        self.assertEqual(hooks.closing_reminder(self.shell("ls"), self.sess, self.now, "claude"), "")


class HandoffGateTests(TurnCase):
    def nudge(self, **data):
        return hooks._handoff_nudge("claude", dict(self.data, **data), self.sess, now=self.now)

    def test_r3_one_fresh_commit_is_reminded_not_blocked(self):
        self.commits(1)
        self.assertIsNone(self.nudge())

    def test_r3_five_commits_block(self):
        self.commits(5)
        self.assertIn("5 commit(s) in acme/api are newer than its handoff note", self.nudge())

    def test_r3_a_commit_older_than_an_hour_blocks(self):
        self.commits(1, minutes_ago=61)
        self.assertIn("61 minutes", self.nudge())

    def test_r3_a_milestone_since_the_note_blocks(self):
        self.commits(1)
        self.sess["milestones"] = [[self.t0 - 60, "dev push"]]  # before the note changed: not a milestone of it
        self.assertIsNone(self.nudge())
        self.sess["milestones"].append([int(self.now) - 30, "dev progress step"])
        self.assertIn("after `dev progress step`", self.nudge())

    def test_r3_a_member_never_gets_the_handoff_gate(self):
        self.commits(5)
        self.assertIsNone(self.nudge(agent_id="a-tank"))

    def test_r3_the_post_tool_hook_records_milestones_of_the_main_session_only(self):
        sf = hooks._session_file("claude", "s1")
        sf.parent.mkdir(parents=True, exist_ok=True)
        state.replace(sf, dict(self.sess, agent="claude"))
        for payload in (self.shell("dev progress step 'parser' --check '✓ 4 tests'"),
                        dict(self.shell("git push origin feat/x"), agent_id="a-tank"), self.shell("ls -la")):
            with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(io.StringIO()):
                hooks.run("post-tool", "claude")
        self.assertEqual([w for _, w in state.read(sf)["milestones"]], ["dev progress step"])


class MeasuredTests(TurnCase):
    def test_r4_r5_a_missing_status_block_still_blocks_and_the_block_is_audited_with_its_rule(self):
        out = self.stop("Done: the export works.", carried=False)
        self.assertEqual(out.get("decision"), "block")
        (rec,) = audit.records(1, "stop.block")
        self.assertEqual((rec["evidence"]["rule"], rec["project"], rec["outcome"]), ("status", "acme/api", "blocked"))

    def test_r4_a_reminder_the_turn_then_satisfied_is_audited_as_prevented(self):
        self.commits(1)
        self.note.write_text(NOTE.replace("Export works for CSV.", "Export works for CSV and PDF."))
        self.assertEqual(self.stop(DONE, told=["handoff"]), {})
        self.assertEqual([r["evidence"]["rule"] for r in audit.records(1, "stop.prevented")], ["handoff"])
        self.assertEqual(audit.records(1, "stop.block"), [])

    def test_r4_dev_canary_shows_the_blocks_per_rule_per_day(self):
        for rule in ("handoff", "handoff", "status"):
            audit.record("stop.block", "acme/api", rule, "blocked", rule=rule, project="acme/api")
        audit.record("stop.prevented", "acme/api", "skills", "ok", rule="skills", project="acme/api")
        day = time.strftime("%Y-%m-%d")
        self.assertEqual(canary.stop_blocks(7), {"blocked": {(day, "handoff"): 2, (day, "status"): 1},
                                                 "prevented": {(day, "skills"): 1}})
        lines = canary.stop_block_lines(7)  # what `dev canary` prints under "Stop hook"
        self.assertIn(f"  sent back                {day}  handoff 2 · status 1", lines)
        self.assertIn(f"  prevented by a reminder  {day}  skills 1", lines)


class RoleNameTests(TurnCase):
    def subagent_stop(self, data):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(data))), contextlib.redirect_stdout(out), \
                mock.patch.dict(core.CFG, {"crew": {"gates": {"enforce": False}}}):  # the crew gates have own tests
            hooks.run("subagent-stop", "claude")
        return json.loads(out.getvalue()) if out.getvalue().strip() else {}

    def member(self, aid, report):
        # a crew member the Agent tool launched (its launch record next to its transcript), ending with `report`
        tr = Path(self.tmp.name) / f"agent-{aid}.jsonl"
        tr.write_text("")
        (Path(self.tmp.name) / f"agent-{aid}.meta.json").write_text(json.dumps({"toolUseId": f"toolu_{aid}",
                                                                              "description": "Tank: the API"}))
        return dict(self.data, agent_id=aid, agent_type="general-purpose", agent_transcript_path=str(tr),
                    last_assistant_message=report)

    def test_a_final_message_naming_a_member_alone_is_sent_back_once_per_turn(self):
        said = DONE + "\n\nTank finished the API; the Code reviewer (Seraph) reviews it."
        out = self.stop(said)
        self.assertEqual(out.get("decision"), "block")
        self.assertIn("without their role: Tank.", out["reason"])
        self.assertIn("Backend engineer (Tank)", out["reason"])
        self.assertEqual([r["evidence"]["rule"] for r in audit.records(1, "stop.block")], ["roles"])
        self.assertEqual(self.stop(said, again=True), {})  # the same turn: never twice, even without stop_hook_active
        self.sess["turn_started"] = self.now + 60  # the next turn: the role form and a command pass
        fixed = DONE + "\n\nThe Backend engineer (Tank) finished the API; `dev crew brief link \"…\"` is next."
        self.assertEqual(self.stop(fixed), {})
        with mock.patch.dict(core.CFG, {"canary": {"roles": False}}):  # the developer can turn the rule off
            self.assertEqual(self.stop(said), {})

    def test_a_member_report_naming_a_member_alone_is_sent_back_once(self):
        sf = hooks._session_file("claude", "s1")
        sf.parent.mkdir(parents=True, exist_ok=True)
        state.replace(sf, dict(self.sess, agent="claude"))
        report = ("**[Firefly> DevKit · Tank]** api · `main` · the API\n- **Done:** built it\n- **Check:** ✓ 12 tests"
                  "\n- **Next:** review\n\nNeeds from Morpheus: nothing")
        first = self.subagent_stop(self.member("a1", report))
        self.assertEqual(first.get("decision"), "block")
        self.assertIn("Backend engineer (Tank)", first["reason"])
        self.assertEqual(self.subagent_stop(self.member("a1", report)), {})  # once per member
        fixed = report.replace("DevKit · Tank]", "DevKit · Backend engineer (Tank)]")
        self.assertEqual(self.subagent_stop(self.member("a2", fixed)), {})


class AcceptanceTests(TurnCase):
    def test_a_turn_that_commits_once_and_ends_is_reminded_before_its_final_message_and_not_blocked(self):
        sf = hooks._session_file("claude", "s1")
        sf.parent.mkdir(parents=True, exist_ok=True)
        state.replace(sf, dict(self.sess, agent="claude"))
        self.commits(1)
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(self.shell("git commit -m 'feat: part'")))), \
                contextlib.redirect_stdout(out):
            hooks.run("post-tool", "claude")
        self.assertIn("1 commit since the handoff note", out.getvalue())
        told = state.read(sf)["checklist_told"]["rules"]
        self.assertEqual(self.stop(DONE, told=told), {})  # 1 commit, minutes old, no milestone: no block
        self.assertEqual(audit.records(1, "stop.block"), [])


if __name__ == "__main__":
    unittest.main()
