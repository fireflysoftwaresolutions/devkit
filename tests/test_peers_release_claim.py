# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev release` holds release:<project> for the session that runs it. A session that started outside any project
(its record names none, as one started in the home folder) claims the release of the checkout the command runs in;
a session whose record names another project still claims nothing there. devkit's state, the session records and
the repositories are temporary (test_peers.PeerCase); the speaker is a stand-in. Run:
python3 -m unittest tests.test_peers_release_claim -v"""
import argparse
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from devkit import gitx, identity, peers  # noqa: E402

from test_peers import CLAUDE_A, CLAUDE_B, PeerCase  # noqa: E402

REL = "release:acme/api"


class ReleaseClaimTests(PeerCase):
    def release_claim(self, sid):
        sp = identity.Speaker("claude", sid, "main", "stamped", True)
        checkout = types.SimpleNamespace(pid="acme/api", top=str(self.repo))
        with mock.patch.object(identity, "speaker", return_value=sp), mock.patch.object(gitx, "info"):
            return gitx._release_claim(checkout, argparse.Namespace(dry_run=False))

    def test_only_a_session_with_no_project_of_its_own_claims_the_release_of_its_checkout(self):
        home = self.root / "home"
        home.mkdir()
        self.session("claude", CLAUDE_A, project="", cwd=home, branch="", pid=101)
        held = self.release_claim(CLAUDE_A)
        self.assertEqual(held and held[1], REL)
        self.assertEqual((peers.holder(REL) or {}).get("key"), f"claude:{CLAUDE_A}")
        peers.release(held[0], REL, why="released")
        web = self.git_repo("acme/web", "main")
        self.session("claude", CLAUDE_B, project="acme/web", cwd=web, pid=202)
        self.assertIsNone(self.release_claim(CLAUDE_B))
        self.assertIsNone(peers.holder(REL))


if __name__ == "__main__":
    unittest.main()
