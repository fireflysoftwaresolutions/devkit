# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""Scheduled jobs: devkit's background work as named launchd jobs you can see, run, change and audit.

Each job is a `dev …` command on a schedule (every N minutes, daily at HH:MM, or weekly), installed as
~/Library/LaunchAgents/com.devkit.job.<name>.plist. launchd starts `dev jobs exec <name>`, which:
  - runs the command with a timeout, its output appended to ~/.local/state/devkit/logs/jobs/<name>.log;
  - writes the run to the journal (~/.local/state/devkit/jobs/journal.jsonl: start, end, exit code, duration, the
    last line of output) and to the audit trail;
  - notifies the developer when a job fails (or always, for jobs with notify: always).
Schedules and switches live in the devkit config (`jobs` key), so `dev jobs set|enable|disable` survive reinstalls.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
import sys
import time
from pathlib import Path

from . import audit, core, ui
from .core import CFG, DEVKIT_HOME, HOME, STATE

AGENTS_DIR = HOME / "Library" / "LaunchAgents"
LABEL = "com.devkit.job.{}"
LEGACY = "com.devkit.sync"
JOURNAL = STATE / "jobs" / "journal.jsonl"
LOGS = STATE / "logs" / "jobs"
WEEKDAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}

# name -> defaults. `every` (minutes), `at` ("HH:MM", daily) or `weekly` ("mon 09:00"); `timeout` in minutes.
DEFAULT_JOBS = {
    "sync": {"about": "Projects map, PRs, sizes, presence, locks, safe cleanup; commits and pushes the handoff repo",
             "cmd": ["sync", "--scheduled", "--quiet"], "every": 60, "timeout": 20, "run_at_load": True},
    "sleep": {"about": "Lessons: decay, merge, forget, cap; trims devkit's old records, caches and logs",
              "cmd": ["sleep", "--quiet"], "at": "03:30", "timeout": 15},
    "gc": {"about": "Reclaims disk safely: merged worktrees, idle build output, stale temp, Docker build cache",
           "cmd": ["gc", "--apply", "--yes", "--auto-only"], "at": "04:00", "timeout": 30},
    "repair": {"about": "Restores devkit-managed agent settings and Codex hook trust if anything drifted",
               "cmd": ["config", "repair", "--quiet"], "every": 360, "timeout": 5},
    "audit": {"about": "Publishes this machine's audit trail to the handoff repo and prunes local copies over 2 years",
              "cmd": ["audit", "--publish", "--quiet"], "at": "05:00", "timeout": 5},
    "doctor": {"about": "Weekly health check of the whole setup; notifies when a check fails",
               "cmd": ["doctor"], "weekly": "mon 09:00", "timeout": 10},
    "lessons": {"about": "Weekly lesson review: recurring lessons to automate, stale ones to retire",
                "cmd": ["lesson", "review"], "weekly": "fri 16:00", "timeout": 5, "notify": "always"},
}


def jobs() -> dict:
    """Effective jobs: defaults merged with the developer's config (`jobs.<name>` overrides)."""
    over = CFG.get("jobs") or {}
    out = {}
    for name, d in DEFAULT_JOBS.items():
        j = {"enabled": True, "notify": "failure", **d, **(over.get(name) or {})}
        if (over.get(name) or {}).get("every") is not None:
            j.pop("at", None), j.pop("weekly", None)
        if (over.get(name) or {}).get("at"):
            j.pop("every", None), j.pop("weekly", None)
        if (over.get(name) or {}).get("weekly"):
            j.pop("every", None), j.pop("at", None)
        out[name] = j
    for name, j in over.items():  # custom jobs added by the developer
        if name not in out and isinstance(j, dict) and j.get("cmd"):
            out[name] = {"enabled": True, "notify": "failure", "timeout": 30, "about": "custom job", **j}
    if not (CFG.get("gc") or {}).get("auto", True) and "enabled" not in ((CFG.get("jobs") or {}).get("gc") or {}):
        out["gc"]["enabled"] = False  # gc.auto off (wizard: "report only") keeps the gc job off
    return out


def schedule_text(j: dict) -> str:
    if j.get("every"):
        m = int(j["every"])
        return f"every {m // 60} h" if m % 60 == 0 and m >= 60 else f"every {m} min"
    if j.get("at"):
        return f"daily at {j['at']}"
    if j.get("weekly"):
        return f"weekly, {j['weekly']}"
    return "manual"


