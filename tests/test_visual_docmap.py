# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the deck and document map of visual diagnosis, lib/devkit/visual/docmap.py (R56): one node per page or
slide named from its title line, the reading order and the PDF's own links as edges, the storyline as one journey,
every string through redact.cjs, and the document's captures attached to their pages.

Every test runs in a temporary handoff repository and state folder, with the config file pointed away from the
developer's (core.HANDOFF, core.STATE, core.CONFIG_FILE). node and gitleaks run for real: the map is saved through
map.py's redaction. PDFKit runs for real too, on PDFs written by hand, in a child Python process with a temporary
HOME: the test run's audit hook refuses osascript in this process (tests/_hermetic.py).
Tests carry comments, not docstrings: `dev test prove` leaves out tests with a docstring.
Run: python3 -m unittest tests.test_visual_docmap
"""
import json
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

from devkit import core, identity  # noqa: E402
from devkit.visual import docmap  # noqa: E402
from devkit.visual import map as vmap  # noqa: E402

NODE = shutil.which("node")
GITLEAKS = shutil.which("gitleaks")
OSASCRIPT = shutil.which("osascript")
P = "acme/billing"
KEY = "AKIA" + "QYLPMN5HHHFPZAM2"  # an AWS access key id, in two parts so no scanner reads this file as one
SHA = "ab" * 32

# docmap.build in a child process: the test's handoff, state, and config in argv[2], the project, the PDF, and the
# captures (JSON) after it; prints {"ok": build's result} or {"error": the refusal}
CHILD = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from devkit import core, identity
tmp = Path(sys.argv[2])
core.HANDOFF, core.STATE, core.CONFIG_FILE = tmp / "handoff", tmp / "state", tmp / "config.json"
identity.hostname = lambda: "mac-test"
from devkit.visual import docmap, map as vmap
try:
    print(json.dumps({"ok": docmap.build(sys.argv[3], sys.argv[4], json.loads(sys.argv[5]), by="trinity")}))
except vmap.MapError as e:
    print(json.dumps({"error": str(e)}))
"""


def pdf(pages, links=()) -> bytes:
    # A PDF written by hand, US Letter pages. `pages`: each page a list of (x, y, points, text) lines in Helvetica;
    # `links`: (page, line, target), the 1-based page, the index of the line the link covers, and the target: a page
    # number (a /Dest array), ("goto", n) (a GoTo action), or a URL (a URI action).
    objs, page_ids = [b"", b""], []  # 1: the catalog, 2: the page tree, both written last

    def add(body) -> int:
        objs.append(body)
        return len(objs)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    contents = []
    for lines in pages:
        stream = "".join(f"BT /F1 {pt} Tf {x} {y} Td ({text}) Tj ET\n" for x, y, pt, text in lines).encode()
        contents.append(add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream"))
        page_ids.append(add(b""))
    annots = {}
    for src, at, target in links:
        x, y, pt, _ = pages[src - 1][at]
        rect = f"/Rect [{x} {y - 2} {x + 300} {y + pt}] /Border [0 0 0]"
        if isinstance(target, int):
            how = f"/Dest [{page_ids[target - 1]} 0 R /Fit]"
        elif isinstance(target, tuple):
            how = f"/A << /S /GoTo /D [{page_ids[target[1] - 1]} 0 R /XYZ 0 792 0] >>"
        else:
            how = f"/A << /S /URI /URI ({target}) >>"
        annots.setdefault(src, []).append(add(f"<< /Type /Annot /Subtype /Link {rect} {how} >>".encode()))
    for i, (pid, content) in enumerate(zip(page_ids, contents)):
        refs = " ".join(f"{a} 0 R" for a in annots.get(i + 1, ()))
        objs[pid - 1] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font} 0 R "
                         f">> >> /Contents {content} 0 R" + (f" /Annots [{refs}]" if refs else "") + " >>").encode()
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(f'{i} 0 R' for i in page_ids)}] /Count {len(page_ids)} >>".encode()
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def line(text, pt=12, y=700):
    # a text line as docmap.read gives it: its box in PDF points from the bottom left, as tall as its type
    return {"text": text, "box": [72, y, 300, pt]}


