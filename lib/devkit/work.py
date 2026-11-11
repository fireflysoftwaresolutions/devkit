# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Work items, locks, history and presence — the multi-user, multi-machine layer of the handoff repo.

Layout (all inside the handoff repository, shared by every developer and machine):

  items/<scope>/<ID>.md          one file per task / feature / bug / spike — no merge conflicts between items
                                 scope = project id (owner/repo) or initiatives/<slug>
  sessions/<scope>/<YYYY-MM>.md  append-only session log (git merge=union)
  presence/<user>@<host>.json    who is working on what, written only by that machine
  plans/<scope>/<slug>.md        versioned plans and specs (frontmatter version + Changelog)

Every change records the actor (GitHub user @ machine, agent, session), a version bump and — for
changes of scope — the reason, both in the item's Changelog and in a git commit of its own.
"""
from __future__ import annotations

import calendar
import contextlib
import hashlib
import json
import os
import re
import socket
import time
from collections import Counter
from pathlib import Path

from . import core
from .core import HANDOFF

ITEMS = HANDOFF / "items"
SESSIONS = HANDOFF / "sessions"
PRESENCE = HANDOFF / "presence"
PLANS = HANDOFF / "plans"
TYPES = ("task", "feature", "bug", "spike")
STATUSES = ("todo", "doing", "blocked", "done", "cancelled")
BUCKETS = ("now", "next", "later")
PRIORITIES = ("high", "normal", "low")
NEEDS_WHY = {"edit", "cancel", "priority", "move", "reopen", "scope", "block", "link"}
OPEN_ONLY = {"move", "block", "cancel"}  # what changes an open item's place or state; a closed one is reopened first
FIELDS = ("id", "type", "title", "scope", "status", "bucket", "priority", "assignee", "claimed_by", "claimed_at",
          "lock_expires", "due", "links", "version", "created", "created_by", "updated", "updated_by")
LIST_FIELDS = ("links",)  # written as `[a, b]`; every other value is text, even one in brackets (a title like "[API] …")


# --------------------------------------------------------------------------- identity

def actor(agent: str = None, session: str = None, user: str = None) -> dict:
    user = user or core.github_user() or os.environ.get("USER", "unknown")
    host = socket.gethostname().split(".")[0].lower()
    agent = agent or os.environ.get("DEVKIT_AGENT") or ("claude" if os.environ.get("CLAUDECODE") else
                                                        "codex" if os.environ.get("CODEX_SANDBOX") or
                                                        os.environ.get("CODEX_THREAD_ID") else "human")
    sid = session or os.environ.get("DEVKIT_SESSION", "")
    sid = sid.split(":", 1)[1] if ":" in sid else sid  # DEVKIT_SESSION is "<agent>:<session id>"
    return {"user": user, "host": host, "agent": agent, "session": sid[:12]}


def actor_str(a: dict) -> str:
    return f"{a['user']}@{a['host']} ({a['agent']}{':' + a['session'] if a.get('session') else ''})"


def now_iso(ts=None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts or time.time()))


def parse_iso(s: str) -> float:
    """A `now_iso` text (UTC) -> seconds since the epoch, the same on any Mac in any season; 0.0 when unreadable."""
    try:
        return float(calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return 0.0


EPOCH = 1767225600  # 2026-01-01T00:00:00Z
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _b36(n: int) -> str:
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = _B36[r] + out
        if not n:
            return out


def new_id(kind: str = "task", scope: str = None) -> str:
    """T-<seconds since 2026, base36><6 random base36>: sortable, short, collision-free across machines."""
    import secrets
    p = {"task": "T", "feature": "F", "bug": "B", "spike": "S"}.get(kind, "T")
    while True:
        iid = f"{p}-{_b36(max(0, int(time.time()) - EPOCH))}{''.join(secrets.choice(_B36) for _ in range(6))}"
        if not scope or not item_path(scope, iid).exists():
            return iid


# --------------------------------------------------------------------------- item files

def scope_dir(scope: str) -> Path:
    return ITEMS / scope


def item_path(scope: str, iid: str) -> Path:
    return scope_dir(scope) / f"{iid}.md"


# --------------------------------------------------------------------------- record files (items, plans)
# A record is frontmatter, a body, and the record's own changelog: a `## Changelog` heading at the end with one
# `- ` line per entry. Bodies are free Markdown, so the same heading can appear in them (a code example, a section
# about a product's changelog): only the last one outside fenced code, followed by nothing but entries, is the
# record's. The frontmatter keeps the lines devkit does not know (another tool's keys, a person's notes) after its
# own fields. A write never drops text of the record on disk that it does not edit (`write_record`).

CHANGELOG = "## Changelog"
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")
_HEADING = re.compile(r"^ {0,3}#{1,6}(\s|$)")
_KEY = re.compile(r"^([A-Za-z_][\w.-]*)[ \t]*:(.*)$")


class RecordLossError(ValueError):
    """A write would drop text of the record on disk that it does not edit. `path`: the file; `lost`: those lines."""

    def __init__(self, path, lost):
        self.path, self.lost = Path(path), list(lost)
        super().__init__(
            f"refused to write {path}: it would drop {len(self.lost)} line(s) of the record that this change does not "
            f"edit, first “{self.lost[0][:100]}”. Nothing was written. If the file changed after devkit read it, run "
            f"the command again; otherwise devkit misread the record: leave the file as it is and report the bug with "
            f"it.")


def split_frontmatter(text: str):
    """-> (frontmatter text, the rest). Frontmatter runs from a first line `---` to the next `---` line."""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 3)
        if end != -1:
            return text[4:end], text[end + 5:]
    return "", text


def _key(line: str):
    m = _KEY.match(line)
    return m.group(1) if m else None


def read_frontmatter(front: str, fields=FIELDS, lists=LIST_FIELDS):
    """-> (meta, extra). `meta`: the value of each `key: value` line, as a list for the `lists` fields written as
    `[a, b]`; `extra`: every line that is not one of devkit's `fields`, as it is, for the render to put back."""
    meta, extra = {}, []
    for ln in front.splitlines():
        m = _KEY.match(ln)
        if m:
            k, v = m.group(1), m.group(2).strip()
            meta[k] = [x.strip() for x in v[1:-1].split(",") if x.strip()] \
                if k in lists and v.startswith("[") and v.endswith("]") else v
        if not m or m.group(1) not in fields:
            extra.append(ln)
    return meta, extra


def _closes(line: str, mark: str) -> bool:
    m = _FENCE.match(line)
    return bool(m) and m.group(1)[0] == mark[0] and len(m.group(1)) >= len(mark) and not m.group(2).strip()


def _fenced(lines: list) -> set:
    """Indexes of the lines in fenced code blocks (``` or ~~~, fence lines included). A fence closes on a line of the
    same character, at least as long, with nothing after it; a fence that never closes hides nothing."""
    inside, i = set(), 0
    while i < len(lines):
        m = _FENCE.match(lines[i])
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            end = next((j for j in range(i + 1, len(lines)) if _closes(lines[j], m.group(1))), None)
            if end is not None:
                inside.update(range(i, end + 1))
                i = end + 1
                continue
        i += 1
    return inside


def split_changelog(body: str):
    """-> (the body without the record's changelog, its entries). The record's changelog is the last `## Changelog`
    heading outside fenced code, when only entries follow it: `- ` lines, each with the lines under it that are not
    headings. Otherwise the record has no changelog yet and the whole body is text."""
    lines = body.split("\n")
    fenced = _fenced(lines)
    head = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].rstrip() == CHANGELOG and i not in fenced), None)
    if head is None:
        return body, []
    entries = []
    for ln in lines[head + 1:]:
        if ln.startswith("- "):
            entries.append(ln[2:].strip())
        elif entries and ln.strip() and not _HEADING.match(ln):
            entries[-1] += "\n" + ln.rstrip()
        elif ln.strip():
            return body, []
    return "\n".join(lines[:head]), entries


def _missing(old, new) -> list:
    """The non-blank lines of `old` that `new` does not have, spacing aside; each line of `new` counts once."""
    have = Counter(" ".join(ln.split()) for ln in new if ln.strip())
    lost = []
    for ln in old:
        n = " ".join(ln.split())
        if not n:
            continue
        if have[n] > 0:
            have[n] -= 1
        else:
            lost.append(n)
    return lost


def lost_lines(before: str, after: str, edited=()) -> list:
    """The lines of `before` that `after` no longer has, apart from the `edited` lines (the old text a change
    replaces). Spacing and line order are not compared. When `before` is a devkit record (its frontmatter has `id:`),
    its body is compared with the new body, and in its frontmatter the values are the record's to change: a `key:` line
    counts as kept while the new frontmatter has that key, and any other line must stay in the frontmatter. Any other
    file is compared whole, so another tool's frontmatter, or a first `---` rule and what follows it, cannot be lost."""
    fb, bb = split_frontmatter(before)
    if "id" not in {_key(ln) for ln in fb.splitlines()}:
        return _missing(before.splitlines(), after.splitlines() + list(edited))
    fa, ba = split_frontmatter(after)
    keys = {_key(ln) for ln in fa.splitlines()} - {None}
    return _missing([ln for ln in fb.splitlines() if _key(ln) not in keys], fa.splitlines()) + \
        _missing(bb.splitlines(), ba.splitlines() + list(edited))


def _replace(path: Path, text: str) -> None:
    """Write `text` to a new file in the same folder, then swap it in (core.replace_text): a reader sees the old
    record or the new one, never part of one."""
    core.replace_text(path, text)