def plist_for(name: str, j: dict) -> dict:
    py = "/opt/homebrew/bin/python3" if Path("/opt/homebrew/bin/python3").exists() else sys.executable
    d = {"Label": LABEL.format(name),
         "ProgramArguments": [py, str(DEVKIT_HOME / "bin" / "dev"), "jobs", "exec", name],
         "ProcessType": "Background", "LowPriorityIO": True, "Nice": 10,
         "StandardOutPath": str(LOGS / f"{name}.launchd.log"), "StandardErrorPath": str(LOGS / f"{name}.launchd.log"),
         "EnvironmentVariables": {"PATH": f"/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:"
                                          f"/usr/sbin:/sbin:{HOME}/.local/bin", "HOME": str(HOME),
                                  "DEVKIT_AGENT": "job"}}
    if j.get("every"):
        d["StartInterval"] = int(j["every"]) * 60
    elif j.get("at"):
        hh, mm = (int(x) for x in j["at"].split(":"))
        d["StartCalendarInterval"] = {"Hour": hh, "Minute": mm}
    elif j.get("weekly"):
        day, hm = j["weekly"].split()
        hh, mm = (int(x) for x in hm.split(":"))
        d["StartCalendarInterval"] = {"Weekday": WEEKDAYS[day[:3].lower()], "Hour": hh, "Minute": mm}
    if j.get("run_at_load"):
        d["RunAtLoad"] = True
    return d


def _target() -> str:
    return f"gui/{os.getuid()}"


def loaded(name: str) -> bool:
    return core.sh(["launchctl", "print", f"{_target()}/{LABEL.format(name)}"], timeout=10)[0] == 0


def install(quiet: bool = False) -> list:
    """Write and (re)load the plists of enabled jobs, unload disabled ones, retire the legacy sync agent."""
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    out = []
    legacy = AGENTS_DIR / f"{LEGACY}.plist"
    if legacy.exists():
        core.sh(["launchctl", "bootout", f"{_target()}/{LEGACY}"], timeout=10)
        legacy.unlink()
        out.append(("legacy", LEGACY, "replaced by com.devkit.job.sync"))
    for name, j in jobs().items():
        path = AGENTS_DIR / f"{LABEL.format(name)}.plist"
        if not j.get("enabled"):
            if path.exists() or loaded(name):
                core.sh(["launchctl", "bootout", f"{_target()}/{LABEL.format(name)}"], timeout=10)
                if path.exists():
                    path.unlink()
                out.append(("disabled", name, ""))
            continue
        data = plistlib.dumps(plist_for(name, j))
        changed = not path.exists() or path.read_bytes() != data
        if changed:
            path.write_bytes(data)
        if changed or not loaded(name):
            core.sh(["launchctl", "bootout", f"{_target()}/{LABEL.format(name)}"], timeout=10)
            rc, _, err = core.sh(["launchctl", "bootstrap", _target(), str(path)], timeout=15)
            out.append(("loaded" if rc == 0 else "failed", name, err[:120] if rc else schedule_text(j)))
        else:
            out.append(("ok", name, schedule_text(j)))
    audit.record("jobs.install", ",".join(n for _, n, _ in out), "; ".join(f"{s}:{n}" for s, n, _ in out))
    return out


# --------------------------------------------------------------------------- running and the journal

def journal(days: float = 30, name: str = None) -> list:
    if not JOURNAL.exists():
        return []
    since = time.time() - days * 86400
    out = []
    for ln in JOURNAL.read_text().splitlines():
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get("start", 0) >= since and (not name or r.get("job") == name):
            out.append(r)
    return out


def last_run(name: str):
    runs = journal(400, name)
    return runs[-1] if runs else None


