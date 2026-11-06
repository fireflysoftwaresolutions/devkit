# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""`dev mcp`: devkit's tools for Claude Desktop (chat and Cowork) over the Model Context Protocol.

A stdlib MCP server on stdio: JSON-RPC 2.0, one message per line. Every tool runs a `dev` command as a subprocess
(an argument list, never a shell) with a timeout, NO_COLOR=1 and DEVKIT_AGENT=claude-desktop, and returns its output
as compact text, plus structuredContent when the command printed JSON. The reads are the commands a developer runs
in a terminal; the two writes (devkit_task_add, devkit_task_update) run `dev task`, so the guard and the audit trail
apply exactly as they do there.

The Claude Desktop extension (desktop/, built with `dev desktop pack`) starts desktop/server/launcher.py, which finds
this checkout and execs `dev mcp`. Updating devkit therefore updates the tools without reinstalling the extension.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

from . import core

# Newest first. The client's version is echoed when it is one of these; otherwise the newest is offered.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
AGENT = "claude-desktop"
DEFAULT_TIMEOUT = 30
PROGRESS_EVERY = 10          # seconds between progress notifications while a command runs
MAX_TEXT = 40000             # characters of text per result; the rest is cut with a note
DEV_BIN = core.DEVKIT_HOME / "bin" / "dev"

INSTRUCTIONS = (
    "devkit is Firefly Software's developer workspace: every repository in a fixed place under ~/Development, and a "
    "handoff repository that keeps each project's status, tasks (Now / Next / Later / Done), plans, lessons and "
    "session log, shared by Claude Code, Codex and the team. These tools read that workspace and handoff on this Mac "
    "through the `dev` command. devkit_task_add and devkit_task_update change tasks through the same command, so "
    "devkit's guard and audit trail apply; add or change tasks only when the developer asks. Project ids look like "
    "owner/repo: call devkit_overview first to find them.\n\n"
    "When you report a project's development status, start the reply with the devkit status block, then a blank "
    "line, then the rest:\n"
    "**[Firefly> DevKit]** <project> · `<branch>` · <the task, in a few plain words>\n"
    "- **Done:** <what was done or found, one sentence>\n"
    "- **Check:** <✓ verified | ⚠ partly or not verified | ✗ failed | – nothing to verify> <the evidence>\n"
    "- **Next:** <the next step, and who takes it>\n"
    "Use `-` for a part that does not apply. Each bullet is one plain sentence of about 100 characters at most."
)

# --------------------------------------------------------------------------- tool definitions

TOOLS = []      # what tools/list returns: name, title, description, inputSchema, annotations
_HANDLERS = {}  # name -> (argv builder, timeout, json output?, digest, exit 1 is a report?)

READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
READ_REMOTE = dict(READ, openWorldHint=True)
WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}

PROJECT = {"type": "string", "minLength": 1, "maxLength": 200,
           "description": "Project id as devkit_overview lists it, for example `acme/billing` (a unique part of the "
                          "name also works). Optional only when the server runs inside a project."}
INITIATIVE = {"type": "string", "minLength": 1, "maxLength": 100,
              "description": "Initiative slug as devkit_overview lists it (a program that spans several projects). "
                             "Give `project` or `initiative`, not both."}

# Server-side checks on top of the schema: identifiers never start with '-' and carry no spaces or shell syntax.
_PATTERNS = {
    "project": re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9._/@+-]*$"),
    "initiative": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
    "branch": re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/+-]*$"),
    "action_prefix": re.compile(r"^[A-Za-z0-9._-]+$"),
}


def _tool(name, title, description, props, argv, annotations, required=(), timeout=DEFAULT_TIMEOUT,
          json_out=False, digest=None, report_on_exit1=False):
    schema = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    TOOLS.append({"name": name, "title": title, "description": description, "inputSchema": schema,
                  "annotations": dict(annotations, title=title)})
    _HANDLERS[name] = (argv, timeout, json_out, digest, report_on_exit1)


