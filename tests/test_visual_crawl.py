# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The crawl and the journey walk of visual diagnosis. `map.crawl` runs lib/devkit/visual/crawl.cjs on the pinned
# playwright-core's Chromium over the fixture app (tests/fixtures/visual/app/, served by its appserver.py with a
# second origin and a log of every request, WebSocket handshake, and WebSocket message) and merges what it found
# into the product map: one node per route pattern, one edge per link or control with its status and redirects
# (R48, R49). The crawler's guards (R70) are proven by the server's log: only GET requests, no request for the
# avoided /logout and /items/3/delete, none for the second origin, no WebSocket handshake or message, nothing from
# the service worker. `walk.walk` performs a declared journey in one browser session and writes the walk record the
# structure checks read (R51, R52), and takes each reached step's captures through a capture callable; here that
# callable runs the real capture.cjs.
#
# Every test runs in a temporary handoff repository and state folder, with the config file pointed away from the
# developer's (core.HANDOFF, core.STATE, core.CONFIG_FILE). Without node, gitleaks, the pinned @playwright/cli, or
# its Chromium, the browser tests skip and say which is missing.
# Tests carry comments, not docstrings: `dev test prove` leaves out tests with a docstring.
# Run: python3 -m unittest tests.test_visual_crawl
import gzip
import json
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

_hermetic.activate()
import _sandbox  # noqa: E402

sys.path.insert(0, str(ROOT / "lib"))
APP = ROOT / "tests" / "fixtures" / "visual" / "app"
sys.path.insert(0, str(APP))

from appserver import KEY, AppServer  # noqa: E402
from devkit import core, identity  # noqa: E402
from devkit.visual import map as vmap  # noqa: E402
from test_visual_capture import NO_PLAYWRIGHT, PLAYWRIGHT  # noqa: E402

CRAWL = ROOT / "lib" / "devkit" / "visual" / "crawl.cjs"
CAPTURE = ROOT / "lib" / "devkit" / "visual" / "capture.cjs"
NODE = shutil.which("node")
P = "acme/billing"
J = "J:export-month"
SECRET = "[redacted: aws-access-token]"