def execute(name: str) -> int:
    """Run one job now: timeout, log, journal, audit, notification. Used by launchd and `dev jobs run`."""
    js = jobs()
    if name not in js:
        ui.err(f"no job “{name}” — dev jobs ls")
        return 2
    j = js[name]
    LOGS.mkdir(parents=True, exist_ok=True)
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    lock = core.Lock(f"job-{name}", wait=0)
    with lock as ok:
        if not ok:
            _journal({"job": name, "start": time.time(), "end": time.time(), "rc": None, "duration": 0,
                      "summary": "skipped: the previous run is still going"})
            return 0
        start = time.time()
        log = LOGS / f"{name}.log"
        with log.open("a") as fh:
            fh.write(f"\n=== {core.fmt_ts(start)} · dev {' '.join(j['cmd'])}\n")
            fh.flush()
            try:
                p = subprocess.run([sys.executable, str(DEVKIT_HOME / "bin" / "dev"), *j["cmd"]], stdout=fh,
                                   stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=int(j["timeout"]) * 60,
                                   env={**os.environ, "DEVKIT_JOB": name, "DEVKIT_NO_NOTIFY": "1", "NO_COLOR": "1"})
                rc = p.returncode
            except subprocess.TimeoutExpired:
                rc = 124
                fh.write(f"\n!!! timed out after {j['timeout']} min\n")
        end = time.time()
        tail = _last_lines(log, 3)
        rec = {"job": name, "start": round(start, 1), "end": round(end, 1), "rc": rc, "duration": round(end - start, 1),
               "summary": tail[-1][:200] if tail else ""}
        _journal(rec)
        audit.record(f"job.{name}", " ".join(j["cmd"]), rec["summary"], "ok" if rc == 0 else f"failed ({rc})",
                     duration=rec["duration"])
        if (rc != 0 and j.get("notify") in ("failure", "always")) or j.get("notify") == "always":
            from . import progress
            progress.notify(f"job {name} " + ("finished" if rc == 0 else f"failed ({rc})"),
                            (rec["summary"] or schedule_text(j))[:160])
        return rc


def _journal(rec: dict):
    with JOURNAL.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def _last_lines(path: Path, n: int) -> list:
    try:
        data = path.read_bytes()[-20000:].decode("utf-8", "ignore")
    except OSError:
        return []
    lines = [re.sub(r"\x1b\[[0-9;]*m", "", ln).strip() for ln in data.splitlines()]
    return [ln for ln in lines if ln and not ln.startswith("===")][-n:]


def set_schedule(name: str, every: str = None, at: str = None, weekly: str = None, enabled: bool = None,
                 why: str = "") -> dict:
    """Change a job in the devkit config (~/.config/devkit/config.json) and reinstall it."""
    if name not in jobs():
        raise ValueError(f"no job “{name}”")
    conf = core.read_json(core.CONFIG_FILE, {}) or {}
    j = conf.setdefault("jobs", {}).setdefault(name, {})
    if every:
        m = re.fullmatch(r"(\d+)\s*(m|min|h)?", every.strip())
        if not m:
            raise ValueError("--every takes minutes or hours: 30m, 2h")
        j.update({"every": int(m.group(1)) * (60 if m.group(2) == "h" else 1)})
        j.pop("at", None), j.pop("weekly", None)
    if at:
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", at):
            raise ValueError("--at takes HH:MM (24 h)")
        j.update({"at": at, "every": None})
        j.pop("weekly", None)
    if weekly:
        m = re.fullmatch(r"(sun|mon|tue|wed|thu|fri|sat)\w*\s+([01]?\d|2[0-3]):([0-5]\d)", weekly.strip().lower())
        if not m:
            raise ValueError('--weekly takes "mon 09:00"')
        j.update({"weekly": f"{m.group(1)} {m.group(2)}:{m.group(3)}", "every": None})
        j.pop("at", None)
    if enabled is not None:
        j["enabled"] = enabled
    j = {k: v for k, v in j.items() if v is not None}
    conf["jobs"][name] = j
    core.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    core.CONFIG_FILE.write_text(json.dumps(conf, indent=2, ensure_ascii=False) + "\n")
    CFG.setdefault("jobs", {})[name] = j
    audit.record("jobs.set", name, json.dumps(j), why=why)
    install(quiet=True)
    return jobs()[name]


def next_run(j: dict, now: float = None):
    """Approximate next start (epoch) from the schedule and the last run."""
    now = now or time.time()
    if not j.get("enabled"):
        return None
    lt = time.localtime(now)
    if j.get("every"):
        return now + int(j["every"]) * 60  # launchd keeps its own interval clock; this is an upper bound
    if j.get("at"):
        hh, mm = (int(x) for x in j["at"].split(":"))
        t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
        return t if t > now else t + 86400
    if j.get("weekly"):
        day, hm = j["weekly"].split()
        hh, mm = (int(x) for x in hm.split(":"))
        target = WEEKDAYS[day[:3]]
        today = (lt.tm_wday + 1) % 7  # time.struct_time: Monday = 0; launchd: Sunday = 0
        t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1)) + ((target - today) % 7) * 86400
        return t if t > now else t + 7 * 86400
    return None
