# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The crew gates: what a crew member owes before it stops (SubagentStop).

The main session's Stop hook checks a turn for unfinished work, tests that cannot fail and missing skills. These
gates hold a crew member to the same standard and to its own procedure (config/crew.json). They read the member's
own transcript (`agent_transcript_path` in the SubagentStop input), never the main session's:

  proofs   the `proof` of each procedure phase that applied ran as a shell command, not as text inside quotes or a
           heredoc. A proof about files applies only when the member changed files of its kind (`FILE_PROOFS`: no
           `dev docs check` without a docs change, no test proof without a code change); the others always apply
  skills   it loaded the skills of each procedure phase it worked in (a phase whose proof applied, or one whose
           devkit commands it ran) and the skills devkit's skill rules require for the files it changed: a Skill
           tool call, or a read of the skill's SKILL.md
  stubs    the files it changed leave no unfinished work and no untracked deferrals (the `stubs` module)
  tests    the test files it changed hold no test that cannot fail (`testaudit`, high findings)
The files it changed are those of its Write and Edit calls and, for what it wrote through its shell, the checkout's
worktree diff since its lane started; only its own lines are blamed on it (`_mine`).
  lessons  two or more guard refusals are answered with `dev lesson add` or `dev lesson recur`
  background  every command it put in the background has ended: Claude Code keeps a member in its agent list while
           one still runs, after its report. Ended means its task notification arrived, a TaskStop (or KillShell)
           call stopped it, or a TaskOutput (or BashOutput) read showed it exited (`background_work`). Codex
           transcripts have no such launch, so there it finds nothing

Read-only members (`tools: read`) have no shell, so they skip the proofs and lessons gates. `check` is the hook's
entry point: it sends a member back once with every missing item, or only records it when `crew.gates.enforce` is
false. Either way the audit trail gets a `crew.gate` record. A member whose lane closes with background work still
running is kept in `sess["crew_background"]` for the tech lead's next context (`hooks.crew_background_notice`).
Everything fails open.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import core, skillguard

RULES = ("proofs", "skills", "stubs", "tests", "lessons", "background")
LABELS = {"proofs": "proof not run", "skills": "skill not loaded", "stubs": "unfinished work in",
          "tests": "tests that cannot fail in", "lessons": "no lesson after repeated refusals:",
          "background": "still running in the background:"}
MAX_BYTES = 8_000_000  # the end of a very long member transcript is enough
SHELL_TOOLS = {"Bash", "exec_command", "shell", "local_shell", "unified_exec"}
# A hook's denial as Claude Code writes it into the tool result: "PreToolUse:Bash hook error: <the guard's reason>".
DENIAL_RE = re.compile(r"^\s*(?:PreToolUse|PermissionRequest)(?::[^\s:]+)? hook (?:blocking )?error\b", re.I)
_SPLIT = re.compile(r"\|\||&&|\$\(|[;&|()`\n]")  # where a new simple command starts
_LEAD = {"then", "do", "else", "elif", "if", "while", "until", "time", "exec", "command", "nohup", "env", "!", "{"}
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PYTHON = re.compile(r"^python(\d+(\.\d+)*)?$")  # `python3 bin/dev test` runs `dev test`
PROOF_ARGS = {"dev progress step": ' "<what finished>" --check "<evidence>"'}  # proofs that need arguments


def enforced() -> bool:
    """`crew.gates.enforce` (default true). False: the gates only record what they find."""
    return bool(((core.CFG.get("crew") or {}).get("gates") or {}).get("enforce", True))


# --------------------------------------------------------------------------- reading the member's transcript

def _records(path) -> list:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > MAX_BYTES:
                fh.seek(size - MAX_BYTES)
                fh.readline()
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except (OSError, TypeError, ValueError):
        return []
    out = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def _activity(path) -> tuple:
    """-> (shell commands, hook denials) of a member, in order."""
    commands, refusals = [], []
    for rec in _records(path):
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict):
                continue
            if rec.get("type") == "assistant" and c.get("type") == "tool_use" and c.get("name") in SHELL_TOOLS:
                cmd = (c.get("input") or {}).get("command") or (c.get("input") or {}).get("cmd") or ""
                commands.append(" ".join(map(str, cmd)) if isinstance(cmd, list) else str(cmd))
            elif rec.get("type") == "user" and c.get("type") == "tool_result":
                text = _text(c.get("content"))
                if DENIAL_RE.match(text):
                    refusals.append(text)
    return commands, refusals


