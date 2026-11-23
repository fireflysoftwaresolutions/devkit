# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# `dev config validate` names a key devkit does not read only when it is a short word no secret rule or pattern finds
# anything in, as `dev audit config` does: a token pasted as a key is said by its length and where it is. Its output
# lands in terminals, session logs and screenshots shared with the team.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_config_validate
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from devkit import audit, cli, core  # noqa: E402

GITHUB = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"  # a shape the built-in rules know
TEAM_WORD = "acmetok7f3k9x2m4q8zr1"  # a digit in it: not a short word
TEAM_ONLY = "zuluquebec"  # a short word only this Mac's pattern knows


class ConfigValidateTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "config.json"
        for p in (mock.patch.object(core, "CONFIG_FILE", self.path),
                  mock.patch.object(audit, "AUDIT", Path(folder.name) / "audit"),
                  mock.patch.object(audit, "policy_file", lambda: Path(folder.name) / "no-policy.json")):
            p.start()
            self.addCleanup(p.stop)

    def validate(self, doc: dict) -> tuple:
        self.path.write_text(json.dumps(doc))
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            rc = cli._config_validate()
        return rc, out.getvalue()

    def test_an_unknown_key_is_named_only_when_it_is_a_short_word(self):
        rc, out = self.validate({"progress": {GITHUB: 3, "max_paralel": 3}, TEAM_WORD: 1, "work.mode": "auto",
                                 "audit": {"redact": {"extra": [TEAM_ONLY]}}, "jobs": {GITHUB: {"every": "1h"}},
                                 "sync": {TEAM_ONLY: True}})
        self.assertEqual(rc, 1)
        for secret in (GITHUB, TEAM_WORD, TEAM_ONLY):
            self.assertNotIn(secret, out)
        for said in (f"unknown key of {len(GITHUB)} characters in progress: devkit does not read it",
                     f"unknown key of {len(TEAM_WORD)} characters: devkit does not read it",
                     f"unknown key of {len(TEAM_ONLY)} characters in sync",
                     f"a map key of {len(GITHUB)} characters in jobs is invalid",
                     "unknown key progress.max_paralel: devkit does not read it",
                     "unknown key work.mode: devkit does not read it"):  # a short word with a dot, at the top
            self.assertIn(said, out)


if __name__ == "__main__":
    unittest.main()
