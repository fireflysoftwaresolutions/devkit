# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The PreToolUse hook (`dev hook pre-tool`, and `dev hook guard` for settings that predate the name) decides nothing:
whatever a call does, its output carries no permission decision. What devkit used to refuse, it now says as one advice
line in the call's context: a crew member's edit outside its scope or by a read-only lane, a launch past the crew's
slots, the main session editing a file a working member holds, a lane call the router cannot place, and a commit that
would include unfinished work.

Every hook runs as the agent runs it: `dev hook <event> --agent claude|codex` with the payload on stdin, in a sandbox
Mac (tests/test_acceptance_crew_sessions.py, tests/_sandbox.py).

Run: python3 -m unittest tests.test_pre_tool_hook -v (and with /usr/bin/python3, for Python 3.9)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "lib"))
from test_acceptance_crew_sessions import GIT_ID  # noqa: E402  (activates the hermetic net and the sandbox)
from test_codex_launch import LEAD, TANK, TRINITY, CodexCrew  # noqa: E402

CLAUDE_LEAD = "c1a0de00-0000-4000-8000-00000000aa01"
ADVICE = "devkit · advice: "
UNFINISHED = "These lines leave unfinished work"


class PreToolCase(CodexCrew):
    def lead_on(self, agent: str, **progress):
        # a tech lead whose run gives Tank the api step (scope api/**) and Trinity the ui step, Tank launched
        if agent == "codex":
            s = self.lead(**progress)
            self.spawn(s, TANK, "tank", "api")
            return s
        self.config(**{"max_parallel": 2, "reserve": 0, **progress})
        s = self.claude(CLAUDE_LEAD, self.api)
        s.ok("progress", "start", "Invoice export", "--plan", "api=30m [backend]; ui=30m [frontend]")
        for step, name in (("api", "Tank"), ("ui", "Trinity")):
            s.ok("progress", "assign", step, "--to", name, "--scope", f"{step}/**")
        self.assertEqual(s.launch(TANK, "tank", "Tank: api"), "")
        return s

    @staticmethod
    def member(agent: str, aid: str, name: str) -> dict:
        return {"agent_id": aid, "agent_type": name if agent == "codex" else f"devkit:{name}"}

    def edit(self, s, path: str, n: int, **who) -> dict:
        # a file edit as each agent makes it: Claude's Write tool, Codex's apply_patch
        if s.agent == "codex":
            call = dict(tool_name="apply_patch", tool_use_id=f"exec-patch-{n}",
                        tool_input={"command": f"*** Begin Patch\n*** Add File: {path}\n+export {{}};\n*** End Patch\n"})
        else:
            call = dict(tool_name="Write", tool_use_id=f"toolu_write_{n}",
                        tool_input={"file_path": str(s.repo / path), "content": "export {};\n"})
        return s.hook("pre-tool", s.payload("PreToolUse", **call, **who))

    def shell(self, s, cmd: str, n: int, event="pre-tool", **who) -> dict:
        prefix = "exec" if s.agent == "codex" else "toolu"
        return s.hook(event, s.payload("PreToolUse", tool_name="Bash", tool_use_id=f"{prefix}-sh-{n}",
                                       tool_input={"command": cmd}, **who))

    def assertDecidesNothing(self, out: dict):
        spec = out.get("hookSpecificOutput") or {}
        self.assertNotIn("permissionDecision", spec, out)
        self.assertNotIn("decision", out, out)

    def advice(self, out: dict) -> list:
        text = ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext") or ""
        return [p[len(ADVICE):] for p in text.split("\n\n") if p.startswith(ADVICE)]


class NoDecisionTests(PreToolCase):
    def test_a_force_push_to_main_runs_with_no_decision(self):
        # under the handler's name and under the name older settings still call it by
        for agent in ("claude", "codex"):
            s = self.claude(CLAUDE_LEAD, self.api) if agent == "claude" else self.codex(LEAD, self.api)
            for n, event in enumerate(("pre-tool", "guard")):
                with self.subTest(agent=agent, event=event):
                    out = self.shell(s, "git push --force origin main", n, event=event)
                    self.assertDecidesNothing(out)
                    self.assertEqual(self.advice(out), [])

    def test_a_write_to_devkits_own_state_runs_with_no_decision(self):
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                s = self.claude(CLAUDE_LEAD, self.api) if agent == "claude" else self.codex(LEAD, self.api)
                target = self.state / "runs" / f"{agent}-forged.json"
                if agent == "codex":
                    call = dict(tool_name="apply_patch", tool_use_id="exec-state-1", tool_input={
                        "command": f"*** Begin Patch\n*** Add File: {target}\n+{{}}\n*** End Patch\n"})
                else:
                    call = dict(tool_name="Write", tool_use_id="toolu_state_1",
                                tool_input={"file_path": str(target), "content": "{}"})
                for event in ("pre-tool", "guard"):
                    self.assertDecidesNothing(s.hook(event, s.payload("PreToolUse", **call)))

    def test_a_members_lane_call_is_routed_with_no_decision(self):
        # Codex's rewrite used to carry an explicit allow; now it carries only the new command
        s = self.lead_on("codex")
        out = self.shell(s, 'dev progress note "half done"', 1, **self.member("codex", TANK, "tank"))
        self.assertDecidesNothing(out)
        self.assertIn(f"DEVKIT_FROM_AGENT={TANK}", out["hookSpecificOutput"]["updatedInput"]["command"])


