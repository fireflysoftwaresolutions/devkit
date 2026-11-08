# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The dashboard's neutral fixture for Now (spec P-fyli4qqn0y7): made-up projects, people and runs, never real data.

Three states, each the inputs of `dashnow.areas()` at a fixed clock (2026-11-06 15:15 UTC, read with TZ=UTC):
  example  a run waits on D1 (Backend engineer (Tank) asks; two crew members wait) and carries a moot D2 (its asking
           Technical writer (Ghost) ended), a run on another machine
           (bob@studio), CI failed on feat/rules-engine, a PR with changes requested, housekeeping, branches in the
           backup states (only on this Mac, embargoed, waiting, on the remote, backed up, merged), a run finished
           this afternoon, tasks done and PRs merged since yesterday
  empty    projects with Now tasks; nothing needs you and nothing runs
  welcome  no project yet
The run records go through devkit's own code (progress.brief, dashboard._runs, build_actions, collisions and
since_yesterday), so the goldens in tests/fixtures/dashboard/ change when that code does. Import it from a test
module only: the suite's hermetic home keeps the developer's own crew changes and state out of the documents.
"""
from __future__ import annotations

import calendar
import copy
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import crew, dashboard, progress  # noqa: E402

try:  # `dev test prove` runs the tests on the code before dashnow.py existed: there they must fail, not vanish
    from devkit import dashnow  # noqa: E402
except ImportError:
    dashnow = None

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "dashboard"
NOW = 1793978100                      # 2026-11-06 15:15:00 UTC
HOST, USER = "mac", "ana"
ME = f"{USER}@{HOST}"
DEV = "/Users/ana/Development"
GENERATED = NOW - 40                  # the registry's last build
PRS_AT = NOW - 600                    # the PR cache's last read


def at(hh: int, mm: int, day: int = 6) -> int:
    """An instant on November `day`, 2026, UTC."""
    return calendar.timegm((2026, 11, day, hh, mm, 0))


def iso(t: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


@contextmanager
def utc():
    """Clock times in the copy ("15:55") and since-yesterday's midnight are local time: read them in UTC."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


# --------------------------------------------------------------------------- runs

def step(name, state="pending", est=10, owner="main", after=(), started=None, finished=None, waiting=None,
         untimed=False, label=""):
    s = {"name": name, "est": est, "est_auto": False, "state": state, "owner": owner, "label": label,
         "started": started, "finished": finished, "after": list(after)}
    if waiting:
        s["waiting"] = waiting
    if untimed:
        s["untimed"] = True
    return s


def raw_run(session, goal, project, plan, *, agent="claude", state="running", started, estimate=None,
            decisions=(), agents=None, updates=(), intervals=(), finished=None, mode="guided"):
    """A progress run as devkit stores it (v2), file name <agent>-<session>."""
    est = estimate if estimate is not None else (sum(s.get("est") or 0 for s in plan) or None)
    return {"v": 2, "agent": agent, "session": session, "project": project, "goal": goal, "estimate": est,
            "estimate_cal": est, "started": started, "finished": finished, "state": state, "mode": mode,
            "plan": copy.deepcopy(list(plan)), "steps": len(plan), "step": 0,
            "agents": copy.deepcopy(agents or {}), "intervals": [list(iv) for iv in intervals],
            "updates": [dict(u) for u in updates], "history": [], "messages": [],
            "decisions": [dict(d) for d in decisions], "tracked_since": started}


def decision(did, question, options, recommend, ts, *, frm="main", from_name="Morpheus", irreversible="", target="",
             state="pending"):
    return {"id": did, "ts": ts, "question": question, "options": list(options), "recommend": recommend,
            "irreversible": irreversible, "target": target, "from": frm, "from_name": from_name, "state": state,
            "answer": "", "by": ""}


