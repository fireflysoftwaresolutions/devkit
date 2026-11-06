# CI/CD pipelines

This guide shows how devkit diagnoses, fixes, sets up, and watches CI/CD pipelines on GitHub Actions, GitLab CI,
Bitbucket Pipelines, and Azure Pipelines. CI confirms a change that already passed locally (see
[Delivery](delivery.md)), so the pipeline itself must be fast, trustworthy, and secure. The `cicd` skill teaches agents
the same method.

```bash
dev ci audit                 # diagnose every pipeline in the repository
dev ci audit --fix           # apply the safe GitHub Actions fixes; print snippets for the other hosts
dev ci init --dry-run        # the pipeline devkit would write for this stack and host
dev ci watch                 # follow the runs of HEAD to the end; diagnose a failure
```

## Diagnose a pipeline

`dev ci audit` reads every pipeline in the repository and reports what makes it slow, unreliable, or unsafe. Each
finding comes with its severity and the exact fix.

| Host | Files it reads |
| --- | --- |
| GitHub Actions | `.github/workflows/*.yml` |
| GitLab CI | `.gitlab-ci.yml` and its local `include:` files |
| Bitbucket Pipelines | `bitbucket-pipelines.yml` |
| Azure Pipelines | `azure-pipelines*.yml`, `.azure-pipelines/`, and the templates they use |

