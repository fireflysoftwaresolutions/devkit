# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Codex crew launches: the launch record, the crew slots and depth limit, the binding to a plan step, and the
member's required skills, made when the agent starts.

Codex (CLI 0.167 and the ChatGPT app) fires no PreToolUse for `collaboration.spawn_agent`, so what Claude Code's guard
does at an Agent call (hooks._guard: the crew limits, progress.note_agent_launch, the brief's skills) happens here, at
SubagentStart, through the same progress functions. What Codex gives at that point (captured 2026-11-09):

- SubagentStart carries `agent_id`, `agent_type` and, as `transcript_path`, the new agent's own rollout. Its first
  record, the session_meta, names the spawn: `source.subagent.thread_spawn` with the parent thread, the depth and
  `agent_path` (/root/…/<task_name>). Codex's own helpers (a review, a compaction) name another source.
- The spawning thread's rollout holds the `spawn_agent` call (`task_name` and `agent_type` in plain text, the brief
  encrypted) and a `SubAgentActivity` item whose id is the call's and whose `agent_thread_id` is the agent.
- SubagentStart output accepts `hookSpecificOutput.additionalContext`: the member's required skills, known pitfalls
  and report format go there, the text a Claude Code member finds appended to its brief.

The tech lead passes the plan step's name as `task_name` (`dev crew brief` prints it), so "Tank: invoice csv" binds
the step "invoice csv" as a Claude Code description would. A launch over the crew's limits cannot be refused before it
starts: it is recorded, its lane is marked (`over_limit`), its step goes back to the tech lead, the tech lead is told,
and the member is told to stop and report.
"""
import json
import re
import time
from pathlib import Path

SPAWN_TAIL = 4_000_000  # bytes of a spawning thread's rollout read for its spawn calls, from the end
META_MAX = 4_000_000  # a session_meta record carries the base instructions: tens of kilobytes
THREAD = re.compile(r"[A-Za-z0-9_-]{1,80}")


def session_meta(path) -> dict:
    """The payload of a Codex rollout's first record, its session_meta; {} when there is none (yet)."""
    try:
        with open(path, "rb") as fh:
            line = fh.readline(META_MAX)
        rec = json.loads(line) if line.strip() else {}
    except (OSError, TypeError, ValueError):
        return {}
    meta = rec.get("payload") if isinstance(rec, dict) and rec.get("type") == "session_meta" else None
    return meta if isinstance(meta, dict) else {}


def thread_spawn(meta: dict):
    """What a session_meta says started its thread: the `thread_spawn` source (a spawn_agent call), False for a
    subagent of another kind (Codex's own review, compaction or other helper), None for a thread no agent started."""
    src = meta.get("source")
    sub = src.get("subagent") if isinstance(src, dict) else None
    if sub is None:
        return None
    spawn = sub.get("thread_spawn") if isinstance(sub, dict) else None
    return spawn if isinstance(spawn, dict) else False


def task_of(agent_path) -> str:
    """The task name in a canonical agent path: "/root/list_files" -> "list_files"."""
    return str(agent_path or "").rstrip("/").rsplit("/", 1)[-1] if agent_path else ""


def spawn_calls(path) -> tuple:
    """The spawn_agent calls in the last SPAWN_TAIL bytes of a rollout, oldest first, [{call_id, task_name,
    agent_type}], and {call id: agent id} for the calls Codex recorded as started (SubAgentActivity)."""
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, 2)
            fh.seek(max(0, size - SPAWN_TAIL))
            lines = fh.read().splitlines()
    except (OSError, TypeError, ValueError):
        return [], {}
    calls, started = [], {}
    for ln in lines:
        if b'"spawn_agent"' not in ln and b'"SubAgentActivity"' not in ln:
            continue
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        p = rec.get("payload") if isinstance(rec, dict) and isinstance(rec.get("payload"), dict) else {}
        if p.get("type") == "function_call" and p.get("name") == "spawn_agent" and p.get("call_id"):
            try:
                args = json.loads(p.get("arguments") or "{}")
            except (TypeError, ValueError):
                args = {}
            args = args if isinstance(args, dict) else {}
            calls.append({"call_id": str(p["call_id"]), "task_name": str(args.get("task_name") or ""),
                          "agent_type": str(args.get("agent_type") or "")})
        item = p.get("item") if p.get("type") == "item_completed" else None
        if isinstance(item, dict) and item.get("type") == "SubAgentActivity" and item.get("kind") == "started" \
                and item.get("id") and item.get("agent_thread_id"):
            started[str(item["id"])] = str(item["agent_thread_id"])
    return calls, started


