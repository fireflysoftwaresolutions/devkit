# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""Live state of CI/CD runs on any git host, with progress and an ETA.

Providers (chosen from the repository's remote):
  GitHub Actions     `gh` (signed in by devkit)
  GitLab CI          `glab api` when installed, else the REST API with GITLAB_TOKEN
  Bitbucket Pipelines the REST API with BITBUCKET_TOKEN (access token), or BITBUCKET_USERNAME + BITBUCKET_APP_PASSWORD
  Azure Pipelines    the REST API with AZURE_DEVOPS_EXT_PAT (the same variable the `az devops` CLI uses)

Every provider is normalized to the same shape:
  run  {provider, id, name, number, branch, sha, event, status, started, finished, elapsed, eta, typical, url, jobs}
  job  {name, status, started, elapsed, steps_done, steps_total, current}
status is one of queued | running | success | failed | cancelled | skipped. ETA = the median duration of the last
successful runs of the same pipeline minus the elapsed time. Tokens are read from the environment and never printed.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import statistics
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import core
from .core import STATE

CACHE = STATE / "cache" / "ci"
RUNNING = ("queued", "running")


def _ts(s) -> float:
    if not s:
        return 0.0
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
    return t if t > 946684800 else 0.0  # placeholders such as 0001-01-01 (GitHub, unfinished jobs) mean "not yet"


def fmt(secs) -> str:
    secs = int(max(0, secs or 0))
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s" if m else f"{s}s"


def _cached(key: str, max_age: float, producer):
    f = CACHE / (key.replace("/", "__").replace(":", "_") + ".json")
    try:
        if f.exists() and time.time() - f.stat().st_mtime < max_age:
            return json.loads(f.read_text())
    except (OSError, ValueError):
        pass
    val = producer()
    if val is not None:
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(val))
        except OSError:
            pass
    return val


def _http(url: str, headers: dict, timeout: float = 10):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "devkit", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310: https URLs built from the remote
        return json.loads(r.read().decode())


def _with_eta(run: dict, typical):
    run["typical"] = typical
    if run["status"] in RUNNING and typical:
        run["eta"] = max(0, typical - run["elapsed"])
    return run


def _median(durations) -> float:
    d = [x for x in durations if x and x > 0]
    return statistics.median(d) if d else 0


# --------------------------------------------------------------------------- GitHub Actions

GH_STATUS = {"completed": None, "in_progress": "running", "queued": "queued", "waiting": "queued",
             "requested": "queued", "pending": "queued"}
GH_CONCLUSION = {"success": "success", "failure": "failed", "timed_out": "failed", "startup_failure": "failed",
                 "action_required": "failed", "cancelled": "cancelled", "skipped": "skipped", "neutral": "success",
                 "stale": "cancelled"}


def _gh_status(status, conclusion) -> str:
    return GH_STATUS.get(status) or GH_CONCLUSION.get(conclusion or "", "failed" if status == "completed" else "queued")


def github_job(j: dict, now: float) -> dict:
    steps = j.get("steps") or []
    done = [s for s in steps if s.get("status") == "completed"]
    cur = next((s.get("name") for s in steps if s.get("status") == "in_progress"), "")
    st, fin = _ts(j.get("startedAt")), _ts(j.get("completedAt"))
    return {"name": j.get("name"), "status": _gh_status(j.get("status"), j.get("conclusion")), "started": st,
            "elapsed": (fin or now) - st if st else 0, "steps_done": len(done), "steps_total": len(steps),
            "current": cur, "url": j.get("url")}