class AdviceTests(PreToolCase):
    def test_a_members_edit_outside_its_scope_runs_with_the_scope_advice(self):
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                s = self.lead_on(agent)
                who = self.member(agent, TANK, "tank")
                inside = self.edit(s, "api/view.ts", 1, **who)
                self.assertDecidesNothing(inside)
                self.assertEqual(self.advice(inside), [])
                outside = self.edit(s, "ui/view.ts", 2, **who)
                self.assertDecidesNothing(outside)
                self.assertEqual(self.advice(outside), [
                    "ui/view.ts is outside Backend engineer (Tank)'s scope (api/**): another crew member may own it. "
                    "Ask the tech lead to widen it: dev crew ask \"may I change ui/view.ts? why: …\""])

    def test_a_read_only_members_edit_runs_with_the_read_only_advice(self):
        s = self.lead_on("claude")
        s.ok("progress", "add", "review=20m")
        s.ok("progress", "assign", "review", "--to", "Seraph", "--scope", "docs/**")
        self.assertEqual(s.launch("claude-seraph-1", "seraph", "Seraph: review"), "")
        out = self.edit(s, "api/view.ts", 1, **self.member("claude", "claude-seraph-1", "seraph"))
        self.assertDecidesNothing(out)
        self.assertEqual(len(self.advice(out)), 1, out)
        self.assertIn("Code reviewer (Seraph) is review-only: report the change with file:line", self.advice(out)[0])

    def test_a_launch_past_the_crews_slots_runs_with_the_slot_advice(self):
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                s = self.lead_on(agent, max_parallel=1)
                tool = "spawn_agent" if agent == "codex" else "Agent"
                call = dict(tool_name=tool, tool_use_id=f"launch-{agent}-2", tool_input={
                    "description": "Trinity: ui", "subagent_type": "devkit:trinity", "agent_type": "trinity",
                    "task_name": "ui", "prompt": "Build the screen.", "message": "Build the screen."})
                out = s.hook("pre-tool", s.payload("PreToolUse", **call))
                self.assertDecidesNothing(out)
                self.assertEqual(len(self.advice(out)), 1, out)
                self.assertIn("1 of 1 crew slots are in use", self.advice(out)[0])

    def test_the_main_session_editing_a_working_members_file_gets_the_holder_advice(self):
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                s = self.lead_on(agent)
                self.shell(s, "git status --short", 1, **self.member(agent, TANK, "tank"))  # Tank is working
                out = self.edit(s, "api/view.ts", 2)
                self.assertDecidesNothing(out)
                self.assertEqual(len(self.advice(out)), 1, out)
                self.assertIn("api/view.ts belongs to Backend engineer (Tank) while they work (scope api/**)",
                              self.advice(out)[0])

    def test_a_lane_call_the_router_cannot_place_gets_the_lane_advice_on_codex(self):
        # two lane calls in one command: the router places one per command, and only Claude can export the lane's
        # variable for a whole command
        cmd = 'dev progress note "one" && dev progress note "two"'
        codex = self.lead_on("codex")
        out = self.shell(codex, cmd, 1, **self.member("codex", TANK, "tank"))
        self.assertDecidesNothing(out)
        self.assertEqual(len(self.advice(out)), 1, out)
        self.assertIn("devkit could not route the dev progress or dev crew call in this command to your lane, so it "
                      "will run as the main session", self.advice(out)[0])
        claude = self.lead_on("claude")
        out = self.shell(claude, cmd, 1, **self.member("claude", TANK, "tank"))
        self.assertDecidesNothing(out)
        self.assertEqual(self.advice(out), [])
        self.assertTrue(out["hookSpecificOutput"]["updatedInput"]["command"].startswith("export "), out)

    def test_a_commit_with_unfinished_work_gets_the_stub_advice_unless_it_is_a_wip_commit(self):
        (self.api / "parser.py").write_text("def parse(text):\n    # " + "TO" + "DO: handle quotes\n    return text\n")
        subprocess.run(["git", "add", "parser.py"], cwd=str(self.api), check=True, capture_output=True,
                       env=dict(self.env, **GIT_ID))
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                s = self.claude(CLAUDE_LEAD, self.api) if agent == "claude" else self.codex(LEAD, self.api)
                out = self.shell(s, 'git commit -m "feat: parse quoted fields"', 1)
                self.assertDecidesNothing(out)
                self.assertEqual(len(self.advice(out)), 1, out)
                self.assertTrue(self.advice(out)[0].startswith(UNFINISHED), out)
                self.assertIn("parser.py:2 todo", self.advice(out)[0])
                wip = self.shell(s, "dev commit --wip", 2)
                self.assertDecidesNothing(wip)
                self.assertFalse(any(a.startswith(UNFINISHED) for a in self.advice(wip)), wip)