# Claude Code's answer when a shell command goes to the background: `run_in_background`, the developer's ctrl+b, or
# moved aside so a message could be handled. Only an answer that starts with it is a launch (not output quoting it).
BG_LAUNCH_RE = re.compile(r"^\s*Command (?:running in background with ID|was manually backgrounded by user with ID|"
                          r"was moved to the background \(ID): ?([A-Za-z0-9_-]+)")
NOTIFICATION_RE = re.compile(r"<task-notification>(.*?)</task-notification>", re.S)
ENDED = {"completed", "failed", "killed", "stopped"}  # a task's status once it no longer runs
STOP_TOOLS = {"TaskStop", "KillShell", "KillBash"}  # KillShell and KillBash: TaskStop's older names
READ_TOOLS = {"TaskOutput", "BashOutput"}  # removed in Claude Code 2.1; older transcripts still hold them


def _tag(text: str, name: str) -> str:
    m = re.search(rf"<{name}>\s*([^<]*?)\s*</{name}>", text or "")
    return m.group(1) if m else ""


def _task_of(inp: dict) -> str:
    return str(inp.get("task_id") or inp.get("shell_id") or inp.get("bash_id") or "")


def _notifications(rec: dict) -> list:
    """The <task-notification> texts of one record: a queued_command attachment, or a user message (some Claude Code
    versions write it there); a task_status attachment as the same shape."""
    if rec.get("type") == "attachment":
        att = rec.get("attachment") or {}
        if att.get("type") == "task_status" and att.get("taskId"):
            return [f"<task-id>{att['taskId']}</task-id><status>{att.get('status') or ''}</status>"]
        return NOTIFICATION_RE.findall(str(att.get("prompt") or att.get("content") or ""))
    if rec.get("type") == "user":
        return NOTIFICATION_RE.findall(_text((rec.get("message") or {}).get("content")))
    return []


def background_work(path) -> list:
    """The shell commands this member put in the background that have not ended, in launch order:
    [{"id": task id, "command": the command}]. Ended: its task notification arrived with a final status, a stop call
    on its id was not refused by a hook, or a read of its output shows a final status or an exit code."""
    shells, stops, reads = {}, {}, {}  # tool_use id -> command | task id
    launched, ended, ended_calls = [], set(), set()
    for rec in _records(path):
        for note in _notifications(rec):
            if _tag(note, "status").lower() in ENDED:
                ended.add(_tag(note, "task-id"))
                ended_calls.add(_tag(note, "tool-use-id"))
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict):
                continue
            if rec.get("type") == "assistant" and c.get("type") == "tool_use":
                inp, name = c.get("input") or {}, c.get("name")
                if name in SHELL_TOOLS:
                    cmd = inp.get("command") or inp.get("cmd") or ""
                    shells[c.get("id")] = " ".join(map(str, cmd)) if isinstance(cmd, list) else str(cmd)
                elif name in STOP_TOOLS and _task_of(inp):
                    stops[c.get("id")] = _task_of(inp)
                elif name in READ_TOOLS and _task_of(inp):
                    reads[c.get("id")] = _task_of(inp)
            elif rec.get("type") == "user" and c.get("type") == "tool_result":
                use, text = c.get("tool_use_id"), _text(c.get("content"))
                result = rec.get("toolUseResult") if isinstance(rec.get("toolUseResult"), dict) else {}
                if use in shells:
                    m = BG_LAUNCH_RE.match(text)
                    tid = m.group(1) if m else str(result.get("backgroundTaskId") or "")
                    if tid:
                        launched.append((tid, use, shells[use]))
                elif use in stops and not (c.get("is_error") and DENIAL_RE.match(text)):
                    ended.add(stops[use])  # stopped, or "not running" / "no task found": it no longer runs
                elif use in reads:
                    status = (_tag(text, "status") or str(result.get("status") or "")).lower()
                    if status in ENDED or re.search(r"<exit_code>\s*-?\d+\s*</exit_code>", text):
                        ended.add(reads[use])
    out = []
    for tid, use, cmd in launched:
        if tid not in ended and use not in ended_calls and all(tid != o["id"] for o in out):
            line = (" ".join(cmd.split()) or "a command").replace("`", "'")
            out.append({"id": tid, "command": line if len(line) <= 120 else line[:119] + "…"})
    return out