def github_run(r: dict, now: float) -> dict:
    st = _ts(r.get("startedAt") or r.get("createdAt"))
    status = _gh_status(r.get("status"), r.get("conclusion"))
    fin = _ts(r.get("updatedAt")) if status not in RUNNING else 0
    return {"provider": "GitHub Actions", "id": str(r.get("databaseId")), "name": r.get("workflowName") or r.get("name"),
            "number": r.get("number"), "title": r.get("displayTitle"), "branch": r.get("headBranch"),
            "sha": (r.get("headSha") or "")[:7], "event": r.get("event"), "status": status, "started": st,
            "finished": fin, "elapsed": (fin or now) - st if st else 0, "eta": None, "url": r.get("url"),
            "workflow_id": r.get("workflowDatabaseId"), "jobs": []}


def _github(owner_repo: str, branch, limit: int, detail: bool) -> list:
    fields = "databaseId,number,status,conclusion,workflowName,workflowDatabaseId,displayTitle,headBranch,headSha," \
             "event,createdAt,startedAt,updatedAt,url"
    args = ["gh", "run", "list", "-R", owner_repo, "-L", str(limit), "--json", fields]
    if branch:
        args += ["--branch", branch]
    rc, out, err = core.sh(args, timeout=20)
    if rc != 0:
        raise RuntimeError((err or out).strip()[:200] or "gh run list failed")
    now = time.time()
    runs = [github_run(r, now) for r in json.loads(out or "[]")]
    for run in runs:
        wid = run.get("workflow_id")

        def typical(wid=wid):
            rc_, o, _ = core.sh(["gh", "run", "list", "-R", owner_repo, "--workflow", str(wid), "--status", "success",
                                 "-L", "10", "--json", "startedAt,createdAt,updatedAt"], timeout=20)
            if rc_ != 0:
                return None
            return _median(_ts(x["updatedAt"]) - _ts(x.get("startedAt") or x["createdAt"]) for x in json.loads(o or "[]"))
        _with_eta(run, _cached(f"gh-{owner_repo}-{wid}", 6 * 3600, typical) if wid else None)
        if detail and run["status"] in RUNNING:
            rc_, o, _ = core.sh(["gh", "run", "view", run["id"], "-R", owner_repo, "--json", "jobs"], timeout=20)
            if rc_ == 0:
                run["jobs"] = [github_job(j, now) for j in (json.loads(o or "{}").get("jobs") or [])]
    return runs


# --------------------------------------------------------------------------- GitLab CI

GL_STATUS = {"created": "queued", "waiting_for_resource": "queued", "preparing": "queued", "pending": "queued",
             "scheduled": "queued", "manual": "queued", "running": "running", "success": "success",
             "failed": "failed", "canceled": "cancelled", "canceling": "cancelled", "skipped": "skipped"}


def gitlab_run(p: dict, now: float) -> dict:
    st = _ts(p.get("started_at") or p.get("created_at"))
    status = GL_STATUS.get(p.get("status"), "queued")
    fin = _ts(p.get("finished_at") or (p.get("updated_at") if status not in RUNNING else None))
    return {"provider": "GitLab CI", "id": str(p.get("id")), "name": p.get("name") or "pipeline", "number": p.get("iid"),
            "title": None, "branch": p.get("ref"), "sha": (p.get("sha") or "")[:7], "event": p.get("source"),
            "status": status, "started": st, "finished": fin, "elapsed": (fin or now) - st if st else 0, "eta": None,
            "url": p.get("web_url"), "jobs": []}


def gitlab_jobs(jobs: list, now: float) -> list:
    """GitLab jobs are the steps of a stage: one row per stage, with its jobs as steps."""
    stages = {}
    for j in jobs:
        stages.setdefault(j.get("stage") or "jobs", []).append(j)
    out = []
    for stage, js in stages.items():
        statuses = [GL_STATUS.get(j.get("status"), "queued") for j in js]
        status = "failed" if "failed" in statuses else "running" if "running" in statuses else \
            "queued" if all(s == "queued" for s in statuses) else "running" if "queued" in statuses else statuses[0]
        starts = [_ts(j.get("started_at")) for j in js if j.get("started_at")]
        ends = [_ts(j.get("finished_at")) for j in js if j.get("finished_at")]
        st = min(starts) if starts else 0
        fin = max(ends) if ends and status not in RUNNING else 0
        out.append({"name": stage, "status": status, "started": st, "elapsed": (fin or now) - st if st else 0,
                    "steps_done": sum(1 for s in statuses if s not in RUNNING), "steps_total": len(js),
                    "current": next((j.get("name") for j in js if j.get("status") == "running"), ""), "url": None})
    return out


