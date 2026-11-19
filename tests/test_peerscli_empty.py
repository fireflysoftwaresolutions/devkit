# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev session escalations --all` with none says there is no escalation at all, not "no open escalation": --all
lists the settled ones too. devkit's state is temporary (test_peers.PeerCase). Run:
python3 -m unittest tests.test_peerscli_empty -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from test_peerscli import CliCase  # noqa: E402


class EmptyListTests(CliCase):
    def test_escalations_all_with_none_says_none_at_all(self):
        self.sessions()
        rc, out, err = self.dev("escalations", "--all")
        self.assertEqual((rc, out.strip()), (0, "devkit · no escalation this session is party to"), err)


if __name__ == "__main__":
    unittest.main()
