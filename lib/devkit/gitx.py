# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Git and GitHub through `dev`, with the delivery rules built in and compact output.

  dev branch new|rename|ls|clean      branches from an up-to-date origin/<default>, named by the standard
  dev commit -m "type(scope): …"      Conventional Commits, no AI attribution, staged-secret scan; --wip/--unwip
  dev rebase                          fetch and rebase on origin/<default>, with a conflict report
  dev push                            one push: name, default-branch, WIP, freshness and local-CI checks, budget
  dev pr create|view|ls|checks|merge|ready|comment|review|diff
  dev run ls|view|rerun               CI runs; failing logs trimmed; rerun only with --flaky "reason"
  dev release                         tag + GitHub release from a verified default branch (CalVer by default)
  dev issue ls|view|create
  dev git … · dev gh …                escape hatches that apply the same policy checks

Local CI evidence: `dev ci --run` (or `dev ci --mark-passed`) writes STATE/ci/<repo>@<HEAD sha>.json with the git
tree it verified, including uncommitted files. `dev push`, `dev pr create` and `dev release` accept a commit whose
tree matches, so committing exactly what was tested keeps the evidence and anything else invalidates it.

Output is one line or a short table, plain text when stdout is not a TTY; read commands take --json. Every `gh`
call goes through the module-level GH runner and every foreground command through EXEC, so tests replace them.
Python 3.9 compatible, stdlib only.
"""
from __future__ import annotations

import argparse
import contextlib
import glob as _glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from . import core, guard, ui

# --------------------------------------------------------------------------- runners (tests replace these)


def _gh_env() -> dict:
    env = dict(core.GIT_ENV)
    env.update(GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", GH_PAGER="cat", NO_COLOR="1", CLICOLOR="0")
    return env


def _gh_run(args, cwd=None, timeout=60):
    """Captured `gh` call -> (rc, stdout, stderr); never raises."""
    return core.sh(["gh", *args], cwd=cwd, timeout=timeout, env=_gh_env())


def _exec(argv, cwd=None, env=None) -> int:
    """Foreground command with the terminal attached (passthroughs, the editor)."""
    try:
        return subprocess.call([str(x) for x in argv], cwd=cwd, env=env)
    except OSError as e:
        ui.err(f"{argv[0]}: {e}")
        return 127


GH = _gh_run
EXEC = _exec
SLEEP = time.sleep
TEMPLATE_DIR = core.DEVKIT_HOME / "templates" / "github"

# --------------------------------------------------------------------------- output


def ok(msg: str):
    print(f"{ui.OK} {msg}")


def info(msg: str):
    print(f"  {ui.dim(msg)}")


def refuse(reason: str, fix: str = None, code: int = 2) -> int:
    """A policy refusal: what is wrong and the one command that fixes it. Exit 2."""
    print(f"{ui.FAIL} {reason}", file=sys.stderr)
    if fix:
        print(f"  {ui.ARROW} {fix}", file=sys.stderr)
    return code


def fail(reason: str, detail: str = None, code: int = 1) -> int:
    """An operation that ran and failed (git, gh, a check). Exit 1."""
    print(f"{ui.FAIL} {reason}", file=sys.stderr)
    for ln in (detail or "").splitlines()[-12:]:
        if ln.strip():
            print(f"  {ui.dim(ln.rstrip()[:200])}", file=sys.stderr)
    return code


def emit(obj) -> int:
    print(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    return 0


def _first_line(text: str) -> str:
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if ln and not ln.lower().startswith(("hint:", "usage:")):
            return ln[:240]
    return ""


def _age(value) -> str:
    """ISO timestamp or epoch -> '3h', '2d', 'now'."""
    ts = value
    if isinstance(value, str):
        import datetime as dt
        try:
            ts = dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return "-"
    return core.ago(ts) if ts else "-"  # "3 min ago", "just now"


# --------------------------------------------------------------------------- repository context


def ctx(cwd=None):
    """The repository around `cwd`, or None outside one."""
    cwd = str(cwd or os.getcwd())
    top = core.gitout(cwd, "rev-parse", "--show-toplevel")
    if not top:
        return None
    from .registry import default_branch, main_of
    main = main_of(Path(top)) or Path(top)
    remotes = core.gitout(top, "remote").split()
    has_origin = "origin" in remotes
    remote = core.gitout(top, "config", "--get", "remote.origin.url") if has_origin else ""
    parsed = core.parse_remote(remote) if remote else None
    host, owner, repo = parsed if parsed else (None, None, None)
    branch = core.gitout(top, "branch", "--show-current")
    if has_origin:
        default = default_branch(Path(top))
    else:
        default = next((b for b in ("main", "master", "trunk", "develop")
                        if core.git(top, "rev-parse", "--verify", "-q", f"refs/heads/{b}")[0] == 0), "main")
    gh_hosts = {h.lower() for h in ((core.CFG.get("gitx") or {}).get("github_hosts") or [])}
    provider = None
    if host and (host == "github.com" or host in gh_hosts):
        provider = "github"
    elif host and "gitlab" in host:
        provider = "gitlab"
    slug = None
    if provider == "github":
        slug = f"{owner}/{repo}" if host == "github.com" else f"{host}/{owner}/{repo}"
    pid = core.project_identity(main, remote)[0]
    return SimpleNamespace(top=top, main=str(main), remote=remote, has_origin=has_origin, host=host, owner=owner,
                           repo=repo, slug=slug, provider=provider, pid=pid, branch=branch, default=default)


def _need_repo(c):
    return refuse("not inside a git repository") if c is None else None


def need_github(c, what: str):
    """None when `dev <what>` can talk to GitHub for this repository, else the refusal exit code."""
    if c.provider == "github":
        if GH is _gh_run and not core.have("gh"):
            return refuse("gh (GitHub CLI) is not installed", "brew install gh && gh auth login")
        return None
    if not c.has_origin:
        return refuse(f"`dev {what}` needs an `origin` remote; this repository has none")
    if c.provider == "gitlab":
        hint = ("this is GitLab: use `glab` directly for now (for example `glab mr list`)" if core.have("glab")
                else "this is GitLab: install `glab` (brew install glab) and use it directly for now")
    else:
        hint = "plain git works here: dev branch · dev commit · dev rebase · dev push"
    return refuse(f"`dev {what}` supports GitHub only; {c.host or 'this remote'} is not supported yet", hint)


def _ref_sha(top, ref) -> str:
    return core.gitout(top, "rev-parse", "--verify", "-q", ref + "^{commit}")


def _count(top, spec) -> int:
    out = core.gitout(top, "rev-list", "--count", spec)
    return int(out) if out.isdigit() else 0


def _is_ancestor(top, a, b) -> bool:
    return core.git(top, "merge-base", "--is-ancestor", a, b)[0] == 0


def _dirty(top, untracked=True) -> int:
    args = ["status", "--porcelain"] + ([] if untracked else ["--untracked-files=no"])
    return len([ln for ln in core.gitout(top, *args).splitlines() if ln.strip()])


def _fetch(c, *refs):
    """git fetch --prune origin -> (ok, error)."""
    if not c.has_origin:
        return True, ""
    rc, _, err = core.git(c.top, "fetch", "--prune", "-q", "origin", *refs, timeout=180)
    return rc == 0, _first_line(err)


def _base_ref(c) -> str:
    ref = f"origin/{c.default}"
    return ref if c.has_origin and _ref_sha(c.top, f"refs/remotes/{ref}") else c.default


def _me_email(top) -> str:
    ident = core.gitout(top, "var", "GIT_AUTHOR_IDENT")
    m = re.search(r"<([^>]*)>", ident)
    return (m.group(1) if m else "").lower()


def _me() -> str:
    return core.github_user()


def _worktree_branches(top) -> dict:
    """branch -> worktree path, for every checkout of the repository."""
    from .registry import list_worktrees
    return {w["branch"]: w["path"] for w in list_worktrees(Path(top)) if w.get("branch")}


# --------------------------------------------------------------------------- naming and message rules

CC_TYPES = ("feat", "fix", "docs", "style", "refactor", "perf", "test", "build", "ci", "chore", "revert", "deps")
CC_RE = re.compile(r"^(?P<type>[A-Za-z]+)(?:\((?P<scope>[^()\s]+)\))?(?P<bang>!)?: (?P<desc>\S.*)$")
MAX_HEADER = 72
_VERBS = set("""
add adjust align allow apply avoid block bring build bump cache call change check clean clear close collapse
combine compute configure convert copy correct cover create declare default define delete deny deprecate
detect disable display document downgrade drop emit enable enforce ensure exclude expand explain export expose
extend extract fetch fill filter finish fix flag format generate get guard handle hide hook ignore implement import
improve include increase inline install introduce keep limit link list load lock log lower make map mark merge
migrate mock move name normalize open optimize parse pass pin polish post prefer prevent print process protect prune
publish pull push put raise read rebase record reduce refactor refuse register reject release reload remove rename
render reorder replace report require reset resolve restore restrict retry return reuse revert rewrite run save
scope send separate set show simplify skip sort speed split start stop store strip support switch sync test tidy
track trim tune tweak unify unlock unpin update upgrade use validate verify warn wrap write
""".split())
_ATTRIBUTION_EXTRA = ("co-authored-by: copilot", "co-authored-by: github copilot", "co-authored-by: gemini",
                      "co-authored-by: cursor", "co-authored-by: devin", "generated with copilot",
                      "generated by copilot", "generated with chatgpt", "generated by chatgpt", "generated with gemini")


def _cc_types() -> tuple:
    return tuple((core.CFG.get("commits") or {}).get("types") or CC_TYPES)


def _non_imperative(desc: str):
    """The first word when it is clearly not imperative ('added', 'adds', 'adding'), else None."""
    w = re.split(r"[\s:,;(]", desc.strip(), maxsplit=1)[0].lower()
    if not w.isalpha():
        return None
    if w.endswith("ed") and (w[:-2] in _VERBS or w[:-1] in _VERBS or (len(w) > 4 and w[:-3] in _VERBS)):
        return w
    if w.endswith("ing") and (w[:-3] in _VERBS or w[:-3] + "e" in _VERBS or (len(w) > 5 and w[:-4] in _VERBS)):
        return w
    if w.endswith("es") and w[:-2] in _VERBS:
        return w
    if w.endswith("s") and not w.endswith("ss") and w[:-1] in _VERBS:
        return w
    return None


def message_violation(header: str, what: str = "Commit message"):
    """Reason when `header` is not a Conventional Commit header (type list, ≤ 72, imperative, no period)."""
    h = (header or "").strip()
    types = _cc_types()
    shape = f"`type(scope): summary` with type in {'|'.join(types)}"
    if not h:
        return f"{what} is empty: use {shape}."
    if re.match(r"(?i)^wip\b", h):
        return f"{what} `{h}` is a WIP marker: use `dev commit --wip` for work in progress, or a real {shape}."
    m = CC_RE.match(h)
    if not m:
        return f"{what} `{h}` is not a Conventional Commit: use {shape} (for example `fix(auth): handle expired tokens`)."
    if m.group("type") not in types:
        low = m.group("type").lower()
        if low in types:
            return f"{what} `{h}`: write the type in lowercase (`{low}`)."
        return f"{what} `{h}`: unknown type `{m.group('type')}`; use one of {'|'.join(types)}."
    if len(h) > MAX_HEADER:
        return f"{what} is {len(h)} characters (max {MAX_HEADER}): shorten the summary; details go in the body."
    desc = m.group("desc").strip()
    if desc.endswith("."):
        return f"{what} `{h}` ends with a period: drop it."
    w = _non_imperative(desc)
    if w:
        return f"{what} `{h}` starts with `{w}`: use the imperative mood (`add`, not `added`, `adds` or `adding`)."
    return None


TITLE_SAMPLE = 20  # the merged pull requests, or default-branch commits, a title convention is read from
TITLE_MIN_SEEN = 5  # fewer titles than this show no convention: Conventional Commits stay the default
TITLE_PLAIN_SHARE = 0.8  # plain sentences in at least this share of the titles are the repository's convention


def _conventional(title: str) -> bool:
    m = CC_RE.match((title or "").strip())
    return bool(m) and m.group("type").lower() in _cc_types()


def _sentence_of(desc: str) -> str:
    d = (desc or "").strip().rstrip(".")
    return d[:1].upper() + d[1:]


def title_convention(c) -> dict:
    """How this repository titles its pull requests, read from its own history: the titles of its last merged pull
    requests on GitHub (bots left out: their prefix is their own configuration), or, when GitHub lists fewer than
    five, the subjects of the default branch's last commits (merges, reverts, WIP and bots left out). Plain sentences
    in at least 80% of five or more titles are the repository's convention, `sentence` (firefly-weave); anything else
    keeps devkit's default, `conventional` (Conventional Commits, as devkit's own repository uses).
    -> {"style", "seen": titles read, "plain": plain sentences among them, "source": where they come from}"""
    titles, source = [], "merged PRs"
    if c.provider == "github" and c.slug:
        prs, _ = gh_json(["pr", "list", "-R", c.slug, "--state", "merged", "--limit", str(TITLE_SAMPLE),
                          "--json", "title,author"], c)
        for p in prs if isinstance(prs, list) else []:
            who = p.get("author") or {}
            login = str(who.get("login") or "")
            if not (who.get("is_bot") or login.endswith("[bot]") or login.startswith("app/")):
                titles.append(str(p.get("title") or "").strip())
    titles = [t for t in titles if t]
    if len(titles) < TITLE_MIN_SEEN:
        ref = f"origin/{c.default}" if _ref_sha(c.top, f"refs/remotes/origin/{c.default}") else c.default
        out = core.gitout(c.top, "log", "--no-merges", f"-{TITLE_SAMPLE * 2}", "--format=%s%x1f%an%x1f%ae", ref)
        titles, source = [], f"commits on {c.default}"
        for line in out.splitlines():
            subject, _, who = line.partition("\x1f")
            if subject.strip() and "[bot]" not in who and not re.match(r"(Merge |Revert \")", subject) and \
                    not _WIP_RE.match(subject):
                titles.append(subject.strip())
        titles = titles[:TITLE_SAMPLE]
    plain = sum(1 for t in titles if not _conventional(t))
    style = "sentence" if len(titles) >= TITLE_MIN_SEEN and plain >= TITLE_PLAIN_SHARE * len(titles) else "conventional"
    return {"style": style, "seen": len(titles), "plain": plain, "source": source}


def _sentence_violation(title: str, conv=None, what: str = "PR title"):
    """Reason when `title` is not a plain imperative sentence (a capital first letter, no Conventional Commit prefix,
    no period, no WIP marker), the convention `conv` found; None when it is one. No length rule: such a repository's
    own titles run past 72 characters."""
    h = (title or "").strip()
    rule = "this repository titles its pull requests as plain imperative sentences" + (
        f" ({conv['plain']} of the last {conv['seen']} {conv['source']})" if conv else "")
    if not h:
        return f"{what} is empty: {rule}, for example `Handle expired tokens`."
    if _WIP_RE.match(h):
        return f"{what} `{h}` is a WIP marker: {rule}; say what the change does."
    m = CC_RE.match(h)
    if m and m.group("type").lower() in _cc_types():
        return f"{what} `{h}` is a Conventional Commit, but {rule}: write `{_sentence_of(m.group('desc'))}`."
    if h[0].isalpha() and h[0].islower():
        return f"{what} `{h}`: start with a capital letter (`{_sentence_of(h)}`), as {rule}."
    if h.endswith("."):
        return f"{what} `{h}` ends with a period: drop it."
    w = _non_imperative(h)
    if w:
        return f"{what} `{h}` starts with `{w}`: use the imperative mood (`Add`, not `Added`, `Adds` or `Adding`)."
    return None


def pr_title_violation(c, title: str, conv=None):
    """Reason when a pull request title breaks this repository's title convention (title_convention), else None."""
    conv = conv or title_convention(c)
    if conv["style"] == "sentence":
        return _sentence_violation(title, conv)
    return message_violation(title, "PR title")


def _title_hint(conv) -> str:
    return '"<an imperative sentence>"' if conv and conv.get("style") == "sentence" else '"type(scope): summary"'


def strip_attribution(text: str):
    """-> (text without AI-attribution lines, number of lines removed)."""
    pats = [p.lower() for p in guard.ATTRIBUTION] + list(_ATTRIBUTION_EXTRA)
    lines = (text or "").split("\n")
    keep = [ln for ln in lines if not any(p in ln.lower() for p in pats)]
    clean = re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip("\n")
    return clean, len(lines) - len(keep)


def _slug(s: str) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9._-]+", "-", s.lower())).strip("-.")


def branch_name(spec: str, ticket: str = None) -> str:
    """'feat/Invoice Export' + PROJ-142 -> 'feat/PROJ-142-invoice-export' (validated separately)."""
    head, sep, rest = (spec or "").strip().partition("/")
    if not sep:
        return spec.strip()
    key = ""
    m = re.match(r"^([A-Z][A-Z0-9]+-\d+)[-_ ]*(.*)$", rest)
    if m:
        key, rest = m.group(1), m.group(2)
    if ticket:
        t = ticket.strip()
        key = t.upper() if re.match(r"^[A-Za-z][A-Za-z0-9]+-\d+$", t) else _slug(t)
    topic = _slug(rest)
    if head.lower() in ("release", "hotfix") and re.match(r"^v?\d+\.\d+", rest):
        topic = rest.strip()
    return f"{head.lower()}/{key + '-' if key and topic else key}{topic}"


# --------------------------------------------------------------------------- local CI evidence


def _ci_dir() -> Path:
    return core.STATE / "ci"


def _key(c) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "__", c.pid)


def work_tree(top):
    """Tree id of the working tree as it is now (tracked and untracked, not ignored); the real index is untouched."""
    import shutil
    import tempfile
    idx = core.gitout(top, "rev-parse", "--git-path", "index")
    idx_path = Path(idx) if os.path.isabs(idx) else Path(top) / idx
    with tempfile.TemporaryDirectory() as d:
        tmp = os.path.join(d, "index")
        if idx and idx_path.exists():
            shutil.copyfile(idx_path, tmp)
        env = dict(core.GIT_ENV, GIT_INDEX_FILE=tmp)
        rc, _, _ = core.sh(["git", "-C", top, "add", "-A"], env=env, timeout=300)
        if rc != 0:
            return None
        rc, out, _ = core.sh(["git", "-C", top, "write-tree"], env=env, timeout=120)
        return out.strip() if rc == 0 and out.strip() else None


def _head(top, rev="HEAD"):
    return _ref_sha(top, rev), core.gitout(top, "rev-parse", "--verify", "-q", f"{rev}^{{tree}}")


def record_evidence(c, how: str, tree: str, **extra) -> dict:
    """Write STATE/ci/<repo>@<HEAD sha>.json (a `-<tree>` suffix when the tested tree had uncommitted changes)."""
    sha, head_tree = _head(c.top)
    clean = bool(sha) and tree == head_tree
    d = _ci_dir()
    d.mkdir(parents=True, exist_ok=True)
    name = f"{_key(c)}@{sha or 'unborn'}" + ("" if clean else f"-{tree[:12]}") + ".json"
    data = {"repo": c.pid, "sha": sha, "tree": tree, "clean": clean, "branch": c.branch, "how": how,
            "ts": int(time.time()), "user": os.environ.get("USER", ""), "host": os.uname().nodename.split(".")[0]}
    data.update(extra)
    (d / name).write_text(json.dumps(data, indent=1))
    cutoff = time.time() - 30 * 86400
    for f in d.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass
    return data


def _markers(c) -> list:
    out = []
    for f in _glob.glob(str(_ci_dir() / (_glob.escape(_key(c)) + "@*.json"))):
        data = core.read_json(f)
        if isinstance(data, dict) and data.get("tree"):
            out.append(data)
    return sorted(out, key=lambda x: -(x.get("ts") or 0))


def evidence(c, rev="HEAD", host_only=False):
    """The newest local-CI marker whose tree is the tree of `rev`, or None. `host_only`: when `rev` is HEAD and no
    marker has its tree, the evidence of an earlier tree carries over if only CI host settings changed since
    (HOST_RULE); it is recorded as a `host-only` marker that names its base and what was checked."""
    sha, tree = _head(c.top, rev)
    if not sha:
        return None
    exact = core.read_json(_ci_dir() / f"{_key(c)}@{sha}.json")
    if isinstance(exact, dict) and exact.get("tree") == tree:
        return exact
    found = next((m for m in _markers(c) if m.get("tree") == tree), None)
    if found or not host_only or sha != _ref_sha(c.top, "HEAD"):
        return found
    return _carry_over(c, tree)


# --------------------------------------------------------------------------- CI host settings
#
# A local run cannot exercise what only the CI host acts on: how long a job may run and which runs cancel each other.
# When every file changed since a tree with local evidence is a GitHub workflow that differs only in those keys or in
# comments, the evidence of that tree holds for the new one, once actionlint and the devkit workflow audit pass on the
# new workflows. The runner is not one of them: another machine (a self-hosted runner) runs the job differently.
# GitHub reads workflows with another YAML parser than devkit's, so three conditions must all hold, and two of them
# read the text alone: devkit's parse is the same once the host keys are left out; every line `git diff` shows is
# blank, a comment or a host-setting line; and neither version uses YAML anchors, aliases, merge keys, tags or
# directives, where two parsers are most likely to disagree. A change to anything else (a run command, a step, a
# trigger, the permissions, the runner, the matrix, any other file) still needs a local run.

HOST_KEYS = {"workflow": ("concurrency",), "job": ("concurrency", "timeout-minutes"), "step": ("timeout-minutes",)}
HOST_RULE = ("When only CI host settings changed since a tree with local CI evidence, that evidence carries over. "
             "Every changed file must be a GitHub workflow (.github/workflows/*.yml) that reads the same once "
             "timeout-minutes (job and step) and concurrency (group, cancel-in-progress) are left out; every line its "
             "text diff adds or removes must be blank, a comment, or a timeout-minutes, concurrency, group or "
             "cancel-in-progress line; neither version may use YAML anchors, aliases, merge keys, tags or "
             "directives; and actionlint and the devkit workflow audit (dev ci audit) must pass on it. Any other "
             "change (a run command, a step, a trigger, the permissions, the runner (runs-on), the matrix, another "
             "file) needs a local run.")
_WORKFLOW_PATH = re.compile(r"\.github/workflows/[^/]+\.ya?ml")
_HOST_WHY = {}  # (top, tree) -> why the evidence of an earlier tree did not carry over to `tree`, for no_evidence
_ABSENT = object()
# A changed line that may carry over: blank, a comment, or a host-setting key with an optional value and comment. Only
# spaces indent it, and none of its characters is a line break or a control character in any YAML version (a CRLF
# ending aside), so it is one line to every parser.
_LINE_CHAR = r"[^\x00-\x1f\x7f\x85  ]"
_HOST_LINE = re.compile(rf" *(?:#{_LINE_CHAR}*|(?:timeout-minutes|concurrency|group|cancel-in-progress):"
                        rf"(?: {_LINE_CHAR}*)?)?\r?")
_YAML_PROPS = {"&": "an anchor (&name)", "*": "an alias (*name)", "!": "a tag (!)"}
_MERGE_KEY = re.compile(r"""(?:<<|'<<'|"<<")[ \t]*:""")
_BLOCK_HEADER = re.compile(r"[|>][+-]?(?:[ \t]+#.*)?[ \t]*")  # no indentation digit: with one, the scan reads on
_SPACE = ("", " ", "\t")


def _quote_end(line, i):
    """The index of the quote that closes the quoted scalar opening at line[i], or -1 when it does not close here."""
    q, j = line[i], i + 1
    while j < len(line):
        c = line[j]
        if (q == '"' and c == "\\") or (q == "'" and c == "'" and line[j + 1:j + 2] == "'"):
            j += 2  # an escaped character, or '' inside single quotes
        elif c == q:
            return j
        else:
            j += 1
    return -1


def _yaml_feature(text):
    """-> (line, what) of the first YAML anchor, alias, merge key, tag or directive in `text`, or None. It reads the
    text alone, not devkit's parser, and errs toward finding one: &, * or ! counts wherever a node can start, in a
    comment too, and only the content of a plain block scalar (`run: |`) is passed over. Inside a flow collection
    whose quoted string spans lines it loses track, and from there on checks after every [ { , : and ?."""
    flow, lost, block = 0, False, None  # block: [the owner's column, the content's indentation once known]
    for no, line in enumerate(text.splitlines(), 1):
        if block:
            lead = len(line) - len(line.lstrip(" "))
            if not line.strip(" "):
                continue
            if block[1] is None and lead > block[0]:
                block[1] = lead
            if block[1] is not None and lead >= block[1]:
                continue
            block = None
        if line.startswith("%"):
            return no, "a directive (%)"
        i, start, owner = 0, True, None
        if line.startswith(("---", "...")) and line[3:4] in _SPACE:
            i = 3
        while i < len(line):
            ch, nxt, inflow = line[i], line[i + 1:i + 2], flow > 0 or lost
            if start:
                if ch in " \t" or (ch in "-?:" and nxt in _SPACE):
                    i += 1
                    continue
                if ch in _YAML_PROPS:
                    return no, _YAML_PROPS[ch]
                if _MERGE_KEY.match(line, i):
                    return no, "a merge key (<<:)"
                owner = i if owner is None else owner
                if ch in "[{":
                    flow, i = flow + 1, i + 1
                    continue
                if ch in "'\"":
                    end = _quote_end(line, i)
                    if end < 0:
                        lost = True
                        break
                    i, start = end + 1, False
                    continue
                if not inflow and ch in "|>" and _BLOCK_HEADER.fullmatch(line, i):
                    block = [owner, None]
                    break
                start = False
            if ch == ":" and (inflow or nxt in _SPACE):
                start = True
            elif inflow and ch in ",[{?":
                start, flow = True, flow + (ch in "[{")
            elif ch in "]}" and flow and not lost:
                flow -= 1
            i += 1
    return None


def _diff_rows(top, blob_a, blob_b):
    """The lines `git diff` removes from blob_a and adds in blob_b -> [(removed, line number, text)], or None when git
    cannot show them. Read as bytes and split on newlines only, so a carriage return stays inside its line."""
    try:
        p = subprocess.run(["git", "-C", top, "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--text",
                            "--unified=0", "--inter-hunk-context=0", blob_a, blob_b],
                           capture_output=True, env=core.GIT_ENV, timeout=60)
        out = p.stdout.decode("utf-8")
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return None
    if p.returncode != 0:
        return None
    rows, old_no, new_no, hunk = [], 0, 0, False
    for ln in out.split("\n"):
        if ln.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", ln)
            if not m:
                return None
            old_no, new_no, hunk = int(m[1]), int(m[2]), True
        elif not hunk or not ln or ln.startswith("\\"):
            continue
        elif ln[0] == "-":
            rows.append((True, old_no, ln[1:]))
            old_no += 1
        elif ln[0] == "+":
            rows.append((False, new_no, ln[1:]))
            new_no += 1
        elif ln[0] == " ":
            old_no, new_no = old_no + 1, new_no + 1
        else:
            return None
    return rows


