# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# lib/devkit/visual/capture.cjs is the capture driver of visual diagnosis: it drives the pinned playwright-core's
# Chromium through the spec's capture sequence and prints one capture record. These tests run it on the pages under
# tests/fixtures/visual/capture/, served on 127.0.0.1 (with localhost as a second origin), in the real pinned browser,
# and read what it wrote: the record on stdout, the picture and the description in a temporary store, and the files
# under its temporary folder. tests/fixtures/visual/capture/harness.cjs runs a capture through a spy on the real
# chromium (launch and context options, spawn arguments, files before close, the DevTools port) and reads pictures in
# a canvas of its own; detector.cjs stands in for `dev impeccable detect`. Without node, gitleaks, the pinned
# @playwright/cli, or its Chromium, the browser tests skip and say which is missing; PDF tests need macOS's osascript.
# Tests carry comments, not docstrings: `dev test prove` leaves out tests with a docstring.
# Run: python3 -m unittest tests.test_visual_capture
import gzip
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
import _sandbox  # noqa: E402  (the net against this Mac's real codex, claude, brew and gh)

sys.path.insert(0, str(ROOT / "lib"))
from devkit.visual import noderun  # noqa: E402

MODULE = ROOT / "lib" / "devkit" / "visual" / "capture.cjs"
FIXTURES = ROOT / "tests" / "fixtures" / "visual" / "capture"
HARNESS = FIXTURES / "harness.cjs"
DETECTOR = FIXTURES / "detector.cjs"
NODE = shutil.which("node")
GITLEAKS = shutil.which("gitleaks")
OSASCRIPT = shutil.which("osascript")

KEY = "AKIA" + "QYLPMN5HHHFPZAM2"  # an AWS access key id, in two parts so no scanner reads this file as one
JWT = ".".join(["eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ",
                "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"])
SECRETS = ["hunter2-visual", "abc123", KEY, "clerk-typed-value", "493817", "4111111111111111", JWT]
REF = re.compile(r"^(f[0-9]+)?e[0-9]+$")


def _account_home() -> Path:
    # the run's HOME is a temporary one (tests/_hermetic.py); Playwright's browsers live in the account's own home
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError):
        return Path.home()


def _pinned_playwright():
    # -> ({core, browsers}, None), or (None, why the browser tests cannot run here)
    if not NODE:
        return None, "node is not on PATH"
    if not GITLEAKS:
        return None, "gitleaks is not installed (the Brewfile pins 8.30.1), and the capture cannot run without it"
    pins = (ROOT / "config" / "npm-globals.txt").read_text().split()
    version = next(p for p in pins if p.startswith("@playwright/cli@")).rpartition("@")[2]
    try:
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=60).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        root = ""
    cli = Path(root or "/nonexistent") / "@playwright" / "cli"
    try:
        installed = json.loads((cli / "package.json").read_text())["version"]
    except (OSError, ValueError, KeyError):
        return None, f"the pinned @playwright/cli {version} is not installed (dev setup installs it)"
    if installed != version:
        return None, f"@playwright/cli {installed} is installed, not the pinned {version}"
    core = cli / "node_modules" / "playwright-core"
    try:
        browsers = json.loads((core / "browsers.json").read_text())["browsers"]
        revision = next(b["revision"] for b in browsers if b["name"] == "chromium")
    except (OSError, ValueError, KeyError, StopIteration):
        return None, f"the pinned @playwright/cli {version} has no playwright-core with a Chromium revision"
    cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(
        _account_home() / ("Library/Caches" if sys.platform == "darwin" else ".cache") / "ms-playwright")
    if not (Path(cache) / f"chromium-{revision}").is_dir():
        return None, f"Chromium {revision}, the pinned Playwright's browser, is not installed in {cache}"
    return {"core": str(core), "browsers": cache, "revision": revision}, None


PLAYWRIGHT, NO_PLAYWRIGHT = _pinned_playwright()
AXE_PAGE = ROOT / "tests" / "fixtures" / "visual" / "axe" / "planted.html"


def _pinned_axe():
    # -> (True, None) when axe-core 4.14.0's axe.min.js is in the global npm folder, else (False, why)
    try:
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=60).stdout.strip()
        data = (Path(root) / "axe-core" / "axe.min.js").read_bytes()
    except (OSError, subprocess.TimeoutExpired):
        return False, "axe-core is not installed in the global npm folder (dev install --only tools)"
    if hashlib.sha256(data).hexdigest() != "20c09fe157a8a34a30e241aaa1fcdade657734f08ab379ecfbeb7d45cc46e878":
        return False, "the global axe-core is not the pinned 4.14.0"
    return bool(PLAYWRIGHT), NO_PLAYWRIGHT


AXE_READY, NO_AXE = _pinned_axe()


