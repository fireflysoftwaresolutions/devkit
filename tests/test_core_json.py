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


class LoadJsonErrorTests(unittest.TestCase):
    """core.load_json's errors say what is wrong, so a refusal can name it: its own depth error, NaN and Infinity
    when the caller reads text Node must parse too, and the parser's other errors as they are."""

    def test_too_deep_raises_the_depth_error_and_cut_short_does_not(self):
        for text in (deep_list(core.JSON_MAX_DEPTH + 1), deep_list(1_000_000)):
            with self.assertRaises(core.JSONDepthError):
                core.load_json(text)
        with self.assertRaises(ValueError) as cut:
            core.load_json('{"a": ')
        self.assertNotIsInstance(cut.exception, core.JSONDepthError)
        self.assertIsInstance(core.JSONDepthError("x"), ValueError)  # every caller that catches ValueError still does

    def test_nan_and_infinity_are_refused_only_when_the_caller_asks(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant):
                text = '{"x": [1, ' + constant + "]}"
                self.assertEqual(json.dumps(core.load_json(text)), '{"x": [1, ' + constant + "]}")
                with self.assertRaises(core.JSONNonFiniteError):
                    core.load_json(text, allow_nan=False)
        self.assertEqual(core.load_json('{"x": 1e5, "NaN": "Infinity"}', allow_nan=False), {"x": 1e5, "NaN": "Infinity"})

    def test_read_json_passes_allow_nan_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "s.json"
            f.write_text('{"x": NaN}')
            self.assertEqual(core.read_json(f, "unread", allow_nan=False), "unread")
            self.assertEqual(list(core.read_json(f, "unread")), ["x"])


if __name__ == "__main__":
    unittest.main()
