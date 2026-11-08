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
    merged_how                                              ancestor, or content (git cherry: every commit has an
                                                            equivalent in the default branch), or null
    last_backup                                             unix time of the newest git.backup record, or null
    embargo                                                 {why, until, who, when} or null
    embargo_via                                             the embargoed branch that carries its commits, or null
    backup_policy                                           the project's recorded {policy, why, who, when}, or null
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


SHA_RE = re.compile(r"[0-9a-f]{40}")
TIPS_KEPT = 20  # the newest tips of an embargoed branch the note keeps


def note_text(text: str) -> str:
    """A note's text as devkit reads it: no byte-order mark, LF line endings. An editor that saves a BOM or CRLF must
    never turn the embargo list or the backup policy into "none" (B2a; the guard reads notes the same way)."""
    return str(text or "").lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")


def _note_meta(pid: str):
    """The metadata of the project's handoff note -> (meta, "") or (None, why devkit cannot read it). No note in a
    handoff repository that is there reads as no metadata; a missing repository, a file that cannot be read, a block
    that never ends or an embargo list that does not parse is unreadable (B2: never "no embargo")."""
    f = core.note_path(pid)
    try:
        text = note_text(f.read_text())
    except FileNotFoundError:
        if not Path(core.HANDOFF).is_dir():
            return None, f"the handoff repository {core.tilde(core.HANDOFF)} is not on this Mac"
        return {}, ""
    except (OSError, UnicodeDecodeError) as e:
        return None, f"{core.tilde(f)} cannot be read ({getattr(e, 'strerror', None) or type(e).__name__})"
    if not text.startswith("---\n"):
        return {}, ""
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, f"the metadata block of {core.tilde(f)} never ends (no closing ---)"
    meta, _ = notes.split_frontmatter(text)
    if re.search(r"(?m)^embargo:", text[4:end]) and not isinstance(meta.get("embargo"), list):
        return None, f"the embargo list in {core.tilde(f)} does not parse"
    return meta, ""


_TRAIL_CACHE: dict = {}  # (file, size, mtime) -> the git.embargo records in it


def _embargo_records(f: Path) -> list:
    try:
        st = f.stat()
    except OSError:
        return []
    key = (str(f), st.st_size, st.st_mtime_ns)
    hit = _TRAIL_CACHE.get(key)
    if hit is not None:
        return hit
    recs = []
    try:
        data = f.read_bytes()
    except OSError:
        data = b""
    if b'"git.embargo"' in data:
        for ln in data.splitlines():
            if b'"git.embargo"' not in ln:
                continue
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("action") == "git.embargo" and r.get("outcome") == "ok":
                recs.append({"id": r.get("id") or "", "project": r.get("project") or "",
                             "branch": r.get("branch") or r.get("target") or "", "ts": r.get("ts") or 0,
                             "detail": str(r.get("detail") or ""), "who": str((r.get("actor") or {}).get("user") or ""),
                             "time": str(r.get("time") or ""),
                             "commit": str((r.get("evidence") or {}).get("commit") or "")})
    for k in [k for k in _TRAIL_CACHE if k[0] == key[0]]:
        del _TRAIL_CACHE[k]
    _TRAIL_CACHE[key] = recs
    return recs


def embargo_trail(pid: str) -> dict:
    """branch -> {who, when, commits} for each embargo whose newest `git.embargo` record sets it (B2a): this Mac's
    audit trail and the trails the team's machines publish to the handoff repository, which agents may not write.
    `dev branch embargo` records each set (with the tip) and `--lift` each lift, so an embargo the trail still holds
    was not lifted, whatever the note says now."""
    from . import audit
    files = sorted(Path(audit.AUDIT).glob("*.jsonl")) if Path(audit.AUDIT).is_dir() else []
    team = Path(core.HANDOFF) / "audit"
    files += sorted(team.rglob("*.jsonl")) if team.is_dir() else []
    seen, recs = set(), []
    for f in files:
        for r in _embargo_records(f):
            if r["project"] == pid and r["branch"] and r["id"] not in seen:
                seen.add(r["id"])
                recs.append(r)
    out: dict = {}
    for r in sorted(recs, key=lambda r: r["ts"]):
        if r["detail"].startswith("lifted"):
            out.pop(r["branch"], None)
        elif r["detail"].startswith("set"):
            e = out.setdefault(r["branch"], {"who": r["who"], "when": r["time"], "commits": []})
            if SHA_RE.fullmatch(r["commit"]) and r["commit"] not in e["commits"]:
                e["commits"].append(r["commit"])
    return out


