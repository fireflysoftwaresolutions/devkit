# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Work item ids keep one width and their time order, across every id devkit wrote. Run:
python3 -m unittest tests.test_work_ids -v"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import work  # noqa: E402

YEAR = 365 * 86400


def id_at(seconds: int, kind: str = "task") -> str:
    """The id `new_id` draws `seconds` after 2026-01-01, with its six random characters all `0`."""
    with mock.patch.object(work.time, "time", lambda: work.EPOCH + seconds), \
            mock.patch("secrets.choice", lambda alphabet: "0"):
        return work.new_id(kind)


class IdOrderTests(unittest.TestCase):
    """An id is `<kind>-<time><6 random>`, and lists, duplicates and file names rely on ids sorting in time order.
    Five base36 characters of seconds since 2026 run out 36**5 seconds in, on 2027-12-01."""

    def test_ids_keep_one_width_and_their_time_order_past_the_five_character_count(self):
        ids = [id_at(s) for s in (0, 36 ** 4, 36 ** 5 - 1, 36 ** 5, 30 * YEAR)]
        self.assertEqual(sorted(ids), ids)
        self.assertEqual({len(i) for i in ids}, {len("T-") + 6 + 6})

    def test_a_new_id_sorts_after_every_id_the_five_character_form_wrote(self):
        # Ids with a five-character time part start with 0 to h until 2026-12-16; the newest possible one of them:
        self.assertGreater(id_at(0), "T-hzzzzzzzzzz")
        self.assertGreater(id_at(0, "bug"), "B-hzzzzzzzzzz")


if __name__ == "__main__":
    unittest.main()