def _host_split(doc):
    """A parsed workflow (plain dicts and lists) -> (the workflow without its host keys, {path: value} of them)."""
    host = {}
    if not isinstance(doc, dict):
        return doc, host
    rest = {}
    for k, v in doc.items():
        if k in HOST_KEYS["workflow"]:
            host[str(k)] = v
        else:
            rest[k] = v
    if not isinstance(rest.get("jobs"), dict):
        return rest, host
    jobs = {}
    for jid, job in rest["jobs"].items():
        if isinstance(job, dict):
            job = dict(job)
            for k in HOST_KEYS["job"]:
                if k in job:
                    host[f"jobs.{jid}.{k}"] = job.pop(k)
            if isinstance(job.get("steps"), list):
                steps = []
                for i, st in enumerate(job["steps"]):
                    if isinstance(st, dict):
                        st = dict(st)
                        for k in HOST_KEYS["step"]:
                            if k in st:
                                host[f"jobs.{jid}.steps[{i}].{k}"] = st.pop(k)
                    steps.append(st)
                job["steps"] = steps
        jobs[jid] = job
    rest["jobs"] = jobs
    return rest, host


def _first_diff(a, b, path=""):
    """The path of the first difference between two parsed documents, such as `jobs.test.steps[1].run`."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in list(a) + [k for k in b if k not in a]:
            sub = f"{path}.{k}" if path else str(k)
            if k not in a or k not in b:
                return sub
            if a[k] != b[k]:
                return _first_diff(a[k], b[k], sub)
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                return _first_diff(x, y, f"{path}[{i}]")
    return path or "the whole file"


def host_only_change(top, old, new):
    """-> (["<workflow>: <host keys changed, or 'comments'>", …], "") when trees `old` and `new` differ only in CI host
    settings (HOST_RULE), else (None, why). It reads the two trees from git, nothing on disk. Three checks must all
    pass for each workflow: the text has no YAML anchor, alias, merge key, tag or directive; devkit's parse is the
    same without the host keys; and every line of the text diff is blank, a comment or a host-setting line."""
    from . import pipelines
    if old == new:
        return None, "nothing changed"
    rc, out, _ = core.sh(["git", "-C", top, "diff-tree", "-r", "--no-renames", "-z", old, new], timeout=120,
                         env=core.GIT_ENV)
    if rc != 0:
        return None, "the tree of the last local run is gone"
    parts, entries = out.split("\0"), []
    for meta, path in zip(parts[0::2], parts[1::2]):
        fields = meta.lstrip(":").split()
        if len(fields) == 5:
            entries.append((path, *fields))
    if not entries:
        return None, "nothing changed"
    for path, mode_a, mode_b, _, _, status in entries:
        if not _WORKFLOW_PATH.fullmatch(path):
            return None, f"{path} changed, and it is not a GitHub workflow"
        if status != "M":
            return None, f"{path} was {'added' if status == 'A' else 'deleted' if status == 'D' else 'replaced'}"
        if {mode_a, mode_b} - {"100644", "100755"}:
            return None, f"{path} is not a regular file"
    changes = []
    for path, _, _, blob_a, blob_b, _ in entries:
        texts = []
        for blob in (blob_a, blob_b):
            rc, text, _ = core.sh(["git", "-C", top, "cat-file", "blob", blob], timeout=60, env=core.GIT_ENV)
            if rc != 0:
                return None, f"git cannot read {path}"
            texts.append(text)
        for which, text in zip(("the earlier version", "the new version"), texts):
            found = _yaml_feature(text)
            if found:
                return None, (f"{path} uses {found[1]} on line {found[0]} of {which}, which GitHub's YAML parser "
                              "may read differently from devkit's")
        docs = []
        for text in texts:
            try:
                docs.append([pipelines.plain(d) for d in pipelines.load_yaml_all(text)])
            except pipelines.YamlError as e:
                return None, f"devkit cannot read {path}: {e}"
        if len(docs[0]) != 1 or len(docs[1]) != 1:
            return None, f"{path} is not one YAML document"
        (rest_a, host_a), (rest_b, host_b) = _host_split(docs[0][0]), _host_split(docs[1][0])
        if rest_a != rest_b:
            return None, f"{path} changed {_first_diff(rest_a, rest_b)}, which a local run exercises"
        rows = _diff_rows(top, blob_a, blob_b)
        if rows is None:
            return None, f"git cannot show the text diff of {path}"
        for removed, no, line in rows:
            if not _HOST_LINE.fullmatch(line):
                shown = "".join(ch if ch.isprintable() else repr(ch)[1:-1] for ch in line.strip(" "))[:80]
                where = f"line {no} of the earlier version" if removed else f"line {no}"
                return None, (f"{path} changed {where} ({shown}): only blank, comment, timeout-minutes, concurrency, "
                              "group and cancel-in-progress lines may change without a local run")
        keys = [k for k in list(host_a) + [k for k in host_b if k not in host_a]
                if host_a.get(k, _ABSENT) != host_b.get(k, _ABSENT)]
        changes.append(f"{path}: {', '.join(keys) or 'comments'}")
    return changes, ""


def _workflow_checks(top, files):
    """actionlint on the changed workflows, then the devkit workflow audit as `dev ci audit` runs it (zizmor too when
    it is installed), on the files on disk -> (passed, what was checked or why it failed)."""
    from . import pipelines
    if not shutil.which("actionlint"):
        return False, "actionlint is not installed (brew install actionlint), so nothing checked the workflows"
    rc, out, err = core.sh(["actionlint", "-no-color", "-oneline", *files], cwd=top, timeout=120)
    if rc != 0:
        first = next((ln.strip() for ln in (out + "\n" + err).splitlines() if ln.strip()), f"exit {rc}")
        return False, f"actionlint: {first[:240]}"
    root = Path(top)
    rep = pipelines.audit(root, policy=pipelines._policy(root), visibility=lambda: pipelines._visibility(root))
    if pipelines.exit_code(rep):
        bad = [f for f in rep["findings"] if pipelines.RANK.get(f["severity"], 9) <= pipelines.RANK["high"]]
        f = bad[0]
        return False, (f"dev ci audit: {len(bad)} high or critical finding(s), first {f['id']} at "
                       f"{f['file']}:{f['line']}: {f['message'][:160]}")
    return True, "actionlint: no findings · dev ci audit: no high or critical finding"


WORKFLOW_CHECKS = _workflow_checks  # tests replace it
# What the workflow checks read from disk besides the workflows: actionlint's configuration
# (.github/actionlint.yaml) and zizmor's (zizmor.yml or zizmor.yaml at the root or in .github), either of which can
# turn findings off. An uncommitted change there keeps the host-only shortcut off.
CHECKS_READ = (".github", "zizmor.yml", "zizmor.yaml")


def _changed_workflows(changes) -> list:
    return sorted({x.split(": ", 1)[0] for x in changes})


_EVIDENCE_LABEL = {"run": "`dev ci --run`", "resumed": "`dev ci --run --resume`", "marked": "`dev ci --mark-passed`",
                   "attested": "an attested local run (--ci-passed)"}


def _carry_over(c, tree):
    """The evidence of an earlier tree, recorded for HEAD's `tree` as a `host-only` marker, when only CI host settings
    changed since (HOST_RULE) and the workflow checks pass; else None, with the reason in _HOST_WHY."""
    first_why = ""
    for m in [m for m in _markers(c) if m.get("how") != "host-only" and m.get("tree") != tree][:10]:
        names = _tree_diff(c.top, m["tree"], tree)
        if not names or not all(_WORKFLOW_PATH.fullmatch(n) for n in names):
            continue  # code changed too: the usual "changed since" says enough
        changes, why = host_only_change(c.top, m["tree"], tree)
        if changes is None:
            first_why = first_why or why
            continue
        dirty = core.gitout(c.top, "status", "--porcelain", "--untracked-files=all", "--", *CHECKS_READ)
        if dirty:
            first = dirty.splitlines()[0].split(None, 1)[-1]
            _HOST_WHY[(c.top, tree)] = (f"only CI host settings changed in HEAD, but {first} has uncommitted changes, "
                                        "so the workflow checks would not read HEAD's files")
            return None
        passed, said = WORKFLOW_CHECKS(c.top, _changed_workflows(changes))
        if not passed:
            _HOST_WHY[(c.top, tree)] = f"only CI host settings changed, but {said}"
            return None
        base = {"sha": m.get("sha"), "tree": m.get("tree"), "how": m.get("how"), "ts": m.get("ts")}
        since = "; ".join(changes)
        note = (f"{_EVIDENCE_LABEL.get(base['how'], base['how'])} passed on {(base['sha'] or '')[:7]}; since then "
                f"only CI host settings changed ({since}), checked: {said}")
        ev = record_evidence(c, "host-only", tree, base=base, changes=changes, checked=said, note=note)
        msg = (f"local CI evidence carried over: only CI host settings changed since the local run on "
               f"{(base['sha'] or '')[:7]} ({since}), which a local run cannot exercise · {said}")
        print(f"  {ui.dim(msg)}", file=sys.stderr)
        return ev
    if first_why:
        _HOST_WHY[(c.top, tree)] = first_why
    return None


def _resume_host(c, old, new, changed, cache) -> str:
    """Why a passed step needs no re-run on `new` (only CI host settings changed since its run on `old`, and the
    workflow checks pass), or '' when it may need one. Checked once per recorded tree."""
    key = ("host", old)
    if key not in cache:
        said = ""
        if changed and all(_WORKFLOW_PATH.fullmatch(f) for f in changed):
            changes, why = host_only_change(c.top, old, new)
            if changes is None:
                info(f"not only CI host settings since {old[:7]}: {why}")
            else:
                passed, checked = WORKFLOW_CHECKS(c.top, _changed_workflows(changes))
                if passed:
                    said = f"only CI host settings changed since ({'; '.join(changes)}) · {checked}"
                else:
                    info(f"only CI host settings changed since {old[:7]}, but {checked}: the steps run again")
        cache[key] = said
    return cache[key]


def _evidence_or_attest(c, ci_passed: bool, action: str):
    """-> (marker or None). `--ci-passed` records an attestation for HEAD. dev never takes it from its own
    environment (spec F8, rule 4): a variable there may have been set by anything that started dev."""
    ev = evidence(c, host_only=True)
    if ev or not ci_passed:
        return ev
    _, tree = _head(c.top)
    return record_evidence(c, "attested", tree, note=f"dev {action} --ci-passed") if tree else None


def no_evidence(c, action: str, rev="HEAD", draft_ci: str = "") -> int:
    """Refuse `action` for lack of local CI evidence. `draft_ci`: why opening a draft PR would start CI here."""
    sha, tree = _head(c.top, rev)
    detail = ""
    last = next(iter(_markers(c)), None)
    if last and tree:
        names = core.gitout(c.top, "diff-tree", "-r", "--name-only", last["tree"], tree).splitlines()
        if names:
            more = f" (+{len(names) - 4} more)" if len(names) > 4 else ""
            detail = (f"; the last local run ({last.get('how')}, {_age(last.get('ts'))}) verified a different tree, "
                      f"changed since: {', '.join(names[:4])}{more}")
    if _HOST_WHY.get((c.top, tree)):
        detail += f"; {_HOST_WHY[(c.top, tree)]}"
    if draft_ci:
        detail += f"; a draft is no exception here: {draft_ci}"
    return refuse(f"no local CI evidence for {sha[:7] or 'HEAD'}{detail}",
                  f"run what CI runs: `dev ci --run` (or run your suite, then `dev ci --mark-passed --note "
                  f"\"<command>: <result>\"`). If it passed in this session already: `dev {action} --ci-passed`."
                  + (" To open drafts without it, gate the jobs on drafts: `dev ci audit --fix`." if draft_ci else ""))


def draft_ci(c, branch: str, head: str) -> str:
    """Why opening a draft PR of `head` would start CI, or '' when the CI configuration says it starts no run: a draft
    then needs no local CI evidence, and `dev pr ready`, which starts the one run, asks for it (spec P-fy3bwa0pshk R6).
    The branch is already on the remote, so only the pull request's own runs count."""
    from . import pipelines
    try:
        r = pipelines.push_ci(c.top, branch, head, remote_sha=head, pr={"number": "new", "draft": True})
    except Exception as e:  # unreadable configuration: count it as a run, as the push budget does
        return f"devkit could not read the CI configuration ({e})"
    if r.get("starts") is False:
        return ""
    why = (r.get("why") or "").replace("draft PR #new", "a new draft PR")
    return why if r.get("starts") else f"devkit cannot tell whether it starts CI ({why or 'unknown CI'})"


def describe_evidence(ev) -> str:
    if not ev:
        return "none"
    how = {"run": "dev ci --run", "attested": "attested", "marked": "dev ci --mark-passed",
           "resumed": "dev ci --run --resume"}.get(ev.get("how"), ev.get("how"))
    if ev.get("how") == "host-only":
        base = ev.get("base") or {}
        return (f"{(_EVIDENCE_LABEL.get(base.get('how'), base.get('how') or '?')).strip('`')} on "
                f"{(base.get('sha') or '')[:7]}, then CI host settings only ({'; '.join(ev.get('changes') or [])}), "
                f"{_age(ev.get('ts'))}")
    steps = f", {len(ev.get('steps') or [])} steps" if ev.get("steps") else ""
    if ev.get("how") == "resumed":
        st = ev.get("steps") or []
        part = [x for x in st if x.get("how") == "partial"]
        steps += f" ({len(part)} partial, {sum(x.get('tests') or 0 for x in part)} tests; " \
                 f"{sum(1 for x in st if x.get('how') == 'kept')} kept)"
    return f"{how}{steps}, {_age(ev.get('ts'))}"


# --------------------------------------------------------------------------- dev branch


def cmd_branch(a) -> int:
    c = ctx()
    if c is None:
        return _need_repo(c)
    return {"new": _branch_new, "rename": _branch_rename, "ls": _branch_ls, "clean": _branch_clean}[a.branch_cmd](c, a)


def _branch_new(c, a) -> int:
    name = branch_name(a.name, a.ticket)
    v = guard.branch_violation(name)
    if v:
        return refuse(v.split(" Rename it:")[0])
    if _ref_sha(c.top, f"refs/heads/{name}"):
        return refuse(f"branch `{name}` already exists", f"git switch {name}")
    fetched, err = _fetch(c)
    if not fetched:
        ui.warn(f"git fetch failed ({err}); branching from the last known origin/{c.default}")
    if c.has_origin and _ref_sha(c.top, f"refs/remotes/origin/{name}"):
        return refuse(f"`{name}` already exists on origin", f"git switch {name} (tracks origin/{name})")
    base = a.base or _base_ref(c)
    if not _ref_sha(c.top, base):
        return refuse(f"start point `{base}` does not exist")
    dirty = _dirty(c.top, untracked=False)
    rc, _, err = core.git(c.top, "switch", "--no-track", "-c", name, base)
    if rc != 0:
        return fail(f"could not create `{name}` from {base}", err)
    sha = core.gitout(c.top, "rev-parse", "--short", "HEAD")
    ok(f"{name} from {base} @ {sha}" + (f" · carried {dirty} uncommitted change(s)" if dirty else ""))
    linked = _link_new_branch(c.pid, name)
    if linked:
        ok(linked)
    return 0


def _link_new_branch(pid: str, branch: str) -> str:
    """Link the claimed item of `pid` that has no branch to the new `branch` (work.link_new_branch). A problem with
    the handoff repository only warns: the branch exists either way."""
    if not pid:
        return ""
    from . import work
    try:
        return work.link_new_branch(pid, branch, a=work.actor(user=_me() or None))
    except Exception as e:
        ui.warn(f"work items not linked to {branch}: {e} (dev task start <id> on the branch links one)")
        return ""


