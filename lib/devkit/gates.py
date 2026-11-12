# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The crew gates: what a crew member owes before it stops (SubagentStop).

The main session's Stop hook checks a turn for unfinished work, tests that cannot fail and missing skills. These
gates hold a crew member to the same standard and to its own procedure (config/crew.json). They read the member's
own transcript (`agent_transcript_path` in the SubagentStop input), never the main session's, and judge it by its own
agent (`agent_of`: a Claude Code transcript or a Codex rollout), whose commands and installed skills they read:

  proofs   the `proof` of each procedure phase that applied ran as a shell command, not as text inside quotes or a
           heredoc. A proof about files applies only when the member changed files of its kind (`FILE_PROOFS`: no
           `dev docs check` without a docs change, no test proof without a code change); the others always apply.
           `dev ci --run`, which a brief or the project may forbid, can be met by the tech lead instead
           (`_ci_instead`): a `Waived proof:` line in the brief, or a passed `dev ci --run` on exactly the tree the
           member leaves, never the member's own `dev ci --mark-passed`; `check` audits each one it accepts
  skills   it loaded the skills of each procedure phase it worked in (a phase whose proof applied, or one whose
           devkit commands it ran) and the skills devkit's skill rules require for the files it changed: a Skill
           tool call, or a read of the skill's SKILL.md
  stubs    the files it changed leave no unfinished work and no untracked deferrals (the `stubs` module)
  tests    the test files it changed hold no test that cannot fail (`testaudit`, high findings)