def read_embargoes(pid: str):
    """branch -> {why, until, who, when, commits}, from the project's handoff note (every machine sees the same) ->
    (embargoes, "") or (None, why the note cannot be read). commits: the branch's tips that devkit recorded, so its
    commits stay held after it is merged elsewhere and deleted. An embargo the audit trail holds that is no longer in
    the note (a hand edit, a merge, an older note checked out) keeps holding until `dev branch embargo --lift` (B2a)."""
    meta, why = _note_meta(pid)
    if meta is None:
        return None, why
    found = meta.get("embargo")
    out = {}
    for rec in found if isinstance(found, list) else []:
        if isinstance(rec, dict) and rec.get("branch"):
            out[rec["branch"]] = dict({f: rec.get(f, "") for f in notes.EMBARGO_FIELDS},
                                      commits=SHA_RE.findall(str(rec.get("commits") or "")))
    for b, t in embargo_trail(pid).items():
        if b not in out:
            out[b] = {"why": f"its embargo left the handoff note without its lift (dev branch embargo --lift {b}), "
                             f"so devkit keeps holding it", "until": "", "who": t["who"], "when": t["when"],
                      "commits": t["commits"]}
        else:
            out[b]["commits"] += [c for c in t["commits"] if c not in out[b]["commits"]]
    return out, ""


def embargoes(pid: str) -> dict:
    """branch -> {why, until, who, when}, for the views; a note that cannot be read shows none (a backup refuses)."""
    held, _ = read_embargoes(pid)
    return {b: {k: e[k] for k in notes.EMBARGO_FIELDS} for b, e in (held or {}).items()}


def record_tips(pid: str, tips: dict) -> None:
    """Add branch tips to the embargo records ({branch: sha}), keeping the newest TIPS_KEPT per branch."""
    tips = {b: t for b, t in tips.items() if SHA_RE.fullmatch(t or "")}
    if not tips:
        return

    def edit(recs):
        for r in recs:
            t = tips.get(r.get("branch"))
            have = SHA_RE.findall(str(r.get("commits") or ""))
            if t and t not in have:
                r["commits"] = " ".join((have + [t])[-TIPS_KEPT:])
        return recs
    _edit_records(pid, "embargo", edit)


def held_commits(top, branch: str, rec: dict, local, exclude=("--remotes",)) -> set:
    """The commits an embargo holds: those its branch (when it is here) and its recorded tips reach, and none of
    `exclude` (rev-list arguments after --not: --remotes, or the commits a push target has) does."""
    tips = ([f"refs/heads/{branch}"] if branch in local else []) + [
        t for t in rec.get("commits") or [] if core.git(top, "cat-file", "-e", t + "^{commit}")[0] == 0]
    if not tips:
        return set()
    return set(core.gitout(top, "rev-list", *tips, "--not", *exclude, timeout=60).split())


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


CONTENT_DAYS = 7  # how far before a branch's oldest own commit the default branch is searched for its equivalents


def _content_merged(top, base_ref: str, off_base, names, cands) -> list:
    """The branches of `cands` whose every own commit (off `base_ref`, merges left out, as git cherry does) has an
    equivalent in `base_ref`: the same patch id among its commits since CONTENT_DAYS before the oldest of them. Two git
    calls for the whole repository, where git cherry takes one per branch (the session start has 30 s)."""
    if off_base is None:
        return []
    want = {n: 1 << k for k, n in enumerate(names) if n in cands}
    mask = 0
    for b in want.values():
        mask |= b
    own = {sha: bits for sha, (_, bits) in off_base.items() if bits & mask}
    stamps = [ts for sha, (ts, _) in off_base.items() if sha in own and ts]
    if not own or not stamps:
        return []
    since = min(stamps) - CONTENT_DAYS * 86400
    rc, patches, _ = core.sh(["git", "-C", str(top), "log", "-p", "--no-merges", "--format=commit %H",
                              f"--since={since}", base_ref, "--stdin"], input="".join(f"{s}\n" for s in own),
                             timeout=120)
    if rc != 0:
        return []
    listed = {ln[7:47] for ln in patches.splitlines() if ln.startswith("commit ") and len(ln) >= 47}
    rc, ids, _ = core.sh(["git", "-C", str(top), "patch-id", "--stable"], input=patches + "\n", timeout=120)
    if rc != 0:
        return []
    pid_of = {}
    for ln in ids.splitlines():
        parts = ln.split()
        if len(parts) == 2:
            pid_of[parts[1]] = parts[0]
    landed = {p for sha, p in pid_of.items() if sha not in own}
    out = []
    for name, bit in want.items():
        mine = [sha for sha, bits in own.items() if bits & bit and sha in listed]  # its non-merge commits
        if mine and all(pid_of.get(sha) in landed for sha in mine):
            out.append(name)
    return out


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
    walked_here = _walk(top, names, tips, ["--remotes"] if remotes else [], with_ts=True)
    only_here = _counts(walked_here, names)
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
    off_base = _walk(top, names, tips, [base_ref], with_ts=True) if base_ref else None  # who carries which commit
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
    for r in rows:
        r["merged_how"] = "ancestor" if r["merged_into"] else None
    # merged by content (the merge test): cherry-picked or carried by a release branch. Without this the hourly backup
    # pushed such a branch back to origin after its remote branch had been deleted.
    cands = [r["name"] for r in rows if not r["default"] and not r["merged_into"] and r["unpushed"]]
    for name in _content_merged(top, base_ref, off_base, names, cands) if base_ref and cands else ():
        by_name[name]["merged_into"], by_name[name]["merged_how"] = default, "content"
    done = backups(pid)
    held_full = read_embargoes(pid)[0] or {}
    held = {b: {k: e[k] for k in notes.EMBARGO_FIELDS} for b, e in held_full.items()}
    via = {}  # branch -> the embargoed branch whose held commits it carries (B2: also after that branch is deleted)
    for b, e in held_full.items():
        for sha in held_commits(top, b, e, tips, ["--remotes"] if remotes else []) if walked_here else ():
            for n in _bits((walked_here.get(sha) or (None, 0))[1], names):
                if n != b and n not in held:
                    via.setdefault(n, b)
    policy = backup_policy(pid)
    tasks = linked_tasks(pid, [n for n in names if n != default])
    after = after_minutes()
    for r in rows:
        rec = done.get(r["name"]) or {}
        r["last_backup"] = rec.get("backup")
        r["embargo"] = held.get(r["name"])
        # a merged branch defers to the branch that carries its commits, and so does that branch's embargo
        r["embargo_via"] = r["merged_into"] if not r["embargo"] and r["unpushed"] and r["merged_into"] in held else \
            via.get(r["name"]) if not r["embargo"] and r["unpushed"] else None
        r["task"] = tasks.get(r["name"])
        r["backup_policy"] = policy
        r["pr"] = _pr_row(prs, r["name"]) if prs else None
        r["backup"] = _backup_state(r, bool(remotes), after, now, rec.get("skipped"))
    return rows


