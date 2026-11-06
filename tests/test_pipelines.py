# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""CI/CD pipelines: the YAML reader, `dev ci audit` on every host, the GitHub autofixes and `dev ci init`.

Hermetic: no network and no external linters (they are mocked where a test needs them). Run: python3 -m unittest -v
"""
import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import audit as audit_trail  # noqa: E402
from devkit import cli, pipelines as P  # noqa: E402

SHA_RE = re.compile(r"@[0-9a-f]{40} # v?\d")
CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1"


def repo(files: dict) -> Path:
    d = Path(tempfile.mkdtemp(prefix="devkit-pipelines-"))
    for name, text in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return d


def run_audit(root, **kw):
    kw.setdefault("external", False)
    return P.audit(root, **kw)


def ids(rep, severity_at_least=None) -> set:
    worst = P.RANK[severity_at_least] if severity_at_least else 99
    return {f["id"] for f in rep["findings"] if P.RANK[f["severity"]] <= worst}


def loud(rep) -> list:
    """Findings at medium or above: a clean pipeline has none."""
    return [(f["severity"], f["id"], f["file"], f["line"], f["message"]) for f in rep["findings"]
            if P.RANK[f["severity"]] <= P.RANK["medium"]]


# --------------------------------------------------------------------------- fixtures

PY_PROJECT = {"pyproject.toml": "[project]\nname = 'app'\nversion = '1.0.0'\n[dependency-groups]\n"
                                "dev = ['pytest', 'pytest-cov', 'ruff']\n[tool.ruff]\nline-length = 100\n",
              "uv.lock": "version = 1\n", "tests/test_app.py": "def test_app():\n    assert True\n"}
DEPENDABOT = ("version: 2\nupdates:\n  - package-ecosystem: github-actions\n    directory: /\n"
              "    schedule:\n      interval: weekly\n")

GOOD_GH = f"""name: ci
on:
  pull_request:
  push:
    branches: [main]
permissions:
  contents: read
concurrency:
  group: ${{{{ github.workflow }}}}-${{{{ github.ref }}}}
  cancel-in-progress: true
jobs:
  test:
    runs-on: ubuntu-24.04
    timeout-minutes: 20
    steps:
      - uses: {CHECKOUT}
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@07a047e608db84b3d8c06cd9a7c7dcc9c355c18e # v10.3.2
        with:
          enable-cache: true
      - run: uv sync --frozen
      - run: uv run ruff check .
      - run: |
          uv run pytest --cov --junitxml=test-results/junit.xml
      - uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2
        if: ${{{{ !cancelled() }}}}
        with:
          name: junit
          path: test-results/junit.xml
  security:
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    steps:
      - uses: {CHECKOUT}
        with:
          persist-credentials: false
      - run: gitleaks dir --redact .
      - run: semgrep scan --config p/default --error
      - run: osv-scanner scan source -r .
"""

BAD_GH = """name: bad
on:
  pull_request_target:
  push:
    branches: [main]
permissions: write-all
jobs:
  build:
    runs-on: [self-hosted, linux]
    container: node:latest
    strategy:
      matrix:
        node: [18, 20]
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
      - uses: some-org/some-action@v1
      - uses: actions/setup-python@v5
        with:
          python-version: 3.10
      - run: npm install
      - run: curl -sSL https://example.com/install.sh | bash
      - run: echo "${{ github.event.pull_request.title }}"
      - run: echo ${{ secrets.NPM_TOKEN }}
      - run: npm test
        continue-on-error: true
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id: ${{ secrets.AWS_KEY }}
          aws-secret-access-key: ${{ secrets.AWS_SECRET }}
      - run: kubectl apply -f k8s/
"""

GOOD_GITLAB = """include:
  - local: ci/security.yml
  - template: Jobs/SAST.gitlab-ci.yml
stages: [test, build]
default:
  interruptible: true
  timeout: 20m
  image: python:3.13-slim
.common:
  script:
    - echo start
.uv: &uv
  cache:
    key:
      files: [uv.lock]
    paths: [.cache/uv]
  before_script:
    - pip install uv==0.13.5
    - uv sync --frozen
lint:
  extends: .uv
  stage: test
  script:
    - uv run ruff check .
test:
  <<: *uv
  stage: test
  script:
    - !reference [.common, script]
    - uv run pytest --cov --junitxml=test-results/junit.xml
  coverage: '/TOTAL.*\\s(\\d+%)/'
  artifacts:
    reports:
      junit: test-results/junit.xml
"""
GITLAB_SECURITY = """secret-scan:
  stage: test
  image: ghcr.io/gitleaks/gitleaks:v8.30.1
  script:
    - gitleaks dir --redact .
dependency-scan:
  stage: test
  image: ghcr.io/google/osv-scanner:v2.7.0
  script:
    - osv-scanner scan source -r .
"""
BAD_GITLAB = """image: node:latest
test:
  script:
    - npm install
    - npm test || true
  retry: 2
  allow_failure: true
deploy:
  script:
    - kubectl apply -f k8s/
"""

GOOD_BITBUCKET = """image: python:3.13-slim
options:
  max-time: 20
definitions:
  steps:
    - step: &test
        name: test
        caches: [pip]
        script:
          - pip install -r requirements.txt
          - ruff check .
          - pytest --cov --junitxml=test-results/junit.xml
    - step: &scan
        name: scan
        image: ghcr.io/gitleaks/gitleaks:v8.30.1
        script:
          - gitleaks dir --redact .
          - semgrep scan --config p/default
          - osv-scanner scan source -r .