def invoice_run(moot: bool = False) -> dict:
    """Invoice export: 3 of 6 steps done, the Backend engineer's step waits on D1 after 10 minutes of work. With `moot`
    (the example state), D2 is moot too: the Technical writer asked it, then ended, and no step waits on it."""
    plan = [step("contract", "done", 15, untimed=True), step("schema", "done", 20, untimed=True),
            step("api tests", "done", 15, untimed=True),
            step("export api", "blocked", 30, owner="a-tank", started=at(14, 50), waiting="D1",
                 label="Tank: export API"),
            step("export screen", "pending", 15, owner="a-trinity", after=["export api"],
                 label="Trinity: export screen"),
            step("review", "pending", 5, after=["export screen"])]
    lanes = {"a-tank": {"label": "Tank: export API", "persona": "Tank", "type": "devkit:tank", "state": "running",
                        "waiting": "D1", "state_before_wait": "running", "started": at(14, 50), "finished": None,
                        "last_seen": at(15, 6), "updates": [], "done": 0}}
    d1 = decision("D1", "Include cancelled invoices in the export?", ["A: Leave them out", "B: Include them"], "A",
                  at(15, 6), frm="a-tank", from_name="Tank")
    decisions = [d1]
    if moot:
        lanes["a-ghost"] = {"label": "Ghost: export notes", "persona": "Ghost", "type": "devkit:ghost", "state": "done",
                            "started": at(14, 20), "finished": at(14, 40), "last_seen": at(14, 40), "updates": [], "done": 0}
        decisions.append(decision("D2", "List the CSV columns in the release notes?", ["A: List them", "B: Link the docs"],
                                  "B", at(14, 35), frm="a-ghost", from_name="Ghost"))
    return raw_run("7f3a", "Invoice export", "acme/lending-core", plan, started=at(14, 0), estimate=100,
                   decisions=decisions, agents=lanes,
                   intervals=[[at(14, 0), at(14, 50), "turn", "main"], [at(14, 50), at(15, 0), "agent", "a-tank"],
                              [at(15, 5), at(15, 6), "turn", "main"]],
                   updates=[{"ts": at(15, 6), "kind": "note", "text": "D1 asked: cancelled invoices", "next": ""}])


def docs_run() -> dict:
    """Docs refresh: finished at 13:00 after 1 h 40 of work, against an estimate of 2 h."""
    plan = [step("draft", "done", 60, started=at(11, 20), finished=at(12, 30)),
            step("review", "done", 60, started=at(12, 30), finished=at(13, 0))]
    return raw_run("2b9e", "Docs refresh", "acme/lending-core", plan, state="done", started=at(11, 20),
                   finished=at(13, 0), estimate=120, intervals=[[at(11, 20), at(13, 0), "turn", "main"]],
                   updates=[{"ts": at(13, 0), "kind": "done", "text": "docs refreshed", "next": ""}])


def remote_presence(updated: int = None) -> dict:
    """bob@studio's presence: one Codex session runs Rules engine migration (2 of 4 steps, 42-55 minutes left)."""
    updated = at(15, 13) if updated is None else updated
    brief = {"goal": "Rules engine migration", "state": "running", "step": 2, "steps": 4, "estimate": 90,
             "started": at(14, 25), "eta_min": 48, "eta_lo": 42, "eta_hi": 55, "active_min": 48.0, "wall_min": 48.0,
             "pace": 1.0,
             "plan": [{"name": "rules model", "state": "done", "est": 20, "min": 18.0, "owner": "main"},
                      {"name": "migration", "state": "done", "est": 20, "min": 22.0, "owner": "main"},
                      {"name": "rate tables", "state": "running", "est": 30, "min": 8.0, "owner": "Neo: rate tables"},
                      {"name": "parity tests", "state": "running", "est": 20, "min": 0.0, "owner": "Mouse: parity"}],
             "agents": [{"label": "Neo: rate tables", "state": "running", "min": 8.0},
                        {"label": "Mouse: parity", "state": "running", "min": 0.0}],
             "headline": "2/4 · 48m active of ~1h30m · ETA ~42m–55m · 2 agents working",
             "last": "migration done", "next": "rate tables", "updated": at(15, 13)}
    return {"user": "bob", "host": "studio", "updated": iso(updated), "locks": [],
            "sessions": [{"agent": "codex", "project": "acme/lending-rules", "branch": "feat/rules-engine",
                          "started": at(14, 25), "status": "", "last_seen": updated, "progress": brief,
                          "subagents": 2, "turn_started": at(15, 10)}]}


# --------------------------------------------------------------------------- registry

