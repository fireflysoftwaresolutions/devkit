# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Hook payload capture (spec C9) and contract tests on real payloads.

Capture: with DEVKIT_HOOK_CAPTURE=1 every `dev hook <event>` keeps its payload, redacted, under
STATE/captures/<agent>/; without it nothing changes. Contract: the fixtures in tests/fixtures/hooks/claude/ are
payloads Claude Code really sent (captured, redacted); every field devkit's code reads from an event must be in them.
Run: python3 -m unittest tests.test_hook_capture"""
import inspect
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import audit, canary, hooks, progress, scope, skillguard  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hooks" / "claude"

FAKE_PAT = "ghp_" + "0123456789abcdefABCDEF0123456789abcd"  # built at runtime: no token-shaped literal in the file
FAKE_BEARER = "Bearer " + "abc123.def456-ghi789"


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.captures = root / "captures"
        self.patches = [mock.patch.object(hooks, "CAPTURES", self.captures),
                        mock.patch.object(hooks, "STATE", root / "state")]
        for p in self.patches:
            p.start()
        self.seen = []
        self.patches.append(mock.patch.dict(hooks.HANDLERS, {"probe": lambda agent: self.seen.append(
            hooks._stdin_json())}))
        self.patches[-1].start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def fire(self, payload, event="probe", capture=True, raw=None):
        env = {"DEVKIT_HOOK_CAPTURE": "1"} if capture else {}
        with mock.patch.dict(os.environ, env), mock.patch("sys.stdin", io.StringIO(
                raw if raw is not None else json.dumps(payload))):
            if not capture:
                os.environ.pop("DEVKIT_HOOK_CAPTURE", None)
            return hooks.run(event, "claude")

    def files(self):
        return sorted(p.name for p in self.captures.rglob("*.json")) if self.captures.exists() else []

    def saved(self, name):
        return json.loads((self.captures / "claude" / name).read_text())

    def test_without_the_variable_nothing_is_captured_and_the_handler_gets_the_payload(self):
        payload = {"hook_event_name": "Stop", "session_id": "s1"}
        self.fire(payload, capture=False)
        self.assertEqual(self.files(), [])
        self.assertEqual(self.seen, [payload])

    def test_with_the_variable_the_payload_is_saved_and_the_handler_still_gets_all_of_it(self):
        payload = {"hook_event_name": "SubagentStart", "session_id": "s1", "agent_id": "a1",
                   "prompt": "p" * 500}
        self.fire(payload)
        self.assertEqual(self.files(), ["SubagentStart-1.json"])
        self.assertEqual(self.saved("SubagentStart-1.json")["agent_id"], "a1")
        self.assertEqual(self.seen, [payload])  # the handler reads the uncut payload

    def test_prompts_tool_text_and_replies_are_cut_tokens_removed_and_paths_kept_whole(self):
        long_path = "/Users/x/.claude/projects/" + "d" * 230 + "/s1.jsonl"
        self.fire({"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "s1",
                   "transcript_path": long_path, "prompt": "q" * 300,
                   "last_assistant_message": "m" * 300, "reason": f"pushed with {FAKE_PAT}",
                   "tool_input": {"command": f"curl -H 'Authorization: {FAKE_BEARER}' x", "api_key": "k1",
                                  "edits": [{"new_string": "n" * 300}]},
                   "tool_response": {"stdout": "o" * 300}})
        got = self.saved("PreToolUse.Bash-1.json")
        self.assertEqual(got["transcript_path"], long_path)
        for text in (got["prompt"], got["last_assistant_message"], got["tool_input"]["edits"][0]["new_string"],
                     got["tool_response"]["stdout"]):
            self.assertEqual(len(text), hooks.CAPTURE_CUT + 1)  # 200 characters and an ellipsis
        self.assertEqual(got["tool_input"]["api_key"], "[redacted]")
        dump = json.dumps(got)
        self.assertNotIn(FAKE_PAT, dump)
        self.assertNotIn("abc123.def456", dump)
        self.assertIn("pushed with [redacted]", got["reason"])

    def test_only_the_last_twenty_are_kept_per_event_and_tool(self):
        self.fire({"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_input": {"description": "Tank"}})
        for i in range(hooks.CAPTURE_KEEP + 3):
            self.fire({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": f"echo {i}"}})
        bash = [n for n in self.files() if n.startswith("PreToolUse.Bash-")]
        self.assertEqual(len(bash), hooks.CAPTURE_KEEP)
        self.assertNotIn("PreToolUse.Bash-3.json", bash)
        self.assertIn("PreToolUse.Bash-4.json", bash)
        self.assertEqual(self.saved("PreToolUse.Bash-23.json")["tool_input"]["command"], "echo 22")
        self.assertIn("PreToolUse.Agent-1.json", self.files())  # Bash calls do not push the launch out

    def test_a_capture_that_cannot_be_written_never_breaks_the_hook(self):
        self.captures.parent.mkdir(parents=True, exist_ok=True)
        self.captures.write_text("a file where the folder should be")
        payload = {"hook_event_name": "Stop", "session_id": "s1"}
        self.assertEqual(self.fire(payload), 0)
        self.assertEqual(self.seen, [payload])

    def test_a_payload_that_is_not_json_is_kept_cut_and_the_handler_runs(self):
        self.fire(None, raw="not json " + "z" * 400)
        got = self.saved("probe-1.json")
        self.assertEqual(len(got["unparsed"]), hooks.CAPTURE_CUT)
        self.assertEqual(self.seen, [{}])

    def test_events_devkit_does_not_handle_are_captured_for_spikes(self):
        self.fire({"hook_event_name": "PostToolUseFailure", "tool_name": "Agent"}, event="post-tool-failure")
        self.assertEqual(self.files(), ["PostToolUseFailure.Agent-1.json"])


# The payload fields a function reads: data.get("x"), data["x"], (data or {}).get("x").
FIELD_READ = re.compile(r"""(?:\bdata|\(data or \{\}\))(?:\.get\(\s*|\[\s*)["']([a-z_]+)["']""")
# Per event: every function that reads that event's payload, and the real payloads captured from Claude Code 2.1.319
# (F0 of the agent and session lifecycle spec). Add a function here when it starts reading a payload.
CONTRACT = {
    "UserPromptSubmit": ((hooks.prompt, progress.on_prompt), ("UserPromptSubmit",)),
    "SessionStart": ((hooks.session_start, hooks._register_session, canary.surface),
                     ("SessionStart.startup", "SessionStart.resume")),
    "PreToolUse": ((hooks.guard, hooks._crew_limits, hooks._stub_commit_gate, hooks._scope_violation,
                    hooks._route_subagent_progress, hooks._unroutable_lane_command, progress.tool_started,
                    progress._note_agent_launch),
                   ("PreToolUse.Agent", "PreToolUse.Agent.in-subagent", "PreToolUse.Bash")),
    "PostToolUse": ((hooks.post_tool, hooks.closing_reminder, hooks._turn_ending, hooks._bulk_rewrite_note,
                     progress.on_tool_done),
                    ("PostToolUse.Agent", "PostToolUse.Agent.background", "PostToolUse.Agent.in-subagent",
                     "PostToolUse.Bash")),
    "SubagentStart": ((hooks.subagent_start, progress._on_subagent, progress._launch_description), ("SubagentStart",)),
    "SubagentStop": ((hooks.subagent_stop, hooks._launch_meta, canary.check_subagent, progress._on_subagent),
                     ("SubagentStop", "SubagentStop.sent-back")),
    "Stop": ((hooks.stop, hooks._stub_gate, hooks._test_gate, hooks._turn_files, hooks._handoff_nudge,
              hooks._session_project, canary.check, scope.decide, skillguard.on_stop), ("Stop",)),
    "SessionEnd": ((hooks.session_end,), ("SessionEnd",)),
}
CODEX_NAMES = {"subagent_id", "subagent_type"}  # read only when Claude's agent_id / agent_type are missing
# Fields devkit reads that Claude Code does not send. Each one is a known bug with the fix that removes the read.
KNOWN_GAPS = {"SubagentStart": {"agent_transcript_path"}}  # F2: SubagentStart carries no agent_transcript_path


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def strings(v):
    if isinstance(v, str):
        yield v
    for x in (v.values() if isinstance(v, dict) else v if isinstance(v, list) else ()):
        yield from strings(x)


def missing(event: str) -> set:
    fns, names = CONTRACT[event]
    read = set().union(*(FIELD_READ.findall(inspect.getsource(fn)) for fn in fns))
    sent = set().union(*(fixture(n).keys() for n in names))  # agent_id: only the in-subagent payloads carry it
    return read - sent - CODEX_NAMES


class PayloadContractTests(unittest.TestCase):
    """Fixtures are real Claude Code payloads (captured with DEVKIT_HOOK_CAPTURE=1, redacted, home path → /Users/dev).
    Refresh them from STATE/captures/claude/ after a Claude Code upgrade and these tests show what moved."""

    def test_every_field_devkit_reads_is_in_the_payload_claude_code_sends(self):
        for event in CONTRACT:
            with self.subTest(event=event):
                self.assertEqual(missing(event) - KNOWN_GAPS.get(event, set()), set())

    @unittest.expectedFailure
    def test_subagent_start_carries_the_agent_transcript_path_devkit_reads(self):
        """progress._launch_description reads agent_transcript_path at SubagentStart; Claude Code sends only
        transcript_path and agent_id, and writes <session>/subagents/agent-<id>.meta.json after the SubagentStart
        hooks return, so the description is never found and the start takes the oldest queued launch. F2 binds the
        lane at the subagent's first hook (tool_use_id and the meta file) instead: it removes the read, this
        decorator and the KNOWN_GAPS entry."""
        self.assertEqual(missing("SubagentStart"), set())

    def test_an_agent_launch_carries_the_tool_input_devkit_reads(self):
        for name in ("PreToolUse.Agent", "PreToolUse.Agent.in-subagent", "PostToolUse.Agent.background"):
            with self.subTest(name):  # label, type, brief (progress, skillguard), background (closing reminder)
                self.assertLessEqual({"description", "subagent_type", "prompt", "run_in_background"},
                                     set(fixture(name)["tool_input"]))

    def test_the_launch_result_and_the_stop_name_the_agent_exactly(self):
        """What exact binding (F2) builds on: the launch's tool_use_id and the agentId of the agent it started."""
        pre, post = fixture("PreToolUse.Agent"), fixture("PostToolUse.Agent.background")
        self.assertEqual(pre["tool_use_id"], post["tool_use_id"])
        start = fixture("SubagentStart")
        self.assertEqual(post["tool_response"]["agentId"], start["agent_id"])
        self.assertTrue(post["tool_response"]["isAsync"])
        stop = fixture("SubagentStop")
        self.assertTrue(stop["agent_transcript_path"].endswith(f"/subagents/agent-{stop['agent_id']}.jsonl"))
        self.assertNotIn("tool_use_id", start)  # SubagentStart cannot bind by itself

    def test_a_failed_launch_comes_as_post_tool_use_failure_not_post_tool_use(self):
        """An unknown subagent_type: PreToolUse, then PostToolUseFailure with the same tool_use_id (C2 `failed`)."""
        fail = fixture("PostToolUseFailure.Agent")
        self.assertEqual((fail["tool_name"], fail["is_interrupt"]), ("Agent", False))
        self.assertIn("not found", fail["error"])
        self.assertTrue(fail["tool_use_id"].startswith("toolu_"))

    def test_the_fixtures_hold_no_tokens_home_paths_or_uncut_text(self):
        for f in sorted(FIXTURES.glob("*.json")):
            with self.subTest(f.name):
                text = f.read_text()
                self.assertIsNone(audit.SECRET.search(text))
                self.assertNotIn(str(Path.home()), text)
                d = json.loads(text)
                for key in hooks.CAPTURE_CUT_KEYS:
                    for s in strings(d.get(key)):
                        self.assertLessEqual(len(s), hooks.CAPTURE_CUT + 1)


if __name__ == "__main__":
    unittest.main()