pipelines:
  pull-requests:
    "**":
      - parallel:
          - step: *test
          - step: *scan
  branches:
    main:
      - step: *test
"""
BAD_BITBUCKET = """image: maven:latest
pipelines:
  default:
    - step:
        script:
          - mvn deploy
          - pipe: atlassian/aws-s3-deploy:1
            variables:
              S3_BUCKET: x
  branches:
    main:
      - step:
          name: deploy
          script:
            - kubectl apply -f k8s/
"""

GOOD_AZURE = """trigger:
  batch: true
  branches:
    include: [main]
pr:
  branches:
    include: ["*"]
pool:
  vmImage: ubuntu-24.04
jobs:
  - job: test
    timeoutInMinutes: 20
    steps:
      - task: Cache@2
        inputs:
          key: 'npm | package-lock.json'
          path: $(npm_config_cache)
      - script: npm ci
      - script: npm run lint
      - script: npm test -- --coverage
      - task: PublishTestResults@2
        condition: succeededOrFailed()
        inputs:
          testResultsFiles: '**/junit.xml'
  - template: .azure-pipelines/security.yml
"""
AZURE_SECURITY = """jobs:
  - job: security
    timeoutInMinutes: 15
    steps:
      - script: docker run --rm -v "$PWD:/src" ghcr.io/gitleaks/gitleaks:v8.30.1 dir /src
      - script: semgrep scan --config p/default
      - script: osv-scanner scan source -r .
"""
BAD_AZURE = """pr:
  autoCancel: false
pool:
  vmImage: ubuntu-latest
steps:
  - script: npm install
  - script: npm test
    continueOnError: true
  - script: az webapp deploy --name app --src-path app.zip
