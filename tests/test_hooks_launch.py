# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# A crew launch is checked before it starts (hooks._launch_naming, run by the PreToolUse hook on Claude Code's Agent
# call): a step binds only to the launch whose description names it (progress._bind_step), so a description that
# names none of its member's steps gets the name of the one step waiting for that launch, in the same updatedInput
# that carries the brief's required skills; with several waiting, or several named equally, the tech lead gets the
# exact description to use. Progress state and the audit trail in a temporary folder (test_crew.TempState).
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_hooks_launch -v
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()  # run alone, this module gets the suite's temporary home too
sys.path.insert(0, str(ROOT / "lib"))

from devkit import audit, hooks  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_crew import TempState  # noqa: E402

VAGUE = "Mouse: visual acceptance tests"  # shares no word with its step "v23-qa": without the hook it takes no step


def call(description: str, tid: str = "toolu_m1", kind: str = "devkit:mouse") -> dict:
    return {"session_id": "s1", "tool_name": "Agent", "tool_use_id": tid, "hook_event_name": "PreToolUse",
            "cwd": tempfile.gettempdir(),
            "tool_input": {"description": description, "subagent_type": kind, "prompt": "Write the tests."}}


def pre_tool(data: dict) -> dict:
    # the hook as Claude Code runs it, in-process: its one JSON output, {} when it prints none
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        hooks._pre_tool("claude", data)
    return (json.loads(out.getvalue()) if out.getvalue().strip() else {}).get("hookSpecificOutput") or {}


def started(aid: str = "a1", tid: str = "toolu_m1", kind: str = "devkit:mouse"):
    # the launched agent starts and the Agent call's result names it, as the hooks record them
    pg.on_subagent("claude", {"session_id": "s1", "agent_id": aid, "agent_type": kind}, True)
    pg.launch_result("claude", {"session_id": "s1", "tool_use_id": tid, "tool_response": {"agentId": aid}})


def owner(step: str) -> str:
    return next(s.get("owner") for s in pg.load_run("claude", "s1")["plan"] if s["name"] == step)


class OneWaitingStepTests(unittest.TestCase):
    def test_a_launch_that_names_no_step_gets_the_waiting_steps_name_and_takes_that_step(self):
        with TempState():
            pg.start("Acceptance", plan=["v23-qa", "docs"])
            pg.assign("v23-qa", "Mouse")
            new = pre_tool(call(VAGUE)).get("updatedInput") or {}
            started()
            self.assertEqual(new.get("description"), "Mouse: v23-qa visual acceptance tests")
            self.assertEqual(owner("v23-qa"), "a1")  # the launch record holds the new description too

    def test_the_tech_lead_reads_the_added_step_in_the_context_and_the_audit_records_it(self):
        with TempState():
            pg.start("Acceptance", plan=["v23-qa"])
            pg.assign("v23-qa", "Mouse")
            context = pre_tool(call(VAGUE)).get("additionalContext") or ""
            records = [r for r in audit.query(days=1) if r.get("action") == "crew.launch-named"]
        self.assertIn("“Mouse: visual acceptance tests” named none of QA engineer (Mouse)'s steps", context)
        self.assertIn("it starts as “Mouse: v23-qa visual acceptance tests” and takes “v23-qa”", context)
        self.assertEqual(len(records), 1, records)
        self.assertIn("launched as “Mouse: v23-qa visual acceptance tests” for “v23-qa”", records[0]["detail"])

    def test_the_added_step_and_the_briefs_required_sections_go_out_in_one_input(self):
        with TempState():
            pg.start("Acceptance", plan=["v23-qa"])
            pg.assign("v23-qa", "Mouse")
            new = pre_tool(call(VAGUE)).get("updatedInput") or {}
        self.assertEqual(new.get("description"), "Mouse: v23-qa visual acceptance tests")
        self.assertTrue(new.get("prompt", "").startswith("Write the tests."), new)
        self.assertIn("## devkit · your report", new.get("prompt", ""))
        self.assertEqual(new.get("subagent_type"), "devkit:mouse")

    def test_a_description_without_a_member_prefix_gets_the_step_name_in_front(self):
        with TempState():
            pg.start("Acceptance", plan=["v23-qa"])
            pg.assign("v23-qa", "Mouse")
            new = pre_tool(call("visual acceptance tests")).get("updatedInput") or {}
            started()
            self.assertEqual(new.get("description"), "v23-qa visual acceptance tests")
            self.assertEqual(owner("v23-qa"), "a1")


class NoticeTests(unittest.TestCase):
    def test_several_waiting_steps_get_a_notice_with_the_description_for_each_and_no_rewrite(self):
        with TempState():
            pg.start("Acceptance", plan=["v23-qa", "v24-a11y"])
            for step in ("v23-qa", "v24-a11y"):
                pg.assign(step, "Mouse")
            out = pre_tool(call(VAGUE))
            given = [hooks._launch_naming("claude", call(d, tid=f"toolu_{i}"))
                     for i, d in enumerate(("Mouse: v23-qa visual acceptance tests",
                                            "Mouse: v24-a11y visual acceptance tests"))]
        context = out.get("additionalContext") or ""
        self.assertEqual((out.get("updatedInput") or {}).get("description", VAGUE), VAGUE)
        self.assertIn("names none of QA engineer (Mouse)'s steps, and 2 wait for its launch (“v23-qa”, “v24-a11y”)",
                      context)
        self.assertIn("“Mouse: v23-qa visual acceptance tests” for “v23-qa”", context)
        self.assertIn("“Mouse: v24-a11y visual acceptance tests” for “v24-a11y”", context)
        self.assertEqual(given, [(None, []), (None, [])])  # each description it gives names its step alone

    def test_a_description_that_names_two_steps_equally_gets_a_notice_that_tells_them_apart(self):
        with TempState():
            pg.start("Tests", plan=["visual tests", "unit tests"])
            for step in ("visual tests", "unit tests"):
                pg.assign(step, "Mouse")
            new, told = hooks._launch_naming("claude", call("Mouse: tests"))
            again = hooks._launch_naming("claude", call("Mouse: visual tests", tid="toolu_m2"))
        self.assertIsNone(new)
        self.assertEqual(len(told), 1, told)
        self.assertIn("“Mouse: tests” names “visual tests”, “unit tests” equally", told[0])
        self.assertIn("“Mouse: visual tests” for “visual tests” · “Mouse: unit tests” for “unit tests”", told[0])
        self.assertEqual(again, (None, []))


if __name__ == "__main__":
    unittest.main()
