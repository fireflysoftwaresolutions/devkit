# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the structure checks of visual diagnosis (`dev visual map check`, lib/devkit/visual/map.py: orphans,
dead ends, depth, journeys over budget, broken edges, redirect chains, loops, link labels, lost context, missing
states and nodes, concept names, component drift; R49, R52, R53, R58, R59) and the map and journey diagrams
(`dev visual map draw`, lib/devkit/visual/mapdiagram.py: R54, R55).

The fixture app (tests/fixtures/visual/app/) plants one defect for each check its journey can show; walked.json is
the walk of its journey. Every test runs in a temporary handoff repository and state folder, with the config file
pointed away from the developer's (core.HANDOFF, core.STATE, core.CONFIG_FILE).
Run: python3 -m unittest tests.test_visual_structure
"""
import json
import shutil
import stat
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

from devkit import core, diagram, identity  # noqa: E402
from devkit.visual import map as vmap  # noqa: E402
from devkit.visual import mapdiagram  # noqa: E402

APP = ROOT / "tests" / "fixtures" / "visual" / "app"
NODE = shutil.which("node")
GITLEAKS = shutil.which("gitleaks")
P = "acme/billing"
J = "J:export-month"


def anchor(journey=None, step=None, edge=None, node=None, state=None):
    return {"journey": journey, "step": step, "edge": edge, "node": node, "state": state}


def found(signals):
    return [(s["check"], s["pass"], s["anchor"]) for s in signals]


def mk(nodes, edges=(), journeys=(), start=None, concepts=(), components=()):
    """A map record as map.py writes it, from short forms: nodes "id" or (id, route, extra); edges (from, to, name)
    or (from, to, name, extra); journeys (id, path, budget, keeps)."""
    out = {**vmap.empty(), "version": 1}
    for n in nodes:
        nid, route, extra = (n, f"/{n.replace('.', '/')}", {}) if isinstance(n, str) else (n + ({},))[:3]
        out["nodes"].append({"id": nid, "title": nid.split(".")[-1].capitalize(), "kind": "screen", "route": route,
                             "page": None, "section": nid.split(".")[0], "origin": "declared", "since": 1,
                             "missing_since": None, "final": False, **extra})
    for e in edges:
        a, b, name, extra = (e + ({},))[:4]
        out["edges"].append({"id": f"{a}>{b}", "from": a, "to": b, "kind": "nav",
                             "trigger": {"role": "link", "name": name, "selector": None}, "when": None,
                             "origin": "declared", "since": 1, "missing_since": None, **extra})
    for jid, path, budget, keeps in journeys:
        names = path.split(" > ")
        out["journeys"].append({"id": jid, "actor": "clerk", "task": "a task", "start": names[0], "end": names[-1],
                                "budget": budget, "keeps": list(keeps), "expect": None, "origin": "declared",
                                "since": 1, "steps": [{"edge": f"{a}>{b}", "do": [], "expect": None, "decision": None}
                                                      for a, b in zip(names, names[1:])]})
    out["start"] = start or out["nodes"][0]["id"]
    out["concepts"] = list(concepts)
    out["components"] = list(components)
    return out


def walk(jid, nodes, **steps):
    """A walk record: one step per node; `steps` adds fields to a step, as s<k>={...}."""
    out = []
    for k, nid in enumerate(nodes, start=1):
        out.append({"step": k, "node": nid, "edge": None if k == 1 else f"{nodes[k - 2]}>{nid}", "status": 200,
                    "redirects": 0, "keeps": {}, **steps.get(f"s{k}", {})})
    return {"journey": jid, "map_version": 1, "broken": None, "steps": out}


def capture(cid, node, state=None, journey=None, step=None):
    return {"id": cid, "audit": "VA-test", "state": state, "map": {"node": node, "state": state, "version": 1,
                                                                    "journey": journey, "step": step}}


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="devkit-visual-structure-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for p in (mock.patch.object(core, "HANDOFF", self.tmp / "handoff"),
                  mock.patch.object(core, "STATE", self.tmp / "state"),
                  mock.patch.object(core, "CONFIG_FILE", self.tmp / "config.json"),
                  mock.patch.object(identity, "hostname", return_value="mac-test")):
            p.start()
            self.addCleanup(p.stop)

    def fixture_map(self):
        vmap.apply(P, json.loads((APP / "declared.json").read_text()), why="declare the fixture app", by="sati")
        return vmap.load(P)

    def fixture_captures(self):
        """The walk's captures of the invoice list (full), and one capture each of its empty and loading states."""
        return [capture("VC-a1", "home", journey=J, step=1),
                capture("VC-a2", "invoices.list", "full", J, 2), capture("VC-a3", "invoices.list", "full", J, 3),
                capture("VC-a4", "invoices.detail", journey=J, step=4),
                capture("VC-a5", "invoices.list", "full", J, 5), capture("VC-a6", "invoices.export", journey=J, step=6),
                capture("VC-b1", "invoices.list", "empty"), capture("VC-b2", "invoices.list", "loading")]


