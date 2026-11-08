# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Branch backups (spec P-fy3bwa0pshk, R1 to R4): copy the commits that exist only on this Mac to the remote, when
that push costs no CI run.

  dev backup [<branch>…] [--all]      the current branch, the named ones, or every branch with commits only here
  dev backup policy [on|off|ask] [-p <project>] --why …   the project's backup policy (shown, or set)
  dev branch embargo <branch> --why … [--until …] · dev branch embargo --lift <branch>
  dev sync (hourly and at session end) backs up every project's branches that are not backed up (backup.auto)

A branch is pushed to its remote under the same name, fast-forward only and never forced, and only when every check
holds, in this order: not the default branch nor a long-lived one (LONG_LIVED), a conforming name, the remote can be
reached (what it has, from `git ls-remote`, decides which commits the push sends: every later check looks at exactly
those, also when another remote has them), not a branch a workflow deploys from, no embargo (on the branch, or on any
of the same commits: those its branch or its recorded tips reach, so they stay held after it is merged elsewhere and
deleted; a handoff note devkit cannot read stops every backup, B2), the remote branch is not ahead or diverged, no WIP
commit, the project's backup policy, no sign of a security fix on a public repository, the CI configuration of the
commit it sends shows that the push starts no CI run (`pipelines.push_ci`), and last the secret scan (gitleaks) passes
on exactly the commits it sends. A check that fails skips the branch with the reason and what to do. `dev sync` only
takes commits older than `backup.after_minutes` (a younger commit may still be amended); `dev backup` takes them all.
Each push is audited as `git.backup` (branch, remote, commit range) and each skip as `git.backup-skipped` (the reason,
which the branch map shows).