def page(*lines, links=(), size=(612, 792)):
    # a page as docmap.read gives it; each link (target, text): a page number, a URL, or None (another kind of link)
    return {"size": list(size), "lines": list(lines),
            "links": [{"page": t if isinstance(t, int) else None, "url": t if isinstance(t, str) else None,
                       "text": text} for t, text in links]}


def doc(*pages, file="deck.pdf", sha=SHA):
    return {"file": file, "sha256": sha, "pages": list(pages)}


@unittest.skipUnless(NODE and GITLEAKS, "node and gitleaks run the map's redaction (brew install node gitleaks)")
class DocMapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="devkit-visual-docmap-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for p in (mock.patch.object(core, "HANDOFF", self.tmp / "handoff"),
                  mock.patch.object(core, "STATE", self.tmp / "state"),
                  mock.patch.object(core, "CONFIG_FILE", self.tmp / "config.json"),
                  mock.patch.object(identity, "hostname", return_value="mac-test")):
            p.start()
            self.addCleanup(p.stop)
        self.dir = core.HANDOFF / "visual" / "acme" / "billing"

    def nodes(self, m):
        return {n["id"]: n for n in m["nodes"]}

    def edges(self, m):
        return sorted((e["from"], e["to"], e["kind"]) for e in m["edges"] if not e.get("missing_since"))

    def capture(self, cid, file, page_no, node=None, sha=SHA):
        record = {"id": cid, "kind": "document", "state": None,
                  "source": {"file": file, "sha256": sha, "page": page_no},
                  "map": {"node": node, "state": None, "version": 1, "journey": None, "step": None,
                          "attached": "2026-11-18T10:00:00Z"} if node else None}
        path = self.dir / "captures" / f"{cid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2))
        return record

    def placed(self, cid):
        return (json.loads((self.dir / "captures" / f"{cid}.json").read_text()).get("map") or {}).get("node")

    def build_child(self, pdf_path, captures=()):
        home = self.tmp / "home"
        home.mkdir(exist_ok=True)
        run = subprocess.run([sys.executable, "-I", "-c", CHILD, str(ROOT / "lib"), str(self.tmp), P, str(pdf_path),
                              json.dumps(list(captures))], capture_output=True, text=True, timeout=180,
                             env=_sandbox.env(HOME=str(home)))
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout.strip().splitlines()[-1])

    # ------------------------------------------------------------------ pages, reading order, storyline

    def test_each_page_is_a_node_titled_by_its_title_line_or_its_number(self):
        long = "Revenue " * 30
        m = docmap.write(P, doc(page(line("Agenda and goals", 12, 600), line("Quarterly review", 32, 700)),
                                page(),
                                page(line("Second at the same size", 24, 500), line(long, 24, 700)),
                                page(line("  Wrap   up ", 24), size=(842, 595))), by="trinity")["map"]
        got = [(n["id"], n["page"], n["kind"], n["title"], n["final"], n["origin"]) for n in m["nodes"]]
        self.assertEqual(got, [("deck.p1", 1, "page", "Quarterly review", False, "document"),
                               ("deck.p2", 2, "page", "page 2", False, "document"),
                               ("deck.p3", 3, "page", long.strip()[:119] + "…", False, "document"),
                               ("deck.p4", 4, "slide", "Wrap up", True, "document")])
        self.assertEqual(m["start"], "deck.p1")
        self.assertEqual(json.loads((self.dir / "map" / "current.json").read_text())["version"], 1)

    def test_the_reading_order_joins_each_page_to_the_next_and_the_storyline_walks_it(self):
        m = docmap.write(P, doc(page(line("Intro")), page(line("Results")), page(line("Summary"))),
                         by="trinity")["map"]
        self.assertEqual(self.edges(m), [("deck.p1", "deck.p2", "next"), ("deck.p2", "deck.p3", "next")])
        [story] = m["journeys"]
        self.assertEqual((story["id"], story["start"], story["end"], story["origin"]),
                         ("J:deck-storyline", "deck.p1", "deck.p3", "document"))
        self.assertEqual([s["edge"] for s in story["steps"]], ["deck.p1>deck.p2", "deck.p2>deck.p3"])

    def test_a_one_page_document_has_no_reading_order_and_no_storyline(self):
        m = docmap.write(P, doc(page(line("Terms"))), by="trinity")["map"]
        self.assertEqual((len(m["nodes"]), m["edges"], m["journeys"]), (1, [], []))
        self.assertTrue(m["nodes"][0]["final"])

    # ------------------------------------------------------------------ the PDF's own links

    def test_the_documents_links_become_one_edge_per_target_page_or_site(self):
        out = docmap.write(P, doc(page(line("Contents"), links=[
            (2, "Next: the market"), (1, "Top of this page"), (4, "See the summary"), (4, "Summary again"),
            ("https://example.com/a", "Pricing"), ("https://example.com/b", "Terms"),
            ("mailto:sales@example.com", "Write to us"), ("file://localhost/Users/ana/notes.pdf", "Notes"),
            (None, "Print this page")]),
            page(line("Market")), page(line("Plan")), page(line("Summary"))), by="trinity")
        m = out["map"]
        self.assertEqual(self.edges(m), [("deck.p1", "deck.p2", "next"), ("deck.p1", "deck.p4", "nav"),
                                         ("deck.p1", "external.example-com", "external"),
                                         ("deck.p2", "deck.p3", "next"), ("deck.p3", "deck.p4", "next")])
        by_id = {e["id"]: e for e in m["edges"]}
        self.assertEqual(by_id["deck.p1>deck.p4"]["trigger"], {"role": "link", "name": "See the summary",
                                                                "selector": None})
        self.assertEqual(by_id["deck.p1>deck.p2"]["trigger"], {"role": None, "name": None, "selector": None})
        site = self.nodes(m)["external.example-com"]
        self.assertEqual((site["kind"], site["route"], site["title"], site["example"]),
                         ("external", "https://example.com", "example.com", "https://example.com/a"))
        self.assertEqual(out["summary"]["left_out"], 3)
        stored = (self.dir / "map" / "current.json").read_text()
        self.assertNotIn("sales@example.com", stored)
        self.assertNotIn("/Users/ana", stored)

    # ------------------------------------------------------------------ redaction (R48, R14, R15)

    def test_every_string_passes_redaction_and_no_id_keeps_a_token_of_the_file_name(self):
        m = docmap.write(P, doc(page(line(f"Access key {KEY}", 24),
                                     links=[("https://example.com/pricing?api_key=abc123", "Pricing")]),
                                page(line("Thanks")), file=f"board-{KEY}.pdf"), by="trinity")["map"]
        self.assertEqual(sorted(self.nodes(m)), ["board.p1", "board.p2", "external.example-com"])
        self.assertEqual(self.nodes(m)["board.p1"]["title"], "[redacted: aws-access-token]")
        self.assertEqual(self.nodes(m)["external.example-com"]["example"],
                         "https://example.com/pricing?api_key=[redacted]")
        for path in self.dir.rglob("*.json"):
            text = path.read_text()
            for secret in (KEY, KEY.lower(), "abc123"):
                self.assertNotIn(secret, text, f"{path.name} holds {secret[:6]}…")

    # ------------------------------------------------------------------ mapping again

    def test_mapping_again_updates_the_documents_items_keeps_declared_ones_and_marks_dropped_pages_missing(self):
        docmap.write(P, doc(page(line("Intro")), page(line("Pricing")), page(line("Plan")), page(line("Q&A"))),
                     by="trinity")
        vmap.add_node(P, "deck.p2", "Our pricing", page=2, by="sati", why="name the pricing slide")
        vmap.add_state(P, "deck.p3", "built", by="sati", why="the plan slide builds its chart")
        m = docmap.write(P, doc(page(line("Introduction")), page(line("Prices")), page(line("Plan"))),
                         by="trinity")["map"]
        nodes = self.nodes(m)
        self.assertEqual(m["version"], 4)
        self.assertEqual((nodes["deck.p1"]["title"], nodes["deck.p1"]["since"]), ("Introduction", 1))
        self.assertEqual((nodes["deck.p2"]["title"], nodes["deck.p2"]["origin"]), ("Our pricing", "declared"))
        self.assertEqual((nodes["deck.p3"]["final"], nodes["deck.p3"]["missing_since"]), (True, None))
        self.assertEqual(nodes["deck.p3"]["states"]["states"], ["built"])
        self.assertEqual(nodes["deck.p4"]["missing_since"], 4)
        self.assertEqual({e["id"]: e["missing_since"] for e in m["edges"]},
                         {"deck.p1>deck.p2": None, "deck.p2>deck.p3": None, "deck.p3>deck.p4": 4})
        self.assertEqual(m["journeys"][0]["end"], "deck.p3")

    # ------------------------------------------------------------------ captures

    def test_the_documents_captures_attach_to_its_own_pages_when_another_document_has_the_same_pages(self):
        docmap.write(P, doc(page(line("A one")), page(line("A two")), file="a.pdf", sha="cd" * 32), by="trinity")
        captures = [self.capture("VC-a1", "a.pdf", 1, node="a.p1", sha="cd" * 32), self.capture("VC-b1", "b.pdf", 1),
                    self.capture("VC-b2", "b.pdf", 2, node="a.p2")]
        m = docmap.write(P, doc(page(line("B one")), page(line("B two")), file="b.pdf"), captures,
                         by="trinity")["map"]
        self.assertEqual([self.placed(c) for c in ("VC-a1", "VC-b1", "VC-b2")], ["a.p1", "b.p1", "b.p2"])
        self.assertEqual(m["start"], "a.p1")

    # ------------------------------------------------------------------ PDFKit (a child process: see the docstring)

    @unittest.skipUnless(OSASCRIPT, "PDFKit runs through osascript, which only macOS has")
    def test_a_pdf_maps_its_pages_its_reading_order_and_its_own_links(self):
        deck = self.tmp / "deck.pdf"
        deck.write_bytes(pdf([[(72, 700, 32, "Quarterly review"), (72, 600, 12, "Agenda and goals")],
                              [(72, 700, 24, "See the summary on page 4"), (72, 600, 12, "Pricing at example.com")],
                              [(72, 700, 24, "Results"), (72, 650, 12, "Back to the start")],
                              [(72, 700, 24, "Summary")]],
                             links=[(2, 0, 4), (2, 1, "https://example.com/pricing"), (3, 1, ("goto", 1))]))
        out = self.build_child(deck)
        self.assertNotIn("error", out)
        m = vmap.load(P)
        self.assertEqual([(n["id"], n["page"], n["title"]) for n in m["nodes"] if n["kind"] == "page"],
                         [("deck.p1", 1, "Quarterly review"), ("deck.p2", 2, "See the summary on page 4"),
                          ("deck.p3", 3, "Results"), ("deck.p4", 4, "Summary")])
        self.assertEqual(self.edges(m), [("deck.p1", "deck.p2", "next"), ("deck.p2", "deck.p3", "next"),
                                         ("deck.p2", "deck.p4", "nav"),
                                         ("deck.p2", "external.example-com", "external"),
                                         ("deck.p3", "deck.p1", "nav"), ("deck.p3", "deck.p4", "next")])
        names = {e["id"]: e["trigger"]["name"] for e in m["edges"]}
        self.assertEqual((names["deck.p2>deck.p4"], names["deck.p3>deck.p1"]),
                         ("See the summary on page 4", "Back to the start"))

    @unittest.skipUnless(OSASCRIPT, "PDFKit runs through osascript, which only macOS has")
    def test_a_file_pdfkit_cannot_read_is_refused_plainly_and_nothing_is_written(self):
        broken = self.tmp / "broken.pdf"
        broken.write_bytes(b"%PDF-1.4\nthis is not a PDF\n")
        out = self.build_child(broken)
        self.assertEqual(out.get("error"), "PDFKit could not read broken.pdf, so its map was not written. Check the "
                                           "file opens in Preview.")
        self.assertFalse((self.dir / "map").exists())

    def test_without_pdfkit_the_map_is_refused_plainly_and_nothing_is_written(self):
        deck = self.tmp / "deck.pdf"
        deck.write_bytes(pdf([[(72, 700, 24, "Intro")]]))
        with mock.patch.object(docmap, "OSASCRIPT", str(self.tmp / "no-pdfkit")):
            with self.assertRaises(vmap.MapError) as cm:
                docmap.build(P, deck, [], by="trinity")
        self.assertEqual(str(cm.exception), "A document map reads the PDF's pages and links with PDFKit through "
                                            "osascript, which only macOS has. Nothing was written.")
        self.assertFalse((self.dir / "map").exists())


if __name__ == "__main__":
    unittest.main()
