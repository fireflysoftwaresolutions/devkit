# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The devkit segment in the shell prompt (spec P-fxs316lh3nw R9).

Inside a devkit project the prompt shows the project's alias, the crew members working, the branch's checks and a
question that waits for the developer; outside devkit projects it shows nothing:

    ✦ devkit · 6 working · checks ✓   ! 1 question for you      (100 columns and wider)
    ✦ devkit ▸6 ✓   !1                                          (narrower)

A prompt must cost under 5 ms, so nothing here runs at prompt time. devkit's hooks and commands call refresh(),
which writes the facts of each checkout of the project to ~/.local/state/devkit/prompt/cache.zsh. The zsh function in
shell/devkit.zsh reads that file with builtins only (no Python, git or other program), keeps it in memory until the
file changes, and sets DEVKIT_PROMPT and DEVKIT_PROMPT_ASK. segment() is the same rule in Python, for tools and for
the tests, which check that both give the same text.

The clock decides at prompt time, not here: the crew part counts members whose heartbeat is newer than 15 minutes in
a run that is still working (registry.run_working, the rule `dev status` and the dashboard share); every part but the
alias hides when the cache is older than 24 hours; and the checks hide when HEAD left the branch they belong to. So
the prompt never claims work that stopped, or a result for another branch.

The variables hold plain text. Starship and oh-my-posh escape `%` for zsh themselves; the plain zsh block and
Powerlevel10k use the escaped form (zsh_escape), so a `%` in a name never becomes a prompt sequence.
"""
from __future__ import annotations

import os
import re
import tempfile
import time
from pathlib import Path

from . import core, state
from .core import STATE

CREW_FRESH_S = 15 * 60       # a member counts while its heartbeat is newer than this
STALE_S = 24 * 3600          # an older cache shows only the alias
ALIAS_MAX = 16               # longer aliases are cut, then "…"
FULL_COLS = 100              # the full form from this width up; the short form below it
MIN_INTERVAL = 5.0           # seconds between two refreshes of one project from the hooks
CHECKS = ("passed", "failed", "running")
MODES = ("starship", "zsh", "oh-my-posh", "powerlevel10k", "off")


def folder() -> Path:
    return STATE / "prompt"


def cache_json() -> Path:
    return folder() / "cache.json"


def cache_zsh() -> Path:
    return folder() / "cache.zsh"


def mode_file() -> Path:
    return folder() / "mode"


# --------------------------------------------------------------------------- the mode dev setup chose


def mode() -> str:
    """How the segment reaches the prompt on this Mac, as `dev setup` recorded it: starship, zsh (devkit's own
    block), oh-my-posh, powerlevel10k, or off. "" before the developer answered."""
    try:
        m = mode_file().read_text().strip()
    except OSError:
        return ""
    return m if m in MODES else ""


def set_mode(m: str):
    if m not in MODES:
        raise ValueError(f"unknown prompt mode {m!r}: one of {', '.join(MODES)}")
    _atomic(mode_file(), m + "\n")


# --------------------------------------------------------------------------- the facts, read without git or network


def head_of(root) -> tuple:
    """-> (the HEAD file of the checkout at root, its first line), without running git: `.git` is a folder in a
    main checkout and a `gitdir:` file in a worktree. ("", "") when root is not a git checkout."""
    g = Path(root) / ".git"
    try:
        if g.is_dir():
            h = g / "HEAD"
        elif g.is_file():
            line = g.read_text().split("\n", 1)[0].strip()
            if not line.startswith("gitdir:"):
                return "", ""
            d = Path(line[7:].strip())
            h = (d if d.is_absolute() else Path(root) / d) / "HEAD"
        else:
            return "", ""
        return os.path.realpath(h), h.read_text().split("\n", 1)[0]
    except OSError:
        return "", ""


def branch_of(ref: str) -> str:
    return ref[len("ref: refs/heads/"):] if ref.startswith("ref: refs/heads/") else ""


def _heartbeat(lane: dict) -> float:
    ts = [lane.get("last_seen") or 0, lane.get("started") or 0]
    ts += [u.get("ts") or 0 for u in lane.get("updates") or []]
    return float(max(ts))


def crew_expiries(runs: list, pid: str, now: float) -> list:
    """When each member working in the project stops counting: its heartbeat plus 15 minutes, but never later than
    its run stops counting as working (registry.run_working: a report in the last 30 minutes). The zsh function
    counts the ones still in the future, so the count drops on time even when no hook runs."""
    from . import registry
    out = []
    for run in runs:
        if run.get("project") != pid or not registry.run_working(run, now):
            continue
        run_until = registry.last_report(run) + registry.WORKING_S
        for lane in (run.get("agents") or {}).values():
            if lane.get("state") == "running":
                until = min(_heartbeat(lane) + CREW_FRESH_S, run_until)
                if until > now:
                    out.append(int(until))
    return sorted(out)


def questions(runs: list, pid: str) -> int:
    """Crew decisions that wait for the developer in the project's live runs."""
    from . import progress
    return sum(len(progress.pending_decisions(r)) for r in runs if r.get("project") == pid)


