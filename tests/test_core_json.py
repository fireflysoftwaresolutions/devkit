# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""JSON files any teammate can write in the handoff repository (the crew overlay, the team's progress policy) are
read by core.read_json. A file nested far deeper than any devkit file must read as invalid, with each caller's
behavior for an invalid file, instead of exhausting the stack: Python 3.9 to 3.13 fail in the parser at about a
thousand levels, Python 3.15 at about 200,000, and what reads a value parsed deeper still can fail after it. Every
file here is temporary. Run: python3 -m unittest discover -s tests -t ."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core, crew, progress  # noqa: E402

DEPTHS = (100_000, 1_000_000)  # Python 3.15 parses the first; the second is past the parser's limit on every Python


def deep_list(d: int) -> str:
    return "[" * d + "]" * d


class DeepTeamFileTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def write(self, name: str, text: str) -> Path:
        f = self.root / name
        f.write_text(text)
        return f

    def test_a_deeply_nested_crew_overlay_reads_as_an_invalid_one(self):
        empty = {"members": {}, "activities": {}, "changelog": []}
        valid = self.write("valid.json", json.dumps({"members": {"neo": {"model": "opus"}}}))
        self.assertEqual(crew.read_overlay(valid)["members"], {"neo": {"model": "opus"}})
        for d in DEPTHS:
            with self.subTest(depth=d):
                f = self.write("crew.json", '{"members": {"neo": {"model": "opus", "notes": ' + deep_list(d) + "}}}")
                self.assertEqual(crew.read_overlay(f), empty)

    def test_a_deeply_nested_team_progress_policy_sets_no_ceiling(self):
        with mock.patch.object(progress, "TEAM_POLICY", self.write("progress.json", '{"max_parallel": 3}')):
            self.assertEqual(progress.team_ceiling(), 3)
        for d in DEPTHS:
            with self.subTest(depth=d):
                f = self.write("progress.json", '{"max_parallel": 3, "why": ' + deep_list(d) + "}")
                with mock.patch.object(progress, "TEAM_POLICY", f):
                    self.assertIsNone(progress.team_ceiling())

    def test_read_json_reads_any_depth_up_to_the_limit_and_none_past_it(self):
        at, past = core.JSON_MAX_DEPTH, core.JSON_MAX_DEPTH + 1
        self.assertEqual(core.read_json(self.write("at.json", deep_list(at)), "invalid"), json.loads(deep_list(at)))
        self.assertEqual(core.read_json(self.write("past.json", deep_list(past)), "invalid"), "invalid")
        self.assertEqual(core.read_json(self.write("cut.json", '{"a": '), "invalid"), "invalid")


if __name__ == "__main__":
    unittest.main()