RECORD_WAIT = 30  # seconds a write waits for another process's write of the same file on this Mac


class RecordBusyError(RecordLossError):
    """Another devkit process held the record's lock for RECORD_WAIT seconds: nothing was written."""

    def __init__(self, path, wait):
        self.path, self.lost = Path(path), []
        ValueError.__init__(self, f"refused to write {path}: another devkit process on this Mac kept it locked for "
                                  f"{wait:.0f} s. Nothing was written; run the command again.")


@contextlib.contextmanager
def record_lock(path, wait: float = RECORD_WAIT):
    """Hold this Mac's lock on a shared file (a record, the month's session log) across a read-modify-write: another
    devkit process that writes the same file waits, then reads what this one wrote. Reentrant; write_record and the
    session log writers take it themselves. Raises RecordBusyError after `wait` seconds."""
    with core.file_lock(path, wait) as got:
        if not got:
            raise RecordBusyError(path, wait)
        yield


def write_record(path: Path, text: str, edited=()) -> None:
    """Write a record file, but refuse (RecordLossError, the file unchanged) when the text would drop lines of the
    version on disk that this change does not edit: a misread record must stop the command, never shrink. The read,
    the check and the rename hold the record's lock (record_lock), so a write that another process made after this
    one's caller read the record is seen by the check and refused, never overwritten: a caller is never told a write
    succeeded that the record then lost. A caller that holds record_lock from its own read takes turns instead."""
    path = Path(path)
    with record_lock(path):
        try:
            before = path.read_text()
        except FileNotFoundError:
            before = ""
        lost = lost_lines(before, text, edited)
        if lost:
            raise RecordLossError(path, lost)
        _replace(path, text)


def parse(text: str) -> dict:
    front, body = split_frontmatter(text)
    meta, extra = read_frontmatter(front)
    title_m = re.search(r"^# (.+)$", body, re.M)
    desc, changelog = split_changelog(body)
    desc = re.sub(r"^# .+\n?", "", desc.strip(), count=1).strip()
    meta["version"] = int(meta.get("version") or 1)
    return {"meta": meta, "extra": extra, "title": meta.get("title") or (title_m.group(1) if title_m else ""),
            "body": desc, "changelog": changelog}


def render(item: dict) -> str:
    m = item["meta"]
    lines = ["---"]
    for k in FIELDS:
        v = m.get(k, "")
        if isinstance(v, list):
            v = "[" + ", ".join(v) + "]"
        lines.append(f"{k}: {v}".rstrip())
    lines += list(item.get("extra") or [])
    lines += ["---", f"# {m.get('title') or item.get('title')}", ""]
    if item.get("body"):
        lines += [item["body"].rstrip(), ""]
    lines += ["## Changelog"] + [f"- {c}" for c in item["changelog"]]
    return "\n".join(lines) + "\n"


_VERSION = re.compile(r"^v\d+ ")
_BY_RULE = ("version", "updated", "updated_by")


def _either(base, remote, mine):
    """(True, the value) when at most one side changed it, or both the same way; (False, None) otherwise."""
    if mine == base or mine == remote:
        return True, remote
    return (True, mine) if remote == base else (False, None)


def _as_list(v) -> list:
    return list(v) if isinstance(v, list) else [x.strip() for x in str(v or "").strip("[]").split(",") if x.strip()]


def merge_item(base: str, remote: str, mine: str):
    """This Mac's change to an item applied again to the remote's version, for a pull whose rebase stopped on the
    item: `base` is the version both sides started from, `remote` the one pushed first, `mine` this Mac's. A field,
    the title, the body and the frontmatter lines devkit does not know take the side that changed them; `links` keep
    what either side added and lose what either removed; this Mac's changelog entries go on top of the remote's,
    numbered after them, and the version counts both. -> the item's text, or None when both sides changed the same
    thing differently (a lock taken on both Macs, the body edited on both), when the changelogs do not both start
    from the base's, or when the result would drop a line either side added (`lost_lines`)."""
    b, r, m = parse(base), parse(remote), parse(mine)
    out = parse(remote)
    for k in FIELDS:
        if k in _BY_RULE:
            continue
        if k in LIST_FIELDS:
            bl, rl, ml = (_as_list(x["meta"].get(k)) for x in (b, r, m))
            out["meta"][k] = [x for x in rl if x in ml or x not in bl] + [x for x in ml if x not in bl + rl]
            continue
        ok, out["meta"][k] = _either(*(x["meta"].get(k, "") for x in (b, r, m)))
        if not ok:
            return None
    for part in ("body", "extra"):
        ok, out[part] = _either(b[part], r[part], m[part])
        if not ok:
            return None
    n = len(b["changelog"])
    if any(x["changelog"][len(x["changelog"]) - n:] != b["changelog"] for x in (r, m) if n):
        return None
    added = m["changelog"][:len(m["changelog"]) - n]
    version = r["meta"]["version"] + m["meta"]["version"] - b["meta"]["version"]
    out["changelog"] = [_VERSION.sub(f"v{version - i} ", e, count=1) for i, e in enumerate(added)] + r["changelog"]
    out["meta"]["version"] = version
    later = max((r, m), key=lambda x: x["meta"].get("updated") or "")
    out["meta"]["updated"], out["meta"]["updated_by"] = later["meta"].get("updated", ""), later["meta"].get("updated_by", "")
    text = render(out)
    renumbered = [ln for e in added for ln in f"- {e}".split("\n")]
    if lost_lines(remote, text, base.splitlines()) or lost_lines(mine, text, base.splitlines() + renumbered):
        return None
    return text


def load(path: Path) -> dict:
    recover_rebase("a read")  # a rebase left stopped leaves conflict markers in the items it stopped on
    it = parse(path.read_text())
    it["path"] = path
    return it


def all_items(scope: str = None) -> list:
    recover_rebase("a read")
    base = scope_dir(scope) if scope else ITEMS
    if not base.exists():
        return []
    out = []
    for f in sorted(base.rglob("*.md")):
        try:
            it = load(f)
        except OSError:
            continue
        it["scope"] = it["meta"].get("scope") or str(f.parent.relative_to(ITEMS))
        out.append(it)
    return out


def find(selector: str, scope: str = None) -> dict:
    """By id (exact or unique prefix), by number in the list `dev task` showed (`by_number`), or by a unique part of
    the title."""
    items = [i for i in all_items(scope) if i["meta"].get("status") not in ("done", "cancelled")] if not \
        selector.upper().startswith(("T-", "F-", "B-", "S-")) else all_items(scope)
    sel = selector.strip()
    exact = [i for i in items if i["meta"]["id"].lower() == sel.lower()]
    if exact:
        return exact[0]
    by_id = [i for i in items if i["meta"]["id"].lower().startswith(sel.lower())]
    if by_id and sel.upper()[:2] in ("T-", "F-", "B-", "S-"):
        if len(by_id) == 1:
            return by_id[0]
        raise ValueError(f"“{sel}” matches {len(by_id)} items")
    if sel.isdigit():
        return by_number(int(sel), scope, items)
    hits = [i for i in items if sel.lower() in i["meta"].get("title", "").lower()]
    if len(hits) == 1:
        return hits[0]
    raise ValueError(f"“{sel}” matches {len(hits)} items; use the id from `dev task`" if hits else f"no item matches “{sel}”")


def ordered_open(items: list) -> list:
    order = {"now": 0, "next": 1, "later": 2}
    pr = {"high": 0, "normal": 1, "low": 2}
    open_ = [i for i in items if i["meta"].get("status") not in ("done", "cancelled")]
    return sorted(open_, key=lambda i: (order.get(i["meta"].get("bucket"), 3), pr.get(i["meta"].get("priority"), 1),
                                        i["meta"].get("created", "")))


# --------------------------------------------------------------------------- numbers and the Now limit
# `dev task` numbers the open items, and `dev task <verb> <n>` takes such a number. The list moves under it: a close,
# an add or a move renumbers every item after it, so `done 9` and then `done 40` once closed the item that had moved up
# to #40. A number therefore means the item at that place in the list `dev task` last showed this viewer (_viewer: the
# Codex thread, else the agent session and the crew lane in it, else the shell that ran `dev`; a command the call
# ledger cannot tie to one caller gets no numbers), and it is refused once the open items are no longer that list in
# that order, or the list is more than LIST_TTL old. The refusal names the id #n had, and has now, so the caller acts
# by id. Ids always work. Each viewer's lists live in STATE/task-lists/<viewer>-<hash>.json (_lists_file): {viewer,
# lists: {scope: {ids, shown_at}}}, on this Mac only.

LIST_TTL = 30 * 60  # seconds a shown list numbers items
NOW_LIMIT = 5  # open items in a project's Now above which devkit warns


