# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Agent providers (Claude Code, Codex): installation, sign-in, plan, quota — and live self-tests.

`status()` is cheap enough for `dev doctor`, the dashboard and agents (`dev agents --json`).
`selftest(agent)` runs one short real session in a scratch repository and verifies devkit's
integration from evidence devkit itself recorded (hook files, guard log, canary log), not
from what the model says about itself.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from . import core
from .core import STATE, sh

SELFTEST_PROMPT = (
    "This is an automated devkit self-test in a scratch repository. Do exactly these steps, then reply briefly:\n"
    "1. Run exactly: git checkout -b {agent}/devkit-selftest   (report whether it was allowed)\n"
    "2. List the names of ALL the skills you can use whose names start with 'devkit' (every one, comma-separated).\n"
    "3. Say whether you received any workspace context when the session started.")



def _appended(path: Path, offset: int) -> str:
    """The text written to `path` after it was `offset` bytes long. The offset is a byte count (stat), so the slice is
    taken on bytes: slicing the decoded text would skip new lines whenever earlier lines hold multi-byte characters."""
    try:
        return path.read_bytes()[offset:].decode("utf-8", "replace")
    except OSError:
        return ""

def _claude_status() -> dict:
    out = {"installed": core.have("claude"), "name": "Claude Code"}
    if not out["installed"]:
        return out
    out["version"] = sh(["claude", "--version"], timeout=20)[1].split(" ")[0]
    rc, txt, _ = sh(["claude", "auth", "status"], timeout=30)
    try:
        a = json.loads(txt)
    except ValueError:
        a = {}
    out.update(logged_in=bool(a.get("loggedIn")), auth_method=a.get("authMethod"), plan=a.get("subscriptionType"),
               api_provider=a.get("apiProvider"), login_expires=a.get("loginExpiresAt"), account=a.get("email"),
               quota=claude_quota())
    out["desktop"] = Path("/Applications/Claude.app").exists()
    return out


def claude_quota(now: float = None) -> dict:
    """Claude's usage windows as recorded by devkit's status line (Claude Code reports them nowhere else)."""
    import time as _t
    now = now or _t.time()
    rec = core.read_json(STATE / "quota" / "claude.json", {}) or {}
    win = {k: rec.get(k) or {} for k in ("seven_day", "five_hour")}
    live = {k: w for k, w in win.items() if w.get("used_percentage") is not None and (w.get("resets_at") or 0) > now}
    if not live:
        note = ("No usage reading yet: devkit records it from Claude Code's status line, so it appears after the "
                "next Claude Code session in a terminal." if not rec else
                f"The last reading ({core.ago(rec.get('ts'))}) has expired; it refreshes in the next Claude Code session.")
        return {"available": None, "note": note}
    main = live.get("seven_day") or live.get("five_hour")
    worst = max(w["used_percentage"] for w in live.values())
    full = next((k for k, w in live.items() if w["used_percentage"] >= 100), None)
    return {"available": worst < 100, "used_percent": main["used_percentage"],
            "window_minutes": 10080 if "seven_day" in live else 300,
            "resets_at": (live[full] if full else main).get("resets_at"),
            "secondary_used_percent": (live.get("five_hour") or {}).get("used_percentage") if "seven_day" in live else None,
            "secondary_resets_at": (live.get("five_hour") or {}).get("resets_at") if "seven_day" in live else None,
            "reached": {"seven_day": "weekly", "five_hour": "5-hour"}.get(full), "as_of": rec.get("ts"),
            "source": "status line"}


def _codex_status() -> dict:
    out = {"installed": core.have("codex"), "name": "Codex"}
    if not out["installed"]:
        return out
    out["version"] = sh(["codex", "--version"], timeout=20)[1].replace("codex-cli ", "")
    out["desktop"] = Path("/Applications/ChatGPT.app").exists() or Path("/Applications/Codex.app").exists()
    from .codexrpc import CodexError, CodexRPC
    try:
        with CodexRPC(timeout=30) as rpc:
            acc = rpc.call("account/read", {}).get("account") or {}
            out.update(logged_in=bool(acc), auth_method=acc.get("type"), plan=acc.get("planType"),
                       account=acc.get("email"))
            rl = rpc.call("account/rateLimits/read", {})
            prim = ((rl.get("rateLimits") or {}).get("primary") or {})
            sec = ((rl.get("rateLimits") or {}).get("secondary") or {})
            credits = (rl.get("rateLimits") or {}).get("credits") or {}
            out["quota"] = {
                "available": bool(rl.get("ordinaryUsageAllowed")),
                "used_percent": prim.get("usedPercent"), "window_minutes": prim.get("windowDurationMins"),
                "resets_at": prim.get("resetsAt"),
                "secondary_used_percent": sec.get("usedPercent"), "secondary_resets_at": sec.get("resetsAt"),
                "credits": credits.get("balance"), "reached": (rl.get("rateLimits") or {}).get("rateLimitReachedType")}
    except CodexError as e:
        out.update(logged_in=False, error=str(e))
    return out


def status(include_account=True) -> dict:
    res = {"claude": _claude_status(), "codex": _codex_status(), "checked": int(time.time())}
    if not include_account:
        for v in res.values():
            if isinstance(v, dict):
                v.pop("account", None)
    try:
        f = STATE / "providers.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({k: ({kk: vv for kk, vv in v.items() if kk != "account"} if isinstance(v, dict) else v)
                                 for k, v in res.items()}, indent=1))
    except OSError:
        pass
    return res


def cached(max_age=3600) -> dict:
    f = STATE / "providers.json"
    try:
        if f.exists() and time.time() - f.stat().st_mtime < max_age:
            return json.loads(f.read_text())
    except (OSError, ValueError):
        pass
    return status(include_account=False)