def rollout_of(near: Path, thread: str):
    """The rollout of thread `thread`: in the folder of `near` (Codex files a rollout under the day its thread
    started), else anywhere under the same sessions folder; None when there is none."""
    if not THREAD.fullmatch(thread or ""):
        return None
    hit = sorted(near.parent.glob(f"rollout-*-{thread}.jsonl"))
    if hit:
        return hit[-1]
    root = near.parent.parent.parent  # <sessions>/<year>/<month>/<day>/rollout-….jsonl
    hit = sorted(root.glob(f"*/*/*/rollout-*-{thread}.jsonl")) if root.name == "sessions" else []
    return hit[-1] if hit else None


def find(data: dict, launches: dict):
    """What spawned this Codex agent -> {key (the spawn call's id, or codex-<agent id>), task, type, parent (an agent
    id, or main)}, or None: Codex's own helper, or an agent no spawn names that runs no crew member. Read from the
    agent's own session_meta, then the spawning thread's rollout: the call Codex recorded as starting this agent,
    else the newest call of its agent type that started no other agent and no launch record holds."""
    from . import crew
    aid, sid = str(data.get("agent_id") or ""), str(data.get("session_id") or "")
    kind = str(data.get("agent_type") or "")
    own = str(data.get("agent_transcript_path") or data.get("transcript_path") or "")
    meta = session_meta(own) if own else {}
    spawn = {"key": "", "task": "", "type": kind, "parent": "main"}
    mine = meta.get("id") == aid  # the agent's own rollout (SubagentStart, and SubagentStop's agent_transcript_path)
    if mine:
        source = thread_spawn(meta)
        if source is False:
            return None
        if source:
            parent = str(source.get("parent_thread_id") or "")
            spawn.update(task=task_of(source.get("agent_path")), type=kind or str(source.get("agent_role") or ""),
                         parent=parent if parent and parent != sid else "main")
    thread = sid if spawn["parent"] == "main" else spawn["parent"]
    paths = [Path(own)] if own and not mine else []  # a payload that names the spawning thread's rollout itself
    found = rollout_of(Path(own), thread) if own else None
    paths += [found] if found and found not in paths else []
    held = {k for k, v in (launches or {}).items() if isinstance(v, dict) and v.get("agent_id") not in (None, "", aid)}
    for path in paths:
        if session_meta(path).get("id") not in (None, "", thread):
            continue  # another thread's rollout: a spawned agent's opens with a copy of its parent's turns
        calls, started = spawn_calls(path)
        call = next((c for c in calls if started.get(c["call_id"]) == aid), None) or next(
            (c for c in reversed(calls) if c["call_id"] not in started and c["call_id"] not in held
             and c["agent_type"] in ("", spawn["type"])), None)
        if call:
            spawn.update(key=call["call_id"], task=spawn["task"] or call["task_name"],
                         type=spawn["type"] or call["agent_type"])
            break
    member = crew.member(spawn["type"]) if spawn["type"] else None
    if not (mine and spawn["task"]) and not spawn["key"] and not (member and not member.get("main")):
        return None  # nothing names a spawn, and no crew member runs it
    spawn["key"] = spawn["key"] or f"codex-{aid}"
    return spawn


def label_of(spawn: dict) -> str:
    """The launch's description, as a Claude Code Agent call writes it: "<member>: <task>" ("Tank: invoice csv")."""
    from . import crew
    m = crew.member(spawn["type"]) if spawn.get("type") else None
    name = m["name"] if m and not m.get("main") else (spawn.get("type") or "Agent")
    task = (spawn.get("task") or "").replace("_", " ").strip()
    return f"{name}: {task}" if task else name