def _viewer() -> tuple:
    """Who reads the numbers -> (the key of their record, '') or ('', why the command has none):
    codex:<thread>             a Codex thread (CODEX_THREAD_ID), also one started from a Claude Code shell, whose
                               DEVKIT_SESSION it inherits
    <agent>:<session>[:<lane>] the agent session that runs the command (progress.resolve_session) and who runs it:
                               the crew lane DEVKIT_FROM_AGENT names, else the caller whose open call the ledger
                               matches to this command (no lane: the tech lead). Every shell of a Claude Code
                               session, its lanes' too, has one DEVKIT_SESSION, so only the ledger tells them apart.
    shell-<parent pid>         no session of its own: a developer's shell (a recent session of the folder is a guess)
    no key                     the ledger cannot say whose command this is: more than one open call of the session
                               names it, or none does (a loop over a variable, a script, a call open past its time
                               limit, a lost ledger write, the developer's own `! dev task`). Taking the tech lead's
                               list then would let a lane close the item at the tech lead's #n, so: no numbers"""
    from . import progress
    thread = os.environ.get("CODEX_THREAD_ID", "")
    if thread:
        return f"codex:{thread}", ""
    lane = os.environ.get("DEVKIT_FROM_AGENT") or ""
    if lane:
        agent, sid, how = progress.resolve_session()
        calls, unmatched = [], False
    else:
        who = progress.caller()
        agent, sid, how, calls = who["agent"], who["session"], who["how"], who["calls"]
        unmatched = who["caller"] == "unknown"
        lane = "" if who["caller"] in progress.NOT_A_LANE else who["caller"]
    if how in ("recency", "shell"):
        return f"shell-{os.getppid()}", ""
    if len(calls) > 1:
        return "", "another open call of this session runs the same command"
    if unmatched:
        return "", ("no open call of this session holds this command as typed (a loop over a variable, a script, a "
                    "call open past its time limit)")
    return f"{agent}:{sid}" + (f":{lane}" if lane else ""), ""


def _lists_file(viewer: str) -> Path:
    """The record of `viewer`'s lists: the key written for a file name and cut to 63 characters, then a hash of the
    whole key, so two viewers never share one, also when their keys differ only in characters a name cannot hold
    (`a:b` and `a_b` both read a_b). A record named before every name had the hash (claude_<session>.json) is not
    read: the first `dev task` after the update records the list afresh, and _record_shown removes the old file once
    it is a day old."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", viewer).strip("._")[:63] or "shell"
    return core.STATE / "task-lists" / f"{name}-{hashlib.sha256(viewer.encode()).hexdigest()[:16]}.json"


def _fresh(shown_at: float, now: float) -> bool:
    """A list shown less than LIST_TTL ago numbers items. One shown at a time still to come (this Mac's clock was set
    back, or the record was edited) does not: it would never expire."""
    return 0 <= now - shown_at < LIST_TTL


def _when(shown_at: float) -> str:
    """When a list was shown, by this Mac's clock: the time, with the day when that was not today."""
    at = time.localtime(shown_at)
    same_day = time.strftime("%Y-%m-%d", at) == time.strftime("%Y-%m-%d", time.localtime(time.time()))
    return time.strftime("%H:%M" if same_day else "%Y-%m-%d %H:%M", at)


def _shown_lists(viewer: str) -> dict:
    try:
        data = json.loads(_lists_file(viewer).read_text())
    except (OSError, ValueError):
        return {}
    lists = data.get("lists") if isinstance(data, dict) else None
    return lists if isinstance(lists, dict) else {}


def _record_shown(viewer: str, scope: str, ids: list, now: float) -> bool:
    """Record `ids` as the list of `scope` this viewer was shown; drop the viewer's expired lists and the files of
    viewers gone for a day. -> whether it was recorded (unrecorded numbers are refused, so print none)."""
    if not viewer:
        return False
    f = _lists_file(viewer)
    lists = {s: x for s, x in _shown_lists(viewer).items()
             if isinstance(x, dict) and _fresh(parse_iso(x.get("shown_at", "")), now)}
    lists[scope or ""] = {"ids": ids, "shown_at": now_iso(now)}
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        _replace(f, json.dumps({"viewer": viewer, "lists": lists}, indent=1, ensure_ascii=False) + "\n")
    except OSError:
        return False
    for old in f.parent.glob("*.json"):
        with contextlib.suppress(OSError):
            if old != f and now - old.stat().st_mtime > 86400:
                old.unlink()
    return True


def show(scope: str, items: list = None, rebind: bool = True) -> tuple:
    """-> (the open items of `scope` in `dev task` order, whether to print their numbers). rebind (the `dev task`
    list): this list is the one this viewer's numbers point into from now on. rebind=False (the list printed after a
    write): numbers keep pointing into the list shown before, so this one is numbered only while it is still that list
    (which then counts as shown again); after a close, an add or a move it is printed without numbers."""
    ordered = ordered_open(all_items(scope) if items is None else items)
    ids = [i["meta"]["id"] for i in ordered]
    now, (viewer, _) = time.time(), _viewer()
    if not rebind:
        shown = _shown_lists(viewer).get(scope or "") if viewer else None
        if not isinstance(shown, dict) or shown.get("ids") != ids or \
                not _fresh(parse_iso(shown.get("shown_at", "")), now):
            return ordered, False
    return ordered, _record_shown(viewer, scope, ids, now)


def by_number(n: int, scope: str, items: list = None) -> dict:
    """The open item at #n of the list `dev task` showed this viewer, while the open items are still that list in that
    order and it was shown less than LIST_TTL ago. Otherwise a ValueError that names the id #n had, and has now."""
    current = ordered_open(all_items(scope) if items is None else items)
    ids = [i["meta"]["id"] for i in current]
    where = scope or "all projects"
    viewer, why = _viewer()
    if not viewer:
        raise ValueError(f"#{n}: {why}, so devkit cannot tell whose list the number points into: use the item's id "
                         f"(T-…, F-…, B-…, S-…)")
    shown = _shown_lists(viewer).get(scope or "")
    then = shown.get("ids") if isinstance(shown, dict) else None
    if not isinstance(then, list) or not all(isinstance(x, str) for x in then):  # none, or a damaged record
        raise ValueError(f"#{n}: no list of {where} was shown here, so the number points at nothing: run dev task and "
                         f"use its numbers, or use the item's id (T-…, F-…, B-…, S-…)")
    shown_at, now = parse_iso(shown.get("shown_at", "")), time.time()
    at = _when(shown_at)
    if not 1 <= n <= len(then):
        raise ValueError(f"no #{n} in the list of {where} shown at {at} ({len(then)} open items): run dev task to "
                         f"number the list again, or use an id")
    was = then[n - 1]
    if _fresh(shown_at, now) and then == ids:
        return current[n - 1]
    titles = {i["meta"]["id"]: i["meta"].get("title", "") for i in all_items(scope)}
    name = lambda iid: f"{iid} “{titles.get(iid, '')}”"  # noqa: E731
    if not _fresh(shown_at, now):
        age = f"more than {LIST_TTL // 60} minutes ago" if now >= shown_at else "a time the clock has not reached"
        raise ValueError(f"#{n} was {name(was)} in the list of {where} shown at {at}, {age}: use its id, or run "
                         f"dev task to number the list again")
    closed, added = len(set(then) - set(ids)), len(set(ids) - set(then))
    change = ", ".join(x for x in (f"{closed} closed" if closed else "", f"{added} added" if added else "") if x) \
        or "the order changed"
    now_is = "" if n > len(ids) else " and still is" if ids[n - 1] == was else f" and is {name(ids[n - 1])} now"
    raise ValueError(f"the open items of {where} changed since the list was shown at {at} ({change}): #{n} was "
                     f"{name(was)} then{now_is}. Use the id you mean, or run dev task to number the list again")


def acted(verb: str, it: dict) -> str:
    """The line a write prints: what it did to which item, by id and title (`done B-… “Fix login”`)."""
    return f"{verb} {it['meta']['id']} “{it['meta'].get('title', '')}”"


def now_warning(scope: str, items: list = None) -> str:
    """'' while the open items in `scope`'s Now are at most NOW_LIMIT, otherwise one line that says how many and how
    many are being worked on. Only a warning: nothing is refused for it."""
    items = all_items(scope) if items is None else items
    now = [i for i in items if i["meta"].get("bucket") == "now" and i["meta"].get("status") in OPEN]
    if len(now) <= NOW_LIMIT:
        return ""
    doing = sum(1 for i in now if i["meta"].get("status") == "doing")
    return (f"{len(now)} items in Now in {scope}, {doing} being worked on: more than {NOW_LIMIT} hides what comes "
            f"first; move what waits with dev task move <id> --to next --why \"…\"")


# --------------------------------------------------------------------------- git (audit trail)
# Every Mac pulls the handoff repository with a rebase. A rebase that stops (a conflict, a killed git) leaves the
# repository on a detached HEAD: a commit made there is lost when the rebase ends, and every later pull and push
# fails. So devkit's git writes to the repository run one at a time on a Mac (`_repo_lock`), a conflict in an item
# is settled item by item (`merge_item`), a conflict in a project's note or an initiative's README file by file
# (`_settle_note`), any other conflict aborts the rebase, and a rebase found stopped is aborted before the next pull
# or commit. Each abort is a sync problem: an audit record, kept at every audit level, and handoff-push-failed.json,
# which `dev status` and the dashboard show until a push succeeds. A rebase rewrites notes in the working tree, so it
# also holds the notes lock that every note writer takes, before _repo_lock as every process takes them
# (`_rebase_locks`); the fetch and the push, which can take a minute, hold _repo_lock alone, so a note writer never
# waits for the network.

_REBASE_DIRS = ("rebase-merge", "rebase-apply")
_BUSY = "another devkit process kept the handoff repository busy"
_held = [0]


@contextlib.contextmanager
def _repo_lock(wait: float = 90):
    """Run devkit's pulls, rebases, commits and pushes of the handoff repository one at a time on this Mac. Nested
    uses share the outer one. -> whether it is held (False after `wait` seconds: another devkit process is busy)."""
    if _held[0]:
        _held[0] += 1
        try:
            yield True
        finally:
            _held[0] -= 1
        return
    with core.Lock("handoff-git", wait=wait) as got:
        _held[0] += 1 if got else 0
        try:
            yield got
        finally:
            _held[0] -= 1 if got else 0