def _invocations(commands) -> list:
    """Every simple command the shell ran, as words; the program is its base name (`bin/dev` -> `dev`). Quoted
    strings and heredoc bodies are dropped first (`hooks.command_words`): they are text, not commands."""
    from .hooks import command_words
    out = []
    for cmd in commands:
        for part in _SPLIT.split(command_words(cmd)):
            words = part.split()
            while words and (words[0] in _LEAD or _ASSIGN.match(words[0])):
                words.pop(0)
            if words and _PYTHON.match(os.path.basename(words[0])):  # the script is the program: skip the flags
                rest = words[1:]
                while rest and rest[0].startswith("-") and rest[0] not in ("-m", "-c"):
                    rest.pop(0)
                if rest and not rest[0].startswith("-"):
                    words = rest
            if words:
                out.append([os.path.basename(words[0])] + words[1:])
    return out


def _written(path) -> dict:
    """{real path: (whole file?, [the text this member wrote there])} from its own Write, Edit, MultiEdit and
    NotebookEdit calls: what the stub and test gates may blame on it."""
    out = {}
    for rec in _records(path):
        if rec.get("type") != "assistant":
            continue
        for c in (rec.get("message") or {}).get("content") or []:
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            inp = c.get("input") or {}
            fp = inp.get("file_path") or inp.get("notebook_path") or ""
            if not fp:
                continue
            whole, texts = out.get(os.path.realpath(fp), (False, []))
            if c.get("name") == "Write":
                whole, texts = True, texts + [str(inp.get("content") or "")]
            elif c.get("name") == "Edit":
                texts = texts + [str(inp.get("new_string") or "")]
            elif c.get("name") == "MultiEdit":
                texts = texts + [str(e.get("new_string") or "") for e in inp.get("edits") or [] if isinstance(e, dict)]
            elif c.get("name") == "NotebookEdit":
                texts = texts + [str(inp.get("new_source") or "")]
            else:
                continue
            out[os.path.realpath(fp)] = (whole, texts)
    return out


def _lane_start(path):
    """When the member's lane started: the `timestamp` of the first record of its transcript (Claude Code writes UTC
    with milliseconds). None when the transcript has none."""
    from datetime import datetime
    for rec in _records(path):
        ts = rec.get("timestamp")
        if isinstance(ts, str) and ts:
            try:
                return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
    return None


def _shell_written(top, path, commands, known) -> dict:
    """What the member may have written through its shell: the paths of the checkout's worktree diff
    (`guard.worktree_state`) changed since its lane started, other than `known` (its Write and Edit files), as
    {real path: (whole file?, [its shell commands])}. Whole when the file is new (untracked) and a command names its
    path; otherwise only lines its commands hold are its (a heredoc's body, a sed expression). {} without a start."""
    start = _lane_start(path) if commands else None
    if start is None:
        return {}
    from . import guard
    out = {}
    for rel, (status, _, mtime_ns) in (guard.worktree_state(top) or {}).items():
        real = os.path.realpath(os.path.join(top, rel))
        if mtime_ns is None or mtime_ns < (start - 1) * 1e9 or real in known or not os.path.isfile(real):
            continue
        out[real] = (status == "??" and _named(top, real, commands), list(commands))
    return out


def _named(top, real, commands) -> bool:
    """A shell command names this file: its repository path as a word (`> docs/a.md`, `sed -i … ./a.py`), or its
    full path."""
    rel = os.path.relpath(real, os.path.realpath(top))
    word = re.compile(r"(?:^|[\s'\"=>(])(?:\./)?" + re.escape(rel) + r"(?![\w./-])|" + re.escape(real))
    return any(word.search(c) for c in commands)


