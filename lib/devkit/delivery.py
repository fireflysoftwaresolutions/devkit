# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Delivery policy: how a project versions, tests, releases and ships — detected, recommended, recorded, enforced.

detect(repo)        what the repository does today: version scheme and source, release tooling, changelog, CI,
                    the local test gate (the commands CI runs), branch model, what it publishes and deploys
recommend(found)    the policy devkit recommends: keep an existing scheme; otherwise SemVer for libraries that others
                    depend on, CalVer YY.MM.PATCH for applications, services and platforms
gaps(found, policy) what is missing for a release you can trust, each with the fix
The recorded policy lives in the handoff repository: policies/<project id>.json, and policies/initiatives/<slug>.json
for release trains that span projects. Sessions get it in their context; the guard refuses tags that break it;
`dev release` follows it.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import core, work
from .core import HANDOFF

POLICIES = HANDOFF / "policies"
CALVER_RE = re.compile(r"^v?(\d{2})\.(\d{1,2})\.(\d+)$")
SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?$")
# A PEP 440 public version in its normal form: [N!]N(.N)*[{a|b|rc}N][.postN][.devN]
PEP440_RE = re.compile(r"^v?(?:\d+!)?\d+(?:\.\d+)*(?:(?:a|b|rc)\d+)?(?:\.post\d+)?(?:\.dev\d+)?$")
PEP440_ONLY = re.compile(r"\d(?:a|b|rc)\d+|\.post\d+|\.dev\d+")  # a pre, post or dev release: not SemVer's syntax
PEP440_FORMAT = "MAJOR.MINOR.MICRO[{a|b|rc}N][.postN][.devN]"
FIELDS = ("versioning", "tag_format", "version_source", "release_flow", "changelog", "test_gate", "ci", "branch_model",
          "publish", "deploy", "kind", "release_train", "job_timeout_minutes", "delete_branch_on_merge")
LIST_FIELDS = ("test_gate", "publish", "deploy")
BOOL_FIELDS = ("delete_branch_on_merge",)  # the host deletes a pull request's branch when it merges
TIMEOUT_RANGE = (5, 360)  # GitHub's hard limit is 6 hours
_BOOLS = {"true": True, "on": True, "yes": True, "1": True, "false": False, "off": False, "no": False, "0": False}


def set_field(policy: dict, key: str, value: str) -> dict:
    """One `--set key=value`, validated: lists split on ';', the job timeout an integer of minutes, switches
    true/false."""
    if key not in FIELDS and key != "bump":
        raise ValueError(f"unknown field {key}: {', '.join(FIELDS)}")
    if key in BOOL_FIELDS:
        if value.strip().lower() not in _BOOLS:
            raise ValueError(f"{key} is true or false, not {value!r}")
        policy[key] = _BOOLS[value.strip().lower()]
    elif key in LIST_FIELDS:
        policy[key] = [x.strip() for x in value.split(";") if x.strip()]
    elif key == "job_timeout_minutes":
        lo, hi = TIMEOUT_RANGE
        if not value.strip().isdigit() or not lo <= int(value) <= hi:
            raise ValueError(f"job_timeout_minutes is a whole number of minutes from {lo} to {hi}, not {value!r}")
        policy[key] = int(value)
    else:
        policy[key] = value
    return policy


def _read(p: Path, limit=300_000) -> str:
    try:
        return p.read_text(errors="ignore")[:limit]
    except OSError:
        return ""


# --------------------------------------------------------------------------- detection

def _tags(repo: Path) -> list:
    return [t for t in core.gitout(repo, "tag", "--sort=-v:refname").splitlines()[:60] if t]


def _pep440(v: str) -> bool:
    """A PEP 440 version that uses a pre, post or dev release (0.1.0a16, 1.0.0.post1, 2.0.0.dev3, 1.1.0rc1)."""
    return bool(PEP440_RE.match(v) and PEP440_ONLY.search(v))


