# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""Tests for the dashboard (decisions in Python, safe rendering). Run: python3 -m unittest discover -s tests -v"""
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
NOW = time.time()
TODAY = time.strftime("%Y-%m-%d", time.localtime(NOW))
YESTERDAY = time.strftime("%Y-%m-%d", time.localtime(NOW - 86400))
PAST = time.strftime("%Y-%m-%d", time.localtime(NOW - 5 * 86400))
FUTURE = time.strftime("%Y-%m-%d", time.localtime(NOW + 20 * 86400))
ISO = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 3600))


def pr(n, checks="passing", review="", author="me", mergeable="MERGEABLE", branch=None):
    return {"number": n, "title": f"PR {n} {EVIL}", "branch": branch or f"feat/x-{n}", "draft": False,
            "url": f"https://github.com/acme/widget/pull/{n}", "updated": ISO, "checks": checks, "review": review,
            "author": author, "mergeable": mergeable, "size": 10}


def registry():
    widget = {
        "id": "acme/widget", "owner": "acme", "name": "widget", "path": "/Users/t/Development/acme/widget", "local": True,
        "remote": "https://github.com/acme/widget.git", "github": "acme/widget", "default_branch": "main", "canonical_ok": True,
        "stack": ["typescript"], "commands": ["npm run <test|build>"], "initiatives": ["lending"], "progress": None, "progress_source": "tasks",
        "main": {"branch": "main", "modified": 2, "untracked": 0, "unpushed": 0, "last_activity": int(NOW - 4 * 86400), "active": False},
        "worktrees": [{"path": "/Users/t/Development/.worktrees/acme/widget/fix-y", "branch": "fix/y", "location": "canonical", "active": False,
                       "modified": 0, "untracked": 0, "unpushed": 0, "pr": {"number": 5, "state": "MERGED"}, "last_activity": int(NOW - 3 * 86400), "size_kb": 4096},
                      {"path": "/Users/t/Development/.worktrees/acme/widget/feat-z", "branch": "feat/z", "location": "canonical", "active": False,
                       "modified": 0, "untracked": 0, "unpushed": 0, "pr": None, "last_activity": int(NOW - 3600), "size_kb": 10}],
        "prs_open": [pr(22, "failing"), pr(17, "passing", mergeable="CONFLICTING", branch="feat/z"), pr(12, "passing", "CHANGES_REQUESTED"),
                     pr(30, "passing", "REVIEW_REQUIRED"), pr(31, "failing", author="someone-else"), pr(32, "passing", "REVIEW_REQUIRED", author="teammate")],
        "prs_known": True, "size_kb": 1000, "last_activity": int(NOW - 600), "note": "projects/acme/widget.md",
        "note_summary": {"status": "blocked", "priority": "high", "milestone": "MVP", "due": PAST, "tags": ["payments"],
                         "purpose": "Widget " + EVIL, "state": "- Waiting on keys " + EVIL, "decisions": "", "links": "",
                         "tasks": {"now": ["Ship export " + EVIL], "next": ["Docs due:" + PAST], "later": ["Cache"], "done_recent": ["CI set up ✓ " + TODAY],
                                   "open": 3, "done": 1, "overdue": ["Docs due:" + PAST]},
                         "sessions": [time.strftime("%Y-%m-%d %H:%M", time.localtime(NOW - 1800)) + " · claude · `main` · 1 commit(s): feat: x"]},
        "activity": {TODAY: 3, YESTERDAY: 2}, "status_line": {"ts": int(NOW - 300), "agent": "claude", "line": "DevKit · widget · export · tests ✓"},
        "plans": [{"title": "Export plan", "path": "plans/acme/widget/export.md", "mtime": int(NOW - 100)}]}
    remote_only = {"id": "acme/api", "owner": "acme", "name": "api", "path": None, "local": False, "remote": "", "github": "acme/api",
                   "canonical_ok": True, "stack": [], "main": {}, "worktrees": [], "prs_open": [], "prs_known": False, "initiatives": ["lending"],
                   "note": "projects/acme/api.md", "last_activity": int(NOW - 9 * 86400), "progress": 40, "progress_source": "manual",
                   "note_summary": {"status": "active", "priority": "normal", "milestone": "", "due": "", "tags": [], "purpose": "", "state": "",
                                    "decisions": "", "links": "", "tasks": {"now": [], "next": [], "later": [], "done_recent": [], "open": 0, "done": 0, "overdue": []}, "sessions": []},
                   "activity": {}, "status_line": None, "plans": []}
    lending = {"id": "lending", "title": "LendingOS " + EVIL, "status": "blocked", "priority": "high", "milestone": "Pilot", "due": PAST,
               "progress": 60, "progress_source": "tasks", "projects": ["acme/widget", "acme/api"],
               "members": [{"id": "acme/widget", "status": "blocked", "local": True, "progress": 25, "now": ["Ship export"], "status_line": None, "workstream": "core"},
                           {"id": "acme/api", "status": "active", "local": False, "progress": 40, "now": [], "status_line": None, "workstream": ""}],
               "goal": "One platform " + EVIL, "scope": "", "state": "- Waiting for the bank", "risks": "", "decisions": "", "links": "",
               "tasks": {"now": ["Align migration"], "next": ["Security review due:" + PAST], "later": [], "done_recent": [], "open": 2, "done": 3,
                         "overdue": ["Security review due:" + PAST]},
               "sessions": [], "tags": [], "owner": "me", "source": "From the archive " + EVIL, "note": "initiatives/lending/README.md",
               "last_activity": int(NOW - 3000), "docs": ["initiatives/lending/docs/arch.md"]}
    return {"host": "testmac", "dev_root": "/Users/t/Development", "generated": int(NOW - 60), "github_user": "me",
            "projects": [widget, remote_only], "initiatives": [lending], "issues": [["layout", "acme/widget lives elsewhere"]],
            "sessions": [{"agent": "codex", "project": "acme/widget", "branch": "fix/a", "started": int(NOW - 900), "last_seen": int(NOW - 30),
                          "status": "DevKit · widget@fix/a · reproduced " + EVIL, "path": "/x", "active": True}],
            "providers": {"claude": {"installed": True, "name": "Claude Code", "logged_in": True, "plan": "max", "quota": {"available": None}},
                          "codex": {"installed": True, "name": "Codex", "logged_in": False, "quota": {"available": False, "used_percent": 100, "resets_at": int(NOW + 86400)}}},
            "drift": [{"tool": "claude", "key": "permissions.defaultMode", "current": "default", "desired": "bypassPermissions"}]}


