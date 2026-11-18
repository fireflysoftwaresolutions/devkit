# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Writers of a handoff note take the registry's notes lock, which is reentrant within a thread: `dev sync` holds it
around its note writes, and a writer inside takes it again without waiting for itself (a plain second core.Lock of
the same file waits its full timeout, then writes without the lock). devkit's state and notes are temporary
folders. Run: python3 -m unittest tests.test_registry_notes_lock -v"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import branches, core, registry  # noqa: E402


class NotesLockTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for target in (mock.patch.object(core, "STATE", root / "state"),
                       mock.patch.object(core, "CONFIG_FILE", root / "config.json"),
                       mock.patch.object(core, "HANDOFF", root / "handoff"),
                       mock.patch.object(core, "NOTES", root / "handoff" / "projects")):
            target.start()
            self.addCleanup(target.stop)

    def test_an_embargo_written_while_sync_holds_the_notes_lock_does_not_wait_for_it(self):
        waited = AssertionError("the embargo writer waited for the notes lock its own thread holds")
        with registry.notes_lock(wait=1) as held, mock.patch.object(core.time, "sleep", side_effect=waited):
            self.assertTrue(held)
            branches.set_embargo("acme/api", "fix/token-leak", why="unshipped security fix", who="ana")
        self.assertEqual([b for b in branches.read_embargoes("acme/api")[0]], ["fix/token-leak"])


if __name__ == "__main__":
    unittest.main()
