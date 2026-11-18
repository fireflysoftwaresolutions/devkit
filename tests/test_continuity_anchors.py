# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The anchors of a developer's request that a compaction summary must keep (continuity.anchors) read code spans as
Markdown does: a run of backticks opens a span and the next run of the same length closes it, so a value quoted with
longer backticks, which holds a backtick of its own, stays one anchor. Pure function. Run:
python3 -m unittest tests.test_continuity_anchors -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import continuity  # noqa: E402


class CodeSpanAnchorTests(unittest.TestCase):
    def test_a_value_fenced_with_longer_backticks_is_one_anchor_with_its_backtick(self):
        found = continuity.anchors("run ``echo `date` > out`` in the build step")
        self.assertIn("echo `date` > out", found)
        self.assertNotIn("date", found)

    def test_one_space_inside_each_fence_is_padding(self):
        self.assertIn("`v26`", continuity.anchors("tag it `` `v26` `` today"))


if __name__ == "__main__":
    unittest.main()
