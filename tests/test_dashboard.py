# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the dashboard (decisions in Python, safe rendering). Run: python3 -m unittest discover -s tests -v"""
import glob
import html as htmlmod
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

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

    def test_only_open_url_opens_a_tab_and_it_goes_through_safe_url(self):
        js = dashboard.JS
        self.assertEqual(js.count("window.open("), 1, "a URL from the data must open through openUrl, never directly")
        self.assertIn('function openUrl(u) { const s = safeUrl(u); if (s !== "#") window.open(s, "_blank", "noopener"); }', js)

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
                       "function viewSearch", "function viewActions", "function renderModal", "function taskCmds", "function changelog",
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
        self.assertIn(".c-who{display:none}", css)                                       # a hidden column hides its header too

    def test_app_shell_and_footer(self):
        """A full-height column: the main area grows and the footer sits at the bottom of short pages; a 12-column grid to 1600 px."""
        css = dashboard.CSS
        self.assertIn("body{display:flex;flex-direction:column;min-height:100vh;min-height:100dvh}", css)
        self.assertIn("#view{flex:1 0 auto;width:100%}", css)
        self.assertIn(".foot{margin-top:auto}", css)
        self.assertIn("--wide:1600px", css)
        self.assertIn(".g12{display:grid;grid-template-columns:repeat(12,minmax(0,1fr))", css)
        self.assertIn('<main id="view"', dashboard.HTML)
        self.assertLess(dashboard.HTML.index('<main id="view"'), dashboard.HTML.index('<footer class="foot">'))

    def test_dialog_accessibility(self):
        """Quick looks are one native <dialog>: labelled, modal (focus trap), Esc and the backdrop close it, focus returns to the
        trigger, and every modal has a deep link."""
        js, html = dashboard.JS, dashboard.HTML
        self.assertIn('<dialog class="modal" id="modal" aria-labelledby="modal-title" aria-modal="true"></dialog>', html)
        self.assertIn("dlg.showModal()", js)
        self.assertIn('dlg.addEventListener("cancel", e => { e.preventDefault(); closeModal(); });', js)
        self.assertIn('dlg.addEventListener("click", e => { if (e.target === dlg) closeModal(); });', js)
        self.assertIn("if (lastFocus && lastFocus.isConnected) lastFocus.focus({preventScroll: true});", js)
        self.assertIn('h("h2", {id: "modal-title", text})', js)
        self.assertIn('"aria-label": "Close (Esc)"', js)
        self.assertIn('modal: ""};', js)                                                  # a route key: ?modal=<kind>:<id>
        self.assertIn('set({modal: kind + ":" + id, task: ""}, true)', js)
        for kind in ("task", "initiative", "project", "person", "session", "action", "actions", "since"):
            self.assertIn(kind + ": ", js[js.index("const MODALS = {"):js.index("const MODALS = {") + 260], kind)
        self.assertIn("dialog.modal::backdrop", dashboard.CSS)
        self.assertNotIn('id="drawer"', html)

    def test_visuals_are_named(self):
        """Every SVG is either a named image (role=img + aria-label) or decorative (aria-hidden); visuals carry tooltips
        (hover and focus), and the status colors come from the fixed dataviz palette."""
        js, css = dashboard.JS, dashboard.CSS
        calls = [m.start() for m in re.finditer(r'sv\("svg", \{', js)]
        self.assertGreaterEqual(len(calls), 7)
        for i in calls:
            attrs = js[i:js.index("}", i)]
            self.assertTrue(('role: "img"' in attrs and '"aria-label"' in attrs) or '"aria-hidden"' in attrs, attrs[:120])
        for fn in ("function ring(", "function waffle(", "function spark(", "function segBar(", "function heatmap(", "function ciStrip(",
                   "function lockRing(", "function duePill(", "function statTile(", "function sessBar("):
            self.assertIn(fn, js, fn)
        self.assertIn('document.addEventListener("focusin"', js)
        self.assertIn('document.addEventListener("pointerover"', js)
        self.assertGreaterEqual(js.count('"data-tip"'), 12)
        for token in ("--s-ok:#0ca30c", "--s-warn:#fab219", "--s-risk:#d03b3b"):
            self.assertIn(token, dashboard.LIGHT)
            self.assertIn(token, dashboard.DARK)

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

    def test_static_export_is_the_page_of_its_data(self):
        """`render` (dev sync, --export) is `page()` of its own data with no nonce: no CSP attributes, no live flag."""
        self.assertEqual(self.html, dashboard.page(self.data))   # the same data again (a second collect sees a later clock)
        self.assertNotIn("nonce=", self.html)
        self.assertNotIn("live", self.data)
        self.assertIn('<script>(function(){try{var t=JSON.parse(localStorage.getItem("devkit.theme"))', self.html)
        self.assertIn('<script id="devkit-data" type="application/json">', self.html)

    def test_a_nonce_marks_every_inline_script_and_style_and_nothing_else(self):
        nonce = "Zm9vYmFyYmF6cXV4MTIzNA"
        live = dashboard.page(self.data, nonce=nonce)
        tags = re.findall(r"<(script|style)\b([^>]*)>", live)
        self.assertEqual(sorted(t for t, _ in tags), ["script", "script", "script", "style"])
        self.assertTrue(all(f'nonce="{nonce}"' in attrs for _, attrs in tags), tags)
        self.assertEqual(live.replace(f' nonce="{nonce}"', ""), self.html)      # the static page plus nonces, byte for byte

    def test_live_mode_replaces_the_timed_reload_and_the_static_page_keeps_it(self):
        # live (D.live, from the server): the event stream, partial fetches and an in-place re-render; the static
        # export still reloads every 2 minutes and never opens a stream
        js = dashboard.JS
        boot = js[js.index("(function boot() {"):]
        live_at, reload_at = boot.index("if (LIVE.on) { liveStart(); return; }"), boot.index("location.reload()")
        self.assertLess(live_at, reload_at)                                   # live returns before the reload timer
        self.assertEqual(js.count("new EventSource("), 1)
        self.assertIn('new EventSource("/api/v1/events?since=" + ver)', js[js.index("function liveStart()"):])
        self.assertIn('"only=" + areas.map(encodeURIComponent).join(",")', js)
        self.assertIn('liveGet("/api/v1/files")', js)
        derive = js[js.index("function derive() {"):js.index("\n}\nderive();")]
        for name in ("ITEMS", "P", "PEOPLE", "RUNS", "INITS", "FILES", "ACTIONS", "W"):
            self.assertIn(f"\n  {name} = ", derive, name)                      # every derived constant is re-derived
        self.assertIn("D = LIVE.pending; LIVE.pending = null; derive(); IDX = null;", js)
        self.assertEqual(re.findall(r"^const (?:ITEMS|P|PEOPLE|RUNS|INITS|FILES|ACTIONS|D) = ", js, re.M), [])

    def test_a_nonce_that_could_break_out_of_the_attribute_is_refused(self):
        for bad in ('abcdefghijklmnop" onload="alert(1)', "short"):
            with self.assertRaises(ValueError, msg=bad):
                dashboard.page(self.data, nonce=bad)

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


def local_ts(y, mo, d, hh, mm):
    return time.mktime((y, mo, d, hh, mm, 0, 0, 0, -1))


