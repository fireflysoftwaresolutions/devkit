# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""A lock renewal reads each item again under the item's record lock before it writes (read-modify-write), so a
change another session made after the renewal listed the items is kept and the lock still renews, instead of the
renewal writing from its older read (refused, and the lock left to run out). The handoff repository is a temporary
folder. Run: python3 -m unittest tests.test_work_lock_renewal -v"""
import contextlib
import io
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import test_work as W  # noqa: E402  (the temporary handoff and the actors)
from devkit import work  # noqa: E402

LIVE = {"project": "acme/api", "branch": "feat/export", "active": True}


class RenewalReadTests(unittest.TestCase):
    def test_a_renewal_keeps_a_change_made_after_it_listed_the_items_and_still_renews(self):
        with W.TempHandoff():
            it = work.claim(work.create("acme/api", "Export invoices", a=W.A), a=W.A, hours=1, branch="feat/export")
            listed = work.all_items

            def edited_after_the_listing(scope=None):
                items = listed(scope)
                work.update(work.load(it["path"]), "edit", why="finance asked first", a=W.A, priority="high")
                return items

            with mock.patch.object(work, "all_items", edited_after_the_listing), \
                    contextlib.redirect_stderr(io.StringIO()):
                renewed = work.renew_locks(W.A, hours=8, sessions=[LIVE])
            meta = work.load(it["path"])["meta"]
        self.assertEqual(renewed, 1)
        self.assertEqual(meta["priority"], "high")
        self.assertGreater(work.parse_iso(meta["lock_expires"]) - time.time(), 7.9 * 3600)


if __name__ == "__main__":
    unittest.main()