class _Handler(SimpleHTTPRequestHandler):
    # serves the fixtures folder; HTML pages get their placeholders filled, so no fixture file holds a whole secret.
    # {{ELSEWHERE}} is this server under a *.localhost name: loopback in Chromium, but not a host devkit treats as this
    # Mac; /redirect-elsewhere answers 302 to its card.html. SVG images are served as they are. Every request's Host
    # and path go into the server's `hits`, so a test can tell which host a request reached, and its method, Host,
    # path, Cookie header and body into `requests`. The redirect chains: a POST to /save answers 303 to /hop/sign-in,
    # which sets the cookie hop=signed and answers 302 to /hop/check; /hop/check answers 403 without that cookie, and
    # with it 302 to /folder, which answers 301 to /folder/ (folder/index.html). /hop/leave sets the cookie too and
    # answers 302 to /hop/check?then=elsewhere, which answers 302 to the other host's card.html.
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FIXTURES), **kwargs)

    def _redirect(self, status, location, cookie=None):
        self.send_response(status)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _seen(self, path, body=None):
        self.server.requests.append({"method": self.command, "host": self.headers.get("Host", ""), "path": path,
                                     "cookie": self.headers.get("Cookie"), "body": body})

    def do_POST(self):  # noqa: N802 (the http.server name)
        path = self.path.split("?")[0]
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        self.server.hits.append((self.headers.get("Host", ""), path))
        self._seen(path, body)
        if path == "/save":
            return self._redirect(303, "/hop/sign-in")
        self.send_error(404)

    def do_GET(self):  # noqa: N802 (the http.server name)
        path, _, query = self.path.split("#")[0].partition("?")
        file = FIXTURES / path.lstrip("/")
        port = self.server.server_address[1]
        self.server.hits.append((self.headers.get("Host", ""), path))
        self._seen(path)
        if path in ("/hop/sign-in", "/hop/leave"):
            then = "" if path == "/hop/sign-in" else "?then=elsewhere"
            return self._redirect(302, f"/hop/check{then}", cookie="hop=signed; Path=/")
        if path == "/hop/check":
            if "hop=signed" not in (self.headers.get("Cookie") or ""):
                return self.send_error(403)
            if query == "then=elsewhere":
                return self._redirect(302, f"http://elsewhere.localhost:{port}/card.html")
            return self._redirect(302, "/folder")
        if path == "/folder":
            return self._redirect(301, "/folder/")
        if path == "/folder/":
            file = FIXTURES / "folder" / "index.html"
        if path.endswith(".svg") and file.is_file():
            body = file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/redirect-elsewhere":
            self.send_response(302)
            self.send_header("Location", f"http://elsewhere.localhost:{port}/card.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if file.suffix == ".html" and file.is_file():
            body = (file.read_text().replace("{{KEY}}", KEY).replace("{{JWT}}", JWT)
                    .replace("{{CROSS}}", f"http://localhost:{port}")
                    .replace("{{ELSEWHERE}}", f"http://elsewhere.localhost:{port}")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def log_message(self, *args):
        pass


class _Server:
    def __enter__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.hits = []
        self.httpd.requests = []
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def recipe(**fields):
    out = {"viewport": [390, 844], "dpr": 1, "theme": "light", "locale": "en-US", "timezone": "UTC", "clock": None,
           "full_page": True, "steps": [], "storage_state": None,
           "emulation": {"forced_colors": False, "contrast": None, "vision": None}, "css": None}
    out.update(fields)
    return out


class _Run:
    # one capture's temporary folders: TMPDIR (watched), the store, the working folder, and a PATH folder holding a
    # playwright-cli that leaves a mark when anything starts it
    def __init__(self):
        self.top = Path(tempfile.mkdtemp(prefix="visual-capture-test-"))
        self.tmp, self.store, self.work, self.bin = (self.top / n for n in ("tmp", "store", "work", "bin"))
        for folder in (self.tmp, self.work, self.bin):
            folder.mkdir()
        self.mark = self.top / "playwright-cli-started"
        cli = self.bin / "playwright-cli"
        cli.write_text(f"#!/bin/sh\ntouch '{self.mark}'\nexit 0\n")
        cli.chmod(0o755)
        self.log = self.top / "detector.json"
        self.extra = {}  # more environment for the capture, such as the detector stand-in's DETECTOR_FINDINGS

    def env(self, path=None):
        e = _sandbox.env(TMPDIR=str(self.tmp), PLAYWRIGHT_BROWSERS_PATH=PLAYWRIGHT["browsers"],
                         DETECTOR_LOG=str(self.log), **self.extra)
        e["PATH"] = os.pathsep.join([str(self.bin), path if path is not None else e["PATH"]])
        return e

    def request(self, url=None, **fields):
        out = {"id": "VC-test", "source": {"url": url}, "recipe": recipe(), "state": "planted", "element": None,
               "mask": [], "remote": False, "check": True, "impeccable": None, "store": str(self.store),
               "core": PLAYWRIGHT["core"]}
        out.update(fields)
        return out

    def cli(self, request, path=None, timeout=120):
        return subprocess.run([NODE, str(MODULE), "--json"], input=json.dumps(request), capture_output=True,
                              text=True, timeout=timeout, env=self.env(path), cwd=str(self.work))

    def harness(self, payload, timeout=120):
        p = subprocess.run([NODE, str(HARNESS)], input=json.dumps(payload), capture_output=True, text=True,
                           timeout=timeout, env=self.env(), cwd=str(self.work))
        if p.returncode != 0:
            raise AssertionError(f"the harness failed (exit {p.returncode}): {p.stderr.strip()[-2000:]}")
        return json.loads(p.stdout)

    def capture(self, request, **spy):
        return self.harness({"mode": "capture", "core": PLAYWRIGHT["core"], "request": request, **spy})

    def pixels(self, png, boxes=(), compare=None):
        return self.harness({"mode": "pixels", "core": PLAYWRIGHT["core"], "png": str(png), "boxes": list(boxes),
                             "compare": compare})

    def stored(self):
        return sorted(p for p in self.store.rglob("*") if p.is_file()) if self.store.exists() else []

    def picture(self, record):
        return self.store / "blobs" / f"{record['image']['sha256']}.png"

    def description(self, record):
        return json.loads(gzip.decompress((self.store / "desc" / f"{record['description']['sha256']}.json.gz").read_bytes()))

    def close(self):
        shutil.rmtree(self.top, ignore_errors=True)


def by_id(desc):
    return {e["id"]: e for e in desc["elements"]}


def find(desc, **match):
    found = [e for e in desc["elements"] if all(e.get(k) == v for k, v in match.items())]
    if len(found) != 1:
        raise AssertionError(f"{len(found)} elements match {match}")
    return found[0]


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from strings(v)


def inset(box, by=1):
    x, y, w, h = box
    return [x + by, y + by, w - 2 * by, h - 2 * by]


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class PlantedPage(unittest.TestCase):
    # one capture of page.html through the spy: scrolled to y 3000 after a step that retitles the page, with the
    # stand-in detector, every secret watched in the capture's folder until the browser closes
    @classmethod
    def setUpClass(cls):
        cls.run_ = _Run()
        cls.server = _Server().__enter__()
        request = cls.run_.request(f"{cls.server.base}/page.html?t=abc123&to=/now",
                                   recipe=recipe(steps=["click #retitle", "scroll 3000"]),
                                   impeccable={"command": [NODE, str(DETECTOR)], "version": "test-detector"})
        cls.out = cls.run_.capture(request, watch=SECRETS, probe_port=True)
        cls.ok = cls.out["ok"]
        if cls.ok:
            cls.record = cls.out["record"]
            cls.desc = cls.run_.description(cls.record)
            cls.detector = json.loads(cls.run_.log.read_text()) if cls.run_.log.exists() else None

    @classmethod
    def tearDownClass(cls):
        cls.server.__exit__()
        cls.run_.close()

    def setUp(self):
        if not self.ok:
            self.fail(f"the capture failed: {self.out}")

    def test_vd_ac_5_the_export_button_is_one_element_with_its_page_box_locator_and_style(self):
        export = find(self.desc, role="button", name="Export")
        self.assertRegex(export["id"], r"^E[1-9][0-9]*$")
        # the page box, not the viewport box: the snapshot ran at scrollY 3000, where the viewport y is 200
        self.assertEqual(export["box"], [40, 3200, 120, 30])
        self.assertEqual(export["locator"], "getByTestId('export')")
        self.assertEqual(export["selector"], '[data-testid="export"]')
        self.assertEqual(export["style"], {"font_size": 14, "font_weight": 700, "color": "#ffffff",
                                           "background": "#0050b3"})
        self.assertIsNone(export["frame"])

    def test_vd_ac_5_no_field_of_the_description_holds_a_snapshot_ref(self):
        refs = [s for s in strings(self.desc) if REF.match(s)]
        self.assertEqual(refs, [])
        self.assertNotIn("ref", set(strings(self.desc)))

    def test_vd_ac_5_the_description_says_what_it_was_built_from(self):
        self.assertEqual(self.desc["schema"], "devkit.visual.description/1")
        self.assertEqual(self.desc["capture"], "VC-test")
        self.assertEqual(self.desc["coords"], "page-css-px")
        self.assertEqual(self.desc["viewport"], [390, 844])
        self.assertEqual(self.desc["page"], [390, 4200])
        self.assertEqual(self.desc["image_sha256"], self.record["image"]["sha256"])
        self.assertEqual(self.desc["total"], len(self.desc["elements"]))
        self.assertEqual(self.record["description"]["elements"], len(self.desc["elements"]))

    def test_vd_ac_9_password_one_time_code_card_and_marked_fields_are_painted_solid(self):
        boxes = {"password": [120, 150, 150, 24], "show-password": [120, 190, 150, 24],
                 "one-time-code in the same-origin frame": [28, 472, 200, 30],
                 "card number in the cross-origin frame": [28, 592, 200, 30],
                 "data-visual-mask": [16, 350, 200, 30], "user field, not masked": [120, 110, 150, 24]}
        seen = self.run_.pixels(self.run_.picture(self.record), [inset(b) for b in boxes.values()])
        for (name, _), got in zip(boxes.items(), seen["boxes"]):
            with self.subTest(name):
                if name.endswith("not masked"):
                    self.assertGreater(got["nonblack"], 0, "the check tells a painted box from a field")
                else:
                    self.assertEqual(got["nonblack"], 0, f"{got['nonblack']} of {got['total']} pixels show through")

    def test_vd_ac_9_field_values_are_dropped_and_the_fields_keep_role_and_box(self):
        # a masked field loses its name and text (R13); its role and box stay, so findings can point at it
        password = find(self.desc, role="textbox", box=[120, 150, 150, 24])
        self.assertEqual((password["redacted"], password["name"]), ("mask", None))
        user = find(self.desc, role="textbox", name="User")
        self.assertEqual((user["redacted"], user["text"], user["box"]), ("value", None, [120, 110, 150, 24]))
        card = [e for e in self.desc["elements"] if e["frame"] is not None and e["box"] == [28, 592, 200, 30]]
        self.assertEqual([(e["role"], e["redacted"]) for e in card], [("textbox", "mask")])
        self.assertGreaterEqual(self.record["redaction"]["values_dropped"], 1)

    def test_vd_ac_10_a_key_in_text_in_a_link_name_and_in_a_field_is_painted_and_marked(self):
        rule = "aws-access-token"
        paragraph = next(e for e in self.desc["elements"] if e["box"] == [16, 270, 340, 20])
        self.assertEqual((paragraph["redacted"], paragraph["text"]), (f"secret:{rule}", f"[redacted: {rule}]"))
        link = find(self.desc, role="link", box=[16, 310, 300, 20])
        self.assertEqual((link["redacted"], link["name"]), (f"secret:{rule}", f"[redacted: {rule}]"))
        field = find(self.desc, role="textbox", name="Access key")
        self.assertEqual(field["redacted"], f"secret:{rule}")
        seen = self.run_.pixels(self.run_.picture(self.record),
                                [inset(b) for b in ([16, 270, 340, 20], [16, 310, 300, 20], [120, 230, 200, 24])])
        self.assertEqual([b["nonblack"] for b in seen["boxes"]], [0, 0, 0])
        counts = self.record["redaction"]
        self.assertEqual((counts["secret_hits"], counts["value_secret_hits"]), (2, 1))

    def test_vd_ac_11_urls_are_redacted_in_the_record_and_the_description(self):
        self.assertEqual(self.record["source"]["url"], f"{self.server.base}/page.html?t=[redacted]&to=/now")
        self.assertEqual(self.record["source"]["route"], "/page.html")
        self.assertEqual(self.record["redaction"]["url_params"], ["t"])
        urls = {e["name"]: e["url"] for e in self.desc["elements"] if e["role"] == "link" and e["url"]}
        self.assertEqual(urls["Reports"], "/reports?token=[redacted]")
        self.assertEqual(urls["Reset"], "/reset/[redacted]?X-Amz-Signature=[redacted]&api_key=[redacted]#[redacted]")
        self.assertEqual(urls["Open"], "#/invoices?tab=open")

    def test_vd_ac_60_no_written_file_and_no_output_holds_a_secret(self):
        for file in self.run_.stored():
            data = file.read_bytes()
            if file.suffix == ".gz":
                data = gzip.decompress(data)
            for secret in SECRETS:
                with self.subTest(file=file.name, secret=secret[:6]):
                    self.assertNotIn(secret.encode(), data)
        printed = json.dumps(self.record)
        self.assertEqual([s[:6] for s in SECRETS if s in printed], [])
        # the browser's own files (profile, cache, artifacts) under the capture's folder, read just before it closed
        self.assertEqual(self.out["spy"]["held"], [])

    def test_vd_ac_60_the_capture_folder_is_gone_and_the_working_folder_unchanged(self):
        self.assertTrue(self.out["spy"]["folder"].startswith(str(self.run_.tmp)))
        self.assertEqual(list(self.run_.tmp.iterdir()), [])
        self.assertEqual(list(self.run_.work.iterdir()), [])
        self.assertFalse(self.run_.mark.exists(), "playwright-cli was started")

    def test_vd_ac_72_control_and_bidi_characters_are_stripped_and_counted(self):
        escape = next(e for e in self.desc["elements"] if e["box"] == [16, 400, 340, 20])
        self.assertEqual(escape["text"], "Copy ]52;c;aGk= now reversed")
        everything = "".join(strings(self.desc)) + json.dumps(self.record, ensure_ascii=False)
        self.assertNotIn("\x1b", everything)
        self.assertNotIn("\u202e", everything)
        self.assertGreaterEqual(self.record["redaction"]["controls_stripped"], 3)

    def test_vd_ac_8_store_files_are_0600_in_0700_folders_and_named_by_their_hash(self):
        picture = self.run_.picture(self.record)
        self.assertEqual(hashlib.sha256(picture.read_bytes()).hexdigest(), self.record["image"]["sha256"])
        desc = self.run_.store / "desc" / f"{self.record['description']['sha256']}.json.gz"
        self.assertEqual(hashlib.sha256(desc.read_bytes()).hexdigest(), self.record["description"]["sha256"])
        for path in [self.run_.store / "blobs", self.run_.store / "desc"]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700, path.name)
        for path in self.run_.stored():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)
        self.assertEqual(len(self.run_.stored()), 2)

    def test_the_record_names_the_picture_the_recipe_and_the_engine(self):
        r = self.record
        # the source URL's t=abc123 is redacted, so the recorded URL is not the one captured: not reproducible
        self.assertEqual((r["kind"], r["state"], r["reproducible"], r["experiment"]), ("web", "planted", False, False))
        self.assertEqual((r["image"]["width"], r["image"]["height"], r["image"]["fold"]), (390, 4200, 844))
        self.assertEqual(r["recipe"]["steps"], ["click #retitle", "scroll 3000"])
        self.assertEqual(r["env"]["browser"], f"chromium {PLAYWRIGHT['revision']}")
        self.assertEqual(r["env"]["playwright_core"], json.loads(
            (Path(PLAYWRIGHT["core"]) / "package.json").read_text())["version"])
        self.assertTrue(r["env"]["fonts_ready"])
        self.assertEqual(r["source"]["origin"], "local")

    def test_vd_ac_17_the_detector_scans_the_open_page_through_the_port_chromium_chose(self):
        log, spy = self.detector, self.out["spy"]
        self.assertIsNotNone(log, "the detector did not run")
        self.assertEqual(log["port"], log["active"], "the port is the first line of DevToolsActivePort")
        self.assertEqual(log["port"], spy["port"])
        self.assertEqual(log["listening"], [f"127.0.0.1:{log['port']}"])
        # the page as the steps left it, the only page open: not a fresh load of the URL
        self.assertEqual([p["title"] for p in log["pages"]], ["state-after-steps"])
        target = log["args"][log["args"].index("--target") + 1]
        self.assertIn(target, log["pages"][0]["url"])
        self.assertNotIn("abc123", " ".join(log["args"]))
        self.assertEqual(spy["page_websocket"], "error")
        self.assertEqual(spy["after_close"], "refused")
        self.assertIn("--remote-debugging-port=0", spy["launch"]["args"])
        self.assertIn("--remote-debugging-address=127.0.0.1", spy["launch"]["args"])
        self.assertFalse([a for a in spy["spawn"][0] if a.startswith("--remote-allow-origins")])

    def test_vd_ac_17_a_detector_finding_becomes_a_signal_on_its_element(self):
        # the low-contrast text is axe's color-contrast too where the pinned axe-core is installed, so the
        # detector's signal is folded into axe's signal on that element as its duplicate (R68); without axe it is a
        # signal of its own
        low = find(self.desc, text="Low contrast note")
        on_low = [s for s in self.record["checks"]["signals"] if s["anchor"]["element"] == low["id"]]
        self.assertEqual(len(on_low), 1, on_low)
        s = on_low[0]
        self.assertEqual((s["anchor"]["selector"], s["anchor"]["region"]), ("#low", low["box"]))
        if AXE_READY:
            self.assertEqual((s["check"], s.get("axe_rule")), ("axe", "color-contrast"))
            [dup] = [d for d in s["duplicates"] if d["check"] == "impeccable"]
        else:
            self.assertEqual(s["check"], "impeccable")
            dup = s
            self.assertEqual(s["pass"], "color")
        self.assertEqual((dup["rule"], dup["measured"]), (["impeccable:low-contrast"], "1.9:1 (need 4.5:1)"))
        self.assertNotIn("file", s)
        self.assertIn("impeccable", self.record["checks"]["ran"])
        self.assertEqual(self.record["env"]["impeccable"], "test-detector")

    def test_every_signal_is_numbered_and_anchored_to_an_element_of_the_description(self):
        ids = set(by_id(self.desc))
        signals = self.record["checks"]["signals"]
        self.assertEqual([s["n"] for s in signals], list(range(1, len(signals) + 1)))
        for s in signals:
            self.assertTrue(s["anchor"]["element"] is None or s["anchor"]["element"] in ids, s)
        self.assertEqual(self.record["checks"]["ran"][0], "devkit")

    def test_vd_ac_69_the_browser_runs_sandboxed_without_crash_reports_traces_or_service_workers(self):
        spy = self.out["spy"]
        self.assertIs(spy["launch"]["chromiumSandbox"], True)
        self.assertTrue({"--disable-breakpad", "--disable-crash-reporter"} <= set(spy["launch"]["args"]))
        self.assertNotIn("tracesDir", spy["launch"])
        self.assertEqual(len(spy["spawn"]), 1, "one browser process holds the capture's profile")
        self.assertNotIn("--no-sandbox", spy["spawn"][0])
        self.assertIn("--disable-breakpad", spy["spawn"][0])
        self.assertEqual(spy["context"]["serviceWorkers"], "block")
        self.assertEqual(spy["context"]["reducedMotion"], "reduce")
        self.assertFalse({"recordHar", "recordVideo"} & set(spy["context"]))
        self.assertFalse(spy["tracing"])


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class CommandLine(unittest.TestCase):
    # `node capture.cjs --json` as visual.py runs it: the record on stdout, an exit code, and nothing written on failure
    @classmethod
    def setUpClass(cls):
        cls.server = _Server().__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.server.__exit__()

    def setUp(self):
        self.run_ = _Run()
        self.addCleanup(self.run_.close)

    def assertWroteNothing(self, p):
        self.assertNotEqual(p.returncode, 0, p.stdout)
        self.assertEqual(p.stdout, "")
        self.assertEqual(self.run_.stored(), [])
        self.assertEqual(list(self.run_.tmp.iterdir()), [], "the capture's folder is left behind")

    def test_vd_ac_3_the_same_recipe_twice_gives_the_same_picture(self):
        request = self.run_.request(f"{self.server.base}/page.html")
        first, second = self.run_.cli(request), self.run_.cli(request)
        self.assertEqual((first.returncode, second.returncode), (0, 0), first.stderr + second.stderr)
        a, b = json.loads(first.stdout), json.loads(second.stdout)
        self.assertEqual(a["image"]["sha256"], b["image"]["sha256"])
        self.assertEqual(first.stdout.count("\n"), 1, "one record, one line")

    def test_vd_ac_4_a_route_step_gives_the_error_state_in_the_description(self):
        steps = ["route **/api/export 500", "click text=Export", "wait text=Export failed"]
        p = self.run_.cli(self.run_.request(f"{self.server.base}/page.html", recipe=recipe(steps=steps)))
        self.assertEqual(p.returncode, 0, p.stderr)
        desc = self.run_.description(json.loads(p.stdout))
        self.assertIn("Export failed (500)", [e["text"] for e in desc["elements"]])

    def test_vd_ac_4_a_step_that_finds_nothing_fails_naming_it_and_writes_nothing(self):
        p = self.run_.cli(self.run_.request(f"{self.server.base}/page.html", recipe=recipe(steps=["click text=Missing"])))
        self.assertWroteNothing(p)
        self.assertIn("Step 1, click text=Missing, found nothing in 10 s", p.stderr)

    def test_recipe_steps_are_redacted_in_the_record_and_a_changed_recipe_is_not_reproducible(self):
        steps = [f"goto {self.server.base}/page.html?token=abc123", f"fill #user {KEY}"]
        p = self.run_.cli(self.run_.request(f"{self.server.base}/page.html", recipe=recipe(steps=steps)))
        self.assertEqual(p.returncode, 0, p.stderr)
        record = json.loads(p.stdout)
        self.assertEqual(record["recipe"]["steps"], [f"goto {self.server.base}/page.html?token=[redacted]",
                                                     "[redacted: aws-access-token]"])
        self.assertIs(record["reproducible"], False)
        self.assertNotIn(KEY, p.stdout)
        self.assertNotIn("abc123", p.stdout)

    def test_a_key_in_the_source_url_is_redacted_by_the_full_scan_and_only_a_changed_url_is_not_reproducible(self):
        # q is no secret-shaped name, so only the gitleaks scan of the URL finds the key in it
        p = self.run_.cli(self.run_.request(f"{self.server.base}/page.html?q={KEY}&tab=2"))
        self.assertEqual(p.returncode, 0, p.stderr)
        record = json.loads(p.stdout)
        self.assertEqual(record["source"]["url"], f"{self.server.base}/page.html?q=[redacted]&tab=2")
        self.assertEqual((record["source"]["route"], record["redaction"]["url_params"]), ("/page.html", ["q"]))
        self.assertIs(record["reproducible"], False)
        self.assertNotIn(KEY, p.stdout)
        # a URL redaction leaves as it is keeps the capture reproducible
        p = self.run_.cli(self.run_.request(f"{self.server.base}/page.html?tab=2"))
        self.assertEqual(p.returncode, 0, p.stderr)
        record = json.loads(p.stdout)
        self.assertEqual((record["source"]["url"], record["reproducible"]), (f"{self.server.base}/page.html?tab=2", True))

    def test_spacing_is_checked_only_on_the_scale_the_request_gives_and_its_absence_is_recorded(self):
        def checks(**fields):
            p = self.run_.cli(self.run_.request(f"{self.server.base}/spacing.html", **fields))
            self.assertEqual(p.returncode, 0, p.stderr)
            out = json.loads(p.stdout)["checks"]
            return [s["measured"] for s in out["signals"] if s["check"] == "off-scale-spacing"], out["skipped"]
        # without the project's scale the check does not run, and the record says why
        for none in ({}, {"spacing": []}):
            measured, skipped = checks(**none)
            self.assertEqual((measured, skipped.get("off-scale-spacing")), ([], "no spacing scale"), none)
        # DESIGN.md's tokens of 8 to 48 px leave out the one padding-top of 10 px
        measured, skipped = checks(spacing=[8, 16, 24, 32, 48])
        self.assertEqual((measured, skipped.get("off-scale-spacing")), (["padding-top 10 px"], None))
        # a scale of 4 and 10 px puts the 10 px on it, and the 8 px off
        measured, _ = checks(spacing=[4, 10])
        self.assertNotIn("padding-top 10 px", measured)
        self.assertIn("padding-top 8 px, padding-right 8 px, padding-bottom 8 px, padding-left 8 px", measured)

    def test_a_page_that_redirects_to_another_host_fails_and_writes_nothing_unless_remote(self):
        p = self.run_.cli(self.run_.request(f"{self.server.base}/redirect-elsewhere"))
        self.assertWroteNothing(p)
        self.assertIn("elsewhere.localhost", p.stderr)
        self.assertIn("after it loaded", p.stderr)
        self.assertIn("--remote", p.stderr)
        # with --remote the same page is captured
        p = self.run_.cli(self.run_.request(f"{self.server.base}/redirect-elsewhere", remote=True))
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_a_redirect_is_followed_hop_by_hop_on_this_mac_and_stopped_before_another_host(self):
        # devkit fetches each hop itself: the POST answered 303 goes on as a GET, the cookie a hop sets reaches the
        # next hop, each address is asked once, and the page lands where the chain ends, so its relative picture loads
        # from /folder/
        requests = self.server.httpd.requests
        requests.clear()
        p = self.run_.cli(self.run_.request(f"{self.server.base}/save.html", recipe=recipe(steps=["click #save"])))
        self.assertEqual(p.returncode, 0, p.stderr)
        find(self.run_.description(json.loads(p.stdout)), role="heading", name="Signed in")
        chain = ("/save", "/hop/sign-in", "/hop/check", "/folder", "/folder/")
        self.assertEqual([(r["method"], r["path"]) for r in requests if r["path"] in chain],
                         [("POST", "/save"), ("GET", "/hop/sign-in"), ("GET", "/hop/check"), ("GET", "/folder"),
                          ("GET", "/folder/")])
        self.assertEqual([r["body"] for r in requests if r["path"] == "/save"], ["note=October"])
        self.assertEqual([r["cookie"] for r in requests if r["path"] == "/hop/check"], ["hop=signed"])
        self.assertIn("/folder/pic.svg", [r["path"] for r in requests])
        # a chain that leaves this Mac after a hop on it: the other host gets no request, and the capture fails
        self.run_ = _Run()
        self.addCleanup(self.run_.close)
        requests.clear()
        p = self.run_.cli(self.run_.request(f"{self.server.base}/hop/leave"))
        self.assertWroteNothing(p)
        self.assertEqual([(r["host"].split(":")[0], r["path"]) for r in requests],
                         [("127.0.0.1", "/hop/leave"), ("127.0.0.1", "/hop/check")])
        self.assertIn("tried to go to elsewhere.localhost", p.stderr)
        self.assertIn("after it loaded", p.stderr)

    def test_a_redirect_to_another_host_sends_that_host_no_request(self):
        self.server.httpd.hits.clear()
        p = self.run_.cli(self.run_.request(f"{self.server.base}/redirect-elsewhere"))
        self.assertWroteNothing(p)
        hosts = {host.split(":")[0] for host, _path in self.server.httpd.hits}
        self.assertEqual(hosts, {"127.0.0.1"}, self.server.httpd.hits)
        self.assertIn("stopped that request before it was sent", p.stderr)

    def test_a_window_the_page_opens_sends_another_host_no_request(self):
        # the capture reads only its page; a new window's redirect to another host, and its own navigation there, are
        # never sent
        self.server.httpd.hits.clear()
        steps = ["click #pop", "click #away", "wait 500"]
        p = self.run_.cli(self.run_.request(f"{self.server.base}/popup.html", recipe=recipe(steps=steps)))
        self.assertEqual(p.returncode, 0, p.stderr)
        reached = [(host.split(":")[0], path) for host, path in self.server.httpd.hits]
        self.assertIn(("127.0.0.1", "/redirect-elsewhere"), reached)
        hosts = {host.split(":")[0] for host, _path in self.server.httpd.hits}
        self.assertEqual(hosts, {"127.0.0.1"}, self.server.httpd.hits)

    def test_a_step_that_leads_to_another_host_fails_naming_the_step_and_writes_nothing(self):
        p = self.run_.cli(self.run_.request(f"{self.server.base}/leave.html", recipe=recipe(steps=["click #out"])))
        self.assertWroteNothing(p)
        self.assertIn("elsewhere.localhost", p.stderr)
        self.assertIn("after Step 1, click #out", p.stderr)

    def test_a_step_that_leads_to_another_host_sends_that_host_no_request(self):
        self.server.httpd.hits.clear()
        p = self.run_.cli(self.run_.request(f"{self.server.base}/leave.html", recipe=recipe(steps=["click #out"])))
        self.assertWroteNothing(p)
        hosts = {host.split(":")[0] for host, _path in self.server.httpd.hits}
        self.assertEqual(hosts, {"127.0.0.1"}, self.server.httpd.hits)

    def test_a_step_the_grammar_does_not_know_is_refused_before_the_browser_starts(self):
        p = self.run_.cli(self.run_.request(f"{self.server.base}/page.html", recipe=recipe(steps=["teleport #x"])))
        self.assertEqual(p.returncode, 2)
        self.assertIn("Step 1, teleport #x: unknown verb teleport", p.stderr)

    def test_vd_ac_60_without_gitleaks_the_capture_fails_and_writes_nothing(self):
        node_only = Path(self.run_.top / "node-only")
        node_only.mkdir()
        (node_only / "node").symlink_to(NODE)
        p = self.run_.cli(self.run_.request(f"{self.server.base}/page.html"), path=str(node_only))
        self.assertWroteNothing(p)
        self.assertIn("gitleaks is not installed", p.stderr)

    def test_vd_ac_12_a_remote_host_is_refused_without_remote(self):
        p = self.run_.cli(self.run_.request("https://example.com/"))
        self.assertWroteNothing(p)
        self.assertIn("--remote", p.stderr)

    def test_vd_ac_17_without_checks_no_devtools_port_is_opened(self):
        request = self.run_.request(f"{self.server.base}/page.html", check=False,
                                    impeccable={"command": [NODE, str(DETECTOR)], "version": "test-detector"})
        out = self.run_.capture(request, probe_port=True)
        self.assertTrue(out["ok"], out)
        self.assertFalse([a for a in out["spy"]["launch"]["args"] if a.startswith("--remote-debugging")])
        self.assertIsNone(out["spy"].get("port"))
        self.assertFalse(self.run_.log.exists(), "the detector ran")
        self.assertEqual((out["record"]["checks"]["ran"], out["record"]["checks"]["signals"]), ([], []))

    def test_vd_ac_42_a_crop_is_the_element_box_plus_24_px_cut_from_the_full_picture(self):
        p = self.run_.cli(self.run_.request(f"{self.server.base}/page.html", element="[data-testid=export]"))
        self.assertEqual(p.returncode, 0, p.stderr)
        record = json.loads(p.stdout)
        self.assertEqual((record["element"]["selector"], record["element"]["box"]),
                         ("[data-testid=export]", [40, 3200, 120, 30]))
        crop = self.run_.store / "blobs" / f"{record['element']['sha256']}.png"
        seen = self.run_.pixels(crop, compare={"png": str(self.run_.picture(record)), "at": [16, 3176]})
        self.assertEqual((seen["width"], seen["height"], seen["same"]), (168, 78, True))
        self.assertEqual(len(self.run_.stored()), 3)

    def test_vd_ac_42_a_crop_of_an_element_below_a_viewport_picture_fails_and_writes_nothing(self):
        p = self.run_.cli(self.run_.request(f"{self.server.base}/page.html", element="[data-testid=export]",
                                            recipe=recipe(full_page=False)))
        self.assertWroteNothing(p)
        self.assertIn("lies outside the picture", p.stderr)

    def test_vd_ac_75_a_page_taller_than_the_bound_is_cut_at_16384_px_and_marked_truncated(self):
        # the endless list grows past 22,000 px once scrolled to its bottom; the picture and the description stop at
        # the height bound, and the record keeps the page's full height
        p = self.run_.cli(self.run_.request(f"{self.server.base}/endless.html", recipe=recipe(steps=["scroll bottom"]),
                                            check=True), timeout=180)
        self.assertEqual(p.returncode, 0, p.stderr)
        record = json.loads(p.stdout)
        image = record["image"]
        self.assertEqual((image["height"], image["truncated"]), (16384, True))
        self.assertGreaterEqual(image["page_height"], 22000)
        self.assertEqual(self.run_.pixels(self.run_.picture(record))["height"], 16384)
        desc = self.run_.description(record)
        boxes = [e["box"] for e in desc["elements"] if e["box"]]
        self.assertTrue(all(b[1] + b[3] <= 16384 for b in boxes), [b for b in boxes if b[1] + b[3] > 16384][:3])
        texts = [e["text"] for e in desc["elements"] if e["text"]]
        self.assertIn("row at 16000", texts)
        self.assertNotIn("row at 17000", texts)
        self.assertTrue(all(s["anchor"]["region"][1] < 16384 for s in record["checks"]["signals"]
                            if s["anchor"].get("region")))

    def test_vd_ac_75_a_step_that_never_ends_stops_at_the_capture_bound_and_writes_nothing(self):
        # the bound is lowered to 3 s so the test reaches it; the step waits 10 s, the longest wait a step takes
        out = self.run_.capture(self.run_.request(f"{self.server.base}/page.html",
                                                  recipe=recipe(steps=["wait 10000"])),
                                bounds={"captureSeconds": 3})
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["code"], "time-bound")
        self.assertIn("took longer than 3 s, the bound for one capture", out["message"])
        self.assertIn("Nothing was recorded", out["message"])
        self.assertLess(out["seconds"], 9)
        self.assertEqual(self.run_.stored(), [])
        self.assertEqual(list(self.run_.tmp.iterdir()), [], "the capture's folder is left behind")

    def test_vd_ac_75_the_bounds_are_90_s_a_capture_and_16384_css_px_of_height(self):
        self.assertEqual(self.run_.harness({"mode": "bounds"}), {"captureSeconds": 90, "pageHeight": 16384})

    @unittest.skipUnless(AXE_READY, NO_AXE)
    def test_vd_ac_61_vd_ac_62_axe_runs_in_the_capture_and_its_duplicate_of_a_devkit_check_folds_into_one(self):
        # R30, R68: the axe fixture page; its picture with no alt that does not load is seen by axe (image-alt) and
        # by checks.js (broken-image), and reaches triage once, as axe's signal with the other as its duplicate (the
        # snapshot leaves a picture with no alt out, so the signal names it by its selector)
        p = self.run_.cli(self.run_.request(AXE_PAGE.as_uri()), timeout=180)
        self.assertEqual(p.returncode, 0, p.stderr)
        record = json.loads(p.stdout)
        checks = record["checks"]
        self.assertIn("axe", checks["ran"])
        self.assertEqual(record["env"]["axe"], "4.14.0")
        signals = checks["signals"]
        self.assertEqual([s["n"] for s in signals], list(range(1, len(signals) + 1)))
        on_logo = [s for s in signals if s["anchor"]["selector"] == "#logo"]
        self.assertEqual([(s["check"], s.get("axe_rule")) for s in on_logo], [("axe", "image-alt")])
        self.assertEqual([d["check"] for d in on_logo[0]["duplicates"]], ["broken-image"])
        self.assertIn("color-contrast", [s.get("axe_rule") for s in signals])
        self.assertTrue(all(s["triage"] is None and isinstance(s["duplicates"], list) for s in signals))


    def test_vd_ac_42_a_crop_selector_must_match_exactly_one_element(self):
        p = self.run_.cli(self.run_.request(f"{self.server.base}/page.html", element="input"))
        self.assertWroteNothing(p)
        self.assertIn("matches 4 elements, not one", p.stderr)


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class HostilePage(unittest.TestCase):
    # hostile.html replaces getComputedStyle, CSS.escape and the input value getter in its own JavaScript world
    @classmethod
    def setUpClass(cls):
        cls.run_ = _Run()
        with _Server() as server:
            cls.p = cls.run_.cli(cls.run_.request(f"{server.base}/hostile.html"))
        cls.record = json.loads(cls.p.stdout) if cls.p.returncode == 0 else None

    @classmethod
    def tearDownClass(cls):
        cls.run_.close()

    def setUp(self):
        if self.record is None:
            self.fail(f"the capture failed: {self.p.stderr}")

    def test_the_checks_still_find_tiny_text_with_its_true_selector(self):
        small = [s for s in self.record["checks"]["signals"] if s["check"] == "min-text-size"]
        self.assertEqual([(s["anchor"]["selector"], s["measured"]) for s in small], [("#tiny", "10 px")])

    def test_a_key_in_a_field_whose_value_the_page_hides_is_still_painted(self):
        desc = self.run_.description(self.record)
        field = next(e for e in desc["elements"] if e["box"] == [16, 60, 220, 24])
        self.assertEqual(field["redacted"], "secret:aws-access-token")
        seen = self.run_.pixels(self.run_.picture(self.record), [inset([16, 60, 220, 24])])
        self.assertEqual(seen["boxes"][0]["nonblack"], 0)


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class TextChangesAfterTheScreenshot(unittest.TestCase):
    def setUp(self):
        self.run_ = _Run()
        self.addCleanup(self.run_.close)
        self.server = _Server().__enter__()
        self.addCleanup(self.server.__exit__)

    def test_vd_ac_71_a_key_that_appears_late_is_painted_or_the_capture_writes_nothing(self):
        p = self.run_.cli(self.run_.request(f"{self.server.base}/late.html"))
        for file in self.run_.stored():
            data = file.read_bytes()
            self.assertNotIn(KEY.encode(), gzip.decompress(data) if file.suffix == ".gz" else data)
        self.assertNotIn(KEY, p.stdout + p.stderr)
        if p.returncode == 0:
            record = json.loads(p.stdout)
            late = next(e for e in self.run_.description(record)["elements"] if e["box"] == [16, 16, 340, 20])
            if late["text"] != "Waiting":
                # the key was on the page when its text was read: masked like any other
                self.assertEqual(late["redacted"], "secret:aws-access-token")
                seen = self.run_.pixels(self.run_.picture(record), [inset([16, 16, 340, 20])])
                self.assertEqual(seen["boxes"][0]["nonblack"], 0)
        else:
            self.assertEqual(self.run_.stored(), [])
            self.assertIn("changed", p.stderr)

    def test_vd_ac_71_a_key_shown_after_the_masks_were_built_is_caught_and_the_capture_taken_again(self):
        # the page writes the key right before the first screenshot, after the snapshot and the masks
        out = self.run_.capture(self.run_.request(f"{self.server.base}/quiet.html"),
                                before_screenshot={"selector": "#late", "texts": [f"Key {KEY}"]})
        self.assertTrue(out["ok"], out)
        record = out["record"]
        self.assertEqual((record["redaction"]["retried"], out["spy"]["screenshots"]), (True, 2))
        late = next(e for e in self.run_.description(record)["elements"] if e["box"] == [16, 16, 340, 20])
        self.assertEqual(late["redacted"], "secret:aws-access-token")
        seen = self.run_.pixels(self.run_.picture(record), [inset([16, 16, 340, 20])])
        self.assertEqual(seen["boxes"][0]["nonblack"], 0)
        for file in self.run_.stored():
            data = file.read_bytes()
            self.assertNotIn(KEY.encode(), gzip.decompress(data) if file.suffix == ".gz" else data)

    def test_vd_ac_71_a_key_that_keeps_changing_after_the_retry_fails_the_capture_and_writes_nothing(self):
        out = self.run_.capture(self.run_.request(f"{self.server.base}/quiet.html"),
                                before_screenshot={"selector": "#late", "texts": [f"Key {KEY}", f"New key {KEY}"]})
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["code"], "text-changed")
        self.assertEqual(self.run_.stored(), [])
        self.assertEqual(list(self.run_.tmp.iterdir()), [])
        self.assertNotIn(KEY, out["message"])

    def test_vd_ac_71_a_live_clock_without_a_secret_is_recorded_with_text_changed(self):
        p = self.run_.cli(self.run_.request(f"{self.server.base}/clock.html"))
        self.assertEqual(p.returncode, 0, p.stderr)
        record = json.loads(p.stdout)
        self.assertEqual((record["redaction"]["text_changed"], record["redaction"]["retried"]), (True, False))


