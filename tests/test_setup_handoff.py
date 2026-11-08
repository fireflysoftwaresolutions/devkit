# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev install` and the handoff repository it finds on this Mac."""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import audit, core, setup, work  # noqa: E402


def _git(root: Path, *args) -> str:
    rc, out, err = core.sh(["git", "-C", str(root), *args])
    if rc != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {err or out}")
    return out


class HandoffSetupTests(unittest.TestCase):
    # A rebase left stopped blocks every later sync of the handoff repository; `dev install` is what a person runs
    # when devkit seems broken, so it ends the rebase too.
    def test_dev_install_aborts_a_rebase_left_stopped_in_the_handoff_repository(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        root = base / ".handoff"
        _git(base, "init", "-q", "-b", "main", str(root))
        _git(root, "config", "user.name", "Ana Dev")
        _git(root, "config", "user.email", "ana@example.com")
        note = root / "notes.md"

        def commit(text):
            note.write_text(text)
            _git(root, "add", "notes.md")
            _git(root, "commit", "-q", "-m", text.strip())

        commit("start\n")
        _git(root, "switch", "-q", "-c", "other")
        commit("their words\n")
        _git(root, "switch", "-q", "main")
        commit("our words\n")
        self.assertNotEqual(core.git(root, "rebase", "other")[0], 0)  # stops on the conflict in notes.md
        self.assertTrue((root / ".git" / "rebase-merge").exists())
        out = io.StringIO()
        with mock.patch.object(setup, "HANDOFF", root), mock.patch.object(work, "HANDOFF", root), \
                mock.patch.object(core, "STATE", base / "state"), mock.patch.object(audit, "AUDIT", base / "audit"), \
                mock.patch.object(audit, "policy_file", lambda: base / "no-policy.json"), \
                mock.patch.object(core, "handoff_repo", lambda: "ana/handoff"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            setup.setup_handoff()
            recs = audit.query(days=1, action="handoff.rebase-aborted")
        self.assertFalse((root / ".git" / "rebase-merge").exists())
        self.assertEqual((_git(root, "branch", "--show-current"), note.read_text()), ("main", "our words\n"))
        self.assertIn("aborted a rebase of the handoff repository that was left stopped", out.getvalue())
        self.assertEqual([(r["outcome"], r["target"]) for r in recs], [("blocked", "notes.md")])


if __name__ == "__main__":
    unittest.main()
