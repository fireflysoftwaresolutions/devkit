# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# A step's --scope is saved in the run and matched against file paths, so `dev crew delegate` and `dev progress
# assign` read it by one rule: paths or globs in the repository only. A secret, a URL or remote, a ".." segment, a
# path outside every checkout and an invisible character are refused, quoting none of the text, and nothing is saved.
# Progress state in a temporary folder (test_progress.TempState).
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_dispatch_scope -v
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import dispatch  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_progress import TempState, repo_with_worktrees  # noqa: E402

T0 = 1_800_000_000.0
SECRET = "ghp_" + "a1B2" * 10  # the shape of a GitHub token


class AssignScopeTests(unittest.TestCase):
    def test_assign_refuses_a_scope_with_a_secret_and_saves_nothing(self):
        # assign saved the scope text unchecked: a token in it went into the run
        with TempState():
            pg.start("Feature", plan=["parser=30m"], now=T0)
            with self.assertRaises(pg.StepError) as e:
                pg.assign("parser", "Neo", scope=f"lib/**,https://u:{SECRET}@h/x", now=T0 + 60)
            self.assertNotIn(SECRET, str(e.exception))
            self.assertIn("secret", str(e.exception))
            self.assertNotIn(SECRET, json.dumps(pg.load_run("claude", "s1")))

    def test_assign_refuses_a_scope_that_is_not_a_repository_path_as_delegate_does(self):
        # a URL, a remote, a ".." segment and a folder outside every checkout of the repository: assign kept them
        with TempState(), tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as scratch:
            main, wt = repo_with_worktrees(d)
            pg.start("Feature", plan=["parser=30m"], now=T0)
            for scope in ("https://example.com/x", "git@host:org/repo", "../other/**", f"{wt}/lib/../tests/**",
                          f"{scratch}/notes/**"):
                with self.subTest(scope=scope):
                    with self.assertRaises(pg.StepError) as by_assign:
                        pg.assign("parser", "Neo", scope=scope, cwd=str(main), now=T0 + 60)
                    with self.assertRaises(pg.StepError) as by_delegate:
                        dispatch.delegate("add the export column", to="Ghost", scope=scope, cwd=str(main),
                                          now=T0 + 60)
                    self.assertIn("repository-relative", str(by_assign.exception))
                    self.assertEqual(str(by_assign.exception), str(by_delegate.exception))
            self.assertEqual([(s["name"], s.get("scope")) for s in pg.load_run("claude", "s1")["plan"]],
                             [("parser", None)])


if __name__ == "__main__":
    unittest.main()
