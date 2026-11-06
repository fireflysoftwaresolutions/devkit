# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""Tests for the dashboard renderer. Run: python3 -m unittest discover -s tests -v"""
import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core, dashboard  # noqa: E402

EVIL = "<img src=x onerror=alert(1)>"
BREAKOUT = "</script><script>alert('pwned')</script>"


def synthetic_registry(now):
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    return {
        "host": "testmac", "dev_root": "/Users/t/Development", "generated": int(now - 120),
        "issues": [["layout", "acme/widget lives at ~/Development/widget; canonical is ~/Development/acme/widget"]],
        "projects": [
            {"id": "acme/widget", "owner": "acme", "name": "widget", "path": "/Users/t/Development/acme/widget",
             "remote": "https://github.com/acme/widget.git", "github": "acme/widget", "default_branch": "main",
             "canonical": "/Users/t/Development/acme/widget", "canonical_ok": True, "stack": ["typescript", "angular"],
             "commands": ["npm run <test|build>"],
             "main": {"branch": "feat/x", "modified": 1, "untracked": 0, "ahead": 1, "behind": 0, "unpushed": 1,
                      "last_commit": "feat: x", "last_commit_ts": int(now - 3600), "last_activity": int(now - 600),
                      "active": True, "pr": {"number": 7, "state": "OPEN", "title": "x"}},
             "worktrees": [{"path": "/Users/t/Development/.worktrees/acme/widget/fix-y", "branch": "fix/y",
                            "location": "canonical", "active": False, "pr": {"number": 6, "state": "MERGED", "title": "y"},
                            "last_activity": int(now - 86400 * 2), "size_kb": 2048}],
             "prs_open": [{"number": 7, "title": "x " + EVIL, "branch": "feat/x", "draft": False}], "prs_known": True,
             "size_kb": 120000, "last_activity": int(now - 600), "note": "projects/acme/widget.md",
             "note_summary": {"status": "blocked", "priority": "high", "milestone": "v2", "due": "2026-12-01",
                              "tags": ["payments"], "purpose": "Widget **service** " + EVIL,
                              "state": "- Waiting on API keys " + EVIL, "decisions": "", "links": "",
                              "tasks": {"now": ["Ship the export " + EVIL], "next": ["Docs"], "later": [],
                                        "done_recent": ["Set up CI ✓ 2026-11-01"], "open": 2, "done": 1,
                                        "overdue": ["Ship the export due:2026-01-01"]},
                              "sessions": [time.strftime("%Y-%m-%d %H:%M", time.localtime(now - 300)) +
                                           " · claude · `feat/x` · 1 commit(s): feat: x"]},
             "activity": {today: 3}, "status_line": {"ts": int(now - 300), "agent": "claude",
                                                     "line": "DevKit · widget · export done · tests ✓", "branch": "feat/x"},
             "plans": [{"title": "Export plan", "path": "plans/acme/widget/export.md", "mtime": int(now - 100)}]},
            {"id": "acme/api", "owner": "acme", "name": "api", "path": "/Users/t/Development/acme/api", "remote": "",
             "github": None, "canonical_ok": True, "stack": ["java/maven"], "main": {"branch": "main"}, "worktrees": [],
             "prs_open": [], "prs_known": False, "size_kb": None, "last_activity": int(now - 86400 * 9),
             "note": "projects/acme/api.md", "note_summary": None, "activity": {}, "status_line": None, "plans": []},
            {"id": "_local/scratch", "owner": None, "name": "scratch", "path": "/Users/t/Development/_local/scratch",
             "git": False, "canonical_ok": True, "stack": [], "main": {}, "worktrees": [], "prs_open": [],
             "note": "projects/_local/scratch.md", "note_summary": None, "activity": {}, "last_activity": int(now - 100)},
        ],
    }


def make_handoff(root: Path):
    (root / "projects" / "acme").mkdir(parents=True)
    (root / "plans" / "acme" / "widget").mkdir(parents=True)
    (root / "log").mkdir()
    (root / "PROJECTS.md").write_text("# Projects map\n\n| a | b |\n|---|---|\n| x | y |\n")
    (root / "projects" / "acme" / "widget.md").write_text(
        "---\nstatus: blocked\n---\n# acme/widget\n\n## Current state\n" + EVIL + "\n" + BREAKOUT + "\n")
    (root / "plans" / "acme" / "widget" / "export.md").write_text("# Export plan\n\n- [ ] step one\n")
    (root / "log" / "2026-11.md").write_text("# Activity log\n\n- 2026-11-05 10:00 · gc · removed worktree x\n")