"""
NODE_PROJECT = {"package.json": json.dumps({"name": "app", "private": True, "scripts": {"lint": "eslint .",
                                                                                        "test": "vitest"},
                                            "devDependencies": {"eslint": "9", "vitest": "1"}}),
                "package-lock.json": "{}", "app.test.js": ""}


# --------------------------------------------------------------------------- the YAML reader

class YamlTests(unittest.TestCase):
    def test_block_flow_scalars_and_lines(self):
        t = P.load_yaml('# c\nname: "ci: main"\non:\n  push:\n    branches: [ main, "rel/**" ]\nenv:\n'
                        '  A: plain # comment\n  B: \'it\'\'s\'\n  V: 3.10\n  N: 7\n  T: true\n  E: ~\n'
                        'steps:\n  - run: |\n      echo "a: b"\n      # kept\n    name: one\n  - - x\n')
        self.assertEqual(t["name"], "ci: main")
        self.assertEqual(t["on"]["push"]["branches"], ["main", "rel/**"])
        self.assertEqual((t["env"]["A"], t["env"]["B"], t["env"]["V"], t["env"]["N"], t["env"]["T"], t["env"]["E"]),
                         ("plain", "it's", "3.10", 7, True, None))  # 3.10 stays text
        self.assertEqual(t["steps"][0]["run"], 'echo "a: b"\n# kept\n')
        self.assertEqual(t["steps"][1], ["x"])
        self.assertEqual((t.klines["on"], t["env"].klines["V"], t["steps"].ilines), (3, 9, [14, 18]))

    def test_anchors_merge_keys_and_gitlab_reference(self):
        t = P.load_yaml(".base: &b\n  image: x:1\n  tags: [a]\njob:\n  <<: *b\n  tags: [b]\n  script:\n"
                        "    - !reference [.base, image]\n    - echo\n")
        self.assertEqual((t["job"]["image"], t["job"]["tags"]), ("x:1", ["b"]))
        self.assertIsInstance(t["job"]["script"][0], P.YTag)
        self.assertEqual(t["job"]["script"][0].value, [".base", "image"])

    def test_folded_literal_and_chomping(self):
        t = P.load_yaml("a: >-\n  one\n  two\n\n  three\nb: |+\n  kept\n\nc: |-\n  x\n  y\nd: >\n  x\n    more\n  y\n")
        self.assertEqual((t["a"], t["b"], t["c"]), ("one two\nthree", "kept\n\n", "x\ny"))
        self.assertEqual(t["d"], "x\n  more\ny\n")

    def test_invalid_yaml_is_reported_with_its_line(self):
        for text, line in (("name: Python: lint\n", 1), ("a: 1\nrun: echo \"a: b\"\n", 2), ("k: \"q\": x\n", 1),
                           ("a:\n  b: 1\n   c: 2\n", 3), ("x: *nope\n", 1)):
            with self.assertRaises(P.YamlError) as cm:
                P.load_yaml(text)
            self.assertEqual(cm.exception.line, line, text)
        self.assertEqual(P.load_yaml("run: echo a:b\n")["run"], "echo a:b")

    def test_gitlab_spec_header_document(self):
        t = P.load_yaml("spec:\n  inputs:\n    x:\n      default: 1\n---\njob:\n  script: [echo]\n")
        self.assertIn("job", t)


# --------------------------------------------------------------------------- audit, per host

class GitHubAuditTests(unittest.TestCase):
    def test_good_workflow_is_clean(self):
        r = repo(dict(PY_PROJECT, **{".github/workflows/ci.yml": GOOD_GH, ".github/dependabot.yml": DEPENDABOT}))
        rep = run_audit(r)
        self.assertEqual(loud(rep), [])
        self.assertEqual(P.exit_code(rep, strict=True), 0)
        self.assertEqual(rep["providers"], {"github": [".github/workflows/ci.yml"]})

    def test_bad_workflow_findings(self):
        r = repo({".github/workflows/bad.yml": BAD_GH, "package.json": "{}"})
        rep = run_audit(r, visibility=lambda: "public")
        want = {"permissions-broad", "privileged-checkout", "script-injection", "secret-exposure", "action-not-pinned",
                "non-lockfile-install", "unpinned-tool", "hidden-failure", "long-lived-cloud-keys",
                "deploy-unprotected", "no-job-timeout", "floating-image", "self-hosted-public", "fail-fast-implicit",
                "yaml-float-version", "no-pr-tests", "no-secret-scan", "no-sast", "no-actions-updates"}
        self.assertTrue(want <= ids(rep), want - ids(rep))
        by = {(f["id"], f["line"]): f for f in rep["findings"]}
        self.assertEqual(by[("script-injection", 24)]["severity"], "critical")  # privileged trigger
        self.assertEqual(by[("privileged-checkout", 15)]["severity"], "critical")
        self.assertEqual(by[("action-not-pinned", 18)]["severity"], "high")  # third-party
        self.assertEqual(by[("action-not-pinned", 15)]["severity"], "low")  # actions/*
        self.assertEqual(by[("secret-exposure", 25)]["severity"], "high")  # printed
        self.assertTrue(all(f["autofix"] for f in rep["findings"] if f["id"] == "action-not-pinned"))
        self.assertEqual(P.exit_code(rep), 1)
        for f in rep["findings"]:  # the documented shape
            self.assertTrue({"provider", "file", "line", "id", "category", "severity", "message", "fix",
                             "autofix"} <= set(f))
            self.assertIn(f["category"], P.CATEGORIES)

    def test_injection_on_an_unprivileged_trigger_is_high_and_env_is_safe(self):
        wf = GOOD_GH.replace("      - run: uv sync --frozen\n",
                             "      - run: uv sync --frozen\n      - run: echo ${{ github.head_ref }}\n"
                             "      - env:\n          TITLE: ${{ github.event.pull_request.title }}\n"
                             "        run: echo \"$TITLE\"\n")
        rep = run_audit(repo(dict(PY_PROJECT, **{".github/workflows/ci.yml": wf, ".github/dependabot.yml": DEPENDABOT})))
        inj = [f for f in rep["findings"] if f["id"] == "script-injection"]
        self.assertEqual([(f["severity"], f["line"]) for f in inj], [("high", 23)])

    def test_permissions_fix_is_not_offered_when_a_job_writes(self):
        wf = GOOD_GH.replace("permissions:\n  contents: read\n", "").replace(
            "      - run: uv sync --frozen\n", "      - run: uv sync --frozen\n      - run: gh release upload v1 dist/*\n")
        rep = run_audit(repo(dict(PY_PROJECT, **{".github/workflows/ci.yml": wf})))
        f = next(f for f in rep["findings"] if f["id"] == "permissions-missing")
        self.assertFalse(f["autofix"])
        self.assertIn("contents: write", f["fix"])

    def test_policy_test_gate_and_tag_format(self):
        wf = GOOD_GH.replace("  security:", "  release:\n    runs-on: ubuntu-24.04\n    timeout-minutes: 5\n"
                                             "    if: startsWith(github.ref, 'refs/tags/')\n    steps:\n"
                                             "      - run: gh release create \"$GITHUB_REF_NAME\"\n  security:")
        wf = wf.replace("    branches: [main]\n", "    branches: [main]\n    tags: [\"release-*\"]\n")
        r = repo(dict(PY_PROJECT, **{".github/workflows/ci.yml": wf, ".github/dependabot.yml": DEPENDABOT}))
        policy = {"tag_format": "vYY.MM.PATCH", "test_gate": ["uv run pytest", "make e2e"]}
        rep = run_audit(r, policy=policy)
        got = ids(rep)
        self.assertIn("test-gate-mismatch", got)
        self.assertIn("make e2e", next(f["message"] for f in rep["findings"] if f["id"] == "test-gate-mismatch"))
        self.assertIn("tag-format-mismatch", got)
        self.assertIn("release-without-tests", got)  # the release job does not need the tests
        self.assertTrue({"no-sbom", "no-signing", "publish-unverified"} <= got)
        ok = run_audit(r, policy={"tag_format": "vYY.MM.PATCH", "test_gate": ["python3 -m pytest", "uv run ruff check ."]})
        self.assertNotIn("test-gate-mismatch", ids(ok))  # python3 and uv run are normalized

    def test_no_pipeline(self):
        rep = run_audit(repo({"README.md": "x"}))
        self.assertEqual([f["id"] for f in rep["findings"]], ["no-pipeline"])
        self.assertIn("dev ci init", rep["findings"][0]["fix"])

    def test_unreadable_workflow_is_a_finding(self):
        rep = run_audit(repo({".github/workflows/ci.yml": "on: [push]\njobs:\n  t:\n    name: a: b\n"}))
        f = next(f for f in rep["findings"] if f["id"] == "parse-error")
        self.assertEqual((f["severity"], f["line"]), ("high", 4))


class GitLabAuditTests(unittest.TestCase):
    def test_good_pipeline_with_includes_extends_and_references(self):
        r = repo(dict(PY_PROJECT, **{".gitlab-ci.yml": GOOD_GITLAB, "ci/security.yml": GITLAB_SECURITY}))
        pls = P.build(r)
        self.assertEqual(pls[0]["files"], [".gitlab-ci.yml", "ci/security.yml"])
        jobs = {j["id"]: j for j in pls[0]["jobs"]}
        self.assertEqual(sorted(jobs), ["dependency-scan", "lint", "secret-scan", "test"])
        self.assertEqual(jobs["secret-scan"]["file"], "ci/security.yml")
        self.assertIn("uv sync --frozen", [s["run"] for s in jobs["lint"]["steps"]])  # from extends
        self.assertEqual(jobs["test"]["steps"][2]["run"], "echo start")  # !reference spliced in
        self.assertTrue(jobs["test"]["junit"] and jobs["test"]["cache"] and jobs["test"]["interruptible"])
        self.assertEqual(loud(run_audit(r)), [])

    def test_bad_pipeline_findings(self):
        rep = run_audit(repo({".gitlab-ci.yml": BAD_GITLAB, "package.json": "{}"}))
        want = {"floating-image", "non-lockfile-install", "hidden-failure", "retry-masks-flakiness", "no-auto-cancel",
                "no-dependency-cache", "deploy-unprotected", "no-secret-scan", "no-sast", "no-job-timeout"}
        self.assertTrue(want <= ids(rep), want - ids(rep))
        self.assertTrue(any(f["id"] == "no-auto-cancel" and "interruptible: true" in f.get("snippet", "")
                            for f in rep["findings"]))
        hidden = [f for f in rep["findings"] if f["id"] == "hidden-failure"]
        self.assertEqual(len(hidden), 2)  # allow_failure and `|| true`

    def test_missing_include_and_merge_request_rules(self):
        t = ("include:\n  - local: /nope.yml\nworkflow:\n  rules:\n    - if: $CI_PIPELINE_SOURCE == \"merge_request_event\"\n"
             "    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH\n    - if: $CI_COMMIT_TAG\nunit:\n  script: [pytest]\nrelease:\n"
             "  rules:\n    - if: $CI_COMMIT_TAG\n  script: [npm publish]\n")
        r = repo({".gitlab-ci.yml": t})
        jobs = {j["id"]: j for j in P.build(r)[0]["jobs"]}
        self.assertEqual((jobs["unit"]["on_pr"], jobs["release"]["on_pr"], jobs["release"]["on_tag"]), (True, False, True))
        rep = run_audit(r)
        self.assertIn("parse-error", ids(rep))
        self.assertIn("release-without-tests", ids(rep))  # same stage: publishes while the tests run
        no_tags = t.replace("    - if: $CI_COMMIT_TAG\n", "")
        jobs = {j["id"]: j for j in P.build(repo({".gitlab-ci.yml": no_tags}))[0]["jobs"]}
        self.assertFalse(jobs["release"]["on_tag"])  # the workflow rules never create a tag pipeline


class BitbucketAuditTests(unittest.TestCase):
    def test_good_pipeline(self):
        r = repo({"bitbucket-pipelines.yml": GOOD_BITBUCKET, "tests/test_a.py": "",
                  "requirements.txt": "httpx==0.27.0\nruff==0.17.2\npytest==9.1.1\npytest-cov==6.0.0\n"})
        rep = run_audit(r)
        self.assertEqual(loud(rep), [])
        self.assertIn("auto-cancel-unavailable", ids(rep))  # Bitbucket has none: said, as info
        jobs = P.build(r)[0]["jobs"]
        self.assertEqual(len({j["line"] for j in jobs}), 2)  # the anchored steps, reused in two pipelines

    def test_bad_pipeline_findings(self):
        rep = run_audit(repo({"bitbucket-pipelines.yml": BAD_BITBUCKET, "pom.xml": "<project/>"}))
        want = {"floating-image", "release-not-tag-driven", "unpinned-tool", "deploy-unprotected",
                "no-dependency-cache", "no-pr-tests"}
        self.assertTrue(want <= ids(rep), want - ids(rep))
        rel = next(f for f in rep["findings"] if f["id"] == "release-not-tag-driven")
        self.assertIn("every branch push", rel["message"])


class AzureAuditTests(unittest.TestCase):
    def test_good_pipeline_with_a_job_template(self):
        r = repo(dict(NODE_PROJECT, **{"azure-pipelines.yml": GOOD_AZURE, ".azure-pipelines/security.yml": AZURE_SECURITY}))
        pls = P.build(r)
        self.assertEqual([pl["file"] for pl in pls], ["azure-pipelines.yml"])  # the template is not a pipeline
        self.assertEqual(sorted(j["id"] for j in pls[0]["jobs"]), ["security", "test"])
        self.assertEqual(loud(run_audit(r)), [])

    def test_bad_pipeline_findings(self):
        rep = run_audit(repo(dict(NODE_PROJECT, **{"azure-pipelines.yml": BAD_AZURE})))
        want = {"no-auto-cancel", "runner-latest", "non-lockfile-install", "hidden-failure", "deploy-unprotected",
                "no-job-timeout", "no-dependency-cache"}
        self.assertTrue(want <= ids(rep), want - ids(rep))

    def test_stage_condition_keeps_publishing_off_pull_requests(self):
        t = ("stages:\n  - stage: publish\n    condition: not(eq(variables['Build.Reason'], 'PullRequest'))\n"
             "    jobs:\n      - job: push\n        steps:\n          - script: docker push x/y:1\n")
        jobs = P.build(repo({"azure-pipelines.yml": t}))[0]["jobs"]
        self.assertFalse(jobs[0]["on_pr"])


# --------------------------------------------------------------------------- external linters

ZIZMOR_JSON = json.dumps([
    {"ident": "unpinned-uses", "desc": "unpinned action reference", "determinations": {"severity": "High"},
     "locations": [{"symbolic": {"key": {"Local": {"given_path": "./.github/workflows/bad.yml"}}, "kind": "Primary",
                                 "annotation": "action is not pinned"},
                    "concrete": {"location": {"start_point": {"row": 17}}}}]},
    {"ident": "artipacked", "desc": "credential persistence", "determinations": {"severity": "Medium"},
     "locations": [{"symbolic": {"key": {"Local": {"given_path": ".github/workflows/bad.yml"}}, "kind": "Primary"},
                    "concrete": {"location": {"start_point": {"row": 14}}}}]}])
ACTIONLINT_JSON = json.dumps([{"message": "property \"x\" is not defined", "filepath": ".github/workflows/bad.yml",
                               "line": 9, "column": 3, "kind": "expression"}])


class ScanSnippetTests(unittest.TestCase):
    """Each missing scanner gets the snippet that adds that scanner, with its pinned image, not a generic one."""

    def test_each_scan_finding_carries_its_own_scanner(self):
        want = {"no-secret-scan": "gitleaks", "no-sast": "semgrep", "no-sca": "osv-scanner"}
        cases = {"gitlab": {".gitlab-ci.yml": BAD_GITLAB, "package.json": "{}"},
                 "bitbucket": {"bitbucket-pipelines.yml": BAD_BITBUCKET, "pom.xml": "<project/>"},
                 "azure": {"azure-pipelines.yml": BAD_AZURE, "package.json": "{}"}}
        for host, files in cases.items():
            rep = run_audit(repo(files))
            got = {f["id"]: f.get("snippet") or "" for f in rep["findings"] if f["id"] in want}
            self.assertEqual(set(got), set(want), host)
            for fid, tool in want.items():
                with self.subTest(host=host, finding=fid):
                    snip = got[fid]
                    self.assertIn(tool, snip)
                    self.assertIn("@sha256:", snip)  # the pinned image devkit's own pipeline uses
                    for other in set(want.values()) - {tool}:
                        self.assertNotIn(other, snip)
                    if host == "gitlab":  # a stage every GitLab pipeline has, not devkit's own lint stage
                        self.assertIn("stage: test", snip)


class ExternalLinterTests(unittest.TestCase):
    def test_zizmor_and_actionlint_are_merged(self):
        r = repo({".github/workflows/bad.yml": BAD_GH})

        def fake(args, cwd=None, timeout=30, **kw):
            return (13, ZIZMOR_JSON, "") if args[0] == "zizmor" else (1, ACTIONLINT_JSON, "")
        with mock.patch.object(P, "_have", lambda t: t in ("zizmor", "actionlint")), mock.patch.object(P, "_run", fake):
            rep = P.audit(r)
        pin = next(f for f in rep["findings"] if f["id"] == "action-not-pinned" and f["line"] == 18)
        self.assertEqual(pin["also"], ["zizmor"])  # one finding, not two
        self.assertNotIn("zizmor/unpinned-uses", ids(rep))
        z = next(f for f in rep["findings"] if f["id"] == "zizmor/artipacked")
        self.assertEqual((z["file"], z["line"], z["severity"], z["source"]), (".github/workflows/bad.yml", 15, "medium",
                                                                              "zizmor"))
        a = next(f for f in rep["findings"] if f["id"] == "actionlint/expression")
        self.assertEqual((a["line"], a["severity"]), (9, "high"))
        self.assertEqual(rep["tools"], {"actionlint": "ran: 1 finding(s)", "zizmor": "ran: 2 finding(s)"})

    def test_missing_tools_and_glab_without_login(self):
        r = repo({".github/workflows/ci.yml": GOOD_GH, ".gitlab-ci.yml": GOOD_GITLAB.replace("  - local: ci/security.yml\n", "")})
        calls = []

        def fake(args, cwd=None, timeout=30, **kw):
            calls.append(args)
            return (1, "", "not logged in") if args[:3] == ["glab", "auth", "status"] else (0, "", "")
        with mock.patch.object(P, "_have", lambda t: t == "glab"), mock.patch.object(P, "_run", fake):
            rep = P.audit(r)
        self.assertIn("not installed", rep["tools"]["actionlint"])
        self.assertIn("not signed in", rep["tools"]["glab"])
        self.assertNotIn(["glab", "ci", "lint"], calls)


# --------------------------------------------------------------------------- --fix

FIXABLE = """# The fixture's CI: comments must survive the fixes.
name: ci
on:
  pull_request:
  push:
    branches: [main]