_WATCH_STATE = {"passed": "passed", "failed": "failed", "timeout": "running"}
_RUN_STATE = {"success": "passed", "failed": "failed", "running": "running", "queued": "running"}


def checks_for(remote: str, branch: str) -> str:
    """The branch's latest checks from what devkit already knows (never the network): the end of a run `dev ci
    watch` followed, or the runs `dev status` and `dev ci status` read last, whichever is newer. -> passed, failed,
    running, or "" (no run yet, or not known)."""
    from . import cistatus, ciwatch
    prov = cistatus.provider_for(remote or "")
    if not prov or not branch:
        return ""
    kind, _host, path = prov
    best, best_ts = "", 0.0
    rec = core.read_json(ciwatch.WATCH / f"{ciwatch._key(path, branch)}.json", None)
    if isinstance(rec, dict) and rec.get("branch") == branch and _WATCH_STATE.get(rec.get("state")):
        best, best_ts = _WATCH_STATE[rec["state"]], float(rec.get("ts") or 0)
    prefix = f"runs-{kind}-{path}-{branch}-".replace("/", "__").replace(":", "_")
    tail = re.compile(r"\d+-[01]\.json")
    try:
        names = [n for n in os.listdir(cistatus.CACHE) if n.startswith(prefix) and tail.fullmatch(n[len(prefix):])]
    except OSError:
        names = []
    for n in names:
        f = cistatus.CACHE / n
        doc = core.read_json(f, None)
        runs = [r for r in (doc or {}).get("runs") or [] if isinstance(r, dict) and r.get("branch") in (None, branch)]
        if not runs:
            continue
        r = max(runs, key=lambda x: x.get("started") or 0)
        try:
            ts = f.stat().st_mtime
        except OSError:
            continue
        st = _RUN_STATE.get(r.get("status"), "")
        if ts > best_ts:
            best, best_ts = st, ts
    return best


# --------------------------------------------------------------------------- refresh: what hooks and commands call


def _registry(build: bool) -> dict:
    """The projects as the last `dev sync` mapped them (a file read, no git). Before the first sync, a command
    (`build`) maps them now; a hook does not, because a hook runs no git."""
    reg = core.read_json(core.HANDOFF / "registry.json", None)
    if isinstance(reg, dict) and isinstance(reg.get("projects"), list):
        return reg
    if not build:
        return {"projects": []}
    from . import registry
    return registry.build(with_prs=False)


def _checkout_of(cwd) -> tuple:
    """-> (the checkout around cwd, its main checkout), from the `.git` entries alone: a worktree made after the
    last `dev sync` still finds its project. (None, None) outside git."""
    d = Path(core.real(cwd))
    for p in [d, *d.parents]:
        g = p / ".git"
        if g.is_dir():
            return str(p), str(p)
        if g.is_file():
            try:
                line = g.read_text().split("\n", 1)[0].strip()
                gitdir = Path(line[7:].strip()) if line.startswith("gitdir:") else None
                if gitdir is None:
                    return str(p), None
                gitdir = gitdir if gitdir.is_absolute() else p / gitdir
                common = (gitdir / "commondir").read_text().strip() if (gitdir / "commondir").exists() else ""
                common_dir = Path(core.real(gitdir / common)) if common else Path(core.real(gitdir)).parent.parent
                return str(p), str(common_dir.parent)
            except OSError:
                return str(p), None
    return None, None