The files it changed are those of its Write and Edit calls and, for what it wrote through its shell, the files of the
checkout's worktree diff since its lane started that one of its commands writes (`_shell_write`: a redirection into
the file, or a command naming it that the guard's read-only allowlist does not hold); a command that only reads a
file (cat, grep, sed -n, git diff) does not make it the member's, and neither does a file another caller edited with
its own edit tool during the lane (the session's edit record, progress.edits_since), unless a redirection of the
member wrote into it. Only its own lines are blamed on it (`_mine`).
  lessons  two or more guard refusals are answered with a lesson that `dev lesson add` recorded or `dev lesson recur`
           counted (its output shows it: a refused add answers nothing, `_lessons_done`), or with devkit's bug
           registry: a `dev bug report --guard …` from the lane, or a vote on the bug a refusal's marker names. A
           refusal the registry served a workaround for in this session (the session ledger, by the marker's
           fingerprint and code), or that an open lesson covers (it names the lesson, or shows its error signature),
           owes nothing and does not count (`_owed`)
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
import shlex
from pathlib import Path

from . import core, skillguard
from .hooks import SHELL_TOOLS  # Bash, Monitor and Codex's shells

RULES = ("proofs", "skills", "stubs", "tests", "lessons", "background")
LABELS = {"proofs": "proof not run", "skills": "skill not loaded", "stubs": "unfinished work in",
          "tests": "tests that cannot fail in", "lessons": "no lesson after repeated refusals:",
          "background": "still running in the background:"}
MAX_BYTES = 8_000_000  # the end of a very long member transcript is enough
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


CODEX_RECORDS = {"session_meta", "response_item", "event_msg", "turn_context"}  # the record types of a Codex rollout


def agent_of(path) -> str:
    """The agent that wrote a member's transcript: "codex" for a Codex rollout, else "claude"."""
    for rec in _records(path)[:20]:
        if rec.get("type") in CODEX_RECORDS and isinstance(rec.get("payload"), dict):
            return "codex"
    return "claude"


def _activity(path, agent: str = "claude") -> tuple:
    """-> (shell commands, hook denials) of a member, in order. A Codex member's denials are not in its rollout."""
    if agent == "codex":
        return skillguard.read("codex", path)[2], []
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


def _uv_command(words) -> list:
    """Unwrap an unambiguous uv run invocation; unknown options keep their original command identity."""
    if len(words) < 3 or os.path.basename(words[0]) != "uv" or words[1] != "run":
        return words
    at = 2
    flags = {"--no-project", "--offline", "--no-sync", "--locked", "--frozen", "--no-python-downloads"}
    while at < len(words):
        word = words[at]
        if word == "--":
            at += 1
            break
        if word in flags:
            at += 1
        elif word in ("--python", "-p"):
            if at + 1 >= len(words) or not words[at + 1] or words[at + 1].startswith("-"):
                return words
            at += 2
        elif word.startswith("--python=") and word[len("--python="):]:
            at += 1
        elif word.startswith("-"):
            return words
        else:
            break
    return words[at:] if at < len(words) else words


def _invocations(commands, agent: str = "claude") -> list:
    """Every simple command the shell ran, as words; the program is its base name (`bin/dev` -> `dev`). Quoted
    strings and heredoc bodies are dropped first (`hooks.command_words`): they are text, not commands."""
    from .hooks import command_words
    out = []
    for cmd in commands:
        for part in _SPLIT.split(command_words(cmd)):
            words = part.split()
            while words and (words[0] in _LEAD or _ASSIGN.match(words[0])):
                words.pop(0)
            if agent == "codex":
                words = _uv_command(words)
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


def _codex_written(path, cwd) -> dict:
    """Completed native file changes from this member, never inherited history or another thread's events.
    FileChange carries whole added files and the added lines of updates, so sibling edits remain unattributed."""
    from .codexlaunch import session_meta
    records = _records(path)
    meta = session_meta(path)
    thread = meta.get("id")
    if not isinstance(thread, str) or not thread:
        return {}
    forked = bool(meta.get("forked_from_id"))
    boundary = meta.get("subagent_history_start_ordinal")
    if forked and (not isinstance(boundary, int) or isinstance(boundary, bool) or boundary < 0):
        return {}
    truncated = not records or records[0].get("type") != "session_meta" or records[0].get("payload") != meta
    # The bounded tail may start after the child's task_started. Its native ordinal still proves the event is
    # beyond the fork boundary from the separately read header; a full transcript must also show task_started.
    start = 0 if truncated else skillguard.fork_start([json.dumps(rec) for rec in records])
    out = {}
    for rec in records[start:]:
        ordinal = rec.get("ordinal")
        if forked and (not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < boundary):
            continue
        payload = rec.get("payload") if rec.get("type") == "event_msg" else None
        if not isinstance(payload, dict) or payload.get("type") != "item_completed" or \
                payload.get("thread_id") != thread:
            continue
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "FileChange" or item.get("status") != "completed":
            continue
        changes = item.get("changes")
        for fp, change in (changes.items() if isinstance(changes, dict) else []):
            if not isinstance(change, dict) or change.get("type") not in ("add", "update", "delete"):
                continue
            target = change.get("move_path") or fp
            if not isinstance(target, str) or not target:
                continue
            real = os.path.realpath(os.path.join(cwd or ".", target))
            whole, texts = out.get(real, (False, []))
            if change["type"] == "add":
                text = change.get("content")
                if not isinstance(text, str):
                    continue
                whole = True
            else:
                diff = change.get("unified_diff") or ""
                if not isinstance(diff, str):
                    continue
                text = "\n".join(line[1:] for line in diff.splitlines()
                                 if line.startswith("+") and not line.startswith("+++ "))
            out[real] = (whole, texts + [text])
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


def _shell_written(top, path, commands, known, session: str = "", lane: str = "") -> dict:
    """What the member wrote through its shell: the paths of the checkout's worktree diff (`guard.worktree_state`)
    changed since its lane started, other than `known` (its Write and Edit files), that one of its commands writes
    (`_shell_write`), as {real path: (whole file?, [its shell commands])}. A file another caller of `session` edited
    with its own edit tool during the lane (`_edited_by_others`) is that caller's unless a redirection of the member
    wrote into it. Whole when the file is new (untracked); otherwise only lines its commands hold are its (a
    heredoc's body, a sed expression). {} without a start."""
    start = _lane_start(path) if commands else None
    if start is None:
        return {}
    from . import guard
    others = _edited_by_others(session, lane, start)
    out = {}
    for rel, (status, _, mtime_ns) in (guard.worktree_state(top) or {}).items():
        real = os.path.realpath(os.path.join(top, rel))
        if mtime_ns is None or mtime_ns < (start - 1) * 1e9 or real in known or not os.path.isfile(real):
            continue
        how = max((_shell_write(top, real, c) for c in commands), key=WRITE_RANK.index, default="")
        if not how or (how != "redirect" and real in others):
            continue
        out[real] = (status == "??", list(commands))
    return out


WRITE_RANK = ("", "program", "redirect")  # how a command writes a file, weakest first (`_shell_write`)


def _shell_write(top, real, cmd: str) -> str:
    """How one shell command writes this file: "redirect" when one of its redirections writes into it (`> f`,
    `>> f`); "program" when a command naming it runs a program the guard's read-only allowlist does not hold (sed -i,
    cp, a script, a check with --fix: it may write it), or when only a heredoc's body names it and the command is not
    read-only; '' when it only reads it (cat, grep, sed -n, git diff) or does not name it."""
    if not _named(top, real, [cmd]):
        return ""
    from . import guard
    segs = guard.split_commands(cmd)
    if segs is None:
        return "program"  # the guard cannot read it: it may write anything it names
    here, named, how = str(top), False, ""
    for argv, redirs in segs:
        for r in redirs:
            p = guard.resolve(r, here)
            if p != guard.UNKNOWN and os.path.realpath(str(p)) == real:
                return "redirect"
        if argv and os.path.basename(argv[0]) in ("cd", "pushd") and len(argv) > 1 and argv[1] != "-":
            p = guard.resolve(argv[1], here)
            here = str(p) if p != guard.UNKNOWN else here
            continue
        words = " ".join(shlex.quote(a) for a in argv)
        if _named(top, real, [words]):
            named = True
            if guard.readonly_refusal(words, here):
                how = "program"
    if not named and guard.readonly_refusal(cmd, top):  # named only in a heredoc's body: a script may write it
        how = "program"
    return how


def _edited_by_others(session: str, lane: str, start) -> set:
    """The real paths that callers of `session` other than `lane` changed with their own Write, Edit or MultiEdit
    call since `start` (the session's edit record, progress.note_edit). Empty without a session or a lane."""
    if not (session and lane and start):
        return set()
    try:
        from . import progress
        return {os.path.realpath(str(e.get("path"))) for e in progress.edits_since(session, start)
                if str(e.get("caller") or "") != lane and e.get("path")}
    except Exception:
        return set()  # fails open: without the record, the member's commands decide


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


def _flag(words, name: str) -> bool:
    return any(x == name or x.startswith(name + "=") for x in words)


def _ran(command: str, invocations) -> bool:
    want = command.split()
    if not want:
        return False
    want[0] = os.path.basename(want[0])
    if want == ["dev", "progress", "step"] and any(
            w[:3] == ["dev", "progress", "note"] and _flag(w, "--step") and _flag(w, "--check") for w in invocations):
        return True  # partial evidence on a long step records it without closing it (progress.note)
    return any(w[:len(want)] == want for w in invocations)


# The full local CI run is the one proof the tech lead may meet for a member. A brief can forbid the run (in a shared
# checkout, other members' uncommitted work fails the suite, which takes ~40 minutes), and so can a project (runner
# preparation that must not run twice). In its place the gate accepts one of two records the member cannot write
# itself (`_ci_instead`). The member's own `dev ci --mark-passed` is not one: it attests a run, and a member does not
# attest its own proof (the escape-hatch rule of `--ci-passed`, gitx._evidence_or_attest).
CI_PROOF = "dev ci --run"
WAIVER_LINE = "Waived proof: dev ci --run — <why>"  # a line of the brief; <why> is a sentence, as a root cause is
_WAIVER = re.compile(r"(?im)^[ \t>*-]*waived proof:[ \t]*`?dev[ \t]+ci[ \t]+--run`?"
                     r"(?:[ \t]*[—–:,]|[ \t]+-|[ \t]+because)[ \t]+(\S[^\n]*?)[ \t]*$")
_TEMPLATE = re.compile(r"<[^<>\n]+>")  # a <placeholder> left in the line: not a reason
CI_FIX = (f"run `{CI_PROOF}`. If your brief or the project forbids that run, only the tech lead can meet the proof "
          f"for you, with either of these: this line in your brief, `{WAIVER_LINE}`, or a passed `{CI_PROOF}` of "
          "their own on exactly the tree you leave. Ask with `dev crew ask`; your own `dev ci --mark-passed` does not "
          "meet it")


def _brief(path) -> str:
    """The member's brief: the launch's prompt, which Claude Code writes as the first record of the member's own
    transcript, a user record with plain text. Read from the file's first line, since `_records` keeps only the end
    of a very long transcript. '' for any other first record, such as a Codex rollout's session metadata."""
    try:
        with open(path, "rb") as fh:
            rec = json.loads(fh.readline().decode("utf-8", "replace"))
    except (OSError, TypeError, ValueError):
        return ""
    if not isinstance(rec, dict) or rec.get("type") != "user":
        return ""
    return _text((rec.get("message") or {}).get("content"))


def brief_waiver(path) -> str:
    """Why the member's brief waives `dev ci --run`: the reason of its first `Waived proof: dev ci --run — <why>` line
    (WAIVER_LINE; a colon, a hyphen or "because" may stand for the dash) that gives one, a sentence of at least
    gitx.CAUSE_MIN characters with no <placeholder> left; else ''. Nothing else in the brief waives it, and no other
    proof can be waived."""
    from . import gitx
    for m in _WAIVER.finditer(_brief(path)):
        why = " ".join(m.group(1).split())
        if len(why) >= gitx.CAUSE_MIN and not _TEMPLATE.search(why):
            return why
    return ""


def _by_lead(lead_name: str) -> bool:
    """The main session launched the member, so the tech lead wrote its brief. `lead_name` is the name the hook
    passes (hooks._lead_name): the member's own then, "a helper of <member>" when a member launched it, and '' when
    devkit's records show neither, which counts as no (fail closed)."""
    return bool(lead_name) and not lead_name.startswith("a helper of ")


def _ci_markers(top) -> list:
    """The local CI evidence markers of the repository at `top`, newest first: what `dev ci --run` and
    `dev ci --mark-passed` record (gitx.record_evidence). [] when they cannot be read."""
    try:
        from . import gitx
        c = gitx.ctx(top)
        return gitx._markers(c) if c else []
    except Exception:
        return []


def _ci_instead(transcript_path, top, by_lead: bool) -> str:
    """How the tech lead met `dev ci --run` for the member, as the audit record says it, or '' (the gate refuses as
    before): (1) the tech lead's brief waives it (`brief_waiver`; `by_lead`: the main session launched the member);
    (2) a passed `dev ci --run` (or `--resume`) holds the checkout's tree as it is now (gitx.work_tree, computed only
    when such a marker exists). A marker does not say who ran it, but devkit writes a `run` marker only after every
    step passed, so a run on the same tree is the same evidence; a `marked` one only attests a run and never counts."""
    if not top:
        return ""
    why = brief_waiver(transcript_path) if by_lead else ""
    if why:
        return f"a waiver in the tech lead's brief: {why}"
    runs = [m for m in _ci_markers(top) if m.get("how") in ("run", "resumed")]
    if not runs:
        return ""
    from . import gitx
    tree = gitx.work_tree(top)
    hit = next((m for m in runs if tree and m.get("tree") == tree), None)
    return f"a passed `{CI_PROOF}` on the tree it leaves ({tree[:7]}: {gitx.describe_evidence(hit)})" if hit else ""


# A lesson a refusal shows, in the two forms devkit writes: a lesson rule's refusal ("… from lesson L-…: <title>") and
# a recalled pitfall ("Lesson L-… (recorded by …"). An id the refusal only quotes (`dev lesson release L-…`) is not one.
_SHOWN_LESSON = re.compile(r"\bfrom lesson (L-[0-9A-Za-z]{6,40}):|\bLesson (L-[0-9A-Za-z]{6,40}) \(recorded by ")


def _markers(text: str) -> list:
    """The (fingerprint, code) of each devkit-bug marker line in a refusal's text (guard.refusal_note)."""
    from . import bugs
    return [m.groups() for m in bugs.MARKER_RE.finditer(text)] if "devkit-bug:" in text else []


# What `dev lesson add` prints when it recorded a lesson and `dev lesson recur` when it counted one (cli.cmd_lesson):
# the lesson's id after a check mark. A refused add or a recur of no lesson prints ✗ and its problems instead.
_LESSON_DONE = re.compile(r"(?m)^\s*✓ L-[0-9A-Za-z]{6,40} (?:recorded in |happened \d+×)")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _lessons_done(path) -> list:
    """The shell commands of a member's transcript whose output shows a lesson recorded or counted (_LESSON_DONE), in
    order. Neither the exit code nor the command alone tells: `dev lesson add … | tail` exits 0 when it is refused."""
    shells, out = {}, []
    for rec in _records(path):
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict):
                continue
            if rec.get("type") == "assistant" and c.get("type") == "tool_use" and c.get("name") in SHELL_TOOLS:
                cmd = (c.get("input") or {}).get("command") or (c.get("input") or {}).get("cmd") or ""
                shells[c.get("id")] = " ".join(map(str, cmd)) if isinstance(cmd, list) else str(cmd)
            elif rec.get("type") == "user" and c.get("type") == "tool_result" and c.get("tool_use_id") in shells:
                if _LESSON_DONE.search(_ANSI.sub("", _text(c.get("content")))):
                    out.append(shells[c["tool_use_id"]])
    return out


