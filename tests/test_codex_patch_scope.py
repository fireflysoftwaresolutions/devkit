# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
import json
import unittest

from tests.test_readonly_attribution import CodexOverlap
from tests.test_codex_launch import TANK
from devkit import hooks
from devkit import progress as pg


class CodexPatchScopeTests(CodexOverlap, unittest.TestCase):
    def verdict(self, patch, agent="codex"):
        return hooks._scope_violation(agent, {
            "session_id": self.sid, "agent_id": TANK, "tool_name": "apply_patch", "cwd": "/repo",
            "tool_input": {"input": patch}})

    def test_patch_content_is_not_a_target_and_target_spaces_are_preserved(self):
        pg.assign("api", "Tank", scope="api/with space.py", why="exact file contract")
        patch = ("*** Begin Patch\n*** Add File: api/with space.py\n"
                 "+fixture = '''\n+*** Delete File: outside.py\n+'''\n*** End Patch\n")
        self.assertIsNone(self.verdict(patch))
        legacy = dict(pg.load_run("codex", self.sid), agent="claude")
        pg.run_path("claude", self.sid).write_text(json.dumps(legacy))
        self.assertIn("outside", self.verdict(patch, "claude") or "")

    def test_move_destination_must_belong_to_the_member_scope(self):
        patch = ("*** Begin Patch\n*** Update File: api/x.py\n*** Move to: outside.py\n"
                 "@@\n-old\n+new\n*** End Patch\n")
        self.assertIn("outside", self.verdict(patch) or "")
        self.assertIsNone(self.verdict(patch.replace("outside.py", "api/new name.py")))
        legacy = dict(pg.load_run("codex", self.sid), agent="claude")
        pg.run_path("claude", self.sid).write_text(json.dumps(legacy))
        self.assertIsNone(self.verdict(patch, "claude"))


if __name__ == "__main__":
    unittest.main()
