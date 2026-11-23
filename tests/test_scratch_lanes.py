"""Each crew lane keeps its scratch files in a folder of its own in the session scratchpad.

Two lanes of one crew member shared scratchpad/<member id>/, so one lane's scripts replaced and deleted the other's
files (two Security engineer lanes on different branches did). A lane that runs a plan step writes in
scratchpad/<member id>-<step>/ instead: the hook makes it once the lane binds to its launch and so knows its step
(SubagentStart comes before that), and the brief names it. The member's plain scratchpad/<member id>/ stays for a
lane that runs no step. The fixtures are a temporary state folder and a temporary scratchpad; no live hook runs."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))
from devkit import codexlaunch, core, crew, dispatch, hooks, progress as pg, ui  # noqa: E402
from test_progress import TempState, launch  # noqa: E402

T0 = 1_800_000_000.0
KEY = "-Users-dev-Development-acme-api"  # Claude Code's folder for the project's sessions


class ClaudeSessionCase(unittest.TestCase):
    """A Claude Code session s1 with a scratchpad and a plan of two steps for the Senior engineer (Neo)."""

    def setUp(self):
        state = TempState().__enter__()
        self.addCleanup(state.__exit__, None, None, None)
        root = Path(state.tmp.name)
        home, self.tmp = root / "claude-home", root / "claude-tmp"
        self.transcript = home / "projects" / KEY / "s1.jsonl"
        self.transcript.parent.mkdir(parents=True)
        self.transcript.write_text("")
        self.pad = self.tmp / KEY / "s1" / "scratchpad"
        self.pad.mkdir(parents=True)
        for p in (mock.patch.object(core, "CLAUDE_HOME", home), mock.patch.object(pg, "claude_tmp", lambda: self.tmp)):
            p.start()
            self.addCleanup(p.stop)
        pg.start("Feature", plan=["api=30m", "ui=30m"], now=T0)
        for step in ("api", "ui"):
            pg.assign(step, "Neo", now=T0 + 1, why="two lanes of one member")

    def start_lane(self, aid, step):
        """The Agent call (PreToolUse) and the agent's SubagentStart, as the hooks run them: the lane waits for its
        launch and knows no step yet."""
        pg.note_agent_launch("claude", {"session_id": "s1", "tool_use_id": f"toolu_{aid}", "tool_input": {
            "description": f"Neo: {step}", "subagent_type": "devkit:neo"}}, now=T0 + 2)
        start = {"session_id": "s1", "agent_id": aid, "agent_type": "devkit:neo",
                 "transcript_path": str(self.transcript)}
        pg.on_subagent("claude", start, True, now=T0 + 2)
        hooks._make_scratch_folder("claude", start)


class LaneFolderTests(ClaudeSessionCase):

    def test_two_lanes_of_one_member_get_different_folders_when_they_bind(self):
        for aid, step in (("a1", "api"), ("a2", "ui")):
            self.start_lane(aid, step)
        self.assertFalse((self.pad / "neo-api").exists())  # SubagentStart: no step is known yet
        # lane a1 binds by its launch's result in the tech lead's PostToolUse; lane a2 at its own first tool call,
        # by the meta file Claude Code keeps of the Agent call that started it
        hooks._bind_launch("claude", {"session_id": "s1", "tool_name": "Agent", "tool_use_id": "toolu_a1",
                                      "tool_response": {"agentId": "a1"}, "transcript_path": str(self.transcript)})
        meta = self.transcript.with_suffix("") / "subagents" / "agent-a2.meta.json"
        meta.parent.mkdir(parents=True)
        meta.write_text(json.dumps({"toolUseId": "toolu_a2", "description": "Neo: ui"}))
        hooks._bind_member("claude", {"session_id": "s1", "agent_id": "a2", "agent_type": "devkit:neo",
                                      "tool_name": "Read", "transcript_path": str(self.transcript)})
        plan = {s["name"]: s.get("owner") for s in pg.load_run("claude", "s1")["plan"]}
        self.assertEqual(plan, {"api": "a1", "ui": "a2"})
        self.assertTrue((self.pad / "neo-api").is_dir())
        self.assertTrue((self.pad / "neo-ui").is_dir())

    def test_each_brief_names_its_own_lanes_folder(self):
        neo = crew.member("neo")
        with tempfile.TemporaryDirectory() as d:
            def scope_of(step):
                text = ui.ANSI_RE.sub("", crew.brief_text(neo, "Build it.", "", step=step, cwd=d))
                return text.split("## Scope", 1)[1].split("## Skills", 1)[0]
            api, ui_, by_number = scope_of("api"), scope_of("ui"), scope_of("2")
        self.assertIn("`scratchpad/neo-api/`", api)
        self.assertNotIn("neo-ui", api)
        self.assertIn("`scratchpad/neo-ui/`", ui_)
        self.assertNotIn("neo-api", ui_)
        self.assertIn("`scratchpad/neo-ui/`", by_number)  # a step given by its number is named by its name


class ScratchNameTests(unittest.TestCase):

    def test_a_lanes_folder_is_its_member_and_its_steps_name_in_plain_letters(self):
        for step, name in (("api", "neo-api"), ("Session graph (v2)", "neo-session-graph-v2"),
                           ("Diseño", "neo-diseno"), ("review_api", "neo-review-api")):
            with self.subTest(step=step):
                self.assertEqual(pg.scratch_names("neo", step), [name, "neo"])

    def test_a_lane_without_a_step_keeps_the_members_plain_folder(self):
        self.assertEqual(pg.scratch_names("neo", ""), ["neo"])

    def test_a_name_that_is_no_member_id_gets_no_folder(self):
        for member in ("", "..", "Neo", "neo/x", "-neo"):
            with self.subTest(member=member):
                self.assertEqual(pg.scratch_names(member, "api"), [])


class OneRule:
    """The hook that makes a lane's folder, Codex's registration that lets the lane write there, and the scope rule
    that lets the lane's step name it find the folder by one rule (progress.session_scratchpad and scratch_names)."""

    def agree(self, agent, sid, event, member, step):
        made = hooks._make_scratch_folder(agent, event)  # the hook, from its event
        self.assertTrue(made and Path(made).is_dir(), made)
        self.assertEqual(Path(made).name, f"{member}-{step}")
        self.assertEqual(Path(made).parent, Path(pg.session_scratchpad(agent, sid)))  # devkit's records find it too
        allowed = dispatch._outside_folders("acme/api", member, (agent, sid), step)  # the scope rule
        self.assertIn(os.path.realpath(made), allowed)
        if agent == "codex":  # the folders a Codex lane may write in, its own first
            with codexlaunch.codex_scratch_context(agent, event):
                self.assertEqual(codexlaunch.APP_SCRATCH.get()[1][0], made)
                self.assertTrue(codexlaunch.app_scratch_allows(os.path.join(made, "probe.log"), member))