class DetailTests(unittest.TestCase):
    """The data the page lacked: honest project states, jobs, CI watches, whole runs and items, explorer files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = TempState()
        self.st = self.state.__enter__()
        from devkit import jobs, progress
        self.patches = [mock.patch.object(progress, "RUNS", self.st / "runs"),
                        mock.patch.object(jobs, "JOURNAL", self.st / "jobs" / "journal.jsonl"),
                        mock.patch.object(jobs, "LOGS", self.st / "logs" / "jobs"),
                        mock.patch.object(jobs, "AGENTS_DIR", self.root / "LaunchAgents")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.state.__exit__()
        self.tmp.cleanup()

    def test_a_project_without_git_or_pr_data_is_not_known(self):
        widget, remote_only = registry()["projects"]
        self.assertTrue(dashboard.project_health(widget, NOW)["known"])
        h = dashboard.project_health(remote_only, NOW)
        self.assertEqual((h["level"], h["known"]), ("ok", False))     # nothing wrong, and nothing to show it is right
        plain = {"id": "_local/x", "git": False, "main": {}, "prs_open": []}
        self.assertFalse(dashboard.project_health(plain, NOW)["known"])
        self.assertIn('if (p.health && p.health.known === false) return "none";', dashboard.JS)

    def test_jobs_show_the_last_and_next_run_and_failures(self):
        from devkit import jobs
        now = local_ts(2026, 11, 6, 10, 0)
        defs = {"sync": {"enabled": True, "every": 60, "about": "map", "cmd": ["sync"], "timeout": 20},
                "gc": {"enabled": True, "at": "04:00", "cmd": ["gc"], "timeout": 30},
                "lessons": {"enabled": False, "weekly": "fri 16:00", "cmd": ["lesson"], "timeout": 5}}
        jobs.JOURNAL.parent.mkdir(parents=True)
        jobs.JOURNAL.write_text("".join(json.dumps(r) + "\n" for r in [
            {"job": "sync", "start": now - 7200, "end": now - 7100, "rc": 0, "duration": 100, "summary": "ok"},
            {"job": "gc", "start": now - 3 * 3600, "end": now - 3 * 3600 + 5, "rc": 0, "duration": 5, "summary": "freed 1 GB"},
            {"job": "sync", "start": now - 3600, "end": now - 3500, "rc": 1, "duration": 100, "summary": "push failed"}]))
        jobs.AGENTS_DIR.mkdir()
        (jobs.AGENTS_DIR / "com.devkit.job.sync.plist").write_text("x")
        with mock.patch.object(jobs, "jobs", lambda: defs), mock.patch("time.time", lambda: now):
            out = {j["name"]: j for j in dashboard.jobs_summary(now)}
        self.assertEqual((out["sync"]["state"], out["sync"]["runs"], out["sync"]["failed"]), ("failed", 2, 1))
        self.assertEqual(out["sync"]["last"]["summary"], "push failed")
        self.assertEqual(out["sync"]["next"], round(now - 3600 + 3600))        # every 60 min from the last start
        self.assertEqual(out["gc"]["next"], round(local_ts(2026, 11, 7, 4, 0)))  # 04:00 has passed today
        self.assertEqual((out["lessons"]["state"], out["lessons"]["next"]), ("off", None))
        self.assertEqual([out[n]["installed"] for n in ("sync", "gc")], [True, False])

    def test_one_job_has_its_history_newest_first_and_its_log_tail(self):
        from devkit import jobs
        now = time.time()
        defs = {"sync": {"enabled": True, "every": 60, "cmd": ["sync"], "timeout": 20}}
        jobs.JOURNAL.parent.mkdir(parents=True)
        jobs.JOURNAL.write_text("".join(json.dumps({"job": "sync", "start": now - 60 * k, "rc": 0, "summary": f"run {k}"}) + "\n"
                                        for k in (3, 2, 1)))
        jobs.LOGS.mkdir(parents=True)
        (jobs.LOGS / "sync.log").write_text("\n".join(f"line {i}" for i in range(250)) + "\n")
        with mock.patch.object(jobs, "jobs", lambda: defs), mock.patch.object(jobs, "loaded", lambda n: True):
            one = dashboard.jobs_detail("sync")
            self.assertIsNone(dashboard.jobs_detail("nope"))
            self.assertIsNone(dashboard.jobs_detail("../sync"))
            every = dashboard.jobs_detail()
        self.assertEqual([r["summary"] for r in one["history"]], ["run 1", "run 2", "run 3"])
        self.assertEqual((len(one["log"]), one["log"][0], one["log"][-1]), (200, "line 50", "line 249"))
        self.assertTrue(one["loaded"])
        self.assertEqual(([j["name"] for j in every["jobs"]], every["complete"]), (["sync"], True))

    def test_ci_watches_newest_first_with_the_first_error_and_live_watchers(self):
        w = self.st / "ci" / "watch"
        w.mkdir(parents=True)
        diag = {"run": "ci", "number": 9, "id": 4242, "url": "https://x/9", "class": "test", "action": "reproduce it locally",
                "failed_jobs": [{"name": "unit", "step": "pytest"}], "excerpt": "E" * 2000}
        (w / "acme_widget-main.json").write_text(json.dumps({"ts": 100, "state": "passed", "repo": "acme/widget", "branch": "main",
                                                            "runs": [], "diagnoses": []}))
        (w / "acme_widget-fix_y.json").write_text(json.dumps({"ts": 200, "state": "failed", "repo": "acme/widget", "branch": "fix/y",
                                                             "sha": "abc1234", "runs": [{"name": "ci", "number": 9}], "diagnoses": [diag]}))
        os.utime(w / "acme_widget-main.json", (100, 100))
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        (w / "widget-fix_y-abc1234.lock").write_text(f"{os.getpid()} 150")
        (w / "widget-main-def5678.lock").write_text(f"{dead.pid} 140")
        ci = dashboard.ci_watches()
        self.assertEqual([x["branch"] for x in ci["watches"]], ["fix/y", "main"])
        top = ci["watches"][0]
        self.assertEqual((top["class"], top["action"], len(top["first_error"])), ("test", "reproduce it locally", 600))
        self.assertEqual((top["failures"][0]["id"], top["failures"][0]["jobs"]), (4242, [{"name": "unit", "step": "pytest"}]))
        self.assertTrue(ci["complete"])
        self.assertEqual(len(dashboard.ci_watches(excerpt=0)["watches"][0]["first_error"]), 2000)
        self.assertEqual(ci["watching"], [{"key": "widget-fix_y", "sha": "abc1234", "pid": os.getpid(), "since": 150}])

    def test_an_item_is_whole_with_everything_that_names_it(self):
        from devkit import progress
        h = self.root / "handoff"
        make_handoff(h)
        log = [f"v{n} · {iso(NOW - n)} · me@testmac (claude) · note: entry {n}" for n in range(100, 0, -1)]
        item(h, "acme/widget", "T-big", "Long task", "doing", "now", "me@testmac (claude:sess12345678)", NOW + 3600, ["initiative:lending"], log=log)
        f = h / "items/acme/widget/T-big.md"
        f.write_text(f.read_text().replace("Body " + EVIL, "x" * 10_000))
        (h / "plans/acme/widget/export.md").write_text("# Export plan\n\nCovers T-big.\n")
        (h / "sessions/acme/widget/2026-11.md").write_text(f"# Sessions\n\n- {time.strftime('%Y-%m-%d %H:%M', time.localtime(NOW))} · me@testmac · claude · `main` · worked on T-big\n")
        (self.st / "sessions").mkdir()
        (self.st / "sessions" / "claude-sess12345678-rest.json").write_text(json.dumps({"branch": "feat/export", "top": "/w/export"}))
        progress.RUNS.mkdir()
        (progress.RUNS / "claude-other.json").write_text(json.dumps({"v": 2, "agent": "claude", "session": "other", "goal": "Ship T-big",
                                                                     "state": "running", "started": NOW - 600, "plan": [], "intervals": []}))
        reg = registry()
        reg["projects"][0]["worktrees"].append({"path": "/w/export", "branch": "feat/export"})
        reg["projects"][0]["prs_open"].append(pr(40, branch="feat/export"))
        reg["projects"][0]["path"] = None
        d = dashboard.item_detail("T-big", handoff=h, reg=reg, now=NOW)
        self.assertEqual((len(d["body"]), len(d["changelog"])), (10_000, 100))
        self.assertEqual(d["links"], [{"kind": "initiative", "target": "lending", "text": "initiative:lending"}])
        self.assertEqual((d["lock"]["state"], d["lock"]["session"]), ("held", "sess12345678"))
        self.assertEqual(d["plans"], [{"path": "plans/acme/widget/export.md", "title": "Export plan"}])
        self.assertEqual(len(d["sessions"]), 1)
        self.assertEqual(d["branches"], [{"branch": "feat/export", "worktree": "/w/export", "match": "the holder's session works on it"}])
        self.assertEqual([(p["number"], p["match"]) for p in d["prs"]], [(40, "the PR's branch is linked")])
        self.assertEqual((d["run"]["id"], d["run"]["match"]), ("claude-other", "its goal or a step has the id"))
        for bad in ("T-nope", "../../etc/passwd", "T-big/../x", ""):
            self.assertIsNone(dashboard.item_detail(bad, handoff=h, reg=reg), bad)

    def test_a_run_is_whole_with_evidence_history_lanes_decisions_and_questions(self):
        from devkit import progress
        progress.RUNS.mkdir()
        now = time.time()
        run = {"v": 2, "agent": "claude", "session": "s1", "project": "acme/widget", "goal": "Ship", "state": "running", "mode": "auto",
               "started": now - 1200, "estimate": 40, "intervals": [[now - 1200, None, "turn", "main"]],
               "plan": [{"name": "api", "state": "done", "owner": "a1", "est": 20, "started": now - 1200, "finished": now - 600},
                        {"name": "ui", "state": "running", "owner": "main", "est": 20, "after": ["api"], "started": now - 600}],
               "agents": {"a1": {"label": "Tank: the API", "persona": "Tank", "state": "done", "scope": "lib/**", "started": now - 1200, "finished": now - 600}},
               "updates": [{"ts": now - 600, "kind": "step", "text": "api done", "check": "12 tests green", "step": "api"},
                           {"ts": now - 300, "kind": "note", "text": "styling", "step": "ui"}],
               "history": [{"ts": now - 600, "step": "api", "from": "running", "to": "done", "by": "a1"}],
               "decisions": [{"id": "D1", "state": "pending", "question": "Keep the old URL?"}],
               "messages": [{"id": "M1", "from": "a1", "to": "main", "text": "may I touch cli.py?"}]}
        (progress.RUNS / "claude-s1.json").write_text(json.dumps(run))
        d = dashboard.run_detail("claude-s1", now=now)
        api, ui = d["steps"]
        self.assertEqual((api["owner_label"], api["scope"], ui["after"]), ("Tank", "lib/**", ["api"]))
        self.assertEqual([e["check"] for e in api["evidence"]], ["12 tests green"])
        self.assertEqual(ui["evidence"], [])                                          # a note is not evidence
        self.assertEqual([x["to"] for x in api["history"]], ["done"])
        self.assertEqual((d["lanes"][0]["persona"], d["mode"]), ("Tank", "auto"))
        self.assertEqual((d["decisions"][0]["id"], d["questions"][0]["text"]), ("D1", "may I touch cli.py?"))
        self.assertIsNotNone(d["eta_lo"])
        for bad in ("claude-nope", "../runs/claude-s1", "..", ""):
            self.assertIsNone(dashboard.run_detail(bad), bad)
        runs = dashboard._runs(now, "testmac")
        self.assertEqual((runs[0]["id"], runs[0]["host"], runs[0]["mode"]), ("claude-s1", "testmac", "auto"))
        self.assertEqual(([q["id"] for q in runs[0]["questions"]], [x["id"] for x in runs[0]["decisions"]]), (["M1"], ["D1"]))
        self.assertEqual(runs[0]["plan"][1]["after"], ["api"])

    def crew_run(self, now):
        # the live release run of 2026-11-06 in small: the lead's own last update is 78 minutes old, a crew lane reports
        from devkit import progress
        progress.RUNS.mkdir(exist_ok=True)
        run = {"v": 2, "agent": "claude", "session": "s1", "project": "acme/widget", "goal": "Release 26.11.3", "state": "running",
               "started": now - 9600, "estimate": 60, "estimate_cal": 25.8,
               "intervals": [[now - 9600, now - 4680, "turn", "main"], [now - 2880, None, "agent", "a2"]],
               "plan": [{"name": "F4 caller identity", "title": "knowing who ran each command", "state": "running", "owner": "a2", "est": 60,
                         "started": now - 2880}],
               "agents": {"a2": {"label": "Tank: F4 caller identity", "persona": "Tank", "state": "running", "started": now - 2880,
                                 "updates": [{"ts": now - 180, "kind": "note", "text": "ledger written"}]}},
               "updates": [{"ts": now - 4680, "kind": "step", "text": "design done"},
                           {"ts": now - 180, "kind": "agent", "agent": "a2", "text": "ledger written"}],
               "history": [], "decisions": [], "messages": [{"id": "M1", "ts": now - 240, "from": "a2", "to": "main", "text": "may I touch cli.py?"}]}
        (progress.RUNS / "claude-s1.json").write_text(json.dumps(run))
        return run

    def test_a_run_is_seen_when_its_crew_reports_though_the_lead_is_quiet(self):
        from devkit import progress
        now = time.time()
        run = self.crew_run(now)
        r = dashboard._runs(now, "testmac")[0]
        self.assertAlmostEqual(r["updated"], now - 4680, delta=1)                    # the lead's own last update stays what it was
        self.assertAlmostEqual(r["seen"], now - 180, delta=1)                        # the lane's note 3 minutes ago is a sign of life
        self.assertAlmostEqual(dashboard.run_detail("claude-s1", now=now)["seen"], now - 180, delta=1)
        run["agents"]["a2"]["last_seen"] = now - 60                                  # a lane's last hook (contract C3) counts too
        (progress.RUNS / "claude-s1.json").write_text(json.dumps(run))
        self.assertAlmostEqual(dashboard._runs(now, "testmac")[0]["seen"], now - 60, delta=1)

    def test_of_about_is_the_plans_current_total_not_the_first_estimate(self):
        from devkit import progress
        progress.RUNS.mkdir()
        now = time.time()
        # one open turn for two hours: 120 active minutes. "design" took its 60-minute estimate (pace 1.0); "build" has used its
        # 60 minutes and keeps 15% of them (9); "release" needs its 30: 39 minutes left, so the plan's total is 120 + 39 = 159
        run = {"v": 2, "agent": "claude", "session": "s2", "goal": "Release", "state": "running", "started": now - 7200, "estimate": 60,
               "estimate_cal": 25.8, "intervals": [[now - 7200, None, "turn", "main"]],
               "plan": [{"name": "design", "state": "done", "owner": "main", "est": 60, "started": now - 7200, "finished": now - 3600},
                        {"name": "build", "state": "running", "owner": "main", "est": 60, "after": ["design"], "started": now - 3600},
                        {"name": "release", "state": "pending", "owner": "main", "est": 30, "after": ["build"]}],
               "agents": {}, "updates": [], "history": [], "decisions": [], "messages": []}
        (progress.RUNS / "claude-s2.json").write_text(json.dumps(run))
        r = dashboard._runs(now, "testmac")[0]
        self.assertEqual((r["total_min"], r["eta_min"], r["estimate_cal"]), (159, 39, 25.8))
        self.assertEqual(dashboard.run_detail("claude-s2", now=now)["total_min"], 159)

    def test_a_step_keeps_its_plain_title_for_the_page(self):
        now = time.time()
        self.crew_run(now)
        self.assertEqual(dashboard._runs(now, "testmac")[0]["plan"][0]["title"], "knowing who ran each command")
        self.assertEqual(dashboard.run_detail("claude-s1", now=now)["steps"][0]["title"], "knowing who ran each command")

    def test_explorer_files_stay_inside_the_handoff_repository(self):
        h = self.root / "handoff"
        make_handoff(h)
        (self.root / "secret.md").write_text("secret")
        (h / "projects" / "link.md").symlink_to(self.root / "secret.md")
        (h / ".git").mkdir()
        (h / ".git" / "x.md").write_text("git")
        (h / "registry.json").write_text("{}")
        self.assertEqual(dashboard.file_detail("projects/acme/widget.md", h)["text"], (h / "projects/acme/widget.md").read_text())
        for bad in ("../secret.md", "projects/link.md", ".git/x.md", "registry.json", "/etc/passwd", "../../etc/passwd", "", "a\x00b"):
            self.assertIsNone(dashboard.file_detail(bad, h), bad)

    def test_counts_state_their_window(self):
        h = self.root / "handoff"
        make_handoff(h)
        (self.st / "logs").mkdir()
        (self.st / "logs" / "hooks.log").write_text("2026-11-01 10:00 guard claude deny: Branch `x`\n2026-11-03 09:00 guard codex ask: git stash\n")
        d = dashboard.collect(registry(), handoff=h, now=NOW)
        self.assertEqual((d["windows"]["guard_since"], d["windows"]["guard_capped"]), ("2026-11-01 10:00", False))
        self.assertEqual(d["windows"]["sessions_since"], min(s["ts"] for s in d["session_log"]))
        self.assertEqual(d["windows"]["commits_days"], 30)
        for k in ("jobs", "ci", "crew"):
            self.assertIn(k, d)


def branch_row(name, state="on_remote", reason="", **kw):
    """One row of the branch map (branches.map) as the registry keeps it: a branch on origin unless kw says otherwise."""
    r = {"name": name, "current": False, "default": False, "default_branch": "main", "sha": "1a2b3c4", "upstream": "origin/" + name,
         "upstream_remote": "origin", "upstream_gone": False, "ahead": 0, "behind": 0, "base": "origin/main", "base_ahead": 0, "base_behind": 0,
         "where": "local + origin", "last_commit": int(NOW - 3600), "unpushed": 0, "oldest_unpushed": None, "merged_into": None, "last_backup": None,
         "embargo": None, "embargo_via": None, "worktree": None, "worktree_missing": False, "changes": None, "session": False, "pr": None, "task": None,
         "backup": {"state": state, "reason": reason}}
    r.update(kw)
    return r


class BranchMapTests(unittest.TestCase):
    """The branch map on the page (spec P-fy3bwa0pshk R8): the registry's rows reach the page in the map's order, and
    the live server maps one project on demand, saying what git or gh answered."""

    def setUp(self):
        from devkit import audit
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        iso_git = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Ana Dev", "GIT_AUTHOR_EMAIL": "ana@example.com",
                   "GIT_COMMITTER_NAME": "Ana Dev", "GIT_COMMITTER_EMAIL": "ana@example.com"}
        self.patches = [mock.patch.object(core, "STATE", self.root / "state"), mock.patch.object(core, "NOTES", self.root / "handoff" / "projects"),
                        mock.patch.object(audit, "AUDIT", self.root / "state" / "audit"), mock.patch.object(work, "ITEMS", self.root / "handoff" / "items"),
                        mock.patch.dict(core.GIT_ENV, iso_git)]
        from devkit import registry as reg_mod
        self.patches.append(mock.patch.object(reg_mod, "STATE", self.root / "state"))
        for p in self.patches:
            p.start()
        self.repo = self.root / "widget"
        self.git("init", "-q", "-b", "main", str(self.repo), cwd=self.root)
        (self.repo / "a.txt").write_text("a\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "chore: start")
        self.git("switch", "-q", "-c", "feat/export")
        (self.repo / "b.txt").write_text("b\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "feat: export")

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def git(self, *args, cwd=None):
        rc, out, err = core.sh(["git", "-C", str(cwd or self.repo), *args], timeout=60)
        self.assertEqual(rc, 0, err or out)
        return out

    def reg(self, path, **kw):
        p = {"id": "acme/widget", "path": str(path), "local": True, "github": None, "default_branch": "main",
             "main": {"branch": "feat/export", "path": str(path)}, "worktrees": []}
        p.update(kw)
        return {"projects": [p]}

    def test_the_page_gets_the_rows_in_the_maps_order_and_no_map_stays_unknown(self):
        reg = registry()
        reg["projects"][0]["branches"] = [branch_row("main", default=True), branch_row("fix/old", "merged", "its commits are in main", merged_into="main"),
                                          branch_row("feat/x", "not_backed_up", "2 commits only on this Mac", where="local only", upstream=None, unpushed=2,
                                                     worktree=str(core.HOME / "Development/.worktrees/acme/widget/feat-x")),
                                          branch_row("release/1", current=True)]
        with TempState():
            d = dashboard.collect(reg, handoff=self.root / "handoff", now=NOW)
        widget, api = d["projects"][0], d["projects"][1]
        self.assertEqual([r["name"] for r in widget["branches"]], ["release/1", "feat/x", "fix/old", "main"])   # current, at risk, newest, default last
        self.assertEqual(widget["branches"][1]["worktree"], "~/Development/.worktrees/acme/widget/feat-x")      # a path in the home folder, as ~
        self.assertEqual(widget["branches"][1]["backup"], {"state": "not_backed_up", "reason": "2 commits only on this Mac"})
        self.assertIsNone(api["branches"])                                  # no map: unknown, never "no branches"

    def test_the_live_map_reads_the_repository_now(self):
        d = dashboard.branch_detail("acme/widget", self.reg(self.repo), now=NOW)
        rows = {r["name"]: r for r in d["rows"]}
        self.assertEqual([r["name"] for r in d["rows"]], ["feat/export", "main"])          # the current branch first
        self.assertEqual((rows["feat/export"]["where"], rows["feat/export"]["backup"]["state"]), ("local only", "no_remote"))
        self.assertEqual((d["counts"]["total"], d["counts"]["not_backed_up"]), (2, 2))
        self.assertEqual((d["prs"], d["mapped"]), ("none", int(NOW)))                        # not on GitHub: no PRs to ask for

    def test_the_rows_carry_the_projects_backup_policy_for_the_page(self):
        # dev backup policy: the page shows the project's choice under the branch table, from the rows it gets
        from devkit import branches
        d = dashboard.branch_detail("acme/widget", self.reg(self.repo), now=NOW)
        self.assertEqual({r["backup_policy"] for r in d["rows"]}, {None})          # no choice: the page says the default
        branches.set_backup_policy("acme/widget", "off", "a client's fork", "ana")
        d = dashboard.branch_detail("acme/widget", self.reg(self.repo), now=NOW)
        self.assertEqual([(r["backup_policy"]["policy"], r["backup_policy"]["why"]) for r in d["rows"]],
                         [("off", "a client's fork")] * 2)

    def test_the_live_map_says_what_git_answered_when_it_cannot_read_the_repository(self):
        plain = self.root / "plain"
        plain.mkdir()
        d = dashboard.branch_detail("acme/widget", self.reg(plain), now=NOW)
        self.assertEqual(d["error"]["source"], "git")
        self.assertIn("not a git repository", d["error"]["detail"])
        self.assertNotIn("rows", d)

    def test_the_live_map_says_when_gh_is_missing_or_did_not_answer(self):
        from devkit import registry as reg_mod
        r = self.reg(self.repo, github="acme/widget")
        with mock.patch.object(core, "have", lambda tool: tool != "gh"):
            self.assertEqual(dashboard.branch_detail("acme/widget", r, now=NOW)["prs"], "missing")
        with mock.patch.object(core, "have", lambda tool: True), mock.patch.object(reg_mod, "prs", lambda slug, **kw: None):
            self.assertEqual(dashboard.branch_detail("acme/widget", r, now=NOW)["prs"], "error")
        pr41 = {"number": 41, "state": "OPEN", "headRefName": "feat/export", "isDraft": True, "url": "https://github.com/acme/widget/pull/41", "title": "Export", "checks": "passing"}
        with mock.patch.object(core, "have", lambda tool: True), mock.patch.object(reg_mod, "prs", lambda slug, **kw: [pr41]):
            d = dashboard.branch_detail("acme/widget", r, now=NOW)
        self.assertEqual((d["prs"], d["rows"][0]["pr"]["number"], d["rows"][0]["pr"]["draft"]), ("ok", 41, True))

    def test_the_live_map_is_only_for_projects_cloned_here(self):
        self.assertIsNone(dashboard.branch_detail("acme/nope", self.reg(self.repo), now=NOW))
        not_here = self.reg(self.repo, local=False)
        not_here["projects"][0]["path"] = None
        self.assertIsNone(dashboard.branch_detail("acme/widget", not_here, now=NOW))


# --------------------------------------------------------------------------- the page itself, in a real browser

def find_chrome():
    """A Chrome or Chromium for the page's behavior tests: $DEVKIT_TEST_CHROME, Google Chrome, or a Playwright build.
    The suite runs with a temporary HOME (tests/_hermetic.py), so the Playwright builds are also looked for in the
    account's real home: a browser binary there is not devkit state, and Chrome still gets the run's HOME."""
    homes = [Path.home()]
    try:
        import pwd
        homes.append(Path(pwd.getpwuid(os.getuid()).pw_dir))
    except (ImportError, KeyError, OSError):
        pass
    cands = [os.environ.get("DEVKIT_TEST_CHROME"), "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
             "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    for home in (h / "Library/Caches/ms-playwright" for h in homes):
        cands += sorted(glob.glob(str(home / "chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell")), reverse=True)
        cands += sorted(glob.glob(str(home / "chromium-*/chrome-mac*/*.app/Contents/MacOS/*")), reverse=True)
    cands += [shutil.which(n) for n in ("google-chrome", "chromium", "chromium-browser")]
    return next((c for c in cands if c and os.path.isfile(c) and os.access(c, os.X_OK)), None)


CHROME = find_chrome()
NO_CHROME = "the page's behavior tests need Chrome or Chromium (set DEVKIT_TEST_CHROME to its binary)"
# runs after the app's own script: `steps` fill `out`, which comes back as JSON in the dumped DOM
PROBE = """<script>(async () => { const out = {}; const tick = ms => new Promise(r => setTimeout(r, ms));
const $ = (s, r) => (r || document).querySelector(s), $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
const text = el => el ? el.innerText.replace(/\\s+/g, " ").trim() : null;
const go = async hs => { location.hash = hs; await tick(200); };
const key = (el, k) => el.dispatchEvent(new KeyboardEvent("keydown", {key: k, bubbles: true}));
await tick(100);
try { %s } catch (e) { out.probeError = String(e && e.stack || e); }
out.pageError = text($("#dk-error"));
const pre = document.createElement("pre"); pre.id = "probe-out"; pre.textContent = JSON.stringify(out); document.body.append(pre); })();</script>"""


def chrome(url: str, width: int, height: int = 900, budget: int = 6000) -> str:
    with tempfile.TemporaryDirectory() as prof:
        res = subprocess.run([CHROME, "--headless", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--hide-scrollbars",
                              f"--user-data-dir={prof}", f"--window-size={width},{height}", f"--virtual-time-budget={budget}", "--dump-dom", url],
                             capture_output=True, text=True, timeout=120)
    m = re.search(r'<pre id="probe-out">(.*?)</pre>', res.stdout, re.S)
    if not m:
        raise AssertionError("the probe did not finish: " + res.stderr[-1500:])
    return json.loads(htmlmod.unescape(m.group(1)))


def browse(page: str, steps: str, width: int = 1440, hash_: str = "", budget: int = 6000) -> dict:
    """Load a page in headless Chromium at a width, run the probe's `steps` (JS that fills `out`) and return `out`."""
    with tempfile.TemporaryDirectory() as t:
        f = Path(t) / "page.html"
        f.write_text(page.replace("</body>", PROBE % steps + "</body>"), encoding="utf-8")
        return chrome(f.as_uri() + hash_, width, budget=budget)