def _scope(a):
    if a.get("project") and a.get("initiative"):
        raise ValueError("give `project` or `initiative`, not both")
    if a.get("initiative"):
        return ["--initiative=" + a["initiative"]]
    return ["--project=" + a["project"]] if a.get("project") else []


def _iso(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts))) if ts else ""
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _short(text, n=240) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _digest_overview(d, a):
    """`dev overview --json` holds every initiative's full note (well over 100 KB): keep what a reply needs."""
    days = d.get("days", a.get("days", 7))
    inits, members = [], set()
    for i in d.get("initiatives") or []:
        mem = i.get("members") or []
        members |= {m.get("id") for m in mem}
        t = i.get("tasks") or {}
        inits.append({"id": i.get("id"), "title": i.get("title"), "status": i.get("status"),
                      "priority": i.get("priority"), "milestone": i.get("milestone"), "due": i.get("due"),
                      "progress": i.get("progress"), "projects": len(mem),
                      "cloned_here": sum(1 for m in mem if m.get("local")),
                      "tasks": {"doing": t.get("doing", 0), "blocked": t.get("blocked", 0), "open": t.get("open", 0),
                                "done": t.get("done", 0), "overdue": len(t.get("overdue") or [])},
                      "now": [_short(x, 200) for x in (t.get("now") or [])[:3]]})
    projects, hidden = [], 0
    for x in d.get("projects") or []:
        if not a.get("include_all") and (x.get("status") == "archived" or (
                x.get("id") in members and not x.get("local") and not x.get("live_sessions"))):
            hidden += 1
            continue
        sl = x.get("status_line") or {}
        projects.append({"id": x.get("id"), "status": x.get("status"), "priority": x.get("priority"),
                         "milestone": x.get("milestone"), "due": x.get("due"), "progress": x.get("progress"),
                         "branch": x.get("branch"), "local": x.get("local"), "initiatives": x.get("initiatives"),
                         "now": [_short(t, 200) for t in (x.get("now") or [])[:3]],
                         "open_tasks": x.get("open_tasks"), "open_prs": x.get("open_prs"),
                         "live_sessions": x.get("live_sessions"), f"commits_{days}d": x.get(f"commits_{days}d"),
                         "last_activity": _iso(x.get("last_activity")),
                         "status_block": _short(sl.get("line"), 400),
                         "status_block_by": f"{sl.get('agent', '')} {_iso(sl.get('ts'))}".strip() if sl else "",
                         "last_session": _short(x.get("last_session"), 240)})
    out = {"generated": _iso(d.get("generated")), "days": days, "canary": d.get("canary"),
           "initiatives": inits, "projects": projects,
           "issues": [_short(m, 240) for _, m in (d.get("issues") or [])[:10]]}
    if hidden:
        out["hidden_projects"] = (f"{hidden} project(s) not shown: archived, or initiative members not cloned on "
                                  "this Mac (summarized in their initiative); include_all lists them")
    return out


def _digest_audit(records, a):
    limit = a.get("limit", 50)
    counts = {}
    for r in records:
        k = str(r.get("action", "")).split(".")[0]
        counts[k] = counts.get(k, 0) + 1
    shown = records[-limit:]
    return {"total": len(records), "shown": len(shown),
            "by_action": dict(sorted(counts.items(), key=lambda kv: -kv[1])[:15]),
            "records": [{"time": _iso(r.get("ts")), "who": f"{r.get('user')}@{r.get('host')}", "agent": r.get("agent"),
                         "action": r.get("action"), "target": _short(r.get("target"), 200),
                         "outcome": r.get("outcome"), "detail": _short(r.get("detail"), 200)} for r in shown]}


_tool("devkit_overview", "Portfolio overview",
      "The developer's whole portfolio from devkit: each initiative (a program across projects) with its progress, "
      "milestone and Now tasks, and each project with its status, priority, milestone, branch, Now tasks, open pull "
      "requests, live agent sessions, recent commits and latest status block, plus what needs attention. Start here "
      "to find the project ids (owner/repo) and initiative slugs the other tools take.",
      {"days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 7,
                "description": "Window for recent commits, in days."},
       "include_all": {"type": "boolean", "default": False,
                       "description": "Also list archived projects and initiative members not cloned on this Mac."}},
      lambda a: ["overview", "--json", f"--days={a['days']}"], READ, json_out=True, digest=_digest_overview)