def _backup_state(r: dict, has_remotes: bool, after: int, now: float, skipped) -> dict:
    n = r["unpushed"]
    if has_remotes and n == 0:
        return {"state": "backed_up", "reason": ""} if r["last_backup"] else {"state": "on_remote", "reason": ""}
    if r["merged_into"] and r.get("merged_how") == "content":
        return {"state": "merged", "reason": f"its commits have an equivalent in {r['merged_into']} (merged by content)"}
    if r["merged_into"]:
        return {"state": "merged", "reason": f"its commits are in {r['merged_into']}"}
    if not has_remotes:
        return {"state": "no_remote", "reason": "no remote is configured"}
    if r["embargo"]:
        until = r["embargo"].get("until")
        return {"state": "embargoed", "reason": f'embargoed until "{until}"' if until else "embargoed"}
    if r.get("embargo_via"):
        return {"state": "embargoed", "reason": f"its commits are in {r['embargo_via']}, which is embargoed"}
    if r["default"]:
        return {"state": "not_backed_up",
                "reason": f"{r['name']} is the default branch, which is never backed up; move "
                          f"{'this commit' if n == 1 else 'these commits'} to a branch (dev branch new <type>/<topic>)"}
    if (r.get("backup_policy") or {}).get("policy") == "off":
        return {"state": "not_backed_up", "reason": "the project's backup policy is off (dev backup policy)"}
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
    if r.get("embargo_via"):
        parts.append(f"held by the embargo on {r['embargo_via']}")
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
    if rows:
        out.append(f"  - backup policy: {policy_words(rows[0].get('backup_policy'))}")
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
    out += ["  " + ui.dim(w) for w in ui.wrap("Backup policy: " + policy_words(rows[0].get("backup_policy")),
                                             ui.text_width(cols) - 3)]
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
    print("  " + ui.dim(f"Backup policy: {policy_words(backup_policy(pid))} (dev backup policy)."))
    print("  " + ui.dim(f"Backups wait for commits older than {after_minutes()} min (backup.after_minutes)."))
    return 0


# --------------------------------------------------------------------------- dev branch embargo (R3)


def _edit_records(pid: str, key: str, edit) -> None:
    """Rewrite the `key:` record list of the project's handoff note metadata under the notes lock: edit(records) ->
    the new records (an empty list drops the key)."""
    f = core.note_path(pid)
    with core.Lock("notes", wait=30):
        text = note_text(f.read_text()) if f.exists() else notes.new_note(pid)
        meta, body = notes.split_frontmatter(text)
        found = meta.get(key)
        recs = edit([r for r in found if isinstance(r, dict)] if isinstance(found, list) else [])
        if recs:
            meta[key] = recs
        else:
            meta.pop(key, None)
        f.parent.mkdir(parents=True, exist_ok=True)
        core.write_if_changed(f, notes.set_meta(notes.render_frontmatter(meta) + body.lstrip("\n")))


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def set_embargo(pid: str, branch: str, why: str = "", until: str = "", who: str = "", lift: bool = False):
    """Write a branch's embargo into the project's handoff note metadata (`embargo:`), or lift it -> the record set,
    or with `lift` the one removed (None when there was none). Every machine reads the same note."""
    box = {"old": None, "rec": None}

    def edit(recs):
        recs = [r for r in recs if r.get("branch")]
        box["old"] = next((r for r in recs if r["branch"] == branch), None)
        recs = [r for r in recs if r["branch"] != branch]
        if not lift:
            rec = {"branch": branch, "why": why}
            if until:
                rec["until"] = until
            rec.update(who=who, when=_now_iso())
            box["rec"] = rec
            recs.append(rec)
        return recs

    _edit_records(pid, "embargo", edit)
    return box["old"] if lift else box["rec"]


