# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev visual check <VC-…|VA-…> [--only <check,…>] [--remote]`: the checks run again on a capture's recipe, or on the
recipe of every capture of an audit, and their signals kept on the capture they ran for.

The recipe runs again as `capture --again` runs it (store.capture_again: the storage state and a document are read
only from the project, and a host that is not this Mac needs --remote), as a probe: capture.cjs writes its picture and
description into a new 0700 folder, deleted when the run ends, and no capture record is written. Then, under the
capture's lock:

- a signal found again keeps its number and its triage, with the new measurement. Two signals are the same signal
  when they come from the same check (the same axe rule, or the same detector rules) on the same element (else the
  same selector, else the same box), with the same second element for an overlap; a duplicate folded into another
  source's signal counts (R68), as verify reads it;
- a new signal takes the capture's next number, untriaged, so it is `VC-…#<n>` like the capture's own signals;
- an earlier signal not found again stays as it was, its triage with it, and the command lists it. A changed-region
  signal (verify's) is never matched or listed, and neither is the signal of a check that did not run this time.

`--only` takes check ids (store.CHECKS): only the run's signals of those checks are kept (a signal with a duplicate of
one counts), and every other signal of the capture stays as it was. `checks.ran` adds the sources that ran for them
(devkit, axe, impeccable); `checks.skipped` drops what ran now and keeps the newest reason for what did not.

Signals sit on the capture's picture: their element ids and boxes are its description's. So the run's signals are
kept only when the page lays out as that description says, every element with the same id, frame, selector, and
box. A page that lays out differently, or a capture whose description is not on this Mac, is refused and nothing
changes: `dev visual capture --again VC-…` takes the page as it is now, with signals of its own. A page whose pixels
differ under the same layout (a color, an image) is checked, and the command says so.

  check(project, rid, *, only=None, remote=False, cwd=None) -> [outcome]   one capture (VC-…), or an audit's (VA-…)
      outcome: {capture, result (checked, skipped, error), message, again, new, gone, not_run, looks_different,
                record}; a capture's refusal raises store.VisualError, an audit's captures each report theirs
  merge(stored, found, *, only=None, ran=()) -> (signals, {"again": [n], "new": [n], "gone": [n]})
  only_checks(text) -> the set of check ids --only names, or None
  cmd(a) -> the exit code of `dev visual check`
"""
from __future__ import annotations

import difflib
import gzip
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from . import store

FAMILIES = ("devkit", "axe", "impeccable")  # what runs the checks, in capture.cjs's order and checks.ran's names
CHANGED = "changed-region"  # verify's signal of changed pixels: not a check, so never found again


def only_checks(text):
    """The check ids `--only` names, comma-separated; None without --only. VisualError for a word that is not one."""
    if text is None:
        return None
    words = [w.strip() for w in str(text).split(",") if w.strip()]
    if not words:
        raise store.VisualError("Name the checks to run, comma-separated: --only overlap,axe")
    known = sorted(store.CHECKS)
    for w in words:
        if w not in store.CHECKS:
            near = difflib.get_close_matches(w, known, n=1, cutoff=0.6)
            raise store.VisualError(f"--only takes the ids of devkit's checks, comma-separated ({', '.join(known)}), "
                                    f"not \"{w}\"." + (f" Nearest: {near[0]}." if near else ""))
    return set(words)


# --------------------------------------------------------------------------- the same signal

def _box(value):
    if isinstance(value, (list, tuple)) and all(isinstance(v, (int, float)) for v in value):
        return tuple(round(v) for v in value)
    return None


def _where(anchor):
    a = anchor if isinstance(anchor, dict) else {}
    return a.get("element") or a.get("selector") or _box(a.get("region"))


def _key(s: dict, anchor=None) -> tuple:
    """What makes a signal the same signal on one capture: its check and rule, its element, and an overlap's other
    element; a signal on no element (a console error) by what it measured."""
    check = s.get("check")
    detail = s.get("axe_rule") if check == "axe" else (
        tuple(sorted(str(r) for r in s.get("rule") or ())) if check == "impeccable" else None)
    where = _where(anchor if anchor is not None else s.get("anchor"))
    other = s.get("other") if isinstance(s.get("other"), dict) else None
    second = (other.get("selector") or _box(other.get("region"))) if other else None
    return check, detail, where, second, None if where else str(s.get("measured"))


def _keys(s: dict) -> set:
    """The signal's own key and its folded duplicates', each on the signal's element."""
    anchor = s.get("anchor") if isinstance(s.get("anchor"), dict) else {}
    return {_key(s)} | {_key(d, anchor) for d in s.get("duplicates") or () if isinstance(d, dict)}


def _selected(s: dict, only) -> bool:
    if only is None:
        return True
    return s.get("check") in only or any(isinstance(d, dict) and d.get("check") in only
                                         for d in s.get("duplicates") or ())


