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
from devkit import identity, progress as pg, watch  # noqa: E402
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



class WhoseWait(unittest.TestCase):
    # whose wait `dev watch --wait` is: the speaker identity resolves (tests/test_identity.py has every rule). A
    # member's wait in a pipeline carries no lane mark; it was taken for the main session's, superseded the tech
    # lead's wait and took its events (B-g3di13i0nfx). Now the member's own open call makes it the member's, the
    # tech lead's stamp makes it the main session's, and a wait devkit cannot place is refused.
    def consumer(self, lane, how, for_=None):
        args = type("Args", (), {"for_": for_})()
        found = identity._Found(identity.Speaker("claude", "s1", lane, how, True), "claude", "env", (),
                                lane if how == "ledger" else "")
        with mock.patch.object(identity, "_find", lambda *a, **k: found):
            return watch._consumer(args)

    def test_a_wait_whose_open_call_is_a_members_is_the_members(self):
        self.assertEqual(self.consumer("a_tank", "ledger"), "a_tank")
        self.assertEqual(self.consumer("a_tank", "ledger", for_="me"), "a_tank")

    def test_a_routed_member_call_is_the_members_with_or_without_for_me(self):
        self.assertEqual(self.consumer("a_ghost", "stamped"), "a_ghost")
        self.assertEqual(self.consumer("a_ghost", "stamped", for_="me"), "a_ghost")

    def test_for_me_never_resolves_to_the_main_session_and_no_wait_is_a_guess(self):
        self.assertEqual(self.consumer("main", "stamped"), "main")
        for lane, how, for_ in (("main", "stamped", "me"), ("", "unknown", "me"), ("", "unknown", None),
                                ("main", "stamped", "you")):
            with self.subTest(how=how, for_=for_), self.assertRaises(ValueError):
                self.consumer(lane, how, for_)


if __name__ == "__main__":
    unittest.main()