def project_at(projects: list, cwd) -> tuple:
    """-> (the devkit project around cwd, the checkout root cwd is in), or (None, None) outside devkit projects."""
    from . import registry
    root, main = _checkout_of(cwd)
    p, w = registry.project_for_cwd({"projects": projects}, cwd)
    if p:
        return p, (w or {}).get("path") or root or p.get("path")
    if main:
        for q in projects:
            if q.get("path") and core.real(q["path"]) == core.real(main):
                return q, root
    return None, None


def roots_of(p: dict, extra=()) -> list:
    """Every checkout of the project on this Mac: the main one and its worktrees (managed, Claude Code, Codex)."""
    paths = [p.get("path")] + [w.get("path") for w in p.get("worktrees") or [] if not w.get("missing")] + list(extra)
    out = []
    for x in paths:
        if x and os.path.isdir(x):
            r = core.real(x)
            if r not in out:
                out.append(r)
    return out


def project_record(p: dict, alias: str, runs: list, now: float, extra=()) -> dict:
    """The prompt facts of one project, per checkout: what refresh() keeps in cache.json."""
    crew = crew_expiries(runs, p["id"], now)
    ask = questions(runs, p["id"])
    roots = {}
    for root in roots_of(p, extra):
        head, ref = head_of(root)
        roots[root] = {"head": head, "ref": ref, "checks": checks_for(p.get("remote") or "", branch_of(ref))}
    return {"alias": alias, "ts": int(now), "crew": crew, "ask": ask, "roots": roots}


def refresh(cwd=None, *, reg: dict = None, runs: list = None, now: float = None, force: bool = False) -> list:
    """Write the prompt facts. With `cwd`, the project around it, at most every MIN_INTERVAL seconds unless `force`
    (hooks, `dev status` and `dev ci watch`: a few file reads, no git, network or subprocess). Without `cwd`, every
    project on this Mac, replacing the cache (`dev setup prompt`, `dev prompt refresh`, `dev alias`). -> the project
    ids written."""
    from . import registry
    now = now or time.time()
    reg = reg if reg is not None else _registry(build=cwd is None)
    projects = [p for p in reg.get("projects") or [] if isinstance(p, dict) and p.get("id")]
    targets, extra = [], {}
    if cwd is not None:
        p, root = project_at(projects, cwd)
        if not p:
            return []
        targets, extra = [p], {p["id"]: [root] if root else []}
    else:
        targets = [p for p in projects if registry.is_local(p)]
    if runs is None:
        from . import progress
        runs = progress.active_runs()
    amap = registry.alias_map(projects)
    written = []
    with state.hold(cache_json()):
        doc = (state.load(cache_json()) if cache_json().exists() else None) or {}
        known = doc.get("projects") if isinstance(doc.get("projects"), dict) else {}
        if cwd is not None and not force:
            last = (known.get(targets[0]["id"]) or {}).get("ts") or 0
            if 0 <= now - last < MIN_INTERVAL:
                return []
        out = dict(known) if cwd is not None else {}
        for p in targets:
            keep = list(((known.get(p["id"]) or {}).get("roots") or {}).keys()) if cwd is not None else []
            rec = project_record(p, amap.get(p["id"], p["id"]), runs, now, extra.get(p["id"], []) + keep)
            if rec["roots"]:
                out[p["id"]] = rec
                written.append(p["id"])
            else:
                out.pop(p["id"], None)  # none of its checkouts is on this Mac any more
        doc = {"version": 1, "ts": int(now), "projects": out}
        state.replace(cache_json(), doc, indent=1)
        _atomic(cache_zsh(), render_zsh(doc))
    return written


def safe_refresh(cwd=None, **kw) -> list:
    """refresh() for hooks and commands, once the developer turned the helper on (`dev setup prompt`); before that,
    and after a no, it does nothing. The prompt is a convenience, so a failure here never fails the caller."""
    try:
        if mode() in ("", "off"):
            return []
        return refresh(cwd, **kw)
    except Exception:
        return []


