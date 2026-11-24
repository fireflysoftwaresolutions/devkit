# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# lib/devkit/visual/store.py keeps the records of visual diagnosis: audits (VA-…), captures (VC-…), and findings
# (VF-…) as JSON in the handoff repository (visual/<owner>/<repo>/), and the pictures and descriptions in the local
# store (~/.local/state/devkit/visual/<owner>/<repo>/, folders 0700 and files 0600). These tests run every function
# in a temporary handoff repository, state folder, and configuration (core.HANDOFF, core.STATE, core.CONFIG_FILE),
# and take real captures with lib/devkit/visual/capture.cjs on a page each test writes into its temporary folder;
# without node, gitleaks, or the pinned Playwright and its Chromium, the capture tests skip and say which is missing.
# Tests carry comments, not docstrings: `dev test prove` leaves out tests with a docstring.
# Run: python3 -m unittest tests.test_visual_store
import calendar
import contextlib
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
sys.path.insert(0, str(ROOT / "lib"))

from devkit import core, gc, identity, work  # noqa: E402
from devkit.visual import map as vmap  # noqa: E402
from devkit.visual import report  # noqa: E402
from devkit.visual import store  # noqa: E402
from test_visual_map import presence  # noqa: E402

P = "acme/billing"
NODE = shutil.which("node")
GITLEAKS = shutil.which("gitleaks")
CWEBP, DWEBP = shutil.which("cwebp"), shutil.which("dwebp")
ID = re.compile(r"^V[ACF]-[0-9a-z]{7,40}$")
ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
HOST = "ana-macbook-pro"  # this Mac's hostname in these tests: a record never holds it
MAC = identity.Machine("m-test0001", HOST, {HOST: [[0, 4e9]]})  # this Mac's machine id, which records name it by

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Invoices</title>
<style>body { margin: 0; font: 16px Arial, sans-serif; } h1 { margin: 16px; font-size: 28px; }
.bar { position: relative; height: 64px; }
#export { position: absolute; left: 20px; top: 10px; width: 120px; height: 40px; }
#amount { position: absolute; left: 100px; top: 20px; width: 120px; height: 40px; }
.primary { display: block; margin: 8px 16px; width: 200px; height: 48px; }
label { display: block; margin: 16px; } input { width: 300px; height: 40px; }</style></head>
<body><h1>Invoices</h1>
<div class="bar"><button id="export" data-testid="export">Export</button><button id="amount">Amount</button></div>
<button class="primary">One</button><button class="primary">Two</button><button class="primary">Three</button>
<label>Password <input id="password" type="password" value="hunter2-visual"></label>
</body></html>
"""


def _account_home() -> Path:
    # the run's HOME is a temporary one (tests/_hermetic.py); Playwright's browsers live in the account's own home
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError):
        return Path.home()


def _browsers():
    # -> the folder of Playwright's browsers when the pinned Chromium can run here, else (None, why)
    if not NODE:
        return None, "node is not on PATH"
    if not GITLEAKS:
        return None, "gitleaks is not installed (brew install gitleaks)"
    try:
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=60).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        root = ""
    core_dir = Path(root or "/nonexistent") / "@playwright" / "cli" / "node_modules" / "playwright-core"
    try:
        browsers = json.loads((core_dir / "browsers.json").read_text())["browsers"]
        revision = next(b["revision"] for b in browsers if b["name"] == "chromium")
    except (OSError, ValueError, KeyError, StopIteration):
        return None, "the pinned @playwright/cli is not installed (dev install --only tools)"
    cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(
        _account_home() / ("Library/Caches" if sys.platform == "darwin" else ".cache") / "ms-playwright")
    if not (Path(cache) / f"chromium-{revision}").is_dir():
        return None, f"Chromium {revision} is not installed in {cache}"
    return cache, None


BROWSERS, NO_BROWSER = _browsers()


def png(width: int, height: int, rgb=(200, 30, 30)) -> bytes:
    # a solid PNG, written by hand so the test needs no image library
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="devkit-visual-store-")))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.scratch = self.tmp / "tmp"
        self.scratch.mkdir()
        patches = [mock.patch.object(core, "HANDOFF", self.tmp / "handoff"),
                   mock.patch.object(core, "STATE", self.tmp / "state"),
                   mock.patch.object(core, "CONFIG_FILE", self.tmp / "config.json"),
                   mock.patch.dict(core.CFG, {"github_user": "devuser"}),
                   mock.patch.object(identity, "hostname", return_value=HOST),
                   mock.patch.object(identity, "machine", lambda now=None: MAC),
                   mock.patch.object(store, "_session", return_value="claude-s1"),
                   mock.patch.object(store, "_member", return_value=None),
                   mock.patch.object(tempfile, "tempdir", str(self.scratch))]
        if BROWSERS:
            patches.append(mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": BROWSERS}))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.records = core.HANDOFF / "visual" / "acme" / "billing"
        self.local = core.STATE / "visual" / "acme" / "billing"
        self.page = self.tmp / "site" / "invoices.html"
        self.page.parent.mkdir()
        self.page.write_text(PAGE)
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(self.page.parent)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_address[1]}/invoices.html"

    def patched(self, target, name, **kw):
        # mock.patch.object for the rest of the test (TestCase.enterContext is Python 3.11 and later; CI runs 3.9)
        p = mock.patch.object(target, name, **kw)
        self.addCleanup(p.stop)
        return p.start()

    def record(self, rid):
        folder = {"VA": "audits", "VC": "captures", "VF": "findings"}[rid[:2]]
        return json.loads((self.records / folder / f"{rid}.json").read_text())

    def description(self, capture):
        sha = capture["description"]["sha256"]
        return json.loads(gzip.decompress((self.local / "desc" / f"{sha}.json.gz").read_bytes()))

    def element(self, capture, selector):
        return next(e for e in self.description(capture)["elements"] if e["selector"] == selector)

    def shot(self, **kw):
        # one real capture of the test page in a new screen audit, at 390x844 light
        store.start(P, "screen", "Is the invoice list usable on a phone?", viewports=["390x844"])
        kw.setdefault("check", True)
        return store.capture(P, self.url, state="list", **kw)[0]


@unittest.skipUnless(NODE and GITLEAKS, "node and gitleaks redact the text a record holds")
class IdsAndAuditsTest(StoreCase):
    def test_ids_follow_the_work_item_scheme_and_never_repeat(self):
        ids = [store.new_id("VC") for _ in range(50)] + [store.new_id("VA"), store.new_id("VF")]
        self.assertTrue(all(ID.match(i) for i in ids), ids[:3])
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual({i[:3] for i in ids}, {"VC-", "VA-", "VF-"})
        with self.assertRaises(store.VisualError):
            store.new_id("VX")

    def test_ids_keep_their_width_and_their_order_past_the_day_the_seconds_grow_a_character(self):
        # the seconds since 2026 need 6 base36 characters from 2027-12-01; the time part keeps 6 from the start
        def made_at(day):
            with mock.patch.object(store.time, "time", return_value=calendar.timegm(time.strptime(day, "%Y-%m-%d"))):
                return store.new_id("VC")
        ids = [made_at(day) for day in ("2026-01-02", "2026-11-18", "2027-11-30", "2027-12-02", "2031-06-01")]
        self.assertEqual({len(i) for i in ids}, {len("VC-") + 12}, ids)
        self.assertEqual(sorted(ids), ids)
        # an id of the old scheme, with a 5-character time part, sorts before them and is still a record id
        old = "VC-gzzzzabc123"
        self.assertLess(old, ids[0])
        self.assertEqual(store._path(P, old).name, f"{old}.json")

    def test_start_writes_the_audit_record_and_opens_it_in_this_session(self):
        # VD-AC-50: the scope, the question, the matrix, and the map version it started from
        vmap.add_node(P, "home", "Home", route="/", by="sati", why="the start")
        audit = store.start(P, "product", "Can a finance clerk export a month of invoices?",
                            task="A clerk exports October", mode="operate", viewports=["1440x900", "390x844"],
                            themes=["light", "dark"], states=["empty"], journeys=["J:export-month"])
        rec = self.record(audit["id"])
        self.assertEqual(rec, audit)
        self.assertEqual(rec["scope"], "product")
        self.assertEqual(rec["question"], "Can a finance clerk export a month of invoices?")
        self.assertEqual(rec["matrix"], {"viewports": [[1440, 900], [390, 844]], "themes": ["light", "dark"],
                                         "states": ["empty"]})
        self.assertEqual(rec["map_version"], 1)
        self.assertEqual(rec["journeys"], ["J:export-month"])
        self.assertEqual(rec["by"], {"member": None, "user": "devuser", "host": "m-test0001", "session": "claude-s1"})
        self.assertTrue(ISO.match(rec["taken"]))
        self.assertEqual((rec["passes"], rec["signals"], rec["include"]), ({}, [], []))
        self.assertEqual(store.open_audit(P), audit["id"])

    def test_an_audit_without_a_map_has_no_map_version_and_writes_no_map(self):
        audit = store.start(P, "screen", "Is the list readable?")
        self.assertIsNone(audit["map_version"])
        self.assertEqual(audit["matrix"], {"viewports": [[1440, 900]], "themes": ["light"], "states": []})
        self.assertFalse((self.records / "map").exists())

    def test_start_refuses_what_it_cannot_record_and_writes_nothing(self):
        cases = ((("galaxy", "Q?", {}), "screen, component, journey, section, product"),
                 (("screen", "Q?", {"viewports": ["1440by900"]}), "1440by900"),
                 (("screen", "Q?", {"themes": ["sepia"]}), "light or dark"),
                 (("screen", "Q?", {"mode": "admire"}), "operate, read, persuade, experience"),
                 (("product", "Q?", {"include": ["VA-g33nothere1"]}), "VA-g33nothere1"),
                 (("screen", "  ", {}), "question"))
        for (scope, question, kw), says in cases:
            with self.subTest(scope=scope, kw=kw):
                with self.assertRaises(store.VisualError) as e:
                    store.start(P, scope, question, **kw)
                self.assertIn(says, str(e.exception))
        self.assertFalse((self.records / "audits").exists())
        self.assertIsNone(store.open_audit(P))

    def test_a_viewport_is_from_100_to_8192_css_px_each_way_as_the_capture_driver_takes_it(self):
        for size in ("99x844", "1440x8193"):
            with self.subTest(size):
                with self.assertRaises(store.VisualError) as e:
                    store.start(P, "screen", "Q?", viewports=[size])
                self.assertIn(f"{size} is not a viewport: each side is from 100 to 8192 CSS px", str(e.exception))
        self.assertFalse((self.records / "audits").exists())
        audit = store.start(P, "screen", "Q?", viewports=["100x8192", "8192x100"])
        self.assertEqual(audit["matrix"]["viewports"], [[100, 8192], [8192, 100]])

    def test_by_names_the_crew_member_when_a_member_runs_the_command(self):
        with mock.patch.object(store, "_member", return_value="sati"):
            audit = store.start(P, "screen", "Q?")
            self.assertEqual(store.who(), "sati")
        self.assertEqual(audit["by"]["member"], "sati")
        self.assertEqual(store.who(), "devuser")


    def test_without_a_configured_user_a_record_names_the_github_login_and_never_the_macs_login(self):
        # records sync to the handoff repository, so the Mac's own login ($USER) stays out of them
        cfg = {k: v for k, v in core.CFG.items() if k != "github_user"}
        with mock.patch.dict(core.CFG, cfg, clear=True), mock.patch.dict(os.environ, {"USER": "mac-login"}):
            with mock.patch.object(core, "gh_user", return_value={"login": "octo"}):
                self.assertEqual((store.actor()["user"], store.who()), ("octo", "octo"))
                self.assertEqual(vmap.add_node(P, "home", "Home", route="/")["map"]["changelog"][-1]["by"], "octo")
            with mock.patch.object(core, "gh_user", return_value={}):
                self.assertEqual((store.actor()["user"], store.who()), (None, "unknown"))
                self.assertEqual(vmap.add_node(P, "about", "About", route="/about")["map"]["changelog"][-1]["by"],
                                 "unknown")


@unittest.skipUnless(BROWSERS, NO_BROWSER or "")
class CaptureTest(StoreCase):
    def test_a_capture_writes_its_record_and_the_picture_its_hash_names(self):
        # VD-AC-1: the recipe, the environment, and the picture's SHA-256 matching the stored file
        git = self.tmp / "app"
        git.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "feat/invoice-export", str(git)], check=True)
        subprocess.run(["git", "-C", str(git), "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q",
                        "--allow-empty", "-m", "init"], check=True)
        store.start(P, "screen", "Q?", viewports=["390x844"], themes=["dark"])
        c = store.capture(P, self.url, state="planted", cwd=str(git))[0]
        rec = self.record(c["id"])
        self.assertEqual(rec, c)
        self.assertEqual(rec["audit"], store.open_audit(P))
        self.assertEqual((rec["kind"], rec["state"]), ("web", "planted"))
        self.assertEqual(rec["recipe"]["viewport"], [390, 844])
        self.assertEqual(rec["recipe"]["theme"], "dark")
        self.assertEqual(rec["recipe"]["timezone"], "UTC")
        head = subprocess.run(["git", "-C", str(git), "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True).stdout.strip()
        self.assertEqual((rec["env"]["commit"], rec["env"]["branch"], rec["env"]["dirty"]),
                         (head, "feat/invoice-export", False))
        self.assertEqual(rec["env"]["devkit"], core.VERSION)
        self.assertTrue(rec["env"]["browser"].startswith("chromium"))
        self.assertEqual(rec["image"]["where"], "local:m-test0001")
        blob = (self.local / "blobs" / f"{rec['image']['sha256']}.png").read_bytes()
        self.assertEqual(hashlib.sha256(blob).hexdigest(), rec["image"]["sha256"])
        self.assertEqual(rec["by"]["user"], "devuser")
        self.assertEqual((rec["marks"], rec["judgment"], rec["passes"]), (None, [], {}))

    # records sync to the handoff repository: they name this Mac by its machine id, never by its hostname, which tells
    # whose Mac it is (macOS names it after its owner) and changes with the network
    def test_the_audit_and_the_capture_name_this_mac_by_its_machine_id_and_hold_no_hostname(self):
        c = self.shot(check=False)
        self.assertEqual((c["by"]["host"], c["image"]["where"]), ("m-test0001", "local:m-test0001"))
        self.assertEqual(self.record(c["audit"])["by"]["host"], "m-test0001")
        for path in self.records.rglob("*.json"):
            self.assertNotIn(HOST, path.read_text(), path.name)

    # without a machine id (devkit cannot write its state folder), identity.machine names this Mac by its hostname;
    # a record then names no Mac at all
    def test_a_capture_names_no_mac_when_this_mac_has_no_machine_id(self):
        self.patched(identity, "machine", new=lambda now=None: identity.Machine(HOST, HOST, {HOST: [[0, 4e9]]}))
        c = self.shot(check=False)
        self.assertEqual((c["by"]["host"], c["image"]["where"]), (None, None))
        for path in self.records.rglob("*.json"):
            self.assertNotIn(HOST, path.read_text(), path.name)

    def test_a_capture_takes_every_viewport_and_theme_of_the_audits_matrix(self):
        store.start(P, "screen", "Q?", viewports=["1440x900", "390x844"], themes=["light", "dark"])
        got = store.capture(P, self.url, state="list", check=False)
        self.assertEqual(sorted((tuple(c["recipe"]["viewport"]), c["recipe"]["theme"]) for c in got),
                         [((390, 844), "dark"), ((390, 844), "light"), ((1440, 900), "dark"), ((1440, 900), "light")])
        self.assertEqual(len({c["id"] for c in got}), 4)
        self.assertEqual(sorted(p.stem for p in (self.records / "captures").glob("*.json")),
                         sorted(c["id"] for c in got))

    def test_no_capture_without_an_audit_open_in_this_session(self):
        with self.assertRaises(store.VisualError) as e:
            store.capture(P, self.url)
        self.assertEqual(str(e.exception), "No audit is open in this session. For one screen: dev visual screen <url>. "
                                           "For more: dev visual start --scope <scope> \"<question>\"")
        self.assertFalse((self.records / "captures").exists())

    def test_a_remote_host_needs_remote_and_nothing_is_written(self):
        # VD-AC-12
        store.start(P, "screen", "Q?")
        with self.assertRaises(store.VisualError) as e:
            store.capture(P, "https://app.example.com/invoices")
        self.assertEqual(str(e.exception), "app.example.com is not on this Mac. Capture it with --remote, and only "
                                           "with demo or fixture data.")
        self.assertFalse((self.records / "captures").exists())
        self.assertFalse((self.local / "blobs").exists())

    def test_the_store_keeps_folders_0700_and_files_0600(self):
        # VD-AC-8: and the handoff repository holds only JSON records for the capture
        self.shot()
        found = list(self.local.rglob("*"))
        self.assertTrue(any(p.suffix == ".png" for p in found))
        for p in [self.local, *found]:
            want = 0o700 if p.is_dir() else 0o600
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), want, p)
        self.assertEqual({p.suffix for p in self.records.rglob("*") if p.is_file()}, {".json"})
        self.assertNotIn("hunter2-visual", "".join(p.read_text() for p in self.records.rglob("*.json")))

    def test_the_captures_temporary_folder_is_gone_after_success_and_after_failure(self):
        self.shot(check=False)
        self.assertEqual(list(self.scratch.iterdir()), [])
        with self.assertRaises(store.VisualError):
            store.capture(P, (self.tmp / "missing.html").as_uri(), check=False)
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertEqual(len(list((self.records / "captures").glob("*.json"))), 1)

    def test_a_capture_attaches_to_the_one_node_its_route_matches(self):
        # VD-AC-51, R50: the route pattern of the URL, its state, and the map version; a URL no node matches stays
        # unattached; a node the map lacks is refused with the nearest one
        vmap.add_node(P, "invoices.list", "Invoices", route="/invoices.html", by="sati", why="declared")
        vmap.add_state(P, "invoices.list", "list", by="sati", why="its states")
        c = self.shot(check=False)
        self.assertEqual(c["source"]["route"], "/invoices.html")
        self.assertEqual({k: c["map"][k] for k in ("node", "state", "version")},
                         {"node": "invoices.list", "state": "list", "version": 2})
        self.assertTrue(ISO.match(c["map"]["attached"]))
        with self.assertRaises(store.VisualError) as e:
            store.capture(P, self.url, node="invoices.lst", check=False)
        self.assertIn("invoices.list", str(e.exception))
        unattached = store.capture(P, self.url.replace("invoices.html", "other.html"), check=False)
        self.assertEqual([x["map"]["node"] for x in unattached], [None])

    def test_capture_again_takes_the_same_recipe_and_points_at_the_first(self):
        first = self.shot(steps=["scroll 0"], check=False)
        again = store.capture_again(P, first["id"])[0]
        self.assertEqual(again["again_of"], first["id"])
        self.assertEqual(again["recipe"], first["recipe"])
        self.assertEqual(again["audit"], first["audit"])
        self.assertNotEqual(again["id"], first["id"])

    def test_signals_list_and_a_dismissal_triages_a_signal_once(self):
        c = self.shot()
        refs = [ref for ref, _s, _o in store.signals(P, c["id"], untriaged=True)]
        self.assertEqual(refs, [f"{c['id']}#{s['n']}" for s in c["checks"]["signals"]])
        self.assertIn("overlap", [s["check"] for s in c["checks"]["signals"]])
        first = refs[0]
        s = store.dismiss(P, first, "decorative overlap", model="human")
        self.assertEqual((s["triage"]["as"], s["triage"]["why"], s["triage"]["by"], s["triage"]["model"]),
                         ("dismissed", "decorative overlap", "devuser", "human"))
        self.assertTrue(ISO.match(s["triage"]["at"]))
        left = [ref for ref, _s, _o in store.signals(P, c["id"], untriaged=True)]
        self.assertEqual(left, refs[1:])
        with self.assertRaises(store.VisualError):
            store.dismiss(P, first, "again")
        with self.assertRaises(store.VisualError):
            store.dismiss(P, first.split("#")[0] + "#99", "no such signal")


@unittest.skipUnless(NODE and GITLEAKS, "node and gitleaks redact the text a record holds")
class ExternalPictureTest(StoreCase):
    def test_a_picture_file_is_an_external_capture_with_no_recipe(self):
        # VD-AC-7, R9
        shot = self.tmp / "shot.png"
        shot.write_bytes(png(40, 30))
        store.start(P, "screen", "What did the customer see?")
        c = store.capture(P, str(shot))[0]
        self.assertEqual((c["kind"], c["recipe"], c["reproducible"]), ("image", None, False))
        self.assertEqual((c["image"]["width"], c["image"]["height"]), (40, 30))
        self.assertEqual(c["image"]["sha256"], hashlib.sha256(shot.read_bytes()).hexdigest())
        self.assertTrue((self.local / "blobs" / f"{c['image']['sha256']}.png").is_file())
        with self.assertRaises(store.VisualError) as e:
            store.capture_again(P, c["id"])
        self.assertEqual(str(e.exception), f"{c['id']} is an external picture: it has no recipe")


@unittest.skipUnless(BROWSERS, NO_BROWSER or "")
class PassAndFindingTest(StoreCase):
    def finding(self, c, **kw):
        args = dict(pass_="layout", rules=["wcag:2.5.8"], severity="P1", title="Export touches Amount",
                    impact="A clerk taps Amount when they mean Export.", evidence="Overlap 40×30 px; expected a gap",
                    fix="Give .bar a gap of var(--space-4).")
        args.update(kw)
        return store.add_finding(P, c["id"], **args)

    def test_a_pass_is_recorded_clean_or_na_on_its_capture_or_audit(self):
        c = self.shot(check=False)
        store.record_pass(P, c["id"], "hierarchy", clean="Export reads first")
        store.record_pass(P, c["id"], "states", na="one state captured")
        store.record_pass(P, c["audit"], "S5", clean="one screen")
        self.assertEqual(self.record(c["id"])["passes"], {"hierarchy": {"result": "clean", "note": "Export reads first"},
                                                          "states": {"result": "n/a", "note": "one state captured"}})
        self.assertEqual(self.record(c["audit"])["passes"], {"S5": {"result": "clean", "note": "one screen"}})
        for rid, pass_, kw, says in ((c["id"], "S1", {"clean": "x"}, "S1 is a structure pass"),
                                     (c["audit"], "layout", {"clean": "x"}, "layout is a screen pass"),
                                     (c["id"], "vibes", {"clean": "x"}, "task, hierarchy"),
                                     (c["id"], "layout", {}, "--clean"),
                                     (c["id"], "layout", {"clean": "a", "na": "b"}, "--clean")):
            with self.subTest(pass_=pass_, kw=kw):
                with self.assertRaises(store.VisualError) as e:
                    store.record_pass(P, rid, pass_, **kw)
                self.assertIn(says, str(e.exception))

    def test_a_finding_on_an_element_takes_its_box_selector_and_locator(self):
        # R20, R22: every field of the record, the element's anchor, the guide, and the pass it marks
        c = self.shot(check=False)
        e = self.element(c, '[data-testid="export"]')
        f = self.finding(c, element=e["id"], guide="dialog-element")
        self.assertTrue(ID.match(f["id"]) and f["id"].startswith("VF-"))
        rec = self.record(f["id"])
        self.assertEqual(rec, f)
        self.assertEqual({k: rec["anchor"][k] for k in ("element", "region", "selector", "locator")},
                         {"element": e["id"], "region": e["box"], "selector": '[data-testid="export"]',
                          "locator": "getByTestId('export')"})
        self.assertEqual(rec["anchor"]["name"], 'button "Export"')
        for key, want in (("audit", c["audit"]), ("capture", c["id"]), ("pass", "layout"), ("rule", ["wcag:2.5.8"]),
                          ("severity", "P1"), ("status", "open"), ("source", "judgment"), ("guide", "dialog-element"),
                          ("evidence_captures", [c["id"]]), ("root", None), ("verification", None)):
            self.assertEqual(rec[key], want, key)
        self.assertEqual([(h["by"], h["change"]) for h in rec["history"]], [("devuser", "open")])
        self.assertEqual(self.record(c["id"])["passes"]["layout"], {"result": "findings"})

    def test_a_finding_is_refused_when_its_anchor_or_rule_does_not_hold(self):
        # VD-AC-14, VD-AC-66: each refusal says why, and nothing is written
        c = self.shot(check=False)
        elements = self.description(c)["elements"]
        buttons = sum(1 for e in elements if e["role"] == "button")
        cases = (({"element": "E9999"}, f"E9999 is not in the description of {c['id']} (E1–E{len(elements)}). List "
                                        f"them: dev visual describe {c['id']} --interactive"),
                 ({"region": [380, 10, 40, 40]}, "The region 380,10,40,40 ends at x 420; the capture is 390 px wide."),
                 ({"selector": "role=button"}, f'"role=button" matches {buttons} elements in {c["id"]}.'),
                 ({"selector": "button.primary"}, f'"button.primary" names no element of the description of {c["id"]}'),
                 ({"region": [0, 0, 10, 10], "rules": ["wcag:9.9.9"]}, "wcag:9.9.9"),
                 ({"region": [0, 0, 10, 10], "rules": ["vibes:1"]}, "vibes:1"),
                 ({"region": [0, 0, 10, 10], "severity": "P5"}, "P0, P1, P2, P3"),
                 ({"region": [0, 0, 10, 10], "pass_": "S2"}, "structure pass"),
                 ({}, "A screen finding needs a region"),
                 ({"region": [0, 0, 10, 10], "title": " "}, "--title"))
        for kw, says in cases:
            with self.subTest(kw=kw):
                with self.assertRaises(store.VisualError) as e:
                    self.finding(c, **kw)
                self.assertIn(says, str(e.exception))
        self.assertFalse((self.records / "findings").exists())

    def test_a_finding_from_a_signal_copies_its_anchor_and_triages_it(self):
        c = self.shot()
        s = next(s for s in c["checks"]["signals"] if s["check"] == "overlap")
        ref = f"{c['id']}#{s['n']}"
        f = store.add_finding(P, from_signal=ref, pass_="layout", severity="P1", title="Export touches Amount",
                              impact="A clerk taps Amount.", fix="Space them.")
        self.assertEqual((f["capture"], f["source"], f["signal"]), (c["id"], "check:overlap", ref))
        self.assertEqual(f["anchor"]["region"], s["anchor"]["region"])
        self.assertEqual(f["anchor"]["element"], s["anchor"]["element"])
        self.assertIn(s["measured"], f["evidence"])
        self.assertEqual(f["rule"], s["rule"])
        triage = next(x for x in self.record(c["id"])["checks"]["signals"] if x["n"] == s["n"])["triage"]
        self.assertEqual((triage["as"], triage["finding"], triage["by"]), ("finding", f["id"], "devuser"))
        self.assertNotIn(ref, [r for r, _s, _o in store.signals(P, c["id"], untriaged=True)])

    def test_a_structure_finding_needs_a_capture_as_evidence(self):
        # VD-AC-40
        vmap.add_node(P, "home", "Home", route="/", by="sati", why="start")
        audit = store.start(P, "product", "Q?")
        doc = self.record(audit["id"])
        doc["signals"] = [{"n": 1, "check": "orphan", "pass": "S2", "rule": ["map:orphan"],
                           "anchor": {"journey": None, "step": None, "edge": None, "node": "home", "state": None},
                           "measured": "home has no link from the start", "threshold": None, "triage": None,
                           "duplicates": []}]
        store.save(P, doc)
        kw = dict(from_signal=f"{audit['id']}#1", pass_="S2", severity="P2", title="Home is an orphan",
                  impact="Nobody finds it.", fix="Link it from the nav.")
        with self.assertRaises(store.VisualError) as e:
            store.add_finding(P, **kw)
        self.assertEqual(str(e.exception), "A structure finding needs at least one capture as evidence")
        c = store.capture(P, self.url, viewports=["390x844"], check=False)[0]
        f = store.add_finding(P, evidence_captures=[c["id"]], **kw)
        self.assertEqual((f["anchor"]["node"], f["capture"], f["source"]), ("home", None, "map:orphan"))
        self.assertEqual(self.record(audit["id"])["passes"]["S2"], {"result": "findings"})

    def test_finding_states_follow_the_table_and_keep_every_change(self):
        # VD-AC-19, R24, R25
        c = self.shot(check=False)
        f = self.finding(c, region=[0, 0, 100, 40])
        with self.assertRaises(store.VisualError) as e:
            store.set_finding(P, f["id"], status="verified", why="looks right")
        self.assertEqual(str(e.exception), "Only dev visual verify marks a finding verified")
        store.set_finding(P, f["id"], status="fixing", why="on it")
        store.set_finding(P, f["id"], status="fixed", why="margin added", commit="4f2a9c1")
        with self.assertRaises(store.VisualError):
            store.set_finding(P, f["id"], status="fixing", why="back")  # fixed waits for verify
        store.close_finding(P, f["id"], "not-a-problem", "the margin is the design's")  # or closes without one
        with self.assertRaises(store.VisualError) as e:
            store.close_finding(P, f["id"], "wontfix", "not now")
        self.assertIn("is not-a-problem: a finding is closed from open, fixing, reopened, or fixed", str(e.exception))
        g = self.finding(c, region=[0, 0, 100, 40], title="Second")
        with self.assertRaises(store.VisualError) as e:
            store.close_finding(P, g["id"], "wontfix", "later")
        self.assertIn("--decision", str(e.exception))
        store.close_finding(P, g["id"], "wontfix", "later", decision="D7")
        store.set_finding(P, g["id"], status="open", why="the decision changed")
        rec = self.record(g["id"])
        self.assertEqual(rec["status"], "reopened")
        self.assertEqual([h["change"] for h in rec["history"]], ["open", "wontfix", "reopened"])
        self.assertEqual(rec["history"][1]["decision"], "D7")
        self.assertEqual([h["change"] for h in self.record(f["id"])["history"]],
                         ["open", "fixing", "fixed", "not-a-problem"])
        self.assertEqual(self.record(f["id"])["history"][2]["commit"], "4f2a9c1")
        h = self.finding(c, region=[0, 0, 100, 40], title="Third", severity="P3")
        store.set_finding(P, h["id"], severity="P2", why="it blocks the phone layout")
        store.set_finding(P, h["id"], root=f["id"])
        self.assertEqual((self.record(h["id"])["severity"], self.record(h["id"])["root"]), ("P2", f["id"]))
        with self.assertRaises(store.VisualError):
            store.set_finding(P, h["id"], root="VF-g33nothere1")

    def test_ls_counts_the_open_findings_of_each_audit_by_severity(self):
        c = self.shot(check=False)
        for sev in ("P1", "P1", "P3"):
            self.finding(c, region=[0, 0, 10, 10], severity=sev)
        closed = self.finding(c, region=[0, 0, 10, 10], severity="P0")
        store.close_finding(P, closed["id"], "not-a-problem", "the design's badge")
        rows = store.audits(P)
        self.assertEqual([(r["audit"]["id"], r["open"]) for r in rows],
                         [(c["audit"], {"P0": 0, "P1": 2, "P2": 0, "P3": 1})])
        # R58: a screen audit counts screen passes 1-10 on each capture; the findings recorded `layout`
        self.assertEqual(rows[0]["passes"], {"recorded": 1, "needed": 10})
        store.record_pass(P, c["id"], "task", clean="the list reads first")
        store.record_pass(P, c["audit"], "S2", clean="one screen")  # a structure pass a screen audit does not count
        self.assertEqual(store.audits(P)[0]["passes"], {"recorded": 2, "needed": 10})
        self.assertEqual([f["id"] for f in store.findings(P, severity=["P0"])], [closed["id"]])
        self.assertEqual(len(store.findings(P, status=["open"])), 3)


class RecordsCase(StoreCase):
    # records written as a sync brings them in, with no capture behind them
    A, B = "VA-g33z0a1b2c", "VA-g33z0a9z9z"

    def put(self, doc: dict) -> dict:
        folder = {"VA": "audits", "VC": "captures", "VF": "findings"}[doc["id"][:2]]
        path = self.records / folder / f"{doc['id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc))
        return doc

    def signal(self, n, check, triage=None, **extra):
        return {"n": n, "check": check, "pass": "responsive", "rule": ["wcag:2.5.8"],
                "anchor": {"element": f"E{n}", "region": [0, 0, 30, 30], "selector": f"#b{n}", "name": None},
                "measured": "30×30 px", "threshold": "44×44 px", "triage": triage, "duplicates": [], **extra}


TAKEN = {"as": "finding", "finding": "VF-g33z0f1n1d", "why": None, "by": "sati", "model": None,
         "at": "2026-11-18T10:00:00Z"}


@unittest.skipUnless(NODE and GITLEAKS, "node and gitleaks check a reason for secrets")
class BulkDismissTest(RecordsCase):
    def setUp(self):
        super().setUp()
        self.put({"id": self.A, "signals": [{**self.signal(1, "depth"), "pass": "S2"}]})
        self.put({"id": self.B, "signals": []})
        self.put({"id": "VC-g33z0c1a1a", "audit": self.A, "checks": {"signals": [
            self.signal(1, "tap-target"), self.signal(2, "tap-target", TAKEN), self.signal(3, "overlap"),
            self.signal(4, "axe", axe_rule="target-size"), self.signal(5, "axe", axe_rule="color-contrast")]}})
        self.put({"id": "VC-g33z0c2b2b", "audit": self.A, "checks": {"signals": [self.signal(1, "tap-target")]}})
        self.put({"id": "VC-g33z0c3c3c", "audit": self.B, "checks": {"signals": [self.signal(1, "tap-target")]}})

    def triage(self, ref):
        return store._signal(P, ref)[1]["triage"]

    def test_every_untriaged_signal_of_one_check_in_the_audit_is_dismissed_with_the_reason_and_who(self):
        refs = store.dismiss_all(P, "tap-target", audit=self.A, why="advice under 44 px at 390 px", model="human")
        self.assertEqual(refs, ["VC-g33z0c1a1a#1", "VC-g33z0c2b2b#1"])
        for ref in refs:
            t = self.triage(ref)
            self.assertEqual((t["as"], t["why"], t["by"], t["model"]),
                             ("dismissed", "advice under 44 px at 390 px", "devuser", "human"))
            self.assertTrue(ISO.match(t["at"]))
        # a signal triaged before, a signal of another check, and a signal of another audit stay as they were
        self.assertEqual(self.triage("VC-g33z0c1a1a#2"), TAKEN)
        self.assertEqual([self.triage(r) for r in ("VC-g33z0c1a1a#3", "VC-g33z0c3c3c#1", f"{self.A}#1")],
                         [None, None, None])

    def test_a_rule_narrows_the_check_and_the_audit_open_in_the_session_is_the_default(self):
        store._open(P, self.A)
        self.assertEqual(store.dismiss_all(P, "axe", rule="target-size", why="the pager is spaced"),
                         ["VC-g33z0c1a1a#4"])
        self.assertIsNone(self.triage("VC-g33z0c1a1a#5"))
        self.assertEqual(store.dismiss_all(P, "depth", why="the settings sit deep on purpose"), [f"{self.A}#1"])
        self.assertEqual(store.dismiss_all(P, "overlap", rule="wcag:1.4.3", why="no such rule here"), [])

    def test_a_check_that_is_not_one_a_blank_reason_or_no_audit_is_refused_and_nothing_is_dismissed(self):
        cases = ((("tap-targt",), {"audit": self.A, "why": "x"}, '"tap-targt" is not a check. Nearest: tap-target.'),
                 (("tap-target",), {"audit": self.A, "why": "  "}, "--why"),
                 (("tap-target",), {"audit": "VC-g33z0c1a1a", "why": "x"}, "--audit takes an audit (VA-…)"),
                 (("tap-target",), {"why": "x"}, "No audit is open in this session"))
        for args, kw, says in cases:
            with self.subTest(args=args, kw=kw):
                with self.assertRaises(store.VisualError) as e:
                    store.dismiss_all(P, *args, **kw)
                self.assertIn(says, str(e.exception))
        self.assertIsNone(self.triage("VC-g33z0c1a1a#1"))


@unittest.skipUnless(NODE and GITLEAKS, "node and gitleaks redact the text a record holds")
class LaneAuditTest(RecordsCase):
    # the crew members of one agent session share its session id: each lane keeps its own open audit, so a capture
    # or a bulk dismissal without --audit never reaches the audit another member's lane opened
    def as_lane(self, lane):
        return mock.patch.object(store, "_lane", return_value=lane)

    def test_a_lane_without_an_audit_of_its_own_has_none_while_another_lane_of_the_session_has_one(self):
        with self.as_lane("a1sati"):
            sati = store.start(P, "screen", "Is the invoice list usable?")["id"]
        with self.as_lane("a2trinity"):
            self.assertIsNone(store.open_audit(P))
            with self.assertRaises(store.VisualError) as e:
                store.capture(P, self.url)
            self.assertIn("No audit is open", str(e.exception))
            trinity = store.start(P, "screen", "Is the tile readable at 390 px?")["id"]
            self.assertEqual(store.open_audit(P), trinity)
        with self.as_lane("a1sati"):
            self.assertEqual(store.open_audit(P), sati)
        self.assertIsNone(store.open_audit(P))  # the tech lead opened none in its own lane

    def test_a_bulk_dismissal_without_audit_dismisses_only_the_signals_of_its_own_lanes_audit(self):
        with self.as_lane("a1sati"):
            sati = store.start(P, "screen", "Is the invoice list usable?")["id"]
        with self.as_lane("a2trinity"):
            trinity = store.start(P, "screen", "Is the tile readable at 390 px?")["id"]
        self.put({"id": "VC-g33z0c4d4d", "audit": sati, "checks": {"signals": [self.signal(1, "tap-target")]}})
        self.put({"id": "VC-g33z0c5e5e", "audit": trinity, "checks": {"signals": [self.signal(1, "tap-target")]}})
        with self.as_lane("a1sati"):
            self.assertEqual(store.dismiss_all(P, "tap-target", why="advice under 44 px at 390 px"),
                             ["VC-g33z0c4d4d#1"])
        self.assertIsNone(store._signal(P, "VC-g33z0c5e5e#1")[1]["triage"])

    # a start that cannot tell which lane runs it refuses before it writes: no audit record is left that no lane has
    # open, however often it is tried, and the refusal names no --audit, an option start does not take
    def test_a_start_that_cannot_tell_its_lane_writes_no_audit_and_names_no_audit_option(self):
        from devkit import progress
        refusal = identity.IdentityError("the open calls of 2 crew members run this same command")
        with mock.patch.object(progress, "_as_lane", side_effect=refusal):
            for _ in range(2):
                with self.assertRaises(store.VisualError) as e:
                    store.start(P, "screen", "Is the invoice list usable?")
        self.assertIn("2 crew members", str(e.exception))
        self.assertNotIn("--audit", str(e.exception))
        self.assertEqual(sorted(p.name for p in self.records.rglob("VA-*.json")), [])


class LaneLookupTest(unittest.TestCase):
    # the lane a session audit belongs to comes from devkit's identity; when devkit cannot tell which of several
    # members runs the command, the lookup refuses instead of taking the tech lead's audit
    def test_the_lane_is_the_members_agent_id_none_for_the_tech_lead_and_refused_when_devkit_cannot_tell(self):
        from devkit import progress
        with mock.patch.object(progress, "_as_lane", return_value="a1b2/c3"):
            self.assertEqual(store._lane(), "a1b2_c3")
        with mock.patch.object(progress, "_as_lane", return_value=None):
            self.assertIsNone(store._lane())
        refusal = identity.IdentityError("the open calls of 2 crew members run this same command")
        with mock.patch.object(progress, "_as_lane", side_effect=refusal):
            with self.assertRaises(store.VisualError) as e:
                store._lane()
        self.assertIn("2 crew members", str(e.exception))
        self.assertIn("--audit VA-", str(e.exception))


class CoveredPassTest(RecordsCase):
    URL = "http://127.0.0.1:4200/invoices"

    def capture(self, cid, audit, viewport, theme="light", url=URL):
        return self.put({"id": cid, "audit": audit, "kind": "web", "state": "list", "element": None,
                         "source": {"url": url, "origin": "local", "route": url.rpartition("4200")[2]},
                         "recipe": {"viewport": viewport, "dpr": 1, "theme": theme, "locale": "en-US",
                                    "timezone": "UTC", "clock": None, "full_page": True, "steps": ["click #month"],
                                    "script": None, "storage_state": None, "data": None, "css": None,
                                    "emulation": {"forced_colors": False, "contrast": None, "vision": None}},
                         "image": {"width": viewport[0], "height": 2000, "fold": viewport[1], "scale": 1},
                         "passes": {}})

    def finding(self, fid, audit, capture, pass_="layout"):
        return self.put({"id": fid, "audit": audit, "capture": capture, "pass": pass_, "status": "open",
                         "anchor": {}, "history": []})

    def setUp(self):
        super().setUp()
        self.wide = self.capture("VC-g33z0c1w1d", self.A, [1440, 900])["id"]
        self.phone = self.capture("VC-g33z0c2p2h", self.A, [390, 844], theme="dark")["id"]
        self.finding("VF-g33z0f1w1d", self.A, self.wide)

    def test_a_pass_is_covered_by_a_finding_on_the_same_recipe_at_another_width_or_theme(self):
        doc = store.record_pass(P, self.phone, "layout", covered_by="VF-g33z0f1w1d")
        self.assertEqual(doc["passes"]["layout"], {"result": "findings", "covered_by": "VF-g33z0f1w1d",
                                                   "note": f"covered by VF-g33z0f1w1d on {self.wide} (1440×900 light)"})
        self.assertEqual(self.record(self.phone)["passes"], doc["passes"])

    def test_a_finding_of_another_audit_page_pass_or_capture_does_not_cover_a_pass(self):
        self.put({"id": self.A, "signals": []})
        self.finding("VF-g33z0f2o2a", self.B, self.capture("VC-g33z0c3o3a", self.B, [1440, 900])["id"])
        self.finding("VF-g33z0f3p3g", self.A, self.capture("VC-g33z0c4p4g", self.A, [1440, 900],
                                                            url=self.URL + "/7")["id"])
        self.finding("VF-g33z0f4s4f", self.A, self.phone)
        self.finding("VF-g33z0f5m5p", self.A, None, pass_="S2")
        cases = (("VF-g33z0f2o2a", "layout", f"VF-g33z0f2o2a belongs to audit {self.B}, and {self.phone} to "
                                             f"{self.A}: a pass is covered only by a finding of its own audit."),
                 ("VF-g33z0f3p3g", "layout", f"VF-g33z0f3p3g sits on VC-g33z0c4p4g, which is not {self.phone}'s "
                                             "recipe at another width or theme"),
                 ("VF-g33z0f1w1d", "color", "VF-g33z0f1w1d is a layout finding: it covers the layout pass, not "
                                            "color."),
                 ("VF-g33z0f4s4f", "layout", f"VF-g33z0f4s4f sits on {self.phone} itself"),
                 ("VF-g33z0f5m5p", "layout", "VF-g33z0f5m5p is a structure finding: it sits on no capture"))
        for fid, pass_, says in cases:
            with self.subTest(fid=fid, pass_=pass_):
                with self.assertRaises(store.VisualError) as e:
                    store.record_pass(P, self.phone, pass_, covered_by=fid)
                self.assertIn(says, str(e.exception))
        with self.assertRaises(store.VisualError) as e:
            store.record_pass(P, self.A, "S2", covered_by="VF-g33z0f5m5p")
        self.assertIn("--covered-by records a screen pass on a capture (VC-…)", str(e.exception))
        with self.assertRaises(store.VisualError) as e:
            store.record_pass(P, self.phone, "layout", clean="ok", covered_by="VF-g33z0f1w1d")
        self.assertIn("one of them", str(e.exception))
        self.assertEqual(self.record(self.phone)["passes"], {})


@unittest.skipUnless(BROWSERS and CWEBP and DWEBP, NO_BROWSER or "cwebp and dwebp come with brew install webp")
class ShareTest(StoreCase):
    def ppm(self, webp: Path):
        out = self.tmp / "decoded.ppm"
        subprocess.run([DWEBP, str(webp), "-ppm", "-o", str(out)], check=True, capture_output=True)
        data = out.read_bytes()
        _magic, w, h, _max, rest = data.split(maxsplit=4)
        return int(w), int(h), rest

    def test_share_copies_a_findings_picture_as_webp_with_its_masks(self):
        # VD-AC-30: the WebP is at most 400 KB, the masked box stays painted over, and the history names who shared
        c = self.shot(check=False)
        box = self.element(c, "#password")["box"]
        f = store.add_finding(P, c["id"], region=[0, 0, 100, 40], pass_="layout", rules=["nielsen:8"],
                              severity="P2", title="t", impact="i", evidence="e", fix="x")
        out = store.share(P, f["id"])
        sha = c["image"]["sha256"]
        webp = self.records / "media" / f"{sha}.webp"
        self.assertEqual(out["media"], [webp])
        self.assertLessEqual(webp.stat().st_size, 400 * 1024)
        w, h, pixels = self.ppm(webp)
        self.assertEqual((w, h), (c["image"]["width"], c["image"]["height"]))
        x0, y0, bw, bh = box
        inner = [pixels[3 * (y * w + x) + i] for y in range(y0 + 3, y0 + bh - 3) for x in range(x0 + 3, x0 + bw - 3)
                 for i in range(3)]
        self.assertLess(max(inner), 40)
        last = self.record(f["id"])["history"][-1]
        self.assertEqual((last["change"], last["by"], last["pictures"], last["host"]),
                         ("shared", "devuser", [sha], "m-test0001"))

    def test_share_refuses_a_changed_picture_and_a_remote_capture(self):
        c = self.shot(check=False)
        f = store.add_finding(P, c["id"], region=[0, 0, 100, 40], pass_="layout", rules=["nielsen:8"],
                              severity="P2", title="t", impact="i", evidence="e", fix="x")
        blob = self.local / "blobs" / f"{c['image']['sha256']}.png"
        data = bytearray(blob.read_bytes())
        data[-20] ^= 1
        os.chmod(blob, 0o600)
        blob.write_bytes(bytes(data))
        with self.assertRaises(store.VisualError) as e:
            store.share(P, f["id"])
        self.assertIn("does not match its record", str(e.exception))
        rec = self.record(c["id"])
        rec["source"]["origin"] = "remote"
        store.save(P, rec)
        with self.assertRaises(store.VisualError) as e:
            store.share(P, f["id"])
        self.assertIn("remote", str(e.exception))
        self.assertFalse((self.records / "media").exists())

    # a picture taken on another Mac is not here to share: the refusal names that Mac as the dashboard does, its
    # name with its machine id, never the record's bare local:<id>
    def test_share_names_the_mac_a_missing_picture_is_on(self):
        c = self.shot(check=False)
        f = store.add_finding(P, c["id"], region=[0, 0, 100, 40], pass_="layout", rules=["nielsen:8"],
                              severity="P2", title="t", impact="i", evidence="e", fix="x")
        rec = self.record(c["id"])
        rec["image"]["where"] = "local:m-studio01"
        store.save(P, rec)
        (self.local / "blobs" / f"{c['image']['sha256']}.png").unlink()
        self.patched(work, "PRESENCE", new=self.tmp / "presence")
        presence(self.tmp / "presence", "bob@m-studio01", "studio")
        with self.assertRaises(store.VisualError) as e:
            store.share(P, f["id"])
        self.assertEqual(str(e.exception), f"The picture of {c['id']} is not on this Mac: it was taken on another Mac "
                                           f"(m-studio01). Share it from there, or capture it here: dev visual capture "
                                           f"--again {c['id']}")


@unittest.skipUnless(BROWSERS, NO_BROWSER or "")
class SharePrunedHereTest(StoreCase):
    # This Mac took the capture and later pruned its picture from its store: the record names this Mac, by its
    # machine id or, from before the machine id, by the hostname it carried then. share says the picture is no
    # longer in this Mac's store, never that it was taken on <this Mac> and is not on this Mac, and that nothing was
    # shared; a new capture would be another picture, so it names none
    def test_share_says_a_picture_this_mac_pruned_is_no_longer_in_its_store(self):
        c = self.shot(check=False)
        f = store.add_finding(P, c["id"], region=[0, 0, 100, 40], pass_="layout", rules=["nielsen:8"],
                              severity="P2", title="t", impact="i", evidence="e", fix="x")
        (self.local / "blobs" / f"{c['image']['sha256']}.png").unlink()
        for where in ("local:m-test0001", f"local:{HOST}"):
            with self.subTest(where=where):
                rec = self.record(c["id"])
                rec["image"]["where"] = where
                store.save(P, rec)
                with self.assertRaises(store.VisualError) as e:
                    store.share(P, f["id"])
                self.assertEqual(str(e.exception), f"The picture of {c['id']} is no longer in this Mac's store. "
                                                   "Nothing was shared.")
        self.assertFalse((self.records / "media").exists())


class TakenHereByUserTest(StoreCase):
    # A record from before the machine id, or written while this Mac had none (identity.machine falls back to the
    # hostname), names its Mac by a hostname, and two Macs can carry one short hostname at the same time (a default
    # or a DHCP name). Such a capture is this Mac's only when its user is this user too, as work.mine reads an older
    # record: another person's capture in the same period reads as taken on that Mac, never as pruned from this
    # Mac's store. A machine id names one Mac, whoever took the capture on it
    def test_a_hostname_names_this_mac_only_for_this_user_and_a_machine_id_for_anyone(self):
        renamed = calendar.timegm((2026, 6, 1, 0, 0, 0))
        renamed_mac = identity.Machine("m-test0001", "ana-studio", {
            "mac-55": [[calendar.timegm((2026, 1, 1, 0, 0, 0)), renamed]], "ana-studio": [[renamed, 4e9]]})
        no_id = identity.Machine("mac-55", "mac-55", {"mac-55": [[0, 4e9]]})
        said = {}
        for mac, where in ((renamed_mac, "mac-55"), (no_id, "mac-55"), (renamed_mac, "m-test0001")):
            self.patched(identity, "machine", new=lambda now=None, m=mac: m)
            for user in ("devuser", "bob"):
                c = {"id": "VC-g33z1a8k0e", "image": {"where": f"local:{where}"}, "taken": "2026-03-01T00:00:00Z",
                     "by": {"member": None, "user": user, "host": where, "session": None}}
                said[(mac.id, where, user)] = (store.taken_here(c), store.not_here(c, "picture"))
        here = (True, "The picture of VC-g33z1a8k0e is no longer in this Mac's store")
        away = (False, "The picture of VC-g33z1a8k0e is not on this Mac: it was taken on mac-55")
        self.assertEqual(said, {("m-test0001", "mac-55", "devuser"): here, ("m-test0001", "mac-55", "bob"): away,
                                ("mac-55", "mac-55", "devuser"): here, ("mac-55", "mac-55", "bob"): away,
                                ("m-test0001", "m-test0001", "devuser"): here,
                                ("m-test0001", "m-test0001", "bob"): here})


@unittest.skipUnless(BROWSERS, NO_BROWSER or "")
class FindingOnADescriptionNotHereTest(StoreCase):
    # A finding on an element needs the capture's description: when it is not in this Mac's store, the refusal names
    # where it is as every other command does (not_here), gone from this Mac's store or on the Mac that took it
    def test_a_finding_on_an_element_names_where_the_description_is(self):
        c = self.shot(check=False)
        (self.local / "desc" / f"{c['description']['sha256']}.json.gz").unlink()
        self.patched(work, "PRESENCE", new=self.tmp / "presence")
        presence(self.tmp / "presence", "bob@m-studio01", "studio")
        said = {}
        for where in ("m-test0001", "m-studio01"):
            rec = self.record(c["id"])
            rec["image"]["where"] = f"local:{where}"
            store.save(P, rec)
            with self.assertRaises(store.VisualError) as e:
                store.add_finding(P, c["id"], element="E4", pass_="layout", rules=["nielsen:8"], severity="P2",
                                  title="t", impact="i", evidence="e", fix="x")
            said[where] = str(e.exception)
        then = f"so its elements cannot be read. Capture it here: dev visual capture --again {c['id']}"
        self.assertEqual(said, {
            "m-test0001": f"The description of {c['id']} is no longer in this Mac's store, {then}",
            "m-studio01": f"The description of {c['id']} is not on this Mac: it was taken on another Mac (m-studio01), {then}"})


# --------------------------------------------------------------------------- records read, replays, the driver

ESC = "\x1b]52;c;cHduZWQ=\x07\u202e"
DESIGN = ROOT / "tests" / "fixtures" / "visual" / "describe" / "DESIGN.md"


def project_repo(folder: Path) -> Path:
    folder.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(folder)], check=True)
    return folder


class SyncedRecordTest(StoreCase):
    def write(self, folder: str, doc: dict) -> Path:
        path = self.records / folder / f"{doc['id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc))
        return path

    def test_a_record_a_sync_brought_in_reads_back_without_control_or_bidi_characters(self):
        # M5: a teammate's record edited by hand never carries a terminal escape into what devkit prints
        cid = store.new_id("VC")
        self.write("captures", {"id": cid, "audit": "VA-g33z0a1b2c", "state": f"ok{ESC}evil",
                                "checks": {"signals": [{"n": 1, "me\x07\u202easured": f"M{ESC}"}]}})
        doc = store.load(P, cid)
        self.assertEqual(doc["state"], "ok]52;c;cHduZWQ=evil")
        self.assertEqual(doc["checks"]["signals"][0], {"n": 1, "measured": "M]52;c;cHduZWQ="})
        self.assertEqual(store._all(P, "VC")[0]["state"], "ok]52;c;cHduZWQ=evil")

    def test_the_report_and_the_map_read_synced_files_without_control_characters(self):
        aid = "VA-g33z0a1b2c"
        self.write("audits", {"id": aid, "scope": "screen", "question": f"Q{ESC}", "include": []})
        self.write("captures", {"id": "VC-g33z0a4d5e", "audit": aid, "state": f"s{ESC}"})
        loaded = report.load(self.records, self.local, aid)
        self.assertEqual((loaded["audit"]["question"], loaded["captures"][0]["state"]),
                         ("Q]52;c;cHduZWQ=", "s]52;c;cHduZWQ="))
        (self.records / "map").mkdir(parents=True)
        (self.records / "map" / "current.json").write_text(json.dumps(
            {"version": 1, "nodes": [{"id": "home", "title": f"Home{ESC}", "kind": "screen"}]}))
        self.assertEqual(vmap.load(P)["nodes"][0]["title"], "Home]52;c;cHduZWQ=")

    def test_the_store_source_holds_no_raw_bidirectional_or_control_character(self):
        # a raw U+202E in a pattern reorders what a reviewer reads; the pattern names it as an escape
        text = (ROOT / "lib" / "devkit" / "visual" / "store.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search("[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u061c\u200e\u200f\u202a-\u202e"
                                    "\u2066-\u2069]", text))
        self.assertEqual(store._strip(f"a\u061cb\u200ec\u202ad\u2069e\x1bf\x9fg\th\n"), "abcdefg\th\n")


class ReplayTest(StoreCase):
    def recorded(self, **recipe) -> dict:
        cid = store.new_id("VC")
        rec = {"id": cid, "audit": "VA-g33z0a1b2c", "kind": "web", "state": None, "map": None, "element": None,
               "source": {"url": self.url, "origin": "local", "route": "/invoices.html"},
               "recipe": {"viewport": [390, 844], "dpr": 1, "theme": "light", "steps": [], "storage_state": None,
                          "emulation": {}, **recipe}}
        store.save(P, rec)
        return rec

    def test_a_replay_refuses_a_storage_state_outside_the_project_and_captures_nothing(self):
        # L6: a synced recipe cannot make this Mac read ~/.zshrc as a storage state
        repo = project_repo(self.tmp / "app")
        outside = self.tmp / "secrets.json"
        outside.write_text("{}")
        for given in (str(outside), "../secrets.json"):
            rec = self.recorded(storage_state=given)
            with self.subTest(given=given), mock.patch.object(store, "capture") as cap:
                with self.assertRaises(store.VisualError) as e:
                    store.capture_again(P, rec["id"], cwd=str(repo))
                self.assertIn(f"{rec['id']}'s recipe names the storage state {given}, outside this project",
                              str(e.exception))
                cap.assert_not_called()

    def test_a_replay_reads_a_storage_state_inside_the_project_by_its_full_path(self):
        repo = project_repo(self.tmp / "app")
        (repo / "e2e" / ".auth").mkdir(parents=True)
        (repo / "e2e" / ".auth" / "clerk.json").write_text("{}")
        rec = self.recorded(storage_state="e2e/.auth/clerk.json")
        with mock.patch.object(store, "capture", return_value=["done"]) as cap:
            self.assertEqual(store.capture_again(P, rec["id"], cwd=str(repo / "e2e")), ["done"])
        self.assertEqual(cap.call_args.kwargs["storage_state"], str(repo / "e2e" / ".auth" / "clerk.json"))

    def test_a_document_replay_reads_its_file_only_from_the_project(self):
        repo = project_repo(self.tmp / "app")
        (repo / "docs").mkdir()
        deck = repo / "docs" / "deck.pdf"
        deck.write_bytes(b"%PDF-1.4 deck")
        sha = hashlib.sha256(deck.read_bytes()).hexdigest()
        outside = self.tmp / "other.pdf"
        outside.write_bytes(b"%PDF-1.4 other")
        for given, expect in ((str(outside), None), ("deck.pdf", str(deck)), (str(deck), str(deck))):
            cid = store.new_id("VC")
            store.save(P, {"id": cid, "audit": "VA-g33z0a1b2c", "kind": "document", "state": None, "map": None,
                           "element": None, "source": {"file": given, "sha256": sha, "page": 2},
                           "recipe": {"page": 2}})
            with self.subTest(given=given), mock.patch.object(store, "capture", return_value=["done"]) as cap:
                if expect is None:
                    with self.assertRaises(store.VisualError) as e:
                        store.capture_again(P, cid, cwd=str(repo))
                    self.assertIn("outside this project", str(e.exception))
                    cap.assert_not_called()
                else:
                    store.capture_again(P, cid, cwd=str(repo))
                    self.assertEqual(cap.call_args.args[1], expect)

    def test_a_document_named_by_its_base_name_is_refused_when_missing_or_ambiguous(self):
        repo = project_repo(self.tmp / "app")
        cid = store.new_id("VC")
        store.save(P, {"id": cid, "audit": "VA-g33z0a1b2c", "kind": "document", "state": None, "map": None,
                       "element": None, "source": {"file": "terms.pdf", "sha256": "0" * 64, "page": 1},
                       "recipe": {"page": 1}})
        with mock.patch.object(store, "capture") as cap:
            with self.assertRaises(store.VisualError) as e:
                store.capture_again(P, cid, cwd=str(repo))
            self.assertIn("The document terms.pdf that", str(e.exception))
            self.assertIn("is not in this project", str(e.exception))
            for folder in ("a", "b"):
                (repo / folder).mkdir()
                (repo / folder / "terms.pdf").write_bytes(f"%PDF-1.4 {folder}".encode())
            with self.assertRaises(store.VisualError) as e:
                store.capture_again(P, cid, cwd=str(repo))
            self.assertIn("2 files of this project are named terms.pdf", str(e.exception))
            cap.assert_not_called()


def one_page_pdf(text: str) -> bytes:
    # a one-page PDF with one line of Helvetica, written by hand
    stream = f"BT /F1 18 Tf 72 700 Td ({text}) Tj ET\n".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> "
            b"/Contents 5 0 R >>", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


@unittest.skipUnless(BROWSERS and shutil.which("osascript"), NO_BROWSER or "PDF pages render through osascript")
class DocumentVerifyTest(StoreCase):
    def test_verify_recaptures_a_pdf_page_from_another_working_folder_of_the_project(self):
        # the record keeps the document's base name (no path of this Mac syncs); the replay finds it in the project.
        # The page count is PDFKit's through osascript, which this test process may not start; capture.cjs renders
        # the page through it in its own process.
        self.patched(store, "_page_count", return_value=1)
        repo = project_repo(self.tmp / "app")
        (repo / "docs").mkdir()
        (repo / "src").mkdir()
        (repo / "docs" / "terms.pdf").write_bytes(one_page_pdf("Terms of the October statement"))
        store.start(P, "screen", "Are the terms readable?", cwd=str(repo))
        before = store.capture(P, str(repo / "docs" / "terms.pdf"), cwd=str(repo))[0]
        self.assertEqual(before["source"]["file"], "terms.pdf")
        f = store.add_finding(P, before["id"], region=[0, 0, 200, 100], pass_="typography", rules=["nielsen:8"],
                              severity="P3", title="t", impact="i", evidence="e", fix="x")
        store.set_finding(P, f["id"], status="fixed", why="the terms use a larger size")
        [out] = store.verify(P, f["id"], cwd=str(repo / "src"))
        self.assertNotEqual(out["result"], "error", out["message"])
        after = self.record(out["verification"]["capture"])
        self.assertEqual((after["again_of"], after["source"]["file"], after["source"]["page"]),
                         (before["id"], "terms.pdf", 1))


def pages_pdf(*texts: str) -> bytes:
    # a PDF with one line of Helvetica on each page, written by hand
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(texts))).encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(texts)),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for i, text in enumerate(texts):
        stream = f"BT /F1 18 Tf 72 700 Td ({text}) Tj ET\n".encode()
        objs += [b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
                 b"/Contents %d 0 R >>" % (5 + 2 * i), b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


@unittest.skipUnless(BROWSERS and shutil.which("osascript"), NO_BROWSER or "PDF pages render through osascript")
class DocumentPlacementTest(StoreCase):
    # two PDFs of one project that both have a page 3: a capture of one attaches to its own document's page 3
    def setUp(self):
        super().setUp()
        self.patched(store, "_page_count", return_value=3)
        self.repo = project_repo(self.tmp / "app")
        for name in ("a", "b"):
            (self.repo / f"{name}.pdf").write_bytes(pages_pdf(f"{name} one", f"{name} two", f"{name} three"))
        store.start(P, "screen", "Are both decks readable?", cwd=str(self.repo))

    def mapped(self, name):
        # the map dev visual product <name>.pdf writes: one node a page, named by the document it belongs to
        from devkit.visual import docmap
        pdf = self.repo / f"{name}.pdf"
        docmap.write(P, {"file": pdf.name, "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
                         "pages": [{"size": [612, 792], "lines": [], "links": []}] * 3}, by="trinity")

    def page_three(self, name):
        return store.capture(P, str(self.repo / f"{name}.pdf"), pages="3", cwd=str(self.repo))[0]

    def test_a_capture_of_page_3_takes_its_own_documents_node_and_never_the_other_pdfs(self):
        self.mapped("a")
        self.assertEqual(self.page_three("b")["map"]["node"], None)
        self.mapped("b")
        self.assertEqual([self.page_three(n)["map"]["node"] for n in ("b", "a")], ["b.p3", "a.p3"])

    def test_a_capture_taken_before_the_map_attaches_to_its_own_documents_page(self):
        c = self.page_three("b")
        self.mapped("a")
        self.assertIsNone((self.record(c["id"])["map"] or {}).get("node"))
        self.mapped("b")
        self.assertEqual(self.record(c["id"])["map"]["node"], "b.p3")

    def test_a_page_declared_without_its_document_takes_a_page_its_own_document_has_no_node_for(self):
        vmap.add_node(P, "terms.p3", "Terms", page=3, by="ghost", why="the terms page, declared by hand")
        self.mapped("a")
        self.assertEqual([self.page_three(n)["map"]["node"] for n in ("a", "b")], ["a.p3", "terms.p3"])


@unittest.skipUnless(NODE, "node runs the stand-in capture driver")
class DriverGroupTest(StoreCase):
    def test_a_capture_that_runs_past_its_time_stops_node_and_the_browser_it_started(self):
        # L3: the browser runs in a process group of its own (Playwright starts it detached), with its profile in
        # the capture's folder; both groups end when the capture times out
        driver = self.tmp / "driver.cjs"
        pids = self.tmp / "pids.json"
        driver.write_text(r"""
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const forever = ['-e', 'setInterval(() => {}, 1000)', '--'];
const helper = spawn(process.execPath, forever, { stdio: 'ignore' });
const browser = spawn(process.execPath, [...forever,
  '--user-data-dir=' + path.join(process.env.TMPDIR, 'playwright_chromiumdev_profile-x')],
  { stdio: 'ignore', detached: true });