class HandlerNameTests(PreToolCase):
    def test_the_guard_name_runs_the_same_handler_as_pre_tool(self):
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                s = self.lead_on(agent)
                who = self.member(agent, TANK, "tank")
                by_name = [self.edit(s, "ui/view.ts", n, **who) for n in (1, 2)]
                old = s.hook("guard", s.payload("PreToolUse", tool_name="Write", tool_use_id="toolu_write_3",
                                                tool_input={"file_path": str(s.repo / "ui" / "view.ts"),
                                                            "content": "export {};\n"}, **who)) \
                    if agent == "claude" else s.hook("guard", s.payload(
                        "PreToolUse", tool_name="apply_patch", tool_use_id="exec-patch-3", tool_input={
                            "command": "*** Begin Patch\n*** Add File: ui/view.ts\n+export {};\n*** End Patch\n"},
                        **who))
                self.assertEqual(old, by_name[1])
                self.assertEqual(len(self.advice(old)), 1, old)

    def test_the_main_sessions_first_call_records_that_the_hook_ran_for_the_agents_self_test(self):
        from devkit import providers
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                s = self.claude(CLAUDE_LEAD, self.api) if agent == "claude" else self.codex(LEAD, self.api)
                record = self.state / "sessions" / f"{agent}-{s.sid}.json"
                self.assertFalse(providers.pre_tool_seen(record))
                self.shell(s, "git status --short", 1)
                self.assertTrue(providers.pre_tool_seen(record))

    def test_both_settings_templates_call_pre_tool(self):
        claude = json.loads((ROOT / "config" / "claude" / "settings.json").read_text())
        codex = json.loads((ROOT / "config" / "codex" / "hooks.json").read_text())
        for name, conf in (("claude", claude), ("codex", codex)):
            commands = [h["command"] for e in conf["hooks"]["PreToolUse"] for h in e["hooks"]]
            self.assertEqual(commands, [f"'{{{{DEVKIT}}}}/bin/dev' hook pre-tool --agent {name}"], name)