def _answered(invocations, refusals, done=()) -> bool:
    """The lane answered its refusals: a lesson recorded or counted (`done`: the invocations of the commands whose
    output shows it, _lessons_done), a refusal reported to devkit's bug registry (`dev bug report --guard`, with an
    id or the session's last refusal), or a vote on a bug that a refusal's marker names (what the refusal itself asks
    for)."""
    if _ran("dev lesson add", done) or _ran("dev lesson recur", done):
        return True
    reports = [w[3:] for w in invocations if w[:3] == ["dev", "bug", "report"]]
    if any(a == "--guard" or a.startswith("--guard=") for args in reports for a in args):
        return True
    named = {fp for text in refusals for fp, _ in _markers(text)}
    return any(a in named for args in reports for a in args)


def _owed(refusals, session: str, top: str) -> list:
    """The refusals that still owe a lesson: those the registry served no workaround for in this session (the session
    ledger's `served` rows, matched by the marker's fingerprint and one-time code, so text alone proves nothing) and
    that no open lesson covers (the refusal shows it, as a lesson rule's refusal or the call's recalled pitfalls do,
    or shows its error signature: the project's lessons and the global ones, or the global ones alone outside a
    checkout). A lookup that fails covers nothing: the refusal counts, as it did before."""
    try:
        from . import bugs
        served = {(r.get("served"), r.get("code")) for r in bugs.ledger(session) if r.get("served")} if session \
            else set()
    except Exception:
        served = set()
    open_ids = None
    project, inits = "global", []
    out = []
    for text in refusals:
        if any(m in served for m in _markers(text)):
            continue
        try:
            from . import lessons
            if open_ids is None:
                open_ids = {x["meta"]["id"] for x in lessons.all_lessons()
                            if x["meta"].get("status") == "open" and not x["meta"].get("superseded_by")}
                if top:
                    from . import delivery
                    project = delivery.scope_for(top) or "global"
                from . import guard
                inits = guard.initiatives_of(project) if project != "global" else []
            shown = {i for pair in _SHOWN_LESSON.findall(text) for i in pair if i}
            if open_ids & shown or lessons.for_output(project, text, limit=1, initiatives=inits):
                continue
        except Exception:
            pass
        out.append(text)
    return out


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


