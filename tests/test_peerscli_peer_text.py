# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""What `dev session` reads show of other sessions' words (run goals and headlines, claim reasons, escalation
questions, messages): the tech lead's JSON carries the data frame with them (peers.FRAME_HEADER, as the text output
prints it), and a crew member's read shows other sessions' metadata only (session, project, branch, state, times),
never their words. devkit's state and the session records are temporary (test_peers.PeerCase); another session's run
is a stand-in. Run: python3 -m unittest tests.test_peerscli_peer_text -v"""
import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from devkit import cli, identity, peers  # noqa: E402
from devkit import progress as pg  # noqa: E402

from test_peers import CLAUDE_A, CODEX_A, sender  # noqa: E402
from test_peerscli import WHO_VARS, CliCase  # noqa: E402

REL = "release:acme/api"
GOAL = "Ship the payroll export for Northwind"
WHY = "cutting the payroll release"
QUESTION = "Which payroll project releases first?"
MEMBER = "a1b2c3d4e5f6a7b8c"  # a crew member's lane id in Claude A's session


class PeerTextCase(CliCase):
    def setUp(self):
        super().setUp()
        self.me, self.codex, _ = self.sessions()
        real = pg.load_run
        runs = mock.patch.object(pg, "load_run", lambda agent, sid, *a, **k: {
            "goal": GOAL, "state": "running", "agents": {}} if sid == CODEX_A else real(agent, sid, *a, **k))
        runs.start()
        self.addCleanup(runs.stop)
        peers.claim(sender(CODEX_A, "codex"), REL, WHY, now=self.now)
        asked = json.loads(self.dev("ask", "@0199a213", "Who goes first?", "--json")[1])["id"]
        self.assertEqual(self.dev("escalate", asked, QUESTION, "--options", "A: payroll first; B: billing first")[0], 0)

    def read(self, *argv, lane="main"):
        rc, out, err = self.dev(*argv, lane=lane)
        self.assertEqual(rc, 0, err)
        return out


class FrameTests(PeerTextCase):
    def test_the_tech_leads_json_reads_carry_the_frame_with_other_sessions_words(self):
        rows = {r["key"]: r for r in json.loads(self.read("ls", "--json"))}
        self.assertEqual((rows[self.codex]["run"]["goal"], rows[self.codex].get("frame")), (GOAL, peers.FRAME_HEADER))
        self.assertNotIn("frame", rows[self.me])
        claims = json.loads(self.read("claims", "--json"))
        self.assertEqual([(c["why"], c.get("frame")) for c in claims], [(WHY, peers.FRAME_HEADER)])
        escalations = json.loads(self.read("escalations", "--json"))
        self.assertEqual([(e["question"], e.get("frame")) for e in escalations], [(QUESTION, peers.FRAME_HEADER)])


class MemberTests(PeerTextCase):
    def test_a_crew_members_reads_show_other_sessions_metadata_and_never_their_words(self):
        for argv in (("ls",), ("ls", "--json"), ("claims",), ("claims", "--json"), ("escalations",),
                     ("escalations", "--json")):
            out = self.read(*argv, lane=MEMBER)
            for words in (GOAL, WHY, QUESTION, "payroll first"):
                self.assertNotIn(words, out, argv)
        rows = {r["key"]: r for r in json.loads(self.read("ls", "--json", lane=MEMBER))}
        self.assertEqual({k: rows[self.codex][k] for k in ("project", "branch", "state")},
                         {"project": "acme/api", "branch": "main", "state": rows[self.codex]["state"]})
        self.assertEqual([c["resource"] for c in json.loads(self.read("claims", "--json", lane=MEMBER))], [REL])
        self.assertEqual([e["state"] for e in json.loads(self.read("escalations", "--json", lane=MEMBER))], ["open"])


READS = (("ls",), ("ls", "--json"), ("claims",), ("claims", "--json"), ("escalations",), ("escalations", "--json"))


# A read whose caller devkit cannot name, or whose lookup fails, shows only what a crew member's read shows: only the
# tech lead and the developer's own terminal see other sessions' words.
class UnsureReaderTests(PeerTextCase):
    def unstamped(self, *argv):
        # Claude A's shell runs the read with no stamp (the hook could not read the command, as with
        # `d=dev; $d session ls`), and the call ledger holds no call of it
        env = {"DEVKIT_SESSION": f"claude:{CLAUDE_A}", "DEVKIT_AGENT": "claude"}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            for k in set(WHO_VARS) - set(env):
                os.environ.pop(k, None)
            rc = cli.main(["session", *argv])
        self.assertEqual(rc, 0, err.getvalue())
        return out.getvalue()

    def assertNoWords(self, out, argv):
        for words in (GOAL, WHY, QUESTION, "payroll first"):
            self.assertNotIn(words, out, argv)

    def test_a_read_whose_caller_devkit_cannot_name_shows_no_other_sessions_words(self):
        # a crew member's launch waits in this session, so the unstamped call may be that member's
        pg.note_agent_launch("claude", {"session_id": CLAUDE_A, "tool_use_id": "launch-1", "tool_name": "Agent",
                                        "tool_input": {"description": "Mouse: tests",
                                                       "subagent_type": "devkit:mouse"}})
        for argv in READS:
            self.assertNoWords(self.unstamped(*argv), argv)
        rows = {r["key"]: r for r in json.loads(self.unstamped("ls", "--json"))}
        self.assertEqual((rows[self.codex]["project"], rows[self.codex]["branch"]), ("acme/api", "main"))
        # the developer's own terminal is no crew member, and reads them
        rc, out, err = self.dev("claims", who=None)
        self.assertEqual((rc, WHY in out), (0, True), err)

    def test_a_read_whose_speaker_lookup_fails_shows_no_other_sessions_words(self):
        with mock.patch.object(identity, "speaker", side_effect=OSError("the call ledger cannot be read")):
            for argv in READS:
                self.assertNoWords(self.read(*argv), argv)
            self.assertEqual([c["resource"] for c in json.loads(self.read("claims", "--json"))], [REL])


if __name__ == "__main__":
    unittest.main()
