# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""Tests for the dashboard (decisions in Python, safe rendering). Run: python3 -m unittest discover -s tests -v"""
import json
import re
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core, dashboard, plans, work  # noqa: E402

EVIL = "<img src=x onerror=alert(1)>"
BREAKOUT = "</script><script>alert('pwned')</script>"
NOW = time.time()
TODAY = time.strftime("%Y-%m-%d", time.localtime(NOW))
YESTERDAY = time.strftime("%Y-%m-%d", time.localtime(NOW - 86400))
PAST = time.strftime("%Y-%m-%d", time.localtime(NOW - 5 * 86400))
FUTURE = time.strftime("%Y-%m-%d", time.localtime(NOW + 20 * 86400))
ISO = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 3600))
MARK = "[Firefly> DevKit]"           # the status-line marker agents write
FAKE_PAT = "ghp_" + "0123456789abcdefABCDEF0123456789abcd"   # built at runtime: no token-shaped literal in the file
LONG_MILESTONE = "Business-first journey delivery across the experience, domain and core layers (plan of 2026-10-31, revised after review)"
LONG_GOAL = "Deliver working banking and insurance operations through Experience, Domain and Core services, with verified artefacts and consumer adoption."
LEGACY = "DevKit ·"                  # the legacy marker, still accepted
SAVED = "widget · main ❯ Ship export ❯ export endpoint done ❯ ✓ 31 tests green ❯ ▸ 3/7 ❯ next → contract test"   # what devkit saves
ME = f"me@{socket.gethostname().split('.')[0].lower()}"


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


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
        "activity": {TODAY: 3, YESTERDAY: 2}, "status_line": {"ts": int(NOW - 300), "agent": "claude", "line": SAVED},
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
                          "status": "widget · fix/a ❯ - ❯ reproduced " + EVIL + " ❯ ⚠ e2e flaky ❯ next → fix", "path": "/x", "active": True, "session": "s1",
                          "progress": {"goal": "Fix " + EVIL, "state": "running", "step": 2, "steps": 5, "estimate": 30, "started": int(NOW - 900),
                                       "eta_min": 20, "last": "reproduced", "next": "fix", "updated": int(NOW - 60)},
                          "subagents": 1, "turn_started": int(NOW - 120)}],
            "providers": {"claude": {"installed": True, "name": "Claude Code", "logged_in": True, "plan": "max", "quota": {"available": None}},
                          "codex": {"installed": True, "name": "Codex", "logged_in": False, "quota": {"available": False, "used_percent": 100, "resets_at": int(NOW + 86400)}}},
            "drift": [{"tool": "claude", "key": "permissions.defaultMode", "current": "default", "desired": "bypassPermissions"}]}


def item(root: Path, scope: str, iid: str, title: str, status="todo", bucket="next", holder="", expires=0.0, links=(), log=None, updated=None):
    meta = {"id": iid, "type": "task", "title": title, "scope": scope, "status": status, "bucket": bucket, "priority": "normal",
            "assignee": "", "claimed_by": holder, "claimed_at": iso(NOW - 7200) if holder else "", "lock_expires": iso(expires) if holder else "",
            "due": "", "links": list(links), "version": len(log or []) or 1, "created": iso(NOW - 86400 * 3), "created_by": "me@testmac",
            "updated": iso(updated or NOW - 7200), "updated_by": "me@testmac"}
    f = root / "items" / scope / f"{iid}.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(work.render({"meta": meta, "title": title, "body": "Body " + EVIL, "changelog": log or [f"v1 · {iso(NOW - 86400 * 3)} · me@testmac (claude) · create: task “{title}” in {bucket} — why: needed"]}))