def gated_skills(member, roster=None) -> list:
    """The skills the skills gate checks from a member's procedure, for its brief and for the gate itself:
    [{"skill", "phase", "always"}], one per phase and skill, in procedure order. `always`: the phase's proof is owed
    whatever files the member changes (a proof about files, FILE_PROOFS, is owed only for changes of its kind), so
    every stop owes the skill; otherwise only a stop after work in that phase does (one of its devkit commands ran,
    or its proof became owed). The skill rules add the skills of the files the member changes
    (`skillguard.rules_for_files`)."""
    m = _member(member, roster) or {}
    out = []
    for x in m.get("procedure") or []:
        if not isinstance(x, dict):
            continue
        proof = " ".join(str(x.get("proof") or "").split())
        for s in x.get("skills") or []:
            out.append({"skill": s, "phase": x.get("phase"), "always": bool(proof) and proof not in FILE_PROOFS})
    return out


def _skill_findings(m, invocations, loaded, changed, top, kinds=(), agent: str = "claude") -> list:
    """`changed`: the paths the skill rules match; `kinds`: the changed paths as the phases see them (repository
    paths), which decide the phases the member worked in. `agent`: the member's agent, whose skills it can load."""
    need = {}  # short name -> (name as written, [why])
    phases = {}
    for x in (m or {}).get("procedure") or []:
        if isinstance(x, dict):
            phases.setdefault(x.get("phase"), x)
    for g in gated_skills(m or {}):
        if g["always"] or _phase_applied(phases.get(g["phase"]) or {}, invocations, kinds):
            need.setdefault(skillguard.short(g["skill"]), (g["skill"], []))[1].append(
                f"the {g['phase']} phase of your procedure")
    for rule, matched in skillguard.rules_for_files(skillguard.delivered(changed)):
        names = ", ".join(_rel(f, top) for f in matched[:3]) + (" …" if len(matched) > 3 else "")
        for s in skillguard.required(rule, agent):
            need.setdefault(s, (s, []))[1].append(f"your change to {names} ({rule.get('why') or rule.get('id')})")
    have = skillguard.installed(agent)
    out = []
    for key, (name, why) in need.items():
        if key in loaded or key not in have:  # loaded, or not on this Mac: nothing the member can do
            continue
        path = skillguard.skill_path(name, agent)
        if agent == "claude":
            fix = f'the Skill tool with skill "{name}"' + (f", or Read {core.tilde(path)}" if path else "")
        else:
            fix = f"read {core.tilde(path)}" if path else f"read the SKILL.md of {name} (listed among your skills)"
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


