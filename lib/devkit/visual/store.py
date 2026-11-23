# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The records and the local store of visual diagnosis: audits (VA-…), captures (VC-…), and findings (VF-…).

Where things are (R10):

  ~/Development/.handoff/visual/<owner>/<repo>/     the text records, which sync with the handoff repository
    audits/VA-….json  captures/VC-….json  findings/VF-….json  media/<sha256>.webp (only after `share`)
  ~/.local/state/devkit/visual/<owner>/<repo>/      the local store, which never leaves this Mac
    blobs/<sha256>.png  desc/<sha256>.json.gz  sessions/<session>.json  reports/VA-…/  map/diagrams/

Every folder this module creates in the local store has mode 0700 and every file 0600, since pictures can show
personal data. Records are one JSON file each, written whole in one step under the record's lock, so two Macs
rarely touch the same file; a finding's history only grows.

Ids follow the work-item scheme: `VA-`, `VC-`, or `VF-`, the seconds since 2026 in base 36, and six random base-36
characters, unique across machines (R1). `by` in a record is the crew member's id when one of its lanes runs the
command, else the configured GitHub user (then $USER); a record's `by` object adds the user, the Mac, and the session.

A capture runs lib/devkit/visual/capture.cjs with TMPDIR set to a new 0700 folder, deleted when it ends, also after
a failure (R69), in a session of its own: a capture that does not end in CAPTURE_TIMEOUT, or fails, is stopped with
its process group and the browser's (Playwright starts the browser detached, with its profile in that folder). The
driver redacts everything before it writes the picture and the description into the store; this module then writes
the capture record with the audit, the map node it attaches to (R50), the commit, and who took it. When the project
has a DESIGN.md whose front matter holds spacing tokens, the capture gets that scale for off-scale-spacing
(`spacing`, px); otherwise it gets none. A picture file given as the source (PNG, JPEG, WebP) is an external capture
with no recipe (R9).

Records sync from teammates, so what they hold is data. Every record is read with its control and bidirectional
characters stripped (R71), and a replay (`capture --again`, `verify`) reads the recipe's storage state and a
document's file only from inside the project: a relative path is the project's, and a document's base name (the
record keeps no path of this Mac) is looked up among the project's files.

Each audit has one scope (R57), and each scope counts its passes (R58): SCOPE_PASSES. A pass is recorded once per
capture (screen passes) or per audit (structure passes), as findings, `clean` with what was checked, or `n/a` with
the reason (R19); a capture's pass can also be covered by a finding on its recipe at another width or theme, which
counts as findings. Every text a person or a model writes into a record (notes, finding text, reasons) passes the
redaction of map strings first (R71, R15, R14, through redact.cjs). `share` copies the pictures that findings use
into the handoff repository as WebP after checking each one against its record (R12).

`verify` is the only way to `verified` (R25). A screen finding's recipe is captured again on the working tree (never
as an experiment, R37), its anchor found again (by locator, then its own test id or id, then its selector in one
spelling, then its role and name, and only then by its old position, which the outcome then says), the check that
raised it read on the new capture, and the pictures compared with pixelmatch (compare.py, R34, R35): the finding
keeps the result in `verification` (with the difference picture's hash, size, and scale, and how the anchor was
found), and the changed regions outside the anchor become `changed-region` signals numbered on the new capture. A
structure finding is decided by `dev visual map check` on the current map and walks; a journey walked again has
each step's new capture compared with the one the earlier walk took (`verification.steps`). The findings it is the
root of are checked on the same evidence (R23). A judgment finding waits for a reviewer, who accepts it with
`--accept --why`.

Errors are VisualError (a ValueError): the message says what happened and what to do, and nothing was written.
"""
from __future__ import annotations

import difflib
import gzip
import json
import hashlib
import os
import re
import secrets
import shutil
import struct
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from .. import core, identity, work
from . import compare as vcompare
from . import map as vmap
from . import noderun

HERE = Path(__file__).resolve().parent
CAPTURE = HERE / "capture.cjs"
CAPTURE_TIMEOUT = 150  # seconds for one capture: capture.cjs's own bounds, node's start, and the browser's
MAX_PAGE_HEIGHT = 16384  # CSS px: capture.cjs cuts a taller page's picture and description there (R74)
SCOPES = ("screen", "component", "journey", "section", "product")
MODES = ("operate", "read", "persuade", "experience")
THEMES = ("light", "dark")
SEVERITIES = ("P0", "P1", "P2", "P3")
OPEN = ("open", "reopened", "fixing", "fixed")
CLOSED = ("wontfix", "duplicate", "not-a-problem")
SCREEN_PASSES = ("task", "hierarchy", "layout", "spacing", "typography", "color", "content", "states", "responsive",
                 "accessibility")
STRUCTURE_PASSES = ("S1", "S2", "S3", "S4", "S5", "S6")
# the passes each scope's coverage counts (R58): (screen passes on each capture, structure passes on the audit)
SCOPE_PASSES = {"screen": (SCREEN_PASSES, ()), "component": (SCREEN_PASSES[1:], ("S5",)),
                "journey": (SCREEN_PASSES, ("S1", "S3", "S4", "S6")), "section": (SCREEN_PASSES, STRUCTURE_PASSES),
                "product": (SCREEN_PASSES, STRUCTURE_PASSES)}
DEFAULT_VIEWPORT = [1440, 900]
KINDS = {"VA": ("audits", "audit"), "VC": ("captures", "capture"), "VF": ("findings", "finding")}
ID = re.compile(r"V[ACF]-[0-9a-z]{4,40}")
SIGNAL_REF = re.compile(r"(V[AC]-[0-9a-z]{4,40})#([1-9][0-9]{0,4})")
ELEMENT_ID = re.compile(r"E[1-9][0-9]{0,5}")
GUIDE = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}")
DECISION = re.compile(r"D[1-9][0-9]{0,5}")
COMMIT = re.compile(r"[0-9a-f]{4,40}")
VIEWPORT = re.compile(r"([1-9][0-9]{1,3})x([1-9][0-9]{1,3})")
VIEWPORT_MIN, VIEWPORT_MAX = 100, 8192  # CSS px of each side, as capture.cjs checks a recipe
EPOCH = 1767225600  # 2026-01-01T00:00:00Z, the work-item scheme's (work.new_id)
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
PICTURES = (".png", ".jpg", ".jpeg", ".webp")
SHARE_MAX = 400 * 1024         # bytes of one shared WebP (R12)
AUDIT_SHARE_MAX = 8 * 1024 * 1024  # bytes of the WebPs one audit shares
NO_AUDIT = ("No audit is open in this session. For one screen: dev visual screen <url>. For more: dev visual start "
            "--scope <scope> \"<question>\"")
# Rule references (R21): devkit checks wcag, nielsen, check, and map against this catalog; the others name rules the
# project or the platform owns.
SCHEMES = ("wcag", "nielsen", "check", "map", "impeccable", "ds", "platform")
_WCAG = {"1.1": 1, "1.2": 9, "1.3": 6, "1.4": 13, "2.1": 4, "2.2": 6, "2.3": 3, "2.4": 13, "2.5": 8, "3.1": 6,
         "3.2": 6, "3.3": 9}
WCAG = frozenset([f"{g}.{n}" for g, k in _WCAG.items() for n in range(1, k + 1)] + ["4.1.2", "4.1.3"])  # WCAG 2.2
CHECKS = frozenset(("overlap", "page-overflow", "element-overflow", "truncation", "contrast", "contrast-unknown",
                    "tap-target", "non-semantic-control", "min-text-size", "layout-shift", "broken-image", "offscreen",
                    "console", "impeccable", "axe", "focus-visible", "near-miss-alignment", "off-scale-spacing",
                    "safe-margin", "words-per-slide"))
MAP_RULES = frozenset(r.split(":", 1)[1] for _, rules in vmap._CHECKS.values() for r in rules if r.startswith("map:"))
_CONTROLS = vmap.CONTROLS  # control and bidirectional characters (R71)
_PDF_PAGES = ("ObjC.import('PDFKit'); function run(a) { const d = $.PDFDocument.alloc.initWithURL("
              "$.NSURL.fileURLWithPath(a[0])); return d && !d.isNil() ? String(d.pageCount) : 'unreadable'; }")


class VisualError(ValueError):
    """devkit refuses or cannot finish a `dev visual` command; the message says why and what to do. Nothing was
    written."""


# --------------------------------------------------------------------------- where things are

def records_dir(project: str) -> Path:
    """visual/<owner>/<repo>/ in the handoff repository."""
    try:
        return vmap.root(project)
    except vmap.MapError as e:
        raise VisualError(str(e)) from None


def store_dir(project: str) -> Path:
    """The project's local store: ~/.local/state/devkit/visual/<owner>/<repo>."""
    return core.STATE / "visual" / records_dir(project).relative_to(core.HANDOFF / "visual")


def _private_dir(path: Path) -> Path:
    """`path` as a folder, each folder this creates with mode 0700 (R10)."""
    path = Path(path)
    for folder in reversed([path, *path.parents]):
        if not folder.exists():
            folder.mkdir(mode=0o700, exist_ok=True)  # another devkit process can make it meanwhile
            os.chmod(folder, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions (0700 is owner-only, R10)
    return path


def _private_write(path: Path, data: bytes) -> None:
    """Write a file of the local store in one step, with mode 0600 (R10)."""
    _private_dir(path.parent)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------- ids, people, time

def _b36(n: int) -> str:
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = _B36[r] + out
        if not n:
            return out


def new_id(kind: str, project: str = None) -> str:
    """`VA-`, `VC-`, or `VF-` with the work-item scheme's body: a time part of 6 base36 characters (work.TIME_BASE
    plus the seconds since EPOCH, so ids sort by time and after every id of the old 5-character scheme, which stay
    readable), then 6 random ones; with `project`, one no record of it holds yet."""
    if kind not in KINDS:
        raise VisualError(f"{kind} is not a kind of visual record: VA (audit), VC (capture), or VF (finding).")
    while True:
        when = _b36(work.TIME_BASE + max(0, int(time.time()) - EPOCH))
        rid = f"{kind}-{when}{''.join(secrets.choice(_B36) for _ in range(6))}"
        if not project or not _path(project, rid).exists():
            return rid


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _member():
    """The id of the crew member whose lane runs this command (its lane variable, else its open call in the
    session's ledger), or None for the tech lead and the developer."""
    try:
        from .. import crew, progress
        aid = progress._as_lane(os.environ.get(identity.LANE_VAR) or None)
        if not aid:
            return None
        _, lane = progress.lane_of(*progress.current_session(), aid)
        m = crew._for_lane(lane) if lane else None
        return m["id"] if m else None
    except Exception:  # who acts never stops the work: the record then names the user
        return None


def _session() -> str:
    """The agent session running this command, as `<agent>-<session id>` (a shell's `human-shell-<pid>`)."""
    try:
        from .. import progress
        agent, sid = progress.current_session()
    except Exception:
        agent, sid = "human", f"shell-{os.getppid()}"
    return re.sub(r"[^A-Za-z0-9._-]", "_", f"{agent}-{sid}")[:120]


def _lane(hint: bool = True):
    """The lane running this command (the crew member's agent id), or None for the tech lead and the developer. The
    crew members of one agent session share its session id, so the lane tells their open audits apart. VisualError
    when devkit cannot tell which lane runs it: guessing would take another lane's audit. The refusal names --audit
    when `hint`, for the commands that take it."""
    from .. import progress
    try:
        lane = progress._as_lane(os.environ.get(identity.LANE_VAR) or None)
    except identity.IdentityError as e:
        raise VisualError(f"{e}." + (" Or name the audit: --audit VA-…." if hint else "")) from None
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(lane))[:80] if lane else None


def _user():
    """The developer's GitHub login (configured, else gh's), or None. Never the Mac's own login ($USER): records sync
    to the handoff repository."""
    return core.github_user() or None


def who() -> str:
    """Who acts, for history entries: the crew member's id, else the developer's GitHub login, else "unknown"."""
    return _member() or _user() or "unknown"


def actor() -> dict:
    """A record's `by`: the crew member (or None), the user, this Mac by its machine id (or None), and the session."""
    return {"member": _member(), "user": _user(), "host": vmap.this_mac(), "session": _session()}