def _gitlab(host: str, path: str, branch, limit: int, detail: bool) -> list:
    pid = urllib.parse.quote(path, safe="")

    def api(endpoint):
        if core.have("glab"):
            rc, out, err = core.sh(["glab", "api", "--hostname", host, endpoint], timeout=20)
            if rc != 0:
                raise RuntimeError((err or out).strip()[:200] or "glab api failed")
            return json.loads(out or "[]")
        token = os.environ.get("GITLAB_TOKEN") or os.environ.get("GITLAB_PRIVATE_TOKEN")
        if not token:
            raise RuntimeError("GitLab: install `glab` and sign in (`glab auth login`), or set GITLAB_TOKEN")
        return _http(f"https://{host}/api/v4/{endpoint}", {"PRIVATE-TOKEN": token})

    q = f"projects/{pid}/pipelines?per_page={limit}&order_by=id&sort=desc" + \
        (f"&ref={urllib.parse.quote(branch, safe='')}" if branch else "")
    now = time.time()
    runs = [gitlab_run(p, now) for p in api(q)]

    def typical():
        ok = api(f"projects/{pid}/pipelines?per_page=10&status=success" +
                 (f"&ref={urllib.parse.quote(branch, safe='')}" if branch else ""))
        return _median(_ts(p.get("updated_at")) - _ts(p.get("created_at")) for p in ok)
    t = _cached(f"gl-{host}-{path}-{branch}", 6 * 3600, typical) if runs else None
    for run in runs:
        _with_eta(run, t)
        if detail and run["status"] in RUNNING:
            run["jobs"] = gitlab_jobs(api(f"projects/{pid}/pipelines/{run['id']}/jobs?per_page=100"), now)
    return runs


# --------------------------------------------------------------------------- Bitbucket Pipelines

def _bb_status(state: dict) -> str:
    name = (state or {}).get("name", "")
    result = ((state or {}).get("result") or {}).get("name", "")
    if name in ("PENDING", "PAUSED", "HALTED") and not result:
        return "queued"
    if name == "IN_PROGRESS" or ((state or {}).get("stage") or {}).get("name") == "RUNNING":
        return "running"
    return {"SUCCESSFUL": "success", "FAILED": "failed", "ERROR": "failed", "STOPPED": "cancelled",
            "EXPIRED": "cancelled"}.get(result, "queued" if name == "PENDING" else "failed")


def bitbucket_run(p: dict, now: float, workspace: str, repo: str) -> dict:
    st = _ts(p.get("created_on"))
    status = _bb_status(p.get("state"))
    fin = _ts(p.get("completed_on"))
    target = p.get("target") or {}
    return {"provider": "Bitbucket Pipelines", "id": p.get("uuid"), "name": (target.get("selector") or {}).get("pattern")
            or "pipeline", "number": p.get("build_number"), "title": None, "branch": target.get("ref_name"),
            "sha": ((target.get("commit") or {}).get("hash") or "")[:7], "event": (p.get("trigger") or {}).get("name"),
            "status": status, "started": st, "finished": fin, "elapsed": (fin or now) - st if st else 0, "eta": None,
            "url": f"https://bitbucket.org/{workspace}/{repo}/pipelines/results/{p.get('build_number')}", "jobs": []}


def bitbucket_steps(steps: list, now: float) -> list:
    out = []
    for s in steps:
        st, fin = _ts(s.get("started_on")), _ts(s.get("completed_on"))
        out.append({"name": s.get("name") or "step", "status": _bb_status(s.get("state")), "started": st,
                    "elapsed": (fin or now) - st if st else 0, "steps_done": 0, "steps_total": 0, "current": "",
                    "url": None})
    return out