@unittest.skipUnless(NODE and GITLEAKS, "node and gitleaks run the map's redaction (brew install node gitleaks)")
class FixtureAppTest(Sandbox):
    def test_the_fixture_app_and_its_walked_journey_give_exactly_the_planted_signals(self):
        m = self.fixture_map()
        walked = json.loads((APP / "walked.json").read_text())

        signals = vmap.check(m, audit={"id": "VA-test", "scope": "product"}, walks=[walked],
                             captures=self.fixture_captures())

        self.assertEqual(found(signals), [
            ("journey-steps", "S1", anchor(journey=J)),
            ("orphan", "S2", anchor(node="settings.legacy")),
            ("dead-end", "S2", anchor(node="customers.detail")),
            ("loop", "S3", anchor(journey=J, step=5, edge="invoices.detail>invoices.list", node="invoices.list")),
            ("label-mismatch", "S3", anchor(edge="home>customers.list")),
            ("lost-context", "S4", anchor(journey=J, step=5, node="invoices.list")),
            ("concept-naming", "S5", anchor(node="customers.detail")),
            ("missing-state", "S6", anchor(node="invoices.list", state="error")),
        ])
        said = {s["check"]: (s["measured"], s["threshold"], s["rule"]) for s in signals}
        self.assertEqual(said["journey-steps"], ("J:export-month takes 6 steps; its budget is 4", "4",
                                                 ["map:journey-steps"]))
        self.assertEqual(said["orphan"], ("settings.legacy has no link from the start", None, ["map:orphan"]))
        self.assertEqual(said["dead-end"], ("customers.detail has no way on but the browser's back button", None,
                                            ["map:dead-end", "nielsen:3"]))
        self.assertEqual(said["loop"], ("J:export-month returns to invoices.list at step 5 after leaving it at "
                                        "step 4", None, ["map:loop"]))
        self.assertEqual(said["label-mismatch"], ('link "Billing" leads to "Customers"', None,
                                                  ["wcag:2.4.4", "nielsen:4"]))
        self.assertEqual(said["lost-context"], ("the month chosen in step 3 is absent at step 5", None,
                                                ["map:lost-context", "nielsen:6"]))
        self.assertEqual(said["concept-naming"], ('Invoice is "Invoice" on invoices.list and "Bill" on '
                                                  "customers.detail", None, ["nielsen:4"]))
        self.assertEqual(said["missing-state"], ("invoices.list has no capture in the state error", None,
                                                 ["map:missing-state"]))

    def test_signals_have_the_audit_signal_shape_and_keep_their_number_when_checked_again(self):
        m = self.fixture_map()
        walked = json.loads((APP / "walked.json").read_text())
        caps = self.fixture_captures()
        first = vmap.check(m, audit={"id": "VA-test", "scope": "product"}, walks=[walked], captures=caps)

        self.assertEqual([s["n"] for s in first], list(range(1, 9)))
        self.assertEqual({tuple(s) for s in first},
                         {("n", "check", "pass", "rule", "anchor", "measured", "threshold", "triage", "duplicates")})
        self.assertTrue(all(s["triage"] is None and s["duplicates"] == [] for s in first))
        self.assertEqual(vmap.ref("VA-test", first[1]), "VA-test#2")

        triage = {"as": "dismissed", "finding": None, "why": "kept for old links", "by": "sati", "model": "human",
                  "at": "2026-11-17T10:00:00Z"}
        first[1]["triage"] = triage
        gone = {**first[0], "n": 12, "check": "broken-edge", "anchor": anchor(journey=J, step=2)}
        stored = vmap.merge([*first, gone], first)
        again = vmap.check(m, audit={"id": "VA-test", "scope": "product", "signals": stored}, walks=[walked],
                           captures=[c for c in caps if c["map"]["state"] != "empty"])

        self.assertEqual({vmap.ref("VA-test", s): (s["check"], s["anchor"]) for s in again if s["n"] != 13},
                         {vmap.ref("VA-test", s): (s["check"], s["anchor"]) for s in first})
        self.assertEqual(next(s for s in again if s["n"] == 2)["triage"], triage)
        (new,) = [s for s in again if s["n"] == 13]
        self.assertEqual((new["check"], new["anchor"]), ("missing-state", anchor(node="invoices.list", state="empty")))
        self.assertEqual(len(again), 9)
        kept = vmap.merge(stored, again)
        self.assertEqual([s["n"] for s in kept], [1, 2, 3, 4, 5, 6, 7, 8, 12, 13])
        self.assertEqual(kept[8], gone)

    def test_a_journey_audit_checks_only_the_journey_passes_on_the_journeys_own_nodes(self):
        m = self.fixture_map()
        walked = json.loads((APP / "walked.json").read_text())

        signals = vmap.check(m, audit={"id": "VA-test", "scope": "journey", "journeys": [J]}, walks=[walked],
                             captures=self.fixture_captures())

        self.assertEqual([(s["check"], s["pass"]) for s in signals],
                         [("journey-steps", "S1"), ("loop", "S3"), ("lost-context", "S4"), ("missing-state", "S6")])
        self.assertEqual(vmap.check(m, audit={"id": "VA-test", "scope": "screen"}, walks=[walked]), [])