def _numbered(s) -> bool:
    return isinstance(s.get("n"), int) and s.get("check") != CHANGED


def merge(stored, found, *, only=None, ran=()) -> tuple:
    """The capture's signals after a run: each of `found` (the run's, as capture.cjs numbered them; with --only, the
    ones `only` selects) found again among `stored` keeps that signal's number and triage, the others take the next
    numbers, and every stored signal not found again stays unchanged. -> (the signals ordered by number, {"again",
    "new", "gone"}: numbers; `gone` lists only signals of checks that ran (`ran`: the sources) and that --only
    selects)"""
    stored = [s for s in stored or () if isinstance(s, dict)]
    n = max([s["n"] for s in stored if isinstance(s.get("n"), int)] + [0])
    matched, out, counts = set(), [], {"again": [], "new": [], "gone": []}
    for f in found or ():
        if not isinstance(f, dict) or not _selected(f, only):
            continue
        keys = _keys(f)
        earlier = next((s for s in stored if _numbered(s) and s["n"] not in matched and _keys(s) & keys), None)
        if earlier:
            matched.add(earlier["n"])
            out.append(dict(f, n=earlier["n"], triage=earlier.get("triage")))
            counts["again"].append(earlier["n"])
        else:
            n += 1
            out.append(dict(f, n=n, triage=None))
            counts["new"].append(n)
    kept = [s for s in stored if s.get("n") not in matched]
    counts["gone"] = [s["n"] for s in kept if _numbered(s) and store._family(s.get("check")) in ran and
                      _selected(s, only)]
    return sorted(out + kept, key=lambda s: s["n"] if isinstance(s.get("n"), int) else 0), counts


# --------------------------------------------------------------------------- the run

def _wanted(key: str, only) -> bool:
    """Whether a source (devkit, axe, impeccable) or a check id of `checks.ran` or `checks.skipped` is part of this
    run: always without --only, else a check it names or the source of one."""
    if only is None or key in only:
        return True
    return key in FAMILIES and any(store._family(c) == key for c in only)


def _layout(desc) -> dict:
    return {e.get("id"): (e.get("frame"), e.get("selector"), _box(e.get("box")))
            for e in (desc or {}).get("elements") or () if isinstance(e, dict)}


def _moved(held, now) -> int:
    """How many elements lay out differently in the run than in the capture's description: moved or resized, with
    another selector or frame, new, or gone."""
    a, b = _layout(held), _layout(now)
    return sum(1 for k in a.keys() | b.keys() if a.get(k) != b.get(k))


def _probe(project: str, cid: str, remote: bool, cwd) -> tuple:
    """The capture's recipe run again with every check, into a folder deleted when it ends. -> (the driver's record,
    the run's description)"""
    folder = tempfile.mkdtemp(prefix="devkit-visual-check-")
    try:
        os.chmod(folder, 0o700)
        rec = store.capture_again(project, cid, remote=remote, cwd=cwd, probe=folder)[0]
        sha = str((rec.get("description") or {}).get("sha256") or "")
        try:
            if not re.fullmatch(r"[0-9a-f]{64}", sha):
                raise ValueError(sha)
            desc = json.loads(gzip.decompress((Path(folder) / "desc" / f"{sha}.json.gz").read_bytes()))
        except (OSError, ValueError, EOFError):
            raise store.VisualError(f"The run of {cid}'s recipe left no description to compare with its own. Nothing "
                                    "was changed.") from None
        return rec, desc
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _one(project: str, c: dict, only, remote: bool, cwd) -> dict:
    cid = c["id"]
    if c.get("kind") == "image" or c.get("recipe") is None:
        raise store.VisualError(f"{cid} is an external picture: it has no recipe to run the checks on. Capture the "
                                "screen with dev visual capture <url>, then check that capture.")
    held = store.description(project, c)
    if held is None:
        raise store.VisualError(f"The description of {cid} is not on this Mac, so devkit cannot tell whether the page "
                                "still lays out as its picture shows. Nothing was changed. Capture it again here, with "
                                f"its own signals: dev visual capture --again {cid}")
    rec, desc = _probe(project, cid, remote, cwd)
    moved = _moved(held, desc)
    if moved:
        raise store.VisualError(f"The page of {cid} lays out differently now: {moved} of its elements moved, changed, "
                                "or are new or gone, so this run's signals would not sit on its picture. Nothing was "
                                f"changed. Capture the page as it is now, with its own signals: dev visual capture "
                                f"--again {cid}")
    checks = rec.get("checks") or {}
    ran_now = [f for f in FAMILIES if f in (checks.get("ran") or ()) and _wanted(f, only)]
    skipped_now = {k: v for k, v in (checks.get("skipped") or {}).items() if _wanted(k, only)}
    commit = store._git(cwd)["commit"]
    counts = {}

    def fn(doc):
        had = doc.get("checks") or {}
        signals, got = merge(had.get("signals") or [], checks.get("signals") or [], only=only, ran=ran_now)
        ran = [f for f in FAMILIES if f in {*(had.get("ran") or ()), *ran_now}]
        skipped = {k: v for k, v in (had.get("skipped") or {}).items()
                   if k not in ran_now and not (k not in FAMILIES and store._family(k) in ran_now)}
        skipped.update(skipped_now)
        doc["checks"] = {**had, "ran": ran, "skipped": {k: v for k, v in skipped.items() if k not in ran},
                         "signals": signals}
        named = f" (only {', '.join(sorted(only))})" if only else ""
        doc.setdefault("history", []).append(
            {"at": store._now(), "by": store.who(), "commit": commit,
             "change": f"checks run again{named}: {len(got['again'])} found again, {len(got['new'])} new, "
                       f"{len(got['gone'])} not found"})
        counts.update(got)
    store._update(project, cid, fn)
    not_run = dict(skipped_now)
    for f in FAMILIES:
        if only is not None and _wanted(f, only) and f not in (checks.get("ran") or ()) and f not in not_run:
            not_run[f] = "not on a document page" if c.get("kind") == "document" else "did not run"
    looks = (rec.get("image") or {}).get("sha256") != (c.get("image") or {}).get("sha256")
    return {"capture": cid, "result": "checked", "message": None, **counts, "not_run": not_run,
            "looks_different": looks, "record": c}


