# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The map of a deck or a document (R56): from a PDF and the captures of its pages, the map that map.py keeps for the
project, written as its next version through map.change, so every string passes redact.cjs first (R48, R71, R15, R14)
and the captures attach to their pages (R60).

- One node per page or slide (`kind` page, or slide for a landscape page, as capture.cjs renders it 1920 px wide),
  with its page number and its title line (the tallest line of text, the topmost of equals), or "page N" when it has
  no text. Its id is the document's slug and the page (deck.pdf: deck.p1, deck.p2, …); the last page is final.
- The reading order: a `next` edge from each page to the one after it.
- The PDF's own link annotations: a `nav` edge to the page a link goes to, or an `external` edge to the site of a web
  link (the external node the crawl gives that origin), named by the text the link covers. A link to its own page or
  where the reading order already goes adds nothing; a link of another kind (mailto:, a file, a viewer action) is left
  out and counted, so no address or path of this Mac reaches the map.
- The storyline: one journey of a reader through the pages in reading order (J:<slug>-storyline).

Mapping the document again replaces what an earlier map of it wrote, keeps what was declared or crawled with the same
ids, and marks the pages and links it no longer has as missing since the new version, as a crawl does (R49).

PDFKit reads the pages and their links through `osascript -l JavaScript`, as capture.cjs renders the pages, so a
document map needs macOS and no other dependency.

  build(project, pdf, captures) -> {"map", "attach", "summary"}   read() then write(): the product handler's call
  read(pdf) -> {"file", "sha256", "pages": [{"size": [w, h], "lines": [{"text", "box"}],
                                             "links": [{"page": n | None, "url": str | None, "text"}]}]}
  write(project, doc, captures) -> the same as build, from read()'s document
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from . import map as vmap

OSASCRIPT = "osascript"
READ_TIMEOUT = 120  # seconds PDFKit may take to read a document's text lines and links
ORIGIN = "document"  # the origin of the nodes, edges, and journeys a document map writes
TITLE_LIMIT = 120  # characters of a title line, as the description cuts names
NO_PDFKIT = ("A document map reads the PDF's pages and links with PDFKit through osascript, which only macOS has. "
             "Nothing was written.")
# Runs in osascript's JavaScript: each page's size in points, its text lines with their boxes (points, from the
# bottom left), and its link annotations, each with the 1-based page it goes to or its URL and the text it covers
_READ_JS = r"""
function run(argv) {
  try { ObjC.import('PDFKit'); } catch (e) { return JSON.stringify({error: 'no-pdfkit'}); }
  if (typeof $.PDFDocument === 'undefined') return JSON.stringify({error: 'no-pdfkit'});
  const doc = $.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(argv[0]));
  if (!doc || doc.isNil()) return JSON.stringify({error: 'unreadable'});
  const count = doc.pageCount, pages = [];
  const number = (dest) => {
    if (!dest || dest.isNil() || !dest.page || dest.page.isNil()) return null;
    const i = Number(doc.indexForPage(dest.page));
    return i >= 0 && i < count ? i + 1 : null;
  };
  const text = (sel) => (sel && !sel.isNil() ? ObjC.unwrap(sel.string) : null);
  for (let i = 0; i < count; i++) {
    const page = doc.pageAtIndex(i), bounds = page.boundsForBox(0), lines = [], links = [];
    const all = page.selectionForRect(bounds);
    if (all && !all.isNil()) {
      const list = all.selectionsByLine;
      for (let k = 0; k < list.count; k++) {
        const s = list.objectAtIndex(k), b = s.boundsForPage(page);
        lines.push({text: ObjC.unwrap(s.string), box: [b.origin.x, b.origin.y, b.size.width, b.size.height]});
      }
    }
    const annotations = page.annotations;
    for (let k = 0; k < annotations.count; k++) {
      const a = annotations.objectAtIndex(k);
      if (ObjC.unwrap(a.type) !== 'Link') continue;
      // PDFKit resolves a /Dest, a GoTo action, and a named destination into `destination`, a URI action into `URL`
      const url = a.URL && !a.URL.isNil() ? ObjC.unwrap(a.URL.absoluteString) : null;
      links.push({page: number(a.destination), url, text: text(page.selectionForRect(a.bounds))});
    }
    pages.push({size: [bounds.size.width, bounds.size.height], lines, links});
  }
  return JSON.stringify({pages});
}
"""


