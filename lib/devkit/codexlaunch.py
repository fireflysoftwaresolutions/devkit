# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Codex crew launches: the launch record, the crew slots and depth limit, the binding to a plan step, and the
member's required skills, made when the agent starts.

Codex (CLI 0.167 and the ChatGPT app) fires no PreToolUse for `collaboration.spawn_agent`, so what Claude Code's
PreToolUse hook does at an Agent call (the crew limits, progress.note_agent_launch, the brief's skills) happens here,
at SubagentStart, through the same progress functions. What Codex gives at that point (captured 2026-11-09):

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
import contextlib
import contextvars
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from . import core
from .core import is_under, real
from .lanescope import current_project, in_checkout

SPAWN_TAIL = 4_000_000  # bytes of a spawning thread's rollout read for its spawn calls, from the end
THREAD = re.compile(r"[A-Za-z0-9_-]{1,80}")
RECOVERY_CHUNK = 64 << 10
RECOVERY_SECONDS = 2.0
RECOVERY_CANDIDATES = 4096


def _same_prefix(before, after) -> bool:
    """Whether the first `before.st_size` bytes can still be the ones read: the same file, and either grown (Codex
    only appends to a rollout) or untouched. A shrink, a replacement, or a same-size rewrite breaks the snapshot."""
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or after.st_size < before.st_size:
        return False
    return after.st_size > before.st_size or after.st_mtime_ns == before.st_mtime_ns


def _snapshot_lines(stream, size, deadline, limit):
    """Consume the first `size` bytes of the file, retaining at most one bounded JSON record."""
    pending = bytearray()
    while size:
        if time.monotonic() >= deadline:
            raise ValueError("native recovery deadline exhausted")
        chunk = stream.read(min(RECOVERY_CHUNK, size))
        if not chunk:
            raise ValueError("native rollout truncated during recovery")
        size -= len(chunk)
        start = 0
        while start < len(chunk):
            end = chunk.find(b"\n", start)
            stop = len(chunk) if end < 0 else end + 1
            if len(pending) + stop - start > limit:
                raise ValueError("native rollout record exceeds recovery limit")
            pending.extend(chunk[start:stop])
            if end < 0:
                break
            if time.monotonic() >= deadline:
                raise ValueError("native recovery deadline exhausted")
            yield pending
            pending = bytearray()
            start = stop
    if pending:
        raise ValueError("native rollout ends with an incomplete record")


def lifecycle_events(path, thread_id: str, agent_ids) -> dict:
    """Latest native interruption for each known child of this thread.

    Read only the registered parent's own rollout and native completed items, never
    message text or a caller-supplied target path. An interaction only delivers a
    message: it cannot cancel a stop before the child's next admission check.
    """
    if not THREAD.fullmatch(str(thread_id or "")) or session_meta(path).get("id") != thread_id:
        return {}
    known = {aid for aid in agent_ids if isinstance(aid, str) and THREAD.fullmatch(aid)}
    try:
        with open(path, "rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - SPAWN_TAIL))
            lines = stream.read().splitlines()
    except (OSError, TypeError, ValueError):
        return {}
    events = {}
    for line in lines:
        if b'"SubAgentActivity"' not in line:
            continue
        try:
            rec = core.load_json(line)
            payload = rec.get("payload") if isinstance(rec, dict) else None
            if rec.get("type") != "event_msg" or not isinstance(payload, dict) or \
                    payload.get("type") != "item_completed" or payload.get("thread_id") != thread_id:
                continue
            item = payload.get("item")
            if not isinstance(item, dict) or item.get("type") != "SubAgentActivity" or \
                    item.get("kind") != "interrupted" or \
                    item.get("agent_thread_id") not in known or not item.get("id"):
                continue
            stamp = datetime.fromisoformat(str(rec.get("timestamp", "")).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                continue
            at = stamp.timestamp()
            aid = item["agent_thread_id"]
            if at > 0 and at >= events.get(aid, {}).get("at", 0):
                events[aid] = {"kind": item["kind"], "at": at, "call_id": item["id"]}
        except (ValueError, TypeError, AttributeError, OverflowError):
            continue
    return events


def reconcile_lifecycle(run: dict, session: dict, now: float) -> list:
    """Reconcile native stops through the normal lane transition; never resume a lane."""
    from . import progress
    if run.get("agent") != "codex" or not isinstance(session, dict) or \
            session.get("agent") != "codex" or session.get("session") != run.get("session"):
        return []
    sid = run["session"]
    lanes = run.get("agents") or {}
    events = lifecycle_events(session.get("transcript"), sid, lanes)
    moves = []
    for aid, event in events.items():
        if event["kind"] != "interrupted" or event["at"] > now:
            continue
        # The snapshot predates this lock: a child may already have resumed. The
        # existing lane stop takes this same reentrant session lock.
        with progress._tx("codex", sid):
            live = progress.load_run("codex", sid) or {}
            lane = (live.get("agents") or {}).get(aid) or {}
            if live.get("state") not in ("running", "blocked") or lane.get("state") != "running" or \
                    _admitted_at(lane) > event["at"]:
                continue
            moves += progress.lane_stopped(aid, "Codex native interruption", agent="codex", sid=sid,
                                           now=event["at"])
    return moves


def _admitted_at(lane: dict) -> float:
    """Precise admission times; rounded telemetry cannot supersede a native stop."""
    return max(float(lane.get(key) or 0) for key in ("started", "resumed_at"))


def session_meta(path) -> dict:
    """The payload of a Codex rollout's first record, its session_meta; {} when there is none (yet). It reads as much
    of the first line as progress does (progress.CODEX_META_MAX: the record carries the base instructions)."""
    from .progress import CODEX_META_MAX
    try:
        with open(path, "rb") as fh:
            line = fh.readline(CODEX_META_MAX)
        rec = core.load_json(line) if line.strip() else {}
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


def _timestamp(record):
    try:
        stamp = datetime.fromisoformat(str(record.get("timestamp", "")).replace("Z", "+00:00"))
        return stamp.timestamp() if stamp.tzinfo else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def spawn_calls(path, expected_meta=None, diagnostics=None) -> tuple:
    """Exact native calls and completed starts from one complete, bounded-cost snapshot.

    This proves launch linkage only. It never authenticates a shell invocation.
    A missing, partial or conflicting record leaves registration for a later hook.
    """
    from .progress import CODEX_META_MAX
    calls, started, seen = [], {}, set()
    deadline = time.monotonic() + RECOVERY_SECONDS
    retained, candidates = 0, 0

    def incomplete(reason):
        if diagnostics is not None:
            diagnostics.append(reason)
        return [], {}

    try:
        with open(path, "rb") as stream:
            before = os.fstat(stream.fileno())
            lines = _snapshot_lines(stream, before.st_size, deadline, CODEX_META_MAX)
            first = core.load_json(next(lines))
            meta = first.get("payload") if first.get("type") == "session_meta" else None
            if not isinstance(meta, dict) or expected_meta is not None and meta != expected_meta:
                return incomplete("parent metadata changed or is incomplete")
            created = _timestamp(first)
            for line in lines:
                if b'"spawn_agent"' not in line and b'"SubAgentActivity"' not in line:
                    continue  # most of a rollout is neither; parsing it all would spend the deadline
                record = core.load_json(line)
                if time.monotonic() >= deadline:
                    raise ValueError("native recovery deadline exhausted during parsing")
                if _timestamp(record) < created:
                    continue  # inherited fork history belongs to the parent, not this thread
                p = record.get("payload") or {}
                if record.get("type") == "response_item" and p.get("type") == "function_call" and \
                        p.get("name") == "spawn_agent" and p.get("namespace") == "collaboration":
                    cid = p.get("call_id")
                    if not cid or cid in seen:
                        return incomplete("duplicate or incomplete native launch call")
                    args = core.load_json(p.get("arguments") or "{}")
                    if not isinstance(cid, str) or not isinstance(args, dict):
                        return incomplete("malformed native launch arguments")
                    seen.add(cid)
                    calls.append({"call_id": cid, "task_name": args.get("task_name"),
                                  "agent_type": args.get("agent_type"), "at": _timestamp(record)})
                    candidates += 1
                    retained += len(json.dumps(calls[-1]).encode("utf-8"))
                item = p.get("item")
                if record.get("type") == "event_msg" and p.get("type") == "item_completed" and \
                        p.get("thread_id") == meta.get("id") and isinstance(item, dict) and \
                        item.get("type") == "SubAgentActivity" and item.get("kind") == "started":
                    started.setdefault(item.get("id"), []).append({"agent": item.get("agent_thread_id"),
                        "path": item.get("agent_path"), "at": _timestamp(record)})
                    candidates += 1
                    retained += len(json.dumps(item).encode("utf-8"))
                if candidates > RECOVERY_CANDIDATES or retained > CODEX_META_MAX:
                    raise ValueError("native recovery evidence retention exhausted")
            if time.monotonic() >= deadline:
                return incomplete("native recovery deadline exhausted")
            if not _same_prefix(before, os.fstat(stream.fileno())) or \
                    Path(path).is_symlink() or not _same_prefix(before, Path(path).stat()):
                return incomplete("parent rollout changed during recovery")
    except (OSError, TypeError, ValueError, AttributeError, StopIteration, RecursionError) as error:
        # Parser and OS messages can contain private native data or paths.
        reason = str(error) if type(error) is ValueError and str(error).startswith("native ") else \
            "parent rollout is incomplete, unreadable, or malformed"
        return incomplete(reason)
    return calls, started


def rollout_of(near: Path, thread: str):
    """One canonical rollout for this thread; duplicates are unresolved, never newest-wins."""
    from . import core
    if not THREAD.fullmatch(thread or ""):
        return None
    root = core.HOME / ".codex/sessions"
    hits = list(root.glob(f"*/*/*/rollout-*-{thread}.jsonl"))
    if len(hits) != 1 or hits[0].is_symlink():
        return None
    return hits[0]


def look(data: dict):
    """Reciprocal lineage named by a trusted child lifecycle/tool hook, without caller inference."""
    aid, sid = str(data.get("agent_id") or ""), str(data.get("session_id") or "")
    own = str(data.get("agent_transcript_path") or data.get("transcript_path") or "")
    path = rollout_of(Path(own), aid)
    if path is None or path.resolve() != Path(own).resolve():
        return None
    meta = session_meta(path)
    source = thread_spawn(meta)
    if not isinstance(source, dict):
        return None
    parent_id = source.get("parent_thread_id")
    parent = rollout_of(path, parent_id)
    pm = session_meta(parent) if parent else {}
    depth = (thread_spawn(pm) or {}).get("depth", 0) if isinstance(thread_spawn(pm), (dict, type(None))) else -1
    expected = (pm.get("agent_path") or "/root") + "/" + task_of(source.get("agent_path"))
    kind = data.get("agent_type") or ""
    if not isinstance(depth, int) or isinstance(depth, bool) or meta.get("id") != aid or \
            meta.get("session_id") != sid or pm.get("id") != parent_id or pm.get("session_id") != sid or \
            meta.get("parent_thread_id") != parent_id or source.get("depth") != depth + 1 or \
            meta.get("agent_path") != expected or source.get("agent_path") != expected or \
            not kind or kind != meta.get("agent_role") or kind != source.get("agent_role"):
        return None
    diagnostics = []
    calls, started = spawn_calls(parent, expected_meta=pm, diagnostics=diagnostics)
    return {"spawn": {"task": task_of(expected), "type": kind,
                      "parent": "main" if parent_id == sid else parent_id},
            "path": expected, "calls": calls, "started": started, "incomplete": "; ".join(diagnostics)}


def find(data: dict, seen: dict, launches: dict):
    """Only an exact native call/result pair can bind a previously unknown child."""
    aid = data.get("agent_id")
    spawn = seen["spawn"]
    matches = []
    for call in seen["calls"]:
        events = seen["started"].get(call["call_id"], [])
        if len(events) != 1:
            continue
        event = events[0]
        held = (launches or {}).get(call["call_id"]) or {}
        if event["agent"] == aid and event["path"] == seen["path"] and \
                call["task_name"] == spawn["task"] and (call["agent_type"] or "default") == spawn["type"] and \
                0 < call["at"] <= event["at"] <= time.time() and held.get("agent_id") in (None, "", aid):
            matches.append(dict(spawn, key=call["call_id"], at=call["at"], waiting=[]))
    return matches[0] if len(matches) == 1 else None


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
    agent), Codex's own helper, or a session devkit has no record of. The rollouts are read first; the call is chosen,
    judged, recorded and bound inside one hold of the session's lock, so two starts at once never take the same call
    or the same free slot."""
    from . import core, progress, state

    def known(sess) -> bool:
        ls = sess.get("launches") if isinstance(sess.get("launches"), dict) else {}
        return any(isinstance(v, dict) and v.get("agent_id") == aid for v in ls.values())

    sid, aid = str(data.get("session_id") or ""), str(data.get("agent_id") or "")
    if not (sid and progress.AGENT_ID.fullmatch(aid)):
        return None
    sess = core.read_json(progress.session_path(agent, sid), {}) or {}
    if not isinstance(sess, dict) or not sess or known(sess):
        return None
    seen = look(data)
    if not seen:
        _registration_pending(agent, data, sess)
        return None
    with progress._tx(agent, sid):  # the session's one lock: its launch records and its run
        sess = state.load(progress.session_path(agent, sid)) or {}
        if not sess or known(sess):
            return None
        launches = sess.get("launches") if isinstance(sess.get("launches"), dict) else {}
        spawn = find(data, seen, launches)
        if not spawn:
            _registration_pending(agent, data, sess, seen["calls"], reason=seen.get("incomplete"))
            return None
        if spawn["parent"] != "main" and not (sess.get("subagents") or {}).get(spawn["parent"], {}).get("launch"):
            return None
        run = progress.load_run(agent, sid)
        if run and spawn["at"] < run.get("started", 0):
            return None  # an old native launch cannot acquire a fresh run's assignment
        label = label_of(spawn)
        prior = launches.get(spawn["key"])
        if prior:
            same_run = prior.get("run_started") == (run or {}).get("started") if "run_started" in prior else \
                prior.get("ts", 0) >= (run or {}).get("started", 0)
            if prior.get("state") != "pending" or prior.get("tool_use_id") != spawn["key"] or \
                    (prior.get("type") or "default") != spawn["type"] or \
                    prior.get("parent") != spawn["parent"] or prior.get("agent_id") not in (None, "", aid) or \
                    not same_run:
                _registration_pending(agent, data, sess, seen["calls"])
                return None
            if prior.get("label") != label:
                # Earlier parent hooks used the message as a derived label. The
                # exact native call/result can correct that label, never its
                # immutable role, parent, child, call ID or run ownership.
                prior["label"] = label
                state.replace(progress.session_path(agent, sid), sess)
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
        progress.note_agent_launch(agent, call, now=spawn["at"])
        progress.launch_result(agent, {"session_id": sid, "tool_use_id": spawn["key"],
                                       "tool_response": {"agentId": aid}})
        if spawn["waiting"]:
            _tell_unplaced(agent, sid, aid, spawn, label)
    if spawn["waiting"]:
        progress._audit("crew.launch-unplaced", label.split(":")[0].strip() or aid[:8],
                        f"{len(spawn['waiting'])} spawns of {spawn['type']} waited for their agent: "
                        f"{', '.join(spawn['waiting'])}"[:300], "unbound", project=sess.get("project") or "",
                        agent_id=aid[:16])
    return {"key": spawn["key"], "label": label, "type": spawn["type"], "parent": spawn["parent"], "why": why}


def _registration_pending(agent, data, sess, calls=None, reason=None):
    """A failed linkage grants nothing; tell the lead once so a later hook can retry."""
    from . import crew, progress
    member = crew.member(data.get("agent_type") or "")
    if not member or member.get("main"):
        return
    aid, sid = data["agent_id"], data["session_id"]
    if calls is None:
        calls, _ = spawn_calls(sess.get("transcript"))
    names = ", ".join(f"“{c['task_name']}”" for c in calls if c.get("task_name"))
    with progress._tx(agent, sid):
        run = progress.load_run(agent, sid)
        if not run or run.get("state") == "done" or any(
                aid in m.get("text", "") and "could not verify the native launch" in m.get("text", "")
                for m in run.get("messages", [])):
            return
        progress._post(run, "devkit", "main", f"devkit could not verify the native launch of Codex agent {aid}"
                       f" ({reason or names or 'no complete parent spawn'}). It takes no step or scope. Wait for the native "
                       "launch records, then retry its tool call; if hooks stay missing, stop and report.",
                       time.time(), from_name="devkit", kind="notice")
        progress._save(run, touch_session=False)


def _tell_unplaced(agent: str, sid: str, aid: str, spawn: dict, label: str):
    """Tell the tech lead which launch devkit could not place: an agent started while two or more spawns of its type
    waited for their agent and nothing said which one was its own. Under record's hold of the session's lock."""
    from . import crew, progress
    run = progress.load_run(agent, sid)
    if not run or run.get("state") == "done":
        return
    run = progress.normalize(run)
    who, kind, tasks = crew.who(label.split(":")[0].strip() or "a crew member"), spawn["type"], spawn["waiting"]
    progress._post(run, "devkit", "main", f"devkit could not tell which of {len(tasks)} spawns of {kind} started "
                   f"{who} (Codex agent {aid}): “{'”, “'.join(tasks)}” all waited for their agent, and Codex had not "
                   f"yet recorded which one started it. It runs with none of their steps rather than a guess. Spawn "
                   f"one {kind} at a time, or stop it and spawn that step again.", time.time(), from_name="devkit",
                   kind="notice")
    progress._save(run, touch_session=False)


def mark_over_limit(agent: str, data: dict, launch: dict) -> str:
    """A launch past the crew's limits, after its lane opened: the lane is marked `over_limit`, the steps it took go
    back to the tech lead, who is told once, and the audit trail records it. -> what the member is told: stop now and
    report."""
    from . import approvals, crew, progress
    sid, aid, why = str(data.get("session_id") or ""), str(data.get("agent_id") or ""), launch["why"].strip()
    why += "" if why.endswith((".", "!", "?")) else "."  # it is followed by the next sentence
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
            progress._post(run, "devkit", "main", f"devkit: {who} started over the crew's limit, which devkit cannot "
                           f"refuse for a Codex launch before its agent starts: {why} It was told to stop and "
                           f"report.{back}", now, from_name="devkit", kind="notice")
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


# --------------------------------------------------------------------------- Codex scratch folders
#
# A Codex session's scratch folders come from its registered session record, never from a tool call's workdir: the
# ChatGPT app's work/ and outputs/ (a member's work/scratchpad/<member>/), or the CLI's TMP/codex/<session>/scratchpad/
# <member or main>/. codex_scratch_context keeps them for one hook event (APP_SCRATCH), so the hook makes a member's
# folder and the read-only classifier counts a redirect there as no write (app_scratch_allows).
APP_SCRATCH = contextvars.ContextVar("devkit_codex_scratch", default=None)


def _codex_registered_checkout(rec, cwd):
    """True when an app event's checkout matches the session record's registered, canonical repository facts."""
    from .registry import main_of
    top, pid = rec.get("top"), rec.get("project")
    if not isinstance(pid, str) or not pid:
        return False
    for path in (top, cwd):
        if not isinstance(path, str) or not os.path.isabs(path) or \
                path != os.path.normpath(path) or path != real(path) or not Path(path).is_dir():
            return False
    if not is_under(cwd, top) or core.gitout(cwd, "rev-parse", "--show-toplevel") != top:
        return False
    main = main_of(Path(top))
    canonical = str(core.canonical_path(pid))
    return bool(main and real(main) == canonical and current_project(top) == pid and
                (top == canonical or is_under(top, core.WT_ROOT)))


def _codex_scratch_registration(agent, data):
    """(root, scratch folders, member) of a registered Codex session for this hook event, from its session record and
    never from the tool's workdir; None for Claude, a malformed or absent record, or a member the record does not
    place. No path becomes a temporary folder of its own."""
    value = None
    if agent == "codex" and isinstance(data, dict) and isinstance(data.get("session_id"), str):
        from . import progress
        sid = data["session_id"]
        if not sid or any(char in sid for char in ("/", "\\", "\0")):
            return None
        rec = core.read_json(progress.session_path(agent, sid), {})
        if isinstance(rec, dict) and rec.get("agent") == agent and rec.get("session") == sid:
            root = rec.get("cwd")
            if isinstance(root, str) and os.path.isabs(root) and root == os.path.normpath(root) == real(root):
                app = is_under(root, core.HOME / "Documents" / "Codex") and \
                    root != real(core.HOME / "Documents" / "Codex")
                bound_checkout = data.get("cwd") != root
                if bound_checkout and (not app or not _codex_registered_checkout(rec, data.get("cwd"))):
                    return None
                if not app:
                    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", sid):
                        return None
                    from .registry import main_of
                    top = core.gitout(root, "rev-parse", "--show-toplevel")
                    pid = current_project(root)
                    main = main_of(Path(top)) if top else None
                    if not pid or not top or real(main or top) != str(core.canonical_path(pid)) or \
                            not (top == str(core.canonical_path(pid)) or is_under(top, core.WT_ROOT)):
                        return None
                aid, member = data.get("agent_id"), ""
                if bound_checkout and aid is not None and not isinstance(aid, str):
                    return None
                if aid:
                    from . import hooks
                    run, lane = progress.lane_of(agent, sid, aid) if isinstance(aid, str) else (None, None)
                    if lane:
                        member = hooks._member_folder(run, aid)
                    if (not app or bound_checkout) and \
                            (not run or run.get("agent") != agent or run.get("session") != sid):
                        return None
                    if bound_checkout and run.get("project") != rec.get("project"):
                        return None
                    if not re.fullmatch(r"[A-Za-z0-9_-]+", member):
                        member = ""
                if not aid or member:
                    if app:
                        folders = (str(Path(root) / "work" / "scratchpad" / member),) if member else \
                            (str(Path(root) / "work"), str(Path(root) / "outputs"))
                    else:
                        root = str(core.TMP_ROOT / "codex")
                        folder = str(Path(root) / sid / "scratchpad" / (member or "main"))
                        if root != real(root) or folder != real(folder):
                            return None
                        folders = (folder,)
                    value = (root, folders, member)
    return value


@contextlib.contextmanager
def codex_scratch_context(agent, data):
    """Keep a registered Codex session's scratch folders (_codex_scratch_registration) for one hook event; a malformed
    record gives none."""
    try:
        value = _codex_scratch_registration(agent, data)
    except (OSError, ValueError, TypeError, AttributeError):
        value = None
    token = APP_SCRATCH.set(value)
    try:
        yield
    finally:
        APP_SCRATCH.reset(token)


def app_scratch_allows(path, member=None):
    """True when `path` lies in a scratch folder of this event's registered Codex session (codex_scratch_context), of
    crew member `member` when given, and outside any git checkout."""
    ctx = APP_SCRATCH.get()
    if not ctx or member is not None and member != ctx[2]:
        return False
    return any(folder == real(folder) and is_under(path, folder) for folder in ctx[1]) and not in_checkout(path)
