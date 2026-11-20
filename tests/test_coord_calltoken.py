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


class HistoryMarksTests(unittest.TestCase):
    """A step change records how its caller was found and the call that typed it (the audit fields)."""

    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.addCleanup(pg.mark_calls, {})

    def test_a_dev_process_resolves_its_caller_once_and_each_history_entry_names_it(self):
        from unittest import mock
        from devkit import cli, identity
        sp = identity.Speaker("claude", "s1", "main", "token", True)
        with mock.patch.dict("os.environ", {identity.CALL_VAR: "toolu_hist1." + "a" * 22}), \
                mock.patch.object(identity, "speaker", return_value=sp) as found:
            cli.main(["progress", "start", "Export", "--plan", "api=30m; docs=15m"])
            pg.reopen("api", "skip it", skip=True)
        found.assert_called_once()
        entries = [h for h in pg.load_run("claude", "s1")["history"] if h["step"] in ("api", "docs")]
        self.assertTrue(entries)
        self.assertEqual({(h.get("how"), h.get("tid")) for h in entries}, {("token", "toolu_hist1")})

    def test_without_a_call_token_nothing_is_resolved_and_no_marks_are_written(self):
        from unittest import mock
        from devkit import cli, identity
        with mock.patch.object(identity, "speaker") as found:
            cli.main(["progress", "start", "Export", "--plan", "api=30m; docs=15m"])
            pg.reopen("api", "skip it", skip=True)
        found.assert_not_called()
        self.assertFalse([h for h in pg.load_run("claude", "s1")["history"] if "tid" in h])

    def test_the_background_sync_starts_without_the_call_token(self):
        from unittest import mock
        from devkit import cli, identity
        with mock.patch.dict("os.environ", {identity.CALL_VAR: "toolu_hist2." + "a" * 22}), \
                mock.patch.dict("os.environ", {"DEVKIT_NO_AUTOSYNC": ""}), \
                mock.patch("subprocess.Popen") as popen:
            popen.return_value.pid = 4242
            cli._spawn_flusher(0)
        self.assertTrue(popen.called)
        self.assertNotIn(identity.CALL_VAR, popen.call_args.kwargs.get("env") or {identity.CALL_VAR: ""})


if __name__ == "__main__":
    unittest.main()
