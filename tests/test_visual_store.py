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
import gzip
import hashlib
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

from devkit import core, identity  # noqa: E402
from devkit.visual import map as vmap  # noqa: E402
from devkit.visual import store  # noqa: E402

P = "acme/billing"
NODE = shutil.which("node")
GITLEAKS = shutil.which("gitleaks")
CWEBP, DWEBP = shutil.which("cwebp"), shutil.which("dwebp")
ID = re.compile(r"^V[ACF]-[0-9a-z]{7,40}$")
ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

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
                   mock.patch.dict(core.CFG, {"github_user": "ancongui"}),
                   mock.patch.object(identity, "hostname", return_value="mac-test"),
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
        self.assertEqual(rec["by"], {"member": None, "user": "ancongui", "host": "mac-test", "session": "claude-s1"})
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

    def test_by_names_the_crew_member_when_a_member_runs_the_command(self):
        with mock.patch.object(store, "_member", return_value="sati"):
            audit = store.start(P, "screen", "Q?")
            self.assertEqual(store.who(), "sati")
        self.assertEqual(audit["by"]["member"], "sati")
        self.assertEqual(store.who(), "ancongui")


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
        self.assertEqual(rec["image"]["where"], "local:mac-test")
        blob = (self.local / "blobs" / f"{rec['image']['sha256']}.png").read_bytes()
        self.assertEqual(hashlib.sha256(blob).hexdigest(), rec["image"]["sha256"])
        self.assertEqual(rec["by"]["user"], "ancongui")
        self.assertEqual((rec["marks"], rec["judgment"], rec["passes"]), (None, [], {}))

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
                         ("dismissed", "decorative overlap", "ancongui", "human"))
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
        self.assertEqual([(h["by"], h["change"]) for h in rec["history"]], [("ancongui", "open")])
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
        self.assertEqual((triage["as"], triage["finding"], triage["by"]), ("finding", f["id"], "ancongui"))
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
        with self.assertRaises(store.VisualError) as e:
            store.close_finding(P, f["id"], "wontfix", "not now")
        self.assertIn("open, fixing, reopened", str(e.exception))
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
        self.assertEqual([h["change"] for h in self.record(f["id"])["history"]], ["open", "fixing", "fixed"])
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
        self.assertEqual((last["change"], last["by"], last["pictures"]), ("shared", "ancongui", [sha]))

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


if __name__ == "__main__":
    unittest.main()
