# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Write attribution for review-only and read-only lanes (progress.scope_written).

A review lane's shell call is a window: every file `git status` shows changed during it is judged. A file is
another writer's when its own edit tool touched it then (the edit record). Otherwise it is "possible" when another
writer, a builder lane or the tech lead, had a shell call that overlapped the reviewer's call, still open or already
closed, and "certain" when nobody else could have written it. Only "certain" makes the review unverified. A builder's
scope alone is no alibi (tests/test_progress.py ScopeWriteTests keeps that case).

Seen live in one run: about 15 notices held the Security engineer's `python3 - <<'EOF'` rewrites of guard.py, and
the tech lead's own heredoc and `sed -i` edits, against the Code reviewer, because those calls had closed before the
reviewer's call ended and the call ledger forgets a call when it closes; every review report was marked unverified.

Run: python3 -m unittest tests.test_readonly_attribution -v (and with /usr/bin/python3, for Python 3.9)
"""
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import audit, codexlaunch  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_codex_launch import REVIEWER, TANK, CodexCrew  # noqa: E402
from test_progress import TempState, launch  # noqa: E402

REVIEW_REPORT = ("**[Firefly> DevKit · Code reviewer (Seraph)]** api · `main` · review\n"
                 "- **Done:** read the diff of the work.\n- **Check:** ✓ read the diff, 12 tests pass\n"
                 "- **Next:** the tech lead merges it.")


class ShellOverlap:
    """Shell calls as the hooks hand them to progress, under a fixed clock: PreToolUse opens one in the call ledger,
    PostToolUse closes it, and a refusal at PreToolUse closes it without running."""

    agent, sid = "claude", "s1"

    def call(self, aid, tid, at, cmd="python3 - <<'EOF'\nprint('x')\nEOF", tool="Bash", key="command"):
        data = {"session_id": self.sid, "tool_use_id": tid, "tool_name": tool, "hook_event_name": "PreToolUse",
                "cwd": "/repo", "tool_input": {key: cmd}}
        if aid != "main":
            data["agent_id"] = aid
        pg.call_opened(self.agent, data, now=at)
        return data

    def closed(self, data, at, event="PostToolUse"):
        with mock.patch.object(pg.time, "time", lambda: at):
            pg.call_closed(dict(data, hook_event_name=event))

    def ran(self, aid, tid, opened, closed, **kw):
        self.closed(self.call(aid, tid, opened, **kw), closed)

    def written(self, paths, reader="a_seraph"):
        return pg.scope_written(self.agent, self.sid, reader, paths, now=self.t0 + 30)

    def told(self):
        return [m["text"] for m in pg.load_run(self.agent, self.sid).get("messages") or []
                if m.get("to") == "main" and m.get("kind") == "notice" and "shell call" in m["text"]]

    def scope_audit(self):
        return sorted((r["target"], r["detail"]) for r in audit.query(action="scope.write"))


class ClaudeShellOverlapTests(ShellOverlap, unittest.TestCase):
    # Claude Code: the Senior engineer builds docs/audit.md, the Backend engineer api/**, and the Code reviewer
    # (review-only) runs one shell call from t0 to t0 + 30, when its PostToolUse judges what changed
    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        pg.start("Work", plan="audit docs=10m; api=10m; review=10m")  # the review runs beside the work it reads
        pg.assign("audit docs", "Neo", scope="docs/audit.md", why="test")
        pg.assign("api", "Tank", scope="api/**", why="test")
        pg.assign("review", "Seraph")
        launch("a_neo", "devkit:neo", "Neo: audit docs")
        launch("a_tank", "devkit:tank", "Tank: api")
        launch("a_seraph", "devkit:seraph", "Seraph: review")
        self.t0 = time.time()
        self.call("a_seraph", "toolu_seraph", self.t0, cmd="python3 -m unittest tests.test_audit")

    def review_ends(self):
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a_seraph", "agent_type": "devkit:seraph",
                                  "last_assistant_message": REVIEW_REPORT}, False, now=self.t0 + 60)
        return next(st for st in pg.load_run("claude", "s1")["plan"] if st["name"] == "review")

    def test_a_builders_shell_call_that_closed_during_the_reviewers_call_makes_its_file_possible(self):
        # this run's case: the builder's heredoc rewrote its own file and closed before the reviewer's call ended
        self.ran("a_neo", "toolu_neo_heredoc", self.t0 + 2, self.t0 + 8)
        flagged = self.written(["docs/audit.md"])
        self.assertEqual((flagged["paths"], flagged["certainty"]), (["docs/audit.md"], "possible"))
        (text,) = self.told()
        self.assertIn("possibly the shell call of Senior engineer (Neo)", text)
        self.assertNotIn("unverified", text)

    def test_a_review_whose_files_are_only_possible_ends_done_on_its_check(self):
        self.ran("a_neo", "toolu_neo_heredoc", self.t0 + 2, self.t0 + 8)
        self.written(["docs/audit.md"])
        self.assertEqual(self.review_ends()["state"], "done")

    def test_the_tech_leads_shell_call_that_closed_during_the_reviewers_call_makes_a_file_possible(self):
        # the tech lead's `sed -i` lasts a second; the Backend engineer's call closed before the reviewer's opened
        self.ran("a_tank", "toolu_tank_early", self.t0 - 20, self.t0 - 10)
        self.ran("main", "toolu_main_sed", self.t0 + 3, self.t0 + 4, cmd="sed -i '' 's/a/b/' lib/x.py")
        flagged = self.written(["lib/x.py"])
        self.assertEqual((flagged["paths"], flagged["certainty"]), (["lib/x.py"], "possible"))
        (text,) = self.told()
        self.assertIn("possibly the shell call of Tech lead (Morpheus)", text)
        self.assertNotIn("Backend engineer (Tank)", text)

    def test_the_tech_leads_open_shell_call_makes_a_file_possible(self):
        self.call("main", "toolu_main_open", self.t0 + 25)
        self.assertEqual(self.written(["lib/x.py"])["certainty"], "possible")

    def test_only_a_shell_call_that_ran_counts_as_another_writer(self):
        # the guard refused the Backend engineer's call: it never ran, so api/x.py stays the reviewer's
        self.closed(self.call("a_tank", "toolu_tank_refused", self.t0 + 5), self.t0 + 5, event="PreToolUse")
        self.ran("a_neo", "toolu_neo_heredoc", self.t0 + 6, self.t0 + 9)
        self.written(["api/x.py", "docs/audit.md"])
        self.assertEqual(self.scope_audit(), [("Seraph", "certain: api/x.py"), ("Seraph", "possible: docs/audit.md")])


class CodexOverlap(ShellOverlap):
    # Codex: a member's tool calls come under the tech lead's thread with its agent_id; the shell call is
    # exec_command, whose command is `cmd`, and edits are apply_patch, whose files the patch names. The lanes come
    # from the launch records codexlaunch writes when the agent starts.
    agent, sid = "codex", "c1"
    plan = "api=10m [backend]; review=10m [review] after api"

    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        env = mock.patch.dict(os.environ, {"DEVKIT_SESSION": f"codex:{self.sid}"})
        env.start()
        self.addCleanup(env.stop)
        (pg.SESSIONS / f"codex-{self.sid}.json").write_text(json.dumps({
            "agent": "codex", "session": self.sid, "started": time.time() - 600}))
        pg.start("Work", plan=self.plan)
        pg.assign("api", "Tank", scope="api/**", why="test")
        pg.assign("review", "Seraph")
        self.started(TANK, "tank", "api")
        self.started(REVIEWER, "seraph", "review")
        self.t0 = time.time()
        self.review = self.exec(REVIEWER, "exec-review", self.t0, "python3 -m unittest tests.test_api")

    def exec(self, aid, tid, at, cmd):
        return self.call(aid, tid, at, cmd=cmd, tool="exec_command", key="cmd")

    def started(self, aid, member, task):
        # what codexlaunch.record writes at SubagentStart, then the start itself
        key, label = f"call_{aid[-6:]}", codexlaunch.label_of({"type": member, "task": task})
        pg.note_agent_launch("codex", {"session_id": self.sid, "tool_use_id": key,
                                       "tool_input": {"description": label, "agent_type": member}})
        pg.launch_result("codex", {"session_id": self.sid, "tool_use_id": key, "tool_response": {"agentId": aid}})
        pg.on_subagent("codex", {"session_id": self.sid, "agent_id": aid, "agent_type": member}, True)

    def written(self, paths, reader=REVIEWER):
        return super().written(paths, reader)


class CodexShellOverlapTests(CodexOverlap, unittest.TestCase):
    def test_a_codex_builders_exec_command_during_the_reviewers_call_makes_its_file_possible(self):
        self.ran(TANK, "exec-tank", self.t0 + 2, self.t0 + 6, tool="exec_command", key="cmd")
        flagged = self.written(["api/x.py"])
        self.assertEqual((flagged["paths"], flagged["certainty"]), (["api/x.py"], "possible"))
        (text,) = self.told()
        self.assertIn("possibly the shell call of Backend engineer (Tank)", text)

    def test_a_codex_builders_apply_patch_during_the_reviewers_call_is_credited_to_it(self):
        patch = "*** Begin Patch\n*** Update File: api/x.py\n@@\n-a\n+b\n*** End Patch\n"
        with mock.patch.object(pg.time, "time", lambda: self.t0 + 5):
            pg.on_tool_done("codex", {"session_id": self.sid, "tool_name": "apply_patch", "tool_use_id": "exec-p1",
                                      "hook_event_name": "PostToolUse", "agent_id": TANK, "cwd": "/repo",
                                      "tool_input": {"command": patch}})
        self.assertIsNone(self.written(["api/x.py"]))
        self.assertEqual(self.scope_audit(), [("Tank", "attributed: api/x.py")])


class ReaderCallTests(CodexOverlap, unittest.TestCase):
    """A shell call that cannot write files is nobody's alibi: a Codex tech lead waits for the crew in the foreground
    with `dev watch --wait --timeout 10`, again and again, so one of its calls is open across nearly every review.
    The call counts by what it runs, for the tech lead and the builders alike: devkit's own record commands and tools
    that only read are no writers; a test run, a script or any other program that runs code is one."""

    plan = "api=10m [backend]; review=10m [review]"  # the review runs beside the work it reads

    def review_ends(self):
        pg.on_subagent("codex", {"session_id": self.sid, "agent_id": REVIEWER, "agent_type": "seraph",
                                 "last_assistant_message": REVIEW_REPORT}, False, now=self.t0 + 60)
        return next(st for st in pg.load_run("codex", self.sid)["plan"] if st["name"] == "review")

    def test_a_codex_tech_leads_open_dev_watch_is_no_alibi_for_the_reviewers_write(self):
        self.exec("main", "exec-main-watch", self.t0 + 2, "dev watch --wait --timeout 10")
        flagged = self.written(["lib/x.py"])
        self.assertEqual((flagged["paths"], flagged["certainty"]), (["lib/x.py"], "certain"))
        self.assertEqual(self.review_ends()["state"], "unverified")

    def judged(self, caller, cmds, prefix, open_while_judged=False):
        """The certainty of the reviewer's write of one file per command, each in a reviewer call of its own that
        starts after every call of the commands before it; the caller's call opens 2 s into it and closes 2 s later,
        or stays open until the write is judged."""
        review, out = self.review, {}
        for k, cmd in enumerate(cmds):
            at, path = self.t0 + 100 * k, f"{prefix}{k}.py"
            if k:
                self.closed(review, at - 1)
                review = self.exec(REVIEWER, f"exec-review-{k}", at, "python3 -m unittest tests.test_api")
            call = self.exec(caller, f"exec-{k}", at + 2, cmd)
            if not open_while_judged:
                self.closed(call, at + 4)
            flagged = pg.scope_written(self.agent, self.sid, REVIEWER, [path], now=at + 30)
            self.closed(call, at + 40)
            out[cmd] = "certain" if path in flagged["certain"] else "possible"
        return out

    def test_the_tech_leads_call_is_an_alibi_only_when_it_can_write(self):
        rows = {"sed -i '' 's/a/b/' lib/x.py": "possible",
                "python3 - <<'EOF'\nopen('lib/x.py', 'w').write('x')\nEOF": "possible",
                "python3 -m unittest tests.test_api": "possible",  # a test run runs code that can write
                "dev crew sync": "possible",  # it writes the generated agent files
                "find . -name '*.pyc' -delete": "possible",
                "cd /repo && dev watch --wait --timeout 10 2>&1 | tail -5": "certain",
                "git diff --stat; dev session inbox": "certain",
                "grep -rn call_reads lib | wc -l": "certain"}
        self.assertEqual(self.judged("main", list(rows), "lib/x"), rows)

    def test_a_builders_call_is_an_alibi_only_when_it_can_write(self):
        # api/** is the Backend engineer's scope; its call is still open when the reviewer's write is judged
        rows = {"python3 -m unittest tests.test_api": "possible", "git status --short": "certain"}
        self.assertEqual(self.judged(TANK, list(rows), "api/x", open_while_judged=True), rows)


class CodexReviewerHookTests(CodexCrew):
    """A Codex reviewer's exec_command through the real hooks: the guard opens it, the shell runs it, PostToolUse
    judges what changed in the repository, and SubagentStop ends the review."""

    def exec_call(self, s, aid, member, cmd, tid):
        call = dict(tool_name="exec_command", tool_input={"cmd": cmd}, tool_use_id=tid, agent_id=aid,
                    agent_type=member)
        g = s.hook("guard", s.payload("PreToolUse", **call))
        spec = g.get("hookSpecificOutput") or {}
        self.assertNotEqual(spec.get("permissionDecision"), "deny", spec)
        return s.finish(s.execute({"call": call, "ran": (spec.get("updatedInput") or {}).get("cmd") or cmd}))

    def test_a_codex_reviewers_shell_write_that_no_one_else_could_have_made_ends_its_review_unverified(self):
        s = self.lead(plan="api=30m [backend]; review=20m [review]")
        s.ok("progress", "assign", "review", "--to", "Seraph")
        self.spawn(s, REVIEWER, "seraph", "review")
        # a check the review lane may run, whose test writes into the repository (no other writer is at work)
        (self.api / "tests").mkdir(exist_ok=True)
        (self.api / "tests" / "test_notes.py").write_text(
            "import unittest\n\n\nclass Notes(unittest.TestCase):\n    def test_notes(self):\n"
            "        open('notes.md', 'w').write('x')\n")
        self.exec_call(s, REVIEWER, "seraph", "python3 -m unittest tests.test_notes", "exec-review-1")
        self.assertEqual((s.run_doc()["agents"][REVIEWER].get("scope_writes") or {}).get("certainty"), "certain")
        for again in (False, True):  # the crew gates send the first stop back once (no `dev progress step`)
            s.hook("subagent-stop", s.payload(
                "SubagentStop", agent_id=REVIEWER, agent_type="seraph", model="gpt-6-luna", stop_hook_active=again,
                agent_transcript_path=str(self.rollout(REVIEWER)),
                last_assistant_message=REVIEW_REPORT.replace("`main`", "`feat/invoice-export`")))
        self.assertEqual(s.step_state("review"), "unverified")


if __name__ == "__main__":
    unittest.main()