# --------------------------------------------------------------------------- the backup policy (decided 2026-11-06)
# Each project chooses whether its branches are backed up: on (dev backup and the hourly dev sync push them, with
# every check), off (never), or ask (dev backup asks the developer once, in a terminal, and records the answer as on
# or off; dev sync waits for it). Without a choice, private repositories are on and public ones ask (a repository
# whose visibility devkit cannot read counts as public), because a backup of a public repository publishes the
# branch at once. The choice stays in the handoff note (`backup_policy:` with why, who and when), so every machine
# and the team follow it. Setting it is the developer's action, like the one-time `dev backup --public`.

POLICY_KEY = "backup_policy"
POLICIES = ("on", "off", "ask")


def backup_policy(pid: str):
    """The project's recorded choice {policy, why, who, when}, or None: the default by visibility applies."""
    try:
        text = note_text(core.note_path(pid).read_text())
    except OSError:
        return None
    meta, _ = notes.split_frontmatter(text)
    found = meta.get(POLICY_KEY)
    for rec in reversed(found if isinstance(found, list) else []):
        if isinstance(rec, dict) and rec.get("policy") in POLICIES:
            return {"policy": rec["policy"], "why": rec.get("why") or "", "who": rec.get("who") or "",
                    "when": rec.get("when") or "", "visibility": rec.get("visibility") or ""}
    return None


def set_backup_policy(pid: str, policy: str, why: str, who: str, visibility=None) -> dict:
    """Record the project's choice (it replaces the one before) with the visibility of the repository it was decided
    for ("public", "private", "internal"; None when devkit could not read it, recorded as "unknown") -> the record."""
    if policy not in POLICIES:
        raise ValueError(f"the backup policy is one of {', '.join(POLICIES)}, not {policy!r}")
    rec = {"policy": policy, "why": why, "who": who, "when": _now_iso(), "visibility": visibility or "unknown"}
    _edit_records(pid, POLICY_KEY, lambda recs: [rec])
    return dict(rec)


def outgrown_on(rec, visibility) -> bool:
    """True when a recorded on no longer holds: the repository is not known to be private now (public, or devkit
    cannot read it) and on was decided for another visibility, or before devkit kept it (no visibility recorded: it
    may have been decided while the repository was private). An on decided while devkit could not read the
    visibility ("unknown") holds while it still cannot. Backups then wait for the developer's answer again, so a
    repository made public, or one devkit cannot read (gh failing, offline), stops publishing with the hourly dev
    sync."""
    if not rec or rec.get("policy") != "on" or visibility in ("private", "internal"):
        return False
    then = rec.get("visibility") or ""
    return not then or then != (visibility or "unknown")


def default_policy(visibility) -> str:
    """The policy without a recorded choice: on for a private (or internal) repository, else ask."""
    return "on" if visibility in ("private", "internal") else "ask"


def policy_words(rec, visibility="?") -> str:
    """The policy in words: `on, set by Ana on 2026-11-06: <why>`, or `ask, the default for a public repository`.
    visibility "?" means devkit did not look (the views read no network)."""
    if rec:
        return (f"{rec['policy']}, set by {who(rec['who']) if rec.get('who') else 'the developer'}"
                f"{' on ' + rec['when'][:10] if rec.get('when') else ''}" + (f": {rec['why']}" if rec.get("why") else ""))
    if visibility == "?":
        return "the default: on for a private repository, ask for a public one"
    kind = "private" if visibility in ("private", "internal") else "public" if visibility == "public" else \
        "repository whose visibility devkit cannot read, counted as public"
    return f"{default_policy(visibility)}, the default for a {kind}" + ("" if " " in kind else " repository")


def _actor() -> str:
    """Who sets an embargo: the crew member whose lane runs the command, else the developer."""
    try:
        from . import audit
        member = ((audit.context() or {}).get("crew") or {}).get("member")
        if member:
            return member
    except Exception:
        pass
    from . import work
    return work.actor()["user"]