def _scheme_from_tags(tags: list):
    """-> (scheme, tag format) of the version tags: calver, semver, pep440 (tags that use PEP 440's pre, post or dev
    releases and none of SemVer's own `-pre` or `+build` forms), other, or (None, None) without tags."""
    cal = [t for t in tags if CALVER_RE.match(t) and 1 <= int(CALVER_RE.match(t).group(2)) <= 12]
    sem = [t for t in tags if SEMVER_RE.match(t) and t not in cal]
    pep = [t for t in tags if _pep440(t) and t not in cal and t not in sem]
    if not tags:
        return None, None
    if len(cal) >= max(1, len(sem) + len(pep)):
        return "calver", "vYY.MM.PATCH" if cal[0].startswith("v") else "YY.MM.PATCH"
    if pep and not any(re.search(r"[-+]", t) for t in sem):
        return "pep440", ("v" if pep[0].startswith("v") else "") + PEP440_FORMAT
    if sem:
        return "semver", "vMAJOR.MINOR.PATCH" if sem[0].startswith("v") else "MAJOR.MINOR.PATCH"
    return "other", tags[0]


def _version_source(repo: Path):
    checks = [
        ("package.json", lambda t: json.loads(t).get("version") if t.strip().startswith("{") else None),
        ("pom.xml", lambda t: (re.search(r"<artifactId>[^<]+</artifactId>\s*<version>([^<$]+)</version>", t) or [None, None])[1]),
        ("pyproject.toml", lambda t: (re.search(r'(?m)^version\s*=\s*"([^"]+)"', t) or [None, None])[1]),
        ("composer.json", lambda t: json.loads(t).get("version") if t.strip().startswith("{") else None),
        ("gradle.properties", lambda t: (re.search(r"(?m)^version\s*=\s*(\S+)", t) or [None, None])[1]),
        ("build.gradle.kts", lambda t: (re.search(r'(?m)^version\s*=\s*"([^"]+)"', t) or [None, None])[1]),
        ("Cargo.toml", lambda t: (re.search(r'(?m)^version\s*=\s*"([^"]+)"', t) or [None, None])[1]),
        ("VERSION", lambda t: t.strip().splitlines()[0] if t.strip() else None),
    ]
    for name, get in checks:
        f = repo / name
        if f.exists():
            try:
                v = get(_read(f))
            except (ValueError, AttributeError, TypeError, IndexError):
                v = None
            if v and "dynamic" not in str(v):
                return name, str(v).strip()
    return None, None


def _workflow_text(repo: Path) -> str:
    d = repo / ".github" / "workflows"
    return "\n".join(_read(f) for f in sorted(d.glob("*.y*ml"))) if d.is_dir() else ""