@contextlib.contextmanager
def _rebase_locks(wait: float = 90):
    """The locks a rebase of the handoff repository holds: the notes lock (registry.notes_lock), then _repo_lock, the
    order every devkit process takes them in. While git and the settling of conflicts rewrite notes, some with
    conflict markers, a note writer (dev note set, an embargo, dev sync's own) waits: it never reads a note mid-rebase,
    nor writes one between the settling and its `git add`. Only local git work runs under them. Never enter it while
    holding _repo_lock. -> whether both are held."""
    from . import registry
    with registry.notes_lock(wait) as notes:
        if not notes:
            yield False
            return
        with _repo_lock(wait) as got:
            yield got


def push_failed_file() -> Path:
    return core.STATE / "handoff-push-failed.json"


def sync_problem(problem: str, action: str = "handoff.push", target: str = "") -> None:
    """Record a sync of the handoff repository that did not reach the remote: an audit record that every audit level
    keeps (`target`: the files involved), and handoff-push-failed.json, shown as a problem until a push succeeds."""
    from . import audit
    audit.record(action, target or core.handoff_repo() or "handoff", problem[:300], outcome="blocked")
    try:
        f = push_failed_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"ts": int(time.time()), "error": problem[:300]}))
    except OSError:
        pass


def _stopped() -> bool:
    return any((HANDOFF / ".git" / d).exists() for d in _REBASE_DIRS)


def _conflicted() -> list:
    return core.gitout(HANDOFF, "diff", "--name-only", "--diff-filter=U").splitlines()


def _abort(reason: str, files=(), keep=()) -> bool:
    """Abort the rebase and record why. `keep`: files this process wrote after the rebase stopped, written back
    after the abort (it restores the files of the commits it puts back). -> whether git aborted it."""
    saved = {}
    for p in keep:
        try:
            saved[Path(p)] = Path(p).read_text()
        except OSError:
            pass
    rc, _, err = core.git(HANDOFF, "rebase", "--abort", timeout=60)
    for p, text in saved.items():
        try:
            _replace(p, text)
        except OSError:
            pass
    problem = reason + (f"; git rebase --abort failed: {err.strip()[:120]}" if rc != 0 else "")
    sync_problem(problem, "handoff.rebase-aborted", ", ".join(files)[:300])
    from . import ui
    ui.warn(f"handoff repository: {problem}. See “The handoff repository stopped syncing” in docs/teams.md.")
    return rc == 0


def recover_rebase(where: str, keep=()) -> bool:
    """A rebase of the handoff repository left stopped, with no devkit process working on it: abort it (its commits
    come back as they were) and record it as a sync problem. `where`: what found it (`dev sync`, a commit). Runs
    before every pull and commit. -> True when it aborted one."""
    if not _stopped():
        return False
    with _repo_lock(wait=0) as got:  # busy: another devkit process is rebasing right now, so it is not stopped
        if not got or not _stopped():
            return False
        files = _conflicted()
        return _abort(f"a rebase of the handoff repository was left stopped"
                      + (f" on a conflict in {', '.join(files)}" if files else "") + f"; {where} aborted it",
                      files, keep)


def _stage(rel: str, n: int):
    rc, out, _ = core.git(HANDOFF, "show", f":{n}:{rel}")
    return out + "\n" if rc == 0 else None


# The append-only logs: the month's activity log (log/) and the session logs (sessions/). Every Mac only adds lines at
# the end, so a conflict in one keeps both sides' lines, as their merge=union rule in .gitattributes does. Git applies
# that rule only from the commit it rebases onto: a Mac whose own unpushed commit brings the rule would stop on every
# pull until another Mac pushed it.
_APPEND_ONLY = re.compile(r"^(log/[^/]+\.md|sessions/.+)$")


def _settle(rel: str, retake: bool):
    """The text of a file in conflict during the rebase, or None when devkit cannot settle it. An append-only log: both
    sides' lines, the remote's first. An item: the remote's version with this Mac's change applied again
    (`merge_item`); when both changed the same thing, the remote's version if `retake` (the caller applies its change
    again), else None."""
    base, remote, mine = _stage(rel, 1), _stage(rel, 2), _stage(rel, 3)  # in a rebase, 2 is the remote's side
    if _APPEND_ONLY.match(rel):  # a side that deleted the log loses to the one that added lines
        return _merge_text(base or "", remote, mine, union=True) if remote and mine else remote or mine
    if not (rel.startswith("items/") and remote and mine):
        return None
    merged = merge_item(base, remote, mine) if base else None
    return merged if merged is not None else remote if retake else None


# A project's note (projects/<id>.md) and an initiative's README hold blocks devkit writes again on every sync (the
# auto block: this Mac's checkout, branches and PRs; the tasks block: the work items) around text people write. Two
# Macs' blocks always differ, so a conflict in them is never merged line by line: the hand-written text is merged
# (three-way, with the blocks emptied), the blocks are taken whole from this Mac, and after the pull the tasks block
# is written again from the work items both Macs now have (`_rewrite_tasks`); the next `dev sync` writes the auto
# block from this Mac's checkout. A conflict in the hand-written text keeps the remote's note, so the rest of the
# pull goes on, and saves this Mac's version with the command that brings it back (`_save_note_conflict`).
_NOTE_FILE = re.compile(r"^(projects/.+\.md|initiatives/[^/]+/README\.md)$")


def _generated_blocks() -> tuple:
    from .registry import AUTO_END, AUTO_START, TASKS_END, TASKS_START
    return (AUTO_START, AUTO_END), (TASKS_START, TASKS_END)


def _hollow(text: str) -> tuple:
    """-> (the note with each generated block emptied to its two marker lines, {start marker: the whole block})."""
    blocks = {}
    for start, end in _generated_blocks():
        i = text.find(start)
        j = text.find(end, i + len(start)) if i != -1 else -1
        if j == -1:
            continue
        blocks[start] = text[i:j + len(end)]
        text = text[:i] + start + "\n" + end + text[j + len(end):]
    return text, blocks