It also compares the pipelines with the project's delivery policy: the test gate CI must run and the tag format a
release must match (see [Delivery › The delivery policy](delivery.md#the-delivery-policy)).

| Option | Effect |
| --- | --- |
| `--provider github\|gitlab\|bitbucket\|azure` | Only one host's pipelines |
| `--strict` | Exit `1` on medium findings too (default: only high and critical) |
| `--fix` | Apply the safe fixes; see [Fix what is safe](#fix-what-is-safe) |
| `--json` | The report, the exit code, and the applied fixes as JSON |
| `-p PROJECT` | Another project (id or a unique part of its name) |

The command exits with `1` when a high or critical finding remains, `0` otherwise, and `2` outside a repository.

This is a real audit of a small Python project whose workflow pins nothing, sets no permissions, and hides a lint
failure (`ruff check . || true`):

```text
  GitHub Actions  .github/workflows/ci.yml
  actionlint: ran: 0 finding(s) · zizmor: ran: 4 finding(s)

  critical 0 · high 1 · medium 9 · low 4 · info 1  ·  5 fixable with --fix

  Security ─────────────────────────────────────────────────────────────────────
  HIGH     .github/workflows/ci.yml:11  action-not-pinned  [--fix]
           `astral-sh/setup-uv@v6` points at a tag, which its owner can move (third-party code runs with this job's token and secrets)
           fix: pin the full commit SHA with the version in a comment: `uses: owner/repo@<sha> # vX.Y.Z` (`dev ci audit --fix` does it)
  MEDIUM   .github/workflows/ci.yml:1  permissions-missing  [--fix] (also zizmor)
           no `permissions:`: 1 job(s) get the repository's default token, which may write to everything
           fix: add `permissions: contents: read` at the top of the workflow
  …
  Reliability ──────────────────────────────────────────────────────────────────
  MEDIUM   .github/workflows/ci.yml:13  hidden-failure
           `ruff check . || true` ignores its own failure
           fix: remove `|| true`; a gate must fail the job
  …
  Speed ────────────────────────────────────────────────────────────────────────
  MEDIUM   .github/workflows/ci.yml:7  no-job-timeout  [--fix]
           job `test` has no `timeout-minutes` (GitHub's default is 6 hours)
           fix: add `timeout-minutes: 30`
  …
  Next ─────────────────────────────────────────────────────────────────────────
  dev ci audit --fix   applies the [--fix] items to the working tree
  dev branch new ci/<topic> · dev commit · dev ci --run · dev push · dev pr create
```

`[--fix]` marks what `--fix` can change, and `(also zizmor)` marks a finding that zizmor reported too.

### Severities

| Severity | Meaning |
| --- | --- |
| `critical` | An outside contributor can run code or steal secrets, for example through a privileged trigger |
| `high` | A real supply chain, token scope, or gate problem: a mutable action, broad write permissions, no tests on pull requests |
| `medium` | A risk or a waste under common conditions: no scanning, no cache, no timeout, a hidden failure |
| `low` | A hardening or speed gap with little direct risk |
| `info` | An observation, such as a runner label that moves |

### Findings catalog

Each finding has an id, a category, and a default severity. A few severities depend on context; the notes after the
tables list them.

#### Security

| Finding | Severity | What it means |
| --- | --- | --- |
| `privileged-checkout` | critical | A privileged trigger checks out and runs pull request code |
| `permissions-broad` | high | The `GITHUB_TOKEN` can write more than the jobs need |
| `action-not-pinned` | high | An action referenced by a tag or branch that can be moved |
| `script-injection` | high | Untrusted text pasted into a shell script |
| `secret-exposure` | high | A secret printed or passed on a command line |
| `self-hosted-public` | high | A self-hosted runner that public pull requests can reach |
| `image-push-unscanned` | high | An image is pushed without a vulnerability scan |
| `permissions-missing` | medium | The `GITHUB_TOKEN` gets the repository's default permissions |
| `long-lived-cloud-keys` | medium | Long-lived cloud keys where OIDC is available |
| `no-secret-scan` | medium | No secret scanning in the pipeline |
| `no-sast` | medium | No static analysis (SAST) in the pipeline |
| `no-sca` | medium | No dependency vulnerability scanning (SCA) in the pipeline |
| `no-iac-scan` | medium | Infrastructure as code is not scanned |
| `no-container-scan` | medium | Container images are not scanned |
| `no-sbom` | medium | Releases ship without a software bill of materials (SBOM) |
| `no-signing` | medium | Releases are neither signed nor attested |
| `deploy-unprotected` | medium | A deploy without a protected environment or approval |
| `no-actions-updates` | low | Nothing keeps pinned actions up to date |

#### Reliability

| Finding | Severity | What it means |
| --- | --- | --- |
| `parse-error` | high | A pipeline file cannot be read |
| `floating-image` | medium | A container image without a fixed tag |
| `non-lockfile-install` | medium | Dependencies installed without the lockfile |
| `unpinned-tool` | medium | A tool installed without a fixed version |
| `hidden-failure` | medium | A failure that does not fail the pipeline |
| `retry-masks-flakiness` | medium | Retries that hide flaky tests |
| `yaml-float-version` | medium | A version YAML reads as a number (`3.10` becomes `3.1`) |
| `runner-latest` | info | A runner image label that moves (`ubuntu-latest`) |

#### Speed

| Finding | Severity | What it means |
| --- | --- | --- |
| `no-dependency-cache` | medium | Dependencies are downloaded again on every run |
| `no-auto-cancel` | medium | Superseded runs keep running after a newer push |
| `no-job-timeout` | medium | A hung job runs until the host's limit |
| `no-path-filters` | low | Every part of a monorepo is built for every change |
| `no-test-sharding` | low | A long test job runs on one machine |
| `no-junit-report` | low | No JUnit report: the host cannot show (or re-run) only the failed tests |
| `tool-reinstall` | low | The same tools are installed again in more than one step or job |
| `fail-fast-implicit` | low | A matrix relies on the default fail-fast |
| `auto-cancel-unavailable` | info | The host cannot cancel superseded runs (Bitbucket) |

#### Quality gates

| Finding | Severity | What it means |
| --- | --- | --- |
| `no-pipeline` | high | No CI pipeline: nothing confirms a change before it lands |
| `no-pr-tests` | high | Tests do not run on pull requests |
| `no-lint` | medium | No lint step |
| `no-typecheck` | medium | The type checker the project configures does not run |
| `test-gate-mismatch` | medium | CI does not run the delivery policy's test gate |
| `no-coverage` | low | Test coverage is not reported |

#### Delivery

| Finding | Severity | What it means |
| --- | --- | --- |
| `release-not-tag-driven` | medium | Releases are not driven by version tags |
| `tag-format-mismatch` | medium | Tag triggers do not match the policy's tag format |
| `release-without-tests` | medium | A release publishes without running the test gate |
| `publish-unverified` | low | Nothing checks the published artifact |

Severities that depend on context:

- `action-not-pinned` is high for a third-party action or any branch reference (`@main`), and low for a first-party
  action (`actions/*`, `github/*`) on a version tag.
- `script-injection` is critical on a privileged trigger (`pull_request_target`, `workflow_run`, `issue_comment`,
  `issues`) and high on the others.
- `secret-exposure` is high when the secret is printed, and medium when it is passed on a command line or traced with
  `set -x`.
- `no-lint` is medium when the project configures a linter that CI does not run, and low otherwise.
- `no-job-timeout` is low on GitLab, Bitbucket, and Azure, whose default limits are shorter than GitHub's six hours.
- `no-signing` is low on Bitbucket and Azure, which have no built-in attestations.
- `release-not-tag-driven` is high when a job publishes on every push.

### actionlint, zizmor, and glab

On GitHub, the audit also runs `actionlint` (workflow syntax, expressions, and the shell in `run:` steps) and
`zizmor --offline` (workflow security) when they are installed; on GitLab, it runs `glab ci lint` when `glab` is
installed and signed in. The line under the host names says what ran:

```text
  actionlint: ran: 0 finding(s) · zizmor: ran: 0 finding(s)
```

Their findings join the report with the tool's name in the id (`actionlint/expression`, `zizmor/artipacked`,
`glab/lint`). When zizmor reports what devkit already found (for example `unpinned-uses` and `action-not-pinned`, or
`template-injection` and `script-injection`), devkit keeps its own finding, which carries the fix, and adds
`(also zizmor)`. actionlint's syntax, expression, and credential findings are high, its shellcheck findings low. A
rejected GitLab file is the high finding `glab/lint`. Azure Pipelines has no offline linter, so its pipelines get
devkit's static checks only.

## Fix what is safe

`dev ci audit --fix` edits the GitHub workflows in your working tree. It never commits, keeps comments and
indentation, and audits again afterward. Four fixes are safe enough to apply without asking:

| Finding | Fix |
| --- | --- |
| `permissions-missing` | `permissions: contents: read` at the top; a job that needs more asks for it |
| `no-auto-cancel` | A `concurrency` group with `cancel-in-progress: true` for a pull-request-only workflow, otherwise `${{ github.event_name == 'pull_request' }}`, so runs on the default branch finish |
| `no-job-timeout` | `timeout-minutes` on each job, after `runs-on`, from the policy's `job_timeout_minutes` (default 30); jobs that call a reusable workflow are left alone |
| `action-not-pinned` | The tag's commit SHA, with the most precise version in a comment (`@v6` becomes `@<sha> # v6.8.0`) |

Pinning asks GitHub for each tag's commit through `gh api`, so `gh` must be signed in. A reference that is not a tag
(a branch) is skipped, and the report says to pin it to a release by hand. When GitHub cannot be reached, the pins are
skipped and the other fixes still apply.

The same project after `--fix`:

```text
  critical 0 · high 0 · medium 6 · low 3 · info 1

  Fixed (working tree, not committed) ──────────────────────────────────────────
  ✓ .github/workflows/ci.yml: permissions: contents: read
  ✓ .github/workflows/ci.yml: concurrency with cancel-in-progress: ${{ github.event_name == 'pull_request' }}
  ✓ .github/workflows/ci.yml: timeout-minutes: 30 on job `test`
  ✓ .github/workflows/ci.yml: astral-sh/setup-uv@v6 → d0cc045d04cc… # v6.8.0
  ✓ .github/workflows/ci.yml: actions/checkout@v4 → 11d5960a3267… # v4.4.0
```

```yaml
permissions:
  contents: read # least privilege: a job that needs more asks for it

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }} # a newer push replaces a PR run; other runs finish

jobs:
  test:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0
      - uses: astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e # v6.8.0
```

What remains (scanning, the hidden failure, unpinned tools) needs a decision about the project, so the report keeps
it with its fix.

On GitLab, Bitbucket, and Azure, `--fix` changes nothing and prints the snippet to add under each finding instead.
From a real GitLab audit:

```text
  MEDIUM   .gitlab-ci.yml:1  floating-image
           job `test` run in `node:latest`, which has no fixed tag and moves
           fix: use a fixed tag, better a digest (name:tag@sha256:…)
             image: node:<version>@sha256:<digest>
  MEDIUM   .gitlab-ci.yml:1  no-auto-cancel
           no job is `interruptible`: GitLab cannot cancel a pipeline that a newer push makes redundant
           fix: mark build and test jobs interruptible (not deploys), and keep the project's auto-cancel setting on
             default:
               interruptible: true
  LOW      .gitlab-ci.yml:3  no-job-timeout
           1 job(s) without `timeout:` (the project default applies, 1 hour unless changed)
           fix: set a default timeout and raise it only where needed
             default:
               timeout: 30m
```

### Job timeouts and the delivery policy

The timeout that `dev ci audit` suggests and `--fix` writes comes from the optional delivery-policy field
`job_timeout_minutes` (default 30), a whole number of minutes from 5 to 360. Set it when the project's slowest job
needs more:

```bash
dev delivery apply --set job_timeout_minutes=45 --why "integration tests take 35 minutes"
```

`dev ci init` uses the same field: when the policy sets it, every generated job gets that timeout. Without it, each
job keeps its own limit (15 minutes for lint, 20 for builds, 30 for tests and integration tests); the security and
release jobs keep 20 and 30.

## Set up a pipeline

`dev ci init` writes a pipeline for the detected stack and host. It reads the host from the `origin` remote (GitHub
when there is none) and the stack from the lockfiles and build files.

```bash
dev ci init --dry-run                   # print the files, and audit them, without writing anything
dev ci init                             # write them into the working tree
dev ci init --provider gitlab           # another host
dev ci init --stack java-gradle         # another stack: python, node, java-maven, java-gradle, go, php, docker
dev ci init --force                     # replace an existing pipeline file
```

It refuses to overwrite an existing pipeline without `--force`; run `dev ci audit` on that one instead, or compare it
with `--dry-run` first. It exits with `1` when the result has a medium or higher finding, or when `actionlint` rejects
it, and `0` otherwise.

| Host | Files |
| --- | --- |
| GitHub Actions | `.github/workflows/ci.yml`, `.github/workflows/release.yml`, and `.github/dependabot.yml` when the repository has no Dependabot or Renovate configuration |
| GitLab CI | `.gitlab-ci.yml` |
| Bitbucket Pipelines | `bitbucket-pipelines.yml` |
| Azure Pipelines | `azure-pipelines.yml` |

Every generated pipeline:

- runs on pull requests (merge requests), on pushes to the default branch, and on release tags in the policy's
  format (`vYY.MM.PATCH` by default);
- cancels runs that a newer push makes redundant (Bitbucket cannot);
- caches dependencies and installs them from the lockfile when the project has one;
- runs lint, the type checker the project configures, unit tests with a JUnit report, integration tests when the
  project has them (a `docker compose` test file on GitHub and Azure), and the build;
- has a security job: gitleaks (secrets), semgrep (code), osv-scanner (dependencies), trivy (filesystem and IaC), and
  zizmor (workflows, on GitHub); with a `Dockerfile`, it also builds the image and scans it with trivy before any push;
- on a release tag, runs the tests and scans again, then builds an SBOM with syft, writes checksums, attests the files
  (GitHub) or signs them with cosign (the other hosts), publishes them, and downloads them again to verify them;
- pins every action by commit SHA and every image by digest (`templates/ci/pins.json`), and sets least-privilege
  permissions and a timeout on every job.

When the project has a delivery policy, its test gate is what the test job runs: on devkit, whose gate is
`python3 -m compileall -q lib bin` and `unittest` on two Pythons, the job runs `python -m compileall -q lib bin` and
`python -m unittest discover -s tests` once, on the Python the setup step installs. The result audits clean by
construction, and the dry run ends like this:

```text
  • added .github/dependabot.yml: weekly, grouped updates keep the pinned actions current
✓ audit: clean (no medium or high findings; 1 low or info)

  Next ─────────────────────────────────────────────────────────────────────────
  dev branch new ci/<topic> · dev commit · dev ci --run · dev push · dev pr create
```

Deliver the new pipeline like any change, with those commands: one branch, one local run, one push.

### GitHub: ci.yml and release.yml

`ci.yml` runs the checks on pull requests and on the default branch, with caches. `release.yml` runs on release tags
only, and runs the test gate and the scans again **without caches**: a cache that a pull request could write must never
feed a release (zizmor calls this cache poisoning). Its `release` job is the only one with write permissions:

```yaml
  release:
    name: Release (tag)
    needs: [test, security]
    runs-on: ubuntu-24.04
    timeout-minutes: 30
    environment: release # add required reviewers in Settings → Environments to approve each release
    permissions:
      contents: write # create the GitHub release and upload its files
      id-token: write # sign the provenance attestation with this workflow's identity (OIDC)
      attestations: write # store the attestation
```

It archives the source (or collects the build artifacts), writes `sbom.spdx.json` and `SHA256SUMS`, attests every
file with `actions/attest-build-provenance`, creates the GitHub release, then downloads the files and checks them with
`sha256sum -c` and `gh attestation verify`.

`dependabot.yml` keeps the pinned actions and the dependencies current: one weekly, grouped pull request per ecosystem
(`github-actions` plus the stack's, such as `uv`, `npm`, `maven`, or `docker`), and new versions wait 14 days, except
security updates.

### Release signing on GitLab, Bitbucket, and Azure

| Host | Signing | What you set up |
| --- | --- | --- |
| GitLab CI | Keyless: cosign signs with the pipeline's own OIDC identity (`id_tokens: SIGSTORE_ID_TOKEN`, audience `sigstore`) and verifies each file against the project URL and the tag | Nothing to store; protect the `release` environment (**Settings → CI/CD → Protected environments**) to approve each release |
| Bitbucket Pipelines | cosign with a key pair; files are published to the repository's Downloads | Secured repository variables `BITBUCKET_ACCESS_TOKEN` (a repository access token with write access), `COSIGN_PRIVATE_KEY`, `COSIGN_PASSWORD`, and `COSIGN_PUBLIC_KEY` |
| Azure Pipelines | cosign with a key pair; files are kept as the `release` pipeline artifact | Secret variables `COSIGN_PRIVATE_KEY`, `COSIGN_PASSWORD`, and `COSIGN_PUBLIC_KEY` |

Create the key pair once with `cosign generate-key-pair` and store the three values as secrets on the host, never in
the repository. GitLab publishes to its generic package registry and creates the release with the job's own token.

## Watch every run

`dev ci watch` follows the CI/CD runs of one commit until they finish, on all four hosts. You rarely run it yourself:
after a `git push`, `dev push`, `gh pr create`, `dev pr create`, `gh pr merge`, `dev pr merge`, or `dev release` in a
main session, devkit starts it in the background (one watcher per repository, branch, and commit), and the result
comes back to that session.

```bash
dev ci watch                         # the current branch at HEAD
dev ci watch --branch feat/export --sha a1b2c3d --timeout 60
dev ci watch --json                  # the result as JSON
```

| Result | Exit code | Meaning |
| --- | --- | --- |
| `passed` | `0` | Every run of the commit succeeded |
| `failed` | `1` | A run failed; the result carries the diagnosis |
| `timeout` | `3` | Still running after `--timeout` minutes (default 90); follow it with `dev ci status --watch` |
| `none` | `4` | No run appeared within five minutes: the branch has no CI, or the host was unreachable |

For each failed run, devkit finds the failed jobs and steps and reads the failed log from its **first** error, because
the errors after it follow from it (`gh run view --log-failed` on GitHub, `glab ci trace` on GitLab; Bitbucket and
Azure give the job, the step, and the link). It sorts the error into one class and gives one next action:

| Class | Typical first error | Next action |
| --- | --- | --- |
| `pipeline` | Invalid workflow file, an unknown property, an action that cannot be resolved | `dev ci audit`, fix the workflow, check it with actionlint, push once |
| `infrastructure` | Network errors, rate limits, a lost runner, a full disk | Confirm it is not the change, then rerun once: `dev run rerun <id> --flaky "<evidence>"` |
| `dependencies` | A version that cannot be resolved, a lockfile out of date | Install from the lockfile locally, fix, push once |
| `test` | A failed assertion or test | `dev test run --failed`, fix for the real reason, `dev ci --run --resume`, push once |
| `lint` | A linter, formatter, or type checker | The same check locally (`dev ci --run`), fix, push once |
| `build` | A compile or packaging error | The build command CI runs, fix, `dev ci --run --resume`, push once |
| `unknown` | Anything else | Read the log from the first error (`dev run view <id> --failed`), reproduce, fix, push once |

### How a failure reaches Link

The session that pushed gets the result. When it has a progress run, the result arrives as a crew message from CI
watch to Morpheus, the main session, with its next action; otherwise it appears with the session's next tool call or
prompt. A
failure's message names the run, the failed job and step, the class, the next action, and the log link, followed by
the first error lines. It ends with the hand-off: when the fix is not trivial, Morpheus briefs Link, the crew's CI/CD
engineer (`dev crew brief link "Fix the failed CI run: <job>, <class>"`), at once in auto mode. A speculative fix is
never pushed; the failure is reproduced locally first. See [The crew](crew.md#how-devkit-coordinates-them).

Logs are untrusted: a pull request title or a contributor's test output can carry text that someone else controls.
The excerpt reaches agents marked as data, not instructions, with secrets redacted, and only the first error lines.

Every result is also saved in `~/.local/state/devkit/ci/watch/`, recorded in the audit trail (`ci.watch`), and sent as
a desktop notification. Set `DEVKIT_NO_CI_WATCH=1` to stop the automatic start.

## Take over an old project

Before you change an inherited pipeline, find out where it stands, and fix it in order of risk.

1. Run `dev ci audit` and `dev sec scan`, and read the delivery policy with `dev delivery`.
2. Turn the findings into work items: `dev task add "<finding>" --why "<risk>"`.
3. Fix security first (permissions, pinning, injection, secrets), then reliability (lockfile installs, hidden
   failures), then speed (caches, auto-cancel, timeouts), then the quality gates. `dev ci audit --fix` does the safe
   part on GitHub; on the other hosts, paste the snippets it prints.
4. When the pipeline is beyond repair, compare it with a generated one (`dev ci init --dry-run`) and replace it with
   `dev ci init --force`.
5. Run what CI runs locally (`dev ci --run`), then push once and let `dev ci watch` confirm the single run.

The work is done when `dev ci audit` reports no high or medium findings, or each remaining one has a task and a
reason, and the pipeline ran once on the pull request with a test report.

See also [Delivery](delivery.md), [Testing](testing.md), [The crew](crew.md), and
[Commands › Delivery](commands.md#delivery).
