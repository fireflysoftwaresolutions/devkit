# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Pure decisions over trusted, normalized authorization facts.

Adapters must obtain these facts from authenticated session/role/step state,
validated configuration and the operation classifier, never from tool arguments
that assert their own authority. This module performs no lookup, I/O or grant
consumption. Its result is a decision for this operation, not a bearer capability.

Required caller fields: session, kind (main/member), role, authenticated (bool),
and lane (None for main). Members also need step, live_step and delegated_actions.
The adapter's authenticated verdict includes current registration, not merely a
historically valid session ID. Its scope verdict includes the current role and
ownership checks, including supported cross-project scope.

Required context fields: config_valid, registry {action, tier}, scope {allowed,
repo, target, account}, readiness {allowed}, prohibitions (list of reason codes),
account and account_resolved. The registry entry comes from the trusted action
catalog; an unclassified operation has no entry. Account identifies the exact
remote account (or the authenticated local account for local operations). Target
identifies the exact resource; a tool name alone cannot identify a remote write.
All positive verdicts must be literal True. Explicit policy-change operations
must have applicable prohibitions resolved by their supported adapter; automatic
mode never removes a prohibition itself.

An `unknown` operation is the classifier's explicit verdict on a program it cannot
see through; its target is the command digest (cmd- and ten hex digits). Automatic
mode authorizes it for the main session only. The developer's own decisions
(developer-only, claim-break) and any operation whose context sets policy_eligible
to anything but True need human authority in both modes: the result is
require-human, never allow, so the adapter looks for a typed grant.

mode_source is config, default, run, or legacy. The digest binds these inputs and
the policy revision for audit/revalidation; it is not a signature or permission.
Callers must recompute against fresh trusted state before execution.
"""
import hashlib
import json
import re


_POLICY_REVISION = 1
_UNCLASSIFIABLE = frozenset(("unparsed", "registry-write"))
_HUMAN_ONLY = frozenset(("developer-only", "claim-break"))  # the developer's own decisions, never policy
_UNKNOWN_TARGET = re.compile(r"cmd-[0-9a-f]{10}\Z")
_MEMBER_ACTIONS = frozenset(("push", "pr"))
_ACTION = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_REPO_PART = re.compile(r"[A-Za-z0-9_.-]+\Z")


def _exact(value: object) -> bool:
    """Accept a resolved identifier, never a placeholder or expansion."""
    return (isinstance(value, str) and bool(value) and value == value.strip()
            and value not in ("?", "unknown", ".", "..")
            and not any(char in value for char in ("*", "$", "`", "\x00", "{}"))
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _repository(value: object) -> bool:
    if not _exact(value):
        return False
    parts = value.split("/")
    return len(parts) >= 2 and all(part not in (".", "..") and _REPO_PART.fullmatch(part) for part in parts)


def decide(*, mode: str, mode_source: str, action: str, target: str,
           repo: str, caller: dict, context: dict) -> dict:
    """Return allow, require-human or deny after all mandatory integrity checks.

    Reasons are fixed diagnostics: no configuration values, prohibition text or
    caller payload is interpolated into them. The exact operation is returned
    separately so the adapter can apply its existing audit redaction rules.
    """
    inputs = {"revision": _POLICY_REVISION, "mode": mode, "mode_source": mode_source,
              "action": action, "target": target, "repo": repo, "caller": caller, "context": context}
    try:
        encoded = json.dumps(inputs, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                             allow_nan=False).encode("ascii")
    except (TypeError, ValueError, RecursionError):
        # Invalid non-JSON facts cannot authorize. This digest intentionally binds
        # only the refusal class; it must never be used as evidence for execution.
        encoded = b"devkit.authorization:invalid-input:v1"
        serializable = False
    else:
        serializable = True
    digest = hashlib.sha256(encoded).hexdigest()

    def result(outcome, source, reason):
        return {"outcome": outcome, "source": source, "reason": reason,
                "action": action, "target": target, "repo": repo, "policy_digest": digest}

    def deny(reason):
        return result("deny", "integrity", reason)

    if not serializable or not isinstance(context, dict) or not isinstance(caller, dict):
        return deny("Invalid authorization context; rebuild trusted normalized facts.")
    if mode not in ("auto", "guided") or mode_source not in ("config", "default", "run", "legacy"):
        return deny("Invalid mode or mode origin; repair configuration or the run override.")
    if context.get("config_valid") is not True:
        return deny("Invalid policy configuration; repair configuration before retrying.")

    registry = context.get("registry")
    if (not isinstance(action, str) or not _ACTION.fullmatch(action)
            or action in _UNCLASSIFIABLE or not _exact(target) or not _repository(repo)
            or (action == "unknown" and not _UNKNOWN_TARGET.fullmatch(target))
            or not isinstance(registry, dict) or registry.get("action") != action
            or type(registry.get("tier")) is not int or registry["tier"] not in (1, 2)):
        return deny("Unresolved or unsupported operation; use an exactly classified action, target and repository.")

    if (caller.get("authenticated") is not True or not _exact(caller.get("session"))
            or not _exact(caller.get("role")) or caller.get("kind") not in ("main", "member")
            or "lane" not in caller or (caller["kind"] == "main" and caller["lane"] is not None)):
        return deny("Unresolved caller; restore authenticated current session and role registration.")

    scope = context.get("scope")
    if (context.get("account_resolved") is not True or not _exact(context.get("account"))
            or (isinstance(scope, dict) and scope.get("account") != context["account"])):
        return deny("Unresolved account scope; bind the exact authenticated account and resource.")
    if (not isinstance(scope, dict) or scope.get("allowed") is not True
            or scope.get("repo") != repo or scope.get("target") != target):
        return deny("Unresolved or disallowed scope; use the caller's current role and assigned operation scope.")

    prohibitions = context.get("prohibitions")
    if not isinstance(prohibitions, list) or prohibitions:
        return deny("An explicit prohibition or unresolved prohibition check prevents this operation.")
    readiness = context.get("readiness")
    if not isinstance(readiness, dict) or readiness.get("allowed") is not True:
        return deny("Operation readiness is unverified; complete the required checks before retrying.")

    if caller["kind"] == "member":
        if registry["tier"] != 1 or action not in _MEMBER_ACTIONS:
            return deny("Members must route this exact operation to the tech lead for execution.")
        delegated = caller.get("delegated_actions")
        if (not _exact(caller.get("lane")) or not _exact(caller.get("step"))
                or caller.get("live_step") is not True or not isinstance(delegated, list)
                or any(item not in _MEMBER_ACTIONS for item in delegated if isinstance(item, str))
                or any(not isinstance(item, str) for item in delegated) or action not in delegated):
            return deny("A member needs a live assigned step and explicit action delegation within its scope.")

    if mode == "auto" and action not in _HUMAN_ONLY and context.get("policy_eligible", True) is True:
        return result("allow", "policy-auto", "Automatic policy authorizes this exact operation.")
    if mode == "auto":
        return result("require-human", "human-only",
                      "Automatic policy does not authorize this operation; it needs the developer's typed grant.")
    return result("require-human", "guided", "Guided mode requires scoped human authorization.")
