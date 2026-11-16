# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Pure, closed configuration contract. No filesystem or runtime imports."""
from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse

DEFAULTS = {
    "dev_root": "~/Development",
    "github_user": "",              # empty: detected from `gh api user`
    "handoff_repo": "",             # empty: <github_user>/handoff
    "handoff_dir": "",              # empty: <dev_root>/.handoff
    "sync": {"push": True, "interval_minutes": 60},
    "gc": {
        "auto": True,               # the background job applies the safe class automatically
        "grace_hours": 24,          # merged / fully-contained worktrees idle this long are removed
        "idle_artefact_days": 7,    # regenerable build dirs are pruned in worktrees idle this long
        "temp_days": 7,             # temp entries older than this are purged
        "docker": True,             # also prune dangling images and build cache older than 7 days (never volumes)
    },
    "docker": {"cpu": 6, "memory": 12, "disk": 80, "vm_type": "vz", "rosetta": True,
               "mount_type": "virtiofs", "autostart": False},
    "editor": "code",
    "agents": {"claude": True, "codex": True},
    "claude": {"default_mode": "bypassPermissions"},
    "codex": {"mode": "full", "approval_policy": "never", "sandbox_mode": "danger-full-access"},
    "stacks": ["java", "python", "php", "node"],
    # skills.groups: the optional skill groups of config/skills.json this Mac installs. skills.optional: groups never
    # installed by default, which the setup wizard offers when the developer picks one of these tools.
    "skills": {"groups": ["security-deep", "postgres", "observability"], "optional": {"iac": ["terraform"]},
               "enforce": True, "rules": []},
    # ext: the developer's own extensions on top of devkit's (`dev ext add|remove|enable|disable`, spec P-fym583l7sg1)
    "ext": {"add": [], "remove": [], "disable": [], "enable": []},
    # language: what the agents reply to the developer in. auto: the language of the developer's latest message (the
    # earlier ones when it is too short to tell, American English when none tells); a locale (en-US, es-ES) fixes it.
    # Code, commits, PRs, docs and records stay in American English (lib/devkit/language.py). Setup never writes the
    # key, so a stored value is the developer's own choice.
    "language": "auto",
    "hosts": ["github"],
    "clouds": [],
    "packages": {"auto": True},
    "ui": {"powerline": False},
    # progress: updates, timing, and the crew's slots (spec P-fxtwig4hqvx). quota_steps scales max_parallel by the
    # plan's usage (5-hour and weekly windows), and from quota_percent one member works at a time; slot_weights is
    # what one launch costs by kind; reserve keeps slots for the developer's own request (docs/configuration.md).
    "progress": {"nudge_minutes": 8, "notify": True, "notify_after_minutes": 10, "slow_tool_seconds": 20,
                 "max_parallel": 4, "max_depth": 2, "quota_percent": 90,
                 "quota_steps": {"50": 1.0, "70": 0.75, "85": 0.5},
                 "slot_weights": {"builder": 1.0, "review": 0.5, "read": 0.5, "scout": 0.0},
                 "reserve": 1, "stale_agent_hours": 6, "stale_agent_minutes": 90},
    "work": {"lock_hours": 8, "mode": "auto"},
    "lock": False,
    "handoff": {"enforce": True},
    "canary": {"enforce": True},
    "dashboard": {"idle_minutes": 30},  # the live dashboard server stops after this long with no page open
    # devkit's public bug registry: share is the developer's consent to post (on, reports, off; unset asks), and
    # sync lets the hourly sync refresh the known bugs and send what the outbox holds
    "bugs": {"share": None, "sync": True},
    # the audit trail (docs/audit.md). The team policy (<handoff>/policy/audit.json) sets a floor: a machine may
    # record more and keep longer, never less. Only the developer changes these, in a terminal (spec C11).
    # Retention (compliance review): the team copy is read more widely, so it keeps no longer than this machine by
    # default; floor_months is never under AUDIT_FLOOR_MIN. Pruning the handoff copy leaves the months in git history.
    "audit": {
        "publish": True,            # the daily job copies this machine's records to the handoff repository
        "level": "changes",         # security | changes | all (all: also read-only commands)
        "retention": {"local_months": 13, "team_months": 13, "floor_months": 3},
        "hold": False,              # a legal hold: nothing is pruned, here or in the handoff copy
        "forward": "",              # "" | a file (JSONL lines) | "syslog" | "syslog://host:port" (CEF lines)
        "redact": {"extra": []},    # more regular expressions for the redactor
    },
}