def _merge_text(base: str, remote: str, mine: str, union: bool = False):
    """Three-way merge of two versions of a text with the one both started from (git merge-file). -> the merged text,
    or None when both changed the same lines; with `union`, both sides' lines there instead (the remote's first)."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="devkit-merge-") as d:
        paths = []
        for name, text in (("remote", remote), ("base", base), ("mine", mine)):
            p = Path(d) / name
            p.write_text(text)
            paths.append(str(p))
        rc, out, _ = core.sh(["git", "merge-file", "-p", "-q", *(["--union"] if union else []), *paths])
    return out + "\n" if rc == 0 else None


def _settle_note(rel: str, set_aside: dict = None) -> tuple:
    """A note in conflict during the rebase -> (the text to keep, the problem when this Mac's hand-written change
    could not be merged and was saved aside, else ""). See _NOTE_FILE; `set_aside`: see _save_note_conflict."""
    base, remote, mine = _stage(rel, 1), _stage(rel, 2), _stage(rel, 3)  # in a rebase, 2 is the remote's side
    if remote is None or mine is None:  # one side deleted the note: keep the text the other side has
        return remote or mine or "", ""
    (hb, _), (hr, remote_blocks), (hm, my_blocks) = _hollow(base or ""), _hollow(remote), _hollow(mine)
    merged = _merge_text(hb, hr, hm)
    if merged is None:
        return remote, _save_note_conflict(rel, base or "", mine, set_aside)
    for start, end in _generated_blocks():
        block = my_blocks.get(start) or remote_blocks.get(start)
        if block:
            merged = merged.replace(start + "\n" + end, block, 1)
    return merged, ""


def _save_note_conflict(rel: str, base: str, mine: str, set_aside: dict = None) -> str:
    """Save this Mac's version of a note whose hand-written text conflicts, and the version both sides started from,
    in STATE/handoff-conflicts/; record it (audit, kept at every level) and say how to bring it back. -> the problem.
    `set_aside`: the notes this pull saved already ({rel: (the saved copy, the problem)}). When a later commit of this
    Mac conflicts on the same note in the same pull, only the saved copy changes, to this Mac's latest version: the
    copy then holds every local commit's change against the version the first one started from, so the command the
    first warning printed brings them all back. A copy never replaces another pull's: a second one saved in the same
    second takes a counter after the time."""
    import shlex
    set_aside = {} if set_aside is None else set_aside
    if rel in set_aside:
        saved, problem = set_aside[rel]
        core.replace_text(saved, mine)
        return problem
    stem = f"{rel[:-len('.md')]}.{time.strftime('%Y%m%d-%H%M%S')}"
    saved, n = core.STATE / "handoff-conflicts" / f"{stem}.mine.md", 1
    while saved.exists():
        n += 1
        saved = saved.with_name(f"{Path(stem).name}-{n}.mine.md")
    started = saved.with_name(saved.name[:-len(".mine.md")] + ".base.md")
    saved.parent.mkdir(parents=True, exist_ok=True)
    core.replace_text(saved, mine)
    core.replace_text(started, base)
    cmd = " ".join(shlex.quote(str(w)) for w in ("git", "-C", HANDOFF, "merge-file", rel, started, saved))
    problem = (f"{rel}: its hand-written text changed on this Mac and on another one at the same place, so devkit kept "
               f"the remote's version and saved this Mac's at {saved}; to bring your changes back, run `{cmd}`, keep "
               f"the lines both sides need between the conflict markers, then run `dev sync`")
    from . import audit, ui
    audit.record("handoff.note-conflict", rel, problem[:300], outcome="blocked")
    ui.warn(f"handoff repository: {problem}")
    set_aside[rel] = (saved, problem)
    return problem


def _rewrite_tasks(rels) -> None:
    """After a pull that settled the generated blocks of these notes, write each one's tasks block again from the work
    items as they are now, and commit what changed: the note lists the items both Macs added. It runs within the
    rebase, under the notes lock that every note writer takes (_rebase_locks), so no writer changes a note between
    this read and this write."""
    if not rels:
        return
    from .registry import TASKS_END, TASKS_START, render_tasks_block
    changed = []
    for rel in dict.fromkeys(rels):
        f = HANDOFF / rel
        try:
            text = f.read_text()
        except OSError:
            continue
        if TASKS_START not in text or TASKS_END not in text.split(TASKS_START, 1)[1]:
            continue
        scope = rel[len("projects/"):-len(".md")] if rel.startswith("projects/") else rel.rsplit("/", 1)[0]
        pre, rest = text.split(TASKS_START, 1)
        new = pre + render_tasks_block(scope) + rest.split(TASKS_END, 1)[1]
        if new != text:
            _replace(f, new)
            changed.append(f)
    if changed:
        _commit(changed, f"sync: the tasks block of {len(changed)} note(s) again after the pull", push=False)


def fetch_handoff() -> tuple:
    """Fetch the remote's commits of the handoff repository (the network), under _repo_lock alone.
    -> (ok, the problem when not ok)."""
    with _repo_lock() as got:
        if not got:
            return False, _BUSY
        rc, out, err = core.git(HANDOFF, "fetch", "-q", timeout=60)
        return rc == 0, ("" if rc == 0 else (err or out).strip()[:300] or "git fetch failed")


def pull_rebase(retake=()) -> tuple:
    """Pull the handoff repository: fetch, then rebase this Mac's commits on the remote's (rebase_fetched). Never call
    it while holding _repo_lock (the rebase takes the notes lock first). -> (ok, the problem when not ok)."""
    ok, problem = fetch_handoff()
    return rebase_fetched(retake) if ok else (False, problem)


def rebase_fetched(retake=()) -> tuple:
    """Rebase this Mac's commits on the remote's as last fetched: local git work only, under _rebase_locks, and nothing
    to do (no lock taken) when the remote has no new commit or the branch follows none. A conflict in an item or an
    append-only log is settled (`_settle`), one in a note file by file (`_settle_note`: never a stop, so the other
    files still go through); any other conflict aborts the rebase, leaves the commits as they were and records a sync
    problem.
    `retake`: item paths (relative) whose change the caller applies again itself. Never call it while holding
    _repo_lock. -> (ok, the problem when not ok)."""
    if core.gitout(HANDOFF, "rev-list", "--count", "HEAD..@{u}") in ("", "0"):
        return True, ""
    with _rebase_locks() as got:
        if not got:
            return False, "another devkit process kept the handoff notes or repository busy"
        recover_rebase("a pull")
        return _rebase(retake)


def _rebase(retake) -> tuple:
    """rebase_fetched's work, under _rebase_locks."""
    rc, out, err = core.git(HANDOFF, "rebase", "-q", "--autostash", timeout=60)
    if rc == 0:
        return True, ""
    if not _stopped():  # git refused to start it: nothing changed here
        return False, (err or out).strip()[:300] or "git rebase failed"
    settled_notes, set_aside = [], {}
    for _ in range(200):  # one round per commit of this Mac that conflicts
        files = _conflicted()
        if not files:
            break
        for rel in files:
            if _NOTE_FILE.match(rel):
                text, problem = _settle_note(rel, set_aside)
                if not problem:
                    settled_notes.append(rel)
            else:
                text = _settle(rel, rel in retake)
            if text is None:
                problem = f"a pull of the handoff repository stopped on a conflict in {', '.join(files)} that devkit " \
                          f"cannot settle; it aborted the rebase and kept this Mac's commits unpushed"
                _abort(problem, files)
                return False, problem
            _replace(HANDOFF / rel, text)
            core.git(HANDOFF, "add", "--", rel)
        nothing = core.git(HANDOFF, "diff", "--cached", "--quiet")[0] == 0
        core.git(HANDOFF, "-c", "core.editor=true", "rebase", "--skip" if nothing else "--continue", timeout=60)
        if not _stopped():
            _rewrite_tasks(settled_notes)
            return True, ""
    problem = "a pull of the handoff repository stopped and did not finish; devkit aborted the rebase"
    _abort(problem, _conflicted())
    return False, problem


def _pushes() -> bool:
    """Does this Mac push the handoff repository (a git repository with a remote, and sync.push on)?"""
    return (HANDOFF / ".git").exists() and bool(core.gitout(HANDOFF, "remote")) and \
        core.CFG.get("sync", {}).get("push", True)


def push_now(retake=()) -> tuple:
    """Pull (rebase), then push, now: the fetch and the push under _repo_lock alone, the rebase under the notes lock
    too (pull_rebase). -> (ok, the problem when not ok)."""
    with _repo_lock() as got:
        if not got:
            return False, _BUSY
        recover_rebase("a push")
    ok, problem = pull_rebase(retake)
    return push_handoff() if ok else (False, problem)


def push_handoff(timeout: int = 60) -> tuple:
    """Push the handoff repository's commits (the network), under _repo_lock alone. -> (ok, the problem when not ok)."""
    with _repo_lock() as got:
        if not got:
            return False, _BUSY
        rc, _, err = core.git(HANDOFF, "push", "-q", timeout=timeout)
        return rc == 0, (err.strip()[:300] if rc else "")


def _commit(paths, message: str, push: bool = True, wait: bool = False):
    """One commit per change (the audit trail); push in the background unless `wait` (push now)."""
    if not (HANDOFF / ".git").exists():
        return True
    rel = [str(Path(p).relative_to(HANDOFF)) for p in paths]
    with _repo_lock() as got:
        if got:
            recover_rebase("a commit", keep=paths)
        core.git(HANDOFF, "add", "--", *rel)
        rc, _, _ = core.git(HANDOFF, "diff", "--cached", "--quiet")
        if rc != 0:
            core.git(HANDOFF, "commit", "-q", "-m", message, "--", *rel)
            from . import audit
            audit.record("handoff." + (rel[0].split("/")[0] if rel else "write"), ", ".join(rel)[:300], message)
    if not push or not _pushes():
        return True
    if os.environ.get("DEVKIT_NO_AUTOSYNC") and not wait:  # bulk operations push once at the end
        return True
    if wait:
        return push_now()[0]
    request_sync(message[:80], delay=10)
    return True


SYNC_PENDING = core.STATE / "sync.pending"


def request_sync(reason: str = "", delay: int = 15):
    """Near-real-time handoff sync: one queued background run; events inside the delay share it."""
    if os.environ.get("DEVKIT_NO_AUTOSYNC") or os.environ.get("DEVKIT_JOB") == "sync":
        return False
    now = time.time()
    try:
        SYNC_PENDING.parent.mkdir(parents=True, exist_ok=True)
        if SYNC_PENDING.exists() and now - SYNC_PENDING.stat().st_mtime < 120:
            with SYNC_PENDING.open("a") as fh:  # a run is already queued: it will carry this change too
                fh.write(reason[:120] + "\n")
            return False
        SYNC_PENDING.write_text(reason[:120] + "\n")
    except OSError:
        pass
    import subprocess
    import sys
    log = core.STATE / "logs" / "sync.log"
    try:
        out = open(log, "a")
    except OSError:
        out = subprocess.DEVNULL
    try:
        subprocess.Popen([sys.executable, "-I", "-S", str(core.release_home() / "bin" / "dev"), "sync", "--quiet",
                          "--offline", "--delay", str(delay)], stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         start_new_session=True, close_fds=True)  # isolated, as its shebang runs it
    finally:
        if out is not subprocess.DEVNULL:
            out.close()  # the child holds its own copy of the log
    return True


def pull(retake=()) -> bool:
    """Bring the remote's changes to this Mac (`pull_rebase`), after aborting a rebase left stopped. -> whether the
    repository is now up to date with the remote (True when it has none)."""
    if not (HANDOFF / ".git").exists():
        return True
    with _repo_lock() as got:
        if not got:
            return False
        recover_rebase("a pull")
        if not core.gitout(HANDOFF, "remote"):
            return True
    return pull_rebase(retake)[0]  # after _repo_lock is released: the rebase takes the notes lock first


# --------------------------------------------------------------------------- operations

def _touch(it: dict, a: dict, op: str, why: str = "", detail: str = ""):
    m = it["meta"]
    m["version"] = int(m.get("version") or 1) + (0 if op == "create" else 1)
    m["updated"], m["updated_by"] = now_iso(), f"{a['user']}@{a['host']}"
    entry = f"v{m['version']} · {now_iso()} · {actor_str(a)} · {op}" + (f": {detail}" if detail else "") + \
        (f" — why: {why}" if why else "")
    it["changelog"].insert(0, entry)


def task_body(kind: str, why: str = "", done=None) -> str:
    """A structured body for a new work item: the type's template with the reason and acceptance criteria filled in."""
    f = core.DEVKIT_HOME / "templates" / "tasks" / f"{kind}.md"
    text = f.read_text().strip() if f.exists() else "## Done when\n- [ ] _Acceptance criterion, testable._"
    if why:
        text = re.sub(r"(## (Why|Context|Question|Symptom)\n\n?)_[^\n]*_", lambda m: m.group(1) + why, text, count=1)
    if done:
        crit = "\n".join(f"- [ ] {d}" for d in done)
        text = re.sub(r"(## Done when\n\n?)(- \[ \] [^\n]*\n?)+", lambda m: m.group(1) + crit + "\n", text, count=1)
    return text.strip()