def cmd_embargo(a) -> int:
    """`dev branch embargo [<branch> --why "…" [--until "…"]] | --lift <branch>`: no branch lists the embargoes."""
    top = core.gitout(os.getcwd(), "rev-parse", "--show-toplevel")
    if not top:
        print(f"{ui.WARN} not inside a git repository", file=sys.stderr)
        return 2
    from . import audit, registry
    main = registry.main_of(Path(top)) or Path(top)
    pid = core.project_identity(main, core.gitout(top, "config", "--get", "remote.origin.url"))[0]
    held = embargoes(pid)
    b = (a.branch or "").strip()
    if not b:
        if a.lift:
            print(f'{ui.WARN} name the branch: dev branch embargo --lift <branch>', file=sys.stderr)
            return 2
        if getattr(a, "json", False):
            print(json.dumps([{"branch": k, **v} for k, v in held.items()], indent=2, ensure_ascii=False))
        elif not held:
            print(f"{ui.INFO} {pid}: no embargoed branch")
        else:
            for k, v in held.items():
                print(f"  {k}" + (f' until "{v["until"]}"' if v.get("until") else "") + ui.dim(
                    f" · {who(v['who'])}" if v.get("who") else "") + ui.dim(f" · {v['why']}" if v.get("why") else ""))
        return 0
    if a.lift:
        from . import backup
        if backup.crew_member():  # the guard asks the main session for the developer's grant (grant embargo <branch>)
            print(f"{ui.WARN} dev branch embargo --lift is the developer's own decision: lifting it lets a backup "
                  f"publish {b}'s commits. The developer types it in a terminal, or grants the main session once "
                  f"(grant embargo {b}). A crew member never runs it: put the exact command and why in your report.",
                  file=sys.stderr)
            return 2
        old = set_embargo(pid, b, lift=True) or held.get(b)  # also one that left the note without its lift (B2a)
        if not old:
            print(f"{ui.INFO} {b} has no embargo")
            return 0
        audit.record("git.embargo", b, "lifted" + (f" (it was: {old.get('why')})" if old.get("why") else ""), "ok",
                     project=pid, branch=b, category="security", severity="notice")
        _share(b)
        print(f"{ui.OK} {b}: embargo lifted · the next backup may push it (dev backup {b})")
        return 0
    why = (a.why or "").strip()
    if not why:
        print(f'{ui.WARN} an embargo needs its reason: dev branch embargo {b} --why "…" [--until "<step or date>"]',
              file=sys.stderr)
        return 2
    default = registry.default_branch(main) if core.gitout(top, "remote") else ""
    if b == default:
        print(f"{ui.WARN} {b} is the default branch, which a backup never pushes", file=sys.stderr)
        return 2
    until = (a.until or "").strip()
    set_embargo(pid, b, why, until, _actor())
    tip = core.gitout(top, "rev-parse", "--verify", "-q", f"refs/heads/{b}^{{commit}}")
    record_tips(pid, {b: tip})
    audit.record("git.embargo", b, f"set: {why}" + (f' (until "{until}")' if until else ""), "ok", project=pid,
                 branch=b, commit=tip, category="security", severity="notice")  # the trail keeps it (B2a)
    _share(b)
    on_remote = [x for x in core.gitout(top, "for-each-ref", "--format=%(refname:short)",
                                        f"refs/remotes/*/{b}").splitlines() if x]
    print(f"{ui.OK} {b} is embargoed" + (f' until "{until}"' if until else "") + ": dev backup and dev sync never "
          f"push it · lift it: dev branch embargo --lift {b}")
    if not core.gitout(top, "rev-parse", "--verify", "-q", f"refs/heads/{b}"):
        print(f"  {ui.dim('there is no local branch ' + b + ' yet: the embargo waits for it')}")
    if on_remote:
        print(f"  {ui.WARN} {on_remote[0]} exists already: the embargo keeps new commits off it, what is there is "
              f"public")
    return 0


def _share(branch: str, what: str = "embargo") -> None:
    """Commit and push the handoff note soon, so every machine sees the embargo (or the answer) before its next
    backup."""
    try:
        from . import work
        work.request_sync(f"{what} {branch}")
    except Exception:
        pass


# --------------------------------------------------------------------------- the merge test
# One test decides whether a branch is merged, for `dev branch prune`, `dev branch clean`, the nightly gc, the worktree
# cleanup, `dev pr merge` and `dev release`, and every one of them says which way it holds.

HOWS = ("ancestor", "pr", "content")


def _sha(top, ref) -> str:
    return core.gitout(top, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}") if ref else ""


def _ancestor(top, a, b) -> bool:
    return core.git(top, "merge-base", "--is-ancestor", a, b, timeout=60)[0] == 0


