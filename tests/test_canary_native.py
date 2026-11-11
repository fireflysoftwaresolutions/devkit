# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import canary, cli


class CodexCanaryDisplayTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        saved = canary.LOG
        canary.LOG = Path(folder.name) / "canary.jsonl"
        self.addCleanup(setattr, canary, "LOG", saved)

    def render(self, rows):
        canary.LOG.write_text("".join(json.dumps(dict(ts=time.time(), **row)) + "\n" for row in rows))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = cli.cmd_canary(SimpleNamespace(days=7, limit=10))
        self.assertEqual(result, 0)
        return output.getvalue()

    def test_codex_crew_only_results_are_visible_without_a_main_score(self):
        rows = [dict(agent="codex", kind="crew", member="Neo", ok=True),
                dict(agent="codex", kind="crew", member="Kid", ok=False),
                dict(agent="codex", kind="crew", member="Mouse", ok=True),
                dict(agent="codex", kind="crew", member="Neo", ok=True, corrected=True),
                dict(agent="codex", kind="crew", member="internal", ok=True, internal=True)]
        output = self.render(rows)
        self.assertIn("Codex crew 2/3 reports carried the marker (66%)", output)
        self.assertIn("Codex main: no tracked replies yet", output)
        self.assertEqual(canary.summary(), {})
        # A Claude-only history retains its existing display and does not acquire Codex output.
        claude = self.render([dict(agent="claude", kind="crew", member="Neo", ok=True)])
        self.assertNotIn("Codex", claude)
        self.assertNotIn("crew 1/1", claude)

    def test_codex_crew_and_main_counts_stay_separate_in_mixed_history(self):
        output = self.render([dict(agent="codex", ok=True), dict(agent="claude", ok=True),
                              dict(agent="codex", kind="crew", member="Neo", ok=True),
                              dict(agent="codex", kind="crew", member="Kid", ok=False),
                              dict(agent="claude", kind="crew", member="Neo", ok=True)])
        self.assertIn("Codex crew 1/2 reports carried the marker (50%)", output)
        self.assertNotIn("Codex main: no tracked replies yet", output)
        self.assertEqual(canary.summary(), {"codex": (1, 1), "claude": (1, 1)})
        self.assertIn("Claude   1/1 replies carried the marker (100%)", output)


if __name__ == "__main__":
    unittest.main()
