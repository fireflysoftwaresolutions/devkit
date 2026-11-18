# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the product map of visual diagnosis, lib/devkit/visual/map.py: versioned map records in the handoff
repository (R42), the declare functions for nodes, edges, journeys, states, concepts, and components (R43-R47, R61),
route patterns from redact.cjs, every map string through `node redact.cjs --json` (R48, R71, R15, R14), and the
attachment of captures and findings when the map changes (R50, R60).

Every test runs in a temporary handoff repository and state folder, with the config file pointed away from the
developer's (core.HANDOFF, core.STATE, core.CONFIG_FILE). node and gitleaks run for real: the redaction is the
behavior under test.
Run: python3 -m unittest tests.test_visual_map
"""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
sys.path.insert(0, str(ROOT / "lib"))

from devkit import core, identity  # noqa: E402
from devkit.visual import map as vmap  # noqa: E402

APP = ROOT / "tests" / "fixtures" / "visual" / "app"
NODE = shutil.which("node")
GITLEAKS = shutil.which("gitleaks")
P = "acme/billing"
KEY = "AKIA" + "QYLPMN5HHHFPZAM2"  # an AWS access key id, in two parts so no scanner reads this file as one
JWT = ".".join(["eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ",
                "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"])
SECRET = "[redacted: aws-access-token]"
ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


@unittest.skipUnless(NODE and GITLEAKS, "node and gitleaks run the redaction (brew install node gitleaks)")
class MapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="devkit-visual-map-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for p in (mock.patch.object(core, "HANDOFF", self.tmp / "handoff"),
                  mock.patch.object(core, "STATE", self.tmp / "state"),
                  mock.patch.object(core, "CONFIG_FILE", self.tmp / "config.json"),
                  mock.patch.object(identity, "hostname", return_value="mac-test")):
            p.start()
            self.addCleanup(p.stop)
        self.dir = core.HANDOFF / "visual" / "acme" / "billing"

    def files(self):
        return {p: p.read_text() for p in self.dir.rglob("*") if p.is_file()}

    def assertNowhere(self, *texts):
        for path, text in self.files().items():
            for t in texts:
                self.assertNotIn(t, text, f"{path} holds {t[:6]}…")

    def node(self, m, nid):
        return next(n for n in m["nodes"] if n["id"] == nid)

    # ------------------------------------------------------------------ versions (R42)

    def test_each_change_writes_a_new_version_and_keeps_every_earlier_one(self):
        vmap.add_node(P, "home", "Home", route="/", by="sati", why="start the map at the home page")
        v1 = (self.dir / "map" / "v1.json").read_bytes()
        vmap.add_node(P, "invoices.list", "Invoices", route="/invoices", by="trinity")

        current = vmap.load(P)
        self.assertEqual(current["version"], 2)
        self.assertEqual([n["id"] for n in current["nodes"]], ["home", "invoices.list"])
        self.assertEqual((self.dir / "map" / "v1.json").read_bytes(), v1)
        self.assertEqual([n["id"] for n in vmap.load(P, version=1)["nodes"]], ["home"])
        self.assertEqual(json.loads((self.dir / "map" / "current.json").read_text()), vmap.load(P, version=2))
        log = current["changelog"]
        self.assertEqual([(e["v"], e["by"], e["host"]) for e in log],
                         [(1, "sati", "mac-test"), (2, "trinity", "mac-test")])
        self.assertEqual(log[0]["why"], "start the map at the home page")
        self.assertIn("invoices.list", log[1]["why"])
        self.assertTrue(all(ISO.match(e["at"]) for e in log))
        with self.assertRaises(vmap.MapError) as cm:
            vmap.load(P, version=7)
        self.assertEqual(str(cm.exception), "No map v7: the map is at v2.")

    def test_a_change_made_elsewhere_since_the_read_is_refused(self):
        vmap.add_node(P, "home", "Home", route="/", by="sati")
        stale = vmap.load(P)
        with mock.patch.object(identity, "hostname", return_value="mac-studio"):
            vmap.add_node(P, "invoices.list", "Invoices", route="/invoices", by="kid")
        stale["partial"] = True
        with self.assertRaises(vmap.MapConflict) as cm:
            vmap.save(P, stale, base=1, why="crawl from /", by="neo")
        self.assertEqual(str(cm.exception), "The map changed to v2 on mac-studio. Run the command again on v2.")
        self.assertFalse((self.dir / "map" / "v3.json").exists())
        self.assertEqual(vmap.load(P)["version"], 2)
        self.assertFalse(vmap.load(P)["partial"])

    # ------------------------------------------------------------------ route patterns (R15, the map's routes)

    def test_routes_become_route_patterns_from_redact_cjs(self):
        vmap.add_node(P, "invoices.detail", "Invoice", route="/invoices/1204")
        vmap.add_node(P, "reset", "Reset password", route=f"/reset/{JWT}?X-Amz-Signature=a&api_key=k#access_token=t")
        vmap.add_node(P, "app.invoices", "Invoices", route="http://127.0.0.1:4200/#/invoices/88?tab=open")

        m = vmap.load(P)
        self.assertEqual({n["id"]: n["route"] for n in m["nodes"]},
                         {"invoices.detail": "/invoices/:id", "reset": "/reset/:token",
                          "app.invoices": "/#/invoices/:id"})
        self.assertEqual(self.node(m, "invoices.detail")["section"], "invoices")
        self.assertEqual(vmap.route_patterns(["/invoices?month=2026-10", "/customers/7/bills"]),
                         ["/invoices", "/customers/:id/bills"])
        self.assertNowhere(JWT.split(".")[2], "access_token=t")

    def test_unreadable_output_of_redact_or_crawl_is_a_plain_error(self):
        for garbled in ("", "not json", "{\"strings\": ["):
            with self.subTest(output=garbled), mock.patch.object(vmap, "_run_node", return_value=garbled):
                for call in (lambda: vmap.redact_strings(["a"]), lambda: vmap.route_patterns(["/a"]),
                             lambda: vmap.route_of(vmap.empty(), "/a")):
                    with self.assertRaisesRegex(vmap.MapError, "answered with something devkit cannot read"):
                        call()

    # ------------------------------------------------------------------ every map string redacted (R48, R71)

    def test_every_map_string_loses_secrets_urls_credentials_and_control_characters(self):
        vmap.add_node(P, "home", f"Home {KEY}", route="/")
        vmap.add_node(P, "invoices.list", "Invoices\x1b]52;c;aGk=\x07 \u202elist", route="/invoices")
        vmap.add_edge(P, "home", "invoices.list", trigger=f'link "{KEY}"', when="see /auth?token=abc123&to=/now",
                      why=f"the link reads {KEY}")

        m = vmap.load(P)
        self.assertEqual(self.node(m, "home")["title"], SECRET)
        title = self.node(m, "invoices.list")["title"]
        self.assertTrue(title.startswith("Invoices"))
        for ch in ("\x1b", "\x07", "\u202e"):
            self.assertNotIn(ch, title)
        (edge,) = m["edges"]
        self.assertEqual(edge["trigger"], {"role": "link", "name": SECRET, "selector": None})
        self.assertEqual(edge["when"], "see /auth?token=[redacted]&to=/now")
        self.assertEqual(m["changelog"][-1]["why"], SECRET)
        self.assertNowhere(KEY, "abc123")

    def test_an_identifier_holding_a_secret_is_refused_and_nothing_is_written(self):
        vmap.add_node(P, "home", "Home", route="/")
        before = self.files()
        with self.assertRaises(vmap.MapError) as cm:
            vmap.add_state(P, "home", f"full {KEY}")
        self.assertIn("aws-access-token", str(cm.exception))
        self.assertNotIn(KEY, str(cm.exception))
        self.assertEqual(self.files(), before)

    def test_without_gitleaks_nothing_is_written(self):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        os.symlink(NODE, bin_dir / "node")
        with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}):
            with self.assertRaises(vmap.MapError) as cm:
                vmap.add_node(P, "home", "Home", route="/")
        self.assertIn("gitleaks is not installed", str(cm.exception))
        self.assertEqual(self.files(), {})

    # ------------------------------------------------------------------ declare commands (R43-R47, R61)

    def test_ids_must_be_lowercase_words(self):
        with self.assertRaises(vmap.MapError) as cm:
            vmap.add_node(P, "Invoices List", "Invoices", route="/invoices")
        self.assertEqual(str(cm.exception), '"Invoices List" is not a node id: use lowercase letters, digits, dots, '
                                            'dashes, and underscores, such as invoices.list.')
        with self.assertRaises(vmap.MapError):
            vmap.add_node(P, "invoices.list", "Invoices")  # neither a route nor a page
        self.assertEqual(self.files(), {})

    def test_an_edge_to_an_unknown_node_is_refused_with_the_nearest_id(self):
        vmap.add_node(P, "home", "Home", route="/")
        vmap.add_node(P, "invoices.export", "Export invoices", route="/invoices", kind="overlay")
        with self.assertRaises(vmap.MapError) as cm:
            vmap.add_edge(P, "home", "invoices.exprot", trigger='button "Export"')
        self.assertEqual(str(cm.exception), 'No node "invoices.exprot" in map v2. Nearest: invoices.export. '
                                            'List them with dev visual map show.')
        self.assertEqual(vmap.load(P)["version"], 2)

    def test_declared_nodes_edges_journeys_and_states_have_the_fields_of_the_map_format(self):
        vmap.add_node(P, "home", "Home", route="/")
        vmap.add_node(P, "invoices.list", "Invoices", route="/invoices")
        vmap.add_node(P, "invoices.export", "Export invoices", route="/invoices", kind="overlay", final=True)
        vmap.add_edge(P, "home", "invoices.list", trigger='role=link[name="Invoices"]')
        vmap.add_edge(P, "invoices.list", "invoices.export", trigger="[data-testid=export]", kind="action",
                      when="the month has invoices")
        vmap.add_journey(P, "J:export-month", actor="finance clerk", task="Export a month of invoices as CSV",
                         path="home > invoices.list > invoices.export",
                         do=['click role=link[name="Invoices"]', ["select #month 2026-10", "click text=Export"]],
                         decisions=["invoices.list: the month has invoices, or the empty state"],
                         expect="A CSV of the month's invoices downloads", budget=4, keeps=["month"])
        vmap.add_state(P, "invoices.list", "loading")
        vmap.add_state(P, "invoices.list", "full", from_state="loading", on="invoices loaded")

        m = vmap.load(P)
        self.assertEqual(m["start"], "home")
        self.assertEqual(self.node(m, "invoices.export"), {
            "id": "invoices.export", "title": "Export invoices", "kind": "overlay", "route": "/invoices", "page": None,
            "section": "invoices", "origin": "declared", "since": 3, "missing_since": None, "final": True,
            "example": None})
        self.assertEqual(m["edges"][1], {
            "id": "invoices.list>invoices.export", "from": "invoices.list", "to": "invoices.export", "kind": "action",
            "trigger": {"role": None, "name": None, "selector": "[data-testid=export]"},
            "when": "the month has invoices", "origin": "declared", "since": 5, "missing_since": None})
        self.assertEqual(m["edges"][0]["trigger"], {"role": "link", "name": "Invoices", "selector": None})
        self.assertEqual(m["journeys"], [{
            "id": "J:export-month", "actor": "finance clerk", "task": "Export a month of invoices as CSV",
            "start": "home", "end": "invoices.export", "budget": 4, "keeps": ["month"],
            "expect": "A CSV of the month's invoices downloads", "origin": "declared", "since": 6,
            "steps": [{"edge": "home>invoices.list", "do": ['click role=link[name="Invoices"]'], "expect": None,
                       "decision": None},
                      {"edge": "invoices.list>invoices.export", "do": ["select #month 2026-10", "click text=Export"],
                       "expect": None, "decision": "the month has invoices, or the empty state"}]}])
        self.assertEqual(self.node(m, "invoices.list")["states"], {
            "initial": "loading", "states": ["loading", "full"],
            "events": [{"from": "loading", "to": "full", "on": "invoices loaded"}]})

    def test_a_journey_over_an_edge_the_map_lacks_and_an_unknown_state_are_refused(self):
        vmap.add_node(P, "home", "Home", route="/")
        vmap.add_node(P, "invoices.list", "Invoices", route="/invoices")
        vmap.add_state(P, "invoices.list", "loading")
        with self.assertRaises(vmap.MapError) as cm:
            vmap.add_journey(P, "J:browse", actor="clerk", task="Find an invoice", path="home > invoices.list")
        self.assertEqual(str(cm.exception), 'No edge home>invoices.list in map v3. Add it first: '
                                            'dev visual map edge add home invoices.list --trigger "…"')
        with self.assertRaises(vmap.MapError) as cm:
            vmap.add_state(P, "invoices.list", "full", from_state="loadng", on="invoices loaded")
        self.assertEqual(str(cm.exception), 'No state "loadng" on invoices.list. Nearest: loading.')
        self.assertEqual(vmap.load(P)["version"], 3)

    def test_concepts_and_components_are_declared_and_merged(self):
        vmap.add_node(P, "invoices.list", "Invoices", route="/invoices")
        vmap.add_node(P, "customers.detail", "Customer", route="/customers/7")
        vmap.add_concept(P, "Invoice", relates=["Customer:belongs to"], labels=["invoices.list=Invoice"])
        vmap.add_concept(P, "Invoice", labels=["customers.detail=Bill"])
        vmap.add_component(P, "ui.button.primary", selector="[data-variant=primary]")

        m = vmap.load(P)
        self.assertEqual(m["concepts"], [{"name": "Invoice", "relates": [{"to": "Customer", "as": "belongs to"}],
                                          "labels": {"invoices.list": "Invoice", "customers.detail": "Bill"}}])
        self.assertEqual(m["components"], [{"id": "ui.button.primary", "selector": "[data-variant=primary]"}])
        with self.assertRaises(vmap.MapError) as cm:
            vmap.add_concept(P, "Customer", labels=["customer.detail=Customer"])
        self.assertIn('No node "customer.detail"', str(cm.exception))

    def test_removing_a_node_takes_its_edges_and_is_refused_while_a_journey_uses_it(self):
        for nid, route in (("home", "/"), ("invoices.list", "/invoices"), ("customers.detail", "/customers/7")):
            vmap.add_node(P, nid, nid, route=route)
        vmap.add_edge(P, "home", "invoices.list", trigger='link "Invoices"')
        vmap.add_edge(P, "invoices.list", "customers.detail", trigger='link "Northwind"')
        vmap.add_journey(P, "J:browse", actor="clerk", task="Find an invoice", path="home > invoices.list")

        vmap.remove(P, "customers.detail", why="the customer page was dropped")
        m = vmap.load(P)
        self.assertEqual([n["id"] for n in m["nodes"]], ["home", "invoices.list"])
        self.assertEqual([e["id"] for e in m["edges"]], ["home>invoices.list"])
        self.assertEqual(m["changelog"][-1]["why"], "the customer page was dropped")
        with self.assertRaises(vmap.MapError) as cm:
            vmap.remove(P, "invoices.list", why="merged into home")
        self.assertEqual(str(cm.exception), 'invoices.list is in journey J:browse. Remove the journey first: '
                                            'dev visual map remove J:browse --why "…"')
        vmap.remove(P, "J:browse", why="no longer a task")
        vmap.remove(P, "invoices.list", why="merged into home")
        self.assertEqual([n["id"] for n in vmap.load(P)["nodes"]], ["home"])

    def test_the_fixture_apps_declared_map_applies_as_one_version(self):
        doc = json.loads((APP / "declared.json").read_text())
        vmap.apply(P, doc, why="declare the fixture app", by="sati")

        m = vmap.load(P)
        self.assertEqual(m["version"], 1)
        self.assertEqual(m["start"], "home")
        self.assertEqual({n["id"] for n in m["nodes"]}, {n["id"] for n in doc["nodes"]})
        self.assertEqual(self.node(m, "invoices.detail")["route"], "/invoices/:id")
        self.assertEqual(self.node(m, "invoices.list")["states"]["initial"], "loading")
        (journey,) = m["journeys"]
        self.assertEqual([s["edge"] for s in journey["steps"]],
                         ["home>invoices.list", "invoices.list>invoices.list", "invoices.list>invoices.detail",
                          "invoices.detail>invoices.list", "invoices.list>invoices.export"])
        self.assertEqual(journey["steps"][1]["decision"], "the month has invoices, or the empty state")
        self.assertEqual(m["concepts"][0]["labels"], {"invoices.list": "Invoice", "customers.detail": "Bill"})
        self.assertEqual(m["changelog"], [{"v": 1, "at": m["changelog"][0]["at"], "by": "sati", "host": "mac-test",
                                           "why": "declare the fixture app"}])

        doc["edges"].append({"from": "home", "to": "settings", "trigger": "link \"Settings\""})
        with self.assertRaises(vmap.MapError):
            vmap.apply(P, doc, why="again")
        self.assertEqual(vmap.load(P)["version"], 1)

    # ------------------------------------------------------------------ captures and findings (R50, R60)

    def write_record(self, folder, rid, record):
        path = self.dir / folder / f"{rid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2))

    def read_record(self, folder, rid):
        return json.loads((self.dir / folder / f"{rid}.json").read_text())

    def capture(self, cid, route, kind="web"):
        source = {"url": f"http://127.0.0.1:4200{route}", "origin": "local", "route": route} if route else {}
        self.write_record("captures", cid, {"id": cid, "kind": kind, "state": "full", "source": source,
                                            "map": None, "taken": "2026-11-17T10:00:00Z"})

    def test_when_the_map_changes_captures_and_their_findings_attach_to_the_one_node_they_match(self):
        self.capture("VC-aaaa", "/invoices")
        self.capture("VC-cccc", None, kind="image")
        self.write_record("findings", "VF-aaaa", {
            "id": "VF-aaaa", "capture": "VC-aaaa", "status": "open",
            "anchor": {"element": "E57", "selector": "[data-variant=primary]", "node": None, "state": None,
                       "component": None},
            "history": [{"at": "2026-11-17T10:01:00Z", "by": "sati", "change": "open", "why": "from signal #1"}]})

        result = vmap.add_node(P, "invoices.list", "Invoices", route="/invoices", by="sati")
        self.assertEqual(result["attach"]["attached"], [{"capture": "VC-aaaa", "node": "invoices.list"}])
        capture = self.read_record("captures", "VC-aaaa")
        self.assertEqual((capture["map"]["node"], capture["map"]["version"]), ("invoices.list", 1))
        self.assertEqual(capture["history"][-1]["change"], "attached to invoices.list (map v1)")
        finding = self.read_record("findings", "VF-aaaa")
        self.assertEqual(finding["anchor"]["node"], "invoices.list")
        self.assertEqual(finding["history"][-1]["change"], "attached to invoices.list (map v1)")
        self.assertIsNone(self.read_record("captures", "VC-cccc")["map"])

        vmap.add_node(P, "customers.detail", "Customer", route="/customers/7")
        vmap.add_node(P, "customers.edit", "Edit customer", route="/customers/:id")
        self.capture("VC-bbbb", "/customers/:id")
        listed = vmap.attach(P, by="sati")
        ambiguous = [{"capture": "VC-bbbb", "nodes": ["customers.detail", "customers.edit"]}]
        self.assertEqual(listed, {"attached": [], "findings": [], "ambiguous": ambiguous})
        self.assertIsNone(self.read_record("captures", "VC-bbbb")["map"])
        with self.assertRaises(vmap.MapError):
            vmap.attach(P, choose={"../findings/VF-aaaa": "customers.detail"})  # a path, not a capture id
        chosen = vmap.attach(P, choose={"VC-bbbb": "customers.detail"}, by="sati")
        self.assertEqual(chosen["attached"], [{"capture": "VC-bbbb", "node": "customers.detail"}])
        self.assertEqual(self.read_record("captures", "VC-bbbb")["map"]["node"], "customers.detail")

        vmap.add_component(P, "ui.button.primary", selector="button.btn-primary, [data-variant=primary]")
        finding = self.read_record("findings", "VF-aaaa")
        self.assertEqual(finding["anchor"]["component"], "ui.button.primary")
        self.assertEqual(finding["history"][-1]["change"], "attached to component ui.button.primary (map v4)")

    def test_a_finding_joins_its_component_however_a_writer_spelled_the_selector(self):
        # the checks write a path from the root, the description the shortest one, a person types any spacing
        self.capture("VC-aaaa", "/invoices")
        anchors = {"VF-aaaa": "html > body > main > button.btn-primary", "VF-bbbb": "main>button.btn-primary"}
        for fid, selector in anchors.items():
            self.write_record("findings", fid, {
                "id": fid, "capture": "VC-aaaa", "status": "open", "history": [],
                "anchor": {"element": "E57", "selector": selector, "node": None, "state": None, "component": None}})
        vmap.add_component(P, "ui.button.primary", selector="main > button.btn-primary, [data-variant=primary]")
        for fid in anchors:
            self.assertEqual(self.read_record("findings", fid)["anchor"]["component"], "ui.button.primary", fid)

    def test_captures_match_nodes_by_route_pattern_or_page(self):
        vmap.add_node(P, "invoices.list", "Invoices", route="/invoices")
        vmap.add_node(P, "deck.p2", "Slide 2", page=2, kind="slide")
        m = vmap.load(P)
        self.assertEqual(vmap.match_nodes(m, route="/invoices"), ["invoices.list"])
        self.assertEqual(vmap.match_nodes(m, page=2), ["deck.p2"])
        self.assertEqual(vmap.match_nodes(m, route="/reports"), [])


if __name__ == "__main__":
    unittest.main()
