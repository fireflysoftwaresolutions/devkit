# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
import json
import unittest

from tests.test_codex_launch import CodexCrew, REVIEWER, TANK


class CodexPatchAttributionTests(CodexCrew):
    """Normalized hook contracts; file effects occur between real pre/post handlers, not injected call records."""
    def setup_lanes(self):
        s = self.lead(plan="api=30m [backend]; review=20m [review]")
        s.ok("progress", "assign", "review", "--to", "Seraph")
        self.spawn(s, TANK, "tank", "api")
        self.spawn(s, REVIEWER, "seraph", "review")
        (self.api / "api").mkdir(exist_ok=True)
        return s

    def patch_call(self, s, path="api/x.py", content="value = 1"):
        call = dict(tool_name="apply_patch", tool_use_id="patch-builder", agent_id=TANK, agent_type="tank",
                    tool_input={"input": f"*** Begin Patch\n*** Add File: {path}\n+{content}\n*** End Patch\n"})
        out = s.hook("guard", s.payload("PreToolUse", **call))
        return call, (out.get("hookSpecificOutput") or {})

    def writes(self, s):
        return s.run_doc()["agents"][REVIEWER].get("scope_writes") or {}

    def test_pending_patch_is_possible_only_for_its_declared_path_and_later_is_exact(self):
        s = self.setup_lanes()
        review = s.begin("git status --short", REVIEWER, "seraph")
        self.assertFalse(review["denied"], review)
        patch, verdict = self.patch_call(s)
        self.assertNotEqual(verdict.get("permissionDecision"), "deny", verdict)
        # The external file tool writes after its guard and before its PostToolUse, while the reader finishes.
        (self.api / "api" / "x.py").write_text("value = 1\n")
        (self.api / "api" / "unexplained.py").write_text("unknown = 1\n")
        s.finish(s.execute(review))
        self.assertEqual(self.writes(s).get("certain"), ["api/unexplained.py"])
        self.assertEqual(self.writes(s).get("paths"), ["api/unexplained.py", "api/x.py"])
        s.hook("post-tool", s.payload("PostToolUse", tool_response={"success": True}, **patch))
        edits = json.loads((self.state / "calls" / s.sid / ".edits.json").read_text())["edits"]
        self.assertEqual([(e["caller"], e["path"]) for e in edits], [(TANK, str(self.api / "api" / "x.py"))])
        again = s.begin("git status --short", REVIEWER, "seraph")
        s.finish(s.execute(again))
        self.assertEqual(self.writes(s).get("certain"), ["api/unexplained.py"])

    def test_denied_patch_supplies_no_writer_and_failed_patch_supplies_no_exact_edit(self):
        s = self.setup_lanes()
        review = s.begin("git status --short", REVIEWER, "seraph")
        patch, verdict = self.patch_call(s, path="outside.py")
        self.assertEqual(verdict.get("permissionDecision"), "deny", verdict)
        (self.api / "outside.py").write_text("unknown = 1\n")
        s.finish(s.execute(review))
        self.assertEqual(self.writes(s).get("certain"), ["outside.py"])
        patch, verdict = self.patch_call(s)
        self.assertNotEqual(verdict.get("permissionDecision"), "deny", verdict)
        s.hook("post-tool", s.payload("PostToolUse", tool_response={"isError": True, "error": "patch rejected"},
                                      **patch))
        path = self.state / "calls" / s.sid / ".edits.json"
        self.assertEqual(json.loads(path.read_text()).get("edits", []) if path.exists() else [], [])
        self.assertFalse((self.state / "calls" / s.sid / "patch-builder.json").exists())

    def test_failed_closed_patch_keeps_only_its_targets_possible(self):
        s = self.setup_lanes()
        review = s.begin("git status --short", REVIEWER, "seraph")
        patch, verdict = self.patch_call(s)
        self.assertNotEqual(verdict.get("permissionDecision"), "deny", verdict)
        (self.api / "api" / "x.py").write_text("partial = 1\n")
        (self.api / "api" / "unexplained.py").write_text("unknown = 1\n")
        s.hook("post-tool", s.payload("PostToolUse", tool_response={"success": False}, **patch))
        s.finish(s.execute(review))
        writes = self.writes(s)
        self.assertEqual(writes.get("paths"), ["api/unexplained.py", "api/x.py"])
        self.assertEqual(writes.get("certain"), ["api/unexplained.py"])

    def test_a_successful_patch_in_another_repository_does_not_clear_an_unknown_writer(self):
        s = self.setup_lanes()
        review = s.begin("git status --short", REVIEWER, "seraph")
        # A successful edit with the same relative path belongs to a different repository.
        call = dict(tool_name="apply_patch", tool_use_id="foreign-patch",
                    tool_input={"input": "*** Begin Patch\n*** Add File: api/x.py\n+value = 1\n*** End Patch\n",
                                "workdir": str(self.kit)})
        guard = s.hook("guard", s.payload("PreToolUse", **call))
        self.assertNotEqual((guard.get("hookSpecificOutput") or {}).get("permissionDecision"), "deny", guard)
        (self.kit / "api").mkdir(exist_ok=True)
        (self.kit / "api" / "x.py").write_text("value = 1\n")
        s.hook("post-tool", s.payload("PostToolUse", tool_response={"success": True}, **call))
        (self.api / "api" / "x.py").write_text("unknown = 1\n")
        s.finish(s.execute(review))
        self.assertEqual(self.writes(s).get("certain"), ["api/x.py"])


if __name__ == "__main__":
    unittest.main()