def _isolate(case):
    # the handoff repository, devkit's state, and the config file in a temporary folder, for a test or a class
    tmp = Path(tempfile.mkdtemp(prefix="devkit-visual-crawl-"))
    patches = [mock.patch.object(core, "HANDOFF", tmp / "handoff"), mock.patch.object(core, "STATE", tmp / "state"),
               mock.patch.object(core, "CONFIG_FILE", tmp / "config.json"),
               mock.patch.object(identity, "hostname", return_value="mac-test")]
    if PLAYWRIGHT:
        patches.append(mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": PLAYWRIGHT["browsers"]}))
    for p in patches:
        p.start()
    return tmp, patches


def _release(tmp, patches):
    for p in reversed(patches):
        p.stop()
    shutil.rmtree(tmp, ignore_errors=True)


def crawl(url, **options):
    return vmap.crawl(P, url, core=PLAYWRIGHT["core"], by="sati", **options)


def node_of(m, route):
    found = [n for n in m["nodes"] if n["route"] == route]
    if len(found) != 1:
        raise AssertionError(f"{len(found)} nodes have the route {route}: {[n['route'] for n in m['nodes']]}")
    return found[0]


def edge_of(m, from_route, to_route):
    a, b = node_of(m, from_route)["id"], node_of(m, to_route)["id"]
    return next(e for e in m["edges"] if e["id"] == f"{a}>{b}")


def map_files():
    folder = core.HANDOFF / "visual" / "acme" / "billing" / "map"
    return {p.name: p.read_text() for p in sorted(folder.glob("*.json"))}


class AvoidWordsAndExcludeGlobs(unittest.TestCase):
    # the crawler's two name rules, in crawl.cjs itself: avoid words on names and URL paths, and --exclude globs

    def ask(self, fn, *args):
        script = f"process.stdout.write(JSON.stringify(require(process.argv[1]).{fn}(...JSON.parse(process.argv[2]))))"
        p = subprocess.run([NODE, "-e", script, str(CRAWL), json.dumps(args)], capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    @unittest.skipUnless(NODE, "node runs crawl.cjs")
    def test_avoid_words_match_whole_words_of_names_paths_and_queries(self):
        words = self.ask("avoidWords", ["archive"])
        cases = {"Delete invoice": "delete", "Sign out": "sign out", "SIGN-OUT now": "sign out",
                 "/logout": "logout", "/items/3/delete": "delete", "/account?confirm=remove": "remove",
                 "Pay now": "pay", "Sending reports": "send", "Archive month": "archive",
                 "Payments": None, "/payments/7": None, "Invoices": None, "Signal outage": None}
        for text, word in cases.items():
            self.assertEqual(self.ask("avoided", text, words), word, text)

    @unittest.skipUnless(NODE, "node runs crawl.cjs")
    def test_exclude_globs_match_route_paths_by_segment(self):
        cases = [("/admin/users/3", ["/admin/**"], True), ("/admin", ["/admin/**"], True),
                 ("/reports/2025", ["/reports/*"], True), ("/reports/2025/q1", ["/reports/*"], False),
                 ("/invoices/88", ["/reports/*", "/invoices/8?"], True), ("/invoices", ["/admin/**"], False)]
        for path, globs, hit in cases:
            self.assertEqual(self.ask("excluded", path, globs), hit, (path, globs))

    @unittest.skipUnless(NODE, "node runs crawl.cjs")
    def test_exclude_globs_match_the_decoded_path_as_the_app_reads_it(self):
        # an app decodes /%61dmin/panel to /admin/panel, so an encoded letter must not slip past --exclude
        cases = [("/%61dmin/panel", ["/admin/**"], True), ("/admin%2Fusers", ["/admin/**"], True),
                 ("/%E0%A4%A", ["/admin/**"], False), ("/reports/%32025", ["/reports/2025"], True)]
        for path, globs, hit in cases:
            self.assertEqual(self.ask("excluded", path, globs), hit, (path, globs))


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class CrawlOfTheFixtureApp(unittest.TestCase):
    # one crawl of the whole fixture app from its home page, read by every test of the class

    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.patches = _isolate(cls)
        cls.app = AppServer().__enter__()
        try:
            cls.result = crawl(f"{cls.app.base}/", why="crawl from /")
        except Exception:
            cls.tearDownClass()
            raise
        cls.m = cls.result["map"]
        cls.summary = cls.result["summary"]

    @classmethod
    def tearDownClass(cls):
        cls.app.__exit__()
        _release(cls.tmp, cls.patches)

    def test_vd_ac_31_one_node_per_route_pattern_and_one_edge_per_link_with_its_trigger(self):
        routes = {n["route"]: n for n in self.m["nodes"]}
        self.assertEqual(sorted(r for r, n in routes.items() if n["kind"] == "screen"),
                         ["/", "/account", "/customers", "/customers/:id", "/invoices", "/invoices/:id", "/items/:id/delete",
                          "/keys", "/live", "/logout", "/offline", "/reports", "/usage"])
        self.assertEqual(self.m["start"], "home")
        detail = node_of(self.m, "/invoices/:id")
        self.assertEqual((detail["id"], detail["title"], detail["example"], detail["origin"], detail["since"]),
                         ("invoices.id", "Invoice 1204", "/invoices/1204", "crawled", 1))
        self.assertEqual(node_of(self.m, "/invoices")["section"], "invoices")
        billing = edge_of(self.m, "/", "/customers")
        self.assertEqual((billing["kind"], billing["origin"], billing["status"], billing["redirects"]),
                         ("nav", "crawled", 200, 0))
        self.assertEqual(billing["trigger"], {"role": "link", "name": "Billing", "selector": 'a[href="/customers"]'})
        # the orphan has no link from anywhere, so the crawl cannot find it
        self.assertNotIn("/settings/legacy", routes)
        self.assertFalse(self.m["partial"])
        self.assertEqual(self.m["changelog"][-1]["by"], "sati")
        self.assertEqual(self.m["changelog"][-1]["why"], "crawl from /")

    def test_vd_ac_31_the_crawl_sends_only_gets_and_never_opens_logout_delete_or_a_button_that_destroys(self):
        methods = {method for method, _ in self.app.requests()}
        self.assertEqual(methods, {"GET"}, self.app.log)
        paths = [path for _, path in self.app.requests()]
        for never in ("/logout", "/items/3/delete", "/clicked/invoice-button"):
            self.assertNotIn(never, paths)
        self.assertIn("/account", paths)
        # the links are edges all the same, to nodes the crawl did not open
        for route in ("/logout", "/items/:id/delete"):
            self.assertEqual(node_of(self.m, route)["skipped"], "avoid")
            self.assertEqual(edge_of(self.m, "/account", route)["status"], None)
        self.assertEqual(edge_of(self.m, "/account", "/logout")["trigger"]["name"], "Sign out")
        self.assertEqual(self.summary["not_clicked"], ["Clear old items", "Sign out", "Delete invoice"])
        # the form's Save button was clicked, and its POST never left the browser
        self.assertEqual(self.summary["blocked"], [{"method": "POST", "route": "/account"}])

    def test_vd_ac_31_no_websocket_handshake_or_message_and_nothing_from_the_service_worker(self):
        self.assertIn("/live", [path for _, path in self.app.requests()])
        for method, path in self.app.requests():
            self.assertNotIn(method, ("UPGRADE", "MESSAGE"), path)
            self.assertNotIn(path, ("/sw.js", "/from-service-worker"))
        self.assertEqual(self.summary["websockets"], 1)

    def test_vd_ac_31_a_link_that_redirects_to_another_origin_is_an_external_edge_and_that_origin_gets_nothing(self):
        self.assertEqual(self.app.requests("second"), [])
        host = self.app.second.split("//")[1]
        external = [n for n in self.m["nodes"] if n["kind"] == "external"]
        self.assertEqual([(n["title"], n["example"]) for n in external], [(host, f"{self.app.second}/landing")])
        edge = next(e for e in self.m["edges"] if e["to"] == external[0]["id"])
        self.assertEqual((edge["from"], edge["kind"], edge["trigger"]["name"], edge["redirects"]),
                         ("account", "external", "Help center", 1))
        self.assertEqual(self.summary["external"], 1)

    def test_edges_carry_the_status_and_the_redirects_the_structure_checks_read(self):
        reports = edge_of(self.m, "/account", "/reports")
        self.assertEqual((reports["trigger"]["name"], reports["status"], reports["redirects"]), ("Reports", 200, 2))
        usage = edge_of(self.m, "/account", "/usage")
        self.assertEqual((usage["status"], usage["redirects"]), (404, 0))
        self.assertEqual(node_of(self.m, "/usage")["skipped"], "answered 404")
        found = {(s["check"], s["anchor"]["edge"]) for s in vmap.check(self.m, audit={"id": "VA-test"})}
        self.assertIn(("redirect-chain", "account>reports"), found)
        self.assertIn(("broken-edge", "account>usage"), found)
        # nodes the crawl did not open have no known way on, so they are no dead ends
        dead = sorted(a for check, a in ((s["check"], s["anchor"]["node"]) for s in vmap.check(
            self.m, audit={"id": "VA-test"})) if check == "dead-end")
        self.assertEqual(dead, ["customers.id"])

    def test_vd_ac_70_the_map_holds_neither_the_key_nor_the_token_from_the_crawl(self):
        files = map_files()
        self.assertEqual(sorted(files), ["current.json", "v1.json"])
        for name, text in files.items():
            self.assertNotIn(KEY, text, name)
            self.assertNotIn("abc123", text, name)
        keys = edge_of(self.m, "/account", "/keys")
        self.assertEqual(keys["trigger"]["name"], SECRET)
        self.assertEqual(keys["trigger"]["selector"], 'a[href="/keys?token=[redacted]"]')
        self.assertEqual(node_of(self.m, "/keys")["example"], "/keys?token=[redacted]")
        # a redacted name cannot be compared with its target's heading
        labels = [s for s in vmap.check(self.m, audit={"id": "VA-test"}) if s["check"] == "label-mismatch"]
        self.assertEqual([s["anchor"]["edge"] for s in labels], ["home>customers"])

    def test_the_terminal_lines_say_what_the_crawl_did_and_did_not_do(self):
        lines = self.summary["lines"]
        screens = sum(1 for n in self.m["nodes"] if n["kind"] == "screen")
        self.assertEqual(lines[0], f"✓ map v1 · {screens} screens ({screens} new) · {len(self.m['edges'])} edges "
                                   f"({len(self.m['edges'])} new)")
        self.assertEqual(lines[1], "  stopped at depth 3 · 0 links left · 1 request blocked (not GET) · "
                                   "3 controls not clicked (Clear old items, Sign out, Delete invoice)")
        host = self.app.base.split("//")[1]
        self.assertEqual(lines[2], f"– 1 link leads outside {host}; it is recorded as an external edge and not "
                                   "followed.")


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class CrawlLimitsVersionsAndScreens(unittest.TestCase):
    def setUp(self):
        tmp, patches = _isolate(self)
        self.addCleanup(_release, tmp, patches)

    def test_vd_ac_32_the_page_limit_stops_the_crawl_and_marks_the_map_partial(self):
        with AppServer() as app:
            out = crawl(f"{app.base}/", max_pages=5)
        m = out["map"]
        self.assertEqual(len(m["nodes"]), 5)
        self.assertTrue(m["partial"])
        left = out["summary"]["left"]
        self.assertGreater(left, 0)
        self.assertIn(f"⚠ The crawl stopped at 5 screens with {left} links left. The map is marked partial. Narrow "
                      "it with --within, or raise --max.", out["summary"]["lines"])

    def test_vd_ac_33_a_new_crawl_marks_what_it_no_longer_finds_and_keeps_declared_items(self):
        with AppServer() as app:
            crawl(f"{app.base}/", why="first crawl")
        vmap.add_node(P, "invoices.export", "Export invoices", route="/invoices#export", kind="overlay", by="sati")
        v2 = map_files()["v2.json"]
        with AppServer(hide=["/offline"]) as app:
            out = crawl(f"{app.base}/", why="crawl again")
        m = out["map"]
        self.assertEqual(m["version"], 3)
        self.assertEqual(node_of(m, "/offline")["missing_since"], 3)
        self.assertEqual(edge_of(m, "/account", "/offline")["missing_since"], 3)
        self.assertEqual(node_of(m, "/invoices#export")["missing_since"], None)
        self.assertEqual(node_of(m, "/live")["missing_since"], None)
        self.assertEqual(map_files()["v2.json"], v2)
        self.assertEqual((m["changelog"][-1]["by"], m["changelog"][-1]["why"]), ("sati", "crawl again"))
        self.assertIn("1 screen missing since v3: /offline", out["summary"]["lines"][0])
        # found again on the next crawl, it is no longer missing
        with AppServer() as app:
            m = crawl(f"{app.base}/")["map"]
        self.assertEqual(node_of(m, "/offline")["missing_since"], None)

    def test_query_parameters_a_node_lists_make_separate_screens(self):
        with AppServer() as app:
            m = crawl(f"{app.base}/")["map"]
            self.assertNotIn("/account?tab=profile", [n["route"] for n in m["nodes"]])
            vmap.add_node(P, "account", "Account", route="/account", params=["tab"], by="sati")
            m = crawl(f"{app.base}/")["map"]
        profile, security = node_of(m, "/account?tab=profile"), node_of(m, "/account?tab=security")
        self.assertEqual((profile["title"], security["title"]), ("Profile", "Security"))
        self.assertEqual(node_of(m, "/account")["params"], ["tab"])
        self.assertEqual(edge_of(m, "/account", "/account?tab=security")["trigger"]["name"], "Security")
        self.assertEqual(vmap.route_of(m, "http://127.0.0.1:9/account?utm=x&tab=security"),
                         "/account?tab=security")
        self.assertEqual(vmap.route_of(m, "http://127.0.0.1:9/invoices/1204?tab=x"), "/invoices/:id")

    def test_a_section_crawl_adds_only_the_nodes_under_its_prefix_and_requests_nothing_outside_it(self):
        with AppServer() as app:
            m = crawl(f"{app.base}/", within="/invoices")["map"]
        self.assertEqual(sorted(n["route"] for n in m["nodes"]), ["/invoices", "/invoices/:id"])
        # the start page is read for its links; no other page outside the prefix is requested
        outside = [path for _, path in app.requests() if path != "/" and not path.startswith("/invoices")]
        self.assertEqual(outside, [])
        # the invoice page's link to Home leaves the section; its link back to the list stays
        self.assertEqual(sorted(e["id"] for e in m["edges"]), ["invoices.id>invoices", "invoices>invoices.id"])

    def test_steps_run_on_the_start_page_first_and_one_that_finds_nothing_writes_nothing(self):
        with AppServer() as app:
            m = crawl(f"{app.base}/invoices", within="/invoices", steps=["fill #month 2026-10"])["map"]
            self.assertEqual(m["start"], "invoices")
            with self.assertRaises(vmap.MapError) as cm:
                crawl(f"{app.base}/invoices", steps=['click role=button[name="Archive"]'])
        self.assertEqual(str(cm.exception),
                         'Step 1, click role=button[name="Archive"], found nothing in 10 s. Nothing was written.')
        self.assertEqual(sorted(map_files()), ["current.json", "v1.json"])

    def test_screen_parameters_belong_to_a_route(self):
        with self.assertRaises(vmap.MapError) as cm:
            vmap.add_node(P, "deck.intro", "Intro", page=1, params=["tab"])
        self.assertEqual(str(cm.exception), "Screen parameters belong to a route: give node deck.intro --route with "
                                            "--params.")

    def test_a_start_on_another_host_needs_remote(self):
        with self.assertRaises(vmap.MapError) as cm:
            crawl("http://example.com/")
        self.assertEqual(str(cm.exception), "example.com is not this Mac, so it is crawled only with --remote (and "
                                            "then only fixture or demo data). Nothing was written.")
        self.assertEqual(map_files(), {})


HARDEN = ROOT / "tests" / "fixtures" / "visual" / "harden"
# the hardening page's redirects: three of its requests hop to avoided paths, one to a plain file on the app, and one
# to the second origin
HOPS = {"/hop-img": (302, "/items/3/delete"), "/hop-fetch": (302, "/account/remove"),
        "/hop-worker": (302, "/send-invoices"), "/hop-logo": (302, "/logo.svg"),
        "/hop-away": (302, "{{SECOND}}/pic.png")}


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class CrawlGuardsBeyondTheDocument(unittest.TestCase):
    # one crawl of tests/fixtures/visual/harden/ with --exclude /admin/**, run as map.py runs crawl.cjs, and the log of
    # what its server received: what a page's images, fetches, and worker reach after a redirect or through a socket

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="devkit-visual-harden-"))
        cls.app = AppServer(root=HARDEN, redirects=HOPS).__enter__()
        request = {"mode": "crawl", "start": f"{cls.app.base}/", "exclude": ["/admin/**"], "timeout_s": 120,
                   "core": PLAYWRIGHT["core"]}
        try:
            p = subprocess.run([NODE, str(CRAWL), "--json"], input=json.dumps(request), capture_output=True, text=True,
                               timeout=300, env=_sandbox.env(TMPDIR=str(cls.tmp), HOME=str(cls.tmp),
                                                             PLAYWRIGHT_BROWSERS_PATH=PLAYWRIGHT["browsers"]))
        finally:
            cls.app.__exit__()
            shutil.rmtree(cls.tmp, ignore_errors=True)
        if p.returncode != 0:
            raise AssertionError(f"crawl.cjs failed (exit {p.returncode}): {p.stderr.strip()[-1000:]}")
        cls.result = json.loads(p.stdout)
        cls.paths = [path for _, path in cls.app.requests()]

    def test_a_redirect_of_an_image_a_fetch_or_a_worker_request_is_followed_only_to_an_allowed_path_on_the_app(self):
        # the first request of each hop left the browser, and the worker ran, so the page did try every one
        for asked in ("/hop-img", "/hop-fetch", "/hop-worker", "/worker-ran", "/hop-away"):
            self.assertIn(asked, self.paths)
        for never in ("/items/3/delete", "/account/remove", "/send-invoices"):
            self.assertNotIn(never, self.paths)
        # a hop to a plain file on the app is still followed; the hop to the second origin is not
        self.assertIn("/logo.svg", self.paths)
        self.assertEqual(self.app.requests("second"), [])

    def test_a_workers_websocket_is_refused_by_the_crawlers_policy_and_never_reaches_the_server(self):
        # routeWebSocket only sees a page's sockets; both workers report the directive that refused theirs: the one
        # whose script the guard served, and the blob: one that takes the page's policy
        self.assertIn("/worker-ran", self.paths)
        self.assertIn("/worker-refused?connect-src", self.paths)
        self.assertIn("/blob-refused?connect-src", self.paths)
        self.assertEqual([path for method, path in self.app.requests() if method in ("UPGRADE", "MESSAGE")], [])

    def test_an_excluded_path_written_with_an_encoded_letter_is_never_requested(self):
        self.assertNotIn("/%61dmin/panel", self.paths)
        skipped = {n["key"]: n["skipped"] for n in self.result["nodes"]}
        self.assertEqual(skipped.get("/%61dmin/panel"), "exclude")
        self.assertEqual(self.result["excluded"], 1)