def crew_gates(member, transcript_path, cwd, roster=None, agent: str = "", session: str = "",
               lane: str = "", by_lead: bool = False, accepted=None) -> list:
    """What a crew member still owes, from its own transcript: [{rule, item, evidence, fix}], in the order of
    RULES. `member` is a roster entry or a name (`Kid`, `DevEx engineer (Kid)`, `devkit:kid`); an unknown name
    still gets the gates that need no procedure. `agent`: the member's agent ("claude" or "codex"; default: the one
    that wrote the transcript), whose transcript format and installed skills the gates read. `session`, `lane`: the
    session and the member's agent id, whose edit record tells which files other callers edited meanwhile.
    `by_lead`: the main session launched the member, so a waiver in its brief counts (`_ci_instead`). `accepted`: a
    list that gets {proof, how} for each proof met another way, for the audit trail."""
    m = _member(member, roster)
    read_only = (m or {}).get("tools") == "read"
    agent = agent or agent_of(transcript_path)
    commands, refusals = _activity(transcript_path, agent)
    inv = _invocations(commands, agent)
    loaded, edited, _ = skillguard.scan(agent, transcript_path)
    native_written = _codex_written(transcript_path, cwd) if agent == "codex" else {}
    edited += list(native_written)
    paths = [str(Path(f) if os.path.isabs(f) else Path(cwd or ".") / f) for f in dict.fromkeys(edited) if f]
    top = core.gitout(cwd, "rev-parse", "--show-toplevel") if cwd and os.path.isdir(cwd) else ""
    # real paths: git names the top as /private/var/… where the member wrote /var/… (macOS temp folders)
    in_repo = [os.path.realpath(f) for f in paths if top and os.path.isfile(f) and core.is_under(f, top)]
    shell = _shell_written(top, transcript_path, commands, set(in_repo), session, lane) if top and not read_only \
        else {}
    touched = list(shell)  # what its shell wrote: the phases count it
    kinds = [_rel(f, top) for f in dict.fromkeys(in_repo + touched)] + [f for f in paths if f not in in_repo
                                                                         and not (top and core.is_under(f, top))]
    found = []
    if not read_only:
        for x in (m or {}).get("procedure") or []:
            proof = x.get("proof") if isinstance(x, dict) else None
            proof_kinds = kinds
            if agent == "codex" and " ".join(str(proof or "").split()) == "dev test prove":
                # A diagnostic probe outside every repository has no delivered change to mutate. Keep actual
                # repository writes (including a repository nested in scratch), and keep phase skills unchanged.
                delivered = skillguard.delivered([os.path.realpath(f) for f in paths + touched])
                proof_kinds = [_rel(f, top) for f in delivered]
            if proof and _proof_owed(x, proof_kinds) and not _ran(proof, inv):
                ci = " ".join(str(proof).split()) == CI_PROOF
                how = _ci_instead(transcript_path, top, by_lead) if ci else ""
                if how:
                    if accepted is not None:
                        accepted.append({"proof": proof, "how": how})
                    continue
                found.append(_finding("proofs", proof, f"`{proof}` is not among your {len(commands)} shell commands "
                                      "(text inside quotes or a heredoc does not count)",
                                      CI_FIX if ci else f"run `{proof}{PROOF_ARGS.get(proof, '')}`"))
    # a member that never edits (tools review or read) wrote nothing through its shell: a file its commands name that
    # changed during its lane is another lane's, and owes it no skill
    edits = (m or {}).get("tools") not in ("review", "read")
    found += _skill_findings(m, inv, loaded, paths + (touched if edits else []), top, kinds, agent)
    if in_repo or shell:
        written = {**(native_written if agent == "codex" else _written(transcript_path)), **shell}
        changed = in_repo + sorted(shell)
        found += _stub_findings(top, changed, written)
        found += _test_findings(top, changed, written)
    owed = _owed(refusals, session, top) if not read_only and len(refusals) >= 2 and \
        not _answered(inv, refusals, _invocations(_lessons_done(transcript_path), agent)) else []
    if len(owed) >= 2:
        first = (re.sub(r"^[:\s]+", "", DENIAL_RE.sub("", owed[0], 1)).splitlines() or [""])[0][:120]
        found.append(_finding("lessons", "dev lesson add",
                              f"{len(owed)} guard refusals and no dev lesson add or dev lesson recur; the first: "
                              f"\"{first}\"",
                              'run `dev lesson add "<the rule to follow>" --happened "<what you ran and what the '
                              'guard said>" --cause "<why it happened>" --prevent "<the rule for next time>" '
                              '--command "<the refused command words>" --error "<text of the refusal>"` (an anchor '
                              'says where it applies: a lesson with none is refused), or '
                              '`dev lesson recur <id> --evidence "<what happened>"` for a known pitfall; when the guard '
                              'was wrong, `dev bug report --guard <refusal id>` instead'))
    for t in background_work(transcript_path):
        found.append(dict(_finding("background", t["command"], "you started it, and nothing in your transcript shows "
                                   "that it ended (no task notification, TaskStop or exit)",
                                   f"do not end your turn to wait (that ends your report): keep working until it ends, or re-run "
                                   f"it in the foreground with a longer timeout; or stop it with TaskStop `{t['id']}`; then "
                                   f"report again"), task=t["id"]))
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
    (`hooks.crew_background_notice`). `key` is the agent id devkit records for the lane, which TaskStop takes.
    `who` is the name devkit's launch records give the agent; without one nothing is kept (the audit has it)."""
    if not who:
        return
    sess.setdefault("crew_background", {})[key] = {"who": who, "tasks": [{"id": t["id"], "command": t["command"]}
                                                                         for t in tasks]}