def fixture_data(root: Path) -> dict:
    """The page's data for the behavior tests: the registry fixture, plus runs, jobs, CI watches and a crew, all fixed."""
    t = time.time()
    make_handoff(root)
    (root / "projects/acme/old.md").write_text("# acme/old\n\n| Workstream | Owner | Milestone | Status | Next step | Notes |\n|---|---|---|---|---|---|\n"
                                               "| Underwriting | Platform | Pilot readiness | Running | Integration tests | Waiting on the bank |\n")
    reg = registry()
    reg["projects"].append({"id": "acme/old", "owner": "acme", "name": "old", "path": "/Users/t/Development/acme/old", "local": True, "github": "acme/old",
                            "main": {"branch": "main"}, "worktrees": [], "prs_open": [], "prs_known": True, "last_activity": int(NOW - 30 * 86400),
                            "note": "projects/acme/old.md", "note_summary": {"status": "active", "priority": "normal", "tasks": {}},
                            "status_line": {"ts": int(NOW - 30 * 86400), "agent": "claude", "line": SAVED, "branch": "release/1"}})
    d = dashboard.collect(reg, handoff=root, now=NOW)
    plan = lambda name, state, est, mins, owner="main", label="", persona="", after=(): {  # noqa: E731
        "name": name, "state": state, "est": est, "min": mins, "owner": owner, "label": label, "persona": persona, "after": list(after), "scope": "", "waiting": ""}
    d["runs"] = [
        {"id": "claude-rel", "agent": "claude", "session": "rel", "project": "acme/widget", "goal": "Release 26.11.3 (T-a1)", "state": "running", "mode": "guided",
         "host": "testmac", "step": 1, "steps": 5, "estimate": 180, "estimate_cal": 234, "started": t - 9600, "finished": None, "eta_min": 30, "eta_lo": 25, "eta_hi": 40,
         "active_min": 130, "wall_min": 160, "pace": 1.3, "updated": t - 240, "last": "the snapshot rebuild debounces registry builds", "next": "Trinity builds the screens",
         "plan": [plan("design", "done", 15, 14), plan("dashboard server", "running", 45, 34, "Tank: dashboard server", persona="Tank", after=["design"]),
                  plan("dashboard ui", "running", 60, 2, after=["dashboard server"]), plan("crew docs", "pending", 30, 0, label="Ghost"),
                  plan("release", "pending", 20, 0, after=["dashboard ui"])],
         "agents": [{"label": "Tank: dashboard server", "state": "running", "min": 34}, {"label": "Sati: dashboard screens", "state": "running", "min": 18},
                    {"label": "Sati: dashboard screens", "state": "running", "min": 3}, {"label": "Codex settings research", "state": "done", "min": 9},
                    {"label": "Neo2: graph engine", "state": "done", "min": 4}],
         "decisions": [{"id": "D2", "ts": t - 720, "question": "Keep the 2-minute reload? " + EVIL, "options": ["Keep it", "Drop it"], "recommend": "Keep it",
                        "irreversible": "", "from_name": "Tank"}],
         "questions": [{"id": "M9", "ts": t - 180, "from_name": "Sati", "text": "Which data needs do you take?"}], "updates": []},
        {"id": "codex-old", "agent": "codex", "session": "old", "project": "acme/api", "goal": "Migrate the importer for T-a4", "state": "running", "mode": "auto",
         "host": "testmac", "step": 1, "steps": 2, "estimate": 60, "started": t - 7200, "finished": None, "eta_min": 20, "eta_lo": 15, "eta_hi": 25, "active_min": 50,
         "wall_min": 120, "pace": 1.0, "updated": t - 47 * 60, "last": "", "next": "", "plan": [plan("map fields", "done", 30, 25), plan("import", "running", 30, 25)],
         "agents": [], "decisions": [], "questions": [], "updates": []}]
    d["jobs"] = [
        {"name": "doctor", "about": "Weekly health check of the whole setup", "schedule": "weekly, mon 09:00", "enabled": True, "installed": True, "notify": "failure",
         "timeout_min": 10, "next": int(t + 3 * 86400), "last": {"start": t - 86400, "end": t - 86400 + 41, "rc": 2, "duration": 41, "summary": "2 checks failed"},
         "state": "failed", "runs": 4, "failed": 1},
        {"name": "sync", "about": "Projects map, PRs, sizes", "schedule": "every 1 h", "enabled": True, "installed": True, "notify": "failure", "timeout_min": 20,
         "next": int(t - 2 * 3600), "last": {"start": t - 3 * 3600, "end": t - 3 * 3600 + 17, "rc": 0, "duration": 17, "summary": ""}, "state": "ok", "runs": 712, "failed": 0},
        {"name": "sleep", "about": "Lessons: decay, merge, forget", "schedule": "daily at 03:30", "enabled": True, "installed": True, "notify": "failure", "timeout_min": 15,
         "next": int(t + 3600), "last": {"start": t - 3600, "end": t - 3591, "rc": 0, "duration": 9, "summary": ""}, "state": "ok", "runs": 30, "failed": 0},
        {"name": "lessons", "about": "Weekly lesson review", "schedule": "weekly, fri 16:00", "enabled": False, "installed": True, "notify": "always", "timeout_min": 5,
         "next": None, "last": {"start": t - 5 * 86400, "end": t - 5 * 86400 + 2, "rc": 0, "duration": 2, "summary": ""}, "state": "off", "runs": 4, "failed": 0},
        {"name": "audit", "about": "Publishes the audit trail", "schedule": "daily at 05:00", "enabled": True, "installed": False, "notify": "failure", "timeout_min": 5,
         "next": int(t + 7200), "last": {"start": t - 7200, "end": t - 7199, "rc": 0, "duration": 1, "summary": ""}, "state": "ok", "runs": 2, "failed": 0},
        {"name": "gc", "about": "Reclaims disk safely", "schedule": "daily at 04:00", "enabled": True, "installed": True, "notify": "failure", "timeout_min": 30,
         "next": int(t + 5400), "last": None, "state": "never", "runs": 0, "failed": 0}]
    fail = {"id": 18562211, "run": "ci", "number": 14, "url": "https://github.com/acme/widget/actions/runs/18562211", "class": "test",
            "action": "a test failed. Reproduce it locally, fix it, then push once", "jobs": [{"name": "test", "step": "Run tests"}],
            "excerpt": EVIL + "\nFAILED tests/test_x.py::test_y - AssertionError: assert 2 == 3"}
    watch = lambda branch, sha, state, ago, fails=(): {"repo": "acme/widget", "branch": branch, "sha": sha, "state": state, "ts": int(t - ago), "minutes": 3.1,  # noqa: E731
                                                       "provider": "github", "error": None, "runs": [{"name": "ci", "number": 14, "status": "success" if state == "passed" else "failure"}],
                                                       "failures": list(fails), "class": fails[0]["class"] if fails else "", "action": "", "first_error": ""}
    d["ci"] = {"watches": [watch("feat/x", "4ca6703", "failed", 720, [fail]), watch("release/26.11.2", "5f1bb6b", "passed", 7200),
                           watch("feat/x", "1111111", "passed", 9000), dict(watch("main", "45ec3e4", "none", 3600), repo="acme/api", runs=[]),
                           watch("old/branch", "abcdef0", "passed", 20 * 86400)],
               "complete": True, "total": 5, "watching": [{"key": "widget-main", "sha": "d11f175", "pid": 4242, "since": int(t - 180)}]}
    member = lambda i, n, t, tools="all", tier="standard", main=False: {"id": i, "name": n, "title": t, "tier": tier, "tools": tools, "summary": f"What {n} does.", "main": main}  # noqa: E731
    d["crew"] = {"members": [member("morpheus", "Morpheus", "Tech lead", tier="strong", main=True), member("tank", "Tank", "Backend engineer"),
                             member("sati", "Sati", "UX designer", "build", "strong"), member("trinity", "Trinity", "Frontend engineer"),
                             member("ghost", "Ghost", "Technical writer", "build"), member("seraph", "Seraph", "Code reviewer", "review", "strong"),
                             member("link", "Link", "Release engineer"), member("sparks", "Sparks", "Lookup scout", "read", "fast")],
                 "activities": [{"id": "plan", "name": "Planning, estimates, status and integration", "R": ["morpheus"], "A": "morpheus", "C": [], "I": []},
                                {"id": "ux", "name": "UX and visual design", "R": ["sati"], "A": "sati", "C": ["trinity"], "I": ["morpheus"]},
                                {"id": "backend", "name": "Back-end services and APIs", "R": ["tank"], "A": "seraph", "C": ["sati"], "I": ["trinity"]},
                                {"id": "docs", "name": "Documentation", "R": ["ghost"], "A": "ghost", "C": ["tank"], "I": []}]}
    for x in d["people"]:   # a run on another machine reaches this Mac only through presence
        if x["who"] == "bob@studio":
            x["sessions"][0]["progress"] = {"goal": "Reconcile ledgers", "state": "running", "step": 1, "steps": 3, "estimate": 40, "started": int(t - 1200),
                                            "eta_min": 25, "active_min": 18, "wall_min": 20, "plan": [], "agents": [], "last": "matching rows", "next": "", "updated": int(t - 300)}
    return d


