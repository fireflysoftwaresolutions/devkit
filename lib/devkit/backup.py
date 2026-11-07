# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Branch backups (spec P-fy3bwa0pshk, R1 to R4): copy the commits that exist only on this Mac to the remote, when
that push costs no CI run.

  dev backup [<branch>…] [--all]      the current branch, the named ones, or every branch with commits only here
  dev branch embargo <branch> --why … [--until …] · dev branch embargo --lift <branch>
  dev sync (hourly and at session end) backs up every project's branches that are not backed up (backup.auto)

A branch is pushed to its remote under the same name, fast-forward only and never forced, and only when every check
holds, in this order: not the default branch, a conforming name, no embargo (on the branch, or on a branch that
holds any of the same commits: a merged branch defers to the branch that carries its commits), the remote branch is
not ahead or diverged, no WIP commit, no sign of a security fix on a public repository, the CI configuration of the
commit it sends shows that the push starts no CI run (`pipelines.push_ci`), and last the secret scan (gitleaks)
passes on exactly the commits it sends. A check that fails
skips the branch with the reason and what to do. `dev sync` only takes commits older than `backup.after_minutes`
(a younger commit may still be amended); `dev backup` takes them all. Each push is audited as `git.backup` (branch,
remote, commit range) and each skip as `git.backup-skipped` (the reason, which the branch map shows).

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


def auto_enabled(cfg=None) -> bool:
    """`backup.auto` (default true): `dev sync` backs up the branches by itself."""
    v = ((cfg if cfg is not None else core.CFG).get("backup") or {}).get("auto", True)
    return v is not False


def _sha(top, ref) -> str:
    return core.gitout(top, "rev-parse", "--verify", "-q", ref + "^{commit}")


def _commits(top, tip, *exclude) -> list:
    """The commits reachable from `tip` and from none of `exclude` (default: no remote ref), newest first."""
    out = core.gitout(top, "rev-list", tip, "--not", *(exclude or ("--remotes",)), timeout=60)
    return [x for x in out.split() if x]


def _result(branch, remote, state, reason="", fix=None, **kw) -> dict:
    return {"branch": branch, "remote": remote, "state": state, "reason": reason, "fix": list(fix or []),
            "commit": kw.get("commit"), "range": kw.get("range"), "count": kw.get("count", 0)}


def scan(top, tip: str) -> tuple:
    """gitleaks on the commits only on this Mac up to `tip` -> (True, []) clean, (False, findings) or (None, why)."""
    import subprocess
    if not core.have("gitleaks"):
        return None, "the secret scan needs gitleaks, which is not installed (brew install gitleaks)"
    args = ["gitleaks", "git", "--no-banner", "--redact", "--exit-code", "3", "--report-format", "json",
            "--report-path", "-", f"--log-opts={tip} --not --remotes", "."]
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
    files = core.gitout(top, "log", "--format=", "--name-only", commits[0], "--not", "--remotes") if commits else ""
    hit = next((f.strip() for f in files.splitlines() if RED_TEAM_FILE.search(f.strip())), "")
    return f"it changes a red-team test, {hit}" if hit else ""


def _public(top) -> bool:
    """Is the repository public? Unknown counts as public: the push would publish the commits at once."""
    try:
        from . import pipelines
        vis = pipelines._visibility(Path(top))
    except Exception:
        vis = None
    return vis not in ("private", "internal")