# --------------------------------------------------------------------------- self-test

def _scratch(agent: str) -> Path:
    d = core.TMP_ROOT / f"selftest-{agent}"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    sh(["git", "init", "-q", "-b", "main", str(d)])
    (d / "README.md").write_text("devkit self-test scratch repository\n")
    sh(["git", "-C", str(d), "add", "-A"])
    sh(["git", "-C", str(d), "commit", "-q", "-m", "chore: self-test fixture"])
    return d


def _run_claude(cwd: Path, model: str):
    rc, out, err = sh(["claude", "-p", SELFTEST_PROMPT.format(agent="claude"), "--model", model,
                       "--output-format", "json", "--max-turns", "12"], cwd=str(cwd), timeout=600)
    try:
        d = json.loads(out)
    except ValueError:
        return None, (out + err)[-500:], rc
    return d.get("session_id"), d.get("result") or "", rc


def _run_codex(cwd: Path, model: str = None):
    args = ["codex", "exec", "--skip-git-repo-check", SELFTEST_PROMPT.format(agent="codex")]
    if model:
        args[2:2] = ["-m", model]
    rc, out, err = sh(args, cwd=str(cwd), timeout=600)
    m = re.search(r"session id:\s*([0-9a-f-]{36})", err + out)
    return (m.group(1) if m else None), (out or err)[-1500:], rc


def selftest(agent: str, model: str = None) -> dict:
    """Run one real session and check the evidence. -> {agent, ok, checks: [(name, ok, detail)], reply}"""
    st = _claude_status() if agent == "claude" else _codex_status()
    checks = [("installed", st.get("installed"), st.get("version") or "not installed"),
              ("signed in", st.get("logged_in"), f"{st.get('auth_method') or '-'} · plan {st.get('plan') or '-'}")]
    q = st.get("quota") or {}
    if q.get("available") is False:
        reset = core.fmt_ts(q.get("resets_at")) if q.get("resets_at") else "?"
        checks.append(("quota", False, f"usage limit reached ({q.get('used_percent')}% used) until {reset}"))
        return {"agent": agent, "ok": False, "checks": checks, "reply": "", "skipped": "quota"}
    if not (st.get("installed") and st.get("logged_in")):
        return {"agent": agent, "ok": False, "checks": checks, "reply": ""}
    scratch = _scratch(agent)
    log = STATE / "logs" / "hooks.log"
    log_before = log.stat().st_size if log.exists() else 0
    canary_f = STATE / "canary.jsonl"
    canary_before = canary_f.stat().st_size if canary_f.exists() else 0
    started = time.time()
    if agent == "claude":
        sid, reply, rc = _run_claude(scratch, model or "claude-sonnet-5-5")
    else:
        sid, reply, rc = _run_codex(scratch, model)
    dur = time.time() - started
    limited = bool(re.search(r"usage limit|rate limit|quota", reply or "", re.I)) and not sid
    checks.append(("session ran", bool(sid) and not limited,
                   f"session {sid[:8] if sid else '-'} · {dur:.0f}s" + (" · rate limited" if limited else "")))
    sess = STATE / "sessions" / f"{agent}-{sid}.json" if sid else None
    checks.append(("SessionStart hook (context)", bool(sess and sess.exists()), "session recorded by devkit"
                   if sess and sess.exists() else "no session record"))
    new_log = _appended(log, log_before)
    blocked = f"{agent}/devkit-selftest" in new_log and f"guard {agent} deny" in new_log
    checks.append(("PreToolUse guard", blocked, "tool-prefixed branch blocked" if blocked else "no guard decision logged"))
    new_canary = _appended(canary_f, canary_before)
    recs = [json.loads(ln) for ln in new_canary.splitlines() if ln.strip()]
    mine = [r for r in recs if sid and r.get("session") == sid[:12]]
    checks.append(("Stop hook + instructions (canary)", bool(mine) and all(r["ok"] for r in mine),
                   (mine[-1].get("status") or mine[-1].get("excerpt") or "")[:80] if mine else "no canary record"))
    if mine and all(r["ok"] for r in mine):
        fmt = [r.get("format") or [] for r in mine if "format_ok" in r]
        checks.append(("status block format", bool(fmt) and not fmt[-1],
                       "header + Done / Check / Next" if fmt and not fmt[-1] else "; ".join((fmt or [["not scored"]])[-1])))
    if agent == "claude" and sid:
        inst = STATE / "instructions" / f"claude-{sid}.jsonl"
        loaded = inst.exists() and str(core.CLAUDE_HOME / "CLAUDE.md") in inst.read_text()
        checks.append(("global instructions loaded", loaded, "~/.claude/CLAUDE.md (InstructionsLoaded)"))
    want = sorted(d.name for d in (core.DEVKIT_HOME / "plugin" / "skills").iterdir() if (d / "SKILL.md").exists())
    sees = [n for n in want if re.search(rf"devkit[:\-]?\s*{re.escape(n)}|\b{re.escape(n)}\b", reply or "", re.I)]
    checks.append(("devkit plugin skills visible", len(sees) >= max(3, len(want) - 2),
                   f"{len(sees)}/{len(want)}: " + (", ".join(sees) or "none mentioned")))
    shutil.rmtree(scratch, ignore_errors=True)
    for p in (STATE / "sessions").glob(f"{agent}-{sid}*") if sid else []:
        p.unlink()
    ok = all(c[1] for c in checks)
    res = {"agent": agent, "ok": ok, "checks": checks, "reply": (reply or "")[:1200], "ts": int(time.time())}
    (STATE / f"selftest-{agent}.json").write_text(json.dumps(res, indent=1))
    return res
