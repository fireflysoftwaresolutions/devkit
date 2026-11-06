# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Who does what: routing work to the right crew member, delegation between members, and dispatch of ready work.

Subagents can launch agents of their own (Claude Code's meta files carry spawnDepth), but such a helper only extends
its member: it gets the member's tool level and scope, takes a crew slot, takes no plan step, and stops at
progress.max_depth (spec F3). So the crew coordinates through the run's plan, and Morpheus (the main session) is the
only dispatcher:

  route      a task description (and the files it touches) -> crew members ranked by fit, with the reasons
  delegate   anyone adds a step for another member; it joins the plan with who asked. If that member is working,
             the step goes to its lane and the member gets a message; otherwise Morpheus is told to launch it
  decline    a member hands a delegated step back to Morpheus, with the reason
  ready      steps whose dependencies are done, assigned to a member who is not working, within the parallel limit:
             Morpheus is told once per step, with the exact launch command

Everything is deterministic: routing scores come from the roster's words and file patterns, not from a model.
"""
from __future__ import annotations

import fnmatch
import json
import re
import time

from . import core, progress
from .progress import StepError, _find_step, _lane, _log_change, _new_step, load_run, normalize, current_session

# files that say who should do the work (globs on the repo-relative path)
FILE_HINTS = [
    (("*.tsx", "*.jsx", "*.vue", "*.svelte", "*.css", "*.scss", "*.html", "web/**", "frontend/**", "ui/**",
      "src/app/**", "*.component.ts"), "trinity"),
    (("*.swift", "*.kt", "ios/**", "android/**", "*.dart"), "switch"),
    (("*migration*", "migrations/**", "db/**", "*.sql", "schema*", "prisma/**", "flyway/**", "liquibase/**",
      "alembic/**"), "dozer"),
    ((".github/workflows/**", ".gitlab-ci.yml", "bitbucket-pipelines.yml", "azure-pipelines*.yml", ".gitlab/ci/**"),
     "link"),
    (("Dockerfile*", "docker-compose*", "*.tf", "terraform/**", "k8s/**", "helm/**", "charts/**", "infra/**",
      "deploy/**"), "niobe"),
    (("test_*.py", "*_test.py", "tests/**", "*.spec.*", "*.test.*", "*Test.java", "*IT.java", "*_test.go"), "mouse"),
    (("docs/**", "*.md", "*.mdx"), "ghost"),
    (("*.java", "*.py", "*.go", "*.php", "*.rb", "*.cs", "api/**", "services/**", "server/**"), "tank"),
    (("Makefile", "scripts/**", "bin/**", "tools/**", ".devcontainer/**"), "kid"),
]
STOP = set("the a an and or of to in on for with by from at as is are be it this that into add make do fix update "
           "change new use using our we you el la los las de del y o en con por para un una que".split())


def _roster() -> list:
    """The crew without Morpheus: devkit's roster with the team's changes from the handoff repository."""
    from . import crew
    return crew.members(include_main=False)


def _terms(text: str) -> set:
    out = set()
    for w in re.findall(r"[a-záéíóúñü0-9+#.-]{2,}", (text or "").lower()):
        w = w.strip(".-")
        if w and w not in STOP:
            out.add(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w)
    return out


def route(task: str, files=None, limit: int = 3) -> list:
    """-> [(member, score, [reasons])], best first. The crew's RACI decides first: when the task is clearly one kind
    of work, its Responsible members lead. Then the member scores: keywords (x5), words shared with its role and
    use_when (x3), summary and skills (x1), and the files the task touches (x4 per matching file kind)."""
    from . import crew, raci
    lead = []
    top = raci.best(task)
    if top:
        act, score, hits = top
        everyone = crew.members(include_main=True)  # planning is Morpheus's own kind of work
        for mid in act.get("R") or []:
            m = next((x for x in everyone if x["id"] == mid), None)
            if m:
                lead.append((m, float(10 + score), [f"RACI R: {act['name']}",
                                                    f"approves: {raci.names([act.get('A')])}",
                                                    "signals: " + ", ".join(hits[:4])]))
    words, said = _terms(task), raci.norm(task)
    scored = []
    for m in _roster():
        score, why = 0.0, []
        kws = [str(k) for k in m.get("keywords") or []]
        single = [k for k in kws if not re.search(r"[\s-]", k.strip())]
        keys = _terms(" ".join(single)) | {k.lower() for k in single}
        # a hyphenated or multi-word keyword hits only as the whole phrase ("app-store" or "app store"), never
        # through its parts: "upgrade the app" is not app-store work
        phrases = sorted({k.lower() for k in kws if k not in single and any(
            raci._pattern(v).search(said) for v in (k, k.replace("-", " ")))})
        hit_k = sorted(words & keys) + phrases
        if hit_k:
            score += 5 * len(hit_k)
            why.append("keywords: " + ", ".join(hit_k[:4]))
        strong = _terms(m.get("role", "") + " " + " ".join(m.get("use_when") or [])) - set(hit_k)
        weak = _terms(m.get("summary", "") + " " + " ".join(m.get("skills") or []))
        hit_s = sorted(words & strong)
        hit_w = sorted((words & weak) - set(hit_s))
        if hit_s:
            score += 3 * len(hit_s)
            why.append("role: " + ", ".join(hit_s[:4]))
        if hit_w:
            score += len(hit_w)
            why.append("also: " + ", ".join(hit_w[:4]))
        for f in files or []:
            for globs, who in FILE_HINTS:
                if who == m["id"] and any(fnmatch.fnmatch(f, g) or fnmatch.fnmatch(f.split("/")[-1], g) for g in globs):
                    score += 4
                    why.append(f"file {f}")
                    break
        nots = _terms(" ".join(m.get("not_for") or []))
        if words & nots:
            score -= 2 * len(words & nots)
            why.append("not for: " + ", ".join(sorted(words & nots)[:3]))
        if score >= 3:  # at least one keyword, role or file hit: a loose summary word alone does not route
            scored.append((m, round(score, 1), why))
    scored.sort(key=lambda x: (-x[1], x[0]["id"] != "neo"))  # ties: Neo, the generalist, first
    if lead:
        ids = {m["id"] for m, _, _ in lead}
        own = {m["id"]: why for m, _, why in scored}
        lead = [(m, s, why[:1] + [w for w in own.get(m["id"], []) if w.startswith("file ")] + why[1:])
                for m, s, why in lead]  # the files the task touches stay visible next to the RACI reason
        return (lead + [x for x in scored if x[0]["id"] not in ids])[:limit]
    if not scored:  # nothing specific: the senior engineer
        neo = next((m for m in _roster() if m["id"] == "neo"), None)
        return [(neo, 0.0, ["no specialist matched: the generalist"])] if neo else []
    return scored[:limit]


def _member_name(ref: str):
    """'Tank', 'tank', 'devkit:tank', 'The Oracle', 'oracle' -> the member dict, or None."""
    r = (ref or "").strip().lower().split(":")[-1]
    for m in _roster():
        names = {m["id"], m["name"].lower(), m["name"].lower().replace("the ", ""), m["name"].lower().split()[-1]}
        if r in names:
            return m
    return None


def delegate(task: str, to: str = "auto", files=None, scope: str = "", after: str = "", est: str = "",
             from_agent: str | None = None, cwd: str | None = None, now: float | None = None) -> dict:
    """Add a step for another crew member. -> {"step", "member", "routed", "lane"} (lane: the working lane it
    joined, or None when Morpheus has to launch the member)."""
    now = now or time.time()
    from_agent = progress._as_lane(from_agent, cwd)  # a member's call names the member, whatever the variables say
    agent, sid = current_session(cwd)
    with progress._tx(agent, sid):
        run = load_run(agent, sid)
        if not run:
            raise StepError("no progress run in this session: Morpheus starts one with dev progress start")
        run = normalize(run)
        if not (task or "").strip():
            raise StepError("say what to do: dev crew delegate \"<task>\" --to <member>")
        routed = None
        if not to or to.lower() == "auto":
            ranked = route(task, files)
            if not ranked:
                raise StepError("no crew member fits; name one with --to")
            m, routed = ranked[0][0], ranked
        else:
            m = _member_name(to)
            if not m:
                raise StepError(f"no crew member “{to}”: dev crew lists them")
        name = re.sub(r"\s+", " ", task.strip())[:60]
        base, i = name, 2
        while any(s["name"].lower() == name.lower() for s in run["plan"]):
            name, i = f"{base} ({i})", i + 1
        s = _new_step(name, progress.parse_minutes(est) or None)
        s.update(label=m["name"], requested_by=from_agent or "main", task=task.strip()[:1500])
        if scope:
            s["scope"] = [x.strip() for x in scope.split(",") if x.strip()]
        if after:
            dep = _find_step(run, after)
            if not dep:
                raise StepError(f"no step “{after}” to wait for")
            s["after"] = [dep["name"]]
        run["plan"].append(s)
        progress._allocate(run)
        run["steps"] = len(run["plan"])
        by = progress._name_of(run, from_agent or "main")
        _log_change(run, s, None, "pending", from_agent or "main", f"delegated by {by} to {m['name']}", now)
        lane_id = next((k for k, v in (run.get("agents") or {}).items() if v.get("state") == "running"
                        and progress._label_matches(m["name"], v)), None)
        if lane_id and lane_id != from_agent and not progress.deps_open(run, s):  # a step that waits does not start
            s.update(owner=lane_id, state="running", started=now)
            if s.get("scope"):
                run["agents"][lane_id]["scope"] = sorted(set((run["agents"][lane_id].get("scope") or []) + s["scope"]))
        progress._save(run, touch_session=False)
    progress._audit("crew.delegate", f"{by} → {m['name']}", task.strip()[:300], project=run.get("project") or "",
                    step=name, routed="yes" if routed else "no")
    from . import crew
    text = f"{by} delegates “{task.strip()[:200]}” to {crew.who(m)} (step “{name}”)"
    if lane_id and s.get("owner") == lane_id:
        progress.crew_ask(text + ". It is in your lane now; if you cannot take it: dev crew decline "
                          f"\"{name}\" --why \"…\"", to=lane_id, from_agent=from_agent, cwd=cwd, now=now)
    elif from_agent:
        progress.crew_ask(text + f". Launch {m['name']} when it is ready: dev crew next", to="main",
                          from_agent=from_agent, cwd=cwd, now=now)
    return {"step": s, "member": m, "routed": routed, "lane": lane_id if s.get("owner") == lane_id else None}


def decline(which: str, why: str, from_agent: str | None = None, cwd: str | None = None, now: float | None = None):
    """A member hands a delegated step back: unassigned, pending, and Morpheus is told why."""
    now = now or time.time()
    if not (why or "").strip():
        raise StepError("say why: --why \"…\"")
    agent, sid = current_session(cwd)
    with progress._tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        s = _find_step(run, which)
        if not s:
            raise StepError(f"no step “{which}” in the plan")
        if from_agent and s.get("owner") not in (from_agent, "main", None):
            raise StepError(f"step “{s['name']}” is not yours")
        old = s.get("state")
        who = s.get("label") or "the member"
        s.update(state="pending", owner="main", label="", started=None)
        _log_change(run, s, old, "pending", from_agent or "main", f"declined by {who}: {why}", now)
        progress._audit("crew.decline", who, why[:300], project=run.get("project") or "", step=s["name"])
        progress._save(run, touch_session=False)
    progress.crew_ask(f"{who} declined “{s['name']}”: {why}. Route it again: dev crew route \"{s['name']}\"",
                      to="main", from_agent=from_agent, cwd=cwd, now=now)
    return s


def ready(run, now: float | None = None) -> dict:
    """{"launch": [steps ready to launch], "waiting": [(step, why)], "busy": [lanes working], "free": n,
    "limit": n} (the parallel limit, lower while the 5-hour quota is short)."""
    run = normalize(run)
    plan = run.get("plan") or []
    busy = progress.running_agents(run)
    limit = progress.parallel_limit(run.get("agent") or "claude", now)[0]
    free = max(0, limit - len(progress.slots(run)))  # launches count before they start
    launch, waiting = [], []
    for s in plan:
        if s.get("state") not in ("pending", "blocked") or not s.get("label") or (s.get("owner") or "main") != "main":
            continue
        deps = progress.deps_open(run, s)  # the plan's one dependency rule: done or skipped satisfies a step
        if s.get("waiting"):
            waiting.append((s, f"decision {s['waiting']}"))
        elif deps:
            waiting.append((s, "after " + ", ".join(deps)))
        elif any(progress._label_matches(s["label"], v) for _, v in busy):
            waiting.append((s, f"{s['label']} is busy: send it to them (SendMessage / send_input)"))
        else:
            launch.append(s)
    return {"launch": launch[:free], "queued": launch[free:], "waiting": waiting, "busy": busy, "free": free,
            "limit": limit}


def launch_line(s: dict) -> str:
    """The exact `dev crew brief …` command for a step, safely quoted for the shell."""
    import shlex
    m = None
    try:
        from . import crew
        m = crew.member(s.get("label") or "")
    except Exception:
        m = None
    m = m or _member_name(s.get("label") or "")
    mid = m["id"] if m else (s.get("label") or "").lower()
    parts = ["dev", "crew", "brief", mid, s.get("task") or s["name"]]
    if s.get("scope"):
        parts += ["--scope", ",".join(s["scope"])]
    parts += ["--step", s["name"]]
    return " ".join(shlex.quote(p) for p in parts)


def ready_context(agent: str, sid: str) -> str:
    """For Morpheus's next action: steps that just became ready to launch (once per step)."""
    run = load_run(agent, sid)
    if not run or run.get("state") == "done" or all(s.get("ready_told") for s in ready(run)["launch"]):
        return ""  # the usual case, without the lock
    with progress._tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        r = ready(run)
        new = [s for s in r["launch"] if not s.get("ready_told")]
        if not new:
            return ""
        for s in new:
            s["ready_told"] = True
        progress._save(run, touch_session=False)
    mode = progress.mode_of(run)
    lines = [f"devkit · ready to launch ({r['free']} of {r['limit']} crew slots free, {mode} "
             f"mode{': launch them now, in one message' if mode == 'auto' else ''}):"]
    from . import crew
    lines += [f"- {crew._who_label(s['label'])}: {s['name']} → {launch_line(s)}" for s in new]
    if r["queued"]:
        lines.append(f"- queued until a slot frees: {', '.join(s['name'] for s in r['queued'])}")
    return "\n".join(lines)


def assign_plan(apply: bool = False, cwd: str | None = None, now: float | None = None) -> list:
    """Route every plan step nobody owns: -> [(step, member, score, reasons)]; with apply, assign them (Morpheus
    only, like dev progress assign: spec C4)."""
    now = now or time.time()
    if apply:
        progress._main_only(None, "assigns the plan's steps", cwd)
    agent, sid = current_session(cwd)
    with progress._tx(agent, sid):
        run = load_run(agent, sid)
        if not run:
            raise StepError("no progress run in this session")
        run = normalize(run)
        out = []
        for s in run.get("plan") or []:
            if s.get("state") in ("done", "skipped") or s.get("label") or (s.get("owner") or "main") != "main":
                continue
            ranked = route(s.get("task") or s["name"])
            if ranked and ranked[0][1] > 0:
                m, score, why = ranked[0]
                out.append((s, m, score, why))
                if apply:
                    s["label"] = m["name"]
                    if s.get("state") == "running":
                        s["state"] = "pending"
                    _log_change(run, s, None, "pending", "main", f"routed to {m['name']} ({'; '.join(why)[:120]})", now)
        if apply and out:
            progress._advance_main(run, now)
            progress._save(run)
    return out


def to_json(x) -> str:
    return json.dumps(x, ensure_ascii=False, default=str)
