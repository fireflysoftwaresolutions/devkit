# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
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
import socket
import time
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
NEEDS_WHY = {"edit", "cancel", "priority", "move", "reopen", "scope"}
FIELDS = ("id", "type", "title", "scope", "status", "bucket", "priority", "assignee", "claimed_by", "claimed_at",
          "lock_expires", "due", "links", "version", "created", "created_by", "updated", "updated_by")


# --------------------------------------------------------------------------- identity

def actor(agent: str = None, session: str = None) -> dict:
    user = core.github_user() or os.environ.get("USER", "unknown")
    host = socket.gethostname().split(".")[0].lower()
    agent = agent or os.environ.get("DEVKIT_AGENT") or ("claude" if os.environ.get("CLAUDECODE") else
                                                        "codex" if os.environ.get("CODEX_SANDBOX") or
                                                        os.environ.get("CODEX_THREAD_ID") else "human")
    return {"user": user, "host": host, "agent": agent, "session": (session or os.environ.get("DEVKIT_SESSION", ""))[:12]}


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


def parse(text: str) -> dict:
    meta, body = {}, text
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            for ln in text[4:end].splitlines():
                k, _, v = ln.partition(":")
                v = v.strip()
                if v.startswith("[") and v.endswith("]"):
                    meta[k.strip()] = [x.strip() for x in v[1:-1].split(",") if x.strip()]
                else:
                    meta[k.strip()] = v
            body = text[end + 5:]
    title_m = re.search(r"^# (.+)$", body, re.M)
    desc, changelog = body, []
    if "\n## Changelog" in body:
        desc, _, log = body.partition("\n## Changelog")
        changelog = [ln[2:].strip() for ln in log.splitlines() if ln.startswith("- ")]
    desc = re.sub(r"^# .+\n?", "", desc.strip(), count=1).strip()
    meta["version"] = int(meta.get("version") or 1)
    return {"meta": meta, "title": meta.get("title") or (title_m.group(1) if title_m else ""), "body": desc,
            "changelog": changelog}


def render(item: dict) -> str:
    m = item["meta"]
    lines = ["---"]
    for k in FIELDS:
        v = m.get(k, "")
        if isinstance(v, list):
            v = "[" + ", ".join(v) + "]"
        lines.append(f"{k}: {v}".rstrip())
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
    if not push or not core.gitout(HANDOFF, "remote") or not core.CFG["sync"].get("push", True):
        return True
    if os.environ.get("DEVKIT_NO_AUTOSYNC") and not wait:  # bulk operations push once at the end
        return True
    if wait:
        core.git(HANDOFF, "pull", "-q", "--rebase", "--autostash", timeout=60)
        rc, _, _ = core.git(HANDOFF, "push", "-q", timeout=60)
        return rc == 0
    import subprocess
    import sys
    subprocess.Popen([sys.executable, str(core.DEVKIT_HOME / "bin" / "dev"), "sync", "--quiet", "--offline"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)
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
        text = re.sub(r"(## (Why|Context|Question|Symptom)\n)_[^\n]*_", lambda m: m.group(1) + why, text, count=1)
    if done:
        crit = "\n".join(f"- [ ] {d}" for d in done)
        text = re.sub(r"(## Done when\n)(- \[ \] [^\n]*\n?)+", lambda m: m.group(1) + crit + "\n", text, count=1)
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
    p.write_text(render(it))
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
    for k, v in changes.items():
        if k == "body":
            it["body"] = v
        elif k == "title":
            it["meta"]["title"] = v
        else:
            it["meta"][k] = v
    _touch(it, a, op, why, detail)
    it["path"].write_text(render(it))
    _commit([it["path"]], f"{it['meta']['id']}: {op}{(' ' + detail) if detail else ''} ({actor_str(a)})"
            + (f" — {why}" if why else ""))
    return it


def claim(it: dict, a=None, hours: float = None, why: str = "") -> dict:
    """Take the lock (pull first, push immediately so other machines see it)."""
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
    _touch(it, a, "claim", why, f"lock until {m['lock_expires']}")
    it["path"].write_text(render(it))
    ok = _commit([it["path"]], f"{m['id']}: claim by {actor_str(a)}", wait=True)
    if not ok:
        pull()
        it2 = load(it["path"])
        h2, _ = lock_holder(it2)
        if h2 and h2 != m["claimed_by"]:
            raise ValueError(f"{m['id']} was claimed by {h2} on another machine first")
    return it


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
    it["path"].write_text(render(it))
    _commit([it["path"]], f"{m['id']}: {'release' if not status else status} ({actor_str(a)})"
            + (f" — {why}" if why else ""), wait=True)
    return it


def renew_locks(a=None, hours: float = None) -> int:
    """Extend the locks this actor holds (called while a session is active)."""
    a = a or actor()
    hours = hours or float(core.CFG.get("work", {}).get("lock_hours", 8))
    me = f"{a['user']}@{a['host']}"
    changed = []
    for it in all_items():
        holder, expired = lock_holder(it)
        if holder and holder.startswith(me) and not expired:
            if parse_iso(it["meta"]["lock_expires"]) - time.time() < hours * 1800:
                it["meta"]["lock_expires"] = now_iso(time.time() + hours * 3600)
                it["path"].write_text(render(it))
                changed.append(it["path"])
    if changed:
        _commit(changed, f"renew {len(changed)} lock(s) held by {me}")
    return len(changed)


def expire_locks() -> list:
    """Release expired locks (run by the hourly sync on any machine)."""
    out = []
    for it in all_items():
        holder, expired = lock_holder(it)
        if holder and expired:
            m = it["meta"]
            it["changelog"].insert(0, f"v{m['version'] + 1} · {now_iso()} · devkit · lock of {holder} expired")
            m["version"] += 1
            m["claimed_by"] = m["claimed_at"] = m["lock_expires"] = ""
            it["path"].write_text(render(it))
            out.append(it["path"])
    if out:
        _commit(out, f"release {len(out)} expired lock(s)", push=False)
    return out


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