fs.writeFileSync(process.env.PIDS_FILE, JSON.stringify([helper.pid, browser.pid]));
setInterval(() => {}, 1000);
""")
        self.addCleanup(_kill, pids)
        with mock.patch.object(store, "CAPTURE", driver), mock.patch.object(store, "CAPTURE_TIMEOUT", 3), \
                mock.patch.dict(os.environ, {"PIDS_FILE": str(pids)}):
            with self.assertRaises(store.VisualError) as e:
                store._run_capture({"id": "VC-x"}, self.tmp)
        self.assertIn("did not end in 3 s", str(e.exception))
        alive = json.loads(pids.read_text())
        for _ in range(50):
            alive = [pid for pid in alive if _running(pid)]
            if not alive:
                break
            subprocess.run(["sleep", "0.1"])
        self.assertEqual(alive, [])


    def test_a_stopped_capture_leaves_a_process_whose_folder_only_starts_like_its_own(self):
        # the capture's folder is found on a command line as a path, as noderun.stop finds it: a process of another
        # run whose folder's name only starts with it keeps running. The driver starts that process through a shell
        # that exits at once, so it is no child of the driver: noderun.stop also stops the driver's own children
        driver = self.tmp / "driver.cjs"
        pids = self.tmp / "pids.json"
        driver.write_text(r"""
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const forever = ['-e', 'setInterval(() => {}, 1000)', '--'];
const browser = spawn(process.execPath, [...forever, '--user-data-dir=' + path.join(process.env.TMPDIR, 'profile')],
  { stdio: 'ignore', detached: true });
