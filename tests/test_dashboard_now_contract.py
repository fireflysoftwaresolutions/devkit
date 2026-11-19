# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The Now contract's check of a decision's option keys: a key cut with "…" is refused (no typed answer matches it),
but an option without a letter is answered by its whole text, and that text may itself end in "…". The contract's
example document with one decision's options changed. Run: python3 -m unittest tests.test_dashboard_now_contract -v"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import now_contract  # noqa: E402  (the contract check the server's tests share)

EXAMPLE = Path(__file__).resolve().parent / "fixtures" / "dashboard" / "now-example.json"


def option_errors(options) -> list:
    doc = json.loads(EXAMPLE.read_text())
    doc["attention"]["items"][0]["options"] = options
    return [e for e in now_contract.check(doc) if ".options[" in e]


class OptionKeyTests(unittest.TestCase):
    def test_a_key_ending_in_an_ellipsis_passes_only_when_it_is_the_options_whole_text(self):
        whole = {"key": "Wait for the build…", "text": "Wait for the build…", "recommended": True}
        cut = {"key": "Ship it once the release b…", "text": "Ship it once the release branch is green",
               "recommended": False}
        self.assertEqual(option_errors([whole]), [])
        self.assertEqual(len(option_errors([whole, cut])), 1)
        self.assertIn("options[1].key: ends with …", option_errors([whole, cut])[0])


if __name__ == "__main__":
    unittest.main()