def create(scope: str, title: str, kind="task", bucket="next", priority="normal", body="", why="", a=None,
           links=None, due="") -> dict:
    a = a or actor()
    if kind not in TYPES:
        raise ValueError(f"type must be one of {', '.join(TYPES)}")
    iid = new_id(kind, scope)
    it = {"meta": {"id": iid, "type": kind, "title": title.strip(), "scope": scope, "status": "todo",
                   "bucket": bucket, "priority": priority, "assignee": "", "claimed_by": "", "claimed_at": "",
                   "lock_expires": "", "due": due, "links": links or [], "version": 1, "created": now_iso(),
                   "created_by": f"{a['user']}@{a['host']}"}, "body": body, "changelog": []}
    _touch(it, a, "create", why, f"{kind} “{title.strip()}” in {bucket}")
    p = item_path(scope, iid)
    p.parent.mkdir(parents=True, exist_ok=True)
    write_record(p, render(it))
    it["path"] = p
    _commit([p], f"{iid}: create {kind} “{title.strip()[:60]}” ({actor_str(a)})" + (f" — {why}" if why else ""))
    return it


def lock_holder(it: dict):
    """-> (holder string, expired?) or (None, False)."""
    m = it["meta"]
    if not m.get("claimed_by"):
        return None, False
    return m["claimed_by"], parse_iso(m.get("lock_expires")) < time.time()


def _owned_by(it: dict, a: dict) -> bool:
    holder, expired = lock_holder(it)
    return not holder or expired or holder.split(" ")[0] == f"{a['user']}@{a['host']}"


def update(it: dict, op: str, why: str = "", a=None, **changes) -> dict:
    a = a or actor()
    if op in NEEDS_WHY and not why.strip():
        raise ValueError(f"`{op}` changes the scope or plan: give the reason with --why \"…\" (recorded in the "
                         f"item's changelog and the commit)")
    iid, status = it["meta"].get("id"), it["meta"].get("status") or "todo"
    if op in OPEN_ONLY and status not in OPEN:  # a closed item keeps the state it was closed in
        raise ValueError(f"{iid} is {status}: `{op}` applies to open items; if work is left, reopen it first: "
                         f"dev task reopen {iid} --why \"<what is left>\"")
    if op == "unblock" and status != "blocked":
        raise ValueError(f"{iid} is {status}, not blocked: there is nothing to unblock")
    holder, expired = lock_holder(it)
    if holder and not expired and not _owned_by(it, a) and op not in ("comment",):
        raise ValueError(f"{it['meta']['id']} is claimed by {holder} until {it['meta'].get('lock_expires')}; "
                         f"coordinate with them or wait for the lock to expire")
    detail = ", ".join(f"{k}={v}" for k, v in changes.items() if k not in ("body",)) + \
        (" (body edited)" if "body" in changes else "")
    edited = (it.get("body") or "").splitlines() if "body" in changes else []
    if "title" in changes:
        edited.append(f"# {it['meta'].get('title') or it.get('title')}")
    for k, v in changes.items():
        if k == "body":
            it["body"] = v
        elif k == "title":
            it["meta"]["title"] = v
        else:
            it["meta"][k] = v
    _touch(it, a, op, why, detail)
    write_record(it["path"], render(it), edited)
    _commit([it["path"]], f"{it['meta']['id']}: {op}{(' ' + detail) if detail else ''} ({actor_str(a)})"
            + (f" — {why}" if why else ""))
    return it


def _has_entry(it: dict, entry: str) -> bool:
    """Does the item's changelog have `entry`, whatever its version number (a merge renumbers it)?"""
    want = _VERSION.sub("", entry, count=1)
    return any(_VERSION.sub("", e, count=1) == want for e in it.get("changelog") or [])


def _on_remote(path: Path):
    """The item as this Mac last pushed or fetched it from the remote, or None."""
    rc, out, _ = core.git(HANDOFF, "show", f"@{{u}}:{Path(path).relative_to(HANDOFF)}")
    return parse(out + "\n") if rc == 0 else None


def _push_change(path: Path, change, what: str, must_reach: bool) -> dict:
    """Apply `change` to the item at `path`, commit it and push it now. `change(item)` checks the item as it is and
    changes it (ValueError when it may not) -> the commit message. When another Mac pushed a change to the item
    first and the two cannot be merged, the item is read again as the remote has it and `change` applied again, so
    its checks run on the remote's version: a lock taken there first refuses this one. On this Mac the read, the
    change and the commit hold the item's lock (record_lock), so two sessions changing it at once take turns and the
    second one's checks see the first one's change. `must_reach`: raise when the
    change is not on the remote after three tries (it stays committed here, and `dev sync` pushes it later); without
    a remote the commit is the whole change. -> the item."""
    remote, rel = _pushes(), str(Path(path).relative_to(HANDOFF))
    entry, problem = None, ""
    for attempt in range(3):
        if remote and attempt == 0:
            pull(retake=[rel])
        with record_lock(path):  # another session of this Mac changing the item waits, then checks this change
            it = load(path)
            if entry is None or not _has_entry(it, entry):
                message = change(it)
                entry = it["changelog"][0]
                write_record(path, render(it))
                _commit([path], message, push=False)
        if not remote:
            return it
        ok, problem = push_now(retake=[rel])
        there = _on_remote(path) if ok else None
        if there is not None and _has_entry(there, entry):
            return load(path)
    if must_reach:
        raise ValueError(f"{it['meta']['id']}: {what} did not reach the remote ({problem or 'the push did not take it'}),"
                         f" so other Macs do not see it. It is committed on this Mac and `dev sync` pushes it when the "
                         f"remote answers; run the command again to confirm it.")
    return load(path)


def _refuse_closed(it: dict, what: str) -> None:
    """A closed item keeps the state it was closed in: starting it, releasing it to todo or closing it again needs
    dev task reopen first (ValueError)."""
    iid, status = it["meta"].get("id"), it["meta"].get("status") or "todo"
    if status not in OPEN:
        raise ValueError(f"{iid} is {status}: `{what}` applies to open items; if work is left, reopen it first: "
                         f"dev task reopen {iid} --why \"<what is left>\"")


def claim(it: dict, a=None, hours: float = None, why: str = "", branch: str = "") -> dict:
    """Take the lock: pull first, then push at once so other Macs see it; it reports success only when the lock is on
    the remote (a handoff repository without a remote: when it is committed). `branch`: the branch the work happens
    on, kept as `branch:<name>` in the item's links (the private handoff), so task ids never go into branch names
    and the branch map still finds the task (spec P-fy3bwa0pshk R7)."""
    a = a or actor()
    hours = hours or float(core.CFG.get("work", {}).get("lock_hours", 8))

    def take(it):
        _refuse_closed(it, "start")
        holder, expired = lock_holder(it)
        if holder and not expired and not _owned_by(it, a):
            raise ValueError(f"{it['meta']['id']} is already claimed by {holder} (until "
                             f"{it['meta'].get('lock_expires')})")
        m = it["meta"]
        m["claimed_by"] = actor_str(a)
        m["claimed_at"] = now_iso()
        m["lock_expires"] = now_iso(time.time() + hours * 3600)
        if m.get("status") == "todo":
            m["status"], m["bucket"] = "doing", "now"
        m["assignee"] = m.get("assignee") or a["user"]
        links = [x for x in (m.get("links") or []) if x]
        if branch and f"branch:{branch}" not in links:
            m["links"] = links + [f"branch:{branch}"]
        _touch(it, a, "claim", why, f"lock until {m['lock_expires']}" + (f", on {branch}" if branch else ""))
        return f"{m['id']}: claim by {actor_str(a)}"

    return _push_change(it["path"], take, "the lock", must_reach=True)


def reopen(it: dict, why: str, a=None) -> dict:
    """A finished or cancelled item goes back to work (todo), with its reason in the changelog and the commit."""
    if not (why or "").strip():
        raise ValueError("reopening a finished item needs its reason: --why \"…\"")
    status = (it.get("meta") or {}).get("status")
    if status not in ("done", "cancelled"):
        raise ValueError(f"{it['meta'].get('id')} is {status}, not finished: there is nothing to reopen")
    return update(it, "reopen", why=why, a=a, status="todo")


def release(it: dict, a=None, why: str = "", status: str = None) -> dict:
    """Give the lock back (and set `status`, such as done), pushed at once. When the push cannot reach the remote the
    change stays committed here for `dev sync`: finishing work does not need the network."""
    a = a or actor()

    def give_back(it):
        if status:  # without a status it only frees a lock, which a closed item may still hold
            _refuse_closed(it, status)
        if not _owned_by(it, a):
            holder, _ = lock_holder(it)
            raise ValueError(f"{it['meta']['id']} is claimed by {holder}; only they (or an expired lock) can release "
                             f"it")
        m = it["meta"]
        m["claimed_by"] = m["claimed_at"] = m["lock_expires"] = ""
        if status:
            m["status"] = status
        _touch(it, a, "release" if not status else status, why)
        return f"{m['id']}: {'release' if not status else status} ({actor_str(a)})" + (f" — {why}" if why else "")

    return _push_change(it["path"], give_back, f"the {status or 'release'}", must_reach=False)


