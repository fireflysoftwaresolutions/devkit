# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev sleep`: what a brain does at night — consolidate what matters, forget what does not.

Runs nightly as the `sleep` job (`dev jobs`), or by hand (--dry-run previews and changes nothing):
  events    the lesson outcome events (heeded, missed) are folded into the usage counts first, so strength sees the
            latest ones; then usage is published for the team. Events older than 45 days leave the log, after a
            fold that worked (one that failed keeps them for the next night)
  check     a prevented lesson whose prevention is gone (the test, guard rule, skill or lint rule its prevention_ref
            names no longer exists in its project's checkout) is open again, with the reason in its changelog;
            projects not checked out on this Mac are not judged, and lessons with no project (global, initiatives)
            only for the guard rules they name, which exist on every machine
  lessons   no cap on how many lessons a scope keeps: a lesson leaves only by the archive policy (retired 30 days
            after retirement; prevented, idle 90 days and weak; open, idle 180 days, weak, seen once and older than
            30 days; never one younger than 30 days, seen twice or more, or kept by a merge that night) or as the
            dropped side of a merge (restorable with `dev lesson restore`). The first night of this version brings
            back, once, the lessons an older devkit archived only to fit its per-scope cap. Patterns seen in several
            projects are proposed as global lessons
  hygiene   old timing records, finished runs and session records, stale presence of machines that went quiet,
            CI caches, the hooks log, the dev rm manifest and devkit's own bug records are trimmed, so local state
            does not grow forever
The summary counts the lessons restored, merged, archived and reopened, and the events folded and pruned.
Nothing the developer wrote is deleted: lessons go to lessons/archive/ (git keeps every version), and only
devkit's own records and caches are trimmed.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import core, lessons, ui
from .core import STATE

LAST = STATE / "sleep.json"


def _old_files(folder: Path, pattern: str, days: float, now: float) -> list:
    if not folder.exists():
        return []
    return [f for f in folder.glob(pattern) if f.is_file() and now - f.stat().st_mtime > days * 86400]


def hygiene_plan(now: float = None) -> list:
    """[(what, path, reason)] of devkit's own records and caches that have done their job."""
    now = now or time.time()
    plan = []
    month_cut = time.strftime("%Y-%m", time.localtime(now - 92 * 86400))
    timing = STATE / "timing"
    for f in sorted(timing.glob("*.jsonl")) if timing.exists() else []:
        if f.stem < month_cut:
            plan.append(("timing", f, "timing records older than 3 months"))
    plan += [("pending", f, "unfinished tool timers older than a day") for f in _old_files(timing / "pending", "*.json", 1, now)]
    codex = STATE / "codex-pending"  # Codex calls waiting for their rollout record (at most ten minutes each)
    waiting = _old_files(codex, "*.json", 1, now)
    plan += [("codex-pending", f, "Codex calls left waiting for their rollout record over a day") for f in waiting]
    live = {f.stem for f in codex.glob("*.json")} - {f.stem for f in waiting} if codex.exists() else set()
    plan += [("codex-pending", f, "locks of those records older than a day")
             for f in _old_files(codex / "locks", "*.lock", 1, now) if f.stem[len("rec-"):] not in live]
    plan += [("run", f, "progress runs older than 14 days") for f in _old_files(STATE / "runs", "*.json", 14, now)]
    plan += [("session", f, "session records older than 30 days")
             for f in _old_files(STATE / "sessions", "*", 30, now)]
    plan += [("instructions", f, "instruction-load logs older than 30 days")
             for f in _old_files(STATE / "instructions", "*.jsonl", 30, now)]
    plan += [("ci-cache", f, "CI status caches older than 7 days") for f in _old_files(STATE / "cache" / "ci", "*.json", 7, now)]
    pres = core.HANDOFF / "presence"
    plan += [("presence", f, "a machine quiet for 14 days") for f in _old_files(pres, "*.json", 14, now)]
    log = STATE / "logs" / "hooks.log"
    if log.exists() and log.stat().st_size > 5_000_000:
        plan.append(("hooks-log", log, "hooks log over 5 MB (keep the newest 1 MB)"))
    trash = STATE / "trash.jsonl"
    if trash.exists():
        old = [ln for ln in trash.read_text().splitlines() if ln.strip() and
               now - (json.loads(ln).get("ts") or now) > 90 * 86400]
        if old:
            plan.append(("trash-manifest", trash, f"{len(old)} dev rm records older than 90 days"))
    return plan


def apply_hygiene(plan: list, now: float = None):
    now = now or time.time()
    for what, f, _ in plan:
        try:
            if what == "hooks-log":
                data = f.read_bytes()[-1_000_000:]
                f.write_bytes(data[data.find(b"\n") + 1:])
            elif what == "trash-manifest":
                keep = [ln for ln in f.read_text().splitlines() if ln.strip() and
                        now - (json.loads(ln).get("ts") or now) <= 90 * 86400]
                f.write_text("".join(ln + "\n" for ln in keep))
            elif what == "presence":
                core.git(core.HANDOFF, "rm", "-q", "--cached", str(f))
                f.unlink()
            else:
                f.unlink()
        except OSError:
            pass
    if any(w == "presence" for w, _, _ in plan):
        core.git(core.HANDOFF, "commit", "-q", "-m", "sleep: presence of machines quiet for 14 days removed")


EVENTS_KEEP_DAYS = 45  # lesson outcome events older than this leave the log, once folded into the usage counts


def _mark(kind: str) -> str:
    """The summary's label for a lesson action, colored when it is printed (ui reads the color switch per call)."""
    color = {"merge": ui.cyan, "forget": ui.yellow, "promote": ui.green, "reopen": ui.yellow}.get(kind, ui.dim)
    return color(f"{kind:<7}")


def checkouts() -> dict:
    """{project id: its checkout on this Mac} from the projects map (registry.json); a project with no checkout here
    is left out."""
    reg = core.read_json(core.HANDOFF / "registry.json", {}) or {}
    out = {}
    for p in (reg.get("projects") or []) if isinstance(reg, dict) else []:
        if isinstance(p, dict) and p.get("id") and p.get("path") and Path(p["path"]).is_dir():
            out[p["id"]] = Path(p["path"])
    return out


def _guard_rules_only(les: dict):
    """A global or initiative lesson as the nightly check judges it: the guard rule ids its guard prevention names
    (the parts of prevention_ref that are one word), which exist on every machine; None when it names none. Its tests,
    skills and files belong to no checkout here, so they are not judged."""
    from . import lessonlint
    m = les["meta"]
    if m.get("prevention") != "guard":
        return None
    ids = [p for p in lessonlint._parts(str(m.get("prevention_ref") or "")) if len(p.split()) == 1]
    return dict(les, meta=dict(m, prevention_ref=", ".join(ids))) if ids else None


def check_preventions(dry_run: bool = False, a=None) -> list:
    """Reopen each prevented lesson whose prevention no longer exists (lessonlint.prevention_gone), with the reason in
    its changelog. Open again, it is not judged on a later night. A project's lesson is judged in its checkout on this
    Mac, and skipped when there is none; a global or initiative lesson is judged for the guard rules it names only
    (_guard_rules_only). -> [("reopen", lesson id, reason)]; with dry_run nothing changes."""
    from . import lessonlint, work
    roots = checkouts()
    acts = []
    for les in lessons.all_lessons():
        scope = les["meta"]["scope"]
        if les["meta"].get("status") != "prevented":
            continue
        if scope == "global" or scope.startswith("initiatives/"):
            judged = _guard_rules_only(les)
            reason = lessonlint.prevention_gone(judged, None) if judged else ""
        else:
            root = roots.get(scope)
            reason = lessonlint.prevention_gone(les, root) if root is not None else ""
        if not reason:
            continue
        acts.append(("reopen", les["meta"]["id"], reason))
        if not dry_run:
            lessons.update(les, "reopened: its prevention is gone", reason, a or work.actor("devkit"), status="open")
    return acts


def run(dry_run: bool = False, quiet: bool = False, now: float = None) -> dict:
    """One night: fold the outcome events, publish usage, reopen lessons whose prevention is gone, consolidate,
    prune the event log, then the hygiene. A part that fails is reported and the others still run; a failed fold
    keeps the event log whole. -> {lessons, hygiene, bugs, counts, folded, errors}."""
    from . import lessonevents
    now = time.time() if now is None else now
    errors = []

    def step(what: str, fn, failed):
        try:
            return fn()
        except Exception as e:  # one part failing never stops the rest of the night
            errors.append(f"{what}: {e}")
            return failed

    folded = None
    if not dry_run:
        folded = step("outcome events", lambda: lessonevents.fold(now=now), None)  # before strength is judged
        step("usage", lessons.publish_usage, None)
    acts = step("prevention check", lambda: check_preventions(dry_run), [])
    acts += step("lessons", lambda: lessons.consolidate(dry_run=dry_run, now=now), [])
    pruned = 0
    if folded is not None:
        pruned = step("event log", lambda: int(lessonevents.prune(days=EVENTS_KEEP_DAYS, now=now) or 0), 0)
    kinds = [k for k, _, _ in acts]
    counts = {"restored": kinds.count("restore"), "merged": kinds.count("merge"), "archived": kinds.count("forget"),
              "reopened": kinds.count("reopen"), "pruned": pruned,
              "folded": sum(int((folded or {}).get(k) or 0) for k in ("heeded", "missed"))}
    plan = hygiene_plan(now)
    trimmed = {}
    if not dry_run:
        apply_hygiene(plan, now)
        try:  # devkit's own bug records: occurrences, session ledgers and crash files past their retention
            from . import bugs
            trimmed = bugs.trim(now)
        except Exception as e:  # never let the bug records break the night's consolidation
            if not quiet:
                print(ui.dim(f"  bug records: {e}"))
        LAST.parent.mkdir(parents=True, exist_ok=True)
        LAST.write_text(json.dumps({"ts": int(now), "lessons": acts and [list(a) for a in acts], "hygiene": len(plan),
                                    "counts": counts, "errors": errors}))
        if acts or plan or pruned or errors:
            from . import gc
            gc._log(f"sleep · lessons: {_counted(counts)} · events: {counts['folded']} folded, {counts['pruned']} "
                    f"pruned · hygiene: {len(plan)} item(s)"
                    + "".join(f" · failed: {e}" for e in errors))
    if not quiet:
        print(ui.section("Sleep" + (" (dry run: nothing changes)" if dry_run else "")))
        if not acts and not plan:
            print(f"  {ui.OK} nothing to consolidate or forget")
        if counts["restored"]:
            print(f"  {ui.green('restore')} {counts['restored']} lesson(s) the old per-scope cap archived "
                  f"{ui.dim('(back once, with the status they had; each changelog says so)')}")
        for kind, lid, detail in acts:
            if kind != "restore":
                print(f"  {_mark(kind)} {lid}  {ui.dim(detail)}")
        by = {}
        for what, _, why in plan:
            by.setdefault(why, 0)
            by[why] += 1
        for why, n in by.items():
            print(f"  {ui.dim('tidy   ')} {n} × {why}")
        if sum(trimmed.values()):
            print(f"  {ui.dim('tidy   ')} {sum(trimmed.values())} × devkit bug records past their retention")
        for e in errors:
            print(f"  {ui.yellow('failed ')} {e}")
        print(f"\n  lessons  {_counted(counts)}")
        print("  events   " + ("not folded or pruned in a dry run" if dry_run else
                               f"{counts['folded']} folded · {counts['pruned']} pruned "
                               f"{ui.dim(f'(older than {EVENTS_KEEP_DAYS} days)')}"))
        if (counts["merged"] or counts["archived"]) and not dry_run:
            print(ui.dim("\n  forgotten lessons are in lessons/archive/ — dev lesson restore <id> --why \"…\" brings one back"))
    return {"lessons": acts, "hygiene": plan, "bugs": trimmed, "counts": counts, "folded": folded or {},
            "errors": errors}


def _counted(counts: dict) -> str:
    return " · ".join(f"{counts[k]} {k}" for k in ("restored", "merged", "archived", "reopened"))


def due(hours: float = 20) -> bool:
    last = (core.read_json(LAST, {}) or {}).get("ts") or 0
    return time.time() - last > hours * 3600
