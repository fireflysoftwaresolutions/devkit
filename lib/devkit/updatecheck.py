# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev update --check`: is a newer devkit release out, and what changes in it (spec P-fym583l7sg1 R7).

Read-only for what runs: it fetches tags (and the remote branches) into the checkout's git store, as `dev update`
does, and never changes the working tree, HEAD or the pinned copy. The daily job `update-check` records the result in
~/.local/state/devkit/update-check.json; the session start and `dev status` read that file and never reach the
network.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import core
from .core import tilde

CACHE = core.STATE / "update-check.json"
HEADING = re.compile(r"^## \[(\d+\.\d+\.\d+)\]")
SUB = re.compile(r"^### (.+?)\s*$")


def _v(text: str) -> tuple:
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", str(text or ""))
    return tuple(int(x) for x in m.groups()) if m else ()


def installed_version() -> str:
    """The release the hooks run: the pinned copy `dev install` recorded, else this copy's VERSION."""
    return str(core.install_record().get("version") or core.VERSION).split("-")[0]


def headings(changelog: str, newer_than: str) -> list:
    """[{"version", "headings"}] of the CHANGELOG versions newer than `newer_than`, newest first."""
    out, cur = [], None
    floor = _v(newer_than)
    for ln in changelog.splitlines():
        m = HEADING.match(ln)
        if m:
            cur = {"version": m.group(1), "headings": []} if _v(m.group(1)) > floor else None
            if cur:
                out.append(cur)
            continue
        m = SUB.match(ln)
        if m and cur is not None:
            cur["headings"].append(m.group(1))
    return sorted(out, key=lambda x: _v(x["version"]), reverse=True)


def read_cache(path: Path = None) -> dict:
    try:
        doc = json.loads(Path(path or CACHE).read_text())
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def record(res: dict, path: Path = None):
    """Write the result for the session start and `dev status`, keeping the day a session was last told."""
    from . import state
    keep = {k: res.get(k) for k in ("checked", "installed", "newest", "checkout", "behind", "releases",
                                    "pin_behind_checkout")}

    def merge(doc):  # a newer release than the one a session was told about is told again
        notified = doc.get("notified") if doc.get("newest") == keep["newest"] else None
        doc.clear()
        doc.update(keep, notified=notified)
    try:
        state.update(Path(path or CACHE), merge, indent=1)
    except state.StateBusy:
        pass


def check(home: Path, installed: str = None, cache: Path = None, fetch: bool = True) -> dict:
    """The newest release against the installed one and the checkout. On a failed fetch: the last recorded result
    with its date, and `error` set."""
    from .cli import newest_release
    installed = installed or installed_version()
    res = {"checked": int(time.time()), "installed": installed, "newest": None, "behind": 0, "releases": [],
           "checkout": {"path": str(home)}, "pin_behind_checkout": False, "error": ""}
    if fetch:
        rc, out, err = core.git(home, "fetch", "--quiet", "--tags", timeout=60)
        if rc != 0:
            last = read_cache(cache)
            res["error"] = (err or out or f"git fetch exit {rc}").strip().splitlines()[-1][:200]
            res["last"] = last or None
            return res
    branch = core.gitout(home, "symbolic-ref", "-q", "--short", "HEAD")
    co = res["checkout"]
    co["version"] = (Path(home) / "VERSION").read_text().strip() if (Path(home) / "VERSION").exists() else ""
    if branch:
        co["branch"] = branch
        n = core.gitout(home, "rev-list", "--count", "HEAD..@{u}")
        co["behind_commits"] = int(n) if n.isdigit() else None
    else:
        co["at"] = newest_release(core.gitout(home, "tag", "--points-at", "HEAD").split()) or \
            core.gitout(home, "rev-parse", "--short", "HEAD")
    newest = newest_release(core.gitout(home, "tag", "--list", "v*").split())
    res["newest"] = newest
    if newest and _v(newest) > _v(installed):
        rc, log, _ = core.git(home, "show", f"{newest}:CHANGELOG.md")
        rels = headings(log, installed) if rc == 0 else []
        tags = [t for t in core.gitout(home, "tag", "--list", "v*").split() if _v(t) > _v(installed)
                and newest_release([t]) == t]
        res["behind"] = len(tags)
        listed = {r["version"] for r in rels}
        res["releases"] = rels + [{"version": t[1:], "headings": []} for t in tags if t[1:] not in listed]
        res["releases"].sort(key=lambda r: _v(r["version"]), reverse=True)
    res["pin_behind_checkout"] = bool(co["version"]) and _v(co["version"]) > _v(installed)
    return res