def embedded_data(html: str) -> dict:
    m = re.search(r'<script id="devkit-data" type="application/json">(.*?)</script>', html, re.S)
    assert m, "data script missing"
    return json.loads(m.group(1))


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.handoff = Path(cls.tmp.name) / "handoff"
        cls.handoff.mkdir()
        make_handoff(cls.handoff)
        cls.now = time.time()
        cls.reg = synthetic_registry(cls.now)
        cls.html = dashboard.render(cls.reg, handoff=cls.handoff, now=cls.now)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_contains_projects_copyright_version(self):
        for pid in ("acme/widget", "acme/api", "_local/scratch"):
            self.assertIn(pid, self.html)
        self.assertIn(core.COPYRIGHT.replace("&", "&amp;"), self.html)
        self.assertIn("devkit " + core.VERSION, self.html)

    def test_self_contained(self):
        self.assertIsNone(re.search(r"<script[^>]+\bsrc=", self.html), "no external scripts allowed")
        links = re.findall(r'<link[^>]+href="(https?://[^"]+)"', self.html)
        self.assertTrue(all(re.match(r"https://fonts\.(googleapis|gstatic)\.com", u) for u in links), links)

    def test_embedded_json_is_valid_and_complete(self):
        data = embedded_data(self.html)
        self.assertEqual(len(data["projects"]), 3)
        self.assertEqual(data["copyright"], core.COPYRIGHT)
        paths = {f["path"] for f in data["files"]}
        self.assertTrue({"PROJECTS.md", "projects/acme/widget.md", "plans/acme/widget/export.md", "log/2026-11.md"} <= paths)
        self.assertFalse(data["files_meta"]["capped"])

    def test_note_and_explorer_text_is_escaped(self):
        self.assertNotIn(EVIL, self.html)
        self.assertNotIn("<img", self.html.split("<script id=\"devkit-data\"")[1])
        self.assertNotIn(BREAKOUT, self.html)
        self.assertEqual(self.html.count("</script>"), 3, "only the theme, data and app scripts may close")
        data = embedded_data(self.html)
        widget = next(f for f in data["files"] if f["path"] == "projects/acme/widget.md")
        self.assertIn(EVIL, widget["text"])          # round-trips intact as data, rendered later with textContent
        self.assertIn(BREAKOUT, widget["text"])

    def test_safe_json(self):
        s = dashboard.safe_json({"a": "<b>&</b> "})
        self.assertNotIn("<", s)
        self.assertNotIn(">", s)
        self.assertNotIn("&", s)
        self.assertEqual(json.loads(s), {"a": "<b>&</b> "})

    def test_explorer_cap(self):
        files, meta = dashboard.explorer_files(self.handoff, cap=200)
        self.assertTrue(meta["capped"])
        self.assertGreater(meta["omitted"], 0)
        self.assertLessEqual(sum(f["size"] for f in files), 200)

    def test_derived_issues(self):
        kinds = {i["kind"] for i in dashboard.derived_issues(self.reg, self.now)}
        self.assertTrue({"layout", "blocked", "overdue", "reclaimable"} <= kinds, kinds)
        blocked = next(i for i in dashboard.derived_issues(self.reg, self.now) if i["kind"] == "blocked")
        self.assertEqual(blocked["project"], "acme/widget")
        self.assertNotIn("- ", blocked["text"].split("—", 1)[-1][:3])

    def test_app_features_present(self):
        js = dashboard.JS
        for needle in ("function openPalette", "function encode(", "function decode(", "hashchange",
                       "localStorage", "function viewExplorer", "function viewHealth", "function viewTasks",
                       "function viewSearch", "function highlight", "prefers-reduced-motion", "@media print"):
            self.assertIn(needle, js + dashboard.CSS, needle)
        self.assertNotIn("innerHTML", js, "all DOM is built with createElement/textContent")
        self.assertNotIn("insertAdjacentHTML", js)
        self.assertNotIn("document.write", js)

    def test_write_is_atomic_and_in_state(self):
        old = core.STATE
        try:
            core.STATE = Path(self.tmp.name) / "state"
            out = dashboard.write(self.reg, handoff=self.handoff)
            self.assertEqual(out, core.STATE / "dashboard" / "index.html")
            self.assertTrue(out.read_text().startswith("<!doctype html>"))
            self.assertFalse(out.with_suffix(".html.tmp").exists())
        finally:
            core.STATE = old

    def test_empty_registry_renders(self):
        html = dashboard.render({"projects": [], "issues": []}, handoff=Path(self.tmp.name) / "missing", now=self.now)
        self.assertEqual(embedded_data(html)["projects"], [])


if __name__ == "__main__":
    unittest.main()