@unittest.skipUnless(CHROME, NO_CHROME)
class PageBehaviorTests(unittest.TestCase):
    """The page's behavior from fixture data, in headless Chromium: what each view shows, the honest states, role-first names,
    the navigation at every width, the drawers, and the live states."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.data = fixture_data(Path(cls.tmp.name) / "handoff")
        cls.page = dashboard.page(cls.data)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def probe(self, steps, width=1440, hash_="#/overview", budget=6000, page=None):
        out = browse(page or self.page, steps, width, hash_, budget)
        self.assertIsNone(out.get("probeError"), out.get("probeError"))
        self.assertIsNone(out.get("pageError"), out.get("pageError"))
        return out

    def variant(self, change):
        # the fixture's page with one change to a copy of its data
        d = json.loads(json.dumps(self.data))
        change(d)
        return dashboard.page(d)

    def test_a_javascript_url_in_the_data_is_never_opened(self):
        """A PR's url is data: the search result and the command palette open it only through safeUrl, so a
        javascript: URL opens nothing, also in the static export where no CSP applies."""
        def bad(d):
            next(p for p in d["projects"] if p["id"] == "acme/widget")["prs_open"][0]["url"] = "javascript:alert(1)"
        out = self.probe("""const opened = []; window.open = u => { opened.push(String(u)); return null; };
            await go("#/search?q=PR%2022"); $$("main .results .res").find(b => text(b).includes("#22")).click();
            await go("#/search?q=PR%2017"); $$("main .results .res").find(b => text(b).includes("#17")).click();
            document.dispatchEvent(new KeyboardEvent("keydown", {key: "k", metaKey: true, bubbles: true})); await tick(100);
            const inp = $("#palette input"); inp.value = "#22"; inp.dispatchEvent(new Event("input", {bubbles: true})); await tick(100);
            $$("#pal-list li").find(li => text(li).includes("#22")).click(); await tick(100);
            out.opened = opened;""", page=self.variant(bad))
        self.assertEqual(out["opened"], ["https://github.com/acme/widget/pull/17"])  # the safe one opens; javascript: never

    def test_background_work_puts_what_waits_for_you_first(self):
        out = self.probe("""out.sections = $$("main section[id]").map(s => s.id); out.waiting = text($("#waiting")); out.runs = $$("#runs .rcard").map(text);
            out.ci = $$("#ci .cirow").map(text); out.more = text($("#ci .pb > .btn")); out.jobs = $$("#jobs tbody tr").map(text); out.imgs = $$("img").length;
            out.figs = $$(".stile .s-l").map(text); out.dot = $('#tabs [data-view="background"] .n').getAttribute("aria-label");""", hash_="#/background")
        self.assertEqual(out["sections"], ["waiting", "runs", "ci", "jobs"])
        self.assertIn("Keep the 2-minute reload? " + EVIL, out["waiting"])                      # agent text is text, never HTML
        self.assertEqual(out["imgs"], 0)
        self.assertIn("Backend engineer (Tank) asks · Options: Keep it, Drop it · Recommended: Keep it", out["waiting"])
        self.assertEqual(out["figs"], ["Waiting for you", "Runs", "CI failures, 14 days", "Scheduled jobs"])
        self.assertIn("1 decision waiting for you", out["dot"])
        rel_, lost = out["runs"][0], out["runs"][1]
        self.assertIn("Blocked · waiting for you", rel_)
        self.assertIn("“dashboard ui” is running before “dashboard server” finished.", rel_)
        self.assertIn("Two UX designer (Sati) lanes run “dashboard screens”.", rel_)
        self.assertIn("dashboard server · Backend engineer (Tank)", rel_)
        self.assertIn("Stopped reporting", lost)
        self.assertIn("No update for 47 min and its session is not live.", lost)
        self.assertTrue(out["ci"][0].startswith("Failed · widget · feat/x @ 4ca6703"), out["ci"][0])
        self.assertIn("ci #14 failed in “test”, step “Run tests” · Test failure", out["ci"][0])
        self.assertIn("FAILED tests/test_x.py::test_y", out["ci"][0])
        self.assertTrue(out["ci"][1].startswith("Watching · widget · main @ d11f175"), out["ci"][1])
        self.assertEqual(sum("feat/x" in r for r in out["ci"]), 1)                                # the latest result per branch
        self.assertEqual(out["more"], "Show 1 older result")
        jobs = out["jobs"]
        self.assertTrue(jobs[0].startswith("Failed (exit 2) doctor"), jobs[0])
        self.assertIn("Mondays at 09:00", jobs[0])
        self.assertEqual([j.split(" ")[0] for j in jobs[1:]], ["Not", "Overdue", "OK", "No", "Off"])
        self.assertIn("by ", jobs[2])                                                            # an interval's next run is an upper bound

    def test_navigation_never_scrolls_sideways_and_every_view_stays_reachable(self):
        views = ["overview", "tasks", "background", "team", "projects", "initiatives", "activity", "explorer", "health"]
        steps = """out.row = $$("#tabs .tab").map(a => a.getAttribute("data-view")); out.menu = $$("#moremenu a").map(a => a.getAttribute("data-view"));
            out.tabs = [$("#tabs").scrollWidth, $("#tabs").clientWidth]; out.page = [document.documentElement.scrollWidth, document.documentElement.clientWidth];
            out.more = text($("#morebtn")); out.tabRoles = $$("[role=tab]").filter(el => !el.closest(".ptabs")).length; out.current = $('nav[aria-label="Views"] [aria-current="page"]').getAttribute("data-view");"""
        for width in (320, 390, 768, 1024, 1180, 1440, 1600):
            out = self.probe(steps, width, "#/health")
            self.assertEqual(out["row"] + out["menu"], views, width)                               # every view, in order, once
            self.assertLessEqual(out["tabs"][0], out["tabs"][1], width)
            self.assertLessEqual(out["page"][0], out["page"][1], width)
            self.assertEqual((out["tabRoles"], out["current"]), (0, "health"), width)
            if width >= 1440:
                self.assertEqual(out["menu"], [], width)
            if out["menu"]:
                self.assertEqual(out["more"], "Health", width)                                  # the current view inside More names itself
        out = self.probe(steps, 390, "#/overview")
        self.assertEqual((out["row"][:3], out["menu"][-1], out["more"]), (["overview", "tasks", "background"], "health", "More"))

    def test_the_more_menu_works_with_the_keyboard(self):
        out = self.probe("""const b = $("#morebtn"); out.menu = $$("#moremenu a").map(a => a.getAttribute("data-view")); b.focus(); key(b, "ArrowDown"); out.expanded = b.getAttribute("aria-expanded");
            out.first = document.activeElement.getAttribute("data-view"); key(document.activeElement, "End"); out.last = document.activeElement.getAttribute("data-view");
            key(document.activeElement, "ArrowDown"); out.wrap = document.activeElement.getAttribute("data-view");
            key(document.activeElement, "Escape"); out.back = document.activeElement.id; out.closed = $("#moremenu").hidden;
            key(b, "ArrowDown"); document.activeElement.click(); await tick(200); out.hash = location.hash; out.menuOpen = !!$("#moremenu") && !$("#moremenu").hidden;""", 390)
        first = out["menu"][0]
        self.assertEqual((out["expanded"], out["first"], out["last"], out["wrap"]), ("true", first, "health", first))
        self.assertEqual((out["back"], out["closed"]), ("morebtn", True))
        self.assertTrue(out["hash"].startswith("#/" + first), out["hash"])
        self.assertFalse(out["menuOpen"])

    def test_no_data_and_no_recent_activity_are_never_on_track(self):
        out = self.probe("""out.dots = Object.fromEntries($$(".ptile").map(t => [text($(".pt-n .strong", t)), $(".pt-h .dot", t).getAttribute("aria-label")]));
            await go("#/overview"); out.tile = text($$(".stile")[2]);""", 1440, "#/projects")
        self.assertEqual(out["dots"], {"widget": "Off track", "api": "No data", "old": "Quiet"})
        self.assertIn("1 quiet", out["tile"])
        self.assertIn("1 no data", out["tile"])
        self.assertNotIn("on track", out["tile"])

    def test_contradictions_are_flagged_where_they_occur(self):
        out = self.probe("""out.project = text($(".phead")); await go("#/health"); out.health = $$("section[aria-label=Contradictions] li").map(text);""", 1440, "#/projects/acme/old")
        self.assertIn("The latest status was saved on release/1; git is on main now. It may be out of date.", out["project"])     # C3
        flags = " | ".join(out["health"])
        for text in ("Done, but still claimed by bob@studio.", "The goal names T-a4, which is done.", "No update for 47 min and its session is not live.",
                     "“dashboard ui” is running before “dashboard server” finished."):
            self.assertIn(text, flags)

    def test_the_crew_and_who_does_what_name_members_role_first(self):
        out = self.probe("""out.working = $$("#tabpanel .prows li").map(text); out.roster = $$("table.crew tbody tr").map(text); out.tabs = $$(".ptab").map(text);
            await go("#/team?tab=raci&rm=tank"); out.sum = text($("#tabpanel .lead")); out.rows = $$("table.raci tbody th").map(text);
            out.me = $$("table.raci .me").map(text); await go("#/team?tab=raci&rm=tank&rall=1"); out.all = $$("table.raci tbody th").length;""", 1440, "#/team?tab=crew")
        self.assertEqual(out["tabs"], ["People", "Crew 8", "Who does what 4"])
        self.assertTrue(out["working"][0].startswith("■ Backend engineer (Tank) dashboard server"), out["working"])
        self.assertIn("Waiting for you · D2", out["working"][0])
        self.assertEqual([w[:20] for w in out["working"][1:]], ["▸ UX designer (Sati)"] * 2)
        self.assertTrue(out["roster"][0].startswith("Tech lead (Morpheus)"), out["roster"][0])
        self.assertIn("Leads Release 26.11.3 (T-a1)", out["roster"][0])
        self.assertEqual(out["sum"], "Backend engineer (Tank) does 1 kind of work, approves 0, is consulted on 1 and is informed about 0.")
        self.assertEqual(out["rows"], ["Back-end services and APIs", "Documentation"])
        self.assertEqual(out["me"], ["Backend engineer (Tank)", "Backend engineer (Tank)"])
        self.assertEqual(out["all"], 4)

    def test_the_run_drawer_shows_each_step_with_its_owner_and_what_waits_for_you(self):
        out = self.probe("""const m = $("#modal"); out.open = m.open; out.title = text($("#modal-title")); out.steps = $$("ol.steps", m)[0] ? $$("li", $$("ol.steps", m)[0]).map(text) : [];
            out.crew = $$("li", $$("ol.steps", m)[1]).map(text); out.waiting = text($("#waiting-for-you")); out.notice = text($(".notice", m)); out.flags = $$(".callout", m).map(text);""",
                         1440, "#/background?modal=" + "run:claude-rel")
        self.assertTrue(out["open"])
        self.assertEqual(out["title"], "Release 26.11.3 (T-a1)")
        self.assertIn("dashboard server · Backend engineer (Tank) Running · 34m of ~45m", out["steps"][1])
        self.assertIn("crew docs · Technical writer (Ghost)", out["steps"][3])
        self.assertIn("Assigned to Technical writer (Ghost), not started.", out["steps"][3])
        self.assertIn("Waits for ▸ dashboard ui", out["steps"][4])
        self.assertIn("Keep it (recommended) · Drop it", out["waiting"])
        self.assertIn("Asked by Backend engineer (Tank)", out["waiting"])
        self.assertTrue(any("Done: Subagent: Codex settings research Done · 9m" in c for c in out["crew"]), out["crew"])
        self.assertTrue(any("Done: Neo2 (not in the crew)" in c for c in out["crew"]), out["crew"])
        self.assertIn("A static copy from", out["notice"])
        self.assertIn("“dashboard ui” is running before “dashboard server” finished.", " ".join(out["flags"]))

    def test_the_job_and_ci_drawers_say_why_it_failed_and_what_to_do(self):
        out = self.probe("""await go("#/background?modal=job%3Adoctor"); out.job = text($("#modal .pline:last-of-type")); out.jobBody = text($("#modal .mbody")); out.jobFoot = text($("#modal .mf"));
            await go("#/background?modal=job%3Async"); out.sync = text($("#modal .callout"));
            await go("#/background?modal=ci%3Aacme%2Fwidget%40feat%2Fx"); out.ci = text($("#modal .mbody")); out.ciFoot = text($("#modal .mf")); out.imgs = $$("img").length;""", 1440, "#/background")
        self.assertEqual(out["job"], "Failed (exit 2) Mondays at 09:00 On")
        for text in ("Failures, 30 days 1 of 4", "The whole job: its runs and log needs the live dashboard", "Run it now dev jobs run doctor", "Turn it off dev jobs disable doctor"):
            self.assertIn(text, out["jobBody"])
        self.assertEqual(out["jobFoot"], "Copy dev jobs run doctor")
        self.assertIn("Overdue It last ran 3 h ago and runs every 1 h. The Mac may have been asleep, or launchd stopped it.", out["sync"])
        for text in ("ci #14 failed Test failure", "Failed job test, step “Run tests”", EVIL + " FAILED tests/test_x.py::test_y",
                     "Next A test failed. Reproduce it locally, fix it, then push once.", "Hand it to Release engineer (Link)"):
            self.assertIn(text, out["ci"])
        self.assertEqual((out["ciFoot"], out["imgs"]), ("Open the log on GitHub Copy dev run view 18562211 --failed", 0))

    def test_a_drawer_opens_from_its_row_traps_focus_and_esc_returns_it(self):
        out = self.probe("""const a = $("#runs .rcard .rh a"); a.focus(); a.click(); await tick(200); out.open = $("#modal").open; out.focus = document.activeElement.getAttribute("aria-label");
            out.inside = $("#modal").contains(document.activeElement); const cs = getComputedStyle($("#modal")); out.side = [cs.right, cs.top, Math.round(parseFloat(cs.height)) === innerHeight, parseFloat(cs.width) <= 760];
            key(document.activeElement, "Escape"); await tick(200); out.closed = !$("#modal").open; out.back = document.activeElement === a; out.hash = location.hash;""", 1440, "#/background")
        self.assertEqual((out["open"], out["focus"], out["inside"], out["side"]), (True, "Close (Esc)", True, ["0px", "0px", True, True]))   # a right-hand drawer, full height, at most 760 px
        self.assertEqual((out["closed"], out["back"], out["hash"]), (True, True, "#/background"))

    def test_a_drawer_for_a_record_that_is_gone_says_so(self):
        out = self.probe("""out.run = text($("#modal-title")); out.runBody = text($("#modal .mbody")); await go("#/background?modal=job%3Anope"); out.job = text($("#modal-title"));
            await go("#/tasks?task=T-nope"); out.task = text($("#modal-title")); out.taskBody = text($("#modal .mbody")); out.foot = text($("#modal .mf"));""", 1440, "#/background?modal=run%3Anope")
        self.assertEqual((out["run"], out["job"], out["task"]), ("Run nope no longer exists", "Job nope no longer exists", "Task T-nope no longer exists"))
        self.assertEqual(out["runBody"], "Runs live on the machine that ran them, and a finished run leaves this page after an hour.")
        self.assertEqual(out["taskBody"], "It may have been deleted or moved to another project. The lists update on their own.")
        self.assertEqual(out["foot"], "Close")

    def test_brackets_move_to_the_next_and_previous_record_of_the_list(self):
        out = self.probe("""const keys = $$("#view [data-open^='task:']").map(e => e.getAttribute("data-open")); out.keys = keys.slice(0, 2);
            location.hash = "#/tasks?task=" + keys[0].slice(5); await tick(200); key(document.activeElement, "]"); await tick(200); out.next = location.hash;
            key(document.activeElement, "["); await tick(200); out.back = location.hash; out.open = $("#modal").open;""", 1440, "#/tasks")
        first, second = (k[len("task:"):] for k in out["keys"])
        self.assertIn("task=" + second, out["next"])
        self.assertIn("task=" + first, out["back"])
        self.assertTrue(out["open"])

    def test_the_connected_task_answers_first_and_says_how_each_link_was_found(self):
        out = self.probe("""const m = $("#modal"); out.dts = $$(".ans dt", m).map(text); out.ans = $$(".ans dd", m).map(text); out.conns = $$(".conn", m).map(text);
            out.notFound = text($(".msec .tnote", m)); out.notice = text($(".notice", m));
            await go("#/tasks?task=T-a4"); out.flags = $$("#modal .callout").map(text);""", 1440, "#/tasks?task=T-a1")
        self.assertEqual(out["dts"], ["Who has it", "What blocks it", "What's next"])
        self.assertTrue(out["ans"][0].startswith("me@testmac with Claude Code, since"), out["ans"][0])
        self.assertIn("The lock ends in", out["ans"][0])
        self.assertIn("Tech lead (Morpheus) works on it in “Release 26.11.3 (T-a1)”.", out["ans"][0])
        self.assertIn("Waiting for your answer to D2 in “Release 26.11.3 (T-a1)”.", out["ans"][1])
        self.assertIn("Trinity builds the screens", out["ans"][2])
        self.assertTrue(any(c.startswith("Run Release 26.11.3 (T-a1)") and "Found: Its goal or a step has the id." in c for c in out["conns"]), out["conns"])
        self.assertEqual(out["notFound"], "Not found: a branch, a pull request or a plan.")
        self.assertIn("A static copy from", out["notice"])
        self.assertIn("Done, but still claimed by bob@studio.", " | ".join(out["flags"]))                                   # C1
        self.assertIn("The goal of the run “Migrate the importer for T-a4” names T-a4, which is done.", " | ".join(out["flags"]))  # C11

    def test_the_static_copy_says_it_is_static(self):
        out = self.probe("""out.live = text($("#live")); out.dot = !!$("#live .dot"); out.banner = $("#banner").hidden; $("#live").click(); out.pop = text($("#livepop"));""")
        self.assertTrue(out["live"].startswith("Generated "), out["live"])
        self.assertEqual((out["dot"], out["banner"]), (False, True))
        self.assertIn("A static copy It does not update. Run dev dashboard for the live page.", out["pop"])

    def test_every_view_fits_a_phone_and_tables_keep_their_words(self):
        out = self.probe("""out.w = {}; for (const hs of ["#/overview", "#/tasks", "#/background", "#/team", "#/team?tab=crew", "#/team?tab=raci", "#/projects", "#/initiatives", "#/activity", "#/health", "#/explorer?f=projects/acme/old.md"]) {
              await go(hs); out.w[hs] = document.documentElement.scrollWidth; }
            out.cells = $$(".md td").map(td => td.getBoundingClientRect().height); out.line = parseFloat(getComputedStyle($(".md td")).lineHeight);""", 390, budget=9000)
        for view, w in out["w"].items():
            self.assertLessEqual(w, 390, view)
        self.assertLess(max(out["cells"]), 3 * out["line"] + 20)                                 # words wrap whole, never letter by letter

    def test_a_run_whose_crew_reports_is_running_though_its_lead_was_quiet(self):
        # the live run of 2026-11-06: the lead's last update 47 min ago, a lane reported 3 min ago, the session not in presence
        def change(d):
            d["runs"][1].update(seen=int(time.time() - 180), agents=[{"label": "Neo: importer mapping", "state": "running", "min": 20}])
        out = self.probe("""out.cards = $$("#runs .rcard").map(text); out.dot = $('#tabs [data-view="background"] .n').getAttribute("aria-label");
            await go("#/health"); out.flags = $$("section[aria-label=Contradictions] li").map(text);""", hash_="#/background", page=self.variant(change))
        card = next(c for c in out["cards"] if c.startswith("Migrate the importer"))
        self.assertTrue(card.startswith("Migrate the importer for T-a4 Running · 15m–25m left"), card)
        self.assertNotIn("Stopped reporting", card + out["dot"])
        self.assertFalse([f for f in out["flags"] if "No update for" in f], out["flags"])

    def test_of_about_is_the_plans_current_total_beside_the_time_left(self):
        # the live card read "4h09m active of about 26m" beside "3h14m–5h04m left": 26m was the first estimate, calibrated
        def change(d):
            d["runs"][1].update(seen=int(time.time() - 180), active_min=256, wall_min=256, eta_min=236, eta_lo=189, eta_hi=295.2,
                                estimate=60, estimate_cal=25.8, total_min=492)
        out = self.probe("""out.card = text($$("#runs .rcard").find(c => text(c).startsWith("Migrate the importer"))); await go("#/background?modal=run%3Acodex-old"); out.figs = text($("#modal .mfigs"));""",
                         hash_="#/background", page=self.variant(change))
        self.assertIn("4h16m active of about 8h10m", out["card"])                               # 256 active + 236 left, to 5 minutes
        self.assertIn("3h10m–4h55m left", out["card"])
        self.assertNotIn("of about 25m", out["card"] + out["figs"])
        self.assertIn("Time 4h16m active of about 8h10m first estimate 25m", out["figs"])
        self.assertIn("Left 3h10m–4h55m", out["figs"])

    def test_estimates_round_to_five_minutes_and_measured_times_stay_exact(self):
        # "of about 1h59m" claimed a precision no estimate has: an estimate or a time left rounds to 5 minutes, under 10 minutes
        # to the minute; active, wall and elapsed time are measured and stay exact
        def change(d):
            d["runs"][1].update(seen=int(time.time() - 180), active_min=84, wall_min=84, total_min=119, eta_min=35, eta_lo=27.4, eta_hi=43,
                                estimate=60, estimate_cal=156)
            d["runs"][1]["plan"][1].update(est=12, min=7)
            d["runs"][0].update(eta_min=5, eta_lo=3.2, eta_hi=7.4)
        out = self.probe("""out.card = text($$("#runs .rcard").find(c => text(c).startsWith("Migrate the importer")));
            await go("#/background?modal=run%3Acodex-old"); out.figs = text($("#modal .mfigs")); out.steps = $$("#modal ol.steps li").map(text);
            await go("#/background?modal=run%3Aclaude-rel"); out.short = text($("#modal .mfigs"));""", hash_="#/background", page=self.variant(change))
        self.assertIn("1h24m active of about 2h00m", out["card"])                               # 119 → 120; 84 measured, not 85
        self.assertIn("25m–45m left", out["card"])                                              # 27.4 → 25, 43 → 45
        self.assertIn("7m of ~10m", out["card"])                                                # the step's 12-minute estimate
        self.assertIn("Time 1h24m active of about 2h00m first estimate 2h35m", out["figs"])     # 156 → 155
        self.assertIn("Left 25m–45m", out["figs"])
        self.assertTrue(any("7m of ~10m" in s for s in out["steps"]), out["steps"])
        self.assertIn("Left 3m–7m", out["short"])                                               # under 10 minutes: to the minute

    def test_lists_show_what_a_step_does_and_never_its_code(self):
        def change(d):
            plan, lanes = d["runs"][0]["plan"], d["runs"][0]["agents"]
            plan[1].update(name="F4 caller identity", title="knowing who ran each command", owner="Tank: F4 caller identity")
            plan[2]["after"] = ["F4 caller identity"]
            plan[3]["name"] = "K1 protect devkit state"                                          # no title: its name without the code
            lanes[0]["label"] = "Tank: F4 caller identity"
        out = self.probe("""out.card = text($("#runs .rcard")); await go("#/team?tab=crew"); out.working = $$("#tabpanel .prows li").map(text);
            await go("#/background?modal=run%3Aclaude-rel"); const m = $("#modal"); out.steps = $$("li", $$("ol.steps", m)[0]).map(text);
            out.codes = $$("ol.steps .sc", m).map(text); out.crew = $$("li", $$("ol.steps", m)[1]).map(text);""", hash_="#/background", page=self.variant(change))
        self.assertIn("▸ Running: knowing who ran each command · Backend engineer (Tank)", out["card"])
        self.assertIn("○ To do: protect devkit state · Technical writer (Ghost)", out["card"])
        self.assertIn("“dashboard ui” is running before “knowing who ran each command” finished.", out["card"])
        self.assertTrue(out["working"][0].startswith("■ Backend engineer (Tank) knowing who ran each command · "), out["working"][0])
        for shown in [out["card"]] + out["working"] + out["crew"]:
            self.assertIsNone(re.search(r"\b(F4|K1)\b", shown), shown)                              # codes stay with the crew
        self.assertIn("knowing who ran each command", out["steps"][1])
        self.assertEqual(out["codes"], ["F4 caller identity", "K1"])                             # the drawer keeps them, dim

    def test_decisions_say_the_tech_lead_records_the_answer(self):
        out = self.probe("""out.waiting = text($("#waiting")); await go("#/background?modal=run%3Aclaude-rel"); out.drawer = text($("#waiting-for-you"));""",
                         hash_="#/background")
        self.assertIn("answer in that session; Tech lead (Morpheus) records it", out["waiting"])
        self.assertIn("Answer in the session; Tech lead (Morpheus) records it.", out["drawer"])

    def test_a_step_that_stopped_without_evidence_waits_for_the_tech_lead_in_amber(self):
        # contract C3: a lane that stops without evidence leaves its step unverified; Morpheus closes or reopens it
        def change(d):
            d["runs"][0]["plan"][3].update(state="unverified", owner="Ghost: crew docs", persona="Ghost")
            d["runs"][0]["agents"].append({"label": "Ghost: crew docs", "state": "unverified", "min": 12})
        out = self.probe("""const w = document.createElement("span"); w.className = "warn-t"; document.body.append(w); out.amber = getComputedStyle(w).color;
            const row = $$("#runs .rcard .rsteps li").find(li => text(li).includes("crew docs")); out.row = text(row); out.cls = row && row.className; out.color = row && getComputedStyle($(".g", row)).color;
            await go("#/team?tab=crew"); const g = $$("#tabpanel .prows li").find(li => text(li).includes("Ghost")); out.working = text(g) || ""; out.wcolor = g && getComputedStyle($(".g", g)).color;
            await go("#/background?modal=run%3Aclaude-rel"); const m = $("#modal"); out.step = text($$("li", $$("ol.steps", m)[0])[3]); out.lane = $$("li", $$("ol.steps", m)[1]).map(text).find(t => t.includes("Ghost"));
            await go("#/actions"); out.needs = text($("main"));""", hash_="#/background", page=self.variant(change))
        self.assertEqual(out["row"], "! Tech lead (Morpheus) checks it: crew docs · Technical writer (Ghost) Tech lead (Morpheus) checks it")
        self.assertEqual((out["cls"], out["color"], out["wcolor"]), ("unverified", out["amber"], out["amber"]))
        self.assertIn("Technical writer (Ghost) crew docs", out["working"])
        self.assertIn("Tech lead (Morpheus) checks it", out["working"])
        self.assertIn("Tech lead (Morpheus) checks it", out["step"])
        self.assertIn("Tech lead (Morpheus) checks it", out["lane"])
        for shown in (out["row"], out["working"], out["step"], out["lane"]):
            self.assertNotIn("Blocked", shown)
        self.assertNotIn("crew docs", out["needs"])                                              # Morpheus checks it; nothing asks the developer

    @staticmethod
    def with_branches(d):
        # acme/widget's branch map: backed up, only on this Mac, embargoed, the default branch, and five merged branches
        t = time.time()
        w = next(p for p in d["projects"] if p["id"] == "acme/widget")
        merged = [branch_row(f"fix/old-{i}", "merged", "its commits are in main", merged_into="main", last_commit=int(t - (10 + i) * 86400)) for i in range(5)]
        merged[0]["pr"] = {"number": 30, "state": "merged", "draft": False, "checks": None, "url": "javascript:alert(1)", "title": "Old fix"}
        w["branches"] = [
            branch_row("release/1", "backed_up", current=True, ahead=2, base_ahead=12, last_backup=int(t - 240),
                       pr={"number": 41, "state": "open", "draft": True, "checks": "passing", "url": "https://github.com/acme/widget/pull/41", "title": "Release 1"}),
            branch_row("feat/x", "not_backed_up", "3 commits only on this Mac", where="local only", upstream=None, unpushed=3, oldest_unpushed=int(t - 7200),
                       base_ahead=3, worktree="~/Development/.worktrees/acme/widget/feat-x", session=True, task={"id": "T-a1", "title": "Ship export", "status": "doing"}),
            branch_row("release/x", "embargoed", 'embargoed until "guard refusal rules"', where="local only", upstream=None, unpushed=2, base_ahead=2,
                       embargo={"why": "keeps the guard fix off the public remote", "until": "guard refusal rules", "who": "link", "when": "2026-11-06T10:00:00Z"}),
            branch_row("main", "on_remote", default=True)] + merged

    def test_the_branches_tab_shows_where_each_branch_lives_and_whether_it_is_backed_up(self):
        out = self.probe("""await go("#/projects/acme/widget?tab=branches"); const box = $("#branches");
            out.cols = $$("thead th", box).map(text); out.head = text($(".ph", box)); out.tab = text($("#tab-branches"));
            out.rows = $$("tbody tr", box).map(text); out.quiet = $$("tbody tr.quiet", box).map(tr => text($("th", tr)));
            out.links = $$("tbody a[target=_blank]", box).map(a => [a.textContent, a.getAttribute("href")]);
            const more = $$("button", box).find(b => text(b).startsWith("Show")); out.more = text(more); more.focus(); more.click(); await tick(100);
            out.after = $$("#branches tbody tr").length; out.focusIn = $("#branches").contains(document.activeElement);
            await go("#/projects/acme/widget"); out.facts = text($$(".kv dt").find(dt => text(dt) === "Branches").nextElementSibling);
            await go("#/projects?layout=table"); out.cell = text($$("table.pt tbody tr").find(tr => text(tr).includes("widget")).querySelector("td.c-bk"));
            await go("#/projects?layout=cards"); out.tile = text($$(".ptile").find(x => text(x).includes("widget")));""", page=self.variant(self.with_branches))
        self.assertEqual(out["cols"], ["Backup", "Branch", "Where", "Ahead / behind", "Pull request", "Worktree"])
        self.assertEqual(out["head"], "Branches 9 1 not backed up 1 embargoed")                 # a static copy: the navbar says when it was made
        self.assertEqual(out["tab"], "Branches 9")
        rel1, featx, relx, main = out["rows"][:4]
        self.assertEqual(rel1, "Backed up 4 min ago release/1current local + origin ↑2 ↓0 12 ahead of main #41 draft checks passing main checkout")
        self.assertEqual(featx, "Not backed up 3 commits only on this Mac, the oldest 2 h ago. feat/x T-a1 Ship export local only ↑3 3 ahead of main — feat-x live session")
        self.assertEqual(relx, "Embargoed Until “guard refusal rules” Release engineer (Link): keeps the guard fix off the public remote release/x local only ↑2 2 ahead of main — —")
        self.assertEqual(main, "On the remote main local + origin ↑0 ↓0 default branch — —")
        self.assertEqual(out["quiet"], ["fix/old-0", "fix/old-1", "fix/old-2"])                 # merged: quiet, last, the first three
        self.assertTrue(out["rows"][4].startswith("Merged into main fix/old-0 local + origin"), out["rows"][4])
        self.assertEqual(out["more"], "Show 2 more merged branches")
        self.assertEqual((out["after"], out["focusIn"]), (9, True))
        self.assertEqual(out["links"], [["#41", "https://github.com/acme/widget/pull/41"], ["#30", "#"]])   # a javascript: URL is never a link
        self.assertEqual(out["facts"], "9 branches, 1 not backed up, 1 embargoed")
        self.assertEqual(out["cell"], "1")
        self.assertIn("acme · 1 not backed up", out["tile"])

    def test_the_branch_map_says_when_it_is_missing_empty_without_a_remote_or_without_gh(self):
        def change(d):
            w = next(p for p in d["projects"] if p["id"] == "acme/widget")
            w.update(remote="", github=None, branches=[branch_row("main", "no_remote", "no remote is configured", default=True, current=True,
                                                                  where="local only", upstream=None, unpushed=4)])
            old = next(p for p in d["projects"] if p["id"] == "acme/old")
            old.update(prs_known=False, github="acme/old", remote="https://github.com/acme/old.git", branches=[branch_row("main", default=True, current=True)])
            for pid, main, rows in (("acme/broken", {"branch": "main"}, []), ("acme/fresh", {}, []), ("acme/legacy", {"branch": "main"}, None)):
                d["projects"].append(dict(old, id=pid, name=pid.split("/")[1], main=main, branches=rows, prs_known=True, github=None))
        out = self.probe("""const view = async id => { await go("#/projects/" + id + "?tab=branches"); return text($("#branches")); };
            for (const id of ["acme/widget", "acme/old", "acme/broken", "acme/fresh", "acme/legacy", "acme/api"]) out[id] = await view(id);
            await go("#/projects?layout=table"); out.cells = Object.fromEntries($$("table.pt tbody tr").map(tr => [text($(".pname a", tr)), text($("td.c-bk", tr))]));""",
                         page=self.variant(change))
        self.assertIn("No remote Every branch exists only on this Mac.", out["acme/widget"])
        self.assertIn("Not backed up No remote is configured. main", out["acme/widget"])
        self.assertIn("Pull requests not known gh did not answer", out["acme/old"])
        self.assertIn("git listed no branches The checkout is on main, but the map came back empty.", out["acme/broken"])
        self.assertIn("No branches yet The repository has no commits.", out["acme/fresh"])
        self.assertIn("No branch map in this copy", out["acme/legacy"])
        self.assertIn("No branches on this Mac", out["acme/api"])
        self.assertIn("dev clone acme/api", out["acme/api"])
        self.assertEqual(out["cells"], {"widget": "1", "api": "—", "old": "0", "broken": "—", "fresh": "0", "legacy": "—"})   # unknown is —, never 0

    def test_the_branch_map_stacks_on_a_phone_without_scrolling_sideways(self):
        out = self.probe("""await go("#/projects/acme/widget?tab=branches"); const tr = $("#branches tbody tr");
            out.first = text(tr.firstElementChild.tagName === "TH" ? tr.firstElementChild : $("th", tr)); out.firstShown = getComputedStyle($("th", tr)).order;
            out.row = text(tr); out.page = [document.documentElement.scrollWidth, document.documentElement.clientWidth];
            out.rowW = [tr.getBoundingClientRect().right, document.documentElement.clientWidth];""", width=390, page=self.variant(self.with_branches))
        self.assertLessEqual(out["page"][0], out["page"][1])
        self.assertLessEqual(out["rowW"][0], out["rowW"][1])
        self.assertEqual(out["firstShown"], "-1")                                                # the name leads the stacked row
        self.assertIn("Where local + origin", out["row"])                                       # each value says what it is
        self.assertIn("Pull request #41 draft", out["row"])


class LivePageTests(unittest.TestCase):
    """The live page against a small stand-in server: the session-ended state, and an update that arrives while a drawer is open."""

    def serve(self, routes):
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                status, ctype, body = routes(self.path)
                if not status:              # a server that stopped: the connection closes without an answer
                    self.close_connection = True
                    return
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "handoff"
        cls.data = dict(fixture_data(cls.root), live=True, snapshot=1)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def page(self, steps):
        return dashboard.page(self.data).replace("</body>", PROBE % steps + "</body>").encode()

    @unittest.skipUnless(CHROME, NO_CHROME)
    def test_a_401_ends_the_session_and_says_how_to_sign_in_again(self):
        steps = """await tick(1500); out.word = text($("#live")); out.label = $("#live").getAttribute("aria-label"); out.banner = text($("#banner"));"""
        problem = json.dumps({"type": "about:blank", "title": "Unauthorized", "status": 401}).encode()
        base = self.serve(lambda path: (200, "text/html", self.page(steps)) if path == "/" else (401, "application/problem+json", problem))
        out = chrome(base + "/#/overview", 1440)
        self.assertEqual((out["word"], out["label"]), ("Session ended", "Live updates: session ended"))
        self.assertIn("This page is no longer signed in.", out["banner"])
        self.assertIn("Run dev dashboard to open a signed-in page.", out["banner"])

    @unittest.skipUnless(CHROME, NO_CHROME)
    def test_a_server_that_stops_answering_reads_reconnecting_then_offline_after_30_seconds(self):
        steps = """await tick(5000); out.first = text($("#live")); await tick(35000); out.second = text($("#live")); out.banner = text($("#banner"));"""
        base = self.serve(lambda path: (200, "text/html", self.page(steps)) if path == "/" else (0, "", b""))
        out = chrome(base + "/#/overview", 1440, budget=60000)
        self.assertEqual((out["first"], out["second"]), ("Reconnecting…", "Offline"))
        self.assertIn("The dashboard server stopped. It stops by itself after 30 minutes with no open page. You are looking at data from", out["banner"])

    @unittest.skipUnless(CHROME, NO_CHROME)
    def test_a_new_devkit_version_asks_for_a_reload(self):
        steps = """await tick(7000); out.banner = text($("#banner")); out.primary = text($("#banner .btn.primary"));"""

        def routes(path):
            if path == "/":
                return 200, "text/html", self.page(steps)
            if path.startswith("/api/v1/events"):
                return 200, "text/event-stream", b"retry: 60000\n\n"
            if path == "/api/v1/health":
                return 200, "application/json", b'{"ok": true, "version": "99.1.0"}'
            return 404, "application/problem+json", b'{"title": "Not found"}'
        out = chrome(self.serve(routes) + "/#/overview", 1440, budget=15000)
        self.assertIn("devkit was updated to 99.1.0. Reload to get the new page. This page changes nothing, so reloading is safe.", out["banner"])
        self.assertEqual(out["primary"], "Reload")

    @unittest.skipUnless(CHROME, NO_CHROME)
    def test_an_update_keeps_the_open_drawer_flashes_changed_rows_and_announces_decisions(self):
        items = [dict(i, status="blocked") if i["id"] == "T-a2" else i for i in self.data["items"]]
        runs = json.loads(json.dumps(self.data["runs"]))
        runs[1]["decisions"] = [{"id": "D7", "ts": NOW, "question": "Drop the old importer?", "options": ["Yes", "No"], "recommend": "No", "irreversible": "", "from_name": "Tank"}]
        detail = {"id": "T-a1", "title": "Ship export", "scope": "acme/widget", "type": "task", "status": "doing", "meta": {"bucket": "now", "version": 2},
                  "body": "Body", "changelog": [], "links": [], "lock": {"holder": "me@testmac (claude:abc)", "agent": "claude", "session": "abc", "expires": iso(NOW + 3600), "state": "held"},
                  "plans": [], "sessions": [], "branches": [], "prs": [], "ci": [], "commits": [], "commits_complete": True, "run": None, "runs": [], "path": "items/acme/widget/T-a1.md"}
        event = b'retry: 60000\n\nid: 2\nevent: snapshot\ndata: {"version": 2, "changed": ["items", "runs"]}\n\n'

        def routes(path):
            if path == "/":
                return 200, "text/html", self.page("""await tick(12000); out.open = $("#modal").open; out.title = text($("#modal-title")); out.flash = $$(".flash").map(e => e.getAttribute("data-key"));
                    out.said = $("#live-status").textContent; out.card = text($('[data-key="task~T-a2"]'));""")
            if path.startswith("/api/v1/events"):
                return 200, "text/event-stream", event
            if path.startswith("/api/v1/dashboard"):
                return 200, "application/json", json.dumps({"items": items, "runs": runs, "snapshot": 2, "live": True}).encode()
            if path == "/api/v1/items/T-a1":
                return 200, "application/json", json.dumps(detail).encode()
            if path == "/api/v1/health":
                return 200, "application/json", json.dumps({"ok": True, "version": self.data["version"]}).encode()
            return 404, "application/problem+json", b'{"title": "Not found"}'
        out = chrome(self.serve(routes) + "/#/tasks?task=T-a1", 1440, budget=20000)
        self.assertTrue(out["open"])
        self.assertEqual(out["title"], "Ship export")
        self.assertEqual(out["flash"], ["task~T-a2"])                                            # only the row that changed
        self.assertIn("Decision waiting: Drop the old importer?, asked by Backend engineer (Tank).", out["said"])

    @unittest.skipUnless(CHROME, NO_CHROME)
    def test_the_branches_tab_reads_git_now_shows_its_error_and_tries_again(self):
        data = json.loads(json.dumps(self.data))
        PageBehaviorTests.with_branches(data)
        data["generated"] = int(time.time()) - 90         # the snapshot is a minute and a half old, whenever the suite reaches this test
        asked = []
        fresh = {"project": "acme/widget", "rows": [branch_row("feat/live", "not_backed_up", "1 commit only on this Mac", current=True, where="local only", upstream=None, unpushed=1)],
                 "counts": {"total": 1, "not_backed_up": 1, "embargoed": 0, "waiting": 0}, "prs": "ok", "mapped": int(time.time())}
        steps = """window.addEventListener("hashchange", () => { out.loading = text($("#branches .brst")); out.rowsWhileLoading = $$("#branches tbody tr").length; });
            await go("#/projects/acme/widget?tab=branches"); await tick(300); out.error = text($("#branches .callout")); out.rows = $$("#branches tbody tr").length;
            $("#branches .brretry").click(); await tick(500); out.after = $$("#branches tbody tr").map(text); out.status = text($("#branches .brst")); out.callout = !!$("#branches .callout");
            out.focusIn = $("#branches").contains(document.activeElement);"""

        def routes(path):
            if path == "/":
                return 200, "text/html", dashboard.page(data).replace("</body>", PROBE % steps + "</body>").encode()
            if path.startswith("/api/v1/branches/"):
                asked.append(path)
                if len(asked) == 1:
                    return 502, "application/problem+json", json.dumps({"title": "git could not read the branches", "status": 502,
                                                                        "detail": "fatal: not a git repository (or any of the parent directories): .git"}).encode()
                return 200, "application/json", json.dumps(fresh).encode()
            if path.startswith("/api/v1/events"):
                return 200, "text/event-stream", b"retry: 60000\n\n"
            if path == "/api/v1/health":
                return 200, "application/json", json.dumps({"ok": True, "version": data["version"]}).encode()
            return 404, "application/problem+json", b'{"title": "Not found"}'
        out = chrome(self.serve(routes) + "/#/overview", 1440, budget=15000)
        self.assertEqual((out["loading"], out["rowsWhileLoading"]), ("Checking with git…", 8))  # the snapshot's rows show at once
        self.assertEqual(out["error"], "git could not read the branches fatal: not a git repository (or any of the parent directories): .git "
                                       "The rows below are from 1 min ago. Try again")
        self.assertEqual(out["rows"], 8)
        self.assertEqual(asked, ["/api/v1/branches/acme/widget"] * 2)
        self.assertEqual(len(out["after"]), 1)                                                    # git's answer replaces the snapshot's rows
        self.assertIn("feat/live", out["after"][0])
        self.assertEqual((out["status"], out["callout"], out["focusIn"]), ("Checked just now", False, True))


if __name__ == "__main__":
    unittest.main()