# Runs capture.run with the capture bound lowered and the browser's start held back, then prints, when node exits, how
# the capture ended and whether the held-back browser did start: "late" holds the real start until after the bound and
# then starts with a timeout of its own (the one capture.cjs gives it, computed before the hold, is spent by then);
# "hung" never starts, and fails the way Playwright's launch does when its `timeout` runs out (180 s when none is
# given), so a pending start keeps node alive as a real one does.
START_DRIVER = r"""
const fs = require('fs');
const [module, input] = [process.argv[1], JSON.parse(fs.readFileSync(0, 'utf8'))];
const capture = require(module);
const { chromium } = require(input.core);
capture.BOUNDS.captureSeconds = input.bound;
const wait = (ms) => new Promise((done) => setTimeout(done, ms));
const out = { started: false };
const launch = input.start === 'late'
  ? async (options) => {
    await wait(input.hold_ms);
    const browser = await chromium.launch({ ...options, timeout: 60000 });
    out.started = true;
    return browser;
  }
  : (options) => new Promise((_, fail) => setTimeout(() => fail(Object.assign(
    new Error(`browserType.launch: Timeout ${options.timeout || 180000}ms exceeded.`), { name: 'TimeoutError' })),
  options.timeout || 180000));
capture.run(input.request, { chromium: { launch } }).then(
  () => { Object.assign(out, { ok: true }); },
  (e) => { Object.assign(out, { ok: false, code: e.code, step: e.step }); });
// the capture fails at its bound, before a late browser has started: node ends once that browser is closed
process.on('exit', () => { fs.writeSync(1, JSON.stringify(out)); });
"""


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class TheBoundCoversTheBrowsersStart(unittest.TestCase):
    # The capture bound ends the capture process, even when the bound runs out before the browser has started: a start
    # that hangs (a Chrome that never answers on a loaded Mac) and a start that ends after the bound both end there.
    HANG_GUARD = 60  # s: the process ends a second after its 1 s bound; a start the bound does not end runs 180 s

    def setUp(self):
        self.run_ = _Run()
        self.addCleanup(self.run_.close)
        self.server = _Server().__enter__()
        self.addCleanup(self.server.__exit__)

    def start(self, how, **extra):
        payload = {"request": self.run_.request(f"{self.server.base}/page.html"), "core": PLAYWRIGHT["core"],
                   "bound": 1, "start": how, **extra}
        proc = subprocess.Popen([NODE, "-e", START_DRIVER, str(MODULE)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=self.run_.env(), cwd=str(self.run_.work),
                                start_new_session=True)
        try:
            out, err = proc.communicate(json.dumps(payload), timeout=self.HANG_GUARD)
        except subprocess.TimeoutExpired:
            # node's group, and the browser's own group, which names its profile in the run's TMPDIR
            noderun.stop(proc, str(self.run_.tmp))
            out, err = proc.communicate()
            out = None
        if out is None:
            self.fail(f"the capture process still ran {self.HANG_GUARD} s after its 1 s bound")
        self.assertEqual(proc.returncode, 0, err[-2000:])
        return json.loads(out)

    def test_a_start_that_hangs_ends_with_the_bound_and_writes_nothing(self):
        out = self.start("hung")
        self.assertEqual((out["ok"], out["code"], out["step"]), (False, "time-bound", 1))
        self.assertEqual(self.run_.stored(), [])

    def test_a_browser_that_starts_after_the_bound_is_closed_before_it_loads_the_page(self):
        out = self.start("late", hold_ms=3000)
        self.assertEqual((out["ok"], out["code"], out["step"], out["started"]), (False, "time-bound", 1, True))
        self.assertEqual(self.server.httpd.hits, [], "the capture went on after its bound and loaded the page")
        self.assertEqual(self.run_.stored(), [])


# Runs capture.run with the capture bound lowered, a step that outlasts it, and a browser slow to exit: the first call
# to its close ends `exit_ms` after Playwright's own, the way a browser that disconnected keeps writing its profile
# for a moment, or never ends (`exit_ms` null: a browser that does not answer). Prints, when node exits, how the
# capture ended, how often the browser was closed, whether the capture killed a process group, and, when the
# capture's folder was removed, whether that first close had ended and how many processes still named the folder.
CLOSE_DRIVER = r"""
const fs = require('fs');
const path = require('path');
const [module, input] = [process.argv[1], JSON.parse(fs.readFileSync(0, 'utf8'))];
const capture = require(module);
const { chromium } = require(input.core);
capture.BOUNDS.captureSeconds = input.bound;
const wait = (ms) => new Promise((done) => setTimeout(done, ms));
const out = { closes: 0, exited: false, killed: false, removed: null };
const kill = process.kill;
process.kill = (pid, sig) => {
  if (pid < 0) out.killed = true;
  return kill.call(process, pid, sig);
};
const launch = async (options) => {
  const browser = await chromium.launch(options);
  const close = browser.close.bind(browser);
  browser.close = async () => {
    out.closes += 1;
    if (out.closes > 1) return close();
    if (input.exit_ms === null) return new Promise(() => {});
    await close();
    await wait(input.exit_ms);
    out.exited = true;
  };
  return browser;
};
const rmSync = fs.rmSync;
fs.rmSync = (target, options) => {
  if (path.basename(String(target)).startsWith('devkit-capture-')) {
    const ps = require('child_process').execFileSync('ps', ['-A', '-ww', '-o', 'command='], { encoding: 'utf8' });
    out.removed = { exited: out.exited, browsers: ps.split('\n').filter((line) => line.includes(target)).length };
  }
  return rmSync(target, options);
};
capture.run(input.request, { chromium: { launch } }).then(
  () => { out.code = null; }, (e) => { out.code = e.code; });
process.on('exit', () => { fs.writeSync(1, JSON.stringify(out)); });
"""

# A detector that never answers, started the way dev impeccable starts its engine: a wrapper process that runs the
# engine as its own child, which shares the wrapper's stdout, and waits for it. Each writes its pid into the file
# named by its first argument (the wrapper's line first, the engine's second), and the engine waits two minutes.
HUNG_ENGINE = ("require('fs').appendFileSync(process.argv[1], String(process.pid) + '\\n'); "
               "setTimeout(() => {}, 120000);")
HUNG_DETECTOR = ("const fs = require('fs'); fs.appendFileSync(process.argv[1], String(process.pid) + '\\n'); "
                 "require('child_process').spawn(process.execPath, ['-e', " + json.dumps(HUNG_ENGINE) + ", "
                 "process.argv[1]], { stdio: ['ignore', 'inherit', 'ignore'] }).on('exit', () => process.exit(1));")


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class TheBoundEndsWhatTheCaptureStarted(unittest.TestCase):
    # At the bound the capture stops the browser and the detector, and removes its folder only once the browser has
    # exited, so nothing the capture started outlives it.
    def setUp(self):
        self.run_ = _Run()
        self.addCleanup(self.run_.close)
        self.server = _Server().__enter__()
        self.addCleanup(self.server.__exit__)

    def close_at_the_bound(self, exit_ms):
        payload = {"request": self.run_.request(f"{self.server.base}/page.html", recipe=recipe(steps=["wait 10000"])),
                   "core": PLAYWRIGHT["core"], "bound": 3, "exit_ms": exit_ms}
        p = noderun.run([NODE, "-e", CLOSE_DRIVER, str(MODULE)], json.dumps(payload), timeout=90,
                        env=self.run_.env(), tmpdir=str(self.run_.tmp), cwd=str(self.run_.work))
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        out = json.loads(p.stdout)
        self.assertEqual(out["code"], "time-bound", out)
        self.assertEqual(list(self.run_.tmp.iterdir()), [], "the capture's folder is left behind")
        return out

    def test_the_capture_removes_its_folder_after_the_browser_closed_at_the_bound_has_exited(self):
        # the browser exits 2 s after Playwright's close; on a loaded Mac that can pass the wait for it, and then the
        # capture kills it: either way no browser process is left to write in the folder when it is removed
        out = self.close_at_the_bound(2000)
        self.assertEqual(out["closes"], 1, out)
        self.assertEqual(out["removed"]["browsers"], 0, out)
        self.assertTrue(out["removed"]["exited"] or out["killed"], f"the folder was removed while the browser ran: {out}")

    def test_a_browser_that_does_not_close_at_the_bound_is_stopped_before_its_folder_is_removed(self):
        out = self.close_at_the_bound(None)
        self.assertEqual((out["killed"], out["removed"]["browsers"]), (True, 0), out)
        ps = subprocess.run(["ps", "-A", "-ww", "-o", "command="], capture_output=True, text=True, timeout=30).stdout
        self.assertEqual([line for line in ps.splitlines() if str(self.run_.tmp) in line], [],
                         "the browser outlived the capture")

    def test_a_detector_still_running_at_the_bound_is_stopped_with_its_engine_and_the_capture_process_ends(self):
        # the bound leaves the capture time to reach its checks (step 8) on a loaded Mac; the detector it starts there
        # never answers, and its engine, a child of its own, holds the detector's output open until it ends
        pid_file = self.run_.top / "detector.pid"
        self.addCleanup(self._kill, pid_file)
        request = self.run_.request(f"{self.server.base}/page.html",
                                    impeccable={"command": [NODE, "-e", HUNG_DETECTOR, str(pid_file)],
                                                "version": "hung"})
        payload = {"mode": "capture", "core": PLAYWRIGHT["core"], "request": request,
                   "bounds": {"captureSeconds": 20}}
        started = time.monotonic()
        try:
            p = noderun.run([NODE, str(HARNESS)], json.dumps(payload), timeout=60, env=self.run_.env(),
                            tmpdir=str(self.run_.tmp), cwd=str(self.run_.work))
        except subprocess.TimeoutExpired:
            self.fail("the capture process waited for the detector's engine after its bound")
        took = time.monotonic() - started
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        out = json.loads(p.stdout)
        self.assertEqual((out["ok"], out["code"], out["step"]), (False, "time-bound", 8), out)
        pids = self._pids(pid_file)
        self.assertEqual(len(pids), 2, "the capture did not start the detector and its engine")
        self.assertLess(took, 45, "the capture process waited for the detector after its bound")
        for pid, who in zip(pids, ("detector", "engine")):
            with self.assertRaises(ProcessLookupError, msg=f"the {who} outlived the capture"):
                os.kill(pid, 0)

    @staticmethod
    def _pids(pid_file):
        try:
            return [int(line) for line in pid_file.read_text().split()]
        except (OSError, ValueError):
            return []

    @classmethod
    def _kill(cls, pid_file):
        for pid in cls._pids(pid_file):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class MasksBeyondTheDocumentQuery(unittest.TestCase):
    # masked fields a document-wide query does not reach (open and closed shadow roots), masked text that has no node
    # of its own in the snapshot, and masks that move while the screenshot is taken
    PRIVATE = ("98,765", "493817", "4111111111111111")

    def setUp(self):
        self.run_ = _Run()
        self.addCleanup(self.run_.close)
        self.server = _Server().__enter__()
        self.addCleanup(self.server.__exit__)

    def capture(self, page):
        p = self.run_.cli(self.run_.request(f"{self.server.base}/{page}", check=False))
        self.assertEqual(p.returncode, 0, p.stderr)
        record = json.loads(p.stdout)
        return p, record, self.run_.description(record)

    def assertStoresNothingPrivate(self, p, desc):
        written = p.stdout + json.dumps(desc, ensure_ascii=False)
        for private in self.PRIVATE:
            self.assertNotIn(private, written)

    def test_masked_fields_inside_open_and_closed_shadow_roots_are_painted_and_stored_nowhere(self):
        p, record, desc = self.capture("shadow.html")
        boxes = {f"{root} {name}": [16, top + dy, 300, 40] for root, top in (("open", 16), ("closed", 220))
                 for name, dy in (("one-time code", 0), ("card number", 64), ("data-visual-mask", 128))}
        boxes["plain paragraph, not masked"] = [16, 420, 300, 40]
        seen = self.run_.pixels(self.run_.picture(record), [inset(b, 2) for b in boxes.values()])
        for (name, _), got in zip(boxes.items(), seen["boxes"]):
            with self.subTest(name):
                if name.endswith("not masked"):
                    self.assertGreater(got["nonblack"], 0, "the check tells a painted box from a paragraph")
                else:
                    self.assertEqual(got["nonblack"], 0, f"{got['nonblack']} of {got['total']} pixels show through")
        self.assertStoresNothingPrivate(p, desc)
        self.assertGreaterEqual(record["redaction"]["masked"], 6)

    def test_a_masked_element_with_no_node_of_its_own_takes_its_text_out_of_the_element_that_holds_it(self):
        p, record, desc = self.capture("orphan.html")
        self.assertStoresNothingPrivate(p, desc)
        holders = [e for e in desc["elements"] if e["redacted"] == "mask"]
        self.assertTrue(holders, desc["elements"])
        self.assertEqual([(e["name"], e["text"]) for e in holders], [(None, None)] * len(holders))
        seen = self.run_.pixels(self.run_.picture(record), [inset([16, 16, 300, 40], 2)])
        self.assertEqual(seen["boxes"][0]["nonblack"], 0)

    def test_a_mask_that_moves_during_the_screenshot_is_read_again_and_the_picture_taken_again(self):
        # shift.html moves its masked block 50 px down; move.html also drops its data-visual-mask as it moves
        for page, moved_to in (("shift.html", [16, 66, 300, 40]), ("move.html", [16, 200, 300, 40])):
            with self.subTest(page):
                p, record, desc = self.capture(page)
                seen = self.run_.pixels(self.run_.picture(record), [inset(moved_to, 2)])
                self.assertEqual(seen["boxes"][0]["nonblack"], 0, f"{page}: the moved block shows through")
                self.assertIs(record["redaction"]["retried"], True)
                self.assertStoresNothingPrivate(p, desc)

    def test_a_mask_that_moves_again_after_the_retake_fails_the_capture_and_writes_nothing(self):
        p = self.run_.cli(self.run_.request(f"{self.server.base}/drift.html", check=False))
        self.assertNotEqual(p.returncode, 0, p.stdout)
        self.assertEqual((p.stdout, self.run_.stored()), ("", []))
        self.assertIn("moved", p.stderr)


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class MaskExtent(unittest.TestCase):
    # a mask covers everything its element shows: its own box, its descendants' boxes, and its text's rectangles, so
    # nowrap text that runs past a narrow box, a positioned child, and the content of a box-less wrapper are painted
    PRIVATE = ("98,765", "493817")

    def setUp(self):
        self.run_ = _Run()
        self.addCleanup(self.run_.close)
        self.server = _Server().__enter__()
        self.addCleanup(self.server.__exit__)

    def capture(self, page):
        p = self.run_.cli(self.run_.request(f"{self.server.base}/{page}", check=False,
                                            recipe=recipe(viewport=[800, 600])))
        self.assertEqual(p.returncode, 0, p.stderr)
        record = json.loads(p.stdout)
        return p, record, self.run_.description(record)

    def test_text_and_children_a_masked_element_shows_outside_its_box_are_painted(self):
        _, record, _ = self.capture("spill.html")
        zones = {"text past the 60 px box": [80, 18, 200, 26], "the menu below and right of its button": [218, 142, 296, 36],
                 "the paragraph, not masked": [18, 322, 296, 26]}
        seen = self.run_.pixels(self.run_.picture(record), list(zones.values()))
        for (name, _), got in zip(zones.items(), seen["boxes"]):
            with self.subTest(name):
                if name.endswith("not masked"):
                    self.assertGreater(got["nonblack"], 0, "the check tells a painted box from a paragraph")
                else:
                    self.assertEqual(got["nonblack"], 0, f"{got['nonblack']} of {got['total']} pixels show through")

    def test_a_masked_wrapper_without_a_box_of_its_own_masks_what_it_holds(self):
        p, record, desc = self.capture("spill.html")
        seen = self.run_.pixels(self.run_.picture(record), [[18, 262, 296, 26]])
        self.assertEqual(seen["boxes"][0]["nonblack"], 0, "the paragraph in the display: contents wrapper shows")
        written = p.stdout + json.dumps(desc, ensure_ascii=False)
        for private in self.PRIVATE:
            self.assertNotIn(private, written)

    def test_masked_text_that_moves_outside_the_painted_area_is_read_again_and_painted(self):
        # the masked box stays at [16, 16, 60, 30]; its text moves 300 px right when the screenshot starts
        _, record, _ = self.capture("spill-move.html")
        seen = self.run_.pixels(self.run_.picture(record), [[330, 18, 150, 26]])
        self.assertEqual(seen["boxes"][0]["nonblack"], 0, "the moved text shows through")
        self.assertIs(record["redaction"]["retried"], True)


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class MaskClip(unittest.TestCase):
    # a mask covers only what its element shows: content a box clips (overflow other than visible) is painted inside
    # that box alone, a child the box does not clip is painted where it is, and a track that moves inside its clip
    # is not a moved mask; captured at 800 px as MaskExtent does
    setUp = MaskExtent.setUp
    capture = MaskExtent.capture

    def assertPainted(self, record, zones):
        # each zone is painted solid, or, when its name ends with "not masked", shows the page
        seen = self.run_.pixels(self.run_.picture(record), list(zones.values()))
        for (name, _), got in zip(zones.items(), seen["boxes"]):
            with self.subTest(name):
                if name.endswith("not masked"):
                    self.assertGreater(got["nonblack"], 0, f"{name} is painted over")
                else:
                    self.assertEqual(got["nonblack"], 0, f"{got['nonblack']} of {got['total']} pixels show through")

    def test_a_masked_carousel_whose_track_moves_inside_its_clip_is_captured_without_a_retake(self):
        # the track moves every 50 ms inside the carousel's overflow: hidden box, so what the carousel shows stays put
        _, record, _ = self.capture("carousel.html")
        self.assertPainted(record, {"the carousel": [18, 18, 196, 36],
                                    "the label beside it, not masked": [242, 18, 156, 26]})
        self.assertIs(record["redaction"]["retried"], False)

    def test_the_label_beside_a_still_masked_carousel_is_not_painted(self):
        # the track runs 3,000 px to the right of the carousel, which clips it at 200 px
        _, record, _ = self.capture("carousel.html?still")
        self.assertPainted(record, {"the carousel": [18, 18, 196, 36],
                                    "the label beside it, not masked": [242, 18, 156, 26]})

    def test_a_masked_scrolling_list_is_painted_inside_its_box_and_a_child_its_clip_does_not_hold_still_is(self):
        # the list's 1,200 px of rows scroll inside its 120 px box; the escaping child is placed by the page
        _, record, _ = self.capture("clip.html")
        self.assertPainted(record, {"the scrolling list": [18, 18, 296, 116],
                                    "the paragraph below the list, not masked": [18, 162, 296, 26],
                                    "the child the box does not clip": [402, 302, 200, 26]})

    def test_a_box_above_the_masked_element_that_clips_it_cuts_its_cover(self):
        # the boxes that clip a masked element from above (overflow: hidden, a clip-path inset) cut what it shows too;
        # a clip-path on an element with no box cuts nothing, so its text is painted whole
        _, record, _ = self.capture("clip-above.html")
        # each zone keeps to the 20 px line of text, so a zone painted at all is painted whole
        self.assertPainted(record, {"the text inside the overflow box": [18, 18, 196, 18],
                                    "the paragraph beside the overflow box, not masked": [242, 18, 156, 18],
                                    "the text inside the clip-path": [18, 102, 196, 18],
                                    "the paragraph beside the clip-path, not masked": [242, 102, 156, 18],
                                    "the text of the wrapper whose own clip-path cuts nothing": [18, 186, 400, 18]})

    def test_a_popover_and_a_modal_dialog_inside_masked_clipping_cards_are_painted_where_they_show(self):
        # the top layer is clipped by no ancestor, not even a card with contain: paint or a transform
        _, record, _ = self.capture("toplayer.html?dialog")
        self.assertPainted(record, {"the popover in the contain: paint card": [202, 302, 156, 56],
                                    "the fixed child of the popover": [202, 402, 156, 56],
                                    "the modal dialog in the transformed card": [422, 302, 156, 56]})


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class MaskedTextInSignals(unittest.TestCase):
    # one capture of cut-mask.html with the checks and the stand-in detector, which reports a finding on the marked
    # paragraph and one on `p` (three elements, so no region) whose snippets hold the masked text: no signal keeps it
    PRIVATE = ("98,765", "493817")

    @classmethod
    def setUpClass(cls):
        cls.run_ = _Run()
        cls.server = _Server().__enter__()
        cls.run_.extra = {"DETECTOR_FINDINGS": json.dumps([
            {"antipattern": "low-contrast", "selector": "#marked",
             "snippet": '<p id="marked" data-visual-mask>Balance 98,765.43 private</p>'},
            {"antipattern": "text-overflow", "selector": "p", "snippet": "Account 493817 private"}])}
        cls.p = cls.run_.cli(cls.run_.request(f"{cls.server.base}/cut-mask.html", mask=[".balance"],
                                              impeccable={"command": [NODE, str(DETECTOR)], "version": "test-detector"}))

    @classmethod
    def tearDownClass(cls):
        cls.server.__exit__()
        cls.run_.close()

    def setUp(self):
        self.assertEqual(self.p.returncode, 0, self.p.stderr)
        self.record = json.loads(self.p.stdout)

    def test_no_masked_text_reaches_the_record_through_a_signal(self):
        for private in self.PRIVATE:
            with self.subTest(private):
                self.assertNotIn(private, self.p.stdout)

    def test_a_cut_paragraph_the_page_unmarked_keeps_its_signal_without_its_text(self):
        cuts = [s for s in self.record["checks"]["signals"] if s["check"] == "truncation"]
        self.assertTrue(cuts, self.record["checks"]["signals"])
        self.assertEqual([(s["text"], s["measured"]) for s in cuts], [(None, None)] * len(cuts))

    def test_the_detectors_snippets_holding_masked_text_are_dropped(self):
        found = [s for s in self.record["checks"]["signals"] if s["check"] == "impeccable"]
        self.assertEqual(sorted(s["rule"][0] for s in found), ["impeccable:low-contrast", "impeccable:text-overflow"])
        self.assertEqual([s["measured"] for s in found], [None, None])


@unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
class LazyContent(unittest.TestCase):
    # a full-page picture at 390 px of lazy.html: content that loads only near the viewport is there, 6,000 px down
    @classmethod
    def setUpClass(cls):
        cls.run_ = _Run()
        cls.server = _Server().__enter__()
        cls.p = cls.run_.cli(cls.run_.request(f"{cls.server.base}/lazy.html", check=False))

    @classmethod
    def tearDownClass(cls):
        cls.server.__exit__()
        cls.run_.close()

    def setUp(self):
        self.assertEqual(self.p.returncode, 0, self.p.stderr)
        self.record = json.loads(self.p.stdout)

    def test_a_lazy_image_below_the_fold_is_painted_in_a_full_page_picture(self):
        # the tile is solid black, so a painted tile has no non-black pixel and a blank one is all white
        seen = self.run_.pixels(self.run_.picture(self.record), [[18, 6002, 196, 196]])
        self.assertEqual(seen["boxes"][0]["nonblack"], 0, f"{seen['boxes'][0]['nonblack']} pixels of the tile are blank")

    def test_content_shown_when_scrolled_into_view_is_in_the_description(self):
        texts = [e["text"] for e in self.run_.description(self.record)["elements"] if e["text"]]
        self.assertIn("Shown when scrolled into view", texts)

    def test_a_layout_shift_the_page_makes_as_it_finishes_loading_is_still_measured(self):
        # late-shift.html pushes its card down 300 ms after the load; scrolling through the page then would hide it
        p = self.run_.cli(self.run_.request(f"{self.server.base}/late-shift.html"))
        self.assertEqual(p.returncode, 0, p.stderr)
        shifts = [s for s in json.loads(p.stdout)["checks"]["signals"] if s["check"] == "layout-shift"]
        self.assertEqual([s["anchor"]["selector"] for s in shifts], ["#card"])


@unittest.skipUnless(NODE, "node is not on PATH")
class Adapter(unittest.TestCase):
    # the description adapter on fixture snapshots: what it copies, and what it refuses
    def call(self, fn, *args):
        p = subprocess.run([NODE, str(HARNESS)], input=json.dumps({"mode": "call", "calls": [{"fn": fn, "args": list(args)}]}),
                           capture_output=True, text=True, timeout=60, env=_sandbox.env(), cwd=str(ROOT))
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)["results"][0]

    def adapt(self, fixture):
        snapshot = json.loads((FIXTURES / fixture).read_text())
        return self.call("adapt", snapshot, {"origins": [[0, 0]], "walks": [[]], "frames": {}})

    def test_vd_ac_68_the_adapter_copies_only_allowlisted_fields(self):
        result = self.adapt("snapshot-allowlist.json")
        self.assertTrue(result["ok"], result)
        out = result["value"]
        self.assertNotIn("hunter2-visual", json.dumps(out["elements"]))
        self.assertEqual(sorted(out["values"]), ["E3"], "the textbox's value is held apart, for the scan")
        e = {x["id"]: x for x in out["elements"]}
        self.assertEqual((e["E1"]["role"], e["E1"]["text"]), ("generic", "Sign in"))
        self.assertEqual((e["E2"]["role"], e["E2"]["name"], e["E2"]["states"]), ("heading", "Account", {"level": 2}))
        self.assertEqual((e["E3"]["text"], e["E3"]["states"]), (None, {"invalid": True}))
        self.assertEqual((e["E4"]["url"], e["E4"]["cursor"], e["E4"]["states"]), ("/help", "pointer", {}))
        self.assertEqual(e["E5"]["states"], {"checked": True})
        self.assertEqual([x["parent"] for x in out["elements"]], [None, "E1", "E1", "E1", "E1"])
        allowed = {"id", "parent", "frame", "role", "name", "text", "states", "box", "locator", "selector", "url",
                   "placeholder", "cursor", "style", "above_fold", "component", "redacted"}
        for x in out["elements"]:
            self.assertEqual(set(x), allowed)

    def test_vd_ac_61_a_signal_in_a_frame_is_anchored_on_the_element_of_that_frame(self):
        # anchorElements keys boxes by frame: the same box in the top page and in frame 1 are two elements, and a
        # signal without a frame is the top page's
        desc = {"elements": [
            {"id": "E3", "frame": None, "box": [48, 412, 304, 24], "selector": "#top"},
            {"id": "E10", "frame": 1, "box": [48, 412, 304, 24], "selector": "#paid"}]}
        signal = {"check": "axe", "anchor": {"region": [48, 412, 304, 24], "selector": "#paid"}}
        out = [self.call("anchorElements", [s], desc) for s in ({**signal, "frame": 1}, signal, {**signal, "frame": 2})]
        self.assertEqual([r["value"][0]["anchor"]["element"] for r in out], ["E10", "E3", None])

    def test_a_test_id_selector_becomes_its_locator_with_css_escapes_undone(self):
        # CSS.escape writes a leading digit as a hex escape ("1abc" -> "\31 abc"); the locator holds the real test id
        cases = [('[data-testid="\\31 abc"]', "getByTestId('1abc')"),
                 ('[data-testid="export"]', "getByTestId('export')"),
                 ('[data-testid="a\\"b"]', "getByTestId('a\"b')"),
                 ('[data-testid="\\1F600 x"]', "getByTestId('\U0001F600x')")]
        got = [self.call("fallbackLocator", None, None, sel)["value"] for sel, _ in cases]
        self.assertEqual(got, [want for _, want in cases])

    def test_vd_ac_68_an_unknown_node_type_fails_and_names_it(self):
        result = self.adapt("snapshot-unknown.json")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "snapshot")
        self.assertIn("an object without a role (keys: kind, ref, children)", result["message"])

    @unittest.skipUnless(PLAYWRIGHT, NO_PLAYWRIGHT)
    def test_vd_ac_68_an_unknown_node_in_a_real_capture_writes_nothing(self):
        run = _Run()
        self.addCleanup(run.close)
        with _Server() as server:
            out = run.capture(run.request(f"{server.base}/page.html"),
                              snapshot_file=str(FIXTURES / "snapshot-unknown.json"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "snapshot")
        self.assertIn("an object without a role", out["message"])
        self.assertEqual(run.stored(), [])
        self.assertEqual(list(run.tmp.iterdir()), [])


def _pdf(path, pages):
    # a PDF with one page per (width, height, lines); each line is (x, y, size, text) in PDF points; standard library
    objs = [None, None]
    kids = []
    font = len(objs) + 1
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for width, height, lines in pages:
        text = b"".join(b"BT /F1 %d Tf %d %d Td (%s) Tj ET\n" % (size, x, y, t.encode()) for x, y, size, t in lines)
        objs.append(b"<< /Length %d >>\nstream\n" % len(text) + text + b"endstream")
        content = len(objs)
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Contents %d 0 R "
                    b"/Resources << /Font << /F1 %d 0 R >> >> >>" % (width, height, content, font))
        kids.append(len(objs))
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % k for k in kids), len(kids))
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    Path(path).write_bytes(bytes(out))