def _mine(written, top, rel, *needles) -> bool:
    """Did this member write it: the whole file, or a text it wrote holds one of the needles (the finding's line, or
    its test's name)? Another member's uncommitted lines in a shared file are theirs."""
    whole, texts = written.get(os.path.realpath(os.path.join(top, rel)), (False, []))
    if whole:
        return True
    needles = [n.strip() for n in needles if n and n.strip()]
    return any(n in t for n in needles for t in texts)


DOC_SUFFIXES = (".md", ".mdx", ".rst", ".adoc", ".txt")


def _is_doc(rel: str) -> bool:
    p = rel.replace(os.sep, "/").lower()
    return p.endswith(DOC_SUFFIXES) or p.split("/")[0] in ("docs", "doc")


def _is_test(rel: str) -> bool:
    from . import testaudit
    return testaudit.is_test_file(rel)


# A proof about files is owed only when the member changed files of its kind (crew review 2, R2-6): no docs check
# for a step that changed no docs, no test proof for a step that changed no code. Other proofs are always owed.
FILE_PROOFS = {"dev docs check": _is_doc, "dev test": lambda f: not _is_doc(f),
               "dev test prove": lambda f: not _is_doc(f), "dev test audit": _is_test}


def _proof_owed(phase: dict, changed) -> bool:
    proof = phase.get("proof")
    kind = FILE_PROOFS.get(" ".join(str(proof or "").split()))
    return bool(proof) and (kind is None or any(kind(f) for f in changed))


def _phase_applied(phase: dict, invocations, changed) -> bool:
    """The member worked in this phase: its proof is owed, or it ran one of the phase's devkit commands."""
    return _proof_owed(phase, changed) or any(_ran(c, invocations) for c in phase.get("devkit") or [])


def _ran(command: str, invocations) -> bool:
    want = command.split()
    if not want:
        return False
    want[0] = os.path.basename(want[0])
    return any(w[:len(want)] == want for w in invocations)


# --------------------------------------------------------------------------- the gates

def _member(member, roster):
    if isinstance(member, dict):
        return member
    from . import crew
    try:
        return crew.member(member, roster) or crew._for_label(member, roster)
    except Exception:
        return None


def _finding(rule, item, evidence, fix) -> dict:
    return {"rule": rule, "item": item, "evidence": evidence, "fix": fix}


def _rel(path, top) -> str:
    try:
        return os.path.relpath(os.path.realpath(path), top) if top and core.is_under(path, top) else core.tilde(path)
    except ValueError:
        return str(path)


def _skill_findings(m, invocations, loaded, changed, top, kinds=()) -> list:
    """`changed`: the paths the skill rules match; `kinds`: the changed paths as the phases see them (repository
    paths), which decide the phases the member worked in."""
    need = {}  # short name -> (name as written, [why])
    for x in (m or {}).get("procedure") or []:
        if not isinstance(x, dict) or not _phase_applied(x, invocations, kinds):
            continue
        for s in x.get("skills") or []:
            need.setdefault(skillguard.short(s), (s, []))[1].append(f"the {x.get('phase')} phase of your procedure")
    for rule, matched in skillguard.rules_for_files(changed):
        names = ", ".join(_rel(f, top) for f in matched[:3]) + (" …" if len(matched) > 3 else "")
        for s in skillguard.required(rule, "claude"):
            need.setdefault(s, (s, []))[1].append(f"your change to {names} ({rule.get('why') or rule.get('id')})")
    have = skillguard.installed("claude")
    out = []
    for key, (name, why) in need.items():
        if key in loaded or key not in have:  # loaded, or not on this Mac: nothing the member can do
            continue
        path = skillguard.skill_path(name)
        fix = f'the Skill tool with skill "{name}"' + (f", or Read {core.tilde(path)}" if path else "")
        out.append(_finding("skills", name, "never loaded, but " + " and ".join(why) + " need it", fix))
    return out


