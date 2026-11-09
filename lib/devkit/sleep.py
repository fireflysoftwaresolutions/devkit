# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev sleep`: what a brain does at night — consolidate what matters, forget what does not.

Runs nightly as the `sleep` job (`dev jobs`), or by hand (--dry-run previews):
  lessons   usage is published for the team; strength decays unless a lesson is used; near-duplicates are merged;
            weak, retired and long-prevented lessons are archived (restorable with `dev lesson restore`); the active
            set per scope is capped; patterns seen in several projects are proposed as global lessons
  hygiene   old timing records, finished runs and session records, stale presence of machines that went quiet,
            CI caches, the hooks log and the dev rm manifest are trimmed, so local state does not grow forever
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


def run(dry_run: bool = False, quiet: bool = False) -> dict:
    now = time.time()
    lessons.publish_usage()
    acts = lessons.consolidate(dry_run=dry_run, now=now)
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
        LAST.write_text(json.dumps({"ts": int(now), "lessons": acts and [list(a) for a in acts], "hygiene": len(plan)}))
        if acts or plan:
            from . import gc
            gc._log(f"sleep · lessons: {len(acts)} change(s) · hygiene: {len(plan)} item(s)")
    if not quiet:
        print(ui.section("Sleep" + (" (dry run: nothing changes)" if dry_run else "")))
        if not acts and not plan:
            print(f"  {ui.OK} nothing to consolidate or forget")
        for kind, lid, detail in acts:
            mark = {"merge": ui.cyan("merge  "), "forget": ui.yellow("forget "), "promote": ui.green("promote")}[kind]
            print(f"  {mark} {lid}  {ui.dim(detail)}")
        by = {}
        for what, _, why in plan:
            by.setdefault(why, 0)
            by[why] += 1
        for why, n in by.items():
            print(f"  {ui.dim('tidy   ')} {n} × {why}")
        if sum(trimmed.values()):
            print(f"  {ui.dim('tidy   ')} {sum(trimmed.values())} × devkit bug records past their retention")
        if acts and not dry_run:
            print(ui.dim("\n  forgotten lessons are in lessons/archive/ — dev lesson restore <id> --why \"…\" brings one back"))
    return {"lessons": acts, "hygiene": plan, "bugs": trimmed}


def due(hours: float = 20) -> bool:
    last = (core.read_json(LAST, {}) or {}).get("ts") or 0
    return time.time() - last > hours * 3600
