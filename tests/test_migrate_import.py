# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# `dev handoff-import` commits everything in the handoff repository with one `git add -A`: a team policy file this
# Mac left broken and not committed must not ride along, since the push that follows would give it to every Mac.
# The repositories live in a temporary folder (tests/test_work.py's TwoMacs); no network, no background sync.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_migrate_import
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / "lib"))
sys.path.insert(0, str(TESTS))

from devkit import gc, migrate, work  # noqa: E402
from test_work import TwoMacs, _git  # noqa: E402


class ImportCommitTests(unittest.TestCase):
    def test_an_import_never_commits_a_team_policy_no_mac_could_load(self):
        with TwoMacs() as m, tempfile.TemporaryDirectory() as staging, \
                mock.patch.object(migrate, "HANDOFF", m.here), \
                mock.patch.object(migrate, "NOTES", m.here / "projects"), \
                mock.patch.object(gc, "LOGDIR", m.here / "log"):
            good = json.dumps({"redact": {"extra": ["acmetok-[a-z0-9]{14}"]}})
            policy = m.here / "policy" / "audit.json"
            policy.parent.mkdir(parents=True)
            policy.write_text(good)
            _git(m.here, "add", "-A")
            _git(m.here, "commit", "-qm", "team policy")
            policy.write_text('{"redact": ')  # broken, not committed
            (m.here / "policy" / "progress.json").write_text("{")  # a new file, broken too
            Path(staging, "README.md").write_text("---\nkind: initiative\ntitle: Payments\n---\n# Payments\n")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(migrate.run(Path(staging), "payments"), 0)
            committed = _git(m.here, "show", "--name-only", "--format=", "HEAD").splitlines()
            self.assertIn("initiatives/payments/README.md", committed)
            self.assertEqual(_git(m.here, "show", "HEAD:policy/audit.json"), good)
            self.assertNotIn("policy/progress.json", _git(m.here, "ls-tree", "-r", "--name-only", "HEAD"))
            self.assertEqual(sorted(h["path"] for h in work.held_policies()),
                             ["policy/audit.json", "policy/progress.json"])
        # and when the held copy cannot be written, the broken files stay out of the commit, on this Mac
        with TwoMacs() as m, tempfile.TemporaryDirectory() as staging, \
                mock.patch.object(migrate, "HANDOFF", m.here), \
                mock.patch.object(migrate, "NOTES", m.here / "projects"), \
                mock.patch.object(gc, "LOGDIR", m.here / "log"):
            work.held_policy_dir().parent.mkdir(parents=True, exist_ok=True)
            work.held_policy_dir().write_text("a file where the folder goes")
            (m.here / "policy").mkdir()
            (m.here / "policy" / "audit.json").write_text('{"redact": ')
            Path(staging, "README.md").write_text("---\nkind: initiative\ntitle: Payments\n---\n# Payments\n")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(migrate.run(Path(staging), "payments"), 0)
            self.assertNotIn("policy/audit.json", _git(m.here, "ls-tree", "-r", "--name-only", "HEAD"))
            self.assertEqual((m.here / "policy" / "audit.json").read_text(), '{"redact": ')


if __name__ == "__main__":
    unittest.main()