def _branch(name, state, reason="", unpushed=0, oldest=None, embargo=None, default=False, merged_into=""):
    return {"name": name, "current": default, "default": default, "sha": "0" * 40, "upstream": "", "upstream_gone": False,
            "ahead": unpushed, "behind": 0, "base_ahead": unpushed, "base_behind": 0, "where": "",
            "last_commit": oldest or at(9, 0), "unpushed": unpushed, "oldest_unpushed": oldest,
            "merged_into": merged_into, "last_backup": None, "embargo": embargo, "embargo_via": "", "worktree": None,
            "worktree_missing": False, "session": None, "pr": None, "task": None,
            "backup": {"state": state, "reason": reason}, "backup_policy": {"policy": "on"}}


def _pr(n, branch, *, checks="passing", review="", updated, author=USER, repo="acme/lending-core"):
    return {"number": n, "title": f"Change {n}", "branch": branch, "draft": False,
            "url": f"https://github.com/{repo}/pull/{n}", "updated": iso(updated), "checks": checks,
            "review": review, "author": author, "mergeable": "MERGEABLE", "size": 40}


def _project(slug, *, last_activity, prs=(), branches=None, worktrees=(), note=None, activity=None, remote=True):
    owner, name = slug.split("/")
    return {"id": slug, "owner": owner, "name": name, "path": f"{DEV}/{slug}", "local": True, "git": True,
            "remote": f"https://github.com/{slug}.git" if remote else "", "github": slug if remote else None,
            "default_branch": "main", "canonical": f"{DEV}/{slug}", "canonical_ok": True, "stack": ["python"],
            "commands": [], "main": {"branch": "main", "modified": 0, "untracked": 0, "unpushed": 0,
                                      "last_activity": last_activity, "active": False},
            "worktrees": list(worktrees), "prs_open": list(prs), "prs_known": True, "size_kb": 2048,
            "last_activity": last_activity, "note": f"projects/{slug}.md", "note_summary": note or {},
            "activity": activity or {}, "status_line": None, "plans": [], "initiatives": [],
            "branches": branches}


def _wt(branch, pr):
    return {"path": f"{DEV}/.worktrees/acme/lending-core/{branch.replace('/', '-')}", "branch": branch,
            "location": "canonical", "active": False, "locked": False, "modified": 0, "untracked": 0, "unpushed": 0,
            "pr": {"number": pr, "state": "MERGED"}, "last_activity": at(10, 0, 5), "size_kb": 3072, "missing": False}


def _note(now_tasks=(), sessions=()):
    return {"status": "active", "priority": "normal", "milestone": "", "due": "", "tags": [], "purpose": "",
            "state": "", "decisions": "", "links": "",
            "tasks": {"now": list(now_tasks), "next": [], "later": [], "done_recent": [], "open": len(now_tasks),
                      "done": 0, "overdue": []}, "sessions": list(sessions)}


def _providers(logged_in=True):
    return {"claude": {"name": "Claude Code", "installed": True, "logged_in": logged_in}}


def registry_example() -> dict:
    core_branches = [
        _branch("main", "on_remote", default=True),
        _branch("fix/rounding-error", "not_backed_up", "3 commits only on this Mac", 3, at(13, 15)),
        _branch("hotfix/auth-header", "embargoed", 'embargoed until "the advisory is published"', 2, 1793960000,
                embargo={"until": "the advisory is published"}),
        _branch("feat/rate-limits", "waiting", "commits younger than 30 min wait for the backup", 1, 1793977000),
        _branch("refactor/ledger-ports", "backed_up"),
        _branch("feat/statement-pdf", "merged", "its commits are in main", 0, merged_into="main")]
    lending_core = _project(
        "acme/lending-core", last_activity=at(15, 10),
        prs=[_pr(45, "refactor/ledger-ports", review="CHANGES_REQUESTED", updated=at(13, 40))],
        branches=core_branches, worktrees=[_wt("feat/statement-pdf", 41), _wt("fix/fee-rounding", 42)],
        note=_note(["Document the export endpoint"], ["2026-10-30 09:00 · claude · `main` · notes"]),
        activity={"2026-11-05": 4, "2026-11-06": 2})
    lending_rules = _project(
        "acme/lending-rules", last_activity=at(15, 13),
        prs=[_pr(51, "feat/rules-engine", checks="failing", updated=at(15, 2), repo="acme/lending-rules")],
        branches=[_branch("main", "on_remote", default=True), _branch("feat/rules-engine", "backed_up")],
        note=_note(["Add the rate table"], ["2026-11-06 10:00 · codex · `feat/rules-engine` · migration started"]),
        activity={"2026-11-06": 1})
    return {"version": 1, "host": HOST, "dev_root": DEV, "generated": GENERATED, "github_user": USER,
            "projects": [lending_core, lending_rules], "issues": [], "sessions": [], "providers": _providers(),
            "initiatives": [], "team": None}


