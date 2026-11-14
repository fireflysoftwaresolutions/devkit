# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
from tests.test_evidence import EvidenceCase
from devkit import guard


class BrowserTabGuard(EvidenceCase):
    def test_exact_local_tab_creation_remains_a_restricted_write(self):
        tool = "mcp__claude-in-chrome__tabs_create_mcp"
        self.assertEqual(guard.mcp_external(tool), "")
        self.assertEqual(guard.mcp_kind(tool), "write")
        self.assertIsNone(guard.member_tool_verdict(tool, {}, str(self.root), level="build", member="a1"))
        for level in ("read", "review"):
            self.assertIsNotNone(guard.member_tool_verdict(tool, {}, str(self.root), level=level, member="a1"))
        self.assertIsNotNone(guard.member_tool_verdict(tool, {"filePath": "/outside/shot.png"}, str(self.root),
                                                     level="build", member="a1", scope=["src/**"]))
        for other in ("mcp__unknown__tabs_create_mcp", "mcp__claude-in-chrome__send_message"):
            self.assertEqual(guard.mcp_external(other), "external-message")