_tool("devkit_project_status", "Project status",
      "Status of one project as `dev status` shows it: branch and working tree state, worktrees, pull requests, the "
      "latest status block and what needs attention. Without `project` it shows the workspace home. Use "
      "devkit_tasks for its work items and devkit_ci_status for its CI runs.",
      {"project": PROJECT},
      lambda a: ["status"] + ([a["project"]] if a.get("project") else []), READ_REMOTE, timeout=60)

_tool("devkit_tasks", "Tasks",
      "Work items of a project or an initiative from the handoff repository: Now, Next and Later with each item's "
      "id, type, status and lock holder, and recently done items. Use the ids with devkit_task_update.",
      {"project": PROJECT, "initiative": INITIATIVE,
       "include_done": {"type": "boolean", "default": False, "description": "List every done item, not only recent ones."}},
      lambda a: ["task"] + _scope(a) + (["--all"] if a.get("include_done") else []), READ)

_tool("devkit_standup", "Standup",
      "What happened across every project in the last days (tasks done, the developer's commits, agent sessions) "
      "and what is in progress now. Good for a daily standup or for 'what did I do yesterday?'.",
      {"days": {"type": "integer", "minimum": 1, "maximum": 30, "default": 1, "description": "How many days back."}},
      lambda a: ["standup", f"--days={a['days']}"], READ)