class AdviceFunctionTests(unittest.TestCase):
    # the advice functions in-process, as the hook calls them, on a run in a temporary state
    def setUp(self):
        from test_progress import TempState
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def git_repo(self, name: str) -> Path:
        r = self.tmp / name
        for args in (["init", "-q", str(r)], ["-C", str(r), "-c", "user.email=a@example.com", "-c", "user.name=a",
                                             "commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(["git", *args], check=True, capture_output=True)
        return r

    @staticmethod
    def edit(cwd, path, aid="a1") -> dict:
        d = {"session_id": "s1", "tool_name": "Edit", "cwd": str(cwd), "tool_input": {"file_path": str(path)}}
        return dict(d, agent_id=aid) if aid else d

    def test_scope_advice_keeps_parallel_members_apart_and_leaves_scratch_files_alone(self):
        from devkit import hooks, progress as pg
        from test_progress import launch
        repo = self.git_repo("api")
        pg.start("Feature", plan=["parser", "ui"])
        pg.assign("parser", "Neo", scope="lib/parser/**,tests/test_parser.py")
        launch("a1", "devkit:neo", "Neo: parser")
        self.assertEqual(hooks._scope_advice("claude", self.edit(repo, repo / "lib/parser/core.py")), [])
        self.assertEqual(hooks._scope_advice("claude", self.edit(repo, repo / "tests/test_parser.py")), [])
        told = hooks._scope_advice("claude", self.edit(repo, repo / "lib/ui/app.ts"))
        self.assertEqual(len(told), 1, told)
        self.assertIn("lib/ui/app.ts is outside Senior engineer (Neo)'s scope", told[0])
        self.assertIn("dev crew ask", told[0])
        self.assertEqual(hooks._scope_advice("claude", self.edit(repo, repo / "lib/ui/app.ts", aid=None)), [])
        scratch = Path(tempfile.mkdtemp())  # a scratch file outside any checkout is nobody's
        self.addCleanup(shutil.rmtree, scratch, True)
        self.assertEqual(hooks._scope_advice("claude", self.edit(repo, scratch / "notes.md")), [])
        other = self.git_repo("billing")  # another repository is not scratch
        self.assertIn("outside Senior engineer (Neo)'s scope",
                      " ".join(hooks._scope_advice("claude", self.edit(repo, other / "src/app.py"))))

    def test_scope_advice_follows_the_member_into_a_worktree_of_the_same_repository(self):
        from devkit import hooks, progress as pg
        from test_progress import launch
        main, wt = self.git_repo("api"), self.tmp / "wt"
        subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", "-b", "feat/x", str(wt)], check=True,
                       capture_output=True)
        pg.start("Feature", plan=["installer"])
        pg.assign("installer", "Kid", scope="install.sh,tests/test_install*.py")
        launch("a1", "devkit:kid", "Kid: installer")
        self.assertEqual(hooks._scope_advice("claude", self.edit(main, wt / "install.sh")), [])
        self.assertEqual(hooks._scope_advice("claude", self.edit(main, wt / "tests" / "test_install_curl.py")), [])
        self.assertIn("lib/cli.py is outside DevEx engineer (Kid)'s scope",
                      " ".join(hooks._scope_advice("claude", self.edit(main, wt / "lib" / "cli.py"))))

    def test_crew_limits_advice_names_the_working_member_that_holds_the_main_sessions_file(self):
        from devkit import hooks, progress as pg
        from test_progress import launch
        repo = self.git_repo("api")
        pg.start("Feature", plan=["parser"])
        pg.assign("parser", "Neo", scope="lib/parser/**")
        launch("a1", "devkit:neo", "Neo: parser")
        told = hooks._crew_limits_advice("claude", self.edit(repo, repo / "lib/parser/core.py", aid=None))
        self.assertEqual(len(told), 1, told)
        self.assertIn("belongs to Senior engineer (Neo) while they work (scope lib/parser/**)", told[0])
        self.assertEqual(hooks._crew_limits_advice("claude", self.edit(repo, repo / "README.md", aid=None)), [])

    def test_only_an_agent_launch_gets_the_slot_advice_never_a_monitor(self):
        from devkit import core, hooks, progress as pg
        from test_progress import launch
        pg.start("Feature", plan=["parser", "ui"])
        launch("a1", "devkit:neo", "Neo: parser")
        with mock.patch.dict(core.CFG, {"progress": {"max_parallel": 1, "reserve": 0}}):
            agent = {"session_id": "s1", "tool_name": "Agent", "tool_use_id": "toolu_l2",
                     "tool_input": {"description": "Trinity: ui", "subagent_type": "devkit:trinity", "prompt": "x"}}
            monitor = {"session_id": "s1", "tool_name": "Monitor", "tool_use_id": "toolu_m1",
                       "tool_input": {"command": "tail -f build.log"}}
            self.assertIn("crew slots are in use", " ".join(hooks._crew_limits_advice("claude", agent)))
            self.assertEqual(hooks._crew_limits_advice("claude", monitor), [])

    def test_the_pre_tool_handler_keeps_a_read_only_lanes_worktree_state_before_its_call(self):
        # hooks._watch_writes, run by the pre-tool handler: the write detection of review and read-only lanes
        import contextlib
        import io
        from devkit import hooks, progress as pg
        from test_progress import launch
        repo = self.git_repo("api")
        (repo / "notes.md").write_text("draft\n")
        pg.start("Feature", plan=["review"])
        pg.assign("review", "Seraph", scope="lib/**")
        launch("a1", "devkit:seraph", "Seraph: review")
        call = {"session_id": "s1", "agent_id": "a1", "tool_name": "Bash", "tool_use_id": "toolu_w1",
                "hook_event_name": "PreToolUse", "cwd": str(repo), "tool_input": {"command": "git status --short"}}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(call))), contextlib.redirect_stdout(io.StringIO()):
            hooks.run("pre-tool", "claude")
        entry = pg.call_entry(call)
        self.assertEqual(entry.get("wt_base"), os.path.realpath(repo), entry)
        self.assertIn("notes.md", entry.get("wt_before") or {}, entry)


if __name__ == "__main__":
    unittest.main()