def merge_test(top, name: str, base: str, ref: str = None, prs=None) -> dict:
    """Is the branch `name` (its `ref`, refs/heads/<name> unless given) merged into `base` (origin/<default>)?

      ancestor  its tip is in `base`: a merge commit or a fast-forward;
      pr        a merged pull request of the branch (any method: merge, squash or rebase) had its tip, or a later
                commit, as its head, so nothing was added after the merge; it came from the repository itself (a
                fork's branch can carry any name and head) and its merge commit is in `base` (not merged elsewhere);
      content   every commit `base` lacks has an equivalent there (`git cherry`: all "-"): cherry-picked, or carried by
                a release branch that merged; merge commits are left out, as git cherry leaves them out.

    prs: the repository's pull requests ({number, state, headRefName, headRefOid, isCrossRepository, mergeCommit});
    none skips the pull-request test.
    -> {merged, how (one of HOWS or None), pr, sha, ahead (commits `base` lacks), missing (of them, those without an
    equivalent; None when git cherry could not tell)}."""
    sha = _sha(top, ref or f"refs/heads/{name}")
    out = {"merged": False, "how": None, "pr": None, "sha": sha, "ahead": 0, "missing": None}
    if not sha or not _sha(top, base):
        return out
    if _ancestor(top, sha, base):
        return dict(out, merged=True, how="ancestor", missing=0)
    n = core.gitout(top, "rev-list", "--count", f"{base}..{sha}", timeout=60)
    out["ahead"] = int(n) if n.isdigit() else 0
    for p in prs or []:
        head = str(p.get("headRefOid") or "")
        landed = str((p.get("mergeCommit") or {}).get("oid") or "")
        if p.get("headRefName") != name or str(p.get("state") or "").upper() != "MERGED" or not head or \
                p.get("isCrossRepository") is not False or not landed or not _sha(top, landed) or \
                not _ancestor(top, landed, base):
            continue
        if head == sha or (_sha(top, head) and _ancestor(top, sha, head)):
            return dict(out, merged=True, how="pr", pr=p.get("number"), missing=0)
    rc, cherry, _ = core.git(top, "cherry", base, sha, timeout=120)
    if rc != 0:
        return out
    marks = [ln[:1] for ln in cherry.splitlines() if ln[:1] in ("+", "-")]
    out["missing"] = marks.count("+")
    if marks and not out["missing"]:
        return dict(out, merged=True, how="content")
    return out


def _commits(n: int) -> str:
    return f"{n} commit{'' if n == 1 else 's'}"


def merge_words(m: dict, base: str) -> str:
    """The merge test's answer in words, for every output that closes or keeps a branch."""
    if not m.get("sha"):
        return "the branch does not exist"
    if m.get("how") == "ancestor":
        return f"merged: its tip is in {base}"
    if m.get("how") == "pr":
        return f"merged: PR #{m.get('pr')} was merged"
    if m.get("how") == "content":
        n = m.get("ahead") or 0
        return f"merged by content: {_commits(n)} {'has' if n == 1 else 'have'} an equivalent in {base}"
    ahead, missing = m.get("ahead") or 0, m.get("missing")
    if missing is None or missing == ahead:
        return f"not merged: {_commits(ahead)} not in {base}"
    return f"not merged: {missing} of {_commits(ahead)} not in {base}"


# --------------------------------------------------------------------------- dev branch prune
# Every local and remote branch with its state; with --apply the merged ones go: the remote branch, the local branch
# and its worktree when that is clean. What nothing may delete: the default branch and the permanent ones, the
# current branch, a branch with an open pull request, an embargoed branch, a worktree with uncommitted changes, a
# lock or a live session, and a branch with commits the default branch does not have by content.

PRUNE_STATES = ("merged", "open_pr", "not_merged", "protected")
FRESH_HOURS = 24  # a branch that never had a commit of its own is kept this long after it was created


def _live_tops() -> set:
    """The real paths where a devkit session is live now, from its session records (registry._session_live)."""
    from . import registry
    out = set()
    d = Path(registry.STATE) / "sessions"
    for f in d.glob("*.json") if d.is_dir() else ():
        s = core.read_json(f, {}) or {}
        top = s.get("top") or ""
        if isinstance(s, dict) and top and registry._session_live(s, top):
            out.add(core.real(top))
    return out


def _created_only(top, name: str, now: float):
    """When the branch `name` was created, if that is all its reflog holds and it was less than FRESH_HOURS ago (it
    never had a commit of its own yet); else None."""
    lines = core.gitout(top, "reflog", "show", "--date=unix", "--format=%gd%x09%gs", f"refs/heads/{name}", "--",
                        timeout=30).splitlines()
    if len(lines) != 1:
        return None
    when, _, what = lines[0].partition("\t")  # feat/x@{1794119002}: when the entry was written, not the commit's date
    found = re.search(r"@\{(\d+)\}$", when)
    ts = int(found.group(1)) if found else 0
    return ts if what.startswith("branch: Created from") and now - ts < FRESH_HOURS * 3600 else None


def _worktree_hold(top, w: dict, live: set):
    """Why a merged branch's worktree must stay, or None when it can go."""
    from . import registry
    path = w["path"]
    if not os.path.isdir(path):
        return None
    if w.get("locked"):
        return "its worktree is locked"
    if registry.classify_location(Path(path), Path(top)) == "codex":
        return "the Codex app manages its worktree"
    if core.gitout(path, "status", "--porcelain", timeout=60):
        return "its worktree has uncommitted changes"
    if core.real(path) in live or core.is_active(path):
        return "a live session works in its worktree"
    return None


