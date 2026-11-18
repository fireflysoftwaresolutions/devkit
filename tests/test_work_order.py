# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The open items' order (work.ordered_open: bucket, priority, creation time) is the same however the items were
read: `created` has one-second steps, so items added in the same second break the tie by their id instead of by the
order a caller listed them in, and `dev task <n>` finds the item `dev task` numbered n. Pure function. Run:
python3 -m unittest tests.test_work_order -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import work  # noqa: E402


def item(iid: str, created: str = "2026-11-18T10:00:00Z") -> dict:
    return {"meta": {"id": iid, "bucket": "now", "priority": "normal", "status": "todo", "created": created}}


class OpenOrderTests(unittest.TestCase):
    def test_items_added_in_the_same_second_keep_one_order_however_they_were_read(self):
        first, second, older = item("T-ih0001aaaaaa"), item("T-ih0001bbbbbb"), item("T-ih0000zzzzzz", "2026-11-18T09:59:59Z")
        want = ["T-ih0000zzzzzz", "T-ih0001aaaaaa", "T-ih0001bbbbbb"]
        for listed in ([first, second, older], [second, first, older], [older, second, first]):
            self.assertEqual([i["meta"]["id"] for i in work.ordered_open(listed)], want)


if __name__ == "__main__":
    unittest.main()