def registry_empty() -> dict:
    reg = registry_example()
    for p, last in zip(reg["projects"], (at(15, 12), at(11, 0))):
        p.update(prs_open=[], worktrees=[], note_summary=_note(p["note_summary"]["tasks"]["now"]), activity={},
                 branches=[_branch("main", "on_remote", default=True), _branch("feat/x", "backed_up")],
                 last_activity=last)
    return reg


def registry_welcome() -> dict:
    return {"version": 1, "host": HOST, "dev_root": DEV, "generated": GENERATED, "github_user": USER, "projects": [],
            "issues": [], "sessions": [], "providers": _providers(), "initiatives": [], "team": None}


# --------------------------------------------------------------------------- work items, CI, jobs, PR cache

def item(iid, title, scope, *, status="todo", bucket="now", priority="normal", updated, claimed_by="",
         lock_expires=""):
    lock = ("expired" if lock_expires and lock_expires < iso(NOW) else "held") if claimed_by else ""
    return {"id": iid, "type": "task", "title": title, "scope": scope, "initiative": "", "status": status,
            "bucket": bucket, "priority": priority, "assignee": "", "claimed_by": claimed_by, "claimed_at": "",
            "lock_expires": lock_expires, "lock": lock, "due": "", "links": [], "version": 1,
            "created": iso(updated - 86400), "created_by": f"{ME} (claude:7f3a)", "updated": iso(updated),
            "updated_by": f"{ME} (claude:7f3a)", "body": "", "changelog": [], "path": f"items/{scope}/{iid}.md"}


def items_example() -> list:
    core, rules = "acme/lending-core", "acme/lending-rules"
    return [item("T-fz1doc", "Document the export endpoint", core, updated=at(9, 0)),
            item("T-fz2rate", "Add the rate table", rules, updated=at(9, 30)),
            item("T-fz3lock", "Tidy the fee rules", rules, status="doing", bucket="next", updated=at(8, 0),
                 claimed_by="bob@studio (codex:41c2)", lock_expires=iso(at(9, 0))),
            item("T-fz4a", "Export CSV header", core, status="done", updated=at(10, 5)),
            item("T-fz4b", "Fix the rounding of fees", core, status="done", updated=at(11, 30)),
            item("T-fz4c", "Rate table schema", rules, status="done", updated=at(12, 10)),
            item("T-fz4d", "Release notes for 26.11.2", core, status="done", updated=at(14, 45))]


def items_empty() -> list:
    return [i for i in items_example() if i["status"] == "todo"]


def ci_example() -> dict:
    """What dashboard.ci_watches() returns: a failure on feat/rules-engine at 15:03, an older one, a passing run."""
    def watch(repo, branch, state, ts, failures=()):
        fails = list(failures)
        first = fails[0] if fails else {}
        return {"repo": repo, "branch": branch, "sha": "1a2b3c4", "state": state, "ts": ts, "minutes": 6,
                "provider": "github", "error": None, "runs": [], "failures": fails, "class": first.get("class", ""),
                "action": first.get("action", ""), "first_error": first.get("excerpt", "")}
    fail = {"run": "CI", "number": 14, "id": 14, "url": "https://github.com/acme/lending-rules/actions/runs/14",
            "class": "test", "action": "reproduce with the test command, fix, push once",
            "jobs": [{"name": "test", "step": "Run tests"}],
            "excerpt": "AssertionError: 10.01 == 10.00\n  File \"tests/test_rates.py\", line 12"}
    old = dict(fail, number=9, id=9, url="https://github.com/acme/lending-core/actions/runs/9")
    return {"watches": [watch("acme/lending-rules", "feat/rules-engine", "failed", at(15, 3), [fail]),
                        watch("acme/lending-core", "feat/rate-limits", "passed", at(14, 30)),
                        watch("acme/lending-core", "fix/old-parser", "failed", at(9, 0, 5), [old])],
            "complete": True, "total": 3, "watching": []}