def make_handoff(root: Path):
    for d in ("projects/acme", "plans/acme/widget", "log", "initiatives/lending/docs", "presence", "sessions/acme/widget", "plans/initiatives/lending"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "PROJECTS.md").write_text("# Projects map\n\n| a | b |\n|---|---|\n| x | y |\n")
    (root / "projects/acme/widget.md").write_text("---\nstatus: blocked\n---\n# acme/widget\n\n" + EVIL + "\n" + BREAKOUT + "\n")
    (root / "plans/acme/widget/export.md").write_text("# Export plan\n\n- [ ] step one\n")
    (root / "log/2026-11.md").write_text("# Activity log\n\n- 2026-11-05 10:00 · gc · removed worktree x\n")
    (root / "initiatives/lending/README.md").write_text("# LendingOS\n\n## Goal\n" + EVIL + "\n")
    (root / "initiatives/lending/source.md").write_text("# Source\n\n" + BREAKOUT + "\n")
    (root / "initiatives/lending/docs/arch.md").write_text("# Architecture\n")
    # work items: a valid lock, an expired lock, a blocked task of mine, initiative and linked items
    item(root, "acme/widget", "T-a1", "Ship export " + EVIL, "doing", "now", "me@testmac (claude:abc)", NOW + 4 * 3600, ["initiative:lending"],
         log=[f"v2 · {iso(NOW - 7200)} · me@testmac (claude) · claim: lock until {iso(NOW + 4 * 3600)}",
              f"v1 · {iso(NOW - 86400)} · me@testmac (claude) · create: task “Ship export” in now — why: the pilot needs it"])
    item(root, "acme/widget", "T-a2", "Reconcile", "doing", "now", "bob@studio (codex:def)", NOW - 3600)
    item(root, "acme/widget", "T-a3", "Keys from the bank", "blocked", "now", f"{ME} (claude:x)", NOW + 3600)
    item(root, "acme/widget", "T-a4", "Old expired but done", "done", "later", "bob@studio (codex)", NOW - 9 * 3600, updated=NOW - 600)
    item(root, "acme/api", "T-b1", "Linked API task", "todo", "next", links=["initiative:lending"])
    item(root, "acme/api", "T-b2", "Unlinked API task", "todo", "later")
    item(root, "initiatives/lending", "T-i1", "Align migration", "todo", "now")
    pres = lambda user, host, updated, sessions, locks=(): {"user": user, "host": host, "updated": iso(updated), "sessions": sessions, "locks": list(locks)}  # noqa: E731
    s = lambda agent, branch, seen: {"agent": agent, "project": "acme/widget", "branch": branch, "started": int(NOW - 3000), "status": "", "last_seen": int(seen),  # noqa: E731
                                     "session": agent + branch, "progress": None, "subagents": 0, "turn_started": 0}
    (root / "presence/me@testmac.json").write_text(json.dumps(pres("me", "testmac", NOW - 120, [s("claude", "feat/z", NOW - 60)], [{"id": "T-a1", "title": "Ship export", "scope": "acme/widget", "until": iso(NOW + 4 * 3600)}])))
    (root / "presence/bob@studio.json").write_text(json.dumps(pres("bob", "studio", NOW - 300, [s("codex", "feat/z", NOW - 200)])))
    (root / "presence/carol@studio.json").write_text(json.dumps(pres("carol", "studio", NOW - 3 * 86400, [s("claude", "feat/z", NOW - 3 * 86400)])))
    t1, t2 = time.strftime("%Y-%m-%d %H:%M", time.localtime(NOW - 1800)), time.strftime("%Y-%m-%d %H:%M", time.localtime(NOW - 7200))
    (root / "sessions/acme/widget/2026-11.md").write_text(
        f"# Sessions · acme/widget\n\n- {t2} · bob@studio · codex · `feat/z` · status: {LEGACY} widget@feat/z · legacy marker <!-- s:1 -->\n"
        f"- {t1} · me@testmac · claude · `main` · 2 commit(s): feat: x · status: {SAVED} {EVIL} <!-- s:2 -->\n")
    body = "# Release train\n\n## Goal\nShip."
    (root / "plans/initiatives/lending/train.md").write_text(plans._render(
        {"id": "P-1", "title": "Release train", "scope": "initiatives/lending", "status": "approved", "version": 2, "body_sha": plans._sha(body),
         "updated": ISO, "updated_by": "me@testmac"}, body, [f"v2 · {ISO} · me@testmac (claude) · approved — why: agreed", f"v1 · {ISO} · me@testmac (claude) · created"]))


def embedded(html: str) -> dict:
    m = re.search(r'<script id="devkit-data" type="application/json">(.*?)</script>', html, re.S)
    assert m, "data script missing"
    return json.loads(m.group(1))


