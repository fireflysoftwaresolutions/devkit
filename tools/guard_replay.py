#!/usr/bin/env python3
# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Replay the guard refusals of this Mac's Claude Code transcripts against the guard of this checkout.

Usage: python3 -I tools/guard_replay.py [--since YYYY-MM-DD] [--root ~/.claude/projects] [--samples 3] [--json]

It finds every tool call the PreToolUse hook refused since the date (main sessions and subagents), judges the same
call again with this checkout's guard in the same context, and prints, per rule, how many were refused then, how
many are still refused now and how many pass now, with a sample of each. The context is rebuilt from the transcript:
the call's folder and session; for a subagent, its agent id and the crew member its `.meta.json` names
(`devkit:<member>`), whose roster tool level makes it a write lane or a read-only (review, read) lane. A subagent
that names no member is judged as a write lane with no member.

What it does not replay: the gates that depend on the live run at the time (a step's file scope, crew slots, the
irreversible-action gate, identity); those refusals are counted under `not replayed`. A refused script the call ran
from a scratch folder that no longer exists is judged as it would be now, with the file missing.

It reads only and prints only: a Python audit hook refuses every file write, folder change and program start other
than a read-only git command for the whole run, so a guard rule that would record something fails as it would on a
read-only disk. The output names commands of other projects: it stays on this Mac.
"""
import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARK = "PreToolUse:"
_GIT_READS = frozenset("""status rev-parse ls-files log show diff merge-base symbolic-ref branch remote cat-file
    for-each-ref check-ignore config describe rev-list name-rev worktree ls-tree var check-ref-format""".split())
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
_BLOCKED = Counter()


def _read_only_audit(event, args):
    """The audit hook that keeps the replay read-only: a write, a folder or file change, or a program other than a
    read-only git command raises PermissionError, which the guard's own error handling sees as a read-only disk."""
    if event == "open":
        path, mode, flags = (list(args) + [None, None, None])[:3]
        writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
            (isinstance(flags, int) and flags & _WRITE_FLAGS)
        if writes and str(path) not in ("/dev/null",):
            _BLOCKED["write"] += 1
            raise PermissionError(f"guard_replay reads only: {path}")
    elif event in ("os.mkdir", "os.rename", "os.remove", "os.rmdir", "os.chmod", "os.symlink", "os.link",
                   "os.truncate", "os.utime", "os.chown", "os.replace", "shutil.rmtree", "shutil.move"):
        _BLOCKED[event] += 1
        raise PermissionError(f"guard_replay reads only: {event}")
    elif event in ("subprocess.Popen", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.fork"):
        argv = list(args[1]) if event == "subprocess.Popen" and len(args) > 1 and args[1] else []
        words = [str(a) for a in argv]
        if words[:1] and os.path.basename(words[0]) == "git":
            sub = next((w for w in words[1:] if not w.startswith("-") and not _after_option(words, w)), "")
            if sub in _GIT_READS:
                return
        _BLOCKED["program"] += 1
        raise PermissionError(f"guard_replay reads only: {' '.join(words[:3]) or event}")


def _after_option(words, w):
    """True when `w` is the value of a git option that takes one (`-C <dir>`, `-c <k=v>`)."""
    i = words.index(w)
    return i > 0 and words[i - 1] in ("-C", "-c", "--git-dir", "--work-tree")


# --------------------------------------------------------------------------- reading the transcripts

def _refusals(root: Path, since: str):
    """Each refused tool call: {ts, file, tool, input, refusal, cwd, session, agent_id, member} in file order."""
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            if not name.endswith(".jsonl"):
                continue
            path = Path(dirpath) / name
            try:
                if time.strftime("%Y-%m-%d", time.gmtime(path.stat().st_mtime)) < since[:10]:
                    continue
                fh = path.open(encoding="utf-8", errors="replace")
            except OSError:
                continue
            aid = name[len("agent-"):-len(".jsonl")] if path.parent.name == "subagents" and \
                name.startswith("agent-") else ""
            member = _meta_member(path) if aid else ""
            uses = {}
            with fh:
                for line in fh:
                    if MARK not in line and '"tool_use"' not in line:
                        continue
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    content = (o.get("message") or {}).get("content")
                    if not isinstance(content, list):
                        continue
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        if c.get("type") == "tool_use":
                            uses[c.get("id")] = (c.get("name"), c.get("input"), o.get("cwd") or "",
                                                 o.get("sessionId") or "")
                        elif c.get("type") == "tool_result" and str(o.get("timestamp") or "") >= since:
                            text = c.get("content")
                            if isinstance(text, list):
                                text = " ".join(x.get("text", "") for x in text if isinstance(x, dict))
                            if not isinstance(text, str) or MARK not in text or "hook error" not in text:
                                continue
                            tool, inp, cwd, sid = uses.get(c.get("tool_use_id"), (None, None, "", ""))
                            if not tool or not isinstance(inp, dict):
                                continue
                            yield {"ts": o.get("timestamp"), "file": str(path), "tool": tool, "input": inp,
                                   "refusal": text, "cwd": cwd or o.get("cwd") or "",
                                   "session": sid or o.get("sessionId") or "", "agent_id": aid, "member": member}


def _meta_member(path: Path) -> str:
    """The crew member a subagent's `.meta.json` names by its agent type (`devkit:seraph` -> seraph), else ''."""
    try:
        meta = json.loads(path.with_suffix(".meta.json").read_text())
    except (OSError, ValueError):
        return ""
    kind = str(meta.get("agentType") or "")
    return kind.split(":", 1)[1] if kind.startswith("devkit:") else ""


# --------------------------------------------------------------------------- rule labels

# The refusals of the gates that read the live run (scope, slots, the irreversible gate, identity), not replayed
_NOT_REPLAYED = re.compile(r"is outside .{0,80}scope|crew slots?|crew slot|irreversible|This session speaks as|"
                           r"another session|DEVKIT_SESSION", re.I)
_LABELS = (
    ("lane-solo", r"as a command of its own"),
    ("readonly", r"^read-only lane"),
    ("pipe-commit", r"after a pipeline"),
    ("unclosed-quote", r"quote that is never closed"),
    ("program-writes-state", r"program writes files and names devkit's state"),
    ("program-writes-handoff", r"program writes files and names a project's handoff"),
    ("input-command", r"runs a command its input names"),
    ("tier2-unknown", r"whose name is only known when it runs|can call a function by a name"),
    ("backup-copy", r"backup cop"),
    ("hidden-agent", r"hides from what it runs that an agent"),
    ("install-copy", r"names a devkit copy|releases/` names"),
)


def then_label(refusal: str) -> str:
    """The rule a recorded refusal came from, by its text: a named class, else its first words without paths."""
    why = refusal.split("hook error:", 1)[-1].strip()
    for label, pat in _LABELS:
        if re.search(pat, why):
            return label
    why = re.sub(r"`[^`]*`", "`…`", why)
    why = re.sub(r"(?:~|/)[^\s`'\"]*", "<path>", why)
    return " ".join(why.split()[:8])[:70]


# --------------------------------------------------------------------------- judging again

def _synthetic_run(row: dict, level_of) -> dict:
    """The run a subagent's lane is judged in: its member and tool level, no scope (the scope gate is not
    replayed), so the lane rules and the member's scratch folder apply as they did."""
    aid = row["agent_id"]
    lane = {"persona": row["member"] or None, "state": "running", "binding": "exact", "scope": [], "label": aid}
    return {"agents": {aid: lane}, "plan": [], "project": None, "session": row["session"], "agent": "claude",
            "state": "running"}


def judge(row: dict, hooks, guard, progress):
    """(verdict now: 'refused' | 'passes' | 'error', the refusal id or error, the reason) for one recorded call."""
    data = {"session_id": row["session"] or "-", "cwd": row["cwd"] or str(Path.home()), "tool_name": row["tool"],
            "tool_input": row["input"], "hook_event_name": "PreToolUse"}
    if row["agent_id"]:
        data["agent_id"] = row["agent_id"]
        run = _synthetic_run(row, None)
        progress.lane_of = lambda agent, sid, aid, _run=run: (_run, _run["agents"].get(aid))
    try:
        v = guard.check(row["tool"], row["input"], data["cwd"], "claude", call=data)
        if v and v[0] == "deny":
            return "refused", guard.refusal_of(v)[0] or "guard", v[1]
        if row["agent_id"]:
            if hooks._route_subagent_progress(data, "claude"):
                return "passes", "", ""
            why = hooks._unroutable_lane_command(data, "claude")
            if why:
                v = ("deny", why)
                return "refused", guard.refusal_of(v)[0] or "hooks:" + hooks._lane_rule(why), why
        return "passes", "", ""
    except Exception as e:  # the hook fails closed on a shell call the guard cannot judge
        return "error", type(e).__name__, str(e)[:200]


def _command(row) -> str:
    inp = row["input"]
    return str(inp.get("command") or inp.get("cmd") or inp.get("file_path") or inp)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--since", default=time.strftime("%Y-%m-%d", time.gmtime(time.time() - 14 * 86400)),
                    help="the first day of refusals to replay (YYYY-MM-DD, UTC; default: 14 days ago)")
    ap.add_argument("--root", default=str(Path.home() / ".claude" / "projects"), help="the transcripts folder")
    ap.add_argument("--samples", type=int, default=2, help="sample commands per rule and outcome (default 2)")
    ap.add_argument("--rule", default="", help="only rules whose label matches this regular expression")
    ap.add_argument("--json", action="store_true", help="one JSON line per refusal instead of the table")
    args = ap.parse_args(argv)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.since):
        ap.error("--since takes a date, YYYY-MM-DD")

    import warnings
    warnings.simplefilter("ignore", SyntaxWarning)  # the guard parses the recorded programs; their escapes are theirs
    sys.path.insert(0, str(ROOT / "lib"))
    from devkit import audit, guard, hooks, progress  # noqa: E402  (this checkout's guard)
    audit.record = lambda *a, **k: None  # the guard's records: nothing to write
    hooks._log = lambda *a, **k: None
    sys.addaudithook(_read_only_audit)

    rows = [r for r in _refusals(Path(os.path.expanduser(args.root)), args.since)]
    stats = defaultdict(Counter)
    samples = defaultdict(list)
    now_ids = defaultdict(Counter)
    started = time.time()
    for row in rows:
        label = then_label(row["refusal"])
        if args.rule and not re.search(args.rule, label):
            continue
        ctx = "main" if not row["agent_id"] else ("lane:" + (row["member"] or "helper"))
        if _NOT_REPLAYED.search(row["refusal"]):
            outcome, rid, why = "not replayed", "", ""
        else:
            outcome, rid, why = judge(row, hooks, guard, progress)
        stats[label]["then"] += 1
        stats[label][outcome] += 1
        if rid:
            now_ids[label][rid] += 1
        if args.json:
            print(json.dumps({"ts": row["ts"], "context": ctx, "rule": label, "now": outcome, "id": rid,
                              "command": _command(row)[:400], "then": row["refusal"][:300], "why": why[:300]}))
        elif len(samples[(label, outcome)]) < args.samples:
            samples[(label, outcome)].append((ctx, _command(row), why))
    if args.json:
        return 0
    total = Counter()
    for c in stats.values():
        total.update(c)
    print(f"guard replay since {args.since}: {total['then']} refusals, {len(stats)} rules, "
          f"{time.time() - started:.0f}s; writes refused during the replay: {sum(_BLOCKED.values())}")
    print(f"{'rule':<52} {'then':>6} {'refused':>8} {'passes':>7} {'error':>6} {'not rep.':>8}")
    for label, c in sorted(stats.items(), key=lambda kv: -kv[1]["then"]):
        print(f"{label[:52]:<52} {c['then']:>6} {c['refused']:>8} {c['passes']:>7} {c['error']:>6} "
              f"{c['not replayed']:>8}")
    print(f"{'total':<52} {total['then']:>6} {total['refused']:>8} {total['passes']:>7} {total['error']:>6} "
          f"{total['not replayed']:>8}")
    if args.samples:
        for label, c in sorted(stats.items(), key=lambda kv: -kv[1]["then"]):
            print(f"\n== {label} (now: {', '.join(f'{k} {n}' for k, n in now_ids[label].most_common(4))})")
            for outcome in ("refused", "passes", "error"):
                for ctx, cmd, why in samples[(label, outcome)]:
                    print(f"  [{outcome} · {ctx}] {cmd[:220]!r}")
                    if why:
                        print(f"      now: {why[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