def _bitbucket(path: str, branch, limit: int, detail: bool) -> list:
    workspace, _, repo = path.partition("/")
    if os.environ.get("BITBUCKET_TOKEN"):
        headers = {"Authorization": f"Bearer {os.environ['BITBUCKET_TOKEN']}"}
    elif os.environ.get("BITBUCKET_USERNAME") and os.environ.get("BITBUCKET_APP_PASSWORD"):
        cred = f"{os.environ['BITBUCKET_USERNAME']}:{os.environ['BITBUCKET_APP_PASSWORD']}".encode()
        headers = {"Authorization": "Basic " + base64.b64encode(cred).decode()}
    else:
        raise RuntimeError("Bitbucket: set BITBUCKET_TOKEN (a repository or workspace access token with "
                           "pipeline:read), or BITBUCKET_USERNAME and BITBUCKET_APP_PASSWORD")
    base = f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo}/pipelines/"
    q = f"?sort=-created_on&pagelen={limit}" + (f"&target.ref_name={urllib.parse.quote(branch, safe='')}" if branch else "")
    now = time.time()
    runs = [bitbucket_run(p, now, workspace, repo) for p in _http(base + q, headers).get("values", [])]

    def typical():
        ok = [p for p in _http(base + "?sort=-created_on&pagelen=30", headers).get("values", [])
              if _bb_status(p.get("state")) == "success"][:10]
        return _median(p.get("duration_in_seconds") for p in ok)
    t = _cached(f"bb-{path}", 6 * 3600, typical) if runs else None
    for run in runs:
        _with_eta(run, t)
        if detail and run["status"] in RUNNING:
            uuid = urllib.parse.quote(run["id"], safe="")
            run["jobs"] = bitbucket_steps(_http(f"{base}{uuid}/steps/", headers).get("values", []), now)
    return runs


# --------------------------------------------------------------------------- Azure Pipelines

def _az_status(status, result) -> str:
    if status in ("notStarted", "postponed", "pending"):
        return "queued"
    if status in ("inProgress", "cancelling"):
        return "running" if status == "inProgress" else "cancelled"
    return {"succeeded": "success", "partiallySucceeded": "success", "failed": "failed", "canceled": "cancelled",
            "skipped": "skipped", "abandoned": "cancelled"}.get(result or "", "queued")


def azure_run(b: dict, now: float) -> dict:
    st = _ts(b.get("startTime") or b.get("queueTime"))
    status = _az_status(b.get("status"), b.get("result"))
    fin = _ts(b.get("finishTime"))
    return {"provider": "Azure Pipelines", "id": str(b.get("id")), "name": (b.get("definition") or {}).get("name"),
            "number": b.get("buildNumber"), "title": None,
            "branch": (b.get("sourceBranch") or "").replace("refs/heads/", ""), "sha": (b.get("sourceVersion") or "")[:7],
            "event": b.get("reason"), "status": status, "started": st, "finished": fin,
            "elapsed": (fin or now) - st if st else 0, "eta": None,
            "url": ((b.get("_links") or {}).get("web") or {}).get("href"), "definition": (b.get("definition") or {}).get("id"),
            "jobs": []}


def azure_jobs(records: list, now: float) -> list:
    """Timeline records: Job rows, with their Task records as steps."""
    tasks = {}
    for r in records:
        if r.get("type") == "Task":
            tasks.setdefault(r.get("parentId"), []).append(r)
    out = []
    for j in (r for r in records if r.get("type") == "Job"):
        ts_ = tasks.get(j.get("id"), [])
        st, fin = _ts(j.get("startTime")), _ts(j.get("finishTime"))
        out.append({"name": j.get("name"), "status": _az_status(j.get("state"), j.get("result")), "started": st,
                    "elapsed": (fin or now) - st if st else 0,
                    "steps_done": sum(1 for t in ts_ if t.get("state") == "completed"), "steps_total": len(ts_),
                    "current": next((t.get("name") for t in ts_ if t.get("state") == "inProgress"), ""), "url": None})
    return out


