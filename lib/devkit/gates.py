# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The crew gates: what a crew member owes before it stops (SubagentStop).

The main session's Stop hook checks a turn for unfinished work, tests that cannot fail and missing skills. These
gates hold a crew member to the same standard and to its own procedure (config/crew.json). They read the member's
own transcript (`agent_transcript_path` in the SubagentStop input), never the main session's:

  proofs   each `proof` of its procedure ran as a shell command, not as text inside quotes or a heredoc
  skills   it loaded the skills of each procedure phase it worked in (a phase with a proof, or one whose devkit
           commands it ran) and the skills devkit's skill rules require for the files it changed: a Skill tool call,
           or a read of the skill's SKILL.md
  stubs    the files it changed leave no unfinished work and no untracked deferrals (the `stubs` module)
  tests    the test files it changed hold no test that cannot fail (`testaudit`, high findings)
  lessons  two or more guard refusals are answered with `dev lesson add` or `dev lesson recur`

Read-only members (`tools: read`) have no shell, so they skip the proofs and lessons gates. `check` is the hook's
entry point: it sends a member back once with every missing item, or only records it when `crew.gates.enforce` is
false. Either way the audit trail gets a `crew.gate` record. Everything fails open.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import core, skillguard

RULES = ("proofs", "skills", "stubs", "tests", "lessons")
LABELS = {"proofs": "proof not run", "skills": "skill not loaded", "stubs": "unfinished work in",
          "tests": "tests that cannot fail in", "lessons": "no lesson after repeated refusals:"}
MAX_BYTES = 8_000_000  # the end of a very long member transcript is enough
SHELL_TOOLS = {"Bash", "exec_command", "shell", "local_shell", "unified_exec"}
# A hook's denial as Claude Code writes it into the tool result: "PreToolUse:Bash hook error: <the guard's reason>".
DENIAL_RE = re.compile(r"^\s*(?:PreToolUse|PermissionRequest)(?::[^\s:]+)? hook (?:blocking )?error\b", re.I)
_SPLIT = re.compile(r"\|\||&&|\$\(|[;&|()`\n]")  # where a new simple command starts
_LEAD = {"then", "do", "else", "elif", "if", "while", "until", "time", "exec", "command", "nohup", "env", "!", "{"}
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
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
            if words:
                out.append([os.path.basename(words[0])] + words[1:])
    return out


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


def _skill_findings(m, invocations, loaded, changed, top) -> list:
    need = {}  # short name -> (name as written, [why])
    for x in (m or {}).get("procedure") or []:
        if not isinstance(x, dict) or not (x.get("proof") or any(_ran(c, invocations) for c in x.get("devkit") or [])):
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


def _stub_findings(top, files) -> list:
    from . import stubs
    found = stubs.scan_changes(top, [os.path.relpath(f, top) for f in files])
    if not found:
        return []
    where = sorted({f["file"] for f in found})
    lines = "; ".join(f"{f['file']}:{f['line']} {f['rule']}: {f['text'].strip()[:80]}" for f in found[:5])
    more = f" (and {len(found) - 5} more)" if len(found) > 5 else ""
    return [_finding("stubs", ", ".join(where), lines + more,
                     'build it for real; if deferring it is the plan, run `dev task add "<the deferred work>" --why '
                     '"<why>"` and name the task on the line; then check with `dev stubs ' + " ".join(where) + "`")]


def _test_findings(top, files) -> list:
    from . import testaudit
    tests = [f for f in files if testaudit.is_test_file(os.path.relpath(f, top))]
    high = [f for f in testaudit.audit(tests, top=top) if f.get("severity") == "high"] if tests else []
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
    found = []
    if not read_only:
        from . import crew
        for proof in crew.proofs(m or {}):
            if not _ran(proof, inv):
                found.append(_finding("proofs", proof, f"`{proof}` is not among your {len(commands)} shell commands "
                                      "(text inside quotes or a heredoc does not count)",
                                      f"run `{proof}{PROOF_ARGS.get(proof, '')}`"))
    found += _skill_findings(m, inv, loaded, paths, top)
    if in_repo:
        found += _stub_findings(top, in_repo)
        found += _test_findings(top, in_repo)
    if not read_only and len(refusals) >= 2 and not (_ran("dev lesson add", inv) or _ran("dev lesson recur", inv)):
        first = (re.sub(r"^[:\s]+", "", DENIAL_RE.sub("", refusals[0], 1)).splitlines() or [""])[0][:120]
        found.append(_finding("lessons", "dev lesson add",
                              f"{len(refusals)} guard refusals and no dev lesson add or dev lesson recur; the first: "
                              f"\"{first}\"",
                              'run `dev lesson add "<what the guard refused>" --cause "<why it happened>" --prevent '
                              '"<the rule for next time>"`, or `dev lesson recur <id> --evidence "<what happened>"` '
                              'for a known pitfall'))
    return found


def send_back_reason(findings, who) -> str:
    """One message for the hook's `decision: block`, naming each missing item and the command that fixes it."""
    if not findings:
        return ""
    lines = [f"devkit · {who}, the crew gates found {len(findings)} thing{'s' if len(findings) != 1 else ''} "
             f"missing before you stop:"]
    for i, f in enumerate(findings, 1):
        lines.append(f"{i}. {LABELS.get(f['rule'], f['rule'])} `{f['item']}`: {f['evidence']}. Fix: {f['fix']}.")
    lines.append("Fix each one, then end with the same report, status block first. devkit sends you back only once.")
    return "\n".join(lines)


def check(name, data: dict, sess: dict, pid: str = "", roster=None):
    """The SubagentStop entry point. -> the hook's {"decision": "block", "reason": …} the first time a member stops
    with something missing, else None. Records each finding set as `crew.gate` in the audit trail ("blocked", or
    "recorded" when `crew.gates.enforce` is false) and marks the member in `sess["crew_gated"]`, so it is sent back
    only once. Fails open."""
    try:
        key = data.get("agent_id") or str(name or "")
        transcript = data.get("agent_transcript_path")
        if not transcript or key in (sess.get("crew_gated") or {}):
            return None
        m = _member(name, roster)
        findings = crew_gates(m or name, transcript, data.get("cwd") or "", roster)
        if not findings:
            return None
        from . import audit, crew
        who = crew.who(m) if m else str(name or "Agent")
        sess.setdefault("crew_gated", {})[key] = who
        enforce = enforced()
        audit.record("crew.gate", pid or "", f"{who}: " + "; ".join(f"{f['rule']}: {f['item']}" for f in findings),
                     "blocked" if enforce else "recorded", member=who, rules=sorted({f["rule"] for f in findings}))
        return {"decision": "block", "reason": send_back_reason(findings, who)} if enforce else None
    except Exception:
        return None