_tool("devkit_report", "Status report",
      "A status report in Markdown for the team or management: health with its reason, progress, milestone, done, "
      "in progress, next, blockers and risks, and team activity, for the whole portfolio or for one project or "
      "initiative.",
      {"project": PROJECT, "initiative": INITIATIVE,
       "days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 7, "description": "Reporting window in days."}},
      lambda a: ["report", f"--days={a['days']}"] + _scope(a), READ, timeout=60)

_tool("devkit_ci_status", "CI status",
      "Live CI/CD runs of a project's branch (GitHub Actions, GitLab CI, Bitbucket Pipelines or Azure Pipelines): "
      "each run's status, its jobs and current step, elapsed time and an ETA from recent successful runs. Asks the "
      "CI provider through the developer's existing sign-in. Defaults to the branch checked out in the project.",
      {"project": PROJECT,
       "branch": {"type": "string", "minLength": 1, "maxLength": 200, "description": "Runs of this branch instead."}},
      lambda a: ["ci"] + ([a["project"], "--status"] if a.get("project") else ["status"]) + ["--json", "--limit=5"]
      + ([f"--branch={a['branch']}"] if a.get("branch") else []), READ_REMOTE, timeout=60, json_out=True)

_tool("devkit_lessons", "Lessons",
      "Lessons learned that apply to a project, plus the global ones: blameless records of past mistakes with what "
      "happened, the root cause and the prevention, and whether automation already prevents them. Use `search` to "
      "find lessons about an area before advising on it.",
      {"search": {"type": "string", "minLength": 1, "maxLength": 200, "description": "Words to search for in every lesson."},
       "project": PROJECT},
      lambda a: ["lesson"] + ([f"--search={a['search']}"] if a.get("search") else [])
      + ([f"--project={a['project']}"] if a.get("project") else []), READ)

_tool("devkit_delivery_policy", "Delivery policy",
      "How a project versions, tests and releases: what devkit detects in the repository (versioning, latest tag, "
      "version file, changelog, CI, the test gate CI runs, branch model, what it publishes or deploys), the recorded "
      "or recommended policy, and the gaps with their fixes. For an initiative, its recorded release-train policy.",
      {"project": PROJECT, "initiative": INITIATIVE},
      lambda a: ["delivery", "--json"] + _scope(a), READ, json_out=True)

_tool("devkit_stubs", "Unfinished work",
      "Unfinished work in a project: TODO, FIXME, 'not implemented', placeholders and empty function bodies that "  # devkit: allow-stub describes the markers
      "name no task, issue or reason. By default the uncommitted changes; with everything=true, every tracked file. "
      "devkit builds things for real: planned work names its task (TODO(T-…)).",  # devkit: allow-stub describes the markers
      {"project": PROJECT, "everything": {"type": "boolean", "default": False,
                                          "description": "Scan every tracked file, not only the uncommitted changes."}},
      lambda a: ["stubs", "--json"] + (["--all"] if a.get("everything") else [])
      + ([f"--project={a['project']}"] if a.get("project") else []), READ, json_out=True)

_tool("devkit_test_plan", "Test plan",
      "Which tests a project's current changes can break, and why: each changed file mapped to the tests that can see "
      "it (the blast radius), the last failures, and the time estimate. Nothing runs. Agents use it to run only what "
      "a change can break instead of the whole suite.",
      {"project": PROJECT},
      lambda a: ["test"] + ([f"--project={a['project']}"] if a.get("project") else []) + ["plan", "--json"], READ,
      json_out=True)

_tool("devkit_ci_audit", "CI/CD audit",
      "Diagnoses a project's CI/CD pipelines (GitHub Actions, GitLab CI, Bitbucket Pipelines, Azure Pipelines): "
      "findings by severity in five areas (speed, reliability, security and DevSecOps, quality gates, delivery), each "
      "with the file, line and fix, plus actionlint and zizmor results for GitHub when installed. Read-only: it never "
      "changes a file.",
      {"project": PROJECT},
      lambda a: ["ci", "audit", "--json"] + ([f"--project={a['project']}"] if a.get("project") else []), READ,
      json_out=True)

_tool("devkit_team", "Team",
      "Who is working on what across the developer's machines and teammates: live agent sessions with project, "
      "branch and status, and the task locks each person holds.",
      {"hours": {"type": "number", "minimum": 1, "maximum": 720, "default": 24,
                 "description": "Machines active in the last N hours."}},
      lambda a: ["team", f"--hours={a['hours']:g}"], READ)

_tool("devkit_crew", "Crew",
      "devkit's crew of specialist agents (Neo, Trinity, Seraph, The Keymaker and the others) that Claude Code and "
      "Codex sessions delegate to: each member's role, when to use them, skills, tier and tools. When a session on "
      "this Mac is running a progress run, also who is working now (lanes with state, minutes and the files they "
      "own), the plan steps assigned to each member, who is ready to launch, queued or waiting and why, open crew "
      "messages, the mode (guided: the developer decides; auto: the crew goes on and records) with its approvals, "
      "and the decisions waiting for the developer.",
      {}, lambda a: ["crew", "--json", "--latest"], READ, json_out=True)

_tool("devkit_doctor", "Doctor",
      "Checks the whole devkit setup on this Mac (toolchain, GitHub sign-in and git identity, workspace and handoff "
      "repository, Claude Code and Codex, skills, hooks, Docker, scheduled jobs) and gives the fix for each failed "
      "check. Takes up to about a minute.",
      {}, lambda a: ["doctor"], READ_REMOTE, timeout=120, report_on_exit1=True)

_tool("devkit_audit", "Audit trail",
      "devkit's audit trail on this Mac: what devkit changed or refused (task and plan changes, git and GitHub "
      "actions, guard refusals, settings repairs, scheduled job runs) with time, who, agent, target and outcome, "
      "newest last.",
      {"days": {"type": "number", "minimum": 0.01, "maximum": 366, "default": 7, "description": "How many days back."},
       "action": {"type": "string", "minLength": 1, "maxLength": 60,
                  "description": "Only actions starting with this, for example guard, task, job, handoff, dev.commit."},
       "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50,
                 "description": "Most recent records to return."}},
      lambda a: ["audit", "--json", f"--days={a['days']:g}"] + ([f"--action={a['action']}"] if a.get("action") else []),
      READ, json_out=True, digest=_digest_audit)

_tool("devkit_jobs", "Scheduled jobs",
      "devkit's scheduled background jobs (sync, sleep, gc, repair, audit, doctor, lessons): whether each is on, "
      "its schedule, the last run and its result, and the next run.",
      {}, lambda a: ["jobs"], READ)