def _branch_rename(c, a) -> int:
    new = branch_name(a.name, a.ticket)
    v = guard.branch_violation(new)
    if v:
        return refuse(v.split(" Rename it:")[0])
    if _ref_sha(c.top, f"refs/heads/{new}"):
        return refuse(f"branch `{new}` already exists")
    old = c.branch
    if not old:
        rc, _, err = core.git(c.top, "switch", "-c", new)
        if rc != 0:
            return fail("could not create a branch at the detached HEAD", err)
        ok(f"created {new} at the detached HEAD ({core.gitout(c.top, 'rev-parse', '--short', 'HEAD')})")
        return 0
    if old == new:
        ok(f"already on {new}")
        return 0
    if old == c.default or old in guard.PERMANENT_BRANCHES:
        return refuse(f"`{old}` is a permanent branch; do not rename it",
                      "dev branch new <type>/<topic> (your uncommitted changes come along)")
    upstream = core.gitout(c.top, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream or (c.has_origin and _ref_sha(c.top, f"refs/remotes/origin/{old}")):
        return refuse(f"`{old}` is already on origin: renaming now leaves the remote branch (and any PR) behind",
                      "keep the name, or ask the user before replacing the remote branch")
    rc, _, err = core.git(c.top, "branch", "-m", old, new)
    if rc != 0:
        return fail(f"git branch -m {old} {new} failed", err)
    ok(f"renamed {old} → {new}")
    return 0


def _prs(c, limit=100):
    """Recent PRs of the repository (any state), or None when GitHub is not reachable."""
    if c.provider != "github":
        return None
    data, _ = gh_json(["pr", "list", "-R", c.slug, "--state", "all", "--limit", str(limit), "--json",
                       "number,state,headRefName,headRefOid,updatedAt,title,url,isCrossRepository,mergeCommit"], c)
    return data if isinstance(data, list) else None


def _pr_by_branch(prs) -> dict:
    out = {}
    for p in sorted(prs or [], key=lambda p: (p.get("state") == "OPEN", p.get("updatedAt") or "")):
        out[p.get("headRefName")] = p  # the open one (or the most recent) wins
    return out


def branch_rows(c, offline=False, prs=None) -> list:
    """prs: the repository's pull requests, already read (None: read them here, unless offline)."""
    fmt = "%(refname:short)%00%(upstream:short)%00%(upstream:track,nobracket)%00%(committerdate:unix)%00%(objectname)"
    base = _base_ref(c)
    merged = set(core.gitout(c.top, "branch", "--format=%(refname:short)", "--merged", base).splitlines())
    wts = _worktree_branches(c.top)
    prs = _pr_by_branch(prs if prs is not None else None if offline else _prs(c))
    rows = []
    for ln in core.gitout(c.top, "for-each-ref", "--format=" + fmt, "refs/heads").splitlines():
        parts = (ln.split("\x00") + [""] * 5)[:5]
        name, upstream, track, ts, sha = parts
        ahead = behind = 0
        for part in track.split(","):
            part = part.strip()
            if part.startswith("ahead "):
                ahead = int(part[6:])
            elif part.startswith("behind "):
                behind = int(part[7:])
        lr = core.gitout(c.top, "rev-list", "--left-right", "--count", f"{base}...{name}").split()
        b_behind, b_ahead = (int(lr[0]), int(lr[1])) if len(lr) == 2 else (0, 0)
        pr = prs.get(name)
        rows.append({"name": name, "current": name == c.branch, "sha": sha[:7], "upstream": upstream or None,
                     "gone": track.strip() == "gone", "ahead": ahead, "behind": behind, "base": base,
                     "base_ahead": b_ahead, "base_behind": b_behind,
                     "merged": name != c.default and (name in merged or (pr or {}).get("state") == "MERGED"),
                     "pr": {"number": pr["number"], "state": pr["state"].lower(), "head": pr.get("headRefOid")}
                     if pr else None,
                     "worktree": wts.get(name), "last_commit": int(ts) if ts.isdigit() else 0})
    return rows


def _branch_ls(c, a) -> int:
    rows = branch_rows(c, offline=a.offline)
    if a.json:
        return emit(rows)
    table = []
    for r in rows:
        if r["gone"]:
            up = "gone"
        elif r["upstream"]:
            up = " ".join(x for x in (r["upstream"], f"↑{r['ahead']}" if r["ahead"] else "",
                                      f"↓{r['behind']}" if r["behind"] else "") if x) or r["upstream"]
            up = up if (r["ahead"] or r["behind"]) else up + " ="
        else:
            up = "-"
        vs = "merged" if r["merged"] else ("=" if r["name"] == c.default else f"+{r['base_ahead']} −{r['base_behind']}")
        pr = f"#{r['pr']['number']} {r['pr']['state']}" if r["pr"] else "-"
        where = "" if not r["worktree"] or core.real(r["worktree"]) == core.real(c.top) else core.tilde(r["worktree"])
        table.append([("* " if r["current"] else "  ") + r["name"], up, vs, pr, _age(r["last_commit"]), where])
    print(ui.table(table, ["BRANCH", "UPSTREAM", f"VS {_base_ref(c).upper()}", "PR", "AGE", "WORKTREE"],
                   max_widths={0: 48, 5: 60}, indent=0))
    return 0


def _clean_reason(c, r, default, prs=None):
    """(why branch `r` may be deleted, None, True) or (None, why it is kept, merged). Merged is the merge test of
    `dev branch prune` (branches.merge_test): its tip is in the base, its merged pull request from this repository had
    its tip, or every commit has an equivalent in the base. prs: the repository's pull requests (None: no PR test)."""
    from . import branches
    m = branches.merge_test(c.top, r["name"], r["base"], prs=prs) if r["name"] != default else {"merged": False}
    if r["current"]:
        return None, "current branch", m["merged"]
    if r["name"] in (default,) or r["name"] in guard.PERMANENT_BRANCHES:
        return None, "permanent branch", m["merged"]
    if r["worktree"]:
        return None, f"checked out in {core.tilde(r['worktree'])}", m["merged"]
    if m["merged"]:
        return branches.merge_words(m, r["base"]), None, True
    pr = r.get("pr")
    if pr and pr["state"] == "merged":  # from a fork, merged elsewhere, or the branch moved on since
        return None, f"PR #{pr['number']} merged, but " + branches.merge_words(m, r["base"]).split(": ", 1)[-1], False
    if r["gone"]:
        unique = int(core.gitout(c.top, "rev-list", "--count", f"refs/heads/{r['name']}", "--not", "--remotes") or 0)
        if unique == 0:
            return "gone upstream, all commits are on the remote", None, False
        return None, f"gone upstream but {unique} commit(s) exist only here", False
    return None, "", False


def _branch_clean(c, a) -> int:
    if not a.offline:
        _fetch(c)
    prs = None if a.offline else _prs(c)  # read once: the rows and the merge test share them
    rows = branch_rows(c, prs=prs or [])
    drop, keep = [], []
    for r in rows:
        why, kept, merged = _clean_reason(c, r, c.default, prs)
        if why:
            drop.append((r["name"], why))
        elif kept and (r["gone"] or merged or (r.get("pr") or {}).get("state") == "merged"):
            keep.append((r["name"], kept))
    if a.json:
        return emit({"delete": [{"branch": b, "why": w} for b, w in drop],
                     "keep": [{"branch": b, "why": w} for b, w in keep], "applied": bool(a.apply)})
    for b, w in drop:
        print(f"  {'deleted' if a.apply else 'delete '}  {b}  {ui.dim(w)}")
    for b, w in keep:
        print(f"  kept     {b}  {ui.dim(w)}")
    if not drop:
        ok("no merged or gone branches to delete")
        return 0
    if not a.apply:
        print(f"{ui.INFO} dry run: {len(drop)} branch(es) can go · dev branch clean --apply")
        return 0
    failed = []
    for b, _ in drop:
        rc, _, err = core.git(c.top, "branch", "-D", b)
        if rc != 0:
            failed.append(f"{b}: {_first_line(err)}")
    if failed:
        return fail(f"could not delete {len(failed)} branch(es)", "\n".join(failed))
    ok(f"deleted {len(drop)} local branch(es)")
    return 0


# --------------------------------------------------------------------------- dev commit


def _shortstat(top, rev="HEAD"):
    out = core.gitout(top, "show", "--shortstat", "--format=", rev)
    nums = {k: int(v) for v, k in re.findall(r"(\d+) (file|insertion|deletion)", out)}
    return nums.get("file", 0), nums.get("insertion", 0), nums.get("deletion", 0)


def _branch_gate(c):
    """Refusal for committing on the default branch or on a non-conforming branch name, else None."""
    if c.branch and c.has_origin and (c.branch == c.default or c.branch in ("main", "master")):
        return refuse(f"you are on `{c.branch}`: changes go through a branch and a PR",
                      "dev branch new <type>/<topic> (your uncommitted changes come along), then commit")
    v = guard.branch_violation(c.branch) if c.branch else None
    if v:
        return refuse(v.split(" Rename it:")[0], "dev branch rename <type>/<topic> before the first commit")
    return None


def _guard_commit(c, message: str):
    """The guard's commit rules (attribution, staged-secret scan) for a commit devkit runs itself."""
    text = "git commit -m " + shlex.quote(message)
    return guard.check_git(["git", "commit", "-m", message], c.top, text)


# The commit template (config/git/commit-template.txt, spec P-fy3bwa0pshk R9): the header; the why, what changed and
# `Verified:` paragraphs; then the `Refs:` and `BREAKING CHANGE:` trailers. devkit's task ids stay in the private
# handoff, whose session log links commits to tasks: `Refs:` names only what the repository's host or tracker knows.
TICKET_LINKS = ("ticket", "issue", "jira", "ref", "refs")
_REF_ISSUE = re.compile(r"^(?:[\w.-]+/[\w.-]+)?#\d+$")
_REF_URL = re.compile(r"^https?://\S+$")


def ticket_ref(value: str):
    """A `Refs:` value from a task link (`ticket:PROJ-142`, `issue:#12`, a URL) or from `--refs`: an issue (`#12`,
    `owner/repo#12`), a URL or a ticket key; None for anything else, devkit's own task ids and links included."""
    v = (value or "").strip()
    kind, sep, rest = v.partition(":")
    if sep and kind.lower() in TICKET_LINKS:
        v = rest.strip()
    if not v or _WORK_ID.search(v):
        return None
    if v.isdigit():
        v = "#" + v
    return v if _REF_ISSUE.match(v) or _REF_URL.match(v) or _JIRA.fullmatch(v) else None


def task_tickets(c) -> list:
    """The tickets of the tasks started on this branch and not finished: `dev task start` links the branch
    (`branch:<name>`) and keeps the lock holder in `claimed_by`; `dev task release|done` clears them."""
    if not (c and c.branch and c.pid):
        return []
    try:
        from . import work
        items = work.all_items(c.pid)
    except Exception:
        return []
    out = []
    for it in items:
        m = it.get("meta") or {}
        links = m.get("links") or []
        if isinstance(links, str):
            links = [x.strip() for x in links.strip("[]").split(",")]
        if (not m.get("claimed_by") or m.get("status") in ("done", "cancelled")
                or f"branch:{c.branch}" not in links):
            continue
        for ref in filter(None, map(ticket_ref, links)):
            if ref not in out:
                out.append(ref)
    return out


CAUSE_TYPES = ("fix", "revert")  # the commits that must name their root cause (--cause)
CAUSE_MIN = 15  # characters: a cause is a sentence about what made it fail, not a word
# the words a root cause may not be (a list, not unfinished work)
CAUSE_PLACEHOLDERS = frozenset({"unknown", "n/a", "na", "tbd", "todo", "none", "-", "?"})  # devkit: allow-stub list
_CAUSE_LABEL = re.compile(r"(?i)^root cause:\s*")


def cause_text(cause) -> str:
    """The root cause as one line, without a `Root cause:` label the caller already wrote."""
    return _CAUSE_LABEL.sub("", " ".join(str(cause or "").split()))


def cause_violation(header: str, cause):
    """Why a commit's root cause is refused, or None. A fix or revert commit names one (`--cause`); a cause is a
    sentence of at least CAUSE_MIN characters, never a placeholder. `cause` is None when the flag was not given."""
    m = CC_RE.match((header or "").strip())
    kind = m.group("type") if m else ""
    if cause is None:
        return f"a `{kind}` commit names its root cause: add --cause" if kind in CAUSE_TYPES else None
    text = cause_text(cause)
    if not text:
        return "--cause is empty: say what caused the problem"
    if text.lower().rstrip(".!") in CAUSE_PLACEHOLDERS:
        return f"--cause `{text}` is a placeholder, not a cause: say what caused the problem"
    if len(text) < CAUSE_MIN:
        return (f"--cause `{text}` has {len(text)} characters; a root cause takes at least {CAUSE_MIN} characters: "
                "say what caused the problem")
    return None


def commit_message(header: str, why: str = "", paragraphs=(), verified: str = "", refs=(), breaking: str = "",
                   cause: str = "") -> str:
    """The message in the commit template's order. `cause` becomes the `Root cause:` paragraph right after the why;
    `verified` gets its `Verified:` label when it has none."""
    c = cause_text(cause)
    body = [p.strip() for p in [why, f"Root cause: {c}" if c else "", *paragraphs] if p and p.strip()]
    v = (verified or "").strip()
    if v:
        body.append(v if v.lower().startswith("verified:") else f"Verified: {v}")
    trailers = [f"Refs: {', '.join(refs)}"] if refs else []
    if breaking and breaking.strip():
        trailers.append(f"BREAKING CHANGE: {breaking.strip()}")
    return "\n\n".join([header] + body + (["\n".join(trailers)] if trailers else []))


def _task_id_refusal(ids) -> int:
    return refuse(f"the message names devkit task id {', '.join(ids)}: task ids stay in the private handoff, whose "
                  "session log links each commit to its task", 'name the issue or ticket instead (--refs "#12", '
                  "--refs PROJ-142 or a URL), or leave the id out")


def _commit_refs(c, a, text: str):
    """-> (refs, refusal exit code or None, what the output says about them)."""
    refs = []
    for r in getattr(a, "refs", None) or []:
        ids = _WORK_ID.findall(r)
        if ids:
            return [], _task_id_refusal(ids), ""
        t = ticket_ref(r)
        if not t:
            return [], refuse(f"Refs takes an issue number (#12 or owner/repo#12), a URL or a ticket key (PROJ-142), "
                              f"not `{r}`"), ""
        refs.append(t)
    if refs or re.search(r"(?mi)^refs:", text):
        return refs, None, ""
    found = task_tickets(c)
    if len(found) == 1:
        return found, None, f"Refs: {found[0]} from the task on {c.branch}"
    if found:
        return [], None, f"no Refs: the tasks on {c.branch} name {', '.join(found)}; name one with --refs"
    return [], None, ""


# Crew members who work in one checkout share one git index. In a crew lane `dev commit` commits
# from a temporary index that holds HEAD plus the lane's own staged entries, so the shared index keeps every other
# staged entry exactly as it was, for its owner. The main session, a developer's shell and `dev commit <paths>` commit
# as before.


def _commit_lane(c):
    """The crew lane that runs this `dev commit` -> (lane id, scope layers, the folder its globs count from), or None
    in the main session and a developer's shell. The lane is DEVKIT_FROM_AGENT when the hooks set it, else the member
    the call ledger names (progress._as_lane); its scope is the one the guard checks its edits against: its own globs
    and those of the open steps it owns, inside its parents' (progress.lane_scope), counted from guard.scope_base."""
    from . import progress
    aid = progress._as_lane(os.environ.get("DEVKIT_FROM_AGENT") or None, c.top)
    if not aid:
        return None
    agent, sid = progress.current_session(c.top)
    run, lane = progress.lane_of(agent, sid, aid)
    layers = progress.lane_scope(run, aid) if lane else []
    return aid, layers, guard.scope_base((run or {}).get("project"), c.top)


def _own(path: str, top, layers, base) -> bool:
    """Is a repository-relative path inside every layer of the lane's scope? Without a scope, nothing is."""
    return bool(layers) and all(guard.scope_holds(path, top, layer, base=base) for layer in layers)


def _git_z(top, *args, env=None, input=None):
    """A git call that reads or writes NUL-separated paths, kept byte for byte (surrogate escapes) -> (rc, out, err)."""
    try:
        p = subprocess.run(["git", "-C", str(top), *args], capture_output=True, encoding="utf-8",
                           errors="surrogateescape", env=env or core.GIT_ENV, input=input, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, "", str(e)
    return p.returncode, p.stdout, p.stderr.strip()


def _zsplit(out: str) -> list:
    return [x for x in (out or "").split("\0") if x]


def _few(paths, n: int = 6) -> str:
    return ", ".join(paths[:n]) + (f" and {len(paths) - n} more" if len(paths) > n else "")


def _lane_index(top, head: str, paths):
    """A temporary index of HEAD plus the shared index's entries for `paths`: their staged content, and their removal
    when the shared index no longer has them. A commit from it carries those paths and nothing else; the shared index
    is only read. -> (folder to delete, index file, error)."""
    folder = tempfile.mkdtemp(prefix="devkit-index-")
    index = os.path.join(folder, "index")
    env = dict(core.GIT_ENV, GIT_INDEX_FILE=index)
    rc, out, err = _git_z(top, "read-tree", head or "--empty", env=env)
    if rc == 0:
        rc, out, err = _git_z(top, "ls-files", "-s", "-z")
    if rc == 0:
        entries = {}
        for rec in _zsplit(out):  # "mode blob stage\tpath", one per stage of a path
            entries.setdefault(rec.split("\t", 1)[-1], []).append(rec)
        lines = []
        for p in paths:
            if head:
                lines.append(f"0 {'0' * len(head)}\t{p}")  # mode 0 drops HEAD's entry, before the staged ones
            lines.extend(entries.get(p, []))
        rc, out, err = _git_z(top, "update-index", "-z", "--index-info", env=env,
                              input="".join(x + "\0" for x in lines))
    if rc != 0:
        shutil.rmtree(folder, ignore_errors=True)
        return None, None, err or out
    return folder, index, ""


@contextlib.contextmanager
def _index_file(index):
    """While it runs, core's git calls use `index` as the index, so the guard's staged-secret scan and the commit see
    the lane's index and not the shared one. Without an index nothing changes."""
    old = core.GIT_ENV.get("GIT_INDEX_FILE")
    if index:
        core.GIT_ENV["GIT_INDEX_FILE"] = index
    try:
        yield
    finally:
        if index and old is None:
            core.GIT_ENV.pop("GIT_INDEX_FILE", None)
        elif index:
            core.GIT_ENV["GIT_INDEX_FILE"] = old


def _sync_shared_index(top, before: str, left) -> str:
    """After a lane's commit, the shared index takes the commit's version of every path the commit changed, except the
    paths another lane keeps staged there. A pre-commit hook that fixed and re-staged a file changed only the lane's
    own index: without this the shared index would still hold the old version, a staged revert of the hook's fix that
    the next commit would carry. -> "" or the error."""
    if before:
        rc, out, err = _git_z(top, "diff-tree", "-r", "--name-only", "--no-renames", "-z", before, "HEAD")
    else:
        rc, out, err = _git_z(top, "diff-tree", "-r", "--root", "--no-commit-id", "--name-only", "--no-renames", "-z",
                              "HEAD")
    if rc != 0:
        return err or out
    keep = set(left)
    paths = [p for p in _zsplit(out) if p not in keep]
    if not paths:
        return ""
    rc, out, err = _git_z(top, "--literal-pathspecs", "reset", "-q", "HEAD", "--pathspec-from-file=-",
                          "--pathspec-file-nul", input="".join(p + "\0" for p in paths))
    return "" if rc == 0 else (err or out or "git reset failed")


def _lane_refusal(layers, left) -> int:
    """No staged file of the lane's own: what stays staged, and why."""
    if not layers:
        return refuse(f"your crew lane has no file scope, so devkit cannot tell which of the {len(left)} staged "
                      f"file(s) are yours; they stay staged: {_few(left)}",
                      'name your files: dev commit -m "type(scope): summary" <paths>')
    where = "; ".join(", ".join(layer) for layer in layers)
    return refuse(f"none of the {len(left)} staged file(s) is in your step's scope ({where}); they stay staged for "
                  f"their owner: {_few(left)}",
                  'stage and commit your files in one step: dev commit -m "type(scope): summary" <paths>')


def cmd_commit(a) -> int:
    c = ctx()
    if c is None:
        return _need_repo(c)
    if a.unwip:
        return _unwip(c)
    if a.wip:
        return _wip(c, a)
    msgs = list(a.message or [])
    template = [f"--{k}" for k in ("why", "cause", "verified", "refs", "breaking") if getattr(a, k, None)]
    if template and not msgs:
        return refuse(f"{', '.join(template)} {'writes' if len(template) == 1 else 'write'} the message around its "
                      f"header", 'dev commit -m "type(scope): summary" --why "…" --verified "…"')
    if not msgs and not a.amend:
        return refuse("give a message", 'dev commit -m "type(scope): summary" [--why "…"] [--verified "…"]')
    g = _branch_gate(c)
    if g:
        return g
    message, removed, said = "", 0, ""
    if msgs:
        first, _, rest = msgs[0].partition("\n")
        header = first.strip()
        what = [x for x in ([rest.strip()] + msgs[1:] + list(a.body or [])) if x and x.strip()]
        v = message_violation(header)
        if v:
            return refuse(v)
        cause = getattr(a, "cause", None)
        v = cause_violation(header, cause)
        if v:
            return refuse(v, f'dev commit -m {shlex.quote(header)} --why "…" --cause "<what caused it, as your '
                             f'reproduction proved>" --verified "…" (the root-cause skill)')
        refs, refused, said = _commit_refs(c, a, "\n".join(what))
        if refused is not None:
            return refused
        message, removed = strip_attribution(commit_message(header, getattr(a, "why", None) or "", what,
                                                            getattr(a, "verified", None) or "", refs,
                                                            getattr(a, "breaking", None) or "", cause or ""))
        ids = list(dict.fromkeys(_WORK_ID.findall(message)))
        if ids:
            return _task_id_refusal(ids)
    lane = None if a.paths else _commit_lane(c)
    if a.paths:
        rc, _, err = core.git(c.top, "add", "--", *a.paths)
        if rc != 0:
            return fail("git add failed", err)
    elif a.all and lane:  # only the lane's tracked changes: the others' work stays unstaged
        mine = [p for p in _zsplit(_git_z(c.top, "diff", "--name-only", "--no-renames", "-z")[1])
                if _own(p, c.top, lane[1], lane[2])]
        rc, _, err = core.git(c.top, "--literal-pathspecs", "add", "-u", "--", *mine) if mine else (0, "", "")
        if rc != 0:
            return fail("git add failed", err)
    elif a.all:
        core.git(c.top, "add", "-u")
    staged = core.gitout(c.top, "diff", "--cached", "--name-only").splitlines()
    if not staged and not a.amend:
        return fail("nothing staged to commit",
                    "stage files, list them (dev commit -m … <paths>), or use -a for every tracked change")
    folder = index = None
    head, left = "", []
    if lane:
        rc, out, err = _git_z(c.top, "diff", "--cached", "--name-only", "--no-renames", "-z")
        if rc != 0:
            return fail("git diff failed", err or out)
        names = _zsplit(out)
        own = [p for p in names if _own(p, c.top, lane[1], lane[2])]
        mine = set(own)
        left = [p for p in names if p not in mine]
        if not own and not a.amend:
            return _lane_refusal(lane[1], left)
        head = core.gitout(c.top, "rev-parse", "-q", "--verify", "HEAD")
        folder, index, err = _lane_index(c.top, head, own)
        if not folder:
            return fail("could not prepare the lane's own index", err)
    try:
        with _index_file(index):
            v = _guard_commit(c, message)
            if v:
                return refuse(v[1])
            args = ["commit", "-q"] + (["--amend"] if a.amend else []) + (["-m", message] if message else ["--no-edit"])
            if a.paths:
                args += ["--", *a.paths]
            if lane and core.gitout(c.top, "rev-parse", "-q", "--verify", "HEAD") != head:
                return fail("another commit landed while this one was prepared; nothing was committed and the staged "
                            "files are as they were", "run the same dev commit again")
            pushed_before = a.amend and bool(core.gitout(c.top, "branch", "-r", "--contains", "HEAD"))
            rc, out, err = core.git(c.top, *args, timeout=900)
    finally:
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
    if rc != 0:
        return fail("git commit failed", err or out)
    synced = _sync_shared_index(c.top, head, left) if lane else ""
    sha = core.gitout(c.top, "rev-parse", "--short", "HEAD")
    subject = core.gitout(c.top, "log", "-1", "--format=%s")
    files, ins, dels = _shortstat(c.top)
    ahead = _count(c.top, f"{_base_ref(c)}..HEAD") if c.has_origin else 0
    extra = [f"{files} file(s) +{ins} −{dels}", c.branch + (f" ↑{ahead}" if ahead else "")]
    if said:
        extra.append(said)
    if left:
        extra.append(f"left {len(left)} staged file(s) outside your scope for their owner: {_few(left)}")
    if removed:
        extra.append(f"removed {removed} AI-attribution line(s)")
    if synced:
        extra.append(f"the shared index still holds the old version of what you committed ({synced}): run "
                     "git reset -q -- <your committed paths>")
    if pushed_before:
        extra.append("amended a pushed commit: the next push needs dev push --force-with-lease")
    ok(f"{sha} {subject} · " + " · ".join(extra))
    if message and cause_text(getattr(a, "cause", None)):  # that the commit names its root cause, never the cause
        from . import audit
        audit.record("dev.commit.cause", sha, "", "ok", commit=core.gitout(c.top, "rev-parse", "HEAD"),
                     check="cause: yes")
    return 0


def _wip(c, a) -> int:
    core.git(c.top, "add", "-A")
    staged = core.gitout(c.top, "diff", "--cached", "--name-only").splitlines()
    if not staged:
        return fail("nothing to save: the working tree is clean")
    what = (a.message[0].strip() if a.message else "") or "work in progress"
    message = (f"wip: {what}\n\nSaved by `dev commit --wip` instead of a stash (the stash is shared by every "
               f"worktree). Restore with `dev commit --unwip`; never push it.")
    v = _guard_commit(c, message)
    if v:
        return refuse(v[1])
    rc, out, err = core.git(c.top, "commit", "-q", "--no-verify", "-m", message, timeout=300)
    if rc != 0:
        return fail("git commit failed", err or out)
    ok(f"{core.gitout(c.top, 'rev-parse', '--short', 'HEAD')} wip saved ({len(staged)} file(s)) on "
       f"{c.branch or 'detached HEAD'} · restore: dev commit --unwip")
    return 0


def _unwip(c) -> int:
    subject = core.gitout(c.top, "log", "-1", "--format=%s")
    if not subject.lower().startswith("wip:"):
        return refuse(f"HEAD is not a WIP commit (`{subject[:60]}`)")
    if core.gitout(c.top, "branch", "-r", "--contains", "HEAD"):
        return refuse("this WIP commit is already on the remote; undoing it would rewrite pushed history")
    n = len(core.gitout(c.top, "show", "--name-only", "--format=", "HEAD").splitlines())
    if _ref_sha(c.top, "HEAD~1"):
        rc, _, err = core.git(c.top, "reset", "-q", "--mixed", "HEAD~1")
    else:
        rc, _, err = core.git(c.top, "update-ref", "-d", "HEAD")
        if rc == 0:
            core.git(c.top, "rm", "-q", "-r", "--cached", ".")
    if rc != 0:
        return fail("could not undo the WIP commit", err)
    ok(f"restored {n} file(s) from the WIP commit as uncommitted changes")
    return 0


# --------------------------------------------------------------------------- dev rebase


def _rebase_dir(top):
    for d in ("rebase-merge", "rebase-apply"):
        p = core.gitout(top, "rev-parse", "--git-path", d)
        path = Path(p) if os.path.isabs(p) else Path(top) / p
        if p and path.exists():
            return path
    return None


def _conflict_report(c, out: str) -> int:
    files = core.gitout(c.top, "diff", "--name-only", "--diff-filter=U").splitlines()
    m = re.search(r"[Cc]ould not apply ([0-9a-f]{7,})\.\.\.\s*(.*)", out)
    at = f" while applying {m.group(1)[:7]} {m.group(2).strip()[:60]}" if m else ""
    print(f"{ui.FAIL} rebase stopped: conflicts in {len(files)} file(s){at}", file=sys.stderr)
    for f in files[:10]:
        print(f"    {f}", file=sys.stderr)
    if len(files) > 10:
        print(f"    … {len(files) - 10} more", file=sys.stderr)
    print(f"  {ui.ARROW} resolve them, `git add <files>`, then `dev rebase --continue` (or `dev rebase --abort`)",
          file=sys.stderr)
    return 1


def cmd_rebase(a) -> int:
    c = ctx()
    if c is None:
        return _need_repo(c)
    env = dict(core.GIT_ENV, GIT_EDITOR="true", GIT_SEQUENCE_EDITOR="true")
    if a.cont or a.abort or a.skip:
        if not _rebase_dir(c.top):
            return refuse("no rebase in progress")
        op = "--continue" if a.cont else "--abort" if a.abort else "--skip"
        rc, out, err = core.sh(["git", "-C", c.top, "rebase", op], env=env, timeout=600)
        if rc != 0:
            if _rebase_dir(c.top):
                return _conflict_report(c, out + "\n" + err)
            return fail(f"git rebase {op} failed", err or out)
        done = {"--continue": "finished", "--abort": "aborted", "--skip": "continued after the skip"}[op]
        ok(f"rebase {done} · {core.gitout(c.top, 'branch', '--show-current') or 'detached'}"
           f" @ {core.gitout(c.top, 'rev-parse', '--short', 'HEAD')}")
        return 0
    if _rebase_dir(c.top):
        return refuse("a rebase is already in progress", "dev rebase --continue | --abort")
    if not c.branch:
        return refuse("detached HEAD: nothing to rebase", "dev branch rename <type>/<topic> creates a branch here")
    if not c.has_origin:
        return refuse("no `origin` remote to sync with")
    n = _dirty(c.top, untracked=False)
    if n:
        return refuse(f"{n} uncommitted change(s): a rebase needs a clean tree",
                      "dev commit --wip (and dev commit --unwip after the rebase)")
    fetched, err = _fetch(c)
    if not fetched:
        return fail("git fetch failed", err)
    onto = a.base or f"origin/{c.default}"
    if not _ref_sha(c.top, onto):
        return refuse(f"`{onto}` does not exist")
    if c.branch == c.default:
        ahead = _count(c.top, f"{onto}..HEAD")
        if ahead:
            return refuse(f"local `{c.branch}` has {ahead} commit(s) that are not on {onto}",
                          "move them to a branch: dev branch new <type>/<topic> --base HEAD")
        behind = _count(c.top, f"HEAD..{onto}")
        if not behind:
            ok(f"{c.branch} is up to date with {onto}")
            return 0
        rc, _, err = core.git(c.top, "merge", "--ff-only", "-q", onto)
        if rc != 0:
            return fail(f"fast-forward to {onto} failed", err)
        ok(f"{c.branch} fast-forwarded to {onto} @ {core.gitout(c.top, 'rev-parse', '--short', 'HEAD')} "
           f"({behind} new commit(s))")
        return 0
    behind = _count(c.top, f"HEAD..{onto}")
    if not behind:
        ok(f"{c.branch} is up to date with {onto}")
        return 0
    mine = _count(c.top, f"{onto}..HEAD")
    pushed = bool(_ref_sha(c.top, f"refs/remotes/origin/{c.branch}"))
    rc, out, err = core.sh(["git", "-C", c.top, "rebase", onto], env=env, timeout=600)
    if rc != 0:
        if _rebase_dir(c.top):
            return _conflict_report(c, out + "\n" + err)
        return fail(f"git rebase {onto} failed", err or out)
    tail = ["re-run the local suite: dev ci --run"]
    if pushed and mine:
        tail.insert(0, "next push: dev push --force-with-lease")
    ok(f"{c.branch} rebased on {onto} · {behind} new upstream commit(s), {mine} of yours replayed · " + " · ".join(tail))
    return 0


# --------------------------------------------------------------------------- dev push

_WIP_RE = re.compile(r"(?i)^(wip\b|fixup!|squash!|amend!)")


def _push_url(c, err: str):
    for m in re.finditer(r"remote:\s+(https?://\S+)", err or ""):
        return m.group(1)
    return None


def _pr_for(c, branch):
    if c.provider != "github":
        return None
    data, _ = gh_json(["pr", "view", branch, "-R", c.slug, "--json", "number,url,state,isDraft"], c)
    return data if isinstance(data, dict) and data.get("state") == "OPEN" else None


def push_budget_count(c, branch) -> int:
    main = core.gitout(c.top, "rev-parse", "--path-format=absolute", "--git-common-dir") or c.top
    data = core.read_json(core.STATE / "ci-pushes.json", {}) or {}
    return len([t for t in data.get(f"{main}|{branch}", []) if time.time() - t < guard.PUSH_WINDOW_S])


def cmd_push(a) -> int:
    c = ctx()
    if c is None:
        return _need_repo(c)
    return push_branch(c, lease=a.force_with_lease, ci_passed=a.ci_passed, dry_run=a.dry_run,
                       allow_behind=a.allow_behind)


def push_branch(c, lease=False, ci_passed=False, dry_run=False, allow_behind=False) -> int:
    b = c.branch
    if not b:
        return refuse("detached HEAD: nothing to push", "dev branch rename <type>/<topic> creates a branch here")
    if not c.has_origin:
        return refuse("no `origin` remote")
    if b == c.default or b in guard.PERMANENT_BRANCHES:
        return refuse(f"`{b}` is the default branch: changes go through a branch and a PR",
                      "dev branch new <type>/<topic> (only when the user explicitly asks: plain `git push`)")
    v = guard.branch_violation(b)
    if v:
        return refuse(v.split(" Rename it:")[0], "dev branch rename <type>/<topic> before pushing")
    fetched, err = _fetch(c)
    if not fetched:
        return fail("git fetch failed; not pushing on stale remote state", err)
    head = _ref_sha(c.top, "HEAD")
    remote = _ref_sha(c.top, f"refs/remotes/origin/{b}")
    if remote == head:
        if not core.gitout(c.top, "rev-parse", "--abbrev-ref", "@{u}"):
            core.git(c.top, "branch", f"--set-upstream-to=origin/{b}")
        ok(f"{b} is already on origin @ {head[:7]} · nothing to push")
        return 0
    if remote and _is_ancestor(c.top, head, remote):
        return refuse(f"origin/{b} has {_count(c.top, f'HEAD..{remote}')} commit(s) you do not have",
                      f"git merge --ff-only origin/{b}")
    diverged = bool(remote) and not _is_ancestor(c.top, remote, head)
    if diverged and not lease:
        return refuse(f"`{b}` has diverged from origin/{b} (rebased or amended)",
                      "dev push --force-with-lease (on your own branch only)")
    if diverged:
        me = _me_email(c.top)
        others = sorted({ln.split("\t", 1)[1] for ln in core.gitout(c.top, "log", "--format=%ae\t%an",
                                                                     f"HEAD..{remote}").splitlines()
                         if "\t" in ln and ln.split("\t", 1)[0].lower() != me})
        if others:
            return refuse(f"the force push would drop commits by {', '.join(others)} on origin/{b}",
                          f"integrate them first: git rebase origin/{b}")
    base = _base_ref(c)
    excl = [base] + ([remote] if remote and not diverged else [])
    log = core.gitout(c.top, "log", "--format=%h\t%s", "HEAD", "--not", *excl).splitlines()
    wip = [ln for ln in log if _WIP_RE.match(ln.split("\t", 1)[-1])]
    if wip:
        return refuse(f"{len(wip)} WIP/fixup commit(s) would be pushed ({wip[0].replace(chr(9), ' ')[:60]})",
                      "dev commit --unwip and commit properly (WIP commits never reach the remote)")
    behind = _count(c.top, f"HEAD..{base}")
    if behind and not remote and not allow_behind:
        return refuse(f"`{b}` is {behind} commit(s) behind {base}: rebase before review",
                      "dev rebase, re-run the local suite, then dev push (or --allow-behind)")
    ev = evidence(c, host_only=True) if dry_run else _evidence_or_attest(c, ci_passed, "push")
    if not ev and not (dry_run and ci_passed):
        return no_evidence(c, "push")
    envs = frozenset({"DEVKIT_LOCAL_CI_PASSED"} if ev and ev.get("how") == "run" else ())  # only a real run lifts it
    args = ["-u", "origin", b] + ([f"--force-with-lease={b}:{remote}"] if diverged else [])
    what = f"{len(log)} commit(s)" + (" · force-with-lease" if diverged else "") + (" · first push" if not remote else "")
    if dry_run:
        n = push_budget_count(c, b)
        ev_text = describe_evidence(ev) if ev else "attested with --ci-passed (recorded on the real push)"
        ok(f"dry run: would push {b} → origin ({what}) · evidence: {ev_text} · "
           f"pushes this hour: {n}/{guard.PUSH_LIMIT}" + (f" · {behind} behind {base}" if behind else ""))
        return 0
    v = guard.check_git(["git", "push", *args], c.top, "git push " + " ".join(args), envs)
    if v:
        return refuse(v[1])
    rc, out, err = core.git(c.top, "push", *args, timeout=900)
    if rc != 0:
        return fail(f"git push failed", err or out)
    url = _push_url(c, err)
    pr = _pr_for(c, b)
    tail = []
    if pr:
        tail.append(f"PR #{pr['number']} {pr['url']}")
        tail.append("watch: dev pr checks --watch")
    else:
        if url:
            tail.append(url)
        tail.append("open the PR: dev pr create")
    if behind:
        tail.append(f"{behind} behind {base}: dev rebase before review")
    ok(f"pushed {b} → origin ({what}) @ {head[:7]} · " + " · ".join(tail))
    return 0


# --------------------------------------------------------------------------- dev ci --run | --mark-passed | --evidence
#
# The workflow `run:` steps are executed locally only when that is safe and meaningful: workflows triggered by
# push/PR, bash steps, no `${{ }}` expressions, no secrets, nothing that publishes, deploys, pushes or installs
# system packages, and no dependency reinstall (your local environment is used). Steps that cannot run here are
# listed; when one of them could be a real check, no evidence is recorded and you run it yourself, then
# `dev ci --mark-passed`.

CI_TRIGGERS = {"push", "pull_request", "pull_request_target", "merge_group"}
_DEPLOY_JOB = re.compile(r"(?i)(deploy|release|publish|pages|upload|docker[-_ ]?push|promote)")
_SIDE_EFFECT = re.compile(r"(?i)("
                          r"\b(npm|pnpm|yarn|bun)\s+publish\b|\bdocker\s+(push|login)\b|\b(twine|flit)\s+upload\b|"
                          r"\b(poetry|cargo|gem|dotnet\s+nuget)\s+(publish|push)\b|\bgh\s+\w+|\bgit\s+(push|tag|commit|config)\b|"
                          r"\b(kubectl|helm|terraform|tofu|pulumi|flyctl|vercel|netlify|firebase|heroku|serverless|sls|"
                          r"ansible-playbook|eksctl)\b|\b(aws|gcloud|gsutil|az)\s|\bsudo\b|\b(apt-get|apt|yum|dnf|apk|choco|"
                          r"brew)\s+(install|update|upgrade|add)\b|\b(curl|wget)\b[^|\n]*\|\s*(ba|z)?sh\b|\b(ssh|scp|rsync)\s|"
                          r"\bcodecov\b|\bcoveralls\b|\bmvnw?\b[^\n]*\bdeploy\b|\bgradlew?\b[^\n]*\bpublish)")
_PLUMBING = re.compile(r"\$\{?GITHUB_(ENV|OUTPUT|PATH|STEP_SUMMARY|STATE)\b")
_INSTALL = re.compile(r"(?i)^\s*(npm\s+(ci|install|i)\b|pnpm\s+(i|install)\b|yarn(\s+install)?\s*($|--)|bun\s+install|"
                      r"pip3?\s+install|python3?\s+-m\s+pip\s+install|uv\s+(sync|pip\s+install)|poetry\s+install|"
                      r"pipenv\s+install|bundle\s+install|composer\s+install|go\s+mod\s+download|corepack\s+enable)")
_SAFE_EXPR = {"github.workspace": None, "runner.temp": None, "runner.os": "macOS", "github.event_name": "pull_request"}


def _ind(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _scalar(v: str) -> str:
    v = v.strip()
    if v[:1] in ("'", '"') and v[-1:] == v[:1]:
        return v[1:-1]
    return re.sub(r"\s+#.*$", "", v)


def _mapping(lines, start, end) -> dict:
    """Block mapping in lines[start:end] -> {key: (value, body_start, body_end)} at its first indentation."""
    items, i, base = {}, start, None
    while i < end:
        s = lines[i].strip()
        if not s or s.startswith("#"):
            i += 1
            continue
        ind = _ind(lines[i])
        if base is None:
            base = ind
        if ind < base:
            break
        if ind == base and not s.startswith("- ") and ":" in s:
            key, _, val = s.partition(":")
            j = i + 1
            while j < end and (not lines[j].strip() or _ind(lines[j]) > base):
                j += 1
            items[key.strip().strip("'\"")] = (val.strip(), i + 1, j)
            i = j
        else:
            i += 1
    return items


def _sequence(lines, start, end) -> list:
    """Block sequence -> list of item line lists, each re-indented as a mapping."""
    out, i, base = [], start, None
    while i < end:
        s = lines[i].strip()
        if not s or s.startswith("#"):
            i += 1
            continue
        ind = _ind(lines[i])
        if base is None:
            base = ind
        if ind < base:
            break
        if ind == base and s.startswith("-"):
            j = i + 1
            while j < end and (not lines[j].strip() or _ind(lines[j]) > base):
                j += 1
            first = " " * (base + 2) + s[1:].lstrip()
            out.append([first] + lines[i + 1:j])
            i = j
        else:
            i += 1
    return out


def _block(lines, val, start, end) -> str:
    if val in ("|", "|-", "|+", ">", ">-", ">+"):
        body = lines[start:end]
        base = min((_ind(b) for b in body if b.strip()), default=0)
        text = "\n".join(b[base:] for b in body).strip("\n")
        if val.startswith(">"):
            text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
        return text
    return _scalar(val)


def _env(lines, entry) -> dict:
    if not entry:
        return {}
    val, s, e = entry
    if val and val.startswith("{"):
        return {}
    return {k: _scalar(v[0]) for k, v in _mapping(lines, s, e).items()}


def _defaults_wd(lines, entry):
    if not entry:
        return None
    run = _mapping(lines, entry[1], entry[2]).get("run")
    wd = _mapping(lines, run[1], run[2]).get("working-directory") if run else None
    return _scalar(wd[0]) if wd else None


def plan_workflow(text: str, fname: str, root: str) -> list:
    """-> jobs [{workflow, job, steps: [{name, run, cwd, env, skip, blocking}], skip, blocking}] for one workflow."""
    from . import ci
    wf = ci.parse_workflow(text)
    lines = text.splitlines()
    top = _mapping(lines, 0, len(lines))
    name = wf.get("name") or fname
    triggers = set(wf.get("on") or [])
    wf_env = _env(lines, top.get("env"))
    wf_wd = _defaults_wd(lines, top.get("defaults"))
    jobs_entry = top.get("jobs")
    if not jobs_entry:
        return []
    jobs = []
    for jid, (_, js, je) in _mapping(lines, jobs_entry[1], jobs_entry[2]).items():
        jm = _mapping(lines, js, je)
        jname = _scalar(jm["name"][0]) if "name" in jm else jid
        jname = re.sub(r"\$\{\{[^}]*\}\}", "", re.sub(r"\s*\([^()]*\$\{\{[^}]*\}\}[^()]*\)", "", jname)).strip()
        jname = jname or jid
        job = {"workflow": name, "file": fname, "job": jname, "steps": [], "skip": None, "blocking": False}
        jobs.append(job)
        runs_on = _scalar(jm["runs-on"][0]) if "runs-on" in jm else ""
        cond = _scalar(jm["if"][0]) if "if" in jm else ""
        if not triggers & CI_TRIGGERS:
            job["skip"] = f"workflow runs on {', '.join(sorted(triggers)) or '?'}, not on push or pull requests"
        elif "uses" in jm:
            job.update(skip=f"calls the reusable workflow {_scalar(jm['uses'][0])}", blocking=True)
        elif _DEPLOY_JOB.search(jid + " " + jname):
            job["skip"] = "deploy/publish job"
        elif "windows" in runs_on.lower():
            job["skip"] = "Windows runner (not reproducible on macOS)"
        elif cond:
            verdict = _condition(cond)
            if verdict != "run":
                job.update(skip=f"conditional job (if: {cond[:50]})", blocking=verdict == "unknown")
        if job["skip"]:
            continue
        job_env = dict(wf_env, **_env(lines, jm.get("env")))
        job_wd = _defaults_wd(lines, jm.get("defaults")) or wf_wd
        steps_entry = jm.get("steps")
        if not steps_entry:
            continue
        for item in _sequence(lines, steps_entry[1], steps_entry[2]):
            sm = _mapping(item, 0, len(item))
            st = {"job": jname, "name": "", "run": None, "cwd": root, "env": {}, "skip": None, "blocking": False}
            if "uses" in sm:
                st.update(name=_scalar(sm["uses"][0]), skip="setup")
                job["steps"].append(st)
                continue
            if "run" not in sm:
                continue
            run = _block(item, *sm["run"])
            st["run"] = run
            st["name"] = _scalar(sm["name"][0]) if "name" in sm else run.splitlines()[0][:60] if run else "run"
            st["env"] = dict(job_env, **_env(item, sm.get("env")))
            shell = _scalar(sm["shell"][0]) if "shell" in sm else ""
            scond = _scalar(sm["if"][0]) if "if" in sm else ""
            wd = _scalar(sm["working-directory"][0]) if "working-directory" in sm else job_wd
            st["skip"], st["blocking"] = _step_skip(run, shell, scond, wd, root, st["env"])
            if not st["skip"] and wd:
                st["cwd"] = str(Path(root) / wd)
            st["run"] = _expand(run, root)
            job["steps"].append(st)
    return jobs


_DRAFT_GATE = re.compile(r"\s*(&&\s*)?(github\.event\.pull_request\.draft\s*==\s*false|!\s*github\.event\.pull_request"
                         r"\.draft)\b(\s*&&)?\s*")


def _condition(cond: str) -> str:
    """'run' | 'never' (only on other events, or only after a failure) | 'unknown'."""
    c = cond.replace("${{", "").replace("}}", "").strip()
    # the draft gate (`dev ci init`, spec P-fy3bwa0pshk R6) holds for a ready PR and a push: judge what is left
    c = re.sub(r"^\((.*)\)$", r"\1", _DRAFT_GATE.sub("", c).strip()).strip()
    if not c or re.fullmatch(r"(always|success)\(\)", c):
        return "run"
    if re.fullmatch(r"(failure|cancelled)\(\)", c):
        return "never"
    if "pull_request" in c and "!=" not in c:
        return "run"
    if re.search(r"github\.ref|refs/tags|refs/heads|schedule|workflow_dispatch|release|'push'|\"push\"", c):
        return "never"
    return "unknown"


def _expand(run: str, root: str) -> str:
    import tempfile
    vals = dict(_SAFE_EXPR, **{"github.workspace": root, "runner.temp": tempfile.gettempdir()})
    return re.sub(r"\$\{\{\s*([\w.]+)\s*\}\}", lambda m: vals.get(m.group(1), m.group(0)), run)


_PRUNABLE = frozenset({"system", "image", "container", "volume", "network", "builder", "buildx"})
_SHELL_VAR = re.compile(r"\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))")


def _shell_path(word: str, cwd, env: dict):
    """The absolute path a shell word names, as the shell expands it here: `~`, and `$VAR` / `${VAR}` from `env`
    (an unset variable is empty, so `"$UNSET/"` is `/`). None when this Mac cannot tell: a command's output (the
    guard's reader leaves a `$` for `$(…)`), another expansion, or a folder `cd` went to that is not known."""
    if word == "~" or word.startswith("~/"):
        word = os.path.expanduser("~") + word[1:]
    word = _SHELL_VAR.sub(lambda m: env.get(m.group(1) or m.group(2)) or "", word)
    if "$" in word or "`" in word or word.startswith("~") or (cwd is None and not os.path.isabs(word)):
        return None
    return os.path.realpath(os.path.join(cwd or "/", word))


_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_OS_PACKAGERS = frozenset({"apt-get", "apt", "aptitude", "yum", "dnf", "microdnf", "apk", "zypper", "snap", "brew",
                           "port", "choco", "winget", "scoop"})
_PACKAGER_VERBS = frozenset({"install", "reinstall", "upgrade", "update", "dist-upgrade", "full-upgrade", "add",
                             "remove", "uninstall", "purge", "autoremove", "del", "tap", "bundle", "link"})
_GIT_CONFIG_READS = frozenset({"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l", "get", "list"})


def _segments(run: str, depth: int = 0):
    """(the words as written, the command they run) for each command of a step's script, read by the guard's command
    reader (read-only use: it unwraps env, sudo, nice, npx, uv run, if/then and the like). The script a shell runs
    with `-c` is read too, wherever it sits (`xvfb-run bash -c '…'`, `dbus-run-session -- bash -c '…'`)."""
    for argv, _ in guard.split_commands(run) or []:
        argv = list(argv)
        yield argv, guard._unwrap(argv)
        if depth < 3:
            for i, w in enumerate(argv[:-2]):
                if os.path.basename(w) in _SHELLS and re.fullmatch(r"-[a-z]*c[a-z]*", argv[i + 1]):
                    yield from _segments(argv[i + 2], depth + 1)
                    break


def _as_root(argv) -> bool:
    """The words run their command with sudo or doas, after any VAR=value assignments and env."""
    for w in argv:
        b = os.path.basename(w)
        if b in ("sudo", "doas"):
            return True
        if not (b == "env" or (re.match(r"[A-Za-z_]\w*=", w))):
            return False
    return False


def _runner_prep(run: str, cwd: str, root: str, env: dict):
    """Why a step only sets up a hosted runner, which on this Mac would change the developer's own machine, or None:
    it deletes a path that is not inside the checkout (`rm -rf /opt/hostedtoolcache/CodeQL`, a `~` path, a variable
    unset here, a command's output, `cd /opt && rm -rf ghc`), prunes Docker (`docker system prune -af`), runs a command
    as root (`sudo …`), installs OS packages (`apt-get update`, `brew install`, `playwright install --with-deps`),
    installs browsers into the shared Playwright cache (`npx playwright install chromium`; the tests then use the
    browsers installed here), or changes the global git config (`git config --global …`). The one classifier for
    runner-only setup: `dev ci --run` skips these steps and `dev ci --commands` leaves them out."""
    top = os.path.realpath(root)
    here = os.path.realpath(cwd)
    for argv, words in _segments(run):
        if not words:
            continue
        name = os.path.basename(words[0])
        if re.fullmatch(r"python[\d.]*", name) and words[1:3] == ["-m", "playwright"]:
            words, name = words[2:], "playwright"
        pos = [w for w in words[1:] if not w.startswith("-")]
        if name == "cd":
            here = _shell_path(words[1], here, env) if len(words) > 1 and words[1] != "-" else None
        elif name in ("docker", "podman"):
            if pos[:1] and pos[0] in _PRUNABLE and pos[1:2] == ["prune"]:
                return f"runner prep ({name} {pos[0]} prune deletes Docker data on this Mac)"
        elif name in _OS_PACKAGERS and pos[:1] and pos[0] in _PACKAGER_VERBS:
            return f"runner prep ({name} {pos[0]} changes the runner's OS packages)"
        elif name == "playwright" and pos[:1] in (["install"], ["install-deps"]):
            if pos[0] == "install-deps" or "--with-deps" in words:
                return f"runner prep (playwright {pos[0]}{' --with-deps' if pos[0] == 'install' else ''} installs " \
                       f"OS packages for the browsers)"
            return "runner prep (playwright install writes the shared Playwright browser cache; the browsers " \
                   "installed here are used)"
        elif name == "git" and "config" in words[1:3] and {"--global", "--system"} & set(words) and \
                not _GIT_CONFIG_READS & set(words):
            return "runner prep (git config --global changes this Mac's git settings)"
        elif name in ("rm", "rmdir", "unlink"):
            ops, opts = [], True
            for w in words[1:]:
                if opts and w == "--":
                    opts = False
                elif not (opts and w.startswith("-") and w != "-"):
                    ops.append(w)
            for w in ops:
                p = _shell_path(w, here, env)
                if p is None or not p.startswith(top + os.sep):
                    return f"runner prep (deletes {w}, which is not inside the checkout)"
        if _as_root(argv):
            return f"runner prep (sudo {' '.join(words[:2])} runs as root on the runner's machine)"
    return None


def runner_setup(run: str, root, cwd=None, env=None):
    """Why a step's script only sets up a hosted runner (see _runner_prep), or None. `cwd`: its working directory
    (the checkout by default); `env`: its variables over this Mac's, as `dev ci --run` gives them."""
    step_env = dict(os.environ, CI="true")
    step_env.update({k: v for k, v in (env or {}).items() if "${{" not in str(v)})
    return _runner_prep(_expand(run, str(root)), str(cwd or root), str(root), step_env)


_DOCKER_VALUE_OPTS = frozenset({"-H", "--host", "--config", "-l", "--log-level", "--tlscacert", "--tlscert",
                                "--tlskey"})


def _docker_contexts(env: dict) -> set:
    """The Docker contexts this Mac's Docker CLI has set up by name (its context store under $DOCKER_CONFIG or
    ~/.docker); the built-in `default` is never in it."""
    meta = Path(env.get("DOCKER_CONFIG") or os.path.expanduser("~/.docker")) / "contexts" / "meta"
    names = set()
    for f in meta.glob("*/meta.json"):
        try:
            n = json.loads(f.read_text()).get("Name")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(n, str) and n:
            names.add(n)
    return names


def _named_contexts(run: str, env: dict) -> list:
    """The Docker contexts a step's script chooses: DOCKER_CONTEXT=…, `docker --context …` / `-c …`,
    `docker context use …`, and a program's `--docker-context …`. A value this Mac cannot tell (a command's output, a
    variable unset here) names none."""
    out = []

    def add(v):
        v = _SHELL_VAR.sub(lambda m: env.get(m.group(1) or m.group(2)) or "", v or "")
        if v and "$" not in v and "`" not in v and v not in out:
            out.append(v)

    for argv, words in _segments(run):
        for w in argv:
            if w.startswith("DOCKER_CONTEXT="):
                add(w.split("=", 1)[1])
            elif not re.match(r"[A-Za-z_]\w*=", w):
                break
        if not words:
            continue
        name = os.path.basename(words[0])
        if name == "export":
            for w in words[1:]:
                if w.startswith("DOCKER_CONTEXT="):
                    add(w.split("=", 1)[1])
        if name == "docker":
            rest = words[1:]
            while rest and rest[0].startswith("-"):
                flag = rest.pop(0)
                if flag in ("-c", "--context") and rest:
                    add(rest.pop(0))
                elif flag.startswith("--context="):
                    add(flag.split("=", 1)[1])
                elif flag in _DOCKER_VALUE_OPTS and rest:
                    rest.pop(0)
            if rest[:2] == ["context", "use"] and len(rest) > 2:
                add(rest[2])
        for i, w in enumerate(words):
            if w == "--docker-context" and i + 1 < len(words):
                add(words[i + 1])
            elif w.startswith("--docker-context="):
                add(w.split("=", 1)[1])
    return out


def _foreign_context(run: str, env: dict):
    """Why a step cannot run against this Mac's Docker, or None: it names a Docker context that is not set up here
    (a hosted or self-hosted runner's), or the built-in `default`, a hosted runner's own engine, which on this Mac is
    whatever DOCKER_HOST or /var/run/docker.sock points to. It is not run against the current context instead: that
    is another engine (another architecture, the developer's own containers and networks), not the one CI uses."""
    named = _named_contexts(run, env)
    if not named:
        return None
    here = _docker_contexts(env)
    for n in named:
        if n not in here:
            where = f"contexts here: {', '.join(sorted(here)) or 'none'}"
            if n == "default":
                return f"Docker context default is a hosted runner's own engine ({where})"
            return f"Docker context {n} is not set up here ({where})"
    return None


def _step_skip(run: str, shell: str, cond: str, wd, root, env=None):
    """-> (reason or None, blocking). Blocking: a real check that could not run here. `env`: the step's variables."""
    if cond:
        verdict = _condition(cond)
        if verdict == "never":
            return f"conditional (if: {cond[:40]})", False
        if verdict == "unknown":
            return f"conditional (if: {cond[:40]})", True
    if re.search(r"\$\{\{\s*secrets\.", run) or re.search(r"\bsecrets\.", cond or ""):
        return "needs CI secrets", False
    here = Path(root) / wd if wd and not os.path.isabs(wd) and "${{" not in wd else Path(root)
    step_env = dict(os.environ, CI="true")  # what run_local_ci gives the step
    step_env.update({k: v for k, v in (env or {}).items() if "${{" not in str(v)})
    prep = _runner_prep(_expand(run, root), str(here), root, step_env)
    if prep:
        return prep, False
    if _SIDE_EFFECT.search(run):
        return "side effect (publish, deploy, push, system install)", False
    foreign = _foreign_context(_expand(run, root), step_env)
    if foreign:
        return foreign, True
    if _PLUMBING.search(run):
        return "CI plumbing ($GITHUB_ENV/OUTPUT)", False
    if _INSTALL.search(run):
        return "dependency install (your local environment is used)", False
    left = re.sub(r"\$\{\{\s*([\w.]+)\s*\}\}", lambda m: "" if m.group(1) in _SAFE_EXPR else m.group(0), run)
    if "${{" in left:
        return "uses ${{ }} expressions", True
    if shell and shell.split()[0] not in ("bash", "sh"):
        return f"shell {shell}", True
    if wd:
        if "${{" in wd or os.path.isabs(wd) or not core.is_under(Path(root) / wd, root) or \
                not (Path(root) / wd).is_dir():
            return f"working-directory {wd} not found here", True
    return None, False


def plan(root) -> list:
    d = Path(root) / ".github" / "workflows"
    jobs = []
    for f in sorted(list(d.glob("*.yml")) + list(d.glob("*.yaml"))) if d.is_dir() else []:
        try:
            jobs += plan_workflow(f.read_text(), f.name, str(root))
        except (OSError, UnicodeDecodeError):
            continue
    return jobs


def add_ci_arguments(p):
    """Extra `dev ci` options (call from cli.py's parser for `ci`)."""
    g = p.add_argument_group("local CI evidence (checked by dev push, dev pr create and dev release)", HOST_RULE)
    g.add_argument("--run", action="store_true",
                   help="run the workflow `run:` steps locally, in order, and record the evidence when all pass")
    g.add_argument("--mark-passed", action="store_true",
                   help="record that you ran the full local suite on the current tree")
    g.add_argument("--note", help="with --mark-passed: what you ran and the result (shown in the PR body)")
    g.add_argument("--evidence", action="store_true", help="show whether HEAD has local CI evidence")
    g.add_argument("--dry-run", action="store_true", help="with --run: show the plan without running it")
    g.add_argument("--keep-going", action="store_true", help="with --run: do not stop at the first failure")
    g.add_argument("--step-timeout", type=int, default=30, metavar="MIN", help="with --run: minutes per step")
    g.add_argument("--resume", action="store_true",
                   help="with --run: re-run failed and never-run steps, and passed steps the changes since can affect; "
                        "a test step re-runs only its failed and affected tests (whole when the change is global); "
                        "a passed step is kept when only CI host settings changed since its run (see above), "
                        "except a scanner that reads every file")
    return p


def ci_command(a, top):
    """Call from cmd_ci once `top` is known: an exit code when a local-evidence flag is set, else None."""
    if not (getattr(a, "run", False) or getattr(a, "mark_passed", False) or getattr(a, "evidence", False)
            or getattr(a, "resume", False)):
        return None
    c = ctx(top)
    if c is None:
        return _need_repo(c)
    if a.evidence:
        ev = evidence(c, host_only=True)
        if getattr(a, "json", False):
            return emit({"head": _head(c.top)[0], "evidence": ev})
        if ev:
            ok(f"local CI evidence for {_head(c.top)[0][:7]}: {describe_evidence(ev)}")
            return 0
        return no_evidence(c, "push")
    if a.mark_passed:
        tree = work_tree(c.top)
        if not tree:
            return fail("could not compute the working tree")
        ev = record_evidence(c, "marked", tree, note=(a.note or "").strip()[:300])
        dirty = "" if ev["clean"] else " (includes uncommitted changes: commit exactly these files)"
        ok(f"local CI recorded as passed for tree {tree[:7]} at {(ev['sha'] or 'HEAD')[:7]}{dirty} · "
           f"dev push will accept it")
        return 0
    return run_local_ci(c, dry_run=a.dry_run, keep_going=a.keep_going, step_timeout=a.step_timeout,
                        resume=getattr(a, "resume", False))


def _fmt_secs(s) -> str:
    s = int(s)
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def _steps_file(c) -> Path:
    return _ci_dir() / "steps" / f"{_key(c)}.json"


def step_records(c) -> dict:
    """The last result of every local CI step: {"<job> › <step>": {rc, tree, sha, secs, how, failed_tests, …}}."""
    data = core.read_json(_steps_file(c), {})
    return (data.get("steps") or {}) if isinstance(data, dict) else {}


def _save_step(c, key, rec) -> None:
    f = _steps_file(c)
    f.parent.mkdir(parents=True, exist_ok=True)
    data = core.read_json(f, {}) or {}
    data.setdefault("steps", {})[key] = rec
    data["repo"] = c.pid
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, f)


def _step_key(s) -> str:
    return f"{s['job']} › {s['name']}"


def _tree_diff(top, old, new):
    """Files that differ between two recorded trees; None when one of them is gone (pruned)."""
    if old == new:
        return []
    rc, out, _ = core.sh(["git", "-C", top, "diff-tree", "-r", "--name-only", "--no-renames", "-z", old, new],
                         timeout=120, env=core.GIT_ENV)
    return [f for f in out.split("\0") if f] if rc == 0 else None


# steps that read every file, documents included: a docs-only change can change their result (a leaked key in a
# README, a broken link, a style finding), so the docs shortcut never applies to them
READS_EVERY_FILE = re.compile(r"\b(?:gitleaks|trufflehog|detect-secrets|ggshield|trivy|semgrep|osv-scanner|"
                              r"markdownlint(?:-cli2?)?|vale|lychee|cspell|codespell|prettier|"
                              r"dev\s+(?:docs\s+check|sec\s+scan))\b")


def _reads_every_file(cmd: str) -> bool:
    return bool(READS_EVERY_FILE.search(cmd or ""))


def _resume_plan(c, s, rec, tree, cache):
    """-> ("kept" | "partial" | "full", detail) for one step on `tree`, from its last recorded result."""
    from . import testx
    if not rec or rec.get("command") != s["run"] or not rec.get("tree"):
        return "full", ("never run" if not rec else "its command changed")
    if rec["tree"] not in cache:
        cache[rec["tree"]] = _tree_diff(c.top, rec["tree"], tree)
    changed = cache[rec["tree"]]
    if changed is None:
        return "full", "the tree it ran on is gone"
    runner = testx.recognize(s["run"], s["cwd"], c.top)
    if rec.get("rc") != 0:
        if runner and rec.get("failed") and not runner["failfast"]:
            return "partial", {"runner": runner, "failed": rec["failed"], "changed": changed}
        return "full", "failed last time"
    if not changed:
        return "kept", "same tree"
    host = _resume_host(c, rec["tree"], tree, changed, cache)
    if host and not _reads_every_file(s["run"]):  # a scanner reads the workflow too: a comment can leak a key
        return "kept", host
    if runner:
        return "partial", {"runner": runner, "failed": [], "changed": changed}
    key = ("radius", rec["tree"])
    if key not in cache:
        cache[key] = testx.blast_radius(c.top, changed)
    if cache[key]["mode"] == "none" and not _reads_every_file(s["run"]):
        return "kept", "only docs changed since"
    return "full", f"{len(changed)} file(s) changed since"


def run_local_ci(c, dry_run=False, keep_going=False, step_timeout=30, resume=False) -> int:
    jobs = plan(c.top)
    if not jobs:
        return fail("no GitHub Actions workflows to run here",
                    'run your local suite, then: dev ci --mark-passed --note "<command>: <result>"')
    runnable = [s for j in jobs if not j["skip"] for s in j["steps"] if not s["skip"]]
    skipped = [(j["job"], j["workflow"], j["skip"], j["blocking"]) for j in jobs if j["skip"]] + \
              [(s["job"], s["name"], s["skip"], s["blocking"]) for j in jobs if not j["skip"] for s in j["steps"]
               if s["skip"] and s["skip"] != "setup"]
    setup = sum(1 for j in jobs for s in j["steps"] if s["skip"] == "setup")
    blocking = [x for x in skipped if x[3]]
    records = step_records(c) if resume else {}
    if resume and not records:
        info("nothing recorded yet: --resume runs every step")
        resume = False
    if dry_run:
        tree, cache = (work_tree(c.top) if resume else None), {}
        print(ui.section(f"Local CI plan · {len(runnable)} step(s) · {len(skipped)} skipped · {setup} setup action(s)"))
        for s in runnable:
            cwd = "" if s["cwd"] == c.top else f"  (in {os.path.relpath(s['cwd'], c.top)})"
            how = ""
            if resume and tree:
                d, detail = _resume_plan(c, s, records.get(_step_key(s)), tree, cache)
                how = f"  resume: {d}" + (f" ({detail})" if isinstance(detail, str) else
                                          " (failed and affected tests; whole step if the change is global)")
            print(f"  $ {s['job']} › {s['name']}{ui.dim(cwd + how)}")
        for job, step, why, blk in skipped:
            print(f"  {ui.WARN if blk else '–'} {job} › {step}  {ui.dim(why)}")
        return 0
    import shutil
    import tempfile
    from . import testx
    tree = work_tree(c.top)
    sha, head_tree = _head(c.top)
    shim = None
    if not shutil.which("python") and shutil.which("python3"):
        shim = tempfile.mkdtemp(prefix="devkit-ci-")  # setup-python gives CI a `python`; macOS has python3 only
        os.symlink(shutil.which("python3"), os.path.join(shim, "python"))
        info("python → python3 for these steps (setup-python provides `python` on the runner)")
    logs = _ci_dir() / "logs" / _key(c)
    logs.mkdir(parents=True, exist_ok=True)
    for f in logs.glob("*.log"):
        f.unlink()
    results, failed, t_all, cache = [], [], time.time(), {}
    for n, s in enumerate(runnable, 1):
        key = _step_key(s)
        env = dict(os.environ, CI="true")
        env.update({k: v for k, v in s["env"].items() if "${{" not in v})
        if shim:
            env["PATH"] = shim + os.pathsep + env.get("PATH", "")
        log = logs / f"{n:02d}-{_slug(s['job'] + '-' + s['name'])[:50]}.log"
        label = f"{s['job']} › {s['name']}"
        command = s["run"].splitlines()[0][:120] if s["run"] else ""
        rec = records.get(key)
        decision, detail = _resume_plan(c, s, rec, tree, cache) if resume and tree else ("full", None)
        res = None
        if decision == "partial":
            runner = detail["runner"]
            runner["env"] = dict(runner.get("env") or {})
            p = testx.partial(c.top, runner, failed=detail["failed"], changed=detail["changed"],
                              env=dict(env, **runner["env"]), timeout=step_timeout * 60, log=log)
            if "skip" in p:
                decision, detail = "kept", p["skip"]
            elif "whole" in p:
                info(f"{label}: the whole step ({p['whole']})")
                decision = "full"
            else:
                res = p["result"]
        if decision == "kept":
            base = rec.get("base") or {"sha": rec.get("sha"), "tree": rec.get("tree")}
            results.append({"job": s["job"], "step": s["name"], "rc": 0, "secs": 0, "command": command, "how": "kept",
                            "sha": rec.get("sha"), "tree": rec.get("tree"), "base_sha": base.get("sha"),
                            "partials": rec.get("partials") or []})
            print(f"  {ui.OK} {label}  {ui.dim('kept: passed on ' + (rec.get('sha') or '?')[:7] + ' · ' + detail)}")
            continue
        if res is not None:
            rc, secs, n_tests = (1 if res["failures"] else 0), res["secs"], res["count"]
            base = rec.get("base") or {"sha": rec.get("sha"), "tree": rec.get("tree")}
            partials = ((rec.get("partials") or []) + [{"sha": sha, "tree": tree, "tests": res["count"]}])[-5:]
            print(f"  {ui.OK if rc == 0 else ui.FAIL} {label}  "
                  f"{ui.dim(f're-ran {n_tests} failed or affected test(s) · {_fmt_secs(secs)}')}")
            for fx in res["failures"][:10]:
                where = f"{fx['file']}:{fx['line']}" if fx["file"] and fx["line"] else fx["file"] or fx["id"]
                print(f"      {where}  {fx['id']}")
                if fx["message"]:
                    print(f"        {ui.dim(fx['message'][:200])}")
            new = {"job": s["job"], "step": s["name"], "command": s["run"], "rc": rc, "tree": tree, "sha": sha,
                   "clean": tree == head_tree, "secs": round(secs, 1), "ts": int(time.time()), "how": "partial",
                   "tests": res["count"], "runner": res["runner"], "base": base, "partials": partials,
                   "failed_tests": [fx["id"] for fx in res["failures"]],
                   "failed": [{"id": fx["id"], "file": fx["file"]} for fx in res["failures"]]}
            results.append({"job": s["job"], "step": s["name"], "rc": rc, "secs": round(secs, 1), "command": command,
                            "how": "partial", "tests": res["count"], "sha": sha, "tree": tree,
                            "base_sha": base.get("sha"), "base_tree": base.get("tree"), "partials": partials})
        else:
            t0 = time.time()
            try:
                with open(log, "w") as fh:
                    rc = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", s["run"]],
                                        cwd=s["cwd"], stdout=fh, stderr=subprocess.STDOUT, env=env,
                                        timeout=step_timeout * 60).returncode
            except subprocess.TimeoutExpired:
                rc = 124
            except OSError as e:
                log.write_text(str(e))
                rc = 127
            secs = time.time() - t0
            runner = testx.recognize(s["run"], s["cwd"], c.top)
            fails = testx.step_failures(runner, log.read_text(errors="replace"), c.top, t0) \
                if rc != 0 and runner else None
            new = {"job": s["job"], "step": s["name"], "command": s["run"], "rc": rc, "tree": tree, "sha": sha,
                   "clean": tree == head_tree, "secs": round(secs, 1), "ts": int(time.time()), "how": "full",
                   "runner": runner["name"] if runner else None,
                   "failed_tests": [fx["id"] for fx in fails] if fails else None, "failed": fails,
                   "base": {"sha": sha, "tree": tree}, "partials": []}
            results.append({"job": s["job"], "step": s["name"], "rc": rc, "secs": round(secs, 1), "command": command,
                            "how": "full", "sha": sha, "tree": tree})
            print(f"  {ui.OK if rc == 0 else ui.FAIL} {label}  {ui.dim(_fmt_secs(secs))}")
            if rc != 0:
                tail = [ln.rstrip()[:200] for ln in log.read_text(errors="replace").splitlines() if ln.strip()][-20:]
                for ln in tail:
                    print(f"      {ui.dim(ui.ANSI_RE.sub('', ln))}")
                print(f"      {ui.dim('exit ' + str(rc) + ' · full log: ' + core.tilde(log))}")
        if tree:
            _save_step(c, key, new)
        if rc != 0:
            failed.append(s)
            if not keep_going:
                break
    took = _fmt_secs(time.time() - t_all)
    if shim:
        shutil.rmtree(shim, ignore_errors=True)
    for job, step, why, blk in skipped:
        print(f"  {ui.WARN if blk else '–'} {job} › {step}  {ui.dim(why)}")
    again = "dev ci --run --resume again (it re-runs only what failed or changed)" if resume else \
        "fix it, then dev ci --run --resume (only what failed or changed)"
    if failed:
        return fail(f"local CI failed: {len(failed)} of {len(results)} step(s) ({took}) · no evidence recorded",
                    again)
    if blocking:
        return fail(f"{len(results)} step(s) passed ({took}), but {len(blocking)} check(s) could not run here · "
                    f"no evidence recorded",
                    'run them yourself, then: dev ci --mark-passed --note "<command>: <result>"')
    if not tree or work_tree(c.top) != tree:
        return fail("files changed while the suite ran · no evidence recorded", "dev ci --run again")
    how = "run" if all(r["how"] == "full" for r in results) else "resumed"
    record_evidence(c, how, tree, steps=results, took=took)
    if how == "resumed":
        kinds = {k: sum(1 for r in results if r["how"] == k) for k in ("full", "partial", "kept")}
        tests = sum(r.get("tests") or 0 for r in results)
        ok(f"local CI passed (resumed): {kinds['full']} full, {kinds['partial']} partial ({tests} test(s)), "
           f"{kinds['kept']} kept in {took} · evidence recorded for tree {tree[:7]} · dev push accepts it")
        return 0
    ok(f"local CI passed: {len(results)} step(s) in {took}" + (f" · {len(skipped)} skipped" if skipped else "") +
       f" · evidence recorded for tree {tree[:7]} · dev push accepts it")
    return 0


# --------------------------------------------------------------------------- gh helpers


def gh_json(args, c=None, timeout=60):
    """-> (parsed JSON, None) or (None, error). JSON on stdout is accepted even with a non-zero exit (pr checks)."""
    rc, out, err = GH(list(args), cwd=c.top if c else None, timeout=timeout)
    if out and out.strip()[:1] in ("[", "{"):
        try:
            return json.loads(out), None
        except ValueError:
            pass
    if rc != 0:
        return None, _first_line(err or out) or f"gh exited with {rc}"
    try:
        return json.loads(out or "null"), None
    except ValueError:
        return None, "unexpected output from gh"


def gh_do(args, c=None, timeout=120):
    """A gh action -> (ok, stdout, error line)."""
    rc, out, err = GH(list(args), cwd=c.top if c else None, timeout=timeout)
    return rc == 0, out, _first_line(err or out)


# --------------------------------------------------------------------------- PR body: template, pre-fill, checks

REPO_TEMPLATES = (".github/pull_request_template.md", ".github/PULL_REQUEST_TEMPLATE.md", "pull_request_template.md",
                  "PULL_REQUEST_TEMPLATE.md", "docs/pull_request_template.md", "docs/PULL_REQUEST_TEMPLATE.md",
                  ".github/PULL_REQUEST_TEMPLATE/pull_request_template.md")
BUILTIN_TEMPLATE = """## What and why

<!-- Two or three plain sentences: what this changes and why it matters to users or the team. -->

## Changes

<!-- The changes a reviewer must know about, most important first. One line each. -->
-

## How to verify

<!-- The exact commands you ran and their results, then how a reviewer can check it. -->
- Local CI: `<command>` → <result>
- Manual check: <steps, or "not needed">

## Risk and rollback

<!-- What could break, who would notice, and how to undo it. -->

## Links

<!-- Task (T-…), issue (Closes #…), plan, design doc, related PRs. -->
-
"""
SECTION_ALIASES = {
    "what": ("what and why", "what & why", "summary", "description", "what", "why", "motivation", "context",
             "qué y por qué", "que y por que", "resumen", "descripción", "descripcion", "contexto"),
    "changes": ("changes", "what changed", "changelog", "cambios"),
    "verify": ("how to verify", "how to test", "testing", "test plan", "tests", "verification", "how was this tested",
               "how has this been tested", "cómo verificar", "como verificar", "cómo verificarlo",
               "como verificarlo", "pruebas", "verificación"),
    "risk": ("risk and rollback", "risk", "risks", "rollback", "riesgo y vuelta atrás", "riesgos", "riesgo"),
    "links": ("links", "related", "references", "related issues", "issues", "enlaces", "referencias"),
}
STRINGS = {
    "en": {"local_ci": "Local CI", "passed": "passed", "steps": "steps", "attested": "suite passed (attested)",
           "task": "Task", "ticket": "Ticket", "full_on": "full on", "tests_on": "affected tests on",
           "unaffected": "unaffected since"},
    "es": {"local_ci": "CI local", "passed": "superado", "steps": "pasos", "attested": "suite superada (confirmado)",
           "task": "Tarea", "ticket": "Ticket", "full_on": "completo en", "tests_on": "pruebas afectadas en",
           "unaffected": "sin cambios que le afecten desde entonces"},
}
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_PLACEHOLDER = re.compile(r"<(?!https?://|!--)[^<>\n]+>")
_CLOSES = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+#(\d+)\b")
_WORK_ID = re.compile(r"\b([TFBS]-(?=[0-9a-z]*\d)[0-9a-z]{9,})\b")
_JIRA = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


def lang_of(a=None) -> str:
    lang = (getattr(a, "lang", None) if a is not None else None) or core.CFG.get("language") or "en-US"
    return str(lang)


def _lang_base(lang: str) -> str:
    return re.split(r"[-_]", lang.lower())[0] or "en"


def pr_template(top, lang="en-US"):
    """-> (template text, source): the repository's own template, else devkit's (per language), else built in."""
    for rel in REPO_TEMPLATES:
        f = Path(top) / rel
        if f.is_file():
            return f.read_text(errors="replace"), rel
    for name in (f"pull_request.{lang}.md", f"pull_request.{_lang_base(lang)}.md", "pull_request.md"):
        f = TEMPLATE_DIR / name
        if f.is_file():
            return f.read_text(errors="replace"), core.tilde(f)
    return BUILTIN_TEMPLATE, "built-in"


def _kind(heading: str):
    h = re.sub(r"[^\w&\s?¿áéíóúñ]", "", heading.lower()).strip(" ?")
    for kind, names in SECTION_ALIASES.items():
        if h in names:
            return kind
    return None


def sections(body: str) -> list:
    """-> [(kind or None, heading, first content line, end line)] over body.split('\\n')."""
    lines = body.split("\n")
    heads = []
    in_comment = False
    for i, ln in enumerate(lines):
        if "<!--" in ln and "-->" not in ln.split("<!--", 1)[1]:
            in_comment = True
        if in_comment:
            if "-->" in ln:
                in_comment = False
            continue
        m = _HEADING.match(ln)
        if m:
            heads.append((i, len(m.group(1)), m.group(2)))
    out = []
    for n, (i, level, title) in enumerate(heads):
        end = next((j for j, lv, _ in heads[n + 1:] if lv <= level), len(lines))
        out.append((_kind(title), title, i + 1, end))
    return out


def meaningful(text: str) -> list:
    """Lines an author actually wrote: no HTML comments, empty bullets or <placeholders>."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    keep = []
    for ln in text.split("\n"):
        s = ln.strip()
        if not s or re.fullmatch(r"[-*+](\s*\[[ xX]?\])?", s) or _PLACEHOLDER.search(s):
            continue
        keep.append(s)
    return keep


def body_problems(body: str, template: str) -> list:
    """Headings of the required sections (What and why, How to verify) that are missing or empty."""
    required = [(k, t) for k, t, _, _ in sections(template) if k in ("what", "verify")]
    have = {}
    lines = body.split("\n")
    for k, t, s, e in sections(body):
        if k in ("what", "verify") and k not in have:
            have[k] = meaningful("\n".join(lines[s:e]))
    return [t for k, t in required if not have.get(k)]


def _section_text(body: str, kind: str) -> str:
    lines = body.split("\n")
    for k, _, s, e in sections(body):
        if k == kind:
            return "\n".join(meaningful("\n".join(lines[s:e])))
    return ""


def links_for(branch: str, commits: list) -> list:
    text = "\n".join([branch or ""] + [x["subject"] + "\n" + x["body"] for x in commits])
    out = []
    for wid in dict.fromkeys(_WORK_ID.findall(text) + _WORK_ID.findall((branch or "").upper())):
        out.append(("task", wid))
    rest = (branch or "").partition("/")[2]
    for key in dict.fromkeys(_JIRA.findall(rest) + _JIRA.findall("\n".join(x["subject"] for x in commits))):
        if not _WORK_ID.fullmatch(key):
            out.append(("ticket", key))
    nums = _CLOSES.findall(text)
    m = re.match(r"^(?:gh-|issue-|#)?(\d+)(?:-|$)", rest)
    if m:
        nums.insert(0, m.group(1))
    for n in dict.fromkeys(nums):
        out.append(("closes", n))
    return out


def _verify_line(ev, T) -> str:
    if not ev:
        return ""
    sha = (ev.get("sha") or "")[:7]
    if ev.get("how") == "resumed":
        steps = ev.get("steps") or []
        parts = "; ".join(_resumed_step(x, T) for x in steps)
        return f"- {T['local_ci']}: `dev ci --run --resume` → {T['passed']} ({len(steps)} {T['steps']}): {parts} @ {sha}"
    if ev.get("how") == "run":
        names = ", ".join(s["step"] for s in (ev.get("steps") or [])[:4])
        more = f" +{len(ev['steps']) - 4}" if len(ev.get("steps") or []) > 4 else ""
        return f"- {T['local_ci']}: `dev ci --run` → {T['passed']} ({len(ev.get('steps') or [])} {T['steps']}: " \
               f"{names}{more}) @ {sha}"
    if ev.get("note") and (ev.get("how") == "host-only" or not ev["note"].startswith("dev ")):
        return f"- {T['local_ci']}: {ev['note']} @ {sha}"
    return f"- {T['local_ci']}: {T['attested']} @ {sha}"


def _resumed_step(x, T) -> str:
    """One step of resumed evidence: 'Unit: full on abc1234 + 37 affected tests on def5678'."""
    base = (x.get("base_sha") or x.get("sha") or "")[:7]
    chain = "".join(f" + {p.get('tests')} {T['tests_on']} {(p.get('sha') or '')[:7]}" for p in x.get("partials") or [])
    if x.get("how") == "full":
        return f"{x['step']}: {T['full_on']} {(x.get('sha') or '')[:7]}"
    if x.get("how") == "kept":
        return f"{x['step']}: {T['full_on']} {base}{chain} ({T['unaffected']})"
    return f"{x['step']}: {T['full_on']} {base}{chain}"


def _fill(lines, start, end, items, drop) -> list:
    """Replace the placeholder lines `drop` matches in lines[start:end] with `items`; else append after the comment."""
    if not items:
        return lines
    seg = lines[start:end]
    idx = [i for i, ln in enumerate(seg) if drop(ln)]
    if idx:
        seg = seg[:idx[0]] + items + [ln for i, ln in enumerate(seg) if i > idx[0] and i not in idx]
    else:
        last = max([i for i, ln in enumerate(seg) if ln.strip()], default=-1)
        seg = seg[:last + 1] + items + seg[last + 1:]
    return lines[:start] + seg + lines[end:]


def prefill_body(template: str, commits: list, ev, branch: str, lang="en-US") -> str:
    """The template with Changes (commits), Links (T-…, tickets, Closes #n) and How to verify (local CI) filled."""
    T = STRINGS.get(_lang_base(lang), STRINGS["en"])
    lines = template.rstrip("\n").split("\n")
    links = links_for(branch, commits)
    changes = [f"- {x['subject']}" for x in commits if not _WIP_RE.match(x["subject"])]
    link_items = [f"- {T['task']}: {v}" if k == "task" else f"- {T['ticket']}: {v}" if k == "ticket"
                  else f"- Closes #{v}" for k, v in links]
    verify = _verify_line(ev, T)
    bare = lambda ln: bool(re.fullmatch(r"\s*[-*+]\s*", ln))  # noqa: E731
    for kind, _, s, e in reversed(sections(template.rstrip("\n"))):
        if kind == "changes":
            lines = _fill(lines, s, e, changes, bare)
        elif kind == "links":
            lines = _fill(lines, s, e, link_items, bare)
        elif kind == "verify" and verify:
            lines = _fill(lines, s, e, [verify], lambda ln: bool(re.match(r"\s*[-*]\s*(local ci|ci local)\b", ln, re.I))
                          and bool(_PLACEHOLDER.search(ln)))
    return "\n".join(lines) + "\n"


def squash_message(title: str, body: str):
    """(subject, body) for a squash merge: the PR title; the What-and-why paragraph plus Closes #n lines."""
    what = _section_text(body or "", "what")
    if not what:
        paras = [p for p in re.sub(r"<!--.*?-->", "", body or "", flags=re.S).split("\n\n")
                 if p.strip() and not p.strip().startswith("#")]
        what = paras[0].strip() if paras else ""
    closes = list(dict.fromkeys(_CLOSES.findall(body or "")))
    text = "\n\n".join(x for x in (what, "\n".join(f"Closes #{n}" for n in closes)) if x)
    return title.strip(), strip_attribution(text)[0]


def branch_commits(c, base: str) -> list:
    out = core.gitout(c.top, "log", "--reverse", "--format=%h%x1f%s%x1f%b%x1e", f"{base}..HEAD")
    commits = []
    for rec in out.split("\x1e"):
        parts = rec.strip("\n").split("\x1f")
        if len(parts) >= 2 and parts[0].strip():
            commits.append({"sha": parts[0].strip(), "subject": parts[1].strip(),
                            "body": (parts[2] if len(parts) > 2 else "").strip()})
    return commits


def derive_title(branch: str, commits: list, style: str = "conventional") -> str:
    """The default PR title: the branch's first real commit, else its name; `style` is the repository's title
    convention (title_convention): a plain sentence there, made from a Conventional Commit's summary if need be."""
    real = [x for x in commits if not _WIP_RE.match(x["subject"])]
    head, _, rest = (branch or "").partition("/")
    rest = _JIRA.sub("", rest).strip("-_ ")
    words = re.sub(r"[-_]+", " ", rest).strip()
    if style == "sentence":
        if real:
            m = CC_RE.match(real[0]["subject"])
            cand = _sentence_of(m.group("desc")) if _conventional(real[0]["subject"]) else real[0]["subject"]
            if not _sentence_violation(cand):
                return cand
        verb = {"fix": "Fix", "hotfix": "Fix", "release": "Release", "integration": "Integrate",
                "revert": "Revert"}.get(head)
        return f"{verb} {words}" if verb else _sentence_of(words)
    if real and not message_violation(real[0]["subject"]):
        return real[0]["subject"]
    if head == "release":
        return f"chore(release): {words}"
    if head == "integration":
        return f"chore: integrate {words}"
    kind = {"hotfix": "fix"}.get(head, head)
    return f"{kind}: {words}".strip()


# --------------------------------------------------------------------------- dev pr

PR_FIELDS = ("number,title,state,isDraft,headRefName,baseRefName,statusCheckRollup,reviewDecision,mergeable,"
             "mergeStateStatus,additions,deletions,changedFiles,url,author,updatedAt")


def cmd_pr(a) -> int:
    if a.pr_cmd == "ls" and getattr(a, "all_projects", False):
        return _pr_ls_all(a)
    c = ctx()
    if c is None:
        return _need_repo(c)
    r = need_github(c, "pr " + a.pr_cmd)
    if r is not None:
        return r
    if a.pr_cmd not in ("create", "ls") and not _sel(c, a):
        return refuse("detached HEAD: give a PR number")
    return {"create": _pr_create, "view": _pr_view, "ls": _pr_ls, "checks": _pr_checks, "merge": _pr_merge,
            "ready": _pr_ready, "comment": _pr_comment, "review": _pr_review, "diff": _pr_diff}[a.pr_cmd](c, a)


def _sel(c, a):
    n = getattr(a, "number", None)
    if n:
        return str(n).lstrip("#")
    return c.branch or None


def _checks_word(rollup) -> str:
    from .registry import _checks
    return _checks(rollup or [])


def _pr_line(p) -> str:
    bits = [p.get("state", "").lower() + (" draft" if p.get("isDraft") else ""),
            f"{p.get('headRefName')} → {p.get('baseRefName')}", "checks " + _checks_word(p.get("statusCheckRollup"))]
    if p.get("reviewDecision"):
        bits.append("review " + p["reviewDecision"].lower().replace("_", " "))
    if p.get("mergeable") == "CONFLICTING":
        bits.append("conflicts")
    return f"#{p.get('number')} {p.get('title')} · " + " · ".join(bits)


def _pr_view(c, a) -> int:
    sel = _sel(c, a)
    if not sel:
        return refuse("detached HEAD: give a PR number")
    p, err = gh_json(["pr", "view", sel, "-R", c.slug, "--json", PR_FIELDS + (",body" if a.body else "")], c)
    if p is None:
        return fail(f"no PR for {sel}: {err}")
    if a.json:
        return emit(p)
    print(_pr_line(p))
    print(f"  +{p.get('additions', 0)} −{p.get('deletions', 0)} in {p.get('changedFiles', 0)} file(s) · by "
          f"{(p.get('author') or {}).get('login', '?')} · updated {_age(p.get('updatedAt'))} · {p.get('url')}")
    if a.body:
        text = re.sub(r"<!--.*?-->", "", p.get("body") or "", flags=re.S)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        for ln in text.splitlines()[:60]:
            print("  " + ln)
    return 0


def _pr_create(c, a) -> int:
    b = c.branch
    if not b:
        return refuse("detached HEAD: nothing to open a PR for")
    if b == c.default or b in guard.PERMANENT_BRANCHES:
        return refuse(f"`{b}` is the default branch", "dev branch new <type>/<topic>")
    v = guard.branch_violation(b)
    if v:
        return refuse(v.split(" Rename it:")[0], "dev branch rename <type>/<topic>, then dev push")
    base = a.base or c.default
    draft = bool(a.draft) or (draft_by_default() and not getattr(a, "ready", False))
    remote = _ref_sha(c.top, f"refs/remotes/origin/{b}")
    head = _ref_sha(c.top, "HEAD")
    if not remote:
        return refuse(f"`{b}` is not on origin yet", "dev push (runs the local-CI gate), then dev pr create"
                      + (" (or dev backup, when that push starts no CI run)" if draft else ""))
    if remote != head:
        return refuse(f"HEAD ({head[:7]}) is not what origin/{b} has ({remote[:7]})",
                      "dev push first" + (" (or dev backup)" if draft else ""))
    lang = lang_of(a)
    existing, _ = gh_json(["pr", "list", "-R", c.slug, "--head", b, "--state", "open", "--json", "number,url"], c)
    if existing and not a.dry_run:
        ok(f"PR #{existing[0]['number']} is already open for {b} · {existing[0]['url']}")
        return 0
    ev = evidence(c, host_only=True) if a.dry_run else _evidence_or_attest(c, a.ci_passed, "pr create")
    if not ev and not a.dry_run:
        why = draft_ci(c, b, head) if draft else ""
        if not draft or why:
            return no_evidence(c, "pr create", draft_ci=why)
    commits = branch_commits(c, f"origin/{base}" if _ref_sha(c.top, f"refs/remotes/origin/{base}") else base)
    conv = title_convention(c)
    title = (a.title or derive_title(b, commits, conv["style"])).strip()
    v = pr_title_violation(c, title, conv)
    if v:
        return refuse(v, f"dev pr create --title {_title_hint(conv)}")
    template, source = pr_template(c.top, lang)
    if a.body_file:
        try:
            body = sys.stdin.read() if a.body_file == "-" else Path(a.body_file).expanduser().read_text()
        except OSError as e:
            return fail(f"cannot read {a.body_file}: {e}")
    else:
        body = prefill_body(template, commits, ev, b, lang)
        if a.edit:
            body = _edit(body)
            if body is None:
                return refuse("--edit needs an interactive terminal",
                              "dev pr create --dry-run > body.md, fill it in, then dev pr create --body-file body.md")
    body, removed = strip_attribution(body)
    missing = body_problems(body, template)
    if a.dry_run:  # the body alone on stdout (`> body.md` works); the summary on stderr
        print(f"title: {title}\ntemplate: {source}\nevidence: {describe_evidence(ev)}" +
              (f"\nstill empty: {', '.join(missing)}" if missing else "") + "\n---", file=sys.stderr)
        print(body)
        return 0
    if missing and not draft:
        return refuse(f"PR body: {' and '.join(f'“{m}”' for m in missing)} {'is' if len(missing) == 1 else 'are'} empty",
                      "dev pr create --dry-run prints the pre-filled body: fill those sections, then "
                      "dev pr create --body-file <file> (or --draft for an early draft)")
    args = ["pr", "create", "-R", c.slug, "--head", b, "--base", base, "--title", title, "--body", body]
    if draft:
        args.append("--draft")
    done, out, err = gh_do(args, c)
    if not done:
        return fail(f"gh pr create failed: {err}")
    url = (out.strip().splitlines() or [""])[-1]
    n = url.rstrip("/").rsplit("/", 1)[-1]
    ok(f"PR #{n} opened{' as draft' if draft else ''} · {title} · {url}" +
       (f" · removed {removed} AI-attribution line(s)" if removed else "") +
       (" · no CI runs on the draft: after `dev ci --run`, `dev pr ready` starts the one run" if draft and not ev
        else " · watch: dev pr checks --watch"))
    _refresh_prs(c)
    return 0


def _refresh_prs(c) -> None:
    """Read the repository's PRs from GitHub again into the cache the dashboard and `dev status` show (kept 15
    minutes otherwise), right after dev changed one. Never fails the command that changed it."""
    if c.provider != "github":
        return
    try:
        from . import registry
        registry.prs(c.slug, refresh=True)
    except Exception:
        pass


def _edit(text: str):
    import tempfile
    if not sys.stdin.isatty():
        return None
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or core.CFG.get("editor") or "vi"
    argv = shlex.split(editor)
    if os.path.basename(argv[0]) in ("code", "cursor", "subl", "zed") and not any(x in argv for x in ("-w", "--wait")):
        argv.append("--wait")
    with tempfile.NamedTemporaryFile("w+", suffix=".md", prefix="dev-pr-", delete=False) as f:
        f.write(text)
        path = f.name
    try:
        EXEC(argv + [path])
        return Path(path).read_text()
    finally:
        os.unlink(path)


def _pr_ls(c, a) -> int:
    me = _me()
    fields = "number,title,headRefName,author,isDraft,statusCheckRollup,reviewDecision,updatedAt,url"
    prs, err = gh_json(["pr", "list", "-R", c.slug, "--state", "open", "--limit", "100", "--json", fields], c)
    if prs is None:
        return fail(f"gh pr list failed: {err}")
    asked, _ = gh_json(["pr", "list", "-R", c.slug, "--state", "open", "--search", "review-requested:@me",
                        "--json", "number"], c)
    asked = {p["number"] for p in (asked or [])}
    mine = [p for p in prs if (p.get("author") or {}).get("login") == me]
    review = [p for p in prs if p["number"] in asked and p not in mine]
    others = [p for p in prs if p not in mine and p not in review]
    if a.json:
        return emit({"mine": mine, "review": review, "others": others if a.all else len(others)})
    rows = []
    for group, items in (("mine", mine), ("review", review)) + ((("other", others),) if a.all else ()):
        for p in items:
            rows.append([group, f"#{p['number']}", p["title"], p["headRefName"],
                         _checks_word(p.get("statusCheckRollup")) + (" draft" if p.get("isDraft") else ""),
                         (p.get("reviewDecision") or "-").lower().replace("_", " "),
                         (p.get("author") or {}).get("login", ""), _age(p.get("updatedAt"))])
    if rows:
        print(ui.table(rows, ["FOR", "#", "TITLE", "BRANCH", "CHECKS", "REVIEW", "BY", "AGE"],
                       max_widths={2: 50, 3: 34}, indent=0))
    else:
        print(f"no open PRs of yours or waiting for your review in {c.slug}")
    if others and not a.all:
        info(f"{len(others)} other open PR(s) · dev pr ls --all")
    return 0


def _pr_ls_all(a) -> int:
    reg = core.read_json(core.HANDOFF / "registry.json", {}) or {}
    me = _me()
    rows, data = [], {"mine": [], "review": []}
    for p in reg.get("projects", []):
        for pr in p.get("prs_open") or []:
            if pr.get("author") == me:
                data["mine"].append(dict(pr, project=p["id"]))
                rows.append(["mine", p["id"], f"#{pr['number']}", pr.get("title", ""), pr.get("checks", "?"),
                             (pr.get("review") or "-").lower().replace("_", " "), _age(pr.get("updated"))])
    if not a.offline:
        found, err = gh_json(["search", "prs", "--review-requested=@me", "--state=open", "--limit", "50", "--json",
                              "repository,number,title,url,updatedAt,isDraft"])
        for pr in found or []:
            repo = (pr.get("repository") or {}).get("nameWithOwner", "?")
            data["review"].append(dict(pr, project=repo.lower()))
            rows.append(["review", repo.lower(), f"#{pr['number']}", pr.get("title", ""), "-", "requested",
                         _age(pr.get("updatedAt"))])
        if found is None:
            ui.warn(f"review queue unavailable: {err}")
    if a.json:
        return emit(data)
    if not rows:
        print("no open PRs of yours or waiting for your review")
    else:
        print(ui.table(rows, ["FOR", "PROJECT", "#", "TITLE", "CHECKS", "REVIEW", "AGE"],
                       max_widths={1: 36, 3: 50}, indent=0))
    try:
        info(f"your PRs from the registry cache ({_age((core.HANDOFF / 'registry.json').stat().st_mtime)} old; "
             f"dev sync refreshes it)")
    except OSError:
        info("no registry cache yet: dev sync builds it")
    return 0


_LINK_RE = re.compile(r"/actions/runs/(\d+)(?:/job/(\d+))?")
_TS_RE = re.compile(r"^\ufeff?\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ?")
_NOISE_STEP = re.compile(r"^(Post |Set up job|Complete job|Stop containers|Initialize containers)")
_NOISE_LINE = re.compile(r"^(Cleaning up orphan processes|##\[warning\]Node\.js \d+ (is|actions are) deprecated|"
                         r"Post job cleanup|Temporarily overriding HOME)")
_ECHO_RE = re.compile(r"^(\x1b|\^\[)\[36;1m")
_CARET_ANSI = re.compile(r"\^\[\[[0-9;]*m")
_ERR_RE = re.compile(r"(?i)(error|fail|assert|exception|traceback|panic|fatal|expected|✗|×|not ok|^E\s{2,})")


def trim_log(text: str, n: int = 25) -> list:
    """`gh run view --log-failed` output -> the relevant lines: the failing step (from its last ##[group] to the first
    ##[error]), error lines first, then its tail; without post-job noise, timestamps or colour."""
    rows, groups, err_at = [], [], None
    for ln in (text or "").splitlines():
        parts = ln.split("\t", 2)
        if len(parts) == 3 and _NOISE_STEP.match(parts[1].strip()):
            continue
        msg = _TS_RE.sub("", parts[2] if len(parts) == 3 else ln)
        if _ECHO_RE.match(msg):
            continue  # GitHub echoes the step's script in cyan before running it
        msg = _CARET_ANSI.sub("", ui.ANSI_RE.sub("", msg)).rstrip()
        if msg.startswith("##[group]"):
            groups.append(len(rows))
            continue
        if not msg.strip() or msg.startswith(("##[endgroup]", "[command]")) or _NOISE_LINE.match(msg):
            continue
        if msg.startswith("##[error]") and err_at is None:
            err_at = len(rows)
        rows.append(msg.replace("##[error]", "error: ")[:220])
    if err_at is not None:
        start = max([g for g in groups if g <= err_at], default=0)
        rows = rows[start:err_at + 1]
    if len(rows) <= n:
        return rows
    k = max(1, n // 2)
    tail = rows[-k:]
    errs = [r for r in rows[:-k] if _ERR_RE.search(r)][-(n - k):]
    return errs + ["…"] + tail


def failing_logs(c, checks, lines=25, max_jobs=3) -> list:
    out = []
    for ch in [x for x in checks if x.get("bucket") == "fail"][:max_jobs]:
        m = _LINK_RE.search(ch.get("link") or "")
        if not m:
            continue
        args = ["run", "view", m.group(1), "-R", c.slug, "--log-failed"] + (["--job", m.group(2)] if m.group(2) else [])
        rc, log, err = GH(args, cwd=c.top, timeout=120)
        out.append((ch.get("name"), trim_log(log, lines) if rc == 0 else [f"(log unavailable: {_first_line(err)})"]))
    return out


def _pr_checks(c, a) -> int:
    sel = _sel(c, a)
    if not sel:
        return refuse("detached HEAD: give a PR number")
    fields = "name,state,bucket,link,workflow,startedAt,completedAt"
    if a.watch:
        if sys.stderr.isatty():
            print(ui.dim(f"  watching the checks of {sel} (gh pr checks --watch); only the result is printed"),
                  file=sys.stderr)
        for _ in range(6):  # a fresh push or `dev pr ready`: wait up to ~2 minutes for the real run to report
            rc, out, err = GH(["pr", "checks", sel, "-R", c.slug, "--json", "name,bucket"], cwd=c.top, timeout=60)
            if "no checks reported" not in (out + err).lower() and not _only_skipped(out):
                break
            SLEEP(20)
        args = ["pr", "checks", sel, "-R", c.slug, "--watch", "--interval", str(a.interval)]
        if a.fail_fast:
            args.append("--fail-fast")
        rc, out, err = GH(args, cwd=c.top, timeout=a.timeout * 60)
        if rc == 127 and "timed out" in (err or "").lower():
            print(f"{ui.WARN} checks of {sel} still running after {a.timeout} min · dev pr checks {sel} --watch")
            return 8
    checks, err = gh_json(["pr", "checks", sel, "-R", c.slug, "--json", fields], c)
    if checks is None:
        if "no checks reported" in (err or "").lower():
            print(f"{ui.INFO} no checks reported for {sel} yet")
            return 8
        return fail(f"gh pr checks failed: {err}")
    if a.json:
        return emit(checks)
    count = {}
    for ch in checks:
        count[ch.get("bucket") or "pending"] = count.get(ch.get("bucket") or "pending", 0) + 1
    nfail, npend = count.get("fail", 0) + count.get("cancel", 0), count.get("pending", 0)
    if checks and count.get("skipping", 0) == len(checks):  # a draft's run: no real run yet, never green (B3)
        print(f"{ui.INFO} no checks yet for {sel}: every check was skipped (a draft's run, or the real run has not "
              f"reported); the real run starts when the PR is ready (dev pr ready), then dev pr checks {sel} --watch")
        return 8
    mark = ui.FAIL if nfail else ui.INFO if npend else ui.OK
    print(f"{mark} checks {('#' + sel) if sel.isdigit() else sel}: {count.get('pass', 0)} pass · {nfail} fail · "
          f"{npend} pending" + (f" · {count['skipping']} skipped" if count.get("skipping") else ""))
    for ch in checks:
        if ch.get("bucket") in ("fail", "cancel"):
            print(f"  {ui.FAIL} {ch.get('workflow') + ' › ' if ch.get('workflow') else ''}{ch.get('name')} · "
                  f"{ch.get('link') or ''}")
    if nfail:
        for name, rows in failing_logs(c, checks, lines=a.lines):
            print(ui.dim(f"  ── {name}"))
            for ln in rows:
                print("    " + ln)
        info("reproduce locally (dev ci --run), fix, then push once")
    return 1 if nfail else 8 if npend else 0


def _only_skipped(text) -> bool:
    """True when `gh pr checks --json name,bucket` lists checks and every one was skipped."""
    try:
        rows = json.loads(text or "[]")
    except ValueError:
        return False
    return isinstance(rows, list) and bool(rows) and all(
        isinstance(r, dict) and r.get("bucket") == "skipping" for r in rows)


def _rollup_skipped(rollup) -> bool:
    """True when a statusCheckRollup holds checks and every one was skipped."""
    return bool(rollup) and all(str(c.get("conclusion") or c.get("state") or c.get("status") or "").upper() ==
                                "SKIPPED" for c in rollup if isinstance(c, dict))


def _pr_workflows(top) -> bool:
    """True when a workflow runs on pull requests, so a PR without checks means they have not started."""
    from . import ci
    return any(set(wf.get("on") or []) & {"pull_request", "pull_request_target", "merge_group"}
               for wf in ci.workflows(Path(top)))


def merge_gate(c, p, auto=False) -> list:
    """Reasons `p` must not be merged now (empty: go)."""
    n = p.get("number")
    why = []
    if p.get("state") != "OPEN":
        return [f"PR #{n} is {str(p.get('state')).lower()}"]
    if p.get("isDraft"):
        why.append(f"PR #{n} is a draft (dev pr ready {n})")
    checks = _checks_word(p.get("statusCheckRollup"))
    if checks == "failing":
        why.append(f"checks are failing (dev pr checks {n})")
    elif checks == "pending" and not auto:
        why.append(f"checks are still running (dev pr checks {n} --watch, or dev pr merge {n} --auto)")
    elif checks == "none" and _rollup_skipped(p.get("statusCheckRollup")):
        why.append(f"every check was skipped, so no real run has reported yet (dev pr checks {n} --watch)")
    elif checks == "none" and _pr_workflows(c.top):
        why.append("no checks reported yet")
    review = p.get("reviewDecision") or ""
    if review == "CHANGES_REQUESTED":
        why.append("changes were requested")
    elif review == "REVIEW_REQUIRED":
        why.append("an approving review is required")
    state = p.get("mergeStateStatus") or ""
    if p.get("mergeable") == "CONFLICTING" or state == "DIRTY":
        why.append("it conflicts with the base (dev rebase, then dev push --force-with-lease)")
    elif state == "BEHIND":
        why.append("it is behind the base (dev rebase, re-run the local suite, dev push --force-with-lease)")
    elif state == "BLOCKED" and not why and not auto:
        why.append("branch protection blocks it (required reviews or checks)")
    return why


def merge_method(repo: dict, requested=None):
    """-> (method, None) or (None, reason): the requested method if allowed, the only one, the viewer default."""
    allowed = [m for m, k in (("squash", "squashMergeAllowed"), ("merge", "mergeCommitAllowed"),
                              ("rebase", "rebaseMergeAllowed")) if repo.get(k)]
    if not allowed:
        return None, "the repository allows no merge method"
    if requested:
        return (requested, None) if requested in allowed else \
            (None, f"the repository does not allow {requested} merges (allowed: {', '.join(allowed)})")
    if len(allowed) == 1:
        return allowed[0], None
    d = (repo.get("viewerDefaultMergeMethod") or "").lower()
    return (d if d in allowed else allowed[0]), None


def _pr_merge(c, a) -> int:
    sel = _sel(c, a)
    if not sel:
        return refuse("detached HEAD: give a PR number")
    p, err = gh_json(["pr", "view", sel, "-R", c.slug, "--json", PR_FIELDS + ",body,headRefOid"], c)
    if p is None:
        return fail(f"no PR for {sel}: {err}")
    n = p["number"]
    why = merge_gate(c, p, auto=a.auto)
    if why:
        return refuse(f"not merging #{n}: " + "; ".join(why))
    repo, err = gh_json(["repo", "view", c.slug, "--json", "squashMergeAllowed,mergeCommitAllowed,rebaseMergeAllowed,"
                         "deleteBranchOnMerge,viewerDefaultMergeMethod"], c)
    if repo is None:
        return fail(f"cannot read the repository's merge settings: {err}")
    method, err = merge_method(repo, a.method)
    if not method:
        return refuse(err)
    args = ["pr", "merge", str(n), "-R", c.slug, f"--{method}", "--match-head-commit", p.get("headRefOid", "")]
    if method == "squash":
        conv = title_convention(c)
        v = pr_title_violation(c, p.get("title", ""), conv)
        if v:
            return refuse(v + " It becomes the squash commit on " + p.get("baseRefName", c.default) + ".",
                          f"dev gh pr edit {n} --title {_title_hint(conv)}")
        subject, body = squash_message(p["title"], p.get("body") or "")
        args += ["--subject", subject, "--body", body]
    if a.auto:
        args.append("--auto")
    if not a.keep_branch and not a.auto:
        args.append("--delete-branch")
    if a.dry_run:
        ok(f"dry run: would merge #{n} ({method}) into {p.get('baseRefName')}" +
           (" · auto when checks pass" if a.auto else "") + ("" if a.keep_branch else " · delete the branch"))
        if method == "squash":
            print(f"  subject: {subject}\n  body: " + (body.replace("\n", "\n        ") or "-"))
        return 0
    done, out, err = gh_do(args, c, timeout=180)
    if not done:
        return fail(f"gh pr merge failed: {err}")
    if a.auto:
        ok(f"auto-merge ({method}) enabled for #{n}: GitHub merges it when the checks pass")
        return 0
    tail, kept = [f"merged #{n} ({method}) into {p.get('baseRefName')}"], ""
    if not a.keep_branch:
        tail.append("remote branch deleted")
        note, kept = _after_merge(c, p.get("headRefName"), p)
        tail.append(note)
    ok(" · ".join(x for x in tail if x))
    if kept:
        ui.warn(kept)
    _refresh_prs(c)
    _close_merged_items(c, p.get("headRefName"), n)
    return 0


def _close_merged_items(c, head: str, n) -> list:
    """The work landed: the open items linked to the merged branch (`branch:<head>`) are done, with the PR and its
    merge commit in their changelog. A problem with the handoff repository only warns: the merge itself is done."""
    if not head or not c.pid:
        return []
    from . import work
    try:
        work.pull()
        if not work.linked_open(c.pid, [head]):
            return []
        pm, _ = gh_json(["pr", "view", str(n), "-R", c.slug, "--json", "mergeCommit"], c)
        sha = ((pm or {}).get("mergeCommit") or {}).get("oid") or ""
        closed = work.close_merged(c.pid, head, f"merged in PR #{n}" + (f" ({sha[:7]})" if sha else ""),
                                   a=work.actor(user=_me() or None))
    except Exception as e:  # the merge is done; the items wait for `dev task reconcile`
        ui.warn(f"work items of {head} not closed: {e} (dev task reconcile --apply closes them later)")
        return []
    if closed:
        ok(f"closed {len(closed)} work item(s) linked to {head}: " +
           ", ".join(f"{i['meta']['id']} “{i['meta']['title']}”" for i in closed))
    return closed


_MERGE_PR = re.compile(r"^Merge pull request #(\d+) from [^/\s]+/(\S+)")
_MERGE_BRANCH = re.compile(r"^Merge (?:branch '([^']+)'|remote-tracking branch '[^/']+/([^']+)')")


def merged_branch(subject: str):
    """The branch a merge commit's subject names and its PR number: GitHub's `Merge pull request #7 from
    owner/feat/x`, git's `Merge branch 'feat/x' [into …]` and `Merge remote-tracking branch 'origin/feat/x'`.
    -> (branch, PR number or None), or (None, None) for any other subject (a squash commit names no branch)."""
    s = (subject or "").strip()
    m = _MERGE_PR.match(s)
    if m:
        return m.group(2), int(m.group(1))
    m = _MERGE_BRANCH.match(s)
    return (m.group(1) or m.group(2), None) if m else (None, None)


FUTURE_SLACK = 600  # a commit dated more than ten minutes past this Mac's clock: its date is its author's word only


def merged_branches(top, rev: str) -> dict:
    """branch -> {"sha", "pr", "at"} for the merge commits made on `rev`'s own line (a range such as
    `v26.11.5..HEAD`, or a ref), the newest merge of each branch; `at`: its commit time, in seconds since the epoch.
    Only first-parent merges count: a merge inside a merged branch names whatever that branch's author wrote. A merge
    dated in the future is left out, so its date cannot outrun the link of a later item to the same branch name."""
    out = {}
    limit = time.time() + FUTURE_SLACK
    for ln in core.gitout(top, "log", "--merges", "--first-parent", "--format=%H%x09%ct%x09%s", rev,
                          timeout=60).splitlines():
        sha, _, rest = ln.partition("\t")
        at, _, subject = rest.partition("\t")
        branch, pr = merged_branch(subject)
        at = int(at) if at.isdigit() else 0
        if branch and branch not in out and at <= limit:
            out[branch] = {"sha": sha, "pr": pr, "at": at}
    return out


def github_merged_prs(c):
    """The merged pull requests of the repository on GitHub that came from the repository itself (a fork's branch can
    carry any name), newest 200: [{number, headRefName, headRefOid, mergeCommit, mergedAt, baseRefName}]. None off
    GitHub; [] when GitHub cannot be read, so nothing counts as merged there without its pull request."""
    if c is None or c.provider != "github":
        return None
    prs, _ = gh_json(["pr", "list", "-R", c.slug, "--state", "merged", "--limit", "200", "--json",
                      "number,mergedAt,mergeCommit,headRefName,headRefOid,isCrossRepository,baseRefName"], c)
    return [p for p in prs if isinstance(p, dict) and p.get("isCrossRepository") is False and
            (p.get("mergeCommit") or {}).get("oid")] if isinstance(prs, list) else []


def _with_pr(merged: dict, prs: list) -> dict:
    """On GitHub a merge commit counts only with the merged pull request that made it: the same merge commit and
    branch, from the repository itself; `at` becomes the PR's mergedAt, which GitHub sets, not the commit date."""
    from . import work
    by_sha = {(p.get("mergeCommit") or {}).get("oid"): p for p in prs}
    out = {}
    for b, m in merged.items():
        p = by_sha.get(m["sha"])
        if p and p.get("headRefName") == b:
            out[b] = {"sha": m["sha"], "pr": p.get("number"), "at": work.parse_iso(p.get("mergedAt") or "")}
    return out


def merged_without_merge_commit(top, ref: str, branches, since: str = None, prs=None) -> dict:
    """Which of `branches` reached `ref` without a merge commit that names them. On GitHub (`prs`: its merged pull
    requests, `github_merged_prs`) only by a merged PR of the branch whose merge commit is in `ref`: a squash or rebase
    merge. Elsewhere (`prs` None) also by a fast-forward: the branch's tip is in `ref` and not dated in the future.
    `since`: a tag; a merge already in it does not count. branch -> {"sha", "pr", "at"}, `at` being when it was merged
    (the PR) or committed (the tip), which `work.linked_open` compares with each item's link to the branch."""
    from . import work
    branches, out = list(branches or ()), {}
    by_head = {}
    for p in prs or []:
        sha = (p.get("mergeCommit") or {}).get("oid") or ""
        if sha and p.get("headRefName") in branches:
            by_head.setdefault(p["headRefName"], []).append((sha, p.get("number"),
                                                             work.parse_iso(p.get("mergedAt") or "")))
    limit = time.time() + FUTURE_SLACK
    for b in branches:
        found = sorted(by_head.get(b, []), key=lambda x: -x[2])  # the newest merge of the name first
        for name in (f"refs/heads/{b}", f"refs/remotes/origin/{b}") if prs is None else ():
            tip = _ref_sha(top, name)
            at = int(core.gitout(top, "log", "-1", "--format=%ct", tip) or 0) if tip else 0
            if tip and at <= limit:
                found.append((tip, None, at))
        for sha, pr, at in found:
            if _is_ancestor(top, sha, ref) and not (since and _is_ancestor(top, sha, since)):
                out[b] = {"sha": sha, "pr": pr, "at": at}
                break
    return out


def merges_into(top, ref: str, branches=(), since: str = None, c=None) -> dict:
    """The branches merged into `ref` (since the tag `since`): its first-parent merge commits, checked against their
    pull requests on GitHub, and `branches` merged without a merge commit. branch -> {"sha", "pr", "at"}."""
    c = c or ctx(top)
    prs = github_merged_prs(c)
    merged = merged_branches(top, f"{since}..{ref}" if since else ref)
    if prs is not None:
        merged = _with_pr(merged, prs)
    merged.update(merged_without_merge_commit(top, ref, [b for b in branches or () if b not in merged], since, prs))
    return merged


def merged_into_default(top, default: str, branches=()) -> dict:
    """The branches merged into the default branch as origin has it (the local branch when there is no origin):
    a merge that only exists on this Mac has not shipped. `branches`: those to look for also among merges without a
    merge commit (`merged_without_merge_commit`). branch -> {"sha", "pr", "at"}."""
    ref = f"origin/{default}" if _ref_sha(top, f"refs/remotes/origin/{default}") else default
    return merges_into(top, ref, branches)


def _merge_ref(m: dict) -> str:
    return f"PR #{m['pr']}" if m.get("pr") else m["sha"][:7]


def _merged_pr(c, p: dict):
    """The pull request just merged, as the merge test reads it ([{number, state, headRefName, headRefOid,
    isCrossRepository, mergeCommit}]), from GitHub after the merge; None when GitHub cannot say."""
    if not p or not p.get("number") or c.provider != "github":
        return None
    pm, _ = gh_json(["pr", "view", str(p["number"]), "-R", c.slug, "--json",
                     "number,headRefName,headRefOid,isCrossRepository,mergeCommit"], c)
    return [dict(pm, state="MERGED")] if isinstance(pm, dict) else None


def _after_merge(c, head: str, p: dict = None) -> tuple:
    """Local clean-up after a merge, when the merge test finds the branch merged (it says how): the branch's clean
    worktree goes with the local branch (gc.remove_worktree checks it again), or this checkout leaves the branch for
    an updated default branch and deletes it. A branch with work the default branch lacks stays, with the reason.
    -> (a note for the merge's success line, a warning of its own or ''): a worktree gc.remove_worktree kept holds
    work, so it is a warning, never part of the success line."""
    note = _after_merge_note(c, head, p)
    return ("", note[1]) if isinstance(note, tuple) else (note, "")


def _after_merge_note(c, head: str, p: dict = None):
    """_after_merge's clean-up: its note, or (False, the reason) when gc.remove_worktree kept the worktree."""
    _fetch(c)
    if not head or not _ref_sha(c.top, f"refs/heads/{head}"):
        return ""
    from . import branches
    base = _base_ref(c)
    prs = _merged_pr(c, p)
    m = branches.merge_test(c.top, head, base, prs=prs)
    if not m["merged"]:
        return f"local branch kept: {branches.merge_words(m, base)} (dev branch prune lists it)"
    wts = _worktree_branches(c.top)
    here = core.real(c.top)
    if head in wts and core.real(wts[head]) != here:
        if core.real(wts[head]) == core.real(c.main):
            return f"the main checkout has {head}: switch it to {c.default}, then dev branch prune --apply"
        from . import gc
        removed, msg = gc.remove_worktree(c.main, wts[head], head, merged={"base": base, "sha": m["sha"], "prs": prs})
        return msg if removed else (False, msg)
    if c.branch == head:
        if core.real(c.top) != core.real(c.main):
            return f"this worktree's branch is merged: dev wt rm {head} when you are done here"
        if _dirty(c.top, untracked=False):
            return "local branch kept: uncommitted changes here"
        if c.default in wts and core.real(wts[c.default]) != here:
            return f"local branch kept ({c.default} is checked out in another worktree)"
        rc, _, _ = core.git(c.top, "switch", "-q", c.default)
        if rc != 0:
            return "local branch kept"
        core.git(c.top, "merge", "--ff-only", "-q", f"origin/{c.default}")
    core.git(c.top, "branch", "-D", head)
    return (f"local branch deleted ({branches.merge_words(m, base)}) · {c.default} @ "
            f"{core.gitout(c.top, 'rev-parse', '--short', c.default)}")


def _pr_ready(c, a) -> int:
    sel = _sel(c, a)
    p, err = gh_json(["pr", "view", sel or "", "-R", c.slug, "--json",
                      "number,title,body,isDraft,url,headRefName,headRefOid"], c)
    if p is None:
        return fail(f"no PR for {sel}: {err}")
    conv = title_convention(c)
    v = pr_title_violation(c, p.get("title", ""), conv)
    if v:
        return refuse(v, f'dev gh pr edit {p["number"]} --title {_title_hint(conv)}')
    template, _ = pr_template(c.top, lang_of(a))
    missing = body_problems(p.get("body") or "", template)
    if missing:
        return refuse(f"PR #{p['number']}: {' and '.join(f'“{m}”' for m in missing)} still empty",
                      f"fill them: dev gh pr edit {p['number']} --body-file <file>")
    if not p.get("isDraft"):
        ok(f"#{p['number']} is already ready for review · {p['url']}")
        return 0
    if p.get("headRefName") and p.get("headRefName") == c.branch:  # ready starts the one CI run (spec P-fy3bwa0pshk R6)
        head = _ref_sha(c.top, "HEAD")
        if p.get("headRefOid") and head != p["headRefOid"]:
            return refuse(f"PR #{p['number']} is at {p['headRefOid'][:7]} and your HEAD at {head[:7]}: the one CI run "
                          "would check another tree", "dev push (or dev backup) first, then dev pr ready")
        if not _evidence_or_attest(c, getattr(a, "ci_passed", False), "pr ready"):
            return no_evidence(c, "pr ready")
    done, _, err = gh_do(["pr", "ready", str(p["number"]), "-R", c.slug], c)
    if not done:
        return fail(f"gh pr ready failed: {err}")
    ok(f"#{p['number']} is ready for review · {p['url']} · its CI runs now: dev pr checks --watch")
    _refresh_prs(c)
    return 0


def draft_by_default() -> bool:
    """`delivery.draft_pr` (default false): `dev pr create` opens a draft unless --ready (spec P-fy3bwa0pshk R6)."""
    return (core.CFG.get("delivery") or {}).get("draft_pr", False) is True


def _text_arg(a):
    if getattr(a, "body_file", None):
        try:
            return sys.stdin.read() if a.body_file == "-" else Path(a.body_file).expanduser().read_text()
        except OSError:
            return None
    return a.message or ""


def _pr_comment(c, a) -> int:
    sel = _sel(c, a)
    text, removed = strip_attribution(_text_arg(a) or "")
    if not text.strip():
        return refuse("empty comment", 'dev pr comment [n] -m "…" (or --body-file)')
    done, out, err = gh_do(["pr", "comment", sel, "-R", c.slug, "--body", text], c)
    if not done:
        return fail(f"gh pr comment failed: {err}")
    ok(f"commented on {sel} · {out.strip().splitlines()[-1] if out.strip() else ''}".rstrip(" ·"))
    return 0


def _pr_review(c, a) -> int:
    sel = _sel(c, a)
    text, _ = strip_attribution(_text_arg(a) or "")
    if a.verdict in ("request-changes", "comment") and not text.strip():
        return refuse(f"--{a.verdict} needs a message", '-m "what must change and why"')
    args = ["pr", "review", sel, "-R", c.slug, f"--{a.verdict}"] + (["--body", text] if text.strip() else [])
    done, _, err = gh_do(args, c)
    if not done:
        return fail(f"gh pr review failed: {err}")
    ok(f"review on {sel}: {a.verdict.replace('-', ' ')}")
    return 0


def _pr_diff(c, a) -> int:
    sel = _sel(c, a)
    if a.patch:
        rc, out, err = GH(["pr", "diff", sel, "-R", c.slug, "--color", "never"], cwd=c.top, timeout=120)
        if rc != 0:
            return fail(f"gh pr diff failed: {_first_line(err)}")
        print(out)
        return 0
    p, err = gh_json(["pr", "view", sel, "-R", c.slug, "--json", "number,files,additions,deletions,changedFiles"], c)
    if p is None:
        return fail(f"no PR for {sel}: {err}")
    files = sorted(p.get("files") or [], key=lambda f: -((f.get("additions") or 0) + (f.get("deletions") or 0)))
    if a.json:
        return emit(files)
    if a.name_only:
        print("\n".join(f["path"] for f in files))
        return 0
    for f in files[:40]:
        print(f"  +{f.get('additions', 0):<5} −{f.get('deletions', 0):<5} {f['path']}")
    if len(files) > 40:
        info(f"… {len(files) - 40} more file(s)")
    print(f"#{p['number']}: {p.get('changedFiles', len(files))} file(s), +{p.get('additions', 0)} "
          f"−{p.get('deletions', 0)} · full diff: dev pr diff {p['number']} --patch")
    return 0


# --------------------------------------------------------------------------- dev run (listing and watching live in cistatus)


def cmd_run(a) -> int:
    c = ctx()
    if c is None:
        return _need_repo(c)
    if a.run_cmd == "ls":
        return _run_ls(c, a)
    r = need_github(c, "run " + a.run_cmd)
    if r is not None:
        return r
    return {"view": _run_view, "rerun": _run_rerun}[a.run_cmd](c, a)


def _run_ls(c, a) -> int:
    from . import cistatus
    branch = None if a.all_branches else (a.branch or c.branch or None)
    res = cistatus.runs_for(c.top, branch, limit=a.limit, detail=False)
    if a.json:
        return emit(res)
    if res.get("error"):
        return fail(f"CI runs unavailable: {res['error']}")
    if not res.get("runs"):
        print(f"no CI runs for {branch or 'this repository'}")
        return 0
    for r in res["runs"]:
        print(f"  {str(r.get('id')):>12}  {cistatus.describe(r)}" + (f"  {r.get('branch')}" if not branch else ""))
    if res.get("provider") == "github":
        info("failing logs: dev run view <id> --failed · live progress: dev ci status --watch")
    return 0


def _latest_run_id(c):
    from . import cistatus
    res = cistatus.runs_for(c.top, c.branch or None, limit=1, detail=False)
    runs = res.get("runs") or []
    return str(runs[0]["id"]) if runs else None


def _run_view(c, a) -> int:
    rid = a.id or _latest_run_id(c)
    if not rid:
        return refuse("no runs for this branch", "dev run ls --all-branches")
    r, err = gh_json(["run", "view", rid, "-R", c.slug, "--json", "databaseId,number,workflowName,headBranch,headSha,"
                      "event,status,conclusion,url,displayTitle,jobs"], c)
    if r is None:
        return fail(f"gh run view {rid} failed: {err}")
    if a.json:
        return emit(r)
    state = r.get("conclusion") or r.get("status")
    mark = ui.OK if state == "success" else ui.FAIL if state in ("failure", "timed_out", "cancelled",
                                                                  "startup_failure") else ui.INFO
    print(f"{mark} {r.get('workflowName')} #{r.get('number')} {state} · {r.get('headBranch')} "
          f"{(r.get('headSha') or '')[:7]} · {r.get('event')} · {r.get('url')}")
    jobs = r.get("jobs") or []
    bad = [j for j in jobs if j.get("conclusion") in ("failure", "timed_out", "cancelled", "startup_failure")]
    for j in bad:
        step = next((s.get("name") for s in j.get("steps") or [] if s.get("conclusion") == "failure"), "")
        print(f"  {ui.FAIL} {j.get('name')}" + (f" · failed at “{step}”" if step else ""))
    good = len([j for j in jobs if j.get("conclusion") == "success"])
    if good:
        info(f"{good} job(s) passed")
    if a.failed and bad:
        rc, log, err = GH(["run", "view", rid, "-R", c.slug, "--log-failed"], cwd=c.top, timeout=120)
        if rc != 0:
            return fail(f"failing log unavailable: {_first_line(err)}")
        by_job = {}
        for ln in log.splitlines():
            by_job.setdefault(ln.split("\t", 1)[0], []).append(ln)
        for name, lines in list(by_job.items())[:4]:
            print(ui.dim(f"  ── {name}"))
            for row in trim_log("\n".join(lines), a.lines):
                print("    " + row)
    elif bad:
        info(f"failing steps' logs: dev run view {rid} --failed")
    return 1 if bad else 0


def _handoff_log(pid: str, text: str) -> bool:
    """One line in the project's session log in the handoff repo (as `dev handoff --log` writes it)."""
    try:
        from . import work
        ac = work.actor()
        f = work.log_session(pid, f"{core.fmt_ts(time.time())} · {ac['user']}@{ac['host']} · {ac['agent']} · {text}")
        work._commit([f], f"session log: {pid} ({work.actor_str(ac)})")
        return True
    except Exception:  # the handoff repo is optional: never fail the rerun over it
        return False


def _run_rerun(c, a) -> int:
    reason = (a.flaky or "").strip()
    if len(reason) < 8:
        return refuse("re-running CI without a change repeats the failure and costs a full CI cycle",
                      f"read the log (dev run view {a.id} --failed), reproduce it locally (dev ci --run), fix, push once. "
                      f"Only a proven flaky test: dev run rerun {a.id} --flaky \"<test and evidence>\"")
    r, err = gh_json(["run", "view", a.id, "-R", c.slug, "--json", "workflowName,headBranch,conclusion,jobs,url"], c)
    if r is None:
        return fail(f"gh run view {a.id} failed: {err}")
    jobs = [j.get("name") for j in r.get("jobs") or [] if j.get("conclusion") in ("failure", "timed_out")]
    rec = {"ts": int(time.time()), "repo": c.pid, "run": a.id, "workflow": r.get("workflowName"),
           "branch": r.get("headBranch"), "jobs": jobs, "reason": reason, "user": os.environ.get("USER", "")}
    f = core.STATE / "flaky.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    logged = _handoff_log(c.pid, f"flaky CI: {r.get('workflowName')} run {a.id} ({', '.join(jobs) or 'failed jobs'}) "
                                 f"re-run — {reason}")
    args = ["run", "rerun", a.id, "-R", c.slug] + ([] if a.all_jobs else ["--failed"])
    done, _, err = gh_do(args, c)
    if not done:
        return fail(f"gh run rerun failed: {err}")
    ok(f"re-running {'all' if a.all_jobs else 'the failed'} jobs of run {a.id} · flaky test recorded "
       f"({'handoff session log' if logged else core.tilde(f)}) · progress: dev ci status --watch")
    return 0


# --------------------------------------------------------------------------- dev release

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def _plausible_calver(v: str) -> bool:
    m = _VERSION_RE.match(v)
    if not m:
        return False
    yy, mm = int(m.group(1)), int(m.group(2))
    return 20 <= yy <= (time.localtime().tm_year % 100) + 1 and 1 <= mm <= 12


def version_files(top) -> dict:
    """Literal versions the repository keeps in files: VERSION, package.json, pyproject.toml."""
    out = {}
    root = Path(top)
    try:
        if (root / "VERSION").is_file():
            v = (root / "VERSION").read_text().strip().splitlines()[0].strip()
            if _VERSION_RE.match(v):
                out["VERSION"] = v.lstrip("v")
        pkg = core.read_json(root / "package.json", {}) or {}
        if isinstance(pkg, dict) and isinstance(pkg.get("version"), str) and _VERSION_RE.match(pkg["version"]):
            out["package.json"] = pkg["version"]
        if (root / "pyproject.toml").is_file():
            m = re.search(r'(?m)^version\s*=\s*["\']([^"\']+)["\']', (root / "pyproject.toml").read_text())
            if m and _VERSION_RE.match(m.group(1)):
                out["pyproject.toml"] = m.group(1)
    except (OSError, IndexError):
        pass
    return {k: v for k, v in out.items() if v != "0.0.0"}


def delivery_policy(top) -> dict:
    """The project's recorded delivery policy (`dev delivery`), or {}."""
    try:
        from . import delivery
        return delivery.load(delivery.scope_for(top)) or {}
    except Exception:  # the handoff repository is optional
        return {}


def version_scheme(top):
    """-> (scheme 'calver'|'semver', latest version tag or None, next CalVer). The recorded policy wins."""
    from .cli import calver
    cur, nxt = calver(top)
    tags = [t for t in core.gitout(top, "tag", "--list").split() if _VERSION_RE.match(t)]
    other = [t for t in tags if not _plausible_calver(t)]
    key = lambda t: tuple(int(x) for x in _VERSION_RE.match(t).groups())  # noqa: E731
    recorded = delivery_policy(top).get("versioning")
    if recorded == "calver":
        return "calver", cur, nxt
    files = version_files(top)
    if recorded == "semver" or other or any(not _plausible_calver(v) for v in files.values()):
        return "semver", (sorted(other or tags, key=key)[-1] if (other or tags) else None), nxt
    return "calver", cur, nxt


def changelog_section(top, version: str):
    for name in ("CHANGELOG.md", "CHANGES.md", "HISTORY.md"):
        f = Path(top) / name
        if not f.is_file():
            continue
        lines = f.read_text(errors="replace").split("\n")
        for i, ln in enumerate(lines):
            m = re.match(r"^(#{1,3})\s*\[?v?" + re.escape(version) + r"\]?(\s|$|\s*[-—(])", ln)
            if m:
                level = len(m.group(1))
                end = next((j for j in range(i + 1, len(lines))
                            if re.match(r"^#{1,%d}\s" % level, lines[j])), len(lines))
                text = "\n".join(lines[i + 1:end]).strip()
                return text or None
    return None


def _ci_runs(c, sha):
    """The GitHub Actions runs of commit `sha` (as `gh run list --commit` lists them), or None when gh cannot say."""
    runs, _ = gh_json(["run", "list", "-R", c.slug, "--commit", sha, "--json",
                       "databaseId,status,conclusion,workflowName,workflowDatabaseId,event"], c)
    return runs if isinstance(runs, list) else None


def _run_jobs(c, run):
    """The jobs of one GitHub Actions run (`gh run view --json jobs`: name, status, conclusion), or None when gh
    cannot say."""
    rid = run.get("databaseId") if isinstance(run, dict) else None
    if not rid:
        return None
    data, _ = gh_json(["run", "view", str(rid), "-R", c.slug, "--json", "jobs"], c)
    jobs = data.get("jobs") if isinstance(data, dict) else None
    return jobs if isinstance(jobs, list) else None


def _ci_state(runs):
    """'passing' | 'failing' | 'pending' | 'none' | 'unknown', with the workflows that failed or are still running."""
    if runs is None:
        return "unknown", []
    if not runs:
        return "none", []
    bad = [r.get("workflowName") for r in runs if r.get("status") == "completed" and
           r.get("conclusion") not in ("success", "skipped", "neutral")]
    if bad:
        return "failing", bad
    running = [r.get("workflowName") for r in runs if r.get("status") != "completed"]
    if running:
        return "pending", running
    return "passing", []


def _commit_tree(c, sha) -> str:
    tree = core.gitout(c.top, "rev-parse", "--verify", "-q", f"{sha}^{{tree}}")
    if tree:
        return tree
    commit, _ = gh_json(["api", f"repos/{c.slug}/git/commits/{sha}"], c)
    return str(((commit or {}).get("tree") or {}).get("sha") or "") if isinstance(commit, dict) else ""


def _own_repo_run(c, run) -> bool:
    """Whether a run's head came from this repository, not a fork (`gh api repos/{slug}/actions/runs/{id}`: its
    head_repository is its repository). A fork's pull request run had no secrets, so a job that skips its
    secret-backed steps still reports success. False when gh cannot say."""
    rid = run.get("databaseId") if isinstance(run, dict) else None
    if not rid:
        return False
    data, _ = gh_json(["api", f"repos/{c.slug}/actions/runs/{rid}"], c)
    if not isinstance(data, dict):
        return False
    came, repo = data.get("head_repository"), data.get("repository")
    came_id = came.get("id") if isinstance(came, dict) else None
    return came_id is not None and came_id == (repo.get("id") if isinstance(repo, dict) else None)


def _passed_twin(c, head, head_runs):
    """A commit with the tree of `head` whose pull request CI vouches for every workflow still running on `head`, as
    (sha, PR number or None); else None. Main's run of a merge commit repeats what the PR run tested when the trees
    are the same. Candidates: the parents of `head` (a merge commit's second parent is the PR's head), then the heads
    of the pull requests GitHub links to `head` (a squash or rebase merge). Workflows are matched by their file
    (workflowDatabaseId), since two files can share one name. Each workflow still running on `head` needs a passed
    pull request run of the same file whose head came from this repository (a fork's run had no secrets), in which:

    - every job still running or queued on `head` succeeded, by name;
    - every job that did not succeed, skipped included, has already succeeded on `head`. GitHub lists some jobs only
      once they start (one that calls a reusable workflow, a dynamic matrix, one waiting on `needs:`), so a job that
      runs only on push (a publish gated by `if: github.event_name == 'push'`) holds the release until main shows it
      passed, even when main does not list it yet. A job of another name (`publish / build` for `publish`) does not
      stand in.

    A skipped or still running PR run does not count. A job that already failed on `head`, a run whose jobs or whose
    origin gh cannot read, or a running run without a workflow id keeps the wait."""
    tree = _commit_tree(c, head)
    if not tree:
        return None
    running, needed, passed_here = set(), set(), set()
    for run in head_runs or []:
        if run.get("status") == "completed":
            continue
        wf = run.get("workflowDatabaseId")
        jobs = _run_jobs(c, run)
        if not wf or not jobs:
            return None
        running.add(wf)
        for j in jobs:
            key = (wf, j.get("name"))
            if j.get("status") != "completed":
                needed.add(key)
            elif j.get("conclusion") == "success":
                passed_here.add(key)
            elif j.get("conclusion") not in ("skipped", "neutral"):
                return None

    def candidates():
        for parent in core.gitout(c.top, "rev-list", "--parents", "-n", "1", head).split()[1:]:
            yield parent, None
        prs, _ = gh_json(["api", f"repos/{c.slug}/commits/{head}/pulls"], c)
        for pr in prs if isinstance(prs, list) else []:
            sha = ((pr or {}).get("head") or {}).get("sha") if isinstance(pr, dict) else None
            if sha:
                yield sha, pr.get("number")

    def vouches(runs):
        """Whether the pull request runs among `runs` (a twin's) cover every workflow and job still open on `head`."""
        own = [r for r in runs if r.get("event") == "pull_request" and r.get("conclusion") == "success"
               and r.get("workflowDatabaseId") in running and _own_repo_run(c, r)]
        if {r.get("workflowDatabaseId") for r in own} != running:
            return False
        done, not_done = set(), set()
        for r in own:
            jobs = _run_jobs(c, r)
            if not jobs:
                return False
            for j in jobs:
                passed = j.get("status") == "completed" and j.get("conclusion") == "success"
                (done if passed else not_done).add((r.get("workflowDatabaseId"), j.get("name")))
        return needed <= done and not_done <= passed_here

    seen = {head}
    for sha, pr in candidates():
        if sha in seen:
            continue
        seen.add(sha)
        if _commit_tree(c, sha) != tree:
            continue
        runs = _ci_runs(c, sha)
        if _ci_state(runs)[0] == "passing" and vouches(runs):
            return sha, pr
    return None


RELEASE_CLAIM_MIN = 60  # minutes dev release holds release:<project> for its run


def cmd_release(a) -> int:
    c = ctx()
    if c is None:
        return _need_repo(c)
    held = _release_claim(c, a)
    if isinstance(held, int):
        return held
    try:
        return _release(a, c)
    finally:
        _give_back(held)


def _release_claim(c, a):
    """Hold release:<project> while this release runs (peers.claim, RELEASE_CLAIM_MIN minutes), so the guard refuses
    another agent session's release, tag push or merge into the default branch meanwhile. The claim is the session's:
    devkit takes it for whoever of the session runs `dev release`, its tech lead or a crew member. -> (sender, resource)
    to give back, None when there is nothing to give back (a dry run, a project devkit cannot name, a claim the session
    already held, which it keeps as it is, or the developer's own terminal, which is never refused and is told who
    holds it), or the exit code 3 when another session holds it."""
    from . import identity, peers
    resource = f"release:{str(getattr(c, 'pid', '') or '').lower()}"
    if not peers.RESOURCE.match(resource):
        return None
    try:
        sp = identity.speaker(c.top)
        other = None if sp.how == "human" else peers.holder(resource, me=sp.key())
        if sp.how == "human" or a.dry_run:
            h = peers.holder(resource) if sp.how == "human" else other
            if h:
                ui.warn(f"{resource} is claimed by session {h['agent']}:{h['prefix'].lstrip('@')} "
                        f"({h.get('project') or '-'} · {h.get('branch') or '-'}): "
                        + ("releasing from your own terminal anyway" if sp.how == "human" else
                           "dev release refuses until it is free"))
            return None
        if other:
            return refuse(peers.refusal(other), code=3)
        if peers.holder(resource):
            return None  # this session holds it already, and keeps it as it is
        sender = dict(sp.as_dict(), lane="main")
        peers.claim(sender, resource, "dev release runs", minutes=RELEASE_CLAIM_MIN)
        return sender, resource
    except peers.PeerError as e:
        if e.code == 3:
            return refuse(str(e), code=3)
        info(f"{resource} not claimed for this release: {e}")
    except Exception as e:  # the claim is a guard rail; its failure never stops a release
        info(f"{resource} not claimed for this release ({type(e).__name__}: {e})")
    return None


def _give_back(held):
    """Give the release claim back, whatever the release did; one that cannot be given back lapses when it expires."""
    if not held:
        return
    from . import peers
    sender, resource = held
    try:
        peers.release(sender, resource, why="dev release ended")
    except Exception as e:
        info(f"{resource} stays claimed until it expires ({type(e).__name__}: {e})")


def _release(a, c) -> int:
    if not c.has_origin:
        return refuse("no `origin` remote to release to")
    fetched, err = _fetch(c, "--tags")
    if not fetched:
        return fail("git fetch failed", err)
    flow = str(delivery_policy(c.top).get("release_flow") or "")
    tool = next((t for t in ("release-please", "semantic-release") if t in flow), None)
    if tool:
        return refuse(f"this project releases with {tool} (its delivery policy)",
                      f"merge the {tool} release PR; it tags and publishes (dev delivery shows the policy)")
    if c.branch != c.default:
        return refuse(f"releases are cut from `{c.default}` (you are on `{c.branch or 'a detached HEAD'}`)",
                      f"merge the release PR (dev pr merge), then: git switch {c.default} && dev rebase")
    n = _dirty(c.top, untracked=False)
    if n:
        return refuse(f"{n} uncommitted change(s): release from a clean tree")
    ahead, behind = _count(c.top, f"origin/{c.default}..HEAD"), _count(c.top, f"HEAD..origin/{c.default}")
    if ahead or behind:
        return refuse(f"`{c.default}` is {ahead} ahead and {behind} behind origin/{c.default}",
                      "release exactly what is on origin: dev rebase")
    scheme, current, nxt = version_scheme(c.top)
    version = (a.version or "").lstrip("v") or (nxt if scheme == "calver" else "")
    if not version:
        return refuse(f"this repository uses its own version scheme (latest {current or 'in its files'})",
                      "dev release --version X.Y.Z")
    if not _VERSION_RE.match(version):
        return refuse(f"`{version}` is not a version (YY.MM.PATCH or X.Y.Z)")
    if scheme == "calver" and not _plausible_calver(version):
        return refuse(f"`{version}` is not CalVer YY.MM.PATCH (next: {nxt})")
    tag = "v" + version
    if _ref_sha(c.top, f"refs/tags/{tag}"):
        return refuse(f"tag {tag} already exists", f"next version: {nxt}" if scheme == "calver" else None)
    wrong = {f: v for f, v in version_files(c.top).items() if v != version}
    if wrong:
        f, v = next(iter(wrong.items()))
        return refuse(f"{f} says {v}, not {version}",
                      "bump the version (and the CHANGELOG) on the release branch, merge it, then release")
    why = (getattr(a, "why", None) or "").strip()
    pending = release_open_items(c, current)
    listing = "".join(f"\n    {it['meta']['id']} “{it['meta']['title']}” · {b} ({_merge_ref(m)})" for it, b, m in pending)
    if pending and not why:
        return refuse(f"{len(pending)} open work item(s) linked to branches merged since "
                      f"{current or 'the first commit'}:{listing}",
                      "close the shipped ones (dev task done <id>, or dev task reconcile --apply), or release anyway: "
                      "dev release --why \"<reason>\"")
    ev = evidence(c, host_only=True) if a.dry_run else _evidence_or_attest(c, a.ci_passed, "release")
    if not ev and not a.dry_run:
        return no_evidence(c, "release")
    head = _ref_sha(c.top, "HEAD")
    head_runs = _ci_runs(c, head) if c.provider == "github" else None
    remote_ci, names = _ci_state(head_runs) if c.provider == "github" else ("unknown", [])
    if remote_ci == "failing":
        return refuse(f"CI failed on {head[:7]}: {', '.join(map(str, names))}",
                      "fix it on a branch first (dev run view --failed)")
    if remote_ci == "pending":
        twin = _passed_twin(c, head, head_runs)
        if not twin:
            return refuse(f"CI is still running on {head[:7]}", "dev ci status --watch, then release")
        sha, pr = twin
        info(f"CI still runs on {head[:7]} ({', '.join(sorted(set(map(str, names))))}), but its tree passed the pull "
             f"request run on {sha[:7]}" + (f" (PR #{pr})" if pr else "") + ": releasing without waiting")
        remote_ci = f"pending, its tree passed on {sha[:7]}"
    notes = None
    if a.notes_file:
        try:
            notes = Path(a.notes_file).expanduser().read_text()
        except OSError as e:
            return fail(f"cannot read {a.notes_file}: {e}")
    else:
        notes = changelog_section(c.top, version)
    if notes:
        notes = strip_attribution(notes)[0]
    sign = a.sign or core.gitout(c.top, "config", "--bool", "tag.gpgSign") == "true"
    steps = [f"tag {tag} at {head[:7]}" + (" (signed)" if sign else ""), f"push {tag} to origin"]
    if c.provider == "github":
        steps.append("gh release create " + tag + (" with the CHANGELOG section" if notes else " with generated notes"))
    override = f"released with {len(pending)} open work item(s) · why: {why}" if pending else ""
    if pending:
        print(f"  open work items of merged branches, released anyway ({why}):{listing}")
    if a.dry_run:
        ok(f"dry run: release {tag} ({scheme}) · local CI: {describe_evidence(ev)} · remote CI: {remote_ci}")
        for s in steps:
            print(f"  → {s}")
        for line in released_branches(c, current, apply=False):
            print(line)
        if notes:
            print(ui.dim("  notes: " + notes.splitlines()[0][:100]))
        return 0
    v = guard.check_git(["git", "tag", "-a", tag, "-m", f"Release {version}"], c.top, f"git tag {tag} Release {version}")
    if v:
        return refuse(v[1])
    rc, _, err = core.git(c.top, "tag", "-s" if sign else "-a", tag, "-m", f"Release {version}")
    if rc != 0:
        return fail(f"git tag {tag} failed", err)
    rc, _, err = core.git(c.top, "push", "origin", f"refs/tags/{tag}", timeout=300)
    if rc != 0:
        return fail(f"pushing {tag} failed (the tag exists locally: git tag -d {tag} to retry)", err)
    if pending:
        from . import audit
        audit.record("dev.release.open-items", tag, ", ".join(f"{it['meta']['id']} ({b})" for it, b, _ in pending),
                     why=why)
    if c.provider != "github":
        ok(f"tagged and pushed {tag} · release pages for {c.host} are not supported yet")
        if override:
            ok(override)
        for line in released_branches(c, current, apply=True):
            print(line)
        return 0
    args = ["release", "create", tag, "-R", c.slug, "--verify-tag", "--title", tag]
    args += ["--notes", notes] if notes else ["--generate-notes"]
    if a.draft:
        args.append("--draft")
    if a.prerelease:
        args.append("--prerelease")
    done, out, err = gh_do(args, c, timeout=180)
    if not done:
        return fail(f"{tag} is pushed, but gh release create failed: {err}",
                    f"dev gh release create {tag} --verify-tag --notes-file <notes>")
    url = (out.strip().splitlines() or [""])[-1]
    ok(f"released {tag} · {url} · verify the artefact: gh release download {tag} -R {c.slug} -D <scratch dir>, "
       f"install and smoke-test it")
    if override:
        ok(override)
    if a.draft:
        info("the branches this release merged stay until it is published: dev branch prune --apply then")
    else:
        for line in released_branches(c, current, apply=True):
            print(line)
    return 0


def released_branches(c, since, apply: bool) -> list:
    """The branches a release merged: merged into it by the merge test (dev branch prune's) and not into the previous
    release `since`. With `apply`, deleted (the remote branch, the local one and its clean worktree, audited as
    branch.pruned); either way -> the lines to print. Branches merged before stay for dev gc and dev branch prune."""
    from . import branches
    try:
        plan = branches.prune_plan(c.top, offline=not c.has_origin)
    except Exception as e:  # the release is out; the branches wait for dev branch prune
        return [f"  {ui.WARN} merged branches not deleted ({e}): dev branch prune --apply"]

    def shipped_now(r) -> bool:
        ref = f"refs/heads/{r['name']}" if r["where"] == "local" else f"refs/remotes/origin/{r['name']}"
        return not since or not branches.merge_test(c.top, r["name"], since, ref=ref, prs=plan.get("prs"))["merged"]

    kept = [f"  {ui.dim('kept ' + r['where'] + ' ' + r['name'] + ': ' + r['why'])}" for r in plan["branches"]
            if r["state"] == "merged" and r["action"] == "keep" and shipped_now(r)]
    if not apply:
        return [f"  → delete {r['where']} {r['name']} ({r['why']})" for r in plan["branches"]
                if r["action"] == "delete" and shipped_now(r)] + kept
    return [branches._prune_line(r, True) for r in branches.prune(plan, only=shipped_now)] + kept


def release_open_items(c, since) -> list:
    """The open work items linked to the branches merged since the tag `since` (every merge of HEAD without one):
    [(item, branch, {"sha", "pr", "at"})]. The merges are `merges_into`'s: the first-parent merge commits of
    `since..HEAD` (on GitHub only with their merged pull request), and the squash, rebase and (off GitHub) fast-forward
    merges of the linked branches."""
    if not c.pid:
        return []
    from . import work
    merged = merges_into(c.top, "HEAD", work.linked_branches(c.pid), since, c)
    if not merged:
        return []
    return [(it, b, merged[b]) for it, b in work.linked_open(c.pid, merged)]


# --------------------------------------------------------------------------- dev issue


def cmd_issue(a) -> int:
    c = ctx()
    if c is None:
        return _need_repo(c)
    r = need_github(c, "issue " + a.issue_cmd)
    if r is not None:
        return r
    if a.issue_cmd == "ls":
        args = ["issue", "list", "-R", c.slug, "--state", a.state, "--limit", str(a.limit), "--json",
                "number,title,labels,assignees,updatedAt,url"]
        if a.mine:
            args += ["--assignee", "@me"]
        for lb in a.label or []:
            args += ["--label", lb]
        items, err = gh_json(args, c)
        if items is None:
            return fail(f"gh issue list failed: {err}")
        if a.json:
            return emit(items)
        if not items:
            print(f"no {a.state} issues" + (" assigned to you" if a.mine else ""))
            return 0
        rows = [[f"#{i['number']}", i["title"], ",".join(x["name"] for x in i.get("labels") or []) or "-",
                 ",".join(x["login"] for x in i.get("assignees") or []) or "-", _age(i.get("updatedAt"))] for i in items]
        print(ui.table(rows, ["#", "TITLE", "LABELS", "ASSIGNEE", "AGE"], max_widths={1: 60, 2: 30}, indent=0))
        return 0
    if a.issue_cmd == "view":
        fields = "number,title,state,body,labels,assignees,author,updatedAt,url" + (",comments" if a.comments else "")
        i, err = gh_json(["issue", "view", str(a.number), "-R", c.slug, "--json", fields], c)
        if i is None:
            return fail(f"gh issue view failed: {err}")
        if a.json:
            return emit(i)
        labels = ",".join(x["name"] for x in i.get("labels") or [])
        print(f"#{i['number']} {i['title']} · {i['state'].lower()}" + (f" · {labels}" if labels else "") +
              f" · by {(i.get('author') or {}).get('login', '?')} · {i.get('url')}")
        body = re.sub(r"\n{3,}", "\n\n", re.sub(r"<!--.*?-->", "", i.get("body") or "", flags=re.S)).strip()
        lines = body.splitlines()
        for ln in lines[:40]:
            print("  " + ln)
        if len(lines) > 40:
            info(f"… {len(lines) - 40} more line(s) · dev issue view {i['number']} --json")
        for cm in (i.get("comments") or [])[-5:]:
            print(ui.dim(f"  ── {(cm.get('author') or {}).get('login', '?')}, {_age(cm.get('createdAt'))}"))
            for ln in (cm.get("body") or "").strip().splitlines()[:12]:
                print("    " + ln)
        return 0
    title = (a.title or "").strip()
    if not title:
        return refuse("an issue needs a title", 'dev issue create --title "…" --body "…"')
    if a.body_file:
        raw = _text_arg(a)
        if raw is None:
            return fail(f"cannot read {a.body_file}")
    else:
        raw = a.body or ""
    body, _ = strip_attribution(raw)
    args = ["issue", "create", "-R", c.slug, "--title", strip_attribution(title)[0], "--body", body]
    for lb in a.label or []:
        args += ["--label", lb]
    if a.assignee:
        args += ["--assignee", a.assignee]
    done, out, err = gh_do(args, c)
    if not done:
        return fail(f"gh issue create failed: {err}")
    url = (out.strip().splitlines() or [""])[-1]
    ok(f"issue #{url.rstrip('/').rsplit('/', 1)[-1]} · {url}")
    return 0


# --------------------------------------------------------------------------- dev git … · dev gh … (escape hatches)

# `dev git` and `dev gh` pass no escape hatch from their environment to the guard (spec F8, rule 4; decided for
# 26.11.3 for DEVKIT_FLAKY and DEVKIT_CROSS_PROJECT too): dev's environment is not the command the guard parsed, and an
# exported hatch would lift a rule for every later call. A hatch is a command's own prefix at PreToolUse
# (`DEVKIT_FLAKY=1 gh run rerun 5`, a recorded use), or, for the push budget, a real local run's evidence.


def _agent() -> str:
    return os.environ.get("DEVKIT_AGENT") or "codex"  # no prompt is possible here: "ask" decisions become refusals


def _guard_cmd(argv, cwd):
    acks = []
    if list(argv[:1]) == ["gh"] and list(argv[1:3]) in (["pr", "merge"], ["release", "create"]):
        acks.append("DEVKIT_RAW_GH=1")  # devkit gates these itself below (checks, reviews, policy); `--admin` stays refused
    return guard.check_bash(" ".join(shlex.quote(x) for x in acks + list(argv)), cwd, _agent())


def _strip_dashdash(args):
    return args[1:] if args[:1] == ["--"] else args


def _git_sub(args):
    i = 0
    while i < len(args):
        x = args[i]
        if x in ("-c", "-C", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(args):
            i += 2
        elif x.startswith("-"):
            i += 1
        else:
            return x, args[i + 1:]
    return None, []


def git_push_policy(args, cwd, ci_passed: bool = False):
    """`dev git push …`: the dev push rules for arbitrary push arguments. Exit code to refuse with, or None.
    `ci_passed`: the command's own --ci-passed, which stands for local CI evidence but never lifts the push budget."""
    c = ctx(cwd)
    text = "git push " + " ".join(args)
    acks = set()
    force = any(x in ("--force", "-f") or (re.match(r"^-[a-zA-Z]*f[a-zA-Z]*$", x) and not x.startswith("--"))
                for x in args) or any(x.startswith("+") for x in args if not x.startswith("-"))
    if force:
        return refuse("plain force-push is blocked", "dev push --force-with-lease (on your own branch only)")
    if c is None or any(x in ("--tags", "--delete", "-d", "--mirror", "--all", "--prune") for x in args):
        v = guard.check_git(["git", "push", *args], cwd, text, frozenset(acks))
        return refuse(v[1]) if v else None
    pos = guard._positionals(args, {"-o", "--push-option", "--repo", "--receive-pack", "--exec"})
    refspecs = pos[1:] or ([c.branch] if c.branch else [])
    run_marker = False
    for spec in refspecs:
        src, _, dst = spec.lstrip("+").partition(":")
        if not src:
            continue  # ":branch" deletes
        dst = (dst or (c.branch if src == "HEAD" else src)).replace("refs/heads/", "", 1)
        if dst.startswith("refs/tags/"):
            continue
        if dst == c.default or dst in guard.PERMANENT_BRANCHES:
            return refuse(f"`{dst}` is the default branch: changes go through a branch and a PR",
                          "dev push from your branch (only when the user explicitly asks: plain `git push`)")
        ev = evidence(c, src)
        if not ev and ci_passed:
            ev = {"how": "attested"}
        if not ev:
            return no_evidence(c, "push", rev=src)
        run_marker = run_marker or ev.get("how") == "run"
    if run_marker:
        acks.add("DEVKIT_LOCAL_CI_PASSED")
    v = guard.check_git(["git", "push", *args], cwd, text, frozenset(acks))
    return refuse(v[1]) if v else None


def git_commit_policy(args, cwd):
    """`dev git commit …`: Conventional Commit header, branch rules, attribution and the staged-secret scan."""
    c = ctx(cwd)
    msgs, i = [], 0
    while i < len(args):
        x = args[i]
        if x in ("-m", "--message") and i + 1 < len(args):
            msgs.append(args[i + 1])
            i += 2
            continue
        if x.startswith("--message="):
            msgs.append(x.split("=", 1)[1])
        elif x.startswith("-m") and len(x) > 2:
            msgs.append(x[2:])
        elif x in ("-F", "--file") and i + 1 < len(args):
            try:
                msgs.append(Path(cwd, args[i + 1]).read_text())
            except OSError:
                pass
            i += 2
            continue
        i += 1
    reuse = any(x.startswith(("--fixup", "--squash", "-C", "-c", "--reuse-message", "--reedit-message", "--no-edit"))
                for x in args)
    if c is not None:
        g = _branch_gate(c)
        if g:
            return g
    if msgs and not reuse:
        v = message_violation(msgs[0].strip().split("\n")[0])
        if v:
            return refuse(v)
    elif not msgs and not reuse and not ("--amend" in args) and not sys.stdin.isatty():
        return refuse("no message and no terminal for an editor", 'dev commit -m "type(scope): summary"')
    if c is not None and any(x in ("-a", "--all") or re.fullmatch(r"-[a-zA-Z]*a[a-zA-Z]*", x) and not x.startswith("--")
                             for x in args):
        core.git(c.top, "add", "-u")  # so the staged-secret scan sees what -a commits
    v = _guard_cmd(["git", "-C", cwd, "commit", *args], cwd)
    return refuse(v[1]) if v else None


_INTERACTIVE = {"rebase": ("-i", "--interactive"), "add": ("-i", "--interactive", "-p", "--patch", "-e", "--edit"),
                "commit": ("--interactive", "-p", "--patch"), "checkout": ("-p", "--patch"), "reset": ("-p", "--patch"),
                "restore": ("-p", "--patch"), "stash": ("-p", "--patch"), "clean": ("-i", "--interactive")}


def cmd_git(a) -> int:
    args = _strip_dashdash(list(a.args or []))
    cwd = a.git_dir or os.getcwd()
    if not args:
        return refuse("usage: dev git <git arguments> (options before the subcommand: dev git -- -c k=v …)")
    sub, rest = _git_sub(args)
    if sub == "stash" and (not rest or rest[0] in ("push", "save", "pop", "apply", "drop", "clear", "create",
                                                   "store") or rest[0].startswith("-")):
        return refuse("the stash is shared by every worktree of the repository",
                      "dev commit --wip saves your changes as a commit; dev commit --unwip restores them")
    if sub in _INTERACTIVE and any(x in _INTERACTIVE[sub] for x in rest) and not sys.stdin.isatty():
        return refuse(f"`git {sub} {' '.join(x for x in rest if x in _INTERACTIVE[sub])}` needs an interactive terminal")
    if sub == "push":
        ci_passed = "--ci-passed" in rest
        if ci_passed:
            rest = [x for x in rest if x != "--ci-passed"]
            args = [x for x in args if x != "--ci-passed"]
        r = git_push_policy(rest, cwd, ci_passed=ci_passed)
    elif sub == "commit":
        r = git_commit_policy(rest, cwd)
    else:
        v = _guard_cmd(["git", *args], cwd)
        r = refuse(v[1]) if v else None
    if r is not None:
        return r
    return EXEC(["git", *args], cwd=cwd)


def cmd_gh(a) -> int:
    args = _strip_dashdash(list(a.args or []))
    cwd = os.getcwd()
    if not args:
        return refuse("usage: dev gh <gh arguments>")
    ci_passed = "--ci-passed" in args
    args = [x for x in args if x != "--ci-passed"]
    v = _guard_cmd(["gh", *args], cwd)
    if v:
        return refuse(v[1])
    c = ctx(cwd)
    head = args[:2]
    if c is not None and c.provider == "github":
        if head == ["pr", "create"]:
            draft = "--draft" in args or "-d" in args
            if not (evidence(c) or ci_passed):
                why = draft_ci(c, c.branch, _ref_sha(c.top, "HEAD")) if draft else ""
                if not draft or why:
                    return no_evidence(c, "pr create", draft_ci=why)
            t = guard._opt_value(args[2:], ("--title", "-t"))
            v = pr_title_violation(c, t) if t else None
            if v:
                return refuse(v)
            body = guard._opt_value(args[2:], ("--body", "-b"))
            bf = guard._opt_value(args[2:], ("--body-file", "-F"))
            if bf and bf != "-":
                try:
                    body = Path(cwd, bf).read_text()
                except OSError:
                    body = None
            if body is not None and not draft:
                missing = body_problems(body, pr_template(c.top, lang_of())[0])
                if missing:
                    return refuse(f"PR body: {' and '.join(missing)} empty", "fill them, or open it with --draft")
            if "--fill" in args or "--fill-first" in args or "--fill-verbose" in args:
                info("gh fills the body from the commits; dev pr create uses the PR template instead")
        elif head == ["pr", "merge"]:
            if "--admin" in args:
                return refuse("--admin bypasses branch protection", "dev pr merge (checks and reviews first)")
            pos = guard._positionals(args[2:], {"-t", "--subject", "-b", "--body", "-F", "--body-file",
                                                "--match-head-commit", "-A", "--author-email", "-R", "--repo"})
            sel = pos[0] if pos else c.branch
            p, err = gh_json(["pr", "view", sel or "", "-R", c.slug, "--json", PR_FIELDS], c)
            if p is None:
                return fail(f"no PR for {sel}: {err}")
            why = merge_gate(c, p, auto="--auto" in args)
            if why:
                return refuse(f"not merging #{p['number']}: " + "; ".join(why))
            v = pr_title_violation(c, p.get("title", "")) if "--squash" in args or "-s" in args else None
            if v:
                return refuse(v)
        elif head == ["release", "create"]:
            if c.branch != c.default or _dirty(c.top, untracked=False):
                return refuse(f"release from a clean `{c.default}`", "dev release (checks, tag, notes)")
            if not (evidence(c) or ci_passed):
                return no_evidence(c, "release")
    return EXEC(["gh", *args], cwd=cwd)


# --------------------------------------------------------------------------- registration

COMMAND_NAMES = ("branch", "commit", "rebase", "push", "pr", "run", "release", "issue", "git", "gh")
HELP = ("Git and GitHub", [
    ("branch new|rename|ls|clean", "Branches from an up-to-date origin/<default>, named by the standard"),
    ("branch prune [--apply]", "Every branch with its state; --apply deletes the merged ones, remote and local"),
    ("branch embargo <branch> --why …", "Keep a branch off the remote; dev backup and dev sync never push it"),
    ("commit -m \"type(scope): …\"", "Conventional Commit (-a, paths, --amend); --wip/--unwip instead of a stash"),
    ("rebase [--continue|--abort]", "Fetch and rebase on origin/<default>, with a conflict report"),
    ("push [--force-with-lease]", "One push: local-CI evidence, budget, no default branch, URL of the PR"),
    ("pr create|view|ls|checks|merge", "PRs from the template; checks --watch with failing logs; gated merge"),
    ("run ls|view --failed|rerun", "CI runs, failing logs trimmed; rerun only with --flaky \"reason\""),
    ("release [--version V]", "Tag and publish from a verified default branch (CalVer by default)"),
    ("issue ls|view|create", "Issues, compact"),
    ("git … · gh …", "Escape hatches with the same policy checks"),
])


def _lang_arg(p):
    p.add_argument("--lang", help="language of generated text (default: devkit config `language`, else en-US)")


def register(add):
    """Add the git/GitHub commands. `add(name, fn, help)` returns an argparse subparser; handlers return exit codes."""
    p = add("branch", cmd_branch, "Branches: new (from an up-to-date origin/<default>) · rename · ls · clean · prune · "
                                  "embargo")
    sp = p.add_subparsers(dest="branch_cmd", metavar="new|rename|ls|clean|prune|embargo")
    sp.required = True
    q = sp.add_parser("new", help="create <type>/<topic> from an up-to-date origin/<default> and switch to it")
    q.add_argument("name", help="<type>/<topic>, e.g. feat/invoice-export")
    q.add_argument("--ticket", help="ticket key or issue number to put in front of the topic")
    q.add_argument("--base", help="start point (default origin/<default>)")
    q = sp.add_parser("rename", help="rename the current branch (harness names such as claude/…) before the first push")
    q.add_argument("name", help="<type>/<topic>")
    q.add_argument("--ticket")
    for name, h in (("ls", "local branches: upstream, ahead/behind, merged, PR, worktree"),
                    ("clean", "delete local branches that are merged or gone upstream (dry run without --apply)")):
        q = sp.add_parser(name, help=h)
        q.add_argument("--offline", action="store_true", help="no fetch and no GitHub lookups")
        q.add_argument("--json", action="store_true")
        if name == "clean":
            q.add_argument("--apply", action="store_true", help="delete them")
    q = sp.add_parser("prune", help="every local and remote branch with its state (merged and how, open PR, not "
                                    "merged, protected); --apply deletes the merged ones and their clean worktrees")
    q.add_argument("--apply", action="store_true", help="delete the merged branches nothing protects (the remote "
                                                         "branch, the local one and its clean worktree)")
    side = q.add_mutually_exclusive_group()
    side.add_argument("--remote", action="store_true", help="only the branches on origin")
    side.add_argument("--local", action="store_true", help="only the branches on this Mac (and their worktrees)")
    q.add_argument("--offline", action="store_true", help="no fetch and no GitHub lookups; remote branches stay")
    q.add_argument("--json", action="store_true")
    q = sp.add_parser("embargo", help="keep a branch off the remote: a backup never pushes it (no branch: the list)")
    q.add_argument("branch", nargs="?", help="the branch (default with --lift: none; without a branch: list them)")
    q.add_argument("--why", help="why it stays off the remote (required to set one)")
    q.add_argument("--until", help="the step or date after which it may be public")
    q.add_argument("--lift", action="store_true", help="end the embargo of the branch")
    q.add_argument("--json", action="store_true")

    p = add("commit", cmd_commit, "Conventional Commit of the staged changes, -a (tracked) or the given paths; in a "
                                  "crew lane, only the staged files inside its scope (the others stay staged)")
    p.add_argument("-m", "--message", action="append", help="header `type(scope): summary`; more -m add paragraphs")
    p.add_argument("--why", help="first paragraph of the body: why the change is needed (the commit template)")
    p.add_argument("--cause", help="the root cause, as `Root cause: …` right after the why; required for fix and "
                                   "revert commits, at least 15 characters, never a placeholder such as tbd")  # devkit: allow-stub help
    p.add_argument("--body", action="append", help="a body paragraph (repeat for more): what changed (after --why), "
                                                   "or the reason without --why")
    p.add_argument("--verified", help="last paragraph of the body: how you verified it, as `Verified: …`")
    p.add_argument("--refs", action="append", help="trailer `Refs:`, an issue (#12), a URL or a ticket key (repeat for "
                                                   "more); default: the ticket of the task started on this branch")
    p.add_argument("--breaking", help="trailer `BREAKING CHANGE:`: what breaks and how to move to the new behavior")
    p.add_argument("-a", "--all", action="store_true", help="stage every tracked change first (in a crew lane, those "
                                                            "inside its scope)")
    p.add_argument("--amend", action="store_true", help="amend HEAD (keeps its message without -m)")
    p.add_argument("--wip", action="store_true", help="save everything as a WIP commit (instead of git stash)")
    p.add_argument("--unwip", action="store_true", help="undo the WIP commit at HEAD, keeping its changes")
    p.add_argument("paths", nargs="*", help="commit only these paths")

    p = add("rebase", cmd_rebase, "Fetch and rebase the current branch on origin/<default> (fast-forward on it)")
    p.add_argument("--base", help="rebase onto this ref instead of origin/<default>")
    p.add_argument("--continue", dest="cont", action="store_true")
    p.add_argument("--abort", action="store_true")
    p.add_argument("--skip", action="store_true")

    p = add("push", cmd_push, "Push the current branch once: naming, WIP, freshness, local-CI evidence, push budget")
    p.epilog = "Local CI evidence: `dev ci --run` records it for a tree. " + HOST_RULE
    p.add_argument("--force-with-lease", action="store_true", help="after a rebase or amend, on your own branch")
    p.add_argument("--ci-passed", action="store_true", help="attest that the full local suite passed on this tree")
    p.add_argument("--allow-behind", action="store_true", help="first push although origin/<default> moved on")
    p.add_argument("--dry-run", action="store_true")

    p = add("pr", cmd_pr, "Pull requests (GitHub): create · view · ls · checks · merge · ready · comment · review · diff")
    sp = p.add_subparsers(dest="pr_cmd", metavar="create|view|ls|checks|merge|ready|comment|review|diff")
    sp.required = True
    q = sp.add_parser("create", help="open the PR of the pushed branch, body from the PR template")
    q.add_argument("--title", help="Conventional Commit title (default: the first commit, else the branch)")
    q.add_argument("--base", help="base branch (default: the repository default)")
    q.add_argument("--draft", action="store_true", help="open as a draft (empty sections allowed); the default "
                   "with delivery.draft_pr")
    q.add_argument("--ready", action="store_true", help="ready for review although delivery.draft_pr opens drafts")
    q.add_argument("--fill", action="store_true", help="fill from the commits and the template (the default)")
    q.add_argument("--body-file", help="your own body (devkit checks it); - reads stdin")
    q.add_argument("--edit", action="store_true", help="edit the pre-filled body in $EDITOR first")
    q.add_argument("--ci-passed", action="store_true", help="attest that the local suite passed on HEAD")
    q.add_argument("--dry-run", action="store_true", help="print the title and the pre-filled body")
    _lang_arg(q)
    q = sp.add_parser("view", help="one-line state of a PR")
    q.add_argument("number", nargs="?")
    q.add_argument("--body", action="store_true")
    q.add_argument("--json", action="store_true")
    q = sp.add_parser("ls", help="your PRs and PRs waiting for your review")
    q.add_argument("--all", action="store_true", help="also the other open PRs")
    q.add_argument("--all-projects", action="store_true", help="across projects (registry cache + review search)")
    q.add_argument("--offline", action="store_true", help="with --all-projects: cache only")
    q.add_argument("--json", action="store_true")
    q = sp.add_parser("checks", help="check summary and failing jobs with trimmed logs; --watch waits for the result")
    q.add_argument("number", nargs="?")
    q.add_argument("--watch", action="store_true", help="gh pr checks --watch; prints only the final result")
    q.add_argument("--fail-fast", action="store_true", help="with --watch: stop at the first failure")
    q.add_argument("--interval", type=int, default=30, help="with --watch: seconds between refreshes (default 30)")
    q.add_argument("--timeout", type=int, default=90, help="with --watch: minutes (default 90)")
    q.add_argument("--lines", type=int, default=25, help="log lines per failing job")
    q.add_argument("--json", action="store_true")
    q = sp.add_parser("merge", help="merge with the repository's method when checks pass and reviews allow")
    q.add_argument("number", nargs="?")
    q.add_argument("--squash", dest="method", action="store_const", const="squash")
    q.add_argument("--merge", dest="method", action="store_const", const="merge")
    q.add_argument("--rebase", dest="method", action="store_const", const="rebase")
    q.add_argument("--auto", action="store_true", help="let GitHub merge when the checks pass")
    q.add_argument("--keep-branch", action="store_true")
    q.add_argument("--dry-run", action="store_true")
    q = sp.add_parser("ready", help="mark a draft ready for review: the one CI run (title, body and local CI checked)")
    q.add_argument("number", nargs="?")
    q.add_argument("--ci-passed", action="store_true", help="attest that the local suite passed on HEAD")
    _lang_arg(q)
    for name in ("comment", "review"):
        q = sp.add_parser(name, help="comment on a PR" if name == "comment" else "approve, request changes or comment")
        q.add_argument("number", nargs="?")
        q.add_argument("-m", "--message", help="text")
        q.add_argument("--body-file")
        if name == "review":
            g = q.add_mutually_exclusive_group(required=True)
            g.add_argument("--approve", dest="verdict", action="store_const", const="approve")
            g.add_argument("--request-changes", dest="verdict", action="store_const", const="request-changes")
            g.add_argument("--comment", dest="verdict", action="store_const", const="comment")
    q = sp.add_parser("diff", help="files changed with +/− (default) or the full patch")
    q.add_argument("number", nargs="?")
    q.add_argument("--stat", action="store_true", help="per-file +/− (the default)")
    q.add_argument("--patch", action="store_true")
    q.add_argument("--name-only", action="store_true")
    q.add_argument("--json", action="store_true")

    p = add("run", cmd_run, "CI runs: ls (any host) · view [id] --failed · rerun <id> --flaky \"reason\"")
    sp = p.add_subparsers(dest="run_cmd", metavar="ls|view|rerun")
    sp.required = True
    q = sp.add_parser("ls", help="recent runs of this branch (live progress: dev ci status --watch)")
    q.add_argument("--branch")
    q.add_argument("--all-branches", action="store_true")
    q.add_argument("--limit", type=int, default=8)
    q.add_argument("--json", action="store_true")
    q = sp.add_parser("view", help="a run's failing jobs; --failed adds the trimmed failing-step logs")
    q.add_argument("id", nargs="?")
    q.add_argument("--failed", action="store_true")
    q.add_argument("--lines", type=int, default=30)
    q.add_argument("--json", action="store_true")
    q = sp.add_parser("rerun", help="re-run failed jobs of a run, only for a proven flaky test (recorded)")
    q.add_argument("id")
    q.add_argument("--flaky", metavar="REASON", help="the flaky test and the evidence (required)")
    q.add_argument("--all-jobs", action="store_true")

    p = add("release", cmd_release, "Tag and publish a release from a verified default branch (CalVer by default)")
    p.add_argument("--version", help="YY.MM.PATCH (default: the next CalVer) or X.Y.Z for other schemes")
    p.add_argument("--notes-file", help="release notes (default: the CHANGELOG section, else generated)")
    p.add_argument("--ci-passed", action="store_true", help="attest that the local suite passed on HEAD")
    p.add_argument("--sign", action="store_true", help="signed tag (default: git config tag.gpgSign)")
    p.add_argument("--draft", action="store_true")
    p.add_argument("--prerelease", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--why", metavar="REASON", help="release although work items of the merged branches are still "
                                                   "open (recorded in the audit and the output)")

    p = add("issue", cmd_issue, "Issues (GitHub): ls · view · create")
    sp = p.add_subparsers(dest="issue_cmd", metavar="ls|view|create")
    sp.required = True
    q = sp.add_parser("ls")
    q.add_argument("--mine", action="store_true", help="assigned to you")
    q.add_argument("--label", action="append")
    q.add_argument("--state", default="open", choices=["open", "closed", "all"])
    q.add_argument("--limit", type=int, default=20)
    q.add_argument("--json", action="store_true")
    q = sp.add_parser("view")
    q.add_argument("number")
    q.add_argument("--comments", action="store_true")
    q.add_argument("--json", action="store_true")
    q = sp.add_parser("create")
    q.add_argument("--title", required=True)
    q.add_argument("--body")
    q.add_argument("--body-file")
    q.add_argument("--label", action="append")
    q.add_argument("--assignee")
    q.set_defaults(message=None)

    p = add("git", cmd_git, "git with devkit's policy checks (push and commit get the dev push/commit rules)")
    p.add_argument("-C", dest="git_dir", metavar="DIR", help="run in DIR")
    p.add_argument("args", nargs=argparse.REMAINDER, help="git arguments")
    p = add("gh", cmd_gh, "gh with devkit's policy checks (pr create/merge and release create are gated)")
    p.add_argument("args", nargs=argparse.REMAINDER, help="gh arguments")
    return list(COMMAND_NAMES)