class StructureCheckTest(Sandbox):
    def test_a_section_audit_measures_orphans_from_the_sections_entry(self):
        m = mk(["home", ("reports.list", "/reports", {"title": "Reports"}),
                ("reports.archive", "/reports/archive", {"title": "Archive"}),
                ("reports.month", "/reports/month", {"title": "Month"})],
               [("home", "reports.list", "Reports"), ("home", "reports.archive", "Archive"),
                ("reports.list", "reports.month", "Month"), ("reports.month", "reports.list", "Reports"),
                ("reports.archive", "home", "Home")])

        signals = vmap.check(m, audit={"id": "VA-test", "scope": "section", "within": "/reports"})

        self.assertEqual(found(signals), [("orphan", "S2", anchor(node="reports.archive"))])
        self.assertEqual(signals[0]["measured"], "reports.archive has no link from reports.list, the section's entry")
        self.assertEqual(vmap.check(m, audit={"id": "VA-test", "scope": "product"}), [])

    def test_depth_fires_past_three_edges_from_the_start_for_journey_and_frequent_nodes(self):
        m = mk(["a", "b", "c", "d", ("e", "/e", {"frequent": True}), "f", ("g", "/g", {"final": True})],
               [("a", "b", "B"), ("b", "c", "C"), ("c", "d", "D"), ("d", "e", "E"), ("e", "f", "F"),
                ("f", "g", "G"), ("d", "g", "G")],
               journeys=[("J:deep", "a > b > c > d > g", None, ())])

        signals = vmap.check(m, walks=[walk("J:deep", ["a", "b", "c", "d", "g"])])

        self.assertEqual(found(signals), [("depth", "S2", anchor(node="e")), ("depth", "S2", anchor(node="g"))])
        self.assertEqual([(s["measured"], s["threshold"]) for s in signals],
                         [("e is 4 edges from the start", "3"), ("g is 4 edges from the start", "3")])

    def test_depth_leaves_out_the_pages_of_a_linear_deck(self):
        slides = [(f"deck.p{i}", None, {"kind": "slide", "page": i, "final": i == 8}) for i in range(1, 9)]
        m = mk(slides, [(f"deck.p{i}", f"deck.p{i + 1}", "next", {"kind": "next"}) for i in range(1, 8)],
               journeys=[("J:deck-storyline", " > ".join(f"deck.p{i}" for i in range(1, 9)), None, ())])

        self.assertEqual([s["check"] for s in vmap.check(m) if s["check"] == "depth"], [])

    def test_a_broken_or_redirected_walk_and_crawled_edge_give_broken_edge_and_redirect_chain(self):
        m = mk(["a", "b", "c", ("d", "/d", {"final": True})],
               [("a", "b", "B", {"origin": "crawled", "status": 500}), ("b", "c", "C", {"redirects": 3}),
                ("c", "d", "D"), ("b", "a", "A"), ("c", "a", "A"), ("d", "a", "A")],
               journeys=[("J:go", "a > b > c > d", None, ())])
        w = walk("J:go", ["a", "b", "c"], s2={"redirects": 2}, s3={"status": 404})
        w["broken"] = {"step": 4, "edge": "c>d", "do": 'click role=link[name="D"]', "error": "found nothing in 10 s"}

        signals = vmap.check(m, walks=[w])

        self.assertEqual(found(signals), [
            ("broken-edge", "S1", anchor(journey="J:go", step=3, edge="b>c", node="c")),
            ("broken-edge", "S1", anchor(journey="J:go", step=4, edge="c>d")),
            ("broken-edge", "S1", anchor(edge="a>b")),
            ("redirect-chain", "S1", anchor(journey="J:go", step=2, edge="a>b", node="b")),
            ("redirect-chain", "S1", anchor(edge="b>c")),
        ])
        self.assertEqual([s["measured"] for s in signals], [
            "step 3 answered 404",
            'step 4, click role=link[name="D"], found nothing in 10 s',
            "a>b leads to a page that answers 500",
            "step 2 passed 2 redirects",
            "b>c passes 3 redirects",
        ])

    def test_a_return_with_a_declared_reason_or_an_action_on_the_same_screen_is_no_loop(self):
        m = mk([("a", "/a", {"title": "Export"}), "b", ("c", "/c", {"final": True})],
               [("a", "a", "Filter"), ("a", "b", "B"), ("b", "a", "Export again", {"when": "the export fails"}),
                ("a", "c", "C"), ("b", "c", "C"), ("c", "b", "B")],
               journeys=[("J:retry", "a > a > b > a > c", None, ()), ("J:back", "a > b > c > b", None, ())])

        signals = vmap.check(m, walks=[walk("J:retry", ["a", "a", "b", "a", "c"])])
        self.assertEqual(signals, [])

        signals = vmap.check(m, walks=[walk("J:back", ["a", "b", "c", "b"])])
        self.assertEqual(found(signals), [("loop", "S3", anchor(journey="J:back", step=4, edge="c>b", node="b"))])

    def test_link_labels_are_checked_against_the_title_and_the_headings_captured_on_their_target(self):
        m = mk(["home", ("people.detail", "/people/:id", {"title": "Person"}),
                ("reports.detail", "/reports/:id", {"title": "Report"}), ("zed.detail", "/zed/:id"),
                ("settings", "/settings", {"title": "Settings"}), ("help", "/help", {"title": "Help centre"})],
               [("home", "people.detail", "Ada Lovelace"), ("home", "reports.detail", "Archive"),
                ("home", "zed.detail", "Zed"), ("home", "settings", "Preferences"), ("home", "help", "Get help"),
                *[(n, "home", "Home") for n in ("people.detail", "reports.detail", "zed.detail", "settings", "help")]])
        caps = [capture("VC-p", "people.detail"), capture("VC-r", "reports.detail")]
        descs = {"VC-p": {"outline": [{"level": 1, "text": "Ada Lovelace", "element": "E3"}]},
                 "VC-r": {"outline": [{"level": 1, "text": "Monthly report", "element": "E3"}]}}

        signals = vmap.check(m, captures=caps, descriptions=descs)

        self.assertEqual(found(signals), [("label-mismatch", "S3", anchor(edge="home>reports.detail")),
                                          ("label-mismatch", "S3", anchor(edge="home>settings"))])
        self.assertEqual([s["measured"] for s in signals],
                         ['link "Archive" leads to "Report" (heading "Monthly report")',
                          'link "Preferences" leads to "Settings"'])

    def test_missing_nodes_and_edges_are_signals_and_leave_the_graph(self):
        m = mk(["a", ("b", "/b", {"origin": "crawled", "missing_since": 3}), "c", "d"],
               [("a", "c", "C", {"origin": "crawled", "missing_since": 3}), ("a", "d", "D"), ("d", "a", "A"),
                ("c", "a", "A"), ("b", "a", "A")])

        signals = vmap.check(m)

        self.assertEqual(found(signals), [("orphan", "S2", anchor(node="c")),
                                          ("missing-node", "S2", anchor(node="b")),
                                          ("missing-node", "S2", anchor(edge="a>c"))])
        self.assertEqual([s["measured"] for s in signals[1:]],
                         ["b is missing since v3: the latest crawl did not find it",
                          "a>c is missing since v3: the latest crawl did not find it"])
        self.assertEqual(signals[1]["rule"], ["map:missing"])

    def test_one_component_that_looks_different_on_one_screen_drifts(self):
        m = mk(["home", ("invoices.list", "/invoices", {"title": "Invoices"}),
                ("invoices.detail", "/invoices/:id", {"title": "Invoice"})],
               [("home", "invoices.list", "Invoices"), ("invoices.list", "invoices.detail", "Invoice"),
                ("invoices.detail", "home", "Home"), ("invoices.list", "home", "Home")],
               components=[{"id": "ui.button.primary", "selector": "button[data-variant=primary]"}])

        def button(size, color="#ffffff"):
            return {"outline": [], "elements": [
                {"id": "E1", "role": "button", "component": "ui.button.primary", "box": [0, 0, 80, 32],
                 "style": {"font_size": size, "font_weight": 600, "color": color, "background": "#1a4dd8"}},
                {"id": "E2", "role": "link", "component": None, "box": [0, 40, 80, 20],
                 "style": {"font_size": 11, "font_weight": 400, "color": "#000000", "background": None}}]}
        caps = [capture("VC-h", "home"), capture("VC-l", "invoices.list"), capture("VC-d", "invoices.detail")]
        descs = {"VC-h": button(16), "VC-l": button(16), "VC-d": button(14, "#eeeeee")}

        signals = vmap.check(m, captures=caps, descriptions=descs)

        self.assertEqual(found(signals), [("component-drift", "S5", anchor(node="invoices.detail"))])
        self.assertEqual(signals[0]["measured"], "ui.button.primary on invoices.detail: font size 14 px, text color "
                                                 "#eeeeee; on home, invoices.list: 16 px, #ffffff")
        self.assertEqual(signals[0]["rule"], ["ds:component-drift", "nielsen:4"])