def _capture_with_cjs(store: Path, top: Path, calls: list):
    # the capture callable the store passes to the walk, here running capture.cjs itself: one record per viewport
    # and theme, each in a new 0700 TMPDIR
    def capture(source, *, steps=(), state=None, node=None, journey=None, step=None, viewports=None, themes=None):
        call = {"source": source, "steps": list(steps), "node": node, "journey": journey, "step": step, "ids": []}
        calls.append(call)
        records = []
        for viewport in viewports or [[1280, 800]]:
            for theme in themes or ["light"]:
                folder = Path(tempfile.mkdtemp(dir=top))
                request = {"id": f"VC-walk{step}x{viewport[0]}{theme}", "source": {"url": source}, "state": state,
                           "recipe": {"viewport": viewport, "theme": theme, "steps": list(steps)}, "check": False,
                           "store": str(store), "core": PLAYWRIGHT["core"]}
                p = subprocess.run([NODE, str(CAPTURE), "--json"], input=json.dumps(request), capture_output=True,
                                   text=True, timeout=120, env=_sandbox.env(TMPDIR=str(folder)))
                shutil.rmtree(folder, ignore_errors=True)
                if p.returncode != 0:
                    raise AssertionError(f"capture.cjs failed: {p.stderr.strip()[-1000:]}")
                record = json.loads(p.stdout)
                # the store gives the record its id and its map fields; capture.cjs prints the rest
                record["id"] = request["id"]
                record["map"] = {"node": node, "journey": journey, "step": step}
                call["ids"].append(record["id"])
                records.append(record)
        return records
    return capture


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class JourneyWalk(unittest.TestCase):
    def setUp(self):
        self.tmp, patches = _isolate(self)
        self.addCleanup(_release, self.tmp, patches)
        vmap.apply(P, json.loads((APP / "declared.json").read_text()), why="declare the fixture app", by="sati")
        from devkit.visual import walk
        self.walk = walk

    def headings(self, record):
        desc = json.loads(gzip.decompress((self.tmp / "store" / "desc" /
                                           f"{record['description']['sha256']}.json.gz").read_bytes()))
        return [e["name"] for e in desc["elements"] if e["role"] == "heading"]

    def test_the_walk_of_the_fixture_journey_records_what_walked_json_holds(self):
        with AppServer() as app:
            record = self.walk.walk(vmap.load(P), J, base_url=app.base, core=PLAYWRIGHT["core"])
        expected = json.loads((APP / "walked.json").read_text())
        steps = [{k: v for k, v in s.items() if k != "captures"} for s in record["steps"]]
        self.assertEqual({**record, "steps": steps}, expected)
        self.assertEqual([s["captures"] for s in record["steps"]], [[]] * 6)

    def test_vd_ac_35_each_reached_step_is_captured_in_every_viewport_with_the_steps_that_reach_it(self):
        vmap.add_journey(P, "J:open-invoice", actor="finance clerk", task="Open an invoice",
                         path="home > invoices.list > invoices.detail",
                         do=['click role=link[name="Invoices"]', 'click role=link[name="Invoice 1204"]'], by="sati")
        calls = []
        capture = _capture_with_cjs(self.tmp / "store", self.tmp, calls)
        with AppServer() as app:
            record = self.walk.walk(vmap.load(P), "J:open-invoice", base_url=app.base, viewports=[[1280, 800], [390, 844]],
                                    capture=capture, core=PLAYWRIGHT["core"])
        self.assertEqual([c["steps"] for c in calls],
                         [[], ['click role=link[name="Invoices"]'],
                          ['click role=link[name="Invoices"]', 'click role=link[name="Invoice 1204"]']])
        self.assertEqual({c["source"] for c in calls}, {f"{app.base}/"})
        self.assertEqual([(c["journey"], c["step"], c["node"]) for c in calls],
                         [("J:open-invoice", 1, "home"), ("J:open-invoice", 2, "invoices.list"),
                          ("J:open-invoice", 3, "invoices.detail")])
        # each step names the captures the store returned for it: one per viewport
        self.assertEqual([s["captures"] for s in record["steps"]], [c["ids"] for c in calls])
        self.assertEqual([len(c["ids"]) for c in calls], [2, 2, 2])
        self.assertEqual(len({i for c in calls for i in c["ids"]}), 6)

    def test_the_captures_of_each_step_show_the_screen_that_step_reached(self):
        vmap.add_journey(P, "J:open-invoice", actor="finance clerk", task="Open an invoice",
                         path="home > invoices.list > invoices.detail",
                         do=['click role=link[name="Invoices"]', 'click role=link[name="Invoice 1204"]'], by="sati")
        shown = []

        def capture(source, **kw):
            records = _capture_with_cjs(self.tmp / "store", self.tmp, [])(source, **kw)
            shown.append(self.headings(records[0]))
            return records

        with AppServer() as app:
            self.walk.walk(vmap.load(P), "J:open-invoice", base_url=app.base, capture=capture, core=PLAYWRIGHT["core"])
        self.assertEqual([h[0] for h in shown], ["Home", "Invoices", "Invoice 1204"])

    def test_vd_ac_35_a_missing_trigger_stops_the_walk_at_its_step_and_raises_a_broken_edge(self):
        vmap.add_journey(P, "J:open-invoice", actor="finance clerk", task="Open an invoice",
                         path="home > invoices.list > invoices.detail",
                         do=['click role=link[name="Invoices"]', 'click role=link[name="Invoice 9999"]'], by="sati")
        calls = []
        with AppServer() as app:
            record = self.walk.walk(vmap.load(P), "J:open-invoice", base_url=app.base,
                                    capture=_capture_with_cjs(self.tmp / "store", self.tmp, calls),
                                    core=PLAYWRIGHT["core"])
        self.assertEqual(record["broken"], {"step": 3, "edge": "invoices.list>invoices.detail",
                                            "do": 'click role=link[name="Invoice 9999"]',
                                            "error": "found nothing in 10 s"})
        self.assertEqual([s["step"] for s in record["steps"]], [1, 2])
        self.assertEqual([c["step"] for c in calls], [1, 2])
        self.assertEqual(self.walk.message(record, vmap.load(P), signal="VA-test#5"),
                         '✗ J:open-invoice step 3 (click role=link[name="Invoice 9999"]) found nothing in 10 s on '
                         "invoices.list. Steps 1–2 were captured; the journey is broken at step 3 (signal VA-test#5).")
        broken = [s for s in vmap.check(vmap.load(P), audit={"id": "VA-test", "scope": "journey",
                                                             "journeys": ["J:open-invoice"]}, walks=[record])
                  if s["check"] == "broken-edge"]
        self.assertEqual([(s["anchor"]["journey"], s["anchor"]["step"]) for s in broken], [("J:open-invoice", 3)])

    def args(self, *argv, project=P):
        # the command line as dev visual journey walk parses it, with the project the handler resolved
        from devkit import cli
        a = cli.build_parser("visual").parse_args(["visual", "journey", "walk", *argv])
        a.project_id = project
        return a

    def test_the_walk_command_captures_each_step_through_the_store_and_keeps_the_walk_under_its_audit(self):
        from devkit.visual import store
        vmap.add_journey(P, "J:open-invoice", actor="finance clerk", task="Open an invoice",
                         path="home > invoices.list > invoices.detail",
                         do=['click role=link[name="Invoices"]', 'click role=link[name="Invoice 1204"]'], by="sati")
        with AppServer() as app:
            code = self.walk.cmd(self.args("J:open-invoice", "--base", app.base, "--viewport", "390x844",
                                           "--no-check"))
        self.assertEqual(code, 0)
        aid = store.open_audit(P)
        audit = store.load(P, aid)
        self.assertEqual((audit["scope"], audit["journeys"]), ("journey", ["J:open-invoice"]))
        caps = store.captures_of(P, aid)
        self.assertEqual([(c["map"]["journey"], c["map"]["step"]) for c in caps],
                         [("J:open-invoice", 1), ("J:open-invoice", 2), ("J:open-invoice", 3)])
        [record] = self.walk.load(P, aid)
        self.assertEqual([s["captures"] for s in record["steps"]], [[c["id"]] for c in caps])

    def test_vd_ac_43_a_walked_path_declares_its_nodes_edges_and_journey_in_a_project_with_no_map(self):
        q = "acme/ledger"
        with AppServer() as app:
            code = self.walk.cmd(self.args("--path", "home > invoices.list > invoices.detail",
                                           "--do", 'click role=link[name="Invoices"]',
                                           "--do", 'click role=link[name="Invoice 1204"]', "--base", f"{app.base}/",
                                           "--viewport", "390x844", "--no-check", project=q))
        self.assertEqual(code, 0)
        m = vmap.load(q)
        self.assertEqual([(n["id"], n["route"], n["title"], n["origin"]) for n in m["nodes"]],
                         [("home", "/", "Home", "declared"), ("invoices.list", "/invoices", "Invoices", "declared"),
                          ("invoices.detail", "/invoices/:id", "Invoice 1204", "declared")])
        self.assertEqual([(e["id"], e["kind"], e["trigger"]["role"], e["trigger"]["name"]) for e in m["edges"]],
                         [("home>invoices.list", "nav", "link", "Invoices"),
                          ("invoices.list>invoices.detail", "nav", "link", "Invoice 1204")])
        self.assertEqual([(j["id"], j["start"], j["end"]) for j in m["journeys"]],
                         [("J:home-to-invoices-detail", "home", "invoices.detail")])
        from devkit.visual import store
        [record] = self.walk.load(q, store.open_audit(q))
        self.assertEqual((record["journey"], record["map_version"], [len(s["captures"]) for s in record["steps"]]),
                         ("J:home-to-invoices-detail", 1, [1, 1, 1]))

    def test_a_walked_path_that_breaks_declares_nothing(self):
        q = "acme/ledger"
        with AppServer() as app:
            with self.assertRaises(vmap.MapError) as cm:
                self.walk.cmd(self.args("--path", "home > invoices.list", "--do", 'click role=link[name="Ledger"]',
                                        "--base", app.base, "--no-check", project=q))
        self.assertEqual(str(cm.exception), 'J:home-to-invoices-list step 2 (click role=link[name="Ledger"]) found '
                                            "nothing in 10 s on home. Nothing was declared: fix the step, then walk "
                                            "the path again.")
        self.assertEqual(vmap.load(q)["version"], 0)

    def test_a_walk_without_the_apps_address_is_refused_before_an_audit_opens(self):
        from devkit.visual import store
        for argv in ([J], ["--path", "home > invoices.list", "--do", 'click role=link[name="Invoices"]']):
            with self.assertRaises(vmap.MapError) as cm:
                self.walk.cmd(self.args(*argv))
            self.assertEqual(str(cm.exception), "The map has no address of the app from a crawl. Give the address "
                                                "the app runs on: --base http://127.0.0.1:4200")
            self.assertIsNone(store.open_audit(P))

    def test_a_walk_record_is_kept_per_audit_and_journey(self):
        with AppServer() as app:
            record = self.walk.walk(vmap.load(P), J, base_url=app.base, core=PLAYWRIGHT["core"])
        path = self.walk.save(P, "VA-test", record)
        self.assertEqual(path, core.HANDOFF / "visual" / "acme" / "billing" / "walks" / "VA-test" / "export-month.json")
        self.assertEqual(self.walk.load(P, "VA-test"), [record])
        self.assertEqual(self.walk.load(P, "VA-other"), [])