_tool("devkit_task_add", "Add task",
      "Add a work item to a project's or an initiative's task list in the handoff repository, through `dev task "
      "add`: it is recorded in the audit trail and synced to the team. Add tasks only when the developer asks. "
      "`when` puts it in Now, Next or Later; `why` is recorded in the item's changelog.",
      {"title": {"type": "string", "minLength": 3, "maxLength": 200, "description": "What to do, as a short imperative."},
       "project": PROJECT, "initiative": INITIATIVE,
       "type": {"type": "string", "enum": ["task", "feature", "bug", "spike"], "default": "task",
                "description": "Kind of work item."},
       "when": {"type": "string", "enum": ["now", "next", "later"], "description": "Which list it goes to."},
       "why": {"type": "string", "minLength": 3, "maxLength": 500, "description": "Why this work exists."}},
      lambda a: ["task", "add"] + _scope(a) + [f"--type={a['type']}", f"--{a['when']}"]
      + ([f"--why={a['why']}"] if a.get("why") else []) + ["--", a["title"]],
      WRITE, required=("title", "when"))

_tool("devkit_task_update", "Update task",
      "Change a task's state through `dev task`: start (claim its lock and move it to Now, so others see it is "
      "taken), done (mark it done and release the lock), block (needs `why`), unblock, or release (give the lock "
      "back). Recorded in the item's history and the audit trail. Change tasks only when the developer asks.",
      {"id": {"type": "string", "minLength": 1, "maxLength": 200,
              "description": "The task's T-… id from devkit_tasks, its number in that list, or a unique part of its title."},
       "action": {"type": "string", "enum": ["start", "done", "block", "unblock", "release"],
                  "description": "What to do with the task."},
       "why": {"type": "string", "minLength": 3, "maxLength": 500,
               "description": "The reason, recorded in the item's changelog (required to block)."},
       "project": PROJECT, "initiative": INITIATIVE},
      lambda a: ["task", a["action"]] + _scope(a) + ([f"--why={a['why']}"] if a.get("why") else []) + ["--", a["id"]],
      WRITE, required=("id", "action"))


# --------------------------------------------------------------------------- arguments

def validate(name: str, args) -> dict:
    """Check and normalise a tool's arguments against its schema. Raises ValueError with a message for the model."""
    schema = next(t["inputSchema"] for t in TOOLS if t["name"] == name)
    props = schema["properties"]
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    out = {}
    for k, v in args.items():
        if k not in props:
            raise ValueError(f"unknown argument `{k}`; {name} takes " + (", ".join(props) or "no arguments"))
        if v is None:
            continue
        spec, t = props[k], props[k]["type"]
        if t == "string":
            if not isinstance(v, str):
                raise ValueError(f"`{k}` must be a string")
            v = " ".join(v.split())
            if any(ord(c) < 32 or ord(c) == 127 for c in v):
                raise ValueError(f"`{k}` contains control characters")
            if not v:
                continue
            if len(v) < spec.get("minLength", 0) or len(v) > spec.get("maxLength", 10 ** 6):
                raise ValueError(f"`{k}` must be {spec.get('minLength', 0)}–{spec.get('maxLength')} characters")
            if "enum" in spec and v not in spec["enum"]:
                raise ValueError(f"`{k}` must be one of: {', '.join(spec['enum'])}")
            pat = _PATTERNS.get("action_prefix" if (name, k) == ("devkit_audit", "action") else k)
            if pat and not pat.match(v):
                raise ValueError(f"`{k}` has characters a {k} cannot contain: {v!r}")
        elif t in ("integer", "number"):
            if isinstance(v, str):
                try:
                    v = float(v.strip()) if t == "number" or "." in v else int(v.strip())
                except ValueError:
                    raise ValueError(f"`{k}` must be a {t}") from None
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"`{k}` must be a {t}")
            if t == "integer":
                if v != int(v):
                    raise ValueError(f"`{k}` must be a whole number")
                v = int(v)
            if v < spec.get("minimum", v) or v > spec.get("maximum", v):
                raise ValueError(f"`{k}` must be between {spec.get('minimum')} and {spec.get('maximum')}")
        elif t == "boolean":
            if isinstance(v, str) and v.lower() in ("true", "false"):
                v = v.lower() == "true"
            if not isinstance(v, bool):
                raise ValueError(f"`{k}` must be true or false")
        out[k] = v
    for k in schema.get("required", []):
        if k not in out:
            raise ValueError(f"`{k}` is required")
    for k, spec in props.items():
        if k not in out and "default" in spec:
            out[k] = spec["default"]
    return out


