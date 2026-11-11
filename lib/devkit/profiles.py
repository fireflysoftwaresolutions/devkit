# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Saved language and framework choices shared by setup, extensions and diagnostics.

An absent frameworks key retains legacy framework capabilities. An explicit list,
including an empty one, selects capabilities without installing project libraries.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from . import core


@lru_cache(maxsize=1)
def catalog() -> dict:
    """The versioned capability map shipped with this copy of devkit."""
    return json.loads((Path(__file__).resolve().parents[2] / "config" / "profiles.json").read_text())


def resolve(cfg=None) -> dict:
    """Resolve implied runtimes and package groups without mutating saved configuration."""
    cfg = core.CFG if cfg is None else cfg
    definitions = catalog()["frameworks"]
    legacy = "frameworks" not in cfg
    chosen = list(definitions) if legacy else cfg["frameworks"]
    if not isinstance(chosen, list) or any(not isinstance(f, str) or f not in definitions for f in chosen):
        raise ValueError("frameworks must be a list of supported IDs: " + ", ".join(definitions))
    frameworks = list(dict.fromkeys(chosen))
    stacks = list(dict.fromkeys(cfg.get("stacks", core.DEFAULTS["stacks"]) or []))
    implied = []
    if not legacy:
        for f in frameworks:
            stack = definitions[f]["stack"]
            if stack not in stacks:
                stacks.append(stack)
                implied.append(stack)
    groups = list(stacks)
    for f in frameworks:
        # Historically ng was part of Node, even though framework skills and MCPs
        # were installed independently of language choices.
        if not legacy or definitions[f]["stack"] in stacks:
            groups.extend(g for g in definitions[f].get("package_groups", []) if g not in groups)
    return {"legacy_frameworks": legacy, "stacks": stacks, "frameworks": frameworks,
            "package_groups": groups, "implied_stacks": implied}


def extension_selected(cfg, kind: str, name: str, agent: str, explicit=False, required=False) -> bool:
    """Apply profile recommendations after validating the existing extension overlay.

Explicit or required entries win recommendations. Neither enables a disabled agent.
Unmapped extensions retain their existing behavior and trust requirements.
"""
    cfg = core.CFG if cfg is None else cfg
    profile = resolve(cfg)
    if not (cfg.get("agents") or {}).get(agent, True):
        return False
    if explicit or required:
        return True
    name = name.split("@", 1)[0] if kind == "plugin" else name
    matched = False
    for family in ("stacks", "frameworks"):
        for key, definition in catalog()[family].items():
            if name in definition.get("extensions", {}).get(kind, []):
                matched = True
                if key in profile[family]:
                    return True
    return not matched


def suggested_frameworks(detected_ids) -> list:
    """Supported framework suggestions from read-only project metadata, never commands."""
    return list(dict.fromkeys(f for f in detected_ids if f in catalog()["frameworks"]))