# --------------------------------------------------------------------------- the zsh cache


def zsh_quote(s) -> str:
    return "'" + str(s).replace("'", "'\\''") + "'"


def entries(doc: dict) -> dict:
    """{checkout root: the facts the prompt reads}, flattened from cache.json."""
    out = {}
    for rec in ((doc or {}).get("projects") or {}).values():
        for root, r in (rec.get("roots") or {}).items():
            out[root] = {"alias": rec.get("alias") or "", "ts": int(rec.get("ts") or 0),
                         "crew": [int(x) for x in rec.get("crew") or []], "ask": int(rec.get("ask") or 0),
                         "checks": r.get("checks") if r.get("checks") in CHECKS else "",
                         "head": r.get("head") or "", "ref": r.get("ref") or ""}
    return out


def render_zsh(doc: dict) -> str:
    """cache.zsh: one associative array per fact, keyed by checkout root. Every value is single-quoted, so a path or
    a name can never run as code when the shell sources the file."""
    ents = entries(doc)
    rows = {"_dkp_alias": "alias", "_dkp_ts": "ts", "_dkp_crew": "crew", "_dkp_ask": "ask", "_dkp_ck": "checks",
            "_dkp_head": "head", "_dkp_ref": "ref"}
    lines = ["# devkit prompt cache: written by lib/devkit/prompt.py, read by shell/devkit.zsh. Do not edit."]
    for var, key in rows.items():
        words = []
        for root, e in sorted(ents.items()):
            v = " ".join(str(x) for x in e[key]) if key == "crew" else e[key]
            words += [zsh_quote(root), zsh_quote(v)]
        lines.append(f"{var}=( {' '.join(words)} )")
    return "\n".join(lines) + "\n"


def _atomic(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))  # 0600
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- the text (the zsh function's rule)


def zsh_escape(s: str) -> str:
    return s.replace("%", "%%")


def segment(e: dict, cols: int = 80, now: float = None, utf8: bool = True, ref: str = None) -> tuple:
    """-> (DEVKIT_PROMPT, DEVKIT_PROMPT_ASK) for one checkout's facts, "" for a variable left unset: the rule the zsh
    function in shell/devkit.zsh applies. `ref` is HEAD's first line now (read from e["head"] when None)."""
    now = time.time() if now is None else now
    star, run, ok, bad, dots, sep = ("✦", "▸", "✓", "✗", "…", "·") if utf8 else ("*", ">", "ok", "x", "...", "-")
    full = cols >= FULL_COLS
    a = e.get("alias") or ""
    if len(a) > ALIAS_MAX:
        a = a[:ALIAS_MAX] + dots
    seg, ask = f"{star} {a}", ""
    if now - (e.get("ts") or 0) < STALE_S:
        n = sum(1 for t in e.get("crew") or [] if t > now)
        if n:
            seg += f" {sep} {n} working" if full else f" {run}{n}"
        ck = e.get("checks") or ""
        if ck and e.get("head"):
            if ref is None:
                try:
                    ref = Path(e["head"]).read_text().split("\n", 1)[0]
                except OSError:
                    ref = ""
            if ref != e.get("ref"):
                ck = ""
        g = {"passed": ok, "failed": bad, "running": dots}.get(ck, "")
        if g:
            seg += f" {sep} checks {g}" if full else f" {g}"
        q = int(e.get("ask") or 0)
        if q:
            ask = f"! {q} question{'s' if q > 1 else ''} for you" if full else f"!{q}"
    return seg, ask


def show(cwd=None, cols: int = None, now: float = None) -> tuple:
    """What the prompt shows in cwd right now, from the cache (for `dev prompt` and checks)."""
    doc = core.read_json(cache_json(), {}) or {}
    ents = entries(doc)
    d = core.real(cwd or os.getcwd())
    while d:
        if d in ents:
            return segment(ents[d], cols or core_width(), now)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return "", ""


def core_width() -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return int(os.environ.get("COLUMNS") or 80)
