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
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
import _sandbox  # noqa: E402  (the net against this Mac's real codex, claude, brew and gh)

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


class _Handler(SimpleHTTPRequestHandler):
    # serves the fixtures folder; HTML pages get their placeholders filled, so no fixture file holds a whole secret
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FIXTURES), **kwargs)

    def do_GET(self):  # noqa: N802 (the http.server name)
        path = self.path.split("?")[0].split("#")[0]
        file = FIXTURES / path.lstrip("/")
        if path.endswith(".html") and file.is_file():
            port = self.server.server_address[1]
            body = (file.read_text().replace("{{KEY}}", KEY).replace("{{JWT}}", JWT)
                    .replace("{{CROSS}}", f"http://localhost:{port}")).encode()
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

    def env(self, path=None):
        e = _sandbox.env(TMPDIR=str(self.tmp), PLAYWRIGHT_BROWSERS_PATH=PLAYWRIGHT["browsers"],
                         DETECTOR_LOG=str(self.log))
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
        self.assertNotIn("‮", everything)
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
        self.assertEqual((r["kind"], r["state"], r["reproducible"], r["experiment"]), ("web", "planted", True, False))
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
        low = find(self.desc, text="Low contrast note")
        signals = [s for s in self.record["checks"]["signals"] if s["check"] == "impeccable"]
        self.assertEqual(len(signals), 1)
        s = signals[0]
        self.assertEqual((s["pass"], s["rule"], s["measured"]), ("color", ["impeccable:low-contrast"], "1.9:1 (need 4.5:1)"))
        self.assertEqual((s["anchor"]["element"], s["anchor"]["selector"], s["anchor"]["region"]),
                         (low["id"], "#low", low["box"]))
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