def check(project: str, rid: str, *, only=None, remote=False, cwd=None) -> list:
    """`dev visual check`: the checks again on the recipe of a capture (VC-…), or of each capture of an audit and of
    the audits it includes (VA-…). -> one outcome per capture (the module docstring has the shape)."""
    rid = str(rid or "")
    if rid.startswith("VC-"):
        return [_one(project, store.load(project, rid), only, remote, cwd)]
    if not rid.startswith("VA-"):
        raise store.VisualError(f"dev visual check takes a capture (VC-…) or an audit (VA-…), not {rid}.")
    out = []
    for c in store.captures_of(project, rid):
        if c.get("kind") == "image" or c.get("recipe") is None:
            out.append({"capture": c["id"], "result": "skipped", "record": c,
                        "message": "an external picture: it has no recipe to run the checks on"})
            continue
        try:
            out.append(_one(project, c, only, remote, cwd))
        except store.VisualError as e:
            out.append({"capture": c["id"], "result": "error", "message": str(e), "record": c})
    return out


def _refs(numbers) -> str:
    return ", ".join(f"#{n}" for n in numbers)


def cmd(a) -> int:
    """`dev visual check` (commands.py): `a` is the parsed command line with `project_id`. -> 1 when a capture could
    not be checked."""
    from .. import ui
    from . import commands  # the command line's helpers; this module is imported by it, never the reverse
    only = only_checks(getattr(a, "only", None))
    out = check(a.project_id, a.record, only=only, remote=bool(getattr(a, "remote", False)))
    if not out:
        print(f"{a.record} has no captures yet, so there is nothing to check. Capture its screens: dev visual capture "
              f"<url> --audit {a.record}")
        return 0
    failed = new = 0
    for o in out:
        c = o["record"]
        head = f"{o['capture']}  {commands._label(c)} · {commands._size(c)}"
        if o["result"] == "error":
            failed += 1
            ui.err(f"{head}: {o['message']}")
            continue
        if o["result"] == "skipped":
            print(ui.dim(f"– {head} · {o['message']}"))
            continue
        new += len(o["new"])
        parts = [f"{len(o['again'])} found again", f"{len(o['new'])} new" + (f" ({_refs(o['new'])})" if o["new"] else "")]
        if o["gone"]:
            parts.append(f"{len(o['gone'])} not found this time ({_refs(o['gone'])})")
        print(f"{ui.OK} {head} · {' · '.join(parts)}")
        if o["gone"]:
            print(ui.dim("  a signal not found this time stays on the capture with its triage; dev visual verify "
                         "decides whether a finding's fix holds"))
        if o["not_run"]:
            print(ui.dim(f"  not run: {', '.join(f'{k} ({v})' for k, v in sorted(o['not_run'].items()))}"))
        if o["looks_different"]:
            ui.warn(f"The page's pixels differ from {o['capture']}'s picture, though it lays out the same: a signal "
                    f"measured now may not show in the picture. To see the page as it is now: dev visual capture "
                    f"--again {o['capture']}")
    if new:
        print(ui.dim(f"  next: triage the new signal{'s' if new != 1 else ''}: dev visual signal ls {a.record} "
                     "--untriaged"))
    return 1 if failed else 0
