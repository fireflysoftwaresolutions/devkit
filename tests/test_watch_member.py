# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# A crew member's own view of `dev watch`: the peek, `dev watches` and a wait that ends at once name the member as
# Role (Name), never by its agent id, and give the member its own reasons, never the main session's.
# Run: python3 -m unittest tests.test_watch_member
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import identity, progress as pg, state, watch  # noqa: E402
from test_progress import launch  # noqa: E402
from test_state_e2e import dev  # noqa: E402
from test_watch import Script, WatchCase  # noqa: E402

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


class ClaudeCodeMemberCap(WatchCase):
    # Weave, after 26.11.16 (B-g8be33pedqt): a member's wait ended "nothing arrived in 2.5 min · arm again: dev watch
    # --wait --for me --timeout 9", three times. Claude Code moves a crew member's foreground command to the
    # background after 180 s, whatever Bash timeout it asks for (its result says "subagentCapApplied"), so the
    # 9-minute wait the member is told to run never held: members ran 2.5 minutes to stay in the foreground, and the
    # wait sent them back to the 9 minutes Claude Code cuts. A Codex member and the main session have no such cap.
    def wait(self, agent, me):
        self.clock.t = self.t0  # each wait starts at the same moment: no lane goes silent across the cases
        out, start = io.StringIO(), self.clock.t
        rc = watch.wait(agent, "s1", me, 9, out=out, clock=self.clock.time, changes=Script(self.clock))
        return rc, out.getvalue().strip().splitlines()[-1], self.clock.t - start

    def test_only_a_claude_code_members_wait_stops_before_claude_code_moves_it_to_the_background(self):
        self.run_with_member()
        session = state.read(pg.session_path("claude", "s1"))
        run = dict(pg.load_run("claude", "s1"), agent="codex", messages=[])
        state.update(pg.run_path("codex", "s1"), lambda d: d.update(run))
        state.update(pg.session_path("codex", "s1"), lambda d: d.update(dict(session, agent="codex")))
        with self.subTest(wait="the main session"):  # before the question, which would wake it
            rc, last, waited = self.wait("claude", "main")
            self.assertEqual((rc, waited), (3, 540), last)
            self.assertEqual(last, "watch: nothing arrived in 9 min · arm again: dev watch --wait")
        with mock.patch.dict(os.environ, {"DEVKIT_SESSION": "codex:s1"}):
            pg.crew_ask("May I widen the scope?", from_agent="a1", now=self.t0)
        pg.crew_ask("May I widen the scope?", from_agent="a1", now=self.t0)
        with self.subTest(wait="a Codex member"):
            rc, last, waited = self.wait("codex", "a1")
            self.assertEqual((rc, waited), (3, 540), last)
            self.assertEqual(last, "watch: nothing arrived in 9 min · arm again: dev watch --wait --for me --timeout 9")
        with self.subTest(wait="a Claude Code member"):
            rc, last, waited = self.wait("claude", "a1")
            self.assertEqual(rc, 3, last)
            self.assertLess(waited, 180)  # it ends before Claude Code moves it to the background
            self.assertGreaterEqual(waited, 120)
            self.assertEqual(last, "watch: no answer yet after 2.5 min: Claude Code moves a crew member's command to "
                                   "the background after 3 min, so a member's wait stops before that, not at the 9 "
                                   "min asked · still blocked? arm again: dev watch --wait --for me --timeout 9")
            waits = [w for w in watch.records(self.clock.t) if (w.get("agent"), w.get("consumer")) == ("claude", "a1")]
            self.assertEqual([w["expires"] - w["started"] for w in waits], [150])  # what `dev watches` shows


class SameWaitTwoMembers(WatchCase):
    # Weave (a Codex crew): two members waited for their answers with the very same `dev watch --wait --for me
    # --timeout 9`, and no lane stamp reached them, so the call ledger held both calls and devkit gave the wait to the
    # more restricted member. The other member's wait said none of its questions waited (and could take the first
    # member's answers), while its `dev watch --json`, a command only it had typed, listed its question
    def test_a_wait_two_members_typed_alike_is_never_given_to_one_of_them(self):
        self.run_with_member()
        launch("a2", "devkit:neo", "Neo: tests", self.t0)
        cmd = "dev watch --wait --for me --timeout 9"
        for aid in ("a1", "a2"):
            pg.call_opened("claude", {"session_id": "s1", "agent_id": aid, "tool_use_id": f"toolu_{aid}_w",
                                      "tool_input": {"command": cmd}})
        with mock.patch.dict(os.environ, {"DEVKIT_FROM_AGENT": "", "DEVKIT_SPEAKER": ""}), \
                mock.patch.object(sys, "argv", cmd.split()):
            with self.assertRaises(ValueError) as e:
                identity.consumer(True)
        self.assertIn("several crew members", str(e.exception))


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
