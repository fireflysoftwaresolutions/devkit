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

    # `dev update` runs `dev install`, so an older handoff repository gets the rules a newer devkit adds: the month's
    # activity log (log/) merges as a union like the session logs, and so do the rules themselves. Lines already in
    # the file stay as they are, in their order: two Macs that upgrade only ever add lines at the end.
    def test_dev_install_adds_the_missing_merge_rules_to_an_older_handoff_repository(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / ".handoff"
        _git(Path(tmp.name), "init", "-q", "-b", "main", str(root))
        older = ("*.pdf binary\n# devkit: safe merges between machines and developers\nsessions/** merge=union\n"
                 "presence/*.json merge=ours\nPROJECTS.md merge=ours\nTASKS.md merge=ours\nACTIVITY.md merge=ours\n"
                 "registry.json merge=ours\n")  # a person's rule, then what devkit 26.11.14 and older wrote
        (root / ".gitattributes").write_text(older)
        with mock.patch.object(setup, "HANDOFF", root):
            setup.handoff_merge_rules()
            once = (root / ".gitattributes").read_text()
            setup.handoff_merge_rules()
        merge = _git(root, "check-attr", "merge", "--", "log/2026-11.md", "sessions/acme/api/2026-11.md",
                     ".gitattributes", "PROJECTS.md", "a.pdf")
        self.assertEqual(merge.splitlines(), ["log/2026-11.md: merge: union",
                                              "sessions/acme/api/2026-11.md: merge: union",
                                              ".gitattributes: merge: union", "PROJECTS.md: merge: ours",
                                              "a.pdf: merge: unset"])
        self.assertTrue(once.startswith(older), once)
        self.assertEqual((root / ".gitattributes").read_text(), once)
        self.assertEqual(_git(root, "config", "merge.ours.driver"), "true")


if __name__ == "__main__":
    unittest.main()