def render(res: dict) -> list:
    pin = core.install_record().get("path") or ""
    if res.get("error"):
        lines = [f"could not be checked: {res['error']}"]
        last = res.get("last") or {}
        if last.get("newest"):
            lines.append(f"last check {core.fmt_ts(last.get('checked'))}: installed {last.get('installed')}, newest "
                         f"{last['newest']}" + (f", {last.get('behind')} release(s) behind" if last.get("behind")
                                                else ""))
        else:
            lines.append("no earlier check is recorded")
        return lines
    co = res.get("checkout") or {}
    where = tilde(co.get("path") or "")
    lines = [f"installed  {res['installed']}" + (f" (pinned copy {tilde(pin)})" if pin else "")]
    if co.get("branch"):
        b = co.get("behind_commits")
        lines.append(f"checkout   {where} on {co['branch']}" +
                     (f", {b} commits behind origin" if b else ", up to date with origin" if b == 0 else
                      ", no upstream branch"))
    else:
        lines.append(f"checkout   {where} at {co.get('at')} (detached)")
    if not res.get("newest"):
        lines.append("newest     no release tag found in the checkout")
        return lines
    n = res.get("behind") or 0
    both = "checkout and pinned copy" if not res.get("pin_behind_checkout") else "pinned copy"
    lines.append(f"newest     {res['newest']} · " + (f"{n} release{'s' if n != 1 else ''} behind ({both})" if n else
                                                       "up to date"))
    for r in res.get("releases") or []:
        lines.append(f"  {r['version']}: " + (" · ".join(r["headings"]) or "no CHANGELOG entry"))
    if res.get("pin_behind_checkout"):
        lines.append(f"the pinned copy ({res['installed']}) is older than the checkout ({co.get('version')}): "
                     f"run dev install")
    if n:
        lines.append("run in a terminal: dev update")
    return lines


def pending(doc: dict, installed: str = None) -> bool:
    return bool(doc.get("newest")) and _v(doc["newest"]) > _v(installed or doc.get("installed") or "")


def session_line(path: Path = None, today: str = None, installed: str = None):
    """The first session of the day gets one line while a newer release is pending; it marks the day. -> the line or
    None. Reads only the recorded result: no network."""
    from . import state
    f = Path(path or CACHE)
    doc = read_cache(f)
    if not pending(doc, installed):
        return None
    today = today or time.strftime("%Y-%m-%d")
    if doc.get("notified") == today:
        return None
    try:
        state.update(f, lambda d: d.update(notified=today), create=False, indent=1)
    except state.StateBusy:
        return None
    return (f"devkit {doc['newest']} is out (installed: {doc.get('installed')}); the developer runs `dev update` in "
            f"a terminal. `dev update --check` shows what changes.")


def status_line(path: Path = None, installed: str = None):
    """`dev status`: one line while a newer release is pending, or None."""
    doc = read_cache(path)
    if not pending(doc, installed):
        return None
    return f"devkit {doc['newest']} is out: run dev update in a terminal (dev update --check shows what changes)"


def cmd_check(a) -> int:
    from . import ui
    res = check(core.source_home())
    if a.record and not res.get("error"):
        record(res)
    if not a.quiet:
        for i, ln in enumerate(render(res)):
            print(ui.yellow(ln) if res.get("error") and i == 0 else ln)
    return 1 if res.get("error") else 0
