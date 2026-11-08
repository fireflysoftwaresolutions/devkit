# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# A crew member's own view of `dev watch`: the peek, `dev watches` and a wait that ends at once name the member as
# Role (Name), never by its agent id, and give the member its own reasons, never the main session's.
# Run: python3 -m unittest tests.test_watch_member
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import progress as pg, watch  # noqa: E402
from test_progress import launch  # noqa: E402
from test_state_e2e import dev  # noqa: E402
from test_watch import WatchCase  # noqa: E402

AID = "ab7b44761db23b396"  # an agent id as Claude Code gives it


class MemberView(WatchCase):
    def setUp(self):
        super().setUp()
        pg.start("Feature", plan=["docs", "tests"], now=self.t0)
        pg.assign("docs", "Ghost", now=self.t0)
        launch(AID, "devkit:ghost", "Ghost: docs", self.t0)
        self.stack.enter_context(mock.patch.dict(os.environ, {"DEVKIT_FROM_AGENT": AID}))

    def test_the_peek_names_the_member_role_first(self):
        rc, out, err = dev("watch")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.splitlines()[0], "watch · claude:s1 · for Technical writer (Ghost)")
        self.assertNotIn(AID, out.splitlines()[0])

    def test_dev_watches_names_a_members_wait_role_first(self):
        watch.register("claude", "s1", AID, os.getpid(), 5, now=self.t0)
        rc, out, err = dev("watches")
        self.assertEqual(rc, 0, out + err)
        line = next(ln for ln in out.splitlines() if " · for " in ln)
        self.assertIn(" · for Technical writer (Ghost) · ", line)
        self.assertNotIn(AID, line)

    def test_a_members_wait_with_no_question_of_its_own_says_so(self):
        script = self.world()
        rc, out, err = dev("watch", "--wait", "--for", "me", "--timeout", "240")
        self.assertEqual(rc, 2, out + err)
        self.assertEqual(out.strip(), "watch: nothing can arrive for you: none of your questions waits for an "
                                      "answer. End the turn without a wait.")
        self.assertEqual(script.calls, 0)


if __name__ == "__main__":
    unittest.main()