The backup policy (the developer's decision, 2026-11-06; branches.py keeps it in the handoff note): on, off or ask
per project, by default on for a private repository and ask for a public one (or one whose visibility devkit cannot
read), because a backup of a public repository publishes the branch at once. With ask, `dev backup` asks the
developer once, in their own terminal and never in an agent's call, and records the answer as on or off; `dev sync`
never asks and never publishes a branch the policy does not allow. `dev backup --public` pushes once whatever the
policy (and a branch that reads as a security fix). Setting the policy and `--public` are the developer's own
actions: devkit refuses them from a crew member's lane, and the guard asks the main session for the developer's
grant. The policy is audited as `dev.policy` (target backup/<project>).

`run()` returns one result per branch: {branch, remote, state, reason, fix, commit, range, count}, where state is
pushed, would_push (dry run), skipped (a check failed), nothing (no commit only here) or waiting (commits too young).
Python 3.9 compatible, stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from . import core, ui

WIP_RE = re.compile(r"(?i)^(wip\b|fixup!|squash!|amend!)")
# a commit that looks like a security fix: on a public repository, devkit suggests an embargo before it pushes (R3)
SECURITY_SUBJECT = re.compile(r"(?i)^\w+\((security|sec|vuln)\)!?:|\b(vulnerabilit|cve-\d{4}-|exploit|security fix)")
RED_TEAM_FILE = re.compile(r"(?i)(^|/)[^/]*(red[-_]?team|adversarial)[^/]*$")
SKIP_RECORD_HOURS = 24  # the same skip reason is audited again only after this long
PUSHED, WOULD, SKIPPED, NOTHING, WAITING = "pushed", "would_push", "skipped", "nothing", "waiting"
# long-lived branches a backup never pushes, whatever the default branch is: a push there publishes or deploys
LONG_LIVED = ("main", "master", "develop", "trunk", "gh-pages")
# a workflow step that deploys or publishes: with an `environment:`, it makes a branch one "a workflow deploys from"
DEPLOY_STEP = re.compile(r"(?i)deploy|gh-pages|pages-artifact|publish|release-action")


def auto_enabled(cfg=None) -> bool:
    """`backup.auto` (default true): `dev sync` backs up the branches by itself."""
    v = ((cfg if cfg is not None else core.CFG).get("backup") or {}).get("auto", True)
    return v is not False


def _sha(top, ref) -> str:
    return core.gitout(top, "rev-parse", "--verify", "-q", ref + "^{commit}")


def _commits(top, tip, has=None) -> list:
    """The commits reachable from `tip` that the push target lacks, newest first: none of `has` (the commits it has
    that this repository knows) reaches them; has=None means no remote ref here reaches them."""
    if has is None:
        out = core.gitout(top, "rev-list", tip, "--not", "--remotes", timeout=60)
        return [x for x in out.split() if x]
    rc, out, err = core.sh(["git", "-C", str(top), "rev-list", tip, "--stdin"], input="".join(f"^{h}\n" for h in has),
                           timeout=60)
    if rc != 0:
        raise RuntimeError(f"git rev-list failed: {(err or out).strip()[:120]}")
    return [x for x in out.split() if x]


def _advertised(c, remote, ctx):
    """What the push target has: {ref: sha} of its branches and tags (git ls-remote, once per remote), and the
    commits among them this repository knows -> (refs, known shas, "") or (None, None, why it cannot be reached)."""
    if remote not in ctx["adv"]:
        rc, out, err = core.git(c.top, "ls-remote", "--heads", "--tags", remote, timeout=60)
        if rc != 0:
            text = (err or out).strip().splitlines()
            ctx["adv"][remote] = (None, None, (text[-1] if text else "no answer")[:100])
        else:
            refs = {}
            for ln in out.splitlines():
                parts = ln.split()
                if len(parts) == 2:
                    refs[parts[1]] = parts[0]
            ctx["adv"][remote] = (refs, _known(c.top, refs.values()), "")
    return ctx["adv"][remote]


def _known(top, shas) -> list:
    """The objects among `shas` that exist in this repository (one git cat-file call)."""
    shas = sorted(set(shas))
    if not shas:
        return []
    rc, out, _ = core.sh(["git", "-C", str(top), "cat-file", "--batch-check"], input="".join(x + "\n" for x in shas),
                         timeout=60)
    return [ln.split()[0] for ln in out.splitlines() if ln.strip() and not ln.endswith(" missing")] if rc == 0 else []


def _result(branch, remote, state, reason="", fix=None, **kw) -> dict:
    return {"branch": branch, "remote": remote, "state": state, "reason": reason, "fix": list(fix or []),
            "commit": kw.get("commit"), "range": kw.get("range"), "count": kw.get("count", 0)}


def scan(top, tip: str, has=None) -> tuple:
    """gitleaks on exactly the commits a push of `tip` sends (none of `has`, the commits the push target has, reaches
    them; has=None: no remote ref here) -> (True, []) clean, (False, findings) or (None, why)."""
    import subprocess
    if not core.have("gitleaks"):
        return None, "the secret scan needs gitleaks, which is not installed (brew install gitleaks)"
    opts = f"{tip} --not --remotes" if has is None else " ".join([tip] + ["^" + h for h in has])
    args = ["gitleaks", "git", "--no-banner", "--redact", "--exit-code", "3", "--report-format", "json",
            "--report-path", "-", f"--log-opts={opts}", "."]
    try:
        p = subprocess.run(args, cwd=str(top), capture_output=True, text=True, timeout=180, env=core.GIT_ENV)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"the secret scan did not finish ({e})"
    if p.returncode == 0:
        return True, []
    if p.returncode != 3:
        last = ((p.stderr or p.stdout).strip().splitlines() or ["no output"])[-1]
        return None, "the secret scan failed: " + last[:160]
    try:
        found = [f"{x.get('RuleID')} in {x.get('File')}:{x.get('StartLine')} ({str(x.get('Commit'))[:7]})"
                 for x in json.loads(p.stdout or "[]")]
    except ValueError:
        found = []
    return False, found or ["secret-like content"]


def _security_hint(top, commits: list) -> str:
    """Why the commits look like a security fix (R3), or ''."""
    for sha in commits[:200]:
        subject = core.gitout(top, "log", "-1", "--format=%s", sha)
        if SECURITY_SUBJECT.search(subject):
            return f"commit {sha[:7]} reads as a security fix: {subject[:60]}"
    files = core.gitout(top, "log", "--format=", "--name-only", "--no-walk=unsorted", *commits[:200]) if commits else ""
    hit = next((f.strip() for f in files.splitlines() if RED_TEAM_FILE.search(f.strip())), "")
    return f"it changes a red-team test, {hit}" if hit else ""


def _github(top, remote):
    """`[host/]owner/repo` of the GitHub repository a remote names (its configured URL, as written), or None."""
    from . import cistatus
    prov = cistatus.provider_for(core.gitout(top, "config", "--get", f"remote.{remote}.url"))
    if not prov or prov[0] != "github":
        return None
    return prov[2] if prov[1] == "github.com" else f"{prov[1]}/{prov[2]}"


def _visibility(top, remote="origin"):
    """The visibility of the repository the push goes to (the remote's, not origin's): "public", "private",
    "internal" or None (unknown, or not on GitHub)."""
    try:
        from . import gitx
        slug = _github(top, remote)
        if not slug:
            return None
        rc, out, _ = gitx.GH(["repo", "view", slug, "--json", "visibility", "-q", ".visibility"], cwd=str(top),
                             timeout=15)
        v = out.strip().lower() if rc == 0 else ""
        return v if v in ("public", "private", "internal") else None
    except Exception:
        return None


def _public(c, remote, ctx) -> bool:
    """Is the repository the push goes to public? Unknown counts as public: the push would publish the commits."""
    if remote not in ctx["vis"]:
        ctx["vis"][remote] = _visibility(c.top, remote)
    return ctx["vis"][remote] not in ("private", "internal")


def _prs_of(c, remote, ctx):
    """The open pull requests of the repository the push goes to -> (list, known). origin's come from the caller."""
    slug = _github(c.top, remote)
    if remote == "origin" or (slug and slug == c.slug):
        return ctx["prs"], ctx["prs_known"]
    if not slug:
        return [], False
    if slug not in ctx["remote_prs"]:
        from . import gitx
        data, _ = gitx.gh_json(["pr", "list", "-R", slug, "--state", "open", "--limit", "200", "--json",
                                "number,isDraft,url,headRefName,state"], c, timeout=30)
        ctx["remote_prs"][slug] = (data, True) if isinstance(data, list) else ([], False)
    return ctx["remote_prs"][slug]


def _push_reaches(push, name) -> bool:
    """Whether a workflow's `push` trigger can run for a push of branch `name`: with no filter it runs for every
    branch; only a branches filter that leaves it out, a branches-ignore filter that names it, or a tag filter alone
    rules it out. A paths filter still counts as running, since the deploy check stays on the safe side."""
    cfg = push if isinstance(push, dict) else {}
    from . import pipelines
    if "branches" in cfg:
        return pipelines._gh_filter(cfg["branches"], name)
    if "branches-ignore" in cfg:
        return not pipelines._gh_filter(cfg["branches-ignore"], name)
    return not ("tags" in cfg or "tags-ignore" in cfg)


def _deploys_from(top, name, sha) -> str:
    """Why a GitHub Actions workflow of the commit to push deploys from `name`, or "": its push trigger runs for the
    branch (named in `branches:`, or any branch when nothing filters it out, see _push_reaches), and one of its jobs
    has an `environment:` or a deploy or publish step."""
    import tempfile
    from . import pipelines
    files = [f for f in core.gitout(top, "ls-tree", "-r", "--name-only", sha, "--", ".github/workflows").splitlines()
             if f.endswith((".yml", ".yaml"))]
    if not files:
        return ""
    with tempfile.TemporaryDirectory(prefix="devkit-deploy-") as tmp:
        if not pipelines._materialize(top, sha, files, Path(tmp)):
            return f"devkit could not read the workflows of {sha[:7]}, so it cannot rule out a deploy"
        pls = pipelines.build(tmp)
    for pl in pls:
        tree = pl.get("tree")
        if pl.get("provider") != "github" or not isinstance(tree, dict):
            continue
        events = pipelines._gh_events(tree.get("on", tree.get(True)))
        if "push" not in events or not _push_reaches(events["push"], name):
            continue
        for jid, job in (tree.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            env = job.get("environment")
            env = env.get("name") if isinstance(env, dict) else env
            step = next((str(st["uses"]).split("@")[0] for st in job.get("steps") or []
                         if isinstance(st, dict) and DEPLOY_STEP.search(str(st.get("uses") or ""))), "")
            if env or step:
                what = f"environment {env}" if env else f"step {step}"
                return f"{Path(pl['file']).name} deploys from it (job {jid}, {what}): a push to it may deploy"
    return ""


def _remote_id(top, remote) -> str:
    """The repository a remote names, as host/owner/repo (its URL when devkit cannot read it as one)."""
    url = core.gitout(top, "config", "--get", f"remote.{remote}.url")
    parsed = core.parse_remote(url) if url else None
    return "/".join(str(x) for x in parsed).lower() if parsed else url


def developer_here() -> bool:
    """True when the developer runs this command in their own terminal and types the answer: stdin is a terminal and
    no agent runs it (core.set_by_developer). Piped input and an agent's call never count."""
    try:
        tty = bool(sys.stdin) and sys.stdin.isatty()
    except (AttributeError, ValueError):
        tty = False
    return tty and core.set_by_developer()


def crew_member() -> str:
    """The crew member whose lane runs this command, or "" (the developer, or the main session)."""
    try:
        from . import audit
        return str(((audit.context() or {}).get("crew") or {}).get("member") or "")
    except Exception:
        return ""


def set_policy(pid, policy, why, branch="", visibility=None) -> dict:
    """Record the project's backup policy in its handoff note, with the visibility of the repository it is decided
    for, audit it (`dev.policy`, target backup/<project>) and share it soon."""
    from . import audit, branches, work
    rec = branches.set_backup_policy(pid, policy, why, work.actor()["user"], visibility=visibility)
    audit.record("dev.policy", f"backup/{pid}", f"{policy}: {why}", "ok", project=pid, branch=branch or None)
    branches._share(pid, "backup policy of")
    return rec


def _ask_policy(pid, rid, vis):
    """Ask the developer whether this project's branches may be backed up -> True, False, or None (no answer)."""
    what = "public" if vis == "public" else "not known to be private"
    print(f"{ui.WARN} {rid} is {what}: a backup publishes each branch there at once, for anyone to read.",
          file=sys.stderr)
    try:
        ans = input(f"  Back up {pid}'s branches there, now and with the hourly dev sync? A no keeps them on this "
                    f"Mac. [y/N] ")
    except (EOFError, KeyboardInterrupt):
        return None
    return ans.strip().lower() in ("y", "yes")


def _policy_gate(c, name, remote, ctx):
    """The project's backup policy (decided 2026-11-06): on pushes, off never does, ask waits for the developer, whom
    `dev backup` asks once, in a terminal, recording the answer as on or off; `dev sync` and agents are never asked.
    `dev backup --public` (the developer's own action) pushes this once, whatever the policy. -> a skip, or None."""
    from . import branches
    _public(c, remote, ctx)
    vis, rec = ctx["vis"][remote], ctx["policy"]
    policy = rec["policy"] if rec else branches.default_policy(vis)
    ctx["policy_words"] = branches.policy_words(rec, vis)
    outgrown = branches.outgrown_on(rec, vis)  # on, decided while the repository was private: ask again
    if outgrown:
        policy = "ask"
    if policy == "on" or ctx["public_ok"]:
        return None
    if policy == "ask" and not ctx["auto"] and not ctx["dry_run"] and not ctx["asked"] and developer_here():
        ctx["asked"] = True
        yes = _ask_policy(c.pid, _remote_id(c.top, remote), vis)
        if yes is not None:
            rec = ctx["policy"] = set_policy(c.pid, "on" if yes else "off", "the developer's answer to dev backup",
                                             name, visibility=vis)
            ctx["policy_words"] = branches.policy_words(rec, vis)
            if yes:
                return None
            policy = "off"
    if policy == "off":
        return _result(name, remote, SKIPPED, f"the backup policy of {c.pid} is {ctx['policy_words']}", [
            'dev backup policy on --why "…"   (the developer: back up this project\'s branches)',
            f"dev backup --public {name}   (the developer: this once)"])
    if outgrown:
        then = rec.get("visibility") or "of a visibility devkit did not record"
        now = vis if vis in ("public", "private", "internal") else "of a visibility devkit cannot read"
        return _result(name, remote, SKIPPED, f"the backup policy of {c.pid} is on, but it was set while the "
                       f"repository was {then}, and it is {now} now: the developer has not answered again", [
                           "dev backup, run by the developer in a terminal, asks once (agents are never asked, dev "
                           "sync waits for the answer)",
                           'dev backup policy on --why "…"   (or off: the developer\'s choice for this project)'])
    return _result(name, remote, SKIPPED, f"the backup policy of {c.pid} is {ctx['policy_words']}, and the developer "
                   f"has not answered", [
                       "dev backup, run by the developer in a terminal, asks once (agents are never asked, dev sync "
                       "waits for the answer)",
                       'dev backup policy on --why "…"   (or off: the developer\'s choice for this project)'])


def _embargo_holder(top, name, mine: set, held: dict, local: set, has=None):
    """The embargo that holds any of `mine` (this branch's, or one whose branch or recorded tips reach the same
    commits, also after that branch was merged elsewhere and deleted; `has`: the commits the push target has, so a
    commit another remote has stays held) -> (branch, until) or None."""
    from . import branches
    if name in held:
        return name, held[name].get("until") or ""
    for other, e in held.items():
        held_here = branches.held_commits(top, other, e, local, list(has) if has is not None else ("--remotes",))
        if other != name and mine & held_here:
            return other, e.get("until") or ""
    return None


def _diverged_fix(name, remote) -> list:
    return [f"git fetch {remote}", f"git log --oneline --left-right {name}...{remote}/{name}   (what each side has)",
            f"git switch {name} && git rebase {remote}/{name}   (keep the remote commits), then dev backup {name}",
            "dev push --force-with-lease   (or replace them: your own branch only)"]


def _open_prs(c):
    """The repository's open pull requests (gh) -> (list, known)."""
    if c.provider != "github" or not c.slug:
        return [], False
    from . import gitx
    data, _ = gitx.gh_json(["pr", "list", "-R", c.slug, "--state", "open", "--limit", "200", "--json",
                            "number,isDraft,url,headRefName,state"], c, timeout=30)
    return (data, True) if isinstance(data, list) else ([], False)


def _record(pid, res: dict, last_skip=None) -> None:
    from . import audit
    target = f"{res['remote']}/{res['branch']}" if res["remote"] else res["branch"]
    if res["state"] == PUSHED:
        n = res["count"]
        audit.record("git.backup", target, f"{res['range']} ({n} commit{'s' if n != 1 else ''})", "ok", project=pid,
                     branch=res["branch"], commit=res["commit"], count=n, category="security", severity="notice")
    elif res["state"] == SKIPPED:
        if last_skip and last_skip[1] == res["reason"] and time.time() - last_skip[0] < SKIP_RECORD_HOURS * 3600:
            return  # the same reason an hour later says nothing new
        audit.record("git.backup-skipped", target, res["reason"], "recorded", project=pid, branch=res["branch"],
                     category="security", severity="info")


def _remote_of(c, name) -> str:
    up = core.gitout(c.top, "config", "--get", f"branch.{name}.remote")
    if up and up != ".":
        return up
    return "origin" if c.has_origin else (core.gitout(c.top, "remote").split() or [""])[0]


def _target(c, name, tip, ctx) -> str:
    """The commit to push: the tip, or for the scheduled sync the newest commit older than backup.after_minutes."""
    if not ctx["auto"]:
        return tip
    before = int(ctx["now"] - ctx["after"] * 60)
    return core.gitout(c.top, "rev-list", "-1", "--first-parent", f"--before={before}", tip) or ""


def _ci_gate(c, name, target, remote_sha, ctx):
    """The CI check (R1) -> (push_ci result, skip result or None)."""
    from . import pipelines
    top, remote = c.top, _remote_of(c, name)
    prs, known = _prs_of(c, remote, ctx)  # the pull requests of the repository the push goes to
    pr = pipelines.open_pr(prs, name)
    base = next((f"refs/remotes/{r}/{c.default}" for r in (remote, "origin")
                 if _sha(top, f"refs/remotes/{r}/{c.default}")), c.default)
    ci = pipelines.push_ci(top, name, target, remote_sha=remote_sha or None, base=base, pr=pr, prs_known=known)
    if ci["starts"] is False or ctx["accept_ci"]:
        return ci, None
    if ci["starts"]:
        why = f"a push to this branch starts CI ({ci['why']})"
        fix = [f"dev backup --accept-ci {name}   (push anyway: one CI run)"] + ([ci["fix"]] if ci["fix"] else [])
    else:
        why = f"devkit cannot tell whether a push starts CI ({ci['why']})"
        fix = [f"dev backup --accept-ci {name}   (push anyway, accepting a possible CI run)"]
    return ci, _result(name, _remote_of(c, name), SKIPPED, why, fix)


def _one(c, name, ctx) -> dict:
    """Back up one branch (the checks in the module docstring, in that order)."""
    from . import guard
    top = c.top
    tip = _sha(top, f"refs/heads/{name}")
    remote = _remote_of(c, name)
    if not tip:
        return _result(name, remote, SKIPPED, f"there is no local branch {name}", ["dev branch ls"])
    if name == c.default:
        return _result(name, remote, SKIPPED, f"{name} is the default branch, which is never backed up",
                       ["dev branch new <type>/<topic>   (the commits come along), then dev backup"])
    if name in LONG_LIVED:
        return _result(name, remote, SKIPPED, f"{name} is a long-lived branch, which a backup never pushes",
                       ["dev branch new <type>/<topic>   (the commits come along), then dev backup"])
    if guard.branch_violation(name):
        return _result(name, remote, SKIPPED, "its name breaks the branch naming rule",
                       [f"git switch {name} && dev branch rename <type>/<topic>"])
    if not remote:
        return _result(name, "", SKIPPED, "no remote is configured", ["git remote add origin <url>"])
    merge = core.gitout(top, "config", "--get", f"branch.{name}.merge")
    if merge and merge != f"refs/heads/{name}":
        return _result(name, remote, SKIPPED, f"its upstream is {remote}/{merge.replace('refs/heads/', '')}, not "
                       f"{name}", ["dev push   (pushes to the upstream it has)"])
    # what the push target has (its own refs, not every remote's): the commits it lacks are exactly what the push
    # sends, so the embargo, WIP, security and secret checks look at those
    refs, has, why = _advertised(c, remote, ctx)
    if refs is None:
        return _result(name, remote, SKIPPED, f"{remote} cannot be reached ({why})",
                       [f"git ls-remote {remote}   (check the network and the credentials), then dev backup {name}"])
    remote_sha = refs.get(f"refs/heads/{name}", "")
    if remote_sha and not _sha(top, remote_sha):
        core.git(top, "fetch", "--no-tags", "-q", remote, f"+refs/heads/{name}:refs/remotes/{remote}/{name}",
                 timeout=120)
        if not _sha(top, remote_sha):  # without its commits devkit cannot tell ahead from diverged
            return _result(name, remote, SKIPPED, f"devkit could not fetch {remote}/{name}",
                           [f"git fetch {remote} {name}, then dev backup {name}"])
        has = sorted(set(has) | {remote_sha})
    target = _target(c, name, tip, ctx)
    mine = _commits(top, target, has) if target else []
    if not mine:
        if _commits(top, tip, has):
            return _result(name, remote, WAITING, f"commits younger than {ctx['after']} min wait for the backup")
        return _result(name, remote, NOTHING, f"{remote} has every commit already" if remote_sha else
                       f"its commits are on {remote} already")
    deploy = _deploys_from(top, name, target)
    if deploy:  # never, not even with --accept-ci or --public
        return _result(name, remote, SKIPPED, deploy, [f"dev push   (with local CI evidence: a push to {name} is a "
                                                       f"release step, not a backup)"])
    held = _embargo_holder(top, name, set(mine), ctx["held"], ctx["local"], has)
    if held:
        why = ("embargoed" if held[0] == name else f"its commits are in {held[0]}, which is embargoed") + (
            f' until "{held[1]}"' if held[1] else "")
        return _result(name, remote, SKIPPED, why, [f"dev branch embargo --lift {held[0]}   (when it may be public)"])
    if remote_sha and core.git(top, "merge-base", "--is-ancestor", remote_sha, target)[0] != 0:
        return _result(name, remote, SKIPPED, f"{remote}/{name} has diverged from {name} (commits on both sides)",
                       _diverged_fix(name, remote))
    wip = [s for s in mine if WIP_RE.match(core.gitout(top, "log", "-1", "--format=%s", s))]
    if wip:
        return _result(name, remote, SKIPPED, f"WIP commit {wip[-1][:7]} never reaches the remote",
                       ["dev commit --unwip, then commit properly; the next backup takes it"])
    start = remote_sha or core.gitout(top, "rev-parse", "--verify", "-q", mine[-1] + "^") or "root"
    rng = f"{start[:12]}..{target[:12]}"
    skip = _policy_gate(c, name, remote, ctx)
    if skip:
        return skip
    hint = _security_hint(top, mine)
    if hint and not ctx["public_ok"] and _public(c, remote, ctx):
        return _result(name, remote, SKIPPED, f"it may carry a security fix ({hint}), and a push publishes it", [
            f'dev branch embargo {name} --why "<the open issue>" --until "<the step or date it may be public>"',
            f"dev backup {name} --public   (it is not a security fix, or it may be public now)"])
    ci, skip = _ci_gate(c, name, target, remote_sha, ctx)
    if skip:
        return skip
    clean, found = scan(top, target, has)  # last before the push: on exactly the commits it sends
    if clean is None:
        return _result(name, remote, SKIPPED, found, [
            "brew install gitleaks" if "not installed" in found else
            f"gitleaks git --log-opts='{target[:12]} --not --remotes={remote}' ."])
    if not clean:
        return _result(name, remote, SKIPPED, f"the secret scan found {len(found)} finding(s): {'; '.join(found[:3])}",
                       [f"remove the secret from those commits and rotate it, then dev backup {name}",
                        "a false positive: ask the main session to allow it (.gitleaksignore)"])
    res = _result(name, remote, WOULD, ci["why"] if ci["starts"] is False else "you accepted the CI run",
                  commit=target, range=rng, count=len(mine))
    if ctx["dry_run"]:
        return res
    if ci["starts"] is not False:  # an accepted CI run counts against the push budget like any other (R5)
        v = guard.check_push_budget(top, [remote, f"{target}:{name}"], frozenset())
        if v:
            return _result(name, remote, SKIPPED, "the push budget for this hour is spent",
                           ["dev ci --run, then dev push"])
    env = dict(core.GIT_ENV)
    if ctx["auto"] and not env.get("GIT_SSH_COMMAND"):
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"  # the scheduled sync never waits for a passphrase
    rc, out, err = core.sh(["git", "-C", top, "push", "--porcelain", remote, f"{target}:refs/heads/{name}"],
                           timeout=300, env=env)
    if rc != 0:
        text = (err or out).strip().splitlines()
        return _result(name, remote, SKIPPED, f"git push failed: {(text[-1] if text else 'no output')[:160]}",
                       [f"git push {remote} {name}   (to see the whole error)"])
    if core.gitout(top, "config", "--get", f"branch.{name}.remote") != remote:
        core.git(top, "branch", f"--set-upstream-to={remote}/{name}", name)
    res["state"] = PUSHED
    return res


def run(repo, names=None, every: bool = False, auto: bool = False, accept_ci: bool = False, dry_run: bool = False,
        public_ok: bool = False, prs=None, prs_known=None, now: float = None, record: bool = True) -> list:
    """Back up branches of the repository at `repo` -> one result per branch (see the module docstring).

    names: the branches (default: the current one); every: every local branch with commits only on this Mac; auto:
    the scheduled sync (only commits older than backup.after_minutes, never --accept-ci or --public). prs: the
    repository's open pull requests (`gh pr list` rows; None looks them up); prs_known: whether that list is
    complete."""
    from . import branches, gitx
    c = gitx.ctx(str(repo))
    if c is None:
        return []
    local = [x for x in core.gitout(c.top, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines() if x]
    if every:
        names = [n for n in local if n != c.default and _commits(c.top, f"refs/heads/{n}")]
    elif not names:
        names = [c.branch] if c.branch else []
    held, unreadable = branches.read_embargoes(c.pid)
    if held is None:  # B2: a note devkit cannot read means no backup, never "no embargo"
        out = [_result(n, _remote_of(c, n), SKIPPED, f"devkit cannot read the handoff note ({unreadable}), so it "
                       f"cannot tell which branches are embargoed", [
                           f"fix the note ({core.tilde(core.note_path(c.pid))}) or the handoff repository (dev "
                           f"doctor), then dev backup"]) for n in names]
        for res in out if record and not dry_run else ():
            _record(c.pid, res)
        return out
    moved = {b: _sha(c.top, f"refs/heads/{b}") for b in held if b in local}
    moved = {b: t for b, t in moved.items() if t and t not in held[b]["commits"]}
    if moved and not dry_run:  # the embargo follows the branch's new commits, so they stay held after a merge
        branches.record_tips(c.pid, moved)
        for b, t in moved.items():
            held[b]["commits"].append(t)
    if prs is None:
        prs, known = _open_prs(c)
        prs_known = known if prs_known is None else prs_known
    ctx = {"auto": auto, "accept_ci": accept_ci and not auto, "dry_run": dry_run, "public_ok": public_ok and not auto,
           "now": now or time.time(), "after": branches.after_minutes(), "held": held,
           "local": set(local), "prs": prs or [], "prs_known": bool(prs_known), "vis": {},
           "policy": branches.backup_policy(c.pid), "asked": False, "policy_words": "", "adv": {}, "remote_prs": {}}
    last = branches.backups(c.pid) if record and not dry_run else {}
    out = []
    for name in names:
        try:
            res = _one(c, name, ctx)
        except Exception as e:  # one broken branch must not stop the others
            res = _result(name, "", SKIPPED, f"devkit failed on it ({type(e).__name__}: {e})"[:200])
        if record and not dry_run:
            _record(c.pid, res, (last.get(name) or {}).get("skipped"))
        res["policy"] = ctx["policy_words"]
        out.append(res)
    return out


def lines(results, verbose: bool = True) -> list:
    """The results in words: one line per branch, with what to do under a skipped one."""
    out = []
    for r in results:
        b, st = r["branch"], r["state"]
        if st in (PUSHED, WOULD):
            n = r["count"]
            head = (f"{ui.OK} {b} → {r['remote']}" if st == PUSHED else
                    f"{ui.INFO} dry run: would push {b} → {r['remote']}")
            out.append(f"{head}: {n} commit{'s' if n != 1 else ''} ({r['range']}) · {r['reason']}")
        elif st == SKIPPED:
            out.append(f"{ui.WARN} {b} not backed up: {r['reason']}")
            if verbose:
                out += ["    " + ui.dim(x) for x in r["fix"]]
        else:
            out.append(f"  · {b}: " + ("nothing to back up, " if st == NOTHING else "") + r["reason"])
    return out


def register(p) -> None:
    """The arguments of `dev backup` on its argparse subparser (cli.py's build_parser calls this)."""
    p.add_argument("branches", nargs="*", help="the branches (default: the current one), or: policy [on|off|ask]")
    p.add_argument("--all", action="store_true", help="every local branch with commits only on this Mac")
    p.add_argument("--accept-ci", action="store_true", help="push although the push starts (or may start) CI")
    p.add_argument("--public", action="store_true", help="the developer's own action: push this once, whatever "
                   "the backup policy, and a branch that looks like a security fix")
    p.add_argument("--dry-run", action="store_true", help="every check, no push (with policy: nothing is written)")
    p.add_argument("-p", "--project", help="with policy: the project (default: the repository you are in)")
    p.add_argument("--why", help="with policy: why the project chooses it (needed to set one)")
    p.add_argument("--json", action="store_true")


def _refuse(text) -> int:
    print(f"{ui.WARN} {text}", file=sys.stderr)
    return 2


OWN_ACTION = ("{what} is the developer's own decision: they type it in a terminal, or grant the main session once "
              "(grant backup <project>). A crew member never runs it: put the exact command and why in your report.")


def cmd_policy(a) -> int:
    """`dev backup policy [on|off|ask] [-p <project>] --why "…" [--dry-run] [--json]`: show or set the policy."""
    from . import branches, gitx
    args = list(a.branches or [])[1:]
    c = gitx.ctx()
    pid = (getattr(a, "project", None) or "").strip().strip("/") or (c.pid if c else "")
    if not pid:
        return _refuse("name the project: dev backup policy [on|off|ask] -p <owner/repo>, or run it in the repository")
    here = c is not None and c.pid == pid
    if not here and not core.note_path(pid).exists():
        return _refuse(f"{pid} has no handoff note: check the project id (dev overview lists them)")
    if not args:
        rec = branches.backup_policy(pid)
        vis = _visibility(c.top) if here else "?"
        if getattr(a, "json", False):
            print(json.dumps({"project": pid, "policy": rec["policy"] if rec else
                              (branches.default_policy(vis) if vis != "?" else None), "recorded": rec,
                              "visibility": None if vis == "?" else vis}, indent=2, ensure_ascii=False))
            return 0
        print(f"{ui.INFO} {pid}: backup policy {branches.policy_words(rec, vis)}")
        print("  " + ui.dim("on: dev backup and the hourly dev sync push its branches · off: never · ask: dev backup "
                            "asks you once"))
        print("  " + ui.dim(f'set it: dev backup policy on|off|ask{"" if here else " -p " + pid} --why "…"'))
        return 0
    if len(args) != 1 or args[0] not in branches.POLICIES:
        return _refuse(f"the backup policy is one of {', '.join(branches.POLICIES)}: "
                       'dev backup policy on --why "…"')
    if crew_member():
        return _refuse(OWN_ACTION.format(what=f"dev backup policy {args[0]}"))
    why = (getattr(a, "why", None) or "").strip()
    if not why:
        return _refuse(f'say why: dev backup policy {args[0]} --why "…" (the team sees it)')
    if a.dry_run:
        print(f"{ui.INFO} dry run: {pid}'s backup policy would be {args[0]} ({why})")
        return 0
    top = c.top if here else core.DEV / pid  # the main checkout of a project named with -p
    rec = set_policy(pid, args[0], why, visibility=_visibility(top) if Path(top, ".git").exists() else None)
    print(f"{ui.OK} {pid}: backup policy {branches.policy_words(rec)}")
    return 0


def cmd_backup(a) -> int:
    """`dev backup [<branch>…] [--all] [--accept-ci] [--public] [--dry-run] [--json]`, or `dev backup policy …`."""
    from . import gitx
    if list(a.branches or [])[:1] == ["policy"]:  # never a branch: names follow <type>/<topic>
        return cmd_policy(a)
    if getattr(a, "project", None) or getattr(a, "why", None):
        return _refuse("-p and --why go with dev backup policy")
    if a.public and crew_member():
        return _refuse(OWN_ACTION.format(what="dev backup --public"))
    c = gitx.ctx()
    if c is None:
        print(f"{ui.WARN} not inside a git repository", file=sys.stderr)
        return 2
    res = run(c.top, names=list(a.branches or []), every=bool(a.all), accept_ci=bool(a.accept_ci),
              dry_run=bool(a.dry_run), public_ok=bool(a.public))
    if getattr(a, "json", False):
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 1 if any(r["state"] == SKIPPED for r in res) else 0
    print("\n".join(lines(res) or [f"{ui.INFO} no branch to back up"]))
    words = next((r["policy"] for r in res if r.get("policy")), "")
    if words:
        print("  " + ui.dim(f"Backup policy of {c.pid}: {words} (dev backup policy)."))
    return 1 if any(r["state"] == SKIPPED for r in res) else 0


# --------------------------------------------------------------------------- dev sync (R4)


def sync(reg: dict, quiet: bool = True) -> list:
    """The automatic backup of `dev sync`: every project's branches that the branch map shows as not backed up. The
    projects whose branches moved get fresh branch rows in `reg`. -> the results that pushed or skipped a branch."""
    if not auto_enabled():
        return []
    done = []
    for p in reg.get("projects") or []:
        rows = p.get("branches") or []
        due = [r["name"] for r in rows
               if (r.get("backup") or {}).get("state") == "not_backed_up" and not r.get("default")]
        if not due or not p.get("path") or not os.path.isdir(p["path"]):
            continue
        prs = [{"headRefName": x.get("branch"), "number": x.get("number"), "isDraft": bool(x.get("draft")),
                "url": x.get("url"), "state": "OPEN"} for x in p.get("prs_open") or []]
        try:
            res = run(p["path"], names=due, auto=True, prs=prs, prs_known=bool(p.get("prs_known")))
        except Exception as e:
            if not quiet:
                ui.warn(f"backup {p.get('id')}: {e}")
            continue
        moved = [r for r in res if r["state"] in (PUSHED, SKIPPED)]
        done += [dict(r, project=p.get("id")) for r in moved]
        if any(r["state"] == PUSHED for r in res):
            try:
                from . import branches
                checkouts = [p.get("main") or {}] + [w for w in p.get("worktrees") or [] if not w.get("missing")]
                p["branches"] = branches.map(Path(p["path"]), prs=None, offline=True, pid=p.get("id"),
                                             default=p.get("default_branch"), checkouts=checkouts)
            except Exception:
                pass
        if not quiet and moved:
            print("\n".join(f"  {p.get('id')}: {x.strip()}" for x in lines(moved, verbose=False)))
    return done