def check(name, data: dict, sess: dict, pid: str = "", roster=None, lead_name: str = "", agent: str = ""):
    """The SubagentStop entry point. -> the hook's {"decision": "block", "reason": …} the first time a member stops
    with something missing, else None. Records each finding set as `crew.gate` in the audit trail ("blocked", or
    "recorded" when `crew.gates.enforce` is false) and marks the member in `sess["crew_gated"]`, so it is sent back
    only once. A member let through with background work still running (sent back once already, or not enforced) is
    audited and kept for the tech lead as `lead_name` (`_keep_for_lead`): the name devkit's launch records give it
    (`hooks._lead_name`), never `name`, which may come from a description a member wrote for its helper. Without
    one, nothing is kept. `lead_name` also says whether the tech lead wrote the brief (`_by_lead`), and a proof met
    another way is audited as `crew.gate` with outcome "ok" and how it was met. Fails open."""
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
                _keep_for_lead(sess, key, lead_name, tasks)
            return None
        m = _member(name, roster)
        accepted = []
        findings = crew_gates(m or name, transcript, data.get("cwd") or "", roster, agent,
                              session=data.get("session_id") or "", lane=data.get("agent_id") or "",
                              by_lead=_by_lead(lead_name), accepted=accepted)
        who = crew.who(m) if m else str(name or "Agent")
        if accepted:  # a proof met another way is a choice someone can review: `dev audit --action crew.gate`
            audit.record("crew.gate", pid or "", f"{who}: " + "; ".join(
                f"proofs: {a['proof']} met instead by {a['how']}" for a in accepted), "ok", member=who,
                         rules=["proofs"])
        if not findings:
            return None
        sess.setdefault("crew_gated", {})[key] = who
        enforce = enforced()
        audit.record("crew.gate", pid or "", f"{who}: " + "; ".join(f"{f['rule']}: {f['item']}" for f in findings),
                     "blocked" if enforce else "recorded", member=who, rules=sorted({f["rule"] for f in findings}))
        if enforce:
            return {"decision": "block", "reason": send_back_reason(findings, who)}
        running = [{"id": f["task"], "command": f["item"]} for f in findings if f["rule"] == "background"]
        if running:
            _keep_for_lead(sess, key, lead_name, running)
        return None
    except Exception:
        return None
