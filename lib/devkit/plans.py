# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Versioned plans and specs: plans/<scope>/<slug>.md with frontmatter, a body hash and a Changelog.

Any edit to a plan's body without `dev plan revise --why` is detected (`needs_revision`), and the Stop hook
asks the agent to record the reason before finishing. Raw plans (for example written by Superpowers) are
adopted into this format on their first revision.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from . import core, work
from .core import HANDOFF

PLANS = HANDOFF / "plans"
STATUSES = ("draft", "approved", "superseded")
FIELDS = ("id", "title", "kind", "scope", "status", "version", "body_sha", "created", "created_by", "updated", "updated_by",
          "supersedes")


def _split(text: str):
    meta, body = {}, text
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            for ln in text[4:end].splitlines():
                k, _, v = ln.partition(":")
                meta[k.strip()] = v.strip()
            body = text[end + 5:]
    changelog = []
    if "\n## Changelog" in body:
        body, _, log = body.partition("\n## Changelog")
        changelog = [ln[2:].strip() for ln in log.splitlines() if ln.startswith("- ")]
    return meta, body.strip("\n"), changelog


def _sha(body: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", body).strip().encode()).hexdigest()[:16]


def _render(meta: dict, body: str, changelog: list) -> str:
    head = "---\n" + "\n".join(f"{k}: {meta.get(k, '')}".rstrip() for k in FIELDS) + "\n---\n"
    return head + body.rstrip() + "\n\n## Changelog\n" + "".join(f"- {c}\n" for c in changelog)


def needs_revision(path: Path) -> bool:
    try:
        meta, body, _ = _split(Path(path).read_text())
    except OSError:
        return False
    return not meta.get("body_sha") or meta["body_sha"] != _sha(body)


def scope_of(path: Path) -> str:
    rel = Path(path).resolve().relative_to(PLANS.resolve())
    return str(rel.parent)


KINDS = ("plan", "spec", "spike", "bugfix", "adr")


def template(kind: str, title: str) -> str:
    f = core.DEVKIT_HOME / "templates" / "plans" / f"{kind}.md"
    if not f.exists():
        raise ValueError(f"unknown plan kind “{kind}”: {', '.join(KINDS)}")
    return f.read_text().replace("{{title}}", title).replace("{{date}}", __import__("time").strftime("%Y-%m-%d")).strip()


def new(scope: str, title: str, a=None, kind: str = "plan") -> Path:
    a = a or work.actor()
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
    path = PLANS / scope / f"{__import__('time').strftime('%Y-%m-%d')}-{slug}.md"
    if path.exists():
        raise ValueError(f"{path.name} already exists — `dev plan revise` it, or choose another title")
    path.parent.mkdir(parents=True, exist_ok=True)
    body = template(kind, title)
    meta = {"id": "P-" + work.new_id("task")[2:], "title": title, "kind": kind, "scope": scope, "status": "draft", "version": 1,
            "body_sha": _sha(body), "created": work.now_iso(), "created_by": f"{a['user']}@{a['host']}",
            "updated": work.now_iso(), "updated_by": f"{a['user']}@{a['host']}"}
    log = [f"v1 · {work.now_iso()} · {work.actor_str(a)} · created from the {kind} template"]
    path.write_text(_render(meta, body, log))
    work._commit([path], f"{meta['id']}: plan “{title[:60]}” created ({work.actor_str(a)})")
    return path


def revise(path: Path, why: str, a=None, status: str = None) -> dict:
    """Record a change of the plan: bump the version, log who/when/why and the size of the change, commit."""
    if not why.strip():
        raise ValueError("a plan revision needs the reason: --why \"…\"")
    a = a or work.actor()
    path = Path(path)
    meta, body, log = _split(path.read_text())
    adopted = not meta.get("id")
    if adopted:
        title = next((ln[2:].strip() for ln in body.splitlines() if ln.startswith("# ")), path.stem)
        meta = {"id": "P-" + work.new_id("task")[2:], "title": title, "scope": scope_of(path), "status": "draft",
                "version": 0, "created": work.now_iso(), "created_by": f"{a['user']}@{a['host']}"}
    if status:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        meta["status"] = status
    rel = str(path.relative_to(HANDOFF))
    stat = core.gitout(HANDOFF, "diff", "--shortstat", "HEAD", "--", rel) if not adopted else ""
    meta["version"] = int(meta.get("version") or 0) + 1
    meta["body_sha"] = _sha(body)
    meta["updated"], meta["updated_by"] = work.now_iso(), f"{a['user']}@{a['host']}"
    change = "adopted into devkit format" if adopted else (status or "revised")
    detail = f" ({stat.strip()})" if stat.strip() else ""
    log.insert(0, f"v{meta['version']} · {work.now_iso()} · {work.actor_str(a)} · {change}{detail} — why: {why}")
    path.write_text(_render(meta, body, log))
    work._commit([path], f"{meta['id']}: {change} v{meta['version']} ({work.actor_str(a)}) — {why}")
    return meta


def list_plans(scope: str = None) -> list:
    base = PLANS / scope if scope else PLANS
    out = []
    for f in sorted(base.rglob("*.md")) if base.exists() else []:
        meta, body, log = _split(f.read_text())
        out.append({"path": f, "id": meta.get("id", "—"), "title": meta.get("title") or f.stem,
                    "status": meta.get("status", "unversioned"), "version": meta.get("version", "-"),
                    "updated": meta.get("updated", ""), "needs_revision": needs_revision(f), "changelog": log})
    return out
