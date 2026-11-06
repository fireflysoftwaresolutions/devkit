# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""`dev handoff-import <staging dir> --slug <slug>`: adopt an existing handoff/programme repository into the handoff standard.

A staging folder (produced by an analysis of the old repository) contains:
  README.md      the initiative note in devkit's format (frontmatter with projects/source, sections, Tasks)
  source.md      provenance and a complete index of the old repository (permalinks at a pinned SHA)
  docs.txt       key documents to import verbatim: "<repo>/<path>" per line (fetched at the pinned SHA)
  projects.json  member projects: [{"id", "role", "state", "now", "next", ...}]
  report.md      the migration analysis (confidence, overlaps, inconsistencies, secrets) → MIGRATION.md

The import creates initiatives/<slug>/{README.md, source.md, docs/…}, one versioned work item per task (tasks
tagged `[owner/repo]` go to that project's board, linked to the initiative), a note for every member project that
has none, a session-log line and an operations-log entry. The old repository is not touched.
"""
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path

from . import core, notes, ui, work
from .core import HANDOFF, NOTES


def _fetch(repo: str, sha: str, path: str):
    rc, out, err = core.sh(["gh", "api", f"repos/{repo}/contents/{path}?ref={sha}", "--jq", ".content"], timeout=60)
    if rc != 0 or not out:
        return None
    try:
        return base64.b64decode(out)
    except ValueError:
        return None


def _sources(meta: dict) -> dict:
    """source: [owner/repo@sha12, …] -> {repo short name: (owner/repo, sha)}; resolves short SHAs via gh."""
    out = {}
    for s in meta.get("source") or []:
        repo, _, sha = s.partition("@")
        if len(sha) < 40:
            rc, full, _ = core.sh(["gh", "api", f"repos/{repo}/commits/{sha}", "--jq", ".sha"], timeout=30)
            sha = full if rc == 0 and full else sha
        out[repo.split("/")[-1]] = (repo, sha)
    return out


def run(staging: Path, slug: str, dry_run: bool = False) -> int:
    staging = Path(staging).expanduser()
    readme = staging / "README.md"
    if not readme.exists():
        ui.err(f"{staging}: no README.md (initiative note)")
        return 2
    text = readme.read_text()
    meta, _ = notes.split_frontmatter(text)
    if meta.get("kind") != "initiative":
        ui.err("README.md is not an initiative note (frontmatter kind: initiative)")
        return 2
    dest = HANDOFF / "initiatives" / slug
    if (dest / "README.md").exists():
        ui.err(f"initiative {slug} already exists: {core.tilde(dest)}")
        return 2
    members = [p.lower() for p in (meta.get("projects") or [])]
    legacy = notes.tasks(text)
    plist = json.loads((staging / "projects.json").read_text()) if (staging / "projects.json").exists() else []
    docs = [ln.strip() for ln in (staging / "docs.txt").read_text().splitlines()
            if ln.strip() and not ln.startswith("#")] if (staging / "docs.txt").exists() else []
    print(ui.section(f"Import {meta.get('title') or slug} → initiatives/{slug}"))
    print(ui.kv("members", ", ".join(members) or "-"))
    print(ui.kv("tasks", f"{len(legacy)} ({sum(1 for t in legacy if t['done'])} done)"))
    print(ui.kv("documents", str(len(docs))))
    if dry_run:
        return 0
    os.environ["DEVKIT_NO_AUTOSYNC"] = "1"
    a = work.actor("devkit")
    dest.mkdir(parents=True, exist_ok=True)
    srcs = _sources(meta)
    # 1. the note (its Tasks section becomes the generated block on the next sync)
    (dest / "README.md").write_text(text)
    for name, target in (("source.md", "source.md"), ("report.md", "MIGRATION.md")):
        if (staging / name).exists():
            (dest / target).write_text((staging / name).read_text())
    # 2. documents at the pinned SHA
    fetched = 0
    for d in docs:
        repo_short, _, path = d.partition("/")
        if repo_short not in srcs:  # lines without a repo prefix: the only source
            repo_short, path = next(iter(srcs)), d
        repo, sha = srcs[repo_short]
        data = _fetch(repo, sha, path)
        if data is None:
            ui.warn(f"could not fetch {repo}@{sha[:12]}:{path}")
            continue
        out = dest / "docs" / repo_short / path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        fetched += 1
    # 3. work items
    prefix = re.compile(r"^`?\[([\w.-]+/[\w.-]+)\]`?\s*(.+)$")
    suffix = re.compile(r"^(.+?)\s*`?\[([\w.-]+/[\w.-]+)\]`?\s*$")
    for t in legacy:
        title = t["text"].rsplit(" ✓ ", 1)[0].strip()
        scope = f"initiatives/{slug}"
        m1, m2 = prefix.match(title), suffix.match(title)
        if m1 and m1.group(1).lower() in members:
            scope, title = m1.group(1).lower(), m1.group(2)
        elif m2 and m2.group(2).lower() in members:
            scope, title = m2.group(2).lower(), m2.group(1)
        bucket = t["bucket"].lower() if t["bucket"] != "Done" else "next"
        it = work.create(scope, title, bucket=bucket, a=a, links=[f"initiative:{slug}"],
                         why=f"imported from {', '.join(meta.get('source') or ['the old handoff'])}")
        if t["done"]:
            work.update(it, "done", a=a, status="done")
        elif t.get("state") == "blocked":
            work.update(it, "block", a=a, status="blocked", why="blocked in the old handoff")
    # 4. member project notes
    by_id = {p.get("id", "").lower(): p for p in plist}
    for pid in members:
        f = NOTES / f"{pid}.md"
        if f.exists():
            continue
        p = by_id.get(pid, {})
        body = notes.new_note(pid)
        if p.get("role"):
            body = body.replace("_What this project is, who uses it, where it runs (1-3 lines)._",
                                f"{p['role']} Member of the **{meta.get('title') or slug}** initiative.")
        if p.get("state"):
            body = body.replace("_What works, what is half done, what is broken. Keep it true; rewrite rather than append._",
                                f"_Imported {work.now_iso()[:10]} from the {slug} handoff._ {p['state']}")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    # 5. session log + operations log
    work.log_session(f"initiatives/{slug}", f"{core.fmt_ts(__import__('time').time())} · {a['user']}@{a['host']} · "
                     f"import · distilled from {', '.join(meta.get('source') or [])}: {len(legacy)} tasks, "
                     f"{fetched} documents, {len(members)} member projects")
    from . import gc
    gc._log(f"import · initiative {slug} from {', '.join(meta.get('source') or [])}")
    os.environ.pop("DEVKIT_NO_AUTOSYNC", None)
    core.git(HANDOFF, "add", "-A")
    core.git(HANDOFF, "commit", "-q", "-m", f"import initiative {slug} from {', '.join(meta.get('source') or [])} "
             f"({work.actor_str(a)})")
    print(f"  {ui.OK} {fetched}/{len(docs)} documents · {len(legacy)} work items · {len(members)} member projects")
    return 0
