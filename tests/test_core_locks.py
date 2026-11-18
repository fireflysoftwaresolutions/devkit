# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""devkit's lock on one file (lib/devkit/core.py, file_lock) when its lock file is removed: `dev sleep` deletes the
lock files nobody holds, and a process that opened one just before keeps no lock of its own. The state folder is
temporary. Run: python3 -m unittest discover -s tests -t ."""
import fcntl
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core  # noqa: E402


class FileLockRemovedTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        p = mock.patch.object(core, "STATE", self.root / "state")
        p.start()
        self.addCleanup(p.stop)

    def test_a_lock_file_removed_between_its_open_and_its_flock_never_gives_two_holders(self):
        path, real = self.root / "plan.md", core._flock_within
        other_holds, release, other = threading.Event(), threading.Event(), []

        def hold():  # another process: it locks the file the path names now
            with core.file_lock(path, wait=5) as ok:
                if ok:
                    other_holds.set()
                    release.wait(5)

        def race(fd, wait):
            if not other:  # between this open and its flock: the lock file is removed, and another process locks
                os.unlink(next((core.STATE / "locks" / "files").glob("*.lock")))
                other.append(threading.Thread(target=hold))
                other[0].start()
                other_holds.wait(5)
            return real(fd, wait)
        with mock.patch.object(core, "_flock_within", race):
            with core.file_lock(path, wait=0.3) as ok:
                both = ok and other_holds.is_set()
        release.set()
        other[0].join(5)
        self.assertTrue(other_holds.is_set())
        self.assertFalse(both, "two holders of the lock on one file")


class LockExitTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = mock.patch.object(core, "STATE", Path(tmp.name) / "state")
        p.start()
        self.addCleanup(p.stop)

    def test_a_second_exit_of_a_named_lock_touches_no_file(self):
        # flush_handoff leaves its lock on an early return and again in its finally: the second exit unlocks nothing
        lock, ops, real = core.Lock("handoff-flush"), [], fcntl.flock
        self.assertTrue(lock.__enter__())

        def flock(fd, op):
            ops.append(op)
            return real(fd, op)
        with mock.patch.object(core.fcntl, "flock", flock):
            lock.__exit__(None, None, None)
            lock.__exit__(None, None, None)
        self.assertEqual(ops, [fcntl.LOCK_UN])
        with core.Lock("handoff-flush") as free:
            self.assertTrue(free)


if __name__ == "__main__":
    unittest.main()