def _report_skipped(job: str, op: str, skipped: list) -> None:
    """A lock job leaves an item it cannot rewrite (RecordLossError) as it is. Say so where someone will see it: a
    warning on stderr, which is the sync's log when the job runs in the background, and an audit record kept at every
    audit level (`dev audit --action handoff.record-skipped`): the file, and `<item id>: <op>`. Neither quotes the
    item: the background sync keeps its last log line as the job's audit detail, so the warning names the file and
    the number of lines, and `dev task show` shows them."""
    if not skipped:
        return
    from . import audit, ui
    for err in skipped:
        try:
            target = str(err.path.relative_to(HANDOFF))
        except ValueError:
            target = str(err.path)
        ui.warn(f"{job}: left {core.tilde(err.path)} as it is: it would drop {len(err.lost)} line(s) that the job does "
                f"not change; `dev task show {err.path.stem}` shows the item. See “devkit refused to write a task or "
                f"plan file” in docs/troubleshooting.md.")
        audit.record("handoff.record-skipped", target, f"{err.path.stem}: {op}", outcome="blocked")


def _links(it: dict) -> list:
    links = it["meta"].get("links") or []
    return [x.strip() for x in links.strip("[]").split(",")] if isinstance(links, str) else list(links)


def _live_branches(sessions) -> set:
    """(project, branch) of each live session: the branches someone is working on now."""
    return {(s.get("project"), s.get("branch")) for s in sessions or []
            if s.get("active") and s.get("project") and s.get("branch")}


def renew_locks(a=None, hours: float = None, sessions=None) -> int:
    """Extend the locks this actor holds on open items (OPEN) linked to a branch (`branch:<name>`, set by `claim`)
    that one of the live sessions on this machine is on now; a done or cancelled item's lock is never renewed.
    `sessions`: `registry.live_sessions()` (read when None). Any other lock is left to run out after
    `work.lock_hours`, and `expire_locks` then sends its item back to todo."""
    a = a or actor()
    hours = hours or float(core.CFG.get("work", {}).get("lock_hours", 8))
    if sessions is None:
        from .registry import live_sessions
        sessions = live_sessions()
    live = _live_branches(sessions)
    me = f"{a['user']}@{a['host']}"
    changed, skipped = [], []
    for it in all_items():
        if (it["meta"].get("status") or "todo") not in OPEN:  # a done or cancelled item needs no lock kept alive
            continue
        holder, expired = lock_holder(it)
        working = any((it["scope"], ln[len("branch:"):]) in live for ln in _links(it) if ln.startswith("branch:"))
        if holder and holder.startswith(me) and not expired and working:
            if parse_iso(it["meta"]["lock_expires"]) - time.time() < hours * 1800:
                it["meta"]["lock_expires"] = now_iso(time.time() + hours * 3600)
                try:
                    write_record(it["path"], render(it))
                except RecordLossError as e:
                    skipped.append(e)
                    continue
                changed.append(it["path"])
    if changed:
        _commit(changed, f"renew {len(changed)} lock(s) held by {me}")
    _report_skipped("lock renewal", "lock-renewal-skipped", skipped)
    return len(changed)


def expire_locks() -> list:
    """Release expired locks (run by the hourly sync on any machine). A doing item goes back to todo, so nobody's
    board shows work in progress that nobody holds; a blocked item stays blocked. One commit for all."""
    out, skipped, back = [], [], 0
    for it in all_items():
        holder, expired = lock_holder(it)
        if holder and expired:
            m = it["meta"]
            doing = m.get("status") == "doing"
            it["changelog"].insert(0, f"v{m['version'] + 1} · {now_iso()} · devkit · lock of {holder} expired at "
                                      f"{m.get('lock_expires')}" + (": back to todo" if doing else ""))
            m["version"] += 1
            m["claimed_by"] = m["claimed_at"] = m["lock_expires"] = ""
            if doing:
                m["status"] = "todo"
            try:
                write_record(it["path"], render(it))
            except RecordLossError as e:
                skipped.append(e)
                continue
            out.append(it["path"])
            back += 1 if doing else 0
    if out:
        _commit(out, f"release {len(out)} expired lock(s)" + (f", {back} item(s) back to todo" if back else ""),
                push=False)
    _report_skipped("expired-lock release", "lock-release-skipped", skipped)
    return out


# --------------------------------------------------------------------------- items of merged branches

OPEN = ("todo", "doing", "blocked")


def _linked_at(it: dict, branch: str) -> float:
    """When the item was tied to `branch`: the oldest changelog entry that links it (a claim `on <branch>`, a
    `branch:<branch>` link), else its claim, else its creation. Seconds since the epoch."""
    names = re.compile(r"(?:, on |branch:)" + re.escape(branch) + r"(?![\w./-])")
    for entry in reversed(it.get("changelog") or []):
        parts = entry.split(" · ")
        if len(parts) > 1 and names.search(entry):
            return parse_iso(parts[1])
    m = it["meta"]
    return parse_iso(m.get("claimed_at") or m.get("created") or "")


def linked_open(scope: str, branches) -> list:
    """The open items of `scope` linked to one of `branches` (`branch:<name>`, set by `claim`): [(item, branch)].
    `branches` may map each name to its merge ({"at": seconds since the epoch, …}): a merge older than the item's link
    to the branch was an earlier use of the same name, and does not count for the item."""
    want = branches if isinstance(branches, dict) else dict.fromkeys(branches or (), None)
    out = []
    for it in all_items(scope):
        if it["meta"].get("status") not in OPEN:
            continue
        for ln in _links(it):
            name = ln[len("branch:"):]
            if not ln.startswith("branch:") or name not in want:
                continue
            at = (want[name] or {}).get("at")
            if at and at < _linked_at(it, name):
                continue
            out.append((it, name))
            break
    return out


def linked_branches(scope: str) -> list:
    """The branches the open items of `scope` are linked to."""
    return sorted({ln[len("branch:"):] for it in all_items(scope) if it["meta"].get("status") in OPEN
                   for ln in _links(it) if ln.startswith("branch:")})


def link_new_branch(scope: str, branch: str, a=None) -> str:
    """A branch was just created in `scope`'s repository: when the person holds a live lock on exactly one open item
    of `scope` that no branch is linked to (claimed on the default branch, or before the branch existed), link it
    (`branch:<branch>`), so its lock renews while they work there and the merge closes it. -> the line to print:
    what was linked, how to choose when several items qualify, or "" when none does."""
    a = a or actor()
    held = [it for it in all_items(scope) if it["meta"].get("status") in OPEN
            and (lock_holder(it)[0] or "").split("@")[0] == a["user"] and not lock_holder(it)[1]
            and not any(ln.startswith("branch:") for ln in _links(it))]
    if len(held) > 1:
        return (f"{len(held)} items you hold have no branch ({', '.join(it['meta']['id'] for it in held[:5])}): run "
                f"`dev task start <id>` on {branch} for the one it is for")
    if not held:
        return ""
    it = held[0]
    done = _apply([(it, "link", f"branch:{branch}, created while it was claimed",
                    {"links": _links(it) + [f"branch:{branch}"]})], a, "branch link", "branch-link-skipped",
                  lambda n: f"{it['meta']['id']}: link branch:{branch}")
    if not done:
        return ""
    return (f"linked {it['meta']['id']} “{it['meta'].get('title')}” to {branch}: its lock renews while you work there, "
            f"and the merge closes it")


def _apply(changes, a: dict, job: str, op: str, message) -> list:
    """Write each (item, op, detail, meta changes) through `write_record`, then one commit whose message is
    `message(<items written>)`. An item the write would shrink is left as it is and reported (`_report_skipped`).
    -> the written items."""
    done, skipped = [], []
    for it, verb, detail, meta in changes:
        it["meta"].update(meta)
        _touch(it, a, verb, detail=detail)
        try:
            write_record(it["path"], render(it))
        except RecordLossError as e:
            skipped.append(e)
            continue
        done.append(it)
    if done:
        _commit([it["path"] for it in done], message(len(done)) + f" ({actor_str(a)})")
    _report_skipped(job, op, skipped)
    return done


UNLOCK = {"claimed_by": "", "claimed_at": "", "lock_expires": ""}


def close_merged(scope: str, branch: str, detail: str, a=None) -> list:
    """After `branch` is merged, every open item of `scope` linked to it is done and its lock cleared, whoever holds
    it: the work landed. `detail` says where (`merged in PR #7 (abc1234)`) and goes in each changelog line. One
    commit. -> the closed items."""
    a = a or actor()
    changes = [(it, "done", detail, dict(UNLOCK, status="done")) for it, _ in linked_open(scope, [branch])]
    return _apply(changes, a, "merge", "merge-close-skipped", lambda n: f"close {n} item(s) of {branch}: {detail}")


def normal_title(title: str) -> str:
    """A title for comparison: lower case, words only (`Add the CSV export.` and `add the  csv export` match)."""
    return " ".join(re.sub(r"[^\w]+", " ", (title or "").lower()).split())


def _brief(it: dict, **extra) -> dict:
    m = it["meta"]
    return dict({"id": m.get("id"), "title": m.get("title") or it.get("title"), "status": m.get("status")}, **extra)