def detect(repo) -> dict:
    repo = Path(repo)
    wf = _workflow_text(repo)
    tags = _tags(repo)
    scheme, tag_format = _scheme_from_tags(tags)
    vsrc, vval = _version_source(repo)
    if not scheme and vval:
        scheme = "calver" if CALVER_RE.match(vval) else "semver" if SEMVER_RE.match(vval.split("-")[0]) else \
            "pep440" if _pep440(vval) else "other"
    tools = []
    for marker, name in ((".release-please-manifest.json", "release-please"), ("release-please-config.json", "release-please"),
                         (".releaserc", "semantic-release"), (".releaserc.json", "semantic-release"),
                         ("release.config.js", "semantic-release"), (".goreleaser.yml", "goreleaser"),
                         (".goreleaser.yaml", "goreleaser"), ("cliff.toml", "git-cliff"), (".changeset", "changesets"),
                         (".bumpversion.cfg", "bump-my-version"), (".cz.toml", "commitizen")):
        if (repo / marker).exists() and name not in tools:
            tools.append(name)
    if "maven-release-plugin" in _read(repo / "pom.xml"):
        tools.append("maven-release-plugin")
    if re.search(r"release-please-action|googleapis/release-please", wf):
        tools.append("release-please") if "release-please" not in tools else None
    changelog = None
    for name in ("CHANGELOG.md", "CHANGES.md", "HISTORY.md", "RELEASE_NOTES.md"):
        if (repo / name).exists():
            text = _read(repo / name, 4000).lower()
            changelog = "keep-a-changelog" if "keep a changelog" in text or "keepachangelog" in text else name
            break
    ci = [n for f, n in ((".github/workflows", "github-actions"), (".gitlab-ci.yml", "gitlab-ci"),
                         ("bitbucket-pipelines.yml", "bitbucket-pipelines"), ("azure-pipelines.yml", "azure-pipelines"),
                         ("Jenkinsfile", "jenkins")) if (repo / f).exists()]
    try:
        from . import ci as cimod
        gate = cimod.local_commands(repo)[:8]
    except Exception:
        gate = []
    if not gate:
        gate = _default_gate(repo)
    branches = core.gitout(repo, "branch", "-r", "--format=%(refname:short)").splitlines()
    model = "gitflow" if any(b.endswith("/develop") for b in branches) else "trunk"
    if any("/release/" in b for b in branches) or re.search(r"branches:\s*\[?[^\n]*release/", wf):
        model += " + release branches"
    publish = []
    pom = _read(repo / "pom.xml")
    if "<distributionManagement>" in pom or "central-publishing-maven-plugin" in pom or "nexus-staging" in pom:
        publish.append("maven")
    pkg = _read(repo / "package.json")
    if pkg and '"private": true' not in pkg and ('"publishConfig"' in pkg or "npm publish" in wf or "pnpm publish" in wf):
        publish.append("npm")
    if re.search(r"pypa/gh-action-pypi-publish|twine upload|uv publish", wf):
        publish.append("pypi")
    if re.search(r"packagist", wf + _read(repo / "composer.json")):
        publish.append("packagist")
    if re.search(r"docker/build-push-action|docker push|ghcr\.io", wf):
        publish.append("container image")
    deploy = []
    if re.search(r"actions/deploy-pages|peaceiris/actions-gh-pages|mkdocs gh-deploy", wf):
        deploy.append("GitHub Pages")
    if any((repo / d).exists() for d in ("helm", "charts", "k8s", "kubernetes", "deploy", "kustomization.yaml")):
        deploy.append("kubernetes")
    if (repo / "Dockerfile").exists() and "container image" not in publish:
        deploy.append("container")
    if any((repo / f).exists() for f in ("fly.toml", "vercel.json", "netlify.toml", "app.yaml", "serverless.yml")):
        deploy.append("platform config")
    kind = "library" if publish and set(publish) <= {"maven", "npm", "pypi", "packagist"} else \
        "service" if deploy else "application"
    return {"versioning": scheme, "tag_format": tag_format, "latest_tag": tags[0] if tags else None,
            "version_source": vsrc, "version": vval, "release_tools": tools, "changelog": changelog, "ci": ci,
            "test_gate": gate, "branch_model": model, "publish": publish, "deploy": deploy, "kind": kind,
            "has_release_workflow": bool(re.search(r"(?s)on:.*?tags:|release:\s*\n\s*types", wf))}


def _default_gate(repo: Path) -> list:
    if (repo / "mvnw").exists():
        return ["./mvnw -B verify"]
    if (repo / "gradlew").exists():
        return ["./gradlew check"]
    if (repo / "pnpm-lock.yaml").exists():
        return ["pnpm install --frozen-lockfile", "pnpm test"]
    if (repo / "package-lock.json").exists():
        return ["npm ci", "npm test"]
    if (repo / "uv.lock").exists():
        return ["uv sync --frozen", "uv run pytest"]
    if (repo / "composer.json").exists():
        return ["composer install", "composer test"]
    if (repo / "tests").is_dir() and list(repo.glob("**/*.py"))[:1]:
        return ["python3 -m unittest discover -s tests"]
    return []


# --------------------------------------------------------------------------- recommendation and gaps

def recommend(found: dict) -> dict:
    scheme = found.get("versioning")  # a consistent scheme the repository already has is kept
    if scheme in (None, "other"):
        scheme = "semver" if found.get("kind") == "library" else "calver"
    tag_format = found.get("tag_format") or {"calver": "vYY.MM.PATCH", "pep440": "v" + PEP440_FORMAT}.get(
        scheme, "vMAJOR.MINOR.PATCH")
    tools = found.get("release_tools") or []
    if "release-please" in tools or "semantic-release" in tools:
        flow = tools[0]
    else:
        flow = "integration branch → one PR → merge → tag on main → GitHub release (dev release)"
    return {
        "versioning": scheme, "tag_format": tag_format,
        "version_source": found.get("version_source") or "git tags",
        "release_flow": flow,
        "changelog": "git-cliff → CHANGELOG.md (Keep a Changelog sections) and release notes"
        if "git-cliff" in tools or not found.get("changelog") else found.get("changelog"),
        "test_gate": found.get("test_gate") or [],
        "ci": (found.get("ci") or ["none"])[0],
        "branch_model": "trunk" if "gitflow" not in (found.get("branch_model") or "") else found["branch_model"],
        "publish": found.get("publish") or [], "deploy": found.get("deploy") or [], "kind": found.get("kind"),
        "release_train": None, "delete_branch_on_merge": True,
        "bump": "CalVer: `dev calver --next` (YY.MM.PATCH; PATCH restarts each month)" if scheme == "calver" else
        "PEP 440, the repository's own scheme: the next pre-release raises N (0.1.0a16 → 0.1.0a17); aN → bN → rcN "
        "→ the final release; .postN republishes a release, .devN marks a development build" if scheme == "pep440"
        else "SemVer from Conventional Commits: BREAKING CHANGE → major, feat → minor, fix/perf → patch",
    }