def _stub_findings(top, files, written=None) -> list:
    from . import stubs
    found = stubs.scan_changes(top, [os.path.relpath(f, top) for f in files])
    if written is not None:
        found = [f for f in found if _mine(written, top, f["file"], f.get("text") or "")]
    if not found:
        return []
    where = sorted({f["file"] for f in found})
    lines = "; ".join(f"{f['file']}:{f['line']} {f['rule']}: {f['text'].strip()[:80]}" for f in found[:5])
    more = f" (and {len(found) - 5} more)" if len(found) > 5 else ""
    return [_finding("stubs", ", ".join(where), lines + more,
                     'build it for real; if deferring it is the plan, run `dev task add "<the deferred work>" --why '
                     '"<why>"` and name the task on the line; then check with `dev stubs ' + " ".join(where) + "`")]


def _line(top, rel, n) -> str:
    try:
        return (Path(rel if os.path.isabs(rel) else os.path.join(top, rel)).read_text(errors="ignore")
                .splitlines() or [""])[max(0, int(n or 1) - 1)]
    except (OSError, IndexError, ValueError):
        return ""


def _test_findings(top, files, written=None) -> list:
    from . import testaudit
    tests = [f for f in files if testaudit.is_test_file(os.path.relpath(f, top))]
    high = [f for f in testaudit.audit(tests, top=top) if f.get("severity") == "high"] if tests else []
    if written is not None:
        high = [f for f in high if _mine(written, top, os.path.relpath(f["file"], top) if os.path.isabs(f["file"])
                                         else f["file"], f.get("test") or "", _line(top, f["file"], f.get("line")))]
    if not high:
        return []
    where = sorted({_rel(f["file"], top) if os.path.isabs(f["file"]) else f["file"] for f in high})
    lines = "; ".join(f"{f['file']}:{f['line']} {f.get('test') or ''} {f['rule']}".replace("  ", " ")
                      for f in high[:5])
    more = f" (and {len(high) - 5} more)" if len(high) > 5 else ""
    return [_finding("tests", ", ".join(where), lines + more,
                     "make each test able to fail for a real bug, or delete it; then check with `dev test audit "
                     + " ".join(where) + "`")]


def crew_gates(member, transcript_path, cwd, roster=None) -> list:
    """What a crew member still owes, from its own transcript: [{rule, item, evidence, fix}], in the order of
    RULES. `member` is a roster entry or a name (`Kid`, `DevEx engineer (Kid)`, `devkit:kid`); an unknown name
    still gets the gates that need no procedure."""
    m = _member(member, roster)
    read_only = (m or {}).get("tools") == "read"
    commands, refusals = _activity(transcript_path)
    inv = _invocations(commands)
    loaded, edited, _ = skillguard.scan("claude", transcript_path)
    paths = [str(Path(f) if os.path.isabs(f) else Path(cwd or ".") / f) for f in dict.fromkeys(edited) if f]
    top = core.gitout(cwd, "rev-parse", "--show-toplevel") if cwd and os.path.isdir(cwd) else ""
    # real paths: git names the top as /private/var/… where the member wrote /var/… (macOS temp folders)
    in_repo = [os.path.realpath(f) for f in paths if top and os.path.isfile(f) and core.is_under(f, top)]
    shell = _shell_written(top, transcript_path, commands, set(in_repo)) if top and not read_only else {}
    touched = [r for r in shell if _named(top, r, commands)]  # shell writes it named: the phases count them
    kinds = [_rel(f, top) for f in dict.fromkeys(in_repo + touched)] + [f for f in paths if f not in in_repo
                                                                         and not (top and core.is_under(f, top))]
    found = []
    if not read_only:
        for x in (m or {}).get("procedure") or []:
            proof = x.get("proof") if isinstance(x, dict) else None
            if proof and _proof_owed(x, kinds) and not _ran(proof, inv):
                found.append(_finding("proofs", proof, f"`{proof}` is not among your {len(commands)} shell commands "
                                      "(text inside quotes or a heredoc does not count)",
                                      f"run `{proof}{PROOF_ARGS.get(proof, '')}`"))
    found += _skill_findings(m, inv, loaded, paths + touched, top, kinds)
    if in_repo or shell:
        written = {**_written(transcript_path), **shell}
        changed = in_repo + sorted(shell)
        found += _stub_findings(top, changed, written)
        found += _test_findings(top, changed, written)
    if not read_only and len(refusals) >= 2 and not (_ran("dev lesson add", inv) or _ran("dev lesson recur", inv)):
        first = (re.sub(r"^[:\s]+", "", DENIAL_RE.sub("", refusals[0], 1)).splitlines() or [""])[0][:120]
        found.append(_finding("lessons", "dev lesson add",
                              f"{len(refusals)} guard refusals and no dev lesson add or dev lesson recur; the first: "
                              f"\"{first}\"",
                              'run `dev lesson add "<what the guard refused>" --cause "<why it happened>" --prevent '
                              '"<the rule for next time>"`, or `dev lesson recur <id> --evidence "<what happened>"` '
                              'for a known pitfall'))
    for t in background_work(transcript_path):
        found.append(dict(_finding("background", t["command"], "you started it, and nothing in your transcript shows "
                                   "that it ended (no task notification, TaskStop or exit)",
                                   f"do not end your turn to wait (that ends your report): keep working, or run it in the "
                                   f"foreground, until it ends; or stop it with TaskStop `{t['id']}`; then report again"), task=t["id"]))
    return found


