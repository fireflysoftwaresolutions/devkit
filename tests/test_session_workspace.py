# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The session record keeps the workspace's checkouts as they were when the session started
# (registry.workspace_baseline: each project's git folder by device and inode). Automatic policy reads it to tell a
# checkout that was there from one cloned, moved or replaced later, so only the record's creation writes it: a resume
# or a later SessionStart never refreshes it.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_session_workspace
import sys
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / "lib"))
sys.path.insert(0, str(TESTS))

from devkit import hooks, state  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_language_neutral_hooks import HookCase  # noqa: E402


class SessionWorkspaceTests(HookCase):
    def register(self, checkouts):
        # the workspace's checkouts as registry.workspace_baseline reads them (the handoff's project list and one
        # lstat each), fixed here so no test reads the developer's own workspace
        with mock.patch.object(hooks, "_session_file", lambda a, sid: pg.SESSIONS / f"{a}-{sid}.json"), \
                mock.patch("devkit.registry.workspace_baseline", lambda: checkouts):
            return hooks._register_session("claude", "s9", str(self.root), {})

    def test_a_new_session_record_carries_the_workspace_checkouts(self):
        start = {"projects": {"acme/api": [16777231, 4242]}, "partial": False}
        f = self.register(start)
        self.assertEqual(state.read(f)["workspace_checkouts"], start)

    def test_a_resumed_session_keeps_the_checkouts_of_its_start(self):
        start = {"projects": {"acme/api": [16777231, 4242]}, "partial": False}
        f = self.register(start)
        self.register({"projects": {"acme/api": [16777231, 9999], "acme/web": [16777231, 5151]},
                       "partial": False})  # a resume, after the API was cloned again
        self.assertEqual(state.read(f)["workspace_checkouts"], start)


if __name__ == "__main__":
    unittest.main()