def js_strip_marker():
    """The page's own stripMarker regexes, applied with Python (same syntax), so the test checks what ships."""
    m = re.search(r'function stripMarker\(line\) \{ return String\(line \|\| ""\)\.replace\(/(.+?)/i, ""\)\.replace\(/(.+?)/gi, "\$1"\); \}', dashboard.JS)
    assert m, "stripMarker not found"
    lead, inner = re.compile(m.group(1), re.I), re.compile(m.group(2), re.I)
    return lambda s: inner.sub(r"\1", lead.sub("", s, count=1))


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
        self.assertIn(EVIL, {i["id"]: i for i in self.data["items"]}["T-a1"]["title"])

    def test_payload(self):
        d = self.data
        self.assertEqual(d["v"], 3)
        self.assertEqual([p["id"] for p in d["projects"]], ["acme/widget", "acme/api"])
        self.assertEqual(d["initiatives"][0]["id"], "lending")
        self.assertEqual(d["initiatives"][0]["task_index"], {"Align migration": 1, "Security review due:" + PAST: 2})
        self.assertEqual(sorted(d["initiatives"][0]["linked_items"]), ["T-a1", "T-b1", "T-i1"])
        self.assertEqual(d["projects"][0]["task_index"]["Ship export " + EVIL], 1)
        self.assertIs(d["projects"][1]["local"], False)
        self.assertEqual(d["projects"][1]["progress_source"], "manual")
        paths = {f["path"] for f in d["files"]}
        self.assertTrue({"initiatives/lending/README.md", "initiatives/lending/source.md", "initiatives/lending/docs/arch.md",
                         "projects/acme/widget.md", "plans/acme/widget/export.md", "log/2026-11.md", "PROJECTS.md"} <= paths)
        self.assertEqual(d["providers"]["codex"]["logged_in"], False)
        self.assertEqual(len(d["sessions"]), 1)
        self.assertEqual(d["who"], ME)
        self.assertEqual(sorted(p["who"] for p in d["people"]), sorted(["bob@studio", ME, "me@testmac"]))   # carol is stale
        self.assertEqual({p["title"]: p["status"] for p in d["plans"]}, {"Release train": "approved", "Export plan": "unversioned"})
        self.assertEqual({c["kind"] for c in d["collisions"]}, {"same-branch", "expired-lock"})
        for k in ("kpis", "timing", "runs", "session_log", "items", "team"):
            self.assertIn(k, d)

    def test_app_features(self):
        js, css = dashboard.JS, dashboard.CSS
        for needle in ("function openPalette", "function encode(", "function decode(", "hashchange", "localStorage",
                       "function viewHome", "function viewInitiatives", "function viewInitiative", "function viewProjects", "function viewProject",
                       "function viewTeam", "function viewTasks", "function viewActivity", "function viewExplorer", "function viewHealth",
                       "function viewSearch", "function viewActions", "function renderDrawer", "function taskCmds", "function changelog",
                       "function pageTabs", "function crumbs", "function fitTables", "function statusCard", "function statusLine", "function liveRun", "function timePanel", "function membersBlock",
                       "function roadmap", "function progOf", "function highlight", 'role: "tablist"', 'role: "tabpanel"'):
            self.assertIn(needle, js, needle)
        for needle in ("function icon(", "function sessionBlock", "function actionGroups", "function cmdMenu", "function rowClick"):
            self.assertIn(needle, js, needle)
        for needle in ("prefers-reduced-motion", "@media print", "--logo-dark", "max-width:768px", ".ptabs{position:sticky", "main .tw.st:not(.x) table.t th", "main .tw.st.x{overflow-x:auto}", ".sblock{", ".figs{"):
            self.assertIn(needle, css, needle)
        self.assertNotIn("innerHTML", js)
        self.assertNotIn("insertAdjacentHTML", js)
        self.assertNotIn("document.write", js)

    def test_design_system(self):
        """v4: four type sizes, two weights, sentence case, no stripes, SVG icons, one amber accent per surface."""
        css, js = dashboard.CSS, dashboard.JS
        self.assertEqual(set(re.findall(r"font-size:([^;}\"]+)", css + js)), {"var(--fs-1)", "var(--fs-2)", "var(--fs-3)", "var(--fs-4)"})
        self.assertEqual(set(re.findall(r"font-weight:(\d+)", css + js)), {"400", "600"})
        self.assertNotIn("uppercase", css)
        self.assertEqual(re.findall(r"border-(?:left|right):\s*[2-9]px", css), [], "no coloured side stripes")
        for glyph in "⧉⎇⌕◐▲▼❚":                                            # icons are authored SVG, not Unicode glyphs
            self.assertNotIn(glyph, js, glyph)
        self.assertNotIn("backdrop-filter", css)
        self.assertEqual(css.count("background:var(--accent)"), 1, "amber fills only the primary button")
        self.assertIn("--accent:#ffb34a", dashboard.LIGHT)
        self.assertIn("--accent:#ffb34a", dashboard.DARK)
        # elevation is declared once: a shadow in light, a hairline in dark
        self.assertIn("--elev-border:0 solid transparent", dashboard.LIGHT)
        self.assertIn("--shadow:none", dashboard.DARK)

    def test_long_texts_do_not_widen_the_page(self):
        """Real milestones and goals are long: the initiatives table has a fixed layout (the name takes the flexible
        width), the next date is the due date with the milestone as one truncated line and the full text on hover."""
        css, js = dashboard.CSS, dashboard.JS
        self.assertIn("table.it{table-layout:fixed}", css)
        for col in ("c-health", "c-prog", "c-ms", "c-work", "c-st", "c-who", "c-act"):
            self.assertRegex(css, r"table\.it th\." + col + r"\{width:\d+px\}", col)    # every column but the name is fixed
        self.assertNotRegex(css, r"table\.it th\.c-name\{width")
        self.assertIn('h("span", {class: "ms ell", text: x.milestone})', js)             # one truncated line
        self.assertIn('"Milestone: " + x.milestone', js)                                 # the full text on hover
        self.assertIn('"No due date"', js)
        self.assertIn(".inir .nd{display:flex", css)                                     # Home: one line per initiative
        self.assertIn(".c-who{display:none}", css)                                       # a hidden column hides its header too

    def test_rows_are_links(self):
        """Clickable rows hold a real link (keyboard, screen readers, j/k) instead of being focusable themselves."""
        js = dashboard.JS
        self.assertEqual(re.findall(r'h\("(?:tr|li|article)", \{[^}]*tabindex: "0"', js), [])
        self.assertIn('"data-nav": ""', js)
        self.assertIn("function taskHref(", js)

    def test_routes(self):
        js = dashboard.JS
        # #/<view>[/<id with slashes and dots>][?query]: ids are kept whole; v2 links (?p=, ?i=, #/portfolio) still resolve
        self.assertIn(r'/^#\/([a-z]+)(\/[^?]*)?(?:\?(.*))?$/', js)
        self.assertIn('String(st.id).split("/").map(encodeURIComponent).join("/")', js)
        self.assertIn('m[1] === "portfolio" ? "initiatives"', js)
        self.assertIn('if (k === "p" && val) { st.view = "projects"; st.id = val; continue; }', js)
        for route in ('"initiatives"', '"projects"', '"team"', '"actions"'):
            self.assertIn(route, js)
        # every dev task command puts -p/-i before the verb (argparse rejects it after)
        self.assertIsNone(re.search(r'dev task (start|done|release|move|block|cancel|history|show|add) [^"\n]*-[pi] ', js))
        self.assertIn('"dev task " + flagOf(it.scope) + " "', js)

    def test_marker_both_forms_stripped(self):
        strip = js_strip_marker()
        self.assertEqual(strip(MARK + " devkit@main · did X · tests green · next: Y"), "devkit@main · did X · tests green · next: Y")
        self.assertEqual(strip("**" + MARK + "** devkit@main · x"), "devkit@main · x")
        self.assertEqual(strip(LEGACY + " kit@release/26.11.4 · y"), "kit@release/26.11.4 · y")
        self.assertEqual(strip("2 commit(s) · status: " + MARK + " widget@main · z"), "2 commit(s) · status: widget@main · z")
        self.assertEqual(strip("1 commit(s) · status: " + LEGACY + " widget@main · z"), "1 commit(s) · status: widget@main · z")
        self.assertEqual(strip("devkit@main · no marker"), "devkit@main · no marker")
        self.assertIn("function markerChip", dashboard.JS)

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
        self.assertEqual((d["projects"], d["initiatives"], d["actions"], d["items"], d["people"], d["collisions"]), ([], [], [], [], [], []))
        self.assertEqual(d["kpis"]["open"], 0)

    def test_scale(self):
        """90 projects, a 77-member initiative with no clones, 400 items: renders, links the right items, stays compact."""
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            members = [f"fireflyframework/core-{i:02d}" for i in range(77)]
            projects = [{"id": pid, "owner": pid.split("/")[0], "name": pid.split("/")[1], "local": False, "path": None, "prs_open": [], "worktrees": [], "main": {},
                         "note_summary": {"status": "active", "tasks": {"now": [], "next": [], "later": [], "done_recent": [], "open": 0, "done": 0, "overdue": [], "items": []}},
                         "initiatives": ["jafly"]} for pid in members]
            projects += [{"id": f"acme/p{i}", "owner": "acme", "name": f"p{i}", "local": True, "prs_open": [pr(i, "failing")], "worktrees": [], "main": {"branch": "main"}} for i in range(13)]
            for n in range(400):
                pid = members[n % 77] if n < 330 else f"acme/p{n % 13}"
                item(root, pid, f"T-s{n:03d}", f"Task {n}", ["todo", "doing", "blocked", "done"][n % 4], ["now", "next", "later"][n % 3],
                     links=["initiative:jafly"] if n % 5 == 0 and n < 330 else [])
            ini = {"id": "jafly", "title": "Jafly", "projects": members, "members": [{"id": m, "status": "active", "local": False} for m in members],
                   "milestone": LONG_MILESTONE, "goal": LONG_GOAL, "due": ""}           # no due date, long texts, nobody on it
            reg = {"projects": projects, "initiatives": [ini] + [{"id": f"i{k}", "title": f"I{k}", "projects": []} for k in range(4)], "github_user": "me"}
            html = dashboard.render(reg, handoff=root, now=NOW)
            d = embedded(html)
        self.assertEqual((len(d["projects"]), len(d["initiatives"]), len(d["items"])), (90, 5, 400))
        self.assertEqual(len(d["initiatives"][0]["linked_items"]), 66)
        self.assertEqual((d["initiatives"][0]["milestone"], d["initiatives"][0]["goal"], d["initiatives"][0]["due"]), (LONG_MILESTONE, LONG_GOAL, ""))
        self.assertEqual(d["people"], [])
        self.assertEqual(d["kpis"]["not_cloned"], 77)
        self.assertEqual(d["kpis"]["prs_failing"], 13)
        self.assertLess(len(html), 3_000_000)


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
        self.assertIn("dev task -p acme/widget done 2", cmds)             # overdue project task (Now=1, Next=2); -p before the verb
        self.assertIn("dev initiative show lending", cmds)                # blocked / overdue initiative
        self.assertIn("dev task -i lending done 2", cmds)                 # overdue initiative task
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