def jobs_example() -> list:
    run = {"start": at(6, 0), "end": at(6, 1), "rc": 0, "duration": 42, "summary": "all checks pass"}
    return [{"name": "doctor", "about": "checks the workspace", "schedule": "daily at 06:00", "enabled": True,
             "notify": "", "timeout_min": 10, "next": at(6, 0, 7), "installed": True, "last": run, "state": "ok",
             "runs": 1, "failed": 0}]


def pr_cache() -> dict:
    def merged(n, title, t):
        return {"number": n, "state": "MERGED", "title": title, "url": f"https://github.com/acme/lending-core/pull/{n}",
                "updatedAt": iso(t), "author": USER}
    return {"acme/lending-core": [merged(43, "Export header", at(10, 20)), merged(44, "Fee rounding", at(11, 40))]}


# --------------------------------------------------------------------------- the three states

def _inputs(reg, raws, items, ci, jobs, team, cache) -> dict:
    briefs = dashboard._runs(NOW, HOST, raw=copy.deepcopy(raws))
    colls = dashboard.collisions([], items, NOW)
    actions = dashboard.build_actions(reg, NOW, items, colls)
    since = dashboard.since_yesterday(reg, cache, NOW, items, [], ME)
    return {"reg": reg, "raw_runs": raws, "briefs": briefs, "actions": actions, "ci": ci, "jobs": jobs,
            "items": items, "since": since, "team": team, "now": NOW, "host": HOST,
            "roster": crew.load(), "prs_at": PRS_AT}


def example() -> dict:
    # this Mac's own presence carries its run too: Now shows it once, from the run file
    mine = {"user": USER, "host": HOST, "updated": iso(at(15, 14)), "locks": [],
            "sessions": [{"agent": "claude", "project": "acme/lending-core", "branch": "feat/invoice-export",
                          "started": at(14, 0), "status": "", "last_seen": at(15, 14),
                          "progress": progress.brief(invoice_run(moot=True), NOW)}]}
    return _inputs(registry_example(), [invoice_run(moot=True), docs_run()], items_example(), ci_example(),
                   jobs_example(), [mine, remote_presence()], pr_cache())


def empty() -> dict:
    return _inputs(registry_empty(), [], items_empty(), {"watches": [], "complete": True, "total": 0, "watching": []},
                   jobs_example(), [], {})


def welcome() -> dict:
    return _inputs(registry_welcome(), [], [], {"watches": [], "complete": True, "total": 0, "watching": []}, [], [],
                   {})


STATES = {"example": example, "empty": empty, "welcome": welcome}


@contextmanager
def live_sessions(*sessions):
    """A record for each (agent, session) of this Mac's runs, as a live session keeps one: progress.lane_states reads
    a run whose session has no record as one whose session ended, and its members as stale. No pid: its process is
    unknown, so it counts as live. The records sit in a folder of their own, removed after."""
    import tempfile
    from unittest import mock
    with tempfile.TemporaryDirectory() as d:
        for agent, sid in sessions:
            (Path(d) / f"{agent}-{sid}.json").write_text(json.dumps({"agent": agent, "session": sid}))
        with mock.patch.object(progress, "SESSIONS", Path(d)):
            yield


def document(state: str, memo=None) -> dict:
    """The five areas for one state, at the fixed clock, in UTC, with a memo of its own; this Mac's runs belong to
    live sessions."""
    with utc(), live_sessions(("claude", "7f3a")):
        kw = STATES[state]()
        return dashnow.areas(**kw, memo=memo or dashnow.LandingMemo(clock=lambda: NOW))


def golden_path(state: str) -> Path:
    return GOLDEN / f"now-{state}.json"


def dumps(doc: dict) -> str:
    return json.dumps(doc, indent=1, ensure_ascii=False, sort_keys=True) + "\n"


def write_goldens():
    """Regenerate the goldens inside the hermetic suite (NOW_GOLDENS=write python3 -m unittest tests.test_dashnow);
    the Software architect approves the diff."""
    GOLDEN.mkdir(parents=True, exist_ok=True)
    for state in STATES:
        golden_path(state).write_text(dumps(document(state)))