def _embargo_holder(top, name, mine: set, held: dict, local: set):
    """The embargoed branch (this one, or one that holds any of the same commits) -> (branch, until) or None."""
    if name in held:
        return name, held[name].get("until") or ""
    for other, e in held.items():
        if other != name and other in local and mine & set(_commits(top, f"refs/heads/{other}")):
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
    top = c.top
    pr = pipelines.open_pr(ctx["prs"], name)
    base = f"refs/remotes/origin/{c.default}" if _sha(top, f"refs/remotes/origin/{c.default}") else c.default
    ci = pipelines.push_ci(top, name, target, remote_sha=remote_sha or None, base=base, pr=pr,
                           prs_known=ctx["prs_known"])
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
    if guard.branch_violation(name):
        return _result(name, remote, SKIPPED, "its name breaks the branch naming rule",
                       [f"git switch {name} && dev branch rename <type>/<topic>"])
    if not remote:
        return _result(name, "", SKIPPED, "no remote is configured", ["git remote add origin <url>"])
    merge = core.gitout(top, "config", "--get", f"branch.{name}.merge")
    if merge and merge != f"refs/heads/{name}":
        return _result(name, remote, SKIPPED, f"its upstream is {remote}/{merge.replace('refs/heads/', '')}, not "
                       f"{name}", ["dev push   (pushes to the upstream it has)"])
    target = _target(c, name, tip, ctx)
    mine = _commits(top, target) if target else []
    if not mine:
        if _commits(top, tip):
            return _result(name, remote, WAITING, f"commits younger than {ctx['after']} min wait for the backup")
        return _result(name, remote, NOTHING, f"its commits are on {remote} already")
    held = _embargo_holder(top, name, set(mine), ctx["held"], ctx["local"])
    if held:
        why = ("embargoed" if held[0] == name else f"its commits are in {held[0]}, which is embargoed") + (
            f' until "{held[1]}"' if held[1] else "")
        return _result(name, remote, SKIPPED, why, [f"dev branch embargo --lift {held[0]}   (when it may be public)"])
    rc, out, err = core.git(top, "ls-remote", "--heads", remote, f"refs/heads/{name}", timeout=60)
    if rc != 0:
        text = (err or out).strip().splitlines()
        why = (text[-1] if text else "no answer")[:100]
        return _result(name, remote, SKIPPED, f"{remote} cannot be reached ({why})",
                       [f"git ls-remote {remote}   (check the network and the credentials), then dev backup {name}"])
    remote_sha = (out.split() or [""])[0]
    if remote_sha:
        if not _sha(top, remote_sha):
            core.git(top, "fetch", "--no-tags", "-q", remote, f"+refs/heads/{name}:refs/remotes/{remote}/{name}",
                     timeout=120)
        if not _sha(top, remote_sha):  # without its commits devkit cannot tell ahead from diverged
            return _result(name, remote, SKIPPED, f"devkit could not fetch {remote}/{name}",
                           [f"git fetch {remote} {name}, then dev backup {name}"])
        if remote_sha == target or core.git(top, "merge-base", "--is-ancestor", target, remote_sha)[0] == 0:
            return _result(name, remote, NOTHING, f"{remote}/{name} has every commit already")
        if core.git(top, "merge-base", "--is-ancestor", remote_sha, target)[0] != 0:
            return _result(name, remote, SKIPPED, f"{remote}/{name} has diverged from {name} (commits on both sides)",
                           _diverged_fix(name, remote))
        mine = _commits(top, target, remote_sha, "--remotes")
    wip = [s for s in mine if WIP_RE.match(core.gitout(top, "log", "-1", "--format=%s", s))]
    if wip:
        return _result(name, remote, SKIPPED, f"WIP commit {wip[-1][:7]} never reaches the remote",
                       ["dev commit --unwip, then commit properly; the next backup takes it"])
    start = remote_sha or core.gitout(top, "rev-parse", "--verify", "-q", mine[-1] + "^") or "root"
    rng = f"{start[:12]}..{target[:12]}"
    hint = _security_hint(top, mine)
    if hint and not ctx["public_ok"] and _public(top):
        return _result(name, remote, SKIPPED, f"it may carry a security fix ({hint}), and a push publishes it", [
            f'dev branch embargo {name} --why "<the open issue>" --until "<the step or date it may be public>"',
            f"dev backup {name} --public   (it is not a security fix, or it may be public now)"])
    ci, skip = _ci_gate(c, name, target, remote_sha, ctx)
    if skip:
        return skip
    clean, found = scan(top, target)  # last before the push: on exactly the commits it sends
    if clean is None:
        return _result(name, remote, SKIPPED, found, ["brew install gitleaks" if "not installed" in found else
                                                      f"gitleaks git --log-opts='{target[:12]} --not --remotes' ."])
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
    if prs is None:
        prs, known = _open_prs(c)
        prs_known = known if prs_known is None else prs_known
    ctx = {"auto": auto, "accept_ci": accept_ci and not auto, "dry_run": dry_run, "public_ok": public_ok and not auto,
           "now": now or time.time(), "after": branches.after_minutes(), "held": branches.embargoes(c.pid),
           "local": set(local), "prs": prs or [], "prs_known": bool(prs_known)}
    last = branches.backups(c.pid) if record and not dry_run else {}
    out = []
    for name in names:
        try:
            res = _one(c, name, ctx)
        except Exception as e:  # one broken branch must not stop the others
            res = _result(name, "", SKIPPED, f"devkit failed on it ({type(e).__name__}: {e})"[:200])
        if record and not dry_run:
            _record(c.pid, res, (last.get(name) or {}).get("skipped"))
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


def cmd_backup(a) -> int:
    """`dev backup [<branch>…] [--all] [--accept-ci] [--public] [--dry-run] [--json]`."""
    from . import gitx
    c = gitx.ctx()
    if c is None:
        print(f"{ui.WARN} not inside a git repository", file=sys.stderr)
        return 2
    res = run(c.top, names=list(a.branches or []), every=bool(a.all), accept_ci=bool(a.accept_ci),
              dry_run=bool(a.dry_run), public_ok=bool(a.public))
    if getattr(a, "json", False):
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print("\n".join(lines(res) or [f"{ui.INFO} no branch to back up"]))
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