def read(pdf) -> dict:
    """PDFKit's reading of `pdf`: its base name, its SHA-256, and each page's size, text lines, and links (the module
    docstring has the shape). MapError, saying why, when PDFKit is not there or cannot read the file."""
    path = Path(pdf)
    try:
        data = path.read_bytes()
    except OSError:
        raise vmap.MapError(f"{path.name} cannot be read, so its map was not written.") from None
    try:
        run = subprocess.run([OSASCRIPT, "-l", "JavaScript", "-e", _READ_JS, str(path.resolve())],
                             capture_output=True, text=True, encoding="utf-8", timeout=READ_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise vmap.MapError(f"PDFKit did not read {path.name} within {READ_TIMEOUT} s, so its map was not written.") \
            from None
    except OSError:
        raise vmap.MapError(NO_PDFKIT) from None
    try:
        out = json.loads(run.stdout)
    except ValueError:
        out = None
    if isinstance(out, dict) and out.get("error") == "no-pdfkit":
        raise vmap.MapError(NO_PDFKIT)
    if run.returncode != 0 or not isinstance(out, dict) or not isinstance(out.get("pages"), list):
        raise vmap.MapError(f"PDFKit could not read {path.name}, so its map was not written. Check the file opens in "
                            "Preview.")
    if not out["pages"]:
        raise vmap.MapError(f"{path.name} has no pages, so there is nothing to map.")
    return {"file": path.name, "sha256": hashlib.sha256(data).hexdigest(), "pages": out["pages"]}


def build(project: str, pdf, captures=(), *, by=None, why=None) -> dict:
    """`dev visual product <file.pdf>`'s map: `pdf` read with PDFKit, then written as the project's next map version
    with `captures` (the records `store.capture` returned for its pages) attached to their pages. `by`: who maps it;
    `why`: the changelog's reason (default: what the map holds). -> {"map": the map as written, "attach": map.attach's
    result, "summary": the counts and `lines`, the terminal copy}"""
    return write(project, read(pdf), captures, by=by, why=why)


def _clean(text) -> str:
    return " ".join(str(text or "").split())


def _cut(text: str) -> str:
    return text if len(text) <= TITLE_LIMIT else text[:TITLE_LIMIT - 1] + "…"


def _title(lines: list, number: int) -> str:
    """The page's title line: its tallest line of text, the topmost of equals; "page N" when it has no text."""
    found = [(ln["box"][3], ln["box"][1] + ln["box"][3], _clean(ln.get("text"))) for ln in lines or ()]
    found = [f for f in found if f[2]]
    return _cut(max(found, key=lambda f: f[:2])[2]) if found else f"page {number}"


def _slug(file: str) -> str:
    """The document's part of its node and journey ids, from its file name: the words of its name in lowercase,
    joined by dashes, at most 40 characters. A word of 16 or more characters that mixes letters and digits is left
    out: it may be a token, and an id, unlike a title, cannot be redacted once other records point at it."""
    stem = re.sub(r"\.pdf$", "", str(file), flags=re.I)
    words = [w for w in re.findall(r"[a-z0-9]+", stem.lower())
             if not (len(w) >= 16 and re.search(r"[a-z]", w) and re.search(r"[0-9]", w))]
    return "-".join(words)[:40].strip("-") or "document"


def _pages(doc: dict, version: int) -> list:
    slug, pages = _slug(doc["file"]), doc["pages"]
    out = []
    for n, p in enumerate(pages, start=1):
        w, h = p["size"]
        out.append({"id": f"{slug}.p{n}", "title": _title(p.get("lines"), n), "kind": "slide" if w > h else "page",
                    "route": None, "page": n, "section": slug, "document": doc["file"], "origin": ORIGIN,
                    "since": version, "missing_since": None, "final": n == len(pages), "example": None})
    return out


def _edge(source: str, target: str, kind: str, version: int, name=None) -> dict:
    return {"id": f"{source}>{target}", "from": source, "to": target, "kind": kind,
            "trigger": {"role": "link" if kind != "next" else None, "name": name, "selector": None}, "when": None,
            "origin": ORIGIN, "since": version, "missing_since": None}


def _storyline(slug: str, nodes: list, edges: list, file: str, version: int):
    """The storyline: one journey of a reader through the pages in reading order; None for a single page."""
    if len(nodes) < 2:
        return None
    return {"id": f"J:{slug}-storyline", "actor": "reader", "task": f"Read {file} from the first page to the last",
            "start": nodes[0]["id"], "end": nodes[-1]["id"], "budget": None, "keeps": [], "expect": None,
            "origin": ORIGIN, "since": version,
            "steps": [{"edge": e["id"], "do": [], "expect": None, "decision": None} for e in edges]}


def _site(url: str, version: int):
    """The external node of a web link's site, with the id, route, and title the crawl gives an external origin;
    None for a link that is not http or https (mailto:, file:, another program)."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    host = f"[{host}]" if ":" in host else host
    if port and (parts.scheme, port) not in (("http", 80), ("https", 443)):
        host += f":{port}"
    origin = f"{parts.scheme}://{host}"
    return {"id": "external." + vmap._slug(host), "title": host, "kind": "external", "route": origin, "page": None,
            "section": "external", "origin": ORIGIN, "since": version, "missing_since": None, "final": False,
            "example": url}


def _links(pages: list, nodes: list, taken: set, version: int):
    """The PDF's own links as edges: one to each page or site a page links to (the first link's text names it),
    none to the page itself or where the reading order already goes. -> (edges, sites, links left out)"""
    edges, sites, left_out = [], {}, 0
    for p, node in zip(pages, nodes):
        for link in p.get("links") or ():
            name = _cut(_clean(link.get("text"))) or None
            target = link.get("page")
            site = _site(link["url"], version) if isinstance(link.get("url"), str) else None
            if isinstance(target, int) and 1 <= target <= len(nodes):
                to, kind, site = nodes[target - 1]["id"], "nav", None
            elif site:
                to, kind = site["id"], "external"
            else:
                left_out += 1
                continue
            eid = f"{node['id']}>{to}"
            if to == node["id"] or eid in taken:
                continue
            taken.add(eid)
            edges.append(_edge(node["id"], to, kind, version, name))
            if site:
                sites.setdefault(site["id"], site)
    return edges, list(sites.values()), left_out


def _put(items: list, record: dict) -> None:
    """Add `record`, or replace this document's item with its id from an earlier map, keeping the version it arrived
    in and the states declared on it. An item declared or crawled with that id keeps its own fields."""
    for i, old in enumerate(items):
        if old["id"] == record["id"]:
            if old.get("origin") == ORIGIN:
                record["since"] = old.get("since", record["since"])
                if old.get("states"):
                    record["states"] = old["states"]
                items[i] = record
            return
    items.append(record)


def _lines(m: dict, summary: dict, attached: int) -> list:
    """The terminal copy of a document map; the command prints the first line as it is and the others under it."""
    plural = vmap._plural
    lines = [f"✓ map v{m['version']} · {summary['file']} · {plural(summary['pages'], summary['noun'])} · "
             f"{plural(summary['next'], 'step')} in reading order · {plural(summary['links'], 'link')} between "
             f"{summary['noun']}s · {plural(summary['external'], 'link')} to other sites"]
    if summary["journey"]:
        lines.append(f"storyline {summary['journey']}: the {summary['pages']} {summary['noun']}s in reading order")
    if summary["left_out"]:
        lines.append(f"– {plural(summary['left_out'], 'link')} of another kind (mail, a file, a viewer action) left "
                     "out of the map")
    if attached:
        lines.append(f"attached {plural(attached, 'capture')} to their {summary['noun']}s")
    return lines


def write(project: str, doc: dict, captures=(), *, by=None, why=None) -> dict:
    """The document map of `doc` (read()'s shape) written as the map's next version, and `captures` attached to this
    document's pages. -> build()'s result"""
    summary = {"file": doc["file"]}
    slug = _slug(doc["file"])
    mine = re.compile(rf"{re.escape(slug)}\.p[0-9]+")

    def fn(m, version):
        pages = _pages(doc, version)
        order = [_edge(a["id"], b["id"], "next", version) for a, b in zip(pages, pages[1:])]
        links, sites, summary["left_out"] = _links(doc["pages"], pages, {e["id"] for e in order}, version)
        for rec in pages + sites:
            _put(m["nodes"], rec)
        for rec in order + links:
            _put(m["edges"], rec)
        story = _storyline(slug, pages, order, doc["file"], version)
        if story:
            _put(m["journeys"], story)
        # what this document had in an earlier map and no longer has stays, marked missing, as a crawl marks it
        ids, eids = {n["id"] for n in pages}, {e["id"] for e in order + links}
        for item, key, seen in [(n, "id", ids) for n in m["nodes"]] + [(e, "from", eids) for e in m["edges"]]:
            if (item.get("origin") == ORIGIN and mine.fullmatch(item[key]) and item["id"] not in seen and
                    not item.get("missing_since")):
                item["missing_since"] = version
        if m["start"] is None:
            m["start"] = pages[0]["id"]
        summary.update(pages=len(pages), next=len(order), links=sum(e["kind"] == "nav" for e in links),
                       external=sum(e["kind"] == "external" for e in links), journey=story and story["id"],
                       noun="slide" if all(n["kind"] == "slide" for n in pages) else "page")
        return (f"map of {doc['file']}: {vmap._plural(len(pages), summary['noun'])}, "
                f"{vmap._plural(summary['links'], 'link')} between them, "
                f"{vmap._plural(summary['external'], 'link')} to other sites")

    out = vmap.change(project, fn, why, by)
    # map.py attaches a capture to the one node its page matches; with two documents in the map a page matches two,
    # or the other document's alone when it was captured before this map, so this document's captures go to its own
    done = {a["capture"]: a["node"] for a in out["attach"]["attached"]}
    choose = {}
    for c in captures or ():
        source = c.get("source") or {}
        number = source.get("page")
        if source.get("sha256") != doc.get("sha256") or not isinstance(number, int):
            continue  # a capture of another file, or not of a page
        want = f"{slug}.p{number}"
        if done.get(c["id"], (c.get("map") or {}).get("node")) != want:
            choose[c["id"]] = want
    if choose:
        again = vmap.attach(project, choose=choose, by=by, current=out["map"])
        done.update({a["capture"]: a["node"] for a in again["attached"]})
        out["attach"] = {**out["attach"], "attached": [{"capture": c, "node": n} for c, n in done.items()],
                         "findings": out["attach"]["findings"] + again["findings"]}
    summary["lines"] = _lines(out["map"], summary, len(out["attach"]["attached"]))
    return {**out, "summary": summary}