def _where():
    """Where a capture's picture stays: `local:<machine id>` (the local store of that Mac), or None when this Mac has
    no machine id."""
    mac = vmap.this_mac()
    return f"local:{mac}" if mac else None


# --------------------------------------------------------------------------- records

def _path(project: str, rid: str) -> Path:
    if not isinstance(rid, str) or not ID.fullmatch(rid):
        raise VisualError(f"{rid} is not a visual record id (VA-…, VC-…, or VF-…).")
    return records_dir(project) / KINDS[rid[:2]][0] / f"{rid}.json"


clean_record = vmap.clean_record  # a record as read, without control characters (R71)


def load(project: str, rid: str) -> dict:
    """The record `rid` (an audit, a capture, or a finding), its text stripped of control characters."""
    path = _path(project, rid)
    try:
        doc = clean_record(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        raise VisualError(f"No {KINDS[rid[:2]][1]} {rid} in {project}.") from None
    except ValueError:
        raise VisualError(f"{core.tilde(path)} is not a record devkit can read. Restore it from the handoff "
                          f"repository's history: git log -- {path.name}") from None
    if not isinstance(doc, dict) or doc.get("id") != rid:
        raise VisualError(f"{core.tilde(path)} does not hold the record {rid}.")
    return doc


def save(project: str, doc: dict) -> Path:
    """Write a record whole, in one step."""
    path = _path(project, doc.get("id"))
    path.parent.mkdir(parents=True, exist_ok=True)
    core.replace_text(path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    return path


def _update(project: str, rid: str, fn) -> dict:
    """Read the record, let fn(doc) change it, and write it, holding its lock. -> the record as written."""
    path = _path(project, rid)
    with core.file_lock(path) as held:
        if not held:
            raise VisualError(f"Another devkit command is changing {rid}. Run this one again when it ends.")
        doc = load(project, rid)
        fn(doc)
        save(project, doc)
    return doc


def _all(project: str, kind: str) -> list:
    out = []
    for path in sorted((records_dir(project) / KINDS[kind][0]).glob(f"{kind}-*.json")):
        try:
            doc = clean_record(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and doc.get("id") == path.stem:
            out.append(doc)
    return out


def _clean(texts: list) -> list:
    """Text a person or a model writes into a record, through redact.cjs (R71, R15, R14): a text with a secret
    becomes `[redacted: <rule>]`."""
    if not texts:
        return []
    try:
        return vmap.redact_strings([str(t) for t in texts])["strings"]
    except vmap.MapError as e:
        raise VisualError(str(e).replace("the map's text", "the text")) from None


def _text(value, option: str, required: bool = True):
    text = " ".join(str(value or "").split())
    if required and not text:
        raise VisualError(f'Give {option} "…".')
    return text or None


# --------------------------------------------------------------------------- the session's audit

def _session_file(project: str, hint: bool = True) -> Path:
    """Where the session keeps its open audit: one file for the tech lead and the developer, and one for each crew
    lane, so a member's capture or bulk dismissal without --audit never lands in another lane's audit."""
    lane = _lane(hint)
    return store_dir(project) / "sessions" / (f"{_session()}.lane-{lane}.json" if lane else f"{_session()}.json")


def open_audit(project: str):
    """The audit open in this session's lane for the project, or None."""
    try:
        aid = json.loads(_session_file(project).read_text()).get("audit")
    except (OSError, ValueError, AttributeError):
        return None
    return aid if isinstance(aid, str) and ID.fullmatch(aid) and _path(project, aid).is_file() else None


def _open(project: str, aid: str, session: Path = None) -> None:
    """Open `aid` in this session's lane: in `session`, its session file found before (start), else found now."""
    _private_write(session or _session_file(project), json.dumps({"audit": aid, "at": _now()}).encode())


# --------------------------------------------------------------------------- audits (R18, R57)

def _viewports(values) -> list:
    out = []
    for v in values or ():
        m = VIEWPORT.fullmatch(str(v).strip().lower())
        if not m:
            raise VisualError(f"{v} is not a viewport: give it as <width>x<height> in CSS px, such as 1440x900.")
        size = [int(m.group(1)), int(m.group(2))]
        if not all(VIEWPORT_MIN <= n <= VIEWPORT_MAX for n in size):  # capture.cjs takes no other size
            raise VisualError(f"{v} is not a viewport: each side is from {VIEWPORT_MIN} to {VIEWPORT_MAX} CSS px.")
        out.append(size)
    return out


def _themes(values) -> list:
    out = [str(t) for t in values or ()]
    bad = [t for t in out if t not in THEMES]
    if bad:
        raise VisualError(f"{bad[0]} is not a theme: light or dark.")
    return out


def _git(cwd) -> dict:
    cwd = str(cwd or os.getcwd())
    if not core.gitout(cwd, "rev-parse", "--show-toplevel"):
        return {"commit": None, "dirty": None, "branch": None}
    branch = core.gitout(cwd, "rev-parse", "--abbrev-ref", "HEAD") or None
    return {"commit": core.gitout(cwd, "rev-parse", "--short", "HEAD") or None,
            "dirty": bool(core.gitout(cwd, "status", "--porcelain")), "branch": None if branch == "HEAD" else branch}


def start(project: str, scope: str, question: str, *, task=None, mode=None, viewports=(), themes=(), states=(),
          include=(), journeys=(), within=None, element=None, cwd=None) -> dict:
    """Open an audit of one scope and make it this session's (R18, R57). -> the audit record."""
    if scope not in SCOPES:
        raise VisualError(f"{scope} is not a scope. Scopes: {', '.join(SCOPES)}.")
    question = _text(question, "the question the audit answers, as its first argument,")
    if mode is not None and mode not in MODES:
        raise VisualError(f"{mode} is not a surface mode: {', '.join(MODES)}.")
    sizes, looks = _viewports(viewports), _themes(themes)
    for aid in include or ():
        if not isinstance(aid, str) or not aid.startswith("VA-"):
            raise VisualError(f"--include takes audits (VA-…), not {aid}.")
        load(project, aid)
    for jid in journeys or ():
        if not vmap.JOURNEY_ID.fullmatch(str(jid)):
            raise VisualError(f"{jid} is not a journey id such as J:export-month.")
    task_text, scope_text = _clean([question, task or ""]), None
    if within is not None:
        scope_text = _clean([within])[0]
    version = vmap.load(project)["version"]
    session = _session_file(project, hint=False)  # before the record: a lane devkit cannot tell leaves no audit
    audit = {"id": new_id("VA", project), "scope": scope, "question": task_text[0], "task": task_text[1] or None,
             "mode": mode, "matrix": {"viewports": sizes or [list(DEFAULT_VIEWPORT)], "themes": looks or ["light"],
                                      "states": [s for s in _clean([str(s) for s in states or ()])]},
             "journeys": list(journeys or ()), "map_version": version or None, "within": scope_text,
             "element": element, "include": list(include or ()), "commit": _git(cwd)["commit"], "taken": _now(),
             "by": actor(), "passes": {}, "signals": []}
    save(project, audit)
    _open(project, audit["id"], session)
    return audit


def captures_of(project: str, audit_id: str) -> list:
    """The captures of an audit and of the audits it includes (R62), oldest first."""
    a = load(project, audit_id)
    ids = {a["id"], *(i for i in a.get("include") or () if isinstance(i, str))}
    return sorted((c for c in _all(project, "VC") if c.get("audit") in ids), key=lambda c: (c.get("taken") or "",
                                                                                           c["id"]))


def audits(project: str) -> list:
    """`dev visual ls`: each audit, newest first, with its open findings by severity, its untriaged signals, and the
    passes its scope counts (R58) recorded so far against those needed: the screen passes on each capture and the
    structure passes once."""
    findings = _all(project, "VF")
    captures = _all(project, "VC")
    out = []
    for a in sorted(_all(project, "VA"), key=lambda a: (a.get("taken") or "", a["id"]), reverse=True):
        ids = {a["id"], *(i for i in a.get("include") or () if isinstance(i, str))}  # as the report counts them
        mine = [f for f in findings if f.get("audit") in ids]
        open_ = {sev: sum(1 for f in mine if f.get("status") in OPEN and f.get("severity") == sev)
                 for sev in SEVERITIES}
        caps = [c for c in captures if c.get("audit") in ids]
        sigs = list(a.get("signals") or []) + [s for c in caps for s in (c.get("checks") or {}).get("signals") or []]
        screen, structure = SCOPE_PASSES.get(a.get("scope"), ((), ()))
        recorded = sum(1 for c in caps for p in screen if p in (c.get("passes") or {})) + \
            sum(1 for p in structure if p in (a.get("passes") or {}))
        out.append({"audit": a, "open": open_, "findings": len(mine), "captures": len(caps),
                    "untriaged": sum(1 for s in sigs if isinstance(s, dict) and not s.get("triage")),
                    "passes": {"recorded": recorded, "needed": len(screen) * len(caps) + len(structure)}})
    return out


# --------------------------------------------------------------------------- captures (R1–R9, R50, R69)

def _remote_refusal(host: str) -> VisualError:
    return VisualError(f"{host} is not on this Mac. Capture it with --remote, and only with demo or fixture data.")


def _source(source: str, remote: bool) -> dict:
    """What a capture's source is: {"url"} (web), {"file", "kind": document|image}."""
    text = str(source or "").strip()
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", text):
        parts = urlsplit(text)
        if parts.scheme not in ("http", "https", "file"):
            raise VisualError(f"{parts.scheme}: is not a source devkit captures: give an http, https, or file URL, "
                              "or a PDF or picture file.")
        if parts.scheme != "file" and (parts.hostname or "") not in LOCAL_HOSTS and not remote:
            raise _remote_refusal(parts.hostname or text)
        return {"url": text}
    path = Path(text).expanduser()
    if not path.is_file():
        raise VisualError(f"{text} is neither a URL nor a file here. Give an http, https, or file URL, or a PDF or "
                          "picture file.")
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return {"file": path.resolve(), "kind": "document"}
    if suffix in PICTURES:
        return {"file": path.resolve(), "kind": "image"}
    raise VisualError(f"{path.name} is not a PDF or a picture (PNG, JPEG, WebP). Export a PDF from the app that made "
                      "it, then capture the PDF.")


def _pages(spec, count: int) -> list:
    if spec in (None, ""):
        return list(range(1, count + 1))
    out = []
    for part in str(spec).split(","):
        m = re.fullmatch(r"\s*([1-9][0-9]*)\s*(?:-\s*([1-9][0-9]*)\s*)?", part)
        if not m:
            raise VisualError(f"{spec} is not a page list: give pages such as 3, 1-12, or 1,3,5-7.")
        first, last = int(m.group(1)), int(m.group(2) or m.group(1))
        out += range(first, last + 1)
    beyond = [p for p in out if p > count]
    if beyond:
        raise VisualError(f"The PDF has {count} page{'s' if count != 1 else ''}, so there is no page {beyond[0]}.")
    return sorted(set(out))


def _page_count(path: Path) -> int:
    try:
        run = subprocess.run(["osascript", "-l", "JavaScript", "-e", _PDF_PAGES, str(path)], capture_output=True,
                             text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        run = None
    text = (run.stdout.strip() if run else "")
    if not text.isdigit():
        raise VisualError(f"PDFKit could not read {path.name}, so its pages cannot be counted. Give the pages with "
                          "--pages, or check the file opens in Preview.")
    return int(text)


_LENGTH = re.compile(r"(\d+(?:\.\d+)?)\s*(px|rem|em)?")


def _length(value: str):
    """A spacing token's value in px: 8, 8px, "1rem" (16 px a rem), or None for anything else (a reference, two
    values, another unit)."""
    text = value.split(" #", 1)[0].strip().strip("'\"").strip()
    m = _LENGTH.fullmatch(text)
    if not m:
        return None
    px = float(m.group(1)) * (16 if m.group(2) in ("rem", "em") else 1)
    return (int(px) if px == int(px) else round(px, 2)) if px > 0 else None


def design_spacing(root: Path):
    """The spacing scale of the project's DESIGN.md: the `spacing:` tokens of its YAML front matter (the DESIGN.md
    format impeccable writes), in px, sorted; None without the file, its front matter, or spacing tokens. The
    off-scale-spacing check reads it; without it, the check uses the five values the page uses most."""
    try:
        lines = (Path(root) / "DESIGN.md").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    if not lines or lines[0].strip() != "---":
        return None
    end = next((i for i in range(1, len(lines)) if lines[i].strip() in ("---", "...")), None)
    if end is None:
        return None
    values, inside = set(), False
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            inside = line.split(" #", 1)[0].strip() == "spacing:"
            continue
        key, sep, value = line.strip().partition(":")
        px = _length(value) if inside and sep and value.strip() else None
        if px is not None:
            values.add(px)
    return sorted(values) or None


def _impeccable():
    """The detector capture.cjs runs as its last check, or None when devkit's impeccable engine is not installed
    (the capture then opens no DevTools port, R29)."""
    try:
        from .. import impeccable, setup
        impeccable.verified_engine()
        rec = (core.read_json(setup.engines_record(), {}) or {}).get("impeccable") or {}
    except Exception:  # an engine that cannot be verified is an engine that does not run
        return None
    return {"command": [str(core.DEVKIT_HOME / "bin" / "dev"), "impeccable"], "version": rec.get("version")}


def _strip(text: str) -> str:
    return _CONTROLS.sub("", text)


def _run_capture(request: dict, cwd) -> dict:
    """One run of capture.cjs with TMPDIR set to a new 0700 folder, deleted when it ends (R69). -> the record it
    printed. The driver writes nothing when it fails. It runs in a session of its own, so a capture that does not end
    in CAPTURE_TIMEOUT, or that fails, stops with every process it started, the browser included."""
    node = shutil.which("node")
    if not node:
        raise VisualError("node is not installed, so devkit cannot drive the browser. Nothing was recorded. Install "
                          "it: dev install --only tools.")
    folder = tempfile.mkdtemp(prefix="devkit-visual-")
    try:
        os.chmod(folder, 0o700)
        env = identity.detached_env({**os.environ, "TMPDIR": folder})  # it acts for no call
        proc = subprocess.Popen([node, str(CAPTURE), "--json"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env, cwd=str(cwd), start_new_session=True)
        try:
            stdout, stderr = proc.communicate(json.dumps(request), timeout=CAPTURE_TIMEOUT)
        except subprocess.TimeoutExpired:
            noderun.stop(proc, folder)
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            raise VisualError(f"The capture did not end in {CAPTURE_TIMEOUT} s and was stopped, with its browser. "
                              "Nothing was recorded.") from None
        except BaseException:
            noderun.stop(proc, folder)
            raise
        if proc.returncode != 0:
            noderun.stop(proc, folder)
        run = subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if run.returncode != 0:
        lines = [_strip(ln).strip() for ln in run.stderr.splitlines() if ln.strip()]
        why = (lines[-1] if lines else "the capture driver stopped without saying why").lstrip("✗ ").strip()
        raise VisualError(why if "Nothing was recorded" in why else f"{why} Nothing was recorded.")
    try:
        rec = json.loads(run.stdout)
    except ValueError:
        raise VisualError("The capture driver printed no capture record. Nothing was recorded.") from None
    if not isinstance(rec, dict) or not isinstance(rec.get("image"), dict):
        raise VisualError("The capture driver printed no capture record. Nothing was recorded.")
    return rec


def _png_size(data: bytes):
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def _picture(path: Path) -> bytes:
    """A picture file as PNG bytes: a PNG as it is, a JPEG or WebP converted by macOS's sips."""
    if path.suffix.lower() == ".png":
        data = path.read_bytes()
        if not _png_size(data):
            raise VisualError(f"{path.name} is not a PNG file.")
        return data
    folder = tempfile.mkdtemp(prefix="devkit-visual-")
    try:
        out = Path(folder) / "picture.png"
        try:
            run = subprocess.run(["sips", "-s", "format", "png", str(path), "--out", str(out)], capture_output=True,
                                 text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            run = None
        if not run or run.returncode != 0 or not out.is_file():
            raise VisualError(f"{path.name} could not be read as a picture (sips, part of macOS, converts JPEG and "
                              "WebP). Save it as PNG, then capture the PNG.")
        return out.read_bytes()
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _placed(m: dict, rec: dict, node, state, journey, step) -> dict:
    """The capture's `map` (R50): the node given, else the one node its route, or its document and page, matches;
    None with no map and no journey."""
    if not m["version"]:
        return None if journey is None else {"node": None, "state": None, "version": None, "journey": journey,
                                             "step": step, "attached": None}
    source = rec.get("source") or {}
    if node:
        try:
            nodes = [vmap._node_of(m, node)["id"]]
        except vmap.MapError as e:
            raise VisualError(str(e)) from None
    else:
        nodes = vmap.match_nodes(m, route=source.get("route"), page=source.get("page"),
                                 document=source.get("file") if rec.get("kind") == "document" else None)
    nid = nodes[0] if len(nodes) == 1 else None
    machine = (vmap._node_of(m, nid).get("states") or {}) if nid else {}
    return {"node": nid, "state": state if nid and state in (machine.get("states") or ()) else None,
            "version": m["version"], "journey": journey, "step": step, "attached": _now() if nid else None}


def _record(project, cid, aid, rec, placed, again_of, cwd) -> dict:
    image = rec["image"]
    env = {**_git(cwd), "devkit": core.VERSION, **(rec.get("env") or {})}
    return {"id": cid, "audit": aid, "kind": rec.get("kind"), "state": rec.get("state"), "source": rec.get("source"),
            "map": placed, "element": rec.get("element"), "recipe": rec.get("recipe"),
            "reproducible": bool(rec.get("reproducible")), "experiment": bool(rec.get("experiment")),
            "again_of": again_of, "env": env, "taken": _now(), "by": actor(),
            "image": {"sha256": image.get("sha256"), "width": image.get("width"), "height": image.get("height"),
                      "fold": image.get("fold"), "scale": image.get("scale"), "where": _where(),
                      "truncated": bool(image.get("truncated")), "page_height": image.get("page_height")},
            "description": rec.get("description"), "redaction": rec.get("redaction"), "checks": rec.get("checks"),
            "marks": None, "judgment": [], "passes": {}}


def _recipe(viewport, theme, *, dpr, steps, storage_state, data, clock, locale, timezone, forced_colors, contrast,
            vision, css, masks) -> dict:
    recipe = {"viewport": list(viewport), "dpr": dpr or 1, "theme": theme, "locale": locale or "en-US",
              "timezone": timezone or "UTC", "clock": clock, "full_page": True, "steps": [str(s) for s in steps or ()],
              "script": None, "storage_state": storage_state, "data": data,
              "emulation": {"forced_colors": bool(forced_colors), "contrast": contrast, "vision": vision},
              "css": css}
    if masks:
        recipe["mask"] = [str(s) for s in masks]
    return recipe


def capture(project: str, source: str, *, audit=None, state=None, node=None, viewports=None, themes=None, dpr=None,
            steps=(), storage_state=None, data=None, clock=None, locale=None, timezone=None, forced_colors=False,
            contrast=None, vision=None, pages=None, masks=(), css=None, remote=False, check=True, element=None,
            journey=None, step=None, again_of=None, cwd=None, probe=None) -> list:
    """`dev visual capture`: one capture of `source` (a URL, a PDF, or a picture file) for every viewport and theme
    (the audit's matrix unless given), or for every PDF page asked; each written as a record. -> the records.

    `probe`: for a URL or a PDF, a folder of the caller's (`dev visual check`, capture_checks.py) that the driver
    writes the picture and the description into instead of the local store; no record is written, and the driver's
    records are returned as it printed them."""
    aid = audit or open_audit(project)
    if not aid:
        raise VisualError(NO_AUDIT)
    a = load(project, aid)
    if not aid.startswith("VA-"):
        raise VisualError(f"--audit takes an audit (VA-…), not {aid}.")
    src = _source(source, remote)
    cwd = Path(cwd or os.getcwd())
    m = vmap.load(project)
    if node and not m["version"]:
        raise VisualError(f"No node \"{node}\": the project has no map yet. Declare it: dev visual map node add "
                          f"{node} --title \"…\".")
    if node:
        _placed(m, {}, node, None, None, None)  # a node the map does not have is refused before the capture runs
    state = _clean([state])[0] if state else None
    store = Path(probe) if probe else _private_dir(store_dir(project))
    out = []
    if src.get("kind") == "image":
        picture = _picture(src["file"])
        width, height = _png_size(picture)
        sha = hashlib.sha256(picture).hexdigest()
        blob = store / "blobs" / f"{sha}.png"
        if not blob.exists():
            _private_write(blob, picture)
        rec = {"kind": "image", "state": state,
               "source": {"file": src["file"].name, "sha256": hashlib.sha256(src["file"].read_bytes()).hexdigest(),
                          "origin": "local"},
               "element": None, "recipe": None, "reproducible": False, "experiment": False,
               "env": {}, "image": {"sha256": sha, "width": width, "height": height, "fold": None, "scale": 1,
                                    "truncated": False, "page_height": height},
               "description": None, "redaction": None, "checks": {"ran": [], "skipped": {}, "signals": []}}
        doc = _record(project, new_id("VC", project), aid, rec, _placed(m, rec, node, state, journey, step),
                      again_of, cwd)
        save(project, doc)
        return [doc]
    sizes = _viewports(viewports) if viewports else [list(v) for v in a["matrix"]["viewports"]]
    looks = _themes(themes) if themes else list(a["matrix"]["themes"])
    # the configured selectors join the capture's own in every capture, recaptures included; the recipe keeps only
    # the capture's own, so a record from another Mac cannot take this Mac's masks away
    given = [str(s) for s in masks or ()]
    spacing = design_spacing(_project_root(cwd))
    common = {"state": state, "element": element, "mask": [*config_masks(), *given],
              "components": [{"id": c["id"], "selector": c["selector"]} for c in m["components"]],
              "remote": bool(remote), "check": bool(check), "impeccable": _impeccable() if check else None,
              "store": str(store), "cwd": str(cwd), **({"spacing": spacing} if spacing else {})}
    opts = dict(dpr=dpr, steps=steps, storage_state=storage_state, data=data, clock=clock, locale=locale,
                timezone=timezone, forced_colors=forced_colors, contrast=contrast, vision=vision, css=css, masks=masks)
    if src.get("kind") == "document":
        jobs = [({"file": str(src["file"]), "page": p}, {"page": p}) for p in
                _pages(pages, _page_count(src["file"]))]
    else:
        jobs = [({"url": src["url"]}, _recipe(v, t, **opts)) for v in sizes for t in looks]
    for given, recipe in jobs:
        cid = new_id("VC", project)
        rec = _run_capture({**common, "id": cid, "source": given, "recipe": recipe}, cwd)
        out.append(rec if probe else
                   _record(project, cid, aid, rec, _placed(m, rec, node, state, journey, step), again_of, cwd))
    # written once every job is done, so a capture that fails at one viewport or page records none of them, as the
    # driver's "Nothing was recorded" says
    for doc in out if not probe else ():
        save(project, doc)
    return out


def _project_root(cwd) -> Path:
    """The project a command runs in: the git checkout of `cwd`, else `cwd` itself."""
    cwd = Path(cwd or os.getcwd())
    top = core.gitout(str(cwd), "rev-parse", "--show-toplevel")
    return Path(top or cwd).resolve()


def _outside(cid: str, what: str, value, root: Path) -> VisualError:
    return VisualError(f"{cid}'s recipe names the {what} {value}, outside this project ({core.tilde(root)}). A replay "
                       "reads it only from inside the project, since records sync from other Macs. Capture again with "
                       "a file of this project: dev visual capture <url|file> …")


def _in_project(cid: str, what: str, value, root: Path, sha=None) -> str:
    """The file a replayed recipe names, read only from inside the project (records sync from teammates, so a
    recipe never makes this Mac read a file elsewhere). A relative path is the project's; a bare name that is not at
    the project's root is looked up among the project's files, by name and, when several match, by its SHA-256.
    -> the file's full path"""
    given = Path(str(value))
    path = (given if given.is_absolute() else root / given).resolve()
    if not path.is_relative_to(root):
        raise _outside(cid, what, value, root)
    if path.is_file():
        return str(path)
    if len(given.parts) == 1:
        listed = core.gitout(str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        found = [(root / rel).resolve() for rel in listed.split("\0") if rel and Path(rel).name == given.name]
        found = [f for f in found if f.is_relative_to(root) and f.is_file()]
        if len(found) > 1 and sha:
            found = [f for f in found if hashlib.sha256(f.read_bytes()).hexdigest() == sha] or found
        if len(found) == 1:
            return str(found[0])
        if found:
            shown = ", ".join(core.tilde(f) for f in found[:3])
            raise VisualError(f"{len(found)} files of this project are named {given.name} ({shown}), and none holds "
                              f"the bytes {cid} was taken from, so its {what} is not known. Capture the one you "
                              "mean again: dev visual capture <file>")
    raise VisualError(f"The {what} {value} that {cid}'s recipe names is not in this project ({core.tilde(root)}). "
                      "Put it back, or capture again with a file of this project.")


def capture_again(project: str, cid: str, *, remote=False, audit=None, check=True, cwd=None, css=True,
                  probe=None) -> list:
    """`dev visual capture --again VC-…`: the same recipe, now, on the working tree; the new record's `again_of`
    names the first. css=False leaves out an experiment's --css rules, so the new capture is no experiment (R37).
    The recipe's storage state and a document's file are read only from inside the project. `probe`: as capture's,
    a run that writes no record (the node is not looked up either: the run attaches to nothing)."""
    rec = load(project, cid)
    if rec.get("kind") == "image" or rec.get("recipe") is None:
        raise VisualError(f"{cid} is an external picture: it has no recipe")
    source = rec.get("source") or {}
    if source.get("origin") == "remote" and not remote:
        raise _remote_refusal(urlsplit(source.get("url") or "").hostname or cid)
    r = rec["recipe"]
    root = _project_root(cwd)
    placed = rec.get("map") or {}
    common = dict(audit=audit or rec.get("audit"), state=rec.get("state"), node=None if probe else placed.get("node"),
                  again_of=cid, remote=remote, check=check, journey=placed.get("journey"), step=placed.get("step"),
                  cwd=cwd, element=(rec.get("element") or {}).get("selector"), probe=probe)
    if rec.get("kind") == "document":
        file = _in_project(cid, "document", source.get("file"), root, source.get("sha256"))
        return capture(project, file, pages=str(source["page"]), **common)
    emulation = r.get("emulation") or {}
    viewport = r.get("viewport") or DEFAULT_VIEWPORT
    storage = _in_project(cid, "storage state", r["storage_state"], root) if r.get("storage_state") else None
    return capture(project, source["url"], viewports=[f"{viewport[0]}x{viewport[1]}"], themes=[r.get("theme") or
                   "light"], dpr=r.get("dpr"), steps=r.get("steps") or (), storage_state=storage,
                   data=r.get("data"), clock=r.get("clock"), locale=r.get("locale"), timezone=r.get("timezone"),
                   forced_colors=emulation.get("forced_colors"), contrast=emulation.get("contrast"),
                   vision=emulation.get("vision"), css=r.get("css") if css else None, masks=r.get("mask") or (),
                   **common)


def description(project: str, capture_rec: dict):
    """The capture's description (devkit.visual.description/1) from the local store, or None when this Mac does not
    hold it."""
    sha = (capture_rec.get("description") or {}).get("sha256")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        return None
    try:
        return json.loads(gzip.decompress((store_dir(project) / "desc" / f"{sha}.json.gz").read_bytes()))
    except (OSError, ValueError, EOFError):
        return None


# --------------------------------------------------------------------------- passes (R19)

def _pass_of(rid: str, pass_: str) -> str:
    if rid.startswith("VC-"):
        if pass_ in STRUCTURE_PASSES:
            raise VisualError(f"{pass_} is a structure pass: record it on the audit (VA-…), not on a capture.")
        if pass_ not in SCREEN_PASSES:
            raise VisualError(f"{pass_} is not a screen pass. Screen passes: {', '.join(SCREEN_PASSES)}.")
    elif rid.startswith("VA-"):
        if pass_ in SCREEN_PASSES:
            raise VisualError(f"{pass_} is a screen pass: record it on a capture (VC-…), not on the audit.")
        if pass_ not in STRUCTURE_PASSES:
            raise VisualError(f"{pass_} is not a structure pass. Structure passes: {', '.join(STRUCTURE_PASSES)}.")
    else:
        raise VisualError(f"A pass is recorded on a capture (VC-…) or an audit (VA-…), not on {rid}.")
    return pass_


def _siblings(a: dict, b: dict) -> bool:
    """Two captures of one recipe at another width or theme: the same kind, page or document page, state, and
    element, and recipes that differ only in their viewport and theme."""
    if a.get("kind") != b.get("kind") or a.get("kind") == "image":
        return False
    sa, sb = a.get("source") or {}, b.get("source") or {}
    if any(sa.get(k) != sb.get(k) for k in ("url", "file", "page")) or a.get("state") != b.get("state"):
        return False
    if (a.get("element") or {}).get("selector") != (b.get("element") or {}).get("selector"):
        return False
    ra, rb = a.get("recipe") or {}, b.get("recipe") or {}
    return all(ra.get(k) == rb.get(k) for k in {*ra, *rb} - {"viewport", "theme"})


def _covered(project: str, cid: str, pass_: str, fid) -> dict:
    """The entry of a screen pass covered by a finding on a sibling capture (`--covered-by`): the same audit, the
    same pass, and the capture's recipe at another width or theme. The report counts it as findings."""
    if not str(cid).startswith("VC-"):
        raise VisualError(f"--covered-by records a screen pass on a capture (VC-…), not on {cid}: record a structure "
                          "pass with its findings, --clean, or --na.")
    if not str(fid).startswith("VF-"):
        raise VisualError(f"--covered-by takes a finding (VF-…), not {fid}.")
    c, f = load(project, cid), load(project, fid)
    if f.get("audit") != c.get("audit"):
        raise VisualError(f"{fid} belongs to audit {f.get('audit')}, and {cid} to {c.get('audit')}: a pass is covered "
                          "only by a finding of its own audit.")
    if not f.get("capture"):
        raise VisualError(f"{fid} is a structure finding: it sits on no capture, so it covers no screen pass.")
    if f.get("pass") != pass_:
        raise VisualError(f"{fid} is a {f.get('pass')} finding: it covers the {f.get('pass')} pass, not {pass_}.")
    if f["capture"] == cid:
        raise VisualError(f"{fid} sits on {cid} itself, so the pass is recorded as findings already.")
    s = load(project, f["capture"])
    if not _siblings(c, s):
        raise VisualError(f"{fid} sits on {s['id']}, which is not {cid}'s recipe at another width or theme (another "
                          "page, state, or steps). Record the pass with a finding of its own, --clean, or --na.")
    r = s.get("recipe") or {}
    vp = r.get("viewport") or [(s.get("image") or {}).get("width"), (s.get("image") or {}).get("fold")]
    where = f"page {(s.get('source') or {}).get('page')}" if s.get("kind") == "document" else \
        f"{vp[0]}×{vp[1]} {r.get('theme') or 'light'}"
    return {"result": "findings", "covered_by": fid, "note": f"covered by {fid} on {s['id']} ({where})"}


def record_pass(project: str, rid: str, pass_: str, *, clean=None, na=None, covered_by=None) -> dict:
    """`dev visual pass`: the pass recorded `clean` with what was checked, `n/a` with the reason, or covered by a
    finding on the same recipe at another width or theme (`covered_by`, a screen pass only)."""
    _pass_of(rid, pass_)
    given = [(r, t) for r, t in (("clean", clean), ("n/a", na), ("covered", covered_by))
             if t is not None and str(t).strip()]
    if len(given) != 1:
        raise VisualError('Record the pass with --clean "<what was checked>", --na "<reason>", or --covered-by VF-…: '
                          'one of them.')
    if given[0][0] == "covered":
        entry = _covered(project, rid, pass_, str(covered_by).strip())
    else:
        entry = {"result": given[0][0], "note": _clean([_text(given[0][1], "--clean")])[0]}

    def fn(doc):
        doc.setdefault("passes", {})[pass_] = entry
    return _update(project, rid, fn)


# --------------------------------------------------------------------------- findings (R20–R26)

def _rule(ref: str) -> str:
    text = str(ref or "").strip()
    scheme, _, rid = text.partition(":")
    if scheme not in SCHEMES or not rid or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,80}", rid):
        raise VisualError(f"{text} is not a rule reference: <scheme>:<id>, the scheme one of {', '.join(SCHEMES)}.")
    known = {"wcag": WCAG, "check": CHECKS, "map": MAP_RULES, "nielsen": {str(n) for n in range(1, 11)}}.get(scheme)
    if known is not None and rid not in known:
        what = {"wcag": "a WCAG 2.2 success criterion", "check": "a devkit check", "map": "a structure check",
                "nielsen": "one of Nielsen's ten heuristics (1-10)"}[scheme]
        raise VisualError(f"{text} is not {what}.")
    return text


def _name(e: dict):
    label = e.get("name") or e.get("text")
    return f'{e.get("role")} "{label}"' if label else e.get("role")


def _role_name(selector: str):
    m = re.fullmatch(r'role=([a-z]+)(?:\[name=(["\'])(.*)\2\])?', selector)
    return (m.group(1), m.group(3)) if m else None


def _by_selector(elements: list, cid: str, selector: str) -> dict:
    """The one element `selector` names: an element's own selector or locator, or role=<role>[name="<name>"]."""
    rn = _role_name(selector)
    found = [e for e in elements if selector in (e.get("selector"), e.get("locator")) or
             (rn and e.get("role") == rn[0] and (rn[1] is None or e.get("name") == rn[1]))]
    if len(found) == 1:
        return found[0]
    if not found:
        raise VisualError(f'"{selector}" names no element of the description of {cid}. Use an element\'s selector or '
                          f'locator (dev visual describe {cid} --interactive), or role=<role>[name="<name>"].')
    ones = [x for e in found[:2] for x in (e.get("selector"), f'role={e.get("role")}[name="{e.get("name")}"]'
                                           if e.get("name") else None) if x]
    raise VisualError(f'"{selector}" matches {len(found)} elements in {cid}. Use one that matches one: '
                      f'{" or ".join(ones[:2])}.')


def _region(value, c: dict) -> list:
    try:
        region = [int(round(float(v))) for v in (value.split(",") if isinstance(value, str) else value)]
    except (TypeError, ValueError):
        region = []
    if len(region) != 4 or region[2] <= 0 or region[3] <= 0 or region[0] < 0 or region[1] < 0:
        raise VisualError(f"{value} is not a region: give x,y,width,height in the capture's CSS px.")
    x, y, w, h = region
    width, height = (c.get("image") or {}).get("width"), (c.get("image") or {}).get("height")
    spot = ",".join(str(v) for v in region)
    if isinstance(width, int) and x + w > width:
        raise VisualError(f"The region {spot} ends at x {x + w}; the capture is {width} px wide.")
    if isinstance(height, int) and y + h > height:
        raise VisualError(f"The region {spot} ends at y {y + h}; the capture is {height} px high.")
    return region


def _signal(project: str, ref: str):
    """(owner record, signal) of a signal reference VC-…#n or VA-…#n."""
    m = SIGNAL_REF.fullmatch(str(ref or ""))
    if not m:
        raise VisualError(f"{ref} is not a signal: give it as VC-…#<n> or VA-…#<n>.")
    doc = load(project, m.group(1))
    sigs = doc.get("signals") if doc["id"].startswith("VA-") else (doc.get("checks") or {}).get("signals")
    s = next((s for s in sigs or () if isinstance(s, dict) and s.get("n") == int(m.group(2))), None)
    if s is None:
        raise VisualError(f"{doc['id']} has no signal #{m.group(2)}. List them: dev visual signal ls {doc['id']}")
    return doc, s


def _map_anchor(project: str, a: dict, node, edge, journey) -> dict:
    """The map part of an anchor: a node (with a state after #), an edge, or a journey step, each in the audit's map
    version (R22)."""
    out = {}
    if not (node or edge or journey):
        return out
    try:
        m = vmap.load(project, a["map_version"]) if a.get("map_version") else vmap.load(project)
    except vmap.MapError as e:
        raise VisualError(str(e)) from None
    v = m["version"]
    if node:
        nid, _, state = str(node).partition("#")
        try:
            n = vmap._node_of(m, nid)
        except vmap.MapError as e:
            raise VisualError(str(e)) from None
        if state and state not in ((n.get("states") or {}).get("states") or ()):
            raise VisualError(f"{nid} has no state \"{state}\" in map v{v}. Add it: dev visual map state add {nid} "
                              f"\"{state}\".")
        out.update(node=nid, state=state or None)
    if edge:
        if not any(e["id"] == edge for e in m["edges"]):
            raise VisualError(f"No edge {edge} in map v{v}.{vmap._nearest(edge, [e['id'] for e in m['edges']])} "
                              "List them with dev visual map show.")
        out["edge"] = edge
    if journey:
        jid, _, k = str(journey).partition("#")
        j = next((j for j in m["journeys"] if j["id"] == jid), None)
        if j is None or not k.isdigit() or not 1 <= int(k) <= len(j["steps"]) + 1:
            raise VisualError(f"No journey step {journey} in map v{v}: give J:<slug>#<step>, the steps numbered from 1 "
                              "at the start screen.")
        out["journey"] = f"{jid}#{int(k)}"
    return out


def mark_element(c: dict, mark) -> str:
    """The element id a mark of the capture's marked copy names (R65), or VisualError when the marks map has no such
    mark."""
    marks = (c.get("marks") or {}).get("map") or {}
    if str(mark) not in marks:
        shown = sorted(int(k) for k in marks if str(k).isdigit())
        raise VisualError(f"Mark {mark} is not on the marked copy of {c['id']}" +
                          (f" (marks {shown[0]}–{shown[-1]})." if shown else " (it has no marked copy yet).") +
                          f" Look at it again: dev visual marks {c['id']}")
    return marks[str(mark)]


def add_finding(project: str, capture_id=None, *, element=None, mark=None, selector=None, region=None,
                from_signal=None, page=None, node=None, edge=None, journey=None, evidence_captures=(), pass_=None,
                rules=(), severity=None, title=None, impact=None, evidence=None, fix=None, guide=None, root=None,
                audit=None) -> dict:
    """`dev visual finding add`: one problem pinned where it is, with its rule, severity, and fix (R20, R22). A
    finding from a signal (`from_signal`) copies its anchor, rule, and measurement, and triages it."""
    if severity not in SEVERITIES:
        raise VisualError(f"{severity} is not a severity: P0, P1, P2, P3.")
    if pass_ not in SCREEN_PASSES + STRUCTURE_PASSES:
        raise VisualError(f"{pass_} is not a pass. Screen passes: {', '.join(SCREEN_PASSES)}; structure passes: "
                          f"{', '.join(STRUCTURE_PASSES)}.")
    texts = [_text(title, "--title"), _text(impact, "--impact"), _text(evidence, "--evidence", not from_signal),
             _text(fix, "--fix")]
    rule = [_rule(r) for r in rules or ()]
    if guide is not None and not GUIDE.fullmatch(str(guide)):
        raise VisualError(f"{guide} is not a modern-web-guidance id (lowercase words joined by hyphens).")
    if root is not None:
        if not str(root).startswith("VF-"):
            raise VisualError(f"--root takes a finding (VF-…), not {root}.")
        load(project, root)
    evidence_ids = [str(i) for i in evidence_captures or ()]
    for cid in evidence_ids:
        if not cid.startswith("VC-"):
            raise VisualError(f"--evidence-capture takes captures (VC-…), not {cid}.")
        load(project, cid)
    anchor = {"element": None, "locator": None, "region": None, "selector": None, "name": None, "page": page,
              "text": None, "node": None, "state": None, "edge": None, "journey": None, "component": None}
    signal, owner, source, c = None, None, "judgment", None
    if from_signal:
        owner, signal = _signal(project, from_signal)
        if signal.get("triage"):
            raise VisualError(f"{from_signal} is already triaged ({signal['triage'].get('as')}).")
        rule = rule or [str(r) for r in signal.get("rule") or ()]
        if not texts[2]:
            measured, threshold = signal.get("measured"), signal.get("threshold")
            texts[2] = f"{measured}; threshold {threshold}" if threshold else str(measured)
        sa = signal.get("anchor") or {}
        if owner["id"].startswith("VA-"):
            source = f"map:{signal.get('check')}"
            if not evidence_ids:
                raise VisualError("A structure finding needs at least one capture as evidence")
            if pass_ in SCREEN_PASSES:
                raise VisualError(f"{pass_} is a screen pass: a structure signal's finding takes its structure pass, "
                                  f"{signal.get('pass')}.")
            anchor.update(node=sa.get("node"), state=sa.get("state"), edge=sa.get("edge"),
                          journey=f"{sa['journey']}#{sa['step']}" if sa.get("journey") and sa.get("step") else
                          sa.get("journey"))
            audit = owner["id"]
        else:
            c = owner
            source = f"check:{signal.get('check')}"
            anchor.update({k: sa.get(k) for k in ("element", "region", "selector", "name")})
    elif capture_id:
        c = load(project, capture_id)
        if not capture_id.startswith("VC-"):
            raise VisualError(f"A screen finding is pinned to a capture (VC-…), not {capture_id}.")
    elif not evidence_ids or not (node or edge or journey):
        raise VisualError("A finding needs a capture with an anchor on it (VC-… with --element, --mark, --selector, "
                          "--region, or --from), or a map element (--node, --edge, --journey) with --evidence-capture.")
    if not rule:
        raise VisualError("Give at least one rule the finding breaks: --rule <scheme>:<id>.")
    if c is None and pass_ in SCREEN_PASSES:  # its pass is recorded on the audit, which counts structure passes
        raise VisualError(f"{pass_} is a screen pass: a finding on the map without a capture takes a structure pass "
                          "(S1–S6).")
    if c is not None:
        if pass_ in STRUCTURE_PASSES:
            raise VisualError(f"{pass_} is a structure pass: a finding on a capture takes a screen pass.")
        desc = description(project, c) if (element or mark or selector or anchor["element"]) else None
        elements = (desc or {}).get("elements") or []
        if mark is not None:
            element = mark_element(c, mark)
        if element or selector:
            if desc is None:
                raise VisualError(f"The description of {c['id']} is not on this Mac, so its elements cannot be "
                                  f"read. Capture it here: dev visual capture --again {c['id']}")
            if element:
                e = next((e for e in elements if e.get("id") == element), None)
                if e is None:
                    raise VisualError(f"{element} is not in the description of {c['id']} (E1–E{len(elements)}). "
                                      f"List them: dev visual describe {c['id']} --interactive")
            else:
                e = _by_selector(elements, c["id"], selector)
            anchor.update(element=e["id"], locator=e.get("locator"), region=list(e.get("box") or []) or None,
                          selector=e.get("selector"), name=_name(e), component=e.get("component"))
            if anchor["region"]:
                anchor["region"] = _region(anchor["region"], c)
        elif region is not None:
            anchor["region"] = _region(region, c)
        elif anchor["element"] and not anchor["locator"]:
            e = next((e for e in elements if e.get("id") == anchor["element"]), None)
            if e:
                anchor.update(locator=e.get("locator"), component=e.get("component"))
        if not anchor["region"]:
            raise VisualError("A screen finding needs a region: --element, --mark, --selector, --region, or --from.")
        placed = c.get("map") or {}
        anchor.update(node=placed.get("node"), state=placed.get("state"))
        audit = c.get("audit")
        evidence_ids = evidence_ids or [c["id"]]
        if c["id"] not in evidence_ids:
            evidence_ids.insert(0, c["id"])
    audit = audit or open_audit(project)
    if not audit:
        raise VisualError(NO_AUDIT)
    if not str(audit).startswith("VA-"):
        raise VisualError(f"--audit takes an audit (VA-…), not {audit}.")
    a = load(project, audit)
    anchor.update(_map_anchor(project, a, node, edge, journey))
    title_, impact_, evidence_, fix_ = _clean(texts)
    fid = new_id("VF", project)
    who_ = who()
    finding = {"id": fid, "audit": a["id"], "capture": c["id"] if c else None, "pass": pass_, "title": title_,
               "anchor": anchor, "evidence_captures": evidence_ids, "rule": rule, "severity": severity,
               "impact": impact_, "evidence": evidence_, "fix": fix_, "guide": guide, "source": source,
               "signal": from_signal or None, "root": root, "status": "open", "task": None, "verification": None,
               "history": [{"at": _now(), "by": who_, "change": "open",
                            "why": f"from signal #{signal['n']}" if signal else "added"}]}
    triage = {"as": "finding", "finding": fid, "why": None, "by": who_, "model": None, "at": _now()}
    if owner is not None:  # the signal first, so a signal another command triaged meanwhile writes no finding
        def mark_signal(doc):
            sigs = doc.get("signals") if doc["id"].startswith("VA-") else doc["checks"]["signals"]
            _triage(next(s for s in sigs if s.get("n") == signal["n"]), from_signal, triage)
            if doc["id"].startswith("VA-"):
                doc.setdefault("passes", {})[pass_] = {"result": "findings"}
        _update(project, owner["id"], mark_signal)
    save(project, finding)
    target = c["id"] if c is not None else a["id"]
    if owner is None or not owner["id"].startswith("VA-"):
        _update(project, target, lambda doc: doc.setdefault("passes", {}).__setitem__(pass_, {"result": "findings"}))
    return finding


def findings(project: str, *, status=None, severity=None, pass_=None, audit=None) -> list:
    """`dev visual finding ls`: the findings that match every filter given, by severity, then id."""
    out = [f for f in _all(project, "VF") if (not status or f.get("status") in status) and
           (not severity or f.get("severity") in severity) and (not pass_ or f.get("pass") == pass_) and
           (not audit or f.get("audit") == audit)]
    return sorted(out, key=lambda f: (SEVERITIES.index(f["severity"]) if f.get("severity") in SEVERITIES else 9,
                                      f["id"]))


_MOVES = {"fixing": ("open", "reopened"), "fixed": ("open", "fixing", "reopened"), "open": CLOSED}


def _history(doc: dict, change: str, why, **extra) -> None:
    entry = {"at": _now(), "by": who(), "change": change, "why": why}
    entry.update({k: v for k, v in extra.items() if v is not None})
    doc.setdefault("history", []).append(entry)


def set_finding(project: str, fid: str, *, status=None, why=None, commit=None, severity=None, root=None) -> dict:
    """`dev visual finding set`: move a finding to fixing or fixed, reopen a closed one, change its severity, or
    name its root (R23, R24); every change goes into its history (R25)."""
    if not str(fid).startswith("VF-"):
        raise VisualError(f"{fid} is not a finding (VF-…).")
    given = [x for x in (status, severity, root) if x is not None]
    if len(given) != 1:
        raise VisualError("Change one thing at a time: --status, --severity, or --root.")
    if status == "verified":
        raise VisualError("Only dev visual verify marks a finding verified")
    if status is not None and status not in _MOVES:
        raise VisualError(f"{status} is not a status finding set gives: fixing, fixed, or open (to reopen a closed "
                          "finding). Close one with dev visual finding close.")
    if commit is not None and not COMMIT.fullmatch(str(commit)):
        raise VisualError(f"{commit} is not a commit (a hexadecimal SHA).")
    if severity is not None and severity not in SEVERITIES:
        raise VisualError(f"{severity} is not a severity: P0, P1, P2, P3.")
    reason = _clean([_text(why, "--why", root is None)])[0] if (why or root is None) else None
    if root is not None:
        if root == fid:
            raise VisualError("A finding cannot be its own root.")
        seen, at = {fid}, load(project, root)
        while at.get("root"):
            if at["root"] in seen:
                raise VisualError(f"{root} has {fid} among its roots, so {fid} cannot take it as its root.")
            seen.add(at["id"])
            at = load(project, at["root"])

    def fn(doc):
        cur = doc.get("status")
        if status is not None:
            if cur not in _MOVES[status]:
                raise VisualError(f"{fid} is {cur}: --status {status} is for a finding that is "
                                  f"{', '.join(_MOVES[status])}.")
            new = "reopened" if status == "open" else status
            doc["status"] = new
            _history(doc, new, reason, commit=commit)
        elif severity is not None:
            _history(doc, f"severity {doc.get('severity')} to {severity}", reason)
            doc["severity"] = severity
        else:
            doc["root"] = root
            _history(doc, f"root {root}", reason)
    return _update(project, fid, fn)


def close_finding(project: str, fid: str, as_: str, why: str, *, of=None, decision=None) -> dict:
    """`dev visual finding close`: closed without a fix, always with a reason; `wontfix` on P0 or P1 needs the
    Product owner's recorded decision (R24)."""
    if as_ not in CLOSED:
        raise VisualError(f"{as_} is not a way to close a finding: {', '.join(CLOSED)}.")
    reason = _clean([_text(why, "--why")])[0]
    if as_ == "duplicate":
        if not of or not str(of).startswith("VF-"):
            raise VisualError("A duplicate names the finding it repeats: --of VF-….")
        load(project, of)
    if decision is not None and not DECISION.fullmatch(str(decision)):
        raise VisualError(f"{decision} is not a decision id such as D7.")

    def fn(doc):
        if doc.get("status") not in ("open", "fixing", "reopened", "fixed"):
            raise VisualError(f"{fid} is {doc.get('status')}: a finding is closed from open, fixing, reopened, or "
                              "fixed.")
        if as_ == "wontfix" and doc.get("severity") in ("P0", "P1") and not decision:
            raise VisualError(f"wontfix on a {doc['severity']} finding needs the Product owner's recorded decision: "
                              "--decision D<n>.")
        doc["status"] = as_
        _history(doc, as_, reason, decision=decision, of=of)
    return _update(project, fid, fn)


# --------------------------------------------------------------------------- signals (R27, R33, R68)

def signals(project: str, rid=None, *, untriaged=False) -> list:
    """`dev visual signal ls`: [(VC-…#n or VA-…#n, signal, owner id)] of a capture, of an audit (its structure
    signals, then its captures'), or of the session's audit. A folded duplicate is inside its signal's
    `duplicates`, so it counts once."""
    rid = rid or open_audit(project)
    if not rid:
        raise VisualError(NO_AUDIT)
    doc = load(project, rid)
    owners = [doc]
    if rid.startswith("VA-"):
        owners += [c for c in _all(project, "VC") if c.get("audit") == rid]
    elif not rid.startswith("VC-"):
        raise VisualError(f"Signals belong to a capture (VC-…) or an audit (VA-…), not {rid}.")
    out = []
    for o in owners:
        sigs = o.get("signals") if o["id"].startswith("VA-") else (o.get("checks") or {}).get("signals")
        for s in sigs or ():
            if isinstance(s, dict) and isinstance(s.get("n"), int) and not (untriaged and s.get("triage")):
                out.append((f"{o['id']}#{s['n']}", s, o["id"]))
    return out


def _model(model):
    if model is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,80}", str(model)):
        raise VisualError(f"{model} is not a model id (or human).")
    return model


def dismiss(project: str, ref: str, why: str, *, model=None) -> dict:
    """`dev visual signal dismiss`: the signal is triaged as dismissed, with the reason, who, and the model (an id,
    or `human`) that judged it. -> the signal."""
    owner, s = _signal(project, ref)
    if s.get("triage"):
        raise VisualError(f"{ref} is already triaged ({s['triage'].get('as')}).")
    triage = {"as": "dismissed", "finding": None, "why": _clean([_text(why, "--why")])[0], "by": who(),
              "model": _model(model), "at": _now()}

    def fn(doc):
        sigs = doc.get("signals") if doc["id"].startswith("VA-") else doc["checks"]["signals"]
        _triage(next(x for x in sigs if x.get("n") == s["n"]), ref, triage)
    _update(project, owner["id"], fn)
    return {**s, "triage": triage}


def _triage(signal: dict, ref: str, triage: dict) -> None:
    """Triage the signal as its record holds it, inside the record's lock: another command may have triaged it
    since it was read."""
    if signal.get("triage"):
        raise VisualError(f"{ref} is already triaged ({signal['triage'].get('as')}).")
    signal["triage"] = triage


def _has_rule(s: dict, rule: str) -> bool:
    """Whether `--rule` names the signal's axe rule, one of its rule references (wcag:2.5.8), or one's id (2.5.8)."""
    rules = [str(r) for r in s.get("rule") or ()]
    return rule == s.get("axe_rule") or rule in rules or rule in [r.partition(":")[2] for r in rules]


def dismiss_all(project: str, check: str, *, rule=None, audit=None, why=None, model=None) -> list:
    """`dev visual signal dismiss --check <check> [--rule <rule>]`: every untriaged signal of one check (and rule) of
    an audit (--audit, or the session's), its structure signals and its captures', dismissed at once, each with the
    reason, who, and the model. A signal is of the check its own `check` names: one folded into it (R68) is not, so
    dismissing advice never dismisses the WCAG failure that kept it. -> the references dismissed, as signal ls lists
    them"""
    known = sorted(CHECKS | set(vmap._CHECKS) | {"changed-region"})
    if check not in known:
        near = difflib.get_close_matches(str(check), known, n=1, cutoff=0.6)
        raise VisualError(f'"{check}" is not a check.' + (f" Nearest: {near[0]}." if near else "") +
                          f" The checks: {', '.join(known)}.")
    reason = _clean([_text(why, "--why")])[0]
    if audit is not None and not str(audit).startswith("VA-"):
        raise VisualError(f"--audit takes an audit (VA-…), not {audit}.")
    aid = audit or open_audit(project)
    if not aid:
        raise VisualError(NO_AUDIT)
    rows = [(ref, s, owner) for ref, s, owner in signals(project, aid, untriaged=True)
            if s.get("check") == check and (rule is None or _has_rule(s, str(rule)))]
    triage = {"as": "dismissed", "finding": None, "why": reason, "by": who(), "model": _model(model), "at": _now()}
    wanted, done = {}, []
    for ref, s, owner in rows:
        wanted.setdefault(owner, set()).add(s["n"])
    for owner, numbers in wanted.items():
        def fn(doc, numbers=numbers):
            sigs = doc.get("signals") if doc["id"].startswith("VA-") else (doc.get("checks") or {}).get("signals")
            for x in sigs or ():
                if isinstance(x, dict) and x.get("n") in numbers and not x.get("triage"):
                    x["triage"] = dict(triage)
                    done.append(f"{doc['id']}#{x['n']}")
        _update(project, owner, fn)
    return [ref for ref, _s, _o in rows if ref in done]


# --------------------------------------------------------------------------- structure signals (R52)

def _walks(project: str, audit_id: str) -> list:
    """The journey walks kept under the audit (walk.py)."""
    from . import walk  # walk.py reaches the store from inside its command only, so this import is not a cycle
    return walk.load(project, audit_id)


def crawl_avoid() -> list:
    """The configuration's `visual.crawl.avoid` words, read fresh: the crawl adds them to its own."""
    cfg, _errors = core.config_snapshot()
    return [str(w) for w in ((cfg.get("visual") or {}).get("crawl") or {}).get("avoid") or ()]


def config_masks() -> list:
    """The configuration's `visual.mask` selectors, read fresh: every capture paints them over (R13)."""
    cfg, _errors = core.config_snapshot()
    return [str(s) for s in (cfg.get("visual") or {}).get("mask") or ()]


def structure_signals(project: str, audit_id: str):
    """`dev visual map check` for an audit: the structure signals of the current map and the audit's walks, kept on
    the audit (a signal found again keeps its number and triage). -> (the signals found, the map)"""
    audit = load(project, audit_id)
    if not audit_id.startswith("VA-"):
        raise VisualError(f"Structure signals are kept on an audit (VA-…), not {audit_id}.")
    caps = captures_of(project, audit_id)
    m = vmap.load(project)
    found = vmap.check(m, audit=audit, walks=_walks(project, audit_id), captures=caps,
                       descriptions={c["id"]: description(project, c) or {} for c in caps})
    _update(project, audit_id, lambda doc: doc.__setitem__("signals", vmap.merge(doc.get("signals") or [], found)))
    return found, m


# --------------------------------------------------------------------------- verification (R23, R25, R34, R35)

VERIFIES = ("open", "reopened", "fixing", "fixed", "verified")  # verified again: a regression check
REOPENS = ("fixed", "verified")  # a fix was claimed or proven, and the check fires again


def _family(check: str) -> str:
    """What runs a check, as a capture's `checks.ran` names it: axe, the detector, or devkit's own checks."""
    return check if check in ("axe", "impeccable") else "devkit"


OLD_POSITION = "anchor found by its old position only"
_ROOT_STEPS = re.compile(r"^html > (?:body > )?")
_OWN_TESTID = re.compile(r'\[data-testid="((?:[^"\\]|\\.)*)"\]')
_OWN_ID = re.compile(r"#((?:\\[0-9a-fA-F]{1,6}[ \t\n]?|\\.|[^\s>+~:.,#\[\]()\\'\"])+)")
_TESTID_LOCATOR = re.compile(r"getByTestId\('((?:[^'\\]|\\.)*)'\)")
_ID_LOCATOR = re.compile(r"locator\('(#(?:[^'\\]|\\.)*)'\)")
_CSS_ESCAPE = re.compile(r"\\(?:([0-9a-fA-F]{1,6})[ \t\n]?|(.))", re.S)
_JS_ESCAPE = re.compile(r"\\(.)", re.S)


def _css_text(escaped: str) -> str:
    """A CSS identifier or string as text: `\\31 a` is `1a`, `\\:` is `:`."""
    return _CSS_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)) if m.group(1) else m.group(2), escaped)


def _one_spelling(selector):
    """`selector` spelled one way, so the selectors devkit's writers produce compare: the checks write a path from
    the root (`html > body > header > p`), the description the shortest path that is unique (`header > p`). Space
    around a combinator becomes one space on each side and other runs of space one space, outside quotes, brackets,
    parentheses, and escapes; then a leading `html > body > ` or `html > ` goes. None for no selector."""
    if not isinstance(selector, str) or not selector.strip():
        return None
    out, space, combinator, quote, depth, i = [], False, None, None, 0, 0
    while i < len(selector):
        ch = selector[i]
        step = selector[i:i + 2] if ch == "\\" else ch
        i += len(step)
        if quote or depth:
            out.append(step)
            if quote and step == quote:
                quote = None
            elif not quote and ch in "\"'":
                quote = ch
            elif not quote:
                depth += {"[": 1, "(": 1, "]": -1, ")": -1}.get(ch, 0)
            continue
        if ch.isspace():
            space = True
            continue
        if ch in ">+~":
            combinator = ch
            continue
        if out and combinator:
            out.append(f" {combinator} ")
        elif out and space:
            out.append(" ")
        space, combinator = False, None
        out.append(step)
        if ch in "\"'":
            quote = ch
        elif ch in "[(":
            depth = 1
    return _ROOT_STEPS.sub("", "".join(out), count=1)


def _own_ids(keys: dict) -> tuple:
    """The test ids and the ids an element's own selector or locator names it by (a test id or id at the start of a
    path is an ancestor's, so it does not count). -> ({test ids}, {ids})"""
    testids, ids = set(), set()
    selector, locator = keys.get("selector"), keys.get("locator")
    if isinstance(selector, str):
        if m := _OWN_TESTID.fullmatch(selector.strip()):
            testids.add(_css_text(m.group(1)))
        elif m := _OWN_ID.fullmatch(selector.strip()):
            ids.add(_css_text(m.group(1)))
    if isinstance(locator, str):  # a JavaScript string in quotes, as capture.cjs and Playwright write it
        if m := _TESTID_LOCATOR.fullmatch(locator.strip()):
            testids.add(_JS_ESCAPE.sub(r"\1", m.group(1)))
        elif (m := _ID_LOCATOR.fullmatch(locator.strip())) and \
                (own := _OWN_ID.fullmatch(_JS_ESCAPE.sub(r"\1", m.group(1)))):
            ids.add(_css_text(own.group(1)))
    return testids, ids


def _named(name) -> bool:
    """A role with a name (`button "Export"`); a role alone names too many elements to find one by."""
    return isinstance(name, str) and bool(re.fullmatch(r'\S+ ".*"', name, re.S))


def _found_again(elements: list, anchor: dict, before: dict = None) -> dict:
    """The anchor on the new capture, and how it was found (`found_by`): the one element its locator names, else the
    one its own test id or id names, else the one its selector names spelled one way, else the one with its role and
    name; only when none of these names exactly one element, its old region (with the element of exactly that box,
    when the new description has one), and `found_by` is "region". The anchor's keys are its own and, when the
    capture before is described on this Mac, its element's there: a check writes the selector its own way, and the
    descriptions before and after write theirs alike."""
    known = [anchor]
    own = next((e for e in (before or {}).get("elements") or () if anchor.get("element") and
                e.get("id") == anchor["element"]), None)
    if own:
        known.append({"locator": own.get("locator"), "selector": own.get("selector"), "name": _name(own)})
    boxed = [e for e in elements if e.get("box")]
    ways = (("locator", lambda k: {k.get("locator")}),
            ("test id", lambda k: _own_ids(k)[0]),
            ("id", lambda k: _own_ids(k)[1]),
            ("selector", lambda k: {_one_spelling(k.get("selector"))}),
            ("role and name", lambda k: {k.get("name")} if _named(k.get("name")) else set()))
    named = [{**e, "name": _name(e)} for e in boxed]
    for how, keys in ways:
        wanted = list(dict.fromkeys(v for k in known for v in keys(k) if v))
        index = [keys(e) for e in named] if wanted else []
        for want in wanted:
            hits = [e for e, have in zip(boxed, index) if want in have]
            if len(hits) == 1:
                return {"element": hits[0]["id"], "region": list(hits[0]["box"]), "found_by": how}
    region = list(anchor.get("region") or []) or None
    same = [e for e in elements if region and list(e.get("box") or []) == region]
    return {"element": same[-1]["id"] if same else None, "region": region, "found_by": "region"}


def _meets(a, b) -> bool:
    return bool(a and b) and a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def _fires(signals: list, check: str, original: dict, at: dict):
    """The signal of `check` on the new capture at the anchor found again, or None. The same axe rule or detector
    rule as the signal that raised the finding; a duplicate folded into another source's signal counts (R68)."""
    for s in signals:
        if not isinstance(s, dict) or s.get("check") == "changed-region":
            continue
        for src in [s, *[d for d in s.get("duplicates") or () if isinstance(d, dict)]]:
            if src.get("check") != check:
                continue
            if check == "axe" and original.get("axe_rule") and src.get("axe_rule") != original["axe_rule"]:
                continue
            if check == "impeccable" and sorted(src.get("rule") or ()) != sorted(original.get("rule") or ()):
                continue
            sa = s.get("anchor") or {}
            same = sa.get("element") == at["element"] if sa.get("element") and at["element"] else \
                _meets(sa.get("region"), at["region"])
            if same:
                return {**s, "measured": src.get("measured", s.get("measured"))}
    return None


def _original(project: str, f: dict) -> dict:
    """The signal that raised the finding, as its capture or audit keeps it ({} when it is gone)."""
    try:
        return _signal(project, f["signal"])[1] if f.get("signal") else {}
    except VisualError:
        return {}


def _number_regions(project: str, cid: str, regions: list) -> list:
    """The changed regions as signals on the new capture, numbered after its own. -> their references"""
    refs = []

    def fn(doc):
        sigs = doc.setdefault("checks", {"ran": [], "skipped": {}, "signals": []}).setdefault("signals", [])
        n = max([s["n"] for s in sigs if isinstance(s, dict) and isinstance(s.get("n"), int)] + [0])
        for s in regions:
            n += 1
            sigs.append({"n": n, **s, "triage": None, "duplicates": []})
            refs.append(f"{cid}#{n}")
    _update(project, cid, fn)
    return refs


def _outcome(doc: dict, result: str, message: str) -> dict:
    return {"finding": doc["id"], "status": doc.get("status"), "result": result, "message": message,
            "verification": doc.get("verification")}


def _verification(capture=None, check="none", **fields) -> dict:
    v = {"capture": capture, "check": check, "inside": None, "outside": None, "new_signals": [], "accepted_by": None,
         "diff": None, "anchor": None, "measured": None, "error": None, "commit": None, "map_version": None,
         "review": None, "compared": None, "at": _now(), "by": who()}
    v.update(fields)
    return v


def _failed(project: str, f: dict, message: str, commit, capture=None) -> dict:
    """A verification that could not run (the recapture or the comparison failed): the error is kept, the status
    stays."""
    error = re.sub(r"\s*Nothing was (?:recorded|compared)\.?\s*$", "", str(message)).rstrip(" .")

    def fn(doc):
        doc["verification"] = _verification(capture, error=error, commit=commit)
        _history(doc, "verify failed", error)
    doc = _update(project, f["id"], fn)
    on = f" on commit {commit}" if commit else ""
    return _outcome(doc, "error", f"Could not verify {f['id']}: {error}{on}. Status: {doc['status']}.")


def _settle(project: str, f: dict, v: dict, result: str, what: str, note: str = None, before_on=None) -> dict:
    """Keep the verification and move the status: verified when the check no longer fires; reopened when it still
    fires on a finding that was fixed or verified, else the status stays (R24). When the anchor was found by its old
    position only, the message and the history say so: what sits there now may be another element, so a check that
    no longer fires there never verifies the finding (`result` is then "none"); a reviewer accepts it instead. When
    the picture before is not on this Mac (`v["compared"]` is False), nothing was compared: the history names the
    Mac that took it by its machine id (`before_on`, as the record does), the message by its name (map.mac_name)."""
    check = (f.get("source") or "").partition(":")[2]
    only = f" · {OLD_POSITION}" if (v.get("anchor") or {}).get("found_by") == "region" else ""
    away = v.get("compared") is False
    taken = "picture before was taken on {} and is not on this Mac"
    said = taken.format(vmap.mac_name(before_on))
    kept = f" · nothing compared: the {taken.format(before_on or 'another Mac')}" if away else ""  # the record's words
    nothing = f" · nothing compared: the {said}" if away else ""  # the words a person reads

    def fn(doc):
        doc["verification"] = v
        cur = doc.get("status")
        if result == "pass":
            doc["status"] = "verified"
            _history(doc, "verified", f"{check} no longer fires on {what}{only}{kept}")
        elif result == "fail":
            doc["status"] = "reopened" if cur in REOPENS else cur
            _history(doc, "reopened" if doc["status"] != cur else "verify", f"still there on {what}: {check}{only}{kept}")
        else:
            _history(doc, "verify", f"{note}{kept}" if note else note)
    doc = _update(project, f["id"], fn)
    fid = doc["id"]
    note = f"{note}{nothing}" if note else note
    if result == "pass":
        message = f"{fid}  verified · {check} no longer fires on {what}{only}{nothing}"
    elif result == "fail":
        message = f"{fid} is still there: {check} fires again on {what}{only}{nothing}. Status: {doc['status']}."
    elif f.get("source") == "judgment" and away:
        message = (f"{fid} came from judgment, and its {said}. Look at {what}, then accept it: dev visual verify {fid} "
                   f"--accept --why \"…\"")
    elif f.get("source") == "judgment":
        message = (f"{fid} came from judgment. Before and after are in the report; accept with dev visual verify "
                   f"{fid} --accept --why \"…\"")
    elif only and _waits_for_review(doc) and doc.get("status") == "fixed":
        message = (f"{fid}: {note}, so its status stays {doc['status']} until a reviewer compares the before and after "
                   f"and accepts it: dev visual verify {fid} --accept --why \"…\"")
    else:
        message = f"{fid}: {note}, so its status stays {doc['status']}."
    return _outcome(doc, result, message)


def _judge_screen(project: str, f: dict, before: dict, after: dict, elements: list, regions: bool) -> dict:
    """One screen finding on the new capture: its anchor found again, its check read, the pictures compared."""
    anchor = f.get("anchor") or {}
    at = _found_again(elements, anchor, description(project, before))
    boxes = []
    for box in (anchor.get("region"), at["region"]):
        if box and list(box) not in boxes:
            boxes.append(list(box))
    commit = (after.get("env") or {}).get("commit")
    # the picture before can be on the Mac that took it only (records sync, pictures do not): then the check decides
    # on the new capture alone, and nothing is compared
    sha = (before.get("image") or {}).get("sha256")
    here = isinstance(sha, str) and (store_dir(project) / "blobs" / f"{sha}.png").is_file()
    before_on = None if here else str((before.get("image") or {}).get("where") or "").removeprefix("local:") or None
    try:
        cmp = vcompare.compare_captures(records_dir(project), store_dir(project), before["id"], after["id"],
                                        boxes or None) if here else \
            {"inside": None, "outside": None, "diff": None, "signals": [], "pixels": None, "changed": None,
             "regions_total": None}
    except vcompare.CompareError as e:
        return _failed(project, f, str(e), commit, capture=after["id"])
    source = f.get("source") or ""
    check = source[len("check:"):] if source.startswith("check:") else None
    result, measured, note, review = "none", None, None, None
    if check:
        checks = after.get("checks") or {}
        family = _family(check)
        if family in (checks.get("ran") or ()):
            hit = _fires(checks.get("signals") or [], check, _original(project, f), at)
            result, measured = ("fail", hit.get("measured")) if hit else ("pass", None)
            if result == "pass" and at["found_by"] == "region":
                # what is at the old position now may be another element, and the defect may have moved with its own
                result, note = "none", f"{check} no longer fires on {after['id']} · {OLD_POSITION}"
                review = "old position"
        else:
            why = (checks.get("skipped") or {}).get(family) or "not run"
            note = f"the {check} check did not run on {after['id']} ({why})"
    else:
        note = f"before and after are {before['id']} and {after['id']}; waiting for a reviewer"
    refs = _number_regions(project, after["id"], cmp["signals"]) if regions and cmp["signals"] else []
    v = _verification(after["id"], result, inside=cmp["inside"], outside=cmp["outside"], new_signals=refs,
                      diff=cmp["diff"], anchor=at, measured=measured, commit=commit, review=review, compared=here)
    out = _settle(project, f, v, result, after["id"], note, before_on)
    out["numbers"] = {k: cmp[k] for k in ("pixels", "changed", "regions_total")}
    return out


def _verify_screen(project: str, f: dict, children: list, remote: bool, cwd) -> list:
    before = load(project, f["capture"])
    if before.get("kind") == "image" or before.get("recipe") is None:
        raise VisualError(f"{f['id']} is on an external picture ({before['id']}), which has no recipe to capture "
                          "again. Its findings are verified by eye against a new capture: capture the screen with "
                          "dev visual capture <url>, and add the finding there, or accept the fix once you have looked: "
                          f"dev visual verify {f['id']} --accept --why \"…\"")
    source = before.get("source") or {}
    if source.get("origin") == "remote" and not remote:
        raise _remote_refusal(urlsplit(source.get("url") or "").hostname or before["id"])
    try:
        after = capture_again(project, before["id"], remote=remote, cwd=cwd, css=False)[0]
    except VisualError as e:
        return [_failed(project, f, str(e), _git(cwd)["commit"])]
    elements = (description(project, after) or {}).get("elements") or []
    out = [_judge_screen(project, f, before, after, elements, regions=True)]
    for g in children:
        if g.get("capture") == before["id"]:
            out.append(_judge_screen(project, g, before, after, elements, regions=False))
        else:
            out.append(_outcome(g, "skipped", f"{g['id']} has {f['id']} as its root but another capture: verify it "
                                              f"on its own, dev visual verify {g['id']}"))
    return out


def _judge_structure(project: str, f: dict, found: list, m: dict, new: list, done: str, steps=()) -> dict:
    source = f.get("source") or ""
    v = _verification(map_version=m["version"], new_signals=new, steps=list(steps))
    if not source.startswith("map:"):
        return _settle(project, f, v, "none", f"map v{m['version']}", "waiting for a reviewer")
    key = vmap.signal_key(_original(project, f) or {"check": source[4:], "anchor": {}})
    hit = next((s for s in found if vmap.signal_key(s) == key), None)
    v.update(check="fail" if hit else "pass", measured=hit.get("measured") if hit else None)
    out = _settle(project, f, v, v["check"], f"map v{m['version']} ({done})")
    if hit and done == "the map as declared":
        out["message"] += (" The map has no address from a crawl, so the check read it as declared: after a fix, "
                           "change the map (dev visual map edge add) or crawl it (dev visual map crawl <url>), then "
                           "verify.")
    return out


def _compare_steps(project: str, earlier, record: dict) -> list:
    """Each step of a journey walked again, its captures compared with the ones the earlier walk took at that step
    of the same recipe (pixelmatch, compare.py). -> [{step, node, before, after, changed, pixels, regions, diff,
    error}]; `before` is None for a step the earlier walk did not capture, `error` says why a pair was not compared."""
    before = {s.get("step"): s.get("captures") or [] for s in (earlier or {}).get("steps") or [] if isinstance(s, dict)}
    out = []
    for s in record.get("steps") or []:
        for after_id in s.get("captures") or []:
            row = {"step": s.get("step"), "node": s.get("node"), "before": None, "after": after_id, "changed": None,
                   "pixels": None, "regions": None, "diff": None, "error": None}
            try:
                after = load(project, after_id)
                for bid in before.get(s.get("step")) or []:
                    try:
                        prior = load(project, bid)
                    except VisualError:
                        continue
                    if not vcompare.recipe_difference(prior, after):
                        row["before"] = bid
                        break
                if row["before"]:
                    r = vcompare.compare_captures(records_dir(project), store_dir(project), row["before"], after_id)
                    row.update(changed=r["changed"], pixels=r["pixels"], regions=r["regions_total"], diff=r["diff"])
            except (VisualError, vcompare.CompareError) as e:
                row["error"] = str(e)
            out.append(row)
    return out


def _walk_or_crawl(project: str, f: dict, remote: bool):
    """New structure evidence before the check: the journey the finding's signal is anchored on, walked again with
    a capture of each step in the audit's matrix, each compared with the capture of its step on the earlier walk;
    otherwise the map crawled again from its start screen (within the section of a section audit). A map that was
    only declared, with no address from a crawl, is checked as it is. -> (what was done, for the message; the step
    comparisons of a walk, else [])"""
    from . import walk
    anchor = (_original(project, f).get("anchor") or {}) if f.get("signal") else {}
    m = vmap.load(project)
    audit = load(project, f["audit"])
    journey = anchor.get("journey") or (str((f.get("anchor") or {}).get("journey") or "").partition("#")[0] or None)
    if journey and m.get("base"):
        def shoot(source, **step):
            step.pop("viewports", None)
            step.pop("themes", None)
            return capture(project, source, audit=audit["id"], remote=remote, **step)
        earlier = next((w for w in walk.load(project, audit["id"]) if w.get("journey") == journey), None)
        record = walk.walk(m, journey, capture=shoot, remote=remote)
        walk.save(project, audit["id"], record)
        steps = _compare_steps(project, earlier, record)
        compared = len({s["step"] for s in steps if s["diff"]})
        done = f"walked {journey} again" + (f"; {compared} step{'s' if compared != 1 else ''} compared with their "
                                            "earlier captures" if compared else "")
        return done, steps
    start = next((n for n in m["nodes"] if n["id"] == m.get("start")), None)
    where = (start or {}).get("example") or (start or {}).get("route")
    if not (m.get("base") and where and ":" not in where.split("?")[0]):
        return "the map as declared", []
    vmap.crawl(project, walk._origin(m["base"]) + (where if where.startswith("/") else f"/{where}"),
               within=audit.get("within"), avoid=crawl_avoid(), remote=remote, by=who(), why=f"verify {f['id']}")
    return "crawled again", []


def _verify_structure(project: str, f: dict, children: list, remote: bool) -> list:
    aid = f.get("audit")
    before = {s.get("n") for s in load(project, aid).get("signals") or () if isinstance(s, dict)}
    try:
        done, steps = _walk_or_crawl(project, f, remote)
    except (VisualError, vmap.MapError) as e:
        return [_failed(project, f, str(e), None)]
    found, m = structure_signals(project, aid)
    new = [vmap.ref(aid, s) for s in found if s.get("n") not in before]
    out = [_judge_structure(project, f, found, m, new, done, steps)]
    for g in children:
        if not g.get("capture") and g.get("audit") == aid:
            out.append(_judge_structure(project, g, found, m, [], done, steps))
        else:
            out.append(_outcome(g, "skipped", f"{g['id']} has {f['id']} as its root but other evidence: verify it "
                                              f"on its own, dev visual verify {g['id']}"))
    return out


def _verify_one(project: str, f: dict, remote: bool, cwd) -> list:
    status = f.get("status")
    if status not in VERIFIES:
        raise VisualError(f"{f['id']} is {status}: a closed finding is not verified. Reopen it first: dev visual "
                          f"finding set {f['id']} --status open --why \"…\"")
    children = [g for g in findings(project) if g.get("root") == f["id"] and g.get("status") in VERIFIES]
    if f.get("capture"):
        return _verify_screen(project, f, children, remote, cwd)
    return _verify_structure(project, f, children, remote)


def _on_picture(project: str, f: dict) -> bool:
    """Whether the finding is on an external picture: a capture with no recipe to capture again."""
    try:
        c = load(project, f["capture"]) if f.get("capture") else None
    except VisualError:
        return False
    return bool(c) and (c.get("kind") == "image" or c.get("recipe") is None)


def _waits_for_review(f: dict) -> bool:
    """Whether the finding's check no longer fires at its anchor's old position, where no element was found again:
    only a person comparing the before and after can tell whether the defect moved with its element."""
    v = f.get("verification") or {}
    return v.get("review") == "old position" and not v.get("error") and not v.get("accepted_by")


def _accept(project: str, f: dict, why) -> dict:
    """`verify --accept --why`: a reviewer accepts the fix of a judgment finding on the before and after already
    recorded, of a finding on an external picture after looking at the screen, or of a check finding whose anchor
    was found by its old position only."""
    reason = _clean([_text(why, "--why")])[0]
    fid = f["id"]
    picture = _on_picture(project, f)
    if f.get("source") != "judgment" and not picture and not _waits_for_review(f):
        raise VisualError(f"{fid} came from {f.get('source')}: that check decides it, so it takes no --accept. Run "
                          f"dev visual verify {fid}")
    v = f.get("verification") or {}
    if not picture and (not v or v.get("error") or (f.get("capture") and not v.get("capture"))):
        raise VisualError(f"{fid} has no before and after to accept yet. Capture its recipe again first: dev visual "
                          f"verify {fid}")
    if f.get("status") != "fixed":
        raise VisualError(f"{fid} is {f.get('status')}: a reviewer accepts a fix once the finding is fixed (dev visual "
                          f"finding set {fid} --status fixed --why \"…\").")
    who_ = who()

    def fn(doc):
        doc["verification"] = {**(doc.get("verification") or _verification()), "accepted_by": who_}
        doc["status"] = "verified"
        _history(doc, "verified", reason, accepted=True)
    doc = _update(project, fid, fn)
    return _outcome(doc, "accepted", f"{fid}  verified · accepted by {who_}: {reason}")


def verify(project: str, rid: str, *, accept=False, why=None, remote=False, cwd=None) -> list:
    """`dev visual verify`: a finding (VF-…) with the findings it is the root of, or every fixed or verified finding
    of an audit (VA-…), checked on new evidence. -> one outcome per finding: {finding, status, result (pass, fail,
    none, error, accepted, skipped), message, verification}"""
    rid = str(rid or "")
    if rid.startswith("VA-"):
        if accept:
            raise VisualError("--accept takes one finding (VF-…), not an audit.")
        load(project, rid)
        todo = [f for f in findings(project, audit=rid) if f.get("status") in ("fixed", "verified")]
        ids = {f["id"] for f in todo}
        out = []
        for f in todo:
            if f.get("root") not in ids:  # a finding whose root is here is checked with its root
                try:
                    out += _verify_one(project, f, remote, cwd)
                except VisualError as e:  # one finding that cannot be verified here stops none of the others
                    out.append(_outcome(f, "error", str(e)))
        return out
    if not rid.startswith("VF-"):
        raise VisualError(f"dev visual verify takes a finding (VF-…) or an audit (VA-…), not {rid}.")
    f = load(project, rid)
    return [_accept(project, f, why)] if accept else _verify_one(project, f, remote, cwd)


# --------------------------------------------------------------------------- share (R12)

def _webp(png_path: Path, out: Path) -> int:
    """Encode `png_path` as WebP at most SHARE_MAX bytes: the best quality that fits, then half the size."""
    cwebp = shutil.which("cwebp")
    if not cwebp:
        raise VisualError("cwebp is not installed, so the pictures cannot be shared as WebP. Nothing was shared. "
                          "Install it: dev install --only tools (Homebrew's webp).")
    for resize in (None, 0.5, 0.25):
        for quality in (90, 80, 70, 60, 50, 40):
            args = [cwebp, "-quiet", "-metadata", "none", "-q", str(quality)]
            if resize:
                w = _png_size(png_path.read_bytes())[0]
                args += ["-resize", str(max(1, int(w * resize))), "0"]
            run = subprocess.run([*args, str(png_path), "-o", str(out)], capture_output=True, text=True, timeout=120)
            if run.returncode != 0:
                raise VisualError(f"cwebp could not encode {png_path.name}. Nothing was shared.")
            size = out.stat().st_size
            if size <= SHARE_MAX:
                return size
    raise VisualError(f"{png_path.name} does not fit in {SHARE_MAX // 1024} KB as WebP. Nothing was shared.")


def share(project: str, rid: str) -> dict:
    """`dev visual share VF-…|VA-…`: copy the pictures the finding (or every finding of the audit) uses into the
    handoff repository as media/<sha256>.webp, each checked against its record first. Refuses a remote capture, a
    picture not on this Mac or not matching its record's SHA-256, and more than SHARE_MAX a picture or
    AUDIT_SHARE_MAX an audit. -> {"media": [paths], "bytes": n}"""
    doc = load(project, rid)
    if rid.startswith("VF-"):
        fs = [doc]
    elif rid.startswith("VA-"):
        fs = [f for f in _all(project, "VF") if f.get("audit") == rid]
    else:
        raise VisualError(f"share takes a finding (VF-…) or an audit (VA-…), not {rid}.")
    ids = []
    for f in fs:
        for cid in [f.get("capture"), *(f.get("evidence_captures") or ()), (f.get("verification") or {}).get("capture")]:
            if isinstance(cid, str) and cid.startswith("VC-") and cid not in ids:
                ids.append(cid)
    if not ids:
        raise VisualError(f"{rid} uses no capture, so there is no picture to share.")
    store, media = store_dir(project), records_dir(project) / "media"
    pictures = []
    for cid in ids:
        c = load(project, cid)
        if (c.get("source") or {}).get("origin") == "remote":
            raise VisualError(f"{cid} is a capture of a remote host, and share never copies one. Nothing was shared.")
        if c.get("kind") != "image" and not isinstance(c.get("redaction"), dict):
            raise VisualError(f"{cid} has no redaction record, so its picture cannot be shared. Nothing was shared.")
        sha = (c.get("image") or {}).get("sha256") or ""
        blob = store / "blobs" / f"{sha}.png"
        try:
            data = blob.read_bytes()
        except OSError:
            mac = vmap.mac_name(str((c.get("image") or {}).get("where") or "").removeprefix("local:"))
            raise VisualError(f"The picture of {cid} is not on this Mac: it was taken on {mac}. Share it from there, "
                              f"or capture it here: dev visual capture --again {cid}") from None
        if hashlib.sha256(data).hexdigest() != sha:
            raise VisualError(f"The picture of {cid} does not match its record (SHA-256 {sha[:12]}…). Nothing was "
                              f"shared. Capture it again: dev visual capture --again {cid}")
        if sha not in [p[1] for p in pictures]:
            pictures.append((c, sha, blob))
    folder = Path(tempfile.mkdtemp(prefix="devkit-visual-"))
    try:
        os.chmod(folder, 0o700)
        sizes = {sha: _webp(blob, folder / f"{sha}.webp") for _c, sha, blob in pictures}
        audit_ids = {doc["id"] if rid.startswith("VA-") else doc.get("audit")}
        others = {(c.get("image") or {}).get("sha256") for c in _all(project, "VC") if c.get("audit") in audit_ids}
        already = sum((media / f"{s}.webp").stat().st_size for s in others - set(sizes)
                      if isinstance(s, str) and (media / f"{s}.webp").is_file())
        if already + sum(sizes.values()) > AUDIT_SHARE_MAX:
            raise VisualError(f"The audit's shared pictures would take {(already + sum(sizes.values())) // 1024} KB, "
                              f"more than {AUDIT_SHARE_MAX // (1024 * 1024)} MB. Nothing was shared. Share fewer "
                              "findings.")
        media.mkdir(parents=True, exist_ok=True)
        out = []
        for _c, sha, _blob in pictures:
            target = media / f"{sha}.webp"
            shutil.copyfile(folder / f"{sha}.webp", target)
            out.append(target)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    shas = [sha for _c, sha, _b in pictures]
    _update(project, rid, lambda d: _history(d, "shared", None, pictures=shas, host=vmap.this_mac()))
    return {"media": out, "bytes": sum(sizes.values())}