def reconcile(scope: str, merged: dict, apply: bool = False, a=None) -> dict:
    """What is stale in `scope`'s items: doing items without a live lock, open items linked to a branch in `merged`
    (branch -> {"sha", "pr"}: the merges into the default branch, `gitx.merged_into_default`), and items that share a
    normalized title (open ones, with any done one of the same title). `apply` closes the merged ones (done, with
    the merge commit as evidence) and sends the lockless doing ones back to todo, each with a changelog line, in one
    commit; the same titles are only reported. Without `apply` nothing is written.
    -> {"scope", "lockless_doing", "merged", "duplicates", "changed": the ids written}"""
    items = all_items(scope)
    shipped = linked_open(scope, merged)
    closing = {it["meta"]["id"] for it, _ in shipped}
    lockless = [it for it in items if it["meta"].get("status") == "doing" and it["meta"]["id"] not in closing
                and (not lock_holder(it)[0] or lock_holder(it)[1])]
    groups = {}
    for it in items:
        if it["meta"].get("status") != "cancelled" and normal_title(it["meta"].get("title")):
            groups.setdefault(normal_title(it["meta"].get("title")), []).append(it)
    dups = [g for g in groups.values() if len(g) > 1 and any(i["meta"].get("status") in OPEN for i in g)]
    report = {"scope": scope, "lockless_doing": [_brief(it) for it in lockless],
              "merged": [_brief(it, branch=b, commit=merged[b]["sha"], pr=merged[b].get("pr")) for it, b in shipped],
              "duplicates": [[_brief(it) for it in g] for g in dups], "changed": []}
    if not apply:
        return report
    a = a or actor()
    changes = [(it, "done", f"{b} merged in {merged[b]['sha'][:7]}" +
                (f" (PR #{merged[b]['pr']})" if merged[b].get("pr") else ""), dict(UNLOCK, status="done"))
               for it, b in shipped]
    changes += [(it, "todo", "doing without a lock: back to todo", dict(UNLOCK, status="todo")) for it in lockless]
    written = _apply(changes, a, "reconcile", "reconcile-skipped",
                     lambda n: f"reconcile {n} item(s): {len(shipped)} merged, {len(lockless)} back to todo")
    report["changed"] = [it["meta"]["id"] for it in written]
    return report


# --------------------------------------------------------------------------- sessions log and presence

def _append_session(f: Path, scope: str, month: str, line: str) -> None:
    """Append one line to the month's log, starting the file with its heading; the caller holds its record_lock."""
    if not f.exists():
        core.replace_text(f, f"# Sessions · {scope} · {month}\n\n")
    with f.open("a") as fh:
        fh.write(f"- {line}\n")


def log_session(scope: str, line: str):
    """Append-only, one file per scope and month (merge=union): concurrent machines never conflict. On this Mac the
    append holds the month file's lock (record_lock), so a session that rewrites its own line at the same moment
    (replace_session_line) never writes over this one. -> the month's file."""
    month = time.strftime("%Y-%m")
    f = SESSIONS / scope / f"{month}.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    with record_lock(f):
        _append_session(f, scope, month, line)
    return f


def replace_session_line(scope: str, key: str, line: str):
    """One session's line in the month's log, which the Stop hook rewrites on every turn: the lines that carry `key`
    (the session's marker, such as `<!-- s:<id> -->`) become `- <line>`, in place; with none, the line is appended
    (log_session). The read and the rewrite hold the same lock as log_session's appends, and the rewrite replaces the
    file in one step, so lines other sessions append meanwhile are never lost. -> the month's file."""
    month = time.strftime("%Y-%m")
    f = SESSIONS / scope / f"{month}.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    with record_lock(f):
        text = f.read_text() if f.exists() else ""
        if key not in text:
            _append_session(f, scope, month, line)
            return f
        new = "\n".join(f"- {line}" if key in ln else ln for ln in text.splitlines()) + "\n"
        if new != text:
            core.replace_text(f, new)
    return f


def recent_sessions(scope: str, limit: int = 12) -> list:
    d = SESSIONS / scope
    out = []
    if d.exists():
        for f in sorted(d.glob("*.md"), reverse=True):
            lines = [ln[2:] for ln in f.read_text().splitlines() if ln.startswith("- ")]
            out += list(reversed(lines))
            if len(out) >= limit:
                break
    return out[:limit]


RUN_TEXT = ("goal", "state", "headline", "last", "next")  # what run_summary keeps of a run brief, as it is
RUN_NUMBERS = ("step", "steps", "estimate", "started", "updated", "eta_min", "eta_lo", "eta_hi", "active_min",
               "wall_min", "pace")
CURRENT_SHOWN = 3  # steps running now that presence names


def run_summary(brief):
    """What presence publishes of a session's run (progress.brief): its goal and state, the step counts (done, that
    count, skipped), the steps running or blocked now (at most CURRENT_SHOWN, by name; `current_more` counts the
    rest), how many crew lanes work (`crew`), and the ETA. Never the whole plan or the lanes: a run of 60 steps made
    one Mac's presence 49 KB, and above 256 KB another Mac's dashboard hides it. `plan` keeps only the current steps,
    in the shape older dashboards read. None when the session has no run."""
    if not isinstance(brief, dict):
        return None
    plan = [s for s in brief.get("plan") or [] if isinstance(s, dict)]
    states = Counter(s.get("state") for s in plan)
    now = [s for s in plan if s.get("state") in ("running", "blocked")]
    out = {k: brief[k][:200] if isinstance(brief[k], str) else brief[k] for k in RUN_TEXT if k in brief}
    out.update({k: brief[k] for k in RUN_NUMBERS if k in brief})
    out.update(steps_done=states["done"], steps_total=len(plan) - states["skipped"], steps_skipped=states["skipped"],
               current=[str(s.get("name") or "")[:120] for s in now[:CURRENT_SHOWN]],
               current_more=max(0, len(now) - CURRENT_SHOWN),
               crew=sum(1 for g in brief.get("agents") or [] if isinstance(g, dict) and g.get("state") == "running"),
               plan=[dict({k: s.get(k) for k in ("state", "est", "min", "owner")}, name=str(s.get("name") or "")[:120])
                     for s in now[:CURRENT_SHOWN]])
    return out


def write_presence(sessions: list, a=None):
    """presence/<user>@<host>.json: this machine's tracked sessions, each with its `active` flag (a session still
    tracked a while after its agent stopped is not active) and a summary of its run (run_summary), and held locks
    (written only by this machine)."""
    a = a or actor("human")
    me = f"{a['user']}@{a['host']}"
    held = [{"id": i["meta"]["id"], "title": i["meta"]["title"], "scope": i["scope"],
             "until": i["meta"]["lock_expires"]} for i in all_items()
            if (i["meta"].get("claimed_by") or "").startswith(me) and not lock_holder(i)[1]]
    data = {"user": a["user"], "host": a["host"], "updated": now_iso(),
            "sessions": [dict({k: s.get(k) for k in ("agent", "project", "branch", "started", "status", "last_seen",
                                                     "subagents", "turn_started", "active")},
                              progress=run_summary(s.get("progress")))
                         for s in sessions], "locks": held}
    f = PRESENCE / f"{me}.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    old = core.read_json(f, {}) or {}
    if {k: v for k, v in old.items() if k != "updated"} != {k: v for k, v in data.items() if k != "updated"}:
        f.write_text(json.dumps(data, indent=1) + "\n")
        return True
    return False


def team(max_age_h: float = 24) -> list:
    """The machines that published presence in the last `max_age_h` hours, each with its live sessions only: a
    session marked not active has ended (presence from an older devkit has no flag, and its sessions stay)."""
    out = []
    for f in sorted(PRESENCE.glob("*.json")) if PRESENCE.exists() else []:
        d = core.read_json(f, None)
        if d and time.time() - parse_iso(d.get("updated")) < max_age_h * 3600:
            if isinstance(d, dict) and isinstance(d.get("sessions"), list):
                d["sessions"] = [s for s in d["sessions"] if not (isinstance(s, dict) and s.get("active") is False)]
            out.append(d)
    return out


# --------------------------------------------------------------------------- summaries

def initiative_items(slug: str, members: list) -> list:
    """Initiative-scope items plus member-project items linked to the initiative (links: initiative:<slug>)."""
    tag = f"initiative:{slug}"
    items = all_items(f"initiatives/{slug}")
    for m in members or []:
        items += [i for i in all_items(m) if tag in (i["meta"].get("links") or [])]
    return items


def summary(scope: str, items: list = None) -> dict:
    items = all_items(scope) if items is None else items
    open_ = ordered_open(items)
    done = [i for i in items if i["meta"].get("status") == "done"]

    def brief(i):
        holder, expired = lock_holder(i)
        return {"id": i["meta"]["id"], "title": i["meta"]["title"], "type": i["meta"].get("type"), "scope": i["scope"],
                "status": i["meta"].get("status"), "bucket": i["meta"].get("bucket"),
                "priority": i["meta"].get("priority"), "claimed_by": holder if holder and not expired else "",
                "version": i["meta"].get("version"), "updated": i["meta"].get("updated"), "due": i["meta"].get("due")}

    done_sorted = sorted(done, key=lambda i: i["meta"].get("updated", ""), reverse=True)
    return {"open": [brief(i) for i in open_], "done_recent": [brief(i) for i in done_sorted[:10]],
            "counts": {"open": len(open_), "done": len(done),
                       "cancelled": len([i for i in items if i["meta"].get("status") == "cancelled"]),
                       "doing": len([i for i in open_ if i["meta"].get("status") == "doing"]),
                       "blocked": len([i for i in open_ if i["meta"].get("status") == "blocked"])}}


def progress(scope: str, items: list = None):
    s = summary(scope, items)["counts"]
    total = s["open"] + s["done"]
    return (round(100 * s["done"] / total), "tasks") if total else (None, "none")
