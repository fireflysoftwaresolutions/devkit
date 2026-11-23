"""Each crew lane keeps its scratch files in a folder of its own in the session scratchpad.

Two lanes of one crew member shared scratchpad/<member id>/, so one lane's scripts replaced and deleted the other's
files (two Security engineer lanes on different branches did). A lane that runs a plan step writes in
scratchpad/<member id>-<step>/ instead: the hook makes it once the lane binds to its launch and so knows its step
(SubagentStart comes before that), and the brief names it. The member's plain scratchpad/<member id>/ stays for a
lane that runs no step. The fixtures are a temporary state folder and a temporary scratchpad; no live hook runs."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))
from devkit import core, crew, hooks, progress as pg, ui  # noqa: E402
from test_progress import TempState  # noqa: E402

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
        for p in (mock.patch.object(core, "CLAUDE_HOME", home), mock.patch.object(hooks, "_claude_tmp", lambda: self.tmp)):
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


if __name__ == "__main__":
    unittest.main()