def record(agent: str, data: dict, judge: bool = True):
    """A Codex agent's start: its launch record, as progress.note_agent_launch writes one at a Claude Code Agent call,
    and its binding to the agent at once (progress.launch_result), so the SubagentStart that follows (on_subagent)
    opens its lane and binds the step its task names. `judge`: first ask progress.launch_verdict whether a Claude
    Code launch would have been refused (crew slots, depth, order); False at a stop, where nothing is left to limit.
    -> {key, label, type, parent, why: the refusal, or ""}, or None: an agent recorded already (a new turn of a known
    agent), Codex's own helper, or a session devkit has no record of."""
    from . import core, progress
    sid, aid = str(data.get("session_id") or ""), str(data.get("agent_id") or "")
    if not (sid and progress.AGENT_ID.fullmatch(aid)):
        return None
    sess = core.read_json(progress.session_path(agent, sid), {}) or {}
    if not isinstance(sess, dict) or not sess:
        return None
    launches = sess.get("launches") if isinstance(sess.get("launches"), dict) else {}
    if any(isinstance(v, dict) and v.get("agent_id") == aid for v in launches.values()):
        return None
    spawn = find(data, launches)
    if not spawn:
        return None
    label = label_of(spawn)
    call = {"session_id": sid, "tool_use_id": spawn["key"],
            "tool_input": {"description": label, "agent_type": spawn["type"]}}
    if spawn["parent"] != "main":
        call["agent_id"] = spawn["parent"]
    why = ""
    if judge and spawn["key"] not in launches:  # one recorded at its own call was judged there
        run = progress.load_run(agent, sid)
        if run and run.get("state") != "done":
            run = progress.normalize(run)
            progress.expire_stale(run)
            why = progress.launch_verdict(agent, call, run) or ""
    progress.note_agent_launch(agent, call)
    progress.launch_result(agent, {"session_id": sid, "tool_use_id": spawn["key"],
                                   "tool_response": {"agentId": aid}})
    return {"key": spawn["key"], "label": label, "type": spawn["type"], "parent": spawn["parent"], "why": why}


def mark_over_limit(agent: str, data: dict, launch: dict) -> str:
    """A launch that broke the crew's limits, after its lane opened: the lane is marked `over_limit` (the guard lets
    it read but not edit), the steps it took go back to the tech lead, who is told once, and the audit trail records
    it. -> what the member is told: stop now and report."""
    from . import approvals, crew, progress
    sid, aid, why = str(data.get("session_id") or ""), str(data.get("agent_id") or ""), launch["why"]
    now, who, given = time.time(), crew.who(launch["label"].split(":")[0].strip() or "a crew member"), []
    with progress._tx(agent, sid):
        run = progress.load_run(agent, sid)
        if isinstance(((run or {}).get("agents") or {}).get(aid), dict):
            run = progress.normalize(run)
            lane = run["agents"][aid]
            lane["over_limit"] = {"ts": round(now), "why": why[:400]}
            lane.pop("scope", None)
            reason = "launched over the crew's limit, which Codex cannot refuse before the agent starts; the step " \
                     "waits for a launch of its own"
            mine = [s for s in run.get("plan") or [] if s.get("owner") == aid and s.get("state") not in progress.DONE]
            given = [m["id"] for m in approvals.apply(run, [approvals._step_move(s, "pending", "main", reason)
                                                            for s in mine], now)]
            back = f" “{'”, “'.join(given)}” went back to you: launch it when a slot frees (dev crew next)." \
                if given else ""
            progress._post(run, "devkit", "main", f"devkit: {who} started over the crew's limit, which a Codex launch "
                           f"cannot be refused for before its agent starts: {why} It was told to stop and report, and "
                           f"may read but not edit.{back}", now, from_name="devkit", kind="notice")
            progress._save(run, touch_session=False)
    progress._audit("crew.over-limit", launch["label"].split(":")[0].strip() or aid[:8], why[:300], "marked",
                    project=(run or {}).get("project") or "", agent_id=aid[:16])
    return (f"devkit · stop now and report: the tech lead's launch of you is over the crew's limit, and Codex cannot "
            f"refuse a launch before its agent starts. {why} Do not start the task and change no file: end at once "
            f"with your report, status block first (**[Firefly> DevKit · {who}]** <project> · `<branch>` · <task>), "
            f"Done: not started, over the crew's limit, and `Needs from the tech lead:` this task, which waits for "
            f"room in the crew. devkit gave your step back to the tech lead.")


def brief_context(agent: str, data: dict, launch: dict, project: str) -> str:
    """What a Codex member's encrypted brief cannot carry: the text skillguard.on_agent_brief appends to a Claude
    Code brief (required skills, known pitfalls, report format), for the kind of work and the files of the plan step
    its launch bound to. '' when there is nothing to add."""
    from . import progress, skillguard
    run, _ = progress.lane_of(agent, str(data.get("session_id") or ""), str(data.get("agent_id") or ""))
    step = next((s for s in (run or {}).get("plan") or [] if s.get("owner") == data.get("agent_id")
                 and s.get("state") not in progress.DONE), None) or {}
    out = skillguard.on_agent_brief(agent, {"description": launch["label"], "prompt": ""}, project,
                                    activity=str(step.get("activity") or ""), files=step.get("scope") or None)
    return str((out or {}).get("prompt") or "").strip()
