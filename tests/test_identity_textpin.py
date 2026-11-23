# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Caller identity without the stamp where the host checks the developer's permission rules (ADR-9): a plain dev call
# goes out as the agent wrote it, so an allow rule such as Bash(dev progress:*) matches it, and its dev command names
# its caller by kernel facts (how=text-pinned).
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_identity_textpin -v
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
sys.path.insert(0, str(ROOT / "lib"))

from devkit import identity  # noqa: E402
from devkit import progress as pg  # noqa: E402


class CallerRecordTests(unittest.TestCase):
    # progress.caller reports a Codex caller devkit cannot verify as `unverified`; a text-pinned one is bound by the
    # same kernel facts as a token, so it is the caller it names (the tech lead, or the member)
    def test_a_text_pinned_codex_caller_is_verified(self):
        for lane, how, want in (("main", "text-pinned", "main"), ("a_tank", "text-pinned", "a_tank"),
                                ("main", "ledger", "unverified")):
            with self.subTest(how=how, lane=lane):
                found = identity._Found(identity.Speaker("codex", "s1", lane, how, True), "codex", "env", ("t1",),
                                        lane, (lane,) if lane != "main" else ())
                with mock.patch.object(identity, "_find", lambda *a, **k: found):
                    self.assertEqual(pg.caller()["caller"], want)


if __name__ == "__main__":
    unittest.main()