# the jobs
jobs:
  test:
    runs-on: ubuntu-24.04  # the runner
    steps:
      - uses: actions/checkout@v4 # checkout
      - uses: "some-org/tool@v2"
      - uses: other/thing@main
      - run: make test
  reuse:
    uses: org/repo/.github/workflows/x.yml@v1
"""
SHA1, SHA2, SHA3, TAGOBJ = "1" * 40, "2" * 40, "3" * 40, "a" * 40


def fake_api(path):
    table = {
        "repos/actions/checkout/git/ref/tags/v4": {"ref": "refs/tags/v4", "object": {"type": "commit", "sha": SHA1}},
        "repos/actions/checkout/tags?per_page=100": [{"name": "v4", "commit": {"sha": SHA1}},
                                                     {"name": "v4.2.1", "commit": {"sha": "f" * 40}},
                                                     {"name": "v4.2.2", "commit": {"sha": SHA1}}],
        "repos/some-org/tool/git/ref/tags/v2": {"object": {"type": "tag", "sha": TAGOBJ}},
        f"repos/some-org/tool/git/tags/{TAGOBJ}": {"object": {"type": "commit", "sha": SHA2}},
        "repos/some-org/tool/tags?per_page=100": [{"name": "v2.0.3", "commit": {"sha": SHA2}}],
        "repos/org/repo/git/ref/tags/v1": {"object": {"type": "commit", "sha": SHA3}},
        "repos/org/repo/tags?per_page=100": [],
    }
    if path in table:
        return table[path], None
    return None, "gh: Not Found (HTTP 404)"


class FixTests(unittest.TestCase):
    def test_fixes_keep_comments_reaudit_clean_and_are_idempotent(self):
        r = repo({".github/workflows/ci.yml": FIXABLE, "Makefile": "test:\n\tpytest\n"})
        rep = run_audit(r)
        self.assertTrue({"permissions-missing", "no-auto-cancel", "no-job-timeout", "action-not-pinned"} <= ids(rep))
        res = P.apply_fixes(r, rep, api=fake_api)
        self.assertEqual(res["changed"], [".github/workflows/ci.yml"])
        text = (r / ".github/workflows/ci.yml").read_text()
        for comment in ("# The fixture's CI: comments must survive the fixes.", "# the jobs", "# the runner"):
            self.assertIn(comment, text)
        self.assertIn(f"uses: actions/checkout@{SHA1} # v4.2.2 · checkout", text)  # the precise version
        self.assertIn(f'uses: "some-org/tool@{SHA2}" # v2.0.3', text)  # annotated tag peeled, quotes kept
        self.assertIn(f"uses: org/repo/.github/workflows/x.yml@{SHA3} # v1", text)
        self.assertIn("uses: other/thing@main", text)  # a branch: left for a person
        self.assertTrue(any("not a tag" in s for s in res["skipped"]))
        self.assertIn("permissions:\n  contents: read", text)
        self.assertLess(text.index("permissions:"), text.index("# the jobs"))  # above the jobs' comment
        self.assertIn("cancel-in-progress: ${{ github.event_name == 'pull_request' }}", text)
        self.assertIn("    runs-on: ubuntu-24.04  # the runner\n    timeout-minutes: 30\n", text)
        self.assertNotIn("timeout-minutes", text.split("  reuse:")[1])  # reusable workflow jobs cannot have one
        P.load_yaml(text)
        after = run_audit(r)
        self.assertFalse({"permissions-missing", "no-auto-cancel", "no-job-timeout"} & ids(after))
        branch_line = text.splitlines().index("      - uses: other/thing@main") + 1
        self.assertEqual([f["line"] for f in after["findings"] if f["id"] == "action-not-pinned"], [branch_line])
        again = P.apply_fixes(r, after, api=fake_api)
        self.assertEqual(again["changed"], [])
        self.assertEqual((r / ".github/workflows/ci.yml").read_text(), text)

    def test_offline_pins_are_skipped_and_the_rest_applied(self):
        r = repo({".github/workflows/ci.yml": FIXABLE})
        res = P.apply_fixes(r, run_audit(r), api=lambda path: (None, "error connecting to api.github.com"))
        text = (r / ".github/workflows/ci.yml").read_text()
        self.assertIn("uses: actions/checkout@v4 # checkout", text)
        self.assertIn("permissions:", text)
        self.assertTrue(any("could not reach GitHub" in s for s in res["skipped"]))

    def test_policy_timeout_and_only_pull_request_workflows(self):
        wf = "on: pull_request\njobs:\n  t:\n    runs-on: ubuntu-24.04\n    steps:\n      - run: make test\n"
        r = repo({".github/workflows/ci.yml": wf})
        P.apply_fixes(r, run_audit(r), policy={"job_timeout_minutes": 12}, api=fake_api)
        text = (r / ".github/workflows/ci.yml").read_text()
        self.assertIn("timeout-minutes: 12", text)
        self.assertIn("cancel-in-progress: true", text)  # nothing else triggers it

    def test_resolve_pin_problems(self):
        self.assertEqual(P.resolve_pin("actions", "checkout", "v4", fake_api), (SHA1, "v4.2.2", None))
        sha, version, problem = P.resolve_pin("other", "thing", "main", fake_api)
        self.assertIsNone(sha)
        self.assertIn("not a tag", problem)


# --------------------------------------------------------------------------- dev ci init

STACKS = {
    "python": PY_PROJECT,
    "node": NODE_PROJECT,
    "java-maven": {"pom.xml": "<project><properties><java.version>21</java.version></properties>"
                              "<artifactId>testcontainers</artifactId></project>", "mvnw": "#!/bin/sh\n",
                   "src/test/java/ATest.java": ""},
    "java-gradle": {"build.gradle.kts": "plugins { checkstyle }\n", "gradlew": "#!/bin/sh\n",
                    "src/test/java/ATest.java": ""},
    "go": {"go.mod": "module x\n\ngo 1.25\n", "go.sum": "", "main.go": "package main\n\nfunc main() {}\n",
           "main_test.go": "package main\n"},
    "php": {"composer.json": json.dumps({"require-dev": {"phpunit/phpunit": "11", "phpstan/phpstan": "2"}}),
            "composer.lock": "{}", "tests/ATest.php": ""},
    "docker": dict(NODE_PROJECT, **{"Dockerfile": "FROM node:24\n"}),
}


class InitTests(unittest.TestCase):
    def test_pins_table_is_complete(self):
        pins = P._pins()
        for name, a in pins["actions"].items():
            self.assertRegex(a["sha"], r"^[0-9a-f]{40}$", name)
            self.assertTrue(a["version"], name)
        self.assertEqual(pins["actions"]["actions/checkout"]["sha"], "3d3c42e5aac5ba805825da76410c181273ba90b1")
        self.assertEqual(pins["actions"]["actions/setup-python"]["sha"], "6d4fd22b20465ccc0f7354476588e8df7c9ca0e0")
        for name, im in pins["images"].items():
            self.assertRegex(im.get("digest", ""), r"^sha256:[0-9a-f]{64}$", name)
        self.assertRegex(pins["binaries"]["cosign"]["sha256"], r"^[0-9a-f]{64}$")

    def test_the_policy_test_gate_and_timeout_shape_the_generated_pipeline(self):
        policy = {"tag_format": "vYY.MM.PATCH", "job_timeout_minutes": 45,
                  "test_gate": ["python3 -m compileall -q lib", "python3 -m unittest discover -s tests",
                                "/usr/bin/python3 -m unittest discover -s tests"]}
        r = repo(STACKS["python"])
        for provider in P.PROVIDERS:
            with self.subTest(provider=provider):
                gen, notes = P.generate(r, provider, policy=policy)
                main = gen[P.TARGETS[provider]]
                self.assertEqual(main.count("python -m unittest discover -s tests"), 1)  # one interpreter, once
                self.assertIn("python -m compileall -q lib", main)
                self.assertNotIn("/usr/bin/python3", main)  # the runner's system Python is not the configured one
                self.assertIn("45", main)
                rep = P.audit(r, provider=provider, policy=policy, only=set(gen), overlay=gen, external=False)
                self.assertNotIn("test-gate-mismatch", ids(rep))
                self.assertEqual(loud(rep), [])
                self.assertTrue(any("test gate" in n for n in notes))

    def test_every_provider_and_stack_parses_back_and_audits_clean(self):
        policy = {"tag_format": "vYY.MM.PATCH"}
        for stack, files in STACKS.items():
            r = repo(files)
            for provider in P.PROVIDERS:
                with self.subTest(stack=stack, provider=provider):
                    gen, _ = P.generate(r, provider, policy=policy)
                    for rel, text in gen.items():
                        self.assertNotIn("@{", text, rel)
                        self.assertIsNotNone(P.load_yaml(text), rel)
                    rep = P.audit(r, provider=provider, policy=policy, only=set(gen), overlay=gen, external=False)
                    self.assertEqual(loud(rep), [])
                    if provider == "github":
                        self.assertEqual(sorted(gen), [".github/dependabot.yml", ".github/workflows/ci.yml",
                                                       ".github/workflows/release.yml"])
                        for rel in (".github/workflows/ci.yml", ".github/workflows/release.yml"):
                            uses = re.findall(r"uses: (\S+.*)", gen[rel])
                            self.assertTrue(uses)
                            self.assertEqual([u for u in uses if not SHA_RE.search(u)], [], rel)
                        self.assertNotIn("enable-cache: true", gen[".github/workflows/release.yml"])
                        self.assertIn("cancel-in-progress", gen[".github/workflows/ci.yml"])

    def test_init_reads_the_stack(self):
        st = P.detect_stacks(repo(STACKS["python"]))[0]
        self.assertEqual((st["key"], st["pm"], st["install"]), ("python", "uv", ["uv sync --frozen"]))
        self.assertIn("--junitxml=test-results/junit.xml", st["test"][-1])
        mvn = P.detect_stacks(repo(STACKS["java-maven"]))[0]
        self.assertTrue(mvn["integration"] and mvn["test"][0].startswith("./mvnw -B -ntp test"))
        node = P.detect_stacks(repo({"package.json": json.dumps({"name": "a", "scripts": {"test": "x"}}),
                                     "pnpm-lock.yaml": "", ".nvmrc": "22\n"}))[0]
        self.assertEqual((node["pm"], node["install"], node["vars"]["node_file"]),
                         ("pnpm", ["pnpm install --frozen-lockfile"], ".nvmrc"))
        two = P.detect_stacks(repo(dict(STACKS["python"], **NODE_PROJECT)))
        self.assertEqual([s["prefix"] for s in two], ["python-", "node-"])

    def test_tag_triggers_follow_the_policy(self):
        r = repo(STACKS["go"])
        gh, _ = P.generate(r, "github", policy={"tag_format": "YY.MM.PATCH"})
        self.assertIn('tags: ["[0-9]+.[0-9]+.[0-9]+"]', gh[".github/workflows/release.yml"])
        gl, _ = P.generate(r, "gitlab", policy={"tag_format": "vMAJOR.MINOR.PATCH"})
        self.assertIn("$CI_COMMIT_TAG =~ /^v\\d+\\.\\d+\\.\\d+$/", gl[".gitlab-ci.yml"])

    def test_cmd_init_writes_refuses_and_dry_runs(self):
        r = repo(STACKS["python"])
        quiet = {"_policy": lambda root: None, "_visibility": lambda root: None, "_have": lambda t: False,
                 "_provider_from_remote": lambda root: "gitlab"}
        with mock.patch.multiple(P, **quiet):
            a = cli.build_parser().parse_args(["ci", "init", "--dry-run"])
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(P.cmd_init(a, str(r)), 0)
            self.assertFalse((r / ".gitlab-ci.yml").exists())
            self.assertIn("dry run", out.getvalue())
            a = cli.build_parser().parse_args(["ci", "init"])
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(P.cmd_init(a, str(r)), 0)
            self.assertIn("audit: clean", out.getvalue())
            self.assertIn("dev branch new ci/<topic>", out.getvalue())
            self.assertTrue((r / ".gitlab-ci.yml").exists())
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                self.assertEqual(P.cmd_init(a, str(r)), 1)
            self.assertIn("--force", err.getvalue())
            a = cli.build_parser().parse_args(["ci", "init", "--force", "--provider", "azure", "--stack", "python"])
            with redirect_stdout(io.StringIO()):
                self.assertEqual(P.cmd_init(a, str(r)), 0)
            self.assertTrue((r / "azure-pipelines.yml").exists())


# --------------------------------------------------------------------------- the command line

class CliTests(unittest.TestCase):
    def test_options_parse_next_to_the_local_ci_options(self):
        a = cli.build_parser().parse_args(["ci", "audit", "--fix", "--provider", "gitlab", "--strict", "--json"])
        self.assertEqual((a.query, a.fix, a.provider, a.strict, a.json), ("audit", True, "gitlab", True, True))
        a = cli.build_parser().parse_args(["ci", "init", "--dry-run", "--stack", "java-maven", "--force"])
        self.assertEqual((a.query, a.dry_run, a.stack, a.force), ("init", True, "java-maven", True))
        a = cli.build_parser().parse_args(["ci", "--run", "--dry-run"])  # gitx's options still work
        self.assertTrue(a.run and a.dry_run)

    def test_cmd_audit_json_and_fix_through_gh_api(self):
        r = repo({".github/workflows/ci.yml": FIXABLE})

        def fake_run(args, cwd=None, timeout=30, **kw):
            if args[:2] == ["gh", "api"]:
                obj, err = fake_api(args[2])
                return (0, json.dumps(obj), "") if obj is not None else (1, "", err)
            return 1, "", ""
        with mock.patch.multiple(P, _policy=lambda root: None, _visibility=lambda root: None,
                                 _have=lambda t: False, _run=fake_run):
            a = cli.build_parser().parse_args(["ci", "audit", "--json"])
            with redirect_stdout(io.StringIO()) as out:
                code = P.cmd_audit(a, str(r))
            data = json.loads(out.getvalue())
            self.assertEqual((code, data["exit"]), (1, 1))  # some-org/tool@v2: a third-party action on a tag
            self.assertGreaterEqual(data["summary"]["fixable"], 4)
            self.assertEqual((r / ".github/workflows/ci.yml").read_text(), FIXABLE)  # audit alone never writes
            a = cli.build_parser().parse_args(["ci", "audit", "--fix", "--strict"])
            with redirect_stdout(io.StringIO()) as out:
                code = P.cmd_audit(a, str(r))
            self.assertIn("Fixed (working tree, not committed)", out.getvalue())
            self.assertIn("dev pr create", out.getvalue())
            self.assertIn(SHA1, (r / ".github/workflows/ci.yml").read_text())

    def test_fix_prints_snippets_for_other_hosts(self):
        r = repo({".gitlab-ci.yml": BAD_GITLAB})
        with mock.patch.multiple(P, _policy=lambda root: None, _visibility=lambda root: None, _have=lambda t: False):
            a = cli.build_parser().parse_args(["ci", "audit", "--fix"])
            with redirect_stdout(io.StringIO()) as out:
                P.cmd_audit(a, str(r))
        self.assertIn("interruptible: true", out.getvalue())
        self.assertEqual((r / ".gitlab-ci.yml").read_text(), BAD_GITLAB)  # GitLab files are never edited

    def test_audit_trail_records_only_changes(self):
        self.assertFalse(audit_trail.audited(["ci", "audit"]))
        self.assertFalse(audit_trail.audited(["ci", "audit", "--json"]))
        self.assertTrue(audit_trail.audited(["ci", "audit", "--fix"]))
        self.assertTrue(audit_trail.audited(["ci", "init"]))
        self.assertFalse(audit_trail.audited(["ci", "init", "--dry-run"]))
        self.assertFalse(audit_trail.audited(["ci", "status"]))
        self.assertFalse(audit_trail.audited(["ci"]))


if __name__ == "__main__":
    unittest.main()