@unittest.skipUnless(NODE and GITLEAKS and PLAYWRIGHT and OSASCRIPT and sys.platform == "darwin",
                     NO_PLAYWRIGHT or "PDF pages render with PDFKit through osascript, which only macOS has")
class DocumentPages(unittest.TestCase):
    # A4 pages: landscape 842 x 595 pt renders 1920 px wide (scale 1920 / 842), portrait 595 x 842 pt 1240 px
    @classmethod
    def setUpClass(cls):
        cls.run_ = _Run()
        cls.deck = cls.run_.top / "deck.pdf"
        _pdf(cls.deck, [(842, 595, [(72, 500, 24, f"Page {n} title"), (72, 300, 20, f"Key {KEY}" if n == 2 else "Body")])
                        for n in (1, 2, 3)])
        cls.records = []
        for page in (1, 2, 3):
            p = cls.run_.cli(cls.run_.request(None, source={"file": str(cls.deck), "page": page}))
            cls.records.append(json.loads(p.stdout) if p.returncode == 0 else p.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.run_.close()

    def setUp(self):
        for r in self.records:
            if isinstance(r, str):
                self.fail(f"a page capture failed: {r}")

    def test_vd_ac_6_each_landscape_page_is_1920_px_wide_with_its_number_and_text_lines(self):
        scale = 1920 / 842
        for n, record in enumerate(self.records, 1):
            with self.subTest(page=n):
                self.assertEqual((record["kind"], record["source"]["page"]), ("document", n))
                self.assertEqual((record["image"]["width"], record["image"]["height"]), (1920, round(595 * scale)))
                self.assertAlmostEqual(record["image"]["scale"], scale, places=6)
                title = find(self.run_.description(record), text=f"Page {n} title")
                x, y, w, h = title["box"]
                self.assertEqual(title["role"], "text")
                self.assertAlmostEqual(x, 72 * scale, delta=3)
                baseline = (595 - 500) * scale
                self.assertTrue(y < baseline < y + h, (title["box"], baseline))

    def test_a_key_on_a_pdf_page_is_painted_and_redacted(self):
        record = self.records[1]
        line = find(self.run_.description(record), redacted="secret:aws-access-token")
        scale = 1920 / 842
        x, y, w, h = line["box"]
        self.assertAlmostEqual(x, 72 * scale, delta=3)
        self.assertTrue(y < (595 - 300) * scale < y + h)
        self.assertEqual(line["text"], "[redacted: aws-access-token]")
        seen = self.run_.pixels(self.run_.picture(record), [line["box"]])
        self.assertEqual(seen["boxes"][0]["nonblack"], 0)
        self.assertEqual(record["redaction"]["secret_hits"], 1)
        for file in self.run_.stored():
            data = file.read_bytes()
            self.assertNotIn(KEY.encode(), gzip.decompress(data) if file.suffix == ".gz" else data)

    def test_a_document_record_names_its_file_by_its_base_name_only(self):
        # the folder a PDF sits in names the account and the project; the record keeps the file's name and hash
        for record in self.records:
            self.assertEqual(record["source"]["file"], "deck.pdf")
            self.assertNotIn(str(self.run_.top), json.dumps(record))

    def test_a_portrait_page_renders_1240_px_wide(self):
        portrait = self.run_.top / "portrait.pdf"
        _pdf(portrait, [(595, 842, [(72, 700, 18, "Portrait page")])])
        p = self.run_.cli(self.run_.request(None, source={"file": str(portrait), "page": 1}))
        self.assertEqual(p.returncode, 0, p.stderr)
        record = json.loads(p.stdout)
        self.assertEqual((record["image"]["width"], record["image"]["height"]), (1240, round(842 * 1240 / 595)))

    def test_a_page_past_the_end_is_refused_and_writes_nothing(self):
        before = self.run_.stored()
        p = self.run_.cli(self.run_.request(None, source={"file": str(self.deck), "page": 4}))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("has 3 pages", p.stderr)
        self.assertEqual(self.run_.stored(), before)


if __name__ == "__main__":
    unittest.main()
