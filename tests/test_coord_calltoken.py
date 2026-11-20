# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The call ledger keeps the hash of a call's token in the entry's one write, so an entry of a call that carries a
token never exists without it. Run: python3 -m unittest tests.test_coord_calltoken"""
import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import progress as pg  # noqa: E402
from test_progress import TempState  # noqa: E402


def call(tid):
    return {"session_id": "s1", "tool_use_id": tid, "tool_name": "Bash", "tool_input": {"command": "dev crew inbox"}}


class CallTokenHashTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)

    def test_the_entry_carries_the_token_hash_from_its_first_write(self):
        digest = hashlib.sha256(b"nonce-value").hexdigest()
        pg.call_opened("claude", call("toolu_tok1"), token_sha256=digest)
        self.assertEqual(pg.call_entry(call("toolu_tok1"))["token_sha256"], digest)

    def test_a_malformed_hash_writes_no_entry(self):
        for bad in ("nonce-value", "A" * 64, "ab" * 31):
            with self.assertRaises(ValueError):
                pg.call_opened("claude", call("toolu_tok3"), token_sha256=bad)
        self.assertEqual(pg.call_entry(call("toolu_tok3")), {})


if __name__ == "__main__":
    unittest.main()