def _azure(path: str, branch, limit: int, detail: bool) -> list:
    """path = <org>/<project>/<repo> (from dev.azure.com/<org>/<project>/_git/<repo>)."""
    parts = path.split("/")
    if len(parts) < 3:
        raise RuntimeError(f"Azure DevOps: cannot read organization/project/repository from {path}")
    org, project, repo = parts[0], parts[1], parts[-1]
    pat = os.environ.get("AZURE_DEVOPS_EXT_PAT") or os.environ.get("AZURE_DEVOPS_PAT")
    if not pat:
        raise RuntimeError("Azure DevOps: set AZURE_DEVOPS_EXT_PAT (a personal access token with Build: Read)")
    headers = {"Authorization": "Basic " + base64.b64encode(f":{pat}".encode()).decode()}
    base = f"https://dev.azure.com/{urllib.parse.quote(org)}/{urllib.parse.quote(project)}/_apis"
    rid = _cached(f"az-repo-{path}", 7 * 86400,
                  lambda: _http(f"{base}/git/repositories/{urllib.parse.quote(repo)}?api-version=7.1", headers).get("id"))
    q = f"{base}/build/builds?repositoryId={rid}&repositoryType=TfsGit&$top={limit}&queryOrder=queueTimeDescending" \
        f"&api-version=7.1" + (f"&branchName=refs/heads/{urllib.parse.quote(branch, safe='/')}" if branch else "")
    now = time.time()
    runs = [azure_run(b, now) for b in _http(q, headers).get("value", [])]
    for run in runs:
        d = run.get("definition")

        def typical(d=d):
            ok = _http(f"{base}/build/builds?definitions={d}&resultFilter=succeeded&$top=10&api-version=7.1",
                       headers).get("value", [])
            return _median(_ts(b.get("finishTime")) - _ts(b.get("startTime")) for b in ok)
        _with_eta(run, _cached(f"az-{path}-{d}", 6 * 3600, typical) if d else None)
        if detail and run["status"] in RUNNING:
            tl = _http(f"{base}/build/builds/{run['id']}/timeline?api-version=7.1", headers)
            run["jobs"] = azure_jobs(tl.get("records") or [], now)
    return runs


# --------------------------------------------------------------------------- entry points

def provider_for(remote: str):
    """-> (kind, host, path) from a git remote URL."""
    parsed = core.parse_remote(remote or "")
    if not parsed:
        return None
    host, path = parsed[0].lower(), f"{parsed[1]}/{parsed[2]}"
    if host == "github.com" or "github" in host:
        return "github", host, path
    if "gitlab" in host:
        return "gitlab", host, path
    if host == "bitbucket.org":
        return "bitbucket", host, path
    if host in ("dev.azure.com", "ssh.dev.azure.com") or host.endswith("visualstudio.com"):
        return "azure", host, path
    return None


def runs_for(repo_path, branch=None, limit: int = 5, detail: bool = True, max_age: float = 20):
    """-> {"provider", "repo", "branch", "runs": [...], "error"} for a local repository. Cached for `max_age` s."""
    remote = core.gitout(repo_path, "config", "--get", "remote.origin.url")
    prov = provider_for(remote)
    if not prov:
        return {"provider": None, "repo": None, "branch": branch, "runs": [], "error": "no CI provider for this remote"}
    kind, host, path = prov

    def produce():
        try:
            if kind == "github":
                runs = _github(path, branch, limit, detail)
            elif kind == "gitlab":
                runs = _gitlab(host, path, branch, limit, detail)
            elif kind == "bitbucket":
                runs = _bitbucket(path, branch, limit, detail)
            else:
                runs = _azure(path, branch, limit, detail)
            return {"provider": kind, "repo": path, "branch": branch, "runs": runs, "error": None}
        except Exception as e:  # network, auth, API change: report it, never fail the caller
            return {"provider": kind, "repo": path, "branch": branch, "runs": [], "error": str(e)[:300]}
    return _cached(f"runs-{kind}-{path}-{branch}-{limit}-{int(detail)}", max_age, produce)