AUDIT_LEVELS = ("security", "changes", "all")  # from the fewest records to the most
# the least floor_months may be: 3 months covers the time it takes to notice an incident and investigate it. Less
# data comes from audit.level security, not from a shorter floor (compliance review, GDPR art. 5(1)(e) and 32)
AUDIT_FLOOR_MIN = 3


def _months_ok(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 1200


def _floor_ok(v) -> bool:
    return _months_ok(v) and v >= AUDIT_FLOOR_MIN


def _patterns_ok(v) -> bool:
    return isinstance(v, list) and all(isinstance(p, str) and p for p in v)


def _bad_patterns(v: list) -> list:
    bad = []
    for p in v:
        try:
            re.compile(p)
        except (re.error, OverflowError, RecursionError) as e:
            bad.append(f"{p!r} ({e})")
    return bad


def _forward_ok(v) -> bool:
    if not isinstance(v, str):
        return False
    if v in ("", "syslog"):
        return True
    if v.startswith("syslog://"):
        return bool(re.fullmatch(r"syslog://[A-Za-z0-9.-]+:\d{1,5}", v))
    return v.startswith(("/", "~"))


# the audit section's keys -> (the check, what the value must be)
AUDIT_RULES = {
    ("publish",): (lambda v: isinstance(v, bool), "true or false"),
    ("level",): (lambda v: v in AUDIT_LEVELS, "security, changes or all"),
    ("hold",): (lambda v: isinstance(v, bool), "true or false"),
    ("forward",): (_forward_ok, '"" (off), an absolute file path, "syslog" or "syslog://host:port"'),
    ("retention", "local_months"): (_months_ok, "a whole number of months from 1 to 1200"),
    ("retention", "team_months"): (_months_ok, "a whole number of months from 1 to 1200"),
    ("retention", "floor_months"): (_floor_ok, f"a whole number of months from {AUDIT_FLOOR_MIN} to 1200 "
                                               f"({AUDIT_FLOOR_MIN} is the hard minimum)"),
    ("redact", "extra"): (_patterns_ok, "a list of regular expressions"),
}


def audit_config(raw, fill: bool = True):
    """An audit section checked against AUDIT_RULES -> (values, errors). `fill`: every key, a default where the
    value is missing or wrong; else only the keys `raw` sets with a valid value (a team policy's floor)."""
    values = json.loads(json.dumps(DEFAULTS["audit"])) if fill else {}
    errors = []
    if raw is None:
        return values, errors
    if not isinstance(raw, dict):
        return values, ["audit must be an object"]
    groups = {path[0] for path in AUDIT_RULES if len(path) == 2}
    for key, val in raw.items():
        if key in groups:
            if not isinstance(val, dict):
                errors.append(f"audit.{key} must be an object")
                continue
            items = [((key, k), v) for k, v in val.items()]
        else:
            items = [((key,), val)]
        for path, v in items:
            name = "audit." + ".".join(path)
            rule = AUDIT_RULES.get(path)
            if not rule:
                errors.append(f"unknown key {name}")
                continue
            if not rule[0](v):
                errors.append(f"{name} must be {rule[1]} (got {json.dumps(v, ensure_ascii=False)[:40]})")
                continue
            if path == ("redact", "extra") and _bad_patterns(v):  # the valid patterns still apply
                errors += [f"{name}: {b} is not a regular expression" for b in _bad_patterns(v)]
                v = [p for p in v if not _bad_patterns([p])]
            if len(path) == 2:
                values.setdefault(path[0], {})[path[1]] = v
            else:
                values[path[0]] = v
    return values, errors


def _number_in(lo, hi, whole=False):
    def ok(v):
        if isinstance(v, bool) or not isinstance(v, int if whole else (int, float)):
            return False
        return lo <= v <= hi
    return ok


def _steps_ok(v) -> bool:
    """quota_steps: {"<usage percent>": <share of max_parallel, 0 to 1>}, such as {"70": 0.75}."""
    if not isinstance(v, dict):
        return False
    for k, f in v.items():
        try:
            pct = float(k)
        except (TypeError, ValueError):
            return False
        if not 0 <= pct <= 100 or not _number_in(0, 1)(f):
            return False
    return True


SLOT_KINDS = ("builder", "review", "read", "scout")


def _weights_ok(v) -> bool:
    return isinstance(v, dict) and all(k in SLOT_KINDS and _number_in(0, 1)(w) for k, w in v.items())


# the progress section's keys -> (the check, what the value must be)
PROGRESS_RULES = {
    "nudge_minutes": (_number_in(1, 240), "a number of minutes from 1 to 240"),
    "notify": (lambda v: isinstance(v, bool), "true or false"),
    "notify_after_minutes": (_number_in(0, 1440), "a number of minutes from 0 to 1440"),
    "slow_tool_seconds": (_number_in(1, 3600), "a number of seconds from 1 to 3600"),
    "max_parallel": (_number_in(1, 10, whole=True), "a whole number from 1 to 10"),
    "max_depth": (_number_in(1, 5, whole=True), "a whole number from 1 to 5"),
    "quota_percent": (_number_in(1, 100), "a usage share in percent from 1 to 100"),
    "quota_steps": (_steps_ok, 'an object of usage percent to the share of max_parallel from 0 to 1, such as '
                               '{"70": 0.75}'),
    "slot_weights": (_weights_ok, "an object of builder, review, read or scout to a cost from 0 to 1"),
    "reserve": (_number_in(0, 9, whole=True), "a whole number of slots from 0 to 9"),
    "stale_agent_hours": (_number_in(1, 168), "a number of hours from 1 to 168"),
    "stale_agent_minutes": (_number_in(5, 1440), "a number of minutes from 5 to 1440"),
}


def progress_config(raw):
    """A progress section checked against PROGRESS_RULES -> (values, errors): every key, with its default where the
    value is missing or wrong. quota_steps and slot_weights add to their defaults, key by key."""
    values = json.loads(json.dumps(DEFAULTS["progress"]))
    if raw is None:
        return values, []
    if not isinstance(raw, dict):
        return values, ["progress must be an object"]
    errors = []
    for key, v in raw.items():
        rule = PROGRESS_RULES.get(key)
        if not rule:
            errors.append(f"unknown key progress.{key}")
        elif not rule[0](v):
            errors.append(f"progress.{key} must be {rule[1]} (got {json.dumps(v, ensure_ascii=False)[:40]})")
        elif isinstance(values.get(key), dict):
            values[key] = {**values[key], **v}
        else:
            values[key] = v
    return values, errors


class SchemaError(ValueError):
    """A candidate or JSON document violates the configuration contract."""


NPM_EXACT = re.compile(r"^(@?[A-Za-z0-9][\w.\-]*(?:/[\w.\-]+)?)@(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?)$")
PYPI_EXACT = re.compile(r"^([A-Za-z0-9][\w.\-]*(?:\[[\w,\-]+\])?)==(\d+(?:\.\d+)+(?:(?:a|b|rc|\.post|\.dev)\d+)?)$")
MCP_RUNNERS = {"npx": ("npm", NPM_EXACT, frozenset({"-y", "--yes", "-q", "--quiet"})),
               "uvx": ("pypi", PYPI_EXACT, frozenset({"-q", "--quiet"}))}
# variables that make node, python or the dynamic loader run other code, or npm and uv fetch from another registry
CODE_ENV = re.compile(r"^(?:NODE_OPTIONS|NODE_PATH|PYTHONPATH|PYTHONSTARTUP|PYTHONHOME|PYTHONUSERBASE|DYLD_\w*|LD_\w*"
                      r"|NPM_CONFIG_\w*|UV_INDEX\w*|UV_DEFAULT_INDEX|UV_EXTRA_INDEX_URL|PIP_INDEX_URL|"
                      r"PIP_EXTRA_INDEX_URL)$", re.I)


def mcp_pin(eid: str, spec: dict) -> tuple:
    """(pin kind, pin, source, upstream) of an MCP server; SchemaError unless it is an https URL, or npx or uvx running a
    package at an exact version with no variable that loads other code."""
    if not isinstance(spec, dict):
        raise SchemaError(f"{eid}: an MCP server is an object with command and args, or a url")
    url = spec.get("url")
    if url:
        u = urlparse(str(url))
        if u.scheme != "https" or not u.hostname:
            raise SchemaError(f"{eid}: a remote MCP server needs an https URL, not {url}")
        return "url", str(url), u.hostname, ""
    cmd, args = str(spec.get("command") or ""), [str(a) for a in spec.get("args") or []]
    if cmd not in MCP_RUNNERS:
        raise SchemaError(f"{eid}: a local MCP server runs through npx or uvx, so the pinned package is what runs; got "
                       f"{cmd or 'no command'}")
    env = spec.get("env") or {}
    bad = sorted(k for k in (env if isinstance(env, dict) else {}) if CODE_ENV.match(str(k)))
    if bad:
        raise SchemaError(f"{eid}: env {', '.join(bad)} makes the runtime load other code or another registry, so the "
                       f"pinned package would not be all that runs")
    registry, exact, flags = MCP_RUNNERS[cmd]
    i, pkg = 0, ""
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if cmd == "uvx" and a == "--from" and i + 1 < len(args):
            pkg, i = args[i + 1], i + 2
            break
        if cmd == "uvx" and a.startswith("--from="):
            pkg, i = a.split("=", 1)[1], i + 1
            break
        if a not in flags:
            raise SchemaError(f"{eid}: `{cmd} {a}` may run another package than the pinned one; only "
                           f"{', '.join(sorted(flags))} may come before it")
        i += 1
    if not pkg:
        pkg = args[i] if i < len(args) else ""
    m = exact.match(pkg)
    if not m:
        form = "pkg@1.2.3" if cmd == "npx" else "pkg==1.2.3, or --from pkg==1.2.3 <command>"
        raise SchemaError(f"{eid}: an MCP server needs an exact version on the package {cmd} runs ({form}) or an https "
                       f"url; got {' '.join([cmd] + args)}")
    return "version", m.group(2), f"{registry} {m.group(1)}", f"{registry}:{m.group(1)}"


@dataclass(frozen=True)
class Rule:
    check: Callable = lambda value: True
    expected: str = "a supported value"
    children: dict = field(default_factory=dict)
    values: object = None
    key: Callable = lambda value: True
    items: object = None
    required: tuple = ()
    object_type: bool = False


def obj(children, required=()):
    return Rule(children=children, required=required, object_type=True, expected="an object")


def mapping(key, values):
    return Rule(values=values, key=key, object_type=True, expected="an object")


def array(items):
    return Rule(check=lambda v: isinstance(v, list), expected="a list", items=items)


def enum(*values):
    return Rule(lambda v: type(v) in (str, type(None)) and v in values,
                "one of " + ", ".join(str(v) for v in values))


def number(lo=0, hi=float("inf"), whole=False, positive=False):
    return Rule(lambda v: not isinstance(v, bool) and isinstance(v, int if whole else (int, float))
                and (isinstance(v, int) or math.isfinite(v)) and lo <= v <= hi and (not positive or v > 0),
                ("a whole number" if whole else "a finite number") +
                (" greater than zero" if positive else f" from {lo} to {hi}"))


def pattern(regex, description):
    return Rule(lambda v: isinstance(v, str) and re.fullmatch(regex, v) is not None, description)


TEXT = Rule(lambda v: isinstance(v, str), "a string")
NONEMPTY = Rule(lambda v: isinstance(v, str) and bool(v.strip()), "a nonempty string")
PATH = Rule(lambda v: isinstance(v, str) and "\x00" not in v and
            (v.startswith(("/", "~/")) or bool(re.fullmatch(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|"
                                                         r"\{[A-Za-z_][A-Za-z0-9_]*\})(?:/.*)?", v))),
            "an absolute path, ~/ path, or environment-rooted path")
BOOL = Rule(lambda v: isinstance(v, bool), "true or false")
POSINT = number(whole=True, positive=True)
TOKEN = pattern(r"[A-Za-z0-9][A-Za-z0-9_-]*", "a name of letters, digits, dash or underscore")
HOST = pattern(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
               r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*", "a hostname")
GROUPS = ("security-deep", "codeql", "postgres", "observability", "iac", "load-testing",
          "contract-testing", "skill-review")
STACKS = ("java", "python", "php", "node")
FRAMEWORKS = ("angular", "react", "nextjs", "nestjs", "spring", "laravel", "symfony")
HOSTS = ("github", "gitlab", "bitbucket", "azure-devops")
CLOUDS = ("aws", "azure", "gcp", "digitalocean", "kubernetes", "terraform")
APPROVAL = enum("never", "untrusted", "on-request", "on-failure")
SANDBOX = enum("read-only", "workspace-write", "danger-full-access")
EFFORT = enum("", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
ACTIVITIES = ("plan", "scope", "requirements", "research", "architecture", "ux", "frontend", "backend",
              "database", "feature", "debug", "mobile", "ai", "tooling", "testing", "security", "redteam",
              "review", "compliance", "cicd", "release", "infrastructure", "incident", "performance", "migration",
              "data-analysis", "docs", "lookup", "i18n", "dependencies", "accessibility", "sso", "design-system",
              "codebase", "backup")
PROFILE = obj({"model": TEXT, "review_model": TEXT, "model_reasoning_effort": EFFORT,
               "plan_mode_reasoning_effort": EFFORT, "sandbox_mode": SANDBOX, "approval_policy": APPROVAL})
PROFILES = mapping(TOKEN.check, PROFILE)
JOB = obj({"cmd": Rule(lambda v: isinstance(v, list) and bool(v) and all(NONEMPTY.check(x) for x in v),
                       "a nonempty list of command arguments"),
           "every": POSINT, "timeout": number(positive=True),
           "at": pattern(r"(?:[01]?\d|2[0-3]):[0-5]\d", "HH:MM (24-hour clock)"),
           "weekly": pattern(r"(?:sun|mon|tue|wed|thu|fri|sat) (?:[01]?\d|2[0-3]):[0-5]\d",
                             "a weekday and HH:MM"),
           "notify": enum("failure", "always", "never"), "enabled": BOOL, "about": TEXT, "run_at_load": BOOL})
BUILTIN_JOBS = ("sync", "sleep", "gc", "repair", "audit", "update-check", "doctor", "lessons")


def regex_ok(value):
    if not isinstance(value, str):
        return False
    try:
        re.compile(value)
        return True
    except (re.error, OverflowError, RecursionError):
        return False


REGEX = Rule(regex_ok, "a valid regular expression")


SKILL_RULE = obj({"id": TOKEN, "why": TEXT, "group": enum(*GROUPS), "files": array(NONEMPTY),
                  "commands": array(NONEMPTY), "activities": array(enum(*ACTIVITIES)), "content": REGEX,
                  "content_files": array(NONEMPTY), "content_exclude": array(NONEMPTY),
                  "project": obj({"files": array(NONEMPTY), "content": REGEX}),
                  "skills": obj({"claude": array(NONEMPTY), "codex": array(NONEMPTY)})}, required=("id",))
EXT_COMMON = {"kind": enum("plugin", "skill", "mcp"), "license": NONEMPTY, "why": NONEMPTY}
EXT_NAME = pattern(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", "an extension name without path separators")
EXT_ID = pattern(r"(?:plugin:(?:claude|codex):[\w.][\w.-]*@[\w.][\w.-]*|"
                 r"(?:skill|mcp):(?:claude|codex):[A-Za-z0-9_][A-Za-z0-9_.-]*)", "an extension catalog ID")
EXT_RECORDS = {
    "plugin": obj({**EXT_COMMON, "agent": enum("claude", "codex"),
                   "id": pattern(r"[\w.][\w.-]*@[\w.][\w.-]*", "name@marketplace")},
                  required=("kind", "license", "why", "agent", "id")),
    "skill": obj({**EXT_COMMON, "name": EXT_NAME, "agents": array(enum("claude", "codex")),
                  "repo": pattern(r"[\w.-]+/[\w.-]+", "owner/repository"),
                  "ref": pattern(r"[a-fA-F0-9]{40}", "a full commit SHA"),
                  "path": Rule(lambda v: isinstance(v, str) and bool(v) and not v.startswith("/")
                               and ".." not in v.split("/"), "a relative repository path")},
                 required=("kind", "license", "why", "name", "repo", "ref", "path")),
    "mcp": obj({**EXT_COMMON, "name": EXT_NAME, "agents": array(enum("claude", "codex")),
                "command": NONEMPTY, "args": array(TEXT), "url": NONEMPTY,
                "env": mapping(lambda k: bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)), TEXT)},
               required=("kind", "license", "why", "name")),
}


def _default_rule(value):
    if isinstance(value, dict):
        return obj({k: _default_rule(v) for k, v in value.items()})
    if isinstance(value, bool):
        return BOOL
    if isinstance(value, (int, float)):
        return number()
    if isinstance(value, list):
        return array(TEXT)
    return TEXT


SCHEMA = {k: _default_rule(v) for k, v in DEFAULTS.items()}
SCHEMA.update({
    "dev_root": PATH, "handoff_dir": Rule(lambda v: v == "" or PATH.check(v), "empty or an absolute path"),
    "language": pattern(r"(?i:auto|[a-z]{2,3}(?:[-_][a-z]{2,4})?)", "auto or a locale such as en-US"),
    "default_host": HOST,
    "host_dirs": mapping(HOST.check, pattern(r"[A-Za-z0-9][A-Za-z0-9._-]*", "a relative folder alias")),
    "stacks": array(enum(*STACKS)), "frameworks": array(enum(*FRAMEWORKS)),
    "hosts": array(enum(*HOSTS)), "clouds": array(enum(*CLOUDS)),
    "sync": obj({"push": BOOL, "interval_minutes": POSINT}),
    "docker": obj({"cpu": POSINT, "memory": POSINT, "disk": POSINT, "vm_type": enum("vz", "qemu"),
                   "mount_type": enum("virtiofs", "sshfs", "9p"), "rosetta": BOOL, "autostart": BOOL}),
    "claude": obj({"default_mode": enum("bypassPermissions", "acceptEdits", "default"),
                   "subagent_model": TEXT, "subagent_effort": EFFORT,
                   "fast_mode_per_session": BOOL, "statusline": BOOL}),
    "codex": obj({"mode": enum("full", "workspace"), "approval_policy": APPROVAL, "sandbox_mode": SANDBOX,
                  "max_subagent_threads": POSINT, "subagent_model": TEXT, "subagent_effort": EFFORT,
                  "profiles": PROFILES}),
    "skills": obj({"groups": array(enum("all", *GROUPS)), "optional": mapping(lambda k: k in GROUPS,
                   array(enum(*CLOUDS))), "enforce": BOOL, "rules": array(SKILL_RULE)}),
    "ext": obj({"add": array(Rule()), "remove": array(EXT_ID), "disable": array(EXT_ID),
                "enable": array(EXT_ID)}),
    "progress": obj({k: Rule(check, expected) for k, (check, expected) in PROGRESS_RULES.items()}),
    "work": obj({"lock_hours": number(positive=True), "mode": enum("auto", "guided")}),
    "crew": obj({"gates": obj({"enforce": BOOL}), "route_hint": BOOL}),
    "handoff": obj({"enforce": BOOL, "block_after_commits": number(whole=True),
                    "block_after_minutes": number()}),
    "canary": obj({"enforce": BOOL, "roles": BOOL, "language": BOOL}),
    "dashboard": obj({"idle_minutes": number(positive=True)}),
    "backup": obj({"auto": BOOL, "after_minutes": number(0, 1440)}),
    "bugs": obj({"share": enum(None, "on", "reports", "off"), "sync": BOOL}),
    "ai": obj({"enabled": BOOL, "asked": BOOL, "agent": enum("", "claude", "codex"),
               "model": obj({"claude": TEXT, "codex": TEXT}), "daily_calls": number(whole=True),
               "timeout": number(positive=True)}),
    "branches": obj({"types": array(TOKEN), "forbidden_words": array(TOKEN)}),
    "commits": obj({"types": array(TOKEN)}),
    "gitx": obj({"github_hosts": array(HOST)}), "delivery": obj({"draft_pr": BOOL}),
    "jobs": mapping(lambda k: bool(re.fullmatch(r"[a-z0-9][a-z0-9-]*", k)), JOB),
})
_audit = {}
for _path, (_check, _expected) in AUDIT_RULES.items():
    if len(_path) == 1:
        _audit[_path[0]] = Rule(_check, _expected)
    else:
        _audit.setdefault(_path[0], {})[_path[1]] = Rule(_check, _expected)
SCHEMA["audit"] = obj({k: obj(v) if isinstance(v, dict) else v for k, v in _audit.items()})
SCHEMA["audit"].children["redact"].children["extra"] = array(
    Rule(lambda value: bool(value) and regex_ok(value), "a nonempty valid regular expression"))
SCHEMA["progress"].children["quota_steps"] = mapping(
    lambda k: bool(re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)", k)) and 0 <= float(k) <= 100, number(0, 1))
SCHEMA["progress"].children["slot_weights"] = obj({k: number(0, 1) for k in SLOT_KINDS})
ROOT = obj(SCHEMA)

# Top-level keys an earlier devkit version read and this one ignores: a configuration that still holds one is valid,
# and the next configuration change removes it. `safeguards` switched the command guard, which devkit no longer has.
RETIRED = frozenset({"safeguards"})


def retired_keys(raw) -> list:
    """The retired top-level keys a saved configuration document still holds, sorted."""
    return sorted(k for k in raw if k in RETIRED) if isinstance(raw, dict) else []


def without_retired(raw):
    """The document as every configuration writer saves it: without its retired keys."""
    return {k: v for k, v in raw.items() if k not in RETIRED} if isinstance(raw, dict) else raw


def retired_notes(raw) -> list:
    """One line per retired key, as `dev config validate` and `dev doctor` show it."""
    return [f"retired setting {k} is ignored; the next configuration change removes it" for k in retired_keys(raw)]


def _walk(value, rule, path, errors):
    if path == "codex.profiles" and value is False:
        return False
    if re.fullmatch(r"ext\.add\[\d+\]", path):
        if not isinstance(value, dict) or not isinstance(value.get("kind"), str) or value["kind"] not in EXT_RECORDS:
            errors.append(f"{path}.kind must be plugin, skill or mcp")
            return None
        rule = EXT_RECORDS[value["kind"]]
    if rule.object_type:
        if not isinstance(value, dict):
            errors.append(f"{path or 'configuration'} must be an object")
            return None
        out = {}
        for name in sorted(value, key=str):
            if not path and name in RETIRED:
                continue
            child = f"{path}.{name}" if path else str(name)
            if not isinstance(name, str) or (rule.values is not None and not rule.key(name)):
                errors.append(f"{child}: invalid map key")
                continue
            sub = rule.children.get(name) if rule.values is None else rule.values
            if sub is None:
                errors.append(f"unknown key {child}")
                if child == "agents.max_parallel":
                    errors.append("agents.max_parallel is unsupported; use progress.max_parallel, "
                                  "then remove the invalid key with dev config edit")
                continue
            count = len(errors)
            result = _walk(value[name], sub, child, errors)
            if len(errors) == count or (isinstance(result, dict) and rule.values is None):
                out[name] = result
        for name in rule.required:
            if name not in value:
                errors.append(f"{path}.{name} is required")
        if path.startswith("jobs.") and path.count(".") == 1:
            if sum(k in value for k in ("every", "at", "weekly")) > 1:
                errors.append(f"{path} must select only one schedule")
            if path.split(".")[1] not in BUILTIN_JOBS and "cmd" not in value:
                errors.append(f"{path}.cmd is required for a custom job")
        if re.fullmatch(r"ext\.add\[\d+\]", path) and value.get("kind") == "mcp":
            if "url" in value and ("command" in value or "args" in value):
                errors.append(f"{path} must select a URL or a pinned local command")
            try:
                mcp_pin(path, value)
            except (SchemaError, TypeError, ValueError):
                errors.append(f"{path} must use HTTPS or an exactly pinned npx/uvx package without code-loading env")
        return out
    if not rule.check(value):
        errors.append(f"{path} must be {rule.expected}")
        return None
    if isinstance(value, float) and not math.isfinite(value):
        errors.append(f"{path} must be finite")
        return None
    if rule.items:
        return [_walk(v, rule.items, f"{path}[{i}]", errors) for i, v in enumerate(value)]
    return copy.deepcopy(value)


def validate(raw):
    """Return deterministic errors for the complete sparse saved document."""
    errors = []
    _walk(raw, ROOT, "", errors)
    return errors


_UNKNOWN = "unknown key "


def unknown_key(error: str) -> str:
    """The dotted path an "unknown key" error names, or "" for any other error."""
    return error[len(_UNKNOWN):] if isinstance(error, str) and error.startswith(_UNKNOWN) else ""


def describe(errors) -> list:
    """The errors as a person reads them: an unknown key names the command that removes it."""
    return [f"{e}: devkit does not read it; remove it with dev config edit" if unknown_key(e) else e
            for e in errors]


def _lookup(doc, path: str):
    for part in path.split("."):
        if not isinstance(doc, dict) or part not in doc:
            return _MISSING
        doc = doc[part]
    return doc


_MISSING = object()


def tolerated_unknown(error: str, before, candidate) -> bool:
    """Whether a writer may save past this error: an unknown key that the saved document already held with the same
    value, so the writer neither added nor changed it (a key another devkit version wrote, or one this schema no
    longer reads). The saved configuration stays invalid until it is removed with `dev config edit`."""
    path = unknown_key(error)
    if not path:
        return False
    old = _lookup(before, path)
    return old is not _MISSING and old == _lookup(candidate, path)


def _merge(base, extra, path=""):
    out = copy.deepcopy(base)
    for key, value in extra.items():
        child = f"{path}.{key}" if path else key
        if child == "skills.optional" and value == {}:
            out[key] = {}
        elif isinstance(value, dict) and isinstance(out.get(key), dict) and child != "codex.profiles":
            out[key] = _merge(out[key], value, child)
        else:
            out[key] = copy.deepcopy(value)
    return out


def effective(raw):
    """Return safe effective values and diagnostics without modifying saved overrides."""
    errors = []
    valid = _walk(raw, ROOT, "", errors)
    return _merge(DEFAULTS, valid or {}), errors


def set_value(raw, key, value):
    """Replace a supported dotted path in a copy, then validate the entire candidate."""
    parts = key.split(".") if isinstance(key, str) else []
    if not parts or any(not p or p.strip() != p for p in parts):
        raise SchemaError("configuration path must have nonempty segments without surrounding whitespace")
    candidate = copy.deepcopy(raw)
    if not isinstance(candidate, dict):
        raise SchemaError("configuration must be an object")
    cursor, rule = candidate, ROOT
    for i, part in enumerate(parts):
        sub = rule.children.get(part) if rule.values is None else rule.values if rule.key(part) else None
        if sub is None:
            raise SchemaError(f"unknown key {'.'.join(parts[:i + 1])}")
        if i == len(parts) - 1:
            cursor[part] = copy.deepcopy(value)
        else:
            if not sub.object_type or (part in cursor and not isinstance(cursor[part], dict)):
                raise SchemaError(f"{'.'.join(parts[:i + 1])} must be an object to set a child")
            cursor = cursor.setdefault(part, {})
        rule = sub
    errors = [e for e in validate(candidate) if not tolerated_unknown(e, raw, candidate)]
    if errors:
        raise SchemaError("; ".join(describe(errors)))
    return candidate


def known_paths():
    """Registered fixed paths, with <name> marking an intentional map entry."""
    out = []
    def visit(rule, path):
        if path:
            out.append(path)
        for name, child in rule.children.items():
            visit(child, f"{path}.{name}" if path else name)
        if rule.values:
            visit(rule.values, path + ".<name>")
    visit(ROOT, "")
    return tuple(sorted(out))


def loads(text):
    """Parse strict JSON without duplicate object keys or nonfinite numbers."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise SchemaError("duplicate JSON object key")
            result[key] = value
        return result
    def finite(text):
        value = float(text)
        if not math.isfinite(value):
            raise SchemaError("JSON numbers must be finite")
        return value
    def constant(text):
        raise SchemaError("JSON numbers must be finite")
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_float=finite, parse_constant=constant)
    except (ValueError, TypeError, RecursionError) as exc:
        raise SchemaError("invalid configuration JSON: " + str(exc)) from None

