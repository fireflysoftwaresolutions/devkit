# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""A live session renews its release claim at most until it has held it for 4 x CLAIM_MAX_MIN minutes (16 h): a
renewal keeps the claim's start, so without a total cap one session could keep release:<project> forever. devkit's
state and the session records are temporary (test_peers.PeerCase). Run:
python3 -m unittest tests.test_peers_claim_cap -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from devkit import peers  # noqa: E402

from test_peers import CLAUDE_A, PeerCase, sender  # noqa: E402

REL = "release:acme/api"
CAP_S = 4 * peers.CLAIM_MAX_MIN * 60


class ReleaseClaimCapTests(PeerCase):
    def at(self, seconds_later: float):
        """The clock `seconds_later`, with the holder's session still live then."""
        self.now = self.start + seconds_later
        self.session("claude", CLAUDE_A, pid=101)
        return self.now

    def test_a_release_claim_renews_until_it_was_held_for_the_cap_then_the_renewal_is_refused(self):
        self.start = self.now
        peers.claim(sender(CLAUDE_A), REL, "cutting 26.11.24", minutes=peers.CLAIM_MAX_MIN, now=self.at(0))
        for later in (3 * 3600, 6 * 3600, 9 * 3600, 12 * 3600, CAP_S - 60):
            got = peers.claim(sender(CLAUDE_A), REL, "still cutting", minutes=peers.CLAIM_MAX_MIN, now=self.at(later))
            self.assertEqual((got["state"], got["since"]), ("renewed", self.start))
        with self.assertRaises(peers.PeerError) as e:
            peers.claim(sender(CLAUDE_A), REL, "still cutting", minutes=peers.CLAIM_MAX_MIN, now=self.at(CAP_S))
        self.assertEqual(e.exception.code, 2)
        self.assertIn(f"dev session release {REL}", str(e.exception))
        self.assertEqual(peers.holder(REL, now=self.now)["until"], self.start + CAP_S - 60 + peers.CLAIM_MAX_MIN * 60)


if __name__ == "__main__":
    unittest.main()