ESC = "\x1b]52;c;cHduZWQ=\x07‮"  # a terminal escape that sets the clipboard, then a right-to-left override


class WalkRecordsAndDocumentJourneys(unittest.TestCase):
    # no browser: a walk record a sync brought in, and the journeys a walk refuses before it opens anything
    def setUp(self):
        self.tmp, patches = _isolate(self)
        self.addCleanup(_release, self.tmp, patches)
        from devkit.visual import walk
        self.walk = walk

    def deck_map(self, document="deck.pdf"):
        # the map dev visual product deck.pdf writes (docmap.py): a node per page with its document, the reading order,
        # and the storyline; written as the file a sync brings in
        nodes = [{"id": f"deck.p{n}", "title": f"page {n}", "kind": "slide", "route": None, "page": n,
                  "section": "deck", "origin": "document", "since": 1, "missing_since": None, "final": n == 3,
                  "example": None, **({"document": document} if document else {})} for n in (1, 2, 3)]
        edges = [{"id": f"deck.p{n}>deck.p{n + 1}", "from": f"deck.p{n}", "to": f"deck.p{n + 1}", "kind": "next",
                  "trigger": {"role": None, "name": None, "selector": None}, "when": None, "origin": "document",
                  "since": 1, "missing_since": None} for n in (1, 2)]
        story = {"id": "J:deck-storyline", "actor": "reader", "task": "Read deck.pdf from the first page to the last",
                 "start": "deck.p1", "end": "deck.p3", "budget": None, "keeps": [], "expect": None,
                 "origin": "document", "since": 1,
                 "steps": [{"edge": e["id"], "do": [], "expect": None, "decision": None} for e in edges]}
        folder = core.HANDOFF / "visual" / "acme" / "billing" / "map"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "current.json").write_text(json.dumps({"version": 1, "partial": False, "start": "deck.p1",
                                                         "base": "http://127.0.0.1:4200", "nodes": nodes,
                                                         "edges": edges, "journeys": [story], "concepts": [],
                                                         "components": [], "changelog": []}))

    def args(self, *argv):
        from devkit import cli
        a = cli.build_parser("visual").parse_args(["visual", "journey", "walk", *argv])
        a.project_id = P
        return a

    def test_a_walk_record_a_sync_brought_in_reads_back_without_control_or_bidi_characters(self):
        # the walk records live in the handoff repository beside the other records, and are read the same way
        folder = core.HANDOFF / "visual" / "acme" / "billing" / "walks" / "VA-test"
        folder.mkdir(parents=True)
        (folder / "export-month.json").write_text(json.dumps(
            {"journey": "J:export-month", "map_version": 1,
             "broken": {"step": 2, "edge": "home>invoices.list", "do": f"click #x{ESC}", "error": f"found{ESC} nothing"},
             "steps": [{"step": 1, "node": f"home{ESC}", "edge": None, "status": 200, "redirects": 0,
                        f"ke{ESC}eps": {}, "captures": []}]}))
        [record] = self.walk.load(P, "VA-test")
        self.assertEqual((record["broken"]["do"], record["broken"]["error"], record["steps"][0]["node"]),
                         ("click #x]52;c;cHduZWQ=", "found]52;c;cHduZWQ= nothing", "home]52;c;cHduZWQ="))
        self.assertIn("ke]52;c;cHduZWQ=eps", record["steps"][0])

    def test_a_documents_storyline_is_refused_with_the_command_that_captures_its_pages(self):
        from devkit.visual import store
        self.deck_map()
        says = ("J:deck-storyline reads the pages of deck.pdf in order, and a document has no browser steps to walk. "
                "Capture its pages instead: dev visual product deck.pdf")
        for argv in (["J:deck-storyline"], ["deck-storyline", "--base", "http://127.0.0.1:4200"]):
            with self.subTest(argv=argv):
                with self.assertRaises(vmap.MapError) as cm:
                    self.walk.cmd(self.args(*argv))
                self.assertEqual(str(cm.exception), says)
                self.assertIsNone(store.open_audit(P))
        # verify walks a journey again through walk(), which refuses it the same way
        with self.assertRaises(vmap.MapError) as cm:
            self.walk.walk(vmap.load(P), "J:deck-storyline", base_url="http://127.0.0.1:4200")
        self.assertEqual(str(cm.exception), says)

    def test_a_storyline_of_pages_declared_without_their_document_names_the_pdf_to_give(self):
        self.deck_map(document=None)
        with self.assertRaises(vmap.MapError) as cm:
            self.walk.cmd(self.args("J:deck-storyline"))
        self.assertEqual(str(cm.exception), "J:deck-storyline reads the pages of a document in order, and a document "
                                            "has no browser steps to walk. Capture its pages instead: dev visual "
                                            "product <file>.pdf")


if __name__ == "__main__":
    unittest.main()
