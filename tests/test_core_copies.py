# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The copies of the configuration devkit's writers keep (lib/devkit/core.py) and the copy the restore hint offers:
always a copy of the file that broke. The configuration files and the state folder are temporary; the developer's
own are never touched. Run: python3 -m unittest discover -s tests -t ."""
import contextlib
import io
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core  # noqa: E402

STAMP = "20261117T195600Z"  # every copy is made in this second


class RestoreHintSourceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name).resolve()
        self.mine, self.other = root / "mine" / "config.json", root / "other" / "config.json"  # DEVKIT_CONFIG elsewhere
        for f in (self.mine, self.other):
            f.parent.mkdir()
        clock = time.strptime(STAMP, "%Y%m%dT%H%M%SZ")
        for p in (mock.patch.object(core, "STATE", root / "state"), mock.patch.object(core, "CONFIG_FILE", self.mine),
                  mock.patch.object(core, "_copy_warned", [False]),
                  mock.patch.object(core.time, "gmtime", return_value=clock)):
            p.start()
            self.addCleanup(p.stop)

    def change(self, path, editor):
        with mock.patch.object(core, "CONFIG_FILE", path):
            core.config_update(lambda saved: {**saved, "editor": editor})

    def test_the_hint_offers_only_a_copy_of_the_file_that_broke(self):
        self.mine.write_text('{"editor":"vim"}\n')
        self.change(self.mine, "nano")  # keeps mine: vim
        self.other.write_text('{"editor":"emacs"}\n')
        self.change(self.other, "ed")  # keeps the other file's emacs, the newest copy of all
        self.mine.write_text('{"editor":')
        copy_of_mine = next(c for c in core.config_copies() if c.read_text() == '{"editor":"vim"}\n')
        self.assertEqual(core.config_restore_hint(self.mine), f"restore with: cp {copy_of_mine} {self.mine}")

    def test_a_file_with_no_copy_of_its_own_gets_no_hint(self):
        self.other.write_text('{"editor":"emacs"}\n')
        self.change(self.other, "ed")
        self.mine.write_text('{"editor":')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(core.config_restore_hint(self.mine), "")


if __name__ == "__main__":
    unittest.main()
