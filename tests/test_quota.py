# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Claude's usage windows, recorded by devkit's status line (hooks.statusline → providers.claude_quota)."""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from devkit import hooks, providers  # noqa: E402


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (hooks.QUOTA, providers.STATE, hooks._stdin_json)
        hooks.QUOTA = Path(self.tmp.name) / "quota"
        providers.STATE = Path(self.tmp.name)
        os.environ["NO_COLOR"] = "1"

    def tearDown(self):
        hooks.QUOTA, providers.STATE, hooks._stdin_json = self.saved
        os.environ.pop("NO_COLOR", None)
        self.tmp.cleanup()

    def _line(self, payload):
        hooks._stdin_json = lambda: payload
        buf = io.StringIO()
        with redirect_stdout(buf):
            hooks.statusline("claude")
        return buf.getvalue().strip()

    def test_status_line_records_and_draws_usage(self):
        now = int(time.time())
        line = self._line({"model": {"display_name": "Claude Opus 5.5"}, "workspace": {"current_dir": self.tmp.name},
                           "context_window": {"used_percentage": 42.4},
                           "rate_limits": {"five_hour": {"used_percentage": 12, "resets_at": now + 3600},
                                           "seven_day": {"used_percentage": 83.6, "resets_at": now + 86400}}})
        self.assertIn("Opus 5.5 · ctx 42% · 5h 12% · 7d 84%", line)
        q = providers.claude_quota()
        self.assertEqual((q["available"], q["used_percent"], q["window_minutes"]), (True, 83.6, 10080))
        self.assertEqual(q["secondary_used_percent"], 12)

    def test_a_full_window_blocks_and_names_its_reset(self):
        now = int(time.time())
        self._line({"rate_limits": {"five_hour": {"used_percentage": 100, "resets_at": now + 600},
                                    "seven_day": {"used_percentage": 40, "resets_at": now + 86400}}})
        q = providers.claude_quota()
        self.assertFalse(q["available"])
        self.assertEqual((q["reached"], q["resets_at"]), ("5-hour", now + 600))

    def test_no_or_expired_reading_is_unknown_not_fine(self):
        self.assertIsNone(providers.claude_quota()["available"])
        self._line({"rate_limits": {"seven_day": {"used_percentage": 50, "resets_at": int(time.time()) + 60}}})
        self.assertIsNone(providers.claude_quota(now=time.time() + 120)["available"])
        self.assertIn("expired", providers.claude_quota(now=time.time() + 120)["note"])

    def test_no_rate_limits_still_draws_a_line(self):
        self.assertTrue(self._line({"model": {"display_name": "Claude Sonnet 5.5"}}).endswith("Sonnet 5.5"))


if __name__ == "__main__":
    unittest.main()