class TeamWorkTests(unittest.TestCase):
    """Multi-user decisions: work items, locks, collisions, people, KPIs, session logs, plans, timing."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.h = Path(cls.tmp.name)
        make_handoff(cls.h)
        cls.items = dashboard.read_items(cls.h, NOW)
        cls.by = {i["id"]: i for i in cls.items}
        cls.team = dashboard.read_presence(cls.h, {}, now=NOW)
        cls.slog = dashboard.read_session_log(cls.h)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_items_and_lock_state(self):
        a1, a2, b2 = self.by["T-a1"], self.by["T-a2"], self.by["T-b2"]
        self.assertEqual((a1["scope"], a1["status"], a1["bucket"], a1["lock"], a1["links"]), ("acme/widget", "doing", "now", "held", ["initiative:lending"]))
        self.assertEqual(a2["lock"], "expired")
        self.assertEqual(b2["lock"], "")
        self.assertEqual(self.by["T-i1"]["initiative"], "lending")
        self.assertEqual(len(a1["changelog"]), 2)
        self.assertTrue(a1["changelog"][0].startswith("v2 · ") and "claim" in a1["changelog"][0])
        self.assertEqual(a1["path"], "items/acme/widget/T-a1.md")

    def test_linked_items_match_the_cli(self):
        ids = sorted(i["id"] for i in dashboard.linked_items(self.items, "lending", ["acme/widget", "acme/api"]))
        self.assertEqual(ids, ["T-a1", "T-b1", "T-i1"])            # own + members' items tagged initiative:lending

    def test_presence_ignores_stale_people(self):
        self.assertEqual(sorted(f"{p['user']}@{p['host']}" for p in self.team), ["bob@studio", "me@testmac"])
        self.assertEqual(dashboard.read_presence(self.h, {"team": [{"user": "x", "host": "y"}]}), [{"user": "x", "host": "y"}])

    def test_collisions(self):
        cs = dashboard.collisions(self.team, self.items, NOW)
        same = [c for c in cs if c["kind"] == "same-branch"]
        self.assertEqual([(c["project"], c["branch"]) for c in same], [("acme/widget", "feat/z")])
        self.assertEqual(same[0]["who"], ["bob@studio (codex)", "me@testmac (claude)"])
        exp = [c for c in cs if c["kind"] == "expired-lock"]
        self.assertEqual([c["id"] for c in exp], ["T-a2"])                  # T-a4 is done: not a collision
        one = [{"user": "a", "host": "h", "sessions": [{"project": "p", "branch": "main", "agent": "claude", "last_seen": NOW}]},
               {"user": "b", "host": "h", "sessions": [{"project": "p", "branch": "", "agent": "codex", "last_seen": NOW}]}]
        self.assertEqual(dashboard.collisions(one, [], NOW), [])           # unknown branch is not a collision

    def test_people(self):
        local = [{"agent": "codex", "project": "acme/widget", "branch": "fix/a", "active": True, "last_seen": int(NOW), "session": "s1", "progress": {"step": 1}}]
        ppl = {p["who"]: p for p in dashboard.people(self.team, self.items, self.slog, NOW, local=local, me="me@testmac")}
        me = ppl["me@testmac"]
        self.assertEqual([s["branch"] for s in me["sessions"]], ["fix/a"])     # this machine: the fresher local sessions
        self.assertEqual(me["sessions"][0]["progress"], {"step": 1})
        self.assertEqual(sorted(me["claimed"]), ["T-a1"])
        self.assertEqual(ppl["bob@studio"]["claimed"], ["T-a2"])               # expired but still held
        self.assertTrue(all(r["who"] == "bob@studio" for r in ppl["bob@studio"]["recent"]))

    def test_actions_for_locks_and_collisions(self):
        reg = registry()
        cs = dashboard.collisions(self.team, self.items, NOW)
        acts = dashboard.build_actions(reg, NOW, self.items, cs)
        cmds = {a["kind"]: a["cmd"] for a in acts}
        self.assertEqual(cmds["expired-lock"], 'dev task -p acme/widget release T-a2 --why "lock expired"')
        self.assertEqual(cmds["collision"], "dev team")
        self.assertEqual(cmds["blocked-task"], "dev task -p acme/widget show T-a3")   # mine (me@<this host>) and blocked
        kinds = [a["kind"] for a in acts]
        self.assertLess(kinds.index("expired-lock"), kinds.index("blocked"))

    def test_kpis(self):
        reg = registry()
        ppl = dashboard.people(self.team, self.items, self.slog, NOW, local=reg["sessions"], me=ME)
        k = dashboard.kpis(reg, self.items, ppl, [{"sev": "risk"}, {"sev": "warn"}], NOW)
        self.assertEqual((k["initiatives"], k["projects"], k["not_cloned"]), (1, 2, 1))
        self.assertEqual((k["open"], k["doing"], k["blocked"]), (6, 2, 1))
        self.assertEqual((k["people"], k["people_active"], k["live_sessions"]), (3, 3, 3))
        self.assertEqual((k["locks"], k["prs_failing"], k["needs_me"], k["urgent"]), (2, 2, 2, 1))

    def test_session_log(self):
        self.assertEqual(len(self.slog), 2)
        new, old = self.slog
        self.assertGreater(new["ts"], old["ts"])
        self.assertEqual((new["scope"], new["who"], new["agent"], new["branch"]), ("acme/widget", "me@testmac", "claude", "main"))
        self.assertNotIn("<!--", new["text"])
        self.assertEqual((new["pre"], new["parts"]["task"], new["parts"]["check"]), ("2 commit(s): feat: x", "Ship export", "✓ 31 tests green"))
        self.assertIn(LEGACY, old["text"])                                  # the legacy one-line form: shown as written
        self.assertIsNone(old["parts"])

    def test_plans(self):
        ps = {p["title"]: p for p in dashboard.read_plans(self.h)}
        self.assertEqual((ps["Release train"]["status"], ps["Release train"]["version"], ps["Release train"]["needs_revision"]), ("approved", "2", False))
        self.assertEqual(len(ps["Release train"]["changelog"]), 2)
        self.assertEqual((ps["Export plan"]["status"], ps["Export plan"]["needs_revision"]), ("unversioned", True))

    def test_since_yesterday_uses_logs_and_items(self):
        s = dashboard.since_yesterday(registry(), {}, NOW, self.items, self.slog)
        self.assertIn({"project": "acme/widget", "text": "Old expired but done", "id": "T-a4", "who": "me@testmac"}, s["done"])
        self.assertEqual({x["who"] for x in s["sessions"]}, {ME, "me@testmac", "bob@studio"})   # bob's codex line is not hidden by my live codex session
        self.assertTrue(any(x["who"] == ME and x["live"] for x in s["sessions"]))

    def test_timing_summary(self):
        recs = [{"ts": NOW - 3600, "kind": "turn", "secs": m * 60, "project": "acme/widget"} for m in (1, 2, 3, 4, 10)]
        recs += [{"ts": NOW - 3600, "kind": "run", "secs": 90 * 60, "estimate": 60, "project": "acme/widget", "what": "export"},
                 {"ts": NOW - 7200, "kind": "run", "secs": 30 * 60, "estimate": 30, "project": "acme/api", "what": "api"},
                 {"ts": NOW - 60, "kind": "tool", "secs": 300, "project": "acme/widget", "what": f"Bash: export GITHUB_TOKEN={FAKE_PAT} && ./mvnw verify"},
                 {"ts": NOW - 60, "kind": "tool", "secs": 5, "project": "acme/widget", "what": "Bash: ls"},
                 {"ts": NOW - 40 * 86400, "kind": "tool", "secs": 999, "what": "too old"},
                 {"ts": NOW - 60, "kind": "subagent", "secs": 120, "what": "Explore"}, {"ts": NOW - 60, "kind": "subagent", "secs": 600, "what": "review"}]
        t = dashboard.timing_summary(recs, NOW)
        self.assertEqual((t["turns"]["n"], t["turns"]["median"], t["turns"]["p90"]), (5, 3.0, 4.0))
        self.assertEqual((t["ratio"], t["runs"]), (1.25, 2))                 # median of 1.5 and 1.0
        self.assertEqual([r["secs"] for r in t["slow_tools"]], [300, 5])
        self.assertNotIn("ghp_", json.dumps(t))
        self.assertIn("./mvnw verify", t["slow_tools"][0]["what"])
        self.assertEqual((t["subagents"]["n"], t["subagents"]["median"]), (2, 6.0))
        self.assertEqual(t["by_project"]["acme/widget"]["ratio"], 1.5)
        self.assertEqual(dashboard.timing_summary([], NOW)["turns"], {"n": 0, "median": None, "p90": None})

    def test_status_parts(self):
        sp = dashboard.status_parts
        self.assertEqual(sp(SAVED), {"project": "widget", "branch": "main", "task": "Ship export", "done": "export endpoint done",
                                     "check": "✓ 31 tests green", "progress": "3/7", "next": "contract test"})
        p = sp("**" + MARK + "** api · `main` ❯ - ❯ did it ❯ – no tests (docs only) ❯ next → review")
        self.assertEqual((p["project"], p["branch"], p["task"], p["check"]), ("api", "main", "", "– no tests (docs only)"))
        seg = sp("api ⎇ main ❯ T-fwk6cesh07u ❯ release prepared ❯ next → push once")           # older segmented form
        self.assertEqual((seg["project"], seg["branch"], seg["done"], seg["next"]), ("api", "main", "release prepared", "push once"))
        for legacy in ("x@main · did · tests", LEGACY + " x@main · did", "", None):
            self.assertIsNone(sp(legacy))

    def test_status_parts_in_payload(self):
        html = dashboard.render(registry(), handoff=self.h, now=NOW)
        d = embedded(html)
        self.assertEqual(d["projects"][0]["status_line"]["parts"]["next"], "contract test")
        self.assertEqual(d["sessions"][0]["status_parts"]["check"], "⚠ e2e flaky")
        me = next(p for p in d["people"] if p["who"] == ME)
        self.assertEqual(me["sessions"][0]["status_parts"]["task"], "")
        self.assertTrue(any(x.get("parts") for x in d["session_log"]))
        self.assertNotIn(EVIL, html)

    def test_redact(self):
        r = dashboard._redact
        self.assertEqual(r("curl -H 'Authorization: Bearer abc.def' x"), "curl -H 'Authorization: Bearer ***' x")
        self.assertEqual(r("gh auth login --token abc123"), "gh auth login --token ***")
        self.assertEqual(r("password=hunter2 ./run"), "password=*** ./run")
        self.assertEqual(r("git clone fireflyframework-org-redesign-handoff-really-long"), "git clone fireflyframework-org-redesign-handoff-really-long")


if __name__ == "__main__":
    unittest.main()