GLYPH = {"success": "✓", "failed": "✗", "cancelled": "⊘", "skipped": "–", "running": "▸", "queued": "○"}


def describe(run: dict, now: float = None) -> str:
    """One plain line: `▸ CI #42 running · 12m elapsed · ETA ~26m (usually 38m) · push a1b2c3d`."""
    now = now or time.time()
    head = f"{GLYPH.get(run['status'], '·')} {run.get('name') or 'pipeline'}" + \
        (f" #{run['number']}" if run.get("number") else "") + f" {run['status']}"
    parts = [head]
    if run["status"] in RUNNING:
        parts.append(f"{fmt(run['elapsed'])} elapsed")
        if run.get("eta") is not None:
            parts.append(f"ETA ~{fmt(run['eta'])} (usually {fmt(run['typical'])})")
    elif run.get("finished"):
        parts.append(f"took {fmt(run['elapsed'])}, {core.ago(run['finished'])}")
    if run.get("event") or run.get("sha"):
        parts.append(" ".join(x for x in (run.get("event"), run.get("sha")) if x))
    return " · ".join(parts)


def describe_job(j: dict) -> str:
    steps = f" · step {min(j['steps_done'] + 1, j['steps_total'])}/{j['steps_total']}" if j.get("steps_total") and \
        j["status"] == "running" else ""
    cur = f" “{j['current']}”" if j.get("current") else ""
    took = f" · {fmt(j['elapsed'])}" if j.get("elapsed") else ""
    name = j["name"] if len(j["name"] or "") <= 60 else j["name"][:57] + "…"
    return f"{GLYPH.get(j['status'], '·')} {name}{steps}{cur}{took}"


def job_summary(jobs: list) -> str:
    """`9/23 jobs done · 14 running · 1 failed`."""
    if not jobs:
        return ""
    by = {}
    for j in jobs:
        by[j["status"]] = by.get(j["status"], 0) + 1
    done = sum(v for k, v in by.items() if k not in RUNNING)
    parts = [f"{done}/{len(jobs)} jobs done"]
    for k in ("running", "queued", "failed", "cancelled"):
        if by.get(k):
            parts.append(f"{by[k]} {k}")
    return " · ".join(parts)


def jobs_to_show(jobs: list, max_running: int = 6) -> tuple:
    """Failed jobs first, then running ones (capped). -> (jobs, how many running were left out)."""
    failed = [j for j in jobs if j["status"] == "failed"]
    running = sorted([j for j in jobs if j["status"] in RUNNING], key=lambda j: -(j.get("elapsed") or 0))
    return failed + running[:max_running], max(0, len(running) - max_running)


def context_lines(repo_path, branch=None, limit: int = 3) -> list:
    """Short plain lines for agent context (status questions): runs of this branch with job progress."""
    res = runs_for(repo_path, branch, limit=limit, detail=True)
    if res.get("error"):
        return [f"CI ({res.get('provider') or 'unknown'}): could not read runs — {res['error']}"]
    out = []
    for r in res["runs"][:limit]:
        out.append(f"CI {res['provider']}: {describe(r)} — {r.get('url') or ''}".rstrip(" —"))
        if r.get("jobs"):
            out.append("    " + job_summary(r["jobs"]))
            shown, more = jobs_to_show(r["jobs"])
            out += ["    " + describe_job(j) for j in shown] + ([f"    … +{more} more running"] if more else [])
    return out or [f"CI {res['provider']}: no runs for {branch or 'this repository'}"]