def gaps(found: dict, policy: dict, recorded: bool) -> list:
    """[(severity, gap, fix)] — what stands between this project and a release you can trust."""
    out = []
    if not recorded:
        out.append(("high", "no delivery policy recorded in the handoff repository",
                    "dev delivery apply --why \"…\" (records the recommended policy; adjust with --set key=value)"))
    if not found.get("ci"):
        out.append(("high", "no CI pipeline: nothing confirms a change on the shared branch",
                    "add .github/workflows/ci.yml running the test gate (pinned actions, read-only permissions)"))
    if not policy.get("test_gate"):
        out.append(("high", "no test gate: no command proves a change before it is pushed",
                    "define the test commands (dev delivery apply --set test_gate=\"…\") and add tests"))
    if found.get("versioning") in (None, "other") and found.get("kind") != "application":
        out.append(("medium", "no version scheme yet (no version tags or version file)",
                    f"adopt {policy['versioning']} and tag the first release ({policy['tag_format']})"))
    if not found.get("changelog"):
        out.append(("medium", "no CHANGELOG: users cannot see what changed between versions",
                    "add CHANGELOG.md (Keep a Changelog); generate entries with git-cliff"))
    if (found.get("publish") or found.get("kind") == "library") and not found.get("has_release_workflow") and \
            not set(found.get("release_tools") or []) & {"release-please", "semantic-release", "goreleaser"}:
        out.append(("medium", "publishes artifacts but has no release workflow: releases depend on one laptop",
                    "add a workflow that builds, signs and publishes on a v* tag"))
    if found.get("versioning") == "calver" and found.get("version_source") and found.get("version") and \
            not CALVER_RE.match(found["version"]) and found["version"] not in ("0.0.0", "0.0.0-dev"):
        out.append(("low", f"{found['version_source']} says {found['version']}, which does not follow the CalVer tags",
                    "align the version file with the tags during the next release"))
    if "gitflow" in (found.get("branch_model") or ""):
        out.append(("low", "a long-lived develop branch (GitFlow): devkit's standard is trunk-based delivery",
                    "keep it only if the team decided so; record that in the policy"))
    return out


# --------------------------------------------------------------------------- recorded policy

def policy_path(scope: str) -> Path:
    return POLICIES / f"{scope}.json"


def load(scope: str):
    return core.read_json(policy_path(scope), None)


def record(scope: str, policy: dict, why: str, a=None) -> dict:
    """Write policies/<scope>.json with a version and a changelog of who changed what and why; one commit."""
    if not why.strip():
        raise ValueError("recording a delivery policy needs the reason: --why \"…\"")
    a = a or work.actor()
    old = load(scope) or {}
    changed = sorted(k for k in FIELDS if old.get(k) != policy.get(k)) if old else ["created"]
    if isinstance(old.get("changelog"), list):  # written before the history field existed
        changed = [c for c in changed if c != "changelog"] or ["history moved"]
    data = {k: policy.get(k) for k in FIELDS}
    data["bump"] = policy.get("bump")
    data["version"] = int(old.get("version") or 0) + 1
    data["updated"], data["updated_by"] = work.now_iso(), f"{a['user']}@{a['host']}"
    prev = old.get("history") or ([] if isinstance(old.get("changelog"), str) else old.get("changelog") or [])
    data["history"] = [f"v{data['version']} · {data['updated']} · {work.actor_str(a)} · "
                       f"{', '.join(changed) or 'no change'} — why: {why}"] + prev[:49]
    f = policy_path(scope)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    work._commit([f], f"delivery policy {scope} v{data['version']} ({work.actor_str(a)}) — {why}")
    return data