spawn('/bin/sh', ['-c', '"$@" &', 'sh', process.execPath, '-e',
  "require('fs').writeFileSync(process.env.OTHER_UP, String(process.pid)); setInterval(() => {}, 1000)", '--',
  '--user-data-dir=' + process.env.TMPDIR + '-other/profile'], { stdio: 'ignore', detached: true });
fs.writeFileSync(process.env.PIDS_FILE, JSON.stringify([browser.pid]));
setInterval(() => {}, 1000);
""")
        up = self.tmp / "other-up"  # the other run's process writes it once it runs
        self.addCleanup(_kill, pids)
        with mock.patch.object(store, "CAPTURE", driver), mock.patch.object(store, "CAPTURE_TIMEOUT", 3), \
                mock.patch.dict(os.environ, {"PIDS_FILE": str(pids), "OTHER_UP": str(up)}):
            with self.assertRaises(store.VisualError):
                store._run_capture({"id": "VC-x"}, self.tmp)
        self.assertTrue(up.exists(), "the other run's process never started, so this proves nothing")
        browser, other = json.loads(pids.read_text())[0], int(up.read_text())
        pids.write_text(json.dumps([browser, other]))  # the cleanup stops both
        for _ in range(50):
            if not _running(browser):
                break
            subprocess.run(["sleep", "0.1"])
        self.assertEqual((_running(browser), _running(other)), (False, True))


def _kill(pids: Path) -> None:
    # whatever the test left running, it stops: the stand-ins never outlive the test
    try:
        found = json.loads(pids.read_text())
    except (OSError, ValueError):
        return
    for pid in found:
        command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
        if "setInterval(() => {}, 1000)" in command:  # still the stand-in, not a process that took its pid since
            try:
                os.kill(pid, 9)
            except (ProcessLookupError, PermissionError):
                pass


def _running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


class SpacingTest(StoreCase):
    def request(self, repo: Path) -> dict:
        aid = store.new_id("VA")
        store.save(P, {"id": aid, "scope": "screen", "question": "Q?", "matrix": {"viewports": [[390, 844]],
                                                                                 "themes": ["light"], "states": []}})
        seen = []

        def driver(request, cwd):
            seen.append(request)
            return {"kind": "web", "image": {"sha256": "0" * 64, "width": 390, "height": 844, "fold": 844,
                                             "scale": 1}, "checks": {"ran": [], "skipped": {}, "signals": []}}
        with mock.patch.object(store, "_run_capture", driver):
            store.capture(P, self.url, audit=aid, check=False, cwd=str(repo))
        return seen[0]

    def test_the_capture_gets_the_spacing_tokens_of_the_projects_design_md_in_px_and_none_without_them(self):
        # off-scale-spacing runs only with a scale: DESIGN.md's front-matter tokens, never a body that looks like one
        repo = project_repo(self.tmp / "app")
        (repo / "src").mkdir()
        self.assertNotIn("spacing", self.request(repo / "src"))
        (repo / "DESIGN.md").write_text("---\nname: Billing\nrounded:\n  sm: 4px\n---\n# Billing\n\nspacing:\n  a: 8px\n")
        self.assertNotIn("spacing", self.request(repo / "src"))
        shutil.copy(DESIGN, repo / "DESIGN.md")
        self.assertEqual(self.request(repo / "src")["spacing"], [4, 8, 16, 24, 32])


class PictureCleanupTest(StoreCase):
    # R11: `dev gc --apply` and the nightly gc job keep the local pictures an open finding uses and prune the others
    # once they are older than 30 days, with their capture's tiles and marked copy
    def setUp(self):
        super().setUp()
        self.patched(core, "have", return_value=False)  # no Docker pruning in a test
        self.now = time.time()
        self.aid = store.new_id("VA")
        store.save(P, {"id": self.aid, "scope": "screen", "question": "Q?"})

    def picture(self, rgb, days: float, marks=False) -> dict:
        data = png(20, 10, rgb)
        sha = hashlib.sha256(data).hexdigest()
        blob = self.local / "blobs" / f"{sha}.png"
        store._private_write(blob, data)
        os.utime(blob, (self.now - days * 86400,) * 2)
        rec = {"id": store.new_id("VC"), "audit": self.aid, "kind": "image", "image": {"sha256": sha}, "marks": None}
        if marks:
            tile, marked = hashlib.sha256(b"t" + data).hexdigest(), hashlib.sha256(b"m" + data).hexdigest()
            store._private_write(self.local / "marks" / f"{tile}.png", b"tile")
            store._private_write(self.local / "marks" / f"{marked}-marked.png", b"marked")
            rec["marks"] = {"tiles": [tile], "image_sha256": marked, "map": {}}
        store.save(P, rec)
        return rec

    def finding(self, capture: dict, status: str, **extra) -> dict:
        f = {"id": store.new_id("VF"), "audit": self.aid, "capture": capture["id"], "status": status,
             "evidence_captures": [capture["id"]], "verification": None, **extra}
        store.save(P, f)
        return f

    def blob(self, rec) -> Path:
        return self.local / "blobs" / f"{rec['image']['sha256']}.png"

    def test_old_pictures_no_open_finding_uses_are_pruned_with_their_marks_and_the_rest_kept(self):
        used = self.picture((1, 0, 0), 40)
        self.finding(used, "fixed")
        unused = self.picture((2, 0, 0), 40, marks=True)
        closed = self.picture((3, 0, 0), 40)
        self.finding(closed, "wontfix")
        recent = self.picture((4, 0, 0), 5)
        evidence = self.picture((5, 0, 0), 40)
        self.finding(used, "open", evidence_captures=[used["id"], evidence["id"]])
        actions = gc.visual_pictures(self.now)
        self.assertEqual(sorted(a["path"] for a in actions), sorted(str(self.blob(r)) for r in (unused, closed)))
        self.assertTrue(all(a["kind"] == "prune-picture" and a["auto"] for a in actions))
        results = gc.apply({"actions": actions}, remote=False)
        self.assertTrue(all(ok for ok, _msg in results), results)
        self.assertEqual([self.blob(r).exists() for r in (used, unused, closed, recent, evidence)],
                         [True, False, False, True, True])
        self.assertEqual(sorted((self.local / "marks").iterdir()), [])

    def test_a_picture_an_open_finding_took_after_the_plan_is_kept_when_the_cleanup_runs(self):
        old = self.picture((6, 0, 0), 40)
        actions = gc.visual_pictures(self.now)
        self.assertEqual([a["path"] for a in actions], [str(self.blob(old))])
        self.finding(old, "open")
        [(ok, msg)] = gc.apply({"actions": actions}, remote=False)
        self.assertFalse(ok)
        self.assertIn("an open finding uses it", msg)
        self.assertTrue(self.blob(old).exists())

    def test_nothing_is_pruned_when_a_finding_record_cannot_be_read(self):
        self.picture((7, 0, 0), 40)
        (self.records / "findings").mkdir(parents=True, exist_ok=True)
        (self.records / "findings" / "VF-g33zbroken1.json").write_text("{not json")
        self.assertEqual(gc.visual_pictures(self.now), [])


OVERLAP = {"n": 1, "check": "overlap", "pass": "layout", "rule": ["check:overlap"], "measured": "4×18 px",
           "threshold": "0 px", "triage": None, "duplicates": [],
           "anchor": {"element": None, "region": [20, 10, 120, 40], "selector": "#export", "name": 'button "Export"'}}


@unittest.skipUnless(NODE and GITLEAKS, "node and gitleaks redact the text a record holds")
class RecordRulesTest(StoreCase):
    # capture records written by hand, so each rule runs without a browser
    def recorded_capture(self, signals=()):
        aid = store.start(P, "screen", "Is the list readable?")["id"]
        cid = store.new_id("VC")
        store.save(P, {"id": cid, "audit": aid, "kind": "web", "state": None, "map": None, "recipe": None,
                       "image": {"sha256": "0" * 64, "width": 390, "height": 844}, "description": None,
                       "checks": {"ran": ["devkit"], "skipped": {}, "signals": [dict(s) for s in signals]},
                       "passes": {}})
        return aid, cid

    def from_overlap(self, cid):
        return store.add_finding(P, from_signal=f"{cid}#1", pass_="layout", severity="P1", title="Overlap",
                                 impact="A clerk taps Amount.", fix="Space the buttons.")

    def test_a_capture_that_fails_at_one_viewport_writes_no_record_for_the_others(self):
        store.start(P, "screen", "Is it usable?", viewports=["390x844", "1440x900"])
        asked = []

        def driver(request, cwd):
            asked.append(request["recipe"]["viewport"])
            if len(asked) == 2:
                raise store.VisualError("The page did not load. Nothing was recorded.")
            return {"kind": "web", "image": {"sha256": "0" * 64, "width": 390, "height": 844},
                    "source": {"url": self.url, "origin": "local", "route": "/invoices.html"},
                    "checks": {"ran": [], "skipped": {}, "signals": []}}
        self.patched(store, "_run_capture", new=driver)
        self.patched(store, "_impeccable", return_value=None)
        with self.assertRaises(store.VisualError) as e:
            store.capture(P, self.url)
        self.assertEqual(asked, [[390, 844], [1440, 900]])
        self.assertIn("Nothing was recorded", str(e.exception))
        self.assertEqual(list((self.records / "captures").glob("VC-*.json")), [])

    def test_a_finding_on_the_map_without_a_capture_takes_a_structure_pass(self):
        a = store.start(P, "product", "Does the product hold together?")
        picture = self.tmp / "shot.png"
        picture.write_bytes(png(40, 30))
        c = store.capture(P, str(picture))[0]
        vmap.add_node(P, "home", "Home", route="/", by="devuser", why="declare home")
        with self.assertRaises(store.VisualError) as e:
            store.add_finding(P, node="home", evidence_captures=[c["id"]], pass_="layout", rules=["map:orphan"],
                              severity="P2", title="Home is orphaned", impact="Nobody finds it.",
                              evidence="no edge reaches it", fix="Link it.", audit=a["id"])
        self.assertIn("layout is a screen pass: a finding on the map without a capture takes a structure pass "
                      "(S1–S6)", str(e.exception))
        self.assertEqual(self.record(a["id"])["passes"], {})
        self.assertEqual(list((self.records / "findings").glob("VF-*.json")), [])

    def test_a_signal_another_command_triaged_meanwhile_is_not_triaged_again(self):
        _aid, cid = self.recorded_capture([OVERLAP])
        stale = store._signal(P, f"{cid}#1")  # read before the other command took the record's lock
        store.dismiss(P, f"{cid}#1", "the overlap is a design choice", model="human")
        with mock.patch.object(store, "_signal", return_value=stale):
            for name, act in (("finding add", lambda: self.from_overlap(cid)),
                              ("dismiss", lambda: store.dismiss(P, f"{cid}#1", "a second look", model="human"))):
                with self.subTest(name):
                    with self.assertRaises(store.VisualError) as e:
                        act()
                    self.assertIn(f"{cid}#1 is already triaged (dismissed)", str(e.exception))
        self.assertEqual(self.record(cid)["checks"]["signals"][0]["triage"]["why"], "the overlap is a design choice")
        self.assertEqual(list((self.records / "findings").glob("VF-*.json")), [])

    def test_ls_counts_the_captures_and_findings_of_the_audits_an_audit_includes_as_the_report_does(self):
        inner, cid = self.recorded_capture([OVERLAP])
        self.from_overlap(cid)
        outer = store.start(P, "product", "Does the product hold together?", include=[inner])
        row = next(r for r in store.audits(P) if r["audit"]["id"] == outer["id"])
        shown = report.load(self.records, self.local, outer["id"])
        self.assertEqual((row["captures"], row["findings"]), (len(shown["captures"]), len(shown["findings"])))
        self.assertEqual((row["captures"], row["findings"], row["open"]["P1"], row["untriaged"]), (1, 1, 1, 0))

    def test_a_damaged_record_synced_from_another_mac_prints_a_refusal_not_a_traceback(self):
        from devkit import cli
        from devkit.visual import commands
        _aid, cid = self.recorded_capture()
        doc = self.record(cid)
        doc["checks"] = "damaged"
        store.save(P, doc)
        self.patched(commands, "_project", new=lambda a: P)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            a = cli.parse(["visual", "-p", P, "ls"])
            rc = a.fn(a)
        self.assertEqual(rc, 1, out.getvalue())
        self.assertIn("A visual record could not be read (AttributeError:", err.getvalue())
        self.assertIn("visual/", err.getvalue())


class PrivateFolderTest(StoreCase):
    def test_a_folder_another_process_makes_at_the_same_moment_is_kept_and_made_private(self):
        folder = self.tmp / "state" / "visual" / "acme" / "race"
        folder.mkdir(parents=True, mode=0o755)
        seen = Path.exists
        # the other process makes the folder between this one's look and its mkdir
        with mock.patch.object(Path, "exists", lambda p: False if p == folder else seen(p)):
            self.assertEqual(store._private_dir(folder), folder)
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
