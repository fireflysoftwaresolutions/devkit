# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Who does what: routing work to the right crew member, delegation between members, and dispatch of ready work.

Subagents can launch agents of their own (Claude Code's meta files carry spawnDepth), but such a helper only extends
its member: it gets the member's tool level and scope, takes a crew slot, takes no plan step, and stops at
progress.max_depth (spec F3). So the crew coordinates through the run's plan, and Morpheus (the main session) is the
only dispatcher:

  route      a task description (and the files it touches) -> crew members ranked by fit, with the reasons
  delegate   anyone adds a step for another member; it joins the plan with who asked. If that member is working,
             the step goes to its lane and the member gets a message (its launch does not name the step, so only the
             lane's evidence naming it closes it: progress._steps_about); otherwise Morpheus is told to launch it
  decline    a member hands a delegated step back to Morpheus, with the reason
  ready      steps whose dependencies are done (progress.deps_open: a review step also takes unverified work), assigned
             to a member who is not working, within the parallel limit:
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
    # Kotlin by where it lives: Android keeps it under src/main/java, Spring and Ktor under src/main/kotlin (tank)
    (("*.swift", "ios/**", "android/**", "*.dart", "src/main/java/**/*.kt", "*/src/main/java/**/*.kt",
      "*AndroidManifest.xml"), "switch"),
    (("*migration*", "migrations/**", "db/**", "*.sql", "schema*", "prisma/**", "flyway/**", "liquibase/**",
      "alembic/**"), "dozer"),
    ((".github/workflows/**", ".gitlab-ci.yml", "bitbucket-pipelines.yml", "azure-pipelines*.yml", ".gitlab/ci/**"),
     "link"),
    (("Dockerfile*", "docker-compose*", "*.tf", "terraform/**", "k8s/**", "helm/**", "charts/**", "infra/**",
      "deploy/**"), "niobe"),
    (("test_*.py", "*_test.py", "tests/**", "*.spec.*", "*.test.*", "*Test.java", "*IT.java", "*_test.go"), "mouse"),
    # the Markdown of the docs skill rule (config/skill-rules.json, rule docs): what the writer edits needs that skill
    (("docs/**", "README.md", "docs/*.md", "docs/**/*.md", "*.mdx", "CHANGELOG.md", "CONTRIBUTING.md",
      "plugin/skills/**", "**/SKILL.md", "plugin/agents/*.md", "plugin/commands/*.md", "agents/AGENTS.md"), "ghost"),
    (("*.java", "*.py", "*.go", "*.php", "*.rb", "*.cs", "src/main/kotlin/**", "*/src/main/kotlin/**", "api/**",
      "services/**", "server/**"), "tank"),
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
            raise StepError("no progress run in this session: the tech lead starts one with dev progress start")
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
        s.update(label=m["name"], requested_by=from_agent or "main", task=task.strip()[:1500], added=round(now, 1))
        if scope:  # repository-relative, as dev progress assign keeps it: the form the Edit guard matches
            globs, rewrites = progress._scope_globs([x.strip() for x in scope.split(",") if x.strip()],
                                                    run.get("project") or "", cwd)
            s["scope"] = globs
            progress._tell_rewrites(run, name, rewrites, now)
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
        # only a lane whose member still works takes it (progress.working_agents, in the caller's own run): one that
        # ended or went stale would hold a step nobody starts, so the step waits for a new launch, with Morpheus
        lane_id = next((k for k, v in progress.working_agents(run, now, own=True)
                        if progress._label_matches(m["name"], v)), None)
        if lane_id and lane_id != from_agent and not progress.deps_open(run, s):  # a step that waits does not start
            s.update(owner=lane_id, state="running", started=now, joined=lane_id)  # its launch does not name it
            if s.get("scope"):
                run["agents"][lane_id]["scope"] = sorted(set((run["agents"][lane_id].get("scope") or []) + s["scope"]))
        progress._save(run, touch_session=False)
    progress._audit("crew.delegate", f"{by} → {m['name']}", task.strip()[:300], project=run.get("project") or "",
                    step=name, routed="yes" if routed else "no")
    from . import crew
    text = f"{crew.who(by)} delegates “{task.strip()[:200]}” to {crew.who(m)} (step “{name}”)"
    if lane_id and s.get("owner") == lane_id:
        progress.crew_ask(text + f". It is in your lane now: record it with dev progress step \"…\" --step \"{name}\" "
                          f"--check \"…\", or your report's ✓ does not close it; if you cannot take it: dev crew "
                          f"decline \"{name}\" --why \"…\"", to=lane_id, from_agent=from_agent, cwd=cwd, now=now,
                          kind="notice")
    elif from_agent:
        progress.crew_ask(text + f". Launch {crew.who(m)} when it is ready: dev crew next", to="main",
                          from_agent=from_agent, cwd=cwd, now=now, kind="notice")
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
    from . import crew
    progress.crew_ask(f"{crew.who(who)} declined “{s['name']}”: {why}. Route it again: dev crew route \"{s['name']}\"",
                      to="main", from_agent=from_agent, cwd=cwd, now=now, kind="notice")
    return s


def _sorted(run, now: float | None = None):
    """The plan's steps for the crew, sorted: (launchable [steps], waiting [(step, why)]). A member is busy while it
    works (progress.working_agents): one that ended or went stale is not."""
    busy = progress.working_agents(run, now)
    launch, waiting = [], []
    for s in run.get("plan") or []:
        if s.get("state") not in ("pending", "blocked") or not s.get("label") or (s.get("owner") or "main") != "main":
            continue
        deps = progress.deps_open(run, s)  # the plan's one dependency rule: done or skipped, or unverified for a review
        if s.get("waiting"):
            waiting.append((s, f"decision {s['waiting']}"))
        elif deps:
            waiting.append((s, "after " + ", ".join(deps)))
        elif any(progress._label_matches(s["label"], v) for _, v in busy):
            from . import crew
            waiting.append((s, f"{crew.who(s['label'])} is busy: send it to them (SendMessage / send_input)"))
        else:
            launch.append(s)
    return launch, waiting


def critical_path(run) -> dict:
    """{step name: (minutes, steps)}: the longest chain of open work from each step through the steps that wait for
    it, by their estimates (the step itself included)."""
    plan = (run or {}).get("plan") or []
    by = {s["name"]: s for s in plan}
    waits = {n: [] for n in by}
    for s in plan:
        for d in s.get("after") or []:
            if d in waits:
                waits[d].append(s["name"])
    memo = {}

    def walk(n):
        if n not in memo:
            memo[n] = (0.0, 0)  # a cycle stops here (the plan is validated acyclic)
            rest = max((walk(t) for t in waits[n] if by[t].get("state") not in progress.DONE), default=(0.0, 0))
            memo[n] = (float(by[n].get("est") or 0) + rest[0], 1 + rest[1])
        return memo[n]
    return {n: walk(n) for n in by}


def order(run, now: float | None = None, sess: dict | None = None) -> list:
    """The steps Morpheus can launch now, in the order he launches them (spec R5) -> [(step, why)]: the longest
    critical path first (the step and what waits for it), then the developer's latest request (a step added in the
    current turn), then the oldest (no step starves), then the plan's order."""
    run = normalize(run)
    cands, _ = _sorted(run, now)
    if not cands:
        return []
    if sess is None:
        sess = core.read_json(progress.session_path(run.get("agent") or "", run.get("session") or ""), {}) or {}
    turn = (sess or {}).get("turn_started")
    chain, index = critical_path(run), {s["name"]: i for i, s in enumerate(run.get("plan") or [])}
    latest = {s["name"]: bool(turn) and progress.is_latest_request(run, sess, s) for s in cands}
    cands.sort(key=lambda s: (-round(chain[s["name"]][0]), not latest[s["name"]], progress._step_added(run, s),
                              index[s["name"]]))
    out = []
    for s in cands:
        mins, n = chain[s["name"]]
        why = [f"critical path: {n} steps, ~{progress.fmt_about(mins)}" if n > 1 else
               f"~{progress.fmt_about(mins)}, no step waits for it"]
        if latest[s["name"]]:
            why.append("the developer's latest request")
        added = progress._step_added(run, s)
        if added:
            why.append(f"waiting since {time.strftime('%H:%M', time.localtime(added))}")
        out.append((s, " · ".join(why)))
    return out


def ready(run, now: float | None = None) -> dict:
    """{"launch": [steps ready to launch, in order], "queued": [steps that wait for a slot], "waiting": [(step,
    why)], "busy": [lanes whose member still works], "free": slots, "limit": n, "slots": progress.slot_budget,
    "why": {step: its place in the order}}: the ready steps in dispatch order, launched while their cost fits the
    limit less the developer's reserve; a step the developer asked for in this turn may take the reserve, and goes
    ahead of queued plan work (spec R3-R5)."""
    run = normalize(run)
    busy = progress.working_agents(run, now)
    sess = core.read_json(progress.session_path(run.get("agent") or "", run.get("session") or ""), {}) or {}
    b = progress.slot_budget(run.get("agent") or "claude", run, sess, now)  # launches count before they start
    free = max(0.0, b["limit"] - b["reserve"] - b["used"])
    _, waiting = _sorted(run, now)
    launch, queued, room, reasons = [], [], free, {}
    for s, why in order(run, now, sess):
        reasons[s["name"]] = why
        cost = progress.step_cost(run, s)
        mine = bool(sess.get("turn_started")) and progress.is_latest_request(run, sess, s)
        if mine and cost <= room + b["reserve"] + 1e-9:
            launch.append(s)
            room -= cost  # below 0 once it took the reserve: nothing else fits
        elif not queued and cost <= room + 1e-9:
            launch.append(s)
            room -= cost
        else:
            queued.append(s)  # in order: a later, smaller step does not pass a queued one
    return {"launch": launch, "queued": queued, "waiting": waiting, "busy": busy,
            "free": int(free) if float(free).is_integer() else round(free, 2), "limit": b["limit"], "slots": b,
            "why": reasons}


def order_why(which: str, why: str, from_agent: str | None = None, cwd: str | None = None,
              now: float | None = None) -> dict:
    """Morpheus launches a step ahead of the recorded order (dev crew next --first <step> --why "…"): the reason is
    kept on the run and audited, and the launch for that step is no longer held back (spec R5). -> the step"""
    now = now or time.time()
    progress._main_only(from_agent, "changes the dispatch order", cwd)
    if not (why or "").strip():
        raise StepError("say why this step goes first: --why \"…\"")
    agent, sid = current_session(cwd)
    with progress._tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        s = _find_step(run, which)
        if not s:
            raise StepError(f"no step “{which}” in the plan")
        ahead = [x["name"] for x, _ in order(run, now)]
        run.setdefault("order_why", {})[s["name"]] = {"why": why.strip()[:300], "ts": round(now),
                                                       "order": ahead[:8]}
        progress._save(run, touch_session=False)
    progress._audit("crew.order-override", s["name"], why.strip()[:300], project=run.get("project") or "",
                    step=s["name"])
    return s


def record_order(run, now: float | None = None, cwd: str | None = None) -> list:
    """`dev crew next` records the order it shows (spec R5): kept on the run and audited when it changed. -> order"""
    ranked = order(run, now)
    names = [s["name"] for s, _ in ranked]
    if names and names != ((run or {}).get("order") or {}).get("steps"):
        agent, sid = run.get("agent") or "", run.get("session") or ""
        try:
            with progress._tx(agent, sid):
                doc = load_run(agent, sid)
                if isinstance(doc, dict) and doc.get("state") != "done":
                    doc["order"] = {"steps": names, "ts": round(now or time.time())}
                    progress._save(doc, touch_session=False)
        except Exception:
            return ranked
        progress._audit("crew.order", names[0], " → ".join(names)[:600], project=run.get("project") or "")
    return ranked


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
    lines = [f"devkit · ready to launch, in this order ({progress.slots_text(r['slots'])}, {mode} "
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