def send_back_reason(findings, who) -> str:
    """One message for the hook's `decision: block`, naming each missing item and the command that fixes it."""
    if not findings:
        return ""
    lines = [f"devkit · {who}, the crew gates found {len(findings)} thing{'s' if len(findings) != 1 else ''} "
             f"missing before you stop:"]
    for i, f in enumerate(findings, 1):
        task = f" (task `{f['task']}`)" if f.get("task") else ""
        lines.append(f"{i}. {LABELS.get(f['rule'], f['rule'])} `{f['item']}`{task}: {f['evidence']}. "
                     f"Fix: {f['fix']}.")
    lines.append("Fix each one, then end with the same report, status block first. devkit sends you back only once.")
    return "\n".join(lines)


def _keep_for_lead(sess: dict, key: str, who: str, tasks: list):
    """The lane closes while this member's background work runs: keep it for the tech lead's next context, once
    (`hooks.crew_background_notice`). `key` is the agent id devkit records for the lane, which TaskStop takes."""
    sess.setdefault("crew_background", {})[key] = {"who": who, "tasks": [{"id": t["id"], "command": t["command"]}
                                                                         for t in tasks]}


def check(name, data: dict, sess: dict, pid: str = "", roster=None):
    """The SubagentStop entry point. -> the hook's {"decision": "block", "reason": …} the first time a member stops
    with something missing, else None. Records each finding set as `crew.gate` in the audit trail ("blocked", or
    "recorded" when `crew.gates.enforce` is false) and marks the member in `sess["crew_gated"]`, so it is sent back
    only once. A member let through with background work still running (sent back once already, or not enforced) is
    audited and kept for the tech lead (`_keep_for_lead`). Fails open."""
    try:
        key = data.get("agent_id") or str(name or "")
        transcript = data.get("agent_transcript_path")
        if not transcript:
            return None
        from . import audit, crew
        if key in (sess.get("crew_gated") or {}):  # sent back once: let through, but not silently
            tasks = background_work(transcript)
            if tasks:
                who = sess["crew_gated"][key]
                audit.record("crew.gate", pid or "", f"{who}: background: " + "; ".join(
                    f"{t['command']} (task {t['id']})" for t in tasks) + "; sent back once already",
                             "let through", member=who, rules=["background"])
                _keep_for_lead(sess, key, who, tasks)
            return None
        m = _member(name, roster)
        findings = crew_gates(m or name, transcript, data.get("cwd") or "", roster)
        if not findings:
            return None
        who = crew.who(m) if m else str(name or "Agent")
        sess.setdefault("crew_gated", {})[key] = who
        enforce = enforced()
        audit.record("crew.gate", pid or "", f"{who}: " + "; ".join(f"{f['rule']}: {f['item']}" for f in findings),
                     "blocked" if enforce else "recorded", member=who, rules=sorted({f["rule"] for f in findings}))
        if enforce:
            return {"decision": "block", "reason": send_back_reason(findings, who)}
        running = [{"id": f["task"], "command": f["item"]} for f in findings if f["rule"] == "background"]
        if running:
            _keep_for_lead(sess, key, who, running)
        return None
    except Exception:
        return None