def build_argv(name: str, args: dict) -> list:
    """The `dev` arguments for a tool call (validated args). Raises ValueError."""
    return _HANDLERS[name][0](args)


# --------------------------------------------------------------------------- running dev

class Call:
    """One running tool call: its process, so a cancellation or shutdown can stop it."""

    def __init__(self):
        self.proc = None
        self.cancelled = False

    def stop(self):
        self.cancelled = True
        _kill(self.proc)


def _kill(proc):
    if proc is None or proc.poll() is not None:
        return
    for sig, wait in ((signal.SIGTERM, 3), (signal.SIGKILL, 3)):
        try:
            os.killpg(proc.pid, sig)  # the command's own process group (start_new_session)
        except OSError:
            pass
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def run_dev(argv: list, timeout: float = DEFAULT_TIMEOUT, call: Call = None, progress=None, cwd=None):
    """Run `dev <argv>` -> (exit code or None on timeout, stdout, stderr). Output goes to temporary files, so a
    background process the command starts can never hold the result back."""
    env = dict(os.environ, NO_COLOR="1", TERM="dumb", COLUMNS="120", PYTHONIOENCODING="utf-8", DEVKIT_AGENT=AGENT)
    for k in ("FORCE_COLOR", "CLICOLOR_FORCE"):
        env.pop(k, None)
    call = call or Call()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            call.proc = subprocess.Popen([sys.executable, str(DEV_BIN)] + [str(x) for x in argv], cwd=cwd or _cwd(),
                                         stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env,
                                         start_new_session=True)
        except OSError as e:
            return 127, "", str(e)
        start, rc = time.monotonic(), None
        while rc is None and not call.cancelled:
            left = timeout - (time.monotonic() - start)
            if left <= 0:
                break
            try:
                rc = call.proc.wait(timeout=min(PROGRESS_EVERY, left))
            except subprocess.TimeoutExpired:
                if progress:
                    progress(round(time.monotonic() - start, 1))
        if rc is None:  # timed out, or cancelled (possibly before the process started)
            _kill(call.proc)
        out.seek(0)
        err.seek(0)
        return rc, out.read().decode("utf-8", "replace"), err.read().decode("utf-8", "replace")


def _cwd() -> str:
    try:
        return os.getcwd()
    except OSError:
        return str(core.HOME)


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_ART = set("█▄▀▐▌▗▖▝▘▓▒░╗╔═║╝╚╲╱◉")


