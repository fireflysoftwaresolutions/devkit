# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""The audit trail: one record for everything devkit changes or decides.

Recorded: every `dev` command that changes something (tasks, plans, lessons, policies, files through dev rm/mv/cp,
git and GitHub actions, installs, config repairs and locks, jobs, gc, sleep), every guard refusal, blocked settings
change and missed status block, every write to the handoff repository, and every scheduled job run.

Where: ~/.local/state/devkit/audit/YYYY-MM.jsonl on this machine (append-only), published by the sync job to the
handoff repository as audit/<user>@<host>/YYYY-MM.jsonl so the team has one history. Secrets are redacted before a
record is written. Query with `dev audit`.
"""
from __future__ import annotations

import json
import os
import re
import socket
import time
from pathlib import Path

from . import core
from .core import STATE

AUDIT = STATE / "audit"
SECRET = re.compile(r"(?i)(gh[pousr]_[0-9a-z]{20,}|github_pat_[0-9a-z_]{20,}|xox[abpr]-[0-9a-z-]{10,}|"
                    r"sk-[0-9a-z_-]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
                    r"(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*\S+)")

# dev commands that only read; everything else is audited
READ_ONLY = {"help", "ls", "go", "status", "open", "overview", "standup", "dashboard", "team", "report", "time",
             "files", "find", "grep", "diff", "canary", "du", "doctor", "version", "agents", "calver", "audit",
             "__complete", "hook", "guard", "plugin", "docs", "diagram", "ci"}
READ_SUB = {("task", "ls"), ("task", "show"), ("task", "history"), ("task", None), ("plan", "ls"), ("plan", None),
            ("lesson", "ls"), ("lesson", "show"), ("lesson", "scan"), ("lesson", "review"), ("lesson", None),
            ("progress", None), ("progress", "time"), ("delivery", None), ("initiative", "ls"), ("initiative", "show"),
            ("wt", "ls"), ("config", "diff"), ("config", "status"), ("config", "show"), ("config", "validate"),
            ("jobs", "ls"), ("jobs", None), ("jobs", "show"), ("jobs", "logs"), ("jobs", "journal"), ("pr", "ls"),
            ("pr", "view"), ("pr", "checks"), ("pr", "diff"), ("run", "ls"), ("run", "view"), ("branch", "ls"),
            ("issue", "ls"), ("issue", "view"), ("sec", "scan")}


def redact(text: str) -> str:
    return SECRET.sub(lambda m: m.group(0)[:4] + "…[redacted]", text or "")


def actor() -> dict:
    sess = os.environ.get("DEVKIT_SESSION", "")
    return {"user": core.CFG.get("github_user") or os.environ.get("USER", "?"),
            "host": socket.gethostname().split(".")[0].lower(),
            "agent": os.environ.get("DEVKIT_AGENT") or ("claude" if os.environ.get("CLAUDECODE") else "human"),
            "session": sess.split(":", 1)[-1][:12] if sess else ""}


def record(action: str, target: str = "", detail: str = "", outcome: str = "ok", **extra):
    """Append one audit record. Never raises."""
    try:
        a = actor()
        rec = {"ts": round(time.time(), 3), "user": a["user"], "host": a["host"], "agent": a["agent"],
               "session": a["session"], "action": action, "target": redact(str(target))[:300],
               "detail": redact(str(detail))[:600], "outcome": outcome}
        rec.update({k: (redact(v)[:300] if isinstance(v, str) else v) for k, v in extra.items()})
        AUDIT.mkdir(parents=True, exist_ok=True)
        with (AUDIT / time.strftime("%Y-%m.jsonl")).open("a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def audited(argv: list) -> bool:
    """Should this dev invocation be recorded?"""
    if not argv:
        return False
    cmd = argv[0]
    sub = next((x for x in argv[1:] if not x.startswith("-")), None)
    if cmd in READ_ONLY:
        return False
    if cmd == "sleep" and "--dry-run" in argv:
        return False
    if cmd == "gc" and "--apply" not in argv:
        return False
    if (cmd, sub) in READ_SUB or (cmd, None) in READ_SUB and sub is None:
        return False
    if any(x in ("--dry-run", "-n") for x in argv) and cmd in ("rm", "mv", "cp", "release", "push", "pr"):
        return False
    return True


def records(days: float = 7, action: str = None, actor_: str = None, team: bool = False) -> list:
    since = time.time() - days * 86400
    files = []
    if team and (core.HANDOFF / "audit").exists():
        files = sorted((core.HANDOFF / "audit").rglob("*.jsonl"))
    files += sorted(AUDIT.glob("*.jsonl")) if AUDIT.exists() else []
    out, seen = [], set()
    for f in files:
        try:
            lines = f.read_text().splitlines()
        except OSError:
            continue
        for ln in lines:
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            key = (r.get("ts"), r.get("host"), r.get("action"), r.get("target"))
            if key in seen or r.get("ts", 0) < since:
                continue
            seen.add(key)
            if action and not str(r.get("action", "")).startswith(action):
                continue
            if actor_ and actor_ not in f"{r.get('user')}@{r.get('host')} {r.get('agent')}":
                continue
            out.append(r)
    return sorted(out, key=lambda r: r.get("ts", 0))


def publish() -> list:
    """Copy this machine's audit files into the handoff repository (audit/<user>@<host>/). -> files changed."""
    if not (core.CFG.get("audit") or {}).get("publish", True) or not AUDIT.exists():
        return []
    a = actor()
    dest = core.HANDOFF / "audit" / f"{a['user']}@{a['host']}"
    changed = []
    for f in sorted(AUDIT.glob("*.jsonl"))[-3:]:  # this month and the two before
        target = dest / f.name
        data = f.read_text()
        if not target.exists() or target.read_text() != data:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(data)
            changed.append(target)
    return changed


def prune(months: int = 24):
    """Keep two years of local audit (the handoff copy keeps the history in git)."""
    if not AUDIT.exists():
        return
    cut = time.strftime("%Y-%m", time.localtime(time.time() - months * 31 * 86400))
    for f in AUDIT.glob("*.jsonl"):
        if f.stem < cut:
            try:
                f.unlink()
            except OSError:
                pass