def prune_plan(top, prs=None, offline=False, where=("local", "remote"), now=None) -> dict:
    """What `dev branch prune` would do in the repository at `top`: {project, base, default, applied, branches: rows}.
    A row: name, where (local|remote), sha, state (PRUNE_STATES), how (merge_test), pr, ahead, missing, last_commit,
    merge (the merge test in words), worktree, action (delete|keep), why, backup (devkit pushed it: a git.backup record),
    pr_merged (a merged pull request of the branch exists). Fetches origin first unless `offline`; changes nothing."""
    from . import gitx, registry
    now = now or time.time()
    c = gitx.ctx(top)
    if c is None:
        raise ValueError("not inside a git repository")
    if not offline and c.has_origin:
        gitx._fetch(c)
    base = gitx._base_ref(c)
    github = c.provider == "github"
    if prs is None and github:
        prs = cached_prs(c.slug) if offline else gitx._prs(c, limit=200)
    held, unreadable = read_embargoes(c.pid)
    done = backups(c.pid)
    trees = registry.list_worktrees(Path(c.main))
    in_main = (trees[:1] or [{}])[0].get("branch")
    wts = {w["branch"]: w for w in trees[1:] if w.get("branch")}
    live = _live_tops()
    fmt = "%(refname)%00%(objectname)%00%(committerdate:unix)"
    refs = []
    if "local" in where:
        refs += [("local", ln) for ln in core.gitout(c.top, "for-each-ref", "--format=" + fmt, "refs/heads").splitlines()]
    if "remote" in where and c.has_origin:
        refs += [("remote", ln) for ln in core.gitout(c.top, "for-each-ref", "--format=" + fmt,
                                                      "refs/remotes/origin").splitlines()]
    rows = []
    for kind, ln in refs:
        ref, sha, ts = (ln.split("\x00") + ["", ""])[:3]
        name = ref[len("refs/heads/"):] if kind == "local" else ref[len("refs/remotes/origin/"):]
        if not name or name == "HEAD":
            continue
        m = merge_test(c.top, name, base, ref=ref, prs=prs)
        mine = [p for p in prs or [] if p.get("headRefName") == name]
        open_pr = next((p for p in mine if str(p.get("state") or "").upper() == "OPEN"), None)
        w = wts.get(name) if kind == "local" else None
        r = {"name": name, "where": kind, "sha": sha, "state": "", "how": m["how"], "pr": m["pr"], "ahead": m["ahead"],
             "missing": m["missing"], "last_commit": int(ts) if ts.isdigit() else 0, "merge": merge_words(m, base),
             "worktree": w["path"] if w else None, "action": "keep", "why": "",
             "backup": bool((done.get(name) or {}).get("backup")),
             "pr_merged": any(str(p.get("state") or "").upper() == "MERGED" for p in mine)}
        protect = ("the default branch" if name == c.default else
                   "a permanent branch" if name in _permanent() else
                   "the current branch" if name == c.branch else
                   "checked out in the main checkout" if kind == "local" and name == in_main else
                   "embargoed" if name in (held or {}) else None)
        if protect:
            r.update(state="protected", why=f"protected: {protect}")
        elif open_pr:
            r.update(state="open_pr", why=f"open PR #{open_pr.get('number')}")
        elif not m["merged"]:
            r.update(state="not_merged", why=r["merge"] + f", last commit {core.ago(r['last_commit'])}")
        else:
            r["state"] = "merged"
            hold = None
            if held is None:
                hold = f"the embargo list cannot be read ({unreadable})"
            elif kind == "remote" and offline:
                hold = "offline: remote branches are left as they are"
            elif kind == "remote" and not github:
                hold = f"the merge requests on {c.host or 'this remote'} are not read here: an open one would lose its branch"
            elif kind == "remote" and prs is None:
                hold = "GitHub could not be read: an open pull request would lose its branch"
            elif w:
                hold = _worktree_hold(c.main, w, live)
            if not hold and kind == "local" and m["how"] == "ancestor":
                created = _created_only(c.top, name, now)
                if created:
                    hold = f"created {core.ago(created)}, no commits yet"
            if hold:
                r["why"] = f"kept: {hold} ({r['merge']})"
            else:
                r.update(action="delete", why=r["merge"])
        rows.append(r)
    order = {"delete": 0, "keep": 1}
    rows.sort(key=lambda r: (order[r["action"]], PRUNE_STATES.index(r["state"]), r["name"], r["where"]))
    return {"project": c.pid, "top": c.top, "main": c.main, "base": base, "default": c.default, "applied": False,
            "prs": prs, "branches": rows}


def _permanent() -> set:
    from . import guard
    return set(guard.PERMANENT_BRANCHES)


def _record(plan: dict, r: dict, what: str, outcome: str = "ok", error: str = "") -> None:
    from . import audit
    audit.record("branch.pruned", r["name"], f"{what}, {r['merge']}" + (f": {error}" if error else ""), outcome,
                 project=plan["project"], branch=r["name"], commit=r["sha"])