def clean(text: str) -> str:
    """Terminal output -> compact text: no colour codes, banner art, section rules or table rules."""
    lines = []
    for ln in _ANSI.sub("", text or "").splitlines():
        s = ln.rstrip()
        if s and not re.search(r"[A-Za-z0-9]", s) and any(c in _ART for c in s):
            continue
        s = re.sub(r"\s*─{3,}$", "", s)
        if s.strip() and set(s.strip()) <= set("─ "):
            continue
        lines.append(s)
    return cap(re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n"))


def cap(text: str, n: int = MAX_TEXT) -> str:
    if len(text) <= n:
        return text
    return text[:n] + f"\n… [{len(text) - n} more characters cut; narrow the request]"


def _prune(x):
    """Drop empty values (None, '', [], {}) so JSON results stay small."""
    if isinstance(x, dict):
        x = {k: _prune(v) for k, v in x.items()}
        return {k: v for k, v in x.items() if v not in (None, "", [], {})}
    if isinstance(x, list):
        return [_prune(v) for v in x]
    return x


def compact_json(x) -> str:
    return json.dumps(x, ensure_ascii=False, separators=(",", ":"))


def call_tool(name: str, args, call: Call = None, progress=None, structured: bool = True) -> dict:
    """Run a tool -> an MCP CallToolResult. Unknown tools raise KeyError (a protocol error); everything else, bad
    arguments included, comes back as a result with isError so the model can correct itself."""
    if name not in _HANDLERS:
        raise KeyError(name)
    _, timeout, json_out, digest, report_on_exit1 = _HANDLERS[name]
    try:
        a = validate(name, args)
        argv = build_argv(name, a)
    except ValueError as e:
        return _result(f"Invalid arguments for {name}: {e}", error=True)
    t0 = time.monotonic()
    rc, out, err = run_dev(argv, timeout, call, progress)
    _log(f"{name} → dev {argv[0]} · {'timeout' if rc is None else f'exit {rc}'} · {time.monotonic() - t0:.1f}s")
    if rc is None:
        why = "was cancelled" if call and call.cancelled else f"did not finish within {timeout} s"
        return _result(f"`dev {argv[0]}` {why}." + (f"\n{clean(err)[-1500:]}" if err.strip() else ""), error=True)
    if rc == 1 and report_on_exit1 and out.strip():  # e.g. doctor: some checks failed, and the report says which
        return _result(f"`dev {argv[0]}` found problems (exit 1). The full report:\n\n" + clean(out))
    if rc != 0:
        msg = clean(err)[-2000:] or clean(out)[-2000:] or f"`dev {argv[0]}` failed (exit {rc})"
        if re.search(r"not inside a (project|repository)", msg):
            msg += "\nPass `project` (an id such as owner/repo; devkit_overview lists them)."
        return _result(msg, error=True)
    if json_out:
        try:
            data = json.loads(out)
        except ValueError:
            data = None
        if data is not None:
            data = _prune(digest(data, a) if digest else data)
            if not isinstance(data, dict):
                data = {"items": data}
            return _result(cap(compact_json(data)), data if structured else None)
    return _result(clean(out) or "(no output)")


def _result(text: str, structured=None, error: bool = False) -> dict:
    r = {"content": [{"type": "text", "text": text}]}
    if structured is not None:
        r["structuredContent"] = structured
    if error:
        r["isError"] = True
    return r


def _log(msg: str):
    try:
        sys.stderr.write(f"[devkit-mcp {time.strftime('%H:%M:%S')}] {msg}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


# --------------------------------------------------------------------------- JSON-RPC over stdio

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


class Server:
    def __init__(self, rfile, wfile):
        self.rfile, self.wfile = rfile, wfile
        self.lock = threading.Lock()
        self.version = PROTOCOL_VERSIONS[0]
        self.calls = {}      # request id (JSON) -> Call
        self.threads = []
        self.closed = False

    def send(self, msg: dict):
        data = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        with self.lock:
            if self.closed:
                return
            try:
                self.wfile.write(data)
                self.wfile.flush()
            except (BrokenPipeError, OSError, ValueError):
                self.closed = True

    def serve(self) -> int:
        while not self.closed:
            line = self.rfile.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8") if isinstance(line, bytes) else line)
            except ValueError:
                self.send(_error(None, PARSE_ERROR, "Parse error: each line must be one JSON-RPC message"))
                continue
            if isinstance(msg, list):  # a JSON-RPC batch (protocol 2025-03-26): answered together, in order
                if not msg:
                    self.send(_error(None, INVALID_REQUEST, "Invalid Request: empty batch"))
                    continue
                replies = [r for r in (self.handle(m, inline=True) for m in msg) if r is not None]
                if replies:
                    self.send(replies)
                continue
            reply = self.handle(msg)
            if reply is not None:
                self.send(reply)
        self.shutdown()
        return 0

    def shutdown(self, kill: bool = False):
        """The client closed stdin: finish the calls in flight (each has its own timeout) and answer them, unless
        `kill`. A client that will not wait sends SIGTERM, which stops them."""
        for c in list(self.calls.values()) if kill else []:
            c.stop()
        for t in self.threads:
            t.join(timeout=5 if kill else None)

    def handle(self, msg, inline: bool = False):
        """One message -> the reply, or None (notifications, responses, and tool calls answered by their thread)."""
        if not isinstance(msg, dict):
            return _error(None, INVALID_REQUEST, "Invalid Request: a message must be a JSON object")
        if "method" not in msg and ("result" in msg or "error" in msg):
            return None  # a response; this server sends no requests
        mid, method = msg.get("id"), msg.get("method")
        if msg.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return _error(mid if isinstance(mid, (str, int)) else None, INVALID_REQUEST,
                          "Invalid Request: expected jsonrpc 2.0 and a method")
        params = msg.get("params") or {}
        if "id" not in msg:
            self.notification(method, params)
            return None
        if isinstance(mid, bool) or not isinstance(mid, (str, int)):
            return _error(None, INVALID_REQUEST, "Invalid Request: id must be a string or an integer")
        if not isinstance(params, dict):
            return _error(mid, INVALID_PARAMS, "params must be an object")
        try:
            if method == "initialize":
                asked = params.get("protocolVersion")
                self.version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                return {"jsonrpc": "2.0", "id": mid, "result": {
                    "protocolVersion": self.version, "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "devkit", "version": core.VERSION}, "instructions": INSTRUCTIONS}}
            if method == "ping":
                return {"jsonrpc": "2.0", "id": mid, "result": {}}
            if method == "tools/list":
                return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
            if method == "tools/call":
                name = params.get("name")
                if not isinstance(name, str) or name not in _HANDLERS:
                    return _error(mid, INVALID_PARAMS, f"Unknown tool: {name}")
                if inline:
                    return self.run_call(mid, params)
                t = threading.Thread(target=self.answer_call, args=(mid, params), daemon=True)
                self.threads = [x for x in self.threads if x.is_alive()] + [t]
                t.start()
                return None
            return _error(mid, METHOD_NOT_FOUND, f"Method not found: {method}")
        except Exception as e:  # never let one message stop the server
            _log(f"internal error on {method}: {e!r}")
            return _error(mid, INTERNAL_ERROR, f"Internal error: {e}")

    def notification(self, method: str, params):
        if method == "notifications/cancelled" and isinstance(params, dict):
            c = self.calls.get(json.dumps(params.get("requestId")))
            if c:
                c.stop()

    def run_call(self, mid, params) -> dict:
        key = json.dumps(mid)
        call = self.calls[key] = Call()
        token = (params.get("_meta") or {}).get("progressToken") if isinstance(params.get("_meta"), dict) else None

        def progress(seconds):
            if token is not None and not call.cancelled:
                self.send({"jsonrpc": "2.0", "method": "notifications/progress",
                           "params": {"progressToken": token, "progress": seconds,
                                      "message": f"{params.get('name')} still running ({seconds:.0f} s)"}})
        try:
            result = call_tool(params["name"], params.get("arguments"), call, progress,
                               structured=self.version >= "2025-06-18")
            return None if call.cancelled else {"jsonrpc": "2.0", "id": mid, "result": result}
        except Exception as e:
            _log(f"internal error in {params.get('name')}: {e!r}")
            return _error(mid, INTERNAL_ERROR, f"Internal error: {e}")
        finally:
            self.calls.pop(key, None)

    def answer_call(self, mid, params):
        reply = self.run_call(mid, params)
        if reply is not None:  # a cancelled request gets no response
            self.send(reply)


def serve() -> int:
    """`dev mcp`: serve on stdin/stdout until the client closes stdin."""
    rfile, wfile = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr  # a stray print must never corrupt the protocol stream
    server = Server(rfile, wfile)

    def on_term(*_):
        for c in list(server.calls.values()):
            c.stop()
        os._exit(0)
    try:
        signal.signal(signal.SIGTERM, on_term)
    except (ValueError, OSError):
        pass
    _log(f"devkit {core.VERSION} MCP server on stdio · {len(TOOLS)} tools · {core.tilde(core.DEVKIT_HOME)}")
    try:
        return server.serve()
    except KeyboardInterrupt:
        server.shutdown(kill=True)
        return 130