def summary_line(scope: str) -> str:
    p = load(scope)
    if not p:
        return ""
    gate = "; ".join(p.get("test_gate") or [])[:120] or "none"
    return (f"Delivery policy (v{p.get('version')}): {p.get('versioning')} tags {p.get('tag_format')} · "
            f"release: {p.get('release_flow')} · test gate: {gate}" +
            (f" · release train: {p['release_train']}" if p.get("release_train") else ""))


def tag_violation(scope: str, tag: str):
    """A tag that does not follow the recorded scheme -> the reason, else None."""
    p = load(scope)
    if not p or not tag or not p.get("versioning"):
        return None
    if p["versioning"] == "calver":
        m = CALVER_RE.match(tag)
        if m and 1 <= int(m.group(2)) <= 12:
            return None
        return (f"`{tag}` does not follow this project's CalVer policy ({p.get('tag_format')}). The next version is "
                f"`dev calver --next`; tag it as v<that>, or change the policy with `dev delivery apply --set "
                f"versioning=… --why \"…\"`.")
    if p["versioning"] == "semver":
        if SEMVER_RE.match(tag):
            return None
        return (f"`{tag}` is not a SemVer tag ({p.get('tag_format')}). Use vMAJOR.MINOR.PATCH, or change the policy "
                f"with `dev delivery apply --set versioning=… --why \"…\"`.")
    if p["versioning"] == "pep440":
        if PEP440_RE.match(tag):
            return None
        return (f"`{tag}` is not a PEP 440 version tag ({p.get('tag_format')}). Use v{PEP440_FORMAT}, for example "
                f"v0.1.0a17, or change the policy with `dev delivery apply --set versioning=… --why \"…\"`.")
    return None


# Where GitHub's delete_branch_on_merge has no API devkit drives, what the developer sets instead.
OTHER_HOSTS = (
    ("gitlab", "GitLab: Settings › Merge requests › “Enable ‘Delete source branch’ option by default” "
               "(the project's remove_source_branch_after_merge)"),
    ("bitbucket", "Bitbucket: tick “Delete source branch after merge” when you merge each pull request"),
    ("azure", "Azure DevOps: choose “Delete <branch> after merging” when you complete each pull request"),
    ("visualstudio", "Azure DevOps: choose “Delete <branch> after merging” when you complete each pull request"),
)


def branch_cleanup(repo, want: bool):
    """Make the host delete a pull request's branch when it merges, as the policy says (GitHub's
    delete_branch_on_merge, through `gh api`). -> (the value to record: `want` when GitHub took it, else None; the
    line to print). Other hosts: what applies there, and nothing changed."""
    from . import gitx
    c = gitx.ctx(repo)
    if c is None or not c.has_origin:
        return None, "no origin remote: no host deletes merged branches here"
    if c.provider == "github":
        args = ["api", "-X", "PATCH", f"repos/{c.owner}/{c.repo}", "-F",
                f"delete_branch_on_merge={'true' if want else 'false'}"]
        if c.host != "github.com":
            args += ["--hostname", c.host]
        done, _, err = gitx.gh_do(args, c, timeout=60)
        if done:
            return want, ("GitHub now deletes the branch of each merged pull request (delete_branch_on_merge on)"
                          if want else "GitHub keeps the branch of each merged pull request (delete_branch_on_merge off)")
        return None, (f"GitHub's delete_branch_on_merge is unchanged: {err} · an admin of {c.owner}/{c.repo} turns it "
                      f"on in Settings › General › “Automatically delete head branches”")
    host = (c.host or "").lower()
    words = next((w for key, w in OTHER_HOSTS if key in host), None)
    if words:
        return None, f"set it on the host, devkit does not: {words}; dev branch prune --apply deletes what is merged"
    return None, f"{c.host or 'this remote'}: no setting devkit knows; dev branch prune --apply deletes merged branches"


def scope_for(repo) -> str:
    from .registry import main_of
    m = main_of(Path(repo)) or Path(repo)
    return core.project_identity(m, core.gitout(m, "config", "--get", "remote.origin.url"))[0]


def report(repo, scope: str = None) -> dict:
    scope = scope or scope_for(repo)
    found = detect(repo)
    recorded = load(scope)
    pol = recorded or recommend(found)
    return {"scope": scope, "detected": found, "recommended": recommend(found), "recorded": recorded,
            "gaps": gaps(found, pol, bool(recorded)), "generated": int(time.time())}
