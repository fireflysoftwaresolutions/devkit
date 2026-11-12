# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# A crew member's file write through a link (hooks._scope_violation). Its step scope holds a path only when the file
# the write reaches, links resolved, is in the scope too: a link inside the scope cannot lead out of the repository or
# into files of the repository that another lane owns. A path that does not exist yet (a new file) is judged by its
# nearest existing folder. The repository and the folder outside it are built under the run's temporary home, which
# the guard does not count as scratch (tests/_hermetic.py, home_base): a write into a temp folder outside the
# repository is a scratch file any member may write, so a target there would prove nothing.
# Run: python3 -m unittest tests.test_scope_links_write -v
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()  # run alone, this module gets the suite's temporary home too (tests/_hermetic.py)
sys.path.insert(0, str(ROOT / "lib"))

from devkit import core, guard, hooks, progress  # noqa: E402


def _fail_open(rule, e):
    raise AssertionError(f"the {rule} check failed open, which lets every write through: {e!r}")


class ScopeLinksWriteTests(unittest.TestCase):
    """A write lane of the Backend engineer scoped to docs/**, in a repository whose docs/ folder holds links: out of
    the repository (to a LaunchAgents folder, the place a write would run code at the next login), and into lib/."""

    def setUp(self):
        sandbox = Path(os.path.realpath(tempfile.mkdtemp(dir=core.HOME)))
        self.addCleanup(shutil.rmtree, sandbox, True)
        self.repo, self.away = sandbox / "repo", sandbox / "Library" / "LaunchAgents"
        for d in (self.repo / "docs", self.repo / "lib", self.away):
            d.mkdir(parents=True)
        self.assertFalse(core.is_temp(self.away), "the target outside the repository must not be a scratch folder")
        (self.repo / "lib" / "core.py").write_text("x = 1\n")
        (self.repo / "docs" / "a.md").write_text("# A\n")
        (self.away / "agent.plist").write_text("<plist/>\n")
        links = {"out": self.away, "agent.plist": self.away / "agent.plist", "later.plist": self.away / "later.plist",
                 "inlink.py": "../lib/core.py", "libdir": "../lib", "alias.md": "a.md"}
        for name, to in links.items():
            os.symlink(to, self.repo / "docs" / name)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        run = {"agents": {"t1": {"persona": "Tank"}}}
        for name, value in (("lane_of", lambda *a: (run, run["agents"]["t1"])), ("lane_chain", lambda r, a: [a]),
                            ("lane_tools", lambda r, a: "write"), ("lane_scope", lambda r, a: [["docs/**"]])):
            p = mock.patch.object(progress, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(guard, "failopen", _fail_open)
        p.start()
        self.addCleanup(p.stop)

    def write(self, rel):
        return hooks._scope_violation("claude", {
            "agent_id": "t1", "tool_name": "Write", "session_id": "s", "cwd": str(self.repo),
            "tool_input": {"file_path": f"{self.repo}/{rel}", "content": "x\n"}})

    def refused(self, rel, lands):
        why = self.write(rel)
        self.assertIsNotNone(why, f"{rel} leads to {lands}, outside docs/**, yet the write was allowed")
        self.assertIn("outside", why)
        self.assertIn(lands, why)  # the refusal names where the write would land

    def test_a_link_out_of_the_repository_holds_nothing(self):
        for rel, lands in (("docs/out/agent.plist", "Library/LaunchAgents/agent.plist"),
                           ("docs/agent.plist", "Library/LaunchAgents/agent.plist"),
                           ("docs/out/../evil.md", "Library/evil.md")):  # `..` after a link climbs from its target
            with self.subTest(rel=rel):
                self.refused(rel, lands)

    def test_a_link_into_files_of_another_lane_holds_nothing(self):
        for rel in ("docs/inlink.py", "docs/libdir/core.py"):
            with self.subTest(rel=rel):
                self.refused(rel, "lib/core.py")
        self.assertIsNone(self.write("docs/alias.md"))  # a link that stays in the scope still holds

    def test_a_new_file_is_judged_by_the_folder_it_lands_in(self):
        self.assertIsNone(self.write("docs/new.md"))
        self.refused("docs/out/evil.plist", "Library/LaunchAgents/evil.plist")
        self.refused("docs/later.plist", "Library/LaunchAgents/later.plist")  # a link to a file not there yet

    def test_a_new_file_in_a_new_subfolder_is_judged_by_its_nearest_existing_folder(self):
        self.assertIsNone(self.write("docs/guides/how-to/new.md"))
        self.refused("docs/out/new/evil.plist", "Library/LaunchAgents/new/evil.plist")
        self.refused("docs/libdir/new/x.py", "lib/new/x.py")


if __name__ == "__main__":
    unittest.main()