class ClaudeOneRuleTests(OneRule, ClaudeSessionCase):

    def test_a_claude_code_lanes_folder_is_found_by_one_rule(self):
        launch("a1", "devkit:neo", "Neo: api", at=T0 + 2)
        self.agree("claude", "s1", {"session_id": "s1", "agent_id": "a1", "transcript_path": str(self.transcript)},
                   "neo", "api")


class CodexOneRuleTests(OneRule, unittest.TestCase):
    """A Codex session "chat" whose Code reviewer (Seraph) lane runs the step review: in the Codex CLI, from a managed
    checkout, and in the ChatGPT app, from a chat's folder."""

    def setUp(self):
        state = TempState().__enter__()
        self.addCleanup(state.__exit__, None, None, None)
        self.home = Path(state.tmp.name).resolve()
        self.dev = self.home / "Development"
        for name, value in (("HOME", self.home), ("DEV", self.dev), ("TMP_ROOT", self.dev / ".tmp"),
                            ("WT_ROOT", self.dev / ".worktrees")):
            patch = mock.patch.object(core, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        pg.RUNS.mkdir(exist_ok=True)
        pg.run_path("codex", "chat").write_text(json.dumps({
            "agent": "codex", "session": "chat", "state": "running", "project": "acme/api",
            "agents": {"child": {"persona": "seraph", "state": "running", "binding": "bound",
                                 "assigned_steps": ["review"]}},
            "plan": [{"name": "review", "label": "Seraph", "owner": "child", "state": "running"}], "messages": []}))

    def session_in(self, cwd, temp_roots):
        pg.session_path("codex", "chat").write_text(json.dumps({"agent": "codex", "session": "chat", "cwd": str(cwd)}))
        patch = mock.patch.object(core, "temp_roots", return_value=temp_roots)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_codex_cli_lanes_folder_is_found_by_one_rule(self):
        host = self.dev / "acme" / "api"
        subprocess.run(["git", "init", "-q", str(host)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(host), "remote", "add", "origin", "https://github.com/acme/api.git"],
                       check=True, capture_output=True)
        self.session_in(host, [str(self.dev / ".tmp")])
        self.agree("codex", "chat", {"session_id": "chat", "agent_id": "child", "cwd": str(host)}, "seraph", "review")

    def test_a_codex_app_lanes_folder_is_found_by_one_rule(self):
        host = self.home / "Documents" / "Codex" / "2026-11-10" / "chat"
        host.mkdir(parents=True)
        self.session_in(host, [])
        self.agree("codex", "chat", {"session_id": "chat", "agent_id": "child", "cwd": str(host)}, "seraph", "review")


class JoinedStepFolderTests(ClaudeSessionCase):
    """The Senior engineer's lane, launched for api, the step its brief named, takes ui when it closes api. Its folders
    followed only the step it runs now, so scratchpad/neo-api/ was no longer its own, and scratchpad/neo-ui/ was
    never made: only the lane's launch made a folder."""

    def setUp(self):
        super().setUp()
        launch("a1", "devkit:neo", "Neo: api", at=T0 + 2)
        self.call = {"session_id": "s1", "agent_id": "a1", "agent_type": "devkit:neo", "tool_name": "Bash",
                     "transcript_path": str(self.transcript)}
        hooks._make_scratch_folder("claude", self.call)  # the launch's folder, as the hook makes it at binding
        pg.step("api built", which="api", check="✓ 2 of 2 tests", from_agent="a1", now=T0 + 60)
        self.run_ = pg.load_run("claude", "s1")
        self.assertEqual({s["name"]: s.get("owner") for s in self.run_["plan"]}, {"api": "a1", "ui": "a1"})

    def test_the_lane_keeps_the_folder_its_brief_named_after_another_step_joins_it(self):
        self.assertEqual(pg.lane_scratch(self.run_, "a1"), ["neo-ui", "neo-api", "neo"])

    def test_the_joined_steps_folder_is_made_at_the_lanes_next_tool_call(self):
        self.assertEqual(sorted(p.name for p in self.pad.iterdir()), ["neo-api"])
        hooks._bind_member("claude", self.call)
        self.assertEqual(sorted(p.name for p in self.pad.iterdir()), ["neo-api", "neo-ui"])


class SharedFolderStepNameTests(unittest.TestCase):
    """Step names that differ only in case, spaces or punctuation gave their lanes one scratch folder: "api v2",
    "api-v2" and "API_V2" all wrote in scratchpad/<member id>-api-v2/, so one lane's files replaced another's. A plan
    refuses such a name, when it starts and when a step is added, and keeps what it had."""

    def test_a_plan_refuses_a_step_whose_name_gives_another_steps_folder(self):
        with TempState():
            with self.assertRaises(pg.StepError) as at_start:
                pg.start("Fix", plan=["api v2=30m", "web=30m", "API_V2=30m"], now=T0)
            self.assertIn("“api v2” and “API_V2”", str(at_start.exception))
            pg.start("Fix", plan=["api-v2=30m", "web=30m"], now=T0)
            with self.assertRaises(pg.StepError) as on_add:
                pg.add_step("api v2=30m", now=T0 + 60)
            self.assertIn("“api-v2” and “api v2”", str(on_add.exception))
            self.assertEqual([s["name"] for s in pg.load_run("claude", "s1")["plan"]], ["api-v2", "web"])


if __name__ == "__main__":
    unittest.main()
