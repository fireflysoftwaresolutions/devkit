# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The branch map (spec P-fy3bwa0pshk, R7, R8 and R11): where each local branch of a repository lives.

`map(repo)` returns one row per local branch: its upstream and how far it is from it and from the default branch, its
last commit, the commits that exist only on this Mac, its last backup (the `git.backup` audit records), its embargo
(the handoff note's `embargo:` list), its worktree, its pull request and its linked task, and what that means for its
safety off this Mac (`backup`: a state and the reason in words). The handoff note's generated block, `dev status`,
`dev overview`, `dev branch ls`, the session-start context and the dashboard all read these rows, so every view says
the same thing about the same branch.

A row (`dev branch ls --json` prints the list; `registry.json` keeps it as `projects[].branches`):

    name, current, default, default_branch, sha            current: checked out where the map was asked
    upstream, upstream_remote, upstream_gone, ahead, behind   ahead and behind its upstream (0 without one)
    base, base_ahead, base_behind                           against origin/<default> (or the local default)
    where                                                   "local + origin" | "local only"
    last_commit, unpushed, oldest_unpushed                  unix times; unpushed: commits on no remote ref
    merged_into                                             a branch that has every commit of this one, or null
    last_backup                                             unix time of the newest git.backup record, or null
    embargo                                                 {why, until, who, when} or null
    worktree, worktree_missing                              the linked worktree that has it checked out
    changes, session                                        {modified, untracked} and a live session, when known
    pr                                                      {number, state, draft, checks, url, title} or null
    task                                                    {id, title, status} or null
    backup                                                  {state, reason}

`backup.state` is one of: backed_up (no commit only here, and a backup is recorded), on_remote (no commit only
here), merged (another branch carries its commits), waiting (its commits are younger than `backup.after_minutes`),
not_backed_up, embargoed, no_remote (the repository has no remote). The reason has no times in it; the views add them
from the row (relative on screens, absolute in the note so it changes only when a branch does).

Nothing here pushes or fetches. Pull requests come from the caller (the registry's cached list), from the same
cache, or, for `dev branch ls`, from `registry.prs` (cached, with timeouts); a hook never waits on GitHub.
Python 3.9 compatible, stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from . import core, notes, ui

AFTER_MINUTES = 30  # backup.after_minutes when the configuration does not set it
AUDIT_MONTHS = 3  # backups older than this many monthly audit files are not looked up
STATES = ("backed_up", "on_remote", "merged", "waiting", "not_backed_up", "embargoed", "no_remote")
AT_RISK = ("not_backed_up", "no_remote")  # work that exists only on this Mac (embargoes are counted apart)
_FMT = "%00".join(("%(refname:short)", "%(upstream:short)", "%(upstream:remotename)", "%(upstream:track,nobracket)",
                   "%(committerdate:unix)", "%(objectname)", "%(HEAD)"))


def after_minutes(cfg=None) -> int:
    """`backup.after_minutes`: commits younger than this wait for the backup; a wrong value falls back to 30."""
    v = ((cfg if cfg is not None else core.CFG).get("backup") or {}).get("after_minutes", AFTER_MINUTES)
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 1440 else AFTER_MINUTES


# --------------------------------------------------------------------------- the sources


_AUDIT_CACHE: dict = {}  # (file, size, mtime) -> the git.backup* records in it, so a registry build reads each once


def _backup_records(f: Path) -> list:
    try:
        st = f.stat()
    except OSError:
        return []
    key = (str(f), st.st_size, st.st_mtime_ns)
    hit = _AUDIT_CACHE.get(key)
    if hit is not None:
        return hit
    recs = []
    try:
        data = f.read_bytes()
    except OSError:
        data = b""
    if b'"git.backup' in data:
        for ln in data.splitlines():
            if b'"git.backup' not in ln:
                continue
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("action") in ("git.backup", "git.backup-skipped"):
                recs.append({"action": r["action"], "project": r.get("project") or "", "branch": r.get("branch") or "",
                             "ts": r.get("ts") or 0, "outcome": r.get("outcome") or "", "detail": r.get("detail") or ""})
    for k in [k for k in _AUDIT_CACHE if k[0] == key[0]]:
        del _AUDIT_CACHE[k]
    _AUDIT_CACHE[key] = recs
    return recs


def backups(pid: str) -> dict:
    """branch -> {"backup": ts of the newest successful git.backup, "skipped": (ts, why) of the newest
    git.backup-skipped}, from this Mac's audit trail (the last AUDIT_MONTHS monthly files)."""
    from . import audit
    d = Path(audit.AUDIT)
    files = sorted(d.glob("*.jsonl"))[-AUDIT_MONTHS:] if d.is_dir() else []
    out: dict = {}
    for f in files:
        for r in _backup_records(f):
            if r["project"] != pid or not r["branch"]:
                continue
            e = out.setdefault(r["branch"], {"backup": None, "skipped": None})
            if r["action"] == "git.backup" and r["outcome"] == "ok":
                if not e["backup"] or r["ts"] > e["backup"]:
                    e["backup"] = r["ts"]
            elif r["action"] == "git.backup-skipped" and r["detail"]:
                if not e["skipped"] or r["ts"] > e["skipped"][0]:
                    e["skipped"] = (r["ts"], r["detail"])
    return out


def embargoes(pid: str) -> dict:
    """branch -> {why, until, who, when}, from the project's handoff note (every machine sees the same)."""
    f = core.note_path(pid)
    try:
        text = f.read_text()
    except OSError:
        return {}
    return {e["branch"]: {k: e[k] for k in notes.EMBARGO_FIELDS} for e in notes.embargoes(text)}


def linked_tasks(pid: str, names) -> dict:
    """branch -> {id, title, status}: the work item that links the branch (`branch:<name>` in its links), else the one
    whose id is a part of the branch name (`fix/t-fy0000abc12-login`). Open items win over finished ones."""
    names = [n for n in names if n]
    if not names:
        return {}
    try:
        from . import work
        items = work.all_items(pid)
    except Exception:
        return {}
    rank = lambda it: it["meta"].get("status") in ("done", "cancelled")  # noqa: E731
    items = sorted(items, key=rank)
    out = {}
    for name in names:
        hit = next((it for it in items if f"branch:{name}" in (it["meta"].get("links") or [])), None)
        if hit is None:
            low = name.lower()
            hit = next((it for it in items if it["meta"].get("id") and re.search(
                r"(^|[/_-])" + re.escape(str(it["meta"]["id"]).lower()) + r"($|[/_-])", low)), None)
        if hit is not None:
            m = hit["meta"]
            out[name] = {"id": m.get("id"), "title": m.get("title") or hit.get("title") or "",
                         "status": m.get("status") or ""}
    return out


def cached_prs(slug: str):
    """The registry's last list of pull requests for `owner/repo`, whatever its age; None when there is none."""
    from . import registry
    f = Path(registry.STATE) / "prs" / (slug.replace("/", "__") + ".json")
    data = core.read_json(f, None) if f.exists() else None
    return data if isinstance(data, list) else None


def _github_slug(remote: str):
    parsed = core.parse_remote(remote) if remote else None
    if not parsed or parsed[0] != "github.com" or not parsed[1]:
        return None
    return f"{parsed[1]}/{parsed[2]}"


def _pr_row(pr_list, name):
    from . import registry
    pr = registry.pr_for_branch(pr_list, name)
    if not pr:
        return None
    state = str(pr.get("state") or "").lower()
    return {"number": pr.get("number"), "state": state, "draft": bool(pr.get("isDraft")),
            "checks": (pr.get("checks") or "unknown") if state == "open" else None,
            "url": pr.get("url") or "", "title": pr.get("title") or ""}


# --------------------------------------------------------------------------- the map


def _track(text: str):
    ahead = behind = 0
    for part in text.split(","):
        part = part.strip()
        if part.startswith("ahead "):
            ahead = int(part[6:])
        elif part.startswith("behind "):
            behind = int(part[7:])
    return ahead, behind


def _ahead_behind(top, base_ref):
    """name -> (ahead, behind) of every local branch against `base_ref`, in one call (git 2.41's ahead-behind atom);
    None when this git cannot."""
    rc, out, _ = core.git(top, "for-each-ref", f"--format=%(refname:short)%00%(ahead-behind:{base_ref})", "refs/heads",
                          timeout=60)
    if rc != 0:
        return None
    res = {}
    for ln in out.splitlines():
        name, _, ab = ln.partition("\x00")
        parts = ab.split()
        if len(parts) == 2 and all(x.isdigit() for x in parts):
            res[name] = (int(parts[0]), int(parts[1]))
    return res


def _walk(top, names, tips, exclude, with_ts=False):
    """The commits that the local branches `names` reach and `exclude` (refs, or --remotes) does not, in one
    `git rev-list` -> {sha: (commit time or None, bits)}, bit k set when names[k] reaches the commit; None when git
    fails. In topological order every child comes before its parents, so one pass hands each commit's branches down."""
    if not names:
        return {}
    args = ["rev-list", "--parents", "--topo-order"] + (["--timestamp"] if with_ts else []) + ["--not", *exclude,
                                                                                               "--stdin"]
    rc, out, _ = core.sh(["git", "-C", str(top), *args], input="".join(f"refs/heads/{n}\n" for n in names),
                         timeout=60)
    if rc != 0:
        return None
    own: dict = {}
    for k, n in enumerate(names):
        own[tips[n]] = own.get(tips[n], 0) | (1 << k)
    reach: dict = {}
    res = {}
    for ln in out.splitlines():
        parts = ln.split()
        ts = None
        if with_ts and parts:
            ts, parts = (int(parts[0]) if parts[0].isdigit() else None), parts[1:]
        if not parts:
            continue
        sha = parts[0]
        bits = reach.pop(sha, 0) | own.get(sha, 0)
        res[sha] = (ts, bits)
        for parent in parts[1:]:
            reach[parent] = reach.get(parent, 0) | bits
    return res


def _bits(bits: int, names) -> list:
    out = []
    while bits:
        low = bits & -bits
        out.append(names[low.bit_length() - 1])
        bits ^= low
    return out


def _counts(walked, names):
    """name -> (commits, oldest commit time) from a `_walk`; None when the walk failed."""
    if walked is None:
        return None
    count, oldest = [0] * len(names), [None] * len(names)
    for ts, bits in walked.values():
        while bits:
            low = bits & -bits
            k = low.bit_length() - 1
            count[k] += 1
            if ts is not None and (oldest[k] is None or ts < oldest[k]):
                oldest[k] = ts
            bits ^= low
    return {n: (count[k], oldest[k]) for k, n in enumerate(names)}


def _carrier_order(r: dict, head: str):
    # among branches at the same commit, the one that carries the backup: the default branch, then one with an
    # upstream, then the one checked out, then by name
    return (not r["default"], not (r["upstream"] and not r["upstream_gone"]), r["name"] != head, r["name"])


def map(repo, prs=None, offline=False, now=None, pid=None, default=None, current=None, checkouts=None,
        pr_max_age=900) -> list:
    """One row per local branch of the repository at `repo` (see the module docstring for the fields).

    prs: the pull requests of the repository (the registry's list); None looks them up (only the cache when
    `offline`). current: the branch to mark as current (default: the one checked out at `repo`). checkouts: the
    registry's inspected checkouts ({path, branch, modified, untracked, active, missing}), for `changes` and
    `session`."""
    from . import registry
    top = str(repo)
    now = now or time.time()
    main = registry.main_of(Path(top)) or Path(top)
    remotes = core.gitout(top, "remote").split()
    remote_url = core.gitout(top, "config", "--get", "remote.origin.url") if "origin" in remotes else ""
    pid = pid or core.project_identity(main, remote_url)[0]
    if default is None:
        default = registry.default_branch(main) if remotes else (
            next((b for b in ("main", "master", "trunk", "develop")
                  if core.git(top, "rev-parse", "--verify", "-q", f"refs/heads/{b}")[0] == 0), "main"))
    base = f"origin/{default}" if "origin" in remotes and core.git(
        top, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{default}")[0] == 0 else default
    base_ref = f"refs/remotes/{base}" if base != default else f"refs/heads/{default}"
    if core.git(top, "rev-parse", "--verify", "-q", base_ref)[0] != 0:
        base_ref = None
    raw = []
    for ln in core.gitout(top, "for-each-ref", "--format=" + _FMT, "refs/heads").splitlines():
        parts = (ln.split("\x00") + [""] * 7)[:7]
        raw.append(parts)
    if not raw:
        return []
    head = next((p[0] for p in raw if p[6].strip() == "*"), "")
    current = head if current is None else current
    wts = {}
    for w in registry.list_worktrees(main)[1:]:
        if w.get("branch") and not w.get("bare"):
            wts[w["branch"]] = w["path"]
    seen = {}
    for c in checkouts or []:
        if c.get("branch"):
            seen[c["branch"]] = c
    if prs is None:
        slug = _github_slug(remote_url)
        if slug and not offline and core.have("gh"):
            prs = registry.prs(slug, max_age=pr_max_age)
        elif slug:
            prs = cached_prs(slug)
    tips = {p[0]: p[5] for p in raw}
    names = [p[0] for p in raw]
    # a few git calls for the whole repository instead of three per branch (a session start has 30 s, R8)
    vs_base = _ahead_behind(top, base_ref) if base_ref else None
    only_here = _counts(_walk(top, names, tips, ["--remotes"] if remotes else [], with_ts=True), names)
    rows = []
    for name, upstream, uremote, track, ts, sha, _ in raw:
        ref = f"refs/heads/{name}"
        gone = track.strip() == "gone"
        ahead, behind = _track(track) if upstream and not gone else (0, 0)
        b_behind = b_ahead = 0
        if base_ref and base_ref != ref:
            if vs_base is not None and name in vs_base:
                b_ahead, b_behind = vs_base[name]
            else:
                lr = core.gitout(top, "rev-list", "--left-right", "--count", f"{base_ref}...{ref}").split()
                b_behind, b_ahead = (int(lr[0]), int(lr[1])) if len(lr) == 2 else (0, 0)
        unpushed, oldest = 0, None
        if only_here is not None and (not remotes or not (upstream and not gone and ahead == 0)):
            unpushed, oldest = only_here.get(name, (0, None))
            oldest = oldest if remotes else None
        elif not remotes:
            unpushed = int(core.gitout(top, "rev-list", "--count", ref) or 0)
        elif not (upstream and not gone and ahead == 0):
            times = [int(x) for x in core.gitout(top, "log", "--format=%ct", ref, "--not", "--remotes").split()
                     if x.isdigit()]
            unpushed, oldest = len(times), (min(times) if times else None)
        c = seen.get(name) or {}
        wt = wts.get(name)
        rows.append({
            "name": name, "current": name == current, "default": name == default, "default_branch": default,
            "sha": sha[:7], "upstream": upstream or None, "upstream_remote": uremote or None, "upstream_gone": gone,
            "ahead": ahead, "behind": behind, "base": base if base_ref else None,
            "base_ahead": b_ahead, "base_behind": b_behind,
            "where": f"local + {uremote or upstream.split('/', 1)[0]}" if upstream and not gone else "local only",
            "last_commit": int(ts) if ts.isdigit() else 0, "unpushed": unpushed, "oldest_unpushed": oldest,
            "merged_into": None, "last_backup": None, "embargo": None,
            "worktree": wt, "worktree_missing": bool(wt) and not os.path.isdir(wt),
            "changes": {"modified": c.get("modified", 0), "untracked": c.get("untracked", 0)} if c else None,
            "session": bool(c.get("active")),
            "pr": None, "task": None, "backup": None})
    by_name = {r["name"]: r for r in rows}
    off_base = _walk(top, names, tips, [base_ref]) if base_ref else None  # who carries which commit, in one walk
    for r in rows:  # merged into: the default branch when the base has every commit, else the closest container
        if r["default"]:
            continue
        if base_ref and r["base_ahead"] == 0:  # the default branch (or origin's) has every commit
            r["merged_into"] = default
            continue
        sha = tips[r["name"]]
        if off_base is not None and sha in off_base:
            found = [x for x in _bits(off_base[sha][1], names) if x != r["name"]]
        else:
            found = [x for x in core.gitout(top, "for-each-ref", "--contains", sha, "--format=%(refname:short)",
                                            "refs/heads").splitlines() if x and x != r["name"] and x in by_name]
        strict = [x for x in found if tips[x] != sha]
        same = [x for x in found if tips[x] == sha]
        if same and min([r] + [by_name[x] for x in same], key=lambda y: _carrier_order(y, head)) is not r:
            r["merged_into"] = min((by_name[x] for x in same), key=lambda y: _carrier_order(y, head))["name"]
        elif strict:
            if len(strict) > 1:
                strict.sort(key=lambda x: (int(core.gitout(top, "rev-list", "--count", f"{sha}..refs/heads/{x}") or 0),
                                           x != default, x))
            r["merged_into"] = strict[0]
    done = backups(pid)
    held = embargoes(pid)
    tasks = linked_tasks(pid, [n for n in names if n != default])
    after = after_minutes()
    for r in rows:
        rec = done.get(r["name"]) or {}
        r["last_backup"] = rec.get("backup")
        r["embargo"] = held.get(r["name"])
        r["task"] = tasks.get(r["name"])
        r["pr"] = _pr_row(prs, r["name"]) if prs else None
        r["backup"] = _backup_state(r, bool(remotes), after, now, rec.get("skipped"))
    return rows


def _backup_state(r: dict, has_remotes: bool, after: int, now: float, skipped) -> dict:
    n = r["unpushed"]
    if has_remotes and n == 0:
        return {"state": "backed_up", "reason": ""} if r["last_backup"] else {"state": "on_remote", "reason": ""}
    if r["merged_into"]:
        return {"state": "merged", "reason": f"its commits are in {r['merged_into']}"}
    if not has_remotes:
        return {"state": "no_remote", "reason": "no remote is configured"}
    if r["embargo"]:
        until = r["embargo"].get("until")
        return {"state": "embargoed", "reason": f'embargoed until "{until}"' if until else "embargoed"}
    if r["default"]:
        return {"state": "not_backed_up",
                "reason": f"{r['name']} is the default branch, which is never backed up; move "
                          f"{'this commit' if n == 1 else 'these commits'} to a branch (dev branch new <type>/<topic>)"}
    if r["oldest_unpushed"] and now - r["oldest_unpushed"] < after * 60:
        return {"state": "waiting", "reason": f"commits younger than {after} min wait for the backup"}
    if skipped and (not r["last_backup"] or skipped[0] > r["last_backup"]):
        return {"state": "not_backed_up", "reason": skipped[1]}
    reason = f"{n} commit{'s' if n != 1 else ''} only on this Mac"
    if r["upstream_gone"]:
        reason += f", and its branch on {r['upstream_remote'] or 'the remote'} was deleted"
    return {"state": "not_backed_up", "reason": reason}


# --------------------------------------------------------------------------- reading the rows


def counts(rows) -> dict:
    """What `dev overview` counts: branches, not backed up (only on this Mac), embargoed, waiting."""
    rows = rows or []
    return {"total": len(rows),
            "not_backed_up": sum(1 for r in rows if (r.get("backup") or {}).get("state") in AT_RISK),
            "embargoed": sum(1 for r in rows if r.get("embargo")),
            "waiting": sum(1 for r in rows if (r.get("backup") or {}).get("state") == "waiting")}


def ordered(rows) -> list:
    """The current branch first, then work at risk, then the newest commits; the default branch last."""
    risk = {"not_backed_up": 0, "embargoed": 1, "no_remote": 2, "waiting": 3}
    return sorted(rows or [], key=lambda r: (not r.get("current"), bool(r.get("default")),
                                             risk.get((r.get("backup") or {}).get("state"), 9),
                                             -(r.get("last_commit") or 0), r.get("name") or ""))


def who(text: str) -> str:
    """A crew member by role first ("Security engineer (The Keymaker)", R11); anyone else as written."""
    try:
        from . import crew
        m = crew.member(text)
        return crew.who(m) if m else text
    except Exception:
        return text


def _when(ts, now, absolute) -> str:
    return core.fmt_ts(ts) if absolute else ui.rel_time(ts, now)


def describe(r: dict, now=None, absolute=False) -> str:
    """The row in words, after its name and where it lives: `↑0 ↓0 · 199 ahead of main · backed up 4 min ago ·
    PR #41 draft`. absolute: dates instead of "4 min ago" (the handoff note)."""
    now = now or time.time()
    parts = []
    if r.get("upstream") and not r.get("upstream_gone"):
        parts.append(f"↑{r['ahead']} ↓{r['behind']}")
    elif r.get("unpushed") and not r.get("merged_into"):  # a merged branch's commits are the carrier's to back up
        parts.append(f"↑{r['unpushed']}")
    if r.get("merged_into"):
        parts.append(f"merged into {r['merged_into']}")
    elif r.get("default"):
        parts.append("default branch")
    elif r.get("base_ahead") or r.get("base_behind"):
        dflt = r.get("default_branch") or "main"
        parts.append(f"{r['base_ahead']} ahead of {dflt}" + (f", {r['base_behind']} behind" if r["base_behind"] else "")
                     if r.get("base_ahead") else f"{r['base_behind']} behind {dflt}")
    b = r.get("backup") or {}
    st = b.get("state")
    if st == "backed_up":
        parts.append("backed up " + _when(r.get("last_backup"), now, absolute))
    elif st in ("on_remote", "merged"):
        parts.append("nothing to back up")
    elif st == "waiting":
        parts.append("backup waits: " + b.get("reason", ""))
    elif st:
        text = "not backed up: " + b.get("reason", "")
        if st == "not_backed_up" and r.get("oldest_unpushed") and b.get("reason", "").endswith("only on this Mac"):
            text += ", oldest commit " + _when(r["oldest_unpushed"], now, absolute)
        parts.append(text)
    emb = r.get("embargo")
    if emb and st != "embargoed":
        parts.append(f'embargoed until "{emb["until"]}"' if emb.get("until") else "embargoed")
    pr = r.get("pr")
    if pr:
        state = "draft" if pr.get("state") == "open" and pr.get("draft") else pr.get("state")
        parts.append(f"PR #{pr['number']} {state}" + (f" · checks {pr['checks']}" if pr.get("checks") in (
            "failing", "pending", "passing") else ""))
    if r.get("task"):
        parts.append(f"task {r['task']['id']}")
    return " · ".join(parts)


NOTE_ROWS = 20  # the handoff note lists this many branches (work at risk first); `dev branch ls` lists them all


def note_lines(rows) -> list:
    """The handoff note's rows (in the generated block): one bullet per branch, with absolute times."""
    out = []
    rows = ordered(rows)
    for r in rows[:NOTE_ROWS]:
        line = f"  - `{r['name']}` · {r['where']} · {describe(r, absolute=True)}"
        if r.get("worktree"):
            line += f" · worktree `{core.tilde(r['worktree'])}`" + (" (missing)" if r.get("worktree_missing") else "")
        ch = r.get("changes") or {}
        dirty = ", ".join(x for x in (f"{ch['modified']} modified" if ch.get("modified") else "",
                                      f"{ch['untracked']} untracked" if ch.get("untracked") else "") if x)
        if dirty:
            line += f" · {dirty}"
        if r.get("session"):
            line += " · active session"
        emb = r.get("embargo")
        if emb and (emb.get("why") or emb.get("who")):
            line += " · embargo" + (f" by {who(emb['who'])}" if emb.get("who") else "") + (
                f": {emb['why']}" if emb.get("why") else "")
        out.append(line)
    if len(rows) > NOTE_ROWS:
        out.append(f"  - and {len(rows) - NOTE_ROWS} more branches (`dev branch ls`)")
    return out


def _columns(rows, cols, limit=None):
    rows = ordered(rows)
    shown = rows[:limit] if limit else rows
    nw = min(max((len(r["name"]) for r in shown), default=0), 34)
    ww = max((len(r["where"]) for r in shown), default=0)
    return rows, shown, nw, ww


def status_lines(rows, cols=None, now=None, limit=8) -> list:
    """`dev status`'s Branches section: one row per branch (the spec's layout), the words wrapped under their column."""
    if not rows:
        return []
    cols = cols or ui.width()
    rows, shown, nw, ww = _columns(rows, cols, limit)
    lead = 2 + nw + 2 + ww + 2
    room = ui.text_width(cols) - lead - 1
    out = ["", "  " + ui.bold("Branches")]
    for r in shown:
        name = r["name"] if len(r["name"]) <= nw else r["name"][:nw - 1] + ui.glyph("ellipsis")
        words = ui.wrap(describe(r, now=now), room)
        out.append("  " + name.ljust(nw) + "  " + r["where"].ljust(ww) + "  " + words[0])
        out += [" " * lead + w for w in words[1:]]
    if len(rows) > len(shown):
        out.append("  " + ui.dim(f"and {len(rows) - len(shown)} more (dev branch ls)"))
    return out


def ls_lines(rows, cols=None, now=None, title="") -> list:
    """`dev branch ls`: every branch, with its worktree, task, pull request link and embargo on the lines under it."""
    cols = cols or ui.width()
    if not rows:
        return ["  no local branches"]
    rows, _, _, ww = _columns(rows, cols)
    nw = max(len(r["name"]) for r in rows)
    lead = 2 + 2 + nw + 2 + ww + 2
    room = ui.text_width(cols) - lead - 1
    out = [ui.rule_title(title, cols)] if title else []
    for r in rows:
        words = ui.wrap(describe(r, now=now), room)
        out.append("  " + ("* " if r.get("current") else "  ") + r["name"].ljust(nw) + "  " + r["where"].ljust(ww)
                   + "  " + words[0])
        out += [" " * lead + w for w in words[1:]]
        more = []
        if r.get("worktree"):
            gone = " (missing)" if r.get("worktree_missing") else ""
            more.append("worktree " + ui.short_path(r["worktree"], max(20, room - 9 - len(gone))) + gone)
        if r.get("task"):
            more.append(f"task {r['task']['id']}: {r['task']['title']}")
        if (r.get("pr") or {}).get("url"):
            more.append(r["pr"]["url"])
        emb = r.get("embargo")
        if emb:
            more.append("embargo" + (f" by {who(emb['who'])}" if emb.get("who") else "")
                        + (f" on {emb['when'][:10]}" if emb.get("when") else "") + (f": {emb['why']}" if emb.get("why")
                                                                                    else ""))
        for m in more:
            out += [" " * lead + ui.dim(w) for w in ui.wrap(m, room)]
    return out


def context_line(rows) -> str:
    """The session-start context's line: how many branches, which ones exist only on this Mac, which are embargoed."""
    if not rows:
        return ""
    risky = [r for r in ordered(rows) if (r.get("backup") or {}).get("state") in AT_RISK]
    held = [r for r in ordered(rows) if r.get("embargo")]
    parts = [f"Branches: {len(rows)}"]
    if risky:
        shown = []
        for r in risky[:4]:
            n = r.get("unpushed") or 0
            why = (f"{n} commit{'s' if n != 1 else ''} only on this Mac" if r["backup"]["state"] == "not_backed_up"
                   and r["backup"]["reason"].endswith("only on this Mac") else r["backup"]["reason"])
            shown.append(f"`{r['name']}` ({why})")
        parts.append("not backed up: " + ", ".join(shown) + (f" and {len(risky) - 4} more" if len(risky) > 4 else ""))
    if held:
        parts.append("embargoed: " + ", ".join(f"`{r['name']}`" for r in held[:4])
                     + (f" and {len(held) - 4} more" if len(held) > 4 else "") + " (a backup never pushes them)")
    if not risky and not held:
        parts.append("nothing is only on this Mac")
    return " · ".join(parts) + " — `dev branch ls` shows the map."


# --------------------------------------------------------------------------- dev branch ls


def cmd_ls(a) -> int:
    """`dev branch ls [--offline] [--json]`: the full branch map of the repository you are in."""
    top = core.gitout(os.getcwd(), "rev-parse", "--show-toplevel")
    if not top:
        print(f"{ui.WARN} not inside a git repository", file=sys.stderr)
        return 2
    from . import registry
    main = registry.main_of(Path(top)) or Path(top)
    here = core.gitout(top, "branch", "--show-current")
    rows = map(main, offline=bool(getattr(a, "offline", False)), current=here)
    if getattr(a, "json", False):
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    c = counts(rows)
    pid = core.project_identity(main, core.gitout(top, "config", "--get", "remote.origin.url"))[0]
    head = f"{pid} · {c['total']} branch{'es' if c['total'] != 1 else ''}"
    if c["not_backed_up"]:
        head += f" · {c['not_backed_up']} not backed up"
    if c["embargoed"]:
        head += f" · {c['embargoed']} embargoed"
    print("\n".join(ls_lines(rows, title=head)))
    print("  " + ui.dim(f"Backups wait for commits older than {after_minutes()} min (backup.after_minutes)."))
    return 0
