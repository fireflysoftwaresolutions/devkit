# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the diagram engine and validator. Run: python3 -m unittest discover -s tests -v"""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from devkit import diagram as d  # noqa: E402


def flow(**over):
    spec = {"name": "t", "kind": "flow", "title": "Test", "desc": "A test diagram.",
            "nodes": [{"id": "a", "label": "Alpha", "col": 0, "row": 0},
                      {"id": "b", "label": "Beta", "col": 1, "row": 0},
                      {"id": "c", "label": "Gamma", "col": 2, "row": 0}],
            "edges": [{"from": "a", "to": "b", "label": "calls"}, {"from": "b", "to": "c", "label": "writes"}]}
    spec.update(over)
    return spec


class RenderTests(unittest.TestCase):
    def test_every_kind_renders_valid_svg(self):
        for kind in d.KINDS:
            with self.subTest(kind=kind):
                spec = d.scaffold(kind, f"t-{kind}")
                for theme in ("light", "dark"):
                    svg = d.render(spec, theme)
                    root = ET.fromstring(svg)
                    self.assertTrue(root.get("viewBox"))
                    self.assertIn("<title", svg)
                    self.assertIn("<desc", svg)
                    self.assertNotIn("<script", svg)
                    self.assertNotIn("foreignObject", svg)
                    self.assertIsNone(re.search(r'href="(?!#)', svg), "no external references")

    def test_scaffolds_validate_clean(self):
        for kind in d.KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(d.check(d.scaffold(kind, "x")), [])

    def test_valid_spec_has_no_problems(self):
        self.assertEqual(d.check(flow()), [])

    def test_text_is_escaped(self):
        svg = d.render(flow(nodes=[{"id": "a", "label": "<script>alert(1)</script>", "col": 0, "row": 0}], edges=[]))
        self.assertNotIn("<script>", svg)
        self.assertIn("&lt;script&gt;", svg)

    def test_wrap_never_shrinks_type(self):
        lines = d.wrap("a fairly long label that has to wrap onto lines", 16, 600, 160)
        self.assertGreater(len(lines), 1)
        for ln in lines[:-1]:
            self.assertLessEqual(d.text_width(ln, 16, 600), 160 + 1e-6)

    def test_write_produces_both_themes(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp) / "x.json"
            spec.write_text(json.dumps(flow(name="x")))
            paths = d.write(spec, Path(tmp) / "out")
            names = sorted(p.name for p in paths)
            self.assertEqual(names, ["x-dark.svg", "x-light.svg"])
            for p in paths:
                self.assertEqual(d.check_svg(p), [])


class ValidatorTests(unittest.TestCase):
    def test_overlap(self):
        spec = flow(grid={"col": 120, "row": 120},
                    nodes=[{"id": "a", "label": "Alpha with a long name", "col": 0, "row": 0},
                           {"id": "b", "label": "Beta with a long name", "col": 1, "row": 0}], edges=[])
        self.assertTrue(any(p.startswith("overlap") for p in d.check(spec)))

    def test_overflow(self):
        spec = flow(nodes=[{"id": "a", "label": "An extraordinarily verbose label for a tiny box", "width": 96,
                            "col": 0, "row": 0}], edges=[])
        self.assertTrue(any(p.startswith("overflow") for p in d.check(spec)))

    def test_edge_through_node(self):
        spec = flow(edges=[{"from": "a", "to": "c", "route": "straight"}])
        self.assertTrue(any("runs through node b" in p for p in d.check(spec)))

    def test_label_overlap(self):
        spec = flow(grid={"col": 200, "row": 120}, edges=[{"from": "a", "to": "b", "label": "a very long edge label"}])
        self.assertTrue(any(p.startswith("label") for p in d.check(spec)))

    def test_low_contrast(self):
        m = d.Model("flow", "t", "d")
        m.W, m.H = 800, 400
        m.texts.append(("faint", 16, 600, "#BFB8AB", "#F3F1EB", d.Rect(100, 120, 60, 16)))
        self.assertTrue(any(p.startswith("contrast") for p in d.validate(m)))

    def test_small_type(self):
        m = d.Model("flow", "t", "d")
        m.W, m.H = 800, 400
        m.texts.append(("tiny", 10, 400, "#272820", "#F3F1EB", d.Rect(100, 120, 30, 10)))
        self.assertTrue(any(p.startswith("type") for p in d.validate(m)))

    def test_missing_title_and_desc(self):
        spec = flow()
        spec.pop("title")
        spec.pop("desc")
        probs = d.check(spec)
        self.assertTrue(any("missing title" in p for p in probs))
        self.assertTrue(any("missing description" in p for p in probs))

    def test_one_amber_focal(self):
        spec = flow(nodes=[{"id": "a", "label": "Alpha", "col": 0, "row": 0, "emphasis": True},
                           {"id": "b", "label": "Beta", "col": 1, "row": 0, "emphasis": True},
                           {"id": "c", "label": "Gamma", "col": 2, "row": 0}])
        self.assertTrue(any(p.startswith("amber") for p in d.check(spec)))

    def test_primary_path_counts_once(self):
        spec = flow(edges=[{"from": "a", "to": "b", "style": "primary"}, {"from": "b", "to": "c", "style": "primary"}])
        self.assertFalse(any(p.startswith("amber") for p in d.check(spec)))

    def test_check_svg_flags_problems(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.svg"
            p.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10" '
                         'fill="#F3F1EB"/><text x="1" y="8" font-size="9" fill="#BFB8AB">x</text>'
                         '<image href="https://example.com/x.png"/></svg>')
            probs = d.check_svg(p)
            for code in ("svg: no viewBox", "a11y: no <title>", "type:", "contrast:", "svg: external reference"):
                self.assertTrue(any(x.startswith(code) for x in probs), code)


class DevkitDiagramsTests(unittest.TestCase):
    def test_own_specs_validate_clean(self):
        specs = sorted((ROOT / "docs" / "diagrams").glob("*.json"))
        self.assertGreaterEqual(len(specs), 5)
        for spec in specs:
            with self.subTest(spec=spec.name):
                self.assertEqual(d.check(spec), [])

    def test_rendered_files_are_current_and_clean(self):
        for spec in sorted((ROOT / "docs" / "diagrams").glob("*.json")):
            name = d.load(spec).get("name") or spec.stem
            for theme in ("dark", "light"):
                with self.subTest(spec=spec.name, theme=theme):
                    f = ROOT / "docs" / "images" / "diagrams" / f"{name}-{theme}.svg"
                    self.assertTrue(f.exists(), f"{f.name} missing — run dev diagram render docs/diagrams")
                    self.assertEqual(f.read_text(), d.render(d.load(spec), theme),
                                     f"{f.name} is stale — run dev diagram render docs/diagrams")
                    self.assertEqual(d.check_svg(f), [])

    def test_docs_width_budget(self):
        """README/docs diagrams stay legible in GitHub's ~830-1000 px column."""
        for spec in sorted((ROOT / "docs" / "diagrams").glob("*.json")):
            with self.subTest(spec=spec.name):
                _, m = d.build(d.load(spec))
                self.assertLessEqual(m.W, 1320)


if __name__ == "__main__":
    unittest.main()
