# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The timestamped copies `dev install` keeps of the agents' files before it changes them (lib/devkit/setup.py,
backup): rotation keeps the newest ones, however many were made in one second. The state folder and the files are
temporary. Run: python3 -m unittest discover -s tests -t ."""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import setup  # noqa: E402

SECOND = time.strptime("20261118-101500", "%Y%m%d-%H%M%S")  # every copy is made in this second


class BackupRotationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.file = root / "settings.json"
        self.backups = root / "state" / "backups"
        real = time.strftime
        for p in (mock.patch.object(setup, "STATE", root / "state"),
                  mock.patch.object(setup.time, "strftime", lambda fmt, t=None: real(fmt, SECOND))):
            p.start()
            self.addCleanup(p.stop)

    def kept(self) -> list:
        return sorted(int(p.read_text()) for p in self.backups.iterdir())

    def test_the_newest_copies_are_kept_when_more_than_ten_come_in_one_second(self):
        for n in range(14):
            self.file.write_text(str(n))
            setup.backup(self.file, keep=3)
        self.assertEqual(self.kept(), [11, 12, 13])

    def test_a_name_rotation_freed_is_never_given_to_the_newest_copy(self):
        for n in range(4):
            self.file.write_text(str(n))
            setup.backup(self.file, keep=2)
        self.assertEqual(self.kept(), [2, 3])
        self.assertFalse((self.backups / "settings.json.20261118-101500").exists())


if __name__ == "__main__":
    unittest.main()