@unittest.skipUnless(NODE and GITLEAKS, "node and gitleaks run the map's redaction (brew install node gitleaks)")
class MapDiagramTest(Sandbox):
    def store(self):
        return core.STATE / "visual" / "acme" / "billing"

    def specs(self):
        return core.HANDOFF / "visual" / "acme" / "billing" / "map" / "diagrams"

    def test_the_fixture_map_and_its_journey_draw_light_and_dark_and_pass_the_check(self):
        self.fixture_map()

        out = mapdiagram.draw(P)

        self.assertEqual([d["name"] for d in out["diagrams"]], ["map", "journey.export-month"])
        self.assertIsNone(out["message"])
        self.assertEqual(out["table"], [])
        folder = self.store() / "map" / "diagrams"
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         ["journey.export-month-dark.svg", "journey.export-month-light.svg", "map-dark.svg",
                          "map-light.svg"])
        self.assertEqual(sorted(p.name for p in self.specs().iterdir()), ["journey.export-month.json", "map.json"])
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual({stat.S_IMODE(p.stat().st_mode) for p in folder.iterdir()}, {0o600})
        for d in out["diagrams"]:
            spec = json.loads(d["spec"].read_text())
            self.assertEqual(diagram.check(spec), [], d["name"])
            self.assertEqual(diagram.check_svg(d["light"]), [], d["name"])
            self.assertIn("<svg", d["dark"].read_text())

        whole = json.loads((self.specs() / "map.json").read_text())
        self.assertEqual({n["id"] for n in whole["nodes"]},
                         {"home", "invoices.list", "invoices.detail", "invoices.export", "customers.list",
                          "customers.detail", "settings.legacy"})
        self.assertEqual({g["title"] for g in whole["groups"]}, {"home", "invoices", "customers", "settings"})
        styles = {(e["from"], e["to"]): e.get("style", "data") for e in whole["edges"]}
        self.assertNotIn(("invoices.list", "invoices.list"), styles)
        self.assertEqual(styles[("invoices.detail", "invoices.list")], "dashed")
        self.assertEqual(styles[("invoices.list", "invoices.detail")], "data")
        self.assertNotIn("primary", styles.values())

        trip = json.loads((self.specs() / "journey.export-month.json").read_text())
        self.assertEqual({n["id"] for n in trip["nodes"]},
                         {"home", "invoices.list", "invoices.detail", "invoices.export"})
        self.assertEqual({(e["from"], e["to"]): e["style"] for e in trip["edges"]},
                         {("home", "invoices.list"): "primary", ("invoices.list", "invoices.detail"): "primary",
                          ("invoices.detail", "invoices.list"): "primary",
                          ("invoices.list", "invoices.export"): "primary"})
        self.assertEqual({n["id"]: n["sub"] for n in trip["nodes"]}["invoices.list"], "steps 2, 3, 5")

    def test_columns_are_distances_from_the_start_and_rows_follow_the_barycenter(self):
        m = mk([(n, f"/{n}", {"section": "t"}) for n in ("s", "p0", "p1", "c1", "c0", "x")],
               [("s", "p0", "P0"), ("s", "p1", "P1"), ("p1", "c1", "C1"), ("p0", "c0", "C0"), ("c1", "s", "S")])

        grid = mapdiagram.layout(m, [n["id"] for n in m["nodes"]], "s")

        self.assertEqual({nid: col for nid, (col, _) in grid.items()}, {"s": 0, "p0": 1, "p1": 1, "c1": 2, "c0": 2,
                                                                        "x": 0})
        self.assertLess(grid["p0"][1], grid["p1"][1])
        self.assertLess(grid["c0"][1], grid["c1"][1])
        self.assertGreater(grid["x"][1], grid["s"][1])

    def test_a_map_too_dense_for_one_diagram_is_drawn_one_section_at_a_time(self):
        nodes, edges = [], []
        for s in "abcd":
            nodes += [{"id": f"{s}.p{i}", "title": f"{s.upper()} page {i}", "route": f"/{s}/p{i}", "section": s}
                      for i in range(10)]
            edges += [{"from": f"{s}.p{(i - 1) // 2}", "to": f"{s}.p{i}", "trigger": f'link "{s.upper()} page {i}"'}
                      for i in range(1, 10)]
            edges.append({"from": f"{s}.p9", "to": f"{s}.p0", "trigger": f'link "{s.upper()} page 0"'})
        edges += [{"from": "a.p0", "to": f"{s}.p0", "trigger": f'link "{s.upper()} page 0"'} for s in "bcd"]
        vmap.apply(P, {"start": "a.p0", "nodes": nodes, "edges": edges}, why="a generated map of 40 screens")

        out = mapdiagram.draw(P)

        self.assertEqual([d["name"] for d in out["diagrams"]], ["map.a", "map.b", "map.c", "map.d"])
        self.assertEqual(out["message"], "⚠ The map has 40 screens: too many for one clear diagram. Drew 4 sections "
                                         "instead (a, b, c, d); each passed dev diagram check.")
        for d in out["diagrams"]:
            spec = json.loads(d["spec"].read_text())
            self.assertEqual(len(spec["nodes"]), 10)
            self.assertEqual(diagram.check(spec), [], d["name"])
        self.assertFalse((self.specs() / "map.json").exists())

    def test_a_section_too_dense_to_draw_falls_back_to_the_node_and_edge_table(self):
        nodes = [{"id": f"x.p{i}", "title": f"Page {i}", "route": f"/x/p{i}"} for i in range(30)]
        edges = [{"from": f"x.p{i - 1}", "to": f"x.p{i}", "trigger": f'link "Page {i}"'} for i in range(1, 30)]
        vmap.apply(P, {"start": "x.p0", "nodes": nodes, "edges": edges}, why="a map of 30 screens in one section")
        stale = self.store() / "map" / "diagrams" / "map-light.svg"
        stale.parent.mkdir(parents=True)
        stale.write_text("<svg/>")

        out = mapdiagram.draw(P)

        self.assertEqual(out["diagrams"], [])
        self.assertEqual(out["table"], ["x"])
        self.assertEqual(out["message"], "⚠ The map has 30 screens: too many for one clear diagram, and section x "
                                         "is too dense to draw on its own. The report shows its screens and edges "
                                         "as a table.")
        self.assertFalse(stale.exists())


if __name__ == "__main__":
    unittest.main()