def make_handoff(root: Path):
    for d in ("projects/acme", "plans/acme/widget", "log", "initiatives/lending/docs"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "PROJECTS.md").write_text("# Projects map\n\n| a | b |\n|---|---|\n| x | y |\n")
    (root / "projects/acme/widget.md").write_text("---\nstatus: blocked\n---\n# acme/widget\n\n" + EVIL + "\n" + BREAKOUT + "\n")
    (root / "plans/acme/widget/export.md").write_text("# Export plan\n\n- [ ] step one\n")
    (root / "log/2026-11.md").write_text("# Activity log\n\n- 2026-11-05 10:00 · gc · removed worktree x\n")
    (root / "initiatives/lending/README.md").write_text("# LendingOS\n\n## Goal\n" + EVIL + "\n")
    (root / "initiatives/lending/source.md").write_text("# Source\n\n" + BREAKOUT + "\n")
    (root / "initiatives/lending/docs/arch.md").write_text("# Architecture\n")


def embedded(html: str) -> dict:
    m = re.search(r'<script id="devkit-data" type="application/json">(.*?)</script>', html, re.S)
    assert m, "data script missing"
    return json.loads(m.group(1))


class TempState:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = core.STATE
        core.STATE = Path(self.tmp.name)
        return core.STATE

    def __exit__(self, *exc):
        core.STATE = self.old
        self.tmp.cleanup()


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.handoff = Path(cls.tmp.name) / "handoff"
        make_handoff(cls.handoff)
        cls.reg = registry()
        cls.html = dashboard.render(cls.reg, handoff=cls.handoff, now=NOW)
        cls.data = embedded(cls.html)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_self_contained_with_brand(self):
        self.assertIsNone(re.search(r"<script[^>]+\bsrc=", self.html), "no external scripts")
        links = re.findall(r'<link[^>]+href="(https?://[^"]+)"', self.html)
        self.assertTrue(all(re.match(r"https://fonts\.(googleapis|gstatic)\.com", u) for u in links), links)
        self.assertIn('class="ff ff-dark"', self.html)
        self.assertIn('class="ff ff-light"', self.html)
        self.assertIn(core.COPYRIGHT.replace("&", "&amp;"), self.html)

    def test_everything_is_escaped(self):
        self.assertNotIn(EVIL, self.html)
        self.assertNotIn(BREAKOUT, self.html)
        self.assertEqual(self.html.count("</script>"), 3, "only the theme, data and app scripts close")
        texts = {f["path"]: f["text"] for f in self.data["files"]}
        self.assertIn(EVIL, texts["projects/acme/widget.md"])          # data round-trips intact; rendered with textContent
        self.assertIn(BREAKOUT, texts["initiatives/lending/source.md"])

    def test_payload(self):
        d = self.data
        self.assertEqual([p["id"] for p in d["projects"]], ["acme/widget", "acme/api"])
        self.assertEqual(d["initiatives"][0]["id"], "lending")
        self.assertEqual(d["initiatives"][0]["task_index"], {"Align migration": 1, "Security review due:" + PAST: 2})
        self.assertEqual(d["projects"][0]["task_index"]["Ship export " + EVIL], 1)
        self.assertIs(d["projects"][1]["local"], False)
        self.assertEqual(d["projects"][1]["progress_source"], "manual")
        paths = {f["path"] for f in d["files"]}
        self.assertTrue({"initiatives/lending/README.md", "initiatives/lending/source.md", "initiatives/lending/docs/arch.md",
                         "projects/acme/widget.md", "plans/acme/widget/export.md", "log/2026-11.md", "PROJECTS.md"} <= paths)
        self.assertEqual(d["providers"]["codex"]["logged_in"], False)
        self.assertEqual(len(d["sessions"]), 1)

    def test_app_features(self):
        js, css = dashboard.JS, dashboard.CSS
        for needle in ("function openPalette", "function encode(", "function decode(", "hashchange", "localStorage",
                       "function viewHome", "function viewPortfolio", "function initiativeDrawer", "function viewProjects",
                       "function viewTasks", "function viewActivity", "function viewExplorer", "function viewHealth",
                       "function viewSearch", "function roadmap", "function progOf", "function highlight"):
            self.assertIn(needle, js, needle)
        for needle in ("prefers-reduced-motion", "@media print", "--logo-dark", "max-width:768px"):
            self.assertIn(needle, css, needle)
        self.assertNotIn("innerHTML", js)
        self.assertNotIn("insertAdjacentHTML", js)
        self.assertNotIn("document.write", js)

    def test_explorer_cap(self):
        files, meta = dashboard.explorer_files(self.handoff, cap=150)
        self.assertTrue(meta["capped"])
        self.assertLessEqual(sum(f["size"] for f in files), 150)

    def test_write_is_atomic(self):
        with TempState() as st:
            out = dashboard.write(self.reg, handoff=self.handoff)
            self.assertEqual(out, st / "dashboard" / "index.html")
            self.assertTrue(out.read_text().startswith("<!doctype html>"))
            self.assertFalse(out.with_suffix(".html.tmp").exists())

    def test_empty_registry(self):
        html = dashboard.render({"projects": []}, handoff=Path(self.tmp.name) / "missing", now=NOW)
        d = embedded(html)
        self.assertEqual((d["projects"], d["initiatives"], d["actions"]), ([], [], []))


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.reg = registry()
        self.acts = dashboard.build_actions(self.reg, NOW)
        self.kinds = [a["kind"] for a in self.acts]

    def test_priority_order(self):
        order = ["ci-failing", "changes-requested", "conflicts", "review-waiting", "review-requested", "blocked", "overdue",
                 "idle-work", "reclaim", "drift", "sign-in"]
        first = [self.kinds.index(k) for k in order]
        self.assertEqual(first, sorted(first), self.kinds)

    def test_ci_failing_only_my_prs_with_exact_command(self):
        ci = [a for a in self.acts if a["kind"] == "ci-failing"]
        self.assertEqual([a["title"] for a in ci], ["PR #22 is failing CI"])
        self.assertEqual(ci[0]["cmd"], "gh pr checks 22 -R acme/widget --watch")
        self.assertEqual(ci[0]["url"], "https://github.com/acme/widget/pull/22")

    def test_conflicts_use_existing_worktree(self):
        c = next(a for a in self.acts if a["kind"] == "conflicts")
        self.assertEqual(c["cmd"], "cd /Users/t/Development/.worktrees/acme/widget/feat-z && git fetch origin && git rebase origin/main")

    def test_reviews(self):
        self.assertIn("gh pr view 12 -R acme/widget --comments", [a["cmd"] for a in self.acts])
        self.assertIn("Review PR #32 by @teammate", [a["title"] for a in self.acts])

    def test_project_and_initiative_items(self):
        cmds = [a["cmd"] for a in self.acts]
        self.assertIn("dev handoff acme/widget", cmds)                    # blocked project
        self.assertIn("dev task done 2 -p acme/widget", cmds)             # overdue project task (Now=1, Next=2)
        self.assertIn("dev initiative show lending", cmds)                # blocked / overdue initiative
        self.assertIn("dev task done 2 -i lending", cmds)                 # overdue initiative task
        self.assertIn("cd /Users/t/Development/acme/widget && git status -sb", cmds)   # idle uncommitted work
        self.assertIn("dev wt rm fix/y", cmds)                            # merged worktree
        self.assertIn("dev config diff", cmds)
        self.assertIn("codex login", cmds)
        self.assertNotIn("dev adopt", " ".join(cmds))                     # canonical, and the remote-only project is skipped

    def test_health(self):
        h = dashboard.project_health(self.reg["projects"][0], NOW)
        self.assertEqual(h["level"], "risk")
        joined = " ".join(h["reasons"])
        for frag in ("PR #22 is failing CI", "PR #12 has changes requested", "PR #17 has merge conflicts", "blocked", "overdue task",
                     "uncommitted", "past its due date"):
            self.assertIn(frag, joined)
        self.assertEqual(dashboard.project_health(self.reg["projects"][1], NOW)["level"], "ok")

    def test_since_yesterday(self):
        with TempState() as st:
            (st / "prs").mkdir()
            (st / "prs" / "acme__widget.json").write_text(json.dumps([
                {"number": 9, "title": "Merged yesterday", "state": "MERGED", "updatedAt": ISO, "url": "u9"},
                {"number": 3, "title": "Merged long ago", "state": "MERGED", "updatedAt": "2020-01-01T00:00:00Z", "url": "u3"},
                {"number": 8, "title": "Still open", "state": "OPEN", "updatedAt": ISO, "url": "u8"}]))
            s = dashboard.since_yesterday(self.reg, dashboard.pr_cache(), NOW)
        self.assertEqual([m["number"] for m in s["merged"]], [9])
        self.assertEqual(s["commits"], [["acme/widget", 5]])
        self.assertEqual(s["done"], [{"project": "acme/widget", "text": "CI set up"}])
        self.assertTrue(any(x["agent"] == "codex" and x["live"] for x in s["sessions"]))

    def test_guard_rules(self):
        events = [{"ts": "2026-11-05 10:00", "agent": "claude", "decision": "deny", "reason": "Branch `claude/x` is prefixed"},
                  {"ts": "2026-11-05 11:00", "agent": "codex", "decision": "deny", "reason": "Branch `ai/y` is prefixed"},
                  {"ts": "2026-11-05 09:00", "agent": "claude", "decision": "ask", "reason": "Writes outside ~/Development"}]
        rules = dashboard.guard_rules(events)
        self.assertEqual((rules[0]["rule"], rules[0]["count"], rules[0]["agents"]), ("branch", 2, {"claude": 1, "codex": 1}))
        self.assertEqual(rules[0]["last"], "2026-11-05 11:00")
        self.assertEqual((rules[1]["rule"], rules[1]["ask"]), ("outside", 1))

    def test_task_index(self):
        ns = {"tasks": {"now": ["a"], "next": ["b", "c"], "later": ["d"]}}
        self.assertEqual([dashboard.task_index(ns, t) for t in "abcd"], [1, 2, 3, 4])
        self.assertIsNone(dashboard.task_index(ns, "zzz"))


if __name__ == "__main__":
    unittest.main()