def prune(plan: dict, only=None) -> list:
    """Delete the rows of `plan` whose action is delete (and `only(row)` holds): each remote branch with a lease on
    the commit the plan saw (a branch someone pushed to since stays), then each local branch with its worktree
    (gc.remove_worktree, which checks again that it is clean and idle). Each deletion is audited (branch.pruned).
    -> the rows acted on, each with result (deleted|failed) and error."""
    from . import gc
    top = plan["top"]
    todo = [r for r in plan["branches"] if r["action"] == "delete" and (only is None or only(r))]
    for r in sorted(todo, key=lambda r: r["where"] != "remote"):
        name = r["name"]
        if r["where"] == "remote":
            rc, _, err = core.git(top, "push", "--quiet", f"--force-with-lease=refs/heads/{name}:{r['sha']}", "origin",
                                  "--delete", name, timeout=120)
            what = "remote branch deleted"
        else:
            what, rc, err = "local branch deleted", 0, ""
            if r["worktree"] and os.path.isdir(r["worktree"]):
                ok, msg = gc.remove_worktree(plan["main"], r["worktree"], name, log=False,
                                             merged={"base": plan["base"], "sha": r["sha"], "prs": plan.get("prs")})
                rc, err, what = (0, "", "local branch and its worktree deleted") if ok else (1, msg, what)
            elif r["worktree"]:
                core.git(plan["main"], "worktree", "prune", timeout=60)
            tip = _sha(top, f"refs/heads/{name}")
            if rc == 0 and tip and tip != r["sha"]:
                rc, err = 1, f"it moved to {tip[:7]} since the listing"
            elif rc == 0 and tip:
                rc, _, err = core.git(top, "branch", "-D", name, timeout=60)
        last = (err or "").strip().splitlines()[-1:] or ["failed"]
        r["result"], r["error"] = ("deleted", "") if rc == 0 else ("failed", last[0][:200])
        _record(plan, r, what if rc == 0 else what.replace("deleted", "not deleted"), "ok" if rc == 0 else "failed",
                r["error"])
        r["did"] = what
    plan["applied"] = True
    return todo


def _prune_line(r: dict, applied: bool) -> str:
    where = f"{r['where']:<6} {r['name']}"
    if applied and r.get("result") == "deleted":
        tree = f" and its worktree {core.tilde(r['worktree'])}" if r.get("did", "").startswith("local branch and") else ""
        return f"  {ui.OK} deleted {r['where']} {r['name']}{tree} ({r['why']})"
    if applied and r.get("result") == "failed":
        return f"  {ui.FAIL} not deleted {r['where']} {r['name']}: {r.get('error') or 'failed'} ({r['why']})"
    verb = "delete" if r["action"] == "delete" else "keep"
    return f"  {verb:<6} {where}  {ui.dim(r['why']) if r['action'] == 'keep' else r['why']}"


def cmd_prune(a) -> int:
    """`dev branch prune [--apply] [--remote|--local] [--offline] [--json]`."""
    top = core.gitout(os.getcwd(), "rev-parse", "--show-toplevel")
    if not top:
        print(f"{ui.WARN} not inside a git repository", file=sys.stderr)
        return 2
    where = ("remote",) if a.remote else ("local",) if a.local else ("local", "remote")
    plan = prune_plan(top, offline=bool(a.offline), where=where)
    acted = prune(plan) if a.apply else []
    if a.json:
        out = {k: plan[k] for k in ("project", "base", "default", "applied")}
        out["branches"] = plan["branches"]
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 1 if any(r.get("result") == "failed" for r in acted) else 0
    rows = plan["branches"]
    n_local, n_remote = (sum(1 for r in rows if r["where"] == w) for w in ("local", "remote"))
    print(f"{plan['project']} · against {plan['base']} · {n_local} local and {n_remote} remote branch"
          f"{'es' if n_remote != 1 else ''}")
    for r in rows:
        print(_prune_line(r, bool(a.apply)))
    drop = [r for r in rows if r["action"] == "delete"]
    trees = sum(1 for r in drop if r["worktree"])
    if not drop:
        print(f"{ui.OK} nothing to delete: no merged branch is free to go")
        return 0
    if not a.apply:
        print(f"{ui.INFO} dry run: nothing deleted · dev branch prune --apply deletes {len(drop)} branch"
              f"{'es' if len(drop) != 1 else ''}" + (f" and {trees} worktree{'s' if trees != 1 else ''}" if trees else ""))
        return 0
    failed = [r for r in acted if r.get("result") == "failed"]
    print(f"{ui.OK if not failed else ui.WARN} deleted {len(acted) - len(failed)} of {len(acted)} merged branch"
          f"{'es' if len(acted) != 1 else ''}" + (f"; {len(failed)} failed (see above)" if failed else ""))
    return 1 if failed else 0
