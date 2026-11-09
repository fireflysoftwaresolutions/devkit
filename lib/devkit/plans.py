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


def _read(text: str):
    """-> (frontmatter fields, the frontmatter's other lines as they are, body, changelog entries). Only the record's
    own changelog at the end is taken out of the body (work.split_changelog), so a `## Changelog` in a code example or
    a section stays in the body."""
    front, body = work.split_frontmatter(text)
    meta, extra = work.read_frontmatter(front, FIELDS, ())
    body, changelog = work.split_changelog(body)
    return meta, extra, body.strip("\n"), changelog


def _split(text: str):
    """-> (frontmatter fields, body, changelog entries)."""
    meta, _, body, changelog = _read(text)
    return meta, body, changelog


def _sha(body: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", body).strip().encode()).hexdigest()[:16]


def _render(meta: dict, body: str, changelog: list, extra=()) -> str:
    head = "---\n" + "\n".join([f"{k}: {meta.get(k, '')}".rstrip() for k in FIELDS] + list(extra)) + "\n---\n"
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
    work.write_record(path, _render(meta, body, log))
    work._commit([path], f"{meta['id']}: plan “{title[:60]}” created ({work.actor_str(a)})")
    return path


def revise(path: Path, why: str, a=None, status: str = None) -> dict:
    """Record a change of the plan: bump the version, log who/when/why and the size of the change, commit. An
    approval covers the text approved: an approved plan whose text changed goes back to draft when it is revised
    without a `status`, and the changelog line names the version the approval covered."""
    if not why.strip():
        raise ValueError("a plan revision needs the reason: --why \"…\"")
    a = a or work.actor()
    path = Path(path)
    with work.record_lock(path):  # another session revising this plan waits, then reads what this one wrote
        meta = _revise_locked(path, why, a, status)
    work._commit([path], f"{meta['id']}: {meta['_change']} v{meta['version']} ({work.actor_str(a)}) — {why}")
    meta.pop("_change", None)
    return meta


def _revise_locked(path: Path, why: str, a: dict, status) -> dict:
    """revise's read through write, under the plan's record lock. -> the new frontmatter, with `_change` for the
    commit message."""
    text = path.read_text()
    meta, extra, body, log = _read(text)
    adopted = not meta.get("id")
    if adopted:  # not devkit's frontmatter: another tool's, or a first `---` rule, stays at the top of the body
        body, log = work.split_changelog(text)
        body, extra = body.strip("\n"), []
        title = next((ln[2:].strip() for ln in body.splitlines() if ln.startswith("# ")), path.stem)
        meta = {"id": "P-" + work.new_id("task")[2:], "title": title, "scope": scope_of(path), "status": "draft",
                "version": 0, "created": work.now_iso(), "created_by": f"{a['user']}@{a['host']}"}
    unapproved = ""
    if status:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        meta["status"] = status
    elif meta.get("status") == "approved" and meta.get("body_sha") != _sha(body):
        unapproved = f"; back to draft: the approval covered v{int(meta.get('version') or 0)}"
        meta["status"] = "draft"
    rel = str(path.relative_to(HANDOFF))
    stat = core.gitout(HANDOFF, "diff", "--shortstat", "HEAD", "--", rel) if not adopted else ""
    meta["version"] = int(meta.get("version") or 0) + 1
    meta["body_sha"] = _sha(body)
    meta["updated"], meta["updated_by"] = work.now_iso(), f"{a['user']}@{a['host']}"
    change = "adopted into devkit format" if adopted else (status or "revised") + unapproved
    detail = f" ({stat.strip()})" if stat.strip() else ""
    log.insert(0, f"v{meta['version']} · {work.now_iso()} · {work.actor_str(a)} · {change}{detail} — why: {why}")
    work.write_record(path, _render(meta, body, log, extra))
    return dict(meta, _change=change)


def list_plans(scope: str = None) -> list:
    base = PLANS / scope if scope else PLANS
    out = []
    for f in sorted(base.rglob("*.md")) if base.exists() else []:
        meta, body, log = _split(f.read_text())
        out.append({"path": f, "id": meta.get("id", "—"), "title": meta.get("title") or f.stem,
                    "status": meta.get("status", "unversioned"), "version": meta.get("version", "-"),
                    "updated": meta.get("updated", ""), "needs_revision": needs_revision(f), "changelog": log})
    return out
