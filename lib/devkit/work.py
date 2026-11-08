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

import json
import os
import re
import secrets
import socket
import stat
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
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
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
    """Write `text` to a new file in the same folder, then swap it in (os.replace): a reader sees the old record or
    the new one, never part of one. The temporary name ends in `.tmp`, which the handoff repository ignores."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)  # the umask applies, as to a plain write
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))  # keep the record's own permissions
        except FileNotFoundError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def write_record(path: Path, text: str, edited=()) -> None:
    """Write a record file, but refuse (RecordLossError, the file unchanged) when the text would drop lines of the
    version on disk that this change does not edit: a misread record must stop the command, never shrink."""
    path = Path(path)
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


def load(path: Path) -> dict:
    it = parse(path.read_text())
    it["path"] = path
    return it


def all_items(scope: str = None) -> list:
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
    """By id (exact or unique prefix), by number in `dev task` order, or by a unique part of the title."""
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
        ordered = ordered_open(items)
        n = int(sel)
        if 1 <= n <= len(ordered):
            return ordered[n - 1]
        raise ValueError(f"no open item #{n}")
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


# --------------------------------------------------------------------------- git (audit trail)

def _commit(paths, message: str, push: bool = True, wait: bool = False):
    """One commit per change (the audit trail); push in the background unless `wait` (locks)."""
    if not (HANDOFF / ".git").exists():
        return True
    rel = [str(Path(p).relative_to(HANDOFF)) for p in paths]
    core.git(HANDOFF, "add", "--", *rel)
    rc, _, _ = core.git(HANDOFF, "diff", "--cached", "--quiet")
    if rc != 0:
        core.git(HANDOFF, "commit", "-q", "-m", message, "--", *rel)
        from . import audit
        audit.record("handoff." + (rel[0].split("/")[0] if rel else "write"), ", ".join(rel)[:300], message)
    if not push or not core.gitout(HANDOFF, "remote") or not core.CFG["sync"].get("push", True):
        return True
    if os.environ.get("DEVKIT_NO_AUTOSYNC") and not wait:  # bulk operations push once at the end
        return True
    if wait:
        core.git(HANDOFF, "pull", "-q", "--rebase", "--autostash", timeout=60)
        rc, _, _ = core.git(HANDOFF, "push", "-q", timeout=60)
        return rc == 0
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
        subprocess.Popen([sys.executable, "-I", "-S", str(core.DEVKIT_HOME / "bin" / "dev"), "sync", "--quiet",
                          "--offline", "--delay", str(delay)], stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         start_new_session=True, close_fds=True)  # isolated, as its shebang runs it
    finally:
        if out is not subprocess.DEVNULL:
            out.close()  # the child holds its own copy of the log
    return True


def pull():
    if (HANDOFF / ".git").exists() and core.gitout(HANDOFF, "remote"):
        core.git(HANDOFF, "pull", "-q", "--rebase", "--autostash", timeout=60)


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


def claim(it: dict, a=None, hours: float = None, why: str = "", branch: str = "") -> dict:
    """Take the lock (pull first, push immediately so other machines see it). `branch`: the branch the work happens
    on, kept as `branch:<name>` in the item's links (the private handoff), so task ids never go into branch names
    and the branch map still finds the task (spec P-fy3bwa0pshk R7)."""
    a = a or actor()
    hours = hours or float(core.CFG.get("work", {}).get("lock_hours", 8))
    pull()
    it = load(it["path"])
    holder, expired = lock_holder(it)
    if holder and not expired and not _owned_by(it, a):
        raise ValueError(f"{it['meta']['id']} is already claimed by {holder} (until {it['meta'].get('lock_expires')})")
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
    write_record(it["path"], render(it))
    ok = _commit([it["path"]], f"{m['id']}: claim by {actor_str(a)}", wait=True)
    if not ok:
        pull()
        it2 = load(it["path"])
        h2, _ = lock_holder(it2)
        if h2 and h2 != m["claimed_by"]:
            raise ValueError(f"{m['id']} was claimed by {h2} on another machine first")
    return it


def reopen(it: dict, why: str, a=None) -> dict:
    """A finished or cancelled item goes back to work (todo), with its reason in the changelog and the commit."""
    if not (why or "").strip():
        raise ValueError("reopening a finished item needs its reason: --why \"…\"")
    status = (it.get("meta") or {}).get("status")
    if status not in ("done", "cancelled"):
        raise ValueError(f"{it['meta'].get('id')} is {status}, not finished: there is nothing to reopen")
    return update(it, "reopen", why=why, a=a, status="todo")


def release(it: dict, a=None, why: str = "", status: str = None) -> dict:
    a = a or actor()
    if not _owned_by(it, a):
        holder, _ = lock_holder(it)
        raise ValueError(f"{it['meta']['id']} is claimed by {holder}; only they (or an expired lock) can release it")
    m = it["meta"]
    m["claimed_by"] = m["claimed_at"] = m["lock_expires"] = ""
    if status:
        m["status"] = status
    _touch(it, a, "release" if not status else status, why)
    write_record(it["path"], render(it))
    _commit([it["path"]], f"{m['id']}: {'release' if not status else status} ({actor_str(a)})"
            + (f" — {why}" if why else ""), wait=True)
    return it


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
    """Extend the locks this actor holds on items linked to a branch (`branch:<name>`, set by `claim`) that one of the
    live sessions on this machine is on now. `sessions`: `registry.live_sessions()` (read when None). Any other lock
    is left to run out after `work.lock_hours`, and `expire_locks` then sends its item back to todo."""
    a = a or actor()
    hours = hours or float(core.CFG.get("work", {}).get("lock_hours", 8))
    if sessions is None:
        from .registry import live_sessions
        sessions = live_sessions()
    live = _live_branches(sessions)
    me = f"{a['user']}@{a['host']}"
    changed, skipped = [], []
    for it in all_items():
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


def linked_open(scope: str, branches) -> list:
    """The open items of `scope` linked to one of `branches` (`branch:<name>`, set by `claim`): [(item, branch)]."""
    want, out = set(branches or ()), []
    for it in all_items(scope):
        if it["meta"].get("status") not in OPEN:
            continue
        branch = next((ln[len("branch:"):] for ln in _links(it)
                       if ln.startswith("branch:") and ln[len("branch:"):] in want), None)
        if branch:
            out.append((it, branch))
    return out


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

def log_session(scope: str, line: str):
    """Append-only, one file per scope and month (merge=union): concurrent machines never conflict."""
    f = SESSIONS / scope / f"{time.strftime('%Y-%m')}.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    if not f.exists():
        f.write_text(f"# Sessions · {scope} · {time.strftime('%Y-%m')}\n\n")
    with f.open("a") as fh:
        fh.write(f"- {line}\n")
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


def write_presence(sessions: list, a=None):
    """presence/<user>@<host>.json: this machine's live sessions and held locks (written only by this machine)."""
    a = a or actor("human")
    me = f"{a['user']}@{a['host']}"
    held = [{"id": i["meta"]["id"], "title": i["meta"]["title"], "scope": i["scope"],
             "until": i["meta"]["lock_expires"]} for i in all_items()
            if (i["meta"].get("claimed_by") or "").startswith(me) and not lock_holder(i)[1]]
    data = {"user": a["user"], "host": a["host"], "updated": now_iso(),
            "sessions": [{k: s.get(k) for k in ("agent", "project", "branch", "started", "status", "last_seen",
                                                "progress", "subagents", "turn_started")}
                         for s in sessions], "locks": held}
    f = PRESENCE / f"{me}.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    old = core.read_json(f, {}) or {}
    if {k: v for k, v in old.items() if k != "updated"} != {k: v for k, v in data.items() if k != "updated"}:
        f.write_text(json.dumps(data, indent=1) + "\n")
        return True
    return False


def team(max_age_h: float = 24) -> list:
    out = []
    for f in sorted(PRESENCE.glob("*.json")) if PRESENCE.exists() else []:
        d = core.read_json(f, None)
        if d and time.time() - parse_iso(d.get("updated")) < max_age_h * 3600:
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
